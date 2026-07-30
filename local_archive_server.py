#!/usr/bin/env python3
"""Serve the result website and process browser-imported EI/HN event folders."""

from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import hashlib
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import os
from pathlib import Path, PurePosixPath
import subprocess
import sys
import threading
import time
import traceback
from urllib.parse import parse_qs, urlsplit
import uuid
import webbrowser

from process_events_windows import (
    ADMIN_LEVEL_FILES,
    DEFAULT_OUTPUT_ROOT,
    FRONTEND_ROOT,
    INPUT_FOLDER_NAMES,
    WORLD_BOUNDARY,
    build_event,
    basemap_sha256,
    cached_detail,
    discover_events,
    inspect_event,
    infer_admin_boundaries_dir,
    json_write,
    pipeline_sha256,
    published_details,
    snapshot_sha256,
    write_batch_catalog,
)


PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_ARCHIVE_ROOT = PROJECT_ROOT / "workspace" / "imports"
MAX_MANIFEST_BYTES = 10 * 1024 * 1024
MAX_FILES = 25_000
MAX_FILE_BYTES = 2 * 1024 * 1024 * 1024
MAX_TOTAL_BYTES = 20 * 1024 * 1024 * 1024
MAX_TARGET_POINTS = 2_500
WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}


class ImportRequestError(ValueError):
    """A browser import request is invalid or incomplete."""


def native_directory_dialog(initial_path: Path | None = None) -> str | None:
    """Open the operating system folder picker and return its selected path."""
    environment = os.environ.copy()
    if initial_path is not None and initial_path.is_dir():
        environment["ZSL_INITIAL_FOLDER"] = str(initial_path)

    if sys.platform == "win32":
        script = r"""
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
Add-Type -AssemblyName System.Windows.Forms
$dialog = New-Object System.Windows.Forms.FolderBrowserDialog
$dialog.Description = '选择地震事件监控根目录'
$dialog.ShowNewFolderButton = $false
if ($env:ZSL_INITIAL_FOLDER -and (Test-Path -LiteralPath $env:ZSL_INITIAL_FOLDER -PathType Container)) {
    $dialog.SelectedPath = $env:ZSL_INITIAL_FOLDER
}
if ($dialog.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) {
    [Console]::Write($dialog.SelectedPath)
}
"""
        command = [
            "powershell.exe",
            "-NoLogo",
            "-NoProfile",
            "-STA",
            "-EncodedCommand",
            base64.b64encode(script.encode("utf-16le")).decode("ascii"),
        ]
    elif sys.platform == "darwin":
        command = [
            "osascript",
            "-e",
            'try',
            "-e",
            'set chosenFolder to choose folder with prompt "选择地震事件监控根目录"',
            "-e",
            "return POSIX path of chosenFolder",
            "-e",
            "on error number -128",
            "-e",
            'return ""',
            "-e",
            "end try",
        ]
    else:
        raise ImportRequestError("当前系统暂不支持原生目录选择器")

    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=environment,
            timeout=600,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ImportRequestError(f"无法打开系统目录选择器: {error}") from error
    if result.returncode:
        detail = result.stderr.strip() or f"退出码 {result.returncode}"
        raise ImportRequestError(f"系统目录选择器失败: {detail}")
    selected = result.stdout.strip()
    return selected or None


def normalize_target_config(value: object | None) -> dict:
    """Validate and canonicalize browser-selected target points."""
    if value is None:
        value = {"mode": "grid", "rows": 10, "columns": 10}
    if not isinstance(value, dict):
        raise ImportRequestError("targetConfig 必须是对象")
    mode = value.get("mode", "grid")
    if mode == "grid":
        rows = value.get("rows", 10)
        columns = value.get("columns", 10)
        if (
            isinstance(rows, bool)
            or isinstance(columns, bool)
            or not isinstance(rows, int)
            or not isinstance(columns, int)
        ):
            raise ImportRequestError("网格行数和列数必须是整数")
        if not 2 <= rows <= 50 or not 2 <= columns <= 50:
            raise ImportRequestError("网格行数和列数必须在 2 到 50 之间（最多 2500 点）")
        if rows * columns > MAX_TARGET_POINTS:
            raise ImportRequestError(f"推测点总数不能超过 {MAX_TARGET_POINTS}")
        core = {"mode": "grid", "rows": rows, "columns": columns, "count": rows * columns}
    elif mode == "custom":
        source = value.get("source")
        if source not in {"csv", "manual"}:
            raise ImportRequestError("自定义点 source 必须是 csv 或 manual")
        raw_points = value.get("points")
        if not isinstance(raw_points, list) or not 1 <= len(raw_points) <= MAX_TARGET_POINTS:
            raise ImportRequestError(f"自定义推测点数必须在 1 到 {MAX_TARGET_POINTS} 之间")
        points = []
        seen = set()
        for index, point in enumerate(raw_points, start=1):
            if not isinstance(point, dict):
                raise ImportRequestError(f"第 {index} 个自定义点必须是包含 lon、lat 的对象")
            lon = point.get("lon")
            lat = point.get("lat")
            if (
                isinstance(lon, bool)
                or isinstance(lat, bool)
                or not isinstance(lon, (int, float))
                or not isinstance(lat, (int, float))
            ):
                raise ImportRequestError(f"第 {index} 个自定义点的经纬度必须是数值")
            lon = float(lon)
            lat = float(lat)
            if not math.isfinite(lon) or not math.isfinite(lat):
                raise ImportRequestError(f"第 {index} 个自定义点的经纬度必须是有限数")
            if not -180 <= lon <= 180 or not -90 <= lat <= 90:
                raise ImportRequestError(f"第 {index} 个自定义点的经纬度超出合法范围")
            coordinate = (round(lon, 4), round(lat, 4))
            if coordinate in seen:
                raise ImportRequestError(
                    f"第 {index} 个自定义点在 4 位小数计算精度下与前面的坐标重复"
                )
            seen.add(coordinate)
            points.append({"lon": coordinate[0], "lat": coordinate[1]})
        core = {"mode": "custom", "source": source, "points": points, "count": len(points)}
    else:
        raise ImportRequestError("targetConfig.mode 必须是 grid 或 custom")

    canonical = json.dumps(core, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return {**core, "hash": hashlib.sha256(canonical.encode("utf-8")).hexdigest()}


def processed_event_identifier(
    inspected_event_id: str,
    input_hash: str,
    method_hash: str,
    basemap_hash: str | None,
    target_hash: str,
) -> str:
    origin_prefix = inspected_event_id.split("-", 1)[0]
    return (
        f"{origin_prefix}-{input_hash[:12]}-p{method_hash[:8]}-"
        f"m{basemap_hash[:8] if basemap_hash else 'nomap'}-t{target_hash[:8]}"
    )


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def safe_relative_path(value: object) -> str:
    raw = str(value or "").replace("\\", "/").strip("/")
    if not raw or len(raw) > 768 or "\x00" in raw:
        raise ImportRequestError("文件相对路径为空或过长")
    path = PurePosixPath(raw)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ImportRequestError(f"不安全的文件相对路径: {raw}")
    if ":" in path.parts[0]:
        raise ImportRequestError(f"文件路径不能包含盘符: {raw}")
    for part in path.parts:
        if any(character in '<>:"|?*' or ord(character) < 32 for character in part):
            raise ImportRequestError(f"Windows 路径段包含非法字符: {raw}")
        if part.rstrip(" .") != part:
            raise ImportRequestError(f"Windows 路径段不能以空格或点结尾: {raw}")
        if part.split(".", 1)[0].upper() in WINDOWS_RESERVED_NAMES:
            raise ImportRequestError(f"Windows 保留名称不能作为路径段: {raw}")
    return path.as_posix()


def input_content_sha256(files: dict[str, dict]) -> str:
    digest = hashlib.sha256()
    for relative, entry in sorted(files.items(), key=lambda item: item[0].casefold()):
        digest.update(relative.encode("utf-8"))
        digest.update(f"\0{entry['size']}\0{entry['sha256']}\n".encode("ascii"))
    return digest.hexdigest()


class ArchiveService:
    """Own uploaded raw inputs, processing jobs, and the persistent archive index."""

    def __init__(
        self,
        *,
        archive_root: Path = DEFAULT_ARCHIVE_ROOT,
        output_root: Path = DEFAULT_OUTPUT_ROOT,
        tianditu_bundle: Path | None = None,
        tianditu_index: Path | None = None,
        admin_boundaries_dir: Path | None = None,
        save_dat: bool = True,
        make_package: bool = True,
    ) -> None:
        self.archive_root = archive_root.resolve()
        self.output_root = output_root.resolve()
        self.tianditu_bundle = tianditu_bundle.resolve() if tianditu_bundle else None
        self.tianditu_index = tianditu_index.resolve() if tianditu_index else None
        self.admin_boundaries_dir = (
            admin_boundaries_dir.resolve() if admin_boundaries_dir else None
        )
        self.save_dat = save_dat
        self.make_package = make_package
        self.archive_index = self.output_root / "archive.json"
        self.archive_root.mkdir(parents=True, exist_ok=True)
        self.output_root.mkdir(parents=True, exist_ok=True)
        self._jobs: dict[str, dict] = {}
        self._lock = threading.RLock()
        self._processing_lock = threading.Lock()
        self._watch_stop = threading.Event()
        self._watch_thread: threading.Thread | None = None
        self._watch_seen: dict[str, dict] = {}
        self._watch = {
            "status": "stopped",
            "root": None,
            "stableSeconds": 15,
            "processExisting": False,
            "detectedEvents": 0,
            "pendingEvents": 0,
            "lastJobId": None,
            "lastEvent": None,
            "lastScanAt": None,
            "messages": [],
        }

        if not WORLD_BOUNDARY.is_file():
            raise FileNotFoundError(f"缺少网站全球底图: {WORLD_BOUNDARY}")
        if self.tianditu_bundle is not None:
            if not self.tianditu_bundle.is_file():
                raise FileNotFoundError(f"天地图总包不存在: {self.tianditu_bundle}")
            if self.tianditu_index is None:
                self.tianditu_index = self.tianditu_bundle.with_name("tianditu_250k_batches.csv")
            if not self.tianditu_index.is_file():
                raise FileNotFoundError(f"天地图图幅索引不存在: {self.tianditu_index}")
        elif self.tianditu_index is not None:
            raise ValueError("天地图索引只能与总包一起使用")

        if self.admin_boundaries_dir is None and self.tianditu_bundle is not None:
            self.admin_boundaries_dir = infer_admin_boundaries_dir(self.tianditu_bundle)
        if self.admin_boundaries_dir is not None:
            missing_admin = [
                filename
                for _, filename in ADMIN_LEVEL_FILES
                if not (self.admin_boundaries_dir / filename).is_file()
            ]
            if missing_admin:
                raise FileNotFoundError(
                    f"行政区划目录缺少文件: {', '.join(missing_admin)}"
                )

        self.boundary = json.loads(WORLD_BOUNDARY.read_text(encoding="utf-8"))
        self.method_hash = pipeline_sha256()
        self.basemap_hash = basemap_sha256(
            self.tianditu_bundle,
            self.tianditu_index,
            self.admin_boundaries_dir,
        )

    def _current_pipeline_hash(self) -> str:
        """Fingerprint the source currently present on disk."""
        return pipeline_sha256()

    def _require_current_pipeline(self) -> None:
        current_hash = self._current_pipeline_hash()
        if current_hash != self.method_hash:
            raise ImportRequestError(
                "本机处理代码已更新，但当前 8000 端口仍在运行旧版本；"
                "请关闭并重新运行启动脚本后再处理，避免生成旧版图件"
            )

    def health(self) -> dict:
        current_hash = self._current_pipeline_hash()
        return {
            "status": "ok",
            "service": "zsl-local-archive",
            "schemaVersion": 1,
            "processingPipelineSha256": self.method_hash,
            "diskPipelineSha256": current_hash,
            "restartRequired": current_hash != self.method_hash,
            "tiandituConfigured": self.tianditu_bundle is not None,
            "administrativeBasemapConfigured": self.admin_boundaries_dir is not None,
            "basemapMode": (
                "administrative-boundaries-on-demand"
                if self.admin_boundaries_dir
                else "world-boundary-fallback"
            ),
            "basemapSourceSha256": self.basemap_hash,
            "acceptedFolders": sorted(INPUT_FOLDER_NAMES),
            "archiveCount": len(self.archives()["archives"]),
            "watch": self.watch_status(),
        }

    def watch_status(self) -> dict:
        with self._lock:
            status = {
                key: self._watch.get(key)
                for key in [
                    "status",
                    "root",
                    "stableSeconds",
                    "processExisting",
                    "detectedEvents",
                    "pendingEvents",
                    "lastJobId",
                    "lastEvent",
                    "lastScanAt",
                    "error",
                    "messages",
                ]
                if key in self._watch
            }
            queue, counts = self._watch_queue_locked()
            status["queue"] = queue
            status["queueCounts"] = counts
            return status

    def _watch_queue_locked(self) -> tuple[list[dict], dict[str, int]]:
        """Return current monitor work plus a short in-memory job history."""
        now = time.monotonic()
        stable_seconds = max(1, int(self._watch.get("stableSeconds") or 15))
        pending = []
        for relative, state in self._watch_seen.items():
            fingerprint = state.get("fingerprint")
            if (
                state.get("processedFingerprint") == fingerprint
                or state.get("queuedFingerprint") == fingerprint
            ):
                continue
            elapsed = max(0.0, now - float(state.get("stableSince") or now))
            remaining = max(0.0, stable_seconds - elapsed)
            stabilizing = remaining > 0
            pending.append(
                {
                    "event": relative,
                    "status": "stabilizing" if stabilizing else "waiting",
                    "message": (
                        f"目录内容稳定后进入队列，约剩 {max(1, int(math.ceil(remaining)))} 秒"
                        if stabilizing
                        else "文件已稳定，等待前序事件处理完成"
                    ),
                    "progress": {
                        "phase": "file_stability" if stabilizing else "queued",
                        "label": "等待文件稳定" if stabilizing else "排队等待",
                        "completed": min(int(elapsed), stable_seconds) if stabilizing else 0,
                        "total": stable_seconds if stabilizing else 1,
                        "unit": "秒" if stabilizing else "事件",
                        "percent": round(min(100 * elapsed / stable_seconds, 100), 1) if stabilizing else 0,
                        "elapsedSeconds": round(elapsed, 1),
                    },
                }
            )

        jobs = []
        for job in self._jobs.values():
            if job.get("sourceMode") != "folder-monitor":
                continue
            jobs.append(
                {
                    "event": job.get("watchedSourceRelativePath") or job.get("rootName") or "未命名事件",
                    "status": job.get("status", "queued"),
                    "jobId": job.get("jobId"),
                    "createdAt": job.get("createdAt"),
                    "updatedAt": job.get("updatedAt"),
                    "completedAt": job.get("completedAt"),
                    "eventUrl": job.get("eventUrl"),
                    "message": job.get("message") or "等待处理",
                    "error": job.get("error"),
                    "progress": dict(job.get("progress") or {}),
                }
            )

        active_statuses = {"uploading", "queued", "inspecting", "processing"}
        active = [job for job in jobs if job["status"] in active_statuses]
        history = sorted(
            (job for job in jobs if job["status"] not in active_statuses),
            key=lambda job: job.get("completedAt") or job.get("updatedAt") or "",
            reverse=True,
        )[:10]
        pending.sort(key=lambda row: (row["status"] == "stabilizing", row["event"].casefold()))
        active.sort(key=lambda row: row.get("createdAt") or "")
        queue = [*active, *pending, *history]
        counts = {
            "waiting": len(pending) + sum(job["status"] in {"uploading", "queued"} for job in jobs),
            "processing": sum(job["status"] in {"inspecting", "processing"} for job in jobs),
            "success": sum(job["status"] == "success" for job in jobs),
            "failed": sum(job["status"] == "failed" for job in jobs),
        }
        return queue, counts

    def _watch_log(self, message: str) -> None:
        with self._lock:
            messages = [*self._watch.get("messages", []), {"time": utc_now(), "message": message}]
            self._watch["messages"] = messages[-30:]

    @staticmethod
    def _contains(parent: Path, child: Path) -> bool:
        try:
            child.relative_to(parent)
            return True
        except ValueError:
            return False

    def select_watch_folder(self, payload: object) -> dict:
        if not isinstance(payload, dict):
            raise ImportRequestError("目录选择参数必须是 JSON 对象")
        raw_initial = str(payload.get("initialPath") or "").strip()
        initial_path = Path(raw_initial).expanduser() if raw_initial else None
        selected = native_directory_dialog(initial_path)
        if not selected:
            return {"cancelled": True, "path": None}
        root = Path(selected).expanduser().resolve()
        if not root.is_dir():
            raise ImportRequestError(f"所选目录不存在: {root}")
        if self._contains(root, self.archive_root) or self._contains(root, self.output_root):
            raise ImportRequestError("监控根目录不能包含网站输出或原始输入归档目录")
        return {"cancelled": False, "path": str(root)}

    def start_watch(self, payload: object) -> dict:
        if not isinstance(payload, dict):
            raise ImportRequestError("监控参数必须是 JSON 对象")
        raw_root = str(payload.get("path") or "").strip()
        if not raw_root:
            raise ImportRequestError("请填写本机监控根目录")
        root = Path(raw_root).expanduser().resolve()
        if not root.is_dir():
            raise ImportRequestError(f"监控根目录不存在: {root}")
        if self._contains(root, self.archive_root) or self._contains(root, self.output_root):
            raise ImportRequestError("监控根目录不能包含网站输出或原始输入归档目录")
        try:
            stable_seconds = int(payload.get("stableSeconds", 15))
        except (TypeError, ValueError) as error:
            raise ImportRequestError("稳定等待秒数必须是整数") from error
        if not 5 <= stable_seconds <= 3600:
            raise ImportRequestError("稳定等待秒数必须在 5 到 3600 之间")
        process_existing = bool(payload.get("processExisting", False))
        with self._lock:
            if self._watch.get("status") == "running":
                raise ImportRequestError("文件夹监控已经运行")
            previous_thread = self._watch_thread
        if previous_thread and previous_thread.is_alive():
            previous_thread.join(timeout=4)
        with self._lock:
            if self._watch.get("status") == "running":
                raise ImportRequestError("文件夹监控已经运行")
            self._watch_stop = threading.Event()
            self._watch_seen = {}
            now = time.monotonic()
            sources = discover_events(root)
            for source in sources:
                fingerprint = snapshot_sha256(source.files, root)
                self._watch_seen[source.relative_path] = {
                    "fingerprint": fingerprint,
                    "stableSince": now,
                    "processedFingerprint": None if process_existing else fingerprint,
                    "queuedFingerprint": None,
                }
            self._watch = {
                "status": "running",
                "root": str(root),
                "stableSeconds": stable_seconds,
                "processExisting": process_existing,
                "detectedEvents": len(sources),
                "pendingEvents": len(sources) if process_existing else 0,
                "lastJobId": None,
                "lastEvent": None,
                "lastScanAt": utc_now(),
                "messages": [],
            }
            self._watch_thread = threading.Thread(
                target=self._watch_loop,
                args=(self._watch_stop,),
                daemon=True,
            )
            self._watch_thread.start()
        action = "会处理现有事件和后续变化" if process_existing else "只处理启动后的新增或变化事件"
        self._watch_log(f"开始监控 {root}；{action}；文件稳定 {stable_seconds} 秒后进入处理")
        return self.watch_status()

    def stop_watch(self) -> dict:
        with self._lock:
            if self._watch.get("status") != "running":
                return self.watch_status()
            self._watch_stop.set()
            watch_thread = self._watch_thread
            self._watch["status"] = "stopped"
            self._watch["lastScanAt"] = utc_now()
        if watch_thread and watch_thread is not threading.current_thread():
            watch_thread.join(timeout=4)
        self._watch_log("文件夹监控已停止；已进入计算队列的事件会继续完成")
        return self.watch_status()

    def _watch_loop(self, stop_event: threading.Event) -> None:
        while not stop_event.is_set():
            try:
                self._scan_watch_once()
            except Exception as error:
                with self._lock:
                    self._watch["error"] = f"{type(error).__name__}: {error}"
                    self._watch["lastScanAt"] = utc_now()
                self._watch_log(f"监控扫描失败: {error}")
            stop_event.wait(3)

    def _processing_busy(self) -> bool:
        with self._lock:
            return any(
                job.get("status") in {"queued", "inspecting", "processing"}
                for job in self._jobs.values()
            )

    def _scan_watch_once(self) -> None:
        with self._lock:
            if self._watch.get("status") != "running":
                return
            root = Path(self._watch["root"])
            stable_seconds = int(self._watch["stableSeconds"])
        sources = discover_events(root)
        now = time.monotonic()
        candidates = []
        current_paths = set()
        with self._lock:
            for source in sources:
                current_paths.add(source.relative_path)
                fingerprint = snapshot_sha256(source.files, root)
                state = self._watch_seen.get(source.relative_path)
                if state is None:
                    state = {
                        "fingerprint": fingerprint,
                        "stableSince": now,
                        "processedFingerprint": None,
                        "queuedFingerprint": None,
                    }
                    self._watch_seen[source.relative_path] = state
                    self._watch_log(f"发现新事件目录: {source.relative_path}")
                elif state["fingerprint"] != fingerprint:
                    state.update(
                        fingerprint=fingerprint,
                        stableSince=now,
                        processedFingerprint=None,
                        queuedFingerprint=None,
                    )
                    self._watch_log(f"事件目录内容变化，重新等待稳定: {source.relative_path}")
                if (
                    state.get("processedFingerprint") != fingerprint
                    and state.get("queuedFingerprint") != fingerprint
                    and now - float(state["stableSince"]) >= stable_seconds
                ):
                    candidates.append((source, fingerprint))
            for relative in set(self._watch_seen) - current_paths:
                del self._watch_seen[relative]
            self._watch["detectedEvents"] = len(sources)
            self._watch["pendingEvents"] = len(candidates)
            self._watch["lastScanAt"] = utc_now()
            self._watch.pop("error", None)
        if not candidates or self._processing_busy():
            return
        source, fingerprint = sorted(candidates, key=lambda item: item[0].relative_path.casefold())[0]
        with self._lock:
            state = self._watch_seen.get(source.relative_path)
            if (
                state is None
                or state["fingerprint"] != fingerprint
                or state.get("processedFingerprint") == fingerprint
                or state.get("queuedFingerprint") == fingerprint
            ):
                return
            state["queuedFingerprint"] = fingerprint
        try:
            job_id = self._import_watched_source(root, source)
        except Exception as error:
            with self._lock:
                state = self._watch_seen.get(source.relative_path)
                if (
                    state
                    and state["fingerprint"] == fingerprint
                    and state.get("queuedFingerprint") == fingerprint
                ):
                    state["queuedFingerprint"] = None
                    state["stableSince"] = time.monotonic()
            self._watch_log(f"无法归档监控事件 {source.relative_path}: {error}")
            return
        with self._lock:
            state = self._watch_seen.get(source.relative_path)
            if (
                state
                and state["fingerprint"] == fingerprint
                and state.get("queuedFingerprint") == fingerprint
            ):
                state["processedFingerprint"] = fingerprint
            self._watch["lastJobId"] = job_id
            self._watch["lastEvent"] = source.relative_path
        self._watch_log(f"事件已稳定并进入处理队列: {source.relative_path}")

    def _import_watched_source(self, watch_root: Path, source) -> str:
        rows = []
        for path in source.files:
            stat = path.stat()
            rows.append(
                {
                    "path": path.relative_to(watch_root).as_posix(),
                    "size": stat.st_size,
                    "lastModified": stat.st_mtime_ns // 1_000_000,
                }
            )
        public = self.create_job(
            {"rootName": source.event_root.name, "files": rows}
        )
        job_id = public["jobId"]
        with self._lock:
            job = self._jobs[job_id]
            job["sourceMode"] = "folder-monitor"
            job["watchedSourceRelativePath"] = source.relative_path
            self._persist_job(job)
        for path, row in zip(source.files, rows):
            with path.open("rb") as stream:
                self.upload_file(job_id, row["path"], stream, row["size"])
        self.start_processing(job_id, {})
        return job_id

    def archives(self) -> dict:
        try:
            payload = json.loads(self.archive_index.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"schemaVersion": 1, "generatedAt": utc_now(), "archives": []}
        except (OSError, json.JSONDecodeError) as error:
            raise ImportRequestError(f"归档索引无法读取: {error}") from error
        if not isinstance(payload, dict) or not isinstance(payload.get("archives"), list):
            raise ImportRequestError("归档索引格式无效")
        return payload

    def create_job(self, payload: object) -> dict:
        if not isinstance(payload, dict):
            raise ImportRequestError("导入清单必须是 JSON 对象")
        root_name = str(payload.get("rootName") or "event").strip()[:200] or "event"
        rows = payload.get("files")
        if not isinstance(rows, list) or not rows:
            raise ImportRequestError("导入目录中没有 .dat 文件")
        if len(rows) > MAX_FILES:
            raise ImportRequestError(f"单次导入最多允许 {MAX_FILES} 个文件")

        files: dict[str, dict] = {}
        folded_paths: set[str] = set()
        total_bytes = 0
        has_data_folder = False
        for row in rows:
            if not isinstance(row, dict):
                raise ImportRequestError("文件清单行必须是对象")
            relative = safe_relative_path(row.get("path"))
            if Path(relative).suffix.casefold() != ".dat":
                raise ImportRequestError(f"原始导入只接受 .dat 文件: {relative}")
            if relative in files:
                raise ImportRequestError(f"文件清单含重复路径: {relative}")
            folded = relative.casefold()
            if folded in folded_paths:
                raise ImportRequestError(f"文件清单含 Windows 大小写冲突路径: {relative}")
            folded_paths.add(folded)
            try:
                size = int(row.get("size"))
                last_modified = int(row.get("lastModified") or 0)
            except (TypeError, ValueError) as error:
                raise ImportRequestError(f"文件大小或修改时间无效: {relative}") from error
            if size < 0 or size > MAX_FILE_BYTES:
                raise ImportRequestError(f"文件大小超限: {relative}")
            total_bytes += size
            if total_bytes > MAX_TOTAL_BYTES:
                raise ImportRequestError("单次导入总大小超过 20 GiB")
            has_data_folder = has_data_folder or any(
                part.casefold() in INPUT_FOLDER_NAMES for part in PurePosixPath(relative).parts[:-1]
            )
            files[relative] = {
                "size": size,
                "lastModified": last_modified,
                "uploaded": False,
                "sha256": None,
            }
        if not has_data_folder:
            raise ImportRequestError("请选择包含 EIdata、HNdata 或 ElData 的事件目录")

        job_id = uuid.uuid4().hex[:16]
        job_dir = self.archive_root / job_id
        source_dir = job_dir / "source"
        source_dir.mkdir(parents=True)
        created_at = utc_now()
        job = {
            "schemaVersion": 1,
            "jobId": job_id,
            "status": "uploading",
            "rootName": root_name,
            "createdAt": created_at,
            "updatedAt": created_at,
            "expectedFiles": len(files),
            "uploadedFiles": 0,
            "totalBytes": total_bytes,
            "uploadedBytes": 0,
            "files": files,
            "sourceDirectory": str(source_dir),
            "eventIds": [],
            "message": "等待浏览器上传原始记录",
            "error": None,
            "progress": {
                "phase": "queued",
                "label": "等待处理",
                "stageIndex": 1,
                "stageCount": 9,
                "completed": 0,
                "total": 1,
                "unit": "阶段",
                "percent": 0.0,
                "message": "原始记录上传完成后可开始本机处理",
                "startedAt": None,
                "updatedAt": created_at,
                "elapsedSeconds": 0.0,
            },
        }
        with self._lock:
            self._jobs[job_id] = job
            self._persist_job(job)
        return self.public_job(job)

    def public_job(self, job: dict) -> dict:
        return {
            key: job.get(key)
            for key in [
                "schemaVersion",
                "jobId",
                "status",
                "rootName",
                "createdAt",
                "startedAt",
                "completedAt",
                "updatedAt",
                "expectedFiles",
                "uploadedFiles",
                "totalBytes",
                "uploadedBytes",
                "inputContentSha256",
                "eventIds",
                "eventUrl",
                "targetConfig",
                "message",
                "progress",
                "error",
                "inspection",
                "sourceMode",
                "watchedSourceRelativePath",
            ]
            if key in job
        }

    def get_job(self, job_id: str) -> dict:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise ImportRequestError("导入任务不存在或服务已经重启")
            return self.public_job(job)

    def upload_file(self, job_id: str, relative_path: object, stream, content_length: int) -> dict:
        relative = safe_relative_path(relative_path)
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise ImportRequestError("导入任务不存在")
            if job["status"] != "uploading":
                raise ImportRequestError("导入任务已不接受文件")
            entry = job["files"].get(relative)
            if entry is None:
                raise ImportRequestError(f"文件不在导入清单中: {relative}")
            if content_length != entry["size"]:
                raise ImportRequestError(
                    f"文件大小与清单不一致: {relative} ({content_length} != {entry['size']})"
                )

        source_dir = Path(job["sourceDirectory"])
        target = source_dir.joinpath(*PurePosixPath(relative).parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.upload-{os.getpid()}-{threading.get_ident()}")
        digest = hashlib.sha256()
        remaining = content_length
        try:
            with temporary.open("wb") as output:
                while remaining:
                    block = stream.read(min(1024 * 1024, remaining))
                    if not block:
                        raise ImportRequestError(f"文件上传提前结束: {relative}")
                    output.write(block)
                    digest.update(block)
                    remaining -= len(block)
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)

        if entry["lastModified"] > 0:
            timestamp = entry["lastModified"] / 1000.0
            os.utime(target, (timestamp, timestamp))
        with self._lock:
            if not entry["uploaded"]:
                job["uploadedFiles"] += 1
                job["uploadedBytes"] += content_length
            entry["uploaded"] = True
            entry["sha256"] = digest.hexdigest()
            job["updatedAt"] = utc_now()
            job["message"] = f"已上传 {job['uploadedFiles']}/{job['expectedFiles']} 个文件"
            self._persist_job(job)
            return self.public_job(job)

    def start_processing(self, job_id: str, payload: object | None = None) -> dict:
        self._require_current_pipeline()
        if payload is None:
            payload = {}
        if not isinstance(payload, dict):
            raise ImportRequestError("处理参数必须是 JSON 对象")
        override = payload.get("metadataOverride")
        if override is not None and not isinstance(override, dict):
            raise ImportRequestError("metadataOverride 必须是对象")
        target_config = normalize_target_config(payload.get("targetConfig"))
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise ImportRequestError("导入任务不存在")
            if job["status"] != "uploading":
                raise ImportRequestError("导入任务已经开始处理")
            if job["uploadedFiles"] != job["expectedFiles"]:
                raise ImportRequestError(
                    f"仍有 {job['expectedFiles'] - job['uploadedFiles']} 个文件未上传"
                )
            if any(not entry.get("sha256") for entry in job["files"].values()):
                raise ImportRequestError("文件哈希尚未完整生成")
            job["status"] = "queued"
            job["metadataOverride"] = override
            job["targetConfig"] = target_config
            job["inputContentSha256"] = input_content_sha256(job["files"])
            self._update_progress(
                job,
                {
                    "phase": "queued",
                    "label": "等待处理",
                    "stageIndex": 1,
                    "stageCount": 9,
                    "completed": 0,
                    "total": 1,
                    "unit": "阶段",
                    "percent": 0,
                    "message": "已进入本机计算队列",
                },
            )
            thread = threading.Thread(target=self._process_job, args=(job_id,), daemon=True)
            thread.start()
            return self.public_job(job)

    def _set_job(self, job: dict, status: str, message: str, **values) -> None:
        with self._lock:
            job.update(values)
            job["status"] = status
            job["message"] = message
            job["updatedAt"] = utc_now()
            self._persist_job(job)

    def _update_progress(self, job: dict, values: dict) -> None:
        with self._lock:
            previous = job.get("progress") or {}
            now = utc_now()
            started_at = values.get("startedAt") or previous.get("startedAt") or job.get("startedAt")
            previous_percent = float(previous.get("percent") or 0)
            percent = max(previous_percent, min(max(float(values.get("percent", previous_percent)), 0), 100))
            elapsed_seconds = 0.0
            if started_at:
                elapsed_seconds = max(
                    0.0,
                    (datetime.fromisoformat(now) - datetime.fromisoformat(started_at)).total_seconds(),
                )
            progress = {
                "phase": values.get("phase", previous.get("phase", "queued")),
                "label": values.get("label", previous.get("label", "等待处理")),
                "stageIndex": int(values.get("stageIndex", previous.get("stageIndex", 1))),
                "stageCount": int(values.get("stageCount", previous.get("stageCount", 9))),
                "completed": int(values.get("completed", previous.get("completed", 0))),
                "total": int(values.get("total", previous.get("total", 1))),
                "unit": values.get("unit", previous.get("unit", "阶段")),
                "percent": round(percent, 1),
                "message": values.get("message", previous.get("message", job.get("message", ""))),
                "startedAt": started_at,
                "updatedAt": now,
                "elapsedSeconds": round(elapsed_seconds, 3),
            }
            for key in ("accepted", "skipped"):
                if key in values:
                    progress[key] = int(values[key])
            job["progress"] = progress
            job["updatedAt"] = now
            job["message"] = progress["message"]
            self._persist_job(job)

    def _process_job(self, job_id: str) -> None:
        with self._lock:
            job = self._jobs[job_id]
        inspection_report = None
        try:
            self._set_job(job, "queued", "等待当前计算结束")
            self._update_progress(
                job,
                {
                    "phase": "queued",
                    "label": "等待处理",
                    "stageIndex": 1,
                    "stageCount": 9,
                    "completed": 0,
                    "total": 1,
                    "unit": "阶段",
                    "percent": 0,
                    "message": "等待当前计算任务结束",
                },
            )
            with self._processing_lock:
                self._require_current_pipeline()
                started_at = utc_now()
                self._set_job(job, "inspecting", "正在解析头段并执行输入质量控制", startedAt=started_at)
                self._update_progress(
                    job,
                    {
                        "phase": "input_read",
                        "label": "读取输入记录",
                        "stageIndex": 1,
                        "stageCount": 9,
                        "completed": 0,
                        "total": job["expectedFiles"],
                        "unit": "文件",
                        "percent": 0,
                        "message": "正在解析头段并执行输入质量控制",
                        "startedAt": started_at,
                    },
                )
                source_root = Path(job["sourceDirectory"])
                sources = discover_events(source_root)
                if len(sources) != 1:
                    raise ImportRequestError(
                        f"本次导入识别到 {len(sources)} 个事件；请选择只包含一个事件的目录"
                    )
                inspection = inspect_event(sources[0], source_root, job.get("metadataOverride"))
                inspection_report = inspection.report()
                if inspection.status != "ready":
                    reasons = "；".join(inspection.errors) or "完整三分量台站不足或台站几何不适用"
                    raise ImportRequestError(f"输入质量控制未通过: {reasons}")

                target_config = job.get("targetConfig") or normalize_target_config(None)
                inspection.event_id = processed_event_identifier(
                    inspection.event_id,
                    job["inputContentSha256"],
                    self.method_hash,
                    self.basemap_hash,
                    target_config["hash"],
                )
                self._set_job(
                    job,
                    "processing",
                    f"正在计算 {inspection.complete_stations} 个实测台站的推测场与图件",
                    eventIds=[inspection.event_id],
                    inspection=inspection_report,
                )

                def persist_build_progress(values: dict) -> None:
                    progress = dict(values)
                    if progress.get("stageIndex") == progress.get("stageCount"):
                        completed = int(progress["completed"])
                        total = int(progress["total"]) + 1
                        progress["total"] = total
                        progress["percent"] = round(
                            100 * (int(progress["stageIndex"]) - 1 + completed / total)
                            / int(progress["stageCount"]),
                            1,
                        )
                    self._update_progress(job, progress)

                detail = cached_detail(
                    inspection,
                    self.output_root,
                    self.method_hash,
                    self.basemap_hash,
                    save_dat=self.save_dat,
                    make_package=self.make_package,
                )
                if detail is None:
                    detail = build_event(
                        inspection,
                        self.output_root,
                        self.boundary,
                        save_dat=self.save_dat,
                        make_package=self.make_package,
                        method_hash=self.method_hash,
                        tianditu_bundle=self.tianditu_bundle,
                        tianditu_index=self.tianditu_index,
                        admin_boundaries_dir=self.admin_boundaries_dir,
                        basemap_hash=self.basemap_hash,
                        input_content_sha256=job["inputContentSha256"],
                        archive_id=job_id,
                        grid_shape=(
                            (target_config["rows"], target_config["columns"])
                            if target_config["mode"] == "grid"
                            else None
                        ),
                        custom_target_points=(
                            target_config["points"]
                            if target_config["mode"] == "custom"
                            else None
                        ),
                        target_config=target_config,
                        progress_callback=persist_build_progress,
                    )
                    cache_used = False
                else:
                    cache_used = True
                    self._update_progress(
                        job,
                        {
                            "phase": "package_publish",
                            "label": "发布缓存结果",
                            "stageIndex": 9,
                            "stageCount": 9,
                            "completed": 1,
                            "total": 2,
                            "unit": "项",
                            "percent": 94.4,
                            "message": "已复用校验通过的结果，正在更新事件目录与归档记录",
                        },
                    )

                all_details = published_details(self.output_root)
                summary = {
                    "mode": "interactive-import",
                    "generatedAt": utc_now(),
                    "archiveId": job_id,
                    "published": len(all_details),
                    "eventId": detail["eventId"],
                    "cacheUsed": cache_used,
                    "tiandituBasemap": self.tianditu_bundle is not None,
                    "administrativeBasemap": self.admin_boundaries_dir is not None,
                    "processingPipelineSha256": self.method_hash,
                    "basemapSourceSha256": self.basemap_hash,
                    "targetConfiguration": {
                        key: target_config[key]
                        for key in ("mode", "count", "hash", "rows", "columns", "source")
                        if key in target_config
                    },
                }
                write_batch_catalog(all_details, summary)
                event_url = f"./demo/batch/events/{detail['eventId']}/event.json"
                completed_at = utc_now()
                record = {
                    "archiveId": job_id,
                    "status": "success",
                    "rootName": job["rootName"],
                    "createdAt": job["createdAt"],
                    "completedAt": completed_at,
                    "inputFiles": job["expectedFiles"],
                    "inputBytes": job["totalBytes"],
                    "inputContentSha256": job["inputContentSha256"],
                    "sourceRetained": True,
                    "sourceMode": job.get("sourceMode", "browser-upload"),
                    "watchedSourceRelativePath": job.get("watchedSourceRelativePath"),
                    "eventId": detail["eventId"],
                    "title": detail["title"],
                    "originTime": detail["originTime"],
                    "observedStations": len(detail["observed"]),
                    "inferredPoints": len(detail["inferred"]),
                    "eventUrl": event_url,
                    "packageUrl": (detail.get("resultPackage") or {}).get("url"),
                    "basemap": (
                        "administrative-boundaries-on-demand"
                        if detail.get("basemapUrl")
                        else "world-boundary-fallback"
                    ),
                    "cacheUsed": cache_used,
                    "processingPipelineSha256": self.method_hash,
                    "basemapSourceSha256": self.basemap_hash,
                    "targetConfiguration": summary["targetConfiguration"],
                }
                self._write_archive_record(record)
                with self._lock:
                    self._set_job(
                        job,
                        "success",
                        "计算完成，结果已写入事件目录与归档记录",
                        completedAt=completed_at,
                        eventUrl=event_url,
                        inspection=inspection.report(),
                        archiveId=job_id,
                    )
                    current_progress = job["progress"]
                    self._update_progress(
                        job,
                        {
                            "phase": "complete",
                            "label": "处理完成",
                            "stageIndex": 9,
                            "stageCount": 9,
                            "completed": current_progress["total"],
                            "total": current_progress["total"],
                            "unit": current_progress["unit"],
                            "percent": 100,
                            "message": "计算完成，结果已写入事件目录与归档记录",
                        },
                    )
        except Exception as error:
            completed_at = utc_now()
            record = {
                "archiveId": job_id,
                "status": "failed",
                "rootName": job.get("rootName"),
                "createdAt": job.get("createdAt"),
                "completedAt": completed_at,
                "inputFiles": job.get("expectedFiles"),
                "inputBytes": job.get("totalBytes"),
                "inputContentSha256": job.get("inputContentSha256"),
                "sourceRetained": True,
                "sourceMode": job.get("sourceMode", "browser-upload"),
                "watchedSourceRelativePath": job.get("watchedSourceRelativePath"),
                "targetConfiguration": {
                    key: job["targetConfig"][key]
                    for key in ("mode", "count", "hash", "rows", "columns", "source")
                    if job.get("targetConfig") and key in job["targetConfig"]
                },
                "error": f"{type(error).__name__}: {error}",
                "inspection": inspection_report,
            }
            try:
                self._write_archive_record(record)
            finally:
                with self._lock:
                    self._set_job(
                        job,
                        "failed",
                        "处理失败；原始输入和质控记录已保留",
                        completedAt=completed_at,
                        error=f"{type(error).__name__}: {error}",
                        inspection=inspection_report,
                        traceback=traceback.format_exc(),
                    )
                    current_progress = job.get("progress") or {}
                    self._update_progress(
                        job,
                        {
                            "phase": "failed",
                            "label": "处理失败",
                            "stageIndex": current_progress.get("stageIndex", 1),
                            "stageCount": current_progress.get("stageCount", 9),
                            "completed": current_progress.get("completed", 0),
                            "total": current_progress.get("total", 1),
                            "unit": current_progress.get("unit", "阶段"),
                            "percent": current_progress.get("percent", 0),
                            "message": "处理失败；原始输入和质控记录已保留",
                        },
                    )

    def _write_archive_record(self, record: dict) -> None:
        with self._lock:
            payload = self.archives()
            records = [row for row in payload["archives"] if row.get("archiveId") != record["archiveId"]]
            records.append(record)
            records.sort(key=lambda row: row.get("completedAt") or row.get("createdAt") or "", reverse=True)
            json_write(
                self.archive_index,
                {"schemaVersion": 1, "generatedAt": utc_now(), "archives": records},
            )

    def _persist_job(self, job: dict) -> None:
        job_path = self.archive_root / job["jobId"] / "job.json"
        json_write(job_path, job)


def json_body(handler: SimpleHTTPRequestHandler, limit: int = MAX_MANIFEST_BYTES) -> object:
    try:
        length = int(handler.headers.get("Content-Length", "0"))
    except ValueError as error:
        raise ImportRequestError("Content-Length 无效") from error
    if length < 0 or length > limit:
        raise ImportRequestError("JSON 请求体过大")
    raw = handler.rfile.read(length)
    try:
        return json.loads(raw.decode("utf-8")) if raw else {}
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ImportRequestError(f"JSON 请求体无效: {error}") from error


def handler_class(service: ArchiveService):
    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs) -> None:
            super().__init__(*args, directory=str(FRONTEND_ROOT), **kwargs)

        def end_headers(self) -> None:
            if not urlsplit(self.path).path.startswith("/api/"):
                self.send_header("Cache-Control", "no-store, max-age=0")
            super().end_headers()

        def send_json(self, status: int, payload: object) -> None:
            body = (json.dumps(payload, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def api_error(self, error: Exception, status: int = 400) -> None:
            self.send_json(status, {"error": str(error), "type": type(error).__name__})

        def require_local_host(self) -> str:
            host = (self.headers.get("Host") or "").strip()
            parsed = urlsplit(f"http://{host}")
            if parsed.hostname not in {"127.0.0.1", "localhost"}:
                raise ImportRequestError("本机服务只接受 localhost 或 127.0.0.1 请求")
            if parsed.username or parsed.password or parsed.path not in {"", "/"}:
                raise ImportRequestError("Host 请求头无效")
            return host

        def require_same_origin(self) -> None:
            host = self.require_local_host()
            origin = self.headers.get("Origin")
            if origin and origin.rstrip("/").casefold() != f"http://{host}".casefold():
                raise ImportRequestError("拒绝非同源的本机服务写入请求")

        def do_GET(self) -> None:  # noqa: N802
            path = urlsplit(self.path).path.rstrip("/") or "/"
            try:
                if path.startswith("/api/"):
                    self.require_local_host()
                if path == "/api/health":
                    self.send_json(200, service.health())
                    return
                if path == "/api/archives":
                    self.send_json(200, service.archives())
                    return
                if path == "/api/watch":
                    self.send_json(200, service.watch_status())
                    return
                if path.startswith("/api/imports/"):
                    job_id = path.split("/")[-1]
                    self.send_json(200, service.get_job(job_id))
                    return
            except ImportRequestError as error:
                self.api_error(error, 404)
                return
            super().do_GET()

        def do_POST(self) -> None:  # noqa: N802
            path = urlsplit(self.path).path.rstrip("/")
            try:
                self.require_same_origin()
                if self.headers.get_content_type() != "application/json":
                    raise ImportRequestError("POST 请求必须使用 application/json")
                if path == "/api/imports":
                    self.send_json(201, service.create_job(json_body(self)))
                    return
                if path == "/api/folders/select":
                    self.send_json(200, service.select_watch_folder(json_body(self)))
                    return
                if path == "/api/watch/start":
                    self.send_json(200, service.start_watch(json_body(self)))
                    return
                if path == "/api/watch/stop":
                    json_body(self)
                    self.send_json(200, service.stop_watch())
                    return
                parts = path.split("/")
                if len(parts) == 5 and parts[1:3] == ["api", "imports"] and parts[4] == "process":
                    self.send_json(202, service.start_processing(parts[3], json_body(self)))
                    return
                self.api_error(ImportRequestError("未知 API 路径"), 404)
            except ImportRequestError as error:
                self.api_error(error)
            except Exception as error:
                self.api_error(error, 500)

        def do_PUT(self) -> None:  # noqa: N802
            parsed = urlsplit(self.path)
            parts = parsed.path.rstrip("/").split("/")
            try:
                self.require_same_origin()
                if len(parts) != 5 or parts[1:3] != ["api", "imports"] or parts[4] != "file":
                    raise ImportRequestError("未知 API 路径")
                values = parse_qs(parsed.query, keep_blank_values=True)
                relative = (values.get("path") or [""])[0]
                try:
                    length = int(self.headers.get("Content-Length", "-1"))
                except ValueError as error:
                    raise ImportRequestError("Content-Length 无效") from error
                self.send_json(200, service.upload_file(parts[3], relative, self.rfile, length))
            except ImportRequestError as error:
                self.api_error(error)
            except Exception as error:
                self.api_error(error, 500)

    return Handler


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tianditu-bundle", type=Path, help="天地图 1:25万全国扁平图幅 ZIP")
    parser.add_argument("--tianditu-index", type=Path, help="天地图图幅索引 CSV；默认使用 ZIP 同目录批次索引")
    parser.add_argument(
        "--admin-boundaries-dir",
        type=Path,
        help="含中国_省/市/县.geojson 的天地图行政区划目录；默认从天地图 ZIP 旁自动查找",
    )
    parser.add_argument("--archive-root", type=Path, default=DEFAULT_ARCHIVE_ROOT, help="浏览器导入的原始输入归档目录")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument(
        "--save-dat",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="归档全部推测三分量 DAT（默认开启；用 --no-save-dat 关闭）",
    )
    parser.add_argument("--no-package", action="store_true", help="不生成每事件结果 ZIP")
    parser.add_argument("--watch-root", type=Path, help="启动后监控的地震事件根目录")
    parser.add_argument("--watch-existing", action="store_true", help="监控启动时也处理根目录内已有事件")
    parser.add_argument("--watch-stable-seconds", type=int, default=15, help="文件无变化多少秒后进入处理")
    parser.add_argument("--no-browser", action="store_true", help="启动后不自动打开浏览器")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not 1 <= args.port <= 65535:
        print("--port 必须在 1 到 65535 之间")
        return 2
    try:
        service = ArchiveService(
            archive_root=args.archive_root,
            tianditu_bundle=args.tianditu_bundle,
            tianditu_index=args.tianditu_index,
            admin_boundaries_dir=args.admin_boundaries_dir,
            save_dat=args.save_dat,
            make_package=not args.no_package,
        )
    except (OSError, ValueError) as error:
        print(f"本机服务无法启动: {error}")
        return 2

    try:
        server = ThreadingHTTPServer(("127.0.0.1", args.port), handler_class(service))
    except OSError as error:
        print(f"本机服务无法监听端口 {args.port}: {error}")
        return 2
    url = f"http://127.0.0.1:{args.port}/"
    map_status = (
        "天地图省/市/县行政区划按事件抽取"
        if service.admin_boundaries_dir
        else "全球边界回退"
    )
    print(f"网站与归档服务已启动: {url}")
    print(f"底图模式: {map_status}")
    print(f"原始输入归档: {service.archive_root}")
    if args.watch_root:
        try:
            watch = service.start_watch(
                {
                    "path": str(args.watch_root),
                    "stableSeconds": args.watch_stable_seconds,
                    "processExisting": args.watch_existing,
                }
            )
        except ImportRequestError as error:
            print(f"文件夹监控无法启动: {error}")
            server.server_close()
            return 2
        print(f"文件夹监控: {watch['root']}")
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n网站与归档服务已停止")
    finally:
        service.stop_watch()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
