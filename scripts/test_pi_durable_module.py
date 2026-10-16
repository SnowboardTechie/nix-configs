#!/usr/bin/env python3
"""services.pi-durable: Studio stays uncommissioned; enabling fails closed.

Evaluates through a `path:` flake reference so an uncommitted module is
evaluated as it is on disk (a git flake hides untracked files).
"""
import json
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from functools import cache
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
ROUTE_SCRIPT = REPO / "modules" / "services" / "pi-durable-page-route.sh"
HOME = "/Users/bryan"


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
ENABLED = "{ enable = lib.mkForce true; }"
PROXY = '{ enable = lib.mkForce true; proxy = { listenAddress = "192.168.128.1"; allowedClients = [ "192.168.128.0/24" ]; }; }'


class StudioStaysUncommissioned(unittest.TestCase):
    """The ordinary Studio update installs, starts, exposes and routes nothing."""

    def test_imported_but_disabled(self):
        s = studio("{ }", SUMMARY)
        self.assertFalse(s["enable"])
        self.assertEqual([], s["failed"])
        for name in s["agents"] + s["system"]:
            self.assertNotIn("pi-durable", name)
        for item in s["brews"] + s["casks"] + s["packages"]:
            self.assertNotIn("tinyproxy", item)
            self.assertNotIn("container", item)
        self.assertNotIn("pi-durable", s["activation"])
        self.assertNotIn("tinyproxy", s["activation"])


class EnabledWithoutCommissioningInputs(unittest.TestCase):
    """Enabled alone: the paused workspace agent, and no proxy, firewall hole or route."""

    def test_agent_runs_the_contract_command_without_secrets(self):
        agent = studio(ENABLED, "config.launchd.user.agents.pi-durable-sgg.serviceConfig")
        self.assertEqual(
            [
                "/opt/homebrew/bin/node",
                f"{HOME}/code/durable-pi-workspace/dist/workspaces/sgg/src/main.js",
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
        self.assertNotIn("tinyproxy", s["activation"])
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


def proxy(listen='"192.168.128.1"', clients='[ "192.168.128.0/24" ]'):
    return f"{{ enable = lib.mkForce true; proxy = {{ listenAddress = {listen}; allowedClients = {clients}; }}; }}"


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
            [proxy(listen=value) for value in (
                '"0.0.0.0"', '"999.1.1.1"', '"192.168.128.256"', '"192.168.128.01"', '"192.168.128"',
                '"192.168.128.1 "', r'"192.168.128.1\nListen 0.0.0.0"', '"studio.local"', '"::"', '""',
            )],
        )

    def test_clients_are_canonical_ipv4_addresses_or_cidrs(self):
        self.assertOnlyRefuses(
            "proxy.allowedClients",
            [proxy(clients=clients) for clients in (
                '[ "192.168.128.0/24" ]',
                '[ "192.168.128.0/24" "192.168.128.5" ]',
                '[ "10.0.0.0/8" "192.168.128.7/32" ]',
            )],
            [proxy(clients=clients) for clients in (
                "[ ]", '[ "0.0.0.0/0" ]', '[ "0.0.0.0/00" ]', '[ "0.0.0.1/0" ]', '[ "192.168.128.0/33" ]',
                '[ "192.168.128.0/024" ]', '[ "256.168.128.0/24" ]', '[ "010.0.0.0/8" ]', '[ "192.168.128.0/" ]',
                r'[ "192.168.128.0/24\nConnectPort 22" ]', '[ "192.168.128.0/24 0.0.0.0/0" ]',
                '[ " 192.168.128.0/24" ]', '[ "192.168.128.0/24" "0.0.0.0" ]', '[ "::/0" ]', '[ "localhost" ]', '[ "" ]',
            )],
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
              services.pi-durable = {{ enable = lib.mkForce true; page.tailscaleHttpsPort = 8443; }};
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
            "{ enable = lib.mkForce true; page.tailscaleHttpsPort = 8443; }",
            "config.system.activationScripts.extraActivation.text",
        )
        self.assertIn("pi-durable-page-route.sh", activation)
        self.assertIn("8443 http://127.0.0.1:8790", activation)


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
            '{ enable = lib.mkForce true; proxy = { listenAddress = "127.0.0.1"; port = %d; '
            'allowedClients = [ "127.0.0.1" ]; '
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


if __name__ == "__main__":
    unittest.main()
