from __future__ import annotations

import json
import sys
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
TESTS = ROOT / "tests"
for entry in (SCRIPTS, TESTS):
    if str(entry) not in sys.path:
        sys.path.insert(0, str(entry))

import generate_voiceover  # noqa: E402
import project_workspace  # noqa: E402
import render_timing  # noqa: E402
import validation_receipts  # noqa: E402
from test_annotation_batch import AnnotationBatchFixture  # noqa: E402


class FormalValidationContextTests(AnnotationBatchFixture):
    def make_current_project(self, count: int = 2, *, edge: bool = False):
        project, audio_sha, full_identity = self.make_project(count, edge=edge)
        current = None
        if edge:
            current = {
                "fullApproved": True,
                "audioSha256": audio_sha,
                "fullIdentityHash": full_identity,
            }
        patcher = (
            mock.patch.object(
                generate_voiceover,
                "validate_current_voiceover",
                return_value=current,
            )
            if edge
            else mock.patch.object(
                generate_voiceover,
                "validate_current_voiceover",
            )
        )
        with patcher:
            context = render_timing.build_formal_validation_context(project)
        for scene in project.plan["scenes"]:
            project_workspace.write_json_atomic(
                project.root / "scenes" / f"{scene['sceneId']}.annotation.json",
                self.annotation(project, scene["sceneId"], context),
            )
        return project_workspace.load_project(project.root), current

    def make_receipt(self, project, run_id: str = "unit-run"):
        context = render_timing.build_formal_validation_context(project)
        scene_ids = [scene["sceneId"] for scene in project.plan["scenes"]]
        formals = render_timing.resolve_formal_scenes(
            project,
            scene_ids,
            context=context,
        )
        loaded, path = render_timing.write_formal_validation_context_receipt(
            project,
            context,
            run_id=run_id,
            validated_formals=list(formals),
        )
        return context, formals, loaded, path

    def test_same_run_receipt_reuses_deep_annotation_evidence(self) -> None:
        project, _current = self.make_current_project(2)
        with mock.patch.object(
            render_timing,
            "validate_annotation",
            wraps=render_timing.validate_annotation,
        ) as deep:
            _base, formals, loaded, path = self.make_receipt(project)
        self.assertEqual(deep.call_count, 2)
        reloaded = render_timing.load_formal_validation_context_receipt(
            project,
            path,
            expected_run_id="unit-run",
        )
        with mock.patch.object(
            render_timing,
            "validate_annotation",
            side_effect=AssertionError("binding 复用不得再次 deep validate annotation"),
        ):
            rebound = render_timing.resolve_formal_scenes(
                project,
                list(loaded.scene_order),
                context=reloaded,
            )
        self.assertEqual([item.scene_id for item in rebound], [item.scene_id for item in formals])

    def test_receipt_missing_and_cross_run_are_fail_closed(self) -> None:
        project, _current = self.make_current_project(1)
        _base, _formals, _loaded, path = self.make_receipt(project)
        with self.assertRaisesRegex(render_timing.RenderTimingError, "同 run"):
            render_timing.load_formal_validation_context_receipt(
                project,
                path,
                expected_run_id="other-run",
            )
        with self.assertRaisesRegex(render_timing.RenderTimingError, "不可读"):
            render_timing.load_formal_validation_context_receipt(
                project,
                path.with_name("missing.json"),
                expected_run_id="unit-run",
            )

    def test_annotation_timing_generation_render_and_contract_changes_stale(self) -> None:
        mutations = ("annotation", "timing", "generation", "render", "contract")
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                project, _current = self.make_current_project(1)
                run_id = f"run-{mutation}"
                _base, _formals, _loaded, path = self.make_receipt(project, run_id)
                if mutation == "annotation":
                    annotation_path = project.root / "scenes" / "scene-01.annotation.json"
                    value = json.loads(annotation_path.read_text(encoding="utf-8"))
                    value["elements"][0]["label"] = "binding changed"
                    project_workspace.write_json_atomic(annotation_path, value)
                elif mutation == "timing":
                    project.timing_plan_path.write_text(
                        project.timing_plan_path.read_text(encoding="utf-8") + " ",
                        encoding="utf-8",
                    )
                elif mutation == "generation":
                    project.plan_path.write_text(
                        project.plan_path.read_text(encoding="utf-8") + " ",
                        encoding="utf-8",
                    )
                elif mutation == "render":
                    project.metadata["renderProfile"]["fps"] = 30
                else:
                    receipt = json.loads(path.read_text(encoding="utf-8"))
                    receipt["validatorContract"] = "legacy-formal-validator"
                    receipt["receiptSha256"] = validation_receipts.receipt_sha256(receipt)
                    project_workspace.write_json_atomic(path, receipt)
                with self.assertRaises(render_timing.RenderTimingError):
                    render_timing.load_formal_validation_context_receipt(
                        project,
                        path,
                        expected_run_id=run_id,
                    )
                self.tearDown()
                self.setUp()

    def test_expired_receipt_is_rejected(self) -> None:
        project, _current = self.make_current_project(1)
        _base, _formals, _loaded, path = self.make_receipt(project)
        receipt = json.loads(path.read_text(encoding="utf-8"))
        receipt["createdAt"] = "2026-01-01T00:00:00Z"
        receipt["expiresAt"] = "2026-01-01T00:00:01Z"
        receipt["receiptSha256"] = validation_receipts.receipt_sha256(receipt)
        project_workspace.write_json_atomic(path, receipt)
        with self.assertRaisesRegex(render_timing.RenderTimingError, "过期"):
            render_timing.load_formal_validation_context_receipt(
                project,
                path,
                expected_run_id="unit-run",
                now="2026-01-01T00:00:01Z",
            )

    def test_voice_binding_change_stales_receipt_without_reusing_old_approval(self) -> None:
        project, current = self.make_current_project(1, edge=True)
        assert current is not None
        with mock.patch.object(
            generate_voiceover,
            "validate_current_voiceover",
            return_value=current,
        ):
            _base, _formals, _loaded, path = self.make_receipt(project)
        (project.root / "audio" / "narration.wav").write_bytes(b"changed-current-wave")
        with self.assertRaisesRegex(render_timing.RenderTimingError, "narration.wav 已变化"):
            render_timing.load_formal_validation_context_receipt(
                project,
                path,
                expected_run_id="unit-run",
            )

    def test_receipt_reuse_does_not_change_formal_render_identity(self) -> None:
        project, _current = self.make_current_project(1)
        _base, formals, loaded, _path = self.make_receipt(project)
        rebound = render_timing.resolve_formal_scenes(
            project,
            ["scene-01"],
            context=loaded,
        )
        options = {"inkPath": "grid", "colorFill": "contour-wipe"}
        self.assertEqual(
            render_timing.render_identity(formals[0], render_options=options),
            render_timing.render_identity(rebound[0], render_options=options),
        )

    def test_forged_in_memory_receipt_marker_cannot_skip_deep_validation(self) -> None:
        project, _current = self.make_current_project(1)
        context = render_timing.build_formal_validation_context(project)
        annotation_path = project.root / "scenes" / "scene-01.annotation.json"
        created = datetime.now(timezone.utc)
        forged = replace(
            context,
            annotation_bindings=((
                "scene-01",
                project_workspace.sha256_file(annotation_path),
                annotation_path.stat().st_size,
            ),),
            receipt_run_id="forged-run",
            receipt_sha256="f" * 64,
            receipt_file=".work/formal-context-forged-run/receipt.json",
            receipt_created_at=created.isoformat(),
            receipt_expires_at=(created + timedelta(minutes=5)).isoformat(),
        )
        with self.assertRaisesRegex(render_timing.RenderTimingError, "落盘证据不存在"):
            render_timing.resolve_formal_scenes(
                project,
                ["scene-01"],
                context=forged,
            )


if __name__ == "__main__":
    unittest.main()
