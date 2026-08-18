from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from scripts.srt_timeline import (
    SrtValidationError,
    build_source_timing_plan,
    group_scenes,
    parse_srt,
    serialize_srt,
)


ROOT = Path(__file__).resolve().parents[1]


def cue_block(index: int, start: str, end: str, text: str) -> str:
    return f"{index}\n{start} --> {end}\n{text}"


class StrictSrtTests(unittest.TestCase):
    def test_bom_crlf_multiline_original_index_and_raw_text_are_preserved(self) -> None:
        raw = (
            "\ufeff42\r\n00:00:01,250 --> 00:00:02,500\r\n"
            "第一行 {不是指令}\r\n第二行 \\N 与 {\\pos(1,2)}\r\n\r\n"
            "7\r\n00:00:03.00 --> 00:00:04.125\r\nEnglish 123，！？\r\n"
        )
        cues = parse_srt(raw)

        self.assertEqual([cue["sourceOrdinal"] for cue in cues], [1, 2])
        self.assertEqual([cue["index"] for cue in cues], [1, 2])
        self.assertEqual([cue["originalIndex"] for cue in cues], [42, 7])
        self.assertEqual(cues[0]["text"], "第一行 {不是指令}\n第二行 \\N 与 {\\pos(1,2)}")
        self.assertEqual(cues[1]["startMs"], 3000)
        self.assertEqual(cues[1]["endMs"], 4125)

    def test_timeline_first_input_without_original_index_is_supported(self) -> None:
        cues = parse_srt("00:00:00,000 --> 00:00:01,000\n无原始编号")
        self.assertEqual(cues[0]["sourceOrdinal"], 1)
        self.assertNotIn("originalIndex", cues[0])

    def test_re_numbering_original_indices_does_not_change_source_ordinal(self) -> None:
        first = parse_srt(
            cue_block(10, "00:00:00,000", "00:00:01,000", "甲")
            + "\n\n"
            + cue_block(20, "00:00:01,000", "00:00:02,000", "乙")
        )
        second = parse_srt(
            cue_block(999, "00:00:00,000", "00:00:01,000", "甲")
            + "\n\n"
            + cue_block(3, "00:00:01,000", "00:00:02,000", "乙")
        )
        self.assertEqual(
            [(cue["sourceOrdinal"], cue["startMs"], cue["endMs"], cue["text"]) for cue in first],
            [(cue["sourceOrdinal"], cue["startMs"], cue["endMs"], cue["text"]) for cue in second],
        )

    def test_serialize_round_trip_preserves_text_and_timing(self) -> None:
        raw = (
            cue_block(8, "00:00:00,001", "00:00:01,111", "中文 {x} \\N\n第二行")
            + "\n\n"
            + cue_block(9, "00:00:02,000", "00:00:03,003", "End!")
        )
        cues = parse_srt(raw)
        round_tripped = parse_srt(serialize_srt(cues))
        self.assertEqual(round_tripped, cues)

    def test_strict_validation_rejects_empty_text_bad_timing_and_overlap(self) -> None:
        invalid_cases = {
            "empty document": " \n\n ",
            "empty cue": "1\n00:00:00,000 --> 00:00:01,000\n   ",
            "reverse": "1\n00:00:02,000 --> 00:00:01,000\n倒序",
            "zero": "1\n00:00:01,000 --> 00:00:01,000\n零时长",
            "bad minute": "1\n00:60:00,000 --> 01:00:01,000\n非法分钟",
            "bad index": "abc\n00:00:00,000 --> 00:00:01,000\n非法编号",
            "overlap": (
                cue_block(1, "00:00:00,000", "00:00:02,000", "甲")
                + "\n\n"
                + cue_block(2, "00:00:01,999", "00:00:03,000", "乙")
            ),
        }
        for label, raw in invalid_cases.items():
            with self.subTest(label=label), self.assertRaises(SrtValidationError):
                parse_srt(raw)


class SourceSceneGroupingTests(unittest.TestCase):
    def test_first_cue_at_zero_produces_global_coverage(self) -> None:
        cues = parse_srt(
            cue_block(1, "00:00:00,000", "00:00:01,000", "甲")
            + "\n\n"
            + cue_block(2, "00:00:01,000", "00:00:02,000", "乙")
        )
        scenes = group_scenes(cues, 1, 1, 2)
        self.assertEqual([(s["startMs"], s["endMs"]) for s in scenes], [(0, 1000), (1000, 2000)])
        self.assertEqual(sum(s["frameCount"] for s in scenes), 120)

    def test_first_cue_nonzero_preserves_leading_silence(self) -> None:
        cues = parse_srt("5\n00:00:02,000 --> 00:00:03,000\n开场后出现")
        scenes = group_scenes(cues, 30, 25, 35)
        self.assertEqual(scenes[0]["startMs"], 0)
        self.assertEqual(scenes[0]["endMs"], 3000)
        self.assertEqual(scenes[0]["sceneDurationMs"], 3000)
        self.assertEqual(scenes[0]["sourceCueRange"], [1, 1])

    def test_two_second_inter_scene_gap_belongs_to_preceding_scene(self) -> None:
        cues = parse_srt(
            cue_block(1, "00:00:00,000", "00:00:01,000", "第一幕")
            + "\n\n"
            + cue_block(2, "00:00:03,000", "00:00:04,000", "第二幕")
        )
        scenes = group_scenes(cues, 1, 1, 4)
        self.assertEqual([(s["startMs"], s["endMs"]) for s in scenes], [(0, 3000), (3000, 4000)])
        self.assertEqual(scenes[0]["sceneDurationMs"], 3000)


class TimingPlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "source.srt"
        self.profile = {
            "contractVersion": "whiteboard-render-v2",
            "width": 1920,
            "height": 1080,
            "fps": 60,
            "pixelFormat": "yuv420p",
            "videoCodec": "h264",
            "frameRounding": "cumulative-ceil-v1",
        }

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_cumulative_non_integral_boundaries_sum_to_global_target(self) -> None:
        self.source.write_text(
            cue_block(100, "00:00:00,000", "00:00:01,000", "甲")
            + "\n\n"
            + cue_block(200, "00:00:01,001", "00:00:02,003", "乙"),
            encoding="utf-8",
        )
        plan = build_source_timing_plan(
            project_id="project-a",
            source_srt_path=self.source,
            scene_specs=[
                {"sceneId": "scene-a", "cueRange": [1, 1]},
                {"sceneId": "scene-b", "cueRange": [2, 2]},
            ],
            render_profile=self.profile,
        )
        scenes = plan["scenes"]
        self.assertEqual(scenes[0]["endFrameExclusive"], 61)
        self.assertEqual(scenes[1]["startFrame"], 61)
        self.assertEqual(scenes[1]["endFrameExclusive"], 121)
        self.assertEqual([scene["frameCount"] for scene in scenes], [61, 60])
        self.assertEqual(sum(scene["frameCount"] for scene in scenes), 121)

    def test_full_plan_contract_hashes_leading_blank_gap_and_final_close(self) -> None:
        content = (
            "\ufeff"
            + cue_block(70, "00:00:00,501", "00:00:01,501", "第一条")
            + "\n\n"
            + cue_block(80, "00:00:03,501", "00:00:04,001", "第二条")
            + "\n"
        )
        self.source.write_text(content, encoding="utf-8")
        plan = build_source_timing_plan(
            project_id="project-b",
            source_srt_path=self.source,
            scene_specs=[
                {"sceneId": "scene-01", "cueRange": [1, 1]},
                {"sceneId": "scene-02", "cueRange": [2, 2]},
            ],
            render_profile=self.profile,
            voiceover_mode="disabled",
        )

        source_sha = hashlib.sha256(self.source.read_bytes()).hexdigest()
        profile_sha = hashlib.sha256(
            json.dumps(
                self.profile, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
        ).hexdigest()
        self.assertEqual(plan["schemaVersion"], 1)
        self.assertEqual(plan["sourceSrtSha256"], source_sha)
        self.assertEqual(plan["renderProfileSha256"], profile_sha)
        self.assertEqual(
            plan["activeTimeline"],
            {"kind": "source-srt", "file": "source/source.srt", "sha256": source_sha},
        )
        self.assertEqual(
            [(s["startMs"], s["endMs"], s["sourceCueRange"]) for s in plan["scenes"]],
            [(0, 3501, [1, 1]), (3501, 4001, [2, 2])],
        )
        self.assertEqual(
            sum(scene["frameCount"] for scene in plan["scenes"]),
            plan["scenes"][-1]["endFrameExclusive"],
        )

    def test_generation_plan_subtitle_ranges_are_supported(self) -> None:
        self.source.write_text(
            cue_block(4, "00:00:00,000", "00:00:01,000", "甲")
            + "\n\n"
            + cue_block(5, "00:00:01,500", "00:00:02,500", "乙"),
            encoding="utf-8",
        )
        plan = build_source_timing_plan(
            project_id="project-c",
            source_srt_path=self.source,
            scene_specs=[
                {
                    "sceneId": "scene-01",
                    "subtitleRange": {"startMs": 0, "endMs": 1000},
                    "sceneDurationMs": 1000,
                },
                {
                    "sceneId": "scene-02",
                    "subtitleRange": {"startMs": 1500, "endMs": 2500},
                    "sceneDurationMs": 1000,
                },
            ],
            render_profile=self.profile,
        )
        self.assertEqual([s["sourceCueRange"] for s in plan["scenes"]], [[1, 1], [2, 2]])
        self.assertEqual([(s["startMs"], s["endMs"]) for s in plan["scenes"]], [(0, 1500), (1500, 2500)])

    def test_scene_ranges_must_cover_every_stable_ordinal_once(self) -> None:
        self.source.write_text(
            cue_block(1, "00:00:00,000", "00:00:01,000", "甲")
            + "\n\n"
            + cue_block(2, "00:00:01,000", "00:00:02,000", "乙"),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(SrtValidationError, "未覆盖全部"):
            build_source_timing_plan(
                project_id="project-d",
                source_srt_path=self.source,
                scene_specs=[{"sceneId": "scene-01", "cueRange": [1, 1]}],
                render_profile=self.profile,
            )

    def test_edge_project_starts_with_provisional_source_timeline(self) -> None:
        self.source.write_text(
            cue_block(11, "00:00:00,250", "00:00:01,250", "待生成旁白"),
            encoding="utf-8",
        )
        plan = build_source_timing_plan(
            project_id="project-edge",
            source_srt_path=self.source,
            scene_specs=[{"sceneId": "scene-01", "cueRange": [1, 1]}],
            render_profile=self.profile,
            voiceover_mode="edge-tts",
        )
        self.assertEqual(plan["voiceoverMode"], "edge-tts")
        self.assertEqual(plan["activeTimeline"]["kind"], "source-srt")
        self.assertEqual(plan["activeTimeline"]["file"], "source/source.srt")
        self.assertEqual(plan["scenes"][0]["startMs"], 0)
        self.assertEqual(plan["scenes"][0]["endMs"], 1250)


class ParseSrtCliTests(unittest.TestCase):
    def test_cli_keeps_compatibility_fields_and_adds_global_frame_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "输入.srt"
            source.write_text("9\n00:00:00,500 --> 00:00:01,500\n你好", encoding="utf-8")
            completed = subprocess.run(
                [sys.executable, str(ROOT / "scripts" / "parse_srt.py"), str(source)],
                cwd=ROOT,
                text=True,
                encoding="utf-8",
                capture_output=True,
                shell=False,
                check=False,
                env={**os.environ, "PYTHONIOENCODING": "utf-8"},
            )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        output = json.loads(completed.stdout)
        self.assertEqual(output["cues"][0]["index"], 1)
        self.assertEqual(output["cues"][0]["sourceOrdinal"], 1)
        self.assertEqual(output["cues"][0]["originalIndex"], 9)
        scene = output["scenes"][0]
        self.assertEqual(scene["cueRange"], [1, 1])
        self.assertEqual(scene["sourceCueRange"], [1, 1])
        self.assertEqual(scene["startFrame"], 0)
        self.assertEqual(scene["endFrameExclusive"], 90)
        self.assertEqual(scene["frameCount"], 90)
        self.assertEqual(scene["sceneDurationMs"], 1500)

    def test_cli_reports_invalid_srt_as_contract_exit_code_two(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "bad.srt"
            source.write_text("1\n00:00:00,000 --> 00:00:00,000\n坏字幕", encoding="utf-8")
            completed = subprocess.run(
                [sys.executable, str(ROOT / "scripts" / "parse_srt.py"), str(source)],
                cwd=ROOT,
                text=True,
                encoding="utf-8",
                capture_output=True,
                shell=False,
                check=False,
                env={**os.environ, "PYTHONIOENCODING": "utf-8"},
            )
        self.assertEqual(completed.returncode, 2)
        self.assertIn("SRT 无效", completed.stderr)


if __name__ == "__main__":
    unittest.main()
