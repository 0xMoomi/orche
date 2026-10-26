import datetime as dt
import json
import os
import stat
import unittest

import support
from orche import config, log, paths, util


class SlugAndIdsTest(unittest.TestCase):
    def test_slugify(self):
        self.assertEqual(util.slugify("Acme Shop!"), "acme-shop")
        self.assertEqual(util.slugify("  --Foo__Bar 2.0-- "), "foo-bar-2-0")

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


class FenceTest(unittest.TestCase):
    def test_fence_outgrows_every_backtick_run(self):
        self.assertEqual(util.fence("a"), "````text\na\n````")
        self.assertEqual(util.fence("x `````` y"), "```````text\nx `````` y\n```````")
        self.assertEqual(util.fence("```dispatch\nTO x\n```", "markdown"), "````markdown\n```dispatch\nTO x\n```\n````")


class FenceSpansTest(unittest.TestCase):
    """util.fence_spans: ``` and ~~~ fences close with the same character and at least the same length."""

    def spans(self, text):
        return util.fence_spans(text.splitlines())

    def test_backtick_and_tilde_blocks(self):
        for f in ("```", "~~~"):
            self.assertEqual(self.spans(f"a\n{f}dispatch\nTO b\n{f}\nz"), [(1, 3, "dispatch")], f)
            self.assertEqual(self.spans(f"{f}\nx\n{f}\n{f}py\ny\n{f}"), [(0, 2, ""), (3, 5, "py")], f)
            self.assertEqual(self.spans(f"{f}dispatch\nTO b"), [(0, None, "dispatch")], f)   # unclosed

    def test_the_other_character_or_a_shorter_fence_is_content(self):
        self.assertEqual(self.spans("```dispatch\n~~~\nTO b\n```"), [(0, 3, "dispatch")])
        self.assertEqual(self.spans("~~~dispatch\n```\nTO b\n~~~"), [(0, 3, "dispatch")])
        self.assertEqual(self.spans("~~~~dispatch\n~~~\nTO b\n~~~~"), [(0, 3, "dispatch")])
        self.assertEqual(self.spans("````dispatch\n```\nTO b\n`````"), [(0, 3, "dispatch")])
        self.assertEqual(self.spans("```\nx\n```py"), [(0, None, "")])    # a fence with info never closes

    def test_a_snippet_indented_inside_a_trailer_block_stays_inside(self):
        for f in ("```", "~~~"):
            body = (f"{f}dispatch\nTO build\nCONTEXT the call:\n  {f}py\n  parse(x)\n  {f}\n  bare:\n  {f}\n"
                    f"  y = 1\n  {f}\nTIER standard\n{f}")
            self.assertEqual(self.spans(body), [(0, 11, "dispatch")], f)
            # A plain block has no such rule: an indented bare fence closes it.
            self.assertEqual(self.spans(f"{f}\n  {f}\nx\n{f}"), [(0, 1, ""), (3, None, "")], f)


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
        (self.home / "config.json").unlink()        # the test home's wake-off config
        self.assertEqual(config.load(), config.DEFAULTS)
        self.assertEqual(config.DEFAULTS["inbox"], {"wake": True, "quiet_s": 90, "quiet_max_s": 240})
        (self.home / "config.json").write_text(json.dumps({"lang": "ko", "jev": {"enabled": True}}))
        cfg = config.load()
        self.assertEqual(cfg["lang"], "ko")
        self.assertEqual(cfg["jev"], {**config.DEFAULTS["jev"], "enabled": True})
        self.assertEqual(cfg["orca_bin"], "orca")

    def test_config_corrupt_falls_back_and_logs(self):
        (self.home / "config.json").write_text("{oops")
        self.assertEqual(config.load(), config.DEFAULTS)
        self.assertIn("using defaults", (self.home / "logs" / "config.log").read_text())


if __name__ == "__main__":
    unittest.main()
