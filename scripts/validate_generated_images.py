#!/usr/bin/env python3
"""标注前并发重验生成图片；技术 validated 不写线稿人工批准。"""
from __future__ import annotations

import argparse
import json
import re
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from PIL import Image, UnidentifiedImageError

from agent_task_contract import (
    RESULT_CONTRACT_VERSION,
    ROLE_CONTRACT_VERSION,
    TASK_CONTRACT_VERSION,
    AgentContractError,
    TrustedTaskContext,
    ValidatedAgentResult,
    ValidatedAgentTask,
    build_agent_batch_audit,
    build_agent_prompt,
    decide_agent_dispatch,
    sha256_file as agent_sha256_file,
    validate_agent_result,
    validate_agent_task,
)
from bounded_execution import CONTINUE_INDEPENDENT, WorkerFailure, WorkerOutcome, execute_bounded
from project_workspace import (
    ProjectValidationError,
    ProjectWorkspace,
    WorkspaceError,
    sha256_file,
    write_json_atomic,
)


SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
VISUAL_REVIEW_ROLE_CONTRACT = """# visualReview frozen role contract

- 只读 task.json 列出的 generation plan、generation manifest 与 current PNG。
- 必须真实查看全部图片并保持跨幕人物、配色、纸张和构图的全局视野。
- 只写 findings.json/result.json；不得修改图片、调用 provider、写 manifest 或批准。
- findings 必须按 generation plan scene 顺序；技术 validated 不能替代用户逐图确认。
"""
HOST_SPAWN_PACKAGE_VERSION = "whiteboard-host-spawn-package-v1"
HOST_VISUAL_REVIEW_CAPABILITIES = (
    "readFiles",
    "viewImage",
    "writeCandidateJson",
)


class ManifestValidationError(ValueError):
    pass


class CliArgumentError(ValueError):
    pass


class JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise CliArgumentError(message)


@dataclass(frozen=True)
class ImageValidationTask:
    scene_id: str
    file: str
    path: Path
    expected_hash: str


def _emit(value: dict[str, Any]) -> None:
    print(json.dumps(value, ensure_ascii=False, separators=(",", ":")))


def _summary(
    *,
    ok: bool,
    exit_code: int,
    project: str,
    total: int = 0,
    validated: int = 0,
    failed: int = 0,
    configured_concurrency: int = 1,
    effective_concurrency: int = 0,
    task_count: int = 0,
    consumable: list[dict[str, str]] | None = None,
    failures: list[dict[str, str]] | None = None,
    visual_review: dict[str, Any] | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    value: dict[str, Any] = {
        "ok": ok,
        "command": "validate_generated_images",
        "exitCode": exit_code,
        "project": project,
        "total": total,
        "validated": validated,
        "failed": failed,
        "configuredConcurrency": configured_concurrency,
        "effectiveConcurrency": effective_concurrency,
        "taskCount": task_count,
        "consumable": consumable or [],
        "failures": failures or [],
        "userConfirmationRequired": True,
        "approvalWritten": False,
    }
    if visual_review is not None:
        value["visualReview"] = visual_review
    if error:
        value["error"] = error
    return value


def _read_manifest(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ManifestValidationError("manifest 不存在") from exc
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ManifestValidationError("manifest 无法读取") from exc
    if not isinstance(raw, dict):
        raise ManifestValidationError("manifest 顶层必须是对象")
    return raw


def _manifest_scenes(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    scenes = manifest.get("scenes")
    if not isinstance(scenes, list):
        raise ManifestValidationError("manifest.scenes 必须是数组")
    by_id: dict[str, dict[str, Any]] = {}
    for index, scene in enumerate(scenes):
        if not isinstance(scene, dict):
            raise ManifestValidationError(f"manifest.scenes[{index}] 必须是对象")
        scene_id = scene.get("sceneId")
        if not isinstance(scene_id, str) or not scene_id or scene_id in by_id:
            raise ManifestValidationError("manifest sceneId 缺失或重复")
        by_id[scene_id] = scene
    return by_id


def _validate_manifest_contract(project: Any, manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    if manifest.get("schemaVersion") != 1:
        raise ManifestValidationError("manifest schemaVersion 必须为 1")
    if manifest.get("projectId") != project.project_id:
        raise ManifestValidationError("manifest projectId 与 project.json 不一致")
    generation_plan = manifest.get("generationPlan")
    if not isinstance(generation_plan, dict):
        raise ManifestValidationError("manifest.generationPlan 必须是对象")
    if generation_plan.get("file") != "planning/generation-plan.json":
        raise ManifestValidationError("manifest generationPlan.file 无效")
    plan_hash = generation_plan.get("sha256")
    if not isinstance(plan_hash, str) or not SHA256_RE.fullmatch(plan_hash):
        raise ManifestValidationError("manifest generationPlan.sha256 无效")
    if sha256_file(project.plan_path) != plan_hash:
        raise ManifestValidationError("generation plan SHA-256 与 manifest 不一致")
    if not isinstance(manifest.get("runs"), list) or not isinstance(manifest.get("summary"), dict):
        raise ManifestValidationError("manifest runs/summary 无效")
    by_id = _manifest_scenes(manifest)
    expected_ids = [scene["sceneId"] for scene in project.plan["scenes"]]
    if list(by_id) != expected_ids:
        raise ManifestValidationError("manifest 场景顺序与 generation plan 不一致")
    if manifest["summary"].get("sceneTotal") != len(expected_ids):
        raise ManifestValidationError("manifest summary.sceneTotal 与计划不一致")
    return by_id


def _validate_image(task: ImageValidationTask) -> WorkerOutcome[dict[str, str]]:
    if not task.path.is_file():
        return WorkerOutcome.failed(WorkerFailure("missing", "输出图片不存在"))
    try:
        # 同一打开周期只做一次完整 load；不再 verify() 后重新打开。
        with Image.open(task.path) as image:
            image.load()
            if image.format != "PNG":
                return WorkerOutcome.failed(WorkerFailure("format", f"实际格式不是 PNG: {image.format}"))
            if image.size != (1920, 1080):
                return WorkerOutcome.failed(
                    WorkerFailure("size", f"实际尺寸必须为 1920x1080，当前为 {image.width}x{image.height}")
                )
            if image.mode != "RGB":
                return WorkerOutcome.failed(WorkerFailure("mode", f"实际颜色模式必须为 RGB，当前为 {image.mode}"))
    except (OSError, UnidentifiedImageError, ValueError):
        return WorkerOutcome.failed(WorkerFailure("decode", "图片无法完整解码"))
    if sha256_file(task.path) != task.expected_hash:
        return WorkerOutcome.failed(WorkerFailure("sha256", "图片 SHA-256 与 manifest 不一致"))
    return WorkerOutcome.success({"sceneId": task.scene_id, "file": task.file})


def create_visual_review_task(
    *,
    workspace: Any,
    project: Any,
    manifest_path: Path,
    coordinator_can_view: bool,
    runtime_child_slots: int = 0,
    coordinator_resource_budget: int = 1,
    runtime_role_capabilities: Iterable[str] = (),
) -> tuple[ValidatedAgentTask, dict[str, Any]]:
    """创建 global visualReview task，并给出宿主协作决策。"""

    run_id = f"vr-{uuid.uuid4().hex[:12]}"
    project.create_run_dir(run_id)
    context = TrustedTaskContext(
        workspace_root=workspace.config.root,
        scope_root=project.root,
        scope_kind="project",
        run_id=run_id,
        task_id="vr-global",
        attempt=1,
    )
    context.task_dir.mkdir(parents=True, exist_ok=False)
    role_contract = context.task_dir / "role-contract.md"
    role_contract.write_text(VISUAL_REVIEW_ROLE_CONTRACT, encoding="utf-8", newline="\n")
    input_paths = [role_contract, project.plan_path, manifest_path]
    input_paths.extend(project.scenes_dir / scene["outputFile"] for scene in project.plan["scenes"])
    inputs = [
        {"file": context.relative_posix(path), "sha256": agent_sha256_file(path)}
        for path in input_paths
    ]
    findings = context.task_dir / "findings.json"
    current_bindings = {
        "generationPlanSha256": agent_sha256_file(project.plan_path),
        "imageManifestSha256": agent_sha256_file(manifest_path),
    }
    task_data = {
        "contractVersion": TASK_CONTRACT_VERSION,
        "taskId": context.task_id,
        "taskKind": "visualReview",
        "scopeKind": "project",
        "roleContractVersion": ROLE_CONTRACT_VERSION,
        "roleContractSha256": agent_sha256_file(role_contract),
        "attempt": 1,
        "sequence": 1,
        "inputs": inputs,
        "currentBindings": current_bindings,
        "requiredCapabilities": ["readFiles", "viewImage", "writeCandidateJson"],
        "allowedOutputs": [
            context.relative_posix(findings),
            context.relative_posix(context.result_json),
        ],
        "formalWritesAllowed": False,
        "approvalWritesAllowed": False,
    }
    write_json_atomic(context.task_json, task_data)
    task = validate_agent_task(
        context.task_json,
        context,
        expected_current_bindings=current_bindings,
    )
    coordinator_caps = {"readFiles", "writeCandidateJson"}
    if coordinator_can_view:
        coordinator_caps.add("viewImage")
    decision = decide_agent_dispatch(
        task,
        configured=workspace.config.for_role("visualReview"),
        ready_tasks=1,
        runtime_child_slots=runtime_child_slots,
        resource_budget=coordinator_resource_budget,
        runtime_role_capabilities=tuple(runtime_role_capabilities),
        coordinator_capabilities=coordinator_caps,
    )
    audit = build_agent_batch_audit(
        stage="visualReview",
        configured=workspace.config.for_role("visualReview"),
        task_count=1,
        decision=decision,
    )
    if decision.mode == "dispatch":
        status = "pending_child_result"
    elif decision.mode == "fallback":
        status = "pending_coordinator_findings"
    else:
        status = "blocked"
    audit.update(
        {
            "taskKind": "visualReview",
            "status": status,
            "taskFile": context.relative_posix(context.task_json),
            "approvalWritten": False,
        }
    )
    return task, audit


def build_visual_review_spawn_package(
    task: ValidatedAgentTask,
    audit: Mapping[str, Any],
) -> dict[str, Any] | None:
    """把 frozen task 转成宿主可直接消费的 spawn_agent 参数；本函数不创建 child。"""

    if not audit.get("dispatchAllowed"):
        return None
    role_contract = (task.context.task_dir / "role-contract.md").resolve()
    task_json = task.context.task_json.resolve()
    attempt_dir = task.context.task_dir.resolve()
    result_json = task.context.result_json.resolve()
    prompt = build_agent_prompt(
        task_json=task_json,
        role_contract=role_contract,
        task_kind=str(task.data["taskKind"]),
        task_sha256=task.task_sha256,
        role_contract_sha256=str(task.data["roleContractSha256"]),
    )
    run_suffix = re.sub(r"[^a-z0-9_]", "_", task.context.run_id.lower())
    task_name = f"visual_review_{run_suffix}"[:64].rstrip("_")
    return {
        "contractVersion": HOST_SPAWN_PACKAGE_VERSION,
        "preparedOnly": True,
        "hostSpawnRequired": True,
        "hostSpawnExecuted": False,
        "taskId": task.data["taskId"],
        "taskKind": task.data["taskKind"],
        "taskJsonPath": str(task_json),
        "taskSha256": task.task_sha256,
        "roleContractPath": str(role_contract),
        "roleContractSha256": task.data["roleContractSha256"],
        "allowedAttemptDir": str(attempt_dir),
        "resultJsonPath": str(result_json),
        "requiredCapabilities": list(task.data["requiredCapabilities"]),
        "spawnAgentCall": {
            "task_name": task_name,
            "fork_turns": "none",
            "message": prompt,
        },
        "completionContract": {
            "resultJsonPath": str(result_json),
            "returnFields": [
                "TASK_STATUS",
                "RESULT_JSON",
                "VALIDATOR_STATUS",
                "SUMMARY",
            ],
        },
    }


def prepare_visual_review_dispatch(
    *,
    workspace: Any,
    project: Any,
    manifest_path: Path,
) -> tuple[ValidatedAgentTask, dict[str, Any]]:
    """准备 visualReview attempt 与宿主派发包，但绝不伪造真实 spawn/agentId。"""

    task, audit = create_visual_review_task(
        workspace=workspace,
        project=project,
        manifest_path=manifest_path,
        coordinator_can_view=True,
        runtime_child_slots=1,
        coordinator_resource_budget=1,
        runtime_role_capabilities=(
            HOST_VISUAL_REVIEW_CAPABILITIES
        ),
    )
    spawn_package = build_visual_review_spawn_package(task, audit)
    audit.update(
        {
            "status": (
                "ready_for_host_spawn"
                if spawn_package is not None
                else audit["status"]
            ),
            "preparedOnly": True,
            "hostSpawnExecuted": False,
            "spawnPackage": spawn_package,
        }
    )
    return task, audit


def record_visual_review_fallback(
    task: ValidatedAgentTask,
    *,
    scene_order: list[str],
    findings: list[dict[str, Any]],
    warnings: list[str] | None = None,
) -> ValidatedAgentResult:
    """coordinator 真实看图后写 findings/result，再交给现有合同重验。"""

    finding_scene_ids = [item.get("sceneId") for item in findings]
    if finding_scene_ids != [scene_id for scene_id in scene_order if scene_id in finding_scene_ids]:
        raise AgentContractError("findings_order", "visualReview findings 必须按 generation plan 顺序")
    normalized_findings: list[dict[str, Any]] = []
    allowed_result_fields = {"priority", "code", "message", "file", "summary"}
    for item in findings:
        scene_id = item.get("sceneId")
        if scene_id not in scene_order:
            raise AgentContractError("findings_scene", "visualReview finding sceneId 不在计划")
        normalized = {key: value for key, value in item.items() if key in allowed_result_fields}
        normalized.setdefault("file", str(scene_id))
        normalized_findings.append(normalized)
    findings_path = task.context.task_dir / "findings.json"
    finding_document = {
        "contractVersion": "whiteboard-visual-review-findings-v1",
        "sceneOrder": scene_order,
        "findings": findings,
        "approvalWritten": False,
    }
    write_json_atomic(findings_path, finding_document)
    outputs = [
        {
            "file": task.context.relative_posix(findings_path),
            "sha256": agent_sha256_file(findings_path),
        }
    ]
    result = {
        "contractVersion": RESULT_CONTRACT_VERSION,
        "taskId": task.data["taskId"],
        "taskKind": task.data["taskKind"],
        "scopeKind": task.data["scopeKind"],
        "attempt": task.data["attempt"],
        "taskSha256": task.task_sha256,
        "roleContractVersion": task.data["roleContractVersion"],
        "roleContractSha256": task.data["roleContractSha256"],
        "sequence": task.data["sequence"],
        "status": "completed",
        "inspectedInputs": list(task.data["inputs"]),
        "outputs": outputs,
        "findings": normalized_findings,
        "warnings": warnings or [],
        "error": None,
    }
    write_json_atomic(task.context.result_json, result)
    return validate_agent_result(
        task.context.result_json,
        task,
        dispatched_task_sha256=task.task_sha256,
        expected_current_bindings=task.data["currentBindings"],
    )


def build_parser() -> argparse.ArgumentParser:
    parser = JsonArgumentParser(description="标注前校验生成图片消费契约")
    parser.add_argument("--project", required=True, help="项目根目录")
    parser.add_argument(
        "--prepare-visual-review",
        action="store_true",
        help="技术验证通过后冻结 global visualReview task，并输出宿主可直接消费的 spawn 包；不实际创建 child",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except CliArgumentError:
        _emit(_summary(ok=False, exit_code=2, project="", error="参数无效"))
        return 2
    except SystemExit as exc:
        return int(exc.code)
    project_arg = str(Path(args.project).resolve(strict=False))

    try:
        workspace = ProjectWorkspace.from_config()
        project = workspace.load_project(args.project)
        configured_concurrency = workspace.config.for_stage("imageValidation")
        plan_scenes = project.plan["scenes"]
        if not plan_scenes:
            raise ManifestValidationError("generation plan 没有场景，当前项目没有可消费图片")
        manifest_path = project.path(project.plan["manifestFile"])
        manifest = _read_manifest(manifest_path)
        by_id = _validate_manifest_contract(project, manifest)
    except (OSError, WorkspaceError, ProjectValidationError, ManifestValidationError) as exc:
        _emit(_summary(ok=False, exit_code=2, project=project_arg, error=str(exc)))
        return 2

    preflight_failures: dict[str, str] = {}
    tasks: list[ImageValidationTask] = []
    for scene in plan_scenes:
        scene_id = scene["sceneId"]
        record = by_id[scene_id]
        if record.get("outputFile") != scene["outputFile"]:
            preflight_failures[scene_id] = "manifest outputFile 与计划不一致"
            continue
        if record.get("status") != "validated":
            preflight_failures[scene_id] = f"场景状态不是 validated: {record.get('status')}"
            continue
        expected_hash = record.get("imageSha256")
        if not isinstance(expected_hash, str) or not SHA256_RE.fullmatch(expected_hash):
            preflight_failures[scene_id] = "manifest imageSha256 无效"
            continue
        tasks.append(
            ImageValidationTask(
                scene_id=scene_id,
                file=f"scenes/{scene['outputFile']}",
                path=project.scenes_dir / scene["outputFile"],
                expected_hash=expected_hash,
            )
        )

    report = execute_bounded(
        tasks,
        _validate_image,
        max_workers=configured_concurrency,
        failure_policy=CONTINUE_INDEPENDENT,
    )
    outcomes = {result.task.scene_id: result for result in report.results}
    failures: list[dict[str, str]] = []
    consumable: list[dict[str, str]] = []
    for scene in plan_scenes:
        scene_id = scene["sceneId"]
        if scene_id in preflight_failures:
            failures.append({"sceneId": scene_id, "error": preflight_failures[scene_id]})
            continue
        result = outcomes[scene_id]
        if result.outcome is None or not result.outcome.ok:
            message = result.outcome.error.message if result.outcome and result.outcome.error else "图片验证失败"
            failures.append({"sceneId": scene_id, "error": message})
        else:
            assert result.outcome.value is not None
            consumable.append(result.outcome.value)

    visual_review: dict[str, Any] | None = None
    if not failures and args.prepare_visual_review:
        try:
            _, visual_review = prepare_visual_review_dispatch(
                workspace=workspace,
                project=project,
                manifest_path=manifest_path,
            )
        except (OSError, AgentContractError) as exc:
            visual_review = {
                "taskKind": "visualReview",
                "mode": "blocked",
                "status": "blocked",
                "reason": str(exc),
                "approvalWritten": False,
            }

    exit_code = 1 if failures else 0
    _emit(
        _summary(
            ok=not failures,
            exit_code=exit_code,
            project=str(project.root),
            total=len(plan_scenes),
            validated=len(consumable),
            failed=len(failures),
            configured_concurrency=configured_concurrency,
            effective_concurrency=report.effective_workers,
            task_count=len(tasks),
            consumable=consumable,
            failures=failures,
            visual_review=visual_review,
        )
    )
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
