from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from content_source import ContentSourceError, validate_source_package  # noqa: E402
import prepare_source as prepare_source_module  # noqa: E402
from prepare_source import prepare_source  # noqa: E402


EXAMPLE = ROOT / "examples" / "topic-habit-loop-content-draft.json"


class PrepareSourceCliTests(unittest.TestCase):
    def test_cli_writes_four_file_package_and_machine_readable_summary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "准备包"
            completed = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "prepare_source.py"),
                    "--draft",
                    str(EXAMPLE),
                    "--output-dir",
                    str(output),
                ],
                cwd=ROOT,
                text=True,
                encoding="utf-8",
                capture_output=True,
                shell=False,
                check=False,
                env={**os.environ, "PYTHONIOENCODING": "utf-8"},
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(
                {path.name for path in output.iterdir()},
                {"input.json", "source.srt", "generation-plan.json", "manifest.json"},
            )
            package = validate_source_package(
                output / "input.json",
                output / "manifest.json",
                output / "source.srt",
                output / "generation-plan.json",
            )
            self.assertIn(f"CONTENT_DRAFT_IDENTITY={package.content_draft_identity}", completed.stdout)
            self.assertIn("INPUT_MODE=topic", completed.stdout)
            self.assertIn("TARGET_DURATION_SECONDS=60", completed.stdout)
            self.assertIn("CUE_COUNT=4", completed.stdout)
            self.assertIn("SCENE_COUNT=2", completed.stdout)

    def test_repeated_run_is_byte_deterministic_and_atomically_replaces_valid_package(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "package"
            prepare_source(EXAMPLE, output)
            first = {path.name: path.read_bytes() for path in output.iterdir()}
            prepare_source(EXAMPLE, output)
            second = {path.name: path.read_bytes() for path in output.iterdir()}
            self.assertEqual(first, second)

    def test_invalid_draft_does_not_overwrite_last_valid_package(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "package"
            prepare_source(EXAMPLE, output)
            before = {path.name: path.read_bytes() for path in output.iterdir()}
            invalid = json.loads(EXAMPLE.read_text(encoding="utf-8"))
            invalid["rewritePolicy"] = "preserve"
            bad_path = root / "bad.json"
            bad_path.write_text(json.dumps(invalid, ensure_ascii=False), encoding="utf-8")
            with self.assertRaises(ContentSourceError):
                prepare_source(bad_path, output)
            after = {path.name: path.read_bytes() for path in output.iterdir()}
            self.assertEqual(before, after)

    def test_post_commit_validation_failure_restores_previous_valid_package(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "package"
            prepare_source(EXAMPLE, output)
            before = {path.name: path.read_bytes() for path in output.iterdir()}
            real_validate = prepare_source_module.validate_source_package
            calls = 0

            def fail_third(*args, **kwargs):
                nonlocal calls
                calls += 1
                if calls == 3:
                    raise ContentSourceError("模拟提交后重验失败")
                return real_validate(*args, **kwargs)

            with mock.patch.object(
                prepare_source_module, "validate_source_package", side_effect=fail_third
            ):
                with self.assertRaisesRegex(ContentSourceError, "提交后"):
                    prepare_source(EXAMPLE, output)
            after = {path.name: path.read_bytes() for path in output.iterdir()}
            self.assertEqual(before, after)

    def test_tampering_any_bound_file_is_detected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("input.json", "source.srt", "generation-plan.json", "manifest.json"):
                output = root / name.replace(".", "-")
                prepare_source(EXAMPLE, output)
                path = output / name
                path.write_bytes(path.read_bytes() + b" ")
                with self.subTest(name=name), self.assertRaises(ContentSourceError):
                    validate_source_package(
                        output / "input.json",
                        output / "manifest.json",
                        output / "source.srt",
                        output / "generation-plan.json",
                    )

    def test_json_artifacts_contain_no_absolute_paths_or_secret_keys(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "package"
            prepare_source(EXAMPLE, output)
            combined = "\n".join(
                (output / name).read_text(encoding="utf-8")
                for name in ("input.json", "generation-plan.json", "manifest.json")
            )
            self.assertNotRegex(combined, r"(?i)[a-z]:[\\/]")
            self.assertNotRegex(combined, r'(?i)"(?:api[_-]?key|token|cookie|secret|authorization)"\s*:')


if __name__ == "__main__":
    unittest.main()
