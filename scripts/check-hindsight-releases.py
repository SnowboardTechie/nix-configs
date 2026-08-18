#!/usr/bin/env python3
"""Daily read-only watch on Hindsight releases vs the pins committed here.

Registered as a Hermes cron job in no-agent mode (stdout delivered to the
general Matrix conversation). Silent while the latest upstream versions match
the pinned ones and no change happened since the last run; mentions Bryan
when a pin falls behind or the watch itself changes state. Never edits locks,
installs, restarts, or updates anything.

Pins are read from the committed lock material:
  - hindsight-api: modules/services/hindsight-env/uv.lock
  - @vectorize-io/hindsight-control-plane:
      modules/services/hindsight-env/control-plane/package-lock.json
  - @vectorize-io/hindsight-coding-agents: CODING_AGENTS_PIN below
    (pinned in ~/code/dotfiles, mirrored here for the watch)
"""

from __future__ import annotations

import json
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
CODING_AGENTS_PIN = "0.3.4"

STATE_PATH = Path.home() / ".hermes" / "state" / "hindsight-release-watch.json"
USER_AGENT = "nix-configs-hindsight-release-watch/1.0"
TIMEOUT_SECONDS = 20
MATRIX_MENTION = "@bryan:snowboardtechie.com"
RELEASES_URL = "https://github.com/vectorize-io/hindsight/releases"


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


def build_report(pins: Mapping[str, str], latest: Mapping[str, str]) -> tuple[bool, str]:
    stale = {name: (pins[name], latest[name]) for name in pins if pins[name] != latest[name]}
    if not stale:
        return False, ""
    lines = [f"{MATRIX_MENTION} Hindsight release watch: pinned versions are behind upstream."]
    for name, (pinned, current) in sorted(stale.items()):
        lines.append(f"- {name}: pinned {pinned} -> latest {current}")
    lines.append(f"Releases: {RELEASES_URL}")
    lines.append(
        "Security posture: Hindsight ships security fixes only on its latest release, "
        "so a stale pin also means no security support."
    )
    lines.append(
        "Compatibility to confirm before updating: macOS launchd service, PostgreSQL 17 "
        "schema migrations, and the coding-agent integration on all client machines."
    )
    lines.append(
        "This watch is read-only. Reply here to ask for a full update assessment; "
        "updating means a reviewed lock change in nix-configs/dotfiles + update-studio."
    )
    return True, "\n".join(lines)


def main() -> int:
    pins = {
        "hindsight-api": pinned_api_version(),
        "hindsight-control-plane": pinned_cp_version(),
        "hindsight-coding-agents": CODING_AGENTS_PIN,
    }
    latest = latest_versions()
    changed, report = build_report(pins, latest)

    try:
        previous = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        previous = {}
    state = {"pins": pins, "latest": latest, "checked_at": datetime.now(UTC).isoformat()}
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, sort_keys=True) + "\n", encoding="utf-8")

    # Silent while unchanged: only speak when versions differ from the pins
    # and the (pins, latest) pair differs from the previous run's.
    if changed and (previous.get("pins"), previous.get("latest")) != (pins, latest):
        print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
