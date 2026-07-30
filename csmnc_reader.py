"""Strict reader for the two 16-line strong-motion text variants in the archive."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import re

import numpy as np


HEADER_ROWS = 16
COMPONENTS = {"EW", "NS", "UD"}
FORMAT_MARKERS = {"CSMNC", "SMOC.IEM"}
_NUMBER = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)"


class CsmncFormatError(ValueError):
    """Raised when a record cannot be used as a CSMNC acceleration trace."""


@dataclass(frozen=True)
class CsmncHeader:
    path: Path
    event_number: str
    origin_time: datetime
    source_latitude_deg: float
    source_longitude_deg: float
    source_depth_km: float
    magnitude: float
    magnitude_type: str
    station: str
    station_latitude_deg: float
    station_longitude_deg: float
    instrument_type: str
    component: str
    unit: str
    sample_count: int
    sampling_interval_s: float
    format_marker: str

    @property
    def sampling_rate_hz(self) -> float:
        return 1.0 / self.sampling_interval_s

    @property
    def duration_s(self) -> float:
        return self.sample_count * self.sampling_interval_s


def _match(pattern: str, text: str, field: str, flags: int = 0) -> re.Match[str]:
    match = re.search(pattern, text, flags)
    if not match:
        raise CsmncFormatError(f"无法解析 {field}: {text.strip()}")
    return match


def _coordinate(value: str, hemisphere: str) -> float:
    number = float(value)
    return -number if hemisphere.upper() in {"S", "W"} else number


def read_header(path: str | Path) -> CsmncHeader:
    """Read and validate the fixed CSMNC header without loading samples."""
    record_path = Path(path)
    try:
        with record_path.open("r", encoding="ascii", errors="strict", newline=None) as stream:
            lines = [next(stream) for _ in range(HEADER_ROWS)]
    except (OSError, UnicodeError, StopIteration) as error:
        raise CsmncFormatError(f"无法读取 16 行 CSMNC 头段: {record_path}") from error

    if not lines[5].lstrip().startswith("STATION:") or not lines[9].lstrip().startswith("COMP."):
        raise CsmncFormatError(f"不是受支持的 CSMNC 文本记录: {record_path}")
    format_marker = lines[14].strip().upper()
    if format_marker not in FORMAT_MARKERS:
        raise CsmncFormatError(
            f"不支持的强震文本格式标记 {format_marker or '<空>'}: {record_path}"
        )

    origin = _match(
        r"(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2}:\d{2}(?:\.\d+)?)",
        lines[1],
        "发震时刻",
    )
    try:
        origin_time = datetime.fromisoformat(f"{origin.group(1)}T{origin.group(2)}")
    except ValueError as error:
        raise CsmncFormatError(f"发震时刻无效: {lines[1].strip()}") from error

    source = _match(
        rf"EPICENTER\s+({_NUMBER})\s*([NS])\s+({_NUMBER})\s*([EW])\s+DEPTH\s+({_NUMBER})\s*KM",
        lines[3],
        "震源参数",
        re.IGNORECASE,
    )
    magnitude = _match(
        rf"MAGNITUDE\s+({_NUMBER})(?:\s*\(([^)]+)\))?",
        lines[4],
        "震级",
        re.IGNORECASE,
    )
    station = _match(
        rf"STATION:\s*(\S+)\s+({_NUMBER})\s*([NS])\s+({_NUMBER})\s*([EW])",
        lines[5],
        "台站参数",
        re.IGNORECASE,
    )
    instrument = _match(
        r"INSTRUMENT\s+TYPE:\s*(\S+)", lines[7], "仪器类型", re.IGNORECASE
    )
    component = _match(
        r"COMP\.\s*(EW|NS|UD)\b", lines[9], "分量", re.IGNORECASE
    ).group(1).upper()
    unit = _match(r"UNIT:\s*([^\s]+)", lines[10], "单位", re.IGNORECASE).group(1).upper()
    samples = _match(
        rf"NO\.\s*OF\s*POINTS:\s*(\d+).*?INTERVALS\s+OF:\s*({_NUMBER})\s*SEC",
        lines[11],
        "样点数与采样间隔",
        re.IGNORECASE,
    )

    sample_count = int(samples.group(1))
    sampling_interval = float(samples.group(2))
    if sample_count < 2 or sampling_interval <= 0:
        raise CsmncFormatError("样点数必须至少为 2，采样间隔必须大于 0")
    sampling_rate = 1.0 / sampling_interval
    if sampling_rate <= 50:
        raise CsmncFormatError("当前 0.1–25 Hz 流程要求采样率高于 50 Hz")
    if unit not in {"CM/SEC/SEC", "CM/S/S", "GAL"}:
        raise CsmncFormatError(f"只接受 gal 等价加速度单位，实际为 {unit}")

    filename_component = re.search(
        r"[._](EW|NS|UD)(?:_RawAcc)?\.dat$", record_path.name, re.IGNORECASE
    )
    if filename_component and filename_component.group(1).upper() != component:
        raise CsmncFormatError(
            f"文件名分量 {filename_component.group(1).upper()} 与头段分量 {component} 不一致"
        )

    header = CsmncHeader(
        path=record_path,
        event_number=lines[1].split()[0],
        origin_time=origin_time,
        source_latitude_deg=_coordinate(source.group(1), source.group(2)),
        source_longitude_deg=_coordinate(source.group(3), source.group(4)),
        source_depth_km=float(source.group(5)),
        magnitude=float(magnitude.group(1)),
        magnitude_type=(magnitude.group(2) or "M").strip(),
        station=station.group(1).strip(),
        station_latitude_deg=_coordinate(station.group(2), station.group(3)),
        station_longitude_deg=_coordinate(station.group(4), station.group(5)),
        instrument_type=instrument.group(1).strip(),
        component=component,
        unit=unit,
        sample_count=sample_count,
        sampling_interval_s=sampling_interval,
        format_marker=format_marker,
    )
    if not (-90 <= header.source_latitude_deg <= 90 and -90 <= header.station_latitude_deg <= 90):
        raise CsmncFormatError("纬度超出 [-90, 90]")
    if not (-180 <= header.source_longitude_deg <= 180 and -180 <= header.station_longitude_deg <= 180):
        raise CsmncFormatError("经度超出 [-180, 180]")
    if header.source_depth_km < 0:
        raise CsmncFormatError("震源深度不能为负数")
    return header


def read_acceleration(header_or_path: CsmncHeader | str | Path) -> tuple[CsmncHeader, np.ndarray]:
    """Load one component in gal and require exactly the declared sample count."""
    header = header_or_path if isinstance(header_or_path, CsmncHeader) else read_header(header_or_path)
    try:
        with header.path.open("r", encoding="ascii", errors="strict", newline=None) as stream:
            for _ in range(HEADER_ROWS):
                next(stream)
            payload = stream.read()
    except (OSError, UnicodeError, StopIteration) as error:
        raise CsmncFormatError(f"无法读取波形数组: {header.path}") from error
    values = np.fromstring(payload, sep=" ", dtype=np.float64)
    if values.size != header.sample_count:
        raise CsmncFormatError(
            f"样点数不符: 头段 {header.sample_count}，实际 {values.size} ({header.path.name})"
        )
    if not np.isfinite(values).all():
        raise CsmncFormatError(f"波形含 NaN 或 Inf: {header.path.name}")
    return header, values


__all__ = [
    "COMPONENTS",
    "FORMAT_MARKERS",
    "CsmncFormatError",
    "CsmncHeader",
    "read_acceleration",
    "read_header",
]
