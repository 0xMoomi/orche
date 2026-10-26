"""The owner's voice (C10 T9, lib/orche/flow/owner.py and lib/orche/notify.py): Main's closing line is
the only owner notification; ask.json; no first notification while the owner is at the screen; at most
one per project per 300 s; a parked guard refusal records an ask; one argv-safe notify path (the text is
an osascript argument, never script). The Stop-hook path itself is in test_hooks.OwnerVoiceHookTest."""
import datetime as dt
import json
import os
import unittest
from unittest import mock

import support
from orche import inbox, notify, shell
from orche.flow import guard, owner

T0 = dt.datetime(2026, 10, 23, 5, 0, tzinfo=dt.timezone.utc)
SCRIPT = ["osascript", "-e", "on run argv", "-e",
          'display notification (item 1 of argv) with title (item 2 of argv) sound name "Glass"', "-e", "end run"]
ORCA_RUNNING = notify.orca_running      # the real probe (through shell.RUNNER), saved before OwnerCase patches it
NON_ASCII = '決済キーを入れてください — "STRIPE" \\ 既定: 保留; do shell script "rm -rf ~"'


def at(seconds):
    return T0 + dt.timedelta(seconds=seconds)


def iso(t):
    return t.astimezone().isoformat(timespec="seconds")


class ParseTest(unittest.TestCase):
    CASES = {
        "running": ("work\nRUNNING: build runs the tests", ("RUNNING", "build runs the tests")),
        "owner": ("OWNER: approve the price (default: keep)", ("OWNER", "approve the price (default: keep)")),
        "done, trailing blank lines": ("Summary\n\nDONE: shipped v2\n\n   \n", ("DONE", "shipped v2")),
        "untagged": ("all good, nothing else", None),
        "a tag that is not last": ("OWNER: first\nthen prose", None),
        "a tag inside a fence": ("intro\n```\nOWNER: inside a fence\n```\n", None),
        "a fence after the tag": ("OWNER: real\n```text\nDONE: in code\n```", ("OWNER", "real")),
        "a longer fence holds a shorter one": ("OWNER: before\n````\n```\nDONE: nested\n```\n````",
                                               ("OWNER", "before")),
        "an unclosed fence": ("OWNER: kept\n~~~\nDONE: never closed", ("OWNER", "kept")),
        "bold, non-ASCII intact": (f"**OWNER:** {NON_ASCII}", ("OWNER", NON_ASCII)),
        "bold around the line": ("**DONE: shipped**", ("DONE", "shipped")),
        "a fenced dispatch holds a nested snippet": ("```dispatch\nTO div-acme-build\nCONTEXT see\n  ```python\n"
                                                     "  x = 1\n  ```\n```\nOWNER: approve the plan",
                                                     ("OWNER", "approve the plan")),
        "wrapped in backticks": ("`OWNER: approve`", ("OWNER", "approve")),
        "a quote": ("> OWNER: approve", ("OWNER", "approve")),
        "a bullet": ("- DONE: shipped", ("DONE", "shipped")),
        "underscore bold": ("__OWNER:__ approve", ("OWNER", "approve")),
        "underscore bold around the line": ("__DONE: shipped__", ("DONE", "shipped")),
        "a full-width colon": ("OWNER： 決済キー", ("OWNER", "決済キー")),
        "a space before the colon": ("RUNNING : tests", ("RUNNING", "tests")),
        "lower case": ("owner: lower case", None),
        "a bare tag": ("OWNER:", None),
        "empty": ("", None),
    }

    def test_table(self):
        for name, (text, want) in self.CASES.items():
            with self.subTest(name):
                self.assertEqual(owner.parse(text), want)

    def test_step_of_takes_the_last_owner_line_outside_fences(self):
        text = "STATUS: blocked\nOWNER: secret A\nmore\nOWNER: secret STRIPE_KEY\n```\nOWNER: in a fence\n```"
        self.assertEqual(owner.step_of(text), "secret STRIPE_KEY")
        self.assertIsNone(owner.step_of("STATUS: done\nall good"))
        self.assertEqual(len(owner.step_of("OWNER: " + "あ" * 300)), owner.STEP_MAX)
        self.assertNotIn("A" * 40, owner.step_of("OWNER: use sk-ant-api03-" + "A" * 40))
        self.assertEqual(owner.kind_of("secret STRIPE_KEY, GH_TOKEN"), "secret")
        self.assertEqual(owner.kind_of("approve the secret plan"), "owner")


class RenderTest(unittest.TestCase):
    def test_the_owner_step_is_the_pointers_first_line(self):
        ptr = inbox.make("acme-x-1", "div-acme-build", "main-acme", "blocked", summary="need the key",
                         owner="secret STRIPE_KEY")
        plain = inbox.make("acme-y-1", "div-acme-build", "main-acme", "done", summary="ok")
        lines = inbox.render([(None, ptr), (None, plain)]).splitlines()
        self.assertEqual(lines[2:5], ["- unit acme-x-1 · from div-acme-build · status blocked",
                                      "  OWNER STEP: secret STRIPE_KEY", "  brief_rev 1 · head - · ledger_row no"])
        self.assertEqual(sum("OWNER STEP" in line for line in lines), 1)


class OwnerCase(support.TempHomeCase):
    """A fake clock (`now`), quiet hours off, notifications on macOS, Orca not running and no terminal-notifier
    (the plain osascript path): every argv is recorded."""

    def setUp(self):
        super().setUp()
        self.config({})
        self.calls = []

        def runner(argv, *, timeout, input, env=None):
            self.calls.append(list(argv))
            return shell.Result(0, "", "", list(argv))
        for obj, name, value in ((shell, "RUNNER", runner), (notify, "PLATFORM", "darwin"),
                                 (notify, "orca_running", lambda: False)):
            patcher = mock.patch.object(obj, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def config(self, cfg):
        (self.home / "config.json").write_text(json.dumps(support.no_wake({"flow": {"quiet_hours": ""}, **cfg})))

    def notes(self):
        return [c for c in self.calls if c[0] == "osascript"]

    def ask(self, project="acme"):
        path = guard.folder(project) / owner.ASK
        return json.loads(path.read_text()) if path.exists() else None

    def log(self, name="events"):
        path = self.home / "logs" / f"{name}.log"
        return path.read_text() if path.exists() else ""

    def owner_seen(self, ago, project="acme"):
        path = guard.folder(project) / "owner-seen"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
        t = T0.timestamp() - ago
        os.utime(path, (t, t))


class ClosingLineTest(OwnerCase):
    def test_owner_records_the_ask_and_notifies_once_the_body_an_argument(self):
        line = f"OWNER: {NON_ASCII}"
        self.assertEqual(owner.closing("acme", f"Research done.\n\n{line}\n", now=T0), "OWNER")
        self.assertEqual(self.ask(), {"text": NON_ASCII, "at": iso(T0), "kind": "owner", "source": "main"})
        [call] = self.notes()
        self.assertEqual(call, SCRIPT + [line, "ORCHE acme"])        # verbatim: quotes and backslashes are data
        notified = json.loads((guard.folder("acme") / owner.SENT).read_text())
        self.assertEqual(notified, {"at": iso(T0), "n": 1, "ask_at": iso(T0), "ask_last": iso(T0), "kind": "owner"})
        self.assertIn("owner ask project=acme kind=owner source=main\n", self.log())
        self.assertIn("owner notify project=acme kind=owner n=1 quiet=0\n", self.log())
        for name in ("events", "hooks"):
            self.assertNotIn("決済", self.log(name))
            self.assertNotIn("STRIPE", self.log(name))

    def test_done_closes_the_ask_and_notifies_once(self):
        owner.closing("acme", "OWNER: pick a plan", now=T0)
        self.assertEqual(owner.closing("acme", "All landed.\nDONE: shipped v2 (3 PRs)", now=at(400)), "DONE")
        self.assertEqual(self.ask()["closed_at"], iso(at(400)))
        self.assertIsNone(owner.open_ask("acme"))
        self.assertEqual([c[7] for c in self.notes()], ["OWNER: pick a plan", "DONE: shipped v2 (3 PRs)"])
        self.assertIn("owner closed project=acme\n", self.log())
        owner.closing("acme", "DONE: again", now=at(800))             # no open ask: still one notification
        self.assertEqual(len(self.notes()), 3)
        self.assertEqual(self.ask()["closed_at"], iso(at(400)))

    def test_running_and_untagged_notify_nothing(self):
        self.assertEqual(owner.closing("acme", "RUNNING: build runs the coupon tests", now=T0), "RUNNING")
        self.assertIsNone(owner.closing("acme", "Working on it.", now=T0))
        self.assertEqual((self.notes(), self.ask()), ([], None))
        self.assertIn("owner untagged project=acme\n", self.log())

    def test_no_first_notification_while_the_owner_is_at_the_screen(self):
        self.owner_seen(60)
        owner.closing("acme", "OWNER: approve", now=T0)
        self.assertEqual((self.ask()["text"], self.notes()), ("approve", []))
        self.assertIn("owner skipped project=acme kind=owner reason=present\n", self.log())
        owner.closing("acme", "DONE: shipped", now=T0)
        self.assertEqual(self.notes(), [])
        self.owner_seen(121)
        owner.closing("acme", "OWNER: approve", now=T0)
        self.assertEqual(len(self.notes()), 1)

    def test_one_notification_per_project_per_300_s(self):
        owner.closing("acme", "OWNER: first", now=T0)
        owner.closing("acme", "OWNER: second", now=at(299))
        self.assertEqual(self.ask()["text"], "second")                   # recorded, not sent
        self.assertEqual(len(self.notes()), 1)
        self.assertIn("owner held project=acme kind=owner reason=gap\n", self.log())
        owner.closing("docs", "OWNER: other project", now=at(10))        # per project
        owner.closing("acme", "OWNER: third", now=at(300))
        self.assertEqual([c[7] for c in self.notes()], ["OWNER: first", "OWNER: other project", "OWNER: third"])

    def test_a_secret_ask_and_notifications_off(self):
        self.config({"notify": False})
        owner.closing("acme", "OWNER: secret STRIPE_KEY, GH_TOKEN", now=T0)
        self.assertEqual((self.ask()["kind"], self.notes()), ("secret", []))
        self.assertIn("owner held project=acme kind=secret reason=off\n", self.log())

    def test_no_sound_in_quiet_hours(self):
        h = T0.hour                                   # the clock passed, not the host's zone
        self.config({"flow": {"quiet_hours": f"{h:02d}:00-{(h + 1) % 24:02d}:00"}})
        owner.closing("acme", "OWNER: approve", now=T0)
        [call] = self.notes()
        self.assertEqual(call[4], notify.SCRIPT[1])
        self.assertIn("quiet=1", self.log())

    def test_a_parked_guard_refusal_records_an_ask_and_notifies_with_the_owner_present(self):
        self.owner_seen(10)
        text = "not started: acme-x-20261023-1 ran 4 times since 10:00 without the owner (cap 4)"
        owner.parked("acme", "acme-x-20261023-1", text, now=T0)
        self.assertEqual(self.ask(), {"text": text, "at": iso(T0), "kind": "guard", "source": "runner",
                                      "unit": "acme-x-20261023-1"})
        [call] = self.notes()
        self.assertEqual(call[7:], [f"OWNER: {text}", "ORCHE acme"])


class NotifySendTest(OwnerCase):
    def test_one_line_masked_gated_and_never_script(self):
        secret = "sk-ant-api03-" + "A" * 40
        self.assertTrue(notify.send("ORCHE\nacme", f"line one\nline\ttwo\x07 {secret}", kind="t"))
        [call] = self.notes()
        self.assertEqual(call[:7], SCRIPT[:4] + [notify.SCRIPT[1]] + SCRIPT[5:])     # no sound unless asked
        body, title = call[7:]
        self.assertTrue(body.startswith("line one line two "), body)
        self.assertNotIn("A" * 40, body)
        self.assertEqual(title, "ORCHE acme")
        self.assertIn("notify kind=t ok=1\n", self.log())
        with mock.patch.object(notify, "PLATFORM", "linux"):
            self.assertFalse(notify.send("ORCHE", "x"))
        self.config({"notify": False})
        self.assertFalse(notify.send("ORCHE", "x"))
        self.assertEqual(len(self.notes()), 1)

    def test_a_leading_dash_is_never_an_option(self):
        self.assertTrue(notify.send("-t", "-e do shell script", kind="t"))
        [call] = self.notes()
        self.assertEqual(call[7:], [" -e do shell script", " -t"])

    def test_orca_running_wraps_the_line_in_a_tell_to_orca(self):
        """c62: a click opens Orca, not Script Editor; one `pgrep -xq Orca` decides (never launch Orca)."""
        with mock.patch.object(notify, "orca_running", ORCA_RUNNING):
            self.assertTrue(notify.send("ORCHE", "-x", sound=True, kind="t", project="acme"))
        self.assertEqual(self.calls, [["pgrep", "-xq", "Orca"], [
            "osascript", "-e", "on run argv", "-e", 'tell application id "com.stablyai.orca"',
            "-e", 'display notification (item 1 of argv) with title (item 2 of argv) sound name "Glass"',
            "-e", "end tell", "-e", "end run", " -x", "ORCHE"]])
        self.calls.clear()
        with mock.patch.object(shell, "RUNNER", lambda argv, **kw: self.calls.append(list(argv)) or
                               shell.Result(1 if argv[0] == "pgrep" else 0, "", "", list(argv))), \
                mock.patch.object(notify, "orca_running", ORCA_RUNNING):
            self.assertTrue(notify.send("ORCHE", "x", kind="t"))
        self.assertEqual(self.calls[1], ["osascript", "-e", "on run argv", "-e", notify.SCRIPT[1], "-e", "end run",
                                         "x", "ORCHE"])

    def test_terminal_notifier_opens_the_projects_main_tab(self):
        with mock.patch.object(notify, "WHICH", lambda name: "/opt/homebrew/bin/" + name):
            self.assertTrue(notify.send("ORCHE", "x", sound=True, kind="t", project="acme"))
            self.assertTrue(notify.send("-t", "-e do shell script", kind="t"))
            self.assertTrue(notify.send("ORCHE", "x", kind="t", project="acme; rm -rf ~"))
        bin_ = str(support.ROOT / "bin" / "orche")
        self.assertEqual(self.calls, [
            ["terminal-notifier", "-title", "ORCHE", "-message", "x", "-sound", "Glass", "-activate",
             "com.stablyai.orca", "-execute", f"{bin_} focus acme"],
            ["terminal-notifier", "-title", " -t", "-message", " -e do shell script", "-activate", "com.stablyai.orca"],
            ["terminal-notifier", "-title", "ORCHE", "-message", "x", "-activate", "com.stablyai.orca"]])
        self.assertIn("notify t: project key refused", self.log("hooks"))
        self.assertNotIn("rm -rf", self.log("hooks") + self.log())
        self.assertEqual(notify.ORCHE_BIN, support.ROOT / "bin" / "orche")
        with mock.patch.object(notify, "ORCHE_BIN", support.ROOT / "a dir" / "orche"):
            self.assertEqual(notify.notifier_argv("T", "B", project="acme")[-1],
                             f"'{support.ROOT / 'a dir' / 'orche'}' focus acme")

    def test_a_refused_terminal_notifier_falls_back_to_osascript(self):
        """c64: terminal-notifier exits 3 (permission refused) or times out: osascript shows it instead."""
        timeouts = []

        def runner(code):
            def run(argv, *, timeout, input, env=None):
                self.calls.append(list(argv))
                timeouts.append(timeout)
                ok = argv[0] != "terminal-notifier"
                return shell.Result(0 if ok else code, "", "", list(argv))
            return run
        osa = notify.argv("ORCHE", "secret plan 42", orca=False)
        with mock.patch.object(notify, "WHICH", lambda name: "/opt/homebrew/bin/" + name):
            for code in (3, 124):
                self.calls.clear()
                timeouts.clear()
                with mock.patch.object(shell, "RUNNER", runner(code)):
                    self.assertTrue(notify.send("ORCHE", "secret plan 42", kind="t"))
                self.assertEqual([c[0] for c in self.calls], ["terminal-notifier", "osascript"])
                self.assertEqual(self.calls[1], osa)
                self.assertEqual(timeouts, [notify.NOTIFIER_TIMEOUT_S, notify.TIMEOUT_S])
                self.assertIn(f"notify t: terminal-notifier exit {code}, osascript fallback", self.log("hooks"))
            self.calls.clear()
            with mock.patch.object(shell, "RUNNER", lambda argv, **kw: self.calls.append(list(argv)) or
                                   shell.Result(1, "", "", list(argv))):
                self.assertFalse(notify.send("ORCHE", "x", kind="u"))   # the result is osascript's
            self.assertEqual([c[0] for c in self.calls], ["terminal-notifier", "osascript"])
            self.assertIn("notify u: osascript exit 1", self.log("hooks"))
            self.calls.clear()

            def raises(argv, **kw):
                self.calls.append(list(argv))
                if argv[0] == "terminal-notifier":
                    raise OSError("terminal-notifier secret plan 42")
                return shell.Result(0, "", "", list(argv))
            with mock.patch.object(shell, "RUNNER", raises):
                self.assertTrue(notify.send("ORCHE", "x", kind="v"))
            self.assertEqual([c[0] for c in self.calls], ["terminal-notifier", "osascript"])
            self.assertIn("notify v: terminal-notifier OSError, osascript fallback", self.log("hooks"))
            self.calls.clear()
            self.assertTrue(notify.send("ORCHE", "x", kind="w"))       # exit 0: no osascript
            self.assertEqual([c[0] for c in self.calls], ["terminal-notifier"])
        self.assertNotIn("secret plan", self.log("hooks") + self.log())
        self.assertEqual(notify.NOTIFIER_TIMEOUT_S, 3)

    def test_a_failing_osascript_is_logged_without_the_text(self):
        with mock.patch.object(shell, "RUNNER", lambda argv, **kw: shell.Result(1, "", "boom", argv)):
            self.assertFalse(notify.send("ORCHE", "secret plan 42", kind="t"))
        self.assertIn("notify t: osascript exit 1", self.log("hooks"))
        self.assertNotIn("secret plan", self.log("hooks") + self.log())
