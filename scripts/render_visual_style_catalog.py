#!/usr/bin/env python3
"""从视觉模板注册表确定性生成中文 Markdown 选型目录。

目录只用于帮助 coordinator/用户查看和选择模板；它不是批准文件、质量 Gate、
作品 identity 或 provider 输入，也不会展开完整 promptRecipe。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import uuid
from pathlib import Path
from urllib.parse import quote

try:  # direct CLI execution
    from visual_style_presets import (
        DEFAULT_VISUAL_STYLE_PRESET_ID,
        VisualStylePreset,
        list_visual_style_presets,
    )
except ImportError:  # imported as scripts.render_visual_style_catalog
    from scripts.visual_style_presets import (
        DEFAULT_VISUAL_STYLE_PRESET_ID,
        VisualStylePreset,
        list_visual_style_presets,
    )


class CatalogError(ValueError):
    """模板目录输入、资产或输出位置不可安全使用。"""


def _preview_asset_path(skill_root: Path, preset: VisualStylePreset) -> Path:
    candidate = (skill_root / preset.preview_asset).resolve(strict=True)
    try:
        candidate.relative_to(skill_root)
    except ValueError as exc:
        raise CatalogError("preview_asset_outside_skill") from exc
    if not candidate.is_file() or candidate.suffix.lower() != ".svg":
        raise CatalogError("preview_asset_invalid")
    return candidate


def _markdown_target(asset: Path, output_parent: Path) -> str:
    try:
        relative = os.path.relpath(asset, output_parent)
        target = Path(relative).as_posix()
    except ValueError:
        target = asset.as_posix()
    return quote(target, safe="/:._-")


def render_catalog_markdown(output: Path) -> str:
    """返回按注册表稳定顺序生成的 Markdown；不执行任何写入。"""

    skill_root = Path(__file__).resolve().parent.parent
    presets = list_visual_style_presets()
    lines = [
        "# 白板视觉模板目录",
        "",
        "> 本目录仅用于模板选型。模板会在草案或传统 SRT 分镜 attempt 创建前冻结；本文件不是质量 Gate、批准记录或作品 identity。",
        "",
    ]
    for index, preset in enumerate(presets, start=1):
        asset = _preview_asset_path(skill_root, preset)
        target = _markdown_target(asset, output.parent)
        default_note = "（默认兼容模板）" if preset.id == DEFAULT_VISUAL_STYLE_PRESET_ID else ""
        lines.extend(
            [
                f"## {index}. {preset.display_name}{default_note}",
                "",
                f"![{preset.display_name}预览]({target})",
                "",
                f"[打开本地 SVG 预览]({target})",
                "",
                f"- 模板 ID：`{preset.id}`",
                f"- 推荐内容：{'、'.join(preset.recommended_for)}",
                f"- 渲染兼容：`{preset.renderer_compatibility}`",
                f"- 配方 SHA：`{preset.recipe_sha256[:12]}`",
                "",
            ]
        )
    return "\n".join(lines)


def _write_atomic_once(path: Path, payload: bytes) -> None:
    if path.is_symlink():
        raise CatalogError("output_symlink_rejected")
    if path.exists():
        if not path.is_file():
            raise CatalogError("output_not_file")
        try:
            current = path.read_bytes()
        except OSError as exc:
            raise CatalogError("output_unreadable") from exc
        if current != payload:
            raise CatalogError("output_exists_with_different_content")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def create_catalog(output: Path) -> dict[str, object]:
    resolved = output.expanduser().resolve(strict=False)
    if resolved.suffix.lower() != ".md":
        raise CatalogError("output_must_be_markdown")
    markdown = render_catalog_markdown(resolved)
    payload = markdown.encode("utf-8")
    _write_atomic_once(resolved, payload)
    return {
        "status": "PASS",
        "output": str(resolved),
        "catalogSha": hashlib.sha256(payload).hexdigest(),
        "templateCount": len(list_visual_style_presets()),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="确定性生成白板视觉模板 Markdown 目录；只作选型辅助"
    )
    parser.add_argument("--output", required=True, help="待创建的 Markdown 输出路径")
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        from .cli_runtime import configure_utf8_stdio
    except ImportError:  # pragma: no cover - direct script execution
        from cli_runtime import configure_utf8_stdio  # type: ignore
    configure_utf8_stdio()
    args = build_parser().parse_args(argv)
    try:
        result = create_catalog(Path(args.output))
    except (CatalogError, OSError, ValueError):
        print("visual_style_catalog_invalid", file=sys.stderr)
        return 2
    print(
        json.dumps(
            result,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0
if __name__ == "__main__":
    raise SystemExit(main())
