#!/usr/bin/env python3
"""Self-check for check-hindsight-releases.py report logic and version discovery."""

import importlib.util
import json
import sys
import tempfile
from pathlib import Path

sys.dont_write_bytecode = True

spec = importlib.util.spec_from_file_location(
    "check_hindsight_releases", Path(__file__).parent / "check-hindsight-releases.py"
)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


def test_pins_parse_from_committed_locks():
    assert mod.pinned_api_version() == "0.9.2"
    assert mod.pinned_cp_version() == "0.9.2"


def test_reads_the_auto_updated_runtime_version():
    with tempfile.TemporaryDirectory() as directory:
        package = Path(directory) / "package.json"
        package.write_text(json.dumps({"version": "0.5.1"}), encoding="utf-8")
        assert mod.installed_coding_agents_version(package) == "0.5.1"


def test_silent_when_current():
    pins = {
        "hindsight-api": "0.9.2",
        "hindsight-control-plane": "0.9.2",
        "hindsight-coding-agents": "0.5.1",
    }
    changed, report = mod.build_report(pins, dict(pins))
    assert not changed and report == ""


def test_mentions_bryan_when_an_auto_updated_client_is_stale():
    pins = {
        "hindsight-api": "0.9.2",
        "hindsight-control-plane": "0.9.2",
        "hindsight-coding-agents": "0.5.1",
    }
    latest = {**pins, "hindsight-coding-agents": "0.5.2"}
    changed, report = mod.build_report(pins, latest)
    assert changed
    assert mod.MATRIX_MENTION in report
    assert "installed 0.5.1 -> latest 0.5.2" in report
    assert "automatic client update has not landed" in report


def test_mentions_bryan_when_server_release_changes():
    pins = {
        "hindsight-api": "0.9.2",
        "hindsight-control-plane": "0.9.2",
        "hindsight-coding-agents": "0.5.1",
    }
    latest = {
        "hindsight-api": "0.9.3",
        "hindsight-control-plane": "0.9.3",
        "hindsight-coding-agents": "0.5.1",
    }
    changed, report = mod.build_report(pins, latest)
    assert changed
    assert mod.MATRIX_MENTION in report
    assert "locked 0.9.2 -> latest 0.9.3" in report
    assert "review the server migration" in report
    assert mod.RELEASES_URL in report


if __name__ == "__main__":
    test_pins_parse_from_committed_locks()
    test_reads_the_auto_updated_runtime_version()
    test_silent_when_current()
    test_mentions_bryan_when_an_auto_updated_client_is_stale()
    test_mentions_bryan_when_server_release_changes()
    print("ok")
