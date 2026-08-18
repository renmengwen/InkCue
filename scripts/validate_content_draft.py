#!/usr/bin/env python3
"""只读校验 content-draft-v1，不持久化草案或任何派生产物。"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from content_source import (
    ContentSourceError,
    build_source_package,
    validate_content_draft,
)


def _emit(value: dict[str, Any], *, stream: Any = sys.stdout) -> None:
    print(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        file=stream,
    )


def _invalid_result() -> dict[str, Any]:
    return {
        "error": "content_draft_invalid",
        "valid": False,
        "writesPerformed": False,
    }


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        # 参数值可能包含正文、秘密或绝对路径；错误输出只给稳定错误码。
        _emit(_invalid_result(), stream=sys.stderr)
        raise SystemExit(2)


def _parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(
        description=(
            "只在内存中校验 content-draft-v1 并计算确定性 identity；"
            "不准备 source 包、不创建项目、不写批准。"
        )
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--stdin",
        action="store_true",
        help="从标准输入读取 UTF-8 JSON；用于人工确认前的只读检查",
    )
    source.add_argument(
        "--draft",
        type=Path,
        help="读取已持久化的已确认草案或测试 fixture",
    )
    return parser


def _read_json(args: argparse.Namespace) -> Any:
    if args.stdin:
        raw = sys.stdin.buffer.read()
    else:
        raw = args.draft.read_bytes()
    return json.loads(raw.decode("utf-8"))


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        raw_draft = _read_json(args)
        normalised = validate_content_draft(raw_draft)
        _, _, _, manifest = build_source_package(normalised)
    except (ContentSourceError, OSError, UnicodeError, json.JSONDecodeError):
        # 不回显异常，避免把正文、秘密字段值或本机绝对路径带入日志。
        _emit(_invalid_result(), stream=sys.stderr)
        return 2

    _emit(
        {
            "contentDraftIdentitySha256": manifest["contentDraftIdentitySha256"],
            "contractVersion": normalised["contractVersion"],
            "cueCount": len(normalised["narrationCues"]),
            "inputMode": normalised["inputMode"],
            "rewritePolicy": normalised["rewritePolicy"],
            "sceneCount": len(normalised["scenes"]),
            "targetDurationSeconds": normalised["targetDurationSeconds"],
            "valid": True,
            "writesPerformed": False,
        }
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
