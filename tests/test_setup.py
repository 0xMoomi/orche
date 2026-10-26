"""Setup wave S2: choosing and customising divisions in `orche init` (stubbed Orca, fake TTY stdin)."""
import hashlib
import io
import json
import os
import shutil
import subprocess
import unittest
from pathlib import Path
from unittest import mock

import support
from orche import __version__ as VERSION
from orche import cmd_init, registry, setup_flow, shell
from orche.hr import onboarding
from test_hr import ALL, HrCase, git_repo

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
        self.assertIn("Summary: rnd, research, build, gtm · stage building · 4 worktrees · pane main · models: "
                      "rnd claude-opus-5-5, research claude-sonnet-5-5, build claude-opus-5-5, gtm claude-sonnet-5-5\n",
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
        self.assertIn("Summary: build · stage building · 0 new worktrees · pane main", out)
        self.assertIn("build: skipped (already onboarded)", out)

    def test_json_on_a_terminal_never_prompts(self):
        code, out, err = self.typed("n", argv=("--json", "--dry-run", "init", "Acme Shop"))
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        self.assertEqual(list(data["divisions"]), ["rnd", "research", "build", "gtm"])
        self.assertEqual(data["summary"], "Summary: rnd, research, build, gtm · stage building · 4 worktrees · "
                                          "pane main · models: rnd claude-opus-5-5, research claude-sonnet-5-5, "
                                          "build claude-opus-5-5, gtm claude-sonnet-5-5")


# ---- interactive checklist ----------------------------------------------------------------------

class InteractiveTest(SetupCase):
    def test_toggle_add_custom_and_apply(self):
        self.real_personas()
        code, out, err = self.typed("a", "design", "Design", CHARTER, "", "", "1 2", "", "", "dw, Acme", "")
        self.assertEqual(code, 0, err)
        self.assertIn("Divisions for acme-shop  (type numbers to toggle, a to add a custom division, Enter to "
                      "continue)\n"
                      " 1 [x] rnd       R&D — prototypes and technical spikes  [claude-opus-5-5]\n"
                      " 2 [x] research  Research — market, users, competitors  [claude-sonnet-5-5]\n"
                      " 3 [x] build     Build — integrates features, owns the ledger, opens PRs  [claude-opus-5-5]\n"
                      " 4 [x] gtm       GTM — launch, content, growth  [claude-sonnet-5-5]\n"
                      " 5 [ ] ops       Ops — deploy and operate (dormant until stage public)  [claude-sonnet-5-5]\n> ",
                      out)
        self.assertIn(" 6 [x] design    Design — owns the visual system, reviews UI chan…  [claude-opus-5-5]\n", out)
        self.assertIn(" 1 [ ] rnd ", out)
        self.assertIn("Summary: build, gtm, design(custom) · stage building · 3 worktrees · pane main · models: "
                      "build claude-opus-5-5, gtm claude-sonnet-5-5, design claude-opus-5-5\n"
                      "Apply? [Y/n] ", out)
        self.assertEqual(self.created(), ["div-build", "div-gtm", "div-design"])
        proj = self.proj()
        self.assertEqual(proj["custom_divisions"], {"design": DESIGN})
        self.assertEqual(registry.divisions_of(proj), ["build", "gtm", "design"])
        self.assertEqual(proj["aliases"], ["dw", "acme"])
        design = proj["divisions"]["design"]
        self.assertEqual((design["status"], design["session"], design["branch"], design["model"]),
                         ("dormant", "div-acme-shop-design", "div/design", "claude-opus-5-5"))
        self.assertNotIn("effort", design)   # the custom spec's effort renders the persona only
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
        self.assertIn("Summary: rnd, research, build, gtm · stage building · 4 worktrees · pane main · "
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
        self.assertIn(" 3 [x] build     Build — integrates features, owns the ledger, opens PRs  [claude-opus-5-5]  (onboarded)", out)
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
        self.assertIn("Summary: build, design(custom), qa(custom) · stage building · 3 worktrees · pane main · wake design", out)

    def test_a_bad_kinds_default_model_refuses_init_before_any_write(self):
        cfg = json.loads((self.home / "config.json").read_text())
        cfg["kinds"] = {"rnd": {"model": "gpt-5"}}
        (self.home / "config.json").write_text(json.dumps(cfg))
        root = Path(os.path.realpath(self.repo))
        code, out, err = self.orche("init", "Acme Shop", "--divisions", "rnd,build", "--no-intake")
        self.assertEqual(code, 1, out)
        self.assertIn("config kinds.rnd.model: unknown model 'gpt-5'", err)
        self.assertIn("Claude ids start with claude-", err)
        self.assertNotIn("acme-shop", registry.load()["projects"])
        self.assertEqual(self.created(), [])
        self.assertFalse((root / ".env.example").exists())

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
            ({"custom": {"legal": {"label": "Legal"}}}, None, "legal.charter: must be one paragraph of 1-400"),
            # 0.8.0: a setup file carries models; each key is a division of the setup, each value a model.
            ({"divisions": ["build"], "models": ["opus"]}, None, "setup: models must be an object"),
            ({"divisions": ["build"], "models": {"gtm": "opus"}}, None, "models: unknown division(s) gtm"),
            ({"divisions": ["build"], "models": {"build": "gpt-4"}}, None, "models.build: unknown model 'gpt-4'"),
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
                                           "stage": "building", "aliases": [], "models": {}})
        self.assertEqual(registry.load()["projects"], {})

    def test_registered_project_prints_its_setup_and_it_round_trips(self):
        self.assertEqual(self.orche("init", "Acme Shop", "--divisions", "build,gtm", "--aliases", "shop",
                                    "--custom", f"design=Design:{CHARTER}")[0], 0)
        self.calls.clear()
        code, out, err = self.orche("init", "--print-setup", "shop")
        self.assertEqual(code, 0, err)
        setup = json.loads(out)
        self.assertEqual(setup, {"divisions": ["build", "gtm"], "custom": {"design": DESIGN},
                                 "stage": "building", "aliases": ["shop"],
                                 "models": {"build": "claude-opus-5-5", "gtm": "claude-sonnet-5-5",
                                            "design": "claude-opus-5-5"}})
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
        self.assertEqual(reg["projects"]["acme-shop"]["orche_version"], VERSION)   # a fresh init stamps
        del reg["projects"]["acme-shop"]["custom_divisions"]     # an entry written before the field existed
        registry.save(reg)
        path = registry.path()
        before = hashlib.sha256(path.read_bytes()).hexdigest()
        for argv in (("init", "Acme Shop"), ("init", "Acme Shop", "--divisions", "build")):
            code, out, err = self.orche(*argv)
            self.assertEqual(code, 0, err)
            self.assertIn("build: skipped (already onboarded)", out)
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), before, argv)


# ---- a re-run upgrades a registered project in place (T10) ---------------------------------------

class UpgradeTest(SetupCase):
    """Real persona templates; a project rendered by an earlier ORCHE (no stamp, outdated personas)."""
    OLD = "---\nname: old\n---\nhand-edited rules\n"
    USER_FILES = (".orche/STANDING.md", "docs/LEDGER.tsv", "PROJECT_CARD.md", "docs/GRAVEYARD.md",
                  "docs/RUBRIC.md", ".env.example")

    def setUp(self):
        super().setUp()
        self.real_personas()
        code, out, err = self.orche("init", "Acme Shop", "--divisions", "build,gtm", "--wake", "build",
                                    "--no-intake")
        self.assertEqual(code, 0, err)
        self.assertNotIn("refreshed", out)
        self.assertNotIn("personas updated", out)            # a fresh init upgrades nothing
        self.root = Path(os.path.realpath(self.repo))
        self.build = [self.root / ".claude" / "agents" / "div-acme-shop-build.md",
                      self.wtroot / "div-build" / ".claude" / "agents" / "div-acme-shop-build.md"]

    def rendered_by(self, version):
        reg = registry.load()
        if version is None:
            del reg["projects"]["acme-shop"]["orche_version"]
        else:
            reg["projects"]["acme-shop"]["orche_version"] = version
        registry.save(reg)
        for path in self.build:
            path.write_text(self.OLD)

    def user_files(self):
        unit = self.wtroot / "div-build" / ".orche" / "div" / "build"
        files = [self.root / rel for rel in self.USER_FILES] + [unit / "CHARTER.md", unit / "DECISIONS.tsv"]
        return {str(p): p.read_bytes() for p in files}

    def test_rerun_rerenders_personas_names_panes_and_leaves_user_files_byte_identical(self):
        self.rendered_by(None)
        unit = self.wtroot / "div-build" / ".orche" / "div" / "build"
        for path in [self.root / rel for rel in self.USER_FILES] + [unit / "CHARTER.md", unit / "DECISIONS.tsv"]:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write("edited by the user\n")
        before = self.user_files()
        code, out, err = self.orche("init", "Acme Shop", "--no-intake")
        self.assertEqual(code, 0, err)
        lines = out.splitlines()
        self.assertEqual([ln for ln in lines if ln.startswith("refreshed ")],
                         [f"refreshed {cmd_init._tilde(p)}" for p in self.build])
        backups = [ln for ln in lines if ln.startswith("backups: ")]
        self.assertEqual(len(backups), 1, lines)
        self.assertIn(f"rendered by an earlier version, now {VERSION}", lines)
        self.assertEqual(lines[-1], "personas updated for main-acme-shop, div-acme-shop-build: "
                                    "read at their next session start")
        self.assertEqual(self.user_files(), before)
        for path in self.build:
            text = path.read_text()
            self.assertTrue(text.startswith("---\nname: div-acme-shop-build\n"))
            self.assertIn("\n---\n<!-- generated by ORCHE from templates/personas/div-build.md: ", text)
        backup_dir = self.home / "backups" / "acme-shop"
        self.assertEqual(backups[0], f"backups: {cmd_init._tilde(next(backup_dir.iterdir()))}")
        self.assertEqual(sorted((p.name, p.read_text()) for p in next(backup_dir.iterdir()).iterdir()),
                         [("div-acme-shop-build.main.md", self.OLD), ("div-acme-shop-build.worktree.md", self.OLD)])
        self.assertEqual(registry.load()["projects"]["acme-shop"]["orche_version"], VERSION)

    def test_json_keys(self):
        self.rendered_by("0.0.1")
        code, out, err = self.orche("--json", "init", "Acme Shop", "--no-intake")
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        self.assertEqual(data["refreshed"], [str(p) for p in self.build])
        self.assertEqual(data["kept"], [])
        self.assertEqual(data["rendered_by"], {"before": "0.0.1", "now": VERSION})
        self.assertEqual(data["restart"], ["main-acme-shop", "div-acme-shop-build"])
        code, out, _ = self.orche("--json", "init", "Acme Shop", "--no-intake")   # nothing left to do
        data = json.loads(out)
        self.assertEqual((data["refreshed"], data["rendered_by"], data["restart"]), ([], None, []))

    def test_dry_run_rerun_writes_no_stamp_and_no_persona(self):
        self.rendered_by(None)
        reg_bytes = registry.path().read_bytes()
        code, out, err = self.orche("--dry-run", "init", "Acme Shop", "--no-intake")
        self.assertEqual(code, 0, err)
        self.assertEqual([ln for ln in out.splitlines() if "refresh" in ln],
                         [f"would refresh {cmd_init._tilde(p)}" for p in self.build])
        self.assertNotIn("personas updated", out)
        self.assertEqual(registry.path().read_bytes(), reg_bytes)
        self.assertTrue(all(p.read_text() == self.OLD for p in self.build))
        self.assertFalse((self.home / "backups").exists())

    def test_card_link_pointing_elsewhere_is_relinked(self):
        link = self.home / "cards" / "acme-shop.md"
        link.unlink()
        link.symlink_to(self.home / "elsewhere.md")
        code, out, err = self.orche("init", "Acme Shop", "--no-intake")
        self.assertEqual(code, 0, err)
        self.assertEqual(os.readlink(link), str(self.root / "PROJECT_CARD.md"))
        self.assertIn(f"refreshed {cmd_init._tilde(link)}", out.splitlines())

    def test_an_unsafe_target_in_the_refresh_stops_init(self):
        agents = self.build[1].parent
        shutil.rmtree(agents)
        agents.symlink_to(self.root / ".claude" / "agents")
        code, _, err = self.orche("init", "Acme Shop", "--no-intake")
        self.assertEqual(code, 3)
        self.assertIn("orche: init acme-shop stopped: ", err)
        self.assertIn("(re-run orche init acme-shop to resume)", err)


# ---- session names are unique registry-wide (I2) ------------------------------------------------

class SessionUniquenessTest(SetupCase):
    """Projects `acme-shop` (the fixture repo) and `acme` (a second Orca repo): `div-acme-shop-build` is
    both acme-shop's standard Build session and acme's custom `shop-build` session."""

    def setUp(self):
        super().setUp()
        self.other = self.home / "repos" / "acme"
        self.other.mkdir(parents=True)

    def fake_runner(self, argv, *, timeout, input, env=None):
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


# ---- C7 A2: an unlisted git repo is added to Orca; init lands on Main ---------------------------

class AddRepoTest(SetupCase):
    """`orche init <path>` for a git repo Orca does not list adds it first (`orca repo add`)."""

    def setUp(self):
        super().setUp()
        self.new = self.home / "repos" / "new-app"
        (self.new / ".git").mkdir(parents=True)
        self.listed_after_add = True
        self.list_error_after_add = None
        self.display = "new-app"
        self.added = []

    def fake_runner(self, argv, *, timeout, input, env=None):
        if argv[:3] == ["orca", "repo", "add"]:
            self.calls.append(argv)
            self.added.append(argv[argv.index("--path") + 1])
            return shell.Result(0, json.dumps({"ok": True, "result": {}}), "", argv)
        result = super().fake_runner(argv, timeout=timeout, input=input)
        if argv[:3] == ["orca", "repo", "list"] and self.added and self.list_error_after_add:
            return shell.Result(0, json.dumps({"ok": False, "error": {"message": self.list_error_after_add}}),
                                "", argv)
        if argv[:3] == ["orca", "repo", "list"] and self.added and self.listed_after_add:
            data = json.loads(result.out)
            data["result"]["repos"].append({"id": "repo-new", "path": self.added[-1], "displayName": self.display,
                                            "kind": "git"})
            return shell.Result(0, json.dumps(data), "", argv)
        return result

    def test_unlisted_git_repo_is_added_then_onboarded(self):
        code, out, err = self.orche("init", str(self.new), "--divisions", "build", "--no-intake")
        self.assertEqual(code, 0, err)
        self.assertNotIn("warning", err)
        real = os.path.realpath(self.new)
        self.assertEqual(self.orca_calls("repo add"), [["orca", "repo", "add", "--path", real, "--json"]])
        self.assertEqual(len(self.orca_calls("repo list")), 2)   # the lookup, then the re-read after the add
        proj = registry.load()["projects"]["new-app"]
        self.assertEqual((proj["repo_id"], proj["path"]), ("repo-new", real))
        self.assertEqual(out.splitlines()[0], f"added {real} to Orca")

    def test_a_submodule_git_file_counts_as_a_repo(self):
        os.rmdir(self.new / ".git")
        (self.new / ".git").write_text("gitdir: ../../super/.git/modules/new-app\n")
        self.assertEqual(self.orche("init", str(self.new), "--no-intake")[0], 0)
        self.assertEqual(len(self.orca_calls("repo add")), 1)

    def test_a_linked_worktree_is_refused_and_names_the_main_checkout(self):
        main, linked = self.home / "repos" / "mainrepo", self.home / "repos" / "linked"
        main.mkdir(parents=True)
        git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-C", str(main)]
        for argv in (["init", "-q"], ["commit", "-q", "--allow-empty", "-m", "x"],
                     ["worktree", "add", "-q", str(linked)]):
            subprocess.run(git + argv, check=True, capture_output=True)
        for argv in (("init", str(linked), "--no-intake"), ("init", "--print-setup", str(linked))):
            code, out, err = self.orche(*argv)
            self.assertEqual(code, 1, argv)
            self.assertEqual(err, f"orche: {os.path.realpath(linked)} is a git worktree of "
                                  f"{os.path.realpath(main)}; put ORCHE on the main checkout\n")
        self.assertEqual(self.orca_calls("repo add"), [])
        self.assertEqual(self.orche("init", str(main), "--no-intake")[0], 0)   # the main checkout is fine

    def test_added_but_not_listed_fails_with_the_path(self):
        self.listed_after_add = False
        code, out, err = self.orche("init", str(self.new), "--no-intake")
        real = os.path.realpath(self.new)
        self.assertEqual(code, 1)
        self.assertEqual(err, f"orche: added {real} to Orca but orca repo list does not show it\n")
        self.assertEqual(self.orca_calls("worktree create"), [])
        self.assertNotIn("new-app", registry.load().get("projects", {}))

    def test_a_failed_list_after_the_add_names_the_path(self):
        self.list_error_after_add = "orca is busy"
        code, _, err = self.orche("init", str(self.new), "--no-intake")
        self.assertEqual(code, 1)
        self.assertEqual(err, f"orche: added {os.path.realpath(self.new)} to Orca but orca repo list failed: "
                              "orca_error: orca is busy\n")
        self.assertEqual(self.orca_calls("worktree create"), [])

    def test_json_says_whether_the_repo_was_added_to_orca(self):
        code, out, err = self.orche("init", str(self.new), "--no-intake", "--json")
        self.assertEqual(code, 0, err)
        self.assertIs(json.loads(out)["added_to_orca"], True)
        code, out, err = self.orche("init", str(self.new), "--no-intake", "--json")   # now listed: no add
        self.assertEqual(code, 0, err)
        self.assertIs(json.loads(out)["added_to_orca"], False)
        self.assertEqual(len(self.orca_calls("repo add")), 1)

    def test_an_orca_display_name_other_than_the_folder_name_says_so_once(self):
        self.display = "Shiny App"
        code, out, err = self.orche("init", str(self.new), "--divisions", "build", "--no-intake")
        self.assertEqual(code, 0, err)
        self.assertEqual(out.splitlines()[1], "registered as shiny-app (Orca names the repo Shiny App)")
        self.assertIn("shiny-app", registry.load()["projects"])
        self.assertEqual(self.created(), ["div-build"])

    def test_an_aborted_checklist_leaves_orca_untouched(self):
        code, out, err = self.typed("", "", "", "n", argv=("init", str(self.new)))
        self.assertEqual((code, err), (0, ""))
        self.assertTrue(out.endswith("aborted; nothing written\n"), out)
        self.assertEqual(self.orca_calls("repo add"), [])
        self.assertEqual(registry.load()["projects"], {})

    def test_a_path_without_git_keeps_the_not_found_failure(self):
        plain = self.home / "plain"
        plain.mkdir()
        code, _, err = self.orche("init", str(plain), "--no-intake")
        self.assertEqual(code, 2)
        self.assertIn("no registry project or Orca repo matches", err)
        self.assertEqual(self.orca_calls("repo add"), [])

    def test_registered_project_whose_repo_left_orca_keeps_the_repo_missing_failure(self):
        self.assertEqual(self.orche("init", "Acme Shop", "--divisions", "build", "--no-intake")[0], 0)
        reg = registry.load()   # registered at a git path Orca no longer lists
        reg["projects"]["acme-shop"].update(repo_id="repo-gone", path=str(self.new))
        registry.save(reg)
        self.calls.clear()
        code, _, err = self.orche("init", "acme-shop", "--no-intake")
        self.assertEqual(code, 2)
        self.assertIn("is registered as acme-shop but its Orca repo is not in", err)
        self.assertEqual(self.orca_calls("repo add"), [])

    def test_a_registered_path_whose_repo_left_orca_is_repo_missing_and_never_added(self):
        self.assertEqual(self.orche("init", "Acme Shop", "--divisions", "build", "--no-intake")[0], 0)
        reg = registry.load()
        reg["projects"]["acme-shop"].update(repo_id="repo-gone", path=str(self.new))
        registry.save(reg)
        self.calls.clear()
        for argv in (("init", str(self.new), "--no-intake"), ("init", "--print-setup", str(self.new))):
            code, _, err = self.orche(*argv)
            self.assertEqual(code, 2, argv)
            self.assertEqual(err, f"orche: {str(self.new)!r} is registered as acme-shop but its Orca repo is not "
                                  "in `orca repo list`\n")
        self.assertEqual(self.orca_calls("repo add"), [])

    def test_dry_run_adds_nothing_and_says_it_would(self):
        code, out, err = self.orche("--dry-run", "init", str(self.new), "--no-intake")
        self.assertEqual(code, 0, err)
        real = os.path.realpath(self.new)
        lines = out.splitlines()
        self.assertEqual(lines[0], f"would add {real} to Orca (orca repo add --path {real})")
        self.assertTrue(lines[2].startswith("Summary: "), lines)
        self.assertEqual(lines[-2], "would switch to main-new-app")
        self.assertEqual([c for c in self.calls if c[1:3] in (["repo", "add"], ["terminal", "switch"],
                                                              ["terminal", "create"], ["worktree", "create"])], [])
        self.assertIn("id:pending::", err)   # the placeholder repo id in the printed create argvs

    def test_print_setup_of_an_unlisted_git_repo_prints_the_defaults_and_adds_nothing(self):
        code, out, err = self.orche("init", "--print-setup", str(self.new))
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out), setup_flow.defaults())
        self.assertEqual(self.orca_calls("repo add"), [])


class BareInitTest(SetupCase):
    """`orche init` with no argument: the git repository holding the current directory."""

    def setUp(self):
        super().setUp()
        self.here = git_repo(self.home / "repos" / "cwd-app")

    def fake_runner(self, argv, *, timeout, input, env=None):
        result = super().fake_runner(argv, timeout=timeout, input=input)
        if argv[:3] == ["orca", "repo", "list"]:
            data = json.loads(result.out)
            data["result"]["repos"].append({"id": "repo-cwd", "path": str(self.here), "displayName": "cwd-app",
                                            "kind": "git"})
            return shell.Result(0, json.dumps(data), "", argv)
        return result

    def from_dir(self, cwd, *argv):
        """`orche <argv>` run with `cwd` as the current directory (restored afterwards)."""
        before = os.getcwd()
        os.chdir(cwd)
        try:
            return self.orche(*argv)
        finally:
            os.chdir(before)

    def test_bare_init_inside_a_repo_onboards_it_under_the_folder_name(self):
        sub = self.here / "src"
        sub.mkdir()
        code, out, err = self.from_dir(sub, "init", "--divisions", "build", "--no-intake")
        self.assertEqual(code, 0, err)
        proj = registry.load()["projects"]["cwd-app"]
        self.assertEqual((proj["repo_id"], proj["path"]), ("repo-cwd", os.path.realpath(self.here)))
        self.assertEqual(self.created(), ["div-build"])
        self.assertEqual(self.orca_calls("repo add"), [])
        self.assertEqual(out.splitlines()[-2], "Main is open in the main-cwd-app tab with the setup summary; from here on, talk to Main there.")

    def test_bare_init_outside_any_repo_asks_for_the_path(self):
        plain = self.home / "plain"
        plain.mkdir()
        # GIT_CEILING_DIRECTORIES: git never looks above the temp home, wherever the suite runs
        with mock.patch.dict(os.environ, {"GIT_CEILING_DIRECTORIES": str(self.home)}):
            for argv in (("init", "--no-intake"), ("init", "--print-setup")):
                code, out, err = self.from_dir(plain, *argv)
                self.assertEqual((code, out), (1, ""), argv)
                self.assertEqual(err, "orche: not inside a git repository: pass the project path "
                                      "(orche init <path>)\n")
        self.assertEqual(registry.load().get("projects", {}), {})
        self.assertEqual(self.calls, [])

    def test_bare_print_setup_inside_a_repo_prints_the_defaults(self):
        code, out, err = self.from_dir(self.here, "init", "--print-setup")
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out), setup_flow.defaults())
        self.assertEqual(registry.load().get("projects", {}), {})

    def test_bare_init_in_a_linked_worktree_is_refused_and_names_the_main_checkout(self):
        linked = self.home / "repos" / "cwd-app-wt"
        subprocess.run(["git", "-C", str(self.here), "worktree", "add", "-q", str(linked)], check=True,
                       capture_output=True)
        code, out, err = self.from_dir(linked, "init", "--no-intake")
        self.assertEqual(code, 1)
        self.assertEqual(err, f"orche: {os.path.realpath(linked)} is a git worktree of "
                              f"{os.path.realpath(self.here)}; put ORCHE on the main checkout\n")
        self.assertEqual(self.calls, [])


class LandOnMainTest(SetupCase):
    """A live init ends with one `terminal switch` to `main-<proj>`; dry-run never switches."""

    def test_switches_once_to_main_at_the_very_end(self):
        code, out, err = self.orche("init", "Acme Shop", "--divisions", "build", "--no-intake")
        self.assertEqual(code, 0, err)
        self.assertNotIn("warning", err)
        self.assertEqual(out.splitlines()[-2], "Main is open in the main-acme-shop tab with the setup summary; from here on, talk to Main there.")
        self.assertEqual(self.orca_calls("terminal switch"),
                         [["orca", "terminal", "switch", "--terminal", "t-main-acme-shop", "--json"]])
        writes = [" ".join(c[1:3]) for c in self.calls if c[1] in ("terminal", "automations", "worktree")
                  and c[2] not in ("list",)]
        self.assertEqual(writes[-1], "terminal switch")   # after both panes were created
        [main] = [c for c in self.orca_calls("terminal create") if c[c.index("--title") + 1] == "main-acme-shop"]
        self.assertIn("--permission-mode bypassPermissions --autocompact 200000 --name main-acme-shop", main[main.index("--command") + 1])

    def test_open_panes_are_not_switched_to_before_the_end(self):
        self.terminals = [{"handle": "t1", "title": "main-acme-shop"}]
        code, out, err = self.orche("init", "Acme Shop", "--divisions", "build", "--no-intake")
        self.assertEqual(code, 0, err)
        self.assertNotIn("warning", err)
        self.assertEqual(self.orca_calls("terminal create"), [])
        self.assertEqual(self.orca_calls("terminal switch"), [["orca", "terminal", "switch", "--terminal", "t1", "--json"]])

    def test_json_reports_switched(self):
        code, out, err = self.orche("--json", "init", "Acme Shop", "--divisions", "build", "--no-intake")
        self.assertEqual(code, 0, err)
        self.assertIs(json.loads(out)["switched"], True)
        code, out, err = self.orche("--json", "--dry-run", "init", "Acme Shop", "--no-intake")
        self.assertEqual(code, 0, err)
        self.assertIs(json.loads(out)["switched"], False)

    def test_a_failed_switch_is_one_warning_and_init_still_succeeds(self):
        real = self.fake_runner

        def runner(argv, *, timeout, input, env=None):
            if argv[1:3] == ["terminal", "switch"]:
                self.calls.append(argv)
                return shell.Result(1, json.dumps({"ok": False, "error": {"code": "boom", "message": "no window"}}),
                                    "", argv)
            return real(argv, timeout=timeout, input=input)
        shell.RUNNER = runner
        code, out, err = self.orche("--json", "init", "Acme Shop", "--divisions", "build", "--no-intake")
        self.assertEqual(code, 0, err)
        warnings = [l for l in err.splitlines() if l.startswith("orche: warning:")]
        self.assertEqual(warnings, [self.SWITCH_WARNING], err)
        self.assertIs(json.loads(out)["switched"], False)
        self.assertEqual(registry.divisions_of(self.proj()), ["build"])

    SWITCH_WARNING = "orche: warning: could not switch to main-acme-shop; open that tab in Orca"

    def test_a_failed_switch_never_claims_main_is_open(self):
        real = self.fake_runner

        def runner(argv, *, timeout, input, env=None):
            if argv[1:3] == ["terminal", "switch"]:
                self.calls.append(argv)
                return shell.Result(1, json.dumps({"ok": False, "error": {"code": "boom", "message": "no window"}}),
                                    "", argv)
            return real(argv, timeout=timeout, input=input)
        shell.RUNNER = runner
        code, out, err = self.orche("init", "Acme Shop", "--divisions", "build", "--no-intake")
        self.assertEqual(code, 0, err)
        self.assertEqual([l for l in err.splitlines() if l.startswith("orche: warning:")], [self.SWITCH_WARNING], err)
        self.assertNotIn("Main is open", out)

    def test_a_missing_main_tab_warns_and_does_not_switch(self):
        real = self.fake_runner

        def runner(argv, *, timeout, input, env=None):
            result = real(argv, timeout=timeout, input=input)
            self.terminals[:] = [t for t in self.terminals if t["title"] != "main-acme-shop"]
            return result
        shell.RUNNER = runner
        code, out, err = self.orche("init", "Acme Shop", "--divisions", "build", "--no-intake")
        self.assertEqual(code, 0, err)
        self.assertEqual([l for l in err.splitlines() if l.startswith("orche: warning:")], [self.SWITCH_WARNING], err)
        self.assertNotIn("Main is open", out)
        self.assertEqual(self.orca_calls("terminal switch"), [])
        code, out, err = self.orche("--json", "init", "Acme Shop", "--divisions", "build", "--no-intake")
        self.assertIs(json.loads(out)["switched"], False, err)

    def test_dry_run_prints_would_switch_and_switches_nothing(self):
        self.terminals = [{"handle": "t1", "title": "main-acme-shop"}]
        code, out, err = self.orche("--dry-run", "init", "Acme Shop", "--no-intake")
        self.assertEqual(code, 0, err)
        self.assertEqual(out.splitlines()[-2], "would switch to main-acme-shop")
        self.assertEqual(self.orca_calls("terminal switch"), [])


class SetupModelTest(unittest.TestCase):
    def test_parse_custom_flag_defaults(self):
        self.assertEqual(setup_flow.parse_custom_flag("design=Design:Owns  the\tbrand"),
                         ("design", {"label": "Design", "charter": "Owns the brand", "model": "claude-opus-5-5",
                                     "effort": "high"}))

    def test_summary_counts(self):
        setup = {"divisions": ["build"], "custom": {"qa": DESIGN}, "stage": "public", "aliases": []}
        self.assertEqual(setup_flow.summary(setup, new=1, registered=False),
                         "Summary: build, qa(custom) · stage public · 1 worktree · pane main")
        self.assertEqual(setup_flow.summary({**setup, "divisions": [], "custom": {}}, new=0, registered=True),
                         "Summary: no divisions · stage public · 0 new worktrees · pane main")

    def test_models_take_the_custom_spec_then_the_record_then_the_recommendation(self):
        setup = {"divisions": ["rnd", "build"], "custom": {"qa": {**DESIGN, "model": "codex"}},
                 "stage": "building", "aliases": []}
        existing = {"divisions": {"rnd": {"model": "codex:gpt-5.5"}}}
        models = setup_flow.models(setup, existing, cfg={})
        self.assertEqual(models, {"rnd": "codex:gpt-5.5", "build": "claude-opus-5-5", "qa": "codex"})
        self.assertEqual(setup_flow.summary(setup, new=1, registered=True, models=models),
                         "Summary: rnd, build, qa(custom) · stage building · 1 new worktree · pane main · "
                         "models: rnd codex:gpt-5.5, build claude-opus-5-5, qa codex")

    def test_checklist_row_shows_the_recorded_model(self):
        setup = {"divisions": ["rnd"], "custom": {}, "stage": "building", "aliases": []}
        c = setup_flow.Checklist("acme", setup, ["rnd"], io.StringIO(), io.StringIO(),
                                 recorded={"rnd": "codex"})
        self.assertEqual(c.describe("rnd"), "R&D — prototypes and technical spikes  [codex]  (onboarded)")
        self.assertTrue(c.describe("research").endswith("[claude-sonnet-5-5]"))


if __name__ == "__main__":
    unittest.main()


class SetupFileModelTest(support.TempHomeCase):
    def test_a_chosen_model_of_a_custom_division_replaces_its_spec_model(self):
        base = setup_flow.defaults()
        setup = setup_flow.from_data({"custom": {"design": {}}, "models": {"design": "claude-sonnet-5-5"}}, base)
        self.assertEqual(setup["custom"]["design"]["model"], "claude-sonnet-5-5")
        self.assertEqual(setup_flow.models(setup)["design"], "claude-sonnet-5-5")

    def test_the_readme_setup_json_with_a_picked_design_parses(self):
        readme = (support.ROOT / "README.md").read_text()
        self.assertIn('{"divisions": [...], "custom": {"<kind>": {}}, "models": {"<division>": "<model>"}}', readme)
        self.assertNotIn("--lang", readme)
        data = {"divisions": ["rnd"], "custom": {"design": {}}, "models": {"rnd": "claude-sonnet-5-5"}}
        setup = setup_flow.from_data(data, setup_flow.defaults())
        self.assertIn("design", setup["custom"])
        self.assertEqual(setup_flow.names(setup), ["rnd", "design"])
        self.assertEqual(setup_flow.models(setup)["rnd"], "claude-sonnet-5-5")

    def test_a_custom_spec_model_is_checked_strictly_at_write(self):
        with self.assertRaises(setup_flow.SetupError) as ctx:
            setup_flow.custom_spec("design", {"model": "gpt-4"})
        self.assertTrue(any(e.startswith("custom division design.model: unknown model 'gpt-4'")
                            for e in ctx.exception.errors), ctx.exception.errors)
