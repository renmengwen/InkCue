from __future__ import annotations

import base64
import io
import json
import shutil
import struct
import sys
import tempfile
import unittest
import urllib.error
import uuid
import zlib
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from PIL import Image


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import image_generation as ig  # noqa: E402


TEST_RUNS_ROOT = (Path(tempfile.gettempdir()) / "srt-whiteboard-image-unit-tests").resolve(strict=False)


def create_test_run() -> Path:
    if TEST_RUNS_ROOT.drive.upper() != "C:":
        raise RuntimeError("图片测试临时根必须位于 C 盘")
    TEST_RUNS_ROOT.mkdir(parents=True, exist_ok=True)
    run_root = (TEST_RUNS_ROOT / str(uuid.uuid4())).resolve(strict=False)
    relative = run_root.relative_to(TEST_RUNS_ROOT)
    if len(relative.parts) != 1:
        raise RuntimeError("测试运行目录必须是 .test-runs 的 UUID 直属子目录")
    uuid.UUID(relative.name)
    run_root.mkdir()
    return run_root


def cleanup_test_run(run_root: Path) -> None:
    resolved = run_root.resolve(strict=False)
    try:
        relative = resolved.relative_to(TEST_RUNS_ROOT)
    except ValueError as exc:
        raise RuntimeError("拒绝清理 .test-runs 之外的路径") from exc
    if len(relative.parts) != 1:
        raise RuntimeError("拒绝递归清理非 UUID 测试运行目录")
    uuid.UUID(relative.name)
    if resolved.exists():
        shutil.rmtree(resolved)


class BytesResponse:
    def __init__(self, body: bytes, headers: dict[str, str] | None = None) -> None:
        self._stream = io.BytesIO(body)
        self.headers = headers or {}

    def read(self, size: int = -1) -> bytes:
        return self._stream.read(size)

    def __enter__(self) -> "BytesResponse":
        return self

    def __exit__(self, *_args: object) -> None:
        return None


def png_bytes(size: tuple[int, int], color: tuple[int, int, int] = (25, 50, 75)) -> bytes:
    stream = io.BytesIO()
    Image.new("RGB", size, color).save(stream, format="PNG")
    return stream.getvalue()


def transparent_png_bytes(size: tuple[int, int]) -> bytes:
    stream = io.BytesIO()
    Image.new("RGBA", size, (0, 0, 0, 0)).save(stream, format="PNG")
    return stream.getvalue()


def patch_png_dimensions(payload: bytes, width: int, height: int) -> bytes:
    data = bytearray(payload)
    data[16:24] = struct.pack(">II", width, height)
    data[29:33] = struct.pack(">I", zlib.crc32(bytes(data[12:29])) & 0xFFFFFFFF)
    return bytes(data)


def provider_document(base_url: str = "https://example.test/v1") -> dict[str, object]:
    def provider(model: str, response_format: str) -> dict[str, object]:
        return {
            "protocol": "openai-images-generations",
            "baseUrl": base_url,
            "apiKey": f"secret-{model}",
            "model": model,
            "request": {
                "size": "1536x1024",
                "responseFormat": response_format,
                "timeoutSeconds": 2,
            },
            "download": {"timeoutSeconds": 2, "maxBytes": 2_000_000},
            "extraBody": {},
        }

    return {
        "schemaVersion": 1,
        "activeProvider": "primary",
        "providers": {
            "primary": provider("model-primary", "b64_json"),
            "backup": provider("model-backup", "url"),
        },
    }


class ProviderConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = create_test_run()
        self.addCleanup(cleanup_test_run, self.root)
        self.config_path = self.root / "image-providers.local.json"
        self.config_path.write_text(
            json.dumps(provider_document(), ensure_ascii=False), encoding="utf-8"
        )

    def test_selects_active_and_named_provider(self) -> None:
        active = ig.load_provider_config(self.config_path)
        backup = ig.load_provider_config(self.config_path, "backup")
        self.assertEqual(active.name, "primary")
        self.assertEqual(active.endpoint, "https://example.test/v1/images/generations")
        self.assertEqual(backup.name, "backup")
        self.assertEqual(backup.response_format, "url")

    def test_default_selection_uses_non_primary_active_provider(self) -> None:
        document = provider_document()
        document["providers"]["shuaiapi"] = document["providers"].pop("primary")  # type: ignore[index]
        document["activeProvider"] = "shuaiapi"
        self.config_path.write_text(json.dumps(document), encoding="utf-8")

        selected = ig.load_provider_config(self.config_path)

        self.assertEqual(selected.name, "shuaiapi")
        self.assertEqual(selected.model, "model-primary")

    def test_unknown_provider_and_unsupported_protocol_fail(self) -> None:
        with self.assertRaises(ig.ConfigError):
            ig.load_provider_config(self.config_path, "missing")
        document = provider_document()
        document["providers"]["primary"]["protocol"] = "something-else"  # type: ignore[index]
        self.config_path.write_text(json.dumps(document), encoding="utf-8")
        with self.assertRaises(ig.ConfigError):
            ig.load_provider_config(self.config_path)

    def test_extra_body_cannot_override_core_fields(self) -> None:
        document = provider_document()
        document["providers"]["primary"]["extraBody"] = {"model": "stolen"}  # type: ignore[index]
        self.config_path.write_text(json.dumps(document), encoding="utf-8")
        with self.assertRaisesRegex(ig.ConfigError, "model"):
            ig.load_provider_config(self.config_path)

    def test_non_git_config_is_allowed_with_warning(self) -> None:
        with mock.patch.object(
            ig.subprocess,
            "run",
            return_value=SimpleNamespace(returncode=128, stdout=""),
        ):
            warnings = ig.verify_config_git_safety(self.config_path)
        self.assertEqual(len(warnings), 1)
        self.assertIn("允许继续", warnings[0])

    def test_git_tracked_credential_file_is_rejected(self) -> None:
        with mock.patch.object(
            ig.subprocess,
            "run",
            side_effect=[
                SimpleNamespace(returncode=0, stdout=str(self.root)),
                SimpleNamespace(returncode=1, stdout=""),
            ],
        ):
            with self.assertRaises(ig.CredentialSafetyError):
                ig.verify_config_git_safety(self.config_path)

    def test_git_ignored_credential_file_is_allowed(self) -> None:
        with mock.patch.object(
            ig.subprocess,
            "run",
            side_effect=[
                SimpleNamespace(returncode=0, stdout=str(self.root)),
                SimpleNamespace(returncode=0, stdout=""),
            ],
        ):
            self.assertEqual(ig.verify_config_git_safety(self.config_path), [])

    def test_provider_repr_does_not_expose_api_key(self) -> None:
        provider = ig.load_provider_config(self.config_path)
        self.assertNotIn(provider.api_key, repr(provider))

    def test_final_prompt_is_deterministic(self) -> None:
        self.assertEqual(
            ig.build_final_prompt(" global ", " scene "),
            "global\n\n场景要求：\nscene",
        )


class ClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = create_test_run()
        self.addCleanup(cleanup_test_run, self.root)
        root = self.root
        config = root / "providers.json"
        config.write_text(json.dumps(provider_document()), encoding="utf-8")
        self.provider = ig.load_provider_config(config)
        self.image = png_bytes((1024, 1024))

    def test_b64_is_strictly_decoded_and_preferred_over_url(self) -> None:
        body = json.dumps(
            {
                "data": [
                    {
                        "b64_json": base64.b64encode(self.image).decode("ascii"),
                        "url": "ftp://must-not-be-used.invalid/image.png",
                    }
                ]
            }
        ).encode("utf-8")
        calls: list[str] = []

        def open_url(request: object, timeout: float) -> BytesResponse:
            calls.append(request.get_method())  # type: ignore[attr-defined]
            return BytesResponse(body)

        result = ig.ImagesGenerationsClient(
            self.provider, urlopen=open_url, sleep_fn=lambda _: None
        ).generate("prompt")
        self.assertEqual(result.data, self.image)
        self.assertEqual(result.source, "b64_json")
        self.assertEqual(calls, ["POST"])

    def test_url_download_does_not_forward_authorization(self) -> None:
        response = json.dumps(
            {"data": [{"url": "https://storage.example.test/file.png"}]}
        ).encode("utf-8")
        seen: list[tuple[str, str | None, str | None]] = []

        def open_url(request: object, timeout: float) -> BytesResponse:
            method = request.get_method()  # type: ignore[attr-defined]
            auth = request.get_header("Authorization")  # type: ignore[attr-defined]
            user_agent = request.get_header("User-agent")  # type: ignore[attr-defined]
            seen.append((method, auth, user_agent))
            return BytesResponse(response if method == "POST" else self.image)

        result = ig.ImagesGenerationsClient(self.provider, urlopen=open_url).generate("prompt")
        self.assertEqual(result.source, "url")
        self.assertEqual(result.data, self.image)
        self.assertEqual(
            seen[0],
            ("POST", f"Bearer {self.provider.api_key}", "curl/8.12.1"),
        )
        self.assertEqual(seen[1], ("GET", None, "srt-whiteboard-animation/1"))

    def test_429_and_500_retry_then_succeed(self) -> None:
        body = json.dumps(
            {"data": [{"b64_json": base64.b64encode(self.image).decode("ascii")}]}
        ).encode("utf-8")
        statuses: list[int | None] = [429, 500, None]
        sleeps: list[float] = []

        def open_url(request: object, timeout: float) -> BytesResponse:
            status = statuses.pop(0)
            if status is not None:
                raise urllib.error.HTTPError(
                    request.full_url, status, "failed", {}, None  # type: ignore[attr-defined]
                )
            return BytesResponse(body)

        result = ig.ImagesGenerationsClient(
            self.provider,
            urlopen=open_url,
            sleep_fn=sleeps.append,
            random_fn=lambda: 0.0,
        ).generate("prompt")
        self.assertEqual(result.attempts, 3)
        self.assertEqual(sleeps, [1.0, 2.0])

    def test_401_is_not_retried(self) -> None:
        calls = 0

        def open_url(request: object, timeout: float) -> BytesResponse:
            nonlocal calls
            calls += 1
            raise urllib.error.HTTPError(
                request.full_url, 401, "unauthorized", {}, None  # type: ignore[attr-defined]
            )

        with self.assertRaises(ig.HttpRequestError) as caught:
            ig.ImagesGenerationsClient(self.provider, urlopen=open_url).generate("prompt")
        self.assertFalse(caught.exception.retryable)
        self.assertEqual(calls, 1)

    def test_invalid_response_shapes_and_base64_fail_without_retry(self) -> None:
        for document in ({"data": []}, {"data": [{}]}, {"data": [{"b64_json": "%%%"}]}):
            calls = 0

            def open_url(request: object, timeout: float) -> BytesResponse:
                nonlocal calls
                calls += 1
                return BytesResponse(json.dumps(document).encode("utf-8"))

            with self.assertRaises(ig.ResponseDecodeError):
                ig.ImagesGenerationsClient(self.provider, urlopen=open_url).generate("prompt")
            self.assertEqual(calls, 1)

    def test_download_limit_is_enforced_without_content_type_trust(self) -> None:
        document = provider_document()
        document["providers"]["primary"]["download"]["maxBytes"] = 16  # type: ignore[index]
        path = self.root / "tiny-limit.json"
        path.write_text(json.dumps(document), encoding="utf-8")
        provider = ig.load_provider_config(path)
        response = json.dumps(
            {"data": [{"url": "https://storage.example.test/error"}]}
        ).encode("utf-8")

        def open_url(request: object, timeout: float) -> BytesResponse:
            return BytesResponse(response if request.get_method() == "POST" else b"<html>error</html>")  # type: ignore[attr-defined]

        with self.assertRaises(ig.ResponseDecodeError):
            ig.ImagesGenerationsClient(provider, urlopen=open_url).generate("prompt")


class NormalizationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = create_test_run()
        self.addCleanup(cleanup_test_run, self.root)
        self.work = self.root / ".work" / "run-1"
        self.destination = self.root / "scenes" / "scene-01.png"

    def test_16_by_9_is_not_padded(self) -> None:
        metadata = ig.normalize_and_store_image(
            png_bytes((1920, 1080)), self.destination, self.work, "scene-01"
        )
        self.assertEqual((metadata.scaled_width, metadata.scaled_height), (1920, 1080))
        self.assertEqual((metadata.offset_x, metadata.offset_y), (0, 0))
        self.assertFalse(metadata.padded)
        with Image.open(self.destination) as image:
            self.assertEqual(image.size, (1920, 1080))
            self.assertEqual(image.mode, "RGB")

    def test_three_by_two_is_contained_and_center_padded(self) -> None:
        metadata = ig.normalize_and_store_image(
            png_bytes((1536, 1024)), self.destination, self.work, "scene-01"
        )
        self.assertEqual((metadata.scaled_width, metadata.scaled_height), (1620, 1080))
        self.assertEqual((metadata.offset_x, metadata.offset_y), (150, 0))
        self.assertTrue(metadata.padded)
        with Image.open(self.destination) as image:
            self.assertEqual(image.getpixel((0, 0)), (245, 235, 215))
            self.assertEqual(image.getpixel((960, 540)), (25, 50, 75))

    def test_transparency_is_composited_on_fixed_paper_background(self) -> None:
        ig.normalize_and_store_image(
            transparent_png_bytes((1920, 1080)),
            self.destination,
            self.work,
            "scene-01",
        )
        with Image.open(self.destination) as image:
            self.assertEqual(image.mode, "RGB")
            self.assertEqual(image.getpixel((960, 540)), (245, 235, 215))

    def test_extreme_aspect_ratio_and_too_small_image_fail(self) -> None:
        with self.assertRaisesRegex(ig.ImageValidationError, "覆盖面积"):
            ig.normalize_and_store_image(
                png_bytes((512, 2048)), self.destination, self.work, "scene-01"
            )
        with self.assertRaisesRegex(ig.ImageValidationError, "不得小于"):
            ig.normalize_and_store_image(
                png_bytes((511, 1024)), self.destination, self.work, "scene-01"
            )

    def test_declared_pixel_bomb_is_rejected_before_full_decode(self) -> None:
        forged = patch_png_dimensions(png_bytes((512, 512)), 7000, 7000)
        with self.assertRaisesRegex(ig.ImageValidationError, "总像素数"):
            ig.normalize_and_store_image(forged, self.destination, self.work, "scene-01")

    def test_failure_does_not_damage_existing_destination(self) -> None:
        self.destination.parent.mkdir(parents=True)
        self.destination.write_bytes(b"existing-valid-result")
        with self.assertRaises(ig.ImageValidationError):
            ig.normalize_and_store_image(
                b"not an image", self.destination, self.work, "scene-01", overwrite=True
            )
        self.assertEqual(self.destination.read_bytes(), b"existing-valid-result")
        self.assertEqual(list(self.work.glob("scene-01*")), [])

    def test_candidate_path_escape_is_rejected_before_creating_outside_directory(self) -> None:
        attempt_root = self.root / ".work" / "attempt"
        outside = self.root / "outside-attempt"
        with self.assertRaisesRegex(ig.ImageValidationError, "attempt"):
            ig.normalize_image_candidate(
                png_bytes((1024, 1024)),
                outside / "candidate.png",
                attempt_root,
                "scene-01",
                attempt_id="scene-01-attempt-0001",
                formal_file="scenes/scene-01.png",
                input_identity_sha256="a" * 64,
                source="b64_json",
                provider_attempts=1,
            )
        self.assertFalse(outside.exists())


class ManifestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = create_test_run()
        self.addCleanup(cleanup_test_run, self.root)
        self.plan = self.root / "planning" / "generation-plan.json"
        self.plan.parent.mkdir(parents=True)
        self.plan.write_text('{"schemaVersion":1}', encoding="utf-8")
        self.config = self.root / "providers.json"
        self.config.write_text(json.dumps(provider_document()), encoding="utf-8")
        self.provider = ig.load_provider_config(self.config)
        self.scenes = [
            {"sceneId": "scene-01", "outputFile": "scene-01.png"},
            {"sceneId": "scene-02", "outputFile": "scene-02.png"},
        ]

    def test_manifest_tracks_runs_scenes_hashes_and_redacts_secret(self) -> None:
        store = ig.ManifestStore.open(self.root, "project-1", self.plan, self.scenes)
        store.begin_run("run-1", self.provider, "2026-08-14T00:00:00+00:00")
        metadata = ig.ImageMetadata(
            1536, 1024, 1920, 1080, 1620, 1080, 150, 0, True, "a" * 64
        )
        prompt = f"safe prompt {self.provider.api_key}"
        scene = store.mark_scene(
            "scene-01",
            status="validated",
            provider=self.provider.name,
            model=self.provider.model,
            prompt=prompt,
            source="b64_json",
            attempts=1,
            image_metadata=metadata,
        )
        store.mark_scene(
            "scene-02",
            status="failed",
            provider=self.provider.name,
            model=self.provider.model,
            prompt="prompt-2",
            attempts=3,
            failure_stage="requesting",
            error=f"401: {self.provider.api_key}",
        )
        store.finish_run("run-1", exit_result=1, completed_at="2026-08-14T00:01:00+00:00")
        store.save()

        serialized = store.manifest_path.read_text(encoding="utf-8")
        self.assertNotIn(self.provider.api_key, serialized)
        self.assertEqual(scene["prompt"], "safe prompt [REDACTED]")
        self.assertEqual(
            scene["promptSha256"],
            ig.sha256_bytes("safe prompt [REDACTED]".encode("utf-8")),
        )
        self.assertEqual(store.data["summary"], {"sceneTotal": 2, "successCount": 1, "failedCount": 1})
        self.assertEqual(store.data["runs"][0]["provider"], "primary")
        self.assertEqual(store.data["scenes"][0]["normalization"]["offset"], {"x": 150, "y": 0})
        self.assertTrue(store.manifest_path.is_file())

    def test_requesting_without_candidate_is_not_downgraded_to_retryable_failed(self) -> None:
        store = ig.ManifestStore.open(self.root, "project-1", self.plan, self.scenes)
        store.begin_run("run-1", self.provider)
        store.prepare_attempt(
            "scene-01",
            attempt_id="scene-01-attempt-0001",
            input_identity_sha256="a" * 64,
            candidate_file=".work/run-1/external-tasks/scene-01/attempt-0001/candidate.png",
            receipt_file=".work/run-1/external-tasks/scene-01/attempt-0001/candidate-receipt.json",
            formal_file="scenes/scene-01.png",
            overwrite=False,
            provider=self.provider.name,
            model=self.provider.model,
            prompt="prompt",
        )
        store.mark_attempt("scene-01", status="requesting", external_outcome="not_started")
        store.mark_attempt(
            "scene-01",
            status="unknown_external_outcome",
            external_outcome="unknown_external_outcome",
            error="禁止自动重试",
        )
        self.assertEqual(store.data["scenes"][0]["status"], "unknown_external_outcome")
        self.assertNotEqual(store.data["scenes"][0]["status"], "failed")

    def test_changed_plan_rejects_existing_manifest(self) -> None:
        store = ig.ManifestStore.open(self.root, "project-1", self.plan, self.scenes)
        store.begin_run("run-1", self.provider)
        store.finish_run("run-1", exit_result=1)
        store.save()
        self.plan.write_text('{"schemaVersion":1,"changed":true}', encoding="utf-8")
        with self.assertRaisesRegex(ig.ManifestError, "已变化"):
            ig.ManifestStore.open(self.root, "project-1", self.plan, self.scenes)


if __name__ == "__main__":
    unittest.main()
