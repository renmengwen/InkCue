from __future__ import annotations

import base64
import json
import socket
import tempfile
import unittest
import urllib.error
from pathlib import Path

from scripts.doubao_adapter import (
    DoubaoAdapter,
)
from scripts.voice_provider_config import (
    VoiceProviderConfigError,
    active_provider_id,
    load_voice_provider_config,
)
from scripts.voiceover import (
    DOUBAO_ENDPOINT,
    DOUBAO_MODEL,
    DOUBAO_PROMPT_SCHEMA_VERSION,
    DOUBAO_PROMPT_SPEC_KIND,
    DOUBAO_PROMPT_VOICE_ID,
    PermanentProviderError,
    RetryableProviderError,
    SynthesisRequest,
    build_voice_plan,
)


class _Headers:
    def __init__(self, values: dict[str, str] | None = None) -> None:
        self.values = {key.lower(): value for key, value in (values or {}).items()}

    def get(self, name: str, default=None):
        return self.values.get(name.lower(), default)


class _Response:
    def __init__(self, payload: dict, headers: dict[str, str] | None = None):
        self.payload = json.dumps(payload).encode("utf-8")
        self.headers = _Headers(headers)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self.payload


def request(
    text: str = "霓虹映在雨后的街道上。",
    rate: str = "+10%",
    pitch: str = "+0Hz",
    volume: str = "+10%",
) -> SynthesisRequest:
    return SynthesisRequest(
        text,
        DOUBAO_PROMPT_VOICE_ID,
        rate,
        pitch,
        volume,
        5,
        None,
    )


class DoubaoAdapterTests(unittest.TestCase):
    def test_builds_seed_audio_request_and_decodes_base64_wav(self) -> None:
        seen = {}

        def opener(req, timeout):
            seen["headers"] = {
                key.lower(): value for key, value in req.header_items()
            }
            seen["timeout"] = timeout
            seen["payload"] = json.loads(req.data.decode("utf-8"))
            return _Response(
                {"code": 0, "message": "success", "audio": base64.b64encode(b"RIFF").decode("ascii")},
                {"X-Tt-Logid": "provider-log-secret"},
            )

        result = DoubaoAdapter(
            api_key="top-secret",
            opener=opener,
            queue_interval_seconds=0,
            request_id_factory=lambda: "client-request-id",
        ).synthesize(request())

        self.assertEqual(result.bytes, b"RIFF")
        self.assertEqual(result.declaredFormat, "audio/wav")
        self.assertEqual(result.providerRequestId, "sha256:7eb66142094e7485")
        self.assertEqual(seen["headers"]["x-api-key"], "top-secret")
        self.assertEqual(seen["headers"]["x-api-request-id"], "client-request-id")
        self.assertEqual(seen["timeout"], 5)
        self.assertEqual(seen["payload"]["model"], "seed-audio-1.0")
        self.assertNotIn("references", seen["payload"])
        self.assertEqual(seen["payload"]["audio_config"]["format"], "wav")
        self.assertEqual(seen["payload"]["audio_config"]["sample_rate"], 24000)
        self.assertEqual(seen["payload"]["audio_config"]["speech_rate"], 10)
        self.assertEqual(seen["payload"]["audio_config"]["loudness_rate"], 10)
        self.assertEqual(seen["payload"]["audio_config"]["pitch_rate"], 0)
        self.assertTrue(seen["payload"]["audio_config"]["enable_subtitle"])

    def test_accepts_success_response_without_business_code(self) -> None:
        result = DoubaoAdapter(
            api_key="top-secret",
            opener=lambda *_args, **_kwargs: _Response(
                {"audio": base64.b64encode(b"RIFF").decode("ascii")}
            ),
            queue_interval_seconds=0,
        ).synthesize(request())

        self.assertEqual(result.bytes, b"RIFF")
        self.assertEqual(result.declaredFormat, "audio/wav")

    def test_provider_error_does_not_echo_key_or_audio_url(self) -> None:
        def opener(_req, **_kwargs):
            return _Response(
                {
                    "code": 4001,
                    "message": "invalid speaker",
                    "url": "https://temporary.example/audio?token=secret",
                }
            )

        with self.assertRaises(PermanentProviderError) as raised:
            DoubaoAdapter(
                api_key="top-secret", opener=opener, queue_interval_seconds=0
            ).synthesize(request())
        message = str(raised.exception)
        self.assertNotIn("top-secret", message)
        self.assertNotIn("temporary.example", message)

    def test_429_and_connection_failures_are_bounded_retryable(self) -> None:
        calls = []

        def limited(_req, **_kwargs):
            calls.append(1)
            raise urllib.error.HTTPError(
                "https://openspeech.bytedance.com/api/v3/tts/create",
                429,
                "busy",
                {},
                None,
            )

        with self.assertRaises(RetryableProviderError):
            DoubaoAdapter(
                api_key="top-secret",
                opener=limited,
                max_attempts=2,
                queue_interval_seconds=0,
            ).synthesize(request())
        self.assertEqual(len(calls), 2)

        with self.assertRaises(RetryableProviderError):
            DoubaoAdapter(
                api_key="top-secret",
                opener=lambda *_args, **_kwargs: (_ for _ in ()).throw(socket.timeout()),
                max_attempts=1,
                queue_interval_seconds=0,
            ).synthesize(request())

    def test_invalid_ranges_and_response_audio_are_permanent(self) -> None:
        unused = lambda *_args, **_kwargs: None
        with self.assertRaisesRegex(PermanentProviderError, "pitch_rate"):
            DoubaoAdapter(
                api_key="top-secret", opener=unused, queue_interval_seconds=0
            ).synthesize(request(pitch="+13Hz"))
        with self.assertRaisesRegex(PermanentProviderError, "Base64"):
            DoubaoAdapter(
                api_key="top-secret",
                opener=lambda *_args, **_kwargs: _Response(
                    {"code": 0, "message": "success", "audio": "not-base64***"}
                ),
                queue_interval_seconds=0,
            ).synthesize(request())


class DoubaoConfigurationAndPlanTests(unittest.TestCase):
    def test_local_configuration_requires_secret_and_normalizes_selection(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = root / "config"
            config.mkdir()
            value = {
                "schemaVersion": 1,
                "activeProvider": "Doubao",
                "providers": {
                    "doubao": {
                        "protocol": "Doubao",
                        "apiKey": "local-secret",
                        "language": "zh-CN",
                        "rate": 10,
                        "pitch": 0,
                        "volume": 0,
                        "outputFormat": "audio-24khz-mono-wav",
                        "requestTimeoutSeconds": 60,
                        "model": "seed-audio-1.0",
                    }
                },
            }
            (config / "voice-providers.local.json").write_text(
                json.dumps(value), encoding="utf-8"
            )
            self.assertEqual(active_provider_id(root=root), "doubao")
            loaded = load_voice_provider_config(root=root)
            self.assertEqual(loaded["id"], "doubao")
            self.assertEqual(loaded["rate"], "+10%")
            self.assertEqual(loaded["apiKey"], "local-secret")

            del value["providers"]["doubao"]["apiKey"]
            (config / "voice-providers.local.json").write_text(
                json.dumps(value), encoding="utf-8"
            )
            with self.assertRaisesRegex(VoiceProviderConfigError, "apiKey"):
                load_voice_provider_config(root=root)

    def test_doubao_voice_plan_is_a_first_class_audio_mode(self) -> None:
        plan = build_voice_plan(
            project_id="p1",
            source_srt_sha256="a" * 64,
            cues=[
                {
                    "sourceOrdinal": 1,
                    "startMs": 0,
                    "endMs": 1000,
                    "text": "测试文本。",
                }
            ],
            provider_id="doubao",
            protocol="Doubao",
            voice=DOUBAO_PROMPT_VOICE_ID,
            provider_options={
                "model": DOUBAO_MODEL,
                "endpoint": DOUBAO_ENDPOINT,
                "promptSpec": {
                    "schemaVersion": DOUBAO_PROMPT_SCHEMA_VERSION,
                    "kind": DOUBAO_PROMPT_SPEC_KIND,
                },
                "maxTextPromptCharacters": 3000,
                "maxAudioDurationSeconds": 120,
                "nativeWordSubtitlesRequired": True,
                "voiceControlMode": "text_prompt",
                "timeControlMode": "scene_windows",
            },
        )
        self.assertEqual(plan["mode"], "doubao")
        self.assertEqual(plan["provider"]["protocol"], "Doubao")
        self.assertEqual(plan["provider"]["options"]["model"], "seed-audio-1.0")


if __name__ == "__main__":
    unittest.main()
