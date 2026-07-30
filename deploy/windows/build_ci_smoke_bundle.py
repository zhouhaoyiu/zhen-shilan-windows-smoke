#!/usr/bin/env python3
"""Build a small Windows deployment fixture for GitHub Actions smoke tests."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import shutil


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT = PROJECT_ROOT / "artifacts" / "ZhenShiLan-Windows-smoke"

ROOT_FILES = (
    "local_archive_server.py",
    "process_events_windows.py",
    "csmnc_reader.py",
    "tianditu_250k.py",
    "event_overrides.example.json",
    "start_windows.bat",
    "run_windows.bat",
    "scan_windows.bat",
    "START_ZHEN_SHILAN.bat",
    "PROCESS_DATA_FOLDER.bat",
    "WINDOWS_README.md",
)
FRONTEND_FILES = (
    "frontend/index.html",
    "frontend/app.js",
    "frontend/data.js",
    "frontend/styles.css",
    "frontend/artifact-layout.js",
    "frontend/attenuation-bins.js",
    "frontend/target-config.js",
    "frontend/demo/knet-demo.json",
    "frontend/demo/japan-prefectures.geojson",
    "frontend/demo/world-land.geojson",
)
MAP_FILES = ("中国_省.geojson", "中国_市.geojson", "中国_县.geojson")
LOCAL_PATH = re.compile(r"(?:/Users/[^/]+|/Volumes/[^/]+)(?:/[^\s\"']+)+")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def copy_file(relative: str, destination: Path) -> None:
    source = PROJECT_ROOT / relative
    if not source.is_file():
        raise FileNotFoundError(source)
    target = destination / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)


def normalize_windows_scripts(root: Path) -> None:
    for pattern in ("*.bat", "*.cmd"):
        for path in root.rglob(pattern):
            data = path.read_bytes().replace(b"\r\n", b"\n").replace(b"\r", b"\n")
            path.write_bytes(data.replace(b"\n", b"\r\n"))


def sanitize_json(path: Path) -> None:
    payload = json.loads(path.read_text(encoding="utf-8"))

    def scrub(value: object) -> object:
        if isinstance(value, dict):
            return {key: scrub(item) for key, item in value.items()}
        if isinstance(value, list):
            return [scrub(item) for item in value]
        if isinstance(value, str):
            return LOCAL_PATH.sub("bundled-source", value)
        return value

    path.write_text(
        json.dumps(scrub(payload), ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )


def write_catalogs(destination: Path) -> None:
    event_catalog = {
        "schemaVersion": 1,
        "defaultEventId": "0010061330",
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "eventCount": 1,
        "selection": {
            "purpose": "GitHub Actions Windows deployment smoke fixture",
        },
        "events": [
            {
                "eventId": "0010061330",
                "title": "2000年鸟取县西部地震",
                "originTime": "2000-10-06 13:30:00",
                "magnitude": 7.3,
                "stationCount": 34,
                "latitude": 35.278,
                "longitude": 133.345,
                "depthKm": 11.0,
                "url": "./demo/knet-demo.json",
            }
        ],
    }
    batch_catalog = {
        "schemaVersion": 1,
        "source": "GitHub Actions smoke fixture",
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "defaultEventId": None,
        "defaultBoundaryUrl": "./demo/world-land.geojson",
        "eventCount": 0,
        "events": [],
    }
    catalog_root = destination / "frontend/demo"
    (catalog_root / "events").mkdir(parents=True, exist_ok=True)
    (catalog_root / "batch").mkdir(parents=True, exist_ok=True)
    (catalog_root / "events/catalog.json").write_text(
        json.dumps(event_catalog, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (catalog_root / "batch/catalog.json").write_text(
        json.dumps(batch_catalog, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def build(destination: Path) -> dict[str, object]:
    destination = destination.resolve()
    shutil.rmtree(destination, ignore_errors=True)
    destination.mkdir(parents=True)

    for relative in (*ROOT_FILES, *FRONTEND_FILES):
        copy_file(relative, destination)
    for relative in ("frontend/assets", "frontend/vendor", "frontend/demo/site_data"):
        shutil.copytree(
            PROJECT_ROOT / relative,
            destination / relative,
            dirs_exist_ok=True,
            ignore=shutil.ignore_patterns(".DS_Store"),
        )

    deploy_target = destination / "deploy/windows"
    deploy_target.mkdir(parents=True, exist_ok=True)
    for name in (
        "prepare_runtime.bat",
        "requirements-windows.lock.txt",
        "verify_windows_runtime.py",
    ):
        shutil.copy2(PROJECT_ROOT / "deploy/windows" / name, deploy_target / name)

    map_target = destination / "data/admin_geojson"
    map_target.mkdir(parents=True)
    for name in MAP_FILES:
        source = PROJECT_ROOT / "deploy/windows/data/admin_geojson" / name
        if not source.is_file():
            raise FileNotFoundError(source)
        shutil.copy2(source, map_target / name)

    write_catalogs(destination)
    for path in destination.rglob("*.json"):
        sanitize_json(path)
    (destination / "workspace/imports").mkdir(parents=True)
    (destination / "workspace/runtime-cache").mkdir(parents=True)
    normalize_windows_scripts(destination)

    manifest_lines: list[str] = []
    for path in sorted(item for item in destination.rglob("*") if item.is_file()):
        relative = path.relative_to(destination).as_posix()
        manifest_lines.append(f"{sha256(path)}  {relative}")
    (destination / "SMOKE_MANIFEST.sha256").write_text(
        "\n".join(manifest_lines) + "\n",
        encoding="utf-8",
    )
    summary = {
        "status": "ok",
        "bundle": destination.name,
        "files": len(manifest_lines),
        "bytes": sum(
            path.stat().st_size for path in destination.rglob("*") if path.is_file()
        ),
        "eventCount": 1,
        "mapCount": len(MAP_FILES),
    }
    (destination / "SMOKE_BUILD.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args()


def main() -> int:
    summary = build(parse_args().output)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
