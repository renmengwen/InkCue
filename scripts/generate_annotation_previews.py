#!/usr/bin/env python3
"""Phase 5 batch annotation previews and ordered contact sheet generation."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont, UnidentifiedImageError

try:
    from .bounded_execution import (
        CONTINUE_INDEPENDENT,
        WorkerFailure,
        WorkerOutcome,
        execute_bounded,
    )
    from .agent_task_contract import (
        ROLE_CONTRACT_VERSION,
        TASK_CONTRACT_VERSION,
        TrustedTaskContext,
        ValidatedAgentTask,
        build_agent_batch_audit,
        build_agent_prompt,
        decide_agent_dispatch,
        sha256_file as agent_sha256_file,
        validate_agent_task,
    )
    from .annotation_review import write_annotation_review_technical
    from .project_workspace import (
        Project,
        ProjectValidationError,
        WorkspaceConfig,
        WorkspaceError,
        load_project,
        load_workspace_config,
        sha256_file,
        sha256_json,
        write_json_atomic,
    )
    from .render_annotation_preview import render_annotation_preview
    from .render_timing import (
        FormalSceneRender,
        FormalValidationContext,
        RenderTimingError,
        build_formal_validation_context,
        load_formal_validation_context_receipt,
        resolve_formal_scenes,
        validate_formal_context_current,
    )
    from .validation_receipts import (
        ReceiptValidationError,
        bind_candidate_receipt,
        build_candidate_receipt,
    )
except ImportError:  # pragma: no cover - direct script execution
    from bounded_execution import (
        CONTINUE_INDEPENDENT,
        WorkerFailure,
        WorkerOutcome,
        execute_bounded,
    )
    from agent_task_contract import (
        ROLE_CONTRACT_VERSION,
        TASK_CONTRACT_VERSION,
        TrustedTaskContext,
        ValidatedAgentTask,
        build_agent_batch_audit,
        build_agent_prompt,
        decide_agent_dispatch,
        sha256_file as agent_sha256_file,
        validate_agent_task,
    )
    from annotation_review import write_annotation_review_technical
    from project_workspace import (
        Project,
        ProjectValidationError,
        WorkspaceConfig,
        WorkspaceError,
        load_project,
        load_workspace_config,
        sha256_file,
        sha256_json,
        write_json_atomic,
    )
    from render_annotation_preview import render_annotation_preview
    from render_timing import (
        FormalSceneRender,
        FormalValidationContext,
        RenderTimingError,
        build_formal_validation_context,
        load_formal_validation_context_receipt,
        resolve_formal_scenes,
        validate_formal_context_current,
    )
    from validation_receipts import (
        ReceiptValidationError,
        bind_candidate_receipt,
        build_candidate_receipt,
    )


PREVIEW_BATCH_CONTRACT = "whiteboard-annotation-preview-batch-v1"
PREVIEW_VALIDATOR_CONTRACT = "whiteboard-annotation-preview-png-validator-v1"
EXPECTED_SIZE = (1920, 1080)
REVIEW_POLICIES = frozenset({"user_first", "agent_first"})
HOST_SPAWN_PACKAGE_VERSION = "whiteboard-host-spawn-package-v1"
ANNOTATION_VISUAL_REVIEW_ROLE_CONTRACT = """# annotation preview visualReview frozen role contract

- 只读 task.json 冻结的 generation/timing plan、current source PNG、annotation JSON、
  annotation preview、contact sheet 与 technical manifest。
- 必须真实查看全量 current annotation preview bundle；检查区域是否覆盖正确墨迹簇、
  标签/叙事角色是否与画面一致，以及跨幕区域表达是否明显异常。
- annotationDrafting 已完成；本 task 只做 post-generation 额外语义预审，不得修改
  annotation、preview、contact sheet、technical manifest 或任何正式项目文件。
- 只写 findings.json/result.json；findings 仅供用户确认时参考，绝不代表批准，也不得
  写 annotation review approval。
"""
HOST_ANNOTATION_VISUAL_REVIEW_CAPABILITIES = (
    "readFiles",
    "viewImage",
    "writeCandidateJson",
)
_WINDOWS_PATH_RE = re.compile(r"(?i)(?:[a-z]:[\\/][^\s\"']+)")
_SENSITIVE_RE = re.compile(
    r"(?i)(api[_-]?key|access[_-]?token|authorization|cookie|password|secret)\s*[:=]\s*[^\s,;}]+"
)


class AnnotationPreviewBatchError(ValueError):
    """Stable local contract error for Phase 5 preview generation."""


@dataclass(frozen=True)
class AnnotationPreviewTask:
    sequence: int
    scene_id: str
    scene_name: str
    duration_ms: int
    element_count: int
    formal: FormalSceneRender
    annotation_sha256: str
    candidate_path: Path
    output_path: Path


@dataclass(frozen=True)
class AnnotationPreviewCandidate:
    task: AnnotationPreviewTask
    sha256: str
    byte_count: int
    receipt: dict[str, Any]


def _annotation_semantic_review_skipped() -> dict[str, Any]:
    return {
        "taskKind": "visualReview",
        "scope": "annotation_preview_bundle",
        "status": "skipped_by_user",
        "preparedOnly": False,
        "hostSpawnExecuted": False,
        "spawnPackage": None,
        "findingsAreAdvisory": True,
        "approvalWritten": False,
    }


def _annotation_review_bindings(project: Project, context: Any) -> dict[str, str | None]:
    return {
        "generationPlanSha256": agent_sha256_file(project.plan_path),
        "timingPlanSha256": context.timing_plan_sha256,
        "renderProfileSha256": context.render_profile_sha256,
        "activeTimelineSha256": sha256_json(context.active_timeline),
        "audioSha256": context.audio_sha256,
        "fullApprovalIdentityHash": context.full_approval_identity_hash,
    }


def _annotation_review_input_paths(
    project: Project,
    formals: Sequence[FormalSceneRender],
    role_contract: Path,
) -> list[Path]:
    paths = [role_contract, project.plan_path]
    if project.timing_plan_persisted:
        paths.append(project.timing_plan_path)
    paths.append(project.path("manifests/annotation-review-manifest.json"))
    for formal in formals:
        scene = next(
            item for item in project.plan["scenes"] if item["sceneId"] == formal.scene_id
        )
        stem = Path(scene["outputFile"]).stem
        paths.extend(
            [
                formal.image_path,
                formal.annotation_path,
                project.path(f"previews/{stem}-annotation-preview.png"),
            ]
        )
    paths.append(project.path("previews/annotation-preview-contact-sheet.png"))
    return paths


def _build_annotation_visual_review_spawn_package(
    task: ValidatedAgentTask,
    audit: Mapping[str, Any],
) -> dict[str, Any] | None:
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
    task_name = f"annotation_review_{run_suffix}"[:64].rstrip("_")
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


def prepare_annotation_visual_review_dispatch(
    workspace: WorkspaceConfig,
    project: Project,
    formals: Sequence[FormalSceneRender],
    context: Any,
) -> tuple[ValidatedAgentTask, dict[str, Any]]:
    """Freeze a post-generation annotation review task; never spawn or approve."""

    run_id = f"annotation-vr-{uuid.uuid4().hex[:12]}"
    project.create_run_dir(run_id)
    trusted = TrustedTaskContext(
        workspace_root=workspace.root,
        scope_root=project.root,
        scope_kind="project",
        run_id=run_id,
        task_id="annotation-review-global",
        attempt=1,
    )
    trusted.task_dir.mkdir(parents=True, exist_ok=False)
    role_contract = trusted.task_dir / "role-contract.md"
    role_contract.write_text(
        ANNOTATION_VISUAL_REVIEW_ROLE_CONTRACT,
        encoding="utf-8",
        newline="\n",
    )
    inputs = [
        {"file": trusted.relative_posix(path), "sha256": agent_sha256_file(path)}
        for path in _annotation_review_input_paths(project, formals, role_contract)
    ]
    bindings = _annotation_review_bindings(project, context)
    findings = trusted.task_dir / "findings.json"
    task_data = {
        "contractVersion": TASK_CONTRACT_VERSION,
        "taskId": trusted.task_id,
        "taskKind": "visualReview",
        "scopeKind": "project",
        "roleContractVersion": ROLE_CONTRACT_VERSION,
        "roleContractSha256": agent_sha256_file(role_contract),
        "attempt": 1,
        "sequence": 1,
        "inputs": inputs,
        "currentBindings": bindings,
        "requiredCapabilities": list(HOST_ANNOTATION_VISUAL_REVIEW_CAPABILITIES),
        "allowedOutputs": [
            trusted.relative_posix(findings),
            trusted.relative_posix(trusted.result_json),
        ],
        "formalWritesAllowed": False,
        "approvalWritesAllowed": False,
    }
    write_json_atomic(trusted.task_json, task_data)
    task = validate_agent_task(
        trusted.task_json,
        trusted,
        expected_current_bindings=bindings,
    )
    decision = decide_agent_dispatch(
        task,
        configured=workspace.for_role("visualReview"),
        ready_tasks=1,
        runtime_child_slots=1,
        resource_budget=1,
        runtime_role_capabilities=HOST_ANNOTATION_VISUAL_REVIEW_CAPABILITIES,
        coordinator_capabilities=HOST_ANNOTATION_VISUAL_REVIEW_CAPABILITIES,
    )
    audit = build_agent_batch_audit(
        stage="visualReview",
        configured=workspace.for_role("visualReview"),
        task_count=1,
        decision=decision,
    )
    audit.update(
        {
            "taskKind": "visualReview",
            "scope": "annotation_preview_bundle",
            "status": (
                "pending_child_result"
                if decision.mode == "dispatch"
                else "pending_coordinator_findings"
                if decision.mode == "fallback"
                else "blocked"
            ),
            "taskFile": trusted.relative_posix(trusted.task_json),
            "preparedOnly": True,
            "hostSpawnExecuted": False,
            "findingsAreAdvisory": True,
            "approvalWritten": False,
        }
    )
    spawn_package = _build_annotation_visual_review_spawn_package(task, audit)
    audit.update(
        {
            "status": "ready_for_host_spawn" if spawn_package is not None else audit["status"],
            "spawnPackage": spawn_package,
        }
    )
    return task, audit


def annotation_binding_sha256(formals: Sequence[FormalSceneRender]) -> str:
    return sha256_json(
        [
            {
                "sceneId": formal.scene_id,
                "annotationSha256": sha256_file(formal.annotation_path),
                "timingPlanSha256": formal.timing_plan_sha256,
                "renderProfileSha256": formal.render_profile_sha256,
                "audioSha256": formal.audio_sha256,
            }
            for formal in formals
        ]
    )


def _sanitize_error(error: BaseException | str) -> str:
    message = str(error).replace("\r", " ").replace("\n", " ")
    message = _SENSITIVE_RE.sub(lambda match: f"{match.group(1)}=<redacted>", message)
    return _WINDOWS_PATH_RE.sub("<path>", message)


def _scene_name(scene: Mapping[str, Any]) -> str:
    for key in ("name", "title", "coreIdea", "visualSubject"):
        value = scene.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    output = scene.get("outputFile")
    return Path(output).stem if isinstance(output, str) else str(scene.get("sceneId", "scene"))


def _save_candidate_png(image: Image.Image, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=False)
    image.save(path, format="PNG", compress_level=1, optimize=False)


def _validate_preview_candidate(path: Path) -> tuple[str, int]:
    try:
        with Image.open(path) as image:
            image.load()
            if image.format != "PNG":
                raise AnnotationPreviewBatchError("preview candidate 必须是 PNG")
            if image.mode != "RGB":
                raise AnnotationPreviewBatchError("preview candidate 必须是 RGB")
            if image.size != EXPECTED_SIZE:
                raise AnnotationPreviewBatchError("preview candidate 必须是 1920x1080")
    except (OSError, UnidentifiedImageError, ValueError) as exc:
        if isinstance(exc, AnnotationPreviewBatchError):
            raise
        raise AnnotationPreviewBatchError("preview candidate 无法完整解码") from exc
    return sha256_file(path), path.stat().st_size


def _render_task(task: AnnotationPreviewTask) -> WorkerOutcome[AnnotationPreviewCandidate]:
    try:
        with Image.open(task.formal.image_path) as source:
            source.load()
            rendered = render_annotation_preview(source, task.formal.annotation)
        _save_candidate_png(rendered, task.candidate_path)
        digest, byte_count = _validate_preview_candidate(task.candidate_path)
        receipt = build_candidate_receipt(
            candidate_sha256=digest,
            candidate_bytes=byte_count,
            decoded=True,
            format="PNG",
            validator_contract=PREVIEW_VALIDATOR_CONTRACT,
            ttl_seconds=600,
            evidence={"sceneId": task.scene_id, "stage": "annotation-preview"},
        )
        write_json_atomic(task.candidate_path.with_name("candidate.receipt.json"), receipt)
        return WorkerOutcome.success(AnnotationPreviewCandidate(task, digest, byte_count, receipt))
    except Exception as exc:
        return WorkerOutcome.failed(
            WorkerFailure(type(exc).__name__, _sanitize_error(exc), retryable=False)
        )


def _publish_bytes_atomic(
    candidate: Path,
    target: Path,
    expected_sha256: str,
    receipt: Mapping[str, Any] | None = None,
) -> None:
    if receipt is None:
        raise AnnotationPreviewBatchError("preview candidate receipt 缺失，拒绝 binding 发布")
    try:
        bind_candidate_receipt(
            candidate,
            receipt,
            expected_format="PNG",
            expected_validator_contract=PREVIEW_VALIDATOR_CONTRACT,
            require_expiry=True,
        )
    except ReceiptValidationError as exc:
        raise AnnotationPreviewBatchError(f"preview candidate receipt 无效: {exc}") from exc
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.part")
    backup = target.with_name(f".{target.name}.{uuid.uuid4().hex}.previous")
    had_formal = target.is_file()
    published = False
    preserve_backup = False
    try:
        if had_formal:
            try:
                os.link(target, backup)
            except OSError:
                shutil.copy2(target, backup)
        data = candidate.read_bytes()
        with temporary.open("xb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        if sha256_file(temporary) != expected_sha256:
            raise AnnotationPreviewBatchError("preview 原子发布前 SHA 核对失败")
        os.replace(temporary, target)
        published = True
        try:
            bound = bind_candidate_receipt(
                target,
                receipt,
                expected_format="PNG",
                expected_validator_contract=PREVIEW_VALIDATOR_CONTRACT,
                require_expiry=True,
            )
        except ReceiptValidationError as exc:
            raise AnnotationPreviewBatchError(f"正式 preview binding 失败: {exc}") from exc
        if bound["candidateSha256"] != expected_sha256:
            raise AnnotationPreviewBatchError("正式 preview SHA 与候选不一致")
    except Exception:
        if published:
            try:
                if had_formal and backup.is_file():
                    os.replace(backup, target)
                elif target.is_file():
                    target.unlink()
            except OSError as restore_error:
                preserve_backup = True
                raise AnnotationPreviewBatchError(
                    "preview 发布后 binding 失败，旧正式文件恢复失败"
                ) from restore_error
        raise
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        if not preserve_backup:
            backup.unlink(missing_ok=True)


def _contact_font(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", size)


def build_annotation_preview_contact_sheet(
    candidates: Sequence[AnnotationPreviewCandidate],
) -> Image.Image:
    if not candidates:
        raise AnnotationPreviewBatchError("contact sheet 至少需要一个 preview")
    columns = min(2, len(candidates))
    rows = (len(candidates) + columns - 1) // columns
    tile_width, tile_height = 760, 500
    sheet = Image.new("RGB", (columns * tile_width, rows * tile_height), (245, 239, 221))
    draw = ImageDraw.Draw(sheet)
    title_font = _contact_font(22)
    meta_font = _contact_font(18)
    for index, candidate in enumerate(candidates):
        task = candidate.task
        column, row = index % columns, index // columns
        left, top = column * tile_width, row * tile_height
        with Image.open(candidate.task.output_path) as preview:
            preview.load()
            thumbnail = preview.copy()
        thumbnail.thumbnail((720, 405), Image.Resampling.LANCZOS)
        sheet.paste(thumbnail, (left + 20, top + 20))
        draw.text(
            (left + 20, top + 432),
            f"{task.scene_id}  {task.scene_name}",
            font=title_font,
            fill=(45, 45, 45),
        )
        draw.text(
            (left + 20, top + 464),
            f"元素 {task.element_count} · 时长 {task.duration_ms} ms",
            font=meta_font,
            fill=(80, 80, 80),
        )
    return sheet


def _current_bindings_unchanged(project: Project, tasks: Sequence[AnnotationPreviewTask], context: Any) -> None:
    current = load_project(project.root)
    validate_formal_context_current(current, context)
    for task in tasks:
        if not task.formal.annotation_path.is_file():
            raise RenderTimingError("batch 期间 annotation 已删除")
        if sha256_file(task.formal.annotation_path) != task.annotation_sha256:
            raise RenderTimingError("batch 期间 annotation binding 已变化")


def generate_annotation_preview_batch(
    workspace: WorkspaceConfig,
    project: Project,
    *,
    review_policy: str = "user_first",
    allow_v1_disabled_compat: bool = False,
    executor_factory: Callable[[int], Any] | None = None,
    context: FormalValidationContext | None = None,
    formal_context_receipt: str | Path | None = None,
    formal_context_run_id: str | None = None,
) -> dict[str, Any]:
    """Validate the complete Phase 4 technical gate, render, and publish in plan order."""

    if review_policy not in REVIEW_POLICIES:
        raise AnnotationPreviewBatchError(
            "review_policy 必须是 user_first 或 agent_first"
        )

    if context is not None and formal_context_receipt is not None:
        raise AnnotationPreviewBatchError("context 与 formal_context_receipt 不能同时提供")
    if (formal_context_receipt is None) != (formal_context_run_id is None):
        raise AnnotationPreviewBatchError("formal context receipt 与 runId 必须同时提供")
    if formal_context_receipt is not None:
        context = load_formal_validation_context_receipt(
            project,
            formal_context_receipt,
            expected_run_id=formal_context_run_id or "",
        )
    context = context or build_formal_validation_context(project)
    validate_formal_context_current(project, context)
    scene_ids = [scene["sceneId"] for scene in project.plan["scenes"]]
    formals = resolve_formal_scenes(
        project,
        scene_ids,
        context=context,
        allow_v1_disabled_compat=allow_v1_disabled_compat,
    )
    if len(formals) != len(scene_ids):
        raise AnnotationPreviewBatchError("Phase 4 未覆盖 generation plan 全部 scene")
    receipt_file = context.receipt_file
    receipt_mode = "binding" if context.receipt_sha256 is not None else "deep"

    generation = {scene["sceneId"]: scene for scene in project.plan["scenes"]}
    configured = workspace.for_stage("annotationPreview")
    run_root = project.root / ".work" / f"annotation-preview-{uuid.uuid4().hex}"
    tasks: list[AnnotationPreviewTask] = []
    for sequence, formal in enumerate(formals, start=1):
        scene = generation[formal.scene_id]
        stem = Path(scene["outputFile"]).stem
        tasks.append(
            AnnotationPreviewTask(
                sequence=sequence,
                scene_id=formal.scene_id,
                scene_name=_scene_name(scene),
                duration_ms=int(formal.timing_scene["endMs"] - formal.timing_scene["startMs"]),
                element_count=len(formal.annotation["elements"]),
                formal=formal,
                annotation_sha256=sha256_file(formal.annotation_path),
                candidate_path=run_root / f"{sequence:04d}-{formal.scene_id}" / "candidate.png",
                output_path=project.root / "previews" / f"{stem}-annotation-preview.png",
            )
        )

    execution_kwargs: dict[str, Any] = {}
    if executor_factory is not None:
        execution_kwargs["executor_factory"] = executor_factory
    report = execute_bounded(
        tasks,
        _render_task,
        max_workers=configured,
        failure_policy=CONTINUE_INDEPENDENT,
        **execution_kwargs,
    )

    global_stale: str | None = None
    try:
        _current_bindings_unchanged(project, tasks, context)
    except Exception as exc:
        global_stale = _sanitize_error(exc)

    scene_results: list[dict[str, Any]] = []
    published_candidates: list[AnnotationPreviewCandidate] = []
    published_order: list[str] = []
    failed_count = 0
    for result in report.results:
        task = result.task
        if global_stale is not None:
            failed_count += 1
            scene_results.append({"sceneId": task.scene_id, "status": "stale", "error": global_stale})
            continue
        if result.outcome is None or not result.outcome.ok or result.outcome.value is None:
            failed_count += 1
            error = (
                result.outcome.error.message
                if result.outcome is not None and result.outcome.error is not None
                else "preview candidate 未完成"
            )
            scene_results.append({"sceneId": task.scene_id, "status": "failed", "error": _sanitize_error(error)})
            continue
        candidate = result.outcome.value
        try:
            _publish_bytes_atomic(
                candidate.task.candidate_path,
                task.output_path,
                candidate.sha256,
                candidate.receipt,
            )
            published_candidates.append(candidate)
            published_order.append(task.scene_id)
            scene_results.append(
                {
                    "sceneId": task.scene_id,
                    "status": "published_current_technical",
                    "file": task.output_path.relative_to(project.root).as_posix(),
                    "sha256": candidate.sha256,
                }
            )
        except Exception as exc:
            failed_count += 1
            scene_results.append({"sceneId": task.scene_id, "status": "failed", "error": _sanitize_error(exc)})

    contact_file: str | None = None
    contact_sha: str | None = None
    contact_error: str | None = None
    if failed_count == 0 and len(published_candidates) == len(tasks):
        try:
            contact_candidate = run_root / "contact-sheet" / "candidate.png"
            contact_candidate.parent.mkdir(parents=True, exist_ok=False)
            contact = build_annotation_preview_contact_sheet(published_candidates)
            contact.save(contact_candidate, format="PNG", compress_level=1, optimize=False)
            with Image.open(contact_candidate) as image:
                image.load()
                if image.format != "PNG" or image.mode != "RGB":
                    raise AnnotationPreviewBatchError("contact sheet candidate 无效")
            contact_sha = sha256_file(contact_candidate)
            contact_target = project.root / "previews" / "annotation-preview-contact-sheet.png"
            contact_receipt = build_candidate_receipt(
                candidate_sha256=contact_sha,
                candidate_bytes=contact_candidate.stat().st_size,
                decoded=True,
                format="PNG",
                validator_contract=PREVIEW_VALIDATOR_CONTRACT,
                ttl_seconds=600,
                evidence={"stage": "annotation-preview-contact-sheet"},
            )
            write_json_atomic(
                contact_candidate.with_name("candidate.receipt.json"),
                contact_receipt,
            )
            _publish_bytes_atomic(contact_candidate, contact_target, contact_sha, contact_receipt)
            contact_file = contact_target.relative_to(project.root).as_posix()
        except Exception as exc:
            failed_count += 1
            contact_error = _sanitize_error(exc)

    all_passed = failed_count == 0 and len(published_candidates) == len(tasks) and contact_file is not None
    annotation_review_identity: str | None = None
    semantic_review: dict[str, Any] = {
        "taskKind": "visualReview",
        "scope": "annotation_preview_bundle",
        "status": "not_started_technical_failure",
        "preparedOnly": False,
        "hostSpawnExecuted": False,
        "spawnPackage": None,
        "findingsAreAdvisory": True,
        "approvalWritten": False,
    }
    if all_passed:
        try:
            _current_bindings_unchanged(project, tasks, context)
            technical = write_annotation_review_technical(project, formals, context)
            annotation_review_identity = technical["identityHash"]
            if review_policy == "user_first":
                semantic_review = _annotation_semantic_review_skipped()
            else:
                _, semantic_review = prepare_annotation_visual_review_dispatch(
                    workspace,
                    project,
                    formals,
                    context,
                )
        except Exception as exc:
            all_passed = False
            failed_count += 1
            contact_error = _sanitize_error(exc)
            semantic_review = {
                "taskKind": "visualReview",
                "scope": "annotation_preview_bundle",
                "status": "blocked",
                "preparedOnly": False,
                "hostSpawnExecuted": False,
                "spawnPackage": None,
                "findingsAreAdvisory": True,
                "approvalWritten": False,
                "error": contact_error,
            }
    return {
        "contractVersion": PREVIEW_BATCH_CONTRACT,
        "status": "PASS" if all_passed else "FAIL",
        "partialSuccess": bool(published_candidates and not all_passed),
        "configuredConcurrency": configured,
        "effectiveConcurrency": report.effective_workers,
        "peakActiveWorkers": report.peak_active_workers,
        "taskCount": len(tasks),
        "publishedCount": len(published_candidates),
        "failedCount": failed_count,
        "publishedOrder": published_order,
        "scenes": scene_results,
        "contactSheet": contact_file,
        "contactSheetSha256": contact_sha,
        "contactSheetError": contact_error,
        "annotationBindingSha256": annotation_binding_sha256(formals),
        "annotationReviewIdentitySha256": annotation_review_identity,
        "reviewPolicy": review_policy,
        "semanticReview": semantic_review,
        "userConfirmationRequired": True,
        "previewConfirmationWritten": False,
        "approvalWritten": False,
        "nextHumanGate": "annotation_review_confirmation" if all_passed else None,
        "formalValidationMode": receipt_mode,
        "formalValidationReceipt": receipt_file,
        "formalValidationRunId": context.receipt_run_id,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="批量生成 current annotation 区域预览与 contact sheet")
    parser.add_argument("--project", required=True)
    parser.add_argument("--all", action="store_true", dest="all_scenes")
    parser.add_argument("--config")
    parser.add_argument(
        "--review-policy",
        choices=sorted(REVIEW_POLICIES),
        default="user_first",
        help="annotation preview 完成后直接交用户，或先准备一次 AI 语义预审",
    )
    parser.add_argument("--allow-v1-disabled-compat", action="store_true")
    parser.add_argument("--formal-context-receipt", type=Path)
    parser.add_argument("--formal-context-run-id")
    return parser


def _gate_exit_code(exc: BaseException) -> int:
    if isinstance(exc, RenderTimingError):
        text = str(exc).lower()
        if any(token in text for token in ("stale", "变化", "approval", "批准", "current narration")):
            return 5
        return 2
    if isinstance(exc, (AnnotationPreviewBatchError, ProjectValidationError, WorkspaceError, OSError, ValueError)):
        return 2
    return 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if not args.all_scenes:
        parser.error("正式批量入口必须显式传 --all")
    try:
        workspace = load_workspace_config(args.config)
        project = load_project(args.project)
        summary = generate_annotation_preview_batch(
            workspace,
            project,
            review_policy=args.review_policy,
            allow_v1_disabled_compat=args.allow_v1_disabled_compat,
            formal_context_receipt=args.formal_context_receipt,
            formal_context_run_id=args.formal_context_run_id,
        )
        exit_code = 0 if summary["status"] == "PASS" else 1
    except Exception as exc:
        exit_code = _gate_exit_code(exc)
        summary = {
            "contractVersion": PREVIEW_BATCH_CONTRACT,
            "status": "FAIL",
            "error": _sanitize_error(exc),
            "configuredConcurrency": None,
            "effectiveConcurrency": 0,
            "peakActiveWorkers": 0,
            "taskCount": 0,
            "publishedCount": 0,
            "failedCount": 0,
            "partialSuccess": False,
            "reviewPolicy": args.review_policy,
            "semanticReview": {
                "taskKind": "visualReview",
                "scope": "annotation_preview_bundle",
                "status": "not_started_technical_failure",
                "preparedOnly": False,
                "hostSpawnExecuted": False,
                "spawnPackage": None,
                "findingsAreAdvisory": True,
                "approvalWritten": False,
            },
            "userConfirmationRequired": True,
            "previewConfirmationWritten": False,
            "approvalWritten": False,
        }
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())


__all__ = [
    "AnnotationPreviewBatchError",
    "PREVIEW_BATCH_CONTRACT",
    "REVIEW_POLICIES",
    "annotation_binding_sha256",
    "build_annotation_preview_contact_sheet",
    "generate_annotation_preview_batch",
    "prepare_annotation_visual_review_dispatch",
    "main",
]
