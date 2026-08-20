from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import agent_task_contract as atc  # noqa: E402


def write_bytes(path: Path, payload: bytes) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return atc.sha256_file(path)


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


class AgentTaskContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.workspace = self.root / "workspace"
        self.scope = self.workspace / "projects" / "project-one"
        self.scope.mkdir(parents=True)
        self.context = atc.TrustedTaskContext(
            workspace_root=self.workspace,
            scope_root=self.scope,
            scope_kind="project",
            run_id="run-001",
            task_id="annotation-scene-03",
            attempt=1,
        )
        self.context.task_dir.mkdir(parents=True)
        self.role_sha = write_bytes(
            self.context.task_dir / "role-contract.md", b"frozen role contract\n"
        )
        self.image_sha = write_bytes(
            self.scope / "scenes" / "scene-03.png", b"png fixture bytes"
        )
        self.timing_sha = write_bytes(
            self.scope / "planning" / "timing-plan.json", b"{\"timing\":true}\n"
        )
        self.bindings = {
            "generationPlanSha256": "1" * 64,
            "renderProfileSha256": "2" * 64,
            "activeTimelineSha256": None,
        }
        self.task_data = self.make_task_data()
        write_json(self.context.task_json, self.task_data)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def relative(self, path: Path) -> str:
        return self.context.relative_posix(path)

    def make_task_data(self) -> dict[str, object]:
        return {
            "contractVersion": atc.TASK_CONTRACT_VERSION,
            "taskId": self.context.task_id,
            "taskKind": "annotationDrafting",
            "scopeKind": "project",
            "roleContractVersion": atc.ROLE_CONTRACT_VERSION,
            "roleContractSha256": self.role_sha,
            "attempt": 1,
            "sequence": 3,
            "sceneId": "scene-03",
            "inputs": [
                {"file": "scenes/scene-03.png", "sha256": self.image_sha},
                {
                    "file": "planning/timing-plan.json",
                    "sha256": self.timing_sha,
                },
                {
                    "file": self.relative(
                        self.context.task_dir / "role-contract.md"
                    ),
                    "sha256": self.role_sha,
                },
            ],
            "currentBindings": dict(self.bindings),
            "requiredCapabilities": [
                "readFiles",
                "viewImage",
                "writeCandidateJson",
            ],
            "allowedOutputs": [
                self.relative(
                    self.context.task_dir / "candidate.annotation.json"
                ),
            ],
            "formalWritesAllowed": False,
            "approvalWritesAllowed": False,
        }

    def validate_task(self) -> atc.ValidatedAgentTask:
        return atc.validate_agent_task(
            self.context.task_json,
            self.context,
            expected_current_bindings=self.bindings,
        )

    def make_completed_result(
        self, task: atc.ValidatedAgentTask
    ) -> dict[str, object]:
        candidate = self.context.task_dir / "candidate.annotation.json"
        candidate_sha = write_bytes(candidate, b"{\"annotation\":true}\n")
        return {
            "contractVersion": atc.RESULT_CONTRACT_VERSION,
            "taskId": self.context.task_id,
            "taskKind": "annotationDrafting",
            "scopeKind": "project",
            "attempt": 1,
            "taskSha256": task.task_sha256,
            "roleContractVersion": atc.ROLE_CONTRACT_VERSION,
            "roleContractSha256": self.role_sha,
            "sequence": 3,
            "status": "completed",
            "inspectedInputs": list(self.task_data["inputs"]),
            "outputs": [
                {
                    "file": self.relative(candidate),
                    "sha256": candidate_sha,
                }
            ],
            "findings": [],
            "warnings": [],
            "error": None,
        }

    def test_valid_task_and_result_bind_frozen_bytes_and_business_validator(self) -> None:
        task = self.validate_task()
        result_data = self.make_completed_result(task)
        write_json(self.context.result_json, result_data)
        validated_outputs: list[tuple[str, str]] = []

        result = atc.validate_agent_result(
            self.context.result_json,
            task,
            dispatched_task_sha256=task.task_sha256,
            expected_current_bindings=self.bindings,
            output_validator=lambda role, path: validated_outputs.append(
                (role, path.name)
            ),
        )

        self.assertEqual(result.data["status"], "completed")
        self.assertEqual(
            validated_outputs,
            [("annotationDrafting", "candidate.annotation.json")],
        )
        self.assertEqual(result.output_files[0].parent, self.context.task_dir)

    def test_task_and_nested_records_reject_unknown_or_missing_fields(self) -> None:
        for mutate in (
            lambda value: value.update({"unknown": True}),
            lambda value: value["inputs"][0].update({"size": 1}),  # type: ignore[index,union-attr]
            lambda value: value.pop("formalWritesAllowed"),
        ):
            with self.subTest(mutate=mutate):
                value = self.make_task_data()
                mutate(value)
                write_json(self.context.task_json, value)
                with self.assertRaises(atc.AgentContractError):
                    self.validate_task()

    def test_permissions_must_be_explicit_false(self) -> None:
        for field, bad in (
            ("formalWritesAllowed", True),
            ("approvalWritesAllowed", True),
            ("formalWritesAllowed", 0),
        ):
            with self.subTest(field=field, bad=bad):
                value = self.make_task_data()
                value[field] = bad
                write_json(self.context.task_json, value)
                with self.assertRaisesRegex(atc.AgentContractError, "必须显式为 false"):
                    self.validate_task()

    def test_role_scope_attempt_and_task_location_are_cross_checked(self) -> None:
        value = self.make_task_data()
        value["taskKind"] = "storyboardPlanning"
        write_json(self.context.task_json, value)
        with self.assertRaises(atc.AgentContractError) as caught:
            self.validate_task()
        self.assertEqual(caught.exception.code, "role_scope")

        value = self.make_task_data()
        value["attempt"] = 2
        write_json(self.context.task_json, value)
        with self.assertRaises(atc.AgentContractError) as caught:
            self.validate_task()
        self.assertEqual(caught.exception.code, "attempt")

        write_json(self.context.task_json, self.make_task_data())
        with self.assertRaises(atc.AgentContractError) as caught:
            atc.validate_agent_task(self.scope / "task.json", self.context)
        self.assertEqual(caught.exception.code, "task_location")

    def test_draft_scope_and_storyboard_role_match(self) -> None:
        scope = self.workspace / "drafts" / "draft-one"
        context = atc.TrustedTaskContext(
            self.workspace, scope, "draft", "run-002", "storyboard", 1
        )
        context.task_dir.mkdir(parents=True)
        role_sha = write_bytes(context.task_dir / "role-contract.md", b"role")
        source_sha = write_bytes(scope / "source.srt", b"1\n00:00:00,000 --> 00:00:01,000\nx\n")
        parsed_sha = write_bytes(scope / "parsed-srt.json", b"{}")
        data = {
            "contractVersion": atc.TASK_CONTRACT_VERSION,
            "taskId": "storyboard",
            "taskKind": "storyboardPlanning",
            "scopeKind": "draft",
            "roleContractVersion": atc.ROLE_CONTRACT_VERSION,
            "roleContractSha256": role_sha,
            "attempt": 1,
            "sequence": 1,
            "inputs": [
                {"file": "source.srt", "sha256": source_sha},
                {"file": "parsed-srt.json", "sha256": parsed_sha},
                {
                    "file": context.relative_posix(context.task_dir / "role-contract.md"),
                    "sha256": role_sha,
                },
            ],
            "currentBindings": {
                "sourceSrtSha256": source_sha,
                "parsedSrtSha256": parsed_sha,
            },
            "requiredCapabilities": ["readFiles", "writeCandidateJson"],
            "allowedOutputs": [
                context.relative_posix(
                    context.task_dir / "candidate.generation-plan.json"
                ),
                context.relative_posix(context.result_json),
            ],
            "formalWritesAllowed": False,
            "approvalWritesAllowed": False,
        }
        write_json(context.task_json, data)
        task = atc.validate_agent_task(context.task_json, context)
        self.assertEqual(task.data["scopeKind"], "draft")

    def test_phase0_content_drafting_uses_frozen_input_and_attempt_candidate(self) -> None:
        scope = self.workspace / "drafts" / "content-one"
        context = atc.TrustedTaskContext(
            self.workspace, scope, "draft", "run-content", "content-draft", 1
        )
        context.task_dir.mkdir(parents=True)
        role_sha = write_bytes(context.task_dir / "role-contract.md", b"phase0 role")
        content_sha = write_bytes(
            scope / "content-input.json",
            b'{"inputMode":"topic","topic":"fixture","targetDurationSeconds":60}\n',
        )
        data = {
            "contractVersion": atc.TASK_CONTRACT_VERSION,
            "taskId": context.task_id,
            "taskKind": "contentDrafting",
            "scopeKind": "draft",
            "roleContractVersion": atc.ROLE_CONTRACT_VERSION,
            "roleContractSha256": role_sha,
            "attempt": 1,
            "sequence": 1,
            "inputs": [
                {"file": "content-input.json", "sha256": content_sha},
                {
                    "file": context.relative_posix(context.task_dir / "role-contract.md"),
                    "sha256": role_sha,
                },
            ],
            "currentBindings": {"contentInputSha256": content_sha},
            "requiredCapabilities": ["readFiles", "writeCandidateJson"],
            "allowedOutputs": [
                context.relative_posix(
                    context.task_dir / "candidate.content-draft.json"
                ),
                context.relative_posix(context.result_json),
            ],
            "formalWritesAllowed": False,
            "approvalWritesAllowed": False,
        }
        write_json(context.task_json, data)
        task = atc.validate_agent_task(
            context.task_json,
            context,
            expected_current_bindings={"contentInputSha256": content_sha},
        )
        self.assertEqual(task.data["taskKind"], "contentDrafting")
        self.assertEqual(
            {path.name for path in task.allowed_output_files},
            {"candidate.content-draft.json", "result.json"},
        )

    def test_paths_reject_absolute_parent_windows_and_cross_attempt(self) -> None:
        bad_paths = [
            "/absolute/candidate.annotation.json",
            "../candidate.annotation.json",
            r"C:\temp\candidate.annotation.json",
            ".work/other-run/agent-tasks/other/attempt-0001/result.json",
        ]
        for bad_path in bad_paths:
            with self.subTest(path=bad_path):
                value = self.make_task_data()
                value["allowedOutputs"] = [
                    bad_path,
                ]
                write_json(self.context.task_json, value)
                with self.assertRaises(atc.AgentContractError):
                    self.validate_task()

    def test_input_cannot_cross_run_task_or_scope(self) -> None:
        other = (
            self.scope
            / ".work"
            / "other-run"
            / "agent-tasks"
            / "other-task"
            / "attempt-0001"
            / "brief.json"
        )
        other_sha = write_bytes(other, b"{}")
        value = self.make_task_data()
        value["inputs"].append(  # type: ignore[union-attr]
            {"file": self.relative(other), "sha256": other_sha}
        )
        write_json(self.context.task_json, value)
        with self.assertRaises(atc.AgentContractError) as caught:
            self.validate_task()
        self.assertEqual(caught.exception.code, "cross_attempt")

    def test_symlink_component_is_rejected(self) -> None:
        linked = self.scope / "scenes" / "linked.png"
        linked_sha = write_bytes(linked, b"linked")
        value = self.make_task_data()
        value["inputs"][0] = {  # type: ignore[index]
            "file": "scenes/linked.png",
            "sha256": linked_sha,
        }
        write_json(self.context.task_json, value)
        path_class = type(linked)
        original = path_class.is_symlink

        def fake_is_symlink(path: Path) -> bool:
            return path.name == "linked.png" or original(path)

        with mock.patch.object(path_class, "is_symlink", fake_is_symlink):
            with self.assertRaises(atc.AgentContractError) as caught:
                self.validate_task()
        self.assertEqual(caught.exception.code, "symlink")

    def test_unknown_capability_and_missing_visual_capability_are_rejected(self) -> None:
        for capabilities in (
            ["readFiles", "viewImage", "writeCandidateJson", "runProvider"],
            ["readFiles", "writeCandidateJson"],
        ):
            value = self.make_task_data()
            value["requiredCapabilities"] = capabilities
            write_json(self.context.task_json, value)
            with self.assertRaises(atc.AgentContractError) as caught:
                self.validate_task()
            self.assertEqual(caught.exception.code, "capability")

    def test_task_input_and_role_contract_changes_become_stale(self) -> None:
        task = self.validate_task()
        result_data = self.make_completed_result(task)
        write_json(self.context.result_json, result_data)
        write_bytes(self.scope / "scenes" / "scene-03.png", b"changed")
        with self.assertRaises(atc.StaleAgentTaskError):
            atc.validate_agent_result(
                self.context.result_json,
                task,
                dispatched_task_sha256=task.task_sha256,
            )

        write_bytes(self.scope / "scenes" / "scene-03.png", b"png fixture bytes")
        write_bytes(self.context.task_dir / "role-contract.md", b"changed role")
        with self.assertRaises(atc.StaleAgentTaskError):
            atc.validate_agent_result(
                self.context.result_json,
                task,
                dispatched_task_sha256=task.task_sha256,
            )

    def test_task_immutability_and_result_task_sha_are_required(self) -> None:
        task = self.validate_task()
        result_data = self.make_completed_result(task)
        write_json(self.context.result_json, result_data)
        mutated = self.make_task_data()
        mutated["sequence"] = 4
        write_json(self.context.task_json, mutated)
        with self.assertRaises(atc.StaleAgentTaskError):
            atc.validate_agent_result(
                self.context.result_json,
                task,
                dispatched_task_sha256=task.task_sha256,
            )

        write_json(self.context.task_json, self.make_task_data())
        task = self.validate_task()
        result_data = self.make_completed_result(task)
        result_data["taskSha256"] = "9" * 64
        write_json(self.context.result_json, result_data)
        with self.assertRaises(atc.AgentContractError) as caught:
            atc.validate_agent_result(
                self.context.result_json,
                task,
                dispatched_task_sha256=task.task_sha256,
            )
        self.assertEqual(caught.exception.code, "result_binding")

    def test_result_rejects_output_sha_unknown_fields_and_missing_candidate(self) -> None:
        task = self.validate_task()
        cases: list[dict[str, object]] = []
        wrong_sha = self.make_completed_result(task)
        wrong_sha["outputs"][0]["sha256"] = "8" * 64  # type: ignore[index]
        cases.append(wrong_sha)
        unknown = self.make_completed_result(task)
        unknown["unknown"] = True
        cases.append(unknown)
        missing = self.make_completed_result(task)
        missing["outputs"] = []
        cases.append(missing)
        for value in cases:
            with self.subTest(value=value):
                write_json(self.context.result_json, value)
                with self.assertRaises(atc.AgentContractError):
                    atc.validate_agent_result(
                        self.context.result_json,
                        task,
                        dispatched_task_sha256=task.task_sha256,
                    )

    def test_result_requires_all_inspected_inputs_and_current_bindings(self) -> None:
        task = self.validate_task()
        result_data = self.make_completed_result(task)
        result_data["inspectedInputs"] = result_data["inspectedInputs"][:-1]  # type: ignore[index]
        write_json(self.context.result_json, result_data)
        with self.assertRaises(atc.AgentContractError) as caught:
            atc.validate_agent_result(
                self.context.result_json,
                task,
                dispatched_task_sha256=task.task_sha256,
            )
        self.assertEqual(caught.exception.code, "inspected_inputs")

        result_data = self.make_completed_result(task)
        write_json(self.context.result_json, result_data)
        changed_bindings = dict(self.bindings)
        changed_bindings["generationPlanSha256"] = "3" * 64
        with self.assertRaises(atc.StaleAgentTaskError):
            atc.validate_agent_result(
                self.context.result_json,
                task,
                dispatched_task_sha256=task.task_sha256,
                expected_current_bindings=changed_bindings,
            )

    def test_effective_concurrency_converts_child_slots_only_once(self) -> None:
        task = self.validate_task()
        decision = atc.decide_agent_dispatch(
            task,
            configured=3,
            ready_tasks=8,
            runtime_child_slots=2,
            resource_budget=4,
            runtime_role_capabilities=self.task_data["requiredCapabilities"],
            coordinator_capabilities=self.task_data["requiredCapabilities"],
        )
        self.assertTrue(decision.dispatch_allowed)
        self.assertEqual(decision.effective_agent_concurrency, 2)
        self.assertEqual(decision.mode, "dispatch")

    def test_missing_runtime_capability_falls_back_and_missing_view_is_blocked(self) -> None:
        task = self.validate_task()
        fallback = atc.decide_agent_dispatch(
            task,
            configured=3,
            ready_tasks=8,
            runtime_child_slots=2,
            resource_budget=2,
            runtime_role_capabilities=[],
            coordinator_capabilities=self.task_data["requiredCapabilities"],
        )
        self.assertFalse(fallback.dispatch_allowed)
        self.assertEqual(fallback.effective_agent_concurrency, 0)
        self.assertEqual(fallback.mode, "fallback")

        blocked = atc.decide_agent_dispatch(
            task,
            configured=1,
            ready_tasks=1,
            runtime_child_slots=0,
            resource_budget=1,
            runtime_role_capabilities=[],
            coordinator_capabilities=["readFiles", "writeCandidateJson"],
        )
        self.assertEqual(blocked.mode, "blocked")
        self.assertIn("viewImage", blocked.reason)

    def test_host_collaboration_dispatch_is_audited(self) -> None:
        task = self.validate_task()
        decision = atc.decide_agent_dispatch(
            task,
            configured=3,
            ready_tasks=8,
            runtime_child_slots=2,
            resource_budget=4,
            runtime_role_capabilities=self.task_data["requiredCapabilities"],
            coordinator_capabilities=self.task_data["requiredCapabilities"],
        )
        self.assertTrue(decision.dispatch_allowed)
        self.assertEqual(decision.effective_agent_concurrency, 2)
        audit = atc.build_agent_batch_audit(
            stage="annotationDrafting",
            configured=3,
            task_count=8,
            decision=decision,
            peak_child_agents=2,
            task_agents=[
                {
                    "taskId": "ann-scene-01",
                    "agentId": "/root/integration_forward_test/gate34_phase0_a",
                    "status": "completed",
                },
                {
                    "taskId": "ann-scene-02",
                    "agentId": "agent/child-02",
                    "status": "running",
                },
            ],
        )
        self.assertEqual(audit["mode"], "host_collaboration_dispatch")
        self.assertEqual(audit["adapter"], "codex_collaboration")
        self.assertEqual(audit["peakChildAgents"], 2)
        self.assertEqual(len(audit["taskAgents"]), 2)
        self.assertEqual(
            audit["taskAgents"][0]["agentId"],
            "/root/integration_forward_test/gate34_phase0_a",
        )

    def test_runtime_agent_id_allows_only_bounded_collaboration_namespaces(self) -> None:
        for accepted in (
            "/root",
            "/root/integration_forward_test/gate34_phase0_a",
            "agent/child-01",
            "019f-agent-id",
        ):
            with self.subTest(accepted=accepted):
                self.assertEqual(atc._require_runtime_id(accepted, "agentId"), accepted)

        rejected = (
            "/tmp/child-agent",
            "C:/temp/child-agent",
            r"C:\temp\child-agent",
            "/root/../child-agent",
            "/root//child-agent",
            "/root/secret/child-agent",
            "/root/token",
            "agent\nchild",
            "a" * 257,
            "",
        )
        for value in rejected:
            with self.subTest(rejected=value):
                with self.assertRaises(atc.AgentContractError):
                    atc._require_runtime_id(value, "agentId")

    def test_zero_runtime_capacity_audit_uses_fallback(self) -> None:
        task = self.validate_task()
        decision = atc.decide_agent_dispatch(
            task,
            configured=3,
            ready_tasks=8,
            runtime_child_slots=0,
            resource_budget=4,
            runtime_role_capabilities=self.task_data["requiredCapabilities"],
            coordinator_capabilities=self.task_data["requiredCapabilities"],
        )
        audit = atc.build_agent_batch_audit(
            stage="annotationDrafting",
            configured=3,
            task_count=8,
            decision=decision,
        )
        self.assertFalse(audit["dispatchAllowed"])
        self.assertEqual(audit["effectiveAgentConcurrency"], 0)
        self.assertEqual(audit["peakChildAgents"], 0)

    def test_prompt_contains_only_frozen_locator_contract(self) -> None:
        task = self.validate_task()
        prompt = atc.build_agent_prompt(
            task_json=self.context.task_json,
            role_contract=self.context.task_dir / "role-contract.md",
            task_kind="annotationDrafting",
            task_sha256=task.task_sha256,
            role_contract_sha256=self.role_sha,
        )
        self.assertIn(str(self.context.task_json), prompt)
        self.assertIn(str(self.context.task_dir / "role-contract.md"), prompt)
        self.assertIn(f"ALLOWED_ATTEMPT_DIR={self.context.task_dir}", prompt)
        self.assertIn("TASK_STATUS=<candidate_ready|failed|cancelled>", prompt)
        self.assertIn("CANDIDATE_JSON=", prompt)
        self.assertIn("VALIDATOR_STATUS=<PASS|FAIL|NOT_RUN>", prompt)
        self.assertIn("SUMMARY=<不超过240个字符的精简摘要>", prompt)
        self.assertNotIn("annotationDrafting", prompt)
        self.assertNotIn("png fixture bytes", prompt)
        self.assertNotIn("provider", prompt.lower())
        self.assertNotIn("SECRET", prompt)

    def test_non_annotation_prompts_expose_only_frozen_locator_fields(self) -> None:
        task_json = (self.context.task_dir / "task.json").resolve()
        role_contract = (self.context.task_dir / "role-contract.md").resolve()
        for task_kind in ("contentDrafting", "storyboardPlanning", "visualReview"):
            with self.subTest(task_kind=task_kind):
                prompt = atc.build_agent_prompt(
                    task_json=task_json,
                    role_contract=role_contract,
                    task_kind=task_kind,
                    task_sha256="1" * 64,
                    role_contract_sha256="2" * 64,
                )
                keys = {
                    line.split("=", 1)[0]
                    for line in prompt.splitlines()
                    if "=" in line and not line.startswith("TASK_STATUS=<")
                }
                self.assertEqual(
                    keys,
                    {
                        "ROLE_CONTRACT_PATH",
                        "ROLE_CONTRACT_SHA256",
                        "TASK_JSON_PATH",
                        "TASK_SHA256",
                        "ALLOWED_ATTEMPT_DIR",
                        "RESULT_JSON",
                        "VALIDATOR_STATUS",
                        "SUMMARY",
                    },
                )
                for forbidden in (
                    "主对话",
                    "完整 SRT",
                    "provider",
                    "apiKey",
                    "approval",
                    "SECRET",
                ):
                    self.assertNotIn(forbidden, prompt)

    def test_fake_scheduler_orders_results_and_retries_only_failed_cancelled_stale(self) -> None:
        planned = [
            atc.AgentAttemptSummary("task-a", 1, 1, "completed"),
            atc.AgentAttemptSummary("task-b", 1, 2, "failed"),
            atc.AgentAttemptSummary("task-c", 1, 3, "cancelled"),
            atc.AgentAttemptSummary("task-d", 1, 4, "stale"),
            atc.AgentAttemptSummary("task-e", 1, 5, "blocked"),
        ]
        scheduler = atc.FakeAgentScheduler(planned)
        for item in reversed(planned):
            scheduler.record(item)
        self.assertEqual(
            [item.task_id for item in scheduler.ordered_results()],
            ["task-a", "task-b", "task-c", "task-d", "task-e"],
        )
        self.assertEqual(
            [item.task_id for item in scheduler.retry_candidates()],
            ["task-b", "task-c", "task-d"],
        )
        self.assertEqual(scheduler.pending(), ())
        with self.assertRaises(atc.AgentContractError):
            scheduler.record(planned[0])


if __name__ == "__main__":
    unittest.main()
