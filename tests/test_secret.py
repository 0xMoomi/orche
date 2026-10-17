"""Keymaster: `orche secret …` / `orche dev …` with a fake shell runner and a fake dialog UI.

A canary value is stored through the dialog and injected by `run`; it must never appear in
captured stdout/stderr, logs, audit, queue or shares files, or any command argv.
"""
import copy
import hashlib
import io
import json
import os
import subprocess
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import support
from orche import registry, shell
from orche.secrets import SecretError, backends_gcp, backends_gh, backends_keychain, dialog, share, store
from test_registry import load_cli

SAMPLE = json.loads((support.FIXTURES / "registry.sample.json").read_text())
CANARY = "sk_test_" + "CANARY" + "x7Q2m9Lp4Rz8"  # synthetic, built at runtime
OTHER = "tok-" + "other" + "-9f8e7d6c"
GH_TOKEN = "ghp_" + "A1b2C3d4" * 5  # synthetic github-token shape for redaction counts
AWS_KEY = "AKIA" + "ABCDEFGHIJKLMNOP"
PUBKEY = "age1" + "q" * 58
SECRETS_CFG = {"backend": "gcp", "gcp_project": {"dev": "acme-dev", "prod": "acme-prod"},
               "gh_repo": "acme/shop", "cache_local": True}


class FakeCloud:
    """In-memory gcloud / security / age / tar / vercel / gh / osascript / pb* behaviour."""

    def __init__(self):
        self.calls = []  # (argv, input)
        self.gcp = {}  # (project, sid) -> [[value, state], ...]
        self.keychain = {}
        self.age_inputs = []
        self.fail_writes = False

    def __call__(self, argv, *, timeout, input):
        self.calls.append((list(argv), input))
        ok = shell.Result(0, "", "", list(argv))
        if argv[:2] == ["gcloud", "secrets"]:
            return self._gcloud(argv, input)
        if argv[:2] == ["security", "-i"]:
            words = input.split('"')
            self.keychain[words[1]] = words[3]
            return ok
        if argv[:2] == ["security", "find-generic-password"]:
            svc = argv[argv.index("-s") + 1]
            if svc in self.keychain:
                return shell.Result(0, self.keychain[svc] + "\n", "", list(argv))
            return shell.Result(44, "", "The specified item could not be found in the keychain.", list(argv))
        if argv[0] == "age":
            self.age_inputs.append(input)
        if argv[0] == "tar":
            Path(argv[2]).write_bytes(b"fake-tarball")
        return ok

    def _gcloud(self, argv, input):
        def opt(name):
            return argv[argv.index(name) + 1]
        res = lambda code=0, out="": shell.Result(code, out, "" if code == 0 else "NOT_FOUND", list(argv))  # noqa: E731
        verb = argv[2:4]
        if self.fail_writes and (verb[0] == "create" or verb == ["versions", "add"]):
            return res(1)
        if verb[0] == "describe":
            return res(0 if (opt("--project"), argv[3]) in self.gcp else 1)
        if verb[0] == "create":
            self.gcp[(opt("--project"), argv[3])] = [[input, "ENABLED"]]
            return res()
        if verb == ["versions", "add"]:
            self.gcp[(opt("--project"), argv[4])].append([input, "ENABLED"])
            return res()
        if verb == ["versions", "access"]:
            versions = self.gcp.get((opt("--project"), opt("--secret")))
            return res(0, versions[-1][0]) if versions else res(1)
        if verb == ["versions", "list"]:
            versions = self.gcp[(opt("--project"), argv[4])]
            return res(0, json.dumps([{"name": f"projects/p/secrets/{argv[4]}/versions/{i + 1}", "state": s}
                                      for i, (_, s) in enumerate(versions)]))
        if verb == ["versions", "disable"]:
            self.gcp[(opt("--project"), opt("--secret"))][int(argv[4]) - 1][1] = "DISABLED"
            return res()
        return res(1)

    def argvs(self, prefix=()):
        return [a for a, _ in self.calls if a[:len(prefix)] == list(prefix)]


class FakeUI:
    def __init__(self, answers, clipboard=""):
        self.answers = answers if isinstance(answers, dict) else list(answers)
        self.clipboard = clipboard
        self.asked, self.errors, self.notes, self.cleared = [], [], [], 0

    def ask(self, req, error=None):
        self.asked.append(req["name"])
        self.errors.append(error)
        queue = self.answers.get(req["name"], []) if isinstance(self.answers, dict) else self.answers
        return queue.pop(0) if queue else (dialog.CANCEL, "")

    def paste(self):
        return self.clipboard

    def clear_clipboard(self):
        self.cleared += 1
        self.clipboard = ""

    def notify(self, text):
        self.notes.append(text)


class SecretCase(support.TempHomeCase):
    def setUp(self):
        super().setUp()
        self.cli = load_cli()
        reg = copy.deepcopy(SAMPLE)
        reg["projects"]["acme-shop"]["secrets"] = copy.deepcopy(SECRETS_CFG)
        registry.save(reg)
        self.cloud = FakeCloud()
        self.addCleanup(setattr, shell, "RUNNER", shell.RUNNER)
        shell.RUNNER = self.cloud
        self.spawned = []
        self.addCleanup(setattr, store, "spawn_dialog", store.spawn_dialog)
        store.spawn_dialog = lambda: self.spawned.append(1) or object()
        self.addCleanup(setattr, dialog, "UI_FACTORY", dialog.UI_FACTORY)
        self.outputs = []
        self.repo = self.home / "repo"
        (self.repo / ".git").mkdir(parents=True)
        self._old_keymaster = os.environ.pop("ORCHE_KEYMASTER", None)
        self.addCleanup(self._restore_keymaster)

    def _restore_keymaster(self):
        os.environ.pop("ORCHE_KEYMASTER", None)
        if self._old_keymaster is not None:
            os.environ["ORCHE_KEYMASTER"] = self._old_keymaster

    def orche(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = self.cli.main(list(argv))
        self.outputs += [out.getvalue(), err.getvalue()]
        return code, out.getvalue(), err.getvalue()

    def use_ui(self, ui):
        dialog.UI_FACTORY = lambda ex: ui
        return ui

    def fill_canary(self, envs="dev,prod"):
        self.orche("secret", "request", "STRIPE_SECRET_KEY", "--env", envs, "--why", "payments",
                   "--how", "https://dashboard.stripe.example/apikeys", "--project", "acme-shop", "--no-dialog")
        ui = self.use_ui(FakeUI([(dialog.PASTE, "")], clipboard=CANARY + "\n"))
        code, _, _ = self.orche("secret", "fill")
        self.assertEqual(code, 0)
        return ui

    def assert_no_leak(self, *values, skip=None):
        values = values or (CANARY,)
        for value in values:
            for text in self.outputs:
                self.assertNotIn(value, text)
            for path in self.home.rglob("*"):
                if skip and skip in path.parents:
                    continue
                if path.is_file() and ".git" not in path.parts and path.suffix != ".gz":
                    self.assertNotIn(value, path.read_text(errors="ignore"), str(path))
            for argv, _ in self.cloud.calls:
                self.assertNotIn(value, " ".join(argv))


class ValidateFormatTest(unittest.TestCase):
    def test_cases(self):
        good = [("STRIPE_SECRET_KEY", "sk_live_abc123"), ("STRIPE_KEY", "sk_test_abc"), ("STRIPE_R", "rk_live_1"),
                ("STRIPE_PUBLISHABLE_KEY", "pk_live_abc123"), ("STRIPE_PUBLISHABLE_KEY", "pk_test_abc123"),
                ("STRIPE_WEBHOOK_SECRET", "whsec_abc123"),
                ("WALLET_PRIVATE_KEY", "ab" * 32), ("WALLET_PRIVATE_KEY", "0x" + "cd" * 32),
                ("TLS_PRIVATE_KEY", "-----BEGIN PRIVATE KEY-----\nMIIB\n-----END PRIVATE KEY-----"),
                ("AWS_ACCESS_KEY_ID", AWS_KEY), ("GITHUB_TOKEN", GH_TOKEN),
                ("GOOGLE_API_KEY", "AIza" + "x" * 35), ("API_TOKEN", "abcdefgh")]
        for name, value in good:
            self.assertEqual(store.validate_format(name, value), (True, ""), name)
        bad = [("STRIPE_SECRET_KEY", "pk_abc12345"), ("STRIPE_SECRET_KEY", "xwhsec_12"), ("WALLET_PRIVATE_KEY", "ab" * 31),
               ("AWS_ACCESS_KEY_ID", "ASIA1234"), ("GITHUB_TOKEN", "gha_123"), ("GOOGLE_API_KEY", "Aiza1234"),
               ("API_TOKEN", "short"), ("API_TOKEN", "has space here"), ("API_TOKEN", "")]
        for name, value in bad:
            ok, reason = store.validate_format(name, value)
            self.assertFalse(ok, name)
            if value:
                self.assertNotIn(value, reason)


class RegistrySecretsSchemaTest(unittest.TestCase):
    def reg(self, sec):
        reg = copy.deepcopy(SAMPLE)
        reg["projects"]["acme-shop"]["secrets"] = sec
        return registry.validate(reg)

    def test_valid_and_optional(self):
        self.assertEqual(self.reg(copy.deepcopy(SECRETS_CFG)), [])
        self.assertEqual(self.reg({}), [])
        self.assertEqual(self.reg({"backend": "vercel", "vercel_project": "shop"}), [])

    def test_errors(self):
        self.assertTrue(any(".secrets.backend" in e for e in self.reg({"backend": "aws"})))
        self.assertTrue(any(".gh_repo" in e for e in self.reg({"gh_repo": "noslash"})))
        self.assertTrue(any(".gcp_project.qa" in e for e in self.reg({"gcp_project": {"qa": "x"}})))
        self.assertTrue(any(".cache_local" in e for e in self.reg({"cache_local": "yes"})))
        self.assertTrue(any("unknown key" in e for e in self.reg({"backnd": "gcp"})))
        self.assertTrue(any(".secrets: must be an object" in e for e in self.reg([])))


class BackendNamingTest(unittest.TestCase):
    def test_gcp_locate(self):
        cfg = {"project": "p", "gcp_project": {"dev": "a-dev", "prod": "a-prod"}}
        self.assertEqual(backends_gcp.locate(cfg, "K", "prod"), ("a-prod", "K"))
        with self.assertRaises(SecretError):
            backends_gcp.locate(cfg, "K", "preview")
        single = {"project": "p", "gcp_project": {"dev": "a-all"}}
        self.assertEqual(backends_gcp.locate(single, "K", "prod"), ("a-all", "K__prod"))
        self.assertEqual(backends_gcp.locate(single, "K", "dev"), ("a-all", "K__dev"))

    def test_gh_and_keychain_names(self):
        self.assertEqual(backends_gh.secret_name("K", "prod"), "K")
        self.assertEqual(backends_gh.secret_name("K", "dev"), "K_DEV")
        self.assertEqual(backends_keychain.service("acme", "dev", "K"), "orche/acme/dev/K")

    def test_parse_answer(self):
        self.assertEqual(dialog.parse_answer("button returned:Enter, text returned:a, b\n"), ("Enter", "a, b"))
        self.assertEqual(dialog.parse_answer("garbage"), (dialog.CANCEL, ""))


class RequestTest(SecretCase):
    def test_request_writes_queue_and_notifies(self):
        code, out, err = self.orche("secret", "request", "STRIPE_SECRET_KEY", "--env", "dev,prod", "--why",
                                    "payments", "--how", "https://x.example/keys", "--project", "acme")
        self.assertEqual((code, err), (0, ""))
        files = list((self.home / "secrets" / "requests").glob("*.json"))
        self.assertEqual(len(files), 1)
        self.assertEqual(out, f"requested {files[0].stem}\n")
        self.assertRegex(files[0].stem, r"^\d{8}T\d{6}-STRIPE_SECRET_KEY$")
        req = json.loads(files[0].read_text())
        self.assertEqual(sorted(req), ["at", "envs", "how_url", "name", "project", "requested_by", "status", "why"])
        self.assertEqual((req["envs"], req["status"], req["project"]), (["dev", "prod"], "pending", "acme-shop"))
        notes = self.cloud.argvs(["osascript", "-e"])
        self.assertEqual(len(notes), 1)
        self.assertTrue(notes[0][2].startswith('display notification "STRIPE_SECRET_KEY needed for acme-shop'))
        self.assertEqual(self.spawned, [1])
        # a second request for the same name extends the pending one; --no-dialog spawns nothing
        self.orche("secret", "request", "STRIPE_SECRET_KEY", "--env", "preview", "--why", "p", "--project",
                   "acme", "--no-dialog")
        self.assertEqual(len(list((self.home / "secrets" / "requests").glob("*.json"))), 1)
        self.assertEqual(json.loads(files[0].read_text())["envs"], ["dev", "prod", "preview"])
        self.assertEqual(self.spawned, [1])

    def test_request_dry_run_and_bad_input(self):
        code, out, _ = self.orche("--dry-run", "secret", "request", "API_TOKEN", "--env", "dev", "--why", "x",
                                  "--project", "acme-shop")
        self.assertEqual(code, 0)
        self.assertIn("would run: osascript -e", out)
        self.assertEqual(self.cloud.calls, [])
        self.assertFalse(any((self.home / "secrets" / "requests").glob("*.json")))
        self.assertEqual(self.spawned, [])
        self.assertEqual(self.orche("secret", "request", "lower", "--env", "dev", "--why", "x",
                                    "--project", "acme")[0], 1)
        self.assertEqual(self.orche("secret", "request", "API_TOKEN", "--env", "qa", "--why", "x",
                                    "--project", "acme")[0], 1)
        self.assertEqual(self.orche("secret", "request", "API_TOKEN", "--env", "dev", "--why", "x",
                                    "--project", "nope")[0], 2)


    def test_dialog_script_runs_standalone(self):
        script = Path(dialog.__file__)
        cp = subprocess.run([sys.executable, "-I", str(script)], capture_output=True, text=True, timeout=30,
                            env={**os.environ, "ORCHE_KEYMASTER": "1"})
        self.assertEqual((cp.returncode, cp.stdout, cp.stderr), (0, "", ""))
        self.assertIn("dialog done", (self.home / "logs" / "keymaster.log").read_text())
        self.assertFalse((self.home / "secrets" / "dialog.pid").exists())


class FillTest(SecretCase):
    def test_fill_stores_everywhere_and_clears_clipboard(self):
        ui = self.fill_canary()
        self.assertEqual(ui.cleared, 1)
        self.assertEqual(self.cloud.gcp[("acme-dev", "STRIPE_SECRET_KEY")], [[CANARY, "ENABLED"]])
        self.assertEqual(self.cloud.gcp[("acme-prod", "STRIPE_SECRET_KEY")], [[CANARY, "ENABLED"]])
        self.assertIn(["gcloud", "secrets", "create", "STRIPE_SECRET_KEY", "--project", "acme-dev",
                       "--replication-policy", "automatic", "--data-file=-"], self.cloud.argvs())
        self.assertIn(["gh", "secret", "set", "STRIPE_SECRET_KEY_DEV", "--repo", "acme/shop"], self.cloud.argvs())
        self.assertIn(["gh", "secret", "set", "STRIPE_SECRET_KEY", "--repo", "acme/shop"], self.cloud.argvs())
        self.assertEqual(self.cloud.keychain["orche/acme-shop/dev/STRIPE_SECRET_KEY"], CANARY)
        req = store.list_requests()[0][1]
        self.assertEqual(req["status"], "filled")
        self.assertEqual(ui.notes, ["STRIPE_SECRET_KEY stored (dev,prod)"])
        audit = (self.home / "secrets" / "audit.log").read_text()
        self.assertEqual(audit.count("\tstore\tacme-shop\tSTRIPE_SECRET_KEY\t"), 6)
        self.assertIsNone(os.environ.get("ORCHE_KEYMASTER"))
        self.assertEqual(self.orche("secret", "wait", "STRIPE_SECRET_KEY", "--timeout", "0")[:2], (0, "READY\n"))
        self.assert_no_leak()

    def test_invalid_then_valid_and_cancel(self):
        self.orche("secret", "request", "STRIPE_SECRET_KEY", "--env", "dev", "--why", "p", "--project", "acme",
                   "--no-dialog")
        self.orche("secret", "request", "API_TOKEN", "--env", "dev", "--why", "p", "--project", "acme",
                   "--no-dialog")
        ui = self.use_ui(FakeUI({"STRIPE_SECRET_KEY": [(dialog.ENTER, "pub_wrong123"), (dialog.ENTER, CANARY)],
                                 "API_TOKEN": [(dialog.CANCEL, "")]}))
        code, out, _ = self.orche("secret", "fill")
        self.assertEqual(code, 0)
        self.assertEqual(sorted(ui.asked), ["API_TOKEN", "STRIPE_SECRET_KEY", "STRIPE_SECRET_KEY"])
        self.assertEqual(len([e for e in ui.errors if e]), 1)
        self.assertEqual(ui.cleared, 3)  # typed-invalid, typed-valid, cancel
        self.assertIn("filled:", out)
        self.assertIn("skipped:", out)
        status = {r["name"]: r["status"] for _, r in store.list_requests()}
        self.assertEqual(status, {"STRIPE_SECRET_KEY": "filled", "API_TOKEN": "pending"})
        self.assertEqual(self.orche("secret", "wait", "API_TOKEN", "--timeout", "0")[:2], (2, "TIMEOUT\n"))
        self.assertEqual(self.orche("secret", "wait", "NEVER_ASKED", "--timeout", "0")[0], 2)
        self.assert_no_leak()

    def test_fill_dry_run_plans_with_placeholder(self):
        self.orche("secret", "request", "API_TOKEN", "--env", "dev", "--why", "p", "--project", "acme",
                   "--no-dialog")
        self.cloud.calls.clear()
        ui = self.use_ui(FakeUI([]))
        code, out, _ = self.orche("--dry-run", "secret", "fill")
        self.assertEqual(code, 0)
        self.assertEqual(self.cloud.calls, [])
        self.assertEqual(ui.asked, [])
        self.assertIn("would run: gcloud secrets versions add API_TOKEN --project acme-dev --data-file=- <<< <value>",
                      out)
        self.assertIn("would run: security -i <<< 'add-generic-password -U -a orche -s "
                      "\"orche/acme-shop/dev/API_TOKEN\" -w <value>'", out)
        self.assertEqual(store.list_requests()[0][1]["status"], "pending")


class AddLsMissingTest(SecretCase):
    def test_add_ls_missing(self):
        self.addCleanup(setattr, store, "GETPASS", store.GETPASS)
        store.GETPASS = lambda prompt: OTHER
        code, out, _ = self.orche("secret", "add", "API_TOKEN", "--env", "dev,prod", "--project", "acme")
        self.assertEqual(code, 0, out)
        self.assertIn("stored API_TOKEN (dev) -> gcp, gh, keychain", out)
        store.GETPASS = lambda prompt: CANARY
        self.orche("secret", "add", "STRIPE_SECRET_KEY", "--env", "dev", "--project", "acme")
        (self.repo / ".env.example").write_text("# names\nAPI_TOKEN=\nexport SENTRY_DSN=\n")
        code, out, _ = self.orche("secret", "ls", "--project", "acme")
        self.assertEqual(out, "API_TOKEN\tdev,prod\nSTRIPE_SECRET_KEY\tdev\n")
        code, out, _ = self.orche("secret", "missing", "prod", "--project", "acme", "--repo", str(self.repo))
        self.assertEqual((code, out), (0, "SENTRY_DSN\nSTRIPE_SECRET_KEY\n"))
        code, out, _ = self.orche("--json", "secret", "missing", "dev", "--project", "acme", "--repo", str(self.repo))
        self.assertEqual(json.loads(out)["missing"], ["SENTRY_DSN"])
        store.GETPASS = lambda prompt: "bad"
        self.assertEqual(self.orche("secret", "add", "API_TOKEN", "--env", "dev", "--project", "acme")[0], 1)
        self.assert_no_leak(CANARY, OTHER)

    def test_add_dry_run_never_prompts_or_runs(self):
        self.addCleanup(setattr, store, "GETPASS", store.GETPASS)
        store.GETPASS = lambda prompt: self.fail("prompted in dry-run")
        code, out, _ = self.orche("--dry-run", "secret", "add", "API_TOKEN", "--env", "prod", "--project", "acme")
        self.assertEqual(code, 0)
        self.assertEqual(self.cloud.calls, [])
        self.assertIn("gh secret set API_TOKEN --repo acme/shop <<< <value>", out)
        self.assertFalse((self.home / "secrets" / "audit.log").exists())


class RunTest(SecretCase):
    CHILD = ("import hashlib, json, os; "
             "print(json.dumps(sorted(os.environ))); "
             "print(hashlib.sha256(os.environ.get('STRIPE_SECRET_KEY', '').encode()).hexdigest())")

    def setUp(self):
        super().setUp()
        self.fill_canary(envs="dev")
        self.cloud.gcp[("acme-dev", "API_TOKEN")] = [[OTHER, "ENABLED"]]
        self.cloud.gcp[("acme-dev", "UNLISTED_SECRET")] = [["unlisted-value-123", "ENABLED"]]
        (self.repo / ".env.example").write_text("STRIPE_SECRET_KEY=\n")
        self.child = {}
        self.addCleanup(setattr, store, "EXEC", store.EXEC)

        def fake_exec(file, argv, env):
            cp = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=30)
            self.child = {"argv": argv, "out": cp.stdout, "err": cp.stderr, "code": cp.returncode}
        store.EXEC = fake_exec

    def test_run_injects_only_listed_names(self):
        code, out, err = self.orche("secret", "run", "--project", "acme", "--repo", str(self.repo),
                                    "--names", "API_TOKEN", "--", sys.executable, "-c", self.CHILD)
        self.assertEqual((code, out, err), (0, "", ""))
        self.assertEqual(self.child["code"], 0, self.child["err"])
        keys, digest = self.child["out"].splitlines()
        keys = json.loads(keys)
        self.assertIn("STRIPE_SECRET_KEY", keys)
        self.assertIn("API_TOKEN", keys)
        self.assertNotIn("UNLISTED_SECRET", keys)
        self.assertNotIn("ORCHE_KEYMASTER", keys)
        self.assertEqual(digest, hashlib.sha256(CANARY.encode()).hexdigest())
        self.assertNotIn("STRIPE_SECRET_KEY", os.environ)
        self.assertIsNone(os.environ.get("ORCHE_KEYMASTER"))
        self.assertIn("\trun\tacme-shop\tSTRIPE_SECRET_KEY,API_TOKEN\tdev\t",
                      (self.home / "secrets" / "audit.log").read_text())
        self.assert_no_leak(CANARY, OTHER)

    def test_run_missing_and_dry_run(self):
        code, _, err = self.orche("secret", "run", "--project", "acme", "--repo", str(self.repo),
                                  "--names", "NOT_STORED", "--", "true")
        self.assertEqual(code, 2)
        self.assertIn("NOT_STORED", err)
        self.assertEqual(self.child, {})
        self.cloud.calls.clear()
        code, out, _ = self.orche("--dry-run", "secret", "run", "--project", "acme", "--repo", str(self.repo),
                                  "--", "pnpm", "dev")
        self.assertEqual(code, 0)
        self.assertEqual(self.cloud.calls, [])
        self.assertEqual(self.child, {})
        self.assertIn("would exec: pnpm dev with STRIPE_SECRET_KEY", out)
        self.assertEqual(self.orche("secret", "run", "--project", "acme", "--repo", str(self.repo))[0], 1)
        self.assert_no_leak()

    def test_render(self):
        out_file = self.repo / ".env.local"
        code, out, err = self.orche("secret", "render", "--env", "dev", "--out", str(out_file), "--project",
                                    "acme", "--repo", str(self.repo))
        self.assertEqual(code, 0)
        self.assertIn("agents cannot read it", err)
        self.assertEqual(out_file.stat().st_mode & 0o777, 0o600)
        self.assertEqual(out_file.read_text(), f'STRIPE_SECRET_KEY="{CANARY}"\n')
        out_file.unlink()
        code, _, err = self.orche("secret", "render", "--out", str(self.repo / ".env.example"), "--project", "acme")
        self.assertEqual(code, 3)
        self.assert_no_leak()

    def test_render_refuses_existing_out_without_force(self):
        out_file = self.repo / ".env.local"
        out_file.write_text("HAND_KEPT=1\n")
        code, _, err = self.orche("secret", "render", "--out", str(out_file), "--project", "acme",
                                  "--repo", str(self.repo))
        self.assertEqual(code, 3)
        self.assertIn("exists; refusing to overwrite", err)
        self.assertEqual(out_file.read_text(), "HAND_KEPT=1\n")
        self.assertEqual(list(self.repo.glob(".env.bak-*")), [])

    def test_render_force_backs_up_old_file(self):
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        cloud = self.cloud

        def runner(argv, *, timeout, input):  # real git for the info/exclude helper
            if argv[:1] == ["git"]:
                return shell._subprocess_runner(argv, timeout=timeout, input=input)
            return cloud(argv, timeout=timeout, input=input)
        shell.RUNNER = runner
        out_file = self.repo / ".env.local"
        original = b"HAND_KEPT=1\r\nLATIN=caf\xe9\r\n"  # CRLF + not UTF-8
        out_file.write_bytes(original)
        for _ in range(2):
            code, _, err = self.orche("secret", "render", "--out", str(out_file), "--project", "acme",
                                      "--repo", str(self.repo), "--force")
            self.assertEqual(code, 0, err)
        baks = sorted(self.repo.glob(".env.bak-*.local"), key=lambda p: p.stat().st_mtime_ns)
        self.assertEqual(len(baks), 2)  # one backup per run
        bak = baks[0]
        self.assertRegex(bak.name, r"^\.env\.bak-\d{8}T\d{6}(-\d+)?\.local$")
        self.assertEqual(bak.read_bytes(), original)
        self.assertEqual(bak.stat().st_mode & 0o777, 0o600)
        exclude = (self.repo / ".git" / "info" / "exclude").read_text().splitlines()
        self.assertEqual(exclude.count(".env.bak-*.local"), 1)
        status = subprocess.run(["git", "status", "--porcelain", "--untracked-files=all"], cwd=self.repo,
                                capture_output=True, text=True).stdout
        self.assertNotIn(".env.bak-", status)
        for b in baks:
            b.unlink()
        self.assertEqual(out_file.read_text(), f'STRIPE_SECRET_KEY="{CANARY}"\n')
        out_file.unlink()
        self.assert_no_leak()

    def test_render_out_must_be_dotenv_name(self):
        for name in ("notes.txt", "env.local", ".envrc", "x.env"):
            code, _, err = self.orche("secret", "render", "--out", str(self.repo / name), "--project", "acme",
                                      "--repo", str(self.repo))
            self.assertEqual(code, 3, name)
            self.assertIn("must be a .env", err)
            self.assertFalse((self.repo / name).exists())
        code, _, err = self.orche("secret", "render", "--out", str(self.repo / ".ENV.Production"), "--project",
                                  "acme", "--repo", str(self.repo))
        self.assertEqual(code, 0, err)
        (self.repo / ".ENV.Production").unlink()
        self.assert_no_leak()


class ShareRevokeTest(SecretCase):
    def setUp(self):
        super().setUp()
        self.fill_canary(envs="dev")
        self.cloud.calls.clear()

    def test_dev_add_refused_in_single_gcp_project_layout(self):
        reg = registry.load()
        reg["projects"]["acme-shop"]["secrets"]["gcp_project"] = {"dev": "acme-all", "prod": "acme-all"}
        registry.save(reg)
        self.cloud.calls.clear()
        code, _, err = self.orche("dev", "add", "dana@example.com", "--project", "acme")
        self.assertEqual(code, 3)
        self.assertIn("prod and dev share one GCP project; per-secret IAM is not implemented — "
                      "split projects or grant manually", err)
        self.assertEqual(self.cloud.argvs(["gcloud", "projects"]), [])
        self.assertNotIn("dana@example.com", json.dumps(share.load()))

    def test_dev_add_share_revoke(self):
        code, out, _ = self.orche("dev", "add", "dana@example.com", "--project", "acme", "--age-pubkey", PUBKEY)
        self.assertEqual(code, 0, out)
        self.assertEqual(self.cloud.argvs(["gcloud", "projects"]), [[
            "gcloud", "projects", "add-iam-policy-binding", "acme-dev", "--member", "user:dana@example.com",
            "--role", "roles/secretmanager.secretAccessor"]])
        self.assertEqual(self.orche("dev", "add", "dana@example.com", "--project", "acme", "--env", "prod")[0], 3)
        out_file = self.repo / ".env.dev.age"
        code, out, _ = self.orche("secret", "share", "--to", "dana", "--env", "dev", "--project", "acme",
                                  "--out", str(out_file))
        self.assertEqual(code, 0, out)
        self.assertEqual(self.cloud.argvs(["age"]), [["age", "-r", PUBKEY, "-o", str(out_file)]])
        self.assertEqual(self.cloud.age_inputs, [f'STRIPE_SECRET_KEY="{CANARY}"\n'])
        shares = json.loads((self.home / "secrets" / "shares.json").read_text())
        self.assertEqual(shares["shares"][0]["secrets"], ["STRIPE_SECRET_KEY"])
        self.assertEqual(shares["devs"]["dana@example.com"]["age_pubkey"], PUBKEY)

        # revoke: rotates exactly the shared names (dialog supplies the new value)
        new_value = "sk_live_" + "rotated" + "Z9y8x7"
        ui = self.use_ui(FakeUI([(dialog.ENTER, new_value)]))
        code, out, err = self.orche("secret", "revoke", "dana@example.com", "--yes")
        self.assertEqual(code, 0, err)
        self.assertEqual(ui.asked, ["STRIPE_SECRET_KEY"])
        rotations = [r for _, r in store.list_requests() if r.get("mode") == "rotate"]
        self.assertEqual([(r["name"], r["envs"], r["status"]) for r in rotations],
                         [("STRIPE_SECRET_KEY", ["dev"], "filled")])
        self.assertEqual(self.cloud.gcp[("acme-dev", "STRIPE_SECRET_KEY")],
                         [[CANARY, "DISABLED"], [new_value, "ENABLED"]])
        self.assertIn(["gcloud", "secrets", "versions", "disable", "1", "--secret", "STRIPE_SECRET_KEY",
                       "--project", "acme-dev"], self.cloud.argvs())
        self.assertTrue(json.loads((self.home / "secrets" / "shares.json").read_text())["shares"][0]["revoked"])
        self.assertEqual(self.orche("secret", "revoke", "dana", "--yes")[0], 2)

        code, out, _ = self.orche("dev", "remove", "dana@example.com")
        self.assertEqual(code, 0)
        self.assertEqual(self.cloud.argvs(["gcloud", "projects"])[-1][2], "remove-iam-policy-binding")
        self.assertEqual(len(json.loads((self.home / "secrets" / "shares.json").read_text())["shares"]), 1)
        self.assert_no_leak(CANARY, new_value)

    def test_share_dry_run_and_revoke_confirm(self):
        self.orche("dev", "add", "dana@example.com", "--project", "acme", "--age-pubkey", PUBKEY)
        self.cloud.calls.clear()
        code, out, _ = self.orche("--dry-run", "secret", "share", "--to", "dana", "--project", "acme",
                                  "--out", str(self.repo / ".env.dev.age"))
        self.assertEqual(code, 0)
        self.assertEqual(self.cloud.calls, [])
        self.assertIn(f"age -r {PUBKEY} -o", out)
        self.assertIn("<<< <dotenv: STRIPE_SECRET_KEY>", out)
        self.assertEqual(json.loads((self.home / "secrets" / "shares.json").read_text())["shares"], [])
        self.orche("secret", "share", "--to", "dana", "--project", "acme", "--out", str(self.repo / "x.age"))
        self.addCleanup(setattr, store, "CONFIRM", store.CONFIRM)
        store.CONFIRM = lambda prompt: "n"
        self.assertEqual(self.orche("secret", "revoke", "dana", "--no-dialog")[0], 1)
        self.assertFalse([r for _, r in store.list_requests() if r.get("mode") == "rotate"])
        code, out, _ = self.orche("--dry-run", "secret", "revoke", "dana")
        self.assertEqual(code, 0)
        self.assertIn("gcloud secrets versions disable '<each older enabled version>'", out)
        self.assertFalse([r for _, r in store.list_requests() if r.get("mode") == "rotate"])
        store.CONFIRM = lambda prompt: "y"
        code, out, _ = self.orche("secret", "revoke", "dana", "--no-dialog")
        self.assertEqual(code, 0)
        self.assertIn("rotated: -\npending: acme-shop/STRIPE_SECRET_KEY\n", out)
        self.assertEqual([r["name"] for _, r in store.list_requests(status="pending")], ["STRIPE_SECRET_KEY"])
        entry = self.shares()[0]
        self.assertNotIn("revoked", entry)
        self.assertTrue(entry["revoke_requested"])
        # a second revoke re-queues (merges into) the pending rotation instead of "nothing shared"
        code, out, _ = self.orche("secret", "revoke", "dana", "--yes", "--no-dialog")
        self.assertEqual(code, 0)
        self.assertEqual(len(store.list_requests(status="pending")), 1)
        # the human fills it later; the next revoke sees the filled rotation and completes
        self.use_ui(FakeUI([(dialog.ENTER, "sk_live_" + "later" + "Rot8ed99")]))
        self.orche("secret", "fill")
        self.cloud.calls.clear()
        code, out, _ = self.orche("secret", "revoke", "dana", "--yes", "--no-dialog")
        self.assertEqual(code, 0)
        self.assertIn("rotated: acme-shop/STRIPE_SECRET_KEY\npending: -\n", out)
        self.assertFalse(self.cloud.argvs(["gcloud"]))
        self.assertTrue(self.shares()[0]["revoked"])
        self.assertEqual(self.orche("secret", "revoke", "dana", "--yes")[0], 2)
        self.assert_no_leak()

    def shares(self):
        return json.loads((self.home / "secrets" / "shares.json").read_text())["shares"]

    def _shared(self):
        self.orche("dev", "add", "dana@example.com", "--project", "acme", "--age-pubkey", PUBKEY)
        self.orche("secret", "share", "--to", "dana", "--project", "acme", "--out", str(self.repo / "x.age"))

    def test_revoke_cancelled_keeps_share_active(self):
        self._shared()
        ui = self.use_ui(FakeUI([(dialog.CANCEL, "")]))
        code, out, _ = self.orche("secret", "revoke", "dana", "--yes")
        self.assertEqual(code, 0)
        self.assertEqual(ui.asked, ["STRIPE_SECRET_KEY"])
        self.assertIn("pending: acme-shop/STRIPE_SECRET_KEY", out)
        self.assertNotIn("revoked", self.shares()[0])
        self.assertEqual(email_items(), [("acme-shop", "STRIPE_SECRET_KEY")])

    def test_revoke_failed_store_keeps_share_active(self):
        self._shared()
        self.cloud.fail_writes = True
        self.use_ui(FakeUI([(dialog.ENTER, "sk_live_" + "fails" + "Store123")]))
        code, out, _ = self.orche("secret", "revoke", "dana", "--yes")
        self.assertEqual(code, 1)
        self.assertIn("pending: acme-shop/STRIPE_SECRET_KEY", out)
        self.assertNotIn("revoked", self.shares()[0])
        self.assertTrue(self.shares()[0]["revoke_requested"])
        self.assert_no_leak()

    def test_revoke_rotation_must_cover_shared_envs(self):
        self.fill_canary(envs="prod")
        self.orche("dev", "add", "dana@example.com", "--project", "acme", "--age-pubkey", PUBKEY)
        self.orche("secret", "share", "--to", "dana", "--env", "prod", "--project", "acme",
                   "--out", str(self.repo / "p.age"))
        code, out, _ = self.orche("secret", "revoke", "dana", "--yes", "--no-dialog")
        self.assertIn("pending: acme-shop/STRIPE_SECRET_KEY", out)

        def filled_rotation(stamp, envs):
            store.requests_dir().joinpath(f"{stamp}-STRIPE_SECRET_KEY.json").write_text(json.dumps({
                "name": "STRIPE_SECRET_KEY", "envs": envs, "why": "rotate", "how_url": "", "project": "acme-shop",
                "requested_by": "user", "at": "2099-01-01T00:00:00+00:00", "status": "filled", "mode": "rotate",
                "filled_at": "2099-01-01T00:00:00+00:00"}))
        filled_rotation("20990101T000000", ["dev"])
        code, out, _ = self.orche("secret", "revoke", "dana", "--yes", "--no-dialog")
        self.assertIn("rotated: -\npending: acme-shop/STRIPE_SECRET_KEY\n", out)
        self.assertNotIn("revoked", self.shares()[0])
        self.assertTrue(self.shares()[0]["revoke_requested"])
        filled_rotation("20990101T000001", ["dev", "prod"])
        code, out, _ = self.orche("secret", "revoke", "dana", "--yes", "--no-dialog")
        self.assertIn("rotated: acme-shop/STRIPE_SECRET_KEY\npending: -\n", out)
        self.assertTrue(self.shares()[0]["revoked"])

    def test_share_out_guard_is_case_insensitive_and_follows_links(self):
        self.orche("dev", "add", "dana@example.com", "--project", "acme", "--age-pubkey", PUBKEY)
        example = self.repo / ".env.example"
        example.write_text("STRIPE_SECRET_KEY=\n")
        (self.repo / "alias.age").symlink_to(example)
        for target in (self.repo / ".ENV.EXAMPLE", self.repo / "alias.age"):
            code, _, err = self.orche("secret", "share", "--to", "dana", "--project", "acme", "--out", str(target))
            self.assertEqual(code, 1, err)
            code, _, err = self.orche("secret", "render", "--out", str(target), "--project", "acme",
                                      "--repo", str(self.repo))
            self.assertEqual(code, 3, err)
        self.assertEqual(example.read_text(), "STRIPE_SECRET_KEY=\n")
        self.assertFalse(self.cloud.argvs(["age"]))


def email_items():
    return [(p, n) for p, n, _, _ in share.received("dana")[1]]


class ClipboardTest(SecretCase):
    """The real OsaUI clipboard path (pbpaste/pbcopy through the fake runner); only `ask` is scripted."""

    def run_with(self, answers, clipboard=""):
        cloud = self.cloud
        answers = list(answers)

        class ScriptedUI(dialog.OsaUI):
            def ask(self, req, error=None):
                return answers.pop(0) if answers else (dialog.CANCEL, "")

        def runner(argv, *, timeout, input):
            if argv == ["pbpaste"]:
                cloud.calls.append((list(argv), input))
                return shell.Result(0, clipboard, "", list(argv))
            return cloud(argv, timeout=timeout, input=input)
        shell.RUNNER = runner
        self.use_ui(ScriptedUI())
        return self.orche("secret", "fill")

    def pbcopy_calls(self):
        return [inp for argv, inp in self.cloud.calls if argv == ["pbcopy"]]

    def test_cleared_after_typed_value_and_after_invalid(self):
        self.orche("secret", "request", "API_TOKEN", "--env", "dev", "--why", "p", "--project", "acme", "--no-dialog")
        code, _, _ = self.run_with([(dialog.ENTER, "bad"), (dialog.ENTER, OTHER)])
        self.assertEqual(code, 0)
        self.assertEqual(self.pbcopy_calls(), ["", ""])
        self.assertEqual(store.list_requests()[0][1]["status"], "filled")
        self.assert_no_leak(OTHER)

    def test_cleared_after_cancel_and_paste(self):
        self.orche("secret", "request", "API_TOKEN", "--env", "dev", "--why", "p", "--project", "acme", "--no-dialog")
        self.run_with([(dialog.CANCEL, "")])
        self.assertEqual(self.pbcopy_calls(), [""])
        self.run_with([(dialog.PASTE, "")], clipboard=OTHER + "\n")
        self.assertEqual(self.pbcopy_calls(), ["", ""])
        self.assertEqual(store.list_requests()[0][1]["status"], "filled")
        self.assert_no_leak(OTHER)


class RedactTranscriptsTest(SecretCase):
    def setUp(self):
        super().setUp()
        self.root = self.home / "projects"
        (self.root / "-srv-acme").mkdir(parents=True)
        self.file = self.root / "-srv-acme" / "s1.jsonl"
        lines = [{"type": "user", "message": {"content": f"token {GH_TOKEN} and {AWS_KEY}"}},
                 {"type": "assistant", "message": {"content": f"again {GH_TOKEN}"}},
                 {"type": "user", "message": {"content": "nothing here"}}]
        self.original = "".join(json.dumps(x) + "\n" for x in lines)
        self.file.write_text(self.original)

    def test_dry_run_counts_and_changes_nothing(self):
        code, out, err = self.orche("--dry-run", "secret", "redact-transcripts", "--root", str(self.root))
        self.assertEqual(code, 0, err)
        self.assertIn("would redact 1/1 files", out)
        self.assertIn("  github_token: 2\n", out)
        self.assertIn("  aws_access_key: 1\n", out)
        self.assertEqual(self.file.read_text(), self.original)
        self.assertEqual(self.cloud.calls, [])
        self.assertFalse((self.home / "archive").exists())
        self.assert_no_leak(GH_TOKEN, AWS_KEY, skip=self.root)

    def test_backup_then_rewrite(self):
        code, out, _ = self.orche("--json", "secret", "redact-transcripts", "--root", str(self.root))
        self.assertEqual(code, 0)
        report = json.loads(out)
        tar = self.cloud.argvs(["tar"])
        self.assertEqual(tar, [["tar", "czf", report["backup"], str(self.root)]])
        self.assertTrue(self.cloud.calls[0][0][0] == "tar")
        self.assertEqual(Path(report["backup"]).stat().st_mode & 0o777, 0o600)
        self.assertEqual(report["counts"], {"github_token": 2, "aws_access_key": 1})
        text = self.file.read_text()
        self.assertEqual(text.count("[REDACTED]"), 3)
        for line in text.splitlines():
            json.loads(line)
        self.assertEqual(text.splitlines()[2], json.dumps({"type": "user", "message": {"content": "nothing here"}}))
        self.assert_no_leak(GH_TOKEN, AWS_KEY)
        code, out, _ = self.orche("secret", "redact-transcripts", "--root", str(self.root))
        self.assertIn("kept indefinitely until you delete it", out)

    def test_line_separators_inside_strings_and_crlf(self):
        token = "ghp_" + "Z9y8X7w6" * 4 + "Ab12Cd"
        record = json.dumps({"a": "x\u2028\u2029\x85 " + token}, ensure_ascii=False, separators=(",", ":"))
        self.assertIn("\u2028", record)  # raw separator, as JSON.stringify writes it
        crlf = self.root / "-srv-acme" / "crlf.jsonl"
        crlf.write_bytes(('{"k":"' + AWS_KEY + '"}\r\n{"k":"plain"}\r\n').encode())
        self.file.write_text(record + "\n")
        code, out, _ = self.orche("--json", "secret", "redact-transcripts", "--root", str(self.root))
        self.assertEqual(code, 0)
        report = json.loads(out)
        self.assertEqual((report["unparsed"], report["errors"]), (0, 0))
        text = self.file.read_text()
        self.assertEqual(text.count("\n"), 1)
        self.assertEqual(json.loads(text), {"a": "x\u2028\u2029\x85 [REDACTED]"})
        self.assertEqual(crlf.read_bytes(), b'{"k":"[REDACTED]"}\r\n{"k":"plain"}\r\n')
        self.assert_no_leak(token, AWS_KEY)

    def test_lone_surrogate_and_per_file_errors(self):
        self.file.write_text('{"a":"\\ud83d tail ' + GH_TOKEN + '"}\n')
        code, out, err = self.orche("--json", "secret", "redact-transcripts", "--root", str(self.root))
        self.assertEqual((code, err), (0, ""))
        text = self.file.read_text()
        self.assertNotIn(GH_TOKEN, text)
        self.assertEqual(json.loads(text), {"a": "\ud83d tail [REDACTED]"})
        # an unexpected failure on one file is counted and logged; the others are still processed
        bad = self.root / "-srv-acme" / "bad.jsonl"
        bad.write_text('{"x":"' + AWS_KEY + '"}\n')
        self.file.write_text('{"y":"' + AWS_KEY + '"}\n')
        real = share.redact_jsonl
        self.addCleanup(setattr, share, "redact_jsonl", real)
        share.redact_jsonl = lambda t: (_ for _ in ()).throw(RuntimeError("boom")) if '"x"' in t else real(t)
        code, out, err = self.orche("--json", "secret", "redact-transcripts", "--root", str(self.root))
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out)["errors"], 1)
        self.assertIn(AWS_KEY, bad.read_text())
        self.assertNotIn(AWS_KEY, self.file.read_text())
        self.assertIn("bad.jsonl: RuntimeError", (self.home / "logs" / "keymaster.log").read_text())

    def test_unreadable_file_counted_as_error(self):
        bad = self.root / "-srv-acme" / "binary.jsonl"
        bad.write_bytes(b'{"x":"\xff\xfe"}\n')
        code, out, _ = self.orche("--json", "secret", "redact-transcripts", "--root", str(self.root))
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out)["errors"], 1)
        self.assertIn("binary.jsonl: unreadable (UnicodeDecodeError)",
                      (self.home / "logs" / "keymaster.log").read_text())

    def test_compact_json_edge_cases(self):
        pem = "-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBgkqhkiG9w0BAQEFAASC"  # no END marker
        records = [{"a": "rpc https://eth-mainnet.alchemy.example/v2/abcdef0123", "b": 1},
                   {"t": "line one\nops@example.com said é ops2@example.com"},
                   {"k": pem},
                   {"after": "still here", "n": [1, 2]},
                   {"x": "x\n" + GH_TOKEN + "\n" + AWS_KEY, "nested": {"list": ["\nsk-" + "a" * 24]}}]
        lines = [json.dumps(r, ensure_ascii=False, separators=(",", ":")) for r in records]
        broken = '{"unterminated": "AKIA' + 'ZZZZZZZZZZZZZZZZ'
        self.file.write_text("\n".join(lines[:3] + [broken] + lines[3:]) + "\n")
        code, out, _ = self.orche("--json", "secret", "redact-transcripts", "--root", str(self.root))
        self.assertEqual(code, 0)
        report = json.loads(out)
        self.assertEqual(report["unparsed"], 1)
        self.assertEqual(report["counts"], {"rpc_url": 1, "email": 2, "private_key_block": 1, "github_token": 1,
                                            "aws_access_key": 1, "openai_key": 1})
        out_lines = self.file.read_text().splitlines()
        self.assertEqual(out_lines[3], broken)
        parsed = [json.loads(ln) for i, ln in enumerate(out_lines) if i != 3]
        self.assertEqual(parsed[0], {"a": "rpc [REDACTED]", "b": 1})
        self.assertEqual(parsed[1]["t"], "line one\n[REDACTED] said é [REDACTED]")
        self.assertEqual(parsed[2], {"k": "[REDACTED]"})
        self.assertEqual(out_lines[4], lines[3])  # untouched record stays byte-identical
        self.assertEqual(parsed[4], {"x": "x\n[REDACTED]\n[REDACTED]", "nested": {"list": ["\n[REDACTED]"]}})
        text = self.file.read_text()
        for secret in (GH_TOKEN, "ops@example.com", "alchemy", "MIIEvQ", "sk-aaaa"):
            self.assertNotIn(secret, text)


if __name__ == "__main__":
    unittest.main()
