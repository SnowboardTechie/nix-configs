#!/usr/bin/env python3
"""services.fseventsd-watchdog: fseventsd metrics, alerts and automatic restarts.

The watchdog script runs against stand-in pgrep, ps, kill and sleep commands
and a fake clock; it never looks at or signals the real fseventsd. The module
is evaluated through a `path:` flake reference so an uncommitted module is
evaluated as it is on disk, and the generated alert rules and the script's
metrics are checked with the real promtool. Nothing is activated.
"""
import json
import os
import stat
import subprocess
import tempfile
import unittest
from functools import cache
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "fseventsd-watchdog.sh"
GIB = 1024 ** 3
START = 1_800_000_000

FAKES = {
    # pgrep -x fseventsd: the pid in $FAKE/pid, or nothing (exit 1) when absent.
    "pgrep": 'cat "$FAKE/pid" 2>/dev/null || exit 1\n',
    # ps -o rss= -o time= -p PID: "rss_kb cpu_time" from $FAKE/ps.
    "ps": 'cat "$FAKE/ps"\n',
    # kill -SIG PID: logged. -0 reports the process gone unless $FAKE/stubborn
    # exists; signals fail when $FAKE/kill_fails exists.
    "kill": '''echo "$*" >> "$FAKE/kill.log"
case $1 in
  -0) [ -e "$FAKE/stubborn" ] ;;
  *) [ ! -e "$FAKE/kill_fails" ] ;;
esac
''',
    "sleep": "exit 0\n",
}


def nix(*args):
    result = subprocess.run(["nix", *args], cwd=REPO, text=True, capture_output=True)
    if result.returncode != 0:
        raise AssertionError(result.stderr[-4000:])
    return result.stdout


def studio_expr(expr, extra="{ }"):
    """`expr` with `config`, `lib`, `pkgs` and `flake` bound to Studio, extended by `extra`."""
    return f"""
      let
        flake = builtins.getFlake "path:{REPO}";
        studio = flake.darwinConfigurations.studio;
        inherit (studio) pkgs;
        inherit (pkgs) lib;
        config = (studio.extendModules {{ modules = [ {extra} ]; }}).config;
        flagValue = flag: args: lib.removePrefix flag (lib.findFirst (lib.hasPrefix flag) null args);
        readJSON = path: builtins.fromJSON (builtins.unsafeDiscardStringContext (builtins.readFile path));
        prometheusConfig = flagValue "--config.file="
          config.launchd.daemons.prometheus.serviceConfig.ProgramArguments;
        watchdog = config.launchd.daemons.fseventsd-watchdog.serviceConfig;
      in {expr}
    """


@cache
def studio(expr, extra="{ }"):
    return json.loads(nix("eval", "--json", "--impure", "--expr", studio_expr(expr, extra)))


@cache
def realised():
    """Studio's generated Prometheus config, Grafana provisioning and promtool, built locally."""
    out = nix("build", "--no-link", "--print-out-paths", "--impure", "--expr", studio_expr("""
      pkgs.writeText "fseventsd-watchdog-paths.json" (builtins.toJSON {
        prometheusConfig = prometheusConfig;
        grafanaProvisioning = config.launchd.user.agents.grafana.serviceConfig.EnvironmentVariables.GF_PATHS_PROVISIONING;
        script = lib.last watchdog.ProgramArguments;
        promtool = "${pkgs.prometheus.cli}/bin/promtool";
      })
    """))
    return json.loads(Path(out.strip().splitlines()[-1]).read_text())


def rules():
    """The generated alert rules by name."""
    rule_file = json.loads(Path(realised()["prometheusConfig"]).read_text())["rule_files"][0]
    return {r["alert"]: r for g in json.loads(Path(rule_file).read_text())["groups"] for r in g["rules"]}


class Watchdog:
    """One fseventsd as seen through the stand-ins, and the script's view of it."""

    def __init__(self, test, **env):
        tmp = Path(tempfile.mkdtemp())
        test.addCleanup(subprocess.run, ["rm", "-rf", str(tmp)])
        self.fake = tmp / "fake"
        self.fake.mkdir()
        bin_dir = tmp / "bin"
        bin_dir.mkdir()
        for name, body in FAKES.items():
            path = bin_dir / name
            path.write_text("#!/bin/sh\n" + body)
            path.chmod(0o755)
        self.state_dir = tmp / "state"
        self.metrics_dir = tmp / "metrics"
        self.now = START
        self.env = {
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "FAKE": str(self.fake),
            "STATE_DIR": str(self.state_dir),
            "METRICS_DIR": str(self.metrics_dir),
            "RESTART_MEMORY_BYTES": str(2 * GIB),
            "RESTART_CPU_PERCENT": "90",
            "SUSTAINED_CHECKS": "3",
            "COOLDOWN_SECONDS": "3600",
            "PGREP_CMD": str(bin_dir / "pgrep"),
            "PS_CMD": str(bin_dir / "ps"),
            "KILL_CMD": str(bin_dir / "kill"),
            "SLEEP_CMD": str(bin_dir / "sleep"),
            **env,
        }
        self.output = ""

    def process(self, pid, rss_bytes=10 * 1024 * 1024, cpu_time="0:35.74"):
        (self.fake / "pid").write_text(f"{pid}\n")
        (self.fake / "ps").write_text(f"{rss_bytes // 1024} {cpu_time}\n")

    def gone(self):
        (self.fake / "pid").unlink(missing_ok=True)

    def check(self, advance=60, env=None):
        """Run one check `advance` seconds after the previous one, with `env` overriding the defaults."""
        self.now += advance
        result = subprocess.run(["/bin/sh", str(SCRIPT)], env={**self.env, "NOW": str(self.now), **(env or {})},
                                text=True, capture_output=True)
        self.output += result.stdout
        return result

    def kills(self):
        log = self.fake / "kill.log"
        return log.read_text().splitlines() if log.exists() else []

    def metrics(self):
        values = {}
        for line in (self.metrics_dir / "fseventsd.prom").read_text().splitlines():
            if line and not line.startswith("#"):
                name, value = line.rsplit(" ", 1)
                values[name] = float(value)
        return values


def cpu_time(seconds):
    return f"{seconds // 60}:{seconds % 60:02d}.00"


class Checks(unittest.TestCase):
    def test_a_healthy_check_exports_metrics_and_stays_quiet(self):
        w = Watchdog(self)
        w.process(113)
        result = w.check()
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("", result.stdout + result.stderr)
        self.assertEqual({
            "fseventsd_up": 1,
            "fseventsd_resident_memory_bytes": 10 * 1024 * 1024,
            "fseventsd_cpu_seconds_total": 35.74,
            "fseventsd_watchdog_over_limit_checks": 0,
            'fseventsd_watchdog_restarts_total{reason="memory"}': 0,
            'fseventsd_watchdog_restarts_total{reason="cpu"}': 0,
            "fseventsd_watchdog_last_restart_timestamp_seconds": 0,
            "fseventsd_watchdog_last_check_timestamp_seconds": w.now,
            "fseventsd_watchdog_restart_memory_bytes": 2 * GIB,
            "fseventsd_watchdog_restart_cpu_percent": 90,
        }, w.metrics())
        self.assertEqual([], w.kills())

    def test_memory_over_the_limit_restarts_after_the_sustained_checks(self):
        w = Watchdog(self)
        w.process(113, rss_bytes=12 * GIB)
        w.check()
        w.check()
        self.assertEqual([], w.kills())
        self.assertEqual(2, w.metrics()["fseventsd_watchdog_over_limit_checks"])
        w.check()
        self.assertEqual(["-TERM 113", "-0 113"], w.kills())
        metrics = w.metrics()
        self.assertEqual(1, metrics['fseventsd_watchdog_restarts_total{reason="memory"}'])
        self.assertEqual(0, metrics['fseventsd_watchdog_restarts_total{reason="cpu"}'])
        self.assertEqual(w.now, metrics["fseventsd_watchdog_last_restart_timestamp_seconds"])
        self.assertEqual(0, metrics["fseventsd_watchdog_over_limit_checks"])
        self.assertNotIn("fseventsd_resident_memory_bytes", metrics)
        self.assertIn("is over its memory limit: memory 12288 MiB", w.output)
        self.assertIn("restarting fseventsd (pid 113): over its memory limit for 3 checks", w.output)
        # launchd's relaunch is a new, healthy process.
        w.process(2001)
        w.check()
        self.assertEqual(1, w.metrics()["fseventsd_up"])
        self.assertEqual(2, len(w.kills()))

    def test_a_full_core_between_checks_restarts_after_the_sustained_checks(self):
        w = Watchdog(self)
        used = 0
        for _ in range(3):  # the first check is only the CPU baseline
            w.process(113, cpu_time=cpu_time(used))
            w.check()
            used += 60
        self.assertEqual([], w.kills())
        w.process(113, cpu_time=cpu_time(used))
        w.check()
        self.assertEqual(["-TERM 113", "-0 113"], w.kills())
        self.assertEqual(1, w.metrics()['fseventsd_watchdog_restarts_total{reason="cpu"}'])
        self.assertIn("over its cpu limit: memory 10 MiB, cpu 100%", w.output)

    def test_busy_but_under_the_cpu_limit_never_restarts(self):
        w = Watchdog(self)
        for minute in range(10):
            w.process(113, cpu_time=cpu_time(minute * 30))
            w.check()
        self.assertEqual([], w.kills())
        self.assertEqual(0, w.metrics()["fseventsd_watchdog_over_limit_checks"])
        self.assertEqual("", w.output)

    def test_a_dip_under_the_limits_starts_the_count_again(self):
        w = Watchdog(self)
        w.process(113, rss_bytes=3 * GIB)
        w.check()
        w.check()
        w.process(113)
        w.check()
        self.assertIn("back under its limits after 2 checks", w.output)
        w.process(113, rss_bytes=3 * GIB)
        w.check()
        w.check()
        self.assertEqual([], w.kills())
        w.check()
        self.assertEqual(["-TERM 113", "-0 113"], w.kills())

    def test_a_new_process_starts_the_count_again(self):
        w = Watchdog(self)
        w.process(113, rss_bytes=3 * GIB)
        w.check()
        w.check()
        w.process(114, rss_bytes=3 * GIB)  # restarted by hand or by launchd
        w.check()
        w.check()
        self.assertEqual([], w.kills())
        self.assertEqual(2, w.metrics()["fseventsd_watchdog_over_limit_checks"])
        w.check()
        self.assertEqual(["-TERM 114", "-0 114"], w.kills())

    def test_the_cooldown_holds_off_a_second_restart(self):
        w = Watchdog(self)
        w.process(113, rss_bytes=3 * GIB)
        for _ in range(3):
            w.check()
        self.assertEqual(1, len([k for k in w.kills() if k.startswith("-TERM")]))
        w.process(114, rss_bytes=3 * GIB)  # bloated again straight away
        for _ in range(10):
            w.check()
        self.assertEqual(["-TERM 113"], [k for k in w.kills() if k.startswith("-TERM")])
        self.assertEqual(1, w.output.count("waiting for the 3600s cooldown"))
        self.assertEqual(10, w.metrics()["fseventsd_watchdog_over_limit_checks"])
        w.check(advance=3600)
        self.assertEqual(["-TERM 113", "-TERM 114"], [k for k in w.kills() if k.startswith("-TERM")])
        self.assertEqual(2, w.metrics()['fseventsd_watchdog_restarts_total{reason="memory"}'])

    def test_sigkill_when_sigterm_is_ignored(self):
        w = Watchdog(self)
        (w.fake / "stubborn").touch()
        w.process(113, rss_bytes=3 * GIB)
        for _ in range(3):
            w.check()
        self.assertEqual(["-TERM 113"] + ["-0 113"] * 11 + ["-KILL 113"], w.kills())
        self.assertIn("ignored SIGTERM for 10s; sending SIGKILL", w.output)

    def test_a_failed_signal_is_not_counted_and_is_retried(self):
        w = Watchdog(self)
        (w.fake / "kill_fails").touch()
        w.process(113, rss_bytes=3 * GIB)
        for _ in range(3):
            w.check()
        self.assertIn("could not signal fseventsd (pid 113)", w.output)
        self.assertEqual(0, w.metrics()['fseventsd_watchdog_restarts_total{reason="memory"}'])
        (w.fake / "kill_fails").unlink()
        w.check()
        self.assertEqual(["-TERM 113", "-TERM 113", "-0 113"], w.kills())
        self.assertEqual(1, w.metrics()['fseventsd_watchdog_restarts_total{reason="memory"}'])

    def test_not_running_reports_down_and_logs_once_it_disappears(self):
        w = Watchdog(self)
        w.check()
        self.assertEqual(0, w.metrics()["fseventsd_up"])
        self.assertNotIn("fseventsd_resident_memory_bytes", w.metrics())
        self.assertEqual("", w.output)
        w.process(113)
        w.check()
        w.gone()
        w.check()
        w.check()
        self.assertEqual(1, w.output.count("fseventsd is not running (last seen as pid 113)"))
        self.assertEqual([], w.kills())

    def test_counters_survive_between_checks(self):
        w = Watchdog(self)
        w.process(113, rss_bytes=3 * GIB)
        for _ in range(3):
            w.check()
        w.process(114)
        for _ in range(5):
            w.check()
        self.assertEqual(1, w.metrics()['fseventsd_watchdog_restarts_total{reason="memory"}'])

    def test_ps_cpu_time_formats(self):
        for shown, seconds in (("536:57.38", 32217.38), ("1:02:03.50", 3723.5),
                               ("2-01:00:00.00", 176400.0), ("0:00.00", 0.0)):
            with self.subTest(shown=shown):
                w = Watchdog(self)
                w.process(113, cpu_time=shown)
                w.check()
                self.assertEqual(seconds, w.metrics()["fseventsd_cpu_seconds_total"])

    def test_unreadable_ps_output_counts_as_not_running(self):
        w = Watchdog(self)
        (w.fake / "pid").write_text("113\n")
        (w.fake / "ps").write_text("")
        w.check()
        self.assertEqual(0, w.metrics()["fseventsd_up"])
        self.assertEqual([], w.kills())

    def test_refuses_a_bad_configuration(self):
        for name, value in (("SUSTAINED_CHECKS", "ten"), ("RESTART_MEMORY_BYTES", "-1"),
                            ("COOLDOWN_SECONDS", ""), ("STATE_DIR", "relative/state")):
            with self.subTest(name=name, value=value):
                w = Watchdog(self)
                w.process(113, rss_bytes=3 * GIB)
                result = w.check(env={name: value})
                self.assertEqual(2, result.returncode)
                self.assertIn(name, result.stderr)
                self.assertEqual([], w.kills())

    def test_state_is_private_and_metrics_are_readable(self):
        w = Watchdog(self)
        w.process(113)
        w.check()
        self.assertEqual(0o600, stat.S_IMODE(os.stat(w.state_dir / "state").st_mode))
        self.assertEqual(0o644, stat.S_IMODE(os.stat(w.metrics_dir / "fseventsd.prom").st_mode))
        self.assertEqual(["fseventsd.prom"], sorted(p.name for p in w.metrics_dir.iterdir()))

    def test_promtool_accepts_the_metrics(self):
        promtool = realised()["promtool"]
        for setup in ("healthy", "restarted", "down"):
            with self.subTest(setup=setup):
                w = Watchdog(self)
                if setup != "down":
                    w.process(113, rss_bytes=3 * GIB if setup == "restarted" else 10 * 1024 * 1024)
                for _ in range(3):
                    w.check()
                text = (w.metrics_dir / "fseventsd.prom").read_text()
                result = subprocess.run([promtool, "check", "metrics"], input=text, text=True, capture_output=True)
                self.assertEqual(0, result.returncode, result.stdout + result.stderr)


class Wiring(unittest.TestCase):
    def test_studio_runs_the_script_as_a_root_daemon_every_minute(self):
        daemon = studio("watchdog")
        self.assertEqual("org.nixos.fseventsd-watchdog", daemon["Label"])
        self.assertIsNone(daemon["UserName"])
        self.assertEqual(60, daemon["StartInterval"])
        self.assertTrue(daemon["RunAtLoad"])
        self.assertEqual(["/bin/sh", "-c"], daemon["ProgramArguments"][:2])
        self.assertEqual("wait-for-nix-store", daemon["ProgramArguments"][3])
        self.assertEqual("/var/log/fseventsd-watchdog.log", daemon["StandardOutPath"])
        self.assertEqual("/var/log/fseventsd-watchdog.log", daemon["StandardErrorPath"])
        self.assertEqual({
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "STATE_DIR": "/var/db/fseventsd-watchdog",
            "METRICS_DIR": "/var/db/fseventsd-watchdog/metrics",
            "RESTART_MEMORY_BYTES": str(2 * GIB),
            "RESTART_CPU_PERCENT": "90",
            "SUSTAINED_CHECKS": "15",
            "COOLDOWN_SECONDS": "3600",
        }, daemon["EnvironmentVariables"])
        script = Path(realised()["script"])
        self.assertEqual(SCRIPT.read_text(), script.read_text())
        self.assertTrue(os.access(script, os.X_OK))

    def test_activation_creates_root_owned_directories(self):
        activation = studio("config.system.activationScripts.extraActivation.text")
        self.assertIn("/usr/bin/install -d -o root -g wheel -m 0755 "
                      "/var/db/fseventsd-watchdog /var/db/fseventsd-watchdog/metrics", activation)

    def test_node_exporter_reads_the_metrics(self):
        args = studio("config.launchd.user.agents.node-exporter.serviceConfig.ProgramArguments")
        self.assertIn("--collector.textfile.directory=/var/db/fseventsd-watchdog/metrics", args)

    def test_without_the_watchdog_monitoring_is_unchanged(self):
        off = "{ services.fseventsd-watchdog.enable = lib.mkForce false; }"
        # Other modules (nas-storage-health) add their own; none of the watchdog's remain.
        args = studio("config.launchd.user.agents.node-exporter.serviceConfig.ProgramArguments", off)
        self.assertFalse(any("fseventsd" in a for a in args))
        self.assertFalse(studio("config.launchd.daemons ? fseventsd-watchdog", off))
        self.assertEqual({}, studio("config.services.monitoring.extraDashboards", off))
        rules = studio("map (r: r.alert) config.services.monitoring.extraAlertRules", off)
        self.assertFalse(any(r.startswith("Fseventsd") for r in rules), rules)

    def test_a_host_without_monitoring_gets_just_the_watchdog(self):
        result = json.loads(nix("eval", "--json", "--impure", "--expr", f"""
          let
            flake = builtins.getFlake "path:{REPO}";
            mbp = flake.darwinConfigurations.mbp.extendModules {{ modules = [
              flake.modules.darwin.fseventsd-watchdog
              {{ services.fseventsd-watchdog.enable = true; }}
            ]; }};
          in {{
            interval = mbp.config.launchd.daemons.fseventsd-watchdog.serviceConfig.StartInterval;
            monitoring = mbp.options.services ? monitoring;
          }}
        """))
        self.assertEqual({"interval": 60, "monitoring": False}, result)

    def test_alert_limits_above_restart_limits_are_refused(self):
        failed = studio("map (a: a.message) (builtins.filter (a: !a.assertion) config.assertions)",
                        "{ services.fseventsd-watchdog.alertCpuPercent = 95; }")
        self.assertTrue(any("alert limits must not exceed restart limits" in m for m in failed), failed)
        self.assertEqual([], studio("map (a: a.message) (builtins.filter (a: !a.assertion) config.assertions)"))

    def test_extra_dashboard_names_are_checked(self):
        for name in ("service-health.json", "../escape.json", "Upper.json"):
            with self.subTest(name=name):
                failed = studio("map (a: a.message) (builtins.filter (a: !a.assertion) config.assertions)",
                                f'{{ services.monitoring.extraDashboards."{name}" = pkgs.writeText "x.json" "{{}}"; }}')
                self.assertTrue(any("extraDashboards names" in m for m in failed), failed)


class Grafana(unittest.TestCase):
    def dashboard(self):
        provisioning = Path(realised()["grafanaProvisioning"])
        provider = json.loads((provisioning / "dashboards" / "default.yml").read_text())
        directory = Path(provider["providers"][0]["options"]["path"])
        self.assertEqual({"node-exporter-macos.json", "service-health.json", "unraid.json", "studio-logs.json",
                          "fseventsd.json"}, {p.name for p in directory.iterdir()})
        return json.loads((directory / "fseventsd.json").read_text())

    def test_the_dashboard_is_provisioned_with_the_configured_limits(self):
        dashboard = self.dashboard()
        self.assertEqual("fseventsd", dashboard["uid"])
        panels = {p["id"]: p for p in dashboard["panels"]}
        self.assertEqual(len(panels), len(dashboard["panels"]))
        memory = panels[5]
        self.assertEqual("fseventsd_resident_memory_bytes", memory["targets"][0]["expr"])
        self.assertEqual([None, GIB, 2 * GIB],
                         [s["value"] for s in memory["fieldConfig"]["defaults"]["thresholds"]["steps"]])
        cpu = panels[6]
        self.assertEqual([None, 80, 90], [s["value"] for s in cpu["fieldConfig"]["defaults"]["thresholds"]["steps"]])
        for panel in dashboard["panels"]:
            self.assertEqual({"type": "prometheus", "uid": "Prometheus"}, panel["datasource"])
            for target in panel["targets"]:
                self.assertTrue(target["expr"].startswith(("fseventsd_", "rate(fseventsd_", "time() - fseventsd_",
                                                            "sum(round(increase(fseventsd_", "round(increase(fseventsd_")),
                                target["expr"])


class Rules(unittest.TestCase):
    INSTANCE = {"instance": "localhost:9100", "job": "node_exporter"}

    def series(self, metric, values, **labels):
        all_labels = ",".join(f'{k}="{v}"' for k, v in {**self.INSTANCE, **labels}.items())
        return {"series": f"{metric}{{{all_labels}}}", "values": values}

    def firing(self, name, labels, templates):
        annotations = {}
        for key, text in rules()[name]["annotations"].items():
            for template, value in templates.items():
                text = text.replace(template, value)
            self.assertNotIn("{{", text, f"{name} {key} left a template unfilled")
            annotations[key] = text
        return [{"exp_labels": {**labels, "severity": rules()[name]["labels"]["severity"]},
                 "exp_annotations": annotations}]

    def promtool_test(self, tests):
        paths = realised()
        rule_file = json.loads(Path(paths["prometheusConfig"]).read_text())["rule_files"][0]
        spec = {"rule_files": [rule_file], "evaluation_interval": "1m", "tests": tests}
        with tempfile.TemporaryDirectory() as tmp:
            test_file = Path(tmp) / "fseventsd-rule-test.json"
            test_file.write_text(json.dumps(spec))
            result = subprocess.run([paths["promtool"], "test", "rules", str(test_file)],
                                    text=True, capture_output=True)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)

    def case(self, inputs, alertname, *expectations):
        return {"interval": "1m", "input_series": inputs, "alert_rule_test": [
            {"eval_time": t, "alertname": alertname, "exp_alerts": exp} for t, exp in expectations]}

    def test_every_rule_is_generated_and_promtool_accepts_them(self):
        self.assertTrue({"FseventsdHighMemory", "FseventsdHighCpu", "FseventsdRestartedByWatchdog",
                         "FseventsdDown", "FseventsdWatchdogSilent", "HighCpuUsage",
                         "AlertDeliveryFailed"} <= set(rules()))
        paths = realised()
        rule_file = json.loads(Path(paths["prometheusConfig"]).read_text())["rule_files"][0]
        result = subprocess.run([paths["promtool"], "check", "rules", rule_file], text=True, capture_output=True)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)

    def test_high_memory_warns_after_ten_minutes(self):
        templates = {"{{ $value | humanize1024 }}": "2Gi", "{{ $labels.instance }}": "localhost:9100"}
        self.promtool_test([self.case(
            [self.series("fseventsd_resident_memory_bytes", f"10485760x10 {2 * GIB}x40")],
            "FseventsdHighMemory",
            ("5m", []), ("15m", []), ("25m", self.firing("FseventsdHighMemory", self.INSTANCE, templates)))])
        self.assertIn("over 2048 MiB for about 15 minutes", rules()["FseventsdHighMemory"]["annotations"]["description"])

    def test_high_cpu_warns_after_ten_minutes_and_not_below_the_limit(self):
        templates = {'{{ $value | printf "%.0f" }}': "100", "{{ $labels.instance }}": "localhost:9100"}
        self.promtool_test([
            self.case([self.series("fseventsd_cpu_seconds_total", "0+60x40")], "FseventsdHighCpu",
                      ("10m", []), ("25m", self.firing("FseventsdHighCpu", self.INSTANCE, templates))),
            self.case([self.series("fseventsd_cpu_seconds_total", "0+30x40")], "FseventsdHighCpu",
                      ("30m", [])),
        ])

    def test_a_restart_alerts_once_with_its_reason(self):
        templates = {"{{ $labels.reason }}": "memory", "{{ $labels.instance }}": "localhost:9100"}
        self.promtool_test([self.case(
            [self.series("fseventsd_watchdog_restarts_total", "0x20 1x40", reason="memory"),
             self.series("fseventsd_watchdog_restarts_total", "0x60", reason="cpu")],
            "FseventsdRestartedByWatchdog",
            ("15m", []),
            ("22m", self.firing("FseventsdRestartedByWatchdog", {**self.INSTANCE, "reason": "memory"}, templates)),
            ("40m", []))])

    def test_down_is_critical_after_five_minutes(self):
        self.promtool_test([self.case(
            [self.series("fseventsd_up", "1x10 0x20")], "FseventsdDown",
            ("12m", []),
            ("17m", self.firing("FseventsdDown", self.INSTANCE, {"{{ $labels.instance }}": "localhost:9100"})))])
        self.assertEqual("critical", rules()["FseventsdDown"]["labels"]["severity"])

    def test_a_silent_watchdog_alerts(self):
        self.promtool_test([
            # Checks stop after 10 minutes: the series goes stale, then absent.
            self.case([self.series("fseventsd_watchdog_last_check_timestamp_seconds", "60+60x9")],
                      "FseventsdWatchdogSilent",
                      ("9m", []), ("30m", self.firing("FseventsdWatchdogSilent", {}, {}))),
            # Still exported but frozen (the daemon stopped, node_exporter serves the old file).
            self.case([self.series("fseventsd_watchdog_last_check_timestamp_seconds", "60x60")],
                      "FseventsdWatchdogSilent",
                      ("5m", []),
                      ("30m", self.firing("FseventsdWatchdogSilent", self.INSTANCE, {}))),
        ])


if __name__ == "__main__":
    unittest.main()
