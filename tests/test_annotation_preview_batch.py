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
import validation_receipts  # noqa: E402
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

    def test_renderer_derives_optional_label_and_hand_path(self) -> None:
        project = self.make_current_project(1)
        annotation_path = project.root / "scenes" / "scene-01.annotation.json"
        annotation = json.loads(annotation_path.read_text(encoding="utf-8"))
        element = annotation["elements"][0]
        element.pop("label")
        element.pop("handPath")
        element["reveal"]["direction"] = "top-to-bottom"
        source_path = project.root / "scenes" / "scene-01.png"
        with Image.open(source_path) as source:
            source.load()
            result = render_annotation_preview.render_annotation_preview(source, annotation)
        self.assertEqual(result.mode, "RGB")
        self.assertEqual(result.size, (1920, 1080))

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

    def test_preview_candidate_deep_once_then_receipt_binding_publish(self) -> None:
        project = self.make_current_project(1)
        with mock.patch.object(
            previews,
            "_validate_preview_candidate",
            wraps=previews._validate_preview_candidate,
        ) as deep, mock.patch.object(
            previews,
            "bind_candidate_receipt",
            wraps=previews.bind_candidate_receipt,
        ) as binding:
            summary = previews.generate_annotation_preview_batch(
                self.workspace_for_preview(1),
                project,
            )
        self.assertEqual(summary["status"], "PASS")
        self.assertEqual(deep.call_count, 1)
        # scene candidate 发布前/后各一次；contact sheet 同样各一次。
        self.assertEqual(binding.call_count, 4)

    def test_preview_batch_accepts_same_run_formal_receipt_without_second_deep_pass(self) -> None:
        project = self.make_current_project(2)
        context = render_timing.build_formal_validation_context(project)
        scene_ids = [scene["sceneId"] for scene in project.plan["scenes"]]
        formals = render_timing.resolve_formal_scenes(project, scene_ids, context=context)
        loaded, receipt_path = render_timing.write_formal_validation_context_receipt(
            project,
            context,
            run_id="preview-reuse",
            validated_formals=list(formals),
        )
        self.assertEqual(loaded.receipt_run_id, "preview-reuse")
        with mock.patch.object(
            previews,
            "build_formal_validation_context",
            side_effect=AssertionError("receipt binding 不得重建 deep context"),
        ), mock.patch.object(
            render_timing,
            "validate_annotation",
            side_effect=AssertionError("receipt binding 不得再次 deep annotation"),
        ):
            summary = previews.generate_annotation_preview_batch(
                self.workspace_for_preview(1),
                project,
                formal_context_receipt=receipt_path,
                formal_context_run_id="preview-reuse",
            )
        self.assertEqual(summary["status"], "PASS")
        self.assertEqual(summary["formalValidationMode"], "binding")
        self.assertEqual(
            summary["formalValidationReceipt"],
            receipt_path.relative_to(project.root).as_posix(),
        )

    def test_preview_binding_rejects_missing_or_mismatched_receipt_without_overwrite(self) -> None:
        root = self.project_root / "receipt-binding"
        root.mkdir()
        candidate = root / "candidate.png"
        target = root / "current.png"
        Image.new("RGB", (1920, 1080), "#F5EBD7").save(candidate, format="PNG")
        Image.new("RGB", (1920, 1080), "#123456").save(target, format="PNG")
        old_sha = project_workspace.sha256_file(target)
        digest, byte_count = previews._validate_preview_candidate(candidate)
        receipt = validation_receipts.build_candidate_receipt(
            candidate_sha256=digest,
            candidate_bytes=byte_count,
            decoded=True,
            format="PNG",
            validator_contract=previews.PREVIEW_VALIDATOR_CONTRACT,
            ttl_seconds=60,
        )
        with self.assertRaisesRegex(previews.AnnotationPreviewBatchError, "receipt 缺失"):
            previews._publish_bytes_atomic(candidate, target, digest)
        mismatched = dict(receipt)
        mismatched["candidateBytes"] += 1
        mismatched["receiptSha256"] = validation_receipts.receipt_sha256(mismatched)
        with self.assertRaisesRegex(previews.AnnotationPreviewBatchError, "receipt 无效"):
            previews._publish_bytes_atomic(candidate, target, digest, mismatched)
        self.assertEqual(project_workspace.sha256_file(target), old_sha)

    def test_preview_post_publish_binding_failure_restores_previous_current(self) -> None:
        root = self.project_root / "receipt-restore"
        root.mkdir()
        candidate = root / "candidate.png"
        target = root / "current.png"
        Image.new("RGB", (1920, 1080), "#F5EBD7").save(candidate, format="PNG")
        Image.new("RGB", (1920, 1080), "#123456").save(target, format="PNG")
        old_sha = project_workspace.sha256_file(target)
        digest, byte_count = previews._validate_preview_candidate(candidate)
        receipt = validation_receipts.build_candidate_receipt(
            candidate_sha256=digest,
            candidate_bytes=byte_count,
            decoded=True,
            format="PNG",
            validator_contract=previews.PREVIEW_VALIDATOR_CONTRACT,
            ttl_seconds=60,
        )
        real_bind = previews.bind_candidate_receipt
        calls = 0

        def fail_after_publish(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise validation_receipts.ReceiptValidationError("post publish stale")
            return real_bind(*args, **kwargs)

        with mock.patch.object(previews, "bind_candidate_receipt", side_effect=fail_after_publish):
            with self.assertRaisesRegex(previews.AnnotationPreviewBatchError, "正式 preview binding"):
                previews._publish_bytes_atomic(candidate, target, digest, receipt)
        self.assertEqual(project_workspace.sha256_file(target), old_sha)

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

    def test_user_first_skips_only_semantic_review_and_keeps_human_gate(self) -> None:
        project = self.make_current_project(2)
        summary = previews.generate_annotation_preview_batch(
            self.workspace_for_preview(2),
            project,
            review_policy="user_first",
        )
        self.assertEqual(summary["status"], "PASS")
        self.assertEqual(summary["reviewPolicy"], "user_first")
        self.assertEqual(
            summary["semanticReview"]["status"], "skipped_by_user"
        )
        self.assertFalse(summary["semanticReview"]["preparedOnly"])
        self.assertIsNone(summary["semanticReview"]["spawnPackage"])
        self.assertTrue(summary["userConfirmationRequired"])
        self.assertEqual(summary["nextHumanGate"], "annotation_review_confirmation")
        self.assertTrue(
            (project.root / annotation_review.TECHNICAL_MANIFEST_FILE).is_file()
        )
        self.assertFalse((project.root / annotation_review.APPROVAL_FILE).exists())

    def test_agent_first_freezes_full_preview_bundle_without_changing_review_identity(self) -> None:
        project = self.make_current_project(2)
        direct = previews.generate_annotation_preview_batch(
            self.workspace_for_preview(2),
            project,
            review_policy="user_first",
        )
        reviewed = previews.generate_annotation_preview_batch(
            self.workspace_for_preview(2),
            project_workspace.load_project(project.root),
            review_policy="agent_first",
        )
        self.assertEqual(reviewed["status"], "PASS")
        self.assertEqual(reviewed["reviewPolicy"], "agent_first")
        self.assertEqual(
            reviewed["annotationReviewIdentitySha256"],
            direct["annotationReviewIdentitySha256"],
        )
        semantic = reviewed["semanticReview"]
        self.assertEqual(semantic["stage"], "visualReview")
        self.assertEqual(semantic["taskKind"], "visualReview")
        self.assertEqual(semantic["scope"], "annotation_preview_bundle")
        self.assertEqual(semantic["status"], "ready_for_host_spawn")
        self.assertTrue(semantic["preparedOnly"])
        self.assertFalse(semantic["hostSpawnExecuted"])
        self.assertTrue(semantic["findingsAreAdvisory"])
        self.assertFalse(semantic["approvalWritten"])

        package = semantic["spawnPackage"]
        self.assertEqual(
            package["contractVersion"], "whiteboard-host-spawn-package-v1"
        )
        self.assertEqual(package["taskKind"], "visualReview")
        task_path = Path(package["taskJsonPath"])
        self.assertTrue(task_path.is_absolute())
        task = json.loads(task_path.read_text(encoding="utf-8"))
        input_files = [item["file"] for item in task["inputs"]]
        self.assertIn("manifests/annotation-review-manifest.json", input_files)
        self.assertIn("scenes/scene-01.annotation.json", input_files)
        self.assertIn("scenes/scene-02.annotation.json", input_files)
        self.assertIn("previews/scene-01-annotation-preview.png", input_files)
        self.assertIn("previews/scene-02-annotation-preview.png", input_files)
        self.assertIn(
            "previews/annotation-preview-contact-sheet.png", input_files
        )
        self.assertNotIn("reviewPolicy", task)
        self.assertNotIn("agent_first", json.dumps(task, ensure_ascii=False))
        self.assertFalse((project.root / annotation_review.APPROVAL_FILE).exists())

    def test_cli_exposes_annotation_review_policy(self) -> None:
        help_text = previews._parser().format_help()
        self.assertIn("--review-policy", help_text)
        self.assertIn("agent_first", help_text)
        self.assertIn("user_first", help_text)

    def test_preview_generation_needs_only_full_technical_current_gate(self) -> None:
        project = self.make_current_project(2)
        summary = previews.generate_annotation_preview_batch(
            self.workspace_for_preview(2), project
        )
        self.assertEqual(summary["status"], "PASS")
        self.assertEqual(summary["publishedOrder"], ["scene-01", "scene-02"])


if __name__ == "__main__":
    unittest.main()
