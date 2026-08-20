#!/usr/bin/env python3
"""Project-aware annotation timing validation and formal scene render identity."""
from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

try:
    from .project_workspace import Project, sha256_file, sha256_json, write_json_atomic
    from .annotation_contract import (
        AnnotationContractError,
        normalize_legacy_visual_elements,
        validate_visual_elements,
    )
    from .validation_receipts import (
        ReceiptValidationError,
        receipt_sha256,
        require_current_bindings,
        validate_receipt_window,
    )
except ImportError:  # pragma: no cover - direct script execution
    from project_workspace import Project, sha256_file, sha256_json, write_json_atomic
    from annotation_contract import (
        AnnotationContractError,
        normalize_legacy_visual_elements,
        validate_visual_elements,
    )
    from validation_receipts import (
        ReceiptValidationError,
        receipt_sha256,
        require_current_bindings,
        validate_receipt_window,
    )


RENDER_CONTRACT_VERSION = "whiteboard-project-scene-render-v1"
RENDER_MANIFEST_FILE = "manifests/render-manifest.json"
FORMAL_CONTEXT_RECEIPT_CONTRACT_VERSION = "whiteboard-formal-validation-context-receipt-v1"
FORMAL_CONTEXT_VALIDATOR_CONTRACT = "whiteboard-formal-validation-context-validator-v1"
FORMAL_CONTEXT_RECEIPT_TTL_SECONDS = 600
FORMAL_CONTEXT_RECEIPT_MAX_TTL_SECONDS = 3600
_FORMAL_RUN_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")


class RenderTimingError(ValueError):
    """The project, annotation, or current timing identity is not renderable."""


@dataclass(frozen=True)
class FormalValidationContext:
    """一次 batch 冻结并复用的正式渲染全局证据。"""

    timing_plan_sha256: str
    timing_plan_file: str | None
    render_profile_sha256: str
    active_timeline: dict[str, Any]
    audio_sha256: str | None
    full_approval_identity_hash: str | None
    voice_manifest_sha256: str | None = None
    project_id: str = ""
    generation_plan_sha256: str = ""
    scene_order: tuple[str, ...] = ()
    validator_contract: str = FORMAL_CONTEXT_VALIDATOR_CONTRACT
    annotation_bindings: tuple[tuple[str, str, int], ...] = ()
    receipt_run_id: str | None = None
    receipt_sha256: str | None = None
    receipt_file: str | None = None
    receipt_created_at: str | None = None
    receipt_expires_at: str | None = None


@dataclass(frozen=True)
class FormalSceneRender:
    project: Project
    scene_id: str
    image_path: Path
    annotation_path: Path
    annotation_sha256: str
    annotation_bytes: int
    output_path: Path
    timing_scene: dict[str, Any]
    timing_plan_sha256: str
    timing_plan_file: str | None
    render_profile_sha256: str
    active_timeline: dict[str, Any]
    audio_sha256: str | None
    full_approval_identity_hash: str | None
    annotation: dict[str, Any]
    compatibility_mode: str | None


def _load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RenderTimingError(f"无法读取{label} {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise RenderTimingError(f"{label}顶层必须是 JSON 对象")
    return value


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(ch in "0123456789abcdef" for ch in value)


def _scene(project: Project, scene_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    generation = next((item for item in project.plan["scenes"] if item.get("sceneId") == scene_id), None)
    timing = next((item for item in project.timing_plan["scenes"] if item.get("sceneId") == scene_id), None)
    if generation is None or timing is None:
        raise RenderTimingError(f"sceneId 不存在于 current generation/timing plan: {scene_id}")
    return generation, timing


def _validate_audio_approval(project: Project) -> tuple[str, str]:
    try:
        try:
            from .generate_voiceover import validate_current_voiceover
        except ImportError:  # pragma: no cover - direct script execution
            from generate_voiceover import validate_current_voiceover
        current = validate_current_voiceover(project, require_full=True)
    except Exception as exc:
        raise RenderTimingError(f"正式音频渲染要求 current approve-full: {exc}") from exc
    if current.get("fullApproved") is not True:
        raise RenderTimingError("正式音频渲染要求 current approve-full")
    audio_sha = current.get("audioSha256")
    approval_identity = current.get("fullIdentityHash")
    if not _is_sha256(audio_sha) or not _is_sha256(approval_identity):
        raise RenderTimingError("Edge current audio/full approval identity 无效")
    return audio_sha, approval_identity


def build_formal_validation_context(project: Project) -> FormalValidationContext:
    """深验一次全局 timing/voice evidence，供逐幕校验复用。"""

    timing_plan_sha = (
        sha256_file(project.timing_plan_path)
        if project.timing_plan_persisted
        else sha256_json(project.timing_plan)
    )
    active = copy.deepcopy(project.timing_plan["activeTimeline"])
    audio_sha: str | None = None
    approval_identity: str | None = None
    if project.voiceover_mode in {"edge-tts", "minimax"}:
        if active.get("kind") not in {"edge-tts-audio-timeline", "audio-authoritative-timeline"}:
            raise RenderTimingError("正式音频渲染只接受 current audio timeline timing plan")
        audio_sha, approval_identity = _validate_audio_approval(project)
    elif active.get("kind") != "source-srt":
        raise RenderTimingError("Disabled 正式渲染只接受 current source-srt timing plan")
    voice_manifest_sha: str | None = None
    if project.voiceover_mode in {"edge-tts", "minimax"}:
        voice_manifest = project.path("manifests/voice-manifest.json")
        if voice_manifest.is_file():
            voice_manifest_sha = sha256_file(voice_manifest)
    return FormalValidationContext(
        timing_plan_sha256=timing_plan_sha,
        timing_plan_file=("planning/timing-plan.json" if project.timing_plan_persisted else None),
        render_profile_sha256=sha256_json(project.render_profile),
        active_timeline=active,
        audio_sha256=audio_sha,
        full_approval_identity_hash=approval_identity,
        voice_manifest_sha256=voice_manifest_sha,
        project_id=project.project_id,
        generation_plan_sha256=sha256_file(project.plan_path),
        scene_order=tuple(scene["sceneId"] for scene in project.plan["scenes"]),
    )


def _formal_annotation_bindings(
    project: Project,
    scene_order: tuple[str, ...],
) -> tuple[tuple[str, str, int], ...]:
    generation = {scene["sceneId"]: scene for scene in project.plan["scenes"]}
    bindings: list[tuple[str, str, int]] = []
    for scene_id in scene_order:
        scene = generation.get(scene_id)
        if scene is None:
            raise RenderTimingError(f"formal receipt sceneId 不在 current generation plan: {scene_id}")
        path = project.path(
            Path("scenes") / f"{Path(scene['outputFile']).stem}.annotation.json"
        )
        if path.is_symlink() or not path.is_file():
            raise RenderTimingError(f"formal receipt annotation 不存在或不是普通文件: {scene_id}")
        bindings.append((scene_id, sha256_file(path), path.stat().st_size))
    return tuple(bindings)


def _formal_context_bindings(
    project: Project,
    context: FormalValidationContext,
) -> dict[str, Any]:
    return {
        "projectId": context.project_id,
        "generationPlanSha256": context.generation_plan_sha256,
        "timingPlanSha256": context.timing_plan_sha256,
        "renderProfileSha256": context.render_profile_sha256,
        "activeTimeline": copy.deepcopy(context.active_timeline),
        "voice": {
            "mode": project.voiceover_mode,
            "audioSha256": context.audio_sha256,
            "fullApprovalIdentityHash": context.full_approval_identity_hash,
            "voiceManifestSha256": context.voice_manifest_sha256,
        },
        "sceneOrder": list(context.scene_order),
        "annotations": [
            {"sceneId": scene_id, "sha256": sha256, "bytes": byte_count}
            for scene_id, sha256, byte_count in context.annotation_bindings
        ],
        "validatorContract": context.validator_contract,
    }


def validate_formal_context_current(
    project: Project,
    context: FormalValidationContext,
    *,
    receipt_now: str | datetime | None = None,
) -> None:
    """只做字节/binding current 核对，不再次调用语音 deep validator。"""

    if context.validator_contract != FORMAL_CONTEXT_VALIDATOR_CONTRACT:
        raise RenderTimingError("formal validator contract 已 stale")
    if context.receipt_sha256 is not None:
        if not all(
            (
                context.receipt_run_id,
                context.receipt_file,
                context.receipt_created_at,
                context.receipt_expires_at,
            )
        ):
            raise RenderTimingError("formal receipt 元数据不完整")
        try:
            validate_receipt_window(
                created_at=context.receipt_created_at,
                expires_at=context.receipt_expires_at,
                now=receipt_now,
                require_expiry=True,
                label="formal receipt",
            )
        except ReceiptValidationError as exc:
            raise RenderTimingError(str(exc)) from exc
        expected_path = formal_validation_context_receipt_path(
            project, context.receipt_run_id or ""
        )
        expected_relative = expected_path.relative_to(project.root).as_posix()
        if context.receipt_file != expected_relative:
            raise RenderTimingError("formal receipt 必须绑定同 run 固定路径")
        raw_path = project.root / Path(context.receipt_file)
        current_path = project.root
        for part in Path(context.receipt_file).parts:
            current_path = current_path / part
            if current_path.is_symlink():
                raise RenderTimingError("formal receipt 路径不得包含符号链接")
        if not raw_path.is_file():
            raise RenderTimingError("formal receipt 落盘证据不存在")
        persisted = _load_json(raw_path, "formal receipt")
        required_keys = {
            "contractVersion",
            "validatorContract",
            "runId",
            "projectId",
            "createdAt",
            "expiresAt",
            "bindings",
            "receiptSha256",
        }
        if set(persisted) != required_keys:
            raise RenderTimingError("formal receipt schema 不一致")
        if (
            persisted.get("contractVersion")
            != FORMAL_CONTEXT_RECEIPT_CONTRACT_VERSION
            or persisted.get("validatorContract")
            != FORMAL_CONTEXT_VALIDATOR_CONTRACT
        ):
            raise RenderTimingError("formal receipt contract 已 stale 或属于旧格式")
        if (
            persisted.get("runId") != context.receipt_run_id
            or persisted.get("projectId") != context.project_id
            or persisted.get("createdAt") != context.receipt_created_at
            or persisted.get("expiresAt") != context.receipt_expires_at
            or persisted.get("receiptSha256") != context.receipt_sha256
            or persisted.get("receiptSha256") != receipt_sha256(persisted)
        ):
            raise RenderTimingError("formal receipt 落盘证据与 context 不一致")
        try:
            require_current_bindings(
                persisted.get("bindings"),
                _formal_context_bindings(project, context),
                label="formal receipt",
            )
        except ReceiptValidationError as exc:
            raise RenderTimingError(str(exc)) from exc
    if context.project_id != project.project_id:
        raise RenderTimingError("formal context projectId 与 current project 不一致")
    if sha256_file(project.plan_path) != context.generation_plan_sha256:
        raise RenderTimingError("batch 期间 generation plan 已变化")
    current_scene_order = tuple(scene["sceneId"] for scene in project.plan["scenes"])
    if current_scene_order != context.scene_order:
        raise RenderTimingError("batch 期间 scene 顺序已变化")

    timing_sha = (
        sha256_file(project.timing_plan_path)
        if project.timing_plan_persisted
        else sha256_json(project.timing_plan)
    )
    if timing_sha != context.timing_plan_sha256:
        raise RenderTimingError("batch 期间 timing plan 已变化")
    if sha256_json(project.render_profile) != context.render_profile_sha256:
        raise RenderTimingError("batch 期间 render profile 已变化")
    if project.timing_plan.get("activeTimeline") != context.active_timeline:
        raise RenderTimingError("batch 期间 active timeline 已变化")
    if project.voiceover_mode in {"edge-tts", "minimax"}:
        audio_path = project.path("audio/narration.wav")
        if not audio_path.is_file() or sha256_file(audio_path) != context.audio_sha256:
            raise RenderTimingError("batch 期间 current narration.wav 已变化")
        manifest_path = project.path("manifests/voice-manifest.json")
        manifest = _load_json(manifest_path, "voice manifest")
        if sha256_file(manifest_path) != context.voice_manifest_sha256:
            raise RenderTimingError("batch 期间 voice manifest 已变化")
        approval = manifest.get("fullApproval")
        if (
            not isinstance(approval, Mapping)
            or approval.get("approved") is not True
            or approval.get("identityHash") != context.full_approval_identity_hash
        ):
            raise RenderTimingError("batch 期间 full approval identity 已变化")
    if context.annotation_bindings:
        if _formal_annotation_bindings(project, context.scene_order) != context.annotation_bindings:
            raise RenderTimingError("batch 期间 annotation binding 已变化")


def _validate_formal_run_id(run_id: str) -> str:
    if not isinstance(run_id, str) or _FORMAL_RUN_ID_RE.fullmatch(run_id) is None:
        raise RenderTimingError("formal receipt runId 必须是 1-64 位安全标识")
    return run_id


def formal_validation_context_receipt_path(project: Project, run_id: str) -> Path:
    safe_run_id = _validate_formal_run_id(run_id)
    work = project.metadata["paths"]["work"]
    return project.path(Path(work) / f"formal-context-{safe_run_id}" / "receipt.json")


def _parse_formal_receipt_context(
    receipt: Mapping[str, Any],
    *,
    receipt_file: str,
) -> FormalValidationContext:
    bindings = receipt.get("bindings")
    if not isinstance(bindings, Mapping):
        raise RenderTimingError("formal receipt.bindings 必须是对象")
    scene_order = bindings.get("sceneOrder")
    annotations = bindings.get("annotations")
    voice = bindings.get("voice")
    active = bindings.get("activeTimeline")
    if (
        not isinstance(scene_order, list)
        or any(not isinstance(item, str) or not item for item in scene_order)
        or len(scene_order) != len(set(scene_order))
    ):
        raise RenderTimingError("formal receipt sceneOrder 无效")
    if not isinstance(annotations, list) or not isinstance(voice, Mapping) or not isinstance(active, Mapping):
        raise RenderTimingError("formal receipt annotations/voice/activeTimeline 无效")
    annotation_bindings: list[tuple[str, str, int]] = []
    for index, item in enumerate(annotations):
        if not isinstance(item, Mapping):
            raise RenderTimingError(f"formal receipt annotations[{index}] 必须是对象")
        scene_id = item.get("sceneId")
        sha = item.get("sha256")
        byte_count = item.get("bytes")
        if (
            not isinstance(scene_id, str)
            or not _is_sha256(sha)
            or isinstance(byte_count, bool)
            or not isinstance(byte_count, int)
            or byte_count <= 0
        ):
            raise RenderTimingError(f"formal receipt annotations[{index}] binding 无效")
        annotation_bindings.append((scene_id, sha, byte_count))
    if [item[0] for item in annotation_bindings] != scene_order:
        raise RenderTimingError("formal receipt annotation 顺序必须与 sceneOrder 一致")
    required_hashes = {
        "generationPlanSha256": bindings.get("generationPlanSha256"),
        "timingPlanSha256": bindings.get("timingPlanSha256"),
        "renderProfileSha256": bindings.get("renderProfileSha256"),
    }
    if any(not _is_sha256(value) for value in required_hashes.values()):
        raise RenderTimingError("formal receipt generation/timing/render binding 无效")
    for key in ("audioSha256", "fullApprovalIdentityHash", "voiceManifestSha256"):
        if voice.get(key) is not None and not _is_sha256(voice.get(key)):
            raise RenderTimingError(f"formal receipt voice.{key} 无效")
    return FormalValidationContext(
        timing_plan_sha256=required_hashes["timingPlanSha256"],
        timing_plan_file="planning/timing-plan.json",
        render_profile_sha256=required_hashes["renderProfileSha256"],
        active_timeline=copy.deepcopy(dict(active)),
        audio_sha256=voice.get("audioSha256"),
        full_approval_identity_hash=voice.get("fullApprovalIdentityHash"),
        voice_manifest_sha256=voice.get("voiceManifestSha256"),
        project_id=bindings.get("projectId", ""),
        generation_plan_sha256=required_hashes["generationPlanSha256"],
        scene_order=tuple(scene_order),
        validator_contract=bindings.get("validatorContract", ""),
        annotation_bindings=tuple(annotation_bindings),
        receipt_run_id=receipt.get("runId"),
        receipt_sha256=receipt.get("receiptSha256"),
        receipt_file=receipt_file,
        receipt_created_at=receipt.get("createdAt"),
        receipt_expires_at=receipt.get("expiresAt"),
    )


def load_formal_validation_context_receipt(
    project: Project,
    receipt_path: str | Path,
    *,
    expected_run_id: str,
    now: str | datetime | None = None,
) -> FormalValidationContext:
    """只在同 run/短时效内加载 current formal receipt；失败绝不回退 PASS。"""

    run_id = _validate_formal_run_id(expected_run_id)
    expected_path = formal_validation_context_receipt_path(project, run_id)
    raw_path = Path(receipt_path)
    if raw_path.is_symlink():
        raise RenderTimingError("formal receipt 路径不得是符号链接")
    try:
        path = raw_path.resolve(strict=True)
    except OSError as exc:
        raise RenderTimingError(f"formal receipt 不可读: {exc}") from exc
    if path != expected_path.resolve() or not path.is_file():
        raise RenderTimingError("formal receipt 必须位于同 run 的固定路径")
    receipt = _load_json(path, "formal receipt")
    required_keys = {
        "contractVersion",
        "validatorContract",
        "runId",
        "projectId",
        "createdAt",
        "expiresAt",
        "bindings",
        "receiptSha256",
    }
    if set(receipt) != required_keys:
        raise RenderTimingError("formal receipt schema 不一致")
    if receipt.get("contractVersion") != FORMAL_CONTEXT_RECEIPT_CONTRACT_VERSION:
        raise RenderTimingError("formal receipt contract 已 stale 或属于旧格式")
    if receipt.get("validatorContract") != FORMAL_CONTEXT_VALIDATOR_CONTRACT:
        raise RenderTimingError("formal validator contract 已 stale")
    if receipt.get("runId") != run_id:
        raise RenderTimingError("formal receipt 不能跨 run 复用")
    if receipt.get("projectId") != project.project_id:
        raise RenderTimingError("formal receipt projectId 与 current project 不一致")
    if receipt.get("receiptSha256") != receipt_sha256(receipt):
        raise RenderTimingError("formal receipt SHA-256 校验失败")
    try:
        validate_receipt_window(
            created_at=receipt.get("createdAt"),
            expires_at=receipt.get("expiresAt"),
            now=now,
            require_expiry=True,
            label="formal receipt",
        )
    except ReceiptValidationError as exc:
        raise RenderTimingError(str(exc)) from exc
    created_text = str(receipt.get("createdAt")).replace("Z", "+00:00")
    expires_text = str(receipt.get("expiresAt")).replace("Z", "+00:00")
    if datetime.fromisoformat(expires_text) - datetime.fromisoformat(created_text) > timedelta(
        seconds=FORMAL_CONTEXT_RECEIPT_MAX_TTL_SECONDS
    ):
        raise RenderTimingError("formal receipt 生命周期超过 3600 秒")
    relative = path.relative_to(project.root.resolve()).as_posix()
    context = _parse_formal_receipt_context(receipt, receipt_file=relative)
    validate_formal_context_current(project, context, receipt_now=now)
    try:
        require_current_bindings(
            receipt.get("bindings"),
            _formal_context_bindings(project, context),
            label="formal receipt",
        )
    except ReceiptValidationError as exc:
        raise RenderTimingError(str(exc)) from exc
    return context


def write_formal_validation_context_receipt(
    project: Project,
    context: FormalValidationContext,
    *,
    run_id: str,
    validated_formals: tuple[FormalSceneRender, ...] | list[FormalSceneRender],
    ttl_seconds: int = FORMAL_CONTEXT_RECEIPT_TTL_SECONDS,
    now: datetime | None = None,
) -> tuple[FormalValidationContext, Path]:
    """由 coordinator 写入短生命周期 formal receipt；不写正式产物或批准。"""

    run_id = _validate_formal_run_id(run_id)
    if context.receipt_sha256 is not None:
        raise RenderTimingError("formal receipt 只能由本次 deep validation context 生成")
    if (
        isinstance(ttl_seconds, bool)
        or not isinstance(ttl_seconds, int)
        or ttl_seconds <= 0
        or ttl_seconds > FORMAL_CONTEXT_RECEIPT_MAX_TTL_SECONDS
    ):
        raise RenderTimingError("formal receipt ttl_seconds 必须在 1-3600 范围内")
    validate_formal_context_current(project, context)
    scene_order = context.scene_order or tuple(scene["sceneId"] for scene in project.plan["scenes"])
    if tuple(formal.scene_id for formal in validated_formals) != scene_order:
        raise RenderTimingError("formal receipt 只能覆盖已按 current scene 顺序完成 deep validation 的全部场景")
    for formal in validated_formals:
        if formal.project.project_id != project.project_id or not isinstance(formal.annotation, dict):
            raise RenderTimingError("formal receipt 收到无效的 deep validation evidence")
    annotation_bindings = _formal_annotation_bindings(project, scene_order)
    deep_bindings = tuple(
        (formal.scene_id, formal.annotation_sha256, formal.annotation_bytes)
        for formal in validated_formals
    )
    if annotation_bindings != deep_bindings:
        raise RenderTimingError("formal receipt 发布前 annotation current binding 已变化")
    enriched = replace(context, annotation_bindings=annotation_bindings)
    created = now or datetime.now(timezone.utc)
    if created.tzinfo is None or created.utcoffset() is None:
        raise RenderTimingError("formal receipt now 必须带时区")
    expires = created + timedelta(seconds=ttl_seconds)
    receipt: dict[str, Any] = {
        "contractVersion": FORMAL_CONTEXT_RECEIPT_CONTRACT_VERSION,
        "validatorContract": FORMAL_CONTEXT_VALIDATOR_CONTRACT,
        "runId": run_id,
        "projectId": project.project_id,
        "createdAt": created.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "expiresAt": expires.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "bindings": _formal_context_bindings(project, enriched),
    }
    receipt["receiptSha256"] = receipt_sha256(receipt)
    path = formal_validation_context_receipt_path(project, run_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=False)
    except FileExistsError as exc:
        raise RenderTimingError("formal receipt runId 已存在，不能覆盖既有证据") from exc
    write_json_atomic(path, receipt)
    loaded = load_formal_validation_context_receipt(project, path, expected_run_id=run_id, now=created)
    return loaded, path


def _validate_frame_range(annotation: Mapping[str, Any], timing_scene: Mapping[str, Any]) -> None:
    frame_range = annotation.get("sceneFrameRange")
    expected = {
        "startFrame": timing_scene["startFrame"],
        "endFrameExclusive": timing_scene["endFrameExclusive"],
        "frameCount": timing_scene["frameCount"],
    }
    if frame_range != expected:
        raise RenderTimingError("annotation sceneFrameRange 与 current timing plan 不一致")


def _validate_timing_source(
    annotation: Mapping[str, Any],
    *,
    project: Project,
    timing_scene: Mapping[str, Any],
    active_timeline: Mapping[str, Any],
    audio_sha256: str | None,
) -> None:
    source = annotation.get("timingSource")
    if not isinstance(source, Mapping):
        raise RenderTimingError("annotation 缺少 timingSource")
    expected_common = {
        "kind": active_timeline["kind"],
        "timelineFile": active_timeline["file"],
        "timelineSha256": active_timeline["sha256"],
        "sceneId": timing_scene["sceneId"],
        "sceneStartMs": timing_scene["startMs"],
        "sceneEndMs": timing_scene["endMs"],
    }
    for key, expected in expected_common.items():
        if source.get(key) != expected:
            raise RenderTimingError(f"annotation timingSource.{key} 与 current timing plan 不一致")
    if project.voiceover_mode in {"edge-tts", "minimax"}:
        if source.get("audioSha256") != audio_sha256:
            raise RenderTimingError("annotation timingSource.audioSha256 与 current narration.wav 不一致")
    elif "audioSha256" in source and source.get("audioSha256") not in (None, ""):
        raise RenderTimingError("Disabled annotation 不得绑定 Edge audioSha256")


def validate_annotation(
    annotation: Mapping[str, Any],
    *,
    project: Project,
    timing_scene: Mapping[str, Any],
    timing_plan_sha256: str,
    render_profile_sha256: str,
    active_timeline: Mapping[str, Any],
    audio_sha256: str | None,
    allow_v1_disabled_compat: bool,
) -> dict[str, Any]:
    """Validate global timeline bindings and scene-local reveal coordinates."""
    value = copy.deepcopy(dict(annotation))
    if value.get("sceneId") != timing_scene["sceneId"]:
        raise RenderTimingError("annotation sceneId 与请求场景不一致")
    duration = timing_scene["sceneDurationMs"]
    if value.get("sceneDurationMs") != duration:
        raise RenderTimingError("annotation sceneDurationMs 与 current timing scene 不一致")

    compatibility = project.schema_version == 1
    if compatibility:
        if project.voiceover_mode != "disabled" or not allow_v1_disabled_compat:
            raise RenderTimingError("schema v1 仅允许 --allow-v1-disabled-compat 明确只读兼容渲染")
    else:
        if value.get("timingPlanSha256") != timing_plan_sha256:
            raise RenderTimingError("annotation timingPlanSha256 stale")
        if value.get("renderProfileSha256") != render_profile_sha256:
            raise RenderTimingError("annotation renderProfileSha256 stale")
        _validate_frame_range(value, timing_scene)
        _validate_timing_source(
            value,
            project=project,
            timing_scene=timing_scene,
            active_timeline=active_timeline,
            audio_sha256=audio_sha256,
        )

    canvas = value.get("canvas")
    profile = project.render_profile
    if canvas != {"width": profile["width"], "height": profile["height"]}:
        raise RenderTimingError("annotation canvas 必须与 project renderProfile 尺寸一致")
    elements = value.get("elements")
    try:
        validator = normalize_legacy_visual_elements if compatibility else validate_visual_elements
        elements = validator(elements, canvas=canvas, scene_duration_ms=duration)
    except AnnotationContractError as exc:
        raise RenderTimingError(str(exc)) from exc
    value["elements"] = elements
    previous_end = 0
    for index, element in enumerate(sorted(elements, key=lambda item: item.get("sequence", 0)), start=1):
        if not isinstance(element, Mapping) or element.get("sequence") != index:
            raise RenderTimingError("annotation element sequence 必须从 1 起连续")
        reveal = element.get("reveal")
        if not isinstance(reveal, Mapping):
            raise RenderTimingError(f"element-{index} 缺少 reveal")
        start = reveal.get("startMs")
        length = reveal.get("durationMs")
        if (
            isinstance(start, bool)
            or not isinstance(start, int)
            or isinstance(length, bool)
            or not isinstance(length, int)
            or start < 0
            or length <= 0
        ):
            raise RenderTimingError(f"element-{index} reveal 必须使用正时长的场景局部毫秒")
        end = start + length
        if start < previous_end:
            raise RenderTimingError("annotation elements 必须按场景局部时间串行且不重叠")
        if end > duration - 500:
            raise RenderTimingError(
                f"element-{index} 结束于 {end}ms，超过 sceneDurationMs - 500 ({duration - 500}ms)"
            )
        previous_end = end
        region = element.get("region")
        if not isinstance(region, Mapping):
            raise RenderTimingError(f"element-{index} 缺少 region")
        coords = [region.get(key) for key in ("x", "y", "width", "height")]
        if any(isinstance(item, bool) or not isinstance(item, int) for item in coords):
            raise RenderTimingError(f"element-{index} region 必须是整数像素")
        x, y, width, height = coords
        if x < 0 or y < 0 or width <= 0 or height <= 0 or x + width > canvas["width"] or y + height > canvas["height"]:
            raise RenderTimingError(f"element-{index} region 越出 annotation canvas")
        protected = reveal.get("protectedRegions", [])
        if not isinstance(protected, list):
            raise RenderTimingError(f"element-{index} protectedRegions 必须是数组")
        for protected_index, protected_region in enumerate(protected, start=1):
            if not isinstance(protected_region, Mapping):
                raise RenderTimingError(
                    f"element-{index} protectedRegions[{protected_index}] 必须是对象"
                )
            protected_coords = [
                protected_region.get(key) for key in ("x", "y", "width", "height")
            ]
            if any(
                isinstance(item, bool) or not isinstance(item, int)
                for item in protected_coords
            ):
                raise RenderTimingError(
                    f"element-{index} protectedRegions[{protected_index}] 必须是整数像素"
                )
            px, py, pwidth, pheight = protected_coords
            if (
                px < 0
                or py < 0
                or pwidth <= 0
                or pheight <= 0
                or px + pwidth > canvas["width"]
                or py + pheight > canvas["height"]
            ):
                raise RenderTimingError(
                    f"element-{index} protectedRegions[{protected_index}] 越出 annotation canvas"
                )
    return value


def resolve_formal_scenes(
    project: Project,
    scene_ids: list[str] | tuple[str, ...],
    *,
    context: FormalValidationContext | None = None,
    allow_v1_disabled_compat: bool = False,
) -> tuple[FormalSceneRender, ...]:
    """按请求顺序解析多幕；全局 evidence 在本调用内只深验一次。"""

    frozen = context or build_formal_validation_context(project)
    validate_formal_context_current(project, frozen)
    receipt_annotations = {
        scene_id: (sha256, byte_count)
        for scene_id, sha256, byte_count in frozen.annotation_bindings
    }
    reuse_receipt_evidence = frozen.receipt_sha256 is not None
    resolved: list[FormalSceneRender] = []
    for scene_id in scene_ids:
        generation_scene, timing_scene = _scene(project, scene_id)
        output_file = generation_scene["outputFile"]
        image_path = project.path(Path("scenes") / output_file)
        annotation_path = project.path(
            Path("scenes") / f"{Path(output_file).stem}.annotation.json"
        )
        output_path = project.path(
            Path("scenes") / f"{Path(output_file).stem}-whiteboard.mp4"
        )
        if not image_path.is_file():
            raise RenderTimingError(f"场景图片不存在: {image_path}")
        if not annotation_path.is_file():
            raise RenderTimingError(f"场景 annotation 不存在: {annotation_path}")
        raw_annotation = _load_json(annotation_path, "annotation")
        current_binding = (sha256_file(annotation_path), annotation_path.stat().st_size)
        if reuse_receipt_evidence and receipt_annotations.get(scene_id) == current_binding:
            # Receipt bytes were fully validated under the current validator
            # contract. Loading JSON is still required by the renderer, but a
            # second schema/timing deep pass in the same run is not.
            annotation = copy.deepcopy(raw_annotation)
        else:
            annotation = validate_annotation(
                raw_annotation,
                project=project,
                timing_scene=timing_scene,
                timing_plan_sha256=frozen.timing_plan_sha256,
                render_profile_sha256=frozen.render_profile_sha256,
                active_timeline=frozen.active_timeline,
                audio_sha256=frozen.audio_sha256,
                allow_v1_disabled_compat=allow_v1_disabled_compat,
            )
        resolved.append(
            FormalSceneRender(
                project=project,
                scene_id=scene_id,
                image_path=image_path,
                annotation_path=annotation_path,
                annotation_sha256=current_binding[0],
                annotation_bytes=current_binding[1],
                output_path=output_path,
                timing_scene=copy.deepcopy(timing_scene),
                timing_plan_sha256=frozen.timing_plan_sha256,
                timing_plan_file=frozen.timing_plan_file,
                render_profile_sha256=frozen.render_profile_sha256,
                active_timeline=copy.deepcopy(frozen.active_timeline),
                audio_sha256=frozen.audio_sha256,
                full_approval_identity_hash=frozen.full_approval_identity_hash,
                annotation=annotation,
                compatibility_mode=(
                    "schema-v1-disabled-readonly" if project.schema_version == 1 else None
                ),
            )
        )
    return tuple(resolved)


def resolve_formal_scene(
    project: Project,
    scene_id: str,
    *,
    context: FormalValidationContext | None = None,
    allow_v1_disabled_compat: bool = False,
) -> FormalSceneRender:
    """兼容单幕入口；显式 context 时不重复全局深验。"""

    return resolve_formal_scenes(
        project,
        [scene_id],
        context=context,
        allow_v1_disabled_compat=allow_v1_disabled_compat,
    )[0]


def local_frame_boundary(local_ms: int, *, scene_start_ms: int, scene_start_frame: int, fps: int) -> int:
    """Map a scene-local millisecond boundary onto the cumulative global frame clock."""
    return ((scene_start_ms + local_ms) * fps + 999) // 1000 - scene_start_frame


def render_identity(context: FormalSceneRender, *, render_options: Mapping[str, Any]) -> str:
    scene = context.timing_scene
    return sha256_json(
        {
            "contractVersion": RENDER_CONTRACT_VERSION,
            "projectId": context.project.project_id,
            "sceneId": context.scene_id,
            "imageSha256": sha256_file(context.image_path),
            "annotationSha256": sha256_file(context.annotation_path),
            "timingPlanSha256": context.timing_plan_sha256,
            "renderProfileSha256": context.render_profile_sha256,
            "activeTimeline": context.active_timeline,
            "audioSha256": context.audio_sha256,
            "fullApprovalIdentityHash": context.full_approval_identity_hash,
            "frameRange": {
                "startFrame": scene["startFrame"],
                "endFrameExclusive": scene["endFrameExclusive"],
                "frameCount": scene["frameCount"],
            },
            "renderOptions": dict(render_options),
        }
    )


def update_render_manifest(
    context: FormalSceneRender,
    *,
    media: Mapping[str, Any],
    render_options: Mapping[str, Any],
) -> dict[str, Any]:
    path = context.project.path(RENDER_MANIFEST_FILE)
    if path.is_file():
        manifest = _load_json(path, "render manifest")
        if manifest.get("schemaVersion") != 1 or manifest.get("projectId") != context.project.project_id:
            raise RenderTimingError("既有 render manifest 与 current project 不一致")
    else:
        manifest = {
            "schemaVersion": 1,
            "contractVersion": RENDER_CONTRACT_VERSION,
            "projectId": context.project.project_id,
            "scenes": {},
            "sceneReviewApproval": None,
        }
    scene = context.timing_scene
    options = dict(render_options)
    identity = render_identity(context, render_options=options)
    manifest["scenes"][context.scene_id] = {
        "renderIdentityHash": identity,
        "outputFile": context.output_path.relative_to(context.project.root).as_posix(),
        "image": {
            "file": context.image_path.relative_to(context.project.root).as_posix(),
            "sha256": sha256_file(context.image_path),
        },
        "annotation": {
            "file": context.annotation_path.relative_to(context.project.root).as_posix(),
            "sha256": sha256_file(context.annotation_path),
        },
        "timingPlan": {
            "file": context.timing_plan_file,
            "sha256": context.timing_plan_sha256,
            "activeTimeline": context.active_timeline,
        },
        "renderProfileSha256": context.render_profile_sha256,
        "audioSha256": context.audio_sha256,
        "fullApprovalIdentityHash": context.full_approval_identity_hash,
        "frameRange": {
            "startFrame": scene["startFrame"],
            "endFrameExclusive": scene["endFrameExclusive"],
            "frameCount": scene["frameCount"],
        },
        "renderOptions": options,
        "compatibilityMode": context.compatibility_mode,
        "media": copy.deepcopy(dict(media)),
    }
    # 每次成功发布正式 scene 都代表用户需要重新审阅整批 current bundle。
    # 即使确定性重渲染碰巧产生相同字节，也不得沿用旧人工批准。
    manifest["sceneReviewApproval"] = None
    write_json_atomic(path, manifest)
    return manifest


__all__ = [
    "FormalValidationContext",
    "FormalSceneRender",
    "FORMAL_CONTEXT_RECEIPT_CONTRACT_VERSION",
    "FORMAL_CONTEXT_VALIDATOR_CONTRACT",
    "RENDER_CONTRACT_VERSION",
    "RENDER_MANIFEST_FILE",
    "RenderTimingError",
    "build_formal_validation_context",
    "formal_validation_context_receipt_path",
    "load_formal_validation_context_receipt",
    "write_formal_validation_context_receipt",
    "local_frame_boundary",
    "render_identity",
    "resolve_formal_scene",
    "resolve_formal_scenes",
    "update_render_manifest",
    "validate_annotation",
    "validate_formal_context_current",
]
