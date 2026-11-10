#!/usr/bin/env python3
"""Tests for Dashy's read-only view of Prometheus's probe results.

A stand-in Prometheus answers /api/v1/query with what each test sets, and
records every query, so the tests see exactly what the adapter asks.
"""

from __future__ import annotations

import http.client
import importlib.util
import json
import subprocess
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "modules" / "services" / "dashy-status-api.py"
PROBES = ["Grafana", "Hermes Dashboard", "SGG pre-review"]


def load_module():
    spec = importlib.util.spec_from_file_location("dashy_status_api", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def vector(*values: str) -> dict:
    return {
        "status": "success",
        "data": {
            "resultType": "vector",
            "result": [{"metric": {}, "value": [time.time(), v]} for v in values],
        },
    }


class StandInPrometheus(ThreadingHTTPServer):
    """Answers each query with `answers[kind]`, kind "latest" or "recent"."""

    daemon_threads = True
    # Prometheus (Go) listens with the kernel's full backlog; Python's
    # default of 5 before 3.15 would drop a burst before the adapter's turn.
    request_queue_size = 128

    def __init__(self) -> None:
        self.answers: dict[str, object] = {"latest": vector("1"), "recent": vector("1")}
        self.status = 200
        self.queries: list[str] = []
        super().__init__(("127.0.0.1", 0), StandInHandler)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_port}"


class StandInHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        return

    def do_GET(self) -> None:
        server = self.server
        assert isinstance(server, StandInPrometheus)
        parsed = urlparse(self.path)
        assert parsed.path == "/api/v1/query", parsed.path
        expression = parse_qs(parsed.query)["query"][0]
        server.queries.append(expression)
        answer = server.answers["recent" if expression.startswith("max_over_time(") else "latest"]
        body = answer if isinstance(answer, bytes) else json.dumps(answer).encode()
        self.send_response(server.status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class PrometheusTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.module = load_module()

    def setUp(self) -> None:
        self.prometheus = StandInPrometheus()
        thread = threading.Thread(target=self.prometheus.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(self.prometheus.server_close)
        self.addCleanup(self.prometheus.shutdown)

    def status(self, name: str = "Grafana") -> tuple[int, dict]:
        return self.module.probe_status(self.prometheus.url, frozenset(PROBES), name)


class ProbeStatusTest(PrometheusTestCase):
    def test_passing_probe_is_green(self) -> None:
        status, payload = self.status()
        self.assertEqual(status, 200)
        self.assertIs(payload["successStatus"], True)
        self.assertEqual(payload["statusCode"], 200)
        self.assertEqual(payload["statusText"], "Healthy")

    def test_queries_the_named_blackbox_probe_over_servicedowns_window(self) -> None:
        self.status("Hermes Dashboard")
        selector = 'probe_success{job="blackbox",probe="Hermes Dashboard"}'
        self.assertEqual(self.prometheus.queries, [selector, f"max_over_time({selector}[5m])"])

    def test_failure_inside_the_window_stays_green_and_says_so(self) -> None:
        self.prometheus.answers = {"latest": vector("0"), "recent": vector("1")}
        status, payload = self.status()
        self.assertEqual(status, 200)
        self.assertIs(payload["successStatus"], True)
        self.assertEqual(payload["statusText"], "Probe failed")
        self.assertIn("failed", payload["message"])

    def test_failing_for_the_whole_window_is_red(self) -> None:
        self.prometheus.answers = {"latest": vector("0"), "recent": vector("0")}
        status, payload = self.status()
        self.assertEqual(status, 200)
        self.assertIs(payload["successStatus"], False)
        self.assertEqual(payload["statusCode"], 503)
        self.assertEqual(payload["statusText"], "Unhealthy")

    def test_any_failing_series_counts(self) -> None:
        # Two series under one name (say, mid-relabel): one failing is enough.
        self.prometheus.answers = {"latest": vector("1", "0"), "recent": vector("0", "0")}
        _, payload = self.status()
        self.assertIs(payload["successStatus"], False)

    def test_no_series_is_red_as_no_data(self) -> None:
        self.prometheus.answers = {"latest": vector(), "recent": vector()}
        status, payload = self.status()
        self.assertEqual(status, 200)
        self.assertIs(payload["successStatus"], False)
        self.assertEqual(payload["statusText"], "No data")

    def test_prometheus_failures_are_unavailable_without_detail(self) -> None:
        cases = {
            "server error": (500, vector("1")),
            "query error": (200, {"status": "error", "error": "secret detail"}),
            "not JSON": (200, b"<html>secret detail</html>"),
            "matrix": (200, {"status": "success", "data": {"resultType": "matrix", "result": []}}),
            "bad value": (200, {"status": "success", "data": {"resultType": "vector",
                                                              "result": [{"value": [0, "x"]}]}}),
        }
        for case, (code, answer) in cases.items():
            with self.subTest(case=case):
                self.prometheus.status = code
                self.prometheus.answers = {"latest": answer, "recent": answer}
                status, payload = self.status()
                self.assertEqual(status, 503)
                self.assertIs(payload["successStatus"], False)
                self.assertEqual(payload["statusText"], "Prometheus unavailable")
                self.assertNotIn("secret", json.dumps(payload))

    def test_prometheus_down_is_unavailable(self) -> None:
        status, payload = self.module.probe_status("http://127.0.0.1:9", frozenset(PROBES), "Grafana")
        self.assertEqual(status, 503)
        self.assertEqual(payload["statusText"], "Prometheus unavailable")

    def test_oversized_answer_is_refused(self) -> None:
        # Valid JSON one byte over the cap: only the size check refuses it.
        big = vector("1")
        big["padding"] = ""
        big["padding"] = "x" * (self.module.MAX_QUERY_BYTES + 1 - len(json.dumps(big).encode()))
        self.assertEqual(len(json.dumps(big).encode()), self.module.MAX_QUERY_BYTES + 1)
        self.prometheus.answers = {"latest": big, "recent": big}
        status, _ = self.status()
        self.assertEqual(status, 503)
        # The same answer at the cap is read.
        big["padding"] = big["padding"][1:]
        self.prometheus.answers = {"latest": big, "recent": big}
        self.assertEqual(self.status()[0], 200)

    def test_unlisted_name_is_refused_without_a_query(self) -> None:
        for name in ("Plex", 'Grafana"} or vector(1) or {x="', "Grafana\n"):
            with self.subTest(name=name):
                status, payload = self.status(name)
                self.assertEqual(status, 404)
                self.assertIs(payload["successStatus"], False)
        self.assertEqual(self.prometheus.queries, [])


class TargetParsingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.module = load_module()

    def test_probe_url_parser_accepts_only_probe_names(self) -> None:
        self.assertEqual(self.module.probe_name_from_target("probe:///Hermes%20Dashboard"), "Hermes Dashboard")
        self.assertEqual(self.module.probe_name_from_target("probe:///SGG pre-review"), "SGG pre-review")
        for target in (
            "http://127.0.0.1:8888/health",
            "https://example.com",
            "watchdog:///Grafana",
            "probe://",
            "probe:///",
            "probe://host/Grafana",
            "probe:///Grafana?x=1",
            'probe:///Grafana%22%7D',
            "probe:///a%0Ab",
            "probe:///-leading",
        ):
            with self.subTest(target=target):
                self.assertIsNone(self.module.probe_name_from_target(target))

    def test_status_route_accepts_dashy_trailing_slash(self) -> None:
        self.assertTrue(self.module.is_status_path("/status-check"))
        self.assertTrue(self.module.is_status_path("/status-check/"))
        self.assertFalse(self.module.is_status_path("/status-check/other"))


class ServerTest(PrometheusTestCase):
    """The adapter served over HTTP, as Caddy reaches it."""

    BURST = 64

    def setUp(self) -> None:
        super().setUp()
        self.threads_before = set(threading.enumerate())
        self.server = self.module.build_server("127.0.0.1", 0, self.prometheus.url, PROBES)
        self.serve_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.serve_thread.start()
        self.addCleanup(self.stop_server)

    def stop_server(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.serve_thread.join(timeout=5)
        self.assertEqual(self.server.socket.fileno(), -1)

    def get(self, path: str) -> tuple[int, dict]:
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=10)
        try:
            connection.request("GET", path, headers={"Connection": "close"})
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_dashy_request_reaches_the_named_probe(self) -> None:
        # Dashy sends its statusCheckUrl URL-encoded in `url`.
        status, payload = self.get("/status-check/?&url=" + quote("probe:///Hermes%20Dashboard", safe=""))
        self.assertEqual(status, 200)
        self.assertEqual(payload["statusText"], "Healthy")
        self.assertIn('probe="Hermes Dashboard"', self.prometheus.queries[0])

    def test_other_targets_are_refused(self) -> None:
        for target in ("watchdog:///Grafana", "http://127.0.0.1:9090/api/v1/query"):
            with self.subTest(target=target):
                status, payload = self.get("/status-check/?url=" + quote(target, safe=""))
                self.assertEqual(status, 400)
                self.assertIs(payload["successStatus"], False)
        self.assertEqual(self.prometheus.queries, [])

    def test_health_and_unknown_paths(self) -> None:
        self.assertEqual(self.get("/health"), (200, {"status": "healthy"}))
        self.assertEqual(self.get("/api/v1/query?query=up")[0], 404)

    def test_burst_of_fresh_connections_all_get_answers(self) -> None:
        """Dashy checks every tile at once on page load; none may be dropped."""
        port = self.server.server_address[1]
        start = threading.Barrier(self.BURST)
        results: list[object] = [None] * self.BURST

        def run(index: int) -> None:
            start.wait(timeout=5)
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            try:
                connection.request("GET", "/status-check/?&url=probe:///Grafana", headers={"Connection": "close"})
                response = connection.getresponse()
                results[index] = (response.status, json.loads(response.read()))
            except (OSError, http.client.HTTPException, ValueError) as error:
                results[index] = error
            finally:
                connection.close()

        clients = [threading.Thread(target=run, args=(i,)) for i in range(self.BURST)]
        for client in clients:
            client.start()
        for client in clients:
            client.join(timeout=15)

        failures = [r for r in results if not (isinstance(r, tuple) and r[0] == 200)]
        self.assertEqual(failures, [], f"{len(failures)} of {self.BURST} requests failed")
        for _, payload in results:  # type: ignore[misc]
            self.assertEqual(payload["statusText"], "Healthy")

    def test_listen_backlog_holds_a_dashy_burst(self) -> None:
        # Interpreters disagree on the default (5 before Python 3.15, 128 after),
        # so pin it: the burst test alone passes on 3.15 whatever the adapter sets.
        self.assertGreaterEqual(self.server.request_queue_size, self.BURST)
        self.assertIs(self.server.daemon_threads, True)


def nix_eval(expr: str):
    result = subprocess.run(["nix", "eval", "--json", "--impure", "--expr", expr],
                            cwd=REPO, text=True, capture_output=True)
    if result.returncode != 0:
        raise AssertionError(result.stderr[-4000:])
    return json.loads(result.stdout)


def studio_dashy(extra: str = "{ }") -> str:
    """A Nix expression for Studio's Dashy wiring, built through a `path:` flake reference."""
    return f"""
      let
        studio = (builtins.getFlake "path:{REPO}").darwinConfigurations.studio
          .extendModules {{ modules = [ ({extra}) ]; }};
        inherit (studio) config pkgs;
        inherit (pkgs) lib;
        caddyArgs = config.launchd.user.agents.dashy.serviceConfig.ProgramArguments;
        caddy = builtins.fromJSON (builtins.unsafeDiscardStringContext (builtins.readFile (builtins.elemAt caddyArgs 3)));
        routes = caddy.apps.http.servers.dashy.routes;
        root = (lib.last routes).handle;
        status = config.launchd.user.agents.dashy-status or {{ }};
        args = status.serviceConfig.ProgramArguments or [ ];
        flagValues = flag: lib.concatLists (lib.imap0 (i: a: lib.optional (a == flag) (builtins.elemAt args (i + 1))) args);
      in {{
        root = (builtins.head root).root;
        statusRoute = builtins.length routes == 2;
        statusAgent = config.launchd.user.agents ? dashy-status;
        probes = flagValues "--probe";
        prometheus = flagValues "--prometheus";
        contentProbes = builtins.attrNames config.services.monitoring.blackbox.contentProbes;
        failed = map (a: a.message) (builtins.filter (a: !a.assertion) config.assertions);
      }}
    """


def dashy_conf(root: str) -> dict:
    """appConfig's statusCheck and each tile's statusCheck and statusCheckUrl, by title, from the built conf.yml.

    The build writes sorted YAML: a section's items are `  - key: value` blocks
    whose further keys sit at four spaces.
    """
    app_check, tiles, item = None, {}, None
    for line in Path(root, "conf.yml").read_text().splitlines():
        if line.startswith("  statusCheck: "):
            app_check = line.split(": ", 1)[1]
        if line.startswith("  - ") and not line.startswith("  - path:"):
            item = {}
            tiles.setdefault("", []).append(item)
            line = "    " + line[4:]
        if item is not None and line.startswith("    ") and not line.startswith("     ") and ": " in line:
            key, value = line.split(": ", 1)
            item[key.strip()] = value.strip()
    by_title = {}
    for item in tiles.get("", []):
        if "title" in item and "url" in item:
            by_title[item["title"]] = {k: v for k, v in item.items() if k.startswith("statusCheck")}
    return {"app": app_check, "tiles": by_title}


@unittest.skipUnless(subprocess.run(["which", "nix"], capture_output=True).returncode == 0, "needs nix")
class StudioWiringTest(unittest.TestCase):
    """Studio's Dashy, evaluated and built: every dot asks for a probe the adapter answers."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.studio = nix_eval(studio_dashy())
        cls.conf = dashy_conf(cls.studio["root"])

    def test_every_dot_names_a_probe_the_adapter_answers(self) -> None:
        self.assertEqual(self.studio["failed"], [])
        self.assertEqual(sorted(self.studio["probes"]), sorted(self.studio["contentProbes"]))
        self.assertEqual(self.studio["prometheus"], ["http://127.0.0.1:9090"])
        self.assertEqual(self.conf["app"], "true")
        dotted = {}
        for title, tile in self.conf["tiles"].items():
            if tile.get("statusCheck") == "true":
                url = tile["statusCheckUrl"]
                dotted[title] = load_module().probe_name_from_target(url)
        self.assertTrue(all(name in self.studio["probes"] for name in dotted.values()), dotted)
        # Every service the Hermes watchdog checks keeps its dot.
        self.assertEqual(
            {"Hermes Dashboard", "Open WebUI", "Grafana", "Artifact Shelf", "SGG pre-review", "Claude usage",
             "Planning", "Plex", "Jellyfin", "Prometheus", "Grafana Alloy", "Alertmanager", "Loki", "Ollama",
             "Syncthing"},
            set(dotted.values()),
        )
        self.assertEqual(self.conf["tiles"]["Tailscale"], {"statusCheck": "false"})

    def test_a_tile_without_a_probe_has_no_dot(self) -> None:
        # Alertmanager is probed only with alerting on.
        result = nix_eval(studio_dashy(
            "{ lib, ... }: { services.monitoring.alertEmail = lib.mkForce null;"
            " services.monitoring.alertWebhook = lib.mkForce null; }"))
        self.assertEqual(result["failed"], [])
        self.assertNotIn("Alertmanager", result["probes"])
        self.assertEqual(dashy_conf(result["root"])["tiles"]["Alertmanager"], {"statusCheck": "false"})

    def test_a_pi_durable_link_follows_its_service(self) -> None:
        # A page's link, like its probe, exists only while its service runs.
        for setting, gone in (
            ("sampler.enable = lib.mkForce false;", "Claude usage"),
            ("forgeBroker.enable = lib.mkForce false; vmBroker.enable = lib.mkForce false;"
             " planning.enable = lib.mkForce false;", "Planning"),
            ("planning.tailscaleHttpsPort = lib.mkForce null;", "Planning"),
        ):
            with self.subTest(gone=gone, setting=setting):
                result = nix_eval(studio_dashy(f"{{ lib, ... }}: {{ services.pi-durable = {{ {setting} }}; }}"))
                self.assertEqual(result["failed"], [])
                tiles = dashy_conf(result["root"])["tiles"]
                self.assertNotIn(gone, tiles)
                self.assertIn("SGG pre-review", tiles)

    def test_a_link_naming_no_probe_is_refused(self) -> None:
        result = nix_eval(studio_dashy(
            '{ lib, ... }: { services.dashy.dailyLinks = lib.mkAfter [ { title = "X"; description = "x";'
            ' url = "https://x"; icon = "x"; statusName = "Nowhere"; } ]; }'))
        self.assertTrue(any("statusName" in m and "Nowhere" in m for m in result["failed"]), result["failed"])

    def test_without_monitoring_there_are_no_dots_or_adapter(self) -> None:
        result = nix_eval(studio_dashy("{ lib, ... }: { services.monitoring.enable = lib.mkForce false; }"))
        self.assertFalse(result["statusAgent"])
        self.assertFalse(result["statusRoute"])
        conf = dashy_conf(result["root"])
        self.assertEqual(conf["app"], "false")


if __name__ == "__main__":
    unittest.main()
