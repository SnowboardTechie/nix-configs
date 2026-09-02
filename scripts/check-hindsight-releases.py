#!/usr/bin/env python3
"""Daily read-only watch on Hindsight release and installed-version drift.

Registered as a Hermes cron job in no-agent mode (stdout delivered to the
general Matrix conversation). The API and Control Plane remain lock-managed
because they own database migrations and service startup. Coding Agents updates
its staged runtime automatically, so this watcher compares the installed
runtime rather than a repository bootstrap pin. Never edits locks, installs,
restarts, or updates anything.

Versions are read from the committed lock material or installed runtime:
  - hindsight-api: modules/services/hindsight-env/uv.lock
  - @vectorize-io/hindsight-control-plane:
      modules/services/hindsight-env/control-plane/package-lock.json
  - @vectorize-io/hindsight-coding-agents:
      ~/.hindsight/coding-agents/package.json (auto-updated runtime)
"""

from __future__ import annotations

import json
import os
import re
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable, Mapping

# Deployed copies run from ~/.hermes/scripts/ with the repo as workdir, so
# prefer the CWD when it holds the lock material; fall back to file-relative.
_FILE_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = Path.cwd() if (Path.cwd() / "modules/services/hindsight-env/uv.lock").exists() else _FILE_ROOT
UV_LOCK = REPO_ROOT / "modules/services/hindsight-env/uv.lock"
CP_LOCK = REPO_ROOT / "modules/services/hindsight-env/control-plane/package-lock.json"
CODING_AGENTS_PACKAGE = Path.home() / ".hindsight" / "coding-agents" / "package.json"

STATE_PATH = Path.home() / ".hermes" / "state" / "hindsight-release-watch.json"
USER_AGENT = "nix-configs-hindsight-release-watch/1.0"
TIMEOUT_SECONDS = 20
MATRIX_MENTION = "@bryan:snowboardtechie.com"
RELEASES_URL = "https://github.com/vectorize-io/hindsight/releases"
CODING_AGENTS_URL = "https://www.npmjs.com/package/@vectorize-io/hindsight-coding-agents"


def http_json(url: str) -> dict:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
        return json.load(response)


def pinned_api_version() -> str:
    text = UV_LOCK.read_text(encoding="utf-8")
    match = re.search(r'name = "hindsight-api"\nversion = "([^"]+)"', text)
    if not match:
        raise RuntimeError("hindsight-api pin not found in uv.lock")
    return match.group(1)


def pinned_cp_version() -> str:
    lock = json.loads(CP_LOCK.read_text(encoding="utf-8"))
    entry = lock["packages"]["node_modules/@vectorize-io/hindsight-control-plane"]
    return entry["version"]


def installed_coding_agents_version(package_path: Path = CODING_AGENTS_PACKAGE) -> str:
    """Return the staged runtime version managed by Coding Agents auto-update."""
    try:
        package = json.loads(package_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return "not installed"
    version = package.get("version")
    if not isinstance(version, str) or not version.strip():
        raise RuntimeError(f"invalid Coding Agents package metadata at {package_path}")
    return version.strip()


def latest_versions(fetch: Callable[[str], dict] = http_json) -> dict[str, str]:
    return {
        "hindsight-api": fetch("https://pypi.org/pypi/hindsight-api/json")["info"]["version"],
        "hindsight-control-plane": fetch(
            "https://registry.npmjs.org/@vectorize-io/hindsight-control-plane/latest"
        )["version"],
        "hindsight-coding-agents": fetch(
            "https://registry.npmjs.org/@vectorize-io/hindsight-coding-agents/latest"
        )["version"],
    }


def build_report(
    pins: Mapping[str, str],
    latest: Mapping[str, str],
) -> tuple[bool, str]:
    stale = {name: (pins[name], latest[name]) for name in pins if pins[name] != latest[name]}
    server_stale = {name: versions for name, versions in stale.items() if name != "hindsight-coding-agents"}
    coding_stale = "hindsight-coding-agents" in stale

    if not stale:
        return False, ""

    headline = "installed Hindsight components are behind current releases."
    lines = [f"{MATRIX_MENTION} Hindsight release watch: {headline}"]
    for name, (pinned, current) in sorted(stale.items()):
        state = "installed" if name == "hindsight-coding-agents" else "locked"
        lines.append(f"- {name}: {state} {pinned} -> latest {current}")

    if server_stale:
        lines.append("- server action: review the server migration, take a pre-upgrade backup, and update the locks")
    if coding_stale:
        lines.append(
            "- client action: the automatic client update has not landed; start a configured coding-agent "
            "session, then verify the staged runtime"
        )

    lines.append(f"Releases: {RELEASES_URL}")
    lines.append(f"Coding agents: {CODING_AGENTS_URL}")
    lines.append(
        "Security posture: Hindsight ships security fixes only on its latest release, "
        "so server-lock or installed-runtime drift also means no security support."
    )
    lines.append(
        "This watch is read-only. Server updates remain lock-managed because they can migrate PostgreSQL; "
        "Coding Agents owns its supported automatic runtime update."
    )
    return True, "\n".join(lines)


def main() -> int:
    pins = {
        "hindsight-api": pinned_api_version(),
        "hindsight-control-plane": pinned_cp_version(),
        "hindsight-coding-agents": installed_coding_agents_version(),
    }
    latest = latest_versions()
    changed, report = build_report(pins, latest)

    state_path = Path(os.environ.get("HINDSIGHT_RELEASE_WATCH_STATE_PATH", STATE_PATH)).expanduser()
    try:
        previous = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        previous = {}
    notification_signature = {
        "pins": pins,
        "latest": latest,
        "actionable": changed,
    }
    state = {
        "pins": pins,
        "latest": latest,
        "notification_signature": notification_signature,
        "checked_at": datetime.now(UTC).isoformat(),
    }
    state_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = state_path.with_name(f".{state_path.name}.tmp")
    temporary.write_text(json.dumps(state, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(state_path)

    # Persist before emitting so a delivery retry cannot create repeated alerts.
    if changed and previous.get("notification_signature") != notification_signature:
        print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
