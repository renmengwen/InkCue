from __future__ import annotations

import copy
from dataclasses import replace
import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import agent_task_contract  # noqa: E402
import project_workspace  # noqa: E402
import render_timing  # noqa: E402
import validate_annotations  # noqa: E402
from tests.test_annotation_batch import AnnotationBatchFixture  # noqa: E402


class AnnotationAgentOrchestrationTests(AnnotationBatchFixture):
    def write_failed_result(self, drafting) -> None:
        task = drafting.task
        project_workspace.write_json_atomic(
            task.context.result_json,
            {
                "contractVersion": "whiteboard-agent-result-v1",
                "taskId": task.data["taskId"],
                "taskKind": task.data["taskKind"],
                "scopeKind": task.data["scopeKind"],
                "attempt": task.data["attempt"],
                "taskSha256": task.task_sha256,
                "roleContractVersion": task.data["roleContractVersion"],
                "roleContractSha256": task.data["roleContractSha256"],
                "sequence": task.data["sequence"],
                "status": "failed",
                "inspectedInputs": list(task.data["inputs"]),
                "outputs": [],
                "findings": [],
                "warnings": [],
                "error": {"category": "visual", "message": "本幕无法可靠标注"},
            },
        )

    def test_missing_runtime_capacity_uses_coordinator_fallback(self) -> None:
        project, _, _ = self.make_project()
        context = render_timing.build_formal_validation_context(project)
        tasks, audit = self.prepare(project, context)
        self.assertEqual(len(tasks), 3)
        self.assertFalse(audit["dispatchAllowed"])
        self.assertEqual(audit["effectiveAgentConcurrency"], 0)
        self.assertEqual(audit["peakChildAgents"], 0)
        self.assertEqual(audit["mode"], "coordinator_fallback")

    def test_host_collaboration_prepares_bounded_dispatch(self) -> None:
        project, _, _ = self.make_project()
        context = render_timing.build_formal_validation_context(project)
        workspace = self.workspace()
        configured = replace(
            workspace,
            agents=project_workspace.ExecutionAgentConcurrency(
                default=3,
                annotation_drafting=3,
            ),
        )
        tasks, audit = validate_annotations.prepare_annotation_drafting_tasks(
            configured,
            project,
            images_confirmed=True,
            context=context,
            coordinator_can_view=True,
            runtime_child_slots=2,
            coordinator_resource_budget=4,
            runtime_role_capabilities=(
                "readFiles",
                "viewImage",
                "writeCandidateJson",
            ),
        )
        self.assertEqual(len(tasks), 3)
        self.assertTrue(audit["dispatchAllowed"])
        self.assertEqual(audit["configuredAgentConcurrency"], 3)
        self.assertEqual(audit["effectiveAgentConcurrency"], 2)
        self.assertEqual(audit["peakChildAgents"], 0)
        self.assertEqual(audit["mode"], "host_collaboration_dispatch")
        self.assertEqual(audit["adapter"], "codex_collaboration")

    def test_image_confirmation_is_an_explicit_precondition(self) -> None:
        project, _, _ = self.make_project()
        context = render_timing.build_formal_validation_context(project)
        with self.assertRaisesRegex(
            validate_annotations.AnnotationBatchError, "线稿已获用户明确确认"
        ):
            validate_annotations.prepare_annotation_drafting_tasks(
                self.workspace(),
                project,
                images_confirmed=False,
                context=context,
                coordinator_can_view=True,
            )

    def test_frozen_role_contract_uses_visual_ink_clusters_and_flexible_element_counts(self) -> None:
        project, _, _ = self.make_project(count=1)
        context = render_timing.build_formal_validation_context(project)
        tasks, _ = self.prepare(project, context)
        frozen_role = tasks[0].task.role_contract_file
        source_role = ROOT / "references" / "annotation-drafting-role.md"
        self.assertEqual(frozen_role.read_bytes(), source_role.read_bytes())

        contract = frozen_role.read_text(encoding="utf-8")
        for required_rule in (
            "把叙事顺序和字幕语义映射到实际可见的视觉簇",
            "标注单元按视觉上连续的墨迹簇划分",
            "不按叙事名词或字幕概念数量拆分",
            "同一不可分割主体、共享背景或贯穿性连接结构必须合并",
            "一幕允许只有 1 个元素",
            "优先使用 2–3 个元素",
            "首版不得超过 3 个",
            "元素的 reveal 时间必须严格串行且不得重叠",
            "空间上的 `region` 不要求绝对没有交集",
            "任一矩形边界不得横穿另一个视觉簇的有效墨迹",
            "渲染器会从前一元素的允许掩码中扣除后续 region",
            "它不能替代正确分区",
            "sequence` 从 1 连续递增",
            "元素使用本幕局部毫秒时钟且彼此串行",
            "每个 `result.json` 使用 `whiteboard-agent-result-v1`（由 coordinator 生成）",
        ):
            self.assertIn(required_rule, contract)

    def test_formal_annotation_output_declaration_fails_closed(self) -> None:
        project, _, _ = self.make_project()
        context = render_timing.build_formal_validation_context(project)
        tasks, _ = self.prepare(project, context, count=1)
        drafting = tasks[0]
        data = copy.deepcopy(dict(drafting.task.data))
        data["allowedOutputs"][0] = "scenes/scene-01.annotation.json"
        project_workspace.write_json_atomic(drafting.task.context.task_json, data)
        with self.assertRaises(agent_task_contract.AgentContractError):
            agent_task_contract.validate_agent_task(
                drafting.task.context.task_json,
                drafting.task.context,
                expected_current_bindings=validate_annotations.context_bindings(
                    project, context
                ),
            )

    def test_stale_global_timing_refuses_candidate_and_wrong_role_contract_refuses_result(self) -> None:
        project, _, _ = self.make_project()
        context = render_timing.build_formal_validation_context(project)
        tasks, _ = self.prepare(project, context, count=2)
        timing_path = project.timing_plan_path
        timing_path.write_text(timing_path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
        with self.assertRaisesRegex(render_timing.RenderTimingError, "timing plan 已变化"):
            validate_annotations.record_coordinator_annotation_candidate(
                tasks[0],
                self.annotation(project, tasks[0].scene_id, context),
                project=project,
                context=context,
            )

        project_workspace.write_json_atomic(timing_path, project.timing_plan)
        fresh = project_workspace.load_project(project.root)
        fresh_context = render_timing.build_formal_validation_context(fresh)
        fresh_tasks, _ = validate_annotations.prepare_annotation_drafting_tasks(
            self.workspace(),
            fresh,
            images_confirmed=True,
            context=fresh_context,
            scene_ids=["scene-01"],
            coordinator_can_view=True,
        )
        role_contract = fresh_tasks[0].task.role_contract_file
        role_contract.write_text(
            role_contract.read_text(encoding="utf-8") + "\n篡改", encoding="utf-8"
        )
        with self.assertRaises(agent_task_contract.StaleAgentTaskError):
            validate_annotations.record_coordinator_annotation_candidate(
                fresh_tasks[0],
                self.annotation(fresh, "scene-01", fresh_context),
                project=fresh,
                context=fresh_context,
            )

    def test_completed_result_is_not_formal_or_approval_until_validator_publish(self) -> None:
        project, _, _ = self.make_project()
        context = render_timing.build_formal_validation_context(project)
        tasks, _ = self.prepare(project, context, count=1)
        drafting = tasks[0]
        validate_annotations.record_coordinator_annotation_candidate(
            drafting,
            self.annotation(project, drafting.scene_id, context),
            project=project,
            context=context,
        )
        self.assertFalse(drafting.formal_path.exists())
        summary = validate_annotations.validate_and_publish_annotation_batch(
            project,
            tasks,
            context=context,
            configured_concurrency=1,
        )
        self.assertTrue(drafting.formal_path.is_file())
        self.assertEqual(summary["status"], "FAIL")
        self.assertFalse(summary["globalAnnotationConfirmationWritten"])
        self.assertFalse(summary["fullPreviewStarted"])
        self.assertIsNone(summary["nextHumanGate"])

    def test_candidate_protected_region_out_of_canvas_fails_closed(self) -> None:
        project, _, _ = self.make_project()
        context = render_timing.build_formal_validation_context(project)
        tasks, _ = self.prepare(project, context, count=1)
        annotation = self.annotation(project, "scene-01", context)
        annotation["elements"][0]["reveal"]["protectedRegions"] = [
            {"x": 1910, "y": 0, "width": 20, "height": 20}
        ]
        with self.assertRaisesRegex(render_timing.RenderTimingError, "protectedRegions"):
            validate_annotations.record_coordinator_annotation_candidate(
                tasks[0],
                annotation,
                project=project,
                context=context,
            )

    def test_candidate_root_reloads_only_current_bound_annotation_tasks(self) -> None:
        project, _, _ = self.make_project()
        context = render_timing.build_formal_validation_context(project)
        tasks, _ = self.prepare(project, context)
        for drafting in tasks:
            validate_annotations.record_coordinator_annotation_candidate(
                drafting,
                self.annotation(project, drafting.scene_id, context),
                project=project,
                context=context,
            )
        candidate_root = tasks[0].task.context.task_dir.parents[1]
        loaded = validate_annotations.load_annotation_tasks_from_candidate_root(
            self.workspace(), project, candidate_root, context=context
        )
        self.assertEqual(
            [item.scene_id for item in loaded],
            ["scene-01", "scene-02", "scene-03"],
        )

    def test_retry_creates_only_failed_scene_new_attempt_and_keeps_plan_sequence(self) -> None:
        project, _, _ = self.make_project()
        context = render_timing.build_formal_validation_context(project)
        first, _ = validate_annotations.prepare_annotation_drafting_tasks(
            self.workspace(),
            project,
            images_confirmed=True,
            context=context,
            run_id="retry-run",
            coordinator_can_view=True,
        )
        for drafting in (first[0], first[2]):
            validate_annotations.record_coordinator_annotation_candidate(
                drafting,
                self.annotation(project, drafting.scene_id, context),
                project=project,
                context=context,
            )
        retry, _ = validate_annotations.prepare_annotation_drafting_tasks(
            self.workspace(),
            project,
            images_confirmed=True,
            context=context,
            scene_ids=["scene-02"],
            run_id="retry-run",
            coordinator_can_view=True,
            attempt_by_scene={"scene-02": 2},
            retry_status_by_scene={"scene-02": "failed"},
        )
        self.assertEqual(retry[0].task.data["attempt"], 2)
        self.assertEqual(retry[0].sequence, 2)
        validate_annotations.record_coordinator_annotation_candidate(
            retry[0],
            self.annotation(project, "scene-02", context),
            project=project,
            context=context,
        )
        loaded = validate_annotations.load_annotation_tasks_from_candidate_root(
            self.workspace(),
            project,
            first[0].task.context.task_dir.parents[1],
            context=context,
        )
        self.assertEqual([item.task.data["attempt"] for item in loaded], [1, 2, 1])
        with self.assertRaisesRegex(
            validate_annotations.AnnotationBatchError, "failed/cancelled/stale"
        ):
            validate_annotations.prepare_annotation_drafting_tasks(
                self.workspace(),
                project,
                images_confirmed=True,
                context=context,
                scene_ids=["scene-01"],
                run_id="retry-run",
                coordinator_can_view=True,
                attempt_by_scene={"scene-01": 2},
                retry_status_by_scene={"scene-01": "completed"},
            )

    def test_coordinator_materializes_bindings_and_ignores_legacy_header_copy_errors(self) -> None:
        project, _, _ = self.make_project(count=1)
        context = render_timing.build_formal_validation_context(project)
        tasks, _ = self.prepare(project, context)
        authored = self.annotation(project, "scene-01", context)
        authored["sceneId"] = "scene-08"
        authored["sceneDurationMs"] = 999999
        authored["timingPlanSha256"] = "0" * 64
        authored["timingSource"]["timelineSha256"] = "f" * 64
        self.write_unvalidated_completed_result(tasks[0], authored)

        summary = validate_annotations.validate_and_publish_annotation_batch(
            project,
            tasks,
            context=context,
        )
        self.assertEqual(summary["status"], "PASS")
        materialized = json.loads(tasks[0].materialized_path.read_text(encoding="utf-8"))
        formal = json.loads(tasks[0].formal_path.read_text(encoding="utf-8"))
        self.assertEqual(materialized, formal)
        self.assertEqual(formal["sceneId"], "scene-01")
        self.assertEqual(formal["sceneDurationMs"], project.timing_plan["scenes"][0]["sceneDurationMs"])
        self.assertEqual(formal["timingPlanSha256"], context.timing_plan_sha256)
        self.assertEqual(formal["timingSource"]["timelineSha256"], context.active_timeline["sha256"])
        self.assertEqual(formal["elements"], authored["elements"])

    def test_bundle_tasks_keep_mixed_results_independent_and_missing_result_fails_only_scene(self) -> None:
        project, _, _ = self.make_project(count=3)
        context = render_timing.build_formal_validation_context(project)
        tasks, _ = self.prepare(project, context)
        for drafting in (tasks[0], tasks[2]):
            validate_annotations.record_coordinator_annotation_candidate(
                drafting,
                {"contractVersion": "whiteboard-annotation-visual-elements-v1", "elements": self.annotation(project, drafting.scene_id, context)["elements"]},
                project=project,
                context=context,
            )
        summary = validate_annotations.validate_and_publish_annotation_batch(
            project,
            tasks,
            context=context,
        )
        self.assertEqual(summary["status"], "FAIL")
        self.assertEqual(summary["publishedOrder"], ["scene-01", "scene-03"])
        self.assertEqual([item["status"] for item in summary["scenes"]], ["published_current_technical", "failed", "published_current_technical"])

        retry, _ = validate_annotations.prepare_annotation_drafting_tasks(
            self.workspace(),
            project,
            images_confirmed=True,
            context=context,
            scene_ids=["scene-02"],
            run_id=tasks[0].task.context.run_id,
            coordinator_can_view=True,
            attempt_by_scene={"scene-02": 2},
            retry_status_by_scene={"scene-02": "failed"},
        )
        self.assertEqual([(item.scene_id, item.task.data["attempt"]) for item in retry], [("scene-02", 2)])

        retry[0].candidate_path.write_text(
            json.dumps({"contractVersion": "whiteboard-annotation-visual-elements-v1", "elements": self.annotation(project, "scene-02", context)["elements"]}),
            encoding="utf-8",
        )
        missing = validate_annotations.validate_and_publish_annotation_batch(
            project,
            retry,
            context=context,
        )
        self.assertEqual(missing["status"], "PASS")
        self.assertEqual(missing["scenes"][0]["status"], "published_current_technical")


if __name__ == "__main__":
    unittest.main()
