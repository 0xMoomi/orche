"""0.9.0 scale presets (lib/orche/scale.py, `orche hr scale`): one name sets every cap that limits how many
agents run at once; a dry run writes nothing; applying records an HR `scale` event."""
import json

import test_hr
from orche import config, registry, scale

KEYS = ("runner.max_parallel", "runner.max_parallel_total", "runner.min_free_mb", "runner.queue_wait_s",
        "feature_panes", "feature_min_free_disk_mb", "flow.fanout", "flow.depth", "flow.briefs_per_hour",
        "flow.revisions", "flow.runs_per_unit", "flow.max_unit_cost_usd", "flow.max_project_cost_usd")
LIMITS = ("per_project_per_day", "per_project_per_week", "per_request_auto", "global_per_day_freeze",
          "max_awake_sessions")
# 0.9.0's presets as they were typed in full (KEYS, then LIMITS): the differences resolve to exactly these.
FULL = {"default": ((4, 8, 2048, 1800, False, 2048, 5, 3, 20, 3, 4, 10, 30), (8, 40, 4, 20, 12)),
        "20": ((12, 16, 2048, 7200, False, 8192, 20, 3, 60, 3, 4, 10, 150), (20, 60, 20, 40, 12)),
        "30": ((20, 24, 3072, 7200, False, 10240, 30, 3, 90, 3, 4, 10, 250), (30, 90, 30, 60, 12))}


class ScaleTest(test_hr.HrCase):
    def setUp(self):
        super().setUp()
        cfg = self.cfg()
        del cfg["feature_panes"]   # the default preset staffs headless workers
        (self.home / "config.json").write_text(json.dumps(cfg))
        reg = self.sample()
        reg["limits"] = dict(registry.LIMIT_DEFAULTS)   # a fresh registry's limits are the default preset's
        self.save(reg)

    def cfg(self):
        return json.loads((self.home / "config.json").read_text())

    def test_list_and_json(self):
        code, out, _ = self.orche("hr", "scale")
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines()[0], "scale default")
        self.assertIn("* default: runs 4/project, 8 total", out)
        code, out, _ = self.orche("hr", "scale", "--json")
        data = json.loads(out)
        self.assertEqual((data["current"], sorted(data["presets"])), ("default", ["20", "30", "default"]))

    def test_dry_run_writes_nothing(self):
        before, reg_before = self.cfg(), registry.load()
        code, out, _ = self.orche("--dry-run", "hr", "scale", "30")
        self.assertEqual(code, 0, out)
        first = out.splitlines()[0]
        self.assertTrue(first.startswith("would set scale 30 (was default): "), first)
        self.assertIn("limits.per_project_per_day 8 → 30 · limits.per_project_per_week 40 → 90 · "
                      "limits.per_request_auto 4 → 30", first)
        self.assertIn("runner.max_parallel 4 → 20", first)
        self.assertGreaterEqual(int(first.rsplit("(", 2)[1].split()[0]), 12, first)
        self.assertIn("cost: about $60 an hour", out)
        self.assertEqual(self.cfg(), before)
        self.assertEqual(registry.load()["limits"], reg_before["limits"])
        self.assertEqual(registry.load()["events"], [])

    def test_apply_30_then_back_to_default(self):
        code, out, _ = self.orche("hr", "scale", "30")
        self.assertEqual(code, 0, out)
        cfg, reg = config.load(), registry.load()
        self.assertEqual(scale.current(cfg, reg), "30")
        self.assertEqual(cfg["runner"]["max_parallel"], 20)
        self.assertIs(cfg["feature_panes"], False)
        self.assertEqual(reg["limits"]["per_project_per_day"], 30)
        self.assertEqual(reg["events"][-1]["type"], "scale")
        self.assertIn("scale 30 by ", (self.home / "hr-log.md").read_text())
        self.assertEqual(self.orche("hr", "scale", "default")[0], 0)
        self.assertEqual(scale.current(config.load(), registry.load()), "default")

    def test_each_preset_resolves_to_its_full_set(self):
        cfg = config.load()
        self.assertEqual(cfg["scale"]["presets"]["default"], {})
        for name, (conf, lims) in FULL.items():
            c, lim = scale.target(cfg, name)
            self.assertEqual(list(c.items()), list(zip(KEYS, conf)), name)
            self.assertEqual(list(lim.items()), list(zip(LIMITS, lims)), name)
        self.assertEqual(scale.summary(cfg, "30"),
                         "runs 20/project, 24 total · features headless · spend cap $250 · min free 3072 MB")

    def test_unknown_preset(self):
        code, _, err = self.orche("hr", "scale", "99")
        self.assertEqual(code, 2)
        self.assertIn("unknown scale 99; pick one of default, 20, 30", err)
