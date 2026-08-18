from __future__ import annotations

import io
import json
import sys
from contextlib import redirect_stdout
from pathlib import Path

from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
TESTS = ROOT / "tests"
for entry in (SCRIPTS, TESTS):
    if str(entry) not in sys.path:
        sys.path.insert(0, str(entry))

import annotation_review  # noqa: E402
import approve_annotation_review  # noqa: E402
import generate_annotation_previews as previews  # noqa: E402
import project_workspace  # noqa: E402
import render_timing  # noqa: E402
from test_annotation_batch import AnnotationBatchFixture  # noqa: E402


class AnnotationReviewApprovalTests(AnnotationBatchFixture):
    def workspace_for_preview(self, concurrency: int):
        return project_workspace.WorkspaceConfig(
            root=self.workspace_root,
            config_path=self.workspace_root / "fixture-config.json",
            concurrency=project_workspace.ExecutionConcurrency(
                annotation_preview=concurrency
            ),
        )

    def make_current_project(self, count: int):
        project, _audio, _identity = self.make_project(count)
        context = render_timing.build_formal_validation_context(project)
        for scene_id in [scene["sceneId"] for scene in project.plan["scenes"]]:
            value = self.annotation(project, scene_id, context)
            element = value["elements"][0]
            element.update(
                {
                    "label": f"{scene_id} 主体",
                    "narrativeRole": "主体出现",
                    "subtitle": f"{scene_id} 字幕",
                    "handPath": {"start": [20, 30], "end": [200, 180]},
                }
            )
            element["reveal"]["direction"] = "left-to-right"
            project_workspace.write_json_atomic(
                project.root / "scenes" / f"{scene_id}.annotation.json", value
            )
        return project_workspace.load_project(project.root)

    def generate_review(self, count: int = 2):
        project = self.make_current_project(count)
        summary = previews.generate_annotation_preview_batch(
            self.workspace_for_preview(2), project
        )
        self.assertEqual(summary["status"], "PASS")
        return project_workspace.load_project(project.root), summary

    def test_current_identity_requires_explicit_cli_and_then_passes_read_only_gate(self) -> None:
        project, summary = self.generate_review()
        identity = summary["annotationReviewIdentitySha256"]
        with self.assertRaises(annotation_review.AnnotationReviewApprovalRequired):
            annotation_review.require_current_annotation_review_approval(project)

        output = io.StringIO()
        with redirect_stdout(output):
            code = approve_annotation_review.main(
                ["--project", str(project.root), "--identity-hash", identity]
            )
        self.assertEqual(code, 0)
        self.assertIn(f"ANNOTATION_REVIEW_APPROVED={identity}", output.getvalue())
        current = annotation_review.require_current_annotation_review_approval(project)
        self.assertTrue(current["approved"])
        self.assertEqual(current["identityHash"], identity)

    def test_wrong_identity_returns_five_and_writes_no_approval(self) -> None:
        project, _summary = self.generate_review(1)
        output = io.StringIO()
        with redirect_stdout(output):
            code = approve_annotation_review.main(
                ["--project", str(project.root), "--identity-hash", "f" * 64]
            )
        self.assertEqual(code, 5)
        self.assertFalse(project.path(annotation_review.APPROVAL_FILE).exists())

    def test_annotation_change_stales_technical_receipt_and_old_approval(self) -> None:
        project, summary = self.generate_review(1)
        annotation_review.approve_current_annotation_review(
            project, summary["annotationReviewIdentitySha256"]
        )
        annotation_path = project.root / "scenes" / "scene-01.annotation.json"
        value = json.loads(annotation_path.read_text(encoding="utf-8"))
        value["elements"][0]["label"] = "已修改主体"
        project_workspace.write_json_atomic(annotation_path, value)
        with self.assertRaises(annotation_review.AnnotationReviewApprovalRequired):
            annotation_review.require_current_annotation_review_approval(project.root)

        refreshed_project = project_workspace.load_project(project.root)
        refreshed = previews.generate_annotation_preview_batch(
            self.workspace_for_preview(1), refreshed_project
        )
        self.assertEqual(refreshed["status"], "PASS")
        self.assertNotEqual(
            refreshed["annotationReviewIdentitySha256"],
            summary["annotationReviewIdentitySha256"],
        )
        with self.assertRaises(annotation_review.AnnotationReviewApprovalRequired):
            annotation_review.require_current_annotation_review_approval(project.root)

    def test_preview_byte_change_stales_approval(self) -> None:
        project, summary = self.generate_review(1)
        annotation_review.approve_current_annotation_review(
            project, summary["annotationReviewIdentitySha256"]
        )
        preview = project.root / "previews" / "scene-01-annotation-preview.png"
        with Image.open(preview) as image:
            changed = image.convert("RGB")
        changed.putpixel((0, 0), (1, 2, 3))
        changed.save(preview, format="PNG")
        with self.assertRaises(annotation_review.AnnotationReviewApprovalRequired):
            annotation_review.require_current_annotation_review_approval(project.root)

    def test_timing_plan_byte_change_stales_approval(self) -> None:
        project, summary = self.generate_review(1)
        annotation_review.approve_current_annotation_review(
            project, summary["annotationReviewIdentitySha256"]
        )
        timing_path = project.timing_plan_path
        value = json.loads(timing_path.read_text(encoding="utf-8"))
        timing_path.write_text(
            json.dumps(value, ensure_ascii=False, indent=4) + "\n", encoding="utf-8"
        )
        with self.assertRaises(annotation_review.AnnotationReviewApprovalRequired):
            annotation_review.require_current_annotation_review_approval(project.root)


if __name__ == "__main__":
    import unittest

    unittest.main()
