#!/usr/bin/env python3
"""在工作区 runtime 下准备 Python 环境，并固定 pip 缓存与临时目录。"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import venv
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

try:
    from .project_workspace import (
        WorkspaceError,
        load_workspace_config,
        probe_workspace_access,
    )
except ImportError:  # pragma: no cover - 兼容直接执行 scripts/prepare_env.py
    from project_workspace import (
        WorkspaceError,
        load_workspace_config,
        probe_workspace_access,
    )


BASE_DEPS: dict[str, str] = {
    "cv2": "opencv-python",
    "numpy": "numpy",
    "av": "av",
    "PIL": "Pillow",
}
EDGE_TTS_VERSION = "7.2.8"
FUNASR_VERSION = "1.4.3"
MODELSCOPE_VERSION = "1.39.1"
TORCH_VERSION = "2.11.0"
TORCHAUDIO_VERSION = TORCH_VERSION
NARRATION_ASR_CONTRACT = "narration-asr-models-v1"
NARRATION_ASR_RECEIPT_NAME = "narration-asr-models.json"
NARRATION_ASR_DEPS: dict[str, str] = {
    "funasr": f"funasr=={FUNASR_VERSION}",
    "modelscope": f"modelscope=={MODELSCOPE_VERSION}",
    "torch": f"torch=={TORCH_VERSION}",
    "torchaudio": f"torchaudio=={TORCHAUDIO_VERSION}",
}
NARRATION_ASR_MODELS: tuple[dict[str, str], ...] = (
    {
        "alias": "paraformer-zh",
        "modelId": "iic/speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-pytorch",
        "requestedRevision": "master",
    },
    {
        "alias": "fsmn-vad",
        "modelId": "iic/speech_fsmn_vad_zh-cn-16k-common-pytorch",
        "requestedRevision": "master",
    },
    {
        "alias": "ct-punc",
        "modelId": "iic/punc_ct-transformer_zh-cn-common-vocab272727-pytorch",
        "requestedRevision": "master",
    },
)
FEATURE_DEPS: dict[str, dict[str, str]] = {
    "edge-tts": {"edge_tts": f"edge-tts=={EDGE_TTS_VERSION}"},
    "narration-asr": NARRATION_ASR_DEPS,
}
# 保留旧名称，供只关心基础渲染依赖的调用方使用。
DEPS = BASE_DEPS

_PROBE_RESULT_PREFIX = "ENV_DEPENDENCY_PROBE="
_NARRATION_ASR_RUNTIME_PROBE_PREFIX = "NARRATION_ASR_RUNTIME_PROBE="
_MODEL_PREPARE_RESULT_PREFIX = "NARRATION_ASR_MODEL_PREPARE="
_DEPENDENCY_PROBE_CODE = r"""
import importlib
import importlib.metadata
import json
import sys

specifications = json.loads(sys.argv[1])
results = []
for specification in specifications:
    available = False
    try:
        importlib.import_module(specification["importName"])
        expected_version = specification.get("expectedVersion")
        if expected_version is None:
            available = True
        else:
            actual_version = importlib.metadata.version(specification["distribution"])
            available = actual_version == expected_version
    except BaseException:
        # 单个依赖失败不能阻断同批其他依赖的独立探测。
        available = False
    results.append({"importName": specification["importName"], "available": available})
print("ENV_DEPENDENCY_PROBE=" + json.dumps(results, separators=(",", ":")))
"""
_MODEL_PREPARE_CODE = r"""
import json
import sys
from pathlib import Path

from modelscope.hub.snapshot_download import snapshot_download

cache_root = Path(sys.argv[1]).resolve()
models = json.loads(sys.argv[2])
resolved = []
for model in models:
    path = Path(
        snapshot_download(
            model["modelId"],
            revision=model["requestedRevision"],
            cache_dir=str(cache_root),
        )
    ).resolve()
    resolved.append({**model, "path": str(path)})
print("NARRATION_ASR_MODEL_PREPARE=" + json.dumps(resolved, separators=(",", ":")))
"""
_NARRATION_ASR_RUNTIME_PROBE_CODE = r"""
import json

import torch
import torchaudio
import torchaudio.compliance.kaldi as kaldi
from funasr.frontends.wav_frontend import WavFrontend

sample_count = 3200
waveform = torch.zeros((1, sample_count), dtype=torch.float32)
direct_features = kaldi.fbank(
    waveform,
    num_mel_bins=80,
    sample_frequency=16000,
    dither=0.0,
)
frontend = WavFrontend(fs=16000, n_mels=80, dither=0.0)
frontend_features, frontend_lengths = frontend(
    waveform,
    torch.tensor([sample_count], dtype=torch.int64),
)
available = bool(
    direct_features.ndim == 2
    and direct_features.shape[0] > 0
    and frontend_features.ndim == 3
    and frontend_features.shape[1] > 0
    and int(frontend_lengths[0]) > 0
)
print(
    "NARRATION_ASR_RUNTIME_PROBE="
    + json.dumps(
        {
            "available": available,
            "torchVersion": torch.__version__.split("+")[0],
            "torchaudioVersion": torchaudio.__version__.split("+")[0],
        },
        separators=(",", ":"),
    )
)
"""


class NarrationAsrBlockedError(RuntimeError):
    """narration-asr 首次准备需要的依赖或模型当前无法取得。"""


def runtime_paths(config_path: str | Path | None = None) -> tuple[Path, Path, Path]:
    workspace = load_workspace_config(config_path)
    runtime = workspace.runtime_dir
    return runtime / ".venv", runtime / "cache" / "pip", runtime / "tmp"


def narration_asr_paths(
    config_path: str | Path | None = None,
) -> tuple[Path, Path]:
    """返回当前白板工作区独占的 FunASR 模型缓存与 receipt 路径。"""
    workspace = load_workspace_config(config_path)
    cache_root = workspace.runtime_dir / "cache" / "funasr-models"
    return cache_root, cache_root / NARRATION_ASR_RECEIPT_NAME


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return path != parent


def load_narration_asr_model_paths(
    config_path: str | Path | None = None,
    *,
    receipt_path: str | Path | None = None,
) -> dict[str, Path]:
    """严格读取当前 workspace receipt；只返回已缓存的本地模型目录。"""
    cache_root, default_receipt = narration_asr_paths(config_path)
    receipt = default_receipt if receipt_path is None else Path(receipt_path)
    try:
        payload = json.loads(receipt.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RuntimeError(f"narration-asr 模型 receipt 不存在: {receipt}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"narration-asr 模型 receipt 无法读取: {receipt}") from exc

    expected_cache = cache_root.resolve()
    if not isinstance(payload, dict):
        raise RuntimeError("narration-asr 模型 receipt 必须是 JSON 对象")
    if payload.get("schemaVersion") != 1 or payload.get("contract") != NARRATION_ASR_CONTRACT:
        raise RuntimeError("narration-asr 模型 receipt 合同不匹配")
    try:
        recorded_cache = Path(payload["cacheRoot"]).resolve()
    except (KeyError, TypeError, OSError) as exc:
        raise RuntimeError("narration-asr 模型 receipt cacheRoot 无效") from exc
    if recorded_cache != expected_cache:
        raise RuntimeError("narration-asr 模型 receipt 不属于当前白板工作区")

    raw_models = payload.get("models")
    if not isinstance(raw_models, list):
        raise RuntimeError("narration-asr 模型 receipt models 必须是数组")
    expected_by_alias = {model["alias"]: model for model in NARRATION_ASR_MODELS}
    resolved: dict[str, Path] = {}
    for item in raw_models:
        if not isinstance(item, dict) or not isinstance(item.get("alias"), str):
            raise RuntimeError("narration-asr 模型 receipt 包含无效模型项")
        alias = item["alias"]
        expected = expected_by_alias.get(alias)
        if expected is None or alias in resolved:
            raise RuntimeError("narration-asr 模型 receipt 包含未知或重复 alias")
        if (
            item.get("modelId") != expected["modelId"]
            or item.get("requestedRevision") != expected["requestedRevision"]
        ):
            raise RuntimeError(f"narration-asr 模型合同不匹配: {alias}")
        try:
            local_path = Path(item["path"]).resolve()
        except (KeyError, TypeError, OSError) as exc:
            raise RuntimeError(f"narration-asr 模型路径无效: {alias}") from exc
        if not _is_within(local_path, expected_cache):
            raise RuntimeError(f"narration-asr 模型路径越出当前 workspace 缓存: {alias}")
        try:
            populated = local_path.is_dir() and next(local_path.iterdir(), None) is not None
        except OSError as exc:
            raise RuntimeError(f"narration-asr 模型目录无法读取: {alias}") from exc
        if not populated:
            raise RuntimeError(f"narration-asr 模型目录缺失或为空: {alias}")
        resolved[alias] = local_path
    if set(resolved) != set(expected_by_alias):
        raise RuntimeError("narration-asr 模型 receipt 未完整覆盖固定三模型合同")
    return resolved


def interpreter_path(venv_root: Path) -> Path:
    if sys.platform.startswith("win"):
        return venv_root / "Scripts" / "python.exe"
    return venv_root / "bin" / "python"


def subprocess_environment(pip_cache: Path, runtime_tmp: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["PIP_CACHE_DIR"] = str(pip_cache)
    env["TEMP"] = str(runtime_tmp)
    env["TMP"] = str(runtime_tmp)
    return env


@contextmanager
def _runtime_temp_environment(runtime_tmp: Path) -> Iterator[None]:
    """让 venv 内部启动的 ensurepip 也继承 D 盘临时目录。"""
    previous = {name: os.environ.get(name) for name in ("TEMP", "TMP")}
    os.environ["TEMP"] = str(runtime_tmp)
    os.environ["TMP"] = str(runtime_tmp)
    try:
        yield
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def ensure_venv(
    check_only: bool,
    config_path: str | Path | None = None,
) -> tuple[Path, Path, Path]:
    venv_root, pip_cache, runtime_tmp = runtime_paths(config_path)
    py = interpreter_path(venv_root)
    if venv_root.exists() and py.exists():
        print(f"[ok] 复用现有虚拟环境: {venv_root}")
        return py, pip_cache, runtime_tmp
    if check_only:
        raise RuntimeError(f"虚拟环境尚未建立: {venv_root}")

    pip_cache.mkdir(parents=True, exist_ok=True)
    runtime_tmp.mkdir(parents=True, exist_ok=True)
    print(f"[..] 建立虚拟环境: {venv_root}")
    with _runtime_temp_environment(runtime_tmp):
        venv.create(str(venv_root), with_pip=True)
    if not py.exists():
        raise RuntimeError(f"虚拟环境未产生解释器: {py}")
    print("[ok] 虚拟环境就绪")
    return py, pip_cache, runtime_tmp


def probe_dependencies(
    py: Path,
    dependencies: dict[str, str],
    env: dict[str, str],
) -> dict[str, bool]:
    """用一次解释器子进程独立探测全部 import 与冻结版本。"""
    specifications: list[dict[str, str]] = []
    for import_name, pip_requirement in dependencies.items():
        specification = {"importName": import_name}
        if "==" in pip_requirement:
            distribution, expected_version = pip_requirement.rsplit("==", 1)
            specification.update(
                {"distribution": distribution, "expectedVersion": expected_version}
            )
        specifications.append(specification)

    probe = subprocess.run(
        [
            str(py),
            "-c",
            _DEPENDENCY_PROBE_CODE,
            json.dumps(specifications, ensure_ascii=True, separators=(",", ":")),
        ],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    if probe.returncode != 0:
        raise RuntimeError("依赖批量探测子进程失败")

    payload_line = next(
        (
            line[len(_PROBE_RESULT_PREFIX) :]
            for line in reversed(probe.stdout.splitlines())
            if line.startswith(_PROBE_RESULT_PREFIX)
        ),
        None,
    )
    if payload_line is None:
        raise RuntimeError("依赖批量探测未返回结构化结果")
    try:
        raw_results = json.loads(payload_line)
    except json.JSONDecodeError as exc:
        raise RuntimeError("依赖批量探测返回了无效 JSON") from exc
    if not isinstance(raw_results, list):
        raise RuntimeError("依赖批量探测结果必须是数组")

    results: dict[str, bool] = {}
    for item in raw_results:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("importName"), str)
            or type(item.get("available")) is not bool
            or item["importName"] in results
        ):
            raise RuntimeError("依赖批量探测结果结构无效")
        results[item["importName"]] = item["available"]
    if set(results) != set(dependencies):
        raise RuntimeError("依赖批量探测结果与请求不一致")
    return results


def install(py: Path, packages: list[str], env: dict[str, str]) -> bool:
    if not packages:
        return True
    print(f"[..] 安装依赖: {', '.join(packages)}")
    result = subprocess.run(
        [str(py), "-m", "pip", "install", "--quiet", *packages],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    if result.returncode != 0:
        print(f"[err] 安装失败:\n{result.stderr}", file=sys.stderr)
        return False
    print("[ok] 依赖安装完成")
    return True


def probe_narration_asr_runtime(py: Path, env: dict[str, str]) -> dict[str, object]:
    """验证 FunASR 实际依赖的 torchaudio fbank 与 WavFrontend 能完成计算。"""

    probe = subprocess.run(
        [str(py), "-c", _NARRATION_ASR_RUNTIME_PROBE_CODE],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    if probe.returncode != 0:
        detail = (probe.stderr or probe.stdout).strip().splitlines()
        suffix = f": {detail[-1][:300]}" if detail else ""
        raise RuntimeError(f"narration-asr 前端功能探测失败{suffix}")
    payload_line = next(
        (
            line[len(_NARRATION_ASR_RUNTIME_PROBE_PREFIX) :]
            for line in reversed(probe.stdout.splitlines())
            if line.startswith(_NARRATION_ASR_RUNTIME_PROBE_PREFIX)
        ),
        None,
    )
    if payload_line is None:
        raise RuntimeError("narration-asr 前端功能探测未返回结构化结果")
    try:
        payload = json.loads(payload_line)
    except json.JSONDecodeError as exc:
        raise RuntimeError("narration-asr 前端功能探测返回了无效 JSON") from exc
    if not isinstance(payload, dict) or payload.get("available") is not True:
        raise RuntimeError("narration-asr 前端无法完成 fbank 计算")
    if payload.get("torchVersion") != TORCH_VERSION or payload.get(
        "torchaudioVersion"
    ) != TORCHAUDIO_VERSION:
        raise RuntimeError("narration-asr torch/torchaudio 运行时版本不匹配")
    return payload


def _narration_asr_environment(
    env: dict[str, str],
    cache_root: Path,
) -> dict[str, str]:
    prepared = env.copy()
    prepared["MODELSCOPE_CACHE"] = str(cache_root)
    prepared["FUNASR_HOME"] = str(cache_root)
    return prepared


def _write_json_atomic(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def prepare_narration_asr_models(
    py: Path,
    cache_root: Path,
    receipt_path: Path,
    env: dict[str, str],
    *,
    config_path: str | Path | None = None,
) -> dict[str, Path]:
    """首次准备固定三模型；已有合法 receipt 时严格复用，不触发更新。"""
    try:
        return load_narration_asr_model_paths(
            config_path,
            receipt_path=receipt_path,
        )
    except RuntimeError:
        pass

    cache_root.mkdir(parents=True, exist_ok=True)
    print(f"[..] 准备 narration-asr 固定模型缓存: {cache_root}")
    prepared_env = _narration_asr_environment(env, cache_root)
    result = subprocess.run(
        [
            str(py),
            "-c",
            _MODEL_PREPARE_CODE,
            str(cache_root),
            json.dumps(NARRATION_ASR_MODELS, ensure_ascii=True, separators=(",", ":")),
        ],
        capture_output=True,
        text=True,
        env=prepared_env,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        if len(detail) > 2000:
            detail = detail[-2000:]
        suffix = f": {detail}" if detail else ""
        raise NarrationAsrBlockedError(
            "narration-asr 模型尚未缓存，且本次下载未完成；"
            f"请恢复网络后重试 --feature narration-asr{suffix}"
        )
    payload_line = next(
        (
            line[len(_MODEL_PREPARE_RESULT_PREFIX) :]
            for line in reversed(result.stdout.splitlines())
            if line.startswith(_MODEL_PREPARE_RESULT_PREFIX)
        ),
        None,
    )
    if payload_line is None:
        raise NarrationAsrBlockedError("narration-asr 模型准备未返回结构化结果")
    try:
        models = json.loads(payload_line)
    except json.JSONDecodeError as exc:
        raise NarrationAsrBlockedError("narration-asr 模型准备返回了无效 JSON") from exc
    if not isinstance(models, list):
        raise NarrationAsrBlockedError("narration-asr 模型准备结果必须是数组")
    receipt_payload: dict[str, object] = {
        "schemaVersion": 1,
        "contract": NARRATION_ASR_CONTRACT,
        "cacheRoot": str(cache_root.resolve()),
        "dependencyRequirements": list(NARRATION_ASR_DEPS.values()),
        "models": models,
    }
    _write_json_atomic(receipt_path, receipt_payload)
    try:
        resolved = load_narration_asr_model_paths(
            config_path,
            receipt_path=receipt_path,
        )
    except RuntimeError as exc:
        raise NarrationAsrBlockedError(
            f"narration-asr 模型下载结果未通过本地路径合同校验: {exc}"
        ) from exc
    print("[ok] narration-asr 固定三模型缓存就绪")
    return resolved


def _print_narration_asr_status(
    status: str,
    cache_root: Path,
    receipt_path: Path,
    *,
    reason: str | None = None,
    model_paths: dict[str, Path] | None = None,
) -> None:
    payload: dict[str, object] = {
        "status": status,
        "contract": NARRATION_ASR_CONTRACT,
        "cacheRoot": str(cache_root.resolve()),
        "receiptPath": str(receipt_path.resolve()),
        "models": {
            alias: str(path.resolve()) for alias, path in sorted((model_paths or {}).items())
        },
    }
    if reason is not None:
        payload["reason"] = reason
    print(
        "NARRATION_ASR_ENV="
        + json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    )


def _parse_arguments(arguments: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="准备基础白板渲染环境；语音 provider 依赖仅在显式 feature 下安装。"
    )
    parser.add_argument("--check", action="store_true", help="只检查，不安装缺失依赖")
    parser.add_argument(
        "--feature",
        choices=sorted(FEATURE_DEPS),
        help=(
            "可选能力；edge-tts 准备 Edge，narration-asr 准备当前 workspace "
            "自有的 FunASR CPU 依赖与固定模型缓存"
        ),
    )
    parser.add_argument("--config", help="workspace.local.json 路径")
    parser.add_argument(
        "--check-workspace-access",
        action="store_true",
        help="只运行工作区 create/write/flush/read/delete 预检并输出结构化结果",
    )
    return parser.parse_args(arguments)


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    try:
        args = _parse_arguments(arguments)
    except SystemExit as exc:
        # argparse 已输出具体参数错误；作为可调用函数时仍返回统一退出码。
        if exc.code == 0:
            return 0
        return 2
    try:
        if args.check_workspace_access:
            workspace = load_workspace_config(args.config, verify_writable=False)
            access = probe_workspace_access(workspace.root)
            print(
                "WORKSPACE_ACCESS="
                + json.dumps(access.as_dict(), ensure_ascii=False, sort_keys=True)
            )
            return 0 if access.ok else 2

        py, pip_cache, runtime_tmp = ensure_venv(args.check, args.config)
        # 即使复用既有环境，也要建立并显式使用固定缓存和临时目录。
        pip_cache.mkdir(parents=True, exist_ok=True)
        runtime_tmp.mkdir(parents=True, exist_ok=True)
        env = subprocess_environment(pip_cache, runtime_tmp)
        dependencies = dict(BASE_DEPS)
        if args.feature is not None:
            dependencies.update(FEATURE_DEPS[args.feature])
        availability = probe_dependencies(py, dependencies, env)
        missing: list[str] = []
        for import_name, pip_requirement in dependencies.items():
            if availability[import_name]:
                print(f"[ok] {pip_requirement}")
            else:
                print(f"[miss] {pip_requirement}")
                missing.append(pip_requirement)
        if missing:
            if args.check:
                if args.feature == "narration-asr":
                    cache_root, receipt_path = narration_asr_paths(args.config)
                    _print_narration_asr_status(
                        "BLOCKED",
                        cache_root,
                        receipt_path,
                        reason="dependency_missing",
                    )
                print(f"[err] 缺少 {len(missing)} 个依赖: {', '.join(missing)}", file=sys.stderr)
                return 1
            if not install(py, missing, env):
                if args.feature == "narration-asr":
                    cache_root, receipt_path = narration_asr_paths(args.config)
                    _print_narration_asr_status(
                        "BLOCKED",
                        cache_root,
                        receipt_path,
                        reason="dependency_install_failed",
                    )
                return 1

        if args.feature == "narration-asr":
            try:
                runtime_probe = probe_narration_asr_runtime(py, env)
            except RuntimeError as exc:
                cache_root, receipt_path = narration_asr_paths(args.config)
                _print_narration_asr_status(
                    "BLOCKED",
                    cache_root,
                    receipt_path,
                    reason="runtime_probe_failed",
                )
                print(f"[err] {exc}", file=sys.stderr)
                return 1
            print(
                "[ok] narration-asr torchaudio/WavFrontend fbank "
                f"({runtime_probe['torchVersion']}/{runtime_probe['torchaudioVersion']})"
            )

        if args.feature == "narration-asr":
            cache_root, receipt_path = narration_asr_paths(args.config)
            if args.check:
                try:
                    model_paths = load_narration_asr_model_paths(
                        args.config,
                        receipt_path=receipt_path,
                    )
                except RuntimeError as exc:
                    _print_narration_asr_status(
                        "BLOCKED",
                        cache_root,
                        receipt_path,
                        reason=str(exc),
                    )
                    print(f"[err] {exc}", file=sys.stderr)
                    return 1
            else:
                model_paths = prepare_narration_asr_models(
                    py,
                    cache_root,
                    receipt_path,
                    env,
                    config_path=args.config,
                )
            _print_narration_asr_status(
                "READY",
                cache_root,
                receipt_path,
                model_paths=model_paths,
            )
    except WorkspaceError as exc:
        print(f"[err] {exc}", file=sys.stderr)
        return 2
    except NarrationAsrBlockedError as exc:
        try:
            cache_root, receipt_path = narration_asr_paths(args.config)
            _print_narration_asr_status(
                "BLOCKED",
                cache_root,
                receipt_path,
                reason=str(exc),
            )
        except (WorkspaceError, RuntimeError, OSError):
            pass
        print(f"[blocked] {exc}", file=sys.stderr)
        return 1
    except (RuntimeError, OSError) as exc:
        print(f"[err] {exc}", file=sys.stderr)
        return 1

    print(f"ENV_PY={py}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
