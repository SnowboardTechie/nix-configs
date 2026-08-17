#!/usr/bin/env python3
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).with_name("check-github-status.py")
SPEC = importlib.util.spec_from_file_location("github_status_watch", MODULE_PATH)
assert SPEC is not None
watch = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = watch
SPEC.loader.exec_module(watch)


def summary(*, indicator="major", description="Partial System Outage", incidents=None, components=None):
    return {
        "status": {"indicator": indicator, "description": description},
        "incidents": incidents or [],
        "components": components or [],
        "page": {"updated_at": "2026-08-17T15:00:00Z"},
    }


class Harness:
    def __init__(self, root, payload):
        self.state_path = Path(root) / "state" / "github.json"
        self.payload = payload
        self.fail = False
        self.calls = []

    def http_get(self, url, timeout=20):
        self.calls.append((url, timeout))
        if self.fail:
            raise OSError("network down at a changing time")
        return json.dumps(self.payload).encode()

    def run(self):
        return watch.run(
            http_get=self.http_get,
            state_path=self.state_path,
            sleep_fn=lambda _: None,
        )


INCIDENT = {
    "id": "incident-1",
    "name": "Incident with GitHub.com",
    "status": "investigating",
    "impact": "critical",
    "updated_at": "2026-08-17T14:54:14Z",
    "incident_updates": [
        {
            "body": "Pull Requests is experiencing degraded availability.",
            "updated_at": "2026-08-17T14:54:14Z",
        }
    ],
}
COMPONENTS = [
    {"name": "Pull Requests", "status": "major_outage", "updated_at": "later"},
    {"name": "API Requests", "status": "degraded_performance", "updated_at": "earlier"},
    {"name": "Git Operations", "status": "operational", "updated_at": "now"},
]


class GithubStatusWatchTests(unittest.TestCase):
    def test_snapshot_is_stable_sorted_and_omits_timestamps(self):
        with tempfile.TemporaryDirectory() as root:
            harness = Harness(
                root,
                summary(incidents=[INCIDENT], components=list(reversed(COMPONENTS))),
            )
            first = watch.render(harness.run())
            second = watch.render(harness.run())
            self.assertEqual(first, second)
            payload = json.loads(first)
            self.assertEqual("major", payload["overall"]["indicator"])
            self.assertEqual(
                ["API Requests", "Pull Requests"],
                [item["name"] for item in payload["affected_components"]],
            )
            self.assertEqual("investigating", payload["incidents"][0]["status"])
            self.assertNotIn("updated_at", first)
            self.assertNotIn("2026-08-17", first)

    def test_component_and_incident_recovery_change_the_snapshot(self):
        with tempfile.TemporaryDirectory() as root:
            harness = Harness(root, summary(incidents=[INCIDENT], components=COMPONENTS))
            before = watch.render(harness.run())
            harness.payload = summary(
                indicator="minor",
                description="Minor Service Outage",
                incidents=[
                    {
                        **INCIDENT,
                        "status": "monitoring",
                        "incident_updates": [
                            {"body": "We are seeing recovery and monitoring results."}
                        ],
                    }
                ],
                components=[
                    {"name": "Pull Requests", "status": "degraded_performance"},
                    {"name": "API Requests", "status": "operational"},
                ],
            )
            after = watch.render(harness.run())
            self.assertNotEqual(before, after)
            payload = json.loads(after)
            self.assertEqual("minor", payload["overall"]["indicator"])
            self.assertEqual("monitoring", payload["incidents"][0]["status"])
            self.assertEqual(
                [{"name": "Pull Requests", "status": "degraded_performance"}],
                payload["affected_components"],
            )

    def test_full_recovery_has_no_active_incidents_or_components(self):
        with tempfile.TemporaryDirectory() as root:
            harness = Harness(
                root,
                summary(indicator="none", description="All Systems Operational"),
            )
            payload = harness.run()
            self.assertEqual([], payload["incidents"])
            self.assertEqual([], payload["affected_components"])
            self.assertEqual("none", payload["overall"]["indicator"])

    def test_source_failure_keeps_last_good_status_and_is_stable(self):
        with tempfile.TemporaryDirectory() as root:
            harness = Harness(root, summary(incidents=[INCIDENT], components=COMPONENTS))
            good = harness.run()
            harness.fail = True
            first_failure = watch.render(harness.run())
            second_failure = watch.render(harness.run())
            self.assertEqual(first_failure, second_failure)
            payload = json.loads(first_failure)
            self.assertEqual(good["overall"], payload["overall"])
            self.assertEqual(good["incidents"], payload["incidents"])
            self.assertEqual("unavailable", payload["source_health"])
            self.assertEqual("OSError", payload["source_error"])
            self.assertNotIn("network down", first_failure)

    def test_initial_malformed_source_is_explicitly_unavailable(self):
        with tempfile.TemporaryDirectory() as root:
            harness = Harness(root, {"status": {}})
            payload = harness.run()
            self.assertEqual("unavailable", payload["source_health"])
            self.assertEqual("RuntimeError", payload["source_error"])
            self.assertNotIn("overall", payload)

    def test_last_good_state_is_atomic_and_private(self):
        with tempfile.TemporaryDirectory() as root:
            harness = Harness(root, summary(incidents=[INCIDENT], components=COMPONENTS))
            harness.run()
            self.assertEqual([], list(harness.state_path.parent.glob("*.tmp")))
            self.assertEqual(0o600, harness.state_path.stat().st_mode & 0o777)


if __name__ == "__main__":
    unittest.main()
