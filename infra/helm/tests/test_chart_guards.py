"""Reject configuration regressions that formerly passed a schema-only Helm check."""

import os
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


class ChartGuards(unittest.TestCase):
    def render(self, *overrides):
        return subprocess.run(
            [
                os.environ.get("HELM_BIN", "helm"),
                "template",
                "lostlink",
                str(ROOT / "infra/helm/lostlink"),
                "-n",
                "lostlink",
                "-f",
                str(ROOT / "infra/helm/tests/values-ci.yaml"),
                *overrides,
            ],
            text=True,
            capture_output=True,
            check=False,
        )

    def test_reject_weak_tls_even_if_connection_would_be_encrypted(self):
        for mode in ["require", "verify-ca", "disable", "prefer"]:
            with self.subTest(mode=mode):
                result = self.render("--set", f"keda.sslmode={mode}")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("keda.sslmode must be verify-full", result.stderr)

    def test_reserve_memory_for_both_model_processes(self):
        for prefix in ["services.matching.api", "services.matching.workers.tasks"]:
            for kind in ["requests", "limits"]:
                with self.subTest(process=prefix, kind=kind):
                    result = self.render("--set", f"{prefix}.resources.{kind}.memory=1536Mi")
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("must be at least 4Gi", result.stderr)

    def test_mib_equivalent_is_valid(self):
        result = self.render("--set", "services.matching.api.resources.requests.memory=4096Mi")
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
