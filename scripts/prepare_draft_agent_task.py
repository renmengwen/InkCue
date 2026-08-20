#!/usr/bin/env python3
"""为 contentDrafting/storyboardPlanning 冻结可直接宿主派发的 draft attempt。

本工具只准备 candidate attempt 和真实宿主所需的 spawn package；它不创建
child、不发布正式草案/分镜、不创建项目，也不写任何人工批准。
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import unicodedata
import uuid
from pathlib import Path
from typing import Any, Mapping

try:  # direct CLI execution
    from agent_task_contract import (
        ROLE_CONTRACT_VERSION,
        TASK_CONTRACT_VERSION,
        TrustedTaskContext,
        build_agent_batch_audit,
        build_agent_prompt,
        decide_agent_dispatch,
        sha256_file,
        validate_agent_task,
    )
    from project_workspace import (
        WorkspaceError,
        load_workspace_config,
        write_json_atomic,
    )
    from content_source import (
        ContentSourceError,
        content_draft_identity,
        validate_content_draft,
    )
    from srt_timeline import SrtValidationError, group_scenes, parse_srt
except ImportError:  # imported as scripts.prepare_draft_agent_task
    from scripts.agent_task_contract import (
        ROLE_CONTRACT_VERSION,
        TASK_CONTRACT_VERSION,
        TrustedTaskContext,
        build_agent_batch_audit,
        build_agent_prompt,
        decide_agent_dispatch,
        sha256_file,
        validate_agent_task,
    )
    from scripts.project_workspace import (
        WorkspaceError,
        load_workspace_config,
        write_json_atomic,
    )
    from scripts.content_source import (
        ContentSourceError,
        content_draft_identity,
        validate_content_draft,
    )
    from scripts.srt_timeline import SrtValidationError, group_scenes, parse_srt


PREPARE_CONTRACT_VERSION = "whiteboard-draft-agent-prepare-v1"
HOST_SPAWN_PACKAGE_VERSION = "whiteboard-host-spawn-package-v1"
CONTENT_INPUT_CONTRACT_VERSION = "whiteboard-content-input-v1"
CONTENT_REVISION_REQUEST_CONTRACT_VERSION = "whiteboard-content-revision-request-v1"
HOST_DRAFT_CAPABILITIES = ("readFiles", "writeCandidateJson")
_DRIVE_PATH_RE = re.compile(r"(?i)(?:^|\s)[a-z]:[\\/]")
_ABSOLUTE_PATH_RE = re.compile(
    r"(?i)(?:[a-z]:[\\/]|\\\\[^\\\s]+\\[^\\\s]+|(?:^|[\s\"'(])/(?!/)[^\s]+)"
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_CUE_ID_RE = re.compile(r"^cue-[0-9]{3}$")
_SCENE_ID_RE = re.compile(r"^scene-[0-9]{2}$")

CONTENT_ROLE_CONTRACT = """# contentDrafting frozen role contract

- 只读取 task.json 的 inputs；正文不从 prompt 或主对话补取。
- 根据 content-input.json 生成完整 whiteboard-content-draft-v1：自然中文旁白、连续 cue/scene、自包含的单幕 imagePrompt。
- topic 只允许 generate；text 只允许 preserve/polish；首版 voiceoverMode 固定 edge-tts。
- text+preserve 不改写语义；polish 不改变事实、数字、人物、结论、因果强度或责任主体。
- 每个 imagePrompt 必须自含暖米黄纸张、线稿、配色、主体、构图、留白和禁字/禁水印要求，不引用前图。
- cue 到 scene 按视觉状态变化拆分，不按具体名词类别机械拆分；允许通过增加 scene 降低单图叙事负担，但不得预设固定场景数量。
- 每幕只表达一个核心视觉命题；当语义包含多个可依次呈现的状态或主体时，imagePrompt 应组织 2–3 个可独立揭示的视觉区域。只有不可分割的连续主体才合并，不设固定 scene 数量。
- imagePrompt 应明确左到右或上到下的视觉阅读方向（只作为静态构图顺序，不是绘制元数据），为每个区域指定完整主体和真实、连续的暖米黄纸面留白；不得画漫画格、编号或标题。
- 不得用跨区域的连续背景、共同底面、道路、长线、箭头、光束、河流、山脉或其他贯穿结构把区域连接起来；空间独立区域优先保持不遮挡，但局部接近不等于必须合并。
- 若多个概念在视觉上确实必须组成不可分割的连续构图，则合并为一个视觉簇；不得为了凑数量强拆，也不得因为担心区域交叠把所有内容强行合并为一个簇。
- 只写 allowedOutputs 中的 candidate.content-draft.json 与 result.json；不得运行 prepare_source.py、创建项目、调用 provider、写正式文件或批准。
- result.json 使用 whiteboard-agent-result-v1，完整复制 task identity、taskSha256、role SHA、sequence 与全部 inputs 到 inspectedInputs；completed 时 outputs 列出候选相对路径及 SHA，findings/warnings 为数组，error 为 null。
- 写完候选后把候选 UTF-8 JSON 送入 `python -B scripts/validate_content_draft.py --stdin`；不得使用仅限已确认输入的 `--draft`，只有只读校验退出码 0 才能 completed。
"""

CONTENT_REVISION_ROLE_CONTRACT = """# contentDrafting frozen revision role contract

- 只读取 task.json 的 inputs；不得从 prompt 或主对话补取正文、上一版草案或修改要求。
- base.content-draft.json 是上一版只读候选；revision-request.json 是本 attempt 唯一修改要求。两者都不得改写。
- 输出完整的 whiteboard-content-draft-v1，不输出 patch；未被修改要求触及的事实、数字、人物、结论、因果强度、责任主体、cue/scene 内容与图片约束应保持不变。
- globalInstructions、cueChanges、sceneChanges 是要执行的修改；mustPreserve 是修改时必须继续满足的保护条件。
- cue/scene 可因修改需要重新编号或调整映射，但最终仍须满足连续 cue、连续 scene 和每幕至少一个 cue 的完整合同。
- 每个 imagePrompt 必须自含暖米黄纸张、线稿、配色、主体、构图、留白和禁字/禁水印要求，不引用前图。
- cue 到 scene 按视觉状态变化拆分，不按具体名词类别机械拆分；允许通过增加 scene 降低单图叙事负担，但不得预设固定场景数量。
- 每幕只表达一个核心视觉命题；当语义包含多个可依次呈现的状态或主体时，imagePrompt 应组织 2–3 个可独立揭示的视觉区域。只有不可分割的连续主体才合并，不设固定 scene 数量。
- imagePrompt 应明确左到右或上到下的视觉阅读方向（只作为静态构图顺序，不是绘制元数据），为每个区域指定完整主体和真实、连续的暖米黄纸面留白；不得画漫画格、编号或标题。
- 不得用跨区域的连续背景、共同底面、道路、长线、箭头、光束、河流、山脉或其他贯穿结构把区域连接起来；空间独立区域优先保持不遮挡，但局部接近不等于必须合并。
- 若多个概念在视觉上确实必须组成不可分割的连续构图，则合并为一个视觉簇；不得为了凑数量强拆，也不得因为担心区域交叠把所有内容强行合并为一个簇。
- 只写 allowedOutputs 中的 candidate.content-draft.json 与 result.json；不得覆盖旧 attempt、运行 prepare_source.py、创建项目、调用 provider、写正式文件或批准。
- result.json 使用 whiteboard-agent-result-v1，完整复制 task identity、taskSha256、role SHA、sequence 与全部 inputs 到 inspectedInputs；completed 时 outputs 列出候选相对路径及 SHA，findings/warnings 为数组，error 为 null。
- 写完候选后把候选 UTF-8 JSON 送入 `python -B scripts/validate_content_draft.py --stdin`；不得使用仅限已确认输入的 `--draft`，只有只读校验退出码 0 才能 completed。
"""

STORYBOARD_ROLE_CONTRACT = """# storyboardPlanning frozen role contract

- 只读取 task.json 列出的 source.srt、parsed-srt.json 和 role contract；不得从 prompt 补取字幕正文。
- 仅为传统 SRT 生成 pre-project candidate.generation-plan.json，不携带正式 projectId，不创建项目。
- scenes 必须按 parsed-srt 顺序覆盖全部 cue；每幕包含 sceneId、name、cueRange、sceneDurationMs、outputFile、prompt、coreIdea、visualSubject。
- prompt 必须是可独立生图的暖米黄纸张白板线稿提示词并禁止文字、水印；不得使用 imagePrompt 或 sourceCueRange 字段。
- cue 到 scene 按视觉状态变化拆分，不按具体名词类别机械拆分；允许通过增加 scene 降低单图叙事负担，但不得预设固定场景数量。
- 每幕只表达一个核心视觉命题；当语义包含多个可依次呈现的状态或主体时，prompt 应组织 2–3 个可独立揭示的视觉区域。只有不可分割的连续主体才合并，不设固定 scene 数量。
- prompt 应明确左到右或上到下的视觉阅读方向（只作为静态构图顺序，不是绘制元数据），为每个区域指定完整主体和真实、连续的暖米黄纸面留白；不得画漫画格、编号或标题。
- 不得用跨区域的连续背景、共同底面、道路、长线、箭头、光束、河流、山脉或其他贯穿结构把区域连接起来；空间独立区域优先保持不遮挡，但局部接近不等于必须合并。
- 若多个概念在视觉上确实必须组成不可分割的连续构图，则合并为一个视觉簇；不得为了凑数量强拆，也不得因为担心区域交叠把所有内容强行合并为一个簇。
- 只写 allowedOutputs 中的 candidate.generation-plan.json 与 result.json；不得修改 SRT、调用 provider、写策略批准或正式文件。
- result.json 使用 whiteboard-agent-result-v1，完整复制 task identity、taskSha256、role SHA、sequence 与全部 inputs 到 inspectedInputs；completed 时 outputs 列出候选相对路径及 SHA，findings/warnings 为数组，error 为 null。
"""


class PrepareError(ValueError):
    pass


class JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise PrepareError(message)


def _normalise_text(value: Any, *, label: str, allow_null: bool = False) -> str | None:
    if value is None and allow_null:
        return None
    if not isinstance(value, str):
        raise PrepareError(f"{label} 必须是字符串")
    text = unicodedata.normalize("NFKC", value).replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text or "\x00" in text:
        raise PrepareError(f"{label} 必须是非空安全文本")
    if _DRIVE_PATH_RE.search(text):
        raise PrepareError(f"{label} 不得包含本机盘符绝对路径")
    return text


def validate_content_input(value: Any) -> dict[str, Any]:
    """校验 child 生成草案前的最小冻结输入。"""

    if not isinstance(value, Mapping):
        raise PrepareError("content input 顶层必须是对象")
    expected = {
        "schemaVersion", "contractVersion", "inputMode", "topic", "body",
        "rewritePolicy", "targetDurationSeconds", "voiceoverMode",
    }
    if set(value) != expected:
        raise PrepareError("content input 字段必须与 whiteboard-content-input-v1 完全一致")
    if value.get("schemaVersion") != 1 or value.get("contractVersion") != CONTENT_INPUT_CONTRACT_VERSION:
        raise PrepareError("content input 合同版本无效")
    mode = value.get("inputMode")
    policy = value.get("rewritePolicy")
    if mode == "topic" and policy != "generate":
        raise PrepareError("topic 只允许 rewritePolicy=generate")
    if mode == "text" and policy not in {"preserve", "polish"}:
        raise PrepareError("text 只允许 rewritePolicy=preserve|polish")
    if mode not in {"topic", "text"}:
        raise PrepareError("inputMode 只允许 topic|text")
    if value.get("voiceoverMode") not in {"edge-tts", "minimax"}:
        raise PrepareError("topic/text 只允许 voiceoverMode=edge-tts 或 minimax")
    target = value.get("targetDurationSeconds")
    if isinstance(target, bool) or not isinstance(target, (int, float)) or not math.isfinite(target) or not 15 <= target <= 600:
        raise PrepareError("targetDurationSeconds 必须是 15–600 的有限数字")
    target_ms = round(float(target) * 1000)
    if not math.isclose(float(target) * 1000, target_ms, rel_tol=0, abs_tol=1e-7):
        raise PrepareError("targetDurationSeconds 最多精确到毫秒")
    topic = _normalise_text(value.get("topic"), label="topic", allow_null=mode == "text")
    body = _normalise_text(value.get("body"), label="body", allow_null=mode == "topic")
    if mode == "topic" and body is not None:
        raise PrepareError("topic 模式 body 必须为 null")
    if mode == "text" and body is None:
        raise PrepareError("text 模式必须提供 body")
    if topic is not None and len(topic) > 200:
        raise PrepareError("topic 超过 200 个字符")
    if body is not None and len(body.encode("utf-8")) > 128 * 1024:
        raise PrepareError("body 超过 128 KiB UTF-8")
    return {
        "schemaVersion": 1,
        "contractVersion": CONTENT_INPUT_CONTRACT_VERSION,
        "inputMode": mode,
        "topic": topic,
        "body": body,
        "rewritePolicy": policy,
        "targetDurationSeconds": target_ms // 1000 if target_ms % 1000 == 0 else target_ms / 1000,
        "voiceoverMode": value.get("voiceoverMode", "edge-tts"),
    }


def _normalise_revision_text(value: Any, *, label: str) -> str:
    if not isinstance(value, str):
        raise PrepareError(f"{label} 必须是字符串")
    text = unicodedata.normalize("NFKC", value).replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text or "\x00" in text:
        raise PrepareError(f"{label} 必须是非空安全文本")
    if len(text.encode("utf-8")) > 16 * 1024:
        raise PrepareError(f"{label} 超过 16 KiB UTF-8")
    if _ABSOLUTE_PATH_RE.search(text):
        raise PrepareError(f"{label} 不得包含绝对路径")
    return text


def _normalise_text_list(value: Any, *, label: str) -> list[str]:
    if not isinstance(value, list):
        raise PrepareError(f"{label} 必须是数组")
    return [
        _normalise_revision_text(item, label=f"{label}[{index}]")
        for index, item in enumerate(value)
    ]


def _normalise_targeted_changes(
    value: Any,
    *,
    label: str,
    id_field: str,
    id_pattern: re.Pattern[str],
    valid_ids: set[str],
) -> list[dict[str, str]]:
    if not isinstance(value, list):
        raise PrepareError(f"{label} 必须是数组")
    normalised: list[dict[str, str]] = []
    seen: set[str] = set()
    for index, item in enumerate(value):
        if not isinstance(item, Mapping) or set(item) != {id_field, "instruction"}:
            raise PrepareError(f"{label}[{index}] 字段集合无效")
        target_id = item.get(id_field)
        if not isinstance(target_id, str) or id_pattern.fullmatch(target_id) is None:
            raise PrepareError(f"{label}[{index}].{id_field} 无效")
        if target_id not in valid_ids:
            raise PrepareError(f"{label}[{index}].{id_field} 不存在于上一版草案")
        if target_id in seen:
            raise PrepareError(f"{label} 不得重复指定同一目标")
        seen.add(target_id)
        normalised.append(
            {
                id_field: target_id,
                "instruction": _normalise_revision_text(
                    item.get("instruction"),
                    label=f"{label}[{index}].instruction",
                ),
            }
        )
    return normalised


def validate_revision_request(
    value: Any,
    *,
    base_draft: Mapping[str, Any],
) -> dict[str, Any]:
    """校验并规范化绑定上一版 candidate identity 的冻结修改请求。"""

    if not isinstance(value, Mapping):
        raise PrepareError("revision request 顶层必须是对象")
    expected = {
        "schemaVersion",
        "contractVersion",
        "baseContentDraftIdentitySha256",
        "globalInstructions",
        "cueChanges",
        "sceneChanges",
        "mustPreserve",
    }
    if set(value) != expected:
        raise PrepareError("revision request 字段必须与合同完全一致")
    if value.get("schemaVersion") != 1:
        raise PrepareError("revision request schemaVersion 必须为 1")
    if value.get("contractVersion") != CONTENT_REVISION_REQUEST_CONTRACT_VERSION:
        raise PrepareError("revision request contractVersion 无效")
    base_identity = value.get("baseContentDraftIdentitySha256")
    if not isinstance(base_identity, str) or _SHA256_RE.fullmatch(base_identity) is None:
        raise PrepareError("baseContentDraftIdentitySha256 必须是小写 SHA-256")
    expected_identity = content_draft_identity(base_draft)
    if base_identity != expected_identity:
        raise PrepareError("revision request 绑定的上一版 content draft identity 不匹配")

    cue_ids = {item["cueId"] for item in base_draft["narrationCues"]}
    scene_ids = {item["sceneId"] for item in base_draft["scenes"]}
    global_instructions = _normalise_text_list(
        value.get("globalInstructions"), label="globalInstructions"
    )
    cue_changes = _normalise_targeted_changes(
        value.get("cueChanges"),
        label="cueChanges",
        id_field="cueId",
        id_pattern=_CUE_ID_RE,
        valid_ids=cue_ids,
    )
    scene_changes = _normalise_targeted_changes(
        value.get("sceneChanges"),
        label="sceneChanges",
        id_field="sceneId",
        id_pattern=_SCENE_ID_RE,
        valid_ids=scene_ids,
    )
    must_preserve = _normalise_text_list(value.get("mustPreserve"), label="mustPreserve")
    if not global_instructions and not cue_changes and not scene_changes:
        raise PrepareError("revision request 至少包含一项实质修改")
    if sum(
        len(item.encode("utf-8"))
        for item in [*global_instructions, *must_preserve]
    ) + sum(
        len(item["instruction"].encode("utf-8"))
        for item in [*cue_changes, *scene_changes]
    ) > 128 * 1024:
        raise PrepareError("revision request 文本总量超过 128 KiB UTF-8")
    return {
        "schemaVersion": 1,
        "contractVersion": CONTENT_REVISION_REQUEST_CONTRACT_VERSION,
        "baseContentDraftIdentitySha256": base_identity,
        "globalInstructions": global_instructions,
        "cueChanges": cue_changes,
        "sceneChanges": scene_changes,
        "mustPreserve": must_preserve,
    }


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise PrepareError("输入 JSON 包含重复字段")
        result[key] = value
    return result


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
        )
    except PrepareError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PrepareError("输入 JSON 不可读或无效") from exc
    if not isinstance(value, dict):
        raise PrepareError("输入 JSON 顶层必须是对象")
    return value


def _write_once_json(path: Path, value: Mapping[str, Any]) -> None:
    if path.exists():
        try:
            current = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise PrepareError(f"已存在的 {path.name} 不可读") from exc
        if current != dict(value):
            raise PrepareError(f"拒绝覆盖内容不同的 {path.name}")
        return
    write_json_atomic(path, value)


def _write_once_bytes(path: Path, payload: bytes) -> None:
    if path.exists():
        if path.read_bytes() != payload:
            raise PrepareError(f"拒绝覆盖内容不同的 {path.name}")
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


def _load_revision_sources(
    args: argparse.Namespace,
    draft_root: Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    base_source = Path(getattr(args, "base_content_draft")).resolve(strict=True)
    draft_root_resolved = draft_root.resolve(strict=False)
    try:
        relative = base_source.relative_to(draft_root_resolved)
    except ValueError as exc:
        raise PrepareError("base content draft 必须来自当前 draft-root 的旧 attempt") from exc
    parts = relative.parts
    if (
        len(parts) != 6
        or parts[0] != ".work"
        or parts[2] != "agent-tasks"
        or re.fullmatch(r"attempt-[0-9]{4}", parts[4]) is None
        or parts[5] != "candidate.content-draft.json"
    ):
        raise PrepareError("base content draft 必须是当前 draft-root 中旧 attempt 的候选")
    base_draft = validate_content_draft(_read_json(base_source))
    request_source = Path(getattr(args, "revision_request")).resolve(strict=True)
    revision_request = validate_revision_request(
        _read_json(request_source),
        base_draft=base_draft,
    )
    return base_draft, revision_request


def _prepare_input(args: argparse.Namespace, draft_root: Path) -> tuple[str, Path, dict[str, str | None], str]:
    if args.role == "contentDrafting":
        source = Path(args.content_input).resolve(strict=True)
        normalised = validate_content_input(_read_json(source))
        target = draft_root / "content-input.json"
        _write_once_json(target, normalised)
        sha = sha256_file(target)
        return "content-draft", target, {"contentInputSha256": sha}, CONTENT_ROLE_CONTRACT

    source = Path(args.source_srt).resolve(strict=True)
    payload = source.read_bytes()
    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise PrepareError("source SRT 必须是 UTF-8/UTF-8 BOM") from exc
    cues = parse_srt(text)
    scenes = group_scenes(cues, args.target_sec, args.min_sec, args.max_sec)
    source_target = draft_root / "source.srt"
    parsed_target = draft_root / "parsed-srt.json"
    _write_once_bytes(source_target, payload)
    _write_once_json(parsed_target, {"cues": cues, "scenes": scenes})
    return (
        "storyboard",
        source_target,
        {
            "sourceSrtSha256": sha256_file(source_target),
            "parsedSrtSha256": sha256_file(parsed_target),
        },
        STORYBOARD_ROLE_CONTRACT,
    )


def prepare_draft_task(args: argparse.Namespace) -> dict[str, Any]:
    workspace = load_workspace_config(args.workspace_config)
    draft_root = Path(args.draft_root).resolve(strict=False)
    expected_parent = (workspace.root / "drafts").resolve(strict=False)
    if draft_root.parent != expected_parent or not draft_root.name:
        raise PrepareError("draft-root 必须是 workspace/drafts/<draft-id>")
    revision_request_arg = getattr(args, "revision_request", None)
    base_content_draft_arg = getattr(args, "base_content_draft", None)
    if bool(revision_request_arg) != bool(base_content_draft_arg):
        raise PrepareError(
            "--revision-request 与 --base-content-draft 必须成对提供"
        )
    if args.role == "contentDrafting":
        if bool(revision_request_arg) == bool(args.content_input):
            raise PrepareError(
                "contentDrafting 必须在初次输入与修订输入中二选一"
            )
    elif revision_request_arg or base_content_draft_arg:
        raise PrepareError("storyboardPlanning 不接受 content revision 参数")
    draft_root.mkdir(parents=True, exist_ok=True)
    revision_mode = (
        args.role == "contentDrafting"
        and bool(revision_request_arg)
        and bool(base_content_draft_arg)
    )
    if revision_mode:
        base_draft, revision_request = _load_revision_sources(args, draft_root)
        task_id_default = "content-draft"
        bindings = {
            "baseContentDraftIdentitySha256": revision_request[
                "baseContentDraftIdentitySha256"
            ],
            "revisionRequestSha256": None,
        }
        role_contract_text = CONTENT_REVISION_ROLE_CONTRACT
        primary_input = None
    else:
        task_id_default, primary_input, bindings, role_contract_text = _prepare_input(args, draft_root)
    task_id = args.task_id or task_id_default
    run_id = args.run_id or ("cd-" if args.role == "contentDrafting" else "sb-") + uuid.uuid4().hex[:12]
    context = TrustedTaskContext(workspace.root, draft_root, "draft", run_id, task_id, args.attempt)
    context.task_dir.mkdir(parents=True, exist_ok=False)
    if revision_mode:
        base_target = context.task_dir / "base.content-draft.json"
        revision_target = context.task_dir / "revision-request.json"
        _write_once_json(base_target, base_draft)
        _write_once_json(revision_target, revision_request)
        bindings["revisionRequestSha256"] = sha256_file(revision_target)
    role_contract = context.task_dir / "role-contract.md"
    role_contract.write_text(role_contract_text, encoding="utf-8", newline="\n")
    input_paths = (
        [base_target, revision_target]
        if revision_mode
        else [primary_input]
    )
    if args.role == "storyboardPlanning":
        input_paths.append(draft_root / "parsed-srt.json")
    input_paths.append(role_contract)
    candidate_name = (
        "candidate.content-draft.json"
        if args.role == "contentDrafting"
        else "candidate.generation-plan.json"
    )
    candidate = context.task_dir / candidate_name
    task_data = {
        "contractVersion": TASK_CONTRACT_VERSION,
        "taskId": task_id,
        "taskKind": args.role,
        "scopeKind": "draft",
        "roleContractVersion": ROLE_CONTRACT_VERSION,
        "roleContractSha256": sha256_file(role_contract),
        "attempt": args.attempt,
        "sequence": 1,
        "inputs": [
            {"file": context.relative_posix(path), "sha256": sha256_file(path)}
            for path in input_paths
        ],
        "currentBindings": bindings,
        "requiredCapabilities": list(HOST_DRAFT_CAPABILITIES),
        "allowedOutputs": [
            context.relative_posix(candidate),
            context.relative_posix(context.result_json),
        ],
        "formalWritesAllowed": False,
        "approvalWritesAllowed": False,
    }
    write_json_atomic(context.task_json, task_data)
    task = validate_agent_task(context.task_json, context, expected_current_bindings=bindings)
    decision = decide_agent_dispatch(
        task,
        configured=workspace.for_role(args.role),
        ready_tasks=1,
        runtime_child_slots=1,
        resource_budget=1,
        runtime_role_capabilities=HOST_DRAFT_CAPABILITIES,
        coordinator_capabilities=HOST_DRAFT_CAPABILITIES,
    )
    audit = build_agent_batch_audit(
        stage=args.role,
        configured=workspace.for_role(args.role),
        task_count=1,
        decision=decision,
    )
    prompt = build_agent_prompt(
        task_json=context.task_json.resolve(),
        role_contract=role_contract.resolve(),
        task_kind=args.role,
        task_sha256=task.task_sha256,
        role_contract_sha256=task_data["roleContractSha256"],
    )
    task_name = re.sub(r"[^a-z0-9_]", "_", f"{args.role}_{run_id}".lower())[:64].rstrip("_")
    spawn_package = {
        "contractVersion": HOST_SPAWN_PACKAGE_VERSION,
        "preparedOnly": True,
        "hostSpawnRequired": bool(decision.dispatch_allowed),
        "hostSpawnExecuted": False,
        "taskId": task_id,
        "taskKind": args.role,
        "taskJsonPath": str(context.task_json.resolve()),
        "taskSha256": task.task_sha256,
        "roleContractPath": str(role_contract.resolve()),
        "roleContractSha256": task_data["roleContractSha256"],
        "allowedAttemptDir": str(context.task_dir.resolve()),
        "resultJsonPath": str(context.result_json.resolve()),
        "allowedOutputs": list(task_data["allowedOutputs"]),
        "requiredCapabilities": list(HOST_DRAFT_CAPABILITIES),
        "spawnAgentCall": {
            "task_name": task_name,
            "fork_turns": "none",
            "message": prompt,
        } if decision.dispatch_allowed else None,
        "completionContract": {
            "resultJsonPath": str(context.result_json.resolve()),
            "returnFields": ["TASK_STATUS", "RESULT_JSON", "VALIDATOR_STATUS", "SUMMARY"],
        },
    }
    return {
        "contractVersion": PREPARE_CONTRACT_VERSION,
        "ok": True,
        "preparedOnly": True,
        "formalWritesAllowed": False,
        "approvalWritesAllowed": False,
        "approvalWritten": False,
        "formalPublished": False,
        "draftRoot": str(draft_root),
        "runId": run_id,
        "attempt": args.attempt,
        "dispatchAudit": audit,
        "spawnPackage": spawn_package,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = JsonArgumentParser(description="准备 content/storyboard draft subagent attempt；只准备，不实际 spawn")
    parser.add_argument("role", choices=("contentDrafting", "storyboardPlanning"))
    parser.add_argument("--draft-root", required=True, help="workspace/drafts/<draft-id>")
    parser.add_argument("--workspace-config", help="工作区配置；默认 config/workspace.local.json")
    parser.add_argument("--run-id", help="稳定 run ID；默认自动生成")
    parser.add_argument("--task-id", help="稳定 task ID；默认按 role 生成")
    parser.add_argument("--attempt", type=int, default=1, help="attempt 正整数，默认 1")
    parser.add_argument("--content-input", help="contentDrafting 的 whiteboard-content-input-v1 JSON")
    parser.add_argument(
        "--revision-request",
        help="contentDrafting 修订的 whiteboard-content-revision-request-v1 JSON",
    )
    parser.add_argument(
        "--base-content-draft",
        help="同一 draft-root 中上一 attempt 的 candidate.content-draft.json",
    )
    parser.add_argument("--source-srt", help="storyboardPlanning 的传统严格 SRT")
    parser.add_argument("--target-sec", type=float, default=30.0)
    parser.add_argument("--min-sec", type=float, default=25.0)
    parser.add_argument("--max-sec", type=float, default=35.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
        if args.attempt < 1:
            raise PrepareError("attempt 必须是正整数")
        if args.role == "contentDrafting":
            initial_mode = bool(args.content_input) and not args.revision_request and not args.base_content_draft
            revision_mode = (
                not args.content_input
                and bool(args.revision_request)
                and bool(args.base_content_draft)
            )
            if args.source_srt or not (initial_mode or revision_mode):
                raise PrepareError(
                    "contentDrafting 必须提供 --content-input，或成对提供 "
                    "--revision-request 与 --base-content-draft"
                )
        elif (
            not args.source_srt
            or args.content_input
            or args.revision_request
            or args.base_content_draft
        ):
            raise PrepareError("storyboardPlanning 必须且只能提供 --source-srt")
        result = prepare_draft_task(args)
    except SystemExit as exc:
        return int(exc.code)
    except (PrepareError, WorkspaceError, ContentSourceError, SrtValidationError, OSError, ValueError):
        print(json.dumps({"contractVersion": PREPARE_CONTRACT_VERSION, "ok": False, "exitCode": 2, "error": "draft_agent_prepare_invalid"}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
