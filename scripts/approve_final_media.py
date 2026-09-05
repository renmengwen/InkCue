#!/usr/bin/env python3
"""按项目冻结的审阅模式批准仍 current 的最终成片 identity。"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

try:
    from .audio_normalization import AudioNormalizationError
    from .generate_voiceover import ApprovalGateError, VoiceoverStateError
    from .media_validation import MediaValidationError
    from .project_workspace import ProjectValidationError, write_json_atomic
    from .subtitle_delivery import SubtitleDeliveryError
    from .validate_final_media import FinalMediaStaleError, inspect_project_final_media
    from .voiceover import VoiceoverValidationError
except ImportError:  # pragma: no cover - direct script execution
    from audio_normalization import AudioNormalizationError
    from generate_voiceover import ApprovalGateError, VoiceoverStateError
    from media_validation import MediaValidationError
    from project_workspace import ProjectValidationError, write_json_atomic
    from subtitle_delivery import SubtitleDeliveryError
    from validate_final_media import FinalMediaStaleError, inspect_project_final_media
    from voiceover import VoiceoverValidationError


class FinalApprovalGateError(ValueError):
    """最终成片未验证、stale 或提交 identity 不匹配。"""


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise FinalApprovalGateError(f"{label} 缺失或不是对象")
    return value


def _approval_audit(inspection: Mapping[str, Any]) -> tuple[str, str]:
    project = inspection["project"]
    initial = project.metadata.get("initialApproval")
    autonomous = (
        project.initial_approval_completed
        and project.agent_approval_enabled
        and isinstance(initial, Mapping)
        and initial.get("status") == "approved"
    )
    if not autonomous:
        return "human_full_media_review", "current_final_full_playback"
    if (
        initial.get("approvalBasis") != "user_joint_content_and_plan"
        or initial.get("contentIdentitySha256")
        != project.current_content_identity_sha256
    ):
        raise FinalApprovalGateError("自主最终批准要求 current 初始内容与制作方案授权")
    if project.voiceover_mode == "disabled":
        return "technical_after_initial_approval", "current_final_technical_validation"

    voice_manifest_path = project.path("manifests/voice-manifest.json")
    try:
        voice_manifest = json.loads(voice_manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise FinalApprovalGateError(f"无法读取 current voice manifest: {exc}") from exc
    full_approval = _mapping(voice_manifest.get("fullApproval"), "fullApproval")
    if (
        full_approval.get("approved") is not True
        or full_approval.get("identityHash") != voice_manifest.get("fullIdentityHash")
        or full_approval.get("approvalBasis") != "technical_after_initial_approval"
        or full_approval.get("reviewBasis")
        != "initial_content_plan_authorization_and_current_technical_validation"
    ):
        raise FinalApprovalGateError("自主最终批准要求 current 初始方案与技术 fullApproval 授权链")
    return "technical_after_initial_approval", "current_final_technical_validation"


def approve_final(project_root: str | Path, identity_hash: str) -> dict[str, Any]:
    if not isinstance(identity_hash, str) or len(identity_hash) != 64:
        raise FinalApprovalGateError("--identity-hash 必须是 64 位 current final identity")

    inspection = inspect_project_final_media(project_root)
    current_identity = inspection["finalIdentitySha256"]
    if identity_hash != current_identity:
        raise FinalApprovalGateError("提交的 final identity 与 current final 不一致")
    manifest = inspection["manifest"]
    final = _mapping(manifest.get("final"), "final")
    technical = _mapping(final.get("technicalValidation"), "final.technicalValidation")
    if (
        technical.get("validated") is not True
        or technical.get("fullDecode") is not True
        or technical.get("finalIdentitySha256") != current_identity
        or technical.get("finalMediaSha256") != inspection["finalMedia"]["sha256"]
    ):
        raise FinalApprovalGateError("current final 尚未通过独立技术验证")
    outputs = _mapping(technical.get("outputs"), "final.technicalValidation.outputs")
    if dict(outputs) != inspection["outputs"]:
        raise FinalApprovalGateError("技术验证证据未绑定 current 三层输出")

    approval_basis, review_basis = _approval_audit(inspection)
    approval = {
        "approved": True,
        "identityHash": current_identity,
        "finalMediaSha256": inspection["finalMedia"]["sha256"],
        "approvalBasis": approval_basis,
        "reviewBasis": review_basis,
        "approvedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    manifest["finalApproval"] = approval
    write_json_atomic(inspection["manifestPath"], manifest)
    return {
        "ok": True,
        "voiceoverMode": inspection["project"].voiceover_mode,
        "finalIdentitySha256": current_identity,
        "finalApproval": approval,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="按冻结模式批准已技术验证且仍 current 的最终成片"
    )
    parser.add_argument("--project", required=True, help="项目根目录")
    parser.add_argument("--identity-hash", required=True, help="刚复核的 current final identity")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = approve_final(args.project, args.identity_hash)
    except ProjectValidationError as exc:
        print(f"ERROR={exc}", file=sys.stderr)
        return 2
    except (
        FinalApprovalGateError,
        FinalMediaStaleError,
        ApprovalGateError,
        SubtitleDeliveryError,
    ) as exc:
        print(f"ERROR={exc}", file=sys.stderr)
        return 5
    except (
        AudioNormalizationError,
        MediaValidationError,
        VoiceoverStateError,
        VoiceoverValidationError,
        OSError,
        RuntimeError,
        KeyError,
        TypeError,
        json.JSONDecodeError,
    ) as exc:
        print(f"ERROR={exc}", file=sys.stderr)
        return 4
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    print(f"FINAL_APPROVED={result['finalIdentitySha256']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

