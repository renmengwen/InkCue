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
        TrustedTaskContext,
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
    from .project_workspace import load_project, write_json_atomic
except ImportError:  # pragma: no cover - direct script execution
    from agent_task_contract import (  # type: ignore
        TrustedTaskContext,
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
    from project_workspace import load_project, write_json_atomic  # type: ignore


CONTRACT = "whiteboard-coordinator-cli-v1"
_ATTEMPT_RE = re.compile(r"attempt-(\d+)")


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
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
    if scope_root.parent.name != "drafts":
        raise ValueError("draft task scope 必须位于 workspace/drafts/<draft-id>")
    return TrustedTaskContext(
        workspace_root=scope_root.parent.parent,
        scope_root=scope_root,
        scope_kind="draft",
        run_id=run_dir.name,
        task_id=task_id,
        attempt=int(match.group(1)),
    )


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
            candidate_summary.update(
                {
                    "contentDraftIdentitySha256": content_draft_identity(draft),
                    "cueCount": len(draft["narrationCues"]),
                    "sceneCount": len(draft["scenes"]),
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
        "contractVersion": CONTRACT,
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
    sample_identity = None
    if project.voiceover_mode != "disabled":
        manifest = _read_json(project.path("manifests/voice-manifest.json"))
        sample = manifest.get("sample")
        if not isinstance(sample, dict) or not isinstance(sample.get("identityHash"), str):
            raise ValueError("current 样音 identity 不可用")
        sample_identity = sample["identityHash"]
    selection = parse_initial_approval_response(
        args.reply,
        options=options,
        content_identity_sha256=project.current_content_identity_sha256,
        sample_identity_sha256=sample_identity,
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
        "contractVersion": CONTRACT,
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
        next_gate = "initial_joint_approval"
        next_command = None
        if project.voiceover_mode != "disabled" and not project.path("previews/voice-sample.wav").is_file():
            next_gate = "sample_generation"
            next_command = [python, str(skill_root / "scripts/generate_voiceover.py"), "sample", "--project", str(project.root)]
    elif project.voiceover_mode != "disabled":
        if not project.path("audio/timeline.json").is_file():
            next_gate = "full_voiceover_alignment"
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
        "contractVersion": CONTRACT,
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
    status = sub.add_parser("project-status")
    status.add_argument("--project", required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    configure_utf8_stdio()
    args = build_parser().parse_args(argv)
    try:
        if args.command == "parse-initial-approval":
            summary = parse_initial(args)
        elif args.command == "validate-draft-result":
            summary = validate_draft_result(args.task)
        else:
            summary = project_status(args.project)
        code = 0
    except Exception as exc:
        summary = {
            "contractVersion": CONTRACT,
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


__all__ = ["CONTRACT", "main", "project_status", "validate_draft_result"]
