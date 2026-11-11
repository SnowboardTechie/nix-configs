#!/usr/bin/env python3
"""The Claude usage sampler's Prometheus scrape and OpenCode Go's allowance alerts.

durable-pi-workspace#399, P399.3. The sampler computes each OpenCode Go window's
share from Pi's session records (workspaces/sampler/src/opencode-go.ts) and
exports it on its loopback /metrics; Prometheus scrapes that and these rules
alert at 80% and 95% of any window, and when the records can't all be read or a
model can't be weighed.

The module is evaluated through a `path:` flake reference so uncommitted
changes are evaluated as on disk. The generated rules are checked with the
Studio's own promtool, including `promtool test rules` on series shaped like
the sampler's /metrics. Nothing is activated or started.
"""
import json
import subprocess
import tempfile
import unittest
from functools import cache
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
TARGET = {"instance": "127.0.0.1:8791", "job": "pi-durable-sampler"}

# The Go part of the sampler's /metrics as it writes it (sampler web.ts and
# opencode-go.ts, durable-pi-workspace e108f3b).
GO_METRICS = """\
# HELP pi_durable_sampler_opencode_go_share The OpenCode Go window's estimated use as a fraction of its limit, computed from Pi's session records (and Hermes's while it runs).
# TYPE pi_durable_sampler_opencode_go_share gauge
pi_durable_sampler_opencode_go_share{window="5-hour"} 0
pi_durable_sampler_opencode_go_share{window="weekly"} 0.34897
pi_durable_sampler_opencode_go_share{window="monthly"} 0.174485
# HELP pi_durable_sampler_opencode_go_used_usd The OpenCode Go window's estimated use in dollars of the allowance, from the same records.
# TYPE pi_durable_sampler_opencode_go_used_usd gauge
pi_durable_sampler_opencode_go_used_usd{window="5-hour"} 0
pi_durable_sampler_opencode_go_used_usd{window="weekly"} 10.4691
pi_durable_sampler_opencode_go_used_usd{window="monthly"} 10.4691
# HELP pi_durable_sampler_opencode_go_readable 1 when at least one configured OpenCode Go source could be read, else 0.
# TYPE pi_durable_sampler_opencode_go_readable gauge
pi_durable_sampler_opencode_go_readable 1
# HELP pi_durable_sampler_opencode_go_unreadable_files Session files, directories under a configured one, or Hermes databases that couldn't be read.
# TYPE pi_durable_sampler_opencode_go_unreadable_files gauge
pi_durable_sampler_opencode_go_unreadable_files 0
# HELP pi_durable_sampler_opencode_go_bad_lines Complete session-file lines that aren't JSON (Pi skips them too).
# TYPE pi_durable_sampler_opencode_go_bad_lines gauge
pi_durable_sampler_opencode_go_bad_lines 0
# HELP pi_durable_sampler_opencode_go_missing_dirs Configured session directories missing or unreadable.
# TYPE pi_durable_sampler_opencode_go_missing_dirs gauge
pi_durable_sampler_opencode_go_missing_dirs 0
# HELP pi_durable_sampler_opencode_go_unknown_models Distinct models counted at the dearest weight: not in the weight table, or a paid model Pi recorded no cost for.
# TYPE pi_durable_sampler_opencode_go_unknown_models gauge
pi_durable_sampler_opencode_go_unknown_models 0
# HELP pi_durable_sampler_opencode_go_unattributed Summaries made by an extension, whose model is unknown: not counted.
# TYPE pi_durable_sampler_opencode_go_unattributed gauge
pi_durable_sampler_opencode_go_unattributed 0
"""

# Each rule's severity, as the plan sets it (mechanism 6): only 95% is critical.
SEVERITY = {"OpenCodeGoAllowanceHigh": "warning", "OpenCodeGoAllowanceCritical": "critical",
            "OpenCodeGoUsageUnreadable": "warning", "OpenCodeGoUnknownModel": "warning"}
ALERTS = set(SEVERITY)


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
      in {expr}
    """


@cache
def realised(extra="{ }"):
    out = nix("build", "--no-link", "--print-out-paths", "--impure", "--expr", studio_expr("""
      pkgs.writeText "opencode-go-alert-paths.json" (builtins.toJSON {
        prometheusConfig = prometheusConfig;
        promtool = "${pkgs.prometheus.cli}/bin/promtool";
        samplerPort = config.services.pi-durable.sampler.port;
      })
    """, extra))
    return json.loads(Path(out.strip().splitlines()[-1]).read_text())


@cache
def prometheus(extra="{ }"):
    return json.loads(Path(realised(extra)["prometheusConfig"]).read_text())


@cache
def rules(extra="{ }"):
    rule_file = prometheus(extra)["rule_files"][0]
    return rule_file, {r["alert"]: r for g in json.loads(Path(rule_file).read_text())["groups"]
                       for r in g["rules"]}


class Scrape(unittest.TestCase):
    def test_prometheus_scrapes_the_samplers_loopback_metrics(self):
        jobs = {j["job_name"]: j for j in prometheus()["scrape_configs"]}
        self.assertIn("pi-durable-sampler", jobs)
        port = realised()["samplerPort"]
        self.assertEqual(8791, port, "Studio's sampler page port")
        self.assertEqual({"job_name": "pi-durable-sampler", "metrics_path": "/metrics",
                          "static_configs": [{"targets": [f"127.0.0.1:{port}"]}]}, jobs["pi-durable-sampler"])

    def test_the_target_follows_the_samplers_port(self):
        jobs = {j["job_name"]: j for j in prometheus("{ services.pi-durable.sampler.port = lib.mkForce 9801; }")["scrape_configs"]}
        self.assertEqual(["127.0.0.1:9801"], jobs["pi-durable-sampler"]["static_configs"][0]["targets"])

    def test_without_the_sampler_neither_scrape_nor_rules(self):
        extra = "{ services.pi-durable.sampler.enable = lib.mkForce false; }"
        self.assertNotIn("pi-durable-sampler", {j["job_name"] for j in prometheus(extra)["scrape_configs"]})
        self.assertFalse(ALERTS & set(rules(extra)[1]))

    def test_the_metrics_text_is_what_promtool_reads(self):
        result = subprocess.run([realised()["promtool"], "check", "metrics"], input=GO_METRICS,
                                text=True, capture_output=True)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)


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
        text = ",".join(f'{k}="{v}"' for k, v in {**TARGET, **labels}.items())
        return {"series": f"{name}{{{text}}}", "values": values}

    def firing(self, name, value="", **labels):
        rule = rules()[1][name]
        annotations = {k: v.replace("{{ $labels.window }}", labels.get("window", ""))
                       .replace("{{ $value | humanizePercentage }}", value)
                       .replace("{{ $value }}", value) for k, v in rule["annotations"].items()}
        return [{"exp_labels": {**TARGET, **labels, "severity": SEVERITY[name]},
                 "exp_annotations": annotations}]

    def healthy(self):
        return [self.series("pi_durable_sampler_opencode_go_readable", "1x120"),
                self.series("pi_durable_sampler_opencode_go_missing_dirs", "0x120"),
                self.series("pi_durable_sampler_opencode_go_unreadable_files", "0x120"),
                self.series("pi_durable_sampler_opencode_go_bad_lines", "0x120"),
                self.series("pi_durable_sampler_opencode_go_unknown_models", "0x120")]

    def test_rules_are_generated_and_accepted(self):
        rule_file, by_name = rules()
        self.assertTrue(ALERTS <= set(by_name))
        result = subprocess.run([realised()["promtool"], "check", "rules", rule_file],
                                text=True, capture_output=True)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        for name in ALERTS:
            self.assertIn("console", by_name[name]["annotations"]["description"], f"{name} says the console is authoritative")
            self.assertIn("100%", by_name[name]["annotations"]["description"], f"{name} says what 100% means")

    def test_a_healthy_sampler_under_80_percent_raises_nothing(self):
        inputs = self.healthy() + [self.series("pi_durable_sampler_opencode_go_share", "0.79x120", window="weekly")]
        for alert in ALERTS:
            self.promtool_test(inputs, alert, ("60m", []))

    def test_80_percent_warns_after_5_minutes_per_window(self):
        inputs = self.healthy() + [self.series("pi_durable_sampler_opencode_go_share", "0.5x10 0.8x30", window="weekly"),
                                   self.series("pi_durable_sampler_opencode_go_share", "0.1x40", window="5-hour")]
        self.promtool_test(inputs, "OpenCodeGoAllowanceHigh",
                           ("14m", []),
                           ("16m", self.firing("OpenCodeGoAllowanceHigh", value="80%", window="weekly")))
        self.promtool_test(inputs, "OpenCodeGoAllowanceCritical", ("30m", []))

    def test_94_percent_is_not_critical(self):
        inputs = self.healthy() + [self.series("pi_durable_sampler_opencode_go_share", "0.94x30", window="5-hour")]
        self.promtool_test(inputs, "OpenCodeGoAllowanceCritical", ("20m", []))

    def test_95_percent_is_critical_and_still_warns(self):
        inputs = self.healthy() + [self.series("pi_durable_sampler_opencode_go_share", "0.95x30", window="5-hour")]
        self.promtool_test(inputs, "OpenCodeGoAllowanceCritical",
                           ("4m", []),
                           ("6m", self.firing("OpenCodeGoAllowanceCritical", value="95%", window="5-hour")))
        self.promtool_test(inputs, "OpenCodeGoAllowanceHigh",
                           ("6m", self.firing("OpenCodeGoAllowanceHigh", value="95%", window="5-hour")))

    def test_a_window_that_drops_back_resolves(self):
        inputs = self.healthy() + [self.series("pi_durable_sampler_opencode_go_share", "0.9x20 0.5x20", window="monthly")]
        self.promtool_test(inputs, "OpenCodeGoAllowanceHigh",
                           ("10m", self.firing("OpenCodeGoAllowanceHigh", value="90%", window="monthly")),
                           ("25m", []))

    def test_unreadable_sources_warn_after_30_minutes_each_way(self):
        for name, values in [("pi_durable_sampler_opencode_go_readable", "0x60"),
                             ("pi_durable_sampler_opencode_go_missing_dirs", "1x60"),
                             ("pi_durable_sampler_opencode_go_unreadable_files", "2x60")]:
            with self.subTest(name):
                inputs = [s for s in self.healthy() if name not in s["series"]] + [self.series(name, values)]
                self.promtool_test(inputs, "OpenCodeGoUsageUnreadable",
                                   ("29m", []),
                                   ("31m", self.firing("OpenCodeGoUsageUnreadable")))

    def test_unreadable_needs_only_its_own_series(self):
        # The old reader (before durable-pi-workspace e108f3b) exports readable alone.
        inputs = [self.series("pi_durable_sampler_opencode_go_readable", "0x60")]
        self.promtool_test(inputs, "OpenCodeGoUsageUnreadable", ("40m", self.firing("OpenCodeGoUsageUnreadable")))
        inputs = [self.series("pi_durable_sampler_opencode_go_missing_dirs", "1x60")]
        self.promtool_test(inputs, "OpenCodeGoUsageUnreadable", ("40m", self.firing("OpenCodeGoUsageUnreadable")))

    def test_bad_lines_alone_raise_nothing(self):
        inputs = [s for s in self.healthy() if "bad_lines" not in s["series"]] + [
            self.series("pi_durable_sampler_opencode_go_bad_lines", "3x120")]
        self.promtool_test(inputs, "OpenCodeGoUsageUnreadable", ("90m", []))

    def test_an_unknown_model_warns_after_an_hour(self):
        inputs = [s for s in self.healthy() if "unknown_models" not in s["series"]] + [
            self.series("pi_durable_sampler_opencode_go_unknown_models", "1x120")]
        self.promtool_test(inputs, "OpenCodeGoUnknownModel",
                           ("59m", []),
                           ("61m", self.firing("OpenCodeGoUnknownModel", value="1")))


if __name__ == "__main__":
    unittest.main()
