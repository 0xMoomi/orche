"""0.9.0 scale presets (lib/orche/scale.py, `orche hr scale`): one name sets every cap that limits how many
agents run at once; a dry run writes nothing; applying records an HR `scale` event."""
import json

import test_hr
from orche import config, registry, scale


class ScaleTest(test_hr.HrCase):
    def setUp(self):
        super().setUp()
        self.save(self.sample())

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

    def test_unknown_preset(self):
        code, _, err = self.orche("hr", "scale", "99")
        self.assertEqual(code, 2)
        self.assertIn("unknown scale 99; pick one of default, 20, 30", err)
