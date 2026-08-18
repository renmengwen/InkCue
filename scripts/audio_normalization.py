#!/usr/bin/env python3
"""原始 TTS 媒体到 canonical WAV 的严格规范化和原子发布。"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import uuid
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Mapping, Sequence


CANONICAL_AUDIO_CONTRACT_VERSION = "canonical-wav-pcm-s16le-mono-24000-v1"
CANONICAL_CODEC = "pcm_s16le"
CANONICAL_SAMPLE_RATE = 24000
CANONICAL_CHANNELS = 1
DEFAULT_TOOL_TIMEOUT_SECONDS = 60.0


class AudioNormalizationError(RuntimeError):
    """音频规范化、验证或发布失败。"""


class AudioToolNotFoundError(AudioNormalizationError):
    """FFmpeg 或 ffprobe 不存在。"""


class AudioToolTimeoutError(AudioNormalizationError):
    """FFmpeg 或 ffprobe 超时。"""


class AudioValidationError(AudioNormalizationError):
    """媒体输入或 canonical WAV 不满足合同。"""


class AtomicAudioPublishError(AudioNormalizationError):
    """同卷原子发布失败。"""


@dataclass(frozen=True)
class CanonicalAudioResult:
    path: Path
    contractVersion: str
    codec: str
    sampleRate: int
    channels: int
    durationMs: int
    bytes: int
    sha256: str

    def manifest_media(self) -> dict[str, str | int]:
        """返回不含绝对路径、可直接写入 manifest 的媒体字段。"""

        return {
            "contractVersion": self.contractVersion,
            "codec": self.codec,
            "sampleRate": self.sampleRate,
            "channels": self.channels,
            "durationMs": self.durationMs,
            "bytes": self.bytes,
            "sha256": self.sha256,
        }


def _required_executable(value: str | os.PathLike[str], *, label: str) -> str:
    raw = os.fspath(value)
    if Path(raw).is_absolute():
        candidate = Path(raw)
        if not candidate.is_file():
            raise AudioToolNotFoundError(f"{label} 不存在: {candidate}")
        return str(candidate)
    resolved = shutil.which(raw)
    if resolved is None:
        raise AudioToolNotFoundError(f"缺少必需的可执行文件: {label}")
    return resolved


def _regular_nonempty_file(path: str | Path, *, label: str) -> Path:
    candidate = Path(path)
    try:
        metadata = candidate.lstat()
    except OSError as exc:
        raise AudioValidationError(f"{label} 不存在或不可读取: {candidate}") from exc
    if stat.S_ISLNK(metadata.st_mode):
        raise AudioValidationError(f"{label} 不得是符号链接: {candidate}")
    if not stat.S_ISREG(metadata.st_mode):
        raise AudioValidationError(f"{label} 必须是普通文件: {candidate}")
    if metadata.st_size <= 0:
        raise AudioValidationError(f"{label} 不能为空文件: {candidate}")
    return candidate.resolve()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _run_tool(
    argv: Sequence[str],
    *,
    timeout_seconds: float,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            list(argv),
            shell=False,
            cwd=str(cwd) if cwd is not None else None,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        tool = Path(argv[0]).name if argv else "媒体工具"
        raise AudioToolTimeoutError(f"{tool} 执行超时") from exc
    except OSError as exc:
        tool = Path(argv[0]).name if argv else "媒体工具"
        raise AudioToolNotFoundError(f"无法执行 {tool}") from exc


def _parse_probe_json(completed: subprocess.CompletedProcess[str], *, label: str) -> dict[str, Any]:
    if completed.returncode != 0:
        detail = completed.stderr.strip().splitlines()
        summary = detail[-1][:300] if detail else "无错误摘要"
        raise AudioValidationError(f"{label} ffprobe 失败: {summary}")
    try:
        payload = json.loads(completed.stdout)
    except (json.JSONDecodeError, TypeError) as exc:
        raise AudioValidationError(f"{label} ffprobe 未返回有效 JSON") from exc
    if not isinstance(payload, dict):
        raise AudioValidationError(f"{label} ffprobe 结构无效")
    return payload


def _probe(
    path: Path,
    *,
    ffprobe: str,
    timeout_seconds: float,
    label: str,
) -> dict[str, Any]:
    completed = _run_tool(
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format=duration,size,format_name:stream=index,codec_type,codec_name,sample_rate,channels,duration",
            "-of",
            "json",
            str(path),
        ],
        timeout_seconds=timeout_seconds,
    )
    return _parse_probe_json(completed, label=label)


def _streams(payload: Mapping[str, Any], *, label: str) -> list[Mapping[str, Any]]:
    streams = payload.get("streams")
    if not isinstance(streams, list) or not all(isinstance(item, dict) for item in streams):
        raise AudioValidationError(f"{label} ffprobe 缺少 streams")
    return streams


def _validate_raw_probe(payload: Mapping[str, Any]) -> None:
    streams = _streams(payload, label="原始媒体")
    audio = [stream for stream in streams if stream.get("codec_type") == "audio"]
    video = [stream for stream in streams if stream.get("codec_type") == "video"]
    other = [
        stream
        for stream in streams
        if stream.get("codec_type") not in {"audio", "video"}
    ]
    if len(audio) != 1 or video or other or len(streams) != 1:
        raise AudioValidationError("原始媒体必须恰好包含 1 个音频流且不含视频或其他流")


def _positive_decimal_ms(value: object, *, label: str) -> int:
    try:
        seconds = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise AudioValidationError(f"{label} 不是有效时长") from exc
    if not seconds.is_finite() or seconds <= 0:
        raise AudioValidationError(f"{label} 必须为正时长")
    return int((seconds * 1000).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def validate_canonical_wav(
    path: str | Path,
    *,
    ffprobe: str | os.PathLike[str] = "ffprobe",
    timeout_seconds: float = DEFAULT_TOOL_TIMEOUT_SECONDS,
) -> CanonicalAudioResult:
    """使用 magic bytes 和真实 ffprobe 严格验证 canonical WAV。"""

    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds 必须为正数")
    wav = _regular_nonempty_file(path, label="canonical WAV")
    with wav.open("rb") as handle:
        header = handle.read(12)
    if len(header) != 12 or header[:4] != b"RIFF" or header[8:12] != b"WAVE":
        raise AudioValidationError("canonical WAV 缺少 RIFF/WAVE magic bytes")
    ffprobe_executable = _required_executable(ffprobe, label="ffprobe")
    payload = _probe(
        wav,
        ffprobe=ffprobe_executable,
        timeout_seconds=timeout_seconds,
        label="canonical WAV",
    )
    streams = _streams(payload, label="canonical WAV")
    audio = [stream for stream in streams if stream.get("codec_type") == "audio"]
    video = [stream for stream in streams if stream.get("codec_type") == "video"]
    if len(audio) != 1 or video or len(streams) != 1:
        raise AudioValidationError("canonical WAV 必须恰好包含 1 个音频流和 0 个视频流")
    stream = audio[0]
    if stream.get("codec_name") != CANONICAL_CODEC:
        raise AudioValidationError(f"canonical WAV codec 必须为 {CANONICAL_CODEC}")
    try:
        sample_rate = int(str(stream.get("sample_rate")))
        channels = int(str(stream.get("channels")))
    except (TypeError, ValueError) as exc:
        raise AudioValidationError("canonical WAV 采样率或声道无效") from exc
    if sample_rate != CANONICAL_SAMPLE_RATE:
        raise AudioValidationError(
            f"canonical WAV 采样率必须为 {CANONICAL_SAMPLE_RATE}Hz"
        )
    if channels != CANONICAL_CHANNELS:
        raise AudioValidationError("canonical WAV 必须为 mono")
    format_info = payload.get("format")
    if not isinstance(format_info, dict):
        raise AudioValidationError("canonical WAV ffprobe 缺少 format")
    size = wav.stat().st_size
    try:
        probed_size = int(str(format_info.get("size")))
    except (TypeError, ValueError) as exc:
        raise AudioValidationError("canonical WAV ffprobe size 无效") from exc
    if probed_size != size:
        raise AudioValidationError("canonical WAV ffprobe size 与磁盘字节数不一致")
    duration_value = stream.get("duration")
    if duration_value in (None, "", "N/A"):
        duration_value = format_info.get("duration")
    duration_ms = _positive_decimal_ms(duration_value, label="canonical WAV duration")
    return CanonicalAudioResult(
        path=wav,
        contractVersion=CANONICAL_AUDIO_CONTRACT_VERSION,
        codec=CANONICAL_CODEC,
        sampleRate=sample_rate,
        channels=channels,
        durationMs=duration_ms,
        bytes=size,
        sha256=_sha256_file(wav),
    )


def atomic_publish_wav(candidate: str | Path, destination: str | Path) -> Path:
    """验证同卷条件后以 ``os.replace`` 发布，失败时不触碰旧文件。"""

    source = _regular_nonempty_file(candidate, label="WAV 候选")
    target = Path(destination)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_symlink():
        raise AtomicAudioPublishError(f"正式 WAV 不得是符号链接: {target}")
    try:
        source_device = source.stat().st_dev
        target_device = target.parent.resolve().stat().st_dev
    except OSError as exc:
        raise AtomicAudioPublishError("无法验证 WAV 候选与正式目录是否同卷") from exc
    if source_device != target_device:
        raise AtomicAudioPublishError("WAV 候选与正式文件必须位于同一卷")
    try:
        os.replace(source, target)
    except OSError as exc:
        raise AtomicAudioPublishError(f"canonical WAV 原子发布失败: {target}") from exc
    return target.resolve()


def _suffix_for_declared_format(declared_format: str) -> str:
    normalized = declared_format.strip().lower()
    mapping = {
        "audio/mpeg": ".mp3",
        "audio/mp3": ".mp3",
        "mp3": ".mp3",
        "mpeg": ".mp3",
        "audio/wav": ".wav",
        "audio/wave": ".wav",
        "wav": ".wav",
        "wave": ".wav",
        "audio/ogg": ".ogg",
        "ogg": ".ogg",
        "audio/webm": ".webm",
        "webm": ".webm",
    }
    return mapping.get(normalized, ".media")


def _validate_project_work_location(destination: Path, work_root: Path) -> None:
    """要求候选位于同一项目的 ``.work``，而不只是碰巧在同一盘。"""

    destination_absolute = destination.resolve(strict=False)
    parts = destination_absolute.parts
    project_root: Path | None = None
    for index, part in enumerate(parts):
        if part.casefold() in {"audio", "previews"} and index > 0:
            project_root = Path(*parts[:index])
            break
    if project_root is None:
        raise AtomicAudioPublishError(
            "正式音频必须位于项目 audio/ 或 previews/ 目录"
        )
    try:
        work_relative = work_root.resolve().relative_to(project_root.resolve())
    except ValueError as exc:
        raise AtomicAudioPublishError("规范化工作目录与正式音频不属于同一项目") from exc
    if not work_relative.parts or work_relative.parts[0].casefold() != ".work":
        raise AtomicAudioPublishError("规范化候选必须位于项目 .work/ 目录")


def _validate_candidate_work_location(candidate: Path, work_root: Path) -> None:
    """Candidate 必须严格位于 coordinator 登记的 ``.work`` run 内。"""

    root = work_root.resolve()
    target = candidate.resolve(strict=False)
    try:
        relative = target.relative_to(root)
    except ValueError as exc:
        raise AtomicAudioPublishError("canonical WAV candidate 必须位于登记的 work_dir 内") from exc
    if not relative.parts:
        raise AtomicAudioPublishError("canonical WAV candidate 不能等于 work_dir")
    folded_parts = [part.casefold() for part in root.parts]
    if ".work" not in folded_parts:
        raise AtomicAudioPublishError("canonical WAV candidate work_dir 必须位于项目 .work/ 内")
    if candidate.is_symlink():
        raise AtomicAudioPublishError("canonical WAV candidate 不得是符号链接")


def normalize_to_candidate(
    raw_audio: bytes,
    candidate: str | Path,
    *,
    work_dir: str | Path,
    declared_format: str = "audio/mpeg",
    ffmpeg: str | os.PathLike[str] = "ffmpeg",
    ffprobe: str | os.PathLike[str] = "ffprobe",
    timeout_seconds: float = DEFAULT_TOOL_TIMEOUT_SECONDS,
) -> CanonicalAudioResult:
    """规范化并原子写入 attempt candidate；绝不发布正式 ``audio/`` 文件。"""

    if not isinstance(raw_audio, bytes) or not raw_audio:
        raise AudioValidationError("原始媒体必须是非空 bytes")
    if not isinstance(declared_format, str) or not declared_format.strip():
        raise AudioValidationError("declared_format 不能为空")
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds 必须为正数")
    candidate_path = Path(candidate)
    work_root = Path(work_dir)
    work_root.mkdir(parents=True, exist_ok=True)
    candidate_path.parent.mkdir(parents=True, exist_ok=True)
    _validate_candidate_work_location(candidate_path, work_root)
    try:
        if work_root.resolve().stat().st_dev != candidate_path.parent.resolve().stat().st_dev:
            raise AtomicAudioPublishError("规范化工作目录与 candidate 必须位于同一卷")
    except OSError as exc:
        raise AtomicAudioPublishError("无法验证规范化工作目录与 candidate 是否同卷") from exc

    ffmpeg_executable = _required_executable(ffmpeg, label="ffmpeg")
    ffprobe_executable = _required_executable(ffprobe, label="ffprobe")
    run_dir = work_root / f"normalize-{uuid.uuid4().hex}"
    run_dir.mkdir(parents=False, exist_ok=False)
    raw_path = run_dir / ("raw" + _suffix_for_declared_format(declared_format))
    normalized = run_dir / "canonical.candidate.wav"
    try:
        raw_path.write_bytes(raw_audio)
        raw_path = _regular_nonempty_file(raw_path, label="原始媒体")
        _validate_raw_probe(
            _probe(
                raw_path,
                ffprobe=ffprobe_executable,
                timeout_seconds=timeout_seconds,
                label="原始媒体",
            )
        )
        completed = _run_tool(
            [
                ffmpeg_executable,
                "-y",
                "-loglevel",
                "error",
                "-i",
                str(raw_path),
                "-map",
                "0:a:0",
                "-vn",
                "-c:a",
                CANONICAL_CODEC,
                "-ar",
                str(CANONICAL_SAMPLE_RATE),
                "-ac",
                str(CANONICAL_CHANNELS),
                "-f",
                "wav",
                str(normalized),
            ],
            timeout_seconds=timeout_seconds,
        )
        if completed.returncode != 0:
            detail = completed.stderr.strip().splitlines()
            summary = detail[-1][:300] if detail else "无错误摘要"
            raise AudioNormalizationError(f"FFmpeg 音频规范化失败: {summary}")
        validated = validate_canonical_wav(
            normalized,
            ffprobe=ffprobe_executable,
            timeout_seconds=timeout_seconds,
        )
        published_path = atomic_publish_wav(normalized, candidate_path)
        return CanonicalAudioResult(
            path=published_path,
            contractVersion=validated.contractVersion,
            codec=validated.codec,
            sampleRate=validated.sampleRate,
            channels=validated.channels,
            durationMs=validated.durationMs,
            bytes=validated.bytes,
            sha256=validated.sha256,
        )
    finally:
        shutil.rmtree(run_dir, ignore_errors=True)


def normalize_and_publish(
    raw_audio: bytes,
    destination: str | Path,
    *,
    work_dir: str | Path,
    declared_format: str = "audio/mpeg",
    ffmpeg: str | os.PathLike[str] = "ffmpeg",
    ffprobe: str | os.PathLike[str] = "ffprobe",
    timeout_seconds: float = DEFAULT_TOOL_TIMEOUT_SECONDS,
) -> CanonicalAudioResult:
    """规范化、严格验证并原子发布一段原始媒体。

    本函数只创建和清理 ``work_dir`` 下自己命名的本次子目录，不扫描、
    删除或恢复其他 run。候选验证通过之前不会触碰既有正式文件。
    """

    if not isinstance(raw_audio, bytes) or not raw_audio:
        raise AudioValidationError("原始媒体必须是非空 bytes")
    if not isinstance(declared_format, str) or not declared_format.strip():
        raise AudioValidationError("declared_format 不能为空")
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds 必须为正数")
    destination_path = Path(destination)
    work_root = Path(work_dir)
    work_root.mkdir(parents=True, exist_ok=True)
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    _validate_project_work_location(destination_path, work_root)
    try:
        if work_root.resolve().stat().st_dev != destination_path.parent.resolve().stat().st_dev:
            raise AtomicAudioPublishError("规范化工作目录与正式 WAV 必须位于同一卷")
    except OSError as exc:
        raise AtomicAudioPublishError("无法验证规范化工作目录与正式目录是否同卷") from exc

    candidate = work_root / f"publish-candidate-{uuid.uuid4().hex}.wav"
    try:
        validated = normalize_to_candidate(
            raw_audio,
            candidate,
            work_dir=work_root,
            declared_format=declared_format,
            ffmpeg=ffmpeg,
            ffprobe=ffprobe,
            timeout_seconds=timeout_seconds,
        )
        published_path = atomic_publish_wav(candidate, destination_path)
        return CanonicalAudioResult(
            path=published_path,
            contractVersion=validated.contractVersion,
            codec=validated.codec,
            sampleRate=validated.sampleRate,
            channels=validated.channels,
            durationMs=validated.durationMs,
            bytes=validated.bytes,
            sha256=validated.sha256,
        )
    finally:
        candidate.unlink(missing_ok=True)


__all__ = [
    "AtomicAudioPublishError",
    "AudioNormalizationError",
    "AudioToolNotFoundError",
    "AudioToolTimeoutError",
    "AudioValidationError",
    "CANONICAL_AUDIO_CONTRACT_VERSION",
    "CANONICAL_CHANNELS",
    "CANONICAL_CODEC",
    "CANONICAL_SAMPLE_RATE",
    "CanonicalAudioResult",
    "atomic_publish_wav",
    "normalize_and_publish",
    "normalize_to_candidate",
    "validate_canonical_wav",
]
