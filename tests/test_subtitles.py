from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image, ImageChops

SKILL_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = SKILL_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import validate_final_media  # noqa: E402

from scripts import burn_subtitles
from scripts import merge_scenes
from scripts.project_workspace import FIXED_RENDER_PROFILE, Project, sha256_file
from scripts.srt_timeline import parse_srt
from scripts.subtitle_delivery import (
    DEFAULT_FONT_PATH,
    DISABLED_MUX_CONTRACT_VERSION,
    SUBTITLE_STYLE,
    SubtitleDeliveryError,
    SubtitleStaleError,
    compile_ass,
    compute_final_identity,
    escape_ass_text,
    find_subtitle_gap,
    load_font_identity,
    preflight_subtitles,
    select_authoritative_srt,
    subtitle_burn_contract,
    subtitle_burn_contract_sha256,
    subtitle_identity,
    wrap_subtitle_text,
)


def srt(text: str = "测试字幕", *, start: str = "00:00:00,000", end: str = "00:00:01,000") -> str:
    return f"1\n{start} --> {end}\n{text}\n"


class AuthoritativeSourceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "source").mkdir()
        (self.root / "audio").mkdir()
        self.source = self.root / "source" / "source.srt"
        self.source.write_text(srt("源字幕"), encoding="utf-8")
        self.source_sha = sha256_file(self.source)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def project(self, mode: str, timing: dict) -> Project:
        return Project(
            root=self.root,
            metadata={
                "schemaVersion": 2,
                "projectId": "project-subtitle",
                "voiceoverMode": mode,
                "renderProfile": dict(FIXED_RENDER_PROFILE),
                "source": {"file": "source/source.srt", "sha256": self.source_sha},
                "paths": {"work": ".work", "scenes": "scenes"},
            },
            plan={},
            timing_plan=timing,
        )

    def disabled_timing(self) -> dict:
        return {
            "sourceSrtSha256": self.source_sha,
            "activeTimeline": {
                "kind": "source-srt",
                "file": "source/source.srt",
                "sha256": self.source_sha,
            },
        }

    def test_disabled_selects_only_source_even_when_narration_exists(self) -> None:
        (self.root / "audio" / "narration.srt").write_text(srt("不应选中"), encoding="utf-8")
        selected = select_authoritative_srt(self.project("disabled", self.disabled_timing()))
        self.assertEqual(selected.relative_path, "source/source.srt")
        self.assertEqual(selected.source_kind, "source-srt")
        self.assertEqual(selected.cues[0]["text"], "源字幕")

    def test_disabled_rejects_stale_source_hash(self) -> None:
        timing = self.disabled_timing()
        timing["activeTimeline"]["sha256"] = "0" * 64
        with self.assertRaises(SubtitleStaleError):
            select_authoritative_srt(self.project("disabled", timing))

    def write_edge_timeline(self, narration_hash: str) -> tuple[dict, str]:
        timeline_path = self.root / "audio" / "timeline.json"
        timeline_path.write_text(
            json.dumps(
                {
                    "schemaVersion": 1,
                    "narrationSrt": {
                        "file": "audio/narration.srt",
                        "sha256": narration_hash,
                    },
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        timeline_sha = sha256_file(timeline_path)
        return (
            {
                "activeTimeline": {
                    "kind": "edge-tts-audio-timeline",
                    "file": "audio/timeline.json",
                    "sha256": timeline_sha,
                }
            },
            timeline_sha,
        )

    def test_edge_selects_only_narration_bound_by_current_timeline(self) -> None:
        narration = self.root / "audio" / "narration.srt"
        narration.write_text(srt("真实旁白字幕"), encoding="utf-8")
        timing, timeline_sha = self.write_edge_timeline(sha256_file(narration))
        selected = select_authoritative_srt(self.project("edge-tts", timing))
        self.assertEqual(selected.relative_path, "audio/narration.srt")
        self.assertEqual(selected.timeline_sha256, timeline_sha)
        self.assertEqual(selected.cues[0]["text"], "真实旁白字幕")

    def test_edge_missing_narration_never_falls_back_to_source(self) -> None:
        timing, _ = self.write_edge_timeline("0" * 64)
        with self.assertRaisesRegex(SubtitleStaleError, "禁止回退"):
            select_authoritative_srt(self.project("edge-tts", timing))

    def test_edge_rejects_stale_timeline_and_stale_narration_hash(self) -> None:
        narration = self.root / "audio" / "narration.srt"
        narration.write_text(srt("旁白"), encoding="utf-8")
        timing, _ = self.write_edge_timeline("f" * 64)
        with self.assertRaisesRegex(SubtitleStaleError, "narration.srt"):
            select_authoritative_srt(self.project("edge-tts", timing))
        timing["activeTimeline"]["sha256"] = "e" * 64
        with self.assertRaisesRegex(SubtitleStaleError, "timeline.json"):
            select_authoritative_srt(self.project("edge-tts", timing))


class AssCompilationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.font_identity, cls.font = load_font_identity(DEFAULT_FONT_PATH)

    def test_fixed_style_and_actual_font_identity(self) -> None:
        self.assertEqual(SUBTITLE_STYLE["fontSize"], 48)
        self.assertEqual(SUBTITLE_STYLE["maxTextWidthPx"], 1728)
        self.assertEqual(
            self.font_identity.sha256,
            "d79c55e68b1131eea0cc1c47be4f572d964f28c682e143db2ad09c1e4cb07a3f",
        )
        self.assertGreater(self.font.getlength("中文 Microsoft YaHei"), 0)

    def test_real_metrics_wrap_chinese_into_at_most_two_lines(self) -> None:
        lines = wrap_subtitle_text("这是一段用于验证微软雅黑真实像素度量和确定性换行的中文字幕" * 2, self.font)
        self.assertEqual(len(lines), 2)
        self.assertTrue(all(self.font.getlength(line) <= 1728 for line in lines))
        self.assertEqual(lines, wrap_subtitle_text("这是一段用于验证微软雅黑真实像素度量和确定性换行的中文字幕" * 2, self.font))

    def test_explicit_two_lines_are_preserved_when_they_fit(self) -> None:
        self.assertEqual(wrap_subtitle_text("第一行\nSecond line 123", self.font), ("第一行", "Second line 123"))

    def test_more_than_two_lines_capacity_fails_without_font_shrink(self) -> None:
        with self.assertRaisesRegex(SubtitleDeliveryError, "最多两行"):
            wrap_subtitle_text("中" * 100, self.font)

    def test_ass_escape_blocks_braces_backslashes_and_fake_override(self) -> None:
        escaped = escape_ass_text(r"普通 {文本} \N {\pos(1,2)}")
        self.assertNotIn(r"{\pos", escaped)
        self.assertIn(r"\{", escaped)
        self.assertIn(r"\\N", escaped)
        self.assertIn(r"\}", escaped)

    def test_ass_time_rounding_style_and_deterministic_hash(self) -> None:
        cues = parse_srt(srt(r"中文 {\pos(1,2)} \N", start="00:00:00,001", end="00:00:01,234"))
        first = compile_ass(cues)
        second = compile_ass(cues)
        self.assertEqual(first.content, second.content)
        self.assertEqual(first.sha256, second.sha256)
        self.assertEqual(first.sha256, hashlib.sha256(first.content).hexdigest())
        ass = first.content.decode("utf-8")
        self.assertIn("PlayResX: 1920", ass)
        self.assertIn("Style: Default,Microsoft YaHei,48", ass)
        self.assertIn("Dialogue: 0,0:00:00.00,0:00:01.24", ass)
        self.assertNotIn(r"{\pos", ass)

    def test_subtitle_identity_changes_with_authoritative_timeline(self) -> None:
        cues = tuple(parse_srt(srt("字幕")))
        compiled = compile_ass(cues)
        from scripts.subtitle_delivery import AuthoritativeSrt

        selection = AuthoritativeSrt(
            mode="disabled",
            source_kind="source-srt",
            relative_path="source/source.srt",
            path=Path("source.srt"),
            sha256="a" * 64,
            timeline_sha256="a" * 64,
            cues=cues,
        )
        identity = subtitle_identity(selection, compiled)
        changed = subtitle_identity(
            type(selection)(**{**selection.__dict__, "timeline_sha256": "b" * 64}), compiled
        )
        self.assertNotEqual(identity, changed)

        preset_identities = {
            preset: subtitle_identity(selection, compiled, subtitle_preset=preset)
            for preset in ("medium", "fast", "veryfast")
        }
        self.assertEqual(len(set(preset_identities.values())), 3)
        medium_contract = subtitle_burn_contract(
            subtitle_preset="medium",
            ass_style_contract_sha256=compiled.style_contract_sha256,
        )
        fast_contract = subtitle_burn_contract(
            subtitle_preset="fast",
            ass_style_contract_sha256=compiled.style_contract_sha256,
        )
        self.assertEqual(
            {key: value for key, value in medium_contract.items() if key != "subtitlePreset"},
            {key: value for key, value in fast_contract.items() if key != "subtitlePreset"},
        )
        self.assertNotEqual(
            subtitle_burn_contract_sha256(
                subtitle_preset="medium",
                ass_style_contract_sha256=compiled.style_contract_sha256,
            ),
            subtitle_burn_contract_sha256(
                subtitle_preset="fast",
                ass_style_contract_sha256=compiled.style_contract_sha256,
            ),
        )
        for invalid in (None, True, 1, "slow", "MEDIUM"):
            with self.subTest(invalid_subtitle_preset=invalid), self.assertRaises(SubtitleDeliveryError):
                subtitle_identity(selection, compiled, subtitle_preset=invalid)  # type: ignore[arg-type]


class PreflightGapAndIdentityTests(unittest.TestCase):
    def test_real_preflight_requires_ass_and_exact_font(self) -> None:
        identity = preflight_subtitles()
        self.assertEqual(identity.file_name.lower(), "msyh.ttc")
        with self.assertRaisesRegex(SubtitleDeliveryError, "固定字体"):
            preflight_subtitles(font_path=DEFAULT_FONT_PATH.with_name("missing-msyh.ttc"))

    def test_gap_evidence_is_conditional(self) -> None:
        continuous = parse_srt(
            "1\n00:00:00,000 --> 00:00:01,000\n甲\n\n"
            "2\n00:00:01,000 --> 00:00:02,000\n乙\n"
        )
        self.assertIsNone(find_subtitle_gap(continuous))
        gapped = parse_srt(
            "1\n00:00:00,000 --> 00:00:01,000\n甲\n\n"
            "2\n00:00:03,000 --> 00:00:04,000\n乙\n"
        )
        self.assertEqual(find_subtitle_gap(gapped), {"startMs": 1000, "endMs": 3000, "sampleMs": 2000})

    def test_final_identity_covers_disabled_empty_audio_and_final_sha(self) -> None:
        kwargs = dict(
            voiceover_mode="disabled",
            clean_video_sha256="1" * 64,
            audio_sha256="",
            timeline_sha256="2" * 64,
            authoritative_subtitle_sha256="3" * 64,
            subtitle_style_contract_sha256="4" * 64,
            font_sha256="5" * 64,
            render_profile_sha256="6" * 64,
            timing_plan_sha256="7" * 64,
            mux_contract_version=DISABLED_MUX_CONTRACT_VERSION,
            final_media_sha256="8" * 64,
        )
        inputs, identity = compute_final_identity(**kwargs)
        self.assertEqual(inputs["audioSha256"], "")
        self.assertEqual(inputs["finalMediaSha256"], "8" * 64)
        self.assertEqual(inputs["subtitlePreset"], "medium")
        self.assertEqual(identity, compute_final_identity(**kwargs)[1])
        fast_inputs, fast_identity = compute_final_identity(**kwargs, subtitle_preset="fast")
        self.assertEqual(fast_inputs["subtitlePreset"], "fast")
        self.assertNotEqual(identity, fast_identity)
        self.assertEqual(
            {key: value for key, value in inputs.items() if key != "subtitlePreset"},
            {key: value for key, value in fast_inputs.items() if key != "subtitlePreset"},
        )
        with self.assertRaisesRegex(SubtitleDeliveryError, "audioSha256"):
            compute_final_identity(**{**kwargs, "audio_sha256": "9" * 64})
        for invalid in (None, True, 1, "slow", "MEDIUM"):
            if invalid is None:
                continue
            with self.subTest(invalid_final_subtitle_preset=invalid), self.assertRaises(SubtitleDeliveryError):
                compute_final_identity(**kwargs, subtitle_preset=invalid)

    def test_ffmpeg_runner_uses_argv_shell_false_and_cwd(self) -> None:
        completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        with mock.patch.object(burn_subtitles.subprocess, "run", return_value=completed) as run:
            burn_subtitles._run(["ffmpeg", "-version"], cwd=Path("C:/ascii-run"))
        _, kwargs = run.call_args
        self.assertIs(kwargs["shell"], False)
        self.assertEqual(kwargs["cwd"], "C:\\ascii-run")
        with self.assertRaisesRegex(SubtitleDeliveryError, "-shortest"):
            burn_subtitles._run(["ffmpeg", "-shortest"], cwd=Path("C:/ascii-run"))

    def test_cli_uses_workspace_subtitle_preset(self) -> None:
        config = mock.Mock()
        config.video_encoding.subtitle_preset = "veryfast"
        with mock.patch.object(
            burn_subtitles,
            "burn_project",
            return_value={"voiceoverMode": "disabled"},
        ) as burn, mock.patch("builtins.print"):
            exit_code = burn_subtitles.main(
                ["--project", "C:/fixture-project"],
                workspace_config=config,
            )
        self.assertEqual(exit_code, 0)
        self.assertEqual(burn.call_args.kwargs["subtitle_preset"], "veryfast")
        self.assertFalse(burn.call_args.kwargs["force_deep"])


class DeliveryValidationSchedulingTests(unittest.TestCase):
    def test_merge_has_no_second_scene_input_validation_loop(self) -> None:
        source = (SCRIPTS_DIR / "merge_scenes.py").read_text(encoding="utf-8")
        self.assertNotIn("def _validate_scene_inputs", source)
        self.assertIn("assert_current_scene_review_approval(", source)
        self.assertIn("force_deep=args.force_deep", source)

    def test_final_layer_validation_is_bounded_and_returns_manifest_order(self) -> None:
        serial_order: list[str] = []

        def validate_serial(name: str) -> dict[str, str]:
            serial_order.append(name)
            return {"name": name}

        with mock.patch.object(
            validate_final_media.concurrent.futures,
            "ThreadPoolExecutor",
            side_effect=AssertionError("concurrency=1 must remain on the caller thread"),
        ):
            serial = validate_final_media._validate_media_layers(
                validate_serial,
                configured_concurrency=1,
            )
        self.assertEqual(serial_order, ["clean", "captioned", "final"])
        self.assertEqual([item["name"] for item in serial], serial_order)

        real_executor = validate_final_media.concurrent.futures.ThreadPoolExecutor
        worker_counts: list[int] = []

        def executor(*args: object, **kwargs: object):
            worker_counts.append(int(kwargs["max_workers"]))
            return real_executor(*args, **kwargs)

        completion_order: list[str] = []

        def validate_parallel(name: str) -> dict[str, str]:
            completion_order.append(name)
            return {"name": name}

        with mock.patch.object(
            validate_final_media.concurrent.futures,
            "ThreadPoolExecutor",
            side_effect=executor,
        ):
            parallel = validate_final_media._validate_media_layers(
                validate_parallel,
                configured_concurrency=16,
            )
        self.assertEqual(worker_counts, [3])
        self.assertCountEqual(completion_order, ["clean", "captioned", "final"])
        self.assertEqual([item["name"] for item in parallel], ["clean", "captioned", "final"])


class BurnIntegrationTests(unittest.TestCase):
    def test_disabled_real_ffmpeg_burn_publishes_three_layer_contract(self) -> None:
        from scripts import media_validation
        from scripts.media_validation import full_decode, probe_media
        from scripts.project_workspace import sha256_json

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for relative in ("source", "planning", "output", "subtitles", "previews", "manifests", ".work"):
                (root / relative).mkdir(parents=True, exist_ok=True)
            source = root / "source" / "source.srt"
            source.write_text(srt("真实烧录字幕", end="00:00:00,200"), encoding="utf-8")
            source_sha = sha256_file(source)
            timing = {
                "schemaVersion": 1,
                "projectId": "burn-integration",
                "voiceoverMode": "disabled",
                "sourceSrtSha256": source_sha,
                "renderProfileSha256": sha256_json(FIXED_RENDER_PROFILE),
                "activeTimeline": {
                    "kind": "source-srt",
                    "file": "source/source.srt",
                    "sha256": source_sha,
                },
                "scenes": [
                    {
                        "sceneId": "scene-01",
                        "sourceCueRange": [1, 1],
                        "startMs": 0,
                        "endMs": 200,
                        "sceneDurationMs": 200,
                        "startFrame": 0,
                        "endFrameExclusive": 12,
                        "frameCount": 12,
                    }
                ],
            }
            timing_path = root / "planning" / "timing-plan.json"
            timing_path.write_text(json.dumps(timing, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            project = Project(
                root=root,
                metadata={
                    "schemaVersion": 2,
                    "projectId": "burn-integration",
                    "voiceoverMode": "disabled",
                    "renderProfile": dict(FIXED_RENDER_PROFILE),
                    "source": {"file": "source/source.srt", "sha256": source_sha},
                    "paths": {
                        "planning": "planning",
                        "scenes": "scenes",
                        "previews": "previews",
                        "manifests": "manifests",
                        "output": "output",
                        "work": ".work",
                        "audio": "audio",
                        "subtitles": "subtitles",
                    },
                },
                plan={},
                timing_plan=timing,
            )
            clean = root / "output" / "final-video-only.mp4"
            subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-loglevel",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "color=c=#F5EBD7:s=1920x1080:r=60",
                    "-frames:v",
                    "12",
                    "-an",
                    "-c:v",
                    "libx264",
                    "-pix_fmt",
                    "yuv420p",
                    str(clean),
                ],
                shell=False,
                check=True,
            )
            clean_media = media_validation.validate_video(
                clean,
                render_profile=FIXED_RENDER_PROFILE,
                expected_frame_count=12,
                expected_audio_streams=0,
            )
            initial_manifest = {
                "schemaVersion": burn_subtitles.DELIVERY_SCHEMA_VERSION,
                "projectId": project.project_id,
                "voiceoverMode": project.voiceover_mode,
                "timingPlan": {},
                "cleanVideo": burn_subtitles._media_record(
                    "output/final-video-only.mp4",
                    clean_media,
                ),
                "subtitles": {},
                "captionedVideo": {},
                "final": {},
                "finalApproval": None,
            }
            (root / "manifests" / "delivery-manifest.json").write_text(
                json.dumps(initial_manifest, ensure_ascii=False),
                encoding="utf-8",
            )
            decoded_paths: list[Path] = []
            burn_commands: list[list[str]] = []
            original_full_decode = media_validation.full_decode
            original_run = burn_subtitles._run

            def tracked_full_decode(path: str | Path, *args: object, **kwargs: object) -> dict:
                decoded_paths.append(Path(path))
                return original_full_decode(path, *args, **kwargs)

            def tracked_run(argv: list[str], *, cwd: Path) -> None:
                burn_commands.append(list(argv))
                original_run(argv, cwd=cwd)

            with mock.patch.object(burn_subtitles, "load_project", return_value=project), mock.patch.object(
                media_validation,
                "full_decode",
                side_effect=tracked_full_decode,
            ), mock.patch.object(
                burn_subtitles,
                "_run",
                side_effect=tracked_run,
            ):
                manifest = burn_subtitles.burn_project(root, run_id="subtitle-integration")

            self.assertEqual(set(manifest), set(burn_subtitles.DELIVERY_TOP_LEVEL_KEYS))
            self.assertEqual(manifest["subtitles"]["gapEvidence"], "not_applicable_no_gap")
            self.assertEqual(manifest["subtitles"]["sourceKind"], "source-srt")
            self.assertIn("technicalValidation", manifest["captionedVideo"])
            self.assertNotIn("validation", manifest["captionedVideo"])
            self.assertIn("finalIdentitySha256", manifest["final"])
            self.assertIsNone(manifest["finalApproval"])
            self.assertTrue(
                any(
                    command[command.index("-preset") + 1] == "medium"
                    for command in burn_commands
                    if "-preset" in command
                )
            )
            self.assertEqual(manifest["subtitles"]["encoding"]["subtitlePreset"], "medium")
            self.assertEqual(
                manifest["subtitles"]["style"]["assStyleContractSha256"],
                manifest["subtitles"]["encoding"]["assStyleContractSha256"],
            )
            self.assertEqual(
                manifest["subtitles"]["style"]["contractSha256"],
                manifest["subtitles"]["encoding"]["contractSha256"],
            )
            self.assertEqual(
                manifest["final"]["identityInputs"]["subtitleStyleContractSha256"],
                manifest["subtitles"]["encoding"]["contractSha256"],
            )
            self.assertEqual(
                manifest["captionedVideo"]["technicalValidation"]["subtitleEncoding"],
                manifest["subtitles"]["encoding"],
            )
            self.assertEqual(sum(path.name == "captioned.tmp.mp4" for path in decoded_paths), 1)
            self.assertFalse(
                any(
                    path.name in {"final-subtitled-video-only.mp4", "final.mp4"}
                    for path in decoded_paths
                )
            )
            for key in ("captionedVideo", "final"):
                technical = manifest[key]["technicalValidation"]
                self.assertEqual(technical["validationMode"], "binding")
                self.assertEqual(technical["decodedFrameCount"], 12)
                self.assertEqual(technical["frameCountEvidence"], "decoded_frames_v1")
                self.assertEqual(technical["deepReceipt"]["decodedFrameCount"], 12)
                self.assertEqual(technical["deepReceipt"]["frameCountEvidence"], "decoded_frames_v1")

            medium_identity = manifest["final"]["finalIdentitySha256"]
            medium_contract = manifest["subtitles"]["encoding"]["contractSha256"]
            with mock.patch.object(burn_subtitles, "load_project", return_value=project), mock.patch.object(
                media_validation,
                "full_decode",
                side_effect=AssertionError("相同 preset recovery 不得重复 deep decode"),
            ), mock.patch.object(
                burn_subtitles,
                "_run",
                side_effect=AssertionError("相同 preset recovery 不得重新编码或截帧"),
            ):
                recovered = burn_subtitles.burn_project(
                    root,
                    run_id="subtitle-unused-recovery-run",
                    subtitle_preset="medium",
                )
            self.assertEqual(recovered["final"]["finalIdentitySha256"], medium_identity)
            self.assertEqual(recovered["subtitles"]["encoding"]["contractSha256"], medium_contract)
            self.assertEqual(
                recovered["captionedVideo"]["technicalValidation"]["validationMode"],
                "binding",
            )

            recovered["finalApproval"] = {
                "approved": True,
                "identityHash": medium_identity,
            }
            (root / "manifests" / "delivery-manifest.json").write_text(
                json.dumps(recovered, ensure_ascii=False),
                encoding="utf-8",
            )
            fast_decoded_paths: list[Path] = []
            fast_commands: list[list[str]] = []

            def tracked_fast_decode(path: str | Path, *args: object, **kwargs: object) -> dict:
                fast_decoded_paths.append(Path(path))
                return original_full_decode(path, *args, **kwargs)

            def tracked_fast_run(argv: list[str], *, cwd: Path) -> None:
                fast_commands.append(list(argv))
                original_run(argv, cwd=cwd)

            with mock.patch.object(burn_subtitles, "load_project", return_value=project), mock.patch.object(
                media_validation,
                "full_decode",
                side_effect=tracked_fast_decode,
            ), mock.patch.object(
                burn_subtitles,
                "_run",
                side_effect=tracked_fast_run,
            ):
                manifest = burn_subtitles.burn_project(
                    root,
                    run_id="subtitle-fast-rebuild",
                    subtitle_preset="fast",
                )
            self.assertEqual(manifest["subtitles"]["encoding"]["subtitlePreset"], "fast")
            self.assertNotEqual(manifest["subtitles"]["encoding"]["contractSha256"], medium_contract)
            self.assertNotEqual(manifest["final"]["finalIdentitySha256"], medium_identity)
            self.assertIsNone(manifest["finalApproval"])
            self.assertEqual(sum(path.name == "captioned.tmp.mp4" for path in fast_decoded_paths), 1)
            self.assertFalse(
                any(path.name in {"final-subtitled-video-only.mp4", "final.mp4"} for path in fast_decoded_paths)
            )
            self.assertTrue(
                any(
                    command[command.index("-preset") + 1] == "fast"
                    for command in fast_commands
                    if "-preset" in command
                )
            )
            for relative in (
                "subtitles/final.ass",
                "output/final-video-only.mp4",
                "output/final-subtitled-video-only.mp4",
                "output/final.mp4",
                "previews/final-subtitle-contact-sheet.png",
                "manifests/delivery-manifest.json",
            ):
                self.assertTrue((root / relative).is_file(), relative)
            caption_probe = probe_media(root / "output" / "final-subtitled-video-only.mp4")
            final_probe = probe_media(root / "output" / "final.mp4")
            self.assertEqual(caption_probe["sha256"], final_probe["sha256"])
            self.assertEqual(len(final_probe["streams"]["video"]), 1)
            self.assertEqual(len(final_probe["streams"]["audio"]), 0)
            self.assertEqual(final_probe["streams"]["video"][0]["frameCount"], 12)
            full_decode(root / "output" / "final.mp4")

            published_hashes = {
                relative: sha256_file(root / relative)
                for relative in (
                    "subtitles/final.ass",
                    "output/final-subtitled-video-only.mp4",
                    "output/final.mp4",
                    "previews/final-subtitle-contact-sheet.png",
                    "manifests/delivery-manifest.json",
                )
            }
            original_validate_video = media_validation.validate_video

            def reject_candidate(path: str | Path, *args: object, **kwargs: object) -> dict:
                if Path(path).name == "captioned.tmp.mp4":
                    raise media_validation.MediaValidationError("candidate deep validation failed")
                return original_validate_video(path, *args, **kwargs)

            with mock.patch.object(burn_subtitles, "load_project", return_value=project), mock.patch.object(
                media_validation,
                "validate_video",
                side_effect=reject_candidate,
            ):
                with self.assertRaisesRegex(media_validation.MediaValidationError, "candidate deep"):
                    burn_subtitles.burn_project(root, run_id="subtitle-candidate-failure")
            self.assertEqual(
                {
                    relative: sha256_file(root / relative)
                    for relative in published_hashes
                },
                published_hashes,
            )

    def test_real_gap_frame_has_no_subtitle_residue(self) -> None:
        raw = (
            "1\n00:00:00,000 --> 00:00:00,100\n第一条\n\n"
            "2\n00:00:00,300 --> 00:00:00,400\n第二条\n"
        )
        compiled = compile_ass(parse_srt(raw))
        with tempfile.TemporaryDirectory() as temp:
            run = Path(temp)
            (run / "fonts").mkdir()
            (run / "burn.ass").write_bytes(compiled.content)
            shutil.copyfile(DEFAULT_FONT_PATH, run / "fonts" / "msyh.ttc")
            clean = run / "clean.mp4"
            captioned = run / "captioned.mp4"
            subprocess.run(
                [
                    "ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                    "color=c=#F5EBD7:s=1920x1080:r=60", "-frames:v", "24", "-an",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", str(clean),
                ],
                shell=False,
                check=True,
            )
            burn_subtitles._run(
                [
                    "ffmpeg", "-y", "-loglevel", "error", "-i", str(clean.resolve()),
                    "-vf", "ass=burn.ass:fontsdir=fonts", "-map", "0:v:0", "-an",
                    "-c:v", "libx264", "-preset", "medium", "-crf", "18",
                    "-pix_fmt", "yuv420p", "-fps_mode", "passthrough", str(captioned.resolve()),
                ],
                cwd=run,
            )

            def frame(video: Path, timestamp: str, name: str) -> Path:
                output = run / name
                subprocess.run(
                    [
                        "ffmpeg", "-y", "-loglevel", "error", "-ss", timestamp,
                        "-i", str(video), "-frames:v", "1", str(output),
                    ],
                    shell=False,
                    check=True,
                )
                return output

            clean_active = frame(clean, "0.050", "clean-active.png")
            caption_active = frame(captioned, "0.050", "caption-active.png")
            clean_gap = frame(clean, "0.200", "clean-gap.png")
            caption_gap = frame(captioned, "0.200", "caption-gap.png")

            def mean_bottom_delta(first: Path, second: Path) -> float:
                with Image.open(first) as left_image, Image.open(second) as right_image:
                    left = left_image.convert("RGB").crop((0, 720, 1920, 1080))
                    right = right_image.convert("RGB").crop((0, 720, 1920, 1080))
                    histogram = ImageChops.difference(left, right).histogram()
                    total = sum(index % 256 * count for index, count in enumerate(histogram))
                    return total / (1920 * 360 * 3)

            active_delta = mean_bottom_delta(clean_active, caption_active)
            gap_delta = mean_bottom_delta(clean_gap, caption_gap)
            self.assertGreater(active_delta, 0.5)
            self.assertLess(gap_delta, active_delta / 5)


if __name__ == "__main__":
    unittest.main()
