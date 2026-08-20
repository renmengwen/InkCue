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

    def test_readme_formal_path_keeps_all_human_approval_and_edge_commands(self) -> None:
        readme = normalized_command_text(read_document("README.md"))
        required_commands = (
            "scripts\\generate_voiceover.py sample",
            "scripts\\generate_voiceover.py approve-sample",
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

    def test_content_image_prompt_mapping_to_formal_prompt_is_documented(self) -> None:
        image_generation = read_document("references/image-generation.md")

        self.assertIn("imagePrompt", image_generation)
        self.assertIn("formal", image_generation)
        self.assertIn("prompt", image_generation)
        self.assertRegex(
            image_generation,
            r"`?imagePrompt`?\s*(?:→|->)\s*(?:formal\s+)?`?prompt`?",
        )
        self.assertIn("coordinator", image_generation)
        self.assertIn("确定性", image_generation)

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


if __name__ == "__main__":
    unittest.main()
