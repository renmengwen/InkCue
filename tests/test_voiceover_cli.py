from __future__ import annotations

import io
import json
import shutil
import sys
import tempfile
import threading
import time
import unittest
import uuid
import wave
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import scripts.project_workspace as workspace_module
import scripts.generate_voiceover as voice_module

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from scripts.generate_voiceover import main as voice_main
from scripts.project_workspace import (
    DEFAULT_GLOBAL_PROMPT,
    ExecutionConcurrency,
    FIXED_CANVAS,
    ProjectWorkspace,
    WorkspaceConfig,
    sha256_file,
)
from scripts.validate_voiceover import main as validate_main
from scripts.voiceover import CancelledError, FakeProviderAdapter, PermanentProviderError, RawAudioResult, RetryableProviderError


def canonical_wav_bytes(duration_ms: int = 200) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(24000)
        wav.writeframes(b"\x00\x00" * (24000 * duration_ms // 1000))
    return output.getvalue()


class VoiceoverCliTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._temporary_root = tempfile.TemporaryDirectory(prefix=".test-voice-cli-", dir=str(ROOT))
        cls.root = Path(cls._temporary_root.name).resolve()
        if cls.root.drive.upper() != "C:":
            raise AssertionError(f"语音 CLI 测试根必须位于 C 盘: {cls.root}")
        cls._drive_patcher = mock.patch.object(workspace_module, "_require_d_drive", return_value=None)
        cls._drive_patcher.start()
        cls._provider_patcher = mock.patch.object(
            voice_module, "active_provider_id", return_value="edge-tts"
        )
        cls._provider_patcher.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls._provider_patcher.stop()
        cls._drive_patcher.stop()
        cls._temporary_root.cleanup()

    def make_project(
        self,
        cue_count: int = 2,
        voiceover_mode: str = "edge-tts",
        *,
        agent_approval_enabled: bool = False,
    ):
        case = self.root / uuid.uuid4().hex[:8]
        case.mkdir()
        config = case / "workspace.json"
        workspace_root = case / "workspace"
        config.write_text(
            json.dumps({"schemaVersion": 1, "workspaceRoot": str(workspace_root)}),
            encoding="utf-8",
        )
        source = case / "source.srt"
        source.write_text("\n\n".join(
            f"{index}\n00:00:00,{(index - 1) * 200:03d} --> 00:00:00,{index * 200:03d}\n第{index}幕自然中文句子。"
            for index in range(1, cue_count + 1)
        ) + "\n", encoding="utf-8")
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
                    "sceneId": f"scene-{index:02d}",
                    "sceneDurationMs": 200,
                    "prompt": f"第{index}幕描绘自然中文旁白对应的简洁场景",
                    "outputFile": f"scene-{index:02d}.png",
                    "sourceCueRange": [index, index],
                }
                for index in range(1, cue_count + 1)
            ],
        }
        return ProjectWorkspace.from_config(config).create_project(
            f"v-{uuid.uuid4().hex[:8]}",
            source,
            confirmed_plan=plan,
            voiceover_mode=voiceover_mode,
            agent_approval_enabled=agent_approval_enabled,
        )

    def execution_config(self, project, *, voice_generation: int) -> WorkspaceConfig:
        return WorkspaceConfig(
            root=project.root.parents[1],
            config_path=self.root / "fixture-workspace.json",
            concurrency=ExecutionConcurrency(voice_generation=voice_generation),
        )

    def sample_and_approve(self, project) -> tuple[str, FakeProviderAdapter]:
        adapter = FakeProviderAdapter(canonical_wav_bytes(), "audio/wav")
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(
                voice_main(["sample", "--project", str(project.root)], adapter=adapter), 0
            )
        identity = next(line.split("=", 1)[1] for line in output.getvalue().splitlines() if line.startswith("SAMPLE_IDENTITY="))
        self.assertEqual(
            voice_main(["approve-sample", "--project", str(project.root), "--identity-hash", identity]), 0
        )
        return identity, adapter

    def publish_alignment(self, project) -> str:
        manifest = json.loads(
            project.path("manifests/voice-manifest.json").read_text(encoding="utf-8")
        )
        duration_ms = manifest["composite"]["durationMs"]
        source_cues = voice_module.parse_srt(
            project.path("source/source.srt").read_text(encoding="utf-8-sig")
        )
        asr_cues = []
        for index, cue in enumerate(source_cues):
            start_ms = index * duration_ms // len(source_cues)
            end_ms = (index + 1) * duration_ms // len(source_cues)
            asr_cues.append(
                {
                    "originalIndex": index + 1,
                    "startMs": start_ms,
                    "endMs": end_ms,
                    "text": cue["text"],
                }
            )
        asr_path = project.path(".work/asr-fixture.srt")
        asr_path.parent.mkdir(parents=True, exist_ok=True)
        asr_path.write_text(voice_module.serialize_srt(asr_cues), encoding="utf-8")
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(
                voice_main(
                    [
                        "publish-alignment",
                        "--project",
                        str(project.root),
                        "--asr-srt",
                        str(asr_path),
                    ]
                ),
                0,
            )
        return next(
            line.split("=", 1)[1]
            for line in output.getvalue().splitlines()
            if line.startswith("FULL_IDENTITY=")
        )

    def test_sample_gate_identity_and_full_technical_validation_do_not_auto_approve(self) -> None:
        project = self.make_project()
        sample_adapter = FakeProviderAdapter(canonical_wav_bytes(), "audio/wav")
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(voice_main(["sample", "--project", str(project.root)], adapter=sample_adapter), 0)
        text = output.getvalue()
        self.assertIn("SAMPLE_AUDIO=", text)
        review_audio = Path(next(
            line.split("=", 1)[1]
            for line in text.splitlines()
            if line.startswith("SAMPLE_REVIEW_AUDIO=")
        ))
        request_audit = Path(next(
            line.split("=", 1)[1]
            for line in text.splitlines()
            if line.startswith("SAMPLE_REQUEST_AUDIT=")
        ))
        self.assertTrue(review_audio.is_file())
        self.assertTrue(request_audit.is_file())
        self.assertEqual(
            review_audio.read_bytes(),
            project.path("previews/voice-sample.wav").read_bytes(),
        )
        audit = json.loads(request_audit.read_text(encoding="utf-8"))
        self.assertEqual(audit["request"]["voiceId"], "zh-CN-YunjianNeural")
        self.assertFalse(audit["providerResponse"]["voiceIdEchoAvailable"])
        self.assertFalse(audit["containsCredentials"])
        self.assertFalse(audit["containsNarrationText"])
        self.assertIn("SAMPLE_VOICE_ID=zh-CN-YunjianNeural", text)
        self.assertIn("SAMPLE_AUDIO_SHA256=", text)
        identity = next(line.split("=", 1)[1] for line in text.splitlines() if line.startswith("SAMPLE_IDENTITY="))
        manifest_path = project.path("manifests/voice-manifest.json")
        before = manifest_path.read_bytes()
        self.assertEqual(
            voice_main(["approve-sample", "--project", str(project.root), "--identity-hash", "0" * 64]), 5
        )
        self.assertEqual(manifest_path.read_bytes(), before)
        self.assertEqual(voice_main(["full", "--project", str(project.root)], adapter=FakeProviderAdapter(canonical_wav_bytes(), "audio/wav")), 5)
        self.assertEqual(
            voice_main(["approve-sample", "--project", str(project.root), "--identity-hash", identity]), 0
        )
        output = io.StringIO()
        full_adapter = FakeProviderAdapter(canonical_wav_bytes(400), "audio/wav")
        with redirect_stdout(output):
            self.assertEqual(voice_main(["full", "--project", str(project.root)], adapter=full_adapter), 0)
        self.assertIn("ALIGNMENT_REQUIRED=1", output.getvalue())
        self.assertFalse(project.path("audio/timeline.json").exists())
        full_identity = self.publish_alignment(project)
        self.assertNotIn("NARRATION_REVIEW", output.getvalue())
        self.assertFalse(project.path("previews/narration-review.mp4").exists())
        self.assertFalse(project.path("previews/narration-review.ass").exists())
        self.assertEqual(len(full_adapter.requests), 1)
        self.assertIn("\n\n", full_adapter.requests[0].text)
        manifest_before_validation = manifest_path.read_bytes()
        self.assertEqual(validate_main(["--project", str(project.root)]), 0)
        self.assertEqual(manifest_path.read_bytes(), manifest_before_validation)
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertFalse(manifest["fullApproval"]["approved"])

        timing_before = project.timing_plan_path.read_bytes()
        manifest_before = manifest_path.read_bytes()
        self.assertEqual(
            voice_main([
                "approve-full", "--project", str(project.root),
                "--identity-hash", full_identity,
            ]),
            2,
        )
        self.assertEqual(project.timing_plan_path.read_bytes(), timing_before)
        self.assertEqual(manifest_path.read_bytes(), manifest_before)
        self.assertEqual(
            voice_main([
                "approve-full", "--project", str(project.root),
                "--identity-hash", "f" * 64,
                "--review-policy", "user_first",
            ]), 5
        )
        self.assertEqual(project.timing_plan_path.read_bytes(), timing_before)
        self.assertEqual(manifest_path.read_bytes(), manifest_before)
        # 2 x 200 ms exactly matches the 400 ms source, so no decision flag is allowed.
        self.assertEqual(
            voice_main([
                "approve-full", "--project", str(project.root),
                "--identity-hash", full_identity,
                "--review-policy", "agent_first",
            ]), 0
        )
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["fullApproval"]["durationDecision"], "within_threshold")
        self.assertEqual(manifest["fullApproval"]["identityHash"], full_identity)
        self.assertEqual(manifest["fullApproval"]["reviewPolicy"], "agent_first")
        self.assertEqual(
            workspace_module.resolve_project_review_policy(project), "agent_first"
        )
        with self.assertRaisesRegex(workspace_module.ProjectValidationError, "不一致"):
            workspace_module.resolve_project_review_policy(project, "user_first")
        self.assertNotIn("reviewIdentityHash", manifest["fullApproval"])

    def test_full_can_inject_local_asr_runner_and_publish_alignment_in_one_cli_action(self) -> None:
        project = self.make_project()
        self.sample_and_approve(project)

        def asr_runner(current_project, narration_path: Path) -> Path:
            self.assertEqual(current_project.root, project.root)
            self.assertTrue(narration_path.is_file())
            source_cues = voice_module.parse_srt(
                project.path("source/source.srt").read_text(encoding="utf-8-sig")
            )
            asr_path = project.path(".work/injected-asr/result.srt")
            asr_path.parent.mkdir(parents=True, exist_ok=True)
            asr_path.write_text(
                voice_module.serialize_srt(
                    [
                        {
                            "originalIndex": index,
                            "startMs": (index - 1) * 200,
                            "endMs": index * 200,
                            "text": cue["text"],
                        }
                        for index, cue in enumerate(source_cues, start=1)
                    ]
                ),
                encoding="utf-8",
            )
            return asr_path

        adapter = FakeProviderAdapter(canonical_wav_bytes(400), "audio/wav")
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(
                voice_main(
                    ["full", "--project", str(project.root)],
                    adapter=adapter,
                    asr_runner=asr_runner,
                ),
                0,
            )
        self.assertEqual(len(adapter.requests), 1)
        self.assertIn("FULL_IDENTITY=", output.getvalue())
        self.assertNotIn("ALIGNMENT_REQUIRED=1", output.getvalue())
        manifest = json.loads(
            project.path("manifests/voice-manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual(manifest["alignment"]["status"], "validated")
        self.assertFalse(manifest["fullApproval"]["approved"])

    def test_internal_asr_runner_uses_project_work_evidence_and_structured_result(self) -> None:
        project = self.make_project()
        narration = project.path("audio/narration.wav")
        narration.parent.mkdir(parents=True, exist_ok=True)
        narration.write_bytes(canonical_wav_bytes(400))

        def fake_transcribe(audio_path: Path, output_dir: Path) -> dict:
            self.assertEqual(audio_path, narration.resolve())
            self.assertFalse(output_dir.exists())
            self.assertEqual(output_dir.parent, project.path(".work").resolve())
            self.assertTrue(output_dir.name.startswith("voice-align-"))
            output_dir.mkdir(parents=True)
            raw_srt = output_dir / "transcript.sentence.srt"
            raw_srt.write_text(
                "1\n00:00:00,000 --> 00:00:00,400\n本地旁白。\n",
                encoding="utf-8",
            )
            return {
                "ok": True,
                "outputDirectory": str(output_dir),
                "rawSrtPath": str(raw_srt),
                "rawJsonPath": str(output_dir / "transcript.paraformer.json"),
                "receiptPath": str(output_dir / "receipt.json"),
                "audioInputPath": str(audio_path),
                "durationMs": 400,
                "sentenceCount": 1,
                "timingValidation": {"invalidRanges": 0, "overlaps": 0},
            }

        with mock.patch.object(
            voice_module, "transcribe_narration", side_effect=fake_transcribe
        ):
            raw_srt = voice_module._run_local_asr(project, narration)
        self.assertTrue(raw_srt.is_file())
        self.assertTrue(raw_srt.is_relative_to(project.path(".work").resolve()))

    def test_asr_failure_keeps_canonical_wav_and_retry_reuses_tts_then_publishes(self) -> None:
        project = self.make_project()
        self.sample_and_approve(project)
        adapter = FakeProviderAdapter(canonical_wav_bytes(400), "audio/wav")

        def failed_asr(_project, _narration):
            raise voice_module.VoiceoverStateError("fixture ASR failure")

        self.assertEqual(
            voice_main(
                ["full", "--project", str(project.root)],
                adapter=adapter,
                asr_runner=failed_asr,
            ),
            2,
        )
        self.assertEqual(len(adapter.requests), 1)
        self.assertTrue(project.path("audio/narration.wav").is_file())
        manifest = json.loads(
            project.path("manifests/voice-manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual(manifest["alignment"]["status"], "waiting_alignment")
        self.assertEqual(manifest["runs"][-1]["status"], "waiting_alignment")

        no_call = FakeProviderAdapter(
            outcomes=[PermanentProviderError("provider must not rerun")]
        )

        def recovered_asr(_project, narration_path: Path) -> Path:
            self.assertEqual(narration_path, project.path("audio/narration.wav"))
            source_cues = voice_module.parse_srt(
                project.path("source/source.srt").read_text(encoding="utf-8-sig")
            )
            path = project.path(".work/recovered-asr/result.srt")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                voice_module.serialize_srt(
                    [
                        {
                            "originalIndex": index,
                            "startMs": (index - 1) * 200,
                            "endMs": index * 200,
                            "text": cue["text"],
                        }
                        for index, cue in enumerate(source_cues, start=1)
                    ]
                ),
                encoding="utf-8",
            )
            return path

        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(
                voice_main(
                    ["full", "--project", str(project.root), "--retry-failed"],
                    adapter=no_call,
                    asr_runner=recovered_asr,
                ),
                0,
            )
        self.assertEqual(no_call.requests, [])
        self.assertIn("FULL_IDENTITY=", output.getvalue())

    def test_agent_approval_approve_full_derives_only_agent_first(self) -> None:
        project = self.make_project(agent_approval_enabled=True)
        self.sample_and_approve(project)
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(
                voice_main(
                    ["full", "--project", str(project.root)],
                    adapter=FakeProviderAdapter(canonical_wav_bytes(400), "audio/wav"),
                ),
                0,
            )
        full_identity = self.publish_alignment(project)
        manifest_path = project.path("manifests/voice-manifest.json")
        timing_before = project.timing_plan_path.read_bytes()
        manifest_before = manifest_path.read_bytes()

        self.assertEqual(
            voice_main([
                "approve-full", "--project", str(project.root),
                "--identity-hash", full_identity,
                "--review-policy", "user_first",
            ]),
            2,
        )
        self.assertEqual(project.timing_plan_path.read_bytes(), timing_before)
        self.assertEqual(manifest_path.read_bytes(), manifest_before)

        approval_output = io.StringIO()
        with redirect_stdout(approval_output):
            self.assertEqual(
                voice_main([
                    "approve-full", "--project", str(project.root),
                    "--identity-hash", full_identity,
                ]),
                0,
            )
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["fullApproval"]["reviewPolicy"], "agent_first")
        self.assertIn("REVIEW_POLICY=agent_first", approval_output.getvalue())
        self.assertEqual(
            workspace_module.resolve_project_review_policy(project), "agent_first"
        )
        with self.assertRaisesRegex(workspace_module.ProjectValidationError, "冲突"):
            workspace_module.resolve_project_review_policy(project, "user_first")

    def test_minimax_mode_uses_shared_sample_full_timeline_gate_with_fake_adapter(self) -> None:
        project = self.make_project(voiceover_mode="minimax")
        adapter = FakeProviderAdapter(canonical_wav_bytes(), "audio/wav")
        output = io.StringIO()
        with mock.patch.object(voice_module, "active_provider_id", return_value="minimax"), redirect_stdout(output):
            self.assertEqual(
                voice_main(["sample", "--project", str(project.root)], adapter=adapter), 0
            )
        sample_identity = next(line.split("=", 1)[1] for line in output.getvalue().splitlines() if line.startswith("SAMPLE_IDENTITY="))
        self.assertEqual(voice_main(["approve-sample", "--project", str(project.root), "--identity-hash", sample_identity]), 0)
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(voice_main(
                ["full", "--project", str(project.root)],
                adapter=FakeProviderAdapter(canonical_wav_bytes(400), "audio/wav"),
            ), 0)
        full_identity = self.publish_alignment(project)
        self.assertEqual(voice_main([
            "approve-full", "--project", str(project.root), "--identity-hash", full_identity,
            "--review-policy", "user_first",
        ]), 0)
        plan = json.loads(project.path("planning/voice-plan.json").read_text(encoding="utf-8"))
        timing = json.loads(project.path("planning/timing-plan.json").read_text(encoding="utf-8"))
        self.assertEqual(plan["mode"], "minimax")
        self.assertEqual(timing["activeTimeline"]["kind"], "audio-authoritative-timeline")

    def test_doubao_mode_uses_shared_sample_full_timeline_gate_with_fake_adapter(self) -> None:
        project = self.make_project(voiceover_mode="doubao")
        adapter = FakeProviderAdapter(canonical_wav_bytes(), "audio/wav")
        provider_config = {
            "id": "doubao",
            "protocol": "Doubao",
            "contractVersion": "doubao-seed-audio-http-v1",
            "voice": "speaker-fixture",
            "language": "zh-CN",
            "rate": "+0%",
            "pitch": "+0Hz",
            "volume": "+0%",
            "outputFormat": "audio-24khz-mono-wav",
            "model": "seed-audio-1.0",
        }
        output = io.StringIO()
        with (
            mock.patch.object(voice_module, "active_provider_id", return_value="doubao"),
            mock.patch.object(
                voice_module,
                "load_voice_provider_config",
                return_value=provider_config,
            ),
            redirect_stdout(output),
        ):
            self.assertEqual(
                voice_main(["sample", "--project", str(project.root)], adapter=adapter),
                0,
            )
        sample_identity = next(
            line.split("=", 1)[1]
            for line in output.getvalue().splitlines()
            if line.startswith("SAMPLE_IDENTITY=")
        )
        self.assertEqual(
            voice_main(
                [
                    "approve-sample",
                    "--project",
                    str(project.root),
                    "--identity-hash",
                    sample_identity,
                ]
            ),
            0,
        )
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(
                voice_main(
                    ["full", "--project", str(project.root)],
                    adapter=FakeProviderAdapter(canonical_wav_bytes(400), "audio/wav"),
                ),
                0,
            )
        full_identity = self.publish_alignment(project)
        self.assertEqual(
            voice_main(
                [
                    "approve-full",
                    "--project",
                    str(project.root),
                    "--identity-hash",
                    full_identity,
                    "--review-policy",
                    "user_first",
                ]
            ),
            0,
        )
        plan = json.loads(
            project.path("planning/voice-plan.json").read_text(encoding="utf-8")
        )
        timing = json.loads(
            project.path("planning/timing-plan.json").read_text(encoding="utf-8")
        )
        self.assertEqual(plan["mode"], "doubao")
        self.assertEqual(plan["provider"]["protocol"], "Doubao")
        self.assertEqual(
            timing["activeTimeline"]["kind"], "audio-authoritative-timeline"
        )

    def test_sample_without_provider_uses_active_provider_but_respects_project_mode(self) -> None:
        project = self.make_project(voiceover_mode="minimax")
        adapter = FakeProviderAdapter(canonical_wav_bytes(), "audio/wav")
        output = io.StringIO()
        with mock.patch.object(voice_module, "active_provider_id", return_value="minimax"), redirect_stdout(output):
            self.assertEqual(voice_main(["sample", "--project", str(project.root)], adapter=adapter), 0)
        self.assertIn("SAMPLE_AUDIO=", output.getvalue())
        plan = json.loads(project.path("planning/voice-plan.json").read_text(encoding="utf-8"))
        self.assertEqual(plan["mode"], "minimax")

    def test_sample_cli_has_no_provider_override_entry(self) -> None:
        with self.assertRaises(SystemExit):
            voice_module._parser().parse_args([
                "sample", "--project", "C:/project", "--provider", "minimax"
            ])

    def test_retry_failed_only_requests_unfinished_segment_and_classifies_failures(self) -> None:
        project = self.make_project()
        self.sample_and_approve(project)
        first = FakeProviderAdapter(outcomes=[RetryableProviderError("timeout exhausted")])
        self.assertEqual(voice_main(["full", "--project", str(project.root)], adapter=first), 3)
        manifest = json.loads(project.path("manifests/voice-manifest.json").read_text(encoding="utf-8"))
        self.assertEqual([item["status"] for item in manifest["segments"]], ["failed"])
        retry = FakeProviderAdapter(canonical_wav_bytes(), "audio/wav")
        self.assertEqual(
            voice_main(["full", "--project", str(project.root), "--retry-failed"], adapter=retry), 0
        )
        self.assertEqual(len(retry.requests), 1)

        bad = self.make_project()
        self.assertEqual(
            voice_main(
                ["sample", "--project", str(bad.root)],
                adapter=FakeProviderAdapter(outcomes=[PermanentProviderError("invalid voice")]),
            ),
            2,
        )
        bad_media = self.make_project()
        self.assertEqual(
            voice_main(
                ["sample", "--project", str(bad_media.root)],
                adapter=FakeProviderAdapter(b"not-media", "audio/mpeg"),
            ),
            4,
        )
        cancelled = self.make_project()
        self.assertEqual(
            voice_main(
                ["sample", "--project", str(cancelled.root)],
                adapter=FakeProviderAdapter(outcomes=[CancelledError("user cancelled")]),
            ),
            1,
        )

    def test_status_is_read_only(self) -> None:
        project = self.make_project()
        self.sample_and_approve(project)
        before = sha256_file(project.path("manifests/voice-manifest.json"))
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(voice_main(["status", "--project", str(project.root)]), 0)
        self.assertIn('"approved": true', output.getvalue())
        self.assertEqual(sha256_file(project.path("manifests/voice-manifest.json")), before)

    def test_voice_generation_rolling_window_stops_dispatch_and_keeps_inflight_success(self) -> None:
        project = self.make_project(cue_count=4)
        self.sample_and_approve(project)

        def delayed_failure(_request):
            time.sleep(0.01)
            raise RetryableProviderError("fixture first failure")

        def delayed_success(_request):
            time.sleep(0.05)
            return RawAudioResult(canonical_wav_bytes(), "audio/wav", "safe-fixture")

        adapter = FakeProviderAdapter(outcomes=[delayed_failure, delayed_success])
        code = voice_main(
            ["full", "--project", str(project.root)],
            adapter=adapter,
            workspace_config=self.execution_config(project, voice_generation=2),
        )
        self.assertEqual(code, 3)
        self.assertEqual(len(adapter.requests), 1)
        manifest = json.loads(project.path("manifests/voice-manifest.json").read_text(encoding="utf-8"))
        self.assertEqual([item["status"] for item in manifest["segments"]], ["failed"])
        self.assertEqual(manifest["runs"][-1]["configuredConcurrency"], 2)
        self.assertEqual(manifest["runs"][-1]["effectiveConcurrency"], 1)

    def test_voice_generation_four_way_completion_order_keeps_unit_timeline_order(self) -> None:
        project = self.make_project(cue_count=4)
        self.sample_and_approve(project)
        active = 0
        peak = 0
        lock = threading.Lock()

        def outcome(delay):
            def synthesize(_request):
                nonlocal active, peak
                with lock:
                    active += 1
                    peak = max(peak, active)
                try:
                    time.sleep(delay)
                    return RawAudioResult(canonical_wav_bytes(), "audio/wav")
                finally:
                    with lock:
                        active -= 1
            return synthesize

        adapter = FakeProviderAdapter(outcomes=[outcome(delay) for delay in (0.08, 0.06, 0.04, 0.02)])
        self.assertEqual(
            voice_main(
                ["full", "--project", str(project.root)],
                adapter=adapter,
                workspace_config=self.execution_config(project, voice_generation=4),
            ),
            0,
        )
        self.assertEqual(peak, 1)
        self.assertEqual(len(adapter.requests), 1)
        self.assertFalse(project.path("audio/timeline.json").exists())
        manifest = json.loads(project.path("manifests/voice-manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["runs"][-1]["effectiveConcurrency"], 1)
        self.assertEqual(manifest["runs"][-1]["status"], "waiting_alignment")

    def test_candidate_ready_and_publishing_recover_without_provider_calls(self) -> None:
        for state in ("candidate_ready", "publishing"):
            with self.subTest(state=state):
                project = self.make_project()
                self.sample_and_approve(project)
                self.assertEqual(
                    voice_main(["full", "--project", str(project.root)], adapter=FakeProviderAdapter(canonical_wav_bytes(), "audio/wav")),
                    0,
                )
                manifest_path = project.path("manifests/voice-manifest.json")
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                segment = manifest["segments"][0]
                segment["status"] = state
                segment["currentAttempt"]["status"] = state
                project.path(segment["relativePath"]).unlink()
                manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
                no_call = FakeProviderAdapter(outcomes=[PermanentProviderError("provider must not run")])
                self.assertEqual(
                    voice_main(["full", "--project", str(project.root), "--retry-failed"], adapter=no_call),
                    0,
                )
                self.assertEqual(no_call.requests, [])

    def test_requesting_without_candidate_becomes_unknown_and_never_retries(self) -> None:
        project = self.make_project()
        self.sample_and_approve(project)
        manifest_path = project.path("manifests/voice-manifest.json")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        segment = manifest["segments"][0]
        segment["status"] = "requesting"
        segment["attempts"] = 1
        segment["currentAttempt"] = {
            "attemptId": "unit-0001-attempt-0001",
            "status": "requesting",
            "inputIdentitySha256": segment["voiceSynthesisIdentityHash"],
            "candidateFile": ".work/missing/u0001-a0001.wav",
            "candidateSha256": None,
            "candidateBytes": None,
            "validatorReceipt": None,
            "formalFile": segment["relativePath"],
            "externalOutcome": "requesting",
            "providerReceipt": None,
        }
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        no_call = FakeProviderAdapter(outcomes=[PermanentProviderError("provider must not run")])
        self.assertEqual(
            voice_main(["full", "--project", str(project.root), "--retry-failed"], adapter=no_call),
            5,
        )
        self.assertEqual(no_call.requests, [])
        updated = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(updated["segments"][0]["status"], "unknown_external_outcome")

    def test_voice_validation_receipts_use_binding_and_force_deep_refreshes(self) -> None:
        project = self.make_project()
        self.sample_and_approve(project)
        self.assertEqual(
            voice_main(
                ["full", "--project", str(project.root)],
                adapter=FakeProviderAdapter(canonical_wav_bytes(), "audio/wav"),
            ),
            0,
        )
        self.publish_alignment(project)
        manifest_path = project.path("manifests/voice-manifest.json")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        sample_identity = manifest["sample"]["identityHash"]
        real_validate = voice_module.validate_canonical_wav
        with mock.patch.object(
            voice_module, "_validate_current_sample", return_value=sample_identity
        ), mock.patch.object(
            voice_module, "validate_canonical_wav", wraps=real_validate
        ) as deep:
            voice_module.validate_current_voiceover(project, persist_deep=True)
            self.assertEqual(deep.call_count, 0)
            voice_module.validate_current_voiceover(
                project, force_deep=True, persist_deep=True
            )
            self.assertEqual(deep.call_count, 2)  # one full-track segment plus composite

        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertTrue(all(
            segment["currentAttempt"]["validatorReceipt"]["contractVersion"]
            == voice_module.CANONICAL_WAV_VALIDATOR_RECEIPT_VERSION
            for segment in manifest["segments"]
        ))
        self.assertEqual(
            manifest["composite"]["validatorReceipt"]["contractVersion"],
            voice_module.CANONICAL_WAV_VALIDATOR_RECEIPT_VERSION,
        )
        manifest["segments"][0]["currentAttempt"]["validatorReceipt"] = None
        manifest["composite"]["validatorReceipt"] = None
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        with mock.patch.object(
            voice_module, "_validate_current_sample", return_value=sample_identity
        ), mock.patch.object(
            voice_module, "validate_canonical_wav", wraps=real_validate
        ) as deep:
            voice_module.validate_current_voiceover(project, persist_deep=True)
            self.assertEqual(deep.call_count, 2)
            voice_module.validate_current_voiceover(project, persist_deep=True)
            self.assertEqual(deep.call_count, 2)

    def test_worker_validated_candidate_tamper_fails_before_formal_publish(self) -> None:
        project = self.make_project(cue_count=1)
        self.sample_and_approve(project)
        original = voice_module._synthesize_candidate_worker

        def tamper(**kwargs):
            outcome = original(**kwargs)
            if "result" in outcome:
                kwargs["candidate"].write_bytes(kwargs["candidate"].read_bytes() + b"tamper")
            return outcome

        with mock.patch.object(voice_module, "_synthesize_candidate_worker", side_effect=tamper):
            self.assertNotEqual(
                voice_main(
                    ["full", "--project", str(project.root)],
                    adapter=FakeProviderAdapter(canonical_wav_bytes(), "audio/wav"),
                ),
                0,
            )
        self.assertFalse(project.path("audio/segments/unit-0001.wav").exists())


if __name__ == "__main__":
    unittest.main()
