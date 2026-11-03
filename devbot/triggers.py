"""Event triggers: cheap checks (no Claude) every few seconds that wake a brain the moment its service changes,
instead of waiting for the next scheduled run."""
import base64
import json
import re
import subprocess
import threading
import time
import urllib.parse
import urllib.error
import urllib.request
from datetime import datetime, timezone

from devbot import bus

EVERY = 3        # Dock badges are local and free: check every 3 seconds
API_EVERY = 10   # GitHub and Jira are web APIs (GitHub search allows 30 calls a minute): every 10th pass, so 30 seconds
live = set()     # brains whose trigger can currently see their service: these wait on data alone, with no timer
APPS = {"TeamsBrain": "Microsoft Teams", "OutlookBrain": "Microsoft Outlook", "RoamBrain": "Roam"}


def badge(app):
    """The unread count on an app's Dock icon ("" when none), or None if the app is not running."""
    out = subprocess.run(["lsappinfo", "info", "-only", "StatusLabel", app], capture_output=True, text=True, timeout=5).stdout
    m = re.search(r'"label"="([^"]*)"', out)
    return m.group(1) if m else ("" if "StatusLabel" in out else None)


def github():
    """Changes when a notification arrives. GitHub never notifies you of your own comments or pushes, so DevBot's own
    work does not wake GitHubBrain (a PR's updatedAt did, costing a quiet run each time)."""
    note = subprocess.run(["gh", "api", "notifications", "--jq", "[.[].updated_at] | max"], capture_output=True, text=True, timeout=30)
    return None if note.returncode else note.stdout.strip()


def _env():
    try:
        lines = open(bus.ROOT / bus.CONFIG["credentials_env"]).read().splitlines()
    except (KeyError, OSError):
        return {}
    return {k: v.strip("\"'") for k, _, v in (l.partition("=") for l in lines) if k and not k.startswith("#")}


def jira():
    """Changes when any ticket assigned to you is created or updated."""
    e = _env()
    if not all(e.get(k) for k in ("JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN")):
        return None
    q = urllib.parse.urlencode({"jql": "assignee = currentUser() ORDER BY updated DESC", "maxResults": 1, "fields": "updated"})
    auth = base64.b64encode(f"{e['JIRA_EMAIL']}:{e['JIRA_API_TOKEN']}".encode()).decode()
    req = urllib.request.Request(f"{e['JIRA_BASE_URL'].rstrip('/')}/rest/api/3/search/jql?{q}",
                                 headers={"Authorization": f"Basic {auth}", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        issues = json.load(r).get("issues", [])
    return f"{issues[0]['fields']['updated']}@{issues[0]['key']}" if issues else ""  # timestamp first: newer sorts higher


CHECKS = {**{b: (lambda app=app: badge(app)) for b, app in APPS.items()}, "GitHubBrain": github, "JIRABrain": jira}
WHAT = {"TeamsBrain": "Teams", "OutlookBrain": "Outlook", "RoamBrain": "Roam", "GitHubBrain": "GitHub", "JIRABrain": "Jira"}


def grew(new, old):
    """New activity, not cleanup: a higher unread count or a newer timestamp. Marking things read never wakes a brain."""
    count = lambda s: int(s or 0) if (s or "0").isdigit() else None
    a, b = count(new), count(old)
    return a > b if a is not None and b is not None else new > old


FORCE_GAP = 600  # a check flapping between working and failing may force a run at most every 10 minutes
_forced = {}


def forced(brain):
    if time.time() - _forced.get(brain, 0) >= FORCE_GAP:
        _forced[brain] = time.time()
        bus.wake[brain].set()


def poll(last, broken, only=None):
    """One pass over the checks (all, or just `only`). Wakes a brain whose service shows new activity, even mid-run:
    the wake stays set, so the brain goes again right after its current run instead of missing it."""
    for brain, check in CHECKS.items():
        if only is not None and brain not in only:
            continue
        try:
            sig = check()
        except Exception as e:  # a flaky check must never stop the others
            if brain in live:
                live.discard(brain)
                forced(brain)  # its trigger went blind: run now, then fall back to the timer
            if brain not in broken:
                broken.add(brain)
                bus.log(brain, f"Couldn't check {WHAT[brain]} for changes ({e}); checking on a timer until it's back.", "warning")
            continue
        broken.discard(brain)
        if sig is None:
            if brain in live:
                live.discard(brain)
                forced(brain)  # it was waiting on a trigger that just went blind: run now, then fall back to the timer
            continue
        live.add(brain)
        if brain in last and grew(str(sig), str(last[brain])):
            bus.log(brain, f"Noticed new activity in {WHAT[brain]}; checking now.")
            bus.fire(WHAT[brain], brain, "trigger")
            bus.wake[brain].set()
        last[brain] = sig


def watch():
    last, broken, n, err = {}, set(), 0, None
    while True:
        try:
            poll(last, broken, None if n % API_EVERY == 0 else set(APPS))
            err = None
        except Exception as e:  # this thread dying would leave every trigger-driven brain asleep
            if repr(e) != err:  # log each new failure once, not every 3 seconds
                err = repr(e)
                bus.log("CentralBrain", f"Trigger check failed ({e}); trying again.", "warning")
        n += 1
        time.sleep(EVERY)


def start():
    threading.Thread(target=watch, daemon=True, name="triggers").start()


_roam_seen = {}  # chat id -> lastMessageTime already copied, so only changed chats are fetched (Roam rate-limits hard)
_roam_names = {}  # user id -> name


def _roam(path, key, tries=4):
    """One Roam API call; a 429 waits (Retry-After, else 5 s, 10 s, ...) and tries again."""
    req = urllib.request.Request(f"https://api.ro.am/v1/{path}", headers={"Authorization": f"Bearer {key}"})
    for n in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code != 429 or n == tries - 1:
                raise
            time.sleep(float(e.headers.get("Retry-After") or 5 * (n + 1)))


def roam_chats(days=7):
    """Every Roam chat active in the last `days`, newest message plus the whole chat (latest 200) as its thread, read
    straight from the Roam API in code: the key never reaches a model. Only chats with new messages are fetched."""
    key = _env().get("ROAM_API_KEY")
    if not key:
        return []
    since = time.time() - days * 86400
    out = []
    for c in _roam("chat.list", key).get("chats") or []:
        last = c.get("lastMessageTime") or ""
        try:
            fresh = datetime.fromisoformat(last).timestamp() >= since
        except ValueError:
            fresh = False
        if not fresh or _roam_seen.get(c["id"]) == last:
            continue
        msgs = _roam(f"chat.history?chatId={urllib.parse.quote(c['id'])}&limit=200", key).get("messages") or []
        for uid in {m.get("userId") for m in msgs} - set(_roam_names) - {None}:
            try:
                _roam_names[uid] = (_roam(f"user.info?id={urllib.parse.quote(uid)}", key).get("name") or "?")
            except urllib.error.HTTPError:
                _roam_names[uid] = "Bot"  # bots and apps have no user record
        thread = sorted(({"from": _roam_names.get(m.get("userId"), "?"),
                          "at": datetime.fromtimestamp(m["timestamp"] / 1e6, timezone.utc).isoformat().replace("+00:00", "Z"),
                          "text": m.get("text") or ""} for m in msgs if m.get("timestamp")), key=lambda t: t["at"])
        if thread:
            out.append({"id": f"roam-{c['id']}-{thread[-1]['at']}", "source": "Roam", "chat": c.get("name") or "?",
                        **{k: thread[-1][k] for k in ("from", "at", "text")}, "reply": "", "thread": thread})
        _roam_seen[c["id"]] = last
    return out


def m365(kind):
    """Teams chats ("teams") or Outlook mail ("outlook") from the last 7 days, read in code with DevBot's signed-in Teams
    web session (devbot/m365.mjs). Raises when that session can't serve them, so the caller falls back to a model run."""
    r = subprocess.run(["node", str(bus.ROOT / "devbot" / "m365.mjs"), str(bus.CONFIG["teams_browser_port"]), bus.CONFIG["owner"]],
                       capture_output=True, text=True, timeout=180)
    try:
        d = json.loads(r.stdout or "{}")
    except ValueError:
        d = {}
    if r.returncode or not d.get(kind) and d.get("errors"):
        raise RuntimeError((d.get("errors") or [r.stderr.strip()[-200:] or "no output"])[0])
    return d[kind]
