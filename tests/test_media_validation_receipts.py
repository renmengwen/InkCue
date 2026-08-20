from __future__ import annotations

import copy
import hashlib
import io
import json
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from PIL import Image


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import generate_voiceover as voice  # noqa: E402
import image_generation  # noqa: E402
import media_validation  # noqa: E402
import validation_receipts  # noqa: E402
from audio_normalization import CANONICAL_AUDIO_CONTRACT_VERSION, CanonicalAudioResult  # noqa: E402


class MediaReceiptReuseTests(unittest.TestCase):
    def test_image_candidate_deep_receipt_reuses_binding_without_second_decode(self) -> None:
        with tempfile.TemporaryDirectory(prefix="whiteboard-image-receipt-") as temporary:
            root = Path(temporary)
            attempt_root = root / ".work" / "run" / "scene-01" / "a0001"
            candidate_path = attempt_root / "candidate.png"
            source = io.BytesIO()
            Image.new("RGB", (512, 512), (10, 20, 30)).save(source, format="PNG")
            candidate = image_generation.normalize_image_candidate(
                source.getvalue(),
                candidate_path,
                attempt_root,
                "scene-01",
                attempt_id="scene-01-attempt-0001",
                formal_file="scenes/scene-01.png",
                input_identity_sha256="a" * 64,
                source="b64_json",
                provider_attempts=1,
            )
            self.assertEqual(
                candidate.validator_receipt["contractVersion"],
                validation_receipts.CANDIDATE_RECEIPT_CONTRACT_VERSION,
            )

            with mock.patch.object(
                image_generation,
                "_load_pillow",
                side_effect=AssertionError("binding 不得重复完整解码"),
            ):
                rebound = image_generation.load_image_candidate(
                    candidate_path,
                    expected_attempt_root=attempt_root,
                    expected_attempt_id="scene-01-attempt-0001",
                    expected_scene_id="scene-01",
                    expected_input_identity_sha256="a" * 64,
                    expected_formal_file="scenes/scene-01.png",
                )
            self.assertEqual(rebound.sha256, candidate.sha256)
            self.assertEqual(rebound.validator_receipt, candidate.validator_receipt)

            legacy = {
                "contractVersion": image_generation.CANDIDATE_RECEIPT_VERSION,
                **candidate.validator_receipt["evidence"],
            }
            candidate.receipt_path.write_text(json.dumps(legacy), encoding="utf-8")
            original_load_pillow = image_generation._load_pillow
            with mock.patch.object(
                image_generation, "_load_pillow", wraps=original_load_pillow
            ) as deep:
                upgraded = image_generation.load_image_candidate(
                    candidate_path,
                    expected_attempt_root=attempt_root,
                    expected_attempt_id="scene-01-attempt-0001",
                    expected_scene_id="scene-01",
                    expected_input_identity_sha256="a" * 64,
                    expected_formal_file="scenes/scene-01.png",
                )
            self.assertEqual(deep.call_count, 1)
            self.assertEqual(
                upgraded.validator_receipt["contractVersion"],
                validation_receipts.CANDIDATE_RECEIPT_CONTRACT_VERSION,
            )

            formal = root / "scenes" / "scene-01.png"
            formal.parent.mkdir(parents=True)
            formal.write_bytes(b"old-formal")
            invalid_receipt = copy.deepcopy(upgraded.validator_receipt)
            invalid_receipt["receiptSha256"] = "0" * 64
            with self.assertRaises(image_generation.ImageValidationError):
                image_generation.publish_image_candidate(
                    replace(upgraded, validator_receipt=invalid_receipt),
                    formal,
                    overwrite=True,
                )
            self.assertEqual(formal.read_bytes(), b"old-formal")

            candidate_path.write_bytes(candidate_path.read_bytes() + b"changed")
            with self.assertRaises(image_generation.ImageValidationError), mock.patch.object(
                image_generation,
                "_load_pillow",
                side_effect=AssertionError("binding 失败不得降级 deep"),
            ):
                image_generation.load_image_candidate(
                    candidate_path,
                    expected_attempt_root=attempt_root,
                    expected_attempt_id="scene-01-attempt-0001",
                    expected_scene_id="scene-01",
                    expected_input_identity_sha256="a" * 64,
                    expected_formal_file="scenes/scene-01.png",
                )

    def test_wav_candidate_receipt_reuses_binding_and_old_validator_requires_deep(self) -> None:
        with tempfile.TemporaryDirectory(prefix="whiteboard-wav-receipt-") as temporary:
            path = Path(temporary) / "candidate.wav"
            path.write_bytes(b"RIFF\x00\x00\x00\x00WAVEfixture")
            result = CanonicalAudioResult(
                path=path,
                contractVersion=CANONICAL_AUDIO_CONTRACT_VERSION,
                codec="pcm_s16le",
                sampleRate=24000,
                channels=1,
                durationMs=400,
                bytes=path.stat().st_size,
                sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
            )
            receipt = voice._canonical_validator_receipt(result)
            media = {
                "contractVersion": result.contractVersion,
                "audioCodec": result.codec,
                "sampleRate": result.sampleRate,
                "channels": result.channels,
                "durationMs": result.durationMs,
                "bytes": result.bytes,
                "sha256": result.sha256,
            }
            rebound = voice._canonical_result_from_binding(path, media, receipt)
            self.assertIsNotNone(rebound)
            self.assertEqual(rebound.sha256, result.sha256)  # type: ignore[union-attr]

            old_validator = validation_receipts.build_candidate_receipt(
                candidate_sha256=result.sha256,
                candidate_bytes=result.bytes,
                decoded=True,
                format="WAV",
                validator_contract="canonical-wav-validator-old",
                evidence=receipt["evidence"],
            )
            self.assertIsNone(voice._canonical_result_from_binding(path, media, old_validator))

            stale_evidence = copy.deepcopy(receipt)
            stale_evidence["evidence"]["durationMs"] += 1
            stale_evidence["receiptSha256"] = validation_receipts.receipt_sha256(
                stale_evidence
            )
            with self.assertRaises(voice.ApprovalGateError):
                voice._canonical_result_from_binding(path, media, stale_evidence)

            path.write_bytes(path.read_bytes() + b"changed")
            with self.assertRaises(voice.ApprovalGateError):
                voice._canonical_result_from_binding(path, media, receipt)

    def test_mp4_candidate_deep_then_binding_and_contract_refresh(self) -> None:
        probe = {
            "bytes": 3,
            "sha256": hashlib.sha256(b"mp4").hexdigest(),
            "durationMs": 100,
            "formatName": "mov,mp4",
            "streams": {
                "video": [{
                    "index": 0,
                    "codec": "h264",
                    "width": 1920,
                    "height": 1080,
                    "pixelFormat": "yuv420p",
                    "fps": {"numerator": 60, "denominator": 1, "value": 60.0},
                    "frameCount": 6,
                    "containerNbFrames": 6,
                    "durationMs": 100,
                }],
                "audio": [],
                "subtitle": [],
                "other": [],
            },
        }
        profile = {
            "width": 1920,
            "height": 1080,
            "fps": 60,
            "pixelFormat": "yuv420p",
            "videoCodec": "h264",
            "frameRounding": "cumulative-ceil-v1",
        }
        decode = {
            "decodedFrameCount": 6,
            "frameCountEvidence": media_validation.FRAME_COUNT_EVIDENCE,
            "fullDecode": {"passed": True, "progressEnd": True},
        }
        with mock.patch.object(media_validation, "probe_media", return_value=probe), mock.patch.object(
            media_validation, "full_decode", return_value=decode
        ) as deep:
            first = media_validation.validate_video(
                "candidate.mp4", render_profile=profile, expected_frame_count=6
            )
            receipt = first["validation"]["deepReceipt"]
            second = media_validation.validate_video(
                "candidate.mp4",
                render_profile=profile,
                expected_frame_count=6,
                deep_receipt=receipt,
            )
        self.assertEqual(deep.call_count, 1)
        self.assertEqual(second["validation"]["validationMode"], "binding")

        stale = copy.deepcopy(receipt)
        old_common = validation_receipts.build_candidate_receipt(
            candidate_sha256=probe["sha256"],
            candidate_bytes=probe["bytes"],
            decoded=True,
            format="MP4",
            validator_contract="media-validation-old",
            evidence=receipt["candidateReceipt"]["evidence"],
        )
        stale["candidateReceipt"] = old_common
        with mock.patch.object(media_validation, "probe_media", return_value=probe), mock.patch.object(
            media_validation, "full_decode", return_value=decode
        ) as refreshed:
            result = media_validation.validate_video(
                "candidate.mp4",
                render_profile=profile,
                expected_frame_count=6,
                deep_receipt=stale,
            )
        self.assertEqual(refreshed.call_count, 1)
        self.assertEqual(result["validation"]["validationMode"], "deep")
        with mock.patch.object(media_validation, "full_decode") as forbidden:
            with self.assertRaises(media_validation.MediaValidationError):
                media_validation.bind_validated_video(
                    "candidate.mp4",
                    render_profile=profile,
                    expected_frame_count=6,
                    deep_receipt={"contractVersion": media_validation.DEEP_MEDIA_RECEIPT_CONTRACT_VERSION},
                )
        self.assertEqual(forbidden.call_count, 0)


if __name__ == "__main__":
    unittest.main()
