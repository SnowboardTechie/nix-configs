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
          "PiBotsAccountNotSyncing", "PiBotsMessagesWaiting",
          "PiBotsClockSilent", "PiBotsJobFailed", "PiBotsJobWaiting", "PiBotsJobFileUnusable"}
WATCHDOG_ALERTS = {"SggPreviewWatchdogSilent", "SggPreviewWatchdogBlocked", "SggPreviewNeedsAttention"}

# The clock's part of the health file (pi-bots src/health.ts) and the SGG
# preview watchdog's file (pi-bots jobs/sgg-preview/watchdog.py), as written.
CLOCK_AND_WATCHDOG_FILES = """\
# HELP pi_bots_clock_last_tick_timestamp_seconds When the clock last read the job files and started what was due; 0 if not since the service started.
# TYPE pi_bots_clock_last_tick_timestamp_seconds gauge
pi_bots_clock_last_tick_timestamp_seconds 1794416565.213
# HELP pi_bots_clock_bad_job_files Job files the clock could not use at its last tick.
# TYPE pi_bots_clock_bad_job_files gauge
pi_bots_clock_bad_job_files 0
# HELP pi_bots_job_waiting Whether the job waits, not run, because the service does not bridge its bot.
# TYPE pi_bots_job_waiting gauge
pi_bots_job_waiting{job_id="sgg-preview-watchdog"} 0
# HELP pi_bots_job_last_run_failed Whether the job's latest finished run failed; a run interrupted by a stop does not count.
# TYPE pi_bots_job_last_run_failed gauge
pi_bots_job_last_run_failed{job_id="sgg-preview-watchdog"} 0
# HELP sgg_preview_watchdog_last_run_timestamp_seconds When the watchdog job last ended.
# TYPE sgg_preview_watchdog_last_run_timestamp_seconds gauge
sgg_preview_watchdog_last_run_timestamp_seconds 1794416405.529
# HELP sgg_preview_watchdog_blocked 1 while the watchdog waits for Tailscale or cannot read the preview registry.
# TYPE sgg_preview_watchdog_blocked gauge
sgg_preview_watchdog_blocked{reason="tailscale"} 0
sgg_preview_watchdog_blocked{reason="registry"} 0
# HELP sgg_preview_persistent_preview_needs_attention 1 for a persistent preview the watchdog could not bring back (failed, expired or cleanup_failed).
# TYPE sgg_preview_persistent_preview_needs_attention gauge
sgg_preview_persistent_preview_needs_attention{preview="example",status="cleanup_failed"} 1
"""


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

    def test_studio_has_the_watchdog_rules(self):
        names = studio("map (r: r.alert) config.services.monitoring.extraAlertRules")
        self.assertTrue((ALERTS | WATCHDOG_ALERTS) <= set(names))

    def test_the_watchdog_rules_are_opt_in(self):
        names = studio("map (r: r.alert) config.services.monitoring.extraAlertRules",
                       "{ services.pi-bots.alerts.sggPreviewWatchdog = lib.mkForce false; }")
        self.assertTrue(ALERTS <= set(names))
        self.assertFalse(WATCHDOG_ALERTS & set(names))

    def test_off_declares_nothing(self):
        got = studio("""{
          agent = config.launchd.user.agents ? pi-bots;
          rules = lib.any (r: lib.hasPrefix "PiBots" r.alert && !(lib.hasPrefix "PiBotsBackup" r.alert))
            config.services.monitoring.extraAlertRules;
          dir = lib.elem "/Users/bryan/.local/state/pi-bots/metrics"
            config.services.monitoring.nodeExporter.textfileDirectories;
        }""", "{ services.pi-bots.enable = lib.mkForce false; }")
        self.assertEqual({"agent": False, "rules": False, "dir": False}, got)


# A stand-in for the checkout's command line: prints what it was given (its
# arguments, PI_BOTS_* settings, stdin and pid) and exits with the code in its
# first argument when that is a number.
STAND_IN_CLI = """\
import { readFileSync } from "node:fs";
const keys = Object.keys(process.env).filter((k) => k.startsWith("PI_BOTS_")).sort();
const stdin = readFileSync(0, "utf8");
console.log(JSON.stringify({ argv: process.argv.slice(2), env: Object.fromEntries(keys.map((k) => [k, process.env[k]])), stdin, pid: process.pid }));
process.exit(/^[0-9]+$/.test(process.argv[2] ?? "") ? Number(process.argv[2]) : 0);
"""


@cache
def command_with_checkout(checkout):
    out = nix("build", "--no-link", "--print-out-paths", "--impure", "--expr", studio_expr(
        "config.services.pi-bots.package", f'{{ services.pi-bots.checkout = lib.mkForce "{checkout}"; }}'))
    return Path(out.strip().splitlines()[-1]) / "bin" / "pi-bots"


class Command(unittest.TestCase):
    """The `pi-bots` command Pi Durable's agents find on their PATH (N9.2)."""

    def test_it_is_in_the_system_profile(self):
        got = studio("""{
          installed = lib.elem config.services.pi-bots.package config.environment.systemPackages;
          name = config.services.pi-bots.package.name;
        }""")
        self.assertEqual({"installed": True, "name": "pi-bots"}, got)

    def test_the_system_profile_links_it(self):
        out = nix("build", "--no-link", "--print-out-paths", "--impure", "--expr",
                  studio_expr("config.system.path"))
        link = Path(out.strip().splitlines()[-1]) / "bin" / "pi-bots"
        self.assertTrue(link.exists(), link)
        text = link.resolve().read_text()
        self.assertIn("/opt/homebrew/bin/node", text)
        self.assertIn("/Users/bryan/code/pi-bots/src/cli.ts", text)

    def test_pi_durable_agents_find_it(self):
        # The SGG service and the sampler hand their own PATH to the command
        # (packages/foundation/src/notify.ts passes PATH, HOME and PI_BOTS_*).
        paths = studio("""map (a: a.serviceConfig.EnvironmentVariables.PATH)
          [ config.launchd.user.agents.pi-durable-sgg config.launchd.user.agents.pi-durable-sampler ]""")
        for path in paths:
            self.assertIn("/run/current-system/sw/bin", path.split(":"))

    def test_it_defaults_to_the_service_settings(self):
        with tempfile.TemporaryDirectory() as checkout:
            (Path(checkout) / "src").mkdir()
            (Path(checkout) / "src" / "cli.ts").write_text(STAND_IN_CLI)
            command = command_with_checkout(checkout)

            def run(*args, stdin="", **env):
                child = subprocess.Popen([str(command), *args], text=True, stdin=subprocess.PIPE,
                                         stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                         env={"PATH": "/usr/bin:/bin", "HOME": checkout, **env})
                stdout, stderr = child.communicate(stdin)
                out = json.loads(stdout) if stdout else None
                # exec: the command line is the caller's child itself, so the
                # notifier's SIGKILL on a timeout reaches it, not a shell.
                if out is not None:
                    self.assertEqual(child.pid, out["pid"])
                return child.returncode, out, stderr

            # Only PATH and HOME, as Pi Durable's notifier passes them.
            # The notice's body arrives on stdin (`--file -`), byte for byte.
            body = "**Review ready**\n\n- one `item`\n"
            code, out, err = run("send", "sgg-bot", "a room", "--file", "-", stdin=body)
            self.assertEqual((0, ""), (code, err))
            self.assertEqual(["send", "sgg-bot", "a room", "--file", "-"], out["argv"])
            self.assertEqual(body, out["stdin"])
            self.assertEqual({"PI_BOTS_HOMESERVER": "https://matrix.snowboardtechie.com",
                              "PI_BOTS_STATE_DIR": "/Users/bryan/.local/state/pi-bots"}, out["env"])
            # The caller's own settings win, and nothing else is added.
            code, out, _ = run("help", PI_BOTS_HOMESERVER="https://matrix.example.org",
                               PI_BOTS_STATE_DIR="/tmp/elsewhere", PI_BOTS_OWNER="x")
            self.assertEqual({"PI_BOTS_HOMESERVER": "https://matrix.example.org",
                              "PI_BOTS_STATE_DIR": "/tmp/elsewhere", "PI_BOTS_OWNER": "x"}, out["env"])
            # An empty setting counts as unset.
            _, out, _ = run("help", PI_BOTS_HOMESERVER="", PI_BOTS_STATE_DIR="")
            self.assertEqual({"PI_BOTS_HOMESERVER": "https://matrix.snowboardtechie.com",
                              "PI_BOTS_STATE_DIR": "/Users/bryan/.local/state/pi-bots"}, out["env"])
            # Exit 3 (the bot keeping silent) reaches the caller.
            code, out, _ = run("3")
            self.assertEqual(3, code)

    def test_off_installs_nothing(self):
        got = studio('lib.any (p: (p.name or "") == "pi-bots") config.environment.systemPackages',
                     "{ services.pi-bots.enable = lib.mkForce false; }")
        self.assertFalse(got)


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

    # The clock ticking every minute of the test.
    def tick(self, values="0+60x120"):
        return self.series("pi_bots_clock_last_tick_timestamp_seconds", values)

    # The watchdog finishing a run every five minutes: a fresh timestamp each write.
    def watchdog_ran(self, values="0+60x120"):
        return self.series("sgg_preview_watchdog_last_run_timestamp_seconds", values)

    def test_rules_are_generated_and_accepted(self):
        rule_file, by_name = rules()
        self.assertTrue(ALERTS <= set(by_name))
        result = subprocess.run([realised()["promtool"], "check", "rules", rule_file],
                                text=True, capture_output=True)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)

    def test_the_health_file_is_what_promtool_reads(self):
        for text in (HEALTH_FILE, CLOCK_AND_WATCHDOG_FILES):
            result = subprocess.run([realised()["promtool"], "check", "metrics"], input=text,
                                    text=True, capture_output=True)
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)

    def test_a_healthy_service_raises_nothing(self):
        inputs = [self.up(), self.written(),
                  self.series("pi_bots_bot_runner_up", "1x120", bot="bob"),
                  self.series("pi_bots_bot_up", "1x120", bot="bob"),
                  self.series("pi_bots_account_syncing", "1x120", bot="bob"),
                  self.series("pi_bots_bridge_messages", "0x120", bot="bob", status="received"),
                  self.series("pi_bots_bridge_messages", "3x120", bot="bob", status="answered")]
        inputs += [self.tick(), self.series("pi_bots_clock_bad_job_files", "0x120"),
                   self.series("pi_bots_job_waiting", "0x120", job_id="bob/evening"),
                   self.series("pi_bots_job_last_run_failed", "0x120", job_id="bob/evening")]
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


    # --- The job clock and jobs (W32.4's metrics) ---

    def test_a_stopped_clock_in_a_running_service(self):
        # The file stays fresh; the tick stops at minute 10.
        inputs = [self.up(), self.written(), self.tick("0+60x10 600x110")]
        self.promtool_test(inputs, "PiBotsClockSilent", ("14m", []),
                           ("17m", self.firing("PiBotsClockSilent")))

    def test_a_clock_that_has_not_ticked_since_start(self):
        inputs = [self.up(), self.written(), self.tick("0x120")]
        self.promtool_test(inputs, "PiBotsClockSilent", ("3m", []),
                           ("10m", self.firing("PiBotsClockSilent")))

    def test_a_short_pause_of_the_clock_is_not_silent(self):
        inputs = [self.up(), self.written(), self.tick("0+60x10 600x3 840+60x100")]
        self.promtool_test(inputs, "PiBotsClockSilent", ("13m", []), ("16m", []), ("30m", []))

    def test_the_clock_is_not_reported_while_the_service_is_down(self):
        # A crash: everything freezes. A clean stop: service_up says 0.
        for up, written in (("1x120", "0+60x10 600x110"), ("1x10 0x110", "0+60x10 600x110")):
            with self.subTest(up=up):
                inputs = [self.up(up), self.written(written), self.tick("0+60x10 600x110")]
                self.promtool_test(inputs, "PiBotsClockSilent", ("30m", []), ("60m", []))

    def test_a_failed_job(self):
        inputs = [self.up(), self.written(),
                  self.series("pi_bots_job_last_run_failed", "0x10 1x110", job_id="bob/evening"),
                  self.series("pi_bots_job_last_run_failed", "0x120", job_id="report")]
        rule = rules()[1]["PiBotsJobFailed"]
        expected = [{"exp_labels": {**NODE, "job_id": "bob/evening", "severity": rule["labels"]["severity"]},
                     "exp_annotations": {k: v.replace("{{ $labels.job_id }}", "bob/evening")
                                         for k, v in rule["annotations"].items()}}]
        self.assertEqual("warning", rule["labels"]["severity"])
        self.promtool_test(inputs, "PiBotsJobFailed", ("19m", []), ("21m", expected))

    def test_a_job_that_fails_once_and_then_runs_is_quiet(self):
        inputs = [self.up(), self.written(),
                  self.series("pi_bots_job_last_run_failed", "0x10 1x5 0x100", job_id="sgg-preview-watchdog")]
        for at in ("12m", "16m", "20m", "40m"):
            self.promtool_test(inputs, "PiBotsJobFailed", (at, []))

    def test_a_failed_job_is_not_reported_while_the_service_is_down(self):
        inputs = [self.up("1x5 0x115"), self.written("0+60x5 300x115"),
                  self.series("pi_bots_job_last_run_failed", "1x120", job_id="bob/evening")]
        self.promtool_test(inputs, "PiBotsJobFailed", ("30m", []), ("60m", []))

    def test_job_alerts_are_not_reported_while_the_service_is_down(self):
        # A crash freezes the file saying up; a clean stop writes service_up 0.
        for up, written in (("1x120", "0+60x5 300x115"), ("1x5 0x115", "0+60x5 300x115")):
            inputs = [self.up(up), self.written(written),
                      self.series("pi_bots_job_waiting", "1x120", job_id="steve/report"),
                      self.series("pi_bots_clock_bad_job_files", "2x120")]
            for alert in ("PiBotsJobWaiting", "PiBotsJobFileUnusable"):
                with self.subTest(alert, up=up):
                    self.promtool_test(inputs, alert, ("40m", []), ("100m", []))

    def test_a_waiting_job(self):
        inputs = [self.up(), self.written(),
                  self.series("pi_bots_job_waiting", "0x10 1x110", job_id="steve/report")]
        rule = rules()[1]["PiBotsJobWaiting"]
        expected = [{"exp_labels": {**NODE, "job_id": "steve/report", "severity": "warning"},
                     "exp_annotations": {k: v.replace("{{ $labels.job_id }}", "steve/report")
                                         for k, v in rule["annotations"].items()}}]
        self.promtool_test(inputs, "PiBotsJobWaiting", ("39m", []), ("41m", expected))

    def test_an_unusable_job_file(self):
        inputs = [self.up(), self.written(), self.series("pi_bots_clock_bad_job_files", "0x10 2x110")]
        self.promtool_test(inputs, "PiBotsJobFileUnusable", ("24m", []),
                           ("26m", self.firing("PiBotsJobFileUnusable", value="2")))

    # --- The SGG preview watchdog (pi-bots jobs/sgg-preview/watchdog.py) ---

    def watchdog(self, name, labels=NODE, **more):
        rule = rules()[1][name]
        annotations = {}
        for key, text in rule["annotations"].items():
            for label, value in more.items():
                text = text.replace("{{ $labels.%s }}" % label, value)
            annotations[key] = text
        return [{"exp_labels": {**labels, **more, "severity": rule["labels"]["severity"]},
                 "exp_annotations": annotations}]

    def test_a_running_watchdog_with_nothing_wrong_raises_nothing(self):
        inputs = [self.up(), self.written(), self.watchdog_ran(),
                  self.series("sgg_preview_watchdog_blocked", "0x120", reason="tailscale"),
                  self.series("sgg_preview_watchdog_blocked", "0x120", reason="registry")]
        for alert in sorted(WATCHDOG_ALERTS):
            with self.subTest(alert):
                self.promtool_test(inputs, alert, ("30m", []), ("119m", []))

    def test_a_watchdog_that_stops_running(self):
        # Last run at minute 10: over 1200 s old from minute 31, firing at 36.
        inputs = [self.up(), self.written(), self.tick(), self.watchdog_ran("0+60x10 600x110")]
        self.promtool_test(inputs, "SggPreviewWatchdogSilent", ("29m", []), ("35m", []), ("36m", self.watchdog(
            "SggPreviewWatchdogSilent")))

    def test_a_watchdog_that_never_wrote_its_file(self):
        inputs = [self.up(), self.written(), self.tick()]
        self.promtool_test(inputs, "SggPreviewWatchdogSilent", ("4m", []),
                           ("6m", self.watchdog("SggPreviewWatchdogSilent", labels={})))

    def test_one_long_run_is_not_silent(self):
        # The worst case: a run ends at a slot, the next slot's run takes the
        # watchdog's whole limit (about 14 minutes), so run ends are about 19.5
        # minutes apart. Each sample is a minute, so the age reaches 1170 s.
        inputs = [self.up(), self.written(), self.tick(), self.watchdog_ran("0+60x10 600x19 1770+60x90")]
        for at in ("20m", "25m", "29m", "30m", "35m", "40m"):
            self.promtool_test(inputs, "SggPreviewWatchdogSilent", (at, []))

    def test_the_watchdog_is_not_reported_while_the_service_is_down(self):
        for up, written in (("1x120", "0+60x10 600x110"), ("1x10 0x110", "0+60x10 600x110")):
            with self.subTest(up=up):
                inputs = [self.up(up), self.written(written), self.watchdog_ran("0+60x10 600x110")]
                self.promtool_test(inputs, "SggPreviewWatchdogSilent", ("60m", []), ("110m", []))
        # And with no file ever written, as long as the service is down too:
        # stopped cleanly, or crashed (its own file frozen saying up).
        for up, written in (("0x60", "0+60x60"), ("1x60", "0+60x5 300x55")):
            with self.subTest(up=up, written=written, file="none"):
                inputs = [self.up(up), self.written(written), self.tick(written)]
                self.promtool_test(inputs, "SggPreviewWatchdogSilent", ("30m", []), ("59m", []))
        # The service gate alone: a frozen file saying up, with the clock's
        # series left fresh so the clock gate does not hide it.
        inputs = [self.up("1x60"), self.written("0+60x5 300x55"), self.tick()]
        self.promtool_test(inputs, "SggPreviewWatchdogSilent", ("30m", []), ("59m", []))

    def test_a_stopped_clock_is_the_clocks_alert_not_the_watchdogs(self):
        # The service writes its file but the clock stopped, so the job stopped too.
        inputs = [self.up(), self.written(), self.tick("0+60x10 600x110"),
                  self.watchdog_ran("0+60x10 600x110")]
        self.promtool_test(inputs, "PiBotsClockSilent", ("40m", self.firing("PiBotsClockSilent")))
        self.promtool_test(inputs, "SggPreviewWatchdogSilent", ("40m", []), ("100m", []))
        # The clock stopped at minute 28, after the watchdog went quiet at
        # minute 10: from minute 31 the clock is over a minute stale, so the
        # watchdog's own alert never fires.
        inputs = [self.up(), self.written(), self.tick("0+60x28 1680x92"),
                  self.watchdog_ran("0+60x10 600x110")]
        self.promtool_test(inputs, "SggPreviewWatchdogSilent", ("36m", []), ("40m", []), ("100m", []))
        # A stopped clock and no watchdog file at all: the absent branch is hidden too.
        inputs = [self.up(), self.written(), self.tick("0+60x10 600x110")]
        self.promtool_test(inputs, "SggPreviewWatchdogSilent", ("40m", []), ("100m", []))

    def test_a_blocked_watchdog(self):
        inputs = [self.up(), self.written(), self.watchdog_ran(),
                  self.series("sgg_preview_watchdog_blocked", "0x10 1x110", reason="tailscale"),
                  self.series("sgg_preview_watchdog_blocked", "0x120", reason="registry")]
        self.promtool_test(inputs, "SggPreviewWatchdogBlocked", ("39m", []),
                           ("41m", self.watchdog("SggPreviewWatchdogBlocked", reason="tailscale")))

    def test_a_preview_that_needs_attention(self):
        inputs = [self.up(), self.written(), self.watchdog_ran(),
                  self.series("sgg_preview_persistent_preview_needs_attention", "_x10 1x110",
                              preview="example", status="cleanup_failed")]
        self.promtool_test(inputs, "SggPreviewNeedsAttention", ("24m", []), ("26m", self.watchdog(
            "SggPreviewNeedsAttention", preview="example", status="cleanup_failed")))

    def test_a_preview_failed_for_a_moment_while_restarted_is_quiet(self):
        inputs = [self.up(), self.written(), self.watchdog_ran(),
                  self.series("sgg_preview_persistent_preview_needs_attention", "_x10 1x10 _x100",
                              preview="example", status="failed")]
        for at in ("15m", "20m", "26m", "40m"):
            self.promtool_test(inputs, "SggPreviewNeedsAttention", (at, []))

    def test_a_stale_watchdog_file_raises_only_silent(self):
        # The file froze with a preview needing attention and the watchdog blocked.
        inputs = [self.up(), self.written(), self.watchdog_ran("0+60x10 600x110"),
                  self.series("sgg_preview_persistent_preview_needs_attention", "1x120",
                              preview="example", status="cleanup_failed"),
                  self.series("sgg_preview_watchdog_blocked", "1x120", reason="registry")]
        # The file is fresh until minute 30; the alert is kept 15 minutes more.
        attention = self.watchdog("SggPreviewNeedsAttention", preview="example", status="cleanup_failed")
        self.promtool_test(inputs, "SggPreviewNeedsAttention", ("26m", attention), ("40m", attention), ("50m", []))
        self.promtool_test(inputs, "SggPreviewWatchdogBlocked", ("40m", []))
        self.promtool_test(inputs, "SggPreviewWatchdogSilent", ("40m", self.watchdog("SggPreviewWatchdogSilent")))

    def test_a_short_gap_in_the_preview_list_keeps_the_alert(self):
        # The list could not be read for 10 minutes (the series left out); the
        # preview is still broken after it, so no resolved notice in between.
        attention = self.watchdog("SggPreviewNeedsAttention", preview="example", status="cleanup_failed")
        inputs = [self.up(), self.written(), self.watchdog_ran(),
                  self.series("sgg_preview_persistent_preview_needs_attention", "1x30 _x10 1x80",
                              preview="example", status="cleanup_failed")]
        for at in ("20m", "32m", "38m", "45m"):
            self.promtool_test(inputs, "SggPreviewNeedsAttention", (at, attention))
        # A preview that was recovered resolves 15 minutes after its series goes.
        inputs[-1] = self.series("sgg_preview_persistent_preview_needs_attention", "1x30",
                                 preview="example", status="cleanup_failed")
        self.promtool_test(inputs, "SggPreviewNeedsAttention", ("40m", attention), ("50m", []))


if __name__ == "__main__":
    unittest.main()
