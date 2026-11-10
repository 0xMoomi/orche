"""The owner's voice (T9, lib/orche/flow/owner.py and lib/orche/notify.py): Main's closing line records
the ask and rings nothing (Orca shows its own notification); ask.json; at most one ring per project per
300 s, none in quiet hours; a parked guard refusal records an ask and rings Main's tab; the bell (one BEL
byte to the terminal Main's hooks recorded, never text). The Stop-hook path itself is in
test_hooks.OwnerVoiceHookTest."""
import datetime as dt
import json
import os
import unittest
from unittest import mock

import support
from orche import inbox, notify, shell
from orche.flow import guard, owner

T0 = dt.datetime(2026, 10, 23, 5, 0, tzinfo=dt.timezone.utc)
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


class FitTest(support.TempHomeCase):
    """inbox.fit: report bodies are added LINEAGE.md first, then failed, blocked and question reports, then
    done reports, while they fit; urgent and owner-step pointers are never left pending."""

    def item(self, unit, status, name="REPORT.md", **kw):
        path = self.home / unit / name
        path.parent.mkdir(parents=True)
        path.write_text(f"{unit} body " + "b" * 900)
        return (None, inbox.make(unit, "div-acme-build", "main-acme", status, report_path=str(path), **kw))

    def test_bodies_fill_the_room_in_rank_order(self):
        items = [self.item("u-done", "done"), self.item("u-failed", "failed"), self.item("L-1", "done", "LINEAGE.md")]
        skeleton = len(inbox.render(items))
        for room, shown in ((skeleton + 1000, ["L-1"]), (skeleton + 2000, ["L-1", "u-failed"]),
                            (skeleton + 3000, ["L-1", "u-failed", "u-done"]), (skeleton, [])):
            with self.subTest(room=room):
                text, kept = inbox.fit(items, room=room)
                self.assertLessEqual(len(text), room)
                self.assertEqual(len(kept), 3)
                self.assertEqual([u for u in ("L-1", "u-failed", "u-done") if f"{u} body" in text], shown)

    def test_over_the_room_plain_pointers_wait_urgent_and_owner_steps_stay(self):
        items = [self.item("u-owner", "blocked", owner="approve"), self.item("u-urgent", "info", urgent=True)]
        items += [self.item(f"u-{i}", "done", summary="s" * 400) for i in range(10)]
        text, kept = inbox.fit(items, room=1500)
        units_ = [p["unit"] for _x, p in kept]
        self.assertEqual(units_[:2], ["u-owner", "u-urgent"])
        self.assertLess(len(kept), len(items))
        self.assertLessEqual(len(text), 1500)

    def test_a_lineage_record_is_cut_at_4000_and_a_report_at_1800(self):
        """c120: LINEAGE.md is the request's record and carries up to LINEAGE_MAX; a REPORT.md stays at BODY_MAX."""
        self.assertEqual((inbox.LINEAGE_MAX, inbox.BODY_MAX), (4000, 1800))
        for name, limit in (("LINEAGE.md", 4000), ("REPORT.md", 1800)):
            with self.subTest(name=name):
                path = self.home / name.split(".")[0] / name
                path.parent.mkdir(parents=True)
                path.write_text("x" * 5000)
                body = inbox.body(str(path))
                self.assertIn("x" * limit + "\n", body)
                self.assertNotIn("x" * (limit + 1), body)
                self.assertTrue(body.endswith(f"[not shown in full: read {path}]"))
                path.write_text("y" * limit)
                self.assertNotIn("not shown in full", inbox.body(str(path)))

    def test_the_tail_survives_an_oversized_head(self):
        tail = inbox.REPORT_LINE
        text = inbox._within("## Standing orders\n" + "x" * 20000, "## Inbox (1)", tail, 9000)
        self.assertLessEqual(len(text), 9000)
        self.assertTrue(text.endswith("\n\n" + tail))
        self.assertIn("[not shown in full: [drain] repeats the inbox]", text)


class OwnerCase(support.TempHomeCase):
    """A fake clock (`now`), quiet hours off, notifications on; notify.ring is a fake that records
    (project, kind) and rings."""

    def setUp(self):
        super().setUp()
        self.config({})
        self.rings = []

        def ring(project, *, kind):
            self.rings.append((project, kind))
            return True
        patcher = mock.patch.object(notify, "ring", ring)
        patcher.start()
        self.addCleanup(patcher.stop)

    def config(self, cfg):
        (self.home / "config.json").write_text(json.dumps(support.no_wake({"flow": {"quiet_hours": ""}, **cfg})))

    def notes(self):
        return self.rings

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
    def test_owner_records_the_ask_and_does_not_notify(self):
        line = f"OWNER: {NON_ASCII}"
        self.assertEqual(owner.closing("acme", f"Research done.\n\n{line}\n", now=T0), "OWNER")
        self.assertEqual(self.ask(), {"text": NON_ASCII, "at": iso(T0), "kind": "owner", "source": "main"})
        self.assertEqual(self.notes(), [])                              # Orca shows its own when the turn ends
        notified = json.loads((guard.folder("acme") / owner.SENT).read_text())
        self.assertEqual(notified, {"n": 1, "ask_at": iso(T0), "ask_last": iso(T0)})   # no gap: Orche showed none
        self.assertIn("owner ask project=acme kind=owner source=main\n", self.log())
        self.assertIn("owner recorded project=acme kind=owner by=orca\n", self.log())
        self.assertNotIn("owner notify", self.log())
        for name in ("events", "hooks"):
            self.assertNotIn("決済", self.log(name))
            self.assertNotIn("STRIPE", self.log(name))

    def test_done_closes_the_ask_and_does_not_notify(self):
        owner.closing("acme", "OWNER: pick a plan", now=T0)
        self.assertEqual(owner.closing("acme", "All landed.\nDONE: shipped v2 (3 PRs)", now=at(400)), "DONE")
        self.assertEqual(self.ask()["closed_at"], iso(at(400)))
        self.assertIsNone(owner.open_ask("acme"))
        self.assertEqual(self.notes(), [])
        done = json.loads((guard.folder("acme") / "done.json").read_text())
        self.assertEqual(done["text"], "DONE: shipped v2 (3 PRs)")
        self.assertEqual(json.loads((guard.folder("acme") / owner.SENT).read_text())["shown"], {"done": done["id"]})
        self.assertIn("owner closed project=acme\n", self.log())
        self.assertIn("owner recorded project=acme kind=done by=orca\n", self.log())
        owner.closing("acme", "DONE: again", now=at(800))             # no open ask: the first close stays
        self.assertEqual(self.ask()["closed_at"], iso(at(400)))

    def test_running_and_untagged_notify_nothing(self):
        self.assertEqual(owner.closing("acme", "RUNNING: build runs the coupon tests", now=T0), "RUNNING")
        self.assertIsNone(owner.closing("acme", "Working on it.", now=T0))
        self.assertEqual((self.notes(), self.ask()), ([], None))
        self.assertIn("owner untagged project=acme\n", self.log())

    def test_one_notification_per_project_per_300_s(self):
        owner.parked("acme", "acme-x-1", "first", now=T0)
        owner.parked("acme", "acme-x-1", "second", now=at(299))
        self.assertEqual(self.ask()["text"], "second")                   # recorded, not sent
        self.assertEqual(len(self.notes()), 1)
        self.assertIn("owner held project=acme kind=guard reason=gap\n", self.log())
        owner.parked("docs", "docs-x-1", "other project", now=at(10))   # per project
        owner.parked("acme", "acme-x-1", "third", now=at(300))
        self.assertEqual(self.notes(), [("acme", "guard"), ("docs", "guard"), ("acme", "guard")])

    def test_a_secret_ask_and_notifications_off(self):
        owner.closing("acme", "OWNER: secret STRIPE_KEY, GH_TOKEN", now=T0)
        self.assertEqual((self.ask()["kind"], self.notes()), ("secret", []))
        self.assertIn("owner recorded project=acme kind=secret by=orca\n", self.log())
        self.config({"notify": False})
        owner.parked("acme", "acme-x-1", "not started", now=T0)
        self.assertEqual(self.notes(), [])
        self.assertIn("owner held project=acme kind=guard reason=off\n", self.log())

    def test_nothing_rings_in_quiet_hours(self):
        """Orca's notification has a sound Orche cannot turn off: a ring due inside quiet hours is held (the
        heartbeat rings it at the first pass after them, test_owner_heartbeat.QuietHoursTest)."""
        h = T0.hour                                   # the clock passed, not the host's zone
        self.config({"flow": {"quiet_hours": f"{h:02d}:00-{(h + 1) % 24:02d}:00"}})
        owner.parked("acme", "acme-x-1", "approve", now=T0)
        self.assertEqual(self.notes(), [])
        self.assertEqual(self.ask()["text"], "approve")
        self.assertIn("owner held project=acme kind=guard reason=quiet\n", self.log())
        self.assertFalse((guard.folder("acme") / owner.SENT).exists())      # no try: the first is still due

    def test_a_parked_guard_refusal_records_an_ask_and_rings_mains_tab(self):
        """Orca cannot know a runner's refusal: Orche still rings it, the owner present or not; the text
        stays in ask.json, never in the ring."""
        self.owner_seen(10)
        text = f"not started: acme-x-20261023-1 ran 4 times since 10:00 without the owner (cap 4) {NON_ASCII}"
        owner.parked("acme", "acme-x-20261023-1", text, now=T0)
        self.assertEqual(self.ask(), {"text": owner.one_line(text, owner.ASK_MAX), "at": iso(T0), "kind": "guard",
                                      "source": "runner", "unit": "acme-x-20261023-1"})
        self.assertEqual(self.notes(), [("acme", "guard")])
        notified = json.loads((guard.folder("acme") / owner.SENT).read_text())
        self.assertEqual(notified, {"at": iso(T0), "n": 1, "ask_at": iso(T0), "ask_last": iso(T0), "kind": "guard"})
        self.assertIn("owner notify project=acme kind=guard n=1\n", self.log())

    def test_a_failed_ring_is_a_try(self):
        """No live Main terminal: the ring fails; the try counts (the next waits for its slot)."""
        with mock.patch.object(notify, "ring", lambda project, *, kind: self.rings.append((project, kind)) or False):
            self.assertFalse(owner.notify("acme", "guard", T0, ask_at=iso(T0)))
        self.assertEqual(self.notes(), [("acme", "guard")])
        self.assertEqual(json.loads((guard.folder("acme") / owner.SENT).read_text()),
                         {"n": 1, "ask_at": iso(T0), "ask_last": iso(T0)})
        self.assertIn("owner held project=acme kind=guard reason=failed\n", self.log())


class BellTest(support.TempHomeCase):
    """notify.ring: one BEL byte to the terminal Main's hooks recorded (inbox/<main-p>/.pane), only while `ps`
    still names it that pid's terminal; notify.terminal: the walk up to the first process with one. The ps
    call and the write are fakes (support.no_ps, no_bell by default): no test reads or rings a real terminal."""

    def setUp(self):
        super().setUp()
        from orche import registry
        registry.save(json.loads((support.FIXTURES / "registry.sample.json").read_text()))
        self.table, self.rung = {}, []
        for name, value in (("PS", lambda pid, timeout: self.table.get(pid)),
                            ("BELL", lambda path: self.rung.append(path))):
            patcher = mock.patch.object(notify, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def pane(self, rec):
        from orche import util
        util.write_json(inbox.box("main-acme-shop") / notify.PANE, rec)

    def events(self):
        path = self.home / "logs" / "events.log"
        return path.read_text() if path.exists() else ""

    def test_the_walk_stops_at_the_first_process_with_a_terminal(self):
        self.table = {300: (200, ""), 200: (100, ""), 100: (1, "/dev/ttys013"), 7: (1, "/dev/pts/3")}
        self.assertEqual(notify.terminal(300, now=T0), {"pid": 100, "tty": "/dev/ttys013", "at": T0.isoformat()})
        self.assertIsNone(notify.terminal(7))                     # a terminal, but not /dev/tty<name>
        self.assertIsNone(notify.terminal(55))                    # ps cannot say
        self.table = {i: (i + 1, "") for i in range(2, 20)}
        self.assertIsNone(notify.terminal(2))                     # none within UP processes
        self.assertIsNone(notify.terminal(1))
        seen = []
        with mock.patch.object(notify, "PS", lambda pid, timeout: seen.append(timeout) or (pid + 1, "")):
            notify.terminal(10)
        self.assertEqual(len(seen), notify.UP)
        self.assertTrue(all(0 < t <= notify.PS_S for t in seen))   # one shared budget for the walk

    def test_ps_reads_the_parent_and_the_terminal_of_one_pid(self):
        calls, out = [], {"code": 0, "out": " 4242 ttys013\n"}

        def runner(argv, *, timeout, input, env=None):
            calls.append((list(argv), timeout))
            return shell.Result(out["code"], out["out"], "", list(argv))
        with mock.patch.object(shell, "RUNNER", runner):
            self.assertEqual(notify._ps(77, 0.5), (4242, "/dev/ttys013"))
            out["out"] = "1 ??\n"
            self.assertEqual(notify._ps(77, 0.5), (1, ""))
            for code, text in ((1, ""), (0, "garbage"), (0, "")):
                out.update(code=code, out=text)
                self.assertIsNone(notify._ps(77, 0.5))
        self.assertEqual(calls[0], (["ps", "-o", "ppid=", "-o", "tty=", "-p", "77"], 0.5))

    def test_ring_writes_one_bel_to_the_recorded_live_terminal(self):
        self.pane({"pid": 4242, "tty": "/dev/ttys013", "at": T0.isoformat()})
        self.table = {4242: (1, "/dev/ttys013")}
        self.assertTrue(notify.ring("acme-shop", kind="owner"))
        self.assertEqual(self.rung, ["/dev/ttys013"])
        self.assertIn("notify kind=owner project=acme-shop ok=1\n", self.events())

    def test_ring_refuses_what_it_cannot_confirm(self):
        cases = {"no record": None,
                 "a dead pid": {"pid": 9, "tty": "/dev/ttys013"},
                 "another terminal now": {"pid": 4242, "tty": "/dev/ttys014"},
                 "not a tty path": {"pid": 4242, "tty": "/dev/disk0"},
                 "a path out of /dev": {"pid": 4242, "tty": "/dev/ttys013/../../tmp/x"},
                 "a pid that is not a number": {"pid": "4242", "tty": "/dev/ttys013"},
                 "a pid that is a bool": {"pid": True, "tty": "/dev/ttys013"}}
        self.table = {4242: (1, "/dev/ttys013"), 1: (0, "/dev/ttys013")}
        for name, rec in cases.items():
            with self.subTest(name):
                (inbox.box("main-acme-shop") / notify.PANE).unlink(missing_ok=True)
                if rec is not None:
                    self.pane(rec)
                self.assertFalse(notify.ring("acme-shop", kind="owner"))
        self.assertFalse(notify.ring("nope", kind="owner"))                       # no such project's Main
        self.assertEqual(self.rung, [])
        self.assertEqual(self.events().count("ok=0 reason=no-pane\n"), len(cases) + 1)

    def test_ring_is_off_with_notify_false_and_never_raises(self):
        self.pane({"pid": 4242, "tty": "/dev/ttys013"})
        self.table = {4242: (1, "/dev/ttys013")}
        (self.home / "config.json").write_text(json.dumps(support.no_wake({"notify": False})))
        self.assertFalse(notify.ring("acme-shop", kind="hr"))
        self.assertIn("notify kind=hr project=acme-shop ok=0 reason=off\n", self.events())
        (self.home / "config.json").write_text(json.dumps(support.no_wake({})))

        def broken(path):
            raise BlockingIOError(35, "Resource temporarily unavailable", path)
        with mock.patch.object(notify, "BELL", broken):
            self.assertFalse(notify.ring("acme-shop", kind="owner"))
        self.assertIn("notify kind=owner project=acme-shop ok=0 reason=error\n", self.events())
        hooks = (self.home / "logs" / "hooks.log").read_text()
        self.assertIn("notify owner: BlockingIOError", hooks)
        self.assertNotIn("ttys013", hooks)
        self.assertEqual(self.rung, [])

    def test_the_write_is_one_bel_byte_opened_without_taking_the_terminal(self):
        """On a plain file (a test never opens a terminal): one BEL byte, O_NOCTTY | O_NONBLOCK."""
        path = self.home / "not-a-tty"
        path.write_bytes(b"")
        opened = []
        real_open = os.open
        with mock.patch.object(os, "open", lambda p, flags, *a: opened.append(flags) or real_open(p, flags, *a)):
            notify._bell(str(path))
        self.assertEqual(path.read_bytes(), b"\a")
        self.assertEqual(opened, [os.O_WRONLY | os.O_NOCTTY | os.O_NONBLOCK])


class DefaultSeamTest(support.TempHomeCase):
    def test_no_test_reads_or_rings_a_terminal_by_default(self):
        """support's fakes stand in for the ps call and the write in every test (and its `ps` on PATH
        hides terminals from child processes): a recorded pane never rings."""
        from orche import registry, util
        self.assertEqual((notify.PS, notify.BELL), (support.no_ps, support.no_bell))
        registry.save(json.loads((support.FIXTURES / "registry.sample.json").read_text()))
        util.write_json(inbox.box("main-acme-shop") / notify.PANE, {"pid": os.getpid(), "tty": "/dev/ttys000"})
        self.assertIsNone(notify.terminal())
        self.assertFalse(notify.ring("acme-shop", kind="owner"))
        stub = support.no_claude_dir() + "/ps"
        res = shell.run([stub, "-o", "ppid=", "-o", "tty=", "-p", str(os.getpid())], timeout=5)
        self.assertEqual((res.code, res.out), (1, ""))
        self.assertTrue(shell.run([stub, "-o", "pid=", "-p", str(os.getpid())], timeout=5).ok)   # the real ps
