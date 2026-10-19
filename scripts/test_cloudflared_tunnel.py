#!/usr/bin/env python3
"""Studio's Cloudflare Tunnel connector keeps its token out of argv and plists."""
import json
import os
import stat
import subprocess
import tempfile
import unittest
from functools import cache
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
STUDIO = "darwinConfigurations.studio.config"
TOKEN_FILE = "/etc/cloudflared/tunnel-token"


def nix_eval_json(option):
    result = subprocess.run(
        ["nix", "eval", "--json", f".#{option}"],
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


@cache
def service_config():
    return nix_eval_json(f"{STUDIO}.launchd.daemons.cloudflared-tunnel.serviceConfig")


class StudioTunnelDaemonTests(unittest.TestCase):
    def test_daemon_reads_the_token_from_its_root_only_file(self):
        args = service_config()["ProgramArguments"]
        self.assertEqual(["/bin/sh", "-c"], args[:2])
        self.assertEqual(["cloudflared-tunnel", TOKEN_FILE, "/opt/homebrew/bin/cloudflared"], args[3:])
        self.assertIn('exec "$2" tunnel run --token-file "$1"', args[2])
        self.assertNotIn("--token ", args[2])
        self.assertNotIn("--token=", args[2])

    def test_missing_token_stays_down_instead_of_looping(self):
        config = service_config()
        self.assertTrue(config["RunAtLoad"])
        keep_alive = {key: value for key, value in config["KeepAlive"].items() if value is not None}
        self.assertEqual({"SuccessfulExit": False}, keep_alive)
        self.assertEqual("/var/log/cloudflared-tunnel.log", config["StandardErrorPath"])

    def test_installed_plist_carries_no_token(self):
        plist = build_studio() / "Library/LaunchDaemons/org.nixos.cloudflared-tunnel.plist"
        text = plist.read_text()
        self.assertIn(TOKEN_FILE, text)
        self.assertNotIn("eyJ", text)
        self.assertNotIn("<string>--token</string>", text)

    def test_activation_makes_the_token_root_only(self):
        activation = (build_studio() / "activate").read_text()
        block = activation[activation.index("# === cloudflared-tunnel: root-only token ===") :]
        block = block[: block.index("fi\n")]
        self.assertIn("/usr/bin/install -d -m 0700 -o root -g wheel /etc/cloudflared", block)
        self.assertIn(f"/usr/sbin/chown root:wheel {TOKEN_FILE}", block)
        self.assertIn(f"/bin/chmod 0600 {TOKEN_FILE}", block)

    def test_other_hosts_do_not_run_it(self):
        for host in ("mbp", "a6mbp"):
            with self.subTest(host=host):
                self.assertFalse(
                    nix_eval_json(f"darwinConfigurations.{host}.config.launchd.daemons")
                    .get("cloudflared-tunnel")
                )


class LauncherBehaviourTests(unittest.TestCase):
    """Runs the daemon's inline launcher against a stand-in cloudflared."""

    def run_launcher(self, token_file, root):
        stand_in = root / "cloudflared"
        record = root / "argv"
        stand_in.write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" > {record}\n')
        stand_in.chmod(stand_in.stat().st_mode | stat.S_IXUSR)
        script = service_config()["ProgramArguments"][2]
        result = subprocess.run(
            ["/bin/sh", "-c", script, "cloudflared-tunnel", str(token_file), str(stand_in)],
            text=True,
            capture_output=True,
        )
        return result, record

    def test_absent_or_empty_token_exits_cleanly_without_starting(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            empty = root / "empty-token"
            empty.touch()
            for token_file in (root / "absent-token", empty):
                with self.subTest(token_file=token_file.name):
                    result, record = self.run_launcher(token_file, root)
                    self.assertEqual(0, result.returncode, result.stderr)
                    self.assertIn("missing or empty", result.stderr)
                    self.assertFalse(record.exists())

    def test_present_token_execs_cloudflared_with_only_the_file_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            token_file = root / "tunnel-token"
            token_file.write_text("not-a-real-token")
            os.chmod(token_file, 0o600)
            result, record = self.run_launcher(token_file, root)
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual(
                ["tunnel", "run", "--token-file", str(token_file)],
                record.read_text().splitlines(),
            )


if __name__ == "__main__":
    unittest.main()
