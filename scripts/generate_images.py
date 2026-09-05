#!/usr/bin/env python3
"""按 generation plan 有界并发生图，由 coordinator 串行提交正式图片与 manifest。"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bounded_execution import (
    CONTINUE_INDEPENDENT,
    WorkerFailure,
    WorkerOutcome,
    execute_bounded,
)
from image_generation import (
    ConfigError,
    CredentialSafetyError,
    HttpRequestError,
    ImageCandidate,
    ImageValidationError,
    ImagesGenerationsClient,
    ManifestError,
    ManifestStore,
    ProviderConfig,
    ResponseDecodeError,
    bind_image_candidate,
    build_final_prompt,
    image_input_identity,
    load_image_candidate,
    load_provider_config,
    normalize_image_candidate,
    publish_image_candidate,
    redact_secret,
    sha256_file,
    verify_config_git_safety,
)
from project_workspace import ProjectValidationError, ProjectWorkspace, WorkspaceError


SKILL_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PROVIDER_CONFIG = SKILL_ROOT / "config" / "image-providers.local.json"
RECOVERABLE_STATUSES = frozenset({"prepared", "requesting", "candidate_ready", "publishing"})
IMAGE_GENERATION_LOCK_NAME = "image-generation.lock"
HOST_IMAGE_SCHEMA_VERSION = 1
HOST_IMAGE_BACKEND_IDENTITY = {
    "provider": "gpt-login",
    "protocol": "codex-image-gen",
    "model": "host-managed",
    "tool": {"id": "codex-image-gen", "version": 1},
}


class CliArgumentError(ValueError):
    pass


class JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise CliArgumentError(message)


@dataclass(frozen=True)
class GenerationTask:
    scene_id: str
    prompt: str
    input_identity_sha256: str
    attempt_id: str
    attempt_root: Path
    candidate_path: Path
    formal_file: str


@dataclass(frozen=True)
class HostImageBackend:
    name: str = "gpt-login"
    protocol: str = "codex-image-gen"
    model: str = "host-managed"
    api_key: str = ""


HOST_IMAGE_BACKEND = HostImageBackend()


def _process_is_alive(pid: int) -> bool:
    """跨平台判断锁持有进程是否仍在运行；权限不足时按仍在运行处理。"""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _acquire_generation_lock(project_root: Path) -> Path:
    """防止两个 coordinator 同时操作同一项目并互相覆盖 manifest。"""
    lock_dir = project_root / ".work"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / IMAGE_GENERATION_LOCK_NAME
    payload = json.dumps(
        {"pid": os.getpid(), "startedAt": datetime.datetime.now().astimezone().isoformat()},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    for _ in range(2):
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            owner: dict[str, Any] = {}
            try:
                owner = json.loads(lock_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                raise ManifestError("image_generation_in_progress: 项目已有生图运行，锁文件不可读")
            pid = owner.get("pid")
            if isinstance(pid, int) and _process_is_alive(pid):
                raise ManifestError(
                    f"image_generation_in_progress: pid={pid} 正在运行；请等待其 JSON 结果后再恢复"
                )
            # 只有确认持有进程已退出时才清理陈旧锁，避免并发运行互相覆盖。
            try:
                lock_path.unlink()
            except OSError as exc:
                raise ManifestError("image_generation_in_progress: 无法清理陈旧锁") from exc
            continue
        try:
            os.write(fd, payload)
        finally:
            os.close(fd)
        return lock_path
    raise ManifestError("image_generation_in_progress: 无法取得项目锁")


def _release_generation_lock(lock_path: Path | None) -> None:
    if lock_path is None:
        return
    try:
        owner = json.loads(lock_path.read_text(encoding="utf-8"))
        if owner.get("pid") != os.getpid():
            return
    except (OSError, UnicodeError, json.JSONDecodeError):
        return
    lock_path.unlink(missing_ok=True)


def _checkpoint_hook(stage: str, scene_id: str) -> None:
    """测试崩溃注入点；正式运行保持无副作用。"""


def _summary(
    *,
    ok: bool,
    exit_code: int,
    project: str | None,
    provider: str | None = None,
    run_id: str | None = None,
    total: int = 0,
    targeted: int = 0,
    succeeded: int = 0,
    failed: int = 0,
    skipped: int = 0,
    configured_concurrency: int = 1,
    effective_concurrency: int = 0,
    task_count: int = 0,
    adopted_candidate_count: int = 0,
    unknown_external_outcome_count: int = 0,
    failures: list[dict[str, str]] | None = None,
    warnings: list[str] | None = None,
    error: str | None = None,
    cover: dict[str, Any] | None = None,
    status: str | None = None,
    host_image_generation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    value: dict[str, Any] = {
        "ok": ok,
        "command": "generate_images",
        "exitCode": exit_code,
        "project": project,
        "provider": provider,
        "runId": run_id,
        "total": total,
        "targeted": targeted,
        "succeeded": succeeded,
        "failed": failed,
        "skipped": skipped,
        "configuredConcurrency": configured_concurrency,
        "effectiveConcurrency": effective_concurrency,
        "taskCount": task_count,
        "adoptedCandidateCount": adopted_candidate_count,
        "unknownExternalOutcomeCount": unknown_external_outcome_count,
        "failures": failures or [],
        "warnings": warnings or [],
    }
    if error:
        value["error"] = error
    if cover is not None:
        value["cover"] = cover
    if status is not None:
        value["status"] = status
    if host_image_generation is not None:
        value["hostImageGeneration"] = host_image_generation
    return value


def _emit(value: dict[str, Any]) -> None:
    print(json.dumps(value, ensure_ascii=False, separators=(",", ":")))


def _scene_map(manifest: ManifestStore) -> dict[str, dict[str, Any]]:
    scenes = manifest.data.get("scenes", [])
    if not isinstance(scenes, list):
        raise ManifestError("manifest.scenes 必须是数组")
    result: dict[str, dict[str, Any]] = {}
    for scene in scenes:
        if not isinstance(scene, dict) or not isinstance(scene.get("sceneId"), str):
            raise ManifestError("manifest 场景记录无效")
        result[scene["sceneId"]] = scene
    return result


def _selected_scenes(
    plan_scenes: list[dict[str, Any]],
    manifest: ManifestStore,
    *,
    retry_failed: bool,
    overwrite: bool,
) -> list[dict[str, Any]]:
    by_id = _scene_map(manifest)
    selected: list[dict[str, Any]] = []
    for scene in plan_scenes:
        record = by_id[scene["sceneId"]]
        attempt = manifest.current_attempt(scene["sceneId"])
        if attempt is not None and attempt.get("status") in RECOVERABLE_STATUSES:
            selected.append(scene)
        elif (
            attempt is not None
            and attempt.get("status") == "validated"
            and (manifest.project_root / Path(attempt["candidateFile"])).is_file()
        ):
            selected.append(scene)
        elif record.get("status") == "unknown_external_outcome":
            continue
        elif overwrite:
            selected.append(scene)
        elif retry_failed:
            if record.get("status") == "failed":
                selected.append(scene)
        elif (
            record.get("status") == "validated"
            and (manifest.project_root / "scenes" / scene["outputFile"]).is_file()
        ):
            continue
        else:
            selected.append(scene)
    return selected


def _scoped_plan_scenes(
    plan_scenes: list[dict[str, Any]],
    requested_scene_ids: list[str] | None,
) -> list[dict[str, Any]]:
    """Return an explicit scene scope in generation-plan order."""

    if requested_scene_ids is None:
        return plan_scenes
    if len(set(requested_scene_ids)) != len(requested_scene_ids):
        raise CliArgumentError("--scene-id 不得重复")
    plan_ids = {scene["sceneId"] for scene in plan_scenes}
    unknown = [scene_id for scene_id in requested_scene_ids if scene_id not in plan_ids]
    if unknown:
        raise CliArgumentError(f"--scene-id 不属于 generation plan：{', '.join(unknown)}")
    requested = set(requested_scene_ids)
    return [scene for scene in plan_scenes if scene["sceneId"] in requested]


def _candidate_for_attempt(
    project_root: Path,
    scene_id: str,
    attempt: dict[str, Any],
) -> ImageCandidate:
    candidate = (project_root / Path(attempt["candidateFile"])).resolve(strict=False)
    attempt_root = candidate.parent
    return load_image_candidate(
        candidate,
        expected_attempt_root=attempt_root,
        expected_attempt_id=attempt["attemptId"],
        expected_scene_id=scene_id,
        expected_input_identity_sha256=attempt["inputIdentitySha256"],
        expected_formal_file=attempt["formalFile"],
    )


def _cleanup_candidate(candidate: ImageCandidate, run_dir: Path) -> None:
    candidate.path.unlink(missing_ok=True)
    candidate.receipt_path.unlink(missing_ok=True)
    current = candidate.path.parent
    while current != run_dir.parent and current != current.parent:
        try:
            current.rmdir()
        except OSError:
            break
        if current == run_dir:
            break
        current = current.parent


def _publish_candidate(
    *,
    manifest: ManifestStore,
    scene_id: str,
    candidate: ImageCandidate,
    formal_path: Path,
    overwrite: bool,
    run_dir: Path,
) -> None:
    attempt = manifest.current_attempt(scene_id)
    assert attempt is not None
    if attempt["status"] != "publishing":
        manifest.mark_attempt(
            scene_id,
            status="publishing",
            candidate=candidate,
            external_outcome="succeeded",
        )
        manifest.save()
        _checkpoint_hook("after_publishing_checkpoint", scene_id)
    if formal_path.exists():
        if (
            sha256_file(formal_path) != candidate.sha256
            or formal_path.stat().st_size != candidate.byte_count
        ):
            if not overwrite:
                raise ImageValidationError("publishing 恢复发现正式文件冲突")
            publish_image_candidate(candidate, formal_path, overwrite=True)
    else:
        publish_image_candidate(candidate, formal_path, overwrite=overwrite)
    bind_image_candidate(candidate, formal_path)
    _checkpoint_hook("after_formal_published", scene_id)
    manifest.mark_attempt(
        scene_id,
        status="validated",
        candidate=candidate,
        external_outcome="succeeded",
    )
    manifest.save()
    _checkpoint_hook("after_validated_before_cleanup", scene_id)
    _cleanup_candidate(candidate, run_dir)


def _worker(
    task: GenerationTask,
    *,
    client: ImagesGenerationsClient,
    provider: ProviderConfig,
) -> WorkerOutcome[ImageCandidate]:
    stage = "provider"
    try:
        payload = client.generate(task.prompt, max_attempts=3)
        _checkpoint_hook("after_provider_returned_before_candidate", task.scene_id)
        stage = "candidate_normalization"
        candidate = normalize_image_candidate(
            payload.data,
            task.candidate_path,
            task.attempt_root,
            task.scene_id,
            attempt_id=task.attempt_id,
            formal_file=task.formal_file,
            input_identity_sha256=task.input_identity_sha256,
            source=payload.source,
            provider_attempts=payload.attempts,
        )
        _checkpoint_hook("after_candidate_persisted", task.scene_id)
        stage = "complete"
        return WorkerOutcome.success(candidate)
    except (HttpRequestError, ResponseDecodeError, ImageValidationError, OSError) as exc:
        return WorkerOutcome.failed(
            WorkerFailure(
                category="explicit_external_failure",
                message=f"{stage}: {redact_secret(exc, provider.api_key)}",
                retryable=getattr(exc, "retryable", False),
                exception_type=type(exc).__name__,
            )
        )


def build_parser() -> argparse.ArgumentParser:
    parser = JsonArgumentParser(description="按计划有界并发生成白板场景图片")
    parser.add_argument("--project", required=True, help="项目根目录")
    parser.add_argument("--provider", help="命名供应商；省略时使用 activeProvider")
    parser.add_argument("--config", help="供应商 local 配置的绝对路径")
    parser.add_argument("--overwrite", action="store_true", help="允许原子替换已有图片")
    parser.add_argument("--retry-failed", action="store_true", help="只处理明确 failed 的场景")
    parser.add_argument(
        "--scene-id",
        action="append",
        dest="scene_ids",
        help="仅处理指定 sceneId；可重复传入，正式发布仍按 generation plan 顺序",
    )
    parser.add_argument(
        "--host-results",
        help="gpt-login 宿主生图结果 JSON 的绝对路径",
    )
    return parser


def _all_formal_scenes_validated(
    project: Any,
    manifest: ManifestStore,
    plan_scenes: list[dict[str, Any]],
) -> bool:
    """只有全部正式 scene current 且文件存在时，默认封面链才可执行。"""
    if not plan_scenes:
        return False
    records = _scene_map(manifest)
    for scene in plan_scenes:
        scene_id = scene["sceneId"]
        record = records.get(scene_id)
        if not isinstance(record, dict) or record.get("status") != "validated":
            return False
        if not (project.scenes_dir / scene["outputFile"]).is_file():
            return False
    return True


def _generate_cover_if_ready(project: Any, *, ready: bool, overwrite: bool) -> dict[str, Any] | None:
    """全部 scene 完成后生成封面；已有有效封面则幂等复用。"""
    if not ready:
        return None
    try:
        from cover_generation import generate_cover
        from cover_review import load_cover_review

        cover_path = project.path("previews/social-cover.png")
        manifest_path = project.path("manifests/cover-manifest.json")
        if not overwrite and (cover_path.exists() or manifest_path.exists()):
            try:
                current = load_cover_review(project, required=True)
            except ValueError:
                result = generate_cover(project.root, overwrite=True)
                result["status"] = "regenerated_stale"
                return result
            else:
                assert current is not None
                return {
                    "status": "reused",
                    "file": current["file"],
                    "sha256": current["sha256"],
                    "semanticSource": current["semanticSource"],
                }

        result = generate_cover(project.root, overwrite=overwrite)
        result["status"] = "generated"
        return result
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return {"error": str(exc), "semanticSource": "whole_video"}


def _cover_error(cover: dict[str, Any] | None) -> str | None:
    if not isinstance(cover, dict):
        return None
    value = cover.get("error")
    return f"封面生成失败: {value}" if isinstance(value, str) and value else None


def _host_task_descriptor(task: GenerationTask, *, run_id: str) -> dict[str, str]:
    return {
        "sceneId": task.scene_id,
        "prompt": task.prompt,
        "runId": run_id,
        "attemptId": task.attempt_id,
        "inputIdentitySha256": task.input_identity_sha256,
    }


def _host_task_package(tasks: list[GenerationTask], *, run_id: str) -> dict[str, Any]:
    return {
        "schemaVersion": HOST_IMAGE_SCHEMA_VERSION,
        "kind": "host-image-generation-tasks",
        "resultsSchemaVersion": HOST_IMAGE_SCHEMA_VERSION,
        "runId": run_id,
        "tasks": [_host_task_descriptor(task, run_id=run_id) for task in tasks],
    }


def _load_host_results(path_value: str) -> dict[str, Any]:
    path = Path(path_value).expanduser()
    if not path.is_absolute():
        raise CliArgumentError("--host-results 必须是绝对路径")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CliArgumentError("--host-results 不存在或不是有效 JSON") from exc
    if (
        not isinstance(document, dict)
        or document.get("schemaVersion") != HOST_IMAGE_SCHEMA_VERSION
        or document.get("kind") != "host-image-results"
    ):
        raise CliArgumentError("host results schema/kind 无效")
    run_id = document.get("runId")
    results = document.get("results")
    if not isinstance(run_id, str) or not run_id or not isinstance(results, list):
        raise CliArgumentError("host results runId/results 无效")
    seen: set[str] = set()
    for index, result in enumerate(results):
        if not isinstance(result, dict):
            raise CliArgumentError(f"host results[{index}] 必须是对象")
        scene_id = result.get("sceneId")
        attempt_id = result.get("attemptId")
        status = result.get("status")
        if (
            not isinstance(scene_id, str)
            or not scene_id
            or scene_id in seen
            or not isinstance(attempt_id, str)
            or not attempt_id
            or status not in {"succeeded", "failed"}
        ):
            raise CliArgumentError(f"host results[{index}] identity/status 无效")
        seen.add(scene_id)
        if status == "succeeded":
            if set(result) != {"sceneId", "attemptId", "status", "file"}:
                raise CliArgumentError(f"host results[{index}] succeeded 字段无效")
            source = result.get("file")
            if not isinstance(source, str) or not Path(source).is_absolute():
                raise CliArgumentError(f"host results[{index}].file 必须是绝对路径")
        else:
            if set(result) != {"sceneId", "attemptId", "status", "error"}:
                raise CliArgumentError(f"host results[{index}] failed 字段无效")
            error = result.get("error")
            if (
                not isinstance(error, str)
                or not error.strip()
                or len(error) > 1000
                or "\n" in error
                or "\r" in error
            ):
                raise CliArgumentError(f"host results[{index}].error 必须是去敏短文本")
    return document


def _generation_task_from_attempt(
    project: Any,
    scene: dict[str, Any],
    prompt: str,
    attempt: dict[str, Any],
) -> GenerationTask:
    candidate_path = (project.root / Path(attempt["candidateFile"])).resolve(strict=False)
    return GenerationTask(
        scene_id=scene["sceneId"],
        prompt=prompt,
        input_identity_sha256=attempt["inputIdentitySha256"],
        attempt_id=attempt["attemptId"],
        attempt_root=candidate_path.parent,
        candidate_path=candidate_path,
        formal_file=attempt["formalFile"],
    )


def _host_run_tasks(
    project: Any,
    plan_scenes: list[dict[str, Any]],
    prompts: dict[str, str],
    manifest: ManifestStore,
    run_id: str,
) -> list[GenerationTask]:
    tasks: list[GenerationTask] = []
    for scene in plan_scenes:
        attempt = manifest.current_attempt(scene["sceneId"])
        if attempt is None or attempt.get("runId") != run_id:
            continue
        if attempt.get("provider") != HOST_IMAGE_BACKEND.name:
            raise ManifestError("host run attempt provider binding 无效")
        tasks.append(_generation_task_from_attempt(project, scene, prompts[scene["sceneId"]], attempt))
    if not tasks:
        raise ManifestError("host run 没有绑定的 attempt")
    return tasks


def _waiting_host_run_id(manifest: ManifestStore) -> str | None:
    run_ids: set[str] = set()
    for scene in manifest.data.get("scenes", []):
        if not isinstance(scene, dict):
            continue
        attempt = manifest.current_attempt(scene["sceneId"])
        if (
            attempt is not None
            and attempt.get("provider") == HOST_IMAGE_BACKEND.name
            and attempt.get("status") in {"prepared", "requesting", "candidate_ready", "publishing"}
            and isinstance(attempt.get("runId"), str)
        ):
            run_ids.add(attempt["runId"])
    if len(run_ids) > 1:
        raise ManifestError("存在多个未完成 host run，拒绝猜测结果绑定")
    return next(iter(run_ids), None)


def _run_host_image_generation(
    *,
    args: argparse.Namespace,
    project: Any,
    plan_scenes: list[dict[str, Any]],
    scoped_plan_scenes: list[dict[str, Any]],
    prompts: dict[str, str],
    configured_concurrency: int,
    host_results: dict[str, Any] | None,
) -> int:
    manifest = ManifestStore.open(project.root, project.project_id, project.plan_path, plan_scenes)
    if args.provider is not None or args.config is not None:
        raise CliArgumentError("gpt-login 模式不接受 --provider/--config")

    if host_results is None:
        existing_run_id = _waiting_host_run_id(manifest)
        if existing_run_id is not None:
            tasks = _host_run_tasks(project, plan_scenes, prompts, manifest, existing_run_id)
            statuses = {
                manifest.current_attempt(task.scene_id).get("status")  # type: ignore[union-attr]
                for task in tasks
            }
            if statuses == {"prepared"}:
                _emit(
                    _summary(
                        ok=True,
                        exit_code=0,
                        project=str(project.root),
                        provider=HOST_IMAGE_BACKEND.name,
                        run_id=existing_run_id,
                        total=len(plan_scenes),
                        targeted=len(tasks),
                        configured_concurrency=configured_concurrency,
                        effective_concurrency=0,
                        task_count=len(tasks),
                        status="WAITING_HOST_IMAGE_GENERATION",
                        host_image_generation=_host_task_package(tasks, run_id=existing_run_id),
                    )
                )
                return 0
            # requesting 已是宿主调用提交点；没有结果合同就不能安全猜测成功或重发。
            project_lock = _acquire_generation_lock(project.root)
            try:
                manifest.resume_run(existing_run_id)
                failures: list[dict[str, str]] = []
                unknown = 0
                for task in tasks:
                    attempt = manifest.current_attempt(task.scene_id)
                    assert attempt is not None
                    if attempt.get("status") == "requesting":
                        manifest.mark_attempt(
                            task.scene_id,
                            status="unknown_external_outcome",
                            external_outcome="unknown_external_outcome",
                            error="宿主调用后未提供结果合同；禁止自动重试",
                        )
                        manifest.save()
                        unknown += 1
                        failures.append({"sceneId": task.scene_id, "error": "unknown_external_outcome"})
                manifest.update_active_run_counts(
                    adopted_candidate_count=0,
                    unknown_external_outcome_count=unknown,
                )
                manifest.finish_run(existing_run_id, exit_result=1)
                manifest.save()
            finally:
                _release_generation_lock(project_lock)
            _emit(
                _summary(
                    ok=False,
                    exit_code=1,
                    project=str(project.root),
                    provider=HOST_IMAGE_BACKEND.name,
                    run_id=existing_run_id,
                    total=len(plan_scenes),
                    targeted=len(tasks),
                    failed=len(failures),
                    configured_concurrency=configured_concurrency,
                    task_count=len(tasks),
                    unknown_external_outcome_count=unknown,
                    failures=failures,
                )
            )
            return 1

        targets = _selected_scenes(
            scoped_plan_scenes,
            manifest,
            retry_failed=args.retry_failed,
            overwrite=args.overwrite,
        )
        unknown = sum(
            record.get("status") == "unknown_external_outcome"
            for record in _scene_map(manifest).values()
        )
        if not targets:
            try:
                project_lock = _acquire_generation_lock(project.root)
            except ManifestError as exc:
                _emit(
                    _summary(
                        ok=False,
                        exit_code=1,
                        project=str(project.root),
                        provider=HOST_IMAGE_BACKEND.name,
                        total=len(plan_scenes),
                        skipped=len(plan_scenes),
                        configured_concurrency=configured_concurrency,
                        unknown_external_outcome_count=unknown,
                        warnings=["未生成或复用封面；请等待现有生图运行完成后读取其最终 JSON 结果"],
                        error=str(exc),
                    )
                )
                return 1
            try:
                manifest = ManifestStore.open(
                    project.root,
                    project.project_id,
                    project.plan_path,
                    plan_scenes,
                )
                unknown = sum(
                    record.get("status") == "unknown_external_outcome"
                    for record in _scene_map(manifest).values()
                )
                cover_result = _generate_cover_if_ready(
                    project,
                    ready=_all_formal_scenes_validated(project, manifest, plan_scenes) and unknown == 0,
                    overwrite=args.overwrite,
                )
            finally:
                _release_generation_lock(project_lock)
            cover_error = _cover_error(cover_result)
            exit_code = 1 if unknown or cover_error else 0
            _emit(
                _summary(
                    ok=unknown == 0 and cover_error is None,
                    exit_code=exit_code,
                    project=str(project.root),
                    provider=HOST_IMAGE_BACKEND.name,
                    total=len(plan_scenes),
                    skipped=len(plan_scenes),
                    configured_concurrency=configured_concurrency,
                    unknown_external_outcome_count=unknown,
                    error=cover_error,
                    cover=cover_result,
                )
            )
            return exit_code
        conflicts: list[dict[str, str]] = []
        for scene in targets:
            attempt = manifest.current_attempt(scene["sceneId"])
            recovering = attempt is not None and attempt.get("status") in RECOVERABLE_STATUSES
            formal = project.scenes_dir / scene["outputFile"]
            if formal.exists() and not args.overwrite and not recovering:
                conflicts.append({"sceneId": scene["sceneId"], "error": "正式目标已存在且未授权覆盖"})
        if conflicts:
            _emit(
                _summary(
                    ok=False,
                    exit_code=1,
                    project=str(project.root),
                    provider=HOST_IMAGE_BACKEND.name,
                    total=len(plan_scenes),
                    targeted=len(targets),
                    failed=len(conflicts),
                    configured_concurrency=configured_concurrency,
                    task_count=len(targets),
                    failures=conflicts,
                )
            )
            return 1

        run_id = f"img-{uuid.uuid4().hex[:12]}"
        run_dir = project.create_run_dir(run_id)
        project_lock = _acquire_generation_lock(project.root)
        try:
            manifest.begin_run(
                run_id,
                HOST_IMAGE_BACKEND,  # type: ignore[arg-type]
                configured_concurrency=configured_concurrency,
                effective_concurrency=0,
                task_count=len(targets),
            )
            tasks: list[GenerationTask] = []
            for scene in targets:
                scene_id = scene["sceneId"]
                ordinal = len(_scene_map(manifest)[scene_id].get("attemptRecords", [])) + 1
                attempt_id = f"{scene_id}-attempt-{ordinal:04d}"
                attempt_rel = Path(".work") / run_id / "external-tasks" / scene_id / f"a{ordinal:04d}"
                prompt = prompts[scene_id]
                identity = image_input_identity(
                    scene_id=scene_id,
                    prompt=prompt,
                    backend=HOST_IMAGE_BACKEND_IDENTITY,
                )
                attempt = manifest.prepare_attempt(
                    scene_id,
                    attempt_id=attempt_id,
                    input_identity_sha256=identity,
                    candidate_file=(attempt_rel / "candidate.png").as_posix(),
                    receipt_file=(attempt_rel / "candidate-receipt.json").as_posix(),
                    formal_file=f"scenes/{scene['outputFile']}",
                    overwrite=args.overwrite,
                    provider=HOST_IMAGE_BACKEND.name,
                    model=HOST_IMAGE_BACKEND.model,
                    prompt=prompt,
                    run_id=run_id,
                )
                manifest.save()
                task = _generation_task_from_attempt(project, scene, prompt, attempt)
                tasks.append(task)
                _checkpoint_hook("after_prepared", scene_id)
        finally:
            _release_generation_lock(project_lock)
        _emit(
            _summary(
                ok=True,
                exit_code=0,
                project=str(project.root),
                provider=HOST_IMAGE_BACKEND.name,
                run_id=run_id,
                total=len(plan_scenes),
                targeted=len(tasks),
                configured_concurrency=configured_concurrency,
                effective_concurrency=0,
                task_count=len(tasks),
                status="WAITING_HOST_IMAGE_GENERATION",
                host_image_generation=_host_task_package(tasks, run_id=run_id),
            )
        )
        return 0

    run_id = host_results["runId"]
    result_by_scene = {item["sceneId"]: item for item in host_results["results"]}
    project_lock = _acquire_generation_lock(project.root)
    failures: list[dict[str, str]] = []
    ready_candidates: list[tuple[dict[str, Any], ImageCandidate]] = []
    adopted = 0
    unknown = 0
    succeeded = 0
    cover_result: dict[str, Any] | None = None
    cover_error: str | None = None
    try:
        run = manifest.resume_run(run_id)
        if (
            run.get("provider") != HOST_IMAGE_BACKEND.name
            or run.get("protocol") != HOST_IMAGE_BACKEND.protocol
            or run.get("model") != HOST_IMAGE_BACKEND.model
        ):
            raise ManifestError("host results 与 run backend binding 不一致")
        tasks = _host_run_tasks(project, plan_scenes, prompts, manifest, run_id)
        task_by_scene = {task.scene_id: task for task in tasks}
        if set(result_by_scene) - set(task_by_scene):
            raise CliArgumentError("host results 包含不属于该 run 的 sceneId")
        for scene_id, result in result_by_scene.items():
            if result["attemptId"] != task_by_scene[scene_id].attempt_id:
                raise CliArgumentError(f"host result {scene_id} attemptId binding 不一致")

        # 结果文件表示宿主调用已经结束；先把全部 task 写入 requesting 提交点。
        for task in tasks:
            attempt = manifest.current_attempt(task.scene_id)
            assert attempt is not None
            if attempt.get("status") == "prepared":
                manifest.mark_attempt(task.scene_id, status="requesting", external_outcome="not_started")
                manifest.save()
                _checkpoint_hook("after_requesting", task.scene_id)

        scene_by_id = {scene["sceneId"]: scene for scene in plan_scenes}
        for task in tasks:
            attempt = manifest.current_attempt(task.scene_id)
            assert attempt is not None
            candidate_receipt = task.candidate_path.with_name("candidate-receipt.json")
            if task.candidate_path.is_file() and candidate_receipt.is_file():
                candidate = _candidate_for_attempt(project.root, task.scene_id, attempt)
                manifest.mark_attempt(
                    task.scene_id,
                    status="candidate_ready",
                    candidate=candidate,
                    external_outcome="succeeded",
                )
                manifest.save()
                ready_candidates.append((scene_by_id[task.scene_id], candidate))
                adopted += 1
                continue
            result = result_by_scene.get(task.scene_id)
            if result is None:
                manifest.mark_attempt(
                    task.scene_id,
                    status="unknown_external_outcome",
                    external_outcome="unknown_external_outcome",
                    error="宿主结果合同缺少该 scene；禁止自动重试",
                )
                manifest.save()
                unknown += 1
                failures.append({"sceneId": task.scene_id, "error": "unknown_external_outcome"})
                continue
            if result["status"] == "failed":
                error = result["error"].strip()
                manifest.mark_attempt(
                    task.scene_id,
                    status="failed",
                    external_outcome="explicit_failed",
                    error=error,
                )
                manifest.save()
                failures.append({"sceneId": task.scene_id, "error": error})
                continue
            try:
                source_path = Path(result["file"])
                image_bytes = source_path.read_bytes()
                candidate = normalize_image_candidate(
                    image_bytes,
                    task.candidate_path,
                    task.attempt_root,
                    task.scene_id,
                    attempt_id=task.attempt_id,
                    formal_file=task.formal_file,
                    input_identity_sha256=task.input_identity_sha256,
                    source="host_tool",
                    provider_attempts=1,
                )
                _checkpoint_hook("after_candidate_persisted", task.scene_id)
                manifest.mark_attempt(
                    task.scene_id,
                    status="candidate_ready",
                    candidate=candidate,
                    external_outcome="succeeded",
                )
                manifest.save()
                _checkpoint_hook("after_candidate_ready", task.scene_id)
                ready_candidates.append((scene_by_id[task.scene_id], candidate))
            except (OSError, ImageValidationError) as exc:
                error = f"host result import: {exc}"
                manifest.mark_attempt(
                    task.scene_id,
                    status="failed",
                    external_outcome="succeeded",
                    error=error,
                )
                manifest.save()
                failures.append({"sceneId": task.scene_id, "error": error})

        by_ready = {scene["sceneId"]: (scene, candidate) for scene, candidate in ready_candidates}
        for scene in plan_scenes:
            ready = by_ready.get(scene["sceneId"])
            if ready is None:
                continue
            candidate = ready[1]
            attempt = manifest.current_attempt(scene["sceneId"])
            assert attempt is not None
            try:
                _publish_candidate(
                    manifest=manifest,
                    scene_id=scene["sceneId"],
                    candidate=candidate,
                    formal_path=project.scenes_dir / scene["outputFile"],
                    overwrite=bool(attempt["overwrite"]),
                    run_dir=project.root / ".work" / run_id,
                )
                succeeded += 1
            except (OSError, ImageValidationError, ManifestError) as exc:
                failures.append({"sceneId": scene["sceneId"], "error": str(exc)})
        manifest.update_active_run_counts(
            adopted_candidate_count=adopted,
            unknown_external_outcome_count=unknown,
        )
        cover_result = _generate_cover_if_ready(
            project,
            ready=_all_formal_scenes_validated(project, manifest, plan_scenes) and not failures,
            overwrite=args.overwrite,
        )
        cover_error = _cover_error(cover_result)
        exit_code = 1 if failures or cover_error else 0
        manifest.finish_run(run_id, exit_result=exit_code)
        manifest.save()
    finally:
        _release_generation_lock(project_lock)

    exit_code = 1 if failures or cover_error else 0
    _emit(
        _summary(
            ok=not failures and cover_error is None,
            exit_code=exit_code,
            project=str(project.root),
            provider=HOST_IMAGE_BACKEND.name,
            run_id=run_id,
            total=len(plan_scenes),
            targeted=len(tasks),
            succeeded=succeeded,
            failed=len(failures),
            skipped=len(plan_scenes) - len(tasks),
            configured_concurrency=configured_concurrency,
            effective_concurrency=0,
            task_count=len(tasks),
            adopted_candidate_count=adopted,
            unknown_external_outcome_count=unknown,
            failures=failures,
            error=cover_error,
            cover=cover_result,
        )
    )
    return exit_code


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except CliArgumentError:
        _emit(_summary(ok=False, exit_code=2, project=None, error="参数无效"))
        return 2
    except SystemExit as exc:
        return int(exc.code)

    project_arg = str(Path(args.project).resolve(strict=False))
    try:
        host_results = _load_host_results(args.host_results) if args.host_results else None
    except CliArgumentError as exc:
        _emit(_summary(ok=False, exit_code=2, project=project_arg, error=str(exc)))
        return 2
    config_path = Path(args.config).expanduser() if args.config else DEFAULT_PROVIDER_CONFIG
    if args.config and not config_path.is_absolute():
        _emit(_summary(ok=False, exit_code=2, project=project_arg, error="--config 必须是绝对路径"))
        return 2
    config_path = config_path.resolve(strict=False)
    provider: ProviderConfig | None = None
    try:
        workspace = ProjectWorkspace.from_config()
        project = workspace.load_project(args.project)
        configured_concurrency = workspace.config.for_stage("imageGeneration")
        plan_scenes = project.plan["scenes"]
        if not plan_scenes:
            raise ProjectValidationError("generation plan 没有场景，不能执行完整生图")
        # 使用严格索引；缺失或空白 prompt 已由 load_project 在 provider 前拒绝。
        prompts = {
            scene["sceneId"]: build_final_prompt(project.plan["globalPrompt"], scene["prompt"])
            for scene in plan_scenes
        }
        scoped_plan_scenes = _scoped_plan_scenes(plan_scenes, args.scene_ids)
        image_generation_mode = project.metadata.get("imageGenerationMode", "provider")
        if image_generation_mode == "gpt-login":
            return _run_host_image_generation(
                args=args,
                project=project,
                plan_scenes=plan_scenes,
                scoped_plan_scenes=scoped_plan_scenes,
                prompts=prompts,
                configured_concurrency=configured_concurrency,
                host_results=host_results,
            )
        if host_results is not None:
            raise CliArgumentError("--host-results 只允许用于 gpt-login 项目")
        warnings = verify_config_git_safety(config_path)
        provider = load_provider_config(config_path, args.provider)
        manifest = ManifestStore.open(project.root, project.project_id, project.plan_path, plan_scenes)
        targets = _selected_scenes(
            scoped_plan_scenes,
            manifest,
            retry_failed=args.retry_failed,
            overwrite=args.overwrite,
        )
    except CredentialSafetyError as exc:
        _emit(_summary(ok=False, exit_code=3, project=project_arg, provider=args.provider, error=str(exc)))
        return 3
    except (OSError, WorkspaceError, ProjectValidationError, ConfigError, ManifestError, CliArgumentError) as exc:
        message = redact_secret(exc, provider.api_key) if provider is not None else str(exc)
        _emit(_summary(ok=False, exit_code=2, project=project_arg, provider=args.provider, error=message))
        return 2

    provider_name = provider.name
    if not targets:
        unknown = sum(
            record.get("status") == "unknown_external_outcome"
            for record in _scene_map(manifest).values()
        )
        try:
            project_lock = _acquire_generation_lock(project.root)
        except ManifestError as exc:
            _emit(
                _summary(
                    ok=False,
                    exit_code=1,
                    project=str(project.root),
                    provider=provider_name,
                    total=len(plan_scenes),
                    skipped=len(plan_scenes),
                    configured_concurrency=configured_concurrency,
                    unknown_external_outcome_count=unknown,
                    warnings=["未生成或复用封面；请等待现有生图运行完成后读取其最终 JSON 结果"],
                    error=str(exc),
                )
            )
            return 1
        try:
            manifest = ManifestStore.open(project.root, project.project_id, project.plan_path, plan_scenes)
            unknown = sum(
                record.get("status") == "unknown_external_outcome"
                for record in _scene_map(manifest).values()
            )
            cover_result = _generate_cover_if_ready(
                project,
                ready=_all_formal_scenes_validated(project, manifest, plan_scenes) and unknown == 0,
                overwrite=args.overwrite,
            )
        finally:
            _release_generation_lock(project_lock)
        cover_error = _cover_error(cover_result)
        exit_code = 1 if unknown or cover_error else 0
        _emit(
            _summary(
                ok=unknown == 0 and cover_error is None,
                exit_code=exit_code,
                project=str(project.root),
                provider=provider_name,
                total=len(plan_scenes),
                skipped=len(plan_scenes),
                configured_concurrency=configured_concurrency,
                unknown_external_outcome_count=unknown,
                warnings=warnings,
                error=cover_error,
                cover=cover_result,
            )
        )
        return exit_code

    # 所有新 attempt 的 overwrite 冲突统一在 client/worker 创建前 fail closed。
    conflicts: list[dict[str, str]] = []
    for scene in targets:
        record = _scene_map(manifest)[scene["sceneId"]]
        attempt = manifest.current_attempt(scene["sceneId"])
        recovering = attempt is not None and (
            attempt.get("status") in RECOVERABLE_STATUSES
            or (
                attempt.get("status") == "validated"
                and (project.root / Path(attempt["candidateFile"])).is_file()
            )
        )
        formal = project.scenes_dir / scene["outputFile"]
        if formal.exists() and not args.overwrite and not recovering:
            conflicts.append({"sceneId": scene["sceneId"], "error": "正式目标已存在且未授权覆盖"})

    # Windows 测试/项目路径可能接近 MAX_PATH；run/attempt 目录保持短而唯一。
    run_id = f"img-{uuid.uuid4().hex[:12]}"
    run_dir = project.create_run_dir(run_id)
    try:
        project_lock = _acquire_generation_lock(project.root)
    except ManifestError as exc:
        _emit(
            _summary(
                ok=False,
                exit_code=1,
                project=str(project.root),
                provider=provider_name,
                run_id=run_id,
                total=len(plan_scenes),
                targeted=len(targets),
                configured_concurrency=configured_concurrency,
                effective_concurrency=0,
                task_count=0,
                failures=[{"error": str(exc)}],
                warnings=["未启动新的 provider 请求；请等待现有运行完成后读取其最终 JSON 结果"],
            )
        )
        try:
            run_dir.rmdir()
        except OSError:
            pass
        return 1
    failures: list[dict[str, str]] = list(conflicts)
    adopted = 0
    unknown = 0
    succeeded = 0
    dispatch_tasks: list[GenerationTask] = []
    ready_candidates: list[tuple[dict[str, Any], ImageCandidate]] = []
    effective_concurrency = 0
    cover_result: dict[str, Any] | None = None
    cover_error: str | None = None
    try:
        if conflicts:
            manifest.begin_run(
                run_id,
                provider,
                configured_concurrency=configured_concurrency,
                effective_concurrency=0,
                task_count=len(targets),
            )
            manifest.finish_run(run_id, exit_result=1)
            manifest.save()
        else:
            # 先按 plan 顺序预登记全部新 attempt，并逐个持久化 prepared。
            for scene in targets:
                scene_id = scene["sceneId"]
                attempt = manifest.current_attempt(scene_id)
                validated_candidate_pending_cleanup = (
                    attempt is not None
                    and attempt.get("status") == "validated"
                    and (project.root / Path(attempt["candidateFile"])).is_file()
                )
                if attempt is None or (
                    attempt.get("status") not in RECOVERABLE_STATUSES
                    and not validated_candidate_pending_cleanup
                ):
                    ordinal = len(_scene_map(manifest)[scene_id].get("attemptRecords", [])) + 1
                    attempt_id = f"{scene_id}-attempt-{ordinal:04d}"
                    attempt_rel = (
                        Path(".work") / run_id / "external-tasks" / scene_id / f"a{ordinal:04d}"
                    )
                    prompt = prompts[scene_id]
                    identity = image_input_identity(scene_id=scene_id, prompt=prompt, provider=provider)
                    manifest.prepare_attempt(
                        scene_id,
                        attempt_id=attempt_id,
                        input_identity_sha256=identity,
                        candidate_file=(attempt_rel / "candidate.png").as_posix(),
                        receipt_file=(attempt_rel / "candidate-receipt.json").as_posix(),
                        formal_file=f"scenes/{scene['outputFile']}",
                        overwrite=args.overwrite,
                        provider=provider.name,
                        model=provider.model,
                        prompt=prompt,
                    )

            # 计算真实 provider dispatch 数后写 run 审计，再保存 prepared checkpoint。
            for scene in targets:
                attempt = manifest.current_attempt(scene["sceneId"])
                assert attempt is not None
                if attempt["status"] == "prepared":
                    dispatch_tasks.append(
                        GenerationTask(
                            scene_id=scene["sceneId"],
                            prompt=prompts[scene["sceneId"]],
                            input_identity_sha256=attempt["inputIdentitySha256"],
                            attempt_id=attempt["attemptId"],
                            attempt_root=(project.root / Path(attempt["candidateFile"])).parent.resolve(strict=False),
                            candidate_path=(project.root / Path(attempt["candidateFile"])).resolve(strict=False),
                            formal_file=attempt["formalFile"],
                        )
                    )
            effective_concurrency = min(configured_concurrency, len(dispatch_tasks)) if dispatch_tasks else 0
            manifest.begin_run(
                run_id,
                provider,
                configured_concurrency=configured_concurrency,
                effective_concurrency=effective_concurrency,
                task_count=len(targets),
            )
            manifest.save()
            for task in dispatch_tasks:
                _checkpoint_hook("after_prepared", task.scene_id)

            # 恢复旧 attempt；只按 manifest 登记路径处理，不扫描 .work。
            dispatch_ids = {task.scene_id for task in dispatch_tasks}
            for scene in targets:
                scene_id = scene["sceneId"]
                if scene_id in dispatch_ids:
                    continue
                attempt = manifest.current_attempt(scene_id)
                assert attempt is not None
                status = attempt["status"]
                candidate_path = project.root / Path(attempt["candidateFile"])
                if status == "requesting":
                    if candidate_path.is_file() and candidate_path.with_name("candidate-receipt.json").is_file():
                        candidate = _candidate_for_attempt(project.root, scene_id, attempt)
                        manifest.mark_attempt(scene_id, status="candidate_ready", candidate=candidate, external_outcome="succeeded")
                        manifest.save()
                        ready_candidates.append((scene, candidate))
                        adopted += 1
                    else:
                        manifest.mark_attempt(
                            scene_id,
                            status="unknown_external_outcome",
                            external_outcome="unknown_external_outcome",
                            error="requesting 中断且无完整 candidate/receipt；禁止自动重试",
                        )
                        manifest.save()
                        unknown += 1
                        failures.append({"sceneId": scene_id, "error": "unknown_external_outcome"})
                elif status in {"candidate_ready", "publishing"}:
                    candidate = _candidate_for_attempt(project.root, scene_id, attempt)
                    ready_candidates.append((scene, candidate))
                    adopted += 1
                elif status == "validated":
                    candidate = _candidate_for_attempt(project.root, scene_id, attempt)
                    _cleanup_candidate(candidate, (project.root / Path(attempt["candidateFile"])).parents[2])

            # requesting 是 provider 调用提交点；每个 checkpoint 由 coordinator 单写。
            for task in dispatch_tasks:
                manifest.mark_attempt(task.scene_id, status="requesting", external_outcome="not_started")
                manifest.save()
                _checkpoint_hook("after_requesting", task.scene_id)

            if dispatch_tasks:
                client = ImagesGenerationsClient(provider)
                report = execute_bounded(
                    dispatch_tasks,
                    lambda task: _worker(task, client=client, provider=provider),
                    max_workers=configured_concurrency,
                    failure_policy=CONTINUE_INDEPENDENT,
                )
                effective_concurrency = report.effective_workers
                for result in report.results:
                    task = result.task
                    attempt = manifest.current_attempt(task.scene_id)
                    assert attempt is not None
                    if result.outcome is not None and result.outcome.ok and result.outcome.value is not None:
                        candidate = result.outcome.value
                        manifest.mark_attempt(task.scene_id, status="candidate_ready", candidate=candidate, external_outcome="succeeded")
                        manifest.save()
                        _checkpoint_hook("after_candidate_ready", task.scene_id)
                        scene = next(item for item in targets if item["sceneId"] == task.scene_id)
                        ready_candidates.append((scene, candidate))
                        continue
                    # worker 可能在 candidate 原子落盘后崩溃；完整 receipt 可安全采用。
                    if task.candidate_path.is_file() and task.candidate_path.with_name("candidate-receipt.json").is_file():
                        candidate = _candidate_for_attempt(project.root, task.scene_id, attempt)
                        manifest.mark_attempt(task.scene_id, status="candidate_ready", candidate=candidate, external_outcome="succeeded")
                        manifest.save()
                        scene = next(item for item in targets if item["sceneId"] == task.scene_id)
                        ready_candidates.append((scene, candidate))
                        adopted += 1
                    else:
                        failure = result.outcome.error if result.outcome is not None else None
                        if failure is not None and failure.category == "explicit_external_failure":
                            manifest.mark_attempt(
                                task.scene_id,
                                status="failed",
                                external_outcome="explicit_failed",
                                error=failure.message,
                            )
                            failures.append({"sceneId": task.scene_id, "error": failure.message})
                        else:
                            manifest.mark_attempt(
                                task.scene_id,
                                status="unknown_external_outcome",
                                external_outcome="unknown_external_outcome",
                                error="provider 调用后未形成完整 candidate；禁止自动重试",
                            )
                            unknown += 1
                            failures.append({"sceneId": task.scene_id, "error": "unknown_external_outcome"})
                        manifest.save()

            # 正式发布始终由 coordinator 按 generation plan 顺序串行完成。
            by_ready = {scene["sceneId"]: (scene, candidate) for scene, candidate in ready_candidates}
            for scene in targets:
                ready = by_ready.get(scene["sceneId"])
                if ready is None:
                    continue
                candidate = ready[1]
                try:
                    _publish_candidate(
                        manifest=manifest,
                        scene_id=scene["sceneId"],
                        candidate=candidate,
                        formal_path=project.scenes_dir / scene["outputFile"],
                        overwrite=args.overwrite,
                        run_dir=run_dir,
                    )
                    succeeded += 1
                except (ImageValidationError, ManifestError, OSError) as exc:
                    error = redact_secret(exc, provider.api_key)
                    failures.append({"sceneId": scene["sceneId"], "error": error})
                    # publishing 保留 candidate 供下次 provider=0 恢复，不能降级成 failed。
            manifest.update_active_run_counts(
                adopted_candidate_count=adopted,
                unknown_external_outcome_count=unknown,
            )
            cover_result = _generate_cover_if_ready(
                project,
                ready=_all_formal_scenes_validated(project, manifest, plan_scenes) and not failures,
                overwrite=args.overwrite,
            )
            cover_error = _cover_error(cover_result)
            exit_code = 1 if failures or cover_error else 0
            manifest.finish_run(run_id, exit_result=exit_code)
            manifest.save()
    except (OSError, ManifestError, ImageValidationError) as exc:
        error = redact_secret(exc, provider.api_key)
        _emit(
            _summary(
                ok=False,
                exit_code=2,
                project=str(project.root),
                provider=provider_name,
                run_id=run_id,
                total=len(plan_scenes),
                targeted=len(targets),
                succeeded=succeeded,
                failed=len(failures),
                skipped=len(plan_scenes) - len(targets),
                configured_concurrency=configured_concurrency,
                effective_concurrency=effective_concurrency,
                task_count=len(targets),
                adopted_candidate_count=adopted,
                unknown_external_outcome_count=unknown,
                failures=failures,
                warnings=warnings,
                error=error,
            )
        )
        return 2
    finally:
        _release_generation_lock(project_lock)
        try:
            run_dir.rmdir()
        except OSError:
            pass

    exit_code = 1 if failures or cover_error else 0
    _emit(
        _summary(
            ok=not failures and cover_error is None,
            exit_code=exit_code,
            project=str(project.root),
            provider=provider_name,
            run_id=run_id,
            total=len(plan_scenes),
            targeted=len(targets),
            succeeded=succeeded,
            failed=len(failures),
            skipped=len(plan_scenes) - len(targets),
            configured_concurrency=configured_concurrency,
            effective_concurrency=effective_concurrency,
            task_count=len(targets),
            adopted_candidate_count=adopted,
            unknown_external_outcome_count=unknown,
            failures=failures,
            warnings=warnings,
            error=cover_error,
            cover=cover_result,
        )
    )
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
