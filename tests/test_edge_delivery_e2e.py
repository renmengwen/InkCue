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
import wave
from pathlib import Path
from unittest import mock

from PIL import Image, ImageDraw


SKILL_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = SKILL_ROOT / "scripts"
RUNTIME_PY = Path(r"D:\SRTWhiteboard\runtime\.venv\Scripts\python.exe")
PROJECT_NAME = "批次 E Fake Edge '中文 路径' 验收"
FIXTURE_ID = "edge-delivery-e2e-synthetic-v1"
TEST_ROOT = Path(tempfile.gettempdir()) / "swe2e"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import media_validation  # noqa: E402
import prepare_source  # noqa: E402
import project_workspace  # noqa: E402
import generate_annotation_previews  # noqa: E402
import generate_voiceover  # noqa: E402
from generate_voiceover import main as voice_main  # noqa: E402
from voiceover import FakeProviderAdapter, PermanentProviderError  # noqa: E402


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _canonical_wav_bytes(duration_ms: int) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(24000)
        wav.writeframes(b"\x00\x00" * (24 * duration_ms))
    return output.getvalue()


def _identity(stdout: str, prefix: str) -> str:
    return next(
        line.split("=", 1)[1]
        for line in stdout.splitlines()
        if line.startswith(prefix + "=")
    )


class EdgeDeliveryE2ETests(unittest.TestCase):
    """Persistent, offline-only acceptance fixture for the formal Edge delivery path."""

    maxDiff = None

    def setUp(self) -> None:
        self._provider_patcher = mock.patch.object(
            generate_voiceover, "active_provider_id", return_value="edge-tts"
        )
        self._provider_patcher.start()
        self.assertTrue(RUNTIME_PY.is_file(), f"固定运行时不存在: {RUNTIME_PY}")
        TEST_ROOT.mkdir(parents=True, exist_ok=True)
        self.root = Path(tempfile.mkdtemp(prefix="run-", dir=TEST_ROOT)).resolve()
        self.projects_root = self.root / "projects"
        self.project_root = self.projects_root / PROJECT_NAME
        self.projects_root.mkdir(parents=True)
        self.commands: list[list[str]] = []
        self.generation_plan_sha_before_voice: str | None = None

    def tearDown(self) -> None:
        self._provider_patcher.stop()
        if self.root.exists():
            self.root.relative_to(TEST_ROOT.resolve())
            shutil.rmtree(self.root, ignore_errors=True)

    def _write_fixture_evidence(self, **updates: object) -> None:
        path = self.project_root / "manifests" / "synthetic-fixture-approvals.json"
        evidence: dict[str, object] = {
            "schemaVersion": 1,
            "fixtureId": FIXTURE_ID,
            "fixtureKind": "synthetic test fixture",
            "networkUsed": False,
            "realEdgeServiceAcceptance": "not_run",
            "finalHumanApproval": None,
        }
        if path.is_file():
            evidence.update(json.loads(path.read_text(encoding="utf-8")))
        evidence.update(updates)
        project_workspace.write_json_atomic(path, evidence)

    def _create_project(self):
        runtime_temp = self.root / "runtime"
        runtime_temp.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="edge-e2e-source-", dir=runtime_temp) as temp:
            draft = {
                "schemaVersion": 1,
                "contractVersion": "whiteboard-content-draft-v1",
                "inputMode": "topic",
                "topic": "太阳升起后树苗如何成长",
                "body": None,
                "rewritePolicy": "generate",
                "targetDurationSeconds": 15,
                "voiceoverMode": "edge-tts",
                "narrationCues": [
                    {
                        "cueId": "cue-001",
                        "sceneId": "scene-01",
                        "text": "第一幕：太阳升起。",
                    },
                    {
                        "cueId": "cue-002",
                        "sceneId": "scene-02",
                        "text": "第二幕：树苗成长。",
                    },
                ],
                "scenes": [
                    {
                        "sceneId": "scene-01",
                        "name": "太阳升起",
                        "coreIdea": "阳光为生长提供能量",
                        "visualSubject": "暖色太阳从地平线升起",
                        "imagePrompt": "暖米黄纸张上的太阳与地平线白板手绘，无文字",
                    },
                    {
                        "sceneId": "scene-02",
                        "name": "树苗成长",
                        "coreIdea": "树苗在阳光下向上生长",
                        "visualSubject": "同一株树苗长出新叶",
                        "imagePrompt": "暖米黄纸张上的树苗生长白板手绘，无文字",
                    },
                ],
            }
            draft_path = Path(temp) / "approved-content-draft.json"
            draft_path.write_text(
                json.dumps(draft, ensure_ascii=False, indent=2),
                encoding="utf-8",
                newline="\n",
            )
            package = prepare_source.prepare_source(
                draft_path,
                Path(temp) / "source-package",
            )
            workspace = project_workspace.ProjectWorkspace(
                project_workspace.WorkspaceConfig(
                    root=self.root,
                    config_path=self.root / "workspace.fixture.json",
                )
            )
            project = workspace.create_project(
                PROJECT_NAME,
                package.directory / "source.srt",
                confirmed_plan=package.generation_plan,
                voiceover_mode="edge-tts",
                source_input=package.directory / "input.json",
                source_manifest=package.directory / "manifest.json",
                source_plan=package.directory / "generation-plan.json",
            )
            self.generation_plan_sha_before_voice = _sha256(project.plan_path)
        self._write_fixture_evidence(
            approvals={
                "contentDraft": "synthetic test fixture only",
                "sample": "synthetic test fixture only",
                "fullNarration": "synthetic test fixture only",
                "scenes": "synthetic test fixture only",
                "cleanMaster": "synthetic test fixture only",
            }
        )
        return project

    def _sample(self, *, rate: int = 0) -> str:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = voice_main(
                ["sample", "--project", str(self.project_root), "--rate", str(rate)],
                adapter=FakeProviderAdapter(_canonical_wav_bytes(1017), "audio/wav"),
            )
        self.assertEqual(code, 0)
        return _identity(output.getvalue(), "SAMPLE_IDENTITY")

    def _full(self, *, retry: bool = False, adapter=None) -> str:
        output = io.StringIO()
        argv = ["full", "--project", str(self.project_root)]
        if retry:
            argv.append("--retry-failed")
        source_cues = generate_voiceover.parse_srt(
            (self.project_root / "source" / "source.srt").read_text(encoding="utf-8-sig")
        )

        def asr_runner(_project, _narration_path: Path) -> Path:
            asr_path = self.project_root / ".work" / "e2e-asr" / "result.srt"
            asr_path.parent.mkdir(parents=True, exist_ok=True)
            asr_path.write_text(
                generate_voiceover.serialize_srt(
                    [
                        {
                            "originalIndex": index,
                            "startMs": (index - 1) * 1017,
                            "endMs": index * 1017,
                            "text": cue["text"],
                        }
                        for index, cue in enumerate(source_cues, start=1)
                    ]
                ),
                encoding="utf-8",
            )
            return asr_path

        with contextlib.redirect_stdout(output):
            code = voice_main(
                argv,
                adapter=adapter
                or FakeProviderAdapter(
                    _canonical_wav_bytes(1017 * len(source_cues)), "audio/wav"
                ),
                asr_runner=asr_runner,
            )
        self.assertEqual(code, 0)
        return _identity(output.getvalue(), "FULL_IDENTITY")

    def _prepare_current_approved_fake_voice(self) -> None:
        sample_identity = self._sample()
        self.assertEqual(
            voice_main(
                [
                    "approve-sample",
                    "--project",
                    str(self.project_root),
                    "--identity-hash",
                    sample_identity,
                ]
            ),
            0,
        )
        initial_full_identity = self._full()

        no_request = FakeProviderAdapter(
            outcomes=[PermanentProviderError("validated segment must be reused")]
        )
        self.assertEqual(self._full(retry=True, adapter=no_request), initial_full_identity)
        self.assertEqual(no_request.requests, [])

        self._sample(rate=10)
        stale = json.loads(
            (self.project_root / "manifests" / "voice-manifest.json").read_text(encoding="utf-8")
        )
        self.assertFalse(stale["sample"]["approval"]["approved"])
        self.assertFalse(stale["fullApproval"]["approved"])
        self.assertTrue(all(item["status"] == "pending" for item in stale["segments"]))

        current_sample_identity = self._sample(rate=0)
        self.assertEqual(
            voice_main(
                [
                    "approve-sample",
                    "--project",
                    str(self.project_root),
                    "--identity-hash",
                    current_sample_identity,
                ]
            ),
            0,
        )
        current_full_identity = self._full()
        self.assertEqual(
            voice_main(
                [
                    "approve-full",
                    "--project",
                    str(self.project_root),
                    "--identity-hash",
                    current_full_identity,
                    "--duration-decision",
                    "accept_actual",
                    "--review-policy",
                    "user_first",
                ]
            ),
            0,
        )
        self._write_fixture_evidence(
            voiceApprovalEvidence={
                "approvalBasis": "synthetic test fixture only",
                "approvedBy": "automated fixture harness, not a human user",
                "sampleIdentityHash": current_sample_identity,
                "fullIdentityHash": current_full_identity,
                "notARealEdgeAcceptance": True,
            }
        )

    def _write_scene_assets(self) -> None:
        project = project_workspace.load_project(self.project_root)
        timing_sha = _sha256(project.timing_plan_path)
        render_sha = project_workspace.sha256_json(project.render_profile)
        audio_sha = _sha256(project.path("audio/narration.wav"))
        active = project.timing_plan["activeTimeline"]
        colors = (("#D99B45", "#5A4432"), ("#7A9B55", "#385640"))

        for generation_scene, timing_scene, (fill, ink) in zip(
            project.plan["scenes"], project.timing_plan["scenes"], colors, strict=True
        ):
            image_path = project.path(Path("scenes") / generation_scene["outputFile"])
            image = Image.new("RGB", (1920, 1080), "#F5EBD7")
            draw = ImageDraw.Draw(image)
            draw.ellipse((170, 130, 590, 550), fill=fill, outline=ink, width=16)
            draw.line((380, 550, 380, 800), fill=ink, width=20)
            image.save(image_path)

            annotation = {
                "sceneId": timing_scene["sceneId"],
                "canvas": {"width": 1920, "height": 1080},
                "sceneDurationMs": timing_scene["sceneDurationMs"],
                "timingPlanSha256": timing_sha,
                "renderProfileSha256": render_sha,
                "sceneFrameRange": {
                    "startFrame": timing_scene["startFrame"],
                    "endFrameExclusive": timing_scene["endFrameExclusive"],
                    "frameCount": timing_scene["frameCount"],
                },
                "timingSource": {
                    "kind": active["kind"],
                    "timelineFile": active["file"],
                    "timelineSha256": active["sha256"],
                    "audioSha256": audio_sha,
                    "sceneId": timing_scene["sceneId"],
                    "sceneStartMs": timing_scene["startMs"],
                    "sceneEndMs": timing_scene["endMs"],
                },
                "elements": [
                    {
                        "id": "fixture-main-subject",
                        "sequence": 1,
                        "label": "合成验收主体",
                        "narrativeRole": "synthetic test fixture subject",
                        "subtitle": generation_scene["sceneId"],
                        "region": {"x": 140, "y": 100, "width": 500, "height": 740},
                        "reveal": {
                            "startMs": 50,
                            "durationMs": 417,
                            "protectedRegions": [],
                            "direction": "left-to-right",
                        },
                        "handPath": {"start": [160, 130], "end": [610, 810]},
                    }
                ],
            }
            annotation_path = image_path.with_suffix(".annotation.json")
            project_workspace.write_json_atomic(annotation_path, annotation)

    def _run_script(self, script_name: str, *args: str) -> subprocess.CompletedProcess[str]:
        argv = [str(RUNTIME_PY), str(SCRIPTS_DIR / script_name), *args]
        self.assertNotIn("-short" + "est", argv)
        self.commands.append(argv)
        if script_name == "generate_annotation_previews.py":
            stdout = io.StringIO()
            stderr = io.StringIO()
            workspace = project_workspace.WorkspaceConfig(
                root=self.root,
                config_path=self.root / "workspace.fixture.json",
                concurrency=project_workspace.ExecutionConcurrency(
                    default=1,
                    annotation_preview=2,
                ),
            )
            with mock.patch.object(
                generate_annotation_previews,
                "load_workspace_config",
                return_value=workspace,
            ), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                returncode = generate_annotation_previews.main(list(args))
            completed = subprocess.CompletedProcess(
                argv,
                returncode,
                stdout.getvalue(),
                stderr.getvalue(),
            )
        elif script_name in {"scene_review.py", "approve_scene_review.py"}:
            import approve_scene_review
            import scene_review

            stdout = io.StringIO()
            stderr = io.StringIO()
            workspace = project_workspace.ProjectWorkspace(
                project_workspace.WorkspaceConfig(
                    root=self.root,
                    config_path=self.root / "workspace.fixture.json",
                )
            )
            module = scene_review if script_name == "scene_review.py" else approve_scene_review
            with mock.patch.object(
                module.ProjectWorkspace,
                "from_config",
                return_value=workspace,
            ), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                returncode = module.main(list(args))
            completed = subprocess.CompletedProcess(
                argv,
                returncode,
                stdout.getvalue(),
                stderr.getvalue(),
            )
        elif script_name == "merge_scenes.py":
            import merge_scenes

            stdout = io.StringIO()
            stderr = io.StringIO()
            workspace = project_workspace.ProjectWorkspace(
                project_workspace.WorkspaceConfig(
                    root=self.root,
                    config_path=self.root / "workspace.fixture.json",
                    concurrency=project_workspace.ExecutionConcurrency(
                        default=1,
                        scene_media_validation=2,
                    ),
                )
            )
            with mock.patch.object(
                merge_scenes.ProjectWorkspace,
                "from_config",
                return_value=workspace,
            ), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                returncode = merge_scenes.main(list(args))
            completed = subprocess.CompletedProcess(
                argv,
                returncode,
                stdout.getvalue(),
                stderr.getvalue(),
            )
        else:
            completed = subprocess.run(
                argv,
                cwd=SKILL_ROOT,
                shell=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        self.assertEqual(
            completed.returncode,
            0,
            f"command failed: {argv}\nstdout:\n{completed.stdout}\nstderr:\n{completed.stderr}",
        )
        return completed

    def _persist_command_log(self) -> None:
        def portable(value: str) -> str:
            replacements = (
                (str(RUNTIME_PY), "<RUNTIME_PY>"),
                (str(SCRIPTS_DIR), "<SCRIPTS_DIR>"),
                (str(self.project_root), "<PROJECT>"),
            )
            for before, after in replacements:
                value = value.replace(before, after)
            return value

        project_workspace.write_json_atomic(
            self.project_root / "manifests" / "e2e-command-log.json",
            {
                "schemaVersion": 1,
                "fixtureId": FIXTURE_ID,
                "shell": False,
                "containsShortest": False,
                "commands": [[portable(item) for item in argv] for argv in self.commands],
            },
        )

    def test_topic_fake_edge_delivery_e2e(self) -> None:
        self._create_project()
        self._prepare_current_approved_fake_voice()
        self._write_scene_assets()

        project = project_workspace.load_project(self.project_root)
        self.assertIn("contentSource", project.metadata)
        self.assertEqual(project.metadata["contentSource"]["inputFile"], "source/input.json")
        self.assertEqual(
            project.metadata["contentSource"]["manifestFile"],
            "source/source-manifest.json",
        )
        self.assertEqual(project.plan["scenes"][-1]["subtitleRange"]["endMs"], 15000)
        self.assertIsNotNone(self.generation_plan_sha_before_voice)
        timeline = json.loads(project.path("audio/timeline.json").read_text(encoding="utf-8"))
        self.assertEqual(timeline["audio"]["durationMs"], 2034)
        self.assertEqual(timeline["scenes"][0]["endMs"], 1017)
        self.assertNotEqual(timeline["scenes"][0]["endMs"] * 60 % 1000, 0)
        self.assertEqual(
            [item["frameCount"] for item in project.timing_plan["scenes"]], [62, 61]
        )
        self.assertEqual(project.timing_plan["scenes"][-1]["endMs"], 2034)
        self.assertEqual(
            _sha256(project.plan_path), self.generation_plan_sha_before_voice
        )
        self.assertFalse(project.path("previews/narration-review.ass").exists())
        self.assertFalse(project.path("previews/narration-review.mp4").exists())
        voice_manifest = json.loads(
            project.path("manifests/voice-manifest.json").read_text(encoding="utf-8")
        )
        self.assertNotIn("reviewIdentityHash", voice_manifest["fullApproval"])
        self.assertEqual(
            voice_manifest["fullApproval"]["durationDecision"], "accept_actual"
        )

        preview_result = self._run_script(
            "generate_annotation_previews.py",
            "--project",
            str(self.project_root),
            "--all",
        )
        preview_summary = json.loads(preview_result.stdout)
        self.assertEqual(preview_summary["status"], "PASS")
        self.assertEqual(
            preview_summary["nextHumanGate"], "annotation_review_confirmation"
        )
        annotation_review_identity = preview_summary[
            "annotationReviewIdentitySha256"
        ]
        self.assertEqual(len(annotation_review_identity), 64)
        self.assertFalse(preview_summary["approvalWritten"])
        self._run_script(
            "approve_annotation_review.py",
            "--project",
            str(self.project_root),
            "--identity-hash",
            annotation_review_identity,
        )
        self._write_fixture_evidence(
            annotationReviewApprovalEvidence={
                "approvalBasis": "synthetic test fixture only",
                "approvedBy": "automated fixture harness, not a human user",
                "identityHash": annotation_review_identity,
                "notARealHumanAcceptance": True,
            }
        )

        scene_outputs: list[Path] = []
        for generation_scene in project.plan["scenes"]:
            scene_id = generation_scene["sceneId"]
            output_file = f"{Path(generation_scene['outputFile']).stem}-whiteboard.mp4"
            self._run_script(
                "render_stream_whiteboard.py",
                "--project",
                str(self.project_root),
                "--scene-id",
                scene_id,
                "--bare-tip",
                "--grid-edge",
                "60",
                "--color-fill",
                "brush",
                "--pause",
                "off",
            )
            scene_outputs.append(self.project_root / "scenes" / output_file)

        scene_review_result = self._run_script(
            "scene_review.py",
            "--project",
            str(self.project_root),
        )
        scene_review_identity = _identity(
            scene_review_result.stdout, "SCENE_REVIEW_IDENTITY"
        )
        self._run_script(
            "approve_scene_review.py",
            "--project",
            str(self.project_root),
            "--identity-hash",
            scene_review_identity,
        )
        self._write_fixture_evidence(
            sceneReviewApprovalEvidence={
                "approvalBasis": "synthetic test fixture only",
                "approvedBy": "automated fixture harness, not a human user",
                "identityHash": scene_review_identity,
                "notARealHumanAcceptance": True,
            }
        )

        self._run_script(
            "merge_scenes.py",
            "--project",
            str(self.project_root),
            "--inputs",
            *(str(path) for path in scene_outputs),
        )
        self._run_script("burn_subtitles.py", "--project", str(self.project_root))
        self._run_script("mux_voiceover.py", "--project", str(self.project_root))
        self._run_script("validate_final_media.py", "--project", str(self.project_root))
        self._persist_command_log()

        final = self.project_root / "output" / "final.mp4"
        contact_sheet = self.project_root / "previews" / "final-subtitle-contact-sheet.png"
        self.assertTrue(final.is_file())
        self.assertTrue(contact_sheet.is_file())
        self.assertGreater(contact_sheet.stat().st_size, 0)

        probe = media_validation.probe_media(final)
        self.assertEqual(len(probe["streams"]["video"]), 1)
        self.assertEqual(len(probe["streams"]["audio"]), 1)
        video = probe["streams"]["video"][0]
        audio = probe["streams"]["audio"][0]
        self.assertEqual(video["codec"], "h264")
        self.assertEqual((video["width"], video["height"]), (1920, 1080))
        self.assertEqual(video["pixelFormat"], "yuv420p")
        self.assertEqual(video["fps"]["value"], 60.0)
        self.assertEqual(video["frameCount"], 123)
        self.assertEqual(audio["codec"], "aac")
        self.assertEqual(audio["sampleRate"], 24000)
        self.assertEqual(audio["channels"], 1)
        media_validation.full_decode(final)

        delivery = json.loads(
            (self.project_root / "manifests" / "delivery-manifest.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(delivery["timingPlan"]["frameCount"], 123)
        self.assertEqual(delivery["subtitles"]["sourceKind"], "edge-tts-narration-srt")
        self.assertEqual(delivery["subtitles"]["file"], "audio/narration.srt")
        self.assertEqual(
            delivery["subtitles"]["contactSheet"]["file"],
            "previews/final-subtitle-contact-sheet.png",
        )
        self.assertTrue(delivery["final"]["technicalValidation"]["fullDecode"])
        self.assertIsNone(delivery["finalApproval"])
        self.assertLessEqual(abs(probe["durationMs"] - timeline["audio"]["durationMs"]), 80)


if __name__ == "__main__":
    unittest.main()
