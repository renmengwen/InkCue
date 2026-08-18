from __future__ import annotations

import json
import sys
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont


_FONT_FILE = "C:/Windows/Fonts/msyh.ttc"
_FONT_LOCAL = threading.local()
_COLORS = (
    (38, 103, 255, 225),
    (255, 105, 92, 225),
    (41, 167, 102, 225),
    (181, 100, 255, 225),
)


def _label_font() -> ImageFont.FreeTypeFont:
    font = getattr(_FONT_LOCAL, "label_font", None)
    if font is None:
        font = ImageFont.truetype(_FONT_FILE, 18)
        _FONT_LOCAL.label_font = font
    return font


def render_annotation_preview(
    source: Image.Image,
    annotation: Mapping[str, Any],
) -> Image.Image:
    """Return an RGB annotation preview without reading or writing files."""

    image = source.convert("RGBA")
    overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    small_font = _label_font()
    elements = annotation.get("elements")
    if not isinstance(elements, list):
        raise ValueError("annotation.elements 必须是数组")
    for index, element in enumerate(elements, start=1):
        region = element["region"]
        x, y = region["x"], region["y"]
        right, bottom = x + region["width"], y + region["height"]
        color = _COLORS[(index - 1) % len(_COLORS)]
        fill = (*color[:3], 24)
        draw.rounded_rectangle((x, y, right, bottom), radius=12, outline=color, width=4, fill=fill)
        draw.ellipse((x + 8, y + 8, x + 44, y + 44), fill=color)
        draw.text((x + 19, y + 8), str(index), anchor="ma", font=small_font, fill="white")
        label = f"{index}. {element['label']}  {element['reveal']['direction']}"
        draw.rounded_rectangle((x + 52, y + 8, min(right - 8, x + 52 + len(label) * 19), y + 46), radius=6, fill=(255, 255, 255, 225))
        draw.text((x + 60, y + 12), label, font=small_font, fill=color)
        start = tuple(element["handPath"]["start"])
        end = tuple(element["handPath"]["end"])
        draw.line((start, end), fill=color, width=4)
        draw.polygon((end, (end[0] - 13, end[1] - 7), (end[0] - 13, end[1] + 7)), fill=color)

    return Image.alpha_composite(image, overlay).convert("RGB")


def render_annotation_preview_file(
    image_path: str | Path,
    annotation_path: str | Path,
    output_path: str | Path,
) -> None:
    annotation = json.loads(Path(annotation_path).read_text(encoding="utf-8"))
    with Image.open(image_path) as source:
        source.load()
        result = render_annotation_preview(source, annotation)
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    result.save(output, format="PNG", compress_level=1, optimize=False)


def main(image_path: str, annotation_path: str, output_path: str) -> None:
    """Backward-compatible thin CLI wrapper for a single preview."""

    render_annotation_preview_file(image_path, annotation_path, output_path)


if __name__ == "__main__":
    main(*sys.argv[1:4])
