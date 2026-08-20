from __future__ import annotations

import json
import sys
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
TESTS = ROOT / "tests"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
if str(TESTS) not in sys.path:
    sys.path.insert(0, str(TESTS))

import project_workspace  # noqa: E402
import render_stream_whiteboard  # noqa: E402
import render_timing  # noqa: E402
import test_render_timing as render_fixture  # noqa: E402


class SceneRenderSafetyMatrixTests(unittest.TestCase):
    """正式 scene batch 的并发审计、顺序提交和失败恢复矩阵。"""

    def setUp(self) -> None:
        self.fixture = render_fixture.RenderTimingTests("runTest")
        self.fixture.setUp()
        self.project = self.fixture._project()

    def tearDown(self) -> None:
        self.fixture.tearDown()

    @staticmethod
    def _thread_executor(*, max_workers: int):
        return ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="scene-render-safety-matrix",
        )

    def _args(self):
        return render_stream_whiteboard._parse_args(
            ["--project", str(self.project.root), "--all"]
        )

    @staticmethod
    def _candidate_bytes(scene_id: str) -> bytes:
        return f"deterministic-candidate-{scene_id}".encode("ascii")

    @staticmethod
    def _media_binding(path: Path) -> dict:
        return {
            "sha256": project_workspace.sha256_file(path),
            "bytes": path.stat().st_size,
            "validation": {
                "deepReceipt": {
                    "contractVersion": "scene-render-safety-fixture-v1",
                }
            },
        }

    def _run_controlled_batch(
        self,
        configured: int,
        *,
        failed_scene_ids: set[str] | None = None,
    ) -> tuple[dict, dict]:
        """让 worker 完成顺序可控，同时保留正式 coordinator 发布路径。"""

        failed_scene_ids = failed_scene_ids or set()
        contexts = render_timing.resolve_formal_scenes(
            self.project,
            [item["sceneId"] for item in self.project.plan["scenes"]],
        )
        plan_order = [context.scene_id for context in contexts]
        evidence = {
            "executorCalls": [],
            "completionOrder": [],
            "publishOrder": [],
        }

        def execute(tasks, *, max_workers, **_kwargs):
            evidence["executorCalls"].append(
                {"taskCount": len(tasks), "maxWorkers": max_workers}
            )
            completed: list[dict] = []
            # 明确制造与 generation plan 相反的完成/插入顺序。
            for task in reversed(tasks):
                scene_id = task["sceneId"]
                evidence["completionOrder"].append(scene_id)
                if scene_id in failed_scene_ids:
                    completed.append(
                        {
                            "sceneId": scene_id,
                            "candidatePath": task["candidatePath"],
                            "status": "failed",
                            "stage": "worker_process",
                            "errorType": "FixtureWorkerError",
                            "error": "受控 worker 失败",
                            "exitCode": 4,
                        }
                    )
                    continue
                candidate = Path(task["candidatePath"])
                candidate.write_bytes(self._candidate_bytes(scene_id))
                completed.append(
                    {
                        "sceneId": scene_id,
                        "candidatePath": str(candidate),
                        "status": "succeeded",
                        "deepReceipt": {
                            "contractVersion": "scene-render-safety-fixture-v1",
                        },
                        "startedNs": 1,
                        "finishedNs": 2,
                    }
                )
            peak = min(max_workers, len(tasks))
            return {item["sceneId"]: item for item in completed}, peak

        def publish(candidate: Path, destination: Path, **_kwargs):
            evidence["publishOrder"].append(destination.name)
            destination.write_bytes(candidate.read_bytes())
            candidate.unlink()
            return self._media_binding(destination)

        def render_serial(args, context, frozen, cfg, **kwargs):
            del frozen, kwargs
            evidence["completionOrder"].append(context.scene_id)
            context.output_path.write_bytes(self._candidate_bytes(context.scene_id))
            evidence["publishOrder"].append(context.output_path.name)
            render_options = render_stream_whiteboard._formal_render_options(
                args,
                cfg,
                hand_sha256=None,
            )
            manifest = render_timing.update_render_manifest(
                context,
                media=self._media_binding(context.output_path),
                render_options=render_options,
            )
            return (
                context.output_path,
                manifest["scenes"][context.scene_id]["renderIdentityHash"],
            )

        workspace_config = mock.Mock()
        workspace_config.for_stage.return_value = configured
        with mock.patch.object(
            render_stream_whiteboard.annotation_review,
            "require_current_annotation_review_approval",
            return_value={"approved": True, "identityHash": "fixture"},
        ), mock.patch.object(
            project_workspace,
            "load_workspace_config",
            return_value=workspace_config,
        ), mock.patch.object(
            render_stream_whiteboard,
            "_load_formal_hand",
            return_value=(None, None, None),
        ), mock.patch.object(
            render_stream_whiteboard,
            "_execute_formal_candidate_tasks",
            side_effect=execute,
        ), mock.patch.object(
            render_stream_whiteboard,
            "_publish_and_bind_scene",
            side_effect=publish,
        ), mock.patch.object(
            render_stream_whiteboard,
            "_render_formal_context",
            side_effect=render_serial,
        ):
            result = render_stream_whiteboard._run_formal_batch(self._args())

        self.assertEqual(result["sceneOrder"], plan_order)
        self.assertEqual(
            [item["sceneId"] for item in result["results"]],
            plan_order,
        )
        return result, evidence

    def test_scheduler_peak_is_bounded_for_scene_render_one_two_and_four(self) -> None:
        tasks = [{"sceneId": f"scene-{index:02d}"} for index in range(1, 7)]

        for configured in (1, 2, 4):
            with self.subTest(configured=configured):
                expected_peak = min(configured, len(tasks))
                first_wave_started = threading.Event()
                release = threading.Event()
                lock = threading.Lock()
                active = 0
                observed_peak = 0

                def worker(task: dict) -> dict:
                    nonlocal active, observed_peak
                    started_ns = time.time_ns()
                    with lock:
                        active += 1
                        observed_peak = max(observed_peak, active)
                        if active == expected_peak:
                            first_wave_started.set()
                    if not release.wait(5):
                        raise AssertionError("受控 worker 未被释放")
                    with lock:
                        active -= 1
                    return {
                        "sceneId": task["sceneId"],
                        "candidatePath": task["sceneId"],
                        "status": "succeeded",
                        "startedNs": started_ns,
                        "finishedNs": time.time_ns(),
                    }

                call_result: dict[str, object] = {}

                def run_scheduler() -> None:
                    call_result["value"] = (
                        render_stream_whiteboard._execute_formal_candidate_tasks(
                            tasks,
                            max_workers=configured,
                            worker=worker,
                            executor_factory=self._thread_executor,
                        )
                    )

                coordinator = threading.Thread(
                    target=run_scheduler,
                    name=f"scene-render-coordinator-{configured}",
                )
                coordinator.start()
                self.assertTrue(
                    first_wave_started.wait(5),
                    f"sceneRender={configured} 未填满第一波有界 worker",
                )
                with lock:
                    self.assertEqual(active, expected_peak)
                release.set()
                coordinator.join(10)
                self.assertFalse(coordinator.is_alive(), "受控调度未结束")

                results, reported_peak = call_result["value"]
                self.assertEqual(set(results), {task["sceneId"] for task in tasks})
                self.assertEqual(observed_peak, expected_peak)
                self.assertLessEqual(reported_peak, configured)
                self.assertLessEqual(observed_peak, configured)

    def test_concurrency_only_changes_runtime_audit_not_identity_or_output_sha(self) -> None:
        expected_order = ["scene-01", "scene-02"]
        identity_snapshots: list[dict[str, str]] = []
        sha_snapshots: list[dict[str, str]] = []
        audit_snapshots: list[tuple[int, int, int]] = []

        for configured in (1, 2, 4):
            result, evidence = self._run_controlled_batch(configured)
            self.assertEqual(result["status"], "PASS")
            self.assertFalse(result["partialSuccess"])
            self.assertFalse(result["approvalWritten"])
            self.assertTrue(result["userConfirmationRequired"])
            self.assertEqual(result["configured"], configured)
            self.assertEqual(result["configuredSceneRenderConcurrency"], configured)
            self.assertLessEqual(result["effective"], configured)
            self.assertLessEqual(result["peak"], result["effective"])
            self.assertLessEqual(result["peakSceneRenderWorkers"], configured)
            self.assertEqual(evidence["publishOrder"], [
                "scene-01-whiteboard.mp4",
                "scene-02-whiteboard.mp4",
            ])
            if configured > 1:
                self.assertEqual(evidence["completionOrder"], list(reversed(expected_order)))
                self.assertEqual(
                    evidence["executorCalls"],
                    [{"taskCount": 2, "maxWorkers": min(configured, 2)}],
                )
            else:
                self.assertEqual(evidence["completionOrder"], expected_order)
                self.assertEqual(evidence["executorCalls"], [])

            manifest = json.loads(
                self.project.path(render_timing.RENDER_MANIFEST_FILE).read_text(
                    encoding="utf-8"
                )
            )
            identity_snapshots.append(
                {
                    scene_id: manifest["scenes"][scene_id]["renderIdentityHash"]
                    for scene_id in expected_order
                }
            )
            sha_snapshots.append(
                {
                    scene_id: project_workspace.sha256_file(
                        self.project.path(f"scenes/{scene_id}-whiteboard.mp4")
                    )
                    for scene_id in expected_order
                }
            )
            audit_snapshots.append(
                (result["configured"], result["effective"], result["peak"])
            )

        self.assertTrue(
            all(snapshot == identity_snapshots[0] for snapshot in identity_snapshots[1:])
        )
        self.assertTrue(all(snapshot == sha_snapshots[0] for snapshot in sha_snapshots[1:]))
        self.assertEqual(audit_snapshots, [(1, 1, 1), (2, 2, 2), (4, 2, 2)])

    def test_worker_failure_preserves_old_current_and_partial_batch_stays_failed(self) -> None:
        contexts = render_timing.resolve_formal_scenes(
            self.project,
            [item["sceneId"] for item in self.project.plan["scenes"]],
        )
        old_identities: dict[str, str] = {}
        for context in contexts:
            context.output_path.write_bytes(f"old-current-{context.scene_id}".encode("ascii"))
            manifest = render_timing.update_render_manifest(
                context,
                media=self._media_binding(context.output_path),
                render_options={"fixture": "old-current"},
            )
            old_identities[context.scene_id] = manifest["scenes"][context.scene_id][
                "renderIdentityHash"
            ]

        manifest_path = self.project.path(render_timing.RENDER_MANIFEST_FILE)
        old_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        old_manifest["sceneReviewApproval"] = {
            "identityHash": "stale-old-review",
            "approved": True,
        }
        project_workspace.write_json_atomic(manifest_path, old_manifest)

        result, evidence = self._run_controlled_batch(
            4,
            failed_scene_ids={"scene-01"},
        )

        self.assertEqual(result["status"], "FAIL")
        self.assertTrue(result["partialSuccess"])
        self.assertEqual(result["successCount"], 1)
        self.assertEqual(result["failureCount"], 1)
        self.assertFalse(result["approvalWritten"])
        self.assertTrue(result["userConfirmationRequired"])
        self.assertEqual(result["configured"], 4)
        self.assertLessEqual(result["effective"], 4)
        self.assertLessEqual(result["peak"], result["effective"])
        self.assertEqual(
            [item["status"] for item in result["results"]],
            ["failed", "published_current_technical"],
        )
        self.assertEqual(evidence["completionOrder"], ["scene-02", "scene-01"])
        self.assertEqual(evidence["publishOrder"], ["scene-02-whiteboard.mp4"])
        self.assertEqual(
            contexts[0].output_path.read_bytes(),
            b"old-current-scene-01",
        )
        self.assertEqual(
            contexts[1].output_path.read_bytes(),
            self._candidate_bytes("scene-02"),
        )

        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(
            manifest["scenes"]["scene-01"]["renderIdentityHash"],
            old_identities["scene-01"],
        )
        self.assertNotEqual(
            manifest["scenes"]["scene-02"]["renderIdentityHash"],
            old_identities["scene-02"],
        )
        self.assertIsNone(manifest["sceneReviewApproval"])


if __name__ == "__main__":
    unittest.main()
