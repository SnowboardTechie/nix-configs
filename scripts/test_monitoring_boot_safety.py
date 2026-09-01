#!/usr/bin/env python3
import json
import os
import subprocess
import tempfile
import unittest
from functools import cache
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
STUDIO = "darwinConfigurations.studio.config"
MIGRATION_SCRIPT = REPO / "scripts" / "migrate-monitoring-storage.sh"


def nix_eval_json(option):
    result = subprocess.run(
        [
            "nix",
            "eval",
            "--json",
            f".#{option}",
        ],
        cwd=REPO,
        check=True,
        text=True,
        capture_output=True,
    )
    return json.loads(result.stdout)


@cache
def build_studio():
    result = subprocess.run(
        [
            "nix",
            "build",
            ".#darwinConfigurations.studio.system",
            "--no-link",
            "--print-out-paths",
        ],
        cwd=REPO,
        check=True,
        text=True,
        capture_output=True,
    )
    return Path(result.stdout.strip().splitlines()[-1])


class MonitoringBootSafetyTests(unittest.TestCase):
    def test_nix_store_daemons_start_through_boot_safe_wrapper(self):
        services = {
            "prometheus": "/bin/prometheus",
            "blackbox-exporter": "/bin/blackbox_exporter",
        }

        for service, executable_suffix in services.items():
            with self.subTest(service=service):
                args = nix_eval_json(
                    f"{STUDIO}.launchd.daemons.{service}.serviceConfig.ProgramArguments"
                )
                self.assertEqual("/bin/sh", args[0])
                self.assertEqual("-c", args[1])
                self.assertIn('while [ ! -x "$1" ]; do', args[2])
                self.assertIn('exec "$@"', args[2])
                self.assertEqual("wait-for-nix-store", args[3])
                self.assertTrue(args[4].endswith(executable_suffix), args[4])

    def test_activation_assigns_monitoring_storage_without_following_symlinks_as_root(self):
        activation = (build_studio() / "activate").read_text()
        paths = [
            "/Users/bryan/.prometheus/data",
            "/Users/bryan/.grafana/data",
            "/Users/bryan/.loki/data",
            "/Users/bryan/.alloy/data",
            "/Users/bryan/.alertmanager/data",
        ]

        start = activation.index("# === monitoring: migrate and ensure storage directories ===")
        end = activation.index("# === monitoring: firewall rules ===", start)
        monitoring_activation = activation[start:end]

        self.assertIn("migrate-monitoring-storage.sh", monitoring_activation)
        self.assertNotIn("/usr/sbin/chown", monitoring_activation)
        self.assertIn("/usr/bin/su - bryan -c", monitoring_activation)
        self.assertNotIn(
            "/usr/bin/install -d -m 0750 -o bryan", monitoring_activation
        )

        for path in paths:
            with self.subTest(path=path):
                self.assertIn(path, monitoring_activation)


class MonitoringStorageMigrationTests(unittest.TestCase):
    def run_migration(self, owner_group, *paths):
        return subprocess.run(
            ["/bin/sh", str(MIGRATION_SCRIPT), owner_group, *map(str, paths)],
            text=True,
            capture_output=True,
        )

    def test_rejects_symlinked_parent_without_changing_target(self):
        with tempfile.TemporaryDirectory(
            prefix="monitoring-storage-test-", dir=Path.home()
        ) as root:
            root = Path(root)
            physical_parent = root / "physical"
            physical_parent.mkdir()
            target = physical_parent / "data"
            target.mkdir()
            symlinked_parent = root / "linked"
            symlinked_parent.symlink_to(physical_parent, target_is_directory=True)

            original = target.stat()
            alternate_groups = [group for group in os.getgroups() if group != original.st_gid]
            requested_gid = alternate_groups[0] if alternate_groups else original.st_gid
            result = self.run_migration(
                f"{os.getuid()}:{requested_gid}", symlinked_parent / "data"
            )

            self.assertNotEqual(0, result.returncode)
            self.assertIn("symlinked parent", result.stderr)
            after = target.stat()
            self.assertEqual((original.st_uid, original.st_gid), (after.st_uid, after.st_gid))

    def test_migrates_directory_under_physical_parent(self):
        with tempfile.TemporaryDirectory(
            prefix="monitoring-storage-test-", dir=Path.home()
        ) as root:
            target = Path(root) / "data"
            target.mkdir()

            result = self.run_migration(
                f"{os.getuid()}:{os.getgid()}", target
            )

            self.assertEqual(0, result.returncode, result.stderr)
            current = target.stat()
            self.assertEqual((os.getuid(), os.getgid()), (current.st_uid, current.st_gid))


if __name__ == "__main__":
    unittest.main()
