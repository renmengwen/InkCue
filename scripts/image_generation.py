"""OpenAI-compatible image generation primitives.

This module deliberately contains no command-line orchestration.  It owns the
provider boundary, byte decoding, deterministic image normalization, atomic
scene storage, and the append-only run history in the generation manifest.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import math
import os
import random
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

import validation_receipts


PROTOCOL = "openai-images-generations"
CANVAS_SIZE = (1920, 1080)
CANVAS_BACKGROUND = "#F5EBD7"
MIN_SOURCE_DIMENSION = 512
MAX_SOURCE_PIXELS = 40_000_000
MIN_COVERAGE_RATIO = 0.55
MAX_ATTEMPTS = 3
CORE_REQUEST_FIELDS = frozenset({"model", "prompt", "n", "size", "response_format"})
SCENE_STATUSES = frozenset(
    {
        "pending",
        "prepared",
        "requesting",
        "candidate_ready",
        "publishing",
        "validated",
        "failed",
        "unknown_external_outcome",
    }
)
ATTEMPT_STATUSES = frozenset(
    {
        "prepared",
        "requesting",
        "candidate_ready",
        "publishing",
        "validated",
        "failed",
        "unknown_external_outcome",
    }
)
CANDIDATE_RECEIPT_VERSION = "whiteboard-image-candidate-receipt-v1"
IMAGE_VALIDATOR_CONTRACT_VERSION = "whiteboard-image-candidate-validator-v2"


class ImageGenerationError(RuntimeError):
    """Base class carrying a stable failure stage and retry classification."""

    def __init__(self, message: str, *, stage: str, retryable: bool = False) -> None:
        super().__init__(message)
        self.stage = stage
        self.retryable = retryable


class ConfigError(ImageGenerationError):
    def __init__(self, message: str) -> None:
        super().__init__(message, stage="config")


class CredentialSafetyError(ImageGenerationError):
    def __init__(self, message: str) -> None:
        super().__init__(message, stage="credential-safety")


class HttpRequestError(ImageGenerationError):
    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        retryable: bool = False,
        stage: str = "requesting",
    ) -> None:
        super().__init__(message, stage=stage, retryable=retryable)
        self.status = status


class ResponseDecodeError(ImageGenerationError):
    def __init__(self, message: str) -> None:
        super().__init__(message, stage="decoding")


class ImageValidationError(ImageGenerationError):
    def __init__(self, message: str) -> None:
        super().__init__(message, stage="normalizing")


class ManifestError(ImageGenerationError):
    def __init__(self, message: str) -> None:
        super().__init__(message, stage="manifest")


@dataclass(frozen=True)
class ProviderConfig:
    name: str
    protocol: str
    base_url: str
    endpoint: str
    api_key: str = field(repr=False)
    model: str
    size: str
    response_format: str
    request_timeout_seconds: float
    download_timeout_seconds: float
    max_bytes: int
    extra_body: Mapping[str, Any]
    config_path: Path


@dataclass(frozen=True)
class ImagePayload:
    data: bytes
    source: str
    attempts: int


@dataclass(frozen=True)
class ImageMetadata:
    original_width: int
    original_height: int
    canvas_width: int
    canvas_height: int
    scaled_width: int
    scaled_height: int
    offset_x: int
    offset_y: int
    padded: bool
    image_sha256: str

    def to_manifest_fields(self) -> dict[str, Any]:
        return {
            "originalSize": {
                "width": self.original_width,
                "height": self.original_height,
            },
            "normalization": {
                "canvas": {"width": self.canvas_width, "height": self.canvas_height},
                "scaled": {"width": self.scaled_width, "height": self.scaled_height},
                "offset": {"x": self.offset_x, "y": self.offset_y},
                "padded": self.padded,
            },
            "imageSha256": self.image_sha256,
        }


@dataclass(frozen=True)
class ImageCandidate:
    """worker 只可产生的 attempt candidate；不代表正式文件已经发布。"""

    path: Path
    receipt_path: Path
    attempt_id: str
    formal_file: str
    sha256: str
    byte_count: int
    input_identity_sha256: str
    source: str
    provider_attempts: int
    metadata: ImageMetadata
    validator_receipt: Mapping[str, Any]


def canonical_json_sha256(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256_bytes(encoded)


def image_input_identity(
    *,
    scene_id: str,
    prompt: str,
    provider: ProviderConfig,
) -> str:
    return canonical_json_sha256(
        {
            "contractVersion": "whiteboard-image-input-v1",
            "sceneId": scene_id,
            "promptSha256": sha256_bytes(prompt.encode("utf-8")),
            "provider": provider.name,
            "protocol": provider.protocol,
            "model": provider.model,
            "size": provider.size,
            "responseFormat": provider.response_format,
            "extraBody": dict(provider.extra_body),
        }
    )


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def redact_secret(value: object, secrets: str | Iterable[str]) -> str:
    text = str(value)
    candidates = [secrets] if isinstance(secrets, str) else list(secrets)
    for secret in sorted((item for item in candidates if item), key=len, reverse=True):
        text = text.replace(secret, "[REDACTED]")
    return text


def _load_json_object(path: Path, *, kind: str) -> dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"无法读取{kind}：{path}: {exc}") from exc
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{kind}不是有效 JSON：{path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ConfigError(f"{kind}顶层必须是 JSON 对象：{path}")
    return value


def _required_text(obj: Mapping[str, Any], key: str, context: str) -> str:
    value = obj.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{context}.{key} 必须是非空字符串")
    return value.strip()


def _positive_number(obj: Mapping[str, Any], key: str, context: str) -> float:
    value = obj.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ConfigError(f"{context}.{key} 必须是正数")
    return float(value)


def load_provider_config(
    config_path: str | Path,
    provider_name: str | None = None,
) -> ProviderConfig:
    """Load and select one named provider from a local credential file."""

    path = Path(config_path).expanduser()
    if not path.is_absolute():
        raise ConfigError("供应商配置路径必须是绝对路径")
    path = path.resolve(strict=False)
    document = _load_json_object(path, kind="供应商配置")
    if document.get("schemaVersion") != 1:
        raise ConfigError("供应商配置 schemaVersion 必须为 1")
    providers = document.get("providers")
    if not isinstance(providers, dict) or not providers:
        raise ConfigError("供应商配置 providers 必须是非空对象")
    active = document.get("activeProvider")
    if not isinstance(active, str) or not active:
        raise ConfigError("供应商配置 activeProvider 必须是非空字符串")
    if active not in providers:
        raise ConfigError(f"activeProvider 指向未知供应商：{active}")
    selected_name = provider_name if provider_name is not None else active
    if not isinstance(selected_name, str) or not selected_name or selected_name not in providers:
        raise ConfigError(f"未知供应商：{selected_name}")
    raw_provider = providers[selected_name]
    if not isinstance(raw_provider, dict):
        raise ConfigError(f"providers.{selected_name} 必须是对象")
    context = f"providers.{selected_name}"
    protocol = _required_text(raw_provider, "protocol", context)
    if protocol != PROTOCOL:
        raise ConfigError(f"{context}.protocol 不受支持：{protocol}")
    base_url = _required_text(raw_provider, "baseUrl", context).rstrip("/")
    parsed_base_url = urllib.parse.urlsplit(base_url)
    if parsed_base_url.scheme not in {"http", "https"} or not parsed_base_url.netloc:
        raise ConfigError(f"{context}.baseUrl 必须是 HTTP/HTTPS URL")
    api_key = _required_text(raw_provider, "apiKey", context)
    model = _required_text(raw_provider, "model", context)

    request = raw_provider.get("request")
    if not isinstance(request, dict):
        raise ConfigError(f"{context}.request 必须是对象")
    size = _required_text(request, "size", f"{context}.request")
    response_format = _required_text(request, "responseFormat", f"{context}.request")
    if response_format not in {"b64_json", "url"}:
        raise ConfigError(f"{context}.request.responseFormat 只支持 b64_json 或 url")
    request_timeout = _positive_number(request, "timeoutSeconds", f"{context}.request")

    download = raw_provider.get("download")
    if not isinstance(download, dict):
        raise ConfigError(f"{context}.download 必须是对象")
    download_timeout = _positive_number(download, "timeoutSeconds", f"{context}.download")
    max_bytes_value = download.get("maxBytes")
    if isinstance(max_bytes_value, bool) or not isinstance(max_bytes_value, int) or max_bytes_value <= 0:
        raise ConfigError(f"{context}.download.maxBytes 必须是正整数")

    extra_body = raw_provider.get("extraBody", {})
    if not isinstance(extra_body, dict):
        raise ConfigError(f"{context}.extraBody 必须是对象")
    collisions = CORE_REQUEST_FIELDS.intersection(extra_body)
    if collisions:
        names = ", ".join(sorted(collisions))
        raise ConfigError(f"{context}.extraBody 不得覆盖核心字段：{names}")

    endpoint = (
        base_url
        if base_url.endswith("/images/generations")
        else f"{base_url}/images/generations"
    )
    return ProviderConfig(
        name=selected_name,
        protocol=protocol,
        base_url=base_url,
        endpoint=endpoint,
        api_key=api_key,
        model=model,
        size=size,
        response_format=response_format,
        request_timeout_seconds=request_timeout,
        download_timeout_seconds=download_timeout,
        max_bytes=max_bytes_value,
        extra_body=dict(extra_body),
        config_path=path,
    )


def verify_config_git_safety(config_path: str | Path) -> list[str]:
    """Require a credential file in a Git worktree to be ignored.

    A file outside every Git worktree is allowed, but the returned warning must
    be surfaced by the caller.
    """

    path = Path(config_path).expanduser()
    if not path.is_absolute():
        raise CredentialSafetyError("供应商配置路径必须是绝对路径")
    path = path.resolve(strict=False)
    if not path.is_file():
        raise CredentialSafetyError(f"供应商配置文件不存在：{path}")
    try:
        root_result = subprocess.run(
            ["git", "-C", str(path.parent), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except OSError:
        return ["警告：Git 不可用，无法证明本地供应商配置已被忽略；允许继续执行。"]
    if root_result.returncode != 0:
        return ["警告：供应商配置不属于 Git 仓库，无法证明忽略状态；允许继续执行。"]
    repo_root = Path(root_result.stdout.strip()).resolve(strict=False)
    try:
        relative = path.relative_to(repo_root)
    except ValueError as exc:
        raise CredentialSafetyError("无法确认供应商配置属于检测到的 Git 仓库") from exc
    ignored = subprocess.run(
        ["git", "-C", str(repo_root), "check-ignore", "--quiet", "--", str(relative)],
        capture_output=True,
        check=False,
    )
    if ignored.returncode == 0:
        return []
    if ignored.returncode == 1:
        raise CredentialSafetyError(f"本地供应商配置未被 Git 忽略：{path}")
    raise CredentialSafetyError("Git 无法验证本地供应商配置的忽略状态")


def build_final_prompt(global_prompt: str, scene_prompt: str) -> str:
    if not isinstance(global_prompt, str) or not global_prompt.strip():
        raise ConfigError("globalPrompt 不能为空")
    if not isinstance(scene_prompt, str) or not scene_prompt.strip():
        raise ConfigError("scene.prompt 不能为空")
    return f"{global_prompt.strip()}\n\n场景要求：\n{scene_prompt.strip()}"


def _read_limited(response: Any, max_bytes: int, *, stage: str) -> bytes:
    content_length = response.headers.get("Content-Length") if response.headers else None
    if content_length:
        try:
            if int(content_length) > max_bytes:
                raise ResponseDecodeError(f"响应超过字节上限 {max_bytes}")
        except ValueError:
            pass
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = response.read(min(64 * 1024, max_bytes + 1 - total))
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            if stage == "download":
                raise ResponseDecodeError(f"图片下载超过字节上限 {max_bytes}")
            raise ResponseDecodeError("供应商响应正文超过安全上限")
        chunks.append(chunk)
    return b"".join(chunks)


def _http_status_retryable(status: int) -> bool:
    return status in {408, 429} or 500 <= status <= 599


class ImagesGenerationsClient:
    """Synchronous, single-provider client with bounded automatic retry."""

    def __init__(
        self,
        provider: ProviderConfig,
        *,
        sleep_fn: Callable[[float], None] = time.sleep,
        random_fn: Callable[[], float] = random.random,
        urlopen: Callable[..., Any] = urllib.request.urlopen,
    ) -> None:
        self.provider = provider
        self._sleep = sleep_fn
        self._random = random_fn
        self._urlopen = urlopen

    def _request_generation(self, prompt: str) -> bytes:
        body = {
            "model": self.provider.model,
            "prompt": prompt,
            "n": 1,
            "size": self.provider.size,
            "response_format": self.provider.response_format,
            **self.provider.extra_body,
        }
        encoded = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        request = urllib.request.Request(
            self.provider.endpoint,
            data=encoded,
            method="POST",
            headers={
                "Authorization": f"Bearer {self.provider.api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        # Base64 has 4/3 overhead; bounded JSON framing gets an additional MiB.
        response_limit = math.ceil(self.provider.max_bytes * 4 / 3) + 1024 * 1024
        try:
            with self._urlopen(request, timeout=self.provider.request_timeout_seconds) as response:
                return _read_limited(response, response_limit, stage="request")
        except urllib.error.HTTPError as exc:
            raise HttpRequestError(
                f"供应商请求失败：HTTP {exc.code}",
                status=exc.code,
                retryable=_http_status_retryable(exc.code),
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise HttpRequestError(
                f"供应商请求连接失败：{redact_secret(exc, self.provider.api_key)}",
                retryable=True,
            ) from exc

    def _download_image(self, url: str) -> bytes:
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ResponseDecodeError("图片 URL 只允许有效的 HTTP 或 HTTPS 地址")
        # Deliberately no Authorization header: the URL may point to third-party storage.
        request = urllib.request.Request(
            url,
            method="GET",
            headers={"Accept": "*/*", "User-Agent": "srt-whiteboard-animation/1"},
        )
        try:
            with self._urlopen(request, timeout=self.provider.download_timeout_seconds) as response:
                payload = _read_limited(response, self.provider.max_bytes, stage="download")
        except urllib.error.HTTPError as exc:
            raise HttpRequestError(
                f"图片下载失败：HTTP {exc.code}",
                status=exc.code,
                retryable=_http_status_retryable(exc.code),
                stage="decoding",
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise HttpRequestError(
                f"图片下载连接失败：{redact_secret(exc, self.provider.api_key)}",
                retryable=True,
                stage="decoding",
            ) from exc
        if not payload:
            raise ResponseDecodeError("图片下载结果为空")
        return payload

    def _decode_response(self, raw: bytes) -> tuple[bytes, str]:
        if not raw:
            raise ResponseDecodeError("供应商响应为空")
        try:
            document = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ResponseDecodeError("供应商响应不是有效 JSON") from exc
        if not isinstance(document, dict):
            raise ResponseDecodeError("供应商响应顶层必须是对象")
        data = document.get("data")
        if not isinstance(data, list) or not data:
            raise ResponseDecodeError("供应商响应 data 必须是非空数组")
        first = data[0]
        if not isinstance(first, dict):
            raise ResponseDecodeError("供应商响应 data[0] 必须是对象")
        encoded = first.get("b64_json")
        if isinstance(encoded, str) and encoded:
            try:
                payload = base64.b64decode(encoded, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise ResponseDecodeError("b64_json 不是严格有效的 Base64") from exc
            if not payload:
                raise ResponseDecodeError("b64_json 解码结果为空")
            if len(payload) > self.provider.max_bytes:
                raise ResponseDecodeError(
                    f"Base64 图片超过字节上限 {self.provider.max_bytes}"
                )
            return payload, "b64_json"
        url = first.get("url")
        if isinstance(url, str) and url:
            return self._download_image(url), "url"
        raise ResponseDecodeError("供应商响应 data[0] 缺少 b64_json 或 url")

    def generate(self, prompt: str, max_attempts: int = MAX_ATTEMPTS) -> ImagePayload:
        if not isinstance(prompt, str) or not prompt.strip():
            raise ConfigError("最终提示词不能为空")
        if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or not 1 <= max_attempts <= MAX_ATTEMPTS:
            raise ConfigError(f"max_attempts 必须是 1 到 {MAX_ATTEMPTS} 的整数")
        for attempt in range(1, max_attempts + 1):
            try:
                raw = self._request_generation(prompt)
                payload, source = self._decode_response(raw)
                return ImagePayload(data=payload, source=source, attempts=attempt)
            except HttpRequestError as exc:
                if not exc.retryable or attempt == max_attempts:
                    raise
                delay = (2 ** (attempt - 1)) + self._random() * 0.25
                self._sleep(delay)
        raise AssertionError("unreachable")


def _load_pillow() -> tuple[Any, Any]:
    try:
        from PIL import Image, UnidentifiedImageError
    except ImportError as exc:
        raise ImageValidationError("缺少 Pillow，无法解码和归一化图片") from exc
    return Image, UnidentifiedImageError


def _safe_scene_token(scene_id: str) -> str:
    if not isinstance(scene_id, str) or not scene_id or any(
        char in scene_id for char in "\\/:*?\"<>|"
    ) or scene_id in {".", ".."} or ".." in scene_id:
        raise ImageValidationError("scene_id 不是安全的临时文件名")
    return scene_id


def normalize_image_candidate(
    image_bytes: bytes,
    candidate_path: str | Path,
    run_dir: str | Path,
    scene_id: str,
    *,
    attempt_id: str,
    formal_file: str,
    input_identity_sha256: str,
    source: str,
    provider_attempts: int,
) -> ImageCandidate:
    """完整解码并只原子写 attempt candidate/receipt，不接触正式 scene。"""

    if not isinstance(image_bytes, bytes) or not image_bytes:
        raise ImageValidationError("图片字节为空")
    if not isinstance(input_identity_sha256, str) or len(input_identity_sha256) != 64:
        raise ImageValidationError("input identity 无效")
    if source not in {"b64_json", "url"}:
        raise ImageValidationError("图片来源无效")
    if isinstance(provider_attempts, bool) or not isinstance(provider_attempts, int) or provider_attempts < 1:
        raise ImageValidationError("provider attempts 无效")
    target = Path(candidate_path).resolve(strict=False)
    work = Path(run_dir).resolve(strict=False)
    try:
        target.relative_to(work)
    except ValueError as exc:
        raise ImageValidationError("candidate 必须位于当前 attempt 工作目录") from exc
    if target.parent != work:
        raise ImageValidationError("candidate 必须是当前 attempt 目录直属文件")
    if not isinstance(attempt_id, str) or not attempt_id or Path(attempt_id).name != attempt_id:
        raise ImageValidationError("attemptId 无效")
    if not isinstance(formal_file, str) or not formal_file.startswith("scenes/") or ".." in Path(formal_file).parts:
        raise ImageValidationError("formalFile 无效")
    work.mkdir(parents=True, exist_ok=True)
    token = _safe_scene_token(scene_id)
    decoded_part = work / f"{token}.decoded.part"
    normalized_part = work / f"{token}.normalized.png.part"
    receipt_path = target.with_name("candidate-receipt.json")
    receipt_part = target.with_name("candidate-receipt.json.part")
    if target.exists() or receipt_path.exists():
        raise ImageValidationError("attempt candidate 已存在，拒绝 worker 覆盖")
    Image, UnidentifiedImageError = _load_pillow()
    metadata: ImageMetadata | None = None
    try:
        if len(image_bytes) > MAX_SOURCE_PIXELS * 4 + 1024 * 1024:
            raise ImageValidationError("图片压缩字节异常超限")
        decoded_part.write_bytes(image_bytes)
        try:
            with Image.open(decoded_part) as source_image:
                width, height = source_image.size
                if width < MIN_SOURCE_DIMENSION or height < MIN_SOURCE_DIMENSION:
                    raise ImageValidationError(
                        f"原图尺寸不得小于 {MIN_SOURCE_DIMENSION}×{MIN_SOURCE_DIMENSION}：{width}×{height}"
                    )
                if width * height > MAX_SOURCE_PIXELS:
                    raise ImageValidationError(
                        f"原图总像素数超过 {MAX_SOURCE_PIXELS}：{width * height}"
                    )
                # Force decoding only after dimensions and pixel count are accepted.
                source_image.load()
                if source_image.mode in {"RGBA", "LA"} or "transparency" in source_image.info:
                    foreground = source_image.convert("RGBA")
                    background = Image.new("RGBA", source_image.size, CANVAS_BACKGROUND)
                    rgb = Image.alpha_composite(background, foreground).convert("RGB")
                else:
                    rgb = source_image.convert("RGB")
        except ImageValidationError:
            raise
        except (Image.DecompressionBombError, UnidentifiedImageError, OSError, ValueError) as exc:
            raise ImageValidationError("图片字节无法完整解码") from exc

        canvas_width, canvas_height = CANVAS_SIZE
        scale = min(canvas_width / width, canvas_height / height)
        scaled_width = max(1, min(canvas_width, int(round(width * scale))))
        scaled_height = max(1, min(canvas_height, int(round(height * scale))))
        coverage = (scaled_width * scaled_height) / (canvas_width * canvas_height)
        if coverage < MIN_COVERAGE_RATIO:
            raise ImageValidationError(
                f"contain 后覆盖面积 {coverage:.4f} 低于安全阈值 {MIN_COVERAGE_RATIO:.2f}"
            )
        resampling = getattr(Image, "Resampling", Image).LANCZOS
        resized = rgb.resize((scaled_width, scaled_height), resampling)
        canvas = Image.new("RGB", CANVAS_SIZE, CANVAS_BACKGROUND)
        offset_x = (canvas_width - scaled_width) // 2
        offset_y = (canvas_height - scaled_height) // 2
        canvas.paste(resized, (offset_x, offset_y))
        # Explicit PNG format is required because the file suffix ends in .part.
        # coordinator 可能在 worker 启动前只预登记尚未创建的 attempt 目录；
        # 此处再次确保同一已校验 attempt 根存在，不创建任何根外目录。
        work.mkdir(parents=True, exist_ok=True)
        canvas.save(normalized_part, format="PNG", optimize=False)

        try:
            with Image.open(normalized_part) as verified:
                verified.load()
                if verified.format != "PNG" or verified.mode != "RGB" or verified.size != CANVAS_SIZE:
                    raise ImageValidationError("归一化临时 PNG 的格式、模式或尺寸不正确")
        except ImageValidationError:
            raise
        except (UnidentifiedImageError, OSError, ValueError) as exc:
            raise ImageValidationError("归一化临时 PNG 无法重新打开验证") from exc
        image_hash = sha256_file(normalized_part)
        metadata = ImageMetadata(
            original_width=width,
            original_height=height,
            canvas_width=canvas_width,
            canvas_height=canvas_height,
            scaled_width=scaled_width,
            scaled_height=scaled_height,
            offset_x=offset_x,
            offset_y=offset_y,
            padded=scaled_width != canvas_width or scaled_height != canvas_height,
            image_sha256=image_hash,
        )
        os.replace(normalized_part, target)
        candidate_bytes = target.stat().st_size
        receipt_evidence: dict[str, Any] = {
            "legacyContractVersion": CANDIDATE_RECEIPT_VERSION,
            "attemptId": attempt_id,
            "sceneId": scene_id,
            "inputIdentitySha256": input_identity_sha256,
            "candidateFile": target.name,
            "formalFile": formal_file,
            "candidateSha256": image_hash,
            "candidateBytes": candidate_bytes,
            "source": source,
            "providerAttempts": provider_attempts,
            "providerReceipt": {
                "source": source,
                "attempts": provider_attempts,
            },
            "imageMetadata": metadata.to_manifest_fields(),
        }
        validator_receipt = validation_receipts.build_candidate_receipt(
            candidate_sha256=image_hash,
            candidate_bytes=candidate_bytes,
            decoded=True,
            format="PNG",
            validator_contract=IMAGE_VALIDATOR_CONTRACT_VERSION,
            evidence=receipt_evidence,
        )
        with receipt_part.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(validator_receipt, ensure_ascii=False, indent=2) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(receipt_part, receipt_path)
        return ImageCandidate(
            path=target,
            receipt_path=receipt_path,
            attempt_id=attempt_id,
            formal_file=formal_file,
            sha256=image_hash,
            byte_count=candidate_bytes,
            input_identity_sha256=input_identity_sha256,
            source=source,
            provider_attempts=provider_attempts,
            metadata=metadata,
            validator_receipt=validator_receipt,
        )
    finally:
        for temporary in (decoded_part, normalized_part, receipt_part):
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def _metadata_from_receipt(value: Mapping[str, Any]) -> ImageMetadata:
    try:
        original = value["originalSize"]
        normalization = value["normalization"]
        canvas = normalization["canvas"]
        scaled = normalization["scaled"]
        offset = normalization["offset"]
        image_sha = value["imageSha256"]
        metadata = ImageMetadata(
            original_width=original["width"],
            original_height=original["height"],
            canvas_width=canvas["width"],
            canvas_height=canvas["height"],
            scaled_width=scaled["width"],
            scaled_height=scaled["height"],
            offset_x=offset["x"],
            offset_y=offset["y"],
            padded=normalization["padded"],
            image_sha256=image_sha,
        )
    except (KeyError, TypeError) as exc:
        raise ImageValidationError("candidate receipt 图片元数据无效") from exc
    if (
        metadata.canvas_width,
        metadata.canvas_height,
    ) != CANVAS_SIZE or not isinstance(metadata.padded, bool):
        raise ImageValidationError("candidate receipt 画布元数据无效")
    return metadata


def load_image_candidate(
    candidate_path: str | Path,
    *,
    expected_attempt_root: str | Path,
    expected_attempt_id: str,
    expected_scene_id: str,
    expected_input_identity_sha256: str,
    expected_formal_file: str,
) -> ImageCandidate:
    """按已登记确定路径重验 candidate 与去敏 receipt，不扫描目录猜测。"""

    path = Path(candidate_path).resolve(strict=False)
    attempt_root = Path(expected_attempt_root).resolve(strict=False)
    if path.parent != attempt_root:
        raise ImageValidationError("candidate 不在已登记 attempt 根目录")
    receipt_path = path.with_name("candidate-receipt.json")
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ImageValidationError("candidate receipt 不存在或无效") from exc
    if not isinstance(receipt, dict):
        raise ImageValidationError("candidate receipt contract 无效")
    try:
        receipt_read = validation_receipts.read_candidate_receipt(receipt)
    except validation_receipts.ReceiptValidationError as exc:
        raise ImageValidationError(str(exc)) from exc
    needs_deep = not receipt_read.current_contract
    if receipt_read.current_contract:
        receipt_contract = receipt_read.receipt.get("validatorContract")
        if not isinstance(receipt_contract, str):
            raise ImageValidationError("candidate receipt validator contract 无效")
        try:
            current_receipt = validation_receipts.bind_candidate_receipt(
                path,
                receipt_read.receipt,
                expected_format="PNG",
                expected_validator_contract=receipt_contract,
            )
        except validation_receipts.ReceiptValidationError as exc:
            raise ImageValidationError(str(exc)) from exc
        evidence = current_receipt.get("evidence")
        if not isinstance(evidence, Mapping):
            raise ImageValidationError("candidate receipt evidence 无效")
        needs_deep = receipt_contract != IMAGE_VALIDATOR_CONTRACT_VERSION
    if needs_deep:
        # 旧 receipt 只作为深验所需的兼容元数据，绝不能直接成为 current PASS。
        if not receipt_read.current_contract:
            if receipt.get("contractVersion") != CANDIDATE_RECEIPT_VERSION:
                raise ImageValidationError("candidate receipt contract 无效")
            evidence = receipt
        Image, UnidentifiedImageError = _load_pillow()
        try:
            with Image.open(path) as image:
                image.load()
                if image.format != "PNG" or image.mode != "RGB" or image.size != CANVAS_SIZE:
                    raise ImageValidationError("candidate PNG 格式、模式或尺寸不正确")
        except ImageValidationError:
            raise
        except (UnidentifiedImageError, OSError, ValueError) as exc:
            raise ImageValidationError("candidate PNG 无法完整解码") from exc
        actual_hash = sha256_file(path)
        actual_bytes = path.stat().st_size
        receipt = validation_receipts.build_candidate_receipt(
            candidate_sha256=actual_hash,
            candidate_bytes=actual_bytes,
            decoded=True,
            format="PNG",
            validator_contract=IMAGE_VALIDATOR_CONTRACT_VERSION,
            evidence={"legacyContractVersion": CANDIDATE_RECEIPT_VERSION, **dict(evidence)},
        )
        receipt_part = receipt_path.with_name(f".{receipt_path.name}.{uuid.uuid4().hex}.part")
        try:
            with receipt_part.open("x", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(receipt_part, receipt_path)
        finally:
            receipt_part.unlink(missing_ok=True)
        current_receipt = receipt
    if evidence.get("sceneId") != expected_scene_id:
        raise ImageValidationError("candidate receipt scene identity 不匹配")
    if evidence.get("attemptId") != expected_attempt_id:
        raise ImageValidationError("candidate receipt attempt identity 不匹配")
    if evidence.get("inputIdentitySha256") != expected_input_identity_sha256:
        raise ImageValidationError("candidate receipt input identity 不匹配")
    if evidence.get("candidateFile") != path.name or not path.is_file():
        raise ImageValidationError("candidate receipt 文件绑定无效")
    if evidence.get("formalFile") != expected_formal_file:
        raise ImageValidationError("candidate receipt formalFile 绑定无效")
    actual_hash = sha256_file(path)
    actual_bytes = path.stat().st_size
    if current_receipt.get("candidateSha256") != actual_hash or current_receipt.get("candidateBytes") != actual_bytes:
        raise ImageValidationError("candidate SHA/bytes 与 receipt 不匹配")
    metadata_raw = evidence.get("imageMetadata")
    if not isinstance(metadata_raw, dict):
        raise ImageValidationError("candidate receipt 缺少图片元数据")
    metadata = _metadata_from_receipt(metadata_raw)
    if metadata.image_sha256 != actual_hash:
        raise ImageValidationError("candidate metadata SHA 不匹配")
    source = evidence.get("source")
    provider_attempts = evidence.get("providerAttempts")
    if source not in {"b64_json", "url"}:
        raise ImageValidationError("candidate receipt source 无效")
    if isinstance(provider_attempts, bool) or not isinstance(provider_attempts, int) or provider_attempts < 1:
        raise ImageValidationError("candidate receipt providerAttempts 无效")
    return ImageCandidate(
        path=path,
        receipt_path=receipt_path,
        attempt_id=expected_attempt_id,
        formal_file=expected_formal_file,
        sha256=actual_hash,
        byte_count=actual_bytes,
        input_identity_sha256=expected_input_identity_sha256,
        source=source,
        provider_attempts=provider_attempts,
        metadata=metadata,
        validator_receipt=current_receipt,
    )


def bind_image_candidate(candidate: ImageCandidate, path: str | Path) -> None:
    """只复核发布后 PNG 与 current receipt；binding 失败绝不回退 deep。"""

    try:
        validation_receipts.bind_candidate_receipt(
            path,
            candidate.validator_receipt,
            expected_format="PNG",
            expected_validator_contract=IMAGE_VALIDATOR_CONTRACT_VERSION,
        )
    except validation_receipts.ReceiptValidationError as exc:
        raise ImageValidationError(str(exc)) from exc


def publish_image_candidate(
    candidate: ImageCandidate,
    destination: str | Path,
    *,
    overwrite: bool,
) -> None:
    """coordinator 在正式目录同卷复制、fsync、replace；candidate 保留。"""

    target = Path(destination).resolve(strict=False)
    if target.exists() and not overwrite:
        raise ImageValidationError("正式目标已存在且未授权覆盖")
    bind_image_candidate(candidate, candidate.path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.publishing.tmp")
    backup = target.with_name(f".{target.name}.{uuid.uuid4().hex}.previous")
    had_formal = target.is_file()
    preserve_backup = False
    try:
        if had_formal:
            try:
                os.link(target, backup)
            except OSError:
                shutil.copy2(target, backup)
        with candidate.path.open("rb") as source_handle, temporary.open("xb") as target_handle:
            shutil.copyfileobj(source_handle, target_handle, length=1024 * 1024)
            target_handle.flush()
            os.fsync(target_handle.fileno())
        if sha256_file(temporary) != candidate.sha256 or temporary.stat().st_size != candidate.byte_count:
            raise ImageValidationError("正式发布临时文件 SHA/bytes 不匹配")
        os.replace(temporary, target)
        try:
            bind_image_candidate(candidate, target)
        except Exception:
            try:
                if had_formal and backup.is_file():
                    os.replace(backup, target)
                elif target.is_file():
                    target.unlink()
            except OSError as restore_error:
                preserve_backup = True
                raise ImageValidationError(
                    "图片发布后 binding 失败，旧正式文件恢复失败"
                ) from restore_error
            raise
    finally:
        temporary.unlink(missing_ok=True)
        if not preserve_backup:
            backup.unlink(missing_ok=True)


def normalize_and_store_image(
    image_bytes: bytes,
    destination: str | Path,
    run_dir: str | Path,
    scene_id: str,
    *,
    overwrite: bool = False,
) -> ImageMetadata:
    """串行兼容包装；内部仍严格走 candidate → coordinator publish。"""

    work = Path(run_dir).resolve(strict=False)
    attempt_dir = work / "compat-attempt"
    candidate = normalize_image_candidate(
        image_bytes,
        attempt_dir / "candidate.png",
        attempt_dir,
        scene_id,
        attempt_id="compat-attempt-0001",
        formal_file=f"scenes/{Path(destination).name}",
        input_identity_sha256=sha256_bytes(image_bytes),
        source="b64_json",
        provider_attempts=1,
    )
    publish_image_candidate(candidate, destination, overwrite=overwrite)
    return candidate.metadata


def _relative_project_path(project_root: Path, path: Path) -> str:
    try:
        return path.resolve(strict=False).relative_to(project_root.resolve(strict=False)).as_posix()
    except ValueError as exc:
        raise ManifestError(f"路径必须位于项目根目录内：{path}") from exc


class ManifestStore:
    """Manifest persistence with append-only run history and atomic saves."""

    def __init__(self, project_root: Path, manifest_path: Path, data: dict[str, Any]) -> None:
        self.project_root = project_root
        self.manifest_path = manifest_path
        self.data = data
        self._active_run_id: str | None = None
        self._secrets: set[str] = set()

    @classmethod
    def open(
        cls,
        project_root: str | Path,
        project_id: str,
        plan_path: str | Path,
        scene_specs: Sequence[Mapping[str, Any]],
    ) -> "ManifestStore":
        root = Path(project_root).resolve(strict=False)
        plan = Path(plan_path).resolve(strict=False)
        plan_relative = _relative_project_path(root, plan)
        if not plan.is_file():
            raise ManifestError(f"generation plan 不存在：{plan}")
        plan_hash = sha256_file(plan)
        manifest_path = root / "manifests" / "generation-manifest.json"
        now = utc_now()
        expected_scenes: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        for spec in scene_specs:
            scene_id = spec.get("sceneId")
            output_file = spec.get("outputFile")
            if not isinstance(scene_id, str) or not scene_id or scene_id in seen_ids:
                raise ManifestError("scene_specs 包含缺失或重复的 sceneId")
            if not isinstance(output_file, str) or not output_file:
                raise ManifestError(f"场景 {scene_id} 缺少 outputFile")
            seen_ids.add(scene_id)
            expected_scenes.append(
                {
                    "sceneId": scene_id,
                    "outputFile": output_file,
                    "status": "pending",
                    "provider": None,
                    "model": None,
                    "prompt": None,
                    "promptSha256": None,
                    "originalSize": None,
                    "normalization": None,
                    "imageSha256": None,
                    "source": None,
                    "attempts": 0,
                    "createdAt": None,
                    "failureStage": None,
                    "error": None,
                    "currentAttemptId": None,
                    "attemptRecords": [],
                }
            )
        if manifest_path.exists():
            try:
                existing = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ManifestError(f"manifest 无法读取：{manifest_path}") from exc
            if not isinstance(existing, dict) or existing.get("schemaVersion") != 1:
                raise ManifestError("manifest schemaVersion 无效")
            if existing.get("projectId") != project_id:
                raise ManifestError("manifest projectId 与项目不一致")
            generation_plan = existing.get("generationPlan")
            if not isinstance(generation_plan, dict):
                raise ManifestError("manifest generationPlan 无效")
            if generation_plan.get("file") != plan_relative or generation_plan.get("sha256") != plan_hash:
                raise ManifestError("generation plan 已变化，拒绝复用旧 manifest")
            existing_scenes = existing.get("scenes")
            if not isinstance(existing_scenes, list):
                raise ManifestError("manifest scenes 必须是数组")
            if not isinstance(existing.get("runs"), list):
                raise ManifestError("manifest runs 必须是数组")
            if not isinstance(existing.get("summary"), dict):
                raise ManifestError("manifest summary 必须是对象")
            for item in existing_scenes:
                if not isinstance(item, dict) or item.get("status") not in SCENE_STATUSES:
                    raise ManifestError("manifest 包含结构或状态无效的场景记录")
                item.setdefault("currentAttemptId", None)
                item.setdefault("attemptRecords", [])
                if not isinstance(item["attemptRecords"], list):
                    raise ManifestError("manifest attemptRecords 必须是数组")
            expected_identity = [
                (item["sceneId"], item["outputFile"]) for item in expected_scenes
            ]
            actual_identity = [
                (item.get("sceneId"), item.get("outputFile"))
                for item in existing_scenes
                if isinstance(item, dict)
            ]
            if actual_identity != expected_identity:
                raise ManifestError("manifest 场景列表与 generation plan 不一致")
            data = existing
        else:
            data = {
                "schemaVersion": 1,
                "projectId": project_id,
                "generationPlan": {"file": plan_relative, "sha256": plan_hash},
                "createdAt": now,
                "updatedAt": now,
                "completedAt": None,
                "summary": {
                    "sceneTotal": len(expected_scenes),
                    "successCount": 0,
                    "failedCount": 0,
                },
                "runs": [],
                "scenes": expected_scenes,
            }
        return cls(root, manifest_path, data)

    def _find_scene(self, scene_id: str) -> dict[str, Any]:
        for scene in self.data.get("scenes", []):
            if isinstance(scene, dict) and scene.get("sceneId") == scene_id:
                return scene
        raise ManifestError(f"manifest 中不存在场景：{scene_id}")

    def _find_run(self, run_id: str) -> dict[str, Any]:
        for run in self.data.get("runs", []):
            if isinstance(run, dict) and run.get("runId") == run_id:
                return run
        raise ManifestError(f"manifest 中不存在运行记录：{run_id}")

    def begin_run(
        self,
        run_id: str,
        provider: ProviderConfig,
        started_at: str | None = None,
        *,
        configured_concurrency: int = 1,
        effective_concurrency: int = 1,
        task_count: int = 0,
    ) -> dict[str, Any]:
        if not isinstance(run_id, str) or not run_id or any(
            char in run_id for char in "\\/:*?\"<>|"
        ) or ".." in run_id:
            raise ManifestError("run_id 不是安全的目录名")
        if any(run.get("runId") == run_id for run in self.data.get("runs", []) if isinstance(run, dict)):
            raise ManifestError(f"run_id 已存在：{run_id}")
        record = {
            "runId": run_id,
            "provider": provider.name,
            "protocol": provider.protocol,
            "model": provider.model,
            "startedAt": started_at or utc_now(),
            "completedAt": None,
            "status": "running",
            "exitResult": None,
            "configuredConcurrency": configured_concurrency,
            "effectiveConcurrency": effective_concurrency,
            "taskCount": task_count,
            "adoptedCandidateCount": 0,
            "unknownExternalOutcomeCount": 0,
        }
        self.data.setdefault("runs", []).append(record)
        self._active_run_id = run_id
        self._secrets.add(provider.api_key)
        self.data["completedAt"] = None
        self._touch()
        return record

    def current_attempt(self, scene_id: str) -> dict[str, Any] | None:
        scene = self._find_scene(scene_id)
        attempt_id = scene.get("currentAttemptId")
        records = scene.get("attemptRecords", [])
        if attempt_id is None:
            return None
        for attempt in records:
            if isinstance(attempt, dict) and attempt.get("attemptId") == attempt_id:
                return attempt
        raise ManifestError(f"场景 {scene_id} 的 currentAttemptId 无对应记录")

    def prepare_attempt(
        self,
        scene_id: str,
        *,
        attempt_id: str,
        input_identity_sha256: str,
        candidate_file: str,
        receipt_file: str,
        formal_file: str,
        overwrite: bool,
        provider: str,
        model: str,
        prompt: str,
    ) -> dict[str, Any]:
        scene = self._find_scene(scene_id)
        if any(
            isinstance(item, dict) and item.get("attemptId") == attempt_id
            for item in scene.get("attemptRecords", [])
        ):
            raise ManifestError("attemptId 已存在")
        for label, relative in (
            ("candidateFile", candidate_file),
            ("receiptFile", receipt_file),
            ("formalFile", formal_file),
        ):
            path = Path(relative)
            if path.is_absolute() or ".." in path.parts or "\\" in relative:
                raise ManifestError(f"{label} 必须是项目内相对 POSIX 路径")
            _relative_project_path(self.project_root, self.project_root / path)
        if formal_file != f"scenes/{scene['outputFile']}":
            raise ManifestError("formalFile 与 generation plan 不一致")
        record = {
            "attemptId": attempt_id,
            "status": "prepared",
            "inputIdentitySha256": input_identity_sha256,
            "candidateFile": candidate_file,
            "receiptFile": receipt_file,
            "candidateSha256": None,
            "candidateBytes": None,
            "validatorReceipt": None,
            "formalFile": formal_file,
            "externalOutcome": "not_started",
            "overwrite": overwrite,
            "provider": provider,
            "model": model,
            "promptSha256": sha256_bytes(prompt.encode("utf-8")),
            "source": None,
            "providerAttempts": 0,
            "error": None,
        }
        scene.setdefault("attemptRecords", []).append(record)
        scene["currentAttemptId"] = attempt_id
        scene["status"] = "prepared"
        scene["provider"] = provider
        scene["model"] = model
        sanitized_prompt = redact_secret(prompt, self._secrets)
        scene["prompt"] = sanitized_prompt
        scene["promptSha256"] = sha256_bytes(sanitized_prompt.encode("utf-8"))
        scene["failureStage"] = None
        scene["error"] = None
        self._touch()
        return record

    def mark_attempt(
        self,
        scene_id: str,
        *,
        status: str,
        candidate: ImageCandidate | None = None,
        external_outcome: str | None = None,
        error: str | None = None,
    ) -> dict[str, Any]:
        if status not in ATTEMPT_STATUSES:
            raise ManifestError(f"无效 attempt 状态：{status}")
        scene = self._find_scene(scene_id)
        attempt = self.current_attempt(scene_id)
        if attempt is None:
            raise ManifestError("场景没有 current attempt")
        attempt["status"] = status
        scene["status"] = status
        if external_outcome is not None:
            if external_outcome not in {
                "not_started",
                "succeeded",
                "explicit_failed",
                "unknown_external_outcome",
            }:
                raise ManifestError("externalOutcome 无效")
            attempt["externalOutcome"] = external_outcome
        if candidate is not None:
            expected_candidate = self.project_root / Path(attempt["candidateFile"])
            expected_receipt = self.project_root / Path(attempt["receiptFile"])
            if candidate.path != expected_candidate.resolve(strict=False):
                raise ManifestError("candidate 路径与 attempt 登记不一致")
            if candidate.receipt_path != expected_receipt.resolve(strict=False):
                raise ManifestError("candidate receipt 路径与 attempt 登记不一致")
            if candidate.input_identity_sha256 != attempt["inputIdentitySha256"]:
                raise ManifestError("candidate input identity 与 attempt 不一致")
            if candidate.attempt_id != attempt["attemptId"]:
                raise ManifestError("candidate attempt identity 与登记不一致")
            if candidate.formal_file != attempt["formalFile"]:
                raise ManifestError("candidate formalFile 与登记不一致")
            attempt.update(
                {
                    "candidateSha256": candidate.sha256,
                    "candidateBytes": candidate.byte_count,
                    "validatorReceipt": dict(candidate.validator_receipt),
                    "source": candidate.source,
                    "providerAttempts": candidate.provider_attempts,
                }
            )
        sanitized_error = redact_secret(error, self._secrets) if error is not None else None
        attempt["error"] = sanitized_error
        scene["error"] = sanitized_error
        scene["failureStage"] = status if status in {"failed", "unknown_external_outcome"} else None
        if status == "validated":
            if candidate is None:
                raise ManifestError("validated attempt 必须绑定 candidate")
            scene.update(candidate.metadata.to_manifest_fields())
            scene["source"] = candidate.source
            scene["attempts"] = candidate.provider_attempts
            scene["createdAt"] = utc_now()
        self._touch()
        return attempt

    def update_active_run_counts(
        self,
        *,
        adopted_candidate_count: int,
        unknown_external_outcome_count: int,
    ) -> None:
        if self._active_run_id is None:
            raise ManifestError("当前没有 active run")
        run = self._find_run(self._active_run_id)
        run["adoptedCandidateCount"] = adopted_candidate_count
        run["unknownExternalOutcomeCount"] = unknown_external_outcome_count
        self._touch()

    def mark_scene(
        self,
        scene_id: str,
        *,
        status: str,
        provider: str | None = None,
        model: str | None = None,
        prompt: str | None = None,
        source: str | None = None,
        attempts: int | None = None,
        image_metadata: ImageMetadata | None = None,
        failure_stage: str | None = None,
        error: str | None = None,
        created_at: str | None = None,
    ) -> dict[str, Any]:
        if status not in SCENE_STATUSES:
            raise ManifestError(f"无效场景状态：{status}")
        if source is not None and source not in {"b64_json", "url"}:
            raise ManifestError(f"无效图片来源：{source}")
        if attempts is not None and (
            isinstance(attempts, bool) or not isinstance(attempts, int) or attempts < 0
        ):
            raise ManifestError("attempts 必须是非负整数")
        if status == "validated" and (
            image_metadata is None
            or source not in {"b64_json", "url"}
            or prompt is None
            or provider is None
            or model is None
        ):
            raise ManifestError("validated 场景必须包含供应商、模型、提示词、来源和图片元数据")
        scene = self._find_scene(scene_id)
        if status == "requesting":
            scene.update(
                {
                    "source": None,
                    "attempts": 0,
                    "originalSize": None,
                    "normalization": None,
                    "imageSha256": None,
                    "createdAt": None,
                    "failureStage": None,
                    "error": None,
                }
            )
        scene["status"] = status
        if provider is not None:
            scene["provider"] = provider
        if model is not None:
            scene["model"] = model
        if prompt is not None:
            sanitized_prompt = redact_secret(prompt, self._secrets)
            scene["prompt"] = sanitized_prompt
            scene["promptSha256"] = sha256_bytes(sanitized_prompt.encode("utf-8"))
        if source is not None:
            scene["source"] = source
        if attempts is not None:
            scene["attempts"] = attempts
        if image_metadata is not None:
            scene.update(image_metadata.to_manifest_fields())
        if created_at is not None or status == "validated":
            scene["createdAt"] = created_at or utc_now()
        scene["failureStage"] = failure_stage
        scene["error"] = redact_secret(error, self._secrets) if error is not None else None
        self._touch()
        return scene

    def finish_run(
        self,
        run_id: str,
        *,
        exit_result: int | str,
        completed_at: str | None = None,
    ) -> dict[str, Any]:
        run = self._find_run(run_id)
        finished = completed_at or utc_now()
        run["completedAt"] = finished
        run["exitResult"] = exit_result
        run["status"] = "completed" if exit_result in {0, "0", "success"} else "failed"
        scenes = [scene for scene in self.data.get("scenes", []) if isinstance(scene, dict)]
        success_count = sum(scene.get("status") == "validated" for scene in scenes)
        failed_count = sum(scene.get("status") == "failed" for scene in scenes)
        self.data["summary"] = {
            "sceneTotal": len(scenes),
            "successCount": success_count,
            "failedCount": failed_count,
        }
        self.data["completedAt"] = finished if success_count == len(scenes) else None
        self._active_run_id = run_id
        self._touch(finished)
        return run

    def _touch(self, now: str | None = None) -> None:
        self.data["updatedAt"] = now or utc_now()

    def save(self) -> None:
        if self._active_run_id is None:
            raise ManifestError("保存 manifest 前必须 begin_run")
        work_dir = self.project_root / ".work" / self._active_run_id
        work_dir.mkdir(parents=True, exist_ok=True)
        temporary = work_dir / "generation-manifest.json.part"
        self.manifest_path.parent.mkdir(parents=True, exist_ok=True)
        serialized = json.dumps(self.data, ensure_ascii=False, indent=2) + "\n"
        temporary.write_text(serialized, encoding="utf-8")
        # Verify serialized JSON before replacing a previous valid manifest.
        try:
            reparsed = json.loads(temporary.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            temporary.unlink(missing_ok=True)
            raise ManifestError("manifest 临时文件重新读取验证失败") from exc
        if reparsed.get("schemaVersion") != 1 or reparsed.get("projectId") != self.data.get("projectId"):
            temporary.unlink(missing_ok=True)
            raise ManifestError("manifest 临时文件核心字段验证失败")
        os.replace(temporary, self.manifest_path)
