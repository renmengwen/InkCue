from __future__ import annotations

import os
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock


SKILL_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = SKILL_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import approve_initial_project as approval_module  # noqa: E402
from approve_initial_project import InitialApprovalError, approve_initial_project  # noqa: E402
from initial_approval_options import (  # noqa: E402
    build_initial_approval_options,
    parse_initial_approval_response,
)
from project_workspace import (  # noqa: E402
    ProjectWorkspace,
    WorkspaceConfig,
    load_project,
)


class InitialApprovalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix=".test-initial-approval-",
            dir=str(SKILL_ROOT),
        )
        self.case_root = Path(self.temporary.name).resolve()
        self.workspace_root = self.case_root / "workspace"
        self.workspace = ProjectWorkspace(
            WorkspaceConfig(
                root=self.workspace_root,
                config_path=self.case_root / "workspace.local.json",
            )
        )
        self.source = self.case_root / "source.srt"
        self.source.write_text(
            "1\n00:00:00,000 --> 00:00:03,000\n测试字幕\n",
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def pending_project(self, *, voiceover_mode: str):
        return self.workspace.create_project(
            f"预项目-{uuid.uuid4().hex}",
            self.source,
            voiceover_mode=voiceover_mode,
            pending_initial_approval=True,
        )

    @staticmethod
    def selection(
        project,
        *,
        background_music_enabled: bool | None = None,
        agent_approval_enabled: bool = True,
        image_generation_mode: str = "provider",
        gpt_login_available: bool = False,
        provider_available: bool = True,
        fixed_image_generation_mode: str | None = None,
    ):
        if background_music_enabled is None:
            background_music_enabled = project.voiceover_mode != "disabled"
        options = build_initial_approval_options(
            voiceover_mode=project.voiceover_mode,
            gpt_login_image_generation_available=gpt_login_available,
            configured_image_provider_available=provider_available,
            fixed_image_generation_mode=fixed_image_generation_mode,
        )
        option = next(
            item
            for item in options
            if item["action"] == "approve"
            and item["backgroundMusicEnabled"] is background_music_enabled
            and item["agentApprovalEnabled"] is agent_approval_enabled
            and item["imageGenerationMode"] == image_generation_mode
        )
        return parse_initial_approval_response(
            str(option["number"]),
            options=options,
            content_identity_sha256=project.current_content_identity_sha256,
        )

    def test_silent_joint_approval_promotes_and_freezes_once(self) -> None:
        project = self.pending_project(voiceover_mode="disabled")
        committed = approve_initial_project(
            project.root,
            self.selection(project),
            configured_image_provider_available=True,
        )

        self.assertTrue(committed.initial_approval_completed)
        self.assertFalse(committed.pending_initial_approval)
        self.assertTrue(committed.agent_approval_enabled)
        self.assertFalse(committed.background_music_enabled)
        self.assertEqual(committed.image_generation_mode, "provider")
        approval = committed.metadata["initialApproval"]
        self.assertEqual(approval["approvalBasis"], "user_joint_content_and_plan")
        self.assertEqual(
            set(approval),
            {"status", "contentIdentitySha256", "approvalBasis", "approvedAt"},
        )
        self.assertEqual(
            approval["contentIdentitySha256"],
            committed.current_content_identity_sha256,
        )
        with self.assertRaisesRegex(InitialApprovalError, "不能重复"):
            approve_initial_project(
                committed.root,
                self.selection(committed),
                configured_image_provider_available=True,
            )

    def test_voiced_joint_approval_binds_current_content_and_plan(self) -> None:
        project = self.pending_project(voiceover_mode="edge-tts")
        committed = approve_initial_project(
            project.root,
            self.selection(project),
            configured_image_provider_available=True,
        )

        approval = committed.metadata["initialApproval"]
        self.assertEqual(approval["approvalBasis"], "user_joint_content_and_plan")
        self.assertEqual(
            approval["contentIdentitySha256"],
            committed.current_content_identity_sha256,
        )
        self.assertFalse(project.path("planning/voice-plan.json").exists())
        self.assertFalse(project.path("manifests/voice-manifest.json").exists())
        self.assertTrue(load_project(project.root).initial_approval_completed)

    def test_stale_content_identity_leaves_pending_bytes_unchanged(self) -> None:
        project = self.pending_project(voiceover_mode="edge-tts")
        project_path = project.path("project.json")
        before = project_path.read_bytes()
        selection = self.selection(project)
        selection["contentIdentitySha256"] = "c" * 64
        with self.assertRaisesRegex(InitialApprovalError, "stale"):
            approve_initial_project(
                project.root,
                selection,
                configured_image_provider_available=True,
            )
        self.assertEqual(project_path.read_bytes(), before)

    def test_gpt_login_requires_current_host_capability(self) -> None:
        project = self.pending_project(voiceover_mode="disabled")
        selection = self.selection(
            project,
            image_generation_mode="gpt-login",
            gpt_login_available=True,
            provider_available=True,
        )
        before = project.path("project.json").read_bytes()
        with self.assertRaisesRegex(InitialApprovalError, "合法通过选项"):
            approve_initial_project(
                project.root,
                selection,
                configured_image_provider_available=True,
            )
        self.assertEqual(project.path("project.json").read_bytes(), before)

        committed = approve_initial_project(
            project.root,
            selection,
            gpt_login_image_generation_available=True,
            configured_image_provider_available=True,
        )
        self.assertEqual(committed.image_generation_mode, "gpt-login")

    def test_provider_choice_is_rejected_when_current_provider_is_unavailable(self) -> None:
        project = self.pending_project(voiceover_mode="disabled")
        selection = self.selection(project, provider_available=True)
        before = project.path("project.json").read_bytes()

        with self.assertRaisesRegex(InitialApprovalError, "合法通过选项"):
            approve_initial_project(
                project.root,
                selection,
                gpt_login_image_generation_available=False,
                configured_image_provider_available=False,
            )

        self.assertEqual(project.path("project.json").read_bytes(), before)

    def test_current_option_structure_is_matched_field_by_field(self) -> None:
        project = self.pending_project(voiceover_mode="disabled")
        original = self.selection(project)
        tampering = (
            {"choiceId": "approve-forged"},
            {"optionNumber": original["optionNumber"] + 1},
            {"selectedText": original["selectedText"] + "篡改"},
            {"backgroundMusicEnabled": True},
            {"agentApprovalEnabled": False},
            {"matchedBy": "revision_sentence"},
        )
        before = project.path("project.json").read_bytes()

        for change in tampering:
            forged = dict(original)
            forged.update(change)
            with self.subTest(change=change), self.assertRaises(InitialApprovalError):
                approve_initial_project(
                    project.root,
                    forged,
                    configured_image_provider_available=True,
                )
            self.assertEqual(project.path("project.json").read_bytes(), before)

    def test_project_commit_failure_restores_pending_project(self) -> None:
        project = self.pending_project(voiceover_mode="edge-tts")
        project_path = project.path("project.json")
        project_before = project_path.read_bytes()
        real_replace = os.replace
        failed = False

        def fail_project_commit_once(source, target):
            nonlocal failed
            if Path(target) == project_path and not failed:
                failed = True
                raise OSError("模拟 project.json 提交失败")
            return real_replace(source, target)

        with (
            mock.patch.object(
                approval_module.os,
                "replace",
                side_effect=fail_project_commit_once,
            ),
            self.assertRaisesRegex(InitialApprovalError, "已恢复 pending"),
        ):
            approve_initial_project(
                project.root,
                self.selection(project),
                configured_image_provider_available=True,
            )

        self.assertEqual(project_path.read_bytes(), project_before)
        pending = load_project(
            project.root,
            allow_pending_initial_approval=True,
        )
        self.assertTrue(pending.pending_initial_approval)


if __name__ == "__main__":
    unittest.main()
