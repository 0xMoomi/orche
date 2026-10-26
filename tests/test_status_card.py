"""The card (0.9.0 resume card): `orche status <project>` and the SessionStart card for a cleared or
restarted Main."""
import json
from unittest import mock

import support
from orche import inbox, util
from orche.queue import store
from test_cmd_dispatch import BODY
from test_flow import FlowCase, header

CANARY = "AKIA" + "ABCDEFGHIJKLMNOP"


class CardTest(FlowCase):
    def unit(self, label):
        f = self.first(BODY.replace("Add coupon field", f"Add field {label}"))
        header(f, f.name, "-")
        return f

    def post(self, unit, status, summary):
        with mock.patch.object(inbox, "wake"):
            inbox.post(inbox.make(unit, "div-acme-build", "main-acme", status, brief_rev=1,
                                  summary=summary, lineage=unit))

    def three(self):
        a, b, c = self.unit("A"), self.unit("B"), self.unit("C")
        self.post(a.name, "question", f"which coupon rule? key {CANARY}\nsecond line")
        util.write_json(b / "RUN.json", {"pr": 41})
        with store.edit("acme") as q:
            q["entries"].append({"unit": b.name, "state": "waiting", "pr": 41})
        self.post(c.name, "done", "shipped")
        return a.name, b.name, c.name

    def session(self, source, session="main-acme", cwd=None):
        return self.run_hook("session_start.py", self.payload("session_start_clear.json", source=source,
                                                              cwd=str(cwd or self.repo)), ORCHE_SESSION=session)

    def test_card_names_open_lineages_what_they_wait_on_and_the_closed_one(self):
        a, b, c = self.three()
        res = self.orche("status", "acme")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        out = res.stdout
        self.assertTrue(out.startswith("[orche] resume acme\n"), out)
        self.assertIn(f"L {a} — ", out)
        self.assertIn(f"  last: {a} QUESTION — which coupon rule?", out)
        self.assertIn(f"waits: owner — {a} (question); answer in main-acme", out)
        self.assertNotIn(CANARY, out)
        self.assertIn("[REDACTED]", out)
        self.assertNotIn("second line", out)
        self.assertIn(f"waits: {b} (queued)", out)
        self.assertIn("PRs #41", out)
        self.assertIn(f"closed L {c}", out)
        self.assertIn("DONE: 1 unit (1 done)", out)
        self.assertIn("queue: ", out)
        self.assertLessEqual(len(out.splitlines()), 40)

    def test_json_shape_and_nothing_in_flight(self):
        res = self.orche("status", "acme")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("nothing in flight", res.stdout)
        self.three()
        d = json.loads(self.orche("--json", "status", "acme").stdout)
        for k in ("open", "open_total", "owner", "unread", "blocked", "closed", "queue", "text"):
            self.assertIn(k, d)
        self.assertEqual(len(d["open"]), 2)
        self.assertNotIn(CANARY, json.dumps(d))

    def test_no_project_prints_the_rows_and_an_unknown_project_fails(self):
        self.three()
        out = self.orche("status").stdout
        self.assertTrue(out.startswith("acme stage="), out)
        self.assertNotIn("[orche] resume", out)
        self.assertEqual(self.orche("status", "nope").returncode, 2)

    def test_session_start_appends_the_card_for_main(self):
        startup = self.session("startup").stdout
        self.assertNotIn("[orche] resume", startup)
        clear = self.session("clear").stdout
        self.assertIn("[orche] resume acme\nnothing in flight", clear)
        a, _b, _c = self.three()
        for source in ("clear", "resume", "startup", "compact"):
            out = self.session(source).stdout
            self.assertIn("[orche] resume acme", out, source)
            self.assertIn(f"waits: owner — {a}", out)
            self.assertNotIn(CANARY, out)
            self.assertLessEqual(len(out.rstrip("\n")), 6000)

    def test_a_division_session_gets_no_card(self):
        self.three()
        out = self.session("clear", "div-acme-build", self.root / "acme-build").stdout
        self.assertTrue(out.startswith("## ORCHE role\nsession: div-acme-build"))
        self.assertNotIn("[orche] resume", out)


class CardDocsTest(FlowCase):
    def test_rulebook_and_readme_name_the_card(self):
        skill = (support.ROOT / "skills" / "orche-main" / "SKILL.md").read_text()
        self.assertIn("pick up where we left off", skill)
        self.assertIn("`orche status <project>` (its card)", skill)
        self.assertNotIn("orche resume", skill)
        readme = (support.ROOT / "README.md").read_text()
        self.assertIn("'orche status <project>'", readme)
        self.assertNotIn("orche resume", readme)
        self.assertLessEqual(len(readme.splitlines()), 100)
