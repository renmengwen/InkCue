"""Shared process-boundary helpers for command-line entry points."""
from __future__ import annotations

import sys
from typing import TextIO


def _configure_stream(stream: TextIO | None) -> None:
    """Prefer UTF-8 for a real reconfigurable CLI stream.

    Test doubles such as ``io.StringIO`` intentionally do not expose
    ``reconfigure``; leaving them untouched keeps import/capture behaviour
    deterministic.
    """

    reconfigure = getattr(stream, "reconfigure", None)
    if callable(reconfigure):
        reconfigure(encoding="utf-8", errors="strict")


def configure_utf8_stdio() -> None:
    """Make Windows CLI bytes match Codex's UTF-8 output decoder."""

    _configure_stream(sys.stdout)
    _configure_stream(sys.stderr)


__all__ = ["configure_utf8_stdio"]
