#!/usr/bin/env python3
"""Serve project-bound whiteboard annotation previews on loopback only.

The static preview page cannot read a local project from a URL without a
directory-picker grant.  This module exposes a deliberately narrow HTTP API:
validated projects are addressed by projectId, scene files are resolved from
the frozen generation plan, and annotation writes require a local control
token plus full current-timing validation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import os
import secrets
import subprocess
import sys
import time
import uuid
import urllib.error
import urllib.request
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import parse_qs, quote, unquote, urlencode, urlsplit

from project_workspace import (
    Project,
    ProjectValidationError,
    ProjectWorkspace,
    WorkspaceError,
    sha256_file,
    write_json_atomic,
)
from render_timing import (
    RenderTimingError,
    build_formal_validation_context,
    load_formal_validation_context_receipt,
    resolve_formal_scenes,
    validate_annotation,
    validate_formal_context_current,
    write_formal_validation_context_receipt,
)


SKILL_ROOT = Path(__file__).resolve().parent.parent
ASSETS_DIR = SKILL_ROOT / "assets"
PREVIEW_HTML = ASSETS_DIR / "preview.html"
DRAWING_HAND = ASSETS_DIR / "drawing-hand.png"
PREVIEW_SCHEMA_VERSION = 1
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
MAX_ANNOTATION_BYTES = 2 * 1024 * 1024
TOKEN_FILE_NAME = "preview-server.token"
LOG_FILE_NAME = "preview-server.log"


class PreviewServerError(RuntimeError):
    """A safe preview request or server lifecycle operation failed."""


def _workspace_identity(workspace: ProjectWorkspace) -> str:
    value = str(workspace.config.root.resolve()).casefold().encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def _token_identity(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _token_path(workspace: ProjectWorkspace) -> Path:
    return workspace.config.runtime_dir / TOKEN_FILE_NAME


def ensure_control_token(workspace: ProjectWorkspace) -> str:
    runtime = workspace.config.runtime_dir
    runtime.mkdir(parents=True, exist_ok=True)
    path = _token_path(workspace)
    if path.exists():
        token = path.read_text(encoding="utf-8").strip()
        if len(token) < 32 or any(character.isspace() for character in token):
            raise PreviewServerError("预览服务控制令牌无效，请先停止服务并移走令牌文件")
        return token

    token = secrets.token_urlsafe(32)
    try:
        with path.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(token + "\n")
    except FileExistsError:
        return ensure_control_token(workspace)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return token


@dataclass(frozen=True)
class PreviewScene:
    scene_id: str
    name: str
    image_path: Path
    annotation_path: Path


class PreviewCatalog:
    def __init__(self, workspace: ProjectWorkspace):
        self.workspace = workspace

    def find_project(self, project_id: str) -> Project:
        matches: list[Project] = []
        projects_dir = self.workspace.config.projects_dir
        if not projects_dir.is_dir():
            raise PreviewServerError("工作区 projects 目录不存在")
        for directory in sorted(projects_dir.iterdir(), key=lambda item: item.name.casefold()):
            if not directory.is_dir():
                continue
            try:
                project = self.workspace.load_project(directory)
            except (WorkspaceError, ProjectValidationError, OSError, ValueError):
                continue
            if project.project_id == project_id:
                matches.append(project)
        if not matches:
            raise PreviewServerError("项目不存在或未通过项目校验")
        if len(matches) != 1:
            raise PreviewServerError("检测到重复 projectId，拒绝选择不确定项目")
        return matches[0]

    def load_project_path(self, project_root: str | Path) -> Project:
        return self.workspace.load_project(project_root)

    @staticmethod
    def scenes(project: Project) -> tuple[PreviewScene, ...]:
        result: list[PreviewScene] = []
        for spec in project.plan["scenes"]:
            output_file = spec["outputFile"]
            stem = Path(output_file).stem
            result.append(
                PreviewScene(
                    scene_id=spec["sceneId"],
                    name=stem,
                    image_path=project.scenes_dir / output_file,
                    annotation_path=project.scenes_dir / f"{stem}.annotation.json",
                )
            )
        return tuple(result)

    def project_summary(self, project: Project) -> dict[str, Any]:
        scenes: list[dict[str, Any]] = []
        ready_count = 0
        project_part = quote(project.project_id, safe="")
        for scene in self.scenes(project):
            ready = scene.image_path.is_file() and scene.annotation_path.is_file()
            if ready:
                ready_count += 1
            scene_part = quote(scene.scene_id, safe="")
            base = f"/api/projects/{project_part}/scenes/{scene_part}"
            scenes.append(
                {
                    "sceneId": scene.scene_id,
                    "name": scene.name,
                    "ready": ready,
                    "imageReady": scene.image_path.is_file(),
                    "annotationReady": scene.annotation_path.is_file(),
                    "imageUrl": f"{base}/image",
                    "annotationUrl": f"{base}/annotation",
                }
            )
        return {
            "schemaVersion": PREVIEW_SCHEMA_VERSION,
            "kind": "project-preview",
            "projectId": project.project_id,
            "projectName": project.metadata["projectName"],
            "sceneCount": len(scenes),
            "readySceneCount": ready_count,
            "allScenesReady": ready_count == len(scenes) and bool(scenes),
            "scenes": scenes,
            "approvalWritten": False,
            "userConfirmationRequired": True,
        }

    def scene(self, project: Project, scene_id: str) -> PreviewScene:
        matches = [scene for scene in self.scenes(project) if scene.scene_id == scene_id]
        if len(matches) != 1:
            raise PreviewServerError("场景不存在或 sceneId 不唯一")
        return matches[0]

    def save_annotation(
        self,
        project: Project,
        scene_id: str,
        annotation: Mapping[str, Any],
    ) -> dict[str, Any]:
        scene = self.scene(project, scene_id)
        context = build_formal_validation_context(project)
        timing_scene = next(
            (item for item in project.timing_plan["scenes"] if item["sceneId"] == scene_id),
            None,
        )
        if timing_scene is None:
            raise PreviewServerError("current timing plan 缺少目标场景")
        validated = validate_annotation(
            annotation,
            project=project,
            timing_scene=timing_scene,
            timing_plan_sha256=context.timing_plan_sha256,
            render_profile_sha256=context.render_profile_sha256,
            active_timeline=context.active_timeline,
            audio_sha256=context.audio_sha256,
            allow_v1_disabled_compat=False,
        )
        validate_formal_context_current(project, context)
        write_json_atomic(scene.annotation_path, validated)
        return {
            "status": "saved_current_technical",
            "sceneId": scene_id,
            "annotationSha256": sha256_file(scene.annotation_path),
            "approvalWritten": False,
            "confirmationInvalidated": True,
            "userConfirmationRequired": True,
            "message": "标注已保存并通过 current 技术校验；请回到聊天重新确认标注与预览。",
        }


class PreviewApplication:
    def __init__(self, workspace: ProjectWorkspace, write_token: str):
        self.workspace = workspace
        self.catalog = PreviewCatalog(workspace)
        self.write_token = write_token
        self.workspace_identity = _workspace_identity(workspace)
        self.token_identity = _token_identity(write_token)

    def health(self) -> dict[str, Any]:
        return {
            "schemaVersion": PREVIEW_SCHEMA_VERSION,
            "kind": "preview-server-health",
            "status": "ok",
            "workspaceIdentitySha256": self.workspace_identity,
            "tokenIdentitySha256": self.token_identity,
        }


class PreviewHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], application: PreviewApplication):
        self.application = application
        super().__init__(address, PreviewRequestHandler)


class PreviewRequestHandler(BaseHTTPRequestHandler):
    server: PreviewHTTPServer

    def log_message(self, format: str, *args: object) -> None:
        # Do not persist project ids, query strings, or edit tokens in request logs.
        return

    def _safe_host(self) -> bool:
        host = (self.headers.get("Host") or "").split(":", 1)[0].strip("[]").lower()
        return host in {"127.0.0.1", "localhost"}

    def _send_bytes(self, status: int, payload: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self' blob:; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; connect-src 'self'")
        self.end_headers()
        self.wfile.write(payload)

    def _send_json(self, status: int, value: Mapping[str, Any]) -> None:
        payload = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self._send_bytes(status, payload, "application/json; charset=utf-8")

    def _error(self, status: int, message: str) -> None:
        self._send_json(status, {"error": message, "approvalWritten": False})

    def _parts(self) -> list[str]:
        return [unquote(part) for part in urlsplit(self.path).path.split("/") if part]

    def _project_and_scene(self, parts: list[str]) -> tuple[Project, PreviewScene]:
        if len(parts) != 6 or parts[:2] != ["api", "projects"] or parts[3] != "scenes":
            raise PreviewServerError("无效的场景 API 路径")
        project = self.server.application.catalog.find_project(parts[2])
        return project, self.server.application.catalog.scene(project, parts[4])

    def do_GET(self) -> None:  # noqa: N802
        if not self._safe_host():
            self._error(400, "Host 不允许")
            return
        parts = self._parts()
        try:
            if parts in ([], ["preview"]):
                self._send_bytes(200, PREVIEW_HTML.read_bytes(), "text/html; charset=utf-8")
                return
            if parts in (["assets", "drawing-hand.png"], ["drawing-hand.png"]):
                self._send_bytes(200, DRAWING_HAND.read_bytes(), "image/png")
                return
            if parts == ["api", "health"]:
                self._send_json(200, self.server.application.health())
                return
            if len(parts) == 3 and parts[:2] == ["api", "projects"]:
                project = self.server.application.catalog.find_project(parts[2])
                self._send_json(200, self.server.application.catalog.project_summary(project))
                return
            if len(parts) == 6 and parts[:2] == ["api", "projects"] and parts[3] == "scenes":
                project, scene = self._project_and_scene(parts)
                kind = parts[5]
                if kind == "image":
                    if not scene.image_path.is_file():
                        self._error(404, "场景图片不存在")
                        return
                    content_type = mimetypes.guess_type(scene.image_path.name)[0] or "application/octet-stream"
                    self._send_bytes(200, scene.image_path.read_bytes(), content_type)
                    return
                if kind == "annotation":
                    if not scene.annotation_path.is_file():
                        self._error(404, "场景 annotation 不存在")
                        return
                    self._send_bytes(200, scene.annotation_path.read_bytes(), "application/json; charset=utf-8")
                    return
            self._error(404, "接口不存在")
        except (PreviewServerError, WorkspaceError, ProjectValidationError, RenderTimingError) as exc:
            self._error(404, str(exc))
        except OSError as exc:
            self._error(500, f"本地文件读取失败: {exc.__class__.__name__}")

    def do_PUT(self) -> None:  # noqa: N802
        if not self._safe_host():
            self._error(400, "Host 不允许")
            return
        if not secrets.compare_digest(
            self.headers.get("X-Preview-Token") or "",
            self.server.application.write_token,
        ):
            self._error(403, "缺少或无效的预览编辑令牌")
            return
        parts = self._parts()
        if len(parts) != 6 or parts[5] != "annotation":
            self._error(404, "只允许保存场景 annotation")
            return
        try:
            length = int(self.headers.get("Content-Length") or "0")
            if length <= 0 or length > MAX_ANNOTATION_BYTES:
                raise PreviewServerError("annotation 请求大小无效")
            raw = self.rfile.read(length)
            value = json.loads(raw.decode("utf-8"))
            if not isinstance(value, dict):
                raise PreviewServerError("annotation 必须是 JSON 对象")
            project, _scene = self._project_and_scene(parts)
            result = self.server.application.catalog.save_annotation(project, parts[4], value)
            self._send_json(200, result)
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._error(400, "annotation 不是有效 UTF-8 JSON")
        except (PreviewServerError, WorkspaceError, ProjectValidationError, RenderTimingError) as exc:
            self._error(422, str(exc))
        except OSError as exc:
            self._error(500, f"annotation 保存失败: {exc.__class__.__name__}")


def build_preview_url(
    *,
    host: str,
    port: int,
    project_id: str,
    mode: str,
    token: str | None,
    scene_id: str | None = None,
) -> str:
    query: dict[str, str] = {"project": project_id, "mode": mode}
    if scene_id:
        query["scene"] = scene_id
    url = f"http://{host}:{port}/preview?{urlencode(query)}"
    if mode == "edit":
        if not token:
            raise PreviewServerError("编辑链接必须包含预览编辑令牌")
        url += "#" + urlencode({"token": token})
    return url


def validate_project_for_preview(project: Project) -> int:
    """Deeply bind every formal annotation before an Agent may emit its URL."""
    scene_ids = [scene["sceneId"] for scene in project.plan["scenes"]]
    if not scene_ids:
        raise PreviewServerError("项目没有可预览场景")
    context = build_formal_validation_context(project)
    resolved = resolve_formal_scenes(project, scene_ids, context=context)
    validate_formal_context_current(project, context)
    return len(resolved)


def _read_health(host: str, port: int, timeout: float = 1.0) -> dict[str, Any] | None:
    request = urllib.request.Request(f"http://{host}:{port}/api/health", method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except (OSError, urllib.error.URLError, json.JSONDecodeError, UnicodeDecodeError):
        return None


def _spawn_server(config_path: Path, host: str, port: int, log_path: Path) -> None:
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--serve",
        "--config",
        str(config_path),
        "--host",
        host,
        "--port",
        str(port),
    ]
    creationflags = 0
    popen_kwargs: dict[str, Any] = {"stdin": subprocess.DEVNULL, "close_fds": True}
    if os.name == "nt":
        creationflags = (
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | getattr(subprocess, "DETACHED_PROCESS", 0)
            | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        )
        popen_kwargs["creationflags"] = creationflags
    else:
        popen_kwargs["start_new_session"] = True
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("ab") as log:
        subprocess.Popen(command, stdout=log, stderr=log, **popen_kwargs)


def ensure_server_and_url(
    workspace: ProjectWorkspace,
    project_root: str | Path,
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    mode: str = "edit",
    scene_id: str | None = None,
    startup_timeout: float = 8.0,
) -> tuple[str, dict[str, Any]]:
    if host != DEFAULT_HOST:
        raise PreviewServerError("预览服务只允许绑定 127.0.0.1")
    project = workspace.load_project(project_root)
    catalog = PreviewCatalog(workspace)
    summary = catalog.project_summary(project)
    if not summary["allScenesReady"]:
        raise PreviewServerError(
            f"项目预览尚未就绪: {summary['readySceneCount']}/{summary['sceneCount']} 幕图片与 annotation 成对"
        )
    # The ensure path is a coordinator validation boundary. Deeply validate
    # once, then persist only a short-lived technical receipt; never write
    # approval/identity/manifest state.
    scene_ids = [scene["sceneId"] for scene in project.plan["scenes"]]
    if not scene_ids:
        raise PreviewServerError("项目没有可预览场景")
    base_context = build_formal_validation_context(project)
    validated_formals = resolve_formal_scenes(project, scene_ids, context=base_context)
    validate_formal_context_current(project, base_context)
    current_count = len(validated_formals)
    summary["technicalCurrentSceneCount"] = current_count
    if scene_id and scene_id not in {scene.scene_id for scene in catalog.scenes(project)}:
        raise PreviewServerError("请求定位的 sceneId 不属于当前项目")

    token = ensure_control_token(workspace)
    expected_workspace = _workspace_identity(workspace)
    expected_token = _token_identity(token)
    health = _read_health(host, port)
    if health is None:
        _spawn_server(
            workspace.config.config_path,
            host,
            port,
            workspace.config.runtime_dir / LOG_FILE_NAME,
        )
        deadline = time.monotonic() + startup_timeout
        while time.monotonic() < deadline:
            time.sleep(0.1)
            health = _read_health(host, port)
            if health is not None:
                break
    if health is None:
        raise PreviewServerError("本地预览服务启动失败或端口不可用")
    if (
        health.get("schemaVersion") != PREVIEW_SCHEMA_VERSION
        or health.get("kind") != "preview-server-health"
        or health.get("workspaceIdentitySha256") != expected_workspace
        or health.get("tokenIdentitySha256") != expected_token
    ):
        raise PreviewServerError("端口已被其他服务或不同工作区的预览服务占用")

    request = urllib.request.Request(
        f"http://{host}:{port}/api/projects/{quote(project.project_id, safe='')}",
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=2.0) as response:
            live_summary = json.loads(response.read().decode("utf-8"))
    except (OSError, urllib.error.URLError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise PreviewServerError("预览服务已启动，但项目 API 验证失败") from exc
    if not live_summary.get("allScenesReady"):
        raise PreviewServerError("预览服务返回的项目场景不完整")
    run_id = f"preview-{uuid.uuid4().hex[:16]}"
    _receipt_context, receipt_path = write_formal_validation_context_receipt(
        project,
        base_context,
        run_id=run_id,
        validated_formals=list(validated_formals),
    )
    load_formal_validation_context_receipt(
        project,
        receipt_path,
        expected_run_id=run_id,
    )
    live_summary["technicalCurrentSceneCount"] = current_count
    live_summary["formalValidationReceipt"] = receipt_path.relative_to(project.root).as_posix()
    live_summary["formalValidationRunId"] = run_id
    url = build_preview_url(
        host=host,
        port=port,
        project_id=project.project_id,
        mode=mode,
        token=token if mode == "edit" else None,
        scene_id=scene_id,
    )
    return url, live_summary


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="启动或复用本地白板标注预览服务")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--serve", action="store_true", help="前台运行 loopback 预览服务")
    action.add_argument("--ensure", action="store_true", help="后台启动/复用服务并输出已验证项目地址")
    parser.add_argument("--config", type=Path, default=SKILL_ROOT / "config" / "workspace.local.json")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--project", type=Path, help="--ensure 所需的项目根目录")
    parser.add_argument("--scene-id", help="可选：打开后自动定位该幕")
    parser.add_argument("--mode", choices=("view", "edit"), default="edit")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        workspace = ProjectWorkspace.from_config(args.config)
        if args.host != DEFAULT_HOST:
            raise PreviewServerError("预览服务只允许绑定 127.0.0.1")
        if args.port < 1 or args.port > 65535:
            raise PreviewServerError("port 必须位于 1..65535")
        if args.serve:
            token = ensure_control_token(workspace)
            server = PreviewHTTPServer((args.host, args.port), PreviewApplication(workspace, token))
            server.serve_forever(poll_interval=0.25)
            return 0
        if args.project is None:
            raise PreviewServerError("--ensure 必须提供 --project")
        url, summary = ensure_server_and_url(
            workspace,
            args.project,
            host=args.host,
            port=args.port,
            mode=args.mode,
            scene_id=args.scene_id,
        )
        print("PREVIEW_READY=PASS")
        print(f"PROJECT_ID={summary['projectId']}")
        print(f"READY_SCENES={summary['readySceneCount']}/{summary['sceneCount']}")
        print(f"CURRENT_SCENES={summary['technicalCurrentSceneCount']}/{summary['sceneCount']}")
        print(f"PREVIEW_URL={url}")
        return 0
    except KeyboardInterrupt:
        return 130
    except (PreviewServerError, WorkspaceError, ProjectValidationError, RenderTimingError, OSError) as exc:
        print(f"PREVIEW_READY=FAIL\nERROR={exc}", file=sys.stderr)
        return 5


if __name__ == "__main__":
    raise SystemExit(main())
