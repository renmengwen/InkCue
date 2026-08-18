from __future__ import annotations

import io
import json
import sys
import threading
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
TESTS = ROOT / "tests"
for entry in (SCRIPTS, TESTS):
    if str(entry) not in sys.path:
        sys.path.insert(0, str(entry))

import generate_annotation_previews as previews  # noqa: E402
import annotation_review  # noqa: E402
import generate_voiceover  # noqa: E402
import project_workspace  # noqa: E402
import render_annotation_preview  # noqa: E402
import render_timing  # noqa: E402
from test_annotation_batch import AnnotationBatchFixture  # noqa: E402


class AnnotationPreviewBatchTests(AnnotationBatchFixture):
    def workspace_for_preview(self, concurrency: int):
        return project_workspace.WorkspaceConfig(
            root=self.workspace_root,
            config_path=self.workspace_root / "fixture-config.json",
            concurrency=project_workspace.ExecutionConcurrency(
                annotation_preview=concurrency
            ),
        )

    def preview_annotation(self, project, scene_id: str, context):
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
        return value

    def publish_annotations(self, project, context, scene_ids=None):
        selected = scene_ids or [scene["sceneId"] for scene in project.plan["scenes"]]
        for scene_id in selected:
            project_workspace.write_json_atomic(
                project.root / "scenes" / f"{scene_id}.annotation.json",
                self.preview_annotation(project, scene_id, context),
            )

    def make_current_project(self, count=3):
        project, _audio, _identity = self.make_project(count)
        context = render_timing.build_formal_validation_context(project)
        self.publish_annotations(project, context)
        return project_workspace.load_project(project.root)

    def test_pure_renderer_and_thin_file_wrapper_emit_lossless_rgb_png(self) -> None:
        project = self.make_current_project(1)
        annotation_path = project.root / "scenes" / "scene-01.annotation.json"
        annotation = json.loads(annotation_path.read_text(encoding="utf-8"))
        source_path = project.root / "scenes" / "scene-01.png"
        with Image.open(source_path) as source:
            source.load()
            result = render_annotation_preview.render_annotation_preview(source, annotation)
        self.assertEqual(result.mode, "RGB")
        self.assertEqual(result.size, (1920, 1080))
        output = project.root / "previews" / "single.png"
        render_annotation_preview.render_annotation_preview_file(
            source_path, annotation_path, output
        )
        with Image.open(output) as saved:
            saved.load()
            self.assertEqual((saved.format, saved.mode, saved.size), ("PNG", "RGB", (1920, 1080)))

    def test_missing_annotation_gate_returns_two_and_creates_zero_candidates(self) -> None:
        project, _audio, _identity = self.make_project(2)
        context = render_timing.build_formal_validation_context(project)
        self.publish_annotations(project, context, ["scene-01"])
        output = io.StringIO()
        with mock.patch.object(previews, "load_workspace_config", return_value=self.workspace_for_preview(2)), mock.patch.object(
            previews, "load_project", return_value=project
        ), redirect_stdout(output):
            exit_code = previews.main(["--project", str(project.root), "--all"])
        summary = json.loads(output.getvalue())
        self.assertEqual(exit_code, 2)
        self.assertEqual(summary["taskCount"], 0)
        self.assertFalse(any((project.root / ".work").glob("annotation-preview-*")))

    def test_stale_gate_returns_five_and_creates_zero_candidates(self) -> None:
        project = self.make_current_project(2)
        output = io.StringIO()
        error = render_timing.RenderTimingError("batch 期间 timing plan 已变化")
        with mock.patch.object(previews, "load_workspace_config", return_value=self.workspace_for_preview(2)), mock.patch.object(
            previews, "load_project", return_value=project
        ), mock.patch.object(
            previews, "build_formal_validation_context", side_effect=error
        ), redirect_stdout(output):
            exit_code = previews.main(["--project", str(project.root), "--all"])
        self.assertEqual(exit_code, 5)
        self.assertEqual(json.loads(output.getvalue())["taskCount"], 0)
        self.assertFalse(any((project.root / ".work").glob("annotation-preview-*")))

    def test_eight_edge_scenes_build_global_voice_evidence_once(self) -> None:
        project, audio_sha, full_identity = self.make_project(8, edge=True)
        current = {
            "fullApproved": True,
            "audioSha256": audio_sha,
            "fullIdentityHash": full_identity,
        }
        with mock.patch.object(
            generate_voiceover, "validate_current_voiceover", return_value=current
        ):
            context = render_timing.build_formal_validation_context(project)
            self.publish_annotations(project, context)
        project = project_workspace.load_project(project.root)
        with mock.patch.object(
            generate_voiceover, "validate_current_voiceover", return_value=current
        ) as validate_voice:
            summary = previews.generate_annotation_preview_batch(
                self.workspace_for_preview(4), project
            )
        self.assertEqual(summary["status"], "PASS")
        self.assertEqual(validate_voice.call_count, 1)

    def test_concurrency_one_uses_plain_loop_without_executor(self) -> None:
        project = self.make_current_project(3)

        def forbidden_factory(_workers):
            raise AssertionError("并发 1 不得创建 executor")

        summary = previews.generate_annotation_preview_batch(
            self.workspace_for_preview(1),
            project,
            executor_factory=forbidden_factory,
        )
        self.assertEqual(summary["status"], "PASS")
        self.assertEqual(summary["configuredConcurrency"], 1)
        self.assertEqual(summary["effectiveConcurrency"], 1)
        self.assertEqual(summary["peakActiveWorkers"], 1)

    def test_concurrency_four_is_bounded_and_publishes_in_plan_order(self) -> None:
        project = self.make_current_project(3)
        original = previews._render_task
        completion_order = []
        lock = threading.Lock()
        delay = {"scene-01": 0.12, "scene-02": 0.06, "scene-03": 0.0}

        def controlled(task):
            time.sleep(delay[task.scene_id])
            outcome = original(task)
            with lock:
                completion_order.append(task.scene_id)
            return outcome

        with mock.patch.object(previews, "_render_task", side_effect=controlled):
            summary = previews.generate_annotation_preview_batch(
                self.workspace_for_preview(4), project
            )
        self.assertEqual(summary["status"], "PASS")
        self.assertLessEqual(summary["peakActiveWorkers"], 4)
        self.assertEqual(summary["effectiveConcurrency"], 3)
        self.assertEqual(completion_order, ["scene-03", "scene-02", "scene-01"])
        self.assertEqual(
            summary["publishedOrder"], ["scene-01", "scene-02", "scene-03"]
        )

    def test_concurrency_one_and_four_have_identical_preview_pixels_and_sha(self) -> None:
        project = self.make_current_project(3)
        serial = previews.generate_annotation_preview_batch(
            self.workspace_for_preview(1), project
        )
        serial_hashes = {item["sceneId"]: item["sha256"] for item in serial["scenes"]}
        parallel = previews.generate_annotation_preview_batch(
            self.workspace_for_preview(4), project
        )
        parallel_hashes = {item["sceneId"]: item["sha256"] for item in parallel["scenes"]}
        self.assertEqual(serial_hashes, parallel_hashes)
        for scene_id in serial_hashes:
            with Image.open(project.root / "previews" / f"{scene_id}-annotation-preview.png") as image:
                image.load()
                self.assertEqual((image.mode, image.size), ("RGB", (1920, 1080)))

    def test_candidate_validator_rejects_format_size_and_truncation(self) -> None:
        root = self.project_root / "candidate-cases"
        root.mkdir()
        cases = {
            "format.jpg": lambda path: Image.new("RGB", (1920, 1080)).save(path, format="JPEG"),
            "size.png": lambda path: Image.new("RGB", (320, 180)).save(path, format="PNG"),
            "truncated.png": lambda path: path.write_bytes(b"\x89PNG\r\n\x1a\ntruncated"),
        }
        for name, writer in cases.items():
            with self.subTest(name=name):
                path = root / name
                writer(path)
                with self.assertRaises(previews.AnnotationPreviewBatchError):
                    previews._validate_preview_candidate(path)

    def test_failed_candidate_preserves_old_preview_and_other_scenes_publish(self) -> None:
        project = self.make_current_project(3)
        old = project.root / "previews" / "scene-02-annotation-preview.png"
        Image.new("RGB", (1920, 1080), "#123456").save(old, format="PNG")
        old_sha = project_workspace.sha256_file(old)
        original = previews._save_candidate_png

        def corrupt_one(image, path):
            if "scene-02" in path.as_posix():
                path.parent.mkdir(parents=True, exist_ok=False)
                path.write_bytes(b"\x89PNG\r\n\x1a\ntruncated")
            else:
                original(image, path)

        with mock.patch.object(previews, "_save_candidate_png", side_effect=corrupt_one):
            summary = previews.generate_annotation_preview_batch(
                self.workspace_for_preview(3), project
            )
        self.assertEqual(summary["status"], "FAIL")
        self.assertTrue(summary["partialSuccess"])
        self.assertEqual(summary["publishedOrder"], ["scene-01", "scene-03"])
        self.assertEqual(project_workspace.sha256_file(old), old_sha)
        self.assertTrue((project.root / "previews" / "scene-01-annotation-preview.png").is_file())
        self.assertTrue((project.root / "previews" / "scene-03-annotation-preview.png").is_file())

    def test_contact_sheet_receives_candidates_in_plan_order(self) -> None:
        project = self.make_current_project(3)
        original = previews.build_annotation_preview_contact_sheet
        seen = []

        def capture(candidates):
            seen.extend(candidate.task.scene_id for candidate in candidates)
            return original(candidates)

        with mock.patch.object(
            previews, "build_annotation_preview_contact_sheet", side_effect=capture
        ):
            summary = previews.generate_annotation_preview_batch(
                self.workspace_for_preview(3), project
            )
        self.assertEqual(seen, ["scene-01", "scene-02", "scene-03"])
        self.assertEqual(summary["contactSheet"], "previews/annotation-preview-contact-sheet.png")
        with Image.open(project.root / summary["contactSheet"]) as contact:
            contact.load()
            self.assertEqual((contact.format, contact.mode), ("PNG", "RGB"))

    def test_cli_has_no_approval_parameter_and_technical_pass_writes_no_approval(self) -> None:
        help_text = previews._parser().format_help().lower()
        self.assertNotIn("approved", help_text)
        self.assertNotIn("approval", help_text)
        project = self.make_current_project(1)
        summary = previews.generate_annotation_preview_batch(
            self.workspace_for_preview(1), project
        )
        serialized = json.dumps(summary, ensure_ascii=False)
        self.assertTrue(summary["userConfirmationRequired"])
        self.assertFalse(summary["previewConfirmationWritten"])
        self.assertFalse(summary["approvalWritten"])
        self.assertEqual(summary["nextHumanGate"], "annotation_review_confirmation")
        self.assertEqual(len(summary["annotationReviewIdentitySha256"]), 64)
        self.assertTrue(
            (project.root / annotation_review.TECHNICAL_MANIFEST_FILE).is_file()
        )
        self.assertFalse((project.root / annotation_review.APPROVAL_FILE).exists())
        self.assertNotIn(str(project.root), serialized)

    def test_preview_generation_needs_only_full_technical_current_gate(self) -> None:
        project = self.make_current_project(2)
        summary = previews.generate_annotation_preview_batch(
            self.workspace_for_preview(2), project
        )
        self.assertEqual(summary["status"], "PASS")
        self.assertEqual(summary["publishedOrder"], ["scene-01", "scene-02"])


if __name__ == "__main__":
    unittest.main()
