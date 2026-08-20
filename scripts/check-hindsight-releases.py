#!/usr/bin/env python3
"""Daily read-only watch on Hindsight release and update readiness.

Registered as a Hermes cron job in no-agent mode (stdout delivered to the
general Matrix conversation). A newer coding-agent package is not actionable
until the latest released API artifact can both patch and report knowledge-page
triggers. The watcher stays silent while that known compatibility hold remains,
but reports server release drift and the first compatible coding-agent
candidate. Never edits locks, installs, restarts, or updates anything.

Pins are read from the committed lock material:
  - hindsight-api: modules/services/hindsight-env/uv.lock
  - @vectorize-io/hindsight-control-plane:
      modules/services/hindsight-env/control-plane/package-lock.json
  - @vectorize-io/hindsight-coding-agents: CODING_AGENTS_PIN below
    (pinned in ~/code/dotfiles, mirrored here for the watch)
"""

from __future__ import annotations

import ast
import io
import json
import os
import re
import urllib.request
import zipfile
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
MAX_WHEEL_BYTES = 25 * 1024 * 1024
MAX_API_SOURCE_BYTES = 5 * 1024 * 1024
MATRIX_MENTION = "@bryan:snowboardtechie.com"
RELEASES_URL = "https://github.com/vectorize-io/hindsight/releases"
CODING_AGENTS_URL = "https://www.npmjs.com/package/@vectorize-io/hindsight-coding-agents"
TRIGGER_PATCH_FIX_URL = "https://github.com/vectorize-io/hindsight/pull/3549"
TRIGGER_READBACK_FIX_URL = "https://github.com/vectorize-io/hindsight/pull/3572"


def http_json(url: str) -> dict:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
        return json.load(response)


def http_bytes(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/octet-stream"})
    with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
        declared_size = response.headers.get("Content-Length")
        if declared_size and int(declared_size) > MAX_WHEEL_BYTES:
            raise RuntimeError(f"refusing oversized Hindsight API wheel ({declared_size} bytes)")
        payload = response.read(MAX_WHEEL_BYTES + 1)
    if len(payload) > MAX_WHEEL_BYTES:
        raise RuntimeError(f"refusing oversized Hindsight API wheel (>{MAX_WHEEL_BYTES} bytes)")
    return payload


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


def annotated_fields(source: str, class_name: str) -> set[str]:
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            return {
                child.target.id
                for child in node.body
                if isinstance(child, ast.AnnAssign) and isinstance(child.target, ast.Name)
            }
    raise RuntimeError(f"{class_name} not found in released Hindsight API artifact")


def inspect_api_compatibility(
    api_version: str,
    fetch_json: Callable[[str], dict] = http_json,
    fetch_bytes: Callable[[str], bytes] = http_bytes,
) -> dict[str, bool | str]:
    release = fetch_json(f"https://pypi.org/pypi/hindsight-api-slim/{api_version}/json")
    wheels = sorted(
        (item for item in release.get("urls", []) if item.get("packagetype") == "bdist_wheel"),
        key=lambda item: item.get("filename", ""),
    )
    if not wheels:
        raise RuntimeError(f"no hindsight-api-slim wheel published for {api_version}")

    wheel = fetch_bytes(wheels[0]["url"])
    with zipfile.ZipFile(io.BytesIO(wheel)) as archive:
        sources = [name for name in archive.namelist() if name.endswith("hindsight_api/api/http.py")]
        if len(sources) != 1:
            raise RuntimeError(f"expected one hindsight_api/api/http.py in wheel, found {len(sources)}")
        source_info = archive.getinfo(sources[0])
        if source_info.file_size > MAX_API_SOURCE_BYTES:
            raise RuntimeError(
                f"refusing oversized hindsight_api/api/http.py ({source_info.file_size} bytes)"
            )
        source = archive.read(source_info).decode("utf-8")

    update_fields = annotated_fields(source, "UpdateNodeRequest")
    tree_fields = annotated_fields(source, "KnowledgeNode")
    return {
        "api_version": api_version,
        "accepts_page_trigger_patch": "trigger" in update_fields,
        "reports_page_trigger": "trigger" in tree_fields,
    }


def compatibility_ready(compatibility: Mapping[str, object]) -> bool:
    return bool(
        compatibility.get("accepts_page_trigger_patch")
        and compatibility.get("reports_page_trigger")
    )


def build_report(
    pins: Mapping[str, str],
    latest: Mapping[str, str],
    compatibility: Mapping[str, object],
) -> tuple[bool, str]:
    stale = {name: (pins[name], latest[name]) for name in pins if pins[name] != latest[name]}
    server_stale = {name: versions for name, versions in stale.items() if name != "hindsight-coding-agents"}
    coding_stale = "hindsight-coding-agents" in stale
    coding_ready = coding_stale and compatibility_ready(compatibility)

    # The currently known state is coding-agents 0.4.1 ahead of the pin while
    # the released 0.9.1 API cannot accept its trigger-only page PATCH. That is
    # not an actionable update and should remain silent until a server artifact
    # changes or the compatibility gates pass.
    if not server_stale and not coding_ready:
        return False, ""

    if coding_ready:
        headline = "a coordinated Hindsight update is ready for reassessment."
    else:
        headline = "upstream Hindsight server releases changed; review the available update."
    lines = [f"{MATRIX_MENTION} Hindsight release watch: {headline}"]
    for name, (pinned, current) in sorted(stale.items()):
        lines.append(f"- {name}: pinned {pinned} -> latest {current}")

    if coding_stale and coding_ready:
        lines.append(
            "- coding-agent compatibility: passed — the latest released API artifact accepts "
            "page-trigger PATCHes and reports page triggers for idempotent reconciliation"
        )
    elif coding_stale:
        missing = []
        if not compatibility.get("accepts_page_trigger_patch"):
            missing.append("page-trigger PATCH support")
        if not compatibility.get("reports_page_trigger"):
            missing.append("page-trigger readback")
        lines.append(
            "- coding-agent compatibility: still held — latest released API artifact lacks "
            + " and ".join(missing)
        )
        lines.append(f"Required upstream fixes: {TRIGGER_PATCH_FIX_URL} and {TRIGGER_READBACK_FIX_URL}")

    lines.append(f"Releases: {RELEASES_URL}")
    lines.append(f"Coding agents: {CODING_AGENTS_URL}")
    lines.append(
        "Security posture: Hindsight ships security fixes only on its latest release, "
        "so a stale pin also means no security support."
    )
    lines.append(
        "Treat this as a coordinated server + client assessment: back up PostgreSQL, review migrations, "
        "verify the released OpenAPI surface, canary Studio, then test Claude/OpenCode retention and recall."
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
    compatibility = inspect_api_compatibility(latest["hindsight-api"])
    changed, report = build_report(pins, latest, compatibility)

    state_path = Path(os.environ.get("HINDSIGHT_RELEASE_WATCH_STATE_PATH", STATE_PATH)).expanduser()
    try:
        previous = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        previous = {}
    notification_signature = {
        "pins": pins,
        "latest": latest,
        "compatibility": compatibility,
        "actionable": changed,
    }
    state = {
        "pins": pins,
        "latest": latest,
        "compatibility": compatibility,
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
