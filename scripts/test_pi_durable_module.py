#!/usr/bin/env python3
"""services.pi-durable: Studio as commissioned; every other setting fails closed.

Evaluates through a `path:` flake reference so an uncommitted module is
evaluated as it is on disk (a git flake hides untracked files).
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from functools import cache
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
ROUTE_SCRIPT = REPO / "modules" / "services" / "pi-durable-page-route.sh"
PF_SCRIPT = REPO / "modules" / "services" / "pi-durable-pf.sh"
HOME = "/Users/bryan"
PF_ANCHOR = "com.apple/100.pi-durable-sgg"
BRIDGES = [f"bridge{n}" for n in range(100, 116)]
# Studio's anchor, as measured on 2026-10-16: the sandbox network reached every
# service bound to all interfaces, over IPv4 and IPv6, and the tailnet-only ones.
# Its gateway ports: tinyproxy (8888), the model proxy (8892, #268) and the
# GitHub write proxy's push and signing listener (8893, #268 slice 3) and the
# replay proxy (8889, durable-pi-workspace#21) and the forge broker (8894,
# durable-pi-workspace#48), last.
WRITE_PROXY_PF_RULE = "pass in quick inet proto tcp from { 192.168.128.0/24 } to 192.168.128.1 port 8893 flags S/SA keep state\n"
REPLAY_PROXY_PF_RULE = "pass in quick inet proto tcp from { 192.168.128.0/24 } to 192.168.128.1 port 8889 flags S/SA keep state\n"
FORGE_BROKER_PF_RULE = "pass in quick inet proto tcp from { 192.168.128.0/24 } to 192.168.128.1 port 8894 flags S/SA keep state\n"
STUDIO_PF_RULES = (
    "# Pi Durable SGG sandbox isolation: nix-configs modules/services/pi-durable.nix\n"
    "pass in quick inet proto tcp from { 192.168.128.0/24 } to 192.168.128.1 port 8888 flags S/SA keep state\n"
    "pass in quick inet proto tcp from { 192.168.128.0/24 } to 192.168.128.1 port 8892 flags S/SA keep state\n"
    + WRITE_PROXY_PF_RULE
    + REPLAY_PROXY_PF_RULE
    + FORGE_BROKER_PF_RULE +
    "block return in quick on ! lo0 inet from { 192.168.128.0/24 } to any\n"
    "block return in quick on ! lo0 inet6 from fd85:1c07:74fb:2dc7::/64 to any\n"
    f"block return in quick on {{ {' '.join(BRIDGES)} }} inet6 proto {{ tcp udp }} from fe80::/10 to any\n"
)
# The sandbox anchor before planning on VMs (durable-pi-workspace#48): with the
# forge broker, the VM broker and planning disabled it is exactly this.
SANDBOX_PF_RULES_WITHOUT_PLANNING = STUDIO_PF_RULES.replace(FORGE_BROKER_PF_RULE, "")
# The planning service network's anchor (durable-pi-workspace#48): the network
# reaches the forge broker (8894) and the VM broker (8895) on its gateway and
# nothing else.
PLANNING_PF_ANCHOR = "com.apple/101.pi-durable-planning"
PLANNING_PF_RULES = (
    "# Pi Durable planning service network isolation: nix-configs modules/services/pi-durable.nix\n"
    "pass in quick inet proto tcp from { 192.168.129.0/24 } to 192.168.129.1 port 8894 flags S/SA keep state\n"
    "pass in quick inet proto tcp from { 192.168.129.0/24 } to 192.168.129.1 port 8895 flags S/SA keep state\n"
    "block return in quick on ! lo0 inet from { 192.168.129.0/24 } to any\n"
    "block return in quick on ! lo0 inet6 from fdd2:4a0c:729f:fb6::/64 to any\n"
    f"block return in quick on {{ {' '.join(BRIDGES)} }} inet6 proto {{ tcp udp }} from fe80::/10 to any\n"
)


def nix(*args):
    result = subprocess.run(
        ["nix", *args], cwd=REPO, text=True, capture_output=True
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr[-4000:])
    return result.stdout


def studio_expr(settings, expr):
    """`expr` with `config` and `pkgs` bound to Studio plus `services.pi-durable = settings`."""
    return f"""
      let
        studio = (builtins.getFlake "path:{REPO}").darwinConfigurations.studio;
        pkgs = studio.pkgs;
        inherit (pkgs) lib;
        config = (studio.extendModules {{
          modules = [ {{ services.pi-durable = {settings}; }} ];
        }}).config;
      in {expr}
    """


@cache
def studio(settings, expr):
    return json.loads(nix("eval", "--json", "--impure", "--expr", studio_expr(settings, expr)))


FAILED = "map (a: a.message) (builtins.filter (a: !a.assertion) config.assertions)"
SUMMARY = f"""{{
  enable = config.services.pi-durable.enable;
  agents = builtins.attrNames config.launchd.user.agents;
  system = builtins.attrNames config.launchd.agents ++ builtins.attrNames config.launchd.daemons;
  brews = map (b: b.name or b) config.homebrew.brews;
  casks = map (c: c.name or c) config.homebrew.casks;
  packages = map (p: p.name or "") config.environment.systemPackages;
  activation = config.system.activationScripts.extraActivation.text
    + config.system.activationScripts.postActivation.text;
  failed = {FAILED};
}}"""
PF_DAEMON = "config.launchd.daemons.pi-durable-pf.serviceConfig"
PF_RULES = f'(builtins.head (builtins.filter (lib.hasSuffix "-pi-durable-sgg.pf") {PF_DAEMON}.ProgramArguments))'
PLANNING_PF_DAEMON = "config.launchd.daemons.pi-durable-planning-pf.serviceConfig"
PLANNING_PF_RULES_FILE = f'(builtins.head (builtins.filter (lib.hasSuffix "-pi-durable-planning.pf") {PLANNING_PF_DAEMON}.ProgramArguments))'
# Planning on VMs off: the forge broker, the VM broker and the planning service.
NO_PLANNING = "forgeBroker.enable = lib.mkForce false; vmBroker.enable = lib.mkForce false; planning.enable = lib.mkForce false;"
# Studio sets the commissioning inputs, so a scenario forces the ones it is about.
ENABLED = (
    "{ enable = lib.mkForce true; proxy = { network = lib.mkForce null; listenAddress = lib.mkForce null; allowedClients = lib.mkForce [ ]; }; "
    "page.tailscaleHttpsPort = lib.mkForce null; sampler.enable = lib.mkForce false; modelProxy.enable = lib.mkForce false; "
    f"githubProxy.enable = lib.mkForce false; replayProxy.enable = lib.mkForce false; {NO_PLANNING} }}"
)
PROXY = (
    '{ enable = lib.mkForce true; proxy = { listenAddress = lib.mkForce "192.168.128.1"; '
    'allowedClients = lib.mkForce [ "192.168.128.0/24" ]; }; }'
)


class StudioAsCommissioned(unittest.TestCase):
    """Studio runs the paused agent, its proxy on the sandbox network's gateway and the page route, and installs nothing."""

    def test_enabled_with_its_commissioning_inputs(self):
        s = studio("{ }", SUMMARY)
        self.assertTrue(s["enable"])
        self.assertEqual([], s["failed"])
        self.assertIn("pi-durable-sgg", s["agents"])
        self.assertIn("pi-durable-proxy", s["agents"])
        self.assertIn("pi-durable-model-proxy", s["agents"])
        self.assertIn("pi-durable-github-proxy", s["agents"])
        self.assertIn("pi-durable-replay-proxy", s["agents"])
        for agent in ("pi-durable-forge-broker", "pi-durable-vm-broker", "pi-durable-planning-service", "pi-durable-planning-relay"):
            self.assertIn(agent, s["agents"])
        # The only root jobs: the boot-time pf loaders, one per anchor.
        self.assertEqual(["pi-durable-pf", "pi-durable-planning-pf"], [name for name in s["system"] if "pi-durable" in name])
        self.assertIn(f"/sbin/pfctl {PLANNING_PF_ANCHOR} ", s["activation"])
        self.assertIn("8792 http://127.0.0.1:8792", s["activation"])
        self.assertIn(f"/sbin/pfctl {PF_ANCHOR} ", s["activation"])
        # Apple container is installed by hand from its signed installer; nothing here installs it or tinyproxy.
        for item in s["brews"] + s["casks"] + s["packages"]:
            self.assertNotIn("tinyproxy", item)
            self.assertNotIn("container", item)
        self.assertIn("8790 http://127.0.0.1:8790", s["activation"])
        self.assertIn("pi-durable-sampler", s["agents"])
        self.assertIn("8791 http://127.0.0.1:8791", s["activation"])
        proxy_config = studio("{ }", "builtins.readFile (builtins.elemAt config.launchd.user.agents.pi-durable-proxy.serviceConfig.ProgramArguments 3)")
        self.assertIn("Listen 192.168.128.1\n", proxy_config)
        self.assertIn("Allow 192.168.128.0/24\n", proxy_config)
        self.assertEqual(STUDIO_PF_RULES, studio("{ }", f"builtins.readFile {PF_RULES}"))
        self.assertEqual(PLANNING_PF_RULES, studio("{ }", f"builtins.readFile {PLANNING_PF_RULES_FILE}"))


class EnabledWithoutCommissioningInputs(unittest.TestCase):
    """Enabled alone: the paused workspace agent, and no proxy, firewall hole or route."""

    def test_agent_runs_the_contract_command_without_secrets(self):
        agent = studio(ENABLED, "config.launchd.user.agents.pi-durable-sgg.serviceConfig")
        self.assertEqual(
            [
                "/opt/homebrew/bin/node",
                f"{HOME}/.local/share/pi-durable/sgg-deployment/dist/workspaces/sgg/src/main.js",
                "run",
                "--config",
                f"{HOME}/.config/pi-durable/sgg.json",
            ],
            agent["ProgramArguments"],
        )
        # No secret reaches the plist: the service reads its 0600 secrets file itself.
        self.assertEqual({"HOME", "PATH"}, set(agent["EnvironmentVariables"]))
        path = agent["EnvironmentVariables"]["PATH"].split(":")
        for directory in (f"{HOME}/.local/bin", "/run/current-system/sw/bin", "/opt/homebrew/bin", "/usr/local/bin"):
            self.assertIn(directory, path)
        self.assertTrue(agent["RunAtLoad"])
        self.assertTrue(agent["KeepAlive"])
        self.assertGreaterEqual(agent["ExitTimeOut"], 30)
        self.assertEqual("/tmp/pi-durable-sgg.log", agent["StandardOutPath"])
        self.assertEqual("/tmp/pi-durable-sgg.error.log", agent["StandardErrorPath"])

    def test_no_proxy_firewall_or_route_until_their_inputs_exist(self):
        s = studio(ENABLED, SUMMARY)
        self.assertEqual([], s["failed"])
        self.assertIn("pi-durable-sgg", s["agents"])
        self.assertNotIn("pi-durable-proxy", s["agents"])
        self.assertNotIn("pi-durable-model-proxy", s["agents"])
        self.assertNotIn("pi-durable-github-proxy", s["agents"])
        self.assertNotIn("pi-durable-replay-proxy", s["agents"])
        self.assertEqual([], [name for name in s["agents"] if "net-anchor" in name])
        self.assertNotIn(NODE_FIREWALL, s["activation"])
        self.assertEqual([], [name for name in s["system"] if "pi-durable" in name])
        self.assertNotIn("tinyproxy", s["activation"])
        self.assertNotIn("pfctl", s["activation"])
        self.assertNotIn("pi-durable-page-route", s["activation"])


def failures(*settings):
    """Failed assertion messages for each `services.pi-durable` setting, in one evaluation."""
    each = " ".join(f"({s})" for s in settings)
    return json.loads(nix("eval", "--json", "--impure", "--expr", f"""
      let
        studio = (builtins.getFlake "path:{REPO}").darwinConfigurations.studio;
        inherit (studio.pkgs) lib;
        failed = settings: let config = (studio.extendModules {{
          modules = [ {{ services.pi-durable = settings; }} ];
        }}).config; in {FAILED};
      in map failed [ {each} ]
    """))


def proxy(listen='"192.168.128.1"', clients='[ "192.168.128.0/24" ]', ipv6='"fd85:1c07:74fb:2dc7::/64"'):
    return (
        f"{{ enable = lib.mkForce true; proxy = {{ listenAddress = lib.mkForce {listen}; "
        f"allowedClients = lib.mkForce {clients}; ipv6Subnet = lib.mkForce {ipv6}; }}; }}"
    )


# Address cases as Nix source, accepted and refused; the planning service
# network's addresses take the same ones (Planning, below).
LISTEN_REFUSED = (
    '"0.0.0.0"', '"999.1.1.1"', '"192.168.128.256"', '"192.168.128.01"', '"192.168.128"',
    '"192.168.128.1 "', r'"192.168.128.1\nListen 0.0.0.0"', '"studio.local"', '"::"', '""',
)
CLIENTS_ACCEPTED = (
    '[ "192.168.128.0/24" ]',
    '[ "192.168.128.0/24" "192.168.128.5" ]',
    '[ "10.0.0.0/8" "192.168.128.7/32" ]',
)
CLIENTS_REFUSED = (
    "[ ]", '[ "0.0.0.0/0" ]', '[ "0.0.0.0/00" ]', '[ "0.0.0.1/0" ]', '[ "192.168.128.0/33" ]',
    '[ "192.168.128.0/024" ]', '[ "256.168.128.0/24" ]', '[ "010.0.0.0/8" ]', '[ "192.168.128.0/" ]',
    r'[ "192.168.128.0/24\nConnectPort 22" ]', '[ "192.168.128.0/24 0.0.0.0/0" ]',
    '[ " 192.168.128.0/24" ]', '[ "192.168.128.0/24" "0.0.0.0" ]', '[ "::/0" ]', '[ "localhost" ]', '[ "" ]',
)
IPV6_ACCEPTED = ('"fd85:1c07:74fb:2dc7::/64"', '"fd00::/8"', '"fd85:1c07:74fb:2dc7:0:0:0:0/128"')
IPV6_REFUSED = (
    "null", '""', '"fd85:1c07:74fb:2dc7::"', '"fd85:1c07:74fb:2dc7::/0"', '"fd85:1c07:74fb:2dc7::/129"',
    '"fd85:1c07:74fb:2dc7::/064"', '"FD85:1C07:74FB:2DC7::/64"', '"192.168.128.0/24"', '"fd85::/64 to any"',
    r'"fd85::/64\npass quick all"', '"fd85::/64 }"', '"{ fd85::/64 }"', '"any"',
)


class FailClosedAssertions(unittest.TestCase):
    """Values are Nix source, so `\\n` reaches the module as a real newline.

    These validate configuration syntax only, not the VM's network isolation.
    """

    def assertOnlyRefuses(self, field, accepted, refused):
        results = failures(*accepted, *refused)
        for settings, failed in zip(accepted, results):
            with self.subTest(accepted=settings):
                self.assertEqual([], failed)
        for settings, failed in zip(refused, results[len(accepted):]):
            with self.subTest(refused=settings):
                self.assertTrue(any(field in message for message in failed), failed)

    def test_listen_address_is_one_canonical_ipv4_address(self):
        self.assertOnlyRefuses(
            "proxy.listenAddress",
            [proxy()],
            [proxy(listen=value) for value in LISTEN_REFUSED],
        )

    def test_clients_are_canonical_ipv4_addresses_or_cidrs(self):
        self.assertOnlyRefuses(
            "proxy.allowedClients",
            [proxy(clients=clients) for clients in CLIENTS_ACCEPTED],
            [proxy(clients=clients) for clients in CLIENTS_REFUSED],
        )

    def test_ipv6_subnet_is_required_lowercase_hex_with_a_prefix(self):
        self.assertOnlyRefuses(
            "proxy.ipv6Subnet",
            [proxy(ipv6=value) for value in IPV6_ACCEPTED],
            [proxy(ipv6=value) for value in IPV6_REFUSED],
        )

    def test_allowlist_takes_exact_hostnames_only(self):
        self.assertOnlyRefuses(
            "proxy.allowedDomains",
            [proxy()],
            [f"{{ enable = lib.mkForce true; proxy.allowedDomains = [ {domain} ]; }}" for domain in (
                '"*.github.com"', '"GitHub.com"', r'"github.com\nConnectPort 22"', '"github.com evil.example"',
            )],
        )

    def test_page_route_requires_tailscale(self):
        failed = json.loads(nix("eval", "--json", "--impure", "--expr", f"""
          let
            studio = (builtins.getFlake "path:{REPO}").darwinConfigurations.studio;
            inherit (studio.pkgs) lib;
            config = (studio.extendModules {{ modules = [ {{
              services.pi-durable = {{ enable = lib.mkForce true; page.tailscaleHttpsPort = lib.mkForce 8443; }};
              services.tailscale.enable = lib.mkForce false;
            }} ]; }}).config;
          in {FAILED}
        """))
        self.assertTrue(any("services.tailscale" in m for m in failed), failed)


class CommissionedWiring(unittest.TestCase):
    def test_proxy_agent_and_firewall_registration(self):
        s = studio(PROXY, SUMMARY)
        self.assertEqual([], s["failed"])
        self.assertIn("pi-durable-proxy", s["agents"])
        self.assertIn("socketfilterfw --unblock", s["activation"])
        self.assertIn("tinyproxy", s["activation"])

    def test_page_route_goes_through_the_guard(self):
        activation = studio(
            "{ enable = lib.mkForce true; page.tailscaleHttpsPort = lib.mkForce 8443; }",
            "config.system.activationScripts.extraActivation.text",
        )
        self.assertIn("pi-durable-page-route.sh", activation)
        self.assertIn("8443 http://127.0.0.1:8790", activation)


class ModelProxy(unittest.TestCase):
    """The model-provider proxy (#268): Nix's Node on the gateway, its firewall exception and pf port."""

    def test_agent_runs_the_contract_command_on_nix_node_without_secrets(self):
        agent = studio("{ }", "config.launchd.user.agents.pi-durable-model-proxy.serviceConfig")
        node = agent["ProgramArguments"][0]
        self.assertTrue(node.startswith("/nix/store/") and node.endswith("/bin/node"), node)
        self.assertNotIn("homebrew", node)
        self.assertEqual(
            [
                f"{HOME}/.local/share/pi-durable/sgg-deployment/dist/workspaces/proxies/src/model-main.js",
                "run",
                "--config",
                f"{HOME}/.config/pi-durable/model-proxy.json",
            ],
            agent["ProgramArguments"][1:],
        )
        # HOME only: no PATH that could find Homebrew's node, and no secret.
        self.assertEqual({"HOME": HOME}, agent["EnvironmentVariables"])
        self.assertTrue(agent["RunAtLoad"])
        self.assertTrue(agent["KeepAlive"])
        self.assertEqual("/tmp/pi-durable-model-proxy.log", agent["StandardOutPath"])
        self.assertEqual("/tmp/pi-durable-model-proxy.error.log", agent["StandardErrorPath"])
        # The firewall exception names exactly that binary, which is a file, not a symlink.
        activation = studio("{ }", "config.system.activationScripts.extraActivation.text")
        self.assertIn(f"socketfilterfw --add {node} ", activation)
        self.assertIn(f"socketfilterfw --unblock {node} ", activation)
        self.assertNotRegex(activation, r"socketfilterfw --add /opt/homebrew/\S*node")

    def test_disabled_declares_no_agent_or_pf_port(self):
        s = studio("{ modelProxy.enable = lib.mkForce false; }", SUMMARY)
        self.assertEqual([], s["failed"])
        self.assertNotIn("pi-durable-model-proxy", s["agents"])
        # The Node exception stays for the write proxy (GithubProxy covers it).
        rules = studio("{ modelProxy.enable = lib.mkForce false; }", f"builtins.readFile {PF_RULES}")
        self.assertEqual(STUDIO_PF_RULES.replace(STUDIO_PF_RULES.splitlines(True)[2], ""), rules)

    def test_port_follows_the_option_into_pf(self):
        rules = studio("{ modelProxy.port = lib.mkForce 9892; }", f"builtins.readFile {PF_RULES}")
        self.assertIn("to 192.168.128.1 port 9892 flags S/SA keep state\n", rules)
        self.assertNotIn("port 8892", rules)

    def test_refuses_without_the_sandbox_proxy_or_on_a_clashing_port(self):
        accepted, no_listen, disabled, tiny, page, sampler = failures(
            "{ }",
            "{ proxy.listenAddress = lib.mkForce null; }",
            "{ enable = lib.mkForce false; }",
            "{ modelProxy.port = lib.mkForce 8888; }",
            "{ modelProxy.port = lib.mkForce 8790; }",
            "{ modelProxy.port = lib.mkForce 8791; }",
        )
        self.assertEqual([], accepted)
        for failed in (no_listen, disabled):
            self.assertTrue(any("modelProxy requires" in m for m in failed), failed)
        self.assertTrue(any("modelProxy.port must differ from services.pi-durable.proxy.port" in m for m in tiny), tiny)
        for failed in (page, sampler):
            self.assertTrue(any("modelProxy.port must differ from the pages' ports" in m for m in failed), failed)


NODE_FIREWALL = "# === pi-durable proxies' Node firewall ==="


class GithubProxy(unittest.TestCase):
    """The GitHub write proxy (#268): Nix's Node, Nix's git and openssh, its gateway port in pf, the shared exception."""

    def test_agent_runs_the_contract_command_with_only_nix_git_and_openssh_on_path(self):
        agent = studio("{ }", "config.launchd.user.agents.pi-durable-github-proxy.serviceConfig")
        node = agent["ProgramArguments"][0]
        self.assertTrue(node.startswith("/nix/store/") and node.endswith("/bin/node"), node)
        self.assertEqual(node, studio("{ }", "builtins.head config.launchd.user.agents.pi-durable-model-proxy.serviceConfig.ProgramArguments"))
        self.assertEqual(
            [
                f"{HOME}/.local/share/pi-durable/sgg-deployment/dist/workspaces/proxies/src/github-main.js",
                "run",
                "--config",
                f"{HOME}/.config/pi-durable/github-proxy.json",
            ],
            agent["ProgramArguments"][1:],
        )
        # HOME and PATH only: no secret. PATH is Nix's git and openssh, nothing
        # else (no Homebrew, /usr/local or system directory).
        self.assertEqual({"HOME", "PATH"}, set(agent["EnvironmentVariables"]))
        self.assertEqual(HOME, agent["EnvironmentVariables"]["HOME"])
        path = agent["EnvironmentVariables"]["PATH"].split(":")
        self.assertEqual(2, len(path), path)
        self.assertRegex(path[0], r"^/nix/store/[0-9a-z]{32}-git-[0-9.]+/bin$")
        self.assertRegex(path[1], r"^/nix/store/[0-9a-z]{32}-openssh-[0-9.p]+/bin$")
        self.assertEqual(
            path,
            studio("{ }", 'map (p: "${p}/bin") [ pkgs.git pkgs.openssh ]'),
        )
        self.assertTrue(agent["RunAtLoad"])
        self.assertTrue(agent["KeepAlive"])
        self.assertEqual("/tmp/pi-durable-github-proxy.log", agent["StandardOutPath"])
        self.assertEqual("/tmp/pi-durable-github-proxy.error.log", agent["StandardErrorPath"])

    def node_firewall(self, settings):
        activation = studio(settings, "config.system.activationScripts.extraActivation.text")
        node = studio("{ }", "builtins.head config.launchd.user.agents.pi-durable-github-proxy.serviceConfig.ProgramArguments")
        return activation, node

    def test_one_firewall_exception_for_the_shared_node_while_either_proxy_runs(self):
        for name, settings in {
            "both": "{ }",
            "write proxy only": "{ modelProxy.enable = lib.mkForce false; }",
            "model proxy only": "{ githubProxy.enable = lib.mkForce false; }",
        }.items():
            with self.subTest(name):
                activation, node = self.node_firewall(settings)
                self.assertEqual(1, activation.count(NODE_FIREWALL))
                self.assertEqual(1, activation.count(f"socketfilterfw --add {node} "))
                self.assertEqual(1, activation.count(f"socketfilterfw --unblock {node} "))
                self.assertNotRegex(activation, r"socketfilterfw --add /opt/homebrew/\S*node")
        activation, node = self.node_firewall(f"{{ modelProxy.enable = lib.mkForce false; githubProxy.enable = lib.mkForce false; {NO_PLANNING} }}")
        self.assertNotIn(NODE_FIREWALL, activation)
        self.assertNotIn(node, activation)

    def test_disabled_leaves_todays_anchor_byte_identical(self):
        s = studio("{ githubProxy.enable = lib.mkForce false; }", SUMMARY)
        self.assertEqual([], s["failed"])
        self.assertNotIn("pi-durable-github-proxy", s["agents"])
        rules = studio("{ githubProxy.enable = lib.mkForce false; }", f"builtins.readFile {PF_RULES}")
        self.assertEqual(STUDIO_PF_RULES.replace(WRITE_PROXY_PF_RULE, ""), rules)
        self.assertNotIn("8893", rules)

    def test_port_follows_the_option_into_pf_alone(self):
        rules = studio("{ githubProxy.port = lib.mkForce 9893; }", f"builtins.readFile {PF_RULES}")
        self.assertEqual(STUDIO_PF_RULES.replace("port 8893 ", "port 9893 "), rules)
        # Only the write proxy: the anchor passes its port alone beside tinyproxy's.
        rules = studio("{ modelProxy.enable = lib.mkForce false; }", f"builtins.readFile {PF_RULES}")
        self.assertEqual(STUDIO_PF_RULES.replace(STUDIO_PF_RULES.splitlines(True)[2], ""), rules)

    def test_refuses_without_the_sandbox_proxy_or_on_a_clashing_port(self):
        accepted, model_off_same_port, no_listen, disabled, tiny, model, page, sampler = failures(
            "{ }",
            "{ modelProxy.enable = lib.mkForce false; githubProxy.port = lib.mkForce 8892; }",
            "{ proxy.listenAddress = lib.mkForce null; modelProxy.enable = lib.mkForce false; }",
            "{ enable = lib.mkForce false; modelProxy.enable = lib.mkForce false; }",
            "{ githubProxy.port = lib.mkForce 8888; }",
            "{ githubProxy.port = lib.mkForce 8892; }",
            "{ githubProxy.port = lib.mkForce 8790; }",
            "{ githubProxy.port = lib.mkForce 8791; }",
        )
        self.assertEqual([], accepted)
        self.assertEqual([], model_off_same_port)
        for failed in (no_listen, disabled):
            self.assertTrue(any("githubProxy requires" in m for m in failed), failed)
        for failed in (tiny, model):
            self.assertTrue(any("githubProxy.port must differ from services.pi-durable.proxy.port" in m for m in failed), failed)
        for failed in (page, sampler):
            self.assertTrue(any("githubProxy.port must differ from the pages' ports" in m for m in failed), failed)


TINYPROXY_FIREWALL = "# === pi-durable proxy firewall ==="
PACKAGE_HOSTS = ["registry.npmjs.org", "pypi.org", "files.pythonhosted.org"]


class ReplayProxy(unittest.TestCase):
    """The replay proxy (durable-pi-workspace#21): a second tinyproxy on the gateway, package hosts only, never GitHub."""

    AGENT = "config.launchd.user.agents.pi-durable-replay-proxy.serviceConfig"
    SANDBOX = "config.launchd.user.agents.pi-durable-proxy.serviceConfig"

    def test_agent_runs_the_nix_tinyproxy_like_the_sandbox_proxy(self):
        agent = studio("{ }", self.AGENT)
        sandbox = studio("{ }", self.SANDBOX)
        tinyproxy = agent["ProgramArguments"][0]
        self.assertRegex(tinyproxy, r"^/nix/store/[0-9a-z]{32}-tinyproxy-[0-9.]+/bin/tinyproxy$")
        self.assertEqual(sandbox["ProgramArguments"][0], tinyproxy)
        self.assertEqual(["-d", "-c"], agent["ProgramArguments"][1:3])
        self.assertTrue(agent["ProgramArguments"][3].endswith("-pi-durable-replay-tinyproxy.conf"))
        self.assertEqual(4, len(agent["ProgramArguments"]))
        # No environment beyond the sandbox proxy's (none): no secret, nothing to find.
        self.assertEqual(sandbox.get("EnvironmentVariables"), agent.get("EnvironmentVariables"))
        self.assertTrue(agent["RunAtLoad"])
        self.assertTrue(agent["KeepAlive"])
        self.assertEqual("/tmp/pi-durable-replay-proxy.log", agent["StandardOutPath"])
        self.assertEqual("/tmp/pi-durable-replay-proxy.error.log", agent["StandardErrorPath"])

    def test_config_is_the_sandbox_proxys_listener_with_its_own_port_and_package_hosts_only(self):
        config = studio("{ }", f"builtins.readFile (builtins.elemAt {self.AGENT}.ProgramArguments 3)")
        sandbox = studio("{ }", f"builtins.readFile (builtins.elemAt {self.SANDBOX}.ProgramArguments 3)")
        lines = config.splitlines()
        def directives(text, name):
            return [line for line in text.splitlines() if line.startswith(name + " ")]
        self.assertEqual(directives(sandbox, "Listen"), directives(config, "Listen"))
        self.assertEqual(["Listen 192.168.128.1"], directives(config, "Listen"))
        self.assertEqual(directives(sandbox, "Allow"), directives(config, "Allow"))
        self.assertEqual(["Allow 192.168.128.0/24"], directives(config, "Allow"))
        for line in ("Port 8889", "ConnectPort 443", "FilterType fnmatch", "FilterDefaultDeny Yes"):
            self.assertEqual(1, lines.count(line), line)
        self.assertEqual([], directives(config, "Port")[1:])
        [filter_line] = directives(config, "Filter")
        path = filter_line.split('"')[1]
        self.assertTrue(path.endswith("-pi-durable-replay-proxy-allowlist"), path)
        self.assertEqual(PACKAGE_HOSTS, studio("{ }", f'lib.splitString "\\n" (lib.removeSuffix "\\n" (builtins.readFile "{path}"))'))

    def test_disabled_leaves_the_anchor_byte_identical_and_declares_no_agent(self):
        off = "{ replayProxy.enable = lib.mkForce false; }"
        s = studio(off, SUMMARY)
        self.assertEqual([], s["failed"])
        self.assertNotIn("pi-durable-replay-proxy", s["agents"])
        self.assertIn("pi-durable-proxy", s["agents"])
        rules = studio(off, f"builtins.readFile {PF_RULES}")
        self.assertEqual(STUDIO_PF_RULES.replace(REPLAY_PROXY_PF_RULE, ""), rules)
        self.assertNotIn("8889", rules)

    def test_port_follows_the_option_into_pf(self):
        rules = studio("{ replayProxy.port = lib.mkForce 9889; }", f"builtins.readFile {PF_RULES}")
        self.assertEqual(STUDIO_PF_RULES.replace("port 8889 ", "port 9889 "), rules)
        config = studio("{ replayProxy.port = lib.mkForce 9889; }", f"builtins.readFile (builtins.elemAt {self.AGENT}.ProgramArguments 3)")
        self.assertIn("Port 9889\n", config)

    def test_refuses_github_hosts_inexact_names_clashing_ports_and_a_missing_sandbox_proxy(self):
        github = ["github.com", "api.github.com", "raw.githubusercontent.com", "objects.githubusercontent.com"]
        inexact = ["*.npmjs.org", "Registry.npmjs.org"]
        clashing = [8888, 8892, 8893]
        results = failures(
            "{ }",
            *[f'{{ replayProxy.allowedDomains = lib.mkForce [ "registry.npmjs.org" "{d}" ]; }}' for d in github + inexact],
            *[f"{{ replayProxy.port = lib.mkForce {port}; }}" for port in clashing],
            "{ proxy.listenAddress = lib.mkForce null; modelProxy.enable = lib.mkForce false; githubProxy.enable = lib.mkForce false; }",
            "{ enable = lib.mkForce false; modelProxy.enable = lib.mkForce false; githubProxy.enable = lib.mkForce false; }",
        )
        self.assertEqual([], results[0])
        results = iter(results[1:])
        for domain in github:
            with self.subTest(domain=domain):
                failed = next(results)
                self.assertTrue(any("replayProxy.allowedDomains must hold no GitHub host" in m for m in failed), failed)
        for domain in inexact:
            with self.subTest(domain=domain):
                failed = next(results)
                self.assertTrue(any("replayProxy.allowedDomains takes exact lowercase hostnames" in m for m in failed), failed)
        for port in clashing:
            with self.subTest(port=port):
                failed = next(results)
                self.assertTrue(any("replayProxy.port must differ" in m for m in failed), failed)
        for name in ("no listenAddress", "not enabled"):
            with self.subTest(name):
                failed = next(results)
                self.assertTrue(any("replayProxy requires" in m for m in failed), failed)

    def test_one_firewall_exception_for_the_shared_tinyproxy(self):
        tinyproxy = studio("{ }", f"builtins.head {self.AGENT}.ProgramArguments")
        for name, settings in {
            "with the replay proxy": "{ }",
            "without it": "{ replayProxy.enable = lib.mkForce false; }",
        }.items():
            with self.subTest(name):
                activation = studio(settings, "config.system.activationScripts.extraActivation.text")
                self.assertEqual(1, activation.count(TINYPROXY_FIREWALL))
                self.assertEqual(1, activation.count(f"socketfilterfw --add {tinyproxy} "))
                self.assertEqual(1, activation.count(f"socketfilterfw --unblock {tinyproxy} "))


SAMPLER_ONLY = (
    "{ enable = lib.mkForce false; modelProxy.enable = lib.mkForce false; githubProxy.enable = lib.mkForce false; "
    f"replayProxy.enable = lib.mkForce false; {NO_PLANNING} "
    "sampler = { enable = lib.mkForce true; tailscaleHttpsPort = lib.mkForce null; }; }"
)


class Sampler(unittest.TestCase):
    """The shared usage sampler: its own agent and route, independent of the SGG agent."""

    def test_agent_runs_the_contract_command_without_secrets(self):
        agent = studio(SAMPLER_ONLY, "config.launchd.user.agents.pi-durable-sampler.serviceConfig")
        self.assertEqual(
            [
                "/opt/homebrew/bin/node",
                f"{HOME}/.local/share/pi-durable/sgg-deployment/dist/workspaces/sampler/src/main.js",
                "run",
                "--config",
                f"{HOME}/.config/pi-durable/sampler.json",
            ],
            agent["ProgramArguments"],
        )
        self.assertEqual({"HOME", "PATH"}, set(agent["EnvironmentVariables"]))
        sgg = studio(ENABLED, "config.launchd.user.agents.pi-durable-sgg.serviceConfig.EnvironmentVariables")
        self.assertEqual(sgg, agent["EnvironmentVariables"])
        self.assertTrue(agent["RunAtLoad"])
        self.assertTrue(agent["KeepAlive"])
        self.assertEqual("/tmp/pi-durable-sampler.log", agent["StandardOutPath"])
        self.assertEqual("/tmp/pi-durable-sampler.error.log", agent["StandardErrorPath"])

    def test_runs_without_the_sgg_agent_and_routes_nothing_until_a_port_is_set(self):
        s = studio(SAMPLER_ONLY, SUMMARY)
        self.assertFalse(s["enable"])
        self.assertEqual([], s["failed"])
        self.assertEqual(["pi-durable-sampler"], [name for name in s["agents"] if "pi-durable" in name])
        self.assertEqual([], [name for name in s["system"] if "pi-durable" in name])
        self.assertNotIn("pi-durable-page-route", s["activation"])

    def test_disabled_declares_nothing(self):
        s = studio("{ sampler.enable = lib.mkForce false; }", SUMMARY)
        self.assertEqual([], s["failed"])
        self.assertNotIn("pi-durable-sampler", s["agents"])
        self.assertNotIn("http://127.0.0.1:8791", s["activation"])
        self.assertIn("8790 http://127.0.0.1:8790", s["activation"])

    def test_route_goes_through_the_guard_on_its_own_port(self):
        activation = studio(
            "{ sampler = { enable = lib.mkForce true; port = lib.mkForce 8793; tailscaleHttpsPort = lib.mkForce 8444; }; planning.tailscaleHttpsPort = lib.mkForce null; }",
            "config.system.activationScripts.extraActivation.text",
        )
        self.assertEqual(2, activation.count("pi-durable-page-route.sh"))
        self.assertIn("8444 http://127.0.0.1:8793", activation)
        self.assertIn("8790 http://127.0.0.1:8790", activation)

    def test_refuses_the_sgg_page_ports_and_a_route_without_tailscale(self):
        accepted, same_port, same_route = failures(
            "{ }",
            "{ sampler.port = lib.mkForce 8790; }",
            "{ sampler.tailscaleHttpsPort = lib.mkForce 8790; }",
        )
        self.assertEqual([], accepted)
        self.assertTrue(any("sampler.port" in m for m in same_port), same_port)
        self.assertTrue(any("sampler.tailscaleHttpsPort" in m for m in same_route), same_route)
        # Without the SGG agent, its ports are not the sampler's concern.
        self.assertEqual([], failures(f"{{ enable = lib.mkForce false; modelProxy.enable = lib.mkForce false; githubProxy.enable = lib.mkForce false; replayProxy.enable = lib.mkForce false; {NO_PLANNING} sampler.port = lib.mkForce 8790; }}")[0])
        failed = json.loads(nix("eval", "--json", "--impure", "--expr", f"""
          let
            studio = (builtins.getFlake "path:{REPO}").darwinConfigurations.studio;
            inherit (studio.pkgs) lib;
            config = (studio.extendModules {{ modules = [ {{
              services.pi-durable = {{ enable = lib.mkForce false; sampler.tailscaleHttpsPort = lib.mkForce 8791; }};
              services.tailscale.enable = lib.mkForce false;
            }} ]; }}).config;
          in {FAILED}
        """))
        self.assertTrue(any("sampler.tailscaleHttpsPort requires services.tailscale" in m for m in failed), failed)


DEPLOYMENT = f"{HOME}/.local/share/pi-durable/sgg-deployment"
SYSTEM_PATH = "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
AGENTS = "config.launchd.user.agents"


def planning_network(value):
    """A sandbox-network address case moved onto the planning service network's addresses."""
    return value.replace("192.168.128", "192.168.129").replace("fd85:1c07:74fb:2dc7", "fdd2:4a0c:729f:fb6")


def planning(listen='"192.168.129.1"', clients='[ "192.168.129.0/24" ]', ipv6='"fdd2:4a0c:729f:fb6::/64"'):
    return (
        f"{{ planning = {{ listenAddress = lib.mkForce {listen}; "
        f"allowedClients = lib.mkForce {clients}; ipv6Subnet = lib.mkForce {ipv6}; }}; }}"
    )


class PlanningOff(unittest.TestCase):
    """With the forge broker, the VM broker and planning disabled, everything is as before durable-pi-workspace#48."""

    OFF = f"{{ {NO_PLANNING} }}"

    def test_disabled_leaves_the_sandbox_anchor_byte_identical_and_declares_nothing_new(self):
        s = studio(self.OFF, SUMMARY)
        self.assertEqual([], s["failed"])
        for agent in ("pi-durable-forge-broker", "pi-durable-vm-broker", "pi-durable-planning-service", "pi-durable-planning-relay"):
            self.assertNotIn(agent, s["agents"])
        self.assertEqual(["pi-durable-pf"], [name for name in s["system"] if "pi-durable" in name])
        self.assertEqual(SANDBOX_PF_RULES_WITHOUT_PLANNING, studio(self.OFF, f"builtins.readFile {PF_RULES}"))
        self.assertNotIn("8894", SANDBOX_PF_RULES_WITHOUT_PLANNING)
        self.assertNotIn(PLANNING_PF_ANCHOR, s["activation"])
        self.assertNotIn("planning", s["activation"])
        self.assertNotIn("http://127.0.0.1:8792", s["activation"])
        self.assertEqual(2, s["activation"].count("pi-durable-page-route.sh"))

    def test_enabled_adds_exactly_the_forge_brokers_line_to_the_sandbox_anchor(self):
        self.assertEqual(
            SANDBOX_PF_RULES_WITHOUT_PLANNING.splitlines(True),
            [line for line in STUDIO_PF_RULES.splitlines(True) if line != FORGE_BROKER_PF_RULE],
        )
        self.assertEqual(1, STUDIO_PF_RULES.count(FORGE_BROKER_PF_RULE))
        self.assertNotIn("8895", STUDIO_PF_RULES)


class ForgeBroker(unittest.TestCase):
    """The forge broker (durable-pi-workspace#48): Nix's Node, Nix's git and openssh, 8894 on both gateways."""

    AGENT = f"{AGENTS}.pi-durable-forge-broker.serviceConfig"

    def test_agent_runs_the_contract_command_with_only_nix_git_and_openssh_on_path(self):
        agent = studio("{ }", self.AGENT)
        node = studio("{ }", "builtins.head config.launchd.user.agents.pi-durable-model-proxy.serviceConfig.ProgramArguments")
        self.assertTrue(node.startswith("/nix/store/") and node.endswith("/bin/node"), node)
        self.assertEqual(
            [node, f"{DEPLOYMENT}/dist/workspaces/proxies/src/forge-main.js", "run", "--config", f"{HOME}/.config/pi-durable/forge-broker.json"],
            agent["ProgramArguments"],
        )
        # HOME and PATH only, no secret; PATH exactly the write proxy's.
        self.assertEqual({"HOME", "PATH"}, set(agent["EnvironmentVariables"]))
        self.assertEqual(HOME, agent["EnvironmentVariables"]["HOME"])
        self.assertEqual(
            ":".join(studio("{ }", 'map (p: "${p}/bin") [ pkgs.git pkgs.openssh ]')),
            agent["EnvironmentVariables"]["PATH"],
        )
        self.assertEqual(
            studio("{ }", f"{AGENTS}.pi-durable-github-proxy.serviceConfig.EnvironmentVariables"),
            agent["EnvironmentVariables"],
        )
        self.assertIs(True, agent["RunAtLoad"])
        self.assertIs(True, agent["KeepAlive"])
        self.assertIsNone(agent.get("ExitTimeOut"))
        self.assertEqual("/tmp/pi-durable-forge-broker.log", agent["StandardOutPath"])
        self.assertEqual("/tmp/pi-durable-forge-broker.error.log", agent["StandardErrorPath"])

    def test_port_follows_the_option_into_both_anchors(self):
        settings = "{ forgeBroker.port = lib.mkForce 9894; }"
        self.assertEqual(STUDIO_PF_RULES.replace("port 8894 ", "port 9894 "), studio(settings, f"builtins.readFile {PF_RULES}"))
        self.assertEqual(PLANNING_PF_RULES.replace("port 8894 ", "port 9894 "), studio(settings, f"builtins.readFile {PLANNING_PF_RULES_FILE}"))

    def test_refuses_without_the_sandbox_proxy_or_on_a_clashing_port(self):
        clashing = {8888: "proxy.port", 8892: "proxy.port", 8893: "proxy.port", 8889: "proxy.port",
                    8790: "pages' ports", 8791: "pages' ports", 8792: "pages' ports"}
        accepted, model_off_same_port, no_listen, disabled, *ports = failures(
            "{ }",
            "{ modelProxy.enable = lib.mkForce false; forgeBroker.port = lib.mkForce 8892; }",
            "{ proxy.listenAddress = lib.mkForce null; }",
            "{ enable = lib.mkForce false; }",
            *[f"{{ forgeBroker.port = lib.mkForce {port}; }}" for port in clashing],
        )
        self.assertEqual([], accepted)
        self.assertEqual([], model_off_same_port)
        for failed in (no_listen, disabled):
            self.assertTrue(any("forgeBroker requires services.pi-durable.enable and proxy.listenAddress" in m for m in failed), failed)
        for (port, what), failed in zip(clashing.items(), ports):
            with self.subTest(port=port):
                self.assertTrue(any(m.startswith("services.pi-durable.forgeBroker.port must differ") and what in m for m in failed), failed)


class VmBroker(unittest.TestCase):
    """The VM broker (durable-pi-workspace#48): 8895 on the planning gateway alone, never in the sandbox anchor."""

    AGENT = f"{AGENTS}.pi-durable-vm-broker.serviceConfig"

    def test_agent_runs_the_contract_command_with_nix_git_then_the_container_cli(self):
        agent = studio("{ }", self.AGENT)
        node = studio("{ }", "builtins.head config.launchd.user.agents.pi-durable-model-proxy.serviceConfig.ProgramArguments")
        self.assertEqual(
            [node, f"{DEPLOYMENT}/dist/workspaces/proxies/src/vm-broker-main.js", "run", "--config", f"{HOME}/.config/pi-durable/vm-broker.json"],
            agent["ProgramArguments"],
        )
        self.assertEqual({"HOME", "PATH"}, set(agent["EnvironmentVariables"]))
        self.assertEqual(HOME, agent["EnvironmentVariables"]["HOME"])
        [git] = studio("{ }", 'map (p: "${p}/bin") [ pkgs.git ]')
        self.assertRegex(git, r"^/nix/store/[0-9a-z]{32}-git-[0-9.]+/bin$")
        self.assertEqual(f"{git}:{SYSTEM_PATH}", agent["EnvironmentVariables"]["PATH"])
        self.assertIs(True, agent["RunAtLoad"])
        self.assertIs(True, agent["KeepAlive"])
        self.assertEqual(30, agent["ExitTimeOut"])
        self.assertEqual("/tmp/pi-durable-vm-broker.log", agent["StandardOutPath"])
        self.assertEqual("/tmp/pi-durable-vm-broker.error.log", agent["StandardErrorPath"])

    def test_port_follows_the_option_into_the_planning_anchor_alone(self):
        settings = "{ vmBroker.port = lib.mkForce 9895; }"
        self.assertEqual(STUDIO_PF_RULES, studio(settings, f"builtins.readFile {PF_RULES}"))
        self.assertEqual(PLANNING_PF_RULES.replace("port 8895 ", "port 9895 "), studio(settings, f"builtins.readFile {PLANNING_PF_RULES_FILE}"))

    def test_refuses_without_planning_on_a_page_port_or_in_the_sandbox_anchor(self):
        in_sandbox = [
            "{ vmBroker.port = lib.mkForce 8888; }",
            "{ vmBroker.port = lib.mkForce 8892; }",
            "{ vmBroker.port = lib.mkForce 8893; }",
            "{ vmBroker.port = lib.mkForce 8889; }",
            "{ vmBroker.port = lib.mkForce 8894; }",
            # Another proxy moved onto the VM broker's port would put it in the sandbox anchor.
            "{ modelProxy.port = lib.mkForce 8895; }",
            "{ forgeBroker.port = lib.mkForce 8895; }",
        ]
        pages = [f"{{ vmBroker.port = lib.mkForce {port}; }}" for port in (8790, 8791, 8792)]
        accepted, no_planning, *results = failures(
            "{ }",
            "{ planning.enable = lib.mkForce false; }",
            *in_sandbox,
            *pages,
        )
        self.assertEqual([], accepted)
        self.assertTrue(any("vmBroker requires services.pi-durable.planning" in m for m in no_planning), no_planning)
        for settings, failed in zip(in_sandbox, results):
            with self.subTest(settings):
                self.assertTrue(any("vmBroker.port must never be passed by the sandbox network's pf anchor" in m for m in failed), failed)
        for settings, failed in zip(pages, results[len(in_sandbox):]):
            with self.subTest(settings):
                self.assertTrue(any("vmBroker.port must differ from the pages' ports" in m for m in failed), failed)


class Planning(unittest.TestCase):
    """The planning service (durable-pi-workspace#48): its launcher, its relay, its pf anchor and its route."""

    LAUNCHER = f"{AGENTS}.pi-durable-planning-service.serviceConfig"
    RELAY = f"{AGENTS}.pi-durable-planning-relay.serviceConfig"

    def test_launcher_runs_the_contract_command_with_only_the_system_path(self):
        agent = studio("{ }", self.LAUNCHER)
        node = studio("{ }", "builtins.head config.launchd.user.agents.pi-durable-model-proxy.serviceConfig.ProgramArguments")
        self.assertEqual(
            [node, f"{DEPLOYMENT}/dist/workspaces/planning/src/service-vm.js", "run", "--config", f"{HOME}/.config/pi-durable/planning-service-vm.json"],
            agent["ProgramArguments"],
        )
        # The container CLI (/usr/local/bin) and nothing of Homebrew's or Nix's; no secret.
        self.assertEqual({"HOME": HOME, "PATH": SYSTEM_PATH}, agent["EnvironmentVariables"])
        self.assertIs(True, agent["RunAtLoad"])
        self.assertIs(True, agent["KeepAlive"])
        self.assertEqual(60, agent["ExitTimeOut"])
        self.assertEqual("/tmp/pi-durable-planning-service.log", agent["StandardOutPath"])
        self.assertEqual("/tmp/pi-durable-planning-service.error.log", agent["StandardErrorPath"])

    def test_relay_runs_the_contract_command_with_home_only(self):
        agent = studio("{ }", self.RELAY)
        node = studio("{ }", "builtins.head config.launchd.user.agents.pi-durable-model-proxy.serviceConfig.ProgramArguments")
        self.assertEqual(
            [node, f"{DEPLOYMENT}/dist/workspaces/proxies/src/relay-main.js", "run", "--config", f"{HOME}/.config/pi-durable/planning-relay.json"],
            agent["ProgramArguments"],
        )
        self.assertEqual({"HOME": HOME}, agent["EnvironmentVariables"])
        self.assertIs(True, agent["RunAtLoad"])
        self.assertIs(True, agent["KeepAlive"])
        self.assertIsNone(agent.get("ExitTimeOut"))
        self.assertEqual("/tmp/pi-durable-planning-relay.log", agent["StandardOutPath"])
        self.assertEqual("/tmp/pi-durable-planning-relay.error.log", agent["StandardErrorPath"])

    def test_anchor_passes_the_two_brokers_and_refuses_everything_else(self):
        self.assertEqual(PLANNING_PF_RULES, studio("{ }", f"builtins.readFile {PLANNING_PF_RULES_FILE}"))

    def test_this_macs_pfctl_parses_the_planning_anchor_as_intended(self):
        rules = nix(
            "build", "--impure", "--no-link", "--print-out-paths", "--expr",
            studio_expr("{ }", f'pkgs.writeText "pi-durable-planning.pf" (builtins.readFile {PLANNING_PF_RULES_FILE})'),
        ).strip().splitlines()[-1]
        parsed = subprocess.run(["/sbin/pfctl", "-nvf", rules], text=True, capture_output=True)
        self.assertEqual(0, parsed.returncode, parsed.stderr)
        lines = parsed.stdout.splitlines()
        forge = "pass in quick inet proto tcp from 192.168.129.0/24 to 192.168.129.1 port = 8894 flags S/SA keep state"
        vm = "pass in quick inet proto tcp from 192.168.129.0/24 to 192.168.129.1 port = 8895 flags S/SA keep state"
        blocking = "block return in quick on ! lo0 inet from 192.168.129.0/24 to any"
        link_local = {
            f"block return in quick on {bridge} inet6 proto {proto} from fe80::/10 to any"
            for bridge in BRIDGES for proto in ("tcp", "udp")
        }
        self.assertEqual(
            {forge, vm, blocking, "block return in quick on ! lo0 inet6 from fdd2:4a0c:729f:fb6::/64 to any"} | link_local,
            set(lines),
        )
        self.assertEqual(len(lines), len(set(lines)))
        self.assertLess(lines.index(forge), lines.index(blocking))
        self.assertLess(lines.index(vm), lines.index(blocking))

    def test_activation_and_a_root_daemon_load_it_with_its_own_token(self):
        daemon = studio("{ }", PLANNING_PF_DAEMON)
        sandbox = studio("{ }", PF_DAEMON)
        args = daemon["ProgramArguments"]
        # Shaped exactly like the sandbox anchor's daemon: the same wrapper and loader.
        self.assertEqual(sandbox["ProgramArguments"][:7], args[:7])
        self.assertEqual(PLANNING_PF_ANCHOR, args[7])
        self.assertTrue(args[8].endswith("-pi-durable-planning.pf"))
        self.assertEqual("/var/run/pi-durable-planning-pf.token", args[9])
        self.assertEqual(10, len(args))
        unshared = {"Label": None, "ProgramArguments": None, "StandardOutPath": None, "StandardErrorPath": None}
        self.assertEqual({**sandbox, **unshared}, {**daemon, **unshared})
        self.assertEqual("org.nixos.pi-durable-planning-pf", daemon["Label"])
        self.assertEqual("/var/log/pi-durable-planning-pf.log", daemon["StandardOutPath"])
        self.assertEqual("/var/log/pi-durable-planning-pf.log", daemon["StandardErrorPath"])
        self.assertIsNone(daemon["UserName"], "runs as root")
        activation = studio("{ }", "config.system.activationScripts.extraActivation.text")
        self.assertIn(f"/sbin/pfctl {PLANNING_PF_ANCHOR} ", activation)
        self.assertIn("/var/run/pi-durable-planning-pf.token", activation)
        self.assertEqual(1, activation.count("# === pi-durable planning service network isolation (pf) ==="))

    def test_route_goes_through_the_guard_on_8792_only_when_set(self):
        activation = studio("{ }", "config.system.activationScripts.extraActivation.text")
        self.assertEqual(3, activation.count("pi-durable-page-route.sh"))
        self.assertEqual(1, activation.count("# === pi-durable planning page via Tailscale Serve ==="))
        self.assertIn(" 8792 http://127.0.0.1:8792;", activation)
        # The planning network's pf anchor is loaded before its page is routed.
        self.assertLess(
            activation.index("# === pi-durable planning service network isolation (pf) ==="),
            activation.index(" 8792 http://127.0.0.1:8792;"),
        )
        unrouted = studio("{ planning.tailscaleHttpsPort = lib.mkForce null; }", SUMMARY)
        self.assertEqual([], unrouted["failed"])
        self.assertEqual(2, unrouted["activation"].count("pi-durable-page-route.sh"))
        self.assertNotIn("http://127.0.0.1:8792", unrouted["activation"])
        self.assertIn("pi-durable-planning-relay", unrouted["agents"])
        moved = studio("{ planning = { relayPort = lib.mkForce 8793; tailscaleHttpsPort = lib.mkForce 8445; }; }",
                       "config.system.activationScripts.extraActivation.text")
        self.assertIn(" 8445 http://127.0.0.1:8793;", moved)

    def test_addresses_take_the_sandbox_proxys_cases(self):
        for field, accepted, refused in (
            ("planning.listenAddress", [planning()], [planning(listen=planning_network(v)) for v in LISTEN_REFUSED]),
            ("planning.allowedClients", [planning(clients=planning_network(v)) for v in CLIENTS_ACCEPTED],
             [planning(clients=planning_network(v)) for v in CLIENTS_REFUSED]),
            ("planning.ipv6Subnet", [planning(ipv6=planning_network(v)) for v in IPV6_ACCEPTED],
             [planning(ipv6=planning_network(v)) for v in IPV6_REFUSED]),
        ):
            results = failures(*accepted, *refused)
            for settings, failed in zip(accepted, results):
                with self.subTest(accepted=settings):
                    self.assertEqual([], failed)
            for settings, failed in zip(refused, results[len(accepted):]):
                with self.subTest(refused=settings):
                    self.assertTrue(any(f"services.pi-durable.{field} must" in m for m in failed), failed)
        # Required: nothing set is refused as each field.
        [failed] = failures("{ planning = { listenAddress = lib.mkForce null; allowedClients = lib.mkForce [ ]; ipv6Subnet = lib.mkForce null; }; }")
        for field in ("planning.listenAddress", "planning.allowedClients", "planning.ipv6Subnet"):
            self.assertTrue(any(f"services.pi-durable.{field} must" in m for m in failed), (field, failed))

    def test_refuses_the_sandbox_networks_addresses(self):
        cases = [
            planning(listen='"192.168.128.1"'),
            planning(clients='[ "192.168.129.0/24" "192.168.128.0/24" ]'),
            planning(ipv6='"fd85:1c07:74fb:2dc7::/64"'),
        ]
        for settings, failed in zip(cases, failures(*cases)):
            with self.subTest(settings):
                self.assertTrue(any("must never equal the sandbox network's" in m for m in failed), failed)

    def test_refuses_without_its_brokers_or_on_clashing_ports(self):
        no_forge, no_vm, disabled, relay_page, relay_sampler, serve_page, serve_sampler = failures(
            "{ forgeBroker.enable = lib.mkForce false; }",
            "{ vmBroker.enable = lib.mkForce false; }",
            "{ enable = lib.mkForce false; }",
            "{ planning.relayPort = lib.mkForce 8790; }",
            "{ planning.relayPort = lib.mkForce 8791; }",
            "{ planning.tailscaleHttpsPort = lib.mkForce 8790; }",
            "{ planning.tailscaleHttpsPort = lib.mkForce 8791; }",
        )
        for failed in (no_forge, no_vm, disabled):
            self.assertTrue(any("planning requires services.pi-durable.enable, forgeBroker and vmBroker" in m for m in failed), failed)
        for failed in (relay_page, relay_sampler):
            self.assertTrue(any("planning.relayPort must differ from the pages' ports" in m for m in failed), failed)
        for failed in (serve_page, serve_sampler):
            self.assertTrue(any("planning.tailscaleHttpsPort must differ from the other pages' Serve ports" in m for m in failed), failed)
        failed = json.loads(nix("eval", "--json", "--impure", "--expr", f"""
          let
            studio = (builtins.getFlake "path:{REPO}").darwinConfigurations.studio;
            inherit (studio.pkgs) lib;
            config = (studio.extendModules {{ modules = [ {{
              services.pi-durable = {{ page.tailscaleHttpsPort = lib.mkForce null; sampler.tailscaleHttpsPort = lib.mkForce null; }};
              services.tailscale.enable = lib.mkForce false;
            }} ]; }}).config;
          in {FAILED}
        """))
        self.assertIn("services.pi-durable.planning.tailscaleHttpsPort requires services.tailscale", failed)


class PlanningNodeFirewall(unittest.TestCase):
    """One application firewall exception for the proxies' Node, whichever of its listeners run."""

    def test_one_exception_while_any_gateway_listener_runs(self):
        node = studio("{ }", "builtins.head config.launchd.user.agents.pi-durable-forge-broker.serviceConfig.ProgramArguments")
        for name, settings in {
            "all": "{ }",
            "the brokers only": "{ modelProxy.enable = lib.mkForce false; githubProxy.enable = lib.mkForce false; }",
            "the proxies only": f"{{ {NO_PLANNING} }}",
        }.items():
            with self.subTest(name):
                activation = studio(settings, "config.system.activationScripts.extraActivation.text")
                self.assertEqual(1, activation.count(NODE_FIREWALL))
                self.assertEqual(1, activation.count(f"socketfilterfw --add {node} "))
                self.assertEqual(1, activation.count(f"socketfilterfw --unblock {node} "))
                self.assertNotRegex(activation, r"socketfilterfw --add /opt/homebrew/\S*node")


class PageRouteGuard(unittest.TestCase):
    """The activation guard never replaces a Tailscale Serve route it does not own."""

    TARGET = "http://127.0.0.1:8790"
    HOST = "studio.example.ts.net"

    def route(self, status, port="8443", status_fails=False):
        with tempfile.TemporaryDirectory() as tmp:
            calls = Path(tmp, "calls")
            fake = Path(tmp, "tailscale")
            answer = "exit 1" if status_fails else 'cat "$STATUS"; exit 0'
            fake.write_text(f'#!/bin/sh\nif [ "$2" = status ]; then {answer}; fi\necho "$*" >> {calls}\n')
            fake.chmod(0o755)
            Path(tmp, "status.json").write_text(status if isinstance(status, str) else json.dumps(status))
            result = subprocess.run(
                ["/bin/bash", str(ROUTE_SCRIPT), str(fake), shutil.which("jq") or "jq", port, self.TARGET],
                env={**os.environ, "STATUS": str(Path(tmp, "status.json"))},
                text=True,
                capture_output=True,
            )
            return result, calls.read_text() if calls.exists() else ""

    def live_shape(self):
        # The shape Studio reported on 2026-10-16, including a route Nix does not declare.
        return {
            "TCP": {"443": {"HTTPS": True}, "9120": {"HTTPS": True}, "10443": {"HTTPS": True}, "11434": {"TCPForward": "127.0.0.1:11434"}},
            "Web": {
                f"{self.HOST}:443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:9122"}}},
                f"{self.HOST}:9120": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:9121"}}},
                f"{self.HOST}:10443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:14305"}}},
            },
        }

    def owned(self, status):
        """`status` plus exactly this route on 8443: an HTTPS listener and one "/" proxy."""
        return {
            **status,
            "TCP": {**status.get("TCP", {}), "8443": {"HTTPS": True}},
            "Web": {**status.get("Web", {}), f"{self.HOST}:8443": {"Handlers": {"/": {"Proxy": self.TARGET}}}},
        }

    def assertRefused(self, status, port="8443", status_fails=False):
        result, calls = self.route(status, port, status_fails)
        self.assertNotEqual(0, result.returncode)
        self.assertIn("pi-durable:", result.stderr)
        self.assertEqual("", calls)

    def test_refuses_every_port_another_route_holds(self):
        for port in ("443", "9120", "10443", "11434"):
            with self.subTest(port=port):
                self.assertRefused(self.live_shape(), port)

    def test_refuses_when_serve_status_is_unreadable(self):
        self.assertRefused({}, status_fails=True)

    def test_routes_only_a_free_or_exactly_owned_port(self):
        cases = {
            "nothing served": {},
            "other ports served, Funnel on another port": {**self.live_shape(), "AllowFunnel": {f"{self.HOST}:443": True}},
            "already exactly this route (idempotent)": self.owned(self.live_shape()),
        }
        for name, status in cases.items():
            with self.subTest(name):
                result, calls = self.route(status)
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertEqual(f"serve --bg --yes --https=8443 {self.TARGET}\n", calls)

    def test_refuses_funnel_and_conflicting_or_stale_config_on_the_port(self):
        ours = {"/": {"Proxy": self.TARGET}}

        def on_8443(tcp=None, handlers=None):
            status = {}
            if tcp is not None:
                status["TCP"] = {"8443": tcp}
            if handlers is not None:
                status["Web"] = {f"{self.HOST}:8443": {"Handlers": handlers}}
            return status

        cases = {
            "Funnel on this exact route": {**self.owned({}), "AllowFunnel": {f"{self.HOST}:8443": True}},
            "Funnel on the otherwise free port": {"AllowFunnel": {f"{self.HOST}:8443": True}},
            "Funnel on the port under another hostname": {**self.owned({}), "AllowFunnel": {"other.example.ts.net:8443": True}},
            "TCP forward with this handler": on_8443({"TCPForward": "127.0.0.1:8790"}, ours),
            "plain HTTP listener with this handler": on_8443({"HTTP": True}, ours),
            "HTTPS listener that also forwards TCP": on_8443({"HTTPS": True, "TCPForward": "127.0.0.1:1"}, ours),
            "HTTPS listener with no handler": on_8443({"HTTPS": True}),
            "stale handler with no listener": on_8443(handlers=ours),
            "another target": on_8443({"HTTPS": True}, {"/": {"Proxy": "http://127.0.0.1:9000"}}),
            "an extra handler": on_8443({"HTTPS": True}, {**ours, "/other": {"Proxy": "http://127.0.0.1:9000"}}),
            "a foreground session on the port": {"Foreground": {"session": on_8443({"HTTPS": True}, ours)}},
            "unparseable status": "not json",
            "empty status": "",
        }
        for name, status in cases.items():
            with self.subTest(name):
                self.assertRefused(status)


class SandboxPf(unittest.TestCase):
    """The pf anchor that confines the sandbox network to the proxy, and how it is loaded.

    Proves the rules this Mac's pfctl accepts and how the loader behaves against
    a stand-in pfctl. Whether the VM can still reach anything is the live
    sandbox gate's question, not this test's.
    """

    def test_this_macs_pfctl_parses_the_anchor_as_intended(self):
        rules = nix(
            "build", "--impure", "--no-link", "--print-out-paths", "--expr",
            studio_expr("{ }", f'pkgs.writeText "pi-durable-sgg.pf" (builtins.readFile {PF_RULES})'),
        ).strip().splitlines()[-1]
        parsed = subprocess.run(["/sbin/pfctl", "-nvf", rules], text=True, capture_output=True)
        self.assertEqual(0, parsed.returncode, parsed.stderr)
        lines = parsed.stdout.splitlines()
        passing = "pass in quick inet proto tcp from 192.168.128.0/24 to 192.168.128.1 port = 8888 flags S/SA keep state"
        model = "pass in quick inet proto tcp from 192.168.128.0/24 to 192.168.128.1 port = 8892 flags S/SA keep state"
        github = "pass in quick inet proto tcp from 192.168.128.0/24 to 192.168.128.1 port = 8893 flags S/SA keep state"
        replay = "pass in quick inet proto tcp from 192.168.128.0/24 to 192.168.128.1 port = 8889 flags S/SA keep state"
        forge = "pass in quick inet proto tcp from 192.168.128.0/24 to 192.168.128.1 port = 8894 flags S/SA keep state"
        blocking = "block return in quick on ! lo0 inet from 192.168.128.0/24 to any"
        link_local = {
            f"block return in quick on {bridge} inet6 proto {proto} from fe80::/10 to any"
            for bridge in BRIDGES for proto in ("tcp", "udp")
        }
        self.assertEqual(
            {passing, model, github, replay, forge, blocking, "block return in quick on ! lo0 inet6 from fd85:1c07:74fb:2dc7::/64 to any"} | link_local,
            set(lines),
        )
        self.assertEqual(len(lines), len(set(lines)))
        # pfctl may reorder rules that cannot both match; the proxy's pass must still come first.
        self.assertLess(lines.index(passing), lines.index(blocking))
        self.assertLess(lines.index(model), lines.index(blocking))
        self.assertLess(lines.index(github), lines.index(blocking))
        self.assertLess(lines.index(replay), lines.index(blocking))
        self.assertLess(lines.index(forge), lines.index(blocking))

    def test_a_root_daemon_reloads_it_at_every_boot(self):
        daemon = studio("{ }", PF_DAEMON)
        args = daemon["ProgramArguments"]
        # Through /bin/sh until the Nix store is mounted, as every boot-time daemon here.
        self.assertEqual(["/bin/sh", "-c"], args[:2])
        self.assertIn('exec "$@"', args[2])
        self.assertEqual(["/sbin/pfctl", PF_ANCHOR], args[6:8])
        self.assertTrue(args[5].endswith("-pi-durable-pf.sh"))
        self.assertEqual("/var/run/pi-durable-pf.token", args[9])
        self.assertIsNone(daemon["UserName"], "runs as root")
        self.assertTrue(daemon["RunAtLoad"])
        self.assertIs(False, daemon["KeepAlive"]["SuccessfulExit"], "retried until it succeeds, then left alone")
        self.assertEqual("/var/log/pi-durable-pf.log", daemon["StandardErrorPath"])

    # The loader, against a stand-in pfctl that keeps its state in files.
    FAKE_PFCTL = r"""#!/bin/bash
printf '%s\n' "$*" >> "$STATE/calls"
case "$*" in
  "-nvf "*) printf '%s\n' "$EXPECTED" ;;
  "-a $ANCHOR -f "*) [ -z "$LOAD_FAILS" ] || { echo "pfctl: Syntax error" >&2; exit 1; }
                     printf '%s\n' "${LOADED:-$EXPECTED}" > "$STATE/loaded" ;;
  "-a $ANCHOR -s rules") cat "$STATE/loaded" 2>/dev/null ;;
  "-s References") echo TOKENS:; cat "$STATE/refs" 2>/dev/null ;;
  "-E") [ -n "$NO_TOKEN" ] && { echo "pf enabled" >&2; exit 0; }
        n=$((RANDOM + 100000)); echo "501  pfctl  $n  0 secs" >> "$STATE/refs"
        printf 'No ALTQ support in kernel\npf enabled\nToken : %s\n' "$n" >&2 ;;
  "-s info") [ -s "$STATE/refs" ] && echo "Status: Enabled for 0 days" || echo "Status: Disabled" ;;
  "-s rules") [ -n "$NO_HOOK" ] || echo 'anchor "com.apple/*" all' ;;
  *) echo "unexpected: $*" >&2; exit 2 ;;
esac
"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.pfctl = self.tmp / "pfctl"
        self.pfctl.write_text(self.FAKE_PFCTL)
        self.pfctl.chmod(0o755)
        self.rules = self.tmp / "rules.pf"
        self.rules.write_text("pass in quick all\n")
        self.token = self.tmp / "token"

    def load(self, **env):
        result = subprocess.run(
            ["/bin/bash", str(PF_SCRIPT), str(self.pfctl), PF_ANCHOR, str(self.rules), str(self.token)],
            env={**os.environ, "STATE": str(self.tmp), "ANCHOR": PF_ANCHOR, "EXPECTED": "pass in quick all",
                 "PI_DURABLE_PF_WAIT": "0", **env},
            text=True,
            capture_output=True,
        )
        calls = (self.tmp / "calls").read_text().splitlines() if (self.tmp / "calls").exists() else []
        return result, calls

    def test_loads_the_anchor_and_holds_one_enable_reference(self):
        result, calls = self.load()
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn(f"-a {PF_ANCHOR} -f {self.rules}", calls)
        self.assertEqual(1, calls.count("-E"))
        token = self.token.read_text().strip()
        self.assertIn(token, (self.tmp / "refs").read_text())
        # Activation and the boot daemon both run it: the reference is not taken twice.
        result, calls = self.load()
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(1, calls.count("-E"))
        self.assertEqual(token, self.token.read_text().strip())

    def test_a_token_from_before_a_restart_is_replaced(self):
        self.token.write_text("4242\n")
        result, calls = self.load()
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(1, calls.count("-E"))
        self.assertNotEqual("4242", self.token.read_text().strip())

    def test_fails_closed(self):
        cases = {
            "pfctl refuses the rules": ({"LOAD_FAILS": "1"}, "refused"),
            "no enable token": ({"NO_TOKEN": "1"}, "no enable token"),
            "nothing evaluates com.apple/*": ({"NO_HOOK": "1"}, 'anchor "com.apple/*"'),
            "the anchor holds other rules": ({"LOADED": "pass all"}, "does not hold exactly"),
        }
        for name, (env, message) in cases.items():
            with self.subTest(name):
                for state in ("calls", "loaded", "refs", "token"):
                    (self.tmp / state).unlink(missing_ok=True)
                result, calls = self.load(**env)
                self.assertNotEqual(0, result.returncode)
                self.assertIn("pi-durable:", result.stderr)
                self.assertIn(message, result.stderr)

    def test_a_refused_load_enables_nothing(self):
        result, calls = self.load(LOAD_FAILS="1")
        self.assertNotEqual(0, result.returncode)
        self.assertNotIn("-E", calls)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class ProxyAllowlist(unittest.TestCase):
    """The generated tinyproxy configuration, run by the pinned tinyproxy, on loopback.

    Proves only the filter: hosts outside the exact allowlist and CONNECT to
    ports other than 443 are refused. Whether the VM can bypass the proxy is
    the live sandbox gate's question, not this test's.
    """

    def setUp(self):
        class Ok(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"upstream")

            def log_message(self, format, *args):
                pass

        self.upstream = HTTPServer(("127.0.0.1", 0), Ok)
        threading.Thread(target=self.upstream.serve_forever, daemon=True).start()
        self.addCleanup(self.upstream.server_close)
        self.addCleanup(self.upstream.shutdown)

        self.port = free_port()
        settings = (
            '{ enable = lib.mkForce true; proxy = { listenAddress = lib.mkForce "127.0.0.1"; port = %d; '
            'allowedClients = lib.mkForce [ "127.0.0.1" ]; '
            'allowedDomains = [ "localhost" "github.com" "pypi.org" ]; }; }' % self.port
        )
        built = nix(
            "build", "--impure", "--no-link", "--print-out-paths", "--expr",
            studio_expr(settings, 'pkgs.writeText "proxy-args" (builtins.toJSON '
                        "config.launchd.user.agents.pi-durable-proxy.serviceConfig.ProgramArguments)"),
        ).strip().splitlines()[-1]
        args = json.loads(Path(built).read_text())
        self.proxy = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(self.proxy.wait)
        self.addCleanup(self.proxy.terminate)
        for _ in range(50):
            try:
                socket.create_connection(("127.0.0.1", self.port), timeout=0.2).close()
                return
            except OSError:
                time.sleep(0.1)
        self.fail(f"tinyproxy did not listen on {self.port} (exit {self.proxy.poll()})")

    def status(self, request):
        with socket.create_connection(("127.0.0.1", self.port), timeout=10) as s:
            s.sendall(request.encode())
            reply = s.recv(4096).decode(errors="replace")
        return int(reply.split()[1])

    def test_allowlisted_host_is_proxied(self):
        port = self.upstream.server_address[1]
        self.assertEqual(200, self.status(f"GET http://localhost:{port}/ HTTP/1.0\r\nHost: localhost:{port}\r\n\r\n"))

    def test_other_hosts_are_refused(self):
        for host in ("example.com", "github.com.evil.example", "evilgithub.com", "api.github.com.example", "GITHUB.COM.evil"):
            with self.subTest(host=host):
                self.assertEqual(403, self.status(f"CONNECT {host}:443 HTTP/1.0\r\n\r\n"))
                self.assertEqual(403, self.status(f"GET http://{host}/ HTTP/1.0\r\nHost: {host}\r\n\r\n"))

    def test_connect_only_to_443(self):
        self.assertEqual(403, self.status("CONNECT localhost:22 HTTP/1.0\r\n\r\n"))
        # Port 443 on an allowlisted host passes the filter; nothing listens, so it fails upstream, not as 403.
        self.assertNotEqual(403, self.status("CONNECT localhost:443 HTTP/1.0\r\n\r\n"))


NET_ANCHOR_SCRIPT = REPO / "modules" / "services" / "pi-durable-net-anchor.sh"
NET_ANCHOR_IMAGE = "pi-durable-planning-agent:30bade4"
NET_ANCHOR_METRICS = f"{HOME}/.local/state/pi-durable-net-anchors/metrics"
NET_ANCHORS = {"pi-durable-sgg": "pi-durable-net-anchor-pi-durable-sgg",
               "pi-durable-planning": "pi-durable-net-anchor-pi-durable-planning"}
NET_ANCHOR_ALERTS = {"PiDurableNetAnchorRestarted": "warning", "PiDurableNetAnchorDown": "critical",
                     "PiDurableNetAnchorGatewayMissing": "critical"}
# Each network's own gateway, then the gateways of the networks before it (the
# sandbox's first, then the planning network's).
NET_ANCHOR_GATEWAYS = {"pi-durable-sgg": ["192.168.128.1"], "pi-durable-planning": ["192.168.129.1", "192.168.128.1"]}


class NetAnchors(unittest.TestCase):
    """One agent per VM network holds an idle VM attached (durable-pi-workspace#418)."""

    def test_studio_runs_one_anchor_per_network_with_the_contract_command(self):
        script = studio("{ }", f"{AGENTS}.{NET_ANCHORS['pi-durable-sgg']}.serviceConfig.ProgramArguments")[1]
        self.assertRegex(script, r"^/nix/store/[0-9a-z]{32}-pi-durable-net-anchor\.sh$")
        for network, agent in NET_ANCHORS.items():
            with self.subTest(network):
                config = studio("{ }", f"{AGENTS}.{agent}.serviceConfig")
                self.assertEqual(f"org.nixos.{agent}", config["Label"])
                self.assertEqual(["/bin/sh", script, network, NET_ANCHOR_IMAGE, NET_ANCHOR_METRICS, *NET_ANCHOR_GATEWAYS[network]],
                                 config["ProgramArguments"])
                # HOME and the system PATH (Apple's container) only: no secret, mount, volume or grant.
                self.assertEqual({"HOME": HOME, "PATH": SYSTEM_PATH}, config["EnvironmentVariables"])
                self.assertIs(True, config["RunAtLoad"])
                self.assertIs(True, config["KeepAlive"])
                self.assertEqual(30, config["ExitTimeOut"])
                self.assertEqual(f"/tmp/{agent}.log", config["StandardOutPath"])
                self.assertEqual(f"/tmp/{agent}.error.log", config["StandardErrorPath"])
                for key in ("UserName", "WatchPaths", "Sockets", "WorkingDirectory"):
                    self.assertIsNone(config[key], key)
        s = studio("{ }", SUMMARY)
        self.assertEqual(sorted(NET_ANCHORS.values()), sorted(name for name in s["agents"] if "net-anchor" in name))
        # Not brew-backed: activation's re-pin never restarts an anchor, and nothing restarts a network helper.
        self.assertNotIn("net-anchor", s["activation"])
        self.assertNotIn("container-network-vmnet", s["activation"])
        self.assertIn("--collector.textfile.directory=" + NET_ANCHOR_METRICS,
                      studio("{ }", f"{AGENTS}.node-exporter.serviceConfig.ProgramArguments"))

    def test_the_script_runs_exactly_the_anchor_command_and_never_restarts_a_helper(self):
        text = NET_ANCHOR_SCRIPT.read_text()
        self.assertIn('call run --detach --name "$name" --network "$network" --cpus 1 --memory 200M "$image" sleep infinity', text)
        self.assertIn('name="net-anchor-$network"', text)
        code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
        self.assertNotIn("--rm", code)
        # Every `container` call goes through the bounded `call`, but the one inside it.
        self.assertEqual(['  container "$@" >"$work/out" </dev/null &'], [l for l in code.splitlines() if "container " in l and "container $1" not in l and "log " not in l and "give_up" not in l])
        # The only removal is of a stopped leftover; nothing ever stops a VM.
        self.assertEqual(1, code.count("call delete"))
        self.assertNotIn("call stop", code)
        self.assertNotIn("call kill", code)
        self.assertNotIn("kickstart", code)
        self.assertNotIn("launchctl", code)
        self.assertNotIn("container-network-vmnet", code)

    def test_each_anchor_goes_with_its_networks_service(self):
        for name, settings, expected in (
            ("planning off", f"{{ {NO_PLANNING} planning.network = lib.mkForce null; }}", ["pi-durable-sgg"]),
            ("planning off, its network still named", f"{{ {NO_PLANNING} }}", ["pi-durable-sgg"]),
            ("no networks named", "{ proxy.network = lib.mkForce null; planning.network = lib.mkForce null; }", []),
            ("disabled", "{ enable = lib.mkForce false; }", []),
            ("sandbox network only unnamed", "{ proxy.network = lib.mkForce null; }", ["pi-durable-planning"]),
        ):
            with self.subTest(name):
                s = studio(settings, SUMMARY)
                self.assertEqual(sorted(NET_ANCHORS[n] for n in expected),
                                 sorted(a for a in s["agents"] if "net-anchor" in a))
                if not expected:
                    self.assertNotIn(NET_ANCHOR_METRICS, studio(settings, f"toString {AGENTS}.node-exporter.serviceConfig.ProgramArguments"))
                    self.assertFalse(set(NET_ANCHOR_ALERTS) & set(studio(settings, "map (r: r.alert or \"\") config.services.monitoring.extraAlertRules")))
                if name == "disabled":
                    # A disabled module may still carry its other assertions; none is the anchors'.
                    self.assertFalse([m for m in s["failed"] if "anchor holds" in m or "network names" in m or "netAnchor" in m], s["failed"])
                else:
                    self.assertEqual([], s["failed"])

    def test_the_planning_anchor_waits_for_the_sandbox_gateway_and_follows_the_addresses(self):
        settings = '{ proxy.listenAddress = lib.mkForce "192.168.130.1"; proxy.allowedClients = lib.mkForce [ "192.168.130.0/24" ]; planning.listenAddress = lib.mkForce "192.168.131.1"; planning.allowedClients = lib.mkForce [ "192.168.131.0/24" ]; }'
        self.assertEqual(["192.168.130.1"], studio(settings, f"{AGENTS}.{NET_ANCHORS['pi-durable-sgg']}.serviceConfig.ProgramArguments")[5:])
        self.assertEqual(["192.168.131.1", "192.168.130.1"], studio(settings, f"{AGENTS}.{NET_ANCHORS['pi-durable-planning']}.serviceConfig.ProgramArguments")[5:])
        # Alone, the planning anchor waits for nothing.
        alone = studio("{ proxy.network = lib.mkForce null; }", f"{AGENTS}.{NET_ANCHORS['pi-durable-planning']}.serviceConfig.ProgramArguments")
        self.assertEqual(["192.168.129.1"], alone[5:])

    def test_names_and_image_follow_the_options(self):
        settings = '{ proxy.network = lib.mkForce "other-sgg"; netAnchor.image = lib.mkForce "example/anchor@sha256:' + "0" * 64 + '"; }'
        config = studio(settings, f"{AGENTS}.pi-durable-net-anchor-other-sgg.serviceConfig.ProgramArguments")
        self.assertEqual(["other-sgg", "example/anchor@sha256:" + "0" * 64], config[2:4])

    def test_refusals(self):
        bad_names = ['"Pi-Durable"', '"pi durable"', '"-sgg"', '"sgg-"', '"pi;rm"', '""', r'"pi\nx"']
        bad_images = ["null", '""', '"has space:1"', '"x:1 --mount=/"', r'"x\ny"', '"Upper:1"', '"x:$(id)"']
        accepted, no_listen, same, *results = failures(
            "{ }",
            "{ proxy = { listenAddress = lib.mkForce null; allowedClients = lib.mkForce [ ]; }; }",
            '{ planning.network = lib.mkForce "pi-durable-sgg"; }',
            *[f"{{ proxy.network = lib.mkForce {v}; }}" for v in bad_names],
            *[f"{{ netAnchor.image = lib.mkForce {v}; }}" for v in bad_images],
        )
        self.assertEqual([], accepted)
        self.assertTrue(any("proxy.network requires proxy.listenAddress" in m for m in no_listen), no_listen)
        self.assertTrue(any("planning.network must differ from proxy.network" in m for m in same), same)
        for value, failed in zip(bad_names, results):
            with self.subTest(name=value):
                self.assertTrue(any("must be container network names" in m for m in failed), failed)
        for value, failed in zip(bad_images, results[len(bad_names):]):
            with self.subTest(image=value):
                self.assertTrue(any("netAnchor.image must name an image" in m for m in failed), failed)


@cache
def monitoring_paths():
    out = nix("build", "--no-link", "--print-out-paths", "--impure", "--expr", studio_expr("{ }", """
      let
        flagValue = flag: args: lib.removePrefix flag (lib.findFirst (lib.hasPrefix flag) null args);
      in pkgs.writeText "net-anchor-alert-paths.json" (builtins.toJSON {
        prometheusConfig = flagValue "--config.file=" config.launchd.daemons.prometheus.serviceConfig.ProgramArguments;
        promtool = "${pkgs.prometheus.cli}/bin/promtool";
      })
    """))
    return json.loads(Path(out.strip().splitlines()[-1]).read_text())


class NetAnchorAlerts(unittest.TestCase):
    """The anchors' textfile metrics and their alerts, through Studio's own promtool."""

    TARGET = {"instance": "localhost:9100", "job": "node_exporter"}

    @classmethod
    def rules(cls):
        paths = monitoring_paths()
        rule_file = json.loads(Path(paths["prometheusConfig"]).read_text())["rule_files"][0]
        by_name = {r["alert"]: r for g in json.loads(Path(rule_file).read_text())["groups"] for r in g["rules"]}
        return paths["promtool"], rule_file, by_name

    def series(self, name, values, network):
        labels = ",".join(f'{k}="{v}"' for k, v in {**self.TARGET, "network": network}.items())
        return {"series": f"{name}{{{labels}}}", "values": values}

    def healthy(self, network, minutes=180):
        # The agent stamps its check every poll: the stamp follows time().
        return [self.series("pi_durable_net_anchor_up", f"1x{minutes}", network),
                self.series("pi_durable_net_anchor_gateway_present", f"1x{minutes}", network),
                self.series("pi_durable_net_anchor_runs_since_boot", f"1x{minutes}", network),
                self.series("pi_durable_net_anchor_last_run_timestamp_seconds", f"0x{minutes}", network),
                self.series("pi_durable_net_anchor_last_check_timestamp_seconds", f"0+60x{minutes}", network)]

    def replaced(self, network, name, values):
        return [x for x in self.healthy(network) if f"{name}{{" not in x["series"]] + [self.series(name, values, network)]

    def firing(self, alert, network, value, labels=None):
        _, _, by_name = self.rules()
        annotations = {k: v.replace("{{ $labels.network }}", network).replace("{{ $value }}", value)
                       for k, v in by_name[alert]["annotations"].items()}
        return [{"exp_labels": {**(self.TARGET if labels is None else labels), "network": network,
                                "severity": NET_ANCHOR_ALERTS[alert]},
                 "exp_annotations": annotations}]

    def promtool_test(self, inputs, alert, *expectations):
        promtool, rule_file, _ = self.rules()
        spec = {"rule_files": [rule_file], "evaluation_interval": "1m", "tests": [{
            "interval": "1m", "input_series": inputs, "alert_rule_test": [
                {"eval_time": t, "alertname": alert, "exp_alerts": exp} for t, exp in expectations]}]}
        with tempfile.TemporaryDirectory() as tmp:
            spec_file = Path(tmp) / "t.json"
            spec_file.write_text(json.dumps(spec))
            result = subprocess.run([promtool, "test", "rules", str(spec_file)], text=True, capture_output=True)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)

    def test_rules_are_generated_accepted_and_worded_for_a_person(self):
        promtool, rule_file, by_name = self.rules()
        self.assertTrue(set(NET_ANCHOR_ALERTS) <= set(by_name))
        result = subprocess.run([promtool, "check", "rules", rule_file], text=True, capture_output=True)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        for alert in NET_ANCHOR_ALERTS:
            text = by_name[alert]["annotations"]["summary"] + by_name[alert]["annotations"]["description"]
            self.assertIn("bridge", text)
            self.assertIn("may collide until both are checked", text)
            self.assertIn("Never `launchctl kickstart`", text)
        self.assertIn('absent(pi_durable_net_anchor_up{network="pi-durable-sgg"})', by_name["PiDurableNetAnchorDown"]["expr"])
        self.assertIn('absent(pi_durable_net_anchor_up{network="pi-durable-planning"})', by_name["PiDurableNetAnchorDown"]["expr"])
        self.assertIn("(time() - pi_durable_net_anchor_last_check_timestamp_seconds) > 300", by_name["PiDurableNetAnchorDown"]["expr"])
        self.assertEqual("pi_durable_net_anchor_up == 1 and pi_durable_net_anchor_gateway_present == 0",
                         by_name["PiDurableNetAnchorGatewayMissing"]["expr"])
        gateway = by_name["PiDurableNetAnchorGatewayMissing"]["annotations"]["description"]
        for words in ("anchor VM has been running and attached", "no host interface", "collision", "killed InternetSharing",
                      "OPERATIONS.md §9", "It needs a person"):
            self.assertIn(words, gateway)

    def test_the_metrics_the_script_writes_are_what_promtool_reads(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = NetAnchorScript.run_script(Path(tmp))
            text = (Path(tmp) / "metrics" / "net-anchor-pi-durable-sgg.prom").read_text()
        self.assertEqual(1, run.returncode)
        self.assertIn('pi_durable_net_anchor_runs_since_boot{network="pi-durable-sgg"} 1', text)
        self.assertIn('pi_durable_net_anchor_gateway_present{network="pi-durable-sgg"} 1', text)
        self.assertIn('pi_durable_net_anchor_last_check_timestamp_seconds{network="pi-durable-sgg"} ', text)
        result = subprocess.run([self.rules()[0], "check", "metrics"], input=text, text=True, capture_output=True)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)

    def test_healthy_anchors_raise_nothing(self):
        inputs = self.healthy("pi-durable-sgg") + self.healthy("pi-durable-planning")
        for alert in NET_ANCHOR_ALERTS:
            self.promtool_test(inputs, alert, ("170m", []))

    def test_a_restart_warns_for_an_hour_then_resolves(self):
        # Run 2 began at 30m (epoch seconds 1800).
        inputs = self.healthy("pi-durable-planning") + [
            x for x in self.healthy("pi-durable-sgg") if "_up{" not in x["series"] and "runs_since" not in x["series"] and "last_run" not in x["series"]
        ] + [
            self.series("pi_durable_net_anchor_up", "1x29 0 1x150", "pi-durable-sgg"),
            self.series("pi_durable_net_anchor_runs_since_boot", "1x29 1 2x150", "pi-durable-sgg"),
            self.series("pi_durable_net_anchor_last_run_timestamp_seconds", "0x29 0 1800x150", "pi-durable-sgg"),
        ]
        self.promtool_test(inputs, "PiDurableNetAnchorRestarted",
                           ("29m", []),
                           ("31m", self.firing("PiDurableNetAnchorRestarted", "pi-durable-sgg", "2")),
                           ("89m", self.firing("PiDurableNetAnchorRestarted", "pi-durable-sgg", "2")),
                           ("91m", []))
        self.promtool_test(inputs, "PiDurableNetAnchorDown", ("40m", []))

    def test_an_anchor_that_isnt_running_is_critical_after_5_minutes(self):
        inputs = self.healthy("pi-durable-sgg") + self.replaced("pi-durable-planning", "pi_durable_net_anchor_up", "1x10 0x60")
        self.promtool_test(inputs, "PiDurableNetAnchorDown",
                           ("14m", []),
                           ("17m", self.firing("PiDurableNetAnchorDown", "pi-durable-planning", "0")))
        # Not attached, its missing gateway is the Down alert's, not GatewayMissing's.
        inputs = self.healthy("pi-durable-sgg") + [
            x for x in self.replaced("pi-durable-planning", "pi_durable_net_anchor_up", "1x10 0x60") if "gateway_present" not in x["series"]
        ] + [self.series("pi_durable_net_anchor_gateway_present", "1x10 0x60", "pi-durable-planning")]
        self.promtool_test(inputs, "PiDurableNetAnchorGatewayMissing", ("40m", []))

    def test_a_stale_check_is_down_even_when_the_file_says_up(self):
        # A hung agent, or a file left from before a reboot: up 1, but the
        # check stamp stops at 0. Stale from 6m (> 300 s), firing 5m later.
        inputs = self.healthy("pi-durable-sgg") + self.replaced("pi-durable-planning", "pi_durable_net_anchor_last_check_timestamp_seconds", "0x60")
        self.promtool_test(inputs, "PiDurableNetAnchorDown",
                           ("10m", []),
                           ("12m", self.firing("PiDurableNetAnchorDown", "pi-durable-planning", "720")))
        # A stamp that follows time() never fires.
        self.promtool_test(self.healthy("pi-durable-sgg") + self.healthy("pi-durable-planning"), "PiDurableNetAnchorDown", ("170m", []))

    def test_an_attached_anchor_without_its_gateway_is_critical_after_3_minutes(self):
        inputs = self.healthy("pi-durable-planning") + self.replaced("pi-durable-sgg", "pi_durable_net_anchor_gateway_present", "1x10 0x30 1x20")
        self.promtool_test(inputs, "PiDurableNetAnchorGatewayMissing",
                           ("13m", []),
                           ("15m", self.firing("PiDurableNetAnchorGatewayMissing", "pi-durable-sgg", "1")),
                           ("45m", []))
        self.promtool_test(inputs, "PiDurableNetAnchorDown", ("15m", []))

    def test_an_anchor_that_never_reported_is_critical(self):
        inputs = self.healthy("pi-durable-sgg")
        self.promtool_test(inputs, "PiDurableNetAnchorDown",
                           ("4m", []),
                           ("6m", self.firing("PiDurableNetAnchorDown", "pi-durable-planning", "1", labels={})))


class NetAnchorScript(unittest.TestCase):
    """The anchor script against a stand-in `container`, `ifconfig` and `sleep`: nothing real is started.

    No test waits on a clock. A poll of 300 s (CONTROLLED) is the stand-in
    `sleep`'s cue to hand control to the test: it writes to the `ready` FIFO
    and blocks reading `go`, so the test inspects the agent mid-watch and then
    lets it continue (`step`) or signals it.
    """

    NAME = "net-anchor-pi-durable-sgg"
    LABEL = '{network="pi-durable-sgg"}'
    GATEWAY = "192.168.128.1"
    CONTROLLED = "300"

    # The stand-in keeps the VMs that are running and present in files under
    # its state directory. Each `container list --quiet` (running VMs) takes
    # the next line of `lists`: `ok` lists, `fail` exits 1, `hang` never
    # answers, `promote` makes the anchor running first (a VM that was still
    # starting), and `gone` (also once `lists` is empty) stops the anchor
    # first, as if it ended. `run`, `delete` and `system` fail when their
    # `fail-*` file exists. The first call copies the metrics file as it was.
    FAKE = """#!{python}
import shutil, sys, time
from pathlib import Path
state = Path({state!r})
args = sys.argv[1:]
first = state / "first-seen"
if not first.exists():
    metrics = Path({metrics!r})
    shutil.copy(metrics, first) if metrics.exists() else first.write_text("")
with open(state / "calls", "a") as f:
    f.write(" ".join(args) + "\\n")
def names(kind):
    p = state / kind
    return [l for l in p.read_text().splitlines() if l] if p.exists() else []
def put(kind, values):
    (state / kind).write_text("".join(v + "\\n" for v in values))
if args[0] == "list":
    if "--all" in args:
        print("\\n".join(names("present")))
        sys.exit(0)
    queue = names("lists")
    step = queue.pop(0) if queue else "gone"
    put("lists", queue)
    if step == "fail":
        sys.exit(1)
    if step == "hang":
        time.sleep(600)
    if step == "gone":
        put("running", [n for n in names("running") if n != {name!r}])
    if step == "promote":
        put("running", names("running") + [{name!r}])
    print("\\n".join(names("running")))
elif args[0] in ("delete", "system"):
    if (state / f"fail-{{args[0]}}").exists():
        sys.exit(1)
    if args[0] == "delete":
        put("present", [n for n in names("present") if n != args[-1]])
        put("running", [n for n in names("running") if n != args[-1]])
elif args[0] == "run":
    if (state / "fail-run").exists():
        sys.exit(1)
    name = args[args.index("--name") + 1]
    put("present", names("present") + [name])
    put("running", names("running") + [name])
"""
    IFCONFIG = """#!/bin/sh
while read -r address; do
  [ -n "$address" ] && printf '\\tinet %s netmask 0xffffff00 broadcast 0.0.0.0\\n' "$address"
done < {addresses}
exit 0
"""
    SLEEP = """#!/bin/sh
if [ "$1" = {controlled} ]; then
  printf x > {ready}
  read -r _ < {go}
  exit 0
fi
exec /bin/sleep "$@"
"""

    @classmethod
    def setup(cls, tmp, running=(), present=(), lists=(), fail=(), addresses=None):
        state = tmp / "state"
        state.mkdir(exist_ok=True)
        bin_dir = tmp / "bin"
        bin_dir.mkdir(exist_ok=True)
        files = {
            "container": cls.FAKE.format(python=sys.executable, state=str(state), name=cls.NAME,
                                         metrics=str(tmp / "metrics" / "net-anchor-pi-durable-sgg.prom")),
            "ifconfig": cls.IFCONFIG.format(addresses=state / "addresses"),
            "sleep": cls.SLEEP.format(controlled=cls.CONTROLLED, ready=state / "ready", go=state / "go"),
        }
        for tool, text in files.items():
            (bin_dir / tool).write_text(text)
            (bin_dir / tool).chmod(0o755)
        for fifo in ("ready", "go"):
            if not (state / fifo).exists():
                os.mkfifo(state / fifo)
        for kind, values in (("running", running), ("present", tuple(present) + tuple(running)), ("lists", lists),
                             ("addresses", [cls.GATEWAY] if addresses is None else addresses)):
            (state / kind).write_text("".join(v + "\n" for v in values))
        for what in fail:
            (state / f"fail-{what}").touch()
        return {"PATH": f"{bin_dir}:/usr/bin:/bin:/usr/sbin:/sbin", "HOME": str(tmp), "TMPDIR": str(tmp),
                "PI_DURABLE_NET_ANCHOR_POLL_SECONDS": "0", "PI_DURABLE_NET_ANCHOR_SETTLE_SECONDS": "0"}

    @classmethod
    def command(cls, tmp, earlier=()):
        return ["/bin/sh", str(NET_ANCHOR_SCRIPT), "pi-durable-sgg", NET_ANCHOR_IMAGE, str(tmp / "metrics"), cls.GATEWAY, *earlier]

    @classmethod
    def run_script(cls, tmp, timeout=60, env=None, earlier=(), **kwargs):
        environment = {**cls.setup(tmp, **kwargs), **(env or {})}
        return subprocess.run(cls.command(tmp, earlier), env=environment, text=True, capture_output=True, timeout=timeout)

    def start_controlled(self, tmp, earlier=(), **kwargs):
        env = {**self.setup(tmp, **kwargs), "PI_DURABLE_NET_ANCHOR_POLL_SECONDS": self.CONTROLLED}
        # restore_signals resets only SIGPIPE, SIGXFZ and SIGXFSZ. A non-interactive shell starts its background jobs
        # with SIGINT and SIGQUIT ignored, and /bin/sh can't trap a signal that was ignored when it started, so the
        # agent would never see the test's SIGINT: these tests would then pass in the foreground and time out when
        # the suite is run with `&`. Give the agent the default dispositions launchd gives it.
        proc = subprocess.Popen(self.command(tmp, earlier), env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                preexec_fn=self.default_signals)
        self.addCleanup(self.release, tmp)
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        return proc

    @staticmethod
    def default_signals():
        import signal
        for number in (signal.SIGINT, signal.SIGQUIT, signal.SIGTERM, signal.SIGHUP):
            signal.signal(number, signal.SIG_DFL)

    @staticmethod
    def release(tmp):
        """Let a stand-in sleep still blocked on `go` end (a reader exists only if one is)."""
        try:
            fd = os.open(tmp / "state" / "go", os.O_WRONLY | os.O_NONBLOCK)
        except OSError:
            return
        os.write(fd, b"\n")
        os.close(fd)

    def await_pause(self, tmp, proc):
        """Block until the agent is in a controlled pause (a safety bound only, not a timing assumption)."""
        done = threading.Event()
        def read():
            with open(tmp / "state" / "ready") as ready:
                ready.read()
            done.set()
        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        if not done.wait(60):
            proc.kill()
            self.fail(f"the agent never paused: {proc.communicate()}")

    @staticmethod
    def step(tmp):
        with open(tmp / "state" / "go", "w") as go:
            go.write("\n")

    @staticmethod
    def state(tmp, kind):
        path = tmp / "state" / kind
        return [line for line in path.read_text().splitlines() if line] if path.exists() else []

    @staticmethod
    def parse(text):
        values = {}
        for line in text.splitlines():
            if line and not line.startswith("#"):
                key, value = line.rsplit(" ", 1)
                values[key] = int(value)
        return values

    @classmethod
    def metrics(cls, tmp):
        return cls.parse((tmp / "metrics" / "net-anchor-pi-durable-sgg.prom").read_text())

    def calls(self, tmp, verb=None):
        calls = self.state(tmp, "calls")
        return calls if verb is None else [c for c in calls if c.split(" ", 1)[0] == verb]

    def value(self, tmp, metric):
        return self.metrics(tmp)[f"pi_durable_net_anchor_{metric}{self.LABEL}"]

    def runs(self, tmp):
        return self.value(tmp, "runs_since_boot")

    def up(self, tmp):
        return self.value(tmp, "up")

    def test_absent_starts_one_detached_and_counts_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            before = int(time.time())
            run = self.run_script(tmp, present=["pi-sgg-case1"], running=["other"])
            self.assertEqual([f"run --detach --name {self.NAME} --network pi-durable-sgg --cpus 1 --memory 200M {NET_ANCHOR_IMAGE} sleep infinity"],
                             self.calls(tmp, "run"))
            self.assertEqual([], self.calls(tmp, "delete"))
            self.assertEqual(1, self.runs(tmp))
            self.assertGreaterEqual(self.value(tmp, "last_run_timestamp_seconds"), before)
            self.assertGreaterEqual(self.value(tmp, "last_check_timestamp_seconds"), before)
            self.assertEqual(0o644, (tmp / "metrics" / "net-anchor-pi-durable-sgg.prom").stat().st_mode & 0o777)
            self.assertEqual([], [p.name for p in (tmp / "metrics").iterdir() if p.name.startswith(".")])
            # It watched until the VM ended: up 0 and exit 1, for launchd to start the agent again.
            self.assertEqual(1, run.returncode, run.stdout + run.stderr)
            self.assertEqual(0, self.up(tmp))
            self.assertIn("no longer running", run.stdout)
            # The next agent starts a fresh VM and counts it.
            self.run_script(tmp)
            self.assertEqual(2, self.runs(tmp))
            self.assertEqual([], [p.name for p in tmp.iterdir() if p.name.startswith("pi-durable-net-anchor.")], "its work directory is removed")

    def test_running_is_adopted_nothing_deleted_started_or_counted(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            (tmp / "metrics").mkdir()
            started = int(time.time()) - 60
            (tmp / "metrics" / "net-anchor-pi-durable-sgg.prom").write_text(
                f'pi_durable_net_anchor_up{self.LABEL} 1\n'
                f'pi_durable_net_anchor_runs_since_boot{self.LABEL} 1\n'
                f'pi_durable_net_anchor_last_run_timestamp_seconds{self.LABEL} {started}\n')
            proc = self.start_controlled(tmp, running=[self.NAME], lists=["ok", "ok"])
            self.await_pause(tmp, proc)
            self.assertEqual(1, self.up(tmp))
            self.assertEqual(1, self.runs(tmp))
            self.assertEqual(started, self.value(tmp, "last_run_timestamp_seconds"))
            self.assertEqual([], self.calls(tmp, "run") + self.calls(tmp, "delete"))
            self.step(tmp)  # one poll: still running
            self.await_pause(tmp, proc)
            self.assertEqual(1, self.up(tmp))
            self.step(tmp)  # the next: gone
            out, _ = proc.communicate(timeout=60)
            self.assertIn("adopting", out)
            self.assertEqual(1, proc.returncode)
            self.assertEqual([], self.calls(tmp, "run") + self.calls(tmp, "delete"))
            self.assertEqual(1, self.runs(tmp))

    def test_up_0_is_written_before_the_first_container_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            (tmp / "metrics").mkdir()
            # A file left from before a reboot, reading up.
            (tmp / "metrics" / "net-anchor-pi-durable-sgg.prom").write_text(
                f'pi_durable_net_anchor_up{self.LABEL} 1\n'
                f'pi_durable_net_anchor_runs_since_boot{self.LABEL} 1\n'
                f'pi_durable_net_anchor_last_run_timestamp_seconds{self.LABEL} 1000\n')
            self.run_script(tmp, running=[self.NAME])
            first = self.parse(self.state_text(tmp, "first-seen"))
            self.assertEqual(0, first[f"pi_durable_net_anchor_up{self.LABEL}"])
            self.assertIn(f"pi_durable_net_anchor_last_check_timestamp_seconds{self.LABEL}", first)

    @staticmethod
    def state_text(tmp, kind):
        return (tmp / "state" / kind).read_text()

    def test_a_stopped_leftover_is_looked_at_again_then_removed_and_started(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            others = ["net-anchor-pi-durable-planning", "net-anchor-pi-durable-sggx", "pi-planning-service"]
            self.run_script(tmp, present=[self.NAME, *others], lists=["ok", "ok", "ok"])
            self.assertEqual([f"delete --force {self.NAME}"], self.calls(tmp, "delete"))
            self.assertEqual(1, len(self.calls(tmp, "run")))
            calls = self.calls(tmp)
            delete = calls.index(f"delete --force {self.NAME}")
            # Read three times (at start, after any wait, after the settle) before the delete.
            self.assertEqual(3, len([c for c in calls[:delete] if c == "list --quiet"]))
            self.assertLess(delete, next(i for i, c in enumerate(calls) if c.startswith("run ")))
            for other in others:
                self.assertIn(other, self.state(tmp, "present"))
            self.assertEqual(1, self.runs(tmp))

    def test_a_vm_still_starting_is_never_deleted(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            # Present but not running, then running at the second look: it was still starting.
            run = self.run_script(tmp, present=[self.NAME], lists=["ok", "ok", "promote"])
            self.assertIn("still starting", run.stdout)
            self.assertEqual([], self.calls(tmp, "delete") + self.calls(tmp, "run"))
            self.assertEqual(0, self.runs(tmp))

    def test_starts_nothing_when_it_cannot_list_remove_or_start(self):
        for name, kwargs in {"list": {"lists": ["fail", "fail"]},
                             "delete": {"present": [self.NAME], "fail": ["delete"]},
                             "run": {"fail": ["run"]}}.items():
            with self.subTest(name), tempfile.TemporaryDirectory() as tmp:
                tmp = Path(tmp)
                run = self.run_script(tmp, **kwargs)
                self.assertEqual(1, run.returncode)
                self.assertEqual([], self.state(tmp, "running"))
                self.assertEqual(0, self.up(tmp))
                self.assertEqual(0, self.runs(tmp))

    def test_a_runtime_that_isnt_answering_is_started_then_listed_again(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            run = self.run_script(tmp, lists=["fail", "ok"])
            self.assertEqual(["system start"], self.calls(tmp, "system"))
            calls = self.calls(tmp)
            self.assertLess(calls.index("system start"), next(i for i, c in enumerate(calls) if c.startswith("run ")))
            self.assertEqual(1, self.runs(tmp))
            self.assertIn("running container system start", run.stdout)
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            # A runtime that answers needs no start; one that fails to start is still listed again.
            self.run_script(tmp, running=[self.NAME], lists=["ok"])
            self.assertEqual([], self.calls(tmp, "system"))
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            run = self.run_script(tmp, lists=["fail", "ok"], fail=["system"])
            self.assertIn("container system start failed", run.stdout)
            self.assertEqual(1, len(self.calls(tmp, "run")))

    def test_a_hung_call_is_killed_and_counts_as_that_calls_failure(self):
        bound = {"PI_DURABLE_NET_ANCHOR_CALL_SECONDS": "1"}
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            # The first list hangs: a failed list, so the runtime is started and listed again.
            run = self.run_script(tmp, running=[self.NAME], lists=["hang", "ok"], env=bound)
            self.assertIn("container list did not finish within 1s", run.stdout)
            self.assertEqual(["system start"], self.calls(tmp, "system"))
            self.assertIn("adopting", run.stdout)
            self.assertEqual([], self.calls(tmp, "delete") + self.calls(tmp, "run"))
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            # A list that hangs while watching is one failed poll, not the VM gone.
            run = self.run_script(tmp, running=[self.NAME], lists=["ok", "hang", "ok"], env=bound)
            self.assertIn("container list failed (1 in a row); still watching", run.stdout)
            self.assertIn("no longer running", run.stdout)
            self.assertEqual([], self.calls(tmp, "delete") + self.calls(tmp, "run"))

    def test_one_failed_list_while_watching_is_not_the_vm_gone(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            run = self.run_script(tmp, running=[self.NAME], lists=["ok", "fail", "ok", "fail", "ok"])
            self.assertIn("container list failed (1 in a row); still watching", run.stdout)
            self.assertNotIn("(2 in a row)", run.stdout)
            # It kept watching past each failure and left only when the VM ended.
            self.assertIn("no longer running", run.stdout)
            self.assertEqual(6, len(self.calls(tmp, "list")) - len([c for c in self.calls(tmp) if "--all" in c]))
            self.assertEqual([], self.calls(tmp, "delete") + self.calls(tmp, "run"))

    def test_repeated_failed_lists_exit_without_touching_the_vm(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            run = self.run_script(tmp, running=[self.NAME], lists=["ok", "fail", "fail", "fail", "fail", "ok"])
            self.assertEqual(1, run.returncode)
            self.assertIn("failed 4 times in a row; exiting without touching the anchor VM", run.stdout)
            # Four failed polls in a row, and no poll after them.
            self.assertEqual(["ok"], self.state(tmp, "lists"))
            self.assertEqual([self.NAME], self.state(tmp, "running"))
            self.assertEqual([], self.calls(tmp, "delete") + self.calls(tmp, "run"))
            self.assertEqual(0, self.up(tmp))
        # The threshold is injectable too.
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            run = self.run_script(tmp, running=[self.NAME], lists=["ok", "fail", "fail", "ok"],
                                  env={"PI_DURABLE_NET_ANCHOR_LIST_FAILURES": "2"})
            self.assertIn("failed 2 times in a row", run.stdout)

    def test_the_gateway_is_reported_and_never_acted_on(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            proc = self.start_controlled(tmp, running=[self.NAME], lists=["ok", "ok", "ok"], addresses=["192.168.1.4"])
            self.await_pause(tmp, proc)
            self.assertEqual(1, self.up(tmp))
            self.assertEqual(0, self.value(tmp, "gateway_present"))
            # The gateway comes back: the next poll reports it.
            (tmp / "state" / "addresses").write_text(f"192.168.1.4\n{self.GATEWAY}\n")
            self.step(tmp)
            self.await_pause(tmp, proc)
            self.assertEqual(1, self.value(tmp, "gateway_present"))
            # And goes again.
            (tmp / "state" / "addresses").write_text("")
            self.step(tmp)
            self.await_pause(tmp, proc)
            self.assertEqual(0, self.value(tmp, "gateway_present"))
            self.assertEqual(1, self.up(tmp))
            proc.terminate()
            out, _ = proc.communicate(timeout=60)
            self.assertIn(f"attached but gateway {self.GATEWAY} is on no host interface", out)
            # Nothing deleted, started or restarted for it.
            self.assertEqual([], self.calls(tmp, "delete") + self.calls(tmp, "run") + self.calls(tmp, "system"))
            self.assertEqual([self.NAME], self.state(tmp, "running"))

    def test_a_start_waits_for_the_earlier_networks_gateways_reporting_down(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            earlier = ("192.168.200.1", "192.168.201.1")
            proc = self.start_controlled(tmp, earlier=earlier, addresses=[self.GATEWAY, "192.168.201.1"])
            self.await_pause(tmp, proc)
            # Waiting: nothing started, up 0 (so a wait that never ends is the Down alert's).
            self.assertEqual([], self.calls(tmp, "run"))
            self.assertEqual(0, self.up(tmp))
            self.step(tmp)
            self.await_pause(tmp, proc)
            self.assertEqual([], self.calls(tmp, "run"), "still waiting while one earlier gateway is missing")
            (tmp / "state" / "addresses").write_text(f"{self.GATEWAY}\n192.168.200.1\n192.168.201.1\n")
            self.step(tmp)
            # The next pause is the watch's, after the start.
            self.await_pause(tmp, proc)
            self.assertEqual(1, len(self.calls(tmp, "run")))
            self.assertEqual(1, self.up(tmp))
            self.assertEqual(1, self.runs(tmp))
            proc.terminate()
            out, _ = proc.communicate(timeout=60)
            self.assertIn("waiting for the earlier networks' gateways: 192.168.200.1", out)
            self.assertIn("the earlier networks' gateways are up", out)

    def test_adoption_does_not_wait_for_earlier_gateways(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            run = self.run_script(tmp, earlier=("192.168.200.1",), running=[self.NAME], lists=["ok"])
            self.assertIn("adopting", run.stdout)
            self.assertNotIn("waiting", run.stdout)
            self.assertEqual([], self.calls(tmp, "run") + self.calls(tmp, "delete"))

    def test_a_signal_ends_the_agent_and_leaves_the_vm_running(self):
        import signal
        for signal_name in ("SIGTERM", "SIGINT", "SIGHUP"):
            with self.subTest(signal_name), tempfile.TemporaryDirectory() as tmp:
                tmp = Path(tmp)
                proc = self.start_controlled(tmp)
                # In the watch's pause, after the start: no clock involved.
                self.await_pause(tmp, proc)
                self.assertEqual(1, self.up(tmp))
                proc.send_signal(getattr(signal, signal_name))
                out, _ = proc.communicate(timeout=60)
                self.assertEqual(0, proc.returncode, out)
                self.assertIn("the anchor VM is left running", out)
                self.assertEqual([self.NAME], self.state(tmp, "running"))
                self.assertEqual([], self.calls(tmp, "delete"))
                self.assertEqual(1, len(self.calls(tmp, "run")))

    def test_a_start_before_this_boot_is_not_counted(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            (tmp / "metrics").mkdir()
            (tmp / "metrics" / "net-anchor-pi-durable-sgg.prom").write_text(
                f'pi_durable_net_anchor_up{self.LABEL} 1\n'
                f'pi_durable_net_anchor_runs_since_boot{self.LABEL} 7\n'
                f'pi_durable_net_anchor_last_run_timestamp_seconds{self.LABEL} 1000\n')
            self.run_script(tmp)
            self.assertEqual(1, self.runs(tmp))

    def test_wrong_arguments_start_nothing(self):
        for args in (["pi-durable-sgg"], ["pi-durable-sgg", NET_ANCHOR_IMAGE, "/tmp/x"]):
            result = subprocess.run(["/bin/sh", str(NET_ANCHOR_SCRIPT), *args], text=True, capture_output=True)
            self.assertEqual(64, result.returncode)


SUDOERS = 'config.environment.etc."sudoers.d/10-nix-darwin-extra-config".text'
RECOVERY_SUDO_RULE = "bryan ALL=(root) NOPASSWD: /usr/bin/killall InternetSharing"
HERMES_SUDO_LINES = [
    "bryan ALL=(traci) NOPASSWD: ALL",
    "bryan ALL=(root) NOPASSWD: /bin/launchctl kickstart -k system/ai.hermes.gateway-traci, "
    "/bin/launchctl kickstart -k system/ai.hermes.serve-traci, /bin/launchctl kickstart system/ai.hermes.update-traci, "
    "/bin/cat /var/log/hermes-updates/traci.log, /bin/cat /var/log/hermes-updates/traci.error.log",
]


class NetAnchorRecoverySudo(unittest.TestCase):
    """The VM networks' recovery: exactly `/usr/bin/killall InternetSharing`, passwordless (durable-pi-workspace#418)."""

    def rules(self, settings):
        return [line for line in studio(settings, SUDOERS).splitlines() if line and not line.startswith("#")]

    def test_studio_gets_exactly_the_rule_beside_the_hermes_lines(self):
        rules = self.rules("{ }")
        # Exactly one line mentions it, and it is exactly the rule.
        self.assertEqual([RECOVERY_SUDO_RULE], [line for line in rules if "killall" in line or "InternetSharing" in line])
        # It merges with Hermes's operator rule rather than replacing it.
        for line in HERMES_SUDO_LINES:
            self.assertIn(line, rules)
        self.assertEqual(sorted([RECOVERY_SUDO_RULE, *HERMES_SUDO_LINES]),
                         sorted(line for line in rules if not line.startswith("Defaults")))

    def test_rule_names_one_command_line_and_no_wildcard(self):
        self.assertNotRegex(RECOVERY_SUDO_RULE, r"[*?\[\]!,\\]")
        self.assertEqual("/usr/bin/killall InternetSharing", RECOVERY_SUDO_RULE.split("NOPASSWD: ", 1)[1])
        rule = next(line for line in self.rules("{ }") if "InternetSharing" in line)
        self.assertNotRegex(rule.split("NOPASSWD: ", 1)[1], r"[*?\[\]!,\\]")

    def test_off_leaves_no_rule_and_the_hermes_lines(self):
        rules = self.rules("{ netAnchor.recoverySudo.enable = lib.mkForce false; }")
        self.assertEqual([], [line for line in rules if "killall" in line or "InternetSharing" in line])
        for line in HERMES_SUDO_LINES:
            self.assertIn(line, rules)

    def test_nothing_without_the_service(self):
        self.assertNotIn("InternetSharing", studio(
            "{ enable = lib.mkForce false; netAnchor.recoverySudo.enable = lib.mkForce true; }", SUDOERS))

    def test_visudo_accepts_studios_built_sudoers_text(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sudoers"
            path.write_text(studio("{ }", SUDOERS))
            result = subprocess.run(["/usr/sbin/visudo", "-c", "-f", str(path)], text=True, capture_output=True)
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
