#!/usr/bin/env python3
"""services.monitoring.blackbox.contentProbes: probes that need the right body as well as a 2xx.

Evaluates through a `path:` flake reference so an uncommitted module is
evaluated as it is on disk. The real blackbox_exporter runs on loopback with
Studio's generated config against stand-in endpoints; nothing is activated and
no Studio service is contacted.
"""
import json
import socket
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.parse
import urllib.request
from functools import cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

# What each of Studio's content probes must find, and a real healthy body for it
# (as the service answered on 2026-11-10, trimmed).
STUDIO_PROBES = {
    "Jellyfin": ("http://127.0.0.1:8096/health", "Healthy"),
    "Dashy": ("http://127.0.0.1:8088/", "<!DOCTYPE html><html><head><title>Dashy</title></head></html>"),
    "Grafana Alloy": ("http://127.0.0.1:12345/-/ready", "Alloy is ready.\n"),
    "Artifact Shelf": ("http://127.0.0.1:8092/",
                       '<!doctype html><html><head><meta name="generator" content="artifact-shelf"></head></html>'),
    "SGG pre-review": ("http://127.0.0.1:8790/metrics",
                       "# HELP sgg_intake_enabled x\nsgg_intake_enabled 1\n"
                       "# TYPE sgg_intake_cycles_total counter\nsgg_intake_cycles_total 42\n"),
    "Claude usage": ("http://127.0.0.1:8791/metrics",
                     "# HELP pi_durable_claude_window_utilization x\n"
                     "pi_durable_sampler_readings_total{workspace=\"sgg\"} 7\n"),
}

# A 200 with the wrong content: what the Hermes watchdog's content checks caught.
WRONG_BODIES = {
    "Jellyfin": "Unhealthy",
    # Hermes required the whole body to be "Healthy"; these only the anchors refuse.
    "Jellyfin, prefixed": "Not Healthy",
    "Jellyfin, suffixed": "Healthy\nbut degraded",
    "Dashy": "<html><title>502 Bad Gateway</title></html>",
    "Grafana Alloy": "Alloy is not ready",
    "Artifact Shelf": "<html><title>Artifact Shelf</title></html>",
    # Only HELP text naming the counter, not the sample itself.
    "SGG pre-review": "# HELP sgg_intake_cycles_total x\nother_metric 1\n",
    "Claude usage": "# HELP pi_durable_sampler_readings_total x\n",
}


def nix(*args):
    result = subprocess.run(["nix", *args], cwd=REPO, text=True, capture_output=True)
    if result.returncode != 0:
        raise AssertionError(result.stderr[-4000:])
    return result.stdout


def studio_expr(expr, extra="{ }"):
    return f"""
      let
        studio = (builtins.getFlake "path:{REPO}").darwinConfigurations.studio;
        inherit (studio) pkgs;
        inherit (pkgs) lib;
        config = (studio.extendModules {{ modules = [ {extra} ]; }}).config;
        flagValue = flag: args: lib.removePrefix flag (lib.findFirst (lib.hasPrefix flag) null args);
        readJSON = path: builtins.fromJSON (builtins.unsafeDiscardStringContext (builtins.readFile path));
        prometheusConfig = flagValue "--config.file="
          config.launchd.daemons.prometheus.serviceConfig.ProgramArguments;
        blackboxConfig = flagValue "--config.file="
          config.launchd.daemons.blackbox-exporter.serviceConfig.ProgramArguments;
      in {expr}
    """


@cache
def studio(expr, extra="{ }"):
    return json.loads(nix("eval", "--json", "--impure", "--expr", studio_expr(expr, extra)))


def failures(extra):
    return studio("map (a: a.message) (builtins.filter (a: !a.assertion) config.assertions)", extra)


@cache
def realised():
    out = nix("build", "--no-link", "--print-out-paths", "--impure", "--expr", studio_expr("""
      pkgs.writeText "content-probe-paths.json" (builtins.toJSON {
        prometheusConfig = prometheusConfig;
        blackboxConfig = blackboxConfig;
        blackbox = "${pkgs.prometheus-blackbox-exporter}/bin/blackbox_exporter";
        promtool = "${pkgs.prometheus.cli}/bin/promtool";
      })
    """))
    return json.loads(Path(out.strip().splitlines()[-1]).read_text())


def blackbox_job():
    jobs = studio("(readJSON prometheusConfig).scrape_configs")
    [job] = [j for j in jobs if j["job_name"] == "blackbox"]
    return job


class Wiring(unittest.TestCase):
    def test_studio_probes_the_six_services_with_their_content(self):
        probes = studio("config.services.monitoring.blackbox.contentProbes")
        self.assertEqual({name: url for name, (url, _) in STUDIO_PROBES.items()},
                         {name: p["url"] for name, p in probes.items()})

    def test_each_probe_is_a_blackbox_target_with_its_own_module(self):
        job = blackbox_job()
        modules = studio("(readJSON blackboxConfig).modules")
        groups = [g for g in job["static_configs"] if "probe" in g.get("labels", {})]
        self.assertEqual(set(STUDIO_PROBES), {g["labels"]["probe"] for g in groups})
        for group in groups:
            name = group["labels"]["probe"]
            with self.subTest(probe=name):
                self.assertEqual([STUDIO_PROBES[name][0]], group["targets"])
                module = modules[group["labels"]["__param_module"]]
                self.assertEqual(1, len(module["http"]["fail_if_body_not_matches_regexp"]))

    def test_plain_targets_keep_the_status_only_module(self):
        job = blackbox_job()
        self.assertEqual({"module": ["http_2xx"]}, job["params"])
        [plain] = [g for g in job["static_configs"] if "labels" not in g]
        self.assertIn("http://localhost:11434/api/tags", plain["targets"])
        self.assertNotIn("__param_module", json.dumps(plain))

    def test_each_probe_follows_its_service(self):
        names = "builtins.attrNames config.services.monitoring.blackbox.contentProbes"
        for extra, gone in (
            ("{ services.pi-durable.sampler.enable = lib.mkForce false; }", "Claude usage"),
            ("{ services.pi-durable.enable = lib.mkForce false; }", "SGG pre-review"),
            ("{ services.dashy.enable = lib.mkForce false; }", "Dashy"),
            ("{ services.artifact-shelf.enable = lib.mkForce false; }", "Artifact Shelf"),
        ):
            with self.subTest(disabled=gone):
                self.assertEqual(sorted(set(STUDIO_PROBES) - {gone}), studio(names, extra))

    def test_modules_evaluate_on_a_host_without_monitoring(self):
        """dashy, artifact-shelf and pi-durable add probes only where the monitoring module is imported."""
        expr = f"""
          let
            flake = builtins.getFlake "path:{REPO}";
            mbp = flake.darwinConfigurations.mbp.extendModules {{ modules = with flake.modules.darwin; [
              dashy artifact-shelf pi-durable
              ({{ lib, ... }}: {{
                services.dashy.enable = true;
                services.artifact-shelf.enable = true;
                services.pi-durable.sampler.enable = true;
              }})
            ]; }};
          in {{
            hasMonitoring = mbp.options.services ? monitoring;
            failed = map (a: a.message) (builtins.filter (a: !a.assertion) mbp.config.assertions);
            agents = builtins.filter (n: builtins.elem n [ "dashy" "artifact-shelf" "pi-durable-sampler" ])
              (builtins.attrNames mbp.config.launchd.user.agents);
          }}
        """
        result = json.loads(nix("eval", "--json", "--impure", "--expr", expr))
        self.assertEqual({"hasMonitoring": False, "failed": [],
                          "agents": ["artifact-shelf", "dashy", "pi-durable-sampler"]}, result)

    def test_service_down_covers_content_probes(self):
        rules = json.loads(Path(json.loads(Path(realised()["prometheusConfig"]).read_text())["rule_files"][0])
                           .read_text())
        [down] = [r for g in rules["groups"] for r in g["rules"] if r["alert"] == "ServiceDown"]
        self.assertEqual("probe_success == 0", down["expr"])


class Refusals(unittest.TestCase):
    def test_names_must_be_plain_and_distinct(self):
        for extra in (
            '{ services.monitoring.blackbox.contentProbes."bad/name" = { url = "http://127.0.0.1:1/"; bodyMatches = "x"; }; }',
            # Becomes the same module name as "Grafana Alloy".
            '{ services.monitoring.blackbox.contentProbes."grafana-alloy" = { url = "http://127.0.0.1:1/"; bodyMatches = "x"; }; }',
        ):
            with self.subTest(extra=extra):
                self.assertTrue(any("contentProbes names" in m for m in failures(extra)), failures(extra))
        self.assertEqual([], failures("{ }"))

    def test_a_url_is_probed_once(self):
        extra = '{ services.monitoring.blackbox.targets = lib.mkAfter [ "http://127.0.0.1:8096/health" ]; }'
        self.assertTrue(any("each URL is probed once" in m for m in failures(extra)), failures(extra))

    def test_url_must_be_one_http_token(self):
        result = subprocess.run(
            ["nix", "eval", "--json", "--impure", "--expr", studio_expr(
                "config.services.monitoring.blackbox.contentProbes",
                '{ services.monitoring.blackbox.contentProbes.X = { url = "http://a b"; bodyMatches = "x"; }; }')],
            cwd=REPO, text=True, capture_output=True)
        self.assertNotEqual(0, result.returncode, result.stdout)
        self.assertIn("url", result.stderr)


class GeneratedConfigs(unittest.TestCase):
    def test_prometheus_accepts_its_config(self):
        paths = realised()
        result = subprocess.run([paths["promtool"], "check", "config", "--syntax-only", paths["prometheusConfig"]],
                                text=True, capture_output=True)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class StandIn(ThreadingHTTPServer):
    """Answers every GET with `status` and `body`."""

    def __init__(self):
        self.status, self.body = 200, ""
        super().__init__(("127.0.0.1", 0), StandInHandler)


class StandInHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_GET(self):
        server = self.server
        assert isinstance(server, StandIn)
        data = server.body.encode()
        self.send_response(server.status)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class Blackbox(unittest.TestCase):
    """Studio's generated modules, run by the real blackbox_exporter against a stand-in endpoint."""

    @classmethod
    def setUpClass(cls):
        paths = realised()
        cls.port = free_port()
        cls.process = subprocess.Popen(
            [paths["blackbox"], f"--config.file={paths['blackboxConfig']}",
             f"--web.listen-address=127.0.0.1:{cls.port}"],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        deadline = time.monotonic() + 30
        while True:
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{cls.port}/-/healthy", timeout=1).close()
                break
            except OSError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.2)
        cls.stand_in = StandIn()
        threading.Thread(target=cls.stand_in.serve_forever, daemon=True).start()
        cls.modules = {g["labels"]["probe"]: g["labels"]["__param_module"]
                       for g in blackbox_job()["static_configs"] if "probe" in g.get("labels", {})}

    @classmethod
    def tearDownClass(cls):
        cls.stand_in.shutdown()
        cls.stand_in.server_close()
        cls.process.terminate()
        cls.process.communicate(timeout=10)

    def probe(self, name, status, body):
        self.stand_in.status, self.stand_in.body = status, body
        query = urllib.parse.urlencode({"module": self.modules[name],
                                        "target": f"http://127.0.0.1:{self.stand_in.server_port}/"})
        metrics = urllib.request.urlopen(f"http://127.0.0.1:{self.port}/probe?{query}", timeout=10).read().decode()
        [line] = [l for l in metrics.splitlines() if l.startswith("probe_success ")]
        return line.split()[1] == "1"

    def test_healthy_bodies_pass(self):
        for name, (_, body) in STUDIO_PROBES.items():
            with self.subTest(probe=name):
                self.assertTrue(self.probe(name, 200, body))

    def test_wrong_content_fails_despite_200(self):
        for case, body in WRONG_BODIES.items():
            with self.subTest(probe=case):
                self.assertFalse(self.probe(case.split(",")[0], 200, body))

    def test_server_error_fails_despite_right_content(self):
        for name, (_, body) in STUDIO_PROBES.items():
            with self.subTest(probe=name):
                self.assertFalse(self.probe(name, 500, body))


if __name__ == "__main__":
    unittest.main()
