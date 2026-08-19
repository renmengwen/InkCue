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


def _element_label(element: Mapping[str, Any], index: int) -> str:
    """Return a stable preview label without expanding the annotation contract."""

    for field in ("label", "narrativeRole", "subtitle"):
        value = element.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return f"元素 {index}"


def _hand_path(element: Mapping[str, Any]) -> tuple[tuple[int, int], tuple[int, int]]:
    """Use authored preview metadata or derive it deterministically from the region."""

    authored = element.get("handPath")
    if isinstance(authored, Mapping):
        start = authored.get("start")
        end = authored.get("end")
        if (
            isinstance(start, (list, tuple))
            and isinstance(end, (list, tuple))
            and len(start) == 2
            and len(end) == 2
            and all(isinstance(value, int) and not isinstance(value, bool) for value in (*start, *end))
        ):
            return (start[0], start[1]), (end[0], end[1])

    region = element["region"]
    x, y = region["x"], region["y"]
    width, height = region["width"], region["height"]
    inset_x = min(max(12, width // 10), max(12, width // 3))
    inset_y = min(max(12, height // 10), max(12, height // 3))
    left, right = x + inset_x, x + width - inset_x
    top, bottom = y + inset_y, y + height - inset_y
    center_x, center_y = x + width // 2, y + height // 2
    direction = element.get("reveal", {}).get("direction", "left-to-right")
    if direction == "right-to-left":
        return (right, center_y), (left, center_y)
    if direction == "top-to-bottom":
        return (center_x, top), (center_x, bottom)
    if direction == "bottom-to-top":
        return (center_x, bottom), (center_x, top)
    return (left, center_y), (right, center_y)


def _arrow_points(start: tuple[int, int], end: tuple[int, int]) -> tuple[tuple[int, int], ...]:
    dx, dy = end[0] - start[0], end[1] - start[1]
    length = max(1.0, (dx * dx + dy * dy) ** 0.5)
    back_x, back_y = -dx / length, -dy / length
    side_x, side_y = -dy / length, dx / length
    return (
        end,
        (round(end[0] + back_x * 13 + side_x * 7), round(end[1] + back_y * 13 + side_y * 7)),
        (round(end[0] + back_x * 13 - side_x * 7), round(end[1] + back_y * 13 - side_y * 7)),
    )


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
        direction = element.get("reveal", {}).get("direction", "left-to-right")
        label = f"{index}. {_element_label(element, index)}  {direction}"
        draw.rounded_rectangle((x + 52, y + 8, min(right - 8, x + 52 + len(label) * 19), y + 46), radius=6, fill=(255, 255, 255, 225))
        draw.text((x + 60, y + 12), label, font=small_font, fill=color)
        start, end = _hand_path(element)
        draw.line((start, end), fill=color, width=4)
        draw.polygon(_arrow_points(start, end), fill=color)

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
