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

    readable, detail = system.check_read_access(cfg.watch_dir)
    print(f"read access:       {'ok' if readable else 'DENIED'} - {detail}")
    ok = ok and readable

    try:
        print(f"backend:           {make_backend(cfg).check()}")
    except BackendError as e:
        print(f"backend:           FAILED - {e}")
        ok = False

    status = system.agent_status()
    print(f"launch agent:      {status}")
    print(f"log:               {system.LOG_PATH}")
    for line in system.agent_log_tail():
        print(f"  recent error:    {line}")

    protected = system.is_protected(cfg.watch_dir)
    binary = system.tcc_binary()
    if protected:
        print()
        print(f"{cfg.watch_dir} is a privacy-protected location, so whatever runs")
        print("ssrename needs Full Disk Access. Grant it to exactly this binary:")
        print()
        print(f"    {binary}")
        print()
        print("  System Settings > Privacy & Security > Full Disk Access > +,")
        print("  then press Cmd-Shift-G in the file picker and paste that path.")
        if not readable:
            print("  (Read access is failing right now, so this is the likely cause.)")
        elif status == "not loaded":
            print("  This check inherits the permissions of the app running it, so")
            print("  passing here does not prove the LaunchAgent will pass. Install")
            print("  the agent, take a screenshot, and re-run doctor.")
    return 0 if ok else 1


def cmd_init(args) -> int:
    path = write_default_config(args.config, force=args.force)
    print(f"config at {path}")
    return 0


def cmd_install(args) -> int:
    cfg_path = args.config or (DEFAULT_CONFIG_PATH if DEFAULT_CONFIG_PATH.exists() else None)
    # launchd's KeepAlive turns any startup failure into a respawn loop, so make
    # sure the exact command going into the plist actually runs first.
    plist = system.plist_contents(Path(cfg_path) if cfg_path else None)
    probe = system.dry_run_agent_command(plist["ProgramArguments"])
    if probe is not None:
        print(f"refusing to install: the agent command fails\n{probe}", file=sys.stderr)
        return 1
    path = system.install_agent(Path(cfg_path) if cfg_path else None)
    print(f"installed and started {path}")
    print(f"logs: {system.LOG_PATH}")
    cfg = load_config(args.config)
    if system.is_protected(cfg.watch_dir):
        print()
        print(f"{cfg.watch_dir} is privacy-protected and a LaunchAgent cannot show")
        print("a permission prompt. Grant Full Disk Access to this binary:")
        print()
        print(f"    {system.tcc_binary()}")
        print()
        print("Then take a screenshot and run `ssrename doctor`.")
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


def _global_flags() -> argparse.ArgumentParser:
    """Flags accepted on either side of the subcommand.

    SUPPRESS keeps an unused subcommand-side flag from overwriting the value
    already parsed before the subcommand.
    """
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--config",
        type=Path,
        default=argparse.SUPPRESS,
        help=f"config file (default {DEFAULT_CONFIG_PATH})",
    )
    common.add_argument(
        "--dry-run",
        action="store_true",
        default=argparse.SUPPRESS,
        help="say what would happen, change nothing",
    )
    common.add_argument(
        "-v", "--verbose", action="store_true", default=argparse.SUPPRESS
    )
    # Parse and exit 0, so `install` can prove the plist's command works before
    # handing it to launchd.
    common.add_argument(
        "--check-args", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS
    )
    return common


def build_parser() -> argparse.ArgumentParser:
    common = _global_flags()
    p = argparse.ArgumentParser(prog="ssrename", description=__doc__, parents=[common])
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser(
        "watch", parents=[common], help="watch the screenshot directory (foreground)"
    ).set_defaults(func=cmd_watch)

    once = sub.add_parser("once", parents=[common], help="rename specific files now")
    once.add_argument("paths", nargs="+", type=Path)
    once.add_argument("--force", action="store_true", help="ignore filename_pattern")
    once.set_defaults(func=cmd_once)

    backfill = sub.add_parser(
        "backfill", parents=[common], help="rename existing screenshots in the watch dir"
    )
    backfill.add_argument("--limit", type=int, default=0)
    backfill.set_defaults(func=cmd_backfill)

    sub.add_parser(
        "doctor", parents=[common], help="check config, backend, and macOS settings"
    ).set_defaults(func=cmd_doctor)

    init = sub.add_parser("init", parents=[common], help="write a default config file")
    init.add_argument("--force", action="store_true")
    init.set_defaults(func=cmd_init)

    sub.add_parser(
        "install", parents=[common], help="install and start the LaunchAgent"
    ).set_defaults(func=cmd_install)
    sub.add_parser(
        "uninstall", parents=[common], help="stop and remove the LaunchAgent"
    ).set_defaults(func=cmd_uninstall)

    ssd = sub.add_parser(
        "set-screenshot-dir",
        parents=[common],
        help="point macOS screenshots at the watch dir",
    )
    ssd.add_argument("path", nargs="?", type=Path)
    ssd.set_defaults(func=cmd_set_screenshot_dir)
    return p


GLOBAL_DEFAULTS = {"config": None, "dry_run": False, "verbose": False, "check_args": False}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse, then fill in the global flags nobody passed.

    They default to SUPPRESS so that a subparser copy cannot overwrite a value
    given before the subcommand — `parents=` shares the action objects, so a real
    default on either side would land on both.
    """
    args = build_parser().parse_args(argv)
    for name, default in GLOBAL_DEFAULTS.items():
        if not hasattr(args, name):
            setattr(args, name, default)
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.check_args:
        return 0
    _setup_logging(args.verbose)
    try:
        return args.func(args)
    except BackendError as e:
        print(f"backend error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
