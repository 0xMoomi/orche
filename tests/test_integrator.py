"""The merge-queue integrator (0.9.0 T3): candidate, gate, landing, proof, rows, returns, drops, main's
health, pause, lost integrators and the driver — against a temp main checkout, a local bare origin and
a fake `gh` that merges into that origin as GitHub's merge button does (support.FAKE_GH)."""
import json
import os
import subprocess
import sys
import time
import unittest
from unittest import mock

import support
from orche import brief, dispatch, ledger, procs, queue, registry, roster, runs, shell, util
from orche.flow import guard
from orche.queue import gate, integrator, store
from test_ledger import make_registry
from test_queue import GIT, git

CI_SH = """#!/bin/sh
if [ -f src/slow ]; then sleep 5; fi
if grep -rqs RED src; then cat src/*.txt; echo "boom: RED found"; exit 3; fi
echo "verdict: unit-test-verified"
"""
TOKEN = "ghp_" + "Ab3d" * 9


class IntegratorCase(support.TempHomeCase):
    def setUp(self):
        super().setUp()
        wt = self.home / "wt"
        wt.mkdir()
        self.origin, self.acme = wt / "origin.git", wt / "acme"
        subprocess.run([*GIT, "init", "-q", "--bare", str(self.origin)], check=True)
        subprocess.run([*GIT, "init", "-q", "-b", "main", str(self.acme)], check=True)
        (self.acme / "scripts" / "verify").mkdir(parents=True)
        ci = self.acme / "scripts" / "verify" / "ci.sh"
        ci.write_text(CI_SH)
        ci.chmod(0o755)
        (self.acme / "src").mkdir()
        (self.acme / "src" / "app.txt").write_text("app\n")
        (self.acme / ".gitignore").write_text(".orche/\n")
        git(self.acme, "add", "-A")
        git(self.acme, "commit", "-q", "-m", "init")
        git(self.acme, "remote", "add", "origin", str(self.origin))
        git(self.acme, "push", "-q", "-u", "origin", "main")
        make_registry(wt)
        self.clone = self.home / "queue" / "acme" / "checkout"
        self.gh, self.prs = self.home / "fakegh", []
        self.spec()
        deliver = mock.patch.object(dispatch, "_deliver", return_value="runner-started")
        self.delivered = deliver.start()
        self.addCleanup(deliver.stop)

    # fixtures ----------------------------------------------------------------------------------
    def spec(self, **extra):
        support.fake_gh(self.gh, {"prs": self.prs, "origin": str(self.origin), **extra})

    def config(self, **keys):
        path = self.home / "config.json"
        cfg = json.loads(path.read_text())
        cfg.update(keys)
        path.write_text(json.dumps(cfg))

    def branch(self, name, files, message="work"):
        """A commit on `name` from origin/main, pushed; its sha."""
        git(self.acme, "fetch", "-q", "origin")
        git(self.acme, "checkout", "-q", "-B", name, "origin/main")
        for rel, text in files.items():
            if text is None:
                git(self.acme, "rm", "-q", rel)
                continue
            (self.acme / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.acme / rel).write_text(text)
        git(self.acme, "add", "-A")
        git(self.acme, "commit", "-q", "-m", message)
        git(self.acme, "push", "-q", "-f", "origin", name)
        sha = git(self.acme, "rev-parse", "HEAD")
        git(self.acme, "checkout", "-q", "main")
        return sha

    def on_main(self, files):
        """A commit landed on origin/main outside the queue."""
        sha = self.branch("outside", files, "outside the queue")
        git(self.acme, "push", "-q", "origin", f"{sha}:refs/heads/main")
        return sha

    def bump_main(self):
        tip = git(self.origin, "rev-parse", "main")
        new = git(self.origin, "commit-tree", tip + "^{tree}", "-p", tip, "-m", "moved")
        git(self.origin, "update-ref", "refs/heads/main", new, tip)

    def queue_pr(self, number, head, branch, *, unit=None, parent="", rev=1, first=False, owner_ok=True,
                 writer="feat-acme-checkout", surface="internal", **pr):
        self.prs.append({"number": number, "headRefName": branch, "baseRefName": "main", "headRefOid": head,
                         "isDraft": False, "state": "OPEN", "title": f"pr {number}", "reviewDecision": "APPROVED", **pr})
        self.spec()
        e = {"id": queue.entry_id(number, head), "pr": number, "head": head, "branch": branch, "unit": unit,
             "writer": writer, "parent": parent, "rev": rev, "verify": [],
             "approved_by": "div-acme-build (review)", "accept": None, "first": first, "owner_ok": owner_ok,
             "surface": surface, "at": "2026-10-24T00:00:00Z", "state": "waiting", "restarts": 0, "drop": False,
             "test": None}
        queue.add("acme", e, first=first, by="div-acme-build", base="main")
        return e

    def unit(self, to="feat-acme-checkout", sender="div-acme-build", **kw):
        fields = {"TO": to, "GOAL": "Add pay", "SCOPE": "src/**", "CONTEXT": "none", "ACCEPT": "works",
                  "VERIFY": "`scripts/verify/ci.sh`", "TIMEBOX": "20m", "FORBID": "merge", "REPORT": "status",
                  "TIER": "standard"}
        target = roster.Roster(registry.load()).member(to)
        return brief.write(brief.Block("brief", fields), target, sender, "1. x", timeout=0, **kw)

    def newest(self, unit):
        return brief.find(registry.load(), unit)

    def brief_text(self, unit):
        return (self.acme / brief.UNITS / unit / "BRIEF.md").read_text()

    def notes(self, session="div-acme-build"):
        d = self.home / "inbox" / session
        found = [json.loads(p.read_text()) for p in sorted(d.glob("*.json"))] if d.is_dir() else []
        return [p for p in found if p.get("via") == "queue"]

    def merges(self):
        path = self.gh / "merges.jsonl"
        return [json.loads(ln) for ln in path.read_text().splitlines()] if path.exists() else []

    def events(self, name=None):
        return [ln["event"] for ln in store.read_log("acme") if name is None or ln["event"] == name]

    def queue_log(self):
        path = self.home / "logs" / "queue.log"
        return path.read_text() if path.exists() else ""


class LandTest(IntegratorCase):
    def test_two_entries_land_in_order_second_candidate_contains_first(self):
        h1, h2 = self.branch("feat/a", {"src/a.txt": "a\n"}), self.branch("feat/b", {"src/b.txt": "b\n"})
        self.queue_pr(1, h1, "feat/a")
        self.queue_pr(2, h2, "feat/b")
        res = integrator.loop("acme")
        self.assertEqual(res["event"], "idle", self.queue_log())
        m = self.merges()
        self.assertEqual([x["argv"] for x in m], [["pr", "merge", "1", "--squash", "--match-head-commit", h1],
                                                  ["pr", "merge", "2", "--squash", "--match-head-commit", h2]])
        self.assertEqual({x["cwd"] for x in m}, {str(self.clone.resolve())})     # gh ran in the queue's clone
        m1, m2 = m[0]["merge"], m[1]["merge"]
        self.assertEqual(git(self.clone, "rev-parse", m[1]["clone_head"] + "^1"), m1)   # PR 2 tested on top of PR 1
        self.assertEqual(git(self.origin, "rev-parse", "main"), m2)
        q = store.read("acme")
        self.assertEqual((q["entries"], q["green_main"]), ([], m2))
        rows = {(r["pr"], r["sha"], r["verdict"], r["verifier"]) for r in ledger.read("acme")}
        for row in (("1", h1), ("none", m1), ("2", h2), ("none", m2)):
            self.assertIn((*row, "unit-test-verified", "queue"), rows)
        with self.assertRaises(ledger.WriterIsJudge):
            ledger.make_row("1", h1, "type-check-only", "x", "queue", producer="queue")
        merged = sorted(p["summary"] for p in self.notes() if p["summary"].startswith("merged"))
        self.assertTrue(merged[0].startswith(f"merged PR #1 @{h1[:7]} as {m1[:7]} (scripts/verify/ci.sh "), merged)
        self.assertTrue(all(not p["urgent"] and p["status"] == "info" for p in self.notes()))
        self.assertEqual(self.events("merged"), ["merged", "merged"])
        self.assertFalse(any((self.home / "inbox").glob("feat-*")))     # never a pointer to a feature

    def test_skip_ci_in_a_commit_message_changes_nothing(self):
        h = self.branch("feat/a", {"src/a.txt": "a\n"}, message="[skip ci] tweak")
        self.queue_pr(1, h, "feat/a")
        self.assertEqual(integrator.loop("acme", 1)["event"], "merged")
        log = next((self.home / "queue" / "acme" / "logs").glob("pr1-*.1.log")).read_text()
        self.assertIn("$ scripts/verify/ci.sh", log)

    def test_owner_entries_wait_untested_then_land_after_approve(self):
        self.config(queue={"land": "owner"})
        h = self.branch("feat/a", {"src/a.txt": "a\n"})
        self.queue_pr(1, h, "feat/a", owner_ok=False)
        res = integrator.loop("acme", 1)
        self.assertEqual((res["code"], res["event"]), (0, "idle"))
        self.assertFalse((self.gh / "gh-calls.jsonl").exists())
        self.assertEqual(store.read("acme")["entries"][0]["state"], "waiting")
        queue.approve("acme", None, by="main-acme")
        self.assertEqual(integrator.loop("acme")["event"], "idle")
        self.assertEqual(self.events("merged"), ["merged"])

    def test_all_children_landed_tells_build_once(self):
        bu = self.unit(to="div-acme-build", sender="main-acme")
        c1 = self.unit(lineage=bu.lineage, parent=bu.unit)
        c2 = self.unit(lineage=bu.lineage, parent=bu.unit)
        h1, h2 = self.branch("feat/a", {"src/a.txt": "a\n"}), self.branch("feat/b", {"src/b.txt": "b\n"})
        self.queue_pr(1, h1, "feat/a", unit=c1.unit, parent=bu.unit)
        self.queue_pr(2, h2, "feat/b", unit=c2.unit, parent=bu.unit)
        integrator.loop("acme")
        urgent = [p["summary"] for p in self.notes() if p["urgent"]]
        self.assertEqual(urgent, [f"all 2 units of {bu.unit} landed: PR #1, #2 merged — report your unit to Main"])


    def test_a_division_child_that_reported_done_counts_as_landed(self):
        """A child that is another head's unit has no PR by design: its done report lands it."""
        from orche import inbox
        from test_buildreview import DESIGN, add_design
        reg = registry.load()
        add_design(reg, self.home / "wt")
        registry.save(reg)
        bu = self.unit(to="div-acme-build", sender="main-acme")
        cd = self.unit(to=DESIGN, lineage=bu.lineage, parent=bu.unit)
        c1 = self.unit(lineage=bu.lineage, parent=bu.unit)
        c2 = self.unit(lineage=bu.lineage, parent=bu.unit)
        self.queue_pr(1, self.branch("feat/a", {"src/a.txt": "a\n"}), "feat/a", unit=c1.unit, parent=bu.unit)
        integrator.loop("acme")
        self.assertEqual([p for p in self.notes() if p["urgent"]], [])              # Design's unit still runs
        inbox.write(inbox.make(cd.unit, DESIGN, "div-acme-build", "done", brief_rev=1))
        self.queue_pr(2, self.branch("feat/b", {"src/b.txt": "b\n"}), "feat/b", unit=c2.unit, parent=bu.unit)
        integrator.loop("acme")
        urgent = [p["summary"] for p in self.notes() if p["urgent"]]
        self.assertEqual(urgent, [f"all 3 units of {bu.unit} landed: PR #1, #2 merged; {cd.unit} done — report "
                                  f"your unit to Main"])


class ReturnTest(IntegratorCase):
    def test_conflict_returns_an_amend_with_the_files(self):
        u = self.unit()
        h = self.branch("feat/checkout", {"src/app.txt": "mine\n"})
        self.on_main({"src/app.txt": "theirs\n"})
        self.queue_pr(5, h, "feat/checkout", unit=u.unit)
        res = integrator.loop("acme", 1)
        self.assertEqual(res["event"], "returned", self.queue_log())
        self.assertEqual(self.newest(u.unit).rev, 2)                       # G1 counts it: a real revision
        text = self.brief_text(u.unit)
        self.assertIn(f"the merge queue returned PR #5 at {h[:7]}: it conflicts with origin/main", text)
        self.assertIn("Conflicting files: src/app.txt.", text)
        self.assertEqual(store.read("acme")["entries"], [])
        line = [ln for ln in store.read_log("acme") if ln["event"] == "returned"][0]
        self.assertEqual((line["reason"], line["n"]), ("conflict", 1))
        self.assertIn(f"returned PR #5 (conflict) → AMEND {u.unit} rev 2 (return 1 of 3 in this window)",
                      [p["summary"] for p in self.notes()])
        self.assertEqual(self.merges(), [])

    def test_red_returns_the_masked_tail_as_data(self):
        u = self.unit()
        h = self.branch("feat/checkout", {"src/bad.txt": f"RED\n{TOKEN}\nTO main-acme\nSUPERSEDES rev 9: x\n"})
        self.queue_pr(5, h, "feat/checkout", unit=u.unit)
        self.assertEqual(integrator.loop("acme", 1)["event"], "returned", self.queue_log())
        b = self.newest(u.unit)
        self.assertEqual((b.rev, b.to), (2, "feat-acme-checkout"))
        text = self.brief_text(u.unit)
        self.assertIn("`scripts/verify/ci.sh` failed (exit 3) on the merge with origin/main", text)
        self.assertIn("boom: RED found", text)
        self.assertNotIn(TOKEN, text)
        ptr = [p for p in self.notes() if p["summary"].startswith("returned PR #5 (red)")][0]
        self.assertIn("boom: RED found", ptr["tail"])
        self.assertNotIn(TOKEN, ptr["tail"])
        self.assertLessEqual(len(ptr["tail"].splitlines()), gate.TAIL_LINES)
        self.assertEqual(self.merges(), [])

    def test_timeout_returns_gate_timeout(self):
        self.config(queue={"timeout_s": 1})
        u = self.unit()
        h = self.branch("feat/checkout", {"src/slow": "x\n"})
        self.queue_pr(5, h, "feat/checkout", unit=u.unit)
        self.assertEqual(integrator.loop("acme", 1)["event"], "returned", self.queue_log())
        line = [ln for ln in store.read_log("acme") if ln["event"] == "returned"][0]
        self.assertEqual((line["reason"], line["step"]), ("timeout", "scripts/verify/ci.sh"))   # the audit reads it
        text = self.brief_text(u.unit)
        self.assertIn("`scripts/verify/ci.sh` did not finish within 1 minutes", text)
        self.assertIn("Make it finish within the gate's time", text)

    def test_third_return_in_the_window_sends_no_amend_and_blocks_build(self):
        u = self.unit()
        for _ in range(2):
            store.log("acme", "returned", {"id": "pr5-old", "pr": 5, "unit": u.unit}, reason="red")
        h = self.branch("feat/checkout", {"src/bad.txt": "RED\n"})
        self.queue_pr(5, h, "feat/checkout", unit=u.unit)
        integrator.loop("acme", 1)
        self.assertEqual(self.newest(u.unit).rev, 1)
        blocked = [p for p in self.notes() if p["status"] == "blocked"]
        self.assertEqual(len(blocked), 1)
        self.assertTrue(blocked[0]["summary"].startswith("PR #5 returned 3 times in this window (red, red, red): "
                                                         "no AMEND sent. Re-plan it"))

    def test_an_amend_refused_by_g8_reaches_build_as_failed(self):
        u = self.unit()
        h = self.branch("feat/checkout", {"src/bad.txt": "RED\n"})
        self.queue_pr(5, h, "feat/checkout", unit=u.unit)
        guard.pause("acme", "main-acme", "owner")
        with mock.patch.object(integrator, "_flow_paused", return_value=None):   # the gate began before the pause
            integrator.loop("acme", 1)
        self.assertEqual(self.newest(u.unit).rev, 1)
        failed = [p["summary"] for p in self.notes() if p["status"] == "failed"]
        self.assertEqual(len(failed), 1, self.notes())
        self.assertTrue(failed[0].startswith(f"the queue could not return PR #5 to {u.unit}: "), failed)

    def test_a_pr_entry_returns_to_build_as_failed(self):
        h = self.branch("feat/a", {"src/bad.txt": "RED\n"})
        self.queue_pr(3, h, "feat/a")
        integrator.loop("acme", 1)
        failed = [p["summary"] for p in self.notes() if p["status"] == "failed"]
        self.assertEqual(failed, ["returned PR #3 (red): `scripts/verify/ci.sh` failed (exit 3)"])


class LiveTest(IntegratorCase):
    """R-REVIEW-12: an entry the head review found user-visible lands only on the check script's live-ui-verified."""

    LIVE_CI = CI_SH.replace("unit-test-verified", "live-ui-verified")

    def test_a_user_visible_change_with_a_unit_test_verdict_returns_to_its_worker(self):
        u = self.unit()
        h = self.branch("feat/checkout", {"src/pay.txt": "pay\n"})
        self.queue_pr(5, h, "feat/checkout", unit=u.unit, surface="user")
        self.assertEqual(integrator.loop("acme", 1)["event"], "returned", self.queue_log())
        reason = "user-visible change: live verification required (the check script's verdict was unit-test-verified)"
        b = self.newest(u.unit)
        self.assertEqual((b.rev, b.to), (2, "feat-acme-checkout"))
        text = self.brief_text(u.unit)
        self.assertIn(f"the merge queue returned PR #5 at {h[:7]}: {reason}", text)
        self.assertIn("ends with `verdict: live-ui-verified`", text)
        line = [ln for ln in store.read_log("acme") if ln["event"] == "returned"][0]
        self.assertEqual(line["reason"], "live")
        self.assertIn(f"returned PR #5 (live) → AMEND {u.unit} rev 2 (return 1 of 3 in this window)",
                      [p["summary"] for p in self.notes()])
        self.assertEqual(self.merges(), [])
        self.assertFalse(any(r["sha"] == h for r in ledger.read("acme")))

    def test_a_user_visible_pr_entry_returns_to_build_with_the_reason(self):
        h = self.branch("feat/a", {"src/a.txt": "a\n"})
        self.queue_pr(3, h, "feat/a", surface="user")
        integrator.loop("acme", 1)
        failed = [p["summary"] for p in self.notes() if p["status"] == "failed"]
        self.assertEqual(failed, ["returned PR #3 (live): user-visible change: live verification required (the check "
                                  "script's verdict was unit-test-verified)"])

    def test_a_user_visible_change_with_a_live_verdict_lands(self):
        h = self.branch("feat/a", {"src/a.txt": "a\n", "scripts/verify/ci.sh": self.LIVE_CI})
        self.queue_pr(1, h, "feat/a", surface="user")
        self.assertEqual(integrator.loop("acme", 1)["event"], "merged", self.queue_log())
        rows = {(r["pr"], r["sha"], r["verdict"], r["verifier"]) for r in ledger.read("acme")}
        self.assertIn(("1", h, "live-ui-verified", "queue"), rows)

    def test_an_internal_change_with_a_unit_test_verdict_lands(self):
        h = self.branch("feat/a", {"src/a.txt": "a\n"})
        self.queue_pr(1, h, "feat/a", surface="internal")
        self.assertEqual(integrator.loop("acme", 1)["event"], "merged", self.queue_log())

    def test_without_a_check_script_there_is_no_level_to_hold_it_to(self):
        e = {"pr": 1, "head": "a" * 40, "id": "pr1-aaaaaaa", "surface": "user"}
        p = integrator.Pass("acme", e, {"base": "main", "green_main": None, "closed": None})
        self.assertFalse(p.not_live({"gate": "scripts/verify/unit.sh", "verdict": "type-check-only"}))
        self.assertTrue(p.not_live({"gate": "scripts/verify/ci.sh + scripts/verify/unit.sh",
                                    "verdict": "type-check-only"}))
        self.assertFalse(p.not_live({"gate": "scripts/verify/ci.sh", "verdict": "live-ui-verified"}))
        p.entry = {**e, "surface": "internal"}
        self.assertFalse(p.not_live({"gate": "scripts/verify/ci.sh", "verdict": "unit-test-verified"}))

    def test_a_team_project_skips_the_live_rule(self):
        """R-TEAM-2: the team's finish step showed the change on the issue before Main said ready."""
        reg = registry.load()
        reg["projects"]["acme"]["team"] = "team-os@acme"
        registry.save(reg)
        e = {"pr": 1, "head": "a" * 40, "id": "pr1-aaaaaaa", "surface": "user"}
        p = integrator.Pass("acme", e, {"base": "main", "green_main": None, "closed": None})
        self.assertFalse(p.not_live({"gate": "scripts/verify/ci.sh", "verdict": "unit-test-verified"}))


class TeamsHeadTest(IntegratorCase):
    """T2: the queue's notes and AMENDs go to the worker's head (here Design), a --pr entry still to Build."""

    def setUp(self):
        super().setUp()
        from test_buildreview import DESIGN, add_design
        self.design = DESIGN
        reg = registry.load()
        add_design(reg, self.home / "wt")
        divs = reg["projects"]["acme"]["divisions"]
        divs["design"]["features"] += divs["build"].pop("features")      # checkout now works for Design
        registry.save(reg)

    def test_a_red_design_worker_is_returned_by_design(self):
        u = self.unit(sender=self.design)
        h = self.branch("feat/checkout", {"src/bad.txt": "RED\n"})
        self.queue_pr(5, h, "feat/checkout", unit=u.unit)
        self.assertEqual(integrator.loop("acme", 1)["event"], "returned", self.queue_log())
        b = self.newest(u.unit)
        self.assertEqual((b.rev, b.to, b.sender), (2, "feat-acme-checkout", self.design))
        self.assertTrue([p for p in self.notes(self.design) if p["summary"].startswith("returned PR #5 (red)")])
        self.assertEqual(self.notes(), [])

    def test_all_landed_tells_design(self):
        bu = self.unit(to=self.design, sender="main-acme")
        c1 = self.unit(sender=self.design, lineage=bu.lineage, parent=bu.unit)
        h1 = self.branch("feat/a", {"src/a.txt": "a\n"})
        self.queue_pr(1, h1, "feat/a", unit=c1.unit, parent=bu.unit)
        integrator.loop("acme")
        urgent = [p["summary"] for p in self.notes(self.design) if p["urgent"]]
        self.assertEqual(urgent, [f"all 1 units of {bu.unit} landed: PR #1 merged — report your unit to Main"])
        self.assertEqual([p for p in self.notes() if p["urgent"]], [])

    def test_a_pr_entry_without_a_worker_still_goes_to_build(self):
        h = self.branch("feat/a", {"src/bad.txt": "RED\n"})
        self.queue_pr(3, h, "feat/a", writer="")
        integrator.loop("acme", 1)
        self.assertEqual([p["summary"] for p in self.notes() if p["status"] == "failed"],
                         ["returned PR #3 (red): `scripts/verify/ci.sh` failed (exit 3)"])
        self.assertEqual(self.notes(self.design), [])

    def test_a_queue_wide_pause_stays_with_build(self):
        """A pause is traffic control: Build and Main get it, not the worker's head (0.9.0 R-QUEUE-9)."""
        h = self.branch("feat/checkout", {"src/a.txt": "a\n"})
        self.queue_pr(1, h, "feat/checkout", unit=self.unit(sender=self.design).unit)
        self.spec(fail=StopsTest.LOGGED_OUT)
        for _ in range(3):
            integrator.loop("acme", 1)
        self.assertEqual(store.read("acme")["paused"]["by"], "stops")
        self.assertTrue([p for p in self.notes() if p["urgent"] and p["summary"].startswith("the merge queue")])
        self.assertTrue([p for p in self.notes("main-acme") if p["urgent"] and "gh auth login" in p["summary"]])
        self.assertEqual([p for p in self.notes(self.design) if "paused" in p["summary"]], [])


class DropTest(IntegratorCase):
    def test_moved_head_draft_and_changes_requested_drop(self):
        h1, h2 = self.branch("feat/a", {"src/a.txt": "a\n"}), self.branch("feat/b", {"src/b.txt": "b\n"})
        h3 = self.branch("feat/c", {"src/c.txt": "c\n"})
        e1 = self.queue_pr(1, h1, "feat/a")
        self.queue_pr(2, h2, "feat/b", reviewDecision="CHANGES_REQUESTED")
        self.queue_pr(3, h3, "feat/c", isDraft=True)
        self.prs[0]["headRefOid"] = "b" * 40
        self.spec()
        integrator.loop("acme")
        self.assertEqual([(ln["pr"], ln["reason"]) for ln in store.read_log("acme") if ln["event"] == "dropped"],
                         [(1, "head-moved"), (2, "changes-requested"), (3, "draft")])
        urgency = {p["pr"]: p["urgent"] for p in self.notes()}
        self.assertEqual(urgency, {1: False, 2: True, 3: True})
        self.assertEqual(self.merges(), [])
        self.assertEqual(e1["head"], h1)


class LandRefusalTest(IntegratorCase):
    def test_base_moved_during_the_gate_restarts_then_pauses(self):
        h = self.branch("feat/a", {"src/a.txt": "a\n"})
        self.queue_pr(1, h, "feat/a")
        real = gate.run

        def moving(where, steps, **kw):
            out = real(where, steps, **kw)
            if subprocess.run(["git", "-C", str(where), "rev-parse", "-q", "--verify", "HEAD^2"],
                              capture_output=True).returncode == 0:
                self.bump_main()                                   # only during a candidate's gate
            return out

        with mock.patch.object(gate, "run", side_effect=moving):
            res = integrator.loop("acme")
        self.assertEqual((res["code"], res["event"]), (3, "paused"))
        q = store.read("acme")
        self.assertEqual((q["paused"]["by"], q["entries"][0]["restarts"], q["entries"][0]["state"]),
                         ("restarts", 3, "waiting"))
        self.assertEqual(self.events("restarted"), ["restarted"] * 3)
        self.assertEqual(self.merges(), [])
        self.assertTrue(any(p["urgent"] and "keeps moving" in p["summary"] for p in self.notes()))

    def test_github_refusal_pauses_with_the_owner_step(self):
        h = self.branch("feat/a", {"src/a.txt": "a\n"})
        self.queue_pr(1, h, "feat/a")
        self.spec(merge_fail="GraphQL: Base branch policy prohibits the merge. (mergePullRequest)")
        res = integrator.loop("acme")
        self.assertEqual((res["code"], res["event"]), (3, "paused"))
        q = store.read("acme")
        self.assertEqual((q["paused"]["by"], q["entries"][0]["state"]), ("github", "waiting"))
        failed = [p["summary"] for p in self.notes() if p["status"] == "failed"]
        self.assertEqual(len(failed), 1)
        self.assertIn("GitHub refused to merge PR #1: GraphQL: Base branch policy prohibits the merge.", failed[0])
        self.assertIn("OWNER: allow merges without required reviews or checks on", failed[0])
        self.assertTrue([p for p in self.notes("main-acme") if p["urgent"]])

    def test_proof_mismatch_closes_the_queue(self):
        h = self.branch("feat/a", {"src/a.txt": "a\n"})
        self.queue_pr(1, h, "feat/a")
        self.spec(merge_other=True)
        integrator.loop("acme", 1)
        q = store.read("acme")
        self.assertEqual(q["closed"]["reason"], "proof")
        self.assertEqual(q["closed"]["main"], git(self.origin, "rev-parse", "main"))
        self.assertFalse([r for r in ledger.read("acme") if r["sha"] == h])
        self.assertTrue(any(p["status"] == "failed" and "proof failed" in p["summary"] for p in self.notes()))
        self.assertTrue(self.notes("main-acme"))


class HealthTest(IntegratorCase):
    def test_red_main_closes_the_queue_and_a_first_entry_reopens_it(self):
        bad = self.on_main({"src/red.txt": "RED\n"})
        h = self.branch("feat/a", {"src/a.txt": "a\n"})
        self.queue_pr(1, h, "feat/a")
        res = integrator.loop("acme")
        self.assertEqual((res["code"], res["event"]), (3, "closed"))
        q = store.read("acme")
        self.assertEqual((q["closed"]["reason"], q["closed"]["main"]), ("main-red", bad))
        self.assertEqual((q["entries"][0]["state"], q["entries"][0]["restarts"]), ("waiting", 0))
        failed = [p["summary"] for p in self.notes() if p["status"] == "failed"]
        self.assertTrue(failed[0].startswith(f"merge queue closed: origin/main {bad[:7]} fails scripts/verify/ci.sh"))
        self.assertTrue([p for p in self.notes("main-acme") if p["urgent"]])
        self.assertEqual(integrator.loop("acme", 1)["code"], 3)
        fix = self.branch("feat/fix", {"src/red.txt": None})
        self.queue_pr(2, fix, "feat/fix", first=True)
        integrator.loop("acme")
        self.assertEqual([x["argv"][2] for x in self.merges()], ["2", "1"])
        self.assertIsNone(store.read("acme")["closed"])
        self.assertIn("opened", self.events())


class PauseLostTest(IntegratorCase):
    def test_pause_and_flow_pause_stop_new_tests(self):
        h = self.branch("feat/a", {"src/a.txt": "a\n"})
        self.queue_pr(1, h, "feat/a")
        queue.pause("acme", by="div-acme-build")
        self.assertEqual(integrator.loop("acme", 1)["code"], 3)
        queue.resume("acme", by="div-acme-build")
        guard.pause("acme", "main-acme", "owner")
        self.assertEqual(integrator.loop("acme", 1)["event"], "flow-paused")
        self.assertFalse((self.gh / "gh-calls.jsonl").exists())
        self.assertEqual(store.read("acme")["entries"][0]["state"], "waiting")

    def dead_pid(self):
        p = subprocess.Popen(["true"])
        p.wait()
        self.lost_pid = p.pid
        return p.pid

    def test_a_lost_integrator_entry_goes_back_to_the_front_and_lands(self):
        h = self.branch("feat/a", {"src/a.txt": "a\n"})
        e = self.queue_pr(1, h, "feat/a")
        queue.mark("acme", e["id"], state="testing", test={"pid": self.dead_pid(), "started": "x", "pgid": None})
        integrator.loop("acme")
        self.assertEqual(self.events(), ["added", "lost", "testing", "merged"])
        self.assertEqual([ln.get("reason") for ln in store.read_log("acme") if ln["event"] == "lost"],
                         [f"pid {self.lost_pid}"])

    def test_lost_past_max_restarts_pauses(self):
        h = self.branch("feat/a", {"src/a.txt": "a\n"})
        e = self.queue_pr(1, h, "feat/a")
        queue.mark("acme", e["id"], state="testing", restarts=2, test={"pid": self.dead_pid(), "pgid": None})
        res = integrator.loop("acme", 1)
        self.assertEqual((res["code"], res["event"]), (3, "paused"))
        q = store.read("acme")
        self.assertEqual((q["paused"]["by"], q["entries"][0]["restarts"]), ("restarts", 3))
        self.assertTrue(any(p["urgent"] for p in self.notes()))

    def test_start_spawns_only_when_an_entry_can_be_taken(self):
        with mock.patch.object(procs, "spawn_detached") as spawn:
            self.assertFalse(queue.start("acme"))
            e = self.queue_pr(1, "a" * 40, "feat/a", owner_ok=False)
            self.assertFalse(queue.start("acme"))
            queue.approve("acme", [e["id"]], by="main-acme")
            with mock.patch.object(runs, "in_sandbox", return_value=True):
                self.assertFalse(queue.start("acme"))
            self.assertEqual(spawn.call_count, 0)
            self.assertTrue(queue.start("acme"))
            self.assertEqual(spawn.call_args[0][0][-3:], ["queue", "run", "acme"])
            with integrator.locked("acme") as held:
                self.assertTrue(held)
                self.assertTrue(queue.running("acme"))
                self.assertTrue(queue.start("acme"))
                self.assertEqual(integrator.loop("acme", 1)["event"], "busy")
            self.assertEqual(spawn.call_count, 1)
            self.assertFalse(queue.running("acme"))


def _wait(cond, timeout=8.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.05)
    return bool(cond())


class LostGateTest(IntegratorCase):
    """A lost take stops only its own gate's processes; a live pid waits lost_after_s."""

    def proc(self, argv, **kw):
        p = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True, **kw)
        self.addCleanup(lambda: (p.poll() is None and p.kill(), p.wait()))
        return p

    def execd(self, p, needle):
        self.assertTrue(_wait(lambda: needle in subprocess.run(["ps", "-ww", "-o", "command=", "-p", str(p.pid)],
                                                               capture_output=True, text=True, errors="replace").stdout))

    def lose(self, **test):
        """PR 1 left `testing` by a dead integrator, with `test`'s fields."""
        if not store.read("acme")["entries"]:
            self.queue_pr(1, self.branch("feat/a", {"src/a.txt": "a\n"}), "feat/a")
        dead = subprocess.Popen(["true"])
        dead.wait()
        queue.mark("acme", "#1", state="testing", test={"pid": dead.pid, "started": "2026-10-20T01:00:00+00:00", **test})

    def test_lost_pids_take_the_tag_and_the_verified_step_only(self):
        P = procs.Proc
        t = {"tag": "T1", "pgid": 500, "pgid_cmd": "./scripts/verify/ci.sh", "pgid_at": 1000.0}
        rows = [P(500, 1, 500, "/bin/sh ./scripts/verify/ci.sh", "", 1001),       # the step; env hidden (macOS)
                P(501, 500, 500, "sleep 9", "", 1002),                            # in its group
                P(600, 1, 600, "python x", f"{gate.TAG}=T1 PATH=/bin", 1500),     # tagged, left the group
                P(601, 600, 601, "node y", None, 1501),                           # its child, own group
                P(700, 1, 700, "/bin/zsh -l", "", 1000),                          # someone else's
                P(701, 700, 700, "vim", f"{gate.TAG}=T10", 1000)]                 # another tag
        self.assertEqual(gate.lost_pids(rows, t), {500, 501, 600, 601})
        for bad in ({"pgid_at": 1000.0 + 86400}, {"pgid_cmd": "./other.sh"}, {"pgid_at": None}, {"pgid": 700}):
            self.assertEqual(gate.lost_pids(rows, {**t, **bad, "tag": None}), set(), bad)
        other = [P(500, 1, 500, "/bin/sh ./scripts/verify/ci.sh", f"{gate.TAG}=T2", 1001)]
        self.assertEqual(gate.lost_pids(other, t), set())                        # a readable env must carry it

    def test_recovery_never_signals_a_recorded_pgid_that_is_not_its_gate(self):
        victim = self.proc(["sleep", "300"])                     # an unrelated group leader holding that pgid
        self.execd(victim, "sleep 300")
        self.lose(tag="pr1-x@1", pgid=victim.pid, pgid_cmd="./scripts/verify/ci.sh", pgid_at=time.time() - 86400)
        self.assertEqual([e["pr"] for e in integrator.recover("acme")], [1])           # recorded before a reboot
        self.lose(tag="pr1-x@2", pgid=victim.pid, pgid_cmd="./scripts/verify/ci.sh", pgid_at=time.time())
        self.assertEqual([e["pr"] for e in integrator.recover("acme")], [1])           # not its command
        self.lose(pgid=victim.pid)                                                      # a bare pgid (0.9.0 record)
        self.assertEqual([e["pr"] for e in integrator.recover("acme")], [1])
        time.sleep(0.3)
        self.assertIsNone(victim.poll())
        self.assertEqual(self.events("lost"), ["lost"] * 3)

    def test_recovery_stops_what_the_lost_gate_left_running(self):
        tag = f"pr1-abcdef0@{time.time_ns()}"                   # unique: a parallel suite's sweep never meets it
        step = self.home / "step.sh"
        step.write_text("#!/bin/sh\nsleep 300\n")
        step.chmod(0o755)
        lead = self.proc(["./step.sh"], cwd=str(self.home), env=gate.env(1, tag))
        at = time.time()
        loose = self.proc([sys.executable, "-c", "import time; time.sleep(300)"], env=gate.env(1, tag))
        self.execd(lead, "step.sh")
        self.execd(loose, "time.sleep(300)")
        self.lose(tag=tag, pgid=lead.pid, pgid_cmd="./step.sh", pgid_at=at)
        integrator.recover("acme")
        self.assertTrue(_wait(lambda: lead.poll() is not None and loose.poll() is not None), self.queue_log())

        def group_gone():
            try:
                os.killpg(lead.pid, 0)
            except ProcessLookupError:
                return True
            return False
        if not _wait(group_gone):                                # the step's own child too
            left = subprocess.run(["ps", "-o", "pid=,ppid=,pgid=,stat=,command=", "-g", str(lead.pid)],
                                  capture_output=True, text=True).stdout
            self.fail(f"group {lead.pid} still has: {left!r}\n{self.queue_log()}")

    def test_a_live_pid_is_lost_only_after_lost_after_s(self):
        self.queue_pr(1, self.branch("feat/a", {"src/a.txt": "a\n"}), "feat/a")
        queue.mark("acme", "#1", state="testing", test={"pid": os.getppid(), "started": util.now_iso(), "pgid": None})
        self.assertEqual(integrator.recover("acme"), [])
        self.assertEqual(store.read("acme")["entries"][0]["state"], "testing")
        self.config(queue={"lost_after_s": 0})
        self.assertEqual([e["pr"] for e in integrator.recover("acme")], [1])
        self.assertEqual(store.read("acme")["entries"][0]["state"], "waiting")


class StopsTest(IntegratorCase):
    """Git/gh failures in a row pause the queue with a notice; a pass that gets through clears them."""
    LOGGED_OUT = "To get started with GitHub CLI, please run:  gh auth login"

    def test_gh_logged_out_three_passes_pause_with_a_notice(self):
        h = self.branch("feat/a", {"src/a.txt": "a\n"})
        self.queue_pr(1, h, "feat/a")
        self.spec(fail=self.LOGGED_OUT)
        for n in (1, 2):
            self.assertEqual(integrator.loop("acme", 1)["event"], "error")
            self.assertEqual((store.read("acme")["stops"], store.read("acme")["paused"]), (n, None))
        self.assertIn("the queue paused", integrator.loop("acme", 1)["line"])
        q = store.read("acme")
        self.assertEqual((q["paused"]["by"], q["stops"], q["entries"][0]["state"]), ("stops", 3, "waiting"))
        self.assertIn("gh auth login", q["paused"]["reason"])
        failed = [p["summary"] for p in self.notes() if p["urgent"] and p["status"] == "failed"]
        self.assertEqual(len(failed), 1, self.notes())
        self.assertTrue(failed[0].startswith("the merge queue of acme paused: 3 passes in a row stopped (PR #1): gh: "
                                             "To get started"), failed)
        self.assertTrue([p for p in self.notes("main-acme") if p["urgent"] and "gh auth login" in p["summary"]])
        self.assertIn("queue paused project=acme by=stops", (self.home / "logs" / "events.log").read_text())
        self.assertEqual(integrator.loop("acme", 1)["code"], 3)                     # paused: nothing taken
        self.spec()
        queue.resume("acme", by="div-acme-build")
        self.assertEqual(integrator.loop("acme")["event"], "idle")
        self.assertEqual((self.events("merged"), store.read("acme")["stops"]), (["merged"], 0))

    def test_a_pass_that_gets_through_clears_the_count(self):
        h = self.branch("feat/a", {"src/a.txt": "a\n"})
        self.queue_pr(1, h, "feat/a")
        self.spec(fail=self.LOGGED_OUT)
        integrator.loop("acme", 1)
        integrator.loop("acme", 1)
        self.spec()
        self.assertEqual(integrator.loop("acme", 1)["event"], "merged")
        q = store.read("acme")
        self.assertEqual((q["stops"], q["paused"]), (0, None))


class StaleLockTest(IntegratorCase):
    """Smoke s13: a git killed mid-write (a lost integrator, a timeout) leaves its lock in the clone; the next
    pass removes it instead of stopping on it pass after pass until the queue pauses."""

    def test_a_lock_left_by_a_killed_git_is_removed_and_the_pr_lands(self):
        self.branch("feat/a", {"src/a.txt": "a\n"})
        integrator.checkout("acme")
        git(self.clone, "fetch", "-q", "origin")                  # the clone knows feat/a's old head
        h = self.branch("feat/a", {"src/a.txt": "a2\n"})          # the PR's head, not fetched yet
        ref = self.clone / ".git" / "refs" / "remotes" / "origin" / "feat" / "a.lock"
        ref.parent.mkdir(parents=True, exist_ok=True)
        ref.write_text(h + "\n")                                  # what a fetch SIGKILLed mid-update leaves
        index = self.clone / ".git" / "index.lock"
        index.write_text("")                                      # and a checkout killed mid-write
        old = time.time() - 120
        for p in (ref, index):
            os.utime(p, (old, old))
        self.queue_pr(1, h, "feat/a")
        with mock.patch.object(integrator, "_unlock"):            # kept: every fetch fails on it
            res = integrator.loop("acme", 1)
        self.assertEqual(res["event"], "error", res)
        self.assertIn("git fetch origin failed: error: ", res["line"])   # git's error, not its `From <url>`
        res = integrator.loop("acme", 1)
        self.assertEqual(res["event"], "merged", (res, self.queue_log()))
        self.assertFalse(ref.exists() or index.exists())
        self.assertIn("removed stale git locks", self.queue_log())
        self.assertEqual(store.read("acme")["stops"], 0)

    def test_a_fresh_lock_survives(self):
        integrator.checkout("acme")
        fresh = self.clone / ".git" / "FETCH_HEAD.lock"             # `orche queue add --pr` fetching right now
        fresh.write_text("")
        integrator._unlock(self.clone)
        self.assertTrue(fresh.exists())


class ProofTest(IntegratorCase):
    """A fetch error after the merge proves nothing; a proof closure needs a resume."""

    def test_a_failed_fetch_after_the_merge_stays_testing_and_the_next_pass_proves_it(self):
        h = self.branch("feat/a", {"src/a.txt": "a\n"})
        self.queue_pr(1, h, "feat/a")
        real = shell.git

        def offline_after_merge(where, *argv, **kw):
            if argv and argv[0] == "fetch" and self.merges():
                return shell.Result(128, "", "fatal: unable to access origin: Could not resolve host", ["git", *argv])
            return real(where, *argv, **kw)
        with mock.patch.object(shell, "git", side_effect=offline_after_merge), \
                mock.patch.object(integrator, "PROOF_RETRY_S", 0):
            res = integrator.loop("acme", 1)
        self.assertEqual((res["code"], res["event"]), (1, "unknown"), res)
        q = store.read("acme")
        self.assertEqual((q["entries"][0]["state"], q["closed"]), ("testing", None))
        self.assertTrue(q["entries"][0]["test"]["landed"])
        self.assertNotIn("closed", self.events())
        self.assertFalse([r for r in ledger.read("acme") if r["sha"] == h])
        self.assertIn("its proof waits for the next pass: git fetch of origin/main failed: fatal: unable to access",
                      self.queue_log())
        self.assertEqual(integrator.recover("acme"), [])                             # landed: never `lost`
        self.assertTrue(queue.startable("acme"))
        self.assertEqual(integrator.loop("acme", 1)["event"], "merged")
        self.assertEqual(store.read("acme")["entries"], [])
        self.assertEqual(self.events(), ["added", "testing", "merged"])
        m = git(self.origin, "rev-parse", "main")
        rows = {(r["pr"], r["sha"]) for r in ledger.read("acme")}
        self.assertTrue({("1", h), ("none", m)} <= rows, rows)
        self.assertEqual(len(self.merges()), 1)

    def test_a_first_entry_neither_tests_nor_reopens_a_proof_closure(self):
        h = self.branch("feat/a", {"src/a.txt": "a\n"})
        self.queue_pr(1, h, "feat/a")
        self.spec(merge_other=True)
        integrator.loop("acme", 1)
        self.spec()
        self.queue_pr(2, self.branch("feat/fix", {"src/fix.txt": "f\n"}), "feat/fix", first=True)
        res = integrator.loop("acme")
        self.assertEqual((res["code"], res["event"]), (3, "closed"))
        self.assertIn("closed (proof); nothing is tested until orche queue resume acme", res["line"])
        self.assertEqual((len(self.merges()), store.read("acme")["closed"]["reason"]), (1, "proof"))
        queue.resume("acme", by="main-acme")
        integrator.loop("acme")
        self.assertEqual([x["argv"][2] for x in self.merges()], ["1", "2"])

    def test_a_github_refusal_needs_a_resume_even_for_a_first_entry(self):
        self.queue_pr(1, self.branch("feat/a", {"src/a.txt": "a\n"}), "feat/a")
        self.spec(merge_fail="GraphQL: Base branch policy prohibits the merge. (mergePullRequest)")
        integrator.loop("acme")
        self.spec()
        self.queue_pr(2, self.branch("feat/fix", {"src/fix.txt": "f\n"}), "feat/fix", first=True)
        self.assertEqual(integrator.loop("acme")["event"], "paused")
        self.assertEqual(self.merges(), [])


class AutoQueueTest(IntegratorCase):
    """The runner's auto-queue records the sender session as `parent`; the unit's brief names it."""

    def test_all_children_landed_when_the_entry_parent_is_the_sender_session(self):
        bu = self.unit(to="div-acme-build", sender="main-acme")
        c1 = self.unit(lineage=bu.lineage, parent=bu.unit)
        c2 = self.unit(lineage=bu.lineage, parent=bu.unit)
        h1, h2 = self.branch("feat/a", {"src/a.txt": "a\n"}), self.branch("feat/b", {"src/b.txt": "b\n"})
        self.queue_pr(1, h1, "feat/a", unit=c1.unit, parent="div-acme-build")
        self.queue_pr(2, h2, "feat/b", unit=c2.unit, parent="div-acme-build")
        integrator.loop("acme")
        urgent = [p["summary"] for p in self.notes() if p["urgent"]]
        self.assertEqual(urgent, [f"all 2 units of {bu.unit} landed: PR #1, #2 merged — report your unit to Main"])


class LandModeTest(IntegratorCase):
    """`land: auto` releases entries queued while the project was `land: owner`."""

    def test_switching_to_auto_releases_entries_awaiting_the_owner(self):
        self.config(queue={"land": "owner"})
        self.queue_pr(1, self.branch("feat/a", {"src/a.txt": "a\n"}), "feat/a", owner_ok=False)
        self.assertEqual(integrator.loop("acme", 1)["event"], "idle")
        self.config(queue={"land": "auto"})
        self.assertEqual(integrator.loop("acme")["event"], "idle")
        self.assertEqual(self.events("merged"), ["merged"])
        released = [(ln["pr"], ln["by"], ln["reason"]) for ln in store.read_log("acme") if ln["event"] == "approved"]
        self.assertEqual(released, [(1, queue.LAND_AUTO, "land: auto")])
        self.config(queue={"land": "owner"})
        self.assertFalse(queue.approved_in_log("acme", 1))                  # never the owner's word


class GateEnvTest(unittest.TestCase):
    """The gate gets no secrets."""

    def test_the_gate_env_drops_every_secret_name(self):
        canary = {"GH_TOKEN": "c", "GITHUB_TOKEN": "c", "OPENAI_API_KEY": "c", "JEV_API_KEY": "c", "JEV_URL": "c",
                  "ORCHE_HOME": "/c", "CODEX_HOME": "/c", "AWS_SECRET_ACCESS_KEY": "c", "PGPASSWORD": "c",
                  "MYSQL_PWD": "c", "npm_config__authToken": "c", "SSH_AUTH_SOCK": "/c", "ANTHROPIC_API_KEY": "c",
                  "GOOGLE_APPLICATION_CREDENTIALS": "/c", "CLAUDE_CODE_ENTRYPOINT": "c", "ORCHE_SESSION": "c",
                  "ORCHE_RUN_ID": "c", "ORCHE_RUNNER": "c", "ORCHE_QUEUE_PR": "99", "ORCHE_QUEUE_RUN": "old"}
        keep = {"PATH": "/usr/bin:/bin", "HOME": "/home/u", "LANG": "en_US.UTF-8", "TMPDIR": "/tmp/u",
                "GIT_AUTHOR_NAME": "u"}
        with mock.patch.dict(os.environ, {**canary, **keep}):
            pr_env, main_env = gate.env(5, "pr5-abcdef0@1"), gate.env(None, "pr5-abcdef0@1")
        self.assertEqual(sorted(set(canary) & set(pr_env)), ["ORCHE_QUEUE_PR", "ORCHE_QUEUE_RUN"])
        self.assertEqual({k: pr_env[k] for k in keep}, keep)
        self.assertEqual([pr_env[k] for k in ("CI", "ORCHE_QUEUE", "ORCHE_QUEUE_PR", "ORCHE_QUEUE_RUN")],
                         ["1", "1", "5", "pr5-abcdef0@1"])
        self.assertNotIn("ORCHE_QUEUE_PR", main_env)


class OwnRefAndAuthPauseTest(IntegratorCase):
    """The integrator compares its own ref; an auth pause names the OWNER step."""

    def test_a_queue_add_fetch_between_the_integrators_fetch_and_compare_changes_nothing(self):
        from pathlib import Path
        from orche import cmd_queue
        h1 = self.branch("feat/a", {"src/a.txt": "a\n"})
        self.queue_pr(1, h1, "feat/a")
        real, add_wrote_fetch_head = shell.git, []

        def add_in_between(where, *argv, **kw):
            res = real(where, *argv, **kw)
            if "+refs/heads/feat/a:refs/remotes/origin/feat/a" in argv and not add_wrote_fetch_head:
                h2 = self.branch("feat/b", {"src/b.txt": "b\n"})            # a PR head the clone lacks
                before = (Path(where) / ".git" / "FETCH_HEAD").read_text()
                cmd_queue._pr_checkout(registry.load(), "acme", 2, h2, "main")
                add_wrote_fetch_head.append(before != (Path(where) / ".git" / "FETCH_HEAD").read_text())
                real(where, "fetch", "--no-tags", "origin", "refs/heads/feat/b")   # any other FETCH_HEAD writer
            return res
        with mock.patch.object(shell, "git", side_effect=add_in_between):
            res = integrator.loop("acme", 1)
        self.assertEqual(add_wrote_fetch_head, [False])
        self.assertEqual((res["event"], self.events("dropped")), ("merged", []), res)

    def test_an_auth_pause_carries_the_owner_step(self):
        h = self.branch("feat/a", {"src/a.txt": "a\n"})
        self.queue_pr(1, h, "feat/a")
        self.spec(fail="To get started with GitHub CLI, please run:  gh auth login")
        for _ in range(3):
            integrator.loop("acme", 1)
        paused = [p for p in self.notes() + self.notes("main-acme") if p["urgent"] and "paused" in p["summary"]]
        self.assertEqual(len(paused), 2, paused)
        self.assertEqual({p.get("owner") for p in paused}, {"gh auth login, then orche queue resume acme"})
