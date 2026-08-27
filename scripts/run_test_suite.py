from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


SKILL_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TIMEOUT_SECONDS = 90


@dataclass(frozen=True)
class TestInvocation:
    label: str
    unittest_args: tuple[str, ...]


# Keep the default suite explicit and reviewable. New test modules do not join
# the Codex fast path until they have been classified and added here.
FAST_TEST_MODULES = (
    "tests.test_agent_task_contract",
    "tests.test_annotation_agent_orchestration",
    "tests.test_annotation_batch",
    "tests.test_annotation_contract",
    "tests.test_annotation_dispatch",
    "tests.test_annotation_prepare_cli",
    "tests.test_annotation_preview_batch",
    "tests.test_annotation_review",
    "tests.test_bounded_execution",
    "tests.test_ci_status_contract",
    "tests.test_content_revision_flow",
    "tests.test_content_source",
    "tests.test_cover_frame",
    "tests.test_cover_generation",
    "tests.test_cover_review",
    "tests.test_directional_ink_order",
    "tests.test_documentation_contract",
    "tests.test_doubao_adapter",
    "tests.test_edge_tts_adapter",
    "tests.test_final_delivery_runner",
    "tests.test_formal_validation_context",
    "tests.test_image_generation_cli",
    "tests.test_image_generation",
    "tests.test_initial_approval",
    "tests.test_initial_approval_options",
    "tests.test_media_validation_receipts",
    "tests.test_minimax_adapter",
    "tests.test_phase4_runner_contract",
    "tests.test_prepare_draft_agent_task",
    "tests.test_prepare_env_funasr",
    "tests.test_prepare_source_cli",
    "tests.test_project_workspace",
    "tests.test_reference_audio_alignment",
    "tests.test_render_timing",
    "tests.test_scene_render_benchmark",
    "tests.test_scene_render_concurrency",
    "tests.test_scene_render_metrics",
    "tests.test_scene_render_safety_matrix",
    "tests.test_scene_review",
    "tests.test_srt_timeline",
    "tests.test_stale_identity_matrix",
    "tests.test_subtitles",
    "tests.test_test_suite_runner",
    "tests.test_transcribe_narration",
    "tests.test_validate_content_draft_cli",
    "tests.test_validation_receipts",
    "tests.test_visual_review_prepare_cli",
    "tests.test_voice_provider_safety",
    "tests.test_voiceover_cli",
    "tests.test_voiceover_timing",
    "tests.test_voiceover",
)

FAST_INVOCATIONS = tuple(
    TestInvocation(
        label=f"fast:{module.removeprefix('tests.')}",
        unittest_args=(module,),
    )
    for module in FAST_TEST_MODULES
)


def _terminate_process_tree(process: subprocess.Popen[object]) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
    else:
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
            process.wait(timeout=3)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            except ProcessLookupError:
                pass
    if process.poll() is None:
        process.kill()
        process.wait(timeout=5)


def _unittest_command(invocation: TestInvocation) -> list[str]:
    return [sys.executable, "-m", "unittest", "-q", *invocation.unittest_args]


def _bounded_child_output(output: str, *, max_lines: int = 20, max_chars: int = 6000) -> str:
    lines = output.splitlines()
    bounded = "\n".join(lines[-max_lines:])
    if len(bounded) > max_chars:
        bounded = bounded[-max_chars:]
    if len(lines) > max_lines or len(output) > len(bounded):
        return f"[仅保留失败输出末尾]\n{bounded}"
    return bounded


def _console_safe_text(value: str, encoding: str | None) -> str:
    """把任意子进程文本降级为当前控制台一定可写出的字符串。"""

    target_encoding = encoding or "utf-8"
    try:
        return value.encode(target_encoding, errors="backslashreplace").decode(
            target_encoding,
            errors="strict",
        )
    except LookupError:
        return value.encode("ascii", errors="backslashreplace").decode("ascii")


def _run_child(invocation: TestInvocation, timeout_seconds: int) -> int:
    command = _unittest_command(invocation)
    print(f"[RUN] {invocation.label} timeout={timeout_seconds}s", flush=True)
    child_env = os.environ.copy()
    # stdout/stderr 都接到 PIPE；显式固定子解释器输出为 UTF-8，避免 Windows
    # 本地代码页字节被父进程按 UTF-8 解码后变成替换字符。
    child_env["PYTHONIOENCODING"] = "utf-8"
    popen_kwargs: dict[str, object] = {
        "cwd": SKILL_ROOT,
        "env": child_env,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.STDOUT,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
    }
    if os.name == "nt":
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        popen_kwargs["start_new_session"] = True
    process = subprocess.Popen(command, **popen_kwargs)
    try:
        output, _ = process.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        _terminate_process_tree(process)
        print(f"[TIMEOUT] {invocation.label} 超过 {timeout_seconds}s，已结束子进程树", flush=True)
        return 124
    except KeyboardInterrupt:
        _terminate_process_tree(process)
        raise
    return_code = process.returncode if process.returncode is not None else 1
    if return_code != 0:
        bounded_output = _bounded_child_output(output or "")
        if bounded_output:
            print(
                _console_safe_text(bounded_output, getattr(sys.stdout, "encoding", None)),
                flush=True,
            )
    return return_code


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="串行运行 fast 测试套件")
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=DEFAULT_TIMEOUT_SECONDS,
        help="每个独立 Python 子进程的超时秒数（默认 90）",
    )
    parser.add_argument("--list", action="store_true", help="仅列出将执行的测试，不启动子进程")
    args = parser.parse_args(argv)
    if args.timeout_seconds < 1:
        parser.error("--timeout-seconds 必须大于 0")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if args.list:
        for invocation in FAST_INVOCATIONS:
            print(f"{invocation.label}: {' '.join(invocation.unittest_args)}")
        return 0

    for invocation in FAST_INVOCATIONS:
        return_code = _run_child(invocation, args.timeout_seconds)
        if return_code != 0:
            print(
                f"[FAIL] {invocation.label}={return_code}；已停止，未启动后续测试",
                flush=True,
            )
            return 1
    print("[PASS] suite=fast", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
