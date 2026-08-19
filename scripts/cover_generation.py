#!/usr/bin/env python3
"""Generate a deterministic whole-video social cover.

The cover is deliberately not a scene source image.  Semantic inputs are
collected from the whole content package (topic/body, narration cues and every
scene's coreIdea/visualSubject/imagePrompt), while the pixels are composed
locally so the title is stable and does not depend on a provider rendering
Chinese text correctly.  If no scene image exists, a clean whiteboard canvas is
used as a safe fallback.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import unicodedata
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont, ImageFilter


WIDTH = 1920
HEIGHT = 1080
BACKGROUND = (245, 235, 215)
MANIFEST_VERSION = "whole-video-cover-v1"


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    value = json.loads(path.read_text(encoding="utf-8"))
    return value if isinstance(value, dict) else None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _parse_srt(path: Path) -> list[str]:
    if not path.is_file():
        return []
    lines = path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
    cues: list[str] = []
    current: list[str] = []
    for line in lines + [""]:
        stripped = line.strip()
        if not stripped:
            if current:
                cues.append(" ".join(current))
                current = []
            continue
        if stripped.isdigit() or "-->" in stripped:
            continue
        current.append(stripped)
    return cues


def collect_semantics(project_root: str | Path) -> dict[str, Any]:
    """Collect the whole-video semantic snapshot used by cover generation."""
    root = Path(project_root).resolve()
    plan_path = root / "planning" / "generation-plan.json"
    plan = _read_json(plan_path) or {}
    source_input_path = root / "source" / "input.json"
    source_input = _read_json(source_input_path) or {}
    topic = _text(source_input.get("topic"))
    body = _text(source_input.get("body"))
    cues_raw = source_input.get("narrationCues")
    cues: list[str] = []
    if isinstance(cues_raw, list):
        for cue in cues_raw:
            if isinstance(cue, dict) and _text(cue.get("text")):
                cues.append(_text(cue["text"]))
    if not cues:
        cues = _parse_srt(root / "source" / "source.srt")

    # Content source has richer scene records; generation-plan remains the
    # authoritative list and may carry additional prompt fields in older runs.
    content_scenes = {
        str(item.get("sceneId")): item
        for item in (source_input.get("scenes") or [])
        if isinstance(item, dict) and _text(item.get("sceneId"))
    }
    plan_scenes = plan.get("scenes") if isinstance(plan.get("scenes"), list) else []
    scenes: list[dict[str, Any]] = []
    for item in plan_scenes:
        if not isinstance(item, dict):
            continue
        scene_id = _text(item.get("sceneId"))
        rich = content_scenes.get(scene_id, {})
        scenes.append(
            {
                "sceneId": scene_id,
                "coreIdea": _text(item.get("coreIdea")) or _text(rich.get("coreIdea")),
                "visualSubject": _text(item.get("visualSubject")) or _text(rich.get("visualSubject")),
                "imagePrompt": _text(item.get("imagePrompt")) or _text(item.get("prompt")) or _text(rich.get("imagePrompt")),
                "outputFile": _text(item.get("outputFile")),
            }
        )
    # A content package can be inspected before a formal plan is copied. Keep
    # all rich scenes in the snapshot rather than silently dropping them.
    seen = {item["sceneId"] for item in scenes}
    for scene_id, item in content_scenes.items():
        if scene_id not in seen:
            scenes.append(
                {
                    "sceneId": scene_id,
                    "coreIdea": _text(item.get("coreIdea")),
                    "visualSubject": _text(item.get("visualSubject")),
                    "imagePrompt": _text(item.get("imagePrompt")),
                    "outputFile": "",
                }
            )

    # Fallback title is whole-video based: topic, then body/cues, then scene
    # ideas. It intentionally never uses only scene-01 as its meaning.
    first_sentence = (body or "").splitlines()[0].strip() if body else ""
    if not first_sentence and cues:
        first_sentence = cues[0]
    ideas = [item["coreIdea"] for item in scenes if item.get("coreIdea")]
    title = topic or first_sentence or (ideas[0] if ideas else "白板动画")
    title = " ".join(title.split())
    if len(title) > 34:
        title = title[:33].rstrip() + "…"
    conclusion = ideas[-1] if ideas else (cues[-1] if cues else "")
    if len(conclusion) > 52:
        conclusion = conclusion[:51].rstrip() + "…"
    return {
        "semanticSource": "whole_video",
        "topic": topic or None,
        "body": body or None,
        "narrationCues": cues,
        "scenes": scenes,
        "title": title,
        "subtitle": conclusion,
        "planSha256": _sha256(plan_path) if plan_path.is_file() else None,
        "sourceInputSha256": _sha256(source_input_path) if source_input_path.is_file() else None,
        "sourceSrtSha256": _sha256(root / "source" / "source.srt") if (root / "source" / "source.srt").is_file() else None,
    }


def _font(size: int, *, bold: bool = False) -> ImageFont.ImageFont:
    candidates = [
        Path("C:/Windows/Fonts/msyhbd.ttc" if bold else "C:/Windows/Fonts/msyh.ttc"),
        Path("C:/Windows/Fonts/simhei.ttf" if bold else "C:/Windows/Fonts/simsun.ttc"),
        Path("/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc" if bold else "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
    ]
    for path in candidates:
        if path.is_file():
            try:
                return ImageFont.truetype(str(path), size=size)
            except OSError:
                pass
    return ImageFont.load_default()


def _wrap_title(text: str, font: ImageFont.ImageFont, max_width: int) -> list[str]:
    # Width-aware deterministic wrapping, including CJK characters without
    # relying on whitespace tokenization.
    lines: list[str] = []
    current = ""
    for char in text:
        candidate = current + char
        if current and font.getlength(candidate) > max_width:
            lines.append(current)
            current = char
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines or ["白板动画"]


def _pick_background(root: Path, scenes: list[dict[str, Any]]) -> tuple[Image.Image, str | None]:
    for scene in scenes:
        name = scene.get("outputFile")
        if not name:
            continue
        candidate = root / "scenes" / name
        if candidate.is_file():
            try:
                return Image.open(candidate).convert("RGB"), scene.get("sceneId")
            except OSError:
                continue
    # Rich scene records may not have outputFile in an older project.
    for candidate in sorted((root / "scenes").glob("*.png")) if (root / "scenes").is_dir() else []:
        try:
            return Image.open(candidate).convert("RGB"), candidate.stem
        except OSError:
            pass
    return Image.new("RGB", (WIDTH, HEIGHT), BACKGROUND), None


def generate_cover(project_root: str | Path, *, overwrite: bool = False, title: str | None = None) -> dict[str, Any]:
    root = Path(project_root).resolve()
    semantics = collect_semantics(root)
    output = root / "previews" / "social-cover.png"
    manifest_path = root / "manifests" / "cover-manifest.json"
    if output.exists() and not overwrite:
        raise FileExistsError(f"封面已存在；如需替换请传 --overwrite: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)

    image, source_scene = _pick_background(root, semantics["scenes"])
    image = image.resize((WIDTH, HEIGHT), Image.Resampling.LANCZOS)
    # Keep the whiteboard character but make deterministic title readable.
    image = image.filter(ImageFilter.GaussianBlur(radius=0.35))
    overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    draw.rounded_rectangle((70, 80, WIDTH - 70, HEIGHT - 90), radius=34, fill=(255, 250, 239, 42), outline=(40, 32, 22, 80), width=3)
    draw.rectangle((90, 690, WIDTH - 90, HEIGHT - 115), fill=(34, 28, 20, 178))
    image = Image.alpha_composite(image.convert("RGBA"), overlay)
    draw = ImageDraw.Draw(image)
    title_text = _text(title) or semantics["title"]
    title_font = _font(96, bold=True)
    lines = _wrap_title(title_text, title_font, WIDTH - 300)
    y = 735 - (len(lines) - 1) * 52
    for line in lines:
        box = draw.textbbox((0, 0), line, font=title_font, stroke_width=2)
        x = (WIDTH - (box[2] - box[0])) // 2
        draw.text((x, y), line, font=title_font, fill=(255, 251, 240), stroke_width=3, stroke_fill=(20, 16, 12))
        y += 108
    subtitle = semantics.get("subtitle") or ""
    if subtitle:
        sub_font = _font(34)
        if len(subtitle) > 48:
            subtitle = subtitle[:47].rstrip() + "…"
        box = draw.textbbox((0, 0), subtitle, font=sub_font)
        draw.text(((WIDTH - (box[2] - box[0])) // 2, HEIGHT - 175), subtitle, font=sub_font, fill=(245, 220, 172))
    image.convert("RGB").save(output, format="PNG", optimize=False)

    manifest = {
        "schemaVersion": 1,
        "contractVersion": MANIFEST_VERSION,
        "semanticSource": "whole_video",
        "projectId": _text((_read_json(root / "project.json") or {}).get("projectId")) or None,
        "file": "previews/social-cover.png",
        "width": WIDTH,
        "height": HEIGHT,
        "title": title_text,
        "subtitle": subtitle,
        "sourceSceneIds": [source_scene] if source_scene else [],
        "visualReviewExcluded": True,
        "coverFrameRange": {"startFrame": 0, "endFrameExclusive": 1},
        "sha256": _sha256(output),
        "semanticInputs": semantics,
    }
    tmp = manifest_path.with_name(f".{manifest_path.name}.tmp")
    tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, manifest_path)
    return manifest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="根据全片内容生成独立社交平台封面")
    parser.add_argument("--project", required=True, help="项目根目录")
    parser.add_argument("--overwrite", action="store_true", help="允许替换已有封面")
    parser.add_argument("--title", help="可选的确定性覆盖标题")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        manifest = generate_cover(args.project, overwrite=args.overwrite, title=args.title)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"ok": False, "command": "cover_generation", "error": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps({"ok": True, "command": "cover_generation", "file": manifest["file"], "sha256": manifest["sha256"], "semanticSource": "whole_video"}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
