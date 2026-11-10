#!/usr/bin/env python3
"""services.nas-storage-health: btrfs error rules and the NAS write probe.

The probe script runs against a stand-in mount table and temporary
directories; it never touches a real share. The module is evaluated through a
`path:` flake reference so uncommitted changes are evaluated as on disk, and
the generated rules are checked with the real promtool, including
`promtool test rules` on series shaped like the NAS's own exporter output
(per-device btrfs error counters that jump when a device drops off, while
the filesystem's read-only flag stays 0). Nothing is activated.
"""
import json
import os
import signal
import subprocess
import tempfile
import time
import unittest
from functools import cache
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "nas-write-probe.sh"
NODE = {"instance": "192.168.1.3:9100", "job": "unraid-node", "host": "unraid"}
POOL = "11111111-2222-3333-4444-555555555555"
DOCKER = "66666666-7777-8888-9999-000000000000"


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
        probe = config.launchd.user.agents.nas-write-probe.serviceConfig;
      in {expr}
    """


@cache
def studio(expr, extra="{ }"):
    return json.loads(nix("eval", "--json", "--impure", "--expr", studio_expr(expr, extra)))


@cache
def realised():
    out = nix("build", "--no-link", "--print-out-paths", "--impure", "--expr", studio_expr("""
      pkgs.writeText "nas-storage-health-paths.json" (builtins.toJSON {
        prometheusConfig = prometheusConfig;
        promtool = "${pkgs.prometheus.cli}/bin/promtool";
        timeout = "${pkgs.coreutils}/bin/timeout";
      })
    """))
    return json.loads(Path(out.strip().splitlines()[-1]).read_text())


def rule_file():
    return json.loads(Path(realised()["prometheusConfig"]).read_text())["rule_files"][0]


def rules():
    return {r["alert"]: r for g in json.loads(Path(rule_file()).read_text())["groups"] for r in g["rules"]}


class Probe:
    """One run of the probe against stand-in shares."""

    def __init__(self, test, shares=("Bryan",)):
        self.tmp = Path(tempfile.mkdtemp())
        test.addCleanup(subprocess.run, ["chmod", "-R", "u+w", str(self.tmp)])
        test.addCleanup(subprocess.run, ["rm", "-rf", str(self.tmp)])
        self.metrics = self.tmp / "metrics"
        self.table = self.tmp / "mount-table"
        mount_cmd = self.tmp / "mount"
        mount_cmd.write_text('#!/bin/sh\ncat "$TABLE"\n')
        mount_cmd.chmod(0o755)
        self.args = []
        self.lines = []
        for name in shares:
            mp = self.tmp / "Volumes" / name
            (mp / "Backups").mkdir(parents=True)
            self.args += [name, str(mp), str(mp / "Backups")]
            self.lines.append(f"//bryan@nas/{name} on {mp} (smbfs, nodev, nosuid, mounted by bryan)")
        self.mount(*shares)
        self.env = {
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "TABLE": str(self.table),
            "METRICS_DIR": str(self.metrics),
            "MOUNT_CMD": str(mount_cmd),
            "MOUNT_DEVICE_CHECK": "0",
            "TIMEOUT_CMD": realised()["timeout"],
            "TIMEOUT": "5",
            "RETRY_DELAY": "0",
            "NOW": "1800000000",
        }

    def share(self, name):
        return self.tmp / "Volumes" / name

    def mount(self, *names):
        self.table.write_text("".join(l + "\n" for l in self.lines if any(f"/{n} on " in l for n in names)))

    def run(self, *args, **env):
        return subprocess.run(["/bin/sh", str(SCRIPT), *(args or self.args)], cwd=self.tmp,
                              env={**self.env, **env}, text=True, capture_output=True)

    def metrics_text(self):
        return (self.metrics / "nas-write-probe.prom").read_text()

    def value(self, metric, share):
        for line in self.metrics_text().splitlines():
            if line.startswith(f'{metric}{{share="{share}"}} '):
                return int(line.split()[-1])
        raise AssertionError(f"{metric} for {share} missing:\n{self.metrics_text()}")


class ProbeScript(unittest.TestCase):
    def test_a_writable_share_passes_and_leaves_nothing_behind(self):
        p = Probe(self)
        result = p.run()
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("", result.stdout)
        self.assertEqual(1, p.value("nas_write_probe_success", "Bryan"))
        self.assertEqual(1, p.value("nas_write_probe_mounted", "Bryan"))
        self.assertIn("nas_write_probe_last_run_timestamp_seconds 1800000000", p.metrics_text())
        self.assertEqual([], list((p.share("Bryan") / "Backups").iterdir()))

    def test_a_passing_blip_is_retried_once(self):
        p = Probe(self)
        # The mount table is empty on the first look only.
        flaky = p.tmp / "flaky-mount"
        flaky.write_text('#!/bin/sh\nif [ -e "$TABLE.seen" ]; then cat "$TABLE"; else : > "$TABLE.seen"; fi\n')
        flaky.chmod(0o755)
        result = p.run(MOUNT_CMD=str(flaky))
        self.assertEqual(1, p.value("nas_write_probe_success", "Bryan"))
        self.assertIn("Bryan: write probe passed on the retry", result.stdout)

    def test_a_read_only_share_fails_after_one_retry(self):
        p = Probe(self)
        (p.share("Bryan") / "Backups").chmod(0o555)
        result = p.run()
        self.assertEqual(0, p.value("nas_write_probe_success", "Bryan"))
        self.assertEqual(1, p.value("nas_write_probe_mounted", "Bryan"))
        self.assertIn("Bryan: write probe failed twice; could not write", result.stdout)

    def test_an_unmounted_share_is_reported_not_written(self):
        p = Probe(self)
        p.mount()
        result = p.run()
        self.assertEqual(0, p.value("nas_write_probe_success", "Bryan"))
        self.assertEqual(0, p.value("nas_write_probe_mounted", "Bryan"))
        self.assertIn("find", result.stdout)
        self.assertIn("mounted (smbfs)", result.stdout)
        self.assertEqual([], list((p.share("Bryan") / "Backups").iterdir()))

    def test_a_non_smb_mount_does_not_count(self):
        p = Probe(self)
        p.table.write_text(p.table.read_text().replace("(smbfs,", "(apfs,"))
        p.run()
        self.assertEqual(0, p.value("nas_write_probe_mounted", "Bryan"))

    def test_a_missing_directory_fails_and_is_not_created(self):
        p = Probe(self)
        (p.share("Bryan") / "Backups").rmdir()
        result = p.run()
        self.assertEqual(0, p.value("nas_write_probe_success", "Bryan"))
        self.assertFalse((p.share("Bryan") / "Backups").exists())
        self.assertIn("find the directory", result.stdout)

    def test_a_hanging_write_times_out(self):
        p = Probe(self)
        # The write step never returns; the real timeout ends it (exit 124)
        # and the probe reports, not hangs.
        hang = self.stand_in_timeout(p, "hang-write", f'''if [ "$1" = /bin/sh ]; then
  exec {realised()["timeout"]} -k 1 1 /bin/sleep 30
fi
exec "$@"
''')
        result = subprocess.run(["/bin/sh", str(SCRIPT), *p.args], env={**p.env, "TIMEOUT_CMD": hang},
                                text=True, capture_output=True, timeout=60)
        self.assertEqual(0, p.value("nas_write_probe_success", "Bryan"))
        self.assertIn("could not write", result.stdout)

    def test_a_share_returning_other_content_fails(self):
        p = Probe(self)
        # A stand-in timeout that runs each step but answers every read with
        # stale content, as a share serving a cached copy would.
        stale = self.stand_in_timeout(p, "stale", 'if [ "$1" = /bin/cat ]; then echo stale; exit 0; fi\nexec "$@"\n')
        result = p.run(TIMEOUT_CMD=stale)
        self.assertEqual(0, p.value("nas_write_probe_success", "Bryan"))
        self.assertIn("could not compare", result.stdout)
        self.assertEqual([], list((p.share("Bryan") / "Backups").iterdir()))

    def stand_in_timeout(self, p, name, body):
        """A TIMEOUT_CMD stand-in: drops `-k 5 SECONDS`, then runs `body` with the command in "$@"."""
        path = p.tmp / name
        path.write_text("#!/bin/sh\nshift 3\n" + body)
        path.chmod(0o755)
        return str(path)

    def test_a_share_hanging_on_the_directory_check_fails_instead_of_blocking(self):
        p = Probe(self)
        # Only `test -d` on the share hangs (a stalled server); the real timeout ends it.
        hang = self.stand_in_timeout(p, "hang-test", f'''if [ "$1" = /bin/test ]; then
  exec {realised()["timeout"]} 1 /bin/sleep 30
fi
exec "$@"
''')
        result = p.run(TIMEOUT_CMD=hang)
        self.assertEqual(0, p.value("nas_write_probe_success", "Bryan"))
        self.assertEqual(1, p.value("nas_write_probe_mounted", "Bryan"))
        self.assertIn("find the directory", result.stdout)

    def test_a_write_that_fails_after_creating_the_file_is_cleaned_up(self):
        p = Probe(self)
        # The write step creates the file, then fails (a partial write, a full
        # disk). "$@" is: /bin/sh -c SCRIPT probe FILE TOKEN.
        partial = self.stand_in_timeout(p, "partial-write", '''if [ "$1" = /bin/sh ]; then
  : > "$5"; exit 1
fi
exec "$@"
''')
        result = p.run(TIMEOUT_CMD=partial)
        self.assertEqual(0, p.value("nas_write_probe_success", "Bryan"))
        self.assertFalse((p.tmp / "probe").exists())
        self.assertIn("could not write", result.stdout)
        self.assertEqual([], list((p.share("Bryan") / "Backups").iterdir()))

    def test_a_run_stopped_mid_probe_leaves_nothing_behind(self):
        p = Probe(self)
        marker = p.tmp / "reading"
        # The read-back step blocks after the file is written; then the run's
        # process group is terminated, as a logout does. (A SIGKILL leaves the
        # small, distinctly named file behind; nothing can clean that up.)
        block = self.stand_in_timeout(p, "block-read", f'''if [ "$1" = /bin/cat ]; then
  : > {marker}; exec /bin/sleep 30
fi
exec "$@"
''')
        proc = subprocess.Popen(["/bin/sh", str(SCRIPT), *p.args], env={**p.env, "TIMEOUT_CMD": block},
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                start_new_session=True)
        for _ in range(100):
            if marker.exists():
                break
            time.sleep(0.05)
        self.assertTrue(marker.exists())
        self.assertEqual(1, len(list((p.share("Bryan") / "Backups").iterdir())))
        os.killpg(proc.pid, signal.SIGTERM)
        proc.communicate(timeout=30)
        self.assertEqual(143, proc.returncode)
        self.assertEqual([], list((p.share("Bryan") / "Backups").iterdir()))
        self.assertEqual([], [f.name for f in p.metrics.iterdir()] if p.metrics.exists() else [])

    def test_the_metrics_file_is_replaced_whole(self):
        p = Probe(self)
        p.metrics.mkdir()
        target = p.metrics / "nas-write-probe.prom"
        target.write_text("old\n")
        before = target.stat().st_ino
        p.run()
        # A rename gives a new inode; writing in place keeps the old one.
        self.assertNotEqual(before, target.stat().st_ino)
        self.assertEqual(["nas-write-probe.prom"], [f.name for f in p.metrics.iterdir()])

    def test_one_failing_share_does_not_hide_another(self):
        p = Probe(self, shares=("Bryan", "media"))
        (p.share("media") / "Backups").chmod(0o555)
        p.run()
        self.assertEqual(1, p.value("nas_write_probe_success", "Bryan"))
        self.assertEqual(0, p.value("nas_write_probe_success", "media"))

    def test_refuses_bad_arguments(self):
        p = Probe(self)
        for args in (["Bryan", "/v"], ["bad name", "/v", "/v/d"], ["Bryan", "rel", "/v/d"],
                     ["Bryan", "/v", "/v/../etc"], ["Bryan", "/v", "/v//d"], ["Bryan", "/v", "/w/d"],
                     ["Bryan", "/v", "/vx/d"]):
            result = p.run(*args)
            self.assertEqual(2, result.returncode, args)
        self.assertEqual(2, p.run(METRICS_DIR="relative").returncode)
        self.assertEqual(2, p.run(TIMEOUT="0").returncode)
        self.assertFalse((p.metrics / "nas-write-probe.prom").exists())

    def test_promtool_accepts_the_metrics(self):
        p = Probe(self, shares=("Bryan", "media"))
        (p.share("media") / "Backups").chmod(0o555)
        p.run()
        result = subprocess.run([realised()["promtool"], "check", "metrics"], input=p.metrics_text(),
                                text=True, capture_output=True)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)


class Module(unittest.TestCase):
    def test_studio_probes_the_backup_share_every_ten_minutes(self):
        agent = studio("probe")
        self.assertEqual("org.nixos.nas-write-probe", agent["Label"])
        self.assertEqual(["Bryan", "/Users/bryan/NAS/Bryan", "/Users/bryan/NAS/Bryan/Backups"], agent["ProgramArguments"][1:])
        self.assertEqual(600, agent["StartInterval"])
        self.assertTrue(agent["RunAtLoad"])
        self.assertGreater(agent["ExitTimeOut"], int(agent["EnvironmentVariables"]["TIMEOUT"]) + 5)
        self.assertTrue(agent["EnvironmentVariables"]["TIMEOUT_CMD"].endswith("/bin/timeout"))

    def test_node_exporter_reads_the_probe_metrics(self):
        args = studio("config.launchd.user.agents.node-exporter.serviceConfig.ProgramArguments")
        self.assertIn("--collector.textfile.directory=/Users/bryan/.local/state/nas-storage-health/metrics", args)

    def test_without_shares_only_the_btrfs_rules_are_added(self):
        extra = "{ services.nas-storage-health.shares = lib.mkForce { }; }"
        got = studio("""{
          agent = config.launchd.user.agents ? nas-write-probe;
          rules = map (r: r.alert) config.services.monitoring.extraAlertRules;
          dirs = config.services.monitoring.nodeExporter.textfileDirectories;
        }""", extra)
        self.assertFalse(got["agent"])
        self.assertIn("NasBtrfsDeviceErrors", got["rules"])
        self.assertNotIn("NasWriteProbeFailed", got["rules"])
        self.assertFalse(any("nas-storage-health" in d for d in got["dirs"]))

    def test_a_directory_outside_its_share_is_refused(self):
        extra = '{ services.nas-storage-health.shares.Bryan.directory = lib.mkForce "/Volumes/Other/x"; }'
        result = subprocess.run(["nix", "eval", "--impure", "--expr",
                                 studio_expr("config.system.build.toplevel.drvPath", extra)],
                                cwd=REPO, text=True, capture_output=True)
        self.assertNotEqual(0, result.returncode)
        self.assertIn("plain path inside", result.stderr)

    def test_a_host_without_monitoring_gets_just_the_probe(self):
        expr = f"""
          let
            flake = builtins.getFlake "path:{REPO}";
            system = flake.inputs.nix-darwin.lib.darwinSystem {{
              modules = [
                flake.modules.darwin.nas-storage-health
                {{
                  nixpkgs.hostPlatform = "aarch64-darwin";
                  system.primaryUser = "someone";
                  system.stateVersion = 4;
                  services.nas-storage-health = {{
                    enable = true;
                    shares.s = {{ mountPoint = "/Volumes/s"; directory = "/Volumes/s"; }};
                  }};
                }}
              ];
            }};
          in system.config.launchd.user.agents.nas-write-probe.serviceConfig.Label
        """
        self.assertEqual('"org.nixos.nas-write-probe"', nix("eval", "--impure", "--expr", expr).strip())


class Rules(unittest.TestCase):
    def series(self, metric, values, **labels):
        all_labels = ",".join(f'{k}="{v}"' for k, v in labels.items())
        return {"series": f"{metric}{{{all_labels}}}", "values": values}

    def btrfs(self, values, device, type_, uuid=POOL):
        return self.series("node_btrfs_device_errors_total", values, **NODE, uuid=uuid,
                           btrfs_dev_uuid=f"dev-{device}", device=device, type=type_)

    def promtool_test(self, tests):
        spec = {"rule_files": [rule_file()], "evaluation_interval": "1m", "tests": tests}
        with tempfile.TemporaryDirectory() as tmp:
            test_file = Path(tmp) / "nas-rule-test.json"
            test_file.write_text(json.dumps(spec))
            result = subprocess.run([realised()["promtool"], "test", "rules", str(test_file)],
                                    text=True, capture_output=True)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)

    def case(self, inputs, alertname, *expectations):
        return {"interval": "1m", "input_series": inputs, "alert_rule_test": [
            {"eval_time": t, "alertname": alertname, "exp_alerts": exp} for t, exp in expectations]}

    def alert(self, name, labels, value=None):
        """The alert promtool should see: labels plus annotations with the templates filled."""
        fills = {f"{{{{ $labels.{k} }}}}": v for k, v in labels.items()}
        if value is not None:
            fills['{{ $value | printf "%.0f" }}'] = value
        annotations = {}
        for key, text in rules()[name]["annotations"].items():
            for template, filled in fills.items():
                text = text.replace(template, filled)
            self.assertNotIn("{{", text, f"{name} {key} left a template unfilled")
            annotations[key] = text
        return {"exp_labels": {**labels, "severity": rules()[name]["labels"]["severity"]},
                "exp_annotations": annotations}

    def test_every_rule_is_generated_and_promtool_accepts_them(self):
        self.assertTrue({"NasBtrfsDeviceErrors", "NasBtrfsMetricsMissing", "NasWriteProbeFailed",
                         "NasShareNotMounted", "NasWriteProbeSilent", "ServiceDown"} <= set(rules()))
        result = subprocess.run([realised()["promtool"], "check", "rules", rule_file()],
                                text=True, capture_output=True)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)

    def test_no_rule_trusts_the_read_only_flag(self):
        for name, rule in rules().items():
            if name.startswith("Nas"):
                self.assertNotIn("readonly", rule["expr"], name)

    def test_a_pool_dropping_off_fires_while_the_filesystem_says_writable(self):
        # Both devices of a pool report new read errors, other counters sit
        # flat, and the filesystem's read-only flag stays 0.
        inputs = [
            self.btrfs("0x30 1 300 3000x34", "sda1", "read"),
            self.btrfs("100x30 101 800 3400x34", "sdb1", "read"),
            self.btrfs("5000x66", "sdb1", "write"),
            self.btrfs("20000x66", "sdb1", "corruption"),
            self.btrfs("0x66", "loop1", "write", uuid=DOCKER),
            self.series("node_filesystem_readonly", "0x66", **NODE, device="/dev/sda1",
                        fstype="btrfs", mountpoint="/mnt/pool"),
        ]
        sda1 = {**NODE, "uuid": POOL, "btrfs_dev_uuid": "dev-sda1", "device": "sda1", "type": "read"}
        sdb1 = {**sda1, "btrfs_dev_uuid": "dev-sdb1", "device": "sdb1"}
        self.promtool_test([self.case(
            inputs, "NasBtrfsDeviceErrors",
            ("30m", []),
            # The first error is counted at the next evaluation; by 34m the
            # whole jump is in the window (increase() extrapolates toward the
            # window's edges, so a value can differ a little from the raw jump).
            ("31m", [self.alert("NasBtrfsDeviceErrors", sda1, "1"),
                     self.alert("NasBtrfsDeviceErrors", sdb1, "1")]),
            ("34m", [self.alert("NasBtrfsDeviceErrors", sda1, "3000"),
                     self.alert("NasBtrfsDeviceErrors", sdb1, "3400")]),
            # Quiet again once the counters stop moving for the window.
            ("65m", []),
        )])

    def test_devices_firing_together_share_a_summary(self):
        # Notifications group by alertname and instance; the Matrix message
        # shows only annotations common to the group.
        a = self.alert("NasBtrfsDeviceErrors", {**NODE, "uuid": POOL, "btrfs_dev_uuid": "dev-sda1",
                                                "device": "sda1", "type": "read"}, "1")
        b = self.alert("NasBtrfsDeviceErrors", {**NODE, "uuid": POOL, "btrfs_dev_uuid": "dev-sdb1",
                                                "device": "sdb1", "type": "write"}, "1")
        self.assertEqual(a["exp_annotations"]["summary"], b["exp_annotations"]["summary"])

    def test_a_filesystem_relabelled_but_present_is_not_missing(self):
        self.promtool_test([self.case(
            [self.series("node_btrfs_info", "1x10 _x50", **NODE, uuid=POOL, label="old"),
             self.series("node_btrfs_info", "_x10 1x50", **NODE, uuid=POOL, label="new"),
             self.series("up", "1x60", **NODE)],
            "NasBtrfsMetricsMissing", ("40m", []))])

    def test_old_errors_alone_never_fire(self):
        self.promtool_test([self.case(
            [self.btrfs("5000x120", "sdb1", "write"), self.btrfs("20000x120", "sdb1", "read")],
            "NasBtrfsDeviceErrors", ("5m", []), ("60m", []), ("119m", []))])

    def test_a_single_new_write_error_fires(self):
        dev = {**NODE, "uuid": POOL, "btrfs_dev_uuid": "dev-sda1", "device": "sda1", "type": "write"}
        self.promtool_test([self.case(
            [self.btrfs("0x10 7x30", "sda1", "write")], "NasBtrfsDeviceErrors",
            ("9m", []), ("11m", [self.alert("NasBtrfsDeviceErrors", dev, "7")]))])

    def test_missing_btrfs_metrics_warn(self):
        self.promtool_test([
            # The collector reports failure.
            self.case([self.series("node_scrape_collector_success", "1x5 0x30", **NODE, collector="btrfs")],
                      "NasBtrfsMetricsMissing",
                      ("10m", []),
                      ("25m", [self.alert("NasBtrfsMetricsMissing", {**NODE, "collector": "btrfs"})])),
            # A pool seen earlier is gone while the exporter is up.
            self.case([self.series("node_btrfs_info", "1x10", **NODE, uuid=POOL),
                       self.series("up", "1x60", **NODE)],
                      "NasBtrfsMetricsMissing",
                      ("5m", []),
                      ("40m", [self.alert("NasBtrfsMetricsMissing", {**NODE, "uuid": POOL})])),
            # The exporter itself down is PrometheusTargetDown's, not this.
            self.case([self.series("node_btrfs_info", "1x10", **NODE, uuid=POOL),
                       self.series("up", "1x10 0x50", **NODE)],
                      "NasBtrfsMetricsMissing", ("40m", [])),
        ])

    def test_a_failed_write_probe_is_critical_after_two_runs(self):
        share = {"instance": "localhost:9100", "job": "node_exporter", "share": "Bryan"}
        self.promtool_test([
            self.case([self.series("nas_write_probe_success", "1x10 0x40", **share),
                       self.series("nas_write_probe_mounted", "1x50", **share)],
                      "NasWriteProbeFailed",
                      ("20m", []),
                      ("27m", [self.alert("NasWriteProbeFailed", share)])),
            # One bad run that clears is not an alert.
            self.case([self.series("nas_write_probe_success", "1x10 0x9 1x30", **share),
                       self.series("nas_write_probe_mounted", "1x50", **share)],
                      "NasWriteProbeFailed", ("15m", []), ("30m", [])),
            # Unmounted is its own, lower-severity alert, not a write failure.
            self.case([self.series("nas_write_probe_success", "0x40", **share),
                       self.series("nas_write_probe_mounted", "0x40", **share)],
                      "NasWriteProbeFailed", ("30m", [])),
        ])
        self.assertEqual("critical", rules()["NasWriteProbeFailed"]["labels"]["severity"])

    def test_an_unmounted_share_warns(self):
        share = {"instance": "localhost:9100", "job": "node_exporter", "share": "Bryan"}
        self.promtool_test([self.case(
            [self.series("nas_write_probe_mounted", "1x10 0x40", **share)], "NasShareNotMounted",
            ("20m", []), ("27m", [self.alert("NasShareNotMounted", share)]))])

    def test_a_silent_probe_warns(self):
        inst = {"instance": "localhost:9100", "job": "node_exporter"}
        self.promtool_test([
            self.case([self.series("nas_write_probe_last_run_timestamp_seconds", "0+600x3", **inst)],
                      "NasWriteProbeSilent", ("5m", []), ("90m", [self.alert("NasWriteProbeSilent", {})])),
            self.case([self.series("nas_write_probe_last_run_timestamp_seconds", "0x120", **inst)],
                      "NasWriteProbeSilent", ("20m", []),
                      ("50m", [self.alert("NasWriteProbeSilent", inst)])),
        ])


if __name__ == "__main__":
    unittest.main()
