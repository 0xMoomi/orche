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
from orche import registry, roster, util

SAMPLE = json.loads((support.FIXTURES / "registry.sample.json").read_text())
ORCHE_BIN = support.ROOT / "bin" / "orche.py"   # the Python entry point behind the bin/orche launcher


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

    def test_division_effort_is_neither_required_nor_refused(self):
        reg = sample()   # the sample's records carry no effort (C2 T6); one written before still validates
        self.assertFalse([d for d, v in reg["projects"]["acme-shop"]["divisions"].items() if "effort" in v])
        self.assertEqual(registry.validate(reg), [])
        reg["projects"]["acme-shop"]["divisions"]["build"]["effort"] = "high"
        self.assertEqual(registry.validate(reg), [])

    def test_non_object(self):
        self.assertEqual(registry.validate([]), ["registry: must be an object"])

    def test_orche_version_is_optional_and_a_version(self):
        reg = sample()
        self.assertNotIn("orche_version", reg["projects"]["acme-shop"])   # absent: valid
        self.assertNotIn("orche_version", registry.PROJECT_STR_KEYS)
        reg["projects"]["acme-shop"]["orche_version"] = "0.6.0"
        self.assertEqual(registry.validate(reg), [])
        for bad in ("x", 6, "0.6", "v0.6.0", "0.6.0\n"):
            reg["projects"]["acme-shop"]["orche_version"] = bad
            self.assertEqual(registry.validate(reg),
                             ["projects.acme-shop.orche_version: must be a version like 0.6.0"], bad)

    def test_old_liaison_session_key_is_ignored(self):
        """C8: a project written before the Liaison left keeps its `liaison_session`; it validates and names no member."""
        reg = sample()
        reg["projects"]["acme-shop"]["liaison_session"] = "liaison-x"
        self.assertEqual(registry.validate(reg), [])
        self.assertIsNone(roster.Roster(reg).member("liaison-x"))


DESIGN = {"label": "Design", "charter": "Owns the visual system.", "model": "claude-opus-5-5", "effort": "high"}


def with_custom(reg, name="design", spec=None, record=True):
    proj = reg["projects"]["acme-shop"]
    proj["custom_divisions"] = {name: dict(spec or DESIGN)}
    if record:
        proj["divisions"][name] = {"status": "dormant", "worktree": f"repo-acme::/srv/orca/acme-shop-{name}",
                                   "branch": f"div/{name}", "session": f"div-acme-shop-{name}",
                                   "model": "claude-opus-5-5",
                                   "last_activity": "2026-10-16T09:00:00+09:00"}
    return reg


class CustomDivisionsTest(unittest.TestCase):
    def test_custom_division_record_is_valid_and_divisions_of_orders_it_last(self):
        reg = with_custom(sample())
        self.assertEqual(registry.validate(reg), [])
        self.assertEqual(registry.divisions_of(reg["projects"]["acme-shop"]), ["rnd", "build", "design"])
        self.assertEqual(registry.divisions_of(sample()["projects"]["docs-site"]), [])
        reg["projects"]["acme-shop"]["divisions"]["design"]["status"] = "active"
        self.assertIn("div-acme-shop-design", registry.awake_sessions(reg))   # counts toward the awake limit

    def test_project_without_the_field_is_valid(self):
        reg = sample()
        self.assertNotIn("custom_divisions", reg["projects"]["acme-shop"])
        self.assertEqual(registry.validate(reg), [])

    def test_division_record_needs_a_custom_definition(self):
        reg = with_custom(sample())
        del reg["projects"]["acme-shop"]["custom_divisions"]["design"]
        self.assertTrue(any("unknown division 'design'" in e for e in registry.validate(reg)))

    def test_bad_custom_definitions(self):
        cases = {
            "Design": ("custom_divisions: custom division name 'Design' must be a slug", DESIGN),
            "d": ("must be a slug of 2-16", DESIGN),
            "a-very-long-name-xx": ("must be a slug of 2-16", DESIGN),
            "build": ("'build' is a standard division", DESIGN),
            "hr": ("'hr' is reserved", DESIGN),
            "design": ("design.label: must be one line of 1-24", {**DESIGN, "label": "x" * 25}),
            "design2": ("design2.charter: must be one paragraph of 1-400", {**DESIGN, "charter": "x" * 401}),
            "design3": ("design3.charter: must be one paragraph", {**DESIGN, "charter": "a\nb"}),
            "design4": ("design4.effort: must be one of low|medium|high|xhigh|max", {**DESIGN, "effort": "huge"}),
            "design5": ("design5.model: must be a model id without spaces", {**DESIGN, "model": "claude opus"}),
            "design6": ("design6.color: unknown key", {**DESIGN, "color": "red"}),
            "ab-": ("custom division name 'ab-' must be a slug", DESIGN),
            "a--b": ("custom division name 'a--b' must be a slug", DESIGN),
            "design7": ("design7.label: must not contain line breaks or control characters",
                        {**DESIGN, "label": "De\x1b[31msign"}),
            "design8": ("design8.charter: must not contain control characters", {**DESIGN, "charter": "a\x07b"}),
            "design9": ("design9.label: 'Research' is a standard division's name or label", {**DESIGN, "label": "Research"}),
            "design10": ("design10.model: must be a model id without spaces", {**DESIGN, "model": "claude\x00x"}),
        }
        for name, (needle, spec) in cases.items():
            errors = registry.validate(with_custom(sample(), name, spec, record=False))
            self.assertTrue(any(needle in e for e in errors), (name, errors))
        for name in ("qa", "a1", "web-app", "a-b-c", "x" * 16, "d2-v3"):
            self.assertEqual(registry.custom_name_errors(name), [], name)


class CustomRoundTripTest(support.TempHomeCase):
    def test_custom_round_trip_through_save_and_load(self):
        reg = with_custom(sample())
        registry.save(reg)
        self.assertEqual(registry.load(), reg)
        self.assertEqual(registry.load()["projects"]["acme-shop"]["custom_divisions"]["design"], DESIGN)


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
        self.assertEqual(sorted(p.name for p in self.home.iterdir()), ["config.json", "registry.json"])

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
        self.assertEqual(self.orche("version"), (0, "orche 0.6.1\n", ""))

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
