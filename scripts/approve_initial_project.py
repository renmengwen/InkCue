#!/usr/bin/env python3
"""原子完成预项目的内容/样音联合批准并提升为正式项目。"""
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
from typing import Any, Callable, Mapping

from initial_approval_options import (
    CHOICE_CONTRACT_VERSION,
    InitialApprovalOptionError,
    build_initial_approval_options,
)
from project_workspace import (
    AUDIO_VOICEOVER_MODES,
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


def _validate_current_sample(project: Project) -> tuple[dict[str, Any], str, bool]:
    # 延迟导入，避免 project_workspace 的共享加载器形成模块加载环。
    from generate_voiceover import validate_current_voiceover

    result = validate_current_voiceover(project, require_full=False)
    current_identity = result.get("sampleIdentityHash")
    if not _is_sha256(current_identity):
        raise InitialApprovalError("current 样音 identity 无效")
    manifest = _read_json(
        project.path("manifests/voice-manifest.json"),
        "voice manifest",
    )
    approval = manifest.get("sample", {}).get("approval")
    if not isinstance(approval, dict):
        raise InitialApprovalError("预项目样音批准结构无效")
    if approval.get("approved") is False:
        return manifest, current_identity, False
    if (
        approval.get("approved") is True
        and approval.get("identityHash") == current_identity
        and approval.get("approvalBasis") == "user_joint_initial_approval"
        and isinstance(approval.get("approvedAt"), str)
        and approval["approvedAt"]
    ):
        # project.json 仍为 pending 时，这只能是上次进程在提交点前退出留下的
        # 联合批准准备态；默认 loader 仍会阻止所有下游。identity 完整匹配时
        # 本次调用可直接完成 project.json 提交点。
        return manifest, current_identity, True
    raise InitialApprovalError("pending 项目存在非 current 或非联合 basis 的样音批准")


_SELECTION_FIELDS = {
    "contractVersion",
    "choiceId",
    "optionNumber",
    "action",
    "contentApproved",
    "sampleApproved",
    "sampleApprovalRequired",
    "backgroundMusicEnabled",
    "agentApprovalEnabled",
    "imageGenerationMode",
    "contentIdentitySha256",
    "sampleIdentitySha256",
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
        "sampleApproved": "sampleApproved",
        "sampleApprovalRequired": "sampleApprovalRequired",
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
    sample_loader: Callable[[Project], tuple[dict[str, Any], str, bool]],
) -> tuple[dict[str, Any] | None, str | None, bool]:
    _match_current_option(
        project,
        selection,
        gpt_login_image_generation_available=(
            gpt_login_image_generation_available
        ),
        configured_image_provider_available=configured_image_provider_available,
        fixed_image_generation_mode=fixed_image_generation_mode,
    )
    if selection.get("contractVersion") != CHOICE_CONTRACT_VERSION:
        raise InitialApprovalError("联合选择 contractVersion 无效")
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

    sample_required = project.voiceover_mode in AUDIO_VOICEOVER_MODES
    if selection.get("sampleApprovalRequired") is not sample_required:
        raise InitialApprovalError("选择的样音要求与 current 项目模式不一致")
    if selection.get("sampleApproved") is not sample_required:
        raise InitialApprovalError("联合选择未按 current 项目模式批准样音")
    if selection["backgroundMusicEnabled"] and not sample_required:
        raise InitialApprovalError("静音项目不能启用 BGM")

    if not sample_required:
        if selection.get("sampleIdentitySha256") not in (None, ""):
            raise InitialApprovalError("静音项目不得绑定样音 identity")
        return None, None, False

    supplied_sample_identity = selection.get("sampleIdentitySha256")
    if not _is_sha256(supplied_sample_identity):
        raise InitialApprovalError("旁白项目选择缺少有效 sampleIdentitySha256")
    manifest, current_sample_identity, sample_prepared = sample_loader(project)
    if supplied_sample_identity != current_sample_identity:
        raise InitialApprovalError("选择绑定的样音 identity 已 stale")
    approval = manifest.get("sample", {}).get("approval")
    if not isinstance(approval, Mapping):
        raise InitialApprovalError("current 样音批准结构无效")
    if sample_prepared:
        if (
            approval.get("approved") is not True
            or approval.get("identityHash") != current_sample_identity
            or approval.get("approvalBasis") != "user_joint_initial_approval"
            or not isinstance(approval.get("approvedAt"), str)
            or not approval["approvedAt"]
        ):
            raise InitialApprovalError("联合批准准备态未绑定 current 样音")
    elif approval.get("approved") is not False:
        raise InitialApprovalError("预项目样音必须尚未批准")
    return manifest, current_sample_identity, sample_prepared


def approve_initial_project(
    project_root: str | Path,
    selection: Mapping[str, Any],
    *,
    gpt_login_image_generation_available: bool = False,
    configured_image_provider_available: bool = False,
    fixed_image_generation_mode: str | None = None,
    sample_loader: Callable[
        [Project], tuple[dict[str, Any], str, bool]
    ] = _validate_current_sample,
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

    manifest, sample_identity, sample_prepared = _validate_selection(
        project,
        selection,
        gpt_login_image_generation_available=(
            gpt_login_image_generation_available
        ),
        configured_image_provider_available=configured_image_provider_available,
        fixed_image_generation_mode=fixed_image_generation_mode,
        sample_loader=sample_loader,
    )
    approved_at = (
        str(manifest["sample"]["approval"]["approvedAt"])
        if sample_prepared and manifest is not None
        else _now()
    )
    metadata = copy.deepcopy(project.metadata)
    metadata.update(
        {
            "backgroundMusic": {"enabled": selection["backgroundMusicEnabled"]},
            "agentApprovalEnabled": selection["agentApprovalEnabled"],
            "imageGenerationMode": selection["imageGenerationMode"],
            "initialApproval": {
                "status": INITIAL_APPROVAL_APPROVED,
                "contentIdentitySha256": project.current_content_identity_sha256,
                "sampleIdentityHash": sample_identity,
                "approvalBasis": (
                    "user_joint_content_and_sample"
                    if sample_identity is not None
                    else "user_joint_silent_plan"
                ),
                "approvedAt": approved_at,
            },
        }
    )
    validate_project_metadata_data(project.root, metadata)

    updated_manifest = None
    manifest_path = project.path("manifests/voice-manifest.json")
    if manifest is not None and not sample_prepared:
        updated_manifest = copy.deepcopy(manifest)
        updated_manifest["sample"]["approval"] = {
            "approved": True,
            "identityHash": sample_identity,
            "approvalBasis": "user_joint_initial_approval",
            "approvedAt": approved_at,
        }

    project_path = project.path("project.json")
    project_before = project_path.read_bytes()
    manifest_before = manifest_path.read_bytes() if manifest is not None else None
    run_dir = project.path(".work") / f"initial-approval-{uuid.uuid4().hex}"
    run_dir.mkdir(parents=True, exist_ok=False)
    project_candidate = run_dir / "project.json.candidate"
    manifest_candidate = run_dir / "voice-manifest.json.candidate"
    try:
        _write_candidate(project_candidate, metadata)
        if updated_manifest is not None:
            _write_candidate(manifest_candidate, updated_manifest)
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
        if (
            manifest_before is not None
            and manifest_path.read_bytes() != manifest_before
        ):
            raise InitialApprovalError("提交前 current 样音状态已 stale")
        # project.json 是提交点：先写样音批准，最后移除 pending marker。
        if updated_manifest is not None:
            os.replace(manifest_candidate, manifest_path)
        os.replace(project_candidate, project_path)
        committed = load_project(project.root)
    except Exception as exc:
        rollback_errors: list[str] = []
        for target, payload in (
            (project_path, project_before),
            (manifest_path, manifest_before),
        ):
            if payload is None:
                continue
            try:
                _restore_bytes_atomic(target, payload)
            except OSError as rollback_exc:
                rollback_errors.append(f"{target.name}: {rollback_exc}")
        if rollback_errors:
            raise InitialApprovalError(
                "联合批准提交失败且回滚不完整: " + "; ".join(rollback_errors)
            ) from exc
        if isinstance(exc, ProjectValidationError):
            raise
        raise InitialApprovalError("联合批准提交失败，已恢复 pending 状态") from exc
    finally:
        for candidate in (project_candidate, manifest_candidate):
            candidate.unlink(missing_ok=True)
        try:
            run_dir.rmdir()
        except OSError:
            pass
    return committed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="使用已绑定 current identities 的结构化选择原子提升预项目",
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
    if approval["sampleIdentityHash"] is not None:
        print(f"SAMPLE_APPROVED_IDENTITY={approval['sampleIdentityHash']}")
    print(
        "AGENT_APPROVAL="
        + ("enabled" if project.agent_approval_enabled else "disabled")
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
