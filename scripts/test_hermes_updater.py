#!/usr/bin/env python3
"""Real-Git regressions and generated-Nix contracts for unattended Hermes updates."""
import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
from functools import cache
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
STUDIO = "darwinConfigurations.studio.config"
SPEC = importlib.util.spec_from_file_location("preflight", REPO / "scripts/hermes-update-preflight.py")
assert SPEC is not None and SPEC.loader is not None
preflight = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(preflight)
STATUS = preflight.STATUS_PATH
ORIGINAL = '''class CLI:
    def _show_session_status(self):
        if is_approval_bypass_active_for_session(getattr(self, "session_key", "") or ""):
            print("YOLO")

    def unrelated(self):
        return "old release"

    def _toggle_yolo(self):
        session_key = self.session_id or "default"
        return session_key
'''.encode()


@cache
def eval_json(option):
    result = subprocess.run(
        ["nix", "eval", "--json", f".#{STUDIO}.{option}"], cwd=REPO,
        check=True, capture_output=True, text=True,
    )
    return json.loads(result.stdout)


@cache
def generated_updater():
    subprocess.run(["nix", "build", "--no-link", ".#darwinConfigurations.studio.system"],
                   cwd=REPO, check=True, capture_output=True)
    [script] = eval_json("launchd.user.agents.hermes-updater.serviceConfig.ProgramArguments")
    return Path(script).read_text()


class CheckoutTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(
            prefix="hermes-updater-test-", dir=Path.home() / ".hermes/cache/scratch")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "hermes-agent"
        self.root.mkdir()
        self.git("init", "-b", "main")
        self.git("config", "user.name", "Updater Test")
        self.git("config", "user.email", "updater@example.invalid")
        self.git("config", "commit.gpgsign", "false")
        (self.root / ".git/info/exclude").write_text("install-stamp.json\n")
        (self.root / STATUS).parent.mkdir()
        (self.root / STATUS).write_bytes(ORIGINAL)
        (self.root / "other.txt").write_text("unchanged\n")
        self.base = self.commit("base")
        self.candidate = self.release(ORIGINAL.replace(b"old release", b"new release"))
        self.git("checkout", "--detach", self.base)
        self.stamp(self.base)

    def git(self, *args):
        return preflight.git(self.root, *args).stdout.decode().strip()

    def commit(self, message):
        self.git("add", ".")
        self.git("commit", "-m", message)
        return self.git("rev-parse", "HEAD")

    def release(self, source):
        (self.root / STATUS).write_bytes(source)
        commit = self.commit("candidate")
        self.git("update-ref", "refs/remotes/origin/main", commit)
        return commit

    def stamp(self, commit):
        (self.root / "install-stamp.json").write_text(json.dumps({"commit": commit, "baseVersion": "test"}))

    def patch(self):
        source = (self.root / STATUS).read_bytes()
        (self.root / STATUS).write_bytes(preflight.corrected_status_source(source))

    def plan(self, target=None, **kwargs):
        return preflight.prepare_checkout(self.root, target or self.candidate, "stable", **kwargs)

    def supported_git_update(self, plan):
        # The actual git operations Hermes's supported updater performs. No live
        # runtime, credential-bearing home, dependency sync, or service is used.
        dirty = bool(self.git("status", "--porcelain"))
        if dirty:
            self.git("stash", "push", "--include-untracked")
        self.git("checkout", "--detach", plan["target"])
        if dirty:
            self.git("stash", "pop")
        self.stamp(plan["target"])

    def test_detached_stable_update_preserves_exact_patch(self):
        self.patch()
        plan = self.plan()
        self.assertTrue(plan["preserve_status_patch"])
        self.supported_git_update(plan)
        preflight.verify_checkout(self.root, plan)
        self.assertTrue(preflight.checkout_patch(self.root))
        self.assertEqual(self.git("branch", "--show-current"), "")

    def test_clean_detached_release_updates_without_introducing_patch(self):
        plan = self.plan()
        self.supported_git_update(plan)
        preflight.verify_checkout(self.root, plan)
        self.assertFalse(preflight.checkout_patch(self.root))

    def test_main_channel_uses_its_resolved_commit(self):
        self.git("checkout", "-B", "main", self.base)
        plan = preflight.prepare_checkout(self.root, self.candidate, "main")
        self.assertEqual(plan["target"], self.candidate)

    def test_already_current_patch_is_a_noop(self):
        self.git("checkout", "--detach", self.candidate)
        self.patch()
        plan = self.plan()
        self.assertEqual(plan["head"], plan["target"])
        self.assertTrue(plan["preserve_status_patch"])

    def test_upstream_fix_absorbs_patch_without_dirtying_new_release(self):
        self.git("checkout", "main")
        self.candidate = self.release(preflight.corrected_status_source(
            ORIGINAL.replace(b"old release", b"upstream fixed")))
        self.git("checkout", "--detach", self.base)
        self.patch()
        plan = self.plan()
        self.supported_git_update(plan)
        preflight.verify_checkout(self.root, plan)
        self.assertEqual(self.git("status", "--porcelain"), "")

    def test_refuses_other_tracked_drift(self):
        self.patch()
        (self.root / "other.txt").write_text("user work\n")
        with self.assertRaisesRegex(ValueError, "unrelated"):
            self.plan()

    def test_refuses_extra_changes_in_patch_file(self):
        self.patch()
        path = self.root / STATUS
        path.write_bytes(path.read_bytes() + b"# user work\n")
        with self.assertRaisesRegex(ValueError, "exactly"):
            self.plan()

    def test_refuses_untracked_files(self):
        (self.root / "untracked.txt").write_text("user work\n")
        with self.assertRaisesRegex(ValueError, "untracked"):
            self.plan()

    def test_refuses_staged_patch(self):
        self.patch()
        self.git("add", STATUS)
        with self.assertRaisesRegex(ValueError, "staged"):
            self.plan()

    def test_refuses_feature_branch(self):
        self.git("checkout", "-b", "user-feature")
        with self.assertRaisesRegex(ValueError, "branch"):
            self.plan()

    def test_refuses_local_commits(self):
        (self.root / "other.txt").write_text("local commit\n")
        self.commit("user work")
        with self.assertRaisesRegex(ValueError, "local commits"):
            self.plan()

    def test_refuses_automatic_downgrade(self):
        self.git("checkout", "--detach", self.candidate)
        with self.assertRaisesRegex(ValueError, "downgrade"):
            self.plan(self.base)

    def test_refuses_patch_discard_policy_before_mutation(self):
        self.patch()
        with self.assertRaisesRegex(ValueError, "discard"):
            self.plan(discard_changes=True)
        self.assertEqual(self.git("rev-parse", "HEAD"), self.base)
        self.assertTrue(preflight.checkout_patch(self.root))

    def test_refuses_changed_target_patch_shape_before_mutation(self):
        self.git("checkout", "main")
        self.candidate = self.release(ORIGINAL.replace(b"session_key\",", b"different_key\","))
        self.git("checkout", "--detach", self.base)
        self.patch()
        with self.assertRaisesRegex(ValueError, "identity"):
            self.plan()
        self.assertEqual(self.git("rev-parse", "HEAD"), self.base)

    def test_verification_rejects_lost_patch(self):
        self.patch()
        plan = self.plan()
        self.git("restore", STATUS)
        self.git("checkout", "--detach", self.candidate)
        self.stamp(self.candidate)
        with self.assertRaisesRegex(ValueError, "did not retain"):
            preflight.verify_checkout(self.root, plan)

    def test_verification_rejects_wrong_target(self):
        plan = self.plan()
        with self.assertRaisesRegex(ValueError, "expected"):
            preflight.verify_checkout(self.root, plan)

    def test_verification_rejects_stale_install_stamp(self):
        plan = self.plan()
        self.supported_git_update(plan)
        self.stamp(self.base)
        with self.assertRaisesRegex(ValueError, "install stamp"):
            preflight.verify_checkout(self.root, plan)


class NixUpdaterTests(unittest.TestCase):
    def test_primary_nightly_updater_is_enabled(self):
        config = eval_json("launchd.user.agents.hermes-updater.serviceConfig")
        self.assertEqual(config["Label"], "ai.hermes.update")
        calendar = {key: value for key, value in config["StartCalendarInterval"].items()
                    if value is not None}
        self.assertEqual(calendar, {"Hour": 4, "Minute": 0})

    def test_primary_services_remain_enabled(self):
        self.assertTrue(eval_json("services.hermes.gateway.enable"))
        self.assertTrue(eval_json("services.hermes.dashboard.enable"))

    def test_headless_weekly_schedule_is_unchanged(self):
        config = eval_json("launchd.daemons.hermes-traci-updater.serviceConfig")
        calendar = {key: value for key, value in config["StartCalendarInterval"].items()
                    if value is not None}
        self.assertEqual(calendar, {"Weekday": 0, "Hour": 5, "Minute": 0})

    def test_generated_wrapper_preserves_patch_and_checks_channel_target(self):
        script = generated_updater()
        self.assertIn("hermes-update-preflight.py", script)
        self.assertIn("--plan-json", script)
        self.assertNotIn("update --yes --no-backup --keep-stash", script)
        self.assertNotIn("HEAD...origin/main", script)
        self.assertLess(script.index("--plan-json"), script.index("Hermes backend healthy"))
        subprocess.run(["bash", "-n"], input=script, text=True, check=True)


if __name__ == "__main__":
    unittest.main()
