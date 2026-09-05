from __future__ import annotations

import copy
import concurrent.futures
import json
import unittest
from dataclasses import fields
from pathlib import Path

from scripts.voiceover import (
    DEFAULT_PROVIDER_CONTRACT_VERSION,
    CancelledError,
    FakeProviderAdapter,
    PermanentProviderError,
    ProviderAdapter,
    RawAudioResult,
    RetryableProviderError,
    SynthesisRequest,
    VoiceoverValidationError,
    bind_synthesis_identities,
    build_voice_plan,
    create_voice_manifest,
    normalize_pitch,
    normalize_rate,
    normalize_volume,
    plan_full_track_unit,
    plan_speech_units,
    validate_voice_manifest,
    validate_voice_plan,
    voice_plan_audit_hash,
)


ROOT = Path(__file__).resolve().parents[1]
SOURCE_SHA = "a" * 64
SEGMENTATION = {
    "contractVersion": "speech-unit-v1",
    "minCodePoints": 6,
    "targetCodePoints": 12,
    "maxCodePoints": 18,
}


def cue(ordinal: int, text: str, start: int | None = None, end: int | None = None) -> dict:
    start_ms = (ordinal - 1) * 1000 if start is None else start
    end_ms = ordinal * 1000 if end is None else end
    return {
        "index": ordinal,
        "sourceOrdinal": ordinal,
        "originalIndex": ordinal * 10,
        "startMs": start_ms,
        "endMs": end_ms,
        "durMs": end_ms - start_ms,
        "text": text,
    }


def make_plan(cues: list[dict], scenes: list[dict] | None = None, **overrides: object) -> dict:
    arguments = {
        "project_id": "project-voiceover-test",
        "source_srt_sha256": SOURCE_SHA,
        "cues": cues,
        "scenes": scenes,
        "segmentation": SEGMENTATION,
    }
    arguments.update(overrides)
    return build_voice_plan(**arguments)


class ProviderProtocolTests(unittest.TestCase):
    def test_frozen_dataclass_fields_and_positional_result_contract(self) -> None:
        self.assertEqual(
            [field.name for field in fields(SynthesisRequest)],
            [
                "text",
                "voice",
                "normalizedRate",
                "normalizedPitch",
                "normalizedVolume",
                "providerContractVersion",
                "timeoutSeconds",
                "cancellationToken",
            ],
        )
        result = RawAudioResult(b"raw", "audio/mpeg")
        self.assertEqual(result.bytes, b"raw")
        self.assertEqual(result.declaredFormat, "audio/mpeg")
        self.assertIsNone(result.providerRequestId)

    def test_provider_exceptions_support_message_only_constructor(self) -> None:
        for error_type in (RetryableProviderError, PermanentProviderError, CancelledError):
            self.assertEqual(str(error_type("message")), "message")

    def test_fake_provider_uses_same_protocol_and_never_networks(self) -> None:
        adapter = FakeProviderAdapter(b"deterministic", "audio/mpeg", "safe-id")
        request = SynthesisRequest(
            text="测试",
            voice="zh-CN-YunjianNeural",
            normalizedRate="+0%",
            normalizedPitch="+0Hz",
            normalizedVolume="+0%",
            providerContractVersion=DEFAULT_PROVIDER_CONTRACT_VERSION,
            timeoutSeconds=60,
            cancellationToken=None,
        )
        self.assertIsInstance(adapter, ProviderAdapter)
        self.assertEqual(adapter.synthesize(request), RawAudioResult(b"deterministic", "audio/mpeg", "safe-id"))
        self.assertEqual(adapter.requests, [request])

    def test_fake_provider_supports_classified_failures_and_cancellation(self) -> None:
        request = SynthesisRequest("文本", "voice", "+0%", "+0Hz", "+0%", DEFAULT_PROVIDER_CONTRACT_VERSION, 5, None)
        adapter = FakeProviderAdapter(outcomes=[RetryableProviderError("timeout"), PermanentProviderError("bad voice")])
        with self.assertRaisesRegex(RetryableProviderError, "timeout"):
            adapter.synthesize(request)
        with self.assertRaisesRegex(PermanentProviderError, "bad voice"):
            adapter.synthesize(request)

        class Token:
            cancelled = True

        cancelled = copy.copy(request)
        object.__setattr__(cancelled, "cancellationToken", Token())
        with self.assertRaisesRegex(CancelledError, "取消"):
            FakeProviderAdapter().synthesize(cancelled)

    def test_fake_provider_request_and_outcome_queues_are_thread_safe(self) -> None:
        requests = [
            SynthesisRequest(f"文本-{index}", "voice", "+0%", "+0Hz", "+0%", DEFAULT_PROVIDER_CONTRACT_VERSION, 5, None)
            for index in range(16)
        ]
        adapter = FakeProviderAdapter(
            outcomes=[RawAudioResult(str(index).encode(), "audio/wav") for index in range(16)]
        )
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(adapter.synthesize, requests))
        self.assertEqual(len(adapter.requests), 16)
        self.assertEqual(len({request.text for request in adapter.requests}), 16)
        self.assertEqual({result.bytes for result in results}, {str(index).encode() for index in range(16)})


class SpeechUnitPlannerTests(unittest.TestCase):
    def test_full_track_planner_preserves_scene_paragraphs_in_one_request(self) -> None:
        cues = [cue(1, "第一幕第一句。"), cue(2, "第一幕第二句。"), cue(3, "第二幕一句。")]
        scenes = [
            {"sceneId": "scene-01", "sourceCueRange": [1, 2]},
            {"sceneId": "scene-02", "sourceCueRange": [3, 3]},
        ]
        units = plan_full_track_unit(cues, scenes)
        self.assertEqual(len(units), 1)
        self.assertEqual(units[0]["speechText"], "第一幕第一句。第一幕第二句。\n\n第二幕一句。")
        self.assertEqual(units[0]["sourceCueRange"], [1, 3])
        self.assertEqual(units[0]["sceneCueRanges"], scenes)

    def test_sentence_end_secondary_break_short_merge_and_punctuation_only(self) -> None:
        cues = [
            cue(1, "第一句很短。第二句也不长！"),
            cue(2, "短句"),
            cue(3, "！！！"),
            cue(4, "这一句包含逗号，后半句用于测试次级断点和稳定拆分。"),
        ]
        units = plan_speech_units(cues, segmentation=SEGMENTATION, voice="voice")
        combined = "".join(unit["speechText"] for unit in units)
        self.assertEqual(combined, "第一句很短。第二句也不长！短句！！！这一句包含逗号，后半句用于测试次级断点和稳定拆分。")
        self.assertTrue(any(unit["speechText"] == "第一句很短。" for unit in units))
        self.assertTrue(any("短句！！！" in unit["speechText"] for unit in units))
        self.assertTrue(all(unit["codePointCount"] <= SEGMENTATION["maxCodePoints"] for unit in units))
        self.assertEqual([unit["index"] for unit in units], list(range(1, len(units) + 1)))
        self.assertEqual({ordinal for unit in units for ordinal in unit["sourceOrdinals"]}, {1, 2, 3, 4})

    def test_long_sentence_hard_splits_by_unicode_code_point_including_emoji(self) -> None:
        text = "甲乙丙丁😀戊己庚辛壬癸子丑寅卯辰巳午未申酉戌亥"
        segmentation = {
            "contractVersion": "speech-unit-v1",
            "minCodePoints": 4,
            "targetCodePoints": 7,
            "maxCodePoints": 8,
        }
        units = plan_speech_units([cue(1, text)], segmentation=segmentation)
        self.assertGreater(len(units), 1)
        self.assertEqual("".join(unit["speechText"] for unit in units), text)
        self.assertEqual(sum(unit["codePointCount"] for unit in units), len(text))
        self.assertTrue(all(unit["sourceCueRange"] == [1, 1] for unit in units))
        parts = [tuple((part["sourceOrdinal"], part["partIndex"])) for unit in units for part in unit["sourceParts"]]
        self.assertEqual(len(parts), len(set(parts)))

    def test_units_never_cross_scene_and_all_cues_are_contiguously_covered(self) -> None:
        cues = [cue(1, "第一幕短句"), cue(2, "仍在第一幕"), cue(3, "第二幕开始"), cue(4, "第二幕结束")]
        scenes = [
            {"sceneId": "scene-01", "sourceCueRange": [1, 2]},
            {"sceneId": "scene-02", "sourceCueRange": [3, 4]},
        ]
        units = plan_speech_units(cues, scenes, segmentation=SEGMENTATION)
        self.assertEqual({unit["sceneId"] for unit in units}, {"scene-01", "scene-02"})
        self.assertTrue(all(unit["sourceCueRange"][1] <= 2 for unit in units if unit["sceneId"] == "scene-01"))
        self.assertTrue(all(unit["sourceCueRange"][0] >= 3 for unit in units if unit["sceneId"] == "scene-02"))
        with self.assertRaisesRegex(VoiceoverValidationError, "不连续"):
            plan_speech_units(cues, [{"sceneId": "bad", "sourceCueRange": [2, 4]}])

    def test_identical_input_produces_identical_units_and_hashes(self) -> None:
        cues = [cue(1, "确定性测试。"), cue(2, "相同输入必须有相同哈希。")]
        first = plan_speech_units(cues, segmentation=SEGMENTATION, voice="voice")
        second = plan_speech_units(copy.deepcopy(cues), segmentation=copy.deepcopy(SEGMENTATION), voice="voice")
        self.assertEqual(first, second)


class IdentityAndPlanTests(unittest.TestCase):
    def test_normalized_units_are_explicit_and_stable(self) -> None:
        self.assertEqual(normalize_rate(0), "+0%")
        self.assertEqual(normalize_rate("+00%"), "+0%")
        self.assertEqual(normalize_pitch("default"), "+0Hz")
        self.assertEqual(normalize_volume(-10), "-10%")
        with self.assertRaises(VoiceoverValidationError):
            normalize_rate("0")

    def test_voice_plan_freezes_required_contract_and_audit_hash(self) -> None:
        cues = [cue(1, "计划测试")]
        plan = make_plan(cues)
        self.assertEqual(plan["provider"]["contractVersion"], "edge-tts-python-7.2.8-v1")
        self.assertEqual(plan["selection"]["rate"], "+0%")
        self.assertEqual(plan["selection"]["pitch"], "+0Hz")
        self.assertEqual(plan["selection"]["volume"], "+0%")
        self.assertEqual(plan["timingPolicy"], {"mode": "audio-authoritative", "durationReviewThresholdRatio": 0.10})
        self.assertEqual(validate_voice_plan(plan), plan)
        self.assertEqual(voice_plan_audit_hash(plan), voice_plan_audit_hash(copy.deepcopy(plan)))

    def test_timing_only_change_keeps_synthesis_identity_but_changes_timing_and_audit(self) -> None:
        first_cues = [cue(1, "相同朗读文本", 0, 1000), cue(2, "第二句", 1000, 2000)]
        timed_cues = [cue(1, "相同朗读文本", 100, 1400), cue(2, "第二句", 1400, 2700)]
        first_plan = make_plan(first_cues, source_srt_sha256="a" * 64)
        timed_plan = make_plan(timed_cues, source_srt_sha256="b" * 64)
        first_units = bind_synthesis_identities(plan_speech_units(first_cues, segmentation=SEGMENTATION), first_plan)
        timed_units = bind_synthesis_identities(plan_speech_units(timed_cues, segmentation=SEGMENTATION), timed_plan)
        self.assertEqual(
            [unit["voiceSynthesisIdentityHash"] for unit in first_units],
            [unit["voiceSynthesisIdentityHash"] for unit in timed_units],
        )
        self.assertNotEqual(
            [unit["sourceTimingIdentityHash"] for unit in first_units],
            [unit["sourceTimingIdentityHash"] for unit in timed_units],
        )
        self.assertNotEqual(voice_plan_audit_hash(first_plan), voice_plan_audit_hash(timed_plan))

    def test_voice_rate_text_scene_and_provider_contract_change_synthesis_identity(self) -> None:
        cues = [cue(1, "第一句"), cue(2, "第二句")]
        base = make_plan(cues)
        units = plan_speech_units(cues, segmentation=SEGMENTATION)
        base_hashes = [unit["voiceSynthesisIdentityHash"] for unit in bind_synthesis_identities(units, base)]
        variants = [
            make_plan(cues, voice="another-voice"),
            make_plan(cues, rate=10),
            make_plan(cues, provider_contract_version="edge-contract-v2"),
        ]
        for variant in variants:
            self.assertNotEqual(
                base_hashes,
                [unit["voiceSynthesisIdentityHash"] for unit in bind_synthesis_identities(units, variant)],
            )
        changed_text = [cue(1, "第一句已改变"), cue(2, "第二句")]
        changed_plan = make_plan(changed_text, source_srt_sha256="c" * 64)
        changed_units = plan_speech_units(changed_text, segmentation=SEGMENTATION)
        self.assertNotEqual(
            base_hashes,
            [unit["voiceSynthesisIdentityHash"] for unit in bind_synthesis_identities(changed_units, changed_plan)],
        )


class VoiceManifestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cues = [cue(1, "第一段旁白。"), cue(2, "第二段旁白。")]
        self.plan = make_plan(self.cues)
        self.units = plan_speech_units(self.cues, segmentation=SEGMENTATION)

    def test_new_manifest_is_pending_and_never_auto_approves(self) -> None:
        manifest = create_voice_manifest(
            project_id=self.plan["projectId"],
            voice_plan=self.plan,
            speech_units=self.units,
            timestamp="2026-08-14T00:00:00Z",
        )
        self.assertTrue(all(segment["status"] == "pending" for segment in manifest["segments"]))
        self.assertNotIn("sample", manifest)
        self.assertFalse(manifest["fullApproval"]["approved"])
        self.assertNotIn("review", manifest)
        self.assertNotIn("reviewIdentityHash", manifest["fullApproval"])
        self.assertTrue(all(segment["relativePath"].startswith("audio/segments/unit-") for segment in manifest["segments"]))
        self.assertEqual(validate_voice_manifest(manifest, voice_plan=self.plan), manifest)

    def test_manifest_accepts_only_frozen_segment_states(self) -> None:
        manifest = create_voice_manifest(
            project_id=self.plan["projectId"],
            voice_plan=self.plan,
            speech_units=self.units,
            timestamp="2026-08-14T00:00:00Z",
        )
        for status in ("pending", "requesting", "normalizing", "failed", "cancelled"):
            candidate = copy.deepcopy(manifest)
            candidate["segments"][0]["status"] = status
            validate_voice_manifest(candidate)
        manifest["segments"][0]["status"] = "complete"
        with self.assertRaisesRegex(VoiceoverValidationError, "status"):
            validate_voice_manifest(manifest)

    def test_technical_validated_status_does_not_grant_approval(self) -> None:
        manifest = create_voice_manifest(
            project_id=self.plan["projectId"],
            voice_plan=self.plan,
            speech_units=self.units,
            timestamp="2026-08-14T00:00:00Z",
        )
        segment = manifest["segments"][0]
        segment.update(
            {
                "status": "validated",
                "audioMime": "audio/wav",
                "audioCodec": "pcm_s16le",
                "sampleRate": 24000,
                "channels": 1,
                "bytes": 1024,
                "durationMs": 250,
                "sha256": "d" * 64,
            }
        )
        validated = validate_voice_manifest(manifest)
        self.assertNotIn("sample", validated)
        self.assertFalse(validated["fullApproval"]["approved"])

    def test_stale_voice_plan_audit_hash_is_rejected_without_mutation(self) -> None:
        manifest = create_voice_manifest(
            project_id=self.plan["projectId"],
            voice_plan=self.plan,
            speech_units=self.units,
            timestamp="2026-08-14T00:00:00Z",
        )
        stale = copy.deepcopy(manifest)
        stale["voicePlan"]["voicePlanAuditHash"] = "0" * 64
        snapshot = copy.deepcopy(stale)
        with self.assertRaisesRegex(VoiceoverValidationError, "current plan"):
            validate_voice_manifest(stale, voice_plan=self.plan)
        self.assertEqual(stale, snapshot)

    def test_example_provider_config_is_secret_free_and_matches_contract(self) -> None:
        config = json.loads((ROOT / "config" / "voice-providers.example.json").read_text(encoding="utf-8"))
        provider = config["providers"]["edge-tts"]
        self.assertEqual(provider["contractVersion"], DEFAULT_PROVIDER_CONTRACT_VERSION)
        self.assertEqual(provider["rate"], "+0%")
        serialized = json.dumps(config).lower()
        for forbidden in ("apikey", "api_key", "token", "cookie", "secret", "baseurl"):
            self.assertNotIn(forbidden, serialized)


if __name__ == "__main__":
    unittest.main()
