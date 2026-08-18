#!/usr/bin/env python3
"""
SRT 解析 + 分镜建议

把 .srt 字幕解析成结构化字幕条，并按「每幕 25-35 秒口播」的建议把字幕
分组成场景，给出每个场景的起止时间、总时长（→ sceneDurationMs）和文本。

用途：作为 srt-whiteboard-animation 工作流第 1 步的输入依据——
读出叙事事件、规划配图策略、并为每张图片的标注确定 sceneDurationMs。

用法：
  python parse_srt.py <字幕.srt> [--target-sec 30] [--min-sec 25] [--max-sec 35]

输出：JSON（stdout），字段：
  cues    每条字幕: {index, startMs, endMs, durMs, text}
  scenes  建议场景: {sceneIndex, startMs, endMs, sceneDurationMs, cueRange, text}
标准 stderr 打印人类可读摘要，便于直接阅读。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

try:  # direct CLI execution
    from srt_timeline import SrtValidationError, group_scenes, parse_srt
except ImportError:  # imported as scripts.parse_srt
    from scripts.srt_timeline import SrtValidationError, group_scenes, parse_srt


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="SRT 解析 + 分镜建议")
    p.add_argument("srt", help="字幕文件路径 (.srt)")
    p.add_argument("--target-sec", type=float, default=30.0, help="每幕目标口播秒数（默认 30）")
    p.add_argument("--min-sec", type=float, default=25.0, help="每幕最短秒数（默认 25）")
    p.add_argument("--max-sec", type=float, default=35.0, help="每幕最长秒数（默认 35）")
    args = p.parse_args(argv)

    try:
        raw = Path(args.srt).read_text(encoding="utf-8-sig")
    except OSError as e:
        print(f"[err] 无法读取字幕: {e}", file=sys.stderr)
        return 1

    try:
        cues = parse_srt(raw)
        scenes = group_scenes(cues, args.target_sec, args.min_sec, args.max_sec)
    except SrtValidationError as exc:
        print(f"[err] SRT 无效: {exc}", file=sys.stderr)
        return 2

    total_ms = cues[-1]["endMs"]
    print(f"字幕条: {len(cues)}  总时长: {total_ms/1000:.1f}s  建议场景: {len(scenes)}", file=sys.stderr)
    for s in scenes:
        print(f"  幕{s['sceneIndex']:>2}  {s['startMs']/1000:6.1f}-{s['endMs']/1000:6.1f}s "
              f"({s['sceneDurationMs']/1000:4.1f}s, 字幕{s['cueRange'][0]}-{s['cueRange'][1]}): "
              f"{s['text'][:40]}", file=sys.stderr)

    json.dump({"cues": cues, "scenes": scenes}, sys.stdout, ensure_ascii=False, indent=2)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
