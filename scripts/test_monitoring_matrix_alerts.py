#!/usr/bin/env python3
"""services.monitoring.alertMatrix: Alertmanager notifications also posted into a Matrix room.

Evaluates through a `path:` flake reference so an uncommitted module is
evaluated as it is on disk. The bridge runs for real, on loopback, against a
stand-in homeserver; nothing is activated and no real homeserver is contacted.
"""
import json
import os
import signal
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
from urllib.parse import unquote, urlsplit

REPO = Path(__file__).resolve().parents[1]
ROOM = "!AbCdEfGhIjKlMnOp:example.com"
TOKEN = "syt_YWxlcnRz_test_token"


def matrix(**fields):
    """A `services.monitoring.alertMatrix` attrset literal with `fields` replacing the defaults."""
    values = {"homeserverUrl": "https://matrix.example.com", "userId": "@alerts:example.com",
              "roomId": ROOM, **fields}
    return "{ " + " ".join(
        f"{k} = {v if isinstance(v, int) else json.dumps(v, ensure_ascii=False)};"
        for k, v in values.items()) + " }"


MATRIX = matrix()


def nix(*args):
    result = subprocess.run(["nix", *args], cwd=REPO, text=True, capture_output=True)
    if result.returncode != 0:
        raise AssertionError(result.stderr[-4000:])
    return result.stdout


def studio_expr(settings, expr, extra="{ }"):
    """`expr` with `config`, `lib` and `pkgs` bound to Studio plus `services.monitoring.alertMatrix = settings`."""
    return f"""
      let
        studio = (builtins.getFlake "path:{REPO}").darwinConfigurations.studio;
        inherit (studio) pkgs;
        inherit (pkgs) lib;
        config = (studio.extendModules {{
          modules = [ {{ services.monitoring.alertMatrix = lib.mkForce ({settings}); }} {extra} ];
        }}).config;
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
def realised(settings=MATRIX):
    """Store paths of the generated configs, launcher and checking tools, built locally."""
    out = nix("build", "--no-link", "--print-out-paths", "--impure", "--expr", studio_expr(settings, """
      pkgs.writeText "matrix-alert-paths.json" (builtins.toJSON {
        alertmanager = alertmanagerConfig;
        prometheus = prometheusConfig;
        launcher = builtins.head
          config.launchd.user.agents.matrix-alertmanager-receiver.serviceConfig.ProgramArguments;
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
        ["nix", "eval", "--json", "--impure", "--expr",
         studio_expr(settings, "config.launchd.user.agents.matrix-alertmanager-receiver.serviceConfig")],
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
    def test_unset_leaves_delivery_email_only(self):
        agents = studio("null", "builtins.attrNames config.launchd.user.agents")
        self.assertIn("alertmanager", agents)
        self.assertNotIn("matrix-alertmanager-receiver", agents)
        receivers = studio("null", "(readJSON alertmanagerConfig).receivers")
        self.assertEqual([{"name": "primary", "email_configs": [
            {"to": "bryan@snowboardtechie.com", "send_resolved": True}]}], receivers)

    def test_set_adds_a_loopback_webhook_to_the_same_receiver(self):
        am = studio(MATRIX, "readJSON alertmanagerConfig")
        self.assertEqual("primary", am["route"]["receiver"])
        [receiver] = am["receivers"]
        self.assertEqual(1, len(receiver["email_configs"]))
        self.assertEqual([{"url": "http://127.0.0.1:9097/alerts/primary", "send_resolved": True}],
                         receiver["webhook_configs"])

    def test_bridge_is_scraped_and_its_failures_alert(self):
        prometheus = studio(MATRIX, "readJSON prometheusConfig")
        jobs = {job["job_name"]: job for job in prometheus["scrape_configs"]}
        self.assertEqual([{"targets": ["127.0.0.1:9097"]}],
                         jobs["matrix-alertmanager-receiver"]["static_configs"])
        rules = json.loads(Path(realised()["prometheus"]).read_text())["rule_files"]
        names = [r["alert"] for g in json.loads(Path(rules[0]).read_text())["groups"] for r in g["rules"]]
        self.assertIn("MatrixAlertDeliveryFailed", names)

    def test_token_stays_out_of_the_store_and_the_plist(self):
        agent = studio(MATRIX, "config.launchd.user.agents.matrix-alertmanager-receiver.serviceConfig")
        self.assertIsNone(agent["EnvironmentVariables"])
        self.assertEqual("/tmp/matrix-alertmanager-receiver.log", agent["StandardOutPath"])
        launcher = Path(realised()["launcher"]).read_text()
        self.assertIn("/Users/bryan/.secrets/matrix-alertmanager-token", launcher)
        config_path = launcher.split("-config-path ")[1].split()[0]
        config = json.loads(Path(config_path).read_text())
        self.assertEqual("${MATRIX_ALERTMANAGER_ACCESS_TOKEN}", config["matrix"]["access-token"])
        self.assertEqual("127.0.0.1", config["http"]["address"])

    def test_activation_warns_when_the_token_is_missing(self):
        activation = studio(MATRIX, "config.system.activationScripts.extraActivation.text")
        self.assertIn("# === matrix-alertmanager-receiver: preflight ===", activation)
        self.assertIn("/Users/bryan/.secrets/matrix-alertmanager-token", activation)


class Refusals(unittest.TestCase):
    def test_requires_email_delivery(self):
        failed = failures(MATRIX, "{ services.monitoring.alertEmail = lib.mkForce null; }")
        self.assertTrue(any("set services.monitoring.alertEmail" in m for m in failed), failed)
        self.assertEqual([], failures(MATRIX))

    def test_room_must_be_an_id_not_an_alias(self):
        self.assertIn("roomId", refusal(matrix(roomId="#alerts:example.com")))

    def test_values_must_be_single_well_formed_tokens(self):
        for field, value in (("homeserverUrl", "https://matrix.example.com/\nx"),
                             ("homeserverUrl", "matrix.example.com"),
                             ("userId", "@alerts:example.com x"),
                             ("userId", "alerts"),
                             ("roomId", f"{ROOM}\n")):
            with self.subTest(field=field, value=value):
                self.assertIn(field, refusal(matrix(**{field: value})))


class GeneratedConfigs(unittest.TestCase):
    def test_alertmanager_accepts_its_config(self):
        paths = realised()
        result = subprocess.run([paths["amtool"], "check-config", paths["alertmanager"]],
                                text=True, capture_output=True)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)

    def test_prometheus_accepts_its_config_and_rules(self):
        paths = realised()
        result = subprocess.run([paths["promtool"], "check", "config", "--syntax-only", paths["prometheus"]],
                                text=True, capture_output=True)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)

    def test_delivery_failure_rule_fires_on_new_failures_only(self):
        paths = realised()
        rules_file = json.loads(Path(paths["prometheus"]).read_text())["rule_files"][0]
        rule = next(r for g in json.loads(Path(rules_file).read_text())["groups"]
                    for r in g["rules"] if r["alert"] == "MatrixAlertDeliveryFailed")
        target = 'instance="127.0.0.1:9097",job="matrix-alertmanager-receiver"'
        labels = {"instance": "127.0.0.1:9097", "job": "matrix-alertmanager-receiver", "severity": "warning"}
        firing = [{"exp_labels": labels, "exp_annotations": rule["annotations"]}]
        join_labels = {**labels, "room": ROOM}
        join_firing = [{"exp_labels": join_labels, "exp_annotations": rule["annotations"]}]

        def case(series, *expectations):
            return {"interval": "1m", "input_series": series, "alert_rule_test": [
                {"eval_time": t, "alertname": "MatrixAlertDeliveryFailed", "exp_alerts": exp}
                for t, exp in expectations]}

        send = f"matrix_alertmanager_receiver_send_failure_total{{{target}}}"
        join = f'matrix_alertmanager_receiver_join_room_failure_total{{{target},room="{ROOM}"}}'
        spec = {"rule_files": [rules_file], "evaluation_interval": "1m", "tests": [
            # Healthy: the send counter exists at zero, the join counter never appears.
            case([{"series": send, "values": "0x60"}], ("30m", []), ("60m", [])),
            # One send failure at 20m fires, then clears once it leaves the window.
            case([{"series": send, "values": "0x19 1x40"}], ("10m", []), ("25m", firing), ("45m", [])),
            # The first join failure creates the series at 1: no increase, still fires.
            case([{"series": send, "values": "0x60"}, {"series": join, "values": "_x20 1x40"}],
                 ("10m", []), ("25m", join_firing), ("45m", [])),
        ]}
        with tempfile.TemporaryDirectory() as tmp:
            test_file = Path(tmp) / "matrix-rule-test.json"
            test_file.write_text(json.dumps(spec))
            result = subprocess.run([paths["promtool"], "test", "rules", str(test_file)],
                                    text=True, capture_output=True)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)


class FakeHomeserver(ThreadingHTTPServer):
    """Answers the three client-server calls the bridge makes and records them."""

    def __init__(self):
        self.requests = []
        super().__init__(("127.0.0.1", 0), FakeHomeserverHandler)


class FakeHomeserverHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def handle_any(self):
        server = self.server
        assert isinstance(server, FakeHomeserver)
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        path = unquote(urlsplit(self.path).path)
        server.requests.append({
            "method": self.command, "path": path,
            "auth": self.headers.get("Authorization"),
            "body": json.loads(body) if body else None,
        })
        if self.command == "GET" and path == "/_matrix/client/v3/joined_rooms":
            reply = {"joined_rooms": []}
        elif self.command == "POST" and path == f"/_matrix/client/v3/rooms/{ROOM}/join":
            reply = {"room_id": ROOM}
        elif self.command == "PUT" and path.startswith(f"/_matrix/client/v3/rooms/{ROOM}/send/m.room.message/"):
            reply = {"event_id": f"$event{len(server.requests)}"}
        else:
            self.send_response(404)
            self.end_headers()
            return
        data = json.dumps(reply).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    do_GET = do_POST = do_PUT = handle_any


def alertmanager_payload(status):
    return {
        "version": "4", "groupKey": "{}:{alertname=\"ServiceDown\"}", "truncatedAlerts": 0,
        "status": status, "receiver": "primary",
        "groupLabels": {"alertname": "ServiceDown"}, "commonLabels": {}, "commonAnnotations": {},
        "externalURL": "http://Bryans-Mac-Studio.local:9093",
        "alerts": [{
            "status": status,
            "labels": {"alertname": "ServiceDown", "severity": "critical",
                       "instance": "http://localhost:8080/health"},
            "annotations": {"summary": "Service <b>open-webui</b> is down",
                            "description": "It has been unreachable for more than 5 minutes."},
            "startsAt": "2026-10-19T12:00:00Z",
            "endsAt": "2026-10-19T12:10:00Z" if status == "resolved" else "0001-01-01T00:00:00Z",
            "generatorURL": "http://Bryans-Mac-Studio.local:9090/graph", "fingerprint": "0123456789abcdef",
        }],
    }


class Bridge(unittest.TestCase):
    def start(self, settings):
        launcher = realised(settings)["launcher"]
        process = subprocess.Popen([launcher], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, start_new_session=True, env={"PATH": "/usr/bin:/bin"})

        def stop():
            if process.returncode is None:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGTERM)
                process.communicate(timeout=10)

        self.addCleanup(stop)
        return process

    def test_posts_firing_and_resolved_alerts_with_the_token_from_its_file(self):
        homeserver = FakeHomeserver()
        threading.Thread(target=homeserver.serve_forever, daemon=True).start()
        self.addCleanup(homeserver.server_close)
        self.addCleanup(homeserver.shutdown)
        port = free_port()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        token_file = Path(tmp.name) / "token"
        token_file.write_text(TOKEN + "\n")
        token_file.chmod(0o600)
        bridge = self.start(matrix(homeserverUrl=f"http://127.0.0.1:{homeserver.server_port}",
                                   port=port, accessTokenFile=str(token_file)))

        deadline = time.monotonic() + 30
        while True:
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics", timeout=1).close()
                break
            except OSError:
                if bridge.poll() is not None or time.monotonic() > deadline:
                    bridge.kill()
                    self.fail(f"bridge did not start: {bridge.communicate()}")
                time.sleep(0.2)

        for status in ("firing", "resolved"):
            request = urllib.request.Request(
                f"http://127.0.0.1:{port}/alerts/primary", method="POST",
                data=json.dumps(alertmanager_payload(status)).encode(),
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(request, timeout=10) as response:
                self.assertEqual(200, response.status)

        self.assertEqual({f"Bearer {TOKEN}"}, {r["auth"] for r in homeserver.requests})
        sent = [r["body"] for r in homeserver.requests if r["method"] == "PUT"]
        self.assertEqual([
            "<p>🔴 <strong>ServiceDown</strong> (critical): Service &lt;b&gt;open-webui&lt;/b&gt; is down</p>"
            "<p>It has been unreachable for more than 5 minutes.</p>",
            "<p>✅ <strong>Resolved: ServiceDown</strong>: Service &lt;b&gt;open-webui&lt;/b&gt; is down</p>",
        ], [body["formatted_body"] for body in sent])
        self.assertEqual({"org.matrix.custom.html"}, {body["format"] for body in sent})
        self.assertTrue(all("ServiceDown" in body["body"] for body in sent), sent)

    def test_waits_for_a_missing_token_instead_of_exiting(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "absent"
            bridge = self.start(matrix(port=free_port(), accessTokenFile=str(missing)))
            time.sleep(2)
            still_running = bridge.poll() is None
            if still_running:
                os.killpg(bridge.pid, signal.SIGTERM)
            _, stderr = bridge.communicate(timeout=10)
            self.assertTrue(still_running, stderr)
            self.assertIn(f"waiting for {missing}", stderr)


if __name__ == "__main__":
    unittest.main()
