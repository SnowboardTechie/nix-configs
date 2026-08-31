#!/usr/bin/env python3
"""Tests for Dashy's read-only view of the Studio watchdog state."""

from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "modules" / "services" / "dashy-status-api.py"


def load_module():
    spec = importlib.util.spec_from_file_location("dashy_status_api", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ServiceStatusTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.module = load_module()

    def write_state(self, root: Path, services: dict) -> Path:
        path = root / "watchdog.json"
        path.write_text(json.dumps({"services": services}), encoding="utf-8")
        return path

    def test_healthy_service_returns_dashy_success_shape(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = self.write_state(
                Path(temporary),
                {"Grafana": {"failures": 0, "alerted": False, "last_error": None}},
            )
            status, payload = self.module.service_status(path, "Grafana", now=time.time())

        self.assertEqual(status, 200)
        self.assertIs(payload["successStatus"], True)
        self.assertEqual(payload["statusCode"], 200)
        self.assertNotIn("last_error", payload)

    def test_current_failure_is_visible_before_notification_threshold(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = self.write_state(
                Path(temporary),
                {"Grafana": {"failures": 1, "alerted": False, "last_error": "HTTP 500"}},
            )
            status, payload = self.module.service_status(path, "Grafana", now=time.time())

        self.assertEqual(status, 200)
        self.assertIs(payload["successStatus"], False)
        self.assertEqual(payload["statusCode"], 503)
        self.assertNotIn("HTTP 500", json.dumps(payload))

    def test_unknown_service_is_rejected_without_network_access(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = self.write_state(Path(temporary), {})
            status, payload = self.module.service_status(
                path, "http://169.254.169.254/latest/meta-data", now=time.time()
            )

        self.assertEqual(status, 404)
        self.assertIs(payload["successStatus"], False)

    def test_stale_watchdog_state_is_unhealthy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = self.write_state(
                Path(temporary),
                {"Grafana": {"failures": 0, "alerted": False, "last_error": None}},
            )
            old = time.time() - self.module.MAX_STATE_AGE_SECONDS - 1
            os.utime(path, (old, old))
            status, payload = self.module.service_status(path, "Grafana", now=time.time())

        self.assertEqual(status, 200)
        self.assertIs(payload["successStatus"], False)
        self.assertEqual(payload["statusText"], "Watchdog stale")

    def test_watchdog_url_parser_accepts_only_private_service_names(self) -> None:
        self.assertEqual(
            self.module.service_name_from_target("watchdog:///Hindsight%20API"),
            "Hindsight API",
        )
        for target in (
            "http://127.0.0.1:8888/health",
            "https://example.com",
            "watchdog://",
        ):
            with self.subTest(target=target):
                self.assertIsNone(self.module.service_name_from_target(target))

    def test_status_route_accepts_dashy_trailing_slash(self) -> None:
        self.assertTrue(self.module.is_status_path("/status-check"))
        self.assertTrue(self.module.is_status_path("/status-check/"))
        self.assertFalse(self.module.is_status_path("/status-check/other"))


if __name__ == "__main__":
    unittest.main()
