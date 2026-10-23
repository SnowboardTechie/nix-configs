#!/usr/bin/env python3
"""The approved primary update hold must survive nix-darwin activation."""
import json
import subprocess
import unittest
from functools import cache
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
STUDIO = "darwinConfigurations.studio.config"


@cache
def eval_json(option):
    result = subprocess.run(
        ["nix", "eval", "--json", f".#{STUDIO}.{option}"],
        cwd=REPO,
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


class HermesUpdateHoldTests(unittest.TestCase):
    def test_primary_updater_is_not_declared_during_hold(self):
        agents = eval_json("launchd.user.agents")
        self.assertNotIn("hermes-updater", list(agents))

    def test_hold_leaves_primary_services_enabled(self):
        self.assertTrue(eval_json("services.hermes.gateway.enable"))
        self.assertTrue(eval_json("services.hermes.dashboard.enable"))

    def test_hold_preserves_headless_weekly_update(self):
        updater = eval_json("launchd.daemons.hermes-traci-updater.serviceConfig")
        self.assertEqual(updater["Label"], "ai.hermes.update-traci")
        calendar = {key: value for key, value in updater["StartCalendarInterval"].items()
                    if value is not None}
        self.assertEqual(calendar, {"Weekday": 0, "Hour": 5, "Minute": 0})


if __name__ == "__main__":
    unittest.main()
