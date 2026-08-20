from __future__ import annotations

import copy
import hashlib
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = SKILL_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import validation_receipts as receipts  # noqa: E402


VALIDATED_AT = "2026-08-20T01:00:00Z"
EXPIRES_AT = "2026-08-20T01:05:00Z"
CURRENT_NOW = "2026-08-20T01:01:00Z"
VALIDATOR_CONTRACT = "png-validator-v2"


class ValidationReceiptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.candidate = self.root / "candidate.png"
        self.candidate.write_bytes(b"canonical-png-candidate")
        self.candidate_sha = hashlib.sha256(self.candidate.read_bytes()).hexdigest()

    def receipt(self, **changes: object) -> dict[str, object]:
        values: dict[str, object] = {
            "candidate_sha256": self.candidate_sha,
            "candidate_bytes": self.candidate.stat().st_size,
            "decoded": True,
            "format": "PNG",
            "validator_contract": VALIDATOR_CONTRACT,
            "validated_at": VALIDATED_AT,
            "expires_at": EXPIRES_AT,
            "evidence": {"width": 1920, "height": 1080, "fullDecode": True},
        }
        values.update(changes)
        return receipts.build_candidate_receipt(**values)

    def test_build_is_canonical_and_receipt_sha_covers_evidence(self) -> None:
        first = self.receipt()
        second = self.receipt(
            evidence={"fullDecode": True, "height": 1080, "width": 1920}
        )
        self.assertEqual(first, second)
        self.assertEqual(first["receiptSha256"], receipts.receipt_sha256(first))

        changed = copy.deepcopy(first)
        changed["evidence"]["width"] = 1280  # type: ignore[index]
        with self.assertRaisesRegex(receipts.ReceiptValidationError, "receipt SHA-256"):
            receipts.validate_candidate_receipt(
                changed,
                expected_validator_contract=VALIDATOR_CONTRACT,
                now=CURRENT_NOW,
                require_expiry=True,
            )

    def test_same_current_candidate_binds_without_deep_validation(self) -> None:
        receipt = self.receipt()
        bound = receipts.bind_candidate_receipt(
            self.candidate,
            receipt,
            expected_format="PNG",
            expected_validator_contract=VALIDATOR_CONTRACT,
            now=CURRENT_NOW,
            require_expiry=True,
        )
        self.assertEqual(bound, receipt)
        self.assertTrue(bound["decoded"])

    def test_changed_bytes_sha_or_validator_contract_fail_closed(self) -> None:
        receipt = self.receipt()
        self.candidate.write_bytes(b"changed")
        with self.assertRaisesRegex(receipts.ReceiptValidationError, "binding 不一致"):
            receipts.bind_candidate_receipt(
                self.candidate,
                receipt,
                expected_format="PNG",
                expected_validator_contract=VALIDATOR_CONTRACT,
                now=CURRENT_NOW,
            )

        with self.assertRaisesRegex(receipts.ReceiptValidationError, "validator contract.*stale"):
            receipts.validate_candidate_receipt(
                receipt,
                expected_validator_contract="png-validator-v3",
                now=CURRENT_NOW,
            )

    def test_expired_or_missing_required_expiry_fails_closed(self) -> None:
        with self.assertRaisesRegex(receipts.ReceiptValidationError, "已过期"):
            receipts.validate_candidate_receipt(
                self.receipt(),
                expected_validator_contract=VALIDATOR_CONTRACT,
                now=EXPIRES_AT,
                require_expiry=True,
            )

        no_expiry = self.receipt(expires_at=None)
        with self.assertRaisesRegex(receipts.ReceiptValidationError, "expiresAt 缺失"):
            receipts.validate_candidate_receipt(
                no_expiry,
                expected_validator_contract=VALIDATOR_CONTRACT,
                now=CURRENT_NOW,
                require_expiry=True,
            )

    def test_decoded_false_never_binds(self) -> None:
        receipt = self.receipt(decoded=False)
        with self.assertRaisesRegex(receipts.ReceiptValidationError, "完整解码 PASS"):
            receipts.bind_candidate_receipt(
                self.candidate,
                receipt,
                expected_format="PNG",
                expected_validator_contract=VALIDATOR_CONTRACT,
                now=CURRENT_NOW,
            )

    def test_legacy_receipt_can_be_read_but_never_current(self) -> None:
        legacy = {
            "validatorReceipt": {
                "contractVersion": "canonical-png-validator-receipt-v1",
                "candidateSha256": self.candidate_sha,
                "candidateBytes": self.candidate.stat().st_size,
                "decoded": True,
            }
        }
        view = receipts.read_candidate_receipt(legacy)
        self.assertEqual(view.source, "validatorReceipt")
        self.assertFalse(view.current_contract)
        with self.assertRaisesRegex(receipts.ReceiptValidationError, "schema 不一致"):
            receipts.bind_candidate_receipt(
                self.candidate,
                view.receipt,
                expected_format="PNG",
                expected_validator_contract=VALIDATOR_CONTRACT,
                now=CURRENT_NOW,
            )

        nested = receipts.read_candidate_receipt(
            {"validation": {"deepReceipt": legacy["validatorReceipt"]}}
        )
        self.assertEqual(nested.source, "validation.deepReceipt")
        self.assertFalse(nested.current_contract)

    def test_current_bindings_require_exact_nested_match(self) -> None:
        frozen = {
            "projectId": "project-1",
            "annotationSha256": "a" * 64,
            "artifacts": [{"sceneId": "scene-01", "sha256": "b" * 64}],
        }
        receipts.require_current_bindings(frozen, copy.deepcopy(frozen))
        changed = copy.deepcopy(frozen)
        changed["artifacts"][0]["sha256"] = "c" * 64  # type: ignore[index]
        with self.assertRaisesRegex(receipts.ReceiptValidationError, "current bindings 不一致"):
            receipts.require_current_bindings(frozen, changed)

    def test_receipt_window_rejects_naive_or_reverse_timestamps(self) -> None:
        with self.assertRaisesRegex(receipts.ReceiptValidationError, "必须带时区"):
            receipts.validate_receipt_window(
                created_at=datetime(2026, 8, 20, 1, 0, 0),
                expires_at=EXPIRES_AT,
                now=CURRENT_NOW,
            )
        with self.assertRaisesRegex(receipts.ReceiptValidationError, "必须晚于"):
            receipts.validate_receipt_window(
                created_at=VALIDATED_AT,
                expires_at=VALIDATED_AT,
                now=datetime(2026, 8, 20, 1, 0, tzinfo=timezone.utc),
            )
        with self.assertRaisesRegex(receipts.ReceiptValidationError, "尚未生效"):
            receipts.validate_receipt_window(
                created_at=VALIDATED_AT,
                expires_at=EXPIRES_AT,
                now="2026-08-20T00:59:59Z",
            )


if __name__ == "__main__":
    unittest.main()
