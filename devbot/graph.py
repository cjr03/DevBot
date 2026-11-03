"""What the system is doing for you: the ticket queue (active ticket pinned and expanded), each ticket's communications link,
and one line per brain with its controls. Clickable
parts use Textual @click markup, which runs a Dashboard action."""
import pathlib
import re
import textwrap
import time
from datetime import datetime

from rich.console import Group
from rich.markdown import Markdown
from rich.markup import escape
from rich.padding import Padding
from rich.text import Text

from devbot import bus, claude, theme

WORKERS = ["CodeBrain", "GitHubBrain", "JIRABrain", "TeamsBrain", "OutlookBrain", "RoamBrain"]
SPIN = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
GENERAL = "Other activity"
TARGET = {"CodeBrain": "its ticket", "GitHubBrain": "GitHub PRs and CI", "JIRABrain": "Jira", "TeamsBrain": "Teams chats",
          "OutlookBrain": "Outlook mail", "RoamBrain": "Roam"}
# Colours (TEAL, MINT, CORAL, ...) are set by theme.apply.


def derive():
    """Three states only. Idle is quiet; working and blocked are the ones you need to see."""
    global STATE
    STATE = {"idle": ("○", DIM), "working": (None, TEAL), "blocked": ("■", CORAL)}


def state(s):
    return "working" if s["state"] == "running" else "blocked" if s["state"] == "error" else "idle"


def dot(st):
    icon, colour = STATE[st]
    return f"[bold {colour}]{icon or SPIN[int(time.time() * 8) % 10]}[/]"


def ago(t):
    s = max(0, int(time.time() - t))
    return f"{s}s" if s < 60 else f"{s // 60}m" if s < 3600 else f"{s // 3600}h{s % 3600 // 60:02d}m"


_last_state, _changed = {}, {}


def flash(key, st):
    """True for two seconds after this row's state changes, so the change catches the eye."""
    if _last_state.get(key, st) != st:
        _changed[key] = time.time()
    _last_state[key] = st
    return time.time() - _changed.get(key, 0) < 2


def hang(text, indent, width):
    """Escaped text wrapped to the width, every continuation line starting at the same column as the first."""
    lines = textwrap.wrap(str(text), max(10, width - indent)) or [""]
    return escape(("\n" + " " * indent).join(lines))  # escaped whole: a tag split across lines can't slip through


def money(usd):
    return f"${usd:.2f}" if usd else "—"


def dur(s):
    s = int(s)
    return f"{s}s" if s < 60 else f"{s // 60}m{s % 60:02d}s" if s < 3600 else f"{s // 3600}h{s % 3600 // 60:02d}m"


JIRA = {"assigned": {}, "worked": {}, "blocked": {}}  # filled by the dashboard from tracker.mine() every few minutes


def finished_tiers(queued=()):
    """Today's finished runs, latest per ticket and newest first, split into (awaiting, done). A ticket queued again
    is shown in the table instead."""
    latest = {}
    for d in bus.done_today():
        latest[d["key"]] = d
    closed = {k for k, (st, _) in JIRA["worked"].items() if section(st) in ("DONE", "DEPLOYED")}  # Jira says done: done, whatever the run said
    runs = [d for d in reversed(list(latest.values())) if d["key"] not in queued and (d["key"] in closed or d["key"] not in JIRA["worked"])]  # reassigned: gone
    done = lambda d: d["key"] in closed or tag(d) == "DONE"
    return [d for d in runs if not done(d)], [d for d in runs if done(d)]


WAITS = {"CI PENDING": "CI", "BLOCKED": "BLOCKED", "NOT PUSHED": "UNPUSHED", "CONFLICT": "CONFLICT", "AWAITING": "REVIEW"}
PEOPLE = re.compile(r"\b([A-Z][a-z]+)(?:(?:,| and|/) ?([A-Z][a-z]+))? (?:still )?(?:need|needs|has|have|haven't|must|to sign)")


def waiting(d):
    """(what it waits for, who or what unblocks it, whether DevBot is watching for the unblock) for an AWAITING run.
    GitHubBrain re-checks every open PR, so CI, conflicts, pushes and reviews are watched; a blocked ticket waits on a
    person and nothing watches it. Who: the run's own "waiting_on", else read from the words of its summary."""
    o = d.get("outcome") or ""
    st = tag(d)
    what = WAITS.get(st, st)
    if st == "CI PENDING":
        n = re.search(r"\b(\d+)/(\d+)\b", o)
        counts = {k: int(v) for v, k in re.findall(r"(\d+) (passed|pending|failed)", o)}
        done = f"{counts.get('passed', 0)}/{sum(counts.values())}" if counts else f"{n[1]}/{n[2]}" if n else ""
        return f"CI {done}".strip(), "CI watcher", "GitHubBrain" not in bus.held
    if st == "BLOCKED":
        who = PEOPLE.search(o)
        return what, d.get("waiting_on") or (", ".join(x for x in who.groups() if x) if who else "you"), False
    return what, d.get("waiting_on") or ("reviewer" if st == "AWAITING" else "me"), "GitHubBrain" not in bus.held


VERDICTS = {"done": "DONE", "blocked": "BLOCKED", "nothing": "AWAITING"}  # a run's own verdict as the table's tag; "waiting" depends on waiting_on


def tag(d):
    """A finished run's real condition: the verdict it gave ("outcome" done / waiting / blocked / nothing, with
    "waiting_on"), else read from the words of its summary (runs recorded before verdicts existed, or a bare summary).
    DONE only when merged or closed with nothing outstanding; anything pending or unclear is not done."""
    if isinstance(d, str):
        d = {"outcome": d}
    v = d.get("verdict")
    if v == "waiting":
        return "CI PENDING" if re.search(r"\bci\b", d.get("waiting_on") or "", re.I) else "AWAITING"
    if v in VERDICTS:
        return VERDICTS[v]
    o = (d.get("outcome") or "").lower()
    if "blocked" in o or "can't" in o or "cannot" in o:
        return "BLOCKED"
    if "not yet pushed" in o or "not pushed" in o or "before push" in o or "locally" in o:
        return "NOT PUSHED"
    if "conflict" in o and "resolved" not in o:
        return "CONFLICT"
    if any(w in o for w in ("waiting", "running", "pending", "watcher", "will notify", "haven't", "not yet")):
        return "CI PENDING"
    if any(w in o for w in ("merged into master", "merged into main", "pr merged", "was merged", "closed", "all checks pass", "ci passed", "ci is green", "ci green")):
        return "DONE"
    return "AWAITING"


def link(label, action):
    return f"[@click={action}][{TEAL}]{label}[/][/]"


def button(label, action, colour):
    """A clickable pill in markup, drawn like the brains' Pause/Run/Stop buttons."""
    return f"[@click={action}][{colour}]\ue0b6[/][bold {INK} on {colour}]{label}[/][{colour}]\ue0b4[/][/]"


def ticket_controls(k, running=False):
    """Running: pause, stop and logs; otherwise run and logs."""
    ops = (("⏸", "pause", YELLOW), ("■", "stop", CORAL)) if running else (("▶", "run", MINT),)
    pills = [button(g, f"app.ticket_op('{op}', '{k}')", c) for g, op, c in ops] + [button("≡", f"app.show_comms('{k}')", BLUE)]
    return " " * (4 * (3 - len(pills))) + " ".join(pills)  # padded to three pills on the left, so they sit flush right like the brains'


def fit(text, n):
    """Text that fits in about n characters without cutting a phrase: whole sentences, dropping the rest.
    A first sentence longer than n is kept whole and wraps."""
    text = " ".join(str(text).split())
    if len(text) <= n:
        return text
    parts = re.split(r"(?<=[.;!?])\s+", text)
    kept = parts[0]
    for part in parts[1:]:
        if len(kept) + 1 + len(part) > n:
            break
        kept += " " + part
    return kept


def outcome(d):
    """A finished run's summary without the ticket key it opens with (the row already shows it)."""
    return re.sub(rf"^(?:{re.escape(d['key'])}\s*[:·-]\s*)+", "", d.get("outcome") or "") or "finished"


def clean(msg):
    return msg.strip().lstrip("·").strip()


def claude_lines():
    """CodeBrain's log as (ts, level, msg, ticket): each line belongs to the ticket it last started."""
    out, current = [], None
    for ts, _, level, msg in list(bus.logs["CodeBrain"]):
        m = bus.TICKET.search(msg)
        if m and (msg.startswith("Started implementing") or msg.startswith("Picking")):
            current = m.group()
        if m and msg.startswith(m.group() + " · "):  # a tool step tagged with its ticket (tickets run in parallel)
            msg = msg[len(m.group()) + 3:]
        out.append((ts, level, msg, (m.group() if m else current)))
    return out


TAGS = re.compile(rf"^((?:{bus.TICKET.pattern} )*{bus.TICKET.pattern}) · ")
PR = re.compile(r"(?:PR #|PR |#)(\d+)\b")


def attributed():
    """Every brain's logged lines as (ts, brain, level, msg, ticket), once for each ticket the line is about: the ones it
    is tagged with or names, the ticket of a PR it names, and for CodeBrain the ticket it last started."""
    out, current, prs = [], None, {}
    for ts, brain, level, msg in list(bus.logs["All"]):
        tags = TAGS.match(msg)
        if tags:  # a tool step tagged with its tickets
            msg = msg[tags.end():]
        keys = list(dict.fromkeys((tags.group(1).split() if tags else []) + bus.TICKET.findall(msg)))
        for n in PR.findall(msg):
            if len(keys) == 1:
                prs.setdefault(n, keys[0])  # "DEMO-3288: PR #877" teaches which ticket PR #877 is
            elif not keys and n in prs:
                keys = [prs[n]]
        if brain == "CodeBrain":
            if keys and (msg.startswith("Started implementing") or msg.startswith("Picking")):
                current = keys[0]
            keys = keys or ([current] if current else [])
        out += [(ts, brain, level, msg, k) for k in keys or [GENERAL]]
    return out


def after(lines, last):
    """The lines after `last` (the last one already shown); all of them when it has rolled off or nothing was shown."""
    for i in range(len(lines) - 1, -1, -1):
        if lines[i] == last:
            return lines[i + 1:]
    return lines


def progress(key):
    """(steps taken on this ticket's current run, seconds elapsed, typical run seconds or 0)."""
    steps = sum(1 for _, level, _, k in claude_lines() if k == key and level == "tool")
    runs = list(bus.status["CodeBrain"]["durations"]) or [d["took"] for d in bus.done_today() if d.get("took")]  # survives a reload
    typical = sum(runs) / len(runs) if runs else 0
    started = bus.working.get(key, {}).get("started")
    return steps, time.time() - started if started else 0, typical


def order():
    """Tickets by priority: the ones being worked on (several run in parallel), then queued in queue order, then failed.
    A ticket is worked on while any engine has a run tagged with it, not just CentralBrain's review."""
    active = list(dict.fromkeys(list(bus.working) + [k for k, procs in claude.RUNNING.items() if procs and k and k not in bus.status]))
    queued = bus.pending()
    keys = [t["key"] for t in queued]
    rows = sorted(queued, key=lambda t: t["key"] not in active) + [t for k, t in bus.failed.items() if k not in keys]
    return active, keys, rows


SECTIONS = ["BACKLOG", "SELECTED FOR DEVELOPMENT", "BLOCKED", "IN PROGRESS", "IN QA", "IN CODE REVIEW", "ON HOLD", "CANCELLED", "DEPLOYED", "DONE"]
SAME = {"TO DO": "BACKLOG", "OPEN": "BACKLOG", "DEV IN PROGRESS": "IN PROGRESS", "CODE REVIEW": "IN CODE REVIEW", "CLOSED": "DONE"}


def section(status):
    """The card section for a Jira status: its own name, a known synonym, or by workflow position."""
    st = status.upper()
    return st if st in SECTIONS else SAME.get(st) or ("DONE" if "DONE" in st else "IN CODE REVIEW" if "REVIEW" in st else "IN QA" if "QA" in st else "IN PROGRESS")


def card_tickets():
    """Your tickets as the card shows them: assigned to you, plus ones you worked that Jira has closed."""
    closed = {k: v for k, v in JIRA["worked"].items() if section(v[0]) in ("DONE", "DEPLOYED", "CANCELLED")}  # open but reassigned: not yours
    return closed | JIRA["assigned"]  # Jira alone decides, so the card matches the board


def tickets(selected=None, width=100, cursor=None, folded=()):
    """Your Jira tickets, one section per status in workflow order. Tickets DevBot is running or has queued sit under
    their Jira status like the rest."""
    fold = lambda name, label: f"  [@click=app.fold('{name}')][{TEAL}]{'▸' if name in folded else '▾'} {label}[/][/]"
    active, keys, _ = order()
    jira = card_tickets()
    groups = {s: {} for s in SECTIONS}
    for k, (st, title) in jira.items():
        groups[section(st)][k] = title
    out = []
    for name, rows in groups.items():
        if not rows:
            continue  # an empty status takes no room
        out += [""] if out else []  # a blank line between sections
        out.append(fold(name, f"{name} ({len(rows)})"))
        if name not in folded:
            for i, (k, title) in enumerate(rows.items()):
                out += [""] if i else []  # a blank line between tickets
                bar = k in active and name not in ('DONE', 'DEPLOYED', 'CANCELLED') and width >= 50  # too narrow: no run bar, so the buttons still fit
                room = max(1, width - 16 - (24 if bar else 12))  # the title fills the middle up to the run bar, or the buttons when there's none
                lines = textwrap.wrap(str(title), room) or [""]
                out.append(f"  {activity(k in active, name)} [@click=app.show_ticket('{k}')]"
                           f"{'[reverse]' if k in (cursor, selected) else ''}[bold {WHITE}]{k:<11}[/]{'[/]' if k in (cursor, selected) else ''}[/] "
                           f"[{DIM}]{escape(lines[0].ljust(room))}[/] {eta(k) + ' ' if bar else ''}"
                           f"{ticket_controls(k, k in active)}")
                out += [f"{' ' * 16}[{DIM}]{escape(l)}[/]" for l in lines[1:]]
                out += [f"    {note}" for note in [doing(k, name, k in active)] if note]
    return "\n".join(out) or f"[{DIM}]    loading from Jira…[/]"


def eta(key):
    """A running ticket's progress against a typical run, five cells and the predicted time left, 11 columns."""
    _, elapsed, typical = progress(key)
    if not typical:
        return f"[{DIM}]{'▱' * 5} ~?m  [/]"
    n = min(5, int(5 * elapsed / typical))
    left = f"~{max(1, round((typical - elapsed) / 60))}m" if elapsed < typical else "soon"
    return f"[{TEAL}]{'▰' * n}[/][{DIM}]{'▱' * (5 - n)} {left:<5}[/]"


def doing(key, section, working):
    """The ticket's state under it: which open tickets block it, or what Claude is doing on it right now."""
    by = [] if section in ("DONE", "DEPLOYED", "CANCELLED") else JIRA.get("blocked", {}).get(key)
    if by:
        return f"[{CORAL}]↳ Blocked by {escape(', '.join(by))}[/]"
    if working and section not in ("DONE", "DEPLOYED", "CANCELLED"):
        return f"[{TEAL}]↳ {escape(bus.working.get(key, {}).get('activity') or 'starting')}[/]"
    return ""


def activity(working, section):
    """A spinner while Claude works on an open ticket; otherwise a blank that keeps the keys aligned."""
    return dot("working") if working and section not in ("DONE", "DEPLOYED", "CANCELLED") else " "


def brain_line(b, width=100, expanded=False):
    """Short status: what the brain is working toward (with its ticket), why it is blocked, or "idle 1m · <first clause
    of its last result>". Expanded, the full last result follows on its own lines."""
    s = bus.status[b]
    st = state(s)
    short = lambda text: fit(text.split(";")[0].split(". ")[0], width - 24)
    if b in bus.held:
        colour, text = AMBER, "paused"
    elif st == "working":
        goal = f"→ {s['ticket']} · " if s["ticket"] else ""
        purpose = "implementing" if s["ticket"] else f"checking {TARGET[b]}"  # the purpose; sub-steps go to the ticket's log
        colour, text = WHITE, goal + purpose
    elif st == "blocked":
        colour, text = CORAL, short(s["note"])
    else:
        idle = f"idle {ago(s['last_tick'])}" if s["last_tick"] else "idle"
        colour, text = DIM, idle + (f" · {short(s['note'])}" if s["note"] else "")
    what = f"[{colour}]{hang(text, 13, width)}[/]"  # wrapped lines start under the status's first character
    name = b.removesuffix("Brain")
    chev = f"[{DIM}]{'▾' if expanded else '▸'}[/]"
    line = f"{chev} {dot(st)} [@click=app.toggle_brain('{b}')][bold {WHITE}]{name}[/][/]{' ' * (8 - len(name))} {what}"
    if expanded and s["note"]:
        line += f"\n{' ' * 13}[{WHITE}]{hang(s['note'], 13, width - 10)}[/]"
    return line


def controls(b):
    """The brain's buttons as (label, action, colour role); None hides one. Running: pause, stop, logs; otherwise play, logs."""
    held, st = b in bus.held, state(bus.status[b])
    logs = ("≡ Logs", f"show_log('{b}')", "blue")
    if st == "working" and not held:
        return [("⏸ Pause", f"hold('{b}')", "yellow"), None, ("■ Stop", f"stop_brain('{b}')", "coral"), logs]
    play = ("▶ Resume", f"hold('{b}')", "mint") if held else \
        ("↻ Retry", f"run_brain('{b}')", "coral") if st == "blocked" else \
        ("▶ Run", f"run_brain('{b}')", "mint") if b != "CodeBrain" else None
    return [None, play, None, logs]


theme.apply(theme.saved(), __import__(__name__, fromlist=['_']))


def addressed(text, owner=bus.CONFIG["owner"]):
    """The message @mentions the owner or everyone, or calls the owner by first name."""
    return bool(re.search(rf"@(everyone|channel|here|team)\b|@?\b{re.escape(owner.split()[0])}\b", str(text), re.I))


def when(ts):
    """Teams/Roam/Jira/GitHub timestamps (UTC or offset) shown in local time."""
    try:
        return datetime.fromisoformat(str(ts)).astimezone().strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return str(ts)[:16].replace("T", " ")


def reply(m):
    """What the owner actually said next in the chat after this message, read from the latest copy of the whole chat;
    the engine's own claim only when the message isn't in it or ends it (engines tag one reply onto several messages)."""
    lines = chat(m)
    me, key = bus.norm(bus.CONFIG["owner"]), bus._line_key(m)
    i = next((i for i, t in enumerate(lines) if bus._line_key(t) == key), None)
    if i is None or i == len(lines) - 1:  # not saved, or the newest line: the reply may postdate the snapshot
        return m.get("reply") or ""
    return next((t.get("text", "") for t in lines[i + 1:] if bus.norm(t.get("from")) == me), "")


def mine(name):
    """Sent by the owner (any spelling: "Roberts, Cole", "Cole Roberts", "Cole Roberts (Bot)")."""
    return bus.norm(name) == bus.norm(bus.CONFIG["owner"])


def wrap(text, width, first, rest):
    """Whole text, every paragraph, each wrapped line lined up under the first character after `first`."""
    out = []
    for para in str(text).splitlines() or [""]:
        out += textwrap.wrap(para, max(10, width), initial_indent=rest, subsequent_indent=rest) or [rest]
    out[0] = first + out[0][len(rest):]
    return escape("\n".join(out))


_shown = [None, None, ""]  # the message list drawn, its width, the markup


def messages(width=60):
    """Direct messages, and messages that @ the owner, @ everyone or address them, newest first, then the reply sent.
    Click one for its whole thread. Drawn again only when the saved messages or the width change: each reply scans every
    saved chat, which eight times a second kept a whole CPU core busy."""
    rows = bus.messages()  # the same list object until the file changes
    if _shown[0] is not rows or _shown[1] != width:
        _shown[:] = [rows, width, _messages(rows, width)]
    return _shown[2]


def _messages(rows, width):
    lines, shown = [], [(i, m) for i, m in enumerate(rows) if bus.norm(m.get("chat")) == bus.norm(m.get("from")) or mine(m.get("from")) or addressed(m.get("text"))][:50]
    replies = {i: reply(m) for i, m in shown if not mine(m.get("from"))}
    said = {" ".join(r.split()) for r in replies.values() if r}
    for i, m in shown:
        if mine(m.get("from")) and " ".join(str(m["text"]).split()) in said:
            continue  # already shown as the reply under the message it answers
        if lines:
            lines.append("")  # a blank line between messages
        if mine(m.get("from")):  # something you sent: never dressed up as received, and nothing to reply to
            head = f"You ↗ {m.get('chat') or '?'} · {m.get('source', '')} · {when(m.get('at', ''))}"
            lines.append(f"[@click=app.show_thread({i})][bold {MINT}]{escape(fit(head, width))}[/][/]")
            lines.append(f"[{DIM}]{wrap(m['text'], width, '  ', '  ')}[/]")
            continue
        head = f"{m.get('source', '')} · {m.get('chat') or m.get('from', '?')} · {when(m.get('at', ''))}"
        lines.append(f"[@click=app.show_thread({i})][bold {TEAL}]{escape(fit(head, width))}[/][/]")
        lines.append(f"[{WHITE}]{wrap(m['text'], width, '  ', '  ')}[/]")
        r = replies[i]
        lines.append(f"[{MINT}]{wrap(r, width, '  ↳ ', '    ')}[/]" if r else
                     f"  [{DIM}]↳ no reply needed[/]")
    return "\n".join(lines) or f"[{DIM}]No messages yet.[/]"


def _cell(line):
    s = line.strip()
    return s.endswith("|") and s.count("|") == 1 and len(s) > 1


def _tables(text):
    """Teams exports a table as one cell per line ('value |'), a blank line between rows. Put each row on one line."""
    lines, out, i = text.split("\n"), [], 0
    while i < len(lines):
        if not _cell(lines[i]):
            out.append(lines[i]); i += 1; continue
        rows, j = [], i
        while j < len(lines):
            if _cell(lines[j]):
                row = []
                while j < len(lines) and _cell(lines[j]):
                    row.append(lines[j].strip()[:-1].strip()); j += 1
                rows.append(row)
            elif not lines[j].strip() and j + 1 < len(lines) and _cell(lines[j + 1]):
                j += 1
            else:
                break
        n = len(rows[0]) if rows else 0
        if n >= 2 and len(rows) >= 2 and all(len(r) == n for r in rows):
            body = ["| " + " | ".join(r) + " |" for r in rows]
            body.insert(1, "| " + " | ".join("---" for _ in range(n)) + " |")
            out.append("\n".join(body)); i = j
        else:
            out.append(lines[i]); i += 1
    return "\n".join(out)


def md(body):
    """Message text rendered like GitHub does: headings, lists, code blocks, tables, links."""
    return Padding(Markdown(_tables(str(body)).strip() or "—", code_theme="monokai", hyperlinks=True), (0, 0, 1, 2))


def chat(m):
    """The whole chat this message belongs to, merged from every saved copy (the newest sync included), oldest first."""
    groups = [m.get("thread") or []]
    if m.get("chat"):  # other messages in this chat carry lines this copy missed
        for o in bus.messages():
            if bus.norm(o.get("chat")) != bus.norm(m.get("chat")) or o.get("source") != m.get("source"):
                continue
            if m.get("id") and o.get("id") == m.get("id"):
                continue
            if o.get("thread"):
                groups.append(o["thread"])
    return bus.merge_threads(groups)


def thread(m, before=False):
    """The whole chat this message belongs to, or the message and its reply (Outlook).
    before=True: only what comes before the message itself, to measure how far down it sits."""
    msgs = chat(m)
    if not msgs:
        msgs = [m] + ([{"from": "Reply", "text": m["reply"]}] if m.get("reply") else [])
    elif m.get("reply") and not any(str(m["reply"]) in str(t.get("text", "")) for t in msgs):
        msgs = msgs + [{"from": bus.CONFIG["owner"], "text": m["reply"]}]
    msgs = [t if isinstance(t, dict) else {"text": str(t)} for t in msgs]  # an engine may report a line as plain text
    if before:
        same = lambda t: str(t.get("at", ""))[:16] == str(m.get("at", ""))[:16] and str(t.get("text", ""))[:40] == str(m.get("text", ""))[:40]
        msgs = msgs[:next((i for i, t in enumerate(msgs) if same(t)), 0)]
    return Group(*[part for t in msgs for part in (
        Text.from_markup(f"[bold {MINT}]You ↗[/]  [{DIM}]{escape(when(t.get('at', '')))}[/]" if mine(t.get("from")) else
                         f"[bold {TEAL}]{escape(str(t.get('from', '?')))}[/]  [{DIM}]{escape(when(t.get('at', '')))}[/]"),
        md(t.get("text", "")), *map(image, t.get("images") or []))])


MEDIA = bus.ROOT / "state" / "media"
PHOTO_NAME = re.compile(r"[\w.-]+\.(png|jpe?g|gif|webp|heic)", re.I)  # what "Open full size" may open


def image(path):
    """One saved chat photo drawn in the terminal; only files under state/media are read. A failure is a dim line."""
    from devbot import tracker
    try:
        f = (MEDIA / pathlib.Path(str(path)).name).resolve()  # engines report paths; only the name is trusted
        return tracker.shown(f.read_bytes()[:tracker.MAX_BYTES], f.name)
    except Exception as e:  # noqa: BLE001 — a missing or broken image just shows as unavailable
        return Text.from_markup(f"[{DIM}]🖼 {escape(pathlib.Path(str(path)).name)}: couldn't show ({escape(type(e).__name__)})[/]")
