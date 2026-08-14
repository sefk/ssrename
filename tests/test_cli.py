from pathlib import Path

import pytest

from ssrename import system
from ssrename.cli import parse_args as _parse


def test_global_flags_before_the_subcommand():
    args = _parse(["--config", "/tmp/c.toml", "--dry-run", "-v", "watch"])
    assert args.config == Path("/tmp/c.toml")
    assert args.dry_run and args.verbose


def test_global_flags_after_the_subcommand():
    # This is the order the LaunchAgent used to fail on.
    args = _parse(["watch", "--config", "/tmp/c.toml", "--dry-run", "-v"])
    assert args.config == Path("/tmp/c.toml")
    assert args.dry_run and args.verbose


@pytest.mark.parametrize(
    "command", ["watch", "doctor", "backfill", "init", "install", "uninstall"]
)
def test_every_subcommand_takes_config_on_either_side(command):
    before = _parse(["--config", "/tmp/c.toml", command])
    after = _parse([command, "--config", "/tmp/c.toml"])
    assert before.config == after.config == Path("/tmp/c.toml")


def test_subcommand_flag_does_not_clobber_the_earlier_one():
    args = _parse(["--config", "/tmp/c.toml", "watch"])
    assert args.config == Path("/tmp/c.toml")


def test_defaults():
    args = _parse(["watch"])
    assert args.config is None and not args.dry_run and not args.verbose


def test_once_keeps_its_own_flags():
    args = _parse(["once", "a.png", "b.png", "--force", "--config", "/tmp/c.toml"])
    assert args.paths == [Path("a.png"), Path("b.png")]
    assert args.force and args.config == Path("/tmp/c.toml")


def test_plist_command_parses():
    """The exact argv launchd will run must be accepted by the parser."""
    argv = system.plist_contents(Path("/tmp/c.toml"))["ProgramArguments"]
    tail = argv[1:] if Path(argv[0]).name == "ssrename" else argv[3:]
    args = _parse(tail)
    assert args.command == "watch"
    assert args.config == Path("/tmp/c.toml")


def test_plist_puts_global_flags_before_the_subcommand():
    argv = system.plist_contents(Path("/tmp/c.toml"))["ProgramArguments"]
    assert argv.index("--config") < argv.index("watch")


def test_check_args_exits_zero():
    from ssrename.cli import main

    assert main(["--config", "/tmp/c.toml", "watch", "--check-args"]) == 0
