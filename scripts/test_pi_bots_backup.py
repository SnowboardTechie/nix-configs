#!/usr/bin/env python3
"""services.pi-bots-backup and services.nas-mounts.

The backup and mount scripts run against stand-in mount tables, a stand-in
mount_smbfs and temporary directories; nothing touches a real share or the
real bot state. The age, zstd, timeout and promtool used are the ones the
Studio's configuration builds. The module is evaluated through a `path:`
flake reference so uncommitted changes are evaluated as on disk. Nothing is
activated.
"""
import json
import os
import re
import sqlite3
import subprocess
import tempfile
import time
import unicodedata
import unittest
from functools import cache
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
BACKUP = REPO / "scripts" / "pi-bots-backup.sh"
MOUNT = REPO / "scripts" / "nas-mount.sh"
STAMP_RE = re.compile(r"^\d{8}T\d{6}Z$")
NOW = 1_800_000_000


def nix(*args):
    result = subprocess.run(["nix", *args], cwd=REPO, text=True, capture_output=True)
    if result.returncode != 0:
        raise AssertionError(result.stderr[-4000:])
    return result.stdout


def studio_expr(expr, extra="{ }"):
    return f"""
      let
        flake = builtins.getFlake "path:{REPO}";
        studio = flake.darwinConfigurations.studio;
        inherit (studio) pkgs;
        inherit (pkgs) lib;
        config = (studio.extendModules {{ modules = [ {extra} ]; }}).config;
        flagValue = flag: args: lib.removePrefix flag (lib.findFirst (lib.hasPrefix flag) null args);
        prometheusConfig = flagValue "--config.file="
          config.launchd.daemons.prometheus.serviceConfig.ProgramArguments;
        backup = config.launchd.user.agents.pi-bots-backup.serviceConfig;
        mounts = config.launchd.user.agents.nas-mounts.serviceConfig;
      in {expr}
    """


@cache
def studio(expr, extra="{ }"):
    return json.loads(nix("eval", "--json", "--impure", "--expr", studio_expr(expr, extra)))


def refused(extra):
    result = subprocess.run(["nix", "eval", "--impure", "--expr",
                             studio_expr("config.system.build.toplevel.drvPath", extra)],
                            cwd=REPO, text=True, capture_output=True)
    return result


@cache
def realised():
    out = nix("build", "--no-link", "--print-out-paths", "--impure", "--expr", studio_expr("""
      pkgs.writeText "pi-bots-backup-paths.json" (builtins.toJSON {
        prometheusConfig = prometheusConfig;
        promtool = "${pkgs.prometheus.cli}/bin/promtool";
        timeout = "${pkgs.coreutils}/bin/timeout";
        age = backup.EnvironmentVariables.AGE;
        ageKeygen = "${pkgs.age}/bin/age-keygen";
        zstd = backup.EnvironmentVariables.ZSTD;
      })
    """))
    return json.loads(Path(out.strip().splitlines()[-1]).read_text())


def rules():
    rule_file = json.loads(Path(realised()["prometheusConfig"]).read_text())["rule_files"][0]
    return rule_file, {r["alert"]: r for g in json.loads(Path(rule_file).read_text())["groups"]
                       for r in g["rules"]}


def keygen(path):
    subprocess.run([realised()["ageKeygen"], "-o", str(path)], check=True, capture_output=True)
    return re.search(r"public key: (age1\w+)", path.read_text()).group(1)


class Backup:
    """A stand-in bot state, secrets directory and share."""

    def __init__(self, test):
        self.test = test
        self.tmp = Path(tempfile.mkdtemp())
        test.addCleanup(subprocess.run, ["chmod", "-R", "u+w", str(self.tmp)])
        test.addCleanup(subprocess.run, ["rm", "-rf", str(self.tmp)])
        self.state = self.tmp / "state"
        self.secrets = self.tmp / "secrets"
        self.share = self.tmp / "NAS" / "Bryan"
        self.dest = self.share / "Backups" / "pi-bots"
        self.staging = self.tmp / "staging"
        self.metrics = self.tmp / "metrics"
        self.identity = self.tmp / "keys" / "identity.txt"
        self.identity.parent.mkdir()
        self.recipient = keygen(self.identity)
        self.make_state()
        self.share.mkdir(parents=True)
        self.table = self.tmp / "mount-table"
        self.mount()
        mount_cmd = self.tmp / "mount"
        mount_cmd.write_text('#!/bin/sh\ncat "$TABLE"\n')
        mount_cmd.chmod(0o755)
        r = realised()
        self.env = {
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "HOME": str(self.tmp),
            "TABLE": str(self.table),
            "STATE_DIR": str(self.state),
            "SECRETS_DIR": str(self.secrets),
            "MOUNT_POINT": str(self.share),
            "DEST_DIR": str(self.dest),
            "STAGING_DIR": str(self.staging),
            "METRICS_DIR": str(self.metrics),
            "AGE_RECIPIENTS": self.recipient,
            "AGE_IDENTITY": str(self.identity),
            "AGE": r["age"],
            "ZSTD": r["zstd"],
            "TIMEOUT_CMD": r["timeout"],
            "MOUNT_CMD": str(mount_cmd),
            "MOUNT_DEVICE_CHECK": "0",
            "TIMEOUT": "10",
        }

    def make_state(self):
        bot = self.state / "bots" / "bob-bot"
        (bot / "crypto" / "abc").mkdir(parents=True)
        (bot / "sessions").mkdir()
        (bot / "sessions" / "s_bob.jsonl").write_text('{"type":"message","text":"remember maple"}\n')
        (bot / "sync.json").write_text('{"since":"s1"}\n')
        (bot / "lock").write_text("")
        (bot / "lock-journal").write_bytes(b"\0" * 16)
        (self.state / "metrics").mkdir()
        (self.state / "metrics" / "pi-bots.prom").write_text("pi_bots_service_up 1\n")
        # A WAL-mode database with committed rows still only in the -wal file,
        # as the live key store and journal are while the service runs.
        self.held = sqlite3.connect(bot / "bridge.db")
        self.held.execute("PRAGMA journal_mode=WAL")
        self.held.execute("PRAGMA wal_autocheckpoint=0")
        self.held.execute("CREATE TABLE messages (id TEXT, status TEXT)")
        self.held.execute("INSERT INTO messages VALUES ('$e1', 'answered')")
        self.held.commit()
        self.test.addCleanup(self.held.close)
        keys = sqlite3.connect(bot / "crypto" / "abc" / "matrix-sdk-crypto.sqlite3")
        keys.execute("CREATE TABLE k (v TEXT)")
        keys.execute("INSERT INTO k VALUES ('olm')")
        keys.commit()
        keys.close()
        bots = self.secrets / "bots" / "bob-bot"
        bots.mkdir(parents=True)
        (bots / "recovery-key").write_text("EsT0 fake recovery key\n")
        (bots / "session.json").write_text('{"accessToken":"fake-token-for-tests"}\n')
        (self.secrets / "registration.yaml").write_text("as_token: fake-as-token-for-tests\n")
        (self.secrets / "owner").write_text("@someone:example.org\n")

    def mount(self, mounted=True, fstype="smbfs"):
        line = f"//bryan@nas/Bryan on {self.share} ({fstype}, nodev, nosuid, mounted by bryan)\n"
        self.table.write_text(line if mounted else "/dev/disk3s5 on / (apfs, local)\n")

    def run(self, stamp="20261111T113000Z", **env):
        return subprocess.run(["/bin/bash", str(BACKUP)], cwd=self.tmp,
                              env={**self.env, "STAMP": stamp, **env}, text=True, capture_output=True)

    def published(self):
        if not self.dest.exists():
            return []
        return sorted(p.name for p in self.dest.iterdir())

    def decrypt(self, stamp, identity=None):
        """The archive's members as {path: bytes}."""
        r = realised()
        out = self.tmp / f"open-{stamp}"
        out.mkdir()
        age = subprocess.run([r["age"], "-d", "-i", str(identity or self.identity),
                              str(self.dest / stamp / "pi-bots-state.tar.zst.age")], capture_output=True)
        if age.returncode != 0:
            raise AssertionError(age.stderr.decode())
        tar = subprocess.run(f'"{r["zstd"]}" -q -d -c | tar -C "{out}" -xf -', shell=True,
                             input=age.stdout, capture_output=True)
        self.test.assertEqual(0, tar.returncode, tar.stderr)
        return {str(p.relative_to(out)): p.read_bytes() for p in out.rglob("*") if p.is_file()}

    def metric(self, name):
        text = (self.metrics / "pi-bots-backup.prom").read_text()
        for line in text.splitlines():
            if line.startswith(name + " "):
                return int(line.split()[1])
        return None


class BackupScript(unittest.TestCase):
    def test_a_backup_lands_encrypted_and_opens_with_the_key(self):
        b = Backup(self)
        result = b.run()
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(["20261111T113000Z"], b.published())
        stamp = b.dest / "20261111T113000Z"
        self.assertEqual(["MANIFEST.json", "pi-bots-state.tar.zst.age"], sorted(p.name for p in stamp.iterdir()))
        files = b.decrypt("20261111T113000Z")
        self.assertIn("state/bots/bob-bot/sessions/s_bob.jsonl", files)
        self.assertIn("state/bots/bob-bot/sync.json", files)
        self.assertIn("secrets/bots/bob-bot/recovery-key", files)
        self.assertIn("secrets/bots/bob-bot/session.json", files)
        self.assertIn("secrets/registration.yaml", files)
        self.assertIn("secrets/owner", files)
        manifest = json.loads((stamp / "MANIFEST.json").read_text())
        self.assertEqual(2, manifest["databases"])
        self.assertEqual(len(files), manifest["files"])
        self.assertEqual("age", manifest["encryption"])
        self.assertEqual(1, b.metric("pi_bots_backup_success"))
        self.assertEqual(manifest["bytes"], b.metric("pi_bots_backup_archive_bytes"))
        self.assertEqual([], list(b.staging.glob("work-*")), "local plaintext left behind")

    def test_nothing_readable_reaches_the_share(self):
        b = Backup(self)
        self.assertEqual(0, b.run().returncode)
        for f in (b.dest / "20261111T113000Z").iterdir():
            data = f.read_bytes()
            for secret in (b"fake-token-for-tests", b"fake-as-token-for-tests", b"recovery key",
                           b"remember maple", b"@someone"):
                self.assertNotIn(secret, data, f.name)
        self.assertTrue((b.dest / "20261111T113000Z" / "pi-bots-state.tar.zst.age").read_bytes()
                        .startswith(b"age-encryption.org/v1\n"))

    def test_another_key_cannot_open_it(self):
        b = Backup(self)
        self.assertEqual(0, b.run().returncode)
        other = b.tmp / "keys" / "other.txt"
        keygen(other)
        with self.assertRaises(AssertionError):
            b.decrypt("20261111T113000Z", identity=other)

    def test_databases_are_copied_through_sqlite_with_their_wal(self):
        b = Backup(self)
        self.assertTrue((b.state / "bots/bob-bot/bridge.db-wal").stat().st_size > 0)
        self.assertEqual(0, b.run().returncode)
        files = b.decrypt("20261111T113000Z")
        self.assertFalse(any(k.endswith(("-wal", "-shm")) for k in files), sorted(files))
        out = b.tmp / "open-20261111T113000Z" / "state" / "bots" / "bob-bot"
        db = sqlite3.connect(out / "bridge.db")
        self.assertEqual([("$e1", "answered")], db.execute("SELECT * FROM messages").fetchall())
        db.close()
        keys = sqlite3.connect(out / "crypto" / "abc" / "matrix-sdk-crypto.sqlite3")
        self.assertEqual([("olm",)], keys.execute("SELECT * FROM k").fetchall())
        keys.close()

    def test_sockets_locks_and_the_metrics_directory_are_left_out(self):
        b = Backup(self)
        import socket
        sock_path = b.state / "bots" / "bob-bot" / "control.sock"
        s = socket.socket(socket.AF_UNIX)
        try:
            s.bind(str(sock_path))
        except OSError:  # path too long for AF_UNIX on this temp dir
            s = None
        result = b.run()
        if s:
            s.close()
        self.assertEqual(0, result.returncode, result.stderr)
        files = b.decrypt("20261111T113000Z")
        for name in files:
            self.assertFalse(name.endswith(("/lock", "-journal", ".sock")), name)
            self.assertFalse(name.startswith("state/metrics"), name)

    def test_an_unmounted_share_is_refused_and_nothing_written(self):
        b = Backup(self)
        b.mount(mounted=False)
        result = b.run()
        self.assertNotEqual(0, result.returncode)
        self.assertIn("is not mounted", result.stderr)
        self.assertFalse(b.dest.exists())
        self.assertEqual(0, b.metric("pi_bots_backup_success"))
        self.assertIsNone(b.metric("pi_bots_backup_last_success_timestamp_seconds"))

    def test_a_non_smb_mount_does_not_count(self):
        b = Backup(self)
        b.mount(fstype="apfs")
        self.assertNotEqual(0, b.run().returncode)
        self.assertFalse(b.dest.exists())

    def test_a_failure_keeps_the_last_success_time(self):
        b = Backup(self)
        self.assertEqual(0, b.run("20261110T113000Z").returncode)
        last = b.metric("pi_bots_backup_last_success_timestamp_seconds")
        self.assertIsNotNone(last)
        b.mount(mounted=False)
        time.sleep(1.1)
        self.assertNotEqual(0, b.run("20261111T113000Z").returncode)
        self.assertEqual(0, b.metric("pi_bots_backup_success"))
        self.assertEqual(last, b.metric("pi_bots_backup_last_success_timestamp_seconds"))
        self.assertGreater(b.metric("pi_bots_backup_last_run_timestamp_seconds"), last)

    def test_a_read_only_share_fails_and_publishes_nothing(self):
        b = Backup(self)
        b.dest.mkdir(parents=True)
        b.dest.chmod(0o555)
        result = b.run()
        self.assertNotEqual(0, result.returncode)
        b.dest.chmod(0o755)
        self.assertEqual([], b.published())
        self.assertEqual(0, b.metric("pi_bots_backup_success"))

    def test_a_corrupt_copy_on_the_share_is_not_published(self):
        # A share that mangles what it stores: cp writes, then the bytes change.
        b = Backup(self)
        bin_dir = b.tmp / "bin"
        bin_dir.mkdir()
        (bin_dir / "cp").write_text('#!/bin/sh\n/bin/cp "$@" && for d; do :; done && '
                                    'printf x >> "$d/MANIFEST.json"\n')
        (bin_dir / "cp").chmod(0o755)
        result = b.run(PATH=f"{bin_dir}:/usr/bin:/bin:/usr/sbin:/sbin")
        self.assertNotEqual(0, result.returncode)
        self.assertIn("checksum mismatch", result.stderr)
        self.assertEqual([], b.published(), "a partial directory was left or published")

    def test_a_hanging_share_times_out(self):
        b = Backup(self)
        bin_dir = b.tmp / "bin"
        bin_dir.mkdir()
        (bin_dir / "cp").write_text("#!/bin/sh\nexec sleep 60\n")
        (bin_dir / "cp").chmod(0o755)
        started = time.monotonic()
        result = b.run(PATH=f"{bin_dir}:/usr/bin:/bin:/usr/sbin:/sbin", TIMEOUT="2")
        self.assertLess(time.monotonic() - started, 30)
        self.assertNotEqual(0, result.returncode)
        self.assertIn("copying to", result.stderr)
        self.assertEqual([], b.published())

    def test_an_archive_that_does_not_open_is_not_published(self):
        # The check key is not the one the archive is encrypted to.
        b = Backup(self)
        other = b.tmp / "keys" / "other.txt"
        keygen(other)
        result = b.run(AGE_IDENTITY=str(other))
        self.assertNotEqual(0, result.returncode)
        self.assertIn("does not open", result.stderr)
        self.assertFalse(b.dest.exists())

    def test_encrypts_to_every_recipient(self):
        b = Backup(self)
        second = b.tmp / "keys" / "second.txt"
        recipient = keygen(second)
        self.assertEqual(0, b.run(AGE_RECIPIENTS=f"{b.recipient} {recipient}").returncode)
        self.assertIn("secrets/owner", b.decrypt("20261111T113000Z", identity=second))

    def test_refuses_a_private_key_or_junk_as_recipient(self):
        b = Backup(self)
        for bad in ("AGE-SECRET-KEY-1QQQ", "ssh-ed25519 AAAA", "age1short", ""):
            result = b.run(AGE_RECIPIENTS=bad)
            self.assertNotEqual(0, result.returncode, bad)
            self.assertFalse(b.dest.exists(), bad)

    def test_refuses_a_destination_outside_the_share(self):
        b = Backup(self)
        for dest in (str(b.tmp / "elsewhere"), str(b.share), f"{b.share}/../x", f"{b.share}/./x"):
            result = b.run(DEST_DIR=dest)
            self.assertNotEqual(0, result.returncode, dest)
        self.assertFalse((b.tmp / "elsewhere").exists())

    def test_retention_keeps_the_newest_and_only_touches_stamps(self):
        b = Backup(self)
        b.dest.mkdir(parents=True)
        for name in ("20261101T113000Z", "20261102T113000Z", "20261103T113000Z", "keep-me",
                     ".partial-20261104T113000Z", ".partial-junk"):
            (b.dest / name).mkdir()
        (b.dest / "notes.txt").write_text("mine")
        self.assertEqual(0, b.run("20261105T113000Z", KEEP="2").returncode)
        self.assertEqual([".partial-junk", "20261103T113000Z", "20261105T113000Z", "keep-me", "notes.txt"],
                         b.published())

    def test_a_second_run_while_one_holds_the_lock_skips(self):
        b = Backup(self)
        b.staging.mkdir(mode=0o700)
        (b.staging / "lock").mkdir()
        holder = subprocess.Popen(["sleep", "30"])
        self.addCleanup(holder.kill)
        (b.staging / "lock" / "pid").write_text(str(holder.pid))
        result = b.run()
        self.assertEqual(0, result.returncode)
        self.assertIn("is running; skipping", result.stdout)
        self.assertFalse(b.dest.exists())

    def test_a_stale_lock_is_recovered(self):
        b = Backup(self)
        b.staging.mkdir(mode=0o700)
        (b.staging / "lock").mkdir()
        (b.staging / "lock" / "pid").write_text("999999")
        result = b.run()
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(["20261111T113000Z"], b.published())

    def test_a_killed_run_leaves_no_plaintext_after_the_next_run(self):
        b = Backup(self)
        slow = b.tmp / "slow-zstd"
        slow.write_text("#!/bin/sh\nexec sleep 60\n")
        slow.chmod(0o755)
        proc = subprocess.Popen(["/bin/bash", str(BACKUP)], cwd=b.tmp, start_new_session=True,
                                env={**b.env, "STAMP": "20261110T113000Z", "ZSTD": str(slow)},
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        left = b.staging / "work-20261110T113000Z" / "tree" / "secrets" / "owner"
        for _ in range(100):
            if left.exists():
                break
            time.sleep(0.1)
        self.assertTrue(left.exists(), "the run never reached the encrypt stage")
        os.killpg(proc.pid, 9)
        proc.wait()
        self.assertTrue(left.exists())
        result = b.run()
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual([], list(b.staging.glob("work-*")))
        self.assertEqual(["20261111T113000Z"], b.published())

    def test_a_symlink_in_the_state_is_kept_and_does_not_fail_the_run(self):
        b = Backup(self)
        (b.state / "bots" / "bob-bot" / "current").symlink_to("sync.json")
        result = b.run()
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(["20261111T113000Z"], b.published())
        b.decrypt("20261111T113000Z")
        link = b.tmp / "open-20261111T113000Z" / "state" / "bots" / "bob-bot" / "current"
        self.assertTrue(link.is_symlink())

    def test_a_non_ascii_name_does_not_fail_the_run(self):
        b = Backup(self)
        (b.state / "bots" / "bob-bot" / "caf\u00e9.json").write_text("{}")
        result = b.run()  # no LANG/LC_ALL, as under launchd
        self.assertEqual(0, result.returncode, result.stderr)
        names = {unicodedata.normalize("NFC", n) for n in b.decrypt("20261111T113000Z")}
        self.assertIn("state/bots/bob-bot/caf\u00e9.json", names)

    def test_a_share_that_is_not_its_own_device_is_refused(self):
        # The mount table claims smbfs, but the directory is on the local disk.
        b = Backup(self)
        result = b.run(MOUNT_DEVICE_CHECK="1")
        self.assertNotEqual(0, result.returncode)
        self.assertIn("same device as its parent", result.stderr)
        self.assertFalse(b.dest.exists())

    def test_database_copies_stand_alone(self):
        b = Backup(self)
        self.assertEqual(0, b.run().returncode)
        b.decrypt("20261111T113000Z")
        out = b.tmp / "open-20261111T113000Z" / "state" / "bots" / "bob-bot"
        for db_path in (out / "bridge.db", out / "crypto" / "abc" / "matrix-sdk-crypto.sqlite3"):
            db = sqlite3.connect(db_path)
            self.assertEqual("delete", db.execute("PRAGMA journal_mode").fetchone()[0], db_path)
            db.close()

    def test_retention_never_follows_a_link_named_like_a_backup(self):
        b = Backup(self)
        b.dest.mkdir(parents=True)
        outside = b.tmp / "outside"
        outside.mkdir()
        (outside / "precious").write_text("x")
        (b.dest / "20261101T113000Z").symlink_to(outside)
        (b.dest / ".partial-20261102T113000Z").symlink_to(outside)
        self.assertEqual(0, b.run("20261105T113000Z", KEEP="1").returncode)
        self.assertTrue((outside / "precious").exists())
        self.assertEqual([".partial-20261102T113000Z", "20261101T113000Z", "20261105T113000Z"], b.published())

    def test_metrics_are_readable_and_promtool_accepts_them(self):
        b = Backup(self)
        self.assertEqual(0, b.run().returncode)
        prom = b.metrics / "pi-bots-backup.prom"
        self.assertEqual(0o644, prom.stat().st_mode & 0o777)
        result = subprocess.run([realised()["promtool"], "check", "metrics"], input=prom.read_text(),
                                text=True, capture_output=True)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual(0o700, b.staging.stat().st_mode & 0o777)


class MountScript(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(subprocess.run, ["rm", "-rf", str(self.tmp)])
        self.table = self.tmp / "table"
        self.table.write_text("/dev/disk3s5 on / (apfs, local)\n")
        self.calls = self.tmp / "calls"
        mount_cmd = self.tmp / "mount"
        mount_cmd.write_text('#!/bin/sh\ncat "$TABLE"\n')
        mount_cmd.chmod(0o755)
        smbfs = self.tmp / "mount_smbfs"
        # Records its arguments and stdin's source; "mounts" by adding a line.
        smbfs.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$CALLS"\n'
                         '[ -n "${FAIL:-}" ] && { echo "mount_smbfs: server connection failed: $FAIL" >&2; exit 64; }\n'
                         'for a; do last=$a; done\n'
                         'echo "//x on $last (smbfs, nodev, nosuid, mounted by bryan)" >> "$TABLE"\n')
        smbfs.chmod(0o755)
        self.mp = self.tmp / "NAS" / "Bryan"
        self.env = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "TABLE": str(self.table),
                    "CALLS": str(self.calls), "MOUNT_CMD": str(mount_cmd), "MOUNT_SMBFS": str(smbfs),
                    "MOUNT_DEVICE_CHECK": "0", "TIMEOUT_CMD": realised()["timeout"], "TIMEOUT": "5"}

    def run_mount(self, *args, **env):
        args = args or ("//bryan@nas.local/Bryan", str(self.mp))
        return subprocess.run(["/bin/sh", str(MOUNT), *args], env={**self.env, **env},
                              text=True, capture_output=True)

    def test_mounts_an_unmounted_share_from_the_keychain(self):
        result = self.run_mount()
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual(f"-N -o soft,nobrowse //bryan@nas.local/Bryan {self.mp}\n", self.calls.read_text())
        self.assertTrue(self.mp.is_dir())

    def test_leaves_a_live_mount_alone(self):
        self.mp.mkdir(parents=True)
        self.table.write_text(f"//bryan@nas/Bryan on {self.mp} (smbfs, nodev)\n")
        self.assertEqual(0, self.run_mount().returncode)
        self.assertFalse(self.calls.exists())

    def test_never_mounts_over_files(self):
        self.mp.mkdir(parents=True)
        (self.mp / "local.txt").write_text("x")
        result = self.run_mount()
        self.assertEqual(1, result.returncode)
        self.assertIn("not empty", result.stdout)
        self.assertFalse(self.calls.exists())

    def test_never_mounts_over_another_mount(self):
        self.mp.mkdir(parents=True)
        self.table.write_text(f"/dev/disk9 on {self.mp} (apfs, local)\n")
        result = self.run_mount()
        self.assertEqual(1, result.returncode)
        self.assertFalse(self.calls.exists())

    def test_a_failed_mount_exits_nonzero_with_the_reason(self):
        result = self.run_mount(FAIL="no route")
        self.assertEqual(1, result.returncode)
        self.assertIn("no route", result.stdout)

    def test_a_hanging_mount_times_out(self):
        slow = self.tmp / "slow_smbfs"
        slow.write_text("#!/bin/sh\nexec sleep 60\n")
        slow.chmod(0o755)
        started = time.monotonic()
        result = self.run_mount(MOUNT_SMBFS=str(slow), TIMEOUT="1")
        self.assertLess(time.monotonic() - started, 20)
        self.assertEqual(1, result.returncode)

    def test_a_mount_point_on_the_parent_device_is_not_taken_for_a_mount(self):
        self.mp.mkdir(parents=True)
        self.table.write_text(f"//bryan@nas/Bryan on {self.mp} (smbfs, nodev)\n")
        result = self.run_mount(MOUNT_DEVICE_CHECK="1")
        self.assertEqual(1, result.returncode)
        self.assertIn("not a live SMB mount", result.stdout)
        self.assertFalse(self.calls.exists())

    def test_refuses_bad_arguments(self):
        for args in (("//bryan@nas/Bryan",), ("nas/Bryan", "/x"), ("//u:pw@nas/Bryan", "/x"),
                     ("//bryan@nas/Bryan", "rel"), ("//bryan@nas/Bryan", "/a/../b"),
                     ("//bryan@nas/Br yan", "/x")):
            result = self.run_mount(*args)
            self.assertEqual(2, result.returncode, args)
        self.assertFalse(self.calls.exists())


class Module(unittest.TestCase):
    def test_studio_backs_up_nightly_to_the_mounted_share(self):
        agent = studio("backup")
        env = agent["EnvironmentVariables"]
        self.assertEqual("org.nixos.pi-bots-backup", agent["Label"])
        self.assertEqual([{"Hour": 3, "Minute": 30}],
                         [{k: v for k, v in e.items() if v is not None} for e in agent["StartCalendarInterval"]])
        self.assertEqual("/Users/bryan/.local/state/pi-bots", env["STATE_DIR"])
        self.assertEqual("/Users/bryan/.secrets/pi-bots", env["SECRETS_DIR"])
        self.assertEqual("/Users/bryan/NAS/Bryan", env["MOUNT_POINT"])
        self.assertEqual("/Users/bryan/NAS/Bryan/Backups/pi-bots", env["DEST_DIR"])
        self.assertRegex(env["AGE_RECIPIENTS"], r"^age1[0-9a-z]{58}$")
        self.assertFalse(env["AGE_IDENTITY"].startswith("/Users/bryan/NAS/"))
        self.assertTrue(env["AGE"].endswith("/bin/age"))
        self.assertNotIn("AGE-SECRET-KEY", json.dumps(agent))
        self.assertGreater(agent["ExitTimeOut"], int(env["TIMEOUT"]) + 5)

    def test_the_share_is_mounted_where_every_user_of_it_looks(self):
        got = studio("""{
          mounts = mounts;
          hermes = config.services.hermes-nas-backup.mountPoint;
          probe = config.services.nas-storage-health.shares.Bryan.mountPoint;
          backup = config.services.pi-bots-backup.mountPoint;
        }""")
        self.assertEqual("org.nixos.nas-mounts", got["mounts"]["Label"])
        self.assertEqual(["//bryan@192.168.1.3/Bryan", "/Users/bryan/NAS/Bryan"],
                         got["mounts"]["ProgramArguments"][1:])
        self.assertTrue(got["mounts"]["RunAtLoad"])
        self.assertEqual(300, got["mounts"]["StartInterval"])
        self.assertIn("/Library/Preferences/SystemConfiguration", got["mounts"]["WatchPaths"])
        self.assertEqual({got["hermes"], got["probe"], got["backup"]}, {"/Users/bryan/NAS/Bryan"})

    def test_node_exporter_reads_the_backup_metrics(self):
        args = studio("config.launchd.user.agents.node-exporter.serviceConfig.ProgramArguments")
        self.assertIn("--collector.textfile.directory=/Users/bryan/.local/state/pi-bots-backup/metrics", args)

    def test_refusals(self):
        cases = {
            '{ services.pi-bots-backup.destDir = lib.mkForce "/Users/bryan/elsewhere"; }': "plain path inside",
            '{ services.pi-bots-backup.ageIdentity = lib.mkForce "/Users/bryan/NAS/Bryan/key.txt"; }': "not be on the share",
            '{ services.pi-bots-backup.ageRecipients = lib.mkForce [ "AGE-SECRET-KEY-1QQQ" ]; }': "ageRecipients",
            '{ services.nas-mounts.shares.Bryan.mountPoint = lib.mkForce "/Volumes/Bryan"; }': "outside /Volumes",
            '{ services.nas-mounts.shares.Bryan.url = lib.mkForce "//bryan:pw@nas/Bryan"; }': "url",
        }
        for extra, message in cases.items():
            result = refused(extra)
            self.assertNotEqual(0, result.returncode, extra)
            self.assertIn(message, result.stderr, extra)


class Rules(unittest.TestCase):
    def promtool_test(self, inputs, alertname, *expectations):
        rule_file, _ = rules()
        spec = {"rule_files": [rule_file], "evaluation_interval": "1m", "tests": [{
            "interval": "1m", "input_series": inputs, "alert_rule_test": [
                {"eval_time": t, "alertname": alertname, "exp_alerts": exp} for t, exp in expectations]}]}
        with tempfile.TemporaryDirectory() as tmp:
            test_file = Path(tmp) / "t.json"
            test_file.write_text(json.dumps(spec))
            result = subprocess.run([realised()["promtool"], "test", "rules", str(test_file)],
                                    text=True, capture_output=True)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)

    def labels(self, name):
        return {"severity": rules()[1][name]["labels"]["severity"]}

    def firing(self, name, **labels):
        return [{"exp_labels": {**labels, **self.labels(name)}}]

    def test_rules_are_generated_and_accepted(self):
        rule_file, by_name = rules()
        self.assertTrue({"PiBotsBackupFailed", "PiBotsBackupOverdue", "PiBotsBackupMissing"} <= set(by_name))
        result = subprocess.run([realised()["promtool"], "check", "rules", rule_file],
                                text=True, capture_output=True)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)

    def test_a_failed_run_fires_at_once_and_clears_on_success(self):
        series = {"series": 'pi_bots_backup_success{job="node"}', "values": "1x5 0x5 1x5"}
        f = self.firing("PiBotsBackupFailed", job="node")
        f[0]["exp_annotations"] = rules()[1]["PiBotsBackupFailed"]["annotations"]
        self.promtool_test([series], "PiBotsBackupFailed", ("3m", []), ("7m", f), ("13m", []))

    def test_no_success_in_over_a_day_fires(self):
        # Last success at t=0, the series keeps reporting it; the run stops succeeding.
        hours = 30
        series = {"series": 'pi_bots_backup_last_success_timestamp_seconds{job="node"}',
                  "values": f"0+0x{hours * 60}"}
        overdue = [{"exp_labels": {"job": "node", **self.labels("PiBotsBackupOverdue")},
                    "exp_annotations": {
                        "summary": "No bot service backup in over a day",
                        "description": "The last bot service backup published to the NAS is 1d 3h 30m 0s old "
                                       "(it runs daily at 3:30)."}}]
        self.promtool_test([series], "PiBotsBackupOverdue", ("25h", []), ("26h30m", []), ("27h30m", overdue))

    def test_no_metrics_at_all_fires_after_a_day(self):
        series = {"series": 'up{job="node"}', "values": "1x1700"}
        missing = [{"exp_labels": self.labels("PiBotsBackupMissing"),
                    "exp_annotations": rules()[1]["PiBotsBackupMissing"]["annotations"]}]
        self.promtool_test([series], "PiBotsBackupMissing", ("25h", []), ("26h30m", missing))

    def test_a_present_last_run_does_not_count_as_missing(self):
        series = {"series": 'pi_bots_backup_last_run_timestamp_seconds{job="node"}', "values": "0x1700"}
        self.promtool_test([series], "PiBotsBackupMissing", ("27h", []))


if __name__ == "__main__":
    unittest.main()
