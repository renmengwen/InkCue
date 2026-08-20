from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class CiStatusContractTests(unittest.TestCase):
    def test_readme_separates_fixture_provider_and_human_gate_status(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("fixture 的通过不能冒充真实 provider、真实媒体或人工验收通过", readme)
        self.assertIn("真实 provider 未执行时必须另报 `SKIP`", readme)
        self.assertIn("外部条件不可用则报 `BLOCKED`", readme)
        self.assertIn("人工 Gate 必须保留为“待确认”且 `approvalWritten=false`", readme)
        self.assertIn(
            "benchmarks\\run_scene_render_benchmark.py --fixture fixture-medium",
            readme,
        )


if __name__ == "__main__":
    unittest.main()
