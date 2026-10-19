#!/usr/bin/env python3
"""Studio's monitoring, Open WebUI and Ollama listeners stay off the LAN.

Every listener binds 127.0.0.1 except Alloy's syslog receiver (UDP 1514, for
Unraid). Grafana, Prometheus and the Alloy UI are published tailnet-only over
HTTPS through Tailscale Serve on their own port numbers.
"""
import json
import subprocess
import unittest
from functools import cache
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
STUDIO = "darwinConfigurations.studio.config"
TAILNET_HOST = "bryans-mac-studio.tail5ba690.ts.net"


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
def activation():
    return (build_studio() / "activate").read_text()


def program_arguments(kind, name):
    return nix_eval_json(f"{STUDIO}.launchd.{kind}.{name}.serviceConfig.ProgramArguments")


def flag_value(args, flag):
    values = [arg.split("=", 1)[1] for arg in args if arg.startswith(f"{flag}=")]
    if len(values) != 1:
        raise AssertionError(f"expected exactly one {flag} in {args}")
    return values[0]


class MonitoringListenerTests(unittest.TestCase):
    def test_metrics_listeners_bind_loopback(self):
        listeners = {
            ("daemons", "prometheus"): ("--web.listen-address", "127.0.0.1:9090"),
            ("daemons", "blackbox-exporter"): ("--web.listen-address", "127.0.0.1:9115"),
            ("user.agents", "node-exporter"): ("--web.listen-address", "127.0.0.1:9100"),
            ("user.agents", "alloy"): ("--server.http.listen-addr", "127.0.0.1:12345"),
        }
        for (kind, name), (flag, expected) in listeners.items():
            with self.subTest(service=name):
                self.assertEqual(expected, flag_value(program_arguments(kind, name), flag))

    def test_grafana_binds_loopback_with_its_tailnet_root_url(self):
        environment = nix_eval_json(
            f"{STUDIO}.launchd.user.agents.grafana.serviceConfig.EnvironmentVariables"
        )
        self.assertEqual("127.0.0.1", environment["GF_SERVER_HTTP_ADDR"])
        self.assertEqual(f"https://{TAILNET_HOST}:33000/", environment["GF_SERVER_ROOT_URL"])

    def test_prometheus_links_open_over_the_tailnet(self):
        self.assertEqual(
            f"https://{TAILNET_HOST}:9090/",
            flag_value(program_arguments("daemons", "prometheus"), "--web.external-url"),
        )

    def test_loki_http_and_grpc_bind_loopback(self):
        config_file = flag_value(program_arguments("user.agents", "loki"), "-config.file")
        build_studio()
        server = json.loads(Path(config_file).read_text())["server"]
        self.assertEqual("127.0.0.1", server["http_listen_address"])
        self.assertEqual("127.0.0.1", server["grpc_listen_address"])

    def test_only_alloy_syslog_is_registered_with_the_firewall(self):
        script = activation()
        start = script.index("# === monitoring: firewall rules ===")
        end = script.index("# === monitoring: Grafana via Tailscale Serve ===", start)
        firewall = script[start:end]
        self.assertIn("--unblock /opt/homebrew/opt/grafana-alloy/bin/alloy", firewall)
        for binary in ("prometheus", "grafana/bin/grafana", "node_exporter", "loki/bin/loki", "blackbox_exporter"):
            with self.subTest(binary=binary):
                self.assertNotIn(binary, firewall)

    def test_web_uis_are_served_tailnet_only_on_their_own_ports(self):
        script = activation()
        for name, port in (("Grafana", 33000), ("Prometheus", 9090), ("Alloy", 12345)):
            with self.subTest(service=name):
                block = script[script.index(f"# === monitoring: {name} via Tailscale Serve ===") :]
                block = block[: block.index("fi\n")]
                self.assertIn(f"--https={port}", block)
                self.assertIn(f"http://127.0.0.1:{port};", block)
                self.assertNotIn("funnel", block)

    def test_unauthenticated_backends_are_not_served(self):
        script = activation()
        for port in (3100, 9093, 9095, 9100, 9115):
            with self.subTest(port=port):
                self.assertNotIn(f"--https={port}", script)
                self.assertNotIn(f"--tcp={port}", script)

    def test_dashy_links_follow_the_served_ports(self):
        self.assertEqual(
            f"https://{TAILNET_HOST}:9090", nix_eval_json(f"{STUDIO}.services.dashy.prometheusUrl")
        )
        self.assertEqual(
            f"https://{TAILNET_HOST}:12345", nix_eval_json(f"{STUDIO}.services.dashy.alloyUrl")
        )


class LocalModelListenerTests(unittest.TestCase):
    def test_open_webui_serves_on_loopback_only(self):
        runner = Path(program_arguments("user.agents", "open-webui")[0])
        build_studio()
        self.assertIn('serve --host 127.0.0.1 --port 8080', runner.read_text())
        self.assertNotIn("# === open-webui firewall ===", activation())

    def test_ollama_keeps_its_default_cors_origins(self):
        environment = nix_eval_json(
            f"{STUDIO}.launchd.user.agents.ollama.serviceConfig.EnvironmentVariables"
        )
        self.assertEqual("127.0.0.1:11434", environment["OLLAMA_HOST"])
        self.assertNotIn("OLLAMA_ORIGINS", environment)


if __name__ == "__main__":
    unittest.main()
