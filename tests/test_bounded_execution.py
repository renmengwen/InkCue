from __future__ import annotations

import sys
import threading
import unittest
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Callable


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import bounded_execution as be  # noqa: E402


WAIT_SECONDS = 5


class AsyncCall:
    def __init__(self, function: Callable[[], object]) -> None:
        self.result: object | None = None
        self.error: BaseException | None = None
        self.done = threading.Event()

        def target() -> None:
            try:
                self.result = function()
            except BaseException as exc:  # 测试线程必须把所有失败带回主线程
                self.error = exc
            finally:
                self.done.set()

        self.thread = threading.Thread(target=target, name="bounded-test-coordinator")
        self.thread.start()

    def finish(self) -> object:
        if not self.done.wait(WAIT_SECONDS):
            self.fail_and_join("异步执行未在期限内结束")
        self.thread.join()
        if self.error is not None:
            raise self.error
        return self.result

    def fail_and_join(self, message: str) -> None:
        raise AssertionError(message)


class ObservableSingleWorkerExecutor(ThreadPoolExecutor):
    """声明可接收两个 future，但只启动一个，用于确定性验证取消排队任务。"""

    def __init__(self, _requested_workers: int) -> None:
        super().__init__(max_workers=1, thread_name_prefix="controlled-bounded")
        self.submitted: list[Future[object]] = []
        self.second_submitted = threading.Event()
        self.second_cancelled = threading.Event()

    def submit(self, fn: Callable[..., object], /, *args: object, **kwargs: object):  # type: ignore[override]
        future = super().submit(fn, *args, **kwargs)
        self.submitted.append(future)
        if len(self.submitted) == 2:
            future.add_done_callback(
                lambda completed: self.second_cancelled.set()
                if completed.cancelled()
                else None
            )
            self.second_submitted.set()
        return future


class BoundedExecutionTests(unittest.TestCase):
    def assert_event(self, event: threading.Event, message: str) -> None:
        self.assertTrue(event.wait(WAIT_SECONDS), message)

    def test_serial_uses_plain_loop_without_executor(self) -> None:
        coordinator_thread = threading.get_ident()
        worker_threads: list[int] = []
        consumed_threads: list[int] = []

        def forbidden_factory(_workers: int):
            raise AssertionError("max_workers=1 不得创建 executor")

        report = be.execute_bounded(
            ["a", "b", "c"],
            lambda task: (
                worker_threads.append(threading.get_ident())
                or be.WorkerOutcome.success(task.upper())
            ),
            max_workers=1,
            consumer=lambda _result: consumed_threads.append(threading.get_ident()),
            executor_factory=forbidden_factory,
        )

        self.assertEqual([result.task for result in report.results], ["a", "b", "c"])
        self.assertEqual(
            [result.outcome.value for result in report.results if result.outcome],
            ["A", "B", "C"],
        )
        self.assertEqual(worker_threads, [coordinator_thread] * 3)
        self.assertEqual(consumed_threads, [coordinator_thread] * 3)
        self.assertEqual(report.peak_active_workers, 1)
        self.assertEqual(report.submitted_count, 3)

    def test_concurrent_path_is_rolling_and_never_exceeds_window(self) -> None:
        first_window_started = threading.Event()
        release = threading.Event()
        lock = threading.Lock()
        started: list[int] = []
        active = 0
        peak = 0

        def worker(task: int) -> be.WorkerOutcome[int]:
            nonlocal active, peak
            with lock:
                started.append(task)
                active += 1
                peak = max(peak, active)
                if len(started) == 3:
                    first_window_started.set()
            if not release.wait(WAIT_SECONDS):
                raise RuntimeError("测试未释放 worker")
            with lock:
                active -= 1
            return be.WorkerOutcome.success(task)

        call = AsyncCall(
            lambda: be.execute_bounded(range(8), worker, max_workers=3)
        )
        self.assert_event(first_window_started, "初始三个 worker 未全部启动")
        with lock:
            self.assertEqual(started, [0, 1, 2])
            self.assertEqual(active, 3)
        release.set()
        report = call.finish()
        self.assertIsInstance(report, be.ExecutionReport)
        assert isinstance(report, be.ExecutionReport)
        self.assertEqual(report.submitted_count, 8)
        self.assertEqual(report.peak_active_workers, 3)
        self.assertEqual(peak, 3)
        self.assertLessEqual(peak, 3)
        self.assertEqual([result.plan_index for result in report.results], list(range(8)))

    def test_out_of_order_completion_keeps_plan_order(self) -> None:
        all_started = threading.Event()
        lock = threading.Lock()
        started = 0
        gates = [threading.Event() for _ in range(3)]
        finished = [threading.Event() for _ in range(3)]
        finish_order: list[int] = []
        consumed: list[int] = []

        def worker(task: int) -> be.WorkerOutcome[str]:
            nonlocal started
            with lock:
                started += 1
                if started == 3:
                    all_started.set()
            if not gates[task].wait(WAIT_SECONDS):
                raise RuntimeError("测试未释放 worker")
            with lock:
                finish_order.append(task)
            finished[task].set()
            return be.WorkerOutcome.success(f"value-{task}")

        call = AsyncCall(
            lambda: be.execute_bounded(
                range(3),
                worker,
                max_workers=3,
                consumer=lambda result: consumed.append(result.plan_index),
            )
        )
        self.assert_event(all_started, "worker 未全部启动")
        for index in (2, 1, 0):
            gates[index].set()
            self.assert_event(finished[index], f"worker {index} 未结束")
            if index != 0:
                # 下一扇 gate 尚未打开，因此完成顺序没有歧义。
                self.assertFalse(finished[index - 1].is_set())
        report = call.finish()
        assert isinstance(report, be.ExecutionReport)
        self.assertEqual(finish_order, [2, 1, 0])
        self.assertCountEqual(consumed, [0, 1, 2])
        self.assertEqual([result.plan_index for result in report.results], [0, 1, 2])
        self.assertEqual(
            [result.outcome.value for result in report.results if result.outcome],
            ["value-0", "value-1", "value-2"],
        )

    def test_continue_independent_collects_failure_exception_and_later_success(self) -> None:
        def worker(task: int) -> be.WorkerOutcome[int]:
            if task == 0:
                return be.WorkerOutcome.failed(
                    be.WorkerFailure("invalid_input", "输入无效")
                )
            if task == 1:
                raise RuntimeError("SECRET-TOKEN-SHOULD-NOT-LEAK")
            return be.WorkerOutcome.success(task)

        report = be.execute_bounded(
            range(4),
            worker,
            max_workers=2,
            failure_policy=be.CONTINUE_INDEPENDENT,
        )

        self.assertEqual(
            [result.status for result in report.results],
            ["failed", "failed", "succeeded", "succeeded"],
        )
        exception_failure = report.results[1].outcome.error  # type: ignore[union-attr]
        self.assertEqual(exception_failure.category, "worker_exception")
        self.assertEqual(exception_failure.exception_type, "RuntimeError")
        self.assertNotIn("SECRET", exception_failure.message)
        self.assertEqual(report.submitted_count, 4)
        self.assertIsNone(report.stop_reason)

    def test_stop_dispatch_keeps_in_flight_tail_and_does_not_start_later_task(self) -> None:
        both_started = threading.Event()
        release_tail = threading.Event()
        failure_consumed = threading.Event()
        lock = threading.Lock()
        started: list[int] = []

        def worker(task: int) -> be.WorkerOutcome[int]:
            with lock:
                started.append(task)
                if 0 in started and 1 in started:
                    both_started.set()
            if task == 0:
                if not both_started.wait(WAIT_SECONDS):
                    raise RuntimeError("在途任务未启动")
                return be.WorkerOutcome.failed(
                    be.WorkerFailure("provider_failure", "首个请求失败")
                )
            if task == 1:
                if not release_tail.wait(WAIT_SECONDS):
                    raise RuntimeError("测试未允许在途任务收尾")
            return be.WorkerOutcome.success(task)

        def consume(result: be.TaskResult[int, int]) -> None:
            if result.plan_index == 0:
                failure_consumed.set()

        call = AsyncCall(
            lambda: be.execute_bounded(
                range(5),
                worker,
                max_workers=2,
                failure_policy=be.STOP_DISPATCH,
                consumer=consume,
            )
        )
        self.assert_event(both_started, "首批两个任务未启动")
        self.assert_event(failure_consumed, "协调器未消费首个失败")
        with lock:
            self.assertEqual(started, [0, 1])
        release_tail.set()
        report = call.finish()
        assert isinstance(report, be.ExecutionReport)
        self.assertEqual(
            [result.status for result in report.results],
            ["failed", "succeeded", "not_dispatched", "not_dispatched", "not_dispatched"],
        )
        self.assertEqual(report.submitted_count, 2)
        self.assertEqual(report.stop_reason, "stop_dispatch")

    def test_cancellation_stops_dispatch_and_cancels_not_started_future(self) -> None:
        started_first = threading.Event()
        release_first = threading.Event()
        cancel_event = threading.Event()
        holder: dict[str, ObservableSingleWorkerExecutor] = {}

        def factory(workers: int) -> ObservableSingleWorkerExecutor:
            executor = ObservableSingleWorkerExecutor(workers)
            holder["executor"] = executor
            return executor

        def worker(task: int) -> be.WorkerOutcome[int]:
            if task == 0:
                started_first.set()
                if not release_first.wait(WAIT_SECONDS):
                    raise RuntimeError("测试未释放首个 worker")
            return be.WorkerOutcome.success(task)

        call = AsyncCall(
            lambda: be.execute_bounded(
                range(5),
                worker,
                max_workers=2,
                cancel_event=cancel_event,
                executor_factory=factory,
            )
        )
        self.assert_event(started_first, "首个 worker 未启动")
        self.assert_event(
            holder["executor"].second_submitted,
            "第二个 future 未进入受控排队状态",
        )
        cancel_event.set()
        self.assert_event(
            holder["executor"].second_cancelled,
            "排队但未启动的 future 未被取消",
        )
        release_first.set()
        report = call.finish()
        assert isinstance(report, be.ExecutionReport)
        self.assertEqual(
            [result.status for result in report.results],
            ["succeeded", "cancelled", "cancelled", "cancelled", "cancelled"],
        )
        self.assertTrue(report.results[1].dispatched)
        self.assertFalse(report.results[2].dispatched)
        self.assertEqual(report.submitted_count, 2)
        self.assertEqual(report.stop_reason, "cancel_requested")

    def test_consumer_fatal_error_stops_dispatch_but_collects_running_tail(self) -> None:
        both_started = threading.Event()
        release_tail = threading.Event()
        consumer_failed = threading.Event()
        lock = threading.Lock()
        started: list[int] = []

        def worker(task: int) -> be.WorkerOutcome[int]:
            with lock:
                started.append(task)
                if 0 in started and 1 in started:
                    both_started.set()
            if task == 0:
                if not both_started.wait(WAIT_SECONDS):
                    raise RuntimeError("在途任务未启动")
            if task == 1 and not release_tail.wait(WAIT_SECONDS):
                raise RuntimeError("测试未允许在途任务收尾")
            return be.WorkerOutcome.success(task)

        def consumer(result: be.TaskResult[int, int]) -> None:
            if result.plan_index == 0:
                consumer_failed.set()
                raise OSError("不得进入报告的正式路径")

        call = AsyncCall(
            lambda: be.execute_bounded(
                range(4),
                worker,
                max_workers=2,
                consumer=consumer,
            )
        )
        self.assert_event(both_started, "首批任务未启动")
        self.assert_event(consumer_failed, "consumer 未触发致命错误")
        with lock:
            self.assertEqual(started, [0, 1])
        release_tail.set()
        report = call.finish()
        assert isinstance(report, be.ExecutionReport)
        self.assertEqual(
            [result.status for result in report.results],
            ["succeeded", "succeeded", "not_dispatched", "not_dispatched"],
        )
        self.assertEqual(report.stop_reason, "coordinator_failure")
        self.assertIsNotNone(report.coordinator_failure)
        self.assertEqual(report.coordinator_failure.phase, "consumer")
        self.assertEqual(report.coordinator_failure.exception_type, "OSError")
        self.assertNotIn("正式路径", report.coordinator_failure.message)

    def test_invalid_worker_contract_becomes_structured_failure(self) -> None:
        report = be.execute_bounded(
            ["task"],
            lambda _task: "raw-value",  # type: ignore[return-value]
            max_workers=1,
        )
        self.assertEqual(report.results[0].status, "failed")
        failure = report.results[0].outcome.error  # type: ignore[union-attr]
        self.assertEqual(failure.category, "worker_contract")

    def test_rejects_invalid_worker_count_and_policy(self) -> None:
        with self.assertRaises(TypeError):
            be.execute_bounded([], lambda _task: be.WorkerOutcome.success(), max_workers=True)
        with self.assertRaises(ValueError):
            be.execute_bounded([], lambda _task: be.WorkerOutcome.success(), max_workers=0)
        with self.assertRaises(ValueError):
            be.execute_bounded(
                [],
                lambda _task: be.WorkerOutcome.success(),
                max_workers=1,
                failure_policy="unknown",  # type: ignore[arg-type]
            )


if __name__ == "__main__":
    unittest.main()
