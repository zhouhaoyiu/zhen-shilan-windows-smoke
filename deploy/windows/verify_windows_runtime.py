#!/usr/bin/env python3
"""Verify the pinned Windows runtime and bundled application resources."""

from __future__ import annotations

import argparse
import hashlib
from importlib import import_module
from importlib.metadata import PackageNotFoundError, version
import json
import os
from pathlib import Path
import sys


EXPECTED = {
    "python": "3.12.13",
    "numpy": "2.4.4",
    "pandas": "3.0.2",
    "scipy": "1.17.1",
    "matplotlib": "3.10.8",
    "h5py": "3.16.0",
    "obspy": "1.5.0",
    "pyshp": "3.0.12",
    "numba": "0.66.0",
    "llvmlite": "0.48.0",
    "pykooh": "0.5.0",
    "pyrotd": "0.6.1",
    "setuptools": "80.9.0",
}
IMPORTS = {
    "numpy": "numpy",
    "pandas": "pandas",
    "scipy": "scipy",
    "matplotlib": "matplotlib",
    "h5py": "h5py",
    "obspy": "obspy",
    "pyshp": "shapefile",
    "numba": "numba",
    "llvmlite": "llvmlite",
    "pykooh": "pykooh",
    "pyrotd": "pyrotd",
}
MAP_FILES = ("中国_省.geojson", "中国_市.geojson", "中国_县.geojson")
PROJECT_FILES = (
    "local_archive_server.py",
    "process_events_windows.py",
    "csmnc_reader.py",
    "tianditu_250k.py",
    "frontend/index.html",
    "frontend/app.js",
    "frontend/styles.css",
    "frontend/demo/build_demo.py",
    "frontend/demo/world-land.geojson",
    "frontend/demo/events/catalog.json",
    "frontend/demo/batch/catalog.json",
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--map-root", type=Path)
    parser.add_argument(
        "--allow-host-python",
        action="store_true",
        help="在 macOS/Linux 上只验证依赖和资源；正式 Windows 包不得使用",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = args.project_root.resolve()
    map_root = (args.map_root or root / "data" / "admin_geojson").resolve()
    errors: list[str] = []
    warnings: list[str] = []
    actual = {"python": ".".join(map(str, sys.version_info[:3]))}
    if os.name != "nt" and not args.allow_host_python:
        errors.append("正式离线运行时必须在 Windows x64 上验证")

    for package, expected in EXPECTED.items():
        if package == "python":
            found = actual[package]
        else:
            try:
                found = version(package)
            except PackageNotFoundError:
                found = "not-installed"
            actual[package] = found
        if found != expected:
            message = f"{package}: expected {expected}, found {found}"
            if args.allow_host_python and os.name != "nt":
                warnings.append(f"host runtime differs from Windows lock: {message}")
            else:
                errors.append(message)

    os.environ.setdefault("MPLBACKEND", "Agg")
    for distribution, module in IMPORTS.items():
        try:
            import_module(module)
        except Exception as error:  # pragma: no cover - emitted in deployment logs
            errors.append(f"cannot import {distribution}: {error}")

    for relative in PROJECT_FILES:
        if not (root / relative).is_file():
            errors.append(f"missing project file: {relative}")

    maps = {}
    for filename in MAP_FILES:
        path = map_root / filename
        if not path.is_file():
            errors.append(f"missing map file: {path}")
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
            if payload.get("type") != "FeatureCollection" or not payload.get("features"):
                raise ValueError("not a non-empty FeatureCollection")
        except (OSError, ValueError, json.JSONDecodeError) as error:
            errors.append(f"invalid map file {filename}: {error}")
            continue
        maps[filename] = {"bytes": path.stat().st_size, "sha256": sha256(path)}

    map_manifest = map_root.parent / "MAP_RESOURCE_MANIFEST.json"
    if map_manifest.is_file():
        declared = json.loads(map_manifest.read_text(encoding="utf-8-sig"))
        declared_files = {item["filename"]: item for item in declared.get("files", [])}
        for filename, actual_map in maps.items():
            item = declared_files.get(filename)
            if not item or item.get("bytes") != actual_map["bytes"] or item.get("sha256") != actual_map["sha256"]:
                errors.append(f"map manifest mismatch: {filename}")

    if not errors:
        try:
            sys.path.insert(0, str(root))
            import_module("process_events_windows")
            import_module("local_archive_server")
        except Exception as error:  # pragma: no cover - emitted in deployment logs
            errors.append(f"cannot import application entrypoints: {error}")

    report = {
        "status": "ok" if not errors else "failed",
        "platform": sys.platform,
        "architecture": __import__("platform").machine(),
        "versions": actual,
        "maps": maps,
        "warnings": warnings,
        "errors": errors,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
