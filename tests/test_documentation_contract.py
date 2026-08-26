from __future__ import annotations

import json
import re
import unittest
from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parent.parent


def read_document(relative_path: str) -> str:
    return (SKILL_ROOT / relative_path).read_text(encoding="utf-8")


def normalized_command_text(document: str) -> str:
    return re.sub(r"\s+", " ", document)


class DocumentationContractTests(unittest.TestCase):
    def test_phase0_pending_project_and_joint_choice_contract_is_explicit(self) -> None:
        documents = "\n".join(
            read_document(path)
            for path in (
                "SKILL.md",
                "README.md",
                "references/phase-0-content.md",
                "references/content-input.md",
                "references/recovery-and-identity.md",
            )
        )
        for required in (
            "pending_initial_approval",
            "initialApproval",
            "SAMPLE_IDENTITY",
            "user_joint_content_and_sample",
            "user_joint_initial_approval",
            "user_joint_silent_plan",
            "原子",
            "旧项目",
            "完整自然语言",
            "编号",
            "active voice provider",
        ):
            with self.subTest(required=required):
                self.assertIn(required, documents)

        for sentence in (
            "草案和样音通过，使用 BGM，后续由 AI 自主推进至成片。",
            "草案和样音通过，不使用 BGM，后续由 AI 自主推进至成片。",
            "草案和样音通过，使用 BGM，后续由我逐阶段确认。",
            "草案和样音通过，不使用 BGM，后续由我逐阶段确认。",
            "草案需要修改，当前样音暂不批准。修改意见：……",
            "草案通过，样音需要调整，其他方案保持不变。调整意见：……",
            "草案和样音都需要修改。修改意见：……",
        ):
            with self.subTest(sentence=sentence):
                self.assertIn(sentence, documents)

    def test_autonomous_audio_contract_uses_sample_authorization_without_fake_listening(self) -> None:
        documents = "\n".join(
            read_document(path)
            for path in (
                "SKILL.md",
                "README.md",
                "references/phase-0-content.md",
                "references/content-input.md",
                "references/voiceover.md",
                "references/subtitles.md",
                "references/phase-4-runner.md",
                "references/recovery-and-identity.md",
                "references/subagent-orchestration.md",
            )
        )
        for required in (
            "唯一声音主观 Gate",
            "用户样音授权后的技术推进",
            "approvalBasis",
            "reviewBasis",
            "canonical WAV",
            "FunASR",
            "原稿对齐",
            "完整解码",
            "不得声称 AI 完整",
            "人工模式",
            "视觉 Gate",
        ):
            with self.subTest(required=required):
                self.assertIn(required, documents)

        obsolete = (
            "为 `true` 时由 coordinator AI 真实试听",
            "AI 代理模式则必须交回 coordinator，由具备真实视听能力的审阅者完整看片/听音",
            "`true`：保留同样数量和边界的 Gate",
        )
        for claim in obsolete:
            with self.subTest(claim=claim):
                self.assertNotIn(claim, documents)

    def test_readme_documents_current_formal_scene_render_contract(self) -> None:
        readme = read_document("README.md")

        for required_term in (
            "sceneRender",
            "configuredSceneRenderConcurrency",
            "readySceneCount",
            "effectiveSceneRenderConcurrency",
            "顺序",
            "任一必需幕失败",
            "FAIL",
            "scene review",
        ):
            with self.subTest(required_term=required_term):
                self.assertIn(required_term, readme)

    def test_readme_formal_path_keeps_joint_initial_and_downstream_approval_commands(self) -> None:
        readme = normalized_command_text(read_document("README.md"))
        required_commands = (
            "scripts\\generate_voiceover.py sample",
            "scripts\\approve_initial_project.py",
            "scripts\\generate_voiceover.py full",
            "scripts\\generate_voiceover.py approve-full",
            "scripts\\approve_annotation_review.py",
            "scripts\\approve_scene_review.py",
            "scripts\\merge_scenes.py",
            "scripts\\mux_voiceover.py",
            "scripts\\approve_final_media.py",
        )

        for command in required_commands:
            with self.subTest(command=command):
                self.assertIn(command, readme)

        self.assertIn("current approved scene review bundle", readme)

    def test_readme_test_runtime_and_ci_status_categories_are_explicit(self) -> None:
        readme = read_document("README.md")
        self.assertIn('D:\\SRTWhiteboard\\runtime\\.venv\\Scripts\\python.exe', readme)
        for status in ("自动 `PASS`", "`SKIP`", "`BLOCKED`", "人工 Gate", "待确认"):
            with self.subTest(status=status):
                self.assertIn(status, readme)

    def test_runtime_interpreter_is_resolved_before_business_commands(self) -> None:
        skill = read_document("SKILL.md")
        readme = read_document("README.md")

        for document in (skill, readme):
            with self.subTest(document="SKILL.md" if document is skill else "README.md"):
                self.assertIn("python scripts/prepare_env.py --check", document.replace("\\", "/"))
                self.assertIn("ENV_PY", document)
                self.assertIn("绝对路径", document)
                self.assertIn("裸 `python`", document)

        self.assertIn("任何业务脚本、导入探测或渲染启动前", skill)
        self.assertIn("不得把系统 Python 缺少 `cv2` 误报成 OpenCV 未安装", skill)
        self.assertIn("不同 Codex 工具调用之间不要假设", readme)

    def test_content_image_prompt_mapping_to_formal_prompt_is_documented(self) -> None:
        image_generation = read_document("references/image-generation.md")
        orchestration = read_document("references/subagent-orchestration.md")

        self.assertIn("imagePrompt", image_generation)
        self.assertIn("formal", image_generation)
        self.assertIn("prompt", image_generation)
        self.assertRegex(
            image_generation,
            r"`?imagePrompt`?\s*(?:→|->)\s*(?:formal\s+)?`?prompt`?",
        )
        self.assertIn("coordinator", image_generation)
        self.assertIn("确定性", image_generation)
        self.assertIn(
            "formal.scenes[i].prompt = candidate.scenes[i].imagePrompt",
            orchestration,
        )
        for invariant in (
            "逐字复制",
            "不得再次 trim",
            "不得再次 trim、拼接、调用模型、改写语义或调换 scene/cue 顺序",
            "不允许 formal plan 保留 `imagePrompt`",
            "任何需要改变提示词的意见都必须回到新的 revision attempt，并回到阶段 0 取得用户对实质新方案的确认",
            "`agentApprovalEnabled` 不授权 coordinator 改写已冻结用户意图",
        ):
            with self.subTest(invariant=invariant):
                self.assertIn(invariant, orchestration)

    def test_child_prompt_contract_is_locator_only_and_excludes_sensitive_context(self) -> None:
        orchestration = read_document("references/subagent-orchestration.md")
        annotation_role = read_document("references/annotation-drafting-role.md")

        for allowed in (
            "taskId`/`taskKind`",
            "taskSha256",
            "roleContractPath`/`roleContractSha256`",
            "TASK_JSON_PATH",
            "ALLOWED_ATTEMPT_DIR",
            "固定的返回字段/枚举",
            "formalWritesAllowed:false",
            "approvalWritesAllowed:false",
        ):
            with self.subTest(allowed=allowed):
                self.assertIn(allowed, orchestration)

        for forbidden in (
            "完整主对话",
            "完整 SRT/正文",
            "完整 scene 数组",
            "provider 名称",
            "API key/token/cookie",
            "批准或拒绝内容",
            "完整工具日志",
            "未冻结状态",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertIn(forbidden, orchestration)
        self.assertIn("宿主 prompt 只是冻结定位器", annotation_role)
        self.assertIn("不得作为判断依据或写入 `agent.log`", annotation_role)

    def test_workspace_examples_separate_safe_baseline_from_performance_example(self) -> None:
        safe = json.loads(read_document("config/workspace.example.json"))
        performance = json.loads(read_document("config/workspace.performance.example.json"))

        safe_execution = safe["execution"]
        performance_execution = performance["execution"]
        for pool in ("agents", "concurrency"):
            with self.subTest(pool=pool):
                self.assertEqual(set(safe_execution[pool]), set(performance_execution[pool]))
                self.assertTrue(all(value == 1 for value in safe_execution[pool].values()))
                self.assertTrue(all(value >= 1 for value in performance_execution[pool].values()))

        self.assertTrue(
            any(value > 1 for value in performance_execution["agents"].values())
            or any(value > 1 for value in performance_execution["concurrency"].values())
        )

    def test_current_documents_do_not_restore_obsolete_serial_only_claims(self) -> None:
        current_documents = [
            SKILL_ROOT / "SKILL.md",
            SKILL_ROOT / "README.md",
            *(SKILL_ROOT / "references").glob("*.md"),
        ]
        obsolete_claims = (
            "Phase 8 多幕正式候选并发仍未实施",
            "`sceneRender=1`；场景只串行渲染",
            "当前版本 `sceneRender` 无条件只能为 1",
            "正式多幕并发属于未来设计备忘",
            "scene 仍串行渲染",
        )

        for path in current_documents:
            document = path.read_text(encoding="utf-8")
            for claim in obsolete_claims:
                with self.subTest(path=path.relative_to(SKILL_ROOT), claim=claim):
                    self.assertNotIn(claim, document)

    def test_historical_phase8_claims_are_explicitly_marked_as_historical(self) -> None:
        plan = read_document("docs/superpowers/plans/2026-08-20-srt-whiteboard-optimization-plan.md")
        self.assertIn("历史设计状态（2026-08-15）", plan)
        self.assertIn("当前实现状态请以 SKILL.md、scripts/render_stream_whiteboard.py 和测试为准", plan)


if __name__ == "__main__":
    unittest.main()
