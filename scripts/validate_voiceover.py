#!/usr/bin/env python3
"""验证 current Edge TTS 旁白；只允许刷新技术 receipt，绝不写人工批准。"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

try:
    from .audio_normalization import AudioNormalizationError
    from .generate_voiceover import ApprovalGateError, VoiceoverStateError, validate_current_voiceover
    from .project_workspace import ExecutionConcurrency, ProjectValidationError, WorkspaceConfig, load_project, load_workspace_config
    from .voiceover import VoiceoverValidationError
except ImportError:  # pragma: no cover - direct script execution
    from audio_normalization import AudioNormalizationError
    from generate_voiceover import ApprovalGateError, VoiceoverStateError, validate_current_voiceover
    from project_workspace import ExecutionConcurrency, ProjectValidationError, WorkspaceConfig, load_project, load_workspace_config
    from voiceover import VoiceoverValidationError


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="只读验证 canonical WAV、timeline 与 narration SRT"
    )
    parser.add_argument("--project", required=True, type=Path)
    parser.add_argument("--force-deep", action="store_true", help="兼容入口：强制按 current 字节重新执行旁白技术校验")
    return parser


def main(
    argv: list[str] | None = None,
    *,
    workspace_config: WorkspaceConfig | None = None,
) -> int:
    args = _parser().parse_args(argv)
    try:
        execution = workspace_config
        if execution is None and argv is None:
            execution = load_workspace_config()
        concurrency = execution.concurrency if execution is not None else ExecutionConcurrency()
        result = validate_current_voiceover(
            load_project(args.project, allow_pending_audio_timeline=True),
            require_full=True,
            voice_validation_concurrency=concurrency.for_stage("voiceValidation"),
            force_deep=args.force_deep,
            persist_deep=True,
        )
    except ApprovalGateError as exc:
        print(f"[stale] {exc}", file=sys.stderr)
        return 5
    except AudioNormalizationError as exc:
        print(f"[media] {exc}", file=sys.stderr)
        return 4
    except (VoiceoverStateError, VoiceoverValidationError, ProjectValidationError, ValueError, OSError) as exc:
        print(f"[err] {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    print("VOICEOVER_VALIDATED=1")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
