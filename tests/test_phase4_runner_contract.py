from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
TESTS = ROOT / "tests"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
if str(TESTS) not in sys.path:
    sys.path.insert(0, str(TESTS))

from test_annotation_batch import AnnotationBatchFixture

import annotation_review  # noqa: E402
import phase_adapters  # noqa: E402
import project_workspace  # noqa: E402
import render_timing  # noqa: E402
import generate_annotation_previews  # noqa: E402
import run_phase  # noqa: E402
from contextlib import redirect_stdout  # noqa: E402
from io import StringIO  # noqa: E402
from unittest import mock  # noqa: E402


class Phase4RunnerContractTests(AnnotationBatchFixture):
    """黑盒锁定 Phase 4 adapter/runner 的 Gate 和 receipt 合同。

    这些测试只使用本地 fixture 和现有 annotation API；不创建 provider client，
    也不调用 approval writer。runner CLI 可复用同一组摘要字段进行等价性回归。
    """

    def _workspace(self):
        return project_workspace.WorkspaceConfig(
            root=self.workspace_root,
            config_path=self.workspace_root / "fixture-config.json",
            concurrency=project_workspace.ExecutionConcurrency(annotation_preview=2),
        )

    def _current_project(self):
        project, _audio, _identity = self.make_project(2)
        context = render_timing.build_formal_validation_context(project)
        for scene in project.plan["scenes"]:
            value = self.annotation(project, scene["sceneId"], context)
            element = value["elements"][0]
            element.update(
                {
                    "label": f"{scene['sceneId']} 主体",
                    "narrativeRole": "主体出现",
                    "subtitle": f"{scene['sceneId']} 字幕",
                    "handPath": {"start": [20, 30], "end": [200, 180]},
                }
            )
            element["reveal"]["direction"] = "left-to-right"
            project_workspace.write_json_atomic(
                project.root / "scenes" / f"{scene['sceneId']}.annotation.json", value
            )
        return project_workspace.load_project(project.root)

    def test_annotation_preview_stops_at_gate_without_approval(self):
        project = self._current_project()
        summary = phase_adapters.run_annotation_preview(self._workspace(), project)

        self.assertEqual(summary["status"], "PASS")
        self.assertEqual(summary["projectId"], project.project_id)
        self.assertTrue(summary["runId"])
        self.assertEqual(summary["taskCount"], 2)
        self.assertIn("currentIdentity", summary)
        self.assertTrue(summary["userConfirmationRequired"])
        self.assertTrue(summary["confirmationRequest"])
        self.assertEqual(summary["nextGate"], "annotation_review_confirmation")
        self.assertFalse(summary["approvalWritten"])
        self.assertFalse((project.root / annotation_review.APPROVAL_FILE).exists())
        self.assertTrue(summary["artifact"] or summary["artifactPaths"])

    def test_current_formal_receipt_is_reused_without_deep_validation(self):
        project = self._current_project()
        first = phase_adapters.run_annotation_preview(self._workspace(), project)
        receipt = first["formalValidationReceipt"]
        self.assertIsInstance(receipt, str)
        receipt_path = project.root / receipt
        self.assertTrue(receipt_path.is_file())

        second = phase_adapters.run_annotation_preview(
            self._workspace(),
            project_workspace.load_project(project.root),
            run_id=first["runId"],
            formal_context_receipt=receipt_path,
        )
        self.assertEqual(second["formalValidationMode"], "binding")
        self.assertTrue(second["deepValidationSkipped"])
        self.assertTrue(second["deepValidationReused"])
        self.assertIn("current binding", second["deepValidationBasis"])
        self.assertEqual(second["currentIdentity"], first["currentIdentity"])
        self.assertFalse(second["approvalWritten"])

    def test_binding_change_fails_closed_and_does_not_reuse_old_approval(self):
        project = self._current_project()
        first = phase_adapters.run_annotation_preview(self._workspace(), project)
        receipt_path = project.root / first["formalValidationReceipt"]

        annotation_path = project.root / "scenes" / "scene-01.annotation.json"
        value = json.loads(annotation_path.read_text(encoding="utf-8"))
        value["elements"][0]["label"] = "已变更主体"
        project_workspace.write_json_atomic(annotation_path, value)

        with self.assertRaises((render_timing.RenderTimingError, ValueError)):
            phase_adapters.run_annotation_preview(
                self._workspace(),
                project_workspace.load_project(project.root),
                run_id=first["runId"],
                formal_context_receipt=receipt_path,
            )
        self.assertFalse((project.root / annotation_review.APPROVAL_FILE).exists())

    def test_receipt_requires_matching_run_id(self):
        project = self._current_project()
        first = phase_adapters.run_annotation_preview(self._workspace(), project)
        receipt_path = project.root / first["formalValidationReceipt"]

        with self.assertRaises((phase_adapters.PhaseAdapterError, render_timing.RenderTimingError)):
            phase_adapters.run_annotation_preview(
                self._workspace(),
                project,
                run_id="different-run",
                formal_context_receipt=receipt_path,
            )

    def test_runner_adapter_and_step_cli_keep_identity_and_manifest_equivalent(self):
        """同一 current project 上，逐步 preview 与 adapter 的正式产物保持等价。"""
        project = self._current_project()
        context = render_timing.build_formal_validation_context(project)
        step = generate_annotation_previews.generate_annotation_preview_batch(
            self._workspace(), project, context=context
        )
        adapter = phase_adapters.run_annotation_preview(
            self._workspace(), project_workspace.load_project(project.root)
        )
        self.assertEqual(
            adapter["annotationReviewIdentitySha256"],
            step["annotationReviewIdentitySha256"],
        )
        self.assertEqual(adapter["contactSheetSha256"], step["contactSheetSha256"])
        self.assertEqual(adapter["publishedOrder"], step["publishedOrder"])
        self.assertEqual(
            json.loads((project.root / "planning" / "timing-plan.json").read_text(encoding="utf-8")),
            project_workspace.load_project(project.root).timing_plan,
        )
        self.assertFalse(adapter["approvalWritten"])

    def test_runner_cli_emits_waiting_gate_json_and_process_success(self):
        project = self._current_project()
        output = StringIO()
        with mock.patch.object(run_phase, "load_workspace_config", return_value=self._workspace()), mock.patch.object(
            run_phase, "load_project", return_value=project
        ), redirect_stdout(output):
            code = run_phase.main(
                ["--project", str(project.root), "--phase", "annotation-preview"]
            )
        self.assertEqual(code, run_phase.EXIT_HUMAN_GATE)
        self.assertEqual(code, 0)
        summary = json.loads(output.getvalue())
        self.assertEqual(summary["contractVersion"], "phase-runner-v3")
        self.assertEqual(summary["status"], "WAITING_HUMAN_GATE")
        self.assertEqual(summary["technicalStatus"], "PASS")
        self.assertEqual(summary["processOutcome"], "completed_waiting_for_user")
        self.assertEqual(summary["nextGate"], "annotation_review_confirmation")
        self.assertFalse(summary["approvalWritten"])
        self.assertTrue(summary["userConfirmationRequired"])
        self.assertIn("resumeCommand", summary["recovery"])

    def test_unknown_phase_fails_with_stable_contract_and_no_approval(self):
        summary, code = run_phase.run_phase(
            project_path=self.project_root,
            phase="not-a-real-phase",
        )
        self.assertEqual(code, run_phase.EXIT_INVALID_OR_TECHNICAL)
        self.assertEqual(summary["status"], "FAIL")
        self.assertEqual(summary["error"]["code"], "unknown_phase")
        self.assertFalse(summary["approvalWritten"])


if __name__ == "__main__":
    import unittest

    unittest.main()
