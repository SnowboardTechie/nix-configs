#!/usr/bin/env python3
"""services.pi-bots-voice: the voice service's launchd agent, Serve route,
firewall exception and content probe.

The module is evaluated through a `path:` flake reference so uncommitted
changes are evaluated as on disk. The firewall block is run against a stand-in
`socketfilterfw`; the Serve block is only read. Nothing is
activated, started or exposed.
"""
import json
import re
import subprocess
import tempfile
import unittest
from functools import cache
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CHECKOUT = "/Users/bryan/code/pi-bots-voice"
FIREWALL = "/usr/libexec/ApplicationFirewall/socketfilterfw"


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
        agent = config.launchd.user.agents.pi-bots-voice.serviceConfig;
        activation = config.system.activationScripts.extraActivation.text;
      in {expr}
    """


@cache
def studio(expr, extra="{ }"):
    return json.loads(nix("eval", "--json", "--impure", "--expr", studio_expr(expr, extra)))


def refused(extra):
    return subprocess.run(["nix", "eval", "--impure", "--expr",
                           studio_expr("config.system.build.toplevel.drvPath", extra)],
                          cwd=REPO, text=True, capture_output=True)


def block(script, heading):
    """The lines of one `# === ... ===` block, up to the next blank line."""
    start = script.index(heading)
    end = script.find("\n\n", start)
    return script[start:] if end < 0 else script[start:end]


SERVE = "# === pi-bots-voice: service via Tailscale Serve ==="
WALL = "# === pi-bots-voice: firewall exception for the venv's interpreter ==="


class Agent(unittest.TestCase):
    def test_runs_the_venv_interpreter_from_the_checkout(self):
        agent = studio("agent")
        self.assertEqual("org.nixos.pi-bots-voice", agent["Label"])
        # Not `uv run`, not Homebrew's Python: the firewall allows this one.
        self.assertEqual([f"{CHECKOUT}/service/.venv/bin/python", "-m", "pi_bots_voice"], agent["ProgramArguments"])
        self.assertEqual(f"{CHECKOUT}/service", agent["WorkingDirectory"])
        self.assertTrue(agent["RunAtLoad"])
        self.assertTrue(agent["KeepAlive"])
        self.assertEqual(30, agent["ThrottleInterval"])
        self.assertEqual("/tmp/pi-bots-voice.log", agent["StandardOutPath"])
        self.assertEqual("/tmp/pi-bots-voice.error.log", agent["StandardErrorPath"])

    def test_environment_is_home_and_path_only(self):
        env = studio("agent")["EnvironmentVariables"]
        # No HF_HUB_OFFLINE (the service decides it from config), no secret.
        self.assertEqual({"HOME", "PATH"}, set(env))
        self.assertEqual("/Users/bryan", env["HOME"])
        # The plain-Pi agent starts `pi`, which needs Homebrew's node.
        self.assertIn("/opt/homebrew/bin", env["PATH"].split(":"))
        self.assertIn("/usr/bin", env["PATH"].split(":"))

    def test_the_checkout_option_moves_it(self):
        agent = studio("agent", '{ services.pi-bots-voice.checkout = lib.mkForce "/tmp/elsewhere"; }')
        self.assertEqual("/tmp/elsewhere/service/.venv/bin/python", agent["ProgramArguments"][0])
        self.assertEqual("/tmp/elsewhere/service", agent["WorkingDirectory"])

    def test_it_is_not_re_pinned_as_a_homebrew_agent(self):
        # Its program is not under /opt/homebrew; base/homebrew.nix must leave it alone.
        script = studio("config.system.activationScripts.postActivation.text")
        line = next(l for l in script.splitlines() if l.lstrip().startswith("for brew_agent in"))
        self.assertNotIn("pi-bots-voice", line.split())


class Activation(unittest.TestCase):
    def test_serve_route_on_its_port(self):
        serve = block(studio("activation"), SERVE)
        self.assertIn("tailscale serve", serve)
        self.assertIn("--bg", serve)
        self.assertIn("--yes", serve)
        self.assertIn("--https=7860", serve)
        self.assertIn("http://127.0.0.1:7860;", serve)
        self.assertIn("timeout --foreground 30s", serve)
        self.assertNotIn("funnel", serve.lower())

    def test_serve_route_follows_the_ports(self):
        serve = block(studio("activation", "{ services.pi-bots-voice = { port = lib.mkForce 7999; "
                                           "tailscaleHttpsPort = lib.mkForce 8999; }; }"), SERVE)
        self.assertIn("--https=8999", serve)
        self.assertIn("http://127.0.0.1:7999;", serve)

    def test_no_serve_route_without_a_tailnet_port(self):
        script = studio("activation", "{ services.pi-bots-voice.tailscaleHttpsPort = lib.mkForce null; }")
        self.assertNotIn(SERVE, script)
        self.assertNotIn("--https=7860", script)
        # The firewall exception stays: a browser on the Mac needs it too.
        self.assertIn(WALL, script)

    def test_firewall_block_allows_the_resolved_interpreter(self):
        wall = block(studio("activation"), WALL)
        self.assertIn(f"realpath -e {CHECKOUT}/service/.venv/bin/python", wall)
        self.assertIn(f'{FIREWALL} --add "$pi_bots_voice_python"', wall)
        self.assertIn(f'{FIREWALL} --unblockapp "$pi_bots_voice_python"', wall)

    def run_firewall_block(self, venv_python):
        """Run the block with a stand-in socketfilterfw; return its calls, stderr and exit code."""
        wall = block(studio("activation"), WALL)
        with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
            tmp = Path(tmp)
            calls = tmp / "calls"
            fake = tmp / "socketfilterfw"
            fake.write_text(f'#!/bin/sh\nprintf "%s\\n" "$*" >> {calls}\n')
            fake.chmod(0o755)
            text = wall.replace(FIREWALL, str(fake)).replace(f"{CHECKOUT}/service/.venv/bin/python", venv_python(tmp))
            result = subprocess.run(["/bin/sh", "-e", "-c", text + "\necho done\n"], text=True, capture_output=True)
            return (calls.read_text().splitlines() if calls.exists() else []), result

    def test_firewall_block_resolves_the_symlink(self):
        def venv(tmp):
            real = tmp / "uv" / "python3.12"
            real.parent.mkdir()
            real.write_text("")
            link = tmp / "python"
            link.symlink_to(real)
            return str(link)
        calls, result = self.run_firewall_block(venv)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("done\n", result.stdout)
        real = [c.split(" ", 1)[1] for c in calls]
        self.assertEqual(["--add", "--unblockapp"], [c.split(" ", 1)[0] for c in calls])
        self.assertTrue(all(r.endswith("/uv/python3.12") for r in real), calls)

    def test_firewall_block_tolerates_a_missing_venv(self):
        calls, result = self.run_firewall_block(lambda tmp: str(tmp / "missing" / "python"))
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("done\n", result.stdout)
        self.assertEqual([], calls)
        self.assertEqual(1, len(result.stderr.splitlines()), result.stderr)
        self.assertIn("pi-bots-voice", result.stderr)

    def test_studio_builds_the_blocks_into_activate(self):
        out = nix("build", "--no-link", "--print-out-paths", ".#darwinConfigurations.studio.system")
        activate = (Path(out.strip().splitlines()[-1]) / "activate").read_text()
        self.assertIn(WALL, activate)
        self.assertIn(SERVE, activate)


class Probe(unittest.TestCase):
    STATUS = ('{"state":"ready","agent":"bob","voice":"marius","listen_while_speaking":false,'
              '"connections":[],"last_turn":null,"time":1794340000.0,"you":"loopback"}')

    def test_probe_on_the_status_route(self):
        probe = studio('config.services.monitoring.blackbox.contentProbes."Pi Bots Voice"')
        self.assertEqual("http://127.0.0.1:7860/v1/status", probe["url"])
        # Go's RE2 and Python's re agree on this pattern.
        self.assertTrue(re.search(probe["bodyMatches"], self.STATUS))
        self.assertTrue(re.search(probe["bodyMatches"], self.STATUS.replace("ready", "loading")))
        self.assertFalse(re.search(probe["bodyMatches"], '{"detail":"forbidden","reason":"x"}'))
        self.assertFalse(re.search(probe["bodyMatches"], "<html>Pipecat</html>"))

    def test_dashy_links_its_test_page(self):
        links = studio("map (l: { inherit (l) title url statusName; }) config.services.dashy.dailyLinks")
        self.assertIn({"title": "Pi Bots Voice", "statusName": "Pi Bots Voice",
                       "url": "https://bryans-mac-studio.tail5ba690.ts.net:7860/client/"}, links)


class Refusals(unittest.TestCase):
    def test_serve_needs_tailscale(self):
        result = refused("{ services.tailscale.enable = lib.mkForce false; }")
        self.assertNotEqual(0, result.returncode)
        self.assertIn("services.pi-bots-voice.tailscaleHttpsPort requires services.tailscale", result.stderr)

    def test_relative_checkout(self):
        result = refused('{ services.pi-bots-voice.checkout = lib.mkForce "code/pi-bots-voice"; }')
        self.assertNotEqual(0, result.returncode)
        self.assertIn("checkout", result.stderr)


class OtherHosts(unittest.TestCase):
    def test_a_host_without_monitoring_evaluates_it(self):
        # mbp imports no monitoring module, so the probe must be left out, not fail.
        got = json.loads(nix("eval", "--json", "--impure", "--expr", f"""
          let
            flake = builtins.getFlake "path:{REPO}";
            mbp = flake.darwinConfigurations.mbp.extendModules {{ modules = [
              flake.modules.darwin.pi-bots-voice
              {{ services.pi-bots-voice.enable = true; }}
            ]; }};
          in {{
            program = builtins.head mbp.config.launchd.user.agents.pi-bots-voice.serviceConfig.ProgramArguments;
            monitoring = mbp.options.services ? monitoring;
            drv = builtins.isString mbp.config.system.build.toplevel.drvPath;
          }}
        """))
        self.assertEqual(False, got["monitoring"])
        self.assertTrue(got["drv"])
        self.assertTrue(got["program"].endswith("/code/pi-bots-voice/service/.venv/bin/python"))


class Off(unittest.TestCase):
    def test_off_declares_nothing(self):
        got = studio("""{
          agent = config.launchd.user.agents ? pi-bots-voice;
          probe = config.services.monitoring.blackbox.contentProbes ? "Pi Bots Voice";
          activation = lib.hasInfix "pi-bots-voice" activation;
        }""", "{ services.pi-bots-voice.enable = lib.mkForce false; "
              "services.dashy.dailyLinks = lib.mkForce [ ]; }")
        self.assertEqual({"agent": False, "probe": False, "activation": False}, got)


if __name__ == "__main__":
    unittest.main()
