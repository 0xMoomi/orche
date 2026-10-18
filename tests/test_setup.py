"""Setup wave S2: choosing and customising divisions in `orche init` (stubbed Orca, fake TTY stdin)."""
import hashlib
import io
import json
import os
import unittest
from pathlib import Path
from unittest import mock

import support
from orche import registry, setup_flow, shell
from orche.hr import onboarding
from test_hr import ALL, HrCase

CHARTER = "owns the visual system, reviews UI changes, keeps the brand kit current"
DESIGN = {"label": "Design", "charter": CHARTER, "model": "claude-opus-5-5", "effort": "high"}


class TTYInput(io.StringIO):
    """Typed answers on a terminal: `isatty()` is what `orche init` checks."""

    def isatty(self):
        return True


class SetupCase(HrCase):
    def typed(self, *answers, argv=("init", "Acme Shop")):
        """Run `orche <argv>` with `answers` typed on a fake terminal, one per line."""
        with mock.patch("sys.stdin", TTYInput("".join(a + "\n" for a in answers))):
            return self.orche(*argv)

    def created(self):
        return [c[c.index("--name") + 1] for c in self.orca_calls("worktree create")]

    def proj(self):
        return registry.load()["projects"]["acme-shop"]

    def real_personas(self):
        onboarding.PERSONAS = self._orig_personas


# ---- defaults (no terminal, no flags) -----------------------------------------------------------

class DefaultsTest(SetupCase):
    def test_no_tty_no_flags_takes_the_screen_defaults_and_prints_the_summary(self):
        code, out, err = self.orche("init", "Acme Shop")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.created(), ["div-rnd", "div-research", "div-build", "div-gtm"])
        self.assertIn("Summary: rnd, research, build, gtm · stage building · 4 worktrees · panes main + liaison\n",
                      out)
        proj = self.proj()
        self.assertEqual(registry.divisions_of(proj), ["rnd", "research", "build", "gtm"])
        self.assertEqual(proj["custom_divisions"], {})
        self.assertTrue(all(d["status"] == "dormant" for d in proj["divisions"].values()))   # nothing woken

    def test_rerun_of_a_registered_project_keeps_its_setup(self):
        self.assertEqual(self.orche("init", "Acme Shop", "--divisions", "build")[0], 0)
        self.calls.clear()
        code, out, err = self.orche("init", "Acme Shop")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.created(), [])
        self.assertIn("Summary: build · stage building · 0 new worktrees · panes main + liaison", out)
        self.assertIn("build: skipped (already onboarded)", out)

    def test_json_on_a_terminal_never_prompts(self):
        code, out, err = self.typed("n", argv=("--json", "--dry-run", "init", "Acme Shop"))
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        self.assertEqual(list(data["divisions"]), ["rnd", "research", "build", "gtm"])
        self.assertEqual(data["summary"], "Summary: rnd, research, build, gtm · stage building · 4 worktrees · "
                                          "panes main + liaison")


# ---- interactive checklist ----------------------------------------------------------------------

class InteractiveTest(SetupCase):
    def test_toggle_add_custom_and_apply(self):
        self.real_personas()
        code, out, err = self.typed("a", "design", "Design", CHARTER, "", "", "1 2", "", "", "dw, Acme", "")
        self.assertEqual(code, 0, err)
        self.assertIn("Divisions for acme-shop  (type numbers to toggle, a to add a custom division, Enter to "
                      "continue)\n"
                      " 1 [x] rnd       R&D — prototypes and technical spikes\n"
                      " 2 [x] research  Research — market, users, competitors\n"
                      " 3 [x] build     Build — integrates features, owns the ledger, opens PRs\n"
                      " 4 [x] gtm       GTM — launch, content, growth\n"
                      " 5 [ ] ops       Ops — deploy and operate (dormant until stage public)\n> ", out)
        self.assertIn(" 6 [x] design    Design — owns the visual system, reviews UI chan…\n", out)
        self.assertIn(" 1 [ ] rnd ", out)
        self.assertIn("Summary: build, gtm, design(custom) · stage building · 3 worktrees · panes main + liaison\n"
                      "Apply? [Y/n] ", out)
        self.assertEqual(self.created(), ["div-build", "div-gtm", "div-design"])
        proj = self.proj()
        self.assertEqual(proj["custom_divisions"], {"design": DESIGN})
        self.assertEqual(registry.divisions_of(proj), ["build", "gtm", "design"])
        self.assertEqual(proj["aliases"], ["dw", "acme"])
        design = proj["divisions"]["design"]
        self.assertEqual((design["status"], design["session"], design["branch"], design["model"], design["effort"]),
                         ("dormant", "div-acme-shop-design", "div/design", "claude-opus-5-5", "high"))
        wt = self.wtroot / "div-design"
        self.assertEqual(sorted(p.name for p in (wt / ".orche" / "div" / "design").iterdir()),
                         ["CHARTER.md", "DECISIONS.tsv", "HANDOFF.md", "STATUS.md"])
        persona = (self.repo / ".claude" / "agents" / "div-acme-shop-design.md").read_text()
        self.assertIn(f"## Charter\n{CHARTER}\n", persona)
        self.assertIn("Under the runner (`ORCHE_RUNNER=1`) do not run `orche report`", persona)
        self.assertTrue((wt / ".claude" / "agents" / "div-acme-shop-design.md").is_file())
        self.assertIn("design: dormant (custom)", out)
        self.assertNotIn("\nSummary:", out.split("Apply? [Y/n] ", 1)[1])   # shown once, by the checklist

    def test_answer_n_aborts_with_exit_0_and_writes_nothing(self):
        code, out, err = self.typed("", "", "", "n")
        self.assertEqual((code, err), (0, ""))
        self.assertTrue(out.endswith("Apply? [Y/n] aborted; nothing written\n"), out)
        self.assertEqual(registry.load()["projects"], {})
        self.assertEqual(self.created(), [])
        self.assertEqual(list(self.repo.iterdir()), [])
        self.assertFalse(self.wtroot.exists())

    def test_end_of_input_aborts(self):
        code, out, _ = self.typed("3")          # stdin ends at the next prompt
        self.assertEqual(code, 0)
        self.assertTrue(out.endswith("aborted; nothing written\n"))
        self.assertEqual(registry.load()["projects"], {})

    def test_ctrl_c_aborts_with_130(self):
        stdin = TTYInput("")
        stdin.readline = mock.Mock(side_effect=KeyboardInterrupt)
        with mock.patch("sys.stdin", stdin):
            code, out, _ = self.orche("init", "Acme Shop")
        self.assertEqual(code, 130)
        self.assertIn("aborted; nothing written", out)
        self.assertEqual(registry.load()["projects"], {})

    def test_bad_answers_are_explained_and_asked_again(self):
        code, out, err = self.typed("9", "x y", "a", "Design", "build", "hr", "design", "x" * 25, "Design",
                                    "", CHARTER, "claude opus", "", "huge", "max", "", "beta", "public", "", "maybe",
                                    "y")
        self.assertEqual(code, 0, err)
        for line in ("  no item 9", "  type item numbers to toggle, a to add a custom division, or Enter to continue",
                     "  ! custom division name 'Design' must be a slug",
                     "  ! 'build' is a standard division", "  ! 'hr' is reserved",
                     "  ! label must be one line of 1-24 characters",
                     "  ! charter must be one paragraph of 1-400 characters",
                     "  ! model must be a model id without spaces or control characters",
                     "  ! effort is one of low|medium|high|xhigh|max",
                     "  ! stage is one of building|public"):
            self.assertIn(line, out)
        self.assertEqual(out.count("Apply? [Y/n] "), 2)
        proj = self.proj()
        self.assertEqual((proj["stage"], proj["custom_divisions"]["design"]["effort"]), ("public", "max"))

    def test_item_numbers_are_ascii_digits_only(self):
        """M2: `²` (str.isdigit but not int-able) gets the hint, `0`/`99` say no item, `1,2` toggles both."""
        code, out, err = self.typed("²", "0", "99", "1,2", "", "", "", "n")
        self.assertEqual(code, 0, err)
        self.assertNotIn("Traceback", out + err)
        self.assertIn(">   type item numbers to toggle, a to add a custom division, or Enter to continue\n> ", out)
        self.assertIn("  no item 0\n", out)
        self.assertIn("  no item 99\n", out)
        self.assertIn(" 1 [ ] rnd ", out)
        self.assertIn(" 2 [ ] research ", out)
        self.assertIn("Summary: build, gtm · stage building", out)

    def test_unticked_wake_division_reshows_the_checklist(self):
        """M3: --wake is checked against the ticked divisions before Stage/Apply, never after."""
        code, out, err = self.typed("1", "", "1", "", "", "", "",
                                    argv=("init", "Acme Shop", "--wake", "rnd,qa"))
        # `qa` is not in the list: the second Enter shows the checklist again; stdin ends there.
        self.assertEqual(code, 0, err)
        self.assertIn("  --wake rnd: rnd is unticked; tick it again (or press Ctrl-D and re-run without it in "
                      "--wake)\n", out)
        self.assertIn("  --wake qa: qa is not in the list; add it with `a`", out)
        self.assertNotIn("Apply?", out)
        self.assertTrue(out.endswith("aborted; nothing written\n"), out)
        self.assertEqual(registry.load()["projects"], {})
        # Re-ticked: the run goes through and wakes it.
        code, out, err = self.typed("1", "", "1", "", "", "", "", argv=("init", "Acme Shop", "--wake", "rnd"))
        self.assertEqual(code, 0, err)
        self.assertEqual(out.count("  --wake rnd: rnd is unticked"), 1)
        self.assertIn("Summary: rnd, research, build, gtm · stage building · 4 worktrees · panes main + liaison · "
                      "wake rnd", out)
        self.assertEqual(self.proj()["divisions"]["rnd"]["status"], "active")

    def test_empty_name_cancels_the_add(self):
        code, out, err = self.typed("a", "", "", "", "", "n")
        self.assertEqual(code, 0, err)
        self.assertNotIn(" 6 [", out)

    def test_dry_run_previews_the_checklist_result_and_writes_nothing(self):
        code, out, err = self.typed("5", "a", "design", "Design", CHARTER, "", "", "", "", "", "",
                                    argv=("--dry-run", "init", "Acme Shop"))
        self.assertEqual(code, 0, err)
        self.assertIn("Summary: rnd, research, build, gtm, ops, design(custom) · stage building · 6 worktrees", out)
        dry = [l for l in err.splitlines() if " worktree create " in l]
        self.assertEqual([l.split("--name ")[1].split()[0] for l in dry],
                         ["div-rnd", "div-research", "div-build", "div-gtm", "div-ops", "div-design"])
        self.assertIn("would init acme-shop", out)
        self.assertIn("design: dormant (custom)", out)
        self.assertEqual(registry.load()["projects"], {})
        self.assertEqual(list(self.repo.iterdir()), [])
        self.assertEqual(self.created(), [])

    def test_registered_project_locks_what_it_has(self):
        self.assertEqual(self.orche("init", "Acme Shop", "--divisions", "build",
                                    "--custom", f"design=Design:{CHARTER}")[0], 0)
        self.calls.clear()
        code, out, err = self.typed("3", "6", "1", "", "")
        self.assertEqual(code, 0, err)
        self.assertIn(" 3 [x] build     Build — integrates features, owns the ledger, opens PRs  (onboarded)", out)
        self.assertIn("  build is already onboarded; removing a division is out of scope "
                      "(sleep it: orche sleep div-acme-shop-build)", out)
        self.assertIn("  design is already onboarded;", out)
        self.assertIn("Stage: building · aliases: -  (registered; change the stage with orche stage set)", out)
        self.assertIn("Summary: rnd, build, design(custom) · stage building · 1 new worktree", out)
        self.assertEqual(self.created(), ["div-rnd"])


# ---- flags and setup files ----------------------------------------------------------------------

class FlagsTest(SetupCase):
    def test_custom_flag_with_model_and_effort_and_wake(self):
        code, out, err = self.orche("init", "Acme Shop", "--divisions", "build",
                                    "--custom", "design=Design:Owns the brand kit:claude-sonnet-5-5:medium",
                                    "--custom", "qa=QA:Tests every release", "--wake", "design")
        self.assertEqual(code, 0, err)
        proj = self.proj()
        self.assertEqual(proj["custom_divisions"]["design"], {"label": "Design", "charter": "Owns the brand kit",
                                                              "model": "claude-sonnet-5-5", "effort": "medium"})
        self.assertEqual(proj["custom_divisions"]["qa"]["model"], "claude-opus-5-5")
        self.assertEqual((proj["divisions"]["design"]["status"], proj["divisions"]["design"]["model"]),
                         ("active", "claude-sonnet-5-5"))
        self.assertEqual(self.created(), ["div-build", "div-design", "div-qa"])
        panes = [c[c.index("--title") + 1] for c in self.orca_calls("terminal create")]
        self.assertEqual(panes[0], "div-acme-shop-design")
        self.assertIn("Summary: build, design(custom), qa(custom) · stage building · 3 worktrees · panes main + "
                      "liaison · wake design", out)

    def test_custom_flag_errors(self):
        cases = [
            (["--custom", "Design=Design:x"], "custom division name 'Design' must be a slug"),
            (["--custom", "build=Build:x"], "'build' is a standard division"),
            (["--custom", "jev=J:x"], "'jev' is reserved"),
            (["--custom", "design=Design:x", "--custom", "design=Design:y"], "--custom design: given twice"),
            (["--custom", f"design={'L' * 25}:x"], "custom division design.label: must be one line of 1-24"),
            (["--custom", f"design=Design:{'c' * 401}"], "custom division design.charter: must be one paragraph"),
            (["--custom", "design=Design"], "want name=label:charter[:model[:effort]]"),
            (["--custom", "design=Design:a:b:c:d"], "a charter or model id containing ':' goes in --setup-file"),
            (["--custom", "design=Design:x::huge"], "design.effort: must be one of"),
            (["--divisions", "build,sales"], "unknown division(s): sales"),
            (["--custom", "design=Design:x", "--wake", "qa"], "unknown division(s): qa"),
            # M6: a slug has single hyphens between parts and 2-16 characters.
            (["--custom", "ab-=AB:x"], "custom division name 'ab-' must be a slug of 2-16"),
            (["--custom", "a--b=AB:x"], "custom division name 'a--b' must be a slug"),
            (["--custom=-ab=AB:x"], "custom division name '-ab' must be a slug"),
            (["--custom", "abcdefghijklmnopq=AB:x"], "must be a slug of 2-16"),
            # M1: a label equal to a standard division's name or label is refused.
            (["--custom", "opsx=Ops:x"], "custom division opsx.label: 'Ops' is a standard division's name or label"),
            (["--custom", "gtmx=gtm:x"], "'gtm' is a standard division's name or label"),
            (["--custom", "lab=r&d:x"], "'r&d' is a standard division's name or label"),
            (["--custom", "rsx=RESEARCH:x"], "'RESEARCH' is a standard division's name or label"),
            # M5: control characters in a label or charter.
            (["--custom", "design=De\x1b[31msign:x"], "design.label: must not contain line breaks or control"),
            (["--custom", "design=De\nsign:x"], "design.label: must not contain line breaks or control"),
            (["--custom", "design=Design:owns \x1b[2Jthe kit"], "design.charter: must not contain control"),
            (["--custom", "design=Design:owns ‮the kit"], "design.charter: must not contain control"),
        ]
        for extra, needle in cases:
            code, out, err = self.orche("init", "Acme Shop", *extra)
            self.assertEqual(code, 1, (extra, err))    # a setup given wrongly is a user error (N-1)
            self.assertIn(needle, err, extra)
            self.assertTrue(err.startswith("orche: ") and err.count("\n") == 1, (extra, err))   # one line
            self.assertNotIn("Traceback", out + err)
        self.assertEqual(registry.load()["projects"], {})
        self.assertEqual(self.created(), [])

    def write_setup(self, data, raw=None):
        path = self.home / "setup.json"
        path.write_text(raw if raw is not None else json.dumps(data))
        return str(path)

    def test_setup_file_applies_the_whole_setup(self):
        path = self.write_setup({"divisions": ["build", "ops"], "stage": "public", "aliases": ["Shop"],
                                 "custom": {"design": {"label": "Design", "charter": "Brand\n kit owner"}}})
        code, out, err = self.orche("init", "Acme Shop", "--setup-file", path)
        self.assertEqual(code, 0, err)
        proj = self.proj()
        self.assertEqual((proj["stage"], proj["aliases"]), ("public", ["shop"]))
        self.assertEqual(proj["custom_divisions"]["design"]["charter"], "Brand kit owner")   # whitespace collapsed
        self.assertEqual(registry.divisions_of(proj), ["build", "ops", "design"])

    def test_setup_file_errors(self):
        cases = [
            ({"divisions": ["build"], "colour": "red"}, None, "setup: unknown key 'colour'"),
            ({"divisions": "build"}, None, "setup: divisions must be a list"),
            ({"divisions": ["sales"]}, None, "unknown division(s): sales"),
            ({"stage": "beta"}, None, "setup: stage must be one of building|public"),
            ({"custom": {"design": {"label": "Design", "charter": "one\n\ntwo"}}}, None, "charter must be one paragraph"),
            ({"custom": {"design": {"label": "Design"}}}, None, "design.charter: must be one paragraph of 1-400"),
            ({"custom": {"design": {"label": "D", "charter": "c", "owner": "x"}}}, None, "unknown key 'owner'"),
            (None, '{"custom": {"design": {"label": "A", "charter": "a"}, "design": {"label": "B", "charter": "b"}}}',
             "key 'design' appears twice"),
            (None, "{not json", "invalid JSON"),
            (None, "[1, 2]", "must be a JSON object"),
            # I1: every field's type is checked; a wrong type is one `orche:` line, never a traceback.
            ({"custom": {"design": {"label": "D", "charter": "x", "model": 5}}}, None,
             "custom division design.model: must be a string, got int"),
            ({"custom": {"design": {"label": "D", "charter": "x", "effort": ["high"]}}}, None,
             "custom division design.effort: must be a string, got list"),
            ({"custom": {"design": {"label": 7, "charter": {"x": 1}}}}, None,
             "custom division design.label: must be a string, got int; "
             "custom division design.charter: must be a string, got dict"),
            ({"custom": []}, None, "setup: custom must be an object"),
            ({"custom": {"design": "Design"}}, None, "custom division design: must be an object"),
            ({"divisions": [5]}, None, "setup: divisions must be a list of division names"),
            ({"stage": ["building"]}, None, "setup: stage must be one of building|public"),
            ({"aliases": "shop"}, None, "setup: aliases must be a list of strings"),
            ({"custom": {"design": {"label": "D", "charter": "x", "model": "claude opus"}}}, None,
             "design.model: must be a model id without spaces"),
            ({"custom": {"design": {"label": "D", "charter": "x", "effort": "huge"}}}, None,
             "design.effort: must be one of"),
            ({"custom": {"design": {"label": "Ops", "charter": "x"}}}, None, "'Ops' is a standard division's"),
            ({"custom": {"design": {"label": "De\tsign", "charter": "x"}}}, None, "design.label: must not contain"),
        ]
        for data, raw, needle in cases:
            code, out, err = self.orche("init", "Acme Shop", "--setup-file", self.write_setup(data, raw))
            self.assertEqual(code, 1, (data, raw, err))
            self.assertIn(needle, err, (data, raw))
            self.assertTrue(err.startswith("orche: ") and err.count("\n") == 1, (data, raw, err))   # one line
            self.assertNotIn("Traceback", out + err)
        code, _, err = self.orche("init", "Acme Shop", "--setup-file", str(self.home / "missing.json"))
        self.assertEqual(code, 1)
        self.assertIn("missing.json: No such file or directory", err)
        code, _, err = self.orche("init", "Acme Shop", "--setup-file", self.write_setup({}), "--divisions", "build")
        self.assertEqual(code, 1)
        self.assertIn("--setup-file holds the whole setup", err)
        self.assertEqual(registry.load()["projects"], {})


# ---- --print-setup ------------------------------------------------------------------------------

class PrintSetupTest(SetupCase):
    def test_unregistered_repo_prints_the_defaults(self):
        code, out, err = self.orche("init", "--print-setup", "Acme Shop")
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out), {"divisions": ["rnd", "research", "build", "gtm"], "custom": {},
                                           "stage": "building", "aliases": []})
        self.assertEqual(registry.load()["projects"], {})

    def test_registered_project_prints_its_setup_and_it_round_trips(self):
        self.assertEqual(self.orche("init", "Acme Shop", "--divisions", "build,gtm", "--aliases", "shop",
                                    "--custom", f"design=Design:{CHARTER}")[0], 0)
        self.calls.clear()
        code, out, err = self.orche("init", "--print-setup", "shop")
        self.assertEqual(code, 0, err)
        setup = json.loads(out)
        self.assertEqual(setup, {"divisions": ["build", "gtm"], "custom": {"design": DESIGN},
                                 "stage": "building", "aliases": ["shop"]})
        self.assertEqual(self.calls, [])   # a registered project needs no Orca call
        setup["divisions"].append("research")
        path = self.home / "setup.json"
        path.write_text(json.dumps(setup))
        code, out, err = self.orche("init", "shop", "--setup-file", str(path))
        self.assertEqual(code, 0, err)
        self.assertNotIn("warning", err)   # the printed setup changes nothing but the added division
        self.assertEqual(self.created(), ["div-research"])

    def test_unknown_repo(self):
        code, _, err = self.orche("init", "--print-setup", "nope")
        self.assertEqual(code, 2)
        self.assertIn("no registry project or Orca repo matches 'nope'", err)


# ---- re-running adds only what is new -----------------------------------------------------------

class RerunTest(SetupCase):
    def snapshot(self):
        files = [p for p in (self.wtroot / "div-build").rglob("*") if p.is_file()]
        files += [self.repo / ".claude" / "agents" / "div-acme-shop-build.md"]
        return {str(p): (p.read_text(), p.stat().st_mtime_ns) for p in files}

    def test_rerun_with_a_custom_adds_only_it(self):
        self.assertEqual(self.orche("init", "Acme Shop", "--divisions", "build")[0], 0)
        before, build_entry = self.snapshot(), self.proj()["divisions"]["build"]
        events = len(registry.load()["events"])
        self.calls.clear()
        code, out, err = self.orche("init", "Acme Shop", "--custom", f"design=Design:{CHARTER}")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.created(), ["div-design"])
        self.assertEqual(self.snapshot(), before)                  # existing files untouched (content and mtime)
        proj = self.proj()
        self.assertEqual(proj["divisions"]["build"], build_entry)  # existing registry entry untouched
        self.assertEqual(registry.divisions_of(proj), ["build", "design"])
        self.assertEqual([e["unit"] for e in registry.load()["events"][events:]], ["design"])
        self.assertIn("Summary: build, design(custom) · stage building · 1 new worktree", out)
        # Same definition again: nothing to do. Another definition: refused, nothing changes.
        self.calls.clear()
        self.assertEqual(self.orche("init", "Acme Shop", "--custom", f"design=Design:{CHARTER}")[0], 0)
        self.assertEqual(self.created(), [])
        code, _, err = self.orche("init", "Acme Shop", "--custom", "design=Design:something else")
        self.assertEqual(code, 1)
        self.assertIn("custom division design already exists with another definition", err)
        self.assertEqual(self.proj()["custom_divisions"]["design"]["charter"], CHARTER)

    def test_rerun_never_removes_and_says_so(self):
        self.assertEqual(self.orche("init", "Acme Shop", "--divisions", "rnd,build")[0], 0)
        path = self.home / "setup.json"
        path.write_text(json.dumps({"divisions": ["build", "gtm"], "stage": "public", "aliases": ["x"]}))
        self.calls.clear()
        code, out, err = self.orche("init", "Acme Shop", "--setup-file", str(path))
        self.assertEqual(code, 0, err)
        self.assertIn("orche: warning: rnd stay(s) onboarded: init never removes a division", err)
        self.assertIn("orche: warning: stage stays building for registered project acme-shop "
                      "(change it with: orche stage set acme-shop public --yes)", err)
        self.assertIn("orche: warning: aliases of registered project acme-shop are not changed by init", err)
        self.assertEqual(self.created(), ["div-gtm"])
        proj = self.proj()
        self.assertEqual((proj["stage"], proj["aliases"], registry.divisions_of(proj)),
                         ("building", [], ["rnd", "build", "gtm"]))

    def test_dry_run_rerun_writes_nothing(self):
        self.assertEqual(self.orche("init", "Acme Shop", "--divisions", "build")[0], 0)
        reg = registry.load()
        self.calls.clear()
        code, _, err = self.orche("--dry-run", "init", "Acme Shop", "--custom", f"design=Design:{CHARTER}")
        self.assertEqual(code, 0, err)
        self.assertIn("worktree create --repo id:repo-acme --name div-design", err)
        self.assertEqual(registry.load(), reg)
        self.assertFalse((self.wtroot / "div-design").exists())


    def test_no_change_rerun_leaves_the_registry_byte_identical(self):
        """M4: an older entry without `custom_divisions` is not rewritten by a no-op re-run."""
        self.assertEqual(self.orche("init", "Acme Shop", "--divisions", "build")[0], 0)
        reg = registry.load()
        del reg["projects"]["acme-shop"]["custom_divisions"]     # an entry written before the field existed
        registry.save(reg)
        path = registry.path()
        before = hashlib.sha256(path.read_bytes()).hexdigest()
        for argv in (("init", "Acme Shop"), ("init", "Acme Shop", "--divisions", "build")):
            code, out, err = self.orche(*argv)
            self.assertEqual(code, 0, err)
            self.assertIn("build: skipped (already onboarded)", out)
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), before, argv)


# ---- session names are unique registry-wide (I2) ------------------------------------------------

class SessionUniquenessTest(SetupCase):
    """Projects `acme-shop` (the fixture repo) and `acme` (a second Orca repo): `div-acme-shop-build` is
    both acme-shop's standard Build session and acme's custom `shop-build` session."""

    def setUp(self):
        super().setUp()
        self.other = self.home / "repos" / "acme"
        self.other.mkdir(parents=True)

    def fake_runner(self, argv, *, timeout, input):
        result = super().fake_runner(argv, timeout=timeout, input=input)
        if argv[:3] != ["orca", "repo", "list"]:
            return result
        data = json.loads(result.out)
        data["result"]["repos"].append({"id": "repo-acme2", "path": str(self.other), "displayName": "Acme",
                                        "kind": "git"})
        return shell.Result(0, json.dumps(data), "", argv)

    def test_custom_division_cannot_take_another_projects_standard_session(self):
        self.assertEqual(self.orche("init", "Acme Shop", "--divisions", "build")[0], 0)
        before = registry.path().read_bytes()
        code, _, err = self.orche("init", "Acme", "--divisions", "rnd", "--custom", "shop-build=Shop:x")
        self.assertEqual(code, 1, err)
        self.assertEqual(err, "orche: session div-acme-shop-build is taken by project acme-shop; pick another name\n")
        # A standard session acme-shop has not onboarded yet is reserved as well.
        code, _, err = self.orche("init", "Acme", "--divisions", "rnd", "--custom", "shop-gtm=Shop:x")
        self.assertEqual(code, 1, err)
        self.assertIn("session div-acme-shop-gtm is taken by project acme-shop", err)
        self.assertEqual(registry.path().read_bytes(), before)
        self.assertNotIn("acme", registry.load()["projects"])

    def test_standard_division_cannot_take_another_projects_custom_session(self):
        self.assertEqual(self.orche("init", "Acme", "--divisions", "rnd", "--custom", "shop-build=Shop:x")[0], 0)
        self.calls.clear()
        code, _, err = self.orche("init", "Acme Shop", "--divisions", "build,gtm")
        self.assertEqual(code, 1, err)
        self.assertEqual(err, "orche: session div-acme-shop-build is taken by project acme; leave build out, or "
                              "rename one of the projects\n")
        self.assertEqual(self.created(), [])
        self.assertEqual(self.orche("init", "Acme Shop", "--divisions", "gtm")[0], 0)   # the free one goes through
        sessions = [u for u in registry.load()["projects"]["acme-shop"]["divisions"].values()]
        self.assertEqual([d["session"] for d in sessions], ["div-acme-shop-gtm"])

    def test_custom_division_cannot_take_another_projects_custom_session(self):
        """N-5: acme-shop's custom `design` and acme's custom `shop-design` share `div-acme-shop-design`."""
        self.assertEqual(self.orche("init", "Acme Shop", "--divisions", "rnd", "--custom", "design=Design:x")[0], 0)
        code, _, err = self.orche("init", "Acme", "--divisions", "rnd", "--custom", "shop-design=Design:x")
        self.assertEqual((code, err), (1, "orche: session div-acme-shop-design is taken by project acme-shop; "
                                          "pick another name\n"))
        self.assertNotIn("acme", registry.load()["projects"])

    def test_custom_division_cannot_take_another_projects_custom_session_other_order(self):
        self.assertEqual(self.orche("init", "Acme", "--divisions", "rnd", "--custom", "shop-design=Design:x")[0], 0)
        code, _, err = self.orche("init", "Acme Shop", "--divisions", "rnd", "--custom", "design=Design:x")
        self.assertEqual((code, err), (1, "orche: session div-acme-shop-design is taken by project acme; "
                                          "pick another name\n"))
        self.assertNotIn("acme-shop", registry.load()["projects"])

    def test_checklist_refuses_a_taken_session_when_the_name_is_typed(self):
        self.assertEqual(self.orche("init", "Acme Shop", "--divisions", "build")[0], 0)
        code, out, err = self.typed("a", "shop-build", "", "", "n", argv=("init", "Acme"))
        self.assertEqual(code, 0, err)
        self.assertIn("  ! session div-acme-shop-build is taken by project acme-shop; pick another name", out)

    def test_checklist_marks_a_taken_default_row_unavailable_before_apply(self):
        """N-3: build is ticked by default, but acme holds `div-acme-shop-build`: shown unticked and
        unavailable on entry, refused on toggle, so Enter + Apply onboards the rest."""
        self.assertEqual(self.orche("init", "Acme", "--divisions", "rnd", "--custom", "shop-build=Shop:x")[0], 0)
        self.calls.clear()
        code, out, err = self.typed("3", "", "", "", "y")
        self.assertEqual(code, 0, err)
        taken = "session div-acme-shop-build is taken by project acme"
        self.assertRegex(out, r" 3 \[ \] build +.*\(unavailable: " + taken + r"\)")
        self.assertIn(f"  build is unavailable: {taken}", out)
        self.assertNotIn("[x] build", out)
        self.assertEqual(sorted(self.proj()["divisions"]), ["gtm", "research", "rnd"])
        self.assertNotIn("div-build", self.created())

    def test_checklist_wake_of_a_taken_row_says_why(self):
        self.assertEqual(self.orche("init", "Acme", "--divisions", "rnd", "--custom", "shop-build=Shop:x")[0], 0)
        code, out, _ = self.typed("", argv=("init", "Acme Shop", "--wake", "build"))
        self.assertIn("  --wake build: build is unavailable (session div-acme-shop-build is taken by project acme)",
                      out)
        self.assertNotIn("acme-shop", registry.load()["projects"])


# ---- --print-setup only prints (M7) -------------------------------------------------------------

class PrintSetupFlagsTest(SetupCase):
    def test_print_setup_with_any_setup_flag_is_refused(self):
        for extra, flag in ((["--custom", "x=y:z"], "--custom"), (["--divisions", "build"], "--divisions"),
                            (["--setup-file", "s.json"], "--setup-file"), (["--stage", "public"], "--stage"),
                            (["--aliases", "x"], "--aliases"), (["--wake", "build"], "--wake")):
            code, out, err = self.orche("init", "--print-setup", *extra, "Acme Shop")
            self.assertEqual((code, out), (1, ""), extra)
            self.assertEqual(err, f"orche: --print-setup only prints; drop {flag}\n")
        code, _, err = self.orche("init", "--print-setup", "--stage", "public", "--wake", "rnd", "Acme Shop")
        self.assertEqual(err, "orche: --print-setup only prints; drop --stage, --wake\n")
        self.assertEqual(self.calls, [])


class SetupModelTest(unittest.TestCase):
    def test_parse_custom_flag_defaults(self):
        self.assertEqual(setup_flow.parse_custom_flag("design=Design:Owns  the\tbrand"),
                         ("design", {"label": "Design", "charter": "Owns the brand", "model": "claude-opus-5-5",
                                     "effort": "high"}))

    def test_summary_counts(self):
        setup = {"divisions": ["build"], "custom": {"qa": DESIGN}, "stage": "public", "aliases": []}
        self.assertEqual(setup_flow.summary(setup, new=1, registered=False),
                         "Summary: build, qa(custom) · stage public · 1 worktree · panes main + liaison")
        self.assertEqual(setup_flow.summary({**setup, "divisions": [], "custom": {}}, new=0, registered=True),
                         "Summary: no divisions · stage public · 0 new worktrees · panes main + liaison")


if __name__ == "__main__":
    unittest.main()
