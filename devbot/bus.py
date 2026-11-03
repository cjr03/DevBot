"""Shared in-process state: brain status, live activity, metrics, logs, chat, and the persisted ticket queue. Stdlib only."""
import collections
import json
import logging
import os
import pathlib
import queue
import re
import subprocess
import threading
import time
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parent.parent
BRAINS = ["CentralBrain", "JIRABrain", "OutlookBrain", "RoamBrain", "CodeBrain", "TeamsBrain", "GitHubBrain"]
CONFIG = json.loads((ROOT / "config.json").read_text())
CONFIG["repos_dir"] = os.path.expanduser(CONFIG["repos_dir"])  # "~/code" works too
TICKET = re.compile(r"[A-Z][A-Z0-9]{1,9}-\d{1,6}")
_SECRET = re.compile(r"(gh[pousr]_|github_pat_|xox[abp]-|sk-ant-)[A-Za-z0-9_\-]+|://[^/@\s]+:[^/@\s]+@|Bearer\s+\S+")
QUEUE_FILE = ROOT / "state" / "queue.json"
SPEND_FILE = ROOT / "state" / "spend.json"

_lock = threading.RLock()
STARTED = time.time()
_spent = collections.deque()  # (time, usd) of this session's runs, for the last-hour rate
try:
    _saved = json.loads(SPEND_FILE.read_text())
except (FileNotFoundError, ValueError):
    _saved = {}
_all_time = float(_saved.get("total_usd") or 0)
_day = {"day": _saved.get("day") or time.strftime("%Y-%m-%d"), "usd": float(_saved.get("day_usd") or 0)}


def logged_today():
    """Today's spend summed from the engine logs ("... cost $0.31."), so a lost save can never under-report the day."""
    day, total = time.strftime("%b %d"), 0.0
    for f in (ROOT / "logs").glob("*.log"):
        for line in f.read_text(errors="ignore").splitlines():
            m = re.search(r"cost \$(\d[0-9.]*)", line) if line.startswith(day) else None
            if m:  # a quoted "cost $" with no amount (e.g. in the chat log) is not spend
                total += float(m.group(1).rstrip("."))
    return total


if _day["day"] == time.strftime("%Y-%m-%d"):
    _day["usd"] = max(_day["usd"], logged_today())
status = {b: {"state": "idle", "last_tick": 0.0, "ticks": 0, "errors": 0, "cost_usd": 0.0, "actions": 0, "note": "",
              "activity": "", "run_started": 0.0, "next_run": 0.0, "durations": collections.deque(maxlen=20), "ticket": ""}
          for b in BRAINS}
logs = {b: collections.deque(maxlen=500) for b in BRAINS}
logs["All"] = collections.deque(maxlen=5000)  # longer: a ticket's log is cut from it
error_times = collections.deque(maxlen=5000)  # epoch of each error, history included; the header counts the last hour
_WORDS = {"  · ": "tool", "Done: ": "ok", "Warning: ": "warning", "Problem: ": "error"}


def load_logs(folder):
    """The tail of each engine's log file, so the log popups survive a reload: (sort key, (ts, brain, level, msg))."""
    out = []
    for b in BRAINS:
        try:
            lines = (folder / f"{b}.log").read_text(errors="ignore").splitlines()[-2000:]
        except FileNotFoundError:
            continue
        for line in lines:
            m = re.match(r"(\w{3} \d\d) (\d\d:\d\d:\d\d)  (.*)", line)
            if not m:
                continue  # a continuation of a multi-line message
            msg, level = m.group(3), "info"
            for word, lv in _WORDS.items():
                if msg.startswith(word):
                    msg, level = msg[len(word):], lv
                    break
            out.append((time.strptime(m.group(1) + " " + m.group(2), "%b %d %H:%M:%S")[1:6], (m.group(2), b, level, msg)))
    return out


for _key, _line in sorted(load_logs(ROOT / "logs"), key=lambda e: e[0]):
    logs[_line[1]].append(_line)
    logs["All"].append(_line)
    if _line[2] == "error":
        error_times.append(time.mktime((time.localtime().tm_year, *_key, 0, 0, -1)))
CHATS_FILE = ROOT / "state" / "chats.json"  # every CentralBrain conversation: name, Claude session and history
_chats_lock = threading.Lock()  # one writer of CHATS_FILE at a time
_DEFAULT_NAME = re.compile(r"New Chat|Chat \d+")  # renamed after your first message; Chat N: older saves


def _blank(name, session=None, log=(), unread=False):
    # log: entries {"ts","who","kind": say|step,"text","status": run|ok|fail}; turn: the reply in progress, for "Working…"
    return {"name": name, "session": session, "unread": unread, "log": collections.deque(log, maxlen=400),
            "turn": {"started": 0.0, "steps": 0, "thinking": False}}


def _load_chats():
    """Saved chats, or one chat that keeps the single conversation older versions saved in chat_session.txt."""
    try:
        d = json.loads(CHATS_FILE.read_text())
        loaded = {str(cid): _blank(str(c["name"]), c.get("session"),  # a step cut off by the restart is not still running
                                   ({**e, "status": "ok" if e.get("status") == "run" else e.get("status", "ok")} for e in c.get("log", [])),
                                   bool(c.get("unread"))) for cid, c in d["chats"].items()}
        if loaded:
            return loaded, d.get("active") if d.get("active") in loaded else next(iter(loaded))
    except (FileNotFoundError, ValueError, KeyError, TypeError, AttributeError):
        pass
    try:
        old = (ROOT / "state" / "chat_session.txt").read_text().strip() or None
    except FileNotFoundError:
        old = None
    return {"main": _blank("New Chat", old)}, "main"


chats, active = _load_chats()
chat, turn = chats[active]["log"], chats[active]["turn"]  # the chat on screen; the UI reads these
events = collections.deque(maxlen=3000)  # (time, src, dst, label): every communication, drawn by the live graph
handoffs = queue.Queue()  # tickets for CodeBrain
_pending = {}  # key -> ticket, mirrored to QUEUE_FILE so a restart never loses one
paused = threading.Event()
held = set()  # brains you paused from the dashboard: each starts nothing new until you resume it
working = {}  # key -> {"started", "activity"}: tickets CodeBrain is implementing right now, several at once
failed = {}  # key -> ticket whose implementation ended in an error, so the dashboard can retry it
retries = 0  # failed tickets or blocked brains you retried this session
DONE_FILE = ROOT / "state" / "done.json"
ticket_cost = collections.Counter(_saved.get("tickets") or {})  # key -> USD spent implementing it, kept across restarts
reload = threading.Event()  # set by CentralBrain when changed code is ready; the UI then restarts the process
wake = {b: threading.Event() for b in BRAINS}
rush = {b: threading.Event() for b in BRAINS}  # set with wake by Run now and CentralBrain: the next run skips the minimum gap


def run_now(brain):
    """Run the brain at once. Triggers and timers only set wake, and the brain waits out MIN_GAP between runs."""
    rush[brain].set()
    wake[brain].set()

for _b in BRAINS + ["Chat"]:
    _h = logging.FileHandler(ROOT / "logs" / f"{_b}.log")
    _h.setFormatter(logging.Formatter("%(asctime)s  %(word)s%(message)s", "%b %d %H:%M:%S"))
    logging.getLogger(_b).addHandler(_h)
    logging.getLogger(_b).setLevel(logging.INFO)
    logging.getLogger(_b).propagate = False


def redact(msg):
    return _SECRET.sub("[redacted]", str(msg))


def log(brain, msg, level="info"):
    msg = redact(msg)[:2000]
    line = (time.strftime("%H:%M:%S"), brain, level, msg)
    with _lock:
        logs["All"].append(line)  # a ticket's log is cut from here
        if level == "error":
            error_times.append(time.time())
        if brain not in logs:  # "You": only the ticket's log, no brain's
            return
        logs[brain].append(line)
    word = {"tool": "  · ", "ok": "Done: ", "warning": "Warning: ", "error": "Problem: "}.get(level, "")
    logging.getLogger(brain).log({"warning": 30, "error": 40}.get(level, 20), msg, extra={"word": word})


def save_chats():
    with _lock:
        d = {"active": active, "chats": {cid: {"name": c["name"], "session": c["session"], "unread": c["unread"], "log": list(c["log"])}
                                         for cid, c in chats.items()}}
    with _chats_lock:  # write then rename: a reload mid-save never leaves a half-written file
        tmp = CHATS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(d))
        tmp.replace(CHATS_FILE)


def select_chat(cid):
    """Show chat cid."""
    global chat, turn, active
    with _lock:
        if cid not in chats:
            return
        active = cid
        chats[cid]["unread"] = False
        chat, turn = chats[cid]["log"], chats[cid]["turn"]
    save_chats()


def new_chat():
    with _lock:
        cid = os.urandom(4).hex()
        chats[cid] = _blank("New Chat")
    select_chat(cid)
    return cid


def delete_chat(cid):
    """Forget chat cid. The neighbour takes its place on screen; closing the last chat leaves a fresh one."""
    with _lock:
        if cid not in chats:
            return
        ids = list(chats)
        del chats[cid]
        nxt = active if active != cid else (ids[ids.index(cid) + 1] if ids.index(cid) + 1 < len(ids) else ids[ids.index(cid) - 1])
    if not chats:
        new_chat()
    else:
        select_chat(nxt)


def say(who, text, cid=None):
    with _lock:
        cid = cid or active
        c = chats.get(cid)
        if c is None:
            return  # the chat was closed while its reply was still arriving
        if who == "You" and _DEFAULT_NAME.fullmatch(c["name"]) and not any(e["who"] == "You" for e in c["log"]):
            first = " ".join(text.split())  # a new chat is named after your first message
            c["name"] = first[:28] + "…" if len(first) > 28 else first or c["name"]
            if first:
                threading.Thread(target=_title, args=(cid, first, c["name"]), daemon=True).start()
        c["log"].append({"ts": time.strftime("%H:%M:%S"), "who": who, "kind": "say", "text": redact(text), "status": "ok", "id": ""})
        c["unread"] = c["unread"] or cid != active
        name = c["name"]
    logging.getLogger("Chat").info(f"[{name}] {who}: {redact(text)}", extra={"word": ""})
    save_chats()


def summarize(text):
    """A 2-5 word title for a chat from the account in use; "" if it can't answer."""
    from devbot import claude  # claude imports bus
    out = claude.sealed("Reply with only a 2-5 word title (no quotes, no full stop) summarising this request:\n\n" + text[:2000],
                        model="haiku", timeout=60)
    return " ".join(out.split()).strip("\"'.")[:40]


def _title(cid, first, placeholder):
    """Swap the cut-off first message for a short summary, unless the chat was renamed or closed meanwhile."""
    name = summarize(first)
    with _lock:
        c = chats.get(cid)
        if not name or c is None or c["name"] != placeholder:
            return
        c["name"] = name
    save_chats()


def step_start(step_id, text, cid=None):
    """A CentralBrain step began: shown in chat with a spinner until step_end."""
    with _lock:
        c = chats.get(cid or active)
        if c is None:
            return
        c["log"].append({"ts": time.strftime("%H:%M:%S"), "who": "CentralBrain", "kind": "step", "text": redact(text),
                         "status": "run", "id": step_id})
        c["turn"]["steps"] += 1
        c["turn"]["thinking"] = False


def step_end(step_id, ok=True, cid=None):
    with _lock:
        c = chats.get(cid or active)
        for e in reversed(c["log"] if c else ()):
            if e["id"] == step_id:
                e["status"] = "ok" if ok else "fail"
                break
    save_chats()


def end_steps(cid=None):
    """A turn finished: nothing it started is still running."""
    with _lock:
        c = chats.get(cid or active)
        if c is None:
            return
        for e in c["log"]:
            if e["status"] == "run":
                e["status"] = "ok"
        c["turn"].update(started=0.0, steps=0, thinking=False)
    save_chats()


def tidy_note(note):
    """Drops message ids and shortens ISO timestamps to local HH:MM in engine status notes."""
    note = re.sub(r"\b(?:msg|message|id)\s*#?\d{9,}\b,?\s*|\b\d{12,}\b,?\s*", "", note)
    hm = lambda m: datetime.fromisoformat(m[0].replace("Z", "+00:00")).astimezone().strftime("%H:%M")
    return re.sub(r"\(\s*\)\s*", "", re.sub(r"\d{4}-\d\d-\d\dT\d\d:\d\d(?::\d\d(?:\.\d+)?)?(?:Z|[+-]\d\d:\d\d)", hm, note))


def set_state(brain, state, note=None, **fields):
    with _lock:
        s = status[brain]
        s["state"] = state
        if note is not None:
            s["note"] = tidy_note(redact(note))[:300]
        if state == "running" and not s["run_started"]:
            s["run_started"] = time.time()
        if state != "running":
            s["run_started"], s["activity"] = 0.0, ""
        s.update(fields)


def activity(brain, text, key=None):
    with _lock:
        status[brain]["activity"] = redact(text)[:160]
        if key in working:
            working[key]["activity"] = status[brain]["activity"]


def acted(brain):
    """Count one real side effect (message, email, ticket change, commit, PR) the moment it happens."""
    with _lock:
        status[brain]["actions"] += 1


def ticked(brain, result, duration=0.0, key=None):
    """Record one completed tick. result: {"cost_usd","actions","error"}."""
    with _lock:
        s = status[brain]
        s["ticks"] += 1
        s["last_tick"] = time.time()
        cost = float(result.get("cost_usd") or 0)
        s["cost_usd"] += cost
        if cost:
            global _all_time
            _all_time += cost
            _spent.append((time.time(), cost))
            if _day["day"] != time.strftime("%Y-%m-%d"):
                _day.update(day=time.strftime("%Y-%m-%d"), usd=0.0)
            _day["usd"] += cost
            if key or s["ticket"]:
                ticket_cost[key or s["ticket"]] += cost
            SPEND_FILE.parent.mkdir(exist_ok=True)
            tmp = SPEND_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps({"total_usd": _all_time, "day": _day["day"], "day_usd": _day["usd"], "tickets": ticket_cost}))
            tmp.replace(SPEND_FILE)
        s["errors"] += 1 if result.get("error") else 0
        if duration:
            s["durations"].append(duration)
    fire(brain, "CentralBrain", "report")


def fire(src, dst, label=""):
    events.append((time.time(), src, dst, redact(label)[:80]))


def _save_queue():
    tmp = QUEUE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(list(_pending.values()), indent=1))
    os.replace(tmp, QUEUE_FILE)


def handoff(ticket, src):
    """Queue a Jira ticket for CodeBrain. Dedup by key while queued or in progress. Returns True if newly queued."""
    key = ticket.get("key") if isinstance(ticket, dict) else None
    if not (isinstance(key, str) and TICKET.fullmatch(key)):
        return False
    t = {"key": key, "summary": str(ticket.get("summary") or "")[:500], "context": str(ticket.get("context") or "")[:20000], "from": src,
         "at": ticket.get("at") or time.time()}
    with _lock:
        if key in _pending:
            return False
        _pending[key] = t
        _save_queue()
    handoffs.put(t)
    fire(src, "CodeBrain", f"handoff {key}")
    log(src, f"Sent {key} to CodeBrain to implement.")
    return True


def handoff_done(key):
    with _lock:
        if _pending.pop(key, None) is not None:
            _save_queue()


def restore_queue():
    """Re-enqueue tickets that were queued or mid-implementation when the app last stopped."""
    try:
        saved = json.loads(QUEUE_FILE.read_text())
    except (FileNotFoundError, ValueError):
        return 0
    n = 0
    for t in saved if isinstance(saved, list) else []:
        n += handoff(t, t.get("from") or "restore")
    return n


def pending():
    with _lock:
        return list(_pending.values())


def spend():
    """(today, last 60 minutes, all time) in USD. Today and all time survive restarts and reloads."""
    with _lock:
        while _spent and _spent[0][0] < time.time() - 3600:
            _spent.popleft()
        today = _day["usd"] if _day["day"] == time.strftime("%Y-%m-%d") else 0.0
        return today, sum(c for _, c in _spent), _all_time


_parsed = {}  # path -> (stat, parsed): the dashboard asks for these files eight times a second; parse once per change


def _json_file(path, default, post=lambda d: d):
    try:
        st = path.stat()
        key = (st.st_mtime_ns, st.st_size)
    except OSError:
        return default
    hit = _parsed.get(path)
    if hit is None or hit[0] != key:
        try:
            hit = _parsed[path] = (key, post(json.loads(path.read_text())))
        except (OSError, ValueError):
            return default
    return hit[1]


def _written(path, value):
    """Keep the parse of what this process just wrote: coherent at once (a same-size rewrite within one clock tick has the
    same stat), and no re-read of our own writes."""
    try:
        st = path.stat()
        _parsed[path] = ((st.st_mtime_ns, st.st_size), value)
    except OSError:
        _parsed.pop(path, None)


def done_today():
    """Ticket keys CodeBrain finished today (local date), oldest first."""
    return [d for d in _json_file(DONE_FILE, []) if d.get("day") == time.strftime("%Y-%m-%d")]


def finished(key, took=0.0, cost=0.0, outcome="", verdict=None, waiting_on=""):
    """Record a completed ticket run: how long it took, what it cost, its one-line result, and the verdict it gave
    (done / waiting / blocked / nothing, and who or what it waits on; None from a run that gave none)."""
    with _lock:
        done = done_today() + [{"key": key, "day": time.strftime("%Y-%m-%d"), "at": time.time(), "took": took,
                                "cost": cost, "outcome": redact(outcome)[:300], "verdict": verdict, "waiting_on": redact(waiting_on)[:60]}]
        tmp = DONE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(done))
        tmp.replace(DONE_FILE)
        _written(DONE_FILE, done)


def rate(seconds):
    """USD per hour over the last `seconds` (at most an hour)."""
    with _lock:
        return sum(c for t, c in _spent if t > time.time() - seconds) * 3600 / seconds


def gate(brain):
    """Block while every engine, or just this brain, is paused."""
    paused.wait()
    while brain in held:
        time.sleep(1)


def snapshot():
    with _lock:
        return {b: {**s, "durations": list(s["durations"])} for b, s in status.items()}, handoffs.qsize()


if (ROOT / "state" / "ClaudeBrain.json").exists() and not (ROOT / "state" / "CodeBrain.json").exists():  # renamed CodeBrain
    (ROOT / "state" / "ClaudeBrain.json").rename(ROOT / "state" / "CodeBrain.json")


def memory_path(brain):
    return ROOT / "state" / f"{brain}.json"


def load_memory(brain):
    try:
        return json.loads(memory_path(brain).read_text())
    except (FileNotFoundError, ValueError):
        return {}


def save_memory(brain, mem):
    data = json.dumps(mem, indent=1)
    if len(data) > 200000:
        log(brain, "Its notes grew too large to save; keeping the previous notes.", "warning")
        return
    tmp = memory_path(brain).with_suffix(f".{threading.get_ident()}.tmp")  # CodeBrain's parallel runs save at once: one tmp each
    tmp.write_text(data)
    os.replace(tmp, memory_path(brain))


MESSAGES_FILE = ROOT / "state" / "messages.json"


def _lines(thread):
    return [t if isinstance(t, dict) else {"text": str(t)} for t in thread or []]


def norm(name):
    """One spelling of a person or chat: "Garcia, Alex (Contractor)" and "Alex Garcia" match; emoji dropped."""
    s = re.sub(r"\(.*?\)", "", str(name or ""))
    if s.count(",") == 1:
        last, first = s.split(",")
        s = f"{first} {last}"
    return " ".join(re.findall(r"\w+", s.lower()))


def _minute(at):
    """The UTC minute of a timestamp, so 21:29:00Z and 14:29-07:00 are one moment; unparsable text as given."""
    at = str(at or "")
    try:
        d = datetime.fromisoformat(re.sub(r"(\.\d{6})\d+", r"\1", at.replace("Z", "+00:00")))
        return (d.astimezone(timezone.utc) if d.tzinfo else d).strftime("%Y-%m-%dT%H:%M")
    except ValueError:
        return at[:16]


def _line_key(t):
    return (_minute(t.get("at")), norm(t.get("from")), " ".join(str(t.get("text", "")).split())[:80])


def merge_threads(threads):
    """One transcript. The longest copy keeps its order; a line only another copy has is inserted by time.
    A photo on any copy is kept."""
    groups = [_lines(t) for t in threads if t]
    if not groups:
        return []
    base = list(max(groups, key=len))
    by = {}
    for t in base:
        by.setdefault(_line_key(t), t)
    for g in groups:
        for t in g:
            k = _line_key(t)
            if k in by and t.get("images") and not by[k].get("images"):
                by[k] = dict(by[k], images=t["images"])
    lines = [by[_line_key(t)] for t in base]
    known = set(by)
    for g in groups:
        for t in g:
            k = _line_key(t)
            if k in known:
                continue
            known.add(k)
            at = str(t.get("at") or "")
            i = next((i for i, x in enumerate(lines) if at and str(x.get("at") or "") > at), len(lines))
            lines.insert(i, t)
    return lines


_CHANNELS = {"Teams", "Roam", "Outlook"}


def _msg_key(m):
    """Same message whether or not an engine gave its id: the chat (or, with no chat, the source), the UTC minute, the opening.
    Not the sender: engines spell one author several ways ("Cole Roberts (Bot)", "Cole's assistant (bot)")."""
    where = norm(m.get("chat")) or str(m.get("source", "")).title()
    return (where, _minute(m.get("at")), " ".join(str(m.get("text", "")).split())[:60])


def _merge_msg(keep, new):
    """Two reports of one message. The fuller reply and thread win; Teams, Roam or Outlook beats a mislabelled source."""
    out = dict(keep)
    if len(new.get("reply") or "") > len(out.get("reply") or ""):
        out["reply"] = new["reply"]
    merged = merge_threads([out.get("thread"), new.get("thread")])
    if merged:
        out["thread"] = merged
    for field in ("chat", "id", "from", "at", "text"):
        if not out.get(field) and new.get(field):
            out[field] = new[field]
    if new.get("source") in _CHANNELS and out.get("source") not in _CHANNELS:
        out["source"] = new["source"]
    return out


def _collapse(rows):
    """One record per message. Same id, or same chat, sender, minute and opening, collapse."""
    order, by_id, by_key = [], {}, {}

    def remember(slot, ids=()):
        if slot.get("id"):
            by_id[str(slot["id"])] = slot
        for i in ids:
            if i:
                by_id[str(i)] = slot
        by_key[_msg_key(slot)] = slot

    for m in rows:
        prev = by_id.get(str(m.get("id") or "")) or by_key.get(_msg_key(m))
        if prev is None:
            order.append(m)
            remember(m)
            continue
        slot = _merge_msg(prev, m)
        order[order.index(prev)] = slot
        for k, v in list(by_id.items()):
            if v is prev:
                by_id[k] = slot
        for k, v in list(by_key.items()):
            if v is prev:
                by_key[k] = slot
        remember(slot, [m.get("id"), prev.get("id")])
    return order


def messages():
    """Messages to the owner on Teams, Roam and Outlook with the reply sent, newest first.
    Copies of one message (two engines, or the same note saved twice) come back as one record. Parsed and collapsed once
    per change: the file is megabytes, and the dashboard asked for it eight times a second."""
    return _json_file(MESSAGES_FILE, [], lambda rows: _collapse([m for m in rows if m.get("from") != "Magic Minutes"]))


def add_messages(new, brain):
    """Record what an engine reported in "messages"; a message seen again (same id, or same chat, sender, minute and
    opening) is merged, keeping the reply and the fuller thread. The last 2000 are kept."""
    new = [dict(m, source=str(m.get("source") or brain.removesuffix("Brain")).title()) for m in new or [] if isinstance(m, dict) and m.get("text")
           and m.get("from") != "Magic Minutes"]  # meeting summaries live in state/minutes, not the chat
    if not new:
        return
    with _lock:
        rows = _collapse(new + messages())
        rows.sort(key=lambda m: str(m.get("at") or ""), reverse=True)  # backfills interleave by time
        tmp = MESSAGES_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(rows[:2000]))
        tmp.replace(MESSAGES_FILE)
        _written(MESSAGES_FILE, rows[:2000])  # already collapsed and filtered: what a fresh parse would give
