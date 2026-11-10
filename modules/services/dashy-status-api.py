#!/usr/bin/env python3
"""Expose allowlisted Prometheus probe results in Dashy's status-check format.

Dashy asks for ``probe:///<name>``; the answer comes from blackbox_exporter's
``probe_success{job="blackbox",probe="<name>"}`` in Prometheus. A service turns
red only when its probe has failed for the whole of the last five minutes (as
long as ServiceDown waits). A latest probe that failed inside that window stays
green, with a message saying so. A probe with no series is red at once: that
is how Prometheus shows a probe it could not scrape (blackbox_exporter down,
say), and ServiceDown does not cover it. Only the names given with ``--probe`` are
answered, so the query is always one of a fixed set; nothing else of
Prometheus's is exposed.
"""

from __future__ import annotations

import argparse
import json
import re
import urllib.error
import urllib.request
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlencode, urlparse

MAX_RESPONSE_BYTES = 16 * 1024
QUERY_TIMEOUT_SECONDS = 5
MAX_QUERY_BYTES = 256 * 1024
# The names services.monitoring.blackbox.contentProbes accepts, so a name can
# sit inside a PromQL string without escaping.
PROBE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9 -]*")
# ServiceDown's `for`.
RED_AFTER = "5m"


class PrometheusUnavailable(Exception):
    """Prometheus did not answer a query with a usable result."""


def probe_name_from_target(target: str) -> str | None:
    parsed = urlparse(target)
    if parsed.scheme != "probe" or parsed.netloc or parsed.query or parsed.fragment:
        return None
    name = unquote(parsed.path.lstrip("/"))
    return name if PROBE_NAME.fullmatch(name) else None


def is_status_path(path: str) -> bool:
    return path.rstrip("/") == "/status-check"


def _payload(success: bool, status_code: int, status_text: str, message: str) -> dict:
    return {
        "successStatus": success,
        "statusCode": status_code,
        "statusText": status_text,
        "timeTaken": 0,
        "message": message,
    }


def query(prometheus_url: str, expression: str) -> list[float]:
    """Each sample's value of an instant query; [] when it has no series."""
    url = f"{prometheus_url.rstrip('/')}/api/v1/query?{urlencode({'query': expression})}"
    try:
        with urllib.request.urlopen(url, timeout=QUERY_TIMEOUT_SECONDS) as response:
            body = response.read(MAX_QUERY_BYTES + 1)
        if len(body) > MAX_QUERY_BYTES:
            raise PrometheusUnavailable("answer too large")
        answer = json.loads(body)
        if answer.get("status") != "success" or answer["data"]["resultType"] != "vector":
            raise PrometheusUnavailable("query failed")
        return [float(sample["value"][1]) for sample in answer["data"]["result"]]
    except PrometheusUnavailable:
        raise
    except urllib.error.HTTPError as error:
        error.close()
        raise PrometheusUnavailable(f"HTTP {error.code}") from None
    except Exception as error:  # noqa: BLE001 - any failure reads as Prometheus being unavailable
        raise PrometheusUnavailable(type(error).__name__) from error


def probe_status(prometheus_url: str, allowed: frozenset[str], name: str) -> tuple[int, dict]:
    if name not in allowed or not PROBE_NAME.fullmatch(name):
        return HTTPStatus.NOT_FOUND, _payload(False, 404, "Unknown service", "Service is not monitored")
    selector = f'probe_success{{job="blackbox",probe="{name}"}}'
    try:
        latest = query(prometheus_url, selector)
        recent = query(prometheus_url, f"max_over_time({selector}[{RED_AFTER}])")
    except PrometheusUnavailable:
        return HTTPStatus.SERVICE_UNAVAILABLE, _payload(
            False, 503, "Prometheus unavailable", "Probe results are unavailable"
        )
    if not latest:
        # A probe that is no longer scraped goes stale in Prometheus.
        return HTTPStatus.OK, _payload(False, 503, "No data", "No recent probe result")
    if min(latest) >= 1:
        return HTTPStatus.OK, _payload(True, 200, "Healthy", "Latest probe passed")
    if recent and max(recent) >= 1:
        return HTTPStatus.OK, _payload(True, 200, "Probe failed", "Latest probe failed")
    return HTTPStatus.OK, _payload(False, 503, "Unhealthy", "Probe has failed for 5 minutes")


class StatusHandler(BaseHTTPRequestHandler):
    prometheus_url: str
    allowed: frozenset[str]

    def log_message(self, format: str, *args: object) -> None:
        return

    def _send_json(self, status: int, payload: dict) -> None:
        encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        if len(encoded) > MAX_RESPONSE_BYTES:
            encoded = b'{"successStatus":false,"message":"Response too large"}'
            status = HTTPStatus.INTERNAL_SERVER_ERROR
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            self._send_json(HTTPStatus.OK, {"status": "healthy"})
            return
        if not is_status_path(parsed.path):
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        target = (parse_qs(parsed.query, keep_blank_values=True).get("url") or [""])[0]
        name = probe_name_from_target(target)
        if name is None:
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                _payload(False, 400, "Invalid target", "Only probe names are accepted"),
            )
            return
        status, payload = probe_status(self.prometheus_url, self.allowed, name)
        self._send_json(status, payload)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--prometheus", required=True, help="Prometheus base URL")
    parser.add_argument("--probe", action="append", default=[], help="a probe name Dashy may ask for")
    args = parser.parse_args()
    for name in args.probe:
        if not PROBE_NAME.fullmatch(name):
            parser.error(f"invalid probe name: {name!r}")
    return args


class StatusServer(ThreadingHTTPServer):
    # Dashy checks every tile at once on page load. socketserver's default
    # backlog of 5 (before Python 3.15) overflows, and macOS resets the extra
    # connections, which Caddy reports as 502s and Dashy as red dots.
    request_queue_size = 128
    daemon_threads = True


def build_server(host: str, port: int, prometheus_url: str, probes: list[str]) -> StatusServer:
    handler = type(
        "ConfiguredStatusHandler",
        (StatusHandler,),
        {"prometheus_url": prometheus_url, "allowed": frozenset(probes)},
    )
    return StatusServer((host, port), handler)


def main() -> int:
    args = parse_args()
    server = build_server(args.host, args.port, args.prometheus, args.probe)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
