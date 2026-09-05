#!/usr/bin/env python3
"""将已获用户确认的 content draft 原子派生为 source 准备包。"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import uuid
from pathlib import Path

from content_source import (
    ContentSourceError,
    SourcePackage,
    build_source_package,
    validate_source_package,
)


PACKAGE_FILES = ("input.json", "source.srt", "generation-plan.json", "manifest.json")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "校验已由用户明确确认的 schemaVersion=1 content draft，并确定性输出 input.json、"
            "source.srt、generation-plan.json 与 manifest.json；本命令不会调用模型或批准草案。"
        )
    )
    parser.add_argument("--draft", required=True, type=Path, help="已获用户确认的 content draft JSON")
    parser.add_argument("--output-dir", required=True, type=Path, help="source 准备包输出目录")
    return parser


def _write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")


def _load_draft(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ContentSourceError(f"content draft 不存在: {path}") from exc
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ContentSourceError(f"无法读取 content draft: {exc}") from exc
    if not isinstance(value, dict):
        raise ContentSourceError("content draft 顶层必须是 JSON 对象")
    return value


def _validate_existing_output(output_dir: Path) -> None:
    if not output_dir.exists():
        return
    if not output_dir.is_dir():
        raise ContentSourceError(f"--output-dir 已存在且不是目录: {output_dir}")
    names = {item.name for item in output_dir.iterdir()}
    if names != set(PACKAGE_FILES):
        raise ContentSourceError("拒绝覆盖不是完整 source 准备包的现有目录")
    validate_source_package(
        output_dir / "input.json",
        output_dir / "manifest.json",
        output_dir / "source.srt",
        output_dir / "generation-plan.json",
    )


def prepare_source(draft_path: str | Path, output_dir: str | Path) -> SourcePackage:
    draft_file = Path(draft_path).resolve(strict=False)
    target = Path(output_dir).resolve(strict=False)
    if target == target.parent:
        raise ContentSourceError("--output-dir 不得是文件系统根目录")
    target.parent.mkdir(parents=True, exist_ok=True)
    _validate_existing_output(target)

    normalised, source_srt, generation_plan, manifest = build_source_package(
        _load_draft(draft_file)
    )
    staging = target.parent / f".{target.name}.prepare-{uuid.uuid4().hex}"
    backup = target.parent / f".{target.name}.backup-{uuid.uuid4().hex}"
    staging.mkdir(parents=False, exist_ok=False)
    old_moved = False
    committed = False
    try:
        _write_json(staging / "input.json", normalised)
        (staging / "source.srt").write_text(source_srt, encoding="utf-8", newline="\n")
        _write_json(staging / "generation-plan.json", generation_plan)
        _write_json(staging / "manifest.json", manifest)
        validate_source_package(
            staging / "input.json",
            staging / "manifest.json",
            staging / "source.srt",
            staging / "generation-plan.json",
        )
        if target.exists():
            os.replace(target, backup)
            old_moved = True
        os.replace(staging, target)
        committed = True
        package = validate_source_package(
            target / "input.json",
            target / "manifest.json",
            target / "source.srt",
            target / "generation-plan.json",
        )
        if old_moved:
            shutil.rmtree(backup, ignore_errors=True)
        return package
    except Exception:
        if old_moved and backup.exists():
            if committed and target.exists():
                failed = target.parent / f".{target.name}.failed-{uuid.uuid4().hex}"
                os.replace(target, failed)
                os.replace(backup, target)
                shutil.rmtree(failed, ignore_errors=True)
            elif not target.exists():
                os.replace(backup, target)
        elif committed and target.exists():
            shutil.rmtree(target)
        raise
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        package = prepare_source(args.draft, args.output_dir)
    except (ContentSourceError, OSError) as exc:
        print(f"[err] {exc}", file=sys.stderr)
        return 2
    print(f"CONTENT_DRAFT_IDENTITY={package.content_draft_identity}")
    print(f"SOURCE_INPUT={package.directory / 'input.json'}")
    print(f"SOURCE_SRT={package.directory / 'source.srt'}")
    print(f"GENERATION_PLAN={package.directory / 'generation-plan.json'}")
    print(f"SOURCE_MANIFEST={package.directory / 'manifest.json'}")
    print(f"INPUT_MODE={package.draft['inputMode']}")
    print(f"VISUAL_STYLE_PRESET={package.draft['visualStylePreset']}")
    print(f"TARGET_DURATION_SECONDS={package.draft['targetDurationSeconds']}")
    print(f"CUE_COUNT={len(package.draft['narrationCues'])}")
    print(f"SCENE_COUNT={len(package.draft['scenes'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
