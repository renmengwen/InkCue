#!/usr/bin/env python3
"""按 generation plan 有界并发生图，由 coordinator 串行提交正式图片与 manifest。"""
from __future__ import annotations

import argparse
import json
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
        elif retry_failed:
            if record.get("status") == "failed":
                selected.append(scene)
        else:
            selected.append(scene)
    return selected


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
    return parser


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
        warnings = verify_config_git_safety(config_path)
        provider = load_provider_config(config_path, args.provider)
        manifest = ManifestStore.open(project.root, project.project_id, project.plan_path, plan_scenes)
        targets = _selected_scenes(plan_scenes, manifest, retry_failed=args.retry_failed)
    except CredentialSafetyError as exc:
        _emit(_summary(ok=False, exit_code=3, project=project_arg, provider=args.provider, error=str(exc)))
        return 3
    except (OSError, WorkspaceError, ProjectValidationError, ConfigError, ManifestError) as exc:
        message = redact_secret(exc, provider.api_key) if provider is not None else str(exc)
        _emit(_summary(ok=False, exit_code=2, project=project_arg, provider=args.provider, error=message))
        return 2

    provider_name = provider.name
    if not targets:
        unknown = sum(
            record.get("status") == "unknown_external_outcome"
            for record in _scene_map(manifest).values()
        )
        _emit(
            _summary(
                ok=unknown == 0,
                exit_code=1 if unknown else 0,
                project=str(project.root),
                provider=provider_name,
                total=len(plan_scenes),
                skipped=len(plan_scenes),
                configured_concurrency=configured_concurrency,
                unknown_external_outcome_count=unknown,
                warnings=warnings,
            )
        )
        return 1 if unknown else 0

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
    failures: list[dict[str, str]] = list(conflicts)
    adopted = 0
    unknown = 0
    succeeded = 0
    dispatch_tasks: list[GenerationTask] = []
    ready_candidates: list[tuple[dict[str, Any], ImageCandidate]] = []
    effective_concurrency = 0
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
            exit_code = 1 if failures else 0
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
        try:
            run_dir.rmdir()
        except OSError:
            pass

    exit_code = 1 if failures else 0
    _emit(
        _summary(
            ok=not failures,
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
        )
    )
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
