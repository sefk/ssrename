"""macOS integration: the LaunchAgent, and the screenshot-location default."""

from __future__ import annotations

import plistlib
import subprocess
import sys
import time
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
        "EnvironmentVariables": {
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin",
            # Redirected stdout is block-buffered, which would hold back anything
            # printed until several KB accumulate. The log should be live.
            "PYTHONUNBUFFERED": "1",
        },
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


def wait_for_agent(timeout: float = 10.0) -> tuple[bool, str]:
    """Confirm the agent came up and logged something. (ok, explanation)."""
    deadline = time.monotonic() + timeout
    size_before = LOG_PATH.stat().st_size if LOG_PATH.exists() else 0
    while time.monotonic() < deadline:
        running = agent_is_running()
        grew = LOG_PATH.exists() and LOG_PATH.stat().st_size > size_before
        if running and grew:
            return True, agent_status()
        time.sleep(0.5)
    if not LOG_PATH.exists():
        return False, (
            f"no log file at {LOG_PATH} — launchd could not run the command at all. "
            f"Check: launchctl print gui/$(id -u)/{LABEL}"
        )
    if not agent_is_running():
        return False, f"agent is not running: {agent_status()}"
    return False, (
        f"agent is running but wrote nothing to {LOG_PATH} within {timeout:.0f}s"
    )


def uninstall_agent() -> bool:
    uid = subprocess.run(["id", "-u"], capture_output=True, text=True).stdout.strip()
    subprocess.run(["launchctl", "bootout", f"gui/{uid}/{LABEL}"], capture_output=True)
    if PLIST_PATH.exists():
        PLIST_PATH.unlink()
        return True
    return False


def agent_fields() -> dict[str, str] | None:
    """`launchctl print` boiled down to the few fields worth reading."""
    uid = subprocess.run(["id", "-u"], capture_output=True, text=True).stdout.strip()
    result = subprocess.run(
        ["launchctl", "print", f"gui/{uid}/{LABEL}"], capture_output=True, text=True
    )
    if result.returncode != 0:
        return None
    fields: dict[str, str] = {}
    for line in result.stdout.splitlines():
        for key in ("state", "pid", "last exit code"):
            prefix = f"{key} = "
            if line.strip().startswith(prefix) and key not in fields:
                fields[key] = line.strip()[len(prefix) :]
    return fields


def agent_status() -> str:
    fields = agent_fields()
    if fields is None:
        return "not loaded"
    state = fields.get("state", "loaded")
    if fields.get("pid"):
        return f"{state} (pid {fields['pid']})"
    exit_code = fields.get("last exit code")
    if exit_code and exit_code not in {"0", "(never exited)"}:
        return f"{state}, NOT running - last exit code {exit_code}"
    return state


def agent_is_running() -> bool:
    fields = agent_fields()
    return bool(fields and fields.get("pid"))


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


#: What the watcher logs on startup. Everything before the last one belongs to
#: an earlier run of the agent.
_STARTUP_MARKER = "watching "


def agent_log_tail(lines: int = 5) -> list[str]:
    """Recent complaints from the agent's *current* run.

    The log is append-only across restarts, so scanning all of it resurrects
    errors that were fixed long ago — a fixed startup crash would be reported
    forever. Only the tail since the last successful startup counts.

    If the agent has never started successfully there is no marker, and then the
    whole log is fair game: that is exactly the case worth reporting, since the
    failure will be the argument or import error that stopped it booting.
    """
    if not LOG_PATH.exists():
        return []
    try:
        content = LOG_PATH.read_text(errors="replace").splitlines()
    except OSError:
        return []
    for i in range(len(content) - 1, -1, -1):
        if _STARTUP_MARKER in content[i]:
            content = content[i + 1 :]
            break
    bad = [ln for ln in content if "ERROR" in ln or "error:" in ln or "Traceback" in ln]
    return bad[-lines:]


#: Preference keys holding the screenshot destination, newest first.
#:
#: macOS 27 split the single `location` key into per-capture-type keys —
#: `/usr/sbin/screencapture` reads `location-screenshot` for stills and
#: `location-screenrecording` for video, mirroring the existing
#: `target-screenshot`/`target-screenrecording` split. Writing only the legacy
#: `location` key leaves screenshots going to ~/Desktop on macOS 27, with no
#: error: the binary just falls back to its built-in default.
#:
#: We write all of them, and read them in this order. Older macOS ignores the
#: keys it does not know, so a single code path covers both.
SCREENSHOT_LOCATION_KEYS = ("location-screenshot", "location")


def screenshot_location() -> str | None:
    for key in SCREENSHOT_LOCATION_KEYS:
        result = subprocess.run(
            ["defaults", "read", "com.apple.screencapture", key],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    return None


def set_screenshot_location(path: Path) -> None:
    path = Path(path).expanduser()
    path.mkdir(parents=True, exist_ok=True)
    for key in SCREENSHOT_LOCATION_KEYS:
        subprocess.run(
            ["defaults", "write", "com.apple.screencapture", key, str(path)],
            check=True,
        )
    # macOS spawns a fresh `screencapture` per hotkey press, so the next
    # screenshot picks this up without restarting anything.
