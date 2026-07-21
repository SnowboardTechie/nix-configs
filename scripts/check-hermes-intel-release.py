#!/usr/bin/env python3
"""Notify once when Hermes' official macOS installer gains Intel support."""

from __future__ import annotations

import hashlib
import fcntl
import json
import os
import plistlib
import re
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable, Mapping

PR_API = "https://api.github.com/repos/NousResearch/hermes-agent/pulls/51777"
PR_URL = "https://github.com/NousResearch/hermes-agent/pull/51777"
WEBSITE_URL = "https://hermes-agent.nousresearch.com/"
FALLBACK_DMG_URL = "https://hermes-assets.nousresearch.com/Hermes-Setup.dmg"
STATE_PATH = Path.home() / ".hermes" / "state" / "hermes-intel-release-watch.json"
USER_AGENT = "nix-configs-hermes-intel-release-watch/1.0"
TIMEOUT_SECONDS = 20
MAX_ATTEMPTS = 3
MATRIX_MENTION = "@bryan:snowboardtechie.com"


@dataclass(frozen=True)
class HttpResponse:
    body: bytes
    headers: Mapping[str, str]


def default_http_get(url: str, headers: Mapping[str, str] | None = None, timeout: int = TIMEOUT_SECONDS) -> HttpResponse:
    request_headers = {"User-Agent": USER_AGENT, "Accept": "*/*"}
    request_headers.update(headers or {})
    request = urllib.request.Request(url, headers=request_headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return HttpResponse(response.read(), dict(response.headers.items()))


def default_command_runner(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(args, check=False, capture_output=True, **kwargs)


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def load_state(path: Path) -> dict[str, object]:
    try:
        with path.open(encoding="utf-8") as handle:
            value = json.load(handle)
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def save_state(path: Path, state: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(state, handle, sort_keys=True, separators=(",", ":"))
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


def fetch_with_retries(
    url: str,
    *,
    http_get: Callable[..., HttpResponse],
    headers: Mapping[str, str] | None,
    sleep_fn: Callable[[float], None],
) -> HttpResponse:
    last_error: Exception | None = None
    for attempt in range(MAX_ATTEMPTS):
        try:
            return http_get(url, headers=headers, timeout=TIMEOUT_SECONDS)
        except (OSError, TimeoutError, urllib.error.URLError) as error:
            last_error = error
            if attempt + 1 < MAX_ATTEMPTS:
                sleep_fn(float(2**attempt))
    assert last_error is not None
    raise last_error


def parse_installer_url(html: bytes) -> str:
    text = html.decode("utf-8", errors="replace")
    matches = re.findall(r"(?:href|src)=[\"']([^\"']+\.dmg(?:\?[^\"']*)?)[\"']", text, flags=re.IGNORECASE)
    hermes_matches = [urllib.parse.urljoin(WEBSITE_URL, value) for value in matches if "hermes" in value.lower()]
    if not hermes_matches:
        return FALLBACK_DMG_URL
    unique = list(dict.fromkeys(hermes_matches))
    if len(unique) != 1:
        raise RuntimeError(f"ambiguous Hermes DMG links on website: {len(unique)}")
    url = unique[0]
    if urllib.parse.urlparse(url).scheme != "https":
        raise RuntimeError("website installer URL is not HTTPS")
    return url


def header_value(headers: Mapping[str, str], name: str) -> str:
    target = name.lower()
    for key, value in headers.items():
        if key.lower() == target:
            return value
    return ""


def artifact_identity(url: str, response: HttpResponse) -> str:
    etag = header_value(response.headers, "ETag")
    modified = header_value(response.headers, "Last-Modified")
    if etag or modified:
        return f"{url}|etag={etag}|last-modified={modified}"
    return f"{url}|sha256={hashlib.sha256(response.body).hexdigest()}"


def inspect_dmg(
    dmg_path: Path,
    *,
    command_runner: Callable[..., subprocess.CompletedProcess[bytes]],
) -> set[str]:
    controlled_mount = dmg_path.parent / "mount"
    controlled_mount.mkdir()
    attach = command_runner(
        [
            "/usr/bin/hdiutil",
            "attach",
            "-nobrowse",
            "-readonly",
            "-mountpoint",
            str(controlled_mount),
            "-plist",
            str(dmg_path),
        ]
    )
    if attach.returncode != 0:
        raise RuntimeError("hdiutil could not mount the installer")

    mount_point: Path | None = None
    detach_target = controlled_mount
    inspection_error: Exception | None = None
    architectures: set[str] | None = None
    try:
        attach_plist = plistlib.loads(attach.stdout)
        mount_points = {
            entity.get("mount-point")
            for entity in attach_plist.get("system-entities", [])
            if isinstance(entity, dict) and isinstance(entity.get("mount-point"), str)
        }
        if len(mount_points) != 1:
            raise RuntimeError("mounted installer did not expose exactly one mount point")
        mount_point = Path(mount_points.pop())
        detach_target = mount_point

        apps = sorted(path for path in mount_point.glob("*.app") if path.is_dir())
        if len(apps) != 1:
            raise RuntimeError(f"mounted installer contains {len(apps)} app bundles")
        app = apps[0]
        plist_path = app / "Contents" / "Info.plist"
        with plist_path.open("rb") as handle:
            executable = plistlib.load(handle).get("CFBundleExecutable")
        if not isinstance(executable, str) or not executable:
            raise RuntimeError("CFBundleExecutable is missing")
        binary = app / "Contents" / "MacOS" / executable
        if not binary.is_file():
            raise RuntimeError("bundle executable is missing")

        lipo = command_runner(["/usr/bin/lipo", "-archs", str(binary)])
        if lipo.returncode != 0:
            raise RuntimeError("lipo could not inspect the bundle executable")
        architectures = set(lipo.stdout.decode("utf-8", errors="replace").split())
    except Exception as error:
        inspection_error = error
    finally:
        detach = command_runner(["/usr/bin/hdiutil", "detach", str(detach_target)])
        if detach.returncode != 0 and inspection_error is None:
            inspection_error = RuntimeError("hdiutil could not detach the installer")

    if inspection_error is not None:
        raise inspection_error
    assert architectures is not None
    return architectures


def reset_failures(state: dict[str, object]) -> None:
    state["consecutive_failures"] = 0
    state.pop("health_warning_emitted", None)
    state.pop("last_error", None)


def record_failure(path: Path, state: dict[str, object], error: Exception) -> str:
    failures = int(state.get("consecutive_failures", 0)) + 1
    state["consecutive_failures"] = failures
    state["last_error"] = type(error).__name__
    state["last_check_at"] = utc_now()
    output = ""
    if failures >= 3 and not state.get("health_warning_emitted"):
        state["health_warning_emitted"] = True
        output = (
            "Hermes Intel release watchdog health warning: three consecutive network or installer "
            "inspection checks failed. Review the Studio job before relying on release notification."
        )
    save_state(path, state)
    return output


def _run_locked(
    *,
    http_get: Callable[..., HttpResponse] = default_http_get,
    command_runner: Callable[..., subprocess.CompletedProcess[bytes]] = default_command_runner,
    state_path: Path = STATE_PATH,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> str:
    state = load_state(state_path)
    if state.get("ready_transition_notified"):
        return ""
    try:
        pr_response = fetch_with_retries(
            PR_API,
            http_get=http_get,
            headers={"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"},
            sleep_fn=sleep_fn,
        )
        pr = json.loads(pr_response.body)
        if not isinstance(pr, dict):
            raise RuntimeError("GitHub PR response is not an object")

        merged = bool(pr.get("merged") or pr.get("merged_at"))
        status = pr.get("state")
        state["last_check_at"] = utc_now()
        state["pr_state"] = status
        state["pr_merged_at"] = pr.get("merged_at")

        if status == "open" and not merged:
            reset_failures(state)
            save_state(state_path, state)
            return ""

        if status == "closed" and not merged:
            reset_failures(state)
            output = ""
            if not state.get("closed_unmerged_warning_emitted"):
                state["closed_unmerged_warning_emitted"] = True
                output = (
                    f"Hermes Intel release watchdog manual review: PR #51777 closed without merging. "
                    f"Reassess the upstream Intel macOS release route. PR: {PR_URL}"
                )
            save_state(state_path, state)
            return output

        if not merged:
            raise RuntimeError(f"unexpected PR state: {status!r}")

        website = fetch_with_retries(
            WEBSITE_URL, http_get=http_get, headers=None, sleep_fn=sleep_fn
        )
        dmg_url = parse_installer_url(website.body)
        dmg = fetch_with_retries(dmg_url, http_get=http_get, headers=None, sleep_fn=sleep_fn)
        identity = artifact_identity(dmg_url, dmg)

        if identity == state.get("last_inspected_artifact") and "x86_64" not in state.get("last_architectures", []):
            reset_failures(state)
            save_state(state_path, state)
            return ""
        if identity == state.get("notified_artifact"):
            reset_failures(state)
            save_state(state_path, state)
            return ""

        with tempfile.TemporaryDirectory(prefix="hermes-intel-release-watch.") as temporary_dir:
            dmg_path = Path(temporary_dir) / "Hermes-Setup.dmg"
            dmg_path.write_bytes(dmg.body)
            architectures = inspect_dmg(dmg_path, command_runner=command_runner)

        state["last_inspected_artifact"] = identity
        state["last_installer_url"] = dmg_url
        state["last_architectures"] = sorted(architectures)
        reset_failures(state)
        if "x86_64" not in architectures:
            save_state(state_path, state)
            return ""

        state["notified_artifact"] = identity
        state["ready_transition_notified"] = True
        state["notified_at"] = utc_now()
        save_state(state_path, state)
        return (
            "Hermes now has an official Intel-capable macOS installer. PR #51777 is merged and the "
            "published Hermes installer contains x86_64. Replace the locally built iMac app only after "
            "the official app is verified, including its Studio connection and relaunch authentication. "
            "After that verification, we can remove the temporary installer and watchdog.\n\n"
            f"PR: {PR_URL}\nInstaller: {dmg_url}"
        )
    except Exception as error:
        return record_failure(state_path, state, error)


def run(
    *,
    http_get: Callable[..., HttpResponse] = default_http_get,
    command_runner: Callable[..., subprocess.CompletedProcess[bytes]] = default_command_runner,
    state_path: Path = STATE_PATH,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> str:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = state_path.with_name(f"{state_path.name}.lock")
    with lock_path.open("a+") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        return _run_locked(
            http_get=http_get,
            command_runner=command_runner,
            state_path=state_path,
            sleep_fn=sleep_fn,
        )


def main() -> int:
    output = run()
    if output:
        print(f"{MATRIX_MENTION} {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
