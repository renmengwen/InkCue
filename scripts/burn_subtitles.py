#!/usr/bin/env python3
"""Burn the current authoritative SRT and publish captioned delivery media."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any, Mapping, Sequence

from PIL import Image, ImageOps

try:
    from .project_workspace import (
        Project,
        ProjectValidationError,
        WorkspaceConfig,
        WorkspaceError,
        load_project,
        load_workspace_config,
        sha256_file,
        sha256_json,
        write_json_atomic,
    )
    from .subtitle_delivery import (
        BURN_CONTRACT_VERSION,
        DEFAULT_FONT_PATH,
        DISABLED_MUX_CONTRACT_VERSION,
        SUBTITLE_STYLE,
        SubtitleDeliveryError,
        SubtitleStaleError,
        compile_ass,
        compute_final_identity,
        find_subtitle_gap,
        preflight_subtitles,
        select_authoritative_srt,
        subtitle_burn_contract,
        subtitle_burn_contract_sha256,
        subtitle_identity,
    )
except ImportError:  # pragma: no cover - direct script execution
    from project_workspace import (
        Project,
        ProjectValidationError,
        WorkspaceConfig,
        WorkspaceError,
        load_project,
        load_workspace_config,
        sha256_file,
        sha256_json,
        write_json_atomic,
    )
    from subtitle_delivery import (
        BURN_CONTRACT_VERSION,
        DEFAULT_FONT_PATH,
        DISABLED_MUX_CONTRACT_VERSION,
        SUBTITLE_STYLE,
        SubtitleDeliveryError,
        SubtitleStaleError,
        compile_ass,
        compute_final_identity,
        find_subtitle_gap,
        preflight_subtitles,
        select_authoritative_srt,
        subtitle_burn_contract,
        subtitle_burn_contract_sha256,
        subtitle_identity,
    )


DELIVERY_SCHEMA_VERSION = 1
DELIVERY_TOP_LEVEL_KEYS = (
    "schemaVersion",
    "projectId",
    "voiceoverMode",
    "timingPlan",
    "cleanVideo",
    "subtitles",
    "captionedVideo",
    "final",
    "finalApproval",
)


def _media_validation_module() -> Any:
    try:
        from . import media_validation
    except ImportError:
        try:
            import media_validation
        except ImportError as exc:
            raise SubtitleDeliveryError("缺少共享 scripts/media_validation.py") from exc
    return media_validation


def _run(argv: Sequence[str], *, cwd: Path) -> None:
    if "-shortest" in argv:
        raise SubtitleDeliveryError("字幕/最终媒体命令禁止使用 -shortest")
    result = subprocess.run(
        list(argv),
        shell=False,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "未知错误").strip()
        raise SubtitleDeliveryError(f"FFmpeg 执行失败: {detail[-2000:]}")


def _expected_frame_count(project: Project) -> int:
    scenes = project.timing_plan.get("scenes")
    if not isinstance(scenes, list) or not scenes:
        raise SubtitleDeliveryError("timing plan 缺少 scenes")
    count = scenes[-1].get("endFrameExclusive")
    if isinstance(count, bool) or not isinstance(count, int) or count <= 0:
        raise SubtitleDeliveryError("timing plan 全局帧数无效")
    if sum(int(scene.get("frameCount", -1)) for scene in scenes) != count:
        raise SubtitleDeliveryError("timing plan 累计帧数合同不一致")
    return count


def _timing_plan_sha(project: Project) -> str:
    if project.timing_plan_persisted:
        return sha256_file(project.timing_plan_path)
    return sha256_json(project.timing_plan)


def _sample_times(cues: Sequence[Mapping[str, Any]]) -> tuple[list[dict[str, Any]], Any]:
    indexes = sorted({0, len(cues) // 2, len(cues) - 1})
    samples = [
        {
            "kind": "cue",
            "sourceOrdinal": int(cues[index].get("sourceOrdinal", index + 1)),
            "timestampMs": (int(cues[index]["startMs"]) + int(cues[index]["endMs"])) // 2,
        }
        for index in indexes
    ]
    gap = find_subtitle_gap(cues)
    if gap is not None:
        samples.append({"kind": "gap", "timestampMs": gap["sampleMs"]})
    return samples, gap


def _generate_contact_sheet(
    *,
    ffmpeg_exe: str,
    video: Path,
    cues: Sequence[Mapping[str, Any]],
    run_dir: Path,
) -> tuple[Path, list[dict[str, Any]], Any]:
    samples, gap = _sample_times(cues)
    frames: list[Image.Image] = []
    frame_paths: list[Path] = []
    try:
        for index, sample in enumerate(samples, start=1):
            frame_path = run_dir / f"contact-{index:02d}.png"
            frame_paths.append(frame_path)
            timestamp = f"{sample['timestampMs'] / 1000:.3f}"
            _run(
                [
                    ffmpeg_exe,
                    "-y",
                    "-loglevel",
                    "error",
                    "-ss",
                    timestamp,
                    "-i",
                    str(video.resolve()),
                    "-frames:v",
                    "1",
                    "-vf",
                    "scale=640:360:force_original_aspect_ratio=decrease",
                    str(frame_path.resolve()),
                ],
                cwd=run_dir,
            )
            with Image.open(frame_path) as image:
                tile = ImageOps.pad(image.convert("RGB"), (640, 360), color=(245, 235, 215))
                frames.append(tile.copy())
        columns = 2
        rows = (len(frames) + columns - 1) // columns
        sheet = Image.new("RGB", (columns * 640, rows * 360), color=(245, 235, 215))
        for index, frame in enumerate(frames):
            sheet.paste(frame, ((index % columns) * 640, (index // columns) * 360))
        candidate = run_dir / "final-subtitle-contact-sheet.tmp.png"
        sheet.save(candidate, format="PNG", optimize=False)
        return candidate, samples, gap
    finally:
        for frame in frames:
            frame.close()


def _load_delivery_manifest(project: Project) -> dict[str, Any]:
    path = project.path("manifests/delivery-manifest.json")
    if path.is_file():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise SubtitleDeliveryError(f"delivery manifest 无法读取: {exc}") from exc
        if not isinstance(existing, dict):
            raise SubtitleDeliveryError("delivery manifest 顶层必须是对象")
        if existing.get("schemaVersion") != DELIVERY_SCHEMA_VERSION:
            raise SubtitleDeliveryError("delivery manifest schemaVersion 不受支持")
        if existing.get("projectId") != project.project_id:
            raise SubtitleDeliveryError("delivery manifest projectId 不匹配")
        if existing.get("voiceoverMode") != project.voiceover_mode:
            raise SubtitleDeliveryError("delivery manifest voiceoverMode stale")
    else:
        existing = {}
    return {
        "schemaVersion": DELIVERY_SCHEMA_VERSION,
        "projectId": project.project_id,
        "voiceoverMode": project.voiceover_mode,
        "timingPlan": existing.get("timingPlan", {}),
        "cleanVideo": existing.get("cleanVideo", {}),
        "subtitles": existing.get("subtitles", {}),
        "captionedVideo": existing.get("captionedVideo", {}),
        "final": existing.get("final", {}),
        "finalApproval": existing.get("finalApproval"),
    }


def _media_record(relative_file: str, validation: Mapping[str, Any]) -> dict[str, Any]:
    record = {"file": relative_file}
    media_fields = dict(validation)
    contract_validation = media_fields.pop("validation", None)
    record.update(media_fields)
    if contract_validation is not None:
        record["technicalValidation"] = contract_validation
    return record


def _encoding_record(*, subtitle_preset: str, ass_style_contract_sha256: str) -> dict[str, Any]:
    contract = subtitle_burn_contract(
        subtitle_preset=subtitle_preset,
        ass_style_contract_sha256=ass_style_contract_sha256,
    )
    return {
        **contract,
        "contractSha256": subtitle_burn_contract_sha256(
            subtitle_preset=subtitle_preset,
            ass_style_contract_sha256=ass_style_contract_sha256,
        ),
    }


def _with_encoding_receipt(
    validation: Mapping[str, Any],
    encoding: Mapping[str, Any],
) -> dict[str, Any]:
    result = dict(validation)
    technical = result.get("validation")
    if not isinstance(technical, Mapping):
        raise SubtitleDeliveryError("媒体验证缺少 technical receipt")
    result["validation"] = {
        **dict(technical),
        "subtitleEncoding": dict(encoding),
    }
    return result


def _record_matches_media(record: Mapping[str, Any], media: Mapping[str, Any], *, file: str) -> bool:
    return (
        record.get("file") == file
        and record.get("sha256") == media.get("sha256")
        and record.get("bytes") == media.get("bytes")
        and record.get("durationMs") == media.get("durationMs")
        and record.get("formatName") == media.get("formatName")
        and record.get("streams") == media.get("streams")
    )


def _reuse_current_burn(
    *,
    project: Project,
    manifest: dict[str, Any],
    selection: Any,
    compiled: Any,
    subtitle_identity_sha256: str,
    encoding: Mapping[str, Any],
    clean_validation: Mapping[str, Any],
    expected_frames: int,
    media: Any,
    force_deep: bool,
) -> bool:
    subtitles = manifest.get("subtitles")
    captioned = manifest.get("captionedVideo")
    if not isinstance(subtitles, Mapping) or not isinstance(captioned, Mapping):
        return False
    style = subtitles.get("style")
    ass = style.get("ass") if isinstance(style, Mapping) else None
    contact = subtitles.get("contactSheet")
    captioned_technical = captioned.get("technicalValidation")
    expected_encoding = dict(encoding)
    expected_subtitle_fields = {
        "sourceKind": selection.source_kind,
        "file": selection.relative_path,
        "sha256": selection.sha256,
        "timelineSha256": selection.timeline_sha256,
        "subtitleIdentitySha256": subtitle_identity_sha256,
    }
    if any(subtitles.get(key) != value for key, value in expected_subtitle_fields.items()):
        return False
    if (
        not isinstance(style, Mapping)
        or style.get("contractSha256") != encoding.get("contractSha256")
        or style.get("assStyleContractSha256") != compiled.style_contract_sha256
        or not isinstance(ass, Mapping)
        or ass.get("file") != "subtitles/final.ass"
        or ass.get("sha256") != compiled.sha256
        or ass.get("bytes") != len(compiled.content)
        or subtitles.get("encoding") != expected_encoding
        or captioned.get("cleanVideoSha256") != clean_validation.get("sha256")
        or captioned.get("subtitleIdentitySha256") != subtitle_identity_sha256
        or captioned.get("burnContractVersion") != BURN_CONTRACT_VERSION
        or captioned.get("burnContractSha256") != encoding.get("contractSha256")
        or not isinstance(captioned_technical, Mapping)
        or captioned_technical.get("subtitleEncoding") != expected_encoding
    ):
        return False
    ass_path = project.path("subtitles/final.ass")
    contact_path = project.path("previews/final-subtitle-contact-sheet.png")
    if (
        not ass_path.is_file()
        or sha256_file(ass_path) != compiled.sha256
        or not isinstance(contact, Mapping)
        or contact.get("file") != "previews/final-subtitle-contact-sheet.png"
        or not contact_path.is_file()
        or contact.get("sha256") != sha256_file(contact_path)
        or contact.get("bytes") != contact_path.stat().st_size
    ):
        return False
    captioned_media = media.validate_video(
        project.path("output/final-subtitled-video-only.mp4"),
        render_profile=project.render_profile,
        expected_frame_count=expected_frames,
        expected_audio_streams=0,
        deep_receipt=captioned_technical,
        force_deep=force_deep,
    )
    if not _record_matches_media(
        captioned,
        captioned_media,
        file="output/final-subtitled-video-only.mp4",
    ):
        raise SubtitleStaleError("captionedVideo receipt 与 current 正式媒体不一致")
    captioned_media = _with_encoding_receipt(captioned_media, encoding)
    manifest["captionedVideo"] = {
        **dict(captioned),
        **_media_record("output/final-subtitled-video-only.mp4", captioned_media),
    }

    if project.voiceover_mode == "disabled":
        final = manifest.get("final")
        if not isinstance(final, Mapping):
            return False
        final_technical = final.get("technicalValidation")
        if (
            not isinstance(final_technical, Mapping)
            or final_technical.get("subtitleEncoding") != expected_encoding
        ):
            return False
        final_media = media.validate_video(
            project.path("output/final.mp4"),
            render_profile=project.render_profile,
            expected_frame_count=expected_frames,
            expected_audio_streams=0,
            deep_receipt=final_technical,
            force_deep=force_deep,
        )
        if (
            not _record_matches_media(final, final_media, file="output/final.mp4")
            or final_media.get("sha256") != captioned_media.get("sha256")
        ):
            raise SubtitleStaleError("Disabled final receipt 与 current captioned media 不一致")
        expected_inputs, expected_identity = compute_final_identity(
            voiceover_mode="disabled",
            clean_video_sha256=str(clean_validation["sha256"]),
            audio_sha256="",
            timeline_sha256=selection.timeline_sha256,
            authoritative_subtitle_sha256=selection.sha256,
            subtitle_style_contract_sha256=str(encoding["contractSha256"]),
            font_sha256=compiled.font.sha256,
            render_profile_sha256=project.timing_plan["renderProfileSha256"],
            timing_plan_sha256=_timing_plan_sha(project),
            mux_contract_version=DISABLED_MUX_CONTRACT_VERSION,
            final_media_sha256=str(final_media["sha256"]),
            subtitle_preset=str(encoding["subtitlePreset"]),
        )
        if final.get("identityInputs") != expected_inputs or final.get("finalIdentitySha256") != expected_identity:
            return False
        final_media = _with_encoding_receipt(final_media, encoding)
        manifest["final"] = {
            **dict(final),
            **_media_record("output/final.mp4", final_media),
        }
    write_json_atomic(project.path("manifests/delivery-manifest.json"), manifest)
    return True


def burn_project(
    project_root: str | Path,
    *,
    font_path: str | Path = DEFAULT_FONT_PATH,
    ffmpeg: str = "ffmpeg",
    ffprobe: str = "ffprobe",
    run_id: str | None = None,
    force_deep: bool = False,
    subtitle_preset: str = "medium",
) -> dict[str, Any]:
    """Compile, burn, validate, publish, and update only B1-owned manifest fields."""

    project = load_project(project_root)
    selection = select_authoritative_srt(project)
    font = preflight_subtitles(font_path=font_path, ffmpeg=ffmpeg, ffprobe=ffprobe)
    compiled = compile_ass(selection.cues, font_path=font.path)
    if compiled.font.sha256 != font.sha256:
        raise SubtitleDeliveryError("preflight 与 ASS 编译使用的字体 identity 不一致")
    encoding = _encoding_record(
        subtitle_preset=subtitle_preset,
        ass_style_contract_sha256=compiled.style_contract_sha256,
    )
    sub_identity = subtitle_identity(
        selection,
        compiled,
        subtitle_preset=subtitle_preset,
    )

    media = _media_validation_module()
    expected_frames = _expected_frame_count(project)
    render_profile = project.render_profile
    clean_relative = "output/final-video-only.mp4"
    clean_path = project.path(clean_relative)
    manifest = _load_delivery_manifest(project)
    clean_record = manifest.get("cleanVideo")
    clean_receipt = (
        clean_record.get("technicalValidation")
        if isinstance(clean_record, Mapping)
        else None
    )
    clean_validation = media.validate_video(
        clean_path,
        render_profile=render_profile,
        expected_frame_count=expected_frames,
        expected_audio_streams=0,
        deep_receipt=clean_receipt if isinstance(clean_receipt, Mapping) else None,
        force_deep=force_deep,
    )
    if _reuse_current_burn(
        project=project,
        manifest=manifest,
        selection=selection,
        compiled=compiled,
        subtitle_identity_sha256=sub_identity,
        encoding=encoding,
        clean_validation=clean_validation,
        expected_frames=expected_frames,
        media=media,
        force_deep=force_deep,
    ):
        return manifest

    run_name = run_id or f"subtitle-{uuid.uuid4().hex}"
    if not run_name.startswith("subtitle-") or not run_name.isascii():
        raise SubtitleDeliveryError("字幕 runId 必须为 subtitle- 前缀的 ASCII 名称")
    run_dir = project.create_run_dir(run_name)
    published = False
    completed = False
    try:
        burn_ass = run_dir / "burn.ass"
        burn_ass.write_bytes(compiled.content)
        fonts_dir = run_dir / "fonts"
        fonts_dir.mkdir()
        copied_font = fonts_dir / "msyh.ttc"
        shutil.copyfile(font.path, copied_font)
        if sha256_file(copied_font) != compiled.font.sha256:
            raise SubtitleDeliveryError("工作目录字体副本 SHA-256 不一致")

        ffmpeg_exe = shutil.which(ffmpeg)
        if not ffmpeg_exe:
            raise SubtitleDeliveryError(f"缺少可执行文件: {ffmpeg}")
        caption_candidate = run_dir / "captioned.tmp.mp4"
        burn_argv = [
            ffmpeg_exe,
            "-y",
            "-loglevel",
            "error",
            "-i",
            str(clean_path.resolve()),
            "-vf",
            "ass=burn.ass:fontsdir=fonts",
            "-map",
            "0:v:0",
            "-an",
            "-c:v",
            "libx264",
            "-preset",
            subtitle_preset,
            "-crf",
            "18",
            "-pix_fmt",
            "yuv420p",
            "-fps_mode",
            "passthrough",
            "-movflags",
            "+faststart",
            str(caption_candidate.resolve()),
        ]
        _run(burn_argv, cwd=run_dir)
        caption_validation = media.validate_video(
            caption_candidate,
            render_profile=render_profile,
            expected_frame_count=expected_frames,
            expected_audio_streams=0,
        )

        contact_candidate, contact_samples, gap = _generate_contact_sheet(
            ffmpeg_exe=ffmpeg_exe,
            video=caption_candidate,
            cues=selection.cues,
            run_dir=run_dir,
        )
        caption_for_final = run_dir / "final.tmp.mp4"
        if project.voiceover_mode == "disabled":
            shutil.copyfile(caption_candidate, caption_for_final)
            if sha256_file(caption_for_final) != caption_validation.get("sha256"):
                raise SubtitleDeliveryError("Disabled final 候选不是 captioned video 的逐字节副本")

        final_ass_path = project.path("subtitles/final.ass")
        caption_path = project.path("output/final-subtitled-video-only.mp4")
        contact_path = project.path("previews/final-subtitle-contact-sheet.png")
        final_path = project.path("output/final.mp4")
        media.atomic_publish(burn_ass, final_ass_path)
        media.atomic_publish(caption_candidate, caption_path)
        media.atomic_publish(contact_candidate, contact_path)
        if project.voiceover_mode == "disabled":
            media.atomic_publish(caption_for_final, final_path)
        published = True
        caption_validation = media.bind_validated_video(
            caption_path,
            render_profile=render_profile,
            expected_frame_count=expected_frames,
            expected_audio_streams=0,
            deep_receipt=caption_validation["validation"],
        )
        final_validation = caption_validation
        if project.voiceover_mode == "disabled":
            final_validation = media.bind_validated_video(
                final_path,
                render_profile=render_profile,
                expected_frame_count=expected_frames,
                expected_audio_streams=0,
                deep_receipt=caption_validation["validation"],
            )
        caption_validation = _with_encoding_receipt(caption_validation, encoding)
        if project.voiceover_mode == "disabled":
            final_validation = _with_encoding_receipt(final_validation, encoding)

        gap_evidence: Any
        if gap is None:
            gap_evidence = "not_applicable_no_gap"
        else:
            gap_evidence = {"status": "captured", **gap}
        manifest["subtitles"] = {
            "sourceKind": selection.source_kind,
            "file": selection.relative_path,
            "sha256": selection.sha256,
            "cueCount": compiled.cue_count,
            "firstStartMs": compiled.first_start_ms,
            "lastEndMs": compiled.last_end_ms,
            "timelineSha256": selection.timeline_sha256,
            "style": {
                "contractVersion": SUBTITLE_STYLE["contractVersion"],
                "contractSha256": encoding["contractSha256"],
                "assStyleContractSha256": compiled.style_contract_sha256,
                "font": {
                    "family": compiled.font.family,
                    "file": compiled.font.file_name,
                    "sha256": compiled.font.sha256,
                },
                "ass": {
                    "file": "subtitles/final.ass",
                    "sha256": compiled.sha256,
                    "bytes": len(compiled.content),
                },
            },
            "encoding": dict(encoding),
            "subtitleIdentitySha256": sub_identity,
            "gapEvidence": gap_evidence,
            "contactSheet": {
                "file": "previews/final-subtitle-contact-sheet.png",
                "sha256": sha256_file(contact_path),
                "bytes": contact_path.stat().st_size,
                "samples": contact_samples,
            },
        }
        manifest["captionedVideo"] = {
            **_media_record("output/final-subtitled-video-only.mp4", caption_validation),
            "cleanVideoSha256": clean_validation["sha256"],
            "subtitleIdentitySha256": sub_identity,
            "burnContractVersion": BURN_CONTRACT_VERSION,
            "burnContractSha256": encoding["contractSha256"],
        }
        manifest["finalApproval"] = None
        if project.voiceover_mode == "disabled":
            timing_sha = _timing_plan_sha(project)
            final_inputs, final_identity = compute_final_identity(
                voiceover_mode="disabled",
                clean_video_sha256=clean_validation["sha256"],
                audio_sha256="",
                timeline_sha256=selection.timeline_sha256,
                authoritative_subtitle_sha256=selection.sha256,
                subtitle_style_contract_sha256=encoding["contractSha256"],
                font_sha256=compiled.font.sha256,
                render_profile_sha256=project.timing_plan["renderProfileSha256"],
                timing_plan_sha256=timing_sha,
                mux_contract_version=DISABLED_MUX_CONTRACT_VERSION,
                final_media_sha256=final_validation["sha256"],
                subtitle_preset=subtitle_preset,
            )
            manifest["final"] = {
                **_media_record("output/final.mp4", final_validation),
                "identityInputs": final_inputs,
                "finalIdentitySha256": final_identity,
            }
        else:
            manifest["final"] = {}
        write_json_atomic(project.path("manifests/delivery-manifest.json"), manifest)
        completed = True
        return manifest
    except Exception as exc:
        if published:
            raise SubtitleDeliveryError(
                f"正式字幕文件已发布，但 delivery manifest 更新失败；保留工作目录 {run_dir}: {exc}"
            ) from exc
        raise
    finally:
        # Failed runs are deliberately retained for diagnosis.  A fully
        # published run has no remaining candidate value and is safe to remove.
        if completed:
            shutil.rmtree(run_dir, ignore_errors=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="按项目 mode 烧录唯一权威字幕")
    parser.add_argument("--project", required=True, help="项目根目录")
    parser.add_argument("--force-deep", action="store_true", help="忽略旧 receipt 并重新深验上游")
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    workspace_config: WorkspaceConfig | None = None,
) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = workspace_config or load_workspace_config()
        manifest = burn_project(
            args.project,
            force_deep=args.force_deep,
            subtitle_preset=config.video_encoding.subtitle_preset,
        )
    except (ProjectValidationError, WorkspaceError) as exc:
        print(f"ERROR={exc}", file=sys.stderr)
        return 2
    except SubtitleStaleError as exc:
        print(f"ERROR={exc}", file=sys.stderr)
        return 5
    except SubtitleDeliveryError as exc:
        print(f"ERROR={exc}", file=sys.stderr)
        return 4
    except Exception as exc:
        # Shared media validation errors are exit-code 4 without coupling this
        # module to the concrete exception class during parallel implementation.
        print(f"ERROR={exc}", file=sys.stderr)
        return 4
    print(f"OUTPUT={args.project}\\output\\final-subtitled-video-only.mp4")
    print(f"VOICEOVER_MODE={manifest['voiceoverMode']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
