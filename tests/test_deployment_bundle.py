from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "deploy/windows/build_portable_windows.py"
SPEC = importlib.util.spec_from_file_location("build_portable_windows", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


class DeploymentBundleTests(unittest.TestCase):
    def test_windows_scripts_are_normalized_to_crlf(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            batch = root / "start.bat"
            command = root / "nested" / "run.cmd"
            command.parent.mkdir()
            batch.write_bytes("@echo off\nrem 中文\n".encode("utf-8"))
            command.write_bytes(b"@echo off\r\nset A=1\rset B=2\n")

            MODULE.normalize_windows_scripts(root)

            self.assertEqual(
                batch.read_bytes(), "@echo off\r\nrem 中文\r\n".encode("utf-8")
            )
            self.assertEqual(
                command.read_bytes(), b"@echo off\r\nset A=1\r\nset B=2\r\n"
            )

    def test_packaged_batch_sources_use_crlf(self):
        project_root = SCRIPT.parents[2]
        paths = [
            *(project_root / name for name in MODULE.ROOT_FILES if name.endswith(".bat")),
            *(project_root / "deploy/windows").glob("*.bat"),
        ]
        self.assertTrue(paths)
        for path in paths:
            with self.subTest(path=path):
                data = path.read_bytes()
                self.assertIn(b"\r\n", data)
                self.assertNotIn(b"\n", data.replace(b"\r\n", b""))

    def test_build_accepts_archive_or_previously_verified_runtime_directory(self):
        with patch(
            "sys.argv",
            [
                "build_portable_windows.py",
                "--runtime-dir",
                "runtime",
                "--wheelhouse",
                "wheelhouse",
                "--map-source",
                "maps",
            ],
        ):
            args = MODULE.parse_args()
        self.assertEqual(args.runtime_dir, Path("runtime"))
        self.assertIsNone(args.runtime_archive)

    def test_demo_copy_excludes_dat_packages_and_step2_outputs(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            target = root / "target"
            for relative in (
                "event.json",
                "map.png",
                "IF_folder/a.dat",
                "Output EW/Acc/acc/a.dat",
                "packages/result.zip",
            ):
                path = source / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"x")
            MODULE.copy_demo_tree(source, target)
            self.assertTrue((target / "event.json").is_file())
            self.assertTrue((target / "map.png").is_file())
            self.assertFalse((target / "IF_folder/a.dat").exists())
            self.assertFalse((target / "Output EW/Acc/acc/a.dat").exists())
            self.assertFalse((target / "packages/result.zip").exists())

    def test_batch_copy_keeps_download_package_without_loose_dat(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            target = root / "target"
            for relative in ("events/id/event.json", "events/id/IF_folder/a.dat", "packages/id.zip"):
                path = source / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"x")
            MODULE.copy_demo_tree(source, target, include_packages=True)
            self.assertTrue((target / "events/id/event.json").is_file())
            self.assertTrue((target / "packages/id.zip").is_file())
            self.assertFalse((target / "events/id/IF_folder/a.dat").exists())

    def test_event_json_removes_unbundled_downloads_and_local_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "event.json"
            path.write_text(
                json.dumps(
                    {
                        "resultPackage": {"url": "result.zip"},
                        "resultFiles": {
                            "observedCsv": "observed.csv",
                            "inferredDatDirectory": "IF_folder",
                            "step2DatDirectories": {"EW": "Output EW"},
                        },
                        "generatedBy": "/Users/example/Desktop/zsl/build.py",
                    }
                ),
                encoding="utf-8",
            )
            MODULE.sanitize_json(path)
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertIsNone(payload["resultPackage"])
            self.assertEqual(payload["resultFiles"], {"observedCsv": "observed.csv"})
            self.assertEqual(payload["generatedBy"], "bundled-source/build.py")

    def test_batch_event_keeps_download_but_removes_loose_dat_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "frontend/demo/batch/events/id/event.json"
            path.parent.mkdir(parents=True)
            path.write_text(
                json.dumps(
                    {
                        "resultPackage": {"filename": "id.zip", "url": "./demo/batch/packages/id.zip"},
                        "resultFiles": {
                            "observedCsv": "observed.csv",
                            "inferredDatDirectory": "IF_folder",
                        },
                    }
                ),
                encoding="utf-8",
            )
            MODULE.sanitize_json(path)
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload["resultPackage"]["filename"], "id.zip")
            self.assertEqual(payload["resultFiles"], {"observedCsv": "observed.csv"})


if __name__ == "__main__":
    unittest.main()
