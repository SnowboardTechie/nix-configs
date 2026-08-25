#!/usr/bin/env python3
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).with_name("vault-git-backup.sh")


def git(*args, cwd=None, check=True):
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=check,
        text=True,
        capture_output=True,
    )


class VaultGitBackupTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.remote = self.root / "remote.git"
        self.seed = self.root / "seed"
        self.vault = self.root / "vault"

        git("init", "--bare", "--initial-branch=main", str(self.remote))
        git("init", "--initial-branch=main", str(self.seed))
        git("config", "user.name", "Backup Test", cwd=self.seed)
        git("config", "user.email", "backup@example.test", cwd=self.seed)
        (self.seed / "existing.md").write_text("existing\n")
        git("add", "existing.md", cwd=self.seed)
        git("commit", "-m", "initial", cwd=self.seed)
        git("remote", "add", "origin", str(self.remote), cwd=self.seed)
        git("push", "-u", "origin", "main", cwd=self.seed)

        git("clone", str(self.remote), str(self.vault))
        git("config", "user.name", "Backup Test", cwd=self.vault)
        git("config", "user.email", "backup@example.test", cwd=self.vault)

    def tearDown(self):
        self.tempdir.cleanup()

    def run_backup(self):
        env = os.environ | {
            "GIT_AUTHOR_NAME": "Backup Test",
            "GIT_AUTHOR_EMAIL": "backup@example.test",
            "GIT_COMMITTER_NAME": "Backup Test",
            "GIT_COMMITTER_EMAIL": "backup@example.test",
            "GNUPGHOME": str(self.root / "missing-gnupg-home"),
        }
        return subprocess.run(
            ["bash", str(SCRIPT), str(self.vault), "origin", "main"],
            env=env,
            text=True,
            capture_output=True,
        )

    def test_commits_untracked_changes_and_pushes(self):
        (self.vault / "new.md").write_text("new note\n")

        result = self.run_backup()

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("automated vault snapshot", result.stdout)
        self.assertEqual("", git("status", "--porcelain", cwd=self.vault).stdout)
        message = git("log", "-1", "--format=%s", cwd=self.remote).stdout.strip()
        self.assertRegex(message, r"^second-brain: automated vault snapshot \d{4}-\d{2}-\d{2}$")

    def test_clean_repository_exits_successfully_without_commit(self):
        before = git("rev-parse", "HEAD", cwd=self.vault).stdout

        result = self.run_backup()

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("no changes to back up", result.stdout)
        self.assertEqual(before, git("rev-parse", "HEAD", cwd=self.vault).stdout)

    def test_refuses_manually_staged_work(self):
        (self.vault / "staged.md").write_text("manual\n")
        git("add", "staged.md", cwd=self.vault)

        result = self.run_backup()

        self.assertNotEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("manually staged", result.stderr)
        self.assertIn("A  staged.md", git("status", "--short", cwd=self.vault).stdout)

    def test_refuses_when_remote_advanced(self):
        other = self.root / "other"
        git("clone", str(self.remote), str(other))
        git("config", "user.name", "Backup Test", cwd=other)
        git("config", "user.email", "backup@example.test", cwd=other)
        (other / "remote.md").write_text("remote\n")
        git("add", "remote.md", cwd=other)
        git("commit", "-m", "remote change", cwd=other)
        git("push", "origin", "main", cwd=other)
        (self.vault / "local.md").write_text("local\n")

        result = self.run_backup()

        self.assertNotEqual(0, result.returncode)
        self.assertIn("does not match origin/main", result.stderr)
        self.assertIn("?? local.md", git("status", "--short", cwd=self.vault).stdout)

    def test_refuses_in_progress_operation(self):
        merge_head = Path(git("rev-parse", "--git-path", "MERGE_HEAD", cwd=self.vault).stdout.strip())
        if not merge_head.is_absolute():
            merge_head = self.vault / merge_head
        merge_head.write_text("pending\n")

        result = self.run_backup()

        self.assertNotEqual(0, result.returncode)
        self.assertIn("MERGE_HEAD", result.stderr)

    def test_refuses_existing_backup_lock(self):
        lock = self.vault / ".git" / "vault-git-backup.lock"
        lock.mkdir()

        result = self.run_backup()

        self.assertNotEqual(0, result.returncode)
        self.assertIn("stale lock", result.stderr)
        self.assertTrue(lock.is_dir())

    def test_whitespace_failure_does_not_touch_real_index(self):
        (self.vault / "bad.md").write_text("trailing whitespace \n")

        result = self.run_backup()

        self.assertNotEqual(0, result.returncode)
        self.assertIn("real index was not changed", result.stderr)
        self.assertEqual(0, git("diff", "--cached", "--quiet", cwd=self.vault).returncode)
        self.assertIn("?? bad.md", git("status", "--short", cwd=self.vault).stdout)

    def test_push_failure_preserves_local_commit(self):
        hook = self.remote / "hooks" / "pre-receive"
        hook.write_text("#!/bin/sh\nexit 1\n")
        hook.chmod(0o755)
        git("config", "core.hooksPath", "hooks", cwd=self.remote)
        (self.vault / "rejected.md").write_text("preserve me\n")

        result = self.run_backup()

        self.assertNotEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("local commit was preserved", result.stderr)
        ahead = git("rev-list", "--count", "origin/main..HEAD", cwd=self.vault).stdout.strip()
        self.assertEqual("1", ahead)
        self.assertEqual("", git("status", "--porcelain", cwd=self.vault).stdout)


if __name__ == "__main__":
    unittest.main()
