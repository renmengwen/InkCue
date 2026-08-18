from __future__ import annotations

import contextlib
import hashlib
import io
import json
import shutil
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from PIL import Image


SKILL_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = SKILL_ROOT / "scripts"
TEST_ROOT = Path(tempfile.gettempdir()) / "srt-whiteboard-visual-review-prepare-tests"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import project_workspace  # noqa: E402
import validate_generated_images  # noqa: E402


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class VisualReviewPrepareCliTests(unittest.TestCase):
    def setUp(self) -> None:
        TEST_ROOT.mkdir(parents=True, exist_ok=True)
        self.project_root = (TEST_ROOT / str(uuid.uuid4())).resolve()
        for relative in (
            "source",
            "planning",
            "scenes",
            "manifests",
            "previews",
            "output",
            ".work",
        ):
            (self.project_root / relative).mkdir(parents=True, exist_ok=True)
        source = self.project_root / "source" / "source.srt"
        source.write_text(
            "1\n00:00:00,000 --> 00:00:01,000\n测试\n",
            encoding="utf-8",
        )
        project_id = str(uuid.uuid4())
        project = {
            "schemaVersion": 1,
            "projectId": project_id,
            "projectName": self.project_root.name,
            "createdAt": "2026-08-17T00:00:00+08:00",
            "source": {"file": "source/source.srt", "sha256": _sha256(source)},
            "paths": {
                "planning": "planning",
                "scenes": "scenes",
                "manifests": "manifests",
                "previews": "previews",
                "output": "output",
                "work": ".work",
            },
        }
        (self.project_root / "project.json").write_text(
            json.dumps(project, ensure_ascii=False),
            encoding="utf-8",
        )
        scene = {
            "sceneId": "scene-01",
            "name": "概念",
            "subtitleRange": {"startMs": 0, "endMs": 1000},
            "sceneDurationMs": 1000,
            "prompt": "一个清晰概念",
            "outputFile": "scene-01-概念.png",
        }
        plan = {
            "schemaVersion": 1,
            "projectId": project_id,
            "outputCanvas": {
                "width": 1920,
                "height": 1080,
                "background": "#F5EBD7",
                "fit": "contain",
            },
            "globalPrompt": "统一白板线稿，不含文字",
            "constraints": {"forbidText": True},
            "scenesDirectory": "scenes",
            "manifestFile": "manifests/generation-manifest.json",
            "scenes": [scene],
        }
        plan_path = self.project_root / "planning" / "generation-plan.json"
        plan_path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
        image_path = self.project_root / "scenes" / scene["outputFile"]
        Image.new("RGB", (1920, 1080), "#F5EBD7").save(image_path, "PNG")
        manifest = {
            "schemaVersion": 1,
            "projectId": project_id,
            "generationPlan": {
                "file": "planning/generation-plan.json",
                "sha256": _sha256(plan_path),
            },
            "createdAt": "2026-08-17T00:00:00+08:00",
            "updatedAt": "2026-08-17T00:00:00+08:00",
            "completedAt": "2026-08-17T00:00:00+08:00",
            "summary": {"sceneTotal": 1, "successCount": 1, "failedCount": 0},
            "runs": [],
            "scenes": [
                {
                    "sceneId": "scene-01",
                    "outputFile": scene["outputFile"],
                    "status": "validated",
                    "imageSha256": _sha256(image_path),
                }
            ],
        }
        (self.project_root / "manifests" / "generation-manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        resolved = self.project_root.resolve()
        self.assertEqual(resolved.parent, TEST_ROOT.resolve())
        shutil.rmtree(resolved)

    def test_cli_emits_frozen_spawn_package_without_spawning_or_approval(self) -> None:
        workspace = mock.Mock()
        workspace.config = SimpleNamespace(
            root=self.project_root.parent,
            for_stage=lambda _stage: 1,
            for_role=lambda _role: 3,
        )
        workspace.load_project.side_effect = project_workspace.load_project
        stdout = io.StringIO()
        with (
            mock.patch.object(
                validate_generated_images.ProjectWorkspace,
                "from_config",
                return_value=workspace,
            ),
            contextlib.redirect_stdout(stdout),
        ):
            exit_code = validate_generated_images.main(
                ["--project", str(self.project_root), "--prepare-visual-review"]
            )

        summary = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 0, summary)
        review = summary["visualReview"]
        self.assertEqual(review["mode"], "host_collaboration_dispatch")
        self.assertEqual(review["status"], "ready_for_host_spawn")
        self.assertTrue(review["dispatchAllowed"])
        self.assertTrue(review["preparedOnly"])
        self.assertFalse(review["hostSpawnExecuted"])
        self.assertEqual(review["peakChildAgents"], 0)
        self.assertEqual(review["taskAgents"], [])

        package = review["spawnPackage"]
        self.assertEqual(
            package["contractVersion"],
            "whiteboard-host-spawn-package-v1",
        )
        self.assertTrue(package["preparedOnly"])
        self.assertFalse(package["hostSpawnExecuted"])
        for key in (
            "taskJsonPath",
            "roleContractPath",
            "allowedAttemptDir",
            "resultJsonPath",
        ):
            self.assertTrue(Path(package[key]).is_absolute(), (key, package[key]))
        self.assertEqual(package["taskJsonPath"], str(Path(package["allowedAttemptDir"]) / "task.json"))
        self.assertEqual(
            package["roleContractPath"],
            str(Path(package["allowedAttemptDir"]) / "role-contract.md"),
        )
        spawn_call = package["spawnAgentCall"]
        self.assertEqual(spawn_call["fork_turns"], "none")
        self.assertIn(f"TASK_SHA256={package['taskSha256']}", spawn_call["message"])
        self.assertIn(package["taskJsonPath"], spawn_call["message"])
        self.assertIn(package["roleContractPath"], spawn_call["message"])

        attempt_dir = Path(package["allowedAttemptDir"])
        self.assertEqual(
            sorted(path.name for path in attempt_dir.iterdir()),
            ["role-contract.md", "task.json"],
        )
        self.assertFalse((self.project_root / "manifests" / "image-review-approval.json").exists())


if __name__ == "__main__":
    unittest.main()
