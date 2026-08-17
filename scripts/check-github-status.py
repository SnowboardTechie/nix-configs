#!/usr/bin/env python3
"""Emit a stable, normalized GitHub Status snapshot for Hermes monitor mode."""

from __future__ import annotations

import json
import os
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, Mapping

GITHUB_STATUS_API = "https://www.githubstatus.com/api/v2/summary.json"
GITHUB_STATUS_PAGE = "https://www.githubstatus.com/"
STATE_PATH = Path.home() / ".hermes" / "state" / "github-status-last-good.json"
USER_AGENT = "nix-configs-github-status-watch/1.0"
TIMEOUT_SECONDS = 20
MAX_ATTEMPTS = 3


def default_http_get(url: str, timeout: int = TIMEOUT_SECONDS) -> bytes:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def fetch_summary(
    *,
    http_get: Callable[..., bytes] = default_http_get,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> Mapping[str, object]:
    last_error: Exception | None = None
    for attempt in range(MAX_ATTEMPTS):
        try:
            body = http_get(GITHUB_STATUS_API, timeout=TIMEOUT_SECONDS)
            value = json.loads(body)
            if not isinstance(value, dict):
                raise RuntimeError("GitHub Status summary was not an object")
            return value
        except (OSError, TimeoutError, urllib.error.URLError, json.JSONDecodeError, RuntimeError) as error:
            last_error = error
            if attempt + 1 < MAX_ATTEMPTS:
                sleep_fn(float(2**attempt))
    assert last_error is not None
    raise last_error


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""


def normalize_summary(summary: Mapping[str, object]) -> dict[str, object]:
    status = summary.get("status")
    incidents = summary.get("incidents")
    components = summary.get("components")
    if not isinstance(status, dict):
        raise RuntimeError("GitHub Status summary omitted status")
    if not isinstance(incidents, list) or not isinstance(components, list):
        raise RuntimeError("GitHub Status summary omitted incidents or components")

    normalized_incidents: list[dict[str, str]] = []
    for incident in incidents:
        if not isinstance(incident, dict):
            continue
        updates = incident.get("incident_updates")
        latest_body = ""
        if isinstance(updates, list):
            for update in updates:
                if isinstance(update, dict) and _text(update.get("body")):
                    latest_body = _text(update.get("body"))
                    break
        normalized_incidents.append(
            {
                "id": _text(incident.get("id")),
                "impact": _text(incident.get("impact")),
                "latest_update": latest_body,
                "name": _text(incident.get("name")),
                "status": _text(incident.get("status")),
            }
        )

    affected_components: list[dict[str, str]] = []
    for component in components:
        if not isinstance(component, dict):
            continue
        component_status = _text(component.get("status"))
        if not component_status or component_status == "operational":
            continue
        affected_components.append(
            {
                "name": _text(component.get("name")),
                "status": component_status,
            }
        )

    return {
        "affected_components": sorted(
            affected_components, key=lambda item: (item["name"], item["status"])
        ),
        "incidents": sorted(
            normalized_incidents, key=lambda item: (item["id"], item["name"])
        ),
        "overall": {
            "description": _text(status.get("description")),
            "indicator": _text(status.get("indicator")),
        },
        "source": GITHUB_STATUS_PAGE,
        "source_health": "ok",
    }


def load_last_good(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def save_last_good(path: Path, snapshot: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(snapshot, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_name, 0o600)
        os.replace(temporary_name, path)
    finally:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass


def run(
    *,
    http_get: Callable[..., bytes] = default_http_get,
    state_path: Path = STATE_PATH,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> dict[str, object]:
    try:
        snapshot = normalize_summary(
            fetch_summary(http_get=http_get, sleep_fn=sleep_fn)
        )
        save_last_good(state_path, snapshot)
        return snapshot
    except Exception as error:
        snapshot = load_last_good(state_path)
        snapshot["source"] = GITHUB_STATUS_PAGE
        snapshot["source_health"] = "unavailable"
        snapshot["source_error"] = type(error).__name__
        return snapshot


def render(snapshot: Mapping[str, object]) -> str:
    return json.dumps(snapshot, sort_keys=True, separators=(",", ":"))


def main() -> int:
    print(render(run()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
