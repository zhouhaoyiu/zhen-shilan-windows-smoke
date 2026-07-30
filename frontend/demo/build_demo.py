#!/usr/bin/env python3
"""Build a reproducible K-NET demonstration of the project's real result chain."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import tempfile
import urllib.request
import warnings
import zipfile
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path

import h5py
import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
import matplotlib.patheffects as patheffects
import matplotlib.tri as mtri
import numpy as np
import pandas as pd
import pykooh
import pyrotd
import shapefile
from matplotlib.cm import ScalarMappable
from matplotlib.colors import BoundaryNorm, LinearSegmentedColormap, ListedColormap, LogNorm, Normalize
from matplotlib import font_manager
from matplotlib.font_manager import FontProperties
from matplotlib.lines import Line2D
from matplotlib.offsetbox import AnnotationBbox, HPacker, TextArea
from matplotlib.patches import Rectangle
from obspy.geodetics import gps2dist_azimuth
from scipy import integrate, signal
from scipy.ndimage import gaussian_filter
from scipy.optimize import curve_fit
from scipy.spatial import Delaunay, QhullError, cKDTree


HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parents[1]
KNET_DATA_ROOT = Path(
    os.environ.get(
        "ZSL_KNET_DATA_ROOT",
        Path.home()
        / "Documents/Codex/2026-06-11/earthquake-llm-strategy/work/knet_converted",
    )
)
MANIFEST = KNET_DATA_ROOT / "knet_full_manifest.csv"
WAVEFORMS = MANIFEST.with_name("knet_full_waveforms.hdf5")
EVENT_ID = "0010061330"
PLOT_TITLE = "2000年鸟取县西部地震"
GRID_SHAPE = (10, 10)
BASE_SEED = 20260714
COMPONENT_INDEX = {"UD": 0, "NS": 1, "EW": 2}
COMPONENT_SEEDS = {"EW": BASE_SEED, "NS": BASE_SEED + 1, "UD": BASE_SEED + 2}
SEED_LABEL = " · ".join(f"{component}={seed}" for component, seed in COMPONENT_SEEDS.items())
RANDOM_PHASE_SEED_STRATEGY = (
    "SHA-256(component base seed | longitude/latitude rounded to 4 decimals), "
    "first 32 bits used as the per-target NumPy RandomState seed"
)
MAX_RETAINED_WAVEFORMS = 4
PSA_PERIODS_S = np.asarray([0.3, 1.0, 3.0])
PSA_FIELDS = ["PSA03", "PSA10", "PSA30"]
PSA_COLORBAR_SCHEMA = "event-adaptive-log-q01-q99-v1"
GROUND_MOTION_FIELDS = ["PGA", "PGV", *PSA_FIELDS, "intensity"]
FULL_EXTENT_MAP_METRICS = frozenset(GROUND_MOTION_FIELDS)
DISTANCE_BIN_LOG10_STEP = 0.1
DISTANCE_BIN_LOG10_EDGES = np.round(
    np.arange(0.0, 3.0 + DISTANCE_BIN_LOG10_STEP, DISTANCE_BIN_LOG10_STEP),
    10,
)
DISTANCE_BIN_EDGES_KM = 10 ** DISTANCE_BIN_LOG10_EDGES
DISTANCE_BIN_CENTERS_KM = 10 ** (
    (DISTANCE_BIN_LOG10_EDGES[:-1] + DISTANCE_BIN_LOG10_EDGES[1:]) / 2
)
MINIMUM_DISTANCE_BIN_COUNT = 3
DISTANCE_SOURCE_LOG10_OFFSETS = {"observed": -0.012, "inferred": 0.012}
STANDARD_GRAVITY_M_S2 = 9.80665
STANDARD_GRAVITY_GAL = STANDARD_GRAVITY_M_S2 * 100
MAXIMUM_MEAN_SAMPLE_DISTANCE_KM: float | None = None
RESULT_DIR = HERE / "project_result"
PACKAGE = HERE / f"KNET_{EVENT_ID}_project_result.zip"
DEMO_JSON = HERE / "knet-demo.json"
BOUNDARY_JSON = HERE / "japan-prefectures.geojson"

NE_URL = (
    "https://naciscdn.org/naturalearth/10m/cultural/"
    "ne_10m_admin_1_states_provinces.zip"
)
NE_PAGE = (
    "https://www.naturalearthdata.com/downloads/10m-cultural-vectors/"
    "10m-admin-1-states-provinces/"
)
NE_VERSION = "5.1.1"
NE_SHA256 = "efc59726337323058f9446210adc96673179cd344e053666ee3d28cb58ba2b05"
STRONG_MOTION_COLORS = [
    "#DEE5FF", "#AEDAFF", "#8DF4FF", "#7CFFBB", "#D3FF30", "#FFD700",
    "#FF9D00", "#FF1800", "#CD0000", "#820000", "#740000",
]
STRONG_MOTION_CLASS_LABELS = ["1", "2", "3", "4", "5", "6", "7", "8", "9", "10", "10+"]
ACCELERATION_TABLE_LABELS = [
    "<2.57", "2.57", "5.28", "10.8", "22.2", "45.6",
    "93.6", "194", "401", "830", "1720", ">1720",
]
VELOCITY_TABLE_LABELS = [
    "<0.177", "0.177", "0.381", "0.819", "1.76", "3.8",
    "8.17", "17.6", "37.8", "81.4", "175", ">175",
]
MAP_MARKER_EDGE_WIDTH = 0.90


def first_available_font(names: list[str], default: str) -> str:
    for name in names:
        try:
            font_manager.findfont(name, fallback_to_default=False)
            return name
        except ValueError:
            continue
    return default


CHINESE_FONT_NAME = first_available_font(
    ["SimSun", "Songti SC", "STSong", "Noto Serif CJK SC", "Arial Unicode MS"],
    "DejaVu Serif",
)
CHINESE_BOLD_FONT_NAME = first_available_font(
    ["Songti SC", "SimHei", "STHeiti", "Noto Serif CJK SC", "SimSun"],
    CHINESE_FONT_NAME,
)
LATIN_FONT_NAME = first_available_font(
    ["Times New Roman", "Times", "Nimbus Roman"],
    "DejaVu Serif",
)
CHINESE_FONT = FontProperties(family=CHINESE_FONT_NAME)
CHINESE_BOLD_FONT = FontProperties(family=CHINESE_BOLD_FONT_NAME, weight="bold")
LATIN_FONT = FontProperties(family=LATIN_FONT_NAME)
ACCELERATION_CLASS_EDGES = (
    -np.inf, 2.57, 5.28, 10.8, 22.2, 45.6, 93.6, 194.0, 401.0, 830.0, 1720.0, np.inf,
)
VELOCITY_CLASS_EDGES = (
    -np.inf, 0.177, 0.381, 0.819, 1.76, 3.8, 8.17, 17.6, 37.8, 81.4, 175.0, np.inf,
)
INTENSITY_CLASS_EDGES = (-np.inf, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0, 11.0, np.inf)
GROUND_MOTION_CLASS_EDGES = {
    "PGA": ACCELERATION_CLASS_EDGES,
    "PGV": VELOCITY_CLASS_EDGES,
    "intensity": INTENSITY_CLASS_EDGES,
}
GROUND_MOTION_MAP_INFO = {
    "PGA": {"name": "峰值加速度", "symbol": "PGA", "unit": "cm/s²", "filename": "峰值加速度空间分布"},
    "PGV": {"name": "峰值速度", "symbol": "PGV", "unit": "cm/s", "filename": "峰值速度空间分布"},
    "PSA03": {
        "name": "谱加速度(0.3s)", "symbol": "PSA 0.3 s", "unit": "cm/s²",
        "filename": "0.3秒谱加速度空间分布",
    },
    "PSA10": {
        "name": "谱加速度(1s)", "symbol": "PSA 1.0 s", "unit": "cm/s²",
        "filename": "1.0秒谱加速度空间分布",
    },
    "PSA30": {
        "name": "谱加速度(3s)", "symbol": "PSA 3.0 s", "unit": "cm/s²",
        "filename": "3.0秒谱加速度空间分布",
    },
    "intensity": {
        "name": "仪器地震烈度", "symbol": "仪器地震烈度", "unit": "",
        "filename": "仪器地震烈度空间分布",
    },
}
STRONG_MOTION_CMAP = ListedColormap(STRONG_MOTION_COLORS, name="fixed_strong_motion_classes")
STRONG_MOTION_NORM = BoundaryNorm(
    np.arange(-0.5, len(STRONG_MOTION_COLORS) + 0.5),
    ncolors=len(STRONG_MOTION_COLORS),
    clip=True,
)
STRONG_MOTION_SMOOTH_CMAP = LinearSegmentedColormap.from_list(
    "fixed_strong_motion_continuous",
    STRONG_MOTION_COLORS,
    N=512,
)
STRONG_MOTION_SMOOTH_NORM = Normalize(
    vmin=0,
    vmax=len(STRONG_MOTION_COLORS) - 1,
    clip=True,
)
OCEAN_COLOR = "#EEF6F8"
LAND_COLOR = "#F7FAF8"
BOUNDARY_COLOR = "#6F8580"
BOUNDARY_HALO_COLOR = "#FFFFFF"
LABEL_COLOR = "#4F6764"
ADMIN_LAYER_STYLES = {
    "admin-province": {"color": "#6F8580", "width": 0.70, "alpha": 0.96, "fontsize": 6.5, "priority": 0},
    "admin-city": {"color": "#91A6A1", "width": 0.45, "alpha": 0.88, "fontsize": 5.8, "priority": 1},
    "admin-county": {"color": "#B9C8C4", "width": 0.30, "alpha": 0.78, "fontsize": 5.2, "priority": 2},
}
ADMIN_LAYER_ORDER = ("admin-county", "admin-city", "admin-province")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def event_waveform_sha256(arrays: dict[str, np.ndarray]) -> str:
    digest = hashlib.sha256()
    for station in sorted(arrays):
        digest.update(station.encode("ascii"))
        digest.update(np.ascontiguousarray(arrays[station]).tobytes())
    return digest.hexdigest()


def load_event(
    event_id: str = EVENT_ID, expected_records: int | None = 34
) -> tuple[pd.DataFrame, dict[str, np.ndarray], dict[str, float]]:
    frame = pd.read_csv(MANIFEST, dtype={"event_id": "string"})
    event_id = str(event_id).zfill(10)
    event = frame[frame["event_id"].str.zfill(10).eq(event_id)].copy()
    event = event.sort_values("station_code").reset_index(drop=True)
    if expected_records is not None and len(event) != expected_records:
        raise ValueError(f"Expected {expected_records} records for {event_id}; found {len(event)}")
    if len(event) < 4:
        raise ValueError(f"Step2 fitting requires at least four stations; found {len(event)} for {event_id}")
    if event["station_code"].duplicated().any():
        raise ValueError("The event contains duplicate station codes")
    if not event[["has_UD", "has_NS", "has_EW"]].eq(1).all(axis=None):
        raise ValueError("The event contains an incomplete three-component record")

    single_value_fields = [
        "magnitude",
        "source_depth_km",
        "source_latitude_deg",
        "source_longitude_deg",
        "origin_time",
    ]
    for field in single_value_fields:
        if event[field].nunique(dropna=False) != 1:
            raise ValueError(f"Event metadata is inconsistent in {field}")

    arrays: dict[str, np.ndarray] = {}
    with h5py.File(WAVEFORMS, "r") as source:
        root = source["waveforms"]
        if root.attrs["component_order"] != "ZNE" or root.attrs["unit"] != "gal":
            raise ValueError("Unexpected HDF5 component order or unit")
        for row in event.itertuples(index=False):
            zne = source[row.hdf5_key][:].astype(np.float64)
            if zne.shape != (3, int(row.n_samples)):
                raise ValueError(f"Unexpected waveform shape for {row.station_code}: {zne.shape}")
            if float(row.sampling_rate_hz) <= 50:
                raise ValueError(
                    f"Step2/Step3 requires sampling above the 25 Hz cutoff: {row.station_code}"
                )
            if not np.isfinite(zne).all():
                raise ValueError(f"Non-finite waveform value for {row.station_code}")
            arrays[row.station_code] = zne

    first = event.iloc[0]
    metadata = {
        "magnitude": float(first["magnitude"]),
        "depth": float(first["source_depth_km"]),
        "latitude": float(first["source_latitude_deg"]),
        "longitude": float(first["source_longitude_deg"]),
        "duration": float(event["duration_s"].max()),
        "origin_time": str(first["origin_time"]),
    }
    return event, arrays, metadata


def bandpass_sos(sampling_rate: float = 100.0) -> np.ndarray:
    return signal.butter(
        4,
        [0.1 / 0.5 / sampling_rate, 25 / 0.5 / sampling_rate],
        btype="bandpass",
        analog=False,
        output="sos",
    )


def sample_count_for_duration(duration: float) -> int:
    """Convert a 100 Hz record duration to its integer sample count."""
    sample_count = int(round(float(duration) / 0.01))
    if sample_count < 2:
        raise ValueError("Duration is too short for Step2 synthesis")
    return sample_count


def target_frequency_grid(duration: float) -> np.ndarray:
    """Return 0.1–25 Hz values aligned exactly to synthesis FFT bins."""
    sample_count = sample_count_for_duration(duration)
    delta_f = 1 / (sample_count * 0.01)
    first_bin = math.ceil(0.1 / delta_f)
    last_bin = math.floor(25 / delta_f)
    frequencies = np.arange(first_bin, last_bin + 1, dtype=float) * delta_f
    if len(frequencies) < 2:
        raise ValueError("Duration does not provide a usable Step2 frequency grid")
    return frequencies


pyrotd.processes = 1


def processed_acceleration_velocity(
    zne_gal: np.ndarray, sampling_rate: float = 100.0
) -> tuple[np.ndarray, np.ndarray]:
    """Apply the Step3 preprocessing path to full-resolution ZNE arrays."""
    if sampling_rate <= 50:
        raise ValueError("Step3 requires a sampling rate above the 25 Hz cutoff")
    acceleration = zne_gal.astype(np.float64) - zne_gal.mean(axis=1, keepdims=True)
    acceleration = signal.sosfiltfilt(
        bandpass_sos(sampling_rate), acceleration / 100, axis=1
    )
    velocity = integrate.cumulative_trapezoid(
        acceleration, dx=1 / sampling_rate, axis=1
    )
    velocity = signal.sosfiltfilt(
        bandpass_sos(sampling_rate), velocity, axis=1
    )
    return acceleration, velocity


def horizontal_psa_gal(
    acceleration_m_s2: np.ndarray, sampling_rate: float = 100.0
) -> np.ndarray:
    """Return 5%-damped PSA at 0.3, 1.0 and 3.0 s in cm/s².

    The horizontal value follows the ShakeMap display convention: the larger
    of the NS and EW component responses. UD is not included.
    """
    component_psa_g = np.vstack(
        [
            pyrotd.calc_spec_accels(
                1 / sampling_rate,
                acceleration_m_s2[index] / STANDARD_GRAVITY_M_S2,
                1 / PSA_PERIODS_S,
                osc_damping=0.05,
                osc_type="psa",
            ).spec_accel
            for index in (1, 2)
        ]
    )
    psa = component_psa_g.max(axis=0) * STANDARD_GRAVITY_GAL
    if psa.shape != PSA_PERIODS_S.shape or not np.isfinite(psa).all() or np.any(psa < 0):
        raise ValueError("Invalid horizontal pseudo-spectral acceleration")
    return psa


def step3_metrics(
    zne_gal: np.ndarray, sampling_rate: float = 100.0, round_digits: int = 2
) -> dict[str, float]:
    """Compute the project's Step3 metrics and 5%-damped horizontal PSA."""
    acceleration, velocity = processed_acceleration_velocity(zne_gal, sampling_rate)

    pga_m_s2 = float(np.linalg.norm(acceleration[:, :-1], axis=0).max())
    pgv_m_s = float(np.linalg.norm(velocity, axis=0).max())
    ia = 3.17 * np.log10(pga_m_s2) + 6.59 if pga_m_s2 > 0 else -np.inf
    iv = 3.00 * np.log10(pgv_m_s) + 9.77 if pgv_m_s > 0 else -np.inf
    intensity = iv if ia >= 6 and iv >= 6 else max(1.0, (ia + iv) / 2)
    if not np.isfinite(intensity):
        intensity = 1.0
    psa = horizontal_psa_gal(acceleration, sampling_rate)
    return {
        "PGA": round(pga_m_s2 * 100, round_digits),
        "PGV": round(pgv_m_s * 100, round_digits),
        "PSA03": round(float(psa[0]), round_digits),
        "PSA10": round(float(psa[1]), round_digits),
        "PSA30": round(float(psa[2]), round_digits),
        "intensity": round(float(intensity), 1),
    }


def hypocentral_distance(row: pd.Series, metadata: dict[str, float]) -> tuple[float, float]:
    surface_m, azimuth, _ = gps2dist_azimuth(
        metadata["latitude"],
        metadata["longitude"],
        float(row["station_latitude_deg"]),
        float(row["station_longitude_deg"]),
    )
    return math.hypot(surface_m / 1000, metadata["depth"]), float(azimuth)


def station_phase_fit(
    filtered: np.ndarray,
    distance_km: float,
    sampling_rate: float = 100.0,
) -> tuple[float, float, float, float]:
    """Reproduce Step2's 2,000-step phase-velocity regression."""
    sample_count = len(filtered)
    delta_f = sampling_rate / sample_count
    frequencies = np.arange(0, sampling_rate / 2, delta_f)
    angles = np.angle(np.fft.fft(filtered))[: sample_count // 2] - np.pi
    angle_diff = np.diff(angles)
    angle_diff = np.where(angle_diff > 0, angle_diff - 2 * np.pi, angle_diff)
    angle_diff = np.append(angle_diff, angle_diff.mean())
    travel_time = (-1 / (2 * np.pi)) * angle_diff / delta_f
    velocity = distance_km / (distance_km / 6 + travel_time)

    start = int(0.1 / delta_f)
    stop = int(25 / delta_f)
    f = frequencies[start:stop]
    y = velocity[start:stop]
    if not np.isfinite(y).all():
        raise ValueError("Step2 phase-velocity regression produced a non-finite station value")
    log_f = np.log10(f)
    design = np.column_stack([np.ones(len(f)), log_f, log_f**2])

    # Algebraically identical to X.T @ (X @ theta - y) / m in Step2.
    gram = design.T @ design / len(y)
    rhs = design.T @ y / len(y)
    theta = np.zeros(3)
    for _ in range(2000):
        theta -= 0.03 * (gram @ theta - rhs)
    residual_std = float(np.sqrt(np.average((y - design @ theta) ** 2)))
    a0, a1, a2 = np.round(theta, 3)
    return round(residual_std, 2), float(a0), float(a1), float(a2)


def smoothed_fas(
    filtered: np.ndarray,
    target_frequencies: np.ndarray,
    sampling_rate: float = 100.0,
) -> np.ndarray:
    sample_count = len(filtered)
    delta_f = sampling_rate / sample_count
    source_frequencies = np.arange(0, sampling_rate / 2, delta_f)
    amplitude = (1 / sampling_rate) * np.abs(
        np.fft.fft(filtered)[: sample_count // 2]
    )
    result = np.asarray(pykooh.smooth(target_frequencies, source_frequencies, amplitude, 20))
    if result.shape != target_frequencies.shape or not np.isfinite(result).all():
        raise ValueError("Step2 Konno-Ohmachi smoothing returned invalid values")
    return result


def coefficient_model(distance: np.ndarray | float, a: float, b: float, c: float) -> np.ndarray:
    log_r = np.log10(distance)
    return a + b * log_r + c * log_r**2


def prepare_step2_models(
    event: pd.DataFrame,
    arrays: dict[str, np.ndarray],
    metadata: dict[str, float],
    target_frequencies: np.ndarray,
) -> tuple[dict[str, dict[str, np.ndarray]], dict[str, dict[str, np.ndarray]], pd.DataFrame]:
    station_rows = []
    for row in event.itertuples(index=False):
        surface_m, azimuth, _ = gps2dist_azimuth(
            metadata["latitude"], metadata["longitude"], row.station_latitude_deg, row.station_longitude_deg
        )
        station_rows.append(
            {
                "station": row.station_code,
                "lon": float(row.station_longitude_deg),
                "lat": float(row.station_latitude_deg),
                "R": round(math.hypot(surface_m / 1000, metadata["depth"]), 2),
                "azi": round(float(azimuth), 2),
                "duration": float(row.duration_s),
            }
        )
    station_table = pd.DataFrame(station_rows)

    fas: dict[str, dict[str, np.ndarray]] = {component: {} for component in COMPONENT_INDEX}
    models: dict[str, dict[str, np.ndarray]] = {}
    sampling_rates = dict(
        zip(
            event.station_code.astype(str),
            event.sampling_rate_hz.astype(float),
            strict=True,
        )
    )
    for component in ["EW", "NS", "UD"]:
        factors = []
        source_index = COMPONENT_INDEX[component]
        for station_row in station_table.itertuples(index=False):
            sampling_rate = sampling_rates[station_row.station]
            raw = arrays[station_row.station][source_index]
            filtered = signal.sosfilt(
                bandpass_sos(sampling_rate), raw - raw.mean()
            )
            std, a0, a1, a2 = station_phase_fit(
                filtered, station_row.R, sampling_rate
            )
            factors.append([station_row.R, std, a0, a1, a2])
            fas[component][station_row.station] = smoothed_fas(
                filtered, target_frequencies, sampling_rate
            )

        factor_array = np.asarray(factors)
        if not np.isfinite(factor_array).all():
            raise ValueError(f"Invalid Step2 station factors for {component}")
        distances = factor_array[:, 0]
        models[component] = {}
        for column, name in zip(range(1, 5), ["std", "a0", "a1", "a2"]):
            params, _ = curve_fit(coefficient_model, distances, factor_array[:, column])
            models[component][name] = params
    return fas, models, station_table


def station_geometry(station_table: pd.DataFrame) -> tuple[pd.DataFrame, Delaunay]:
    """Deduplicate coordinates and build the event station convex hull."""
    unique = station_table.drop_duplicates(subset=["lon", "lat"], keep="first").reset_index(drop=True)
    coordinates = unique[["lon", "lat"]].to_numpy(float)
    if len(unique) < 4 or np.linalg.matrix_rank(coordinates - coordinates.mean(axis=0)) < 2:
        raise ValueError("Station geometry requires four unique, non-collinear coordinates")
    try:
        triangulation = Delaunay(coordinates)
    except QhullError as error:
        raise ValueError("Station geometry cannot form a Delaunay triangulation") from error
    return unique, triangulation


def nearest_delaunay_stations(
    target: np.ndarray,
    station_table: pd.DataFrame,
    station_triangulation: Delaunay,
    sample_count: int = 4,
) -> tuple[list[str], np.ndarray]:
    """Return the requested nearest Delaunay natural neighbours for a target."""
    if sample_count < 1:
        raise ValueError("Delaunay sample_count must be positive")
    coordinates = station_table[["lon", "lat"]].to_numpy(float)
    matching = np.flatnonzero(np.all(np.isclose(coordinates, target, rtol=0, atol=1e-10), axis=1))
    if len(matching):
        target_row = int(matching[0])
        indptr, indices = station_triangulation.vertex_neighbor_vertices
        neighbor_rows = np.concatenate(
            ([target_row], indices[indptr[target_row] : indptr[target_row + 1]])
        )
    else:
        try:
            triangulation = Delaunay(np.vstack([target, coordinates]))
        except QhullError as error:
            raise ValueError("Target and stations cannot form a Delaunay triangulation") from error
        indptr, indices = triangulation.vertex_neighbor_vertices
        neighbor_rows = indices[indptr[0] : indptr[1]] - 1

    candidates: dict[tuple[float, float], tuple[float, str]] = {}
    for row_index in neighbor_rows:
        if row_index < 0:
            continue
        row = station_table.iloc[int(row_index)]
        coordinate = (float(row.lon), float(row.lat))
        distance_m, _, _ = gps2dist_azimuth(row.lat, row.lon, target[1], target[0])
        item = (distance_m / 1000, str(row.station))
        if coordinate not in candidates or item[0] < candidates[coordinate][0]:
            candidates[coordinate] = item
    selected = sorted(candidates.values(), key=lambda item: item[0])[:sample_count]
    return [station for _, station in selected], np.asarray([distance for distance, _ in selected])


def evaluate_target_quality(
    target: np.ndarray,
    station_table: pd.DataFrame,
    station_triangulation: Delaunay,
    maximum_mean_sample_distance_km: float | None = None,
    required_sample_count: int = 4,
) -> tuple[list[str], np.ndarray, dict]:
    """Apply the target-point station geometry gates from the QC workflow."""
    lon, lat = map(float, target)
    quality = {
        "target_longitude": lon,
        "target_latitude": lat,
        "sample_count": 0,
        "mean_sample_distance_km": None,
        "status": "skipped",
        "reason": "outside_station_convex_hull",
    }
    if station_triangulation.find_simplex(target, tol=1e-12) < 0:
        return [], np.asarray([], dtype=float), quality

    stations, distances = nearest_delaunay_stations(
        target,
        station_table,
        station_triangulation,
        sample_count=required_sample_count,
    )
    quality["sample_count"] = len(stations)
    if len(stations) < required_sample_count:
        count_label = {4: "four", 5: "five"}.get(
            required_sample_count, str(required_sample_count)
        )
        quality["reason"] = f"fewer_than_{count_label}_natural_neighbours"
        return stations, distances, quality

    mean_sample_distance_km = float(distances.mean())
    quality["mean_sample_distance_km"] = mean_sample_distance_km
    if (
        maximum_mean_sample_distance_km is not None
        and mean_sample_distance_km > maximum_mean_sample_distance_km
    ):
        quality["reason"] = "mean_sample_distance_exceeds_limit"
        return stations, distances, quality

    quality.update(status="processed", reason="accepted")
    return stations, distances, quality


def interpolate_fas(
    component_fas: dict[str, np.ndarray], stations: list[str], distances: np.ndarray
) -> np.ndarray:
    zero = np.flatnonzero(distances == 0)
    if len(zero):
        return component_fas[stations[int(zero[0])]].copy()
    weights = 1 / distances**2
    stack = np.vstack([component_fas[station] for station in stations])
    return np.average(stack, axis=0, weights=weights)


def synthesize_component(
    amplitude: np.ndarray,
    frequencies: np.ndarray,
    distance_km: float,
    model: dict[str, np.ndarray],
    random_state: np.random.RandomState,
    sample_count: int,
) -> tuple[np.ndarray, int]:
    a0 = coefficient_model(distance_km, *model["a0"])
    a1 = coefficient_model(distance_km, *model["a1"])
    a2 = coefficient_model(distance_km, *model["a2"])
    std = coefficient_model(distance_km, *model["std"])
    mean_velocity = a0 + a1 * np.log10(frequencies) + a2 * np.log10(frequencies) ** 2

    random_term = random_state.randn(len(frequencies))
    random_term -= random_term.mean()
    random_term = (random_term - random_term.min()) / (random_term.max() - random_term.min()) * 2 - 1
    velocity = mean_velocity + std * random_term
    nonpositive = int(np.count_nonzero(velocity <= 0))

    safe_velocity = velocity + np.finfo(float).eps
    integral = integrate.cumulative_trapezoid(1 / safe_velocity, frequencies, initial=0)
    with np.errstate(divide="ignore", invalid="ignore"):
        phase_velocity = frequencies / integral
    phase_velocity[0] = phase_velocity[1]
    phase = 2 * np.pi * frequencies * (distance_km / 6 - distance_km / phase_velocity)
    if not np.isfinite(phase).all():
        raise ValueError("Step2 target phase spectrum contains non-finite values")
    # Step2's amplitude samples start at 0.1 Hz. Place them at their actual FFT
    # bins instead of treating the first sample as the zero-frequency bin.
    delta_f = 1 / (sample_count * 0.01)
    frequency_bins = np.rint(frequencies / delta_f).astype(int)
    if len(np.unique(frequency_bins)) != len(frequency_bins):
        raise ValueError("Target frequencies map to duplicate FFT bins")
    spectrum = np.zeros(sample_count, dtype=np.complex128)
    spectrum[frequency_bins] = 2 * amplitude / 0.01 * np.exp(1j * phase)
    acceleration = np.real(np.fft.ifft(spectrum))
    return np.round(acceleration, 6), nonpositive


def write_dat(path: Path, values: np.ndarray) -> None:
    time = np.arange(len(values), dtype=np.float64) * 0.01
    np.savetxt(
        path,
        np.column_stack([time, values]),
        fmt=("%.2f", "%.6f"),
        header="Time Acc",
        comments="",
    )


def build_observed_rows(
    event: pd.DataFrame,
    arrays: dict[str, np.ndarray],
    metadata: dict[str, float],
    metric_round_digits: int = 2,
) -> list[dict]:
    observed = []
    for _, row in event.iterrows():
        distance, azimuth = hypocentral_distance(row, metadata)
        metrics = step3_metrics(
            arrays[row.station_code], float(row.sampling_rate_hz), metric_round_digits
        )
        observed.append(
            {
                "station": row.station_code,
                "lat": float(row.station_latitude_deg),
                "lon": float(row.station_longitude_deg),
                "R": round(distance, 1),
                "azi": round(azimuth, 2),
                **metrics,
            }
        )
    return observed


def target_coordinates(
    station_table: pd.DataFrame,
    grid_shape: tuple[int, int] | None = None,
    custom_target_points: list[dict] | None = None,
) -> list[tuple[float, float]]:
    """Return a validated row-major grid or the requested custom coordinates."""
    if grid_shape is not None and custom_target_points is not None:
        raise ValueError("Grid and custom target points cannot both be specified")
    if custom_target_points is not None:
        if not 1 <= len(custom_target_points) <= 2_500:
            raise ValueError("Custom target point count must be between 1 and 2500")
        coordinates = []
        seen = set()
        for index, point in enumerate(custom_target_points, start=1):
            if not isinstance(point, dict) or "lon" not in point or "lat" not in point:
                raise ValueError(f"Custom target point {index} must contain lon and lat")
            lon = float(point["lon"])
            lat = float(point["lat"])
            if not np.isfinite([lon, lat]).all():
                raise ValueError(f"Custom target point {index} must contain finite coordinates")
            if not -180 <= lon <= 180 or not -90 <= lat <= 90:
                raise ValueError(f"Custom target point {index} is outside valid longitude/latitude bounds")
            coordinate = (round(lon, 4), round(lat, 4))
            if coordinate in seen:
                raise ValueError(
                    f"Custom target point {index} duplicates an earlier point at 4-decimal precision"
                )
            seen.add(coordinate)
            coordinates.append(coordinate)
        return coordinates

    rows, columns = tuple(grid_shape or GRID_SHAPE)
    if (
        isinstance(rows, bool)
        or isinstance(columns, bool)
        or not isinstance(rows, (int, np.integer))
        or not isinstance(columns, (int, np.integer))
        or not 2 <= rows <= 50
        or not 2 <= columns <= 50
        or rows * columns > 2_500
    ):
        raise ValueError("Grid rows and columns must each be 2-50 and total no more than 2500")
    lon_grid = np.linspace(station_table.lon.min(), station_table.lon.max(), columns)
    lat_grid = np.linspace(station_table.lat.min(), station_table.lat.max(), rows)
    return [
        (round(float(lon), 4), round(float(lat), 4))
        for lat in lat_grid
        for lon in lon_grid
    ]


def target_coordinate_key(longitude: float, latitude: float) -> str:
    """Return the canonical four-decimal target key used by files and seeds."""
    return f"{float(longitude):.4f}_{float(latitude):.4f}"


def target_component_seed(component_base_seed: int, longitude: float, latitude: float) -> int:
    """Derive a stable 32-bit seed for one component at one target coordinate.

    This makes the random phase realization at a coordinate independent of the
    surrounding grid density and target iteration order.
    """
    payload = f"{int(component_base_seed)}|{target_coordinate_key(longitude, latitude)}".encode(
        "ascii"
    )
    return int.from_bytes(hashlib.sha256(payload).digest()[:4], "big")


def target_component_random_state(
    component_base_seed: int, longitude: float, latitude: float
) -> np.random.RandomState:
    """Return the coordinate-local legacy RandomState used by Step2 synthesis."""
    return np.random.RandomState(
        target_component_seed(component_base_seed, longitude, latitude)
    )


def run_step2_grid(
    station_table: pd.DataFrame,
    fas: dict[str, dict[str, np.ndarray]],
    models: dict[str, dict[str, np.ndarray]],
    metadata: dict[str, float],
    result_dir: Path | None = RESULT_DIR,
    component_seeds: dict[str, int] | None = None,
    maximum_mean_sample_distance_km: float | None = MAXIMUM_MEAN_SAMPLE_DISTANCE_KM,
    metric_round_digits: int = 2,
    grid_shape: tuple[int, int] | None = None,
    custom_target_points: list[dict] | None = None,
    progress_callback: Callable[[int, int, dict], None] | None = None,
    max_retained_waveforms: int | None = MAX_RETAINED_WAVEFORMS,
) -> tuple[list[dict], dict[str, dict[str, np.ndarray]], dict[str, int], list[dict]]:
    """Run Step2/Step3 for every accepted target while bounding waveform memory.

    One complete EW/NS/UD DAT set is written point-by-point to ``IF_folder``
    when ``result_dir`` is provided. ``simulated`` retains only a small set of
    representative target waveforms for the web display.
    Pass ``None`` explicitly only when a caller genuinely needs every waveform
    in memory.
    """
    target_frequencies = target_frequency_grid(metadata["duration"])
    sample_count = sample_count_for_duration(metadata["duration"])
    component_seeds = component_seeds or COMPONENT_SEEDS
    missing_seeds = set(COMPONENT_INDEX) - set(component_seeds)
    if missing_seeds:
        raise ValueError(f"Missing component seeds: {', '.join(sorted(missing_seeds))}")
    if max_retained_waveforms is not None:
        if (
            isinstance(max_retained_waveforms, bool)
            or not isinstance(max_retained_waveforms, (int, np.integer))
            or max_retained_waveforms < 4
        ):
            raise ValueError("max_retained_waveforms must be None or an integer of at least 4")
        max_retained_waveforms = int(max_retained_waveforms)
    unique_station_table, station_triangulation = station_geometry(station_table)
    inferred = []
    quality_rows = []
    simulated: dict[str, dict[str, np.ndarray]] = {}
    representative_scores: dict[str, tuple[float, str]] = {}
    nonpositive_counts = {component: 0 for component in COMPONENT_INDEX}
    if_folder = result_dir / "IF_folder" if result_dir is not None else None
    if if_folder is not None:
        if if_folder.exists():
            shutil.rmtree(if_folder)
        if_folder.mkdir(parents=True, exist_ok=True)

    grid_points = target_coordinates(station_table, grid_shape, custom_target_points)
    total_grid_points = len(grid_points)
    for point_index, (grid_point_lon, grid_point_lat) in enumerate(grid_points, start=1):
        lon = float(grid_point_lon)
        lat = float(grid_point_lat)
        target = np.asarray([lon, lat])
        stations, distances, quality = evaluate_target_quality(
            target,
            unique_station_table,
            station_triangulation,
            maximum_mean_sample_distance_km,
        )
        quality_rows.append(quality)
        if quality["status"] != "processed":
            if progress_callback is not None:
                progress_callback(point_index, total_grid_points, quality)
            continue

        surface_m, azimuth, _ = gps2dist_azimuth(
            metadata["latitude"], metadata["longitude"], lat, lon
        )
        distance_km = round(math.hypot(round(surface_m / 1000, 2), metadata["depth"]), 1)
        key = target_coordinate_key(lon, lat)
        components: dict[str, np.ndarray] = {}
        for component in ["EW", "NS", "UD"]:
            amplitude = interpolate_fas(fas[component], stations, distances)
            values, nonpositive = synthesize_component(
                amplitude,
                target_frequencies,
                distance_km,
                models[component],
                target_component_random_state(component_seeds[component], lon, lat),
                sample_count,
            )
            nonpositive_counts[component] += nonpositive
            components[component] = values
            if if_folder is not None:
                if_path = if_folder / f"{key}.{component}.dat"
                write_dat(if_path, values)

        zne = np.vstack([components["UD"], components["NS"], components["EW"]])
        metrics = step3_metrics(zne, round_digits=metric_round_digits)
        inferred.append(
            {
                "lon": lon,
                "lat": lat,
                "dis": distance_km,
                "azi": round(float(azimuth), 2),
                **metrics,
                "key": key,
            }
        )

        # Keep the targets selected by selected_waveforms (three maxima and the
        # epicentral-nearest point). Obsolete representatives become optional
        # filler and are evicted before a newly selected target is retained.
        representative_candidates = {
            "PGA": (float(metrics["PGA"]), key, True),
            "PGV": (float(metrics["PGV"]), key, True),
            "intensity": (float(metrics["intensity"]), key, True),
            "epicentral_distance": (
                (lon - float(metadata["longitude"])) ** 2
                + (lat - float(metadata["latitude"])) ** 2,
                key,
                False,
            ),
        }
        for name, (score, candidate_key, maximize) in representative_candidates.items():
            previous = representative_scores.get(name)
            if previous is None or (score > previous[0] if maximize else score < previous[0]):
                representative_scores[name] = (score, candidate_key)

        if max_retained_waveforms is None:
            simulated[key] = components
        else:
            representative_keys = {candidate_key for _, candidate_key in representative_scores.values()}
            should_retain = key in representative_keys or len(simulated) < max_retained_waveforms
            if should_retain and key not in simulated:
                if len(simulated) >= max_retained_waveforms:
                    removable_key = next(
                        (candidate_key for candidate_key in simulated if candidate_key not in representative_keys),
                        None,
                    )
                    if removable_key is None:
                        raise RuntimeError("Waveform retention invariant was violated")
                    del simulated[removable_key]
                simulated[key] = components
        if progress_callback is not None:
            progress_callback(point_index, total_grid_points, quality)
    return inferred, simulated, nonpositive_counts, quality_rows


DISTANCE_BIN_STATISTIC_COLUMNS = [
    "metric",
    "source",
    "bin_index",
    "bin_left_km",
    "bin_right_km",
    "bin_center_km",
    "n",
    "status",
    "mean",
    "std",
    "std_in_analysis_space",
    "median",
    "lower_1sigma",
    "upper_1sigma",
    "space",
]


def distance_bin_indices(distances) -> np.ndarray:
    """Assign distances to the fixed left-closed 0.1-log10 bins."""
    numeric = np.asarray(distances, dtype=float)
    indices = np.searchsorted(DISTANCE_BIN_EDGES_KM, numeric, side="right") - 1
    indices[np.isclose(numeric, DISTANCE_BIN_EDGES_KM[-1], rtol=0, atol=1e-10)] = (
        len(DISTANCE_BIN_EDGES_KM) - 2
    )
    valid = (
        np.isfinite(numeric)
        & (numeric >= DISTANCE_BIN_EDGES_KM[0])
        & (numeric <= DISTANCE_BIN_EDGES_KM[-1])
    )
    return np.where(valid, indices, -1).astype(np.int16)


def distance_bin_statistics(
    frame: pd.DataFrame,
    distance_column: str,
    metric: str,
    source: str,
) -> pd.DataFrame:
    """Compute one source/metric's fixed-bin mean, sample SD, and median."""
    if metric not in GROUND_MOTION_FIELDS:
        raise ValueError(f"Unsupported ground-motion metric: {metric}")
    missing = {distance_column, metric}.difference(frame.columns)
    if missing:
        raise ValueError(f"Missing attenuation columns: {sorted(missing)}")
    distances = frame[distance_column].to_numpy(float)
    values = frame[metric].to_numpy(float)
    bin_indices = distance_bin_indices(distances)
    log_space = metric != "intensity"
    usable = np.isfinite(values) & (bin_indices >= 0)
    if log_space:
        usable &= values > 0

    rows = []
    for bin_index in np.unique(bin_indices[usable]):
        selected = values[usable & (bin_indices == bin_index)]
        status = "ok" if len(selected) >= MINIMUM_DISTANCE_BIN_COUNT else "insufficient"
        space = "log10" if log_space else "linear"
        mean = sample_std = median = lower = upper = np.nan
        if status == "ok":
            transformed = np.log10(selected) if log_space else selected
            transformed_mean = float(np.mean(transformed))
            sample_std = float(np.std(transformed, ddof=1))
            transformed_median = float(np.median(transformed))
            if log_space:
                mean = float(10**transformed_mean)
                median = float(10**transformed_median)
                lower = float(10 ** (transformed_mean - sample_std))
                upper = float(10 ** (transformed_mean + sample_std))
            else:
                mean = transformed_mean
                median = transformed_median
                lower = transformed_mean - sample_std
                upper = transformed_mean + sample_std
        rows.append(
            {
                "metric": metric,
                "source": source,
                "bin_index": int(bin_index),
                "bin_left_km": float(DISTANCE_BIN_EDGES_KM[bin_index]),
                "bin_right_km": float(DISTANCE_BIN_EDGES_KM[bin_index + 1]),
                "bin_center_km": float(DISTANCE_BIN_CENTERS_KM[bin_index]),
                "n": int(len(selected)),
                "status": status,
                "mean": mean,
                "std": sample_std,
                "std_in_analysis_space": sample_std,
                "median": median,
                "lower_1sigma": lower,
                "upper_1sigma": upper,
                "space": space,
            }
        )
    return pd.DataFrame(rows, columns=DISTANCE_BIN_STATISTIC_COLUMNS)


def attenuation_distance_bin_statistics(
    observed: pd.DataFrame, inferred: pd.DataFrame
) -> pd.DataFrame:
    """Return all six metrics for both sources under one immutable bin scheme."""
    frames = []
    for metric in GROUND_MOTION_FIELDS:
        frames.extend(
            [
                distance_bin_statistics(observed, "R", metric, "observed"),
                distance_bin_statistics(inferred, "dis", metric, "inferred"),
            ]
        )
    return pd.concat(frames, ignore_index=True)[DISTANCE_BIN_STATISTIC_COLUMNS]


def attenuation_statistics_audit(
    observed: pd.DataFrame,
    inferred: pd.DataFrame,
    statistics: pd.DataFrame,
) -> list[dict]:
    """Count every accepted and rejected attenuation input by metric/source."""
    rows = []
    for source, frame, distance_column in [
        ("observed", observed, "R"),
        ("inferred", inferred, "dis"),
    ]:
        distances = frame[distance_column].to_numpy(float)
        for metric in GROUND_MOTION_FIELDS:
            values = frame[metric].to_numpy(float)
            finite = np.isfinite(distances) & np.isfinite(values)
            in_range = (
                finite
                & (distances >= DISTANCE_BIN_EDGES_KM[0])
                & (distances <= DISTANCE_BIN_EDGES_KM[-1])
            )
            nonpositive = (
                in_range & (values <= 0)
                if metric != "intensity"
                else np.zeros(len(frame), dtype=bool)
            )
            usable = in_range & ~nonpositive
            selected = statistics[
                statistics.metric.eq(metric) & statistics.source.eq(source)
            ]
            rows.append(
                {
                    "metric": metric,
                    "source": source,
                    "input": int(len(frame)),
                    "usable": int(usable.sum()),
                    "nonfinite": int((~finite).sum()),
                    "nonpositive": int(nonpositive.sum()),
                    "outOfRange": int((finite & ~in_range).sum()),
                    "okBins": int(selected.status.eq("ok").sum()),
                    "insufficientBins": int(selected.status.eq("insufficient").sum()),
                    "okSamples": int(selected.loc[selected.status.eq("ok"), "n"].sum()),
                    "insufficientSamples": int(
                        selected.loc[selected.status.eq("insufficient"), "n"].sum()
                    ),
                }
            )
    return rows


def attenuation_statistics_payload(statistics: pd.DataFrame, audit: list[dict]) -> dict:
    json_rows = (
        statistics[DISTANCE_BIN_STATISTIC_COLUMNS]
        .astype(object)
        .where(pd.notna(statistics[DISTANCE_BIN_STATISTIC_COLUMNS]), None)
        .to_dict(orient="records")
    )
    return {
        "schema_version": "zsl.distance-bin-statistics.v1",
        "binning": {
            "distance_min_km": float(DISTANCE_BIN_EDGES_KM[0]),
            "distance_max_km": float(DISTANCE_BIN_EDGES_KM[-1]),
            "log10_step": DISTANCE_BIN_LOG10_STEP,
            "minimum_count": MINIMUM_DISTANCE_BIN_COUNT,
            "interval": "left-closed/right-open; final bin right-closed",
        },
        "rows": json_rows,
        "audit": audit,
    }


def attenuation_series_arrays(
    statistics: pd.DataFrame, metric: str, source: str
) -> dict[str, np.ndarray]:
    """Expand sparse valid bins to the fixed global centers with NaN gaps."""
    selected = statistics[
        statistics.metric.eq(metric)
        & statistics.source.eq(source)
        & statistics.status.eq("ok")
    ]
    size = len(DISTANCE_BIN_CENTERS_KM)
    arrays = {
        name: np.full(size, np.nan, dtype=float)
        for name in ["mean", "median", "lower_1sigma", "upper_1sigma"]
    }
    for row in selected.itertuples(index=False):
        for name in arrays:
            arrays[name][int(row.bin_index)] = float(getattr(row, name))
    return {"distance_km": DISTANCE_BIN_CENTERS_KM.copy(), **arrays}


def attenuation_distance_limits(
    observed: pd.DataFrame,
    inferred: pd.DataFrame,
    statistics: pd.DataFrame | None = None,
) -> tuple[float, float]:
    """Return one padded log-distance range shared by all six event panels.

    Prefer the centers of bins that are actually drawable.  This prevents one
    isolated distant record from creating a large empty x-axis region.  Raw
    record distances remain the fallback for sparse events with no valid bin.
    """
    distances = np.asarray([], dtype=float)
    if statistics is not None and len(statistics):
        drawable = statistics[statistics.status.eq("ok")][
            ["source", "bin_center_km"]
        ].drop_duplicates()
        distances = np.asarray(
            [
                float(row.bin_center_km)
                * 10 ** DISTANCE_SOURCE_LOG10_OFFSETS[str(row.source)]
                for row in drawable.itertuples(index=False)
            ],
            dtype=float,
        )
    if not len(distances):
        distances = np.concatenate(
            [
                pd.to_numeric(observed.get("R"), errors="coerce").to_numpy(float),
                pd.to_numeric(inferred.get("dis"), errors="coerce").to_numpy(float),
            ]
        )
    distances = distances[
        np.isfinite(distances)
        & (distances >= DISTANCE_BIN_EDGES_KM[0])
        & (distances <= DISTANCE_BIN_EDGES_KM[-1])
    ]
    if not len(distances):
        return float(DISTANCE_BIN_EDGES_KM[0]), float(DISTANCE_BIN_EDGES_KM[-1])

    log_min = float(np.log10(distances.min()))
    log_max = float(np.log10(distances.max()))
    span = max(log_max - log_min, 0.18)
    padded_low = 10 ** (log_min - max(0.055, 0.10 * span))
    padded_high = 10 ** (log_max + max(0.075, 0.12 * span))

    def nice_floor(value: float) -> float:
        exponent = math.floor(math.log10(value))
        scale = 10 ** exponent
        multiplier = max(item for item in (1.0, 2.0, 5.0) if item <= value / scale)
        return multiplier * scale

    def nice_ceil(value: float) -> float:
        exponent = math.floor(math.log10(value))
        scale = 10 ** exponent
        candidates = [item for item in (1.0, 2.0, 5.0, 10.0) if item >= value / scale]
        return min(candidates) * scale

    lower = max(float(DISTANCE_BIN_EDGES_KM[0]), nice_floor(padded_low))
    upper = min(float(DISTANCE_BIN_EDGES_KM[-1]), nice_ceil(padded_high))
    if lower >= upper:
        lower = max(float(DISTANCE_BIN_EDGES_KM[0]), distances.min() / 1.25)
        upper = min(float(DISTANCE_BIN_EDGES_KM[-1]), distances.max() * 1.25)
    return float(lower), float(upper)


def strong_motion_classes(metric: str, values) -> np.ndarray:
    """Map ground motion to the fixed 1--10/10+ classes used by every event."""
    if metric not in GROUND_MOTION_CLASS_EDGES:
        raise ValueError(f"Unsupported ground-motion metric: {metric}")
    numeric = np.asarray(values, dtype=float)
    if not np.isfinite(numeric).all():
        raise ValueError(f"Non-finite {metric} value cannot be classified")
    thresholds = np.asarray(GROUND_MOTION_CLASS_EDGES[metric][1:-1])
    return np.searchsorted(thresholds, numeric, side="right").astype(np.int8)


def strong_motion_continuous_coordinates(metric: str, values) -> np.ndarray:
    """Map values onto a fixed continuous 0--10 colour coordinate.

    The ten published class thresholds remain fixed for every event.  Values
    between thresholds receive blended colours so the raster communicates a
    continuous field without inventing an event-specific colour range.
    """
    if metric not in GROUND_MOTION_CLASS_EDGES:
        raise ValueError(f"Unsupported ground-motion metric: {metric}")
    numeric = np.asarray(values, dtype=float)
    if not np.isfinite(numeric).all():
        raise ValueError(f"Non-finite {metric} value cannot be rendered")
    thresholds = np.asarray(GROUND_MOTION_CLASS_EDGES[metric][1:-1], dtype=float)
    color_coordinates = np.arange(len(STRONG_MOTION_COLORS), dtype=float)
    if metric == "intensity":
        transformed = numeric
        lower_anchor = thresholds[0] - (thresholds[1] - thresholds[0])
        transformed_anchors = np.r_[lower_anchor, thresholds]
    else:
        if (numeric <= 0).any():
            raise ValueError(f"Non-positive {metric} value cannot be rendered")
        transformed = np.log10(numeric)
        lower_anchor = thresholds[0] ** 2 / thresholds[1]
        transformed_anchors = np.log10(np.r_[lower_anchor, thresholds])
    return np.interp(
        transformed,
        transformed_anchors,
        color_coordinates,
        left=0.0,
        right=float(len(STRONG_MOTION_COLORS) - 1),
    )


def psa_color_scale(values) -> tuple[float, float, list[float]]:
    """Return an event-adaptive, readable logarithmic scale for one PSA map."""
    numeric = np.asarray(values, dtype=float)
    if not len(numeric) or not np.isfinite(numeric).all() or np.any(numeric <= 0):
        raise ValueError("PSA colour inputs must be positive and finite")

    if len(numeric) >= 20:
        lower_raw, upper_raw = np.quantile(numeric, [0.01, 0.99])
    else:
        lower_raw, upper_raw = float(numeric.min()), float(numeric.max())
    if lower_raw >= upper_raw:
        lower_raw, upper_raw = lower_raw / 2.0, upper_raw * 2.0

    def significant_scale(value: float) -> float:
        return 10.0 ** math.floor(math.log10(value))

    lower_scale = significant_scale(lower_raw * 0.98)
    upper_scale = significant_scale(upper_raw * 1.10)
    lower = round(lower_raw * 0.98 / lower_scale) * lower_scale
    upper = math.ceil(upper_raw * 1.10 / upper_scale - 1e-12) * upper_scale
    lower = max(float(lower), np.finfo(float).tiny)
    if lower >= upper:
        lower, upper = lower_raw / 2.0, upper_raw * 2.0

    clipping_limit = 0.0 if len(numeric) < 20 else max(0.02, 1.0 / len(numeric))
    while np.mean(numeric < lower) > clipping_limit:
        exponent = math.floor(math.log10(lower))
        candidates = [
            multiplier * 10.0 ** candidate_exponent
            for candidate_exponent in (exponent - 1, exponent)
            for multiplier in (1.0, 2.0, 5.0)
        ]
        lower_candidates = [value for value in candidates if value < lower * (1.0 - 1e-12)]
        if not lower_candidates:
            lower = float(numeric.min())
            break
        lower = max(lower_candidates)

    minimum_exponent = math.floor(math.log10(lower)) - 1
    maximum_exponent = math.ceil(math.log10(upper)) + 1

    def candidate_ticks(multipliers: tuple[float, ...]) -> list[float]:
        candidates = [
            multiplier * 10.0 ** exponent
            for exponent in range(minimum_exponent, maximum_exponent + 1)
            for multiplier in multipliers
        ]
        return sorted({
            float(value)
            for value in [lower, *candidates, upper]
            if lower <= value <= upper
        })

    ticks = candidate_ticks((1.0, 2.0, 5.0))
    if len(ticks) > 9:
        ticks = candidate_ticks((1.0, 5.0))
    if len(ticks) > 9:
        ticks = candidate_ticks((1.0,))
    return float(lower), float(upper), ticks


def strong_motion_bin_labels(metric: str) -> list[str]:
    """Return fixed numeric interval labels for PGA/PGV/PSA legends."""
    if metric == "intensity":
        return STRONG_MOTION_CLASS_LABELS.copy()
    if metric not in GROUND_MOTION_CLASS_EDGES:
        raise ValueError(f"Unsupported ground-motion metric: {metric}")
    thresholds = GROUND_MOTION_CLASS_EDGES[metric][1:-1]
    return [
        f"<{thresholds[0]:g}",
        *(f"{lower:g}--<{upper:g}" for lower, upper in zip(thresholds, thresholds[1:])),
        f">={thresholds[-1]:g}",
    ]


def set_plot_font() -> None:
    plt.rcParams.update(
        {
            "font.family": CHINESE_FONT_NAME,
            "axes.unicode_minus": False,
            "axes.linewidth": 0.8,
        }
    )


def geographic_tick(value: float, _position: int, axis: str) -> str:
    hemisphere = ("E" if value >= 0 else "W") if axis == "lon" else ("N" if value >= 0 else "S")
    number = f"{abs(value):.2f}".rstrip("0").rstrip(".")
    return f"{number}°{hemisphere}"


def event_display_label(plot_title: str, metadata: dict | None = None) -> str:
    """Normalize an event label to YYYY年MM月DD日…震级…地震."""
    metadata = metadata or {}
    event_name = str(plot_title).strip()
    date_prefix = ""
    iso_date = re.match(r"^(\d{4})-(\d{2})-(\d{2})", event_name)
    compact_date = re.match(r"^(\d{4})(\d{2})(\d{2})(?:\d{6})?", event_name)
    chinese_date = re.match(r"^(\d{4})年(\d{1,2})月(\d{1,2})日", event_name)
    if iso_date:
        year, month, day = iso_date.groups()
        date_prefix = f"{year}年{month}月{day}日"
        event_name = event_name[iso_date.end():].strip(" _-")
    elif compact_date:
        year, month, day = compact_date.groups()
        date_prefix = f"{year}年{month}月{day}日"
        event_name = event_name[compact_date.end():].strip(" _-")
    elif chinese_date:
        year, month, day = chinese_date.groups()
        date_prefix = f"{int(year):04d}年{int(month):02d}月{int(day):02d}日"
        event_name = event_name[chinese_date.end():].strip()
    else:
        origin = str(metadata.get("origin_time", ""))
        origin_date = re.search(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", origin)
        if origin_date:
            year, month, day = origin_date.groups()
            date_prefix = f"{int(year):04d}年{int(month):02d}月{int(day):02d}日"
            event_name = re.sub(r"^\d{4}年", "", event_name).strip()

    event_name = re.sub(r"\s*·\s*K-NET\s*推测结果\s*$", "", event_name).strip()
    knet_event = re.fullmatch(
        r"K-NET\s*事件\s*·\s*(M[A-Za-z]*)\s*(\d+(?:\.\d+)?)",
        event_name,
        flags=re.IGNORECASE,
    )
    if knet_event:
        magnitude_type, magnitude_value = knet_event.groups()
        event_name = f"K-NET {magnitude_type} {magnitude_value}级地震"

    event_name = re.sub(r"(级地震)(?:[_-].*)$", r"\1", event_name)
    if "级地震" in event_name:
        event_name = event_name[: event_name.index("级地震") + len("级地震")]
    elif re.search(r"\d+(?:\.\d+)?级", event_name):
        event_name = re.sub(r"(\d+(?:\.\d+)?)级.*$", r"\1级地震", event_name)

    if not re.search(r"\d+(?:\.\d+)?级地震$", event_name):
        magnitude = metadata.get("magnitude")
        if magnitude is not None and np.isfinite(float(magnitude)):
            event_name = re.sub(r"地震$", "", event_name).strip()
            event_name = f"{event_name}{float(magnitude):g}级地震"
    return f"{date_prefix}{event_name}"


def spatial_map_title(plot_title: str, metadata: dict, metric_name: str) -> str:
    return f"{event_display_label(plot_title, metadata)} 观测和推测融合{metric_name}分布图"


def mixed_text_box(
    text: str,
    fontsize: float,
    *,
    fontweight: str = "normal",
    color: str = "#111111",
):
    """Build one baseline-aligned Chinese/Latin text row with formal fonts."""
    runs: list[tuple[str, bool]] = []
    for character in text:
        is_chinese = (
            "\u3400" <= character <= "\u9fff"
            or character in "：，。（）《》、；！？“”‘’"
        )
        if runs and runs[-1][1] == is_chinese:
            runs[-1] = (runs[-1][0] + character, is_chinese)
        else:
            runs.append((character, is_chinese))
    children = [
        TextArea(
            value,
            textprops={
                "fontproperties": (
                    CHINESE_BOLD_FONT
                    if is_chinese and fontweight == "bold"
                    else CHINESE_FONT
                    if is_chinese
                    else LATIN_FONT
                ),
                "fontsize": fontsize,
                "fontweight": fontweight,
                "color": color,
            },
        )
        for value, is_chinese in runs
    ]
    return HPacker(children=children, align="baseline", pad=0, sep=0)


def add_mixed_figure_title(fig, text: str, y: float, fontsize: float) -> None:
    """Center mixed Chinese/Latin text with SimSun and Times New Roman runs."""
    title = mixed_text_box(text, fontsize, fontweight="bold")
    fig.add_artist(
        AnnotationBbox(
            title,
            (0.5, y),
            xycoords=fig.transFigure,
            box_alignment=(0.5, 0.5),
            frameon=False,
            pad=0,
        )
    )


def cartographic_time_label(generated_at: datetime | str | None) -> str:
    """Format a reproducible production timestamp in China Standard Time."""
    if generated_at is None:
        moment = datetime.now(timezone.utc)
    elif isinstance(generated_at, datetime):
        moment = generated_at
    else:
        moment = datetime.fromisoformat(str(generated_at).replace("Z", "+00:00"))
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    beijing_time = moment.astimezone(timezone(timedelta(hours=8)))
    return f"{beijing_time:%Y年%m月%d日 %H:%M:%S}（北京时间）"


def add_cartographic_footer(
    fig,
    generated_at: datetime | str | None,
    legend_handles: list,
    *,
    x: float = 0.715,
    bottom: float = 0.047,
    height: float = 0.123,
) -> None:
    """Align the legend, time, and unit with the three classification rows."""
    row_centers = [bottom + height * fraction for fraction in (5 / 6, 3 / 6, 1 / 6)]
    legend_font = CHINESE_FONT.copy()
    legend_font.set_size(6.1)
    legend = fig.legend(
        handles=legend_handles,
        loc="center left",
        bbox_to_anchor=(x, row_centers[0]),
        ncol=3,
        frameon=False,
        prop=legend_font,
        handletextpad=0.30,
        columnspacing=0.75,
        borderaxespad=0,
    )
    legend.set_zorder(30)
    footer_items = [
        (cartographic_time_label(generated_at), row_centers[1], 6.4),
        ("中国地震局工程力学研究所 强震动观测中心", row_centers[2], 6.2),
    ]
    for text, y, fontsize in footer_items:
        fig.add_artist(
            AnnotationBbox(
                mixed_text_box(text, fontsize, color="#354441"),
                (x, y),
                xycoords=fig.transFigure,
                box_alignment=(0.0, 0.5),
                frameon=False,
                pad=0,
            )
        )


def draw_fixed_classification_table(
    fig,
    position: tuple[float, float, float, float] = (0.17, 0.018, 0.66, 0.084),
) -> None:
    """Draw the user-specified three-row fixed strong-motion classification table."""
    axis = fig.add_axes(position)
    axis.set_xlim(0, 1)
    axis.set_ylim(0, 1)
    axis.axis("off")
    left_width = 0.16
    row_height = 1 / 3
    rows = [
        ("PGA (cm/s²)", ACCELERATION_TABLE_LABELS, None),
        ("PGV (cm/s)", VELOCITY_TABLE_LABELS, None),
        ("仪器地震烈度", STRONG_MOTION_CLASS_LABELS, STRONG_MOTION_COLORS),
    ]
    for row_index, (label, values, colors) in enumerate(rows):
        y = 1 - (row_index + 1) * row_height
        axis.add_patch(
            Rectangle(
                (0, y), left_width, row_height, facecolor="white",
                edgecolor="#111111", linewidth=0.65,
            )
        )
        axis.text(
            left_width / 2, y + row_height / 2, label,
            ha="center", va="center", fontsize=6.2, color="#111111",
            fontproperties=CHINESE_FONT if row_index == 2 else LATIN_FONT,
        )
        cell_width = (1 - left_width) / len(values)
        for index, value in enumerate(values):
            x = left_width + index * cell_width
            facecolor = colors[index] if colors else "white"
            axis.add_patch(
                Rectangle(
                    (x, y), cell_width, row_height, facecolor=facecolor,
                    edgecolor="#111111", linewidth=0.65,
                )
            )
            text_color = "white" if colors and index >= 7 else "#111111"
            axis.text(
                x + cell_width / 2, y + row_height / 2, value,
                ha="center", va="center", fontsize=5.7,
                color=text_color, fontweight="normal",
                fontproperties=LATIN_FONT,
            )


def draw_psa_colorbar(
    fig,
    metric: str,
    vmin: float,
    vmax: float,
    ticks: list[float],
    *,
    width: float,
    bottom: float = 0.145,
    height: float = 0.026,
) -> None:
    """Draw one centered rectangular, event-adaptive PSA colorbar."""
    info = GROUND_MOTION_MAP_INFO[metric]
    axis = fig.add_axes(((1.0 - width) / 2.0, bottom, width, height))
    scalar = ScalarMappable(
        norm=LogNorm(vmin=vmin, vmax=vmax, clip=True),
        cmap=STRONG_MOTION_SMOOTH_CMAP,
    )
    scalar.set_array([])
    colorbar = fig.colorbar(scalar, cax=axis, orientation="horizontal", extend="neither")
    colorbar.set_ticks(ticks)
    colorbar.ax.xaxis.set_major_formatter(
        matplotlib.ticker.FuncFormatter(lambda value, _position: f"{value:g}")
    )
    colorbar.ax.xaxis.set_minor_formatter(matplotlib.ticker.NullFormatter())
    colorbar.ax.tick_params(length=2.3, width=0.55, pad=1.8, labelsize=5.8)
    for tick_label in colorbar.ax.get_xticklabels():
        tick_label.set_fontproperties(LATIN_FONT)
    colorbar.outline.set_edgecolor("#17211F")
    colorbar.outline.set_linewidth(0.55)
    axis.text(
        1.015,
        0.5,
        f"{info['symbol']} ({info['unit']})",
        transform=axis.transAxes,
        ha="left",
        va="center",
        fontsize=6.2,
        color="#17211F",
        fontproperties=LATIN_FONT,
    )


def add_single_row_cartographic_footer(
    fig,
    generated_at: datetime | str | None,
    legend_handles: list,
    *,
    left: float,
    right: float,
    y: float = 0.082,
) -> None:
    """Align unit, legend, and production time below a continuous colorbar."""
    center = (left + right) / 2.0
    footer_items = [
        ("中国地震局工程力学研究所 强震动观测中心", left, 0.0),
        (cartographic_time_label(generated_at), right, 1.0),
    ]
    for text, x, alignment in footer_items:
        fig.add_artist(
            AnnotationBbox(
                mixed_text_box(text, 6.0, color="#354441"),
                (x, y),
                xycoords=fig.transFigure,
                box_alignment=(alignment, 0.5),
                frameon=False,
                pad=0,
            )
        )
    legend_font = CHINESE_FONT.copy()
    legend_font.set_size(5.7)
    legend = fig.legend(
        handles=legend_handles,
        loc="center",
        bbox_to_anchor=(center, y),
        ncol=3,
        frameon=False,
        prop=legend_font,
        handletextpad=0.28,
        columnspacing=0.78,
        borderaxespad=0,
    )
    legend.set_zorder(30)


def add_attenuation_footer(
    fig,
    generated_at: datetime | str | None,
    legend_handles: list,
    legend_labels: list[str],
) -> None:
    """Place the attenuation legend above a compact unit/time footer."""
    legend_font = CHINESE_FONT.copy()
    legend_font.set_size(7.3)
    legend = fig.legend(
        legend_handles,
        legend_labels,
        loc="lower center",
        bbox_to_anchor=(0.5, 0.054),
        ncol=4,
        frameon=False,
        prop=legend_font,
        handletextpad=0.45,
        columnspacing=1.2,
    )
    legend.set_zorder(30)
    footer_items = [
        ("中国地震局工程力学研究所 强震动观测中心", 0.075, 0.0),
        (cartographic_time_label(generated_at), 0.985, 1.0),
    ]
    for text, x, alignment in footer_items:
        fig.add_artist(
            AnnotationBbox(
                mixed_text_box(text, 6.4, color="#354441"),
                (x, 0.022),
                xycoords=fig.transFigure,
                box_alignment=(alignment, 0.5),
                frameon=False,
                pad=0,
            )
        )


def plot_attenuation(
    observed: pd.DataFrame,
    inferred: pd.DataFrame,
    path: Path,
    plot_title: str = PLOT_TITLE,
    seed_label: str = SEED_LABEL,
    observed_label: str = "K-NET 实测",
    metadata: dict | None = None,
    generated_at: datetime | str | None = None,
) -> dict:
    set_plot_font()
    fig, axes = plt.subplots(2, 3, figsize=(9.6, 6.3), facecolor="white")
    fig.subplots_adjust(left=0.075, right=0.985, bottom=0.17, top=0.94, wspace=0.29, hspace=0.32)
    specifications = [
        ("PGA", "PGA (cm/s²)", True),
        ("PGV", "PGV (cm/s)", True),
        ("PSA03", "PSA 0.3 s (cm/s²)", True),
        ("PSA10", "PSA 1.0 s (cm/s²)", True),
        ("PSA30", "PSA 3.0 s (cm/s²)", True),
        ("intensity", "仪器地震烈度", False),
    ]
    statistics = attenuation_distance_bin_statistics(observed, inferred)
    audit = attenuation_statistics_audit(observed, inferred, statistics)
    payload = attenuation_statistics_payload(statistics, audit)
    distance_limits = attenuation_distance_limits(observed, inferred, statistics)
    source_styles = {
        "observed": {
            "color": "#2D6577", "label": observed_label,
            "mean_marker": "o", "median_marker": "D",
        },
        "inferred": {
            "color": "#C45D3A", "label": "推测场",
            "mean_marker": "s", "median_marker": "v",
        },
    }
    errorbar_handles = {}
    median_handles = {}
    panel_labels = ["(a)", "(b)", "(c)", "(d)", "(e)", "(f)"]
    for axis, (metric, ylabel, log_y), panel_label in zip(
        axes.ravel(), specifications, panel_labels
    ):
        axis.set_facecolor("white")
        axis.set_xscale("log")
        axis.set_xlim(*distance_limits)
        if log_y:
            axis.set_yscale("log")
        for source, style in source_styles.items():
            series = attenuation_series_arrays(statistics, metric, source)
            valid = np.isfinite(series["mean"])
            if not valid.any():
                continue
            display_distance = series["distance_km"] * 10 ** DISTANCE_SOURCE_LOG10_OFFSETS[source]
            lower_error = series["mean"] - series["lower_1sigma"]
            upper_error = series["upper_1sigma"] - series["mean"]
            mean_handle = axis.errorbar(
                display_distance,
                series["mean"],
                yerr=np.vstack([lower_error, upper_error]),
                color=style["color"],
                fmt=style["mean_marker"],
                linestyle="none",
                markersize=3.0,
                markeredgewidth=0.55,
                elinewidth=0.72,
                capsize=1.8,
                capthick=0.65,
                zorder=4,
            )
            median_handle, = axis.plot(
                display_distance,
                series["median"],
                color=style["color"],
                linestyle="none",
                marker=style["median_marker"],
                markersize=2.5,
                markeredgewidth=0.45,
                zorder=5,
            )
            errorbar_handles.setdefault(source, mean_handle)
            median_handles.setdefault(source, median_handle)
        axis.text(
            0.035,
            0.055,
            panel_label,
            transform=axis.transAxes,
            ha="left",
            va="bottom",
            fontsize=8.5,
            fontproperties=LATIN_FONT,
            color="#17211F",
        )
        axis.set_xlabel("震源距 (km)", fontsize=8, fontproperties=CHINESE_FONT)
        axis.set_ylabel(
            ylabel,
            fontsize=8,
            fontproperties=CHINESE_FONT if metric == "intensity" else LATIN_FONT,
        )
        axis.grid(which="major", color="#D7DEDC", linewidth=0.42, alpha=0.74, zorder=0)
        axis.grid(which="minor", visible=False)
        axis.tick_params(
            direction="in", top=True, right=True, labelsize=7.2,
            width=0.65, length=3.2, zorder=21,
        )
        for tick_label in [
            *axis.get_xticklabels(which="both"),
            *axis.get_yticklabels(which="both"),
        ]:
            tick_label.set_fontproperties(LATIN_FONT)
            tick_label.set_fontsize(7.2)
        for spine in axis.spines.values():
            spine.set_color("#313B39")
            spine.set_linewidth(0.65)
            spine.set_zorder(20)
        axis.margins(y=0.08)
    handles = []
    labels = []
    for source in ["observed", "inferred"]:
        style = source_styles[source]
        if source not in errorbar_handles:
            errorbar_handles[source] = axes[0, 0].errorbar(
                [], [], yerr=[], color=style["color"], fmt=style["mean_marker"],
                linestyle="none",
                markersize=3.0, elinewidth=0.72, capsize=1.8,
            )
        if source not in median_handles:
            median_handles[source] = Line2D(
                [0], [0], color=style["color"], marker=style["median_marker"],
                markersize=2.5,
                linestyle="none",
            )
        handles.extend(
            [
                errorbar_handles[source],
                median_handles[source],
            ]
        )
        labels.extend(
            [
                f"{style['label']}均值与箱内离散度",
                f"{style['label']}中位数",
            ]
        )
    add_attenuation_footer(
        fig,
        generated_at,
        handles,
        labels,
    )
    add_mixed_figure_title(
        fig,
        f"{event_display_label(plot_title, metadata)}  实测与推测地震动随距离分布图",
        y=0.976,
        fontsize=11.0,
    )
    fig.savefig(path, dpi=240, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    return payload


def polygon_rings(geometry: dict):
    if geometry["type"] == "Polygon":
        yield from geometry["coordinates"][:1]
    elif geometry["type"] == "MultiPolygon":
        for polygon in geometry["coordinates"]:
            yield from polygon[:1]


def polygon_features(document: dict | None) -> list[dict]:
    return [
        feature
        for feature in (document or {}).get("features", [])
        if feature.get("geometry", {}).get("type") in {"Polygon", "MultiPolygon"}
    ]


def administrative_features(basemap: dict | None) -> list[dict]:
    return [
        feature
        for feature in polygon_features(basemap)
        if feature.get("properties", {}).get("layer") in ADMIN_LAYER_STYLES
    ]


def draw_map_foundation(axis, boundaries: dict, basemap: dict | None = None) -> None:
    axis.set_facecolor(OCEAN_COLOR)
    for feature in polygon_features(boundaries):
        for ring in polygon_rings(feature["geometry"]):
            coordinates = np.asarray(ring)
            axis.fill(coordinates[:, 0], coordinates[:, 1], facecolor=LAND_COLOR, edgecolor="none", zorder=0.2)
    for feature in administrative_features(basemap):
        if feature.get("properties", {}).get("layer") != "admin-province":
            continue
        for ring in polygon_rings(feature["geometry"]):
            coordinates = np.asarray(ring)
            axis.fill(coordinates[:, 0], coordinates[:, 1], facecolor=LAND_COLOR, edgecolor="none", zorder=0.3)


def draw_boundaries(
    axis,
    boundaries: dict,
    color: str = BOUNDARY_COLOR,
    linewidth: float = 0.52,
    zorder: float = 3.2,
) -> None:
    for feature in polygon_features(boundaries):
        for ring in polygon_rings(feature["geometry"]):
            coordinates = np.asarray(ring)
            axis.plot(
                coordinates[:, 0], coordinates[:, 1], color=BOUNDARY_HALO_COLOR,
                alpha=0.70, linewidth=linewidth + 0.65, zorder=zorder,
            )
            axis.plot(
                coordinates[:, 0], coordinates[:, 1], color=color,
                alpha=0.96, linewidth=linewidth, zorder=zorder + 0.01,
            )


def feature_name(feature: dict) -> str:
    properties = feature.get("properties") or {}
    for field in ("name_ja", "name_zh", "name_local", "name", "XZNAME"):
        value = str(properties.get(field, "")).strip()
        if value:
            return value
    return ""


def feature_label_point(feature: dict) -> tuple[float, float] | None:
    rings = list(polygon_rings(feature.get("geometry") or {}))
    if not rings:
        return None
    points = np.asarray(max(rings, key=len), dtype=float)
    if points.ndim != 2 or points.shape[1] < 2 or not np.isfinite(points[:, :2]).all():
        return None
    lon, lat = points[:, :2].mean(axis=0)
    return float(lon), float(lat)


def draw_map_reference(axis, boundaries: dict, basemap: dict | None = None) -> None:
    draw_boundaries(axis, boundaries)
    administrative = administrative_features(basemap)
    for layer in ADMIN_LAYER_ORDER:
        style = ADMIN_LAYER_STYLES[layer]
        for feature in administrative:
            if feature.get("properties", {}).get("layer") != layer:
                continue
            for ring in polygon_rings(feature["geometry"]):
                points = np.asarray(ring, dtype=float)
                axis.plot(
                    points[:, 0], points[:, 1], color=BOUNDARY_HALO_COLOR,
                    alpha=0.62, linewidth=style["width"] + 0.60, zorder=3.3,
                )
                axis.plot(
                    points[:, 0], points[:, 1], color=style["color"],
                    alpha=style["alpha"], linewidth=style["width"], zorder=3.31,
                )

    labels: list[tuple[int, str, tuple[float, float], float]] = []
    for feature in polygon_features(boundaries):
        name = feature_name(feature)
        point = feature_label_point(feature)
        if name and name != "境界线" and point:
            labels.append((0, name, point, 6.0))
    for feature in administrative:
        layer = feature.get("properties", {}).get("layer")
        style = ADMIN_LAYER_STYLES[layer]
        name = feature_name(feature)
        point = feature_label_point(feature)
        if name and name != "境界线" and point:
            labels.append((style["priority"], name, point, style["fontsize"]))

    west, east = sorted(axis.get_xlim())
    south, north = sorted(axis.get_ylim())
    # Text extents are wider than their anchor points.  Keep administrative
    # labels inside an inset frame so they cannot collide with longitude/
    # latitude tick labels or the map spines after tight bounding-box export.
    longitude_guard = 0.075 * (east - west)
    latitude_guard = 0.065 * (north - south)
    seen = set()
    visible_labels = []
    for priority, name, (lon, lat), fontsize in sorted(labels):
        if name in seen or not (
            west + longitude_guard < lon < east - longitude_guard
            and south + latitude_guard < lat < north - latitude_guard
        ):
            continue
        seen.add(name)
        visible_labels.append((priority, name, lon, lat, fontsize))
        if len(visible_labels) == 36:
            break
    for _, name, lon, lat, fontsize in visible_labels:
        axis.text(
            lon, lat, name, ha="center", va="center", fontsize=fontsize,
            color=LABEL_COLOR, alpha=0.78, zorder=3.6,
            bbox={"facecolor": LAND_COLOR, "edgecolor": "none", "alpha": 0.70, "pad": 0.15},
        )


def add_scale_bar(axis, latitude_deg: float) -> float:
    """Draw a compact, rounded-kilometre scale bar in the lower-left corner."""
    west, east = sorted(axis.get_xlim())
    south, north = sorted(axis.get_ylim())
    longitude_span = east - west
    latitude_span = north - south
    kilometres_per_degree = 111.32 * max(math.cos(math.radians(latitude_deg)), 0.05)
    target_km = longitude_span * kilometres_per_degree * 0.20
    magnitude = 10 ** math.floor(math.log10(max(target_km, 1e-6)))
    candidates = [item * magnitude for item in (1.0, 2.0, 5.0, 10.0)]
    scale_km = max(item for item in candidates if item <= target_km)
    scale_degrees = scale_km / kilometres_per_degree

    x_start = west + longitude_span * 0.065
    y_line = south + latitude_span * 0.070
    x_padding = longitude_span * 0.020
    y_padding = latitude_span * 0.020
    box_height = latitude_span * 0.090
    axis.add_patch(
        Rectangle(
            (x_start - x_padding, y_line - y_padding),
            scale_degrees + 2 * x_padding,
            box_height,
            facecolor="white",
            edgecolor="#98A6A3",
            linewidth=0.65,
            alpha=0.92,
            zorder=14,
        )
    )
    cap_height = latitude_span * 0.016
    axis.plot(
        [x_start, x_start + scale_degrees],
        [y_line, y_line],
        color="#17211F",
        linewidth=1.25,
        solid_capstyle="butt",
        zorder=15,
    )
    for longitude in (x_start, x_start + scale_degrees):
        axis.plot(
            [longitude, longitude],
            [y_line - cap_height / 2, y_line + cap_height / 2],
            color="#17211F",
            linewidth=1.05,
            zorder=15,
        )
    axis.text(
        x_start + scale_degrees / 2,
        y_line + latitude_span * 0.024,
        f"{scale_km:g} km",
        ha="center",
        va="bottom",
        fontsize=6.4,
        color="#17211F",
        fontproperties=LATIN_FONT,
        zorder=15,
    )
    return float(scale_km)


def metric_field_inputs(
    observed: pd.DataFrame, inferred: pd.DataFrame, metric: str
) -> pd.DataFrame:
    """Return observed-anchored inputs for the fused spatial field."""
    if metric not in GROUND_MOTION_MAP_INFO:
        raise ValueError(f"Unsupported ground-motion metric: {metric}")
    required = {"lon", "lat", metric}
    for label, frame in (("observed", observed), ("inferred", inferred)):
        missing = required.difference(frame.columns)
        if missing:
            raise ValueError(f"Missing {label} map columns: {sorted(missing)}")
    fused = pd.concat(
        [
            inferred[["lon", "lat", metric]].assign(source="inferred"),
            observed[["lon", "lat", metric]].assign(source="observed"),
        ],
        ignore_index=True,
    ).rename(columns={metric: "value"})
    numeric = fused[["lon", "lat", "value"]].to_numpy(float)
    if not len(fused) or not np.isfinite(numeric).all():
        raise ValueError(f"Non-finite or empty {metric} spatial-field input")
    fused[["lon", "lat", "value"]] = numeric
    # At an exact coordinate match, the observation anchors the fused surface.
    fused = fused.drop_duplicates(["lon", "lat"], keep="last").reset_index(drop=True)
    if metric not in PSA_FIELDS:
        fused["class"] = strong_motion_classes(metric, fused.value)
    return fused


def inverse_distance_surface(
    coordinates: np.ndarray,
    values: np.ndarray,
    grid_lon: np.ndarray,
    grid_lat: np.ndarray,
    neighbours: int = 12,
    power: float = 2.0,
) -> np.ndarray:
    """Bounded IDW extrapolation used only by the full-map preview mode."""
    coordinates = np.asarray(coordinates, dtype=float)
    values = np.asarray(values, dtype=float)
    latitude_scale = math.cos(math.radians(float(np.mean(coordinates[:, 1]))))
    scaled_coordinates = coordinates.copy()
    scaled_coordinates[:, 0] *= latitude_scale
    queries = np.column_stack([grid_lon.ravel() * latitude_scale, grid_lat.ravel()])
    distances, indices = cKDTree(scaled_coordinates).query(
        queries,
        k=min(max(int(neighbours), 1), len(coordinates)),
    )
    distances = np.atleast_2d(distances)
    indices = np.atleast_2d(indices)
    if distances.shape[0] == 1 and len(queries) > 1:
        distances = distances.T
        indices = indices.T
    weights = 1.0 / np.maximum(distances, 1e-9) ** power
    surface = np.sum(weights * values[indices], axis=1) / np.sum(weights, axis=1)
    return surface.reshape(grid_lon.shape)


def draw_intensity_contours(
    axis,
    grid_lon: np.ndarray,
    grid_lat: np.ndarray,
    class_surface: np.ndarray,
    surface_alpha: np.ndarray,
) -> None:
    """Overlay integer isoseismal contours of intensity 6 and above."""
    intensity_surface = np.where(
        np.isfinite(class_surface) & (surface_alpha > 0.12),
        class_surface + 1.0,
        np.nan,
    )
    finite = intensity_surface[np.isfinite(intensity_surface)]
    if finite.size == 0:
        return
    lower = max(6, int(math.ceil(float(finite.min()))))
    upper = min(10, int(math.floor(float(finite.max()))))
    if lower > upper:
        return
    levels = np.arange(lower, upper + 1, dtype=float)
    axis.contour(
        grid_lon,
        grid_lat,
        intensity_surface,
        levels=levels,
        colors="#FFFFFF",
        linewidths=1.35,
        alpha=0.82,
        zorder=4.05,
    )
    lines = axis.contour(
        grid_lon,
        grid_lat,
        intensity_surface,
        levels=levels,
        colors="#495E59",
        linewidths=0.70,
        alpha=0.92,
        zorder=4.10,
    )
    labels = axis.clabel(
        lines,
        fmt=lambda value: f"{value:g}",
        inline=True,
        inline_spacing=3,
        fontsize=6.2,
        colors="#17211F",
    )
    for label in labels:
        label.set_fontproperties(LATIN_FONT)
        label.set_path_effects(
            [patheffects.withStroke(linewidth=1.55, foreground="#FFFFFF")]
        )


def plot_metric_map(
    observed: pd.DataFrame,
    inferred: pd.DataFrame,
    metadata: dict[str, float],
    prefectures: dict,
    path: Path,
    metric: str,
    plot_title: str = PLOT_TITLE,
    observed_label: str = "K-NET 实测",
    basemap: dict | None = None,
    generated_at: datetime | str | None = None,
    fill_map_extent: bool = False,
    outside_fill_value: float | None = None,
    field_inferred: pd.DataFrame | None = None,
) -> None:
    """Render measured/inferred points and their observation-anchored fused field."""
    info = GROUND_MOTION_MAP_INFO.get(metric)
    if info is None:
        raise ValueError(f"Unsupported ground-motion metric: {metric}")
    set_plot_font()
    fused = metric_field_inputs(
        observed,
        field_inferred if field_inferred is not None else inferred,
        metric,
    )
    is_psa = metric in PSA_FIELDS
    psa_scale = None
    if is_psa:
        combined_values = np.r_[
            np.asarray(observed[metric], dtype=float),
            np.asarray(inferred[metric], dtype=float),
        ]
        psa_scale = psa_color_scale(combined_values)
        point_cmap = STRONG_MOTION_SMOOTH_CMAP
        point_norm = LogNorm(vmin=psa_scale[0], vmax=psa_scale[1], clip=True)
        inferred_colours = np.asarray(inferred[metric], dtype=float)
        observed_colours = np.asarray(observed[metric], dtype=float)
        surface_norm = point_norm
    else:
        point_cmap = STRONG_MOTION_CMAP
        point_norm = STRONG_MOTION_NORM
        inferred_colours = strong_motion_classes(metric, inferred[metric])
        observed_colours = strong_motion_classes(metric, observed[metric])
        surface_norm = STRONG_MOTION_SMOOTH_NORM
    fig, axes = plt.subplots(1, 2, figsize=(8.9, 5.30), facecolor="white")
    fig.subplots_adjust(left=0.045, right=0.985, bottom=0.215, top=0.90, wspace=0.008)
    lon_min = min(observed.lon.min(), inferred.lon.min())
    lon_max = max(observed.lon.max(), inferred.lon.max())
    lat_min = min(observed.lat.min(), inferred.lat.min())
    lat_max = max(observed.lat.max(), inferred.lat.max())
    lon_pad = max((lon_max - lon_min) * 0.06, 0.05)
    lat_pad = max((lat_max - lat_min) * 0.06, 0.05)
    map_span = max(lon_max - lon_min + 2 * lon_pad, lat_max - lat_min + 2 * lat_pad)
    lon_center = (lon_min + lon_max) / 2
    lat_center = (lat_min + lat_max) / 2
    map_west, map_east = lon_center - map_span / 2, lon_center + map_span / 2
    map_south, map_north = lat_center - map_span / 2, lat_center + map_span / 2

    for axis_index, axis in enumerate(axes):
        draw_map_foundation(axis, prefectures, basemap)
        axis.set_xlim(map_west, map_east)
        axis.set_ylim(map_south, map_north)
        axis.xaxis.set_major_locator(matplotlib.ticker.MaxNLocator(nbins=4))
        axis.yaxis.set_major_locator(matplotlib.ticker.MaxNLocator(nbins=4))
        axis.xaxis.set_major_formatter(
            matplotlib.ticker.FuncFormatter(lambda value, position: geographic_tick(value, position, "lon"))
        )
        axis.yaxis.set_major_formatter(
            matplotlib.ticker.FuncFormatter(lambda value, position: geographic_tick(value, position, "lat"))
        )
        axis.grid(False)
        axis.tick_params(
            direction="in", top=True, right=True, labeltop=True, length=4.2,
            width=0.9, pad=2.5, labelsize=7.5, zorder=51,
        )
        axis.set_aspect("equal", adjustable="box")
        axis.set_anchor("E" if axis_index == 0 else "W")
        for spine in axis.spines.values():
            spine.set_color("#17211F")
            spine.set_linewidth(1.0)
            spine.set_zorder(50)
    axes[0].tick_params(labelright=False)
    axes[1].tick_params(labelleft=False, labelright=True)
    # Apply the same font after label2 becomes visible on the right axis.
    for axis in axes:
        for tick in [*axis.xaxis.get_major_ticks(), *axis.yaxis.get_major_ticks()]:
            for tick_label in (tick.label1, tick.label2):
                tick_label.set_fontproperties(LATIN_FONT)
                tick_label.set_fontsize(7.5)

    axes[0].scatter(
        inferred.lon,
        inferred.lat,
        c=inferred_colours,
        cmap=point_cmap,
        norm=point_norm,
        s=48,
        marker="o",
        edgecolors="#486764",
        linewidths=MAP_MARKER_EDGE_WIDTH,
        alpha=0.98,
        label="推测场点",
        zorder=5,
    )
    axes[0].scatter(
        observed.lon,
        observed.lat,
        c=observed_colours,
        cmap=point_cmap,
        norm=point_norm,
        s=62,
        marker="^",
        edgecolors="#143F45",
        linewidths=MAP_MARKER_EDGE_WIDTH,
        alpha=0.98,
        label=observed_label,
        zorder=6,
    )
    axes[0].scatter(
        metadata["longitude"], metadata["latitude"], marker="*", s=205,
        facecolors="#FFD700", edgecolors="#17211F", linewidths=1.35, label="震中", zorder=20,
    )
    axes[0].text(
        0.025, 0.975, "(a)", transform=axes[0].transAxes, ha="left", va="top",
        fontsize=9.5, color="#17211F", zorder=9, fontproperties=LATIN_FONT,
    )
    map_legend_handles = [
        Line2D(
            [0], [0], marker="o", linestyle="none", markersize=5.4,
            markerfacecolor=STRONG_MOTION_COLORS[1], markeredgecolor="#486764",
            markeredgewidth=0.8, label="推测场点",
        ),
        Line2D(
            [0], [0], marker="^", linestyle="none", markersize=6.2,
            markerfacecolor=STRONG_MOTION_COLORS[3], markeredgecolor="#143F45",
            markeredgewidth=0.8, label=observed_label,
        ),
        Line2D(
            [0], [0], marker="*", linestyle="none", markersize=6.4,
            markerfacecolor="#FFD700", markeredgecolor="#17211F",
            markeredgewidth=0.9, label="震中",
        ),
    ]
    draw_map_reference(axes[0], prefectures, basemap)

    coordinates = fused[["lon", "lat"]].to_numpy(float)
    can_interpolate = len(coordinates) >= 3 and len(np.unique(coordinates, axis=0)) >= 3
    if can_interpolate:
        try:
            Delaunay(coordinates)
            triangulation = mtri.Triangulation(fused.lon, fused.lat)
            grid_lon, grid_lat = np.meshgrid(
                np.linspace(map_west, map_east, 240),
                np.linspace(map_south, map_north, 240),
            )
            point_coordinates = (
                np.log10(np.asarray(fused.value, dtype=float))
                if is_psa
                else strong_motion_continuous_coordinates(metric, fused.value)
            )
            if fill_map_extent and outside_fill_value is not None:
                interpolated = mtri.LinearTriInterpolator(
                    triangulation,
                    point_coordinates,
                )(grid_lon, grid_lat)
                raw_surface = np.asarray(interpolated.filled(np.nan), dtype=float)
                valid_surface = np.isfinite(raw_surface)
                fill_coordinate = float(
                    strong_motion_continuous_coordinates(metric, [outside_fill_value])[0]
                )
                smooth_surface = gaussian_filter(
                    np.where(valid_surface, raw_surface, fill_coordinate),
                    sigma=2.0,
                    mode="nearest",
                )
                surface_alpha = np.full_like(smooth_surface, 0.82)
            elif fill_map_extent:
                smooth_surface = gaussian_filter(
                    inverse_distance_surface(
                        coordinates,
                        point_coordinates,
                        grid_lon,
                        grid_lat,
                    ),
                    sigma=2.0,
                    mode="nearest",
                )
                surface_alpha = np.full_like(smooth_surface, 0.82)
            else:
                interpolated = mtri.LinearTriInterpolator(
                    triangulation,
                    point_coordinates,
                )(grid_lon, grid_lat)
                raw_surface = np.asarray(interpolated.filled(np.nan), dtype=float)
                valid_surface = np.isfinite(raw_surface)
                if not valid_surface.any():
                    raise ValueError("Fused interpolation produced no finite cells")

                # Smooth the continuous, fixed-scale colour coordinate and its
                # validity mask together.  Dividing the blurred numerator by the
                # blurred mask avoids pulling edge cells toward zero, while the
                # mask itself gives the convex-hull edge a restrained fade.
                sigma_cells = 3.2
                support = gaussian_filter(
                    valid_surface.astype(float),
                    sigma=sigma_cells,
                    mode="constant",
                    cval=0.0,
                )
                weighted = gaussian_filter(
                    np.where(valid_surface, raw_surface, 0.0),
                    sigma=sigma_cells,
                    mode="constant",
                    cval=0.0,
                )
                smooth_surface = np.divide(
                    weighted,
                    support,
                    out=np.full_like(weighted, np.nan),
                    where=support > 0.018,
                )
                surface_alpha = 0.82 * np.power(np.clip(support / 0.92, 0, 1), 0.72)
            rendered_surface = np.power(10.0, smooth_surface) if is_psa else smooth_surface
            axes[1].imshow(
                rendered_surface,
                extent=(map_west, map_east, map_south, map_north),
                origin="lower",
                cmap=STRONG_MOTION_SMOOTH_CMAP,
                norm=surface_norm,
                interpolation="bicubic",
                alpha=surface_alpha,
                zorder=2,
            )
            if metric == "intensity":
                draw_intensity_contours(
                    axes[1],
                    grid_lon,
                    grid_lat,
                    smooth_surface,
                    surface_alpha,
                )
        except (QhullError, RuntimeError, ValueError):
            can_interpolate = False
    # The right panel is the field product itself.  Measured and inferred
    # markers remain exclusively in panel (a), where their symbols can be
    # compared without obscuring the raster.
    axes[1].scatter(
        metadata["longitude"], metadata["latitude"], marker="*", s=205,
        facecolors="#FFD700", edgecolors="#17211F", linewidths=1.35, zorder=20,
    )
    draw_map_reference(axes[1], prefectures, basemap)
    add_scale_bar(axes[1], lat_center)
    axes[1].text(
        0.025, 0.975, "(b)", transform=axes[1].transAxes, ha="left", va="top",
        fontsize=9.5, color="#17211F", zorder=9, fontproperties=LATIN_FONT,
    )

    add_mixed_figure_title(
        fig,
        spatial_map_title(plot_title, metadata, info["name"]),
        y=0.966,
        fontsize=10.8,
    )
    fig.canvas.draw()
    information_x = 0.715
    table_bottom = 0.047
    table_height = 0.123
    left_map_edge = axes[0].get_position().x0
    footer_width = information_x - left_map_edge - 0.024
    if is_psa:
        draw_psa_colorbar(
            fig,
            metric,
            *psa_scale,
            width=footer_width,
        )
        add_single_row_cartographic_footer(
            fig,
            generated_at,
            map_legend_handles,
            left=axes[0].get_position().x0,
            right=axes[1].get_position().x1,
        )
    else:
        draw_fixed_classification_table(
            fig,
            position=(
                left_map_edge,
                table_bottom,
                footer_width,
                table_height,
            ),
        )
        add_cartographic_footer(
            fig,
            generated_at,
            map_legend_handles,
            x=information_x,
            bottom=table_bottom,
            height=table_height,
        )
    fig.savefig(path, dpi=240, bbox_inches="tight", facecolor="white")
    plt.close(fig)


def plot_intensity_map(
    observed: pd.DataFrame,
    inferred: pd.DataFrame,
    metadata: dict[str, float],
    prefectures: dict,
    path: Path,
    plot_title: str = PLOT_TITLE,
    observed_label: str = "K-NET 实测",
    basemap: dict | None = None,
    generated_at: datetime | str | None = None,
) -> None:
    """Backward-compatible wrapper for the original intensity-only renderer."""
    plot_metric_map(
        observed,
        inferred,
        metadata,
        prefectures,
        path,
        "intensity",
        plot_title,
        observed_label,
        basemap,
        generated_at,
        fill_map_extent=True,
    )


def plot_quality_control_map(
    observed: pd.DataFrame,
    quality: pd.DataFrame,
    metadata: dict[str, float],
    prefectures: dict,
    path: Path,
    plot_title: str = PLOT_TITLE,
    observed_label: str = "K-NET 实测",
    basemap: dict | None = None,
) -> None:
    """Render the target-grid acceptance mask separately from result maps."""
    set_plot_font()
    fig, axis = plt.subplots(figsize=(6.25, 5.3), facecolor="white")
    fig.subplots_adjust(left=0.105, right=0.94, bottom=0.065, top=0.90)
    draw_map_foundation(axis, prefectures, basemap)
    accepted = quality[quality.status.eq("processed")]
    skipped = quality[quality.status.eq("skipped")]
    axis.scatter(
        skipped.target_longitude,
        skipped.target_latitude,
        marker="x",
        s=23,
        color="#AA594B",
        linewidths=0.78,
        alpha=0.86,
        label=f"跳过 ({len(skipped)})",
        zorder=5,
    )
    axis.scatter(
        accepted.target_longitude,
        accepted.target_latitude,
        marker="o",
        s=27,
        facecolors="#86C8B5",
        edgecolors="#3E6F66",
        linewidths=0.78,
        alpha=0.90,
        label=f"通过 ({len(accepted)})",
        zorder=5,
    )
    axis.scatter(
        observed.lon,
        observed.lat,
        marker="^",
        s=40,
        facecolors="#6FA4B0",
        edgecolors="#254F58",
        linewidths=0.78,
        alpha=0.86,
        label=f"{observed_label} ({len(observed)})",
        zorder=6,
    )
    axis.scatter(
        metadata["longitude"],
        metadata["latitude"],
        marker="*",
        s=190,
        facecolors="#FFD700",
        edgecolors="#17211F",
        linewidths=1.3,
        label="震中",
        zorder=7,
    )
    lons = np.concatenate([observed.lon.to_numpy(), quality.target_longitude.to_numpy()])
    lats = np.concatenate([observed.lat.to_numpy(), quality.target_latitude.to_numpy()])
    lon_pad = max((lons.max() - lons.min()) * 0.06, 0.05)
    lat_pad = max((lats.max() - lats.min()) * 0.06, 0.05)
    map_span = max(lons.max() - lons.min() + 2 * lon_pad, lats.max() - lats.min() + 2 * lat_pad)
    lon_center = (lons.min() + lons.max()) / 2
    lat_center = (lats.min() + lats.max()) / 2
    axis.set_xlim(lon_center - map_span / 2, lon_center + map_span / 2)
    axis.set_ylim(lat_center - map_span / 2, lat_center + map_span / 2)
    axis.xaxis.set_major_locator(matplotlib.ticker.MaxNLocator(nbins=5))
    axis.yaxis.set_major_locator(matplotlib.ticker.MaxNLocator(nbins=5))
    axis.xaxis.set_major_formatter(
        matplotlib.ticker.FuncFormatter(lambda value, position: geographic_tick(value, position, "lon"))
    )
    axis.yaxis.set_major_formatter(
        matplotlib.ticker.FuncFormatter(lambda value, position: geographic_tick(value, position, "lat"))
    )
    axis.set_aspect("equal", adjustable="box")
    axis.grid(False)
    axis.tick_params(
        direction="in", top=True, right=True, labeltop=True, labelright=True,
        labelsize=7.5, width=0.8, length=3.8, pad=2.5, zorder=51,
    )
    for tick_label in [
        *axis.get_xticklabels(which="both"),
        *axis.get_yticklabels(which="both"),
    ]:
        tick_label.set_fontproperties(LATIN_FONT)
        tick_label.set_fontsize(7.5)
    for spine in axis.spines.values():
        spine.set_color("#17211F")
        spine.set_linewidth(0.85)
        spine.set_zorder(50)
    legend_font = CHINESE_FONT.copy()
    legend_font.set_size(7.5)
    axis.legend(loc="upper right", frameon=True, prop=legend_font, borderpad=0.35)
    draw_map_reference(axis, prefectures, basemap)
    add_mixed_figure_title(
        fig,
        f"{event_display_label(plot_title, metadata)}  推测目标点质量控制图",
        y=0.966,
        fontsize=10.8,
    )
    fig.savefig(path, dpi=240, bbox_inches="tight", facecolor="white")
    plt.close(fig)


def summary_entry(frame: pd.DataFrame, metric: str, distance_column: str) -> dict:
    row = frame.loc[frame[metric].idxmax()]
    return {
        "value": float(row[metric]),
        "lon": float(row.lon),
        "lat": float(row.lat),
        "distanceKm": float(row[distance_column]),
        "station": str(row.station) if "station" in row and pd.notna(row.station) else None,
    }


def selected_waveforms(
    observed: pd.DataFrame,
    inferred: pd.DataFrame,
    source_arrays: dict[str, np.ndarray],
    simulated: dict[str, dict[str, np.ndarray]],
    metadata: dict[str, float],
    max_inferred: int | None = None,
    max_samples: int | None = None,
) -> list[dict]:
    def display_components(components: dict[str, np.ndarray]) -> tuple[dict[str, list[float]], float, int]:
        sample_count = len(next(iter(components.values())))
        stride = max(1, math.ceil(sample_count / max_samples)) if max_samples else 1
        return (
            {component: values[::stride].tolist() for component, values in components.items()},
            100 / stride,
            stride,
        )

    waveforms = []
    observed_station = str(observed.loc[observed.PGA.idxmax(), "station"])
    source_row = observed[observed.station.eq(observed_station)].iloc[0]
    zne = source_arrays[observed_station]
    observed_components, observed_rate, observed_stride = display_components(
        {"EW": zne[2], "NS": zne[1], "UD": zne[0]}
    )
    waveforms.append(
        {
            "key": f"observed-{observed_station}",
            "label": f"实测 · {observed_station}",
            "station": observed_station,
            "kind": "observed",
            "lat": float(source_row.lat),
            "lon": float(source_row.lon),
            "samplingRate": observed_rate,
            "sourceSamplingRate": 100,
            "displayStride": observed_stride,
            "unit": "gal",
            "components": observed_components,
        }
    )

    candidate_indices = [
        inferred.PGA.idxmax(),
        inferred.PGV.idxmax(),
        inferred.intensity.idxmax(),
        ((inferred.lon - metadata["longitude"]) ** 2 + (inferred.lat - metadata["latitude"]) ** 2).idxmin(),
    ]
    unique_indices = []
    seen_keys = set()
    for index in candidate_indices:
        key = str(inferred.loc[index, "key"])
        if key in simulated and key not in seen_keys:
            unique_indices.append(index)
            seen_keys.add(key)
    if max_inferred is not None:
        unique_indices = unique_indices[:max_inferred]
    for index in unique_indices:
        row = inferred.loc[index]
        key = str(row.key)
        components = simulated.get(key)
        if components is None or any(component not in components for component in ["EW", "NS", "UD"]):
            continue
        display_values, display_rate, display_stride = display_components(
            {component: components[component] for component in ["EW", "NS", "UD"]}
        )
        waveforms.append(
            {
                "key": key,
                "label": f"推测 · {float(row.lon):.4f}°E, {float(row.lat):.4f}°N",
                "kind": "inferred",
                "lat": float(row.lat),
                "lon": float(row.lon),
                "samplingRate": display_rate,
                "sourceSamplingRate": 100,
                "displayStride": display_stride,
                "unit": "gal",
                "components": display_values,
            }
        )
    return waveforms


def write_result_files(
    event: pd.DataFrame,
    observed: pd.DataFrame,
    inferred: pd.DataFrame,
    station_table: pd.DataFrame,
    metadata: dict[str, float],
    prefectures: dict,
    nonpositive_counts: dict[str, int],
    source_arrays: dict[str, np.ndarray],
    quality: pd.DataFrame,
) -> tuple[list[dict], dict]:
    generated_at = datetime.now(timezone.utc).isoformat()
    config = {
        "mag": metadata["magnitude"],
        "num_sta": len(observed),
        "e_lat": metadata["latitude"],
        "e_lon": metadata["longitude"],
        "depth_hy": metadata["depth"],
        "duration": metadata["duration"],
        "lon_min": float(station_table.lon.min()),
        "lon_max": float(station_table.lon.max()),
        "lat_min": float(station_table.lat.min()),
        "lat_max": float(station_table.lat.max()),
        "number_horizontal": GRID_SHAPE[1],
        "number_vertical": GRID_SHAPE[0],
    }
    (RESULT_DIR / "config.txt").write_text(
        "\n".join(f"{key}={value}" for key, value in config.items()) + "\n", encoding="utf-8"
    )
    station_table.rename(columns={"R": "hyp_dis"})[["station", "lon", "lat", "hyp_dis", "duration"]].to_csv(
        RESULT_DIR / "points.csv", index=False
    )
    observed_path = RESULT_DIR / f"实测地震动_{PLOT_TITLE}.csv"
    inferred_path = RESULT_DIR / f"推测地震动_{PLOT_TITLE}.csv"
    quality_path = RESULT_DIR / "target_point_quality_control.csv"
    observed.to_csv(observed_path, index=False)
    inferred.drop(columns="key").to_csv(inferred_path, index=False)
    quality.to_csv(quality_path, index=False)

    attenuation_path = RESULT_DIR / f"推测与实测地震动随距离分布_{PLOT_TITLE}.png"
    attenuation_statistics_path = RESULT_DIR / "distance_bin_statistics.csv"
    quality_map_path = RESULT_DIR / f"推测目标点质量控制_{PLOT_TITLE}.png"
    attenuation_payload = plot_attenuation(
        observed,
        inferred,
        attenuation_path,
        metadata=metadata,
        generated_at=generated_at,
    )
    pd.DataFrame(
        attenuation_payload["rows"], columns=DISTANCE_BIN_STATISTIC_COLUMNS
    ).to_csv(attenuation_statistics_path, index=False)
    plot_quality_control_map(observed, quality, metadata, prefectures, quality_map_path)
    artifacts = [
        {
            "label": "实测与推测地震动随距离分布",
            "filename": attenuation_path.name,
            "url": f"./demo/project_result/{attenuation_path.name}",
        }
    ]
    for metric in GROUND_MOTION_FIELDS:
        info = GROUND_MOTION_MAP_INFO[metric]
        map_path = RESULT_DIR / f"{info['filename']}_{PLOT_TITLE}.png"
        plot_metric_map(
            observed,
            inferred,
            metadata,
            prefectures,
            map_path,
            metric,
            generated_at=generated_at,
            fill_map_extent=metric in FULL_EXTENT_MAP_METRICS,
        )
        artifacts.append(
            {
                "label": f"{info['name']}空间分布",
                "filename": map_path.name,
                "url": f"./demo/project_result/{map_path.name}",
                "kind": "spatial-map",
                "metric": metric,
            }
        )
    artifacts.append(
        {
            "label": "推测目标点质量控制",
            "filename": quality_map_path.name,
            "url": f"./demo/project_result/{quality_map_path.name}",
        }
    )

    outside_hull_points = int(quality.reason.eq("outside_station_convex_hull").sum())
    insufficient_points = int(quality.reason.eq("fewer_than_four_natural_neighbours").sum())
    distance_filtered_points = int(quality.reason.eq("mean_sample_distance_exceeds_limit").sum())
    skipped_points = int(quality.status.eq("skipped").sum())

    run_metadata = {
        "eventId": EVENT_ID,
        "title": PLOT_TITLE,
        "generatedAt": generated_at,
        "generatedBy": str(HERE / "build_demo.py"),
        "source": {
            "manifest": str(MANIFEST),
            "manifestSha256": sha256(MANIFEST),
            "waveforms": str(WAVEFORMS),
            "eventWaveformSha256": event_waveform_sha256(source_arrays),
            "componentOrder": "ZNE",
            "unit": "gal",
            "eventRows": len(event),
        },
        "method": {
            "projectFiles": ["Step2.1.py", "Step2.2.py", "Step2.3.py", "Step3.1.py", "Step3.2.py"],
            "projectFileSha256": {
                name: sha256(PROJECT_ROOT / name)
                for name in ["Step2.1.py", "Step2.2.py", "Step2.3.py", "Step3.1.py", "Step3.2.py"]
            },
            "adapterSha256": sha256(HERE / "build_demo.py"),
            "grid": list(GRID_SHAPE),
            "sampleStations": "Four closest Delaunay natural neighbours; inverse-distance-squared FAS interpolation",
            "targetPointQualityControl": {
                "insideOrOnStationConvexHull": True,
                "requiredNaturalNeighbours": 4,
                "maximumMeanSampleDistanceKm": MAXIMUM_MEAN_SAMPLE_DISTANCE_KM,
                "csv": quality_path.name,
            },
            "frequencyBandHz": [0.1, 25],
            "spectralAcceleration": {
                "periodsSeconds": PSA_PERIODS_S.tolist(),
                "dampingRatio": 0.05,
                "horizontalCombination": "greater of NS and EW",
                "unit": "cm/s²",
                "input": "full-resolution Step3 preprocessed acceleration",
            },
            "randomPhaseSeeds": COMPONENT_SEEDS,
            "randomPhaseSeedStrategy": RANDOM_PHASE_SEED_STRATEGY,
            "maxRetainedWaveforms": MAX_RETAINED_WAVEFORMS,
            "phaseRealization": "One deterministic realization for this bundled demonstration",
            "adapter": [
                "Reads the verified local K-NET HDF5 ZNE arrays and maps Z/N/E to UD/NS/EW",
                "Aligns stations and components by station/grid key instead of relying on unsorted directory order",
                "Writes one complete EW/NS/UD Step2 DAT set to IF_folder",
                "Uses the project's Step2 formulas with cached station spectra; fixes paired-coordinate station selection",
                "Places the 0.1-25 Hz spectrum at its physical FFT bins instead of placing 0.1 Hz at the DC bin",
                "Filters target points using the station convex hull and exactly four nearest Delaunay natural neighbours",
                "Computes 5%-damped horizontal PSA at 0.3, 1.0, and 3.0 seconds from full-resolution arrays",
                "Renders six fixed-scale spatial maps from observation-anchored measured/inferred fused fields",
            ],
            "attenuationDistanceBins": {
                "schemaVersion": attenuation_payload["schema_version"],
                **attenuation_payload["binning"],
                "audit": attenuation_payload["audit"],
                "csv": attenuation_statistics_path.name,
                "rowCount": len(attenuation_payload["rows"]),
                "statistics": (
                    "geometric mean, sample SD in log10 space, and median for PGA/PGV/PSA; "
                    "arithmetic mean, sample SD, and median in linear space for intensity"
                ),
            },
            "nonpositivePhaseVelocitySamples": nonpositive_counts,
        },
        "outputs": {
            "observedRows": len(observed),
            "inferredRows": len(inferred),
            "ifFolderFiles": len(list((RESULT_DIR / "IF_folder").glob("*.dat"))),
            "samplesPerInferredComponent": sample_count_for_duration(metadata["duration"]),
            "candidateGridPoints": len(quality),
            "processedPoints": len(inferred),
            "skippedPoints": skipped_points,
            "outsideStationConvexHullPoints": outside_hull_points,
            "insufficientNaturalNeighbourPoints": insufficient_points,
            "meanDistanceFilteredPoints": distance_filtered_points,
            "maximumMeanSampleDistanceKm": MAXIMUM_MEAN_SAMPLE_DISTANCE_KM,
            "qualityControlCsv": quality_path.name,
            "distanceBinStatistics": attenuation_statistics_path.name,
            "distanceBinStatisticsRows": len(attenuation_payload["rows"]),
            "artifacts": [artifact["filename"] for artifact in artifacts],
        },
        "interpretationBoundary": (
            "The inferred field is one reproducible random-phase realization of the project method. "
            "It is a demonstration output, not an independent validation or accuracy claim. "
            "Distance-bin spread is descriptive dispersion, not a confidence interval or accuracy validation."
        ),
    }
    (RESULT_DIR / "run_metadata.json").write_text(
        json.dumps(run_metadata, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8"
    )
    return artifacts, run_metadata


def build_project_demo(prefectures: dict) -> dict:
    event, source_arrays, metadata = load_event()
    if RESULT_DIR.exists():
        shutil.rmtree(RESULT_DIR)
    RESULT_DIR.mkdir(parents=True)

    target_frequencies = target_frequency_grid(metadata["duration"])
    fas, models, station_table = prepare_step2_models(event, source_arrays, metadata, target_frequencies)
    observed_rows = build_observed_rows(event, source_arrays, metadata)
    inferred_rows, simulated, nonpositive_counts, quality_rows = run_step2_grid(
        station_table, fas, models, metadata
    )
    observed = pd.DataFrame(observed_rows)
    inferred = pd.DataFrame(inferred_rows)
    quality = pd.DataFrame(quality_rows)
    artifacts, run_metadata = write_result_files(
        event,
        observed,
        inferred,
        station_table,
        metadata,
        prefectures,
        nonpositive_counts,
        source_arrays,
        quality,
    )

    outputs = run_metadata["outputs"]
    summary = {
        "observedStations": len(observed),
        "candidateGridPoints": len(quality),
        "inferredPoints": len(inferred),
        "skippedPoints": outputs["skippedPoints"],
        "inferredComponentFiles": len(inferred) * 3,
        "samplesPerInferredComponent": sample_count_for_duration(metadata["duration"]),
        "outsideStationConvexHullPoints": outputs["outsideStationConvexHullPoints"],
        "insufficientNaturalNeighbourPoints": outputs["insufficientNaturalNeighbourPoints"],
        "maximumMeanSampleDistanceKm": MAXIMUM_MEAN_SAMPLE_DISTANCE_KM,
        "maxObserved": {metric: summary_entry(observed, metric, "R") for metric in GROUND_MOTION_FIELDS},
        "maxInferred": {metric: summary_entry(inferred, metric, "dis") for metric in GROUND_MOTION_FIELDS},
        "randomPhaseSeeds": COMPONENT_SEEDS,
        "randomPhaseSeedStrategy": RANDOM_PHASE_SEED_STRATEGY,
        "retainedInferredWaveforms": len(simulated),
    }
    waveforms = selected_waveforms(observed, inferred, source_arrays, simulated, metadata)
    distance_bin_statistics_frame = pd.read_csv(
        RESULT_DIR / "distance_bin_statistics.csv"
    )
    distance_bin_statistics_rows = (
        distance_bin_statistics_frame.astype(object)
        .where(pd.notna(distance_bin_statistics_frame), None)
        .to_dict(orient="records")
    )
    return {
        "demoOnly": True,
        "projectResult": True,
        "title": event_display_label(PLOT_TITLE, metadata),
        "eventId": EVENT_ID,
        "originTime": metadata["origin_time"],
        "config": {
            "mag": metadata["magnitude"],
            "num_sta": len(observed),
            "e_lat": metadata["latitude"],
            "e_lon": metadata["longitude"],
            "depth_hy": metadata["depth"],
            "duration": metadata["duration"],
            "number_horizontal": GRID_SHAPE[1],
            "number_vertical": GRID_SHAPE[0],
        },
        "observed": observed.to_dict(orient="records"),
        "inferred": inferred.drop(columns="key").to_dict(orient="records"),
        "waveforms": waveforms,
        "distanceBinStatistics": distance_bin_statistics_rows,
        "artifacts": artifacts,
        "resultPackage": {
            "filename": PACKAGE.name,
            "url": f"./demo/{PACKAGE.name}",
        },
        "resultFiles": {
            "observedCsv": f"./demo/project_result/实测地震动_{PLOT_TITLE}.csv",
            "inferredCsv": f"./demo/project_result/推测地震动_{PLOT_TITLE}.csv",
            "qualityControlCsv": "./demo/project_result/target_point_quality_control.csv",
            "distanceBinStatistics": "./demo/project_result/distance_bin_statistics.csv",
            "runMetadata": "./demo/project_result/run_metadata.json",
        },
        "summary": summary,
        "integrityNote": (
            f"34 个 K-NET 实测台站对应 100 个候选网格，质控后生成 {len(inferred)} 个推测场点。"
            f"推测为固定随机相位单次实现（{SEED_LABEL.replace(' · ', '、')}）；"
            f"跳过 {outputs['outsideStationConvexHullPoints']} 个凸包外点和 "
            f"{outputs['insufficientNaturalNeighbourPoints']} 个邻站不足点。"
            "PSA 为 5% 阻尼、NS/EW 较大值；这些结果不表示独立精度验证。"
        ),
        "provenance": {
            "summary": (
                f"NIED K-NET event {EVENT_ID} · 34 observed · {len(inferred)} QC-passed inferred · "
                f"phase seeds {SEED_LABEL}"
            ),
            "event": {
                "name": "2000 Tottori-ken Seibu earthquake",
                "originTime": metadata["origin_time"],
                "magnitudeType": "Mj",
                "magnitude": metadata["magnitude"],
                "latitude": metadata["latitude"],
                "longitude": metadata["longitude"],
                "depthKm": metadata["depth"],
            },
            "sources": run_metadata["source"],
            "method": run_metadata["method"],
            "outputs": run_metadata["outputs"],
            "boundary": run_metadata["interpretationBoundary"],
            "niedKnet": "https://www.kyoshin.bosai.go.jp/",
        },
    }


def build_japan_prefectures(archive_override: Path | None = None) -> dict:
    fields_to_keep = [
        "featurecla", "scalerank", "adm1_code", "iso_3166_2", "iso_a2", "name", "name_local",
        "type_en", "region", "name_en", "name_ja", "name_zh", "name_zht", "latitude", "longitude",
        "adm0_a3", "admin", "ne_id",
    ]
    with tempfile.TemporaryDirectory() as temp_dir:
        archive = archive_override or Path(temp_dir) / "natural-earth.zip"
        if archive_override is None:
            urllib.request.urlretrieve(NE_URL, archive)
        actual_hash = sha256(archive)
        if actual_hash != NE_SHA256:
            raise ValueError(f"Natural Earth archive checksum changed: {actual_hash}")
        with zipfile.ZipFile(archive) as source:
            source.extractall(temp_dir)

        reader = shapefile.Reader(str(Path(temp_dir) / "ne_10m_admin_1_states_provinces.shp"), encoding="utf-8")
        field_names = [field[0] for field in reader.fields[1:]]
        features = []
        for shape_record in reader.iterShapeRecords():
            properties = dict(zip(field_names, shape_record.record))
            if properties["adm0_a3"] != "JPN":
                continue
            selected = {name: properties[name] for name in fields_to_keep}
            selected.update(
                {"source": "Natural Earth", "source_dataset": "ne_10m_admin_1_states_provinces", "source_version": NE_VERSION}
            )
            features.append(
                {
                    "type": "Feature",
                    "id": str(properties["adm1_code"]),
                    "properties": selected,
                    "geometry": shape_record.shape.__geo_interface__,
                }
            )
    if len(features) != 47:
        raise ValueError(f"Expected 47 Japanese prefectures; found {len(features)}")
    return {
        "type": "FeatureCollection",
        "name": "Natural Earth 1:10m Admin-1 states and provinces — Japan",
        "source": "Natural Earth",
        "sourceDataset": "ne_10m_admin_1_states_provinces",
        "sourceVersion": NE_VERSION,
        "sourcePage": NE_PAGE,
        "downloadUrl": NE_URL,
        "downloadSha256": NE_SHA256,
        "license": "Public domain; see https://www.naturalearthdata.com/about/terms-of-use/",
        "countryFilter": {"field": "adm0_a3", "value": "JPN"},
        "features": sorted(features, key=lambda feature: feature["properties"]["iso_3166_2"]),
    }


def validate(demo: dict, prefectures: dict) -> None:
    if demo["eventId"] != EVENT_ID or len(demo["observed"]) != 34:
        raise ValueError("Demo event identity or observed station count failed validation")
    if len(demo["inferred"]) != 41:
        raise ValueError("Tottori QC regression requires 41 processed inferred points")
    for collection in [demo["observed"], demo["inferred"]]:
        for row in collection:
            for field in ["lon", "lat", *GROUND_MOTION_FIELDS]:
                if not np.isfinite(row[field]):
                    raise ValueError(f"Non-finite {field}: {row}")
            if any(row[field] <= 0 for field in ["PGA", "PGV", *PSA_FIELDS]) or not 1 <= row["intensity"] <= 12:
                raise ValueError(f"Invalid ground-motion metric: {row}")
    dat_files = list((RESULT_DIR / "IF_folder").glob("*.dat"))
    if len(dat_files) != len(demo["inferred"]) * 3:
        raise ValueError(f"Unexpected inferred component file count: {len(dat_files)}")
    expected_names = {
        f"{row['lon']:.4f}_{row['lat']:.4f}.{component}.dat"
        for row in demo["inferred"]
        for component in ["EW", "NS", "UD"]
    }
    if {path.name for path in dat_files} != expected_names:
        raise ValueError("IF_folder filenames do not match QC-passed inferred coordinates")

    quality = pd.read_csv(RESULT_DIR / "target_point_quality_control.csv")
    if len(quality) != 100 or int(quality.status.eq("processed").sum()) != len(demo["inferred"]):
        raise ValueError("Target-point QC row or processed count mismatch")
    if int(quality.reason.eq("outside_station_convex_hull").sum()) != 58:
        raise ValueError("Tottori QC regression requires 58 outside-hull targets")
    if int(quality.reason.eq("fewer_than_four_natural_neighbours").sum()) != 1:
        raise ValueError("Tottori QC regression requires one insufficient-neighbour target")
    accepted_coordinates = {
        (round(float(row.target_longitude), 4), round(float(row.target_latitude), 4))
        for row in quality[quality.status.eq("processed")].itertuples(index=False)
    }
    inferred_coordinates = {(round(float(row["lon"]), 4), round(float(row["lat"]), 4)) for row in demo["inferred"]}
    if accepted_coordinates != inferred_coordinates:
        raise ValueError("QC-processed coordinates differ from inferred output coordinates")

    def validate_metrics(expected: dict[str, float], actual: dict, label: str) -> None:
        if expected["intensity"] != actual["intensity"]:
            raise ValueError(f"Intensity mismatch for {label}")
        for field in ["PGA", "PGV", *PSA_FIELDS]:
            if not np.isclose(expected[field], actual[field], atol=0.0051):
                raise ValueError(f"{field} mismatch for {label}")

    _, source_arrays, metadata = load_event()
    observed_by_station = {row["station"]: row for row in demo["observed"]}
    for station, zne in source_arrays.items():
        expected = step3_metrics(zne)
        actual = observed_by_station[station]
        validate_metrics(expected, actual, station)

    sample_count = sample_count_for_duration(float(demo["config"]["duration"]))
    for row in demo["inferred"]:
        key = f"{row['lon']:.4f}_{row['lat']:.4f}"
        components = {}
        reference_time = None
        for component in ["EW", "NS", "UD"]:
            data = np.loadtxt(RESULT_DIR / "IF_folder" / f"{key}.{component}.dat", skiprows=1)
            if data.shape != (sample_count, 2) or not np.isfinite(data).all():
                raise ValueError(f"Invalid inferred DAT shape or value: {key}.{component}")
            if not np.isclose(data[0, 0], 0) or not np.allclose(np.diff(data[:, 0]), 0.01, atol=1e-10):
                raise ValueError(f"Invalid inferred DAT time axis: {key}.{component}")
            if reference_time is None:
                reference_time = data[:, 0]
            elif not np.array_equal(reference_time, data[:, 0]):
                raise ValueError(f"Misaligned inferred component times: {key}")
            components[component] = data[:, 1]
        zne = np.vstack([components["UD"], components["NS"], components["EW"]])
        expected = step3_metrics(zne)
        validate_metrics(expected, row, key)
        surface_m, _, _ = gps2dist_azimuth(metadata["latitude"], metadata["longitude"], row["lat"], row["lon"])
        expected_distance = round(math.hypot(round(surface_m / 1000, 2), metadata["depth"]), 1)
        if expected_distance != row["dis"]:
            raise ValueError(f"Inferred hypocentral distance mismatch for {key}")

    observed_csv = pd.read_csv(RESULT_DIR / f"实测地震动_{PLOT_TITLE}.csv")
    inferred_csv = pd.read_csv(RESULT_DIR / f"推测地震动_{PLOT_TITLE}.csv")
    if len(observed_csv) != 34 or len(inferred_csv) != len(demo["inferred"]) or observed_csv.station.nunique() != 34:
        raise ValueError("Result CSV row or station uniqueness validation failed")
    distance_statistics = pd.read_csv(RESULT_DIR / "distance_bin_statistics.csv")
    statistic_fields = [
        "mean", "std", "std_in_analysis_space", "median", "lower_1sigma", "upper_1sigma"
    ]
    geometry = distance_statistics[
        ["bin_index", "bin_left_km", "bin_right_km", "bin_center_km", "n"]
    ].apply(pd.to_numeric, errors="coerce")
    statistics = distance_statistics[statistic_fields].apply(pd.to_numeric, errors="coerce")
    ok = distance_statistics.status.eq("ok")
    insufficient = distance_statistics.status.eq("insufficient")
    if (
        distance_statistics.columns.tolist() != DISTANCE_BIN_STATISTIC_COLUMNS
        or len(distance_statistics) != len(demo["distanceBinStatistics"])
        or any(set(row) != set(DISTANCE_BIN_STATISTIC_COLUMNS) for row in demo["distanceBinStatistics"])
        or not np.isfinite(geometry.to_numpy(float)).all()
        or (geometry.n < 1).any()
        or distance_statistics.duplicated(["metric", "source", "bin_index"]).any()
        or not set(distance_statistics.metric).issubset(GROUND_MOTION_FIELDS)
        or not set(distance_statistics.source).issubset({"observed", "inferred"})
        or not set(distance_statistics.status).issubset({"ok", "insufficient"})
        or (geometry.loc[ok, "n"] < MINIMUM_DISTANCE_BIN_COUNT).any()
        or not np.isfinite(statistics.loc[ok].to_numpy(float)).all()
        or (geometry.loc[insufficient, "n"] >= MINIMUM_DISTANCE_BIN_COUNT).any()
        or not statistics.loc[insufficient].isna().all().all()
    ):
        raise ValueError("Distance-bin statistics validation failed")
    if len(prefectures["features"]) != 47:
        raise ValueError("Japan prefecture count failed validation")
    spatial_artifacts = [item for item in demo["artifacts"] if item.get("kind") == "spatial-map"]
    if (
        len(demo["artifacts"]) != 8
        or [item.get("metric") for item in spatial_artifacts] != GROUND_MOTION_FIELDS
        or any(not (RESULT_DIR / item["filename"]).exists() for item in demo["artifacts"])
    ):
        raise ValueError("Generated figure validation failed")
    for artifact in demo["artifacts"]:
        image = plt.imread(RESULT_DIR / artifact["filename"])
        if min(image.shape[:2]) < 800:
            raise ValueError(f"Generated figure is unexpectedly small: {artifact['filename']}")
    for waveform in demo["waveforms"]:
        lengths = {len(values) for values in waveform["components"].values()}
        if len(lengths) != 1 or min(lengths) < 11_900:
            raise ValueError(f"Unexpected waveform length for {waveform['key']}: {lengths}")


def package_results() -> None:
    if PACKAGE.exists():
        PACKAGE.unlink()
    with zipfile.ZipFile(PACKAGE, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path in sorted(RESULT_DIR.rglob("*")):
            if path.is_file():
                archive.write(path, Path(f"KNET_{EVENT_ID}_project_result") / path.relative_to(RESULT_DIR))
    with zipfile.ZipFile(PACKAGE) as archive:
        expected_files = len([path for path in RESULT_DIR.rglob("*") if path.is_file()])
        if archive.testzip() is not None or len(archive.namelist()) != expected_files:
            raise ValueError("Result package integrity validation failed")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--natural-earth-archive", type=Path)
    parser.add_argument("--refresh-boundaries", action="store_true")
    args = parser.parse_args()

    if args.refresh_boundaries or args.natural_earth_archive or not BOUNDARY_JSON.exists():
        prefectures = build_japan_prefectures(args.natural_earth_archive)
        BOUNDARY_JSON.write_text(
            json.dumps(prefectures, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
    else:
        prefectures = json.loads(BOUNDARY_JSON.read_text(encoding="utf-8"))

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", category=RuntimeWarning)
        demo = build_project_demo(prefectures)
    validate(demo, prefectures)
    package_results()
    demo["resultPackage"]["bytes"] = PACKAGE.stat().st_size
    demo["resultPackage"]["sha256"] = sha256(PACKAGE)
    DEMO_JSON.write_text(
        json.dumps(demo, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "eventId": EVENT_ID,
                "observed": len(demo["observed"]),
                "inferred": len(demo["inferred"]),
                "ifFolderFiles": len(list((RESULT_DIR / "IF_folder").glob("*.dat"))),
                "figures": [item["filename"] for item in demo["artifacts"]],
                "packageBytes": PACKAGE.stat().st_size,
                "maxInferred": demo["summary"]["maxInferred"],
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
