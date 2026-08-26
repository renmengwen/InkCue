#!/usr/bin/env python3
"""Deterministic authoritative-SRT selection and ASS compilation.

This module intentionally contains no media probing.  The burn CLI consumes
``media_validation`` for that shared responsibility.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from PIL import ImageFont

try:  # package import in tests; direct import when scripts are on sys.path
    from .project_workspace import (
        SUBTITLE_PRESETS,
        Project,
        ProjectValidationError,
        WorkspaceError,
        load_workspace_config,
        sha256_file,
        sha256_json,
    )
    from .srt_timeline import SrtValidationError, parse_srt
except ImportError:  # pragma: no cover - exercised by command-line entry points
    from project_workspace import (
        SUBTITLE_PRESETS,
        Project,
        ProjectValidationError,
        WorkspaceError,
        load_workspace_config,
        sha256_file,
        sha256_json,
    )
    from srt_timeline import SrtValidationError, parse_srt


DEFAULT_FONT_PATH = Path(r"C:\Windows\Fonts\msyh.ttc")
STYLE_CONTRACT_VERSION = "subtitle-style-v1"
BURN_CONTRACT_VERSION = "subtitle-burn-v2"
FINAL_IDENTITY_CONTRACT_VERSION = "final-media-identity-v1"
DISABLED_MUX_CONTRACT_VERSION = "disabled-copy-v1"

SUBTITLE_STYLE: dict[str, Any] = {
    "contractVersion": STYLE_CONTRACT_VERSION,
    "playResX": 1920,
    "playResY": 1080,
    "fontFamily": "Microsoft YaHei",
    "fontSize": 48,
    "bold": False,
    "primaryColour": "&H00FFFFFF",
    "outlineColour": "&H00000000",
    "borderStyle": 1,
    "outline": 3,
    "shadow": 0,
    "alignment": 2,
    "marginL": 96,
    "marginR": 96,
    "marginV": 54,
    "maxLines": 2,
    "maxTextWidthPx": 1728,
}

_ASS_FILTER_RE = re.compile(r"^\s*[.A-Z]{2,3}\s+ass\s+V->V\b", re.MULTILINE)
_PREFERRED_BREAK_AFTER = frozenset("。！？!?；;……，,、：:")


class SubtitleDeliveryError(ValueError):
    """Raised when subtitle identity, compilation, or preflight is invalid."""


class SubtitleStaleError(SubtitleDeliveryError):
    """Raised when the mode's authoritative subtitle identity is not current."""


@dataclass(frozen=True)
class AuthoritativeSrt:
    mode: str
    source_kind: str
    relative_path: str
    path: Path
    sha256: str
    timeline_sha256: str
    cues: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class FontIdentity:
    family: str
    file_name: str
    sha256: str
    path: Path


@dataclass(frozen=True)
class CompiledAss:
    content: bytes
    sha256: str
    style_contract_sha256: str
    font: FontIdentity
    cue_count: int
    first_start_ms: int
    last_end_ms: int


def _read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SubtitleDeliveryError(f"缺少{label}: {path}") from exc
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SubtitleDeliveryError(f"无法读取{label}: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise SubtitleDeliveryError(f"{label}顶层必须是 JSON 对象")
    return value


def _read_srt(path: Path) -> tuple[dict[str, Any], ...]:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError as exc:
        raise SubtitleDeliveryError(f"权威 SRT 缺失: {path}") from exc
    except (OSError, UnicodeError) as exc:
        raise SubtitleDeliveryError(f"无法读取权威 SRT: {path}: {exc}") from exc
    try:
        return tuple(parse_srt(text))
    except SrtValidationError as exc:
        raise SubtitleDeliveryError(f"权威 SRT 无效: {exc}") from exc


def select_authoritative_srt(project: Project) -> AuthoritativeSrt:
    """Select the one legal SRT for the persisted mode, with no fallback."""

    mode = project.voiceover_mode
    timing = project.timing_plan
    active = timing.get("activeTimeline")
    if not isinstance(active, Mapping):
        raise SubtitleDeliveryError("timing plan 缺少 activeTimeline")

    if mode == "disabled":
        relative = "source/source.srt"
        path = project.path(relative)
        actual_sha = sha256_file(path) if path.is_file() else ""
        project_source = project.metadata.get("source")
        project_sha = project_source.get("sha256") if isinstance(project_source, Mapping) else None
        if (
            active.get("kind") != "source-srt"
            or active.get("file") != relative
            or active.get("sha256") != actual_sha
            or timing.get("sourceSrtSha256") != actual_sha
            or project_sha != actual_sha
        ):
            raise SubtitleStaleError("Disabled 权威 source/source.srt 缺失或 stale")
        return AuthoritativeSrt(
            mode=mode,
            source_kind="source-srt",
            relative_path=relative,
            path=path,
            sha256=actual_sha,
            timeline_sha256=actual_sha,
            cues=_read_srt(path),
        )

    if mode not in {"edge-tts", "minimax", "doubao"}:
        raise SubtitleDeliveryError(f"不支持的 voiceoverMode: {mode}")
    timeline_relative = "audio/timeline.json"
    if active.get("kind") not in {"edge-tts-audio-timeline", "audio-authoritative-timeline"} or active.get("file") != timeline_relative:
        raise SubtitleStaleError("音频 timing plan 未绑定 current audio/timeline.json")
    timeline_path = project.path(timeline_relative)
    if not timeline_path.is_file():
        raise SubtitleStaleError("current audio/timeline.json 缺失")
    timeline_sha = sha256_file(timeline_path)
    if active.get("sha256") != timeline_sha:
        raise SubtitleStaleError("audio/timeline.json SHA-256 stale")
    timeline = _read_json(timeline_path, "audio timeline")
    binding = timeline.get("narrationSrt")
    relative = "audio/narration.srt"
    if not isinstance(binding, Mapping) or binding.get("file") != relative:
        raise SubtitleStaleError("current audio timeline 未绑定 audio/narration.srt")
    path = project.path(relative)
    actual_sha = sha256_file(path) if path.is_file() else ""
    if not actual_sha or binding.get("sha256") != actual_sha:
        raise SubtitleStaleError("audio/narration.srt 缺失或 stale，禁止回退 source SRT")
    return AuthoritativeSrt(
        mode=mode,
        source_kind="edge-tts-narration-srt",
        relative_path=relative,
        path=path,
        sha256=actual_sha,
        timeline_sha256=timeline_sha,
        cues=_read_srt(path),
    )


def _resolve_executable(name: str) -> str:
    resolved = shutil.which(name)
    if not resolved:
        raise SubtitleDeliveryError(f"缺少可执行文件: {name}")
    return resolved


def load_font_identity(font_path: str | Path = DEFAULT_FONT_PATH) -> tuple[FontIdentity, Any]:
    path = Path(font_path)
    if not path.is_file():
        raise SubtitleDeliveryError(f"缺少固定字体: {path}")
    try:
        font = ImageFont.truetype(
            str(path),
            SUBTITLE_STYLE["fontSize"],
            layout_engine=ImageFont.Layout.BASIC,
        )
        measured = float(font.getlength("微软雅黑 Microsoft YaHei 123"))
        family = font.getname()[0]
    except Exception as exc:
        raise SubtitleDeliveryError(f"无法加载或度量固定字体: {path}: {exc}") from exc
    if measured <= 0:
        raise SubtitleDeliveryError("固定字体度量结果无效")
    if "YaHei" not in family and "雅黑" not in family:
        raise SubtitleDeliveryError(f"固定字体 family 不符合 Microsoft YaHei: {family}")
    return (
        FontIdentity(
            family=SUBTITLE_STYLE["fontFamily"],
            file_name=path.name,
            sha256=sha256_file(path),
            path=path.resolve(),
        ),
        font,
    )


def preflight_subtitles(
    *,
    font_path: str | Path = DEFAULT_FONT_PATH,
    ffmpeg: str = "ffmpeg",
    ffprobe: str = "ffprobe",
) -> FontIdentity:
    """Require FFmpeg/ffprobe, libass and the exact measurable font."""

    ffmpeg_exe = _resolve_executable(ffmpeg)
    _resolve_executable(ffprobe)
    result = subprocess.run(
        [ffmpeg_exe, "-hide_banner", "-filters"],
        shell=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    listing = (result.stdout or "") + "\n" + (result.stderr or "")
    if result.returncode != 0 or not _ASS_FILTER_RE.search(listing):
        raise SubtitleDeliveryError("FFmpeg 缺少可用的 ass/libass filter")
    identity, _ = load_font_identity(font_path)
    return identity


def _normalise_cue_text(text: str) -> str:
    lines = [re.sub(r"[\t ]+", " ", line.strip()) for line in text.splitlines()]
    lines = [line for line in lines if line]
    if not lines:
        raise SubtitleDeliveryError("字幕 cue 文本不能为空")
    # Preserve an explicit one/two-line author break.  More source lines are
    # flattened deterministically and then reflowed under the two-line limit.
    if len(lines) <= 2:
        return "\n".join(lines)
    return " ".join(lines)


def _width(font: Any, text: str) -> float:
    return float(font.getlength(text))


def _split_score(text: str, index: int, left: str, right: str, font: Any) -> tuple[Any, ...]:
    before = text[index - 1]
    after = text[index] if index < len(text) else ""
    preferred = before in _PREFERRED_BREAK_AFTER or before.isspace() or after.isspace()
    # Prefer legal punctuation/space breaks, then a balanced visual result,
    # then the earliest index for a total deterministic ordering.
    left_width = _width(font, left)
    right_width = _width(font, right)
    return (0 if preferred else 1, abs(left_width - right_width), max(left_width, right_width), index)


def wrap_subtitle_text(text: str, font: Any, *, max_width_px: int = 1728) -> tuple[str, ...]:
    """Deterministically fit text into one or two lines using real font metrics."""

    normalised = _normalise_cue_text(text)
    explicit = tuple(part.strip() for part in normalised.split("\n"))
    if len(explicit) == 2:
        if all(part and _width(font, part) <= max_width_px for part in explicit):
            return explicit
        flat = " ".join(explicit)
    else:
        flat = explicit[0]
    flat = re.sub(r"\s+", " ", flat).strip()
    if _width(font, flat) <= max_width_px:
        return (flat,)

    candidates: list[tuple[tuple[Any, ...], tuple[str, str]]] = []
    for index in range(1, len(flat)):
        left = flat[:index].rstrip()
        right = flat[index:].lstrip()
        if not left or not right:
            continue
        if _width(font, left) <= max_width_px and _width(font, right) <= max_width_px:
            candidates.append((_split_score(flat, index, left, right, font), (left, right)))
    if not candidates:
        raise SubtitleDeliveryError("字幕文本无法在固定字号和 1728px 宽度内放入最多两行，请拆分 cue")
    candidates.sort(key=lambda item: item[0])
    return candidates[0][1]


def escape_ass_text(text: str) -> str:
    """Escape all ASS control introducers in one already-wrapped line."""

    if "\n" in text or "\r" in text:
        raise SubtitleDeliveryError("ASS 单行转义输入不得包含换行")
    return text.replace("\\", r"\\").replace("{", r"\{").replace("}", r"\}")


def _ass_time(milliseconds: int, *, end: bool) -> str:
    if isinstance(milliseconds, bool) or not isinstance(milliseconds, int) or milliseconds < 0:
        raise SubtitleDeliveryError("ASS 时间必须是非负整数毫秒")
    # ASS has centisecond precision: floor starts and ceil ends so a cue is
    # never shortened by compilation.
    centiseconds = (milliseconds + 9) // 10 if end else milliseconds // 10
    hours, remainder = divmod(centiseconds, 360_000)
    minutes, remainder = divmod(remainder, 6_000)
    seconds, cs = divmod(remainder, 100)
    return f"{hours}:{minutes:02d}:{seconds:02d}.{cs:02d}"


def compile_ass(
    cues: Sequence[Mapping[str, Any]],
    *,
    font_path: str | Path = DEFAULT_FONT_PATH,
) -> CompiledAss:
    if not cues:
        raise SubtitleDeliveryError("不能编译空字幕")
    font_identity, font = load_font_identity(font_path)
    style = SUBTITLE_STYLE
    style_contract_sha = sha256_json(style)
    header = (
        "[Script Info]\n"
        "; Generated deterministically by srt-whiteboard-animation\n"
        "ScriptType: v4.00+\n"
        f"PlayResX: {style['playResX']}\n"
        f"PlayResY: {style['playResY']}\n"
        "ScaledBorderAndShadow: yes\n"
        "WrapStyle: 2\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        "Style: Default,Microsoft YaHei,48,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,"
        "0,0,0,0,100,100,0,0,1,3,0,2,96,96,54,1\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    events: list[str] = []
    for ordinal, cue in enumerate(cues, start=1):
        try:
            start_ms = cue["startMs"]
            end_ms = cue["endMs"]
            text = cue["text"]
        except KeyError as exc:
            raise SubtitleDeliveryError(f"sourceOrdinal={ordinal} 缺少字段 {exc.args[0]}") from exc
        if not isinstance(text, str):
            raise SubtitleDeliveryError(f"sourceOrdinal={ordinal} text 必须是字符串")
        lines = wrap_subtitle_text(text, font, max_width_px=style["maxTextWidthPx"])
        safe_text = r"\N".join(escape_ass_text(line) for line in lines)
        events.append(
            "Dialogue: 0,"
            f"{_ass_time(start_ms, end=False)},{_ass_time(end_ms, end=True)},"
            f"Default,,0,0,0,,{safe_text}\n"
        )
    content = (header + "".join(events)).encode("utf-8")
    return CompiledAss(
        content=content,
        sha256=hashlib.sha256(content).hexdigest(),
        style_contract_sha256=style_contract_sha,
        font=font_identity,
        cue_count=len(cues),
        first_start_ms=int(cues[0]["startMs"]),
        last_end_ms=int(cues[-1]["endMs"]),
    )


def find_subtitle_gap(cues: Sequence[Mapping[str, Any]]) -> dict[str, int] | None:
    """Return the first real leading/inter-cue gap, never a fabricated one."""

    previous_end = 0
    for cue in cues:
        start_ms = int(cue["startMs"])
        if start_ms > previous_end:
            return {
                "startMs": previous_end,
                "endMs": start_ms,
                "sampleMs": previous_end + (start_ms - previous_end) // 2,
            }
        previous_end = int(cue["endMs"])
    return None


def subtitle_burn_contract(
    *,
    subtitle_preset: str,
    ass_style_contract_sha256: str,
) -> dict[str, Any]:
    """Build the identity-bearing libx264 subtitle encoding contract."""

    if not isinstance(subtitle_preset, str) or subtitle_preset not in SUBTITLE_PRESETS:
        allowed = " | ".join(sorted(SUBTITLE_PRESETS))
        raise SubtitleDeliveryError(f"subtitlePreset 必须是以下字符串之一: {allowed}")
    if (
        not isinstance(ass_style_contract_sha256, str)
        or len(ass_style_contract_sha256) != 64
        or any(ch not in "0123456789abcdef" for ch in ass_style_contract_sha256)
    ):
        raise SubtitleDeliveryError("ASS style contract SHA-256 无效")
    return {
        "contractVersion": BURN_CONTRACT_VERSION,
        "codec": "libx264",
        "subtitlePreset": subtitle_preset,
        "crf": 18,
        "pixelFormat": "yuv420p",
        "assStyleContractSha256": ass_style_contract_sha256,
    }


def subtitle_burn_contract_sha256(
    *,
    subtitle_preset: str,
    ass_style_contract_sha256: str,
) -> str:
    return sha256_json(
        subtitle_burn_contract(
            subtitle_preset=subtitle_preset,
            ass_style_contract_sha256=ass_style_contract_sha256,
        )
    )


def subtitle_identity(
    selection: AuthoritativeSrt,
    compiled: CompiledAss,
    *,
    subtitle_preset: str = "medium",
) -> str:
    burn_contract_sha = subtitle_burn_contract_sha256(
        subtitle_preset=subtitle_preset,
        ass_style_contract_sha256=compiled.style_contract_sha256,
    )
    return sha256_json(
        {
            "voiceoverMode": selection.mode,
            "sourceKind": selection.source_kind,
            "sourceFile": selection.relative_path,
            "sourceSha256": selection.sha256,
            "timelineSha256": selection.timeline_sha256,
            "styleContractSha256": compiled.style_contract_sha256,
            "burnContractSha256": burn_contract_sha,
            "subtitlePreset": subtitle_preset,
            "fontSha256": compiled.font.sha256,
            "assSha256": compiled.sha256,
        }
    )


def compute_final_identity_inputs(
    *,
    voiceover_mode: str,
    clean_video_sha256: str,
    audio_sha256: str,
    timeline_sha256: str,
    authoritative_subtitle_sha256: str,
    subtitle_style_contract_sha256: str,
    font_sha256: str,
    render_profile_sha256: str,
    timing_plan_sha256: str,
    mux_contract_version: str,
    final_media_sha256: str,
    subtitle_preset: str | None = None,
) -> dict[str, str]:
    """Build the frozen final-media identity payload shared with D2/B2."""

    if subtitle_preset is None:
        try:
            subtitle_preset = load_workspace_config(
                verify_writable=False,
            ).video_encoding.subtitle_preset
        except WorkspaceError as exc:
            raise SubtitleDeliveryError(f"无法读取 current subtitlePreset: {exc}") from exc
    if not isinstance(subtitle_preset, str) or subtitle_preset not in SUBTITLE_PRESETS:
        allowed = " | ".join(sorted(SUBTITLE_PRESETS))
        raise SubtitleDeliveryError(f"subtitlePreset 必须是以下字符串之一: {allowed}")

    values = {
        "contractVersion": FINAL_IDENTITY_CONTRACT_VERSION,
        "voiceoverMode": voiceover_mode,
        "cleanVideoSha256": clean_video_sha256,
        "audioSha256": audio_sha256,
        "timelineSha256": timeline_sha256,
        "authoritativeSubtitleSha256": authoritative_subtitle_sha256,
        "subtitleStyleContractSha256": subtitle_style_contract_sha256,
        "fontSha256": font_sha256,
        "renderProfileSha256": render_profile_sha256,
        "timingPlanSha256": timing_plan_sha256,
        "burnContractVersion": BURN_CONTRACT_VERSION,
        "subtitlePreset": subtitle_preset,
        "muxContractVersion": mux_contract_version,
        "finalMediaSha256": final_media_sha256,
    }
    if voiceover_mode == "disabled" and audio_sha256 != "":
        raise SubtitleDeliveryError("Disabled final identity 的 audioSha256 必须为空字符串")
    for key, value in values.items():
        if not isinstance(value, str) or (key != "audioSha256" and not value):
            raise SubtitleDeliveryError(f"final identity 字段 {key} 必须是非空字符串")
    return values


def compute_final_identity(**kwargs: Any) -> tuple[dict[str, str], str]:
    inputs = compute_final_identity_inputs(**kwargs)
    return inputs, sha256_json(inputs)


__all__ = [
    "AuthoritativeSrt",
    "BURN_CONTRACT_VERSION",
    "CompiledAss",
    "DEFAULT_FONT_PATH",
    "DISABLED_MUX_CONTRACT_VERSION",
    "FINAL_IDENTITY_CONTRACT_VERSION",
    "FontIdentity",
    "STYLE_CONTRACT_VERSION",
    "SUBTITLE_STYLE",
    "SubtitleDeliveryError",
    "SubtitleStaleError",
    "compile_ass",
    "compute_final_identity",
    "compute_final_identity_inputs",
    "escape_ass_text",
    "find_subtitle_gap",
    "load_font_identity",
    "preflight_subtitles",
    "select_authoritative_srt",
    "subtitle_burn_contract",
    "subtitle_burn_contract_sha256",
    "subtitle_identity",
    "wrap_subtitle_text",
]
