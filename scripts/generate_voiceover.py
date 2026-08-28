#!/usr/bin/env python3
"""语音旁白样音、完整生成、恢复与人工批准 CLI。"""
from __future__ import annotations

import argparse
import copy
import concurrent.futures
import hashlib
import json
import os
import re
import shutil
import sys
import uuid
import wave
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

try:
    from .audio_normalization import (
        AudioNormalizationError,
        AudioValidationError,
        CanonicalAudioResult,
        atomic_publish_wav,
        normalize_and_publish,
        normalize_to_candidate,
        validate_canonical_wav,
    )
    from .project_workspace import (
        Project,
        ProjectValidationError,
        WorkspaceConfig,
        ExecutionConcurrency,
        load_project,
        load_workspace_config,
        sha256_file,
        sha256_json,
        validate_timing_plan_data,
        write_json_atomic,
    )
    from .srt_timeline import parse_srt, serialize_srt
    from .content_source import spoken_text_weight
    from .voiceover import (
        CancelledError,
        FULL_TRACK_SEGMENTATION,
        FULL_TRACK_SEGMENTATION_CONTRACT_VERSION,
        PermanentProviderError,
        ProviderAdapter,
        RetryableProviderError,
        SynthesisRequest,
        VoiceoverValidationError,
        bind_synthesis_identities,
        build_voice_plan,
        create_voice_manifest,
        plan_full_track_unit,
        plan_speech_units,
        synthesis_settings_from_plan,
        validate_voice_manifest,
        validate_voice_plan,
        voice_plan_audit_hash,
    )
    # edge_tts_adapter intentionally imports the protocol through the
    # top-level alias installed by scripts.voiceover.
    from .edge_tts_adapter import EdgeTtsAdapter
    from .doubao_adapter import DoubaoAdapter, DOUBAO_ENDPOINT, DOUBAO_PROVIDER_CONTRACT_VERSION
    from .minimax_adapter import MiniMaxAdapter, MINIMAX_PROVIDER_CONTRACT_VERSION
    from .voice_provider_config import VoiceProviderConfigError, active_provider_id, load_voice_provider_config
    from . import validation_receipts
except ImportError:  # pragma: no cover - direct script execution
    from audio_normalization import (
        AudioNormalizationError,
        AudioValidationError,
        CanonicalAudioResult,
        atomic_publish_wav,
        normalize_and_publish,
        normalize_to_candidate,
        validate_canonical_wav,
    )
    from edge_tts_adapter import EdgeTtsAdapter
    from doubao_adapter import DoubaoAdapter, DOUBAO_ENDPOINT, DOUBAO_PROVIDER_CONTRACT_VERSION
    from minimax_adapter import MiniMaxAdapter, MINIMAX_PROVIDER_CONTRACT_VERSION
    from voice_provider_config import VoiceProviderConfigError, active_provider_id, load_voice_provider_config
    from project_workspace import (
        Project,
        ProjectValidationError,
        WorkspaceConfig,
        ExecutionConcurrency,
        load_project,
        load_workspace_config,
        sha256_file,
        sha256_json,
        validate_timing_plan_data,
        write_json_atomic,
    )
    from srt_timeline import parse_srt, serialize_srt
    from content_source import spoken_text_weight
    from voiceover import (
        CancelledError,
        FULL_TRACK_SEGMENTATION,
        FULL_TRACK_SEGMENTATION_CONTRACT_VERSION,
        PermanentProviderError,
        ProviderAdapter,
        RetryableProviderError,
        SynthesisRequest,
        VoiceoverValidationError,
        bind_synthesis_identities,
        build_voice_plan,
        create_voice_manifest,
        plan_full_track_unit,
        plan_speech_units,
        synthesis_settings_from_plan,
        validate_voice_manifest,
        validate_voice_plan,
        voice_plan_audit_hash,
    )
    import validation_receipts

try:
    from .reference_audio_alignment import ReferenceAlignmentError, align_reference_audio
except ImportError:  # pragma: no cover - direct script execution / staged integration
    try:
        from reference_audio_alignment import ReferenceAlignmentError, align_reference_audio
    except ImportError:  # pragma: no cover - fail closed until the companion module is installed
        ReferenceAlignmentError = ValueError  # type: ignore[assignment,misc]
        align_reference_audio = None  # type: ignore[assignment]

try:
    from .transcribe_narration import (
        CONTRACT_VERSION as NARRATION_ASR_CONTRACT_VERSION,
        MAX_VAD_SEGMENT_MS as NARRATION_ASR_MAX_VAD_SEGMENT_MS,
        SEGMENT_RECONSTRUCTION_CONTRACT as NARRATION_ASR_RECONSTRUCTION_CONTRACT,
        VAD_SEGMENTATION_CONTRACT as NARRATION_ASR_SEGMENTATION_CONTRACT,
        transcribe_narration,
    )
except ImportError:  # pragma: no cover - direct script execution / staged integration
    try:
        from transcribe_narration import (  # type: ignore[no-redef]
            CONTRACT_VERSION as NARRATION_ASR_CONTRACT_VERSION,
            MAX_VAD_SEGMENT_MS as NARRATION_ASR_MAX_VAD_SEGMENT_MS,
            SEGMENT_RECONSTRUCTION_CONTRACT as NARRATION_ASR_RECONSTRUCTION_CONTRACT,
            VAD_SEGMENTATION_CONTRACT as NARRATION_ASR_SEGMENTATION_CONTRACT,
            transcribe_narration,
        )
    except ImportError:  # pragma: no cover - fail closed until the companion module is installed
        NARRATION_ASR_CONTRACT_VERSION = "narration-funasr-vad-token-evidence-v5"
        NARRATION_ASR_MAX_VAD_SEGMENT_MS = 15_000
        NARRATION_ASR_RECONSTRUCTION_CONTRACT = "funasr-vad-segment-token-reconstruction-v2"
        NARRATION_ASR_SEGMENTATION_CONTRACT = "paraformer-vad-15s-rate-guard-v1"
        transcribe_narration = None  # type: ignore[assignment]


VOICE_TIMELINE_SCHEMA_VERSION = 3
VOICE_TIMELINE_CONTRACT_VERSION = "audio-authoritative-timeline-v3"
VOICE_CLI_CONTRACT_VERSION = "voiceover-cli-v2"
DEFAULT_REQUEST_TIMEOUT_SECONDS = 60.0
LEGACY_CANONICAL_WAV_VALIDATOR_RECEIPT_VERSION = "canonical-wav-validator-receipt-v1"
CANONICAL_WAV_VALIDATOR_RECEIPT_VERSION = validation_receipts.CANDIDATE_RECEIPT_CONTRACT_VERSION
CANONICAL_WAV_VALIDATOR_CONTRACT_VERSION = "canonical-wav-validator-v2"


class ApprovalGateError(RuntimeError):
    """Current identity is stale or an explicit human approval is missing."""


class VoiceoverStateError(RuntimeError):
    """Persisted voice-over state is malformed or not technically current."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise VoiceoverStateError(f"无法读取 {label}: {exc}") from exc
    if not isinstance(value, dict):
        raise VoiceoverStateError(f"{label} 顶层必须是对象")
    return value


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _voice_paths(project: Project) -> dict[str, Path]:
    return {
        "plan": project.path("planning/voice-plan.json"),
        "manifest": project.path("manifests/voice-manifest.json"),
        "sample": project.path("previews/voice-sample.wav"),
        "composite": project.path("audio/narration.wav"),
        "timeline": project.path("audio/timeline.json"),
        "srt": project.path("audio/narration.srt"),
    }


def _load_source_context(project: Project) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if project.schema_version != 2 or project.voiceover_mode not in {"edge-tts", "minimax", "doubao"}:
        raise VoiceoverStateError("旁白 CLI 只允许 schema v2 的音频旁白项目")
    source_path = project.path(project.metadata["source"]["file"])
    cues = parse_srt(source_path.read_text(encoding="utf-8-sig"))
    scenes = [
        {
            "sceneId": scene["sceneId"],
            "sourceCueRange": list(scene["sourceCueRange"]),
        }
        for scene in project.timing_plan["scenes"]
    ]
    if not scenes:
        raise VoiceoverStateError("项目尚无已确认语义场景，不能规划旁白")
    return cues, scenes


def _build_plan_and_units(
    project: Project,
    *,
    voice: str,
    rate: int | str,
    provider_id: str = "edge-tts",
    provider_config: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    cues, scenes = _load_source_context(project)
    config = dict(provider_config or {})
    protocol = {
        "edge-tts": "edge-tts",
        "minimax": "MiniMax",
        "doubao": "Doubao",
    }.get(provider_id, provider_id)
    default_contract = {
        "edge-tts": "edge-tts-python-7.2.8-v1",
        "minimax": MINIMAX_PROVIDER_CONTRACT_VERSION,
        "doubao": DOUBAO_PROVIDER_CONTRACT_VERSION,
    }.get(provider_id, "")
    contract = config.get("contractVersion") or default_contract
    plan = build_voice_plan(
        project_id=project.project_id,
        source_srt_sha256=project.metadata["source"]["sha256"],
        cues=cues,
        scenes=scenes,
        voice=voice,
        language=str(config.get("language", "zh-CN")),
        rate=rate,
        pitch=config.get("pitch", 0),
        volume=config.get("volume", 0),
        output_format=str(config.get("outputFormat", "audio-24khz-48kbitrate-mono-mp3")),
        provider_id=provider_id,
        protocol=protocol,
        provider_contract_version=contract,
        provider_options={
            key: config[key]
            for key in (
                "model",
                "emotion",
                "textNormalization",
                "stream",
                "endpoint",
                "requestTimeoutSeconds",
            )
            if key in config
        },
        segmentation=FULL_TRACK_SEGMENTATION,
    )
    units = bind_synthesis_identities(
        plan_full_track_unit(cues, scenes, segmentation=plan["segmentation"]), plan
    )
    return plan, units


def _load_current_plan_units(project: Project) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    paths = _voice_paths(project)
    plan = validate_voice_plan(_read_json(paths["plan"], "voice plan"))
    if plan["projectId"] != project.project_id:
        raise VoiceoverStateError("voice plan projectId 与项目不一致")
    if plan["source"] != project.metadata["source"]:
        raise ApprovalGateError("voice plan 未绑定 current source SRT")
    cues, scenes = _load_source_context(project)
    expected = build_voice_plan(
        project_id=project.project_id,
        source_srt_sha256=project.metadata["source"]["sha256"],
        cues=cues,
        scenes=scenes,
        voice=plan["selection"]["voice"],
        language=plan["selection"]["language"],
        rate=plan["selection"]["rate"],
        pitch=plan["selection"]["pitch"],
        volume=plan["selection"]["volume"],
        output_format=plan["selection"]["outputFormat"],
        provider_id=plan["provider"]["id"],
        protocol=plan["provider"]["protocol"],
        provider_contract_version=plan["provider"]["contractVersion"],
        provider_options=plan["provider"].get("options", {}),
        source_file=plan["source"]["file"],
        segmentation=plan["segmentation"],
    )
    if expected != plan:
        raise ApprovalGateError("voice plan identities 已 stale")
    planner = (
        plan_full_track_unit
        if plan["segmentation"]["contractVersion"]
        == FULL_TRACK_SEGMENTATION_CONTRACT_VERSION
        else plan_speech_units
    )
    units = bind_synthesis_identities(planner(cues, scenes, segmentation=plan["segmentation"]), plan)
    return plan, units


def _sample_text(units: Sequence[Mapping[str, Any]]) -> str:
    natural: list[str] = []
    for unit in units:
        text = str(unit["speechText"]).strip()
        if not text:
            continue
        # full-track unit 只改变完整合成粒度；样音仍选取一条代表性自然句。
        starts = 0
        for match in re.finditer(r"[。！？!?；;]+|\n+", text):
            candidate = text[starts : match.end()].strip()
            if candidate:
                natural.append(candidate)
            starts = match.end()
        tail = text[starts:].strip()
        if tail:
            natural.append(tail)
    if not natural:
        raise VoiceoverStateError("没有可用于样音的自然中文文本")
    chinese = [text for text in natural if any("\u4e00" <= character <= "\u9fff" for character in text)]
    candidates = chinese or natural
    # Prefer a representative sentence near 24 code points, deterministically.
    return min(enumerate(candidates), key=lambda item: (abs(len(item[1]) - 24), item[0]))[1]


def _request(plan: Mapping[str, Any], text: str) -> SynthesisRequest:
    settings = synthesis_settings_from_plan(plan)
    timeout_seconds = plan["provider"].get("options", {}).get(
        "requestTimeoutSeconds", DEFAULT_REQUEST_TIMEOUT_SECONDS
    )
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or float(timeout_seconds) <= 0
    ):
        raise VoiceoverStateError("provider requestTimeoutSeconds 必须为正数")
    return SynthesisRequest(
        text=text,
        voice=settings["voice"],
        normalizedRate=settings["normalizedRate"],
        normalizedPitch=settings["normalizedPitch"],
        normalizedVolume=settings["normalizedVolume"],
        providerContractVersion=settings["providerContractVersion"],
        timeoutSeconds=float(timeout_seconds),
        cancellationToken=None,
    )


def _adapter_from_plan(plan: Mapping[str, Any]) -> ProviderAdapter:
    provider_id = plan["provider"]["id"]
    configured_provider = active_provider_id()
    if configured_provider != provider_id:
        raise VoiceoverStateError(
            "当前项目 provider 与 config/voice-providers.local.json 的 activeProvider 不一致；"
            "请切换 activeProvider 或使用匹配的项目"
        )
    if provider_id == "edge-tts":
        return EdgeTtsAdapter()
    if provider_id == "minimax":
        config = load_voice_provider_config(provider_id="minimax")
        options = plan["provider"].get("options", {})
        return MiniMaxAdapter(
            api_key=str(config["apiKey"]),
            model=str(options.get("model", config.get("model", "speech-2.8-hd"))),
            emotion=str(options.get("emotion", config.get("emotion", "calm"))),
            text_normalization=bool(options.get("textNormalization", config.get("textNormalization", True))),
            endpoint=str(options.get("endpoint", config.get("endpoint", "https://api.minimaxi.com/v1/t2a_v2"))),
            max_attempts=int(config.get("maxRetries", 2)) + 1,
            queue_interval_seconds=float(config.get("queueIntervalMs", 500)) / 1000.0,
            requests_per_minute=int(config.get("requestsPerMinute", 20)),
            rate_limit_backoff_seconds=float(config.get("rateLimitBackoffMs", 35000)) / 1000.0,
        )
    if provider_id == "doubao":
        config = load_voice_provider_config(provider_id="doubao")
        options = plan["provider"].get("options", {})
        return DoubaoAdapter(
            api_key=str(config["apiKey"]),
            model=str(options.get("model", config.get("model", "seed-audio-1.0"))),
            endpoint=str(options.get("endpoint", config.get("endpoint", DOUBAO_ENDPOINT))),
            max_attempts=int(config.get("maxRetries", 2)) + 1,
            queue_interval_seconds=float(config.get("queueIntervalMs", 500)) / 1000.0,
        )
    raise VoiceoverStateError(f"不支持的旁白 provider: {provider_id}")


def _media_dict(result: CanonicalAudioResult) -> dict[str, Any]:
    return {
        "file": None,
        "audioMime": "audio/wav",
        "audioCodec": result.codec,
        "sampleRate": result.sampleRate,
        "channels": result.channels,
        "bytes": result.bytes,
        "durationMs": result.durationMs,
        "sha256": result.sha256,
        "contractVersion": result.contractVersion,
    }


def _sample_identity(plan: Mapping[str, Any], text: str, media: Mapping[str, Any]) -> str:
    return sha256_json(
        {
            "contractVersion": VOICE_CLI_CONTRACT_VERSION,
            "voicePlanAuditHash": voice_plan_audit_hash(plan),
            "text": text,
            "mediaSha256": media["sha256"],
            "mediaContractVersion": media["contractVersion"],
        }
    )


def _sample_review_paths(
    paths: Mapping[str, Path], plan: Mapping[str, Any], identity: str
) -> tuple[Path, Path]:
    voice = str(plan["selection"]["voice"])
    safe_voice = re.sub(r"[^A-Za-z0-9._-]+", "_", voice).strip("._-")[:64] or "voice"
    review_audio = paths["sample"].with_name(
        f"voice-sample-{safe_voice}-{identity[:12]}.wav"
    )
    return review_audio, review_audio.with_suffix(".audit.json")


def _publish_sample_review_artifacts(
    paths: Mapping[str, Path],
    plan: Mapping[str, Any],
    media: Mapping[str, Any],
    identity: str,
    provider_request_id: str | None,
) -> tuple[Path, Path]:
    """发布不可变样音试听副本，避免播放器按 canonical 路径缓存旧音频。"""

    review_audio, audit_path = _sample_review_paths(paths, plan, identity)
    source = paths["sample"]
    expected_sha = str(media["sha256"])
    expected_bytes = int(media["bytes"])
    if review_audio.exists():
        if review_audio.stat().st_size != expected_bytes or sha256_file(review_audio) != expected_sha:
            raise VoiceoverStateError("样音 review identity 文件发生字节冲突")
    else:
        review_audio.parent.mkdir(parents=True, exist_ok=True)
        temporary = review_audio.with_name(f".{review_audio.name}.{uuid.uuid4().hex}.tmp")
        try:
            with source.open("rb") as input_handle, temporary.open("xb") as output_handle:
                shutil.copyfileobj(input_handle, output_handle)
                output_handle.flush()
                os.fsync(output_handle.fileno())
            if temporary.stat().st_size != expected_bytes or sha256_file(temporary) != expected_sha:
                raise VoiceoverStateError("样音 review 临时副本与 current WAV 不一致")
            os.replace(temporary, review_audio)
        finally:
            temporary.unlink(missing_ok=True)
    if review_audio.stat().st_size != expected_bytes or sha256_file(review_audio) != expected_sha:
        raise VoiceoverStateError("样音 review 发布后 SHA/bytes 核对失败")

    selection = plan["selection"]
    provider = plan["provider"]
    audit = {
        "schemaVersion": 1,
        "contractVersion": "sample-request-audit-v1",
        "provider": provider["id"],
        "model": provider.get("options", {}).get("model"),
        "request": {
            "voiceId": selection["voice"],
            "rate": selection["rate"],
            "volume": selection["volume"],
            "pitch": selection["pitch"],
        },
        "providerResponse": {
            "voiceIdEchoAvailable": False,
            "voiceIdEcho": None,
            **_provider_receipt(provider_request_id),
        },
        "sample": {
            "identitySha256": identity,
            "canonicalFile": "previews/voice-sample.wav",
            "reviewFile": f"previews/{review_audio.name}",
            "audioSha256": expected_sha,
            "bytes": expected_bytes,
            "durationMs": media["durationMs"],
            "approved": False,
        },
        "containsCredentials": False,
        "containsNarrationText": False,
    }
    if audit_path.exists():
        current_audit = _read_json(audit_path, "sample request audit")
        if current_audit != audit:
            raise VoiceoverStateError("样音 request audit identity 文件发生内容冲突")
    else:
        write_json_atomic(audit_path, audit)
    return review_audio, audit_path


def _write_manifest(path: Path, manifest: Mapping[str, Any], plan: Mapping[str, Any], units: Sequence[Mapping[str, Any]]) -> None:
    candidate = copy.deepcopy(dict(manifest))
    candidate["updatedAt"] = _now()
    validate_voice_manifest(candidate, voice_plan=plan, speech_units=units)
    write_json_atomic(path, candidate)


def _restore_bytes_atomic(path: Path, payload: bytes) -> None:
    """按原字节恢复正式文件，供跨文件批准事务失败时回滚。"""

    candidate = path.with_name(f".{path.name}.{uuid.uuid4().hex}.rollback")
    try:
        with candidate.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(candidate, path)
    finally:
        try:
            candidate.unlink()
        except FileNotFoundError:
            pass


def _fresh_manifest_with_reuse(
    project: Project,
    plan: Mapping[str, Any],
    units: Sequence[Mapping[str, Any]],
    old: Mapping[str, Any] | None,
) -> dict[str, Any]:
    manifest = create_voice_manifest(
        project_id=project.project_id, voice_plan=plan, speech_units=units
    )
    if not isinstance(old, Mapping):
        return manifest
    old_runs = old.get("runs")
    if isinstance(old_runs, list):
        manifest["runs"] = copy.deepcopy(old_runs)
    old_segments = old.get("segments")
    if not isinstance(old_segments, list):
        return manifest
    by_identity = {
        (segment.get("voiceSynthesisIdentityHash"), segment.get("relativePath")): segment
        for segment in old_segments
        if isinstance(segment, Mapping)
    }
    for segment in manifest["segments"]:
        prior = by_identity.get(
            (segment["voiceSynthesisIdentityHash"], segment["relativePath"])
        )
        if prior is None:
            continue
        for field in (
            "status", "audioMime", "audioCodec", "contractVersion", "sampleRate", "channels",
            "bytes", "durationMs", "sha256", "attempts", "createdAt", "updatedAt",
            "errorStage", "errorSummary", "currentAttempt",
        ):
            segment[field] = copy.deepcopy(prior.get(field))
    return manifest


def _sample(
    project: Project,
    *,
    voice: str,
    rate: int,
    provider_id: str = "edge-tts",
    provider_config: Mapping[str, Any] | None = None,
    adapter: ProviderAdapter,
    normalizer: Callable[..., CanonicalAudioResult],
) -> tuple[str, str, str, str, str]:
    plan, units = _build_plan_and_units(
        project, voice=voice, rate=rate, provider_id=provider_id, provider_config=provider_config
    )
    paths = _voice_paths(project)
    old = _read_json(paths["manifest"], "voice manifest") if paths["manifest"].is_file() else None
    manifest = _fresh_manifest_with_reuse(project, plan, units, old)
    text = _sample_text(units)
    run_dir = project.create_run_dir(f"voice-sample-{uuid.uuid4().hex}")
    raw = adapter.synthesize(_request(plan, text))
    result = normalizer(
        raw.bytes,
        paths["sample"],
        work_dir=run_dir,
        declared_format=raw.declaredFormat,
    )
    media = _media_dict(result)
    media["file"] = "previews/voice-sample.wav"
    identity = _sample_identity(plan, text, media)
    full_text = "\n\n".join(str(unit["speechText"]) for unit in units)
    sample_weight = spoken_text_weight(text)
    full_weight = spoken_text_weight(full_text)
    estimated_full_ms = max(1, round(media["durationMs"] * full_weight / sample_weight))
    source_duration_ms = parse_srt(
        project.path("source/source.srt").read_text(encoding="utf-8-sig")
    )[-1]["endMs"]
    duration_estimate = {
        "estimatedFullDurationMs": estimated_full_ms,
        "targetDurationMs": source_duration_ms,
        "estimatedDeltaMs": estimated_full_ms - source_duration_ms,
        "estimatedDeviationRatio": round(
            abs(estimated_full_ms - source_duration_ms) / source_duration_ms,
            6,
        ),
        "estimateBasis": "sample_weight_ratio_v1",
        "sampleTextWeight": sample_weight,
        "fullNarrationWeight": full_weight,
        "authoritative": False,
    }
    review_audio, audit_path = _publish_sample_review_artifacts(
        paths, plan, media, identity, raw.providerRequestId
    )
    manifest["sample"] = {
        "status": "validated",
        "text": text,
        "identityHash": identity,
        "media": media,
        "providerReceipt": _provider_receipt(raw.providerRequestId),
        "durationEstimate": duration_estimate,
        "approval": {
            "approved": False,
            "identityHash": None,
            "approvalBasis": None,
            "approvedAt": None,
        },
    }
    manifest["runs"].append(
        {"kind": "sample", "status": "validated", "startedAt": _now(), "finishedAt": _now()}
    )
    write_json_atomic(paths["plan"], plan)
    _write_manifest(paths["manifest"], manifest, plan, units)
    return (
        str(paths["sample"].resolve()),
        identity,
        str(review_audio.resolve()),
        str(audit_path.resolve()),
        str(media["sha256"]),
    )


def _canonical_validator_receipt(result: CanonicalAudioResult) -> dict[str, Any]:
    return validation_receipts.build_candidate_receipt(
        candidate_sha256=result.sha256,
        candidate_bytes=result.bytes,
        decoded=True,
        format="WAV",
        validator_contract=CANONICAL_WAV_VALIDATOR_CONTRACT_VERSION,
        evidence={
            "legacyContractVersion": LEGACY_CANONICAL_WAV_VALIDATOR_RECEIPT_VERSION,
            "mediaContractVersion": result.contractVersion,
            "audioCodec": result.codec,
            "sampleRate": result.sampleRate,
            "channels": result.channels,
            "durationMs": result.durationMs,
        },
    )


def _canonical_receipt_evidence(receipt: Mapping[str, Any] | None) -> Mapping[str, Any]:
    if not isinstance(receipt, Mapping):
        return {}
    try:
        read = validation_receipts.read_candidate_receipt(receipt)
    except validation_receipts.ReceiptValidationError:
        return receipt
    evidence = read.receipt.get("evidence") if read.current_contract else read.receipt
    return evidence if isinstance(evidence, Mapping) else {}


def _canonical_result_from_binding(
    path: Path,
    media: Mapping[str, Any],
    receipt: Mapping[str, Any] | None,
) -> CanonicalAudioResult | None:
    """Use SHA/bytes binding when a current deep validator receipt exists."""

    if not isinstance(receipt, Mapping):
        return None
    try:
        receipt_read = validation_receipts.read_candidate_receipt(receipt)
    except validation_receipts.ReceiptValidationError as exc:
        raise ApprovalGateError(str(exc)) from exc
    if not receipt_read.current_contract:
        return None
    candidate_receipt = receipt_read.receipt
    # 先用 receipt 自带合同校验其签名和 current bytes；合法的旧 validator
    # contract 才能触发 deep，篡改或 bytes 不匹配必须 fail-closed。
    receipt_contract = candidate_receipt.get("validatorContract")
    if not isinstance(receipt_contract, str):
        raise ApprovalGateError("WAV candidate receipt validator contract 无效")
    try:
        validation_receipts.bind_candidate_receipt(
            path,
            candidate_receipt,
            expected_format="WAV",
            expected_validator_contract=receipt_contract,
        )
    except validation_receipts.ReceiptValidationError as exc:
        raise ApprovalGateError(str(exc)) from exc
    if receipt_contract != CANONICAL_WAV_VALIDATOR_CONTRACT_VERSION:
        return None
    evidence = _canonical_receipt_evidence(candidate_receipt)
    expected = {
        "mediaContractVersion": media.get("contractVersion"),
        "audioCodec": media.get("audioCodec"),
        "sampleRate": media.get("sampleRate"),
        "channels": media.get("channels"),
        "durationMs": media.get("durationMs"),
    }
    if any(evidence.get(key) != value for key, value in expected.items()):
        raise ApprovalGateError("WAV candidate receipt 与 current media binding 不一致")
    return CanonicalAudioResult(
        path=path.resolve(),
        contractVersion=str(expected["mediaContractVersion"]),
        codec=str(expected["audioCodec"]),
        sampleRate=int(expected["sampleRate"]),
        channels=int(expected["channels"]),
        durationMs=int(expected["durationMs"]),
        bytes=int(candidate_receipt["candidateBytes"]),
        sha256=str(candidate_receipt["candidateSha256"]),
    )


def _validate_media_ref(project: Project, ref: Mapping[str, Any], *, expected_file: str) -> CanonicalAudioResult:
    if ref.get("file", expected_file) != expected_file:
        raise VoiceoverStateError(f"媒体引用必须为 {expected_file}")
    result = validate_canonical_wav(project.path(expected_file))
    expected = {
        "audioCodec": result.codec,
        "sampleRate": result.sampleRate,
        "channels": result.channels,
        "bytes": result.bytes,
        "durationMs": result.durationMs,
        "sha256": result.sha256,
        "contractVersion": result.contractVersion,
    }
    for key, value in expected.items():
        if ref.get(key) != value:
            raise ApprovalGateError(f"{expected_file} 的 {key} 与登记身份不一致")
    return result


def _validate_current_sample(project: Project, plan: Mapping[str, Any], manifest: Mapping[str, Any]) -> str:
    sample = manifest.get("sample")
    if not isinstance(sample, Mapping) or sample.get("status") != "validated":
        raise ApprovalGateError("current 样音尚未技术验证")
    media = sample.get("media")
    if not isinstance(media, Mapping):
        raise ApprovalGateError("current 样音缺少媒体身份")
    _validate_media_ref(project, media, expected_file="previews/voice-sample.wav")
    identity = _sample_identity(plan, str(sample.get("text", "")), media)
    if sample.get("identityHash") != identity:
        raise ApprovalGateError("样音 identity 已 stale")
    return identity


def _approve_sample(project: Project, identity_hash: str) -> str:
    plan, units = _load_current_plan_units(project)
    paths = _voice_paths(project)
    manifest = validate_voice_manifest(
        _read_json(paths["manifest"], "voice manifest"), voice_plan=plan, speech_units=units
    )
    current = _validate_current_sample(project, plan, manifest)
    if identity_hash != current:
        raise ApprovalGateError("提交的样音 identity 与 current sample 不一致")
    manifest["sample"]["approval"] = {
        "approved": True,
        "identityHash": current,
        "approvalBasis": "user_sample_listening",
        "approvedAt": _now(),
    }
    _write_manifest(paths["manifest"], manifest, plan, units)
    return current


def _checkpoint_segment(
    path: Path,
    manifest: dict[str, Any],
    plan: Mapping[str, Any],
    units: Sequence[Mapping[str, Any]],
    segment: dict[str, Any],
    *,
    status: str,
    error_stage: str | None = None,
    error_summary: str | None = None,
) -> None:
    segment["status"] = status
    attempt = segment.get("currentAttempt")
    if isinstance(attempt, dict):
        attempt["status"] = status
    segment["updatedAt"] = _now()
    segment["errorStage"] = error_stage
    segment["errorSummary"] = error_summary[:300] if isinstance(error_summary, str) else None
    _write_manifest(path, manifest, plan, units)


def _candidate_validator_receipt(result: CanonicalAudioResult) -> dict[str, Any]:
    return _canonical_validator_receipt(result)


def _provider_receipt(request_id: str | None) -> dict[str, Any]:
    if not request_id:
        return {"providerRequestIdHash": None}
    digest = hashlib.sha256(request_id.encode("utf-8")).hexdigest()
    return {"providerRequestIdHash": f"sha256:{digest[:16]}"}


def _new_segment_attempt(
    project: Project,
    segment: dict[str, Any],
    unit: Mapping[str, Any],
    run_dir: Path,
) -> dict[str, Any]:
    attempt_number = int(segment.get("attempts") or 0) + 1
    attempt_id = f"unit-{unit['index']:04d}-attempt-{attempt_number:04d}"
    candidate = (
        run_dir
        / "external"
        / f"u{unit['index']:04d}-a{attempt_number:04d}.wav"
    )
    candidate.parent.mkdir(parents=True, exist_ok=True)
    candidate_relative = candidate.resolve().relative_to(project.root.resolve()).as_posix()
    attempt = {
        "attemptId": attempt_id,
        "status": "prepared",
        "inputIdentitySha256": unit["voiceSynthesisIdentityHash"],
        "candidateFile": candidate_relative,
        "candidateSha256": None,
        "candidateBytes": None,
        "validatorReceipt": None,
        "formalFile": segment["relativePath"],
        "externalOutcome": "not_started",
        "providerReceipt": None,
    }
    segment["attempts"] = attempt_number
    segment["currentAttempt"] = attempt
    return attempt


def _attempt_candidate(project: Project, segment: Mapping[str, Any]) -> Path:
    attempt = segment.get("currentAttempt")
    if not isinstance(attempt, Mapping):
        raise VoiceoverStateError("segment 缺少 current attempt")
    if attempt.get("inputIdentitySha256") != segment.get("voiceSynthesisIdentityHash"):
        raise ApprovalGateError("segment attempt synthesis identity 已 stale")
    candidate = project.path(str(attempt.get("candidateFile")))
    try:
        relative = candidate.resolve(strict=False).relative_to(project.path(".work").resolve())
    except ValueError as exc:
        raise VoiceoverStateError("segment attempt candidate 越出项目 .work") from exc
    if not relative.parts:
        raise VoiceoverStateError("segment attempt candidate 路径无效")
    return candidate


def _validate_attempt_candidate(
    project: Project,
    segment: dict[str, Any],
    *,
    validated_result: CanonicalAudioResult | None = None,
) -> CanonicalAudioResult:
    attempt = segment["currentAttempt"]
    candidate = _attempt_candidate(project, segment)
    receipt = attempt.get("validatorReceipt")
    receipt_evidence = _canonical_receipt_evidence(receipt)
    media = {
        "contractVersion": receipt_evidence.get("mediaContractVersion"),
        "audioCodec": receipt_evidence.get("audioCodec"),
        "sampleRate": receipt_evidence.get("sampleRate"),
        "channels": receipt_evidence.get("channels"),
        "bytes": attempt.get("candidateBytes"),
        "durationMs": receipt_evidence.get("durationMs"),
        "sha256": attempt.get("candidateSha256"),
    }
    reused_current_receipt = False
    result = validated_result
    if result is None:
        result = _canonical_result_from_binding(candidate, media, receipt)
        reused_current_receipt = result is not None
    if result is None:
        result = validate_canonical_wav(candidate)
    elif result.path.resolve() != candidate.resolve():
        raise VoiceoverStateError("worker validator receipt 未绑定当前 attempt candidate")
    if candidate.stat().st_size != result.bytes or sha256_file(candidate) != result.sha256:
        raise ApprovalGateError("attempt candidate 在 worker 验证后发生变化")
    if attempt.get("candidateSha256") not in (None, result.sha256):
        raise ApprovalGateError("attempt candidate SHA 与 checkpoint 不一致")
    if attempt.get("candidateBytes") not in (None, result.bytes):
        raise ApprovalGateError("attempt candidate bytes 与 checkpoint 不一致")
    receipt = (
        copy.deepcopy(dict(receipt))
        if reused_current_receipt and isinstance(receipt, Mapping)
        else _candidate_validator_receipt(result)
    )
    previous_receipt = attempt.get("validatorReceipt")
    if previous_receipt not in (None, receipt):
        try:
            previous_read = validation_receipts.read_candidate_receipt(previous_receipt)
        except validation_receipts.ReceiptValidationError as exc:
            raise ApprovalGateError("attempt candidate validator receipt 已 stale") from exc
        if previous_read.current_contract:
            raise ApprovalGateError("attempt candidate validator receipt 已 stale")
    attempt["candidateSha256"] = result.sha256
    attempt["candidateBytes"] = result.bytes
    attempt["validatorReceipt"] = receipt
    return result


def _publish_segment_candidate(project: Project, segment: dict[str, Any]) -> None:
    attempt = segment["currentAttempt"]
    candidate = _attempt_candidate(project, segment)
    expected_sha = attempt.get("candidateSha256")
    expected_bytes = attempt.get("candidateBytes")
    if not isinstance(expected_sha, str) or not isinstance(expected_bytes, int):
        raise VoiceoverStateError("candidate_ready attempt 缺少 SHA/bytes")
    destination = project.path(segment["relativePath"])
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    try:
        with candidate.open("rb") as source, temporary.open("wb") as handle:
            shutil.copyfileobj(source, handle)
            handle.flush()
            os.fsync(handle.fileno())
        if temporary.stat().st_size != expected_bytes or sha256_file(temporary) != expected_sha:
            raise VoiceoverStateError("正式发布临时副本与 candidate SHA/bytes 不一致")
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    if destination.stat().st_size != expected_bytes or sha256_file(destination) != expected_sha:
        raise VoiceoverStateError("正式 segment 发布后 SHA/bytes 核对失败")


def _apply_candidate_media(segment: dict[str, Any]) -> None:
    receipt = segment["currentAttempt"]["validatorReceipt"]
    evidence = _canonical_receipt_evidence(receipt)
    segment.update(
        {
            "audioMime": "audio/wav",
            "audioCodec": evidence["audioCodec"],
            "contractVersion": evidence["mediaContractVersion"],
            "sampleRate": evidence["sampleRate"],
            "channels": evidence["channels"],
            "bytes": receipt["candidateBytes"],
            "durationMs": evidence["durationMs"],
            "sha256": receipt["candidateSha256"],
            "createdAt": segment.get("createdAt") or _now(),
        }
    )


def _synthesize_candidate_worker(
    *,
    plan: Mapping[str, Any],
    unit: Mapping[str, Any],
    candidate: Path,
    work_dir: Path,
    adapter: ProviderAdapter,
    normalizer: Callable[..., CanonicalAudioResult],
) -> dict[str, Any]:
    provider_returned = False
    try:
        raw = adapter.synthesize(_request(plan, unit["speechText"]))
        provider_returned = True
        result = normalizer(
            raw.bytes,
            candidate,
            work_dir=work_dir,
            declared_format=raw.declaredFormat,
        )
        return {
            "result": result,
            "providerReceipt": _provider_receipt(raw.providerRequestId),
        }
    except Exception as exc:  # classified by the single-writer coordinator
        return {
            "exception": exc,
            "providerReturned": provider_returned,
            "candidateExists": candidate.is_file(),
        }


def _segment_is_reusable(
    project: Project,
    segment: Mapping[str, Any],
    unit: Mapping[str, Any],
    *,
    force_deep: bool = False,
    persist_deep: bool = False,
) -> bool:
    if segment.get("status") != "validated":
        return False
    if segment.get("voiceSynthesisIdentityHash") != unit.get("voiceSynthesisIdentityHash"):
        return False
    relative = segment.get("relativePath")
    if relative != f"audio/segments/unit-{unit['index']:04d}.wav":
        raise VoiceoverStateError("validated segment 正式路径与 unit 不一致")
    path = project.path(relative)
    attempt = segment.get("currentAttempt")
    receipt = attempt.get("validatorReceipt") if isinstance(attempt, Mapping) else None
    result = None if force_deep else _canonical_result_from_binding(path, segment, receipt)
    if result is None:
        result = validate_canonical_wav(path)
        if persist_deep:
            if isinstance(segment, dict):
                segment["contractVersion"] = result.contractVersion
            if isinstance(attempt, dict):
                attempt["validatorReceipt"] = _canonical_validator_receipt(result)
    expected = {
        "audioCodec": result.codec,
        "sampleRate": result.sampleRate,
        "channels": result.channels,
        "bytes": result.bytes,
        "durationMs": result.durationMs,
        "sha256": result.sha256,
    }
    if any(segment.get(key) != value for key, value in expected.items()):
        raise ApprovalGateError(f"{relative} 与 validated checkpoint 媒体合同不一致")
    return True


def _recover_segment_attempt(
    project: Project,
    manifest_path: Path,
    manifest: dict[str, Any],
    plan: Mapping[str, Any],
    units: Sequence[Mapping[str, Any]],
    segment: dict[str, Any],
    unit: Mapping[str, Any],
    *,
    retry_failed: bool,
) -> bool:
    """恢复已登记 attempt；返回 True 表示正式 segment 已 validated。"""

    status = segment.get("status")
    if status == "validated":
        return _segment_is_reusable(project, segment, unit)
    if status == "pending":
        return False
    if status in {"failed", "cancelled"}:
        if retry_failed:
            return False
        raise VoiceoverStateError("存在 failed/cancelled unit；请显式使用 --retry-failed")
    if status == "unknown_external_outcome":
        raise ApprovalGateError("存在 unknown external outcome；禁止自动重复请求，需人工决定")
    if status == "normalizing":
        raise ApprovalGateError("旧 normalizing checkpoint 无确定 candidate；禁止自动重复请求")
    attempt = segment.get("currentAttempt")
    if not isinstance(attempt, dict):
        if status == "requesting":
            segment["currentAttempt"] = {
                "attemptId": f"legacy-unit-{unit['index']:04d}",
                "status": "requesting",
                "inputIdentitySha256": unit["voiceSynthesisIdentityHash"],
                "candidateFile": f".work/legacy-missing/unit-{unit['index']:04d}/candidate.wav",
                "candidateSha256": None,
                "candidateBytes": None,
                "validatorReceipt": None,
                "formalFile": segment["relativePath"],
                "externalOutcome": "unknown",
                "providerReceipt": None,
            }
            _checkpoint_segment(
                manifest_path, manifest, plan, units, segment,
                status="unknown_external_outcome",
                error_stage="recovery",
                error_summary="旧 requesting checkpoint 缺少确定 candidate",
            )
            raise ApprovalGateError("requesting 结果不确定；禁止自动重复请求")
        raise VoiceoverStateError("非 pending segment 缺少 current attempt")
    candidate = _attempt_candidate(project, segment)
    if status == "prepared":
        return False
    if status == "requesting":
        if not candidate.is_file():
            attempt["externalOutcome"] = "unknown"
            _checkpoint_segment(
                manifest_path, manifest, plan, units, segment,
                status="unknown_external_outcome",
                error_stage="provider",
                error_summary="requesting 后 candidate 不存在且 provider 不支持幂等查询",
            )
            raise ApprovalGateError("provider 结果不确定；禁止自动重复请求")
        attempt["externalOutcome"] = "succeeded"
        _validate_attempt_candidate(project, segment)
        _checkpoint_segment(manifest_path, manifest, plan, units, segment, status="candidate_ready")
        status = "candidate_ready"
    if status == "candidate_ready":
        _validate_attempt_candidate(project, segment)
        _checkpoint_segment(manifest_path, manifest, plan, units, segment, status="publishing")
        status = "publishing"
    if status == "publishing":
        _validate_attempt_candidate(project, segment)
        destination = project.path(segment["relativePath"])
        expected_sha = attempt["candidateSha256"]
        expected_bytes = attempt["candidateBytes"]
        if destination.exists():
            if destination.stat().st_size != expected_bytes or sha256_file(destination) != expected_sha:
                raise ApprovalGateError("publishing 恢复发现正式 segment 与 candidate 冲突")
        else:
            _publish_segment_candidate(project, segment)
        _apply_candidate_media(segment)
        _checkpoint_segment(manifest_path, manifest, plan, units, segment, status="validated")
        return True
    raise VoiceoverStateError(f"无法恢复 segment 状态: {status}")


def _merge_segments(project: Project, segments: Sequence[Mapping[str, Any]], run_dir: Path) -> CanonicalAudioResult:
    candidate = run_dir / "narration.candidate.wav"
    params: tuple[int, int, int] | None = None
    with wave.open(str(candidate), "wb") as output:
        for segment in segments:
            path = project.path(segment["relativePath"])
            with wave.open(str(path), "rb") as source:
                current = (source.getnchannels(), source.getsampwidth(), source.getframerate())
                if params is None:
                    params = current
                    output.setnchannels(current[0])
                    output.setsampwidth(current[1])
                    output.setframerate(current[2])
                    output.setcomptype("NONE", "not compressed")
                elif current != params:
                    raise AudioValidationError("canonical segments 媒体参数不一致")
                output.writeframes(source.readframes(source.getnframes()))
    validated = validate_canonical_wav(candidate)
    destination = project.path("audio/narration.wav")
    atomic_publish_wav(candidate, destination)
    if destination.stat().st_size != validated.bytes or sha256_file(destination) != validated.sha256:
        raise AudioValidationError("narration.wav 发布后 SHA/bytes binding 失败")
    return CanonicalAudioResult(
        path=destination.resolve(),
        contractVersion=validated.contractVersion,
        codec=validated.codec,
        sampleRate=validated.sampleRate,
        channels=validated.channels,
        durationMs=validated.durationMs,
        bytes=validated.bytes,
        sha256=validated.sha256,
    )


def _build_timeline(
    project: Project,
    plan: Mapping[str, Any],
    units: Sequence[Mapping[str, Any]],
    segments: Sequence[Mapping[str, Any]],
    composite: CanonicalAudioResult,
) -> tuple[dict[str, Any], str]:
    timeline_units: list[dict[str, Any]] = []
    cursor = 0
    for position, (unit, segment) in enumerate(zip(units, segments)):
        end = composite.durationMs if position == len(units) - 1 else cursor + int(segment["durationMs"])
        if end <= cursor:
            raise VoiceoverStateError("旁白 unit 实测时长必须为正")
        timeline_units.append(
            {
                "index": unit["index"],
                "sceneId": unit["sceneId"],
                "sourceCueRange": list(unit["sourceCueRange"]),
                "sourceOrdinalRange": list(unit["sourceOrdinalRange"]),
                "text": unit["speechText"],
                "startMs": cursor,
                "endMs": end,
                "durationMs": end - cursor,
                "segmentSha256": segment["sha256"],
                "voiceSynthesisIdentityHash": unit["voiceSynthesisIdentityHash"],
            }
        )
        cursor = end
    if cursor != composite.durationMs:
        raise VoiceoverStateError("最后 unit 未收口到整轨实测时长")

    fps = int(project.render_profile["fps"])
    scene_specs = project.timing_plan["scenes"]
    scenes: list[dict[str, Any]] = []
    start_ms = 0
    start_frame = 0
    for index, spec in enumerate(scene_specs):
        scene_id = spec["sceneId"]
        scene_units = [unit for unit in timeline_units if unit["sceneId"] == scene_id]
        if not scene_units:
            raise VoiceoverStateError(f"{scene_id} 未覆盖任何 speech unit")
        end_ms = composite.durationMs if index == len(scene_specs) - 1 else scene_units[-1]["endMs"]
        if scene_units[0]["startMs"] != start_ms or scene_units[-1]["endMs"] != end_ms:
            raise VoiceoverStateError(f"{scene_id} units 未连续覆盖场景")
        end_frame = (end_ms * fps + 999) // 1000
        scenes.append(
            {
                "sceneId": scene_id,
                "sourceCueRange": list(spec["sourceCueRange"]),
                "unitRange": [scene_units[0]["index"], scene_units[-1]["index"]],
                "startMs": start_ms,
                "endMs": end_ms,
                "sceneDurationMs": end_ms - start_ms,
                "startFrame": start_frame,
                "endFrameExclusive": end_frame,
                "frameCount": end_frame - start_frame,
            }
        )
        start_ms = end_ms
        start_frame = end_frame

    srt_cues = [
        {
            "originalIndex": unit["index"],
            "startMs": unit["startMs"],
            "endMs": unit["endMs"],
            "text": unit["text"],
        }
        for unit in timeline_units
    ]
    narration_srt = serialize_srt(srt_cues)
    base = {
        "schemaVersion": VOICE_TIMELINE_SCHEMA_VERSION,
        "contractVersion": VOICE_TIMELINE_CONTRACT_VERSION,
        "projectId": project.project_id,
        "sourceSrt": {"file": "source/source.srt", "sha256": plan["source"]["sha256"]},
        "voicePlanAuditHash": voice_plan_audit_hash(plan),
        "audio": {
            "file": "audio/narration.wav",
            "sha256": composite.sha256,
            "durationMs": composite.durationMs,
            "bytes": composite.bytes,
            "contractVersion": composite.contractVersion,
        },
        "renderProfileSha256": sha256_json(project.render_profile),
        "units": timeline_units,
        "scenes": scenes,
        # Intentionally no self file SHA: this is the frozen acyclic linkage.
        "narrationSrt": {
            "file": "audio/narration.srt",
            "sha256": _sha256_text(narration_srt),
        },
    }
    return base, narration_srt


def _publish_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    candidate = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    candidate.write_text(text, encoding="utf-8", newline="\n")
    os.replace(candidate, path)


def _full_identity(
    plan: Mapping[str, Any], composite: Mapping[str, Any], timeline: Mapping[str, Any], narration: Mapping[str, Any]
) -> str:
    return sha256_json(
        {
            "contractVersion": VOICE_CLI_CONTRACT_VERSION,
            "voicePlanAuditHash": voice_plan_audit_hash(plan),
            "audioSha256": composite["sha256"],
            "timelineSha256": timeline["sha256"],
            "narrationSrtSha256": narration["sha256"],
        }
    )


def _full_audio_identity(plan: Mapping[str, Any], composite: Mapping[str, Any]) -> str:
    """整轨音频技术身份；它不是可批准的 FULL_IDENTITY。"""

    return sha256_json(
        {
            "contractVersion": "full-track-audio-v1",
            "voicePlanAuditHash": voice_plan_audit_hash(plan),
            "audioSha256": composite["sha256"],
        }
    )


def _preflight_narration_asr() -> None:
    """在任何完整 TTS 请求前验证本地 FunASR 运行时与固定模型 receipt。"""

    if transcribe_narration is None:
        raise VoiceoverStateError(
            "当前 skill 的 narration ASR runner 尚未安装；请先准备 narration-asr 环境"
        )
    try:
        try:
            from .prepare_env import (
                load_narration_asr_model_paths,
                probe_narration_asr_runtime,
            )
        except ImportError:  # pragma: no cover - direct script execution
            from prepare_env import (  # type: ignore[no-redef]
                load_narration_asr_model_paths,
                probe_narration_asr_runtime,
            )
        probe_narration_asr_runtime(Path(sys.executable).resolve(), os.environ.copy())
        load_narration_asr_model_paths()
    except Exception as exc:
        raise VoiceoverStateError(
            f"narration-ASR preflight 失败；完整 TTS 尚未请求: {str(exc)[-300:]}"
        ) from exc


def _full(
    project: Project,
    *,
    retry_failed: bool,
    adapter: ProviderAdapter,
    normalizer: Callable[..., CanonicalAudioResult],
    configured_concurrency: int,
    asr_preflight: Callable[[], None],
) -> str:
    plan, units = _load_current_plan_units(project)
    if (
        plan["segmentation"]["contractVersion"]
        != FULL_TRACK_SEGMENTATION_CONTRACT_VERSION
        or len(units) != 1
    ):
        raise ApprovalGateError("旧逐句 voice plan 已 stale；请重新生成并批准 full-track 样音")
    paths = _voice_paths(project)
    old = _read_json(paths["manifest"], "voice manifest")
    manifest = _fresh_manifest_with_reuse(project, plan, units, old)
    current_sample = _validate_current_sample(project, plan, old)
    approval = old.get("sample", {}).get("approval", {})
    if not approval.get("approved") or approval.get("identityHash") != current_sample:
        raise ApprovalGateError("未获得 current 样音 voice/rate 人工批准")
    asr_preflight()
    # Preserve only the current sample approval; full/timeline approvals are reset.
    manifest["sample"] = copy.deepcopy(old["sample"])
    if isinstance(configured_concurrency, bool) or not isinstance(configured_concurrency, int):
        raise VoiceoverStateError("voiceGeneration concurrency 必须是整数")
    if not 1 <= configured_concurrency <= 16:
        raise VoiceoverStateError("voiceGeneration concurrency 必须位于 1–16")
    run = {
        "kind": "full-retry" if retry_failed else "full",
        "status": "running",
        "startedAt": _now(),
        "finishedAt": None,
        "configuredConcurrency": configured_concurrency,
        "effectiveConcurrency": 0,
        "taskCount": len(units),
    }
    manifest["runs"].append(run)
    _write_manifest(paths["manifest"], manifest, plan, units)
    run_dir = project.create_run_dir(f"voice-generate-{uuid.uuid4().hex}")
    pending: list[tuple[Mapping[str, Any], dict[str, Any]]] = []
    try:
        for unit, segment in zip(units, manifest["segments"]):
            if not _recover_segment_attempt(
                project,
                paths["manifest"],
                manifest,
                plan,
                units,
                segment,
                unit,
                retry_failed=retry_failed,
            ):
                pending.append((unit, segment))
    except Exception as exc:
        run["status"] = "failed"
        run["finishedAt"] = _now()
        run["errorStage"] = "attempt-recovery"
        run["errorSummary"] = str(exc)[:300]
        _write_manifest(paths["manifest"], manifest, plan, units)
        raise

    run["effectiveConcurrency"] = min(configured_concurrency, len(pending)) if pending else 0
    _write_manifest(paths["manifest"], manifest, plan, units)
    first_error: Exception | None = None
    stop_dispatch = False
    next_pending = 0
    futures: dict[
        concurrent.futures.Future[dict[str, Any]],
        tuple[Mapping[str, Any], dict[str, Any]],
    ] = {}

    def submit_one(executor: concurrent.futures.ThreadPoolExecutor) -> None:
        nonlocal next_pending
        unit, segment = pending[next_pending]
        next_pending += 1
        if segment.get("status") == "prepared" and isinstance(segment.get("currentAttempt"), dict):
            attempt = segment["currentAttempt"]
        else:
            attempt = _new_segment_attempt(project, segment, unit, run_dir)
            _checkpoint_segment(paths["manifest"], manifest, plan, units, segment, status="prepared")
        attempt["externalOutcome"] = "requesting"
        _checkpoint_segment(paths["manifest"], manifest, plan, units, segment, status="requesting")
        candidate = _attempt_candidate(project, segment)
        future = executor.submit(
            _synthesize_candidate_worker,
            plan=plan,
            unit=unit,
            candidate=candidate,
            work_dir=candidate.parent,
            adapter=adapter,
            normalizer=normalizer,
        )
        futures[future] = (unit, segment)

    if pending:
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=configured_concurrency,
            thread_name_prefix="voice-generation",
        ) as executor:
            while next_pending < len(pending) and len(futures) < configured_concurrency:
                submit_one(executor)
            while futures:
                done, _ = concurrent.futures.wait(
                    futures, return_when=concurrent.futures.FIRST_COMPLETED
                )
                ordered_done = sorted(done, key=lambda future: futures[future][0]["index"])
                for future in ordered_done:
                    unit, segment = futures.pop(future)
                    outcome = future.result()
                    attempt = segment["currentAttempt"]
                    worker_error = outcome.get("exception")
                    if worker_error is None:
                        result = outcome.get("result")
                        if not isinstance(result, CanonicalAudioResult):
                            worker_error = VoiceoverStateError("worker 未返回 canonical candidate receipt")
                        else:
                            attempt["externalOutcome"] = "succeeded"
                            attempt["providerReceipt"] = outcome.get("providerReceipt")
                            attempt["candidateSha256"] = result.sha256
                            attempt["candidateBytes"] = result.bytes
                            attempt["validatorReceipt"] = _candidate_validator_receipt(result)
                            try:
                                _validate_attempt_candidate(
                                    project, segment, validated_result=result
                                )
                                _checkpoint_segment(
                                    paths["manifest"], manifest, plan, units, segment,
                                    status="candidate_ready",
                                )
                                _checkpoint_segment(
                                    paths["manifest"], manifest, plan, units, segment,
                                    status="publishing",
                                )
                                _publish_segment_candidate(project, segment)
                                _apply_candidate_media(segment)
                                _checkpoint_segment(
                                    paths["manifest"], manifest, plan, units, segment,
                                    status="validated",
                                )
                            except Exception as exc:
                                worker_error = exc
                    if worker_error is not None:
                        stop_dispatch = True
                        candidate_exists = bool(outcome.get("candidateExists")) or _attempt_candidate(
                            project, segment
                        ).is_file()
                        if candidate_exists:
                            try:
                                attempt["externalOutcome"] = "succeeded"
                                _validate_attempt_candidate(project, segment)
                                _checkpoint_segment(
                                    paths["manifest"], manifest, plan, units, segment,
                                    status="candidate_ready",
                                )
                                _checkpoint_segment(
                                    paths["manifest"], manifest, plan, units, segment,
                                    status="publishing",
                                )
                                _publish_segment_candidate(project, segment)
                                _apply_candidate_media(segment)
                                _checkpoint_segment(
                                    paths["manifest"], manifest, plan, units, segment,
                                    status="validated",
                                )
                                worker_error = None
                            except Exception as recovery_error:
                                worker_error = recovery_error
                        elif isinstance(worker_error, CancelledError):
                            attempt["externalOutcome"] = "cancelled"
                            _checkpoint_segment(
                                paths["manifest"], manifest, plan, units, segment,
                                status="cancelled", error_stage="provider", error_summary=str(worker_error),
                            )
                        elif isinstance(worker_error, (RetryableProviderError, PermanentProviderError)):
                            attempt["externalOutcome"] = "failed"
                            _checkpoint_segment(
                                paths["manifest"], manifest, plan, units, segment,
                                status="failed", error_stage="provider", error_summary=str(worker_error),
                            )
                        elif isinstance(worker_error, AudioNormalizationError):
                            attempt["externalOutcome"] = "succeeded"
                            _checkpoint_segment(
                                paths["manifest"], manifest, plan, units, segment,
                                status="failed", error_stage="normalizing", error_summary=str(worker_error),
                            )
                        else:
                            attempt["externalOutcome"] = "unknown"
                            _checkpoint_segment(
                                paths["manifest"], manifest, plan, units, segment,
                                status="unknown_external_outcome", error_stage="provider-or-candidate",
                                error_summary="外部结果不确定；禁止自动重试",
                            )
                            worker_error = ApprovalGateError(
                                "provider 返回后未形成可采用 candidate；需人工决定"
                            )
                        if worker_error is None:
                            stop_dispatch = first_error is not None
                        if worker_error is not None and first_error is None:
                            first_error = worker_error
                while (
                    not stop_dispatch
                    and next_pending < len(pending)
                    and len(futures) < configured_concurrency
                ):
                    submit_one(executor)

    if first_error is not None:
        run["status"] = "cancelled" if isinstance(first_error, CancelledError) else "failed"
        run["finishedAt"] = _now()
        run["errorStage"] = "unit-generation"
        run["errorSummary"] = str(first_error)[:300]
        _write_manifest(paths["manifest"], manifest, plan, units)
        raise first_error

    try:
        composite = _merge_segments(project, manifest["segments"], run_dir)
    except (AudioNormalizationError, wave.Error, VoiceoverStateError, OSError) as exc:
        run["status"] = "failed"
        run["finishedAt"] = _now()
        run["errorStage"] = "composite-or-timeline"
        run["errorSummary"] = str(exc)[:300]
        _write_manifest(paths["manifest"], manifest, plan, units)
        raise
    source_duration = parse_srt(project.path("source/source.srt").read_text(encoding="utf-8-sig"))[-1]["endMs"]
    delta = composite.durationMs - source_duration
    ratio = abs(delta) / source_duration
    manifest["composite"] = {
        "status": "validated",
        "relativePath": "audio/narration.wav",
        **_media_dict(composite),
        "validatorReceipt": _canonical_validator_receipt(composite),
    }
    manifest["composite"].pop("file", None)
    manifest["fullAudioIdentityHash"] = _full_audio_identity(plan, manifest["composite"])
    manifest["timeline"] = {
        "status": "waiting_alignment",
        "relativePath": "audio/timeline.json",
    }
    manifest["narrationSrt"] = {
        "status": "waiting_alignment",
        "relativePath": "audio/narration.srt",
    }
    manifest["alignment"] = {
        "status": "waiting_alignment",
        "source": "external-asr-srt",
    }
    manifest["durationReview"] = {
        "sourceDurationMs": source_duration,
        "actualDurationMs": composite.durationMs,
        "deltaMs": delta,
        "ratio": ratio,
        "thresholdRatio": 0.10,
        "exceedsThreshold": ratio > 0.10,
    }
    manifest.pop("fullIdentityHash", None)
    manifest["fullApproval"] = {
        "approved": False,
        "identityHash": None,
        "durationDecision": None,
        "reviewPolicy": None,
        "approvalBasis": None,
        "reviewBasis": None,
        "approvedAt": None,
    }
    # Old projects may retain narration-review files and manifest fields as
    # historical evidence. New runs deliberately stop after publishing the
    # canonical WAV/timeline/SRT and never encode a pictureless review video.
    manifest.pop("review", None)
    run["status"] = "waiting_alignment"
    run["finishedAt"] = _now()
    _write_manifest(paths["manifest"], manifest, plan, units)
    return manifest["fullAudioIdentityHash"]


def _build_aligned_timeline(
    project: Project,
    plan: Mapping[str, Any],
    full_track_unit: Mapping[str, Any],
    composite: CanonicalAudioResult,
    alignment: Mapping[str, Any],
    *,
    asr_srt_sha256: str,
    alignment_source: str,
) -> tuple[dict[str, Any], str]:
    cues = alignment.get("cues")
    if not isinstance(cues, list) or not cues:
        raise VoiceoverStateError("reference alignment 未返回 cues")
    source_cues = parse_srt(project.path("source/source.srt").read_text(encoding="utf-8-sig"))
    scene_specs = project.timing_plan["scenes"]
    scene_by_ordinal = {
        ordinal: spec["sceneId"]
        for spec in scene_specs
        for ordinal in range(spec["sourceCueRange"][0], spec["sourceCueRange"][1] + 1)
    }
    expected_source_text = {
        cue["sourceOrdinal"]: re.sub(r"\s+", "", cue["text"])
        for cue in source_cues
    }
    observed_source_text: dict[int, str] = {ordinal: "" for ordinal in expected_source_text}
    timeline_units: list[dict[str, Any]] = []
    previous_end_ms = -1
    previous_ordinal = 0
    for expected_index, cue in enumerate(cues, start=1):
        if not isinstance(cue, Mapping) or cue.get("index") != expected_index:
            raise VoiceoverStateError("reference alignment cue index 必须从 1 起连续")
        ordinal = cue.get("sourceCueOrdinal")
        start_ms = cue.get("startMs")
        end_ms = cue.get("endMs")
        text = cue.get("text")
        scene_id = cue.get("sceneId")
        if (
            isinstance(ordinal, bool)
            or not isinstance(ordinal, int)
            or ordinal not in expected_source_text
            or ordinal < previous_ordinal
        ):
            raise VoiceoverStateError("reference alignment sourceOrdinal 无效或乱序")
        if (
            isinstance(start_ms, bool)
            or not isinstance(start_ms, int)
            or isinstance(end_ms, bool)
            or not isinstance(end_ms, int)
            or start_ms < 0
            or end_ms <= start_ms
            or end_ms > composite.durationMs
            or (previous_end_ms >= 0 and start_ms < previous_end_ms)
        ):
            raise VoiceoverStateError("reference alignment cue 必须非重叠、正时长且位于整轨范围内")
        if not isinstance(text, str) or not text.strip():
            raise VoiceoverStateError("reference alignment cue 文本不能为空")
        if cue.get("sourceCueRange") != [ordinal, ordinal]:
            raise VoiceoverStateError("reference alignment cue 不得跨 source cue")
        if scene_by_ordinal.get(ordinal) != scene_id:
            raise VoiceoverStateError("reference alignment cue 不得跨 scene 或改变 scene mapping")
        observed_source_text[ordinal] += re.sub(r"\s+", "", text)
        timeline_units.append(
            {
                "index": expected_index,
                "sceneId": scene_id,
                "sourceCueRange": [ordinal, ordinal],
                "sourceOrdinalRange": [ordinal, ordinal],
                "text": text,
                "startMs": start_ms,
                "endMs": end_ms,
                "durationMs": end_ms - start_ms,
                "segmentSha256": composite.sha256,
                "voiceSynthesisIdentityHash": full_track_unit["voiceSynthesisIdentityHash"],
            }
        )
        previous_end_ms = end_ms
        previous_ordinal = ordinal
    if observed_source_text != expected_source_text:
        raise VoiceoverStateError("reference alignment 文本未逐字覆盖已确认 source SRT")

    fps = int(project.render_profile["fps"])
    scenes: list[dict[str, Any]] = []
    frame_cursor = 0
    aligned_scenes = alignment.get("scenes")
    if not isinstance(aligned_scenes, list) or len(aligned_scenes) != len(scene_specs):
        raise VoiceoverStateError("reference alignment scenes 与已批准语义场景数量不一致")
    scene_cursor = 0
    for scene_index, (spec, aligned_scene) in enumerate(zip(scene_specs, aligned_scenes)):
        if not isinstance(aligned_scene, Mapping):
            raise VoiceoverStateError("reference alignment scene 结构无效")
        scene_units = [item for item in timeline_units if item["sceneId"] == spec["sceneId"]]
        if not scene_units:
            raise VoiceoverStateError(f"{spec['sceneId']} 未覆盖任何对齐 cue")
        end_ms = aligned_scene.get("endMs")
        last_narrated_end_ms = aligned_scene.get("lastNarratedTokenEndMs")
        next_narrated_start_ms = aligned_scene.get("nextNarratedTokenStartMs")
        available_pause_ms = aligned_scene.get("availablePauseMs")
        boundary_basis = aligned_scene.get("boundaryBasis")
        if (
            aligned_scene.get("sceneId") != spec["sceneId"]
            or aligned_scene.get("sourceCueRange") != spec["sourceCueRange"]
            or aligned_scene.get("startMs") != scene_cursor
            or isinstance(end_ms, bool)
            or not isinstance(end_ms, int)
            or end_ms <= scene_cursor
            or end_ms > composite.durationMs
            or scene_units[0]["startMs"] < scene_cursor
            or scene_units[-1]["endMs"] > end_ms
            or isinstance(last_narrated_end_ms, bool)
            or not isinstance(last_narrated_end_ms, int)
            or last_narrated_end_ms != scene_units[-1]["endMs"]
            or isinstance(available_pause_ms, bool)
            or not isinstance(available_pause_ms, int)
            or available_pause_ms < 0
            or not isinstance(boundary_basis, str)
            or not boundary_basis
        ):
            raise VoiceoverStateError(f"{spec['sceneId']} 对齐 scene 未连续覆盖真实音频时钟")
        if scene_index < len(scene_specs) - 1:
            if (
                isinstance(next_narrated_start_ms, bool)
                or not isinstance(next_narrated_start_ms, int)
                or next_narrated_start_ms < last_narrated_end_ms
                or available_pause_ms != next_narrated_start_ms - last_narrated_end_ms
                or not (last_narrated_end_ms <= end_ms <= next_narrated_start_ms)
            ):
                raise VoiceoverStateError(f"{spec['sceneId']} scene 边界未绑定真实尾音/停顿")
        elif next_narrated_start_ms is not None:
            raise VoiceoverStateError("最后一幕不得声明下一幕旁白起点")
        if scene_index == len(scene_specs) - 1 and end_ms != composite.durationMs:
            raise VoiceoverStateError("reference alignment 最后一幕未收口到整轨真实时长")
        end_frame = (end_ms * fps + 999) // 1000
        scenes.append(
            {
                "sceneId": spec["sceneId"],
                "sourceCueRange": list(spec["sourceCueRange"]),
                "unitRange": [scene_units[0]["index"], scene_units[-1]["index"]],
                "startMs": scene_cursor,
                "endMs": end_ms,
                "sceneDurationMs": end_ms - scene_cursor,
                "startFrame": frame_cursor,
                "endFrameExclusive": end_frame,
                "frameCount": end_frame - frame_cursor,
                "lastNarratedTokenEndMs": last_narrated_end_ms,
                "nextNarratedTokenStartMs": next_narrated_start_ms,
                "availablePauseMs": available_pause_ms,
                "boundaryBasis": boundary_basis,
            }
        )
        scene_cursor = end_ms
        frame_cursor = end_frame

    narration_srt = serialize_srt(
        [
            {
                "originalIndex": unit["index"],
                "startMs": unit["startMs"],
                "endMs": unit["endMs"],
                "text": unit["text"],
            }
            for unit in timeline_units
        ]
    )
    diagnostics = alignment.get("diagnostics")
    diagnostics_summary = {
        key: value
        for key, value in dict(diagnostics).items()
        if (
            key in {
                "matchRatio",
                "normalizedEditRatio",
                "asrCueCount",
                "outputCueCount",
                "maxBoundaryDisplacementTokens",
                "captionRateOutliers",
            }
            and isinstance(value, (int, float))
            and not isinstance(value, bool)
        )
    } if isinstance(diagnostics, Mapping) else {}
    if isinstance(diagnostics, Mapping):
        diagnostics_summary.update(
            {
                "tokenTimingUsed": diagnostics.get("tokenTimingUsed") is True,
                "qualityGatePassed": diagnostics.get("qualityGatePassed") is True,
                "captionSegmentationContract": diagnostics.get(
                    "captionSegmentationContract"
                ),
                "subtitleGaps": copy.deepcopy(diagnostics.get("subtitleGaps")),
                "localAcousticRate": copy.deepcopy(
                    diagnostics.get("localAcousticRate")
                ),
                "acousticEvidence": copy.deepcopy(
                    diagnostics.get("acousticEvidence")
                ),
            }
        )
    timeline = {
        "schemaVersion": VOICE_TIMELINE_SCHEMA_VERSION,
        "contractVersion": VOICE_TIMELINE_CONTRACT_VERSION,
        "projectId": project.project_id,
        "sourceSrt": {"file": "source/source.srt", "sha256": plan["source"]["sha256"]},
        "voicePlanAuditHash": voice_plan_audit_hash(plan),
        "audio": {
            "file": "audio/narration.wav",
            "sha256": composite.sha256,
            "durationMs": composite.durationMs,
            "bytes": composite.bytes,
            "contractVersion": composite.contractVersion,
        },
        "renderProfileSha256": sha256_json(project.render_profile),
        "alignment": {
            "contractVersion": f"reference-audio-alignment-schema-{alignment.get('schemaVersion', 1)}",
            "source": alignment_source,
            "asrSrtSha256": asr_srt_sha256,
            "diagnostics": diagnostics_summary,
        },
        "units": timeline_units,
        "scenes": scenes,
        "narrationSrt": {
            "file": "audio/narration.srt",
            "sha256": _sha256_text(narration_srt),
        },
    }
    return timeline, narration_srt


def _load_asr_acoustic_evidence(asr_srt_path: Path) -> dict[str, Any]:
    raw_json_path = asr_srt_path.with_name("transcript.raw.json")
    receipt_path = asr_srt_path.with_name("asr-receipt.json")
    if not raw_json_path.is_file() or not receipt_path.is_file():
        raise VoiceoverStateError(
            "ASR SRT 缺少同 attempt 的 transcript.raw.json/asr-receipt.json 分段证据"
        )
    raw_json = _read_json(raw_json_path, "ASR raw token evidence")
    receipt = _read_json(receipt_path, "ASR receipt")
    segment_evidence = raw_json.get("vadSegmentEvidence")
    receipt_evidence = receipt.get("segmentEvidence")
    timing = receipt.get("timingValidation")
    if (
        raw_json.get("contractVersion") != NARRATION_ASR_CONTRACT_VERSION
        or receipt.get("contractVersion") != NARRATION_ASR_CONTRACT_VERSION
        or not isinstance(segment_evidence, Mapping)
        or segment_evidence.get("validated") is not True
        or segment_evidence.get("contractVersion")
        != NARRATION_ASR_RECONSTRUCTION_CONTRACT
        or segment_evidence.get("maxVadSegmentMs")
        != NARRATION_ASR_MAX_VAD_SEGMENT_MS
        or segment_evidence.get("segmentationContract")
        != NARRATION_ASR_SEGMENTATION_CONTRACT
        or segment_evidence.get("reconstructionMatchesTopLevel") is not True
        or not isinstance(segment_evidence.get("segmentCount"), int)
        or segment_evidence.get("segmentCount", 0) <= 0
        or not isinstance(segment_evidence.get("tokenCount"), int)
        or segment_evidence.get("tokenCount", 0) <= 0
        or not isinstance(receipt_evidence, Mapping)
        or receipt_evidence.get("validated") is not True
        or receipt_evidence.get("contractVersion")
        != segment_evidence.get("contractVersion")
        or receipt_evidence.get("maxVadSegmentMs")
        != NARRATION_ASR_MAX_VAD_SEGMENT_MS
        or receipt_evidence.get("segmentationContract")
        != segment_evidence.get("segmentationContract")
        or receipt_evidence.get("reconstructedSequenceSha256")
        != segment_evidence.get("reconstructedSequenceSha256")
        or receipt_evidence.get("topLevelSequenceSha256")
        != segment_evidence.get("topLevelSequenceSha256")
        or not isinstance(timing, Mapping)
        or timing.get("evidenceKind") != "vad_segment_token_timestamp"
        or timing.get("segmentEvidenceValidated") is not True
    ):
        raise VoiceoverStateError("ASR 分段 token/timestamp 证据未通过 v5 fail-closed 合同")
    return {
        "asrContractVersion": NARRATION_ASR_CONTRACT_VERSION,
        "contractVersion": segment_evidence.get("contractVersion"),
        "validated": True,
        "evidenceKind": "vad_segment_token_timestamp",
        "maxVadSegmentMs": NARRATION_ASR_MAX_VAD_SEGMENT_MS,
        "segmentationContract": segment_evidence.get("segmentationContract"),
        "segmentCount": segment_evidence["segmentCount"],
        "tokenCount": segment_evidence["tokenCount"],
        "reconstructionMatchesTopLevel": True,
        "reconstructedSequenceSha256": segment_evidence.get(
            "reconstructedSequenceSha256"
        ),
        "topLevelSequenceSha256": segment_evidence.get("topLevelSequenceSha256"),
        "rawJsonSha256": sha256_file(raw_json_path),
        "receiptSha256": sha256_file(receipt_path),
    }


def _publish_alignment(
    project: Project,
    asr_srt_path: Path,
    *,
    alignment_source: str = "external-asr-srt",
) -> str:
    if align_reference_audio is None:
        raise VoiceoverStateError("reference audio alignment 模块尚未安装")
    plan, units = _load_current_plan_units(project)
    if (
        plan["segmentation"]["contractVersion"] != FULL_TRACK_SEGMENTATION_CONTRACT_VERSION
        or len(units) != 1
    ):
        raise VoiceoverStateError("publish-alignment 只支持 full-track voice plan")
    paths = _voice_paths(project)
    manifest = validate_voice_manifest(
        _read_json(paths["manifest"], "voice manifest"), voice_plan=plan, speech_units=units
    )
    current_sample = _validate_current_sample(project, plan, manifest)
    approval = manifest["sample"]["approval"]
    if not approval.get("approved") or approval.get("identityHash") != current_sample:
        raise ApprovalGateError("未获得 current 样音 voice/rate 人工批准")
    if len(manifest["segments"]) != 1 or not _segment_is_reusable(
        project, manifest["segments"][0], units[0]
    ):
        raise VoiceoverStateError("整轨 synthesis segment 尚未 validated")
    composite_ref = manifest.get("composite")
    if not isinstance(composite_ref, Mapping) or composite_ref.get("status") != "validated":
        raise VoiceoverStateError("audio/narration.wav 尚未 validated")
    composite = _validate_media_ref(project, composite_ref, expected_file="audio/narration.wav")
    expected_audio_identity = _full_audio_identity(plan, composite_ref)
    if manifest.get("fullAudioIdentityHash") != expected_audio_identity:
        raise ApprovalGateError("整轨音频技术 identity 已 stale")
    try:
        asr_srt = asr_srt_path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as exc:
        raise VoiceoverStateError(f"无法读取 ASR SRT: {exc}") from exc
    source_srt = project.path("source/source.srt").read_text(encoding="utf-8-sig")
    acoustic_evidence = _load_asr_acoustic_evidence(asr_srt_path)
    alignment = align_reference_audio(
        source_srt,
        asr_srt,
        project.timing_plan["scenes"],
        composite.durationMs,
    )
    if not isinstance(alignment, Mapping):
        raise VoiceoverStateError("reference audio alignment 返回值无效")
    diagnostics = alignment.get("diagnostics")
    if not isinstance(diagnostics, dict):
        raise VoiceoverStateError("reference audio alignment 缺少 diagnostics")
    diagnostics["acousticEvidence"] = acoustic_evidence
    asr_sha = hashlib.sha256(asr_srt.encode("utf-8")).hexdigest()
    timeline, narration_srt = _build_aligned_timeline(
        project,
        plan,
        units[0],
        composite,
        alignment,
        asr_srt_sha256=asr_sha,
        alignment_source=alignment_source,
    )
    _publish_text(paths["srt"], narration_srt)
    write_json_atomic(paths["timeline"], timeline)
    timeline_sha = sha256_file(paths["timeline"])
    narration_sha = sha256_file(paths["srt"])
    if timeline["narrationSrt"]["sha256"] != narration_sha:
        raise VoiceoverStateError("timeline narrationSrt linkage 与正式 SRT 不一致")
    manifest["timeline"] = {
        "status": "validated",
        "relativePath": "audio/timeline.json",
        "sha256": timeline_sha,
        "durationMs": composite.durationMs,
        "contractVersion": VOICE_TIMELINE_CONTRACT_VERSION,
    }
    manifest["narrationSrt"] = {
        "status": "validated",
        "relativePath": "audio/narration.srt",
        "sha256": narration_sha,
        "cueCount": len(timeline["units"]),
    }
    manifest["alignment"] = {
        "status": "validated",
        "source": alignment_source,
        "asrSrtSha256": asr_sha,
        "contractVersion": timeline["alignment"]["contractVersion"],
    }
    manifest["fullIdentityHash"] = _full_identity(
        plan, manifest["composite"], manifest["timeline"], manifest["narrationSrt"]
    )
    manifest["fullApproval"] = {
        "approved": False,
        "identityHash": None,
        "durationDecision": None,
        "reviewPolicy": None,
        "approvalBasis": None,
        "reviewBasis": None,
        "approvedAt": None,
    }
    manifest["runs"].append(
        {
            "kind": "publish-alignment",
            "status": "validated",
            "startedAt": _now(),
            "finishedAt": _now(),
            "taskCount": 1,
        }
    )
    _write_manifest(paths["manifest"], manifest, plan, units)
    return manifest["fullIdentityHash"]


def _run_local_asr(project: Project, narration_path: Path) -> Path:
    """调用当前 skill 内部 FunASR runner；证据只写入项目 .work。"""

    if transcribe_narration is None:
        raise VoiceoverStateError(
            "当前 skill 的 narration ASR runner 尚未安装；请先准备 narration-asr 环境"
        )
    output_dir = project.path(".work") / f"voice-align-{uuid.uuid4().hex}"
    if output_dir.exists():
        raise VoiceoverStateError("ASR 输出目录必须不存在")
    try:
        payload = transcribe_narration(narration_path.resolve(), output_dir.resolve())
    except Exception as exc:
        raise VoiceoverStateError(f"本地 narration ASR 失败: {str(exc)[-300:]}") from exc
    if not isinstance(payload, Mapping):
        raise VoiceoverStateError("本地 narration ASR 返回值必须是对象")
    if payload.get("ok") is not True:
        raise VoiceoverStateError("本地 narration ASR 未报告成功")
    raw_srt = payload.get("rawSrtPath")
    if not isinstance(raw_srt, str) or not raw_srt:
        raise VoiceoverStateError("本地 narration ASR 返回值缺少 rawSrtPath")
    raw_srt_path = Path(raw_srt).resolve()
    try:
        raw_srt_path.relative_to(output_dir.resolve())
    except ValueError as exc:
        raise VoiceoverStateError("本地 narration ASR rawSrtPath 越出本次输出目录") from exc
    if not raw_srt_path.is_file():
        raise VoiceoverStateError("本地 narration ASR rawSrtPath 不存在")
    duration_ms = payload.get("durationMs")
    sentence_count = payload.get("sentenceCount")
    token_count = payload.get("tokenCount")
    timing = payload.get("timingValidation")
    if (
        payload.get("contractVersion") != NARRATION_ASR_CONTRACT_VERSION
        or payload.get("evidenceKind") != "vad_segment_token_timestamp"
        or payload.get("segmentEvidenceValidated") is not True
        or isinstance(payload.get("segmentCount"), bool)
        or not isinstance(payload.get("segmentCount"), int)
        or payload.get("segmentCount", 0) <= 0
        or isinstance(duration_ms, bool)
        or not isinstance(duration_ms, int)
        or duration_ms <= 0
        or isinstance(sentence_count, bool)
        or not isinstance(sentence_count, int)
        or sentence_count <= 0
        or isinstance(token_count, bool)
        or not isinstance(token_count, int)
        or token_count != sentence_count
        or not isinstance(timing, Mapping)
        or timing.get("evidenceKind") != "vad_segment_token_timestamp"
        or timing.get("segmentEvidenceValidated") is not True
        or timing.get("reconstructionMatchesTopLevel") is not True
    ):
        raise VoiceoverStateError(
            "本地 narration ASR 摘要缺少有效的 v5 VAD 分段 token 时间证据"
        )
    return raw_srt_path


def validate_current_voiceover(
    project: Project,
    *,
    require_full: bool = True,
    voice_validation_concurrency: int = 1,
    force_deep: bool = False,
    persist_deep: bool = False,
) -> dict[str, Any]:
    """Strict read-only validation shared by validate_voiceover.py and approvals."""
    plan, units = _load_current_plan_units(project)
    paths = _voice_paths(project)
    manifest = validate_voice_manifest(
        _read_json(paths["manifest"], "voice manifest"), voice_plan=plan, speech_units=units
    )
    sample_identity = _validate_current_sample(project, plan, manifest)
    result: dict[str, Any] = {
        "voicePlanAuditHash": voice_plan_audit_hash(plan),
        "sampleIdentityHash": sample_identity,
        "sampleApproved": bool(manifest["sample"]["approval"]["approved"]),
        "sampleApprovalBasis": manifest["sample"]["approval"].get("approvalBasis"),
    }
    if not require_full:
        return result
    if (
        isinstance(voice_validation_concurrency, bool)
        or not isinstance(voice_validation_concurrency, int)
        or not 1 <= voice_validation_concurrency <= 16
    ):
        raise VoiceoverStateError("voiceValidation concurrency 必须位于 1–16")
    segment_pairs = list(zip(units, manifest["segments"]))
    receipts_before = [
        copy.deepcopy(segment.get("currentAttempt", {}).get("validatorReceipt"))
        if isinstance(segment.get("currentAttempt"), Mapping)
        else None
        for _unit, segment in segment_pairs
    ]
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=min(voice_validation_concurrency, len(segment_pairs)),
        thread_name_prefix="voice-validation",
    ) as executor:
        checks = [
            executor.submit(
                _segment_is_reusable,
                project,
                segment,
                unit,
                force_deep=force_deep,
                persist_deep=persist_deep,
            )
            for unit, segment in segment_pairs
        ]
        for (unit, _segment), check in zip(segment_pairs, checks):
            if not check.result():
                raise VoiceoverStateError(f"unit-{unit['index']:04d} 尚未 validated")
    receipts_changed = receipts_before != [
        segment.get("currentAttempt", {}).get("validatorReceipt")
        if isinstance(segment.get("currentAttempt"), Mapping)
        else None
        for _unit, segment in segment_pairs
    ]
    composite_ref = manifest["composite"]
    composite_path = project.path("audio/narration.wav")
    composite = None if force_deep else _canonical_result_from_binding(
        composite_path, composite_ref, composite_ref.get("validatorReceipt")
    )
    if composite is None:
        composite = _validate_media_ref(
            project, composite_ref, expected_file="audio/narration.wav"
        )
        if persist_deep:
            composite_ref["validatorReceipt"] = _canonical_validator_receipt(composite)
            receipts_changed = True
    timeline = _read_json(paths["timeline"], "audio timeline")
    if manifest["timeline"].get("sha256") != sha256_file(paths["timeline"]):
        raise ApprovalGateError("audio timeline SHA 已 stale")
    if timeline.get("audio", {}).get("sha256") != composite.sha256:
        raise ApprovalGateError("audio timeline 未绑定 current narration.wav")
    if timeline.get("voicePlanAuditHash") != voice_plan_audit_hash(plan):
        raise ApprovalGateError("audio timeline 未绑定 current voice plan")
    if timeline.get("narrationSrt", {}).get("sha256") != sha256_file(paths["srt"]):
        raise ApprovalGateError("audio timeline 未绑定 current narration.srt")
    if timeline.get("narrationSrt", {}).get("file") != "audio/narration.srt":
        raise VoiceoverStateError("audio timeline narrationSrt.file 无效")
    if (
        timeline.get("schemaVersion") != VOICE_TIMELINE_SCHEMA_VERSION
        or timeline.get("contractVersion") != VOICE_TIMELINE_CONTRACT_VERSION
    ):
        raise ApprovalGateError("audio timeline 合同版本已 stale，必须重新执行 token 对齐")
    alignment_diagnostics = timeline.get("alignment", {}).get("diagnostics", {})
    acoustic_evidence = (
        alignment_diagnostics.get("acousticEvidence")
        if isinstance(alignment_diagnostics, Mapping)
        else None
    )
    local_acoustic_rate = (
        alignment_diagnostics.get("localAcousticRate")
        if isinstance(alignment_diagnostics, Mapping)
        else None
    )
    if (
        not isinstance(alignment_diagnostics, Mapping)
        or alignment_diagnostics.get("tokenTimingUsed") is not True
        or alignment_diagnostics.get("qualityGatePassed") is not True
        or alignment_diagnostics.get("captionSegmentationContract")
        != "reference-punctuation-caption-v1"
        or not isinstance(acoustic_evidence, Mapping)
        or acoustic_evidence.get("validated") is not True
        or acoustic_evidence.get("asrContractVersion")
        != NARRATION_ASR_CONTRACT_VERSION
        or acoustic_evidence.get("contractVersion")
        != NARRATION_ASR_RECONSTRUCTION_CONTRACT
        or acoustic_evidence.get("maxVadSegmentMs")
        != NARRATION_ASR_MAX_VAD_SEGMENT_MS
        or acoustic_evidence.get("segmentationContract")
        != NARRATION_ASR_SEGMENTATION_CONTRACT
        or acoustic_evidence.get("evidenceKind")
        != "vad_segment_token_timestamp"
        or acoustic_evidence.get("reconstructionMatchesTopLevel") is not True
        or not isinstance(local_acoustic_rate, Mapping)
        or local_acoustic_rate.get("rateFloorPassed") is not True
        or local_acoustic_rate.get("rateCeilingPassed") is not True
        or local_acoustic_rate.get("rateVariationPassed") is not True
        or local_acoustic_rate.get("outlierCount") != 0
    ):
        raise VoiceoverStateError(
            "audio timeline 缺少 v5 VAD 分段 token 证据、双向局部漂移或语义安全切句 QA"
        )
    timeline_units = timeline.get("units")
    if not isinstance(timeline_units, list) or not timeline_units:
        raise VoiceoverStateError("audio timeline units 不能为空")
    previous = -1
    for unit in timeline_units:
        start_ms = unit.get("startMs")
        end_ms = unit.get("endMs")
        if (
            isinstance(start_ms, bool)
            or not isinstance(start_ms, int)
            or isinstance(end_ms, bool)
            or not isinstance(end_ms, int)
            or start_ms < 0
            or end_ms <= start_ms
            or end_ms > composite.durationMs
            or (previous >= 0 and start_ms < previous)
        ):
            raise VoiceoverStateError("audio timeline units 必须非重叠、正时长且位于整轨范围内")
        previous = end_ms
    narration_cues = parse_srt(paths["srt"].read_text(encoding="utf-8-sig"))
    if len(narration_cues) != len(timeline_units):
        raise VoiceoverStateError("narration SRT cue 数与 timeline units 不一致")
    for cue, unit in zip(narration_cues, timeline_units):
        if (
            cue["startMs"] != unit["startMs"]
            or cue["endMs"] != unit["endMs"]
            or cue["text"] != unit["text"]
        ):
            raise VoiceoverStateError("narration SRT 文本/时间与 canonical units 不一致")
    expected_scene_start_ms = 0
    expected_scene_start_frame = 0
    fps = int(project.render_profile["fps"])
    timeline_scenes = timeline.get("scenes")
    timing_scenes = project.timing_plan["scenes"]
    if not isinstance(timeline_scenes, list) or len(timeline_scenes) != len(timing_scenes):
        raise VoiceoverStateError("audio timeline scenes 与已批准语义场景数量不一致")
    for scene_index, (scene, spec) in enumerate(zip(timeline_scenes, timing_scenes)):
        end_ms = scene.get("endMs")
        scene_units = [unit for unit in timeline_units if unit.get("sceneId") == scene.get("sceneId")]
        last_narrated_end_ms = scene.get("lastNarratedTokenEndMs")
        next_narrated_start_ms = scene.get("nextNarratedTokenStartMs")
        available_pause_ms = scene.get("availablePauseMs")
        expected_end_frame = (end_ms * fps + 999) // 1000 if isinstance(end_ms, int) else -1
        if (
            scene.get("sceneId") != spec.get("sceneId")
            or scene.get("sourceCueRange") != spec.get("sourceCueRange")
            or scene.get("startMs") != expected_scene_start_ms
            or not isinstance(end_ms, int)
            or end_ms <= expected_scene_start_ms
            or scene.get("sceneDurationMs") != end_ms - expected_scene_start_ms
            or scene.get("startFrame") != expected_scene_start_frame
            or scene.get("endFrameExclusive") != expected_end_frame
            or scene.get("frameCount") != expected_end_frame - expected_scene_start_frame
            or not scene_units
            or last_narrated_end_ms != scene_units[-1]["endMs"]
            or isinstance(available_pause_ms, bool)
            or not isinstance(available_pause_ms, int)
            or available_pause_ms < 0
            or not isinstance(scene.get("boundaryBasis"), str)
            or not scene.get("boundaryBasis")
        ):
            raise VoiceoverStateError("audio timeline scene 未遵循连续全局时钟/累计帧边界")
        if scene_index < len(timeline_scenes) - 1:
            if (
                isinstance(next_narrated_start_ms, bool)
                or not isinstance(next_narrated_start_ms, int)
                or available_pause_ms != next_narrated_start_ms - last_narrated_end_ms
                or not (last_narrated_end_ms <= end_ms <= next_narrated_start_ms)
            ):
                raise VoiceoverStateError("audio timeline scene 边界早于本幕实际旁白结束")
        elif next_narrated_start_ms is not None or end_ms != composite.durationMs:
            raise VoiceoverStateError("audio timeline 最后一幕尾音边界无效")
        expected_scene_start_ms = end_ms
        expected_scene_start_frame = expected_end_frame
    if expected_scene_start_ms != composite.durationMs:
        raise VoiceoverStateError("audio timeline 最后一幕未收口到整轨实测时长")
    narration = manifest["narrationSrt"]
    if narration.get("sha256") != sha256_file(paths["srt"]):
        raise ApprovalGateError("narration SRT SHA 已 stale")
    expected_full = _full_identity(plan, manifest["composite"], manifest["timeline"], narration)
    if manifest.get("fullIdentityHash") != expected_full:
        raise ApprovalGateError("完整旁白 identity 已 stale")
    approval = manifest["fullApproval"]
    if approval.get("approved") and approval.get("identityHash") != expected_full:
        raise ApprovalGateError("完整旁白批准未绑定 current full identity")
    result.update(
        {
            "fullIdentityHash": expected_full,
            "fullApproved": bool(manifest["fullApproval"]["approved"]),
            "reviewPolicy": manifest["fullApproval"].get("reviewPolicy"),
            "durationReview": copy.deepcopy(manifest.get("durationReview")),
            "timelineSha256": manifest["timeline"]["sha256"],
            "audioSha256": composite.sha256,
            "narrationSrtSha256": narration["sha256"],
        }
    )
    if persist_deep and receipts_changed:
        _write_manifest(paths["manifest"], manifest, plan, units)
    return result


def _approve_full(
    project: Project,
    identity_hash: str,
    duration_decision: str | None,
    review_policy: str,
) -> str:
    current = validate_current_voiceover(project, require_full=True)
    if identity_hash != current["fullIdentityHash"]:
        raise ApprovalGateError("提交的完整旁白 identity 与 current WAV/timeline/SRT 不一致")
    autonomous = _technical_audio_progress_authorized(project, current)
    review = current.get("durationReview")
    if not isinstance(review, Mapping):
        raise VoiceoverStateError("完整旁白缺少 duration review")
    if review.get("exceedsThreshold"):
        if autonomous:
            recorded_decision = "accept_actual"
        elif duration_decision != "accept_actual":
            raise ApprovalGateError("真实时长偏差超过 10%，必须显式 --duration-decision accept_actual")
        else:
            recorded_decision = "accept_actual"
    else:
        if duration_decision is not None:
            raise VoiceoverStateError("时长偏差在阈值内时不得伪装为 accept_actual")
        recorded_decision = "within_threshold"

    plan, units = _load_current_plan_units(project)
    paths = _voice_paths(project)
    manifest = validate_voice_manifest(
        _read_json(paths["manifest"], "voice manifest"), voice_plan=plan, speech_units=units
    )
    timeline = _read_json(paths["timeline"], "audio timeline")
    timeline_sha = sha256_file(paths["timeline"])
    timing_plan = {
        "schemaVersion": 1,
        "projectId": project.project_id,
        "voiceoverMode": project.voiceover_mode,
        "sourceSrtSha256": project.metadata["source"]["sha256"],
        "renderProfileSha256": sha256_json(project.render_profile),
        "activeTimeline": {
            "kind": "edge-tts-audio-timeline" if project.voiceover_mode == "edge-tts" else "audio-authoritative-timeline",
            "file": "audio/timeline.json",
            "sha256": timeline_sha,
        },
        "scenes": [
            {key: scene[key] for key in (
                "sceneId", "sourceCueRange", "startMs", "endMs", "sceneDurationMs",
                "startFrame", "endFrameExclusive", "frameCount",
            )}
            for scene in timeline["scenes"]
        ],
    }
    validate_timing_plan_data(
        timing_plan,
        project_id=project.project_id,
        voiceover_mode=project.voiceover_mode,
        source_srt_sha256=project.metadata["source"]["sha256"],
        render_profile=project.render_profile,
        generation_scenes=project.plan["scenes"],
    )
    generation_hash = sha256_file(project.plan_path)
    manifest["fullApproval"] = {
        "approved": True,
        "identityHash": identity_hash,
        "durationDecision": recorded_decision,
        "reviewPolicy": review_policy,
        "approvalBasis": (
            "technical_after_user_sample" if autonomous else "human_full_listening"
        ),
        "reviewBasis": (
            "user_joint_initial_sample_authorization_and_current_technical_validation"
            if autonomous
            else "current_full_audio_listening"
        ),
        "approvedAt": _now(),
    }
    # 两个候选先完成 schema/current 校验，再进入短事务。发布任一步失败时按原
    # 字节恢复两个正式文件，使项目回到可由 pending-audio-timeline 模式继续的
    # 待批准状态；错误 identity 在进入此处前已经拒绝，两个文件均不会变化。
    validate_voice_manifest(manifest, voice_plan=plan, speech_units=units)
    timing_before = project.timing_plan_path.read_bytes()
    manifest_before = paths["manifest"].read_bytes()
    try:
        write_json_atomic(project.timing_plan_path, timing_plan)
        _write_manifest(paths["manifest"], manifest, plan, units)
        committed = load_project(project.root)
        committed_manifest = validate_voice_manifest(
            _read_json(paths["manifest"], "voice manifest"),
            voice_plan=plan,
            speech_units=units,
        )
        if (
            committed.pending_audio_timeline
            or committed.timing_plan["activeTimeline"]["sha256"] != timeline_sha
            or committed_manifest["fullApproval"].get("identityHash") != identity_hash
        ):
            raise VoiceoverStateError("完整旁白批准事务提交后 current binding 复核失败")
        if sha256_file(project.plan_path) != generation_hash:
            raise VoiceoverStateError("批准真实时长不得改写 generation plan")
    except Exception:
        rollback_errors: list[str] = []
        for target, payload in (
            (project.timing_plan_path, timing_before),
            (paths["manifest"], manifest_before),
        ):
            try:
                _restore_bytes_atomic(target, payload)
            except OSError as rollback_exc:
                rollback_errors.append(f"{target.name}: {rollback_exc}")
        if rollback_errors:
            raise VoiceoverStateError(
                "完整旁白批准失败且回滚不完整: " + "; ".join(rollback_errors)
            )
        raise
    return identity_hash


def _resolve_full_approval_review_policy(
    project: Project,
    requested: str | None,
) -> str:
    if project.agent_approval_enabled:
        if requested == "user_first":
            raise ProjectValidationError(
                "agentApprovalEnabled=true 与 reviewPolicy=user_first 冲突"
            )
        return "agent_first"
    if requested is None:
        raise ProjectValidationError(
            "人工批准模式必须显式提供 --review-policy user_first|agent_first"
        )
    return requested


def _technical_audio_progress_authorized(
    project: Project,
    current: Mapping[str, Any],
) -> bool:
    """Validate the user-sample authorization required for audio-only autonomy."""

    if not (project.initial_approval_completed and project.agent_approval_enabled):
        return False
    initial = project.metadata.get("initialApproval")
    # Missing initialApproval is a legacy-formal-project compatibility view,
    # not evidence that the user granted the new sample-based autonomy policy.
    if initial is None:
        return False
    if not isinstance(initial, Mapping) or initial.get("status") != "approved":
        raise ApprovalGateError("技术自主推进要求可审计的 current 初始联合批准")
    sample_identity = current.get("sampleIdentityHash")
    if (
        current.get("sampleApproved") is not True
        or current.get("sampleApprovalBasis") != "user_joint_initial_approval"
        or initial.get("sampleIdentityHash") != sample_identity
    ):
        raise ApprovalGateError("技术自主推进要求用户联合批准的 current 样音 identity")
    return True


def _status(project: Project) -> dict[str, Any]:
    paths = _voice_paths(project)
    payload: dict[str, Any] = {
        "projectId": project.project_id,
        "voiceoverMode": project.voiceover_mode,
        "voicePlan": "missing",
        "sample": "missing",
        "segments": {},
        "full": "missing",
        "pendingAudioTimeline": project.pending_audio_timeline,
    }
    if not paths["manifest"].is_file():
        return payload
    manifest = _read_json(paths["manifest"], "voice manifest")
    payload["voicePlan"] = manifest.get("voicePlan", {}).get("voicePlanAuditHash", "invalid")
    payload["sample"] = {
        "status": manifest.get("sample", {}).get("status"),
        "approved": manifest.get("sample", {}).get("approval", {}).get("approved", False),
        "identityHash": manifest.get("sample", {}).get("identityHash"),
    }
    counts: dict[str, int] = {}
    for segment in manifest.get("segments", []):
        status = str(segment.get("status", "invalid"))
        counts[status] = counts.get(status, 0) + 1
    payload["segments"] = counts
    payload["full"] = {
        "audioIdentityHash": manifest.get("fullAudioIdentityHash"),
        "alignmentStatus": manifest.get("alignment", {}).get("status"),
        "identityHash": manifest.get("fullIdentityHash"),
        "approved": manifest.get("fullApproval", {}).get("approved", False),
        "durationDecision": manifest.get("fullApproval", {}).get("durationDecision"),
        "reviewPolicy": manifest.get("fullApproval", {}).get("reviewPolicy"),
        "approvalBasis": manifest.get("fullApproval", {}).get("approvalBasis"),
        "reviewBasis": manifest.get("fullApproval", {}).get("reviewBasis"),
    }
    return payload


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="生成、批准和检查语音旁白")
    sub = parser.add_subparsers(dest="command", required=True)
    sample = sub.add_parser("sample", help="生成 canonical 样音，但不自动批准")
    sample.add_argument("--project", required=True, type=Path)
    sample.add_argument("--voice")
    sample.add_argument("--rate", type=int)
    approve_sample = sub.add_parser("approve-sample", help="持久化用户已试听的 current 样音批准")
    approve_sample.add_argument("--project", required=True, type=Path)
    approve_sample.add_argument("--identity-hash", required=True)
    full = sub.add_parser("full", help="生成/恢复整篇单次请求的 canonical 旁白音频")
    full.add_argument("--project", required=True, type=Path)
    full.add_argument("--retry-failed", action="store_true")
    publish_alignment = sub.add_parser(
        "publish-alignment", help="用外部 ASR SRT 对齐整轨音频并发布 timeline/FULL_IDENTITY"
    )
    publish_alignment.add_argument("--project", required=True, type=Path)
    publish_alignment.add_argument("--asr-srt", required=True, type=Path)
    approve_full = sub.add_parser(
        "approve-full",
        help="持久化完整旁白批准；自主模式基于用户样音授权和 current 技术证据推进",
    )
    approve_full.add_argument("--project", required=True, type=Path)
    approve_full.add_argument("--identity-hash", required=True)
    approve_full.add_argument("--duration-decision", choices=("accept_actual",))
    approve_full.add_argument(
        "--review-policy",
        choices=("user_first", "agent_first"),
        help=(
            "冻结后续图片、annotation 和 scene bundle 的检查顺序；"
            "agentApprovalEnabled=true 时可省略并确定性采用 agent_first"
        ),
    )
    status = sub.add_parser("status", help="只读输出旁白状态")
    status.add_argument("--project", required=True, type=Path)
    return parser


def main(
    argv: list[str] | None = None,
    *,
    adapter: ProviderAdapter | None = None,
    normalizer: Callable[..., CanonicalAudioResult] | None = None,
    workspace_config: WorkspaceConfig | None = None,
    asr_runner: Callable[[Project, Path], Path] | None = None,
    asr_preflight: Callable[[], None] | None = None,
) -> int:
    try:
        from .cli_runtime import configure_utf8_stdio
    except ImportError:  # pragma: no cover - direct script execution
        from cli_runtime import configure_utf8_stdio  # type: ignore
    configure_utf8_stdio()
    args = _parser().parse_args(argv)
    try:
        project = load_project(
            args.project,
            allow_pending_audio_timeline=args.command
            in {"publish-alignment", "approve-full", "status"},
            allow_pending_initial_approval=args.command in {"sample", "status"},
        )
        execution = workspace_config
        if execution is None and argv is None:
            execution = load_workspace_config()
        concurrency = execution.concurrency if execution is not None else ExecutionConcurrency()
        if args.command == "sample":
            # Provider selection has one source of truth: activeProvider.
            provider_id = active_provider_id()
            if provider_id == "disabled":
                raise VoiceoverStateError("disabled 项目不能生成旁白样音")
            provider_config = load_voice_provider_config(provider_id=provider_id)
            voice = args.voice or str(provider_config.get("voice", "zh-CN-YunjianNeural"))
            rate = args.rate if args.rate is not None else provider_config.get("rate", 0)
            if provider_id != project.voiceover_mode:
                raise VoiceoverStateError(
                    "activeProvider 必须与项目 voiceoverMode 一致；请使用匹配的项目"
                )
            audio, identity, review_audio, request_audit, audio_sha256 = _sample(
                project, voice=voice, rate=rate, provider_id=provider_id,
                provider_config=provider_config,
                adapter=adapter or _adapter_from_plan(_build_plan_and_units(
                    project, voice=voice, rate=rate, provider_id=provider_id,
                    provider_config=provider_config,
                )[0]),
                normalizer=normalizer or normalize_and_publish,
            )
            print(f"SAMPLE_AUDIO={audio}")
            print(f"SAMPLE_REVIEW_AUDIO={review_audio}")
            print(f"SAMPLE_REQUEST_AUDIT={request_audit}")
            print(f"SAMPLE_VOICE_ID={voice}")
            print(f"SAMPLE_AUDIO_SHA256={audio_sha256}")
            print(f"SAMPLE_IDENTITY={identity}")
            estimate = _read_json(
                project.path("manifests/voice-manifest.json"), "voice manifest"
            )["sample"]["durationEstimate"]
            print(f"ESTIMATED_FULL_DURATION_MS={estimate['estimatedFullDurationMs']}")
            print(f"TARGET_DURATION_MS={estimate['targetDurationMs']}")
            print(f"ESTIMATED_DEVIATION_RATIO={estimate['estimatedDeviationRatio']}")
            print("ESTIMATE_BASIS=sample_weight_ratio_v1")
            print("ESTIMATE_AUTHORITATIVE=0")
        elif args.command == "approve-sample":
            identity = _approve_sample(project, args.identity_hash)
            print(f"SAMPLE_APPROVED_IDENTITY={identity}")
        elif args.command == "full":
            current_plan, _ = _load_current_plan_units(project)
            audio_identity = _full(
                project, retry_failed=args.retry_failed,
                adapter=adapter or _adapter_from_plan(current_plan),
                normalizer=normalizer or normalize_to_candidate,
                configured_concurrency=concurrency.for_stage("voiceGeneration"),
                asr_preflight=asr_preflight or _preflight_narration_asr,
            )
            print(f"FULL_AUDIO={project.path('audio/narration.wav')}")
            print(f"FULL_AUDIO_IDENTITY={audio_identity}")
            runner = asr_runner or (_run_local_asr if argv is None else None)
            if runner is None:
                print("ALIGNMENT_REQUIRED=1")
            else:
                asr_srt_path = runner(project, project.path("audio/narration.wav"))
                identity = _publish_alignment(
                    project,
                    asr_srt_path,
                    alignment_source="internal-funasr",
                )
                print(f"ASR_SRT={asr_srt_path}")
                print(f"NARRATION_SRT={project.path('audio/narration.srt')}")
                print(f"AUDIO_TIMELINE={project.path('audio/timeline.json')}")
                print(f"FULL_IDENTITY={identity}")
        elif args.command == "publish-alignment":
            identity = _publish_alignment(project, args.asr_srt)
            print(f"NARRATION_SRT={project.path('audio/narration.srt')}")
            print(f"AUDIO_TIMELINE={project.path('audio/timeline.json')}")
            print(f"FULL_IDENTITY={identity}")
        elif args.command == "approve-full":
            review_policy = _resolve_full_approval_review_policy(
                project, args.review_policy
            )
            identity = _approve_full(
                project,
                args.identity_hash,
                args.duration_decision,
                review_policy,
            )
            print(f"FULL_APPROVED_IDENTITY={identity}")
            print(f"REVIEW_POLICY={review_policy}")
            print(f"TIMING_PLAN={project.timing_plan_path}")
        else:
            print(json.dumps(_status(project), ensure_ascii=False, sort_keys=True))
        return 0
    except ApprovalGateError as exc:
        print(f"[stale] {exc}", file=sys.stderr)
        return 5
    except RetryableProviderError as exc:
        print(f"[provider] {exc}", file=sys.stderr)
        return 3
    except CancelledError as exc:
        print(f"[cancelled] {exc}", file=sys.stderr)
        return 1
    except PermanentProviderError as exc:
        print(f"[config] {exc}", file=sys.stderr)
        return 2
    except (AudioNormalizationError, wave.Error) as exc:
        print(f"[media] {exc}", file=sys.stderr)
        return 4
    except (
        VoiceoverStateError,
        VoiceoverValidationError,
        VoiceProviderConfigError,
        ProjectValidationError,
        ValueError,
        OSError,
        json.JSONDecodeError,
    ) as exc:
        print(f"[err] {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
