from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import phase_adapters  # noqa: E402
import project_workspace  # noqa: E402
import run_phase  # noqa: E402


class FinalDeliveryRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="final-delivery-runner-")
        self.root = Path(self.temp.name).resolve()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def project(self, mode: str = "edge-tts") -> project_workspace.Project:
        metadata = {
            "schemaVersion": 2,
            "projectId": "fixture-project",
            "voiceoverMode": mode,
            "paths": dict(project_workspace.PROJECT_PATHS_V2),
            "renderProfile": dict(project_workspace.FIXED_RENDER_PROFILE),
        }
        plan = {
            "scenes": [
                {"sceneId": "scene-01", "outputFile": "scene-01.png"},
                {"sceneId": "scene-02", "outputFile": "scene-02.png"},
            ]
        }
        timing = {
            "scenes": [
                {"sceneId": "scene-01", "endFrameExclusive": 60},
                {"sceneId": "scene-02", "endFrameExclusive": 120},
            ]
        }
        return project_workspace.Project(self.root, metadata, plan, timing)

    def workspace(self) -> project_workspace.WorkspaceConfig:
        return project_workspace.WorkspaceConfig(
            root=self.root,
            config_path=self.root / "workspace.json",
            concurrency=project_workspace.ExecutionConcurrency(final_media_validation=3),
            video_encoding=project_workspace.ExecutionVideoEncoding(subtitle_preset="fast"),
        )

    def test_audio_delivery_runs_fixed_order_and_stops_at_human_gate(self) -> None:
        project = self.project()
        calls: list[str] = []
        inputs = [self.root / "scene-01.mp4", self.root / "scene-02.mp4"]

        with mock.patch.object(phase_adapters, "ordered_scene_inputs", return_value=inputs), mock.patch.object(
            phase_adapters,
            "assert_current_scene_review_approval",
            side_effect=lambda *args, **kwargs: calls.append("preflight") or {"approved": True},
        ), mock.patch.object(
            phase_adapters,
            "merge_project_scenes",
            side_effect=lambda *args, **kwargs: calls.append("merge") or {"ok": True},
        ), mock.patch.object(
            phase_adapters,
            "burn_project",
            side_effect=lambda *args, **kwargs: calls.append("burnSubtitles") or {"ok": True},
        ) as burn, mock.patch.object(
            phase_adapters,
            "mux_project",
            side_effect=lambda *args, **kwargs: calls.append("muxVoiceover") or {"ok": True},
        ), mock.patch.object(
            phase_adapters,
            "validate_project_final_media",
            side_effect=lambda *args, **kwargs: calls.append("validateFinalMedia")
            or {"ok": True, "finalIdentitySha256": "a" * 64, "finalApprovalWritten": False},
        ) as validate:
            result = phase_adapters.run_final_delivery(
                self.workspace(), project, run_id="delivery-fixture"
            )

        self.assertEqual(
            calls,
            ["preflight", "merge", "burnSubtitles", "muxVoiceover", "validateFinalMedia"],
        )
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["nextGate"], "final_media_review")
        self.assertEqual(result["currentIdentity"], "a" * 64)
        self.assertFalse(result["approvalWritten"])
        self.assertTrue(result["userConfirmationRequired"])
        self.assertEqual(result["lastCompletedStep"], "validateFinalMedia")
        self.assertEqual(
            set(result["timingsMs"]),
            {"preflight", "merge", "burnSubtitles", "muxVoiceover", "validateFinalMedia", "total"},
        )
        self.assertEqual(burn.call_args.kwargs["subtitle_preset"], "fast")
        self.assertEqual(validate.call_args.kwargs["configured_concurrency"], 3)

    def test_disabled_delivery_skips_mux(self) -> None:
        project = self.project("disabled")
        with mock.patch.object(
            phase_adapters, "ordered_scene_inputs", return_value=[self.root / "scene.mp4"]
        ), mock.patch.object(
            phase_adapters, "assert_current_scene_review_approval", return_value={"approved": True}
        ), mock.patch.object(
            phase_adapters, "merge_project_scenes", return_value={"ok": True}
        ), mock.patch.object(
            phase_adapters, "burn_project", return_value={"ok": True}
        ), mock.patch.object(
            phase_adapters, "mux_project"
        ) as mux, mock.patch.object(
            phase_adapters,
            "validate_project_final_media",
            return_value={"ok": True, "finalIdentitySha256": "b" * 64},
        ):
            result = phase_adapters.run_final_delivery(self.workspace(), project)

        mux.assert_not_called()
        self.assertEqual(result["outputs"]["muxVoiceover"]["reason"], "voiceover_disabled")
        self.assertEqual(result["taskCount"], 4)

    def test_failure_stops_following_steps_and_keeps_recovery_position(self) -> None:
        project = self.project()
        with mock.patch.object(
            phase_adapters, "ordered_scene_inputs", return_value=[self.root / "scene.mp4"]
        ), mock.patch.object(
            phase_adapters, "assert_current_scene_review_approval", return_value={"approved": True}
        ), mock.patch.object(
            phase_adapters, "merge_project_scenes", side_effect=RuntimeError("merge failed")
        ), mock.patch.object(phase_adapters, "burn_project") as burn, mock.patch.object(
            phase_adapters, "mux_project"
        ) as mux, mock.patch.object(
            phase_adapters, "validate_project_final_media"
        ) as validate:
            result = phase_adapters.run_final_delivery(self.workspace(), project)

        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(result["lastCompletedStep"], "preflight")
        self.assertEqual(result["failures"][0]["scope"], "merge")
        self.assertFalse(result["approvalWritten"])
        burn.assert_not_called()
        mux.assert_not_called()
        validate.assert_not_called()

    def test_outer_runner_projects_pass_to_waiting_human_gate(self) -> None:
        project = self.project()
        raw = {
            "status": "PASS",
            "runId": "delivery-fixture",
            "currentIdentity": "c" * 64,
            "nextGate": "final_media_review",
            "approvalWritten": False,
            "userConfirmationRequired": True,
            "failures": [],
            "lastCompletedStep": "validateFinalMedia",
            "timingsMs": {"total": 1234},
        }
        with mock.patch.object(run_phase, "load_workspace_config", return_value=self.workspace()), mock.patch.object(
            run_phase, "load_project", return_value=project
        ), mock.patch.dict(
            run_phase.PHASE_REGISTRY,
            {"final-delivery": mock.Mock(return_value=raw)},
        ):
            summary, code = run_phase.run_phase(
                project_path=self.root,
                phase="final-delivery",
                run_id="delivery-fixture",
            )

        self.assertEqual(code, run_phase.EXIT_HUMAN_GATE)
        self.assertEqual(summary["status"], "WAITING_HUMAN_GATE")
        self.assertEqual(summary["contractVersion"], "phase-runner-v2")
        self.assertEqual(summary["recovery"]["lastCompletedStep"], "validateFinalMedia")
        self.assertFalse(summary["approvalWritten"])


if __name__ == "__main__":
    unittest.main()
