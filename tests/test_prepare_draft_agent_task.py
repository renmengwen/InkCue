from __future__ import annotations

import argparse
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SKILL_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = SKILL_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import prepare_draft_agent_task as prepare  # noqa: E402
from agent_task_contract import TrustedTaskContext, validate_agent_task  # noqa: E402
from project_workspace import (  # noqa: E402
    ExecutionAgentConcurrency,
    WorkspaceConfig,
)


class PrepareDraftAgentTaskTests(unittest.TestCase):
    VISUAL_CLUSTER_CONTRACT = (
        "按视觉状态变化拆分",
        "不按具体名词类别机械拆分",
        "允许通过增加 scene 降低单图叙事负担",
        "不得预设固定场景数量",
        "每幕只表达一个核心视觉命题",
        "默认只安排一个主要视觉簇",
        "最多增加一个空间独立的辅助视觉簇",
        "明显、干净的纸面间隔",
        "不得互相包含、嵌套、遮挡或重叠",
        "跨簇的连续连接结构或共享连续基底",
        "视为同一个视觉簇整体绘制",
        "不得强拆成多个揭示单元",
    )

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.workspace_root = self.root / "workspace"
        self.workspace_root.mkdir()
        self.config_path = self.root / "workspace.local.json"
        self.config_path.write_text("{}", encoding="utf-8")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def workspace(self) -> WorkspaceConfig:
        return WorkspaceConfig(
            root=self.workspace_root,
            config_path=self.config_path,
            agents=ExecutionAgentConcurrency(default=3),
        )

    def args(self, role: str, draft_name: str, **overrides: object) -> argparse.Namespace:
        values: dict[str, object] = {
            "role": role,
            "draft_root": str(self.workspace_root / "drafts" / draft_name),
            "workspace_config": None,
            "run_id": f"run-{draft_name}",
            "task_id": None,
            "attempt": 1,
            "content_input": None,
            "source_srt": None,
            "target_sec": 30.0,
            "min_sec": 25.0,
            "max_sec": 35.0,
        }
        values.update(overrides)
        return argparse.Namespace(**values)

    def assert_task_valid(self, result: dict[str, object], task_id: str) -> dict[str, object]:
        draft_root = Path(str(result["draftRoot"]))
        context = TrustedTaskContext(
            self.workspace_root,
            draft_root,
            "draft",
            str(result["runId"]),
            task_id,
            int(result["attempt"]),
        )
        task = validate_agent_task(context.task_json, context)
        package = result["spawnPackage"]
        assert isinstance(package, dict)
        self.assertEqual(package["taskSha256"], task.task_sha256)
        self.assertEqual(package["roleContractSha256"], task.data["roleContractSha256"])
        self.assertEqual(package["allowedOutputs"], task.data["allowedOutputs"])
        self.assertIn("TASK_JSON_PATH=", package["spawnAgentCall"]["message"])
        self.assertIn("ALLOWED_ATTEMPT_DIR=", package["spawnAgentCall"]["message"])
        return dict(task.data)

    def assert_visual_cluster_contract(self, result: dict[str, object]) -> None:
        package = result["spawnPackage"]
        assert isinstance(package, dict)
        role_contract = Path(str(package["roleContractPath"]))
        contract_text = role_contract.read_text(encoding="utf-8")
        for required_text in self.VISUAL_CLUSTER_CONTRACT:
            with self.subTest(required_text=required_text):
                self.assertIn(required_text, contract_text)

    def test_content_prepare_freezes_input_and_emits_host_spawn_package(self) -> None:
        source = self.root / "content.json"
        source.write_text(
            json.dumps(
                {
                    "schemaVersion": 1,
                    "contractVersion": "whiteboard-content-input-v1",
                    "inputMode": "topic",
                    "topic": "为什么会拖延",
                    "body": None,
                    "rewritePolicy": "generate",
                    "targetDurationSeconds": 60,
                    "voiceoverMode": "edge-tts",
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        args = self.args("contentDrafting", "content-one", content_input=str(source))
        with mock.patch.object(prepare, "load_workspace_config", return_value=self.workspace()):
            result = prepare.prepare_draft_task(args)

        task = self.assert_task_valid(result, "content-draft")
        self.assertEqual(task["taskKind"], "contentDrafting")
        self.assertEqual(task["formalWritesAllowed"], False)
        self.assertEqual(result["dispatchAudit"]["mode"], "host_collaboration_dispatch")
        self.assertEqual(result["dispatchAudit"]["peakChildAgents"], 0)
        self.assertFalse(result["formalPublished"])
        self.assertFalse(result["approvalWritten"])
        self.assertTrue((Path(str(result["draftRoot"])) / "content-input.json").is_file())
        self.assert_visual_cluster_contract(result)

    def test_storyboard_prepare_parses_srt_and_freezes_both_inputs(self) -> None:
        source = self.root / "source.srt"
        source.write_text(
            "1\n00:00:00,000 --> 00:00:02,000\n第一句\n\n"
            "2\n00:00:02,500 --> 00:00:05,000\n第二句\n",
            encoding="utf-8",
        )
        args = self.args("storyboardPlanning", "story-one", source_srt=str(source))
        with mock.patch.object(prepare, "load_workspace_config", return_value=self.workspace()):
            result = prepare.prepare_draft_task(args)

        task = self.assert_task_valid(result, "storyboard")
        self.assertEqual(task["taskKind"], "storyboardPlanning")
        self.assertEqual(
            [Path(item["file"]).name for item in task["inputs"]],
            ["source.srt", "parsed-srt.json", "role-contract.md"],
        )
        parsed = json.loads((Path(str(result["draftRoot"])) / "parsed-srt.json").read_text(encoding="utf-8"))
        self.assertEqual(len(parsed["cues"]), 2)
        self.assertEqual(parsed["scenes"][0]["cueRange"], [1, 2])
        self.assertTrue(result["spawnPackage"]["hostSpawnRequired"])
        self.assert_visual_cluster_contract(result)

    def test_content_prepare_offers_host_spawn_call(self) -> None:
        source = self.root / "content.json"
        source.write_text(
            json.dumps(
                {
                    "schemaVersion": 1,
                    "contractVersion": "whiteboard-content-input-v1",
                    "inputMode": "text",
                    "topic": None,
                    "body": "原文",
                    "rewritePolicy": "preserve",
                    "targetDurationSeconds": 30,
                    "voiceoverMode": "edge-tts",
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        args = self.args("contentDrafting", "host-one", content_input=str(source))
        with mock.patch.object(
            prepare,
            "load_workspace_config",
            return_value=self.workspace(),
        ):
            result = prepare.prepare_draft_task(args)
        self.assertTrue(result["dispatchAudit"]["dispatchAllowed"])
        self.assertEqual(result["dispatchAudit"]["mode"], "host_collaboration_dispatch")
        self.assertIsNotNone(result["spawnPackage"]["spawnAgentCall"])


if __name__ == "__main__":
    unittest.main()
