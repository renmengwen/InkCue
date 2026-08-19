from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from cover_generation import collect_semantics, generate_cover  # noqa: E402


class CoverGenerationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        for rel in ("planning", "source", "scenes", "previews", "manifests"):
            (self.root / rel).mkdir()
        (self.root / "project.json").write_text(json.dumps({"projectId": "p-cover"}), encoding="utf-8")
        plan = {
            "schemaVersion": 1,
            "projectId": "p-cover",
            "outputCanvas": {"width": 1920, "height": 1080, "background": "#F5EBD7", "fit": "contain"},
            "globalPrompt": "whiteboard",
            "constraints": {"forbidText": True},
            "scenesDirectory": "scenes",
            "manifestFile": "manifests/generation-manifest.json",
            "scenes": [
                {"sceneId": "scene-01", "prompt": "plant seed", "outputFile": "scene-01.png"},
                {"sceneId": "scene-02", "prompt": "habit loop", "outputFile": "scene-02.png"},
            ],
        }
        (self.root / "planning" / "generation-plan.json").write_text(json.dumps(plan), encoding="utf-8")
        source = {
            "schemaVersion": 1,
            "contractVersion": "whiteboard-content-draft-v1",
            "inputMode": "topic",
            "topic": "把小行动变成稳定习惯",
            "body": None,
            "narrationCues": [
                {"cueId": "cue-001", "sceneId": "scene-01", "text": "先从一个微小动作开始。"},
                {"cueId": "cue-002", "sceneId": "scene-02", "text": "重复之后，行动会变成习惯。"},
            ],
            "scenes": [
                {"sceneId": "scene-01", "coreIdea": "降低开始门槛", "visualSubject": "一粒种子", "imagePrompt": "seed on paper"},
                {"sceneId": "scene-02", "coreIdea": "重复形成稳定回路", "visualSubject": "循环箭头", "imagePrompt": "habit loop"},
            ],
        }
        (self.root / "source" / "input.json").write_text(json.dumps(source, ensure_ascii=False), encoding="utf-8")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_collects_whole_video_semantics_and_all_scenes(self) -> None:
        semantics = collect_semantics(self.root)
        self.assertEqual(semantics["semanticSource"], "whole_video")
        self.assertEqual(semantics["topic"], "把小行动变成稳定习惯")
        self.assertEqual(len(semantics["narrationCues"]), 2)
        self.assertEqual([s["coreIdea"] for s in semantics["scenes"]], ["降低开始门槛", "重复形成稳定回路"])
        self.assertEqual(semantics["title"], "把小行动变成稳定习惯")

    def test_generates_deterministic_fallback_cover_and_manifest(self) -> None:
        manifest = generate_cover(self.root)
        cover = self.root / "previews" / "social-cover.png"
        self.assertTrue(cover.is_file())
        self.assertEqual(Image.open(cover).size, (1920, 1080))
        self.assertEqual(manifest["semanticSource"], "whole_video")
        self.assertRegex(manifest["sha256"], r"^[0-9a-f]{64}$")
        saved = json.loads((self.root / "manifests" / "cover-manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["sha256"], manifest["sha256"])
        self.assertTrue(saved["visualReviewExcluded"])
        self.assertEqual(saved["coverFrameRange"], {"startFrame": 0, "endFrameExclusive": 1})

    def test_uses_existing_scene_as_visual_fallback(self) -> None:
        Image.new("RGB", (1920, 1080), (220, 220, 220)).save(self.root / "scenes" / "scene-01.png")
        manifest = generate_cover(self.root)
        self.assertEqual(manifest["sourceSceneIds"], ["scene-01"])


if __name__ == "__main__":
    unittest.main()
