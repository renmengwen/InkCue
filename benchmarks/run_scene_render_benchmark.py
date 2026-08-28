#!/usr/bin/env python3
"""Run the real formal scene-render batch against deterministic local fixtures.

The runner deliberately keeps fixture preparation outside measured intervals.
It injects only the workspace concurrency value in-process because the formal
renderer has no per-invocation config argument. Candidate generation, deep
media validation, coordinator binding, atomic publication, and manifest writes
all execute through ``render_stream_whiteboard._run_formal_batch``.
"""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from ctypes import wintypes
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping
from unittest import mock

from PIL import Image, ImageDraw


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
FIXTURES = Path(__file__).resolve().parent / "fixtures"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import annotation_review  # noqa: E402
import project_workspace  # noqa: E402
import render_stream_whiteboard  # noqa: E402
import render_timing  # noqa: E402
import srt_timeline  # noqa: E402


REPORT_CONTRACT = "whiteboard-scene-render-benchmark-report-v1"
FIXTURE_CONTRACT = "whiteboard-scene-render-benchmark-fixture-v1"
SAMPLE_INTERVAL_SECONDS = 0.02
FIXTURE_PROJECT_IDS = {
    "fixture-small": "11111111-1111-4111-8111-111111111111",
    "fixture-medium": "22222222-2222-4222-8222-222222222222",
}


class BenchmarkError(RuntimeError):
    """The benchmark fixture or invocation is invalid."""


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_file(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BenchmarkError(f"无法读取 benchmark JSON: {path}") from exc
    if not isinstance(value, dict):
        raise BenchmarkError(f"benchmark JSON 顶层必须是对象: {path}")
    return value


def load_fixture(name_or_path: str) -> tuple[dict[str, Any], Path]:
    supplied = Path(name_or_path)
    path = supplied if supplied.suffix.casefold() == ".json" else FIXTURES / f"{name_or_path}.json"
    path = path.resolve()
    fixture = _json_file(path)
    if fixture.get("contractVersion") != FIXTURE_CONTRACT:
        raise BenchmarkError("fixture contractVersion 不受支持")
    fixture_id = fixture.get("fixtureId")
    if fixture_id not in FIXTURE_PROJECT_IDS:
        raise BenchmarkError("fixtureId 不在固定 benchmark allowlist")
    count = fixture.get("sceneCount")
    duration = fixture.get("sceneDurationMs")
    if isinstance(count, bool) or not isinstance(count, int) or count < 2:
        raise BenchmarkError("fixture.sceneCount 必须是至少 2 的整数")
    if isinstance(duration, bool) or not isinstance(duration, int) or duration < 601:
        raise BenchmarkError("fixture.sceneDurationMs 必须至少 601ms")
    if fixture.get("canvas") != {"width": 1920, "height": 1080, "background": "#F5EBD7"}:
        raise BenchmarkError("fixture canvas 必须固定为 1920x1080 暖米黄画布")
    hand = ROOT / str(fixture.get("handAsset", ""))
    if hand.resolve() != (ROOT / "assets" / "drawing-hand.png").resolve() or not hand.is_file():
        raise BenchmarkError("fixture handAsset 必须绑定仓库固定 drawing-hand.png")
    annotation = fixture.get("annotation")
    if not isinstance(annotation, dict) or annotation.get("protectedRegions") != []:
        raise BenchmarkError("fixture annotation 配置无效")
    if annotation.get("revealStartMs", 0) + annotation.get("revealDurationMs", 0) > duration - 500:
        raise BenchmarkError("fixture reveal 超过 sceneDurationMs - 500")
    return fixture, path


def _srt_time(milliseconds: int) -> str:
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def _source_srt(scene_count: int, duration_ms: int) -> str:
    cues: list[str] = []
    for index in range(scene_count):
        start = index * duration_ms
        end = (index + 1) * duration_ms
        cues.append(
            f"{index + 1}\n{_srt_time(start)} --> {_srt_time(end)}\n合成基准场景 {index + 1}\n"
        )
    return "\n".join(cues)


def _write_fixture_image(path: Path, index: int) -> None:
    """Write a deterministic 1920x1080 synthetic line-art image."""

    image = Image.new("RGB", (1920, 1080), "#F5EBD7")
    draw = ImageDraw.Draw(image)
    offset = (index % 4) * 18
    draw.rounded_rectangle(
        (180 + offset, 220, 920 + offset, 820),
        radius=90,
        outline=(27, 27, 27),
        width=15,
    )
    draw.ellipse((1040 - offset, 270, 1570 - offset, 800), outline=(27, 27, 27), width=15)
    draw.line((430, 520 + offset, 1320, 520 - offset), fill=(43, 103, 120), width=24)
    image.save(path, format="PNG", compress_level=1, optimize=False)


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    project_workspace.write_json_atomic(path, dict(value))


def build_fixture_project(destination: Path, fixture: Mapping[str, Any]) -> Path:
    """Materialize a complete synthetic project with a fixture-only approval."""

    project_root = destination / "project"
    if project_root.exists():
        raise BenchmarkError(f"fixture project 已存在: {project_root}")
    for relative in (
        "source",
        "planning",
        "scenes",
        "manifests",
        "previews",
        "output",
        ".work",
        "audio",
        "subtitles",
    ):
        (project_root / relative).mkdir(parents=True, exist_ok=True)

    fixture_id = str(fixture["fixtureId"])
    project_id = FIXTURE_PROJECT_IDS[fixture_id]
    count = int(fixture["sceneCount"])
    duration_ms = int(fixture["sceneDurationMs"])
    source = project_root / "source" / "source.srt"
    source.write_text(_source_srt(count, duration_ms), encoding="utf-8")

    scenes = []
    for index in range(1, count + 1):
        scene_id = f"scene-{index:02d}"
        scenes.append(
            {
                "sceneId": scene_id,
                "sourceCueRange": [index, index],
                "sceneDurationMs": duration_ms,
                "prompt": f"合成基准线稿场景 {index}",
                "outputFile": f"{scene_id}.png",
            }
        )
    plan = {
        "schemaVersion": 1,
        "projectId": project_id,
        "outputCanvas": dict(project_workspace.FIXED_CANVAS),
        "globalPrompt": project_workspace.DEFAULT_GLOBAL_PROMPT,
        "constraints": {"forbidText": False},
        "scenesDirectory": "scenes",
        "manifestFile": "manifests/generation-manifest.json",
        "scenes": scenes,
    }
    _write_json(project_root / "planning" / "generation-plan.json", plan)
    timing = srt_timeline.build_source_timing_plan(
        project_id=project_id,
        source_srt_path=source,
        scene_specs=scenes,
        render_profile=project_workspace.FIXED_RENDER_PROFILE,
        voiceover_mode="disabled",
    )
    _write_json(project_root / "planning" / "timing-plan.json", timing)
    metadata = {
        "schemaVersion": 2,
        "projectId": project_id,
        "projectName": "project",
        "createdAt": "2026-08-20T00:00:00+08:00",
        "voiceoverMode": "disabled",
        "source": {
            "file": "source/source.srt",
            "sha256": project_workspace.sha256_file(source),
        },
        "renderProfile": dict(project_workspace.FIXED_RENDER_PROFILE),
        "paths": dict(project_workspace.PROJECT_PATHS_V2),
    }
    _write_json(project_root / "project.json", metadata)

    timing_sha = project_workspace.sha256_file(project_root / "planning" / "timing-plan.json")
    render_sha = project_workspace.sha256_json(project_workspace.FIXED_RENDER_PROFILE)
    reveal = fixture["annotation"]
    for index, timing_scene in enumerate(timing["scenes"], start=1):
        scene_id = timing_scene["sceneId"]
        _write_fixture_image(project_root / "scenes" / f"{scene_id}.png", index)
        annotation = {
            "sceneId": scene_id,
            "canvas": {"width": 1920, "height": 1080},
            "sceneDurationMs": timing_scene["sceneDurationMs"],
            "timingPlanSha256": timing_sha,
            "renderProfileSha256": render_sha,
            "sceneFrameRange": {
                "startFrame": timing_scene["startFrame"],
                "endFrameExclusive": timing_scene["endFrameExclusive"],
                "frameCount": timing_scene["frameCount"],
            },
            "timingSource": {
                "kind": "source-srt",
                "timelineFile": "source/source.srt",
                "timelineSha256": timing["activeTimeline"]["sha256"],
                "sceneId": scene_id,
                "sceneStartMs": timing_scene["startMs"],
                "sceneEndMs": timing_scene["endMs"],
            },
            "elements": [
                {
                    "id": "synthetic-mark",
                    "sequence": 1,
                    "region": {"x": 120, "y": 160, "width": 1500, "height": 720},
                    "reveal": {
                        "startMs": int(reveal["revealStartMs"]),
                        "durationMs": int(reveal["revealDurationMs"]),
                        "protectedRegions": [],
                        "direction": "left-to-right",
                    },
                }
            ],
        }
        _write_json(project_root / "scenes" / f"{scene_id}.annotation.json", annotation)
        preview = Image.new("RGB", (320, 180), "#F5EBD7")
        ImageDraw.Draw(preview).rectangle((20, 20, 299, 159), outline=(27, 27, 27), width=3)
        preview.save(
            project_root / "previews" / f"{scene_id}-annotation-preview.png",
            format="PNG",
            compress_level=1,
            optimize=False,
        )
    contact = Image.new("RGB", (640, 360), "#F5EBD7")
    ImageDraw.Draw(contact).rectangle((10, 10, 629, 349), outline=(27, 27, 27), width=3)
    contact.save(
        project_root / annotation_review.CONTACT_SHEET_FILE,
        format="PNG",
        compress_level=1,
        optimize=False,
    )

    project = project_workspace.load_project(project_root)
    context = render_timing.build_formal_validation_context(project)
    formals = render_timing.resolve_formal_scenes(
        project,
        [scene["sceneId"] for scene in project.plan["scenes"]],
        context=context,
    )
    technical = annotation_review.write_annotation_review_technical(project, formals, context)
    annotation_review.approve_current_annotation_review(project, technical["identityHash"])
    annotation_review.require_current_annotation_review_approval(project)
    return project_root


def _directory_bytes(root: Path) -> int:
    total = 0
    try:
        for path in root.rglob("*"):
            try:
                if path.is_file() and not path.is_symlink():
                    total += path.stat().st_size
            except OSError:
                continue
    except OSError:
        return total
    return total


def _linux_process_snapshot() -> dict[int, tuple[int, int | None, str]]:
    snapshot: dict[int, tuple[int, int | None, str]] = {}
    proc = Path("/proc")
    if not proc.is_dir():
        return snapshot
    page_size = os.sysconf("SC_PAGE_SIZE")
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            stat = (entry / "stat").read_text(encoding="utf-8")
            close = stat.rfind(")")
            fields = stat[close + 2 :].split()
            parent = int(fields[1])
            rss = int(fields[21]) * page_size
            name = (entry / "comm").read_text(encoding="utf-8").strip()
            snapshot[int(entry.name)] = (parent, rss, name)
        except (OSError, UnicodeError, ValueError, IndexError):
            continue
    return snapshot


if os.name == "nt":
    TH32CS_SNAPPROCESS = 0x00000002
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    PROCESS_VM_READ = 0x0010
    INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("PageFaultCount", wintypes.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        ]


def _windows_rss(pid: int) -> int | None:
    kernel32 = ctypes.windll.kernel32
    psapi = ctypes.windll.psapi
    handle = kernel32.OpenProcess(
        PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_VM_READ,
        False,
        pid,
    )
    if not handle:
        return None
    try:
        counters = PROCESS_MEMORY_COUNTERS()
        counters.cb = ctypes.sizeof(counters)
        if not psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
            return None
        return int(counters.WorkingSetSize)
    finally:
        kernel32.CloseHandle(handle)


def _windows_process_snapshot() -> dict[int, tuple[int, int | None, str]]:
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if handle == INVALID_HANDLE_VALUE:
        return {}
    result: dict[int, tuple[int, int | None, str]] = {}
    entry = PROCESSENTRY32W()
    entry.dwSize = ctypes.sizeof(entry)
    try:
        ok = kernel32.Process32FirstW(handle, ctypes.byref(entry))
        while ok:
            pid = int(entry.th32ProcessID)
            result[pid] = (
                int(entry.th32ParentProcessID),
                None,
                str(entry.szExeFile),
            )
            ok = kernel32.Process32NextW(handle, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(handle)
    return result


def process_snapshot() -> dict[int, tuple[int, int | None, str]]:
    if os.name == "nt":
        try:
            return _windows_process_snapshot()
        except (AttributeError, OSError, ctypes.ArgumentError):
            return {}
    return _linux_process_snapshot()


def _descendants(snapshot: Mapping[int, tuple[int, int | None, str]], root_pid: int) -> set[int]:
    selected = {root_pid}
    changed = True
    while changed:
        changed = False
        for pid, (parent, _rss, _name) in snapshot.items():
            if pid not in selected and parent in selected:
                selected.add(pid)
                changed = True
    return selected


class ResourceMonitor:
    def __init__(self, project_root: Path, interval: float = SAMPLE_INTERVAL_SECONDS) -> None:
        self.project_root = project_root
        self.interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.samples = 0
        self.peak_rss_bytes: int | None = None
        self.peak_ffmpeg_processes: int | None = None
        self.peak_child_processes: int | None = None
        self.peak_work_bytes = 0

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="scene-render-benchmark-monitor", daemon=True)
        self._thread.start()

    def stop(self) -> dict[str, Any]:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        return {
            "measurementMethod": (
                "windows-toolhelp32-working-set-sampling-v1"
                if os.name == "nt"
                else "linux-proc-rss-sampling-v1"
            ),
            "sampleIntervalMs": round(self.interval * 1000, 3),
            "sampleCount": self.samples,
            "peakRssBytes": self.peak_rss_bytes,
            "observedPeakFfmpegProcesses": self.peak_ffmpeg_processes,
            "observedPeakChildProcesses": self.peak_child_processes,
            "peakWorkCandidateBytes": self.peak_work_bytes,
        }

    def _sample(self) -> None:
        snapshot = process_snapshot()
        if snapshot:
            selected = _descendants(snapshot, os.getpid())
            rss_values = (
                [value for pid in selected if (value := _windows_rss(pid)) is not None]
                if os.name == "nt"
                else [snapshot[pid][1] for pid in selected if snapshot[pid][1] is not None]
            )
            if rss_values:
                rss = sum(int(value) for value in rss_values)
                self.peak_rss_bytes = max(self.peak_rss_bytes or 0, rss)
            ffmpeg = sum(
                1
                for pid in selected
                if snapshot[pid][2].casefold() in {"ffmpeg", "ffmpeg.exe"}
            )
            children = max(0, len(selected) - 1)
            self.peak_ffmpeg_processes = max(self.peak_ffmpeg_processes or 0, ffmpeg)
            self.peak_child_processes = max(self.peak_child_processes or 0, children)
        self.peak_work_bytes = max(self.peak_work_bytes, _directory_bytes(self.project_root / ".work"))
        self.samples += 1

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._sample()
            self._stop.wait(self.interval)
        self._sample()


def _workspace_config(root: Path, concurrency: int) -> project_workspace.WorkspaceConfig:
    return project_workspace.WorkspaceConfig(
        root=root,
        config_path=root / "benchmark-workspace.json",
        concurrency=project_workspace.ExecutionConcurrency(default=1, scene_render=concurrency),
    )


def _render_args(project_root: Path, fixture: Mapping[str, Any]) -> argparse.Namespace:
    options = fixture["renderArguments"]
    return render_stream_whiteboard._parse_args(
        [
            "--project",
            str(project_root),
            "--all",
            "--ink-path",
            str(options["inkPath"]),
            "--color-fill",
            str(options["colorFill"]),
            "--pause",
            str(options["pause"]),
            "--grid-edge",
            str(options["gridEdge"]),
        ]
    )


def _output_fingerprint(project_root: Path) -> dict[str, Any]:
    project = project_workspace.load_project(project_root)
    manifest_path = project.path(render_timing.RENDER_MANIFEST_FILE)
    manifest = _json_file(manifest_path)
    scenes: list[dict[str, Any]] = []
    for plan_scene in project.plan["scenes"]:
        scene_id = plan_scene["sceneId"]
        current = manifest.get("scenes", {}).get(scene_id, {})
        output_file = current.get("outputFile")
        output = project.path(output_file) if isinstance(output_file, str) else None
        scenes.append(
            {
                "sceneId": scene_id,
                "outputFile": output_file,
                "renderIdentityHash": current.get("renderIdentityHash"),
                "outputSha256": _sha256_file(output) if output is not None and output.is_file() else None,
                "outputBytes": output.stat().st_size if output is not None and output.is_file() else None,
            }
        )
    identity_payload = [{"sceneId": item["sceneId"], "renderIdentityHash": item["renderIdentityHash"]} for item in scenes]
    sha_payload = [{"sceneId": item["sceneId"], "outputSha256": item["outputSha256"]} for item in scenes]
    return {
        "sceneOrder": [item["sceneId"] for item in scenes],
        "scenes": scenes,
        "identitySetSha256": project_workspace.sha256_json(identity_payload),
        "outputShaSetSha256": project_workspace.sha256_json(sha_payload),
    }


def _summary_metric(summary: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        if name in summary:
            return summary[name]
    return None


def run_measured(
    project_root: Path,
    fixture: Mapping[str, Any],
    *,
    concurrency: int,
    temperature: str,
) -> dict[str, Any]:
    monitor = ResourceMonitor(project_root)
    args = _render_args(project_root, fixture)
    workspace = _workspace_config(project_root.parent, concurrency)
    monitor.start()
    started_ns = time.perf_counter_ns()
    try:
        with mock.patch.object(project_workspace, "load_workspace_config", return_value=workspace):
            summary = render_stream_whiteboard._run_formal_batch(args)
    finally:
        wall_ms = round((time.perf_counter_ns() - started_ns) / 1_000_000, 3)
        resources = monitor.stop()
    fingerprint = _output_fingerprint(project_root) if summary.get("status") == "PASS" else None
    result = {
        "temperature": temperature,
        "configured": _summary_metric(summary, "configuredSceneRenderConcurrency", "configured"),
        "effective": _summary_metric(summary, "effectiveSceneRenderConcurrency", "effective"),
        "peak": _summary_metric(summary, "peakSceneRenderWorkers", "peak"),
        "taskCount": summary.get("taskCount"),
        "wallMs": _summary_metric(summary, "wallMs") or wall_ms,
        "runnerWallMs": wall_ms,
        "peakRssBytes": resources["peakRssBytes"],
        "ffmpegProcessCount": summary.get("ffmpegProcessCount"),
        "observedPeakFfmpegProcesses": resources["observedPeakFfmpegProcesses"],
        "candidateBytes": summary.get("candidateBytes"),
        "candidateBytesByScene": summary.get("candidateBytesByScene"),
        "peakWorkCandidateBytes": resources["peakWorkCandidateBytes"],
        "residualWorkBytes": _directory_bytes(project_root / ".work"),
        "stageDurationsMs": summary.get("stageDurationsMs"),
        "status": summary.get("status"),
        "partialSuccess": summary.get("partialSuccess"),
        "successCount": summary.get("successCount"),
        "failureCount": summary.get("failureCount"),
        "approvalWritten": summary.get("approvalWritten"),
        "sceneOrder": summary.get("sceneOrder"),
        "outputFingerprint": fingerprint,
        "resourceSampling": resources,
        "formalBatchSummary": summary,
    }
    return result


def _deep_receipts(project_root: Path) -> dict[str, dict[str, Any]]:
    manifest = _json_file(project_root / render_timing.RENDER_MANIFEST_FILE)
    receipts: dict[str, dict[str, Any]] = {}
    for scene_id, scene in manifest.get("scenes", {}).items():
        receipt = scene.get("media", {}).get("validation", {}).get("deepReceipt")
        if isinstance(receipt, dict):
            receipts[scene_id] = receipt
    return receipts


def run_failure_policy_probe(
    project_root: Path,
    fixture: Mapping[str, Any],
    *,
    concurrency: int,
) -> dict[str, Any]:
    """Exercise the real coordinator with one injected worker failure.

    This is a contract probe, not a performance sample. Successful worker
    results reuse byte-identical, already deep-validated fixture outputs.
    """

    project = project_workspace.load_project(project_root)
    scene_order = [scene["sceneId"] for scene in project.plan["scenes"]]
    if len(scene_order) < 2:
        return {"status": "SKIP", "reason": "failure probe 至少需要 2 幕"}
    failed_scene = scene_order[0]
    before = _output_fingerprint(project_root)
    before_by_scene = {item["sceneId"]: item for item in before["scenes"]}
    receipts = _deep_receipts(project_root)
    publish_order: list[str] = []
    real_publish = render_stream_whiteboard._publish_and_bind_scene

    def execute(tasks: Iterable[dict[str, Any]], **_kwargs):
        results: dict[str, dict[str, Any]] = {}
        for task in tasks:
            scene_id = task["sceneId"]
            if scene_id == failed_scene:
                results[scene_id] = {
                    "sceneId": scene_id,
                    "candidatePath": task["candidatePath"],
                    "status": "failed",
                    "stage": "benchmark_injected_worker_failure",
                    "errorType": "BenchmarkInjectedFailure",
                    "error": "受控 failure-policy probe",
                    "exitCode": 4,
                }
                continue
            source = project_root / str(before_by_scene[scene_id]["outputFile"])
            candidate = Path(task["candidatePath"])
            candidate.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, candidate)
            results[scene_id] = {
                "sceneId": scene_id,
                "candidatePath": str(candidate),
                "status": "succeeded",
                "deepReceipt": receipts[scene_id],
            }
        return results, min(concurrency, len(scene_order))

    def publish(candidate: Path, destination: Path, **kwargs):
        publish_order.append(destination.name)
        return real_publish(candidate, destination, **kwargs)

    args = _render_args(project_root, fixture)
    workspace = _workspace_config(project_root.parent, concurrency)
    with mock.patch.object(project_workspace, "load_workspace_config", return_value=workspace), mock.patch.object(
        render_stream_whiteboard, "_execute_formal_candidate_tasks", side_effect=execute
    ), mock.patch.object(render_stream_whiteboard, "_publish_and_bind_scene", side_effect=publish):
        summary = render_stream_whiteboard._run_formal_batch(args)
    after = _output_fingerprint(project_root)
    after_by_scene = {item["sceneId"]: item for item in after["scenes"]}
    failed_preserved = before_by_scene[failed_scene]["outputSha256"] == after_by_scene[failed_scene]["outputSha256"]
    all_bytes_stable = before["outputShaSetSha256"] == after["outputShaSetSha256"]
    expected_publish = [f"{scene_id}-whiteboard.mp4" for scene_id in scene_order[1:]]
    stable = all(
        (
            summary.get("status") == "FAIL",
            summary.get("partialSuccess") is True,
            summary.get("failureCount") == 1,
            summary.get("successCount") == len(scene_order) - 1,
            summary.get("approvalWritten") is False,
            summary.get("sceneOrder") == scene_order,
            [item.get("sceneId") for item in summary.get("results", [])] == scene_order,
            failed_preserved,
            all_bytes_stable,
            publish_order == expected_publish,
        )
    )
    return {
        "status": "PASS" if stable else "FAIL",
        "probeType": "controlled-worker-result-coordinator-contract-probe",
        "notAPerformanceSample": True,
        "failedScene": failed_scene,
        "failedCurrentPreserved": failed_preserved,
        "allOutputBytesStable": all_bytes_stable,
        "partialSuccessStillFails": summary.get("status") == "FAIL" and summary.get("partialSuccess") is True,
        "approvalWritten": summary.get("approvalWritten"),
        "sceneOrderStable": summary.get("sceneOrder") == scene_order,
        "publishOrder": publish_order,
        "expectedPublishOrder": expected_publish,
        "formalBatchSummary": summary,
    }


def _command_version(command: str) -> str | None:
    try:
        completed = subprocess.run(
            [command, "-version"],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    lines = (completed.stdout or completed.stderr).splitlines()
    return lines[0].strip() if lines else None


def _git_value(*args: str) -> str | None:
    try:
        completed = subprocess.run(
            ["git", *args], cwd=ROOT, check=False, capture_output=True, text=True, timeout=10
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = completed.stdout.strip()
    return value if completed.returncode == 0 and value else None


def environment_report() -> dict[str, Any]:
    return {
        "platform": platform.platform(),
        "osName": os.name,
        "pythonVersion": platform.python_version(),
        "pythonExecutable": sys.executable,
        "logicalCpuCount": os.cpu_count(),
        "ffmpegVersion": _command_version("ffmpeg"),
        "ffprobeVersion": _command_version("ffprobe"),
        "gitHead": _git_value("rev-parse", "HEAD"),
        "gitBranch": _git_value("branch", "--show-current"),
    }


def _stability(runs: list[dict[str, Any]]) -> dict[str, Any]:
    successful = [run for run in runs if run.get("status") == "PASS" and run.get("outputFingerprint")]
    serial = next((run for run in successful if run.get("configured") == 1 and run.get("temperature") == "cold"), None)
    if serial is None:
        return {
            "baseline": None,
            "identityStableAcrossRuns": None,
            "outputShaStableAcrossRuns": None,
            "sceneOrderStableAcrossRuns": None,
            "reason": "缺少 sceneRender=1 cold 成功基线",
        }
    baseline = serial["outputFingerprint"]
    for run in runs:
        fingerprint = run.get("outputFingerprint")
        run["identityStableAgainstSerial"] = (
            fingerprint.get("identitySetSha256") == baseline["identitySetSha256"] if fingerprint else None
        )
        run["outputShaStableAgainstSerial"] = (
            fingerprint.get("outputShaSetSha256") == baseline["outputShaSetSha256"] if fingerprint else None
        )
        run["sceneOrderStableAgainstSerial"] = (
            fingerprint.get("sceneOrder") == baseline["sceneOrder"] if fingerprint else None
        )
    return {
        "baseline": {"configured": 1, "temperature": "cold", "outputFingerprint": baseline},
        "identityStableAcrossRuns": all(run["identityStableAgainstSerial"] for run in successful),
        "outputShaStableAcrossRuns": all(run["outputShaStableAgainstSerial"] for run in successful),
        "sceneOrderStableAcrossRuns": all(run["sceneOrderStableAgainstSerial"] for run in successful),
        "successfulRunCount": len(successful),
        "totalRunCount": len(runs),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="正式 sceneRender cold/warm 性能基准")
    parser.add_argument("--fixture", default="fixture-medium", help="fixture 名称或 JSON 路径")
    parser.add_argument("--concurrency", nargs="+", type=int, default=[1, 2, 4, 5])
    parser.add_argument("--temperature", nargs="+", choices=("cold", "warm"), default=["cold", "warm"])
    parser.add_argument("--output", type=Path, help="原始 JSON 报告路径；缺省只输出 stdout")
    parser.add_argument("--workspace", type=Path, help="benchmark 临时工作区；缺省使用系统临时目录")
    parser.add_argument("--keep-workspace", action="store_true", help="保留生成的合成项目供复核")
    parser.add_argument("--no-failure-probe", action="store_true", help="不运行受控失败策略 probe")
    return parser


def _validate_concurrency(values: Iterable[int]) -> list[int]:
    result: list[int] = []
    for value in values:
        if isinstance(value, bool) or not 1 <= value <= 16:
            raise BenchmarkError("--concurrency 必须是 1-16 的整数")
        if value not in result:
            result.append(value)
    if not result:
        raise BenchmarkError("--concurrency 不能为空")
    return result


def run_benchmark(args: argparse.Namespace) -> dict[str, Any]:
    fixture, fixture_path = load_fixture(args.fixture)
    concurrency_values = _validate_concurrency(args.concurrency)
    temperatures = list(dict.fromkeys(args.temperature))
    if "warm" in temperatures and "cold" not in temperatures:
        raise BenchmarkError("warm 必须紧接同一项目的 cold；请同时请求 cold warm")
    owned_temp = args.workspace is None
    workspace = (
        Path(tempfile.mkdtemp(prefix="srt-whiteboard-scene-render-benchmark-"))
        if owned_temp
        else args.workspace.resolve()
    )
    if not owned_temp:
        workspace.mkdir(parents=True, exist_ok=False)
    base_project = build_fixture_project(workspace / "baseline", fixture)
    runs: list[dict[str, Any]] = []
    successful_project: Path | None = None
    try:
        for concurrency in concurrency_values:
            run_parent = workspace / f"scene-render-{concurrency}"
            project_root = run_parent / "project"
            run_parent.mkdir(parents=True, exist_ok=False)
            shutil.copytree(base_project, project_root)
            if "cold" in temperatures:
                cold = run_measured(
                    project_root,
                    fixture,
                    concurrency=concurrency,
                    temperature="cold",
                )
                runs.append(cold)
                if cold["status"] == "PASS":
                    successful_project = successful_project or project_root
            if "warm" in temperatures:
                warm = run_measured(
                    project_root,
                    fixture,
                    concurrency=concurrency,
                    temperature="warm",
                )
                runs.append(warm)
                if warm["status"] == "PASS":
                    successful_project = successful_project or project_root

        stability = _stability(runs)
        failure_probe = None
        if not args.no_failure_probe:
            failure_probe = (
                run_failure_policy_probe(
                    successful_project,
                    fixture,
                    concurrency=min(max(2, max(concurrency_values)), int(fixture["sceneCount"])),
                )
                if successful_project is not None
                else {"status": "SKIP", "reason": "没有成功渲染项目可用于 failure probe"}
            )
        warnings: list[str] = []
        if any(run["peakRssBytes"] is None for run in runs):
            warnings.append("peak RSS 在当前平台不可测；报告保留 null")
        if any(run["ffmpegProcessCount"] is None for run in runs):
            warnings.append("正式 batch 未报告 FFmpeg 累计启动数；仅保留采样到的进程峰值")
        if any(run["candidateBytes"] is None for run in runs):
            warnings.append("正式 batch 未报告 candidateBytes；仅保留采样到的 .work 峰值")
        if 1 not in concurrency_values:
            warnings.append("未测 sceneRender=1，无法给出串行基线稳定性结论")
        report = {
            "contractVersion": REPORT_CONTRACT,
            "generatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "fixture": {
                **fixture,
                "fixtureFile": fixture_path.relative_to(ROOT).as_posix(),
                "fixtureSha256": _sha256_file(fixture_path),
                "handSha256": _sha256_file(ROOT / str(fixture["handAsset"])),
            },
            "definitions": {
                "cold": "同一只读基线复制出的新项目第一次正式渲染",
                "warm": "同一项目同一参数紧接着第二次正式渲染",
                "coldDoesNotFlushOsCaches": True,
                "wallTimeIncludes": "formal context, candidate render/deep validation, coordinator binding/atomic publish",
                "failureProbeIsPerformanceSample": False,
            },
            "environment": environment_report(),
            "requestedConcurrency": concurrency_values,
            "requestedTemperatures": temperatures,
            "runs": runs,
            "stability": stability,
            "failurePolicyProbe": failure_probe,
            "measurementSupport": {
                "peakRss": "process-tree sampling; null when unsupported",
                "ffmpeg": "formal batch cumulative launch proxy plus sampled peak descendants",
                "candidateDisk": "formal batch candidateBytes plus sampled peak and residual .work bytes",
            },
            "resourceWarnings": warnings,
            "workspace": str(workspace) if args.keep_workspace else None,
        }
        if args.output is not None:
            output = args.output.resolve()
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return report
    finally:
        if owned_temp and not args.keep_workspace:
            shutil.rmtree(workspace, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        report = run_benchmark(args)
    except (BenchmarkError, OSError, project_workspace.ProjectValidationError, render_timing.RenderTimingError) as exc:
        print(json.dumps({"contractVersion": REPORT_CONTRACT, "status": "FAIL", "error": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2))
    runs_pass = all(run.get("status") == "PASS" for run in report["runs"])
    probe = report.get("failurePolicyProbe")
    probe_pass = probe is None or probe.get("status") in {"PASS", "SKIP"}
    return 0 if runs_pass and probe_pass else 1


if __name__ == "__main__":
    raise SystemExit(main())
