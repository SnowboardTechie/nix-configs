import json
import unittest
from pathlib import Path


JOB_PATH = Path(__file__).with_name("colibri-glm52-readiness-watch.job.json")


class ColibriReadinessWatchContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.job = json.loads(JOB_PATH.read_text())
        cls.prompt = cls.job["prompt"]

    def test_uses_research_capable_cloud_model(self):
        self.assertEqual(self.job["model"], "gpt-5.6-terra")
        self.assertEqual(self.job["provider"], "openai-codex")
        self.assertIsNone(self.job["base_url"])

    def test_distinguishes_dense_from_routed_expert_metal_support(self):
        self.assertIn("coli_metal_moe_submit", self.prompt)
        self.assertIn("dense fmt=4 gemv/gemm support alone does not prove", self.prompt.lower())
        self.assertIn("matmul_qt_ex", self.prompt)
        self.assertIn("metal_fused_fmt_ok", self.prompt)
        self.assertIn("routed fmt=6 expert support alone does not pass", self.prompt)
        self.assertIn("#813", self.prompt)

    def test_requires_exact_deployable_artifact_and_target_hardware(self):
        self.assertIn("base FP8 repository is not itself a deployable", self.prompt)
        self.assertIn("exact Hugging Face model ID and commit", self.prompt)
        self.assertIn("M2 Ultra Mac Studio benchmark with 128 GB", self.prompt)
        self.assertIn("M5 Max laptop correctness tests are not", self.prompt)

    def test_requires_real_model_tool_call_and_state_before_alert(self):
        self.assertIn("exact quantized checkpoint through the exact new server", self.prompt)
        self.assertIn("Mock-engine/parser tests alone do not pass", self.prompt)
        self.assertIn("completed within 180 seconds", self.prompt)
        self.assertIn("before** emitting the deployment-candidate alert", self.prompt)
        self.assertIn("before** emitting the retest-ready alert", self.prompt)
        self.assertIn("either state write fails, respond only `[SILENT]`", self.prompt)

    def test_reports_retest_and_deployment_as_separate_transitions(self):
        self.assertIn("Stage A — retest-ready", self.prompt)
        self.assertIn("does not require a new M2 Ultra benchmark", self.prompt)
        self.assertIn("Stage B — deployment-candidate", self.prompt)
        self.assertIn("retest_signature", self.prompt)
        self.assertIn("deployment_signature", self.prompt)
        self.assertIn("at least 1.0 sustained decode tok/s", self.prompt)
        self.assertIn("this is not deployment readiness", self.prompt)


if __name__ == "__main__":
    unittest.main()
