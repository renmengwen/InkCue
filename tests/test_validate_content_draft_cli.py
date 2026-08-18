from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "validate_content_draft.py"
EXAMPLE = ROOT / "examples" / "topic-habit-loop-content-draft.json"


def _tree_snapshot(root: Path) -> dict[str, tuple[object, ...]]:
    """同时冻结目录项、文件大小与内容 SHA，不能由 CLI 自述替代。"""
    snapshot: dict[str, tuple[object, ...]] = {}
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix()
        if path.is_dir():
            snapshot[relative] = ("directory",)
        elif path.is_file():
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            snapshot[relative] = ("file", path.stat().st_size, digest)
        else:
            snapshot[relative] = ("other",)
    return snapshot


class ValidateContentDraftCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_base = Path(tempfile.gettempdir()).resolve()
        self.assertEqual(self.temp_base.drive.upper(), "C:", "零写入测试必须位于 C 盘")

    def _case_root(self):
        return tempfile.TemporaryDirectory(
            prefix="whiteboard-content-draft-readonly-",
            dir=self.temp_base,
        )

    def _run(
        self,
        root: Path,
        arguments: list[str],
        *,
        stdin_bytes: bytes = b"",
    ) -> subprocess.CompletedProcess[bytes]:
        runtime = root / "runtime"
        workspace = root / "workspace"
        runtime.mkdir(exist_ok=True)
        workspace.mkdir(exist_ok=True)
        environment = {
            **os.environ,
            "TEMP": str(runtime),
            "TMP": str(runtime),
            "PYTHONPYCACHEPREFIX": str(runtime / "pycache"),
            "PYTHONIOENCODING": "utf-8",
        }
        return subprocess.run(
            [sys.executable, "-B", str(SCRIPT), *arguments],
            cwd=workspace,
            input=stdin_bytes,
            capture_output=True,
            shell=False,
            check=False,
            env=environment,
        )

    def _assert_read_only_run(
        self,
        root: Path,
        arguments: list[str],
        *,
        stdin_bytes: bytes = b"",
    ) -> subprocess.CompletedProcess[bytes]:
        runtime = root / "runtime"
        workspace = root / "workspace"
        runtime.mkdir(exist_ok=True)
        workspace.mkdir(exist_ok=True)
        sentinel_dir = workspace / "existing-directory"
        sentinel_dir.mkdir(exist_ok=True)
        sentinel = sentinel_dir / "sentinel.bin"
        if not sentinel.exists():
            sentinel.write_bytes(b"keep-existing-bytes\x00\xff")
        before = _tree_snapshot(root)
        completed = self._run(root, arguments, stdin_bytes=stdin_bytes)
        after = _tree_snapshot(root)
        self.assertEqual(after, before, "CLI 调用前后目录项、文件大小或 SHA-256 发生变化")
        return completed

    def test_stdin_validates_in_memory_and_reports_summary_without_content_or_paths(self) -> None:
        draft_bytes = EXAMPLE.read_bytes()
        draft = json.loads(draft_bytes.decode("utf-8"))
        with self._case_root() as directory:
            root = Path(directory)
            completed = self._assert_read_only_run(root, ["--stdin"], stdin_bytes=draft_bytes)

            self.assertEqual(completed.returncode, 0, completed.stderr.decode("utf-8"))
            result = json.loads(completed.stdout.decode("utf-8"))
            self.assertTrue(result["valid"])
            self.assertFalse(result["writesPerformed"])
            self.assertRegex(result["contentDraftIdentitySha256"], r"^[0-9a-f]{64}$")
            self.assertEqual(result["cueCount"], len(draft["narrationCues"]))
            self.assertEqual(result["sceneCount"], len(draft["scenes"]))
            combined = completed.stdout.decode("utf-8") + completed.stderr.decode("utf-8")
            self.assertNotIn(draft["topic"], combined)
            self.assertNotIn(str(root), combined)
            self.assertNotRegex(combined, r"(?i)[a-z]:[\\/]")

    def test_draft_mode_reads_confirmed_fixture_without_modifying_tree(self) -> None:
        with self._case_root() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            confirmed = workspace / "confirmed-content-draft.json"
            confirmed.write_bytes(EXAMPLE.read_bytes())
            completed = self._assert_read_only_run(root, ["--draft", str(confirmed)])

            self.assertEqual(completed.returncode, 0, completed.stderr.decode("utf-8"))
            result = json.loads(completed.stdout.decode("utf-8"))
            self.assertEqual(result["inputMode"], "topic")
            self.assertFalse(result["writesPerformed"])
            self.assertNotIn(str(confirmed), completed.stdout.decode("utf-8"))

    def test_invalid_stdin_exits_two_without_writes_or_sensitive_echo(self) -> None:
        invalid = json.loads(EXAMPLE.read_text(encoding="utf-8"))
        invalid["apiKey"] = "DO-NOT-ECHO-THIS-SECRET"
        invalid["topic"] = "DO-NOT-ECHO-THIS-BODY"
        invalid["scenes"][0]["imagePrompt"] = r"DO-NOT-ECHO C:\private\token.txt"
        payload = json.dumps(invalid, ensure_ascii=False).encode("utf-8")
        with self._case_root() as directory:
            root = Path(directory)
            completed = self._assert_read_only_run(root, ["--stdin"], stdin_bytes=payload)

            self.assertEqual(completed.returncode, 2)
            self.assertEqual(completed.stdout, b"")
            result = json.loads(completed.stderr.decode("utf-8"))
            self.assertEqual(
                result,
                {
                    "error": "content_draft_invalid",
                    "valid": False,
                    "writesPerformed": False,
                },
            )
            combined = completed.stdout.decode("utf-8") + completed.stderr.decode("utf-8")
            self.assertNotIn("DO-NOT-ECHO", combined)
            self.assertNotIn(str(root), combined)
            self.assertNotRegex(combined, r"(?i)[a-z]:[\\/]")

    def test_stdin_and_draft_are_mutually_exclusive_and_parse_errors_do_not_echo_path(self) -> None:
        with self._case_root() as directory:
            root = Path(directory)
            secret_path = root / "workspace" / "DO-NOT-ECHO-secret.json"
            completed = self._assert_read_only_run(
                root,
                ["--stdin", "--draft", str(secret_path)],
                stdin_bytes=EXAMPLE.read_bytes(),
            )

            self.assertEqual(completed.returncode, 2)
            self.assertEqual(completed.stdout, b"")
            self.assertEqual(
                json.loads(completed.stderr.decode("utf-8"))["error"],
                "content_draft_invalid",
            )
            self.assertNotIn(str(secret_path), completed.stderr.decode("utf-8"))
            self.assertNotIn("DO-NOT-ECHO", completed.stderr.decode("utf-8"))

    def test_malformed_utf8_and_json_exit_two_without_creating_artifacts(self) -> None:
        for payload in (b"\xff\xfe", b'{"schemaVersion":'):
            with self.subTest(payload=payload), self._case_root() as directory:
                root = Path(directory)
                completed = self._assert_read_only_run(root, ["--stdin"], stdin_bytes=payload)
                self.assertEqual(completed.returncode, 2)
                self.assertEqual(completed.stdout, b"")
                self.assertEqual(
                    json.loads(completed.stderr.decode("utf-8"))["writesPerformed"],
                    False,
                )


if __name__ == "__main__":
    unittest.main()
