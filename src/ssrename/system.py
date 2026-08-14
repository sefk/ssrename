"""macOS integration: the LaunchAgent, and the screenshot-location default."""

from __future__ import annotations

import plistlib
import subprocess
import sys
from pathlib import Path

LABEL = "com.sefk.ssrename"
PLIST_PATH = Path(f"~/Library/LaunchAgents/{LABEL}.plist").expanduser()
LOG_PATH = Path("~/Library/Logs/ssrename.log").expanduser()


def executable() -> str:
    """Path to the installed `ssrename` console script, or this interpreter."""
    candidate = Path(sys.argv[0]).resolve()
    if candidate.name == "ssrename" and candidate.exists():
        return str(candidate)
    return str(Path(sys.executable).resolve())


def plist_contents(config_path: Path | None = None) -> dict:
    exe = executable()
    args = [exe] if Path(exe).name == "ssrename" else [exe, "-m", "ssrename"]
    args.append("watch")
    if config_path:
        args += ["--config", str(config_path)]
    return {
        "Label": LABEL,
        "ProgramArguments": args,
        "RunAtLoad": True,
        "KeepAlive": True,
        "ProcessType": "Background",
        "StandardOutPath": str(LOG_PATH),
        "StandardErrorPath": str(LOG_PATH),
        "EnvironmentVariables": {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin"},
    }


def install_agent(config_path: Path | None = None) -> Path:
    PLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with PLIST_PATH.open("wb") as fh:
        plistlib.dump(plist_contents(config_path), fh)
    uid = subprocess.run(["id", "-u"], capture_output=True, text=True).stdout.strip()
    subprocess.run(["launchctl", "bootout", f"gui/{uid}/{LABEL}"], capture_output=True)
    subprocess.run(
        ["launchctl", "bootstrap", f"gui/{uid}", str(PLIST_PATH)],
        capture_output=True,
        check=True,
    )
    return PLIST_PATH


def uninstall_agent() -> bool:
    uid = subprocess.run(["id", "-u"], capture_output=True, text=True).stdout.strip()
    subprocess.run(["launchctl", "bootout", f"gui/{uid}/{LABEL}"], capture_output=True)
    if PLIST_PATH.exists():
        PLIST_PATH.unlink()
        return True
    return False


def agent_status() -> str:
    uid = subprocess.run(["id", "-u"], capture_output=True, text=True).stdout.strip()
    result = subprocess.run(
        ["launchctl", "print", f"gui/{uid}/{LABEL}"], capture_output=True, text=True
    )
    if result.returncode != 0:
        return "not loaded"
    for line in result.stdout.splitlines():
        if "state = " in line:
            return line.strip()
    return "loaded"


def screenshot_location() -> str | None:
    result = subprocess.run(
        ["defaults", "read", "com.apple.screencapture", "location"],
        capture_output=True,
        text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def set_screenshot_location(path: Path) -> None:
    path = Path(path).expanduser()
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["defaults", "write", "com.apple.screencapture", "location", str(path)],
        check=True,
    )
    subprocess.run(["killall", "SystemUIServer"], capture_output=True)
