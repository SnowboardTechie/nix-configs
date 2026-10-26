#!/usr/bin/env python3
"""Real-Git regressions and generated-Nix contracts for unattended Hermes updates."""
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import types
import unittest
from functools import cache
from unittest.mock import patch as mock_patch
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
MATRIX_PATCH = REPO / "scripts/hermes-runtime-patches/matrix-send-gateway.patch"
MATRIX_PREVIOUS = REPO / "scripts/hermes-runtime-patches/matrix-send-gateway-170.patch"
MATRIX_MANIFEST = REPO / "scripts/hermes-runtime-patches/matrix-send-gateway.json"


def matrix_base_sources():
    """Minimal pre-images the reviewed #191 patch (and #170's, whose hunks it contains)
    applies to: each modified file's hunk context, separated by filler that later
    releases can change without conflict."""
    sources, path, hunk = {}, None, None
    for line in MATRIX_PATCH.read_bytes().splitlines(keepends=True):
        if line.startswith(b"diff --git "):
            path = hunk = None
        elif line.startswith(b"--- a/"):
            path = line[6:].strip().decode()
            sources[path] = [b"# head filler\n"]
        elif path and line.startswith(b"@@"):
            hunk = sources[path]
            hunk.append(b"# filler\n")
        elif hunk is not None and line[:1] in (b" ", b"-"):
            hunk.append(line[1:])
    return {path: b"".join(lines + [b"# tail filler\n"]) for path, lines in sources.items()}


MATRIX_BASE = matrix_base_sources()
MATRIX_NEW = ("gateway/matrix_send_forward.py", "tests/gateway/test_matrix_send_forward.py")


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
        for path, source in MATRIX_BASE.items():
            (self.root / path).parent.mkdir(parents=True, exist_ok=True)
            (self.root / path).write_bytes(source)
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

    def matrix_patch(self, patch=MATRIX_PATCH):
        self.git("apply", str(patch))

    def matrix_release(self, edit):
        """Commit a release whose matrix-patch files are changed by edit(root)."""
        self.git("checkout", "main")
        edit(self.root)
        self.candidate = self.commit("matrix release")
        self.git("update-ref", "refs/remotes/origin/main", self.candidate)
        self.git("checkout", "--detach", self.base)

    def plan(self, target=None, **kwargs):
        return preflight.prepare_checkout(self.root, target or self.candidate, "stable", **kwargs)

    def supported_git_update(self, plan):
        # The actual git operations Hermes's supported updater performs. No live
        # runtime, credential-bearing home, dependency sync, or service is used.
        # Restore follows hermes_cli/update_cmd_stash.py: `stash apply`; an untracked
        # file the target already holds is kept; any other failure resets the tree
        # and parks the stash while the code update itself still succeeds.
        dirty = bool(self.git("status", "--porcelain"))
        if dirty:
            self.git("stash", "push", "--include-untracked")
        self.git("checkout", "--detach", plan["target"])
        if dirty:
            restore = preflight.git(self.root, "stash", "apply", check=False)
            errors = [line for line in restore.stderr.decode().splitlines()
                      if line.strip() and not line.startswith(("warning:", "hint:"))]
            if self.git("diff", "--name-only", "--diff-filter=U") or any(
                    "already exists, no checkout" not in line
                    and "could not restore untracked files" not in line for line in errors):
                self.git("reset", "--hard")
            else:
                self.git("stash", "drop")
        self.stamp(plan["target"])

    def test_detached_stable_update_preserves_exact_patch(self):
        self.patch()
        plan = self.plan()
        self.assertTrue(plan["preserve_status_patch"])
        self.supported_git_update(plan)
        preflight.verify_checkout(self.root, plan)
        self.assertEqual(preflight.checkout_patches(self.root), {"status"})
        self.assertEqual(self.git("branch", "--show-current"), "")

    def test_clean_detached_release_updates_without_introducing_patch(self):
        plan = self.plan()
        self.supported_git_update(plan)
        preflight.verify_checkout(self.root, plan)
        self.assertEqual(preflight.checkout_patches(self.root), set())

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
        self.assertEqual(preflight.checkout_patches(self.root), {"status"})

    def test_resolver_refuses_case_insensitive_discard_policy(self):
        self.patch()
        self.git("remote", "add", "origin", str(self.root))
        config_path = self.root.parent / "config.json"
        # Stub only external Hermes release/config APIs; exercise this helper's
        # full resolver and real Git admission without network or bootstrapping.
        package = types.ModuleType("hermes_cli")
        package.__path__ = []
        modules = {
            "yaml": types.SimpleNamespace(safe_load=json.loads),
            "hermes_cli": package,
            "hermes_cli.source_releases": types.SimpleNamespace(
                resolve_source_target=lambda *args, **kwargs: types.SimpleNamespace(commit=self.candidate)),
            "hermes_cli.update_channel": types.SimpleNamespace(
                channel_record=lambda *args: {},
                resolve_update_channel=lambda *args: "stable",
                rides_default_channel=lambda *args: True),
        }
        for policy in ("discard", "DISCARD", "DisCard", "stash"):
            with self.subTest(policy=policy):
                config_path.write_text(json.dumps({"updates": {"non_interactive_local_changes": policy}}))
                with mock_patch.dict(sys.modules, modules), mock_patch.object(sys, "path", sys.path.copy()):
                    if policy.lower() == "discard":
                        with self.assertRaisesRegex(ValueError, "discard"):
                            preflight.resolve_plan(self.root, config_path)
                    else:
                        self.assertTrue(preflight.resolve_plan(self.root, config_path)["preserve_status_patch"])
                self.assertEqual(self.git("rev-parse", "HEAD"), self.base)
                self.assertEqual(preflight.checkout_patches(self.root), {"status"})

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


    # --- #170 matrix-send-through-gateway patch -------------------------------

    def test_matrix_patch_alone_is_preserved(self):
        self.matrix_patch()
        plan = self.plan()
        self.assertTrue(plan["preserve_matrix_patch"])
        self.assertFalse(plan["preserve_status_patch"])
        self.supported_git_update(plan)
        preflight.verify_checkout(self.root, plan)
        self.assertEqual(preflight.checkout_patches(self.root), {"matrix"})

    def test_both_patches_are_preserved(self):
        self.patch()
        self.matrix_patch()
        plan = self.plan()
        self.assertTrue(plan["preserve_status_patch"])
        self.assertTrue(plan["preserve_matrix_patch"])
        self.supported_git_update(plan)
        preflight.verify_checkout(self.root, plan)
        self.assertEqual(preflight.checkout_patches(self.root), {"status", "matrix"})

    def test_matrix_patch_survives_unrelated_upstream_edits_in_its_files(self):
        def edit(root):
            for path in MATRIX_BASE:
                source = (root / path).read_bytes()
                (root / path).write_bytes(source.replace(b"# tail filler", b"# upstream moved on"))
        self.matrix_release(edit)
        self.matrix_patch()
        plan = self.plan()
        self.supported_git_update(plan)
        preflight.verify_checkout(self.root, plan)
        self.assertIn(b"# upstream moved on", (self.root / "gateway/run_bootstrap.py").read_bytes())
        self.assertEqual(preflight.checkout_patches(self.root), {"matrix"})

    def test_refuses_matrix_patch_altered_by_one_byte(self):
        for path in ("gateway/matrix_send_forward.py", "tools/send_message_senders.py"):
            with self.subTest(path=path):
                self.matrix_patch()
                target = self.root / path
                source = target.read_bytes()
                target.write_bytes(source[:-2] + bytes([source[-2] ^ 1]) + source[-1:])
                with self.assertRaisesRegex(ValueError, "matrix-send"):
                    self.plan()
                self.git("apply", "-R", "--exclude", path, str(MATRIX_PATCH))
                if path in MATRIX_NEW:
                    target.unlink()
                else:
                    self.git("restore", path)
                self.assertEqual(self.git("status", "--porcelain"), "")

    def test_refuses_partial_matrix_patch(self):
        self.matrix_patch()
        self.git("restore", "tools/send_message_senders.py")
        with self.assertRaisesRegex(ValueError, "matrix-send"):
            self.plan()

    def test_refuses_executable_matrix_module(self):
        self.matrix_patch()
        (self.root / MATRIX_NEW[0]).chmod(0o755)
        with self.assertRaisesRegex(ValueError, "mode"):
            self.plan()

    def test_refuses_extra_change_beside_matrix_patch(self):
        self.matrix_patch()
        (self.root / "other.txt").write_text("user work\n")
        with self.assertRaisesRegex(ValueError, "unrelated"):
            self.plan()
        self.git("restore", "other.txt")
        (self.root / "gateway/extra.py").write_text("user work\n")
        with self.assertRaisesRegex(ValueError, "untracked"):
            self.plan()

    def test_refuses_target_where_matrix_patch_no_longer_applies(self):
        def edit(root):
            path = root / "gateway/run_bootstrap.py"
            path.write_bytes(path.read_bytes().replace(b"ticket_store", b"tickets"))
        self.matrix_release(edit)
        self.patch()
        self.matrix_patch()
        with self.assertRaisesRegex(ValueError, "matrix-send patch cannot be cleanly preserved"):
            self.plan()
        self.assertEqual(self.git("rev-parse", "HEAD"), self.base)
        self.assertEqual(preflight.checkout_patches(self.root), {"status", "matrix"})

    def test_refuses_target_that_adds_its_own_matrix_module(self):
        def edit(root):
            (root / MATRIX_NEW[0]).write_text("# upstream's own module\n")
        self.matrix_release(edit)
        self.matrix_patch()
        with self.assertRaisesRegex(ValueError, "matrix-send patch cannot be cleanly preserved"):
            self.plan()

    def test_target_containing_matrix_change_absorbs_patch(self):
        self.matrix_release(lambda root: preflight.git(root, "apply", str(MATRIX_PATCH)))
        self.matrix_patch()
        plan = self.plan()
        self.assertTrue(plan["preserve_matrix_patch"])
        self.supported_git_update(plan)
        preflight.verify_checkout(self.root, plan)
        self.assertEqual(self.git("status", "--porcelain"), "")

    def test_refuses_matrix_patch_discard_policy(self):
        self.matrix_patch()
        with self.assertRaisesRegex(ValueError, "discard"):
            self.plan(discard_changes=True)

    def test_verification_rejects_lost_matrix_patch(self):
        self.matrix_patch()
        plan = self.plan()
        self.git("apply", "-R", str(MATRIX_PATCH))
        self.git("checkout", "--detach", self.candidate)
        self.stamp(self.candidate)
        with self.assertRaisesRegex(ValueError, "did not retain the required matrix-send patch"):
            preflight.verify_checkout(self.root, plan)

    def test_verification_rejects_unexpected_matrix_patch(self):
        plan = self.plan()
        self.supported_git_update(plan)
        self.matrix_patch()
        with self.assertRaisesRegex(ValueError, "unexpected matrix-send patch"):
            preflight.verify_checkout(self.root, plan)


    # --- #191: the superseded #170 patch, and the move from it ------------------

    def test_previous_matrix_patch_alone_is_preserved(self):
        self.matrix_patch(MATRIX_PREVIOUS)
        plan = self.plan()
        self.assertTrue(plan["preserve_matrix_patch"])
        self.assertEqual(plan["matrix_patch_sha256"], preflight.MATRIX_PREVIOUS_PATCH_SHA256)
        self.supported_git_update(plan)
        preflight.verify_checkout(self.root, plan)
        self.assertEqual(preflight.checkout_state(self.root),
                         ({"matrix"}, preflight.MATRIX_PREVIOUS_PATCH_SHA256))
        self.assertEqual(self.git("diff", "--name-only", "--", "hermes_cli/send_cmd.py"), "")

    def test_current_matrix_patch_is_recorded_by_its_hash(self):
        self.patch()
        self.matrix_patch()
        plan = self.plan()
        self.assertEqual(plan["matrix_patch_sha256"], preflight.MATRIX_PATCH_SHA256)
        self.supported_git_update(plan)
        preflight.verify_checkout(self.root, plan)
        self.assertEqual(preflight.checkout_state(self.root), ({"status", "matrix"}, preflight.MATRIX_PATCH_SHA256))

    def test_move_from_previous_to_current_patch_is_admitted_at_every_resting_state(self):
        self.patch()
        self.matrix_patch(MATRIX_PREVIOUS)
        self.assertEqual(self.plan()["matrix_patch_sha256"], preflight.MATRIX_PREVIOUS_PATCH_SHA256)
        self.git("apply", "-R", str(MATRIX_PREVIOUS))
        self.assertEqual(preflight.checkout_state(self.root), ({"status"}, None))
        self.matrix_patch()
        plan = self.plan()
        self.assertEqual(plan["matrix_patch_sha256"], preflight.MATRIX_PATCH_SHA256)
        self.supported_git_update(plan)
        preflight.verify_checkout(self.root, plan)

    def test_refuses_previous_module_beside_the_rest_of_the_current_patch(self):
        self.matrix_patch()
        module = "gateway/matrix_send_forward.py"
        self.git("apply", "-R", "--include", module, str(MATRIX_PATCH))
        self.git("apply", "--include", module, str(MATRIX_PREVIOUS))
        with self.assertRaisesRegex(ValueError, "matrix-send"):
            self.plan()

    def test_refuses_current_patch_without_its_send_cmd_change(self):
        self.matrix_patch()
        self.git("restore", "hermes_cli/send_cmd.py")
        with self.assertRaisesRegex(ValueError, "matrix-send"):
            self.plan()

    def test_refuses_previous_patch_plus_send_cmd_change(self):
        self.matrix_patch(MATRIX_PREVIOUS)
        self.git("apply", "--include", "hermes_cli/send_cmd.py", str(MATRIX_PATCH))
        with self.assertRaisesRegex(ValueError, "matrix-send"):
            self.plan()

    def test_verification_rejects_a_switched_matrix_patch(self):
        self.matrix_patch(MATRIX_PREVIOUS)
        plan = self.plan()
        self.supported_git_update(plan)
        self.git("apply", "-R", str(MATRIX_PREVIOUS))
        self.matrix_patch()
        with self.assertRaisesRegex(ValueError, "changed which matrix-send patch"):
            preflight.verify_checkout(self.root, plan)

    def test_refuses_target_where_previous_patch_no_longer_applies(self):
        def edit(root):
            path = root / "gateway/run_bootstrap.py"
            path.write_bytes(path.read_bytes().replace(b"ticket_store", b"tickets"))
        self.matrix_release(edit)
        self.matrix_patch(MATRIX_PREVIOUS)
        with self.assertRaisesRegex(ValueError, "matrix-send patch cannot be cleanly preserved"):
            self.plan()

    def test_previous_patch_is_refused_once_its_pin_is_withdrawn(self):
        self.matrix_patch(MATRIX_PREVIOUS)
        with mock_patch.object(preflight, "MATRIX_PATCH_FILES", (MATRIX_PATCH,)):
            with self.assertRaisesRegex(ValueError, "matrix-send"):
                self.plan()


class MatrixPatchPinTests(unittest.TestCase):
    def test_committed_patch_is_the_pinned_review(self):
        manifest = json.loads(MATRIX_MANIFEST.read_text())
        self.assertEqual(manifest["patch_sha256"], preflight.MATRIX_PATCH_SHA256)
        self.assertEqual(preflight.load_matrix_patch(MATRIX_PATCH), MATRIX_PATCH.read_bytes())
        self.assertEqual(sorted(f["path"] for f in manifest["files"]),
                         sorted((*preflight.MATRIX_MODIFIED, *preflight.MATRIX_NEW)))
        previous = manifest["supersedes"]
        self.assertEqual(previous["patch_sha256"], preflight.MATRIX_PREVIOUS_PATCH_SHA256)
        self.assertEqual(preflight.load_matrix_patch(MATRIX_PREVIOUS), MATRIX_PREVIOUS.read_bytes())
        self.assertEqual(set(preflight.load_matrix_patches()),
                         {preflight.MATRIX_PATCH_SHA256, preflight.MATRIX_PREVIOUS_PATCH_SHA256})

    def test_current_patch_is_required(self):
        with mock_patch.object(preflight, "MATRIX_PATCH_FILES", (MATRIX_PREVIOUS,)):
            with self.assertRaisesRegex(ValueError, "current reviewed"):
                preflight.load_matrix_patches()

    def test_refuses_a_patch_file_that_is_not_the_review(self):
        with tempfile.TemporaryDirectory() as directory:
            copy = Path(directory) / "matrix-send-gateway.patch"
            copy.write_bytes(MATRIX_PATCH.read_bytes() + b"\n")
            with self.assertRaisesRegex(ValueError, "reviewed"):
                preflight.load_matrix_patch(copy)


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

    def test_backup_gate_resolves_adjacent_modules_and_requires_snapshot(self):
        script = generated_updater()
        [backup] = [body for body in re.findall(r"<<'PY'\n(.*?)\nPY", script, re.S)
                    if "from hermes_cli.backup import create_quick_snapshot" in body]
        with tempfile.TemporaryDirectory(prefix="hermes-backup-test-",
                                         dir=Path.home() / ".hermes/cache/scratch") as directory:
            root = Path(directory)
            (root / "hermes_cli").mkdir()
            (root / "hermes_cli/__init__.py").write_text("")
            (root / "hermes_cli/backup.py").write_text(
                "from snapshot_support import snapshot\n"
                "def create_quick_snapshot(label):\n    return snapshot(label)\n")
            for value, expected_code in (("test-snapshot", 0), (None, 1)):
                (root / "snapshot_support.py").write_text(
                    f"def snapshot(label):\n    return {value!r}\n")
                result = subprocess.run([sys.executable, "-I", "-B", "-", "test-label", str(root)],
                                        input=backup, text=True, capture_output=True)
                self.assertEqual(result.returncode, expected_code, result.stderr)
                if value:
                    self.assertIn("test-snapshot", result.stdout)
                else:
                    self.assertIn("produced no backup", result.stderr)

    def test_generated_wrapper_preserves_patch_and_checks_channel_target(self):
        script = generated_updater()
        self.assertIn("hermes-update-preflight.py", script)
        self.assertIn("--plan-json", script)
        patches = [Path(path.strip("'")).read_bytes() for path in re.findall(r"--matrix-patch (\S+)", script)]
        self.assertEqual(patches, [MATRIX_PATCH.read_bytes(), MATRIX_PREVIOUS.read_bytes()] * 2,
                         "plan and verify must both pin both reviewed patches")
        self.assertNotIn("update --yes --no-backup --keep-stash", script)
        self.assertNotIn("HEAD...origin/main", script)
        self.assertLess(script.index("--plan-json"), script.index("Hermes backend healthy"))
        subprocess.run(["bash", "-n"], input=script, text=True, check=True)


if __name__ == "__main__":
    unittest.main()
