#!/usr/bin/env python3
"""用户完整确认 current 场景 bundle 后持久化一次性批量批准。"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from typing import Any, Sequence

try:
    from .media_validation import MediaValidationError
    from .project_workspace import (
        ProjectValidationError,
        ProjectWorkspace,
        WorkspaceError,
        write_json_atomic,
    )
    from .scene_review import (
        SCENE_REVIEW_CONTRACT_VERSION,
        SceneReviewGateError,
        SceneReviewStaleError,
        build_scene_review_bundle,
        load_render_manifest,
    )
except ImportError:  # pragma: no cover - direct script execution
    from media_validation import MediaValidationError
    from project_workspace import (
        ProjectValidationError,
        ProjectWorkspace,
        WorkspaceError,
        write_json_atomic,
    )
    from scene_review import (
        SCENE_REVIEW_CONTRACT_VERSION,
        SceneReviewGateError,
        SceneReviewStaleError,
        build_scene_review_bundle,
        load_render_manifest,
    )


def approve_scene_review(project_root: str, identity_hash: str) -> dict[str, Any]:
    if not isinstance(identity_hash, str) or len(identity_hash) != 64:
        raise SceneReviewGateError("--identity-hash 必须是 64 位 current scene review identity")
    workspace = ProjectWorkspace.from_config()
    project = workspace.load_project(project_root)
    bundle = build_scene_review_bundle(project)
    if identity_hash != bundle["identityHash"]:
        raise SceneReviewGateError("提交的 scene review identity 与 current bundle 不一致")
    manifest_path, manifest = load_render_manifest(project)
    approval = {
        "approved": True,
        "contractVersion": SCENE_REVIEW_CONTRACT_VERSION,
        "identityHash": identity_hash,
        "sceneCount": len(bundle["scenes"]),
        "approvedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    manifest["sceneReviewApproval"] = approval
    write_json_atomic(manifest_path, manifest)
    return {
        "ok": True,
        "projectId": project.project_id,
        "sceneReviewIdentityHash": identity_hash,
        "sceneReviewApproval": approval,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="批准仍 current 的正式场景批量 review bundle")
    parser.add_argument("--project", required=True, help="项目根目录")
    parser.add_argument("--identity-hash", required=True, help="用户刚完整确认的 current bundle identity")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = approve_scene_review(args.project, args.identity_hash)
    except (WorkspaceError, ProjectValidationError) as exc:
        print(f"ERROR={exc}", file=sys.stderr)
        return 2
    except (SceneReviewGateError, SceneReviewStaleError) as exc:
        print(f"ERROR={exc}", file=sys.stderr)
        return 5
    except (MediaValidationError, OSError, RuntimeError, KeyError, TypeError, json.JSONDecodeError) as exc:
        print(f"ERROR={exc}", file=sys.stderr)
        return 4
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    print(f"SCENE_REVIEW_APPROVED={result['sceneReviewIdentityHash']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
