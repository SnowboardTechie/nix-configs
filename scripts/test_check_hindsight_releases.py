#!/usr/bin/env python3
"""Self-check for check-hindsight-releases.py report logic and pin parsing."""

import importlib.util
import io
import zipfile
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "check_hindsight_releases", Path(__file__).parent / "check-hindsight-releases.py"
)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


def test_pins_parse_from_committed_locks():
    assert mod.pinned_api_version() == "0.9.1"
    assert mod.pinned_cp_version() == "0.9.1"
    assert mod.CODING_AGENTS_PIN == "0.3.4"


def test_silent_when_current():
    pins = {"a": "1.0.0", "b": "2.0.0"}
    changed, report = mod.build_report(pins, dict(pins), {})
    assert not changed and report == ""


def test_silent_while_only_coding_agents_is_newer_but_api_is_incompatible():
    pins = {
        "hindsight-api": "0.9.1",
        "hindsight-control-plane": "0.9.1",
        "hindsight-coding-agents": "0.3.4",
    }
    latest = {**pins, "hindsight-coding-agents": "0.4.1"}
    compatibility = {
        "accepts_page_trigger_patch": False,
        "reports_page_trigger": False,
    }
    changed, report = mod.build_report(pins, latest, compatibility)
    assert not changed and report == ""


def test_mentions_bryan_when_server_release_changes_but_coding_agents_stays_held():
    pins = {
        "hindsight-api": "0.9.1",
        "hindsight-control-plane": "0.9.1",
        "hindsight-coding-agents": "0.3.4",
    }
    latest = {
        "hindsight-api": "0.9.2",
        "hindsight-control-plane": "0.9.1",
        "hindsight-coding-agents": "0.4.1",
    }
    compatibility = {
        "accepts_page_trigger_patch": False,
        "reports_page_trigger": False,
    }
    changed, report = mod.build_report(pins, latest, compatibility)
    assert changed
    assert mod.MATRIX_MENTION in report
    assert "0.9.1 -> latest 0.9.2" in report
    assert "still held" in report
    assert mod.RELEASES_URL in report


def test_mentions_bryan_when_coding_agent_compatibility_gates_pass():
    pins = {
        "hindsight-api": "0.9.2",
        "hindsight-control-plane": "0.9.1",
        "hindsight-coding-agents": "0.3.4",
    }
    latest = {**pins, "hindsight-coding-agents": "0.4.1"}
    compatibility = {
        "accepts_page_trigger_patch": True,
        "reports_page_trigger": True,
    }
    changed, report = mod.build_report(pins, latest, compatibility)
    assert changed
    assert "ready for reassessment" in report
    assert "compatibility: passed" in report


def make_api_wheel(*, update_trigger: bool, tree_trigger: bool) -> bytes:
    update_field = "    trigger: dict | None = None\n" if update_trigger else ""
    tree_field = "    trigger: dict | None = None\n" if tree_trigger else ""
    source = (
        "class UpdateNodeRequest:\n"
        "    name: str | None = None\n"
        f"{update_field}"
        "\n"
        "class KnowledgeNode:\n"
        "    name: str\n"
        f"{tree_field}"
    )
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr("hindsight_api/api/http.py", source)
    return payload.getvalue()


def test_inspects_compatibility_from_the_released_api_wheel():
    release = {
        "urls": [
            {
                "filename": "hindsight_api_slim-0.9.2-py3-none-any.whl",
                "packagetype": "bdist_wheel",
                "url": "https://example.test/hindsight-api.whl",
            }
        ]
    }
    compatibility = mod.inspect_api_compatibility(
        "0.9.2",
        fetch_json=lambda _url: release,
        fetch_bytes=lambda _url: make_api_wheel(update_trigger=True, tree_trigger=True),
    )
    assert compatibility == {
        "api_version": "0.9.2",
        "accepts_page_trigger_patch": True,
        "reports_page_trigger": True,
    }


def test_rejects_an_oversized_api_source_before_decompression():
    release = {
        "urls": [
            {
                "filename": "hindsight_api_slim-0.9.2-py3-none-any.whl",
                "packagetype": "bdist_wheel",
                "url": "https://example.test/hindsight-api.whl",
            }
        ]
    }
    original_limit = getattr(mod, "MAX_API_SOURCE_BYTES")
    setattr(mod, "MAX_API_SOURCE_BYTES", 8)
    try:
        try:
            mod.inspect_api_compatibility(
                "0.9.2",
                fetch_json=lambda _url: release,
                fetch_bytes=lambda _url: make_api_wheel(update_trigger=True, tree_trigger=True),
            )
        except RuntimeError as error:
            assert "oversized hindsight_api/api/http.py" in str(error)
        else:
            raise AssertionError("oversized API source should be rejected")
    finally:
        setattr(mod, "MAX_API_SOURCE_BYTES", original_limit)


if __name__ == "__main__":
    test_pins_parse_from_committed_locks()
    test_silent_when_current()
    test_silent_while_only_coding_agents_is_newer_but_api_is_incompatible()
    test_mentions_bryan_when_server_release_changes_but_coding_agents_stays_held()
    test_mentions_bryan_when_coding_agent_compatibility_gates_pass()
    test_inspects_compatibility_from_the_released_api_wheel()
    test_rejects_an_oversized_api_source_before_decompression()
    print("ok")
