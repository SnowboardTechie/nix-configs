#!/usr/bin/env python3
"""Detect and stage one notification when official Inkling-Small weights ship."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable, Mapping

HF_ORG = "thinkingmachines"
HF_MODELS_API = "https://huggingface.co/api/models?" + urllib.parse.urlencode(
    {
        "author": HF_ORG,
        "search": "Inkling",
        "limit": 100,
        "full": "true",
    }
)
HF_MODEL_URL_PREFIX = "https://huggingface.co/"
STATE_PATH = Path.home() / ".hermes" / "state" / "inkling-small-release-watch.json"
USER_AGENT = "nix-configs-inkling-small-release-watch/1.0"
TIMEOUT_SECONDS = 20
MAX_ATTEMPTS = 3
MIN_WEIGHT_BYTES = 1_000_000_000
WEIGHT_SUFFIXES = (".safetensors", ".gguf", ".bin")


@dataclass(frozen=True)
class HttpResponse:
    body: bytes
    headers: Mapping[str, str]


def default_http_get(
    url: str,
    headers: Mapping[str, str] | None = None,
    timeout: int = TIMEOUT_SECONDS,
) -> HttpResponse:
    request_headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
    }
    request_headers.update(headers or {})
    request = urllib.request.Request(url, headers=request_headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return HttpResponse(response.read(), dict(response.headers.items()))


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
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
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
    sleep_fn: Callable[[float], None],
) -> HttpResponse:
    last_error: Exception | None = None
    for attempt in range(MAX_ATTEMPTS):
        try:
            return http_get(url, headers=None, timeout=TIMEOUT_SECONDS)
        except (OSError, TimeoutError, urllib.error.URLError) as error:
            last_error = error
            if attempt + 1 < MAX_ATTEMPTS:
                sleep_fn(float(2**attempt))
    assert last_error is not None
    raise last_error


def parse_json_list(response: HttpResponse, source: str) -> list[dict[str, object]]:
    try:
        value = json.loads(response.body)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"{source} returned invalid JSON") from error
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise RuntimeError(f"{source} did not return a list of objects")
    return value


def is_official_small_model(model: Mapping[str, object]) -> bool:
    model_id = model.get("id")
    if not isinstance(model_id, str):
        return False
    namespace, separator, name = model_id.partition("/")
    if not separator or namespace.lower() != HF_ORG:
        return False
    lowered_name = name.lower()
    if "inkling" not in lowered_name or "small" not in lowered_name:
        return False
    if bool(model.get("private")):
        return False
    if model.get("gated") not in (None, False):
        return False
    return True


def tree_api_url(model_id: str) -> str:
    encoded_id = urllib.parse.quote(model_id, safe="/")
    return (
        f"https://huggingface.co/api/models/{encoded_id}/tree/main"
        "?recursive=true&expand=true"
    )


def inspect_weights(tree: list[dict[str, object]]) -> dict[str, object] | None:
    weight_files: list[dict[str, object]] = []
    total_bytes = 0
    for entry in tree:
        path = entry.get("path")
        size = entry.get("size")
        if entry.get("type") != "file" or not isinstance(path, str):
            continue
        if not path.lower().endswith(WEIGHT_SUFFIXES):
            continue
        if not isinstance(size, int) or size <= 0:
            continue
        weight_files.append({"path": path, "size": size})
        total_bytes += size

    if total_bytes < MIN_WEIGHT_BYTES:
        return None

    def weight_size(item: Mapping[str, object]) -> int:
        value = item.get("size")
        return value if isinstance(value, int) else 0

    return {
        "weight_file_count_seen": len(weight_files),
        "weight_bytes_seen": total_bytes,
        "largest_weight_files": sorted(
            weight_files, key=weight_size, reverse=True
        )[:5],
    }


def release_signature(models: list[dict[str, object]]) -> str:
    normalized = [
        {
            "id": model["id"],
            "sha": model.get("sha"),
            "weight_bytes_seen": model["weight_bytes_seen"],
        }
        for model in sorted(models, key=lambda item: str(item["id"]))
    ]
    payload = json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


def reset_failures(state: dict[str, object]) -> None:
    state["consecutive_failures"] = 0
    state.pop("health_warning_emitted", None)
    state.pop("last_error", None)


def record_failure(path: Path, state: dict[str, object], error: Exception) -> str:
    previous_failures = state.get("consecutive_failures", 0)
    failures = (previous_failures if isinstance(previous_failures, int) else 0) + 1
    state["consecutive_failures"] = failures
    state["last_error"] = type(error).__name__
    state["last_check_at"] = utc_now()
    output = ""
    if failures >= 3 and not state.get("health_warning_emitted"):
        state["health_warning_emitted"] = True
        output = json.dumps(
            {
                "event": "health_warning",
                "message": (
                    "Inkling-Small release watchdog failed three consecutive official "
                    "Hugging Face checks. Review the Studio job before relying on release notification."
                ),
            },
            sort_keys=True,
        )
    save_state(path, state)
    return output


def _run_locked(
    *,
    http_get: Callable[..., HttpResponse] = default_http_get,
    state_path: Path = STATE_PATH,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> str:
    state = load_state(state_path)
    if state.get("ready_transition_notified"):
        return ""

    try:
        search_response = fetch_with_retries(
            HF_MODELS_API, http_get=http_get, sleep_fn=sleep_fn
        )
        search_results = parse_json_list(search_response, "Hugging Face model search")
        candidates = [model for model in search_results if is_official_small_model(model)]
        ready_models: list[dict[str, object]] = []

        for candidate in candidates:
            model_id = str(candidate["id"])
            tree_response = fetch_with_retries(
                tree_api_url(model_id), http_get=http_get, sleep_fn=sleep_fn
            )
            tree = parse_json_list(tree_response, f"Hugging Face tree for {model_id}")
            weights = inspect_weights(tree)
            if weights is None:
                continue
            ready_models.append(
                {
                    "id": model_id,
                    "url": f"{HF_MODEL_URL_PREFIX}{model_id}",
                    "sha": candidate.get("sha"),
                    "last_modified": candidate.get("lastModified"),
                    "pipeline_tag": candidate.get("pipeline_tag"),
                    **weights,
                }
            )

        state["last_check_at"] = utc_now()
        state["candidate_ids"] = sorted(str(model["id"]) for model in candidates)
        reset_failures(state)
        if not ready_models:
            state.pop("pending_signature", None)
            state.pop("pending_models", None)
            save_state(state_path, state)
            return ""

        signature = release_signature(ready_models)
        state["pending_signature"] = signature
        state["pending_models"] = ready_models
        state.setdefault("first_detected_at", utc_now())
        save_state(state_path, state)
        return json.dumps(
            {
                "event": "release_detected",
                "signature": signature,
                "official_models": ready_models,
                "search_url": HF_MODELS_API,
                "ack_command": (
                    f"python3 {Path.home() / '.hermes' / 'scripts' / 'check-inkling-small-release.py'} "
                    f"--ack {signature}"
                ),
            },
            sort_keys=True,
        )
    except Exception as error:
        return record_failure(state_path, state, error)


def run(
    *,
    http_get: Callable[..., HttpResponse] = default_http_get,
    state_path: Path = STATE_PATH,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> str:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = state_path.with_name(f"{state_path.name}.lock")
    with lock_path.open("a+") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        return _run_locked(
            http_get=http_get,
            state_path=state_path,
            sleep_fn=sleep_fn,
        )


def acknowledge(signature: str, state_path: Path = STATE_PATH) -> bool:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = state_path.with_name(f"{state_path.name}.lock")
    with lock_path.open("a+") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        state = load_state(state_path)
        if state.get("pending_signature") != signature:
            return False
        state["ready_transition_notified"] = True
        state["notified_signature"] = signature
        state["notified_at"] = utc_now()
        save_state(state_path, state)
        return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ack", metavar="SIGNATURE")
    args = parser.parse_args()
    if args.ack:
        if acknowledge(args.ack):
            print("Inkling-Small release notification acknowledged.")
            return 0
        print("No matching pending Inkling-Small release to acknowledge.")
        return 2

    output = run()
    if output:
        print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
