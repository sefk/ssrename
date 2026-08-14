"""Command line interface."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from . import __version__, system
from .backends import BackendError, make_backend
from .config import DEFAULT_CONFIG_PATH, load_config, write_default_config
from .renamer import Renamer
from .watcher import Watcher

log = logging.getLogger("ssrename")


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def _renamer(args) -> Renamer:
    cfg = load_config(args.config)
    return Renamer(cfg, make_backend(cfg), dry_run=args.dry_run)


def cmd_watch(args) -> int:
    Watcher(_renamer(args)).run()
    return 0


def cmd_once(args) -> int:
    renamer = _renamer(args)
    renamer.pattern = _maybe_relax(renamer, args)
    failures = 0
    for raw in args.paths:
        result = renamer.process(Path(raw).expanduser())
        if result.error:
            print(f"error  {result.source.name}: {result.error}", file=sys.stderr)
            failures += 1
        elif result.dest and result.skipped == "dry run":
            print(f"would  {result.source.name} -> {result.dest.name}")
        elif result.skipped:
            print(f"skip   {result.source.name}: {result.skipped}")
        else:
            print(f"rename {result.source.name} -> {result.dest.name}")
    return 1 if failures else 0


def cmd_backfill(args) -> int:
    renamer = _renamer(args)
    cfg = renamer.cfg
    files = sorted(p for p in cfg.watch_dir.iterdir() if renamer.is_candidate(p))
    if args.limit:
        files = files[: args.limit]
    if not files:
        print(f"nothing to rename in {cfg.watch_dir}")
        return 0
    print(f"{len(files)} file(s) in {cfg.watch_dir}")
    args.paths = files
    return cmd_once(args)


def _maybe_relax(renamer: Renamer, args):
    """`once` on an explicitly named file should work even if already renamed."""
    import re

    if getattr(args, "force", False):
        return re.compile("")
    return renamer.pattern


def cmd_doctor(args) -> int:
    cfg = load_config(args.config)
    ok = True
    print(f"ssrename {__version__}")
    print(f"config:            {cfg.source} {'(exists)' if cfg.source.exists() else '(defaults; run `ssrename init`)'}")
    print(f"watch dir:         {cfg.watch_dir} {'' if cfg.watch_dir.exists() else '(missing)'}")

    location = system.screenshot_location()
    if location is None:
        print("screenshot dir:    not set (macOS default: ~/Desktop)  <- run `ssrename set-screenshot-dir`")
        ok = False
    else:
        match = Path(location).expanduser().resolve() == cfg.watch_dir.resolve()
        print(f"screenshot dir:    {location} {'' if match else '<- does not match watch_dir'}")
        ok = ok and match

    try:
        print(f"backend:           {make_backend(cfg).check()}")
    except BackendError as e:
        print(f"backend:           FAILED - {e}")
        ok = False

    print(f"launch agent:      {system.agent_status()}")
    print(f"log:               {system.LOG_PATH}")
    return 0 if ok else 1


def cmd_init(args) -> int:
    path = write_default_config(args.config, force=args.force)
    print(f"config at {path}")
    return 0


def cmd_install(args) -> int:
    cfg_path = args.config or (DEFAULT_CONFIG_PATH if DEFAULT_CONFIG_PATH.exists() else None)
    path = system.install_agent(Path(cfg_path) if cfg_path else None)
    print(f"installed and started {path}")
    print(f"logs: {system.LOG_PATH}")
    print(
        "If nothing gets renamed, grant Full Disk Access to the interpreter in "
        "the plist (System Settings > Privacy & Security) — launchd agents "
        "cannot prompt for Desktop access."
    )
    return 0


def cmd_uninstall(args) -> int:
    print("removed" if system.uninstall_agent() else "was not installed")
    return 0


def cmd_set_screenshot_dir(args) -> int:
    cfg = load_config(args.config)
    target = Path(args.path).expanduser() if args.path else cfg.watch_dir
    system.set_screenshot_location(target)
    print(f"macOS will now save screenshots to {target}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ssrename", description=__doc__)
    p.add_argument("--config", type=Path, help=f"config file (default {DEFAULT_CONFIG_PATH})")
    p.add_argument("--dry-run", action="store_true", help="say what would happen, change nothing")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("watch", help="watch the screenshot directory (foreground)").set_defaults(func=cmd_watch)

    once = sub.add_parser("once", help="rename specific files now")
    once.add_argument("paths", nargs="+", type=Path)
    once.add_argument("--force", action="store_true", help="ignore filename_pattern")
    once.set_defaults(func=cmd_once)

    backfill = sub.add_parser("backfill", help="rename existing screenshots in the watch dir")
    backfill.add_argument("--limit", type=int, default=0)
    backfill.set_defaults(func=cmd_backfill)

    sub.add_parser("doctor", help="check config, backend, and macOS settings").set_defaults(func=cmd_doctor)

    init = sub.add_parser("init", help="write a default config file")
    init.add_argument("--force", action="store_true")
    init.set_defaults(func=cmd_init)

    sub.add_parser("install", help="install and start the LaunchAgent").set_defaults(func=cmd_install)
    sub.add_parser("uninstall", help="stop and remove the LaunchAgent").set_defaults(func=cmd_uninstall)

    ssd = sub.add_parser("set-screenshot-dir", help="point macOS screenshots at the watch dir")
    ssd.add_argument("path", nargs="?", type=Path)
    ssd.set_defaults(func=cmd_set_screenshot_dir)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _setup_logging(args.verbose)
    try:
        return args.func(args)
    except BackendError as e:
        print(f"backend error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
