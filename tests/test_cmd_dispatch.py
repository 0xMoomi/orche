"""`orche dispatch`: blocks on stdin (or --file), `---` between them, one outcome line per block."""
import json
import os
import subprocess
import sys
import time
import unittest

import support
from orche import cmd_dispatch
from test_hooks import DISPATCH_OK, HookCase

BODY = DISPATCH_OK.split("```dispatch\n", 1)[1].split("\n```", 1)[0]
GIFT = BODY.replace("Add coupon field", "Add gift card field")
NO_VERIFY = BODY.replace("VERIFY    scripts/verify-checkout.sh\n", "")


class SplitBlocksTest(unittest.TestCase):
    def test_split(self):
        self.assertEqual(cmd_dispatch.split_blocks("TO a\n---\nTO b\n"), ["TO a", "TO b"])
        self.assertEqual(cmd_dispatch.split_blocks("\n---\nTO a\n---\n\n"), ["TO a"])
        self.assertEqual(cmd_dispatch.split_blocks("TO a\nCONTEXT x --- y\n  ----\n"),
                         ["TO a\nCONTEXT x --- y\n  ----"])               # only a bare `---` line splits
        self.assertEqual(cmd_dispatch.split_blocks(""), [])


class CmdDispatchTest(HookCase):
    def orche(self, *argv, stdin="", cwd=None, **env):
        clean = {k: v for k, v in os.environ.items() if not k.startswith(("ORCA_", "ORCHE_", "CLAUDE"))}
        clean.update({"ORCHE_HOME": str(self.home), "ORCHE_FAKE_ORCA": "1",
                      "ORCHE_SCRATCH_ROOT": str(self.root / "scratch"), "ORCHE_SESSION": "main-acme"}, **env)
        return subprocess.run([sys.executable, str(support.ROOT / "bin" / "orche.py"), *argv], input=stdin,
                              env=clean, cwd=str(cwd or self.repo), capture_output=True, text=True, timeout=30)

    def map(self):
        path = self.home / "sessions" / "main-acme.dispatched.json"
        return json.loads(path.read_text()) if path.exists() else {}

    def test_one_block(self):
        res = self.orche("dispatch", stdin=BODY)
        unit = self.only_unit()
        self.assertEqual((res.returncode, res.stdout),
                         (0, f"block 1: delivered {unit} → div-acme-build (rev 1, deliverer-started)\n"))
        [entry] = self.map().values()
        self.assertEqual(entry["routed"], f"{unit} → div-acme-build")

    def test_two_blocks_and_a_rerun_skips_both(self):
        res = self.orche("dispatch", stdin=f"{BODY}\n---\n{GIFT}\n")
        self.assertEqual(res.returncode, 0, res.stdout)
        self.assertEqual(len(list(self.units_dir().iterdir())), 2)
        res = self.orche("dispatch", stdin=f"{BODY}\n---\n{GIFT}\n")
        self.assertEqual(res.returncode, 0)
        self.assertEqual([l.split(" — ")[0] for l in res.stdout.splitlines()],
                         ["block 1: skipped", "block 2: skipped"])
        self.assertEqual(len(list(self.units_dir().iterdir())), 2)

    def test_expired_map_entry_is_sent_again(self):
        self.orche("dispatch", stdin=BODY)
        path = self.home / "sessions" / "main-acme.dispatched.json"
        path.write_text(json.dumps({h: {**e, "at": e["at"] - 601} for h, e in self.map().items()}))
        res = self.orche("dispatch", stdin=BODY)
        self.assertIn("delivered", res.stdout)
        self.assertEqual(len(list(self.units_dir().iterdir())), 2)

    def test_file(self):
        outbox = self.repo / ".orche" / "outbox"
        outbox.mkdir()
        (outbox / "coupon.md").write_text(BODY)
        res = self.orche("dispatch", "--file", ".orche/outbox/coupon.md")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.only_unit()
        res = self.orche("dispatch", "--file", "missing.md")
        self.assertEqual(res.returncode, 1)
        self.assertIn("cannot read missing.md", res.stderr)

    def test_dry_run_writes_nothing(self):
        res = self.orche("--dry-run", "dispatch", stdin=f"{BODY}\n---\n{NO_VERIFY}")
        self.assertEqual(res.returncode, 2)
        self.assertEqual(res.stdout.splitlines(), [
            "block 1: would deliver brief new brief → div-acme-build",
            "block 2: refused — missing VERIFY. Fill every field or drop the block."])
        self.assertFalse(self.units_dir().exists())
        self.assertEqual(self.map(), {})
        self.assertEqual(self.inbox("div-acme-build"), [])

    def test_partial_refusal_delivers_the_rest_and_exits_2(self):
        res = self.orche("dispatch", stdin=f"{NO_VERIFY}\n---\n{GIFT}")
        self.assertEqual(res.returncode, 2)
        unit = self.only_unit()
        self.assertEqual(res.stdout.splitlines(), [
            "block 1: refused — missing VERIFY. Fill every field or drop the block.",
            f"block 2: delivered {unit} → div-acme-build (rev 1, deliverer-started)",
            "1 of 2 refused: fix the refused block(s) and run `orche dispatch` again; the delivered ones went "
            "out (re-sending them unchanged is skipped)."])

    def test_sender_refused(self):
        res = self.orche("dispatch", stdin=BODY, ORCHE_SESSION="ghost-acme")
        self.assertEqual((res.returncode, res.stdout),
                         (2, "refused — only Main or a division may dispatch; ghost-acme is an unknown session\n"))
        self.assertFalse(self.units_dir().exists())

    def test_empty_input(self):
        res = self.orche("dispatch", stdin="\n---\n")
        self.assertEqual(res.returncode, 1)
        self.assertIn("no block on stdin", res.stderr)


if __name__ == "__main__":
    unittest.main()
