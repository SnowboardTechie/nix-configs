#!/usr/bin/env python3
"""Self-check for check-hindsight-releases.py report logic and pin parsing."""

import importlib.util
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
    changed, report = mod.build_report(pins, dict(pins))
    assert not changed and report == ""


def test_mentions_bryan_on_stale_pin():
    pins = {"hindsight-api": "0.9.1"}
    changed, report = mod.build_report(pins, {"hindsight-api": "0.9.2"})
    assert changed
    assert mod.MATRIX_MENTION in report
    assert "0.9.1 -> latest 0.9.2" in report
    assert mod.RELEASES_URL in report


if __name__ == "__main__":
    test_pins_parse_from_committed_locks()
    test_silent_when_current()
    test_mentions_bryan_on_stale_pin()
    print("ok")
