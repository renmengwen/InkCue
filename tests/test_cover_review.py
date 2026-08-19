from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from cover_review import CoverReviewError, load_cover_review  # noqa: E402
from project_workspace import sha256_file  # noqa: E402


class CoverReviewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="srt-cover-review-"))
        (self.root / "manifests").mkdir()
        (self.root / "previews").mkdir()
        self.cover = self.root / "previews" / "social-cover.png"
        self.cover.write_bytes(b"cover-png-fixture")
        self.project = SimpleNamespace(
            root=self.root,
            project_id="project-1",
            path=lambda relative: self.root / relative,
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.root)

    def _write(self, value: dict[str, object]) -> None:
        (self.root / "manifests" / "cover-manifest.json").write_text(
            json.dumps(value), encoding="utf-8"
        )

    def test_accepts_wrapped_cover_and_freezes_visual_exemption(self) -> None:
        self._write(
            {
                "schemaVersion": 1,
                "projectId": "project-1",
                "cover": {
                    "file": "previews/social-cover.png",
                    "sha256": sha256_file(self.cover),
                    "bytes": self.cover.stat().st_size,
                    "frameRange": {"startFrame": 0, "endFrameExclusive": 1},
                    "visualReviewExcluded": True,
                    "semanticSource": "whole_video",
                },
            }
        )
        evidence = load_cover_review(self.project)
        assert evidence is not None
        self.assertEqual(evidence["frameRange"], {"startFrame": 0, "endFrameExclusive": 1})
        self.assertTrue(evidence["visualReviewExcluded"])
        self.assertFalse(evidence["technicalChecksExcluded"])

    def test_rejects_cover_without_visual_exemption(self) -> None:
        self._write(
            {
                "file": "previews/social-cover.png",
                "sha256": sha256_file(self.cover),
                "frameRange": {"startFrame": 0, "endFrameExclusive": 1},
            }
        )
        with self.assertRaisesRegex(CoverReviewError, "visualReviewExcluded"):
            load_cover_review(self.project)

    def test_missing_manifest_is_optional(self) -> None:
        self.assertIsNone(load_cover_review(self.project))
        with self.assertRaises(CoverReviewError):
            load_cover_review(self.project, required=True)


if __name__ == "__main__":
    unittest.main()
