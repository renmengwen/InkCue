"""Artifact-first annotation dispatch helpers.

This module deliberately stays below the real host adapter boundary.  It gives
the coordinator a deterministic dispatch manifest, a small candidate-ready
watchdog, and phase-level audit timestamps.  A host may use these helpers while
waiting for child agents; no child result or formal annotation is written here.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

try:
    from .annotation_contract import (
        SUPPORTED_VISUAL_ELEMENTS_CONTRACTS,
        validate_visual_elements,
    )
except ImportError:  # pragma: no cover - direct script execution
    from annotation_contract import (  # type: ignore
        SUPPORTED_VISUAL_ELEMENTS_CONTRACTS,
        validate_visual_elements,
    )


DISPATCH_MANIFEST_CONTRACT = "whiteboard-annotation-dispatch-v3"
DISPATCH_PROTOCOL = "annotation-artifact-first-v1"
DEFAULT_TAIL_GRACE_SECONDS = 30.0


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _write_json_atomic(path: Path, value: Mapping[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    os.replace(temporary, path)


def _sha256(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class CandidateObservation:
    task_id: str
    status: str
    path: str
    sha256: str | None = None
    bytes: int | None = None
    error: str | None = None
    observed_at: str = field(default_factory=utc_now)


def observe_candidate(
    task_id: str,
    candidate_path: Path,
    *,
    attempt_dir: Path | None = None,
    expected_sha256: str | None = None,
    validator: Callable[[Path], Any] | None = None,
) -> CandidateObservation:
    """Return a fail-closed observation without mutating the attempt.

    ``ready`` means the file exists, is valid UTF-8 JSON and (when supplied)
    passes the caller's candidate validator.  A candidate outside the frozen
    attempt is reported as ``forbidden``; no path is followed through a
    symlink.  ``stale`` is reserved for an expected SHA mismatch.
    """

    if candidate_path.is_symlink():
        return CandidateObservation(task_id, "forbidden", str(candidate_path), error="candidate symlink is not allowed")
    path = candidate_path.resolve(strict=False)
    if attempt_dir is not None:
        root = attempt_dir.resolve(strict=False)
        try:
            path.relative_to(root)
        except ValueError:
            return CandidateObservation(task_id, "forbidden", str(path), error="candidate outside attempt")
    if not path.is_file():
        return CandidateObservation(task_id, "missing", str(path))
    try:
        raw = path.read_bytes()
        json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return CandidateObservation(task_id, "invalid", str(path), error=str(exc))
    digest = _sha256(path)
    if expected_sha256 and digest != expected_sha256:
        return CandidateObservation(
            task_id, "stale", str(path), sha256=digest, bytes=len(raw), error="candidate SHA mismatch"
        )
    if validator is not None:
        try:
            validator(path)
        except Exception as exc:  # validator errors are represented, never raised from watchdog
            return CandidateObservation(
                task_id, "invalid", str(path), sha256=digest, bytes=len(raw), error=str(exc)
            )
    return CandidateObservation(task_id, "ready", str(path), sha256=digest, bytes=len(raw))


def _validate_visual_candidate(path: Path) -> None:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("annotation candidate 顶层必须是对象")
    contract = value.get("contractVersion")
    if contract not in (None, *SUPPORTED_VISUAL_ELEMENTS_CONTRACTS):
        raise ValueError("annotation candidate contractVersion 不支持")
    if contract is not None and set(value) != {"contractVersion", "elements"}:
        raise ValueError("annotation candidate 字段不符合 visual-elements allowlist")
    validate_visual_elements(value.get("elements"))


@dataclass
class _WatchdogRecord:
    ready_at: float | None = None
    ready_at_iso: str | None = None
    last: CandidateObservation | None = None
    finalized: bool = False


class ArtifactFirstWatchdog:
    """Track candidate readiness independently from child process exit.

    Once a valid candidate appears, callers may finalize immediately when the
    child has exited, or after ``tail_grace_seconds`` while it is still running.
    This prevents a natural-language final response from extending the critical
    path indefinitely.
    """

    def __init__(self, *, tail_grace_seconds: float = DEFAULT_TAIL_GRACE_SECONDS, clock: Callable[[], float] = time.monotonic) -> None:
        if tail_grace_seconds < 0:
            raise ValueError("tail_grace_seconds must be non-negative")
        self.tail_grace_seconds = float(tail_grace_seconds)
        self._clock = clock
        self._records: dict[str, _WatchdogRecord] = {}

    def observe(self, observation: CandidateObservation, *, child_running: bool = True) -> str:
        record = self._records.setdefault(observation.task_id, _WatchdogRecord())
        record.last = observation
        if observation.status == "ready" and record.ready_at is None:
            record.ready_at = self._clock()
            record.ready_at_iso = observation.observed_at
        if observation.status != "ready":
            return observation.status
        if not child_running or self.should_finalize(observation.task_id, child_running=child_running):
            record.finalized = True
            return "finalize"
        return "candidate_ready"

    def should_finalize(self, task_id: str, *, child_running: bool = True) -> bool:
        record = self._records.get(task_id)
        if record is None or record.finalized or record.ready_at is None:
            return False
        if not child_running:
            return True
        return self._clock() - record.ready_at >= self.tail_grace_seconds

    def mark_finalized(self, task_id: str) -> None:
        self._records.setdefault(task_id, _WatchdogRecord()).finalized = True

    def snapshot(self) -> dict[str, Any]:
        return {
            task_id: {
                "status": record.last.status if record.last else "missing",
                "readyAt": record.ready_at_iso,
                "finalized": record.finalized,
                "candidateSha256": record.last.sha256 if record.last else None,
                "candidateBytes": record.last.bytes if record.last else None,
                "error": record.last.error if record.last else None,
            }
            for task_id, record in self._records.items()
        }


class DispatchAudit:
    """Small phase-level audit object suitable for JSON persistence."""

    def __init__(self, *, configured_concurrency: int, task_count: int, unit_count: int = 0) -> None:
        self._started = time.monotonic()
        self._last_mark = self._started
        self.data: dict[str, Any] = {
            "auditContractVersion": "whiteboard-annotation-dispatch-audit-v1",
            "configuredConcurrency": int(configured_concurrency),
            "effectiveConcurrency": 0,
            "peakChildAgents": 0,
            "taskCount": int(task_count),
            "dispatchUnitCount": int(unit_count),
            "timestamps": {"dispatchStartedAt": utc_now()},
            "durationsMs": {},
            "counters": {
                "candidateReadyCount": 0,
                "candidateInvalidCount": 0,
                "candidateStaleCount": 0,
                "childCancelCount": 0,
                "tailGraceCount": 0,
            },
        }

    def mark(self, phase: str, *, count: int | None = None) -> None:
        monotonic_now = time.monotonic()
        now = utc_now()
        self.data["timestamps"][phase] = now
        self.data["durationsMs"][phase] = int(round((monotonic_now - self._last_mark) * 1000))
        self.data.setdefault("cumulativeDurationsMs", {})[phase] = int(
            round((monotonic_now - self._started) * 1000)
        )
        self._last_mark = monotonic_now
        if count is not None:
            self.data["counters"][phase] = int(count)

    def set_concurrency(self, *, effective: int, peak: int | None = None) -> None:
        self.data["effectiveConcurrency"] = int(effective)
        if peak is not None:
            self.data["peakChildAgents"] = int(peak)

    def finish(self, *, status: str = "prepared") -> dict[str, Any]:
        self.mark("auditFinishedAt")
        self.data["status"] = status
        return self.data


def build_dispatch_manifest(
    *,
    run_id: str,
    candidate_root: Path,
    tasks: Iterable[Mapping[str, Any]],
    dispatch_units: Iterable[Mapping[str, Any]],
    configured_concurrency: int | None = None,
    effective_concurrency: int | None = None,
    audit: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a host-neutral structured manifest with ASCII control flags."""

    if configured_concurrency is None:
        if effective_concurrency is None:
            raise TypeError("configured_concurrency is required")
        configured_concurrency = effective_concurrency
    elif effective_concurrency is not None and effective_concurrency != configured_concurrency:
        raise ValueError("effective_concurrency compatibility value must match configured_concurrency")
    return {
        "contractVersion": DISPATCH_MANIFEST_CONTRACT,
        "runId": run_id,
        "candidateRoot": str(candidate_root.resolve(strict=False)),
        "protocol": DISPATCH_PROTOCOL,
        "flags": {
            "PROCESS_TASKS_IN_SEQUENCE": True,
            "CONTINUE_AFTER_TASK_FAILURE": True,
            "WRITE_RESULT_JSON": False,
            "WRITE_FORMAL_FILES": False,
            "WRITE_APPROVAL_FILES": False,
            "STOP_AFTER_CANDIDATE_READY": True,
            "LINT_CANDIDATE_BEFORE_NEXT_TASK": True,
            "REPAIR_SCHEMA_JSON_ONLY": True,
            "RESTART_SHORT_CONTEXT_AFTER_PAYLOAD_TOO_LARGE": True,
        },
        "configuredConcurrency": int(configured_concurrency),
        "tasks": [dict(item) for item in tasks],
        "dispatchUnits": [dict(item) for item in dispatch_units],
        "audit": dict(audit or {}),
        "createdAt": utc_now(),
    }


def observe_dispatch_manifest(
    manifest_path: Path,
    *,
    task_id: str,
    child_running: bool,
    tail_grace_seconds: float = DEFAULT_TAIL_GRACE_SECONDS,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Observe one frozen candidate and persist coordinator-owned audit only.

    This function never writes ``result.json``, a formal annotation, an
    approval, or any host-agent command.  ``finalizeRecommended`` is a signal
    for the coordinator to run the existing deterministic materializer.
    """

    if tail_grace_seconds < 0:
        raise ValueError("tail_grace_seconds must be non-negative")
    if manifest_path.is_symlink():
        raise ValueError("dispatch manifest 不能是符号链接")
    path = manifest_path.resolve(strict=True)
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or raw.get("contractVersion") != DISPATCH_MANIFEST_CONTRACT:
        raise ValueError("dispatch manifest contract 无效")
    candidate_root_value = raw.get("candidateRoot")
    if not isinstance(candidate_root_value, str):
        raise ValueError("dispatch manifest candidateRoot 无效")
    candidate_root = Path(candidate_root_value).resolve(strict=True)
    if path.parent != candidate_root:
        raise ValueError("dispatch manifest 必须是 current candidateRoot 直属普通文件")
    tasks = raw.get("tasks")
    if not isinstance(tasks, list):
        raise ValueError("dispatch manifest tasks 无效")
    task = next(
        (item for item in tasks if isinstance(item, Mapping) and item.get("taskId") == task_id),
        None,
    )
    if task is None:
        raise ValueError("taskId 不在 dispatch manifest")
    candidate_value = task.get("candidateAnnotationPath")
    attempt_value = task.get("allowedAttemptDir")
    if not isinstance(candidate_value, str) or not isinstance(attempt_value, str):
        raise ValueError("dispatch task 缺少 candidate/attempt 定位")
    attempt_dir = Path(attempt_value).resolve(strict=True)
    try:
        attempt_dir.relative_to(candidate_root)
    except ValueError as exc:
        raise ValueError("dispatch attempt 越出 candidateRoot") from exc
    candidate_path = Path(candidate_value)
    expected_candidate_path = attempt_dir / "candidate.annotation.json"
    if candidate_path.resolve(strict=False) != expected_candidate_path.resolve(strict=False):
        raise ValueError("dispatch candidate 路径不是冻结 attempt 的标准候选路径")
    audit = raw.setdefault("audit", {})
    if not isinstance(audit, dict):
        raise ValueError("dispatch manifest audit 无效")
    timestamps = audit.setdefault("timestamps", {})
    durations = audit.setdefault("durationsMs", {})
    counters = audit.setdefault("counters", {})
    observations = audit.setdefault("taskObservations", {})
    if not all(isinstance(item, dict) for item in (timestamps, durations, counters, observations)):
        raise ValueError("dispatch manifest audit 子结构无效")
    previous = observations.get(task_id)
    ready_at = previous.get("candidateReadyAt") if isinstance(previous, Mapping) else None
    frozen_sha256 = (
        previous.get("frozenCandidateSha256")
        if isinstance(previous, Mapping)
        else None
    )
    if frozen_sha256 is not None and not isinstance(frozen_sha256, str):
        raise ValueError("dispatch candidate 冻结 SHA 无效")
    observation = observe_candidate(
        task_id,
        candidate_path,
        attempt_dir=attempt_dir,
        expected_sha256=frozen_sha256,
        validator=_validate_visual_candidate,
    )
    observed_now = now or datetime.now(timezone.utc)
    if observation.status == "ready" and not isinstance(ready_at, str):
        ready_at = observed_now.isoformat().replace("+00:00", "Z")
        frozen_sha256 = observation.sha256
        timestamps.setdefault("firstCandidateReadyAt", ready_at)
        timestamps["lastCandidateReadyAt"] = ready_at
        counters["candidateReadyCount"] = int(counters.get("candidateReadyCount") or 0) + 1
    elif observation.status == "invalid" and (
        not isinstance(previous, Mapping) or previous.get("status") != "invalid"
    ):
        counters["candidateInvalidCount"] = int(counters.get("candidateInvalidCount") or 0) + 1
    elif observation.status == "stale" and (
        not isinstance(previous, Mapping) or previous.get("status") != "stale"
    ):
        counters["candidateStaleCount"] = int(counters.get("candidateStaleCount") or 0) + 1
    tail_ms = 0
    finalize = False
    if observation.status == "ready" and isinstance(ready_at, str):
        tail_ms = max(0, int(round((observed_now - _parse_utc(ready_at)).total_seconds() * 1000)))
        finalize = not child_running or tail_ms >= int(round(tail_grace_seconds * 1000))
        durations["childTail"] = tail_ms
        previously_finalized = bool(
            isinstance(previous, Mapping) and previous.get("finalizeRecommended")
        )
        if finalize and not previously_finalized:
            timestamps["finalizeRecommendedAt"] = observed_now.isoformat().replace("+00:00", "Z")
            counters["tailGraceCount"] = int(counters.get("tailGraceCount") or 0) + int(child_running)
    observations[task_id] = {
        "status": observation.status,
        "candidateReadyAt": ready_at,
        "lastObservedAt": observed_now.isoformat().replace("+00:00", "Z"),
        "candidateSha256": observation.sha256,
        "frozenCandidateSha256": frozen_sha256,
        "candidateBytes": observation.bytes,
        "error": observation.error,
        "childRunning": bool(child_running),
        "finalizeRecommended": finalize,
    }
    _write_json_atomic(path, raw)
    return {
        "contractVersion": "whiteboard-annotation-dispatch-observation-v1",
        "status": observation.status,
        "taskId": task_id,
        "candidateSha256": observation.sha256,
        "candidateBytes": observation.bytes,
        "childRunning": bool(child_running),
        "tailGraceSeconds": tail_grace_seconds,
        "childTailMs": tail_ms,
        "finalizeRecommended": finalize,
        "nextAction": "materialize_and_validate_result" if finalize else "continue_observing",
        "manifestPath": str(path),
        "formalWritesPerformed": False,
        "approvalWritten": False,
    }


def main(argv: list[str] | None = None) -> int:
    try:
        from .cli_runtime import configure_utf8_stdio
    except ImportError:  # pragma: no cover - direct script execution
        from cli_runtime import configure_utf8_stdio  # type: ignore
    configure_utf8_stdio()
    parser = argparse.ArgumentParser(description="观察 annotation candidate-ready watchdog")
    sub = parser.add_subparsers(dest="command", required=True)
    observe = sub.add_parser("observe")
    observe.add_argument("--manifest", required=True, type=Path)
    observe.add_argument("--task-id", required=True)
    state = observe.add_mutually_exclusive_group(required=True)
    state.add_argument("--child-running", action="store_true")
    state.add_argument("--child-finished", action="store_true")
    observe.add_argument("--tail-grace-seconds", type=float, default=DEFAULT_TAIL_GRACE_SECONDS)
    args = parser.parse_args(argv)
    try:
        summary = observe_dispatch_manifest(
            args.manifest,
            task_id=args.task_id,
            child_running=args.child_running,
            tail_grace_seconds=args.tail_grace_seconds,
        )
        code = 0 if summary["status"] in {"ready", "missing"} else 2
    except Exception as exc:
        summary = {
            "contractVersion": "whiteboard-annotation-dispatch-observation-v1",
            "status": "FAIL",
            "error": str(exc),
            "formalWritesPerformed": False,
            "approvalWritten": False,
        }
        code = 2
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return code


__all__ = [
    "ArtifactFirstWatchdog",
    "CandidateObservation",
    "DEFAULT_TAIL_GRACE_SECONDS",
    "DISPATCH_MANIFEST_CONTRACT",
    "DISPATCH_PROTOCOL",
    "DispatchAudit",
    "build_dispatch_manifest",
    "observe_candidate",
    "observe_dispatch_manifest",
    "utc_now",
]


if __name__ == "__main__":
    raise SystemExit(main())
