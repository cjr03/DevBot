"""Live terminal dashboard built around one question: what is DevBot doing for me right now, and is it on track?
Status strip, ticket queue, communications grouped by ticket, brains with their controls, CentralBrain's chat,
and an engine's log as a popup opened from its row in Brains."""
import collections
import functools
import json
import math
import pathlib
import re
import subprocess
import time
import webbrowser

from rich.console import Group
from rich.markup import escape
from rich.table import Table
from rich.style import Style as RichStyle
from rich.segment import Segment
from rich.text import Text
from textual.strip import Strip
from textual import events
from textual.screen import Screen
from textual.app import App, ComposeResult, SystemCommand
from textual.command import CommandPalette
from textual.binding import Binding, BindingsMap
from textual.color import Color
from textual.css.query import NoMatches
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.widgets import Static, Tab, TabbedContent, TabPane, TextArea

from devbot import brains, bus, claude, graph, theme, tracker

try:  # ask the terminal once, before Textual owns it, whether it draws real images (Ghostty, Kitty); not under tests
    from textual_image._terminal import get_cell_size
    from textual_image.renderable import Image as _BestImage, TGPImage
    if _BestImage is TGPImage:
        tracker.KITTY = TGPImage
    _cell = get_cell_size()  # cached: Textual can't answer the terminal's reply once it is running
    tracker.CELL = _cell.height / _cell.width or tracker.CELL  # so half-block photos keep their shape too
except (ImportError, OSError, ZeroDivisionError, AttributeError):
    pass

# Colours (BG, PANEL, TEAL, MINT, VIOLET, PINK, ...) come from theme.py and change live with the light/dark toggle.
WORKERS = graph.WORKERS
TARGET = {"CentralBrain": "all engines", "JIRABrain": "Jira", "OutlookBrain": "Outlook", "RoamBrain": "Roam",
          "CodeBrain": "Claude Code", "TeamsBrain": "Teams", "GitHubBrain": "GitHub"}
TABS = bus.BRAINS + ["All"]
SPIN = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
WORD = {"tool": "  · ", "warning": "Warning: ", "error": "Problem: "}
GAPS = {"main", "upper", "lower", "chatdock"}  # bare layout containers: what shows between the cards
LAYOUT_FILE = bus.ROOT / "state" / "layout.json"  # card sizes you dragged, kept across reloads


class Splitter(Static):
    """A drag handle between cards: press, drag and release to resize them."""

    def __init__(self, axis, **kw):
        super().__init__(**kw)
        self.axis = axis

    def render(self):
        """A grip between the panels: a long thin pill floating mid-row, tapered caps on a heavy line."""
        colour = TEAL if self.mouse_hover or self.has_class("-drag") else FAINT
        return Text("╺" + "━" * max(4, self.size.width // 3) + "╸", style=colour)

    def on_mouse_down(self, event):
        self.capture_mouse()
        self.add_class("-drag")
        self.start = (event.screen_y, self.app.pane_sizes(self.axis))

    def on_mouse_move(self, event):
        if self.has_class("-drag"):
            self.app.drag(self.axis, event.screen_x, event.screen_y, self.start)

    def on_mouse_up(self, event):
        self.release_mouse()
        self.remove_class("-drag")
        LAYOUT_FILE.write_text(json.dumps(self.app.layout_sizes))


def derive():
    """Rebuild everything made from the palette. Status colours carry data state only: mint ok, red error, orange warning."""
    global LEVEL
    LEVEL = {"info": WHITE, "ok": MINT, "tool": DIM, "warning": AMBER, "error": f"bold {CORAL}"}


def dur(s):
    s = int(s)
    return f"{s}s" if s < 90 else f"{s // 60}m{s % 60:02d}s"


def pill(text, fg, bg):
    """A solid label chip."""
    return f"[bold {fg} on {bg}] {text.strip()} [/]"


@functools.lru_cache(maxsize=512)
def reply(text):
    """A CentralBrain reply: Markdown wrapped beside the ⏺, parsed once, not on every spinner frame."""
    row = Table.grid(padding=(0, 1))
    row.add_row(Text("⏺", style=TEAL), graph.md(text).renderable)
    return row


class SelectLog(VerticalScroll):
    """A scrolling log whose text can be selected with the mouse and copied (RichLog cannot be selected).
    Keeps at most MAX lines and keeps your place when you have scrolled up and old lines drop off the top."""
    MAX = 300

    def __init__(self, **kw):
        super().__init__(**kw)
        self.lines = collections.deque(maxlen=self.MAX)  # (markup, plain, hang): hang = (prefix, indent, style, text)
        self.body = Selectable("", classes="logbody")
        self.dirty = False
        self.dropped = 0  # lines that fell off the top since the last redraw

    def compose(self) -> ComposeResult:
        yield self.body

    def write(self, markup, plain, hang=None):
        """hang: (prefix markup, its width, style, text): the text is wrapped at draw time to this log's width, every
        wrapped line starting under the first character of the text."""
        if len(self.lines) == self.MAX:
            self.dropped += 1 + markup.count("\n")
        self.lines.append((markup, plain, hang))
        self.dirty = True

    def flush(self):
        """Redraw if changed and visible, unless the user is mid-selection here (a redraw would drop it)."""
        if not self.dirty or not self.display or self.region.height == 0 or self.body in self.screen.selections:
            return
        follow = self.scroll_y >= self.max_scroll_y - 1
        y, dropped = self.scroll_y, self.dropped
        w = self.content_region.width - 1  # leave the scrollbar its column
        parts, run = [], []  # runs of markup lines become one Text; a renderable line (rendered Markdown) stands on its own
        for m, _, h in list(self.lines) + [(None, "", None)]:
            if isinstance(m, str):
                run.append(m if not h else f"{h[0]}[{h[2]}]{graph.hang(h[3], h[1], w)}[/]")
                continue
            if run:
                parts.append(Text.from_markup("\n".join(run)))
                run = []
            if m is not None:
                parts.append(m)
        self.body.update(Group(*parts))
        self.dirty, self.dropped = False, 0
        if follow:
            self.call_after_refresh(self.scroll_end, animate=False)
        elif dropped:  # the text moved up by `dropped` rows; move with it so the view stays put
            self.call_after_refresh(self.scroll_to, y=max(0, y - dropped), animate=False)

    def plain(self):
        return "\n".join(p for _, p, _ in self.lines)

    def on_resize(self):
        self.dirty = True  # wrapped lines follow the new width

    def clear(self):
        self.lines.clear()
        self.dirty = True


class Clickable(Static):
    """Static whose clickable parts keep their own colours: Textual would paint every link in one link colour."""
    link_style = RichStyle()
    CAPS = ("\ue0b6", "\ue0b4")  # a link drawn with these round caps is a pill

    def on_mount(self):
        self.pills = set()  # click actions of the pills seen; matched by action, as each redraw gives links new ids

    @property
    def link_style_hover(self):
        """Textual recolours just the piece under the mouse, over cached lines; a hovered pill must stay wholly violet."""
        return RichStyle() if self.hover_style.meta.get("@click") in self.pills else super().link_style_hover

    def render_line(self, y):
        """The hovered pill turns wholly violet, like the brains' buttons: its caps' colour and its body's fill."""
        strip = super().render_line(y)
        self.pills.update(s.style.meta["@click"] for s in strip if s.text in self.CAPS and s.style and "@click" in s.style.meta)
        hot = self.mouse_hover and self.hover_style.meta.get("@click")
        if hot not in self.pills:
            return strip  # plain text links keep their own hover look
        on = lambda st: st and st.meta.get("@click") == hot
        swap = lambda t, st: st + RichStyle(underline=False, **{'color' if t in self.CAPS else 'bgcolor': VIOLET})
        return Strip([Segment(t, swap(t, st) if on(st) else st, c) for t, st, c in strip], strip.cell_length)


class Backdrop(Static):
    """Dims everything behind an open popup; clicking it closes the popup."""
    def on_click(self):
        self.app.action_close_popups()


class Selectable(Static):
    """Static whose rendered Markdown and photos can be selected too: Textual only selects plain Text."""
    def get_selection(self, selection):
        opts = self.app.console.options.update_width(max(1, self.content_size.width))
        lines = self.app.console.render_lines(self.content, opts, pad=False)
        return selection.extract("\n".join("".join(s.text for s in line).rstrip() for line in lines)), "\n"


class Palette(CommandPalette):
    """Textual's command palette with a ✕ in its top right corner."""
    def on_mount(self):
        self.query_one("#--container").mount(Clickable(f"[@click=screen.dismiss][{WHITE}]✕[/][/]", id="paletteclose"))


class ChatTabs(Clickable):
    """The chat tab row: drag a tab along the row and it moves with the mouse; let go and it stays there."""
    spans = []  # (first cell, end cell, chat id) per tab, set by Dashboard.chat_tabs
    dragged = moved = None
    grab = 0  # where on the dragged tab you took hold, so it floats under the mouse at that spot
    float_x = None  # first cell of the lifted tab while it follows the mouse

    def at(self, event):
        # ponytail: first line only; tabs that wrap onto a second line can't be dragged
        return next((cid for a, b, cid in self.spans if a <= event.x < b), None) if event.y == 0 else None

    def on_mouse_down(self, event):
        if event.button == 2 and self.at(event):  # middle-click closes a tab, like Chrome; its click then does nothing
            self.moved = True
            return self.app.action_close_chat(self.at(event), 1)
        self.dragged, self.start = self.at(event), event.x
        self.grab = event.x - next((a for a, b, c in self.spans if c == self.dragged), event.x)
        self.capture_mouse()

    def on_mouse_move(self, event):
        """The tab follows the mouse while you drag it, like browser tabs."""
        if self.dragged and event.x != self.start:  # a drag, not a click: it never changes which chat is open
            self.moved = True
            # any height, in or out of the row: the slot is how many other tabs have their middle left of the mouse
            self.app.move_chat(self.dragged, sum(a + b < 2 * event.x for a, b, c in self.spans if c != self.dragged))
            self.float_x = event.x - self.grab
            self.update(self.app.chat_tabs())

    def on_mouse_up(self, event):
        self.on_mouse_move(event)
        self.release_mouse()
        self.dragged = self.float_x = None
        self.update(self.app.chat_tabs())  # set down in its slot

    async def _on_click(self, event):
        if self.moved:  # the drop's click would open whatever tab used to sit where you let go
            self.moved = False
            event.prevent_default()
            return event.stop()
        if event.button != 1 or event.chain > 1:  # closing a tab slides the row left, so a quick second click would hit +
            event.prevent_default()  # also skips Widget._on_click, so the + link can't run
            return event.stop()
        # no super()._on_click: Textual already runs Widget._on_click itself; calling it too opened two chats per click on +


class TicketTabs(TabbedContent):
    """The ticket log tabs: open tickets first, done ones after. Drag a tab along its group and it moves with the mouse;
    let go and the order is saved. The row scrolls sideways when the tabs don't fit."""
    dragged = None

    def on_mouse_down(self, event):
        w, _ = self.screen.get_widget_at(event.screen_x, event.screen_y)
        self.dragged = w if isinstance(w, Tab) else None
        if self.dragged:
            self.capture_mouse()

    def on_mouse_move(self, event):
        if not self.dragged:
            return
        row = list(self.dragged.parent.children)
        over = next((t for t in row if isinstance(t, Tab) and t is not self.dragged and t.region.x <= event.screen_x < t.region.right
                     and t.has_class("-done") == self.dragged.has_class("-done")), None)  # only within its own group
        if over:
            self.moved = True
            self.dragged.parent.move_child(self.dragged, **{"before" if row.index(over) < row.index(self.dragged) else "after": over})

    def on_mouse_up(self, event):
        if self.dragged:
            self.release_mouse()  # the capture keeps the Click from the tab, so a press without a drag opens it here
            if not getattr(self, "moved", False):
                self.active = self.dragged.id.removeprefix("--content-tab-")
            self.dragged, self.moved = None, False
            self.app.layout_sizes["tabs"] = [t.id.removeprefix("--content-tab-ct-") for t in self.query(Tab)]
            LAYOUT_FILE.write_text(json.dumps(self.app.layout_sizes))


def usage_bars(usage, tier):
    """Header usage: your Claude account's real usage as a bar (the weekly plan limit, or once that is full, the extra
    usage credits paying for runs). When DevBot has fallen back to the Anthropic or Cursor API key, only that key shows, as
    active: neither key can read its own usage (both providers need an admin key for that)."""
    if tier in ("api", "cursor"):
        return f"[bold {WHITE}]{'Anthropic' if tier == 'api' else 'Cursor'}[/] [{MINT}]● active[/]"
    usage = usage or {}
    week, extra = (usage.get("seven_day") or {}).get("utilization"), usage.get("extra_usage") or {}
    if week is None:
        out = f"[{DIM}]Claude ?[/]"
    elif week >= 100 and extra.get("is_enabled") and extra.get("monthly_limit"):
        pct = extra.get("utilization") or 0
        out = (f"[bold {WHITE}]Claude[/] {meter(pct)} [bold {CORAL if pct >= 100 else AMBER if pct >= 80 else WHITE}]"
               f"${(extra.get('used_credits') or 0) / 100:,.0f}[/][{DIM}] of ${extra['monthly_limit'] / 100:,.0f} extra usage[/]")
    else:
        out = f"[bold {WHITE}]Claude[/] {meter(week)} [bold {CORAL if week >= 100 else AMBER if week >= 80 else WHITE}]{week:.0f}%[/][{DIM}] week[/]"
    resets = (usage.get("seven_day") or {}).get("resets_at")
    if week is not None and week >= 100 and resets:
        from datetime import datetime
        out += f" [{DIM}]· week resets {datetime.fromisoformat(resets).astimezone().strftime('%a %-I:%M %p')}[/]"
    return out


def meter(pct, width=16):
    """A thin ━ line with half-cell precision: filled cells shade teal → amber → coral as usage climbs, on a faint track."""
    halves = round(min(max(pct, 0), 100) / 100 * width * 2)
    stops = [TEAL, AMBER, CORAL]
    out = ""
    for i in range(halves // 2 + halves % 2):
        t = i / max(1, width - 1) * (len(stops) - 1)
        lo, hi = stops[int(t)], stops[min(int(t) + 1, len(stops) - 1)]
        f = t - int(t)
        mix = "#" + "".join(f"{round(int(lo[j:j + 2], 16) * (1 - f) + int(hi[j:j + 2], 16) * f):02X}" for j in (1, 3, 5))
        out += f"[{mix}]{'╸' if i == halves // 2 else '━'}[/]"
    return out + f"[{FAINT}]{'━' * (width - halves // 2 - halves % 2)}[/]"


def logo(t, running=True):
    """A little robot head that glances around and blinks, then D E V B O T with a teal → violet gradient flowing through it. Paused: eyes shut, everything yellow."""
    if not running:
        return f"[bold {YELLOW}]⟦ᴗ ᴗ⟧ D E V B O T[/]"
    eyes = "─ ─" if t % 5 < 0.15 else ("◐ ◐", "◉ ◉", "◑ ◑", "◉ ◉")[int(t / 1.25) % 4]
    head = f"[bold {TEAL}]⟦[/][bold {WHITE}]{eyes}[/][bold {TEAL}]⟧[/]"
    lo, hi = TEAL, VIOLET
    letters = ""
    for i, c in enumerate("DEVBOT"):
        f = (1 + math.sin(t * 2.5 - i * 0.7)) / 2
        mix = "#" + "".join(f"{round(int(lo[j:j + 2], 16) * (1 - f) + int(hi[j:j + 2], 16) * f):02X}" for j in (1, 3, 5))
        letters += f"[bold {mix}]{c}[/] "
    return f"{head} {letters.rstrip()}"


class Banner(Clickable):
    """One-line header, in order of importance: health, done today, errors and retries, usage bars, then when it last updated; normal values stay quiet, only abnormal ones get colour and weight."""

    def tick(self, snap, queue):
        blocked = [b.removesuffix("Brain") for b in WORKERS if snap[b]["state"] == "error"]
        errs = sum(1 for t in list(bus.error_times) if t > time.time() - 3600)  # last hour, reloads included; the popup lists older ones too
        done = len(graph.finished_tiers(queue)[1])  # same definition as the table's DONE: merged or closed, nothing pending
        parts = [pill(f"■ {', '.join(blocked)} blocked", INK, CORAL) if blocked else f"[{MINT}]● healthy[/]"]
        parts.append(f"[@click=app.show_done]{done} done today ▾[/]")
        retried = f" · {bus.retries} retried" if bus.retries else ""
        parts.append(f"[@click=app.show_errors][{CORAL if errs else DIM}]{errs} errors{retried} ▾[/][/]")
        parts.append(usage_bars(claude.account_usage(), claude.tier()))
        sep = f"  [{FAINT}]▎[/]  "
        self.update(logo(time.time(), bus.paused.is_set()) + sep + sep.join(parts))


# ponytail: only the two shapes Ghostty dumped into the box (`[<b;x;yM` and `[code;;textu`). Widen this if a new shape shows up.
_LEAK = re.compile(r"\[(\d+);;\d+u|\[?<\d+;\d+;\d+[Mm]")


def scrub_terminal_leaks(text):
    """Drop mouse reports. A kitty key report becomes the key it names (32 is a space)."""
    def sub(m):
        code = m.group(1)
        if not code:
            return ""
        n = int(code)
        return chr(n) if 32 <= n < 127 else ""
    return _LEAK.sub(sub, text)


class ChatInput(TextArea):
    """Message box: wraps long text and grows up to 8 lines, then scrolls. Enter sends; shift+enter or ctrl+j adds a line.
    Left/right switch chats while the box is empty. Up on the top row recalls your past messages; down past the newest brings back what you had typed."""

    class Submitted(Message):
        def __init__(self, text):
            super().__init__()
            self.text = text

    def __init__(self, **kw):
        super().__init__(soft_wrap=True, compact=True, highlight_cursor_line=False, **kw)
        self.pos = None  # index into your past messages while browsing them, else None
        self.draft = ""  # what you had typed before browsing; down past the newest brings it back
        self._scrubbing = False

    def on_text_area_changed(self, event):
        """Ghostty's mouse and kitty-key reports sometimes arrive as typed text. Take them back out."""
        if self._scrubbing:
            return
        raw = self.text
        clean = scrub_terminal_leaks(raw)
        if clean == raw:
            return
        row, col = self.cursor_location
        at = len(scrub_terminal_leaks(raw[:self.document.get_index_from_location((row, col))]))
        self._scrubbing = True
        try:
            self.load_text(clean)
            self.move_cursor(self.document.get_location_from_index(at))
        finally:
            self._scrubbing = False

    def recall(self, step):
        if self.pos is None and (step > 0 or self.cursor_location[0] > 0):
            return False
        past = [e["text"] for e in list(bus.chat) if e["who"] == "You" and e["kind"] == "say"]
        pos = (len(past) if self.pos is None else self.pos) + step
        if pos < 0:
            return True  # already at the oldest
        if self.pos is None:
            self.draft = self.text
        self.pos = pos if pos < len(past) else None
        self.text = past[pos] if self.pos is not None else self.draft
        self.move_cursor(self.document.end)
        return True

    async def _on_key(self, event):
        if self.app.closing and event.key in ("y", "n"):  # answering "Close this chat?"
            event.prevent_default(); event.stop()
            self.app.action_confirm_close(event.key == "y")
        elif event.key == "enter":
            event.prevent_default(); event.stop()
            text, self.text, self.pos = self.text.strip(), "", None
            if text:
                self.post_message(self.Submitted(text))
        elif event.key in ("up", "down") and self.recall(-1 if event.key == "up" else 1):
            event.prevent_default(); event.stop()
        elif event.key in ("left", "right") and not self.text:
            event.prevent_default(); event.stop()
            self.app.action_step_chat(-1 if event.key == "left" else 1)
        elif event.key in ("shift+enter", "ctrl+j"):
            event.prevent_default(); event.stop()
            self.insert("\n")
        else:
            await super()._on_key(event)


class PillButton(Static):
    """A solid round-ended pill ( label ) in its role colour with a white or, on light fills, black label (needs a Nerd Font), filled violet while hovered. Styled in CSS."""
    ROLE = "BLUE"  # palette role of the outline and fill; also a CSS class
    TIP = ""

    def __init__(self, caption="", **kw):
        super().__init__(classes=f"pill -{self.ROLE.lower()}", **kw)
        self.caption = caption

    def on_mount(self):
        self.tooltip = self.TIP
        self.paint()  # the app sets the live labels once everything is mounted

    def label(self):
        self.paint()

    def paint(self):
        self.update(escape(f"   {self.caption}   "))  # sizes the pill; render() draws it

    def render(self):
        """A solid pill: Nerd Font half-circle caps ( ) in the role colour around a filled label in white."""
        fill = self.styles.color.rich_color
        ink = "#000000" if self.styles.color.brightness > 0.6 else "#FFFFFF"  # dark label on light fills (teal, mint, yellow)
        return Text.assemble(("\ue0b6", RichStyle(color=fill)), (f"  {self.caption}  ", RichStyle(bgcolor=fill, color=ink, bold=True)),
                             ("\ue0b4", RichStyle(color=fill)))


class CommandsButton(PillButton):
    """Opens the command palette: every DevBot action as a clickable list."""
    ROLE, TIP = "TEAL", "Every action, as a list you can click"

    def on_click(self):
        self.app.action_command_palette()


class ActionButton(PillButton):
    """An engine's pause, play or stop pill, like Send. Label, action and colour follow the live state."""
    action = ""

    def show(self, spec):
        self.display = bool(spec)
        if spec and (self.caption, self.action) != spec[:2]:
            self.caption, self.action, role = spec
            self.tooltip = self.caption.partition(" ")[2]
            for r in ("blue", "coral", "mint", "yellow"):
                self.set_class(r == role, f"-{r}")
            self.paint()

    def paint(self):
        self.update(" x ")  # sizes the pill to one glyph, like the ticket buttons

    def render(self):
        fill = self.styles.color.rich_color
        return Text.assemble(("\ue0b6", RichStyle(color=fill)), (self.caption[:1], RichStyle(bgcolor=fill, color=INK, bold=True)),
                             ("\ue0b4", RichStyle(color=fill)))

    async def on_click(self):
        await self.app.run_action(self.action)


class SendButton(PillButton):
    """Sends what is in the chat box (Enter in the box does the same)."""
    ROLE, TIP = "MINT", "Send your message to CentralBrain"

    def on_click(self):
        say = self.app.query_one("#say", ChatInput)
        text, say.text, say.pos = say.text.strip(), "", None
        if text:
            brains.ask(text)


class PostButton(PillButton):
    """Posts the comment box beside it: to Jira on the left of the ticket popup, to the top pull request on the right."""
    ROLE, TIP = "TEAL", "Post this comment"

    def on_click(self):
        self.app.post_comment(self.id.removesuffix("post"))


class ThemeButton(PillButton):
    """Light/dark switch in the title bar. Shows the mode you will switch to."""
    TIP = "Switch between light and dark mode"

    def label(self):
        self.caption = "☾ Dark mode" if THEME == "light" else "☀ Light mode"
        self.set_class(THEME == "light", "-night")
        self.set_class(THEME != "light", "-sun")
        self.paint()

    def on_click(self):
        self.app.action_toggle_theme()


class Base(Screen):
    """The dashboard's screen: mouse events pass through Dashboard.click_off first, so popups close on a click outside."""

    def _forward_event(self, event):
        if self.app.click_off(event):
            return
        # The bare screen is the margin between cards. Textual only starts a selection when the
        # screen itself allows it, so this is off for that one press and back on afterwards.
        bare = isinstance(event, events.MouseDown) and self.get_widget_and_offset_at(event.x, event.y)[0] is self
        if bare:
            self.ALLOW_SELECT = False
        try:
            super()._forward_event(event)
        finally:
            if bare:
                self.ALLOW_SELECT = True

    def _watch__select_state(self, select_state):
        """A drag across several widgets selects everything between, buttons and tabs too: drop the unselectable ones."""
        super()._watch__select_state(select_state)
        if any(not w.allow_select for w in self.selections):
            self.selections = {w: s for w, s in self.selections.items() if w.allow_select}


class Dashboard(App, inherit_bindings=False):
    TITLE = "DevBot"
    AUTO_FOCUS = None

    def action_command_palette(self):
        if not CommandPalette.is_open(self):
            self.push_screen(Palette(id="--command-palette"))
    CSS = """
    * { scrollbar-size-vertical: 1; scrollbar-size-horizontal: 1;
        scrollbar-background: $c-panel; scrollbar-background-hover: $c-panel; scrollbar-background-active: $c-panel;
        scrollbar-color: $c-panel; scrollbar-color-hover: $c-teal; scrollbar-color-active: $c-teal; scrollbar-corner-color: $c-panel; }
    .-scrolling * { scrollbar-color: $c-faint; }  /* bars hide until you scroll */
    Screen { background: $c-bg; color: $c-white; overflow: auto auto; layers: base popup; }  /* below the minimums it scrolls, never squashes */
    Clickable { link-style-hover: bold underline; link-background-hover: transparent; }
    CommandPalette { align: center middle; background: $c-shade; }  /* the same darkened sheet as the other popups */  /* a square card in the middle of the screen */
    CommandPalette > Vertical { width: 64; height: 32;  /* cells are 1:2, so 64x32 is square */ margin: 0; visibility: visible;
                                background: $c-panel; border: round $c-faint; padding: 0 1 1 4; }
    CommandPalette #--results { overlay: none; }  /* the list stays inside the card */
    CommandPalette CommandList { max-height: 24; margin-right: 3; }
    /* the search bar: a raised rounded field, teal ring while you type */
    CommandPalette #--input { margin-right: 3; background: $c-raised; border: round $c-faint; height: 3; padding: 0 1; margin-bottom: 1; }
    CommandPalette #--input:focus-within { border: round $c-teal; }
    CommandPalette #--input SearchIcon { color: $c-teal; margin: 0 1 0 0; }
    CommandPalette CommandInput, CommandPalette CommandInput:focus { background: $c-raised; color: $c-white; border: none; padding: 0; }
    CommandPalette CommandList { background: $c-panel; color: $c-white; }
    #paletteclose { dock: top; height: 1; width: 1fr; text-align: right; }  /* the card's top right corner */
    CommandPalette CommandList > .option-list--option-highlighted { background: $c-raised; color: $c-teal; text-style: bold; }
    CommandPalette CommandList > .option-list--option-hover { background: $c-raised; }
    /* one card convention: panel fill, faint rounded border, bold title, dim subtitle */
    .card { background: $c-panel; border: round $c-faint; border-title-color: $c-white; border-title-style: bold reverse;
            border-subtitle-color: $c-dim; padding: 0 1; }
    #top { height: 3; min-width: 100; margin: 0 1 1 1; }  /* a clear row between the header and the cards */
    Banner { width: 1fr; height: 1; background: $c-panel; }
    .pill.-coral { color: $c-coral; }
    .pill.-mint { color: $c-mint; }
    .pill.-yellow { color: $c-yellow; }
    .brainrow { height: auto; align: left middle; margin-bottom: 1; }  /* one blank line between brains, expanded or not */
    .brainrow.-flash { background: $c-raised; }
    .brainline { width: 1fr; height: auto; }
    .pill { width: auto; height: 1; padding: 0; margin: 1 0 1 1; background: $c-panel; color: $c-blue; }  /* same 3-row slot as before */
    .pill.-teal { color: $c-teal; }
    .pill.-sun { color: #FFE600; }  /* Light mode button: bright yellow */
    .pill.-night { color: #4B0082; }  /* Dark mode button: dark purple */
    .pill:hover { color: $c-violet; }
    ActionButton.pill, #top .pill { margin: 0 0 0 1; }  /* in line with the header text */  /* the whole pill turns violet */
    #main { height: 1fr; min-width: 100; min-height: 22; margin: 0 1; }  /* 4-row cards, bar, 5-row messages, 12-row chat dock */
    #upper { height: auto; }
    #ticketbox, #brains { height: auto; min-height: 4; min-width: 30; overflow-y: auto; }
    #ticketbox, #brains { scrollbar-gutter: stable; }  /* a scrollbar appearing would narrow the rows a frame late and wrap their buttons left; both keep it so their buttons line up */
    Splitter { color: $c-faint; height: 1; width: 100%; overflow: hidden; content-align: center middle; }
    Splitter:hover, Splitter.-drag { color: $c-teal; }
    #tickets { height: auto; }
    #lower { height: 1fr; min-height: 5; }
    #msgbox { width: 1fr; min-width: 30; height: 100%; }
    #chatdock { height: auto; }
    #chatbox { height: auto; }
    #chattabs { height: auto; background: $c-bg; }  /* the tab strip: canvas colour, so the open tab joins the chat page */
    #chatbox.-empty #chat { display: none; }
    SelectLog { background: $c-panel; }
    .logbody { width: 100%; }
    #chat { height: auto; min-height: 3; max-height: 12; }
    #chat .logbody { padding: 0 1; }
    #sayrow { height: auto; }
    #say { width: 1fr; height: auto; min-height: 3; max-height: 10; border: round $c-faint; background: $c-panel; color: $c-white; padding: 0 1; }
    #say:focus { border: round $c-teal; }
    #backdrop { layer: popup; dock: top; display: none; width: 100%; height: 100%; background: $c-shade; }  /* darkens everything behind an open popup */
    #logbox { layer: popup; dock: top; display: none; width: 80%; height: 80%; offset: 10% 10%; }  /* centred over the cards: 10% of the screen on every side */
    #logbox.-open { display: block; }
    #logclose { height: 1; width: 100%; text-align: right; }  /* top right corner */
    #threadbar { height: 1; }
    #threadtitle { width: 1fr; }
    #threadclose { width: 2; }  /* top right corner */
    #threadbox { layer: popup; dock: top; display: none; width: 80%; height: 80%; offset: 10% 10%; }
    #threadbox.-open { display: block; }
    #commsbox { layer: popup; dock: top; display: none; width: 80%; height: 80%; offset: 10% 10%; }
    #commsbox.-open { display: block; }
    #commsclose { height: 1; width: 100%; text-align: right; }  /* top right corner */
    #commstabs { height: 1fr; background: $c-panel; }
    #commstabs #tabs-scroll { overflow-x: auto; scrollbar-size-horizontal: 1; }  /* more tabs than fit: scroll sideways */
    #commstabs Tabs { height: 3; }
    #commstabs Tab.-done { text-style: italic; color: $foreground 35%; }  /* done tickets sit together after the open ones */
    #logs { height: 1fr; background: $c-panel; }
    #ticketpop { layer: popup; dock: top; display: none; width: 90%; height: 90%; offset: 5% 5%; }
    #ticketpop.-open { display: block; }
    #popbar { height: 1; }
    #poplinks { width: 50%; }
    #ghlink { width: 1fr; padding-left: 2; }  /* over the GitHub side: its border plus padding */
    #popclose { width: 2; }  /* top right corner */
    .popside { width: 1fr; height: 1fr; }
    #jiraside { padding-right: 1; }  /* mirrors the GitHub side, so both Post buttons sit the same distance from an edge */
    #ghside { border-left: solid $c-faint; padding-left: 1; }
    .popbody { height: 1fr; background: $c-panel; }
    .poprow { height: auto; }
    .popnote { width: 1fr; height: auto; min-height: 3; max-height: 8; border: round $c-faint; background: $c-panel; color: $c-white; }
    .popnote:focus { border: round $c-teal; }
    Tabs { background: $c-panel; }
    Tab { color: $c-dim; }
    Tab.-active { color: $c-teal; text-style: bold; }
    Tab:hover { text-style: underline; }
    Tab.-active:hover { text-style: bold underline; }
    .brainrow.-cursor { background: $c-raised; }
    Underline > .underline--bar { color: $c-teal; background: $c-panel; }
    """
    # Only these shortcuts, none of Textual's built-ins; everything is also a click away. Priority: they work in the chat box too.
    KEYS = [Binding(k, a, priority=True) for k, a in (
        ("ctrl+q", "quit"), ("ctrl+r", "reload_ui"), ("ctrl+e", "restart"), ("ctrl+n", "new_chat"),
        ("ctrl+p", "command_palette"), ("ctrl+m", "toggle_theme"), ("ctrl+d", "close_chat_active"),
        ("ctrl+c", "ignore"), ("super+c", "copy_selection"), ("super+p", "paste"), ("escape", "close_popups"))]  # Ctrl+C copies nothing; ⌘ keys only reach us in kitty-protocol terminals
    KEYS += [Binding("left", "step_chat(-1)"), Binding("right", "step_chat(1)")]  # the chat box takes them only while it has text

    def __init__(self, **kw):
        super().__init__(**kw)
        self._bindings = BindingsMap(self.KEYS)

    def compose(self) -> ComposeResult:
        with Horizontal(id="top", classes="card"):
            yield Banner(id="banner")
            yield CommandsButton("☰ Commands", id="cmdbtn")
            yield ThemeButton(id="themebtn")
        with Vertical(id="main"):
            with Horizontal(id="lower"):  # Messages on top, under the header: gets every row the others don't need
                with VerticalScroll(id="msgbox", classes="card"):
                    yield Clickable(id="messages")
            yield Splitter("rows", id="split-rows")
            with Vertical(id="upper"):  # Tickets, then Brains under it
                with Vertical(id="ticketbox", classes="card"):
                    yield Clickable(id="tickets")
                yield Splitter("mid", id="split-mid")
                with Vertical(id="brains", classes="card"):
                    for b in WORKERS:
                        with Horizontal(classes="brainrow"):
                            yield Clickable(id=f"line-{b}", classes="brainline")
                            yield ActionButton(id=f"b0-{b}")
                            yield ActionButton(id=f"b1-{b}")
                            yield ActionButton(id=f"b2-{b}")
                            yield ActionButton(id=f"b3-{b}")
            with Vertical(id="chatdock"):  # under Brains: Messages gives up rows instead
                yield Splitter("chat", id="split-chat")
                with Vertical(id="chatbox", classes="card"):
                    yield ChatTabs(id="chattabs")
                    yield SelectLog(id="chat")
                    with Horizontal(id="sayrow"):
                        yield ChatInput(placeholder="Ask CentralBrain…", id="say")
                        yield SendButton("➤ Send", id="send")
        yield Backdrop(id="backdrop")  # first on the popup layer, so it sits under every popup
        with Vertical(id="logbox", classes="card"):
            yield Clickable(f"[@click=app.close_log][{DIM}]✕[/][/]", id="logclose")
            with TabbedContent(id="logs"):
                for b in TABS:
                    with TabPane(b, id=f"tab-{b}"):
                        yield SelectLog(id=f"log-{b}")
        with Vertical(id="commsbox", classes="card"):  # every ticket's communications from every brain, a tab per ticket
            yield Clickable(f"[@click=app.close_comms][{DIM}]✕[/][/]", id="commsclose")
            yield TicketTabs(id="commstabs")
        with Vertical(id="threadbox", classes="card"):
            with Horizontal(id="threadbar"):
                yield Static(id="threadtitle")
                yield Clickable(f"[@click=app.close_thread][{DIM}]✕[/][/]", id="threadclose")
            with VerticalScroll(classes="popbody"):
                yield Selectable(id="thread")
            with Horizontal(classes="poprow"):
                yield TextArea(placeholder="Reply in this thread as you…", id="threadnote", classes="popnote")
                yield PostButton("➤ Send", id="threadpost")
        with Vertical(id="ticketpop", classes="card"):  # a ticket: Jira on the left, its pull request chain on the right
            with Horizontal(id="popbar"):
                yield Clickable(id="poplinks")
                yield Clickable(id="ghlink")
                yield Clickable(f"[@click=app.close_ticket][{DIM}]✕[/][/]", id="popclose")
            with Horizontal():
                for side, hint in (("jira", "Comment on the Jira ticket…"), ("gh", "Comment on the top pull request…")):
                    with Vertical(id=f"{side}side", classes="popside"):
                        with VerticalScroll(classes="popbody"):
                            yield Selectable(id=f"{side}info")
                        with Horizontal(classes="poprow"):
                            yield TextArea(placeholder=hint, id=f"{side}note", classes="popnote")
                            yield PostButton("➤ Post", id=f"{side}post")

    def get_system_commands(self, screen):
        """Every dashboard action, listed in the palette as soon as it opens: pick one by clicking, no typing needed."""
        yield SystemCommand(f"Switch to {'dark' if THEME == 'light' else 'light'} mode", "Change the colour theme", self.action_toggle_theme)
        yield SystemCommand("Resume all engines" if not bus.paused.is_set() else "Pause all engines",
                            "Runs already in progress finish first", self.action_pause)
        yield SystemCommand("Write to CentralBrain", "Put the cursor in the chat box", self.action_focus_chat)
        yield SystemCommand("New CentralBrain chat", "A separate conversation; the others keep going", self.action_new_chat)
        for cid, c in list(bus.chats.items()):
            if cid != bus.active:
                yield SystemCommand(f"Switch to chat: {c['name']}", "Show this CentralBrain conversation", lambda cid=cid: self.action_select_chat(cid))
        yield SystemCommand("Close this chat", "Asks first; stops its reply if one is running", lambda: self.action_close_chat(bus.active))
        for b in WORKERS:
            if b != "CodeBrain":
                yield SystemCommand(f"Run {b} now", f"Check {TARGET[b]} immediately", lambda b=b: self.action_run_brain(b))
            yield SystemCommand(f"{'Resume' if b in bus.held else 'Pause'} {b}", "Only this brain", lambda b=b: self.action_hold(b))
        for k in bus.failed:
            yield SystemCommand(f"Retry {k}", "Hand the failed ticket to CodeBrain again", lambda k=k: self.action_retry_ticket(k))
        for b in TABS:
            yield SystemCommand(f"Show {b} log" if b != "All" else "Show all logs", "Open its tab in the log panel", lambda b=b: self.action_show_log(b))
        yield SystemCommand("Copy the open log", "To the clipboard", self.action_copy_log)
        yield SystemCommand("Clear the open log", "Only clears the panel, not the log file", self.action_clear)
        yield SystemCommand("Scroll the open log to the top", "Oldest lines", lambda: self.action_scroll_log("scroll_home"))
        yield SystemCommand("Scroll the open log to the bottom", "Newest lines, and follow new ones", lambda: self.action_scroll_log("scroll_end"))
        yield SystemCommand("Reload the dashboard", "Rebuild the screen; engines keep running", self.action_reload_ui)
        yield SystemCommand("Reload window", "Restart DevBot now on its latest code; runs in progress resume", self.action_restart)
        yield SystemCommand("Quit DevBot", "Stop the dashboard and every engine", self.action_quit)

    def get_css_variables(self):
        p = theme.PALETTES[THEME]
        # one popup backdrop for every popup: layers can't see through, so it is the canvas darkened, never a translucent tint
        return {**super().get_css_variables(), **{f"c-{k.lower()}": v for k, v in p.items() if isinstance(v, str)},
                "c-shade": Color.parse(p["BG"]).blend(Color(0, 0, 0), 0.4).hex}

    def action_toggle_theme(self):
        """Switch light/dark live, remember it, and redraw text that was coloured with the old palette."""
        name = "dark" if THEME == "light" else "light"
        theme.FILE.write_text(name)
        theme.apply(name, __import__(__name__, fromlist=["_"]), graph)
        self.theme = f"textual-{name}"  # built-in widgets (command palette, toasts) follow the same light/dark
        self.refresh_css()
        for btn in self.query(PillButton):
            btn.label()
        for b in TABS:
            self.query_one(f"#log-{b}", SelectLog).clear()
        self._seen = {b: None for b in TABS}
        self._chat_key = None

    def on_mount(self):
        self.theme = f"textual-{THEME}"
        self._dress()
        self.picked = "CentralBrain"
        self.ticket = None  # the ticket you clicked to expand; None follows the active one
        self.brains_open = set()  # brains whose full last result is shown
        self.folded = {"DONE"}  # sections you collapsed; DONE is long, so it starts folded
        self.set_interval(300, self.load_jira)
        self.load_jira()
        self._chat_key = None
        self.drafts = {}  # chat id -> what you had typed there when you switched away
        self.closing = None  # the chat whose ✕ you clicked, waiting for Yes or No
        self.popped, self.chain, self._popped_at, self.seen_sides = None, [], 0, {}  # the ticket in the popup and its pull request chain
        self.call_after_refresh(lambda: self.set_interval(1 / 8, self.refresh_all))

    def _dress(self):
        """Label, title and size freshly built widgets, and redraw every log from the start."""
        for btn in self.query(PillButton):
            btn.label()
        for sel, title in {"#ticketbox": "Tickets", "#msgbox": "Messages", "#brains": "Brains", "#chatbox": "CentralBrain"}.items():
            self.query_one(sel).border_title = title
        Tab.ALLOW_SELECT = False  # class-wide so tabs added later (the comms popup) can't be selected either
        for w in self.query("*"):  # mouse text selection everywhere except buttons, tabs and links, the resize bars and the gaps between cards
            w.ALLOW_SELECT = not (isinstance(w, (Splitter, Clickable, PillButton, Tab)) or w.id in GAPS)
        self._seen = {b: None for b in TABS}
        self.comms_seen = {}  # ticket -> last line already in its tab of the communications popup (rebuilt empty on reload)
        self._chat_key = None
        try:
            self.layout_sizes = json.loads(LAYOUT_FILE.read_text())
        except (FileNotFoundError, ValueError):
            self.layout_sizes = {}
        self.apply_sizes()

    async def action_reload_ui(self):
        """Rebuild the dashboard in place; the engines and their runs carry on. New Python code still needs Ctrl+E."""
        draft = self.query_one("#say", ChatInput).text
        await self.recompose()
        self.refresh_css()
        self._dress()
        self.query_one("#say", ChatInput).text = draft

    def refresh_all(self):
        try:
            self._refresh()
            for s in self.query(Splitter):  # row handles trace the cards around them, which move as you drag
                s.refresh()
            for box in self.query("#ticketbox, #brains"):  # never taller than one blank row under the last line
                cap = max(4, max((c.virtual_region.bottom for c in box.children), default=0) + 1 + box.gutter.height)
                if box.styles.max_height is None or box.styles.max_height.value != cap:
                    box.styles.max_height = cap
        except NoMatches:  # the timer can fire once more while the app is tearing down
            pass

    def _refresh(self):
        if bus.reload.is_set() and not self.query_one("#say", ChatInput).text.strip():  # a half-typed message holds the reload until sent
            self.exit("restart")
            return
        snap, _ = bus.snapshot()
        queue = [t["key"] for t in bus.pending()]
        self.query_one("#banner", Banner).tick(snap, queue)
        width = lambda sel: self.query_one(sel).content_region.width
        self.query_one("#tickets").update(graph.tickets(self.ticket, width("#tickets"), self.ticket, self.folded))
        if self.query_one("#commsbox").has_class("-open"):  # every ticket's tab fills in as its lines arrive
            entries = graph.attributed()
            for key, seen in list(self.comms_seen.items()):
                try:
                    log = self.query_one(f"#cl-{key}", SelectLog)
                except NoMatches:  # its tab is still being added
                    continue
                lines = [e for e in entries if e[4] == key]
                for ts, brain, level, msg, _ in graph.after(lines, seen):
                    word, style = WORD.get(level, ""), LEVEL.get(level, WHITE)
                    tag = f"[{DIM}]{ts}[/] [{TEAL}]{brain:<12}[/] "
                    log.write(f"{tag}[{style}]{escape(word + msg)}[/]", f"{ts} {brain} {word}{msg}", (f"{tag}[{style}]{escape(word)}[/]", len(ts) + 14 + len(word), style, msg))
                self.comms_seen[key] = lines[-1] if lines else seen
                log.flush()
        self.query_one("#messages").update(graph.messages(width("#msgbox")))
        for b in WORKERS:
            line = self.query_one(f"#line-{b}")
            line.update(graph.brain_line(b, line.content_region.width, b in self.brains_open))
            line.parent.set_class(graph.flash(b, graph.state(bus.status[b])), "-flash")
            line.parent.set_class(b in self.brains_open, "-cursor")
            for i, spec in enumerate(graph.controls(b)):
                self.query_one(f"#b{i}-{b}", ActionButton).show(spec)

        self.query_one("#chattabs", ChatTabs).update(self.chat_tabs())
        talking = bool(bus.chat or bus.turn["started"])
        self.query_one("#chatbox").set_class(not talking, "-empty")  # a 3-row bar until you talk
        if talking and "chat" not in self.layout_sizes:  # once a conversation exists, it gets a third of the screen
            chat = self.query_one("#chat")
            room = self.query_one("#main").size.height - self.query_one("#upper").outer_size.height - 14  # messages, bars, input
            third = max(3, min(self.size.height // 3, room))
            if chat.styles.height is None or chat.styles.height.value != third:
                chat.styles.height = chat.styles.max_height = third
        for b in TABS:
            entries = list(bus.logs[b])
            new = graph.after(entries, self._seen[b])
            self._seen[b] = entries[-1] if entries else self._seen[b]
            log = self.query_one(f"#log-{b}", SelectLog)
            for ts, brain, level, msg in new:
                word = WORD.get(level, "")
                tag = f"[{DIM}]{ts}[/] " + (f"[{TEAL}]{brain:<12}[/] " if b == "All" else "")
                style = LEVEL.get(level, WHITE)
                log.write(f"{tag}[{style}]{escape(word + msg)}[/]", f"{ts} {brain + ' ' if b == 'All' else ''}{word}{msg}",
                          (f"{tag}[{style}]{escape(word)}[/]", len(ts) + 1 + (13 if b == "All" else 0) + len(word), style, msg))
            log.flush()
        self.render_chat()
        self.query_one("#chat", SelectLog).flush()
        if self.popped and time.time() - self._popped_at > 30:
            self.load_ticket()

    def render_chat(self):
        """Claude Code-style stream: ⏺ messages, a spinner on the running step, ✓/✗ when steps finish,
        and a live "Working…" line with elapsed time and step count."""
        entries = list(bus.chat)
        spin = SPIN[int(time.time() * 8) % len(SPIN)]
        live = bus.turn["started"]
        key = (len(entries), id(entries[-1]) if entries else 0, sum(e["status"] == "run" for e in entries), sum(e["status"] == "fail" for e in entries), bool(live))
        if key == self._chat_key and not live and not key[1]:  # a running step must keep its spinner moving
            return
        self._chat_key = key
        log = self.query_one("#chat", SelectLog)
        lines = []
        for e in entries:
            if e["kind"] == "step":
                icon, style = {"run": (spin, TEAL), "ok": ("✓", DIM), "fail": ("✗", f"bold {WHITE}")}[e["status"]]
                lines.append((f"  [{style}]{icon} {escape(e['text'])}[/]", f"  {icon} {e['text']}"))
            elif e["who"] == "You":
                lines.append((f"\n[bold {MINT}]› {escape(e['text'])}[/]  [{DIM}]{e['ts']}[/]", f"\n> {e['text']}"))
            else:
                lines.append((reply(e["text"]), f"* {e['text']}"))
        if live:
            what = "Thinking" if bus.turn["thinking"] else "Working"
            n = bus.turn["steps"]
            status = f"{what}… ({dur(time.time() - live)} · {n} step{'s' if n != 1 else ''})"
            lines.append((f"\n[bold {MINT}]{spin} {status}[/]", status))
        log.lines.clear()
        log.lines.extend((m, p, None) for m, p in lines)
        log.dirty = True

    def on_chat_input_submitted(self, event: "ChatInput.Submitted"):
        brains.ask(event.text)

    def chat_tabs(self):
        """One clickable tab per chat: the open one raised, each with its ✕, ● on a chat with an unread reply, a spinner on a
        chat that is answering, then +. After Ctrl+D, the row asks before closing."""
        spin = SPIN[int(time.time() * 8) % len(SPIN)]
        chats, ChatTabs.spans = list(bus.chats.items()), []
        if self.closing in bus.chats:
            c = bus.chats[self.closing]
            busy = " It is still answering; closing stops it." if c["turn"]["started"] else ""
            return (f"[{AMBER}]Close \"{escape(c['name'])}\" and its history?{busy}[/]  "
                    f"[@click=app.confirm_close(1)][bold {CORAL}]Yes, close it (y)[/][/]   [@click=app.confirm_close(0)][{TEAL}]No (n)[/][/]")
        tabs, x = [], 0
        # Chrome-style: equal-width tabs that shrink as chats are added so the row stays one line, long names cut with …,
        # a fixed icon slot so names line up, the open tab raised, ✕ on every tab
        full = self.query_one("#chattabs").size.width or 120
        room = max(1, min(18, (full - 4) // max(1, len(chats)) - 7))  # 4 cells kept for the + right after the last tab
        for cid, c in chats:
            mark = f"[{MINT}]{spin}[/]" if c["turn"]["started"] else f"[{MINT}]●[/]" if c["unread"] else " "
            name = c["name"] if len(c["name"]) <= room else c["name"][:room - 1] + "…"
            look = f"bold {WHITE} on {PANEL}" if cid == bus.active else DIM  # the open tab is the page below it; the rest sit in the strip
            tabs.append(f"[{look}][@click=app.select_chat('{cid}')] {mark} {escape(name.ljust(room))} [/][@click=app.close_chat('{cid}', 1)]✕ [/][/]")
            width = Text.from_markup(tabs[-1]).cell_len
            ChatTabs.spans.append((x, x + width, cid))
            x += width + 1
        row = f"[{DIM}]│[/]".join(tabs) + f" [@click=app.new_chat][bold {TEAL}] + [/][/]"
        bar = self.query_one("#chattabs", ChatTabs)
        a, b = next(((a, b) for a, b, c in ChatTabs.spans if c == bar.dragged), (0, 0))
        if bar.float_x is None or b == a:
            return row
        row = Text.from_markup(row)
        # dragging: the tab lifts off the row and follows the mouse; its slot stays open as a gap the others slide past
        tab = row[a:b]
        tab.stylize(f"bold {WHITE} on {TEAL}")
        row = row[:a] + Text(" " * (b - a)) + row[b:]
        f = max(0, min(bar.float_x, len(row) - (b - a)))
        return row[:f] + tab + row[f + b - a:]

    def _switch(self, go):
        """Run go() to change chats, keeping what you had typed with the chat you typed it in."""
        say = self.query_one("#say", ChatInput)
        self.drafts[bus.active] = say.text
        go()
        say.text, say.pos = self.drafts.pop(bus.active, ""), None
        self._chat_key = None

    def action_select_chat(self, cid):
        self.closing = None
        self._switch(lambda: bus.select_chat(cid))

    def action_new_chat(self):
        self.closing = None
        self._switch(bus.new_chat)
        self.action_focus_chat()

    def action_close_chat(self, cid, now=0):
        """A tab's ✕ or middle-click closes it at once, like Chrome; the keyboard asks first."""
        self.closing = cid
        if now:
            self.action_confirm_close(1)

    def on_key(self, event):
        if self.closing and event.key in ("y", "n"):  # y/n answer "Close this chat?" wherever the focus is
            event.stop()
            self.action_confirm_close(event.key == "y")

    def action_close_chat_active(self):
        self.closing = bus.active

    def move_chat(self, cid, slot):
        """Put chat cid at position slot among the other chats; the open chat stays open."""
        with bus._lock:
            ids = [i for i in bus.chats if i != cid]
            ids.insert(slot, cid)
            if ids == list(bus.chats):  # most mouse moves while dragging don't change the order
                return
            order = {i: bus.chats[i] for i in ids}
            bus.chats.clear(); bus.chats.update(order)
        bus.save_chats()
        self.query_one("#chattabs", ChatTabs).update(self.chat_tabs())  # redrawn now, so the next mouse move sees where tabs are

    def action_step_chat(self, step):
        ids = list(bus.chats)
        self.action_select_chat(ids[(ids.index(bus.active) + step) % len(ids)])

    def action_confirm_close(self, yes):
        cid, self.closing = self.closing, None
        if yes and cid in bus.chats:
            self._switch(lambda: brains.close_chat(cid))
            self.drafts.pop(cid, None)

    def action_show_log(self, brain):
        self.picked = brain
        self.query_one("#logbox").add_class("-open")
        self._dim()
        self.query_one("#logbox").border_title = f"{brain} log" if brain != "All" else "All logs"
        self.query_one("#logs", TabbedContent).active = f"tab-{brain}"

    async def action_show_comms(self, key):
        """The communications popup, styled like the brains log: a tab for each of your tickets, opened on this one."""
        tabs = self.query_one("#commstabs", TabbedContent)
        _, _, rows = graph.order()
        card = graph.card_tickets()
        keys = dict.fromkeys(list(card) + [t["key"] for t in rows] + [key])  # the card's tickets, plus any DevBot is running
        for k in [k for k in self.comms_seen if k not in keys]:  # reassigned or gone from Jira: its tab goes too
            del self.comms_seen[k]
            await tabs.remove_pane(f"ct-{k}")
        for k in keys:
            if k not in self.comms_seen:
                self.comms_seen[k] = None
                await tabs.add_pane(TabPane(k, SelectLog(id=f"cl-{k}"), id=f"ct-{k}"))
        saved = self.layout_sizes.get("tabs") or []
        done = lambda k: k in card and graph.section(card[k][0]) in ("DONE", "DEPLOYED", "CANCELLED")
        place = {k: (done(k), saved.index(k) if k in saved else len(saved), i) for i, k in enumerate(keys)}  # open, then done; your order in each
        row = tabs.query_one("#tabs-list")
        for k in sorted(keys, key=place.get):
            t = row.query_one(f"#--content-tab-ct-{k}")
            t.set_class(done(k), "-done")
            row.move_child(t, after=row.children[-1]) if row.children[-1] is not t else None
        tabs.active = f"ct-{key}"
        self.action_close_ticket()  # opened from the ticket popup: the log replaces it
        self.query_one("#commsbox").add_class("-open")
        self._dim()

    def action_close_comms(self):
        self.query_one("#commsbox").remove_class("-open")
        self._dim()

    def action_close_log(self):
        self.query_one("#logbox").remove_class("-open")
        self._dim()

    def action_show_thread(self, i):
        msgs = bus.messages()
        if i < len(msgs):
            m, box = msgs[i], self.query_one("#threadbox")
            self.thread_msg = m
            box.border_title = f"{m.get('source', '')} · {m.get('chat') or m.get('from', '?')}"
            self.query_one("#thread", Static).update(graph.thread(m))
            box.add_class("-open")
            self._dim()
            self.call_after_refresh(self._scroll_to_message, m)

    def _scroll_to_message(self, m):
        """Open the thread at the message you clicked, not at the chat's oldest message, so its photos are in view."""
        body = self.query_one("#thread", Static)
        rows = len(self.console.render_lines(graph.thread(m, before=True), self.console.options.update_width(body.content_region.width)))
        self.query_one("#threadbox .popbody").scroll_to(y=rows, animate=False)

    def action_close_thread(self):
        self.query_one("#threadbox").remove_class("-open")
        self._dim()

    def send_reply(self):
        """CentralBrain posts the popup's text, word for word, in the open thread (Teams needs its browser, so no direct API)."""
        note, m = self.query_one("#threadnote", TextArea), self.thread_msg
        text = note.text.strip()
        if not text:
            return self.notify("Nothing to send.", title="Reply")
        brains.ask(f"Post this reply as me, word for word, in the {m.get('source', '')} thread with {m.get('from', '?')} "
                   f"(message id {m.get('id', '')}, sent {m.get('at', '')}, which said: {m.get('text', '')[:200]!r}). Reply:\n{text}")
        note.clear()
        self.notify(f"CentralBrain is sending your reply to {m.get('from', '?')}.", title="Reply")

    def action_close_popups(self):
        """Escape closes every popup at once, the command palette included."""
        if len(self.screen_stack) > 1:
            self.pop_screen()
        self.action_close_log(); self.action_close_thread(); self.action_close_comms(); self.action_close_ticket()

    def _dim(self):
        self.query_one("#backdrop").display = any(w.has_class("-open") for w in self.query("#logbox, #commsbox, #threadbox, #ticketpop"))

    def _popups(self):
        return [w for w in self.query("#logbox, #threadbox, #ticketpop") if w.has_class("-open")]

    def click_off(self, event):
        """A click outside an open popup closes it and goes no further: the card underneath never sees it."""
        if isinstance(event, events.MouseDown) and self._popups():
            try:
                hit = self.screen.get_widget_at(event.screen_x, event.screen_y)[0]
            except NoMatches:
                hit = None
            if not any(p in (hit.ancestors_with_self if hit else ()) for p in self._popups()):
                self.action_close_log(); self.action_close_thread(); self.action_close_ticket()
                self._swallow = True
                return True
        if isinstance(event, events.MouseDown):
            self._swallow = False  # a fresh press is never part of the closing click
        elif self.__dict__.get("_swallow") and isinstance(event, (events.MouseUp, events.Click)):
            self._swallow = not isinstance(event, events.Click)  # the closing click ends with its Click
            return True
        return False

    def get_default_screen(self):
        return Base(id="_default")

    def action_show_done(self):
        done = graph.finished_tiers([t["key"] for t in bus.pending()])[1][::-1]  # same set the header counts, one per ticket, oldest first
        self.notify("\n".join(f"{d['key']}  {time.strftime('%H:%M', time.localtime(d['at']))}" for d in done) or "Nothing finished yet today.",
                    title=f"{f'{len(done)} done today':<54}✕", timeout=10)  # ✕ at the top right; a click anywhere on a toast closes it

    def action_show_errors(self):
        errs = [(ts, b, m) for ts, b, lvl, m in bus.logs["All"] if lvl == "error"][-len(bus.error_times):][-8:] if bus.error_times else []  # none from before a clear
        body = "\n".join(escape(f"{ts} {b.removesuffix('Brain')}: {graph.fit(m, 70)}") for ts, b, m in errs) or "No errors."
        body += f"\n{bus.retries} retried" if bus.retries else ""
        links = "\n[@click=app.clear_errors][b]✕ Clear errors[/b][/]" + ("  [@click=app.fix_errors][b]🔧 Fix errors[/b][/]" if errs else "")
        self.notify(body + (links if errs or bus.retries else ""), title=f"{'Errors':<54}✕", timeout=15)

    def action_clear_errors(self):
        bus.error_times.clear()
        bus.retries = 0
        self.clear_notifications()
        self.notify("Errors cleared.", timeout=3)

    def action_fix_errors(self):
        """Hand every error since the last clear to CentralBrain in a new chat to fix, then clear them."""
        errs = [f"{ts} {b} : {m}" for ts, b, lvl, m in bus.logs["All"] if lvl == "error"][-len(bus.error_times):] if bus.error_times else []
        for s in bus.status.values():
            s["errors"] = 0
        Dashboard.action_clear_errors(self)
        if errs:
            self.action_new_chat()
            brains.ask("Find the root cause of these DevBot errors and fix them (code, config or engine state):\n" + "\n".join(errs))

    def load_jira(self):
        def fetch():
            try:
                graph.JIRA["assigned"], graph.JIRA["worked"], graph.JIRA["blocked"] = tracker.mine()
            except Exception as e:  # offline or no token: keep the last lists
                bus.log("CentralBrain", f"Couldn't load your Jira tickets ({e}); keeping the last lists.", "warning")
        self.run_worker(fetch, thread=True, group="jira")

    def action_fold(self, name):
        self.folded ^= {name}

    def action_toggle_brain(self, brain):
        self.brains_open ^= {brain}

    def action_select_ticket(self, key):
        self.ticket = None if self.ticket == key else key

    def action_show_ticket(self, key):
        self.popped = key
        box = self.query_one("#ticketpop")
        box.border_title = key
        self.query_one("#poplinks").update(graph.link("↗ Open in Jira", f"app.open_ticket('{key}')"))
        self.query_one("#ghlink").update("")
        for side in ("jira", "gh"):  # what it showed last time appears at once while the fresh copy loads
            self.query_one(f"#{side}info").update(self.seen_sides.get((key, side), f"[{DIM}]Loading…[/]"))
        box.add_class("-open")
        self._dim()
        self.load_ticket()

    def action_close_ticket(self):
        self.popped = None
        self.query_one("#ticketpop").remove_class("-open")
        self._dim()

    def load_ticket(self):
        """Fetch both sides off the UI thread; _refresh fetches again every 30 seconds while open, so CI progress moves."""
        key, self._popped_at = self.popped, time.time()

        def fetch(side, get, *shows):  # Jira and GitHub load side by side; GitHub draws once quickly, then again with photos
            try:
                data = get(key)
                for show in shows:
                    self.call_from_thread(self._show_side, key, side, data, show(data))
            except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as e:
                if (key, side) not in self.seen_sides:  # a refresh that fails keeps the last good view
                    self.call_from_thread(self._show_side, key, side, None, f"[{CORAL}]ERROR: Wait for GitHub API cooldown (<60 seconds)[/]" if "rate limit" in str(e)
                                      else f"[{CORAL}]Couldn't load: {escape(str(e))}[/]")
        self.run_worker(lambda: fetch("jira", tracker.issue, tracker.left), thread=True, group="ticketpop")
        self.run_worker(lambda: fetch("gh", tracker.prs, lambda c: tracker.right(c, photos=False), tracker.right), thread=True, group="ticketpop")

    def _show_side(self, key, side, data, text):
        if data is not None:
            self.seen_sides[(key, side)] = text
        if key == self.popped:
            if side == "gh":
                self.chain = data or []
                self.query_one("#ghlink").update(graph.link("↗ Open in GitHub", "app.open_pr()") if self.chain else "")
            self.query_one(f"#{side}info").update(text)

    def post_comment(self, side):
        if side == "thread":
            return self.send_reply()
        note = self.query_one(f"#{side}note", TextArea)
        text, key = note.text.strip(), self.popped
        if not text or not key or (side == "gh" and not self.chain):
            self.notify("Nothing to post." if not text else "No pull request to comment on.", title="Comment")
            return
        top = self.chain[0] if side == "gh" else None

        def post():
            try:
                tracker.comment_jira(key, text) if side == "jira" else tracker.comment_pr(top, text)
            except (OSError, ValueError, KeyError, subprocess.SubprocessError) as e:
                self.call_from_thread(self.notify, f"Not posted: {e}", title="Comment", severity="error")
                return
            self.call_from_thread(note.clear)
            self.call_from_thread(self.notify, f"Posted on {key if side == 'jira' else '#' + str(top['number'])}.", title="Comment")
            self.call_from_thread(self.load_ticket)
        self.run_worker(post, thread=True)

    def action_open_photo(self, name):
        """The original photo in Preview, at full resolution. Only files in state/media open."""
        f = graph.MEDIA / pathlib.Path(name).name
        if graph.PHOTO_NAME.fullmatch(f.name) and f.is_file():  # only image files, and only ever in Preview: never launched
            subprocess.run(["open", "-a", "Preview", "--", str(f)], check=False)

    def action_open_ticket(self, key):
        webbrowser.open(f"{bus.CONFIG['jira_url']}/browse/{key}")

    def action_open_pr(self):
        if self.chain and self.chain[0].get("url", "").startswith("https://github.com/"):
            webbrowser.open(self.chain[0]["url"])

    def action_retry_ticket(self, key):
        t = bus.failed.pop(key, None)
        if t and bus.handoff(t, t.get("from") or "You"):
            bus.retries += 1
            self.notify(f"{key} is queued for CodeBrain again.", title="Retry")

    def action_ticket_op(self, op, key):
        if not brains.ticket_op(op, key):
            self.notify(f"Press Run on {key} first.", title=key)

    def action_run_brain(self, brain):
        if brain == "CodeBrain":
            self.notify("CodeBrain runs whenever a ticket is queued.", title="Run now")
            return
        bus.held.discard(brain)
        bus.retries += bus.status[brain]["state"] == "error"
        bus.run_now(brain)
        self.notify(f"{brain} will run now.", title="Run now")

    def action_hold(self, brain):
        """Pause or resume one brain. A run in progress finishes; nothing new starts until you resume it."""
        if brain in bus.held:
            bus.held.discard(brain)
            bus.run_now(brain)
            self.notify(f"{brain} resumed.")
        else:
            bus.held.add(brain)
            self.notify(f"{brain} paused. A run in progress finishes first; use Stop to end it now.")

    PANES = {"rows": ("#lower", "#ticketbox"), "mid": ("#ticketbox", "#brains"), "chat": ("#brains", "#chat")}  # above, below each bar
    KEY = {"#ticketbox": "rows", "#brains": "mid", "#chat": "chat"}  # Messages takes whatever is left
    MIN = {"#lower": 5, "#ticketbox": 4, "#brains": 4, "#chat": 3}

    def pane_sizes(self, axis):
        return tuple(self.query_one(p).outer_size.height for p in self.PANES[axis])

    def drag(self, axis, x, y, start):
        """Resize from a Splitter drag: the bar is the edge between two cards, so one grows by what the other gives up
        and nothing else moves. Neither goes under its minimum, so Messages never climbs into the header and
        CentralBrain stays pinned to the bottom."""
        y0, (a0, b0) = start
        above, below = self.PANES[axis]
        if not self.query_one(below).display:  # CentralBrain is a bare input bar until you talk: nothing to resize
            return
        cap = lambda p: (m := self.query_one(p).styles.max_height) and p in ("#ticketbox", "#brains") and m.value or 999  # Tickets and Brains stop at their content
        lo = min(0, max(self.MIN[above] - a0, b0 - cap(below)))  # up: above can't shrink past its minimum, below can't outgrow its content
        hi = max(0, min(b0 - self.MIN[below], cap(above) - a0))
        d = max(lo, min(hi, y - y0))
        if self.query_one("#chat").display:  # CentralBrain keeps its height from here on, instead of following the cards above
            self.layout_sizes.setdefault("chat", self.query_one("#chat").outer_size.height)
        for pane, h in ((above, a0 + d), (below, b0 - d)):
            if pane in self.KEY:
                self.layout_sizes[self.KEY[pane]] = h
        self.layout_sizes["w"], self.layout_sizes["h"] = self.size  # the window these sizes were dragged in
        self.apply_sizes()

    def apply_sizes(self):
        """Dragged sizes scale with the window: a pane keeps its share when the terminal grows or shrinks. Each is clamped
        so the rest keep their minimum: Tickets/Brains 4 rows, chat 3, Messages 5."""
        s = self.layout_sizes
        W, H = self.size
        fy = H / s.get("h", H)
        fit = lambda v, lo, hi: max(lo, min(hi, round(v)))
        brains = fit(s["mid"] * fy, 4, H - 24) if "mid" in s else self.query_one("#brains").outer_size.height
        if "mid" in s:
            self.query_one("#brains").styles.height = brains
        if "rows" in s:  # a taller window's sizes leave CentralBrain on screen in a shorter one
            chat = self.query_one("#chat")
            talk = fit(s["chat"] * fy, 3, H - 16) if "chat" in s else chat.outer_size.height if chat.display else 3
            self.query_one("#ticketbox").styles.height = fit(s["rows"] * fy, 4, H - 17 - brains - talk)
        if "chat" in s:
            w = self.query_one("#chat")
            w.styles.max_height = w.styles.height = fit(s["chat"] * fy, 3, H - 16)

    def on_resize(self):
        if hasattr(self, "layout_sizes"):  # a resize can arrive before the saved sizes are loaded
            self.call_after_refresh(self.apply_sizes)  # self.size is the new size only after this event

    def action_stop_brain(self, brain):
        """End the brain's current run now and keep it paused. CodeBrain's ticket goes back in the queue."""
        bus.held.add(brain)
        claude.stop(brain)
        self.notify(f"{brain} stopped and paused. Press Resume to continue.")

    def action_focus_chat(self):
        self.query_one("#say", ChatInput).focus()

    def action_pause(self):
        (bus.paused.clear if bus.paused.is_set() else bus.paused.set)()
        state = "paused" if not bus.paused.is_set() else "resumed"
        bus.log("CentralBrain", "All engines paused. Runs already in progress will finish." if state == "paused" else "All engines resumed.", "warning")
        self.notify(f"Engines {state}. Runs already in progress finish first." if state == "paused" else "Engines resumed.")

    def action_scroll_log(self, how: str):
        getattr(self.query_one("#logs", TabbedContent).active_pane.query_one(SelectLog), how)(animate=False)

    def action_clear(self):
        self.query_one("#logs", TabbedContent).active_pane.query_one(SelectLog).clear()

    def action_restart(self):
        self.exit("restart")

    def on_mouse_scroll_down(self, event):
        """Scroll bars show while the wheel turns and hide a second later."""
        self.screen.add_class("-scrolling")
        if t := getattr(self, "_unscroll", None):
            t.stop()
        self._unscroll = self.set_timer(1, lambda: self.screen.remove_class("-scrolling"))

    on_mouse_scroll_up = on_mouse_scroll_down

    def on_mouse_up(self, event):
        """Selecting text copies it, so ⌘C works even in Apple Terminal, which keeps ⌘ keys to itself."""
        self.call_after_refresh(self.action_copy_selection)

    def action_copy_selection(self):
        area = self.focused if isinstance(self.focused, TextArea) else None
        text = (area.selected_text if area else "") or self.screen.get_selected_text() or ""
        if text and text != getattr(self, "_copied", None):
            self._copied = text
            self.copy_to_clipboard(text)

    def action_paste(self):
        if isinstance(self.focused, TextArea):
            self.focused.insert(subprocess.run(["pbpaste"], capture_output=True, text=True, timeout=5).stdout)

    def action_ignore(self):
        pass

    def action_copy_log(self):
        log = self.query_one("#logs", TabbedContent).active_pane.query_one(SelectLog)
        self.copy_to_clipboard(log.plain())

    def copy_to_clipboard(self, text):
        """Terminal clipboard escape (works in iTerm, kitty, tmux) plus pbcopy for macOS Terminal, which ignores it."""
        super().copy_to_clipboard(text)
        try:
            subprocess.run(["pbcopy"], input=text, text=True, timeout=5)
        except (OSError, subprocess.SubprocessError):
            pass
        self.notify(f"Copied {len(text)} characters.", title="Clipboard")


theme.apply(theme.saved(), __import__(__name__, fromlist=['_']))
