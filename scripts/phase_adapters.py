#!/usr/bin/env python3
"""Deterministic Phase 4 adapters used by the optional phase runner.

This module is deliberately a thin coordinator-facing boundary around the
existing project APIs.  It does not own a CLI, does not dispatch providers,
and never writes an approval.  The independent command line scripts remain
the recovery/debugging path; callers may use :func:`run_annotation_preview`
to avoid starting a new Python process for each deterministic step.
"""

from __future__ import annotations

import re
import time
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

try:
    from .generate_annotation_previews import generate_annotation_preview_batch
    from .project_workspace import Project, WorkspaceConfig
    from .burn_subtitles import burn_project
    from .merge_scenes import merge_project_scenes, ordered_scene_inputs
    from .mux_voiceover import mux_project
    from .scene_review import assert_current_scene_review_approval
    from .validate_final_media import validate_project_final_media
    from .render_timing import (
        FormalValidationContext,
        RenderTimingError,
        build_formal_validation_context,
        load_formal_validation_context_receipt,
        resolve_formal_scenes,
        write_formal_validation_context_receipt,
    )
except ImportError:  # pragma: no cover - direct script execution
    from generate_annotation_previews import generate_annotation_preview_batch  # type: ignore
    from project_workspace import Project, WorkspaceConfig  # type: ignore
    from burn_subtitles import burn_project  # type: ignore
    from merge_scenes import merge_project_scenes, ordered_scene_inputs  # type: ignore
    from mux_voiceover import mux_project  # type: ignore
    from scene_review import assert_current_scene_review_approval  # type: ignore
    from validate_final_media import validate_project_final_media  # type: ignore
    from render_timing import (  # type: ignore
        FormalValidationContext,
        RenderTimingError,
        build_formal_validation_context,
        load_formal_validation_context_receipt,
        resolve_formal_scenes,
        write_formal_validation_context_receipt,
    )


_RUN_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")


class PhaseAdapterError(ValueError):
    """Stable adapter error; no project approval is written on failure."""


def _safe_run_id(value: str) -> str:
    if not isinstance(value, str) or _RUN_ID_RE.fullmatch(value) is None:
        raise PhaseAdapterError("phase runId 必须是 1-64 位安全标识")
    return value


def _new_run_id() -> str:
    # Keep the generated id human-recognisable while staying inside the
    # formal receipt's conservative 64-character contract.
    return f"phase-annotation-{uuid.uuid4().hex[:20]}"


def _failure_projection(summary: Mapping[str, Any]) -> list[dict[str, Any]]:
    failures: list[dict[str, Any]] = []
    for scene in summary.get("scenes", ()):
        if not isinstance(scene, Mapping):
            continue
        if scene.get("status") not in {"published_current_technical"}:
            failures.append(dict(scene))
    contact_error = summary.get("contactSheetError")
    if contact_error:
        failures.append({"scope": "contactSheet", "error": contact_error})
    return failures


def _adapt_summary(
    project: Project,
    context: FormalValidationContext,
    summary: Mapping[str, Any],
    *,
    run_id: str,
    validation_mode: str,
) -> dict[str, Any]:
    """Project the legacy preview summary into the shared phase contract."""

    status = str(summary.get("status", "FAIL"))
    current_identity = summary.get("annotationReviewIdentitySha256")
    if not isinstance(current_identity, str) or not current_identity:
        current_identity = summary.get("annotationBindingSha256")
    contact = summary.get("contactSheet")
    review_manifest = "manifests/annotation-review-manifest.json"
    artifact_paths = [
        item
        for item in (
            contact,
            review_manifest if summary.get("annotationReviewIdentitySha256") else None,
        )
        if isinstance(item, str) and item
    ]
    deep_reused = validation_mode == "binding"
    result: dict[str, Any] = {
        "schemaVersion": 1,
        "phase": "annotation-preview",
        "status": status,
        "projectId": project.project_id,
        "runId": run_id,
        "taskCount": int(summary.get("taskCount") or 0),
        "configuredConcurrency": summary.get("configuredConcurrency"),
        "effectiveConcurrency": summary.get("effectiveConcurrency", 0),
        "peakConcurrency": summary.get(
            "peakConcurrency", summary.get("peakActiveWorkers", 0)
        ),
        "successCount": int(summary.get("publishedCount") or 0),
        "failureCount": int(summary.get("failedCount") or 0),
        "partialSuccess": bool(summary.get("partialSuccess")),
        "currentIdentity": current_identity,
        "approvalWritten": False,
        "userConfirmationRequired": True,
        "nextGate": summary.get("nextHumanGate"),
        "failures": _failure_projection(summary),
        "artifact": contact,
        "artifactUrl": contact,
        "previewUrl": contact,
        "artifactPaths": artifact_paths,
        "contactSheet": contact,
        "reviewManifest": review_manifest
        if summary.get("annotationReviewIdentitySha256")
        else None,
        "formalValidationMode": validation_mode,
        "formalValidationReceipt": summary.get("formalValidationReceipt"),
        "formalValidationRunId": summary.get("formalValidationRunId") or run_id,
        "deepValidationSkipped": deep_reused,
        "deepValidationReused": deep_reused,
        "deepValidationBasis": (
            "同 run、未过期且 current binding 完全匹配的 formal validation receipt"
            if deep_reused
            else "本次运行已完成 timing/voice/annotation deep validation"
        ),
        "deepValidationSkipReason": (
            "receipt current binding 完全匹配，跳过重复 annotation deep validation"
            if deep_reused
            else None
        ),
        "confirmationRequest": (
            f"请明确确认 current annotation review identity "
            f"{current_identity}；确认前不得写入 approval。"
            if current_identity
            else "技术校验未完成，不能进入 annotation review 人工确认。"
        ),
    }
    # Preserve useful, stage-specific data (scene order, per-scene result,
    # semantic review findings) without inventing another status vocabulary.
    for key in (
        "scenes",
        "publishedOrder",
        "publishedCount",
        "failedCount",
        "annotationBindingSha256",
        "annotationReviewIdentitySha256",
        "reviewPolicy",
        "semanticReview",
        "contactSheetSha256",
        "contactSheetError",
    ):
        if key in summary:
            result[key] = summary[key]
    return result


def run_annotation_preview(
    workspace: WorkspaceConfig,
    project: Project,
    *,
    run_id: str | None = None,
    formal_context_receipt: str | Path | None = None,
    review_policy: str | None = None,
    allow_v1_disabled_compat: bool = False,
) -> dict[str, Any]:
    """Run deterministic annotation technical validation and preview generation.

    A missing receipt performs one deep validation of the complete current
    project and writes a short-lived receipt.  A supplied receipt is loaded and
    current-bound before any preview work; stale/mismatched evidence raises and
    cannot silently fall back to PASS.  In either mode this function stops
    after writing technical review evidence and returns the explicit human
    confirmation boundary.  It never calls an image/TTS provider or approval
    writer.
    """

    if formal_context_receipt is not None and run_id is None:
        raise PhaseAdapterError("提供 formal_context_receipt 时必须同时提供 runId")
    requested_run_id = _safe_run_id(run_id) if run_id is not None else _new_run_id()

    if formal_context_receipt is not None:
        context = load_formal_validation_context_receipt(
            project,
            formal_context_receipt,
            expected_run_id=requested_run_id,
        )
        validation_mode = "binding"
    else:
        context = build_formal_validation_context(project)
        # resolve_formal_scenes performs the complete scene-local technical
        # validation exactly once before the coordinator publishes a receipt.
        formals = resolve_formal_scenes(
            project,
            [scene["sceneId"] for scene in project.plan["scenes"]],
            context=context,
            allow_v1_disabled_compat=allow_v1_disabled_compat,
        )
        context, _receipt_path = write_formal_validation_context_receipt(
            project,
            context,
            run_id=requested_run_id,
            validated_formals=formals,
        )
        validation_mode = "deep"

    summary = generate_annotation_preview_batch(
        workspace,
        project,
        review_policy=review_policy,
        allow_v1_disabled_compat=allow_v1_disabled_compat,
        context=context,
    )
    return _adapt_summary(
        project,
        context,
        summary,
        run_id=requested_run_id,
        validation_mode=validation_mode,
    )


def run_final_delivery(
    workspace: WorkspaceConfig,
    project: Project,
    *,
    run_id: str | None = None,
    force_deep: bool = False,
) -> dict[str, Any]:
    """连续执行 merge/burn/mux/validate，并交回冻结模式对应的最终批准动作。"""

    requested_run_id = (
        _safe_run_id(run_id)
        if run_id is not None
        else f"phase-final-{uuid.uuid4().hex[:20]}"
    )
    started = time.perf_counter()
    timings: dict[str, int] = {}
    failures: list[dict[str, Any]] = []
    last_completed: str | None = None
    outputs: dict[str, Any] = {}

    def execute(step: str, action: Any) -> bool:
        nonlocal last_completed
        step_started = time.perf_counter()
        try:
            value = action()
            outputs[step] = value
            last_completed = step
            return True
        except Exception as exc:
            failures.append(
                {"scope": step, "error": f"{type(exc).__name__}: {exc}"}
            )
            return False
        finally:
            timings[step] = round((time.perf_counter() - step_started) * 1000)

    scene_inputs = ordered_scene_inputs(project)
    ok = execute(
        "preflight",
        lambda: assert_current_scene_review_approval(project, inputs=scene_inputs),
    )
    if ok:
        ok = execute(
            "merge",
            lambda: merge_project_scenes(
                project,
                inputs=scene_inputs,
                force_deep=force_deep,
                run_id=requested_run_id,
            ),
        )
    if ok:
        ok = execute(
            "burnSubtitles",
            lambda: burn_project(
                project.root,
                run_id=f"subtitle-{requested_run_id}",
                force_deep=force_deep,
                subtitle_preset=workspace.video_encoding.subtitle_preset,
            ),
        )
    if ok and project.voiceover_mode in {"edge-tts", "minimax", "doubao"}:
        ok = execute(
            "muxVoiceover",
            lambda: mux_project(
                project.root,
                run_id=f"mux-{requested_run_id}",
                force_deep=force_deep,
            ),
        )
    else:
        timings["muxVoiceover"] = 0
        if ok:
            outputs["muxVoiceover"] = {"skipped": True, "reason": "voiceover_disabled"}
    if ok:
        ok = execute(
            "validateFinalMedia",
            lambda: validate_project_final_media(
                project.root,
                configured_concurrency=workspace.for_stage("finalMediaValidation"),
                force_deep=force_deep,
            ),
        )

    for step in (
        "preflight",
        "merge",
        "burnSubtitles",
        "muxVoiceover",
        "validateFinalMedia",
    ):
        timings.setdefault(step, 0)
    timings["total"] = round((time.perf_counter() - started) * 1000)
    validation = outputs.get("validateFinalMedia")
    final_identity = (
        validation.get("finalIdentitySha256")
        if isinstance(validation, Mapping)
        else None
    )
    artifact = project.path("output/final.mp4")
    initial = project.metadata.get("initialApproval")
    autonomous = (
        project.initial_approval_completed
        and project.agent_approval_enabled
        and isinstance(initial, Mapping)
        and initial.get("status") == "approved"
    )
    return {
        "schemaVersion": 1,
        "phase": "final-delivery",
        "status": "PASS" if ok else "FAIL",
        "projectId": project.project_id,
        "runId": requested_run_id,
        "taskCount": 5 if project.voiceover_mode in {"edge-tts", "minimax", "doubao"} else 4,
        "successCount": len([name for name in outputs if name != "muxVoiceover" or project.voiceover_mode != "disabled"]),
        "failureCount": len(failures),
        "partialSuccess": bool(last_completed and not ok),
        "currentIdentity": final_identity,
        "approvalWritten": False,
        "approvalActionRequired": bool(ok),
        "userConfirmationRequired": bool(ok and not autonomous),
        "nextGate": (
            "final_technical_approval" if ok and autonomous else
            "final_media_review" if ok else None
        ),
        "approvalBasis": (
            "technical_after_initial_approval"
            if autonomous and project.voiceover_mode == "disabled"
            else "technical_after_user_sample"
            if autonomous
            else "human_full_media_review"
        ),
        "failures": failures,
        "artifact": str(artifact) if ok else None,
        "artifactPaths": [str(artifact)] if ok else [],
        "timingsMs": timings,
        "lastCompletedStep": last_completed,
        "outputs": outputs,
        "confirmationRequest": (
            (
                f"current final identity {final_identity} 已通过技术链；"
                "coordinator 可按用户样音授权调用 approve_final_media，"
                "不得表述为已完整听审。"
                if project.voiceover_mode != "disabled"
                else f"current final identity {final_identity} 已通过技术链；"
                "coordinator 可按初始静音方案授权调用 approve_final_media。"
            )
            if ok and autonomous
            else (
                f"请完整看片后确认 final identity {final_identity}；确认前不得写入 finalApproval。"
                if project.voiceover_mode == "disabled"
                else f"请完整看片并听音后确认 final identity {final_identity}；确认前不得写入 finalApproval。"
            )
            if ok
            else "最终技术链未完成，不能进入成片人工批准。"
        ),
    }


__all__ = [
    "PhaseAdapterError",
    "run_annotation_preview",
    "run_final_delivery",
]
