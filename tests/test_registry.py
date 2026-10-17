import copy
import importlib.machinery
import importlib.util
import io
import json
import subprocess
import sys
import unittest
from contextlib import redirect_stdout

import support
from orche import registry, util

SAMPLE = json.loads((support.FIXTURES / "registry.sample.json").read_text())
ORCHE_BIN = support.ROOT / "bin" / "orche"


def sample():
    return copy.deepcopy(SAMPLE)


class ValidateTest(unittest.TestCase):
    def test_sample_and_empty_are_valid(self):
        self.assertEqual(registry.validate(sample()), [])
        self.assertEqual(registry.validate(registry.empty()), [])

    def test_missing_stage(self):
        reg = sample()
        del reg["projects"]["acme-shop"]["stage"]
        errors = registry.validate(reg)
        self.assertTrue(any("projects.acme-shop.stage" in e for e in errors), errors)

    def test_bad_stage_value(self):
        reg = sample()
        reg["projects"]["acme-shop"]["stage"] = "beta"
        self.assertTrue(any(".stage" in e for e in registry.validate(reg)))

    def test_bad_division_name(self):
        reg = sample()
        divs = reg["projects"]["acme-shop"]["divisions"]
        divs["marketing"] = divs.pop("rnd")
        errors = registry.validate(reg)
        self.assertTrue(any("unknown division 'marketing'" in e for e in errors), errors)

    def test_feature_without_scope(self):
        reg = sample()
        del reg["projects"]["acme-shop"]["divisions"]["build"]["features"][0]["scope"]
        errors = registry.validate(reg)
        self.assertTrue(any("features[0].scope" in e for e in errors), errors)

    def test_feature_with_empty_scope(self):
        reg = sample()
        reg["projects"]["acme-shop"]["divisions"]["build"]["features"][0]["scope"] = []
        self.assertTrue(any("features[0].scope" in e for e in registry.validate(reg)))

    def test_features_only_under_build(self):
        reg = sample()
        reg["projects"]["acme-shop"]["divisions"]["rnd"]["features"] = []
        self.assertTrue(any("only allowed under build" in e for e in registry.validate(reg)))

    def test_bad_statuses_limits_and_events(self):
        reg = sample()
        reg["projects"]["acme-shop"]["divisions"]["rnd"]["status"] = "asleep"
        reg["limits"]["max_awake_sessions"] = "12"
        reg["frozen"] = "no"
        reg["events"][0]["type"] = "explode"
        reg["version"] = 2
        errors = registry.validate(reg)
        for needle in ("rnd.status", "limits.max_awake_sessions", "frozen", "events[0].type", "version"):
            self.assertTrue(any(needle in e for e in errors), (needle, errors))

    def test_non_object(self):
        self.assertEqual(registry.validate([]), ["registry: must be an object"])


class LookupAndEventsTest(unittest.TestCase):
    def test_project_by_key_and_alias_case_insensitive(self):
        reg = sample()
        self.assertEqual(registry.project(reg, "ACME-SHOP")[0], "acme-shop")
        self.assertEqual(registry.project(reg, "acme")[0], "acme-shop")
        self.assertEqual(registry.project(reg, "Shop")[0], "acme-shop")
        self.assertEqual(registry.project(reg, "docs-site")[0], "docs-site")
        self.assertIsNone(registry.project(reg, "unknown"))

    def test_append_event(self):
        reg = sample()
        ev = registry.append_event(reg, type="sleep", project="acme-shop", unit="rnd", by="hr")
        self.assertEqual(len(reg["events"]), 2)
        self.assertEqual(reg["events"][-1], ev)
        self.assertIn("at", ev)
        self.assertEqual(registry.validate(reg), [])

    def test_append_event_rejects_bad_type(self):
        reg = sample()
        with self.assertRaises(registry.RegistryError):
            registry.append_event(reg, type="explode", project="p", unit="u", by="b")
        self.assertEqual(len(reg["events"]), 1)

    def test_awake_sessions(self):
        self.assertEqual(registry.awake_sessions(sample()),
                         ["div-acme-shop-build", "feat-acme-shop-checkout"])

    def test_awake_sessions_counts_features_under_inactive_division(self):
        reg = sample()
        reg["projects"]["acme-shop"]["divisions"]["build"]["status"] = "dormant"
        self.assertEqual(registry.awake_sessions(reg), ["feat-acme-shop-checkout"])


class LoadSaveTest(support.TempHomeCase):
    def test_load_missing_returns_empty(self):
        self.assertEqual(registry.load(), registry.empty())

    def test_save_then_load_roundtrip(self):
        registry.save(sample())
        self.assertEqual(registry.load(), SAMPLE)
        self.assertEqual(sorted(p.name for p in self.home.iterdir()), ["registry.json"])

    def test_save_rejects_invalid(self):
        reg = sample()
        del reg["projects"]["acme-shop"]["stage"]
        with self.assertRaises(registry.RegistryError):
            registry.save(reg)
        self.assertFalse(registry.path().exists())

    def test_load_corrupt_json_is_registry_error(self):
        registry.path().write_text("{broken")
        with self.assertRaises(registry.RegistryError) as ctx:
            registry.load()
        self.assertIn(str(registry.path()), str(ctx.exception))
        self.assertIn("invalid JSON", str(ctx.exception))

    def test_load_rejects_invalid_file(self):
        util.write_json(registry.path(), {"version": 1})
        with self.assertRaises(registry.RegistryError):
            registry.load()


class CliTest(support.TempHomeCase):
    def orche(self, *args):
        cp = subprocess.run([sys.executable, str(ORCHE_BIN), *args], capture_output=True, text=True,
                            timeout=30)
        return cp.returncode, cp.stdout, cp.stderr

    def test_version(self):
        self.assertEqual(self.orche("version"), (0, "orche 0.1.0\n", ""))

    def test_projects_json_empty_home(self):
        self.assertEqual(self.orche("projects", "--json"), (0, "[]\n", ""))
        self.assertEqual(self.orche("--json", "projects")[1], "[]\n")

    def test_projects_lists_registry(self):
        registry.save(sample())
        code, out, _ = self.orche("projects", "--json")
        self.assertEqual(code, 0)
        rows = json.loads(out)
        self.assertEqual([(r["key"], r["stage"]) for r in rows],
                         [("acme-shop", "building"), ("docs-site", "public")])
        self.assertEqual(rows[0]["aliases"], ["Acme", "shop"])
        code, out, _ = self.orche("projects")
        self.assertEqual(out.splitlines(), ["acme-shop\tbuilding\tAcme,shop", "docs-site\tpublic\t"])

    def test_invalid_registry_is_user_error(self):
        util.write_json(registry.path(), {"version": 1})
        code, out, err = self.orche("projects")
        self.assertEqual((code, out), (1, ""))
        self.assertIn("invalid registry", err)

    def test_unknown_command_is_user_error(self):
        code, _, err = self.orche("nope")
        self.assertEqual(code, 1)
        self.assertIn("invalid choice", err)

    def test_corrupt_registry_is_one_line_user_error(self):
        registry.path().write_text("{broken")
        code, out, err = self.orche("projects")
        self.assertEqual((code, out), (1, ""))
        self.assertEqual(len(err.splitlines()), 1, err)
        self.assertIn("invalid JSON", err)


def load_cli():
    loader = importlib.machinery.SourceFileLoader("orche_cli", str(ORCHE_BIN))
    cli = importlib.util.module_from_spec(importlib.util.spec_from_loader("orche_cli", loader))
    loader.exec_module(cli)
    return cli


class DiscoveryTest(support.TempHomeCase):
    def setUp(self):
        super().setUp()
        self.cli = load_cli()
        self.lib_dir = self.home / "cmds"
        self.lib_dir.mkdir()
        self.addCleanup(sys.modules.pop, "orche.cmd_probe", None)

    def probe(self, body):
        (self.lib_dir / "cmd_probe.py").write_text(body)

    def test_discovers_cmd_modules(self):
        self.probe("def register(sub):\n"
                   "    p = sub.add_parser('zzprobe')\n"
                   "    p.set_defaults(func=lambda a: print('probe', a.json, a.dry_run) or 3)\n")
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = self.cli.main(["zzprobe", "--dry-run", "--json"], lib_dir=self.lib_dir)
        self.assertEqual((code, buf.getvalue()), (3, "probe True True\n"))

    def test_rejects_redeclared_global_flags(self):
        for flag in ("--dry-run", "--json"):
            self.probe("def register(sub):\n"
                       "    p = sub.add_parser('zzprobe')\n"
                       f"    p.add_argument('{flag}', action='store_true')\n")
            with self.assertRaises(RuntimeError) as ctx:
                self.cli.build_parser(self.lib_dir)
            self.assertIn(f"redeclares global flag {flag}", str(ctx.exception))

    def test_rejects_redeclared_flag_in_nested_subcommand(self):
        self.probe("def register(sub):\n"
                   "    p = sub.add_parser('zzprobe')\n"
                   "    inner = p.add_subparsers().add_parser('leaf')\n"
                   "    inner.add_argument('--dry-run', action='store_true')\n")
        with self.assertRaises(RuntimeError) as ctx:
            self.cli.build_parser(self.lib_dir)
        self.assertIn("'zzprobe leaf'", str(ctx.exception))

if __name__ == "__main__":
    unittest.main()
