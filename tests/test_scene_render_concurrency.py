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

# Reuse the existing small schema-v2 project fixture without duplicating its
# image, timing-plan, and annotation construction in this focused test module.
import test_render_timing as render_fixture  # noqa: E402


class SceneRenderConcurrencyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = render_fixture.RenderTimingTests("runTest")
        self.fixture.setUp()
        self.project = self.fixture._project()

    def tearDown(self) -> None:
        self.fixture.tearDown()

    @staticmethod
    def _thread_executor(*, max_workers: int):
        return ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="scene-render-test")

    def test_candidate_scheduler_runs_independent_scenes_concurrently_and_caps_peak(self) -> None:
        tasks = [{"sceneId": f"scene-{index:02d}", "candidatePath": f"candidate-{index}"} for index in range(1, 6)]
        started = threading.Event()
        release = threading.Event()
        lock = threading.Lock()
        active = 0
        peak = 0

        def worker(task: dict) -> dict:
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
                if active == 3:
                    started.set()
            if not release.wait(5):
                raise AssertionError("测试 worker 未被释放")
            with lock:
                active -= 1
            now = time.time_ns()
            return {
                "sceneId": task["sceneId"],
                "candidatePath": task["candidatePath"],
                "status": "succeeded",
                "deepReceipt": {"sceneId": task["sceneId"]},
                "startedNs": now - 1,
                "finishedNs": now,
            }

        call_result: dict[str, object] = {}

        def run() -> None:
            call_result["value"] = render_stream_whiteboard._execute_formal_candidate_tasks(
                tasks,
                max_workers=3,
                worker=worker,
                executor_factory=self._thread_executor,
            )

        thread = threading.Thread(target=run, name="scene-render-coordinator-test")
        thread.start()
        self.assertTrue(started.wait(5), "三个独立 scene 未同时在途")
        with lock:
            self.assertEqual(active, 3)
        release.set()
        thread.join(5)
        self.assertFalse(thread.is_alive(), "受控 scene 调度未结束")
        results, _peak_from_receipts = call_result["value"]
        self.assertEqual(set(results), {task["sceneId"] for task in tasks})
        self.assertEqual(peak, 3)
        self.assertLessEqual(peak, 3)

    def test_scheduler_with_one_worker_is_plain_serial_execution(self) -> None:
        tasks = [{"sceneId": "scene-01"}, {"sceneId": "scene-02"}]
        calls: list[str] = []

        def forbidden_factory(**_kwargs):
            raise AssertionError("sceneRender=1 不应创建 executor")

        def worker(task: dict) -> dict:
            calls.append(task["sceneId"])
            started = time.time_ns()
            time.sleep(0.001)
            return {
                "sceneId": task["sceneId"],
                "candidatePath": task["sceneId"],
                "status": "succeeded",
                "startedNs": started,
                "finishedNs": time.time_ns(),
            }

        results, peak = render_stream_whiteboard._execute_formal_candidate_tasks(
            tasks,
            max_workers=1,
            worker=worker,
            executor_factory=forbidden_factory,
        )
        self.assertEqual(calls, ["scene-01", "scene-02"])
        self.assertEqual(list(results), calls)
        self.assertEqual(peak, 1)

    def _batch_args(self):
        return render_stream_whiteboard._parse_args(
            ["--project", str(self.project.root), "--all"]
        )

    def _run_batch_with_worker_results(self, worker_results: dict[str, dict], configured: int = 5):
        args = self._batch_args()
        contexts = render_timing.resolve_formal_scenes(
            self.project, [item["sceneId"] for item in self.project.plan["scenes"]]
        )
        executions: list[dict] = []

        def execute(tasks, *, max_workers, **_kwargs):
            executions.append({"taskCount": len(tasks), "maxWorkers": max_workers})
            # Deliberately return completion/insertion order opposite to the plan.
            return ({scene_id: worker_results[scene_id] for scene_id in reversed([c.scene_id for c in contexts])}, 2)

        publish_order: list[str] = []

        def publish(candidate, destination, **_kwargs):
            publish_order.append(destination.name)
            return {"marker": f"current-{destination.name}"}

        def update_manifest(context, **_kwargs):
            return {"scenes": {context.scene_id: {"renderIdentityHash": f"identity-{context.scene_id}"}}}

        cfg = mock.Mock()
        cfg.for_stage.return_value = configured
        with mock.patch.object(
            render_stream_whiteboard.annotation_review,
            "require_current_annotation_review_approval",
            return_value={"approved": True},
        ), mock.patch.object(
            project_workspace, "load_workspace_config", return_value=cfg
        ), mock.patch.object(
            render_stream_whiteboard, "_load_formal_hand", return_value=(None, None, None)
        ), mock.patch.object(
            render_stream_whiteboard, "_execute_formal_candidate_tasks", side_effect=execute
        ), mock.patch.object(
            render_stream_whiteboard, "_publish_and_bind_scene", side_effect=publish
        ), mock.patch.object(
            render_timing, "update_render_manifest", side_effect=update_manifest
        ):
            result = render_stream_whiteboard._run_formal_batch(args)
        return result, executions, publish_order

    def test_configured_five_is_consumed_and_effective_is_minimum_of_task_count(self) -> None:
        worker_results = {
            scene_id: {
                "sceneId": scene_id,
                "candidatePath": f"candidate-{scene_id}",
                "status": "succeeded",
                "deepReceipt": {"sceneId": scene_id},
            }
            for scene_id in ("scene-01", "scene-02")
        }
        result, executions, _publish_order = self._run_batch_with_worker_results(worker_results, configured=5)
        self.assertEqual(executions, [{"taskCount": 2, "maxWorkers": 2}])
        self.assertEqual(result["configuredSceneRenderConcurrency"], 5)
        self.assertEqual(result["effectiveSceneRenderConcurrency"], 2)
        self.assertEqual(result["taskCount"], 2)
        self.assertFalse(result["approvalWritten"])

    def test_out_of_order_worker_completion_is_published_in_generation_plan_order(self) -> None:
        worker_results = {
            "scene-01": {"sceneId": "scene-01", "candidatePath": "candidate-01", "status": "succeeded", "deepReceipt": {}},
            "scene-02": {"sceneId": "scene-02", "candidatePath": "candidate-02", "status": "succeeded", "deepReceipt": {}},
        }
        result, _executions, publish_order = self._run_batch_with_worker_results(worker_results, configured=5)
        self.assertEqual(publish_order, ["scene-01-whiteboard.mp4", "scene-02-whiteboard.mp4"])
        self.assertEqual(result["sceneOrder"], ["scene-01", "scene-02"])
        self.assertEqual([item["sceneId"] for item in result["results"]], ["scene-01", "scene-02"])
        self.assertEqual(result["status"], "PASS")

    def test_failed_worker_preserves_old_formal_file_and_allows_other_scene(self) -> None:
        contexts = render_timing.resolve_formal_scenes(
            self.project, ["scene-01", "scene-02"]
        )
        old_bytes = b"old-formal-scene-01"
        contexts[0].output_path.write_bytes(old_bytes)
        worker_results = {
            "scene-01": {
                "sceneId": "scene-01",
                "candidatePath": "candidate-01",
                "status": "failed",
                "stage": "candidate_generation_deep_validation",
                "errorType": "MediaValidationError",
                "error": "candidate deep validation failed",
                "exitCode": 4,
            },
            "scene-02": {
                "sceneId": "scene-02",
                "candidatePath": "candidate-02",
                "status": "succeeded",
                "deepReceipt": {"sceneId": "scene-02"},
            },
        }
        result, _executions, publish_order = self._run_batch_with_worker_results(worker_results, configured=5)
        self.assertEqual(result["status"], "FAIL")
        self.assertTrue(result["partialSuccess"])
        self.assertEqual(result["successCount"], 1)
        self.assertEqual(result["failureCount"], 1)
        self.assertEqual(publish_order, ["scene-02-whiteboard.mp4"])
        self.assertEqual(contexts[0].output_path.read_bytes(), old_bytes)
        self.assertFalse(result["approvalWritten"])

    def test_cli_reports_failed_batch_with_exit_code_one(self) -> None:
        payload = {
            "contractVersion": "whiteboard-scene-render-batch-v2",
            "status": "FAIL",
            "partialSuccess": True,
            "approvalWritten": False,
        }
        with mock.patch.object(render_stream_whiteboard, "_run_formal_batch", return_value=payload):
            exit_code = render_stream_whiteboard.main(
                ["--project", str(self.project.root), "--all"]
            )
        self.assertEqual(exit_code, 1)


if __name__ == "__main__":
    unittest.main()
