#!/usr/bin/env python3
"""Approve the exact current annotation, preview, and timing review identity."""
from __future__ import annotations

import argparse
import json
from typing import Sequence

try:
    from .annotation_review import (
        AnnotationReviewApprovalRequired,
        AnnotationReviewError,
        approve_current_annotation_review,
    )
    from .project_workspace import ProjectValidationError, WorkspaceError
    from .render_timing import RenderTimingError
except ImportError:  # pragma: no cover - direct script execution
    from annotation_review import (
        AnnotationReviewApprovalRequired,
        AnnotationReviewError,
        approve_current_annotation_review,
    )
    from project_workspace import ProjectValidationError, WorkspaceError
    from render_timing import RenderTimingError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="批准仍 current 的标注、区域预览与时序联合审阅")
    parser.add_argument("--project", required=True)
    parser.add_argument("--identity-hash", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        approval = approve_current_annotation_review(args.project, args.identity_hash)
    except (ProjectValidationError, WorkspaceError) as exc:
        print(f"ERROR={exc}")
        return 2
    except (
        AnnotationReviewApprovalRequired,
        AnnotationReviewError,
        RenderTimingError,
        OSError,
    ) as exc:
        print(f"ERROR={exc}")
        return 5
    print(json.dumps({"ok": True, "annotationReviewApproval": approval}, ensure_ascii=False, sort_keys=True))
    print(f"ANNOTATION_REVIEW_APPROVED={approval['identityHash']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
