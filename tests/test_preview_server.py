from __future__ import annotations

import json
import sys
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
TESTS = ROOT / "tests"
for entry in (SCRIPTS, TESTS):
    if str(entry) not in sys.path:
        sys.path.insert(0, str(entry))

import project_workspace  # noqa: E402
import render_timing  # noqa: E402
import serve_preview  # noqa: E402
from test_annotation_batch import AnnotationBatchFixture  # noqa: E402


class PreviewServerTests(AnnotationBatchFixture):
    def setUp(self) -> None:
        super().setUp()
        self.project, _audio, _identity = self.make_project(2)
        context = render_timing.build_formal_validation_context(self.project)
        for scene in self.project.plan["scenes"]:
            project_workspace.write_json_atomic(
                self.project.root / "scenes" / f"{scene['sceneId']}.annotation.json",
                self.annotation(self.project, scene["sceneId"], context),
            )
        self.project = project_workspace.load_project(self.project.root)
        config = project_workspace.WorkspaceConfig(
            root=self.workspace_root,
            config_path=self.workspace_root / "fixture-config.json",
        )
        self.workspace = project_workspace.ProjectWorkspace(config)
        self.token = "fixture-preview-token-that-is-long-enough"
        self.server = serve_preview.PreviewHTTPServer(
            ("127.0.0.1", 0),
            serve_preview.PreviewApplication(self.workspace, self.token),
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        super().tearDown()

    def json_get(self, path: str):
        with urllib.request.urlopen(self.base + path, timeout=2) as response:
            return response.status, json.loads(response.read().decode("utf-8"))

    def put_annotation(self, scene_id: str, value: dict, *, token: str | None):
        headers = {"Content-Type": "application/json"}
        if token is not None:
            headers["X-Preview-Token"] = token
        request = urllib.request.Request(
            f"{self.base}/api/projects/{self.project.project_id}/scenes/{scene_id}/annotation",
            data=json.dumps(value, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="PUT",
        )
        return urllib.request.urlopen(request, timeout=2)

    def test_health_page_and_project_url_api_are_project_bound(self) -> None:
        status, health = self.json_get("/api/health")
        self.assertEqual(status, 200)
        self.assertEqual(health["contractVersion"], serve_preview.SERVER_CONTRACT)
        self.assertNotIn("workspaceRoot", health)

        with urllib.request.urlopen(self.base + "/preview", timeout=2) as response:
            html = response.read().decode("utf-8")
        self.assertIn("loadRemoteProject", html)
        self.assertIn("X-Preview-Token", html)
        self.assertIn("Agent 提供的项目预览链接会自动载入", html)

        status, summary = self.json_get(f"/api/projects/{self.project.project_id}")
        self.assertEqual(status, 200)
        self.assertEqual(summary["projectId"], self.project.project_id)
        self.assertEqual(summary["readySceneCount"], 2)
        self.assertTrue(summary["allScenesReady"])
        self.assertFalse(summary["approvalWritten"])
        self.assertTrue(summary["userConfirmationRequired"])
        self.assertTrue(all(scene["ready"] for scene in summary["scenes"]))

        with urllib.request.urlopen(self.base + summary["scenes"][0]["imageUrl"], timeout=2) as response:
            self.assertEqual(response.headers.get_content_type(), "image/png")
            self.assertGreater(len(response.read()), 100)

    def test_annotation_get_and_token_protected_validated_save(self) -> None:
        annotation_url = (
            f"/api/projects/{self.project.project_id}/scenes/scene-01/annotation"
        )
        _status, original = self.json_get(annotation_url)
        original["elements"][0]["label"] = "用户手工调整后的主体"

        with self.assertRaises(urllib.error.HTTPError) as denied:
            self.put_annotation("scene-01", original, token=None)
        self.assertEqual(denied.exception.code, 403)

        with self.put_annotation("scene-01", original, token=self.token) as response:
            result = json.loads(response.read().decode("utf-8"))
        self.assertEqual(result["status"], "saved_current_technical")
        self.assertFalse(result["approvalWritten"])
        self.assertTrue(result["confirmationInvalidated"])
        persisted = json.loads(
            (self.project.root / "scenes" / "scene-01.annotation.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(persisted["elements"][0]["label"], "用户手工调整后的主体")

    def test_invalid_annotation_does_not_replace_current_file(self) -> None:
        path = self.project.root / "scenes" / "scene-01.annotation.json"
        before = project_workspace.sha256_file(path)
        invalid = json.loads(path.read_text(encoding="utf-8"))
        invalid["sceneDurationMs"] += 1
        with self.assertRaises(urllib.error.HTTPError) as rejected:
            self.put_annotation("scene-01", invalid, token=self.token)
        self.assertEqual(rejected.exception.code, 422)
        self.assertEqual(project_workspace.sha256_file(path), before)

    def test_unknown_project_and_raw_path_are_not_resolved(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as missing:
            self.json_get("/api/projects/00000000-0000-4000-8000-000000000000")
        self.assertEqual(missing.exception.code, 404)
        with self.assertRaises(urllib.error.HTTPError) as escaped:
            self.json_get("/api/projects/D%3A%5Coutside")
        self.assertEqual(escaped.exception.code, 404)

    def test_generated_edit_url_uses_project_id_and_fragment_token(self) -> None:
        url = serve_preview.build_preview_url(
            host="127.0.0.1",
            port=8765,
            project_id=self.project.project_id,
            mode="edit",
            token=self.token,
            scene_id="scene-02",
        )
        self.assertIn(f"project={self.project.project_id}", url)
        self.assertIn("scene=scene-02", url)
        self.assertIn("mode=edit", url)
        self.assertTrue(url.endswith("#token=" + self.token))
        self.assertNotIn(str(self.project.root), url)

    def test_agent_url_gate_deeply_validates_every_current_annotation(self) -> None:
        self.assertEqual(serve_preview.validate_project_for_preview(self.project), 2)
        path = self.project.root / "scenes" / "scene-02.annotation.json"
        stale = json.loads(path.read_text(encoding="utf-8"))
        stale["timingPlanSha256"] = "0" * 64
        project_workspace.write_json_atomic(path, stale)
        with self.assertRaisesRegex(render_timing.RenderTimingError, "stale"):
            serve_preview.validate_project_for_preview(self.project)

    def test_ensure_success_writes_reloadable_short_lived_formal_receipt(self) -> None:
        token_path = serve_preview._token_path(self.workspace)
        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text(self.token, encoding="utf-8")
        url, summary = serve_preview.ensure_server_and_url(
            self.workspace,
            self.project.root,
            port=self.server.server_address[1],
        )
        self.assertTrue(url.startswith(self.base + "/preview?"))
        receipt_path = self.project.root / summary["formalValidationReceipt"]
        self.assertTrue(receipt_path.is_file())
        context = render_timing.load_formal_validation_context_receipt(
            self.project,
            receipt_path,
            expected_run_id=summary["formalValidationRunId"],
        )
        self.assertEqual(context.scene_order, ("scene-01", "scene-02"))
        self.assertIsNotNone(context.receipt_expires_at)

    def test_skill_requires_agent_to_emit_verified_concrete_url(self) -> None:
        skill = (ROOT / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("项目预览链接交付合同", skill)
        self.assertIn("serve_preview.py --ensure --project <项目根目录>", skill)
        self.assertIn("必须包含命令返回的完整、可点击 `PREVIEW_URL`", skill)
        self.assertIn("只报告代码已修改", skill)
        self.assertIn("打开后自动载入当前项目、无需手动导入", skill)


if __name__ == "__main__":
    unittest.main()
