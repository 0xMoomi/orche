import dataclasses
import json
import os
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path

import support  # noqa: F401  (puts lib/ on sys.path)
from orche import brief, shell, triage, units
from orche.brief import Block, Refusal

R = "[orche] dispatch refused: "
ALLOWED = "Allowed: TO GOAL SCOPE CONTEXT ACCEPT VERIFY TIMEBOX FORBID REPORT TIER ROLLBACK."
FILL = "Fill every field or drop the block."
DUP = "Write each key once; continue a value on the lines below it."
FIX = "start each field with its exact KEY."
INCIDENT = ("Stop at the current safe boundary: the owner wants per-kind LUCKY ladders folded into rules\n"
            "13 before world 7 goes live, so nothing may be merged until that lands. If a\n"
            "rebase or merge of PR #25 is in progress, finish it to a clean state first.")
WANT = "(want <proj>-<slug>-<yyyymmdd>-<n>)"
OWN = "put it in its own dispatch block."
ONLY = "a STOP takes only TO and the reason."
FENCE = "code samples go indented by two spaces."
U = "acme-fix-login-20261016-1"

FULL = """TO build
GOAL g
SCOPE s
CONTEXT c
ACCEPT a
VERIFY run:
  npm test
TIMEBOX 1h
FORBID f
REPORT r
TIER standard"""
FIELDS = {"TO": "build", "GOAL": "g", "SCOPE": "s", "CONTEXT": "c", "ACCEPT": "a", "VERIFY": "run:\nnpm test",
          "TIMEBOX": "1h", "FORBID": "f", "REPORT": "r", "TIER": "standard"}


def without(*keys):
    return "\n".join(ln for ln in FULL.splitlines() if ln.split(" ")[0] not in keys and not
                     (ln.startswith("  ") and "VERIFY" in keys))


def shape(b):
    return (b.kind, b.unit, b.rev, b.note, b.fields)


class GrammarTest(unittest.TestCase):
    def test_valid(self):
        for name, body, want in [
            ("full brief", FULL, ("brief", "", 0, "", FIELDS)),
            ("AMEND, SUPERSEDES on line 2", f"AMEND {U} rev 2\nSUPERSEDES scope was wrong\nGOAL g2",
             ("amend", U, 2, "scope was wrong", {"GOAL": "g2"})),
            ("AMEND, SUPERSEDES on line 3", f"AMEND {U} rev 3\nGOAL g2\nSUPERSEDES late",
             ("amend", U, 3, "late", {"GOAL": "g2"})),
            ("AMEND, no SUPERSEDES", f"AMEND {U} rev 2\nGOAL g2", ("amend", U, 2, "", {"GOAL": "g2"})),
            ("AMEND, SUPERSEDES twice", f"AMEND {U} rev 2\nSUPERSEDES one\nSUPERSEDES two",
             ("amend", U, 2, "two", {})),
            ("STOP with reason and TO", f"STOP {U}  wrong target \nTO build",
             ("stop", U, 0, "wrong target", {"TO": "build"})),
            ("STOP bare", f"STOP {U}", ("stop", U, 0, "", {})),
            ("STOP reason spans plain lines", f"STOP {U}\nTO build\n{INCIDENT}",
             ("stop", U, 0, INCIDENT, {"TO": "build"})),
            ("STOP TO after the body", f"STOP {U} first\nsecond line\nTO build",
             ("stop", U, 0, "first\nsecond line", {"TO": "build"})),
            ("STOP indented line continues the reason", f"STOP {U}\nwhy\n  more", ("stop", U, 0, "why\nmore", {})),
            ("AMEND prose before a key is note", f"AMEND {U} rev 2\nscope moved\nGOAL g2",
             ("amend", U, 2, "scope moved", {"GOAL": "g2"})),
            ("AMEND prose after SUPERSEDES is note", f"AMEND {U} rev 2\nSUPERSEDES rev 1: smaller\nand faster\nGOAL g2",
             ("amend", U, 2, "rev 1: smaller\nand faster", {"GOAL": "g2"})),
            ("AMEND prose after a key extends it", f"AMEND {U} rev 2\nGOAL g2\nand more",
             ("amend", U, 2, "", {"GOAL": "g2\nand more"})),
            ("plain continuation", FULL.replace("CONTEXT c", "CONTEXT c\nunindented more"),
             ("brief", "", 0, "", {**FIELDS, "CONTEXT": "c\nunindented more"})),
            ("indented verb", f"   AMEND {U} rev 2\nGOAL g2", ("amend", U, 2, "", {"GOAL": "g2"})),
            ("verb after comment and blanks", f"\n# why\n\n  # more\nSTOP {U}", ("stop", U, 0, "", {})),
            ("one-space-indented key", FULL.replace("GOAL g", " GOAL g"), ("brief", "", 0, "", FIELDS)),
            ("trailing spaces on value", FULL.replace("GOAL g", "GOAL   g   "), ("brief", "", 0, "", FIELDS)),
        ]:
            with self.subTest(name):
                self.assertEqual(shape(brief.parse(body)), want)

    def test_g1_content_lines(self):
        for name, body, key, want in [
            ("indented # in VERIFY", FULL.replace("  npm test", "  # unit tests\n  npm test"),
             "VERIFY", "run:\n# unit tests\nnpm test"),
            ("indented ## in CONTEXT", FULL.replace("CONTEXT c", "CONTEXT upstream:\n  ## Findings\n  x is slow"),
             "CONTEXT", "upstream:\n## Findings\nx is slow"),
            ("unindented comment keeps the field open", FULL.replace("CONTEXT c", "CONTEXT a\n# note\n  more"),
             "CONTEXT", "a\nmore"),
            ("one-space # is a comment", FULL.replace("CONTEXT c", "CONTEXT a\n # aside\n  more"),
             "CONTEXT", "a\nmore"),
            ("continuation of an empty value", FULL.replace("CONTEXT c", "CONTEXT\n  first\n  second"),
             "CONTEXT", "first\nsecond"),
            ("deeper indent keeps the rest", FULL.replace("CONTEXT c", "CONTEXT c\n    nested"),
             "CONTEXT", "c\n  nested"),
        ]:
            with self.subTest(name):
                self.assertEqual(brief.parse(body).fields[key], want)

    def test_g1_indented_comment_after_tier_is_part_of_tier(self):
        body = FULL.replace("TIER standard", "TIER standard\n  # cheap enough")
        with self.assertRaises(Refusal) as cm:
            brief.parse(body)
        self.assertEqual(str(cm.exception),
                         f"{R}TIER must be one of mechanical|standard|hard, got 'standard\\n# cheap enough'.")

    def test_g2_indented_verbs_keys_and_fences_are_content(self):
        b = brief.parse(FULL.replace("  npm test", f"  ```sh\n  npm test\n  ```\n  STOP {U}"))
        self.assertEqual(b.fields["VERIFY"], f"run:\n```sh\nnpm test\n```\nSTOP {U}")
        b = brief.parse(f"STOP {U}\nwhy\n  GOAL g")
        self.assertEqual((b.note, b.fields), ("why\nGOAL g", {}))
        b = brief.parse(FULL.replace("CONTEXT c", "CONTEXT c\nSTOP soon"))   # no unit id: prose
        self.assertEqual(b.fields["CONTEXT"], "c\nSTOP soon")

    def test_amend_indented_supersedes_is_content(self):
        b = brief.parse(f"AMEND {U} rev 2\nGOAL g2\n  SUPERSEDES x")
        self.assertEqual((b.fields["GOAL"], b.note), ("g2\nSUPERSEDES x", ""))

    def test_supersedes_is_prose_outside_amend(self):
        with self.assertRaises(Refusal) as cm:
            brief.parse(FULL + "\nSUPERSEDES x")
        self.assertEqual(str(cm.exception), f"{R}TIER must be one of mechanical|standard|hard, got 'standard\\n"
                                            f"SUPERSEDES x'. Line 12 \"SUPERSEDES x\" was read as part of TIER; {FIX}")

    def test_codec(self):
        for name, lines, want in [
            ("indented before any key is dropped", ["  stray", "GOAL g"], ([("GOAL", 2, "GOAL g")], {"GOAL": "g"},
                                                                            "", [], None)),
            ("plain before any key is stray", ["FOO x", "  more", "GOAL g", "BAR"],
             ([("GOAL", 3, "GOAL g")], {"GOAL": "g\nBAR"}, "", [(4, "BAR", "GOAL")], (1, "FOO x"))),
        ]:
            with self.subTest(name):
                self.assertEqual(brief._codec(lines), want)

    def test_refusals(self):
        all_missing = "TO, GOAL, SCOPE, CONTEXT, ACCEPT, VERIFY, TIMEBOX, FORBID, REPORT, TIER"
        for name, body, want in [
            ("R1 empty", "", f"{R}missing {all_missing}. {FILL}"),
            ("R1 blank", "  \n\n", f"{R}missing {all_missing}. {FILL}"),
            ("R1 comments only", "# a\n  # b", f"{R}missing {all_missing}. {FILL}"),
            ("R2 no rev", "AMEND u", f"{R}AMEND needs 'AMEND <unit> rev <N>'; line 1 \"AMEND u\" does not match."),
            ("R2 AMENDMENT", "\nAMENDMENT x",
             f"{R}AMEND needs 'AMEND <unit> rev <N>'; line 2 \"AMENDMENT x\" does not match."),
            ("R2 rev not a number", f"AMEND {U} rev two",
             f"{R}AMEND needs 'AMEND <unit> rev <N>'; line 1 \"AMEND {U} rev two\" does not match."),
            ("R3 bare STOP", "STOP", f"{R}STOP needs 'STOP <unit>'; line 1 \"STOP\" does not match."),
            ("R3 STOPPED", "STOPPED", f"{R}STOP needs 'STOP <unit>'; line 1 \"STOPPED\" does not match."),
            ("R4 AMEND foo", "AMEND foo rev 2\nGOAL g", f"{R}bad unit id 'foo' {WANT}."),
            ("R4 AMEND acme-x", "AMEND acme-x rev 2\nGOAL g", f"{R}bad unit id 'acme-x' {WANT}."),
            ("R4 STOP foo", "STOP foo", f"{R}bad unit id 'foo' {WANT}."),
            ("R5 brief opens with prose", "Here is the brief:\n" + FULL,
             f"{R}line 1 \"Here is the brief:\" is not a KEY line; a brief starts with TO."),
            ("R5 brief opens with an unknown key", "\nOWNER y\n" + FULL,
             f"{R}line 2 \"OWNER y\" is not a KEY line; a brief starts with TO."),
            ("R5 stray quote is one line of 60", "x" * 70 + "\n" + FULL,
             f"{R}line 1 \"{'x' * 60}\" is not a KEY line; a brief starts with TO."),
            ("R5 typo'd key is caught as missing", FULL.replace("GOAL g", "GAOL g"),
             f"{R}missing GOAL. {FILL} Line 2 \"GAOL g\" was read as part of TO; {FIX}"),
            ("R5 lowercase key", FULL.replace("GOAL g", "goal g"),
             f"{R}missing GOAL. {FILL} Line 2 \"goal g\" was read as part of TO; {FIX}"),
            ("R5 AMEND inside a brief", FULL + f"\nAMEND {U} rev 2",
             f"{R}line 12 \"AMEND {U} rev 2\" starts another block; {OWN}"),
            ("G2 STOP merged with a brief", f"STOP {U}\nTO build\nwhy\n" + FULL.split("\n", 1)[1],
             f"{R}line 4 \"GOAL g\" is a GOAL line inside a STOP; {ONLY}"),
            ("G2 STOP non-TO key", f"STOP {U}\nGOAL g", f"{R}line 2 \"GOAL g\" is a GOAL line inside a STOP; {ONLY}"),
            ("G2 brief merged with a STOP", FULL + f"\nSTOP {U} wrong target",
             f"{R}line 12 \"STOP {U} wrong target\" starts another block; {OWN}"),
            ("G2 STOP inside an AMEND", f"AMEND {U} rev 2\nGOAL g2\nSTOP {U}",
             f"{R}line 3 \"STOP {U}\" starts another block; {OWN}"),
            ("G2 AMEND inside an AMEND", f"AMEND {U} rev 2\nAMEND {U} rev 3\nGOAL g2",
             f"{R}line 2 \"AMEND {U} rev 3\" starts another block; {OWN}"),
            ("G2 bare fence in a brief", FULL.replace("  npm test", "```\nnpm test\n```"),
             f"{R}line 7 \"```\" is a code fence inside a dispatch block; {FENCE}"),
            ("G2 tilde fence with info", FULL.replace("CONTEXT c", "CONTEXT c\n~~~python"),
             f"{R}line 5 \"~~~python\" is a code fence inside a dispatch block; {FENCE}"),
            ("G2 fence in a STOP", f"STOP {U}\nwhy\n```sh", f"{R}line 3 \"```sh\" is a code fence inside a "
                                                                f"dispatch block; {FENCE}"),
            ("R6 ACCEPT twice", FULL + "\nACCEPT two",
             f"{R}duplicate keys ACCEPT; line 12 \"ACCEPT two\" repeats one. {DUP}"),
            ("R6 TO twice in STOP", f"STOP {U}\nTO a\nwhy\nTO b",
             f"{R}duplicate keys TO; line 4 \"TO b\" repeats one. {DUP}"),
            ("R7 epic", FULL.replace("TIER standard", "TIER epic"),
             f"{R}TIER must be one of mechanical|standard|hard, got 'epic'."),
            ("R7 Standard", FULL.replace("TIER standard", "TIER Standard"),
             f"{R}TIER must be one of mechanical|standard|hard, got 'Standard'."),
            ("R7 in an AMEND", f"AMEND {U} rev 2\nTIER big",
             f"{R}TIER must be one of mechanical|standard|hard, got 'big'."),
            ("R8 VERIFY missing", without("VERIFY"), f"{R}missing VERIFY. {FILL}"),
            ("R8 VERIFY and TIER missing", without("VERIFY", "TIER"), f"{R}missing VERIFY, TIER. {FILL}"),
            ("R8 empty value", FULL.replace("GOAL g", "GOAL"), f"{R}missing GOAL. {FILL}"),
        ]:
            with self.subTest(name):
                with self.assertRaises(Refusal) as cm:
                    brief.parse(body)
                self.assertEqual(str(cm.exception), want)

    def test_fault_order(self):
        for name, body, want in [
            ("prose + dup -> R5", "hi\nTO a\nTO b", f"{R}line 1 \"hi\" is not a KEY line; a brief starts with TO."),
            ("dup + missing -> R6", "TO a\nTO b", f"{R}duplicate keys TO; line 2 \"TO b\" repeats one. {DUP}"),
            ("prose note + bad id -> R4", "AMEND foo rev 2\nFOO x", f"{R}bad unit id 'foo' {WANT}."),
            ("bad TIER + missing -> R7", "TO a\nTIER epic",
             f"{R}TIER must be one of mechanical|standard|hard, got 'epic'."),
            ("bad TIER + bad id -> R7", "AMEND foo rev 2\nTIER epic",
             f"{R}TIER must be one of mechanical|standard|hard, got 'epic'."),
            ("verb syntax first", "AMEND u\nFOO x\nFOO y",
             f"{R}AMEND needs 'AMEND <unit> rev <N>'; line 1 \"AMEND u\" does not match."),
        ]:
            with self.subTest(name):
                with self.assertRaises(Refusal) as cm:
                    brief.parse(body)
                self.assertEqual(str(cm.exception), want)


class BlockTest(unittest.TestCase):
    def test_valid_brief(self):
        b = Block("brief", dict(FIELDS))
        self.assertEqual((b.kind, b.unit, b.rev, b.note), ("brief", "", 0, ""))

    def test_refusals(self):
        for name, args, want in [
            ("missing", ("brief", {k: v for k, v in FIELDS.items() if k != "REPORT"}), f"{R}missing REPORT. {FILL}"),
            ("bad TIER", ("brief", {**FIELDS, "TIER": "epic"}),
             f"{R}TIER must be one of mechanical|standard|hard, got 'epic'."),
            ("unknown", ("brief", {**FIELDS, "OWNER": "x"}), f"{R}unknown keys OWNER. {ALLOWED}"),
            ("stop with GOAL", ("stop", {"GOAL": "g"}, U), f"{R}unknown keys GOAL. Allowed: TO."),
            ("amend bad id", ("amend", {}, "foo", 2), f"{R}bad unit id 'foo' {WANT}."),
            ("stop no id", ("stop",), f"{R}bad unit id '' {WANT}."),
        ]:
            with self.subTest(name):
                with self.assertRaises(Refusal) as cm:
                    Block(*args)
                self.assertEqual(str(cm.exception), want)

    def test_unknown_kind(self):
        with self.assertRaises(ValueError):
            Block("weird")

    def test_frozen(self):
        b = Block("stop", {}, U)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            b.unit = "x"

    def test_triage_brief_fields_build_a_valid_block(self):
        d = triage.Decision("direct", 2, prompt="fix the login bug in lib/auth.py", division="build",
                            tier="hard", goal="fix the login bug", verify="python3 -m unittest")
        b = Block("brief", triage.brief_fields(d))
        self.assertEqual((b.fields["TO"], b.fields["TIER"]), ("build", "hard"))
        d = triage.Decision("direct", 2, prompt="tidy up", division="build", goal="tidy up")
        self.assertEqual(Block("brief", triage.brief_fields(d)).fields["TIER"], "standard")


class UnitIdTest(unittest.TestCase):
    def test_valid(self):
        for unit in (U, "orche-c6-brief-20261019-12", "a-20261019-1"):
            with self.subTest(unit):
                self.assertIsNone(brief.unit_id_error(unit))

    def test_invalid(self):
        for unit in ("Bad_Id", "acme-x", "../etc", "", None, "acme-x-2026101-1", "-x-20261019-1"):
            with self.subTest(repr(unit)):
                self.assertEqual(brief.unit_id_error(unit), f"bad unit id {unit!r} {WANT}")


if __name__ == "__main__":
    unittest.main()


# ---- storage: BRIEF.md writer and reader -------------------------------------

STANDING = "1. Opus for hard work, Sonnet for reads.\n2. Never force-push.\n"
SAMPLE = support.FIXTURES / "transcripts" / "dispatch_sample.jsonl"
CLOSURE = {"orche", "orche.bip39", "orche.brief", "orche.log", "orche.mask", "orche.paths", "orche.shell",
           "orche.units", "orche.util"}


def today():
    return f"{units.now().date():%Y%m%d}"


class StoreCase(support.TempHomeCase):
    """A temp project tree laid out like tests/test_hooks.py, with the fixture registry."""

    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(os.path.realpath(tmp.name))
        self.repo = self.root / "acme"
        for d in ("acme", "acme-build", "acme-checkout", "acme-rnd", "acme-design"):
            (self.root / d).mkdir()
        (self.repo / ".orche").mkdir()
        (self.repo / ".orche" / "STANDING.md").write_text(STANDING)
        self.reg = json.loads((support.FIXTURES / "hooks" / "registry.json").read_text()
                              .replace("@ROOT@", str(self.root)))

    def feature(self, **over):
        self.reg["projects"]["acme"]["divisions"]["build"]["features"][0].update(over)

    def member(self, session):
        from orche import roster
        return roster.Roster(self.reg).member(session)

    def write(self, fields, to="div-acme-build", frm="main-acme", timeout=0, **kw):
        return brief.write(Block("brief", {**FIELDS, **fields}), self.member(to), frm, STANDING,
                           timeout=timeout, **kw)

    def units_dir(self, root=None):
        return (root or self.repo) / ".orche" / "units"


class WriteReadTest(StoreCase):
    def test_new_brief(self):
        b = self.write({"TO": "build", "GOAL": "Add coupon field"})
        u = f"acme-add-coupon-field-{today()}-1"
        self.assertEqual((b.unit, b.project, b.root, b.rev, b.to, b.tier),
                         (u, "acme", self.repo, 1, "div-acme-build", "standard"))
        self.assertEqual((b.folder, b.path), (self.units_dir() / u, self.units_dir() / u / "BRIEF.md"))
        self.assertEqual(b.path.read_text(), b.text)
        self.assertEqual((b.folder / "BRIEF.rev1.md").read_text(), b.text)
        self.assertEqual(b.text.splitlines()[:7], [
            "rev: 1", f"unit: {u}", "to: div-acme-build   from: main-acme",
            f"worktree: {self.root / 'acme-build'}   branch: div/build",
            f"project root: {self.repo}   (brief + REPORT.md live under project root/.orche/units/{u}/)",
            "Work only inside `worktree`. SCOPE paths are relative to it.", ""])
        body = b.text.split(f"## Brief {u}\n", 1)[1]
        self.assertLess(b.text.index("Never force-push"), b.text.index("persona: .claude/agents/div-acme-build.md"))
        self.assertEqual([ln.split(" ")[0] for ln in body.splitlines() if not ln.startswith("  ")],
                         [k for k in brief.KEYS if k in FIELDS])
        self.assertTrue(body.startswith("TO        div-acme-build\n"))
        self.assertIn("\nVERIFY    run:\n  npm test\n", body)
        self.assertEqual(b.fields, {**FIELDS, "TO": "div-acme-build", "GOAL": "Add coupon field"})
        self.assertEqual(brief.find(self.reg, u), b)
        self.assertEqual(self.write({"GOAL": "Add coupon field"}).unit, f"acme-add-coupon-field-{today()}-2")

    def test_key_order_is_keys_not_block_order(self):
        b = brief.write(Block("brief", dict(reversed(list(FIELDS.items())))), self.member("div-acme-build"),
                        "main-acme", STANDING, timeout=0)
        body = b.text.split("## Brief", 1)[1].splitlines()[1:]
        self.assertEqual([ln.split(" ")[0] for ln in body if not ln.startswith("  ")],
                         [k for k in brief.KEYS if k in FIELDS])

    def test_secret_in_goal_never_reaches_the_id(self):
        token = "ghp" + "_" + "a" * 36
        b = self.write({"GOAL": f"Rotate {token} now"})
        self.assertNotIn("a" * 36, b.unit)
        self.assertNotIn("ghp", b.unit)
        self.assertIsNone(brief.unit_id_error(b.unit))

    def test_amend(self):
        b = self.write({"GOAL": "Add coupon field"})
        a = brief.write(Block("amend", {"GOAL": "Smaller"}, b.unit, 2, "rev 1: smaller"),
                        self.member("div-acme-build"), "main-acme", STANDING, prior=b, timeout=0)
        self.assertEqual((a.unit, a.rev, a.fields["GOAL"], a.fields["VERIFY"]), (b.unit, 2, "Smaller", "run:\nnpm test"))
        self.assertTrue(a.text.startswith(f"rev: 2\nsupersedes: rev 1\nsupersedes-note: rev 1: smaller\nunit: {b.unit}\n"))
        self.assertEqual((b.folder / "BRIEF.rev1.md").read_text(), b.text)
        self.assertEqual((b.folder / "BRIEF.rev2.md").read_text(), a.text)
        self.assertEqual(a.path.read_text(), a.text)
        self.assertEqual(brief.find(self.reg, b.unit), a)
        self.assertFalse((self.home / "logs" / "hooks.log").exists())

    def test_amend_author_rev_is_advisory(self):
        b = self.write({"GOAL": "Add coupon field"})
        a = brief.write(Block("amend", {}, b.unit, 5), self.member("div-acme-build"), "main-acme", STANDING,
                        prior=b, timeout=0)
        self.assertEqual(a.rev, 2)
        self.assertNotIn("supersedes-note", a.text)
        self.assertIn(f"dispatch: AMEND {b.unit} said rev 5; using 2",
                      (self.home / "logs" / "hooks.log").read_text())

    def test_misuse_raises(self):
        m = self.member("div-acme-build")
        b = self.write({})
        bare = types.SimpleNamespace(session="x", project="acme", home=None, owns_worktree=False,
                                     worktree=None, branch="")
        for block, target, prior in ((Block("stop", {}, b.unit), m, b), (Block("amend", {}, b.unit, 2), m, None),
                                     (Block("brief", FIELDS), m, b), (Block("brief", FIELDS), bare, None)):
            with self.subTest(kind=block.kind, prior=prior is not None):
                with self.assertRaises(ValueError):
                    brief.write(block, target, "main-acme", STANDING, prior=prior, timeout=0)
        self.assertEqual(sorted(p.name for p in self.units_dir().iterdir()), [b.unit])

    def test_g1_round_trip(self):
        fields = {"ACCEPT": "works\n# not a comment\n  deeper", "CONTEXT": "## Brief inside a value"}
        b = self.write(fields)
        self.assertEqual(brief.find(self.reg, b.unit).fields, {**FIELDS, **fields, "TO": "div-acme-build"})
        self.assertEqual(b.fields, {**FIELDS, **fields, "TO": "div-acme-build"})

    def test_tier(self):
        self.assertEqual(self.write({"TIER": "hard"}).tier, "hard")
        self.assertEqual(brief.Brief("u", "acme", self.repo, 1, {"TIER": " Hard "}, "").tier, "hard")
        self.assertEqual(brief.Brief("u", "acme", self.repo, 1, {"TIER": "epic"}, "").tier, "standard")
        self.assertNotIn("text", repr(brief.Brief("u", "acme", self.repo, 1, {}, "secret")))

    def test_sender(self):
        b = self.write({}, frm="div-acme-build")
        self.assertEqual((b.sender, brief.find(self.reg, b.unit).sender), ("div-acme-build", "div-acme-build"))
        a = brief.write(Block("amend", {}, b.unit, 2), self.member("div-acme-build"), "main-acme", STANDING,
                        prior=b, timeout=0)
        self.assertEqual(brief.find(self.reg, b.unit).sender, "main-acme")      # the current revision's
        b.path.write_text("rev: 1\n\n## Brief x\nTO div-acme-build\n")          # hand-written: no header
        self.assertEqual((brief.find(self.reg, b.unit).sender, a.sender), ("", "main-acme"))
        self.assertEqual(brief.Brief("u", "acme", self.repo, 0, {}, "").sender, "")


class HeaderTest(StoreCase):
    def setUp(self):
        super().setUp()
        self.calls = []
        self.addCleanup(setattr, shell, "RUNNER", shell.RUNNER)
        shell.RUNNER = lambda argv, *, timeout, input: (self.calls.append((argv, timeout))
                                                        or shell.Result(0, "trunk\n", "", argv))

    def header(self, to, frm="div-acme-build", timeout=0.3):
        b = self.write({}, to=to, frm=frm, timeout=timeout)
        self.assertNotIn("worktree: .", b.text)
        return b.text.splitlines()[3]

    def test_division_uses_recorded_worktree(self):
        self.assertEqual(self.header("div-acme-build", "main-acme"),
                         f"worktree: {self.root / 'acme-build'}   branch: div/build")
        self.assertEqual(self.calls, [])

    def test_main_asks_git_with_the_budget(self):
        self.assertEqual(self.header("main-acme"), f"worktree: {self.repo}   branch: trunk")
        self.assertEqual(self.calls, [(["git", "-C", str(self.repo), "branch", "--show-current"], 0.3)])

    def test_no_budget_no_git(self):
        for timeout in (0, -0.2):
            self.assertEqual(self.header("main-acme", timeout=timeout), f"worktree: {self.repo}   branch: -")
        self.assertEqual(self.calls, [])

    def test_feature_without_worktree_is_the_main_checkout(self):
        self.feature(worktree="")
        self.assertEqual(self.header("feat-acme-checkout"),
                         f"worktree: {self.repo} (main checkout)   branch: feat/checkout")
        self.assertEqual(self.calls, [])
        self.feature(branch="")
        self.assertEqual(self.header("feat-acme-checkout"), f"worktree: {self.repo} (main checkout)   branch: trunk")
        self.assertEqual(len(self.calls), 1)

    def test_git_failure_is_a_dash(self):
        shell.RUNNER = lambda argv, *, timeout, input: shell.Result(128, "", "not a repo", argv)
        self.assertEqual(self.header("main-acme"), f"worktree: {self.repo}   branch: -")


class FindTest(StoreCase):
    U = f"acme-x-{'20261019'}-1"

    def put(self, text, unit=None, root=None):
        folder = self.units_dir(root) / (unit or self.U)
        folder.mkdir(parents=True, exist_ok=True)
        if text is not None:
            (folder / "BRIEF.md").write_bytes(text if isinstance(text, bytes) else text.encode())
        return folder

    def test_table(self):
        rows = [  # (BRIEF.md content or None for no file, rev, fields)
            (None, 0, {}),
            ("rev: 3\n\n## Brief u\nTO div-acme-build\nGOAL g\n", 3, {"TO": "div-acme-build", "GOAL": "g"}),
            ("## Brief u\nTO div-acme-build\n", 1, {"TO": "div-acme-build"}),
            ("rev: 2\nno mark here\n", 2, {}),
            (b"rev: 2\n\xff\xfe\n", 0, {}),
            ("STANDING\nto: someone-else\n\n## Brief u\nTO   div-acme-rnd\n", 1, {"TO": "div-acme-rnd"}),
            ("rev: 4\n## Brief notes in STANDING\nGOAL fake\n## Brief u\nGOAL real\n", 4, {"GOAL": "real"}),
        ]
        for text, rev, fields in rows:
            with self.subTest(text=text):
                self.put(text)
                b = brief.find(self.reg, self.U)
                self.assertEqual((b.unit, b.project, b.root, b.rev, b.fields), (self.U, "acme", self.repo, rev, fields))
                self.assertEqual(b.text, "" if rev == 0 else text)
                shutil.rmtree(self.units_dir())
        self.put("rev: 1\n## Brief u\nTO   div-acme-rnd\n")
        self.assertEqual(brief.find(self.reg, self.U).to, "div-acme-rnd")

    def test_none(self):
        self.put("rev: 1\n")
        for unit in ("", "nope", "acme-x-2026101-1", "../acme-x-20261019-1", None):
            self.assertIsNone(brief.find(self.reg, unit), unit)
        self.assertIsNone(brief.find(self.reg, "acme-y-20261019-1"))       # no folder
        self.assertIsNone(brief.find(self.reg, "zeta-x-20261019-1"))       # no project slug prefixes it
        self.assertIsNone(brief.find(self.reg, self.U, project="nope"))
        self.assertIsNone(brief.find({"projects": {}}, self.U))
        (self.units_dir() / "acme-file-20261019-1").write_text("not a folder")
        self.assertIsNone(brief.find(self.reg, "acme-file-20261019-1"))

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root reads mode 000 files")
    def test_unreadable_brief_is_rev_0(self):
        path = self.put("rev: 2\n## Brief u\nTO div-acme-build\n") / "BRIEF.md"
        path.chmod(0)
        self.addCleanup(path.chmod, 0o644)
        b = brief.find(self.reg, self.U)
        self.assertEqual((b.rev, b.fields, b.text), (0, {}, ""))
        self.assertIsNone(brief.newest(self.reg, "acme", "div-acme-build"))

    def test_longest_project_key_wins(self):
        shop = self.root / "acme-shop"
        self.reg["projects"]["acme-shop"] = {"path": str(shop)}
        unit = "acme-shop-x-20261019-1"
        self.put("rev: 1\n", unit)
        self.assertEqual(brief.find(self.reg, unit).project, "acme")          # only acme holds it
        self.put("rev: 2\n", unit, shop)
        self.assertEqual((brief.find(self.reg, unit).project, brief.find(self.reg, unit).rev), ("acme-shop", 2))
        b = brief.find(self.reg, unit, project="acme")
        self.assertEqual((b.project, b.root, b.rev), ("acme", self.repo, 1))
        self.put("rev: 1\n", None, shop)
        self.assertEqual(brief.find(self.reg, self.U, project="acme-shop").root, shop)   # no prefix test

    def test_newest(self):
        def put(unit, to, mtime):
            folder = self.put(f"rev: 1\n## Brief {unit}\nTO {to}\n", unit)
            os.utime(folder / "BRIEF.md", (mtime, mtime))
        self.assertIsNone(brief.newest(self.reg, "acme", "div-acme-build"))          # no units folder yet
        put("acme-a-20261018-1", "div-acme-build", 1000)
        put("acme-b-20261018-2", "div-acme-build", 3000)
        put("acme-c-20261019-1", "div-acme-rnd", 9000)
        self.put(None, "acme-d-20261019-3")
        (self.units_dir() / "scratch").mkdir()
        (self.units_dir() / "scratch" / "BRIEF.md").write_text("rev: 1\n## Brief s\nTO div-acme-build\n")
        os.utime(self.units_dir() / "scratch" / "BRIEF.md", (9999, 9999))
        self.assertEqual(brief.newest(self.reg, "acme", "div-acme-build").unit, "acme-b-20261018-2")
        self.assertEqual(brief.newest(self.reg, "acme", "div-acme-rnd").unit, "acme-c-20261019-1")
        put("acme-a-20261018-10", "div-acme-build", 3000)                          # tie: larger id
        self.assertEqual(brief.newest(self.reg, "acme", "div-acme-build").unit, "acme-a-20261018-10")
        put("acme-z-20261019-1", "div-acme-build", 3000)
        self.assertEqual(brief.newest(self.reg, "acme", "div-acme-build").unit, "acme-z-20261019-1")
        self.assertIsNone(brief.newest(self.reg, "acme", "main-acme"))
        self.assertIsNone(brief.newest(self.reg, "nope", "div-acme-build"))
        self.reg["projects"]["acme"]["path"] = ""
        self.assertIsNone(brief.newest(self.reg, "acme", "div-acme-build"))


# ---- the dispatch.py writer brief.write replaced (b6bbc18), frozen as ByteIdentityTest's reference --------

_OLD_KEYS = ("TO", "GOAL", "SCOPE", "CONTEXT", "ACCEPT", "VERIFY", "TIMEBOX", "FORBID", "REPORT", "TIER", "ROLLBACK")
_OLD_KV_KEYS = _OLD_KEYS + ("AMEND", "STOP")


def _old_allocate_unit_id(repo, proj, goal):
    import re
    from orche import mask, util
    date = units.now().date()
    goal = mask.redact(goal)[0]
    slug = util.slugify(" ".join(goal.split()[:4])) or "unit"
    base = Path(repo) / ".orche" / "units"
    base.mkdir(parents=True, exist_ok=True)
    pat = re.compile(rf"^{re.escape(util.slugify(proj))}-.+-{date.strftime('%Y%m%d')}-(\d+)$")
    n = 1 + max([int(m.group(1)) for m in (pat.match(p.name) for p in base.iterdir()) if m] or [0])
    while True:
        unit = f"{util.slugify(proj)}-{util.slugify(slug)}-{date.strftime('%Y%m%d')}-{n}"
        try:
            (base / unit).mkdir()
            return unit
        except FileExistsError:
            n += 1


def _old_current_rev(repo, unit):
    import re
    revs = [int(m.group(1)) for m in (re.fullmatch(r"BRIEF\.rev(\d+)\.md", p.name)
                                      for p in (Path(repo) / ".orche" / "units" / unit).glob("BRIEF.rev*.md")) if m]
    return max(revs or [0])


def _old_where(r, to, frm):
    t = r.member(to)
    owner = t is not None and t.owns_worktree
    root = str(t.home or "") if t is not None else ""
    if owner and t.worktree is not None:
        return (frm, str(t.worktree), t.branch or "-", root)
    recorded = t.branch if owner else ""
    res = shell.run(["git", "-C", root, "branch", "--show-current"], timeout=0.5) if root and not recorded else None
    branch = recorded or (res.out.strip() if res is not None and res.ok else "") or "-"
    return (frm, root + (" (main checkout)" if owner else ""), branch, root)


def _old_render_brief(unit, rev, standing, to, fields, at, supersedes=None, note=""):
    frm, worktree, branch, root = at
    lines = [f"rev: {rev}"]
    if supersedes:
        lines.append(f"supersedes: rev {supersedes}")
        if note:
            lines.append(f"supersedes-note: {note}")
    lines += [f"unit: {unit}",
              f"to: {to}   from: {frm}",
              f"worktree: {worktree}   branch: {branch}",
              f"project root: {root}   (brief + REPORT.md live under project root/.orche/units/{unit}/)",
              "Work only inside `worktree`. SCOPE paths are relative to it."]
    lines += ["", standing.rstrip(), "", f"persona: .claude/agents/{to}.md", "", f"## Brief {unit}"]
    for key in _OLD_KEYS:
        if key in fields:
            first, *more = str(fields[key]).split("\n")
            lines.append(f"{key:<9} {first}".rstrip())
            lines += [f"  {m}" for m in more]
    return "\n".join(lines) + "\n"


def _old_write_brief(repo, unit, rev, text):
    from orche import util
    folder = Path(repo) / ".orche" / "units" / unit
    util.atomic_write(folder / f"BRIEF.rev{rev}.md", text)
    util.atomic_write(folder / "BRIEF.md", text)


def _old_read_brief_fields(repo, unit):
    text = (Path(repo) / ".orche" / "units" / unit / "BRIEF.md").read_text(encoding="utf-8")
    body = text[text.rindex("\n## Brief ") + 1:].split("\n", 1)[1]
    fields, current = {}, None
    for raw in body.splitlines():
        if not raw.strip():
            continue
        if raw.startswith("  "):
            if current is not None:
                piece = raw[2:].rstrip()
                fields[current] = f"{fields[current]}\n{piece}" if fields[current] else piece
            continue
        key, _, value = raw.strip().partition(" ")
        current = key if key in _OLD_KV_KEYS else None
        if current:
            fields[key] = value.strip()
    return fields


class ByteIdentityTest(StoreCase):
    """brief.write renders every dispatch fixture byte for byte like the dispatch writer it replaces."""

    def snapshot(self):
        return {f"{d.name}/{f.name}": f.read_bytes() for d in sorted(self.units_dir().iterdir())
                for f in sorted(d.iterdir())}

    def old(self, fields, to, frm, amend):
        from orche import dispatch, roster
        r = roster.Roster(self.reg)
        standing = dispatch.standing_text(self.reg, "acme")
        unit = _old_allocate_unit_id(self.repo, "acme", fields["GOAL"])
        at = _old_where(r, to, frm)
        _old_write_brief(self.repo, unit, 1, _old_render_brief(unit, 1, standing, to, {**fields, "TO": to}, at))
        if amend is not None:
            prev = _old_current_rev(self.repo, unit)
            merged = {**_old_read_brief_fields(self.repo, unit), **amend.fields, "TO": to}
            _old_write_brief(self.repo, unit, prev + 1, _old_render_brief(
                unit, prev + 1, standing, to, merged, _old_where(r, to, frm), prev or None, amend.note))
        return unit

    def new(self, fields, to, frm, amend):
        target = self.member(to)
        b = brief.write(Block("brief", fields), target, frm, STANDING)
        self.assertEqual(brief.find(self.reg, b.unit), b)
        if amend is not None:
            a = brief.write(dataclasses.replace(amend, unit=b.unit), target, frm, STANDING, prior=b)
            self.assertEqual(brief.find(self.reg, b.unit), a)
            self.assertEqual((b.folder / "BRIEF.rev1.md").read_text(), b.text)
        return b.unit

    def test_fixtures(self):
        subprocess.run(["git", "init", "-q", "-b", "trunk"], cwd=self.repo, check=True, capture_output=True)
        text = json.loads(SAMPLE.read_text().splitlines()[1])["message"]["content"][0]["text"]
        sample = brief.parse(text.split("```dispatch\n", 1)[1].split("\n```", 1)[0]).fields
        amend = brief.parse("AMEND acme-x-20261019-1 rev 2\nSUPERSEDES rev 1: no font change\n"
                            "GOAL      Add coupon field without touching fonts")
        drift = Block("amend", {"VERIFY": "make check\n  # still content"}, "acme-x-20261019-1", 7)
        design = {"worktree": f"repo-acme::{self.root / 'acme-design'}", "branch": "div/design",
                  "session": "div-acme-design", "status": "active"}
        cases = [  # (name, registry edit, fields, to, from, amend)
            ("division", None, sample, "div-acme-build", "main-acme", amend),
            ("full", None, FIELDS, "div-acme-build", "main-acme", drift),
            ("dormant", None, sample, "div-acme-rnd", "main-acme", None),
            ("custom", lambda: self.reg["projects"]["acme"]["divisions"].update(design=design),
             sample, "div-acme-design", "main-acme", amend),
            ("main", None, sample, "main-acme", "div-acme-build", amend),
            ("feature", None, sample, "feat-acme-checkout", "div-acme-build", amend),
            ("bare", lambda: self.feature(worktree=""), sample, "feat-acme-checkout", "div-acme-build", None),
            ("bare-no-branch", lambda: self.feature(worktree="", branch=""), FIELDS, "feat-acme-checkout",
             "div-acme-build", drift),
        ]
        for name, edit, fields, to, frm, amend_block in cases:
            with self.subTest(name):
                if edit:
                    edit()
                old_unit = self.old(fields, to, frm, amend_block)
                old = self.snapshot()
                shutil.rmtree(self.units_dir())
                self.assertEqual(self.new(fields, to, frm, amend_block), old_unit)
                self.assertEqual(self.snapshot(), old)
                self.assertEqual(len(old), 3 if amend_block else 2)
                shutil.rmtree(self.units_dir())


class ImportTest(unittest.TestCase):
    def test_closure(self):
        code = ("import sys; sys.path.insert(0, sys.argv[1]); import orche.brief; "
                "print(' '.join(sorted(m for m in sys.modules if m == 'orche' or m.startswith('orche.'))))")
        out = subprocess.run([sys.executable, "-c", code, str(support.ROOT / "lib")], capture_output=True,
                             text=True, check=True).stdout.split()
        self.assertLessEqual(set(out), CLOSURE)
        self.assertIn("orche.brief", out)
