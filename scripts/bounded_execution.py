"""通用的有界任务执行器。

本模块只负责调度和结构化结果收集。worker 不得通过本模块写共享
manifest、正式文件或批准状态；这些动作应由主线程中的 ``consumer``
串行完成。
"""

from __future__ import annotations

import threading
from concurrent.futures import Executor, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from typing import Any, Callable, Generic, Iterable, Literal, Mapping, Protocol, TypeVar


TaskT = TypeVar("TaskT")
ValueT = TypeVar("ValueT")

FailurePolicy = Literal["continue_independent", "stop_dispatch"]
ResultStatus = Literal["succeeded", "failed", "cancelled", "not_dispatched"]
StopReason = Literal["stop_dispatch", "cancel_requested", "coordinator_failure"]

CONTINUE_INDEPENDENT: FailurePolicy = "continue_independent"
STOP_DISPATCH: FailurePolicy = "stop_dispatch"
_FAILURE_POLICIES = frozenset({CONTINUE_INDEPENDENT, STOP_DISPATCH})


class CancellationEvent(Protocol):
    """``threading.Event`` 所需的最小只读合同。"""

    def is_set(self) -> bool: ...


@dataclass(frozen=True)
class WorkerEvent:
    """worker 随结果返回的去敏阶段事件。"""

    stage: str
    status: str
    details: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class WorkerFailure:
    """可由业务层映射到既有稳定错误类别的结构化失败。"""

    category: str
    message: str
    retryable: bool = False
    exception_type: str | None = None


@dataclass(frozen=True)
class WorkerOutcome(Generic[ValueT]):
    """worker 唯一允许返回的结果信封。"""

    ok: bool
    value: ValueT | None = None
    error: WorkerFailure | None = None
    events: tuple[WorkerEvent, ...] = ()

    def __post_init__(self) -> None:
        if self.ok and self.error is not None:
            raise ValueError("成功的 worker outcome 不能包含 error")
        if not self.ok and self.error is None:
            raise ValueError("失败的 worker outcome 必须包含 error")

    @classmethod
    def success(
        cls,
        value: ValueT | None = None,
        *,
        events: Iterable[WorkerEvent] = (),
    ) -> "WorkerOutcome[ValueT]":
        return cls(ok=True, value=value, events=tuple(events))

    @classmethod
    def failed(
        cls,
        error: WorkerFailure,
        *,
        events: Iterable[WorkerEvent] = (),
    ) -> "WorkerOutcome[ValueT]":
        return cls(ok=False, error=error, events=tuple(events))


@dataclass(frozen=True)
class TaskResult(Generic[TaskT, ValueT]):
    """单个计划任务的结构化终态。"""

    plan_index: int
    task: TaskT
    status: ResultStatus
    dispatched: bool
    outcome: WorkerOutcome[ValueT] | None = None
    reason: str | None = None


@dataclass(frozen=True)
class CoordinatorFailure:
    """主线程派发或消费结果时的致命错误。"""

    phase: Literal["dispatch", "consumer"]
    plan_index: int
    exception_type: str
    message: str


@dataclass(frozen=True)
class ExecutionReport(Generic[TaskT, ValueT]):
    """按输入计划顺序排列的批次报告。"""

    results: tuple[TaskResult[TaskT, ValueT], ...]
    requested_workers: int
    effective_workers: int
    submitted_count: int
    peak_active_workers: int
    failure_policy: FailurePolicy
    stop_reason: StopReason | None = None
    coordinator_failure: CoordinatorFailure | None = None


Worker = Callable[[TaskT], WorkerOutcome[ValueT]]
Consumer = Callable[[TaskResult[TaskT, ValueT]], None]
ExceptionMapper = Callable[[Exception], WorkerFailure]
ExecutorFactory = Callable[[int], Executor]


def _default_exception_failure(exc: Exception) -> WorkerFailure:
    # 不复制异常正文，因为其中可能含 token、URL、路径或 provider 响应。
    return WorkerFailure(
        category="worker_exception",
        message="worker 抛出未处理异常",
        retryable=False,
        exception_type=type(exc).__name__,
    )


def _map_exception(
    exc: Exception,
    exception_mapper: ExceptionMapper | None,
) -> WorkerFailure:
    if exception_mapper is None:
        return _default_exception_failure(exc)
    try:
        mapped = exception_mapper(exc)
    except Exception:
        return _default_exception_failure(exc)
    if not isinstance(mapped, WorkerFailure):
        return _default_exception_failure(exc)
    return mapped


def _validate_max_workers(max_workers: int) -> None:
    if isinstance(max_workers, bool) or not isinstance(max_workers, int):
        raise TypeError("max_workers 必须是正整数")
    if max_workers < 1:
        raise ValueError("max_workers 必须至少为 1")


def _validate_failure_policy(failure_policy: FailurePolicy) -> None:
    if failure_policy not in _FAILURE_POLICIES:
        raise ValueError(f"不支持的 failure_policy: {failure_policy!r}")


def execute_bounded(
    tasks: Iterable[TaskT],
    worker: Worker[TaskT, ValueT],
    *,
    max_workers: int,
    failure_policy: FailurePolicy = CONTINUE_INDEPENDENT,
    consumer: Consumer[TaskT, ValueT] | None = None,
    cancel_event: CancellationEvent | None = None,
    exception_mapper: ExceptionMapper | None = None,
    executor_factory: ExecutorFactory | None = None,
) -> ExecutionReport[TaskT, ValueT]:
    """执行计划任务，并始终按计划顺序返回终态。

    ``max_workers=1`` 明确走普通循环，不实例化 executor。并发路径只维持
    ``effective_workers`` 个已提交且未完成的 future。``consumer`` 只会在
    调用本函数的协调器线程执行；一旦它失败，执行器停止派发并尝试取消尚
    未开始的 future，同时仍收集已经运行的 worker 终态。
    """

    _validate_max_workers(max_workers)
    _validate_failure_policy(failure_policy)
    plan = tuple(tasks)
    effective_workers = min(max_workers, len(plan)) if plan else 0
    if not plan:
        return ExecutionReport(
            results=(),
            requested_workers=max_workers,
            effective_workers=0,
            submitted_count=0,
            peak_active_workers=0,
            failure_policy=failure_policy,
        )

    results: list[TaskResult[TaskT, ValueT] | None] = [None] * len(plan)
    active_lock = threading.Lock()
    active_workers = 0
    peak_active_workers = 0

    def invoke(index: int) -> tuple[int, WorkerOutcome[ValueT]]:
        nonlocal active_workers, peak_active_workers
        with active_lock:
            active_workers += 1
            peak_active_workers = max(peak_active_workers, active_workers)
        try:
            try:
                outcome = worker(plan[index])
            except Exception as exc:
                outcome = WorkerOutcome.failed(_map_exception(exc, exception_mapper))
            if not isinstance(outcome, WorkerOutcome):
                outcome = WorkerOutcome.failed(
                    WorkerFailure(
                        category="worker_contract",
                        message="worker 必须返回 WorkerOutcome",
                        retryable=False,
                        exception_type=type(outcome).__name__,
                    )
                )
            return index, outcome
        finally:
            with active_lock:
                active_workers -= 1

    def terminal_result(
        index: int,
        outcome: WorkerOutcome[ValueT],
    ) -> TaskResult[TaskT, ValueT]:
        return TaskResult(
            plan_index=index,
            task=plan[index],
            status="succeeded" if outcome.ok else "failed",
            dispatched=True,
            outcome=outcome,
        )

    if max_workers == 1:
        submitted_count = 0
        stop_reason: StopReason | None = None
        coordinator_failure: CoordinatorFailure | None = None
        for index in range(len(plan)):
            if cancel_event is not None and cancel_event.is_set():
                stop_reason = "cancel_requested"
                break
            submitted_count += 1
            _, outcome = invoke(index)
            result = terminal_result(index, outcome)
            results[index] = result
            if consumer is not None:
                try:
                    consumer(result)
                except Exception as exc:
                    coordinator_failure = CoordinatorFailure(
                        phase="consumer",
                        plan_index=index,
                        exception_type=type(exc).__name__,
                        message="协调器消费结果时发生致命错误",
                    )
                    stop_reason = "coordinator_failure"
                    break
            if not outcome.ok and failure_policy == STOP_DISPATCH:
                stop_reason = "stop_dispatch"
                break

        for index, existing in enumerate(results):
            if existing is not None:
                continue
            if stop_reason == "cancel_requested":
                results[index] = TaskResult(
                    index, plan[index], "cancelled", False, reason=stop_reason
                )
            else:
                results[index] = TaskResult(
                    index,
                    plan[index],
                    "not_dispatched",
                    False,
                    reason=stop_reason or "not_dispatched",
                )
        return ExecutionReport(
            results=tuple(result for result in results if result is not None),
            requested_workers=max_workers,
            effective_workers=effective_workers,
            submitted_count=submitted_count,
            peak_active_workers=peak_active_workers,
            failure_policy=failure_policy,
            stop_reason=stop_reason,
            coordinator_failure=coordinator_failure,
        )

    factory = executor_factory or (lambda count: ThreadPoolExecutor(max_workers=count))
    submitted_count = 0
    next_index = 0
    stop_reason: StopReason | None = None
    coordinator_failure: CoordinatorFailure | None = None

    def cancellation_requested() -> bool:
        return cancel_event is not None and cancel_event.is_set()

    with factory(effective_workers) as executor:
        futures: dict[Future[tuple[int, WorkerOutcome[ValueT]]], int] = {}

        def cancel_not_started() -> None:
            for future in tuple(futures):
                future.cancel()

        def submit_until_full() -> None:
            nonlocal next_index, submitted_count, stop_reason, coordinator_failure
            while (
                stop_reason is None
                and next_index < len(plan)
                and len(futures) < effective_workers
            ):
                if cancellation_requested():
                    stop_reason = "cancel_requested"
                    cancel_not_started()
                    return
                index = next_index
                try:
                    future = executor.submit(invoke, index)
                except Exception as exc:
                    coordinator_failure = CoordinatorFailure(
                        phase="dispatch",
                        plan_index=index,
                        exception_type=type(exc).__name__,
                        message="协调器派发任务时发生致命错误",
                    )
                    stop_reason = "coordinator_failure"
                    cancel_not_started()
                    return
                futures[future] = index
                submitted_count += 1
                next_index += 1

        submit_until_full()
        while futures:
            if cancellation_requested() and stop_reason != "coordinator_failure":
                stop_reason = "cancel_requested"
                cancel_not_started()

            done, _ = wait(
                tuple(futures),
                timeout=0.05 if cancel_event is not None else None,
                return_when="FIRST_COMPLETED",
            )
            if not done:
                continue

            completed: list[TaskResult[TaskT, ValueT]] = []
            for future in sorted(done, key=lambda item: futures[item]):
                expected_index = futures.pop(future)
                if future.cancelled():
                    result = TaskResult(
                        expected_index,
                        plan[expected_index],
                        "cancelled",
                        True,
                        reason=stop_reason or "cancel_requested",
                    )
                else:
                    try:
                        returned_index, outcome = future.result()
                    except Exception as exc:
                        returned_index = expected_index
                        outcome = WorkerOutcome.failed(
                            _map_exception(exc, exception_mapper)
                        )
                    if returned_index != expected_index:
                        outcome = WorkerOutcome.failed(
                            WorkerFailure(
                                category="worker_contract",
                                message="worker 返回的计划索引不匹配",
                            )
                        )
                    result = terminal_result(expected_index, outcome)
                results[expected_index] = result
                completed.append(result)

            if (
                stop_reason is None
                and failure_policy == STOP_DISPATCH
                and any(result.status == "failed" for result in completed)
            ):
                stop_reason = "stop_dispatch"

            if coordinator_failure is None and consumer is not None:
                for result in completed:
                    try:
                        consumer(result)
                    except Exception as exc:
                        coordinator_failure = CoordinatorFailure(
                            phase="consumer",
                            plan_index=result.plan_index,
                            exception_type=type(exc).__name__,
                            message="协调器消费结果时发生致命错误",
                        )
                        stop_reason = "coordinator_failure"
                        cancel_not_started()
                        break

            if cancellation_requested() and stop_reason != "coordinator_failure":
                stop_reason = "cancel_requested"
                cancel_not_started()
            if stop_reason is None:
                submit_until_full()

    for index, existing in enumerate(results):
        if existing is not None:
            continue
        if stop_reason == "cancel_requested":
            status: ResultStatus = "cancelled"
        else:
            status = "not_dispatched"
        results[index] = TaskResult(
            index,
            plan[index],
            status,
            False,
            reason=stop_reason or "not_dispatched",
        )

    return ExecutionReport(
        results=tuple(result for result in results if result is not None),
        requested_workers=max_workers,
        effective_workers=effective_workers,
        submitted_count=submitted_count,
        peak_active_workers=peak_active_workers,
        failure_policy=failure_policy,
        stop_reason=stop_reason,
        coordinator_failure=coordinator_failure,
    )
