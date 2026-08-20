#!/usr/bin/env python3
"""Candidate validation receipt 的规范化、时效与 current binding 校验。

本模块只描述技术验证证据。它不写正式 manifest、identity、stale、checkpoint
或人工批准，也不能把旧 validator 的证据提升为当前 contract 的 PASS。
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping


CANDIDATE_RECEIPT_CONTRACT_VERSION = "candidate-validation-receipt-v1"

_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_FORMAT_RE = re.compile(r"[A-Z][A-Z0-9._+-]{0,31}")
_RECEIPT_KEYS = frozenset(
    {
        "contractVersion",
        "candidateSha256",
        "candidateBytes",
        "decoded",
        "format",
        "validatorContract",
        "validatedAt",
        "expiresAt",
        "evidence",
        "receiptSha256",
    }
)
_WRAPPER_FIELDS = (
    "candidateReceipt",
    "validationReceipt",
    "validatorReceipt",
    "deepReceipt",
)


class ReceiptValidationError(ValueError):
    """Receipt 缺失、stale、被篡改或与 current candidate 不一致。"""


@dataclass(frozen=True)
class CandidateReceiptRead:
    """兼容读取结果；只有 ``current_contract`` 为真才可能用于新 binding。"""

    receipt: dict[str, Any]
    current_contract: bool
    source: str


def canonical_json_bytes(value: Any) -> bytes:
    """返回本项目 receipt 使用的确定性 UTF-8 JSON。"""
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ReceiptValidationError(f"receipt 包含不可规范化的 JSON 值: {exc}") from exc


def receipt_sha256(receipt: Mapping[str, Any]) -> str:
    """计算 receipt 内容 SHA；顶层自引用字段 ``receiptSha256`` 不参与。"""
    if not isinstance(receipt, Mapping):
        raise ReceiptValidationError("receipt 必须为对象")
    payload = copy.deepcopy(dict(receipt))
    payload.pop("receiptSha256", None)
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc_datetime(value: str | datetime, *, label: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value:
        text = value[:-1] + "+00:00" if value.endswith("Z") else value
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ReceiptValidationError(f"{label} 必须是有效 ISO-8601 时间") from exc
    else:
        raise ReceiptValidationError(f"{label} 必须是带时区的 ISO-8601 时间")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ReceiptValidationError(f"{label} 必须带时区")
    return parsed.astimezone(timezone.utc)


def _isoformat_utc(value: str | datetime, *, label: str) -> str:
    return _as_utc_datetime(value, label=label).isoformat(
        timespec="seconds"
    ).replace("+00:00", "Z")


def validate_receipt_window(
    *,
    created_at: str | datetime,
    expires_at: str | datetime | None,
    now: str | datetime | None = None,
    require_expiry: bool = True,
    label: str = "receipt",
) -> None:
    """校验 receipt 时间窗；边界 ``now == expiresAt`` 视为已过期。"""
    created = _as_utc_datetime(created_at, label=f"{label}.createdAt")
    current = _utc_now() if now is None else _as_utc_datetime(now, label="now")
    if current < created:
        raise ReceiptValidationError(f"{label} 尚未生效")
    if expires_at is None:
        if require_expiry:
            raise ReceiptValidationError(f"{label}.expiresAt 缺失")
        return
    expires = _as_utc_datetime(expires_at, label=f"{label}.expiresAt")
    if expires <= created:
        raise ReceiptValidationError(f"{label}.expiresAt 必须晚于 createdAt")
    if current >= expires:
        raise ReceiptValidationError(f"{label} 已过期")


def require_current_bindings(
    receipt_bindings: Mapping[str, Any],
    current_bindings: Mapping[str, Any],
    *,
    label: str = "receipt",
) -> None:
    """要求 receipt bindings 与 coordinator 现读 current bindings 完全一致。"""
    if not isinstance(receipt_bindings, Mapping):
        raise ReceiptValidationError(f"{label}.bindings 必须为对象")
    if not isinstance(current_bindings, Mapping):
        raise ReceiptValidationError("current bindings 必须为对象")
    if canonical_json_bytes(dict(receipt_bindings)) != canonical_json_bytes(
        dict(current_bindings)
    ):
        raise ReceiptValidationError(f"{label} 与 current bindings 不一致")


def _require_sha256(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise ReceiptValidationError(f"{label} 必须为小写 64 位 SHA-256")
    return value


def _require_candidate_bytes(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ReceiptValidationError("candidateBytes 必须为正整数")
    return value


def _require_format(value: Any, *, label: str = "format") -> str:
    if not isinstance(value, str) or _FORMAT_RE.fullmatch(value) is None:
        raise ReceiptValidationError(f"{label} 必须为规范化的大写格式名")
    return value


def _require_validator_contract(value: Any, *, label: str = "validatorContract") -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 200:
        raise ReceiptValidationError(f"{label} 必须为 1 到 200 字符的非空字符串")
    if value != value.strip():
        raise ReceiptValidationError(f"{label} 不得包含首尾空白")
    return value


def build_candidate_receipt(
    *,
    candidate_sha256: str,
    candidate_bytes: int,
    decoded: bool,
    format: str,
    validator_contract: str,
    validated_at: str | datetime | None = None,
    expires_at: str | datetime | None = None,
    ttl_seconds: int | None = None,
    evidence: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """构造并自签 canonical candidate receipt。

    ``decoded=False`` 可以记录失败前的诊断状态，但这样的 receipt 不能通过
    :func:`validate_candidate_receipt` 或 :func:`bind_candidate_receipt`。
    """
    sha256 = _require_sha256(candidate_sha256, label="candidateSha256")
    byte_count = _require_candidate_bytes(candidate_bytes)
    if not isinstance(decoded, bool):
        raise ReceiptValidationError("decoded 必须为布尔值")
    format_name = _require_format(format)
    contract = _require_validator_contract(validator_contract)

    validated_value = _utc_now() if validated_at is None else validated_at
    validated_dt = _as_utc_datetime(validated_value, label="validatedAt")
    if ttl_seconds is not None:
        if expires_at is not None:
            raise ReceiptValidationError("expires_at 与 ttl_seconds 不能同时提供")
        if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int) or ttl_seconds <= 0:
            raise ReceiptValidationError("ttl_seconds 必须为正整数")
        expires_at = validated_dt + timedelta(seconds=ttl_seconds)
    expires_text = (
        None if expires_at is None else _isoformat_utc(expires_at, label="expiresAt")
    )
    if expires_text is not None:
        validate_receipt_window(
            created_at=validated_dt,
            expires_at=expires_text,
            now=validated_dt,
            label="candidate receipt",
        )
    evidence_value = {} if evidence is None else copy.deepcopy(dict(evidence))
    canonical_json_bytes(evidence_value)
    receipt: dict[str, Any] = {
        "contractVersion": CANDIDATE_RECEIPT_CONTRACT_VERSION,
        "candidateSha256": sha256,
        "candidateBytes": byte_count,
        "decoded": decoded,
        "format": format_name,
        "validatorContract": contract,
        "validatedAt": _isoformat_utc(validated_dt, label="validatedAt"),
        "expiresAt": expires_text,
        "evidence": evidence_value,
    }
    receipt["receiptSha256"] = receipt_sha256(receipt)
    return receipt


def validate_candidate_receipt(
    receipt: Mapping[str, Any],
    *,
    expected_candidate_sha256: str | None = None,
    expected_candidate_bytes: int | None = None,
    expected_format: str | None = None,
    expected_validator_contract: str,
    now: str | datetime | None = None,
    require_expiry: bool = False,
) -> dict[str, Any]:
    """严格校验 current contract receipt；旧证据和 binding 差异均 fail-closed。"""
    if not isinstance(receipt, Mapping):
        raise ReceiptValidationError("candidate receipt 必须为对象")
    candidate = copy.deepcopy(dict(receipt))
    if set(candidate) != _RECEIPT_KEYS:
        missing = sorted(_RECEIPT_KEYS - set(candidate))
        unexpected = sorted(set(candidate) - _RECEIPT_KEYS)
        raise ReceiptValidationError(
            f"candidate receipt schema 不一致: missing={missing}, unexpected={unexpected}"
        )
    if candidate.get("contractVersion") != CANDIDATE_RECEIPT_CONTRACT_VERSION:
        raise ReceiptValidationError("candidate receipt contract 已 stale 或属于旧格式")
    actual_sha = _require_sha256(candidate.get("candidateSha256"), label="candidateSha256")
    actual_bytes = _require_candidate_bytes(candidate.get("candidateBytes"))
    if candidate.get("decoded") is not True:
        raise ReceiptValidationError("candidate receipt 缺少完整解码 PASS")
    actual_format = _require_format(candidate.get("format"))
    actual_contract = _require_validator_contract(candidate.get("validatorContract"))
    expected_contract = _require_validator_contract(
        expected_validator_contract, label="expected_validator_contract"
    )
    if actual_contract != expected_contract:
        raise ReceiptValidationError("validator contract 已 stale")
    if expected_candidate_sha256 is not None:
        expected_sha = _require_sha256(
            expected_candidate_sha256, label="expected_candidate_sha256"
        )
        if actual_sha != expected_sha:
            raise ReceiptValidationError("candidate SHA-256 与 receipt binding 不一致")
    if expected_candidate_bytes is not None:
        expected_bytes = _require_candidate_bytes(expected_candidate_bytes)
        if actual_bytes != expected_bytes:
            raise ReceiptValidationError("candidate bytes 与 receipt binding 不一致")
    if expected_format is not None and actual_format != _require_format(
        expected_format, label="expected_format"
    ):
        raise ReceiptValidationError("candidate format 与 receipt binding 不一致")
    if not isinstance(candidate.get("evidence"), Mapping):
        raise ReceiptValidationError("candidate receipt.evidence 必须为对象")
    canonical_json_bytes(candidate["evidence"])
    _require_sha256(candidate.get("receiptSha256"), label="receiptSha256")
    if candidate["receiptSha256"] != receipt_sha256(candidate):
        raise ReceiptValidationError("candidate receipt SHA-256 校验失败")
    validate_receipt_window(
        created_at=candidate.get("validatedAt"),
        expires_at=candidate.get("expiresAt"),
        now=now,
        require_expiry=require_expiry,
        label="candidate receipt",
    )
    return candidate


def _file_binding(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        before = os.fstat(handle.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size <= 0:
            raise ReceiptValidationError(f"candidate 不是非空普通文件: {path}")
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
        after = os.fstat(handle.fileno())
    stable_fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns")
    if any(getattr(before, field) != getattr(after, field) for field in stable_fields):
        raise ReceiptValidationError("candidate 在 binding 读取期间发生变化")
    current = path.stat()
    if any(getattr(after, field) != getattr(current, field) for field in stable_fields):
        raise ReceiptValidationError("candidate 在 binding 复核前发生变化")
    return digest.hexdigest(), after.st_size


def bind_candidate_receipt(
    path: str | Path,
    receipt: Mapping[str, Any],
    *,
    expected_format: str,
    expected_validator_contract: str,
    now: str | datetime | None = None,
    require_expiry: bool = False,
) -> dict[str, Any]:
    """复核 current 文件的 SHA/bytes 与 receipt；不执行也不回退到 deep。"""
    candidate = Path(path)
    if candidate.is_symlink() or not candidate.is_file():
        raise ReceiptValidationError(f"candidate 不是普通文件: {candidate}")
    sha256, byte_count = _file_binding(candidate)
    return validate_candidate_receipt(
        receipt,
        expected_candidate_sha256=sha256,
        expected_candidate_bytes=byte_count,
        expected_format=expected_format,
        expected_validator_contract=expected_validator_contract,
        now=now,
        require_expiry=require_expiry,
    )


def read_candidate_receipt(value: Mapping[str, Any]) -> CandidateReceiptRead:
    """兼容读取 direct/wrapped receipt，但不把旧 contract 标为 current。"""
    if not isinstance(value, Mapping):
        raise ReceiptValidationError("receipt evidence 必须为对象")
    raw: Mapping[str, Any] = value
    source = "direct"
    containers: list[tuple[str, Mapping[str, Any]]] = [("", value)]
    validation = value.get("validation")
    if isinstance(validation, Mapping):
        containers.append(("validation.", validation))
    for prefix, container in containers:
        for field in _WRAPPER_FIELDS:
            nested = container.get(field)
            if isinstance(nested, Mapping):
                raw = nested
                source = f"{prefix}{field}"
                break
        if raw is not value:
            break
    receipt = copy.deepcopy(dict(raw))
    return CandidateReceiptRead(
        receipt=receipt,
        current_contract=(
            receipt.get("contractVersion") == CANDIDATE_RECEIPT_CONTRACT_VERSION
        ),
        source=source,
    )


__all__ = [
    "CANDIDATE_RECEIPT_CONTRACT_VERSION",
    "CandidateReceiptRead",
    "ReceiptValidationError",
    "bind_candidate_receipt",
    "build_candidate_receipt",
    "canonical_json_bytes",
    "read_candidate_receipt",
    "receipt_sha256",
    "require_current_bindings",
    "validate_candidate_receipt",
    "validate_receipt_window",
]
