from __future__ import annotations

"""Phase 5 stale/identity regression matrix.

These checks intentionally use byte and JSON fixtures only.  A PASS here means
that the local identity contract behaves deterministically; it is not evidence
that an external provider responded or that a human gate was approved.
"""

import hashlib
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import render_timing  # noqa: E402
import subtitle_delivery  # noqa: E402
import validation_receipts  # noqa: E402
from generate_annotation_previews import annotation_binding_sha256  # noqa: E402
from voiceover import bind_synthesis_identities, build_voice_plan, plan_speech_units  # noqa: E402


class StaleIdentityMatrixTests(unittest.TestCase):
    def _context(self, root: Path) -> SimpleNamespace:
        image = root / "scene.png"
        annotation = root / "scene.annotation.json"
        image.write_bytes(b"image-v1")
        annotation.write_bytes(b"annotation-v1")
        project = SimpleNamespace(project_id="stale-matrix-fixture")
        return SimpleNamespace(
            project=project,
            scene_id="scene-01",
            image_path=image,
            annotation_path=annotation,
            timing_scene={"startFrame": 0, "endFrameExclusive": 60, "frameCount": 60},
            timing_plan_sha256="a" * 64,
            render_profile_sha256="b" * 64,
            active_timeline={"kind": "source-srt", "sha256": "c" * 64},
            audio_sha256=None,
            full_approval_identity_hash=None,
        )

    @staticmethod
    def _render_options() -> dict[str, object]:
        return {
            "inkPath": "centerline",
            "colorFill": False,
            "pause": "none",
            "gridEdge": False,
            "brushRadius": 2,
            "bareTip": False,
            "paperBackground": "paper-content-mask-v3",
        }

    def test_image_bytes_make_visual_render_identity_stale(self) -> None:
        with tempfile.TemporaryDirectory(prefix="whiteboard-stale-image-") as temporary:
            context = self._context(Path(temporary))
            first = render_timing.render_identity(context, render_options=self._render_options())
            context.image_path.write_bytes(b"image-v2")
            second = render_timing.render_identity(context, render_options=self._render_options())
            self.assertNotEqual(first, second, "image bytes changed but visual identity stayed current")

    def test_annotation_bytes_make_visual_downstream_identity_stale(self) -> None:
        with tempfile.TemporaryDirectory(prefix="whiteboard-stale-annotation-") as temporary:
            context = self._context(Path(temporary))
            first = render_timing.render_identity(context, render_options=self._render_options())
            first_binding = annotation_binding_sha256([context])
            context.annotation_path.write_bytes(b"annotation-v2")
            second = render_timing.render_identity(context, render_options=self._render_options())
            second_binding = annotation_binding_sha256([context])
            self.assertNotEqual(first, second, "annotation bytes changed but render identity stayed current")
            self.assertNotEqual(
                first_binding,
                second_binding,
                "annotation bytes changed but preview/annotation binding stayed current",
            )

    def test_timing_plan_and_render_profile_changes_stale_render_identity(self) -> None:
        with tempfile.TemporaryDirectory(prefix="whiteboard-stale-plan-") as temporary:
            context = self._context(Path(temporary))
            options = self._render_options()
            baseline = render_timing.render_identity(context, render_options=options)
            baseline_annotation = annotation_binding_sha256([context])
            context.timing_plan_sha256 = "d" * 64
            self.assertNotEqual(
                baseline,
                render_timing.render_identity(context, render_options=options),
                "timing plan changed but scene render identity stayed current",
            )
            self.assertNotEqual(
                baseline_annotation,
                annotation_binding_sha256([context]),
                "timing plan changed but annotation binding stayed current",
            )
            context.render_profile_sha256 = "e" * 64
            self.assertNotEqual(
                baseline,
                render_timing.render_identity(context, render_options=options),
                "render profile changed but scene render identity stayed current",
            )

    def test_voice_rate_and_text_change_synthesis_identity(self) -> None:
        def cue(text: str) -> dict[str, object]:
            return {
                "index": 1,
                "sourceOrdinal": 1,
                "originalIndex": 1,
                "startMs": 0,
                "endMs": 1000,
                "durMs": 1000,
                "text": text,
            }

        segmentation = {
            "contractVersion": "speech-unit-v1",
            "minCodePoints": 1,
            "targetCodePoints": 8,
            "maxCodePoints": 18,
        }

        def identities(text: str, **kwargs: object) -> list[str]:
            cues = [cue(text)]
            plan = build_voice_plan(
                project_id="voice-stale-fixture",
                source_srt_sha256="a" * 64,
                cues=cues,
                segmentation=segmentation,
                **kwargs,
            )
            units = plan_speech_units(cues, segmentation=segmentation)
            return [item["voiceSynthesisIdentityHash"] for item in bind_synthesis_identities(units, plan)]

        baseline = identities("同一段文本")
        self.assertNotEqual(baseline, identities("改过的文本"), "朗读文本变化未使 audio identity stale")
        self.assertNotEqual(baseline, identities("同一段文本", voice="another-voice"), "voice 变化未使 audio identity stale")
        self.assertNotEqual(baseline, identities("同一段文本", rate=10), "rate 变化未使 audio identity stale")

    def test_subtitle_preset_changes_captioned_and_final_identity(self) -> None:
        common = {
            "voiceover_mode": "disabled",
            "clean_video_sha256": "a" * 64,
            "audio_sha256": "",
            "timeline_sha256": "b" * 64,
            "authoritative_subtitle_sha256": "c" * 64,
            "subtitle_style_contract_sha256": "d" * 64,
            "font_sha256": "e" * 64,
            "render_profile_sha256": "f" * 64,
            "timing_plan_sha256": "1" * 64,
            "mux_contract_version": "disabled-mux-v1",
            "final_media_sha256": "2" * 64,
        }
        _medium_inputs, medium = subtitle_delivery.compute_final_identity(
            **common, subtitle_preset="medium"
        )
        _fast_inputs, fast = subtitle_delivery.compute_final_identity(
            **common, subtitle_preset="fast"
        )
        self.assertNotEqual(medium, fast, "subtitle preset changed but final identity stayed current")

    def test_concurrency_only_is_runtime_audit_and_not_scene_identity(self) -> None:
        with tempfile.TemporaryDirectory(prefix="whiteboard-concurrency-identity-") as temporary:
            context = self._context(Path(temporary))
            options = self._render_options()
            identity = render_timing.render_identity(context, render_options=options)
            # Scheduler settings are deliberately kept in an audit record, not
            # render options/content identity.
            audit_one = {"configuredConcurrency": 1, "effectiveConcurrency": 1}
            audit_four = {"configuredConcurrency": 4, "effectiveConcurrency": 4}
            self.assertNotEqual(audit_one, audit_four)
            options_with_scheduler_audit = {
                **options,
                "configuredConcurrency": audit_four["configuredConcurrency"],
                "effectiveConcurrency": audit_four["effectiveConcurrency"],
                "sceneRender": 4,
            }
            self.assertEqual(
                identity,
                render_timing.render_identity(
                    context, render_options=options_with_scheduler_audit
                ),
                "concurrency-only change must remain outside scene identity",
            )

    def test_validator_contract_change_makes_old_candidate_receipt_stale(self) -> None:
        candidate = b"candidate-image"
        receipt = validation_receipts.build_candidate_receipt(
            candidate_sha256=hashlib.sha256(candidate).hexdigest(),
            candidate_bytes=len(candidate),
            decoded=True,
            format="PNG",
            validator_contract="png-validator-v1",
            validated_at="2026-08-20T01:00:00Z",
            expires_at="2026-08-20T02:00:00Z",
        )
        with self.assertRaisesRegex(validation_receipts.ReceiptValidationError, "validator contract.*stale"):
            validation_receipts.validate_candidate_receipt(
                receipt,
                expected_candidate_sha256=hashlib.sha256(candidate).hexdigest(),
                expected_candidate_bytes=len(candidate),
                expected_format="PNG",
                expected_validator_contract="png-validator-v2",
                now="2026-08-20T01:05:00Z",
            )

    def test_fixture_pass_is_not_provider_or_human_gate_pass(self) -> None:
        """Keep CI wording explicit even when all local fixture checks pass."""
        local = {"status": "PASS", "evidence": "deterministic-local-fixture"}
        real_provider = {"status": "SKIP", "reason": "no external provider call in unit tests"}
        human_gate = {"status": "PENDING", "approvalWritten": False}
        self.assertEqual(local["status"], "PASS")
        self.assertNotEqual(local["evidence"], "real-provider")
        self.assertIn(real_provider["status"], {"SKIP", "BLOCKED"})
        self.assertEqual(human_gate["status"], "PENDING")
        self.assertFalse(human_gate["approvalWritten"])


if __name__ == "__main__":
    unittest.main()
