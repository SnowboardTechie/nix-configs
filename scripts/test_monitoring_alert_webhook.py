#!/usr/bin/env python3
"""services.monitoring.alertWebhook: Alertmanager notifications also posted to a token-authenticated webhook.

On Studio the webhook is Hermes's `studio-alerts` route, which posts into an
encrypted Matrix room; Hermes owns that side and it is not exercised here.
Evaluates through a `path:` flake reference so an uncommitted module is
evaluated as it is on disk. The real Alertmanager runs on loopback against a
stand-in endpoint; nothing is activated and Hermes is never contacted.
"""
import json
import socket
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.request
from functools import cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
TOKEN = "s3cret-token-value"
HERMES_URL = "http://127.0.0.1:8644/webhooks/studio-alerts"
HERMES_TOKEN_FILE = "/Users/bryan/.secrets/hermes-alertmanager-webhook-token"


def webhook(**fields):
    """A `services.monitoring.alertWebhook` attrset literal with `fields` replacing the defaults."""
    values = {"url": "http://127.0.0.1:8644/webhooks/alerts", "tokenHeader": "X-Gitlab-Token",
              "tokenFile": "/Users/bryan/.secrets/alert-webhook-token", **fields}
    return "{ " + " ".join(f"{k} = {json.dumps(v)};" for k, v in values.items()) + " }"


def nix(*args):
    result = subprocess.run(["nix", *args], cwd=REPO, text=True, capture_output=True)
    if result.returncode != 0:
        raise AssertionError(result.stderr[-4000:])
    return result.stdout


def studio_expr(settings, expr, extra="{ }"):
    """`expr` with `config`, `lib` and `pkgs` bound to Studio, optionally with `alertWebhook = settings`."""
    override = "" if settings is None else f"{{ services.monitoring.alertWebhook = lib.mkForce ({settings}); }}"
    return f"""
      let
        studio = (builtins.getFlake "path:{REPO}").darwinConfigurations.studio;
        inherit (studio) pkgs;
        inherit (pkgs) lib;
        config = (studio.extendModules {{ modules = [ {override} {extra} ]; }}).config;
        flagValue = flag: args: lib.removePrefix flag (lib.findFirst (lib.hasPrefix flag) null args);
        # The generated configs name other store paths, which fromJSON refuses.
        readJSON = path: builtins.fromJSON (builtins.unsafeDiscardStringContext (builtins.readFile path));
        alertmanagerConfig = flagValue "--config.file="
          config.launchd.user.agents.alertmanager.serviceConfig.ProgramArguments;
        prometheusConfig = flagValue "--config.file="
          config.launchd.daemons.prometheus.serviceConfig.ProgramArguments;
      in {expr}
    """


@cache
def studio(settings, expr):
    return json.loads(nix("eval", "--json", "--impure", "--expr", studio_expr(settings, expr)))


@cache
def realised():
    """Studio's generated configs and the tools that check and run them, built locally."""
    out = nix("build", "--no-link", "--print-out-paths", "--impure", "--expr", studio_expr(None, """
      pkgs.writeText "alert-webhook-paths.json" (builtins.toJSON {
        alertmanagerConfig = alertmanagerConfig;
        prometheusConfig = prometheusConfig;
        alertmanager = "${pkgs.prometheus-alertmanager}/bin/alertmanager";
        amtool = "${pkgs.prometheus-alertmanager}/bin/amtool";
        promtool = "${pkgs.prometheus.cli}/bin/promtool";
      })
    """))
    return json.loads(Path(out.strip().splitlines()[-1]).read_text())


def failures(settings, extra="{ }"):
    return json.loads(nix("eval", "--json", "--impure", "--expr", studio_expr(
        settings, "map (a: a.message) (builtins.filter (a: !a.assertion) config.assertions)", extra)))


def refusal(settings):
    """The evaluation error for a setting the option type itself refuses."""
    result = subprocess.run(
        ["nix", "eval", "--json", "--impure", "--expr", studio_expr(settings, "readJSON alertmanagerConfig")],
        cwd=REPO, text=True, capture_output=True,
    )
    if result.returncode == 0:
        raise AssertionError(f"accepted {settings}: {result.stdout}")
    return result.stderr


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Wiring(unittest.TestCase):
    def test_studio_posts_to_hermes_alongside_email(self):
        am = studio(None, "readJSON alertmanagerConfig")
        self.assertEqual("primary", am["route"]["receiver"])
        [receiver] = am["receivers"]
        self.assertEqual([{"to": "bryan@snowboardtechie.com", "send_resolved": True}], receiver["email_configs"])
        self.assertEqual([{
            "url": HERMES_URL, "send_resolved": True,
            "http_config": {"http_headers": {"X-Gitlab-Token": {"files": [HERMES_TOKEN_FILE]}}},
        }], receiver["webhook_configs"])

    def test_unset_leaves_delivery_email_only(self):
        [receiver] = studio("null", "(readJSON alertmanagerConfig).receivers")
        self.assertEqual({"name", "email_configs"}, set(receiver))

    def test_alertmanager_is_scraped_and_delivery_failures_alert(self):
        prometheus = studio("null", "readJSON prometheusConfig")
        jobs = {job["job_name"]: job for job in prometheus["scrape_configs"]}
        self.assertEqual([{"targets": ["127.0.0.1:9093"]}], jobs["alertmanager"]["static_configs"])
        rules = json.loads(Path(realised()["prometheusConfig"]).read_text())["rule_files"]
        names = [r["alert"] for g in json.loads(Path(rules[0]).read_text())["groups"] for r in g["rules"]]
        self.assertIn("AlertDeliveryFailed", names)

    def test_token_stays_out_of_the_store_and_the_plist(self):
        agent = studio(None, "config.launchd.user.agents.alertmanager.serviceConfig")
        self.assertIsNone(agent["EnvironmentVariables"])
        self.assertNotIn(TOKEN, Path(realised()["alertmanagerConfig"]).read_text())

    def test_activation_warns_when_the_token_is_missing(self):
        activation = studio(None, "config.system.activationScripts.extraActivation.text")
        self.assertIn("# === alertmanager: webhook preflight ===", activation)
        self.assertIn(HERMES_TOKEN_FILE, activation)


class Refusals(unittest.TestCase):
    def test_requires_email_delivery(self):
        failed = failures(None, "{ services.monitoring.alertEmail = lib.mkForce null; }")
        self.assertTrue(any("set services.monitoring.alertEmail" in m for m in failed), failed)
        self.assertEqual([], failures(None))

    def test_values_must_be_single_well_formed_tokens(self):
        for field, value in (("url", "127.0.0.1:8644/webhooks/alerts"),
                             ("url", "http://127.0.0.1:8644/a\nb"),
                             ("tokenHeader", "X-Token: injected"),
                             ("tokenHeader", ""),
                             ("tokenFile", "relative/token"),
                             ("tokenFile", "/Users/bryan/a b")):
            with self.subTest(field=field, value=value):
                self.assertIn(field, refusal(webhook(**{field: value})))


class GeneratedConfigs(unittest.TestCase):
    def test_alertmanager_accepts_its_config(self):
        paths = realised()
        result = subprocess.run([paths["amtool"], "check-config", paths["alertmanagerConfig"]],
                                text=True, capture_output=True)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)

    def test_prometheus_accepts_its_config_and_rules(self):
        paths = realised()
        result = subprocess.run([paths["promtool"], "check", "config", "--syntax-only", paths["prometheusConfig"]],
                                text=True, capture_output=True)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)

    def test_delivery_failure_rule_fires_per_failing_integration(self):
        paths = realised()
        rules_file = json.loads(Path(paths["prometheusConfig"]).read_text())["rule_files"][0]
        target = 'instance="127.0.0.1:9093",job="alertmanager"'

        def series(integration, reason, values):
            return {"series": f'alertmanager_notifications_failed_total{{{target},'
                              f'integration="{integration}",reason="{reason}"}}', "values": values}

        def firing(integration):
            return [{"exp_labels": {"instance": "127.0.0.1:9093", "job": "alertmanager",
                                    "integration": integration, "severity": "warning"},
                     "exp_annotations": {
                         "summary": f"Alert notifications via {integration} are failing",
                         "description": f"Alertmanager gave up on at least one {integration} notification "
                                        "in the last 15 minutes; see /tmp/alertmanager.log."}}]

        def case(inputs, *expectations):
            return {"interval": "1m", "input_series": inputs, "alert_rule_test": [
                {"eval_time": t, "alertname": "AlertDeliveryFailed", "exp_alerts": exp}
                for t, exp in expectations]}

        spec = {"rule_files": [rules_file], "evaluation_interval": "1m", "tests": [
            # Healthy: every counter Alertmanager pre-creates stays at zero.
            case([series("webhook", "serverError", "0x60"), series("email", "other", "0x60")],
                 ("30m", []), ("60m", [])),
            # One webhook failure at 20m (Hermes down or answering 502) fires, then clears.
            case([series("webhook", "serverError", "0x19 1x40"), series("email", "other", "0x60")],
                 ("10m", []), ("25m", firing("webhook")), ("45m", [])),
            # Failures for different reasons on one integration are one alert.
            case([series("email", "other", "0x19 1x40"), series("email", "serverError", "0x19 1x40")],
                 ("25m", firing("email"))),
        ]}
        with tempfile.TemporaryDirectory() as tmp:
            test_file = Path(tmp) / "alert-delivery-rule-test.json"
            test_file.write_text(json.dumps(spec))
            result = subprocess.run([paths["promtool"], "test", "rules", str(test_file)],
                                    text=True, capture_output=True)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)


class FakeEndpoint(ThreadingHTTPServer):
    """Records each POST; answers 502 to the first `fail_first` of them, like Hermes on a failed delivery."""

    def __init__(self, fail_first=0):
        self.requests = []
        self.fail_first = fail_first
        super().__init__(("127.0.0.1", 0), FakeEndpointHandler)


class FakeEndpointHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_POST(self):
        server = self.server
        assert isinstance(server, FakeEndpoint)
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        server.requests.append({"path": self.path, "headers": dict(self.headers), "body": json.loads(body)})
        status = 502 if len(server.requests) <= server.fail_first else 200
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.end_headers()


class Alertmanager(unittest.TestCase):
    """Studio's generated webhook integration, run by the real Alertmanager against a stand-in endpoint."""

    def run_alertmanager(self, endpoint):
        paths = realised()
        [receiver] = json.loads(Path(paths["alertmanagerConfig"]).read_text())["receivers"]
        [integration] = receiver["webhook_configs"]
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(subprocess.run, ["rm", "-rf", str(tmp)])
        token_file = tmp / "token"
        token_file.write_text(TOKEN + "\n")  # as Hermes wrote it: with a trailing newline
        token_file.chmod(0o600)
        header = next(iter(integration["http_config"]["http_headers"]))
        integration["url"] = f"http://127.0.0.1:{endpoint.server_port}/webhooks/studio-alerts"
        integration["http_config"]["http_headers"][header]["files"] = [str(token_file)]
        config = tmp / "alertmanager.json"
        config.write_text(json.dumps({
            "route": {"receiver": "primary", "group_by": ["alertname", "instance"],
                      "group_wait": "0s", "group_interval": "1s", "repeat_interval": "1h"},
            "receivers": [{"name": "primary", "webhook_configs": [integration]}],
        }))
        port = free_port()
        process = subprocess.Popen(
            [paths["alertmanager"], f"--config.file={config}", f"--storage.path={tmp / 'data'}",
             f"--web.listen-address=127.0.0.1:{port}", "--cluster.listen-address="],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)

        def stop():
            process.terminate()
            process.communicate(timeout=10)

        self.addCleanup(stop)
        self.wait_for(lambda: urllib.request.urlopen(f"http://127.0.0.1:{port}/-/ready", timeout=1).status == 200,
                      "Alertmanager ready")
        return port, header

    def wait_for(self, condition, what, timeout=30):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                if condition():
                    return
            except OSError:
                pass
            time.sleep(0.2)
        self.fail(f"timed out waiting for {what}")

    def start_endpoint(self, fail_first=0):
        endpoint = FakeEndpoint(fail_first)
        threading.Thread(target=endpoint.serve_forever, daemon=True).start()
        self.addCleanup(endpoint.server_close)
        self.addCleanup(endpoint.shutdown)
        return endpoint

    def post_alert(self, port):
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/v2/alerts", method="POST",
            headers={"Content-Type": "application/json"},
            data=json.dumps([{
                "labels": {"alertname": "ServiceDown", "severity": "critical",
                           "instance": "http://localhost:8080/health", "job": "blackbox"},
                "annotations": {"summary": "Service http://localhost:8080/health is down",
                                "description": "It has been unreachable for more than 5 minutes."},
            }]).encode())
        urllib.request.urlopen(request, timeout=5).close()

    def test_sends_the_token_without_its_newline_and_the_fields_hermes_renders(self):
        endpoint = self.start_endpoint()
        port, header = self.run_alertmanager(endpoint)
        self.post_alert(port)
        self.wait_for(lambda: endpoint.requests, "the webhook POST")

        [request] = endpoint.requests[:1]
        self.assertEqual("/webhooks/studio-alerts", request["path"])
        self.assertEqual(TOKEN, request["headers"][header])
        body = request["body"]
        # Hermes's route template reads exactly these fields.
        self.assertEqual("firing", body["status"])
        self.assertEqual({"alertname": "ServiceDown", "severity": "critical",
                          "instance": "http://localhost:8080/health", "job": "blackbox"}, body["commonLabels"])
        self.assertEqual({"summary", "description"}, set(body["commonAnnotations"]))

    def test_retries_when_the_endpoint_answers_502(self):
        endpoint = self.start_endpoint(fail_first=1)
        port, _ = self.run_alertmanager(endpoint)
        self.post_alert(port)
        self.wait_for(lambda: len(endpoint.requests) >= 2, "a retry after the 502")
        metrics = urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics", timeout=5).read().decode()
        attempts = [line for line in metrics.splitlines()
                    if line.startswith('alertmanager_notification_requests_failed_total{integration="webhook"')]
        self.assertTrue(attempts and float(attempts[0].split()[-1]) >= 1, attempts)


if __name__ == "__main__":
    unittest.main()
