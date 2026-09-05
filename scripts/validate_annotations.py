#!/usr/bin/env python3
"""Phase 4 annotation task 冻结、候选校验与顺序发布。"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from PIL import Image, UnidentifiedImageError

try:
    from .agent_task_contract import (
        AgentContractError,
        StaleAgentTaskError,
        TrustedTaskContext,
        ValidatedAgentResult,
        ValidatedAgentTask,
        build_agent_bundle_prompt,
        build_prepared_task_descriptor,
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
    from .annotation_dispatch import (
        DISPATCH_MANIFEST_KIND,
        DISPATCH_MANIFEST_SCHEMA_VERSION,
        build_dispatch_manifest,
        utc_now,
    )
    from .annotation_contract import (
        AnnotationContractError,
        VISUAL_ELEMENTS_KIND,
        VISUAL_ELEMENTS_SCHEMA_VERSION,
        validate_visual_elements,
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
        AgentContractError,
        StaleAgentTaskError,
        TrustedTaskContext,
        ValidatedAgentResult,
        ValidatedAgentTask,
        build_agent_bundle_prompt,
        build_prepared_task_descriptor,
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
    from annotation_dispatch import (  # type: ignore
        DISPATCH_MANIFEST_KIND,
        DISPATCH_MANIFEST_SCHEMA_VERSION,
        build_dispatch_manifest,
        utc_now,
    )
    from annotation_contract import (
        AnnotationContractError,
        VISUAL_ELEMENTS_KIND,
        VISUAL_ELEMENTS_SCHEMA_VERSION,
        validate_visual_elements,
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


ANNOTATION_SCHEMA_VERSION = 1
ANNOTATION_BATCH_KIND = "annotation-batch"
ANNOTATION_PREPARE_KIND = "annotation-prepare"
ANNOTATION_LINT_KIND = "annotation-candidate-lint"
ANNOTATION_UNIT_MATERIALIZE_KIND = "annotation-unit-materialize"
ANNOTATION_DISPATCH_BUNDLE_KIND = "agent-task-unit"
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
    attempt_by_scene: Mapping[str, int] | None = None,
    retry_status_by_scene: Mapping[str, str] | None = None,
) -> tuple[tuple[AnnotationDraftingTask, ...], dict[str, Any]]:
    """创建冻结 task；宿主派发与 fallback 只由 coordinator 决定。"""

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
    attempts = dict(attempt_by_scene or {})
    retry_statuses = dict(retry_status_by_scene or {})
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
            "schemaVersion": 1,
            "kind": "annotation-scene-brief",
            "scene": generation_brief,
            "timingScene": timing[scene_id],
            "image": {
                "file": image_path.relative_to(project.root).as_posix(),
                "sha256": sha256_file(image_path),
            },
            "currentBindings": bindings,
            "authoringContract": {
                "mode": "visual-elements-only",
                "preferredCandidate": {
                    "schemaVersion": VISUAL_ELEMENTS_SCHEMA_VERSION,
                    "kind": VISUAL_ELEMENTS_KIND,
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
            "schemaVersion": 1,
            "taskId": task_id,
            "taskKind": "annotationDrafting",
            "scopeKind": "project",
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
    audit = {
        "stage": "annotationDrafting",
        "configuredAgentConcurrency": workspace.for_role("annotationDrafting"),
        "taskCount": len(prepared),
        "preparedTaskCount": len(prepared),
        "preparationMode": "artifact_only",
        "preparedOnly": True,
        "formalWritesAllowed": False,
        "approvalWritesAllowed": False,
    }
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
    if (
        raw.get("schemaVersion") != VISUAL_ELEMENTS_SCHEMA_VERSION
        or raw.get("kind") != VISUAL_ELEMENTS_KIND
        or set(raw) != {"schemaVersion", "kind", "elements"}
    ):
        raise AnnotationBatchError("annotation 视觉候选 schema/kind 不支持")
    try:
        return validate_visual_elements(raw.get("elements"))
    except AnnotationContractError as exc:
        raise AnnotationBatchError(str(exc)) from exc


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


def _materialize_coordinator_result(
    drafting: AnnotationDraftingTask,
    *,
    project: Project,
    context: FormalValidationContext,
) -> dict[str, Any]:
    """Deterministically write the task result after a child candidate is ready.

    annotationDrafting children are deliberately limited to visual judgement and
    must not author ``result.json``.  This helper is the sole result writer for
    that role; it derives identity/inputs/output SHA values from the frozen task
    and the candidate artifact.
    """

    validate_formal_context_current(project, context)
    candidate = drafting.candidate_path
    if not candidate.is_file():
        raise AnnotationBatchError("annotation candidate 缺失，无法生成 coordinator result")
    # Validate the visual payload before advertising a completed result.  The
    # business validator runs immediately afterwards on the materialized form.
    _load_visual_elements_candidate(candidate)
    task = drafting.task
    result = {
        "schemaVersion": 1,
        "taskId": task.data["taskId"],
        "taskKind": task.data["taskKind"],
        "scopeKind": task.data["scopeKind"],
        "attempt": task.data["attempt"],
        "taskSha256": task.task_sha256,
        "roleContractSha256": task.data["roleContractSha256"],
        "sequence": task.data["sequence"],
        "status": "completed",
        "inspectedInputs": list(task.data["inputs"]),
        "outputs": [
            {
                "file": task.context.relative_posix(candidate),
                "sha256": agent_sha256_file(candidate),
            }
        ],
        "findings": [],
        "warnings": [],
        "error": None,
    }
    result_path = task.context.result_json
    # Always materialize canonical coordinator bytes.  New annotation tasks do
    # not authorize child result output; any pre-existing legacy result is
    # replaced deterministically before validation.
    write_json_atomic(result_path, result)
    return result


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
    _materialize_coordinator_result(drafting, project=project, context=context)
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
            # Artifact-first: candidate readiness is sufficient for the child;
            # coordinator now creates the result contract before validation.
            _materialize_coordinator_result(
                drafting,
                project=project,
                context=frozen,
            )
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
        "schemaVersion": ANNOTATION_SCHEMA_VERSION,
        "kind": ANNOTATION_BATCH_KIND,
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

    if candidate_root.is_symlink():
        raise AnnotationBatchError("candidate root 不能是符号链接")
    root = candidate_root.resolve(strict=True)
    relative = root.relative_to(project.root.resolve(strict=True))
    if len(relative.parts) != 3 or relative.parts[0] != ".work" or relative.parts[2] != "agent-tasks":
        raise AnnotationBatchError("candidate root 必须是 project/.work/<run-id>/agent-tasks")
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
    *,
    workspace_config_path: Path | None = None,
) -> dict[str, Any]:
    """把冻结 tasks 转成宿主中立的有序 unit；不判断或编码派发方式。"""

    if not tasks:
        raise AnnotationBatchError("annotation prepare 没有 ready task")
    candidate_root = tasks[0].task.context.task_dir.parents[1].resolve(strict=True)
    run_id = tasks[0].task.context.run_id
    dispatch_manifest_path = candidate_root / "dispatch-manifest.json"
    script_path = str(Path(__file__).resolve())
    project_root = str(tasks[0].task.context.scope_root.resolve(strict=True))
    config_argv = (
        ["--config", str(workspace_config_path.resolve(strict=True))]
        if workspace_config_path is not None
        else []
    )
    ordered_tasks: list[dict[str, Any]] = []
    for drafting in tasks:
        task = drafting.task
        task_json = task.context.task_json.resolve(strict=True)
        role_contract = task.role_contract_file.resolve(strict=True)
        attempt_dir = task.context.task_dir.resolve(strict=True)
        result_json = task.context.result_json.resolve(strict=False)
        ordered_tasks.append(
            {
                **build_prepared_task_descriptor(task, result_writer="coordinator"),
                "taskId": task.data["taskId"],
                "sceneId": drafting.scene_id,
                "sequence": drafting.sequence,
                "attempt": task.data["attempt"],
                # Retained as the deterministic coordinator output location;
                # annotation children are not allowed to author this file.
                "resultJsonPath": str(result_json),
                "resultWriter": "coordinator",
                "candidateAnnotationPath": str(drafting.candidate_path.resolve(strict=False)),
                "materializedAnnotationPath": str(
                    drafting.materialized_path.resolve(strict=False)
                ),
                "formalWritesAllowed": False,
                "approvalWritesAllowed": False,
                "candidateLint": {
                    "command": [
                        sys.executable,
                        script_path,
                        "lint",
                        "--candidate",
                        str(drafting.candidate_path.resolve(strict=False)),
                    ],
                    "writesPerformed": False,
                    "requiredBeforeNextTask": True,
                },
                "candidateLintArgv": [
                    sys.executable,
                    script_path,
                    "lint",
                    "--candidate",
                    str(drafting.candidate_path.resolve(strict=False)),
                ],
            }
        )

    unit_size = ANNOTATION_MAX_TASKS_PER_DISPATCH_UNIT
    configured = int(audit["configuredAgentConcurrency"])
    task_count = len(tasks)
    desired_units = min(configured, task_count)
    unit_count = max((task_count + unit_size - 1) // unit_size, desired_units)
    base_size, larger_units = divmod(task_count, unit_count)
    chunk_sizes = [
        base_size + (1 if index < larger_units else 0)
        for index in range(unit_count)
    ]
    dispatch_units: list[dict[str, Any]] = []
    offset = 0
    for chunk_size in chunk_sizes:
        unit_tasks = list(tasks[offset : offset + chunk_size])
        offset += chunk_size
        unit_number = len(dispatch_units) + 1
        result_paths = [
            str(drafting.task.context.result_json.resolve(strict=False))
            for drafting in unit_tasks
        ]
        dispatch_unit_id = f"annotation-unit-{unit_number:02d}"
        dispatch_units.append(
            {
                "schemaVersion": ANNOTATION_SCHEMA_VERSION,
                "kind": ANNOTATION_DISPATCH_BUNDLE_KIND,
                "dispatchUnitId": dispatch_unit_id,
                "taskCount": len(unit_tasks),
                "taskIds": [drafting.task.data["taskId"] for drafting in unit_tasks],
                "sceneIds": [drafting.scene_id for drafting in unit_tasks],
                "sequences": [drafting.sequence for drafting in unit_tasks],
                "resultJsonPaths": result_paths,
                "candidateJsonPaths": [
                    str(drafting.candidate_path.resolve(strict=False))
                    for drafting in unit_tasks
                ],
                "resultWriter": "coordinator",
                "candidateLintCommands": [
                    [
                        sys.executable,
                        script_path,
                        "lint",
                        "--candidate",
                        str(drafting.candidate_path.resolve(strict=False)),
                    ]
                    for drafting in unit_tasks
                ],
                "lintBeforeNextTask": True,
                "returnAfterUnitComplete": True,
                "stopAfterCandidateReady": False,
                "completionProtocol": "return_after_unit_complete_v1",
                "normalMaterializeRequiresChildFinished": True,
                "agentPrompt": build_agent_bundle_prompt(
                    [drafting.task for drafting in unit_tasks],
                    max_tasks=ANNOTATION_MAX_TASKS_PER_DISPATCH_UNIT,
                ),
                "batchMaterializeArgv": [
                    sys.executable,
                    script_path,
                    "materialize-unit",
                    "--project",
                    project_root,
                    "--candidate-root",
                    str(candidate_root),
                    "--dispatch-unit-id",
                    dispatch_unit_id,
                    *config_argv,
                ],
                "payloadTooLargeRecovery": "new-short-context-json-only",
                "preparedTasks": [
                    build_prepared_task_descriptor(
                        drafting.task,
                        result_writer="coordinator",
                    )
                    for drafting in unit_tasks
                ],
            }
        )

    dispatch_audit = dict(audit)
    dispatch_audit["dispatchUnitCount"] = len(dispatch_units)
    dispatch_audit["tasksPerDispatchUnit"] = [
        unit["taskCount"] for unit in dispatch_units
    ]
    dispatch_audit.setdefault("kind", "annotation-preparation-audit")
    timestamps = dispatch_audit.setdefault("timestamps", {})
    timestamps.setdefault("dispatchStartedAt", audit.get("prepareStartedAt"))
    timestamps.setdefault("prepareStartedAt", audit.get("prepareStartedAt"))
    timestamps["prepareCompletedAt"] = utc_now()
    durations = dispatch_audit.setdefault("durationsMs", {})
    durations.setdefault("prepare", audit.get("prepareDurationMs"))
    durations.setdefault("candidate", None)
    durations.setdefault("childTail", None)
    durations.setdefault("resultMaterialize", None)
    counters = dispatch_audit.setdefault("counters", {})
    counters.setdefault("candidateReadyCount", 0)
    counters.setdefault("candidateInvalidCount", 0)
    counters.setdefault("candidateStaleCount", 0)
    counters.setdefault("childCancelCount", 0)
    # The manifest is a coordinator-owned, structured handoff. It contains
    # frozen task locators only; the coordinator chooses spawn/followup/fallback.
    dispatch_manifest = build_dispatch_manifest(
        run_id=run_id,
        candidate_root=candidate_root,
        tasks=ordered_tasks,
        dispatch_units=dispatch_units,
        configured_concurrency=int(audit["configuredAgentConcurrency"]),
        audit=dispatch_audit,
    )
    write_json_atomic(dispatch_manifest_path, dispatch_manifest)
    return {
        "schemaVersion": ANNOTATION_SCHEMA_VERSION,
        "kind": ANNOTATION_PREPARE_KIND,
        "operation": "prepare",
        "status": "PASS",
        "runId": run_id,
        "candidateRoot": str(candidate_root),
        "dispatchManifestPath": str(dispatch_manifest_path.resolve(strict=True)),
        "dispatchManifestSha256": sha256_file(dispatch_manifest_path),
        "taskCount": len(ordered_tasks),
        "dispatchUnitCount": len(dispatch_units),
        "configuredAgentConcurrency": int(audit["configuredAgentConcurrency"]),
        "preparationAudit": dispatch_audit,
        "dispatchPlan": {
            "coordinatorDispatchRequired": True,
            "granularity": "contiguous-bundle",
            "completionProtocol": "return_after_unit_complete_v1",
            "childReturnGranularity": "dispatch_unit",
            "maxTasksPerDispatchUnit": unit_size,
            "configuredMaxParallel": int(audit["configuredAgentConcurrency"]),
            "orderedDispatchUnitIds": [
                unit["dispatchUnitId"] for unit in dispatch_units
            ],
        },
        "dispatchUnits": dispatch_units,
        "orderedTasks": ordered_tasks,
        "formalWritesAllowed": False,
        "approvalWritesAllowed": False,
    }


def _load_current_dispatch_manifest(
    candidate_root: Path,
) -> tuple[Path, dict[str, Any]]:
    """Load the coordinator-owned manifest for one current annotation run."""

    if candidate_root.is_symlink():
        raise AnnotationBatchError("candidate root 不能是符号链接")
    root = candidate_root.resolve(strict=True)
    manifest_path = root / "dispatch-manifest.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise AnnotationBatchError("current dispatch manifest 缺失或为符号链接")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AnnotationBatchError("current dispatch manifest 不是可读 UTF-8 JSON") from exc
    manifest_root = manifest.get("candidateRoot") if isinstance(manifest, dict) else None
    if (
        not isinstance(manifest, dict)
        or manifest.get("schemaVersion") != DISPATCH_MANIFEST_SCHEMA_VERSION
        or manifest.get("kind") != DISPATCH_MANIFEST_KIND
        or not isinstance(manifest_root, str)
        or Path(manifest_root).resolve(strict=True) != root
    ):
        raise AnnotationBatchError("current dispatch manifest 合同或 candidateRoot 无效")
    if not isinstance(manifest.get("tasks"), list) or not isinstance(
        manifest.get("dispatchUnits"), list
    ):
        raise AnnotationBatchError("current dispatch manifest tasks/dispatchUnits 无效")
    return manifest_path, manifest


def _resolve_dispatch_unit_tasks(
    manifest: Mapping[str, Any],
    tasks: Sequence[AnnotationDraftingTask],
    dispatch_unit_id: str,
) -> tuple[Mapping[str, Any], tuple[AnnotationDraftingTask, ...]]:
    """Resolve one frozen unit and reject any task/path/order drift."""

    units = manifest.get("dispatchUnits")
    assert isinstance(units, list)
    matches = [
        item
        for item in units
        if isinstance(item, Mapping) and item.get("dispatchUnitId") == dispatch_unit_id
    ]
    if len(matches) != 1:
        raise AnnotationBatchError("dispatchUnitId 不在 current dispatch manifest 或不唯一")
    unit = matches[0]
    task_ids = unit.get("taskIds")
    if (
        unit.get("schemaVersion") != ANNOTATION_SCHEMA_VERSION
        or unit.get("kind") != ANNOTATION_DISPATCH_BUNDLE_KIND
        or not isinstance(task_ids, list)
        or not task_ids
        or len(task_ids) > ANNOTATION_MAX_TASKS_PER_DISPATCH_UNIT
        or any(not isinstance(task_id, str) or not task_id for task_id in task_ids)
        or len(set(task_ids)) != len(task_ids)
        or unit.get("taskCount") != len(task_ids)
    ):
        raise AnnotationBatchError("dispatch unit 合同、taskCount 或 taskIds 无效")
    if (
        unit.get("returnAfterUnitComplete") is not True
        or unit.get("stopAfterCandidateReady") is not False
        or unit.get("lintBeforeNextTask") is not True
    ):
        raise AnnotationBatchError("dispatch unit 不是 current unit-complete 协议")

    loaded_by_id = {item.task.data["taskId"]: item for item in tasks}
    if len(loaded_by_id) != len(tasks):
        raise AnnotationBatchError("current candidate root 包含重复 taskId")
    try:
        ordered = tuple(loaded_by_id[task_id] for task_id in task_ids)
    except KeyError as exc:
        raise AnnotationBatchError("dispatch unit 引用了非 current task") from exc
    if [item.sequence for item in ordered] != sorted(item.sequence for item in ordered):
        raise AnnotationBatchError("dispatch unit task 顺序与 current plan 不一致")

    manifest_tasks = manifest.get("tasks")
    assert isinstance(manifest_tasks, list)
    descriptor_by_id: dict[str, Mapping[str, Any]] = {}
    for descriptor in manifest_tasks:
        if not isinstance(descriptor, Mapping):
            raise AnnotationBatchError("dispatch manifest task descriptor 无效")
        task_id = descriptor.get("taskId")
        if not isinstance(task_id, str) or task_id in descriptor_by_id:
            raise AnnotationBatchError("dispatch manifest taskId 无效或重复")
        descriptor_by_id[task_id] = descriptor
    expected_candidates: list[str] = []
    expected_results: list[str] = []
    for drafting in ordered:
        task_id = drafting.task.data["taskId"]
        descriptor = descriptor_by_id.get(task_id)
        if descriptor is None:
            raise AnnotationBatchError("dispatch unit task 缺少冻结 descriptor")
        attempt_dir = drafting.task.context.task_dir.resolve(strict=True)
        candidate = drafting.candidate_path.resolve(strict=False)
        result = drafting.task.context.result_json.resolve(strict=False)
        if (
            descriptor.get("sceneId") != drafting.scene_id
            or descriptor.get("sequence") != drafting.sequence
            or descriptor.get("attempt") != drafting.task.data["attempt"]
            or not isinstance(descriptor.get("allowedAttemptDir"), str)
            or Path(str(descriptor["allowedAttemptDir"])).resolve(strict=True) != attempt_dir
            or not isinstance(descriptor.get("candidateAnnotationPath"), str)
            or Path(str(descriptor["candidateAnnotationPath"])).resolve(strict=False)
            != candidate
            or not isinstance(descriptor.get("resultJsonPath"), str)
            or Path(str(descriptor["resultJsonPath"])).resolve(strict=False) != result
        ):
            raise AnnotationBatchError("dispatch unit task descriptor 已漂移")
        expected_candidates.append(str(candidate))
        expected_results.append(str(result))
    if unit.get("candidateJsonPaths") != expected_candidates or unit.get(
        "resultJsonPaths"
    ) != expected_results:
        raise AnnotationBatchError("dispatch unit candidate/result 路径已漂移")
    return unit, ordered


def _preflight_unit_candidates(
    manifest: Mapping[str, Any],
    tasks: Sequence[AnnotationDraftingTask],
) -> dict[str, str]:
    """Validate every unit candidate before writing any result/materialized file."""

    audit = manifest.get("audit")
    observations = audit.get("taskObservations", {}) if isinstance(audit, Mapping) else {}
    if not isinstance(observations, Mapping):
        raise AnnotationBatchError("dispatch manifest taskObservations 无效")
    frozen: dict[str, str] = {}
    for drafting in tasks:
        task_id = drafting.task.data["taskId"]
        candidate = drafting.candidate_path
        if candidate.is_symlink() or not candidate.is_file():
            raise AnnotationBatchError(f"{task_id}: candidate 缺失或为符号链接")
        if candidate.resolve(strict=True) != (
            drafting.task.context.task_dir / "candidate.annotation.json"
        ).resolve(strict=True):
            raise AnnotationBatchError(f"{task_id}: candidate 不在冻结 attempt 标准路径")
        _load_visual_elements_candidate(candidate)
        candidate_sha256 = agent_sha256_file(candidate)
        observation = observations.get(task_id)
        if observation is not None:
            if not isinstance(observation, Mapping):
                raise AnnotationBatchError(f"{task_id}: candidate observation 无效")
            status = observation.get("status")
            observed_sha256 = observation.get("frozenCandidateSha256")
            if status in {"invalid", "stale", "forbidden"}:
                raise AnnotationBatchError(f"{task_id}: candidate observation 为 {status}")
            if status not in {None, "missing", "ready"}:
                raise AnnotationBatchError(f"{task_id}: candidate observation 状态无效")
            if observed_sha256 is not None:
                if not isinstance(observed_sha256, str):
                    raise AnnotationBatchError(f"{task_id}: candidate 冻结 SHA 无效")
                if candidate_sha256 != observed_sha256:
                    raise AnnotationBatchError(f"{task_id}: current candidate 与冻结 SHA 不一致")
            elif status == "ready":
                raise AnnotationBatchError(f"{task_id}: ready candidate 缺少冻结 SHA")
        frozen[task_id] = candidate_sha256
    return frozen


def _materialize_one_current_candidate(
    drafting: AnnotationDraftingTask,
    *,
    project: Project,
    context: FormalValidationContext,
    frozen_candidate_sha256: str,
    allow_v1_disabled_compat: bool,
) -> dict[str, Any]:
    """Reuse the existing single-task deterministic materialization logic."""

    if agent_sha256_file(drafting.candidate_path) != frozen_candidate_sha256:
        raise AnnotationBatchError(
            f"{drafting.task.data['taskId']}: candidate 在 unit materialize 前已变化"
        )
    _materialize_coordinator_result(drafting, project=project, context=context)
    validated = validate_agent_result(
        drafting.task.context.result_json,
        drafting.task,
        dispatched_task_sha256=drafting.task.task_sha256,
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
    if agent_sha256_file(drafting.candidate_path) != frozen_candidate_sha256:
        raise AnnotationBatchError(
            f"{drafting.task.data['taskId']}: candidate 在 unit materialize 期间已变化"
        )
    return {
        "taskId": drafting.task.data["taskId"],
        "sceneId": drafting.scene_id,
        "sequence": drafting.sequence,
        "candidateSha256": frozen_candidate_sha256,
        "resultJsonPath": str(drafting.task.context.result_json.resolve(strict=True)),
        "resultSha256": agent_sha256_file(drafting.task.context.result_json),
        "materializedAnnotationPath": str(drafting.materialized_path.resolve(strict=True)),
        "materializedAnnotationSha256": agent_sha256_file(drafting.materialized_path),
        "resultStatus": validated.data["status"],
    }


class _StructuredArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise AnnotationPrepareCLIError("invalid_arguments", message, 2)


def _prepare_parser() -> argparse.ArgumentParser:
    parser = _StructuredArgumentParser(
        description="冻结 annotationDrafting tasks 并输出宿主中立的有序 unit"
    )
    parser.add_argument("--project", required=True)
    parser.add_argument("--config")
    parser.add_argument("--images-confirmed", action="store_true")
    parser.add_argument("--run-id")
    parser.add_argument("--scene-id", action="append", dest="scene_ids")
    return parser


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="批量验证并发布 annotation candidate")
    parser.add_argument("--project", required=True)
    parser.add_argument("--candidate-root", required=True)
    parser.add_argument("--config")
    parser.add_argument("--allow-v1-disabled-compat", action="store_true")
    return parser


def _lint_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="只读校验单个 annotation visual-elements candidate"
    )
    parser.add_argument("--candidate", required=True, type=Path)
    return parser


def _materialize_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="为已就绪的 annotation candidate 确定性生成 coordinator result"
    )
    parser.add_argument("--project", required=True)
    parser.add_argument("--candidate-root", required=True, type=Path)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--config")
    parser.add_argument("--allow-v1-disabled-compat", action="store_true")
    return parser


def _materialize_unit_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="在 child 完成后一次确定性生成一个 annotation dispatch unit 的 results"
    )
    parser.add_argument("--project", required=True)
    parser.add_argument("--candidate-root", required=True, type=Path)
    parser.add_argument("--dispatch-unit-id", required=True)
    parser.add_argument("--config")
    parser.add_argument("--allow-v1-disabled-compat", action="store_true")
    return parser


def _materialize_unit_main(argv: Sequence[str]) -> int:
    started = time.perf_counter()
    task_summaries: list[dict[str, Any]] = []
    args = _materialize_unit_parser().parse_args(argv)
    try:
        workspace = load_workspace_config(args.config)
        project = load_project(args.project)
        context = build_formal_validation_context(project)
        validate_formal_context_current(project, context)
        candidate_root = args.candidate_root.resolve(strict=True)
        tasks = load_annotation_tasks_from_candidate_root(
            workspace,
            project,
            candidate_root,
            context=context,
        )
        dispatch_manifest_path, dispatch_manifest = _load_current_dispatch_manifest(
            candidate_root
        )
        _unit, unit_tasks = _resolve_dispatch_unit_tasks(
            dispatch_manifest,
            tasks,
            args.dispatch_unit_id,
        )
        frozen_candidates = _preflight_unit_candidates(dispatch_manifest, unit_tasks)

        # No attempt/result writes occur until every candidate in the unit has
        # passed the same fail-closed preflight.  Formal scene annotations are
        # still published only by the existing final batch validator.
        for drafting in unit_tasks:
            task_id = drafting.task.data["taskId"]
            task_summaries.append(
                _materialize_one_current_candidate(
                    drafting,
                    project=project,
                    context=context,
                    frozen_candidate_sha256=frozen_candidates[task_id],
                    allow_v1_disabled_compat=args.allow_v1_disabled_compat,
                )
            )

        current_project = load_project(project.root)
        validate_formal_context_current(current_project, context)
        if context_bindings(current_project, context) != context_bindings(project, context):
            raise AnnotationBatchError("unit materialize 期间 current bindings 已变化")
        for drafting in unit_tasks:
            task_id = drafting.task.data["taskId"]
            if agent_sha256_file(drafting.candidate_path) != frozen_candidates[task_id]:
                raise AnnotationBatchError(f"{task_id}: unit 完成前 candidate 已变化")

        completed_at = utc_now()
        duration_ms = round((time.perf_counter() - started) * 1000)
        audit = dispatch_manifest.setdefault("audit", {})
        if not isinstance(audit, dict):
            raise AnnotationBatchError("dispatch manifest audit 无效")
        observations = audit.setdefault("taskObservations", {})
        if not isinstance(observations, dict):
            raise AnnotationBatchError("dispatch manifest materialize audit 子结构无效")
        for item in task_summaries:
            task_id = item["taskId"]
            previous = observations.get(task_id)
            observation = dict(previous) if isinstance(previous, Mapping) else {}
            observation.update(
                {
                    "status": "ready",
                    "candidateSha256": item["candidateSha256"],
                    "frozenCandidateSha256": item["candidateSha256"],
                    "finalizeRecommended": True,
                    "finalizeBasis": "unit_complete_materialize_preflight",
                    "resultMaterializedAt": completed_at,
                    "resultSha256": item["resultSha256"],
                }
            )
            observations[task_id] = observation
        audit.setdefault("timestamps", {})["resultMaterializedAt"] = completed_at
        audit.setdefault("durationsMs", {})["resultMaterialize"] = duration_ms
        write_json_atomic(dispatch_manifest_path, dispatch_manifest)
        summary = {
            "schemaVersion": ANNOTATION_SCHEMA_VERSION,
            "kind": ANNOTATION_UNIT_MATERIALIZE_KIND,
            "operation": "materialize-unit",
            "status": "PASS",
            "dispatchUnitId": args.dispatch_unit_id,
            "taskCount": len(task_summaries),
            "taskIds": [item["taskId"] for item in task_summaries],
            "scenes": task_summaries,
            "resultMaterializeMs": duration_ms,
            "nextAction": "materialize_remaining_units_or_validate_current_annotation_batch",
            "formalWritesPerformed": False,
            "approvalWritten": False,
        }
        code = 0
    except Exception as exc:
        summary = {
            "schemaVersion": ANNOTATION_SCHEMA_VERSION,
            "kind": ANNOTATION_UNIT_MATERIALIZE_KIND,
            "operation": "materialize-unit",
            "status": "FAIL",
            "dispatchUnitId": args.dispatch_unit_id,
            "completedTaskCount": len(task_summaries),
            "completedTasks": task_summaries,
            "error": str(exc),
            "formalWritesPerformed": False,
            "approvalWritten": False,
        }
        code = 2
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return code


def _materialize_main(argv: Sequence[str]) -> int:
    started = time.perf_counter()
    args = _materialize_parser().parse_args(argv)
    try:
        workspace = load_workspace_config(args.config)
        project = load_project(args.project)
        context = build_formal_validation_context(project)
        tasks = load_annotation_tasks_from_candidate_root(
            workspace,
            project,
            args.candidate_root,
            context=context,
        )
        drafting = next(
            (item for item in tasks if item.task.data["taskId"] == args.task_id),
            None,
        )
        if drafting is None:
            raise AnnotationBatchError("taskId 不在 current candidate root")
        candidate_root = args.candidate_root.resolve(strict=True)
        dispatch_manifest_path = candidate_root / "dispatch-manifest.json"
        if dispatch_manifest_path.is_symlink() or not dispatch_manifest_path.is_file():
            raise AnnotationBatchError("current dispatch manifest 缺失或为符号链接")
        dispatch_manifest = json.loads(dispatch_manifest_path.read_text(encoding="utf-8"))
        if (
            not isinstance(dispatch_manifest, dict)
            or dispatch_manifest.get("schemaVersion") != DISPATCH_MANIFEST_SCHEMA_VERSION
            or dispatch_manifest.get("kind") != DISPATCH_MANIFEST_KIND
            or Path(str(dispatch_manifest.get("candidateRoot"))).resolve(strict=True)
            != candidate_root
        ):
            raise AnnotationBatchError("current dispatch manifest 合同或 candidateRoot 无效")
        observations = dispatch_manifest.get("audit", {}).get("taskObservations", {})
        observation = observations.get(args.task_id) if isinstance(observations, dict) else None
        if (
            not isinstance(observation, dict)
            or observation.get("status") != "ready"
            or observation.get("finalizeRecommended") is not True
        ):
            raise AnnotationBatchError("watchdog 尚未为 current candidate 建议 materialize")
        frozen_sha256 = observation.get("frozenCandidateSha256")
        if (
            not isinstance(frozen_sha256, str)
            or agent_sha256_file(drafting.candidate_path) != frozen_sha256
        ):
            raise AnnotationBatchError("current candidate 与 watchdog 冻结 SHA 不一致")
        materialized = _materialize_one_current_candidate(
            drafting,
            project=project,
            context=context,
            frozen_candidate_sha256=frozen_sha256,
            allow_v1_disabled_compat=args.allow_v1_disabled_compat,
        )
        duration_ms = round((time.perf_counter() - started) * 1000)
        audit = dispatch_manifest.setdefault("audit", {})
        audit.setdefault("timestamps", {})["resultMaterializedAt"] = utc_now()
        audit.setdefault("durationsMs", {})["resultMaterialize"] = duration_ms
        observation["resultMaterializedAt"] = utc_now()
        observation["resultSha256"] = materialized["resultSha256"]
        write_json_atomic(dispatch_manifest_path, dispatch_manifest)
        summary = {
            "schemaVersion": ANNOTATION_SCHEMA_VERSION,
            "kind": "annotation-result-materialize",
            "operation": "materialize",
            "status": "PASS",
            "taskId": args.task_id,
            "sceneId": drafting.scene_id,
            "resultJsonPath": str(drafting.task.context.result_json.resolve(strict=True)),
            "resultSha256": materialized["resultSha256"],
            "materializedAnnotationPath": str(drafting.materialized_path.resolve(strict=True)),
            "resultStatus": materialized["resultStatus"],
            "resultMaterializeMs": duration_ms,
            "formalWritesPerformed": False,
            "approvalWritten": False,
        }
        code = 0
    except Exception as exc:
        summary = {
            "schemaVersion": ANNOTATION_SCHEMA_VERSION,
            "kind": "annotation-result-materialize",
            "operation": "materialize",
            "status": "FAIL",
            "error": str(exc),
            "formalWritesPerformed": False,
            "approvalWritten": False,
        }
        code = 2
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return code


def _lint_main(argv: Sequence[str]) -> int:
    args = _lint_parser().parse_args(argv)
    try:
        elements = _load_visual_elements_candidate(args.candidate)
        summary = {
            "schemaVersion": ANNOTATION_SCHEMA_VERSION,
            "kind": ANNOTATION_LINT_KIND,
            "status": "PASS",
            "candidate": str(args.candidate.resolve(strict=True)),
            "elementCount": len(elements),
            "writesPerformed": False,
        }
        code = 0
    except Exception as exc:
        summary = {
            "schemaVersion": ANNOTATION_SCHEMA_VERSION,
            "kind": ANNOTATION_LINT_KIND,
            "status": "FAIL",
            "candidate": str(args.candidate.resolve(strict=False)),
            "error": str(exc),
            "writesPerformed": False,
        }
        code = 2
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return code


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
    started = time.perf_counter()
    started_at = utc_now()
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
        )
        audit["prepareStartedAt"] = started_at
        audit["prepareDurationMs"] = round((time.perf_counter() - started) * 1000)
        summary = build_annotation_prepare_summary(
            tasks,
            audit,
            workspace_config_path=workspace.config_path,
        )
    except Exception as exc:
        failure = _prepare_failure(exc)
        summary = {
            "schemaVersion": ANNOTATION_SCHEMA_VERSION,
            "kind": ANNOTATION_PREPARE_KIND,
            "operation": "prepare",
            "status": "FAIL",
            "error": {"code": failure.code, "message": str(failure)},
            "formalWritesAllowed": False,
            "approvalWritesAllowed": False,
            "preparedOnly": False,
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
            "schemaVersion": ANNOTATION_SCHEMA_VERSION,
            "kind": ANNOTATION_BATCH_KIND,
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
    try:
        from .cli_runtime import configure_utf8_stdio
    except ImportError:  # pragma: no cover - direct script execution
        from cli_runtime import configure_utf8_stdio  # type: ignore
    configure_utf8_stdio()
    raw = list(sys.argv[1:] if argv is None else argv)
    if raw and raw[0] == "prepare":
        return _prepare_main(raw[1:])
    if raw and raw[0] == "lint":
        return _lint_main(raw[1:])
    if raw and raw[0] == "materialize":
        return _materialize_main(raw[1:])
    if raw and raw[0] == "materialize-unit":
        return _materialize_unit_main(raw[1:])
    if raw and raw[0] == "validate":
        raw = raw[1:]
    return _validate_main(raw)


if __name__ == "__main__":
    sys.exit(main())


__all__ = [
    "ANNOTATION_BATCH_KIND",
    "ANNOTATION_LINT_KIND",
    "ANNOTATION_PREPARE_KIND",
    "ANNOTATION_SCHEMA_VERSION",
    "ANNOTATION_UNIT_MATERIALIZE_KIND",
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
