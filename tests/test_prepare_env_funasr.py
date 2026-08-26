from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import prepare_env  # noqa: E402


class PrepareEnvFunasrTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="prepare-env-funasr-")
        self.root = Path(self.temp.name)
        self.runtime = self.root / "runtime"
        self.workspace = SimpleNamespace(runtime_dir=self.runtime)
        self.workspace_patch = mock.patch.object(
            prepare_env,
            "load_workspace_config",
            return_value=self.workspace,
        )
        self.workspace_patch.start()

    def tearDown(self) -> None:
        self.workspace_patch.stop()
        self.temp.cleanup()

    def _create_receipt(self, *, cache_root: Path | None = None) -> tuple[Path, dict[str, Path]]:
        default_cache, receipt = prepare_env.narration_asr_paths()
        cache = default_cache if cache_root is None else cache_root
        model_paths: dict[str, Path] = {}
        models: list[dict[str, str]] = []
        for contract in prepare_env.NARRATION_ASR_MODELS:
            path = cache / "snapshots" / contract["alias"]
            path.mkdir(parents=True, exist_ok=True)
            (path / "configuration.json").write_text("{}\n", encoding="utf-8")
            model_paths[contract["alias"]] = path.resolve()
            models.append({**contract, "path": str(path.resolve())})
        receipt.parent.mkdir(parents=True, exist_ok=True)
        receipt.write_text(
            json.dumps(
                {
                    "schemaVersion": 1,
                    "contract": prepare_env.NARRATION_ASR_CONTRACT,
                    "cacheRoot": str(cache.resolve()),
                    "models": models,
                }
            ),
            encoding="utf-8",
        )
        return receipt, model_paths

    def test_narration_asr_feature_pins_cpu_runtime_dependencies_and_models(self) -> None:
        self.assertEqual(
            prepare_env.FEATURE_DEPS["narration-asr"],
            {
                "funasr": f"funasr=={prepare_env.FUNASR_VERSION}",
                "modelscope": f"modelscope=={prepare_env.MODELSCOPE_VERSION}",
                "torch": f"torch=={prepare_env.TORCH_VERSION}",
                "torchaudio": f"torchaudio=={prepare_env.TORCHAUDIO_VERSION}",
            },
        )
        self.assertEqual(prepare_env.TORCH_VERSION, prepare_env.TORCHAUDIO_VERSION)
        self.assertEqual(
            [item["alias"] for item in prepare_env.NARRATION_ASR_MODELS],
            ["paraformer-zh", "fsmn-vad", "ct-punc"],
        )
        self.assertEqual(
            {item["requestedRevision"] for item in prepare_env.NARRATION_ASR_MODELS},
            {"master"},
        )

    def test_cache_and_receipt_are_owned_by_current_workspace_runtime(self) -> None:
        cache_root, receipt = prepare_env.narration_asr_paths()
        self.assertEqual(cache_root, self.runtime / "cache" / "funasr-models")
        self.assertEqual(receipt, cache_root / "narration-asr-models.json")
        self.assertNotIn("Yingshu", str(cache_root))

    def test_loader_returns_only_complete_local_paths_from_current_cache(self) -> None:
        receipt, expected = self._create_receipt()
        actual = prepare_env.load_narration_asr_model_paths(receipt_path=receipt)
        self.assertEqual(actual, expected)

    def test_loader_rejects_model_path_outside_current_workspace_cache(self) -> None:
        receipt, _ = self._create_receipt()
        payload = json.loads(receipt.read_text(encoding="utf-8"))
        outside = self.root / "external-model"
        outside.mkdir()
        (outside / "configuration.json").write_text("{}", encoding="utf-8")
        payload["models"][0]["path"] = str(outside.resolve())
        receipt.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "越出当前 workspace 缓存"):
            prepare_env.load_narration_asr_model_paths(receipt_path=receipt)

    def test_check_feature_is_read_only_and_reports_ready_from_receipt(self) -> None:
        receipt, expected = self._create_receipt()
        py = self.root / "runtime" / ".venv" / "Scripts" / "python.exe"
        dependencies = {
            **prepare_env.BASE_DEPS,
            **prepare_env.NARRATION_ASR_DEPS,
        }
        with (
            mock.patch.object(
                prepare_env,
                "ensure_venv",
                return_value=(py, self.root / "pip-cache", self.root / "tmp"),
            ),
            mock.patch.object(
                prepare_env,
                "probe_dependencies",
                return_value={name: True for name in dependencies},
            ) as probe,
            mock.patch.object(prepare_env, "install") as install,
            mock.patch.object(prepare_env, "prepare_narration_asr_models") as prepare,
            mock.patch.object(
                prepare_env,
                "probe_narration_asr_runtime",
                return_value={
                    "available": True,
                    "torchVersion": prepare_env.TORCH_VERSION,
                    "torchaudioVersion": prepare_env.TORCHAUDIO_VERSION,
                },
            ) as runtime_probe,
        ):
            result = prepare_env.main(["--check", "--feature", "narration-asr"])
        self.assertEqual(result, 0)
        probe.assert_called_once()
        self.assertEqual(probe.call_args.args[1], dependencies)
        install.assert_not_called()
        prepare.assert_not_called()
        runtime_probe.assert_called_once_with(py, mock.ANY)
        self.assertEqual(
            prepare_env.load_narration_asr_model_paths(receipt_path=receipt),
            expected,
        )

    def test_check_feature_blocks_when_dependencies_exist_but_models_are_missing(self) -> None:
        py = self.root / "runtime" / ".venv" / "Scripts" / "python.exe"
        dependencies = {
            **prepare_env.BASE_DEPS,
            **prepare_env.NARRATION_ASR_DEPS,
        }
        with (
            mock.patch.object(
                prepare_env,
                "ensure_venv",
                return_value=(py, self.root / "pip-cache", self.root / "tmp"),
            ),
            mock.patch.object(
                prepare_env,
                "probe_dependencies",
                return_value={name: True for name in dependencies},
            ),
            mock.patch.object(prepare_env, "install") as install,
            mock.patch.object(
                prepare_env,
                "probe_narration_asr_runtime",
                return_value={
                    "available": True,
                    "torchVersion": prepare_env.TORCH_VERSION,
                    "torchaudioVersion": prepare_env.TORCHAUDIO_VERSION,
                },
            ) as runtime_probe,
        ):
            result = prepare_env.main(["--check", "--feature", "narration-asr"])
        self.assertEqual(result, 1)
        install.assert_not_called()
        runtime_probe.assert_called_once_with(py, mock.ANY)

    def test_runtime_probe_requires_torchaudio_fbank_and_wav_frontend(self) -> None:
        py = self.root / "python.exe"
        payload = {
            "available": True,
            "torchVersion": prepare_env.TORCH_VERSION,
            "torchaudioVersion": prepare_env.TORCHAUDIO_VERSION,
        }
        completed = subprocess.CompletedProcess(
            [str(py)],
            0,
            stdout=(
                prepare_env._NARRATION_ASR_RUNTIME_PROBE_PREFIX
                + json.dumps(payload)
                + "\n"
            ),
            stderr="",
        )
        with mock.patch.object(prepare_env.subprocess, "run", return_value=completed) as run:
            actual = prepare_env.probe_narration_asr_runtime(py, {"TEMP": str(self.root)})
        self.assertEqual(actual, payload)
        self.assertIn("WavFrontend", run.call_args.args[0][2])
        self.assertIn("kaldi.fbank", run.call_args.args[0][2])

    def test_first_prepare_downloads_fixed_snapshots_and_writes_valid_receipt(self) -> None:
        cache_root, receipt = prepare_env.narration_asr_paths()
        py = self.root / "python.exe"

        def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            requested = json.loads(command[-1])
            models: list[dict[str, str]] = []
            for contract in requested:
                path = cache_root / "snapshots" / contract["alias"]
                path.mkdir(parents=True, exist_ok=True)
                (path / "configuration.json").write_text("{}", encoding="utf-8")
                models.append({**contract, "path": str(path.resolve())})
            stdout = prepare_env._MODEL_PREPARE_RESULT_PREFIX + json.dumps(models)
            return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

        with mock.patch.object(prepare_env.subprocess, "run", side_effect=fake_run) as run:
            resolved = prepare_env.prepare_narration_asr_models(
                py,
                cache_root,
                receipt,
                {"TEMP": str(self.root / "tmp")},
            )
        self.assertEqual(set(resolved), {"paraformer-zh", "fsmn-vad", "ct-punc"})
        self.assertTrue(receipt.is_file())
        run.assert_called_once()
        called_env = run.call_args.kwargs["env"]
        self.assertEqual(called_env["MODELSCOPE_CACHE"], str(cache_root))
        self.assertEqual(called_env["FUNASR_HOME"], str(cache_root))

        with mock.patch.object(prepare_env.subprocess, "run") as second_run:
            reused = prepare_env.prepare_narration_asr_models(
                py,
                cache_root,
                receipt,
                {},
            )
        second_run.assert_not_called()
        self.assertEqual(reused, resolved)


if __name__ == "__main__":
    unittest.main()
