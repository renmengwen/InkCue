from __future__ import annotations

import io
import json
import sys
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import project_workspace  # noqa: E402
import render_timing  # noqa: E402
import validate_annotations  # noqa: E402
from tests.test_annotation_batch import AnnotationBatchFixture  # noqa: E402


class AnnotationPrepareCLITests(AnnotationBatchFixture):
    def trusted_workspace(self):
        return replace(
            self.workspace(annotation_validation=3, agents=3),
            agents=project_workspace.ExecutionAgentConcurrency(
                default=3,
                annotation_drafting=3,
            ),
        )

    def run_prepare(self, project, workspace, run_id: str, *extra: str):
        stdout = io.StringIO()
        argv = [
            "prepare",
            "--project",
            str(project.root),
            "--run-id",
            run_id,
            *extra,
        ]
        with mock.patch.object(
            validate_annotations, "load_workspace_config", return_value=workspace
        ), redirect_stdout(stdout):
            exit_code = validate_annotations.main(argv)
        return exit_code, json.loads(stdout.getvalue())

    def test_prepare_nine_scenes_three_concurrency_and_existing_validate_consumes_root(self) -> None:
        project, _, _ = self.make_project(count=9)
        workspace = self.trusted_workspace()
        exit_code, summary = self.run_prepare(
            project,
            workspace,
            "cli-nine",
            "--images-confirmed",
        )
        self.assertEqual(exit_code, 0)
        self.assertEqual(summary["status"], "PASS")
        self.assertEqual(summary["contractVersion"], "whiteboard-annotation-prepare-v3")
        self.assertEqual(summary["runId"], "cli-nine")
        self.assertEqual(summary["taskCount"], 9)
        self.assertEqual(summary["dispatchUnitCount"], 3)
        self.assertEqual(summary["configuredAgentConcurrency"], 3)
        self.assertEqual(summary["preparationAudit"]["preparationMode"], "artifact_only")
        self.assertTrue(summary["dispatchPlan"]["coordinatorDispatchRequired"])
        self.assertEqual(summary["dispatchPlan"]["configuredMaxParallel"], 3)
        self.assertEqual(
            summary["dispatchPlan"]["granularity"],
            "contiguous-bundle-v1",
        )
        self.assertEqual(summary["dispatchPlan"]["maxTasksPerDispatchUnit"], 3)
        self.assertEqual(
            [unit["sceneIds"] for unit in summary["dispatchUnits"]],
            [
                ["scene-01", "scene-02", "scene-03"],
                ["scene-04", "scene-05", "scene-06"],
                ["scene-07", "scene-08", "scene-09"],
            ],
        )
        for unit in summary["dispatchUnits"]:
            self.assertEqual(unit["taskCount"], 3)
            self.assertEqual(len(unit["preparedTasks"]), 3)
            self.assertEqual(len(unit["candidateLintCommands"]), 3)
            self.assertTrue(unit["lintBeforeNextTask"])
            self.assertEqual(
                unit["payloadTooLargeRecovery"],
                "new-short-context-json-only",
            )
            self.assertTrue(all(item["preparedOnly"] for item in unit["preparedTasks"]))
            self.assertTrue(all(item["resultWriter"] == "coordinator" for item in unit["preparedTasks"]))
        self.assertEqual(
            [item["sceneId"] for item in summary["orderedTasks"]],
            [f"scene-{index:02d}" for index in range(1, 10)],
        )
        for item in summary["orderedTasks"]:
            self.assertTrue(Path(item["taskJsonPath"]).is_file())
            self.assertTrue(Path(item["roleContractPath"]).is_file())
            self.assertTrue(Path(item["allowedAttemptDir"]).is_dir())
            self.assertNotIn("spawnRequest", item)
            self.assertLess(
                len(Path(item["roleContractPath"]).read_text(encoding="utf-8").splitlines()),
                80,
            )
            self.assertTrue(item["candidateLint"]["requiredBeforeNextTask"])
            self.assertFalse(item["candidateLint"]["writesPerformed"])
            self.assertFalse(item["formalWritesAllowed"])
            self.assertFalse(item["approvalWritesAllowed"])
        for forbidden in ("spawnRequest", "effectiveAgentConcurrency", "dispatchAllowed"):
            self.assertNotIn(forbidden, json.dumps(summary, ensure_ascii=False))
        serialized = json.dumps(summary, ensure_ascii=False)
        for secret_marker in ("Authorization:", '"apiKey"', "sk-test-secret"):
            self.assertNotIn(secret_marker, serialized)
        self.assertFalse(any(project.scenes_dir.glob("*.annotation.json")))
        self.assertFalse((project.root / "manifests" / "annotation-review-approval.json").exists())

        dispatch_manifest = json.loads(
            Path(summary["dispatchManifestPath"]).read_text(encoding="utf-8")
        )
        self.assertTrue(dispatch_manifest["flags"]["LINT_CANDIDATE_BEFORE_NEXT_TASK"])
        self.assertTrue(dispatch_manifest["flags"]["REPAIR_SCHEMA_JSON_ONLY"])
        self.assertTrue(
            dispatch_manifest["flags"]["RESTART_SHORT_CONTEXT_AFTER_PAYLOAD_TOO_LARGE"]
        )

        context = render_timing.build_formal_validation_context(project)
        tasks = validate_annotations.load_annotation_tasks_from_candidate_root(
            workspace,
            project,
            Path(summary["candidateRoot"]),
            context=context,
        )
        for drafting in tasks:
            validate_annotations.record_coordinator_annotation_candidate(
                drafting,
                self.annotation(project, drafting.scene_id, context),
                project=project,
                context=context,
            )
        validate_stdout = io.StringIO()
        with mock.patch.object(
            validate_annotations, "load_workspace_config", return_value=workspace
        ), redirect_stdout(validate_stdout):
            validate_exit = validate_annotations.main(
                [
                    "--project",
                    str(project.root),
                    "--candidate-root",
                    summary["candidateRoot"],
                ]
            )
        validate_summary = json.loads(validate_stdout.getvalue())
        self.assertEqual(validate_exit, 0)
        self.assertEqual(validate_summary["status"], "PASS")
        self.assertEqual(validate_summary["publishedOrder"], [f"scene-{i:02d}" for i in range(1, 10)])

    def test_candidate_lint_rejects_element_level_protected_regions_without_writes(self) -> None:
        project, _, _ = self.make_project(count=1)
        workspace = self.trusted_workspace()
        exit_code, summary = self.run_prepare(
            project,
            workspace,
            "lint-one",
            "--images-confirmed",
        )
        self.assertEqual(exit_code, 0)
        candidate = Path(summary["orderedTasks"][0]["candidateAnnotationPath"])
        candidate.write_text(
            json.dumps(
                {
                    "contractVersion": "whiteboard-annotation-visual-elements-v1",
                    "elements": [
                        {
                            "sequence": 1,
                            "region": {"x": 10, "y": 20, "width": 200, "height": 180},
                            "protectedRegions": [],
                            "reveal": {"startMs": 0, "durationMs": 200},
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            lint_code = validate_annotations.main(
                ["lint", "--candidate", str(candidate)]
            )
        lint = json.loads(stdout.getvalue())
        self.assertEqual(lint_code, 2)
        self.assertEqual(lint["status"], "FAIL")
        self.assertIn("未知字段", lint["error"])
        self.assertFalse(any(project.scenes_dir.glob("*.annotation.json")))

    def test_prepare_refuses_missing_human_confirmation_without_writes(self) -> None:
        project, _, _ = self.make_project()
        workspace = self.trusted_workspace()
        exit_code, summary = self.run_prepare(project, workspace, "no-confirm")
        self.assertEqual(exit_code, 5)
        self.assertEqual(summary["error"]["code"], "missing_human_confirmation")
        self.assertFalse((project.root / ".work" / "no-confirm").exists())

    def test_prepare_missing_image_preflight_leaves_no_partial_tasks(self) -> None:
        project, _, _ = self.make_project(count=9)
        workspace = self.trusted_workspace()
        (project.scenes_dir / "scene-09.png").unlink()
        exit_code, summary = self.run_prepare(
            project, workspace, "missing-image", "--images-confirmed"
        )
        self.assertEqual(exit_code, 2)
        self.assertEqual(summary["error"]["code"], "invalid_input")
        self.assertIn("scene-09", summary["error"]["message"])
        self.assertFalse((project.root / ".work" / "missing-image").exists())

    def test_prepare_stale_binding_returns_five_without_writes(self) -> None:
        project, _, _ = self.make_project()
        workspace = self.trusted_workspace()
        stale_context = render_timing.build_formal_validation_context(project)
        project.timing_plan_path.write_text(
            project.timing_plan_path.read_text(encoding="utf-8") + "\n",
            encoding="utf-8",
        )
        stdout = io.StringIO()
        with mock.patch.object(
            validate_annotations, "load_workspace_config", return_value=workspace
        ), mock.patch.object(
            validate_annotations,
            "build_formal_validation_context",
            return_value=stale_context,
        ), redirect_stdout(stdout):
            exit_code = validate_annotations.main(
                [
                    "prepare",
                    "--project",
                    str(project.root),
                    "--run-id",
                    "stale-binding",
                    "--images-confirmed",
                ]
            )
        summary = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 5)
        self.assertEqual(summary["error"]["code"], "stale_binding")
        self.assertFalse((project.root / ".work" / "stale-binding").exists())

    def test_prepare_is_host_neutral_and_rejects_old_capacity_flags(self) -> None:
        project, _, _ = self.make_project(count=3)
        workspace = self.workspace(annotation_validation=3, agents=3)
        exit_code, summary = self.run_prepare(
            project,
            workspace,
            "fallback-bundle",
            "--images-confirmed",
        )
        self.assertEqual(exit_code, 0)
        self.assertEqual(summary["dispatchUnitCount"], 3)
        self.assertEqual(summary["dispatchPlan"]["granularity"], "contiguous-bundle-v1")
        self.assertEqual(summary["dispatchPlan"]["maxTasksPerDispatchUnit"], 3)
        self.assertEqual([unit["taskCount"] for unit in summary["dispatchUnits"]], [1, 1, 1])
        self.assertTrue(all(len(unit["preparedTasks"]) == 1 for unit in summary["dispatchUnits"]))

        legacy_exit, legacy_summary = self.run_prepare(
            project,
            workspace,
            "legacy-capacity-flags",
            "--images-confirmed",
            "--runtime-child-slots",
            "3",
        )
        self.assertEqual(legacy_exit, 2)
        self.assertEqual(legacy_summary["error"]["code"], "invalid_arguments")


if __name__ == "__main__":
    unittest.main()
