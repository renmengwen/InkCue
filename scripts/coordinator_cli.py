#!/usr/bin/env python3
"""Small public coordinator helpers; no provider, dispatch, or approval writes."""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

try:
    from .agent_task_contract import (
        ROLE_REQUIRED_OUTPUT_BASENAME,
        AgentContractError,
        TrustedTaskContext,
        ValidatedAgentTask,
        build_coordinator_result_payload,
        sha256_file,
        validate_agent_result,
        validate_agent_task,
    )
    from .cli_runtime import configure_utf8_stdio
    from .content_source import content_draft_identity, validate_content_draft
    from .initial_approval_options import (
        build_initial_approval_options,
        parse_initial_approval_response,
    )
    from .project_workspace import (
        Project,
        ProjectValidationError,
        ProjectWorkspace,
        load_project,
        sanitize_project_name,
        validate_pre_project_generation_plan_data,
        write_json_atomic,
    )
    from .prepare_source import prepare_source
    from .render_content_review import create_review_artifact
except ImportError:  # pragma: no cover - direct script execution
    from agent_task_contract import (  # type: ignore
        ROLE_REQUIRED_OUTPUT_BASENAME,
        AgentContractError,
        TrustedTaskContext,
        ValidatedAgentTask,
        build_coordinator_result_payload,
        sha256_file,
        validate_agent_result,
        validate_agent_task,
    )
    from cli_runtime import configure_utf8_stdio  # type: ignore
    from content_source import content_draft_identity, validate_content_draft  # type: ignore
    from initial_approval_options import (  # type: ignore
        build_initial_approval_options,
        parse_initial_approval_response,
    )
    from project_workspace import (  # type: ignore
        Project,
        ProjectValidationError,
        ProjectWorkspace,
        load_project,
        sanitize_project_name,
        validate_pre_project_generation_plan_data,
        write_json_atomic,
    )
    from prepare_source import prepare_source  # type: ignore
    from render_content_review import create_review_artifact  # type: ignore


_ATTEMPT_RE = re.compile(r"attempt-(\d+)")


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"JSON 包含重复字段: {key}")
        result[key] = value
    return result


def _read_json(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"JSON 必须是普通文件: {path}")
    value = json.loads(
        path.read_text(encoding="utf-8"),
        object_pairs_hook=_reject_duplicate_keys,
    )
    if not isinstance(value, dict):
        raise ValueError("JSON 顶层必须是对象")
    return value


def _trusted_context(task_path: Path) -> TrustedTaskContext:
    if task_path.is_symlink():
        raise ValueError("--task 不能是符号链接")
    task = task_path.resolve(strict=True)
    attempt_dir = task.parent
    match = _ATTEMPT_RE.fullmatch(attempt_dir.name)
    if task.name != "task.json" or match is None:
        raise ValueError("--task 必须定位到 attempt-N/task.json")
    task_id = attempt_dir.parent.name
    agent_tasks = attempt_dir.parent.parent
    if agent_tasks.name != "agent-tasks":
        raise ValueError("task 不在 agent-tasks scope")
    run_dir = agent_tasks.parent
    if run_dir.parent.name != ".work":
        raise ValueError("task 不在可信 .work/<run>/agent-tasks scope")
    scope_root = run_dir.parent.parent
    if scope_root.parent.name == "drafts":
        scope_kind = "draft"
    elif scope_root.parent.name == "projects":
        scope_kind = "project"
    else:
        raise ValueError("task scope 必须位于 workspace/drafts 或 workspace/projects")
    return TrustedTaskContext(
        workspace_root=scope_root.parent.parent,
        scope_root=scope_root,
        scope_kind=scope_kind,
        run_id=run_dir.name,
        task_id=task_id,
        attempt=int(match.group(1)),
    )


def _load_dispatched_task(
    task_path: Path,
    dispatched_task_sha256: str,
) -> ValidatedAgentTask:
    context = _trusted_context(task_path)
    task = validate_agent_task(
        context.task_json,
        context,
        expected_current_bindings=None,
    )
    if task.task_sha256 != dispatched_task_sha256:
        raise AgentContractError("stale", "task.json 与派发时 SHA 不一致")
    return task


def _required_candidate_path(task: ValidatedAgentTask) -> Path:
    basename = ROLE_REQUIRED_OUTPUT_BASENAME[task.data["taskKind"]]
    if basename is None:
        raise ValueError("taskKind 没有 candidate 输出合同")
    candidates = [path for path in task.allowed_output_files if path.name == basename]
    if len(candidates) != 1:
        raise ValueError("task allowedOutputs 没有唯一 candidate 输出")
    path = candidates[0]
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"candidate 必须是 attempt 内普通文件: {path.name}")
    return path


def _validate_draft_candidate(task: ValidatedAgentTask, candidate: Path) -> dict[str, Any]:
    value = _read_json(candidate)
    if task.data["taskKind"] == "contentDrafting":
        draft = validate_content_draft(value)
        if draft["visualStylePreset"] != task.data["visualStylePreset"]:
            raise ValueError("candidate visualStylePreset 与冻结 task 不匹配")
        return {
            "contentDraftIdentitySha256": content_draft_identity(draft),
            "cueCount": len(draft["narrationCues"]),
            "sceneCount": len(draft["scenes"]),
            "visualStylePreset": draft["visualStylePreset"],
        }

    source_srt = next(
        (path for path in task.input_files if path.name == "source.srt"),
        None,
    )
    if source_srt is None:
        raise ValueError("storyboard task 缺少冻结 source.srt")
    plan = validate_pre_project_generation_plan_data(
        value,
        source_srt_path=source_srt,
    )
    expected_style = {
        "visualStylePreset": task.data["visualStylePreset"],
        "visualStyleDisplayName": task.data["visualStyleDisplayName"],
        "visualStylePromptRecipeSha256": task.data["visualStylePromptRecipeSha256"],
    }
    for field, expected in expected_style.items():
        if plan.get(field) != expected:
            raise ValueError(f"candidate {field} 与冻结 task 不匹配")
    if plan.get("globalPrompt") != task.data["visualStylePromptRecipe"]:
        raise ValueError("candidate globalPrompt 未原样绑定冻结 promptRecipe")
    return {
        "sceneCount": len(plan["scenes"]),
        **expected_style,
    }


def _scene_order_from_frozen_inputs(task: ValidatedAgentTask) -> list[str]:
    preferred: list[Path] = []
    fallback: list[Path] = []
    for path in task.input_files:
        normalized = path.as_posix()
        if path.name == "scene-review-bundle.json":
            preferred.append(path)
        elif normalized.endswith("/planning/generation-plan.json"):
            fallback.append(path)
    for path in [*preferred, *fallback]:
        value = _read_json(path)
        raw_order = value.get("sceneOrder")
        if raw_order is None:
            scenes = value.get("scenes")
            if isinstance(scenes, list):
                raw_order = [item.get("sceneId") if isinstance(item, dict) else None for item in scenes]
        if (
            isinstance(raw_order, list)
            and raw_order
            and all(isinstance(item, str) and item for item in raw_order)
            and len(set(raw_order)) == len(raw_order)
        ):
            return list(raw_order)
    raise ValueError("visualReview task 缺少可绑定的冻结 sceneOrder")


def _validate_visual_findings(
    task: ValidatedAgentTask,
    findings_path: Path,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    document = _read_json(findings_path)
    required = {"schemaVersion", "sceneOrder", "findings", "approvalWritten"}
    if set(document) != required:
        raise ValueError("findings.json 字段必须严格匹配 schemaVersion=1")
    if document["schemaVersion"] != 1:
        raise ValueError("findings.json schemaVersion 必须为 1")
    if document["approvalWritten"] is not False:
        raise ValueError("findings.json approvalWritten 必须为 false")
    scene_order = _scene_order_from_frozen_inputs(task)
    if document["sceneOrder"] != scene_order:
        raise ValueError("findings.json sceneOrder 与冻结 task 输入不一致")
    findings = document["findings"]
    if not isinstance(findings, list):
        raise ValueError("findings.json findings 必须是数组")

    allowed = {
        "sceneId",
        "priority",
        "code",
        "message",
        "summary",
        "file",
        "timestampMs",
        "frameNumber",
    }
    result_findings: list[dict[str, Any]] = []
    reported_scene_ids: list[str] = []
    for index, finding in enumerate(findings):
        if not isinstance(finding, dict) or set(finding) - allowed:
            raise ValueError(f"findings[{index}] 字段不合法")
        scene_id = finding.get("sceneId")
        if not isinstance(scene_id, str) or scene_id not in scene_order:
            raise ValueError(f"findings[{index}].sceneId 不在冻结 sceneOrder")
        if "message" not in finding and "summary" not in finding:
            raise ValueError(f"findings[{index}] 缺少 message/summary")
        for field in ("message", "summary", "code", "file"):
            if field in finding and (
                not isinstance(finding[field], str) or not finding[field].strip()
            ):
                raise ValueError(f"findings[{index}].{field} 必须是非空字符串")
        reported_scene_ids.append(scene_id)
        normalized = {
            key: value
            for key, value in finding.items()
            if key in {"priority", "code", "message", "file", "summary"}
        }
        normalized.setdefault("file", scene_id)
        result_findings.append(normalized)
    if len(set(reported_scene_ids)) != len(reported_scene_ids):
        raise ValueError("findings.json 同一 sceneId 最多一条 finding")
    expected_subsequence = [scene_id for scene_id in scene_order if scene_id in reported_scene_ids]
    if reported_scene_ids != expected_subsequence:
        raise ValueError("findings.json 必须按冻结 sceneOrder 排序")
    return result_findings, {
        "sceneCount": len(scene_order),
        "findingCount": len(findings),
    }


def _validate_candidate(
    task: ValidatedAgentTask,
) -> tuple[Path, list[dict[str, Any]], dict[str, Any]]:
    candidate = _required_candidate_path(task)
    if task.data["taskKind"] in {"contentDrafting", "storyboardPlanning"}:
        summary = _validate_draft_candidate(task, candidate)
        return candidate, [], summary
    if task.data["taskKind"] == "visualReview":
        findings, summary = _validate_visual_findings(task, candidate)
        return candidate, findings, summary
    raise ValueError("通用 candidate 入口不处理 annotationDrafting")


def validate_agent_candidate(
    task_path: Path,
    dispatched_task_sha256: str,
) -> dict[str, Any]:
    started = time.perf_counter()
    task = _load_dispatched_task(task_path, dispatched_task_sha256)
    candidate, _findings, candidate_summary = _validate_candidate(task)
    return {
        "schemaVersion": 1,
        "operation": "validate-agent-candidate",
        "status": "PASS",
        "taskId": task.data["taskId"],
        "taskKind": task.data["taskKind"],
        "attempt": task.data["attempt"],
        "candidatePath": str(candidate.resolve()),
        "candidateSha256": sha256_file(candidate),
        **candidate_summary,
        "validationDurationMs": round((time.perf_counter() - started) * 1000),
        "formalWritesPerformed": False,
        "approvalWritten": False,
    }


def materialize_agent_result(
    task_path: Path,
    dispatched_task_sha256: str,
) -> dict[str, Any]:
    started = time.perf_counter()
    task = _load_dispatched_task(task_path, dispatched_task_sha256)
    candidate, findings, candidate_summary = _validate_candidate(task)
    payload = build_coordinator_result_payload(
        task,
        output_files=[candidate],
        findings=findings,
    )
    write_json_atomic(task.context.result_json, payload)
    result = validate_agent_result(
        task.context.result_json,
        task,
        dispatched_task_sha256=dispatched_task_sha256,
        expected_current_bindings=task.data["currentBindings"],
    )
    return {
        "schemaVersion": 1,
        "operation": "materialize-agent-result",
        "status": "PASS",
        "taskId": task.data["taskId"],
        "taskKind": task.data["taskKind"],
        "attempt": task.data["attempt"],
        "candidatePath": str(candidate.resolve()),
        "candidateSha256": sha256_file(candidate),
        "resultJsonPath": str(task.context.result_json.resolve()),
        "resultSha256": result.result_sha256,
        **candidate_summary,
        "validationDurationMs": round((time.perf_counter() - started) * 1000),
        "formalWritesPerformed": False,
        "approvalWritten": False,
    }


def _matching_pending_content_project(
    workspace: ProjectWorkspace,
    project_root: Path,
    *,
    project_name: str,
    content_draft_identity_sha256: str,
    source_package_identity_sha256: str,
    generation_plan: dict[str, Any],
    voiceover_mode: str,
    visual_style_preset: str,
) -> Project:
    """只复用一次 finalize 已精确提交的同一 pending 预项目。"""

    if project_root.is_symlink():
        raise ProjectValidationError("既有预项目目录不能是符号链接")
    project = workspace.load_project(
        project_root,
        allow_pending_audio_timeline=True,
        allow_pending_initial_approval=True,
    )
    content_source = project.metadata.get("contentSource")
    detached_plan = dict(project.plan)
    detached_plan["projectId"] = ""
    if (
        project.metadata.get("projectName") != project_name
        or not project.pending_initial_approval
        or project.current_content_identity_sha256 != content_draft_identity_sha256
        or not isinstance(content_source, dict)
        or content_source.get("sourcePackageIdentitySha256")
        != source_package_identity_sha256
        or project.voiceover_mode != voiceover_mode
        or project.visual_style_preset != visual_style_preset
        or detached_plan != generation_plan
    ):
        raise ProjectValidationError(
            "draftRoot 同名项目已存在，但不是本次 content draft 的同一 pending 预项目"
        )
    return project


def finalize_content_draft(args: argparse.Namespace) -> dict[str, Any]:
    """一次收口 contentDrafting candidate，最终停在初始联合批准 Gate。"""

    started = time.perf_counter()
    task = _load_dispatched_task(args.task, args.dispatched_task_sha256)
    if task.data["taskKind"] != "contentDrafting":
        raise ValueError("finalize-content-draft 只接受 contentDrafting task")
    if task.context.scope_kind != "draft":
        raise ValueError("finalize-content-draft task 必须位于 workspace/drafts scope")

    workspace = ProjectWorkspace.from_config(args.workspace_config)
    workspace_root = workspace.config.root.resolve(strict=True)
    if task.context.workspace_root.resolve(strict=True) != workspace_root:
        raise ValueError("task workspace 与 --workspace-config 不一致")
    draft_root = task.context.scope_root.resolve(strict=True)
    expected_drafts = (workspace_root / "drafts").resolve(strict=False)
    if draft_root.parent != expected_drafts or not draft_root.name:
        raise ValueError("task draftRoot 不是 workspace/drafts 的直接子目录")

    materialized = materialize_agent_result(
        args.task,
        args.dispatched_task_sha256,
    )
    candidate = Path(materialized["candidatePath"])

    review = create_review_artifact(
        argparse.Namespace(
            draft_root=draft_root,
            candidate=candidate,
            workspace_config=args.workspace_config,
            gpt_login_image_generation_available=(
                args.gpt_login_image_generation_available
            ),
            configured_image_provider_available=(
                args.configured_image_provider_available
            ),
            fixed_image_generation_mode=args.fixed_image_generation_mode,
        )
    )
    if (
        review.get("ok") is not True
        or review.get("valid") is not True
        or review.get("contentDraftIdentitySha256")
        != materialized["contentDraftIdentitySha256"]
    ):
        raise ValueError("content review artifact 未通过确定性校验")
    review_path = draft_root.joinpath(*Path(review["reviewFile"]).parts)
    if not review_path.is_file() or sha256_file(review_path) != review["reviewSha256"]:
        raise ValueError("content review artifact 写入后 SHA 不一致")

    source_root = draft_root / "source-package"
    if source_root.is_symlink():
        raise ValueError("draftRoot/source-package 不能是符号链接")
    source_package = prepare_source(candidate, source_root)
    if (
        source_package.content_draft_identity
        != materialized["contentDraftIdentitySha256"]
    ):
        raise ValueError("source package 与 materialized candidate identity 不一致")

    project_name = sanitize_project_name(draft_root.name)
    if project_name != draft_root.name:
        raise ValueError("draftRoot basename 不是可直接使用的唯一项目名")
    project_root = (workspace.config.projects_dir / project_name).resolve(strict=False)
    if project_root.exists():
        project = _matching_pending_content_project(
            workspace,
            project_root,
            project_name=project_name,
            content_draft_identity_sha256=source_package.content_draft_identity,
            source_package_identity_sha256=source_package.manifest[
                "sourcePackageIdentitySha256"
            ],
            generation_plan=source_package.generation_plan,
            voiceover_mode=source_package.draft["voiceoverMode"],
            visual_style_preset=source_package.draft["visualStylePreset"],
        )
        project_created = False
    else:
        project = workspace.create_project(
            project_name,
            source_package.directory / "source.srt",
            confirmed_plan=source_package.generation_plan,
            voiceover_mode=source_package.draft["voiceoverMode"],
            visual_style_preset=source_package.draft["visualStylePreset"],
            pending_initial_approval=True,
            source_input=source_package.directory / "input.json",
            source_manifest=source_package.directory / "manifest.json",
            source_plan=source_package.directory / "generation-plan.json",
        )
        project_created = True

    status = project_status(project.root)
    if (
        status.get("status") != "PASS"
        or status.get("pendingInitialApproval") is not True
        or status.get("nextGate") != "initial_content_plan_approval"
        or status.get("approvalWritten") is not False
    ):
        raise ValueError("新预项目没有安全停在 initial content plan approval Gate")

    return {
        "schemaVersion": 1,
        "operation": "finalize-content-draft",
        "status": "待确认",
        "technicalStatus": "PASS",
        "taskId": task.data["taskId"],
        "attempt": task.data["attempt"],
        "candidatePath": materialized["candidatePath"],
        "candidateSha256": materialized["candidateSha256"],
        "contentDraftIdentitySha256": source_package.content_draft_identity,
        "resultJsonPath": materialized["resultJsonPath"],
        "resultSha256": materialized["resultSha256"],
        "reviewFilePath": str(review_path.resolve(strict=True)),
        "reviewSha256": review["reviewSha256"],
        "sourcePackagePath": str(source_package.directory.resolve(strict=True)),
        "sourcePackageIdentitySha256": source_package.manifest[
            "sourcePackageIdentitySha256"
        ],
        "projectRoot": str(project.root.resolve(strict=True)),
        "projectId": project.project_id,
        "projectCreated": project_created,
        "pendingInitialApproval": True,
        "cueCount": materialized["cueCount"],
        "sceneCount": materialized["sceneCount"],
        "visualStylePreset": source_package.draft["visualStylePreset"],
        "initialApprovalOptions": review["initialApprovalOptions"],
        "userConfirmationRequired": True,
        "nextGate": "initial_content_plan_approval",
        "nextCommandArgv": None,
        "formalPublished": True,
        "approvalWritten": False,
        "durationMs": round((time.perf_counter() - started) * 1000),
    }


def validate_draft_result(task_path: Path) -> dict[str, Any]:
    started = time.perf_counter()
    context = _trusted_context(task_path)
    task = validate_agent_task(context.task_json, context)
    if task.data["taskKind"] != "contentDrafting":
        raise ValueError("validate-draft-result 只接受 contentDrafting task")

    candidate_summary: dict[str, Any] = {}

    def validate_output(_relative: str, path: Path) -> None:
        if path.name == "candidate.content-draft.json":
            draft = validate_content_draft(_read_json(path))
            if draft["visualStylePreset"] != task.data["visualStylePreset"]:
                raise ValueError(
                    "candidate visualStylePreset 与冻结 draft task 不匹配"
                )
            candidate_summary.update(
                {
                    "contentDraftIdentitySha256": content_draft_identity(draft),
                    "cueCount": len(draft["narrationCues"]),
                    "sceneCount": len(draft["scenes"]),
                    "visualStylePreset": draft["visualStylePreset"],
                    "visualStyleDisplayName": task.data[
                        "visualStyleDisplayName"
                    ],
                    "visualStylePromptRecipeSha256": task.data[
                        "visualStylePromptRecipeSha256"
                    ],
                }
            )

    result = validate_agent_result(
        context.result_json,
        task,
        dispatched_task_sha256=task.task_sha256,
        output_validator=validate_output,
    )
    if not candidate_summary:
        raise ValueError("result 未包含已校验的 content draft candidate")
    return {
        "schemaVersion": 1,
        "operation": "validate-draft-result",
        "status": "PASS",
        "taskId": task.data["taskId"],
        "attempt": task.data["attempt"],
        "resultStatus": result.data["status"],
        "resultSha256": sha256_file(context.result_json),
        **candidate_summary,
        "validationDurationMs": round((time.perf_counter() - started) * 1000),
        "formalWritesPerformed": False,
        "approvalWritten": False,
    }


def parse_initial(args: argparse.Namespace) -> dict[str, Any]:
    project = load_project(
        args.project,
        allow_pending_audio_timeline=True,
        allow_pending_initial_approval=True,
    )
    options = build_initial_approval_options(
        voiceover_mode=project.voiceover_mode,
        gpt_login_image_generation_available=args.gpt_login_image_generation_available,
        configured_image_provider_available=args.configured_image_provider_available,
        fixed_image_generation_mode=args.fixed_image_generation_mode,
    )
    selection = parse_initial_approval_response(
        args.reply,
        options=options,
        content_identity_sha256=project.current_content_identity_sha256,
    )
    output = args.output.resolve(strict=False)
    try:
        relative = output.relative_to(project.root.resolve(strict=True))
    except ValueError as exc:
        raise ValueError("selection output 必须位于 current project") from exc
    if not relative.parts or relative.parts[0] != ".work" or output.suffix.lower() != ".json":
        raise ValueError("selection output 必须是 project/.work 下的 JSON")
    if output.exists():
        raise ValueError("selection output 已存在；不得覆盖旧选择证据")
    output.parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(output, selection)
    return {
        "schemaVersion": 1,
        "operation": "parse-initial-approval",
        "status": "PASS",
        "projectId": project.project_id,
        "selectionFile": str(output.resolve(strict=True)),
        "selectionSha256": sha256_file(output),
        "readyForAtomicApproval": selection["readyForAtomicApproval"],
        "matchedBy": selection["matchedBy"],
        "approvalWritten": False,
    }


def project_status(project_path: Path) -> dict[str, Any]:
    project = load_project(
        project_path,
        allow_pending_audio_timeline=True,
        allow_pending_initial_approval=True,
    )
    skill_root = Path(__file__).resolve().parents[1]
    python = sys.executable
    if project.pending_initial_approval:
        next_gate = "initial_content_plan_approval"
        next_command = None
    elif project.voiceover_mode != "disabled":
        if not project.path("audio/timeline.json").is_file():
            next_gate = "full_voiceover_alignment"
            next_command = None
            if not (
                project.voiceover_mode == "doubao"
                and not project.path("planning/voice-plan.json").is_file()
            ):
                next_command = [
                    python,
                    str(skill_root / "scripts/generate_voiceover.py"),
                    "full",
                    "--project",
                    str(project.root),
                ]
        else:
            manifest = _read_json(project.path("manifests/voice-manifest.json"))
            full_approval = manifest.get("fullApproval")
            full_identity = manifest.get("fullIdentityHash")
            if (
                not isinstance(full_approval, dict)
                or full_approval.get("approved") is not True
                or not isinstance(full_identity, str)
                or full_approval.get("identityHash") != full_identity
            ):
                next_gate = "full_voiceover_approval"
                next_command = None
            else:
                next_gate = "current_phase_review"
                next_command = [python, str(skill_root / "scripts/run_phase.py"), "--project", str(project.root), "--phase", "annotation-preview"]
    else:
        next_gate = "current_phase_review"
        next_command = [python, str(skill_root / "scripts/run_phase.py"), "--project", str(project.root), "--phase", "annotation-preview"]
    return {
        "schemaVersion": 1,
        "operation": "project-status",
        "status": "PASS",
        "projectId": project.project_id,
        "pendingInitialApproval": project.pending_initial_approval,
        "voiceoverMode": project.voiceover_mode,
        "nextGate": next_gate,
        "nextCommandArgv": next_command,
        "nextCommand": subprocess.list2cmdline(next_command) if next_command else None,
        "readOnly": True,
        "approvalWritten": False,
    }


def _recommendation_content(args: argparse.Namespace) -> tuple[str, str]:
    """只读提取推荐器输入；推荐发生在 attempt 和具体模板落盘之前。"""
    if args.content_input is not None:
        value = _read_json(args.content_input)
        mode = value.get("inputMode")
        if mode == "topic":
            content = value.get("topic")
        elif mode == "text":
            content = value.get("body")
        else:
            raise ValueError("--content-input 的 inputMode 只允许 topic 或 text")
        if not isinstance(content, str) or not content.strip():
            raise ValueError("--content-input 缺少可用于模板推荐的非空内容")
        return "content-input", content.strip()

    try:
        from .srt_timeline import parse_srt
    except ImportError:  # pragma: no cover - direct script execution
        from srt_timeline import parse_srt  # type: ignore

    try:
        source_text = args.source_srt.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as exc:
        raise ValueError("--source-srt 必须是可读的 UTF-8 SRT") from exc
    cues = parse_srt(source_text)
    content = "\n".join(str(cue["text"]).strip() for cue in cues).strip()
    if not content:
        raise ValueError("--source-srt 没有可用于模板推荐的字幕内容")
    return "source-srt", content


def recommend_visual_style(args: argparse.Namespace) -> dict[str, Any]:
    """返回最多三个确定性候选；不创建 attempt、项目或其他持久化状态。"""
    input_kind, content = _recommendation_content(args)
    try:
        from .visual_style_presets import get_visual_style_preset
        from .visual_style_recommendation import recommend_visual_style_presets
    except ImportError:  # pragma: no cover - direct script execution
        from visual_style_presets import get_visual_style_preset  # type: ignore
        from visual_style_recommendation import (  # type: ignore
            recommend_visual_style_presets,
        )

    if args.visual_style_preset is not None:
        selected = get_visual_style_preset(args.visual_style_preset)
        recommendations: list[dict[str, Any]] = [
            {
                "presetId": selected.id,
                "displayName": selected.display_name,
                "rationale": "用户已明确指定具体模板，优先于 AI 推荐。",
                "score": None,
                "evidence": ["user_explicit_selection"],
            }
        ]
        selection_basis = "user_explicit"
    else:
        ranked = recommend_visual_style_presets(content, limit=3)
        if not ranked:
            raise ValueError("模板推荐器没有返回候选")
        recommendations = [item.to_dict() for item in ranked]
        selection_basis = "deterministic_recommendation"

    recommended = recommendations[0]
    return {
        "schemaVersion": 1,
        "operation": "recommend-visual-style",
        "status": "PASS",
        "inputKind": input_kind,
        "selectionBasis": selection_basis,
        "recommendedVisualStylePreset": recommended["presetId"],
        "recommendedDisplayName": recommended["displayName"],
        "rationale": recommended["rationale"],
        "recommendations": recommendations,
        "attemptCreated": False,
        "projectModified": False,
        "readOnly": True,
        "approvalWritten": False,
    }


def visual_style_catalog(args: argparse.Namespace) -> dict[str, Any]:
    """列出模板；仅显式 --output 时生成确定性 Markdown 目录。"""
    try:
        from .render_visual_style_catalog import create_catalog
        from .visual_style_presets import (
            DEFAULT_VISUAL_STYLE_PRESET_ID,
            list_visual_style_presets,
        )
    except ImportError:  # pragma: no cover - direct script execution
        from render_visual_style_catalog import create_catalog  # type: ignore
        from visual_style_presets import (  # type: ignore
            DEFAULT_VISUAL_STYLE_PRESET_ID,
            list_visual_style_presets,
        )

    presets = list_visual_style_presets()
    if args.output is not None:
        resolved_output = args.output.resolve(strict=False)
        if any((parent / "project.json").is_file() for parent in resolved_output.parents):
            raise ValueError("模板目录不得写入项目目录；它只用于建项前选型")
        catalog_result = create_catalog(resolved_output)
    else:
        catalog_result = None
    return {
        "schemaVersion": 1,
        "operation": "visual-style-catalog",
        "status": "PASS",
        "defaultVisualStylePreset": DEFAULT_VISUAL_STYLE_PRESET_ID,
        "templateCount": len(presets),
        "templates": [
            {
                "presetId": preset.id,
                "displayName": preset.display_name,
                "recommendedFor": list(preset.recommended_for),
                "previewAsset": preset.preview_asset,
                "rendererCompatibility": preset.renderer_compatibility,
            }
            for preset in presets
        ],
        "catalogGenerated": catalog_result is not None,
        "catalogFile": catalog_result["output"] if catalog_result else None,
        "catalogSha256": catalog_result["catalogSha"] if catalog_result else None,
        "attemptCreated": False,
        "projectModified": False,
        "approvalWritten": False,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="白板流程 coordinator 的精简公开 CLI")
    sub = parser.add_subparsers(dest="command", required=True)
    initial = sub.add_parser("parse-initial-approval")
    initial.add_argument("--project", required=True, type=Path)
    initial.add_argument("--reply", required=True)
    initial.add_argument("--output", required=True, type=Path)
    initial.add_argument("--gpt-login-image-generation-available", action="store_true")
    initial.add_argument("--configured-image-provider-available", action="store_true")
    initial.add_argument("--fixed-image-generation-mode", choices=("provider", "gpt-login"))
    draft = sub.add_parser("validate-draft-result")
    draft.add_argument("--task", required=True, type=Path)
    candidate = sub.add_parser(
        "validate-agent-candidate",
        help="只读校验 content/storyboard candidate 或 visualReview findings",
    )
    candidate.add_argument("--task", required=True, type=Path)
    candidate.add_argument("--dispatched-task-sha256", required=True)
    materialize = sub.add_parser(
        "materialize-agent-result",
        help="从已校验 candidate 确定性写 result.json 并重验",
    )
    materialize.add_argument("--task", required=True, type=Path)
    materialize.add_argument("--dispatched-task-sha256", required=True)
    finalize = sub.add_parser(
        "finalize-content-draft",
        help="一次校验/materialize content candidate 并创建待联合批准预项目",
    )
    finalize.add_argument("--task", required=True, type=Path)
    finalize.add_argument("--dispatched-task-sha256", required=True)
    finalize.add_argument(
        "--workspace-config",
        type=Path,
        help="工作区配置；省略时使用 config/workspace.local.json",
    )
    finalize.add_argument(
        "--gpt-login-image-generation-available",
        action="store_true",
    )
    finalize_provider = finalize.add_mutually_exclusive_group()
    finalize_provider.add_argument(
        "--configured-image-provider-available",
        dest="configured_image_provider_available",
        action="store_true",
    )
    finalize_provider.add_argument(
        "--configured-image-provider-unavailable",
        dest="configured_image_provider_available",
        action="store_false",
    )
    finalize.set_defaults(configured_image_provider_available=True)
    finalize.add_argument(
        "--fixed-image-generation-mode",
        choices=("provider", "gpt-login"),
    )
    status = sub.add_parser("project-status")
    status.add_argument("--project", required=True, type=Path)
    recommend = sub.add_parser(
        "recommend-visual-style",
        help="从 topic/text 输入或传统 SRT 只读推荐最多三个具体模板",
    )
    recommend_input = recommend.add_mutually_exclusive_group(required=True)
    recommend_input.add_argument("--content-input", type=Path)
    recommend_input.add_argument("--source-srt", type=Path)
    recommend.add_argument(
        "--visual-style-preset",
        help="可选的用户明确选择；必须是具体模板 ID，优先于推荐结果且不能是 auto",
    )
    catalog = sub.add_parser(
        "visual-style-catalog",
        help="列出视觉模板；可选生成确定性 Markdown 目录",
    )
    catalog.add_argument(
        "--output",
        type=Path,
        help="可选的 Markdown 输出路径；省略时仅输出模板列表且不写文件",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    configure_utf8_stdio()
    args = build_parser().parse_args(argv)
    try:
        if args.command == "parse-initial-approval":
            summary = parse_initial(args)
        elif args.command == "validate-draft-result":
            summary = validate_draft_result(args.task)
        elif args.command == "validate-agent-candidate":
            summary = validate_agent_candidate(
                args.task,
                args.dispatched_task_sha256,
            )
        elif args.command == "materialize-agent-result":
            summary = materialize_agent_result(
                args.task,
                args.dispatched_task_sha256,
            )
        elif args.command == "finalize-content-draft":
            summary = finalize_content_draft(args)
        elif args.command == "recommend-visual-style":
            summary = recommend_visual_style(args)
        elif args.command == "visual-style-catalog":
            summary = visual_style_catalog(args)
        else:
            summary = project_status(args.project)
        code = 0
    except Exception as exc:
        summary = {
            "schemaVersion": 1,
            "operation": args.command,
            "status": "FAIL",
            "error": str(exc),
            "approvalWritten": False,
        }
        code = 2
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "finalize_content_draft",
    "main",
    "materialize_agent_result",
    "project_status",
    "recommend_visual_style",
    "validate_draft_result",
    "validate_agent_candidate",
    "visual_style_catalog",
]
