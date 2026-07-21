#!/usr/bin/env python3
import importlib.util
import io
import json
import plistlib
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

MODULE_PATH = Path(__file__).with_name("check-hermes-intel-release.py")
SPEC = importlib.util.spec_from_file_location("hermes_intel_watch", MODULE_PATH)
assert SPEC is not None
watch = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = watch
SPEC.loader.exec_module(watch)


class FakeResponse:
    def __init__(self, body=b"", headers=None):
        self.body = body
        self.headers = headers or {}


class Harness:
    def __init__(self, root, pr, archs="arm64", website_url=None, etag='"artifact-1"'):
        self.root = Path(root)
        self.state_path = self.root / "state" / "watch.json"
        self.mount = self.root / "mount"
        self.mount.mkdir()
        self.app = self.mount / "Hermes.app"
        (self.app / "Contents" / "MacOS").mkdir(parents=True)
        with (self.app / "Contents" / "Info.plist").open("wb") as handle:
            plistlib.dump({"CFBundleExecutable": "Hermes"}, handle)
        (self.app / "Contents" / "MacOS" / "Hermes").write_bytes(b"binary")
        self.pr = pr
        self.archs = archs
        self.website_url = website_url or watch.FALLBACK_DMG_URL
        self.etag = etag
        self.http_calls = []
        self.commands = []
        self.fail_http = False
        self.detach_fails = False
        self.mount_fails = False
        self.malformed_attach = False
        self.failures_by_url = {}

    def http_get(self, url, headers=None, timeout=20):
        self.http_calls.append((url, headers or {}, timeout))
        remaining = self.failures_by_url.get(url, 0)
        if remaining:
            self.failures_by_url[url] = remaining - 1
            raise OSError("temporary network failure")
        if self.fail_http:
            raise OSError("network down")
        if url == watch.PR_API:
            return FakeResponse(json.dumps(self.pr).encode(), {})
        if url == watch.WEBSITE_URL:
            body = f'<html><a href="{self.website_url}">Download for macOS</a></html>'.encode()
            return FakeResponse(body, {})
        if url == self.website_url:
            return FakeResponse(b"fake dmg", {"ETag": self.etag, "Last-Modified": "Sat, 18 Jul 2026 12:00:00 GMT"})
        raise AssertionError(f"unexpected URL {url}")

    def command_runner(self, args, **kwargs):
        self.commands.append(list(args))
        if args[:2] == ["/usr/bin/hdiutil", "attach"]:
            if self.mount_fails:
                return subprocess.CompletedProcess(args, 1, b"", b"mount failed")
            payload = b"not a plist" if self.malformed_attach else plistlib.dumps({"system-entities": [{"mount-point": str(self.mount)}]})
            return subprocess.CompletedProcess(args, 0, payload, b"")
        if args[:2] == ["/usr/bin/hdiutil", "detach"]:
            return subprocess.CompletedProcess(args, 1 if self.detach_fails else 0, b"", b"detach failed")
        if args[:2] == ["/usr/bin/lipo", "-archs"]:
            return subprocess.CompletedProcess(args, 0, self.archs.encode(), b"")
        raise AssertionError(f"unexpected command {args}")

    def run(self):
        return watch.run(
            http_get=self.http_get,
            command_runner=self.command_runner,
            state_path=self.state_path,
            sleep_fn=lambda _: None,
        )


OPEN_PR = {"state": "open", "merged": False, "merged_at": None}
MERGED_PR = {"state": "closed", "merged": True, "merged_at": "2026-07-18T12:00:00Z"}
CLOSED_PR = {"state": "closed", "merged": False, "merged_at": None}


class WatchdogTests(unittest.TestCase):
    def test_main_prefixes_non_silent_output_with_matrix_mention(self):
        output = io.StringIO()
        with mock.patch.object(watch, "run", return_value="Watchdog alert"), redirect_stdout(output):
            self.assertEqual(0, watch.main())
        self.assertEqual(f"{watch.MATRIX_MENTION} Watchdog alert\n", output.getvalue())

    def test_open_pr_is_silent_and_does_not_download_artifact(self):
        with tempfile.TemporaryDirectory() as root:
            harness = Harness(root, OPEN_PR)
            self.assertEqual("", harness.run())
            self.assertEqual([watch.PR_API], [call[0] for call in harness.http_calls])

    def test_merged_arm64_artifact_is_silent(self):
        with tempfile.TemporaryDirectory() as root:
            harness = Harness(root, MERGED_PR, archs="arm64")
            self.assertEqual("", harness.run())
            self.assertTrue(any(command[:2] == ["/usr/bin/lipo", "-archs"] for command in harness.commands))

    def test_merged_universal_artifact_emits_one_notification(self):
        with tempfile.TemporaryDirectory() as root:
            harness = Harness(root, MERGED_PR, archs="arm64 x86_64")
            output = harness.run()
            self.assertIn(watch.PR_URL, output)
            self.assertIn(harness.website_url, output)
            self.assertIn("only after the official app is verified", output)
            self.assertEqual("", harness.run())

    def test_a_later_intel_artifact_does_not_repeat_the_ready_notification(self):
        with tempfile.TemporaryDirectory() as root:
            harness = Harness(root, MERGED_PR, archs="arm64 x86_64", etag='"artifact-1"')
            self.assertIn(watch.PR_URL, harness.run())
            harness.etag = '"artifact-2"'
            self.assertEqual("", harness.run())

    def test_concurrent_ready_checks_emit_only_once(self):
        with tempfile.TemporaryDirectory() as root:
            harness = Harness(root, MERGED_PR, archs="arm64 x86_64")
            outputs = []

            def invoke():
                outputs.append(harness.run())

            threads = [threading.Thread(target=invoke) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual(1, sum(bool(output) for output in outputs))

    def test_nested_electron_helper_apps_do_not_make_top_level_app_ambiguous(self):
        with tempfile.TemporaryDirectory() as root:
            harness = Harness(root, MERGED_PR, archs="arm64 x86_64")
            helper = harness.app / "Contents" / "Frameworks" / "Hermes Helper.app"
            helper.mkdir(parents=True)
            self.assertIn(watch.PR_URL, harness.run())

    def test_closed_unmerged_warning_is_deduplicated(self):
        with tempfile.TemporaryDirectory() as root:
            harness = Harness(root, CLOSED_PR)
            self.assertIn("manual review", harness.run().lower())
            self.assertEqual("", harness.run())

    def test_current_versioned_website_dmg_url_is_inspected(self):
        with tempfile.TemporaryDirectory() as root:
            url = "https://hermes-assets.nousresearch.com/releases/9.9.9/Hermes-Setup.dmg?download=1"
            harness = Harness(root, MERGED_PR, archs="arm64 x86_64", website_url=url)
            self.assertIn(url, harness.run())
            self.assertIn(url, [call[0] for call in harness.http_calls])

    def test_three_bounded_network_failures_trigger_one_health_warning(self):
        with tempfile.TemporaryDirectory() as root:
            harness = Harness(root, OPEN_PR)
            harness.fail_http = True
            self.assertEqual("", harness.run())
            self.assertEqual("", harness.run())
            self.assertIn("watchdog health warning", harness.run().lower())
            self.assertEqual("", harness.run())
            self.assertEqual(12, len(harness.http_calls))

    def test_asset_download_retries_are_bounded_and_can_recover(self):
        with tempfile.TemporaryDirectory() as root:
            harness = Harness(root, MERGED_PR, archs="arm64 x86_64")
            harness.failures_by_url[harness.website_url] = 2
            self.assertIn(watch.PR_URL, harness.run())
            self.assertEqual(3, sum(call[0] == harness.website_url for call in harness.http_calls))

    def test_mount_failure_never_announces_readiness(self):
        with tempfile.TemporaryDirectory() as root:
            harness = Harness(root, MERGED_PR, archs="arm64 x86_64")
            harness.mount_fails = True
            self.assertEqual("", harness.run())

    def test_malformed_attach_plist_never_announces_and_still_detaches(self):
        with tempfile.TemporaryDirectory() as root:
            harness = Harness(root, MERGED_PR, archs="arm64 x86_64")
            harness.malformed_attach = True
            self.assertEqual("", harness.run())
            self.assertTrue(any(command[:2] == ["/usr/bin/hdiutil", "detach"] for command in harness.commands))

    def test_missing_app_never_announces_and_detaches(self):
        with tempfile.TemporaryDirectory() as root:
            harness = Harness(root, MERGED_PR, archs="arm64 x86_64")
            for child in sorted(harness.mount.rglob("*"), reverse=True):
                child.unlink() if child.is_file() else child.rmdir()
            self.assertEqual("", harness.run())
            self.assertTrue(any(command[:2] == ["/usr/bin/hdiutil", "detach"] for command in harness.commands))

    def test_missing_bundle_executable_never_announces(self):
        with tempfile.TemporaryDirectory() as root:
            harness = Harness(root, MERGED_PR, archs="arm64 x86_64")
            with (harness.app / "Contents" / "Info.plist").open("wb") as handle:
                plistlib.dump({}, handle)
            self.assertEqual("", harness.run())

    def test_detach_failure_never_announces_readiness(self):
        with tempfile.TemporaryDirectory() as root:
            harness = Harness(root, MERGED_PR, archs="arm64 x86_64")
            harness.detach_fails = True
            self.assertEqual("", harness.run())

    def test_arm64_only_never_satisfies_live_gate(self):
        with tempfile.TemporaryDirectory() as root:
            harness = Harness(root, MERGED_PR, archs="arm64")
            self.assertEqual("", harness.run())
            state = json.loads(harness.state_path.read_text())
            self.assertNotIn("notified_artifact", state)

    def test_state_write_is_atomic_and_contains_no_credentials(self):
        with tempfile.TemporaryDirectory() as root:
            harness = Harness(root, OPEN_PR)
            self.assertEqual("", harness.run())
            state = json.loads(harness.state_path.read_text())
            self.assertEqual(0, state["consecutive_failures"])
            serialized = json.dumps(state).lower()
            self.assertNotIn("password", serialized)
            self.assertNotIn("cookie", serialized)
            self.assertNotIn("token", serialized)
            self.assertEqual([], list(harness.state_path.parent.glob("*.tmp")))


if __name__ == "__main__":
    unittest.main()
