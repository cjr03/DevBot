"""Runnable checks: handoff validation and persistence, strict JSON parsing, stream parsing, redaction, chat commands, UI boot."""
import asyncio
import io
import datetime
import json
import os
import time
import logging
import pathlib
import queue
import subprocess
import tempfile
import threading
import unittest
from textual.geometry import Region
from textual.widgets import Tab
import urllib.error
import urllib.parse
import urllib.request
from unittest import mock

from rich.console import Console
from rich.segment import Segment
from rich.text import Text

from devbot import awake, brains, bus, claude, graph, meetings, theme, tracker, triggers, ui

_tmp = pathlib.Path(tempfile.mkdtemp())
bus.SPEND_FILE, bus.DONE_FILE = _tmp / "spend.json", _tmp / "done.json"  # tests never touch your real spend or done list
ui.LAYOUT_FILE = _tmp / "layout.json"
bus.CHATS_FILE = _tmp / "chats.json"  # tests never touch your real chats
bus.MESSAGES_FILE = _tmp / "messages.json"
theme.FILE = _tmp / "theme.txt"  # tests never flip your real light/dark choice
claude.BILLING_FILE = _tmp / "billing.json"  # tests never switch the account DevBot is billing
for _c in bus.chats.values():
    _c["session"] = None  # tests never resume, and so never talk into, your real CentralBrain conversations

_real_popen = subprocess.Popen
def _no_real_claude(cmd, *a, **k):  # a test that forgets to mock Popen must not start a live session that writes to your chat
    if cmd and cmd[0] == "claude":
        raise AssertionError("test tried to start a real claude process")
    return _real_popen(cmd, *a, **k)
subprocess.Popen = _no_real_claude

for _name in bus.BRAINS + ["Chat"]:  # keep test lines out of the real logs/*.log files
    logging.getLogger(_name).handlers.clear()


def command(app, title):
    """Run a ☰ Commands entry the way a click on it does."""
    next(c for c in app.get_system_commands(app.screen) if c.title == title).callback()


class T(unittest.TestCase):
    def setUp(self):
        bus.QUEUE_FILE = bus.ROOT / "state" / "queue.test.json"
        brains.INBOX_FILE = bus.ROOT / "state" / "chat_inbox.test.json"
        self._chats = (dict(bus.chats), bus.active)
        p = mock.patch.object(bus, "summarize", return_value=""); p.start(); self.addCleanup(p.stop)  # no real Claude calls
        for name in ("issue", "prs"):  # the ticket popup never reaches Jira or GitHub from a test
            p = mock.patch.object(tracker, name, side_effect=OSError("offline in tests")); p.start(); self.addCleanup(p.stop)
        bus._pending.clear()
        while not bus.handoffs.empty():
            bus.handoffs.get()

    def tearDown(self):
        bus.QUEUE_FILE.unlink(missing_ok=True)
        brains.INBOX_FILE.unlink(missing_ok=True)
        bus.chats.clear(); bus.chats.update(self._chats[0]); bus.select_chat(self._chats[1])

    def test_handoff_dedups_validates_and_persists(self):
        self.assertTrue(bus.handoff({"key": "TK-1", "summary": "x"}, "JIRABrain"))
        self.assertFalse(bus.handoff({"key": "TK-1"}, "TeamsBrain"))
        self.assertFalse(bus.handoff({"key": "rm -rf /; TK-1"}, "TeamsBrain"))
        self.assertEqual([t["key"] for t in json.loads(bus.QUEUE_FILE.read_text())], ["TK-1"])
        bus._pending.clear(); bus.handoffs.get()  # simulate restart
        self.assertEqual(bus.restore_queue(), 1)
        bus.handoff_done("TK-1")
        self.assertEqual(json.loads(bus.QUEUE_FILE.read_text()), [])

    def test_final_json_is_strict(self):
        self.assertEqual(claude.final_json('x {"a": 1} y\n{"summary": "ok"}'), {"summary": "ok"})
        self.assertIsNone(claude.final_json('{"summary": "ok"}\ntrailing words'))
        self.assertEqual(claude.final_json('done\n```json\n{\n  "summary": "ok"\n}\n```'), {"summary": "ok"})

    def test_run_streams_activity_and_parses_result(self):
        events = [{"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash", "input": {"command": "git status"}}]}},
                  {"type": "result", "result": 'done\n{"summary": "ok", "actions": 2, "handoff": [{"key": "AB-9"}], "memory": {"a": 1}, "outcome": "maybe"}',
                   "total_cost_usd": 0.5, "is_error": False}]
        proc = mock.Mock(stdout=io.StringIO("".join(json.dumps(e) + "\n" for e in events)), stderr=io.StringIO(""), returncode=0)
        with mock.patch("subprocess.Popen", return_value=proc) as popen, mock.patch.object(bus, "save_memory") as save:
            r = claude.run("JIRABrain", "my duty text")
        argv = popen.call_args.args[0]
        self.assertFalse(any("my duty text" in a for a in argv), "prompt must not be an argument: --mcp-config would swallow it")
        self.assertIn("my duty text", proc.stdin.write.call_args.args[0])
        self.assertEqual((r["summary"], r["actions"], r["cost_usd"], r["error"], r["handoff"]), ("ok", 0, 0.5, None, [{"key": "AB-9"}]))
        self.assertEqual((r["outcome"], r["waiting_on"]), (None, ""))  # a verdict outside done / waiting / blocked / nothing is no verdict
        self.assertEqual(bus.events[-1][1:3], ("JIRABrain", "Claude"))
        save.assert_called_once_with("JIRABrain", {"a": 1})
        self.assertEqual(bus.logs["JIRABrain"][-1][3], "Checking the code history")

    def test_error_times_record_each_error(self):
        n = len(bus.error_times)
        bus.log("JIRABrain", "fine")
        bus.log("JIRABrain", "broke", "error")
        self.assertEqual(len(bus.error_times), n + 1)
        self.assertLess(time.time() - bus.error_times[-1], 5)

    def test_clear_errors_empties_header_count_and_popup(self):
        bus.log("JIRABrain", "broke [x]", "error")
        app = mock.Mock(error_times=None)
        ui.Dashboard.action_show_errors(app)
        self.assertIn("Clear errors", app.notify.call_args.args[0])
        ui.Dashboard.action_clear_errors(app)
        self.assertEqual((len(bus.error_times), bus.retries), (0, 0))
        ui.Dashboard.action_show_errors(app)
        self.assertEqual(app.notify.call_args.args[0], "No errors.")

    def test_parallel_memory_saves_do_not_collide(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(bus, "memory_path", lambda b: pathlib.Path(d) / f"{b}.json"):
            ts = [threading.Thread(target=bus.save_memory, args=("CodeBrain", {"n": i})) for i in range(8)]
            [t.start() for t in ts]
            [t.join() for t in ts]
            self.assertIn("n", json.loads((pathlib.Path(d) / "CodeBrain.json").read_text()))

    def test_stalled_roam_scan_is_a_missed_scan(self):
        with mock.patch("subprocess.run", side_effect=subprocess.TimeoutExpired("roam_invite", 60)):
            self.assertIsNone(meetings.accept_invite(time.time()))

    def test_fix_errors_clears_and_hands_them_to_centralbrain(self):
        bus.log("JIRABrain", "broke badly", "error")
        bus.status["JIRABrain"]["errors"] = 2
        app = mock.Mock()
        with mock.patch.object(ui.brains, "ask") as ask:
            ui.Dashboard.action_fix_errors(app)
        self.assertIn("broke badly", ask.call_args.args[0])
        app.action_new_chat.assert_called_once()
        self.assertEqual((len(bus.error_times), bus.status["JIRABrain"]["errors"]), (0, 0))

    def test_failed_run_is_an_error_and_saves_nothing(self):
        proc = mock.Mock(stdout=io.StringIO(json.dumps({"type": "result", "result": '{"memory": {"x": 1}}', "is_error": True}) + "\n"),
                         stderr=io.StringIO(""), returncode=1)
        with mock.patch("subprocess.Popen", return_value=proc), mock.patch.object(bus, "save_memory") as save:
            r = claude.run("JIRABrain", "duty")
        self.assertEqual(r["error"], "claude reported failure")
        save.assert_not_called()

    def test_claude_login_then_api_key_then_cursor(self):
        def failed(msg):
            return mock.Mock(stdout=io.StringIO(json.dumps({"type": "result", "is_error": True, "result": msg}) + "\n"),
                             stderr=io.StringIO(""), returncode=1)
        ok = mock.Mock(stdout=io.StringIO(json.dumps({"type": "result", "result": '{"summary": "ok"}',
                                                      "total_cost_usd": 0.1, "is_error": False}) + "\n"),
                       stderr=io.StringIO(""), returncode=0)
        found = {"ANTHROPIC_API_KEY": "sk-ant-test", "CURSOR_API_KEY": "crsr_test"}
        billing = _tmp / "billing.json"
        billing.unlink(missing_ok=True)
        with mock.patch("subprocess.Popen", side_effect=[failed("You've hit your individual spend limit · run /usage-credits to ask your admin for a higher limit"), failed("Your credit balance is too low"), ok]) as popen, \
                mock.patch.object(claude, "keys", return_value=found), mock.patch.object(claude, "BILLING_FILE", billing), \
                mock.patch.dict("os.environ", {"ANTHROPIC_API_KEY": "from-shell"}):
            r = claude.run("JIRABrain", "duty")
        self.assertEqual(r["summary"], "ok")
        cmds, envs = zip(*((c.args[0], c.kwargs["env"]) for c in popen.call_args_list))
        self.assertEqual([c[0] for c in cmds], ["claude", "claude", claude.AGENT])
        self.assertNotIn("ANTHROPIC_API_KEY", envs[0])  # the signed-in Claude account, not any key
        self.assertEqual(envs[1]["ANTHROPIC_API_KEY"], "sk-ant-test")
        self.assertNotIn("ANTHROPIC_API_KEY", envs[2])
        self.assertEqual(envs[2]["CURSOR_API_KEY"], "crsr_test")
        saved = json.loads(billing.read_text())
        self.assertEqual(saved["tier"], "cursor")
        saved = saved["until"]
        until = claude.next_monday_8am()
        self.assertAlmostEqual(saved, until.timestamp(), delta=2)
        self.assertEqual(until.weekday(), 0)
        self.assertEqual((until.hour, until.minute), (8, 0))
        billing.unlink(missing_ok=True)  # a leftover "cursor" tier would make later chat tests open a second process

    def test_reply_mentioning_expiry_keeps_billing(self):
        talk = json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "Checked mail."},
                                                                        {"type": "text", "text": "Your token expired, renew it."}]}})
        ok = mock.Mock(stdout=io.StringIO(talk + "\n" + json.dumps({"type": "result", "result": '{"summary": "ok"}', "is_error": False}) + "\n"),
                       stderr=io.StringIO(""), returncode=0)
        billing = _tmp / "billing-talk.json"
        billing.unlink(missing_ok=True)
        with mock.patch("subprocess.Popen", return_value=ok) as popen, mock.patch.object(claude, "BILLING_FILE", billing):
            claude.run("JIRABrain", "duty")
        self.assertEqual(popen.call_count, 1)
        self.assertFalse(billing.exists())

    def test_sealed_answers_on_every_account(self):
        """Chat titles and meeting replies follow the account in use, and fall through to the next on a credit refusal."""
        billing = _tmp / "billing-sealed.json"
        billing.unlink(missing_ok=True)
        found = {"ANTHROPIC_API_KEY": "sk-ant-test", "CURSOR_API_KEY": "crsr_test"}
        runs = [mock.Mock(returncode=1, stdout="Credit balance is too low", stderr=""),
                mock.Mock(returncode=1, stdout="Credit balance is too low", stderr=""),
                mock.Mock(returncode=0, stdout=" Resize Bar Fix\n", stderr="")]
        with mock.patch.object(claude, "BILLING_FILE", billing), mock.patch.object(claude, "keys", return_value=found), \
                mock.patch("subprocess.run", side_effect=runs) as run:
            self.assertEqual(claude.sealed("name this", model="haiku"), "Resize Bar Fix")
        cmds = [c.args[0] for c in run.call_args_list]
        envs = [c.kwargs["env"] for c in run.call_args_list]
        self.assertEqual(cmds[0][0], "claude"); self.assertNotIn("ANTHROPIC_API_KEY", envs[0])        # Claude login
        self.assertEqual(envs[1]["ANTHROPIC_API_KEY"], "sk-ant-test")                                 # Anthropic API key
        self.assertEqual(cmds[2][0], claude.AGENT); self.assertEqual(envs[2]["CURSOR_API_KEY"], "crsr_test")  # Cursor
        self.assertNotIn("haiku", cmds[2])  # Claude model names don't apply to Cursor
        self.assertIn("--sandbox", cmds[2]); self.assertEqual(envs[2]["HOME"], run.call_args_list[2].kwargs["cwd"])  # no MCP or home files

    def test_chain_starts_over_at_monday_8am(self):
        billing = _tmp / "billing-reset.json"
        found = {"ANTHROPIC_API_KEY": "sk-ant-test", "CURSOR_API_KEY": "crsr_test"}
        with mock.patch.object(claude, "BILLING_FILE", billing), mock.patch.object(claude, "keys", return_value=found):
            billing.write_text(json.dumps({"tier": "cursor", "until": time.time() + 3600}))
            self.assertEqual(claude.tier(), "cursor")  # before Monday 08:00: the fallback holds
            billing.write_text(json.dumps({"tier": "cursor", "until": time.time() - 1}))
            self.assertEqual(claude.tier(), "login")   # after it: back to the Claude account

    def test_credentials_json_overrides_env_keys(self):
        path = _tmp / "credentials.json"
        path.write_text(json.dumps({"anthropic": {"api_key": "sk-ant-json"}, "CURSOR_API_KEY": "crsr_json"}))
        with mock.patch.object(claude, "CREDENTIALS_JSON", path):
            found = claude.keys()
        self.assertEqual((found["ANTHROPIC_API_KEY"], found["CURSOR_API_KEY"]), ("sk-ant-json", "crsr_json"))

    def test_api_key_runs_get_connector_fallbacks(self):
        for t, want in (("login", False), ("api", True), ("cursor", True)):
            with mock.patch.object(claude, "tier", return_value=t), \
                    mock.patch.object(claude, "stream", return_value={"result": "ok"}) as st:
                claude.run("OutlookBrain", "duty")
            self.assertEqual("drive the installed Microsoft Outlook app with AppleScript" in st.call_args.args[0], want, t)  # not just "AppleScript": the engine's memory may mention it

    def test_cursor_tool_calls_read_like_claude_ones(self):
        ev = {"type": "tool_call", "subtype": "started",
              "tool_call": {"shellToolCall": {"args": {"command": "git status"}, "description": "Check the repo"}}}
        block = claude.from_cursor(ev)["message"]["content"][0]
        self.assertEqual((block["name"], claude.describe(block)), ("Bash", "Check the repo"))
        self.assertIsNone(claude.from_cursor({**ev, "subtype": "completed"}))

    def test_log_redacts_tokens(self):
        bus.log("JIRABrain", "token ghp_abc123 at https://u:pw@x.y")
        msg = bus.logs["JIRABrain"][-1][3]
        self.assertNotIn("ghp_abc", msg); self.assertNotIn("u:pw", msg)

    def test_chat_commands(self):
        self.assertEqual(brains.apply({"op": "run", "brain": "TeamsBrain"}), "woke TeamsBrain")
        self.assertTrue(bus.wake["TeamsBrain"].is_set()); bus.wake["TeamsBrain"].clear()
        self.assertEqual(brains.apply({"op": "handoff", "key": "AB-1"}), "queued AB-1")
        self.assertIsNone(brains.apply({"op": "rm"}))
        self.assertEqual(brains.apply({"op": "reload"}), "reloading"); self.assertTrue(bus.reload.is_set()); bus.reload.clear()

    def test_nudge_detects_ignored_input(self):
        ok = mock.Mock(returncode=0, stderr="")
        with mock.patch.object(awake, "idle_seconds", side_effect=[60.0, 0.2]), mock.patch("subprocess.run", return_value=ok):
            self.assertTrue(awake.nudge()[0])
        with mock.patch.object(awake, "idle_seconds", side_effect=[60.0, 62.0]), mock.patch("subprocess.run", return_value=ok):
            good, msg = awake.nudge()
        self.assertFalse(good); self.assertIn("Accessibility", msg)

    def test_code_change_detected(self):
        with mock.patch.object(bus, "STARTED", 0):
            self.assertTrue(brains.code_changed())
        with mock.patch.object(bus, "STARTED", 4e9):
            self.assertFalse(brains.code_changed())

    def test_reload_blocker(self):
        import os, tempfile, time as t
        d = pathlib.Path(tempfile.mkdtemp())
        code, cfg = d / "a.py", d / "config.json"
        code.write_text("x = 1\n"); cfg.write_text("{}")
        old = t.time() - 60
        for f in (code, cfg):
            os.utime(f, (old, old))
        passing = mock.Mock(returncode=0, stderr="")
        with mock.patch.object(brains, "_sources", return_value=[code, cfg]), \
                mock.patch.object(brains.subprocess, "run", return_value=passing) as run_tests:
            self.assertIsNone(brains.reload_blocker())
            run_tests.return_value = mock.Mock(returncode=1, stderr="ERROR: test_x\nNameError: name 're' is not defined")
            brains._monitor.tested = 0.0
            self.assertIn("does not pass its tests", brains.reload_blocker())
            run_tests.return_value = passing
            self.assertIn("does not pass its tests", brains.reload_blocker())  # same version: not re-tested
            os.utime(code, (old + 1, old + 1))                                  # the fix lands
            bus.status["TeamsBrain"]["state"] = "running"
            self.assertIn("TeamsBrain", brains.reload_blocker())
            bus.status["TeamsBrain"]["state"] = "idle"
            code.write_text("def broken(:\n"); os.utime(code, (old, old))
            self.assertIn("does not compile", brains.reload_blocker())
            code.write_text("x = 2\n")
            self.assertEqual(brains.reload_blocker(), "files still changing")
        with mock.patch.object(brains, "code_changed", return_value=True), \
                mock.patch.object(brains, "reload_blocker", return_value="new code does not compile: x"), \
                mock.patch.object(brains.bus, "log") as log, mock.patch.object(brains.bus.reload, "is_set", return_value=False):
            brains._monitor.last_reason = None
            brains._auto_reload()
            self.assertEqual(log.call_args.args[2], "info")  # a blocked reload is not an error
            brains.bus.paused.clear()

    def test_steps_and_errors_read_as_english(self):
        d = claude.describe
        self.assertEqual(d({"name": "mcp__claude_ai_Atlassian__getJiraIssue", "input": {"issueIdOrKey": "DEMO-3223"}}), "Reading Jira ticket DEMO-3223")
        self.assertEqual(d({"name": "mcp__claude_ai_Microsoft_365__chat_message_search", "input": {"query": "Cole"}}), 'Searching Teams messages for "Cole"')
        self.assertEqual(d({"name": "Bash", "input": {"command": "ls", "description": "List repo files."}}), "List repo files")
        self.assertEqual(d({"name": "Bash", "input": {"command": "gh pr checks 9"}}), "Checking whether CI passed")
        self.assertEqual(d({"name": "mcp__teams-browser__browser_press_key"}), "Sending the Teams message")
        self.assertEqual(d({"name": "mcp__x__doSomethingOdd"}), "Using do something odd")
        self.assertIn("could not start", claude.plain_error("claude exit 1: boom"))
        self.assertIn("reload", claude.plain_error("interrupted"))
        self.assertEqual(brains.finished({"summary": "All quiet.", "actions": 1, "cost_usd": 0.5}, 75),
                         "All quiet. Made 1 change. Took 1 min 15 s, cost $0.50.")

    def test_idle_backoff(self):
        waits = []
        bus.paused.set()
        with mock.patch.object(bus.wake["RoamBrain"], "wait", side_effect=lambda t=None: waits.append(t)), \
                mock.patch.object(triggers, "live", set()), mock.patch.object(brains, "MIN_GAP", 0):
            for quiet in (0, 1, 2, 5):
                brains.nap("RoamBrain", quiet)
            triggers.live.add("RoamBrain")
            brains.nap("RoamBrain", 3)                # trigger can see Roam: waits on data alone, no timer
            brains.nap("RoamBrain", 0, failed=True)   # a failed run still retries on the timer
        base = brains.C["interval_seconds"]["RoamBrain"]
        self.assertEqual(waits, [base, base * 2, base * 2, base * 2, brains.MAX_IDLE, base])

    def test_chat_commands_split_from_reply(self):
        self.assertEqual(brains.split_commands('Done.\n{"commands": [{"op": "pause"}]}'), ("Done.", [{"op": "pause"}]))
        self.assertEqual(brains.split_commands('Plain answer {"a": 1} inline.'), ('Plain answer {"a": 1} inline.', []))

    def test_chat_message_goes_into_live_session(self):
        import threading
        hold = threading.Event()

        def open_session():  # a real session keeps its output open until it ends
            hold.wait(5)
            yield from ()
        proc = mock.Mock(stdout=open_session(), stdin=mock.Mock())
        proc.poll.return_value = None
        brains._slot(bus.active).update(proc=None, pending=0)
        with mock.patch("subprocess.Popen", return_value=proc) as popen:
            brains.ask("hello there")
            brains.ask("and a follow-up")
        self.assertEqual(popen.call_count, 1, "one long-lived session, not one process per message")
        self.assertIn("--input-format", popen.call_args.args[0])
        sent = [json.loads(c.args[0])["message"]["content"] for c in proc.stdin.write.call_args_list]
        self.assertTrue(sent[0].endswith("hello there") and sent[1].endswith("and a follow-up"))
        self.assertIn("<live_state>", sent[0])
        hold.set()
        brains._slot(bus.active).update(proc=None, pending=0)
        claude.PROCS.discard(proc)

    def test_one_result_answers_every_queued_message(self):
        proc = mock.Mock(stdout=iter(()), stdin=mock.Mock())
        proc.poll.return_value = None
        brains._slot(bus.active).update(proc=proc, pending=0, tier=claude.tier())  # the live session, on today's account
        brains.ask("first"); brains.ask("second, sent mid-turn")
        result = mock.Mock(stdout=iter([json.dumps({"type": "result", "total_cost_usd": 0}) + "\n"]))
        brains._chat_reader(result, bus.active)
        self.assertEqual(brains._slot(bus.active)["pending"], 0)
        self.assertEqual(brains._inbox().get(bus.active, []), [])  # nothing replayed after a restart
        self.assertNotEqual(bus.status["CentralBrain"]["state"], "running")  # so reloads are not blocked
        brains._slot(bus.active).update(proc=None, pending=0)
        claude.PROCS.discard(proc)

    def test_chat_resends_when_session_dies_before_replying(self):
        dead = mock.Mock(stdout=iter(()), stdin=mock.Mock()); dead.poll.return_value = None; dead.wait.return_value = 1
        brains._slot(bus.active).update(proc=dead, pending=0, retried=False)
        brains.ask("are you there")
        fresh = mock.Mock(stdout=iter(()), stdin=mock.Mock()); fresh.poll.return_value = None
        with mock.patch("subprocess.Popen", return_value=fresh), mock.patch("threading.Thread"):
            brains._chat_reader(dead, bus.active)  # exits without a result
            self.assertTrue(json.loads(fresh.stdin.write.call_args.args[0])["message"]["content"].endswith("are you there"))
            brains._slot(bus.active)["proc"] = dead
            brains._chat_reader(dead, bus.active)  # dies again: say so instead of going quiet
        self.assertIn("Send that again", bus.chat[-1]["text"])
        self.assertEqual(brains._inbox().get(bus.active, []), [])
        brains._slot(bus.active).update(proc=None, pending=0); claude.PROCS.discard(fresh)
        bus.set_state("CentralBrain", "ok", "")

    def test_spend_limit_retries_chat_on_api_key(self):
        spend = "You've hit your individual spend limit · run /usage-credits to ask your admin for a higher limit"
        lines = [json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": spend}]}}) + "\n",
                 json.dumps({"type": "result", "is_error": True, "result": spend}) + "\n"]
        dead = mock.Mock(stdout=iter(lines), stdin=mock.Mock()); dead.poll.return_value = None; dead.wait.return_value = -9
        brains._slot(bus.active).update(proc=dead, pending=1, retried=True)  # a previous crash must not block the next account
        brains._set_inbox(bus.active, ["hello there"])
        billing = _tmp / "billing-chat.json"
        billing.unlink(missing_ok=True)
        fresh = mock.Mock(stdout=iter(()), stdin=mock.Mock()); fresh.poll.return_value = None
        said = []
        with mock.patch("subprocess.Popen", return_value=fresh) as popen, mock.patch("threading.Thread"), \
                mock.patch.object(claude, "keys", return_value={"ANTHROPIC_API_KEY": "sk-ant-test", "CURSOR_API_KEY": "crsr_test"}), \
                mock.patch.object(claude, "BILLING_FILE", billing), mock.patch.object(bus, "say", side_effect=lambda *a, **k: said.append(a)):
            brains._chat_reader(dead, bus.active)
        self.assertFalse(any(spend in str(a) for a in said))
        self.assertTrue(json.loads(fresh.stdin.write.call_args.args[0])["message"]["content"].endswith("hello there"))
        self.assertEqual(popen.call_args.kwargs["env"]["ANTHROPIC_API_KEY"], "sk-ant-test")  # the account failed, so the key is next
        self.assertEqual(json.loads(billing.read_text())["tier"], "api")
        self.assertFalse(brains._slot(bus.active).get("retried"))
        brains._slot(bus.active).update(proc=None, pending=0); claude.PROCS.discard(fresh)
        bus.set_state("CentralBrain", "ok", "")

    def test_interrupted_cursor_chat_keeps_the_message(self):
        brains._set_inbox(bus.active, ["hello there"])
        brains._slot(bus.active)["pending"] = 1
        with mock.patch.object(claude, "stream", return_value={"is_error": True, "result": "", "error": "interrupted"}):
            brains._cursor_turn(bus.active, "hello there")
        self.assertEqual(brains._inbox().get(bus.active), ["hello there"])
        self.assertEqual(brains._slot(bus.active)["pending"], 1)
        brains._set_inbox(bus.active, [])
        brains._slot(bus.active).update(proc=None, pending=0)

    def test_chat_survives_restart(self):
        proc = mock.Mock(stdout=iter(()), stdin=mock.Mock())
        proc.poll.return_value = None
        brains._slot(bus.active).update(proc=proc, pending=0)
        brains.ask("first"); brains.ask("second")
        self.assertEqual(len(brains._inbox()[bus.active]), 2)  # both remembered until answered
        brains._slot(bus.active).update(proc=proc, pending=0); proc.stdin.reset_mock()  # simulate restart mid-reply
        with mock.patch("subprocess.Popen", return_value=proc):
            self.assertEqual(brains.resume_chat(), 2)
        sent = [json.loads(c.args[0])["message"]["content"] for c in proc.stdin.write.call_args_list]
        self.assertTrue(sent[0].startswith(brains.RESUMED) and sent[0].endswith("first"))  # finish the interrupted one first
        self.assertTrue(sent[1].endswith("second") and not sent[1].startswith(brains.RESUMED))
        self.assertEqual(len(brains._inbox()[bus.active]), 2)  # not duplicated by the replay
        brains._slot(bus.active).update(proc=None, pending=0)
        bus.set_state("CentralBrain", "ok", "")
        claude.PROCS.discard(proc)

    def test_chats_persist_name_switch_and_close(self):
        bus.chats.clear(); bus.chats["a"] = bus._blank("Chat 1", "sess-a"); bus.select_chat("a")
        b = bus.new_chat()
        self.assertEqual((bus.active, bus.chats[b]["name"]), (b, "New Chat"))
        bus.say("You", "please fix   the resize bars in the dashboard today")  # named after your first message
        self.assertEqual(bus.chats[b]["name"], "please fix the resize bars i…")
        bus.say("You", "another"); self.assertEqual(bus.chats[b]["name"], "please fix the resize bars i…")
        bus.step_start("s1", "Reading ui.py")  # cut off by a reload mid-step
        bus.say("CentralBrain", "background reply", "a")  # replies land in their own chat, never the open one
        self.assertEqual([e["text"] for e in bus.chats["a"]["log"]], ["background reply"])
        self.assertTrue(bus.chats["a"]["unread"]); self.assertFalse(bus.chats[b]["unread"])
        chats, active = bus._load_chats()  # what a reload sees
        self.assertEqual((list(chats), active, chats["a"]["session"]), (["a", b], b, "sess-a"))
        self.assertEqual([e["status"] for e in chats[b]["log"] if e["kind"] == "step"], ["ok"])  # no spinner forever
        bus.select_chat("a"); self.assertFalse(bus.chats["a"]["unread"])
        bus.select_chat("nope"); self.assertEqual(bus.active, "a")
        self.assertEqual(bus.new_chat() and bus.chats[bus.active]["name"], "New Chat")
        c3 = bus.active
        bus.delete_chat(c3); self.assertEqual(bus.active, b)  # closing the open chat shows its neighbour
        bus.delete_chat("a"); self.assertEqual((list(bus.chats), bus.active), ([b], b))  # closing another keeps yours
        bus.delete_chat(b); self.assertEqual(len(bus.chats), 1); self.assertEqual(bus.chats[bus.active]["name"], "New Chat")
        bus.say("CentralBrain", "late reply", "gone")  # a closed chat's reply is dropped, not misfiled
        self.assertEqual(list(bus.chat), [])
        bus.CHATS_FILE.write_text("{not json")
        chats, active = bus._load_chats()
        self.assertEqual((len(chats), chats[active]["name"]), (1, "New Chat"))

    def test_chat_title_is_a_short_summary(self):
        bus.chats.clear(); bus.chats["a"] = bus._blank("New Chat"); bus.select_chat("a")
        with mock.patch.object(bus, "summarize", return_value="Resize Bar Fix"):
            bus.say("You", "please fix the resize bars in the dashboard today")
            for _ in range(50):
                if bus.chats["a"]["name"] == "Resize Bar Fix": break
                time.sleep(0.02)
        self.assertEqual(bus.chats["a"]["name"], "Resize Bar Fix")
        bus._title("a", "x", "not the current name"); self.assertEqual(bus.chats["a"]["name"], "Resize Bar Fix")  # never overwrites a rename

    def test_chats_answer_independently_and_close_mid_reply(self):
        bus.chats.clear(); bus.chats["a"] = bus._blank("Chat 1"); bus.chats["b"] = bus._blank("Chat 2"); bus.select_chat("a")
        pa, pb = (mock.Mock(stdout=iter(()), stdin=mock.Mock()) for _ in "ab")
        pa.poll.return_value = pb.poll.return_value = None
        brains._slot("a").update(proc=pa, pending=0, tier="login"); brains._slot("b").update(proc=pb, pending=0, tier="login")
        brains.ask("to a"); brains.ask("to b", "b")
        self.assertTrue(json.loads(pb.stdin.write.call_args.args[0])["message"]["content"].endswith("to b"))
        self.assertEqual(brains._inbox(), {"a": [mock.ANY], "b": [mock.ANY]})
        brains._chat_reader(mock.Mock(stdout=iter([json.dumps({"type": "assistant", "session_id": "sb", "message": {"content": [
            {"type": "text", "text": "done in b"}]}}) + "\n", json.dumps({"type": "result", "total_cost_usd": 0}) + "\n"])), "b")
        self.assertEqual((bus.chats["b"]["session"], bus.chats["b"]["unread"]), ("sb", True))
        self.assertNotIn("done in b", [e["text"] for e in bus.chat])
        self.assertEqual(list(brains._inbox()), ["a"])  # b answered; a still owed a reply
        self.assertEqual(bus.status["CentralBrain"]["state"], "running")  # a is still answering: reloads wait
        brains.close_chat("a")
        pa.kill.assert_called_once()
        self.assertEqual((brains._inbox(), bus.active, "a" in brains._procs), ({}, "b", False))
        self.assertNotEqual(bus.status["CentralBrain"]["state"], "running")
        brains._chat_reader(mock.Mock(stdout=iter([json.dumps({"type": "assistant", "message": {"content": [
            {"type": "text", "text": "ghost"}]}}) + "\n"])), "a")  # the killed session's last words go nowhere
        self.assertNotIn("ghost", str([list(c["log"]) for c in bus.chats.values()]))
        brains._procs.clear(); claude.PROCS.discard(pa); claude.PROCS.discard(pb)

    def test_resume_chat_per_chat_and_old_inbox(self):
        bus.chats.clear(); bus.chats["a"] = bus._blank("Chat 1"); bus.chats["b"] = bus._blank("Chat 2"); bus.select_chat("a")
        brains.INBOX_FILE.write_text(json.dumps(["old single-chat message"]))  # written by the previous version
        self.assertEqual(brains._inbox(), {"a": ["old single-chat message"]})
        brains.INBOX_FILE.write_text(json.dumps({"b": ["m1", "m2"], "closed": ["x"]}))
        hold = threading.Event()

        def open_session():  # stays open until the test ends, like a live session
            hold.wait(5)
            yield from ()
        proc = mock.Mock(stdout=open_session(), stdin=mock.Mock()); proc.poll.return_value = None
        with mock.patch("subprocess.Popen", return_value=proc) as popen:
            self.assertEqual(brains.resume_chat(), 2)  # a closed chat's messages are not replayed
        self.assertEqual(popen.call_count, 1)
        sent = [json.loads(c.args[0])["message"]["content"] for c in proc.stdin.write.call_args_list]
        self.assertEqual(sent, [brains.RESUMED + "m1", "m2"])
        self.assertEqual(brains._inbox(), {"b": [brains.RESUMED + "m1", "m2"]})
        self.assertTrue(bus.chats["b"]["turn"]["started"])
        hold.set(); brains._procs.clear(); claude.PROCS.discard(proc); bus.set_state("CentralBrain", "ok", "")

    def test_chat_tabs_in_dashboard(self):
        bus.chats.clear(); bus.chats["a"] = bus._blank("Chat 1"); bus.select_chat("a")
        bus.paused.set()

        async def go():
            async with ui.Dashboard().run_test(size=(160, 64)) as pilot:
                app = pilot.app
                await pilot.pause(0.3)
                self.assertFalse(any(w.ALLOW_SELECT for w in app.query(ui.Splitter)))  # resize bars and gaps can't be selected
                self.assertFalse(app.query_one("#main").ALLOW_SELECT)
                self.assertFalse(any(w.ALLOW_SELECT for w in app.query("Clickable, PillButton, Tab")))  # buttons, tabs, links
                self.assertTrue(app.screen.ALLOW_SELECT and app.query_one("#say").ALLOW_SELECT)  # card text still can
                await pilot.mouse_down("#chat", offset=(2, 1))
                self.assertTrue(app.screen._selecting)
                await pilot.mouse_up("#chat", offset=(4, 1))
                await pilot.mouse_down(app.screen, offset=(0, 20))  # the margin beside the cards
                self.assertFalse(app.screen._selecting)
                await pilot.mouse_up(app.screen, offset=(0, 20))
                await pilot.mouse_down("#split-rows", offset=(8, 0))
                self.assertFalse(app.screen._selecting)
                await pilot.mouse_up("#split-rows", offset=(8, 0))
                self.assertTrue(app.query_one("#chattabs").render().plain.endswith(" + "))
                plus = len(app.query_one("#chattabs").render().plain) - 2
                await pilot.click("#chattabs", offset=(plus, 0), times=2); await pilot.pause(0.2)
                self.assertEqual(len(bus.chats), 2)  # a double-click on + opens one chat, not two
                bus.delete_chat(bus.active); await pilot.pause(0.2)
                app.query_one("#say").text = "half-typed"
                app.action_new_chat(); await pilot.pause(0.2)
                self.assertEqual((len(bus.chats), app.query_one("#say").text), (2, ""))
                bus.say("CentralBrain", "hi from a", "a"); await pilot.pause(0.2)
                self.assertIn("● Chat 1", app.query_one("#chattabs").render().plain)
                app.action_select_chat("a"); await pilot.pause(0.2)
                self.assertEqual(app.query_one("#say").text, "half-typed")  # your draft stayed with its chat
                with mock.patch.object(app, "exit") as exit_:
                    bus.reload.set(); app._refresh()
                    exit_.assert_not_called()  # the draft holds the reload
                    app.query_one("#say").text = ""; app._refresh(); bus.reload.clear()
                    exit_.assert_called_once_with("restart")
                app.query_one("#say").text = "half-typed"
                self.assertIn("hi from a", app.query_one("#chat").plain())
                command(app, "Close this chat"); await pilot.pause(0.2)
                self.assertIn("Yes, close it", app.query_one("#chattabs").render().plain)
                app.action_confirm_close(0); await pilot.pause(0.2)
                self.assertEqual(len(bus.chats), 2)
                self.assertEqual(app.query_one("#chattabs").render().plain.count("✕"), 2)  # every tab has its ✕
                bus.chats["a"]["name"] = "A very long chat name indeed"; await pilot.pause(0.3)
                widths = [b - a for a, b, _ in ui.ChatTabs.spans]
                self.assertEqual(len(set(widths)), 1)  # equal-width tabs
                self.assertIn("…", app.query_one("#chattabs").render().plain)  # long names cut short
                row = app.query_one("#chattabs")
                self.assertEqual(Text.from_markup(app.chat_tabs()).plain.rstrip()[-1], "+")  # + right after the last tab
                self.assertLess(Text.from_markup(app.chat_tabs()).cell_len, row.size.width)  # not pushed to the edge
                keep, extra = bus.active, [bus.new_chat() for _ in range(10)]
                self.assertLessEqual(Text.from_markup(app.chat_tabs()).cell_len, row.size.width)  # many tabs shrink to fit
                for cid in extra:
                    bus.delete_chat(cid)
                bus.select_chat(keep)
                self.assertIn(f"on {ui.PANEL}", ui.Dashboard.chat_tabs(app).split("│")[[s[2] for s in ui.ChatTabs.spans].index(bus.active)])  # open tab = the page
                self.assertEqual(row.styles.background.hex.lower(), ui.BG.lower())  # the strip it sits in
                app.action_close_chat("a", 1); await pilot.pause(0.2)  # ✕ closes at once, no question
                self.assertEqual((len(bus.chats), "a" in bus.chats), (1, False))
                self.assertNotIn("hi from a", app.query_one("#chat").plain())
        asyncio.run(go())

    def test_brains_have_one_blank_line_between_them(self):
        bus.paused.set()

        async def go():
            async with ui.Dashboard().run_test(size=(160, 64)) as pilot:
                pilot.app.brains_open.add(ui.WORKERS[0])
                await pilot.pause(0.5)
                rows = list(pilot.app.query(".brainrow"))
                for a, b in zip(rows, rows[1:]):
                    self.assertEqual(b.region.y - a.region.bottom, 1)
        asyncio.run(go())

    def test_pill_buttons_are_solid_with_rounded_corners(self):
        bus.paused.set()

        async def go():
            async with ui.Dashboard().run_test(size=(160, 64)) as pilot:
                await pilot.pause(0.3)
                send = pilot.app.query_one("#send")
                self.assertEqual(send.outer_size.width, len("➤ Send") + 6)  # 2 caps + 2 cells of fill each side
                pill = send.render()
                self.assertEqual((pill.plain[0], pill.plain[-1]), ("\ue0b6", "\ue0b4"))
                self.assertEqual(pill.get_style_at_offset(pilot.app.console, 2).bgcolor.triplet, send.styles.color.rich_color.triplet)
                btn = pilot.app.query_one("#cmdbtn")  # light fills (dark mode teal) get a black label, dark ones white
                ink = "#000000" if btn.styles.color.brightness > 0.6 else "#ffffff"
                self.assertEqual(btn.render().get_style_at_offset(pilot.app.console, 2).color.triplet.hex, ink)
        asyncio.run(go())

    def test_drag_chat_tab_to_reorder(self):
        bus.chats.clear()
        for cid in "abc":
            bus.chats[cid] = bus._blank("Chat " + cid)
        bus.select_chat("a"); bus.paused.set(); ui.ChatTabs.spans = []  # not an earlier test's tabs

        async def go():
            async with ui.Dashboard().run_test(size=(160, 64)) as pilot:
                for _ in range(50):  # the row is drawn on a timer; a busy machine can take a while
                    if len(ui.ChatTabs.spans) == 3: break
                    await pilot.pause(0.1)
                (a, _, _), _, (c, _, _) = ui.ChatTabs.spans
                await pilot.mouse_down("#chattabs", offset=(c + 1, 0))
                await pilot.mouse_up("#chattabs", offset=(a + 1, 0))  # the pilot sends the release click too, like a terminal
                await pilot.pause(0.2)
                self.assertEqual((list(bus.chats), bus.active), (["c", "a", "b"], "a"))  # moved; the open chat stays open, not the old tab under the drop

                async def drag(src, x):  # by mouse, once the row shows the current order; x(spans) is where you let go
                    for _ in range(50):
                        if [t for _, _, t in ui.ChatTabs.spans] == list(bus.chats): break
                        await pilot.pause(0.1)
                    at = {t: (l, r) for l, r, t in ui.ChatTabs.spans}
                    await pilot.mouse_down("#chattabs", offset=(at[src][0] + 1, 0))
                    await pilot.mouse_up("#chattabs", offset=(x(at), 0)); await pilot.pause(0.2)
                await drag("a", lambda at: at["b"][1] - 1)  # the open tab onto the right half of its neighbour: still open
                self.assertEqual((list(bus.chats), bus.active), (["c", "b", "a"], "a"))
                await drag("a", lambda at: at["c"][1] + 1)  # the open tab let go in the gap after the first tab
                self.assertEqual((list(bus.chats), bus.active), (["c", "a", "b"], "a"))
                at = {t: (l, r) for l, r, t in ui.ChatTabs.spans}  # the tab moves while you drag, before you let go
                await pilot.mouse_down("#chattabs", offset=(at["c"][0] + 1, 0))
                await pilot.hover("#chattabs", offset=(at["b"][1] - 1, 0)); await pilot.pause(0.2)
                self.assertEqual(list(bus.chats), ["a", "b", "c"])
                lifted = pilot.app.chat_tabs()  # it floats under the mouse, lifted in teal, held where you took it
                self.assertEqual(lifted.get_style_at_offset(pilot.app.console, at["b"][1] - 2).bgcolor.name, ui.TEAL.lower())
                await pilot.mouse_up("#chattabs", offset=(at["b"][1] - 1, 0)); await pilot.pause(0.2)
                self.assertIsInstance(pilot.app.chat_tabs(), str)  # set down: a plain row again
                bus.chats.update({k: bus.chats.pop(k) for k in "cab"})  # back to c, a, b
                await drag("c", lambda at: at["b"][1] + 10)  # a closed tab let go past the last one: goes last, open one stays open
                self.assertEqual((list(bus.chats), bus.active), (["a", "b", "c"], "a"))
                at = {t: (l, r) for l, r, t in ui.ChatTabs.spans}  # let go below the row, at an angle: still moves
                await pilot.mouse_down("#chattabs", offset=(at["c"][0] + 1, 0))
                await pilot.mouse_up("#chattabs", offset=(at["a"][0] + 1, 3)); await pilot.pause(0.2)
                self.assertEqual(list(bus.chats), ["c", "a", "b"])
        asyncio.run(go())

    def test_text_selection_everywhere_but_tabs(self):
        theme.apply("light", ui, graph); self.addCleanup(theme.apply, theme.saved(), ui, graph)  # colours below are the light palette, whatever theme this Mac saved
        async def go():
            async with ui.Dashboard().run_test(size=(160, 64)) as pilot:
                q = pilot.app.query_one
                self.assertEqual([q(s).allow_select for s in ("#tickets", "#messages", "#chat", "#line-JIRABrain", "#banner", "#log-All", "#chattabs")],
                                 [False, False, True, False, False, True, False])  # links and buttons can't be selected
                tickets = q("#tickets")
                tickets.update(graph.button("x", "app.bell", ui.MINT))
                await pilot.pause()
                await pilot.hover("#tickets", offset=(1, 0)); await pilot.pause()
                tickets.update(graph.button("x", "app.bell", ui.MINT))  # the live redraw gives the links new ids
                pill = [s for s in tickets.render_line(0) if s.style and s.style.meta.get("@click")]
                self.assertTrue(pill and all(ui.VIOLET.lower() in (s.style.color and s.style.color.name, s.style.bgcolor and s.style.bgcolor.name)
                                             for s in pill))  # hovered pill stays wholly violet, like the brains
                self.assertTrue(all(s.style.color.name == ui.VIOLET.lower() and s.style.bgcolor.name != ui.VIOLET.lower()
                                    for s in pill if s.text in ("\ue0b6", "\ue0b4")))  # caps stay round: violet glyph on the panel
                for x in range(3):  # cap, middle, cap: the whole pill is violet wherever the mouse sits, as actually painted
                    with mock.patch.object(graph, "tickets", lambda *a, **k: graph.button("x", "app.bell", ui.MINT)):
                        tickets.update(graph.tickets()); await pilot.hover("#tickets", offset=(x, 0)); await pilot.pause()
                        painted = [s for s in tickets.render_lines(Region(0, 0, tickets.region.width, 1))[0] if s.style and s.style.meta.get("@click")]
                    self.assertEqual([(s.style.bgcolor if s.text not in tickets.CAPS else s.style.color).name for s in painted], [ui.VIOLET.lower()] * 3)
                app, copied = pilot.app, []
                app.copy_to_clipboard = copied.append
                chat, pill = q("#chat"), app.query(ui.PillButton).first()
                with mock.patch.object(ui.Screen, "_watch__select_state", lambda scr, st: setattr(scr, "selections", {chat: 1, pill: 1})):
                    app.screen._watch__select_state(None)
                self.assertEqual(list(app.screen.selections), [chat])  # a drag across buttons leaves them unselected
                app.screen.selections = {}
                say = q("#say"); say.focus(); say.text = "hello"; say.select_all()
                await pilot.press("ctrl+c"); self.assertEqual(copied, [])  # Ctrl+C no longer copies
                await pilot.press("super+c"); self.assertEqual(copied, ["hello"])
                q("#threadbox").add_class("-open"); pop = q("#thread")  # popups render Markdown, which plain Textual cannot select
                pop.update(graph.thread({"from": "Jordan", "text": "**bold** words"})); await pilot.pause()
                from textual.selection import Selection
                text, _ = pop.get_selection(Selection(None, None))
                self.assertIn("Jordan", text); self.assertIn("bold words", text)
        asyncio.run(go())

    def test_unfinished_ticket_runs_are_continued(self):
        mem = {}
        t = {"key": "TK-7", "summary": "s", "context": "ctx", "from": "JIRABrain"}
        with mock.patch.object(bus, "load_memory", lambda b: dict(mem)), mock.patch.object(bus, "save_memory", lambda b, m: mem.update(m)):
            self.assertFalse(brains._continue_owed("CodeBrain", t, "Blocked on Jordan's sign-off."))  # waits on a person
            self.assertFalse(brains._continue_owed("CodeBrain", t, "PR #5 CI passed and it was merged into master."))
            self.assertTrue(brains._continue_owed("CodeBrain", t, "Merged master locally; tests running before push, not yet pushed."))
            q = bus.handoffs.get_nowait()
            self.assertEqual((q["key"], q["from"]), ("TK-7", "CentralBrain"))
            self.assertIn("not yet pushed", q["context"]); self.assertTrue(q["context"].startswith("ctx"))
            bus._pending.clear()
            for _ in range(2):  # a promise to watch CI is owed work too: the session that made it is gone
                self.assertTrue(brains._continue_owed("CodeBrain", t, "CI running; a background watcher will notify me."))
                bus.handoffs.get_nowait(); bus._pending.clear()
            self.assertFalse(brains._continue_owed("CodeBrain", t, "not yet pushed"))  # capped: no endless loop
            self.assertTrue(bus.handoffs.empty())

    def test_messages_tile_shows_only_addressed(self):
        first = bus.CONFIG["owner"].split()[0]
        self.assertTrue(graph.addressed(f"@{first} can you look?")); self.assertTrue(graph.addressed("hi @Everyone"))
        self.assertTrue(graph.addressed(f"hey {first.lower()}, quick one")); self.assertFalse(graph.addressed("No worries"))
        msgs = [{"text": "No worries", "from": "A", "chat": "Team"}, {"text": f"@{first} ping", "from": "B"},
                {"text": "need test evidence", "from": "C", "chat": "C"},
                {"text": f"@{first} sent it over", "from": "Roberts, " + first, "chat": "Team"},
                {"text": "daily updates here", "from": "Roberts, " + first, "chat": "Sync"}]
        with mock.patch.object(bus, "messages", return_value=msgs):
            raw = graph.messages(80); text = Text.from_markup(raw).plain
            with mock.patch.object(graph, "reply", side_effect=AssertionError("redrawn")):
                self.assertEqual(graph.messages(80), raw)  # unchanged messages and width: the last drawing, no rescan
        self.assertIn("\n\n", text); self.assertFalse(text.startswith("\n"))  # a blank line between messages, none on top
        self.assertIn("show_thread(1)", raw)  # click opens the original message's full thread
        self.assertNotIn("No worries", text); self.assertIn("ping", text); self.assertIn("test evidence", text)  # DMs always show
        long = graph.wrap("word " * 30, 20, "  ↳ ", "    ").split("\n")  # whole text, lined up under its first character
        self.assertEqual(long[0][:6], "  ↳ wo"); self.assertTrue(all(l.startswith("    w") and len(l) <= 20 for l in long[1:]))
        self.assertIn("You ↗ Team", text); self.assertIn("daily updates here", text)  # every sent message, named or not
        self.assertTrue(graph.mine(bus.CONFIG["owner"] + " (Bot)"))  # what you sent reads as sent

    def test_messages_merge_name_variants_drop_minutes_and_reply_from_thread(self):
        self.assertEqual(bus.norm("Garcia, Alex (Contractor)"), bus.norm("Alex Garcia"))
        self.assertEqual(bus.norm("🟢 Team - Platform"), bus.norm("Team - Platform"))
        me = bus.CONFIG["owner"]
        a = {"from": "Alex", "at": "2026-10-30T15:49:00Z", "text": "solve conflicts"}
        b = {"from": "Alex", "at": "2026-10-30T15:58:00Z", "text": "merge it"}
        r = {"from": me, "at": "2026-10-30T16:00:00Z", "text": "on it"}
        self.assertEqual(graph.reply(dict(a, reply="wrong", thread=[a, b, r])), "on it")
        self.assertEqual(graph.reply(dict(a, reply="wrong", thread=[r, a, b])), "")  # nothing said after it
        self.assertEqual(graph.reply(dict(a, reply="claimed")), "claimed")  # no thread: trust the engine
        old, new = dict(a, chat="Team", source="Teams", thread=[a]), {"chat": "Team", "source": "Teams", "id": "n", "thread": [a, b, r]}
        with mock.patch.object(bus, "messages", return_value=[new, old]):
            self.assertEqual(graph.reply(old), "on it")  # a later sync of the chat updates an older record
        self.assertIn("schema\\.skype\\.com\\/Reply", (bus.ROOT / "devbot" / "m365.mjs").read_text())  # quoted text isn't the reply's
        with tempfile.TemporaryDirectory() as d, mock.patch.object(bus, "MESSAGES_FILE", pathlib.Path(d) / "m.json"):
            bus.add_messages([{"from": "Magic Minutes", "text": "### Summary"}, dict(a, chat="Team")], "TeamsBrain")
            self.assertEqual([m["text"] for m in bus.messages()], ["solve conflicts"])
        same = [{"id": "x1", "chat": "Riley", "from": "Cole Roberts (Bot)", "at": "2026-10-30T21:29:00.851066Z", "text": "Hopping in"},
                {"id": "x2", "chat": "Riley", "from": "Cole's assistant (bot)", "at": "2026-10-30T14:29-07:00", "text": "Hopping in"},
                {"id": "x3", "chat": "Fonseca", "from": "Fonseca", "at": "2026-10-30T16:15:57.7400000Z", "text": "ok"},
                {"id": "x4", "chat": "Fonseca", "from": "Fonseca", "at": "2026-10-30T09:15:00-07:00", "text": "ok"}]
        self.assertEqual(len(bus._collapse(same)), 2)  # one moment in two time zones, one author spelled two ways: one message

    def test_teams_messages_mention_people_and_avoid_meetings(self):
        self.assertIn("Always @mention the people", claude.FRAME)
        self.assertIn("never propose or book a meeting", claude.FRAME)
        self.assertIn("Never suggest hopping on a Roam or Teams meeting", claude.FRAME)
        self.assertIn("NEVER STAY BLOCKED", claude.FRAME)  # blockers are asked about over Teams, not parked
        self.assertIn("answers something DevBot asked to unblock", brains.DUTY["TeamsBrain"])

    def test_chat_activity_stream(self):
        bus.chat.clear()
        bus.turn.update(started=bus.time.time() - 5, steps=0, thinking=False)
        bus.say("You", "add a feature")
        bus.step_start("t1", "Reading brains.py"); bus.step_start("t2", "Running the tests")
        bus.step_end("t1"); bus.step_end("t2", ok=False)
        bus.step_start("t3", "Committing code")
        bus.say("CentralBrain", "Working on **it**:\n- `tests`")

        async def go():
            async with ui.Dashboard().run_test(size=(160, 64)) as pilot:
                await pilot.pause(0.5)
                console = Console(width=100, record=True, file=io.StringIO())
                console.print(pilot.app.query_one("#chat").body.content)
                shown = console.export_text()  # CentralBrain's reply is rendered Markdown, not raw
                self.assertIn("⏺ Working on it:", shown); self.assertIn("• tests", shown); self.assertNotIn("**", shown)
                from textual.selection import Selection
                self.assertIn("Working on it:", pilot.app.query_one("#chat").body.get_selection(Selection(None, None))[0])  # still copyable
                text = pilot.app.query_one("#chat").plain()
                self.assertIn("✓ Reading brains.py", text)
                self.assertIn("✗ Running the tests", text)
                self.assertNotIn("✓ Committing code", text)  # still running: spinner, not a check
                self.assertRegex(text, r"Working… \(\d+s · 3 steps\)")
                bus.end_steps(); await pilot.pause(0.5)
                text = pilot.app.query_one("#chat").plain()
                self.assertIn("✓ Committing code", text)
                self.assertNotIn("Working…", text)
        bus.paused.set()
        asyncio.run(go())

    def test_trigger_wakes_brain_on_change(self):
        signals = {"TeamsBrain": "3"}
        checks = {"TeamsBrain": lambda: signals["TeamsBrain"], "JIRABrain": lambda: 1 / 0}
        last, broken = {}, set()
        with mock.patch.object(triggers, "CHECKS", checks):
            bus.wake["TeamsBrain"].clear()
            triggers.poll(last, broken)  # first look only records
            self.assertFalse(bus.wake["TeamsBrain"].is_set())
            signals["TeamsBrain"] = "4"
            triggers.poll(last, broken)
            self.assertTrue(bus.wake["TeamsBrain"].is_set())
            bus.wake["TeamsBrain"].clear()
            signals["TeamsBrain"] = ""  # we marked everything read: no wake-up
            triggers.poll(last, broken)
            self.assertFalse(bus.wake["TeamsBrain"].is_set())
            self.assertTrue(triggers.grew("2026-10-29T02:00|x", "2026-10-28T23:00|x"))
            self.assertIn("JIRABrain", broken)  # a failing check is reported, not fatal
        bus.wake["TeamsBrain"].clear()

    def test_github_trigger_ignores_own_pr_updates(self):
        done = mock.Mock(returncode=0, stdout="2026-10-30T16:00:00Z\n")
        with mock.patch.object(triggers.subprocess, "run", return_value=done) as run:
            self.assertEqual(triggers.github(), "2026-10-30T16:00:00Z")
        self.assertEqual([c.args[0][:3] for c in run.call_args_list], [["gh", "api", "notifications"]])

    def test_logs_and_cards_scroll_and_keep_place(self):
        from textual import events
        for i in range(400):
            bus.log("TeamsBrain", f"line {i}", "ok")

        async def wheel_up(pilot, w, n=5):
            x, y = w.region.x + 2, w.region.y + 1
            t = pilot.app.screen.get_widget_at(x, y)[0]
            for _ in range(n):
                pilot.app.screen._forward_event(events.MouseScrollUp(t, x, y, 0, -1, 0, False, False, False, screen_x=x, screen_y=y))
                await pilot.pause(0.05)

        async def go():
            bus.paused.set()
            async with ui.Dashboard().run_test(size=(160, 50)) as pilot:
                command(pilot.app, "Show TeamsBrain log"); await pilot.pause(0.6)  # unfolds the logs
                for sel in ("#log-TeamsBrain",):
                    w = pilot.app.query_one(sel)
                    self.assertGreater(w.max_scroll_y, 0, sel)
                    await wheel_up(pilot, w)
                    y = w.scroll_y
                    self.assertLess(y, w.max_scroll_y, f"{sel} did not scroll")
                    first = w.lines[0][1]
                    bus.log("TeamsBrain", "brand new line", "ok"); await pilot.pause(0.6)
                    self.assertLessEqual(len(w.lines), ui.SelectLog.MAX)
                    self.assertLess(w.scroll_y, w.max_scroll_y, f"{sel} jumped to the bottom")
        asyncio.run(go())

    def test_chat_input_drops_terminal_leak_sequences(self):
        self.assertEqual(ui.scrub_terminal_leaks("[<35;806;904M[<35;655;1097M<35;627;590M"), "")
        self.assertEqual(ui.scrub_terminal_leaks("this[32;;32uis"), "this is")
        self.assertEqual(ui.scrub_terminal_leaks("keep"), "keep")
        async def go():
            bus.paused.set()
            async with ui.Dashboard().run_test(size=(80, 24)) as pilot:
                say = pilot.app.query_one("#say")
                say.text = "this[32;;32uis[<35;1;1M"
                await pilot.pause()
                self.assertEqual(say.text, "this is")
        asyncio.run(go())

    def test_chat_box_stays_on_screen(self):
        async def go():
            bus.paused.set()
            async with ui.Dashboard().run_test(size=(160, 40)) as pilot:
                pilot.app.layout_sizes.update(rows=30)  # panes dragged big enough to overflow the screen
                pilot.app.apply_sizes(); await pilot.pause(0.5)
                say = pilot.app.query_one("#say").region
                self.assertTrue(say.height and say.bottom <= pilot.app.size.height, say)
                app = pilot.app
                app.layout_sizes.update(mid=1, rows=1, chat=0)  # too short, collapsed: clamped to the minimums
                app.apply_sizes(); await pilot.pause(0.3)
                self.assertEqual(app.query_one("#ticketbox").styles.height.value, 4)
                self.assertEqual(app.query_one("#brains").styles.height.value, 4)
                self.assertEqual(app.query_one("#chat").styles.height.value, 3)
            async with ui.Dashboard().run_test(size=(60, 20)) as pilot:  # far too small: scrolls rather than squashing
                await pilot.pause(0.3)
                q = lambda s: pilot.app.query_one(s).outer_size
                self.assertGreaterEqual(q("#main").width, 100)
                self.assertGreaterEqual(q("#brains").width, 30); self.assertGreaterEqual(q("#ticketbox").width, 30)
                self.assertGreaterEqual(q("#msgbox").height, 5); self.assertEqual(q("#say").height, 3)
        asyncio.run(go())

    def test_service_classification(self):
        self.assertEqual(claude.service({"name": "mcp__claude_ai_Atlassian__getJiraIssue"}), "Jira")
        self.assertEqual(claude.service({"name": "mcp__claude_ai_Microsoft_365__read_resource", "input": {"uri": "teams:///chats/x"}}), "Teams")
        self.assertEqual(claude.service({"name": "mcp__claude_ai_Microsoft_365__outlook_email_search"}), "Outlook")
        self.assertEqual(claude.service({"name": "Bash", "input": {"command": "gh pr checks 12"}}), "GitHub")
        self.assertEqual(claude.service({"name": "Read"}), "Claude")

    def test_ticket_view_feed_and_controls(self):
        bus.handoff({"key": "ZZT-1", "summary": "Fix the cache"}, "GitHubBrain")
        bus.set_state("CodeBrain", "running", "implementing ZZT-1", ticket="ZZT-1")
        bus.working["ZZT-1"] = {"started": time.time(), "activity": ""}
        bus.activity("CodeBrain", "Running a command", "ZZT-1")
        bus.log("CodeBrain", "Started implementing ZZT-1")
        for _ in range(4):
            bus.log("CodeBrain", "Checking code history", "tool")
        bus.ticket_cost.pop("ZZT-1", None)
        bus.ticked("CodeBrain", {"cost_usd": 0.25})
        try:
            with mock.patch.dict(graph.JIRA, {"assigned": {"ZZT-1": ("Dev In Progress", "Fix the cache")}, "worked": {}}):
                text = Text.from_markup(graph.tickets(width=120)).plain
                self.assertRegex(text, "IN PROGRESS \\(1\\)\n  [⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏] ZZT-1")  # the one being worked on spins
                for w in (30, 45):  # a narrow window still fits each row, so its buttons stay flush right instead of wrapping left
                    self.assertLessEqual(max(map(len, Text.from_markup(graph.tickets(width=w)).plain.split("\n"))), w)
            bus.log("GitHubBrain", "Handed off merge conflicts for ZZT-1")  # mentions the ID, but GitHub isn't assigned
            self.assertIn(("GitHubBrain", "ZZT-1"), [(e[1], e[4]) for e in graph.attributed()])  # which its popup shows
            self.assertEqual([c and c[0] for c in graph.controls("CodeBrain")], ["⏸ Pause", None, "■ Stop", "≡ Logs"])  # running: pause, stop, logs
            self.assertEqual(graph.controls("CodeBrain")[0][2], "yellow")  # pause is yellow
            self.assertIn(graph.YELLOW, graph.ticket_controls("X-1", running=True).split("■")[0])
            self.assertIn("■", Text.from_markup(graph.ticket_controls("ZZT-1", True)).plain)
            self.assertNotIn("▶", Text.from_markup(graph.ticket_controls("ZZT-1", True)).plain)
            self.assertEqual(Text.from_markup(graph.ticket_controls("ZZT-1")).plain.count("\ue0b6"), 2)  # idle: run and logs only
            self.assertTrue(Text.from_markup(graph.ticket_controls("ZZT-1")).plain.endswith("\ue0b4"))  # flush right, like the brains
            bus.held.add("TeamsBrain")
            self.assertEqual([c and c[0] for c in graph.controls("TeamsBrain")], [None, "▶ Resume", None, "≡ Logs"])  # paused: play resumes
            bus.held.discard("TeamsBrain")
        finally:
            bus.failed.clear(); bus.handoff_done("ZZT-1"); bus.working.clear()
            bus.set_state("CodeBrain", "idle", "", ticket="")

    def test_ticket_log_gets_every_action(self):
        claude.report_tools("GitHubBrain", {"message": {"content": [{"type": "tool_use", "name": "Bash", "input": {"command": "jira move ZZT-8 ZZT-9", "description": "Move tickets"}}]}})
        bus.log("CodeBrain", "ZZT-7: PR #9123: CI green", "ok")
        bus.log("GitHubBrain", "Replied on PR #9123")
        got = [(e[3], e[4]) for e in graph.attributed()[-4:]]
        self.assertEqual(got, [("Move tickets", "ZZT-8"), ("Move tickets", "ZZT-9"),  # in both tickets' logs, without the tag
                               ("ZZT-7: PR #9123: CI green", "ZZT-7"), ("Replied on PR #9123", "ZZT-7")])  # the PR's ticket

    def test_logs_survive_reload(self):
        a, b, c = ("1", "x"), ("2", "y"), ("3", "z")
        self.assertEqual(graph.after([a, b, c], b), [c])  # only what is new, even when the log is full
        self.assertEqual(graph.after([b, c], a), [b, c])  # the last shown rolled off: all of them
        with tempfile.TemporaryDirectory() as d:
            pathlib.Path(d, "CodeBrain.log").write_text("Oct 30 18:08:56    · DEMO-1 · Running a command\ncontinued\nOct 30 18:09:21  Done: DEMO-1: PR #2\n")
            self.assertEqual([l for _, l in bus.load_logs(pathlib.Path(d))],
                             [("18:08:56", "CodeBrain", "tool", "DEMO-1 · Running a command"), ("18:09:21", "CodeBrain", "ok", "DEMO-1: PR #2")])

    def test_parallel_tickets(self):
        bus.handoff({"key": "ZZT-3", "summary": "Third"}, "JIRABrain")
        bus.handoff({"key": "ZZT-4", "summary": "Fourth"}, "JIRABrain")
        bus.working.update({"ZZT-3": {"started": time.time(), "activity": ""}, "ZZT-4": {"started": time.time(), "activity": ""}})
        bus.log("CodeBrain", "Started implementing ZZT-3"); bus.log("CodeBrain", "Started implementing ZZT-4")
        claude.report_tools("CodeBrain", {"message": {"content": [{"type": "tool_use", "name": "Read", "input": {}}]}}, "ZZT-3")
        try:
            lines = graph.claude_lines()
            self.assertEqual(lines[-1][3], "ZZT-3")  # a tagged step belongs to its ticket even after ZZT-4 started
            self.assertFalse(lines[-1][2].startswith("ZZT-3"))  # and shows without the tag
            self.assertEqual(graph.attributed()[-1][4], "ZZT-3")  # and its log popup shows it too
            with mock.patch.dict(graph.JIRA, {"assigned": {"ZZT-3": ("In Progress", "t")}, "worked": {}}):
                self.assertIn("app.show_comms('ZZT-3')", graph.tickets(width=120))  # each ticket row has a logs button
                for op in ("pause", "stop"):  # and, while it runs, its own Pause and Stop
                    self.assertIn(f"app.ticket_op('{op}', 'ZZT-3')", graph.tickets(width=120))
            self.assertNotEqual(bus.working["ZZT-3"]["activity"], "")
            self.assertEqual(bus.working["ZZT-4"]["activity"], "")
            with mock.patch.dict(graph.JIRA, {"assigned": {"ZZT-3": ("In Progress", "t"), "ZZT-4": ("In Progress", "t")}, "worked": {"ZZT-5": ("Done", "d")}}):
                text = Text.from_markup(graph.tickets(width=120)).plain
                self.assertEqual(sum(text.count(c) for c in graph.SPIN), 2)  # both worked tickets spin
                self.assertNotIn("○", text)
                self.assertRegex(text, r"ZZT-3 .*\n.*↳ .*\n\n.*ZZT-4")  # a blank line between tickets
                self.assertRegex(text, r"ZZT-4 +t +[▰▱]{5} (~\S+|soon) +\ue0b6⏸\ue0b4 \ue0b6■\ue0b4 \ue0b6≡\ue0b4\n    ↳ starting\n\n  ▾")  # running: bar, time left (soon once past a typical run, which other tests vary) and buttons at the right
            with mock.patch.dict(graph.JIRA, {"assigned": {"ZZT-6": ("In Progress", "t")}, "worked": {"ZZT-5": ("Done", "d")}}):
                text = Text.from_markup(graph.tickets(width=120)).plain
                self.assertIn("    ZZT-6", text)  # open, not being worked on: no icon
                self.assertRegex(text, r"ZZT-5 +d {80,}\ue0b6▶\ue0b4 \ue0b6≡\ue0b4")  # not running: no bar, buttons at the far right
            with mock.patch.dict(graph.JIRA, {"assigned": {}, "worked": {"ZZT-5": ("Done", "w " * 60)}}):
                self.assertRegex(Text.from_markup(graph.tickets(width=120)).plain, r"ZZT-5 +(w ){45}w +\ue0b6▶")  # no bar: the title runs on up to the buttons
            with mock.patch.dict(graph.JIRA, {"assigned": {"ZZT-3": ("In Progress", "t")}, "worked": {}, "blocked": {"ZZT-3": ["ZZT-9"]}}):
                self.assertIn("↳ Blocked by ZZT-9", Text.from_markup(graph.tickets(width=120)).plain)  # a blocker beats the working line
                self.assertIn("    ZZT-5", text)  # done: no activity icon
            procs = [mock.Mock(), mock.Mock()]
            claude.RUNNING["CodeBrain"] = set(procs)
            claude.stop("CodeBrain")
            self.assertTrue(all(p.terminate.called for p in procs))  # Stop ends every parallel run
        finally:
            claude.RUNNING.pop("CodeBrain", None); bus.working.clear()
            bus.handoff_done("ZZT-3"); bus.handoff_done("ZZT-4")

    def test_started_ticket_moves_to_dev_in_progress(self):
        seen = []
        def fake_run(brain, duty, **kw):
            seen.append(duty); return {"summary": "", "actions": 0, "handoff": [], "cost_usd": 0, "error": "interrupted"}
        with mock.patch.object(claude, "run", fake_run), mock.patch.object(brains, "_save_ticket_sessions"), \
                mock.patch.object(brains, "_ticket_sessions", return_value={}):
            brains._implement("CodeBrain", {"key": "ZZT-9", "from": "JIRABrain", "summary": "s"})
        bus.handoff_done("ZZT-9")
        self.assertIn("transition it to DEV IN PROGRESS", seen[0])
        self.assertIn("Never use a fork", seen[0])
        self.assertIn("transition ZZT-9 to IN QA", seen[0])  # an opened PR moves the ticket to IN QA

    def test_jira_handoff_reruns_a_done_ticket(self):
        ran = []
        with mock.patch.object(bus, "load_memory", return_value={"done": ["ZZT-8"]}), \
                mock.patch.object(claude, "run", lambda *a, **k: ran.append(1) or {"summary": "", "actions": 0, "handoff": [], "cost_usd": 0, "error": "interrupted"}), \
                mock.patch.object(brains, "_save_ticket_sessions"), mock.patch.object(brains, "_ticket_sessions", return_value={}):
            brains._implement("CodeBrain", {"key": "ZZT-8", "from": "JIRABrain", "summary": "s"})  # still short of IN QA: iterate again
            brains._implement("CodeBrain", {"key": "ZZT-8", "from": "TeamsBrain", "summary": "s"})  # chat mentions of a done ticket are skipped
        bus.handoff_done("ZZT-8")
        self.assertEqual(ran, [1])

    def test_every_queued_ticket_gets_an_agent(self):
        started, release = [], threading.Event()
        def fake(brain, t):
            started.append(t["key"]); release.wait(5)
        q = queue.Queue()  # private queue: the worker thread outlives the test and stays blocked on it
        with mock.patch.object(brains, "_implement", fake), mock.patch.object(bus, "handoffs", q):
            threading.Thread(target=brains.claude_worker, daemon=True).start()
            for k in ("ZZT-5", "ZZT-6", "ZZT-7", "ZZT-8"):
                q.put({"key": k})
            deadline = time.time() + 5
            while len(started) < 4 and time.time() < deadline:
                time.sleep(0.01)
            release.set()
        self.assertEqual(sorted(started), ["ZZT-5", "ZZT-6", "ZZT-7", "ZZT-8"])  # all at once, none waits for a lane

    def test_photos_keep_their_aspect_ratio(self):
        from PIL import Image
        rows = lambda: sum("▀" in l for l in self._draw(tracker.Picture(Image.new("RGB", (100, 50)))).splitlines())
        with mock.patch.object(tracker, "KITTY", None):
            with mock.patch.object(tracker, "CELL", 2.0):
                self.assertEqual(rows(), 5)  # 100x50 at 20 columns: 5 rows of cells twice as tall as wide
            with mock.patch.object(tracker, "CELL", 2.5):
                self.assertEqual(rows(), 4)  # taller cells: fewer rows, same shape
        made = []
        fake = lambda img, **kw: made.append(kw) or Text("x")
        with mock.patch.object(tracker, "KITTY", fake):
            self._draw(tracker.Picture(Image.new("RGB", (100, 50))))
        self.assertEqual(made, [{"width": 20, "height": "auto"}])  # real images: the terminal keeps the ratio

    def test_photo_too_tall_for_the_terminal_falls_back_to_half_blocks(self):
        from PIL import Image

        class TooTall:
            def __init__(self, img, **kw): pass
            def __rich_console__(self, console, options): raise ValueError("Image to large to render")
        with mock.patch.object(tracker, "KITTY", TooTall):
            self.assertIn("▀", self._draw(tracker.Picture(Image.new("RGB", (100, 50)))))

    def test_photos_draw_small_however_tall(self):
        from PIL import Image
        from rich.console import Console
        def lines(size):
            c = Console(width=200, height=500, record=True, color_system="truecolor")
            with mock.patch.object(tracker, "KITTY", None):
                c.print(tracker.Picture(Image.new("RGB", size)))
            return [l for l in c.export_text().splitlines() if "▀" in l]
        self.assertLessEqual(len(lines((1000, 20000))), tracker.MAX_ROWS + 1)  # a long screenshot stays a short thumbnail
        self.assertLessEqual(max(len(l.rstrip()) for l in lines((2000, 100))), tracker.MAX_COLS)  # a wide one stays narrow

    def _draw(self, r):
        from rich.console import Console
        c = Console(width=20, height=25, record=True, color_system="truecolor")  # height set: a dumb TERM ignores width alone
        c.print(r)
        return c.export_text()

    def test_thread_draws_saved_photos_only_from_media(self):
        from PIL import Image
        from rich.console import Console
        media = pathlib.Path(tempfile.mkdtemp())
        Image.new("RGB", (4, 4), "red").save(media / "c1-0.png")
        with mock.patch.object(graph, "MEDIA", media):
            m = {"text": "x", "thread": [{"from": "Ana", "text": "look", "images": ["/etc/../tmp/c1-0.png", "../../missing.png"]}]}
            out = Console(width=80, record=True, color_system="truecolor")
            out.print(graph.thread(m))
            text = out.export_text()
        self.assertIn("▀", text)  # the photo, found by its name inside state/media whatever path was reported
        self.assertIn("missing.png: couldn't show", text)  # a bad one is a dim line, not an error
        out.print(graph.thread({"text": "x", "thread": ["Ana: plain line"]}))
        self.assertIn("Ana: plain line", out.export_text())  # a thread line reported as plain text still shows
        m = {"at": "2026-01-01T10:05", "text": "me", "thread": [{"from": "A", "at": "2026-01-01T10:00", "text": "old"},
                                                               {"from": "B", "at": "2026-01-01T10:05", "text": "me"}, {"from": "C", "text": "later"}]}
        out = Console(width=80, record=True); out.print(graph.thread(m, before=True)); text = out.export_text()
        self.assertIn("old", text); self.assertNotIn("me", text.replace("A  ", ""))  # only what precedes the clicked message

    def test_wrapped_log_lines_keep_their_indent(self):
        if True:
            log = ui.SelectLog()
            log.write("x", "x", ("[dim]10:00:00[/] ", 9, "white", "word " * 40))
            m = log.lines[0][2]
            out = Text.from_markup(f"{m[0]}[{m[2]}]{graph.hang(m[3], m[1], 60)}[/]").plain.splitlines()
            self.assertGreater(len(out), 1); self.assertTrue(all(l.startswith(" " * 9) for l in out[1:]))

    def test_day_spend_recovers_from_logs(self):
        log = pathlib.Path(tempfile.mkdtemp())
        (log / "logs").mkdir()
        (log / "logs" / "X.log").write_text(time.strftime("%b %d") + " 10:00:00  Done: ok. Took 1 s, cost $1.25.\nJan 01 10:00:00  Done: old. cost $9.00.\n"
                                       + time.strftime("%b %d") + " 10:01:00  You: never orphan the cost $ suffix\n")
        with mock.patch.object(bus, "ROOT", log):
            self.assertAlmostEqual(bus.logged_today(), 1.25)

    def test_worked_includes_my_pr_tickets(self):
        def jira(path):
            if "ZZP-2" in urllib.parse.unquote_plus(path):
                raise urllib.error.HTTPError(path, 400, "unknown", {}, None)
            hit = "ZZP-1" in urllib.parse.unquote_plus(path)
            return {"issues": [{"key": "ZZP-1", "fields": {"status": {"name": "Done"}, "summary": "Theirs", "issuelinks": [
                {"type": {"inward": "is blocked by"}, "inwardIssue": {"key": f"ZZB-{n}", "fields": {"status": {"statusCategory": {"key": cat}}}}}
                for n, cat in ((1, "indeterminate"), (2, "done"))]}}] if hit else []}
        with mock.patch.object(tracker, "_jira", jira), mock.patch.object(tracker, "_gh", return_value=[{"title": "ZZP-1 fix"}, {"title": "ZZP-2 x"}]):
            self.assertEqual(tracker.mine(), ({}, {"ZZP-1": ("Done", "Theirs")}, {"ZZP-1": ["ZZB-1"]}))  # only open blockers  # a PR you wrote counts; an unknown key is skipped
        with mock.patch.object(tracker, "_jira", jira), mock.patch.object(tracker, "_gh", side_effect=OSError("HTTP 403: API rate limit exceeded")):
            self.assertEqual(tracker.mine()[1], {"ZZP-1": ("Done", "Theirs")})  # a rate-limited search keeps the last PR keys, Jira still loads
        tracker._pr_keys = set()

    def test_jira_assigned_and_worked(self):
        bus.finished("ZZJ-1", 10, 0.1, "Waiting on review.")
        jira = {"assigned": {"ZZJ-2": ("IN CODE REVIEW", "Open one")}, "worked": {"ZZJ-1": ("DEPLOYED", "Shipped one")}}
        with mock.patch.dict(graph.JIRA, jira):
            text = Text.from_markup(graph.tickets(width=120)).plain
            self.assertIn("IN CODE REVIEW (1)", text); self.assertIn("ZZJ-2", text)
            self.assertNotIn("CURRENT", text); self.assertNotIn("WAITING", text); self.assertNotIn("BLOCKED (0)", text)
            self.assertIn("DEPLOYED (1)", text); self.assertIn("Shipped one", text)  # Jira's status wins over the finished run
            self.assertLess(text.index("IN CODE REVIEW"), text.index("DEPLOYED"))  # in workflow order
            self.assertNotIn("ZZJ-2", Text.from_markup(graph.tickets(width=120, folded={"IN CODE REVIEW"})).plain)  # collapsed
        with mock.patch.dict(graph.JIRA, {"assigned": {}, "worked": {"ZZJ-5": ("IN CODE REVIEW", "Reassigned"), "ZZJ-6": ("Done (Prod Movement)", "Mine, done")}}):
            text = Text.from_markup(graph.tickets(width=120)).plain
            self.assertNotIn("ZZJ-5", text); self.assertIn("DONE (1)", text)  # an open ticket now someone else's is not shown
        with mock.patch.dict(graph.JIRA, {"assigned": {"ZZJ-4": ("X')][@click=app.quit][", "t")}, "worked": {}}):
            self.assertNotIn("app.quit", graph.tickets(width=120))  # a status name cannot inject a click action
        self.assertEqual([graph.section(s) for s in ("To Do", "Dev In Progress", "Closed", "Peer Review", "QA")], ["BACKLOG", "IN PROGRESS", "DONE", "IN CODE REVIEW", "IN QA"])
        long = {"worked": {"ZZJ-3": ("DONE", "word " * 30)}, "assigned": {}}
        with mock.patch.dict(graph.JIRA, long):  # wrapped titles line up under where the title started
            lines = [l for l in Text.from_markup(graph.tickets(width=80)).plain.split("\n") if "word" in l]
            self.assertGreater(len(lines), 1)
            self.assertEqual({l.index("word") for l in lines}, {16})

    def test_ticket_buttons(self):
        class Inline:  # threads run right away, so the whole Run can be checked
            def __init__(self, target, args, **_): self.go = lambda: target(*args)
            def start(self): self.go()
        central = {"summary": "PR #9 has a review comment.", "actions": 0, "cost_usd": 0, "error": None,
                   "handoff": [{"key": "ZZB-1", "context": "fix it"}, {"key": "ZZB-7"}],
                   "tasks": [{"brain": "TeamsBrain", "task": "ask Riley"}, {"brain": "OutlookBrain", "task": "no"}]}
        task = {"summary": "Asked.", "actions": 1, "cost_usd": 0, "error": None, "handoff": []}
        with mock.patch.object(bus, "handoff", return_value=True) as handoff, mock.patch.object(claude, "stop") as stop, \
                mock.patch.object(brains.threading, "Thread", Inline), mock.patch.object(claude, "run", side_effect=[central, task]) as run, \
                mock.patch.object(bus, "ticked"):
            self.assertFalse(brains.ticket_op("pause", "ZZB-1"))  # nothing to pause before Run
            self.assertTrue(brains.ticket_op("run", "ZZB-1"))
            self.assertEqual([c[0][0] for c in run.call_args_list], ["CentralBrain", "TeamsBrain"])  # CentralBrain, then its hand-off
            self.assertEqual({c[1]["tag"] for c in run.call_args_list}, {"ZZB-1"})  # every step lands in the ticket's log
            self.assertEqual([c[0][0]["key"] for c in handoff.call_args_list], ["ZZB-1"])  # code work goes to CodeBrain, other tickets don't
            self.assertNotIn("ZZB-1", bus.working)
            with mock.patch.object(bus, "working", {"ZZB-1": {}}):
                self.assertTrue(brains.ticket_op("stop", "ZZB-1"))
            stop.assert_called_with("ZZB-1")
            with mock.patch.dict(claude.RUNNING, {"ZZB-1": {object()}}):
                self.assertTrue(brains.ticket_op("pause", "ZZB-1"))  # a hand-off run still going stops too
        self.assertIn(("TeamsBrain", "ZZB-1"), [(e[1], e[4]) for e in graph.attributed()])
        with mock.patch.dict(graph.JIRA, {"assigned": {"ZZB-3": ("IN QA", "t")}, "worked": {"ZZB-4": ("DONE", "d"), "ZZB-5": ("IN QA", "gone")}}):
            self.assertEqual(set(graph.card_tickets()), {"ZZB-3", "ZZB-4"})  # the ticket log's tabs: the card's tickets, not reassigned ones
        with mock.patch.object(bus, "_save_queue"), mock.patch.object(bus, "handoffs"), mock.patch.dict(bus._pending):
            self.assertTrue(bus.handoff({"key": "ZZB-2"}, "You"))  # a Run from Cole lands in the ticket's log only
        self.assertIn(("You", "ZZB-2"), [(e[1], e[4]) for e in graph.attributed()])
        self.assertNotIn("You", bus.logs)

    def test_done_section_and_poll_collapse(self):
        bus.finished("ZZD-1", 75, 0.5, "PR is mergeable; waiting on CI before the Jira comment.")
        with mock.patch.object(graph, "finished_tiers", return_value=([], [{"key": "ZZD-9", "outcome": "Merged."}])), mock.patch.dict(graph.JIRA, {"assigned": {}, "worked": {}}):
            self.assertNotIn("ZZD-9", Text.from_markup(graph.tickets(width=120)).plain)  # only Jira fills the card
        bus.finished("ZZD-3", 20, 0.1, "DEMO-3288 is still blocked: Sam and Jordan still have to sign off.")
        self.assertEqual(graph.waiting({"outcome": "CI is still running (8 passed, 32 pending, 0 failed so far)."})[0], "CI 8/40")
        self.assertEqual(graph.waiting({"outcome": "DEMO-3288 is still blocked: Sam and Jordan still have to sign off."})[0], "BLOCKED")
        bus.finished("ZZD-2", 30, 0.2, "PR merged; CI green.")
        self.assertEqual(graph.tag("Pushed the fix."), "AWAITING")  # unclear is not done
        self.assertEqual(graph.tag("DEMO-3288 is blocked: no creds."), "BLOCKED")
        self.assertEqual(graph.money(0), "—")

    def test_structured_result_reaches_the_table(self):
        text = 'Asked Alex.\n{"summary": "PR #12 open; asked Alex to review.", "outcome": "blocked", "waiting_on": "Alex", "actions": 1}'
        proc = mock.Mock(stdout=io.StringIO(json.dumps({"type": "result", "result": text, "total_cost_usd": 0.1, "is_error": False}) + "\n"),
                         stderr=io.StringIO(""), returncode=0)
        with mock.patch("subprocess.Popen", return_value=proc), mock.patch.object(bus, "save_memory"), \
                mock.patch.object(brains, "_save_ticket_sessions"), mock.patch.object(brains, "_ticket_sessions", return_value={}):
            brains._implement("CodeBrain", {"key": "ZZS-1", "from": "JIRABrain", "summary": "s"})
        d = next(x for x in bus.done_today() if x["key"] == "ZZS-1")
        self.assertEqual((d["verdict"], d["waiting_on"]), ("blocked", "Alex"))  # the final JSON's verdict lands on the done record
        with mock.patch.dict(graph.JIRA, {"worked": {}}):
            self.assertEqual(graph.waiting(d), ("BLOCKED", "Alex", False))  # the verdict, not the words "PR open", decides
            self.assertIn("ZZS-1", [x["key"] for x in graph.finished_tiers()[0]])  # so the table lists it as awaiting, not done
        self.assertEqual(graph.tag({"outcome": "CI running, 3 pending.", "verdict": "done"}), "DONE")  # the verdict outranks the words
        self.assertEqual(graph.tag({"outcome": "Blocked? No.", "verdict": "waiting", "waiting_on": "CI checks"}), "CI PENDING")
        self.assertEqual(graph.tag({"outcome": "Pushed.", "verdict": None}), "AWAITING")  # recorded before verdicts: the words decide

    def test_fit_never_cuts_a_word(self):
        self.assertEqual(graph.fit("PR merged. CI green now. Jira updated.", 32), "PR merged. CI green now.")
        self.assertEqual(graph.outcome({"key": "AB-1", "outcome": "AB-1: AB-1: PR #9 open."}), "PR #9 open.")  # key not repeated
        self.assertEqual(graph.fit("short", 18), "short")
        self.assertEqual(graph.fit("one long sentence without any break at all", 10), "one long sentence without any break at all")  # wraps whole

    def test_idle_brain(self):
        bus.set_state("RoamBrain", "ok", "No new messages", last_tick=time.mktime(time.strptime("2026-10-29 09:05", "%Y-%m-%d %H:%M")))
        bus.set_state("RoamBrain", "ok", "No new messages; all meetings recapped.", last_tick=time.time() - 90)
        self.assertIn("Roam     idle 1m · No new messages", Text.from_markup(graph.brain_line("RoamBrain", 120)).plain)
        self.assertNotIn("recapped", Text.from_markup(graph.brain_line("RoamBrain", 120)).plain)
        self.assertNotIn("open log", Text.from_markup(graph.brain_line("RoamBrain", 120, expanded=True)).plain)  # the Logs button opens it  # short until expanded
        self.assertIn("all meetings recapped", Text.from_markup(graph.brain_line("RoamBrain", 120, expanded=True)).plain)
        long = Text.from_markup(graph.brain_line("RoamBrain", 40, expanded=True)).plain.split("\n")[1:]
        self.assertTrue(len(long) > 1 and all(l.startswith(" " * 13) and len(l) <= 40 for l in long))  # wraps indented
        bus.set_state("RoamBrain", "ok", "Checked every chat and channel for new messages since the last run without finding any", last_tick=time.time() - 90)
        short = Text.from_markup(graph.brain_line("RoamBrain", 40)).plain.split("\n")
        self.assertTrue(len(short) > 1 and all(l.startswith(" " * 13) and len(l) <= 40 for l in short[1:]))  # status wraps under itself
        self.assertIn("[@click=app.qui", Text.from_markup(graph.hang("x [@click=app.quit('a b')] y", 13, 30)).plain)  # no tag survives wrapping

    def test_held_brain_waits(self):
        bus.paused.set(); bus.held.add("RoamBrain")
        t = threading.Thread(target=bus.gate, args=("RoamBrain",), daemon=True); t.start()
        t.join(1.2); self.assertTrue(t.is_alive())
        bus.held.discard("RoamBrain"); t.join(2); self.assertFalse(t.is_alive())

    def test_text_colours_have_contrast(self):
        from devbot import theme

        def lum(h):
            c = [int(h[i:i + 2], 16) / 255 for i in (1, 3, 5)]
            c = [x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4 for x in c]
            return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]
        for name, p in theme.PALETTES.items():
            for role in ("WHITE", "DIM", "TEAL", "BLUE", "MINT", "VIOLET", "AMBER", "CORAL"):
                a, b = sorted((lum(p[role]), lum(p["PANEL"])))
                self.assertGreaterEqual((b + 0.05) / (a + 0.05), 4.5, f"{name} {role}")

    def test_theme_toggle(self):
        async def go():
            async with ui.Dashboard().run_test(size=(160, 64)) as pilot:
                start = ui.THEME
                await pilot.click("#themebtn"); await pilot.pause(0.3)
                other = "dark" if start == "light" else "light"
                self.assertEqual((ui.THEME, graph.THEME), (other, other))
                self.assertEqual(ui.TEAL, theme.PALETTES[other]["TEAL"])
                btn = pilot.app.query_one("#themebtn")  # yellow Light mode / purple Dark mode
                self.assertEqual((btn.has_class("-sun"), btn.has_class("-night")), (other == "dark", other == "light"))
                self.assertEqual(str(pilot.app.screen.styles.background.hex).upper(), theme.PALETTES[other]["BG"].upper())
                command(pilot.app, f"Switch to {start} mode"); await pilot.pause(0.3)  # the Commands entry switches back
                self.assertEqual(ui.THEME, start)
        try:
            asyncio.run(go())
        finally:
            theme.FILE.unlink(missing_ok=True)
            theme.apply(theme.saved(), ui, graph)

    def test_command_palette_lists_every_action(self):
        from textual.command import CommandPalette

        async def go():
            async with ui.Dashboard().run_test(size=(180, 50)) as pilot:
                titles = [c.title for c in pilot.app.get_system_commands(pilot.app.screen)]
                for want in ["Pause TeamsBrain", "Run TeamsBrain now", "Show JIRABrain log", "Show all logs", "Reload window", "Quit DevBot"]:
                    self.assertIn(want, titles)
                self.assertNotIn("Theme", titles)  # Textual's own theme switcher would fight ours
                cmd = next(c for c in pilot.app.get_system_commands(pilot.app.screen) if c.title == "Show JIRABrain log")
                cmd.callback(); await pilot.pause(0.1)
                self.assertEqual(pilot.app.query_one("#logs").active, "tab-JIRABrain")
                await pilot.press("escape"); await pilot.pause(0.1)
                self.assertFalse(pilot.app.query_one("#backdrop").display)  # Escape closes every popup
                await pilot.click("#cmdbtn"); await pilot.pause(0.3)
                self.assertIsInstance(pilot.app.screen, CommandPalette)  # the button opens the clickable list
                x = pilot.app.screen.query_one("#paletteclose")
                await pilot.click("#paletteclose", offset=(x.region.width - 1, 0)); await pilot.pause(0.3)  # its ✕ closes it
                self.assertNotIsInstance(pilot.app.screen, CommandPalette)
        asyncio.run(go())

    def test_cards_resize_by_dragging(self):
        seen = list(bus.chat); bus.chat.extend({"ts": 0, "who": "you", "kind": "say", "text": f"line {i}", "status": "ok"} for i in range(30))  # a full chat, as on a real machine
        self.addCleanup(lambda: (bus.chat.clear(), bus.chat.extend(seen)))
        async def go():
            async with ui.Dashboard().run_test(size=(160, 64)) as pilot:
                await pilot.pause(0.3)
                app = pilot.app
                y = lambda s: app.query_one(s).region.y
                self.assertLess(y("#msgbox"), y("#ticketbox")); self.assertLess(y("#ticketbox"), y("#brains")); self.assertLess(y("#brains"), y("#chatbox"))  # Messages, Tickets, Brains, CentralBrain
                await pilot.mouse_down("#split-rows")
                await pilot.hover("#msgbox", offset=(10, 10))
                await pilot.mouse_up("#split-rows")
                await pilot.pause(0.2)
                self.assertEqual(app.query_one("#ticketbox").outer_size.height, app.layout_sizes["rows"])
                self.assertGreater(app.layout_sizes["rows"], 0)
                many = {f"ZZT-{i}": ("Dev In Progress", "Fix the cache") for i in range(40)}
                patch = mock.patch.dict(graph.JIRA, {"assigned": many, "worked": {}}); patch.start(); self.addCleanup(patch.stop)  # enough tickets to fill a tall card
                app.refresh_all(); await pilot.pause(0.3)
                app.drag("rows", 0, 17, (20, app.pane_sizes("rows"))); await pilot.pause(0.2)
                cards = ("#msgbox", "#ticketbox", "#brains", "#chatbox")
                edges = lambda: [(app.query_one(c).region.y, app.query_one(c).region.bottom) for c in cards]
                for axis, i, ds in (("rows", 0, (-2, 2)), ("mid", 1, (2, -2)), ("chat", 2, (-2, 2))):  # each bar is the edge between cards i and i+1; Brains is already as tall as its rows
                    for d in ds:
                        before = edges()
                        app.drag(axis, 0, 20 + d, (20, app.pane_sizes(axis)))
                        await pilot.pause(0.2)
                        after = edges()
                        moved = [(j, k) for j in range(4) for k in range(2) if before[j][k] != after[j][k]]
                        self.assertEqual(moved, [(i, 1), (i + 1, 0)], (axis, d))  # only that one edge moves, and by d
                        self.assertEqual(after[i][1] - before[i][1], d)
                before = edges()
                app.drag("mid", 0, 10, (20, app.pane_sizes("mid"))); await pilot.pause(0.2)  # Brains already shows every row: dragging up changes nothing, Messages included
                self.assertEqual(edges(), before)
                app.drag("rows", 0, -99, (20, app.pane_sizes("rows")))  # dragged far up: Messages stops at its minimum, under the header
                await pilot.pause(0.2)
                self.assertLessEqual(app.query_one("#lower").outer_size.height, 6)
                self.assertGreater(y("#msgbox"), app.query_one("#top").region.bottom - 1)
                self.assertEqual(app.query_one("#chatbox").region.bottom, edges()[3][1])
                self.assertEqual(app.query_one("#top").styles.margin.bottom, 1)  # clear row under the header
                for c in ("#ticketbox", "#brains"):  # at most one blank row under the last line
                    box = app.query_one(c)
                    self.assertLessEqual(box.outer_size.height, max(4, max(w.virtual_region.bottom for w in box.children) + 1 + box.gutter.height))
                for w in app.query(".card, #say, #logbox"):  # every panel and input is a rounded rectangle
                    self.assertEqual(w.styles.border_top[0], "round")
                self.assertEqual(app.query_one("#split-rows").render().plain, "╺" + "━" * (app.query_one("#split-rows").size.width // 3) + "╸")  # a long rounded pill
                self.assertEqual((app.query_one("#split-rows").outer_size.height, app.query_one("#split-mid").outer_size.height), (1, 1))  # half a row above the bar, a row below  # the others, no side marks  # one grip size for every handle
                self.assertLess(app.query_one("#split-rows").content_size.width + 0, 200)
                app.drag("chat", 0, 30, (40, (20, 5)))  # handle above CentralBrain: dragging up grows it
                await pilot.pause(0.2)
                self.assertEqual(app.query_one("#chat").styles.height.value, 15)
                self.assertIn("rows", json.loads(ui.LAYOUT_FILE.read_text()))  # the mouse drag is kept for the next reload
            async with ui.Dashboard().run_test(size=(160, 64)) as pilot:
                await pilot.pause(0.3)
                self.assertIn("rows", pilot.app.layout_sizes)
                rows = pilot.app.layout_sizes["rows"]
                await pilot.resize_terminal(160, 32); await pilot.pause(0.3)  # half the height: the row keeps its share
                self.assertEqual(pilot.app.query_one("#ticketbox").styles.height.value, max(4, min(round(rows / 2), 32 - 17 - pilot.app.query_one("#brains").outer_size.height - 3)))  # clamped to leave room below
            ui.LAYOUT_FILE.unlink()
        asyncio.run(go())

    def test_ui_boots_and_keys_work(self):
        async def go():
            bus.paused.set()
            async with ui.Dashboard().run_test(size=(160, 64)) as pilot:
                await pilot.pause(0.4)
                self.assertFalse(pilot.app.query_one("#logbox").display)  # no log until one is opened from Brains
                theme_before = ui.THEME
                for key in ("t", "g", "p", "q", "slash", "6"):  # plain keys do nothing
                    await pilot.press(key)
                await pilot.pause(0.2)
                self.assertTrue(pilot.app.is_running)
                self.assertEqual((ui.THEME, pilot.app.picked), (theme_before, "CentralBrain"))
                self.assertTrue(bus.paused.is_set())
                self.assertEqual(pilot.app.screen, pilot.app.screen_stack[0])
                first = bus.active
                await pilot.press("ctrl+n"); self.assertNotEqual(bus.active, first)
                new = bus.active
                await pilot.press("left"); self.assertEqual(bus.active, list(bus.chats)[-2])  # empty chat box: arrows switch chats
                await pilot.press("right"); self.assertEqual(bus.active, new)
                await pilot.press("ctrl+d"); self.assertEqual(pilot.app.closing, new)
                await pilot.press("n"); self.assertIn(new, bus.chats)
                self.assertEqual((pilot.app.closing, pilot.app.query_one("#say").text), (None, ""))  # n answered, not typed
                await pilot.press("ctrl+d"); await pilot.press("y"); self.assertNotIn(new, bus.chats)
                pilot.app.action_select_chat(first)
                await pilot.press("ctrl+m"); self.assertNotEqual(ui.THEME, theme_before)
                await pilot.press("ctrl+m"); self.assertEqual(ui.THEME, theme_before)
                pilot.app.query_one("#say").text = "draft"
                await pilot.press("ctrl+r"); await pilot.pause(0.3)
                self.assertTrue(pilot.app.is_running)  # the UI rebuilt itself, nothing exited
                self.assertEqual((pilot.app.query_one("#say").text, pilot.app.query_one("#brains").border_title), ("draft", "Brains"))
                pilot.app.query_one("#say").text = ""
                await pilot.press("ctrl+p"); await pilot.pause(0.2)
                self.assertNotEqual(pilot.app.screen, pilot.app.screen_stack[0])
                await pilot.press("escape"); await pilot.pause(0.2)
                command(pilot.app, "Show TeamsBrain log")
                self.assertEqual(pilot.app.picked, "TeamsBrain")
                command(pilot.app, "Pause all engines"); self.assertFalse(bus.paused.is_set())
                command(pilot.app, "Resume all engines"); self.assertTrue(bus.paused.is_set())
                await pilot.pause(0.2)  # the logs unfolded and moved the chat box
                await pilot.click("#say"); self.assertFalse(pilot.app.query_one("#say").has_focus)  # the first click only closes the log popup
                await pilot.click("#say"); await pilot.press("q")  # typing goes into the chat box
                self.assertEqual(pilot.app.query_one("#say").text, "q")
                say = pilot.app.query_one("#say")
                say.insert(" long message" * 200); await pilot.pause(0.2)
                self.assertGreater(say.size.height, 1)   # grows with wrapped text...
                self.assertLessEqual(say.outer_size.height, 10)  # ...up to a cap, then scrolls
                self.assertGreater(say.max_scroll_y, 0)
                with mock.patch.object(brains, "ask") as ask:
                    await pilot.press("enter"); await pilot.pause(0.1)
                self.assertTrue(ask.call_args.args[0].startswith("q long message"))
                self.assertEqual(say.text, "")
                bus.say("You", "older message"); bus.say("CentralBrain", "a reply"); bus.say("You", "newest message")
                await pilot.press("up"); self.assertEqual(say.text, "newest message")
                await pilot.press("up"); self.assertEqual(say.text, "older message")
                await pilot.press("down"); self.assertEqual(say.text, "newest message")
                await pilot.press("down"); self.assertEqual(say.text, "")  # past the newest: back to empty
                say.insert("draft"); await pilot.press("up"); self.assertEqual(say.text, "newest message")  # top row: recalls
                await pilot.press("down"); self.assertEqual(say.text, "draft")  # ...and the draft comes back
                say.text = ""; say.insert("line1\nline2"); await pilot.press("up")
                self.assertEqual(say.text, "line1\nline2")  # not on the top row: up just moves the cursor
                say.text = ""
                say.insert("sent by click"); await pilot.pause(0.2)  # let the box shrink back so #send is where the click lands
                with mock.patch.object(brains, "ask") as ask:
                    await pilot.click("#send"); await pilot.pause(0.1)
                ask.assert_called_once_with("sent by click"); self.assertEqual(say.text, "")
                pilot.app.set_focus(None); command(pilot.app, "Show TeamsBrain log"); await pilot.pause(0.3)  # clicks above closed it
                self.assertTrue(pilot.app.query_one("#logbox").display)  # popup opened by Show TeamsBrain log
                r, (W, H) = pilot.app.query_one("#logbox").region, pilot.app.size
                self.assertLessEqual(abs(r.x - (W - r.right)), 1); self.assertLessEqual(abs(r.y - (H - r.bottom)), 1)  # centred
                x = pilot.app.query_one("#logclose").size.width - 1; await pilot.click("#logclose", offset=(x, 0)); await pilot.pause(0.1)
                self.assertFalse(pilot.app.query_one("#logbox").display)  # ✕ in the top right closes it
                bus.add_messages([{"id": "1", "source": "Teams", "from": "Alex", "text": "old question", "reply": "x"}], "TeamsBrain")
                bus.add_messages([{"id": "1", "from": "Alex", "chat": "Alpha chat", "text": "@Cole can we sync?", "reply": "Yes, 3pm",
                                   "thread": [{"from": "Ana", "text": "earlier context"}, {"from": "Alex", "text": "@Cole can we sync?"}]},
                                  {"id": "2", "from": "Riley", "text": "fyi @everyone"}], "RoamBrain")
                self.assertEqual([(m["id"], m["source"]) for m in bus.messages()], [("1", "Roam"), ("2", "Roam")])  # same id replaced
                saved = bus.MESSAGES_FILE.read_text()
                bus.add_messages([{"from": "Ana", "at": "2026-01-03T10:00:05Z", "text": "same  words"}], "TeamsBrain")
                bus.add_messages([{"id": "9", "from": "ana", "at": "2026-01-03T10:00:59Z", "text": "same words", "reply": "ok"}], "TeamsBrain")
                self.assertEqual([m.get("id") for m in bus.messages() if m["text"].startswith("same")], ["9"])  # an id-less copy is replaced too
                bus.MESSAGES_FILE.write_text(saved)
                saved = bus.MESSAGES_FILE.read_text()
                bus.add_messages([{"id": "4", "text": "new", "at": "2026-01-02"}], "TeamsBrain"); bus.add_messages([{"id": "3", "text": "old", "at": "2026-01-01"}], "TeamsBrain")
                self.assertEqual([m["id"] for m in bus.messages()[:2]], ["4", "3"]); bus.MESSAGES_FILE.write_text(saved)  # a backfill sorts by time, not arrival
                await pilot.pause(0.3)
                shown = pilot.app.query_one("#messages").render().plain
                self.assertIn("Roam · Alpha chat", shown); self.assertIn("@Cole can we sync?", shown); self.assertIn("Yes, 3pm", shown); self.assertIn("no reply needed", shown)
                await pilot.app.run_action("show_thread(0)"); await pilot.pause(0.2)  # what a click on it runs (the card can sit below a small test window)
                self.assertTrue(pilot.app.query_one("#threadbox").display)  # click a message: its whole thread pops up
                console = Console(width=100, record=True, file=io.StringIO())
                console.print(pilot.app.query_one("#thread").content)
                self.assertIn("earlier context", console.export_text())
                await pilot.click("#threadclose", offset=(0, 0)); await pilot.pause(0.1)
                self.assertFalse(pilot.app.query_one("#threadbox").display)
                await pilot.app.run_action("show_thread(0)"); await pilot.pause(0.2)
                pilot.app.query_one("#threadnote").text = "on it"
                with mock.patch.object(brains, "ask") as ask:
                    await pilot.click("#threadpost"); await pilot.pause(0.1)  # the popup's own chat bar: CentralBrain posts it in the thread
                self.assertTrue(ask.call_args.args[0].endswith("Reply:\non it")); self.assertIn("Alex", ask.call_args.args[0])
                self.assertEqual(pilot.app.query_one("#threadnote").text, "")
                with mock.patch.object(pilot.app, "action_toggle_theme") as flip:
                    await pilot.click("#themebtn"); await pilot.pause(0.1)  # a click off the popup closes it and nothing underneath runs
                flip.assert_not_called(); self.assertFalse(pilot.app.query_one("#threadbox").display)
                await pilot.app.run_action("hold('TeamsBrain')"); self.assertIn("TeamsBrain", bus.held)  # what the Pause button runs
                await pilot.app.run_action("hold('TeamsBrain')"); self.assertNotIn("TeamsBrain", bus.held)
                bus.log("TeamsBrain", "copy me please"); command(pilot.app, "Show TeamsBrain log"); await pilot.pause(0.4)
                for i in range(600):
                    bus.log("TeamsBrain", f"line {i}")
                await pilot.pause(0.4)
                log = pilot.app.query_one("#log-TeamsBrain")
                self.assertEqual(len(log.lines), ui.SelectLog.MAX)  # capped
                command(pilot.app, "Scroll the open log to the top"); await pilot.pause(0.1); self.assertEqual(log.scroll_y, 0)
                command(pilot.app, "Scroll the open log to the bottom"); await pilot.pause(0.1); self.assertEqual(log.scroll_y, log.max_scroll_y)
                with mock.patch("subprocess.run") as run:
                    command(pilot.app, "Copy the open log")
                self.assertIn("line 599", run.call_args.kwargs["input"])
        asyncio.run(go())


    def test_done_today_one_entry_per_ticket(self):
        runs = [{"key": "A-1", "at": 1, "outcome": "PR merged"}, {"key": "A-1", "at": 2, "outcome": "PR merged"},
                {"key": "B-2", "at": 3, "outcome": "PR merged"}]
        with mock.patch.object(bus, "done_today", return_value=runs), mock.patch.dict(graph.JIRA, {"worked": {}}):
            self.assertEqual([d["key"] for d in graph.finished_tiers()[1]], ["B-2", "A-1"])
        runs = [{"key": "C-3", "at": 1, "outcome": "Awaiting review."}, {"key": "D-4", "at": 2, "outcome": "PR merged"}]
        with mock.patch.object(bus, "done_today", return_value=runs), mock.patch.dict(graph.JIRA, {"worked": {"C-3": ("Done", "t"), "D-4": ("In Progress", "t")}}):
            self.assertEqual([d["key"] for d in graph.finished_tiers()[1]], ["C-3"])  # Jira Done counts; reassigned drops


if __name__ == "__main__":
    unittest.main()


class TicketRunning(unittest.TestCase):
    def test_any_engine_run_on_a_ticket_marks_it_running(self):
        from devbot import claude
        with mock.patch.dict(claude.RUNNING, {"DEMO-1": {object()}, "TeamsBrain": {object()}, None: {object()}, "DEMO-2": set()}, clear=True), \
                mock.patch.dict(bus.working, {}, clear=True):
            self.assertEqual(graph.order()[0], ["DEMO-1"])


class PopupShade(unittest.TestCase):
    def test_every_popup_shares_one_opaque_shade(self):
        shade = ui.Dashboard().get_css_variables()["c-shade"]
        self.assertTrue(shade.startswith("#") and len(shade) == 7)  # opaque: layers can't see through a translucent one
        self.assertEqual(ui.Dashboard.CSS.count("$c-shade"), 2)  # the widget backdrop and the command palette


class SpendAndAttribution(unittest.TestCase):
    def test_spend_totals(self):
        from devbot import bus
        before = bus.spend()
        orig = bus.SPEND_FILE
        bus.SPEND_FILE = pathlib.Path(tempfile.mkdtemp()) / "spend.json"
        try:
            bus.ticked("GitHubBrain", {"cost_usd": 0.5})
            after = bus.spend()
            self.assertAlmostEqual(after[0] - before[0], 0.5)
            self.assertAlmostEqual(after[1] - before[1], 0.5)
            self.assertAlmostEqual(json.loads(bus.SPEND_FILE.read_text())["total_usd"], after[2])
            saved = json.loads(bus.SPEND_FILE.read_text())
            self.assertAlmostEqual(saved["day_usd"], after[0])  # today's spend survives a restart
        finally:
            bus.SPEND_FILE = orig

    def test_no_claude_attribution(self):
        from devbot import claude
        self.assertIn("Co-Authored-By", claude.FRAME)  # named only to forbid it
        self.assertIn("--settings", claude.base_cmd(False))
        self.assertIn("mcp__plugin_playwright_playwright", claude.base_cmd(False))  # no pop-up browser windows
        gh = claude.base_cmd(brain="GitHubBrain")  # a brain loads only its own connectors
        self.assertIn("mcp__claude_ai_Roam", gh)
        self.assertNotIn("mcp__claude_ai_Atlassian_MCP", gh)
        self.assertNotIn("mcp__claude_ai_Atlassian", claude.base_cmd(brain="RoamBrain"))
        jira = claude.base_cmd(brain="JIRABrain")  # the retired v1 connectors are unloaded, the v2 one kept
        self.assertIn("mcp__claude_ai_Atlassian", jira); self.assertNotIn("mcp__claude_ai_Atlassian_MCP", " ".join(jira))
        self.assertIn("Atlassian_MCP", claude.FRAME)
        self.assertNotIn("mcp__claude_ai_Roam", claude.base_cmd(brain="CodeBrain"))
        self.assertFalse(json.loads(claude.NO_ATTRIBUTION)["includeCoAuthoredBy"])
        self.assertIn("worktree remove", brains.DUTY["GitHubBrain"]); self.assertIn(bus.CONFIG["repos_dir"], brains.DUTY["GitHubBrain"])
        self.assertFalse(json.loads(claude.NO_ATTRIBUTION)["enabledPlugins"]["playwright@claude-plugins-official"])


class CIIteration(unittest.TestCase):
    def test_failing_ci_is_handed_back_and_iterated(self):
        self.assertIn("no attempt limit", open(brains.__file__).read())
        self.assertIn("no merge conflicts", brains.DUTY["GitHubBrain"])  # conflicts are part of "passing"
        self.assertIn("merge conflicts with <base>", brains.DUTY["GitHubBrain"])  # and get handed off to be resolved
        self.assertIn("only after the transition succeeded (or Jira already shows it done)", brains.DUTY["GitHubBrain"])  # a failed DONE is retried
        self.assertNotIn("merged (by you or anyone) since last run", brains.DUTY["GitHubBrain"])
        self.assertIn("to DONE (never DEPLOYED)", brains.DUTY["GitHubBrain"])  # merged tickets close as DONE
        self.assertIn("never skip this because the key is in memory[\"in_review\"]", brains.DUTY["GitHubBrain"])  # green PR stuck IN QA gets moved
        self.assertIn("gh pr merge", brains.DUTY["GitHubBrain"])  # approved + green PRs get merged, then Jira goes to DONE
        self.assertIn("resolve them", open(brains.__file__).read())  # CodeBrain resolves them
        self.assertIn("CI is failing", brains.DUTY["GitHubBrain"])
        self.assertIn("all CI checks pass", brains.DUTY["TeamsBrain"])
        self.assertIn("review_ask_posted", brains.DUTY["JIRABrain"])
        self.assertIn("EVERY ticket of mine IN QA", brains.DUTY["JIRABrain"])
        self.assertIn("teams.cloud.microsoft/_#/l/chat/", pathlib.Path("devbot/claude.py").read_text())  # the plain /l/chat/ link opens a launcher, not the chat
        self.assertIn("needs review to be merged", brains.DUTY["TeamsBrain"])  # TeamsBrain announces IN CODE REVIEW tickets  # IN QA tickets loop back to CodeBrain until CI passes  # daily ask to review IN CODE REVIEW tickets in both chats


class Actions(unittest.TestCase):
    def test_side_effects_are_counted_live(self):
        n = bus.status["TeamsBrain"]["actions"]
        calls = [{"type": "tool_use", "name": "mcp__claude_ai_Microsoft_365__outlook_send_mail", "input": {}},
                 {"type": "tool_use", "name": "mcp__claude_ai_Microsoft_365__outlook_email_search", "input": {}},
                 {"type": "tool_use", "name": "Bash", "input": {"command": "git commit -m x && git push"}},
                 {"type": "tool_use", "name": "Bash", "input": {"command": "git status"}},
                 {"type": "tool_use", "name": "mcp__playwright__browser_press_key", "input": {"key": "Enter"}},
                 {"type": "tool_use", "name": "mcp__playwright__browser_click", "input": {"element": "Chat list"}}]
        claude.report_tools("TeamsBrain", {"message": {"content": calls}})
        self.assertEqual(bus.status["TeamsBrain"]["actions"] - n, 3)


class PRComments(unittest.TestCase):
    def test_github_brain_handles_human_pr_comments(self):
        d = brains.DUTY["GitHubBrain"]
        self.assertIn("Skip bots", d)
        self.assertIn("handled_comments", d)
        self.assertIn("pulls/{n}/comments/{id}/replies", d)


class Meetings(unittest.TestCase):
    def setUp(self):
        from devbot import meetings
        self.m = meetings
        self.orig = meetings.FILE
        meetings.FILE = pathlib.Path(tempfile.mkdtemp()) / "meetings.json"

    def tearDown(self):
        self.m.FILE = self.orig

    def test_new_invite_replaces_a_room_still_marked_joined(self):
        now = time.time()
        stale = {"url": "https://ro.am/join/x", "title": "Old room", "start": now - 600, "end": now + 1200, "status": "joined"}
        self.m._save([stale])
        new = {"url": "roamhq://invite", "title": "Roam invitation", "start": now, "end": now + 1800, "status": "joined"}
        with mock.patch.object(self.m, "accept_invite", return_value=new):
            self.assertIs(self.m.invite([stale], now), new)
        saved = {m["title"]: m["status"] for m in self.m._load()}
        self.assertEqual(saved, {"Old room": "left", "Roam invitation": "joined"})
        with mock.patch.object(self.m, "accept_invite", return_value=None):
            self.assertIsNone(self.m.invite([new], now))  # nothing showing: the current room stays

    def test_accept_invite(self):
        from unittest import mock
        out = lambda text: mock.patch.object(self.m.subprocess, "run", return_value=mock.Mock(stdout=text))
        with out(""):
            self.assertIsNone(self.m.accept_invite(time.time()))  # nothing on screen
        with out("Riley invited you to team Standup Room\n"), mock.patch.object(self.m, "ready", return_value=False):
            m = self.m.accept_invite(1000.0)
        self.assertEqual((m["status"], m["title"], self.m.app(m["url"])), ("joined", "Roam invitation", "Roam"))
        self.assertEqual(self.m.due(m, 1000.0 + 600, None), None)            # stays while the room is live
        self.assertEqual(self.m.due({**m, "bye": True}, 1100.0, 0), "leave")  # leaves when people say goodbye
        self.assertIn("Riley invited you to team Standup Room", self.m.SEEN)  # clicked once: its stale card is skipped next time
        with out("noperm"):
            self.assertIsNone(self.m.accept_invite(time.time()))  # no Screen Recording permission
        with out(""):
            self.m.accept_invite(time.time())
        self.assertEqual(self.m.SEEN, set())  # card gone: the same invite text counts again
        here = {"url": "https://ro.am/join/x", "title": "Old room", "start": 1, "end": 9, "status": "joined"}
        self.m._save([here])
        with out("admit:Jane knocked\n"):
            self.assertIsNone(self.m.accept_invite(1000.0))  # a knock is not a new meeting
        self.assertEqual(self.m._load()[0]["status"], "joined")
        self.assertIn("Jane knocked", self.m.SEEN)

    def test_blank_scan_keeps_a_just_accepted_invite(self):
        self.m.SEEN.add("Riley invited you")
        self.m._accepted_at = 1000.0
        with mock.patch.object(self.m.subprocess, "run", return_value=mock.Mock(stdout="")):
            self.assertIsNone(self.m.accept_invite(1030.0))
        self.assertIn("Riley invited you", self.m.SEEN)  # still on screen: do not click Accept again
        with mock.patch.object(self.m.subprocess, "run", return_value=mock.Mock(stdout="")):
            self.m.accept_invite(1100.0)
        self.assertEqual(self.m.SEEN, set())
        self.m._accepted_at = 0

    def test_leave_keeps_audio_while_another_meeting_listens(self):
        saved = dict(self.m.EARS)
        self.m.EARS.clear()
        self.m.EARS["other"] = mock.Mock()
        m = {"start": 1, "url": "https://ro.am/join/x", "title": "A", "audio": ["Speakers", "Mic"], "status": "joined"}
        try:
            with mock.patch.object(self.m, "_route") as route, mock.patch.object(self.m, "_click", return_value=True):
                self.m._act(m, "leave")
            route.assert_not_called()
            self.m.EARS.clear()
            with mock.patch.object(self.m, "_route") as route, mock.patch.object(self.m, "_click", return_value=True):
                self.m._act(m, "leave")
            route.assert_called_once_with("Speakers", "Mic")
        finally:
            self.m.EARS.clear()
            self.m.EARS.update(saved)

    def test_joined_plays_a_copy_on_the_speakers(self):
        cmd = self.m._monitor_cmd(self.m.HEAR, "3")
        self.assertEqual((cmd[cmd.index("-i") + 1], cmd[cmd.index("-audio_device_index") + 1]), (f":{self.m.HEAR}", "3"))
        listing = "[0] BlackHole 16ch, BlackHole16ch_UID\n[3]           MacBook Pro Speakers, BuiltInSpeakerDevice\n"
        with mock.patch.object(self.m.subprocess, "run", return_value=mock.Mock(stderr=listing, stdout="BlackHole 2ch\nLG Monitor\n")):
            self.assertEqual(self.m._device("MacBook Pro Speakers"), "3")
            self.assertIsNone(self.m._device("Speakers"))
            with mock.patch.object(self.m, "_current", return_value="BlackHole 2ch"):
                self.assertEqual(self.m._real("output"), "LG Monitor")  # lid shut: first real device, never BlackHole
        m = {"start": 5, "url": "https://ro.am/join/x", "title": "A", "status": "scheduled"}
        ear = mock.Mock()
        saved = dict(self.m.EARS)
        try:
            with mock.patch.object(self.m, "ready", return_value=True), \
                 mock.patch.object(self.m, "_current", side_effect=["Speakers", "Mic"]), \
                 mock.patch.object(self.m, "_route") as route, \
                 mock.patch.object(self.m, "_monitor") as mon, \
                 mock.patch.object(self.m, "Ear", return_value=ear):
                self.m._joined(m, "Roam")
            route.assert_called_once_with(self.m.HEAR, self.m.SPEAK)
            mon.assert_called_once_with(["Speakers", "Mic"])
            self.assertEqual(m["audio"], ["Speakers", "Mic"])
            fake = mock.Mock()
            self.m.MONITOR, self.m.EARS = [fake], {}
            with mock.patch.object(self.m, "_route"), mock.patch.object(self.m, "_click", return_value=True):
                self.m._act({**m, "status": "joined"}, "leave")
            fake.terminate.assert_called_once()
            self.assertEqual(self.m.MONITOR, [])
        finally:
            self.m.EARS.clear()
            self.m.EARS.update(saved)
            self.m.MONITOR = []

    def test_add_validates_and_dedups(self):
        now = time.time()
        soon = datetime.datetime.fromtimestamp(now + 600).astimezone().isoformat()
        items = [{"url": "https://ro.am/r/abc", "title": "Stand Up", "start": soon},
                 {"url": "https://ro.am/r/abc", "start": soon},           # duplicate
                 {"url": "https://evil.example/x", "start": soon},        # not Roam
                 {"url": "roamhq://room/1", "end": now - 5},              # already over
                 {"url": "https://teams.microsoft.com/l/meetup-join/19%3ameeting_x%40thread.v2/0", "start": soon}]
        self.assertEqual(self.m.add(items, "TeamsBrain"), 2)
        self.assertEqual([self.m.app(x["url"]) for x in json.loads(self.m.FILE.read_text())], ["Roam", "Roam"])
        self.assertIn("--tools", claude.SEALED)  # the spoken-reply model never gets tools
        m = json.loads(self.m.FILE.read_text())[0]
        self.assertAlmostEqual(m["start"], now + 600, delta=2)
        self.assertAlmostEqual(m["end"] - m["start"], self.m.IMPROMPTU, delta=1)

    def test_location_decides_roam_or_teams(self):
        r, t = "https://ro.am/join/abc", "https://teams.microsoft.com/meet/123"
        self.assertEqual(self.m.choose({"location": "Roam: Team room", "roam_url": r, "teams_url": t}), r)
        self.assertEqual(self.m.choose({"location": "Microsoft Teams Meeting", "roam_url": r, "teams_url": t}), r)
        self.assertEqual(self.m.choose({"location": r, "teams_url": t}), r)          # link in the location
        self.assertEqual(self.m.choose({"location": "", "roam_url": r, "teams_url": t}), r)  # Roam by default
        self.assertEqual(self.m.choose({"teams_url": t}), self.m.ROOM)  # Teams-only invite: the Roam room
        self.assertIsNone(self.m.choose({"location": "Conference room B"}))
        soon = datetime.datetime.fromtimestamp(time.time() + 900).astimezone().isoformat()
        # the same event reported twice (once per link) is one meeting, settled by its location
        self.m.add([{"title": "GD Daily Sync", "start": soon, "teams_url": t}], "OutlookBrain")
        self.m.add([{"title": "GD Daily Sync", "start": soon, "location": "Roam", "roam_url": r, "teams_url": t}], "OutlookBrain")
        ms = json.loads(self.m.FILE.read_text())
        self.assertEqual([(m["url"], m["location"]) for m in ms], [(r, "Roam")])
        self.m.add([{"title": "GD Daily Sync", "start": soon, "location": "Teams", "teams_url": "https://teams.microsoft.com/meet/999"}], "TeamsBrain")
        self.assertEqual(json.loads(self.m.FILE.read_text())[0]["url"], r)  # a different source cannot redirect it

    def test_join_and_leave_timing(self):
        due, now = self.m.due, 10_000.0
        m = {"status": "scheduled", "start": now + 30, "end": now + 1800}
        self.assertEqual(due(m, now, None), "join")                     # a minute early
        self.assertIsNone(due({**m, "start": now + 300}, now, None))
        self.assertEqual(due({**m, "end": now - 1}, now, None), "missed")
        j = {**m, "status": "joined", "start": now - 600, "end": now + 600}
        self.assertIsNone(due(j, now, 5))                               # mid-meeting
        self.assertEqual(due({**j, "bye": True}, now, 5), "leave")      # goodbyes
        self.assertIsNone(due({**j, "end": now - 200}, now, 3))         # overrun but still talking
        self.assertEqual(due({**j, "end": now - 200}, now, 40), "leave")
        self.assertEqual(due(j, now, self.m.HUSH), "leave")             # everyone went quiet
        self.assertIsNone(due(j, now, None))                            # not listening: wait for the end

    def test_voice_and_cues(self):
        listing = "Fred                en_US    # Hi\nReed (English (US)) en_US    # Hi\nAva (Premium)       en_US    # Hi\nAmélie              fr_CA    # Salut\n"
        self.assertEqual(self.m.voice(listing), "Ava (Premium)")
        self.assertEqual(self.m.voice(listing.replace("Ava (Premium)", "Zed")), "Reed (English (US))")
        self.assertTrue(self.m.NAMED.search("hey cole, any update?"))
        self.assertFalse(self.m.NAMED.search("the coleslaw was great"))
        self.assertTrue(self.m.NAMED.search("Kohl, can you take that?"))  # common mishearing
        from unittest import mock
        ear = self.m.Ear.__new__(self.m.Ear)
        ear.lines, ear.asked = [f"{self.m.OWNER} (me): I pushed the fix.", "ok"], 0.0
        snap = {b: {"note": "PR #2485 for DEMO-3298 is pending"} for b in ("JIRABrain", "CodeBrain", "GitHubBrain")}
        with mock.patch.object(self.m.bus, "snapshot", return_value=(snap, None)):
            self.assertIn("after", ear.cue("Did you run the tests?"))      # follow-up question to what I just said
            ear.lines = ["a", "b", "c"]
            self.assertIn("3298", ear.cue("Where are we on 3298?"))        # my ticket came up
            self.assertIsNone(ear.cue("Let's go for lunch."))              # nothing for me
            ear.asked = time.time()
            self.assertIsNone(ear.cue("Status on 2485?"))                   # rate limit on context cues
            self.assertIn("named", ear.cue("Cole, status on 2485?"))       # the name always counts
        self.assertTrue(self.m.BYE.search("ok thanks everyone"))


class BackToBack(unittest.TestCase):
    def test_joining_leaves_the_previous_meeting_first(self):
        from devbot import meetings
        now = time.time()
        first = {"url": "https://ro.am/join/a", "title": "A", "start": now - 1700, "end": now + 100, "status": "joined", "source": "t"}
        second = {"url": "https://ro.am/join/b", "title": "B", "start": now + 30, "end": now + 1800, "status": "scheduled", "source": "t"}
        orig, meetings.FILE = meetings.FILE, pathlib.Path(tempfile.mkdtemp()) / "m.json"
        acts = []
        try:
            meetings._save([first, second])
            with mock.patch.object(meetings, "_act", lambda m, what: acts.append((m["title"], what)) or m.update(status={"join": "joined", "leave": "left"}[what])), \
                 mock.patch.object(meetings.time, "sleep", side_effect=StopIteration):
                with self.assertRaises(StopIteration):
                    meetings.watch()
        finally:
            meetings.FILE = orig
        self.assertEqual(acts, [("A", "leave"), ("B", "join")])


class Routing(unittest.TestCase):
    def test_sweep_escalates_in_the_same_session(self):
        calls = []
        def fake(brain, duty, **kw):
            calls.append(kw)
            first = len(calls) == 1
            return {"summary": "s", "actions": 1, "handoff": [], "meetings": [], "cost_usd": 0.1 if first else 0.4,
                    "error": None, "escalate": first}
        with mock.patch.object(claude, "run", fake):
            res = brains.sweep("TeamsBrain")
        self.assertEqual(calls[0]["model"], claude.SWEEP)
        self.assertTrue(calls[1]["resume"])
        self.assertEqual(calls[1]["session"], calls[0]["session"])
        self.assertEqual(calls[1]["model"], claude.DEEP)  # resuming keeps the old model unless told otherwise
        self.assertAlmostEqual(res["cost_usd"], 0.5)
        self.assertEqual(res["actions"], 2)
        calls.clear()
        with mock.patch.object(claude, "run", lambda b, d, **kw: calls.append(kw) or {"summary": "", "actions": 0, "handoff": [], "error": None, "escalate": False}):
            brains.sweep("RoamBrain")
        self.assertEqual(len(calls), 1)  # quiet sweeps never touch the stronger model
        saved = []
        with mock.patch.object(claude, "tier", return_value="login"), \
             mock.patch.object(claude, "run", lambda b, d, **kw: saved.append(kw) or {"messages": [{"text": "x"}]}), \
             mock.patch.object(bus, "add_messages", lambda m, b: saved.append(m)), \
             mock.patch.object(bus, "gate"), mock.patch.object(time, "sleep", side_effect=StopIteration), \
             mock.patch.object(triggers, "m365", side_effect=RuntimeError("signed out")), \
             mock.patch.object(triggers, "roam_chats", return_value=[{"text": "roam"}]):
            with self.assertRaises(StopIteration):
                brains.sync("RoamBrain")  # read in code; the Roam key never reaches a model
            with self.assertRaises(StopIteration):
                brains.sync("TeamsBrain")  # Teams web can't serve it: the model is the fallback
        self.assertEqual(saved, [[{"text": "roam"}], {"model": "sonnet", "keep_memory": False, "deny": claude.SYNC_DENY, "allow": claude.SYNC_ALLOW}, [{"text": "x"}]])
        self.assertIn("mcp__claude_ai_Roam__chat_post", claude.base_cmd(brain="RoamBrain", deny=claude.READ_ONLY))
        waits = []
        with mock.patch.object(bus.wake["GitHubBrain"], "wait", waits.append), mock.patch.object(bus, "gate"), \
             mock.patch.object(triggers, "live", {"GitHubBrain"}):
            brains.nap("GitHubBrain", quiet=3)
        self.assertEqual(waits, [brains.MAX_IDLE])  # a working trigger means no timer: only new activity wakes it

    def test_mid_run_activity_is_not_dropped(self):
        bus.wake["TeamsBrain"].clear()
        last, broken = {"TeamsBrain": "1"}, set()
        bus.status["TeamsBrain"]["state"] = "running"
        with mock.patch.object(triggers, "CHECKS", {"TeamsBrain": lambda: "2"}):
            triggers.poll(last, broken)
        self.assertTrue(bus.wake["TeamsBrain"].is_set())
        bus.wake["TeamsBrain"].clear()
        bus.status["TeamsBrain"]["state"] = "idle"


class UsageBars(unittest.TestCase):
    def test_usage_bars(self):
        from devbot import ui
        week = {"utilization": 100.0, "resets_at": "2026-11-02T16:00:00+00:00"}
        full = Text.from_markup(ui.usage_bars({"seven_day": week}, "login")).plain
        self.assertIn("100% week", full); self.assertIn("resets Mon", full); self.assertNotIn("Anthropic", full); self.assertNotIn("Cursor", full)
        self.assertEqual(Text.from_markup(ui.usage_bars({"seven_day": week}, "api")).plain, "Anthropic ● active")
        self.assertEqual(Text.from_markup(ui.usage_bars(None, "cursor")).plain, "Cursor ● active")
        extra = {"is_enabled": True, "used_credits": 33640.0, "monthly_limit": 60000, "utilization": 56.1}
        self.assertIn("$336 of $600 extra usage", Text.from_markup(ui.usage_bars({"seven_day": week, "extra_usage": extra}, "login")).plain)
        self.assertIn("Claude ?", Text.from_markup(ui.usage_bars(None, "login")).plain)  # no reading yet
        self.assertNotIn("resets", Text.from_markup(ui.usage_bars({"seven_day": {"utilization": 40.0, "resets_at": "x"}}, "login")).plain)
        self.assertEqual(len(Text.from_markup(ui.meter(37)).plain), 16)  # the bar never changes width
        self.assertIn("╸", Text.from_markup(ui.meter(50 + 100 / 32)).plain)  # half-cell precision



class ThreadMarkdown(unittest.TestCase):
    def test_chat_thread_renders_markdown(self):
        console = Console(width=80, record=True, file=io.StringIO())
        console.print(graph.thread({"from": "Alex", "at": "2026-10-30T11:00", "text": "**merge** #2269\n- one\n\n`code`"}))
        plain = console.export_text()
        self.assertIn("Alex", plain); self.assertIn("merge #2269", plain); self.assertIn("• one", plain)
        self.assertNotIn("**", plain); self.assertNotIn("`", plain)

    def test_one_chat_one_transcript(self):
        """The same Teams message saved by two engines, one of them labelled Jira, is one row and one transcript.
        A table Teams split into a cell per line renders as a row."""
        saved = bus.MESSAGES_FILE.read_text() if bus.MESSAGES_FILE.exists() else "[]"
        ask = "Roberts, Cole what am I missing on my end review wise?"
        try:
            bus.MESSAGES_FILE.write_text("[]")
            bus.add_messages([
                {"id": "1", "from": "Alex", "chat": "Project Alpha", "at": "2026-10-30T21:28:11Z", "text": ask, "reply": "",
                 "thread": [{"from": "Taylor", "at": "2026-10-30T21:11:20Z", "text": "pic", "images": ["a.jpg"]},
                            {"from": "Alex", "at": "2026-10-30T21:28:11Z", "text": ask}]},
                {"id": "1", "from": "Alex", "chat": "Project Alpha", "at": "2026-10-30T21:28:11Z", "text": ask, "reply": "The five PRs",
                 "thread": [{"from": "Alex", "at": "2026-10-30T21:28:11Z", "text": ask},
                            {"from": "Cole", "at": "2026-10-30T21:36:08Z", "text": "The five PRs"}]},
            ], "TeamsBrain")
            bus.add_messages([
                {"id": "9", "from": "Alex", "chat": "Project Alpha", "at": "2026-10-30T21:28:12Z", "text": ask, "reply": "short",
                 "thread": [{"from": "Cole", "at": "2026-10-30T19:06:00Z", "text": "deployed"}]},
            ], "JIRABrain")
            bus.add_messages([
                {"id": "2", "from": "Taylor", "chat": "Project Alpha", "at": "2026-10-30T19:42:00Z", "text": "Team @Everyone hello",
                 "thread": [{"from": "Remya", "at": "2026-10-20T10:00:00Z", "text": "Field |\nType |\n\nsource_doc |\nmanual filename |"},
                            {"from": "Taylor", "at": "2026-10-30T19:42:00Z", "text": "Team @Everyone hello"}]},
            ], "TeamsBrain")
            copies = [m for m in bus.messages() if m["text"] == ask]
            self.assertEqual(len(copies), 1)
            self.assertEqual(copies[0]["source"], "Teams")
            self.assertEqual(copies[0]["reply"], "The five PRs")
            self.assertEqual({t["text"] for t in copies[0]["thread"]}, {"The five PRs", "deployed", "pic", ask})
            bus.add_messages([{"id": "3", "from": bus.CONFIG["owner"], "chat": "Project Alpha", "at": "2026-10-30T21:36:08Z",
                               "text": "The five PRs"}], "TeamsBrain")
            shown = Text.from_markup(graph.messages(100)).plain
            self.assertEqual(shown.count(ask), 1)
            self.assertEqual(shown.count("The five PRs"), 1)  # shown as the reply, not again as a sent message
            self.assertNotIn("Jira ·", shown)
            console = Console(width=80, record=True, file=io.StringIO())
            console.print(graph.thread(copies[0]))
            plain = console.export_text()
            self.assertIn("pic", plain); self.assertIn("deployed", plain); self.assertIn("The five PRs", plain)
            self.assertEqual(plain.count(ask), 1)
            self.assertTrue(any("source_doc" in l and "manual filename" in l for l in plain.splitlines()))
        finally:
            bus.MESSAGES_FILE.write_text(saved)


class Tracker(unittest.TestCase):
    def test_adf_and_jira_side(self):
        doc = {"type": "doc", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "Hi "},
               {"type": "mention", "attrs": {"text": "@Jordan"}}]}, {"type": "bulletList", "content": [
               {"type": "listItem", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "one"}]}]}]}]}
        self.assertEqual(tracker.adf(doc), "Hi @Jordan\n• one\n")
        t = {"key": "TK-1", "summary": "Do [it]", "status": "In Review", "type": "Story", "priority": "P2", "assignee": "A",
             "reporter": "B", "updated": "2026-10-30T10:00", "description": "", "comments": [{"by": "Jordan", "at": "", "text": "ok"}]}
        console = Console(width=100, record=True, file=io.StringIO())
        console.print(tracker.left(t))
        plain = console.export_text()
        self.assertIn("Do [it]", plain); self.assertIn("In Review", plain); self.assertIn("Comments (1)", plain)

    def test_photos_draw_as_half_blocks_and_failures_say_so(self):
        from PIL import Image
        buf = io.BytesIO(); Image.new("RGB", (4, 4), (255, 0, 0)).save(buf, "PNG")
        console = Console(width=4, height=6, record=True, file=io.StringIO(), color_system="truecolor")
        console.print(tracker.picture(buf.getvalue()))
        self.assertEqual(console.export_text(clear=False), "▀▀▀▀\n▀▀▀▀\n")  # 4x4 pixels: two pixels per cell, so 2 rows
        self.assertIn("#ff0000", console.export_html(inline_styles=True).lower())
        wide = Console(width=2, height=6, record=True, file=io.StringIO()); wide.print(tracker.picture(buf.getvalue()))
        self.assertEqual(wide.export_text(), "▀▀\n")  # it redraws to fit whatever width it gets
        class Real:  # stands in for textual_image's Kitty-protocol renderable
            def __init__(self, img, width, height): self.width = width
            def __rich_console__(self, console, options): yield Segment("▓" * self.width); yield Segment.line()
        with mock.patch.object(tracker, "KITTY", Real):
            real = Console(width=3, height=6, record=True, file=io.StringIO()); real.print(tracker.picture(buf.getvalue()))
        self.assertEqual(real.export_text(), "▓▓▓\n")  # Ghostty or Kitty: the real image, sized to the pane
        self.assertIn("couldn't show", tracker.photo("http://insecure/x.png").plain)  # never fetched over plain http
        seen = []
        def opened(req, timeout):
            seen.append(req.headers.get("Authorization")); raise OSError("offline")
        with mock.patch.object(tracker._OPENER, "open", side_effect=opened), \
                mock.patch.object(tracker.subprocess, "run", return_value=mock.Mock(stdout="ghp_secret")):
            tracker.photo("https://evil.example/github/x.png"); tracker.photo("https://github.com.evil.example/x.png")
            tracker.photo("https://github.com/user-attachments/assets/x")
        self.assertEqual(seen, [None, None, "token ghp_secret"])  # the token only ever goes to GitHub itself
        with tempfile.TemporaryDirectory() as d, mock.patch.object(graph, "MEDIA", pathlib.Path(d)):
            name = tracker.keep(buf.getvalue(), "https://x/evil.command")
            self.assertTrue(name.endswith(".png") and (pathlib.Path(d) / name).read_bytes().startswith(b"\x89PNG"))  # never .command
            (pathlib.Path(d) / "run.command").write_text("echo hi")
            with mock.patch.object(ui.subprocess, "run") as opened:
                ui.Dashboard.action_open_photo(None, "run.command"); opened.assert_not_called()
                ui.Dashboard.action_open_photo(None, name)
            self.assertEqual(opened.call_args[0][0][:4], ["open", "-a", "Preview", "--"])
        buf = io.BytesIO(); Image.new("RGB", (2, 2), (0, 0, 255)).save(buf, "PNG")
        with mock.patch.object(tracker.subprocess, "run", return_value=mock.Mock(returncode=0, stdout=buf.getvalue())) as gh, \
                mock.patch.object(tracker, "keep", return_value="pr-test.png"):
            drawn = tracker.photo("https://github.com/o/r/blob/abc123/docs/a b.png?raw=true")
        self.assertIn("repos/o/r/contents/docs/a b.png?ref=abc123", gh.call_args[0][0])  # private repo files come through the API
        console = Console(width=40, record=True, file=io.StringIO()); console.print(drawn)
        text = console.export_text(); self.assertIn("▀▀", text); self.assertIn("Open full size", text)
        req = urllib.request.Request("https://github.com/a", headers={"Authorization": "token t"})
        moved = tracker._KeepCredsHome().redirect_request(req, None, 302, "", {}, "https://s3.amazonaws.com/b")
        self.assertIsNone(moved.get_header("Authorization"))  # and is dropped when GitHub redirects elsewhere
        self.assertEqual([a or b for a, b in tracker.IMG.findall('![s](https://x/a.png) <img width="9" src="https://x/b.jpg">')],
                         ["https://x/a.png", "https://x/b.jpg"])

    def test_empty_reviews_are_timeline_lines_not_empty_boxes(self):
        from rich.console import Console
        p = {"number": 1, "title": "t", "state": "OPEN", "headRefName": "b", "baseRefName": "main", "body": "desc",
             "author": {"login": "me"}, "commits": [], "comments": [], "statusCheckRollup": [],
             "reviews": [{"author": {"login": "alex"}, "state": "COMMENTED", "body": "", "submittedAt": ""}]}
        c = Console(file=io.StringIO(), width=100)
        for x in tracker.conversation(p, {}):
            c.print(x)
        out = c.file.getvalue()
        self.assertNotIn("No description provided", out)
        self.assertIn("alex reviewed", out)

    def test_pr_chain_stops_at_the_default_branch(self):
        pr = lambda n, base: {"number": n, "title": f"t{n}", "state": "OPEN", "headRefName": f"b{n}", "baseRefName": base,
                              "commits": [{"oid": "abcdef12", "messageHeadline": "c"}], "comments": [], "reviews": [],
                              "statusCheckRollup": [{"name": "unit", "conclusion": "SUCCESS"}, {"name": "lint", "conclusion": "FAILURE"},
                                                    {"name": "e2e", "status": "IN_PROGRESS"}]}
        def gh(*a):
            if a[0] == "search":
                return [{"url": "https://github.com/o/r/pull/2"}]
            if a[0] == "repo":
                return {"defaultBranchRef": {"name": "master"}}
            if a[:2] == ("pr", "view"):
                return pr(int(a[2]), "b1" if a[2] == "2" else "master")
            # every open PR at once; 99 (master→release) must never be followed
            return [{"number": 2, "headRefName": "b2", "baseRefName": "b1"}, {"number": 1, "headRefName": "b1", "baseRefName": "master"},
                    {"number": 99, "headRefName": "master", "baseRefName": "release"}]
        with mock.patch.object(tracker, "_gh", side_effect=gh), mock.patch.object(bus, "load_memory", return_value={}):
            chain = tracker.prs("TK-1")
        self.assertEqual([p["number"] for p in chain], [2, 1])
        def limited(*a):
            if a[0] == "search":
                raise OSError("HTTP 403: API rate limit exceeded")
            return gh(*a)
        with mock.patch.object(tracker, "_gh", side_effect=limited), \
                mock.patch.object(bus, "load_memory", return_value={"TK-1": {"pr": "https://github.com/o/r/pull/2"}}):
            self.assertEqual([p["number"] for p in tracker.prs("TK-1")], [2, 1])  # a rate-limited search still shows known PRs
        with mock.patch.object(tracker, "_gh", side_effect=gh), mock.patch.object(bus, "load_memory", return_value={"TK-1": {"pr": 2}}):
            self.assertEqual([p["number"] for p in tracker.prs("TK-1")], [2, 1])  # CodeBrain may store a bare PR number
        with mock.patch.object(tracker, "_gh", side_effect=limited), mock.patch.object(bus, "load_memory", return_value={}):
            self.assertEqual([p["number"] for p in tracker.prs("TK-1")], [2, 1])  # or the PRs its last search found
            with self.assertRaises(OSError):
                tracker.prs("TK-404")  # never searched: the error still shows
        chain[0]["body"] = "## Summary\n- **bold** item\n\n```py\nx = 1\n```"
        console = Console(width=100, record=True, file=io.StringIO())
        console.print(tracker.right(chain))
        plain = console.export_text()
        self.assertIn("Summary", plain); self.assertNotIn("##", plain); self.assertNotIn("**", plain)  # Markdown rendered, not raw
        self.assertIn("• bold item", plain); self.assertIn("x = 1", plain); self.assertNotIn("```", plain)
        self.assertIn("#2 · open", plain); self.assertIn("▼ into b1", plain); self.assertIn("◉ master", plain)  # the tile flow
        self.assertIn("● abcdef1 c", plain); self.assertIn("✓1 ✗1 ◌1", plain); self.assertIn("╭", plain)
        self.assertIn("1 failing, 1 in progress, 1 successful checks", plain); self.assertIn("Some checks were not successful", plain)
        self.assertIn("wants to merge 1 commit into b1 from b2", plain); self.assertIn("committed  c", plain); self.assertNotIn("#99", plain)
        self.assertEqual(tracker._tone(chain[0]), graph.CORAL)  # a failing check turns the tile coral

    def test_clicking_a_ticket_opens_the_two_sided_popup(self):
        async def go():
            bus.paused.set()
            info = {"key": "TK-1", "summary": "Sum", "status": "Backlog", "type": "", "priority": "", "assignee": "", "reporter": "",
                    "updated": "", "description": "", "comments": []}
            with mock.patch.object(tracker, "issue", return_value=info), mock.patch.object(tracker, "prs", return_value=[]), \
                    mock.patch.object(tracker, "comment_jira") as post:
                async with ui.Dashboard().run_test(size=(160, 64)) as pilot:
                    pilot.app.action_show_ticket("TK-1"); await pilot.pause(0.4)
                    self.assertTrue(pilot.app.query_one("#ticketpop").display)
                    self.assertTrue(pilot.app.query_one("#backdrop").display)  # the dashboard behind it is dimmed
                    console = Console(width=100, record=True, file=io.StringIO())
                    console.print(pilot.app.query_one("#jirainfo").content)
                    self.assertIn("Backlog", console.export_text())
                    self.assertIn("No pull requests", str(pilot.app.query_one("#ghinfo").render()))
                    self.assertNotIn("GitHub", str(pilot.app.query_one("#ghlink").render()))
                    pilot.app._show_side("TK-1", "gh", [{"url": "https://github.com/o/r/pull/7"}], "")
                    self.assertIn("Open in GitHub", str(pilot.app.query_one("#ghlink").render()))
                    with mock.patch.object(ui.webbrowser, "open") as browse:
                        pilot.app.action_open_pr()
                    browse.assert_called_once_with("https://github.com/o/r/pull/7")
                    pilot.app.query_one("#jiranote").text = "looks good"
                    pilot.app.post_comment("jira"); await pilot.pause(0.4)
                    post.assert_called_once_with("TK-1", "looks good")
                    self.assertEqual(pilot.app.query_one("#jiranote").text, "")
                    pilot.app.action_close_ticket()
                    self.assertFalse(pilot.app.query_one("#ticketpop").display)
                    self.assertFalse(pilot.app.query_one("#backdrop").display)
                    bus.log("GitHubBrain", "Rebased TK-1 onto master")
                    await pilot.app.action_show_comms("TK-1"); await pilot.pause(0.4)  # its own popup, not the brains log
                    self.assertTrue(pilot.app.query_one("#commsbox").display); self.assertFalse(pilot.app.query_one("#logbox").display)
                    self.assertEqual(pilot.app.query_one("#commstabs").active, "ct-TK-1")  # a tab per ticket, opened on this one
                    self.assertIn("Rebased TK-1 onto master", pilot.app.query_one("#cl-TK-1").plain())
                    self.assertFalse(pilot.app.query_one("#cl-TK-1").dirty)  # drawn, so it can be scrolled and selected
                    x = pilot.app.query_one("#commsclose").size.width - 1; await pilot.click("#commsclose", offset=(x, 0)); await pilot.pause(0.1)
                    self.assertFalse(pilot.app.query_one("#commsbox").display)  # ✕ in the top right closes it
                    with mock.patch.dict(graph.JIRA, {"assigned": {"TK-2": ("DONE", "d"), "TK-3": ("IN QA", "a"), "TK-4": ("IN QA", "b")}, "worked": {}}), \
                            mock.patch.object(ui, "LAYOUT_FILE", bus.ROOT / "state" / "layout.test.json"):
                        pilot.app.layout_sizes["tabs"] = ["TK-4", "TK-3"]  # your dragged order
                        await pilot.app.action_show_comms("TK-3"); await pilot.pause(0.2)
                        names = lambda: [t.id.removeprefix("--content-tab-ct-") for t in pilot.app.query_one("#commstabs").query(Tab)]
                        self.assertEqual(names(), ["TK-4", "TK-3", "TK-2"])  # open ones in your order, done after; TK-1 left the card
                        tab = lambda k: pilot.app.query_one(f"#--content-tab-ct-{k}")
                        await pilot.mouse_down(tab("TK-4")); await pilot.mouse_up(); await pilot.pause(0.1)
                        self.assertEqual(pilot.app.query_one("#commstabs").active, "ct-TK-4")  # press and release opens it
                        await pilot.mouse_down(tab("TK-3")); await pilot.hover(tab("TK-4")); await pilot.mouse_up(); await pilot.pause(0.1)
                        self.assertEqual(names()[:2], ["TK-3", "TK-4"])  # dragged into place
                        self.assertEqual(pilot.app.layout_sizes["tabs"][:2], ["TK-3", "TK-4"])  # and remembered
                        await pilot.mouse_down(tab("TK-2")); await pilot.hover(tab("TK-4")); await pilot.mouse_up(); await pilot.pause(0.1)
                        self.assertEqual(names()[-1], "TK-2")  # a done ticket can't move in among the open ones
                    (bus.ROOT / "state" / "layout.test.json").unlink(missing_ok=True)
        asyncio.run(go())


class Logo(unittest.TestCase):
    def test_timestamps_show_in_local_time(self):
        with mock.patch.dict(os.environ, {"TZ": "America/Los_Angeles"}):
            time.tzset()
            self.assertEqual(tracker._when("2026-10-30T23:26:00Z"), "2026-10-30 16:26")
            self.assertEqual(tracker._when("2026-10-30T23:26:00.000+0000"), "2026-10-30 16:26")
            self.assertEqual(graph.when("2026-10-30T23:26:00.1234567Z"), "2026-10-30 16:26")
        time.tzset()

    def test_animates_while_running_and_freezes_when_paused(self):
        from rich.text import Text
        plain = lambda m: Text.from_markup(m).plain
        self.assertIn("D E V B O T", plain(ui.logo(0)))
        self.assertNotEqual(ui.logo(0), ui.logo(1))
        self.assertEqual(ui.logo(0, False), ui.logo(1, False))


class ProcessLifecycle(unittest.TestCase):
    def test_runs_get_their_own_session_and_die_as_a_group(self):
        """A hang-up on the terminal must not reach the runs (47 ended with exit 129 in two days), and the time limit must
        kill the helpers holding stdout, not just claude."""
        proc = mock.Mock(stdout=io.StringIO(""), stderr=io.StringIO(""), returncode=0, pid=4242)
        with mock.patch("subprocess.Popen", return_value=proc) as popen, mock.patch.object(claude.threading, "Timer") as timer:
            claude.stream("duty", "JIRABrain")
        self.assertTrue(popen.call_args.kwargs["start_new_session"])
        self.assertEqual(timer.call_args.args[1:], (claude.kill_group, [proc]))
        with mock.patch.object(claude.os, "killpg") as killpg:
            claude.kill_group(proc)
        killpg.assert_called_once_with(4242, claude.signal.SIGKILL)
        with mock.patch.object(claude.os, "killpg", side_effect=ProcessLookupError):
            claude.kill_group(proc)  # already gone: nothing to do
        cid = bus.new_chat()
        self.addCleanup(bus.delete_chat, cid)
        self.addCleanup(brains._procs.pop, cid, None)
        with mock.patch("subprocess.Popen", return_value=mock.Mock(poll=lambda: None, stdout=io.StringIO(""))) as popen, \
                mock.patch.object(brains.threading, "Thread"):
            brains._chat_proc(cid)
        self.assertTrue(popen.call_args.kwargs["start_new_session"])


class RunGap(unittest.TestCase):
    def test_wakes_soon_after_a_run_wait_out_the_gap_unless_rushed(self):
        waits = []
        bus.paused.set()
        with mock.patch.object(brains, "MIN_GAP", 600), mock.patch.object(triggers, "live", {"JIRABrain"}), \
                mock.patch.object(bus.wake["JIRABrain"], "wait", side_effect=lambda t=None: waits.append(("wake", t))), \
                mock.patch.object(bus.rush["JIRABrain"], "wait", side_effect=lambda t=None: waits.append(("gap", round(t, -1)))):
            bus.status["JIRABrain"]["last_tick"] = time.time() - 100  # a run ended 100 s ago
            brains.nap("JIRABrain")  # its trigger fired again (its own writes): wait out the remaining 500 s
            bus.run_now("JIRABrain")
            brains.nap("JIRABrain")  # Run now: no gap
            bus.status["JIRABrain"]["last_tick"] = time.time() - 1000
            brains.nap("JIRABrain")  # the gap has passed
        bus.status["JIRABrain"]["last_tick"] = 0.0
        self.assertEqual(waits, [("wake", brains.MAX_IDLE), ("gap", 500), ("wake", brains.MAX_IDLE), ("wake", brains.MAX_IDLE)])
        self.assertFalse(bus.rush["JIRABrain"].is_set() or bus.wake["JIRABrain"].is_set())


class ParsedOncePerChange(unittest.TestCase):
    def test_messages_and_done_are_parsed_once_per_change(self):
        d = tempfile.TemporaryDirectory(); self.addCleanup(d.cleanup)  # own files: the shared ones feed other tests
        for name in ("MESSAGES_FILE", "DONE_FILE"):
            q = mock.patch.object(bus, name, pathlib.Path(d.name) / f"{name}.json"); q.start(); self.addCleanup(q.stop)
        bus.MESSAGES_FILE.write_text(json.dumps([{"id": "1", "from": "A", "chat": "A", "at": "2026-11-02T10:00:00Z", "text": "hi"}]))
        with mock.patch.object(bus, "_collapse", wraps=bus._collapse) as collapse:
            first, again = bus.messages(), bus.messages()
        self.assertEqual(collapse.call_count, 1)
        self.assertIs(first, again)
        bus.add_messages([{"id": "2", "from": "B", "chat": "B", "at": "2026-11-02T11:00:00Z", "text": "yo"}], "TeamsBrain")
        self.assertEqual([m["id"] for m in bus.messages()], ["2", "1"])  # a write is seen at once
        bus.DONE_FILE.write_text("[]")
        self.assertEqual(bus.done_today(), [])
        bus.finished("ZZ-1", 1, 0.1, "done")
        self.assertEqual([d["key"] for d in bus.done_today()], ["ZZ-1"])


class ReadOnlyRuns(unittest.TestCase):
    def test_chat_copies_run_with_an_allow_list_and_no_bypass(self):
        cmd = claude.base_cmd(brain="TeamsBrain", deny=claude.SYNC_DENY, allow=claude.SYNC_ALLOW)
        self.assertNotIn("--dangerously-skip-permissions", cmd)
        for flag, value in (("--tools", "ToolSearch"), ("--permission-mode", "dontAsk"), ("--permission-prompts", "none")):
            self.assertEqual(cmd[cmd.index(flag) + 1], value)  # no built-in can run a shell or publish; a would-be prompt is a refusal, never a hang
        self.assertEqual(cmd[cmd.index("--allowedTools") + 1:], claude.SYNC_ALLOW)  # last: the prompt goes on stdin
        self.assertTrue(all(t == "ToolSearch" or t.startswith("mcp__claude_ai_Microsoft_365__") for t in claude.SYNC_ALLOW))  # reads, never the browser
        self.assertFalse(set(claude.SYNC_ALLOW) & set(claude.SYNC_DENY))
        for t in ("mcp__teams-browser", "mcp__claude_ai_Roam", "mcp__claude_ai_Microsoft_365__outlook_batch_delete_messages"):
            self.assertIn(t, claude.SYNC_DENY)  # a whole server goes by its bare name
        self.assertNotIn("Bash", claude.READ_ONLY)  # a ticket review still needs gh, with the bypass
        self.assertIn("--dangerously-skip-permissions", claude.base_cmd(brain="JIRABrain", deny=claude.READ_ONLY))
        for t in ("mcp__claude_ai_Roam__chat_delete", "mcp__plugin_atlassian_atlassian__transitionJiraIssue", "mcp__claude_ai_Microsoft_365__outlook_send_mail"):
            self.assertIn(t, claude.READ_ONLY)
        with mock.patch.object(claude, "on_cursor", return_value=True), mock.patch("subprocess.Popen") as popen:
            ev = claude.stream("copy", "TeamsBrain", allow=claude.SYNC_ALLOW)
        self.assertTrue(ev["is_error"] and "Cursor" in ev["error"])
        popen.assert_not_called()  # Cursor's agent grants everything: an allow-listed run never goes there
        logged = []
        with mock.patch.object(claude, "tier", return_value="login"), mock.patch.object(bus, "gate"), \
             mock.patch.object(claude, "run", return_value={"error": "needs Claude Code's permission checks"}), \
             mock.patch.object(time, "sleep", side_effect=StopIteration), mock.patch.object(triggers, "m365", side_effect=RuntimeError("signed out")), \
             mock.patch.object(bus, "log", lambda b, msg, *a: logged.append(msg)):
            with self.assertRaises(StopIteration):
                brains.sync("TeamsBrain")
        self.assertIn("Couldn't copy chats to the dashboard: needs Claude Code's permission checks", logged)  # a failed copy is never silent
        with mock.patch.object(claude, "tier", return_value="api"), mock.patch.object(claude, "run") as run, mock.patch.object(bus, "gate"), \
             mock.patch.object(time, "sleep", side_effect=StopIteration), mock.patch.object(triggers, "m365", side_effect=RuntimeError("signed out")):
            with self.assertRaises(StopIteration):
                brains.sync("OutlookBrain")
        run.assert_not_called()  # off the login tier the connector is not loaded: a copy run could only cost money


class TidyNote(unittest.TestCase):
    def test_drops_ids_and_shortens_times(self):
        out = bus.tidy_note("Fonseca asked (msg 1793646923019, 2026-11-02T19:15:23Z). Done.")
        hm = datetime.datetime.fromisoformat("2026-11-02T19:15:23+00:00").astimezone().strftime("%H:%M")
        self.assertEqual(out, f"Fonseca asked ({hm}). Done.")


class BrowserOutput(unittest.TestCase):
    def test_playwright_files_stay_in_state(self):
        args = json.loads(claude.MCP)["mcpServers"]["teams-browser"]["args"]
        self.assertEqual(args[args.index("--output-dir") + 1], str(bus.ROOT / "state" / "playwright"))
