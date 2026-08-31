#!/usr/bin/env python3
"""封面证据的读取与视觉检查豁免边界。

完整 scene 集合的新生图链路默认产出封面；历史项目仍允许尚无封面。封面是
独立图片，不属于普通 scene 源图。此模块只校验封面证据的身份和
`coverFrameRange`，不会从任何技术媒体校验中扣除封面帧。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from project_workspace import sha256_file


class CoverReviewError(ValueError):
    """封面 manifest 缺失、格式错误或与当前文件不一致。"""


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise CoverReviewError(f"{label} 缺失或不是对象")
    return value


def load_cover_review(project: Any, *, required: bool = False) -> dict[str, Any] | None:
    """读取当前封面 manifest，返回可嵌入 review/delivery evidence 的规范记录。

    兼容两种形态：manifest 顶层直接放 `file`/`frameRange`，或包在 `cover` 下。
    没有封面时返回 None（不强制历史项目立即补生成封面）。
    """

    manifest_path = project.path("manifests/cover-manifest.json")
    if not manifest_path.is_file():
        if required:
            raise CoverReviewError("缺少 manifests/cover-manifest.json")
        return None
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CoverReviewError(f"封面 manifest 无法读取: {exc}") from exc
    if not isinstance(raw, Mapping):
        raise CoverReviewError("封面 manifest 顶层必须是对象")
    if raw.get("projectId") not in (None, project.project_id):
        raise CoverReviewError("封面 manifest projectId 与 current project 不一致")
    if "semanticInputs" in raw:
        semantic_inputs = _mapping(raw.get("semanticInputs"), "cover manifest.semanticInputs")
        current_bindings = {
            "planSha256": sha256_file(project.plan_path) if project.plan_path.is_file() else None,
            "sourceInputSha256": (
                sha256_file(project.path("source/input.json"))
                if project.path("source/input.json").is_file()
                else None
            ),
            "sourceSrtSha256": (
                sha256_file(project.path("source/source.srt"))
                if project.path("source/source.srt").is_file()
                else None
            ),
        }
        for field, current_sha in current_bindings.items():
            if semantic_inputs.get(field) != current_sha:
                raise CoverReviewError(f"封面 semanticInputs.{field} 与 current 项目不一致")
    cover_value = raw.get("cover", raw)
    cover = _mapping(cover_value, "cover manifest.cover")
    file = cover.get("file")
    if not isinstance(file, str) or not file or Path(file).is_absolute():
        raise CoverReviewError("封面 file 必须是项目内相对路径")
    cover_path = project.path(file)
    if not cover_path.is_file():
        raise CoverReviewError("封面图片文件缺失")
    sha = cover.get("sha256")
    actual_sha = sha256_file(cover_path)
    if not isinstance(sha, str) or len(sha) != 64 or sha != actual_sha:
        raise CoverReviewError("封面图片 SHA-256 与 manifest 不一致")
    bytes_value = cover.get("bytes")
    actual_bytes = cover_path.stat().st_size
    if bytes_value is not None and bytes_value != actual_bytes:
        raise CoverReviewError("封面图片 bytes 与 manifest 不一致")
    frame_range = _mapping(cover.get("frameRange", raw.get("coverFrameRange")), "cover frameRange")
    start = frame_range.get("startFrame")
    end = frame_range.get("endFrameExclusive")
    if isinstance(start, bool) or not isinstance(start, int) or start < 0:
        raise CoverReviewError("coverFrameRange.startFrame 无效")
    if isinstance(end, bool) or not isinstance(end, int) or end <= start:
        raise CoverReviewError("coverFrameRange.endFrameExclusive 无效")
    excluded = cover.get("visualReviewExcluded", raw.get("visualReviewExcluded"))
    if excluded is not True:
        raise CoverReviewError("封面必须明确声明 visualReviewExcluded=true")
    return {
        "manifestFile": "manifests/cover-manifest.json",
        "manifestSha256": sha256_file(manifest_path),
        "file": file,
        "sha256": actual_sha,
        "bytes": actual_bytes,
        "frameRange": {"startFrame": start, "endFrameExclusive": end},
        "visualReviewExcluded": True,
        "technicalChecksExcluded": False,
        "semanticSource": cover.get("semanticSource", raw.get("semanticSource", "whole_video")),
    }


def cover_frame_range(cover: Mapping[str, Any] | None) -> dict[str, int] | None:
    if not isinstance(cover, Mapping):
        return None
    value = cover.get("frameRange")
    return dict(value) if isinstance(value, Mapping) else None
