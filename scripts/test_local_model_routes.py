from __future__ import annotations

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent


class CronModelRouteTests(unittest.TestCase):
    def test_every_tracked_job_is_codex_or_model_free(self) -> None:
        for path in ROOT.glob("*.job.json"):
            job = json.loads(path.read_text(encoding="utf-8"))
            with self.subTest(filename=path.name):
                if job.get("no_agent"):
                    self.assertNotIn("model", job)
                    self.assertNotIn("provider", job)
                    self.assertNotIn("base_url", job)
                else:
                    self.assertTrue(job["model"].startswith("gpt-"))
                    self.assertEqual(job["provider"], "openai-codex")
                    self.assertIsNone(job["base_url"])

    def test_agent_watchdogs_use_the_codex_subscription(self) -> None:
        filename = "github-status-watch.job.json"
        job = json.loads((ROOT / filename).read_text(encoding="utf-8"))
        self.assertEqual(job["model"], "gpt-5.6-terra", filename)
        self.assertEqual(job["provider"], "openai-codex", filename)
        self.assertIsNone(job["base_url"], filename)

    def test_source_heavy_watchdog_uses_codex_subscription(self) -> None:
        filename = "inkling-small-release-watch.job.json"
        job = json.loads((ROOT / filename).read_text(encoding="utf-8"))
        self.assertEqual(job["model"], "gpt-5.6-terra", filename)
        self.assertEqual(job["provider"], "openai-codex", filename)
        self.assertIsNone(job["base_url"], filename)


if __name__ == "__main__":
    unittest.main()
