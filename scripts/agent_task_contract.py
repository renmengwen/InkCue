"""Agent task/result 合同与宿主协作调度判定。

本模块不读取 workspace 配置、不派发真实 subagent，也不写正式项目文件。
调用方必须把已经由权威 workspace loader 验证的 ``workspace_root`` 和
``scope_root`` 作为可信上下文传入。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Callable, Iterable, Literal, Mapping, Sequence


TASK_CONTRACT_VERSION = "whiteboard-agent-task-v1"
RESULT_CONTRACT_VERSION = "whiteboard-agent-result-v1"
ROLE_CONTRACT_VERSION = "whiteboard-subagent-orchestration-v1"

TASK_KINDS = frozenset(
    {
        "contentDrafting",
        "storyboardPlanning",
        "visualReview",
        "annotationDrafting",
    }
)
SCOPE_KINDS = frozenset({"draft", "project"})
RESULT_STATUSES = frozenset({"completed", "failed", "cancelled"})
ATTEMPT_STATUSES = frozenset(
    {"completed", "failed", "cancelled", "stale", "blocked"}
)
RETRYABLE_ATTEMPT_STATUSES = frozenset({"failed", "cancelled", "stale"})
CAPABILITY_ALLOWLIST = frozenset(
    {"readFiles", "viewImage", "writeCandidateJson"}
)
ROLE_REQUIRED_CAPABILITIES: Mapping[str, frozenset[str]] = {
    "contentDrafting": frozenset({"readFiles", "writeCandidateJson"}),
    "storyboardPlanning": frozenset({"readFiles", "writeCandidateJson"}),
    "visualReview": frozenset({"readFiles", "viewImage", "writeCandidateJson"}),
    "annotationDrafting": frozenset(
        {"readFiles", "viewImage", "writeCandidateJson"}
    ),
}
ROLE_SCOPE: Mapping[str, str] = {
    "contentDrafting": "draft",
    "storyboardPlanning": "draft",
    "visualReview": "project",
    "annotationDrafting": "project",
}
ROLE_OUTPUT_BASENAMES: Mapping[str, frozenset[str]] = {
    "contentDrafting": frozenset(
        {"candidate.content-draft.json", "result.json", "agent.log"}
    ),
    "storyboardPlanning": frozenset(
        {"candidate.generation-plan.json", "result.json", "agent.log"}
    ),
    "visualReview": frozenset({"findings.json", "result.json", "agent.log"}),
    "annotationDrafting": frozenset(
        {"candidate.annotation.json", "result.json", "agent.log"}
    ),
}
ROLE_REQUIRED_OUTPUT_BASENAME: Mapping[str, str | None] = {
    "contentDrafting": "candidate.content-draft.json",
    "storyboardPlanning": "candidate.generation-plan.json",
    "visualReview": None,
    "annotationDrafting": "candidate.annotation.json",
}
CURRENT_BINDING_ALLOWLIST = frozenset(
    {
        "generationPlanSha256",
        "timingPlanSha256",
        "renderProfileSha256",
        "activeTimelineSha256",
        "audioSha256",
        "fullApprovalIdentityHash",
        "imageManifestSha256",
        "sourceSrtSha256",
        "parsedSrtSha256",
        "contentInputSha256",
        "baseContentDraftIdentitySha256",
        "revisionRequestSha256",
        "coverManifestSha256",
    }
)
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class AgentContractError(ValueError):
    """带稳定类别的 task/result 合同错误。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class StaleAgentTaskError(AgentContractError):
    def __init__(self, message: str) -> None:
        super().__init__("stale", message)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _require_sha(value: Any, field: str) -> str:
    if not isinstance(value, str) or _SHA_RE.fullmatch(value) is None:
        raise AgentContractError("schema", f"{field} 必须是小写 SHA-256")
    return value


def _require_positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise AgentContractError("schema", f"{field} 必须是正整数")
    return value


def _require_safe_id(value: Any, field: str) -> str:
    if (
        not isinstance(value, str)
        or value in {".", ".."}
        or _SAFE_ID_RE.fullmatch(value) is None
    ):
        raise AgentContractError("schema", f"{field} 不是安全标识符")
    return value


def _require_exact_keys(
    value: Any,
    *,
    required: set[str],
    optional: set[str] | None = None,
    field: str,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise AgentContractError("schema", f"{field} 必须是对象")
    optional = optional or set()
    unknown = set(value) - required - optional
    missing = required - set(value)
    if unknown:
        raise AgentContractError(
            "schema", f"{field} 包含未知字段: {sorted(unknown)}"
        )
    if missing:
        raise AgentContractError(
            "schema", f"{field} 缺少字段: {sorted(missing)}"
        )
    return value


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise AgentContractError("schema", f"JSON 包含重复字段: {key}")
        result[key] = value
    return result


def _read_json(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
        value = json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    except AgentContractError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AgentContractError("json", f"无法读取严格 JSON: {path.name}") from exc
    if not isinstance(value, dict):
        raise AgentContractError("schema", f"{path.name} 顶层必须是对象")
    return value


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _assert_no_symlink(path: Path, root: Path) -> None:
    root_absolute = root.absolute()
    path_absolute = path.absolute()
    if not _is_relative_to(path_absolute, root_absolute):
        raise AgentContractError("path_escape", "路径位于可信根之外")
    current = root_absolute
    if current.is_symlink():
        raise AgentContractError("symlink", "可信根不能是符号链接")
    relative = path_absolute.relative_to(root_absolute)
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise AgentContractError("symlink", "路径包含符号链接")


def _parse_posix_relative(value: Any, field: str) -> tuple[str, ...]:
    if not isinstance(value, str) or not value:
        raise AgentContractError("path", f"{field} 必须是非空相对 POSIX 路径")
    if "\\" in value:
        raise AgentContractError("path", f"{field} 必须使用 POSIX 分隔符")
    windows = PureWindowsPath(value)
    posix = PurePosixPath(value)
    raw_parts = value.split("/")
    if (
        posix.is_absolute()
        or windows.is_absolute()
        or bool(windows.drive)
        or any(part in {"", ".", ".."} for part in raw_parts)
    ):
        raise AgentContractError("path_escape", f"{field} 不是安全相对路径")
    return tuple(raw_parts)


@dataclass(frozen=True)
class TrustedTaskContext:
    """coordinator 注入的可信 scope/run/task/attempt 上下文。"""

    workspace_root: Path
    scope_root: Path
    scope_kind: Literal["draft", "project"]
    run_id: str
    task_id: str
    attempt: int

    def __post_init__(self) -> None:
        if self.scope_kind not in SCOPE_KINDS:
            raise AgentContractError("scope", "不支持的可信 scopeKind")
        _require_safe_id(self.run_id, "runId")
        _require_safe_id(self.task_id, "taskId")
        _require_positive_int(self.attempt, "attempt")
        workspace = self.workspace_root.absolute()
        scope = self.scope_root.absolute()
        if scope == workspace or not _is_relative_to(scope, workspace):
            raise AgentContractError("scope", "scopeRoot 必须位于 workspace 内部")
        _assert_no_symlink(scope, workspace)
        if self.scope_kind == "draft":
            expected_parent = workspace / "drafts"
            if scope.parent != expected_parent or not scope.name:
                raise AgentContractError(
                    "scope", "draft scopeRoot 必须是 workspace/drafts/<draft-id>"
                )

    @property
    def task_dir(self) -> Path:
        return (
            self.scope_root
            / ".work"
            / self.run_id
            / "agent-tasks"
            / self.task_id
            / f"attempt-{self.attempt:04d}"
        )

    @property
    def task_json(self) -> Path:
        return self.task_dir / "task.json"

    @property
    def result_json(self) -> Path:
        return self.task_dir / "result.json"

    def resolve_scope_path(
        self,
        relative: Any,
        *,
        field: str,
        require_task_dir: bool = False,
        require_exists: bool = False,
    ) -> Path:
        parts = _parse_posix_relative(relative, field)
        candidate = self.scope_root.joinpath(*parts).absolute()
        scope = self.scope_root.absolute()
        if not _is_relative_to(candidate, scope):
            raise AgentContractError("path_escape", f"{field} 逃逸 scopeRoot")
        _assert_no_symlink(candidate, scope)
        if ".work" in parts and not _is_relative_to(candidate, self.task_dir.absolute()):
            raise AgentContractError(
                "cross_attempt", f"{field} 指向其他 run/task/attempt"
            )
        if require_task_dir and not _is_relative_to(
            candidate, self.task_dir.absolute()
        ):
            raise AgentContractError("output_escape", f"{field} 不在当前 task 目录")
        if require_exists and (not candidate.exists() or not candidate.is_file()):
            raise AgentContractError("missing_file", f"{field} 对应文件不存在")
        return candidate

    def relative_posix(self, path: Path) -> str:
        absolute = path.absolute()
        if not _is_relative_to(absolute, self.scope_root.absolute()):
            raise AgentContractError("path_escape", "路径不在 scopeRoot")
        return PurePosixPath(*absolute.relative_to(self.scope_root.absolute()).parts).as_posix()


@dataclass(frozen=True)
class ValidatedAgentTask:
    data: Mapping[str, Any]
    context: TrustedTaskContext
    task_sha256: str
    input_files: tuple[Path, ...]
    allowed_output_files: tuple[Path, ...]
    role_contract_file: Path


@dataclass(frozen=True)
class ValidatedAgentResult:
    data: Mapping[str, Any]
    task: ValidatedAgentTask
    result_sha256: str
    output_files: tuple[Path, ...]


def _validate_file_records(
    records: Any,
    *,
    context: TrustedTaskContext,
    field: str,
    require_task_dir: bool,
    verify_files: bool,
) -> tuple[tuple[str, str, Path], ...]:
    if not isinstance(records, list):
        raise AgentContractError("schema", f"{field} 必须是数组")
    parsed: list[tuple[str, str, Path]] = []
    seen: set[str] = set()
    for index, raw in enumerate(records):
        record = _require_exact_keys(
            raw,
            required={"file", "sha256"},
            field=f"{field}[{index}]",
        )
        relative = record["file"]
        sha = _require_sha(record["sha256"], f"{field}[{index}].sha256")
        path = context.resolve_scope_path(
            relative,
            field=f"{field}[{index}].file",
            require_task_dir=require_task_dir,
            require_exists=verify_files,
        )
        if relative in seen:
            raise AgentContractError("schema", f"{field} 包含重复路径")
        seen.add(relative)
        if verify_files and sha256_file(path) != sha:
            raise StaleAgentTaskError(f"{field}[{index}] 的文件 SHA 已变化")
        parsed.append((relative, sha, path))
    return tuple(parsed)


def _validate_bindings(
    value: Any,
    expected: Mapping[str, str | None] | None,
) -> dict[str, str | None]:
    if not isinstance(value, dict):
        raise AgentContractError("schema", "currentBindings 必须是对象")
    unknown = set(value) - CURRENT_BINDING_ALLOWLIST
    if unknown:
        raise AgentContractError(
            "schema", f"currentBindings 包含未知字段: {sorted(unknown)}"
        )
    normalized: dict[str, str | None] = {}
    for key, raw in value.items():
        if raw is None:
            normalized[key] = None
        else:
            normalized[key] = _require_sha(raw, f"currentBindings.{key}")
    if expected is not None and normalized != dict(expected):
        raise StaleAgentTaskError("currentBindings 与当前 coordinator binding 不一致")
    return normalized


def validate_agent_task(
    task_json: Path,
    context: TrustedTaskContext,
    *,
    expected_current_bindings: Mapping[str, str | None] | None = None,
) -> ValidatedAgentTask:
    """严格验证 task、冻结输入、attempt 路径和 role contract。"""

    if task_json.absolute() != context.task_json.absolute():
        raise AgentContractError("task_location", "task.json 不在可信 attempt 目录")
    _assert_no_symlink(task_json, context.scope_root)
    data = _read_json(task_json)
    _require_exact_keys(
        data,
        required={
            "contractVersion",
            "taskId",
            "taskKind",
            "scopeKind",
            "roleContractVersion",
            "roleContractSha256",
            "attempt",
            "sequence",
            "inputs",
            "currentBindings",
            "requiredCapabilities",
            "allowedOutputs",
            "formalWritesAllowed",
            "approvalWritesAllowed",
        },
        optional={"sceneId"},
        field="task",
    )
    if data["contractVersion"] != TASK_CONTRACT_VERSION:
        raise AgentContractError("contract_version", "task contractVersion 不支持")
    if data["taskId"] != context.task_id:
        raise AgentContractError("task_identity", "taskId 与可信目录不匹配")
    task_kind = data["taskKind"]
    if task_kind not in TASK_KINDS:
        raise AgentContractError("task_kind", "taskKind 不在 allowlist")
    if data["scopeKind"] != context.scope_kind:
        raise AgentContractError("scope", "scopeKind 与可信上下文不匹配")
    if ROLE_SCOPE[task_kind] != context.scope_kind:
        raise AgentContractError("role_scope", "taskKind 与 scopeKind 不匹配")
    if data["roleContractVersion"] != ROLE_CONTRACT_VERSION:
        raise AgentContractError("role_contract", "roleContractVersion 不支持")
    role_sha = _require_sha(data["roleContractSha256"], "roleContractSha256")
    if _require_positive_int(data["attempt"], "attempt") != context.attempt:
        raise AgentContractError("attempt", "attempt 与 attempt 目录不匹配")
    _require_positive_int(data["sequence"], "sequence")
    if "sceneId" in data:
        _require_safe_id(data["sceneId"], "sceneId")
    if task_kind == "annotationDrafting" and "sceneId" not in data:
        raise AgentContractError("schema", "annotationDrafting 必须包含 sceneId")
    if task_kind != "annotationDrafting" and "sceneId" in data:
        raise AgentContractError("schema", "只有 annotationDrafting 可以包含 sceneId")
    if data["formalWritesAllowed"] is not False:
        raise AgentContractError("permission", "formalWritesAllowed 必须显式为 false")
    if data["approvalWritesAllowed"] is not False:
        raise AgentContractError("permission", "approvalWritesAllowed 必须显式为 false")

    capabilities = data["requiredCapabilities"]
    if not isinstance(capabilities, list) or any(
        not isinstance(item, str) for item in capabilities
    ):
        raise AgentContractError("schema", "requiredCapabilities 必须是字符串数组")
    if len(set(capabilities)) != len(capabilities):
        raise AgentContractError("schema", "requiredCapabilities 不能重复")
    unknown_caps = set(capabilities) - CAPABILITY_ALLOWLIST
    if unknown_caps:
        raise AgentContractError(
            "capability", f"requiredCapabilities 未知: {sorted(unknown_caps)}"
        )
    missing_caps = ROLE_REQUIRED_CAPABILITIES[task_kind] - set(capabilities)
    if missing_caps:
        raise AgentContractError(
            "capability", f"role 缺少必需能力: {sorted(missing_caps)}"
        )

    inputs = _validate_file_records(
        data["inputs"],
        context=context,
        field="inputs",
        require_task_dir=False,
        verify_files=True,
    )
    if not inputs:
        raise AgentContractError("schema", "inputs 不能为空")
    role_relative = context.relative_posix(context.task_dir / "role-contract.md")
    role_records = [record for record in inputs if record[0] == role_relative]
    if len(role_records) != 1 or role_records[0][1] != role_sha:
        raise AgentContractError(
            "role_contract", "inputs 必须绑定当前 attempt 的 frozen role-contract.md"
        )

    allowed = data["allowedOutputs"]
    if not isinstance(allowed, list) or not allowed:
        raise AgentContractError("schema", "allowedOutputs 必须是非空数组")
    if len(set(allowed)) != len(allowed):
        raise AgentContractError("schema", "allowedOutputs 不能重复")
    output_files: list[Path] = []
    for index, relative in enumerate(allowed):
        output = context.resolve_scope_path(
            relative,
            field=f"allowedOutputs[{index}]",
            require_task_dir=True,
        )
        if output.parent.absolute() != context.task_dir.absolute():
            raise AgentContractError(
                "output_escape", "allowedOutputs 必须是 attempt 目录直属文件"
            )
        if output.name not in ROLE_OUTPUT_BASENAMES[task_kind]:
            raise AgentContractError("output_allowlist", "输出文件名不在 role allowlist")
        output_files.append(output)
    if context.result_json.absolute() not in [path.absolute() for path in output_files]:
        raise AgentContractError("schema", "allowedOutputs 必须包含 result.json")
    required_output = ROLE_REQUIRED_OUTPUT_BASENAME[task_kind]
    if required_output is not None and required_output not in {
        path.name for path in output_files
    }:
        raise AgentContractError("schema", "allowedOutputs 缺少 role 候选文件")

    _validate_bindings(data["currentBindings"], expected_current_bindings)
    return ValidatedAgentTask(
        data=data,
        context=context,
        task_sha256=sha256_file(task_json),
        input_files=tuple(record[2] for record in inputs),
        allowed_output_files=tuple(output_files),
        role_contract_file=role_records[0][2],
    )


def _validate_findings(value: Any) -> None:
    if not isinstance(value, list):
        raise AgentContractError("schema", "findings 必须是数组")
    allowed = {"priority", "code", "message", "file", "summary"}
    for index, finding in enumerate(value):
        if isinstance(finding, str):
            continue
        if not isinstance(finding, dict) or set(finding) - allowed:
            raise AgentContractError("schema", f"findings[{index}] 字段不合法")
        if "message" not in finding and "summary" not in finding:
            raise AgentContractError(
                "schema", f"findings[{index}] 缺少 message/summary"
            )


def _validate_error(value: Any, status: str) -> None:
    if value is None:
        if status == "failed":
            raise AgentContractError("schema", "failed result 必须包含 error")
        return
    error = _require_exact_keys(
        value,
        required={"category", "message"},
        optional={"retryable"},
        field="error",
    )
    if not isinstance(error["category"], str) or not isinstance(error["message"], str):
        raise AgentContractError("schema", "error category/message 必须是字符串")
    if "retryable" in error and type(error["retryable"]) is not bool:
        raise AgentContractError("schema", "error.retryable 必须是 bool")


OutputValidator = Callable[[str, Path], None]


def validate_agent_result(
    result_json: Path,
    task: ValidatedAgentTask,
    *,
    dispatched_task_sha256: str,
    expected_current_bindings: Mapping[str, str | None] | None = None,
    output_validator: OutputValidator | None = None,
) -> ValidatedAgentResult:
    """验证 result、task 不可变性、输入 current binding 与输出字节。"""

    context = task.context
    if result_json.absolute() != context.result_json.absolute():
        raise AgentContractError("result_location", "result.json 不在可信 attempt 目录")
    _require_sha(dispatched_task_sha256, "dispatchedTaskSha256")
    current_task_sha = sha256_file(context.task_json)
    if current_task_sha != dispatched_task_sha256 or current_task_sha != task.task_sha256:
        raise StaleAgentTaskError("task.json 在派发后被修改")
    if sha256_file(task.role_contract_file) != task.data["roleContractSha256"]:
        raise StaleAgentTaskError("frozen role-contract.md 已变化")
    for path, record in zip(task.input_files, task.data["inputs"]):
        if not path.exists() or sha256_file(path) != record["sha256"]:
            raise StaleAgentTaskError("task input 在执行期间已变化")
    _validate_bindings(task.data["currentBindings"], expected_current_bindings)

    data = _read_json(result_json)
    _require_exact_keys(
        data,
        required={
            "contractVersion",
            "taskId",
            "taskKind",
            "scopeKind",
            "attempt",
            "taskSha256",
            "roleContractVersion",
            "roleContractSha256",
            "sequence",
            "status",
            "inspectedInputs",
            "outputs",
            "findings",
            "warnings",
            "error",
        },
        field="result",
    )
    expected_equal = {
        "contractVersion": RESULT_CONTRACT_VERSION,
        "taskId": task.data["taskId"],
        "taskKind": task.data["taskKind"],
        "scopeKind": task.data["scopeKind"],
        "attempt": task.data["attempt"],
        "taskSha256": dispatched_task_sha256,
        "roleContractVersion": task.data["roleContractVersion"],
        "roleContractSha256": task.data["roleContractSha256"],
        "sequence": task.data["sequence"],
    }
    for key, expected in expected_equal.items():
        if data[key] != expected:
            raise AgentContractError("result_binding", f"result.{key} 与 task 不匹配")
    status = data["status"]
    if status not in RESULT_STATUSES:
        raise AgentContractError("schema", "result.status 不在 allowlist")
    _validate_findings(data["findings"])
    if not isinstance(data["warnings"], list) or any(
        not isinstance(item, str) for item in data["warnings"]
    ):
        raise AgentContractError("schema", "warnings 必须是字符串数组")
    _validate_error(data["error"], status)

    inspected = _validate_file_records(
        data["inspectedInputs"],
        context=context,
        field="inspectedInputs",
        require_task_dir=False,
        verify_files=True,
    )
    expected_inputs = {
        (record["file"], record["sha256"]) for record in task.data["inputs"]
    }
    if {(record[0], record[1]) for record in inspected} != expected_inputs:
        raise AgentContractError(
            "inspected_inputs", "inspectedInputs 必须完整回显全部冻结 inputs"
        )

    outputs = _validate_file_records(
        data["outputs"],
        context=context,
        field="outputs",
        require_task_dir=True,
        verify_files=True,
    )
    allowed_relatives = {
        context.relative_posix(path)
        for path in task.allowed_output_files
        if path.name != "result.json"
    }
    if any(record[0] not in allowed_relatives for record in outputs):
        raise AgentContractError("output_escape", "result 声明了未授权输出")
    required_output = ROLE_REQUIRED_OUTPUT_BASENAME[task.data["taskKind"]]
    if status == "completed" and required_output is not None and required_output not in {
        record[2].name for record in outputs
    }:
        raise AgentContractError("missing_output", "completed result 缺少必需候选")
    if status == "completed" and output_validator is not None:
        for _relative, _sha, path in outputs:
            if path.name != "agent.log":
                output_validator(task.data["taskKind"], path)

    return ValidatedAgentResult(
        data=data,
        task=task,
        result_sha256=sha256_file(result_json),
        output_files=tuple(record[2] for record in outputs),
    )


DispatchMode = Literal["dispatch", "fallback", "blocked", "no_ready"]


@dataclass(frozen=True)
class AgentDispatchDecision:
    dispatch_allowed: bool
    effective_agent_concurrency: int
    mode: DispatchMode
    reason: str


def _validate_capacity(value: int, field: str, *, allow_zero: bool) -> int:
    minimum = 0 if allow_zero else 1
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise AgentContractError("capacity", f"{field} 必须至少为 {minimum}")
    return value


def decide_agent_dispatch(
    task: ValidatedAgentTask,
    *,
    configured: int,
    ready_tasks: int,
    runtime_child_slots: int,
    resource_budget: int,
    runtime_role_capabilities: Iterable[str],
    coordinator_capabilities: Iterable[str],
) -> AgentDispatchDecision:
    """计算 dispatch/fallback/BLOCKED；child slot 已是一次转换后的子槽数。"""

    configured = _validate_capacity(configured, "configured", allow_zero=False)
    ready_tasks = _validate_capacity(ready_tasks, "readyTasks", allow_zero=True)
    runtime_child_slots = _validate_capacity(
        runtime_child_slots, "runtimeChildSlots", allow_zero=True
    )
    resource_budget = _validate_capacity(
        resource_budget, "resourceBudget", allow_zero=True
    )
    if ready_tasks == 0:
        return AgentDispatchDecision(
            False,
            0,
            "no_ready",
            "没有 ready task",
        )
    required = set(task.data["requiredCapabilities"])
    runtime_caps = set(runtime_role_capabilities)
    coordinator_caps = set(coordinator_capabilities)
    runtime_ready = required.issubset(runtime_caps)
    effective = min(
        configured, ready_tasks, runtime_child_slots, resource_budget
    )
    if runtime_ready and effective > 0:
        return AgentDispatchDecision(
            True,
            effective,
            "dispatch",
            "宿主协作能力与并发预算可用",
        )
    if required.issubset(coordinator_caps):
        if not runtime_ready:
            reason = "runtime 缺少 role 能力，coordinator 串行 fallback"
        else:
            reason = "runtime 子槽或资源预算为 0，coordinator 串行 fallback"
        return AgentDispatchDecision(
            False,
            0,
            "fallback",
            reason,
        )
    if "viewImage" in required and "viewImage" not in coordinator_caps:
        reason = "视觉 role 缺少真实 viewImage 能力"
    else:
        reason = "coordinator 缺少 role fallback 能力"
    return AgentDispatchDecision(
        False,
        0,
        "blocked",
        reason,
    )


AGENT_RUNTIME_STATUSES = frozenset(
    {"dispatched", "running", "completed", "failed", "cancelled"}
)
_RUNTIME_ID_SEGMENT = r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}"
_SAFE_RUNTIME_ID_RE = re.compile(
    rf"^(?:{_RUNTIME_ID_SEGMENT}(?:/{_RUNTIME_ID_SEGMENT})*|"
    rf"/root(?:/{_RUNTIME_ID_SEGMENT})*)$"
)
_SENSITIVE_RUNTIME_ID_SEGMENTS = frozenset(
    {
        "apikey",
        "api-key",
        "api_key",
        "authorization",
        "cookie",
        "cookies",
        "credential",
        "credentials",
        "secret",
        "secrets",
        "token",
        "tokens",
    }
)


def _require_runtime_id(value: Any, field: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) > 256
        or _SAFE_RUNTIME_ID_RE.fullmatch(value) is None
    ):
        raise AgentContractError("audit", f"{field} 不是安全 runtime 标识")
    segments = [segment.casefold() for segment in value.split("/") if segment]
    if any(segment in _SENSITIVE_RUNTIME_ID_SEGMENTS for segment in segments):
        raise AgentContractError("audit", f"{field} 包含敏感型 runtime 标识段")
    return value


def build_agent_batch_audit(
    *,
    stage: str,
    configured: int,
    task_count: int,
    decision: AgentDispatchDecision,
    peak_child_agents: int = 0,
    task_agents: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """构造不进入作品 identity 的非敏感 agent batch 审计。"""

    if not isinstance(stage, str) or stage not in TASK_KINDS:
        raise AgentContractError("audit", "stage 必须是 allowlisted agent role")
    configured = _validate_capacity(configured, "configured", allow_zero=False)
    task_count = _validate_capacity(task_count, "taskCount", allow_zero=True)
    peak_child_agents = _validate_capacity(
        peak_child_agents,
        "peakChildAgents",
        allow_zero=True,
    )
    if peak_child_agents > decision.effective_agent_concurrency:
        raise AgentContractError("audit", "peakChildAgents 不能超过 effective")
    if not decision.dispatch_allowed and peak_child_agents != 0:
        raise AgentContractError("audit", "未 dispatch 时 peakChildAgents 必须为 0")
    records: list[dict[str, str]] = []
    seen_tasks: set[str] = set()
    for index, raw in enumerate(task_agents):
        record = _require_exact_keys(
            raw,
            required={"taskId", "agentId", "status"},
            field=f"taskAgents[{index}]",
        )
        task_id = _require_safe_id(record["taskId"], f"taskAgents[{index}].taskId")
        agent_id = _require_runtime_id(
            record["agentId"],
            f"taskAgents[{index}].agentId",
        )
        status = record["status"]
        if not isinstance(status, str) or status not in AGENT_RUNTIME_STATUSES:
            raise AgentContractError("audit", "task agent status 不在 allowlist")
        if task_id in seen_tasks:
            raise AgentContractError("audit", "同一 taskId 不能重复记录")
        seen_tasks.add(task_id)
        records.append({"taskId": task_id, "agentId": agent_id, "status": status})
    if len(records) > task_count:
        raise AgentContractError("audit", "taskAgents 数量不能超过 taskCount")
    if records and not decision.dispatch_allowed:
        raise AgentContractError("audit", "未 dispatch 时不能记录真实 agentId")

    if decision.mode == "dispatch":
        mode = "host_collaboration_dispatch"
        adapter = "codex_collaboration"
    elif decision.mode == "fallback":
        mode = "coordinator_fallback"
        adapter = "coordinator"
    else:
        mode = decision.mode
        adapter = "none"
    return {
        "stage": stage,
        "configuredAgentConcurrency": configured,
        "effectiveAgentConcurrency": decision.effective_agent_concurrency,
        "dispatchAllowed": decision.dispatch_allowed,
        "mode": mode,
        "adapter": adapter,
        "taskCount": task_count,
        "peakChildAgents": peak_child_agents,
        "taskAgents": records,
        "reason": decision.reason,
    }


def build_agent_prompt(
    *,
    task_json: Path,
    role_contract: Path,
    task_kind: str,
    task_sha256: str,
    role_contract_sha256: str,
) -> str:
    """生成不携带业务正文、主对话或数组的最小定位 prompt。"""

    if task_kind not in TASK_KINDS:
        raise AgentContractError("task_kind", "taskKind 不在 allowlist")
    _require_sha(task_sha256, "taskSha256")
    _require_sha(role_contract_sha256, "roleContractSha256")
    if not task_json.is_absolute() or not role_contract.is_absolute():
        raise AgentContractError("prompt", "prompt 定位路径必须是绝对路径")
    if task_json.parent != role_contract.parent:
        raise AgentContractError("prompt", "task 与 role contract 必须属于同一 attempt")
    attempt_dir = task_json.parent
    result_json = attempt_dir / "result.json"
    return (
        f"ROLE_CONTRACT_PATH={role_contract}\n"
        f"ROLE_CONTRACT_SHA256={role_contract_sha256}\n"
        f"TASK_JSON_PATH={task_json}\n"
        f"TASK_SHA256={task_sha256}\n"
        f"ALLOWED_ATTEMPT_DIR={attempt_dir}\n\n"
        "RETURN_FORMAT:\n"
        "TASK_STATUS=<completed|failed|cancelled>\n"
        f"RESULT_JSON={result_json}\n"
        "VALIDATOR_STATUS=<PASS|FAIL|NOT_RUN>\n"
        "SUMMARY=<不超过240个字符的精简摘要>"
    )


def build_agent_bundle_prompt(
    tasks: Sequence[ValidatedAgentTask],
    *,
    max_tasks: int = 3,
) -> str:
    """为同一 dispatch unit 生成多个独立 task 的最小冻结 prompt。

    bundle 只改变 child 生命周期粒度；每个 task/attempt/result 仍由原合同独立
    校验。
    """

    if isinstance(max_tasks, bool) or not isinstance(max_tasks, int) or max_tasks < 1:
        raise AgentContractError("prompt", "maxTasks 必须是正整数")
    frozen = tuple(tasks)
    if not frozen or len(frozen) > max_tasks:
        raise AgentContractError("prompt", f"bundle task 数必须为 1–{max_tasks}")
    kinds = {str(task.data["taskKind"]) for task in frozen}
    runs = {task.context.run_id for task in frozen}
    sequences = [int(task.data["sequence"]) for task in frozen]
    task_ids = [str(task.data["taskId"]) for task in frozen]
    if kinds != {"annotationDrafting"}:
        raise AgentContractError("prompt", "首版 bundle 只允许 annotationDrafting")
    if len(runs) != 1:
        raise AgentContractError("prompt", "bundle tasks 必须属于同一 run")
    if sequences != sorted(sequences) or len(set(sequences)) != len(sequences):
        raise AgentContractError("prompt", "bundle task sequence 必须严格递增")
    if len(set(task_ids)) != len(task_ids):
        raise AgentContractError("prompt", "bundle taskId 不能重复")

    blocks = [
        "TASK_BUNDLE_VERSION=whiteboard-agent-task-bundle-v1",
        f"TASK_COUNT={len(frozen)}",
    ]
    result_paths: list[str] = []
    for index, task in enumerate(frozen, start=1):
        task_json = task.context.task_json.resolve(strict=True)
        role_contract = task.role_contract_file.resolve(strict=True)
        attempt_dir = task.context.task_dir.resolve(strict=True)
        result_json = task.context.result_json.resolve(strict=False)
        if task_json.parent != role_contract.parent or task_json.parent != attempt_dir:
            raise AgentContractError(
                "prompt", "bundle task 与 role contract 必须属于同一 attempt"
            )
        role_sha = _require_sha(
            task.data["roleContractSha256"],
            f"tasks[{index - 1}].roleContractSha256",
        )
        task_sha = _require_sha(
            task.task_sha256,
            f"tasks[{index - 1}].taskSha256",
        )
        blocks.extend(
            [
                "",
                f"TASK_{index}_ROLE_CONTRACT_PATH={role_contract}",
                f"TASK_{index}_ROLE_CONTRACT_SHA256={role_sha}",
                f"TASK_{index}_JSON_PATH={task_json}",
                f"TASK_{index}_SHA256={task_sha}",
                f"TASK_{index}_ALLOWED_ATTEMPT_DIR={attempt_dir}",
                f"TASK_{index}_RESULT_JSON={result_json}",
            ]
        )
        result_paths.append(str(result_json))
    blocks.extend(
        [
            "",
            "PROCESSING_RULES:",
            "按 TASK_1..N 顺序处理；每个 task 独立写自己的 candidate/result。",
            "单个 task 失败时写该 task 的 failed result，并继续后续 task。",
            "不得写列出的 attempt 目录之外的文件，不得合并多幕 candidate/result。",
            "",
            "RETURN_FORMAT:",
            "DISPATCH_UNIT_STATUS=<completed|partial|failed|cancelled>",
            "RESULT_JSONS=<按 task sequence 的 JSON 字符串数组>",
            "VALIDATOR_STATUS=<PASS|PARTIAL|FAIL|NOT_RUN>",
            "SUMMARY=<不超过240个字符的精简摘要>",
            "EXPECTED_RESULT_JSONS="
            + json.dumps(result_paths, ensure_ascii=False, separators=(",", ":")),
        ]
    )
    return "\n".join(blocks)


@dataclass(frozen=True)
class AgentAttemptSummary:
    task_id: str
    attempt: int
    sequence: int
    status: Literal["completed", "failed", "cancelled", "stale", "blocked"]
    reason: str | None = None

    def __post_init__(self) -> None:
        _require_safe_id(self.task_id, "taskId")
        _require_positive_int(self.attempt, "attempt")
        _require_positive_int(self.sequence, "sequence")
        if self.status not in ATTEMPT_STATUSES:
            raise AgentContractError("status", "attempt status 不在 allowlist")


class FakeAgentScheduler:
    """只记录 fake completion 的确定性 scheduler；不会创建真实 agent。"""

    def __init__(self, planned: Iterable[AgentAttemptSummary]) -> None:
        self._planned = tuple(planned)
        identities = {(item.task_id, item.attempt) for item in self._planned}
        if len(identities) != len(self._planned):
            raise AgentContractError("scheduler", "计划包含重复 task attempt")
        sequences = [item.sequence for item in self._planned]
        if len(set(sequences)) != len(sequences):
            raise AgentContractError("scheduler", "计划 sequence 必须唯一")
        self._by_identity = {
            (item.task_id, item.attempt): item for item in self._planned
        }
        self._completed: dict[tuple[str, int], AgentAttemptSummary] = {}

    def record(self, summary: AgentAttemptSummary) -> None:
        identity = (summary.task_id, summary.attempt)
        planned = self._by_identity.get(identity)
        if planned is None or planned.sequence != summary.sequence:
            raise AgentContractError("scheduler", "结果不属于本批次计划")
        if identity in self._completed:
            raise AgentContractError("scheduler", "同一 attempt 不能重复完成")
        self._completed[identity] = summary

    def ordered_results(self) -> tuple[AgentAttemptSummary, ...]:
        return tuple(sorted(self._completed.values(), key=lambda item: item.sequence))

    def retry_candidates(self) -> tuple[AgentAttemptSummary, ...]:
        return tuple(
            item
            for item in self.ordered_results()
            if item.status in RETRYABLE_ATTEMPT_STATUSES
        )

    def pending(self) -> tuple[AgentAttemptSummary, ...]:
        return tuple(
            item
            for item in sorted(self._planned, key=lambda candidate: candidate.sequence)
            if (item.task_id, item.attempt) not in self._completed
        )
