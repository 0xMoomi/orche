"""orche hr choices (design 2026-10-23 §10.3, Appendix D): the choices JSON, the owner's questions
(`ask`) and their guardrails; kinds as config data; setup files that carry models."""
import copy
import io
import json
import os
import unittest
from unittest import mock

import support  # noqa: F401  (puts lib/ on sys.path)

from orche import config, registry, setup_flow, vendors
from orche.hr import choices
from test_hr import HrCase

OK = vendors.Availability("ok", "codex-cli 0.50.0", "")
ABSENT = vendors.Availability("absent", "", vendors.INSTALL)
BROKEN = vendors.Availability("broken", "boom", vendors.UPGRADE)
OPUS, SONNET = "claude-opus-5-5", "claude-sonnet-5-5"


def reg(**divs):
    return {"projects": {"acme": {"stage": "building", "custom_divisions": {},
                                  "divisions": {n: dict({"status": "dormant", "session": f"div-acme-{n}"}, **d)
                                                for n, d in divs.items()}}}}


def labels(entry):
    return [o["label"] for o in entry["tool"]["options"]]


def entries(data):
    ask = data["ask"]
    out = [ask["add"], ask["change"], *ask["model"].values(), *(ask["setup"] or []), *(ask["pick"] or []), ask["scale"]]
    return [e for e in out if e is not None]


class BuildTest(unittest.TestCase):
    def build(self, r, key="acme", avail=OK, cfg=None, machine=None):
        return choices.build(r, cfg or copy.deepcopy(config.DEFAULTS), key, avail,
                             machine or {"ram_mb": 65536, "free_disk_mb": 200000})

    def test_scale_question(self):
        data = self.build(reg())
        e = data["ask"]["scale"]
        self.assertEqual(labels(e), ["Default (now)", "Up to 20", "Up to 30"])
        self.assertEqual(e["tool"]["header"], "Scale")
        self.assertEqual(e["values"]["Up to 30"], "30")
        self.assertEqual(e["tool"]["question"], "How many agents should run at once?")
        self.assertEqual(data["scale"]["current"], "default")
        tight = self.build(reg(), machine={"ram_mb": 24576, "free_disk_mb": 23552})
        self.assertEqual(tight["ask"]["scale"]["tool"]["question"], "How many agents should run at once? "
                         "(this Mac: 24 GB RAM · 23 GB free disk — tight for 30)")
        tight = self.build(None, key=None, machine={"ram_mb": 16384, "free_disk_mb": None})
        self.assertIn("this Mac: 16 GB RAM · ? GB free disk", tight["ask"]["scale"]["tool"]["question"])

    def test_a_new_project_setup_and_pick(self):
        data = self.build(None, key=None)
        self.assertIsNone(data["ask"]["add"])
        self.assertIsNone(data["ask"]["change"])
        setup, models = data["ask"]["setup"]
        self.assertEqual(setup["tool"]["question"], "Which divisions should this project start with?")
        self.assertEqual(labels(setup), ["The default 4 (recommended)", "Light: 2", "Pick them"])
        self.assertEqual(setup["values"]["The default 4 (recommended)"], ["rnd", "research", "build", "gtm"])
        self.assertEqual(labels(models)[0], "Fitted to each (recommended)")
        std, extra = data["ask"]["pick"]
        self.assertTrue(std["tool"]["multiSelect"])
        self.assertEqual(labels(extra)[-1], "None")
        self.assertEqual(extra["values"]["None"], None)

    def test_b_add_offers_the_suggested_kinds_and_a_new_one(self):
        data = self.build(reg(research={"model": SONNET}, build={"model": OPUS}))
        add = data["ask"]["add"]
        self.assertEqual(add["tool"]["question"], "Which divisions should I add? (pick any)")
        self.assertEqual(labels(add), ["R&D", "GTM", "Ops", "A new one"])
        self.assertEqual(add["values"]["A new one"], "*")
        full = self.build(reg(research={}, build={}, rnd={}, gtm={}))
        self.assertEqual(labels(full["ask"]["add"]), ["Ops", "Design", "Content", "A new one"])

    def test_c_gtm_on_opus_offers_sonnet_first_and_keep_last(self):
        data = self.build(reg(gtm={"model": OPUS}, build={"model": OPUS}))
        gtm = data["ask"]["model"]["gtm"]
        self.assertEqual(gtm["tool"]["question"], "Which model should GTM switch to? (now: Opus 5.5)")
        self.assertEqual(labels(gtm), ["Sonnet 5.5 (recommended)", "Codex", "Keep"])
        self.assertEqual(gtm["values"], {"Sonnet 5.5 (recommended)": SONNET, "Codex": "codex", "Keep": None})
        change = data["ask"]["change"]
        self.assertEqual([o["description"] for o in change["tool"]["options"]],
                         ["now Opus 5.5 (recommended)", "now Opus 5.5 · recommended Sonnet 5.5"])

    def test_d_build_on_its_recommendation_keeps_first(self):
        data = self.build(reg(build={"model": OPUS}))
        self.assertEqual(labels(data["ask"]["model"]["build"]), ["Keep (recommended)", "Sonnet 5.5", "Codex"])

    def test_e_a_new_kind_and_the_star_entry(self):
        data = self.build(reg(build={}))
        self.assertEqual(data["ask"]["model"]["design"]["tool"]["question"], "Which model should Design run on?")
        self.assertEqual(labels(data["ask"]["model"]["*"])[0], "Opus 5.5 (recommended)")
        self.assertEqual(data["custom"]["recommended"], OPUS)
        self.assertIn("2–16 characters", data["custom"]["rules"])

    def test_codex_notes(self):
        for avail, text in ((ABSENT, choices.TEXTS["codex_absent"]),
                            (BROKEN, choices.TEXTS["codex_broken"])):
            data = self.build(reg(build={"model": OPUS}), avail=avail)
            entry = data["ask"]["model"]["build"]
            self.assertNotIn("Codex", labels(entry))
            self.assertTrue(entry["tool"]["question"].endswith(text))
            self.assertEqual(data["models"][2]["reason"], text)
        data = self.build(reg(build={"model": OPUS, "status": "active"}))
        entry = data["ask"]["model"]["build"]
        self.assertNotIn("Codex", labels(entry))
        self.assertTrue(entry["tool"]["question"].endswith(choices.TEXTS["codex_active"]))

    def test_guardrails_hold_everywhere(self):
        many = reg(**{n: {"model": OPUS} for n in ("rnd", "research", "build", "gtm", "ops")})
        for r, key in ((None, None), (reg(), "acme"), (reg(build={}), "acme"), (many, "acme")):
            for avail in (OK, ABSENT):
                for e in entries(self.build(r, key=key, avail=avail)):
                    labs = labels(e)
                    self.assertLessEqual(len(e["tool"]["header"]), 12)
                    self.assertTrue(2 <= len(labs) <= 4, labs)
                    self.assertEqual(len(set(labs)), len(labs))
                    self.assertFalse(any(", " in lab for lab in labs))
                    self.assertEqual(set(e["values"]), set(labs))
        self.assertIn("(also: Ops — type the name)", self.build(many)["ask"]["change"]["tool"]["question"])
        self.assertIsNone(choices._entry("q", "h", False, [("only", "", 1, "x")]))

    def test_a_recorded_custom_spec_wins_over_its_kind(self):
        r = reg(design={"model": OPUS})
        r["projects"]["acme"]["custom_divisions"] = {
            "design": {"label": "Brand Studio", "charter": "Owns the brand kit.", "model": OPUS, "effort": "high"}}
        kind = {k["key"]: k for k in self.build(r)["kinds"]}["design"]
        self.assertEqual((kind["label"], kind["charter"]), ("Brand Studio", "Owns the brand kit."))
        self.assertTrue(kind["desc"])   # the spec has no desc: the kind's stays

    def test_all_on_a_codex_recommendation_needs_codex(self):
        cfg = copy.deepcopy(config.DEFAULTS)
        cfg["kinds"]["custom"] = {**cfg["kinds"]["custom"], "model": "codex"}
        models = self.build(None, key=None, avail=OK, cfg=cfg)["ask"]["setup"][1]
        self.assertIn("All Codex", labels(models))
        for avail, text in ((ABSENT, choices.TEXTS["codex_absent"]),
                            (BROKEN, choices.TEXTS["codex_broken"])):
            models = self.build(None, key=None, avail=avail, cfg=cfg)["ask"]["setup"][1]
            self.assertEqual(models["values"], {"Fitted to each (recommended)": "recommended", "Per division": "pick"})
            self.assertTrue(models["tool"]["question"].endswith(text))

    def test_hidden_divisions_get_no_model_question(self):
        data = self.build(reg(build={"status": "retired"}, research={"status": "onboarding"}))
        self.assertIsNone(data["ask"]["model"]["build"])
        self.assertIsNone(data["ask"]["model"]["research"])


class KindsDataTest(unittest.TestCase):
    def test_standard_labels_match_the_registry(self):
        for name, label in registry.STANDARD_LABELS.items():
            self.assertEqual(config.DEFAULTS["kinds"][name]["label"], label)

    def test_checklist_lines_are_unchanged(self):
        out = io.StringIO()
        cl = setup_flow.Checklist("acme", setup_flow.defaults(), [], io.StringIO(), out)
        rows = [cl.describe(n).split("  [")[0] for n in registry.DIVISIONS]
        self.assertEqual(rows, ["R&D — prototypes and technical spikes", "Research — market, users, competitors",
                                "Build — integrates features, owns the ledger, opens PRs",
                                "GTM — launch, content, growth", "Ops — deploy and operate (dormant until stage public)"])


class CliTest(HrCase):
    def setUp(self):
        super().setUp()
        self.avail = OK
        p = mock.patch.object(vendors, "available", lambda cfg=None: self.avail)
        p.start()
        self.addCleanup(p.stop)
        from orche import procs
        p = mock.patch.object(procs, "spawn_detached", lambda argv, log, **env: None)
        p.start()
        self.addCleanup(p.stop)

    def test_english_texts_and_unknown_project_exits_2(self):
        code, out, _ = self.orche("--json", "hr", "choices")
        self.assertEqual((code, "lang" in json.loads(out)), (0, False))
        code, _, err = self.orche("hr", "choices", "nope")
        self.assertEqual(code, 2)
        self.assertIn("unknown project nope; for a project not under ORCHE yet, run it without a project", err)

    def test_first_install_asks_the_default_four_first(self):
        """R-PLAIN-3: the install prompt runs `orche hr choices --json` right after install, before any
        project is registered; its first setup question offers the default four first, as the recommendation."""
        self.assertEqual(registry.load()["projects"], {})
        code, out, err = self.orche("hr", "choices", "--json")
        self.assertEqual(code, 0, err)
        first = json.loads(out)["ask"]["setup"][0]
        label = first["tool"]["options"][0]["label"]
        self.assertEqual(label, "The default 4 (recommended)")
        self.assertEqual(first["values"][label], list(setup_flow.DEFAULT_DIVISIONS))
        self.assertEqual(registry.load()["projects"], {})

    def test_choices_writes_nothing(self):
        self.assertEqual(self.orche("init", "Acme Shop", "--divisions", "build", "--no-intake")[0], 0)
        before = {p: os.stat(p).st_mtime_ns for p in self.home.rglob("*") if p.is_file()}
        self.assertEqual(self.orche("--json", "hr", "choices", "acme-shop")[0], 0)
        self.assertEqual(self.orche("hr", "choices", "acme-shop")[0], 0)
        after = {p: os.stat(p).st_mtime_ns for p in self.home.rglob("*") if p.is_file()}
        self.assertEqual(before, after)

    def write_setup(self, data):
        path = self.home / "setup.json"
        path.write_text(json.dumps(data))
        return str(path)

    def test_setup_file_models_apply_refuse_a_change_and_need_codex(self):
        f = self.write_setup({"divisions": ["build", "gtm"], "models": {"gtm": OPUS}})
        code, _, err = self.orche("init", "Acme Shop", "--setup-file", f, "--no-intake")
        self.assertEqual(code, 0, err)
        self.assertEqual(registry.load()["projects"]["acme-shop"]["divisions"]["gtm"]["model"], OPUS)
        f = self.write_setup({"divisions": ["build", "gtm"], "models": {"gtm": SONNET}})
        code, _, err = self.orche("init", "Acme Shop", "--setup-file", f, "--no-intake")
        self.assertEqual(code, 1)
        self.assertIn(f"acme-shop/gtm is on {OPUS}; change it with orche hr model acme-shop gtm {SONNET}", err)
        self.avail = ABSENT
        f = self.write_setup({"divisions": ["build", "gtm", "rnd"], "models": {"rnd": "codex"}})
        code, _, err = self.orche("init", "Acme Shop", "--setup-file", f, "--no-intake")
        self.assertEqual(code, 3)
        self.assertNotIn("codex", registry.load()["projects"]["acme-shop"]["divisions"].get("rnd", {}).get("model", ""))


if __name__ == "__main__":
    unittest.main()
