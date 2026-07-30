#!/usr/bin/env python3
"""Recursively process CSMNC event folders and publish them to the web viewer."""

from __future__ import annotations

import argparse
from collections.abc import Callable
import csv
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import hashlib
from http.server import ThreadingHTTPServer
from importlib.metadata import PackageNotFoundError, version
import json
import math
from pathlib import Path
import re
import shutil
import sys
import time
import traceback
import webbrowser
import zipfile

import numpy as np
import pandas as pd

from csmnc_reader import COMPONENTS, CsmncFormatError, CsmncHeader, read_acceleration, read_header
from frontend.demo import build_demo as zsl
from tianditu_250k import (
    ADMIN_LEVEL_FILES,
    BasemapError,
    build_admin_basemap,
    infer_admin_boundaries_dir,
)


PROJECT_ROOT = Path(__file__).resolve().parent
FRONTEND_ROOT = PROJECT_ROOT / "frontend"
DEFAULT_OUTPUT_ROOT = FRONTEND_ROOT / "demo" / "batch"
BATCH_CATALOG = DEFAULT_OUTPUT_ROOT / "catalog.json"
WORLD_BOUNDARY = FRONTEND_ROOT / "demo" / "world-land.geojson"
INPUT_FOLDER_NAMES = {"hndata", "eidata", "eldata"}
COMPONENT_INDEX = {"UD": 0, "NS": 1, "EW": 2}
PARSER_SCHEMA = "strong_motion_text_v2"
SPATIAL_MAPS = (
    ("PGA", "PGA-map.png", "峰值加速度空间分布"),
    ("PGV", "PGV-map.png", "峰值速度空间分布"),
    ("PSA03", "PSA03-map.png", "0.3 s 伪谱加速度空间分布"),
    ("PSA10", "PSA10-map.png", "1.0 s 伪谱加速度空间分布"),
    ("PSA30", "PSA30-map.png", "3.0 s 伪谱加速度空间分布"),
    ("intensity", "intensity-map.png", "仪器地震烈度空间分布"),
)
RUNTIME_DISTRIBUTIONS = (
    "numpy",
    "pandas",
    "scipy",
    "matplotlib",
    "obspy",
    "pyrotd",
    "pykooh",
    "pyshp",
)
DISTANCE_BIN_STATISTICS_FILE = "distance_bin_statistics.csv"
DISTANCE_BIN_STATISTICS_COLUMNS = (
    "metric",
    "source",
    "bin_index",
    "bin_left_km",
    "bin_right_km",
    "bin_center_km",
    "n",
    "mean",
    "std",
    "std_in_analysis_space",
    "median",
    "lower_1sigma",
    "upper_1sigma",
    "space",
    "status",
)


@dataclass(frozen=True)
class EventSource:
    data_root: Path
    event_root: Path
    relative_path: str
    files: tuple[Path, ...]


@dataclass
class EventInspection:
    source: EventSource
    event_id: str
    title: str
    status: str
    headers: dict[tuple[str, str], CsmncHeader] = field(default_factory=dict, repr=False)
    station_headers: dict[str, dict[str, CsmncHeader]] = field(default_factory=dict, repr=False)
    metadata: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    invalid_files: list[dict] = field(default_factory=list)
    incomplete_stations: list[dict] = field(default_factory=list)
    duplicate_components: list[dict] = field(default_factory=list)
    snapshot_sha256: str = ""
    parsed_files: int = 0
    complete_stations: int = 0

    def report(self) -> dict:
        return {
            "eventId": self.event_id,
            "title": self.title,
            "sourceRelativePath": self.source.relative_path,
            "status": self.status,
            "sourceFiles": len(self.source.files),
            "parsedFiles": self.parsed_files,
            "completeStations": self.complete_stations,
            "invalidFiles": self.invalid_files,
            "incompleteStations": self.incomplete_stations,
            "duplicateComponents": self.duplicate_components,
            "metadata": self.metadata,
            "warnings": self.warnings,
            "errors": self.errors,
            "snapshotSha256": self.snapshot_sha256,
        }


def json_write(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os_process_id()}")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, allow_nan=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def os_process_id() -> int:
    # Kept in a helper so tests can replace it without patching pathlib.
    import os

    return os.getpid()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def pipeline_sha256() -> str:
    """Fingerprint parsing, numerical processing, rendering, and packaging code."""
    digest = hashlib.sha256()
    for path in [
        Path(__file__),
        PROJECT_ROOT / "csmnc_reader.py",
        Path(zsl.__file__),
    ]:
        digest.update(path.name.encode("utf-8"))
        digest.update(bytes.fromhex(sha256(path)))
    digest.update(
        json.dumps(runtime_versions(), sort_keys=True, separators=(",", ":")).encode("ascii")
    )
    return digest.hexdigest()


def runtime_versions() -> dict[str, str]:
    versions = {"python": sys.version.split()[0]}
    for distribution in RUNTIME_DISTRIBUTIONS:
        try:
            versions[distribution] = version(distribution)
        except PackageNotFoundError:
            versions[distribution] = "not-installed"
    return versions


def basemap_sha256(
    tianditu_bundle: Path | None,
    tianditu_index: Path | None,
    admin_boundaries_dir: Path | None = None,
) -> str | None:
    """Fingerprint the administrative map converter and the files it actually renders."""
    if admin_boundaries_dir is None:
        return None
    digest = hashlib.sha256()
    converter = PROJECT_ROOT / "tianditu_250k.py"
    digest.update(converter.name.encode("utf-8"))
    digest.update(bytes.fromhex(sha256(converter)))
    for _, filename in ADMIN_LEVEL_FILES:
        source = admin_boundaries_dir / filename
        digest.update(filename.encode("utf-8"))
        digest.update(bytes.fromhex(sha256(source)))
    return digest.hexdigest()


def snapshot_sha256(files: tuple[Path, ...], input_root: Path) -> str:
    """Fingerprint DAT paths and bytes without depending on filesystem timestamps."""
    digest = hashlib.sha256()
    for path in sorted(files, key=lambda item: item.as_posix().casefold()):
        try:
            relative = path.relative_to(input_root).as_posix()
        except ValueError:
            relative = path.name
        digest.update(relative.encode("utf-8"))
        digest.update(f"\0{path.stat().st_size}\0".encode("ascii"))
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        digest.update(b"\n")
    return digest.hexdigest()


def event_metadata_sha256(metadata: dict) -> str:
    """Fingerprint resolved event metadata, including the documented override note."""
    canonical = json.dumps(
        metadata,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def discover_events(input_root: Path) -> list[EventSource]:
    """Group recursively discovered DAT files by their nearest HN/EI data root."""
    input_root = input_root.resolve()
    grouped: dict[Path, set[Path]] = {}
    for path in input_root.rglob("*"):
        if not path.is_file() or path.suffix.casefold() != ".dat":
            continue
        data_root = None
        for parent in path.parents:
            if parent == input_root.parent:
                break
            if parent.name.casefold() in INPUT_FOLDER_NAMES:
                data_root = parent.parent
                break
            if parent == input_root:
                break
        if data_root is None:
            data_root = path.parent
        grouped.setdefault(data_root.resolve(), set()).add(path.resolve())

    sources = []
    for data_root, files in grouped.items():
        event_root = data_root.parent if data_root.name.casefold() in {"accdata", "data"} else data_root
        try:
            relative = event_root.relative_to(input_root).as_posix()
        except ValueError:
            relative = event_root.name
        sources.append(
            EventSource(
                data_root=data_root,
                event_root=event_root,
                relative_path=relative or event_root.name,
                files=tuple(sorted(files, key=lambda item: item.as_posix().casefold())),
            )
        )
    return sorted(sources, key=lambda source: source.relative_path.casefold())


def event_identifier(source: EventSource, origin_time: datetime | None = None) -> str:
    date = origin_time.strftime("%Y%m%d%H%M%S") if origin_time else "unknown-time"
    suffix = hashlib.sha256(source.relative_path.encode("utf-8")).hexdigest()[:10]
    return f"{date}-{suffix}"


def display_title(source: EventSource) -> str:
    return re.sub(r"^\d+_", "", source.event_root.name).strip() or source.event_root.name


def load_overrides(path: Path | None) -> dict[str, dict]:
    if path is None:
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"无法读取事件元数据覆盖文件 {path}: {error}") from error
    if not isinstance(payload, dict) or any(not isinstance(value, dict) for value in payload.values()):
        raise ValueError("元数据覆盖文件必须是 {相对事件路径: {字段: 值}} 对象")
    return {str(key).replace("\\", "/"): value for key, value in payload.items()}


def _resolve_metadata(
    name: str,
    values: list,
    override: dict,
    errors: list[str],
    warnings: list[str],
):
    normalized = sorted(set(values))
    if len(normalized) == 1:
        value = normalized[0]
        if name in override and override[name] != value:
            warnings.append(f"元数据 {name} 使用显式覆盖值 {override[name]!r}，头段值为 {value!r}")
            return override[name]
        return value
    if name not in override:
        errors.append(f"事件元数据 {name} 冲突: {normalized!r}；请在覆盖文件中显式指定")
        return None
    warnings.append(f"元数据 {name} 的冲突值 {normalized!r} 已由显式覆盖解决")
    return override[name]


def inspect_event(source: EventSource, input_root: Path, override: dict | None = None) -> EventInspection:
    override = override or {}
    inspection = EventInspection(
        source=source,
        event_id=event_identifier(source),
        title=display_title(source),
        status="inspecting",
    )
    try:
        inspection.snapshot_sha256 = snapshot_sha256(source.files, input_root)
    except OSError as error:
        inspection.errors.append(f"无法取得输入快照: {error}")
        inspection.status = "invalid_input"
        return inspection

    candidates: dict[tuple[str, str], list[CsmncHeader]] = {}
    for path in source.files:
        try:
            header = read_header(path)
        except CsmncFormatError as error:
            try:
                relative = path.relative_to(source.data_root).as_posix()
            except ValueError:
                relative = path.name
            inspection.invalid_files.append({"file": relative, "reason": str(error)})
            continue
        candidates.setdefault((header.station, header.component), []).append(header)
        inspection.parsed_files += 1

    duplicate_stations = set()
    for (station, component), headers in candidates.items():
        if len(headers) == 1:
            inspection.headers[(station, component)] = headers[0]
            continue
        duplicate_stations.add(station)
        inspection.duplicate_components.append(
            {
                "station": station,
                "component": component,
                "files": [header.path.name for header in headers],
            }
        )

    station_names = sorted({station for station, _ in inspection.headers})
    valid_event_headers: list[CsmncHeader] = []
    for station in station_names:
        present = {component for candidate, component in inspection.headers if candidate == station}
        if station in duplicate_stations:
            inspection.incomplete_stations.append(
                {"station": station, "present": sorted(present), "reason": "duplicate_component"}
            )
            continue
        missing = sorted(COMPONENTS - present)
        if missing:
            inspection.incomplete_stations.append(
                {"station": station, "present": sorted(present), "missing": missing, "reason": "missing_component"}
            )
            continue
        component_headers = {component: inspection.headers[(station, component)] for component in COMPONENTS}
        reference = component_headers["UD"]
        conflicts = []
        for component, header in component_headers.items():
            for field_name in [
                "station_latitude_deg",
                "station_longitude_deg",
                "sampling_interval_s",
                "sample_count",
            ]:
                if getattr(header, field_name) != getattr(reference, field_name):
                    conflicts.append(f"{component}.{field_name}")
        if conflicts:
            inspection.incomplete_stations.append(
                {"station": station, "present": sorted(present), "reason": "component_metadata_conflict", "fields": conflicts}
            )
            continue
        inspection.station_headers[station] = component_headers
        valid_event_headers.extend(component_headers.values())

    inspection.complete_stations = len(inspection.station_headers)
    if inspection.invalid_files:
        inspection.warnings.append(f"{len(inspection.invalid_files)} 个无法解析的分量文件已隔离")
    if inspection.incomplete_stations:
        inspection.warnings.append(f"{len(inspection.incomplete_stations)} 个非完整三分量台站未进入计算")
    if inspection.complete_stations < 4:
        inspection.errors.append(f"完整三分量台站仅 {inspection.complete_stations} 个，少于算法要求的 4 个")

    if valid_event_headers:
        origins = [header.origin_time.isoformat(sep=" ") for header in valid_event_headers]
        latitude = [header.source_latitude_deg for header in valid_event_headers]
        longitude = [header.source_longitude_deg for header in valid_event_headers]
        depth = [header.source_depth_km for header in valid_event_headers]
        magnitude = [header.magnitude for header in valid_event_headers]
        magnitude_type = [header.magnitude_type for header in valid_event_headers]
        resolved = {
            "origin_time": _resolve_metadata("origin_time", origins, override, inspection.errors, inspection.warnings),
            "latitude": _resolve_metadata("latitude", latitude, override, inspection.errors, inspection.warnings),
            "longitude": _resolve_metadata("longitude", longitude, override, inspection.errors, inspection.warnings),
            "depth": _resolve_metadata("depth", depth, override, inspection.errors, inspection.warnings),
            "magnitude": _resolve_metadata("magnitude", magnitude, override, inspection.errors, inspection.warnings),
            "magnitude_type": _resolve_metadata(
                "magnitude_type", magnitude_type, override, inspection.errors, inspection.warnings
            ),
        }
        if any(key in override for key in resolved):
            note = str(override.get("note", "")).strip()
            if not note:
                inspection.errors.append("使用事件元数据覆盖时必须填写非空 note，说明依据")
            else:
                resolved["override_note"] = note
        inspection.metadata = resolved

        if resolved["origin_time"]:
            try:
                origin_time = datetime.fromisoformat(str(resolved["origin_time"]))
                inspection.event_id = event_identifier(source, origin_time)
                folder_date = re.search(r"(?:^|_)(20\d{6})", source.event_root.name)
                if folder_date and folder_date.group(1) != origin_time.strftime("%Y%m%d"):
                    inspection.warnings.append(
                        f"目录日期 {folder_date.group(1)} 与 CSMNC 头段日期 {origin_time:%Y%m%d} 不一致"
                    )
            except ValueError:
                inspection.errors.append(f"覆盖后的 origin_time 不是 ISO 日期时间: {resolved['origin_time']!r}")

    coordinates = np.asarray(
        [
            [headers["UD"].station_longitude_deg, headers["UD"].station_latitude_deg]
            for headers in inspection.station_headers.values()
        ],
        dtype=float,
    )
    if len(coordinates) >= 4:
        unique = np.unique(coordinates, axis=0)
        if len(unique) < 4 or np.linalg.matrix_rank(unique - unique.mean(axis=0)) < 2:
            inspection.errors.append("台站坐标不足 4 个唯一非共线点，无法建立 Delaunay 几何")

    inspection.status = "ready" if not inspection.errors else (
        "not_applicable" if inspection.complete_stations < 4 else "invalid_input"
    )
    return inspection


def load_event_arrays(inspection: EventInspection) -> tuple[pd.DataFrame, dict[str, np.ndarray], dict]:
    arrays: dict[str, np.ndarray] = {}
    rows = []
    for station, component_headers in sorted(inspection.station_headers.items()):
        components = {}
        for component in COMPONENTS:
            header, values = read_acceleration(component_headers[component])
            components[component] = values
        arrays[station] = np.vstack([components["UD"], components["NS"], components["EW"]])
        header = component_headers["UD"]
        rows.append(
            {
                "station_code": station,
                "station_longitude_deg": header.station_longitude_deg,
                "station_latitude_deg": header.station_latitude_deg,
                "sampling_rate_hz": header.sampling_rate_hz,
                "duration_s": header.duration_s,
            }
        )
    event = pd.DataFrame(rows)
    metadata = {
        "magnitude": float(inspection.metadata["magnitude"]),
        "magnitude_type": str(inspection.metadata["magnitude_type"]),
        "depth": float(inspection.metadata["depth"]),
        "latitude": float(inspection.metadata["latitude"]),
        "longitude": float(inspection.metadata["longitude"]),
        "duration": float(event.duration_s.max()),
        "origin_time": str(inspection.metadata["origin_time"]),
    }
    return event, arrays, metadata


def waveform_sha256(arrays: dict[str, np.ndarray]) -> str:
    digest = hashlib.sha256()
    for station in sorted(arrays):
        digest.update(station.encode("utf-8"))
        digest.update(np.ascontiguousarray(arrays[station]).tobytes())
    return digest.hexdigest()


def event_seeds(stable_source_id: str) -> dict[str, int]:
    """Return component seed namespaces that do not depend on target layout."""
    base = int.from_bytes(
        hashlib.sha256(f"zsl-csmnc-{stable_source_id}".encode("ascii")).digest()[:4],
        "big",
    )
    return {component: (base + index) % (2**32) for index, component in enumerate(["EW", "NS", "UD"])}


def legacy_config(
    metadata: dict,
    station_table: pd.DataFrame,
    grid_shape: tuple[int, int] | None,
    target_count: int,
) -> dict:
    config = {
        "mag": metadata["magnitude"],
        "num_sta": len(station_table),
        "e_lat": metadata["latitude"],
        "e_lon": metadata["longitude"],
        "depth_hy": metadata["depth"],
        "duration": metadata["duration"],
        "lon_min": float(station_table.lon.min()),
        "lon_max": float(station_table.lon.max()),
        "lat_min": float(station_table.lat.min()),
        "lat_max": float(station_table.lat.max()),
        "target_count": target_count,
    }
    if grid_shape is not None:
        config.update(
            number_horizontal=grid_shape[1],
            number_vertical=grid_shape[0],
        )
    return config


def inferred_dat_manifest(rows) -> dict[str, set[str]]:
    records = rows.to_dict(orient="records") if isinstance(rows, pd.DataFrame) else list(rows)
    components = ("EW", "NS", "UD")
    return {
        "IF_folder": {
            f"{zsl.target_coordinate_key(float(row['lon']), float(row['lat']))}.{component}.dat"
            for row in records
            for component in components
        }
    }


def package_event(
    event_dir: Path,
    package_path: Path,
    event_id: str,
    expected_dat_files: dict[str, set[str]] | None = None,
) -> tuple[int, str]:
    package_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = package_path.with_name(f".{package_path.name}.tmp-{os_process_id()}")
    try:
        with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for path in sorted(event_dir.rglob("*")):
                if path.is_file() and path.name not in {
                    "event.json",
                    "tianditu-250k.geojson",
                    "admin-boundaries.geojson",
                }:
                    archive.write(path, Path(event_id) / path.relative_to(event_dir))
        with zipfile.ZipFile(temporary) as archive:
            if archive.testzip() is not None:
                raise ValueError("结果 ZIP 完整性检查失败")
            statistics_path = f"{event_id}/{DISTANCE_BIN_STATISTICS_FILE}"
            if statistics_path not in archive.namelist():
                raise ValueError("结果 ZIP 缺少距离分箱统计 CSV")
            if expected_dat_files is not None:
                expected_paths = {
                    f"{event_id}/{directory}/{filename}"
                    for directory, filenames in expected_dat_files.items()
                    for filename in filenames
                }
                archived_paths = {
                    name
                    for name in archive.namelist()
                    if name.startswith(f"{event_id}/") and name.lower().endswith(".dat")
                }
                if archived_paths != expected_paths:
                    missing = sorted(expected_paths - archived_paths)
                    extra = sorted(archived_paths - expected_paths)
                    raise ValueError(
                        "结果 ZIP 的推测三分量 DAT 不完整"
                        f"（缺少 {len(missing)}，多出 {len(extra)}）"
                    )
        temporary.replace(package_path)
        return package_path.stat().st_size, sha256(package_path)
    finally:
        temporary.unlink(missing_ok=True)


def validate_inferred_dat(path: Path) -> int:
    """Validate one generated two-column acceleration time series."""
    try:
        with path.open("r", encoding="ascii", newline="") as stream:
            header = stream.readline().strip()
        if header != "Time Acc":
            raise ValueError(f"{path} 的 DAT 表头应为 'Time Acc'")
        values = np.loadtxt(path, skiprows=1, dtype=float)
    except (OSError, UnicodeError, ValueError) as error:
        if isinstance(error, ValueError) and str(error).startswith(str(path)):
            raise
        raise ValueError(f"无法读取推测 DAT {path}: {error}") from error
    if values.ndim != 2 or values.shape[1] != 2 or values.shape[0] < 2:
        raise ValueError(f"{path} 的 DAT 必须至少包含两行 Time/Acc 数值")
    if not np.isfinite(values).all():
        raise ValueError(f"{path} 的 DAT 含非有限 Time/Acc 数值")
    time_axis = values[:, 0]
    if not np.isclose(time_axis[0], 0.0, rtol=0.0, atol=1e-12):
        raise ValueError(f"{path} 的 DAT 时间轴必须从 0.00 s 开始")
    if not np.allclose(np.diff(time_axis), 0.01, rtol=0.0, atol=1e-10):
        raise ValueError(f"{path} 的 DAT 时间步长必须恒为 0.01 s")
    return int(values.shape[0])


def validate_distance_bin_statistics(path: Path) -> int:
    """Validate the reviewable six-metric attenuation summary contract."""
    try:
        frame = pd.read_csv(path)
    except (OSError, pd.errors.ParserError, pd.errors.EmptyDataError) as error:
        raise ValueError(f"无法读取距离分箱统计 {path}: {error}") from error
    missing = set(DISTANCE_BIN_STATISTICS_COLUMNS) - set(frame.columns)
    if missing:
        raise ValueError(f"距离分箱统计缺少字段: {', '.join(sorted(missing))}")
    if frame.empty:
        return 0
    if not set(frame["metric"]).issubset(zsl.GROUND_MOTION_FIELDS):
        raise ValueError("距分箱统计含未知地震动指标")
    if not set(frame["source"]).issubset({"observed", "inferred"}):
        raise ValueError("距离分箱统计 source 必须为 observed 或 inferred")
    expected_spaces = frame["metric"].map(
        lambda metric: "linear" if metric == "intensity" else "log10"
    )
    if not frame["space"].eq(expected_spaces).all():
        raise ValueError("距离分箱统计的 space 与指标不一致")
    if not set(frame["status"]).issubset({"ok", "insufficient"}):
        raise ValueError("距离分箱统计 status 必须为 ok 或 insufficient")
    geometry = frame[
        ["bin_index", "bin_left_km", "bin_right_km", "bin_center_km", "n"]
    ].apply(pd.to_numeric, errors="coerce")
    if not np.isfinite(geometry.to_numpy(float)).all():
        raise ValueError("距离分箱统计的分箱或样本数含非有限值")
    if (
        (geometry["n"] < 1).any()
        or not np.equal(geometry["n"], np.floor(geometry["n"])).all()
        or (geometry["bin_index"] < 0).any()
        or not np.equal(
            geometry["bin_index"], np.floor(geometry["bin_index"])
        ).all()
    ):
        raise ValueError("距离分箱统计必须保留所有非空箱")
    if (
        (geometry["bin_left_km"] < 0).any()
        or (geometry["bin_right_km"] <= geometry["bin_left_km"]).any()
        or (geometry["bin_center_km"] < geometry["bin_left_km"]).any()
        or (geometry["bin_center_km"] > geometry["bin_right_km"]).any()
    ):
        raise ValueError("距离分箱统计的分箱边界无效")
    if frame.duplicated(["metric", "source", "bin_index"]).any():
        raise ValueError("距离分箱统计含重复的指标、来源与分箱")
    statistics_columns = [
        "mean",
        "std",
        "std_in_analysis_space",
        "median",
        "lower_1sigma",
        "upper_1sigma",
    ]
    statistics = frame[statistics_columns].apply(pd.to_numeric, errors="coerce")
    ok = frame["status"].eq("ok")
    insufficient = frame["status"].eq("insufficient")
    if (geometry.loc[ok, "n"] < 3).any() or not np.isfinite(
        statistics.loc[ok].to_numpy(float)
    ).all():
        raise ValueError("ok 距离分箱必须 n≥3 且统计值完整")
    if (geometry.loc[insufficient, "n"] >= 3).any() or not statistics.loc[
        insufficient
    ].isna().all().all():
        raise ValueError("insufficient 距离分箱必须 n<3 且统计值留空")
    return len(frame)


def validate_result(
    detail: dict,
    output_dir: Path,
    save_dat: bool,
    expected_target_points: int,
) -> None:
    observed = detail["observed"]
    inferred = detail["inferred"]
    if len(observed) < 4 or not inferred:
        raise ValueError("输出缺少足够实测台站或没有质控通过的推测点")
    for row in [*observed, *inferred]:
        values = [row[field] for field in ["lon", "lat", *zsl.GROUND_MOTION_FIELDS]]
        if not np.isfinite(values).all():
            raise ValueError(f"输出含非有限地震动值: {row}")
        if any(float(row[field]) <= 0 for field in ["PGA", "PGV", *zsl.PSA_FIELDS]):
            raise ValueError(f"输出含非正峰值或反应谱值: {row}")
    quality = pd.read_csv(output_dir / "target_point_quality_control.csv")
    if len(quality) != expected_target_points:
        raise ValueError(f"目标点 QC 应为 {expected_target_points} 行，实际 {len(quality)}")
    if int(quality.status.eq("processed").sum()) != len(inferred):
        raise ValueError("QC 通过数与推测结果行数不一致")
    for filename in [
        "config.txt",
        "points.csv",
        "observed.csv",
        "inferred.csv",
        DISTANCE_BIN_STATISTICS_FILE,
        "attenuation.png",
        "quality-control.png",
        "run_metadata.json",
        "pipeline_run.json",
    ]:
        if not (output_dir / filename).is_file():
            raise ValueError(f"缺少结果文件 {filename}")
    validate_distance_bin_statistics(output_dir / DISTANCE_BIN_STATISTICS_FILE)
    for _, filename, _ in SPATIAL_MAPS:
        if not (output_dir / filename).is_file():
            raise ValueError(f"缺少结果文件 {filename}")
    if save_dat:
        expected_dat_files = inferred_dat_manifest(inferred)
        for directory, expected_names in expected_dat_files.items():
            actual_names = {path.name for path in (output_dir / directory).glob("*.dat")}
            if actual_names != expected_names:
                raise ValueError(
                    f"{directory} 的 DAT 文件名或数量与推测场点三分量不一致"
                )
        if len(list(output_dir.rglob("*.dat"))) != sum(
            map(len, expected_dat_files.values())
        ):
            raise ValueError("结果目录含预期之外的推测 DAT")
        records = inferred.to_dict(orient="records") if isinstance(inferred, pd.DataFrame) else inferred
        for row in records:
            coordinate_key = zsl.target_coordinate_key(float(row["lon"]), float(row["lat"]))
            row_count = None
            for component in ("EW", "NS", "UD"):
                if_path = output_dir / "IF_folder" / f"{coordinate_key}.{component}.dat"
                component_rows = validate_inferred_dat(if_path)
                if row_count is None:
                    row_count = component_rows
                elif component_rows != row_count:
                    raise ValueError(f"{coordinate_key} 的 EW/NS/UD DAT 行数不一致")


def event_basemap_bbox(
    observed: pd.DataFrame,
    inferred: pd.DataFrame,
    metadata: dict,
    quality: pd.DataFrame | None = None,
) -> tuple[float, float, float, float]:
    lon_parts = [
        observed.lon.to_numpy(float),
        inferred.lon.to_numpy(float),
        np.asarray([float(metadata["longitude"])], dtype=float),
    ]
    lat_parts = [
        observed.lat.to_numpy(float),
        inferred.lat.to_numpy(float),
        np.asarray([float(metadata["latitude"])], dtype=float),
    ]
    if quality is not None and {
        "target_longitude",
        "target_latitude",
    }.issubset(quality.columns):
        lon_parts.append(quality.target_longitude.to_numpy(float))
        lat_parts.append(quality.target_latitude.to_numpy(float))
    lons = np.concatenate(lon_parts)
    lats = np.concatenate(lat_parts)
    lon_span = float(lons.max() - lons.min())
    lat_span = float(lats.max() - lats.min())
    lon_pad = max(lon_span * 0.16, 0.18)
    lat_pad = max(lat_span * 0.16, 0.18)
    return (
        float(lons.min() - lon_pad),
        float(lats.min() - lat_pad),
        float(lons.max() + lon_pad),
        float(lats.max() + lat_pad),
    )


def build_event(
    inspection: EventInspection,
    output_root: Path,
    boundary: dict,
    save_dat: bool,
    make_package: bool,
    method_hash: str,
    tianditu_bundle: Path | None = None,
    tianditu_index: Path | None = None,
    admin_boundaries_dir: Path | None = None,
    basemap_hash: str | None = None,
    input_content_sha256: str | None = None,
    archive_id: str | None = None,
    grid_shape: tuple[int, int] | None = None,
    custom_target_points: list[dict] | None = None,
    target_config: dict | None = None,
    progress_callback: Callable[[dict], None] | None = None,
) -> dict:
    stage_count = 9

    def report_progress(
        phase: str,
        label: str,
        stage_index: int,
        completed: int,
        total: int,
        unit: str,
        message: str,
        **values,
    ) -> None:
        if progress_callback is None:
            return
        fraction = min(max(completed / total, 0.0), 1.0) if total else 1.0
        progress_callback(
            {
                "phase": phase,
                "label": label,
                "stageIndex": stage_index,
                "stageCount": stage_count,
                "completed": completed,
                "total": total,
                "unit": unit,
                "percent": round(100 * (stage_index - 1 + fraction) / stage_count, 1),
                "message": message,
                **values,
            }
        )

    if grid_shape is not None and custom_target_points is not None:
        raise ValueError("网格和自定义推测点不能同时指定")
    if custom_target_points is not None:
        candidate_points = len(custom_target_points)
        target_core = {
            "mode": "custom",
            "source": (target_config or {}).get("source", "manual"),
            "points": custom_target_points,
            "count": candidate_points,
        }
        effective_grid_shape = None
    else:
        effective_grid_shape = tuple(grid_shape or zsl.GRID_SHAPE)
        candidate_points = math.prod(effective_grid_shape)
        target_core = {
            "mode": "grid",
            "rows": effective_grid_shape[0],
            "columns": effective_grid_shape[1],
            "count": candidate_points,
        }
    canonical = json.dumps(
        target_core,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    target_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    supplied_target_hash = (target_config or {}).get("hash")
    if supplied_target_hash and supplied_target_hash != target_hash:
        raise ValueError("推测点配置哈希与规范化内容不一致")
    target_summary = {
        key: value
        for key, value in {**target_core, "hash": target_hash}.items()
        if key != "points"
    }

    started = time.perf_counter()
    started_at = datetime.now(timezone.utc).isoformat()
    final_dir = output_root / "events" / inspection.event_id
    temporary_dir = final_dir.with_name(f".{final_dir.name}.partial-{os_process_id()}")
    if temporary_dir.exists():
        shutil.rmtree(temporary_dir)
    temporary_dir.mkdir(parents=True)
    try:
        event, arrays, metadata = load_event_arrays(inspection)
        report_progress(
            "input_read",
            "读取输入记录",
            1,
            len(event),
            inspection.complete_stations,
            "台站",
            f"已读取 {len(event)}/{inspection.complete_stations} 个完整三分量台站",
        )
        frequencies = zsl.target_frequency_grid(metadata["duration"])
        report_progress("step2_models", "建立 Step2 模型", 2, 0, 3, "分量", "正在建立三分量 Step2 模型")
        fas, models, station_table = zsl.prepare_step2_models(event, arrays, metadata, frequencies)
        report_progress(
            "step2_models",
            "建立 Step2 模型",
            2,
            len(models),
            3,
            "分量",
            f"已建立 {len(models)}/3 个分量的 Step2 模型",
        )
        observed = pd.DataFrame(
            zsl.build_observed_rows(event, arrays, metadata, metric_round_digits=6)
        )
        seed_source_id = input_content_sha256 or inspection.snapshot_sha256
        seeds = event_seeds(seed_source_id)
        accepted_points = 0
        skipped_points = 0

        def report_grid_point(completed: int, total: int, quality_row: dict) -> None:
            nonlocal accepted_points, skipped_points
            if quality_row.get("status") == "processed":
                accepted_points += 1
            else:
                skipped_points += 1
            report_progress(
                "candidate_inference",
                "推测候选点",
                3,
                completed,
                total,
                "候选点",
                f"已计算 {completed}/{total} 个候选点；通过 {accepted_points}，跳过 {skipped_points}",
                accepted=accepted_points,
                skipped=skipped_points,
            )

        report_progress(
            "candidate_inference",
            "推测候选点",
            3,
            0,
            candidate_points,
            "候选点",
            f"开始计算 {candidate_points} 个候选点",
            accepted=0,
            skipped=0,
        )
        inferred_rows, simulated, nonpositive_counts, quality_rows = zsl.run_step2_grid(
            station_table,
            fas,
            models,
            metadata,
            result_dir=temporary_dir if save_dat else None,
            component_seeds=seeds,
            metric_round_digits=6,
            grid_shape=effective_grid_shape,
            custom_target_points=custom_target_points,
            progress_callback=report_grid_point,
            max_retained_waveforms=zsl.MAX_RETAINED_WAVEFORMS,
        )
        inferred = pd.DataFrame(inferred_rows)
        quality = pd.DataFrame(quality_rows)
        if inferred.empty:
            raise ValueError(f"{candidate_points} 个候选点均未通过目标点空间质量控制")

        distance_bin_statistics = zsl.attenuation_distance_bin_statistics(observed, inferred)
        metric_file_total = 8
        report_progress("metrics_csv", "整理指标与 CSV", 4, 0, metric_file_total, "文件", "正在整理指标并写出结果表")
        config = legacy_config(metadata, station_table, effective_grid_shape, candidate_points)
        (temporary_dir / "config.txt").write_text(
            "\n".join(f"{key}={value}" for key, value in config.items()) + "\n", encoding="utf-8"
        )
        report_progress(
            "metrics_csv",
            "整理指标与 CSV",
            4,
            1,
            metric_file_total,
            "文件",
            f"已写出 1/{metric_file_total} 个指标与数据文件",
        )
        points = station_table.rename(columns={"R": "hyp_dis"})[
            ["station", "lon", "lat", "hyp_dis", "duration"]
        ]
        metric_files = [
            (points, "points.csv"),
            (observed, "observed.csv"),
            (inferred.drop(columns="key"), "inferred.csv"),
            (observed, "实测地震动_事件.csv"),
            (inferred.drop(columns="key"), "推测地震动_事件.csv"),
            (quality, "target_point_quality_control.csv"),
            (distance_bin_statistics, DISTANCE_BIN_STATISTICS_FILE),
        ]
        for completed, (frame, filename) in enumerate(metric_files, start=2):
            frame.to_csv(temporary_dir / filename, index=False)
            report_progress(
                "metrics_csv",
                "整理指标与 CSV",
                4,
                completed,
                metric_file_total,
                "文件",
                f"已写出 {completed}/{metric_file_total} 个指标与数据文件",
            )

        basemap = None
        basemap_metadata = None
        basemap_filename = None
        report_progress("administrative_basemap", "提取行政区划", 5, 0, 1, "底图", "正在准备事件范围行政区划底图")
        if admin_boundaries_dir is not None:
            basemap_filename = "admin-boundaries.geojson"
            basemap_path = temporary_dir / basemap_filename
            try:
                basemap_metadata = build_admin_basemap(
                    admin_boundaries_dir,
                    event_basemap_bbox(observed, inferred, metadata, quality),
                    basemap_path,
                )
                basemap = json.loads(basemap_path.read_text(encoding="utf-8"))
                basemap_metadata["outputSha256"] = sha256(basemap_path)
                if basemap_metadata.get("coverageStatus") == "partial":
                    inspection.warnings.append(
                        "天地图行政区划只覆盖当前地图范围的 "
                        f"{100 * float(basemap_metadata.get('coverageFraction', 0)):.1f}%"
                    )
            except BasemapError as error:
                basemap_filename = None
                inspection.warnings.append(f"天地图行政区划底图回退为全球边界: {error}")
        elif tianditu_bundle is not None:
            inspection.warnings.append(
                "1:25万图幅包不含行政境界线，且未找到 admin_geojson；地图回退为全球陆地边界"
            )
        report_progress(
            "administrative_basemap",
            "提取行政区划",
            5,
            1,
            1,
            "底图",
            "行政区划底图已生成" if basemap is not None else "未生成行政区划底图，已记录边界回退",
        )

        seed_label = " · ".join(f"{component}={seed}" for component, seed in seeds.items())
        map_generated_at = datetime.now(timezone.utc).isoformat()
        report_progress("attenuation_plot", "绘制衰减图", 6, 0, 1, "图件", "正在绘制实测与推测地震动随距离分布")
        attenuation_output = zsl.plot_attenuation(
            observed,
            inferred,
            temporary_dir / "attenuation.png",
            plot_title=inspection.title,
            seed_label=seed_label,
            observed_label="实测台站",
            metadata=metadata,
            generated_at=map_generated_at,
        )
        report_progress("attenuation_plot", "绘制衰减图", 6, 1, 1, "图件", "衰减图已生成")
        spatial_map_total = len(SPATIAL_MAPS)
        report_progress(
            "spatial_plots",
            "绘制六指标空间图",
            7,
            0,
            spatial_map_total,
            "图件",
            f"开始绘制 0/{spatial_map_total} 个空间分布图",
        )
        for completed, (metric, filename, label) in enumerate(SPATIAL_MAPS, start=1):
            zsl.plot_metric_map(
                observed,
                inferred,
                metadata,
                boundary,
                temporary_dir / filename,
                metric=metric,
                plot_title=inspection.title,
                observed_label="实测台站",
                basemap=basemap,
                generated_at=map_generated_at,
                fill_map_extent=metric in zsl.FULL_EXTENT_MAP_METRICS,
            )
            report_progress(
                "spatial_plots",
                "绘制六指标空间图",
                7,
                completed,
                spatial_map_total,
                "图件",
                f"已生成 {completed}/{spatial_map_total}：{label}",
                metric=metric,
                filename=filename,
            )
        report_progress("quality_plot", "绘制质控图", 8, 0, 1, "图件", "正在绘制候选点质量控制图")
        zsl.plot_quality_control_map(
            observed,
            quality,
            metadata,
            boundary,
            temporary_dir / "quality-control.png",
            plot_title=inspection.title,
            observed_label="实测台站",
            basemap=basemap,
        )
        report_progress("quality_plot", "绘制质控图", 8, 1, 1, "图件", "质控图已生成")
        publication_total = 3 if make_package else 2
        report_progress(
            "package_publish",
            "打包并发布",
            9,
            0,
            publication_total,
            "项",
            "正在整理元数据并校验结果",
        )
        waveforms = zsl.selected_waveforms(
            observed,
            inferred,
            arrays,
            simulated,
            metadata,
            max_inferred=3,
            max_samples=6000,
        )
        observed_rate = float(
            event.loc[event.station_code.eq(waveforms[0]["station"]), "sampling_rate_hz"].iloc[0]
        )
        waveforms[0]["sourceSamplingRate"] = observed_rate
        waveforms[0]["samplingRate"] = observed_rate / waveforms[0]["displayStride"]

        skipped = int(quality.status.eq("skipped").sum())
        summary = {
            "observedStations": len(observed),
            "candidateTargetPoints": len(quality),
            "candidateGridPoints": len(quality),
            "inferredPoints": len(inferred),
            "skippedPoints": skipped,
            "targetConfiguration": target_summary,
            "outsideStationConvexHullPoints": int(quality.reason.eq("outside_station_convex_hull").sum()),
            "insufficientNaturalNeighbourPoints": int(
                quality.reason.eq("fewer_than_four_natural_neighbours").sum()
            ),
            "maximumMeanSampleDistanceKm": zsl.MAXIMUM_MEAN_SAMPLE_DISTANCE_KM,
            "maxObserved": {
                metric: zsl.summary_entry(observed, metric, "R") for metric in zsl.GROUND_MOTION_FIELDS
            },
            "maxInferred": {
                metric: zsl.summary_entry(inferred, metric, "dis") for metric in zsl.GROUND_MOTION_FIELDS
            },
            "randomPhaseSeeds": seeds,
            "randomPhaseSeedStrategy": zsl.RANDOM_PHASE_SEED_STRATEGY,
            "retainedInferredWaveforms": len(simulated),
            "distanceBinStatisticsRows": len(distance_bin_statistics),
        }
        artifact_base = f"./demo/batch/events/{inspection.event_id}"
        artifacts = [
            {
                "label": "实测与推测地震动随距离分布",
                "filename": "attenuation.png",
                "url": f"{artifact_base}/attenuation.png",
                "kind": "attenuation",
            },
            *[
                {
                    "label": label,
                    "filename": filename,
                    "url": f"{artifact_base}/{filename}",
                    "kind": "spatial-map",
                    "metric": metric,
                }
                for metric, filename, label in SPATIAL_MAPS
            ],
            {
                "label": "推测目标点质量控制",
                "filename": "quality-control.png",
                "url": f"{artifact_base}/quality-control.png",
                "kind": "quality-control",
            },
        ]
        dat_directories = ["IF_folder"]
        dat_counts = {
            directory: len(list((temporary_dir / directory).glob("*.dat")))
            for directory in dat_directories
        } if save_dat else {directory: 0 for directory in dat_directories}
        if_folder_dat_files = dat_counts["IF_folder"]
        dat_file_count = if_folder_dat_files
        run_metadata = {
            "schemaVersion": 1,
            "eventId": inspection.event_id,
            "title": inspection.title,
            "generatedAt": map_generated_at,
            "source": {
                "format": sorted({header.format_marker for header in inspection.headers.values()}),
                "parserSchema": PARSER_SCHEMA,
                "sourceRelativePath": inspection.source.relative_path,
                "sourceFiles": len(inspection.source.files),
                "parsedFiles": inspection.parsed_files,
                "invalidFiles": inspection.invalid_files,
                "incompleteStations": inspection.incomplete_stations,
                "completeStations": inspection.complete_stations,
                "snapshotSha256": inspection.snapshot_sha256,
                "snapshotBasis": "relative DAT path, byte length, and complete file content",
                "inputContentSha256": input_content_sha256,
                "eventMetadataSha256": event_metadata_sha256(inspection.metadata),
                "archiveId": archive_id,
                "waveformSha256": waveform_sha256(arrays),
                "componentOrder": "ZNE",
                "unit": "gal",
                "metadataOverride": inspection.metadata.get("override_note"),
            },
            "event": metadata,
            "targetConfiguration": target_summary,
            "method": {
                "pipelineSha256": method_hash,
                "runtimeVersions": runtime_versions(),
                "algorithmAdapter": "frontend/demo/build_demo.py Step2/Step3 adapter",
                "grid": list(effective_grid_shape) if effective_grid_shape is not None else None,
                "frequencyBandHz": [0.1, 25],
                "sampleStations": "four closest Delaunay natural neighbours; inverse-distance-squared FAS interpolation",
                "targetPointQualityControl": {
                    "insideOrOnStationConvexHull": True,
                    "requiredNaturalNeighbours": 4,
                    "maximumMeanSampleDistanceKm": zsl.MAXIMUM_MEAN_SAMPLE_DISTANCE_KM,
                },
                "spectralAcceleration": {
                    "periodsSeconds": zsl.PSA_PERIODS_S.tolist(),
                    "dampingRatio": 0.05,
                    "horizontalCombination": "greater of NS and EW",
                    "unit": "cm/s²",
                },
                "randomPhaseSeeds": seeds,
                "randomPhaseSeedBasis": (
                    "inputContentSha256" if input_content_sha256 else "inspectionSnapshotSha256"
                ),
                "randomPhaseSeedStrategy": zsl.RANDOM_PHASE_SEED_STRATEGY,
                "attenuationDistanceBins": {
                    "schemaVersion": attenuation_output["schema_version"],
                    "binning": attenuation_output["binning"],
                    "audit": attenuation_output.get("audit", []),
                    "csv": DISTANCE_BIN_STATISTICS_FILE,
                    "rows": len(distance_bin_statistics),
                    "interpretation": (
                        "Mean ± 1 SD describes within-bin dispersion. Inferred-field bins depend "
                        "on the configured target-point layout and density; they are not confidence "
                        "intervals, prediction intervals, or independent accuracy validation."
                    ),
                },
                "nonpositivePhaseVelocitySamples": nonpositive_counts,
            },
            "outputs": {
                "observedRows": len(observed),
                "inferredRows": len(inferred),
                "candidateTargetPoints": len(quality),
                "candidateGridPoints": len(quality),
                "skippedPoints": skipped,
                "fullDatIncluded": save_dat,
                "resultPackageIncluded": make_package,
                "datFileCount": dat_file_count,
                "datDirectory": "IF_folder" if save_dat else None,
                "datDirectories": dat_directories if save_dat else [],
                "ifFolderDatFiles": if_folder_dat_files,
                "displayWaveforms": len(waveforms),
                "retainedInferredWaveforms": len(simulated),
                "distanceBinStatistics": DISTANCE_BIN_STATISTICS_FILE,
                "distanceBinStatisticsRows": len(distance_bin_statistics),
            },
            "warnings": inspection.warnings,
            "basemap": (
                {
                    **basemap_metadata,
                    "sourceFingerprintSha256": basemap_hash,
                    "attribution": basemap_metadata.get("attribution", "天地图行政区划数据"),
                    "distributionBoundary": (
                        "The generated GeoJSON is a local derivative of third-party TianDiTu data. "
                        "It is retained for local display and excluded from the downloadable result package. "
                        "The project MIT license does not grant redistribution rights for the source map data."
                    ),
                    "licenseEvidence": basemap_metadata.get("licenseEvidence"),
                    "acquisitionDate": basemap_metadata.get("acquisitionDate"),
                    "metadataBoundary": basemap_metadata.get(
                        "metadataBoundary",
                        "The supplied administrative files did not provide verified license or acquisition-date evidence.",
                    ),
                }
                if basemap_metadata
                else None
            ),
            "interpretationBoundary": (
                "The inferred field is one reproducible random-phase realization of the ZSL method. "
                "It is an event-processing result, not an independent accuracy validation. "
                "Distance-bin spread is descriptive and is not an uncertainty or accuracy interval. "
                "Inferred-bin summaries depend on the configured target-point layout and density. "
                "The K-NET residual model is not applied to these CSMNC events."
            ),
        }
        json_write(temporary_dir / "run_metadata.json", run_metadata)
        if basemap_metadata:
            (temporary_dir / "LOCAL_BASEMAP_NOTICE.txt").write_text(
                "本事件的 admin-boundaries.geojson 是第三方天地图行政区划数据的本机显示衍生文件。\n"
                "项目 MIT 许可不涵盖该地图数据；因现有资料未提供可核验的许可与获取日期，\n"
                "结果 ZIP 默认不包含该 GeoJSON。公开发布前需另行核验数据许可与地图合规要求。\n",
                encoding="utf-8",
            )
        pipeline_run = {
            "status": "success",
            "eventId": inspection.event_id,
            "startedAt": started_at,
            "durationSeconds": round(time.perf_counter() - started, 3),
            "warnings": inspection.warnings,
            "outputs": run_metadata["outputs"],
        }
        json_write(temporary_dir / "pipeline_run.json", pipeline_run)
        validate_result(
            {
                "observed": observed.to_dict(orient="records"),
                "inferred": inferred.drop(columns="key").to_dict(orient="records"),
            },
            temporary_dir,
            save_dat,
            candidate_points,
        )
        report_progress(
            "package_publish",
            "打包并发布",
            9,
            1,
            publication_total,
            "项",
            "元数据与运行记录已写出",
        )

        result_package = None
        if make_package:
            package_path = output_root / "packages" / f"{inspection.event_id}.zip"
            expected_dat_files = inferred_dat_manifest(inferred) if save_dat else {}
            package_bytes, package_hash = package_event(
                temporary_dir,
                package_path,
                inspection.event_id,
                expected_dat_files=expected_dat_files,
            )
            result_package = {
                "filename": package_path.name,
                "url": f"./demo/batch/packages/{package_path.name}",
                "bytes": package_bytes,
                "sha256": package_hash,
            }
            report_progress(
                "package_publish",
                "打包并发布",
                9,
                2,
                publication_total,
                "项",
                "结果 ZIP 已生成并校验",
            )

        detail = {
            "schemaVersion": 1,
            "demoOnly": False,
            "projectResult": True,
            "archiveId": archive_id,
            "eventId": inspection.event_id,
            "title": inspection.title,
            "originTime": metadata["origin_time"],
            "boundaryUrl": "./demo/world-land.geojson",
            "basemapUrl": f"{artifact_base}/{basemap_filename}" if basemap_metadata else None,
            "basemap": run_metadata["basemap"],
            "config": config,
            "observed": observed.to_dict(orient="records"),
            "inferred": inferred.drop(columns="key").to_dict(orient="records"),
            "distanceBinStatistics": distance_bin_statistics.astype(object).where(
                pd.notna(distance_bin_statistics), None
            ).to_dict(orient="records"),
            "waveforms": waveforms,
            "artifacts": artifacts,
            "resultPackage": result_package,
            "resultFiles": {
                "observedCsv": f"{artifact_base}/observed.csv",
                "inferredCsv": f"{artifact_base}/inferred.csv",
                "qualityControlCsv": f"{artifact_base}/target_point_quality_control.csv",
                "distanceBinStatistics": f"{artifact_base}/{DISTANCE_BIN_STATISTICS_FILE}",
                "runMetadata": f"{artifact_base}/run_metadata.json",
                "pipelineRun": f"{artifact_base}/pipeline_run.json",
                "inferredDatDirectory": (
                    f"{artifact_base}/IF_folder" if save_dat else None
                ),
            },
            "summary": summary,
            "sourceLabel": "CSMNC + ZSL",
            "sourceDetail": "CSMNC 三分量实测 + ZSL Step2/Step3 推测",
            "badges": ["CSMNC 输入", "ZSL 推测结果", f"{len(inferred)} 场点"],
            "integrityNote": (
                f"{len(observed)} 个完整三分量实测台站进入计算；{candidate_points} 个候选点中 "
                f"{len(inferred)} 个通过空间质控。"
                f"{len(inspection.invalid_files)} 个无效分量文件、{len(inspection.incomplete_stations)} 个非完整台站已记录。"
                "推测采用按输入内容、分量和四位小数坐标稳定派生种子的单次随机相位实现。"
            ),
            "provenance": {
                "summary": (
                    f"CSMNC · {inspection.source.relative_path} · {len(observed)} observed · "
                    f"{len(inferred)} inferred · seeds {seed_label}"
                ),
                "event": metadata,
                "source": run_metadata["source"],
                "method": run_metadata["method"],
                "targetConfiguration": target_summary,
                "outputs": run_metadata["outputs"],
                "boundary": run_metadata["interpretationBoundary"],
            },
        }
        json_write(temporary_dir / "event.json", detail)
        if final_dir.exists():
            shutil.rmtree(final_dir)
        temporary_dir.replace(final_dir)
        report_progress(
            "package_publish",
            "打包并发布",
            9,
            publication_total,
            publication_total,
            "项",
            "事件结果已发布",
        )
        return detail
    except Exception:
        shutil.rmtree(temporary_dir, ignore_errors=True)
        raise


def catalog_entry(detail: dict) -> dict:
    config = detail["config"]
    return {
        "eventId": detail["eventId"],
        "title": detail["title"],
        "originTime": detail["originTime"],
        "magnitude": config["mag"],
        "magnitudeType": detail.get("provenance", {}).get("event", {}).get("magnitude_type", "M"),
        "stationCount": len(detail["observed"]),
        "latitude": config["e_lat"],
        "longitude": config["e_lon"],
        "depthKm": config["depth_hy"],
        "inferredCount": len(detail["inferred"]),
        "candidateCount": detail.get("summary", {}).get(
            "candidateTargetPoints",
            detail.get("summary", {}).get("candidateGridPoints"),
        ),
        "skippedCount": detail.get("summary", {}).get("skippedPoints"),
        "boundaryUrl": detail.get("boundaryUrl"),
        "basemapUrl": detail.get("basemapUrl"),
        "distanceBinStatistics": detail.get("resultFiles", {}).get(
            "distanceBinStatistics"
        ),
        "url": f"./demo/batch/events/{detail['eventId']}/event.json",
    }


def write_batch_catalog(details: list[dict], summary: dict) -> dict:
    entries = sorted(
        [catalog_entry(detail) for detail in details],
        key=lambda entry: (entry.get("originTime", ""), entry["eventId"]),
        reverse=True,
    )
    catalog = {
        "schemaVersion": 1,
        "source": "CSMNC recursive Windows batch",
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "defaultEventId": entries[0]["eventId"] if entries else None,
        "defaultBoundaryUrl": "./demo/world-land.geojson",
        "eventCount": len(entries),
        "runSummaryUrl": "./demo/batch/run_summary.json",
        "events": entries,
    }
    json_write(BATCH_CATALOG, catalog)
    json_write(DEFAULT_OUTPUT_ROOT / "run_summary.json", summary)
    return catalog


def published_details(output_root: Path) -> list[dict]:
    details = []
    for path in sorted((output_root / "events").glob("*/event.json")):
        try:
            detail = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            print(f"忽略无法读取的既有事件 {path}: {error}", file=sys.stderr)
            continue
        if isinstance(detail, dict) and detail.get("eventId"):
            details.append(detail)
    return details


def cached_detail(
    inspection: EventInspection,
    output_root: Path,
    method_hash: str,
    basemap_hash: str | None = None,
    *,
    save_dat: bool = True,
    make_package: bool = True,
) -> dict | None:
    event_dir = output_root / "events" / inspection.event_id
    metadata_path = event_dir / "run_metadata.json"
    detail_path = event_dir / "event.json"
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        detail = json.loads(detail_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    source = metadata.get("source", {})
    outputs = metadata.get("outputs", {})
    if source.get("snapshotSha256") != inspection.snapshot_sha256:
        return None
    if source.get("eventMetadataSha256") != event_metadata_sha256(inspection.metadata):
        return None
    if metadata.get("method", {}).get("pipelineSha256") != method_hash:
        return None
    if (metadata.get("basemap") or {}).get("sourceFingerprintSha256") != basemap_hash:
        return None
    if outputs.get("fullDatIncluded") is not save_dat:
        return None
    if outputs.get("resultPackageIncluded") is not make_package:
        return None

    expected_dat_files = inferred_dat_manifest(detail.get("inferred", [])) if save_dat else {}
    actual_dat_paths = list(event_dir.rglob("*.dat"))
    if save_dat:
        for directory, expected_names in expected_dat_files.items():
            actual_names = {path.name for path in (event_dir / directory).glob("*.dat")}
            if actual_names != expected_names:
                return None
        expected_count = sum(len(names) for names in expected_dat_files.values())
        if len(actual_dat_paths) != expected_count:
            return None
    elif actual_dat_paths:
        return None

    result_package = detail.get("resultPackage")
    if make_package:
        if not isinstance(result_package, dict):
            return None
        filename = result_package.get("filename")
        if not isinstance(filename, str) or Path(filename).name != filename:
            return None
        package_path = output_root / "packages" / filename
        if not package_path.is_file() or package_path.stat().st_size != result_package.get("bytes"):
            return None
        expected_hash = result_package.get("sha256")
        if not isinstance(expected_hash, str) or sha256(package_path) != expected_hash:
            return None
    elif result_package is not None:
        return None
    return detail


def write_scan_reports(reports: list[dict]) -> None:
    json_write(DEFAULT_OUTPUT_ROOT / "scan_report.json", {"events": reports})
    columns = [
        "eventId",
        "title",
        "sourceRelativePath",
        "status",
        "sourceFiles",
        "parsedFiles",
        "completeStations",
        "invalidFileCount",
        "incompleteStationCount",
        "warningCount",
        "errorCount",
        "error",
    ]
    path = DEFAULT_OUTPUT_ROOT / "scan_report.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os_process_id()}")
    with temporary.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        for report in reports:
            writer.writerow(
                {
                    "eventId": report.get("eventId"),
                    "title": report.get("title"),
                    "sourceRelativePath": report.get("sourceRelativePath"),
                    "status": report.get("status"),
                    "sourceFiles": report.get("sourceFiles"),
                    "parsedFiles": report.get("parsedFiles"),
                    "completeStations": report.get("completeStations"),
                    "invalidFileCount": len(report.get("invalidFiles", [])),
                    "incompleteStationCount": len(report.get("incompleteStations", [])),
                    "warningCount": len(report.get("warnings", [])),
                    "errorCount": len(report.get("errors", [])),
                    "error": report.get("error") or " | ".join(report.get("errors", [])),
                }
            )
    temporary.replace(path)


def serve(
    port: int,
    tianditu_bundle: Path | None,
    tianditu_index: Path | None,
    admin_boundaries_dir: Path | None,
) -> None:
    # Imported lazily so the batch processor remains usable without starting a
    # web service.  The shared service keeps browser import, archive and folder
    # monitoring APIs available after a full-directory batch run.
    from local_archive_server import ArchiveService, handler_class

    service = ArchiveService(
        tianditu_bundle=tianditu_bundle,
        tianditu_index=tianditu_index,
        admin_boundaries_dir=admin_boundaries_dir,
    )
    try:
        server = ThreadingHTTPServer(("127.0.0.1", port), handler_class(service))
    except OSError as error:
        raise RuntimeError(f"端口 {port} 无法监听: {error}") from error
    url = f"http://127.0.0.1:{port}/"
    print(f"网站、导入、归档与监控服务已启动: {url}")
    webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n网站已停止")
    finally:
        service.stop_watch()
        server.server_close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-root", type=Path, required=True, help="包含年份/月/事件目录的总数据目录")
    parser.add_argument("--metadata-overrides", type=Path, help="显式解决事件头段元数据冲突的 JSON")
    parser.add_argument(
        "--tianditu-bundle",
        type=Path,
        help="天地图 1:25万全国扁平图幅 ZIP；同目录需有 tianditu_250k_batches.csv",
    )
    parser.add_argument("--tianditu-index", type=Path, help="可选的详细图幅索引 CSV")
    parser.add_argument(
        "--admin-boundaries-dir",
        type=Path,
        help="含中国_省/市/县.geojson 的天地图行政区划目录；默认从天地图 ZIP 旁自动查找",
    )
    parser.add_argument("--match", help="只处理相对路径或事件 ID 中包含该文本的事件")
    parser.add_argument("--limit", type=int, help="最多处理的事件数，用于冒烟测试")
    parser.add_argument("--scan-only", action="store_true", help="只解析头段并生成扫描报告")
    parser.add_argument("--overwrite", action="store_true", help="忽略缓存并重算")
    parser.add_argument(
        "--save-dat",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="保存全部推测三分量 DAT（默认开启；用 --no-save-dat 关闭）",
    )
    parser.add_argument("--no-package", action="store_true", help="不生成每事件结果 ZIP")
    parser.add_argument("--serve", action="store_true", help="处理完成后启动本地网站")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--strict", action="store_true", help="存在无效或失败事件时返回非零退出码")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    input_root = args.input_root.resolve()
    output_root = DEFAULT_OUTPUT_ROOT.resolve()
    if not input_root.is_dir():
        print(f"数据总目录不存在: {input_root}", file=sys.stderr)
        return 2
    if args.limit is not None and args.limit < 1:
        print("--limit 必须大于 0", file=sys.stderr)
        return 2
    if not WORLD_BOUNDARY.is_file():
        print(f"缺少网站全球底图: {WORLD_BOUNDARY}", file=sys.stderr)
        return 2

    try:
        overrides = load_overrides(args.metadata_overrides)
    except ValueError as error:
        print(error, file=sys.stderr)
        return 2

    tianditu_bundle = args.tianditu_bundle.resolve() if args.tianditu_bundle else None
    tianditu_index = args.tianditu_index.resolve() if args.tianditu_index else None
    if tianditu_bundle is not None:
        if not tianditu_bundle.is_file():
            print(f"天地图总包不存在: {tianditu_bundle}", file=sys.stderr)
            return 2
        if tianditu_index is None:
            tianditu_index = tianditu_bundle.with_name("tianditu_250k_batches.csv")
        if not tianditu_index.is_file():
            print(f"天地图图幅索引不存在: {tianditu_index}", file=sys.stderr)
            return 2
    elif tianditu_index is not None:
        print("--tianditu-index 只能与 --tianditu-bundle 一起使用", file=sys.stderr)
        return 2

    admin_boundaries_dir = (
        args.admin_boundaries_dir.resolve() if args.admin_boundaries_dir else None
    )
    if admin_boundaries_dir is None and tianditu_bundle is not None:
        admin_boundaries_dir = infer_admin_boundaries_dir(tianditu_bundle)
    if admin_boundaries_dir is not None:
        missing_admin = [
            filename
            for _, filename in ADMIN_LEVEL_FILES
            if not (admin_boundaries_dir / filename).is_file()
        ]
        if missing_admin:
            print(
                f"行政区划目录缺少文件: {', '.join(missing_admin)}",
                file=sys.stderr,
            )
            return 2

    sources = discover_events(input_root)
    if args.match:
        sources = [source for source in sources if args.match.casefold() in source.relative_path.casefold()]
    if args.limit:
        sources = sources[: args.limit]
    if not sources:
        print("没有发现可扫描的 .dat 事件目录", file=sys.stderr)
        return 2
    print(f"发现 {len(sources)} 个候选事件目录，开始串行检查")

    inspections = []
    for index, source in enumerate(sources, 1):
        print(f"[{index}/{len(sources)}] 检查 {source.relative_path}")
        inspection = inspect_event(source, input_root, overrides.get(source.relative_path))
        inspections.append(inspection)
        print(
            f"  {inspection.status}: {inspection.complete_stations} 个完整台站，"
            f"{len(inspection.invalid_files)} 个无效文件，{len(inspection.errors)} 个错误"
        )

    reports = [inspection.report() for inspection in inspections]
    write_scan_reports(reports)
    if args.scan_only:
        summary = {
            "mode": "scan-only",
            "discovered": len(inspections),
            "ready": sum(inspection.status == "ready" for inspection in inspections),
            "notApplicable": sum(inspection.status == "not_applicable" for inspection in inspections),
            "invalidInput": sum(inspection.status == "invalid_input" for inspection in inspections),
        }
        json_write(DEFAULT_OUTPUT_ROOT / "run_summary.json", summary)
        print(json.dumps(summary, ensure_ascii=False))
        return 1 if args.strict and summary["ready"] != summary["discovered"] else 0

    boundary = json.loads(WORLD_BOUNDARY.read_text(encoding="utf-8"))
    try:
        method_hash = pipeline_sha256()
        basemap_hash = basemap_sha256(
            tianditu_bundle,
            tianditu_index,
            admin_boundaries_dir,
        )
    except (OSError, zipfile.BadZipFile, ValueError) as error:
        print(f"无法核验天地图总包: {error}", file=sys.stderr)
        return 2
    details = []
    success = cached = failed = 0
    for index, inspection in enumerate(inspections, 1):
        if inspection.status != "ready":
            continue
        print(f"[{index}/{len(inspections)}] 计算 {inspection.title} ({inspection.event_id})", flush=True)
        if not args.overwrite:
            detail = cached_detail(
                inspection,
                output_root,
                method_hash,
                basemap_hash,
                save_dat=args.save_dat,
                make_package=not args.no_package,
            )
            if detail is not None:
                cached += 1
                details.append(detail)
                reports[index - 1]["status"] = "cached"
                print("  输入和算法哈希未变化，使用缓存")
                continue
        started = time.perf_counter()
        try:
            detail = build_event(
                inspection,
                output_root,
                boundary,
                save_dat=args.save_dat,
                make_package=not args.no_package,
                method_hash=method_hash,
                tianditu_bundle=tianditu_bundle,
                tianditu_index=tianditu_index,
                admin_boundaries_dir=admin_boundaries_dir,
                basemap_hash=basemap_hash,
            )
        except Exception as error:
            failed += 1
            reports[index - 1]["status"] = "failed"
            reports[index - 1]["error"] = f"{type(error).__name__}: {error}"
            reports[index - 1]["traceback"] = traceback.format_exc()
            print(f"  失败: {type(error).__name__}: {error}", file=sys.stderr)
            continue
        details.append(detail)
        success += 1
        reports[index - 1]["status"] = "success"
        reports[index - 1]["durationSeconds"] = round(time.perf_counter() - started, 3)
        print(
            f"  完成: {len(detail['observed'])} 实测站，{len(detail['inferred'])} 推测点，"
            f"耗时 {reports[index - 1]['durationSeconds']:.1f} s"
        )

    write_scan_reports(reports)
    all_details = published_details(output_root)
    summary = {
        "mode": "run",
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "inputRoot": str(input_root),
        "discovered": len(inspections),
        "success": success,
        "cached": cached,
        "published": len(all_details),
        "failed": failed,
        "notApplicable": sum(inspection.status == "not_applicable" for inspection in inspections),
        "invalidInput": sum(inspection.status == "invalid_input" for inspection in inspections),
        "saveDat": args.save_dat,
        "tiandituBasemap": bool(tianditu_bundle),
        "administrativeBasemap": bool(admin_boundaries_dir),
    }
    catalog = write_batch_catalog(all_details, summary)
    print(json.dumps({**summary, "catalogEvents": catalog["eventCount"]}, ensure_ascii=False))
    if args.serve:
        try:
            serve(args.port, tianditu_bundle, tianditu_index, admin_boundaries_dir)
        except (OSError, RuntimeError, ValueError) as error:
            print(f"本机服务无法启动: {error}", file=sys.stderr)
            return 2
    has_issues = failed or summary["notApplicable"] or summary["invalidInput"]
    return 1 if args.strict and has_issues else 0


if __name__ == "__main__":
    raise SystemExit(main())
