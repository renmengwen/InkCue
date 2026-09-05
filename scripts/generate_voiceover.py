#!/usr/bin/env python3
"""语音旁白完整生成、恢复与批准 CLI。"""
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
    from .voiceover import (
        CancelledError,
        DOUBAO_PROMPT_VOICE_ID,
        FULL_TRACK_SEGMENTATION,
        FULL_TRACK_SEGMENTATION_MODE,
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
    from .edge_tts_adapter import EDGE_TTS_PACKAGE_VERSION, EdgeTtsAdapter
    from .doubao_adapter import (
        DoubaoAdapter,
        DOUBAO_ENDPOINT,
        DOUBAO_SUBTITLE_KIND,
        DOUBAO_SUBTITLE_SCHEMA_VERSION,
        DOUBAO_SUBTITLE_TYPE,
        DOUBAO_TIMESTAMP_TOLERANCE_MS,
    )
    from .doubao_prompt import (
        DOUBAO_MAX_AUDIO_DURATION_SECONDS,
        DoubaoPromptError,
        build_doubao_prompt_spec,
        render_doubao_text_prompt,
        text_prompt_sha256,
        validate_doubao_prompt_spec,
    )
    from .minimax_adapter import (
        MiniMaxAdapter,
        MINIMAX_ENDPOINT,
        MINIMAX_SUBTITLE_TYPE,
    )
    from .voice_provider_config import VoiceProviderConfigError, active_provider_id, load_voice_provider_config
    from . import validation_receipts
except ImportError:  # pragma: no cover - direct script execution
    from audio_normalization import (
        AudioNormalizationError,
        AudioValidationError,
        CanonicalAudioResult,
        atomic_publish_wav,
        normalize_to_candidate,
        validate_canonical_wav,
    )
    from edge_tts_adapter import EDGE_TTS_PACKAGE_VERSION, EdgeTtsAdapter
    from doubao_adapter import (
        DoubaoAdapter,
        DOUBAO_ENDPOINT,
        DOUBAO_SUBTITLE_KIND,
        DOUBAO_SUBTITLE_SCHEMA_VERSION,
        DOUBAO_SUBTITLE_TYPE,
        DOUBAO_TIMESTAMP_TOLERANCE_MS,
    )
    from doubao_prompt import (
        DOUBAO_MAX_AUDIO_DURATION_SECONDS,
        DoubaoPromptError,
        build_doubao_prompt_spec,
        render_doubao_text_prompt,
        text_prompt_sha256,
        validate_doubao_prompt_spec,
    )
    from minimax_adapter import (
        MiniMaxAdapter,
        MINIMAX_ENDPOINT,
        MINIMAX_SUBTITLE_TYPE,
    )
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
    from voiceover import (
        CancelledError,
        DOUBAO_PROMPT_VOICE_ID,
        FULL_TRACK_SEGMENTATION,
        FULL_TRACK_SEGMENTATION_MODE,
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
        ASR_PIPELINE_RECIPE as NARRATION_ASR_PIPELINE_RECIPE,
        MAX_VAD_SEGMENT_MS as NARRATION_ASR_MAX_VAD_SEGMENT_MS,
        SEGMENT_RECONSTRUCTION_RECIPE as NARRATION_ASR_RECONSTRUCTION_RECIPE,
        transcribe_narration,
    )
except ImportError:  # pragma: no cover - direct script execution / staged integration
    try:
        from transcribe_narration import (  # type: ignore[no-redef]
            ASR_PIPELINE_RECIPE as NARRATION_ASR_PIPELINE_RECIPE,
            MAX_VAD_SEGMENT_MS as NARRATION_ASR_MAX_VAD_SEGMENT_MS,
            SEGMENT_RECONSTRUCTION_RECIPE as NARRATION_ASR_RECONSTRUCTION_RECIPE,
            transcribe_narration,
        )
    except ImportError:  # pragma: no cover - fail closed until the companion module is installed
        NARRATION_ASR_MAX_VAD_SEGMENT_MS = 15_000
        NARRATION_ASR_PIPELINE_RECIPE = {
            "algorithm": "funasr_vad_token_timestamps",
            "version": 5,
            "parameters": {
                "sampleRate": 16_000,
                "channels": 1,
                "maxVadSegmentMs": 15_000,
                "sentenceTimestamp": True,
                "predTimestamp": True,
            },
        }
        NARRATION_ASR_RECONSTRUCTION_RECIPE = {
            "algorithm": "funasr_vad_segment_token_reconstruction",
            "version": 2,
            "parameters": {
                "segmentation": "paraformer_vad",
                "segmentationVersion": 1,
                "maxVadSegmentMs": 15_000,
            },
        }
        transcribe_narration = None  # type: ignore[assignment]


VOICE_TIMELINE_SCHEMA_VERSION = 3
VOICE_IDENTITY_SCHEMA_VERSION = 1
DEFAULT_REQUEST_TIMEOUT_SECONDS = 60.0
CANONICAL_WAV_VALIDATOR_ID = "canonical_wav"
CANONICAL_WAV_VALIDATOR_VERSION = 2
NATIVE_SUBTITLE_EVIDENCE_SCHEMA_VERSION = 1
NATIVE_SUBTITLE_EVIDENCE_KIND = "providerNativeWordSubtitleEvidence"


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
        "composite": project.path("audio/narration.wav"),
        "timeline": project.path("audio/timeline.json"),
        "srt": project.path("audio/narration.srt"),
        "minimax_subtitles": project.path("audio/minimax-subtitles.json"),
        "doubao_subtitles": project.path("audio/doubao-subtitles.json"),
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
    doubao_performance_brief: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    cues, scenes = _load_source_context(project)
    config = dict(provider_config or {})
    protocol = {
        "edge-tts": "edge-tts",
        "minimax": "MiniMax",
        "doubao": "Doubao",
    }.get(provider_id, provider_id)
    provider_options = {
        key: config[key]
        for key in (
            "model",
            "packageVersion",
            "emotion",
            "textNormalization",
            "stream",
            "endpoint",
            "requestTimeoutSeconds",
        )
        if key in config
    }
    if provider_id == "edge-tts":
        provider_options.setdefault("packageVersion", EDGE_TTS_PACKAGE_VERSION)
    elif provider_id == "minimax":
        provider_options.setdefault("model", "speech-2.8-hd")
        provider_options.setdefault("endpoint", MINIMAX_ENDPOINT)
    if provider_id == "doubao":
        provider_options.setdefault("model", "seed-audio-1.0")
        provider_options.setdefault("endpoint", DOUBAO_ENDPOINT)
        provider_options["promptSpec"] = build_doubao_prompt_spec(
            cues,
            scenes,
            performance_brief=doubao_performance_brief,
        )
        provider_options["maxTextPromptCharacters"] = 3000
        provider_options["maxAudioDurationSeconds"] = DOUBAO_MAX_AUDIO_DURATION_SECONDS
        provider_options["nativeWordSubtitlesRequired"] = True
        provider_options["voiceControlMode"] = "text_prompt"
        provider_options["timeControlMode"] = "scene_windows"
        voice = DOUBAO_PROMPT_VOICE_ID
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
        provider_options=provider_options,
        segmentation=FULL_TRACK_SEGMENTATION,
    )
    units = _bind_current_synthesis_units(
        project,
        plan,
        plan_full_track_unit(cues, scenes, segmentation=plan["segmentation"]),
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
        provider_options=plan["provider"].get("options", {}),
        source_file=plan["source"]["file"],
        segmentation=plan["segmentation"],
    )
    if expected != plan:
        raise ApprovalGateError("voice plan identities 已 stale")
    planner = (
        plan_full_track_unit
        if plan["segmentation"]["mode"] == FULL_TRACK_SEGMENTATION_MODE
        else plan_speech_units
    )
    units = _bind_current_synthesis_units(
        project, plan, planner(cues, scenes, segmentation=plan["segmentation"])
    )
    return plan, units


def _prepare_full_plan(
    project: Project,
    *,
    voice: str | None,
    rate: int | None,
    doubao_performance_brief: Path | None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """在首次完整旁白请求前冻结 voice plan；已有 plan 只做 current 重验。"""

    paths = _voice_paths(project)
    plan_exists = paths["plan"].is_file()
    manifest_exists = paths["manifest"].is_file()
    if plan_exists != manifest_exists:
        raise VoiceoverStateError("voice plan/manifest 只存在其一，不能开始完整旁白")
    if plan_exists:
        if voice is not None or rate is not None or doubao_performance_brief is not None:
            raise VoiceoverStateError("current voice plan 已冻结；不得在 full 命令中覆盖 voice/rate/brief")
        return _load_current_plan_units(project)

    provider_id = active_provider_id()
    if provider_id != project.voiceover_mode:
        raise VoiceoverStateError(
            "activeProvider 必须与项目 voiceoverMode 一致；请使用匹配的项目"
        )
    provider_config = load_voice_provider_config(provider_id=provider_id)
    if provider_id == "doubao":
        if voice is not None:
            raise VoiceoverStateError(
                "豆包 prompt-only 模式禁止 --voice；音色只由 text_prompt 定义"
            )
        if doubao_performance_brief is None:
            raise VoiceoverStateError(
                "豆包完整旁白首次生成需要 --doubao-performance-brief；"
                "请由 coordinator 参考 current Seed Audio 示例生成"
            )
        selected_voice = DOUBAO_PROMPT_VOICE_ID
        brief = _read_json(doubao_performance_brief, "doubao performance brief")
    else:
        if doubao_performance_brief is not None:
            raise VoiceoverStateError(
                "--doubao-performance-brief 只允许用于豆包项目"
            )
        selected_voice = voice or str(
            provider_config.get("voice", "zh-CN-YunjianNeural")
        )
        brief = None
    selected_rate = rate if rate is not None else provider_config.get("rate", 0)
    plan, units = _build_plan_and_units(
        project,
        voice=selected_voice,
        rate=selected_rate,
        provider_id=provider_id,
        provider_config=provider_config,
        doubao_performance_brief=brief,
    )
    manifest = create_voice_manifest(
        project_id=project.project_id,
        voice_plan=plan,
        speech_units=units,
    )
    write_json_atomic(paths["plan"], plan)
    _write_manifest(paths["manifest"], manifest, plan, units)
    return _load_current_plan_units(project)


def _doubao_text_prompt(
    plan: Mapping[str, Any],
    speech_text: str,
    *,
    background_music_enabled: bool,
    target_duration_seconds: float | None,
) -> str:
    options = plan["provider"].get("options", {})
    prompt_spec = options.get("promptSpec") if isinstance(options, Mapping) else None
    if not isinstance(prompt_spec, Mapping):
        raise VoiceoverStateError("豆包 current voice plan 缺少导演式 promptSpec")
    try:
        return render_doubao_text_prompt(
            prompt_spec,
            speech_text,
            background_music_enabled=background_music_enabled,
            target_duration_seconds=target_duration_seconds,
        )
    except DoubaoPromptError as exc:
        raise VoiceoverStateError(str(exc)) from exc


def _bind_current_synthesis_units(
    project: Project,
    plan: Mapping[str, Any],
    units: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    prepared = [copy.deepcopy(dict(unit)) for unit in units]
    if plan["provider"]["id"] == "doubao":
        source_cues = parse_srt(
            project.path("source/source.srt").read_text(encoding="utf-8-sig")
        )
        try:
            current_prompt_spec = validate_doubao_prompt_spec(
                plan["provider"].get("options", {}).get("promptSpec", {}),
                source_cues,
                project.timing_plan["scenes"],
            )
        except DoubaoPromptError as exc:
            raise ApprovalGateError(str(exc)) from exc
        if plan["provider"].get("options", {}).get("promptSpec") != current_prompt_spec:
            raise ApprovalGateError("豆包导演式 promptSpec 与 current source/scenes 不一致")
        duration_seconds = source_cues[-1]["endMs"] / 1000.0
        if duration_seconds > DOUBAO_MAX_AUDIO_DURATION_SECONDS:
            raise VoiceoverStateError(
                "豆包完整旁白目标时长超过 120 秒；禁止请求、拆句或自动切换 provider"
            )
        for unit in prepared:
            prompt = _doubao_text_prompt(
                plan,
                str(unit["speechText"]),
                background_music_enabled=project.background_music_enabled,
                target_duration_seconds=duration_seconds,
            )
            unit["providerTextPromptSha256"] = text_prompt_sha256(prompt)
            unit["providerTextPromptCharacterCount"] = len(prompt)
            unit["_providerTextPrompt"] = prompt
    return bind_synthesis_identities(prepared, plan)


def _request(
    plan: Mapping[str, Any], text: str, *, provider_text_prompt: str | None = None
) -> SynthesisRequest:
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
        text=provider_text_prompt if provider_text_prompt is not None else text,
        voice=settings["voice"],
        normalizedRate=settings["normalizedRate"],
        normalizedPitch=settings["normalizedPitch"],
        normalizedVolume=settings["normalizedVolume"],
        timeoutSeconds=float(timeout_seconds),
        cancellationToken=None,
    )


def _adapter_from_plan(
    plan: Mapping[str, Any],
    *,
    native_minimax_subtitles: bool = False,
    single_doubao_attempt: bool = False,
) -> ProviderAdapter:
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
            native_word_subtitles=native_minimax_subtitles,
        )
    if provider_id == "doubao":
        config = load_voice_provider_config(provider_id="doubao")
        options = plan["provider"].get("options", {})
        return DoubaoAdapter(
            api_key=str(config["apiKey"]),
            model=str(options.get("model", config.get("model", "seed-audio-1.0"))),
            endpoint=str(options.get("endpoint", config.get("endpoint", DOUBAO_ENDPOINT))),
            max_attempts=(
                1
                if single_doubao_attempt
                else int(config.get("maxRetries", 2)) + 1
            ),
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
        "recipe": copy.deepcopy(dict(result.recipe)),
    }


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
            "status", "audioMime", "audioCodec", "recipe", "sampleRate", "channels",
            "bytes", "durationMs", "sha256", "attempts", "createdAt", "updatedAt",
            "errorStage", "errorSummary", "currentAttempt", "providerSubtitles",
        ):
            segment[field] = copy.deepcopy(prior.get(field))
    return manifest


def _canonical_validator_receipt(result: CanonicalAudioResult) -> dict[str, Any]:
    return validation_receipts.build_candidate_receipt(
        candidate_sha256=result.sha256,
        candidate_bytes=result.bytes,
        decoded=True,
        format="WAV",
        validator_id=CANONICAL_WAV_VALIDATOR_ID,
        validator_version=CANONICAL_WAV_VALIDATOR_VERSION,
        evidence={
            "mediaRecipe": copy.deepcopy(dict(result.recipe)),
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
    receipt_validator = candidate_receipt.get("validator")
    if not isinstance(receipt_validator, Mapping):
        raise ApprovalGateError("WAV candidate receipt validator 无效")
    validator_id = receipt_validator.get("id")
    validator_version = receipt_validator.get("version")
    if not isinstance(validator_id, str) or not isinstance(validator_version, int):
        raise ApprovalGateError("WAV candidate receipt validator 无效")
    try:
        validation_receipts.bind_candidate_receipt(
            path,
            candidate_receipt,
            expected_format="WAV",
            expected_validator_id=validator_id,
            expected_validator_version=validator_version,
        )
    except validation_receipts.ReceiptValidationError as exc:
        raise ApprovalGateError(str(exc)) from exc
    if (
        validator_id != CANONICAL_WAV_VALIDATOR_ID
        or validator_version != CANONICAL_WAV_VALIDATOR_VERSION
    ):
        return None
    evidence = _canonical_receipt_evidence(candidate_receipt)
    expected = {
        "mediaRecipe": media.get("recipe"),
        "audioCodec": media.get("audioCodec"),
        "sampleRate": media.get("sampleRate"),
        "channels": media.get("channels"),
        "durationMs": media.get("durationMs"),
    }
    if any(evidence.get(key) != value for key, value in expected.items()):
        raise ApprovalGateError("WAV candidate receipt 与 current media binding 不一致")
    return CanonicalAudioResult(
        path=path.resolve(),
        recipe=copy.deepcopy(dict(expected["mediaRecipe"])),
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
        "recipe": copy.deepcopy(dict(result.recipe)),
    }
    for key, value in expected.items():
        if ref.get(key) != value:
            raise ApprovalGateError(f"{expected_file} 的 {key} 与登记身份不一致")
    return result


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


def _provider_receipt(
    request_id: str | None,
    *,
    provider_metadata: Mapping[str, Any] | None = None,
    text_prompt_sha256: str | None = None,
) -> dict[str, Any]:
    if request_id:
        digest = hashlib.sha256(request_id.encode("utf-8")).hexdigest()
        request_hash: str | None = f"sha256:{digest[:16]}"
    else:
        request_hash = None
    receipt: dict[str, Any] = {"providerRequestIdHash": request_hash}
    if text_prompt_sha256 is not None:
        if not re.fullmatch(r"[0-9a-f]{64}", text_prompt_sha256):
            raise VoiceoverStateError("provider text_prompt SHA-256 无效")
        receipt["textPromptSha256"] = text_prompt_sha256
    if isinstance(provider_metadata, Mapping):
        for field in (
            "durationMs",
            "originalDurationMs",
            "sentenceCount",
            "wordCount",
        ):
            value = provider_metadata.get(field)
            if value is not None:
                receipt[field] = value
        metadata_prompt_sha = provider_metadata.get("textPromptSha256")
        if metadata_prompt_sha is not None and metadata_prompt_sha != text_prompt_sha256:
            raise VoiceoverStateError("provider response text_prompt SHA-256 与请求不一致")
    return receipt


def _attempt_minimax_subtitle_candidate(
    project: Project, segment: Mapping[str, Any]
) -> Path:
    return _attempt_candidate(project, segment).with_name("minimax-subtitles.json")


def _attempt_doubao_subtitle_candidate(
    project: Project, segment: Mapping[str, Any]
) -> Path:
    return _attempt_candidate(project, segment).with_name("doubao-subtitles.json")


def _minimax_subtitle_receipt(path: Path) -> dict[str, Any]:
    try:
        payload = path.read_bytes()
        value = json.loads(payload.decode("utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise VoiceoverStateError(f"MiniMax 原生字幕文件无效: {exc}") from exc
    if not payload or not isinstance(value, (list, dict)):
        raise VoiceoverStateError("MiniMax 原生字幕 JSON 顶层结构无效")
    return {
        "schemaVersion": NATIVE_SUBTITLE_EVIDENCE_SCHEMA_VERSION,
        "kind": NATIVE_SUBTITLE_EVIDENCE_KIND,
        "provider": "minimax",
        "subtitleType": MINIMAX_SUBTITLE_TYPE,
        "bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }


def _write_minimax_subtitle_candidate(path: Path, payload: bytes) -> dict[str, Any]:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        receipt = _minimax_subtitle_receipt(temporary)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    if _minimax_subtitle_receipt(path) != receipt:
        raise VoiceoverStateError("MiniMax 原生字幕 candidate 发布后发生变化")
    return receipt


def _doubao_subtitle_receipt(path: Path) -> dict[str, Any]:
    try:
        payload = path.read_bytes()
        value = json.loads(payload.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise VoiceoverStateError(f"豆包原生字幕 sidecar 无效: {exc}") from exc
    if not isinstance(value, Mapping):
        raise VoiceoverStateError("豆包原生字幕 sidecar 顶层必须是对象")
    subtitle = value.get("subtitle")
    sentences = subtitle.get("sentences") if isinstance(subtitle, Mapping) else None
    if (
        value.get("schemaVersion") != DOUBAO_SUBTITLE_SCHEMA_VERSION
        or value.get("kind") != DOUBAO_SUBTITLE_KIND
        or value.get("provider") != "doubao"
        or value.get("model") != "seed-audio-1.0"
        or not isinstance(value.get("textPromptSha256"), str)
        or not re.fullmatch(r"[0-9a-f]{64}", value["textPromptSha256"])
        or isinstance(value.get("durationMs"), bool)
        or not isinstance(value.get("durationMs"), int)
        or not 0 < value["durationMs"] <= DOUBAO_MAX_AUDIO_DURATION_SECONDS * 1000
        or isinstance(value.get("originalDurationMs"), bool)
        or not isinstance(value.get("originalDurationMs"), int)
        or not 0 < value["originalDurationMs"] <= DOUBAO_MAX_AUDIO_DURATION_SECONDS * 1000
        or not isinstance(subtitle, Mapping)
        or not isinstance(subtitle.get("text"), str)
        or not subtitle["text"].strip()
        or not isinstance(sentences, list)
        or not sentences
    ):
        raise VoiceoverStateError("豆包原生字幕 sidecar 合同字段无效")
    word_count = 0
    previous_sentence_start = -1
    previous_word_start = -1
    sentence_texts: list[str] = []
    for sentence_index, sentence in enumerate(sentences, start=1):
        if not isinstance(sentence, Mapping):
            raise VoiceoverStateError("豆包原生字幕 sentence 必须是对象")
        start_ms = sentence.get("start_time")
        end_ms = sentence.get("end_time")
        sentence_text = sentence.get("text")
        words = sentence.get("words")
        if (
            isinstance(start_ms, bool)
            or not isinstance(start_ms, int)
            or isinstance(end_ms, bool)
            or not isinstance(end_ms, int)
            or start_ms < 0
            or end_ms <= start_ms
            or end_ms > value["durationMs"] + DOUBAO_TIMESTAMP_TOLERANCE_MS
            or start_ms < previous_sentence_start
            or not isinstance(sentence_text, str)
            or not sentence_text.strip()
            or not isinstance(words, list)
            or not words
        ):
            raise VoiceoverStateError(
                f"豆包原生字幕 sentence[{sentence_index}] 时间或文本无效"
            )
        word_texts: list[str] = []
        for word_index, word in enumerate(words, start=1):
            if not isinstance(word, Mapping):
                raise VoiceoverStateError("豆包原生字幕 word 必须是对象")
            word_start = word.get("start_time")
            word_end = word.get("end_time")
            word_text = word.get("text")
            has_lexical_content = (
                isinstance(word_text, str)
                and any(character.isalnum() for character in word_text)
            )
            if (
                isinstance(word_start, bool)
                or not isinstance(word_start, int)
                or isinstance(word_end, bool)
                or not isinstance(word_end, int)
                or word_start < 0
                or word_end < word_start
                or word_end
                > value["durationMs"] + DOUBAO_TIMESTAMP_TOLERANCE_MS
                or word_start < previous_word_start
                or (word_end == word_start and has_lexical_content)
                or not isinstance(word_text, str)
                or not word_text.strip()
            ):
                raise VoiceoverStateError(
                    f"豆包原生字幕 word[{sentence_index}:{word_index}] 时间或文本无效"
            )
            word_texts.append(word_text)
            previous_word_start = word_start
            word_count += 1
        if re.sub(r"\s+", "", "".join(word_texts)) != re.sub(
            r"\s+", "", sentence_text
        ):
            raise VoiceoverStateError("豆包原生字幕 sentence.text 与 words 不一致")
        sentence_texts.append(sentence_text)
        previous_sentence_start = start_ms
    if re.sub(r"\s+", "", "".join(sentence_texts)) != re.sub(
        r"\s+", "", subtitle["text"]
    ):
        raise VoiceoverStateError("豆包原生字幕 subtitle.text 与 sentences 不一致")
    return {
        "schemaVersion": NATIVE_SUBTITLE_EVIDENCE_SCHEMA_VERSION,
        "kind": NATIVE_SUBTITLE_EVIDENCE_KIND,
        "provider": "doubao",
        "model": value["model"],
        "subtitleType": DOUBAO_SUBTITLE_TYPE,
        "textPromptSha256": value["textPromptSha256"],
        "durationMs": value["durationMs"],
        "originalDurationMs": value["originalDurationMs"],
        "sentenceCount": len(sentences),
        "wordCount": word_count,
        "bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }


def _write_doubao_subtitle_candidate(
    path: Path,
    payload: bytes,
    *,
    expected_prompt_sha256: str,
    voice_synthesis_identity_hash: str,
) -> dict[str, Any]:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        receipt = _doubao_subtitle_receipt(temporary)
        if receipt["textPromptSha256"] != expected_prompt_sha256:
            raise VoiceoverStateError("豆包原生字幕未绑定 current 完整 text_prompt")
        receipt["voiceSynthesisIdentityHash"] = voice_synthesis_identity_hash
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    published = _doubao_subtitle_receipt(path)
    if any(published.get(key) != receipt.get(key) for key in published):
        raise VoiceoverStateError("豆包原生字幕 candidate 发布后发生变化")
    return receipt


def _provider_subtitle_candidate(
    project: Project, segment: Mapping[str, Any]
) -> Path:
    attempt = segment.get("currentAttempt")
    kind = attempt.get("providerSubtitleKind") if isinstance(attempt, Mapping) else None
    if kind == "minimax":
        return _attempt_minimax_subtitle_candidate(project, segment)
    if kind == "doubao":
        return _attempt_doubao_subtitle_candidate(project, segment)
    raise VoiceoverStateError("provider 原生字幕 attempt kind 无效")


def _provider_subtitle_receipt(
    project: Project, segment: Mapping[str, Any], path: Path
) -> dict[str, Any]:
    attempt = segment.get("currentAttempt")
    kind = attempt.get("providerSubtitleKind") if isinstance(attempt, Mapping) else None
    if kind == "minimax":
        return _minimax_subtitle_receipt(path)
    if kind == "doubao":
        receipt = _doubao_subtitle_receipt(path)
        receipt["voiceSynthesisIdentityHash"] = segment["voiceSynthesisIdentityHash"]
        return receipt
    raise VoiceoverStateError("provider 原生字幕 attempt kind 无效")


def _new_segment_attempt(
    project: Project,
    segment: dict[str, Any],
    unit: Mapping[str, Any],
    run_dir: Path,
    *,
    provider_subtitle_kind: str | None = None,
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
        "providerTextPromptSha256": unit.get("providerTextPromptSha256"),
        "candidateFile": candidate_relative,
        "candidateSha256": None,
        "candidateBytes": None,
        "validatorReceipt": None,
        "formalFile": segment["relativePath"],
        "externalOutcome": "not_started",
        "providerReceipt": None,
        "providerSubtitles": None,
        "providerSubtitlesRequired": provider_subtitle_kind is not None,
        "providerSubtitleKind": provider_subtitle_kind,
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
        "recipe": receipt_evidence.get("mediaRecipe"),
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
    if attempt.get("providerSubtitlesRequired") is True:
        subtitle_candidate = _provider_subtitle_candidate(project, segment)
        subtitle_receipt = _provider_subtitle_receipt(
            project, segment, subtitle_candidate
        )
        previous_subtitle_receipt = attempt.get("providerSubtitles")
        if previous_subtitle_receipt not in (None, subtitle_receipt):
            raise ApprovalGateError("provider 原生字幕 candidate receipt 已 stale")
        attempt["providerSubtitles"] = subtitle_receipt
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
    if attempt.get("providerSubtitlesRequired") is True:
        subtitle_candidate = _provider_subtitle_candidate(project, segment)
        subtitle_receipt = attempt.get("providerSubtitles")
        if not isinstance(subtitle_receipt, Mapping):
            raise VoiceoverStateError("validated attempt 缺少 provider 原生字幕 receipt")
        kind = attempt.get("providerSubtitleKind")
        destination = _voice_paths(project)[
            "minimax_subtitles" if kind == "minimax" else "doubao_subtitles"
        ]
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
        try:
            with subtitle_candidate.open("rb") as source, temporary.open("wb") as handle:
                shutil.copyfileobj(source, handle)
                handle.flush()
                os.fsync(handle.fileno())
            if _provider_subtitle_receipt(project, segment, temporary) != dict(
                subtitle_receipt
            ):
                raise VoiceoverStateError("provider 原生字幕正式副本与 receipt 不一致")
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
        if _provider_subtitle_receipt(project, segment, destination) != dict(
            subtitle_receipt
        ):
            raise VoiceoverStateError("provider 原生字幕正式发布后发生变化")


def _apply_candidate_media(project: Project, segment: dict[str, Any]) -> None:
    receipt = segment["currentAttempt"]["validatorReceipt"]
    evidence = _canonical_receipt_evidence(receipt)
    segment.update(
        {
            "audioMime": "audio/wav",
            "audioCodec": evidence["audioCodec"],
            "recipe": copy.deepcopy(evidence["mediaRecipe"]),
            "sampleRate": evidence["sampleRate"],
            "channels": evidence["channels"],
            "bytes": receipt["candidateBytes"],
            "durationMs": evidence["durationMs"],
            "sha256": receipt["candidateSha256"],
            "createdAt": segment.get("createdAt") or _now(),
        }
    )
    provider_subtitles = segment["currentAttempt"].get("providerSubtitles")
    if provider_subtitles is not None:
        kind = segment["currentAttempt"].get("providerSubtitleKind")
        segment["providerSubtitles"] = {
            **copy.deepcopy(provider_subtitles),
            "relativePath": (
                "audio/minimax-subtitles.json"
                if kind == "minimax"
                else "audio/doubao-subtitles.json"
            ),
        }


def _synthesize_candidate_worker(
    *,
    plan: Mapping[str, Any],
    unit: Mapping[str, Any],
    candidate: Path,
    work_dir: Path,
    adapter: ProviderAdapter,
    normalizer: Callable[..., CanonicalAudioResult],
    provider_subtitle_kind: str | None,
) -> dict[str, Any]:
    provider_returned = False
    try:
        provider_prompt = unit.get("_providerTextPrompt")
        if plan["provider"]["id"] == "doubao":
            if (
                not isinstance(provider_prompt, str)
                or text_prompt_sha256(provider_prompt)
                != unit.get("providerTextPromptSha256")
            ):
                raise PermanentProviderError("豆包 current 完整 text_prompt identity 无效")
        raw = adapter.synthesize(
            _request(plan, unit["speechText"], provider_text_prompt=provider_prompt)
        )
        provider_returned = True
        subtitle_receipt = None
        if provider_subtitle_kind is not None:
            expected_type = (
                MINIMAX_SUBTITLE_TYPE
                if provider_subtitle_kind == "minimax"
                else DOUBAO_SUBTITLE_TYPE
            )
            if raw.providerSubtitleType != expected_type or not isinstance(
                raw.providerSubtitleBytes, bytes
            ) or not raw.providerSubtitleBytes:
                error = PermanentProviderError("同请求响应缺少 provider 原生 word 字幕")
                error.provider_response_received = True  # type: ignore[attr-defined]
                error.external_result_incomplete = True  # type: ignore[attr-defined]
                raise error
            if provider_subtitle_kind == "minimax":
                subtitle_receipt = _write_minimax_subtitle_candidate(
                    candidate.with_name("minimax-subtitles.json"),
                    raw.providerSubtitleBytes,
                )
            else:
                subtitle_receipt = _write_doubao_subtitle_candidate(
                    candidate.with_name("doubao-subtitles.json"),
                    raw.providerSubtitleBytes,
                    expected_prompt_sha256=str(unit["providerTextPromptSha256"]),
                    voice_synthesis_identity_hash=str(
                        unit["voiceSynthesisIdentityHash"]
                    ),
                )
        result = normalizer(
            raw.bytes,
            candidate,
            work_dir=work_dir,
            declared_format=raw.declaredFormat,
        )
        return {
            "result": result,
            "providerReceipt": _provider_receipt(
                raw.providerRequestId,
                provider_metadata=raw.providerMetadata,
                text_prompt_sha256=unit.get("providerTextPromptSha256"),
            ),
            "providerSubtitles": subtitle_receipt,
        }
    except Exception as exc:  # classified by the single-writer coordinator
        return {
            "exception": exc,
            "providerReturned": provider_returned
            or getattr(exc, "provider_response_received", False),
            "externalResultIncomplete": getattr(
                exc, "external_result_incomplete", False
            ),
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
                segment["recipe"] = copy.deepcopy(dict(result.recipe))
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
    provider_subtitles_required = (
        isinstance(attempt, Mapping)
        and attempt.get("providerSubtitlesRequired") is True
    ) or isinstance(segment.get("providerSubtitles"), Mapping)
    if provider_subtitles_required:
        provider_subtitles = segment.get("providerSubtitles")
        if not isinstance(provider_subtitles, Mapping):
            raise ApprovalGateError("validated segment 缺少 provider 原生字幕 binding")
        kind = attempt.get("providerSubtitleKind") if isinstance(attempt, Mapping) else None
        if kind not in {"minimax", "doubao"}:
            raise ApprovalGateError("provider 原生字幕 kind 已 stale")
        relative_path = f"audio/{kind}-subtitles.json"
        subtitle_path = _voice_paths(project)[f"{kind}_subtitles"]
        if provider_subtitles.get("relativePath") != relative_path:
            raise ApprovalGateError("provider 原生字幕正式路径已 stale")
        receipt_keys = (
            ("schemaVersion", "kind", "provider", "subtitleType", "bytes", "sha256")
            if kind == "minimax"
            else (
                "schemaVersion",
                "kind",
                "provider",
                "model",
                "subtitleType",
                "textPromptSha256",
                "durationMs",
                "originalDurationMs",
                "sentenceCount",
                "wordCount",
                "bytes",
                "sha256",
                "voiceSynthesisIdentityHash",
            )
        )
        receipt = {key: provider_subtitles.get(key) for key in receipt_keys}
        if _provider_subtitle_receipt(project, segment, subtitle_path) != receipt:
            raise ApprovalGateError("provider 原生字幕 bytes/SHA/binding 已 stale")
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
        if attempt.get("providerSubtitlesRequired") is True:
            try:
                _provider_subtitle_receipt(
                    project, segment, _provider_subtitle_candidate(project, segment)
                )
            except VoiceoverStateError:
                attempt["externalOutcome"] = "unknown"
                _checkpoint_segment(
                    manifest_path,
                    manifest,
                    plan,
                    units,
                    segment,
                    status="unknown_external_outcome",
                    error_stage="provider-subtitles",
                    error_summary=(
                        "requesting 后音频 candidate 已存在，但 provider 原生字幕 "
                        "candidate 缺失或无效"
                    ),
                )
                raise ApprovalGateError(
                    "音频 candidate 已存在但同请求原生字幕结果不完整；"
                    "禁止自动重复请求，需人工决定"
                )
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
            subtitle_key = f"{project.voiceover_mode}_subtitles"
            if attempt.get("providerSubtitlesRequired") is True and not _voice_paths(
                project
            )[subtitle_key].is_file():
                _publish_segment_candidate(project, segment)
        else:
            _publish_segment_candidate(project, segment)
        _apply_candidate_media(project, segment)
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
        recipe=validated.recipe,
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
        "projectId": project.project_id,
        "sourceSrt": {"file": "source/source.srt", "sha256": plan["source"]["sha256"]},
        "voicePlanAuditHash": voice_plan_audit_hash(plan),
        "audio": {
            "file": "audio/narration.wav",
            "sha256": composite.sha256,
            "durationMs": composite.durationMs,
            "bytes": composite.bytes,
            "recipe": copy.deepcopy(dict(composite.recipe)),
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
            "schemaVersion": VOICE_IDENTITY_SCHEMA_VERSION,
            "kind": "fullVoiceIdentity",
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
            "schemaVersion": VOICE_IDENTITY_SCHEMA_VERSION,
            "kind": "fullTrackAudioIdentity",
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
    native_subtitle_provider: str | None = None,
) -> str:
    plan, units = _load_current_plan_units(project)
    if (
        plan["segmentation"]["mode"] != FULL_TRACK_SEGMENTATION_MODE
        or len(units) != 1
    ):
        raise ApprovalGateError("旧逐句 voice plan 已 stale；请重新准备 full-track voice plan")
    paths = _voice_paths(project)
    old = _read_json(paths["manifest"], "voice manifest")
    manifest = _fresh_manifest_with_reuse(project, plan, units, old)
    if project.voiceover_mode == "edge-tts":
        asr_preflight()
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
            attempt = _new_segment_attempt(
                project,
                segment,
                unit,
                run_dir,
                provider_subtitle_kind=native_subtitle_provider,
            )
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
            provider_subtitle_kind=native_subtitle_provider,
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
                            attempt["providerSubtitles"] = outcome.get(
                                "providerSubtitles"
                            )
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
                                _apply_candidate_media(project, segment)
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
                                _apply_candidate_media(project, segment)
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
                        elif outcome.get("externalResultIncomplete") is True:
                            attempt["externalOutcome"] = "unknown"
                            _checkpoint_segment(
                                paths["manifest"], manifest, plan, units, segment,
                                status="unknown_external_outcome",
                                error_stage="provider-subtitles",
                                error_summary=(
                                    "provider 已返回音频但同请求原生字幕不完整；禁止自动重发"
                                ),
                            )
                            worker_error = ApprovalGateError(
                                "音频响应后的同请求原生字幕结果不完整；需人工决定是否重新请求"
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
        "source": (
            f"{native_subtitle_provider}-provider-native-word"
            if native_subtitle_provider is not None
            else "external-asr-srt"
        ),
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
                "timingValidationProfile": diagnostics.get(
                    "timingValidationProfile"
                ),
                "captionSegmentationRecipe": diagnostics.get(
                    "captionSegmentationRecipe"
                ),
                "subtitleGaps": copy.deepcopy(diagnostics.get("subtitleGaps")),
                "localAcousticRate": copy.deepcopy(
                    diagnostics.get("localAcousticRate")
                ),
                "acousticEvidence": copy.deepcopy(
                    diagnostics.get("acousticEvidence")
                ),
                "providerNativeEvidence": copy.deepcopy(
                    diagnostics.get("providerNativeEvidence")
                ),
            }
        )
    timeline = {
        "schemaVersion": VOICE_TIMELINE_SCHEMA_VERSION,
        "projectId": project.project_id,
        "sourceSrt": {"file": "source/source.srt", "sha256": plan["source"]["sha256"]},
        "voicePlanAuditHash": voice_plan_audit_hash(plan),
        "audio": {
            "file": "audio/narration.wav",
            "sha256": composite.sha256,
            "durationMs": composite.durationMs,
            "bytes": composite.bytes,
            "recipe": copy.deepcopy(dict(composite.recipe)),
        },
        "renderProfileSha256": sha256_json(project.render_profile),
        "alignment": {
            "schemaVersion": alignment.get("schemaVersion", 1),
            "kind": "referenceAudioAlignment",
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


def _minimax_subtitle_items(value: object) -> list[Mapping[str, Any]]:
    """Locate the provider's word entries without accepting unrelated arrays."""

    def locate(current: object) -> list[Mapping[str, Any]]:
        if isinstance(current, list):
            if current and all(isinstance(item, Mapping) for item in current):
                entries = [item for item in current if isinstance(item, Mapping)]
                if all(
                    (
                        (
                            any(key in item for key in ("text", "word", "content"))
                            and any(
                                key in item
                                for key in (
                                    "start_time",
                                    "startTime",
                                    "begin_time",
                                    "beginTime",
                                )
                            )
                            and any(key in item for key in ("end_time", "endTime"))
                        )
                        or (
                            "word" in item
                            and "time_begin" in item
                            and "time_end" in item
                        )
                    )
                    for item in entries
                ):
                    return entries

            # MiniMax 的真实长文本响应是句段数组；每个句段各自包含一组
            # timestamped_words。必须按句段原序汇总全部词条，不能在找到
            # 第一组后提前返回，否则只会拿首句去覆盖整篇已确认原稿。
            combined: list[Mapping[str, Any]] = []
            for item in current:
                combined.extend(locate(item))
            return combined

        if isinstance(current, Mapping):
            for key in (
                "timestamped_words",
                "words",
                "subtitles",
                "subtitle",
                "sentences",
                "content",
                "data",
                "result",
            ):
                if key in current:
                    entries = locate(current[key])
                    if entries:
                        return entries
        return []

    entries = locate(value)
    if entries:
        return entries
    raise VoiceoverStateError("MiniMax 原生字幕 JSON 没有 word 时间戳条目")


def _minimax_time_ms(item: Mapping[str, Any], *, start: bool) -> int:
    second_keys = ("start_time", "startTime") if start else ("end_time", "endTime")
    millisecond_keys = (
        ("time_begin", "begin_time", "beginTime")
        if start
        else ("time_end",)
    )
    for key in second_keys:
        if key in item:
            raw = item[key]
            if isinstance(raw, bool) or not isinstance(raw, (int, float)):
                raise VoiceoverStateError(f"MiniMax 原生字幕 {key} 必须是数字")
            return round(float(raw) * 1000)
    for key in millisecond_keys:
        if key in item:
            raw = item[key]
            if isinstance(raw, bool) or not isinstance(raw, (int, float)):
                raise VoiceoverStateError(f"MiniMax 原生字幕 {key} 必须是数字")
            return round(float(raw))
    raise VoiceoverStateError("MiniMax 原生字幕缺少起止时间")


def _minimax_word_srt(path: Path, audio_duration_ms: int) -> tuple[str, dict[str, Any]]:
    receipt = _minimax_subtitle_receipt(path)
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise VoiceoverStateError(f"无法读取 MiniMax 原生字幕: {exc}") from exc
    items = _minimax_subtitle_items(value)
    cues: list[dict[str, Any]] = []
    previous_end = -1
    ignored_whitespace_entries = 0
    for index, item in enumerate(items, start=1):
        text_value = next(
            (item[key] for key in ("text", "word", "content") if key in item),
            None,
        )
        if not isinstance(text_value, str):
            raise VoiceoverStateError(f"MiniMax 原生字幕 word[{index}] 文本为空")
        start_ms = _minimax_time_ms(item, start=True)
        end_ms = _minimax_time_ms(item, start=False)
        if end_ms > audio_duration_ms and end_ms - audio_duration_ms <= 100:
            end_ms = audio_duration_ms
        if (
            start_ms < 0
            or end_ms <= start_ms
            or end_ms > audio_duration_ms
            or (previous_end >= 0 and start_ms < previous_end)
        ):
            raise VoiceoverStateError(
                f"MiniMax 原生字幕 word[{index}] 时间必须递增且位于整轨范围内"
            )
        if not text_value.strip():
            if not text_value or not text_value.isspace():
                raise VoiceoverStateError(f"MiniMax 原生字幕 word[{index}] 文本为空")
            ignored_whitespace_entries += 1
            previous_end = end_ms
            continue
        cues.append(
            {
                "originalIndex": index,
                "startMs": start_ms,
                "endMs": end_ms,
                "text": text_value,
            }
        )
        previous_end = end_ms
    evidence = {
        **receipt,
        "validated": True,
        "evidenceKind": "provider_native_word_timestamp",
        "providerWordEntryCount": len(items),
        "ignoredWhitespaceEntryCount": ignored_whitespace_entries,
        "wordEntryCount": len(cues),
        "audioDurationMs": audio_duration_ms,
    }
    return serialize_srt(cues), evidence


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
        raw_json.get("schemaVersion") != 1
        or raw_json.get("kind") != "narrationAsrEvidence"
        or raw_json.get("pipelineRecipe") != NARRATION_ASR_PIPELINE_RECIPE
        or receipt.get("schemaVersion") != 1
        or receipt.get("kind") != "narrationAsrReceipt"
        or receipt.get("pipelineRecipe") != NARRATION_ASR_PIPELINE_RECIPE
        or not isinstance(segment_evidence, Mapping)
        or segment_evidence.get("validated") is not True
        or segment_evidence.get("recipe")
        != NARRATION_ASR_RECONSTRUCTION_RECIPE
        or segment_evidence.get("maxVadSegmentMs")
        != NARRATION_ASR_MAX_VAD_SEGMENT_MS
        or segment_evidence.get("reconstructionMatchesTopLevel") is not True
        or not isinstance(segment_evidence.get("segmentCount"), int)
        or segment_evidence.get("segmentCount", 0) <= 0
        or not isinstance(segment_evidence.get("tokenCount"), int)
        or segment_evidence.get("tokenCount", 0) <= 0
        or not isinstance(receipt_evidence, Mapping)
        or receipt_evidence.get("validated") is not True
        or receipt_evidence.get("recipe") != segment_evidence.get("recipe")
        or receipt_evidence.get("maxVadSegmentMs")
        != NARRATION_ASR_MAX_VAD_SEGMENT_MS
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
        "pipelineRecipe": copy.deepcopy(NARRATION_ASR_PIPELINE_RECIPE),
        "reconstructionRecipe": copy.deepcopy(
            NARRATION_ASR_RECONSTRUCTION_RECIPE
        ),
        "validated": True,
        "evidenceKind": "vad_segment_token_timestamp",
        "maxVadSegmentMs": NARRATION_ASR_MAX_VAD_SEGMENT_MS,
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


def _commit_alignment(
    project: Project,
    plan: Mapping[str, Any],
    units: Sequence[Mapping[str, Any]],
    manifest: dict[str, Any],
    composite: CanonicalAudioResult,
    alignment: Mapping[str, Any],
    *,
    evidence_sha256: str,
    alignment_source: str,
    run_kind: str,
) -> str:
    paths = _voice_paths(project)
    timeline, narration_srt = _build_aligned_timeline(
        project,
        plan,
        units[0],
        composite,
        alignment,
        asr_srt_sha256=evidence_sha256,
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
        "schemaVersion": VOICE_TIMELINE_SCHEMA_VERSION,
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
        "evidenceSha256": evidence_sha256,
        "schemaVersion": timeline["alignment"]["schemaVersion"],
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
            "kind": run_kind,
            "status": "validated",
            "startedAt": _now(),
            "finishedAt": _now(),
            "taskCount": 1,
        }
    )
    _write_manifest(paths["manifest"], manifest, plan, units)
    return manifest["fullIdentityHash"]


def _publish_alignment(
    project: Project,
    asr_srt_path: Path,
    *,
    alignment_source: str = "external-asr-srt",
) -> str:
    if project.voiceover_mode != "edge-tts":
        raise VoiceoverStateError("publish-alignment 只允许 Edge FunASR 项目")
    if align_reference_audio is None:
        raise VoiceoverStateError("reference audio alignment 模块尚未安装")
    plan, units = _load_current_plan_units(project)
    if (
        plan["segmentation"]["mode"] != FULL_TRACK_SEGMENTATION_MODE
        or len(units) != 1
    ):
        raise VoiceoverStateError("publish-alignment 只支持 full-track voice plan")
    paths = _voice_paths(project)
    manifest = validate_voice_manifest(
        _read_json(paths["manifest"], "voice manifest"), voice_plan=plan, speech_units=units
    )
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
    return _commit_alignment(
        project,
        plan,
        units,
        manifest,
        composite,
        alignment,
        evidence_sha256=asr_sha,
        alignment_source=alignment_source,
        run_kind="publish-alignment",
    )


def _publish_minimax_alignment(project: Project) -> str:
    """Publish MiniMax word timing without running a second ASR engine."""

    if project.voiceover_mode != "minimax":
        raise VoiceoverStateError("MiniMax 原生字幕入口只允许 minimax 项目")
    if align_reference_audio is None:
        raise VoiceoverStateError("reference audio alignment 模块尚未安装")
    plan, units = _load_current_plan_units(project)
    if (
        plan["provider"]["id"] != "minimax"
        or plan["provider"].get("options", {}).get("endpoint") != MINIMAX_ENDPOINT
        or plan["segmentation"]["mode"] != FULL_TRACK_SEGMENTATION_MODE
        or len(units) != 1
    ):
        raise ApprovalGateError("MiniMax voice plan 尚未启用 current 原生 word 字幕合同")
    paths = _voice_paths(project)
    manifest = validate_voice_manifest(
        _read_json(paths["manifest"], "voice manifest"),
        voice_plan=plan,
        speech_units=units,
    )
    segment = manifest["segments"][0] if len(manifest["segments"]) == 1 else None
    attempt = segment.get("currentAttempt") if isinstance(segment, Mapping) else None
    if (
        not isinstance(segment, Mapping)
        or not isinstance(attempt, Mapping)
        or attempt.get("providerSubtitlesRequired") is not True
        or not isinstance(attempt.get("providerSubtitles"), Mapping)
        or not isinstance(segment.get("providerSubtitles"), Mapping)
        or not _segment_is_reusable(project, segment, units[0])
    ):
        raise VoiceoverStateError("MiniMax 整轨 synthesis segment 尚未 validated")
    composite_ref = manifest.get("composite")
    if not isinstance(composite_ref, Mapping) or composite_ref.get("status") != "validated":
        raise VoiceoverStateError("audio/narration.wav 尚未 validated")
    composite = _validate_media_ref(
        project, composite_ref, expected_file="audio/narration.wav"
    )
    if manifest.get("fullAudioIdentityHash") != _full_audio_identity(plan, composite_ref):
        raise ApprovalGateError("整轨音频技术 identity 已 stale")
    provider_srt, native_evidence = _minimax_word_srt(
        paths["minimax_subtitles"], composite.durationMs
    )
    source_srt = project.path("source/source.srt").read_text(encoding="utf-8-sig")
    alignment = align_reference_audio(
        source_srt,
        provider_srt,
        project.timing_plan["scenes"],
        composite.durationMs,
        min_match_ratio=0.98,
        max_normalized_edit_ratio=0.02,
        timing_validation_profile="minimax-provider-native-word",
    )
    if not isinstance(alignment, Mapping):
        raise VoiceoverStateError("MiniMax provider-native alignment 返回值无效")
    diagnostics = alignment.get("diagnostics")
    if not isinstance(diagnostics, dict):
        raise VoiceoverStateError("MiniMax provider-native alignment 缺少 diagnostics")
    native_evidence.update(
        {
            "audioSha256": composite.sha256,
            "fullAudioIdentityHash": manifest["fullAudioIdentityHash"],
            "voiceSynthesisIdentityHash": units[0]["voiceSynthesisIdentityHash"],
        }
    )
    diagnostics["providerNativeEvidence"] = native_evidence
    return _commit_alignment(
        project,
        plan,
        units,
        manifest,
        composite,
        alignment,
        evidence_sha256=native_evidence["sha256"],
        alignment_source="minimax-provider-native-word",
        run_kind="publish-minimax-native-alignment",
    )


def _doubao_word_srt(
    path: Path, audio_duration_ms: int
) -> tuple[str, dict[str, Any]]:
    """严格读取 Seed Audio 固定 subtitle.sentences[].words[] 结构。"""

    receipt = _doubao_subtitle_receipt(path)
    if abs(receipt["durationMs"] - audio_duration_ms) > 250:
        raise VoiceoverStateError("豆包响应 duration 与 canonical narration.wav 不一致")
    value = _read_json(path, "豆包原生字幕 sidecar")
    subtitle = value["subtitle"]
    cues: list[dict[str, Any]] = []
    previous_end = -1
    ignored_non_lexical_tokens = 0
    adjusted_overlap_tokens = 0
    for sentence in subtitle["sentences"]:
        for word in sentence["words"]:
            start_ms = int(word["start_time"])
            end_ms = int(word["end_time"])
            word_text = str(word["text"])
            if not any(character.isalnum() for character in word_text):
                ignored_non_lexical_tokens += 1
                continue
            if end_ms > audio_duration_ms and end_ms - audio_duration_ms <= 100:
                end_ms = audio_duration_ms
            if previous_end >= 0 and start_ms < previous_end:
                overlap_ms = previous_end - start_ms
                if (
                    overlap_ms > DOUBAO_TIMESTAMP_TOLERANCE_MS
                    or end_ms <= previous_end
                ):
                    raise VoiceoverStateError(
                        "豆包原生字幕语义 token 重叠超过安全修正范围"
                    )
                start_ms = previous_end
                adjusted_overlap_tokens += 1
            if (
                start_ms < 0
                or end_ms <= start_ms
                or end_ms > audio_duration_ms
            ):
                raise VoiceoverStateError(
                    "豆包原生字幕 word 时间必须递增且位于 canonical 整轨范围内"
                )
            cues.append(
                {
                    "originalIndex": len(cues) + 1,
                    "startMs": start_ms,
                    "endMs": end_ms,
                    "text": word_text,
                }
            )
            previous_end = end_ms
    if not cues:
        raise VoiceoverStateError("豆包原生字幕没有可用 word token")
    evidence = {
        **receipt,
        "validated": True,
        "evidenceKind": "provider_native_word_timestamp",
        "wordEntryCount": len(cues),
        "ignoredNonLexicalTokenCount": ignored_non_lexical_tokens,
        "adjustedOverlapTokenCount": adjusted_overlap_tokens,
        "audioDurationMs": audio_duration_ms,
    }
    return serialize_srt(cues), evidence


def _publish_doubao_alignment(project: Project) -> str:
    """发布同一次 Seed Audio 响应的严格字级时间证据，不运行 FunASR。"""

    if project.voiceover_mode != "doubao":
        raise VoiceoverStateError("豆包原生字幕入口只允许 doubao 项目")
    if align_reference_audio is None:
        raise VoiceoverStateError("reference audio alignment 模块尚未安装")
    plan, units = _load_current_plan_units(project)
    if (
        plan["provider"]["id"] != "doubao"
        or plan["provider"].get("options", {}).get("model") != "seed-audio-1.0"
        or plan["segmentation"]["mode"] != FULL_TRACK_SEGMENTATION_MODE
        or len(units) != 1
    ):
        raise ApprovalGateError(
            "豆包 voice plan 不符合 current prompt-only v3 原生 word 字幕合同"
        )
    paths = _voice_paths(project)
    manifest = validate_voice_manifest(
        _read_json(paths["manifest"], "voice manifest"),
        voice_plan=plan,
        speech_units=units,
    )
    segment = manifest["segments"][0] if len(manifest["segments"]) == 1 else None
    attempt = segment.get("currentAttempt") if isinstance(segment, Mapping) else None
    if (
        not isinstance(segment, Mapping)
        or not isinstance(attempt, Mapping)
        or attempt.get("providerSubtitlesRequired") is not True
        or attempt.get("providerSubtitleKind") != "doubao"
        or attempt.get("providerTextPromptSha256")
        != units[0].get("providerTextPromptSha256")
        or not isinstance(attempt.get("providerSubtitles"), Mapping)
        or not isinstance(segment.get("providerSubtitles"), Mapping)
        or not _segment_is_reusable(project, segment, units[0])
    ):
        raise VoiceoverStateError(
            "豆包整轨 synthesis segment 尚未按 prompt-only v3 validated"
        )
    composite_ref = manifest.get("composite")
    if not isinstance(composite_ref, Mapping) or composite_ref.get("status") != "validated":
        raise VoiceoverStateError("audio/narration.wav 尚未 validated")
    composite = _validate_media_ref(
        project, composite_ref, expected_file="audio/narration.wav"
    )
    if manifest.get("fullAudioIdentityHash") != _full_audio_identity(plan, composite_ref):
        raise ApprovalGateError("整轨音频技术 identity 已 stale")
    provider_srt, native_evidence = _doubao_word_srt(
        paths["doubao_subtitles"], composite.durationMs
    )
    prompt_sha = units[0]["providerTextPromptSha256"]
    if native_evidence.get("textPromptSha256") != prompt_sha:
        raise ApprovalGateError("豆包字幕 sidecar 未绑定 current 完整 text_prompt")
    source_srt = project.path("source/source.srt").read_text(encoding="utf-8-sig")
    alignment = align_reference_audio(
        source_srt,
        provider_srt,
        project.timing_plan["scenes"],
        composite.durationMs,
        min_match_ratio=0.98,
        max_normalized_edit_ratio=0.02,
        timing_validation_profile="doubao-provider-native-word",
    )
    if not isinstance(alignment, Mapping):
        raise VoiceoverStateError("豆包 provider-native alignment 返回值无效")
    diagnostics = alignment.get("diagnostics")
    if not isinstance(diagnostics, dict):
        raise VoiceoverStateError("豆包 provider-native alignment 缺少 diagnostics")
    native_evidence.update(
        {
            "audioSha256": composite.sha256,
            "fullAudioIdentityHash": manifest["fullAudioIdentityHash"],
            "voiceSynthesisIdentityHash": units[0]["voiceSynthesisIdentityHash"],
            "textPromptSha256": prompt_sha,
        }
    )
    diagnostics["providerNativeEvidence"] = native_evidence
    return _commit_alignment(
        project,
        plan,
        units,
        manifest,
        composite,
        alignment,
        evidence_sha256=native_evidence["sha256"],
        alignment_source="doubao-provider-native-word",
        run_kind="publish-doubao-native-alignment",
    )


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
        payload.get("schemaVersion") != 1
        or payload.get("kind") != "narrationAsrResult"
        or payload.get("pipelineRecipe") != NARRATION_ASR_PIPELINE_RECIPE
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
    result: dict[str, Any] = {
        "voicePlanAuditHash": voice_plan_audit_hash(plan),
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
    if timeline.get("schemaVersion") != VOICE_TIMELINE_SCHEMA_VERSION:
        raise ApprovalGateError("audio timeline schemaVersion 已 stale，必须重新执行 token 对齐")
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
    common_alignment_invalid = (
        not isinstance(alignment_diagnostics, Mapping)
        or alignment_diagnostics.get("tokenTimingUsed") is not True
        or alignment_diagnostics.get("qualityGatePassed") is not True
        or alignment_diagnostics.get("captionSegmentationRecipe")
        != {
            "algorithm": "reference_punctuation_caption",
            "version": 1,
            "parameters": {},
        }
    )
    if project.voiceover_mode == "minimax":
        native_evidence = (
            alignment_diagnostics.get("providerNativeEvidence")
            if isinstance(alignment_diagnostics, Mapping)
            else None
        )
        subtitle_receipt = _minimax_subtitle_receipt(paths["minimax_subtitles"])
        if (
            common_alignment_invalid
            or timeline.get("alignment", {}).get("source")
            != "minimax-provider-native-word"
            or alignment_diagnostics.get("timingValidationProfile")
            != "minimax-provider-native-word"
            or not isinstance(native_evidence, Mapping)
            or native_evidence.get("validated") is not True
            or native_evidence.get("schemaVersion")
            != NATIVE_SUBTITLE_EVIDENCE_SCHEMA_VERSION
            or native_evidence.get("kind") != NATIVE_SUBTITLE_EVIDENCE_KIND
            or native_evidence.get("provider") != "minimax"
            or native_evidence.get("evidenceKind")
            != "provider_native_word_timestamp"
            or native_evidence.get("subtitleType") != MINIMAX_SUBTITLE_TYPE
            or native_evidence.get("sha256") != subtitle_receipt["sha256"]
            or native_evidence.get("bytes") != subtitle_receipt["bytes"]
            or native_evidence.get("audioSha256") != composite.sha256
            or native_evidence.get("fullAudioIdentityHash")
            != manifest.get("fullAudioIdentityHash")
            or native_evidence.get("voiceSynthesisIdentityHash")
            != units[0]["voiceSynthesisIdentityHash"]
        ):
            raise VoiceoverStateError(
                "MiniMax audio timeline 缺少 current provider-native word 字幕证据"
            )
    elif project.voiceover_mode == "doubao":
        native_evidence = (
            alignment_diagnostics.get("providerNativeEvidence")
            if isinstance(alignment_diagnostics, Mapping)
            else None
        )
        subtitle_receipt = _doubao_subtitle_receipt(paths["doubao_subtitles"])
        expected_prompt_sha = units[0].get("providerTextPromptSha256")
        if (
            common_alignment_invalid
            or timeline.get("alignment", {}).get("source")
            != "doubao-provider-native-word"
            or alignment_diagnostics.get("timingValidationProfile")
            != "doubao-provider-native-word"
            or not isinstance(native_evidence, Mapping)
            or native_evidence.get("validated") is not True
            or native_evidence.get("schemaVersion")
            != NATIVE_SUBTITLE_EVIDENCE_SCHEMA_VERSION
            or native_evidence.get("kind") != NATIVE_SUBTITLE_EVIDENCE_KIND
            or native_evidence.get("provider") != "doubao"
            or native_evidence.get("model") != "seed-audio-1.0"
            or native_evidence.get("evidenceKind")
            != "provider_native_word_timestamp"
            or native_evidence.get("subtitleType") != DOUBAO_SUBTITLE_TYPE
            or native_evidence.get("sha256") != subtitle_receipt["sha256"]
            or native_evidence.get("bytes") != subtitle_receipt["bytes"]
            or native_evidence.get("textPromptSha256") != expected_prompt_sha
            or native_evidence.get("audioSha256") != composite.sha256
            or native_evidence.get("fullAudioIdentityHash")
            != manifest.get("fullAudioIdentityHash")
            or native_evidence.get("voiceSynthesisIdentityHash")
            != units[0]["voiceSynthesisIdentityHash"]
        ):
            raise VoiceoverStateError(
                "豆包 audio timeline 缺少 current 同请求原生 word 字幕/prompt/audio 证据"
            )
    elif (
        common_alignment_invalid
        or not isinstance(acoustic_evidence, Mapping)
        or acoustic_evidence.get("validated") is not True
        or acoustic_evidence.get("pipelineRecipe")
        != NARRATION_ASR_PIPELINE_RECIPE
        or acoustic_evidence.get("reconstructionRecipe")
        != NARRATION_ASR_RECONSTRUCTION_RECIPE
        or acoustic_evidence.get("maxVadSegmentMs")
        != NARRATION_ASR_MAX_VAD_SEGMENT_MS
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
            "provider": {
                "id": plan["provider"]["id"],
                **{
                    key: plan["provider"].get("options", {}).get(key)
                    for key in ("packageVersion", "model", "endpoint")
                    if plan["provider"].get("options", {}).get(key) is not None
                },
            },
            "fullAudioIdentityHash": manifest.get("fullAudioIdentityHash"),
        }
    )
    if project.voiceover_mode == "doubao":
        result["providerTextPromptSha256"] = units[0].get(
            "providerTextPromptSha256"
        )
        result["providerSubtitle"] = copy.deepcopy(
            manifest["segments"][0].get("providerSubtitles")
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
    autonomous = _technical_audio_progress_authorized(project)
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
            "technical_after_initial_approval" if autonomous else "human_full_listening"
        ),
        "reviewBasis": (
            "initial_content_plan_authorization_and_current_technical_validation"
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
) -> bool:
    """Validate the initial content/plan authorization required for autonomy."""

    if not (project.initial_approval_completed and project.agent_approval_enabled):
        return False
    initial = project.metadata.get("initialApproval")
    # Missing initialApproval is a legacy-formal-project compatibility view,
    # not evidence that the user granted the autonomous approval policy.
    if initial is None:
        return False
    if not isinstance(initial, Mapping) or initial.get("status") != "approved":
        raise ApprovalGateError("技术自主推进要求可审计的 current 初始联合批准")
    if (
        initial.get("approvalBasis") != "user_joint_content_and_plan"
        or initial.get("contentIdentitySha256")
        != project.current_content_identity_sha256
    ):
        raise ApprovalGateError("技术自主推进要求 current 内容与制作方案批准")
    return True


def _status(project: Project) -> dict[str, Any]:
    paths = _voice_paths(project)
    payload: dict[str, Any] = {
        "projectId": project.project_id,
        "voiceoverMode": project.voiceover_mode,
        "voicePlan": "missing",
        "segments": {},
        "full": "missing",
        "pendingAudioTimeline": project.pending_audio_timeline,
    }
    if not paths["manifest"].is_file():
        return payload
    manifest = _read_json(paths["manifest"], "voice manifest")
    payload["voicePlan"] = manifest.get("voicePlan", {}).get("voicePlanAuditHash", "invalid")
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
    full = sub.add_parser("full", help="生成/恢复整篇单次请求的 canonical 旁白音频")
    full.add_argument("--project", required=True, type=Path)
    full.add_argument("--retry-failed", action="store_true")
    full.add_argument(
        "--voice",
        help="首次生成时仅 Edge/MiniMax 可用；省略读取 provider 配置",
    )
    full.add_argument("--rate", type=int, help="首次生成时可覆盖 provider 默认语速")
    full.add_argument(
        "--doubao-performance-brief",
        type=Path,
        help="豆包首次完整旁白生成必填；已有 current voice plan 时必须省略",
    )
    publish_alignment = sub.add_parser(
        "publish-alignment",
        help="仅为 Edge 导入 FunASR token SRT 并发布 timeline/FULL_IDENTITY",
    )
    publish_alignment.add_argument("--project", required=True, type=Path)
    publish_alignment.add_argument("--asr-srt", required=True, type=Path)
    approve_full = sub.add_parser(
        "approve-full",
        help="持久化完整旁白批准；自主模式基于阶段 0 授权和 current 技术证据推进",
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
            allow_pending_initial_approval=args.command == "status",
        )
        execution = workspace_config
        if execution is None and argv is None:
            execution = load_workspace_config()
        concurrency = execution.concurrency if execution is not None else ExecutionConcurrency()
        if args.command == "full":
            current_plan, _ = _prepare_full_plan(
                project,
                voice=args.voice,
                rate=args.rate,
                doubao_performance_brief=args.doubao_performance_brief,
            )
            native_subtitle_provider = (
                project.voiceover_mode
                if project.voiceover_mode in {"minimax", "doubao"}
                else None
            )
            audio_identity = _full(
                project, retry_failed=args.retry_failed,
                adapter=adapter or _adapter_from_plan(
                    current_plan, native_minimax_subtitles=True
                ),
                normalizer=normalizer or normalize_to_candidate,
                configured_concurrency=concurrency.for_stage("voiceGeneration"),
                asr_preflight=asr_preflight or _preflight_narration_asr,
                native_subtitle_provider=native_subtitle_provider,
            )
            print(f"FULL_AUDIO={project.path('audio/narration.wav')}")
            print(f"FULL_AUDIO_IDENTITY={audio_identity}")
            if native_subtitle_provider == "minimax":
                identity = _publish_minimax_alignment(project)
                print(
                    f"MINIMAX_SUBTITLES={project.path('audio/minimax-subtitles.json')}"
                )
                print(f"NARRATION_SRT={project.path('audio/narration.srt')}")
                print(f"AUDIO_TIMELINE={project.path('audio/timeline.json')}")
                print(f"FULL_IDENTITY={identity}")
            elif native_subtitle_provider == "doubao":
                identity = _publish_doubao_alignment(project)
                print(
                    f"DOUBAO_SUBTITLES={project.path('audio/doubao-subtitles.json')}"
                )
                print(f"NARRATION_SRT={project.path('audio/narration.srt')}")
                print(f"AUDIO_TIMELINE={project.path('audio/timeline.json')}")
                print(f"FULL_IDENTITY={identity}")
            else:
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
