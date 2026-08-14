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
    # Global flags go before the subcommand.
    if config_path:
        args += ["--config", str(config_path)]
    args.append("watch")
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


def dry_run_agent_command(argv: list[str]) -> str | None:
    """Run the plist's command with --check-args. None means it is usable."""
    try:
        result = subprocess.run(
            [*argv, "--check-args"], capture_output=True, text=True, timeout=60
        )
    except (OSError, subprocess.SubprocessError) as e:
        return f"{argv[0]}: {e}"
    if result.returncode == 0:
        return None
    return (result.stderr or result.stdout).strip()[:600]


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


#: Directories macOS puts behind TCC, relative to the home directory.
PROTECTED_DIRS = ("Desktop", "Documents", "Downloads")


def is_protected(path: Path) -> bool:
    """True if macOS gates this path behind a privacy permission."""
    try:
        relative = Path(path).expanduser().resolve().relative_to(Path.home().resolve())
    except ValueError:
        return False
    return bool(relative.parts) and relative.parts[0] in PROTECTED_DIRS


def tcc_binary() -> Path:
    """The executable macOS actually judges — the interpreter, not the script.

    A console script is a text file with a shebang; TCC grants apply to the
    binary that ends up running, so this is what has to appear in the Full Disk
    Access list.
    """
    return Path(sys.executable).resolve()


def check_read_access(directory: Path) -> tuple[bool, str]:
    """Try what the watcher does: list the directory and open a file in it."""
    directory = Path(directory)
    try:
        entries = list(directory.iterdir())
    except PermissionError as e:
        return False, f"cannot list ({e.strerror})"
    except OSError as e:
        return False, f"cannot list ({e.strerror})"
    files = [e for e in entries if e.is_file() and not e.name.startswith(".")]
    if not files:
        return True, f"listed {len(entries)} entries (no file to open)"
    try:
        with files[0].open("rb") as fh:
            fh.read(1)
    except PermissionError as e:
        return False, f"can list but cannot read files ({e.strerror})"
    except OSError as e:
        return False, f"cannot read {files[0].name} ({e.strerror})"
    return True, f"listed {len(entries)} entries, read {files[0].name}"


def agent_log_tail(lines: int = 5) -> list[str]:
    """Recent complaints from the agent, if it has been running."""
    if not LOG_PATH.exists():
        return []
    try:
        content = LOG_PATH.read_text(errors="replace").splitlines()
    except OSError:
        return []
    bad = [ln for ln in content if "ERROR" in ln or "error:" in ln or "Traceback" in ln]
    return bad[-lines:]


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
