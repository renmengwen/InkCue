#!/usr/bin/env python3
"""Identity-bound technical evidence and human approval for annotation review."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

try:
    from .project_workspace import (
        Project,
        load_project,
        sha256_file,
        sha256_json,
        write_json_atomic,
    )
    from .render_timing import (
        FormalSceneRender,
        FormalValidationContext,
        RenderTimingError,
        build_formal_validation_context,
        resolve_formal_scenes,
        validate_formal_context_current,
    )
except ImportError:  # pragma: no cover - direct script execution
    from project_workspace import (
        Project,
        load_project,
        sha256_file,
        sha256_json,
        write_json_atomic,
    )
    from render_timing import (
        FormalSceneRender,
        FormalValidationContext,
        RenderTimingError,
        build_formal_validation_context,
        resolve_formal_scenes,
        validate_formal_context_current,
    )


ANNOTATION_REVIEW_SCHEMA_VERSION = 2
ANNOTATION_REVIEW_KIND = "annotation-review"
TECHNICAL_MANIFEST_FILE = "manifests/annotation-review-manifest.json"
APPROVAL_FILE = "manifests/annotation-review-approval.json"
CONTACT_SHEET_FILE = "previews/annotation-preview-contact-sheet.png"


class AnnotationReviewError(ValueError):
    """Annotation review evidence is missing, invalid, or stale."""


class AnnotationReviewApprovalRequired(AnnotationReviewError):
    """The current annotation review identity lacks matching human approval."""


def _load_mapping(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AnnotationReviewError(f"无法读取 {label}") from exc
    if not isinstance(value, dict):
        raise AnnotationReviewError(f"{label} 顶层必须是 JSON 对象")
    return value


def _project(value: Project | str | Path) -> Project:
    return value if isinstance(value, Project) else load_project(value)


def _preview_file(project: Project, formal: FormalSceneRender) -> Path:
    scene = next(
        item for item in project.plan["scenes"] if item["sceneId"] == formal.scene_id
    )
    stem = Path(scene["outputFile"]).stem
    return project.path(f"previews/{stem}-annotation-preview.png")


def _identity_payload(
    project: Project,
    formals: Sequence[FormalSceneRender],
    context: FormalValidationContext,
) -> dict[str, Any]:
    if [formal.scene_id for formal in formals] != [
        scene["sceneId"] for scene in project.plan["scenes"]
    ]:
        raise AnnotationReviewError("annotation review 必须按 generation plan 覆盖全部场景")
    validate_formal_context_current(project, context)
    scenes: list[dict[str, Any]] = []
    for formal in formals:
        preview = _preview_file(project, formal)
        if not preview.is_file():
            raise AnnotationReviewError(f"缺少 current 区域预览: {formal.scene_id}")
        scenes.append(
            {
                "sceneId": formal.scene_id,
                "annotationSha256": sha256_file(formal.annotation_path),
                "previewFile": preview.relative_to(project.root).as_posix(),
                "previewSha256": sha256_file(preview),
            }
        )
    contact = project.path(CONTACT_SHEET_FILE)
    if not contact.is_file():
        raise AnnotationReviewError("缺少 annotation preview contact sheet")
    return {
        "schemaVersion": ANNOTATION_REVIEW_SCHEMA_VERSION,
        "kind": ANNOTATION_REVIEW_KIND,
        "projectId": project.project_id,
        "voiceoverMode": project.voiceover_mode,
        "generationPlanSha256": sha256_file(project.plan_path),
        "timingPlanSha256": context.timing_plan_sha256,
        "renderProfileSha256": context.render_profile_sha256,
        "activeTimelineSha256": sha256_json(context.active_timeline),
        "audioSha256": context.audio_sha256,
        "fullApprovalIdentityHash": context.full_approval_identity_hash,
        "voiceManifestSha256": context.voice_manifest_sha256,
        "scenes": scenes,
        "contactSheetFile": CONTACT_SHEET_FILE,
        "contactSheetSha256": sha256_file(contact),
    }


def write_annotation_review_technical(
    project: Project,
    formals: Sequence[FormalSceneRender],
    context: FormalValidationContext,
) -> dict[str, Any]:
    """Persist only deterministic review evidence; never writes human approval."""
    payload = _identity_payload(project, formals, context)
    identity = sha256_json(payload)
    manifest = {
        "schemaVersion": ANNOTATION_REVIEW_SCHEMA_VERSION,
        "kind": ANNOTATION_REVIEW_KIND,
        "status": "current_technical",
        "identityHash": identity,
        "identityPayload": payload,
    }
    write_json_atomic(project.path(TECHNICAL_MANIFEST_FILE), manifest)
    return manifest


def inspect_current_annotation_review(
    project_or_root: Project | str | Path,
    *,
    context: FormalValidationContext | None = None,
    formals: Sequence[FormalSceneRender] | None = None,
) -> dict[str, Any]:
    """Rebuild the current identity and require the persisted technical receipt."""
    project = _project(project_or_root)
    frozen = context or build_formal_validation_context(project)
    scene_ids = [scene["sceneId"] for scene in project.plan["scenes"]]
    resolved = tuple(formals) if formals is not None else resolve_formal_scenes(
        project, scene_ids, context=frozen
    )
    if [formal.scene_id for formal in resolved] != scene_ids:
        raise AnnotationReviewError("annotation review formal scenes 与 generation plan 顺序不一致")
    payload = _identity_payload(project, resolved, frozen)
    identity = sha256_json(payload)
    technical = _load_mapping(
        project.path(TECHNICAL_MANIFEST_FILE), "annotation review technical manifest"
    )
    if (
        technical.get("schemaVersion") != ANNOTATION_REVIEW_SCHEMA_VERSION
        or technical.get("kind") != ANNOTATION_REVIEW_KIND
        or technical.get("status") != "current_technical"
        or technical.get("identityHash") != identity
        or technical.get("identityPayload") != payload
    ):
        raise AnnotationReviewError("annotation review technical evidence 已 stale")
    return {
        "project": project,
        "identityHash": identity,
        "identityPayload": payload,
        "technicalManifest": technical,
    }


def approve_current_annotation_review(
    project_or_root: Project | str | Path,
    identity_hash: str,
) -> dict[str, Any]:
    """Persist explicit approval for exactly the supplied current review identity."""
    if not isinstance(identity_hash, str) or len(identity_hash) != 64:
        raise AnnotationReviewApprovalRequired(
            "--identity-hash 必须是 64 位 current annotation review identity"
        )
    inspection = inspect_current_annotation_review(project_or_root)
    if identity_hash != inspection["identityHash"]:
        raise AnnotationReviewApprovalRequired(
            "提交的 annotation review identity 与 current review 不一致"
        )
    approval = {
        "schemaVersion": ANNOTATION_REVIEW_SCHEMA_VERSION,
        "reviewKind": ANNOTATION_REVIEW_KIND,
        "approved": True,
        "identityHash": identity_hash,
        "approvedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    project: Project = inspection["project"]
    write_json_atomic(project.path(APPROVAL_FILE), approval)
    return approval


def require_current_annotation_review_approval(
    project_or_root: Project | str | Path,
    *,
    context: FormalValidationContext | None = None,
    formals: Sequence[FormalSceneRender] | None = None,
) -> dict[str, Any]:
    """Read-only downstream Gate for the current annotation review bundle."""
    try:
        inspection = inspect_current_annotation_review(
            project_or_root,
            context=context,
            formals=formals,
        )
    except (AnnotationReviewError, RenderTimingError) as exc:
        raise AnnotationReviewApprovalRequired(
            "annotation review technical evidence 已 stale 或缺失"
        ) from exc
    project: Project = inspection["project"]
    try:
        approval = _load_mapping(
            project.path(APPROVAL_FILE), "annotation review approval"
        )
    except AnnotationReviewError as exc:
        raise AnnotationReviewApprovalRequired("缺少 annotation review 人工批准") from exc
    if (
        approval.get("schemaVersion") != ANNOTATION_REVIEW_SCHEMA_VERSION
        or approval.get("reviewKind") != ANNOTATION_REVIEW_KIND
        or approval.get("approved") is not True
        or approval.get("identityHash") != inspection["identityHash"]
    ):
        raise AnnotationReviewApprovalRequired("annotation review 人工批准已 stale")
    return {
        "approved": True,
        "identityHash": inspection["identityHash"],
        "approval": approval,
    }


__all__ = [
    "ANNOTATION_REVIEW_KIND",
    "ANNOTATION_REVIEW_SCHEMA_VERSION",
    "APPROVAL_FILE",
    "AnnotationReviewApprovalRequired",
    "AnnotationReviewError",
    "TECHNICAL_MANIFEST_FILE",
    "approve_current_annotation_review",
    "inspect_current_annotation_review",
    "require_current_annotation_review_approval",
    "write_annotation_review_technical",
]
