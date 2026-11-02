#!/usr/bin/env python3
"""Tests for scripts/hermes-nas-backup.sh against a fake Hermes home and a fake share.

Nothing here touches a real mount: the "share" is a temporary directory, the
mount table is a stand-in command, and notifications go to a recording stub.
Needs /usr/bin/sqlite3 and zstd on PATH (or ZSTD set).
"""
import json
import os
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile
import time
import unittest
from pathlib import Path

# BACKUP_TEST_SCRIPT/BACKUP_TEST_BASH let the suite run the Nix-built script under Nix's bash.
SCRIPT = Path(os.environ.get("BACKUP_TEST_SCRIPT") or Path(__file__).with_name("hermes-nas-backup.sh"))
BASH = os.environ.get("BACKUP_TEST_BASH", "bash")
ZSTD = os.environ.get("ZSTD") or shutil.which("zstd")
STAMP_RE = r"^\d{8}T\d{6}Z$"

CONFIG = """\
model:
  default: claude
  max_tokens: 4096
compression:
  threshold_tokens: 0
platforms:
  webhook:
    extra:
      routes:
        studio-alerts:
          secret: hunter2-route-secret
          deliver_only: true
providers:
  custom:
    api_key: fixture-api-key-value
    "auth_token": 'quoted-token-value'
    password: |
      multi-line-password
      second-line
    after_block: kept
terminal:
  env:
    - GITHUB_TOKEN=ghp_envstyle
    - PLAIN=value
"""
SECRETS = [
    "hunter2-route-secret",
    "fixture-api-key-value",
    "quoted-token-value",
    "multi-line-password",
    "second-line",
    "ghp_envstyle",
]


def run(cmd, **kw):
    return subprocess.run(cmd, text=True, capture_output=True, **kw)


class HermesNasBackupTests(unittest.TestCase):
    def setUp(self):
        if not ZSTD:
            self.skipTest("zstd not available")
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.root = root
        self.home = root / "hermes"
        self.share = root / "share"
        self.dest = self.share / "Backups" / "hermes"
        self.staging = root / "staging"
        self.notes = root / "notifications.txt"
        self.home.mkdir()
        self.share.mkdir()

        self._make_db(self.home / "state.db", sessions=3, messages=7)
        (self.home / "memories").mkdir()
        (self.home / "memories" / "MEMORY.md").write_text("memory\n")
        (self.home / "memories" / "USER.md").write_text("user\n")
        (self.home / "memories" / "MEMORY.md.lock").write_text("")
        (self.home / "cron").mkdir()
        (self.home / "cron" / "jobs.json").write_text('{"jobs": []}\n')
        (self.home / "skills" / "mine").mkdir(parents=True)
        (self.home / "skills" / "mine" / "SKILL.md").write_text("skill\n")
        (self.home / "skills" / ".hub").mkdir()
        (self.home / "skills" / ".hub" / "index.json").write_text("{}")
        os.symlink("/nonexistent/dotfiles/skill", self.home / "skills" / "linked")
        (self.home / "config.yaml").write_text(CONFIG)
        (self.home / ".env").write_text("SECRET_ENV=do-not-copy\n")
        (self.home / "auth.json").write_text('{"token": "do-not-copy"}')

        self.mount_cmd = root / "fake-mount"
        self.set_mounted(True)
        self.notify_cmd = root / "fake-hermes"
        self.notify_cmd.write_text(
            "#!/bin/sh\nprintf '%s\\n' \"$*\" >> " + str(self.notes) + "\n"
        )
        self.notify_cmd.chmod(0o755)

    def tearDown(self):
        self.tmp.cleanup()

    # --- helpers -----------------------------------------------------------
    def _make_db(self, path, sessions, messages):
        con = sqlite3.connect(path)
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY)")
        con.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, content TEXT)")
        con.executemany("INSERT INTO sessions VALUES (?)", [(f"s{i}",) for i in range(sessions)])
        con.executemany(
            "INSERT INTO messages (session_id, content) VALUES (?, ?)",
            [("s0", "x" * 100) for _ in range(messages)],
        )
        con.commit()
        con.close()

    def set_mounted(self, mounted, fstype="smbfs"):
        line = f"//bryan@nas/Bryan on {self.share} ({fstype}, nodev, nosuid, mounted by bryan)" if mounted else ""
        self.mount_cmd.write_text(
            "#!/bin/sh\necho '/dev/disk3s5 on /System/Volumes/Data (apfs, local)'\n"
            + (f"echo '{line}'\n" if line else "")
        )
        self.mount_cmd.chmod(0o755)

    def env(self, **extra):
        env = {
            "PATH": os.environ["PATH"],
            "HOME": str(self.root),
            "HERMES_HOME_DIR": str(self.home),
            "MOUNT_POINT": str(self.share),
            "DEST_DIR": str(self.dest),
            "STAGING_DIR": str(self.staging),
            "KEEP": "7",
            "ZSTD": ZSTD,
            "MOUNT_CMD": str(self.mount_cmd),
            "MOUNT_DEVICE_CHECK": "0",
            "NOTIFY_CMD": str(self.notify_cmd),
            "NOTIFY_TARGET": "matrix",
            "NOTIFY_MENTION": "@bryan:example.test",
        }
        env.update(extra)
        return env

    def backup(self, **extra):
        return run([BASH, str(SCRIPT)], env=self.env(**extra))

    def published(self):
        if not self.dest.exists():
            return []
        import re
        return sorted(p.name for p in self.dest.iterdir() if re.match(STAMP_RE, p.name))

    def notifications(self):
        return self.notes.read_text().splitlines() if self.notes.exists() else []

    def status(self):
        return json.loads((self.staging / "last-run.json").read_text())

    # --- tests -------------------------------------------------------------
    def test_publishes_a_checked_restorable_backup(self):
        result = self.backup()
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        [stamp] = self.published()
        out = self.dest / stamp
        self.assertEqual(sorted(p.name for p in out.iterdir()),
                         ["MANIFEST.json", "files.tar.zst", "state.db.zst"])
        manifest = json.loads((out / "MANIFEST.json").read_text())
        self.assertEqual(manifest["state_db"]["sessions"], 3)
        self.assertEqual(manifest["state_db"]["messages"], 7)
        self.assertEqual(manifest["state_db"]["quick_check"], "ok")

        restore = self.root / "restore"
        restore.mkdir()
        self.assertEqual(run([ZSTD, "-q", "-d", str(out / "state.db.zst"), "-o",
                              str(restore / "state.db")]).returncode, 0)
        con = sqlite3.connect(restore / "state.db")
        self.assertEqual(con.execute("select count(*) from messages").fetchone()[0], 7)
        self.assertEqual(con.execute("pragma integrity_check").fetchone()[0], "ok")
        con.close()

        self.assertEqual(run([ZSTD, "-q", "-d", str(out / "files.tar.zst"), "-o",
                              str(restore / "files.tar")]).returncode, 0)
        with tarfile.open(restore / "files.tar") as tar:
            names = tar.getnames()
            self.assertIn("memories/MEMORY.md", names)
            self.assertIn("memories/USER.md", names)
            self.assertIn("cron/jobs.json", names)
            self.assertIn("skills/mine/SKILL.md", names)
            self.assertIn("config.yaml.redacted", names)
            self.assertTrue(tar.getmember("skills/linked").issym())
            for banned in (".env", "auth.json", "config.yaml", "memories/MEMORY.md.lock"):
                self.assertNotIn(banned, names)
            self.assertFalse([n for n in names if n.startswith("skills/.hub")])
            member = tar.extractfile("config.yaml.redacted")
            assert member is not None
            config = member.read().decode()
        for secret in SECRETS:
            self.assertNotIn(secret, config)
        self.assertIn("max_tokens: 4096", config)
        self.assertIn("threshold_tokens: 0", config)
        self.assertIn("after_block: kept", config)
        self.assertIn("PLAIN=value", config)
        self.assertIn('secret: "<redacted>"', config)
        self.assertIn("GITHUB_TOKEN=<redacted>", config)

        self.assertEqual(self.notifications(), [])
        self.assertEqual(self.status()["status"], "ok")
        self.assertFalse(list(self.staging.glob("work-*")), "local work dir left behind")
        self.assertFalse((self.staging / "lock").exists())

    def test_copy_is_consistent_while_a_writer_holds_the_database(self):
        con = sqlite3.connect(self.home / "state.db")
        con.execute("BEGIN IMMEDIATE")
        con.execute("INSERT INTO messages (session_id, content) VALUES ('s1', 'uncommitted')")
        try:
            result = self.backup()
        finally:
            con.rollback()
            con.close()
        self.assertEqual(result.returncode, 0, result.stderr)
        [stamp] = self.published()
        manifest = json.loads((self.dest / stamp / "MANIFEST.json").read_text())
        self.assertEqual(manifest["state_db"]["messages"], 7)

    def test_unmounted_share_writes_nothing_and_alerts(self):
        self.set_mounted(False)
        result = self.backup()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.dest.exists())
        [note] = self.notifications()
        self.assertIn("--to matrix --quiet @bryan:example.test Hermes NAS backup FAILED", note)
        self.assertIn("not mounted", result.stderr)
        self.assertEqual(self.status()["status"], "failed")

    def test_non_smb_mount_is_refused(self):
        self.set_mounted(True, fstype="apfs")
        result = self.backup()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.dest.exists())

    def test_device_check_refuses_a_plain_directory(self):
        result = self.backup(MOUNT_DEVICE_CHECK="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not a live mount", result.stderr)
        self.assertFalse(self.dest.exists())

    def test_destination_outside_the_mount_is_refused(self):
        for dest in (str(self.root / "elsewhere"), str(self.share), str(self.share) + "/../x"):
            result = self.backup(DEST_DIR=dest)
            self.assertNotEqual(result.returncode, 0, dest)
        self.assertFalse((self.root / "elsewhere").exists())

    def test_corrupt_database_publishes_nothing(self):
        (self.home / "state.db").write_bytes(b"SQLite format 3\x00" + b"\xff" * 8192)
        for side in ("state.db-wal", "state.db-shm"):
            (self.home / side).unlink(missing_ok=True)
        result = self.backup()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.published(), [])
        self.assertFalse([p for p in self.dest.iterdir()] if self.dest.exists() else [])
        self.assertEqual(len(self.notifications()), 1)

    def test_retention_keeps_newest_and_touches_nothing_else(self):
        self.dest.mkdir(parents=True)
        old = [f"2026010{d}T030000Z" for d in range(1, 10)]  # 9 old backups
        for name in old:
            (self.dest / name).mkdir()
            (self.dest / name / "MANIFEST.json").write_text("{}")
        (self.dest / "keep-me").mkdir()
        (self.dest / "notes.txt").write_text("mine")
        (self.dest / ".partial-20260101T000000Z").mkdir()
        (self.dest / ".partial-whatever").mkdir()
        result = self.backup(KEEP="3")
        self.assertEqual(result.returncode, 0, result.stderr)
        kept = self.published()
        self.assertEqual(len(kept), 3)
        self.assertEqual(kept[:2], old[-2:])
        self.assertTrue((self.dest / "keep-me").is_dir())
        self.assertTrue((self.dest / "notes.txt").exists())
        self.assertTrue((self.dest / ".partial-whatever").is_dir())
        self.assertFalse((self.dest / ".partial-20260101T000000Z").exists())

    def test_retention_never_runs_after_a_failure(self):
        self.dest.mkdir(parents=True)
        for d in range(1, 10):
            (self.dest / f"2026010{d}T030000Z").mkdir()
        self.set_mounted(False)
        self.backup(KEEP="1")
        self.assertEqual(len(self.published()), 9)

    def test_recovery_is_announced_once(self):
        self.set_mounted(False)
        self.backup()
        self.set_mounted(True)
        self.assertEqual(self.backup().returncode, 0)
        notes = self.notifications()
        self.assertEqual(len(notes), 2)
        self.assertIn("recovered", notes[1])
        time.sleep(1.1)  # next stamp
        self.assertEqual(self.backup().returncode, 0)
        self.assertEqual(len(self.notifications()), 2)

    def test_skips_while_another_run_holds_the_lock(self):
        (self.staging / "lock").mkdir(parents=True)
        (self.staging / "lock" / "pid").write_text(str(os.getpid()))
        result = self.backup()
        self.assertEqual(result.returncode, 0)
        self.assertIn("skipping", result.stdout)
        self.assertEqual(self.published(), [])
        self.assertTrue((self.staging / "lock").exists(), "a skipped run must not remove another's lock")

    def test_recovers_a_stale_lock(self):
        (self.staging / "lock").mkdir(parents=True)
        (self.staging / "lock" / "pid").write_text("999999")
        result = self.backup()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.published()), 1)

    def test_bad_keep_is_refused(self):
        for keep in ("0", "-1", "abc", ""):
            self.assertNotEqual(self.backup(KEEP=keep).returncode, 0, keep)


if __name__ == "__main__":
    unittest.main()
