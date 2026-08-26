from __future__ import annotations

import json
import io
import os
import shutil
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock
from contextlib import redirect_stderr, redirect_stdout


SKILL_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = SKILL_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import prepare_env  # noqa: E402
import prepare_source  # noqa: E402
import project_workspace as workspace_module  # noqa: E402
import create_project  # noqa: E402
import upgrade_project as upgrade_project_cli  # noqa: E402
from project_workspace import (  # noqa: E402
    AGENT_ROLE_FIELDS,
    WORKER_STAGE_FIELDS,
    ExecutionAgentConcurrency,
    ExecutionConcurrency,
    ExecutionVideoEncoding,
    FIXED_CANVAS,
    FIXED_RENDER_PROFILE,
    PROJECT_PATHS_V1,
    PROJECT_PATHS_V2,
    ProjectValidationError,
    ProjectWorkspace,
    WorkspaceError,
    create_generation_plan,
    load_workspace_config,
    safe_project_path,
    sanitize_project_name,
    sha256_file,
    sha256_json,
    validate_generation_plan_data,
    validate_pre_project_generation_plan_data,
)


class ProjectWorkspaceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._temporary_root = tempfile.TemporaryDirectory(
            prefix=".test-project-workspace-",
            dir=str(SKILL_ROOT),
        )
        cls.test_runs_root = Path(cls._temporary_root.name).resolve()
        if cls.test_runs_root.drive.upper() != "C:":
            cls._temporary_root.cleanup()
            raise AssertionError(f"测试临时根必须位于 C 盘: {cls.test_runs_root}")

        def allow_only_test_root(path: Path) -> None:
            try:
                path.resolve().relative_to(cls.test_runs_root)
            except ValueError as exc:
                raise WorkspaceError(f"workspaceRoot 必须位于 D 盘，实际为: {path}") from exc

        cls._drive_patcher = mock.patch.object(
            workspace_module,
            "_require_d_drive",
            side_effect=allow_only_test_root,
        )
        cls._drive_patcher.start()
        cls.run_root = (cls.test_runs_root / str(uuid.uuid4())).resolve()
        cls.run_root.relative_to(cls.test_runs_root)
        cls.run_root.mkdir(parents=True, exist_ok=False)

    @classmethod
    def tearDownClass(cls) -> None:
        target = cls.run_root.resolve()
        relative = target.relative_to(cls.test_runs_root.resolve())
        if len(relative.parts) != 1 or not relative.name:
            raise AssertionError(f"拒绝清理非单次测试目录: {target}")
        if target.exists():
            shutil.rmtree(target)
        cls._drive_patcher.stop()
        cls._temporary_root.cleanup()

    def setUp(self) -> None:
        self.case_root = (self.run_root / str(uuid.uuid4())).resolve()
        self.case_root.relative_to(self.run_root)
        self.case_root.mkdir(parents=True, exist_ok=False)
        self.workspace_root = self.case_root / "workspace"
        self.config_path = self.case_root / "workspace.local.json"
        self.config_path.write_text(
            json.dumps(
                {"schemaVersion": 1, "workspaceRoot": str(self.workspace_root)},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        self.source_srt = self.case_root / "input.srt"
        self.source_srt.write_text(
            "1\n00:00:00,000 --> 00:00:03,000\n测试字幕\n",
            encoding="utf-8",
        )

    def workspace(self) -> ProjectWorkspace:
        return ProjectWorkspace.from_config(self.config_path)

    def new_project(self, name: str | None = None):
        return self.workspace().create_project(name or f"项目-{uuid.uuid4().hex}", self.source_srt)

    def content_package(self, label: str = "content-package"):
        return prepare_source.prepare_source(
            SKILL_ROOT / "examples" / "topic-habit-loop-content-draft.json",
            self.case_root / label,
        )

    def new_content_project(self, name: str | None = None, *, package_label: str = "content-package"):
        package = self.content_package(package_label)
        return self.workspace().create_project(
            name or f"内容项目-{uuid.uuid4().hex}",
            package.directory / "source.srt",
            confirmed_plan=package.generation_plan,
            voiceover_mode="edge-tts",
            source_input=package.directory / "input.json",
            source_manifest=package.directory / "manifest.json",
            source_plan=package.directory / "generation-plan.json",
        )

    def downgrade_to_v1(self, project) -> Path:
        metadata_path = project.path("project.json")
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        metadata["schemaVersion"] = 1
        metadata["paths"] = dict(PROJECT_PATHS_V1)
        metadata.pop("voiceoverMode", None)
        metadata.pop("agentApprovalEnabled", None)
        metadata.pop("imageGenerationMode", None)
        metadata.pop("renderProfile", None)
        metadata_path.write_text(json.dumps(metadata, ensure_ascii=False), encoding="utf-8")
        timing_path = project.path("planning/timing-plan.json")
        timing_path.unlink(missing_ok=True)
        return metadata_path

    def test_example_and_config_require_d_drive_absolute_writable_path(self) -> None:
        example = json.loads(
            (SKILL_ROOT / "config" / "workspace.example.json").read_text(encoding="utf-8")
        )
        self.assertEqual(example["schemaVersion"], 1)
        self.assertEqual(example["workspaceRoot"], r"D:\SRTWhiteboard")
        self.assertEqual(
            set(example["execution"]["agents"]),
            {"default", *AGENT_ROLE_FIELDS},
        )
        self.assertEqual(
            set(example["execution"]["concurrency"]),
            {"default", *WORKER_STAGE_FIELDS},
        )
        self.assertEqual(
            example["execution"]["videoEncoding"],
            {"subtitlePreset": "medium"},
        )
        self.assertEqual(example["execution"]["concurrency"]["sceneRender"], 1)
        config = load_workspace_config(self.config_path)
        self.assertEqual(config.root, self.workspace_root.resolve())
        self.assertTrue(config.root.is_dir())
        self.assertEqual(config.for_stage("imageGeneration"), 1)
        self.assertEqual(config.for_role("storyboardPlanning"), 1)
        self.assertEqual(config.for_role("contentDrafting"), 1)
        self.assertEqual(config.video_encoding.subtitle_preset, "medium")

        local_raw = json.loads(
            (SKILL_ROOT / "config" / "workspace.local.json").read_text(encoding="utf-8")
        )
        local_video = local_raw.get("execution", {}).get("videoEncoding", {})
        self.assertEqual(set(local_video) - {"subtitlePreset"}, set())
        self.assertEqual(local_video.get("subtitlePreset", "medium"), "medium")
        self.assertEqual(local_raw["execution"]["concurrency"]["sceneRender"], 5)
        local_raw["workspaceRoot"] = str(self.workspace_root)
        local_config_path = self.case_root / "workspace-local-test.json"
        local_config_path.write_text(json.dumps(local_raw), encoding="utf-8")
        local_config = load_workspace_config(
            local_config_path,
            verify_writable=False,
        )
        self.assertEqual(local_config.for_stage("sceneRender"), 5)

        relative_config = self.case_root / "relative.json"
        relative_config.write_text(
            json.dumps({"schemaVersion": 1, "workspaceRoot": "relative/path"}),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(WorkspaceError, "绝对路径"):
            load_workspace_config(relative_config)

        c_config = self.case_root / "c-drive.json"
        c_config.write_text(
            json.dumps({"schemaVersion": 1, "workspaceRoot": r"C:\forbidden"}),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(WorkspaceError, "D 盘"):
            load_workspace_config(c_config)

    def test_execution_pools_have_independent_defaults_and_all_overrides(self) -> None:
        raw = {
            "schemaVersion": 1,
            "workspaceRoot": str(self.workspace_root),
            "execution": {
                "videoEncoding": {"subtitlePreset": "fast"},
                "agents": {
                    "default": 3,
                    "contentDrafting": 2,
                    "storyboardPlanning": 4,
                    "visualReview": 5,
                    "annotationDrafting": 6,
                },
                "concurrency": {
                    "default": 7,
                    "imageGeneration": 8,
                    "voiceGeneration": 9,
                    "imageValidation": 10,
                    "voiceValidation": 11,
                    "annotationValidation": 12,
                    "annotationPreview": 13,
                    "sceneRender": 15,
                    "sceneMediaValidation": 14,
                    "finalMediaValidation": 16,
                },
            },
        }
        self.config_path.write_text(json.dumps(raw), encoding="utf-8")
        config = load_workspace_config(self.config_path)
        for stage, expected in {
            "imageGeneration": 8,
            "voiceGeneration": 9,
            "imageValidation": 10,
            "voiceValidation": 11,
            "annotationValidation": 12,
            "annotationPreview": 13,
            "sceneRender": 15,
            "sceneMediaValidation": 14,
            "finalMediaValidation": 16,
        }.items():
            self.assertEqual(config.for_stage(stage), expected)
        for role, expected in {
            "contentDrafting": 2,
            "storyboardPlanning": 4,
            "visualReview": 5,
            "annotationDrafting": 6,
        }.items():
            self.assertEqual(config.for_role(role), expected)
        self.assertEqual(config.video_encoding.subtitle_preset, "fast")

        raw["execution"] = {
            "agents": {"default": 6},
            "concurrency": {"sceneRender": 5},
        }
        self.config_path.write_text(json.dumps(raw), encoding="utf-8")
        config = load_workspace_config(self.config_path)
        self.assertEqual(config.for_role("visualReview"), 6)
        self.assertEqual(config.for_stage("imageGeneration"), 1)
        self.assertEqual(config.for_stage("sceneRender"), 5)
        self.assertEqual(config.video_encoding.subtitle_preset, "medium")

        raw["execution"] = {
            "agents": {"annotationDrafting": 5},
            "concurrency": {"default": 4},
        }
        self.config_path.write_text(json.dumps(raw), encoding="utf-8")
        config = load_workspace_config(self.config_path)
        self.assertEqual(config.for_role("visualReview"), 1)
        self.assertEqual(config.for_role("annotationDrafting"), 5)
        self.assertEqual(config.for_stage("imageGeneration"), 4)
        self.assertEqual(config.for_stage("sceneRender"), 4)
        self.assertEqual(config.video_encoding.subtitle_preset, "medium")

        for preset in ("medium", "fast", "veryfast"):
            with self.subTest(subtitle_preset=preset):
                raw["execution"] = {"videoEncoding": {"subtitlePreset": preset}}
                self.config_path.write_text(json.dumps(raw), encoding="utf-8")
                config = load_workspace_config(self.config_path)
                self.assertEqual(config.video_encoding.subtitle_preset, preset)

    def test_execution_schema_rejects_invalid_values_and_unknowns(self) -> None:
        invalid_pools = [
            {"concurrency": {"imageGeneration": value}}
            for value in (0, -1, 17, 1.5, "2", True)
        ] + [
            {"agents": {"storyboardPlanning": value}}
            for value in (0, -1, 17, 1.5, "2", False)
        ] + [
            {"concurrency": {"imageGeneraton": 2}},
            {"agents": {"storyboardPlaning": 2}},
            {"unknownPool": {}},
            {"agents": None},
            {"concurrency": None},
            {"videoEncoding": None},
            {"videoEncoding": {"subtitlePreset": None}},
            {"videoEncoding": {"subtitlePreset": True}},
            {"videoEncoding": {"subtitlePreset": 1}},
            {"videoEncoding": {"subtitlePreset": "slow"}},
            {"videoEncoding": {"subtitlePreset": "MEDIUM"}},
            {"videoEncoding": {"unknown": "medium"}},
        ]
        for index, execution in enumerate(invalid_pools):
            with self.subTest(index=index, execution=execution):
                path = self.case_root / f"invalid-execution-{index}.json"
                path.write_text(
                    json.dumps(
                        {
                            "schemaVersion": 1,
                            "workspaceRoot": str(self.workspace_root),
                            "execution": execution,
                        }
                    ),
                    encoding="utf-8",
                )
                with self.assertRaises(WorkspaceError):
                    load_workspace_config(path, verify_writable=False)

        null_execution = self.case_root / "null-execution.json"
        null_execution.write_text(
            json.dumps(
                {
                    "schemaVersion": 1,
                    "workspaceRoot": str(self.workspace_root),
                    "execution": None,
                }
            ),
            encoding="utf-8",
        )
        with self.assertRaises(WorkspaceError):
            load_workspace_config(null_execution, verify_writable=False)

        self.assertEqual(ExecutionConcurrency(scene_render=5).for_stage("sceneRender"), 5)
        with self.assertRaises(WorkspaceError):
            ExecutionConcurrency(scene_render=17)
        with self.assertRaises(WorkspaceError):
            ExecutionAgentConcurrency(default=True)
        for invalid in (None, True, 1, "slow", "MEDIUM"):
            with self.subTest(direct_subtitle_preset=invalid), self.assertRaises(WorkspaceError):
                ExecutionVideoEncoding(subtitle_preset=invalid)  # type: ignore[arg-type]
        with self.assertRaisesRegex(WorkspaceError, "未知 worker stage"):
            ExecutionConcurrency().for_stage("missing")
        with self.assertRaisesRegex(WorkspaceError, "未知 agent role"):
            ExecutionAgentConcurrency().for_role("missing")

    def test_create_project_cli_help_documents_empty_scenes_plan_and_plan_input(self) -> None:
        help_text = create_project._parser().format_help()
        self.assertIn("scenes 为空的有效计划骨架", help_text)
        self.assertIn("--plan", help_text)
        self.assertIn("已确认配图策略", help_text)
        self.assertIn("--voiceover-mode", help_text)
        self.assertIn("activeProvider", help_text)
        self.assertIn("disabled", help_text)
        self.assertIn("--source-input", help_text)
        self.assertIn("--source-manifest", help_text)
        self.assertIn("--agent-approval", help_text)
        self.assertIn("--image-generation-mode", help_text)
        upgrade_help = upgrade_project_cli._parser().format_help()
        self.assertIn("--to-schema", upgrade_help)
        self.assertIn("--voiceover-mode", upgrade_help)

    def test_create_project_cli_uses_active_provider_when_voiceover_mode_is_omitted(self) -> None:
        with (
            mock.patch.object(create_project.ProjectWorkspace, "from_config", return_value=self.workspace()),
            mock.patch.object(create_project, "active_provider_id", return_value="minimax"),
            redirect_stdout(io.StringIO()),
            redirect_stderr(io.StringIO()),
        ):
            result = create_project.main([
                "--name", "配置默认旁白项目",
                "--srt", str(self.source_srt),
            ])
        self.assertEqual(result, 0)
        project = self.workspace().load_project(self.workspace_root / "projects" / "配置默认旁白项目")
        self.assertEqual(project.voiceover_mode, "minimax")
        self.assertFalse(project.background_music_enabled)
        self.assertFalse(project.agent_approval_enabled)
        self.assertIs(project.metadata["agentApprovalEnabled"], False)
        self.assertEqual(project.image_generation_mode, "provider")
        self.assertEqual(project.metadata["imageGenerationMode"], "provider")

    def test_create_project_cli_records_enabled_background_music_choice(self) -> None:
        with (
            mock.patch.object(create_project.ProjectWorkspace, "from_config", return_value=self.workspace()),
            mock.patch.object(create_project, "active_provider_id", return_value="edge-tts"),
            redirect_stdout(io.StringIO()),
            redirect_stderr(io.StringIO()),
        ):
            result = create_project.main([
                "--name", "启用背景音乐项目",
                "--srt", str(self.source_srt),
                "--background-music", "enabled",
            ])
        self.assertEqual(result, 0)
        project = self.workspace().load_project(self.workspace_root / "projects" / "启用背景音乐项目")
        self.assertTrue(project.background_music_enabled)
        metadata = json.loads(project.path("project.json").read_text(encoding="utf-8"))
        self.assertEqual(metadata["backgroundMusic"], {"enabled": True})

    def test_create_project_cli_records_enabled_agent_approval_choice(self) -> None:
        output = io.StringIO()
        with (
            mock.patch.object(create_project.ProjectWorkspace, "from_config", return_value=self.workspace()),
            mock.patch.object(create_project, "active_provider_id", return_value="edge-tts"),
            redirect_stdout(output),
            redirect_stderr(io.StringIO()),
        ):
            result = create_project.main([
                "--name", "启用代理批准项目",
                "--srt", str(self.source_srt),
                "--agent-approval", "enabled",
            ])
        self.assertEqual(result, 0)
        project = self.workspace().load_project(self.workspace_root / "projects" / "启用代理批准项目")
        self.assertTrue(project.agent_approval_enabled)
        self.assertIs(project.metadata["agentApprovalEnabled"], True)
        self.assertIn("AGENT_APPROVAL=enabled", output.getvalue())

    def test_create_project_cli_records_gpt_login_image_generation_mode(self) -> None:
        output = io.StringIO()
        with (
            mock.patch.object(create_project.ProjectWorkspace, "from_config", return_value=self.workspace()),
            mock.patch.object(create_project, "active_provider_id", return_value="edge-tts"),
            redirect_stdout(output),
            redirect_stderr(io.StringIO()),
        ):
            result = create_project.main([
                "--name", "GPT 登录态生图项目",
                "--srt", str(self.source_srt),
                "--image-generation-mode", "gpt-login",
            ])
        self.assertEqual(result, 0)
        project = self.workspace().load_project(self.workspace_root / "projects" / "GPT 登录态生图项目")
        self.assertEqual(project.image_generation_mode, "gpt-login")
        self.assertEqual(project.metadata["imageGenerationMode"], "gpt-login")
        self.assertIn("IMAGE_GENERATION_MODE=gpt-login", output.getvalue())

    def test_create_project_cli_resume_rejects_explicit_image_generation_mode(self) -> None:
        project = self.workspace().create_project(
            "续接冻结生图方式项目",
            self.source_srt,
            image_generation_mode="gpt-login",
        )
        metadata_path = project.path("project.json")
        before = metadata_path.read_bytes()

        for choice in ("provider", "gpt-login"):
            stderr = io.StringIO()
            with (
                self.subTest(choice=choice),
                mock.patch.object(
                    create_project.ProjectWorkspace,
                    "from_config",
                    return_value=self.workspace(),
                ),
                redirect_stdout(io.StringIO()),
                redirect_stderr(stderr),
            ):
                result = create_project.main([
                    "--resume", str(project.root),
                    "--srt", str(self.source_srt),
                    "--image-generation-mode", choice,
                ])
            self.assertEqual(result, 2)
            self.assertIn("--image-generation-mode 仅用于创建新项目", stderr.getvalue())
            self.assertEqual(metadata_path.read_bytes(), before)

        loaded = self.workspace().load_project(project.root)
        self.assertEqual(loaded.image_generation_mode, "gpt-login")

    def test_image_generation_mode_rejects_invalid_api_and_persisted_values(self) -> None:
        with self.assertRaisesRegex(ProjectValidationError, "imageGenerationMode"):
            self.workspace().create_project(
                "非法生图方式参数",
                self.source_srt,
                image_generation_mode="browser",  # type: ignore[arg-type]
            )

        project = self.new_project("非法生图方式元数据")
        metadata_path = project.path("project.json")
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        metadata["imageGenerationMode"] = "browser"
        metadata_path.write_text(json.dumps(metadata, ensure_ascii=False), encoding="utf-8")
        with self.assertRaisesRegex(ProjectValidationError, "imageGenerationMode"):
            self.workspace().load_project(project.root)

    def test_create_project_cli_resume_rejects_any_explicit_agent_approval_choice(self) -> None:
        project = self.workspace().create_project(
            "续接冻结代理批准项目",
            self.source_srt,
            agent_approval_enabled=True,
        )
        metadata_path = project.path("project.json")
        before = metadata_path.read_bytes()

        for choice in ("enabled", "disabled"):
            stderr = io.StringIO()
            with (
                self.subTest(choice=choice),
                mock.patch.object(
                    create_project.ProjectWorkspace,
                    "from_config",
                    return_value=self.workspace(),
                ),
                redirect_stdout(io.StringIO()),
                redirect_stderr(stderr),
            ):
                result = create_project.main([
                    "--resume", str(project.root),
                    "--srt", str(self.source_srt),
                    "--agent-approval", choice,
                ])
            self.assertEqual(result, 2)
            self.assertIn("--agent-approval 仅用于创建新项目", stderr.getvalue())
            self.assertEqual(metadata_path.read_bytes(), before)

        loaded = self.workspace().load_project(project.root)
        self.assertTrue(loaded.agent_approval_enabled)
        self.assertIs(loaded.metadata["agentApprovalEnabled"], True)

    def test_agent_approval_rejects_non_boolean_api_and_persisted_values(self) -> None:
        with self.assertRaisesRegex(ProjectValidationError, "agentApprovalEnabled"):
            self.workspace().create_project(
                "非法代理批准参数",
                self.source_srt,
                agent_approval_enabled=1,  # type: ignore[arg-type]
            )

        project = self.new_project("非法代理批准元数据")
        metadata_path = project.path("project.json")
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        metadata["agentApprovalEnabled"] = "enabled"
        metadata_path.write_text(json.dumps(metadata, ensure_ascii=False), encoding="utf-8")
        with self.assertRaisesRegex(ProjectValidationError, "agentApprovalEnabled"):
            self.workspace().load_project(project.root)

    def test_silent_project_agent_approval_derives_agent_first_and_rejects_user_first(self) -> None:
        manual = self.new_project("人工策略项目")
        self.assertEqual(workspace_module.resolve_project_review_policy(manual), "user_first")
        self.assertEqual(
            workspace_module.resolve_project_review_policy(manual, "agent_first"),
            "agent_first",
        )

        automatic = self.workspace().create_project(
            "代理策略项目",
            self.source_srt,
            agent_approval_enabled=True,
        )
        self.assertEqual(
            workspace_module.resolve_project_review_policy(automatic),
            "agent_first",
        )
        self.assertEqual(
            workspace_module.resolve_project_review_policy(automatic, "agent_first"),
            "agent_first",
        )
        with self.assertRaisesRegex(ProjectValidationError, "冲突"):
            workspace_module.resolve_project_review_policy(automatic, "user_first")

    def test_background_music_rejects_silent_project(self) -> None:
        with self.assertRaisesRegex(ProjectValidationError, "只允许用于旁白项目"):
            self.workspace().create_project(
                "静音背景音乐非法项目",
                self.source_srt,
                voiceover_mode="disabled",
                background_music_enabled=True,
            )

    def test_create_project_cli_can_explicitly_keep_silent_mode(self) -> None:
        with (
            mock.patch.object(create_project.ProjectWorkspace, "from_config", return_value=self.workspace()),
            mock.patch.object(create_project, "active_provider_id", side_effect=AssertionError("不应读取 activeProvider")),
            redirect_stdout(io.StringIO()),
            redirect_stderr(io.StringIO()),
        ):
            result = create_project.main([
                "--name", "显式静音项目",
                "--srt", str(self.source_srt),
                "--voiceover-mode", "disabled",
            ])
        self.assertEqual(result, 0)
        project = self.workspace().load_project(self.workspace_root / "projects" / "显式静音项目")
        self.assertEqual(project.voiceover_mode, "disabled")

    def test_missing_or_unwritable_config_fails_without_fallback(self) -> None:
        missing = self.case_root / "missing.local.json"
        with self.assertRaisesRegex(WorkspaceError, "缺少工作区配置"):
            load_workspace_config(missing)
        self.assertFalse(missing.exists())

        with mock.patch.object(
            workspace_module,
            "_verify_writable_directory",
            side_effect=WorkspaceError("工作区目录不可写"),
        ):
            with self.assertRaisesRegex(WorkspaceError, "不可写"):
                load_workspace_config(self.config_path)

    def test_workspace_access_probe_reports_stable_stage_and_error_code(self) -> None:
        success = workspace_module.probe_workspace_access(self.workspace_root)
        self.assertTrue(success.ok)
        self.assertEqual(success.code, "workspace_access_ok")
        self.assertEqual(success.stage, "complete")
        self.assertFalse(list(self.workspace_root.glob(".workspace-write-test-*")))

        with mock.patch.object(
            workspace_module.Path,
            "open",
            side_effect=PermissionError(13, "access denied"),
        ):
            denied = workspace_module.probe_workspace_access(self.workspace_root)
        self.assertFalse(denied.ok)
        self.assertEqual(denied.code, "workspace_write_denied")
        self.assertEqual(denied.stage, "write_and_read")
        self.assertIn("新回合", denied.message)

    def test_prepare_env_workspace_only_probe_does_not_prepare_venv(self) -> None:
        output = io.StringIO()
        access = workspace_module.WorkspaceAccessProbe(
            root=self.workspace_root,
            ok=True,
            code="workspace_access_ok",
            stage="complete",
            message="ok",
        )
        with mock.patch.object(
            prepare_env,
            "load_workspace_config",
            return_value=self.workspace().config,
        ), mock.patch.object(
            prepare_env, "probe_workspace_access", return_value=access
        ), mock.patch.object(
            prepare_env, "ensure_venv"
        ) as ensure, redirect_stdout(output):
            result = prepare_env.main(["--check-workspace-access"])
        self.assertEqual(result, 0)
        ensure.assert_not_called()
        payload = json.loads(output.getvalue().split("=", 1)[1])
        self.assertEqual(payload["code"], "workspace_access_ok")

    def test_project_name_cleanup_preserves_chinese_and_rejects_empty(self) -> None:
        self.assertEqual(sanitize_project_name('中文:项目?*.  '), "中文-项目--")
        self.assertEqual(sanitize_project_name("清晰中文"), "清晰中文")
        with self.assertRaisesRegex(ProjectValidationError, "清理后为空"):
            sanitize_project_name("...   ")

    def test_create_project_writes_all_paths_srt_hash_and_empty_valid_plan(self) -> None:
        project = self.workspace().create_project("示例:项目. ", self.source_srt)
        self.assertEqual(project.root.parent, self.workspace_root.resolve() / "projects")
        self.assertEqual(project.metadata["projectName"], "示例-项目")
        self.assertEqual(project.metadata["source"]["sha256"], sha256_file(self.source_srt))
        self.assertEqual(project.metadata["schemaVersion"], 2)
        self.assertIs(project.metadata["agentApprovalEnabled"], False)
        self.assertFalse(project.agent_approval_enabled)
        self.assertEqual(project.metadata["imageGenerationMode"], "provider")
        self.assertEqual(project.image_generation_mode, "provider")
        self.assertEqual(project.voiceover_mode, "disabled")
        self.assertEqual(project.render_profile, FIXED_RENDER_PROFILE)
        self.assertTrue(project.timing_plan_persisted)
        self.assertEqual(project.timing_plan["sourceSrtSha256"], sha256_file(self.source_srt))
        self.assertEqual(project.timing_plan["renderProfileSha256"], sha256_json(FIXED_RENDER_PROFILE))
        self.assertEqual(project.plan["projectId"], project.project_id)
        self.assertEqual(project.plan["outputCanvas"], FIXED_CANVAS)
        self.assertEqual(project.plan["scenes"], [])
        self.assertTrue(project.plan["constraints"]["forbidText"])
        for relative in [
            "source/source.srt",
            "planning/generation-plan.json",
            "planning/timing-plan.json",
            "scenes",
            "manifests",
            "previews",
            "output",
            "audio",
            "subtitles",
            ".work",
        ]:
            self.assertTrue(project.path(relative).exists())
        self.assertEqual(project.metadata["paths"], PROJECT_PATHS_V2)

    def test_create_with_confirmed_plan_injects_project_id_and_validates(self) -> None:
        plan = create_generation_plan(str(uuid.uuid4()))
        plan.pop("projectId")
        plan["globalPrompt"] = "统一手绘线条、暖米黄背景、画面无文字"
        plan["scenes"] = [
            {
                "sceneId": "scene-01",
                "name": "核心概念",
                "subtitleRange": {"startMs": 0, "endMs": 30000},
                "sceneDurationMs": 30000,
                "prompt": "一个概念由三个简单图形共同说明",
                "outputFile": "scene-01-核心概念.png",
            }
        ]
        project = self.workspace().create_project("完整计划", self.source_srt, confirmed_plan=plan)
        self.assertEqual(project.plan["projectId"], project.project_id)
        self.assertEqual(project.plan["scenes"][0]["outputFile"], "scene-01-核心概念.png")

    def test_new_edge_project_keeps_source_timing_provisional_until_audio_approval(self) -> None:
        plan = create_generation_plan(str(uuid.uuid4()))
        plan.pop("projectId")
        plan["scenes"] = [
            {
                "sceneId": "scene-01",
                "cueRange": [1, 1],
                "sceneDurationMs": 3000,
                "prompt": "一个安全场景",
                "outputFile": "scene-01.png",
            }
        ]
        project = self.workspace().create_project(
            "Edge 项目",
            self.source_srt,
            confirmed_plan=plan,
            voiceover_mode="edge-tts",
        )
        self.assertEqual(project.voiceover_mode, "edge-tts")
        self.assertEqual(project.timing_plan["voiceoverMode"], "edge-tts")
        self.assertEqual(project.timing_plan["activeTimeline"]["kind"], "source-srt")
        self.assertEqual(project.timing_plan["scenes"][0]["startFrame"], 0)
        self.assertEqual(project.timing_plan["scenes"][0]["endFrameExclusive"], 180)

    def test_content_source_project_copies_evidence_and_binds_current_plan_and_srt(self) -> None:
        project = self.new_content_project("主题入口项目")
        self.assertEqual(project.voiceover_mode, "edge-tts")
        self.assertEqual(project.metadata["contentSource"]["inputFile"], "source/input.json")
        self.assertEqual(
            project.metadata["contentSource"]["manifestFile"], "source/source-manifest.json"
        )
        self.assertTrue(project.path("source/input.json").is_file())
        self.assertTrue(project.path("source/source-manifest.json").is_file())
        self.assertEqual(project.plan["projectId"], project.project_id)
        self.assertEqual(project.timing_plan["activeTimeline"]["kind"], "source-srt")
        self.assertEqual(project.timing_plan["scenes"][-1]["endMs"], 60000)
        loaded = self.workspace().load_project(project.root)
        self.assertEqual(loaded.metadata["contentSource"], project.metadata["contentSource"])

    def test_create_project_cli_accepts_paired_content_source_only_for_new_project(self) -> None:
        package = self.content_package()
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            mock.patch.object(create_project.ProjectWorkspace, "from_config", return_value=self.workspace()),
            mock.patch.object(create_project, "active_provider_id", return_value="edge-tts"),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            result = create_project.main(
                [
                    "--name", "CLI 内容项目",
                    "--srt", str(package.directory / "source.srt"),
                    "--plan", str(package.directory / "generation-plan.json"),
                    "--source-input", str(package.directory / "input.json"),
                    "--source-manifest", str(package.directory / "manifest.json"),
                ]
            )
        self.assertEqual(result, 0, stderr.getvalue())
        self.assertIn("VOICEOVER_MODE=edge-tts", stdout.getvalue())
        project = self.workspace().load_project(self.workspace_root / "projects" / "CLI 内容项目")
        self.assertIn("contentSource", project.metadata)

        with (
            mock.patch.object(create_project.ProjectWorkspace, "from_config", return_value=self.workspace()),
            redirect_stdout(io.StringIO()),
            redirect_stderr(io.StringIO()),
        ):
            resume_result = create_project.main(
                [
                    "--resume", str(project.root),
                    "--srt", str(package.directory / "source.srt"),
                    "--source-input", str(package.directory / "input.json"),
                    "--source-manifest", str(package.directory / "manifest.json"),
                ]
            )
        self.assertEqual(resume_result, 2)

    def test_content_source_requires_complete_pair_plan_and_edge_mode(self) -> None:
        package = self.content_package()
        common = {
            "confirmed_plan": package.generation_plan,
            "source_input": package.directory / "input.json",
            "source_manifest": package.directory / "manifest.json",
            "source_plan": package.directory / "generation-plan.json",
        }
        missing_manifest = dict(common)
        missing_manifest["source_manifest"] = None
        with self.assertRaisesRegex(ProjectValidationError, "同时提供"):
            self.workspace().create_project(
                "缺 manifest",
                package.directory / "source.srt",
                voiceover_mode="edge-tts",
                **missing_manifest,
            )
        with self.assertRaisesRegex(ProjectValidationError, "topic/text content source"):
            self.workspace().create_project(
                "错误 Disabled",
                package.directory / "source.srt",
                voiceover_mode="disabled",
                **common,
            )

    def test_content_source_tampering_any_project_evidence_or_plan_is_rejected(self) -> None:
        targets = (
            "source/input.json",
            "source/source-manifest.json",
            "source/source.srt",
            "planning/generation-plan.json",
        )
        for index, relative in enumerate(targets):
            project = self.new_content_project(
                f"篡改证据-{index}", package_label=f"content-package-{index}"
            )
            project.path(relative).write_bytes(project.path(relative).read_bytes() + b" ")
            with self.subTest(relative=relative), self.assertRaises(ProjectValidationError):
                self.workspace().load_project(project.root)

    def test_changed_source_input_cannot_be_combined_with_old_manifest(self) -> None:
        package = self.content_package()
        changed = json.loads((package.directory / "input.json").read_text(encoding="utf-8"))
        changed["topic"] = "已经改变的主题"
        (package.directory / "input.json").write_text(
            json.dumps(changed, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        with self.assertRaises(ProjectValidationError):
            self.workspace().create_project(
                "失配来源",
                package.directory / "source.srt",
                confirmed_plan=package.generation_plan,
                voiceover_mode="edge-tts",
                source_input=package.directory / "input.json",
                source_manifest=package.directory / "manifest.json",
                source_plan=package.directory / "generation-plan.json",
            )

    def test_legacy_v2_without_content_source_is_not_rewritten_on_load(self) -> None:
        project = self.new_project("传统 v2 不改写")
        metadata_path = project.path("project.json")
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        metadata.pop("agentApprovalEnabled")
        metadata_path.write_text(json.dumps(metadata, ensure_ascii=False), encoding="utf-8")
        before = metadata_path.read_bytes()
        loaded = self.workspace().load_project(project.root)
        self.assertNotIn("contentSource", loaded.metadata)
        self.assertNotIn("agentApprovalEnabled", loaded.metadata)
        self.assertFalse(loaded.agent_approval_enabled)
        self.assertEqual(metadata_path.read_bytes(), before)

    def test_legacy_v2_without_image_generation_mode_defaults_without_rewrite(self) -> None:
        project = self.new_project("传统 v2 生图方式兼容")
        metadata_path = project.path("project.json")
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        metadata.pop("imageGenerationMode")
        metadata_path.write_text(json.dumps(metadata, ensure_ascii=False), encoding="utf-8")
        before = metadata_path.read_bytes()

        loaded = self.workspace().load_project(project.root)

        self.assertNotIn("imageGenerationMode", loaded.metadata)
        self.assertEqual(loaded.image_generation_mode, "provider")
        self.assertEqual(metadata_path.read_bytes(), before)

    def test_v1_loader_exposes_disabled_compatibility_view_without_rewrite(self) -> None:
        project = self.new_project("v1 兼容")
        metadata_path = self.downgrade_to_v1(project)
        before = metadata_path.read_bytes()
        timing_path = project.path("planning/timing-plan.json")

        loaded = self.workspace().load_project(project.root)

        self.assertEqual(loaded.schema_version, 1)
        self.assertEqual(loaded.voiceover_mode, "disabled")
        self.assertFalse(loaded.agent_approval_enabled)
        self.assertEqual(loaded.image_generation_mode, "provider")
        self.assertEqual(loaded.render_profile, FIXED_RENDER_PROFILE)
        self.assertFalse(loaded.timing_plan_persisted)
        self.assertEqual(loaded.timing_plan["voiceoverMode"], "disabled")
        self.assertEqual(metadata_path.read_bytes(), before)
        self.assertFalse(timing_path.exists())

    def test_upgrade_v1_publishes_timing_before_project_commit_and_preserves_generation_plan(self) -> None:
        project = self.new_project("原子升级")
        self.downgrade_to_v1(project)
        plan_hash = sha256_file(project.plan_path)
        real_replace = os.replace
        published: list[str] = []

        def tracked_replace(source, target) -> None:
            published.append(Path(target).name)
            real_replace(source, target)

        with mock.patch.object(workspace_module.os, "replace", side_effect=tracked_replace):
            upgraded = self.workspace().upgrade_project(
                project.root,
                to_schema=2,
                voiceover_mode="edge-tts",
            )

        self.assertEqual(published[-2:], ["timing-plan.json", "project.json"])
        self.assertEqual(upgraded.schema_version, 2)
        self.assertEqual(upgraded.voiceover_mode, "edge-tts")
        self.assertEqual(upgraded.metadata["paths"], PROJECT_PATHS_V2)
        self.assertTrue(upgraded.path("audio").is_dir())
        self.assertTrue(upgraded.path("subtitles").is_dir())
        self.assertEqual(sha256_file(upgraded.plan_path), plan_hash)

    def test_failed_upgrade_project_commit_remains_readable_v1_and_retry_recovers(self) -> None:
        project = self.new_project("升级恢复")
        metadata_path = self.downgrade_to_v1(project)
        original_metadata = metadata_path.read_bytes()
        real_replace = os.replace

        def fail_commit(source, target) -> None:
            if Path(target).name == "project.json":
                raise OSError("模拟提交点失败")
            real_replace(source, target)

        with mock.patch.object(workspace_module.os, "replace", side_effect=fail_commit):
            with self.assertRaisesRegex(OSError, "提交点失败"):
                self.workspace().upgrade_project(
                    project.root,
                    to_schema=2,
                    voiceover_mode="edge-tts",
                )

        self.assertEqual(metadata_path.read_bytes(), original_metadata)
        orphan_timing = json.loads(
            project.path("planning/timing-plan.json").read_text(encoding="utf-8")
        )
        self.assertEqual(orphan_timing["voiceoverMode"], "edge-tts")
        compatible = self.workspace().load_project(project.root)
        self.assertEqual(compatible.schema_version, 1)
        self.assertEqual(compatible.voiceover_mode, "disabled")
        self.assertEqual(compatible.timing_plan["voiceoverMode"], "disabled")

        recovered = self.workspace().upgrade_project(
            project.root,
            to_schema=2,
            voiceover_mode="edge-tts",
        )
        self.assertEqual(recovered.schema_version, 2)
        self.assertEqual(recovered.voiceover_mode, "edge-tts")

    def test_conflict_rejected_and_resume_requires_matching_srt_hash(self) -> None:
        project = self.workspace().create_project("冲突项目", self.source_srt)
        with self.assertRaisesRegex(ProjectValidationError, "项目已存在"):
            self.workspace().create_project("冲突项目", self.source_srt)
        resumed = self.workspace().resume_project(project.root, self.source_srt)
        self.assertEqual(resumed.project_id, project.project_id)

        changed_srt = self.case_root / "changed.srt"
        changed_srt.write_text("不同字幕", encoding="utf-8")
        with self.assertRaisesRegex(ProjectValidationError, "SHA-256"):
            self.workspace().resume_project(project.root, changed_srt)

    def test_resume_rejects_invalid_project_id_and_plan_project_id(self) -> None:
        first = self.new_project("损坏项目ID")
        metadata_path = first.path("project.json")
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        metadata["projectId"] = "not-a-uuid"
        metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
        with self.assertRaisesRegex(ProjectValidationError, "UUID"):
            self.workspace().resume_project(first.root, self.source_srt)

        second = self.new_project("计划ID不匹配")
        plan_path = second.plan_path
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        plan["projectId"] = str(uuid.uuid4())
        plan_path.write_text(json.dumps(plan), encoding="utf-8")
        with self.assertRaisesRegex(ProjectValidationError, "projectId"):
            self.workspace().resume_project(second.root, self.source_srt)

    def test_project_paths_and_run_directory_block_traversal(self) -> None:
        project = self.new_project()
        for unsafe in ["../outside", r"..\outside", r"D:\outside", str(project.root)]:
            with self.subTest(unsafe=unsafe):
                with self.assertRaises(ProjectValidationError):
                    safe_project_path(project.root, unsafe)
        run_dir = project.create_run_dir("run-001")
        self.assertEqual(run_dir.parent, project.path(".work"))
        with self.assertRaises(ProjectValidationError):
            project.create_run_dir("../run-002")

    def test_resume_rejects_project_outside_workspace_or_nested_project(self) -> None:
        project = self.new_project()
        with self.assertRaisesRegex(ProjectValidationError, "projects 内"):
            self.workspace().resume_project(self.case_root, self.source_srt)
        with self.assertRaisesRegex(ProjectValidationError, "直接子目录"):
            self.workspace().resume_project(project.root / "nested", self.source_srt)

    def test_generation_plan_rejects_frozen_contract_violations(self) -> None:
        project_id = str(uuid.uuid4())
        base = create_generation_plan(project_id)
        scene = {
            "sceneId": "scene-01",
            "name": "第一幕",
            "subtitleRange": {"startMs": 0, "endMs": 1000},
            "sceneDurationMs": 1000,
            "prompt": "一个安全场景",
            "outputFile": "scene-01.png",
        }
        base["scenes"] = [scene]
        validate_generation_plan_data(base, project_id=project_id)

        mutations = [
            ("空提示词", lambda p: p.__setitem__("globalPrompt", "  ")),
            ("单幕缺提示词", lambda p: p["scenes"][0].pop("prompt")),
            ("单幕空提示词", lambda p: p["scenes"][0].__setitem__("prompt", " \t ")),
            ("画布变化", lambda p: p["outputCanvas"].__setitem__("width", 1280)),
            ("禁字非真", lambda p: p["constraints"].__setitem__("forbidText", 1)),
            ("非正时长", lambda p: p["scenes"][0].__setitem__("sceneDurationMs", 0)),
            ("目录穿越", lambda p: p["scenes"][0].__setitem__("outputFile", "../bad.png")),
            ("非PNG", lambda p: p["scenes"][0].__setitem__("outputFile", "bad.jpg")),
            ("含凭据", lambda p: p.__setitem__("apiKey", "secret-value")),
        ]
        for label, mutate in mutations:
            with self.subTest(label=label):
                candidate = json.loads(json.dumps(base))
                mutate(candidate)
                with self.assertRaises(ProjectValidationError):
                    validate_generation_plan_data(candidate, project_id=project_id)

        duplicate = json.loads(json.dumps(base))
        duplicate["scenes"].append({**duplicate["scenes"][0]})
        with self.assertRaisesRegex(ProjectValidationError, "sceneId 重复"):
            validate_generation_plan_data(duplicate, project_id=project_id)
        duplicate["scenes"][1]["sceneId"] = "scene-02"
        with self.assertRaisesRegex(ProjectValidationError, "outputFile 重复"):
            validate_generation_plan_data(duplicate, project_id=project_id)

    def test_pre_project_storyboard_candidate_is_read_only_and_reuses_timing_validation(self) -> None:
        source = self.case_root / "traditional.srt"
        source.write_text(
            "1\n00:00:00,000 --> 00:00:01,000\n第一条\n\n"
            "2\n00:00:01,000 --> 00:00:02,000\n第二条\n",
            encoding="utf-8",
        )
        candidate = create_generation_plan(str(uuid.uuid4()))
        candidate.pop("projectId")
        candidate["scenes"] = [
            {
                "sceneId": "scene-01",
                "name": "第一幕",
                "coreIdea": "先介绍第一条信息",
                "visualSubject": "一个圆形主体",
                "cueRange": [1, 1],
                "sceneDurationMs": 1000,
                "prompt": "暖米黄纸上的圆形主体",
                "outputFile": "scene-01.png",
            },
            {
                "sceneId": "scene-02",
                "name": "第二幕",
                "coreIdea": "再介绍第二条信息",
                "visualSubject": "两个相连主体",
                "cueRange": [2, 2],
                "sceneDurationMs": 1000,
                "prompt": "暖米黄纸上的两个相连主体",
                "outputFile": "scene-02.png",
            },
        ]
        before = {path.relative_to(self.case_root).as_posix(): path.read_bytes() for path in self.case_root.rglob("*") if path.is_file()}
        validated = validate_pre_project_generation_plan_data(
            candidate,
            source_srt_path=source,
            voiceover_mode="disabled",
        )
        after = {path.relative_to(self.case_root).as_posix(): path.read_bytes() for path in self.case_root.rglob("*") if path.is_file()}
        self.assertEqual(validated, candidate)
        self.assertNotIn("projectId", validated)
        self.assertEqual(after, before)
        self.assertFalse((self.workspace_root / "projects").exists())

        mutations = [
            lambda p: p["scenes"][0].pop("prompt"),
            lambda p: p["scenes"][0].__setitem__("imagePrompt", p["scenes"][0].pop("prompt")),
            lambda p: p["scenes"][0].__setitem__("sourceCueRange", [1, 1]),
            lambda p: p["scenes"][0].__setitem__("cueRange", [1, 2]),
            lambda p: p["scenes"][1].__setitem__("cueRange", [2, 1]),
            lambda p: p["scenes"].pop(),
            lambda p: p["scenes"][0].__setitem__("sceneDurationMs", 999),
        ]
        for index, mutate in enumerate(mutations):
            invalid = json.loads(json.dumps(candidate))
            mutate(invalid)
            with self.subTest(index=index), self.assertRaises(ProjectValidationError):
                validate_pre_project_generation_plan_data(invalid, source_srt_path=source)
        self.assertFalse((self.workspace_root / "projects").exists())

    def test_create_project_cli_consumes_confirmed_candidate_and_injects_real_uuid(self) -> None:
        plan = create_generation_plan(str(uuid.uuid4()))
        plan.pop("projectId")
        plan["scenes"] = [
            {
                "sceneId": "scene-01",
                "coreIdea": "说明测试字幕",
                "visualSubject": "单一测试主体",
                "cueRange": [1, 1],
                "sceneDurationMs": 3000,
                "prompt": "暖米黄背景上的单一测试主体",
                "outputFile": "scene-01.png",
            }
        ]
        confirmed = self.case_root / "confirmed-generation-plan.json"
        confirmed.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
        with (
            mock.patch.object(create_project.ProjectWorkspace, "from_config", return_value=self.workspace()),
            redirect_stdout(io.StringIO()),
            redirect_stderr(io.StringIO()),
        ):
            result = create_project.main(
                [
                    "--name", "确认候选项目",
                    "--srt", str(self.source_srt),
                    "--plan", str(confirmed),
                    "--voiceover-mode", "disabled",
                ]
            )
        self.assertEqual(result, 0)
        project = self.workspace().load_project(self.workspace_root / "projects" / "确认候选项目")
        self.assertEqual(uuid.UUID(project.project_id).version, 4)
        self.assertEqual(project.plan["projectId"], project.project_id)
        self.assertNotEqual(project.project_id, plan.get("projectId"))
        self.assertEqual(json.loads(confirmed.read_text(encoding="utf-8")), plan)

    def test_prepare_env_paths_and_subprocess_environment_stay_in_runtime(self) -> None:
        venv_root, pip_cache, runtime_tmp = prepare_env.runtime_paths(self.config_path)
        runtime = self.workspace_root.resolve() / "runtime"
        self.assertEqual(venv_root, runtime / ".venv")
        self.assertEqual(pip_cache, runtime / "cache" / "pip")
        self.assertEqual(runtime_tmp, runtime / "tmp")
        env = prepare_env.subprocess_environment(pip_cache, runtime_tmp)
        self.assertEqual(env["PIP_CACHE_DIR"], str(pip_cache))
        self.assertEqual(env["TEMP"], str(runtime_tmp))
        self.assertEqual(env["TMP"], str(runtime_tmp))

    def test_prepare_env_venv_creation_inherits_runtime_temp(self) -> None:
        venv_root, _, runtime_tmp = prepare_env.runtime_paths(self.config_path)

        def fake_create(target: str, *, with_pip: bool) -> None:
            self.assertTrue(with_pip)
            self.assertEqual(Path(target), venv_root)
            self.assertEqual(os.environ.get("TEMP"), str(runtime_tmp))
            self.assertEqual(os.environ.get("TMP"), str(runtime_tmp))
            py = prepare_env.interpreter_path(venv_root)
            py.parent.mkdir(parents=True, exist_ok=True)
            py.write_bytes(b"")

        with mock.patch.object(prepare_env.venv, "create", side_effect=fake_create):
            py, pip_cache, actual_tmp = prepare_env.ensure_venv(False, self.config_path)
        self.assertEqual(py, prepare_env.interpreter_path(venv_root))
        self.assertTrue(pip_cache.is_dir())
        self.assertEqual(actual_tmp, runtime_tmp)
        self.assertTrue(runtime_tmp.is_dir())

    def test_prepare_env_edge_feature_is_explicit_and_version_pinned(self) -> None:
        py = self.case_root / "python.exe"
        pip_cache = self.case_root / "pip-cache"
        runtime_tmp = self.case_root / "runtime-tmp"
        checked: list[dict[str, str]] = []

        def available(_py, dependencies, _env) -> dict[str, bool]:
            checked.append(dict(dependencies))
            return {name: True for name in dependencies}

        with (
            mock.patch.object(prepare_env, "ensure_venv", return_value=(py, pip_cache, runtime_tmp)),
            mock.patch.object(prepare_env, "probe_dependencies", side_effect=available),
        ):
            self.assertEqual(prepare_env.main(["--check"]), 0)
        self.assertEqual(checked, [prepare_env.BASE_DEPS])
        self.assertNotIn("edge_tts", checked[0])

        checked.clear()
        with (
            mock.patch.object(prepare_env, "ensure_venv", return_value=(py, pip_cache, runtime_tmp)),
            mock.patch.object(prepare_env, "probe_dependencies", side_effect=available),
        ):
            self.assertEqual(prepare_env.main(["--check", "--feature", "edge-tts"]), 0)
        self.assertEqual(len(checked), 1)
        self.assertEqual(
            checked[0]["edge_tts"],
            f"edge-tts=={prepare_env.EDGE_TTS_VERSION}",
        )

    def test_prepare_env_batches_base_and_edge_in_one_interpreter_probe(self) -> None:
        py = self.case_root / "python.exe"
        dependencies = {
            **prepare_env.BASE_DEPS,
            **prepare_env.FEATURE_DEPS["edge-tts"],
        }
        completed = mock.Mock(
            returncode=0,
            stdout=(
                prepare_env._PROBE_RESULT_PREFIX
                + json.dumps(
                    [
                        {"importName": import_name, "available": True}
                        for import_name in dependencies
                    ]
                )
                + "\n"
            ),
        )
        with mock.patch.object(prepare_env.subprocess, "run", return_value=completed) as run:
            result = prepare_env.probe_dependencies(py, dependencies, {"TEMP": "C:\\temp"})

        self.assertEqual(result, {name: True for name in dependencies})
        self.assertEqual(run.call_count, 1)
        command = run.call_args.args[0]
        self.assertEqual(command[:2], [str(py), "-c"])
        specifications = json.loads(command[3])
        self.assertEqual(
            [item["importName"] for item in specifications],
            list(dependencies),
        )
        edge_specification = next(
            item for item in specifications if item["importName"] == "edge_tts"
        )
        self.assertEqual(edge_specification["distribution"], "edge-tts")
        self.assertEqual(edge_specification["expectedVersion"], prepare_env.EDGE_TTS_VERSION)

    def test_prepare_env_probe_results_are_independent_and_report_missing_items(self) -> None:
        py = self.case_root / "python.exe"
        dependencies = {
            **prepare_env.BASE_DEPS,
            **prepare_env.FEATURE_DEPS["edge-tts"],
        }
        availability = {
            "cv2": True,
            "numpy": False,
            "av": True,
            "PIL": False,
            "edge_tts": True,
        }
        independent_dependencies = {
            "json": "json",
            "module_that_must_not_exist_phase0b": "missing-distribution",
            "pathlib": "pathlib",
        }
        independent_result = prepare_env.probe_dependencies(
            Path(sys.executable),
            independent_dependencies,
            os.environ.copy(),
        )
        self.assertEqual(
            independent_result,
            {
                "json": True,
                "module_that_must_not_exist_phase0b": False,
                "pathlib": True,
            },
        )

        pip_cache = self.case_root / "pip-cache"
        runtime_tmp = self.case_root / "runtime-tmp"
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            mock.patch.object(prepare_env, "ensure_venv", return_value=(py, pip_cache, runtime_tmp)),
            mock.patch.object(prepare_env, "probe_dependencies", return_value=availability),
            mock.patch.object(prepare_env, "install") as install,
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            result = prepare_env.main(["--check", "--feature", "edge-tts"])

        self.assertEqual(result, 1)
        install.assert_not_called()
        self.assertIn("[ok] opencv-python", stdout.getvalue())
        self.assertIn("[miss] numpy", stdout.getvalue())
        self.assertIn("[miss] Pillow", stdout.getvalue())
        self.assertIn("缺少 2 个依赖: numpy, Pillow", stderr.getvalue())

    def test_prepare_env_non_check_installs_missing_requirements_once_in_order(self) -> None:
        py = self.case_root / "python.exe"
        pip_cache = self.case_root / "pip-cache"
        runtime_tmp = self.case_root / "runtime-tmp"
        availability = {
            "cv2": False,
            "numpy": True,
            "av": False,
            "PIL": True,
            "edge_tts": False,
        }
        with (
            mock.patch.object(prepare_env, "ensure_venv", return_value=(py, pip_cache, runtime_tmp)),
            mock.patch.object(prepare_env, "probe_dependencies", return_value=availability),
            mock.patch.object(prepare_env, "install", return_value=True) as install,
            redirect_stdout(io.StringIO()),
            redirect_stderr(io.StringIO()),
        ):
            result = prepare_env.main(["--feature", "edge-tts"])

        self.assertEqual(result, 0)
        install.assert_called_once()
        self.assertEqual(
            install.call_args.args,
            (
                py,
                ["opencv-python", "av", f"edge-tts=={prepare_env.EDGE_TTS_VERSION}"],
                mock.ANY,
            ),
        )

    def test_v2_loader_rejects_mode_render_profile_and_timing_identity_drift(self) -> None:
        project = self.new_project("v2 严格校验")
        metadata_path = project.path("project.json")
        timing_path = project.timing_plan_path

        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        metadata["voiceoverMode"] = "auto"
        metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
        with self.assertRaisesRegex(ProjectValidationError, "voiceoverMode"):
            self.workspace().load_project(project.root)

        metadata["voiceoverMode"] = "disabled"
        metadata["renderProfile"]["fps"] = 30
        metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
        with self.assertRaisesRegex(ProjectValidationError, "renderProfile"):
            self.workspace().load_project(project.root)

        metadata["renderProfile"] = dict(FIXED_RENDER_PROFILE)
        metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
        timing = json.loads(timing_path.read_text(encoding="utf-8"))
        timing["renderProfileSha256"] = "0" * 64
        timing_path.write_text(json.dumps(timing), encoding="utf-8")
        with self.assertRaisesRegex(ProjectValidationError, "renderProfileSha256"):
            self.workspace().load_project(project.root)


if __name__ == "__main__":
    unittest.main()
