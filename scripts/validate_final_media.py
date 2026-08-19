#!/usr/bin/env python3
"""验证三层正式输出、delivery identity 与完整解码，不写人工批准。"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from audio_normalization import AudioNormalizationError, validate_canonical_wav
from cover_review import CoverReviewError, load_cover_review
from generate_voiceover import ApprovalGateError, VoiceoverStateError, validate_current_voiceover
from media_validation import MediaValidationError, validate_video
from mux_voiceover import EDGE_MUX_CONTRACT_VERSION, validate_edge_mux_media
from project_workspace import (
    ExecutionConcurrency,
    Project,
    ProjectValidationError,
    WorkspaceConfig,
    load_project,
    load_workspace_config,
    sha256_file,
    sha256_json,
    write_json_atomic,
)
from subtitle_delivery import (
    DEFAULT_FONT_PATH,
    DISABLED_MUX_CONTRACT_VERSION,
    SubtitleDeliveryError,
    compute_final_identity,
    select_authoritative_srt,
)
from voiceover import VoiceoverValidationError
from cover_frame import COVER_FRAME_RANGE, COVER_RELATIVE_PATH, cover_record


DELIVERY_MANIFEST_KEYS = {
    "schemaVersion",
    "projectId",
    "voiceoverMode",
    "timingPlan",
    "cleanVideo",
    "subtitles",
    "captionedVideo",
    "final",
    "finalApproval",
    "cover",
}
DELIVERY_MANIFEST_OPTIONAL_KEYS = {"coverReview"}
FINAL_TECHNICAL_VALIDATION_VERSION = "final-technical-validation-v1"


class FinalMediaStaleError(ValueError):
    """delivery manifest 或 final identity 已非 current。"""


def _require_mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise FinalMediaStaleError(f"{label} 缺失或不是对象")
    return value


def _load_manifest(project: Project) -> tuple[Path, dict[str, Any]]:
    path = project.path("manifests/delivery-manifest.json")
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise FinalMediaStaleError("缺少 delivery manifest") from exc
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise FinalMediaStaleError(f"delivery manifest 无法读取: {exc}") from exc
    if not isinstance(manifest, dict) or not DELIVERY_MANIFEST_KEYS.issubset(manifest) or (
        set(manifest) - DELIVERY_MANIFEST_KEYS - DELIVERY_MANIFEST_OPTIONAL_KEYS
    ):
        raise FinalMediaStaleError("delivery manifest 顶层字段不符合冻结合同")
    if (
        manifest.get("schemaVersion") != 1
        or manifest.get("projectId") != project.project_id
        or manifest.get("voiceoverMode") != project.voiceover_mode
    ):
        raise FinalMediaStaleError("delivery manifest 项目身份 stale")
    return path, manifest


def _assert_cover_review(
    project: Project,
    manifest: Mapping[str, Any],
    expected_frames: int,
) -> dict[str, Any] | None:
    """验证封面证据；只校验边界，不减少任何正式媒体 expected frame count。"""
    try:
        cover = load_cover_review(project)
    except CoverReviewError as exc:
        raise FinalMediaStaleError(f"cover review evidence stale: {exc}") from exc
    if cover is None:
        if manifest.get("coverReview") is not None:
            raise FinalMediaStaleError("delivery manifest 声明了 coverReview，但 current 封面 manifest 缺失")
        return None
    frame_range = cover["frameRange"]
    if frame_range["startFrame"] < 0 or frame_range["endFrameExclusive"] > expected_frames:
        raise FinalMediaStaleError("coverFrameRange 超出 current final 总帧数")
    stored = manifest.get("coverReview")
    if stored is not None and stored != cover:
        raise FinalMediaStaleError("delivery manifest.coverReview 与 current 封面证据不一致")
    return cover


def _timing_plan_sha(project: Project) -> str:
    if project.timing_plan_persisted:
        return sha256_file(project.timing_plan_path)
    return sha256_json(project.timing_plan)


def _expected_timing_record(project: Project) -> dict[str, Any]:
    scenes = project.timing_plan["scenes"]
    if not scenes:
        raise FinalMediaStaleError("current timing plan 没有场景")
    return {
        "file": "planning/timing-plan.json" if project.timing_plan_persisted else None,
        "sha256": _timing_plan_sha(project),
        "voiceoverMode": project.voiceover_mode,
        "activeTimeline": project.timing_plan["activeTimeline"],
        "renderProfileSha256": project.timing_plan["renderProfileSha256"],
        "frameRounding": project.render_profile["frameRounding"],
        "frameCount": scenes[-1]["endFrameExclusive"],
    }


def _assert_current_timing(project: Project, record: Any) -> None:
    if record != _expected_timing_record(project):
        raise FinalMediaStaleError("delivery timingPlan 与 current timing plan 不一致")
    active = project.timing_plan["activeTimeline"]
    active_path = project.path(active["file"])
    if not active_path.is_file() or sha256_file(active_path) != active["sha256"]:
        raise FinalMediaStaleError("current active timeline 文件缺失或 SHA-256 不一致")


def _assert_cover(project: Project, record: Any) -> None:
    """Validate optional cover identity without weakening media checks."""
    current = cover_record(project)
    if current is None:
        if record not in (None, {}):
            raise FinalMediaStaleError("delivery cover 记录存在但封面文件已缺失")
        return
    stored = _require_mapping(record, "cover")
    if stored.get("file") != COVER_RELATIVE_PATH:
        raise FinalMediaStaleError("cover.file 路径无效")
    if stored.get("sha256") != current["sha256"] or stored.get("bytes") != current["bytes"]:
        raise FinalMediaStaleError("cover 文件 SHA-256 或大小与 current 不一致")
    if stored.get("frameRange") != COVER_FRAME_RANGE:
        raise FinalMediaStaleError("cover.frameRange 必须为首帧 [0,1)")
    if stored.get("visualReviewExcluded") is not True:
        raise FinalMediaStaleError("cover.visualReviewExcluded 必须为 true")


def _assert_record_identity(
    record: Any,
    *,
    expected_file: str,
    media: Mapping[str, Any],
    label: str,
) -> Mapping[str, Any]:
    entry = _require_mapping(record, label)
    if entry.get("file") != expected_file:
        raise FinalMediaStaleError(f"{label}.file 必须为 {expected_file}")
    for field in ("sha256", "bytes", "durationMs"):
        if entry.get(field) != media[field]:
            raise FinalMediaStaleError(f"{label}.{field} 与实际媒体不一致")
    stored_streams = entry.get("streams")
    if stored_streams is not None and stored_streams != media["streams"]:
        raise FinalMediaStaleError(f"{label}.streams 与实际媒体不一致")
    stored_format = entry.get("formatName")
    if stored_format is not None and stored_format != media["formatName"]:
        raise FinalMediaStaleError(f"{label}.formatName 与实际媒体不一致")
    return entry


def _validate_output(
    project: Project,
    *,
    relative_file: str,
    expected_frame_count: int,
    expected_audio_streams: int,
    record: Mapping[str, Any],
    force_deep: bool,
) -> dict[str, Any]:
    path = project.path(relative_file)
    technical = record.get("technicalValidation")
    receipt: Mapping[str, Any] | None = None
    if isinstance(technical, Mapping):
        nested = technical.get("mediaValidation")
        receipt = nested if isinstance(nested, Mapping) else technical
    media = validate_video(
        path,
        render_profile=project.render_profile,
        expected_frame_count=expected_frame_count,
        expected_audio_streams=expected_audio_streams,
        deep_receipt=receipt,
        force_deep=force_deep,
    )
    return media


def _validate_media_layers(
    validate_named: Callable[[str], dict[str, Any]],
    *,
    configured_concurrency: int,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Validate independent media layers with bounded work and stable result order."""

    names = ("clean", "captioned", "final")
    if configured_concurrency == 1:
        clean, captioned, final = (validate_named(name) for name in names)
        return clean, captioned, final
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=min(configured_concurrency, len(names)),
        thread_name_prefix="final-media-validation",
    ) as executor:
        futures = [executor.submit(validate_named, name) for name in names]
        clean, captioned, final = (future.result() for future in futures)
        return clean, captioned, final


def _assert_subtitle_identity(project: Project, manifest: Mapping[str, Any]) -> tuple[Any, Mapping[str, Any]]:
    try:
        selection = select_authoritative_srt(project)
    except SubtitleDeliveryError as exc:
        raise FinalMediaStaleError(str(exc)) from exc
    subtitles = _require_mapping(manifest.get("subtitles"), "subtitles")
    expected_kind = "source-srt" if project.voiceover_mode == "disabled" else "edge-tts-narration-srt"
    expected_values = {
        "sourceKind": expected_kind,
        "file": selection.relative_path,
        "sha256": selection.sha256,
        "timelineSha256": selection.timeline_sha256,
    }
    for field, expected in expected_values.items():
        if subtitles.get(field) != expected:
            raise FinalMediaStaleError(f"subtitles.{field} 与 current 权威字幕不一致")
    style = _require_mapping(subtitles.get("style"), "subtitles.style")
    font = _require_mapping(style.get("font"), "subtitles.style.font")
    ass = _require_mapping(style.get("ass"), "subtitles.style.ass")
    if not isinstance(style.get("contractSha256"), str) or len(style["contractSha256"]) != 64:
        raise FinalMediaStaleError("subtitle style contract SHA-256 无效")
    font_path = Path(DEFAULT_FONT_PATH)
    if not font_path.is_file() or sha256_file(font_path) != font.get("sha256"):
        raise FinalMediaStaleError("字幕字体文件或 SHA-256 已变化")
    if ass.get("file") != "subtitles/final.ass":
        raise FinalMediaStaleError("compiled ASS 路径无效")
    ass_path = project.path("subtitles/final.ass")
    if (
        not ass_path.is_file()
        or sha256_file(ass_path) != ass.get("sha256")
        or ass_path.stat().st_size != ass.get("bytes")
    ):
        raise FinalMediaStaleError("compiled ASS 文件身份不一致")
    contact = _require_mapping(subtitles.get("contactSheet"), "subtitles.contactSheet")
    if contact.get("file") != "previews/final-subtitle-contact-sheet.png":
        raise FinalMediaStaleError("字幕像素证据路径无效")
    contact_path = project.path(contact["file"])
    if (
        not contact_path.is_file()
        or sha256_file(contact_path) != contact.get("sha256")
        or contact_path.stat().st_size != contact.get("bytes")
        or not isinstance(contact.get("samples"), list)
        or not contact["samples"]
    ):
        raise FinalMediaStaleError("字幕像素 contact sheet 证据缺失或 stale")
    return selection, subtitles


def _assert_edge_audio(
    project: Project,
    final_media: Mapping[str, Any],
    identity_inputs: Mapping[str, Any],
    final_record: Mapping[str, Any],
) -> None:
    audio_streams = final_media["streams"]["audio"]
    if len(audio_streams) != 1:
        raise MediaValidationError("Edge final 必须恰好有一路音频")
    audio = audio_streams[0]
    if audio["codec"] != "aac" or audio["sampleRate"] != 24000 or audio["channels"] != 1:
        raise MediaValidationError("Edge final 音频必须为 AAC、24000Hz、mono")
    narration = project.path("audio/narration.wav")
    expected_audio_sha = identity_inputs.get("audioSha256")
    canonical = validate_canonical_wav(narration)
    if canonical.sha256 != expected_audio_sha:
        raise FinalMediaStaleError("Edge canonical narration.wav 身份不一致")
    timeline_path = project.path("audio/timeline.json")
    try:
        timeline = json.loads(timeline_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise FinalMediaStaleError(f"Edge audio timeline 无法读取: {exc}") from exc
    timeline_audio = timeline.get("audio") if isinstance(timeline, Mapping) else None
    duration_ms = timeline_audio.get("durationMs") if isinstance(timeline_audio, Mapping) else None
    if isinstance(duration_ms, bool) or not isinstance(duration_ms, int) or duration_ms <= 0:
        raise FinalMediaStaleError("Edge audio timeline.audio.durationMs 无效")
    if (
        timeline_audio.get("file") != "audio/narration.wav"
        or timeline_audio.get("sha256") != canonical.sha256
        or duration_ms != canonical.durationMs
    ):
        raise FinalMediaStaleError("Edge audio timeline 未绑定 current canonical WAV")
    frame_ms = Decimal(1000) / Decimal(str(project.render_profile["fps"]))
    tolerance = max(Decimal(80), frame_ms)
    if abs(Decimal(final_media["durationMs"]) - Decimal(duration_ms)) > tolerance:
        raise MediaValidationError("Edge final 时长与 canonical timeline 相差超过容差")
    if abs(Decimal(audio["durationMs"]) - Decimal(duration_ms)) > tolerance:
        raise MediaValidationError("Edge final 音频尾部与 canonical timeline 不一致")
    if Decimal(audio["durationMs"]) + Decimal(1) < Decimal(duration_ms):
        raise MediaValidationError("Edge final 音频尾部被截断")

    try:
        current_voice = validate_current_voiceover(project, require_full=True)
    except (ApprovalGateError, VoiceoverStateError, VoiceoverValidationError) as exc:
        raise FinalMediaStaleError(f"current voiceover 无效: {exc}") from exc
    voice_manifest_path = project.path("manifests/voice-manifest.json")
    try:
        voice_manifest = json.loads(voice_manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise FinalMediaStaleError(f"voice manifest 无法读取: {exc}") from exc
    full_approval = voice_manifest.get("fullApproval") if isinstance(voice_manifest, Mapping) else None
    if (
        not isinstance(full_approval, Mapping)
        or full_approval.get("approved") is not True
        or full_approval.get("identityHash") != voice_manifest.get("fullIdentityHash")
        or voice_manifest.get("fullIdentityHash") != current_voice.get("fullIdentityHash")
    ):
        raise FinalMediaStaleError("current full voiceover 尚未批准")
    edge = _require_mapping(final_record.get("edgeDelivery"), "final.edgeDelivery")
    expected_edge = {
        "voicePlan": {
            "file": "planning/voice-plan.json",
            "voicePlanAuditHash": current_voice["voicePlanAuditHash"],
        },
        "audio": {
            "file": "audio/narration.wav",
            **canonical.manifest_media(),
        },
        "timeline": {
            "file": "audio/timeline.json",
            "sha256": sha256_file(timeline_path),
            "contractVersion": timeline.get("contractVersion"),
            "durationMs": canonical.durationMs,
        },
        "narrationSrt": {
            "file": "audio/narration.srt",
            "sha256": current_voice["narrationSrtSha256"],
        },
        "fullApproval": {
            "approved": True,
            "identityHash": full_approval["identityHash"],
            "durationDecision": full_approval.get("durationDecision"),
            "approvedAt": full_approval.get("approvedAt"),
            "fullIdentityHash": voice_manifest["fullIdentityHash"],
        },
        "aac": {
            "codec": "aac",
            "bitrate": "192k",
            "sampleRate": 24000,
            "channels": 1,
        },
        "muxContractVersion": EDGE_MUX_CONTRACT_VERSION,
    }
    if dict(edge) != expected_edge:
        raise FinalMediaStaleError("final.edgeDelivery 与 current Edge 输入不一致")


def inspect_project_final_media(
    project_root: str | Path,
    *,
    configured_concurrency: int = 1,
    force_deep: bool = False,
) -> dict[str, Any]:
    """只读检查 current final 及全部身份；不写技术验证或人工批准。"""
    project = load_project(project_root)
    manifest_path, manifest = _load_manifest(project)
    _assert_current_timing(project, manifest.get("timingPlan"))
    _assert_cover(project, manifest.get("cover"))
    expected_frames = project.timing_plan["scenes"][-1]["endFrameExclusive"]
    cover_review = _assert_cover_review(project, manifest, expected_frames)

    if isinstance(configured_concurrency, bool) or not isinstance(configured_concurrency, int) or not 1 <= configured_concurrency <= 16:
        raise FinalMediaStaleError("finalMediaValidation concurrency 必须位于 1–16")
    clean_record = _require_mapping(manifest.get("cleanVideo"), "cleanVideo")
    captioned_record = _require_mapping(manifest.get("captionedVideo"), "captionedVideo")
    final_record = _require_mapping(manifest.get("final"), "final")
    final_audio_streams = 0 if project.voiceover_mode == "disabled" else 1
    canonical_duration: int | None = None
    if project.voiceover_mode == "edge-tts":
        try:
            timeline = json.loads(project.path("audio/timeline.json").read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise FinalMediaStaleError(f"Edge audio timeline 无法读取: {exc}") from exc
        timeline_audio = timeline.get("audio") if isinstance(timeline, Mapping) else None
        canonical_duration = timeline_audio.get("durationMs") if isinstance(timeline_audio, Mapping) else None
        if isinstance(canonical_duration, bool) or not isinstance(canonical_duration, int):
            raise FinalMediaStaleError("Edge audio timeline.audio.durationMs 无效")
    def validate_named(name: str) -> dict[str, Any]:
        if name == "clean":
            return _validate_output(
                project, relative_file="output/final-video-only.mp4",
                expected_frame_count=expected_frames, expected_audio_streams=0,
                record=clean_record, force_deep=force_deep,
            )
        if name == "captioned":
            return _validate_output(
                project, relative_file="output/final-subtitled-video-only.mp4",
                expected_frame_count=expected_frames, expected_audio_streams=0,
                record=captioned_record, force_deep=force_deep,
            )
        if project.voiceover_mode == "edge-tts":
            technical = final_record.get("technicalValidation")
            nested = technical.get("mediaValidation") if isinstance(technical, Mapping) else None
            receipt = nested if isinstance(nested, Mapping) else technical if isinstance(technical, Mapping) else None
            assert canonical_duration is not None
            return validate_edge_mux_media(
                project, project.path("output/final.mp4"),
                expected_frame_count=expected_frames,
                canonical_duration_ms=canonical_duration,
                deep_receipt=receipt,
                force_deep=force_deep,
            )
        return _validate_output(
            project, relative_file="output/final.mp4",
            expected_frame_count=expected_frames, expected_audio_streams=final_audio_streams,
            record=final_record, force_deep=force_deep,
        )

    clean_media, captioned_media, final_media = _validate_media_layers(
        validate_named,
        configured_concurrency=configured_concurrency,
    )

    clean = _assert_record_identity(
        manifest.get("cleanVideo"),
        expected_file="output/final-video-only.mp4",
        media=clean_media,
        label="cleanVideo",
    )
    captioned = _assert_record_identity(
        manifest.get("captionedVideo"),
        expected_file="output/final-subtitled-video-only.mp4",
        media=captioned_media,
        label="captionedVideo",
    )
    final = _assert_record_identity(
        manifest.get("final"),
        expected_file="output/final.mp4",
        media=final_media,
        label="final",
    )
    if clean.get("frameCount") != expected_frames:
        raise FinalMediaStaleError("cleanVideo.frameCount 与 current timing plan 不一致")
    if captioned.get("cleanVideoSha256") != clean_media["sha256"]:
        raise FinalMediaStaleError("captionedVideo 未绑定 current clean video")

    selection, subtitles = _assert_subtitle_identity(project, manifest)
    if captioned.get("subtitleIdentitySha256") != subtitles.get("subtitleIdentitySha256"):
        raise FinalMediaStaleError("captionedVideo 未绑定 current subtitle identity")
    if project.voiceover_mode == "disabled":
        if final_media["sha256"] != captioned_media["sha256"]:
            raise FinalMediaStaleError("Disabled final 必须是 captioned video 的逐字节发布")

    style = _require_mapping(subtitles.get("style"), "subtitles.style")
    font = _require_mapping(style.get("font"), "subtitles.style.font")
    identity_inputs = _require_mapping(final.get("identityInputs"), "final.identityInputs")
    mux_version = (
        DISABLED_MUX_CONTRACT_VERSION
        if project.voiceover_mode == "disabled"
        else EDGE_MUX_CONTRACT_VERSION
    )
    audio_sha = "" if project.voiceover_mode == "disabled" else str(identity_inputs.get("audioSha256") or "")
    expected_inputs, expected_identity = compute_final_identity(
        voiceover_mode=project.voiceover_mode,
        clean_video_sha256=clean_media["sha256"],
        audio_sha256=audio_sha,
        timeline_sha256=selection.timeline_sha256,
        authoritative_subtitle_sha256=selection.sha256,
        subtitle_style_contract_sha256=str(style.get("contractSha256") or ""),
        font_sha256=str(font.get("sha256") or ""),
        render_profile_sha256=project.timing_plan["renderProfileSha256"],
        timing_plan_sha256=_timing_plan_sha(project),
        mux_contract_version=mux_version,
        final_media_sha256=final_media["sha256"],
    )
    if dict(identity_inputs) != expected_inputs or final.get("finalIdentitySha256") != expected_identity:
        raise FinalMediaStaleError("final identity 与 current 输入不一致")
    if project.voiceover_mode == "edge-tts":
        _assert_edge_audio(project, final_media, identity_inputs, final)

    return {
        "project": project,
        "manifestPath": manifest_path,
        "manifest": manifest,
        "cleanRecord": clean,
        "captionedRecord": captioned,
        "cleanMedia": clean_media,
        "captionedMedia": captioned_media,
        "finalRecord": final,
        "finalMedia": final_media,
        "finalIdentitySha256": expected_identity,
        "frameCount": expected_frames,
        "coverReview": cover_review,
        "outputs": {
            "cleanVideoSha256": clean_media["sha256"],
            "captionedVideoSha256": captioned_media["sha256"],
            "finalSha256": final_media["sha256"],
        },
    }


def validate_project_final_media(
    project_root: str | Path,
    *,
    configured_concurrency: int = 1,
    force_deep: bool = False,
) -> dict[str, Any]:
    inspection = inspect_project_final_media(
        project_root,
        configured_concurrency=configured_concurrency,
        force_deep=force_deep,
    )
    manifest_path = inspection["manifestPath"]
    manifest = inspection["manifest"]
    final = inspection["finalRecord"]
    final_media = inspection["finalMedia"]
    expected_identity = inspection["finalIdentitySha256"]
    expected_frames = inspection["frameCount"]

    previous_approval = manifest.get("finalApproval")
    final_entry = dict(final)
    final_entry["technicalValidation"] = {
        "contractVersion": FINAL_TECHNICAL_VALIDATION_VERSION,
        "validated": True,
        "validatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "finalIdentitySha256": expected_identity,
        "finalMediaSha256": final_media["sha256"],
        "outputs": {
            "cleanVideoSha256": inspection["outputs"]["cleanVideoSha256"],
            "captionedVideoSha256": inspection["outputs"]["captionedVideoSha256"],
            "finalSha256": final_media["sha256"],
        },
        "fullDecode": True,
        "mediaValidation": final_media["validation"],
    }
    clean_entry = dict(inspection["cleanRecord"])
    clean_entry["technicalValidation"] = inspection["cleanMedia"]["validation"]
    captioned_entry = dict(inspection["captionedRecord"])
    captioned_entry["technicalValidation"] = inspection["captionedMedia"]["validation"]
    manifest["cleanVideo"] = clean_entry
    manifest["captionedVideo"] = captioned_entry
    manifest["final"] = final_entry
    if inspection.get("coverReview") is not None:
        # 仅记录视觉检查豁免证据；不会改变 final identity 或技术 expected frame count。
        manifest["coverReview"] = dict(inspection["coverReview"])
    if manifest.get("finalApproval") != previous_approval:
        raise RuntimeError("技术验证不得修改 finalApproval")
    write_json_atomic(manifest_path, manifest)
    return {
        "ok": True,
        "voiceoverMode": inspection["project"].voiceover_mode,
        "finalIdentitySha256": expected_identity,
        "frameCount": expected_frames,
        "outputs": final_entry["technicalValidation"]["outputs"],
        "finalApprovalWritten": False,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="验证 final 三层媒体与 delivery identity")
    parser.add_argument("--project", required=True, help="项目根目录")
    parser.add_argument("--force-deep", action="store_true", help="忽略旧 receipt 并刷新三层 deep receipt")
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    workspace_config: WorkspaceConfig | None = None,
) -> int:
    args = build_parser().parse_args(argv)
    try:
        execution = workspace_config
        if execution is None and argv is None:
            execution = load_workspace_config()
        concurrency = execution.concurrency if execution is not None else ExecutionConcurrency()
        result = validate_project_final_media(
            args.project,
            configured_concurrency=concurrency.for_stage("finalMediaValidation"),
            force_deep=args.force_deep,
        )
    except ProjectValidationError as exc:
        print(f"ERROR={exc}", file=sys.stderr)
        return 2
    except (FinalMediaStaleError, SubtitleDeliveryError) as exc:
        print(f"ERROR={exc}", file=sys.stderr)
        return 5
    except (MediaValidationError, OSError, RuntimeError, KeyError, TypeError) as exc:
        print(f"ERROR={exc}", file=sys.stderr)
        return 4
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
