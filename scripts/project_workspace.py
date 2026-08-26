#!/usr/bin/env python3
"""D 盘项目工作区、项目元数据和 generation plan 的共享校验。"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping


SKILL_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_WORKSPACE_CONFIG = SKILL_ROOT / "config" / "workspace.local.json"
DEFAULT_WORKSPACE_ROOT = Path(r"D:\SRTWhiteboard")

PROJECT_SCHEMA_VERSION = 2
SUPPORTED_PROJECT_SCHEMA_VERSIONS = {1, 2}
IMAGE_GENERATION_MODES = {"provider", "gpt-login"}
INITIAL_APPROVAL_PENDING = "pending_initial_approval"
INITIAL_APPROVAL_APPROVED = "approved"
PLAN_SCHEMA_VERSION = 1
PROJECT_PATHS_V1 = {
    "planning": "planning",
    "scenes": "scenes",
    "manifests": "manifests",
    "previews": "previews",
    "output": "output",
    "work": ".work",
}
PROJECT_PATHS_V2 = {
    **PROJECT_PATHS_V1,
    "audio": "audio",
    "subtitles": "subtitles",
}
PROJECT_PATHS = PROJECT_PATHS_V2
AUDIO_VOICEOVER_MODES = {"edge-tts", "minimax", "doubao"}
VOICEOVER_MODES = {"disabled", *AUDIO_VOICEOVER_MODES}
CONTENT_SOURCE_FIELDS = {
    "contractVersion",
    "inputFile",
    "inputSha256",
    "inputIdentitySha256",
    "manifestFile",
    "manifestSha256",
    "generationPlanSha256",
    "sourcePackageIdentitySha256",
}
FIXED_RENDER_PROFILE = {
    "contractVersion": "whiteboard-render-v2",
    "width": 1920,
    "height": 1080,
    "fps": 60,
    "pixelFormat": "yuv420p",
    "videoCodec": "h264",
    "frameRounding": "cumulative-ceil-v1",
}
FIXED_CANVAS = {
    "width": 1920,
    "height": 1080,
    "background": "#F5EBD7",
    "fit": "contain",
}
DEFAULT_GLOBAL_PROMPT = (
    "暖米黄纸张背景上的简洁白板手绘线稿，统一黑色墨线、少量柔和强调色、"
    "清晰留白与横向构图；画面不得包含任何文字、字母、数字、水印或标志。"
)

WORKER_STAGE_FIELDS = {
    "imageGeneration": "image_generation",
    "voiceGeneration": "voice_generation",
    "imageValidation": "image_validation",
    "voiceValidation": "voice_validation",
    "annotationValidation": "annotation_validation",
    "annotationPreview": "annotation_preview",
    "sceneRender": "scene_render",
    "sceneMediaValidation": "scene_media_validation",
    "finalMediaValidation": "final_media_validation",
}
AGENT_ROLE_FIELDS = {
    "contentDrafting": "content_drafting",
    "storyboardPlanning": "storyboard_planning",
    "visualReview": "visual_review",
    "annotationDrafting": "annotation_drafting",
}
SUBTITLE_PRESETS = frozenset({"medium", "fast", "veryfast"})
REVIEW_POLICIES = frozenset({"user_first", "agent_first"})


class WorkspaceError(ValueError):
    """工作区配置或工作区磁盘不可用。"""


class ProjectValidationError(ValueError):
    """项目元数据、路径或 generation plan 无效。"""


@dataclass(frozen=True)
class WorkspaceAccessProbe:
    """一次真实 create/write/read/delete 工作区访问探针。"""

    root: Path
    ok: bool
    code: str
    stage: str
    message: str
    probe_file: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "code": self.code,
            "stage": self.stage,
            "workspaceRoot": str(self.root),
            "message": self.message,
            "probeFile": self.probe_file,
        }


def _require_concurrency_value(value: Any, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 16:
        raise WorkspaceError(f"{label} 必须是 1–16 的整数，且不能是 bool")
    return value


def _require_subtitle_preset(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or value not in SUBTITLE_PRESETS:
        allowed = " | ".join(sorted(SUBTITLE_PRESETS))
        raise WorkspaceError(f"{label} 必须是以下字符串之一: {allowed}")
    return value


@dataclass(frozen=True)
class ExecutionConcurrency:
    """脚本 worker 的独立并发资源池。"""

    default: int = 1
    image_generation: int | None = None
    voice_generation: int | None = None
    image_validation: int | None = None
    voice_validation: int | None = None
    annotation_validation: int | None = None
    annotation_preview: int | None = None
    scene_render: int | None = None
    scene_media_validation: int | None = None
    final_media_validation: int | None = None

    def __post_init__(self) -> None:
        _require_concurrency_value(self.default, label="execution.concurrency.default")
        for stage, attribute in WORKER_STAGE_FIELDS.items():
            value = getattr(self, attribute)
            if value is not None:
                _require_concurrency_value(value, label=f"execution.concurrency.{stage}")

    def for_stage(self, stage: str) -> int:
        try:
            attribute = WORKER_STAGE_FIELDS[stage]
        except (KeyError, TypeError) as exc:
            raise WorkspaceError(f"未知 worker stage: {stage}") from exc
        configured = getattr(self, attribute)
        return self.default if configured is None else configured


@dataclass(frozen=True)
class ExecutionAgentConcurrency:
    """subagent 的独立并发资源池；不从 worker default 继承。"""

    default: int = 1
    content_drafting: int | None = None
    storyboard_planning: int | None = None
    visual_review: int | None = None
    annotation_drafting: int | None = None

    def __post_init__(self) -> None:
        _require_concurrency_value(self.default, label="execution.agents.default")
        for role, attribute in AGENT_ROLE_FIELDS.items():
            value = getattr(self, attribute)
            if value is not None:
                _require_concurrency_value(value, label=f"execution.agents.{role}")

    def for_role(self, role: str) -> int:
        try:
            attribute = AGENT_ROLE_FIELDS[role]
        except (KeyError, TypeError) as exc:
            raise WorkspaceError(f"未知 agent role: {role}") from exc
        configured = getattr(self, attribute)
        return self.default if configured is None else configured


@dataclass(frozen=True)
class ExecutionVideoEncoding:
    """正式视频编码选项；与 worker/agent 并发资源池分离。"""

    subtitle_preset: str = "medium"

    def __post_init__(self) -> None:
        _require_subtitle_preset(
            self.subtitle_preset,
            label="execution.videoEncoding.subtitlePreset",
        )


@dataclass(frozen=True)
class WorkspaceConfig:
    root: Path
    config_path: Path
    concurrency: ExecutionConcurrency = ExecutionConcurrency()
    agents: ExecutionAgentConcurrency = ExecutionAgentConcurrency()
    video_encoding: ExecutionVideoEncoding = ExecutionVideoEncoding()

    @property
    def projects_dir(self) -> Path:
        return self.root / "projects"

    @property
    def runtime_dir(self) -> Path:
        return self.root / "runtime"

    def for_stage(self, stage: str) -> int:
        return self.concurrency.for_stage(stage)

    def for_role(self, role: str) -> int:
        return self.agents.for_role(role)

@dataclass(frozen=True)
class Project:
    root: Path
    metadata: dict[str, Any]
    plan: dict[str, Any]
    timing_plan: dict[str, Any]
    pending_audio_timeline: bool = False

    @property
    def project_id(self) -> str:
        return self.metadata["projectId"]

    @property
    def plan_path(self) -> Path:
        return safe_project_path(self.root, "planning/generation-plan.json")

    @property
    def timing_plan_path(self) -> Path:
        return safe_project_path(self.root, "planning/timing-plan.json")

    @property
    def schema_version(self) -> int:
        return self.metadata["schemaVersion"]

    @property
    def voiceover_mode(self) -> str:
        if self.schema_version == 1:
            return "disabled"
        return self.metadata["voiceoverMode"]

    @property
    def background_music_enabled(self) -> bool:
        value = self.metadata.get("backgroundMusic")
        return bool(value.get("enabled")) if isinstance(value, dict) else False

    @property
    def agent_approval_enabled(self) -> bool:
        if self.schema_version == 1:
            return False
        return self.metadata.get("agentApprovalEnabled", False)

    @property
    def image_generation_mode(self) -> str:
        return self.metadata.get("imageGenerationMode", "provider")

    @property
    def initial_approval_completed(self) -> bool:
        """旧项目缺少该字段时，兼容视为已经完成初始批准。"""
        approval = self.metadata.get("initialApproval")
        return approval is None or approval.get("status") == INITIAL_APPROVAL_APPROVED

    @property
    def pending_initial_approval(self) -> bool:
        return not self.initial_approval_completed

    @property
    def current_content_identity_sha256(self) -> str:
        content_source = self.metadata.get("contentSource")
        if isinstance(content_source, Mapping):
            return str(content_source["inputIdentitySha256"])
        return sha256_json(
            {
                "contractVersion": "whiteboard-initial-content-identity-v1",
                "sourceSrtSha256": self.metadata["source"]["sha256"],
                "generationPlan": self.plan,
            }
        )

    @property
    def render_profile(self) -> dict[str, Any]:
        if self.schema_version == 1:
            return dict(FIXED_RENDER_PROFILE)
        return dict(self.metadata["renderProfile"])

    @property
    def timing_plan_persisted(self) -> bool:
        return self.schema_version == 2

    @property
    def scenes_dir(self) -> Path:
        return safe_project_path(self.root, self.metadata["paths"]["scenes"])

    def path(self, relative_path: str | Path) -> Path:
        return safe_project_path(self.root, relative_path)

    def create_run_dir(self, run_id: str) -> Path:
        if not run_id or Path(run_id).name != run_id or run_id in {".", ".."}:
            raise ProjectValidationError("运行 ID 必须是不含目录的非空名称")
        run_dir = self.path(Path(self.metadata["paths"]["work"]) / run_id)
        run_dir.mkdir(parents=True, exist_ok=False)
        return run_dir


def _load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise WorkspaceError(f"缺少{label}: {path}") from exc
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise WorkspaceError(f"无法读取{label} {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise WorkspaceError(f"{label}顶层必须是 JSON 对象: {path}")
    return value


def _is_windows_absolute(path_text: str) -> bool:
    return bool(re.match(r"^[A-Za-z]:[\\/]", path_text))


def _resolved(path: Path) -> Path:
    return path.resolve(strict=False)


def _require_d_drive(path: Path) -> None:
    drive = path.drive.upper()
    if drive != "D:":
        raise WorkspaceError(f"workspaceRoot 必须位于 D 盘，实际为: {path}")
    drive_root = Path(f"{drive}\\")
    if not drive_root.exists():
        raise WorkspaceError(f"目标盘不可用: {drive_root}")


def _workspace_probe_failure(path: Path, stage: str, exc: OSError) -> WorkspaceAccessProbe:
    denied = isinstance(exc, PermissionError) or getattr(exc, "winerror", None) in {5, 32}
    code = "workspace_write_denied" if denied else "workspace_unavailable"
    hint = (
        "当前进程不能写目标工作区。若 Codex UI 刚切换为完全访问，请在新回合先重新运行工作区预检。"
        if denied
        else "目标盘、目录或文件系统当前不可用，请检查盘符、磁盘状态和 Windows ACL。"
    )
    return WorkspaceAccessProbe(
        root=path,
        ok=False,
        code=code,
        stage=stage,
        message=f"{hint} 原始错误: {type(exc).__name__}: {exc}",
    )


def probe_workspace_access(path: str | Path) -> WorkspaceAccessProbe:
    """区分目录创建、写入、读回和清理失败，并且不依赖 shell 写文件。"""

    root = _resolved(Path(path))
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return _workspace_probe_failure(root, "create_directory", exc)

    probe = root / f".workspace-write-test-{uuid.uuid4().hex}"
    created = False
    try:
        with probe.open("x", encoding="utf-8") as handle:
            created = True
            handle.write("workspace-access-ok")
            handle.flush()
            os.fsync(handle.fileno())
        if probe.read_text(encoding="utf-8") != "workspace-access-ok":
            raise OSError("工作区探针读回内容不一致")
    except OSError as exc:
        failure = _workspace_probe_failure(root, "write_and_read", exc)
        if created:
            try:
                probe.unlink(missing_ok=True)
            except OSError:
                pass
        return WorkspaceAccessProbe(
            root=failure.root,
            ok=failure.ok,
            code=failure.code,
            stage=failure.stage,
            message=failure.message,
            probe_file=str(probe),
        )

    try:
        probe.unlink()
    except OSError as exc:
        return WorkspaceAccessProbe(
            root=root,
            ok=False,
            code="workspace_probe_cleanup_failed",
            stage="delete_probe",
            message=(
                "工作区可以创建和读取文件，但无法删除访问探针；请检查杀毒软件占用或 Windows ACL。"
                f" 原始错误: {type(exc).__name__}: {exc}"
            ),
            probe_file=str(probe),
        )
    return WorkspaceAccessProbe(
        root=root,
        ok=True,
        code="workspace_access_ok",
        stage="complete",
        message="工作区 create/write/flush/read/delete 预检通过。",
    )


def _verify_writable_directory(path: Path) -> None:
    result = probe_workspace_access(path)
    if not result.ok:
        raise WorkspaceError(f"{result.code}: {result.message} workspaceRoot={path}")


def _parse_execution_pool(
    value: Any,
    *,
    label: str,
    field_map: Mapping[str, str],
    pool_type: type[ExecutionConcurrency] | type[ExecutionAgentConcurrency],
) -> ExecutionConcurrency | ExecutionAgentConcurrency:
    if not isinstance(value, dict):
        raise WorkspaceError(f"{label} 必须是 JSON 对象")
    allowed = {"default", *field_map}
    unknown = set(value) - allowed
    if unknown:
        raise WorkspaceError(f"{label} 包含未知字段: {', '.join(sorted(map(str, unknown)))}")
    kwargs: dict[str, int] = {}
    for json_name, raw_value in value.items():
        attribute = "default" if json_name == "default" else field_map[json_name]
        kwargs[attribute] = _require_concurrency_value(raw_value, label=f"{label}.{json_name}")
    return pool_type(**kwargs)


def _parse_execution_agents(value: Any) -> ExecutionAgentConcurrency:
    label = "execution.agents"
    if not isinstance(value, dict):
        raise WorkspaceError(f"{label} 必须是 JSON 对象")
    allowed = {"default", *AGENT_ROLE_FIELDS}
    unknown = set(value) - allowed
    if unknown:
        raise WorkspaceError(
            f"{label} 包含未知字段: {', '.join(sorted(map(str, unknown)))}"
        )
    kwargs: dict[str, Any] = {}
    for json_name, raw_value in value.items():
        attribute = "default" if json_name == "default" else AGENT_ROLE_FIELDS[json_name]
        kwargs[attribute] = _require_concurrency_value(
            raw_value,
            label=f"{label}.{json_name}",
        )
    return ExecutionAgentConcurrency(**kwargs)


def _parse_video_encoding(value: Any) -> ExecutionVideoEncoding:
    label = "execution.videoEncoding"
    if not isinstance(value, dict):
        raise WorkspaceError(f"{label} 必须是 JSON 对象")
    unknown = set(value) - {"subtitlePreset"}
    if unknown:
        raise WorkspaceError(f"{label} 包含未知字段: {', '.join(sorted(map(str, unknown)))}")
    preset = (
        _require_subtitle_preset(
            value["subtitlePreset"],
            label=f"{label}.subtitlePreset",
        )
        if "subtitlePreset" in value
        else "medium"
    )
    return ExecutionVideoEncoding(subtitle_preset=preset)


def _parse_execution(
    value: Any,
) -> tuple[ExecutionConcurrency, ExecutionAgentConcurrency, ExecutionVideoEncoding]:
    if not isinstance(value, dict):
        raise WorkspaceError("execution 必须是 JSON 对象")
    unknown = set(value) - {"agents", "concurrency", "videoEncoding"}
    if unknown:
        raise WorkspaceError(f"execution 包含未知字段: {', '.join(sorted(map(str, unknown)))}")
    agents = (
        _parse_execution_agents(value["agents"])
        if "agents" in value
        else ExecutionAgentConcurrency()
    )
    concurrency = (
        _parse_execution_pool(
            value["concurrency"],
            label="execution.concurrency",
            field_map=WORKER_STAGE_FIELDS,
            pool_type=ExecutionConcurrency,
        )
        if "concurrency" in value
        else ExecutionConcurrency()
    )
    video_encoding = (
        _parse_video_encoding(value["videoEncoding"])
        if "videoEncoding" in value
        else ExecutionVideoEncoding()
    )
    assert isinstance(concurrency, ExecutionConcurrency)
    assert isinstance(agents, ExecutionAgentConcurrency)
    return concurrency, agents, video_encoding


def load_workspace_config(
    config_path: str | Path | None = None,
    *,
    verify_writable: bool = True,
) -> WorkspaceConfig:
    """读取本地配置；缺失或无效时绝不回退到 C 盘或系统临时目录。"""
    path = _resolved(Path(config_path) if config_path else DEFAULT_WORKSPACE_CONFIG)
    raw = _load_json(path, "工作区配置")
    if raw.get("schemaVersion") != 1:
        raise WorkspaceError("工作区配置 schemaVersion 必须为 1")
    if "execution" in raw:
        concurrency, agents, video_encoding = _parse_execution(raw["execution"])
    else:
        concurrency, agents, video_encoding = (
            ExecutionConcurrency(),
            ExecutionAgentConcurrency(),
            ExecutionVideoEncoding(),
        )
    root_text = raw.get("workspaceRoot")
    if not isinstance(root_text, str) or not root_text.strip():
        raise WorkspaceError("workspaceRoot 必须是非空绝对路径")
    root_text = root_text.strip()
    if os.name == "nt":
        root_candidate = Path(root_text)
        if not root_candidate.is_absolute():
            raise WorkspaceError("workspaceRoot 必须是绝对路径")
    else:
        # 仅用于在非 Windows 主机上明确识别配置；本 Skill 仍要求 D 盘。
        if not _is_windows_absolute(root_text):
            raise WorkspaceError("workspaceRoot 必须是 Windows 绝对路径")
        root_candidate = Path(root_text)
    root = _resolved(root_candidate)
    _require_d_drive(root)
    if verify_writable:
        _verify_writable_directory(root)
    return WorkspaceConfig(
        root=root,
        config_path=path,
        concurrency=concurrency,
        agents=agents,
        video_encoding=video_encoding,
    )


def resolve_project_review_policy(
    project: Project,
    requested: str | None = None,
) -> str:
    """读取完整旁白批准时冻结的策略，并拒绝后续阶段静默改写。"""

    if requested is not None and requested not in REVIEW_POLICIES:
        raise ProjectValidationError("review policy 必须是 user_first 或 agent_first")
    if project.agent_approval_enabled:
        if requested == "user_first":
            raise ProjectValidationError(
                "agentApprovalEnabled=true 与 reviewPolicy=user_first 冲突"
            )
        requested = "agent_first"
    if project.voiceover_mode == "disabled":
        # 静音项目没有 approve-full Gate；保留其现有显式调用兼容入口。
        return requested or "user_first"

    manifest_path = project.path("manifests/voice-manifest.json")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ProjectValidationError(f"无法读取 current voice manifest: {exc}") from exc
    approval = manifest.get("fullApproval") if isinstance(manifest, Mapping) else None
    frozen = approval.get("reviewPolicy") if isinstance(approval, Mapping) else None
    if not isinstance(approval, Mapping) or approval.get("approved") is not True:
        raise ProjectValidationError("视觉阶段要求 current approve-full")
    if frozen not in REVIEW_POLICIES:
        raise ProjectValidationError(
            "完整旁白批准尚未冻结 reviewPolicy；请对 current FULL_IDENTITY 重新执行 "
            "approve-full --review-policy user_first|agent_first"
        )
    if requested is not None and requested != frozen:
        raise ProjectValidationError(
            f"请求的 reviewPolicy={requested} 与 approve-full 冻结值 {frozen} 不一致"
        )
    return str(frozen)


def sanitize_project_name(name: str) -> str:
    if not isinstance(name, str):
        raise ProjectValidationError("项目名必须是字符串")
    sanitized = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "-", name)
    sanitized = sanitized.rstrip(". ")
    if not sanitized:
        raise ProjectValidationError("项目名清理后为空")
    return sanitized


def safe_project_path(project_root: str | Path, relative_path: str | Path) -> Path:
    root = _resolved(Path(project_root))
    relative = Path(relative_path)
    if relative.is_absolute() or _is_windows_absolute(str(relative_path)):
        raise ProjectValidationError(f"项目内部路径必须是相对路径: {relative_path}")
    target = _resolved(root / relative)
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise ProjectValidationError(f"项目内部路径越出项目根目录: {relative_path}") from exc
    if target == root:
        raise ProjectValidationError("项目内部路径不得指向项目根目录本身")
    return target


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def write_json_atomic(path: str | Path, value: Mapping[str, Any]) -> None:
    """在目标同目录写候选并原子替换；失败时不破坏旧正式文件。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    candidate = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        with candidate.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(candidate, target)
    finally:
        try:
            candidate.unlink()
        except FileNotFoundError:
            pass


def create_generation_plan(
    project_id: str,
    confirmed_plan: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """创建计划；无显式策略时只生成有效空场景骨架。"""
    if confirmed_plan is None:
        plan: dict[str, Any] = {
            "schemaVersion": PLAN_SCHEMA_VERSION,
            "projectId": project_id,
            "outputCanvas": dict(FIXED_CANVAS),
            "globalPrompt": DEFAULT_GLOBAL_PROMPT,
            "constraints": {"forbidText": True},
            "scenesDirectory": "scenes",
            "manifestFile": "manifests/generation-manifest.json",
            "scenes": [],
        }
    else:
        # JSON round-trip produces a detached structure using only JSON-compatible values.
        try:
            plan = json.loads(json.dumps(confirmed_plan, ensure_ascii=False))
        except (TypeError, ValueError) as exc:
            raise ProjectValidationError(f"generation plan 不是有效 JSON 数据: {exc}") from exc
        if not isinstance(plan, dict):
            raise ProjectValidationError("generation plan 顶层必须是对象")
        supplied_id = plan.get("projectId")
        if supplied_id not in (None, "", project_id):
            raise ProjectValidationError("已确认策略中的 projectId 与新项目不一致")
        plan["projectId"] = project_id
    validate_generation_plan_data(plan, project_id=project_id)
    return plan


def _contains_sensitive_key(value: Any) -> bool:
    sensitive = {"apikey", "api_key", "authorization", "credential", "credentials", "secret"}
    if isinstance(value, dict):
        for key, child in value.items():
            if isinstance(key, str) and key.casefold() in sensitive:
                return True
            if _contains_sensitive_key(child):
                return True
    elif isinstance(value, list):
        return any(_contains_sensitive_key(item) for item in value)
    return False


def validate_generation_plan_data(plan: Any, *, project_id: str) -> dict[str, Any]:
    if not isinstance(plan, dict):
        raise ProjectValidationError("generation plan 顶层必须是对象")
    if plan.get("schemaVersion") != PLAN_SCHEMA_VERSION:
        raise ProjectValidationError("generation plan schemaVersion 必须为 1")
    if plan.get("projectId") != project_id:
        raise ProjectValidationError("generation plan projectId 与 project.json 不一致")
    if plan.get("outputCanvas") != FIXED_CANVAS:
        raise ProjectValidationError("outputCanvas 必须严格为 1920x1080、#F5EBD7、contain")
    prompt = plan.get("globalPrompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ProjectValidationError("globalPrompt 不能为空")
    constraints = plan.get("constraints")
    if not isinstance(constraints, dict) or constraints.get("forbidText") is not True:
        raise ProjectValidationError("constraints.forbidText 必须严格为 true")
    if plan.get("scenesDirectory") != "scenes":
        raise ProjectValidationError("scenesDirectory 必须为 scenes")
    if plan.get("manifestFile") != "manifests/generation-manifest.json":
        raise ProjectValidationError("manifestFile 必须为 manifests/generation-manifest.json")
    scenes = plan.get("scenes")
    if not isinstance(scenes, list):
        raise ProjectValidationError("scenes 必须是数组")
    scene_ids: set[str] = set()
    output_files: set[str] = set()
    for index, scene in enumerate(scenes):
        label = f"scenes[{index}]"
        if not isinstance(scene, dict):
            raise ProjectValidationError(f"{label} 必须是对象")
        if "imagePrompt" in scene:
            raise ProjectValidationError(
                f"{label}.imagePrompt 仅属于 content draft；formal generation plan 必须使用 prompt"
            )
        scene_id = scene.get("sceneId")
        if not isinstance(scene_id, str) or not scene_id:
            raise ProjectValidationError(f"{label}.sceneId 不能为空")
        if scene_id in scene_ids:
            raise ProjectValidationError(f"sceneId 重复: {scene_id}")
        scene_ids.add(scene_id)
        scene_prompt = scene.get("prompt")
        if not isinstance(scene_prompt, str) or not scene_prompt.strip():
            raise ProjectValidationError(f"{label}.prompt 必须是去空白后非空字符串")
        duration = scene.get("sceneDurationMs")
        if isinstance(duration, bool) or not isinstance(duration, int) or duration <= 0:
            raise ProjectValidationError(f"{label}.sceneDurationMs 必须为正整数")
        output_file = scene.get("outputFile")
        if not isinstance(output_file, str) or not output_file:
            raise ProjectValidationError(f"{label}.outputFile 不能为空")
        output_path = Path(output_file)
        if (
            output_path.name != output_file
            or output_path.is_absolute()
            or _is_windows_absolute(output_file)
            or ".." in output_file
            or "/" in output_file
            or "\\" in output_file
            or output_path.suffix.casefold() != ".png"
        ):
            raise ProjectValidationError(f"{label}.outputFile 必须是安全的 .png 文件名")
        output_key = output_file.casefold()
        if output_key in output_files:
            raise ProjectValidationError(f"outputFile 重复: {output_file}")
        output_files.add(output_key)
    if _contains_sensitive_key(plan):
        raise ProjectValidationError("generation plan 不得包含密钥或供应商凭据字段")
    return plan


def validate_pre_project_generation_plan_data(
    candidate: Any,
    *,
    source_srt_path: str | Path,
    voiceover_mode: str = "disabled",
) -> dict[str, Any]:
    """只读校验传统 SRT 的建项前 generation-plan candidate。

    candidate 不带正式 projectId；本函数仅在内存注入临时 UUID，复用正式
    generation/timing validator。它不创建项目、不写批准，也不修改 source SRT。
    """
    if voiceover_mode not in VOICEOVER_MODES:
        raise ProjectValidationError("voiceoverMode 只允许 disabled、edge-tts、minimax 或 doubao")
    source_path = _resolved(Path(source_srt_path))
    if not source_path.is_file():
        raise ProjectValidationError(f"原始 SRT 不存在: {source_path}")
    if not isinstance(candidate, dict):
        raise ProjectValidationError("pre-project generation plan 顶层必须是对象")
    try:
        detached = json.loads(json.dumps(candidate, ensure_ascii=False))
    except (TypeError, ValueError) as exc:
        raise ProjectValidationError(f"pre-project generation plan 不是有效 JSON 数据: {exc}") from exc
    if detached.get("projectId") not in (None, ""):
        raise ProjectValidationError("pre-project generation plan 不得携带正式 projectId")
    scenes = detached.get("scenes")
    if not isinstance(scenes, list) or not scenes:
        raise ProjectValidationError("pre-project generation plan scenes 必须是非空数组")
    for index, scene in enumerate(scenes):
        label = f"scenes[{index}]"
        if not isinstance(scene, dict):
            raise ProjectValidationError(f"{label} 必须是对象")
        if "imagePrompt" in scene:
            raise ProjectValidationError(f"{label}.imagePrompt 仅属于 content draft；请使用 prompt")
        if "sourceCueRange" in scene:
            raise ProjectValidationError(f"{label}.sourceCueRange 仅属于 timing plan；请使用 cueRange")
        cue_range = scene.get("cueRange")
        if (
            not isinstance(cue_range, list)
            or len(cue_range) != 2
            or any(isinstance(item, bool) or not isinstance(item, int) for item in cue_range)
            or cue_range[0] < 1
            or cue_range[1] < cue_range[0]
        ):
            raise ProjectValidationError(f"{label}.cueRange 必须是递增的两个正整数")
        for field in ("prompt", "coreIdea", "visualSubject"):
            value = scene.get(field)
            if not isinstance(value, str) or not value.strip():
                raise ProjectValidationError(f"{label}.{field} 必须是去空白后非空字符串")

    temporary_project_id = str(uuid.uuid4())
    generation_plan = create_generation_plan(temporary_project_id, detached)
    timing_plan = _build_source_timing_plan(
        project_id=temporary_project_id,
        source_srt_path=source_path,
        scene_specs=generation_plan["scenes"],
        render_profile=FIXED_RENDER_PROFILE,
        voiceover_mode=voiceover_mode,
    )
    validate_timing_plan_data(
        timing_plan,
        project_id=temporary_project_id,
        voiceover_mode=voiceover_mode,
        source_srt_sha256=sha256_file(source_path),
        render_profile=FIXED_RENDER_PROFILE,
        generation_scenes=generation_plan["scenes"],
    )
    for index, (scene, timing_scene) in enumerate(zip(generation_plan["scenes"], timing_plan["scenes"])):
        if scene["sceneDurationMs"] != timing_scene["sceneDurationMs"]:
            raise ProjectValidationError(
                f"scenes[{index}].sceneDurationMs 必须等于 source timing 的 "
                f"{timing_scene['sceneDurationMs']}"
            )
    return detached


def validate_preproject_generation_plan(
    candidate: Any,
    source_srt_path: str | Path,
    *,
    voiceover_mode: str = "disabled",
) -> dict[str, Any]:
    """兼容短名称的 pre-project candidate validator。"""
    return validate_pre_project_generation_plan_data(
        candidate,
        source_srt_path=source_srt_path,
        voiceover_mode=voiceover_mode,
    )


def validate_generation_plan(
    project_root: str | Path,
    plan: Mapping[str, Any] | None = None,
    *,
    project_metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    root = _resolved(Path(project_root))
    metadata = dict(project_metadata) if project_metadata is not None else _load_project_metadata(root)
    if plan is None:
        plan_path = safe_project_path(root, "planning/generation-plan.json")
        try:
            loaded = json.loads(plan_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ProjectValidationError(f"无法读取 generation plan {plan_path}: {exc}") from exc
    else:
        loaded = dict(plan)
    return validate_generation_plan_data(loaded, project_id=metadata["projectId"])


def _build_source_timing_plan(
    *,
    project_id: str,
    source_srt_path: Path,
    scene_specs: list[dict[str, Any]],
    render_profile: Mapping[str, Any],
    voiceover_mode: str,
) -> dict[str, Any]:
    if not scene_specs:
        # 保留既有“空 scenes 的有效 generation plan 骨架”。它尚无可渲染场景，
        # 因此 timing plan 只冻结输入身份，不虚构与 generation plan 脱节的分幕。
        source_hash = sha256_file(source_srt_path)
        return {
            "schemaVersion": 1,
            "projectId": project_id,
            "voiceoverMode": voiceover_mode,
            "sourceSrtSha256": source_hash,
            "renderProfileSha256": sha256_json(dict(render_profile)),
            "activeTimeline": {
                "kind": "source-srt",
                "file": "source/source.srt",
                "sha256": source_hash,
            },
            "scenes": [],
        }
    # 延迟导入可避免 project loader 与共享 SRT 模块形成导入环；解析口径只存在一份。
    try:
        from .srt_timeline import build_source_timing_plan
    except ImportError:  # pragma: no cover - direct script/module execution
        from srt_timeline import build_source_timing_plan

    try:
        return build_source_timing_plan(
            project_id=project_id,
            source_srt_path=source_srt_path,
            scene_specs=scene_specs,
            render_profile=dict(render_profile),
            voiceover_mode=voiceover_mode,
        )
    except (OSError, ValueError) as exc:
        raise ProjectValidationError(f"无法构建 source timing plan: {exc}") from exc


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _validate_relative_posix_file(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise ProjectValidationError(f"{label} 必须是非空 POSIX 项目相对路径")
    path = Path(value)
    if path.is_absolute() or _is_windows_absolute(value) or ".." in path.parts:
        raise ProjectValidationError(f"{label} 必须是安全的项目相对路径")
    if path.as_posix() != value:
        raise ProjectValidationError(f"{label} 必须使用 POSIX 路径")
    return value


def validate_timing_plan_data(
    timing_plan: Any,
    *,
    project_id: str,
    voiceover_mode: str,
    source_srt_sha256: str,
    render_profile: Mapping[str, Any],
    generation_scenes: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """校验持久化或 v1 兼容 timing plan，不把时序写回 generation plan。"""
    if not isinstance(timing_plan, dict):
        raise ProjectValidationError("timing plan 顶层必须是对象")
    if timing_plan.get("schemaVersion") != 1:
        raise ProjectValidationError("timing plan schemaVersion 必须为 1")
    if timing_plan.get("projectId") != project_id:
        raise ProjectValidationError("timing plan projectId 与 project.json 不一致")
    if timing_plan.get("voiceoverMode") != voiceover_mode:
        raise ProjectValidationError("timing plan voiceoverMode 与 project.json 不一致")
    if timing_plan.get("sourceSrtSha256") != source_srt_sha256:
        raise ProjectValidationError("timing plan sourceSrtSha256 与项目源 SRT 不一致")
    expected_profile_hash = sha256_json(dict(render_profile))
    if timing_plan.get("renderProfileSha256") != expected_profile_hash:
        raise ProjectValidationError("timing plan renderProfileSha256 与项目渲染档不一致")

    active = timing_plan.get("activeTimeline")
    if not isinstance(active, dict):
        raise ProjectValidationError("timing plan activeTimeline 必须是对象")
    kind = active.get("kind")
    if kind not in {"source-srt", "edge-tts-audio-timeline", "audio-authoritative-timeline"}:
        raise ProjectValidationError("timing plan activeTimeline.kind 无效")
    active_file = _validate_relative_posix_file(
        active.get("file"), label="timing plan activeTimeline.file"
    )
    if not _is_sha256(active.get("sha256")):
        raise ProjectValidationError("timing plan activeTimeline.sha256 无效")
    if kind == "source-srt":
        if active_file != "source/source.srt" or active["sha256"] != source_srt_sha256:
            raise ProjectValidationError("source timing plan 必须绑定 current source/source.srt")
    elif voiceover_mode not in AUDIO_VOICEOVER_MODES or active_file != "audio/timeline.json":
        raise ProjectValidationError("音频权威 timing plan 仅允许 Edge 模式绑定 audio/timeline.json")

    scenes = timing_plan.get("scenes")
    if not isinstance(scenes, list):
        raise ProjectValidationError("timing plan scenes 必须是数组")
    expected_start_ms = 0
    expected_start_frame = 0
    fps = render_profile["fps"]
    scene_ids: list[str] = []
    for index, scene in enumerate(scenes):
        label = f"timing plan scenes[{index}]"
        if not isinstance(scene, dict):
            raise ProjectValidationError(f"{label} 必须是对象")
        scene_id = scene.get("sceneId")
        if not isinstance(scene_id, str) or not scene_id or scene_id in scene_ids:
            raise ProjectValidationError(f"{label}.sceneId 必须非空且唯一")
        scene_ids.append(scene_id)
        cue_range = scene.get("sourceCueRange")
        if (
            not isinstance(cue_range, list)
            or len(cue_range) != 2
            or any(isinstance(item, bool) or not isinstance(item, int) or item < 1 for item in cue_range)
            or cue_range[0] > cue_range[1]
        ):
            raise ProjectValidationError(f"{label}.sourceCueRange 必须是递增的两个正整数")
        start_ms = scene.get("startMs")
        end_ms = scene.get("endMs")
        duration_ms = scene.get("sceneDurationMs")
        for field, value in (("startMs", start_ms), ("endMs", end_ms), ("sceneDurationMs", duration_ms)):
            if isinstance(value, bool) or not isinstance(value, int):
                raise ProjectValidationError(f"{label}.{field} 必须是整数")
        if start_ms != expected_start_ms or end_ms <= start_ms or duration_ms != end_ms - start_ms:
            raise ProjectValidationError(f"{label} 必须是连续、正时长的全局区间")
        expected_end_frame = (end_ms * fps + 999) // 1000
        start_frame = scene.get("startFrame")
        end_frame = scene.get("endFrameExclusive")
        frame_count = scene.get("frameCount")
        if (
            start_frame != expected_start_frame
            or end_frame != expected_end_frame
            or frame_count != expected_end_frame - expected_start_frame
        ):
            raise ProjectValidationError(f"{label} 未遵循 cumulative-ceil-v1 累计帧边界")
        expected_start_ms = end_ms
        expected_start_frame = expected_end_frame

    if generation_scenes is not None:
        expected_ids = [scene.get("sceneId") for scene in generation_scenes]
        if scene_ids != expected_ids:
            raise ProjectValidationError("timing plan scenes 必须与 generation plan 的语义场景顺序一致")
    return timing_plan


def _validate_uuid4(value: Any) -> str:
    if not isinstance(value, str):
        raise ProjectValidationError("projectId 必须是 UUID v4 字符串")
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as exc:
        raise ProjectValidationError("projectId 不是有效 UUID") from exc
    if parsed.version != 4 or str(parsed) != value.lower():
        raise ProjectValidationError("projectId 必须是规范格式的 UUID v4")
    return value


def validate_project_metadata_data(root: Path, metadata: Any) -> dict[str, Any]:
    if not isinstance(metadata, dict) or metadata.get("schemaVersion") not in SUPPORTED_PROJECT_SCHEMA_VERSIONS:
        raise ProjectValidationError("project.json schemaVersion 必须为 1 或 2")
    schema_version = metadata["schemaVersion"]
    if "agentApprovalEnabled" in metadata and not isinstance(
        metadata["agentApprovalEnabled"], bool
    ):
        raise ProjectValidationError("project.json agentApprovalEnabled 必须是布尔值")
    image_generation_mode = metadata.get("imageGenerationMode", "provider")
    if (
        not isinstance(image_generation_mode, str)
        or image_generation_mode not in IMAGE_GENERATION_MODES
    ):
        raise ProjectValidationError(
            "project.json imageGenerationMode 只允许 provider 或 gpt-login"
        )
    initial_approval = metadata.get("initialApproval")
    if initial_approval is not None:
        if schema_version != 2 or not isinstance(initial_approval, dict):
            raise ProjectValidationError("project.json initialApproval 只允许 schema v2 对象")
        status = initial_approval.get("status")
        if status == INITIAL_APPROVAL_PENDING:
            if set(initial_approval) != {"status"}:
                raise ProjectValidationError(
                    "pending initialApproval 只能包含 status"
                )
            if any(
                field in metadata
                for field in (
                    "backgroundMusic",
                    "agentApprovalEnabled",
                    "imageGenerationMode",
                )
            ):
                raise ProjectValidationError(
                    "pending 初始批准前不得冻结 BGM、代理批准或生图方式"
                )
        elif status == INITIAL_APPROVAL_APPROVED:
            expected_fields = {
                "status",
                "contentIdentitySha256",
                "sampleIdentityHash",
                "approvalBasis",
                "approvedAt",
            }
            if set(initial_approval) != expected_fields:
                raise ProjectValidationError("approved initialApproval 字段集合无效")
            if not _is_sha256(initial_approval.get("contentIdentitySha256")):
                raise ProjectValidationError(
                    "initialApproval.contentIdentitySha256 无效"
                )
            sample_identity = initial_approval.get("sampleIdentityHash")
            if sample_identity is not None and not _is_sha256(sample_identity):
                raise ProjectValidationError("initialApproval.sampleIdentityHash 无效")
            expected_basis = (
                "user_joint_silent_plan"
                if sample_identity is None
                else "user_joint_content_and_sample"
            )
            if initial_approval.get("approvalBasis") != expected_basis:
                raise ProjectValidationError("initialApproval.approvalBasis 无效")
            approved_at = initial_approval.get("approvedAt")
            if not isinstance(approved_at, str):
                raise ProjectValidationError("initialApproval.approvedAt 无效")
            try:
                parsed_approved_at = datetime.fromisoformat(approved_at)
            except ValueError as exc:
                raise ProjectValidationError(
                    "initialApproval.approvedAt 不是 ISO 8601 时间"
                ) from exc
            if parsed_approved_at.tzinfo is None:
                raise ProjectValidationError(
                    "initialApproval.approvedAt 必须包含时区"
                )
        else:
            raise ProjectValidationError(
                "initialApproval.status 只允许 pending_initial_approval 或 approved"
            )
    _validate_uuid4(metadata.get("projectId"))
    project_name = metadata.get("projectName")
    if not isinstance(project_name, str) or sanitize_project_name(project_name) != project_name:
        raise ProjectValidationError("project.json 中的 projectName 无效")
    if root.name != project_name:
        raise ProjectValidationError("项目目录名与 project.json projectName 不一致")
    created_at = metadata.get("createdAt")
    if not isinstance(created_at, str):
        raise ProjectValidationError("project.json createdAt 无效")
    try:
        parsed_created_at = datetime.fromisoformat(created_at)
    except ValueError as exc:
        raise ProjectValidationError("project.json createdAt 不是 ISO 8601 时间") from exc
    if parsed_created_at.tzinfo is None:
        raise ProjectValidationError("project.json createdAt 必须包含时区")
    source = metadata.get("source")
    if not isinstance(source, dict) or source.get("file") != "source/source.srt":
        raise ProjectValidationError("project.json source.file 必须为 source/source.srt")
    source_hash = source.get("sha256")
    if not isinstance(source_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", source_hash):
        raise ProjectValidationError("project.json source.sha256 无效")
    paths = metadata.get("paths")
    if not isinstance(paths, dict):
        raise ProjectValidationError("project.json paths 无效")
    expected_paths = PROJECT_PATHS_V1 if schema_version == 1 else PROJECT_PATHS_V2
    if set(paths) != set(expected_paths):
        raise ProjectValidationError(f"project.json schema v{schema_version} paths 字段集合无效")
    for key, required_path in expected_paths.items():
        if paths.get(key) != required_path:
            raise ProjectValidationError(f"project.json paths.{key} 必须为 {required_path}")
        safe_project_path(root, paths[key])
    if schema_version == 2:
        if metadata.get("voiceoverMode") not in VOICEOVER_MODES:
            raise ProjectValidationError("project.json voiceoverMode 只允许 disabled、edge-tts、minimax 或 doubao")
        if metadata.get("renderProfile") != FIXED_RENDER_PROFILE:
            raise ProjectValidationError("project.json renderProfile 必须严格为 whiteboard-render-v2")
        background_music = metadata.get("backgroundMusic")
        if background_music is not None and (
            not isinstance(background_music, dict)
            or set(background_music) != {"enabled"}
            or not isinstance(background_music.get("enabled"), bool)
        ):
            raise ProjectValidationError("project.json backgroundMusic 必须严格为 {enabled: boolean}")
        if (
            isinstance(background_music, dict)
            and background_music.get("enabled") is True
            and metadata.get("voiceoverMode") not in AUDIO_VOICEOVER_MODES
        ):
            raise ProjectValidationError("当前 BGM 功能只允许用于旁白项目")
    content_source = metadata.get("contentSource")
    if content_source is not None:
        if schema_version != 2 or metadata.get("voiceoverMode") not in AUDIO_VOICEOVER_MODES:
            raise ProjectValidationError("contentSource 仅允许 schema v2 的音频旁白项目")
        if not isinstance(content_source, dict) or set(content_source) != CONTENT_SOURCE_FIELDS:
            raise ProjectValidationError("project.json contentSource 字段集合无效")
        if content_source.get("contractVersion") != "whiteboard-source-package-v1":
            raise ProjectValidationError("project.json contentSource.contractVersion 无效")
        if content_source.get("inputFile") != "source/input.json":
            raise ProjectValidationError("project.json contentSource.inputFile 必须为 source/input.json")
        if content_source.get("manifestFile") != "source/source-manifest.json":
            raise ProjectValidationError(
                "project.json contentSource.manifestFile 必须为 source/source-manifest.json"
            )
        for field in (
            "inputSha256",
            "inputIdentitySha256",
            "manifestSha256",
            "generationPlanSha256",
            "sourcePackageIdentitySha256",
        ):
            if not _is_sha256(content_source.get(field)):
                raise ProjectValidationError(f"project.json contentSource.{field} 无效")
    source_path = safe_project_path(root, source["file"])
    if not source_path.is_file():
        raise ProjectValidationError(f"项目 SRT 副本不存在: {source_path}")
    if sha256_file(source_path) != source_hash:
        raise ProjectValidationError("项目 SRT 副本 SHA-256 与 project.json 不一致")
    if content_source is not None:
        # 延迟导入避免 content_source 复用本模块 validator 时形成模块加载环。
        from content_source import ContentSourceError, validate_project_source_binding

        try:
            validate_project_source_binding(
                root,
                content_source,
                project_id=metadata["projectId"],
                source_srt_sha256=source_hash,
            )
        except ContentSourceError as exc:
            raise ProjectValidationError(str(exc)) from exc
    return metadata


def _load_project_metadata(root: Path) -> dict[str, Any]:
    path = safe_project_path(root, "project.json")
    try:
        metadata = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ProjectValidationError(f"无法读取 project.json {path}: {exc}") from exc
    return validate_project_metadata_data(root, metadata)


def _load_persisted_timing_plan(
    root: Path,
    *,
    metadata: Mapping[str, Any],
    generation_plan: Mapping[str, Any],
    allow_pending_audio_timeline: bool = False,
) -> tuple[dict[str, Any], bool]:
    path = safe_project_path(root, "planning/timing-plan.json")
    try:
        timing_plan = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ProjectValidationError(f"无法读取 timing plan {path}: {exc}") from exc
    active = timing_plan.get("activeTimeline") if isinstance(timing_plan, dict) else None
    validate_timing_plan_data(
        timing_plan,
        project_id=metadata["projectId"],
        voiceover_mode=metadata["voiceoverMode"],
        source_srt_sha256=metadata["source"]["sha256"],
        render_profile=metadata["renderProfile"],
        generation_scenes=generation_plan["scenes"],
    )
    pending_audio_timeline = False
    if isinstance(active, dict):
        if active.get("kind") == "source-srt":
            expected = _build_source_timing_plan(
                project_id=metadata["projectId"],
                source_srt_path=safe_project_path(root, metadata["source"]["file"]),
                scene_specs=generation_plan["scenes"],
                render_profile=metadata["renderProfile"],
                voiceover_mode=metadata["voiceoverMode"],
            )
            if timing_plan != expected:
                raise ProjectValidationError("source timing plan 与 current SRT/语义场景的确定性结果不一致")
        elif active.get("kind") in {"edge-tts-audio-timeline", "audio-authoritative-timeline"}:
            active_path = safe_project_path(root, active["file"])
            if not active_path.is_file():
                raise ProjectValidationError("timing plan 绑定的 audio/timeline.json 缺失或 SHA-256 不一致")
            if sha256_file(active_path) != active["sha256"]:
                if not allow_pending_audio_timeline:
                    raise ProjectValidationError(
                        "timing plan 绑定的 audio/timeline.json 缺失或 SHA-256 不一致"
                    )
                _validate_pending_audio_timeline_binding(
                    root,
                    metadata=metadata,
                    timeline_path=active_path,
                )
                # publish-alignment 会先发布待审阅的新 audio timeline，随后才由
                # approve-full 把正式 timing plan 切换到它。只有旁白状态/验证/批准
                # 命令显式请求本模式时，才允许读取这一种受限的待批准状态。
                pending_audio_timeline = True
    return timing_plan, pending_audio_timeline


def _validate_pending_audio_timeline_binding(
    root: Path,
    *,
    metadata: Mapping[str, Any],
    timeline_path: Path,
) -> None:
    """确认 SHA 漂移来自正式 publish-alignment，而不是任意 timeline 篡改。"""

    manifest_path = safe_project_path(root, "manifests/voice-manifest.json")
    narration_path = safe_project_path(root, "audio/narration.srt")
    audio_path = safe_project_path(root, "audio/narration.wav")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        timeline = json.loads(timeline_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ProjectValidationError(f"pending audio timeline 证据不可读: {exc}") from exc
    timeline_sha = sha256_file(timeline_path)
    timeline_ref = manifest.get("timeline") if isinstance(manifest, dict) else None
    narration_ref = manifest.get("narrationSrt") if isinstance(manifest, dict) else None
    composite_ref = manifest.get("composite") if isinstance(manifest, dict) else None
    approval = manifest.get("fullApproval") if isinstance(manifest, dict) else None
    if (
        manifest.get("projectId") != metadata["projectId"]
        or not isinstance(timeline_ref, dict)
        or timeline_ref.get("status") != "validated"
        or timeline_ref.get("relativePath") != "audio/timeline.json"
        or timeline_ref.get("sha256") != timeline_sha
        or not isinstance(narration_ref, dict)
        or narration_ref.get("status") != "validated"
        or narration_ref.get("relativePath") != "audio/narration.srt"
        or not isinstance(composite_ref, dict)
        or composite_ref.get("status") != "validated"
        or composite_ref.get("relativePath") != "audio/narration.wav"
        or not isinstance(approval, dict)
        or approval.get("approved") is not False
        or approval.get("identityHash") is not None
    ):
        raise ProjectValidationError("pending audio timeline 缺少 current publish-alignment manifest 绑定")
    if not narration_path.is_file() or not audio_path.is_file():
        raise ProjectValidationError("pending audio timeline 缺少 current narration SRT 或 WAV")
    if (
        narration_ref.get("sha256") != sha256_file(narration_path)
        or composite_ref.get("sha256") != sha256_file(audio_path)
    ):
        raise ProjectValidationError("pending audio timeline 的 narration SRT/WAV SHA-256 已 stale")
    if (
        not isinstance(timeline, dict)
        or timeline.get("projectId") != metadata["projectId"]
        or timeline.get("sourceSrt", {}).get("sha256") != metadata["source"]["sha256"]
        or timeline.get("audio", {}).get("sha256") != composite_ref.get("sha256")
        or timeline.get("narrationSrt", {}).get("file") != "audio/narration.srt"
        or timeline.get("narrationSrt", {}).get("sha256") != narration_ref.get("sha256")
    ):
        raise ProjectValidationError("pending audio timeline 内容未绑定 current 项目/WAV/SRT")


def load_project(
    project_root: str | Path,
    *,
    allow_pending_audio_timeline: bool = False,
    allow_pending_initial_approval: bool = False,
) -> Project:
    root = _resolved(Path(project_root))
    if not root.is_dir():
        raise ProjectValidationError(f"项目目录不存在: {root}")
    metadata = _load_project_metadata(root)
    plan = validate_generation_plan(root, project_metadata=metadata)
    if metadata["schemaVersion"] == 1:
        # v1 兼容视图只在内存中确定性构造；忽略可能由失败升级留下的 timing plan，绝不改写项目。
        timing_plan = _build_source_timing_plan(
            project_id=metadata["projectId"],
            source_srt_path=safe_project_path(root, metadata["source"]["file"]),
            scene_specs=plan["scenes"],
            render_profile=FIXED_RENDER_PROFILE,
            voiceover_mode="disabled",
        )
        validate_timing_plan_data(
            timing_plan,
            project_id=metadata["projectId"],
            voiceover_mode="disabled",
            source_srt_sha256=metadata["source"]["sha256"],
            render_profile=FIXED_RENDER_PROFILE,
            generation_scenes=plan["scenes"],
        )
        pending_audio_timeline = False
    else:
        timing_plan, pending_audio_timeline = _load_persisted_timing_plan(
            root,
            metadata=metadata,
            generation_plan=plan,
            allow_pending_audio_timeline=allow_pending_audio_timeline,
        )
    project = Project(
        root=root,
        metadata=metadata,
        plan=plan,
        timing_plan=timing_plan,
        pending_audio_timeline=pending_audio_timeline,
    )
    initial_approval = metadata.get("initialApproval")
    if (
        isinstance(initial_approval, dict)
        and initial_approval.get("status") == INITIAL_APPROVAL_APPROVED
    ):
        if (
            initial_approval.get("contentIdentitySha256")
            != project.current_content_identity_sha256
        ):
            raise ProjectValidationError(
                "initialApproval 绑定的 content identity 已 stale"
            )
        sample_identity = initial_approval.get("sampleIdentityHash")
        if project.voiceover_mode in AUDIO_VOICEOVER_MODES:
            manifest_path = safe_project_path(root, "manifests/voice-manifest.json")
            try:
                voice_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise ProjectValidationError(
                    "已初始批准的旁白项目缺少可读 voice manifest"
                ) from exc
            sample = voice_manifest.get("sample") if isinstance(voice_manifest, dict) else None
            approval = sample.get("approval") if isinstance(sample, dict) else None
            if (
                not _is_sha256(sample_identity)
                or sample.get("identityHash") != sample_identity
                or not isinstance(approval, dict)
                or approval.get("approved") is not True
                or approval.get("identityHash") != sample_identity
                or approval.get("approvalBasis") != "user_joint_initial_approval"
            ):
                raise ProjectValidationError(
                    "initialApproval 未绑定 current 已联合批准样音"
                )
        elif sample_identity is not None:
            raise ProjectValidationError("静音项目 initialApproval 不得绑定样音")
    if project.pending_initial_approval and not allow_pending_initial_approval:
        raise ProjectValidationError(
            "项目仍为 pending_initial_approval；当前操作不允许越过初始联合批准"
        )
    return project


def upgrade_project(
    project_root: str | Path,
    *,
    to_schema: int,
    voiceover_mode: str,
) -> Project:
    """显式升级 v1；timing plan 先发布，project.json 最后作为提交点。"""
    if to_schema != 2:
        raise ProjectValidationError("首版只支持显式升级到 schema 2")
    if voiceover_mode not in VOICEOVER_MODES:
        raise ProjectValidationError("voiceoverMode 只允许 disabled、edge-tts、minimax 或 doubao")
    root = _resolved(Path(project_root))
    project = load_project(root)
    if project.schema_version == 2:
        if project.voiceover_mode != voiceover_mode:
            raise ProjectValidationError("项目已是 schema v2；upgrade 不用于切换 voiceoverMode")
        return project

    generation_plan_hash_before = sha256_file(project.plan_path)
    source_path = safe_project_path(root, project.metadata["source"]["file"])
    timing_plan = _build_source_timing_plan(
        project_id=project.project_id,
        source_srt_path=source_path,
        scene_specs=project.plan["scenes"],
        render_profile=FIXED_RENDER_PROFILE,
        voiceover_mode=voiceover_mode,
    )
    validate_timing_plan_data(
        timing_plan,
        project_id=project.project_id,
        voiceover_mode=voiceover_mode,
        source_srt_sha256=project.metadata["source"]["sha256"],
        render_profile=FIXED_RENDER_PROFILE,
        generation_scenes=project.plan["scenes"],
    )
    metadata = dict(project.metadata)
    metadata.update(
        {
            "schemaVersion": 2,
            "voiceoverMode": voiceover_mode,
            "agentApprovalEnabled": False,
            "renderProfile": dict(FIXED_RENDER_PROFILE),
            "paths": dict(PROJECT_PATHS_V2),
        }
    )
    validate_project_metadata_data(root, metadata)

    run_id = f"upgrade-{uuid.uuid4().hex}"
    run_dir = safe_project_path(root, Path(PROJECT_PATHS_V1["work"]) / run_id)
    run_dir.mkdir(parents=True, exist_ok=False)
    timing_candidate = run_dir / "timing-plan.json.candidate"
    project_candidate = run_dir / "project.json.candidate"
    _write_json(timing_candidate, timing_plan)
    _write_json(project_candidate, metadata)

    # 在提交前重新读取候选，避免序列化/落盘异常进入正式路径。
    persisted_timing_candidate = json.loads(timing_candidate.read_text(encoding="utf-8"))
    persisted_project_candidate = json.loads(project_candidate.read_text(encoding="utf-8"))
    validate_timing_plan_data(
        persisted_timing_candidate,
        project_id=project.project_id,
        voiceover_mode=voiceover_mode,
        source_srt_sha256=project.metadata["source"]["sha256"],
        render_profile=FIXED_RENDER_PROFILE,
        generation_scenes=project.plan["scenes"],
    )
    validate_project_metadata_data(root, persisted_project_candidate)

    for relative in (PROJECT_PATHS_V2["audio"], PROJECT_PATHS_V2["subtitles"]):
        safe_project_path(root, relative).mkdir(parents=True, exist_ok=True)
    timing_target = safe_project_path(root, "planning/timing-plan.json")
    project_target = safe_project_path(root, "project.json")
    os.replace(timing_candidate, timing_target)
    # 唯一提交点：这里失败时原 project.json 仍是 v1，loader 会忽略已发布 timing plan。
    os.replace(project_candidate, project_target)

    if sha256_file(project.plan_path) != generation_plan_hash_before:
        raise ProjectValidationError("升级不得改写 generation plan")
    upgraded = load_project(root)
    try:
        run_dir.rmdir()
    except OSError:
        pass
    return upgraded


class ProjectWorkspace:
    def __init__(self, config: WorkspaceConfig):
        self.config = config

    @classmethod
    def from_config(cls, config_path: str | Path | None = None) -> "ProjectWorkspace":
        return cls(load_workspace_config(config_path))

    def _assert_project_location(self, project_root: str | Path) -> Path:
        root = _resolved(Path(project_root))
        projects = _resolved(self.config.projects_dir)
        try:
            relative = root.relative_to(projects)
        except ValueError as exc:
            raise ProjectValidationError(f"项目目录不在工作区 projects 内: {root}") from exc
        if len(relative.parts) != 1:
            raise ProjectValidationError("项目目录必须是 projects 下的直接子目录")
        return root

    def create_project(
        self,
        name: str,
        source_srt: str | Path,
        *,
        confirmed_plan: Mapping[str, Any] | None = None,
        voiceover_mode: str = "disabled",
        background_music_enabled: bool | None = None,
        agent_approval_enabled: bool | None = None,
        image_generation_mode: str | None = None,
        pending_initial_approval: bool = False,
        source_input: str | Path | None = None,
        source_manifest: str | Path | None = None,
        source_plan: str | Path | None = None,
    ) -> Project:
        if voiceover_mode not in VOICEOVER_MODES:
            raise ProjectValidationError("voiceoverMode 只允许 disabled、edge-tts、minimax 或 doubao")
        if not isinstance(pending_initial_approval, bool):
            raise ProjectValidationError("pending_initial_approval 必须是布尔值")
        if pending_initial_approval and any(
            value is not None
            for value in (
                background_music_enabled,
                agent_approval_enabled,
                image_generation_mode,
            )
        ):
            raise ProjectValidationError(
                "pending 预项目不得提前冻结 BGM、代理批准或生图方式"
            )
        if not pending_initial_approval:
            background_music_enabled = (
                False if background_music_enabled is None else background_music_enabled
            )
            agent_approval_enabled = (
                False if agent_approval_enabled is None else agent_approval_enabled
            )
            image_generation_mode = image_generation_mode or "provider"
            if not isinstance(background_music_enabled, bool):
                raise ProjectValidationError("backgroundMusic.enabled 必须是布尔值")
            if not isinstance(agent_approval_enabled, bool):
                raise ProjectValidationError("agentApprovalEnabled 必须是布尔值")
            if (
                not isinstance(image_generation_mode, str)
                or image_generation_mode not in IMAGE_GENERATION_MODES
            ):
                raise ProjectValidationError(
                    "imageGenerationMode 只允许 provider 或 gpt-login"
                )
        if background_music_enabled and voiceover_mode not in AUDIO_VOICEOVER_MODES:
            raise ProjectValidationError("当前 BGM 功能只允许用于旁白项目")
        project_name = sanitize_project_name(name)
        source_path = _resolved(Path(source_srt))
        if not source_path.is_file():
            raise ProjectValidationError(f"原始 SRT 不存在: {source_path}")
        evidence_args = (source_input, source_manifest, source_plan)
        has_content_source = any(item is not None for item in evidence_args)
        source_package = None
        if has_content_source:
            if any(item is None for item in evidence_args):
                raise ProjectValidationError(
                    "content source 创建必须同时提供 source_input、source_manifest 与 source_plan"
                )
            if confirmed_plan is None:
                raise ProjectValidationError("content source 创建必须提供已确认 generation plan")
            if voiceover_mode not in AUDIO_VOICEOVER_MODES:
                raise ProjectValidationError("topic/text content source 只允许 edge-tts、minimax 或 doubao")
            from content_source import ContentSourceError, validate_source_package

            try:
                source_package = validate_source_package(
                    source_input,
                    source_manifest,
                    source_path,
                    source_plan,
                )
            except ContentSourceError as exc:
                raise ProjectValidationError(str(exc)) from exc
            if dict(confirmed_plan) != source_package.generation_plan:
                raise ProjectValidationError("--plan 与 source manifest 绑定的 generation plan 不一致")
        project_root = self._assert_project_location(self.config.projects_dir / project_name)
        if project_root.exists():
            raise ProjectValidationError(f"项目已存在，若要续接请显式使用 --resume: {project_root}")
        project_id = str(uuid.uuid4())
        plan = create_generation_plan(project_id, confirmed_plan)
        source_hash = sha256_file(source_path)
        metadata: dict[str, Any] = {
            "schemaVersion": PROJECT_SCHEMA_VERSION,
            "projectId": project_id,
            "projectName": project_name,
            "createdAt": datetime.now().astimezone().isoformat(timespec="seconds"),
            "voiceoverMode": voiceover_mode,
            "renderProfile": dict(FIXED_RENDER_PROFILE),
            "source": {"file": "source/source.srt", "sha256": source_hash},
            "paths": dict(PROJECT_PATHS_V2),
        }
        if pending_initial_approval:
            metadata["initialApproval"] = {"status": INITIAL_APPROVAL_PENDING}
        else:
            metadata.update(
                {
                    "backgroundMusic": {"enabled": background_music_enabled},
                    "agentApprovalEnabled": agent_approval_enabled,
                    "imageGenerationMode": image_generation_mode,
                }
            )
        if source_package is not None:
            metadata["contentSource"] = {
                "contractVersion": source_package.manifest["contractVersion"],
                "inputFile": "source/input.json",
                "inputSha256": sha256_file(source_input),
                "inputIdentitySha256": source_package.manifest[
                    "contentDraftIdentitySha256"
                ],
                "manifestFile": "source/source-manifest.json",
                "manifestSha256": sha256_file(source_manifest),
                "generationPlanSha256": "0" * 64,
                "sourcePackageIdentitySha256": source_package.manifest[
                    "sourcePackageIdentitySha256"
                ],
            }
        try:
            project_root.mkdir(parents=True, exist_ok=False)
            for relative in ["source", *PROJECT_PATHS_V2.values()]:
                safe_project_path(project_root, relative).mkdir(parents=True, exist_ok=True)
            copied_srt = safe_project_path(project_root, "source/source.srt")
            shutil.copyfile(source_path, copied_srt)
            if sha256_file(copied_srt) != source_hash:
                raise ProjectValidationError("复制后的 SRT SHA-256 不一致")
            if source_package is not None:
                copied_input = safe_project_path(project_root, "source/input.json")
                copied_manifest = safe_project_path(project_root, "source/source-manifest.json")
                shutil.copyfile(source_input, copied_input)
                shutil.copyfile(source_manifest, copied_manifest)
                if sha256_file(copied_input) != metadata["contentSource"]["inputSha256"]:
                    raise ProjectValidationError("复制后的 input.json SHA-256 不一致")
                if sha256_file(copied_manifest) != metadata["contentSource"]["manifestSha256"]:
                    raise ProjectValidationError("复制后的 source manifest SHA-256 不一致")
            timing_plan = _build_source_timing_plan(
                project_id=project_id,
                source_srt_path=copied_srt,
                scene_specs=plan["scenes"],
                render_profile=FIXED_RENDER_PROFILE,
                voiceover_mode=voiceover_mode,
            )
            validate_timing_plan_data(
                timing_plan,
                project_id=project_id,
                voiceover_mode=voiceover_mode,
                source_srt_sha256=source_hash,
                render_profile=FIXED_RENDER_PROFILE,
                generation_scenes=plan["scenes"],
            )
            _write_json(safe_project_path(project_root, "planning/generation-plan.json"), plan)
            if source_package is not None:
                metadata["contentSource"]["generationPlanSha256"] = sha256_file(
                    safe_project_path(project_root, "planning/generation-plan.json")
                )
            _write_json(safe_project_path(project_root, "planning/timing-plan.json"), timing_plan)
            # project.json 是新项目的提交点；生成/时序快照先落盘。
            _write_json(safe_project_path(project_root, "project.json"), metadata)
            return load_project(
                project_root,
                allow_pending_initial_approval=pending_initial_approval,
            )
        except Exception:
            # 只回滚本次尚未成功创建的、已解析且位于 projects 下的唯一目录。
            if project_root.exists():
                self._assert_project_location(project_root)
                shutil.rmtree(project_root)
            raise

    def load_project(
        self,
        project_root: str | Path,
        *,
        allow_pending_audio_timeline: bool = False,
        allow_pending_initial_approval: bool = False,
    ) -> Project:
        """从当前工作区加载项目，并先约束其必须是 projects 的直接子目录。"""
        return load_project(
            self._assert_project_location(project_root),
            allow_pending_audio_timeline=allow_pending_audio_timeline,
            allow_pending_initial_approval=allow_pending_initial_approval,
        )

    def upgrade_project(
        self,
        project_root: str | Path,
        *,
        to_schema: int,
        voiceover_mode: str,
    ) -> Project:
        return upgrade_project(
            self._assert_project_location(project_root),
            to_schema=to_schema,
            voiceover_mode=voiceover_mode,
        )

    def resume_project(self, project_root: str | Path, source_srt: str | Path) -> Project:
        root = self._assert_project_location(project_root)
        project = self.load_project(root)
        source_path = _resolved(Path(source_srt))
        if not source_path.is_file():
            raise ProjectValidationError(f"原始 SRT 不存在: {source_path}")
        supplied_hash = sha256_file(source_path)
        if supplied_hash != project.metadata["source"]["sha256"]:
            raise ProjectValidationError("--resume 的原始 SRT SHA-256 与项目不一致")
        # load_project 已校验 UUID v4、项目副本哈希及 plan 中的 projectId。
        return project
