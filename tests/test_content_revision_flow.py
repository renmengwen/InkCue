from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import agent_task_contract as atc  # noqa: E402
import prepare_draft_agent_task as prepare  # noqa: E402
from content_source import content_draft_identity, validate_content_draft  # noqa: E402
from project_workspace import ExecutionAgentConcurrency, WorkspaceConfig  # noqa: E402


EXAMPLE = ROOT / "examples" / "topic-habit-loop-content-draft.json"
REVIEW_SCRIPT = SCRIPTS / "render_content_review.py"


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _example_draft() -> dict[str, object]:
    return json.loads(EXAMPLE.read_text(encoding="utf-8"))


class ContentRevisionPrepareTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="whiteboard-revision-prepare-")
        self.root = Path(self.temp.name)
        self.workspace_root = self.root / "workspace"
        self.workspace_root.mkdir()
        self.config_path = self.root / "workspace.local.json"
        self.config_path.write_text("{}", encoding="utf-8")
        self.active_provider_patch = mock.patch.object(
            prepare, "active_provider_id", return_value="edge-tts"
        )
        self.active_provider_patch.start()
        self.workspace = WorkspaceConfig(
            root=self.workspace_root,
            config_path=self.config_path,
            agents=ExecutionAgentConcurrency(
                default=2,
            ),
        )

    def tearDown(self) -> None:
        self.active_provider_patch.stop()
        self.temp.cleanup()

    def _args(self, draft_root: Path, **overrides: object) -> argparse.Namespace:
        values: dict[str, object] = {
            "role": "contentDrafting",
            "draft_root": str(draft_root),
            "workspace_config": None,
            "run_id": "run-content",
            "task_id": "content-draft",
            "attempt": 2,
            "content_input": None,
            "revision_request": None,
            "base_content_draft": None,
            "source_srt": None,
            "target_sec": 30.0,
            "min_sec": 25.0,
            "max_sec": 35.0,
        }
        values.update(overrides)
        return argparse.Namespace(**values)

    def _base_attempt(self, draft_name: str) -> tuple[Path, Path, Path, dict[str, object], str]:
        draft_root = self.workspace_root / "drafts" / draft_name
        old_attempt = (
            draft_root
            / ".work"
            / "run-content"
            / "agent-tasks"
            / "content-draft"
            / "attempt-0001"
        )
        candidate = old_attempt / "candidate.content-draft.json"
        result = old_attempt / "result.json"
        draft = validate_content_draft(_example_draft())
        _write_json(candidate, draft)
        _write_json(result, {"historical": True})
        return draft_root, candidate, result, draft, content_draft_identity(draft)

    @staticmethod
    def _revision(identity: str) -> dict[str, object]:
        return {
            "schemaVersion": 1,
            "contractVersion": "whiteboard-content-revision-request-v1",
            "baseContentDraftIdentitySha256": identity,
            "globalInstructions": ["整体语气更克制"],
            "cueChanges": [
                {"cueId": "cue-003", "instruction": "保留结论，缩短为两句话"}
            ],
            "sceneChanges": [
                {"sceneId": "scene-02", "instruction": "改为单一人物构图"}
            ],
            "mustPreserve": ["所有数字和责任主体"],
        }

    def test_revision_schema_rejects_unknown_empty_and_wrong_base_identity(self) -> None:
        cases: list[tuple[str, object]] = []
        for name in ("unknown", "empty", "wrong-base"):
            draft_root, candidate, _result, _draft, identity = self._base_attempt(name)
            revision = self._revision(identity)
            if name == "unknown":
                revision["unexpected"] = True
            elif name == "empty":
                revision["globalInstructions"] = []
                revision["cueChanges"] = []
                revision["sceneChanges"] = []
            else:
                revision["baseContentDraftIdentitySha256"] = "f" * 64
            request = self.root / f"{name}.revision.json"
            _write_json(request, revision)
            cases.append(
                (
                    name,
                    self._args(
                        draft_root,
                        revision_request=str(request),
                        base_content_draft=str(candidate),
                    ),
                )
            )

        for name, args in cases:
            with self.subTest(case=name), mock.patch.object(
                prepare,
                "load_workspace_config",
                return_value=self.workspace,
            ):
                with self.assertRaises(prepare.PrepareError):
                    prepare.prepare_draft_task(args)

    def test_revision_creates_new_attempt_preserves_history_and_keeps_review_out_of_child_outputs(self) -> None:
        draft_root, candidate, old_result, _draft, identity = self._base_attempt("valid")
        old_candidate_bytes = candidate.read_bytes()
        old_result_bytes = old_result.read_bytes()
        request = self.root / "valid.revision.json"
        _write_json(request, self._revision(identity))
        args = self._args(
            draft_root,
            revision_request=str(request),
            base_content_draft=str(candidate),
        )

        with mock.patch.object(
            prepare,
            "load_workspace_config",
            return_value=self.workspace,
        ):
            prepared = prepare.prepare_draft_task(args)

        context = atc.TrustedTaskContext(
            self.workspace_root,
            draft_root,
            "draft",
            "run-content",
            "content-draft",
            2,
        )
        task = atc.validate_agent_task(context.task_json, context)
        self.assertEqual(
            [path.name for path in task.input_files],
            ["base.content-draft.json", "revision-request.json", "role-contract.md"],
        )
        self.assertEqual(
            task.data["currentBindings"]["baseContentDraftIdentitySha256"],
            identity,
        )
        self.assertRegex(
            task.data["currentBindings"]["revisionRequestSha256"],
            r"^[0-9a-f]{64}$",
        )
        self.assertEqual(
            {path.name for path in task.allowed_output_files},
            {"candidate.content-draft.json", "result.json"},
        )
        self.assertFalse(any("reviews" in path.parts for path in task.allowed_output_files))
        self.assertFalse(task.data["formalWritesAllowed"])
        self.assertFalse(task.data["approvalWritesAllowed"])
        self.assertEqual(candidate.read_bytes(), old_candidate_bytes)
        self.assertEqual(old_result.read_bytes(), old_result_bytes)
        self.assertNotEqual(context.task_dir, candidate.parent)
        self.assertFalse((context.task_dir / "candidate.content-draft.json").exists())
        self.assertFalse((context.task_dir / "result.json").exists())
        self.assertFalse((self.workspace_root / "projects").exists())
        for name in ("input.json", "source.srt", "generation-plan.json", "manifest.json"):
            self.assertFalse((draft_root / name).exists())
        self.assertFalse(prepared["formalPublished"])
        self.assertFalse(prepared["approvalWritten"])
        self.assertEqual(prepared["contractVersion"], "whiteboard-draft-agent-prepare-v2")
        self.assertEqual(prepared["attempt"], 2)
        self.assertTrue(prepared["preparedTask"]["preparedOnly"])
        self.assertEqual(
            Path(prepared["preparedTask"]["allowedAttemptDir"]),
            context.task_dir.resolve(),
        )
        serialized = json.dumps(prepared, ensure_ascii=False)
        for forbidden in ("spawnAgentCall", "spawnPackage", "dispatchAllowed"):
            self.assertNotIn(forbidden, serialized)

        mutated = dict(task.data)
        mutated["allowedOutputs"] = [
            context.relative_posix(
                draft_root / "reviews" / f"content-review-{identity[:12]}.md"
            ),
            context.relative_posix(context.result_json),
        ]
        _write_json(context.task_json, mutated)
        with self.assertRaises(atc.AgentContractError):
            atc.validate_agent_task(context.task_json, context)

    def test_initial_content_drafting_remains_compatible(self) -> None:
        source = self.root / "content-input.json"
        _write_json(
            source,
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
        )
        draft_root = self.workspace_root / "drafts" / "initial-compatible"
        args = self._args(
            draft_root,
            attempt=1,
            content_input=str(source),
        )
        with mock.patch.object(
            prepare,
            "load_workspace_config",
            return_value=self.workspace,
        ):
            prepared = prepare.prepare_draft_task(args)

        context = atc.TrustedTaskContext(
            self.workspace_root,
            draft_root,
            "draft",
            "run-content",
            "content-draft",
            1,
        )
        task = atc.validate_agent_task(context.task_json, context)
        self.assertEqual(
            [path.name for path in task.input_files],
            ["content-input.json", "role-contract.md"],
        )
        self.assertEqual(prepared["contractVersion"], "whiteboard-draft-agent-prepare-v2")


class ContentReviewArtifactTests(unittest.TestCase):
    def setUp(self) -> None:
        self.assertTrue(REVIEW_SCRIPT.is_file(), "缺少 content review renderer")
        self.temp = tempfile.TemporaryDirectory(
            prefix="whiteboard-content-review-",
            dir=Path("D:/"),
        )
        self.root = Path(self.temp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.draft_root = self.workspace / "drafts" / "review-flow"
        self.draft_root.mkdir(parents=True)
        self.config = self.root / "workspace.local.json"
        _write_json(
            self.config,
            {
                "schemaVersion": 1,
                "workspaceRoot": str(self.workspace),
            },
        )
        self.runtime = self.root / "runtime"
        self.runtime.mkdir()

    def tearDown(self) -> None:
        if hasattr(self, "temp"):
            self.temp.cleanup()

    def _candidate_path(self, attempt: int, draft: dict[str, object]) -> Path:
        path = (
            self.draft_root
            / ".work"
            / "run-content"
            / "agent-tasks"
            / "content-draft"
            / f"attempt-{attempt:04d}"
            / "candidate.content-draft.json"
        )
        _write_json(path, draft)
        return path

    def _run(self, candidate: Path) -> subprocess.CompletedProcess[str]:
        environment = {
            **os.environ,
            "TEMP": str(self.runtime),
            "TMP": str(self.runtime),
            "PYTHONPYCACHEPREFIX": str(self.runtime / "pycache"),
            "PYTHONIOENCODING": "utf-8",
        }
        return subprocess.run(
            [
                sys.executable,
                "-B",
                str(REVIEW_SCRIPT),
                "--draft-root",
                str(self.draft_root),
                "--candidate",
                str(candidate),
                "--workspace-config",
                str(self.config),
            ],
            cwd=self.root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="strict",
            shell=False,
            check=False,
            env=environment,
        )

    def _assert_summary_is_redacted(
        self,
        completed: subprocess.CompletedProcess[str],
        draft: dict[str, object],
    ) -> None:
        combined = completed.stdout + completed.stderr
        source = draft["topic"] if draft["inputMode"] == "topic" else draft["body"]
        self.assertNotIn(str(source), combined)
        self.assertNotIn(str(draft["narrationCues"][0]["text"]), combined)  # type: ignore[index]
        self.assertNotIn(str(draft["scenes"][0]["imagePrompt"]), combined)  # type: ignore[index]
        self.assertNotIn(str(self.draft_root), combined)
        self.assertNotRegex(combined, r"(?i)[a-z]:[\\/]")

    def test_review_is_deterministic_identity_bound_complete_and_does_not_create_project(self) -> None:
        draft = validate_content_draft(_example_draft())
        candidate = self._candidate_path(1, draft)
        first = self._run(candidate)
        self.assertEqual(first.returncode, 0, first.stderr)
        first_result = json.loads(first.stdout)
        self.assertEqual(
            first_result["contractVersion"],
            "whiteboard-content-review-artifact-v1",
        )
        self.assertTrue(first_result["valid"])
        self.assertTrue(first_result["userConfirmationRequired"])
        self.assertFalse(first_result["approvalWritten"])
        self.assertFalse(first_result["formalPublished"])
        self._assert_summary_is_redacted(first, draft)

        identity = content_draft_identity(draft)
        self.assertEqual(first_result["contentDraftIdentitySha256"], identity)
        self.assertEqual(
            first_result["reviewFile"],
            f"reviews/content-review-{identity[:12]}.md",
        )
        review = self.draft_root / first_result["reviewFile"]
        self.assertTrue(review.is_file())
        first_bytes = review.read_bytes()
        self.assertEqual(first_result["reviewSha256"], _sha256(review))

        markdown = first_bytes.decode("utf-8")
        for required in (
            identity,
            str(draft["inputMode"]),
            str(draft["rewritePolicy"]),
            str(draft["targetDurationSeconds"]),
            str(draft["voiceoverMode"]),
            str(draft["topic"]),
            "实质改动",
            "provisional",
            "Edge",
            "内容与制作方案联合确认",
        ):
            with self.subTest(required=required):
                self.assertIn(required, markdown)
        for cue in draft["narrationCues"]:  # type: ignore[assignment]
            self.assertIn(cue["cueId"], markdown)
            self.assertIn(cue["sceneId"], markdown)
            self.assertIn(cue["text"], markdown)
        for scene in draft["scenes"]:  # type: ignore[assignment]
            for field in ("sceneId", "name", "coreIdea", "visualSubject", "imagePrompt"):
                self.assertIn(scene[field], markdown)
        self.assertNotRegex(markdown, r"(?i)[a-z]:[\\/]")

        second = self._run(candidate)
        self.assertEqual(second.returncode, 0, second.stderr)
        second_result = json.loads(second.stdout)
        self.assertEqual(second_result["reviewFile"], first_result["reviewFile"])
        self.assertEqual(second_result["reviewSha256"], first_result["reviewSha256"])
        self.assertEqual(review.read_bytes(), first_bytes)
        self.assertFalse((self.workspace / "projects").exists())
        for name in ("input.json", "source.srt", "generation-plan.json", "manifest.json"):
            self.assertFalse((self.draft_root / name).exists())

    def test_revision_gets_new_identity_and_review_while_old_review_remains(self) -> None:
        old_draft = validate_content_draft(_example_draft())
        old_candidate = self._candidate_path(1, old_draft)
        old_run = self._run(old_candidate)
        self.assertEqual(old_run.returncode, 0, old_run.stderr)
        old_result = json.loads(old_run.stdout)
        old_review = self.draft_root / old_result["reviewFile"]
        old_bytes = old_review.read_bytes()

        new_draft = copy.deepcopy(old_draft)
        new_draft["narrationCues"][2]["text"] = "重复多次后，这条路径会更省力，动作也更容易自然开始。"  # type: ignore[index]
        new_draft = validate_content_draft(new_draft)
        new_candidate = self._candidate_path(2, new_draft)
        new_run = self._run(new_candidate)
        self.assertEqual(new_run.returncode, 0, new_run.stderr)
        new_result = json.loads(new_run.stdout)
        self._assert_summary_is_redacted(new_run, new_draft)

        self.assertNotEqual(
            new_result["contentDraftIdentitySha256"],
            old_result["contentDraftIdentitySha256"],
        )
        self.assertNotEqual(new_result["reviewFile"], old_result["reviewFile"])
        self.assertEqual(old_review.read_bytes(), old_bytes)
        self.assertTrue((self.draft_root / new_result["reviewFile"]).is_file())
        self.assertFalse((self.workspace / "projects").exists())

    def test_invalid_candidate_error_does_not_echo_content_or_absolute_path(self) -> None:
        draft = _example_draft()
        draft["apiKey"] = "DO-NOT-ECHO-SECRET"
        draft["topic"] = "DO-NOT-ECHO-BODY"
        draft["scenes"][0]["imagePrompt"] = r"DO-NOT-ECHO C:\private\token.txt"  # type: ignore[index]
        candidate = self._candidate_path(1, draft)
        completed = self._run(candidate)
        self.assertEqual(completed.returncode, 2)
        combined = completed.stdout + completed.stderr
        self.assertNotIn("DO-NOT-ECHO", combined)
        self.assertNotIn(str(candidate), combined)
        self.assertNotRegex(combined, r"(?i)[a-z]:[\\/]")


if __name__ == "__main__":
    unittest.main()
