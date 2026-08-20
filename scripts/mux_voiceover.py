#!/usr/bin/env python3
"""把 current、已批准的 canonical WAV 封装进烧录字幕视频。"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping, Sequence

try:
    from .audio_normalization import AudioNormalizationError, validate_canonical_wav
    from .generate_voiceover import ApprovalGateError, VoiceoverStateError, validate_current_voiceover
    from .media_validation import MediaValidationError, atomic_publish, validate_video
    from .project_workspace import (
        Project,
        ProjectValidationError,
        load_project,
        sha256_file,
        sha256_json,
        write_json_atomic,
    )
    from .subtitle_delivery import (
        BURN_CONTRACT_VERSION,
        DEFAULT_FONT_PATH,
        SubtitleDeliveryError,
        compute_final_identity,
        select_authoritative_srt,
    )
    from .voiceover import VoiceoverValidationError
    from .cover_frame import attach_cover_manifest, attach_cover_review_manifest, cover_record
except ImportError:  # pragma: no cover - direct script execution
    from audio_normalization import AudioNormalizationError, validate_canonical_wav
    from generate_voiceover import ApprovalGateError, VoiceoverStateError, validate_current_voiceover
    from media_validation import MediaValidationError, atomic_publish, validate_video
    from project_workspace import (
        Project,
        ProjectValidationError,
        load_project,
        sha256_file,
        sha256_json,
        write_json_atomic,
    )
    from subtitle_delivery import (
        BURN_CONTRACT_VERSION,
        DEFAULT_FONT_PATH,
        SubtitleDeliveryError,
        compute_final_identity,
        select_authoritative_srt,
    )
    from voiceover import VoiceoverValidationError
    from cover_frame import attach_cover_manifest, attach_cover_review_manifest, cover_record


EDGE_MUX_CONTRACT_VERSION = "edge-aac-mux-v1"
AAC_CODEC = "aac"
AAC_BITRATE = "192k"
AAC_SAMPLE_RATE = 24000
AAC_CHANNELS = 1
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


class MuxStaleError(ValueError):
    """封装输入、批准或 delivery identity 已非 current。"""


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise MuxStaleError(f"{label} 缺失或不是对象")
    return value


def _read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise MuxStaleError(f"缺少 {label}: {path.name}") from exc
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise MuxStaleError(f"{label} 无法读取: {exc}") from exc
    if not isinstance(value, dict):
        raise MuxStaleError(f"{label} 顶层必须是对象")
    return value


def _timing_plan_sha(project: Project) -> str:
    if project.timing_plan_persisted:
        return sha256_file(project.timing_plan_path)
    return sha256_json(project.timing_plan)


def _load_delivery(project: Project) -> tuple[Path, dict[str, Any]]:
    path = project.path("manifests/delivery-manifest.json")
    manifest = _read_json(path, "delivery manifest")
    if not DELIVERY_MANIFEST_KEYS.issubset(manifest) or (
        set(manifest) - DELIVERY_MANIFEST_KEYS - DELIVERY_MANIFEST_OPTIONAL_KEYS
    ):
        raise MuxStaleError("delivery manifest 顶层字段不符合冻结合同")
    if (
        manifest.get("schemaVersion") != 1
        or manifest.get("projectId") != project.project_id
        or manifest.get("voiceoverMode") not in {"edge-tts", "minimax"}
    ):
        raise MuxStaleError("delivery manifest 项目或 mode 身份 stale")
    return path, manifest


def _assert_current_timing(project: Project, manifest: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
    active = _mapping(project.timing_plan.get("activeTimeline"), "timing plan activeTimeline")
    if active.get("kind") not in {"edge-tts-audio-timeline", "audio-authoritative-timeline"} or active.get("file") != "audio/timeline.json":
        raise MuxStaleError("音频 timing plan 必须绑定 audio/timeline.json")
    timeline_path = project.path("audio/timeline.json")
    if not timeline_path.is_file():
        raise MuxStaleError("current audio/timeline.json 缺失")
    timeline_sha = sha256_file(timeline_path)
    if active.get("sha256") != timeline_sha:
        raise MuxStaleError("timing plan activeTimeline SHA-256 stale")
    scenes = project.timing_plan.get("scenes")
    if not isinstance(scenes, list) or not scenes:
        raise MuxStaleError("current timing plan 没有场景")
    expected = {
        "file": "planning/timing-plan.json" if project.timing_plan_persisted else None,
        "sha256": _timing_plan_sha(project),
        "voiceoverMode": project.voiceover_mode,
        "activeTimeline": dict(active),
        "renderProfileSha256": project.timing_plan["renderProfileSha256"],
        "frameRounding": project.render_profile["frameRounding"],
        "frameCount": scenes[-1]["endFrameExclusive"],
    }
    if manifest.get("timingPlan") != expected:
        raise MuxStaleError("delivery timingPlan 与 current timing plan 不一致")
    return timeline_sha, _read_json(timeline_path, "audio timeline")


def _assert_media_record(record: Any, media: Mapping[str, Any], *, file: str, label: str) -> Mapping[str, Any]:
    entry = _mapping(record, label)
    if entry.get("file") != file:
        raise MuxStaleError(f"{label}.file 必须为 {file}")
    for field in ("sha256", "bytes", "durationMs"):
        if entry.get(field) != media.get(field):
            raise MuxStaleError(f"{label}.{field} 与实际媒体不一致")
    validation = _mapping(entry.get("technicalValidation"), f"{label}.technicalValidation")
    if validation.get("validated") is not True:
        raise MuxStaleError(f"{label} 尚未通过技术验证")
    return entry


def _assert_current_subtitles(project: Project, manifest: Mapping[str, Any]) -> tuple[Mapping[str, Any], Any]:
    selection = select_authoritative_srt(project)
    subtitles = _mapping(manifest.get("subtitles"), "subtitles")
    expected = {
        "sourceKind": "edge-tts-narration-srt",
        "file": "audio/narration.srt",
        "sha256": selection.sha256,
        "timelineSha256": selection.timeline_sha256,
    }
    for field, value in expected.items():
        if subtitles.get(field) != value:
            raise MuxStaleError(f"subtitles.{field} 与 current narration SRT 不一致")
    style = _mapping(subtitles.get("style"), "subtitles.style")
    font = _mapping(style.get("font"), "subtitles.style.font")
    ass = _mapping(style.get("ass"), "subtitles.style.ass")
    font_path = Path(DEFAULT_FONT_PATH)
    if not font_path.is_file() or font.get("sha256") != sha256_file(font_path):
        raise MuxStaleError("字幕字体身份 stale")
    ass_path = project.path("subtitles/final.ass")
    if (
        ass.get("file") != "subtitles/final.ass"
        or not ass_path.is_file()
        or ass.get("sha256") != sha256_file(ass_path)
        or ass.get("bytes") != ass_path.stat().st_size
    ):
        raise MuxStaleError("compiled ASS 身份 stale")
    subtitle_identity = subtitles.get("subtitleIdentitySha256")
    if not isinstance(subtitle_identity, str) or len(subtitle_identity) != 64:
        raise MuxStaleError("subtitle identity 无效")
    return subtitles, selection


def _assert_current_voice(project: Project, timeline_sha: str) -> tuple[dict[str, Any], Any, Mapping[str, Any]]:
    current = validate_current_voiceover(project, require_full=True)
    manifest = _read_json(project.path("manifests/voice-manifest.json"), "voice manifest")
    for key, file in (
        ("composite", "audio/narration.wav"),
        ("timeline", "audio/timeline.json"),
        ("narrationSrt", "audio/narration.srt"),
    ):
        record = _mapping(manifest.get(key), f"voice manifest {key}")
        if record.get("status") != "validated" or record.get("relativePath") != file:
            raise MuxStaleError(f"voice manifest {key} 必须为 current validated")
    full_identity = manifest.get("fullIdentityHash")
    approval = _mapping(manifest.get("fullApproval"), "voice manifest fullApproval")
    if (
        approval.get("approved") is not True
        or approval.get("identityHash") != full_identity
        or full_identity != current.get("fullIdentityHash")
    ):
        raise MuxStaleError("完整旁白/真实时长尚未按 current full identity 批准")
    if current.get("timelineSha256") != timeline_sha:
        raise MuxStaleError("voice manifest timeline 与 activeTimeline 不一致")
    audio = validate_canonical_wav(project.path("audio/narration.wav"))
    composite = _mapping(manifest.get("composite"), "voice manifest composite")
    expected_media = {
        "contractVersion": audio.contractVersion,
        "audioMime": "audio/wav",
        "audioCodec": audio.codec,
        "sampleRate": audio.sampleRate,
        "channels": audio.channels,
        "durationMs": audio.durationMs,
        "bytes": audio.bytes,
        "sha256": audio.sha256,
    }
    for field, value in expected_media.items():
        if composite.get(field) != value:
            raise MuxStaleError(f"voice manifest composite.{field} 与 canonical WAV 不一致")
    voice_plan = _mapping(manifest.get("voicePlan"), "voice manifest voicePlan")
    if voice_plan.get("voicePlanAuditHash") != current.get("voicePlanAuditHash"):
        raise MuxStaleError("voice plan audit identity stale")
    return current, audio, approval


def _run_ffmpeg(argv: list[str], *, cwd: Path) -> None:
    completed = subprocess.run(
        argv,
        cwd=cwd,
        shell=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if completed.returncode != 0:
        summary = completed.stderr.strip()[-1000:]
        raise MediaValidationError(f"FFmpeg Edge 音频封装失败: {summary or '无错误输出'}")


def _frame_duration_ms(project: Project, frame_count: int) -> Decimal:
    return Decimal(frame_count * 1000) / Decimal(str(project.render_profile["fps"]))


def validate_edge_mux_media(
    project: Project,
    path: str | Path,
    *,
    expected_frame_count: int,
    canonical_duration_ms: int,
    deep_receipt: Mapping[str, Any] | None = None,
    force_deep: bool = False,
) -> dict[str, Any]:
    """验证 Edge final；容器时长允许由 AAC 决定，但视频帧合同保持严格。"""
    probe = validate_video(
        path,
        render_profile=project.render_profile,
        expected_frame_count=expected_frame_count,
        expected_audio_streams=1,
        deep_receipt=deep_receipt,
        force_deep=force_deep,
    )
    streams = probe["streams"]
    videos = streams["video"]
    audios = streams["audio"]
    if len(videos) != 1 or len(audios) != 1 or streams["subtitle"] or streams["other"]:
        raise MediaValidationError("Edge final 必须恰好包含 1 路视频、1 路音频且无额外流")
    video = videos[0]
    audio = audios[0]
    profile = project.render_profile
    expected_fps = Decimal(str(profile["fps"]))
    actual_fps = Decimal(video["fps"]["numerator"]) / Decimal(video["fps"]["denominator"])
    if (
        video["codec"] != "h264"
        or video["width"] != profile["width"]
        or video["height"] != profile["height"]
        or video["pixelFormat"] != profile["pixelFormat"]
        or actual_fps != expected_fps
        or video["frameCount"] != expected_frame_count
    ):
        raise MediaValidationError("Edge final 视频不满足 H.264/画布/像素格式/fps/帧数合同")
    if audio["codec"] != AAC_CODEC or audio["sampleRate"] != AAC_SAMPLE_RATE or audio["channels"] != AAC_CHANNELS:
        raise MediaValidationError("Edge final 音频必须为 AAC、24000Hz、mono")
    frame_ms = Decimal(1000) / expected_fps
    tolerance = max(frame_ms, Decimal(80))
    video_duration = _frame_duration_ms(project, expected_frame_count)
    if abs(video_duration - Decimal(canonical_duration_ms)) > tolerance:
        raise MediaValidationError("视频帧时长与 canonical timeline 相差超过 max(1帧,80ms)")
    if abs(Decimal(probe["durationMs"]) - Decimal(canonical_duration_ms)) > tolerance:
        raise MediaValidationError("Edge final 容器时长与 canonical timeline 相差超过容差")
    if abs(Decimal(audio["durationMs"]) - Decimal(canonical_duration_ms)) > tolerance:
        raise MediaValidationError("Edge final AAC 时长与 canonical timeline 相差超过容差")
    if Decimal(audio["durationMs"]) + Decimal(1) < Decimal(canonical_duration_ms):
        raise MediaValidationError("Edge final AAC 尾部被截断")
    result = dict(probe)
    result["validation"] = {
        **probe["validation"],
        "edgeMuxContractVersion": EDGE_MUX_CONTRACT_VERSION,
        "validated": True,
        "expectedFrameCount": expected_frame_count,
        "canonicalDurationMs": canonical_duration_ms,
        "durationToleranceMs": float(tolerance),
        "audioTailNotTruncated": True,
        "fullDecode": True,
    }
    return result


def _media_record(file: str, media: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "file": file,
        "sha256": media["sha256"],
        "bytes": media["bytes"],
        "durationMs": media["durationMs"],
        "formatName": media["formatName"],
        "streams": media["streams"],
        "technicalValidation": media["validation"],
    }


def mux_project(
    project_root: str | Path,
    *,
    run_id: str | None = None,
    force_deep: bool = False,
) -> dict[str, Any]:
    project = load_project(project_root)
    if project.voiceover_mode not in {"edge-tts", "minimax"}:
        raise MuxStaleError("mux_voiceover.py 只允许带音频的旁白项目")
    manifest_path, manifest = _load_delivery(project)
    timeline_sha, timeline = _assert_current_timing(project, manifest)
    current_voice, canonical, full_approval = _assert_current_voice(project, timeline_sha)
    subtitles, selection = _assert_current_subtitles(project, manifest)
    attach_cover_manifest(manifest, cover_record(project))
    attach_cover_review_manifest(manifest, project)

    scenes = project.timing_plan["scenes"]
    expected_frames = scenes[-1]["endFrameExclusive"]
    clean_path = project.path("output/final-video-only.mp4")
    captioned_path = project.path("output/final-subtitled-video-only.mp4")
    clean_record = _mapping(manifest.get("cleanVideo"), "cleanVideo")
    clean_receipt = clean_record.get("technicalValidation")
    clean_media = validate_video(
        clean_path,
        render_profile=project.render_profile,
        expected_frame_count=expected_frames,
        expected_audio_streams=0,
        deep_receipt=clean_receipt if isinstance(clean_receipt, Mapping) else None,
        force_deep=force_deep,
    )
    clean = _assert_media_record(
        manifest.get("cleanVideo"), clean_media, file="output/final-video-only.mp4", label="cleanVideo"
    )
    captioned_record = _mapping(manifest.get("captionedVideo"), "captionedVideo")
    captioned_receipt = captioned_record.get("technicalValidation")
    captioned_media = validate_video(
        captioned_path,
        render_profile=project.render_profile,
        expected_frame_count=expected_frames,
        expected_audio_streams=0,
        deep_receipt=captioned_receipt if isinstance(captioned_receipt, Mapping) else None,
        force_deep=force_deep,
    )
    captioned = _assert_media_record(
        manifest.get("captionedVideo"),
        captioned_media,
        file="output/final-subtitled-video-only.mp4",
        label="captionedVideo",
    )
    if captioned.get("cleanVideoSha256") != clean_media["sha256"]:
        raise MuxStaleError("captioned video 未绑定 current clean video")
    if captioned.get("subtitleIdentitySha256") != subtitles.get("subtitleIdentitySha256"):
        raise MuxStaleError("captioned video 未绑定 current subtitle identity")
    if captioned.get("burnContractVersion") != BURN_CONTRACT_VERSION:
        raise MuxStaleError("captioned video burn contract stale")

    timeline_audio = _mapping(timeline.get("audio"), "audio timeline.audio")
    if (
        timeline_audio.get("file") != "audio/narration.wav"
        or timeline_audio.get("sha256") != canonical.sha256
        or timeline_audio.get("durationMs") != canonical.durationMs
    ):
        raise MuxStaleError("audio timeline 未绑定 current canonical WAV")

    run_dir = project.path(f".work/mux-{run_id or uuid.uuid4().hex}")
    if run_dir.exists():
        raise MediaValidationError(f"本次 mux 工作目录已存在: {run_dir}")
    run_dir.mkdir(parents=True)
    candidate = run_dir / "final.tmp.mp4"
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise MediaValidationError("缺少必需的可执行文件: ffmpeg")
    argv = [
        ffmpeg,
        "-y",
        "-loglevel",
        "error",
        "-i",
        str(captioned_path.resolve()),
        "-i",
        str(project.path("audio/narration.wav").resolve()),
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        "-c:v",
        "copy",
        "-c:a",
        AAC_CODEC,
        "-b:a",
        AAC_BITRATE,
        "-ar",
        str(AAC_SAMPLE_RATE),
        "-ac",
        str(AAC_CHANNELS),
        "-movflags",
        "+faststart",
        str(candidate),
    ]
    published = False
    completed = False
    try:
        _run_ffmpeg(argv, cwd=run_dir)
        final_media = validate_edge_mux_media(
            project,
            candidate,
            expected_frame_count=expected_frames,
            canonical_duration_ms=canonical.durationMs,
        )
        final_path = project.path("output/final.mp4")
        atomic_publish(candidate, final_path)
        final_media = validate_edge_mux_media(
            project,
            final_path,
            expected_frame_count=expected_frames,
            canonical_duration_ms=canonical.durationMs,
            deep_receipt=final_media["validation"],
        )
        published = True

        style = _mapping(subtitles.get("style"), "subtitles.style")
        font = _mapping(style.get("font"), "subtitles.style.font")
        inputs, final_identity = compute_final_identity(
            voiceover_mode=project.voiceover_mode,
            clean_video_sha256=clean_media["sha256"],
            audio_sha256=canonical.sha256,
            timeline_sha256=selection.timeline_sha256,
            authoritative_subtitle_sha256=selection.sha256,
            subtitle_style_contract_sha256=str(style.get("contractSha256") or ""),
            font_sha256=str(font.get("sha256") or ""),
            render_profile_sha256=project.timing_plan["renderProfileSha256"],
            timing_plan_sha256=_timing_plan_sha(project),
            mux_contract_version=EDGE_MUX_CONTRACT_VERSION,
            final_media_sha256=final_media["sha256"],
        )
        voice_manifest = _read_json(project.path("manifests/voice-manifest.json"), "voice manifest")
        manifest["final"] = {
            **_media_record("output/final.mp4", final_media),
            "identityInputs": inputs,
            "finalIdentitySha256": final_identity,
            "edgeDelivery": {
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
                    "sha256": timeline_sha,
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
                    "codec": AAC_CODEC,
                    "bitrate": AAC_BITRATE,
                    "sampleRate": AAC_SAMPLE_RATE,
                    "channels": AAC_CHANNELS,
                },
                "muxContractVersion": EDGE_MUX_CONTRACT_VERSION,
            },
        }
        # A new technical final invalidates any old human approval.  Mux never approves.
        manifest["finalApproval"] = None
        write_json_atomic(manifest_path, manifest)
        completed = True
        return manifest
    except Exception as exc:
        if published:
            raise MediaValidationError(
                f"final.mp4 已原子发布，但 delivery manifest 更新失败；保留工作目录 {run_dir}: {exc}"
            ) from exc
        raise
    finally:
        if completed:
            shutil.rmtree(run_dir, ignore_errors=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="封装 current、已批准的 Edge TTS 旁白")
    parser.add_argument("--project", required=True, help="项目根目录")
    parser.add_argument("--force-deep", action="store_true", help="忽略旧 receipt 并重新深验上游")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        manifest = mux_project(args.project, force_deep=args.force_deep)
    except ProjectValidationError as exc:
        print(f"ERROR={exc}", file=sys.stderr)
        return 2
    except (MuxStaleError, ApprovalGateError, SubtitleDeliveryError) as exc:
        print(f"ERROR={exc}", file=sys.stderr)
        return 5
    except (AudioNormalizationError, MediaValidationError, OSError, RuntimeError, KeyError, TypeError, VoiceoverStateError, VoiceoverValidationError) as exc:
        print(f"ERROR={exc}", file=sys.stderr)
        return 4
    final = _mapping(manifest.get("final"), "final")
    print(f"OUTPUT={Path(args.project).resolve() / 'output' / 'final.mp4'}")
    print(f"FINAL_IDENTITY={final['finalIdentitySha256']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
