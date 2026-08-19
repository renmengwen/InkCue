from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import sys

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from annotation_dispatch import (  # noqa: E402
    ArtifactFirstWatchdog,
    CandidateObservation,
    DispatchAudit,
    build_dispatch_manifest,
    observe_candidate,
)


class AnnotationDispatchTests(unittest.TestCase):
    def test_observe_candidate_is_fail_closed_and_records_sha(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            candidate = root / "candidate.annotation.json"
            candidate.write_text(json.dumps({"elements": []}), encoding="utf-8")
            observation = observe_candidate("ann-scene-01", candidate, attempt_dir=root)
            self.assertEqual(observation.status, "ready")
            self.assertEqual(len(observation.sha256 or ""), 64)
            candidate.write_bytes(b"not-json")
            self.assertEqual(
                observe_candidate("ann-scene-01", candidate, attempt_dir=root).status,
                "invalid",
            )

    def test_watchdog_finalizes_after_grace_without_child_final(self) -> None:
        now = [0.0]
        watchdog = ArtifactFirstWatchdog(tail_grace_seconds=5, clock=lambda: now[0])
        observation = CandidateObservation("ann-1", "ready", "candidate.json", sha256="a", bytes=1, observed_at="t")
        self.assertEqual(watchdog.observe(observation, child_running=True), "candidate_ready")
        self.assertFalse(watchdog.should_finalize("ann-1", child_running=True))
        now[0] = 5
        self.assertTrue(watchdog.should_finalize("ann-1", child_running=True))
        self.assertEqual(watchdog.observe(observation, child_running=True), "finalize")

    def test_manifest_uses_ascii_lifecycle_flags(self) -> None:
        manifest = build_dispatch_manifest(
            run_id="run-1",
            candidate_root=Path(".work/run-1/agent-tasks"),
            tasks=[{"taskId": "ann-1"}],
            dispatch_units=[{"dispatchUnitId": "unit-1"}],
            effective_concurrency=1,
        )
        self.assertEqual(manifest["protocol"], "annotation-artifact-first-v1")
        self.assertFalse(manifest["flags"]["WRITE_RESULT_JSON"])
        self.assertTrue(manifest["flags"]["STOP_AFTER_CANDIDATE_READY"])


if __name__ == "__main__":
    unittest.main()
