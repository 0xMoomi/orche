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
    def build(self, r, key="acme", lang="ko", avail=OK, cfg=None, machine=None):
        return choices.build(r, cfg or copy.deepcopy(config.DEFAULTS), key, lang, avail,
                             machine or {"ram_mb": 65536, "free_disk_mb": 200000})

    def test_scale_question(self):
        ko = self.build(reg())
        e = ko["ask"]["scale"]
        self.assertEqual(labels(e), ["기본 (지금)", "20개까지", "30개까지"])
        self.assertEqual(e["tool"]["header"], "규모")
        self.assertEqual(e["values"]["30개까지"], "30")
        self.assertEqual(e["tool"]["question"], "에이전트를 몇 개까지 동시에 쓸까요?")
        self.assertEqual(ko["scale"]["current"], "default")
        en = self.build(reg(), lang="en", machine={"ram_mb": 24576, "free_disk_mb": 23552})
        self.assertEqual(en["ask"]["scale"]["tool"]["question"], "How many agents should run at once? "
                         "(this Mac: 24 GB RAM · 23 GB free disk — tight for 30)")
        self.assertEqual(labels(en["ask"]["scale"])[0], "Default (now)")
        tight = self.build(None, key=None, machine={"ram_mb": 16384, "free_disk_mb": None})
        self.assertIn("이 Mac: RAM 16GB · 디스크 여유 ?GB", tight["ask"]["scale"]["tool"]["question"])

    def test_a_new_project_setup_and_pick(self):
        data = self.build(None, key=None)
        self.assertIsNone(data["ask"]["add"])
        self.assertIsNone(data["ask"]["change"])
        setup, models = data["ask"]["setup"]
        self.assertEqual(setup["tool"]["question"], "이 프로젝트를 어떤 부서로 시작할까요?")
        self.assertEqual(labels(setup), ["기본 4개 (권장)", "가볍게 2개", "직접 고르기"])
        self.assertEqual(setup["values"]["기본 4개 (권장)"], ["rnd", "research", "build", "gtm"])
        self.assertEqual(labels(models)[0], "부서에 맞춰서 (권장)")
        std, extra = data["ask"]["pick"]
        self.assertTrue(std["tool"]["multiSelect"])
        self.assertEqual(labels(extra)[-1], "없음")
        self.assertEqual(extra["values"]["없음"], None)

    def test_b_add_offers_the_suggested_kinds_and_a_new_one(self):
        data = self.build(reg(research={"model": SONNET}, build={"model": OPUS}))
        add = data["ask"]["add"]
        self.assertEqual(add["tool"]["question"], "어떤 부서를 추가할까요? (여러 개 골라도 돼요)")
        self.assertEqual(labels(add), ["R&D", "GTM", "운영", "새 이름으로 만들기"])
        self.assertEqual(add["values"]["새 이름으로 만들기"], "*")
        en = self.build(reg(research={}, build={}, rnd={}, gtm={}), lang="en")
        self.assertEqual(labels(en["ask"]["add"]), ["Ops", "Design", "Content", "A new one"])

    def test_c_gtm_on_opus_offers_sonnet_first_and_keep_last(self):
        data = self.build(reg(gtm={"model": OPUS}, build={"model": OPUS}))
        gtm = data["ask"]["model"]["gtm"]
        self.assertEqual(gtm["tool"]["question"], "GTM 부서를 어떤 모델로 바꿀까요? (지금: Opus 5.5)")
        self.assertEqual(labels(gtm), ["Sonnet 5.5 (권장)", "Codex", "그대로 두기"])
        self.assertEqual(gtm["values"], {"Sonnet 5.5 (권장)": SONNET, "Codex": "codex", "그대로 두기": None})
        change = data["ask"]["change"]
        self.assertEqual([o["description"] for o in change["tool"]["options"]],
                         ["지금 Opus 5.5 (권장 모델)", "지금 Opus 5.5 · 권장 Sonnet 5.5"])

    def test_d_build_on_its_recommendation_keeps_first(self):
        data = self.build(reg(build={"model": OPUS}))
        self.assertEqual(labels(data["ask"]["model"]["build"]), ["그대로 두기 (권장)", "Sonnet 5.5", "Codex"])

    def test_e_a_new_kind_and_the_star_entry(self):
        data = self.build(reg(build={}), lang="en")
        self.assertEqual(data["ask"]["model"]["design"]["tool"]["question"], "Which model should Design run on?")
        self.assertEqual(labels(data["ask"]["model"]["*"])[0], "Opus 5.5 (recommended)")
        self.assertEqual(data["custom"]["recommended"], OPUS)
        self.assertIn("2–16 characters", data["custom"]["rules"])

    def test_codex_notes(self):
        for avail, text in ((ABSENT, choices.TEXTS["ko"]["codex_absent"]),
                            (BROKEN, choices.TEXTS["ko"]["codex_broken"])):
            data = self.build(reg(build={"model": OPUS}), avail=avail)
            entry = data["ask"]["model"]["build"]
            self.assertNotIn("Codex", labels(entry))
            self.assertTrue(entry["tool"]["question"].endswith(text))
            self.assertEqual(data["models"][2]["reason"], text)
        data = self.build(reg(build={"model": OPUS, "status": "active"}))
        entry = data["ask"]["model"]["build"]
        self.assertNotIn("Codex", labels(entry))
        self.assertTrue(entry["tool"]["question"].endswith(choices.TEXTS["ko"]["codex_active"]))

    def test_guardrails_hold_everywhere(self):
        many = reg(**{n: {"model": OPUS} for n in ("rnd", "research", "build", "gtm", "ops")})
        for r, key in ((None, None), (reg(), "acme"), (reg(build={}), "acme"), (many, "acme")):
            for lang in ("ko", "en"):
                for avail in (OK, ABSENT):
                    for e in entries(self.build(r, key=key, lang=lang, avail=avail)):
                        labs = labels(e)
                        self.assertLessEqual(len(e["tool"]["header"]), 12)
                        self.assertTrue(2 <= len(labs) <= 4, labs)
                        self.assertEqual(len(set(labs)), len(labs))
                        self.assertFalse(any(", " in lab for lab in labs))
                        self.assertEqual(set(e["values"]), set(labs))
        self.assertIn("(그 밖: 운영 — 이름을 적어 주세요)", self.build(many)["ask"]["change"]["tool"]["question"])
        self.assertIsNone(choices._entry("q", "h", False, [("only", "", 1, "x")]))

    def test_a_recorded_custom_spec_wins_over_its_kind(self):
        r = reg(design={"model": OPUS})
        r["projects"]["acme"]["custom_divisions"] = {
            "design": {"label": "Brand Studio", "charter": "Owns the brand kit.", "model": OPUS, "effort": "high"}}
        kind = {k["key"]: k for k in self.build(r, lang="en")["kinds"]}["design"]
        self.assertEqual((kind["label"], kind["charter"]), ("Brand Studio", "Owns the brand kit."))
        self.assertTrue(kind["desc"])   # the spec has no desc: the kind's stays

    def test_all_on_a_codex_recommendation_needs_codex(self):
        cfg = copy.deepcopy(config.DEFAULTS)
        cfg["kinds"]["custom"] = {**cfg["kinds"]["custom"], "model": "codex"}
        models = self.build(None, key=None, avail=OK, cfg=cfg)["ask"]["setup"][1]
        self.assertIn("모두 Codex", labels(models))
        for avail, text in ((ABSENT, choices.TEXTS["ko"]["codex_absent"]),
                            (BROKEN, choices.TEXTS["ko"]["codex_broken"])):
            models = self.build(None, key=None, avail=avail, cfg=cfg)["ask"]["setup"][1]
            self.assertEqual(models["values"], {"부서에 맞춰서 (권장)": "recommended", "부서마다 고르기": "pick"})
            self.assertTrue(models["tool"]["question"].endswith(text))

    def test_hidden_divisions_get_no_model_question(self):
        data = self.build(reg(build={"status": "retired"}, research={"status": "onboarding"}))
        self.assertIsNone(data["ask"]["model"]["build"])
        self.assertIsNone(data["ask"]["model"]["research"])


class KindsDataTest(unittest.TestCase):
    def test_standard_labels_match_the_registry(self):
        for name, label in registry.STANDARD_LABELS.items():
            self.assertEqual(config.DEFAULTS["kinds"][name]["label"]["en"], label)

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

    def test_lang_defaults_to_config_and_unknown_project_exits_2(self):
        code, out, _ = self.orche("--json", "hr", "choices")
        self.assertEqual((code, json.loads(out)["lang"]), (0, "en"))
        code, _, err = self.orche("hr", "choices", "nope")
        self.assertEqual(code, 2)
        self.assertIn("unknown project nope; for a project not under ORCHE yet, run it without a project", err)

    def test_first_install_asks_the_default_four_first(self):
        """R-PLAIN-3: the install prompt runs `orche hr choices --lang ko --json` right after install, before any
        project is registered; its first setup question offers the default four first, as the recommendation."""
        self.assertEqual(registry.load()["projects"], {})
        code, out, err = self.orche("hr", "choices", "--lang", "ko", "--json")
        self.assertEqual(code, 0, err)
        first = json.loads(out)["ask"]["setup"][0]
        label = first["tool"]["options"][0]["label"]
        self.assertEqual(label, "기본 4개 (권장)")
        self.assertEqual(first["values"][label], list(setup_flow.DEFAULT_DIVISIONS))
        self.assertEqual(registry.load()["projects"], {})

    def test_choices_writes_nothing(self):
        self.assertEqual(self.orche("init", "Acme Shop", "--divisions", "build", "--no-intake")[0], 0)
        before = {p: os.stat(p).st_mtime_ns for p in self.home.rglob("*") if p.is_file()}
        self.assertEqual(self.orche("--json", "hr", "choices", "acme-shop", "--lang", "ko")[0], 0)
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
