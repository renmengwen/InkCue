#!/usr/bin/env python3
"""Transcribe a canonical narration WAV with the skill-owned local FunASR runtime.

The runner is intentionally small: it accepts one complete narration track,
normalises a private 16 kHz mono ASR input inside the caller-provided ``.work``
directory, and publishes one-token-per-cue acoustic evidence.  It never downloads
models and never invents timing from character counts.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import wave
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from .audio_normalization import CanonicalAudioResult, validate_canonical_wav
    from .srt_timeline import serialize_srt
except ImportError:  # Direct ``scripts/transcribe_narration.py`` execution.
    from audio_normalization import CanonicalAudioResult, validate_canonical_wav
    from srt_timeline import serialize_srt


CONTRACT_VERSION = "narration-funasr-token-timestamp-v3"
MODEL_CONTRACT = "narration-asr-models-v1"
ASR_SAMPLE_RATE = 16_000
ASR_CHANNELS = 1
ASR_SAMPLE_WIDTH_BYTES = 2
DEFAULT_TIMEOUT_SECONDS = 180.0
MAX_VAD_SEGMENT_MS = 15_000
# FunASR timestamps are quantised to acoustic-frame boundaries.  Only the
# final token may be clamped, and only inside the same 80 ms tolerance used
# by the final media duration contract.  Intermediate timestamps remain strict.
MAX_FINAL_TIMESTAMP_OVERSHOOT_MS = 80
MAX_TIMESTAMP_OVERLAP_RECOVERY_MS = 250
MODEL_IDS = {
    "paraformer-zh": "iic/speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-pytorch",
    "fsmn-vad": "iic/speech_fsmn_vad_zh-cn-16k-common-pytorch",
    "ct-punc": "iic/punc_ct-transformer_zh-cn-common-vocab272727-pytorch",
}
_SEMANTIC_TEXT_RE = re.compile(r"[\w\u3400-\u9fff]", re.UNICODE)


class NarrationTranscriptionError(RuntimeError):
    """The narration could not produce trustworthy token timing."""


@dataclass(frozen=True)
class PreparedAudio:
    source: CanonicalAudioResult
    asr_path: Path
    asr_duration_ms: int
    asr_bytes: int
    asr_sha256: str


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _regular_nonempty_file(path: str | Path, *, label: str) -> Path:
    candidate = Path(path)
    try:
        metadata = candidate.lstat()
    except OSError as exc:
        raise NarrationTranscriptionError(f"{label} 不存在或不可读取: {candidate}") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise NarrationTranscriptionError(f"{label} 必须是普通非符号链接文件: {candidate}")
    if metadata.st_size <= 0:
        raise NarrationTranscriptionError(f"{label} 不能为空文件: {candidate}")
    return candidate.resolve()


def _create_output_directory(path: str | Path) -> Path:
    target = Path(path).resolve(strict=False)
    if ".work" not in {part.casefold() for part in target.parts}:
        raise NarrationTranscriptionError("ASR 输出目录必须位于调用项目的 .work/ 内")
    if target.name.casefold() == ".work":
        raise NarrationTranscriptionError("ASR 输出目录不能等于项目 .work 根目录")
    if target.exists():
        raise NarrationTranscriptionError("ASR 输出目录必须是尚不存在的唯一目录")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.mkdir(parents=False, exist_ok=False)
    except OSError as exc:
        raise NarrationTranscriptionError(f"无法创建 ASR 输出目录: {target}") from exc
    return target


def _required_executable(value: str | os.PathLike[str], *, label: str) -> str:
    raw = os.fspath(value)
    if Path(raw).is_absolute():
        if not Path(raw).is_file():
            raise NarrationTranscriptionError(f"{label} 不存在: {raw}")
        return str(Path(raw).resolve())
    resolved = shutil.which(raw)
    if resolved is None:
        raise NarrationTranscriptionError(f"缺少必需的可执行文件: {label}")
    return resolved


def _run_ffmpeg(
    source: Path,
    destination: Path,
    *,
    ffmpeg: str | os.PathLike[str],
    timeout_seconds: float,
) -> None:
    executable = _required_executable(ffmpeg, label="ffmpeg")
    try:
        completed = subprocess.run(
            [
                executable,
                "-y",
                "-loglevel",
                "error",
                "-i",
                str(source),
                "-map",
                "0:a:0",
                "-vn",
                "-c:a",
                "pcm_s16le",
                "-ar",
                str(ASR_SAMPLE_RATE),
                "-ac",
                str(ASR_CHANNELS),
                "-f",
                "wav",
                str(destination),
            ],
            shell=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise NarrationTranscriptionError("FFmpeg ASR 音频规范化超时") from exc
    except OSError as exc:
        raise NarrationTranscriptionError("无法启动 FFmpeg") from exc
    if completed.returncode != 0:
        lines = completed.stderr.strip().splitlines()
        summary = lines[-1][:300] if lines else "无错误摘要"
        raise NarrationTranscriptionError(f"FFmpeg ASR 音频规范化失败: {summary}")


def _validate_asr_wav(path: Path, *, source_duration_ms: int) -> tuple[int, int, str]:
    candidate = _regular_nonempty_file(path, label="ASR 输入 WAV")
    try:
        with wave.open(str(candidate), "rb") as handle:
            channels = handle.getnchannels()
            sample_width = handle.getsampwidth()
            sample_rate = handle.getframerate()
            frame_count = handle.getnframes()
    except (OSError, wave.Error) as exc:
        raise NarrationTranscriptionError("ASR 输入 WAV 无法解码") from exc
    if channels != ASR_CHANNELS:
        raise NarrationTranscriptionError("ASR 输入 WAV 必须为 mono")
    if sample_width != ASR_SAMPLE_WIDTH_BYTES:
        raise NarrationTranscriptionError("ASR 输入 WAV 必须为 16-bit PCM")
    if sample_rate != ASR_SAMPLE_RATE or frame_count <= 0:
        raise NarrationTranscriptionError("ASR 输入 WAV 必须为非空 16 kHz PCM")
    duration_ms = round(frame_count * 1000 / sample_rate)
    if duration_ms <= 0 or abs(duration_ms - source_duration_ms) > 100:
        raise NarrationTranscriptionError("ASR 输入 WAV 与 canonical 旁白时长不一致")
    return duration_ms, candidate.stat().st_size, _sha256_file(candidate)


def _prepare_asr_audio(
    audio_path: str | Path,
    output_dir: Path,
    *,
    ffmpeg: str | os.PathLike[str],
    ffprobe: str | os.PathLike[str],
    timeout_seconds: float,
) -> PreparedAudio:
    try:
        source = validate_canonical_wav(
            audio_path,
            ffprobe=ffprobe,
            timeout_seconds=timeout_seconds,
        )
    except Exception as exc:
        raise NarrationTranscriptionError(f"canonical narration WAV 验证失败: {exc}") from exc
    asr_path = output_dir / "asr-input.wav"
    _run_ffmpeg(source.path, asr_path, ffmpeg=ffmpeg, timeout_seconds=timeout_seconds)
    asr_duration_ms, asr_bytes, asr_sha256 = _validate_asr_wav(
        asr_path,
        source_duration_ms=source.durationMs,
    )
    return PreparedAudio(
        source=source,
        asr_path=asr_path.resolve(),
        asr_duration_ms=asr_duration_ms,
        asr_bytes=asr_bytes,
        asr_sha256=asr_sha256,
    )


def _validate_model_paths(model_paths: Mapping[str, str | Path]) -> dict[str, Path]:
    if set(model_paths) != set(MODEL_IDS):
        raise NarrationTranscriptionError("FunASR 本地模型路径必须完整包含三个固定 alias")
    resolved: dict[str, Path] = {}
    for alias in MODEL_IDS:
        path = Path(model_paths[alias]).resolve()
        if not path.is_dir() or not any(path.iterdir()):
            raise NarrationTranscriptionError(f"FunASR 本地模型目录缺失或为空: {alias}")
        resolved[alias] = path
    return resolved


def _load_model_paths(
    *,
    model_paths: Mapping[str, str | Path] | None,
    model_receipt: str | Path | None,
) -> tuple[dict[str, Path], Path | None]:
    if model_paths is not None:
        receipt = Path(model_receipt).resolve() if model_receipt is not None else None
        return _validate_model_paths(model_paths), receipt
    try:
        from prepare_env import load_narration_asr_model_paths, narration_asr_paths
    except ImportError:
        try:
            from .prepare_env import load_narration_asr_model_paths, narration_asr_paths
        except ImportError as exc:
            raise NarrationTranscriptionError("无法加载当前 skill 的 FunASR 模型 receipt 读取器") from exc
    receipt = (
        Path(model_receipt).resolve()
        if model_receipt is not None
        else narration_asr_paths()[1].resolve()
    )
    try:
        loaded = load_narration_asr_model_paths(receipt_path=receipt)
    except Exception as exc:
        raise NarrationTranscriptionError(f"FunASR 本地模型 receipt 无效: {exc}") from exc
    return _validate_model_paths(loaded), receipt


def _default_model_factory(**kwargs: Any) -> Any:
    try:
        from funasr import AutoModel
    except ImportError as exc:
        raise NarrationTranscriptionError(
            "当前 skill 环境缺少 funasr；请先准备 narration-asr feature"
        ) from exc
    return AutoModel(**kwargs)


def _integral_milliseconds(value: object, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise NarrationTranscriptionError(f"{label} 必须是整数毫秒")
    integer = int(value)
    if float(value) != integer:
        raise NarrationTranscriptionError(f"{label} 必须是整数毫秒")
    return integer


def _funasr_text_tokens(text: str) -> list[str]:
    """Tokenise FunASR text exactly like its timestamp stream.

    Paraformer emits one timestamp for every Han character, while a contiguous
    ASCII word such as ``Claude`` occupies one timestamp.  Treating all text as
    individual characters therefore drops otherwise valid token evidence.
    Punctuation has no timestamp and is intentionally excluded.
    """

    tokens: list[str] = []
    ascii_buffer: list[str] = []

    def flush_ascii() -> None:
        if ascii_buffer:
            tokens.append("".join(ascii_buffer))
            ascii_buffer.clear()

    for character in text:
        if character.isascii() and character.isalnum():
            ascii_buffer.append(character)
            continue
        flush_ascii()
        if "\u3400" <= character <= "\u9fff":
            tokens.append(character)
        elif character.isalnum():
            tokens.append(character)
    flush_ascii()
    return tokens


def _token_timestamp_sentences(record: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """从可一一对应的 token 时间戳恢复声学 cue；绝不按字符比例估时。"""

    raw_timestamps = record.get("timestamp")
    if not isinstance(raw_timestamps, list) or not raw_timestamps:
        raise NarrationTranscriptionError("FunASR 缺少可用的 token 时间戳")
    timestamp_pairs: list[tuple[object, object]] = []
    for ordinal, raw_timestamp in enumerate(raw_timestamps, start=1):
        if (
            not isinstance(raw_timestamp, (list, tuple))
            or len(raw_timestamp) != 2
        ):
            raise NarrationTranscriptionError(
                f"FunASR timestamp[{ordinal}] 必须是 [start, end]"
            )
        timestamp_pairs.append((raw_timestamp[0], raw_timestamp[1]))

    def is_exact(candidate: Sequence[str]) -> bool:
        return len(candidate) == len(timestamp_pairs) and all(
            token and _SEMANTIC_TEXT_RE.search(token) for token in candidate
        )

    raw_token_text = record.get("raw_text")
    if raw_token_text is not None:
        if not isinstance(raw_token_text, str) or not raw_token_text.strip():
            raise NarrationTranscriptionError("FunASR raw_text 必须是非空 token 串")
        raw_tokens = raw_token_text.split()
        if not is_exact(raw_tokens):
            raise NarrationTranscriptionError(
                "FunASR raw_text token 数"
                f" {len(raw_tokens)} 与时间戳数 {len(timestamp_pairs)} 不一致，"
                "拒绝使用加标点文本或句级边界补齐"
            )
        tokens = raw_tokens
    else:
        # Compatibility is deliberately narrow. Explicit token arrays remain
        # trustworthy when they match timestamps exactly. Older FunASR builds
        # may expose only ``text``; it is accepted only when deterministic
        # tokenisation also has the exact timestamp cardinality.
        explicit_tokens = record.get("tokens", record.get("token"))
        explicit_candidate = (
            [token.strip() for token in explicit_tokens]
            if isinstance(explicit_tokens, list)
            and all(isinstance(token, str) for token in explicit_tokens)
            else None
        )
        if explicit_candidate is not None and is_exact(explicit_candidate):
            tokens = explicit_candidate
        else:
            punctuated_text = record.get("text")
            text_candidates: list[list[str]] = []
            if isinstance(punctuated_text, str) and punctuated_text.strip():
                text_candidates = [
                    punctuated_text.split(),
                    _funasr_text_tokens(punctuated_text),
                ]
            tokens = next(
                (candidate for candidate in text_candidates if is_exact(candidate)),
                None,
            )
            if tokens is None:
                candidate_counts = [len(candidate) for candidate in text_candidates]
                explicit_count = (
                    len(explicit_candidate) if explicit_candidate is not None else None
                )
                raise NarrationTranscriptionError(
                    "FunASR 缺少可一一对应的 raw_text/token 证据："
                    f"timestamp={len(timestamp_pairs)}, explicit={explicit_count}, "
                    f"textCandidates={candidate_counts}"
                )
    return [
        {"start": start, "end": end, "text": token}
        for token, (start, end) in zip(tokens, timestamp_pairs, strict=True)
    ]


def _validated_sentences(
    raw_sentences: Sequence[object],
    *,
    duration_ms: int,
    evidence_kind: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    sentences: list[dict[str, Any]] = []
    previous_end = -1
    gap_count = 0
    final_timestamp_clamp_ms = 0
    overlap_adjustments: list[dict[str, int]] = []
    for ordinal, raw in enumerate(raw_sentences, start=1):
        if not isinstance(raw, Mapping):
            raise NarrationTranscriptionError(f"FunASR acoustic token[{ordinal}] 结构无效")
        start_ms = _integral_milliseconds(raw.get("start"), label=f"token {ordinal} start")
        end_ms = _integral_milliseconds(raw.get("end"), label=f"token {ordinal} end")
        text = raw.get("text")
        if not isinstance(text, str):
            raise NarrationTranscriptionError(f"token {ordinal} text 必须是字符串")
        text = text.strip()
        if not text or _SEMANTIC_TEXT_RE.search(text) is None:
            raise NarrationTranscriptionError(f"token {ordinal} 文本不能为空或纯标点")
        if start_ms < 0 or end_ms <= start_ms:
            raise NarrationTranscriptionError(f"token {ordinal} 时间范围无效")
        if end_ms - start_ms > MAX_VAD_SEGMENT_MS:
            raise NarrationTranscriptionError(
                f"token {ordinal} 超过 {MAX_VAD_SEGMENT_MS}ms，不能把超长 VAD 大段作为字幕边界"
            )
        if start_ms < previous_end:
            overlap_ms = previous_end - start_ms
            if overlap_ms > MAX_TIMESTAMP_OVERLAP_RECOVERY_MS:
                raise NarrationTranscriptionError(
                    f"token {ordinal} 与前一 token 重叠 {overlap_ms}ms，超过可审计恢复上限"
                )
            previous_start = sentences[-1]["startMs"]
            original_previous_end = previous_end
            # FunASR frame quantisation may give adjacent tokens overlapping
            # intervals. Moving only the latter start to the former end can
            # collapse a short token completely. Partition the ambiguous
            # overlap at one shared boundary instead: the earlier token ends
            # there and the latter starts there. The boundary remains inside
            # the union of the two real acoustic intervals and both tokens
            # must retain a positive duration.
            minimum_boundary = previous_start + 1
            maximum_boundary = end_ms - 1
            if minimum_boundary > maximum_boundary:
                raise NarrationTranscriptionError(
                    f"token {ordinal} 的重叠区间无法恢复为两个正时长 token"
                )
            shared_boundary = (start_ms + original_previous_end) // 2
            shared_boundary = min(
                maximum_boundary,
                max(minimum_boundary, shared_boundary),
            )
            sentences[-1]["endMs"] = shared_boundary
            overlap_adjustments.append(
                {
                    "ordinal": ordinal,
                    "originalPreviousEndMs": original_previous_end,
                    "originalStartMs": start_ms,
                    "sharedBoundaryMs": shared_boundary,
                    "adjustedPreviousEndMs": shared_boundary,
                    "adjustedStartMs": shared_boundary,
                    "overlapMs": overlap_ms,
                }
            )
            start_ms = shared_boundary
            previous_end = shared_boundary
        if end_ms > duration_ms:
            overshoot_ms = end_ms - duration_ms
            if (
                ordinal != len(raw_sentences)
                or overshoot_ms > MAX_FINAL_TIMESTAMP_OVERSHOOT_MS
            ):
                raise NarrationTranscriptionError(f"token {ordinal} 越过实测旁白时长")
            # FunASR timestamps are frame-quantised and may extend the final
            # token by a few milliseconds beyond the canonical WAV. Clamp only
            # the last token within a narrow, auditable tolerance.
            final_timestamp_clamp_ms = overshoot_ms
            end_ms = duration_ms
        if previous_end >= 0 and start_ms > previous_end:
            gap_count += 1
        sentences.append(
            {
                "ordinal": ordinal,
                "startMs": start_ms,
                "endMs": end_ms,
                "text": text,
            }
        )
        previous_end = end_ms

    timing = {
        "invalidRanges": 0,
        "overlaps": len(overlap_adjustments),
        "overlapAdjustments": overlap_adjustments,
        "maxRecoveredOverlapMs": max(
            (item["overlapMs"] for item in overlap_adjustments), default=0
        ),
        "maxAllowedOverlapRecoveryMs": MAX_TIMESTAMP_OVERLAP_RECOVERY_MS,
        "gaps": gap_count,
        "firstStartMs": sentences[0]["startMs"],
        "lastEndMs": sentences[-1]["endMs"],
        "leadingRoomMs": sentences[0]["startMs"],
        "trailingRoomMs": duration_ms - sentences[-1]["endMs"],
        "finalTimestampClampMs": final_timestamp_clamp_ms,
        "evidenceKind": evidence_kind,
    }
    return sentences, timing


def _extract_sentence_info(result: object, *, duration_ms: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if isinstance(result, Mapping):
        records: Sequence[object] = [result]
    elif isinstance(result, Sequence) and not isinstance(result, (str, bytes)):
        records = result
    else:
        raise NarrationTranscriptionError("FunASR 返回结构无效")
    if not records or not isinstance(records[0], Mapping):
        raise NarrationTranscriptionError("FunASR 未返回转写记录")
    record = records[0]
    if isinstance(record.get("timestamp"), list) and record.get("timestamp"):
        return _validated_sentences(
            _token_timestamp_sentences(record),
            duration_ms=duration_ms,
            evidence_kind="token_timestamp",
        )
    raise NarrationTranscriptionError(
        "FunASR 未返回 token timestamp；句级 sentence_info 不能作为正式字幕证据"
    )


def _atomic_write_text(path: Path, text: str) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8", newline="\n")
    os.replace(temporary, path)


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    _atomic_write_text(
        path,
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )


def transcribe_narration(
    audio_path: str | Path,
    output_dir: str | Path,
    *,
    model_factory: Callable[..., Any] | None = None,
    model_paths: Mapping[str, str | Path] | None = None,
    model_receipt: str | Path | None = None,
    ffmpeg: str | os.PathLike[str] = "ffmpeg",
    ffprobe: str | os.PathLike[str] = "ffprobe",
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Create one-token-per-cue ASR evidence for one complete narration track."""

    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds 必须为正数")
    source_path = _regular_nonempty_file(audio_path, label="narration WAV")
    work_dir = _create_output_directory(output_dir)
    prepared = _prepare_asr_audio(
        source_path,
        work_dir,
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
        timeout_seconds=timeout_seconds,
    )
    local_models, resolved_model_receipt = _load_model_paths(
        model_paths=model_paths,
        model_receipt=model_receipt,
    )
    factory = model_factory or _default_model_factory
    model_kwargs = {
        "model": str(local_models["paraformer-zh"]),
        "vad_model": str(local_models["fsmn-vad"]),
        "punc_model": str(local_models["ct-punc"]),
        "vad_kwargs": {"max_single_segment_time": MAX_VAD_SEGMENT_MS},
        "device": "cpu",
        "disable_update": True,
        "return_raw_text": True,
    }
    try:
        model = factory(**model_kwargs)
        generate = getattr(model, "generate", None)
        if not callable(generate):
            raise TypeError("model factory 未返回具有 generate() 的对象")
        raw_result = generate(
            input=str(prepared.asr_path),
            batch_size=1,
            sentence_timestamp=True,
            pred_timestamp=True,
            return_raw_text=True,
        )
    except NarrationTranscriptionError:
        raise
    except Exception as exc:
        raise NarrationTranscriptionError(f"FunASR 本地推理失败: {exc}") from exc

    sentences, timing = _extract_sentence_info(
        raw_result,
        duration_ms=prepared.source.durationMs,
    )
    raw_srt_path = work_dir / "transcript.raw.srt"
    raw_json_path = work_dir / "transcript.raw.json"
    receipt_path = work_dir / "asr-receipt.json"
    cues = [
        {
            "originalIndex": sentence["ordinal"],
            "startMs": sentence["startMs"],
            "endMs": sentence["endMs"],
            "text": sentence["text"],
        }
        for sentence in sentences
    ]
    _atomic_write_text(raw_srt_path, serialize_srt(cues))
    _atomic_write_json(
        raw_json_path,
        {
            "contractVersion": CONTRACT_VERSION,
            "sentenceInfo": sentences,
            "text": "".join(sentence["text"] for sentence in sentences),
            "timingValidation": timing,
        },
    )

    receipt_sha = (
        _sha256_file(resolved_model_receipt)
        if resolved_model_receipt is not None and resolved_model_receipt.is_file()
        else None
    )
    receipt = {
        "schemaVersion": 1,
        "contractVersion": CONTRACT_VERSION,
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "sourceAudio": prepared.source.manifest_media(),
        "asrInput": {
            "file": prepared.asr_path.name,
            "codec": "pcm_s16le",
            "sampleRate": ASR_SAMPLE_RATE,
            "channels": ASR_CHANNELS,
            "durationMs": prepared.asr_duration_ms,
            "bytes": prepared.asr_bytes,
            "sha256": prepared.asr_sha256,
        },
        "model": {
            "engine": "funasr.AutoModel",
            "modelContract": MODEL_CONTRACT,
            "modelIds": MODEL_IDS,
            "requestedRevision": "master",
            "modelReceiptSha256": receipt_sha,
            "device": "cpu",
            "disableUpdate": True,
        },
        "inference": {
            "batchSize": 1,
            "sentenceTimestamp": True,
            "predTimestamp": True,
            "returnRawText": True,
            "maxVadSegmentMs": MAX_VAD_SEGMENT_MS,
        },
        "sentenceCount": len(sentences),
        "tokenCount": len(sentences),
        "evidenceKind": timing["evidenceKind"],
        "timingValidation": timing,
        "artifacts": {
            "rawSrt": raw_srt_path.name,
            "rawJson": raw_json_path.name,
        },
    }
    _atomic_write_json(receipt_path, receipt)
    return {
        "ok": True,
        "contractVersion": CONTRACT_VERSION,
        "outputDirectory": str(work_dir),
        "sourceAudioPath": str(source_path),
        "audioInputPath": str(prepared.asr_path),
        "rawSrtPath": str(raw_srt_path.resolve()),
        "rawJsonPath": str(raw_json_path.resolve()),
        "receiptPath": str(receipt_path.resolve()),
        "durationMs": prepared.source.durationMs,
        "sentenceCount": len(sentences),
        "tokenCount": len(sentences),
        "evidenceKind": timing["evidenceKind"],
        "timingValidation": timing,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="使用当前 skill 内置 FunASR 对齐完整旁白")
    parser.add_argument("audio_path", help="canonical narration.wav")
    parser.add_argument("output_dir", help="调用项目 .work/ 下尚不存在的唯一输出目录")
    parser.add_argument("--model-receipt", help="显式指定当前 skill 的本地模型 receipt")
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--ffprobe", default="ffprobe")
    parser.add_argument("--timeout-seconds", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    try:
        from .cli_runtime import configure_utf8_stdio
    except ImportError:  # pragma: no cover - direct script execution
        from cli_runtime import configure_utf8_stdio  # type: ignore
    configure_utf8_stdio()
    args = build_parser().parse_args(argv)
    try:
        # Keep stdout machine-readable even when FunASR emits Python-level logs.
        with contextlib.redirect_stdout(sys.stderr):
            result = transcribe_narration(
                args.audio_path,
                args.output_dir,
                model_receipt=args.model_receipt,
                ffmpeg=args.ffmpeg,
                ffprobe=args.ffprobe,
                timeout_seconds=args.timeout_seconds,
            )
    except Exception as exc:
        result = {
            "ok": False,
            "errorType": type(exc).__name__,
            "error": str(exc),
        }
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "CONTRACT_VERSION",
    "MODEL_CONTRACT",
    "MODEL_IDS",
    "NarrationTranscriptionError",
    "transcribe_narration",
]
