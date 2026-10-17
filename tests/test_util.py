import datetime as dt
import json
import os
import stat
import unittest
from pathlib import Path

import support
from orche import config, log, paths, util


class SlugAndIdsTest(unittest.TestCase):
    def test_slugify(self):
        self.assertEqual(util.slugify("Acme Shop!"), "acme-shop")
        self.assertEqual(util.slugify("  --Foo__Bar 2.0-- "), "foo-bar-2-0")

    def test_unit_id(self):
        self.assertEqual(util.unit_id("Acme Shop", "Fix Login", 3, date=dt.date(2026, 10, 16)),
                         "acme-shop-fix-login-20261016-3")

    def test_now_iso_has_offset(self):
        parsed = dt.datetime.fromisoformat(util.now_iso())
        self.assertIsNotNone(parsed.tzinfo)


class JsonIoTest(support.TempHomeCase):
    def test_atomic_write_and_json_roundtrip(self):
        target = self.home / "nested" / "data.json"
        util.write_json(target, {"a": 1, "k": "한"})
        self.assertEqual(util.read_json(target), {"a": 1, "k": "한"})
        util.atomic_write(target, "replaced")
        self.assertEqual(target.read_text(), "replaced")
        self.assertEqual([p.name for p in target.parent.iterdir()], ["data.json"])

    def test_new_file_follows_umask(self):
        old = os.umask(0o022)
        try:
            util.atomic_write(self.home / "STATUS.md", "x")
        finally:
            os.umask(old)
        self.assertEqual(stat.S_IMODE((self.home / "STATUS.md").stat().st_mode), 0o644)

    def test_existing_file_keeps_mode(self):
        target = self.home / "LEDGER.tsv"
        target.write_text("a")
        target.chmod(0o640)
        util.atomic_write(target, "b")
        self.assertEqual((target.read_text(), stat.S_IMODE(target.stat().st_mode)), ("b", 0o640))

    def test_explicit_mode_for_secrets(self):
        target = self.home / "secret.env"
        util.atomic_write(target, "k=v", mode=0o600)
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)

    def test_read_json_corrupt_raises_typed_error(self):
        bad = self.home / "bad.json"
        bad.write_text("{not json")
        with self.assertRaises(util.JsonError) as ctx:
            util.read_json(bad)
        self.assertEqual(ctx.exception.path, str(bad))

    def test_read_json_default(self):
        self.assertEqual(util.read_json(self.home / "missing.json", default={}), {})


class ParseKvBlockTest(unittest.TestCase):
    BLOCK = (
        "TO        div-acme-build\n"
        "GOAL      Ship checkout retry\n"
        "SCOPE     src/checkout/**\n"
        "ACCEPT    retries 3 times\n"
        "  surfaces error after the third\n"
        "  logs each attempt\n"
        "TIER      standard\n"
    )

    def test_keys_and_continuations(self):
        fields, unknown = util.parse_kv_block(self.BLOCK)
        self.assertEqual(unknown, [])
        self.assertEqual(fields["TO"], "div-acme-build")
        self.assertEqual(fields["GOAL"], "Ship checkout retry")
        self.assertEqual(fields["ACCEPT"],
                         "retries 3 times\nsurfaces error after the third\nlogs each attempt")
        self.assertEqual(fields["TIER"], "standard")

    def test_key_with_value_only_on_continuation(self):
        fields, _ = util.parse_kv_block("CONTEXT\n  line one\n  line two\n")
        self.assertEqual(fields["CONTEXT"], "line one\nline two")

    def test_unknown_keys_collected_not_raised(self):
        fields, unknown = util.parse_kv_block("TO x\nPRIORITY high\n  ignored continuation\nGOAL y\n")
        self.assertEqual(unknown, ["PRIORITY"])
        self.assertEqual(fields, {"TO": "x", "GOAL": "y"})

    def test_amend_and_stop(self):
        fields, unknown = util.parse_kv_block("AMEND acme-x-20261016-1 rev 2\nTO div-acme-build\n")
        self.assertEqual((fields["AMEND"], unknown), ("acme-x-20261016-1 rev 2", []))
        fields, _ = util.parse_kv_block("STOP acme-x-20261016-1\n")
        self.assertEqual(fields["STOP"], "acme-x-20261016-1")


class FindFencedBlockTest(unittest.TestCase):
    def test_last_block_wins(self):
        text = ("intro\n```dispatch\nTO a\n```\nmiddle\n```python\nprint(1)\n```\n"
                "```dispatch\nTO b\nGOAL g\n```\nend")
        self.assertEqual(util.find_fenced_block(text, "dispatch"), "TO b\nGOAL g")
        self.assertEqual(util.find_fenced_block(text, "python"), "print(1)")

    def test_none_when_absent(self):
        self.assertIsNone(util.find_fenced_block("no fences here", "dispatch"))
        self.assertIsNone(util.find_fenced_block("```dispatchx\nTO a\n```", "dispatch"))


class PathsLogConfigTest(support.TempHomeCase):
    def test_orche_home_and_dirs(self):
        self.assertEqual(paths.orche_home(), self.home)
        paths.ensure_dirs()
        for name in paths.SUBDIRS:
            self.assertTrue((self.home / name).is_dir(), name)
        self.assertEqual(paths.project_local("/srv/repo").as_posix(), "/srv/repo/.orche")

    def test_default_home(self):
        os.environ.pop("ORCHE_HOME")
        self.assertEqual(paths.orche_home().parts[-2:], (".claude", "orche"))

    def test_log_appends_tab_separated(self):
        log.log("hooks", "first\nline")
        log.log("hooks", "second")
        lines = (self.home / "logs" / "hooks.log").read_text().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0].split("\t")[1:], ["hooks", "first\\nline"])

    def test_log_never_raises(self):
        (self.home / "logs").write_text("not a directory")
        log.log("hooks", "dropped")

    def test_config_defaults_merged(self):
        self.assertEqual(config.load(), config.DEFAULTS)
        (self.home / "config.json").write_text(json.dumps({"lang": "ko", "jev": {"enabled": True}}))
        cfg = config.load()
        self.assertEqual(cfg["lang"], "ko")
        self.assertEqual(cfg["jev"], {**config.DEFAULTS["jev"], "enabled": True})
        self.assertEqual(cfg["orca_bin"], "orca")

    def test_config_corrupt_falls_back_and_logs(self):
        (self.home / "config.json").write_text("{oops")
        self.assertEqual(config.load(), config.DEFAULTS)
        self.assertIn("using defaults", (self.home / "logs" / "config.log").read_text())

    def test_example_config_matches_defaults(self):
        example = json.loads((support.ROOT / "config" / "orche.example.json").read_text())
        self.assertEqual(example, config.DEFAULTS)


if __name__ == "__main__":
    unittest.main()


class ProjectFromCwdTest(unittest.TestCase):
    def test_symlinked_cwd_and_registry_path_resolve(self):
        import tempfile
        from orche import units
        with tempfile.TemporaryDirectory() as tmp:
            real = Path(tmp) / "real" / "acme"
            (real / "src").mkdir(parents=True)
            wt = Path(tmp) / "real" / "acme-build"
            wt.mkdir()
            link = Path(tmp) / "link"
            link.symlink_to(Path(tmp) / "real")
            reg = {"projects": {"acme": {"path": str(link / "acme"), "divisions": {
                "build": {"worktree": f"repo::{wt}", "features": []}}}}}
            self.assertEqual(units.project_from_cwd(reg, real / "src"), "acme")
            self.assertEqual(units.project_from_cwd(reg, link / "acme-build"), "acme")
            self.assertIsNone(units.project_from_cwd(reg, Path(tmp)))
