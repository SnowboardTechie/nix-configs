from __future__ import annotations

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent


class LocalModelRouteTests(unittest.TestCase):
    def test_gemma_watchdogs_use_the_tuned_named_provider(self) -> None:
        for filename in (
            "inkling-small-release-watch.job.json",
            "github-status-watch.job.json",
        ):
            job = json.loads((ROOT / filename).read_text(encoding="utf-8"))
            self.assertEqual(job["model"], "gemma4:31b-mlx", filename)
            self.assertEqual(job["provider"], "custom:local-gemma4", filename)
            self.assertEqual(job["base_url"], "http://127.0.0.1:11434/v1", filename)


if __name__ == "__main__":
    unittest.main()
