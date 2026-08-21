from __future__ import annotations

import io
import json
import shutil
import unittest
import uuid
import sys
import wave
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import scripts.generate_voiceover as voice_module
import scripts.project_workspace as workspace_module
from scripts.generate_voiceover import main as voice_main
from scripts.project_workspace import (
    DEFAULT_GLOBAL_PROMPT,
    FIXED_CANVAS,
    FIXED_RENDER_PROFILE,
    ProjectWorkspace,
    load_project,
    sha256_file,
    write_json_atomic,
)
from scripts.srt_timeline import build_source_timing_plan, parse_srt
from scripts.voiceover import FakeProviderAdapter, PermanentProviderError


def canonical_wav_bytes(duration_ms: int = 200) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(24000)
        wav.writeframes(b"\x00\x00" * (24000 * duration_ms // 1000))
    return output.getvalue()


class VoiceoverTimingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.root = ROOT / f".test-voiceover-timing-{uuid.uuid4().hex[:8]}"
        cls.root.mkdir(parents=True, exist_ok=False)
        cls._drive_patcher = mock.patch.object(
            workspace_module, "_require_d_drive", return_value=None
        )
        cls._drive_patcher.start()
        cls._provider_patcher = mock.patch.object(
            voice_module, "active_provider_id", return_value="edge-tts"
        )
        cls._provider_patcher.start()

    @classmethod
    def tearDownClass(cls) -> None:
        target = cls.root.resolve()
        target.relative_to(ROOT.resolve())
        shutil.rmtree(target)
        cls._provider_patcher.stop()
        cls._drive_patcher.stop()

    def make_project(self):
        case = self.root / uuid.uuid4().hex[:8]
        case.mkdir()
        config = case / "workspace.json"
        config.write_text(
            json.dumps({"schemaVersion": 1, "workspaceRoot": str(case / "workspace")}),
            encoding="utf-8",
        )
        source = case / "source.srt"
        source.write_text(
            "1\n00:00:00,000 --> 00:00:00,200\n第一幕自然中文句子。\n\n"
            "2\n00:00:00,200 --> 00:00:00,400\n第二幕自然中文句子。\n",
            encoding="utf-8",
        )
        plan = {
            "schemaVersion": 1,
            "projectId": None,
            "outputCanvas": dict(FIXED_CANVAS),
            "globalPrompt": DEFAULT_GLOBAL_PROMPT,
            "constraints": {"forbidText": True},
            "scenesDirectory": "scenes",
            "manifestFile": "manifests/generation-manifest.json",
            "scenes": [
                {
                    "sceneId": "scene-01",
                    "subtitleRange": {"startMs": 0, "endMs": 200},
                    "sceneDurationMs": 200,
                    "prompt": "第一幕描绘一句自然中文旁白对应的简洁场景",
                    "outputFile": "scene-01.png",
                },
                {
                    "sceneId": "scene-02",
                    "subtitleRange": {"startMs": 200, "endMs": 400},
                    "sceneDurationMs": 200,
                    "prompt": "第二幕描绘下一句自然中文旁白对应的简洁场景",
                    "outputFile": "scene-02.png",
                },
            ],
        }
        return ProjectWorkspace.from_config(config).create_project(
            f"v-{uuid.uuid4().hex[:8]}", source, confirmed_plan=plan, voiceover_mode="edge-tts"
        )

    def make_multi_unit_project(self):
        case = self.root / uuid.uuid4().hex[:8]
        case.mkdir()
        config = case / "workspace.json"
        config.write_text(
            json.dumps({"schemaVersion": 1, "workspaceRoot": str(case / "workspace")}),
            encoding="utf-8",
        )
        source = case / "source.srt"
        source.write_text(
            "1\n00:00:00,000 --> 00:00:00,200\n第一段描述晨光照亮安静的旧城街道。\n\n"
            "2\n00:00:00,200 --> 00:00:00,400\n第二段描述行人开始穿过清晨的广场。\n\n"
            "3\n00:00:00,400 --> 00:00:00,600\n第三段描述钟声唤醒沉睡的小镇居民。\n\n"
            "4\n00:00:00,600 --> 00:00:00,800\n第四段描述新的一天终于缓缓展开。\n",
            encoding="utf-8",
        )
        plan = {
            "schemaVersion": 1,
            "projectId": None,
            "outputCanvas": dict(FIXED_CANVAS),
            "globalPrompt": DEFAULT_GLOBAL_PROMPT,
            "constraints": {"forbidText": True},
            "scenesDirectory": "scenes",
            "manifestFile": "manifests/generation-manifest.json",
            "scenes": [
                {
                    "sceneId": "scene-01",
                    "subtitleRange": {"startMs": 0, "endMs": 400},
                    "sceneDurationMs": 400,
                    "prompt": "晨光照亮安静的旧城街道，行人开始穿过清晨广场",
                    "outputFile": "scene-01.png",
                },
                {
                    "sceneId": "scene-02",
                    "subtitleRange": {"startMs": 400, "endMs": 800},
                    "sceneDurationMs": 400,
                    "prompt": "钟声唤醒沉睡的小镇居民，新的一天在镇上缓缓展开",
                    "outputFile": "scene-02.png",
                },
            ],
        }
        return ProjectWorkspace.from_config(config).create_project(
            f"v-{uuid.uuid4().hex[:8]}", source, confirmed_plan=plan, voiceover_mode="edge-tts"
        )

    def generate_full(self, project, *, wav_ms: int = 200) -> str:
        adapter = FakeProviderAdapter(canonical_wav_bytes(wav_ms), "audio/wav")
        sample_output = io.StringIO()
        with redirect_stdout(sample_output):
            self.assertEqual(voice_main(["sample", "--project", str(project.root)], adapter=adapter), 0)
        sample_identity = next(line.split("=", 1)[1] for line in sample_output.getvalue().splitlines() if line.startswith("SAMPLE_IDENTITY="))
        self.assertEqual(voice_main(["approve-sample", "--project", str(project.root), "--identity-hash", sample_identity]), 0)
        full_output = io.StringIO()
        with redirect_stdout(full_output):
            self.assertEqual(
                voice_main(["full", "--project", str(project.root)], adapter=FakeProviderAdapter(canonical_wav_bytes(wav_ms), "audio/wav")),
                0,
            )
        lines = full_output.getvalue().splitlines()
        return next(line.split("=", 1)[1] for line in lines if line.startswith("FULL_IDENTITY="))

    def test_timeline_srt_and_scene_cumulative_frames_are_consistent(self) -> None:
        project = self.make_project()
        self.generate_full(project)
        timeline_path = project.path("audio/timeline.json")
        timeline = json.loads(timeline_path.read_text(encoding="utf-8"))
        self.assertNotIn("sha256", {key: value for key, value in timeline.items() if key != "narrationSrt"})
        self.assertEqual(timeline["units"][0]["startMs"], 0)
        for previous, current in zip(timeline["units"], timeline["units"][1:]):
            self.assertEqual(previous["endMs"], current["startMs"])
        self.assertEqual(timeline["units"][-1]["endMs"], timeline["audio"]["durationMs"])
        self.assertEqual(timeline["scenes"][0]["endFrameExclusive"], timeline["scenes"][1]["startFrame"])
        self.assertEqual(
            sum(scene["frameCount"] for scene in timeline["scenes"]),
            timeline["scenes"][-1]["endFrameExclusive"],
        )
        narration_path = project.path("audio/narration.srt")
        narration = parse_srt(narration_path.read_text(encoding="utf-8"))
        self.assertEqual([cue["text"] for cue in narration], [unit["text"] for unit in timeline["units"]])
        self.assertEqual(timeline["narrationSrt"]["sha256"], sha256_file(narration_path))

    def test_production_scene_schema_builds_stable_multi_unit_multi_scene_timeline(self) -> None:
        project = self.make_multi_unit_project()
        first_identity = self.generate_full(project)
        timeline_path = project.path("audio/timeline.json")
        narration_path = project.path("audio/narration.srt")
        timeline = json.loads(timeline_path.read_text(encoding="utf-8"))

        self.assertTrue(all("sourceCueRange" not in scene for scene in project.plan["scenes"]))
        self.assertEqual(
            [unit["sourceCueRange"] for unit in timeline["units"]],
            [[1, 1], [2, 2], [3, 3], [4, 4]],
        )
        self.assertEqual(
            [unit["sourceOrdinalRange"] for unit in timeline["units"]],
            [[1, 1], [2, 2], [3, 3], [4, 4]],
        )
        self.assertEqual(
            [scene["sourceCueRange"] for scene in timeline["scenes"]],
            [[1, 2], [3, 4]],
        )
        self.assertEqual(
            [scene["unitRange"] for scene in timeline["scenes"]],
            [[1, 2], [3, 4]],
        )
        self.assertEqual(
            [(scene["startMs"], scene["endMs"]) for scene in timeline["scenes"]],
            [(0, 400), (400, 800)],
        )
        self.assertEqual(timeline["audio"]["durationMs"], 800)
        self.assertEqual(timeline["units"][-1]["endMs"], 800)
        narration = parse_srt(narration_path.read_text(encoding="utf-8"))
        self.assertEqual(
            [(cue["startMs"], cue["endMs"]) for cue in narration],
            [(0, 200), (200, 400), (400, 600), (600, 800)],
        )

        timeline_sha = sha256_file(timeline_path)
        narration_sha = sha256_file(narration_path)
        no_call = FakeProviderAdapter(outcomes=[PermanentProviderError("segments should be reused")])
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(
                voice_main(["full", "--project", str(project.root)], adapter=no_call),
                0,
            )
        second_identity = next(
            line.split("=", 1)[1]
            for line in output.getvalue().splitlines()
            if line.startswith("FULL_IDENTITY=")
        )
        self.assertEqual(no_call.requests, [])
        self.assertEqual(second_identity, first_identity)
        self.assertEqual(sha256_file(timeline_path), timeline_sha)
        self.assertEqual(sha256_file(narration_path), narration_sha)

    def test_over_ten_percent_requires_accept_actual_and_keeps_generation_plan_hash(self) -> None:
        project = self.make_project()
        identity = self.generate_full(project, wav_ms=400)
        generation_before = sha256_file(project.plan_path)
        timing_before = project.timing_plan_path.read_bytes()
        self.assertEqual(
            voice_main([
                "approve-full", "--project", str(project.root),
                "--identity-hash", identity,
                "--review-policy", "user_first",
            ]), 5
        )
        self.assertEqual(project.timing_plan_path.read_bytes(), timing_before)
        self.assertEqual(
            voice_main([
                "approve-full", "--project", str(project.root), "--identity-hash", identity,
                "--duration-decision", "accept_actual",
                "--review-policy", "user_first",
            ]),
            0,
        )
        current = load_project(project.root)
        self.assertEqual(current.timing_plan["activeTimeline"]["kind"], "edge-tts-audio-timeline")
        self.assertEqual(current.timing_plan["activeTimeline"]["sha256"], sha256_file(project.path("audio/timeline.json")))
        self.assertEqual(sha256_file(project.plan_path), generation_before)

    def test_timing_only_source_change_reuses_validated_segments_but_stales_approvals(self) -> None:
        project = self.make_project()
        self.generate_full(project)
        old_manifest = json.loads(project.path("manifests/voice-manifest.json").read_text(encoding="utf-8"))
        old_segment_hashes = [segment["sha256"] for segment in old_manifest["segments"]]

        source_path = project.path("source/source.srt")
        source_path.write_text(
            "1\n00:00:00,000 --> 00:00:00,150\n第一幕自然中文句子。\n\n"
            "2\n00:00:00,200 --> 00:00:00,400\n第二幕自然中文句子。\n",
            encoding="utf-8",
        )
        metadata = json.loads(project.path("project.json").read_text(encoding="utf-8"))
        metadata["source"]["sha256"] = sha256_file(source_path)
        write_json_atomic(project.path("project.json"), metadata)
        source_timing = build_source_timing_plan(
            project_id=project.project_id,
            source_srt_path=source_path,
            scene_specs=project.plan["scenes"],
            render_profile=FIXED_RENDER_PROFILE,
            voiceover_mode="edge-tts",
        )
        write_json_atomic(project.timing_plan_path, source_timing)

        sample_output = io.StringIO()
        with redirect_stdout(sample_output):
            self.assertEqual(
                voice_main(["sample", "--project", str(project.root)], adapter=FakeProviderAdapter(canonical_wav_bytes(), "audio/wav")),
                0,
            )
        manifest = json.loads(project.path("manifests/voice-manifest.json").read_text(encoding="utf-8"))
        self.assertFalse(manifest["sample"]["approval"]["approved"])
        self.assertFalse(manifest["fullApproval"]["approved"])
        self.assertEqual([segment["sha256"] for segment in manifest["segments"]], old_segment_hashes)
        sample_identity = next(line.split("=", 1)[1] for line in sample_output.getvalue().splitlines() if line.startswith("SAMPLE_IDENTITY="))
        self.assertEqual(voice_main(["approve-sample", "--project", str(project.root), "--identity-hash", sample_identity]), 0)
        no_call = FakeProviderAdapter(outcomes=[PermanentProviderError("segment should be reused")])
        self.assertEqual(voice_main(["full", "--project", str(project.root)], adapter=no_call), 0)
        self.assertEqual(no_call.requests, [])

    def test_voice_rate_change_invalidates_sample_approval_and_segment_reuse(self) -> None:
        project = self.make_project()
        self.generate_full(project)
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(
                voice_main(["sample", "--project", str(project.root), "--rate", "10"], adapter=FakeProviderAdapter(canonical_wav_bytes(), "audio/wav")),
                0,
            )
        manifest = json.loads(project.path("manifests/voice-manifest.json").read_text(encoding="utf-8"))
        self.assertFalse(manifest["sample"]["approval"]["approved"])
        self.assertTrue(all(segment["status"] == "pending" for segment in manifest["segments"]))
        self.assertEqual(voice_main(["full", "--project", str(project.root)], adapter=FakeProviderAdapter(canonical_wav_bytes(), "audio/wav")), 5)


if __name__ == "__main__":
    unittest.main()
