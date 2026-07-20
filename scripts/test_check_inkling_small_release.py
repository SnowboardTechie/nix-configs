#!/usr/bin/env python3
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).with_name("check-inkling-small-release.py")
SPEC = importlib.util.spec_from_file_location("inkling_small_watch", MODULE_PATH)
assert SPEC is not None
watch = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = watch
SPEC.loader.exec_module(watch)


class FakeResponse:
    def __init__(self, value):
        self.body = json.dumps(value).encode()
        self.headers = {}


class Harness:
    def __init__(self, root, models=None, trees=None):
        self.state_path = Path(root) / "state" / "watch.json"
        self.models = models or []
        self.trees = trees or {}
        self.http_calls = []
        self.fail_http = False

    def http_get(self, url, headers=None, timeout=20):
        self.http_calls.append((url, headers or {}, timeout))
        if self.fail_http:
            raise OSError("network down")
        if url == watch.HF_MODELS_API:
            return FakeResponse(self.models)
        for model_id, tree in self.trees.items():
            if url == watch.tree_api_url(model_id):
                return FakeResponse(tree)
        raise AssertionError(f"unexpected URL {url}")

    def run(self):
        return watch.run(
            http_get=self.http_get,
            state_path=self.state_path,
            sleep_fn=lambda _: None,
        )


OFFICIAL_READY = {
    "id": "thinkingmachines/Inkling-Small",
    "private": False,
    "gated": False,
    "sha": "release-sha",
    "lastModified": "2026-08-01T12:00:00Z",
    "pipeline_tag": "image-text-to-text",
}
READY_TREE = [
    {"type": "file", "path": "README.md", "size": 1000},
    {"type": "file", "path": "model-00001-of-00002.safetensors", "size": 800_000_000},
    {"type": "file", "path": "model-00002-of-00002.safetensors", "size": 700_000_000},
]


class WatchdogTests(unittest.TestCase):
    def test_no_official_small_model_is_silent(self):
        with tempfile.TemporaryDirectory() as root:
            harness = Harness(root, models=[])
            self.assertEqual("", harness.run())
            self.assertEqual([watch.HF_MODELS_API], [call[0] for call in harness.http_calls])

    def test_community_model_is_ignored(self):
        with tempfile.TemporaryDirectory() as root:
            harness = Harness(
                root,
                models=[
                    {
                        "id": "community/Inkling-Small-GGUF",
                        "private": False,
                        "gated": False,
                    }
                ],
            )
            self.assertEqual("", harness.run())
            self.assertEqual(1, len(harness.http_calls))

    def test_gated_official_model_is_not_public_release(self):
        with tempfile.TemporaryDirectory() as root:
            model = {**OFFICIAL_READY, "gated": "auto"}
            harness = Harness(root, models=[model])
            self.assertEqual("", harness.run())
            self.assertEqual(1, len(harness.http_calls))

    def test_placeholder_repo_without_weight_bytes_is_silent(self):
        with tempfile.TemporaryDirectory() as root:
            model_id = str(OFFICIAL_READY["id"])
            harness = Harness(
                root,
                models=[OFFICIAL_READY],
                trees={model_id: [{"type": "file", "path": "README.md", "size": 1000}]},
            )
            self.assertEqual("", harness.run())
            state = json.loads(harness.state_path.read_text())
            self.assertEqual([model_id], state["candidate_ids"])
            self.assertNotIn("pending_signature", state)

    def test_public_official_weights_stage_a_release_review(self):
        with tempfile.TemporaryDirectory() as root:
            model_id = str(OFFICIAL_READY["id"])
            harness = Harness(root, models=[OFFICIAL_READY], trees={model_id: READY_TREE})
            payload = json.loads(harness.run())
            self.assertEqual("release_detected", payload["event"])
            self.assertEqual(model_id, payload["official_models"][0]["id"])
            self.assertEqual(1_500_000_000, payload["official_models"][0]["weight_bytes_seen"])
            self.assertIn("--ack", payload["ack_command"])

    def test_pending_release_repeats_until_review_is_acknowledged(self):
        with tempfile.TemporaryDirectory() as root:
            model_id = str(OFFICIAL_READY["id"])
            harness = Harness(root, models=[OFFICIAL_READY], trees={model_id: READY_TREE})
            first = json.loads(harness.run())
            second = json.loads(harness.run())
            self.assertEqual(first["signature"], second["signature"])
            self.assertTrue(watch.acknowledge(first["signature"], harness.state_path))
            self.assertEqual("", harness.run())
            state = json.loads(harness.state_path.read_text())
            self.assertTrue(state["ready_transition_notified"])

    def test_wrong_acknowledgement_does_not_silence_pending_release(self):
        with tempfile.TemporaryDirectory() as root:
            model_id = str(OFFICIAL_READY["id"])
            harness = Harness(root, models=[OFFICIAL_READY], trees={model_id: READY_TREE})
            first = json.loads(harness.run())
            self.assertFalse(watch.acknowledge("wrong-signature", harness.state_path))
            second = json.loads(harness.run())
            self.assertEqual(first["signature"], second["signature"])

    def test_three_failed_runs_emit_one_health_warning(self):
        with tempfile.TemporaryDirectory() as root:
            harness = Harness(root)
            harness.fail_http = True
            self.assertEqual("", harness.run())
            self.assertEqual("", harness.run())
            warning = json.loads(harness.run())
            self.assertEqual("health_warning", warning["event"])
            self.assertEqual("", harness.run())
            self.assertEqual(12, len(harness.http_calls))

    def test_state_is_atomic_private_and_contains_no_credentials(self):
        with tempfile.TemporaryDirectory() as root:
            harness = Harness(root, models=[])
            self.assertEqual("", harness.run())
            state = json.loads(harness.state_path.read_text())
            serialized = json.dumps(state).lower()
            self.assertNotIn("password", serialized)
            self.assertNotIn("cookie", serialized)
            self.assertNotIn("token", serialized)
            self.assertEqual([], list(harness.state_path.parent.glob("*.tmp")))
            self.assertEqual(0o600, harness.state_path.stat().st_mode & 0o777)


if __name__ == "__main__":
    unittest.main()
