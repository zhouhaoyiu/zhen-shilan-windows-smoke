"""Build local TianDiTu-derived event basemaps.

The user's archive is a flat ZIP whose members are per-sheet ZIP files.  Each
per-sheet ZIP contains Shapefiles.  This module reads the nested archives in
memory and never expands the complete data set onto disk.  Administrative-only
maps are extracted separately from the supplied EPSG:4490 province, city and
county GeoJSON files because the 1:250k sheets do not contain boundary layers.
"""

from __future__ import annotations

import csv
from datetime import date, datetime
from decimal import Decimal
import io
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Iterable, Sequence
import zipfile

import shapefile


DEFAULT_LAYERS = ("hyda", "hydl", "lrdl", "lrrl", "agnp")
EVENT_CONTEXT_PROFILE = "event-context"
ADMIN_LEVEL_FILES = (
    ("admin-province", "中国_省.geojson"),
    ("admin-city", "中国_市.geojson"),
    ("admin-county", "中国_县.geojson"),
)
_PROPERTY_FIELDS = {
    "NAME",
    "PINYIN",
    "CLASS",
    "GRADE",
    "LEVEL",
    "TYPE",
    "XZNAME",
    "GB",
    "HYDC",
    "RN",
    "RTEG",
    "GNID",
}
_SHEET_RE = re.compile(
    r"^(?P<band>[a-vA-V])(?P<zone>\d{2})c(?P<row>\d{3})(?P<column>\d{3})$"
)


class BasemapError(ValueError):
    """The source bundle cannot produce a scientifically traceable basemap."""


def _validated_bbox(values: Sequence[float]) -> tuple[float, float, float, float]:
    if len(values) != 4:
        raise BasemapError("bbox 必须是 (lon_min, lat_min, lon_max, lat_max)")
    try:
        bbox = tuple(float(value) for value in values)
    except (TypeError, ValueError) as exc:
        raise BasemapError("bbox 必须由 4 个有限数值组成") from exc
    if not all(math.isfinite(value) for value in bbox):
        raise BasemapError("bbox 必须由 4 个有限数值组成")
    if bbox[0] >= bbox[2] or bbox[1] >= bbox[3]:
        raise BasemapError("bbox 的最小经纬度必须小于最大经纬度")
    return bbox  # type: ignore[return-value]


def _bbox_intersects(
    first: Sequence[float], second: Sequence[float]
) -> bool:
    return not (
        first[2] < second[0]
        or first[0] > second[2]
        or first[3] < second[1]
        or first[1] > second[3]
    )


def _decode_sheet_bbox(sheet: str) -> tuple[float, float, float, float]:
    """Decode the standard 1:250k sheet number used by the supplied bundle."""
    match = _SHEET_RE.fullmatch(sheet)
    if not match:
        raise BasemapError(f"无法解析 1:25 万图幅号: {sheet}")
    band = ord(match.group("band").upper()) - ord("A")
    zone = int(match.group("zone"))
    row = int(match.group("row"))
    column = int(match.group("column"))
    if not (1 <= zone <= 60 and 1 <= row <= 4 and 1 <= column <= 4):
        raise BasemapError(f"1:25 万图幅号超出有效范围: {sheet}")
    lon_min = (zone - 31) * 6.0 + (column - 1) * 1.5
    lat_min = band * 4.0 + (4 - row) * 1.0
    return lon_min, lat_min, lon_min + 1.5, lat_min + 1.0


def _read_csv_rows(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        raise BasemapError(f"天地图索引 CSV 不存在: {path}")
    last_error: UnicodeDecodeError | None = None
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            with path.open("r", encoding=encoding, newline="") as handle:
                reader = csv.DictReader(handle)
                if not reader.fieldnames:
                    raise BasemapError(f"天地图索引 CSV 没有表头: {path}")
                return [
                    {
                        str(key).strip(): (value or "").strip()
                        for key, value in row.items()
                        if key is not None
                    }
                    for row in reader
                ]
        except UnicodeDecodeError as exc:
            last_error = exc
    raise BasemapError(f"无法以 UTF-8 或 GB18030 读取索引 CSV: {path}") from last_error


def _sheet_index(path: Path) -> list[dict[str, Any]]:
    rows = _read_csv_rows(path)
    if not rows:
        raise BasemapError(f"天地图索引 CSV 没有图幅记录: {path}")

    fields = set(rows[0])
    sheets: list[dict[str, Any]] = []
    if {"tile_id", "lon_min", "lat_min", "lon_max", "lat_max"} <= fields:
        for number, row in enumerate(rows, 2):
            sheet = row.get("tile_id", "").lower()
            if not sheet:
                raise BasemapError(f"索引 CSV 第 {number} 行缺少 tile_id")
            try:
                bbox = _validated_bbox(
                    (
                        row["lon_min"],
                        row["lat_min"],
                        row["lon_max"],
                        row["lat_max"],
                    )
                )
            except BasemapError as exc:
                raise BasemapError(f"索引 CSV 第 {number} 行图幅范围无效: {exc}") from exc
            available = row.get("available", "1").lower() not in {
                "0",
                "false",
                "no",
                "n",
            }
            sheets.append(
                {
                    "sheet": sheet,
                    "member": row.get("inner_zip") or f"{sheet}.zip",
                    "bbox": bbox,
                    "available": available,
                }
            )
    elif "id_map_nums" in fields:
        for number, row in enumerate(rows, 2):
            identifiers = [
                value.strip().lower()
                for value in row.get("id_map_nums", "").split(",")
                if value.strip()
            ]
            if not identifiers:
                raise BasemapError(f"批次 CSV 第 {number} 行没有图幅号")
            if row.get("count"):
                try:
                    count = int(row["count"])
                except ValueError as exc:
                    raise BasemapError(f"批次 CSV 第 {number} 行 count 不是整数") from exc
                if count != len(identifiers):
                    raise BasemapError(
                        f"批次 CSV 第 {number} 行 count={count}，实际有 {len(identifiers)} 个图幅"
                    )
            sheets.extend(
                {
                    "sheet": sheet,
                    "member": f"{sheet}.zip",
                    "bbox": _decode_sheet_bbox(sheet),
                    "available": True,
                }
                for sheet in identifiers
            )
    else:
        raise BasemapError(
            "索引 CSV 需要 tile_id/lon_min/lat_min/lon_max/lat_max，"
            "或批次 CSV 的 id_map_nums 字段"
        )

    seen: set[str] = set()
    for item in sheets:
        if item["sheet"] in seen:
            raise BasemapError(f"索引 CSV 中图幅号重复: {item['sheet']}")
        seen.add(item["sheet"])
    return sheets


def _point_in_bbox(point: Sequence[float], bbox: Sequence[float]) -> bool:
    return bbox[0] <= point[0] <= bbox[2] and bbox[1] <= point[1] <= bbox[3]


def _segment_intersects_bbox(
    first: Sequence[float], second: Sequence[float], bbox: Sequence[float]
) -> bool:
    """Liang-Barsky segment/rectangle intersection test."""
    if _point_in_bbox(first, bbox) or _point_in_bbox(second, bbox):
        return True
    x0, y0 = first[:2]
    dx, dy = second[0] - x0, second[1] - y0
    lower, upper = 0.0, 1.0
    for p, q in (
        (-dx, x0 - bbox[0]),
        (dx, bbox[2] - x0),
        (-dy, y0 - bbox[1]),
        (dy, bbox[3] - y0),
    ):
        if p == 0:
            if q < 0:
                return False
            continue
        ratio = q / p
        if p < 0:
            if ratio > upper:
                return False
            lower = max(lower, ratio)
        else:
            if ratio < lower:
                return False
            upper = min(upper, ratio)
    return lower <= upper


def _line_intersects_bbox(
    coordinates: Sequence[Sequence[float]], bbox: Sequence[float]
) -> bool:
    return any(
        _segment_intersects_bbox(first, second, bbox)
        for first, second in zip(coordinates, coordinates[1:])
    ) or (bool(coordinates) and _point_in_bbox(coordinates[0], bbox))


def _point_in_ring(point: Sequence[float], ring: Sequence[Sequence[float]]) -> bool:
    inside = False
    x, y = point[:2]
    for first, second in zip(ring, ring[1:] + ring[:1]):
        x1, y1 = first[:2]
        x2, y2 = second[:2]
        if _segment_intersects_bbox(first, second, (x, y, x, y)):
            return True
        if (y1 > y) != (y2 > y):
            crossing_x = (x2 - x1) * (y - y1) / (y2 - y1) + x1
            if x < crossing_x:
                inside = not inside
    return inside


def _polygon_intersects_bbox(
    rings: Sequence[Sequence[Sequence[float]]], bbox: Sequence[float]
) -> bool:
    if any(_line_intersects_bbox(ring, bbox) for ring in rings):
        return True
    corners = (
        (bbox[0], bbox[1]),
        (bbox[0], bbox[3]),
        (bbox[2], bbox[1]),
        (bbox[2], bbox[3]),
    )
    if not rings:
        return False
    return any(
        _point_in_ring(corner, rings[0])
        and not any(_point_in_ring(corner, hole) for hole in rings[1:])
        for corner in corners
    )


def _geometry_intersects_bbox(geometry: dict[str, Any], bbox: Sequence[float]) -> bool:
    kind = geometry.get("type")
    coordinates = geometry.get("coordinates", [])
    if kind == "Point":
        return _point_in_bbox(coordinates, bbox)
    if kind == "MultiPoint":
        return any(_point_in_bbox(point, bbox) for point in coordinates)
    if kind == "LineString":
        return _line_intersects_bbox(coordinates, bbox)
    if kind == "MultiLineString":
        return any(_line_intersects_bbox(line, bbox) for line in coordinates)
    if kind == "Polygon":
        return _polygon_intersects_bbox(coordinates, bbox)
    if kind == "MultiPolygon":
        return any(_polygon_intersects_bbox(polygon, bbox) for polygon in coordinates)
    return False


def infer_admin_boundaries_dir(
    bundle_path: str | os.PathLike[str],
) -> Path | None:
    """Find the complete administrative GeoJSON directory beside a bundle."""
    bundle = Path(bundle_path)
    for candidate in (
        bundle.parent.parent / "admin_geojson",
        bundle.parent / "admin_geojson",
    ):
        if all((candidate / filename).is_file() for _, filename in ADMIN_LEVEL_FILES):
            return candidate
    return None


def _perpendicular_distance(
    point: Sequence[float], first: Sequence[float], last: Sequence[float]
) -> float:
    dx, dy = last[0] - first[0], last[1] - first[1]
    if dx == dy == 0:
        return math.hypot(point[0] - first[0], point[1] - first[1])
    return abs(dy * point[0] - dx * point[1] + last[0] * first[1] - last[1] * first[0]) / math.hypot(dx, dy)


def _simplify_line(
    coordinates: Sequence[Sequence[float]], tolerance: float
) -> list[Sequence[float]]:
    if tolerance <= 0 or len(coordinates) <= 2:
        return list(coordinates)
    distance, index = max(
        (
            _perpendicular_distance(point, coordinates[0], coordinates[-1]),
            index,
        )
        for index, point in enumerate(coordinates[1:-1], 1)
    )
    if distance <= tolerance:
        return [coordinates[0], coordinates[-1]]
    left = _simplify_line(coordinates[: index + 1], tolerance)
    right = _simplify_line(coordinates[index:], tolerance)
    return left[:-1] + right


def _simplify_ring(
    coordinates: Sequence[Sequence[float]], tolerance: float
) -> list[Sequence[float]]:
    if tolerance <= 0 or len(coordinates) <= 5:
        return list(coordinates)
    points = list(coordinates[:-1] if coordinates[0] == coordinates[-1] else coordinates)
    if len(points) < 4:
        return list(coordinates)
    split = max(
        range(1, len(points)),
        key=lambda index: (points[index][0] - points[0][0]) ** 2
        + (points[index][1] - points[0][1]) ** 2,
    )
    first_arc = _simplify_line(points[: split + 1], tolerance)
    second_arc = _simplify_line(points[split:] + [points[0]], tolerance)
    simplified = first_arc[:-1] + second_arc
    if len(simplified) < 4:
        return list(coordinates)
    if simplified[0] != simplified[-1]:
        simplified.append(simplified[0])
    return simplified


def _simplify_geometry(geometry: dict[str, Any], tolerance: float) -> dict[str, Any]:
    kind = geometry.get("type")
    coordinates = geometry.get("coordinates", [])
    if kind == "LineString":
        coordinates = _simplify_line(coordinates, tolerance)
    elif kind == "MultiLineString":
        coordinates = [_simplify_line(line, tolerance) for line in coordinates]
    elif kind == "Polygon":
        coordinates = [_simplify_ring(ring, tolerance) for ring in coordinates]
    elif kind == "MultiPolygon":
        coordinates = [
            [_simplify_ring(ring, tolerance) for ring in polygon]
            for polygon in coordinates
        ]
    return {"type": kind, "coordinates": coordinates}


def _json_scalar(value: Any) -> str | int | float | bool | None:
    if value is None or isinstance(value, (str, int, float, bool)):
        if isinstance(value, float) and not math.isfinite(value):
            return None
        return value
    if isinstance(value, Decimal):
        number = float(value)
        return number if math.isfinite(number) else None
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return str(value)


def _properties(record: Any, layer: str, sheet: str) -> dict[str, Any]:
    values = record.as_dict() if hasattr(record, "as_dict") else dict(record)
    kept = {
        str(key): _json_scalar(value)
        for key, value in values.items()
        if str(key).upper() in _PROPERTY_FIELDS and value not in (None, "")
    }
    return {"layer": layer, "sheet": sheet, **kept}


def _component_members(
    inner: zipfile.ZipFile, sheet: str, layer: str
) -> tuple[str, str, str] | None:
    names = {name.replace("\\", "/").lower(): name for name in inner.namelist()}
    stems: dict[str, str] = {}
    for normalized, original in names.items():
        if normalized.endswith(f"/{layer}.shp") or normalized == f"{layer}.shp":
            stems["shp"] = original
            base = normalized[:-4]
            for extension in ("shx", "dbf"):
                candidate = names.get(f"{base}.{extension}")
                if candidate:
                    stems[extension] = candidate
            break
    if not stems:
        return None
    missing = [extension for extension in ("shp", "shx", "dbf") if extension not in stems]
    if missing:
        raise BasemapError(
            f"图幅 {sheet} 的 {layer} 图层缺少: {', '.join('.' + item for item in missing)}"
        )
    return stems["shp"], stems["shx"], stems["dbf"]


def _layer_features(
    inner: zipfile.ZipFile,
    sheet: str,
    layer: str,
    bbox: Sequence[float],
    simplify_tolerance: float,
) -> list[dict[str, Any]] | None:
    members = _component_members(inner, sheet, layer)
    if members is None:
        return None
    try:
        reader = shapefile.Reader(
            shp=io.BytesIO(inner.read(members[0])),
            shx=io.BytesIO(inner.read(members[1])),
            dbf=io.BytesIO(inner.read(members[2])),
            encoding="gb18030",
            encodingErrors="replace",
        )
    except (OSError, ValueError, shapefile.ShapefileException, zipfile.BadZipFile) as exc:
        raise BasemapError(f"无法读取图幅 {sheet} 的 {layer} Shapefile: {exc}") from exc

    features: list[dict[str, Any]] = []
    try:
        for item in reader.iterShapeRecords():
            shape = item.shape
            shape_bbox = getattr(shape, "bbox", None)
            if shape_bbox and not _bbox_intersects(shape_bbox, bbox):
                continue
            geometry = dict(shape.__geo_interface__)
            if not _geometry_intersects_bbox(geometry, bbox):
                continue
            features.append(
                {
                    "type": "Feature",
                    "geometry": _simplify_geometry(geometry, simplify_tolerance),
                    "properties": _properties(item.record, layer, sheet),
                }
            )
    except (OSError, ValueError, shapefile.ShapefileException) as exc:
        raise BasemapError(f"读取图幅 {sheet} 的 {layer} 要素失败: {exc}") from exc
    finally:
        reader.close()
    return features


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            newline="\n",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            json.dump(value, handle, ensure_ascii=False, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        if temporary_path and temporary_path.exists():
            temporary_path.unlink()


def build_admin_basemap(
    admin_dir: str | os.PathLike[str],
    bbox: Sequence[float],
    output_path: str | os.PathLike[str],
    simplify_tolerance: float = 0.001,
) -> dict[str, Any]:
    """Build an event-sized administrative basemap from the supplied GeoJSON."""
    source_dir = Path(admin_dir)
    output = Path(output_path)
    requested_bbox = _validated_bbox(bbox)
    if simplify_tolerance < 0 or not math.isfinite(simplify_tolerance):
        raise BasemapError("simplify_tolerance 必须是有限的非负数")

    features: list[dict[str, Any]] = []
    layer_counts: dict[str, int] = {}
    source_hashes: dict[str, str] = {}
    for layer, filename in ADMIN_LEVEL_FILES:
        source = source_dir / filename
        if not source.is_file():
            raise BasemapError(f"行政区划 GeoJSON 不存在: {source}")
        try:
            raw = source.read_bytes()
            document = json.loads(raw.decode("utf-8-sig"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BasemapError(f"无法读取行政区划 GeoJSON: {source}: {exc}") from exc
        if not isinstance(document, dict) or document.get("type") != "FeatureCollection":
            raise BasemapError(f"行政区划文件不是 GeoJSON FeatureCollection: {source}")
        crs = document.get("crs") or {}
        crs_name = str((crs.get("properties") or {}).get("name", "")) if isinstance(crs, dict) else ""
        if not re.search(r"(?:^|[^0-9])4490$", crs_name):
            raise BasemapError(f"行政区划 GeoJSON 需要 EPSG:4490 CRS: {source}")
        rows = document.get("features")
        if not isinstance(rows, list):
            raise BasemapError(f"行政区划 GeoJSON 缺少 features 数组: {source}")

        count = 0
        for number, item in enumerate(rows, 1):
            if not isinstance(item, dict):
                raise BasemapError(f"{source} 第 {number} 个要素无效")
            geometry = item.get("geometry") or {}
            if not isinstance(geometry, dict) or geometry.get("type") not in {
                "Polygon",
                "MultiPolygon",
            }:
                continue
            properties = item.get("properties") or {}
            if not isinstance(properties, dict):
                raise BasemapError(f"{source} 第 {number} 个要素 properties 无效")
            name = str(properties.get("name", "")).strip()
            if name == "境界线":
                continue
            try:
                if not _geometry_intersects_bbox(geometry, requested_bbox):
                    continue
                simplified = _simplify_geometry(geometry, simplify_tolerance)
            except (IndexError, TypeError, ValueError) as exc:
                raise BasemapError(f"{source} 第 {number} 个要素几何无效: {exc}") from exc
            features.append(
                {
                    "type": "Feature",
                    "geometry": simplified,
                    "properties": {
                        "layer": layer,
                        "name": name,
                        "gb": str(properties.get("gb", "")).strip(),
                    },
                }
            )
            count += 1
        layer_counts[layer] = count
        source_hashes[filename] = hashlib.sha256(raw).hexdigest()

    if not features:
        raise BasemapError(
            f"行政区划数据中没有与 bbox={requested_bbox} 相交的面要素"
        )
    metadata: dict[str, Any] = {
        "source": "天地图行政区划数据",
        "crs": "EPSG:4490",
        "horizontalDatum": "CGCS2000",
        "coordinateReference": "EPSG:4490 CGCS2000 geographic longitude/latitude; coordinates emitted unchanged",
        "bbox": list(requested_bbox),
        "layers": [layer for layer, _ in ADMIN_LEVEL_FILES],
        "layerFeatureCounts": layer_counts,
        "featureCount": len(features),
        "simplifyToleranceDegrees": simplify_tolerance,
        "sourceFiles": dict(ADMIN_LEVEL_FILES),
        "sourceFilesSha256": source_hashes,
        "attribution": "天地图行政区划数据",
        "licenseEvidence": None,
        "acquisitionDate": None,
        "metadataBoundary": (
            "The supplied administrative GeoJSON did not provide verified license "
            "or acquisition-date evidence."
        ),
    }
    collection = {
        "type": "FeatureCollection",
        "metadata": metadata,
        "features": features,
    }
    _atomic_json(output, collection)
    return metadata


def _geometry_measure(geometry: dict[str, Any]) -> float:
    """Return a deterministic display-importance proxy in source degrees."""
    kind = geometry.get("type")
    coordinates = geometry.get("coordinates") or []
    if kind == "LineString":
        return sum(
            math.hypot(second[0] - first[0], second[1] - first[1])
            for first, second in zip(coordinates, coordinates[1:])
        )
    if kind == "MultiLineString":
        return sum(_geometry_measure({"type": "LineString", "coordinates": line}) for line in coordinates)
    if kind == "Polygon":
        rings = coordinates[:1]
    elif kind == "MultiPolygon":
        rings = [ring for polygon in coordinates for ring in polygon[:1]]
    else:
        return 0.0
    return sum(
        abs(
            sum(
                first[0] * second[1] - second[0] * first[1]
                for first, second in zip(ring, ring[1:] + ring[:1])
            )
        )
        / 2
        for ring in rings
    )


def _road_class(properties: dict[str, Any]) -> str:
    road_number = str(properties.get("RN", "")).upper()
    grade = str(properties.get("RTEG", ""))
    if road_number.startswith("G") or grade in {"高速", "一级", "二级"}:
        return "major"
    if road_number.startswith("S") or grade == "三级":
        return "secondary"
    return "minor"


def _merge_geometry_features(
    features: list[dict[str, Any]], layer: str, group: str | None = None
) -> dict[str, Any] | None:
    if layer == "hyda":
        polygons: list[Any] = []
        for feature in features:
            geometry = feature["geometry"]
            if geometry["type"] == "Polygon":
                polygons.append(geometry["coordinates"])
            elif geometry["type"] == "MultiPolygon":
                polygons.extend(geometry["coordinates"])
        geometry = {"type": "MultiPolygon", "coordinates": polygons}
    else:
        lines: list[Any] = []
        for feature in features:
            geometry = feature["geometry"]
            if geometry["type"] == "LineString":
                lines.append(geometry["coordinates"])
            elif geometry["type"] == "MultiLineString":
                lines.extend(geometry["coordinates"])
        geometry = {"type": "MultiLineString", "coordinates": lines}
    if not geometry["coordinates"]:
        return None
    properties: dict[str, Any] = {
        "layer": layer,
        "generalized": True,
        "sourceFeatureCount": len(features),
    }
    if group is not None:
        properties["roadClass"] = group
    return {"type": "Feature", "geometry": geometry, "properties": properties}


def _event_context_features(
    features: list[dict[str, Any]], bbox: Sequence[float]
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Generalize a source-faithful extract for a finite event-sized screen map."""
    span = max(bbox[2] - bbox[0], bbox[3] - bbox[1])
    budgets = {
        "hyda": 1500 if span <= 1.5 else 900,
        "hydl": 6000 if span <= 1.5 else 2800,
        "lrdl": 3000 if span <= 1.5 else 1400,
        "lrrl": 1000,
    }
    by_layer = {
        layer: [feature for feature in features if feature["properties"]["layer"] == layer]
        for layer in DEFAULT_LAYERS
    }
    selected: dict[str, list[dict[str, Any]]] = {}
    for layer in ("hyda", "hydl", "lrrl"):
        rows = by_layer[layer]
        if layer == "hydl":
            rows = sorted(
                rows,
                key=lambda feature: (
                    bool(str(feature["properties"].get("NAME", "")).strip()),
                    _geometry_measure(feature["geometry"]),
                ),
                reverse=True,
            )
        else:
            rows = sorted(rows, key=lambda feature: _geometry_measure(feature["geometry"]), reverse=True)
        selected[layer] = rows[: budgets[layer]]

    selected["lrdl"] = sorted(
        by_layer["lrdl"],
        key=lambda feature: (
            {"major": 2, "secondary": 1, "minor": 0}[_road_class(feature["properties"])],
            _geometry_measure(feature["geometry"]),
        ),
        reverse=True,
    )[: budgets["lrdl"]]
    selected["agnp"] = [
        feature
        for feature in by_layer["agnp"]
        if str(feature["properties"].get("CLASS", "")) in {"AC", "AD", "AE", "AF"}
    ]

    rendered: list[dict[str, Any]] = []
    for layer in ("hyda", "hydl", "lrrl"):
        merged = _merge_geometry_features(selected[layer], layer)
        if merged:
            rendered.append(merged)
    for road_class in ("minor", "secondary", "major"):
        rows = [
            feature
            for feature in selected["lrdl"]
            if _road_class(feature["properties"]) == road_class
        ]
        merged = _merge_geometry_features(rows, "lrdl", road_class)
        if merged:
            rendered.append(merged)
    rendered.extend(selected["agnp"])
    audit = {
        "profile": EVENT_CONTEXT_PROFILE,
        "maximumSpanDegrees": span,
        "sourceFeatureCounts": {layer: len(rows) for layer, rows in by_layer.items()},
        "selectedSourceFeatureCounts": {layer: len(rows) for layer, rows in selected.items()},
        "renderFeatureCounts": {
            layer: sum(feature["properties"]["layer"] == layer for feature in rendered)
            for layer in DEFAULT_LAYERS
        },
        "selectionRule": (
            "deterministic screen-scale generalization: largest water areas; named/long hydrography; "
            "road class then length; all railways within budget; AC-AF place labels; merged geometries"
        ),
    }
    return rendered, audit


def _coverage_fraction(bbox: Sequence[float], sheets: Sequence[dict[str, Any]]) -> float:
    west, south, east, north = bbox
    rectangles = []
    for item in sheets:
        sw, ss, se, sn = item["bbox"]
        clipped = (max(west, sw), max(south, ss), min(east, se), min(north, sn))
        if clipped[0] < clipped[2] and clipped[1] < clipped[3]:
            rectangles.append(clipped)
    if not rectangles:
        return 0.0
    xs = sorted({west, east, *(value for rectangle in rectangles for value in rectangle[::2])})
    covered = 0.0
    for left, right in zip(xs, xs[1:]):
        if right <= left:
            continue
        intervals = sorted(
            (rectangle[1], rectangle[3])
            for rectangle in rectangles
            if rectangle[0] <= left and rectangle[2] >= right
        )
        merged_length = 0.0
        cursor_start = cursor_end = None
        for start, end in intervals:
            if cursor_start is None:
                cursor_start, cursor_end = start, end
            elif start <= cursor_end:
                cursor_end = max(cursor_end, end)
            else:
                merged_length += cursor_end - cursor_start
                cursor_start, cursor_end = start, end
        if cursor_start is not None:
            merged_length += cursor_end - cursor_start
        covered += (right - left) * merged_length
    total = (east - west) * (north - south)
    return min(1.0, max(0.0, covered / total))


def build_basemap(
    bundle_path: str | os.PathLike[str],
    batch_csv: str | os.PathLike[str],
    bbox: Sequence[float],
    output_path: str | os.PathLike[str],
    *,
    layers: Iterable[str] = DEFAULT_LAYERS,
    simplify_tolerance: float = 0.001,
    profile: str | None = None,
) -> dict[str, Any]:
    """Build one event-sized GeoJSON from the nested TianDiTu bundle.

    ``batch_csv`` accepts either the detailed ``tianditu_250k_index.csv`` or
    the supplied ``tianditu_250k_batches.csv``.  Coordinates are CGCS2000
    geographic longitude/latitude and are emitted unchanged.
    """
    bundle = Path(bundle_path)
    index_path = Path(batch_csv)
    output = Path(output_path)
    requested_bbox = _validated_bbox(bbox)
    if simplify_tolerance < 0 or not math.isfinite(simplify_tolerance):
        raise BasemapError("simplify_tolerance 必须是有限的非负数")
    if profile not in {None, EVENT_CONTEXT_PROFILE}:
        raise BasemapError(f"未知底图输出 profile: {profile}")
    selected_layers = tuple(dict.fromkeys(str(layer).strip().lower() for layer in layers))
    if not selected_layers or any(layer not in DEFAULT_LAYERS for layer in selected_layers):
        raise BasemapError(f"图层必须来自: {', '.join(DEFAULT_LAYERS)}")
    if not bundle.is_file():
        raise BasemapError(f"天地图总包不存在: {bundle}")

    sheets = _sheet_index(index_path)
    selected = [
        item
        for item in sheets
        if item["available"] and _bbox_intersects(item["bbox"], requested_bbox)
    ]
    if not selected:
        raise BasemapError(f"索引中没有覆盖 bbox={requested_bbox} 的可用图幅")

    features: list[dict[str, Any]] = []
    missing_layers: dict[str, list[str]] = {}
    sheet_hashes: dict[str, str] = {}
    try:
        with zipfile.ZipFile(bundle) as outer:
            outer_names = set(outer.namelist())
            indexed_members = {
                item["member"] for item in sheets if item["available"]
            }
            absent = sorted(indexed_members - outer_names)
            if absent:
                preview = ", ".join(absent[:5])
                more = f" 等 {len(absent)} 个" if len(absent) > 5 else ""
                raise BasemapError(f"总包缺少索引标记为可用的图幅: {preview}{more}")

            for item in selected:
                sheet = item["sheet"]
                try:
                    nested_bytes = outer.read(item["member"])
                    sheet_hashes[sheet] = hashlib.sha256(nested_bytes).hexdigest()
                    with zipfile.ZipFile(io.BytesIO(nested_bytes)) as inner:
                        for layer in selected_layers:
                            layer_features = _layer_features(
                                inner,
                                sheet,
                                layer,
                                requested_bbox,
                                simplify_tolerance,
                            )
                            if layer_features is None:
                                missing_layers.setdefault(sheet, []).append(layer)
                            else:
                                features.extend(layer_features)
                except (OSError, RuntimeError, zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
                    raise BasemapError(f"图幅 {sheet} 的内层 ZIP 损坏或不可读: {exc}") from exc
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
        raise BasemapError(f"天地图总包损坏或不可读: {bundle}: {exc}") from exc

    if not features:
        raise BasemapError(
            f"图幅覆盖 bbox={requested_bbox}，但指定图层中没有与范围相交的要素"
        )

    source_layer_counts = {
        layer: sum(feature["properties"]["layer"] == layer for feature in features)
        for layer in selected_layers
    }
    generalization = None
    if profile == EVENT_CONTEXT_PROFILE:
        features, generalization = _event_context_features(features, requested_bbox)
    layer_counts = {
        layer: sum(feature["properties"]["layer"] == layer for feature in features)
        for layer in selected_layers
    }
    coverage_fraction = _coverage_fraction(requested_bbox, selected)
    metadata: dict[str, Any] = {
        "source": "天地图 1:25万公众版基础地理信息数据",
        "scale": "1:250000",
        "horizontalDatum": "CGCS2000",
        "coordinateReference": "CGCS2000 geographic longitude/latitude; coordinates emitted unchanged",
        "bbox": list(requested_bbox),
        "sheets": [item["sheet"] for item in selected],
        "layers": list(selected_layers),
        "layerFeatureCounts": layer_counts,
        "sourceLayerFeatureCounts": source_layer_counts,
        "featureCount": len(features),
        "sourceFeatureCount": sum(source_layer_counts.values()),
        "missingLayersBySheet": missing_layers,
        "simplifyToleranceDegrees": simplify_tolerance,
        "coverageFraction": coverage_fraction,
        "coverageStatus": "full" if coverage_fraction >= 0.999999 else "partial",
        "displayGeneralization": generalization,
        "sourceBundle": bundle.name,
        "sourceIndex": index_path.name,
        "sheetArchivesSha256": sheet_hashes,
    }
    collection = {
        "type": "FeatureCollection",
        "metadata": metadata,
        "features": features,
    }
    _atomic_json(output, collection)
    return metadata


__all__ = [
    "ADMIN_LEVEL_FILES",
    "BasemapError",
    "DEFAULT_LAYERS",
    "EVENT_CONTEXT_PROFILE",
    "build_admin_basemap",
    "build_basemap",
    "infer_admin_boundaries_dir",
]
