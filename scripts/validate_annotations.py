#!/usr/bin/env python3
"""Phase 4 批量 annotation 候选校验、顺序发布与 coordinator fallback。"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from PIL import Image, UnidentifiedImageError

try:
    from .agent_task_contract import (
        RESULT_CONTRACT_VERSION,
        ROLE_CONTRACT_VERSION,
        TASK_CONTRACT_VERSION,
        AgentContractError,
        StaleAgentTaskError,
        TrustedTaskContext,
        ValidatedAgentResult,
        ValidatedAgentTask,
        build_agent_batch_audit,
        build_agent_bundle_prompt,
        decide_agent_dispatch,
        sha256_file as agent_sha256_file,
        validate_agent_result,
        validate_agent_task,
    )
    from .bounded_execution import (
        CONTINUE_INDEPENDENT,
        WorkerFailure,
        WorkerOutcome,
        execute_bounded,
    )
    from .project_workspace import (
        Project,
        WorkspaceConfig,
        load_project,
        load_workspace_config,
        sha256_file,
        sha256_json,
        write_json_atomic,
    )
    from .render_timing import (
        FormalValidationContext,
        RenderTimingError,
        build_formal_validation_context,
        validate_annotation,
        validate_formal_context_current,
    )
except ImportError:  # pragma: no cover - direct script execution
    from agent_task_contract import (
        RESULT_CONTRACT_VERSION,
        ROLE_CONTRACT_VERSION,
        TASK_CONTRACT_VERSION,
        AgentContractError,
        StaleAgentTaskError,
        TrustedTaskContext,
        ValidatedAgentResult,
        ValidatedAgentTask,
        build_agent_batch_audit,
        build_agent_bundle_prompt,
        decide_agent_dispatch,
        sha256_file as agent_sha256_file,
        validate_agent_result,
        validate_agent_task,
    )
    from bounded_execution import (
        CONTINUE_INDEPENDENT,
        WorkerFailure,
        WorkerOutcome,
        execute_bounded,
    )
    from project_workspace import (
        Project,
        WorkspaceConfig,
        load_project,
        load_workspace_config,
        sha256_file,
        sha256_json,
        write_json_atomic,
    )
    from render_timing import (
        FormalValidationContext,
        RenderTimingError,
        build_formal_validation_context,
        validate_annotation,
        validate_formal_context_current,
    )


ANNOTATION_BATCH_CONTRACT = "whiteboard-annotation-batch-v1"
ANNOTATION_PREPARE_CONTRACT = "whiteboard-annotation-prepare-v2"
ANNOTATION_DISPATCH_BUNDLE_CONTRACT = "whiteboard-agent-task-bundle-v1"
ANNOTATION_MAX_TASKS_PER_DISPATCH_UNIT = 3
_ATTEMPT_RE = re.compile(r"^attempt-([0-9]{4})$")
_REFERENCE = Path(__file__).resolve().parents[1] / "references" / "annotation-drafting-role.md"


class AnnotationBatchError(ValueError):
    """annotation batch 的稳定本地合同错误。"""


class AnnotationPrepareCLIError(ValueError):
    """prepare CLI 的结构化失败。"""

    def __init__(self, code: str, message: str, exit_code: int) -> None:
        super().__init__(message)
        self.code = code
        self.exit_code = exit_code


@dataclass(frozen=True)
class AnnotationDraftingTask:
    scene_id: str
    sequence: int
    task: ValidatedAgentTask
    candidate_path: Path
    materialized_path: Path
    formal_path: Path


@dataclass(frozen=True)
class ValidatedAnnotationCandidate:
    scene_id: str
    sequence: int
    candidate_path: Path
    formal_path: Path
    annotation: dict[str, Any]
    result: ValidatedAgentResult


def _scene_maps(project: Project) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    generation = {scene["sceneId"]: scene for scene in project.plan["scenes"]}
    timing = {scene["sceneId"]: scene for scene in project.timing_plan["scenes"]}
    return generation, timing


def _ordered_scene_ids(project: Project, scene_ids: Iterable[str] | None) -> list[str]:
    plan_order = [scene["sceneId"] for scene in project.plan["scenes"]]
    if scene_ids is None:
        return plan_order
    requested = list(scene_ids)
    if len(requested) != len(set(requested)):
        raise AnnotationBatchError("sceneIds 不能重复")
    unknown = set(requested) - set(plan_order)
    if unknown:
        raise AnnotationBatchError(f"sceneId 不在 generation plan: {sorted(unknown)}")
    requested_set = set(requested)
    return [scene_id for scene_id in plan_order if scene_id in requested_set]


def context_bindings(project: Project, context: FormalValidationContext) -> dict[str, str | None]:
    """task/result 使用的冻结全局 binding 摘要。"""

    return {
        "generationPlanSha256": sha256_file(project.plan_path),
        "timingPlanSha256": context.timing_plan_sha256,
        "renderProfileSha256": context.render_profile_sha256,
        "activeTimelineSha256": sha256_json(context.active_timeline),
        "audioSha256": context.audio_sha256,
        "fullApprovalIdentityHash": context.full_approval_identity_hash,
    }


def prepare_annotation_drafting_tasks(
    workspace: WorkspaceConfig,
    project: Project,
    *,
    images_confirmed: bool,
    context: FormalValidationContext | None = None,
    scene_ids: Iterable[str] | None = None,
    run_id: str | None = None,
    coordinator_can_view: bool,
    attempt_by_scene: Mapping[str, int] | None = None,
    retry_status_by_scene: Mapping[str, str] | None = None,
    runtime_child_slots: int = 0,
    coordinator_resource_budget: int = 1,
    runtime_role_capabilities: Iterable[str] = (),
) -> tuple[tuple[AnnotationDraftingTask, ...], dict[str, Any]]:
    """创建冻结 task，并给出宿主协作决策。"""

    if images_confirmed is not True:
        raise AnnotationBatchError("annotationDrafting 只能在线稿已获用户明确确认后准备")
    frozen = context or build_formal_validation_context(project)
    validate_formal_context_current(project, frozen)
    if not _REFERENCE.is_file():
        raise AnnotationBatchError("冻结 role contract 的权威 reference 不存在")
    ordered = _ordered_scene_ids(project, scene_ids)
    generation, timing = _scene_maps(project)
    plan_sequence = {
        scene["sceneId"]: index
        for index, scene in enumerate(project.plan["scenes"], start=1)
    }
    run = run_id or f"ad-{uuid.uuid4().hex[:8]}"
    bindings = context_bindings(project, frozen)
    prepared: list[AnnotationDraftingTask] = []
    decisions = []
    attempts = dict(attempt_by_scene or {})
    retry_statuses = dict(retry_status_by_scene or {})
    runtime_caps = tuple(runtime_role_capabilities)
    if set(attempts) - set(ordered) or set(retry_statuses) - set(ordered):
        raise AnnotationBatchError("attempt/retry status 只能引用本批 scene")

    # 所有会失败的 scene/run 基础条件先检查完，再创建任何 attempt。这样第 N 幕
    # 缺图、runId 非法或 attempt 已存在时不会留下前 N-1 幕的半准备状态。
    prepared_contexts: dict[str, TrustedTaskContext] = {}
    for scene_id in ordered:
        attempt = attempts.get(scene_id, 1)
        if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
            raise AnnotationBatchError("attempt 必须是正整数")
        if attempt > 1 and retry_statuses.get(scene_id) not in {"failed", "cancelled", "stale"}:
            raise AnnotationBatchError("retry 只允许 failed/cancelled/stale scene 创建新 attempt")
        if attempt == 1 and scene_id in retry_statuses:
            raise AnnotationBatchError("首个 attempt 不得声明 retry status")
        scene = generation[scene_id]
        image_path = project.scenes_dir / scene["outputFile"]
        if not image_path.is_file():
            raise AnnotationBatchError(f"场景图片不存在: {scene_id}")
        trusted = TrustedTaskContext(
            workspace_root=workspace.root,
            scope_root=project.root,
            scope_kind="project",
            run_id=run,
            task_id=f"ann-{scene_id}",
            attempt=attempt,
        )
        if trusted.task_dir.exists():
            raise AnnotationBatchError(f"annotation attempt 已存在: {scene_id}")
        prepared_contexts[scene_id] = trusted

    for scene_id in ordered:
        sequence = plan_sequence[scene_id]
        attempt = attempts.get(scene_id, 1)
        scene = generation[scene_id]
        image_path = project.scenes_dir / scene["outputFile"]
        task_id = f"ann-{scene_id}"
        trusted = prepared_contexts[scene_id]
        trusted.task_dir.mkdir(parents=True, exist_ok=False)
        role_contract = trusted.task_dir / "role-contract.md"
        role_contract.write_bytes(_REFERENCE.read_bytes())
        brief_path = trusted.task_dir / "scene-brief.json"
        generation_brief = {
            key: scene[key]
            for key in ("sceneId", "sourceCueRange", "coreIdea", "visualSubject")
            if key in scene
        }
        brief = {
            "contractVersion": "whiteboard-annotation-scene-brief-v1",
            "scene": generation_brief,
            "timingScene": timing[scene_id],
            "image": {
                "file": image_path.relative_to(project.root).as_posix(),
                "sha256": sha256_file(image_path),
            },
            "currentBindings": bindings,
            "authoringContract": {
                "mode": "visual-elements-only-v1",
                "preferredCandidate": {
                    "contractVersion": "whiteboard-annotation-visual-elements-v1",
                    "elements": "由 child 根据原图与本幕语义填写的非空数组",
                },
                "coordinatorMaterializes": [
                    "sceneId",
                    "canvas",
                    "sceneDurationMs",
                    "timingPlanSha256",
                    "renderProfileSha256",
                    "sceneFrameRange",
                    "timingSource",
                ],
            },
        }
        write_json_atomic(brief_path, brief)
        input_paths = (image_path, brief_path, role_contract)
        candidate = trusted.task_dir / "candidate.annotation.json"
        task_data = {
            "contractVersion": TASK_CONTRACT_VERSION,
            "taskId": task_id,
            "taskKind": "annotationDrafting",
            "scopeKind": "project",
            "roleContractVersion": ROLE_CONTRACT_VERSION,
            "roleContractSha256": agent_sha256_file(role_contract),
            "attempt": attempt,
            "sequence": sequence,
            "sceneId": scene_id,
            "inputs": [
                {"file": trusted.relative_posix(path), "sha256": agent_sha256_file(path)}
                for path in input_paths
            ],
            "currentBindings": bindings,
            "requiredCapabilities": ["readFiles", "viewImage", "writeCandidateJson"],
            "allowedOutputs": [
                trusted.relative_posix(candidate),
                trusted.relative_posix(trusted.result_json),
            ],
            "formalWritesAllowed": False,
            "approvalWritesAllowed": False,
        }
        write_json_atomic(trusted.task_json, task_data)
        validated = validate_agent_task(
            trusted.task_json,
            trusted,
            expected_current_bindings=bindings,
        )
        coordinator_caps = {"readFiles", "writeCandidateJson"}
        if coordinator_can_view:
            coordinator_caps.add("viewImage")
        decision = decide_agent_dispatch(
            validated,
            configured=workspace.for_role("annotationDrafting"),
            ready_tasks=len(ordered),
            runtime_child_slots=runtime_child_slots,
            resource_budget=coordinator_resource_budget,
            runtime_role_capabilities=runtime_caps,
            coordinator_capabilities=coordinator_caps,
        )
        decisions.append(decision)
        prepared.append(
            AnnotationDraftingTask(
                scene_id=scene_id,
                sequence=sequence,
                task=validated,
                candidate_path=candidate,
                materialized_path=trusted.task_dir / "candidate.materialized.annotation.json",
                formal_path=project.scenes_dir / f"{Path(scene['outputFile']).stem}.annotation.json",
            )
        )
    if decisions:
        selected = next(
            (decision for decision in decisions if decision.mode == "blocked"),
            decisions[0],
        )
        audit = build_agent_batch_audit(
            stage="annotationDrafting",
            configured=workspace.for_role("annotationDrafting"),
            task_count=len(prepared),
            decision=selected,
        )
    else:
        audit = {
            "stage": "annotationDrafting",
            "configuredAgentConcurrency": workspace.for_role("annotationDrafting"),
            "effectiveAgentConcurrency": 0,
            "dispatchAllowed": False,
            "mode": "no_ready",
            "adapter": "none",
            "taskCount": 0,
            "peakChildAgents": 0,
            "taskAgents": [],
            "reason": "没有 ready task",
        }
    audit.update(
        {
            "formalWritesAllowed": False,
            "approvalWritesAllowed": False,
        }
    )
    return tuple(prepared), audit


def _candidate_business_validator(
    project: Project,
    context: FormalValidationContext,
    scene_id: str,
    path: Path,
    *,
    allow_v1_disabled_compat: bool,
) -> dict[str, Any]:
    generation, timing = _scene_maps(project)
    if scene_id not in generation:
        raise AnnotationBatchError("candidate sceneId 不在 current generation plan")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AnnotationBatchError("candidate annotation 不是可读 UTF-8 JSON") from exc
    if not isinstance(raw, dict):
        raise AnnotationBatchError("candidate annotation 顶层必须是对象")
    return validate_annotation(
        raw,
        project=project,
        timing_scene=timing[scene_id],
        timing_plan_sha256=context.timing_plan_sha256,
        render_profile_sha256=context.render_profile_sha256,
        active_timeline=context.active_timeline,
        audio_sha256=context.audio_sha256,
        allow_v1_disabled_compat=allow_v1_disabled_compat,
    )


def _load_visual_elements_candidate(path: Path) -> list[Any]:
    """读取 child 视觉判断；legacy 完整 annotation 也只采用 elements。"""

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AnnotationBatchError("annotation 视觉候选不是可读 UTF-8 JSON") from exc
    if not isinstance(raw, dict):
        raise AnnotationBatchError("annotation 视觉候选顶层必须是对象")
    contract = raw.get("contractVersion")
    if contract not in (None, "whiteboard-annotation-visual-elements-v1"):
        raise AnnotationBatchError("annotation 视觉候选 contractVersion 不支持")
    if contract == "whiteboard-annotation-visual-elements-v1" and set(raw) != {
        "contractVersion",
        "elements",
    }:
        raise AnnotationBatchError("visual-elements-v1 只允许 contractVersion/elements")
    elements = raw.get("elements")
    if not isinstance(elements, list) or not elements:
        raise AnnotationBatchError("annotation 视觉候选 elements 必须是非空数组")
    return elements


def _materialize_annotation_candidate(
    drafting: AnnotationDraftingTask,
    *,
    project: Project,
    context: FormalValidationContext,
) -> dict[str, Any]:
    """由 coordinator 注入所有不可变 binding，仅采纳 child 的 elements。"""

    validate_formal_context_current(project, context)
    _generation, timing = _scene_maps(project)
    timing_scene = timing[drafting.scene_id]
    source: dict[str, Any] = {
        "kind": context.active_timeline["kind"],
        "timelineFile": context.active_timeline["file"],
        "timelineSha256": context.active_timeline["sha256"],
        "sceneId": drafting.scene_id,
        "sceneStartMs": timing_scene["startMs"],
        "sceneEndMs": timing_scene["endMs"],
    }
    if context.audio_sha256 is not None:
        source["audioSha256"] = context.audio_sha256
    value = {
        "sceneId": drafting.scene_id,
        "canvas": {
            "width": project.render_profile["width"],
            "height": project.render_profile["height"],
        },
        "sceneDurationMs": timing_scene["sceneDurationMs"],
        "timingPlanSha256": context.timing_plan_sha256,
        "renderProfileSha256": context.render_profile_sha256,
        "sceneFrameRange": {
            "startFrame": timing_scene["startFrame"],
            "endFrameExclusive": timing_scene["endFrameExclusive"],
            "frameCount": timing_scene["frameCount"],
        },
        "timingSource": source,
        "elements": _load_visual_elements_candidate(drafting.candidate_path),
    }
    write_json_atomic(drafting.materialized_path, value)
    return value


def record_coordinator_annotation_candidate(
    drafting: AnnotationDraftingTask,
    annotation: Mapping[str, Any],
    *,
    project: Project,
    context: FormalValidationContext,
    allow_v1_disabled_compat: bool = False,
) -> ValidatedAgentResult:
    """shared-FS fallback：coordinator 只写 attempt candidate/result。"""

    validate_formal_context_current(project, context)
    # fallback 也走与 child 相同的 author/materialize 边界。兼容调用方传入
    # legacy 完整 annotation，但仅其 elements 会进入 coordinator 生成的候选。
    write_json_atomic(drafting.candidate_path, dict(annotation))
    task = drafting.task
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
        "outputs": [
            {
                "file": task.context.relative_posix(drafting.candidate_path),
                "sha256": agent_sha256_file(drafting.candidate_path),
            }
        ],
        "findings": [],
        "warnings": [],
        "error": None,
    }
    write_json_atomic(task.context.result_json, result)
    validated_result = validate_agent_result(
        task.context.result_json,
        task,
        dispatched_task_sha256=task.task_sha256,
        expected_current_bindings=context_bindings(project, context),
        output_validator=lambda kind, path: _load_visual_elements_candidate(path)
        if kind == "annotationDrafting" and path.name == "candidate.annotation.json"
        else None,
    )
    _materialize_annotation_candidate(drafting, project=project, context=context)
    _candidate_business_validator(
        project,
        context,
        drafting.scene_id,
        drafting.materialized_path,
        allow_v1_disabled_compat=allow_v1_disabled_compat,
    )
    return validated_result


def _validate_image_once(path: Path, expected_size: tuple[int, int]) -> None:
    try:
        with Image.open(path) as image:
            image.load()
            if image.format != "PNG":
                raise AnnotationBatchError("场景图片必须是 PNG")
            if image.mode != "RGB":
                raise AnnotationBatchError("场景图片必须是 RGB")
            if image.size != expected_size:
                raise AnnotationBatchError("场景图片尺寸与 render profile 不一致")
    except (OSError, UnidentifiedImageError, ValueError) as exc:
        if isinstance(exc, AnnotationBatchError):
            raise
        raise AnnotationBatchError("场景图片无法完整解码") from exc


def _publish_bytes_atomic(candidate: Path, target: Path) -> None:
    """把已验证候选原字节单文件原子发布，失败不覆盖旧 current。"""

    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.part")
    try:
        data = candidate.read_bytes()
        with temporary.open("xb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        if sha256_file(temporary) != sha256_file(candidate):
            raise AnnotationBatchError("annotation 原子发布前 SHA 核对失败")
        os.replace(temporary, target)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def validate_and_publish_annotation_batch(
    project: Project,
    drafting_tasks: Sequence[AnnotationDraftingTask],
    *,
    context: FormalValidationContext | None = None,
    configured_concurrency: int = 1,
    allow_v1_disabled_compat: bool = False,
) -> dict[str, Any]:
    """并发校验独立候选，随后按 generation plan 顺序逐幕原子发布。"""

    frozen = context or build_formal_validation_context(project)
    validate_formal_context_current(project, frozen)
    ordered_ids = _ordered_scene_ids(project, [item.scene_id for item in drafting_tasks])
    by_id = {item.scene_id: item for item in drafting_tasks}
    ordered_tasks = [by_id[scene_id] for scene_id in ordered_ids]
    generation, _timing = _scene_maps(project)
    expected_size = (project.render_profile["width"], project.render_profile["height"])

    def worker(drafting: AnnotationDraftingTask) -> WorkerOutcome[ValidatedAnnotationCandidate]:
        try:
            _validate_image_once(project.scenes_dir / generation[drafting.scene_id]["outputFile"], expected_size)
            result = validate_agent_result(
                drafting.task.context.result_json,
                drafting.task,
                dispatched_task_sha256=drafting.task.task_sha256,
                expected_current_bindings=context_bindings(project, frozen),
                output_validator=lambda kind, path: _load_visual_elements_candidate(path)
                if kind == "annotationDrafting" and path.name == "candidate.annotation.json"
                else None,
            )
            _materialize_annotation_candidate(
                drafting,
                project=project,
                context=frozen,
            )
            annotation = _candidate_business_validator(
                project,
                frozen,
                drafting.scene_id,
                drafting.materialized_path,
                allow_v1_disabled_compat=allow_v1_disabled_compat,
            )
            return WorkerOutcome.success(
                ValidatedAnnotationCandidate(
                    scene_id=drafting.scene_id,
                    sequence=drafting.sequence,
                    candidate_path=drafting.materialized_path,
                    formal_path=drafting.formal_path,
                    annotation=annotation,
                    result=result,
                )
            )
        except (AnnotationBatchError, AgentContractError, RenderTimingError, OSError) as exc:
            return WorkerOutcome.failed(
                WorkerFailure(type(exc).__name__, str(exc), retryable=False)
            )

    report = execute_bounded(
        ordered_tasks,
        worker,
        max_workers=configured_concurrency,
        failure_policy=CONTINUE_INDEPENDENT,
    )
    global_stale: str | None = None
    try:
        current_project = load_project(project.root)
        validate_formal_context_current(current_project, frozen)
        if context_bindings(current_project, frozen) != context_bindings(project, frozen):
            raise RenderTimingError("batch 期间 current bindings 已变化")
    except Exception as exc:
        global_stale = str(exc)

    scenes: list[dict[str, Any]] = []
    published_order: list[str] = []
    failures = 0
    for result in report.results:
        scene_id = result.task.scene_id
        if global_stale is not None:
            failures += 1
            scenes.append({"sceneId": scene_id, "status": "stale", "error": global_stale})
            continue
        if result.outcome is None or not result.outcome.ok or result.outcome.value is None:
            failures += 1
            message = (
                result.outcome.error.message
                if result.outcome is not None and result.outcome.error is not None
                else "annotation candidate 未完成"
            )
            scenes.append({"sceneId": scene_id, "status": "failed", "error": message})
            continue
        candidate = result.outcome.value
        try:
            _publish_bytes_atomic(candidate.candidate_path, candidate.formal_path)
            if sha256_file(candidate.formal_path) != sha256_file(candidate.candidate_path):
                raise AnnotationBatchError("正式 annotation SHA 与已验证候选不一致")
            published_order.append(scene_id)
            scenes.append(
                {
                    "sceneId": scene_id,
                    "status": "published_current_technical",
                    "file": candidate.formal_path.relative_to(project.root).as_posix(),
                    "sha256": sha256_file(candidate.formal_path),
                }
            )
        except (OSError, AnnotationBatchError) as exc:
            failures += 1
            scenes.append({"sceneId": scene_id, "status": "failed", "error": str(exc)})

    published = len(published_order)
    required_failures: list[dict[str, str]] = []
    published_set = set(published_order)
    for scene_id in _ordered_scene_ids(project, None):
        scene = generation[scene_id]
        formal = project.scenes_dir / f"{Path(scene['outputFile']).stem}.annotation.json"
        try:
            if not formal.is_file():
                raise AnnotationBatchError("正式 annotation 缺失")
            if scene_id not in published_set:
                _validate_image_once(project.scenes_dir / scene["outputFile"], expected_size)
            _candidate_business_validator(
                project,
                frozen,
                scene_id,
                formal,
                allow_v1_disabled_compat=allow_v1_disabled_compat,
            )
        except (AnnotationBatchError, RenderTimingError, OSError) as exc:
            required_failures.append({"sceneId": scene_id, "error": str(exc)})
    all_technical_current = not required_failures
    all_passed = failures == 0 and all_technical_current
    return {
        "contractVersion": ANNOTATION_BATCH_CONTRACT,
        "status": "PASS" if all_passed else "FAIL",
        "partialSuccess": bool(published and not all_passed),
        "configuredConcurrency": configured_concurrency,
        "effectiveConcurrency": report.effective_workers,
        "taskCount": len(ordered_tasks),
        "publishedCount": published,
        "failedCount": failures,
        "requiredSceneCount": len(project.plan["scenes"]),
        "missingOrStaleRequiredCount": len(required_failures),
        "requiredFailures": required_failures,
        "publishedOrder": published_order,
        "scenes": scenes,
        "allTechnicalCurrent": all_technical_current,
        "globalAnnotationConfirmationWritten": False,
        "fullPreviewStarted": False,
        "nextHumanGate": "annotation_content_confirmation" if all_passed else None,
    }


def load_annotation_tasks_from_candidate_root(
    workspace: WorkspaceConfig,
    project: Project,
    candidate_root: Path,
    *,
    context: FormalValidationContext,
) -> tuple[AnnotationDraftingTask, ...]:
    """从一个可信 run/agent-tasks root 读取 task；不接受 scope/run 逃逸。"""

    root = candidate_root.resolve(strict=True)
    relative = root.relative_to(project.root.resolve(strict=True))
    if len(relative.parts) != 3 or relative.parts[0] != ".work" or relative.parts[2] != "agent-tasks":
        raise AnnotationBatchError("candidate root 必须是 project/.work/<run-id>/agent-tasks")
    if root.is_symlink():
        raise AnnotationBatchError("candidate root 不能是符号链接")
    run_id = relative.parts[1]
    bindings = context_bindings(project, context)
    latest_task_json: dict[str, tuple[int, Path]] = {}
    for task_json in root.glob("*/attempt-*/task.json"):
        task_dir = task_json.parent
        match = _ATTEMPT_RE.fullmatch(task_dir.name)
        if match is None or task_dir.parent.parent != root:
            raise AnnotationBatchError("candidate root 包含非法 attempt 路径")
        task_id = task_dir.parent.name
        attempt_number = int(match.group(1))
        previous = latest_task_json.get(task_id)
        if previous is None or attempt_number > previous[0]:
            latest_task_json[task_id] = (attempt_number, task_json)
    loaded: list[AnnotationDraftingTask] = []
    for task_id, (attempt_number, task_json) in latest_task_json.items():
        trusted = TrustedTaskContext(
            workspace_root=workspace.root,
            scope_root=project.root,
            scope_kind="project",
            run_id=run_id,
            task_id=task_id,
            attempt=attempt_number,
        )
        validated = validate_agent_task(
            task_json,
            trusted,
            expected_current_bindings=bindings,
        )
        if validated.data["taskKind"] != "annotationDrafting":
            raise AnnotationBatchError("candidate root 只能包含 annotationDrafting task")
        scene_id = validated.data["sceneId"]
        generation, _ = _scene_maps(project)
        scene = generation.get(scene_id)
        if scene is None:
            raise AnnotationBatchError("candidate task sceneId 已过期")
        loaded.append(
            AnnotationDraftingTask(
                scene_id=scene_id,
                sequence=validated.data["sequence"],
                task=validated,
                candidate_path=trusted.task_dir / "candidate.annotation.json",
                materialized_path=trusted.task_dir / "candidate.materialized.annotation.json",
                formal_path=project.scenes_dir / f"{Path(scene['outputFile']).stem}.annotation.json",
            )
        )
    if not loaded:
        raise AnnotationBatchError("candidate root 没有 task.json")
    if len({item.scene_id for item in loaded}) != len(loaded):
        raise AnnotationBatchError("candidate root 包含重复 scene task")
    plan_order = _ordered_scene_ids(project, [item.scene_id for item in loaded])
    by_id = {item.scene_id: item for item in loaded}
    return tuple(by_id[scene_id] for scene_id in plan_order)


def build_annotation_prepare_summary(
    tasks: Sequence[AnnotationDraftingTask],
    audit: Mapping[str, Any],
) -> dict[str, Any]:
    """把冻结 tasks 转成宿主可直接消费、但不执行 spawn 的机器可读计划。"""

    if not tasks:
        raise AnnotationBatchError("annotation prepare 没有 ready task")
    candidate_root = tasks[0].task.context.task_dir.parents[1].resolve(strict=True)
    run_id = tasks[0].task.context.run_id
    ordered_tasks: list[dict[str, Any]] = []
    for drafting in tasks:
        task = drafting.task
        task_json = task.context.task_json.resolve(strict=True)
        role_contract = task.role_contract_file.resolve(strict=True)
        attempt_dir = task.context.task_dir.resolve(strict=True)
        result_json = task.context.result_json.resolve(strict=False)
        ordered_tasks.append(
            {
                "taskId": task.data["taskId"],
                "sceneId": drafting.scene_id,
                "sequence": drafting.sequence,
                "attempt": task.data["attempt"],
                "taskJsonPath": str(task_json),
                "taskSha256": task.task_sha256,
                "roleContractPath": str(role_contract),
                "roleContractSha256": task.data["roleContractSha256"],
                "allowedAttemptDir": str(attempt_dir),
                "resultJsonPath": str(result_json),
                "candidateAnnotationPath": str(drafting.candidate_path.resolve(strict=False)),
                "materializedAnnotationPath": str(
                    drafting.materialized_path.resolve(strict=False)
                ),
                "formalWritesAllowed": False,
                "approvalWritesAllowed": False,
            }
        )

    unit_size = ANNOTATION_MAX_TASKS_PER_DISPATCH_UNIT
    dispatch_units: list[dict[str, Any]] = []
    for offset in range(0, len(tasks), unit_size):
        unit_tasks = list(tasks[offset : offset + unit_size])
        unit_number = len(dispatch_units) + 1
        first_scene = unit_tasks[0].scene_id
        last_scene = unit_tasks[-1].scene_id
        prompt = build_agent_bundle_prompt(
            [drafting.task for drafting in unit_tasks],
            max_tasks=unit_size,
        )
        result_paths = [
            str(drafting.task.context.result_json.resolve(strict=False))
            for drafting in unit_tasks
        ]
        spawn_request = None
        if audit.get("dispatchAllowed"):
            range_suffix = first_scene.replace("-", "_")
            if last_scene != first_scene:
                range_suffix += f"_to_{last_scene.replace('-', '_')}"
            spawn_request = {
                "taskName": f"annotation_{range_suffix}",
                "forkTurns": "none",
                "prompt": prompt,
            }
        dispatch_units.append(
            {
                "contractVersion": ANNOTATION_DISPATCH_BUNDLE_CONTRACT,
                "dispatchUnitId": f"annotation-unit-{unit_number:02d}",
                "taskCount": len(unit_tasks),
                "taskIds": [drafting.task.data["taskId"] for drafting in unit_tasks],
                "sceneIds": [drafting.scene_id for drafting in unit_tasks],
                "sequences": [drafting.sequence for drafting in unit_tasks],
                "resultJsonPaths": result_paths,
                "spawnRequest": spawn_request,
            }
        )

    task_concurrency_ceiling = int(audit["effectiveAgentConcurrency"])
    effective_child_concurrency = min(task_concurrency_ceiling, len(dispatch_units))
    dispatch_audit = dict(audit)
    dispatch_audit["artifactTaskConcurrencyCeiling"] = task_concurrency_ceiling
    dispatch_audit["effectiveAgentConcurrency"] = effective_child_concurrency
    dispatch_audit["dispatchUnitCount"] = len(dispatch_units)
    dispatch_audit["tasksPerDispatchUnit"] = [
        unit["taskCount"] for unit in dispatch_units
    ]
    return {
        "contractVersion": ANNOTATION_PREPARE_CONTRACT,
        "operation": "prepare",
        "status": "PASS",
        "runId": run_id,
        "candidateRoot": str(candidate_root),
        "taskCount": len(ordered_tasks),
        "dispatchUnitCount": len(dispatch_units),
        "effectiveAgentConcurrency": effective_child_concurrency,
        "dispatchAudit": dispatch_audit,
        "dispatchPlan": {
            "hostAdapter": audit["adapter"],
            "hostSpawnRequired": bool(audit["dispatchAllowed"]),
            "hostSpawnPerformed": False,
            "granularity": "contiguous-bundle-v1",
            "maxTasksPerDispatchUnit": unit_size,
            "maxParallel": effective_child_concurrency,
            "orderedDispatchUnitIds": [
                unit["dispatchUnitId"] for unit in dispatch_units
            ],
        },
        "dispatchUnits": dispatch_units,
        "orderedTasks": ordered_tasks,
        "formalWritesAllowed": False,
        "approvalWritesAllowed": False,
    }


class _StructuredArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise AnnotationPrepareCLIError("invalid_arguments", message, 2)


def _nonnegative_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("必须是非负整数") from exc
    if parsed < 0:
        raise argparse.ArgumentTypeError("必须是非负整数")
    return parsed


def _prepare_parser() -> argparse.ArgumentParser:
    parser = _StructuredArgumentParser(
        description="冻结 annotationDrafting tasks 并输出宿主 spawn 计划"
    )
    parser.add_argument("--project", required=True)
    parser.add_argument("--config")
    parser.add_argument("--images-confirmed", action="store_true")
    parser.add_argument("--run-id")
    parser.add_argument("--scene-id", action="append", dest="scene_ids")
    parser.add_argument(
        "--runtime-child-slots",
        type=_nonnegative_int,
        default=0,
        help="宿主已换算、且已为 coordinator 预留后的 child slots",
    )
    parser.add_argument(
        "--coordinator-resource-budget",
        type=_nonnegative_int,
        default=0,
    )
    parser.add_argument(
        "--runtime-role-capability",
        action="append",
        default=[],
        choices=("readFiles", "viewImage", "writeCandidateJson"),
    )
    parser.add_argument("--coordinator-can-view", action="store_true")
    return parser


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="批量验证并发布 annotation candidate")
    parser.add_argument("--project", required=True)
    parser.add_argument("--candidate-root", required=True)
    parser.add_argument("--config")
    parser.add_argument("--allow-v1-disabled-compat", action="store_true")
    return parser


def _prepare_failure(exc: Exception) -> AnnotationPrepareCLIError:
    message = str(exc)
    if isinstance(exc, AnnotationPrepareCLIError):
        return exc
    if isinstance(exc, StaleAgentTaskError):
        return AnnotationPrepareCLIError("stale_binding", message, 5)
    if isinstance(exc, AgentContractError):
        exit_code = 5 if exc.code == "stale" else 2
        code = "stale_binding" if exit_code == 5 else exc.code
        return AnnotationPrepareCLIError(code, message, exit_code)
    if isinstance(exc, RenderTimingError) and any(
        token in message for token in ("已变化", "current", "不一致", "stale")
    ):
        return AnnotationPrepareCLIError("stale_binding", message, 5)
    if isinstance(exc, AnnotationBatchError) and "明确确认" in message:
        return AnnotationPrepareCLIError("missing_human_confirmation", message, 5)
    return AnnotationPrepareCLIError("invalid_input", message, 2)


def _prepare_main(argv: Sequence[str]) -> int:
    try:
        args = _prepare_parser().parse_args(argv)
        if args.images_confirmed is not True:
            raise AnnotationPrepareCLIError(
                "missing_human_confirmation",
                "annotationDrafting 只能在线稿已获用户明确确认后准备",
                5,
            )
        workspace = load_workspace_config(args.config)
        project = load_project(args.project)
        context = build_formal_validation_context(project)
        tasks, audit = prepare_annotation_drafting_tasks(
            workspace,
            project,
            images_confirmed=True,
            context=context,
            scene_ids=args.scene_ids,
            run_id=args.run_id,
            coordinator_can_view=args.coordinator_can_view,
            runtime_child_slots=args.runtime_child_slots,
            coordinator_resource_budget=args.coordinator_resource_budget,
            runtime_role_capabilities=args.runtime_role_capability,
        )
        summary = build_annotation_prepare_summary(tasks, audit)
    except Exception as exc:
        failure = _prepare_failure(exc)
        summary = {
            "contractVersion": ANNOTATION_PREPARE_CONTRACT,
            "operation": "prepare",
            "status": "FAIL",
            "error": {"code": failure.code, "message": str(failure)},
            "formalWritesAllowed": False,
            "approvalWritesAllowed": False,
            "hostSpawnPerformed": False,
        }
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
        return failure.exit_code
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


def _validate_main(argv: Sequence[str]) -> int:
    args = _parser().parse_args(argv)
    try:
        workspace = load_workspace_config(args.config)
        project = load_project(args.project)
        context = build_formal_validation_context(project)
        tasks = load_annotation_tasks_from_candidate_root(
            workspace,
            project,
            Path(args.candidate_root),
            context=context,
        )
        summary = validate_and_publish_annotation_batch(
            project,
            tasks,
            context=context,
            configured_concurrency=workspace.for_stage("annotationValidation"),
            allow_v1_disabled_compat=args.allow_v1_disabled_compat,
        )
    except Exception as exc:
        summary = {
            "contractVersion": ANNOTATION_BATCH_CONTRACT,
            "status": "FAIL",
            "error": str(exc),
            "globalAnnotationConfirmationWritten": False,
            "fullPreviewStarted": False,
        }
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
        return 1
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0 if summary["status"] == "PASS" else 1


def main(argv: Sequence[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    if raw and raw[0] == "prepare":
        return _prepare_main(raw[1:])
    if raw and raw[0] == "validate":
        raw = raw[1:]
    return _validate_main(raw)


if __name__ == "__main__":
    sys.exit(main())


__all__ = [
    "ANNOTATION_BATCH_CONTRACT",
    "ANNOTATION_PREPARE_CONTRACT",
    "AnnotationBatchError",
    "AnnotationDraftingTask",
    "FormalValidationContext",
    "build_annotation_prepare_summary",
    "context_bindings",
    "load_annotation_tasks_from_candidate_root",
    "main",
    "prepare_annotation_drafting_tasks",
    "record_coordinator_annotation_candidate",
    "validate_and_publish_annotation_batch",
]
