from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SKILL_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = SKILL_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import validate_final_media  # noqa: E402

from scripts import burn_subtitles
from scripts import subtitle_delivery
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

    def test_clean_master_reuse_contract_fails_closed_on_frame_or_duration_change(self) -> None:
        timing = {
            "activeTimeline": {"kind": "source-srt", "file": "source/source.srt", "sha256": "a" * 64},
            "renderProfileSha256": "b" * 64,
            "scenes": [{"endMs": 200, "endFrameExclusive": 12, "frameCount": 12}],
        }
        project = Project(
            root=Path(tempfile.gettempdir()),
            metadata={
                "schemaVersion": 2,
                "projectId": "reuse-contract",
                "voiceoverMode": "disabled",
                "renderProfile": dict(FIXED_RENDER_PROFILE),
                "source": {"file": "source/source.srt", "sha256": "a" * 64},
                "paths": {"work": ".work", "scenes": "scenes"},
            },
            plan={},
            timing_plan=timing,
        )
        current = {
            "file": "planning/timing-plan.json",
            "sha256": "c" * 64,
            "voiceoverMode": "disabled",
            "activeTimeline": timing["activeTimeline"],
            "renderProfileSha256": "b" * 64,
            "frameRounding": "cumulative-ceil-v1",
            "frameCount": 12,
        }
        visual = {**current, "sha256": "d" * 64}
        clean = {
            "sha256": "e" * 64,
            "bytes": 123,
            "durationMs": 200,
            "decodedFrameCount": 12,
            "validation": {"validated": True, "fullDecode": True, "deepReceipt": {"fullDecode": True}},
        }
        evidence = subtitle_delivery.build_subtitle_only_clean_master_reuse(
            project=project,
            visual_timing_plan=visual,
            current_timing_plan=current,
            clean_media=clean,
            subtitle_timeline_sha256="a" * 64,
            audio_sha256="",
            background_music={"enabled": False},
        )
        self.assertEqual(evidence["visualTimingPlanSha256"], visual["sha256"])
        self.assertEqual(evidence["currentSubtitleTimingPlanSha256"], current["sha256"])
        with self.assertRaisesRegex(SubtitleStaleError, "总帧数"):
            subtitle_delivery.build_subtitle_only_clean_master_reuse(
                project=project,
                visual_timing_plan={**visual, "frameCount": 11},
                current_timing_plan=current,
                clean_media=clean,
                subtitle_timeline_sha256="a" * 64,
                audio_sha256="",
                background_music={"enabled": False},
            )
        with self.assertRaisesRegex(SubtitleStaleError, "总时长"):
            subtitle_delivery.build_subtitle_only_clean_master_reuse(
                project=project,
                visual_timing_plan=visual,
                current_timing_plan=current,
                clean_media={**clean, "durationMs": 100},
                subtitle_timeline_sha256="a" * 64,
                audio_sha256="",
                background_music={"enabled": False},
            )

    def test_clean_master_reuse_rejects_changed_audio_or_bgm(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "audio").mkdir()
            narration = root / "audio" / "narration.wav"
            narration.write_bytes(b"current-audio")
            project = Project(
                root=root,
                metadata={
                    "schemaVersion": 2,
                    "projectId": "reuse-audio-bgm",
                    "voiceoverMode": "edge-tts",
                    "renderProfile": dict(FIXED_RENDER_PROFILE),
                    "backgroundMusic": {"enabled": False},
                    "source": {"file": "source/source.srt", "sha256": "a" * 64},
                    "paths": {"work": ".work", "scenes": "scenes"},
                },
                plan={},
                timing_plan={},
            )
            current_audio_sha = sha256_file(narration)
            manifest = {"final": {"identityInputs": {"audioSha256": current_audio_sha}}}
            audio_sha, bgm = burn_subtitles._assert_reuse_audio_and_bgm_unchanged(project, manifest)
            self.assertEqual(audio_sha, current_audio_sha)
            self.assertEqual(bgm, {"enabled": False})
            narration.write_bytes(b"changed-audio")
            with self.assertRaisesRegex(SubtitleStaleError, "音频字节已变化"):
                burn_subtitles._assert_reuse_audio_and_bgm_unchanged(project, manifest)

            narration.write_bytes(b"current-audio")
            project.metadata["backgroundMusic"] = {"enabled": True}
            with self.assertRaisesRegex(SubtitleStaleError, "BGM"):
                burn_subtitles._assert_reuse_audio_and_bgm_unchanged(project, manifest)

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



if __name__ == "__main__":
    unittest.main()
