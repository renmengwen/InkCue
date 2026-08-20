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

from project_workspace import WorkspaceError, load_workspace_config


BASE_DEPS: dict[str, str] = {
    "cv2": "opencv-python",
    "numpy": "numpy",
    "av": "av",
    "PIL": "Pillow",
}
EDGE_TTS_VERSION = "7.2.8"
FEATURE_DEPS: dict[str, dict[str, str]] = {
    "edge-tts": {"edge_tts": f"edge-tts=={EDGE_TTS_VERSION}"},
}
# 保留旧名称，供只关心基础渲染依赖的调用方使用。
DEPS = BASE_DEPS

_PROBE_RESULT_PREFIX = "ENV_DEPENDENCY_PROBE="
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


def runtime_paths(config_path: str | Path | None = None) -> tuple[Path, Path, Path]:
    workspace = load_workspace_config(config_path)
    runtime = workspace.runtime_dir
    return runtime / ".venv", runtime / "cache" / "pip", runtime / "tmp"


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


def _parse_arguments(arguments: list[str]) -> tuple[bool, str | None]:
    parser = argparse.ArgumentParser(
        description="准备基础白板渲染环境；语音 provider 依赖仅在显式 feature 下安装。"
    )
    parser.add_argument("--check", action="store_true", help="只检查，不安装缺失依赖")
    parser.add_argument(
        "--feature",
        choices=sorted(FEATURE_DEPS),
        help="可选能力；Edge TTS 需要显式 edge-tts，MiniMax 使用 Python 标准库",
    )
    parsed = parser.parse_args(arguments)
    return parsed.check, parsed.feature


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    try:
        check_only, feature = _parse_arguments(arguments)
    except SystemExit as exc:
        # argparse 已输出具体参数错误；作为可调用函数时仍返回统一退出码。
        if exc.code == 0:
            return 0
        return 2
    try:
        py, pip_cache, runtime_tmp = ensure_venv(check_only)
        # 即使复用既有环境，也要建立并显式使用固定缓存和临时目录。
        pip_cache.mkdir(parents=True, exist_ok=True)
        runtime_tmp.mkdir(parents=True, exist_ok=True)
        env = subprocess_environment(pip_cache, runtime_tmp)
        dependencies = dict(BASE_DEPS)
        if feature is not None:
            dependencies.update(FEATURE_DEPS[feature])
        availability = probe_dependencies(py, dependencies, env)
        missing: list[str] = []
        for import_name, pip_requirement in dependencies.items():
            if availability[import_name]:
                print(f"[ok] {pip_requirement}")
            else:
                print(f"[miss] {pip_requirement}")
                missing.append(pip_requirement)
        if missing:
            if check_only:
                print(f"[err] 缺少 {len(missing)} 个依赖: {', '.join(missing)}", file=sys.stderr)
                return 1
            if not install(py, missing, env):
                return 1
    except WorkspaceError as exc:
        print(f"[err] {exc}", file=sys.stderr)
        return 2
    except (RuntimeError, OSError) as exc:
        print(f"[err] {exc}", file=sys.stderr)
        return 1

    print(f"ENV_PY={py}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
