"""`uv run python -m devbot`: start all brains, show the dashboard. Ctrl+Q to stop, Ctrl+R to restart on new code."""
import os
import shutil
import signal
import sys

# tmux mangles kitty key reports into typed text like `[32;;32u`; ⌘ keys never reach us through tmux anyway.
os.environ["TEXTUAL_DISABLE_KITTY_KEY"] = "1"

from devbot import brains, bus, claude, ui

shutil.which("claude") or sys.exit("claude CLI not found on PATH")
shutil.which("gh") or sys.exit("gh CLI not found on PATH (needed by GitHubBrain and CodeBrain)")
# Only quitting from the dashboard stops DevBot: a closed window (hang-up) or a stray kill is ignored.
# A handler (not SIG_IGN) so child programs like claude, Chrome and caffeinate still stop normally.
for sig in (signal.SIGHUP, signal.SIGTERM):
    signal.signal(sig, lambda n, _: bus.log("CentralBrain", f"Ignored a {signal.Signals(n).name} signal: press q in the dashboard to stop DevBot.", "warning"))
# One DevBot at a time: a closed window leaves its DevBot running headless (hang-ups are ignored), and two copies
# double every engine run and meeting answer. The newest start wins; a reload (execv) keeps its pid.
pidfile = bus.ROOT / "state" / "devbot.pid"
old = int(pidfile.read_text() or 0) if pidfile.exists() else 0
if old and old != os.getpid() and "-m devbot" in os.popen(f"ps -o command= -p {old}").read():
    os.system(f"pkill -KILL -P {old}; kill -KILL {old}")  # SIGKILL: SIGTERM is ignored above
pidfile.write_text(str(os.getpid()))
brains.start()
if ui.Dashboard().run() == "restart":
    claude.stop_all()  # CodeBrain and the chat resume their sessions after the reload
    os.execv(sys.executable, [sys.executable, "-m", "devbot"])
