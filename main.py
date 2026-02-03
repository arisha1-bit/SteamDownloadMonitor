

from __future__ import annotations

import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Tuple

IS_WINDOWS = os.name == "nt"

def _reg_get_value(root, path: str, name: str) -> Optional[str]:
    try:
        import winreg
        with winreg.OpenKey(root, path) as k:
            val, _ = winreg.QueryValueEx(k, name)
            if isinstance(val, str) and val:
                return val
    except Exception:
        return None
    return None

def find_steam_path() -> Optional[Path]:
    """
    Steam typically stores path in:
      HKCU\Software\Valve\Steam  SteamPath
    or:
      HKLM\SOFTWARE\WOW6432Node\Valve\Steam  InstallPath
    """
    if not IS_WINDOWS:
        return None

    try:
        import winreg
        candidates = [
            (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam", "InstallPath"),
        ]
        for root, key, name in candidates:
            v = _reg_get_value(root, key, name)
            if v:
                p = Path(v).expanduser()
                if p.exists():
                    return p
    except Exception:
        pass
    return None

_ACF_KV_RE = re.compile(r'^\s*"([^"]+)"\s*"([^"]*)"\s*$')

def parse_acf(path: Path) -> Dict[str, str]:
    """
    Minimal ACF parser: extracts key/value pairs we care about (name, StateFlags, etc).
    """
    data: Dict[str, str] = {}
    if not path.exists():
        return data

    stack = []
    try:
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line or line.startswith("//"):
                continue
            if line == "{":
                stack.append("{")
                continue
            if line == "}":
                if stack:
                    stack.pop()
                continue
            m = _ACF_KV_RE.match(line)
            if m and len(stack) >= 1:
                k, v = m.group(1), m.group(2)
                data.setdefault(k, v)
    except Exception:
        return data
    return data

@dataclass
class ActiveDownload:
    appid: str
    name: str
    state: str
    download_dir: Path
    bytes_on_disk: int

def dir_size_bytes(path: Path) -> int:
    total = 0
    try:
        for root, _, files in os.walk(path):
            for f in files:
                fp = Path(root) / f
                try:
                    total += fp.stat().st_size
                except Exception:
                    pass
    except Exception:
        return 0
    return total

def find_active_download(steamapps: Path) -> Optional[ActiveDownload]:
    downloading = steamapps / "downloading"
    if not downloading.exists():
        return None

    candidates: list[Tuple[float, Path]] = []
    for p in downloading.iterdir():
        if p.is_dir() and p.name.isdigit():
            try:
                mtime = p.stat().st_mtime
                candidates.append((mtime, p))
            except Exception:
                pass

    if not candidates:
        return None

    candidates.sort(reverse=True, key=lambda x: x[0])
    for _, app_dir in candidates[:5]:
        size_b = dir_size_bytes(app_dir)
        if size_b > 0 or (time.time() - app_dir.stat().st_mtime) < 10 * 60:
            appid = app_dir.name
            manifest = steamapps / f"appmanifest_{appid}.acf"
            info = parse_acf(manifest)
            name = info.get("name") or f"AppID {appid}"
            state = "downloading"
            return ActiveDownload(appid=appid, name=name, state=state, download_dir=app_dir, bytes_on_disk=size_b)

    return None

def human_rate(bytes_per_sec: float) -> str:
    mb_s = bytes_per_sec / (1024 * 1024)
    mbit_s = (bytes_per_sec * 8) / (1024 * 1024)
    return f"{mb_s:.2f} MB/s ({mbit_s:.2f} Mbit/s)"

def fmt_bytes(n: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    x = float(n)
    i = 0
    while x >= 1024 and i < len(units)-1:
        x /= 1024.0
        i += 1
    return f"{x:.2f} {units[i]}"

def detach_to_background(argv: list[str]) -> None:
    """
    Relaunch as a detached process on Windows so it "runs in background".
    """
    if not IS_WINDOWS:
        return
    import subprocess
    creationflags = 0
    creationflags |= subprocess.DETACHED_PROCESS
    creationflags |= subprocess.CREATE_NEW_PROCESS_GROUP
    creationflags |= subprocess.CREATE_NO_WINDOW
    subprocess.Popen(argv, close_fds=True, creationflags=creationflags)

def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Track Steam download speed (best-effort).")
    parser.add_argument("--minutes", type=int, default=5, help="How many minutes to report (default: 5).")
    parser.add_argument("--interval", type=int, default=60, help="Seconds between reports (default: 60).")
    parser.add_argument("--background", action="store_true", help="Relaunch detached (Windows).")
    args = parser.parse_args()

    if args.background:
        new_argv = [sys.executable] + [a for a in sys.argv if a != "--background"]
        detach_to_background(new_argv)
        return 0

    steam_path = find_steam_path()
    if not steam_path:
        print("Steam path not found via registry. This script is intended for Windows Steam.")
        return 2

    steamapps = steam_path / "steamapps"
    if not steamapps.exists():
        print(f"steamapps folder not found at: {steamapps}")
        return 3

    print(f"[{datetime.now().strftime('%H:%M:%S')}] Steam found at: {steam_path}")
    print("Monitoring download speed...")

    prev: Dict[str, int] = {}
    prev_time: Dict[str, float] = {}

    loops = max(1, args.minutes)
    for i in range(loops):
        now = time.time()
        active = find_active_download(steamapps)

        ts = datetime.now().strftime("%H:%M:%S")
        if not active:
            print(f"[{ts}] No active downloads detected.")
        else:
            last_b = prev.get(active.appid, active.bytes_on_disk)
            last_t = prev_time.get(active.appid, now - args.interval)
            dt = max(1.0, now - last_t)
            delta = max(0, active.bytes_on_disk - last_b)
            rate = delta / dt

            status = "downloading"
            if delta < 256 * 1024:
                status = "paused or idle"

            print(
                f"[{ts}] {active.name} (AppID {active.appid}) - {status} - "
                f"speed {human_rate(rate)} - temp {fmt_bytes(active.bytes_on_disk)}"
            )

            prev[active.appid] = active.bytes_on_disk
            prev_time[active.appid] = now

        if i < loops - 1:
            time.sleep(args.interval)

    return 0

if __name__ == "__main__":
    raise SystemExit(main())
