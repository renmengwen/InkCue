#!/usr/bin/env python3
"""原子完成预项目的内容与制作方案批准并提升为正式项目。"""
from __future__ import annotations

import argparse
import copy
import json
import os
import re
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from initial_approval_options import (
    InitialApprovalOptionError,
    build_initial_approval_options,
)
from project_workspace import (
    IMAGE_GENERATION_MODES,
    INITIAL_APPROVAL_APPROVED,
    Project,
    ProjectValidationError,
    load_project,
    validate_project_metadata_data,
)


class InitialApprovalError(ProjectValidationError):
    """联合选择、current identity 或 pending 状态不允许提交。"""


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise InitialApprovalError(f"无法读取{label}") from exc
    if not isinstance(value, dict):
        raise InitialApprovalError(f"{label}顶层必须是对象")
    return value


def _write_candidate(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _restore_bytes_atomic(path: Path, payload: bytes) -> None:
    candidate = path.with_name(f".{path.name}.{uuid.uuid4().hex}.rollback")
    try:
        with candidate.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(candidate, path)
    finally:
        candidate.unlink(missing_ok=True)


_SELECTION_FIELDS = {
    "schemaVersion",
    "choiceId",
    "optionNumber",
    "action",
    "contentApproved",
    "backgroundMusicEnabled",
    "agentApprovalEnabled",
    "imageGenerationMode",
    "contentIdentitySha256",
    "revisionInstructions",
    "requiresFeedback",
    "readyForAtomicApproval",
    "matchedBy",
    "selectedText",
}


def _match_current_option(
    project: Project,
    selection: Mapping[str, Any],
    *,
    gpt_login_image_generation_available: bool,
    configured_image_provider_available: bool,
    fixed_image_generation_mode: str | None,
) -> Mapping[str, Any]:
    if set(selection) != _SELECTION_FIELDS:
        raise InitialApprovalError("联合选择字段不符合 current choice allowlist")
    try:
        options = build_initial_approval_options(
            voiceover_mode=project.voiceover_mode,
            gpt_login_image_generation_available=(
                gpt_login_image_generation_available
            ),
            configured_image_provider_available=(
                configured_image_provider_available
            ),
            fixed_image_generation_mode=fixed_image_generation_mode,
        )
    except InitialApprovalOptionError as exc:
        raise InitialApprovalError(str(exc)) from exc
    option = next(
        (
            item
            for item in options
            if item.get("choiceId") == selection.get("choiceId")
        ),
        None,
    )
    if option is None or option.get("action") != "approve":
        raise InitialApprovalError("choiceId 不是当前能力下的合法通过选项")
    field_bindings = {
        "optionNumber": "number",
        "action": "action",
        "contentApproved": "contentApproved",
        "backgroundMusicEnabled": "backgroundMusicEnabled",
        "agentApprovalEnabled": "agentApprovalEnabled",
        "imageGenerationMode": "imageGenerationMode",
        "requiresFeedback": "requiresFeedback",
        "selectedText": "text",
    }
    for selection_field, option_field in field_bindings.items():
        if selection.get(selection_field) != option.get(option_field):
            raise InitialApprovalError(
                f"联合选择字段 {selection_field} 与 current option 不匹配"
            )
    if selection.get("matchedBy") not in {"number", "full_sentence"}:
        raise InitialApprovalError("通过选项 matchedBy 无效")
    return option


def _validate_selection(
    project: Project,
    selection: Mapping[str, Any],
    *,
    gpt_login_image_generation_available: bool,
    configured_image_provider_available: bool,
    fixed_image_generation_mode: str | None,
) -> None:
    _match_current_option(
        project,
        selection,
        gpt_login_image_generation_available=(
            gpt_login_image_generation_available
        ),
        configured_image_provider_available=configured_image_provider_available,
        fixed_image_generation_mode=fixed_image_generation_mode,
    )
    if selection.get("schemaVersion") != 2:
        raise InitialApprovalError("联合选择 schemaVersion 必须为 2")
    if selection.get("action") != "approve":
        raise InitialApprovalError("联合批准动作只接受通过选项")
    if selection.get("readyForAtomicApproval") is not True:
        raise InitialApprovalError("联合选择未标记为可原子批准")
    if selection.get("contentApproved") is not True:
        raise InitialApprovalError("联合批准必须明确批准 current 内容")
    if selection.get("requiresFeedback") not in (None, False):
        raise InitialApprovalError("需要修改的选项不能提升预项目")
    if selection.get("revisionInstructions") not in (None, ""):
        raise InitialApprovalError("通过选项不得携带修改意见")

    supplied_content_identity = selection.get("contentIdentitySha256")
    if not _is_sha256(supplied_content_identity):
        raise InitialApprovalError("选择缺少有效 contentIdentitySha256")
    if supplied_content_identity != project.current_content_identity_sha256:
        raise InitialApprovalError("选择绑定的内容 identity 已 stale")

    for field in ("backgroundMusicEnabled", "agentApprovalEnabled"):
        if not isinstance(selection.get(field), bool):
            raise InitialApprovalError(f"选择字段 {field} 必须是布尔值")
    image_mode = selection.get("imageGenerationMode")
    if image_mode not in IMAGE_GENERATION_MODES:
        raise InitialApprovalError("选择中的 imageGenerationMode 无效")

    if selection["backgroundMusicEnabled"] and project.voiceover_mode == "disabled":
        raise InitialApprovalError("静音项目不能启用 BGM")


def approve_initial_project(
    project_root: str | Path,
    selection: Mapping[str, Any],
    *,
    gpt_login_image_generation_available: bool = False,
    configured_image_provider_available: bool = False,
    fixed_image_generation_mode: str | None = None,
) -> Project:
    """重验并提交一次联合选择；校验失败或提交失败不保留半批准状态。"""
    if not isinstance(selection, Mapping):
        raise InitialApprovalError("联合选择必须是结构化对象")
    project = load_project(
        project_root,
        allow_pending_initial_approval=True,
    )
    if not project.pending_initial_approval:
        raise InitialApprovalError("项目已完成初始批准，不能重复联合批准")

    _validate_selection(
        project,
        selection,
        gpt_login_image_generation_available=(
            gpt_login_image_generation_available
        ),
        configured_image_provider_available=configured_image_provider_available,
        fixed_image_generation_mode=fixed_image_generation_mode,
    )
    approved_at = _now()
    metadata = copy.deepcopy(project.metadata)
    metadata.update(
        {
            "backgroundMusic": {"enabled": selection["backgroundMusicEnabled"]},
            "agentApprovalEnabled": selection["agentApprovalEnabled"],
            "imageGenerationMode": selection["imageGenerationMode"],
            "initialApproval": {
                "status": INITIAL_APPROVAL_APPROVED,
                "contentIdentitySha256": project.current_content_identity_sha256,
                "approvalBasis": "user_joint_content_and_plan",
                "approvedAt": approved_at,
            },
        }
    )
    validate_project_metadata_data(project.root, metadata)

    project_path = project.path("project.json")
    project_before = project_path.read_bytes()
    run_dir = project.path(".work") / f"initial-approval-{uuid.uuid4().hex}"
    run_dir.mkdir(parents=True, exist_ok=False)
    project_candidate = run_dir / "project.json.candidate"
    try:
        _write_candidate(project_candidate, metadata)
        rechecked = load_project(
            project.root,
            allow_pending_initial_approval=True,
        )
        if (
            project_path.read_bytes() != project_before
            or rechecked.current_content_identity_sha256
            != project.current_content_identity_sha256
        ):
            raise InitialApprovalError("提交前 project/content identity 已 stale")
        os.replace(project_candidate, project_path)
        committed = load_project(project.root)
    except Exception as exc:
        rollback_errors: list[str] = []
        try:
            _restore_bytes_atomic(project_path, project_before)
        except OSError as rollback_exc:
            rollback_errors.append(f"{project_path.name}: {rollback_exc}")
        if rollback_errors:
            raise InitialApprovalError(
                "联合批准提交失败且回滚不完整: " + "; ".join(rollback_errors)
            ) from exc
        if isinstance(exc, ProjectValidationError):
            raise
        raise InitialApprovalError("联合批准提交失败，已恢复 pending 状态") from exc
    finally:
        project_candidate.unlink(missing_ok=True)
        try:
            run_dir.rmdir()
        except OSError:
            pass
    return committed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="使用已绑定 current content identity 的结构化选择原子提升预项目",
    )
    parser.add_argument("--project", required=True, type=Path)
    parser.add_argument(
        "--selection",
        required=True,
        type=Path,
        help="initial_approval_options.parse_initial_approval_response 的结构化 JSON 输出",
    )
    parser.add_argument(
        "--configured-image-provider-available",
        action="store_true",
        help="仅当已配置图片供应商当前确实可用时传入",
    )
    parser.add_argument(
        "--fixed-image-generation-mode",
        choices=("provider", "gpt-login"),
        help="阶段 0 已固定且当前可用的唯一生图方式",
    )
    parser.add_argument(
        "--gpt-login-capable",
        action="store_true",
        help="仅当当前宿主登录态与 image_gen 能力已实际确认时传入",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        selection = _read_json(args.selection, "联合选择")
        project = approve_initial_project(
            args.project,
            selection,
            gpt_login_image_generation_available=args.gpt_login_capable,
            configured_image_provider_available=(
                args.configured_image_provider_available
            ),
            fixed_image_generation_mode=args.fixed_image_generation_mode,
        )
    except InitialApprovalError as exc:
        print(f"[err] {exc}", file=sys.stderr)
        return 5
    except (ProjectValidationError, OSError) as exc:
        print(f"[err] {exc}", file=sys.stderr)
        return 2

    approval = project.metadata["initialApproval"]
    print(f"PROJECT_ROOT={project.root}")
    print(f"INITIAL_APPROVAL={approval['status']}")
    print(f"CONTENT_IDENTITY={approval['contentIdentitySha256']}")
    print(
        "AGENT_APPROVAL="
        + ("enabled" if project.agent_approval_enabled else "disabled")
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
