from __future__ import annotations

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent


class LocalModelRouteTests(unittest.TestCase):
    def test_gemma_watchdogs_use_the_tuned_named_provider(self) -> None:
        filename = "github-status-watch.job.json"
        job = json.loads((ROOT / filename).read_text(encoding="utf-8"))
        self.assertEqual(job["model"], "gemma4:31b-mlx", filename)
        self.assertEqual(job["provider"], "custom:local-gemma4", filename)
        self.assertEqual(job["base_url"], "http://127.0.0.1:11434/v1", filename)

    def test_source_heavy_watchdog_uses_codex_subscription(self) -> None:
        filename = "inkling-small-release-watch.job.json"
        job = json.loads((ROOT / filename).read_text(encoding="utf-8"))
        self.assertEqual(job["model"], "gpt-5.6-terra", filename)
        self.assertEqual(job["provider"], "openai-codex", filename)
        self.assertIsNone(job["base_url"], filename)


if __name__ == "__main__":
    unittest.main()
