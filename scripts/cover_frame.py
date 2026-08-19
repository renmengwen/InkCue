"""Optional social-cover first-frame replacement shared by delivery stages."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any, Mapping

try:
    from .project_workspace import Project, ProjectValidationError, sha256_file
except ImportError:  # pragma: no cover - direct script execution
    from project_workspace import Project, ProjectValidationError, sha256_file

COVER_RELATIVE_PATH = "previews/social-cover.png"
COVER_FRAME_RANGE = {"startFrame": 0, "endFrameExclusive": 1}


def cover_path(project: Project) -> Path:
    return project.path(COVER_RELATIVE_PATH)


def cover_record(project: Project) -> dict[str, Any] | None:
    """Return deterministic cover identity, or None when cover is not present."""
    path = cover_path(project)
    if not path.is_file():
        return None
    try:
        digest = sha256_file(path)
        size = path.stat().st_size
    except OSError as exc:
        raise ProjectValidationError(f"封面文件无法读取: {path}") from exc
    return {
        "file": COVER_RELATIVE_PATH,
        "sha256": digest,
        "bytes": size,
        "frameRange": dict(COVER_FRAME_RANGE),
        "visualReviewExcluded": True,
    }


def replace_first_frame(
    source: Path,
    target: Path,
    *,
    project: Project,
    expected_frame_count: int,
    ffmpeg: str = "ffmpeg",
) -> dict[str, Any] | None:
    """Replace decoded frame 0 with the social cover while preserving frame count.

    A missing cover is intentionally a no-op for backwards compatibility. The
    resulting file is always written to ``target`` and remains silent video.
    """
    record = cover_record(project)
    if record is None:
        shutil.copyfile(source, target)
        return None
    executable = shutil.which(ffmpeg) or ffmpeg
    profile = project.render_profile
    width, height, fps = int(profile["width"]), int(profile["height"]), int(profile["fps"])
    # The cover input is trimmed to exactly one frame; the source starts at
    # decoded frame 1. concat therefore preserves the authoritative count.
    filter_complex = (
        f"[0:v]trim=end_frame=1,setpts=N/({fps}*TB),scale={width}:{height},setsar=1[cover];"
        f"[1:v]trim=start_frame=1,setpts=N/({fps}*TB)[rest];"
        "[cover][rest]concat=n=2:v=1:a=0,format=yuv420p,setpts=N/("
        f"{fps}*TB)[v]"
    )
    completed = subprocess.run(
        [
            executable,
            "-y",
            "-loglevel",
            "error",
            "-loop",
            "1",
            "-framerate",
            str(fps),
            "-i",
            str(cover_path(project).resolve()),
            "-i",
            str(source.resolve()),
            "-filter_complex",
            filter_complex,
            "-map",
            "[v]",
            "-frames:v",
            str(expected_frame_count),
            "-an",
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            "18",
            "-pix_fmt",
            str(profile["pixelFormat"]),
            "-r",
            str(fps),
            "-movflags",
            "+faststart",
            str(target.resolve()),
        ],
        shell=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "无错误输出").strip()[-2000:]
        raise ProjectValidationError(f"封面首帧替换失败: {detail}")
    return record


def attach_cover_manifest(manifest: dict[str, Any], record: Mapping[str, Any] | None) -> None:
    """Keep a stable top-level cover field in delivery manifests."""
    manifest["cover"] = dict(record) if record is not None else None


def attach_cover_review_manifest(manifest: dict[str, Any], project: Project) -> None:
    """Embed the validated cover-manifest evidence used by final-media checks."""
    try:
        from .cover_review import CoverReviewError, load_cover_review
    except ImportError:  # pragma: no cover - direct script execution
        from cover_review import CoverReviewError, load_cover_review
    try:
        review = load_cover_review(project)
    except CoverReviewError as exc:
        raise ProjectValidationError(f"封面 review evidence 无效: {exc}") from exc
    manifest["coverReview"] = review
