#!/usr/bin/env python3
"""显式、原子地把 v1 白板项目升级为 schema v2。"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from project_workspace import ProjectValidationError, ProjectWorkspace, WorkspaceError
from voice_provider_config import VoiceProviderConfigError, active_provider_id


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "将 v1 项目显式升级为 v2。先发布独立 timing plan，最后以 project.json "
            "作为原子提交点；不会改写 generation plan。"
        )
    )
    parser.add_argument("--project", required=True, type=Path, help="工作区 projects 下的项目根目录")
    parser.add_argument("--to-schema", required=True, type=int, choices=(2,))
    parser.add_argument(
        "--voiceover-mode",
        choices=("disabled",),
        help="显式升级为静音项目；省略时唯一使用 activeProvider",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        workspace = ProjectWorkspace.from_config()
        project = workspace.upgrade_project(
            args.project,
            to_schema=args.to_schema,
            voiceover_mode=args.voiceover_mode or active_provider_id(),
        )
    except (WorkspaceError, ProjectValidationError, VoiceProviderConfigError, OSError) as exc:
        print(f"[err] {exc}", file=sys.stderr)
        return 2

    print(f"PROJECT_ROOT={project.root}")
    print(f"SCHEMA_VERSION={project.schema_version}")
    print(f"VOICEOVER_MODE={project.voiceover_mode}")
    print(f"TIMING_PLAN_PATH={project.timing_plan_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
