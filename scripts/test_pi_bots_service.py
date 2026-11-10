#!/usr/bin/env python3
"""services.pi-bots: the bot service's launchd agent and its alert rules.

The module is evaluated through a `path:` flake reference so uncommitted
changes are evaluated as on disk. The generated rules are checked with the
Studio's own promtool, including `promtool test rules` on series shaped like
the health file the service writes (pi-bots src/health.ts). Nothing is
activated or started.
"""
import json
import subprocess
import tempfile
import unittest
from functools import cache
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
NODE = {"instance": "localhost:9100", "job": "node_exporter"}

# The health file as the service writes it (pi-bots src/health.ts, renderMetrics).
HEALTH_FILE = """\
# HELP pi_bots_service_up Whether the bot service runs; 0 is written as it stops cleanly.
# TYPE pi_bots_service_up gauge
pi_bots_service_up 1
# HELP pi_bots_service_start_time_seconds When the bot service started.
# TYPE pi_bots_service_start_time_seconds gauge
pi_bots_service_start_time_seconds 1794338165.718
# HELP pi_bots_metrics_written_timestamp_seconds When the bot service wrote this file.
# TYPE pi_bots_metrics_written_timestamp_seconds gauge
pi_bots_metrics_written_timestamp_seconds 1794341706.506
# HELP pi_bots_bot_runner_up Whether the bot's runner holds its lock.
# TYPE pi_bots_bot_runner_up gauge
pi_bots_bot_runner_up{bot="bob"} 1
# HELP pi_bots_bot_up Whether the bot's Pi answers on its control socket.
# TYPE pi_bots_bot_up gauge
pi_bots_bot_up{bot="bob"} 1
# HELP pi_bots_account_syncing Whether the account's last successful sync is recent.
# TYPE pi_bots_account_syncing gauge
pi_bots_account_syncing{bot="bob"} 1
# HELP pi_bots_bridge_messages The bridge journal's messages by status.
# TYPE pi_bots_bridge_messages gauge
pi_bots_bridge_messages{bot="bob",status="received"} 0
pi_bots_bridge_messages{bot="bob",status="answered"} 3
"""


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
        flagValue = flag: args: lib.removePrefix flag (lib.findFirst (lib.hasPrefix flag) null args);
        prometheusConfig = flagValue "--config.file="
          config.launchd.daemons.prometheus.serviceConfig.ProgramArguments;
        agent = config.launchd.user.agents.pi-bots.serviceConfig;
      in {expr}
    """


@cache
def studio(expr, extra="{ }"):
    return json.loads(nix("eval", "--json", "--impure", "--expr", studio_expr(expr, extra)))


def refused(extra):
    return subprocess.run(["nix", "eval", "--impure", "--expr",
                           studio_expr("config.system.build.toplevel.drvPath", extra)],
                          cwd=REPO, text=True, capture_output=True)


@cache
def realised():
    out = nix("build", "--no-link", "--print-out-paths", "--impure", "--expr", studio_expr("""
      pkgs.writeText "pi-bots-service-paths.json" (builtins.toJSON {
        prometheusConfig = prometheusConfig;
        promtool = "${pkgs.prometheus.cli}/bin/promtool";
      })
    """))
    return json.loads(Path(out.strip().splitlines()[-1]).read_text())


@cache
def rules():
    rule_file = json.loads(Path(realised()["prometheusConfig"]).read_text())["rule_files"][0]
    return rule_file, {r["alert"]: r for g in json.loads(Path(rule_file).read_text())["groups"]
                       for r in g["rules"]}


ALERTS = {"PiBotsServiceDown", "PiBotsServiceSilent", "PiBotsBotDown", "PiBotsBotCrashLooping",
          "PiBotsAccountNotSyncing", "PiBotsMessagesWaiting"}


class Module(unittest.TestCase):
    def test_studio_runs_the_service_from_its_checkout(self):
        agent = studio("agent")
        env = agent["EnvironmentVariables"]
        self.assertEqual("org.nixos.pi-bots", agent["Label"])
        self.assertEqual(["/opt/homebrew/bin/node", "/Users/bryan/code/pi-bots/src/cli.ts", "service", "run"],
                         agent["ProgramArguments"])
        self.assertEqual("/Users/bryan/code/pi-bots", agent["WorkingDirectory"])
        self.assertEqual("https://matrix.snowboardtechie.com", env["PI_BOTS_HOMESERVER"])
        self.assertEqual("/Users/bryan/.local/state/pi-bots", env["PI_BOTS_STATE_DIR"])
        self.assertEqual("/Users/bryan/.local/state/pi-bots/metrics", env["PI_BOTS_METRICS_DIR"])
        self.assertEqual("/Users/bryan", env["HOME"])
        # pi and the job scripts' tools come from Homebrew and the system.
        self.assertIn("/opt/homebrew/bin", env["PATH"].split(":"))
        self.assertTrue(agent["RunAtLoad"])
        self.assertTrue(agent["KeepAlive"])
        self.assertGreaterEqual(agent["ExitTimeOut"], 10)
        self.assertEqual("/tmp/pi-bots.log", agent["StandardOutPath"])
        self.assertEqual("/tmp/pi-bots.error.log", agent["StandardErrorPath"])
        # No secret ever reaches the plist: the service reads its own files.
        self.assertFalse({k for k in env if "TOKEN" in k or "OWNER" in k})

    def test_it_is_re_pinned_with_the_other_homebrew_agents(self):
        # Homebrew's node: base/homebrew.nix restarts it after an upgrade.
        script = studio("config.system.activationScripts.postActivation.text")
        line = next(l for l in script.splitlines() if l.lstrip().startswith("for brew_agent in"))
        self.assertIn("pi-bots", line.split())

    def test_node_exporter_reads_the_health_file(self):
        args = studio("config.launchd.user.agents.node-exporter.serviceConfig.ProgramArguments")
        self.assertIn("--collector.textfile.directory=/Users/bryan/.local/state/pi-bots/metrics", args)

    def test_the_backup_covers_the_service_state(self):
        got = studio("{ a = config.services.pi-bots.stateDir; b = config.services.pi-bots-backup.stateDir; }")
        self.assertEqual(got["a"], got["b"])

    def test_refusals(self):
        cases = {
            '{ services.pi-bots.stateDir = lib.mkForce "/Users/bryan/elsewhere"; }': "must be the directory",
            '{ services.pi-bots.homeserver = lib.mkForce "http://matrix.example.org"; }': "homeserver",
            '{ services.pi-bots.checkout = lib.mkForce "code/pi-bots"; }': "checkout",
        }
        for extra, message in cases.items():
            result = refused(extra)
            self.assertNotEqual(0, result.returncode, extra)
            self.assertIn(message, result.stderr, extra)

    def test_off_declares_nothing(self):
        got = studio("""{
          agent = config.launchd.user.agents ? pi-bots;
          rules = lib.any (r: lib.hasPrefix "PiBots" r.alert && !(lib.hasPrefix "PiBotsBackup" r.alert))
            config.services.monitoring.extraAlertRules;
          dir = lib.elem "/Users/bryan/.local/state/pi-bots/metrics"
            config.services.monitoring.nodeExporter.textfileDirectories;
        }""", "{ services.pi-bots.enable = lib.mkForce false; }")
        self.assertEqual({"agent": False, "rules": False, "dir": False}, got)


class Rules(unittest.TestCase):
    def promtool_test(self, inputs, alertname, *expectations):
        rule_file, _ = rules()
        spec = {"rule_files": [rule_file], "evaluation_interval": "1m", "tests": [{
            "interval": "1m", "input_series": inputs, "alert_rule_test": [
                {"eval_time": t, "alertname": alertname, "exp_alerts": exp} for t, exp in expectations]}]}
        with tempfile.TemporaryDirectory() as tmp:
            test_file = Path(tmp) / "t.json"
            test_file.write_text(json.dumps(spec))
            result = subprocess.run([realised()["promtool"], "test", "rules", str(test_file)],
                                    text=True, capture_output=True)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)

    @staticmethod
    def series(name, values, **labels):
        text = ",".join(f'{k}="{v}"' for k, v in {**NODE, **labels}.items())
        return {"series": f"{name}{{{text}}}", "values": values}

    def firing(self, name, labels=NODE, value="", **more):
        rule = rules()[1][name]
        annotations = {k: v.replace("{{ $labels.bot }}", more.get("bot", ""))
                       .replace('{{ $value | printf "%.0f" }}', value) for k, v in rule["annotations"].items()}
        return [{"exp_labels": {**labels, **more, "severity": rule["labels"]["severity"]},
                 "exp_annotations": annotations}]

    def up(self, values="1x120"):
        return self.series("pi_bots_service_up", values)

    # A file written every minute of the test: the timestamp keeps up with the clock.
    def written(self, values="0+60x120"):
        return self.series("pi_bots_metrics_written_timestamp_seconds", values)

    def test_rules_are_generated_and_accepted(self):
        rule_file, by_name = rules()
        self.assertTrue(ALERTS <= set(by_name))
        result = subprocess.run([realised()["promtool"], "check", "rules", rule_file],
                                text=True, capture_output=True)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)

    def test_the_health_file_is_what_promtool_reads(self):
        result = subprocess.run([realised()["promtool"], "check", "metrics"], input=HEALTH_FILE,
                                text=True, capture_output=True)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)

    def test_a_healthy_service_raises_nothing(self):
        inputs = [self.up(), self.written(),
                  self.series("pi_bots_bot_runner_up", "1x120", bot="bob"),
                  self.series("pi_bots_bot_up", "1x120", bot="bob"),
                  self.series("pi_bots_account_syncing", "1x120", bot="bob"),
                  self.series("pi_bots_bridge_messages", "0x120", bot="bob", status="received"),
                  self.series("pi_bots_bridge_messages", "3x120", bot="bob", status="answered")]
        for alert in sorted(ALERTS):
            with self.subTest(alert):
                self.promtool_test(inputs, alert, ("30m", []), ("119m", []))

    def test_a_clean_stop_is_down_after_five_minutes(self):
        # The last file says 0 and stops changing; Silent must not fire as well.
        inputs = [self.up("1x10 0x60"), self.written("0+60x10 600x60")]
        down = self.firing("PiBotsServiceDown")
        self.promtool_test(inputs, "PiBotsServiceDown", ("14m", []), ("17m", down))
        self.promtool_test(inputs, "PiBotsServiceSilent", ("40m", []))

    def test_a_crash_goes_silent(self):
        # The file stays as it was, saying the service is up, and goes stale.
        inputs = [self.up("1x120"), self.written("0+60x10 600x110")]
        silent = self.firing("PiBotsServiceSilent")
        self.promtool_test(inputs, "PiBotsServiceSilent", ("12m", []), ("19m", silent))
        self.promtool_test(inputs, "PiBotsServiceDown", ("40m", []))

    def test_no_health_file_at_all_is_silent(self):
        inputs = [self.series("up", "1x60")]
        silent = self.firing("PiBotsServiceSilent", labels={})
        self.promtool_test(inputs, "PiBotsServiceSilent", ("4m", []), ("6m", silent))

    def test_a_closed_tab_is_a_bot_down(self):
        inputs = [self.up(), self.written(),
                  self.series("pi_bots_bot_runner_up", "1x10 0x60", bot="bob"),
                  self.series("pi_bots_bot_up", "1x10 0x60", bot="bob"),
                  self.series("pi_bots_bot_runner_up", "1x70", bot="steve"),
                  self.series("pi_bots_bot_up", "1x70", bot="steve")]
        self.promtool_test(inputs, "PiBotsBotDown", ("14m", []), ("16m", self.firing("PiBotsBotDown", bot="bob")))
        # Steadily down is not a crash loop: one alert, not two.
        self.promtool_test(inputs, "PiBotsBotCrashLooping", ("30m", []), ("60m", []))

    def test_a_dead_pi_with_its_runner_alive_is_a_bot_down(self):
        inputs = [self.up(), self.written(),
                  self.series("pi_bots_bot_runner_up", "1x70", bot="bob"),
                  self.series("pi_bots_bot_up", "1x10 0x60", bot="bob")]
        self.promtool_test(inputs, "PiBotsBotDown", ("16m", self.firing("PiBotsBotDown", bot="bob")))

    def test_a_short_restart_is_not_a_bot_down(self):
        inputs = [self.up(), self.written(),
                  self.series("pi_bots_bot_runner_up", "1x70", bot="bob"),
                  self.series("pi_bots_bot_up", "1x10 0x2 1x60", bot="bob")]
        self.promtool_test(inputs, "PiBotsBotDown", ("12m", []), ("20m", []))
        self.promtool_test(inputs, "PiBotsBotCrashLooping", ("12m", []), ("20m", []))

    def test_bots_are_not_reported_while_the_service_is_down(self):
        inputs = [self.up("1x10 0x60"), self.written("0+60x10 600x60"),
                  self.series("pi_bots_bot_runner_up", "1x10 0x60", bot="bob"),
                  self.series("pi_bots_bot_up", "1x10 0x60", bot="bob"),
                  self.series("pi_bots_account_syncing", "1x10 0x60", bot="bob"),
                  self.series("pi_bots_bridge_messages", "0x10 1x60", bot="bob", status="received")]
        for alert in ("PiBotsBotDown", "PiBotsBotCrashLooping", "PiBotsAccountNotSyncing", "PiBotsMessagesWaiting"):
            with self.subTest(alert):
                self.promtool_test(inputs, alert, ("60m", []))

    def test_a_firing_bot_alert_clears_at_once_on_a_clean_stop(self):
        # The final file is still fresh for two minutes; it says the service is down.
        inputs = [self.up("1x11 0x40"), self.written("0+60x11 660x40"),
                  self.series("pi_bots_bot_runner_up", "1x2 0x50", bot="bob"),
                  self.series("pi_bots_bot_up", "1x2 0x50", bot="bob")]
        self.promtool_test(inputs, "PiBotsBotDown", ("9m", self.firing("PiBotsBotDown", bot="bob")), ("12m", []))

    def test_a_crash_looping_pi_fires_though_it_never_stays_down(self):
        # Up one write in four, as a Pi that dies within a minute of each start.
        inputs = [self.up(), self.written(),
                  self.series("pi_bots_bot_runner_up", "1x70", bot="bob"),
                  self.series("pi_bots_bot_up", "1x10 0 0 0 1 0 0 0 1 0 0 0 1 0 0 0 1 0 0 0 1", bot="bob")]
        self.promtool_test(inputs, "PiBotsBotDown", ("30m", []))
        self.promtool_test(inputs, "PiBotsBotCrashLooping", ("14m", []),
                           ("26m", self.firing("PiBotsBotCrashLooping", bot="bob")))

    def test_one_outage_that_ends_is_not_a_crash_loop(self):
        inputs = [self.up(), self.written(),
                  self.series("pi_bots_bot_runner_up", "1x80", bot="bob"),
                  self.series("pi_bots_bot_up", "1x10 0x8 1x60", bot="bob")]
        for at in ("18m", "20m", "22m", "26m"):
            self.promtool_test(inputs, "PiBotsBotCrashLooping", (at, []))

    def test_a_crashed_service_leaves_no_bot_alert_behind(self):
        # The last file froze with bob down; only the service rule may speak.
        inputs = [self.up("1x80"), self.written("0+60x30 1800x50"),
                  self.series("pi_bots_bot_runner_up", "1x10 0x70", bot="bob"),
                  self.series("pi_bots_bot_up", "1x10 0x70", bot="bob"),
                  self.series("pi_bots_account_syncing", "1x10 0x70", bot="bob")]
        self.promtool_test(inputs, "PiBotsBotDown", ("25m", self.firing("PiBotsBotDown", bot="bob")), ("40m", []))
        self.promtool_test(inputs, "PiBotsAccountNotSyncing", ("40m", []))
        self.promtool_test(inputs, "PiBotsServiceSilent", ("40m", self.firing("PiBotsServiceSilent")))

    def test_an_account_not_syncing(self):
        inputs = [self.up(), self.written(),
                  self.series("pi_bots_account_syncing", "1x10 0x60", bot="bob")]
        self.promtool_test(inputs, "PiBotsAccountNotSyncing", ("19m", []),
                           ("21m", self.firing("PiBotsAccountNotSyncing", bot="bob")))

    def test_messages_waiting(self):
        inputs = [self.up(), self.written(),
                  self.series("pi_bots_bridge_messages", "0x10 2x60", bot="bob", status="received"),
                  self.series("pi_bots_bridge_messages", "5x70", bot="bob", status="answered")]
        self.promtool_test(inputs, "PiBotsMessagesWaiting", ("39m", []),
                           ("41m", self.firing("PiBotsMessagesWaiting", value="2", bot="bob", status="received")))

    def test_a_message_answered_within_the_window_raises_nothing(self):
        inputs = [self.up(), self.written(),
                  self.series("pi_bots_bridge_messages", "0x10 1x20 0x40", bot="bob", status="received")]
        self.promtool_test(inputs, "PiBotsMessagesWaiting", ("31m", []), ("60m", []))


if __name__ == "__main__":
    unittest.main()
