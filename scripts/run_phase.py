#!/usr/bin/env python3
"""Optional deterministic Phase 4 runner.

The runner is intentionally a thin coordinator.  It invokes a registered
deterministic adapter in-process, prints one machine-readable summary, and
stops at the adapter's human gate.  The existing step-by-step CLIs remain the
recovery/debugging path; this file never calls a provider or writes approval.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Callable, Mapping

try:
    from .phase_adapters import PhaseAdapterError, run_annotation_preview, run_final_delivery
    from .project_workspace import (
        Project,
        ProjectValidationError,
        WorkspaceConfig,
        WorkspaceError,
        load_project,
        load_workspace_config,
    )
except ImportError:  # pragma: no cover - direct script execution
    from phase_adapters import PhaseAdapterError, run_annotation_preview, run_final_delivery  # type: ignore
    from project_workspace import (  # type: ignore
        Project,
        ProjectValidationError,
        WorkspaceConfig,
        WorkspaceError,
        load_project,
        load_workspace_config,
    )


RUNNER_CONTRACT = "phase-runner-v2"
PHASE_REGISTRY: dict[str, Callable[..., Mapping[str, Any]]] = {
    "annotation-preview": run_annotation_preview,
    "final-delivery": run_final_delivery,
}

# Stable process-level contract consumed by automation and the recovery docs.
EXIT_OK = 0
EXIT_INVALID_OR_TECHNICAL = 2
EXIT_HUMAN_GATE = 4
EXIT_STALE_BINDING = 5
EXIT_UNKNOWN_EXTERNAL_OUTCOME = 6

_STALE_RE = re.compile(r"stale|current.*(变化|不一致|缺失)|binding|已变化|已过期|批准.*不一致", re.I)
_UNKNOWN_RE = re.compile(r"unknown_external_outcome|未知.*外部|外部.*未知", re.I)


def phase_registry() -> Mapping[str, Callable[..., Mapping[str, Any]]]:
    """Return the immutable-by-convention phase registry for introspection."""

    return dict(PHASE_REGISTRY)


def _run_id_from_args(value: str | None) -> str | None:
    if value is None:
        return None
    # Adapter performs the authoritative validation; this only keeps a clear
    # parser error for accidental path/whitespace input.
    if not value or len(value) > 64 or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", value):
        raise ValueError("--run-id 必须是 1-64 位安全标识")
    return value


def _status_from_exception(exc: BaseException) -> tuple[str, int]:
    message = str(exc)
    if _UNKNOWN_RE.search(message):
        return "UNKNOWN_EXTERNAL_OUTCOME", EXIT_UNKNOWN_EXTERNAL_OUTCOME
    if _STALE_RE.search(message):
        return "STALE", EXIT_STALE_BINDING
    return "FAIL", EXIT_INVALID_OR_TECHNICAL


def _base_summary(
    *,
    phase: str,
    project_id: str | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    return {
        "contractVersion": RUNNER_CONTRACT,
        "phase": phase,
        "status": "FAIL",
        "projectId": project_id,
        "runId": run_id,
        "taskCount": 0,
        "configuredConcurrency": None,
        "effectiveConcurrency": 0,
        "peakConcurrency": 0,
        "successCount": 0,
        "failureCount": 0,
        "partialSuccess": False,
        "currentIdentity": None,
        "approvalWritten": False,
        "userConfirmationRequired": True,
        "nextGate": None,
        "failures": [],
        "artifact": None,
        "previewUrl": None,
        "artifactPaths": [],
        "deepValidation": {"skipped": False, "reused": False, "reason": None},
        "recovery": {"resumeCommand": None, "lastCompletedStep": None},
    }


def _projected_summary(
    phase: str,
    raw: Mapping[str, Any],
    *,
    project: Project,
    run_id: str | None,
    argv: list[str],
) -> dict[str, Any]:
    """Normalize an adapter result without changing its stage-specific data."""

    result = _base_summary(phase=phase, project_id=project.project_id, run_id=run_id)
    result.update(dict(raw))
    # Adapters may use their own contract version; the outer contract is fixed.
    result["contractVersion"] = RUNNER_CONTRACT
    result["phase"] = phase
    result["projectId"] = project.project_id
    result["runId"] = raw.get("runId") or run_id
    # Keep both the compact artifact field and explicit URL spelling for
    # consumers that render the summary directly.
    if "artifactUrl" not in result:
        result["artifactUrl"] = result.get("artifact")
    if "previewUrl" not in result:
        result["previewUrl"] = result.get("artifact")
    result["approvalWritten"] = False
    result["userConfirmationRequired"] = True
    result["failures"] = list(raw.get("failures") or [])
    if "deepValidation" not in result:
        reused = bool(raw.get("deepValidationReused") or raw.get("formalValidationMode") == "binding")
        result["deepValidation"] = {
            "skipped": reused,
            "reused": reused,
            "reason": raw.get("deepValidationBasis"),
        }
    result.setdefault("deepValidationSkipReason", raw.get("deepValidationBasis"))
    if result.get("status") == "PASS" and result.get("nextGate"):
        result["status"] = "WAITING_HUMAN_GATE"
    if result.get("status") == "FAIL" and result.get("failureCount", 0) == 0:
        result["failureCount"] = len(result["failures"])
    # Keep a copyable recovery command; no implicit approval or retry flag is
    # ever emitted.
    project_arg = str(project.root)
    resume = [sys.executable, "scripts/run_phase.py", "--project", project_arg, "--phase", phase]
    effective_run_id = result.get("runId")
    if effective_run_id:
        resume += ["--run-id", str(effective_run_id)]
    receipt = result.get("formalValidationReceipt")
    if receipt and effective_run_id:
        # The adapter deliberately does not implicitly reuse a prior receipt.
        # Make the safe, binding-preserving recovery command explicit.
        receipt_path = Path(str(receipt))
        if not receipt_path.is_absolute():
            receipt_path = project.root / receipt_path
        resume += [
            "--formal-context-receipt",
            str(receipt_path),
            "--formal-context-run-id",
            str(effective_run_id),
        ]
    result["recovery"] = {
        "resumeCommand": " ".join(resume),
        "lastCompletedStep": (
            raw.get("lastCompletedStep")
            if phase == "final-delivery"
            else "annotation_review_technical" if result.get("currentIdentity") else None
        ),
    }
    return result


def run_phase(
    *,
    project_path: str | Path,
    phase: str,
    config_path: str | Path | None = None,
    run_id: str | None = None,
    formal_context_receipt: str | Path | None = None,
    formal_context_run_id: str | None = None,
    review_policy: str | None = None,
    force_deep: bool = False,
) -> tuple[dict[str, Any], int]:
    """Execute one registered phase and return ``(summary, exit_code)``."""

    if phase not in PHASE_REGISTRY:
        summary = _base_summary(phase=phase, run_id=run_id)
        summary["error"] = {"code": "unknown_phase", "message": f"不支持的 phase: {phase}"}
        return summary, EXIT_INVALID_OR_TECHNICAL
    try:
        # A receipt is always bound to an explicit run identity.  Accept the
        # dedicated ``--formal-context-run-id`` as that identity when
        # ``--run-id`` is omitted, but reject conflicting values instead of
        # silently ignoring the second argument.
        if formal_context_receipt is None and formal_context_run_id is not None:
            raise ValueError("--formal-context-run-id 必须与 --formal-context-receipt 同时提供")
        if formal_context_receipt is not None and formal_context_run_id is not None:
            if run_id is not None and run_id != formal_context_run_id:
                raise ValueError("--run-id 与 --formal-context-run-id 必须一致")
            run_id = formal_context_run_id
        requested_run_id = _run_id_from_args(run_id)
        workspace: WorkspaceConfig = load_workspace_config(config_path)
        project: Project = load_project(project_path)
        adapter = PHASE_REGISTRY[phase]
        if phase == "final-delivery":
            if formal_context_receipt is not None or formal_context_run_id is not None:
                raise ValueError("final-delivery 不接受 formal context receipt")
            raw = adapter(
                workspace,
                project,
                run_id=requested_run_id,
                force_deep=force_deep,
            )
        else:
            raw = adapter(
                workspace,
                project,
                run_id=requested_run_id,
                formal_context_receipt=formal_context_receipt,
                review_policy=review_policy,
            )
        summary = _projected_summary(
            phase,
            raw,
            project=project,
            run_id=requested_run_id,
            argv=sys.argv[1:],
        )
        status = summary.get("status")
        if status == "WAITING_HUMAN_GATE":
            return summary, EXIT_HUMAN_GATE
        if status == "STALE":
            return summary, EXIT_STALE_BINDING
        if status == "UNKNOWN_EXTERNAL_OUTCOME":
            return summary, EXIT_UNKNOWN_EXTERNAL_OUTCOME
        return summary, EXIT_OK if status == "PASS" else EXIT_INVALID_OR_TECHNICAL
    except Exception as exc:  # fail closed; no retry and no approval write
        status, code = _status_from_exception(exc)
        summary = _base_summary(phase=phase, run_id=run_id)
        try:
            summary["projectId"] = load_project(project_path).project_id
        except Exception:
            pass
        summary["status"] = status
        summary["error"] = {"code": status.lower(), "message": str(exc)}
        summary["failures"] = [{"scope": phase, "error": str(exc)}]
        summary["failureCount"] = 1
        return summary, code


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="运行一个可恢复的确定性 Phase 4 阶段")
    parser.add_argument("--project", required=True, help="project 根目录")
    parser.add_argument("--phase", required=True, choices=sorted(PHASE_REGISTRY))
    parser.add_argument("--config", help="workspace-config.json")
    parser.add_argument("--run-id")
    parser.add_argument("--formal-context-receipt", type=Path)
    parser.add_argument("--formal-context-run-id")
    parser.add_argument("--review-policy", choices=("user_first", "agent_first"), default=None)
    parser.add_argument("--force-deep", action="store_true", help="忽略可复用技术 receipt 并重新深验")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        summary, exit_code = run_phase(
            project_path=args.project,
            phase=args.phase,
            config_path=args.config,
            run_id=args.run_id,
            formal_context_receipt=args.formal_context_receipt,
            formal_context_run_id=args.formal_context_run_id,
            review_policy=args.review_policy,
            force_deep=args.force_deep,
        )
    except (KeyboardInterrupt, BrokenPipeError):
        # An interrupted run is intentionally not retried or marked approved.
        summary = _base_summary(phase=args.phase, run_id=args.run_id)
        summary["status"] = "FAIL"
        summary["error"] = {"code": "interrupted", "message": "runner interrupted; resume explicitly"}
        exit_code = EXIT_INVALID_OR_TECHNICAL
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())


__all__ = [
    "EXIT_HUMAN_GATE",
    "EXIT_INVALID_OR_TECHNICAL",
    "EXIT_OK",
    "EXIT_STALE_BINDING",
    "EXIT_UNKNOWN_EXTERNAL_OUTCOME",
    "PHASE_REGISTRY",
    "RUNNER_CONTRACT",
    "main",
    "phase_registry",
    "run_phase",
]
