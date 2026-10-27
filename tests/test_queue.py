"""The merge queue (0.9.0 T2): store and library (add, duplicates, --first, approve), and `orche queue`
with its §4.3 refusals against a temp repo (local bare origin) and a fake `gh`."""
import io
import json
import os
import subprocess
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

import support
from orche import brief, inbox, ledger, procs, queue, registry, roster
from orche.queue import store
from test_ledger import make_registry
from test_registry import load_cli

GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false",
       "-c", "init.defaultBranch=main"]


def git(where, *argv) -> str:
    return subprocess.run([*GIT, "-C", str(where), *argv], check=True, capture_output=True, text=True).stdout.strip()


def entry(pr=7, head="a" * 40, **kw):
    return {"id": queue.entry_id(pr, head), "pr": pr, "head": head, "branch": f"feat/x{pr}", "unit": None,
            "owner_ok": True, "state": "waiting", **kw}


class QueueStoreTest(support.TempHomeCase):
    def test_add_appends_and_logs(self):
        res = queue.add("acme", entry(1), by="div-acme-build")
        self.assertEqual((res["status"], res["position"], res["total"]), ("queued", 1, 1))
        queue.add("acme", entry(2))
        q = store.read("acme")
        self.assertEqual([e["pr"] for e in q["entries"]], [1, 2])
        self.assertEqual([ln["event"] for ln in store.read_log("acme")], ["added", "added"])
        self.assertTrue((self.home / "queue" / "acme" / "queue.json").is_file())

    def test_same_head_is_already_queued(self):
        queue.add("acme", entry(1))
        queue.add("acme", entry(2))
        res = queue.add("acme", entry(2))
        self.assertEqual((res["status"], res["position"]), ("already", 2))
        self.assertEqual(len(store.read("acme")["entries"]), 2)

    def test_new_head_supersedes_old_entry(self):
        queue.add("acme", entry(1))
        res = queue.add("acme", entry(1, head="b" * 40))
        self.assertEqual(res["superseded"]["head"], "a" * 40)
        self.assertEqual([e["id"] for e in store.read("acme")["entries"]], ["pr1-bbbbbbb"])
        dropped = [ln for ln in store.read_log("acme") if ln["event"] == "dropped"]
        self.assertEqual(dropped[0]["reason"], "superseded")

    def test_first_goes_after_testing_entries(self):
        queue.add("acme", entry(1, state="testing"))
        queue.add("acme", entry(2))
        res = queue.add("acme", entry(3), first=True)
        self.assertEqual(res["position"], 2)
        self.assertEqual([e["pr"] for e in store.read("acme")["entries"]], [1, 3, 2])

    def test_next_entry_skips_unapproved(self):
        queue.add("acme", entry(1, owner_ok=False))
        queue.add("acme", entry(2))
        self.assertEqual(queue.next_entry(store.read("acme"))["pr"], 2)

    def test_approve_all_flips_and_logs(self):
        queue.add("acme", entry(1, owner_ok=False))
        queue.add("acme", entry(2, owner_ok=False))
        self.assertEqual(queue.approve("acme", None, by="main-acme"), [1, 2])
        self.assertTrue(all(e["owner_ok"] for e in store.read("acme")["entries"]))
        self.assertTrue(queue.approved_in_log("acme", 1, "main-acme"))
        queue.drop("acme", "#1", by="main-acme")
        self.assertFalse(queue.approved_in_log("acme", 1, "main-acme"))

    def test_bad_event_refused(self):
        with self.assertRaises(ValueError):
            store.log("acme", "exploded")

    def test_pause_resume_summary(self):
        queue.add("acme", entry(1, owner_ok=False))
        queue.pause("acme", by="main-acme", reason="release")
        s = queue.summary("acme")
        self.assertEqual((s["state"], s["waiting"], s["await"]), ("paused", 1, 1))
        queue.resume("acme", by="main-acme")
        self.assertEqual(queue.summary("acme")["state"], "open")

    def test_settings_per_project(self):
        cfg = {"queue": {"land": "auto", "per_project": {"acme": {"land": "owner"}}}}
        self.assertEqual(queue.land_mode(cfg, "acme"), "owner")
        self.assertEqual(queue.land_mode(cfg, "other"), "auto")


class QueueCmdTest(support.TempHomeCase):
    def setUp(self):
        super().setUp()
        self.cli = load_cli()
        spawn = mock.patch.object(procs, "spawn_detached")      # `queue add` starts the integrator (T3)
        self.spawned = spawn.start()
        self.addCleanup(spawn.stop)
        wt = self.home / "wt"
        wt.mkdir()
        origin, acme, feat = wt / "origin.git", wt / "acme", wt / "acme-checkout"
        subprocess.run([*GIT, "init", "-q", "--bare", str(origin)], check=True)
        subprocess.run([*GIT, "init", "-q", "-b", "main", str(acme)], check=True)
        (acme / "README.md").write_text("acme\n")
        git(acme, "add", "-A")
        git(acme, "commit", "-q", "-m", "init")
        git(acme, "remote", "add", "origin", str(origin))
        git(acme, "push", "-q", "-u", "origin", "main")
        git(acme, "remote", "set-head", "origin", "main")
        git(acme, "worktree", "add", "-q", "-b", "feat/checkout", str(feat))
        (feat / "scripts" / "verify").mkdir(parents=True)
        (feat / "scripts" / "verify" / "ci.sh").write_text("#!/bin/sh\nexit 0\n")
        (feat / "scripts" / "verify" / "ci.sh").chmod(0o755)
        (feat / "src").mkdir()
        (feat / "src" / "pay.py").write_text("x = 1\n")
        git(feat, "add", "-A")
        git(feat, "commit", "-q", "-m", "add pay")
        self.feat, self.head = feat, git(feat, "rev-parse", "HEAD")
        make_registry(wt)
        fields = {"TO": "feat-acme-checkout", "GOAL": "Add pay", "SCOPE": "src/**", "CONTEXT": "none",
                  "ACCEPT": "works", "VERIFY": "`scripts/verify/ci.sh`", "TIMEBOX": "20m", "FORBID": "merge",
                  "REPORT": "status", "TIER": "standard"}
        target = roster.Roster(registry.load()).member("feat-acme-checkout")
        self.unit = brief.write(brief.Block("brief", fields), target, "div-acme-build", "1. x", timeout=0).unit
        self.gh = self.home / "fakegh"
        self.spec({"prs": [self.pr()]})
        os.environ["ORCHE_SESSION"] = "div-acme-build"

    def pr(self, **kw):
        return {"number": 12, "headRefName": "feat/checkout", "baseRefName": "main", "headRefOid": self.head,
                "isDraft": False, "state": "OPEN", **kw}

    def spec(self, spec):
        support.fake_gh(self.gh, spec)

    def ready(self, ledger_row=True, review="pass", note="", model="gpt-5.5"):
        if ledger_row:
            ledger.append("acme", ledger.make_row("12", self.head, "unit-test-verified", "logs/v.txt",
                                                  "div-acme-build", producer="feat-acme-checkout"))
        if review:
            ledger.record_review("acme", "pre", "12", self.head, review, "review/x.md", "codex-review", model,
                                 writer="feat-acme-checkout", note=note)

    def orche(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            try:
                code = self.cli.main(list(argv))
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    def owner(self, wake=False):
        cfg = {**support.NO_WAKE, "queue": {"land": "owner"}}
        if wake:
            cfg["inbox"] = {**cfg["inbox"], "wake": True}
        (self.home / "config.json").write_text(json.dumps(cfg))

    def queued(self):
        return store.read("acme")["entries"]

    def require_review(self):
        """review.cross_vendor required (the default is off): the queue then needs a pre-review row."""
        (self.home / "config.json").write_text(json.dumps({**support.NO_WAKE, "review": {**support.NO_WAKE["review"],
                                                                                 "cross_vendor": "required"}}))

    # ---- refusals (§4.3) -----------------------------------------------------------------

    def test_feature_session_cannot_add(self):
        self.ready()
        os.environ["ORCHE_SESSION"] = "feat-acme-checkout"
        code, _, err = self.orche("queue", "add", self.unit)
        self.assertEqual(code, 3)
        self.assertIn("only Build, Main or the unit's head may queue", err)

    def heads(self):
        """T2: checkout moves under a Design division; a GTM division heads nobody here."""
        from test_buildreview import add_design
        reg = registry.load()
        add_design(reg, self.home / "wt", gtm=True)
        divs = reg["projects"]["acme"]["divisions"]
        divs["design"]["features"] += divs["build"].pop("features")
        registry.save(reg)

    def test_the_units_head_may_queue_another_head_may_not(self):
        self.ready()
        self.heads()
        os.environ["ORCHE_SESSION"] = "div-acme-gtm"
        code, _, err = self.orche("queue", "add", self.unit)
        self.assertEqual(code, 3)
        self.assertIn("only Build, Main or the unit's head may queue", err)
        self.assertFalse(self.queued() if (self.home / "queue").exists() else [])
        os.environ["ORCHE_SESSION"] = "div-acme-design"
        code, out, err = self.orche("queue", "add", self.unit)
        self.assertEqual(code, 0, err)
        self.assertIn("queued PR #12", out)
        self.assertEqual(self.queued()[0]["writer"], "feat-acme-checkout")

    def test_unknown_unit(self):
        code, _, err = self.orche("queue", "add", "acme-nope-20261024-1")
        self.assertEqual(code, 2)
        self.assertIn("unknown unit 'acme-nope-20261024-1'", err)

    def test_no_open_pr(self):
        self.ready()
        self.spec({"prs": []})
        code, _, err = self.orche("queue", "add", self.unit)
        self.assertEqual(code, 3)
        self.assertIn("no open PR for feat/checkout", err)

    def test_pr_head_differs(self):
        self.ready()
        self.spec({"prs": [self.pr(headRefOid="d" * 40)]})
        code, _, err = self.orche("queue", "add", self.unit)
        self.assertEqual(code, 3)
        self.assertIn(f"PR #12's head is ddddddd, the worktree's is {self.head[:7]}: report done again to publish", err)

    def test_gh_failure_blocks(self):
        self.ready()
        self.spec({"prs": [], "fail": "gh: not logged in"})
        code, _, err = self.orche("queue", "add", self.unit)
        self.assertEqual(code, 3)

    def test_no_ledger_row(self):
        self.ready(ledger_row=False)
        code, _, err = self.orche("queue", "add", self.unit)
        self.assertEqual(code, 3)
        self.assertIn(f"no ledger row for {self.head[:7]}", err)

    def test_no_pre_review(self):
        self.require_review()
        self.ready(review=None)
        code, _, err = self.orche("queue", "add", self.unit)
        self.assertEqual(code, 3)
        self.assertIn(f"no pre-review for {self.head[:7]}: `orche review --pr ", err)     # a valid command (G7)
        self.assertNotIn("--unit", err)

    def test_held_review_needs_accept(self):
        self.require_review()
        self.ready(review="held")
        code, _, err = self.orche("queue", "add", self.unit)
        self.assertEqual(code, 3)
        self.assertIn('--accept "<why>"', err)
        code, _, _ = self.orche("queue", "add", self.unit, "--accept", "reviewer down")
        self.assertEqual(code, 0)
        self.assertEqual(self.queued()[0]["accept"], "reviewer down")

    def test_no_gate(self):
        git(self.feat, "rm", "-q", "scripts/verify/ci.sh")
        git(self.feat, "commit", "-q", "-m", "drop gate")
        self.head = git(self.feat, "rev-parse", "HEAD")
        self.spec({"prs": [self.pr()]})
        self.ready()
        # the VERIFY names scripts/verify/ci.sh, which is gone too
        code, _, err = self.orche("queue", "add", self.unit)
        self.assertEqual(code, 3)
        self.assertIn("no gate: add scripts/verify/ci.sh (Appendix A.13) or a committed VERIFY script", err)

    def test_skip_ci_on_code(self):
        (self.feat / "src" / "pay.py").write_text("x = 2\n")
        git(self.feat, "commit", "-q", "-am", "tweak [skip ci]")
        self.head = git(self.feat, "rev-parse", "HEAD")
        self.spec({"prs": [self.pr()]})
        self.ready()
        code, _, err = self.orche("queue", "add", self.unit)
        self.assertEqual(code, 3)
        self.assertIn(f"head {self.head[:7]} carries [skip ci] on a code change (STANDING 4): add a commit without it", err)

    def test_skip_ci_below_the_head_on_code(self):
        """One [skip ci] rule with the facts (every commit base..head): a code commit below the head counts too."""
        (self.feat / "src" / "pay.py").write_text("x = 2\n")
        git(self.feat, "commit", "-q", "-am", "tweak [skip ci]")
        skipped = git(self.feat, "rev-parse", "HEAD")
        (self.feat / "docs").mkdir(exist_ok=True)
        (self.feat / "docs" / "notes.md").write_text("notes\n")
        git(self.feat, "add", "docs/notes.md")
        git(self.feat, "commit", "-q", "-m", "notes")
        self.head = git(self.feat, "rev-parse", "HEAD")
        self.spec({"prs": [self.pr()]})
        self.ready()
        code, _, err = self.orche("queue", "add", self.unit)
        self.assertEqual(code, 3)
        self.assertIn(f"commit {skipped[:7]} carries [skip ci] on a code change (STANDING 4)", err)

    # ---- success, duplicates, dry run ----------------------------------------------------

    def test_add_queues_pinned_to_head(self):
        self.ready()
        code, out, err = self.orche("queue", "add", self.unit)
        self.assertEqual(code, 0, err)
        self.assertIn(f"queued PR #12 (feat/checkout @ {self.head[:7]}) — position 1 of 1", out)
        e = self.queued()[0]
        self.assertEqual((e["id"], e["head"], e["writer"], e["parent"], e["owner_ok"]),
                         (f"pr12-{self.head[:7]}", self.head, "feat-acme-checkout", "", True))   # parent: the brief's `parent:` unit (none here)
        code, out, _ = self.orche("queue", "add", self.unit)
        self.assertEqual(code, 0)
        self.assertIn("already queued, position 1", out)
        code, out, _ = self.orche("queue", "acme")
        self.assertIn("queue acme: 1 waiting", out)

    def test_dry_run_prints_facts_writes_nothing(self):
        self.ready()
        code, out, err = self.orche("--dry-run", "queue", "add", self.unit)
        self.assertEqual(code, 0, err)
        self.assertEqual(len(out.strip().splitlines()), 6, out)
        self.assertFalse((self.home / "queue").exists())

    def test_add_by_pr_number_from_main(self):
        self.ready()
        os.environ["ORCHE_SESSION"] = "main-acme"
        code, out, err = self.orche("queue", "add", "--pr", "12", "--project", "acme")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.queued()[0]["pr"], 12)

    def test_add_by_pr_number_fetches_a_head_only_origin_has(self):
        wt = self.home / "wt"
        other = wt / "elsewhere"
        git(wt, "clone", "-q", str(wt / "origin.git"), str(other))
        (other / "scripts" / "verify").mkdir(parents=True)
        (other / "scripts" / "verify" / "ci.sh").write_text("#!/bin/sh\nexit 0\n")
        (other / "scripts" / "verify" / "ci.sh").chmod(0o755)
        (other / "src.py").write_text("y = 2\n")
        git(other, "add", "-A")
        git(other, "commit", "-q", "-m", "remote work [skip ci]")
        git(other, "push", "-q", "origin", "HEAD:refs/heads/feat/remote")
        head = git(other, "rev-parse", "HEAD")
        missing = subprocess.run(["git", "-C", str(wt / "acme"), "cat-file", "-e", head], capture_output=True)
        self.assertNotEqual(missing.returncode, 0)                       # not in the main checkout
        remote = {"number": 13, "headRefName": "feat/remote", "baseRefName": "main", "headRefOid": head,
                  "isDraft": False, "state": "OPEN"}
        self.spec({"prs": [self.pr(), remote, {**remote, "number": 14, "headRefOid": "c" * 40}]})
        ledger.append("acme", ledger.make_row("13", head, "unit-test-verified", "logs/v.txt", "div-acme-build",
                                              producer="feat-acme-checkout"))
        ledger.record_review("acme", "pre", "13", head, "pass", "review/x.md", "codex-review", "gpt-5.5",
                             writer="feat-acme-checkout")
        os.environ["ORCHE_SESSION"] = "main-acme"
        code, out, err = self.orche("queue", "add", "--pr", "13", "--project", "acme")
        self.assertNotEqual(code, 0)                                     # read from the fetched head: [skip ci]
        self.assertIn(f"head {head[:7]} carries [skip ci] on a code change", err)
        code, out, err = self.orche("queue", "add", "--pr", "13", "--project", "acme", "--accept", "smoke")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.queued()[0]["head"], head)
        code, out, err = self.orche("queue", "add", "--pr", "14", "--project", "acme")
        self.assertNotEqual(code, 0)
        self.assertIn("cannot fetch PR #14's head ccccccc from origin: ", err)
        self.assertEqual(len(self.queued()), 1)

    def test_status_names_the_lost_threshold_of_a_testing_entry(self):
        queue.add("acme", entry(pr=7), by="div-acme-build")
        queue.mark("acme", "#7", state="testing", test={"pid": 1})
        code, out, err = self.orche("queue", "acme")
        self.assertEqual(code, 0, err)
        self.assertIn("PR #7 x7 aaaaaaa — testing (lost after 3600s)", out)
        queue.mark("acme", "#7", test={"pid": 1, "landed": {"method": "merge"}})
        self.assertIn("merged on GitHub, its proof pending", self.orche("queue", "acme")[1])

    # ---- land: owner ---------------------------------------------------------------------

    def test_owner_add_asks_main_once(self):
        self.owner(wake=True)
        self.ready()
        with mock.patch.object(procs, "spawn_detached") as spawn:
            code, out, err = self.orche("queue", "add", self.unit)
        self.assertEqual(code, 0, err)
        self.assertIn("waits for the owner's word", out)
        self.assertFalse(self.queued()[0]["owner_ok"])
        asks = [p for _, p in inbox.pending("main-acme") if p.get("status") == "question"]
        self.assertEqual(len(asks), 1)
        self.assertIn("orche queue approve acme #12", asks[0]["summary"])
        self.assertEqual(spawn.call_count, 1)
        self.assertIn("main-acme", spawn.call_args[0][0])

    def test_owner_approve_from_build_refused(self):
        self.owner()
        self.ready()
        self.orche("queue", "add", self.unit)
        code, _, err = self.orche("queue", "approve", "acme", "#12")
        self.assertEqual(code, 3)
        self.assertIn("only Main approves landing in a land: owner project; report it to Main", err)

    def test_owner_approve_all_then_readd_keeps_it(self):
        self.owner()
        self.ready()
        self.orche("queue", "add", self.unit)
        os.environ["ORCHE_SESSION"] = "main-acme"
        code, out, err = self.orche("queue", "approve", "acme", "--all")
        self.assertEqual(code, 0, err)
        self.assertTrue(self.queued()[0]["owner_ok"])
        self.assertIn("approved", [ln["event"] for ln in store.read_log("acme")])
        # a new head re-adds the PR: the approval stands
        (self.feat / "src" / "pay.py").write_text("x = 3\n")
        git(self.feat, "commit", "-q", "-am", "fix")
        self.head = git(self.feat, "rev-parse", "HEAD")
        self.spec({"prs": [self.pr()]})
        self.ready()
        os.environ["ORCHE_SESSION"] = "div-acme-build"
        code, _, err = self.orche("queue", "add", self.unit)
        self.assertEqual(code, 0, err)
        self.assertEqual([(e["head"], e["owner_ok"]) for e in self.queued()], [(self.head, True)])

    def test_drop_and_pause(self):
        self.ready()
        self.orche("queue", "add", self.unit)
        code, _, _ = self.orche("queue", "pause", "acme", "--reason", "release")
        self.assertEqual(code, 0)
        code, out, _ = self.orche("queue", "drop", "acme", "#12")
        self.assertEqual(code, 0)
        self.assertEqual(self.queued(), [])
        code, _, _ = self.orche("queue", "drop", "acme", "#12")
        self.assertEqual(code, 2)

    def test_add_by_pr_reads_publish_base_never_a_guess(self):
        """Review B G5: one base resolver — publish.base, else origin/HEAD, else a refusal with the OWNER step."""
        self.ready()
        os.environ["ORCHE_SESSION"] = "main-acme"
        git(self.home / "wt" / "acme", "remote", "set-head", "origin", "-d")
        code, _, err = self.orche("queue", "add", "--pr", "12", "--project", "acme")
        self.assertEqual(code, 3)
        self.assertIn("no base branch: publish.base is unset and origin/HEAD is not set", err)
        self.assertIn("OWNER: git -C", err)
        (self.home / "config.json").write_text(json.dumps({**support.NO_WAKE, "publish": {"base": "main"}}))
        code, _, err = self.orche("queue", "add", "--pr", "12", "--project", "acme")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.queued()[0]["pr"], 12)

    def test_a_closed_queue_names_its_reason_and_what_reopens_it(self):
        """Review B G8e: --first reopens only main-red; a proof closure needs orche queue resume."""
        from orche import cmd_status, project, util
        from orche.install import doctor
        for reason, step in (("proof", "check it, then orche queue resume acme"),
                             ("main-red", "queue the fix with --first")):
            q = store.empty("acme")
            q["closed"] = {"at": "2026-10-25T00:00:00Z", "reason": reason, "main": "d" * 40}
            store.queue_path("acme").parent.mkdir(parents=True, exist_ok=True)
            util.write_json(store.queue_path("acme"), q)
            _code, out, _ = self.orche("queue", "acme")
            [(_level, _name, detail)] = doctor.queue_rows(registry.load())
            [line] = cmd_status._queue_lines({"project": "acme", "queue": project.queue_of("acme")})
            for text in (out, detail, line):
                self.assertIn(f"{reason} — ", text)
                self.assertIn(step, text)
        self.assertNotIn("--first", queue.closed_text("acme", {"reason": "proof", "main": "d" * 40}))
