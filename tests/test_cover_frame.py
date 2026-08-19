from __future__ import annotations

import tempfile
import unittest
import shutil
import subprocess
from pathlib import Path
from unittest import mock

from PIL import Image

from scripts.cover_frame import (
    COVER_FRAME_RANGE,
    COVER_RELATIVE_PATH,
    attach_cover_manifest,
    cover_record,
    replace_first_frame,
)


class _Project:
    def __init__(self, root: Path) -> None:
        self.root = root

    def path(self, relative: str | Path) -> Path:
        return self.root / relative

    @property
    def render_profile(self) -> dict[str, object]:
        return {
            "width": 1920,
            "height": 1080,
            "fps": 60,
            "pixelFormat": "yuv420p",
        }


class CoverFrameTests(unittest.TestCase):
    def test_missing_cover_is_noop_and_manifest_can_be_empty(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "source.mp4"
            source.write_bytes(b"source")
            target = root / "target.mp4"
            project = _Project(root)
            self.assertIsNone(cover_record(project))
            self.assertIsNone(
                replace_first_frame(source, target, project=project, expected_frame_count=3)
            )
            self.assertEqual(target.read_bytes(), b"source")
            manifest: dict[str, object] = {}
            attach_cover_manifest(manifest, None)
            self.assertIsNone(manifest["cover"])

    def test_cover_record_has_sha_and_single_frame_visual_exclusion(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            cover = root / COVER_RELATIVE_PATH
            cover.parent.mkdir(parents=True)
            cover.write_bytes(b"cover-bytes")
            record = cover_record(_Project(root))
            assert record is not None
            self.assertEqual(record["file"], COVER_RELATIVE_PATH)
            self.assertEqual(record["frameRange"], COVER_FRAME_RANGE)
            self.assertTrue(record["visualReviewExcluded"])
            self.assertEqual(len(record["sha256"]), 64)

    @unittest.skipUnless(shutil.which("ffmpeg"), "requires ffmpeg")
    def test_real_replacement_changes_only_first_frame_and_preserves_count(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            project = _Project(root)
            cover = root / COVER_RELATIVE_PATH
            cover.parent.mkdir(parents=True)
            Image.new("RGB", (1920, 1080), (20, 40, 220)).save(cover, format="PNG")
            source = root / "source.mp4"
            target = root / "target.mp4"
            subprocess.run(
                [
                    "ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
                    "-i", "color=c=red:s=1920x1080:r=60", "-frames:v", "4",
                    "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(source),
                ],
                check=True,
            )
            replace_first_frame(source, target, project=project, expected_frame_count=4)
            probe = subprocess.run(
                ["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0",
                 "-show_entries", "stream=nb_read_frames", "-of", "csv=p=0", str(target)],
                check=True, capture_output=True, text=True,
            )
            self.assertEqual(probe.stdout.strip(), "4")
            first = root / "first.png"
            subprocess.run(
                ["ffmpeg", "-y", "-loglevel", "error", "-i", str(target), "-frames:v", "1", str(first)],
                check=True,
            )
            with Image.open(first) as image:
                red, green, blue = image.convert("RGB").getpixel((960, 540))
            self.assertGreater(blue, red)

    @mock.patch("scripts.cover_frame.subprocess.run")
    @mock.patch("scripts.cover_frame.shutil.which", return_value="ffmpeg")
    def test_replacement_command_keeps_expected_count_and_concat(self, _which: object, run: mock.Mock) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            cover = root / COVER_RELATIVE_PATH
            cover.parent.mkdir(parents=True)
            cover.write_bytes(b"cover")
            source = root / "source.mp4"
            source.write_bytes(b"source")
            run.return_value.returncode = 0
            target = root / "target.mp4"
            result = replace_first_frame(
                source,
                target,
                project=_Project(root),
                expected_frame_count=17,
            )
            self.assertIsNotNone(result)
            command = run.call_args.args[0]
            self.assertIn("concat=n=2:v=1:a=0", command[command.index("-filter_complex") + 1])
            self.assertEqual(command[command.index("-frames:v") + 1], "17")
            self.assertEqual(command[command.index("-an")], "-an")


if __name__ == "__main__":
    unittest.main()
