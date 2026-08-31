#!/usr/bin/env python3
"""Expose allowlisted Studio watchdog state in Dashy's status-check format."""

from __future__ import annotations

import argparse
import json
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

MAX_STATE_AGE_SECONDS = 15 * 60
MAX_RESPONSE_BYTES = 16 * 1024


def service_name_from_target(target: str) -> str | None:
    parsed = urlparse(target)
    if parsed.scheme != "watchdog" or parsed.netloc:
        return None
    name = unquote(parsed.path.lstrip("/")).strip()
    return name or None


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


def service_status(state_path: Path, service_name: str, *, now: float | None = None) -> tuple[int, dict]:
    current_time = time.time() if now is None else now
    try:
        age = current_time - state_path.stat().st_mtime
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return HTTPStatus.SERVICE_UNAVAILABLE, _payload(
            False, 503, "Watchdog unavailable", "Watchdog state is unavailable"
        )

    services = state.get("services") if isinstance(state, dict) else None
    if not isinstance(services, dict) or service_name not in services:
        return HTTPStatus.NOT_FOUND, _payload(
            False, 404, "Unknown service", "Service is not monitored"
        )
    if age > MAX_STATE_AGE_SECONDS:
        return HTTPStatus.OK, _payload(
            False, 503, "Watchdog stale", "Watchdog state is older than 15 minutes"
        )

    record = services.get(service_name)
    if not isinstance(record, dict):
        return HTTPStatus.OK, _payload(False, 503, "Unknown", "Service state is invalid")
    if record.get("last_error") is not None:
        return HTTPStatus.OK, _payload(False, 503, "Unhealthy", "Latest check failed")
    return HTTPStatus.OK, _payload(True, 200, "Healthy", "Latest check passed")


class StatusHandler(BaseHTTPRequestHandler):
    state_path: Path

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
        service_name = service_name_from_target(target)
        if service_name is None:
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                _payload(False, 400, "Invalid target", "Only watchdog service names are accepted"),
            )
            return
        status, payload = service_status(self.state_path, service_name)
        self._send_json(status, payload)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--state", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    handler = type("ConfiguredStatusHandler", (StatusHandler,), {"state_path": args.state})
    server = ThreadingHTTPServer((args.host, args.port), handler)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
