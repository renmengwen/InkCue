#!/usr/bin/env python3
"""Build an identity-bound Markdown handoff for current generated line art."""
from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import quote

from project_workspace import Project, sha256_file, sha256_json, write_json_atomic


LINE_ART_REVIEW_CONTRACT = "whiteboard-line-art-review-v1"
LINE_ART_REVIEW_MANIFEST = "manifests/line-art-review-manifest.json"


class LineArtReviewError(ValueError):
    """Current generated images cannot form a trustworthy review handoff."""


def _single_line(value: Any, fallback: str) -> str:
    if not isinstance(value, str):
        return fallback
    normalized = " ".join(value.split())
    return normalized or fallback


def _fenced(text: str) -> list[str]:
    longest = 0
    cursor = 0
    while cursor < len(text):
        if text[cursor] != "`":
            cursor += 1
            continue
        end = cursor
        while end < len(text) and text[end] == "`":
            end += 1
        longest = max(longest, end - cursor)
        cursor = end
    fence = "`" * max(3, longest + 1)
    return [fence + "text", text, fence]


def _write_bytes_atomic_once(path: Path, payload: bytes) -> None:
    if path.exists():
        try:
            current = path.read_bytes()
        except OSError as exc:
            raise LineArtReviewError("线稿审阅文件无法读取") from exc
        if current != payload:
            raise LineArtReviewError("线稿审阅 identity 文件发生内容冲突")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    candidate = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with candidate.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(candidate, path)
    finally:
        candidate.unlink(missing_ok=True)


def _manifest_scenes(manifest: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    scenes = manifest.get("scenes")
    if not isinstance(scenes, list) or not all(isinstance(item, dict) for item in scenes):
        raise LineArtReviewError("generation manifest scenes 无效")
    return scenes


def _identity_payload(
    project: Project,
    generation_manifest: Mapping[str, Any],
    generation_manifest_path: Path,
) -> dict[str, Any]:
    expected_manifest = project.path(project.plan["manifestFile"])
    if generation_manifest_path.resolve() != expected_manifest.resolve():
        raise LineArtReviewError("generation manifest 路径与 current plan 不一致")
    records = _manifest_scenes(generation_manifest)
    plan_scenes = project.plan["scenes"]
    if [item.get("sceneId") for item in records] != [
        item["sceneId"] for item in plan_scenes
    ]:
        raise LineArtReviewError("generation manifest 场景顺序与 current plan 不一致")

    scenes: list[dict[str, Any]] = []
    for scene, record in zip(plan_scenes, records, strict=True):
        scene_id = scene["sceneId"]
        output_file = scene["outputFile"]
        if (
            record.get("sceneId") != scene_id
            or record.get("outputFile") != output_file
            or record.get("status") != "validated"
        ):
            raise LineArtReviewError(f"场景不是 current validated: {scene_id}")
        image = project.scenes_dir / output_file
        if not image.is_file():
            raise LineArtReviewError(f"缺少 current 线稿: {scene_id}")
        actual_sha = sha256_file(image)
        if record.get("imageSha256") != actual_sha:
            raise LineArtReviewError(f"线稿 SHA-256 与 manifest 不一致: {scene_id}")
        scenes.append(
            {
                "sceneId": scene_id,
                "name": _single_line(scene.get("name"), scene_id),
                "imageFile": image.relative_to(project.root).as_posix(),
                "imageSha256": actual_sha,
                "imageBytes": image.stat().st_size,
            }
        )

    canvas = project.plan["outputCanvas"]
    return {
        "contractVersion": LINE_ART_REVIEW_CONTRACT,
        "projectId": project.project_id,
        "generationPlanSha256": sha256_file(project.plan_path),
        "generationManifestSha256": sha256_file(generation_manifest_path),
        "canvas": {
            "width": canvas["width"],
            "height": canvas["height"],
        },
        "sceneOrder": [item["sceneId"] for item in scenes],
        "scenes": scenes,
    }


def render_line_art_review_markdown(
    project: Project,
    identity_payload: Mapping[str, Any],
    identity_hash: str,
) -> str:
    plan_by_id = {scene["sceneId"]: scene for scene in project.plan["scenes"]}
    forbid_text = project.plan.get("constraints", {}).get("forbidText") is True
    text_review = (
        "禁字要求"
        if forbid_text
        else "画内文字是否语义需要、清晰正确且没有乱码或意外文字"
    )
    lines = [
        "---",
        f"contractVersion: {LINE_ART_REVIEW_CONTRACT}",
        f"projectId: {project.project_id}",
        f"lineArtReviewIdentitySha256: {identity_hash}",
        f"sceneCount: {len(identity_payload['scenes'])}",
        "reviewStatus: pending_user_confirmation",
        "---",
        "",
        "# 统一线稿联合审阅",
        "",
        "全部图片已经通过 current generation plan、generation manifest、PNG、尺寸和 SHA-256 技术校验。技术通过不等于人工批准。",
        "",
        f"请按顺序查看每幕线稿，重点检查人物与物体造型、纸张、配色、构图、{text_review}，以及画面是否准确表达当前场景。",
        "",
        f"确认时请回到聊天回复：`确认线稿 {identity_hash[:12]}`。如果需要修改，请直接列出 scene ID 和原因。打开本文件、打开原图或没有回复都不构成批准。",
        "",
    ]
    for item in identity_payload["scenes"]:
        scene_id = item["sceneId"]
        scene = plan_by_id[scene_id]
        image_relative = "../" + item["imageFile"]
        image_url = quote(image_relative, safe="/-_.~")
        lines.extend(
            [
                f"## `{scene_id}`",
                "",
                f"名称：{item['name']}",
                "",
                f"![{scene_id}](<{image_url}>)",
                "",
                f"[打开全分辨率原图](<{image_url}>)",
                "",
            ]
        )
        core_idea = scene.get("coreIdea")
        if isinstance(core_idea, str) and core_idea.strip():
            lines.extend(["核心表达：", "", *_fenced(core_idea.strip()), ""])
        visual_subject = scene.get("visualSubject")
        if isinstance(visual_subject, str) and visual_subject.strip():
            lines.extend(["画面主体：", "", *_fenced(visual_subject.strip()), ""])
        prompt = scene.get("prompt")
        if isinstance(prompt, str) and prompt.strip():
            lines.extend(
                [
                    "<details>",
                    "<summary>查看当前图片提示词</summary>",
                    "",
                    *_fenced(prompt.strip()),
                    "",
                    "</details>",
                    "",
                ]
            )
    return "\n".join(lines)


def create_line_art_review(
    project: Project,
    generation_manifest: Mapping[str, Any],
    generation_manifest_path: Path,
) -> dict[str, Any]:
    """Write deterministic technical evidence and a user-facing Markdown handoff."""

    payload = _identity_payload(project, generation_manifest, generation_manifest_path)
    identity = sha256_json(payload)
    review_relative = f"reviews/line-art-review-{identity[:12]}.md"
    review_path = project.path(review_relative)
    review_bytes = render_line_art_review_markdown(project, payload, identity).encode("utf-8")
    _write_bytes_atomic_once(review_path, review_bytes)
    technical = {
        "contractVersion": LINE_ART_REVIEW_CONTRACT,
        "status": "current_technical",
        "identityHash": identity,
        "identityPayload": payload,
        "reviewDocument": {
            "file": review_relative,
            "sha256": sha256_file(review_path),
            "bytes": review_path.stat().st_size,
        },
        "userConfirmationRequired": True,
        "approvalWritten": False,
    }
    write_json_atomic(project.path(LINE_ART_REVIEW_MANIFEST), technical)
    return {
        "contractVersion": LINE_ART_REVIEW_CONTRACT,
        "lineArtReviewIdentitySha256": identity,
        "reviewFile": review_relative,
        "manifestFile": LINE_ART_REVIEW_MANIFEST,
        "sceneCount": len(payload["scenes"]),
        "userConfirmationRequired": True,
        "approvalWritten": False,
    }


__all__ = [
    "LINE_ART_REVIEW_CONTRACT",
    "LINE_ART_REVIEW_MANIFEST",
    "LineArtReviewError",
    "create_line_art_review",
    "render_line_art_review_markdown",
]
