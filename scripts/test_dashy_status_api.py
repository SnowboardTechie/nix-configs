#!/usr/bin/env python3
"""Tests for Dashy's read-only view of the Studio watchdog state."""

from __future__ import annotations

import http.client
import importlib.util
import json
import os
import tempfile
import threading
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

    def status_for(self, record: dict) -> tuple[int, dict]:
        with tempfile.TemporaryDirectory() as temporary:
            path = self.write_state(Path(temporary), {"Grafana": record})
            return self.module.service_status(path, "Grafana", now=time.time())

    def test_failures_below_alert_threshold_stay_green_with_count(self) -> None:
        for failures in (1, 2):
            with self.subTest(failures=failures):
                status, payload = self.status_for(
                    {"failures": failures, "alerted": False, "last_error": "HTTP 500"}
                )
                self.assertEqual(status, 200)
                self.assertIs(payload["successStatus"], True)
                self.assertEqual(payload["statusCode"], 200)
                self.assertIn(str(failures), payload["message"])
                self.assertIn("failed", payload["message"])
                self.assertNotIn("HTTP 500", json.dumps(payload))

    def test_alerted_service_is_red_without_error_text(self) -> None:
        status, payload = self.status_for(
            {"failures": 3, "alerted": True, "last_error": "HTTP 500"}
        )
        self.assertEqual(status, 200)
        self.assertIs(payload["successStatus"], False)
        self.assertEqual(payload["statusCode"], 503)
        self.assertEqual(payload["statusText"], "Unhealthy")
        self.assertNotIn("HTTP 500", json.dumps(payload))

    def test_only_json_true_counts_as_alerted(self) -> None:
        for alerted in ("yes", 1, "true", [True]):
            with self.subTest(alerted=alerted):
                status, payload = self.status_for(
                    {"failures": 3, "alerted": alerted, "last_error": "HTTP 500"}
                )
                self.assertEqual(status, 200)
                self.assertIs(payload["successStatus"], True)
                self.assertEqual(payload["statusCode"], 200)
                self.assertNotIn("HTTP 500", json.dumps(payload))

    def test_malformed_failure_count_stays_green_with_generic_message(self) -> None:
        for record in (
            {"alerted": False, "last_error": "HTTP 500"},
            {"failures": "two", "alerted": False, "last_error": "HTTP 500"},
            {"failures": None, "alerted": False, "last_error": "HTTP 500"},
            {"failures": 2.5, "alerted": False, "last_error": "HTTP 500"},
            {"failures": True, "alerted": False, "last_error": "HTTP 500"},
        ):
            with self.subTest(record=record):
                status, payload = self.status_for(record)
                self.assertEqual(status, 200)
                self.assertIs(payload["successStatus"], True)
                self.assertEqual(payload["statusCode"], 200)
                self.assertEqual(payload["message"], "Latest check failed")
                self.assertNotIn("HTTP 500", json.dumps(payload))

    def test_non_dict_record_is_invalid(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = self.write_state(Path(temporary), {"Grafana": "broken"})
            status, payload = self.module.service_status(path, "Grafana", now=time.time())
        self.assertEqual(status, 200)
        self.assertIs(payload["successStatus"], False)
        self.assertEqual(payload["statusText"], "Unknown")

    def test_unreadable_state_is_watchdog_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "missing.json"
            status, payload = self.module.service_status(path, "Grafana", now=time.time())
        self.assertEqual(status, 503)
        self.assertEqual(payload["statusText"], "Watchdog unavailable")

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


class StatusServerBurstTest(unittest.TestCase):
    """Dashy checks every tile at once on page load; none may be dropped."""

    BURST = 64
    PATH = "/status-check/?&url=watchdog:///Grafana"

    @classmethod
    def setUpClass(cls) -> None:
        cls.module = load_module()

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        state = Path(temporary.name) / "watchdog.json"
        state.write_text(
            json.dumps({"services": {"Grafana": {"failures": 0, "alerted": False, "last_error": None}}}),
            encoding="utf-8",
        )
        self.threads_before = set(threading.enumerate())
        self.server = self.module.build_server("127.0.0.1", 0, state)
        self.serve_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.serve_thread.start()
        self.addCleanup(self.stop_server)

    def stop_server(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.serve_thread.join(timeout=5)
        deadline = time.monotonic() + 5
        while set(threading.enumerate()) - self.threads_before and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(set(threading.enumerate()) - self.threads_before, set())
        self.assertEqual(self.server.socket.fileno(), -1)

    def fetch(self, port: int, start: threading.Barrier) -> tuple[int, dict] | BaseException:
        start.wait(timeout=5)
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            connection.request("GET", self.PATH, headers={"Connection": "close"})
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        except (OSError, http.client.HTTPException, ValueError) as error:
            return error
        finally:
            connection.close()

    def test_burst_of_fresh_connections_all_get_answers(self) -> None:
        port = self.server.server_address[1]
        start = threading.Barrier(self.BURST)
        results: list[object] = [None] * self.BURST

        def run(index: int) -> None:
            results[index] = self.fetch(port, start)

        clients = [threading.Thread(target=run, args=(i,)) for i in range(self.BURST)]
        for client in clients:
            client.start()
        for client in clients:
            client.join(timeout=10)

        failures = [r for r in results if not (isinstance(r, tuple) and r[0] == 200)]
        self.assertEqual(failures, [], f"{len(failures)} of {self.BURST} requests failed")
        for _, payload in results:  # type: ignore[misc]
            self.assertIs(payload["successStatus"], True)
            self.assertEqual(payload["statusText"], "Healthy")

    def test_listen_backlog_holds_a_dashy_burst(self) -> None:
        # Interpreters disagree on the default (5 before Python 3.15, 128 after),
        # so pin it: the burst test alone passes on 3.15 whatever the adapter sets.
        self.assertGreaterEqual(self.server.request_queue_size, self.BURST)
        self.assertIs(self.server.daemon_threads, True)


if __name__ == "__main__":
    unittest.main()
