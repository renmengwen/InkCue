#!/usr/bin/env python3
"""从已验证 content draft 确定性派生 Markdown 审阅 artifact。"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import uuid
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

try:  # direct CLI execution
    from content_source import (
        ContentSourceError,
        PROVISIONAL_TIMING_VERSION,
        content_draft_identity,
        validate_content_draft,
    )
    from initial_approval_options import (
        InitialApprovalOptionError,
        build_initial_approval_options,
    )
    from project_workspace import WorkspaceError, load_workspace_config
except ImportError:  # imported as scripts.render_content_review
    from scripts.content_source import (
        ContentSourceError,
        PROVISIONAL_TIMING_VERSION,
        content_draft_identity,
        validate_content_draft,
    )
    from scripts.initial_approval_options import (
        InitialApprovalOptionError,
        build_initial_approval_options,
    )
    from scripts.project_workspace import WorkspaceError, load_workspace_config


REVIEW_DOCUMENT_CONTRACT_VERSION = "whiteboard-content-review-v1"
REVIEW_ARTIFACT_CONTRACT_VERSION = "whiteboard-content-review-artifact-v1"
_ATTEMPT_RE = re.compile(r"^attempt-[0-9]{4}$")


class ReviewError(ValueError):
    pass


class JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ReviewError("invalid_arguments")


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ReviewError("duplicate_json_key")
        result[key] = value
    return result


def _read_candidate(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
        )
    except ReviewError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReviewError("candidate_invalid") from exc
    return validate_content_draft(value)


def _validate_candidate_location(candidate: Path, draft_root: Path) -> Path:
    resolved = candidate.resolve(strict=True)
    try:
        relative = resolved.relative_to(draft_root)
    except ValueError as exc:
        raise ReviewError("candidate_outside_draft") from exc
    parts = relative.parts
    if (
        len(parts) != 6
        or parts[0] != ".work"
        or parts[2] != "agent-tasks"
        or _ATTEMPT_RE.fullmatch(parts[4]) is None
        or parts[5] != "candidate.content-draft.json"
    ):
        raise ReviewError("candidate_not_attempt_output")
    return resolved


def _fenced(text: str) -> list[str]:
    longest = max((len(match.group(0)) for match in re.finditer(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    return [fence + "text", text, fence]


def _change_summary(draft: Mapping[str, Any]) -> str:
    if draft["inputMode"] == "topic":
        return "已根据冻结主题生成旁白、cue 与分镜；不确定事实仍需用户在本轮联合审阅中核对。"
    if draft["rewritePolicy"] == "preserve":
        return "未进行风格重写；仅按合同规范换行、口语标点并拆分 cue。"
    return "已按 polish 合同局部润色；事实、数字、人物、结论、因果强度与责任主体应保持不变，请对照原正文核对。"


def render_review_markdown(draft: Mapping[str, Any]) -> str:
    """只从规范化 content draft 生成稳定 Markdown 字节内容。"""

    normalised = validate_content_draft(draft)
    identity = content_draft_identity(normalised)
    provider_label = {
        "edge-tts": "Edge TTS",
        "minimax": "MiniMax",
        "doubao": "豆包语音",
    }[normalised["voiceoverMode"]]
    lines = [
        "---",
        f"contractVersion: {REVIEW_DOCUMENT_CONTRACT_VERSION}",
        f"contentDraftIdentitySha256: {identity}",
        f"inputMode: {normalised['inputMode']}",
        f"rewritePolicy: {normalised['rewritePolicy']}",
        f"targetDurationSeconds: {normalised['targetDurationSeconds']}",
        f"voiceoverMode: {normalised['voiceoverMode']}",
        "approvalStatus: pending",
        "---",
        "",
        "# 内容与制作方案联合审阅",
        "",
        f"当前已采用：{provider_label}（`{normalised['voiceoverMode']}`）。",
        "",
        "## 原始输入",
        "",
    ]
    source_label = "主题" if normalised["inputMode"] == "topic" else "正文"
    source_text = normalised["topic"] if normalised["inputMode"] == "topic" else normalised["body"]
    lines.extend([f"{source_label}：", "", *_fenced(source_text), ""])
    lines.extend(
        [
            "## 完整旁白",
            "",
            *_fenced("\n".join(cue["text"] for cue in normalised["narrationCues"])),
            "",
            "## 实质改动说明",
            "",
            _change_summary(normalised),
            "",
            "## Cue 与场景映射",
            "",
        ]
    )
    for cue in normalised["narrationCues"]:
        lines.extend(
            [
                f"### {cue['cueId']}",
                "",
                f"场景：`{cue['sceneId']}`",
                "",
                *_fenced(cue["text"]),
                "",
            ]
        )
    lines.extend(["## 分镜与图片提示词", ""])
    for scene in normalised["scenes"]:
        lines.extend(
            [
                f"### {scene['sceneId']}",
                "",
                "名称：",
                "",
                *_fenced(scene["name"]),
                "",
                "核心表达：",
                "",
                *_fenced(scene["coreIdea"]),
                "",
                "画面主体：",
                "",
                *_fenced(scene["visualSubject"]),
                "",
                "图片提示词：",
                "",
                *_fenced(scene["imagePrompt"]),
                "",
            ]
        )
    lines.extend(
        [
            "## 时序与确认边界",
            "",
            f"- provisional SRT 使用 `{PROVISIONAL_TIMING_VERSION}`，按旁白字符与停顿权重在目标时长内确定性分配。",
            f"- 当前目标时长只用于内容预算与 provisional source SRT；{provider_label} 获批后的真实音频时间轴才是权威时钟。",
            "- 正式字幕将来自获批真实音频时间轴派生的 narration SRT。",
            "- 当前仍待用户完成“内容与制作方案联合确认”；在新流程中，这次确认还必须包含对 pending 预项目 current 样音的试听与联合批准。",
            "- 当前草案只允许进入标记为 `pending_initial_approval` 的预项目，用于阶段 0 审阅、current 样音、修订与联合批准；不得生成完整旁白、生图、annotation、render 或 final。",
            "- coordinator 必须在预项目内生成并技术验证绑定 current 草案/voice plan 的真实样音，再按当前真实生图能力展示完整自然语言选项；active voice provider 只是“当前已采用”，不是用户选择项。",
            "- 用户的一次合法回复必须绑定 current content identity 与 current `SAMPLE_IDENTITY`，并由项目层重验后原子批准/冻结；本 Markdown 与技术 PASS 都不会自行写批准。",
            "",
        ]
    )
    return "\n".join(lines)


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _write_bytes_atomic_once(path: Path, payload: bytes) -> None:
    if path.exists():
        try:
            current = path.read_bytes()
        except OSError as exc:
            raise ReviewError("review_unreadable") from exc
        if current != payload:
            raise ReviewError("review_identity_collision")
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


def create_review_artifact(args: argparse.Namespace) -> dict[str, Any]:
    workspace = load_workspace_config(args.workspace_config)
    draft_root = Path(args.draft_root).resolve(strict=True)
    expected_parent = (workspace.root / "drafts").resolve(strict=False)
    if draft_root.parent != expected_parent or not draft_root.name:
        raise ReviewError("draft_scope_invalid")
    candidate = _validate_candidate_location(Path(args.candidate), draft_root)
    draft = _read_candidate(candidate)
    identity = content_draft_identity(draft)
    review_relative = PurePosixPath("reviews") / f"content-review-{identity[:12]}.md"
    review_path = draft_root.joinpath(*review_relative.parts)
    payload = render_review_markdown(draft).encode("utf-8")
    _write_bytes_atomic_once(review_path, payload)
    options = build_initial_approval_options(
        voiceover_mode=draft["voiceoverMode"],
        gpt_login_image_generation_available=args.gpt_login_image_generation_available,
        configured_image_provider_available=args.configured_image_provider_available,
        fixed_image_generation_mode=args.fixed_image_generation_mode,
    )
    return {
        "contractVersion": REVIEW_ARTIFACT_CONTRACT_VERSION,
        "ok": True,
        "valid": True,
        "writesPerformed": True,
        "contentDraftIdentitySha256": identity,
        "reviewFile": review_relative.as_posix(),
        "reviewSha256": _sha256_bytes(payload),
        "cueCount": len(draft["narrationCues"]),
        "sceneCount": len(draft["scenes"]),
        "initialApprovalOptions": list(options),
        "userConfirmationRequired": True,
        "approvalWritten": False,
        "formalPublished": False,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = JsonArgumentParser(
        description="校验 attempt content draft 并原子派生 Markdown 审阅文件"
    )
    parser.add_argument("--draft-root", required=True, help="workspace/drafts/<draft-id>")
    parser.add_argument("--candidate", required=True, help="attempt 的 candidate.content-draft.json")
    parser.add_argument("--workspace-config", help="工作区配置；默认 config/workspace.local.json")
    parser.add_argument(
        "--gpt-login-image-generation-available",
        action="store_true",
        help="coordinator 已确认当前登录态 image_gen 可用",
    )
    provider_group = parser.add_mutually_exclusive_group()
    provider_group.add_argument(
        "--configured-image-provider-available",
        dest="configured_image_provider_available",
        action="store_true",
        help="coordinator 已确认图片供应商已配置可用（默认）",
    )
    provider_group.add_argument(
        "--configured-image-provider-unavailable",
        dest="configured_image_provider_available",
        action="store_false",
        help="coordinator 已确认当前没有可用的已配置图片供应商",
    )
    parser.set_defaults(configured_image_provider_available=True)
    parser.add_argument(
        "--fixed-image-generation-mode",
        choices=("provider", "gpt-login"),
        help="阶段 0 已提前固定且当前可用的生图方式；固定后不再作为选项轴",
    )
    return parser


def _emit(value: Mapping[str, Any], *, stream: Any = sys.stdout) -> None:
    print(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        file=stream,
    )


def main(argv: list[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
        result = create_review_artifact(args)
    except SystemExit as exc:
        return int(exc.code)
    except (
        ReviewError,
        InitialApprovalOptionError,
        ContentSourceError,
        WorkspaceError,
        OSError,
        ValueError,
    ):
        _emit(
            {
                "contractVersion": REVIEW_ARTIFACT_CONTRACT_VERSION,
                "ok": False,
                "valid": False,
                "writesPerformed": False,
                "error": "content_review_invalid",
            },
            stream=sys.stderr,
        )
        return 2
    _emit(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
