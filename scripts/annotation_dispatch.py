"""Artifact-first annotation dispatch helpers.

This module deliberately stays below the real host adapter boundary.  It gives
the coordinator a deterministic dispatch manifest, a small candidate-ready
watchdog, and phase-level audit timestamps.  A host may use these helpers while
waiting for child agents; no child result or formal annotation is written here.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping


DISPATCH_MANIFEST_CONTRACT = "whiteboard-annotation-dispatch-v2"
DISPATCH_PROTOCOL = "annotation-artifact-first-v1"
DEFAULT_TAIL_GRACE_SECONDS = 30.0


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


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
        now = utc_now()
        self.data["timestamps"][phase] = now
        self.data["durationsMs"][phase] = int(round((time.monotonic() - self._started) * 1000))
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
    effective_concurrency: int,
    audit: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a host-neutral structured manifest with ASCII control flags."""

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
        },
        "effectiveConcurrency": int(effective_concurrency),
        "tasks": [dict(item) for item in tasks],
        "dispatchUnits": [dict(item) for item in dispatch_units],
        "audit": dict(audit or {}),
        "createdAt": utc_now(),
    }


__all__ = [
    "ArtifactFirstWatchdog",
    "CandidateObservation",
    "DEFAULT_TAIL_GRACE_SECONDS",
    "DISPATCH_MANIFEST_CONTRACT",
    "DISPATCH_PROTOCOL",
    "DispatchAudit",
    "build_dispatch_manifest",
    "observe_candidate",
    "utc_now",
]
