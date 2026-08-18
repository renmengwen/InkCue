#!/usr/bin/env python3
"""FFprobe/FFmpeg 媒体探测、严格视频合同校验与原子发布。"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import copy
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from fractions import Fraction
from pathlib import Path
from typing import Any, Mapping


MEDIA_VALIDATION_CONTRACT_VERSION = "media-validation-v2"
DEEP_MEDIA_RECEIPT_CONTRACT_VERSION = "media-deep-receipt-v1"
FRAME_COUNT_EVIDENCE = "decoded_frames_v1"


class MediaValidationError(ValueError):
    """媒体不存在、探测失败或不满足正式合同。"""


def _regular_file(path: str | Path, *, label: str) -> Path:
    candidate = Path(path)
    if candidate.is_symlink():
        raise MediaValidationError(f"{label}不得是符号链接: {candidate}")
    if not candidate.is_file():
        raise MediaValidationError(f"{label}不是普通文件: {candidate}")
    resolved = candidate.resolve()
    if resolved.stat().st_size <= 0:
        raise MediaValidationError(f"{label}为空文件: {resolved}")
    return resolved


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _required_executable(name: str) -> str:
    executable = shutil.which(name)
    if executable is None:
        raise MediaValidationError(f"缺少必需的可执行文件: {name}")
    return executable


def _milliseconds(value: Any, *, label: str, allow_zero: bool = False) -> int:
    try:
        seconds = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise MediaValidationError(f"{label}不是有效时长: {value!r}") from exc
    if not seconds.is_finite() or seconds < 0 or (seconds == 0 and not allow_zero):
        raise MediaValidationError(f"{label}必须为正时长")
    return int((seconds * 1000).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _integer(value: Any, *, label: str, positive: bool = False) -> int:
    try:
        result = int(str(value))
    except (TypeError, ValueError) as exc:
        raise MediaValidationError(f"{label}不是整数: {value!r}") from exc
    if positive and result <= 0:
        raise MediaValidationError(f"{label}必须为正整数")
    return result


def _frame_rate(stream: Mapping[str, Any]) -> dict[str, int | float]:
    raw = stream.get("avg_frame_rate")
    if not isinstance(raw, str) or raw in {"", "0/0", "N/A"}:
        raw = stream.get("r_frame_rate")
    if not isinstance(raw, str) or not re.fullmatch(r"\d+/[1-9]\d*", raw):
        raise MediaValidationError(f"ffprobe 未返回有效视频帧率: {raw!r}")
    numerator_text, denominator_text = raw.split("/", 1)
    rate = Fraction(int(numerator_text), int(denominator_text))
    if rate <= 0:
        raise MediaValidationError("视频帧率必须为正数")
    return {
        "numerator": rate.numerator,
        "denominator": rate.denominator,
        "value": float(rate),
    }


def _stream_duration_ms(stream: Mapping[str, Any], fallback: int) -> int:
    value = stream.get("duration")
    if value in (None, "", "N/A"):
        return fallback
    return _milliseconds(value, label="ffprobe stream.duration")


def _optional_container_frame_count(value: Any) -> int | None:
    if value in (None, "", "N/A"):
        return None
    try:
        parsed = _integer(value, label="video.nb_frames", positive=True)
    except MediaValidationError:
        return None
    return parsed


def _normalize_video_stream(stream: Mapping[str, Any], format_duration_ms: int) -> dict[str, Any]:
    container_frames = _optional_container_frame_count(stream.get("nb_frames"))
    return {
        "index": _integer(stream.get("index"), label="video.index"),
        "codec": str(stream.get("codec_name") or ""),
        "width": _integer(stream.get("width"), label="video.width", positive=True),
        "height": _integer(stream.get("height"), label="video.height", positive=True),
        "pixelFormat": str(stream.get("pix_fmt") or ""),
        "fps": _frame_rate(stream),
        # Compatibility alias only.  It is not authoritative until validate_video
        # replaces it with the statistical full-decode count.
        "frameCount": container_frames,
        "containerNbFrames": container_frames,
        "durationMs": _stream_duration_ms(stream, format_duration_ms),
    }


def _normalize_audio_stream(stream: Mapping[str, Any], format_duration_ms: int) -> dict[str, Any]:
    sample_rate = stream.get("sample_rate")
    channels = stream.get("channels")
    return {
        "index": _integer(stream.get("index"), label="audio.index"),
        "codec": str(stream.get("codec_name") or ""),
        "sampleRate": _integer(sample_rate, label="audio.sampleRate", positive=True),
        "channels": _integer(channels, label="audio.channels", positive=True),
        "durationMs": _stream_duration_ms(stream, format_duration_ms),
    }


def _normalize_other_stream(stream: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "index": _integer(stream.get("index"), label="stream.index"),
        "codecType": str(stream.get("codec_type") or "unknown"),
        "codec": str(stream.get("codec_name") or ""),
    }


def probe_media(path: str | Path) -> dict[str, Any]:
    """运行 ffprobe 并返回不含绝对路径、可安全写入 manifest 的规范化结果。"""
    media_path = _regular_file(path, label="媒体文件")
    ffprobe = _required_executable("ffprobe")
    argv = [
        ffprobe,
        "-v",
        "error",
        "-show_streams",
        "-show_format",
        "-of",
        "json",
        str(media_path),
    ]
    completed = subprocess.run(
        argv,
        shell=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if completed.returncode != 0:
        summary = completed.stderr.strip()[-1000:]
        raise MediaValidationError(f"ffprobe 失败: {summary or '无错误输出'}")
    try:
        raw = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise MediaValidationError(f"ffprobe JSON 无效: {exc}") from exc
    if not isinstance(raw, dict):
        raise MediaValidationError("ffprobe JSON 顶层必须是对象")
    raw_format = raw.get("format")
    raw_streams = raw.get("streams")
    if not isinstance(raw_format, dict) or not isinstance(raw_streams, list):
        raise MediaValidationError("ffprobe 缺少 format/streams")
    disk_bytes = media_path.stat().st_size
    reported_size = raw_format.get("size")
    if reported_size not in (None, "", "N/A"):
        if _integer(reported_size, label="format.size", positive=True) != disk_bytes:
            raise MediaValidationError("ffprobe format.size 与磁盘字节数不一致")
    format_duration_ms = _milliseconds(raw_format.get("duration"), label="format.duration")
    videos: list[dict[str, Any]] = []
    audios: list[dict[str, Any]] = []
    subtitles: list[dict[str, Any]] = []
    others: list[dict[str, Any]] = []
    for raw_stream in raw_streams:
        if not isinstance(raw_stream, dict):
            raise MediaValidationError("ffprobe streams 元素必须是对象")
        stream_type = raw_stream.get("codec_type")
        if stream_type == "video":
            videos.append(_normalize_video_stream(raw_stream, format_duration_ms))
        elif stream_type == "audio":
            audios.append(_normalize_audio_stream(raw_stream, format_duration_ms))
        elif stream_type == "subtitle":
            subtitles.append(_normalize_other_stream(raw_stream))
        else:
            others.append(_normalize_other_stream(raw_stream))
    return {
        "bytes": disk_bytes,
        "sha256": _sha256_file(media_path),
        "durationMs": format_duration_ms,
        "formatName": str(raw_format.get("format_name") or ""),
        "streams": {
            "video": videos,
            "audio": audios,
            "subtitle": subtitles,
            "other": others,
        },
    }


def _render_profile_sha256(render_profile: Mapping[str, Any]) -> str:
    payload = json.dumps(
        dict(render_profile),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _validate_video_contract(
    probe: Mapping[str, Any],
    *,
    render_profile: Mapping[str, Any],
    expected_frame_count: int,
    expected_audio_streams: int,
    decoded_frame_count: int,
) -> None:
    streams = probe["streams"]
    videos = streams["video"]
    audios = streams["audio"]
    if len(videos) != 1:
        raise MediaValidationError(f"视频流必须恰好为 1，实际为 {len(videos)}")
    if len(audios) != expected_audio_streams:
        raise MediaValidationError(
            f"音频流必须恰好为 {expected_audio_streams}，实际为 {len(audios)}"
        )
    if streams["subtitle"] or streams["other"]:
        raise MediaValidationError("正式媒体不得包含字幕轨或其他额外流")
    video = videos[0]
    expected_codec = str(render_profile["videoCodec"]).casefold()
    accepted_codec = {"h264": "h264", "avc": "h264"}.get(expected_codec, expected_codec)
    if video["codec"].casefold() != accepted_codec:
        raise MediaValidationError(
            f"视频 codec 必须为 {accepted_codec}，实际为 {video['codec']}"
        )
    if video["width"] != render_profile["width"] or video["height"] != render_profile["height"]:
        raise MediaValidationError(
            f"视频尺寸必须为 {render_profile['width']}x{render_profile['height']}，"
            f"实际为 {video['width']}x{video['height']}"
        )
    if video["pixelFormat"] != render_profile["pixelFormat"]:
        raise MediaValidationError(
            f"像素格式必须为 {render_profile['pixelFormat']}，实际为 {video['pixelFormat']}"
        )
    expected_fps = Fraction(str(render_profile["fps"]))
    actual_fps = Fraction(video["fps"]["numerator"], video["fps"]["denominator"])
    if actual_fps != expected_fps:
        raise MediaValidationError(f"视频 fps 必须为 {expected_fps}，实际为 {actual_fps}")
    if decoded_frame_count != expected_frame_count:
        raise MediaValidationError(
            f"统计型完整解码帧数必须为 {expected_frame_count}，实际为 {decoded_frame_count}"
        )
    container_frames = video.get("containerNbFrames")
    if container_frames is not None and container_frames != decoded_frame_count:
        raise MediaValidationError(
            "容器 nb_frames 与统计型完整解码 decodedFrameCount 不一致"
        )
    expected_duration_ms = Decimal(expected_frame_count * 1000) / Decimal(expected_fps.numerator)
    expected_duration_ms *= Decimal(expected_fps.denominator)
    tolerance_ms = max(
        Decimal(1),
        Decimal(1000) * Decimal(expected_fps.denominator) / Decimal(expected_fps.numerator),
    )
    for label, duration in (("视频流", video["durationMs"]), ("容器", probe["durationMs"])):
        if abs(Decimal(duration) - expected_duration_ms) > tolerance_ms:
            raise MediaValidationError(
                f"{label}时长 {duration}ms 与帧合同 {float(expected_duration_ms):.3f}ms "
                f"相差超过一帧"
            )


def _receipt_from(value: Mapping[str, Any]) -> Mapping[str, Any]:
    if isinstance(value.get("deepReceipt"), Mapping):
        return value["deepReceipt"]
    validation = value.get("validation")
    if isinstance(validation, Mapping) and isinstance(validation.get("deepReceipt"), Mapping):
        return validation["deepReceipt"]
    return value


def _validate_deep_receipt_binding(
    probe: Mapping[str, Any],
    receipt_value: Mapping[str, Any],
) -> int:
    receipt = _receipt_from(receipt_value)
    if receipt.get("contractVersion") != DEEP_MEDIA_RECEIPT_CONTRACT_VERSION:
        raise MediaValidationError("媒体 deep receipt contract version 已 stale")
    if receipt.get("validatorContractVersion") != MEDIA_VALIDATION_CONTRACT_VERSION:
        raise MediaValidationError("媒体 validator contract version 已 stale")
    if receipt.get("mediaSha256") != probe.get("sha256") or receipt.get("bytes") != probe.get("bytes"):
        raise MediaValidationError("媒体字节与 deep receipt binding 不一致")
    if receipt.get("durationMs") != probe.get("durationMs"):
        raise MediaValidationError("媒体 duration 与 deep receipt binding 不一致")
    if receipt.get("streams") != probe.get("streams"):
        raise MediaValidationError("媒体 streams 与 deep receipt binding 不一致")
    videos = probe.get("streams", {}).get("video", [])
    if not isinstance(videos, list) or len(videos) != 1:
        raise MediaValidationError("媒体 deep receipt binding 缺少唯一视频流")
    video = videos[0]
    expected_video_binding = {
        "videoCodec": video.get("codec"),
        "width": video.get("width"),
        "height": video.get("height"),
        "pixelFormat": video.get("pixelFormat"),
        "fps": video.get("fps"),
        "videoDurationMs": video.get("durationMs"),
        "containerNbFrames": video.get("containerNbFrames"),
    }
    for key, expected in expected_video_binding.items():
        if receipt.get(key) != expected:
            raise MediaValidationError(f"媒体 deep receipt {key} binding 不一致")
    if receipt.get("fullDecode") != {"passed": True, "progressEnd": True}:
        raise MediaValidationError("媒体 deep receipt 缺少完整解码 PASS")
    if receipt.get("frameCountEvidence") != FRAME_COUNT_EVIDENCE:
        raise MediaValidationError("媒体 deep receipt 缺少权威帧数证据")
    decoded = receipt.get("decodedFrameCount")
    if isinstance(decoded, bool) or not isinstance(decoded, int) or decoded <= 0:
        raise MediaValidationError("媒体 deep receipt decodedFrameCount 无效")
    return decoded


def _validated_media_result(
    probe: Mapping[str, Any],
    *,
    decoded_frame_count: int,
    render_profile: Mapping[str, Any],
    expected_frame_count: int,
    expected_audio_streams: int,
    deep_receipt: Mapping[str, Any],
    mode: str,
) -> dict[str, Any]:
    result = copy.deepcopy(dict(probe))
    result["decodedFrameCount"] = decoded_frame_count
    result["frameCountEvidence"] = FRAME_COUNT_EVIDENCE
    result["streams"]["video"][0]["frameCount"] = decoded_frame_count
    result["validation"] = {
        "contractVersion": MEDIA_VALIDATION_CONTRACT_VERSION,
        "validated": True,
        "validationMode": mode,
        "expectedFrameCount": expected_frame_count,
        "expectedAudioStreams": expected_audio_streams,
        "renderProfileSha256": _render_profile_sha256(render_profile),
        "decodedFrameCount": decoded_frame_count,
        "frameCountEvidence": FRAME_COUNT_EVIDENCE,
        "containerNbFrames": result["streams"]["video"][0].get("containerNbFrames"),
        "fullDecode": True,
        "deepReceipt": copy.deepcopy(dict(deep_receipt)),
    }
    return result


def validate_video(
    path: str | Path,
    *,
    render_profile: Mapping[str, Any],
    expected_frame_count: int,
    expected_audio_streams: int = 0,
    deep_receipt: Mapping[str, Any] | None = None,
    force_deep: bool = False,
) -> dict[str, Any]:
    """严格验证视频；新字节 deep 一次，current receipt 则只走 binding。"""
    if isinstance(expected_frame_count, bool) or not isinstance(expected_frame_count, int) or expected_frame_count <= 0:
        raise MediaValidationError("expected_frame_count 必须为正整数")
    if (
        isinstance(expected_audio_streams, bool)
        or not isinstance(expected_audio_streams, int)
        or expected_audio_streams < 0
    ):
        raise MediaValidationError("expected_audio_streams 必须为非负整数")
    required_profile = {
        "width",
        "height",
        "fps",
        "pixelFormat",
        "videoCodec",
        "frameRounding",
    }
    if not isinstance(render_profile, Mapping) or not required_profile.issubset(render_profile):
        raise MediaValidationError("render_profile 缺少正式媒体字段")
    if render_profile.get("frameRounding") != "cumulative-ceil-v1":
        raise MediaValidationError("render_profile.frameRounding 必须为 cumulative-ceil-v1")
    probe = probe_media(path)
    if deep_receipt is not None and not force_deep:
        decoded = _validate_deep_receipt_binding(probe, deep_receipt)
        receipt = _receipt_from(deep_receipt)
        mode = "binding"
    else:
        decode = full_decode(path, probe=probe)
        decoded = decode["decodedFrameCount"]
        video = probe["streams"]["video"][0]
        receipt = {
            "contractVersion": DEEP_MEDIA_RECEIPT_CONTRACT_VERSION,
            "validatorContractVersion": MEDIA_VALIDATION_CONTRACT_VERSION,
            "mediaSha256": probe["sha256"],
            "bytes": probe["bytes"],
            "durationMs": probe["durationMs"],
            "formatName": probe["formatName"],
            "streams": copy.deepcopy(probe["streams"]),
            "videoCodec": video["codec"],
            "width": video["width"],
            "height": video["height"],
            "pixelFormat": video["pixelFormat"],
            "fps": copy.deepcopy(video["fps"]),
            "videoDurationMs": video["durationMs"],
            "containerNbFrames": video.get("containerNbFrames"),
            "decodedFrameCount": decoded,
            "frameCountEvidence": FRAME_COUNT_EVIDENCE,
            "fullDecode": {"passed": True, "progressEnd": True},
        }
        mode = "deep"
    _validate_video_contract(
        probe,
        render_profile=render_profile,
        expected_frame_count=expected_frame_count,
        expected_audio_streams=expected_audio_streams,
        decoded_frame_count=decoded,
    )
    return _validated_media_result(
        probe,
        decoded_frame_count=decoded,
        render_profile=render_profile,
        expected_frame_count=expected_frame_count,
        expected_audio_streams=expected_audio_streams,
        deep_receipt=receipt,
        mode=mode,
    )


def _parse_decode_progress(stdout: str) -> int:
    decoded: int | None = None
    progress_values: list[str] = []
    for raw_line in stdout.splitlines():
        key, separator, value = raw_line.partition("=")
        if not separator:
            continue
        if key == "frame":
            try:
                parsed = int(value.strip())
            except ValueError as exc:
                raise MediaValidationError("完整解码 frame 统计无效") from exc
            if parsed < 0:
                raise MediaValidationError("完整解码 frame 统计不能为负数")
            decoded = parsed
        elif key == "progress":
            progress_values.append(value.strip())
    if not progress_values or progress_values[-1] != "end":
        raise MediaValidationError("完整解码未产生 progress=end")
    if decoded is None or decoded <= 0:
        raise MediaValidationError("完整解码未产生有效 decodedFrameCount")
    return decoded


def full_decode(
    path: str | Path,
    *,
    probe: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """一次 FFmpeg null-sink deep decode 同时返回权威帧统计。"""
    media_path = _regular_file(path, label="待解码媒体")
    metadata = probe_media(media_path) if probe is None else probe
    videos = metadata.get("streams", {}).get("video", [])
    if not isinstance(videos, list) or len(videos) != 1:
        raise MediaValidationError("统计型完整解码要求恰好 1 路视频流")
    before = media_path.stat()
    ffmpeg = _required_executable("ffmpeg")
    argv = [
        ffmpeg,
        "-v",
        "error",
        "-xerror",
        "-err_detect",
        "explode",
        "-i",
        str(media_path),
        "-map",
        "0:v:0",
        "-map",
        "0:a?",
        "-progress",
        "pipe:1",
        "-nostats",
        "-f",
        "null",
        "-",
    ]
    completed = subprocess.run(
        argv,
        shell=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if completed.returncode != 0:
        summary = completed.stderr.strip()[-1000:]
        raise MediaValidationError(f"完整解码失败: {summary or '无错误输出'}")
    decoded = _parse_decode_progress(completed.stdout)
    after = media_path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise MediaValidationError("媒体在完整解码期间发生变化")
    return {
        "decodedFrameCount": decoded,
        "frameCountEvidence": FRAME_COUNT_EVIDENCE,
        "fullDecode": {"passed": True, "progressEnd": True},
    }


def bind_validated_video(
    path: str | Path,
    *,
    render_profile: Mapping[str, Any],
    expected_frame_count: int,
    expected_audio_streams: int = 0,
    deep_receipt: Mapping[str, Any],
) -> dict[str, Any]:
    """发布后/下游对相同字节只做 SHA、bytes、metadata 与 receipt binding。"""

    return validate_video(
        path,
        render_profile=render_profile,
        expected_frame_count=expected_frame_count,
        expected_audio_streams=expected_audio_streams,
        deep_receipt=deep_receipt,
        force_deep=False,
    )


def _same_volume(candidate: Path, destination_parent: Path) -> bool:
    candidate_drive = candidate.drive.casefold()
    destination_drive = destination_parent.drive.casefold()
    if candidate_drive or destination_drive:
        return bool(candidate_drive) and candidate_drive == destination_drive
    return candidate.stat().st_dev == destination_parent.stat().st_dev


def atomic_publish(candidate: str | Path, destination: str | Path) -> None:
    """把已验证候选以同卷 os.replace 发布；失败不破坏旧正式文件。"""
    source = _regular_file(candidate, label="发布候选")
    target = Path(destination)
    if target.is_symlink():
        raise MediaValidationError(f"正式目标不得是符号链接: {target}")
    if target.exists() and not target.is_file():
        raise MediaValidationError(f"正式目标存在但不是普通文件: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    target_parent = target.parent.resolve()
    resolved_target = target_parent / target.name
    if source == resolved_target:
        raise MediaValidationError("发布候选与正式目标不得是同一文件")
    if not _same_volume(source, target_parent):
        raise MediaValidationError("候选与正式目标必须位于同一卷，才能原子发布")
    try:
        os.replace(source, resolved_target)
    except OSError as exc:
        raise MediaValidationError(f"原子发布失败: {exc}") from exc


__all__ = [
    "DEEP_MEDIA_RECEIPT_CONTRACT_VERSION",
    "FRAME_COUNT_EVIDENCE",
    "MEDIA_VALIDATION_CONTRACT_VERSION",
    "MediaValidationError",
    "probe_media",
    "validate_video",
    "bind_validated_video",
    "full_decode",
    "atomic_publish",
]
