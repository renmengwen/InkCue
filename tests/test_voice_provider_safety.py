from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

import scripts.generate_voiceover as generate_voiceover
import scripts.voice_provider_config as provider_config
from scripts.minimax_adapter import MINIMAX_PROVIDER_CONTRACT_VERSION, MiniMaxAdapter
from scripts.project_workspace import sha256_file
from scripts.voice_provider_config import VoiceProviderConfigError, voice_provider_status
from scripts.voiceover import PermanentProviderError, SynthesisRequest, create_voice_manifest


ROOT = Path(__file__).resolve().parents[1]
SENTINEL = "SENTINEL-VOICE-SECRET-DO-NOT-LEAK"


class _Response:
    def __init__(self, payload: dict) -> None:
        self._payload = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self) -> bytes:
        return self._payload


class _Project:
    schema_version = 2
    voiceover_mode = "minimax"
    project_id = "provider-safety-fixture"

    def __init__(self, root: Path) -> None:
        self.root = root
        source = root / "source" / "source.srt"
        source.parent.mkdir(parents=True)
        source.write_text(
            "1\n00:00:00,000 --> 00:00:01,000\n脱敏测试旁白。\n",
            encoding="utf-8",
        )
        self.metadata = {
            "source": {
                "file": "source/source.srt",
                "sha256": sha256_file(source),
            }
        }
        self.timing_plan = {
            "scenes": [{"sceneId": "scene-01", "sourceCueRange": [1, 1]}]
        }

    def path(self, relative: str) -> Path:
        return self.root / relative


def _write_local(root: Path, *, rate: str = "+10%") -> None:
    path = root / "config" / "voice-providers.local.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "activeProvider": "MiniMax",
                "providers": {
                    "MiniMax": {
                        "protocol": "MiniMax",
                        "contractVersion": MINIMAX_PROVIDER_CONTRACT_VERSION,
                        "apiKey": SENTINEL,
                        "voice": "fixture-voice",
                        "language": "zh-CN",
                        "model": "speech-2.8-hd",
                        "rate": rate,
                        "pitch": "+0Hz",
                        "volume": "+10%",
                        "outputFormat": "audio-32khz-128kbitrate-mono-mp3",
                    }
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


class VoiceProviderSafetyTests(unittest.TestCase):
    def test_redacted_status_api_and_cli_have_strict_allowlist(self) -> None:
        with tempfile.TemporaryDirectory(prefix=".provider-safety-", dir=ROOT) as raw:
            root = Path(raw)
            _write_local(root)
            expected = {
                "provider": "minimax",
                "model": "speech-2.8-hd",
                "voice": "fixture-voice",
                "rate": "+10%",
                "credentialsConfigured": True,
            }
            self.assertEqual(voice_provider_status(root=root), expected)
            output = io.StringIO()
            errors = io.StringIO()
            with (
                mock.patch.object(provider_config, "_root", return_value=root),
                redirect_stdout(output),
                redirect_stderr(errors),
            ):
                self.assertEqual(provider_config.main(["status"]), 0)
            self.assertEqual(json.loads(output.getvalue()), expected)
            self.assertNotIn(SENTINEL, output.getvalue() + errors.getvalue())

    def test_status_validation_error_does_not_echo_secret_or_raw_config(self) -> None:
        with tempfile.TemporaryDirectory(prefix=".provider-safety-", dir=ROOT) as raw:
            root = Path(raw)
            _write_local(root, rate=SENTINEL)
            with self.assertRaises(VoiceProviderConfigError) as raised:
                voice_provider_status(root=root)
            message = str(raised.exception)
            self.assertNotIn(SENTINEL, message)
            self.assertNotIn("apiKey", message)

            local = root / "config" / "voice-providers.local.json"
            payload = json.loads(local.read_text(encoding="utf-8"))
            payload["activeProvider"] = SENTINEL
            local.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(VoiceProviderConfigError) as unknown:
                voice_provider_status(root=root)
            self.assertNotIn(SENTINEL, str(unknown.exception))

    def test_provider_free_text_never_reaches_exception(self) -> None:
        def opener(_request, **_kwargs):
            return _Response(
                {
                    "base_resp": {
                        "status_code": 1004,
                        "status_msg": f"invalid Authorization Bearer {SENTINEL}",
                    }
                }
            )

        request = SynthesisRequest(
            "脱敏测试。",
            "fixture-voice",
            "+0%",
            "+0Hz",
            "+0%",
            MINIMAX_PROVIDER_CONTRACT_VERSION,
            5,
            None,
        )
        with self.assertRaises(PermanentProviderError) as raised:
            MiniMaxAdapter(
                api_key=SENTINEL,
                opener=opener,
                queue_interval_seconds=0,
            ).synthesize(request)
        self.assertNotIn(SENTINEL, str(raised.exception))
        self.assertEqual(str(raised.exception), "MiniMax provider 返回业务错误")

    def test_secret_cannot_flow_to_plan_receipt_or_manifest(self) -> None:
        with tempfile.TemporaryDirectory(prefix=".provider-safety-", dir=ROOT) as raw:
            project = _Project(Path(raw))
            config = {
                "apiKey": SENTINEL,
                "voice": "fixture-voice",
                "language": "zh-CN",
                "model": "speech-2.8-hd",
                "rate": "+10%",
                "pitch": "+0Hz",
                "volume": "+10%",
                "outputFormat": "audio-32khz-128kbitrate-mono-mp3",
                "contractVersion": MINIMAX_PROVIDER_CONTRACT_VERSION,
            }
            plan, units = generate_voiceover._build_plan_and_units(
                project,
                voice="fixture-voice",
                rate=10,
                provider_id="minimax",
                provider_config=config,
            )
            manifest = create_voice_manifest(
                project_id=project.project_id,
                voice_plan=plan,
                speech_units=units,
            )
            receipt = generate_voiceover._provider_receipt(SENTINEL)
            serialized = json.dumps(
                {"plan": plan, "receipt": receipt, "manifest": manifest},
                ensure_ascii=False,
                sort_keys=True,
            )
            self.assertNotIn(SENTINEL, serialized)
            self.assertNotIn('"apiKey"', serialized)
            self.assertRegex(receipt["providerRequestIdHash"], r"^sha256:[0-9a-f]{16}$")


if __name__ == "__main__":
    unittest.main()
