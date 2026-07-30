#!/usr/bin/env python3
"""Launch the packaged Windows batch entrypoint and verify its HTTP surface."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from urllib.error import URLError
from urllib.request import urlopen


def get_bytes(url: str, timeout: float = 5.0) -> bytes:
    with urlopen(url, timeout=timeout) as response:
        if response.status != 200:
            raise RuntimeError(f"{url} returned HTTP {response.status}")
        return response.read()


def get_json(url: str) -> dict:
    return json.loads(get_bytes(url).decode("utf-8"))


def wait_for_health(base_url: str, process: subprocess.Popen[str], timeout: int) -> dict:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"server exited early with code {process.returncode}")
        try:
            payload = get_json(f"{base_url}/api/health")
            if payload.get("status") == "ok":
                return payload
        except (OSError, URLError, ValueError, RuntimeError) as error:
            last_error = error
        time.sleep(1)
    raise TimeoutError(f"server did not become healthy: {last_error}")


def terminate_tree(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    subprocess.run(
        ["taskkill", "/PID", str(process.pid), "/T", "/F"],
        check=False,
        capture_output=True,
        text=True,
    )


def run(bundle: Path, port: int, log_dir: Path, timeout: int) -> dict[str, object]:
    if sys.platform != "win32":
        raise RuntimeError("run_windows_smoke.py must run on Windows")
    bundle = bundle.resolve()
    start_script = bundle / "start_windows.bat"
    if not start_script.is_file():
        raise FileNotFoundError(start_script)

    log_dir.mkdir(parents=True, exist_ok=True)
    stdout_path = log_dir / "server.stdout.log"
    stderr_path = log_dir / "server.stderr.log"
    environment = os.environ.copy()
    environment["ZSL_PYTHON"] = sys.executable
    environment["ZSL_NO_BROWSER"] = "1"
    base_url = f"http://127.0.0.1:{port}"
    launcher = log_dir / "launch-smoke.bat"
    launcher.write_text(
        f'@echo off\r\ncall "{start_script}" "" "{port}"\r\n',
        encoding="utf-8",
        newline="",
    )

    with stdout_path.open("w", encoding="utf-8") as stdout, stderr_path.open(
        "w", encoding="utf-8"
    ) as stderr:
        process = subprocess.Popen(
            ["cmd.exe", "/d", "/c", str(launcher)],
            cwd=bundle,
            env=environment,
            stdout=stdout,
            stderr=stderr,
            text=True,
        )
        try:
            health = wait_for_health(base_url, process, timeout)
            index = get_bytes(f"{base_url}/").decode("utf-8")
            app_js = get_bytes(f"{base_url}/app.js").decode("utf-8")
            event_catalog = get_json(f"{base_url}/demo/events/catalog.json")
            batch_catalog = get_json(f"{base_url}/demo/batch/catalog.json")
            event = get_json(f"{base_url}/demo/knet-demo.json")
            boundary = get_json(f"{base_url}/demo/world-land.geojson")
            watch = get_json(f"{base_url}/api/watch")

            assert "震·时澜" in index
            assert "/api/health" in app_js
            assert health["restartRequired"] is False
            assert health["administrativeBasemapConfigured"] is True
            assert set(health["acceptedFolders"]) == {"EIData", "HNdata"}
            assert event_catalog["eventCount"] == 1
            assert len(event_catalog["events"]) == 1
            assert batch_catalog["eventCount"] == 0
            assert event["eventId"] == "0010061330"
            assert event.get("distanceBinStatistics")
            assert boundary["type"] == "FeatureCollection"
            assert watch["status"] in {"idle", "stopped"}
            result = {
                "status": "ok",
                "platform": sys.platform,
                "python": sys.version.split()[0],
                "baseUrl": base_url,
                "health": health,
                "eventId": event["eventId"],
                "eventCount": event_catalog["eventCount"],
                "batchEventCount": batch_catalog["eventCount"],
            }
            (log_dir / "smoke-result.json").write_text(
                json.dumps(result, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            return result
        finally:
            terminate_tree(process)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--log-dir", type=Path, required=True)
    parser.add_argument("--timeout", type=int, default=90)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    result = run(args.bundle, args.port, args.log_dir, args.timeout)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
