from __future__ import annotations

import math
import shutil
import struct
import subprocess
import sys
import unittest
import uuid
import wave
from pathlib import Path
from unittest import mock


SKILL_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = SKILL_ROOT / "scripts"
TEST_RUNS = Path(r"D:\SRTWhiteboard\.test-runs")
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import audio_normalization  # noqa: E402


def _run(argv: list[str]) -> None:
    completed = subprocess.run(
        argv,
        shell=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if completed.returncode != 0:
        raise AssertionError(completed.stderr)


def _write_pcm_wav(path: Path, *, sample_rate: int = 44100, channels: int = 2) -> None:
    frame_count = max(1, sample_rate // 5)
    with wave.open(str(path), "wb") as writer:
        writer.setnchannels(channels)
        writer.setsampwidth(2)
        writer.setframerate(sample_rate)
        frames = bytearray()
        for index in range(frame_count):
            sample = int(8000 * math.sin(2 * math.pi * 440 * index / sample_rate))
            packed = struct.pack("<h", sample)
            frames.extend(packed * channels)
        writer.writeframes(bytes(frames))


class AudioNormalizationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
            raise unittest.SkipTest("本机没有 FFmpeg/ffprobe")
        TEST_RUNS.mkdir(parents=True, exist_ok=True)

    def setUp(self) -> None:
        self.root = (TEST_RUNS / f"c2-audio-{uuid.uuid4().hex}").resolve()
        self.work = self.root / ".work" / "voice-generate-test"
        self.output = self.root / "audio" / "segments" / "unit-0001.wav"
        self.work.mkdir(parents=True)
        self.output.parent.mkdir(parents=True)
        self.source_wav = self.root / "source.wav"
        _write_pcm_wav(self.source_wav)

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def _make_mp3(self) -> bytes:
        path = self.root / "source.mp3"
        _run(
            [
                shutil.which("ffmpeg") or "ffmpeg",
                "-y",
                "-loglevel",
                "error",
                "-i",
                str(self.source_wav),
                "-c:a",
                "libmp3lame",
                path.as_posix(),
            ]
        )
        return path.read_bytes()

    def test_mp3_is_normalized_and_published_as_strict_canonical_wav(self) -> None:
        result = audio_normalization.normalize_and_publish(
            self._make_mp3(),
            self.output,
            work_dir=self.work,
            declared_format="audio/mpeg",
        )

        self.assertEqual(result.path, self.output.resolve())
        self.assertEqual(result.codec, "pcm_s16le")
        self.assertEqual(result.sampleRate, 24000)
        self.assertEqual(result.channels, 1)
        self.assertGreater(result.durationMs, 0)
        self.assertEqual(result.bytes, self.output.stat().st_size)
        self.assertRegex(result.sha256, r"^[0-9a-f]{64}$")
        self.assertEqual(self.output.read_bytes()[:4], b"RIFF")
        self.assertEqual(self.output.read_bytes()[8:12], b"WAVE")
        self.assertEqual(list(self.work.iterdir()), [])
        self.assertNotIn("path", result.manifest_media())

    def test_raw_media_with_video_stream_is_rejected(self) -> None:
        source = self.root / "with-video.mp4"
        _run(
            [
                shutil.which("ffmpeg") or "ffmpeg",
                "-y",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                "color=c=black:s=64x64:r=10:d=0.2",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:sample_rate=24000:duration=0.2",
                "-map",
                "0:v:0",
                "-map",
                "1:a:0",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                str(source),
            ]
        )

        with self.assertRaisesRegex(audio_normalization.AudioValidationError, "不含视频"):
            audio_normalization.normalize_and_publish(
                source.read_bytes(),
                self.output,
                work_dir=self.work,
                declared_format="video/mp4",
            )

        self.assertFalse(self.output.exists())
        self.assertEqual(list(self.work.iterdir()), [])

    def test_corrupt_media_fails_and_cleans_only_current_normalization_dir(self) -> None:
        sentinel_dir = self.work / "another-run"
        sentinel_dir.mkdir()
        sentinel = sentinel_dir / "keep.txt"
        sentinel.write_text("keep", encoding="utf-8")

        with self.assertRaises(audio_normalization.AudioValidationError):
            audio_normalization.normalize_and_publish(
                b"not media",
                self.output,
                work_dir=self.work,
                declared_format="audio/mpeg",
            )

        self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep")
        self.assertEqual([path.name for path in self.work.iterdir()], ["another-run"])

    def test_tool_timeout_is_explicit_and_temporary_files_are_cleaned(self) -> None:
        timeout = subprocess.TimeoutExpired(cmd=["ffprobe"], timeout=0.01)
        with mock.patch.object(audio_normalization.subprocess, "run", side_effect=timeout):
            with self.assertRaises(audio_normalization.AudioToolTimeoutError):
                audio_normalization.normalize_and_publish(
                    self.source_wav.read_bytes(),
                    self.output,
                    work_dir=self.work,
                    declared_format="audio/wav",
                    timeout_seconds=0.01,
                )

        self.assertFalse(self.output.exists())
        self.assertEqual(list(self.work.iterdir()), [])

    def test_atomic_publish_failure_preserves_existing_official_file(self) -> None:
        old = b"existing-official-wav"
        self.output.write_bytes(old)
        with mock.patch.object(
            audio_normalization.os,
            "replace",
            side_effect=PermissionError("simulated publish failure"),
        ):
            with self.assertRaises(audio_normalization.AtomicAudioPublishError):
                audio_normalization.normalize_and_publish(
                    self._make_mp3(),
                    self.output,
                    work_dir=self.work,
                    declared_format="audio/mpeg",
                )

        self.assertEqual(self.output.read_bytes(), old)
        self.assertEqual(list(self.work.iterdir()), [])

    def test_candidate_work_dir_must_be_same_project_dot_work(self) -> None:
        foreign_work = self.root / "foreign" / ".work" / "run"
        with self.assertRaisesRegex(
            audio_normalization.AtomicAudioPublishError, "项目 .work"
        ):
            audio_normalization.normalize_and_publish(
                self.source_wav.read_bytes(),
                self.output,
                work_dir=foreign_work,
                declared_format="audio/wav",
            )
        self.assertFalse(self.output.exists())

    def test_strict_validation_rejects_noncanonical_wav_and_non_riff(self) -> None:
        with self.assertRaisesRegex(audio_normalization.AudioValidationError, "采样率"):
            audio_normalization.validate_canonical_wav(self.source_wav)

        fake = self.root / "fake.wav"
        fake.write_bytes(b"not-a-wave-file")
        with self.assertRaisesRegex(audio_normalization.AudioValidationError, "RIFF/WAVE"):
            audio_normalization.validate_canonical_wav(fake)


if __name__ == "__main__":
    unittest.main()
