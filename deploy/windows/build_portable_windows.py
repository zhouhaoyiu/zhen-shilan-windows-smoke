#!/usr/bin/env python3
"""Assemble the Windows x64 offline bundle from verified local artifacts."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import tarfile
import zipfile


PROJECT_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_NAME = "ZhenShiLan-Windows-x64-20260730-r4"
EXPECTED_BATCH_EVENTS = 74
EXPECTED_BATCH_YEARS = {"2025": 20, "2026": 54}
MAP_FILES = ("中国_省.geojson", "中国_市.geojson", "中国_县.geojson")
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
    "frontend/demo/build_demo.py",
    "frontend/demo/knet-demo.json",
    "frontend/demo/japan-prefectures.geojson",
    "frontend/demo/world-land.geojson",
)
LOCAL_PATH = re.compile(r"(?:/Users/[^/]+|/Volumes/[^/]+)(?:/[^\s\"']+)+")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def copy_file(relative: str, stage: Path) -> None:
    source = PROJECT_ROOT / relative
    if not source.is_file():
        raise FileNotFoundError(source)
    destination = stage / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


def normalize_windows_scripts(root: Path) -> None:
    """Write batch scripts with the CRLF line endings required by cmd.exe."""
    for pattern in ("*.bat", "*.cmd"):
        for path in root.rglob(pattern):
            data = path.read_bytes().replace(b"\r\n", b"\n").replace(b"\r", b"\n")
            path.write_bytes(data.replace(b"\n", b"\r\n"))


def copy_demo_tree(source: Path, destination: Path, *, include_packages: bool = False) -> None:
    """Copy browser assets, optionally retaining downloadable result packages."""
    for path in source.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(source)
        if any(part in {"IF_folder", "Output EW", "Output NS", "Output UD"} for part in relative.parts):
            continue
        if "packages" in relative.parts and not include_packages:
            continue
        if path.suffix.lower() == ".dat" or path.name == ".DS_Store":
            continue
        if path.suffix.lower() == ".zip" and not (include_packages and "packages" in relative.parts):
            continue
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)


def sanitize_json(path: Path) -> None:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if path.name in {"event.json", "knet-demo.json"}:
        keep_result_package = "batch" in path.parts and path.name == "event.json"
        if not keep_result_package:
            payload["resultPackage"] = None
        result_files = payload.get("resultFiles")
        if isinstance(result_files, dict):
            result_files.pop("inferredDatDirectory", None)
            result_files.pop("step2DatDirectories", None)

    def clean(value):
        if isinstance(value, dict):
            return {key: clean(item) for key, item in value.items()}
        if isinstance(value, list):
            return [clean(item) for item in value]
        if isinstance(value, str):
            return LOCAL_PATH.sub(lambda match: f"bundled-source/{Path(match.group()).name}", value)
        return value

    path.write_text(
        json.dumps(clean(payload), ensure_ascii=False, allow_nan=False, indent=2) + "\n",
        encoding="utf-8",
    )


def sanitize_result_packages(stage: Path) -> None:
    batch = stage / "frontend/demo/batch"
    package_root = batch / "packages"
    records: dict[str, dict[str, int | str]] = {}
    forbidden_markers = (b"/Users/", b"/Volumes/")
    for package in sorted(package_root.glob("*.zip")):
        temporary = package.with_suffix(".zip.tmp")
        with zipfile.ZipFile(package, "r") as source, zipfile.ZipFile(
            temporary, "w", allowZip64=True
        ) as target:
            target.comment = source.comment
            for info in source.infolist():
                pure = PurePosixPath(info.filename)
                if pure.is_absolute() or ".." in pure.parts:
                    raise ValueError(f"unsafe path in result ZIP {package.name}: {info.filename}")
                data = source.read(info)
                if info.filename.lower().endswith(".json"):
                    text = data.decode("utf-8-sig")
                    text = LOCAL_PATH.sub(
                        lambda match: f"bundled-source/{Path(match.group()).name}", text
                    )
                    data = text.encode("utf-8")
                if any(marker in data for marker in forbidden_markers):
                    raise ValueError(
                        f"local absolute path remains in {package.name}:{info.filename}"
                    )
                target.writestr(info, data)
        temporary.replace(package)
        records[package.name] = {"bytes": package.stat().st_size, "sha256": sha256(package)}

    for event_json in (batch / "events").glob("*/event.json"):
        payload = json.loads(event_json.read_text(encoding="utf-8"))
        result_package = payload.get("resultPackage") or {}
        filename = result_package.get("filename", "")
        record = records.get(filename)
        if record is None:
            raise ValueError(f"result package missing for {event_json}")
        result_package.update(record)
        payload["resultPackage"] = result_package
        event_json.write_text(
            json.dumps(payload, ensure_ascii=False, allow_nan=False, indent=2) + "\n",
            encoding="utf-8",
        )


def validate_browser_batch(stage: Path) -> None:
    batch = stage / "frontend/demo/batch"
    catalog = json.loads((batch / "catalog.json").read_text(encoding="utf-8"))
    entries = catalog.get("events", [])
    event_jsons = list((batch / "events").glob("*/event.json"))
    event_dirs = [path for path in (batch / "events").iterdir() if path.is_dir()]
    counts = {
        year: sum(str(item.get("eventId", "")).startswith(year) for item in entries)
        for year in EXPECTED_BATCH_YEARS
    }
    if catalog.get("eventCount") != EXPECTED_BATCH_EVENTS or len(entries) != EXPECTED_BATCH_EVENTS:
        raise ValueError("batch catalog must contain exactly 74 events")
    if len(event_dirs) != EXPECTED_BATCH_EVENTS or len(event_jsons) != EXPECTED_BATCH_EVENTS:
        raise ValueError("batch event directories and event.json files must both total 74")
    if counts != EXPECTED_BATCH_YEARS:
        raise ValueError(f"unexpected batch year counts: {counts}")
    forbidden = [
        path.relative_to(batch).as_posix()
        for path in batch.rglob("*")
        if path.is_file()
        and (
            path.suffix.lower() == ".dat"
            or (path.suffix.lower() == ".zip" and "packages" not in path.relative_to(batch).parts)
        )
    ]
    if forbidden:
        raise ValueError(f"heavy batch artifacts leaked into portable bundle: {forbidden[:3]}")
    package_files = list((batch / "packages").glob("*.zip"))
    if len(package_files) != EXPECTED_BATCH_EVENTS:
        raise ValueError(f"batch result ZIP packages must total 74, found {len(package_files)}")
    packages = {path.name: path for path in package_files}
    for path in event_jsons:
        payload = json.loads(path.read_text(encoding="utf-8"))
        package = payload.get("resultPackage") or {}
        package_path = packages.get(package.get("filename", ""))
        if package_path is None:
            raise ValueError(f"resultPackage is missing in {path}")
        expected_url = f"./demo/batch/packages/{package_path.name}"
        if package.get("url") != expected_url:
            raise ValueError(f"unexpected resultPackage URL in {path}: {package.get('url')}")
        if package.get("bytes") != package_path.stat().st_size or package.get("sha256") != sha256(package_path):
            raise ValueError(f"resultPackage integrity mismatch in {path}")
        result_files = payload.get("resultFiles") or {}
        if "inferredDatDirectory" in result_files or "step2DatDirectories" in result_files:
            raise ValueError(f"DAT paths must be removed from {path}")


def manifest(stage: Path) -> None:
    lines = []
    for path in sorted(item for item in stage.rglob("*") if item.is_file()):
        if path.name == "MANIFEST.sha256":
            continue
        lines.append(f"{sha256(path)}  {path.relative_to(stage).as_posix()}")
    (stage / "MANIFEST.sha256").write_text("\n".join(lines) + "\n", encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    runtime = parser.add_mutually_exclusive_group(required=True)
    runtime.add_argument("--runtime-archive", type=Path)
    runtime.add_argument("--runtime-dir", type=Path)
    parser.add_argument("--wheelhouse", type=Path, required=True)
    parser.add_argument("--map-source", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=PROJECT_ROOT / "dist")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    expected_runtime = "de3e362376859b060fa8b856c434efa81fcf6d4ede3d6e177c7e2169670cac50"
    if args.runtime_archive and sha256(args.runtime_archive) != expected_runtime:
        raise ValueError("Windows Python 运行时 SHA-256 不匹配")
    if args.runtime_dir and not (args.runtime_dir / "python/python.exe").is_file():
        raise FileNotFoundError(args.runtime_dir / "python/python.exe")
    wheel_lock = PROJECT_ROOT / "deploy/windows/requirements-windows.lock.txt"
    if not wheel_lock.is_file():
        raise FileNotFoundError(wheel_lock)
    if not args.wheelhouse.is_dir():
        raise FileNotFoundError(args.wheelhouse)

    build_root = args.output / ".windows-portable-build"
    stage = build_root / PACKAGE_NAME
    shutil.rmtree(stage, ignore_errors=True)
    stage.mkdir(parents=True)

    for relative in (*ROOT_FILES, *FRONTEND_FILES):
        copy_file(relative, stage)
    for relative in ("frontend/assets", "frontend/vendor", "frontend/demo/site_data"):
        shutil.copytree(
            PROJECT_ROOT / relative,
            stage / relative,
            dirs_exist_ok=True,
            ignore=shutil.ignore_patterns(".DS_Store"),
        )
    copy_demo_tree(PROJECT_ROOT / "frontend/demo/events", stage / "frontend/demo/events")
    copy_demo_tree(PROJECT_ROOT / "frontend/demo/project_result", stage / "frontend/demo/project_result")
    copy_demo_tree(
        PROJECT_ROOT / "frontend/demo/batch",
        stage / "frontend/demo/batch",
        include_packages=True,
    )

    deploy_dir = stage / "deploy/windows"
    shutil.copytree(
        PROJECT_ROOT / "deploy/windows",
        deploy_dir,
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store"),
    )
    wheel_target = stage / "wheelhouse"
    shutil.copytree(
        args.wheelhouse,
        wheel_target,
        ignore=shutil.ignore_patterns(".DS_Store"),
    )
    shutil.copy2(wheel_lock, wheel_target / wheel_lock.name)
    if args.runtime_archive:
        with tarfile.open(args.runtime_archive, "r:gz") as archive:
            archive.extractall(stage / "runtime", filter="data")
    else:
        shutil.copytree(
            args.runtime_dir,
            stage / "runtime",
            ignore=shutil.ignore_patterns(".DS_Store"),
        )
    if not (stage / "runtime/python/python.exe").is_file():
        raise RuntimeError("Windows Python runtime layout is invalid")

    map_target = stage / "data/admin_geojson"
    map_target.mkdir(parents=True)
    for name in MAP_FILES:
        source = args.map_source / name
        if not source.is_file():
            raise FileNotFoundError(source)
        shutil.copy2(source, map_target / name)
    (stage / "workspace/imports").mkdir(parents=True)
    (stage / "workspace/runtime-cache").mkdir(parents=True)

    normalize_windows_scripts(stage)

    for path in stage.rglob("*.json"):
        sanitize_json(path)
    sanitize_result_packages(stage)
    validate_browser_batch(stage)
    deployment = {
        "product": "震·时澜",
        "platform": "Windows 11 x64",
        "python": "3.12.13",
        "runtimeBuild": "python-build-standalone 20260623",
        "offline": True,
        "administrativeMapsBundled": list(MAP_FILES),
        "demo": "24 K-NET events plus 74 complete 2025/2026 result packages; duplicate event DAT trees excluded",
        "builtAt": datetime.now(timezone.utc).isoformat(),
    }
    (stage / "DEPLOYMENT.json").write_text(
        json.dumps(deployment, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    manifest(stage)

    args.output.mkdir(parents=True, exist_ok=True)
    package = args.output / f"{PACKAGE_NAME}.zip"
    package.unlink(missing_ok=True)
    with zipfile.ZipFile(
        package,
        "w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=9,
        allowZip64=True,
    ) as archive:
        for path in sorted(item for item in stage.rglob("*") if item.is_file()):
            archive.write(path, f"{PACKAGE_NAME}/{path.relative_to(stage).as_posix()}")
    (package.with_suffix(package.suffix + ".sha256")).write_text(
        f"{sha256(package)}  {package.name}\n", encoding="ascii"
    )
    print(json.dumps({"package": str(package), "bytes": package.stat().st_size, "sha256": sha256(package)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
