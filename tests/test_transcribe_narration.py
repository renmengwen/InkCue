from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from scripts import transcribe_narration as runner
from scripts import prepare_env
from scripts.audio_normalization import CanonicalAudioResult


class FakeModel:
    def __init__(self, result: object) -> None:
        self.result = result
        self.calls: list[dict[str, object]] = []

    def generate(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        return self.result


class NarrationTranscriptionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.audio = self.root / "narration.wav"
        self.audio.write_bytes(b"RIFF-fake-WAVE")
        self.output = self.root / ".work" / "voice-asr-run"
        self.model_paths: dict[str, Path] = {}
        for alias in runner.MODEL_IDS:
            path = self.root / "models" / alias
            path.mkdir(parents=True)
            (path / "model.bin").write_bytes(b"fixture")
            self.model_paths[alias] = path

    def tearDown(self) -> None:
        self.temp.cleanup()

    def prepared_audio(self, output_dir: Path, duration_ms: int = 4000) -> runner.PreparedAudio:
        asr = output_dir / "asr-input.wav"
        asr.write_bytes(b"RIFF-asr-WAVE")
        source = CanonicalAudioResult(
            path=self.audio.resolve(),
            contractVersion="fixture-canonical-v1",
            codec="pcm_s16le",
            sampleRate=24000,
            channels=1,
            durationMs=duration_ms,
            bytes=self.audio.stat().st_size,
            sha256="a" * 64,
        )
        return runner.PreparedAudio(
            source=source,
            asr_path=asr.resolve(),
            asr_duration_ms=duration_ms,
            asr_bytes=asr.stat().st_size,
            asr_sha256="b" * 64,
        )

    def run_with_result(self, result: object, *, duration_ms: int = 4000) -> tuple[dict, FakeModel, dict]:
        fake_model = FakeModel(result)
        factory_kwargs: dict[str, object] = {}

        def factory(**kwargs: object) -> FakeModel:
            factory_kwargs.update(kwargs)
            return fake_model

        with mock.patch.object(
            runner,
            "_prepare_asr_audio",
            side_effect=lambda _audio, output, **_kwargs: self.prepared_audio(output, duration_ms),
        ):
            payload = runner.transcribe_narration(
                self.audio,
                self.output,
                model_factory=factory,
                model_paths=self.model_paths,
            )
        return payload, fake_model, factory_kwargs

    def test_full_track_uses_fixed_local_models_and_publishes_evidence(self) -> None:
        payload, model, factory_kwargs = self.run_with_result(
            [
                {
                    "text": "第一句。第二句！",
                    "sentence_info": [
                        {"start": 120, "end": 1600, "text": "第一句。"},
                        {"start": 1900, "end": 3800, "text": "第二句！"},
                    ],
                }
            ]
        )

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["durationMs"], 4000)
        self.assertEqual(payload["sentenceCount"], 2)
        self.assertEqual(payload["timingValidation"]["gaps"], 1)
        self.assertEqual(payload["timingValidation"]["trailingRoomMs"], 200)
        self.assertEqual(factory_kwargs["device"], "cpu")
        self.assertTrue(factory_kwargs["disable_update"])
        self.assertEqual(factory_kwargs["model"], str(self.model_paths["paraformer-zh"].resolve()))
        self.assertEqual(factory_kwargs["vad_model"], str(self.model_paths["fsmn-vad"].resolve()))
        self.assertEqual(factory_kwargs["punc_model"], str(self.model_paths["ct-punc"].resolve()))
        self.assertEqual(
            model.calls,
            [
                {
                    "input": str((self.output / "asr-input.wav").resolve()),
                    "batch_size": 1,
                    "sentence_timestamp": True,
                }
            ],
        )
        raw_srt = Path(payload["rawSrtPath"])
        self.assertTrue(raw_srt.is_file())
        self.assertIn("00:00:00,120 --> 00:00:01,600", raw_srt.read_text(encoding="utf-8"))
        raw_json = json.loads(Path(payload["rawJsonPath"]).read_text(encoding="utf-8"))
        self.assertEqual(len(raw_json["sentenceInfo"]), 2)
        receipt = json.loads(Path(payload["receiptPath"]).read_text(encoding="utf-8"))
        self.assertEqual(receipt["contractVersion"], runner.CONTRACT_VERSION)
        self.assertEqual(receipt["model"]["modelContract"], runner.MODEL_CONTRACT)
        self.assertNotIn(str(self.root), json.dumps(receipt, ensure_ascii=False))

    def test_default_loader_consumes_skill_receipt_without_alias_download(self) -> None:
        cache_root = self.root / "runtime" / "cache" / "funasr-models"
        models = []
        for alias, model_id in runner.MODEL_IDS.items():
            model_path = cache_root / "snapshots" / alias
            model_path.mkdir(parents=True)
            (model_path / "model.bin").write_bytes(b"fixture")
            models.append(
                {
                    "alias": alias,
                    "modelId": model_id,
                    "requestedRevision": "master",
                    "path": str(model_path.resolve()),
                }
            )
        receipt_path = cache_root / "narration-asr-models.json"
        receipt_path.write_text(
            json.dumps(
                {
                    "schemaVersion": 1,
                    "contract": runner.MODEL_CONTRACT,
                    "cacheRoot": str(cache_root.resolve()),
                    "models": models,
                }
            ),
            encoding="utf-8",
        )
        with mock.patch.dict(sys.modules, {"prepare_env": prepare_env}), mock.patch.object(
            prepare_env,
            "narration_asr_paths",
            return_value=(cache_root.resolve(), receipt_path.resolve()),
        ):
            loaded, resolved_receipt = runner._load_model_paths(
                model_paths=None,
                model_receipt=None,
            )
        self.assertEqual(resolved_receipt, receipt_path.resolve())
        self.assertEqual(set(loaded), set(runner.MODEL_IDS))
        self.assertTrue(all(path.is_absolute() for path in loaded.values()))

    def test_rejects_overlap_without_character_timing_fallback(self) -> None:
        with self.assertRaisesRegex(runner.NarrationTranscriptionError, "重叠或乱序"):
            self.run_with_result(
                [
                    {
                        "sentence_info": [
                            {"start": 100, "end": 1600, "text": "第一句。"},
                            {"start": 1500, "end": 2600, "text": "第二句。"},
                        ]
                    }
                ]
            )

    def test_rejects_sentence_past_measured_audio(self) -> None:
        with self.assertRaisesRegex(runner.NarrationTranscriptionError, "越过实测旁白时长"):
            self.run_with_result(
                [{"sentence_info": [{"start": 10, "end": 4001, "text": "越界。"}]}]
            )

    def test_rejects_empty_or_punctuation_only_sentence(self) -> None:
        with self.assertRaisesRegex(runner.NarrationTranscriptionError, "纯标点"):
            self.run_with_result(
                [{"sentence_info": [{"start": 10, "end": 1000, "text": "……"}]}]
            )

    def test_rejects_transcript_without_chinese_punctuation(self) -> None:
        with self.assertRaisesRegex(runner.NarrationTranscriptionError, "缺少中文标点"):
            self.run_with_result(
                [{"sentence_info": [{"start": 10, "end": 1000, "text": "没有标点"}]}]
            )

    def test_output_directory_must_be_new_and_inside_dot_work(self) -> None:
        existing = self.root / ".work" / "existing"
        existing.mkdir(parents=True)
        with self.assertRaisesRegex(runner.NarrationTranscriptionError, "尚不存在"):
            runner.transcribe_narration(
                self.audio,
                existing,
                model_factory=lambda **_kwargs: FakeModel([]),
                model_paths=self.model_paths,
            )
        with self.assertRaisesRegex(runner.NarrationTranscriptionError, "\.work"):
            runner.transcribe_narration(
                self.audio,
                self.root / "outside",
                model_factory=lambda **_kwargs: FakeModel([]),
                model_paths=self.model_paths,
            )

    def test_cli_stdout_is_one_json_document(self) -> None:
        expected = {
            "ok": True,
            "rawSrtPath": "C:/project/.work/run/transcript.raw.srt",
            "durationMs": 1234,
            "sentenceCount": 1,
            "timingValidation": {"overlaps": 0},
        }
        stdout = io.StringIO()
        with mock.patch.object(runner, "transcribe_narration", return_value=expected), redirect_stdout(stdout):
            code = runner.main([str(self.audio), str(self.output)])
        self.assertEqual(code, 0)
        lines = stdout.getvalue().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0]), expected)


if __name__ == "__main__":
    unittest.main()
