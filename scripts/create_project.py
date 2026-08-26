#!/usr/bin/env python3
"""在已配置的 D 盘工作区创建或显式续接 SRT 白板动画项目。"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from project_workspace import (
    ProjectValidationError,
    ProjectWorkspace,
    WorkspaceError,
    validate_pre_project_generation_plan_data,
)
from voice_provider_config import VoiceProviderConfigError, active_provider_id


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "在第 1 步配图策略已获用户确认后创建 D 盘项目。未提供 --plan 时，"
            "创建固定画布与约束、scenes 为空的有效计划骨架。"
        )
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--name", help="新项目名；Windows 禁止字符会替换为 '-' ")
    mode.add_argument("--resume", type=Path, help="显式续接的已有项目绝对路径")
    parser.add_argument("--srt", required=True, type=Path, help="原始 SRT 路径")
    parser.add_argument(
        "--plan",
        type=Path,
        help="可选：已确认配图策略 JSON；新建时写入并严格校验",
    )
    parser.add_argument(
        "--voiceover-mode",
        choices=("disabled",),
        help="显式创建静音项目；省略时唯一使用 config/voice-providers.local.json 的 activeProvider",
    )
    parser.add_argument(
        "--background-music",
        choices=("enabled", "disabled"),
        default="disabled",
        help="阶段 0 已确认的 BGM 选择；默认 disabled 仅用于旧调用兼容",
    )
    parser.add_argument(
        "--agent-approval",
        choices=("enabled", "disabled"),
        help="阶段 0 已确认的后续批准主体；新建时省略按 disabled 兼容旧调用",
    )
    parser.add_argument(
        "--image-generation-mode",
        choices=("provider", "gpt-login"),
        help="阶段 0 已确认的生图方式；新建时省略按 provider 兼容旧调用",
    )
    parser.add_argument(
        "--source-input",
        type=Path,
        help="可选：content source 准备包中的 input.json；必须与 --source-manifest 成对使用",
    )
    parser.add_argument(
        "--source-manifest",
        type=Path,
        help="可选：content source 准备包中的 manifest.json；必须与 --source-input 成对使用",
    )
    return parser


def _load_confirmed_plan(
    path: Path | None,
    *,
    source_srt: Path,
    voiceover_mode: str,
) -> dict | None:
    if path is None:
        return None
    if not path.is_file():
        raise ProjectValidationError(f"已确认策略文件不存在: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ProjectValidationError(f"无法读取已确认策略: {exc}") from exc
    if not isinstance(value, dict):
        raise ProjectValidationError("已确认策略顶层必须是 JSON 对象")
    return validate_pre_project_generation_plan_data(
        value,
        source_srt_path=source_srt,
        voiceover_mode=voiceover_mode,
    )


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        workspace = ProjectWorkspace.from_config()
        has_source_input = args.source_input is not None
        has_source_manifest = args.source_manifest is not None
        if has_source_input != has_source_manifest:
            raise ProjectValidationError("--source-input 与 --source-manifest 必须同时出现")
        if args.resume is not None:
            if args.plan is not None:
                raise ProjectValidationError("--plan 仅用于创建新项目，续接时校验项目内现有计划")
            if args.voiceover_mode is not None:
                raise ProjectValidationError("--voiceover-mode 仅用于创建新项目，续接时读取已冻结模式")
            if args.background_music != "disabled":
                raise ProjectValidationError("--background-music 仅用于创建新项目，续接时读取已冻结选择")
            if args.agent_approval is not None:
                raise ProjectValidationError("--agent-approval 仅用于创建新项目，续接时读取已冻结选择")
            if args.image_generation_mode is not None:
                raise ProjectValidationError(
                    "--image-generation-mode 仅用于创建新项目，续接时读取已冻结选择"
                )
            if has_source_input:
                raise ProjectValidationError("content source 证据仅用于新建项目，续接时读取项目内冻结证据")
            project = workspace.resume_project(args.resume, args.srt)
        else:
            if has_source_input and args.plan is None:
                raise ProjectValidationError("content source 项目必须显式提供准备包中的 --plan")
            # Provider selection has one source of truth for the CLI.  The
            # explicit disabled mode is only the silent-project escape hatch.
            voiceover_mode = args.voiceover_mode or active_provider_id()
            project = workspace.create_project(
                args.name,
                args.srt,
                confirmed_plan=_load_confirmed_plan(
                    args.plan,
                    source_srt=args.srt,
                    voiceover_mode=voiceover_mode,
                ),
                voiceover_mode=voiceover_mode,
                background_music_enabled=args.background_music == "enabled",
                agent_approval_enabled=args.agent_approval == "enabled",
                image_generation_mode=args.image_generation_mode or "provider",
                source_input=args.source_input,
                source_manifest=args.source_manifest,
                source_plan=args.plan if has_source_input else None,
            )
    except (WorkspaceError, ProjectValidationError, VoiceProviderConfigError, OSError) as exc:
        print(f"[err] {exc}", file=sys.stderr)
        return 2

    print(f"PROJECT_ROOT={project.root}")
    print(f"PLAN_PATH={project.plan_path}")
    print(f"TIMING_PLAN_PATH={project.timing_plan_path}")
    print(f"VOICEOVER_MODE={project.voiceover_mode}")
    print(f"BACKGROUND_MUSIC={'enabled' if project.background_music_enabled else 'disabled'}")
    print(f"AGENT_APPROVAL={'enabled' if project.agent_approval_enabled else 'disabled'}")
    print(f"IMAGE_GENERATION_MODE={project.image_generation_mode}")
    print(f"SCENES_DIR={project.scenes_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
