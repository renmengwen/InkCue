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

from cover_review import CoverReviewError, load_cover_review

from agent_task_contract import (
    AgentContractError,
    TrustedTaskContext,
    ValidatedAgentResult,
    ValidatedAgentTask,
    build_coordinator_result_payload,
    build_prepared_task_descriptor,
    sha256_file as agent_sha256_file,
    validate_agent_result,
    validate_agent_task,
)
from bounded_execution import CONTINUE_INDEPENDENT, WorkerFailure, WorkerOutcome, execute_bounded
from line_art_review import LineArtReviewError, create_line_art_review
from project_workspace import (
    ProjectValidationError,
    ProjectWorkspace,
    WorkspaceError,
    resolve_project_review_policy,
    sha256_file,
    write_json_atomic,
)


SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
VISUAL_REVIEW_ROLE_CONTRACT = """# visualReview frozen role contract

- 只读 task.json 列出的 generation plan、generation manifest 与 current PNG。
- 必须真实查看全部图片并保持跨幕人物、配色、纸张和构图的全局视野。
- 只写 findings.json；不得写 result.json、修改图片、调用 provider、写 manifest 或批准。result 由 coordinator 确定性生成。
- findings.json 顶层严格使用 schemaVersion=1：只含 schemaVersion、sceneOrder、findings、approvalWritten=false；每条 finding 必须带冻结 sceneOrder 内的 sceneId 和 message/summary，同一幕最多一条并按 scene 顺序。
- 写完 findings.json 后立即以 candidate_ready 返回；不得搜索源码、测试、examples、其他 reference 或 CLI help，也不得自行运行 coordinator validator。
- 按 generation plan 的 constraints.forbidText 核对文字策略：false 时不得因图片含文字而判错，
  只检查语义所需文字是否清晰、正确并避免乱码或意外文字；true 时才检查 scene 源图禁字。
- 若 task.inputs 包含封面，封面是独立 review 图片，允许文字；封面对应的
  `coverFrameRange` 仅豁免视觉语义规则，技术检查仍完整保留。
"""
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
    review_policy: str | None = None,
    semantic_review: dict[str, Any] | None = None,
    line_art_review: dict[str, Any] | None = None,
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
    if review_policy is not None:
        value["reviewPolicy"] = review_policy
    if semantic_review is not None:
        value["semanticReview"] = semantic_review
    if line_art_review is not None:
        value["lineArtReview"] = line_art_review
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
) -> tuple[ValidatedAgentTask, dict[str, Any]]:
    """创建 global visualReview task；宿主调度只由 coordinator 决定。"""

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
    cover_review = load_cover_review(project)
    if cover_review is not None:
        input_paths.append(project.path(cover_review["file"]))
    inputs = [
        {"file": context.relative_posix(path), "sha256": agent_sha256_file(path)}
        for path in input_paths
    ]
    findings = context.task_dir / "findings.json"
    current_bindings = {
        "generationPlanSha256": agent_sha256_file(project.plan_path),
        "imageManifestSha256": agent_sha256_file(manifest_path),
    }
    if cover_review is not None:
        current_bindings["coverManifestSha256"] = cover_review["manifestSha256"]
    task_data = {
        "schemaVersion": 1,
        "taskId": context.task_id,
        "taskKind": "visualReview",
        "scopeKind": "project",
        "roleContractSha256": agent_sha256_file(role_contract),
        "attempt": 1,
        "sequence": 1,
        "inputs": inputs,
        "currentBindings": current_bindings,
        "requiredCapabilities": ["readFiles", "viewImage", "writeCandidateJson"],
        "allowedOutputs": [
            context.relative_posix(findings),
        ],
        "formalWritesAllowed": False,
        "approvalWritesAllowed": False,
    }
    if cover_review is not None:
        task_data["coverReview"] = {
            "file": cover_review["file"],
            "sha256": cover_review["sha256"],
            "frameRange": cover_review["frameRange"],
            "visualReviewExcluded": True,
            "technicalChecksExcluded": False,
        }
    write_json_atomic(context.task_json, task_data)
    task = validate_agent_task(
        context.task_json,
        context,
        expected_current_bindings=current_bindings,
    )
    audit = {
        "stage": "visualReview",
        "configuredAgentConcurrency": workspace.config.for_role("visualReview"),
        "taskCount": 1,
        "preparedTaskCount": 1,
        "preparationMode": "artifact_only",
        "taskKind": "visualReview",
        "status": "ready_for_coordinator_dispatch",
        "taskFile": context.relative_posix(context.task_json),
        "preparedOnly": True,
        "approvalWritten": False,
    }
    return task, audit


def prepare_visual_review_dispatch(
    *,
    workspace: Any,
    project: Any,
    manifest_path: Path,
) -> tuple[ValidatedAgentTask, dict[str, Any]]:
    """准备 visualReview attempt；coordinator 直接选择 spawn/followup/fallback。"""

    task, audit = create_visual_review_task(
        workspace=workspace,
        project=project,
        manifest_path=manifest_path,
    )
    audit.update(
        {
            "preparedTask": build_prepared_task_descriptor(
                task,
                result_writer="coordinator",
            ),
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
        "schemaVersion": 1,
        "sceneOrder": scene_order,
        "findings": findings,
        "approvalWritten": False,
    }
    write_json_atomic(findings_path, finding_document)
    result = build_coordinator_result_payload(
        task,
        output_files=[findings_path],
        findings=normalized_findings,
        warnings=warnings or [],
    )
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
    parser.add_argument(
        "--review-policy",
        choices=("user_first", "agent_first"),
        default=None,
        help=(
            "图片技术验证后的语义审阅策略：user_first 直接交用户；"
            "agent_first 准备现有 global visualReview 宿主派发包"
        ),
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

    # --prepare-visual-review 是历史兼容入口，等价于 agent_first。显式
    # user_first 与它组合会产生互相矛盾的要求，必须在任何项目写入前拒绝。
    review_policy = args.review_policy
    if review_policy == "user_first" and args.prepare_visual_review:
        _emit(
            _summary(
                ok=False,
                exit_code=2,
                project=project_arg,
                review_policy=review_policy,
                semantic_review={
                    "status": "invalid_combination",
                    "approvalWritten": False,
                    "userConfirmationRequired": True,
                },
                error="--review-policy user_first 不能与 --prepare-visual-review 同时使用",
            )
        )
        return 2
    if review_policy is None and args.prepare_visual_review:
        # 历史兼容入口明确等价于 agent_first；音频项目仍会与 approve-full
        # 冻结值核对，不能借此改写策略。
        review_policy = "agent_first"

    try:
        workspace = ProjectWorkspace.from_config()
        project = workspace.load_project(args.project)
        review_policy = resolve_project_review_policy(project, review_policy)
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

    line_art_review: dict[str, Any] | None = None
    handoff_error: str | None = None
    if not failures:
        try:
            line_art_review = create_line_art_review(project, manifest, manifest_path)
        except (OSError, ProjectValidationError, LineArtReviewError, ValueError) as exc:
            handoff_error = f"线稿文件交接生成失败: {exc}"

    visual_review: dict[str, Any] | None = None
    if not failures and handoff_error is None and review_policy == "agent_first":
        try:
            _, visual_review = prepare_visual_review_dispatch(
                workspace=workspace,
                project=project,
                manifest_path=manifest_path,
            )
        except (OSError, AgentContractError, CoverReviewError) as exc:
            visual_review = {
                "taskKind": "visualReview",
                "mode": "blocked",
                "status": "blocked",
                "reason": str(exc),
                "approvalWritten": False,
            }

    semantic_review: dict[str, Any] | None = None
    if handoff_error is not None:
        semantic_review = {
            "status": "not_started_due_to_handoff_failure",
            "approvalWritten": False,
            "userConfirmationRequired": True,
        }
    elif review_policy == "user_first":
        # user_first 明确跳过额外 AI 语义审阅，但不跳过上面的 PNG/manifest
        # 技术校验。这里不创建 visualReview task，也不写任何批准文件。
        semantic_review = {
            "status": "skipped_by_user",
            "approvalWritten": False,
            "userConfirmationRequired": True,
        }
    elif review_policy == "agent_first":
        if visual_review is None:
            semantic_review = {
                "status": "not_started_due_to_validation_failure",
                "taskKind": "visualReview",
                "approvalWritten": False,
                "userConfirmationRequired": True,
            }
        else:
            semantic_review = {
                "status": visual_review.get("status", "unknown"),
                "taskKind": "visualReview",
                "approvalWritten": False,
                "userConfirmationRequired": True,
            }

    exit_code = 1 if failures or handoff_error is not None else 0
    _emit(
        _summary(
            ok=not failures and handoff_error is None,
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
            review_policy=review_policy,
            semantic_review=semantic_review,
            line_art_review=line_art_review,
            error=handoff_error,
        )
    )
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
