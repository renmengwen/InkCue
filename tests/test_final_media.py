from __future__ import annotations

import contextlib
import hashlib
import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid
import wave
from pathlib import Path
from unittest import mock

from PIL import Image


SKILL_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = SKILL_ROOT / "scripts"
TEST_RUNS = Path(tempfile.gettempdir()) / "srt-whiteboard-phase6-final-media"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import media_validation  # noqa: E402
import merge_scenes  # noqa: E402
import project_workspace  # noqa: E402
import srt_timeline  # noqa: E402
import subtitle_delivery  # noqa: E402
import burn_subtitles  # noqa: E402
import validate_final_media  # noqa: E402
import approve_final_media  # noqa: E402
import mux_voiceover  # noqa: E402
import background_music  # noqa: E402
from generate_voiceover import main as voice_main  # noqa: E402
from voiceover import FakeProviderAdapter  # noqa: E402


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _run(argv: list[str]) -> None:
    completed = subprocess.run(
        argv,
        shell=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if completed.returncode != 0:
        raise AssertionError(completed.stderr)


def _canonical_wav_bytes(duration_ms: int = 100) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(24000)
        wav.writeframes(b"\x00\x00" * (24000 * duration_ms // 1000))
    return output.getvalue()


class FinalMediaTests(unittest.TestCase):
    def setUp(self) -> None:
        self._provider_patcher = mock.patch(
            "generate_voiceover.active_provider_id", return_value="edge-tts"
        )
        self._provider_patcher.start()
        TEST_RUNS.mkdir(parents=True, exist_ok=True)
        self.root = (TEST_RUNS / f"b2-{uuid.uuid4().hex}").resolve()
        self.root.mkdir()
        for relative in [
            "source",
            "planning",
            "scenes",
            "manifests",
            "previews",
            "output",
            ".work",
            "audio",
            "subtitles",
        ]:
            (self.root / relative).mkdir()
        self.project_id = str(uuid.uuid4())
        source = self.root / "source" / "source.srt"
        source.write_text(
            "1\n00:00:00,000 --> 00:00:00,100\n第一幕\n\n"
            "2\n00:00:00,100 --> 00:00:00,200\n第二幕\n",
            encoding="utf-8",
        )
        self.scenes = [
            {
                "sceneId": "scene-01",
                "sourceCueRange": [1, 1],
                "sceneDurationMs": 100,
                "prompt": "第一幕用一个简洁的红色主体开启叙事",
                "outputFile": "scene-01.png",
            },
            {
                "sceneId": "scene-02",
                "sourceCueRange": [2, 2],
                "sceneDurationMs": 100,
                "prompt": "第二幕用一个简洁的蓝色主体承接叙事",
                "outputFile": "scene-02.png",
            },
        ]
        plan = {
            "schemaVersion": 1,
            "projectId": self.project_id,
            "outputCanvas": dict(project_workspace.FIXED_CANVAS),
            "globalPrompt": "统一白板线稿，不含文字",
            "constraints": {"forbidText": True},
            "scenesDirectory": "scenes",
            "manifestFile": "manifests/generation-manifest.json",
            "scenes": self.scenes,
        }
        timing = srt_timeline.build_source_timing_plan(
            project_id=self.project_id,
            source_srt_path=source,
            scene_specs=self.scenes,
            render_profile=project_workspace.FIXED_RENDER_PROFILE,
            voiceover_mode="disabled",
        )
        metadata = {
            "schemaVersion": 2,
            "projectId": self.project_id,
            "projectName": self.root.name,
            "createdAt": "2026-08-14T12:00:00+08:00",
            "voiceoverMode": "disabled",
            "renderProfile": dict(project_workspace.FIXED_RENDER_PROFILE),
            "source": {"file": "source/source.srt", "sha256": _sha256(source)},
            "paths": dict(project_workspace.PROJECT_PATHS_V2),
        }
        (self.root / "planning" / "generation-plan.json").write_text(
            json.dumps(plan, ensure_ascii=False), encoding="utf-8"
        )
        (self.root / "planning" / "timing-plan.json").write_text(
            json.dumps(timing, ensure_ascii=False), encoding="utf-8"
        )
        (self.root / "project.json").write_text(
            json.dumps(metadata, ensure_ascii=False), encoding="utf-8"
        )
        ffmpeg = shutil.which("ffmpeg")
        if ffmpeg is None:
            self.skipTest("ffmpeg 不可用")
        self.scene_paths: list[Path] = []
        for index, color in enumerate(("red", "blue"), start=1):
            output = self.root / "scenes" / f"scene-{index:02d}-whiteboard.mp4"
            _run(
                [
                    ffmpeg,
                    "-y",
                    "-loglevel",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    f"color=c={color}:s=1920x1080:r=60",
                    "-frames:v",
                    "6",
                    "-an",
                    "-c:v",
                    "libx264",
                    "-preset",
                    "ultrafast",
                    "-crf",
                    "30",
                    "-pix_fmt",
                    "yuv420p",
                    str(output),
                ]
            )
            self.scene_paths.append(output)

    def tearDown(self) -> None:
        self._provider_patcher.stop()
        if hasattr(self, "root") and self.root.exists():
            self.assertEqual(self.root.parent, TEST_RUNS.resolve())
            shutil.rmtree(self.root)

    def _configured_workspace(self):
        workspace = mock.Mock()
        workspace.load_project.side_effect = project_workspace.load_project
        return mock.patch.object(
            merge_scenes.ProjectWorkspace,
            "from_config",
            return_value=workspace,
        )

    def _merge(self, inputs: list[Path] | None = None) -> int:
        values = inputs or self.scene_paths
        with self._configured_workspace(), mock.patch.object(
            merge_scenes,
            "assert_current_scene_review_approval",
            return_value={"identityHash": "fixture-scene-review"},
        ):
            return merge_scenes.main(
                [
                    "--project",
                    str(self.root),
                    "--inputs",
                    *[str(path) for path in values],
                ]
            )

    def test_probe_validate_and_full_decode_real_h264(self) -> None:
        probe = media_validation.probe_media(self.scene_paths[0])
        self.assertEqual(probe["streams"]["video"][0]["containerNbFrames"], 6)
        self.assertEqual(probe["streams"]["audio"], [])
        self.assertNotIn(str(self.root), json.dumps(probe))
        validated = media_validation.validate_video(
            self.scene_paths[0],
            render_profile=project_workspace.FIXED_RENDER_PROFILE,
            expected_frame_count=6,
            expected_audio_streams=0,
        )
        self.assertTrue(validated["validation"]["validated"])
        self.assertEqual(validated["decodedFrameCount"], 6)
        self.assertEqual(validated["streams"]["video"][0]["frameCount"], 6)
        self.assertEqual(validated["frameCountEvidence"], "decoded_frames_v1")
        self.assertEqual(validated["validation"]["validationMode"], "deep")
        self.assertEqual(
            validated["validation"]["deepReceipt"]["fullDecode"],
            {"passed": True, "progressEnd": True},
        )
        receipt = validated["validation"]["deepReceipt"]
        for field in (
            "validatorContractVersion",
            "mediaSha256",
            "bytes",
            "decodedFrameCount",
            "streams",
            "videoCodec",
            "width",
            "height",
            "pixelFormat",
            "fps",
            "videoDurationMs",
            "durationMs",
        ):
            self.assertIn(field, receipt)
        with self.assertRaises(media_validation.MediaValidationError):
            media_validation.validate_video(
                self.scene_paths[0],
                render_profile=project_workspace.FIXED_RENDER_PROFILE,
                expected_frame_count=7,
            )

    def test_all_subprocess_calls_use_argv_and_shell_false(self) -> None:
        raw = {
            "format": {"duration": "0.100000", "size": str(self.scene_paths[0].stat().st_size)},
            "streams": [
                {
                    "index": 0,
                    "codec_type": "video",
                    "codec_name": "h264",
                    "width": 1920,
                    "height": 1080,
                    "pix_fmt": "yuv420p",
                    "avg_frame_rate": "60/1",
                    "nb_read_frames": "6",
                    "duration": "0.100000",
                }
            ],
        }
        completed = mock.Mock(returncode=0, stdout=json.dumps(raw), stderr="")
        with mock.patch.object(media_validation.shutil, "which", return_value="ffprobe"), mock.patch.object(
            media_validation.subprocess, "run", return_value=completed
        ) as run:
            media_validation.probe_media(self.scene_paths[0])
        args, kwargs = run.call_args
        self.assertIsInstance(args[0], list)
        self.assertIs(kwargs["shell"], False)
        self.assertNotIn("-count_frames", args[0])

    def _probe_fixture(self, *, container_frames: int | None = 6) -> dict[str, object]:
        return {
            "bytes": self.scene_paths[0].stat().st_size,
            "sha256": _sha256(self.scene_paths[0]),
            "durationMs": 100,
            "formatName": "mov,mp4,m4a,3gp,3g2,mj2",
            "streams": {
                "video": [
                    {
                        "index": 0,
                        "codec": "h264",
                        "width": 1920,
                        "height": 1080,
                        "pixelFormat": "yuv420p",
                        "fps": {"numerator": 60, "denominator": 1, "value": 60.0},
                        "frameCount": container_frames,
                        "containerNbFrames": container_frames,
                        "durationMs": 100,
                    }
                ],
                "audio": [],
                "subtitle": [],
                "other": [],
            },
        }

    def test_validate_video_deep_decodes_once_without_count_frames_scan(self) -> None:
        original = media_validation.subprocess.run
        calls: list[list[str]] = []

        def recorded(argv, **kwargs):
            calls.append(list(argv))
            return original(argv, **kwargs)

        with mock.patch.object(media_validation.subprocess, "run", side_effect=recorded):
            validated = media_validation.validate_video(
                self.scene_paths[0],
                render_profile=project_workspace.FIXED_RENDER_PROFILE,
                expected_frame_count=6,
            )
        self.assertEqual(validated["decodedFrameCount"], 6)
        self.assertEqual(sum("-progress" in argv for argv in calls), 1)
        self.assertFalse(any("-count_frames" in argv for argv in calls))

    def test_missing_nb_frames_uses_decoded_count_and_wrong_nb_frames_fails(self) -> None:
        decode = {
            "decodedFrameCount": 6,
            "frameCountEvidence": "decoded_frames_v1",
            "fullDecode": {"passed": True, "progressEnd": True},
        }
        with mock.patch.object(
            media_validation, "probe_media", return_value=self._probe_fixture(container_frames=None)
        ), mock.patch.object(media_validation, "full_decode", return_value=decode) as deep:
            validated = media_validation.validate_video(
                self.scene_paths[0],
                render_profile=project_workspace.FIXED_RENDER_PROFILE,
                expected_frame_count=6,
            )
        self.assertEqual(validated["decodedFrameCount"], 6)
        self.assertIsNone(validated["validation"]["containerNbFrames"])
        self.assertEqual(deep.call_count, 1)

        with mock.patch.object(
            media_validation, "probe_media", return_value=self._probe_fixture(container_frames=7)
        ), mock.patch.object(media_validation, "full_decode", return_value=decode):
            with self.assertRaisesRegex(
                media_validation.MediaValidationError, "nb_frames.*decodedFrameCount"
            ):
                media_validation.validate_video(
                    self.scene_paths[0],
                    render_profile=project_workspace.FIXED_RENDER_PROFILE,
                    expected_frame_count=6,
                )

    def test_binding_reuses_deep_receipt_and_changed_bytes_or_version_fail_closed(self) -> None:
        candidate = media_validation.validate_video(
            self.scene_paths[0],
            render_profile=project_workspace.FIXED_RENDER_PROFILE,
            expected_frame_count=6,
        )
        receipt = candidate["validation"]["deepReceipt"]
        published = self.root / "output" / "published-binding.mp4"
        shutil.copyfile(self.scene_paths[0], published)
        with mock.patch.object(
            media_validation,
            "full_decode",
            side_effect=AssertionError("binding 不得重复 deep decode"),
        ) as deep:
            bound = media_validation.bind_validated_video(
                published,
                render_profile=project_workspace.FIXED_RENDER_PROFILE,
                expected_frame_count=6,
                deep_receipt=receipt,
            )
        self.assertEqual(bound["validation"]["validationMode"], "binding")
        self.assertEqual(deep.call_count, 0)

        published.write_bytes(published.read_bytes() + b"changed")
        with mock.patch.object(media_validation, "full_decode") as deep:
            with self.assertRaises(media_validation.MediaValidationError):
                media_validation.bind_validated_video(
                    published,
                    render_profile=project_workspace.FIXED_RENDER_PROFILE,
                    expected_frame_count=6,
                    deep_receipt=receipt,
                )
        self.assertEqual(deep.call_count, 0)

        stale = dict(receipt)
        stale["validatorContractVersion"] = "media-validation-stale"
        with mock.patch.object(media_validation, "full_decode") as deep:
            with self.assertRaisesRegex(media_validation.MediaValidationError, "version.*stale"):
                media_validation.bind_validated_video(
                    self.scene_paths[0],
                    render_profile=project_workspace.FIXED_RENDER_PROFILE,
                    expected_frame_count=6,
                    deep_receipt=stale,
                )
        self.assertEqual(deep.call_count, 0)

    def test_force_deep_refreshes_stale_receipt_only_when_explicit(self) -> None:
        stale = {
            "contractVersion": "old",
            "validatorContractVersion": "old",
        }
        decode = {
            "decodedFrameCount": 6,
            "frameCountEvidence": "decoded_frames_v1",
            "fullDecode": {"passed": True, "progressEnd": True},
        }
        with mock.patch.object(
            media_validation, "probe_media", return_value=self._probe_fixture()
        ), mock.patch.object(media_validation, "full_decode", return_value=decode) as deep:
            refreshed = media_validation.validate_video(
                self.scene_paths[0],
                render_profile=project_workspace.FIXED_RENDER_PROFILE,
                expected_frame_count=6,
                deep_receipt=stale,
                force_deep=True,
            )
        self.assertEqual(refreshed["validation"]["validationMode"], "deep")
        self.assertEqual(deep.call_count, 1)

    def test_progress_or_frame_statistics_failure_never_passes(self) -> None:
        cases = [
            mock.Mock(returncode=0, stdout="frame=6\nprogress=continue\n", stderr=""),
            mock.Mock(returncode=0, stdout="progress=end\n", stderr=""),
            mock.Mock(returncode=1, stdout="frame=6\nprogress=end\n", stderr="decode error"),
        ]
        for completed in cases:
            with self.subTest(completed=completed.returncode, stdout=completed.stdout), mock.patch.object(
                media_validation, "_required_executable", return_value="ffmpeg"
            ), mock.patch.object(media_validation.subprocess, "run", return_value=completed):
                with self.assertRaises(media_validation.MediaValidationError):
                    media_validation.full_decode(
                        self.scene_paths[0], probe=self._probe_fixture()
                    )

    def test_atomic_publish_allows_same_volume_cross_directory(self) -> None:
        candidate = self.root / ".work" / "candidate.bin"
        destination = self.root / "output" / "published.bin"
        candidate.write_bytes(b"new")
        destination.write_bytes(b"old")
        media_validation.atomic_publish(candidate, destination)
        self.assertFalse(candidate.exists())
        self.assertEqual(destination.read_bytes(), b"new")

    def test_merge_two_scenes_publishes_default_name_and_manifest(self) -> None:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            exit_code = self._merge()
        self.assertEqual(exit_code, 0)
        output = self.root / "output" / "final-video-only.mp4"
        self.assertTrue(output.is_file())
        self.assertIn(f"OUTPUT={output}", stdout.getvalue())
        media = media_validation.validate_video(
            output,
            render_profile=project_workspace.FIXED_RENDER_PROFILE,
            expected_frame_count=12,
            expected_audio_streams=0,
        )
        manifest = json.loads(
            (self.root / "manifests" / "delivery-manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual(manifest["cleanVideo"]["sha256"], media["sha256"])
        self.assertEqual(manifest["cleanVideo"]["frameCount"], 12)
        self.assertTrue(manifest["cleanVideo"]["technicalValidation"]["validated"])
        self.assertIsNone(manifest["finalApproval"])

    def test_single_scene_still_publishes_final_video_only(self) -> None:
        source = self.root / "source" / "source.srt"
        source.write_text(
            "1\n00:00:00,000 --> 00:00:00,100\n单幕\n",
            encoding="utf-8",
        )
        plan_path = self.root / "planning" / "generation-plan.json"
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        plan["scenes"] = [
            {
                "sceneId": "scene-01",
                "sourceCueRange": [1, 1],
                "sceneDurationMs": 100,
                "prompt": "单幕画面以一个居中的简洁主体呈现完整内容",
                "outputFile": "scene-01.png",
            }
        ]
        plan_path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
        metadata_path = self.root / "project.json"
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        metadata["source"]["sha256"] = _sha256(source)
        metadata_path.write_text(json.dumps(metadata, ensure_ascii=False), encoding="utf-8")
        timing = srt_timeline.build_source_timing_plan(
            project_id=self.project_id,
            source_srt_path=source,
            scene_specs=plan["scenes"],
            render_profile=project_workspace.FIXED_RENDER_PROFILE,
            voiceover_mode="disabled",
        )
        (self.root / "planning" / "timing-plan.json").write_text(
            json.dumps(timing, ensure_ascii=False), encoding="utf-8"
        )
        self.assertEqual(self._merge([self.scene_paths[0]]), 0)
        output = self.root / "output" / "final-video-only.mp4"
        validated = media_validation.validate_video(
            output,
            render_profile=project_workspace.FIXED_RENDER_PROFILE,
            expected_frame_count=6,
        )
        self.assertEqual(validated["streams"]["video"][0]["frameCount"], 6)

    def test_merge_invalid_scene_does_not_overwrite_formal_output(self) -> None:
        output = self.root / "output" / "final-video-only.mp4"
        output.write_bytes(b"old-formal-output")
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            exit_code = self._merge([self.scene_paths[0], self.scene_paths[0]])
        self.assertEqual(exit_code, 2)
        self.assertEqual(output.read_bytes(), b"old-formal-output")

    def test_merge_rejects_legacy_final_mp4_clean_name(self) -> None:
        with self._configured_workspace():
            exit_code = merge_scenes.main(
                [
                    "--project",
                    str(self.root),
                    "--inputs",
                    *[str(path) for path in self.scene_paths],
                    "--output",
                    str(self.root / "output" / "final.mp4"),
                ]
            )
        self.assertEqual(exit_code, 2)
        self.assertFalse((self.root / "output" / "final.mp4").exists())

    def _write_disabled_delivery(self) -> tuple[dict[str, object], str]:
        self.assertEqual(self._merge(), 0)
        clean_path = self.root / "output" / "final-video-only.mp4"
        captioned_path = self.root / "output" / "final-subtitled-video-only.mp4"
        final_path = self.root / "output" / "final.mp4"
        shutil.copyfile(clean_path, captioned_path)
        shutil.copyfile(clean_path, final_path)
        captioned_media = media_validation.validate_video(
            captioned_path,
            render_profile=project_workspace.FIXED_RENDER_PROFILE,
            expected_frame_count=12,
        )
        final_media = media_validation.validate_video(
            final_path,
            render_profile=project_workspace.FIXED_RENDER_PROFILE,
            expected_frame_count=12,
        )
        project = project_workspace.load_project(self.root)
        selection = subtitle_delivery.select_authoritative_srt(project)
        ass_path = self.root / "subtitles" / "final.ass"
        ass_path.write_text("[Script Info]\n", encoding="utf-8")
        contact_path = self.root / "previews" / "final-subtitle-contact-sheet.png"
        Image.new("RGB", (640, 360), "white").save(contact_path, "PNG")
        font_path = Path(subtitle_delivery.DEFAULT_FONT_PATH)
        style_sha = hashlib.sha256(b"subtitle-style-v1").hexdigest()
        subtitle_identity = hashlib.sha256(b"subtitle-identity").hexdigest()
        manifest_path = self.root / "manifests" / "delivery-manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["subtitles"] = {
            "sourceKind": "source-srt",
            "file": selection.relative_path,
            "sha256": selection.sha256,
            "cueCount": 2,
            "firstStartMs": 0,
            "lastEndMs": 200,
            "timelineSha256": selection.timeline_sha256,
            "style": {
                "contractVersion": "subtitle-style-v1",
                "contractSha256": style_sha,
                "font": {
                    "family": "Microsoft YaHei",
                    "file": font_path.name,
                    "sha256": _sha256(font_path),
                },
                "ass": {
                    "file": "subtitles/final.ass",
                    "sha256": _sha256(ass_path),
                    "bytes": ass_path.stat().st_size,
                },
            },
            "subtitleIdentitySha256": subtitle_identity,
            "gapEvidence": "not_applicable_no_gap",
            "contactSheet": {
                "file": "previews/final-subtitle-contact-sheet.png",
                "sha256": _sha256(contact_path),
                "bytes": contact_path.stat().st_size,
                "samples": [{"kind": "cue", "timeMs": 50}],
            },
        }
        manifest["captionedVideo"] = {
            "file": "output/final-subtitled-video-only.mp4",
            **captioned_media,
            "technicalValidation": captioned_media["validation"],
            "cleanVideoSha256": manifest["cleanVideo"]["sha256"],
            "subtitleIdentitySha256": subtitle_identity,
            "burnContractVersion": subtitle_delivery.BURN_CONTRACT_VERSION,
        }
        manifest["captionedVideo"].pop("validation")
        final_inputs, final_identity = subtitle_delivery.compute_final_identity(
            voiceover_mode="disabled",
            clean_video_sha256=manifest["cleanVideo"]["sha256"],
            audio_sha256="",
            timeline_sha256=selection.timeline_sha256,
            authoritative_subtitle_sha256=selection.sha256,
            subtitle_style_contract_sha256=style_sha,
            font_sha256=_sha256(font_path),
            render_profile_sha256=project.timing_plan["renderProfileSha256"],
            timing_plan_sha256=_sha256(project.timing_plan_path),
            mux_contract_version=subtitle_delivery.DISABLED_MUX_CONTRACT_VERSION,
            final_media_sha256=final_media["sha256"],
        )
        manifest["final"] = {
            "file": "output/final.mp4",
            **final_media,
            "technicalValidation": final_media["validation"],
            "finalIdentitySha256": final_identity,
            "identityInputs": final_inputs,
        }
        manifest["final"].pop("validation")
        manifest["finalApproval"] = {"approved": True, "identityHash": "historical"}
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        return manifest, final_identity

    def _prepare_approved_edge(self) -> str:
        source = self.root / "source" / "source.srt"
        source.write_text(
            "1\n00:00:00,000 --> 00:00:00,100\n第一幕自然中文句子。\n\n"
            "2\n00:00:00,100 --> 00:00:00,200\n第二幕自然中文句子。\n",
            encoding="utf-8",
        )
        metadata_path = self.root / "project.json"
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        metadata["voiceoverMode"] = "edge-tts"
        metadata["source"]["sha256"] = _sha256(source)
        metadata_path.write_text(json.dumps(metadata, ensure_ascii=False), encoding="utf-8")
        timing = srt_timeline.build_source_timing_plan(
            project_id=self.project_id,
            source_srt_path=source,
            scene_specs=self.scenes,
            render_profile=project_workspace.FIXED_RENDER_PROFILE,
            voiceover_mode="edge-tts",
        )
        (self.root / "planning" / "timing-plan.json").write_text(
            json.dumps(timing, ensure_ascii=False), encoding="utf-8"
        )
        sample_stdout = io.StringIO()
        with contextlib.redirect_stdout(sample_stdout):
            self.assertEqual(
                voice_main(
                    ["sample", "--project", str(self.root)],
                    adapter=FakeProviderAdapter(_canonical_wav_bytes(), "audio/wav"),
                ),
                0,
            )
        sample_identity = next(
            line.split("=", 1)[1]
            for line in sample_stdout.getvalue().splitlines()
            if line.startswith("SAMPLE_IDENTITY=")
        )
        self.assertEqual(
            voice_main(
                [
                    "approve-sample",
                    "--project",
                    str(self.root),
                    "--identity-hash",
                    sample_identity,
                ]
            ),
            0,
        )
        full_stdout = io.StringIO()
        with contextlib.redirect_stdout(full_stdout):
            self.assertEqual(
                voice_main(
                    ["full", "--project", str(self.root)],
                    adapter=FakeProviderAdapter(_canonical_wav_bytes(), "audio/wav"),
                ),
                0,
            )
        full_identity = next(
            line.split("=", 1)[1]
            for line in full_stdout.getvalue().splitlines()
            if line.startswith("FULL_IDENTITY=")
        )
        self.assertEqual(
            voice_main(
                [
                    "approve-full",
                    "--project",
                    str(self.root),
                    "--identity-hash",
                    full_identity,
                    "--review-policy",
                    "user_first",
                ]
            ),
            0,
        )
        return full_identity

    def _write_edge_delivery(self) -> tuple[dict[str, object], str]:
        self._prepare_approved_edge()
        self.assertEqual(self._merge(), 0)
        burn_subtitles.burn_project(self.root, run_id=f"subtitle-{uuid.uuid4().hex}")
        manifest = mux_voiceover.mux_project(self.root, run_id=f"mux-{uuid.uuid4().hex}")
        return manifest, manifest["final"]["finalIdentitySha256"]

    def test_validate_disabled_three_layers_and_preserves_approval(self) -> None:
        before, identity = self._write_disabled_delivery()
        result = validate_final_media.validate_project_final_media(self.root)
        self.assertTrue(result["ok"])
        self.assertEqual(result["finalIdentitySha256"], identity)
        self.assertFalse(result["finalApprovalWritten"])
        after = json.loads(
            (self.root / "manifests" / "delivery-manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual(after["finalApproval"], before["finalApproval"])
        self.assertTrue(after["final"]["technicalValidation"]["validated"])
        self.assertTrue(after["final"]["technicalValidation"]["fullDecode"])

    def test_real_merge_burn_and_final_validation_integration(self) -> None:
        self.assertEqual(self._merge(), 0)
        burn_subtitles.burn_project(self.root, run_id=f"subtitle-{uuid.uuid4().hex}")
        result = validate_final_media.validate_project_final_media(self.root)
        self.assertTrue(result["ok"])
        self.assertEqual(result["voiceoverMode"], "disabled")
        final_probe = media_validation.probe_media(self.root / "output" / "final.mp4")
        self.assertEqual(len(final_probe["streams"]["video"]), 1)
        self.assertEqual(len(final_probe["streams"]["audio"]), 0)
        manifest = json.loads(
            (self.root / "manifests" / "delivery-manifest.json").read_text(encoding="utf-8")
        )
        self.assertIsNone(manifest["finalApproval"])
        self.assertTrue(manifest["final"]["technicalValidation"]["fullDecode"])

    def test_validate_rejects_stale_final_identity_without_writing_approval(self) -> None:
        before, _ = self._write_disabled_delivery()
        manifest_path = self.root / "manifests" / "delivery-manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["final"]["finalIdentitySha256"] = "0" * 64
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            exit_code = validate_final_media.main(["--project", str(self.root)])
        self.assertEqual(exit_code, 5)
        after = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(after["finalApproval"], before["finalApproval"])

    def test_edge_real_ffmpeg_mux_has_aac_and_current_delivery_identity(self) -> None:
        manifest, identity = self._write_edge_delivery()
        final = self.root / "output" / "final.mp4"
        probe = media_validation.probe_media(final)
        self.assertEqual([stream["codec"] for stream in probe["streams"]["video"]], ["h264"])
        self.assertEqual([stream["codec"] for stream in probe["streams"]["audio"]], ["aac"])
        self.assertEqual(probe["streams"]["audio"][0]["sampleRate"], 24000)
        self.assertEqual(probe["streams"]["audio"][0]["channels"], 1)
        self.assertEqual(manifest["final"]["finalIdentitySha256"], identity)
        self.assertEqual(manifest["final"]["edgeDelivery"]["aac"]["bitrate"], "192k")
        self.assertEqual(manifest["final"]["edgeDelivery"]["muxContractVersion"], "edge-aac-mux-v1")
        self.assertNotIn("backgroundMusic", manifest["final"])
        self.assertIsNone(manifest["finalApproval"])
        result = validate_final_media.validate_project_final_media(self.root)
        self.assertEqual(result["finalIdentitySha256"], identity)
        self.assertFalse(result["finalApprovalWritten"])

    def test_edge_mux_adds_fixed_minus15db_background_music_when_enabled(self) -> None:
        metadata_path = self.root / "project.json"
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        metadata["backgroundMusic"] = {"enabled": True}
        metadata_path.write_text(json.dumps(metadata, ensure_ascii=False), encoding="utf-8")

        manifest, identity = self._write_edge_delivery()
        bgm = manifest["final"]["backgroundMusic"]
        self.assertEqual(bgm["gainDb"], -15.0)
        self.assertEqual(bgm["assetSha256"], background_music.BGM_ASSET_SHA256)
        self.assertEqual(bgm["license"], "CC0-1.0")
        self.assertEqual(bgm["projectField"], "project.json#backgroundMusic.enabled")
        self.assertEqual(
            manifest["final"]["edgeDelivery"]["muxContractVersion"],
            mux_voiceover.FINAL_AUDIO_MIX_CONTRACT_VERSION,
        )
        probe = media_validation.probe_media(self.root / "output" / "final.mp4")
        self.assertEqual(len(probe["streams"]["audio"]), 1)
        result = validate_final_media.validate_project_final_media(self.root)
        self.assertEqual(result["finalIdentitySha256"], identity)

    def test_edge_mux_command_uses_fixed_minus15db_background_music_filter(self) -> None:
        metadata_path = self.root / "project.json"
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        metadata["backgroundMusic"] = {"enabled": True}
        metadata_path.write_text(json.dumps(metadata, ensure_ascii=False), encoding="utf-8")
        self._prepare_approved_edge()
        self.assertEqual(self._merge(), 0)
        burn_subtitles.burn_project(self.root, run_id=f"subtitle-{uuid.uuid4().hex}")
        original = mux_voiceover.subprocess.run
        calls: list[list[str]] = []

        def recorded(argv, **kwargs):
            calls.append(list(argv))
            return original(argv, **kwargs)

        with mock.patch.object(mux_voiceover.subprocess, "run", side_effect=recorded):
            mux_voiceover.mux_project(self.root, run_id=f"mux-{uuid.uuid4().hex}")
        mux_argv = next(argv for argv in calls if "-filter_complex" in argv)
        filter_graph = mux_argv[mux_argv.index("-filter_complex") + 1]
        self.assertIn("volume=-15dB", filter_graph)
        self.assertIn("amix=inputs=2", filter_graph)
        self.assertIn("-stream_loop", mux_argv)
        self.assertNotIn("-short" + "est", mux_argv)

    def test_edge_mux_uses_argv_shell_false_and_never_shortens(self) -> None:
        self._prepare_approved_edge()
        self.assertEqual(self._merge(), 0)
        burn_subtitles.burn_project(self.root, run_id=f"subtitle-{uuid.uuid4().hex}")
        original = mux_voiceover.subprocess.run
        calls: list[tuple[list[str], dict[str, object]]] = []

        def recorded(argv, **kwargs):
            calls.append((list(argv), dict(kwargs)))
            return original(argv, **kwargs)

        with mock.patch.object(mux_voiceover.subprocess, "run", side_effect=recorded):
            mux_voiceover.mux_project(self.root, run_id=f"mux-{uuid.uuid4().hex}")
        mux_calls = [call for call in calls if "-c:a" in call[0]]
        self.assertEqual(len(mux_calls), 1)
        argv, kwargs = mux_calls[0]
        self.assertIs(kwargs["shell"], False)
        self.assertEqual(argv[argv.index("-map") + 1], "0:v:0")
        second_map = argv.index("-map", argv.index("-map") + 1)
        self.assertEqual(argv[second_map + 1], "1:a:0")
        self.assertEqual(argv[argv.index("-c:v") + 1], "copy")
        self.assertNotIn("-short" + "est", argv)

    def test_edge_mux_failure_preserves_existing_final(self) -> None:
        self._write_edge_delivery()
        final_path = self.root / "output" / "final.mp4"
        before = final_path.read_bytes()
        failed = mock.Mock(returncode=1, stderr="forced mux failure")
        original = mux_voiceover.subprocess.run

        def fail_mux_only(argv, **kwargs):
            if "-c:a" in argv:
                return failed
            return original(argv, **kwargs)

        with mock.patch.object(mux_voiceover.subprocess, "run", side_effect=fail_mux_only):
            with self.assertRaises(media_validation.MediaValidationError):
                mux_voiceover.mux_project(self.root, run_id=f"mux-{uuid.uuid4().hex}")
        self.assertEqual(final_path.read_bytes(), before)

    def test_edge_duration_tolerance_rejects_mismatch(self) -> None:
        self._write_edge_delivery()
        project = project_workspace.load_project(self.root)
        with self.assertRaisesRegex(media_validation.MediaValidationError, "canonical timeline"):
            mux_voiceover.validate_edge_mux_media(
                project,
                self.root / "output" / "final.mp4",
                expected_frame_count=12,
                canonical_duration_ms=1000,
            )

    def test_final_approval_requires_prior_validation_and_preserves_old_on_failure(self) -> None:
        _, identity = self._write_edge_delivery()
        manifest_path = self.root / "manifests" / "delivery-manifest.json"
        before = manifest_path.read_bytes()
        self.assertEqual(
            approve_final_media.main(
                ["--project", str(self.root), "--identity-hash", identity]
            ),
            5,
        )
        self.assertEqual(manifest_path.read_bytes(), before)
        validate_final_media.validate_project_final_media(self.root)
        manifest_before_mismatch = manifest_path.read_bytes()
        self.assertEqual(
            approve_final_media.main(
                ["--project", str(self.root), "--identity-hash", "0" * 64]
            ),
            5,
        )
        self.assertEqual(manifest_path.read_bytes(), manifest_before_mismatch)
        self.assertEqual(
            approve_final_media.main(
                ["--project", str(self.root), "--identity-hash", identity]
            ),
            0,
        )
        approved = json.loads(manifest_path.read_text(encoding="utf-8"))["finalApproval"]
        self.assertTrue(approved["approved"])
        self.assertEqual(approved["identityHash"], identity)

        voice_path = self.root / "manifests" / "voice-manifest.json"
        voice = json.loads(voice_path.read_text(encoding="utf-8"))
        voice["fullApproval"]["identityHash"] = "f" * 64
        voice_path.write_text(json.dumps(voice, ensure_ascii=False), encoding="utf-8")
        old_approval = json.loads(manifest_path.read_text(encoding="utf-8"))["finalApproval"]
        self.assertEqual(
            approve_final_media.main(
                ["--project", str(self.root), "--identity-hash", identity]
            ),
            5,
        )
        self.assertEqual(
            json.loads(manifest_path.read_text(encoding="utf-8"))["finalApproval"],
            old_approval,
        )

    def test_disabled_final_approval_cli(self) -> None:
        self.assertEqual(self._merge(), 0)
        burn_subtitles.burn_project(self.root, run_id=f"subtitle-{uuid.uuid4().hex}")
        result = validate_final_media.validate_project_final_media(self.root)
        identity = result["finalIdentitySha256"]
        self.assertEqual(
            approve_final_media.main(
                ["--project", str(self.root), "--identity-hash", identity]
            ),
            0,
        )
        manifest = json.loads(
            (self.root / "manifests" / "delivery-manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual(manifest["finalApproval"]["identityHash"], identity)


if __name__ == "__main__":
    unittest.main()
