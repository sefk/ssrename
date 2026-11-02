"""Sync between two "machines" that are directories under tmp_path.

The transfers are real `rsync`; only ssh is faked, by a shim that treats the
host name as a directory under $FAKESSH_ROOT and runs the command there. That
keeps the exact remote commands and rsync flags under test.
"""

import os
import shutil
import stat
import threading
import time
from pathlib import Path

import pytest

from ssrename.config import Config, SyncConfig, load_config
from ssrename.sync import (
    REMOTE_STATE_PATH,
    PeerUnreachable,
    SyncError,
    Syncer,
    SyncState,
    remote_dir_for,
)
from ssrename.watcher import _Handler

pytestmark = pytest.mark.skipif(shutil.which("rsync") is None, reason="needs rsync")

FAKESSH = """#!/bin/sh
# ssh stand-in: skip options, treat the host as a directory, run the command there.
while [ $# -gt 0 ]; do
  case "$1" in
    -o|-p|-l|-i|-F) shift 2 ;;
    -*) shift ;;
    *) host="$1"; shift; break ;;
  esac
done
cd "$FAKESSH_ROOT/$host" || exit 255
exec sh -c "$*"
"""

DOWN_SSH = """#!/bin/sh
echo "ssh: connect to host laptop port 22: Operation timed out" >&2
exit 255
"""


def _script(path: Path, body: str) -> Path:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


class Machine:
    """One side: a home directory with a screenshot folder and sync state."""

    def __init__(self, root: Path, name: str, peer: str, ssh: Path):
        self.home = root / name
        self.shots = self.home / "Pictures" / "Screenshots"
        self.shots.mkdir(parents=True)
        self.trash = self.home / ".Trash"
        cfg = Config(
            watch_dir=self.shots,
            sync=SyncConfig(
                peer=peer, remote_dir="Pictures/Screenshots", ssh_command=str(ssh), identity_file=""
            ),
        )
        self.syncer = Syncer(
            cfg, state_path=self.home / REMOTE_STATE_PATH, trash_dir=self.trash
        )

    def add(self, name: str, data: bytes = b"png") -> Path:
        p = self.shots / name
        p.write_bytes(data)
        return p

    def names(self) -> set[str]:
        return {p.name for p in self.shots.iterdir()}

    def state(self) -> SyncState:
        return SyncState.load(self.syncer.state_path)

    def sweep(self, **kw):
        return self.syncer.sweep(**kw)


@pytest.fixture
def pair(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKESSH_ROOT", str(tmp_path))
    ssh = _script(tmp_path / "fakessh", FAKESSH)
    return Machine(tmp_path, "studio", "laptop", ssh), Machine(tmp_path, "laptop", "studio", ssh)


def test_first_sweep_merges_both_ways(pair):
    studio, laptop = pair
    studio.add("2026-11-01-09-00-github-pull-request.png")
    laptop.add("2026-11-01-10-00-slack-thread.png")
    report = studio.sweep()
    assert report.sent == ["2026-11-01-09-00-github-pull-request.png"]
    assert report.received == ["2026-11-01-10-00-slack-thread.png"]
    assert studio.names() == laptop.names() == {
        "2026-11-01-09-00-github-pull-request.png",
        "2026-11-01-10-00-slack-thread.png",
    }
    assert not laptop.sweep().changed


def test_only_renamed_images_are_synced(pair):
    studio, laptop = pair
    studio.add("Screenshot 2026-11-01 at 9.00.00 AM.png")  # not renamed yet
    studio.add("Screen Recording 2026-11-01 at 9.00.00 AM.mov")
    studio.add("2026-11-01-09-00-demo-recording.mov")
    studio.add(".hidden.png")
    studio.add("notes.txt")
    studio.add("IMG_9471.PNG")  # an AirDropped iPhone screenshot, not renamed yet
    studio.add("photo.heic")
    studio.sweep()
    assert laptop.names() == {"photo.heic"}


def test_existing_files_are_never_overwritten(pair):
    studio, laptop = pair
    studio.add("same-name.png", b"studio")
    laptop.add("same-name.png", b"laptop")
    studio.sweep()
    laptop.sweep()
    assert (studio.shots / "same-name.png").read_bytes() == b"studio"
    assert (laptop.shots / "same-name.png").read_bytes() == b"laptop"


def test_deletion_propagates_to_the_trash_and_is_not_pulled_back(pair):
    studio, laptop = pair
    studio.add("keep.png")
    studio.add("delete-me.png")
    studio.sweep()
    laptop.sweep()  # laptop now knows both
    time.sleep(0.01)  # deletion strictly after the laptop's copy arrived

    (studio.shots / "delete-me.png").unlink()
    report = studio.sweep()
    assert report.deleted == ["delete-me.png"]
    assert report.received == []  # the laptop's copy does not come back
    assert "delete-me.png" not in studio.names()

    report = laptop.sweep()
    assert report.trashed == ["delete-me.png"]
    assert laptop.names() == {"keep.png"}
    assert (laptop.trash / "delete-me.png").exists()  # trashed, not destroyed
    assert not studio.sweep().changed
    assert not laptop.sweep().changed


def test_peer_sweeping_first_does_not_undo_a_deletion(pair):
    """Codex review: the peer swept before the deleting side recorded its
    tombstone, and sent its stale copy straight back."""
    studio, laptop = pair
    studio.add("gone.png")
    studio.sweep()
    laptop.sweep()
    (studio.shots / "gone.png").unlink()
    time.sleep(0.01)
    assert laptop.sweep().sent == []
    assert "gone.png" not in studio.names()
    studio.sweep()
    laptop.sweep()
    assert "gone.png" not in laptop.names()
    assert "gone.png" in studio.state().tombstones


def test_deletion_during_a_sweep_is_still_recorded(pair, monkeypatch):
    """Codex review: a file deleted while transfers ran dropped out of `known`
    without a tombstone, so the peer's copy came back later."""
    studio, laptop = pair
    studio.add("mid.png")
    studio.sweep()
    laptop.sweep()
    laptop.add("new.png")  # gives the next sweep a transfer to interrupt
    real = studio.syncer._rsync

    def rsync_then_delete(names, source, dest):
        real(names, source, dest)
        (studio.shots / "mid.png").unlink(missing_ok=True)

    monkeypatch.setattr(studio.syncer, "_rsync", rsync_then_delete)
    studio.sweep()
    monkeypatch.setattr(studio.syncer, "_rsync", real)
    assert "mid.png" in studio.state().known
    assert "new.png" in studio.state().known
    time.sleep(0.01)
    assert studio.sweep().deleted == ["mid.png"]
    laptop.sweep()
    assert "mid.png" not in laptop.names()


def test_a_restored_file_beats_the_older_tombstone(pair):
    studio, laptop = pair
    studio.add("oops.png")
    studio.sweep()
    laptop.sweep()
    time.sleep(0.01)
    (studio.shots / "oops.png").unlink()
    studio.sweep()  # tombstone recorded
    laptop.sweep()  # laptop trashes its copy
    time.sleep(0.01)

    shutil.move(laptop.trash / "oops.png", laptop.shots / "oops.png")  # put back
    report = laptop.sweep()
    assert report.trashed == []
    assert report.sent == ["oops.png"]
    studio.sweep()
    assert "oops.png" in studio.names()
    assert "oops.png" not in studio.state().tombstones


def test_a_user_rename_propagates(pair):
    studio, laptop = pair
    studio.add("2026-11-01-09-00-bad-name.png")
    studio.sweep()
    laptop.sweep()
    time.sleep(0.01)
    (studio.shots / "2026-11-01-09-00-bad-name.png").rename(
        studio.shots / "2026-11-01-09-00-good-name.png"
    )
    studio.sweep()
    laptop.sweep()
    assert laptop.names() == {"2026-11-01-09-00-good-name.png"}


def test_mass_disappearance_is_refused(pair):
    studio, laptop = pair
    for i in range(12):
        studio.add(f"shot-{i:02}.png")
    studio.sweep()
    for p in list(studio.shots.iterdir()):
        p.unlink()
    with pytest.raises(SyncError, match="vanished"):
        studio.sweep()
    assert studio.state().tombstones == {}
    laptop.sweep()
    assert len(laptop.names()) == 12  # nothing trashed on the peer

    # Codex review: the documented override used to be "delete the state file",
    # which pulled everything back instead.
    time.sleep(0.01)
    report = studio.sweep(accept_deletions=True)
    assert len(report.deleted) == 12 and report.received == []
    laptop.sweep()
    assert laptop.names() == set()


def test_unreachable_peer_still_records_local_deletions(pair, tmp_path):
    studio, laptop = pair
    studio.add("gone.png")
    studio.sweep()
    (studio.shots / "gone.png").unlink()
    studio.syncer.cfg.sync.ssh_command = str(_script(tmp_path / "downssh", DOWN_SSH))
    with pytest.raises(PeerUnreachable):
        studio.sweep()
    assert "gone.png" in studio.state().tombstones


def test_bad_auth_is_an_error_not_unreachable(pair, tmp_path):
    studio, _ = pair
    studio.syncer.cfg.sync.ssh_command = str(
        _script(tmp_path / "authssh", "#!/bin/sh\necho 'laptop: Permission denied (publickey).' >&2\nexit 255\n")
    )
    with pytest.raises(SyncError) as e:
        studio.sweep()
    assert not isinstance(e.value, PeerUnreachable)


def test_missing_remote_dir_is_an_error(pair):
    studio, laptop = pair
    shutil.rmtree(laptop.shots)
    with pytest.raises(SyncError, match="cannot list"):
        studio.sweep()


def test_dry_run_changes_nothing(pair):
    studio, laptop = pair
    studio.add("a.png")
    laptop.add("b.png")
    studio.syncer.dry_run = True
    report = studio.sweep()
    assert report.sent == ["a.png"] and report.received == ["b.png"]
    assert studio.names() == {"a.png"} and laptop.names() == {"b.png"}
    assert not studio.syncer.state_path.exists()


def test_run_once_logs_unreachable_once(pair, tmp_path, caplog):
    studio, _ = pair
    studio.syncer.cfg.sync.ssh_command = str(_script(tmp_path / "downssh", DOWN_SSH))
    caplog.set_level("DEBUG", logger="ssrename")
    for _ in range(3):
        studio.syncer.run_once()
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    assert not [r for r in caplog.records if r.levelname == "ERROR"]


def test_background_loop_sweeps_on_start(pair):
    studio, laptop = pair
    studio.add("a.png")
    stop = threading.Event()
    t = threading.Thread(target=studio.syncer.run, args=(stop,))
    t.start()
    try:
        deadline = time.monotonic() + 10
        while "a.png" not in laptop.names() and time.monotonic() < deadline:
            time.sleep(0.05)
    finally:
        stop.set()
        studio.syncer._poked.set()
        t.join(5)
    assert "a.png" in laptop.names()


def test_handler_pokes_sync_on_create_move_and_delete(tmp_path):
    seen = []

    class Ev:
        def __init__(self, src, dest=""):
            self.src_path, self.dest_path, self.is_directory = src, dest, False

    h = _Handler(lambda p: None, seen.append)
    h.on_created(Ev(str(tmp_path / "a.png")))
    h.on_moved(Ev(str(tmp_path / "b.png"), str(tmp_path / "c.png")))
    h.on_deleted(Ev(str(tmp_path / "d.png")))
    assert [p.name for p in seen] == ["a.png", "b.png", "c.png", "d.png"]


def test_remote_dir_defaults_to_watch_dir_under_home():
    cfg = Config(watch_dir=Path.home() / "Pictures" / "Screenshots")
    assert remote_dir_for(cfg) == "Pictures/Screenshots"
    cfg.sync.remote_dir = "~/Desktop/shots"
    assert remote_dir_for(cfg) == "Desktop/shots"
    cfg = Config(watch_dir=Path("/Volumes/x/shots"))
    assert remote_dir_for(cfg) == "/Volumes/x/shots"


def test_sync_config_loads(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[sync]\npeer = "laptop"\ninterval_seconds = 60\nextensions = [".PNG"]\n')
    cfg = load_config(path)
    assert cfg.sync.peer == "laptop"
    assert cfg.sync.interval_seconds == 60
    assert cfg.sync.extensions == ("png",)
    assert load_config(tmp_path / "nope.toml").sync.peer == ""


def test_first_sweep_says_up_to_date_once(pair, caplog):
    studio, _ = pair
    caplog.set_level("INFO", logger="ssrename")
    studio.syncer.run_once()
    studio.syncer.run_once()
    assert [r.getMessage() for r in caplog.records] == ["sync with laptop: up to date"]


def test_ssh_uses_only_the_sync_key_when_it_exists(tmp_path):
    key = tmp_path / "ssrename_ed25519"
    cfg = Config(watch_dir=tmp_path, sync=SyncConfig(peer="p", identity_file=str(key)))
    assert "-i" not in Syncer(cfg)._ssh()  # no key yet: plain ssh, agent and all
    key.write_text("k")
    argv = Syncer(cfg)._ssh()
    assert argv[argv.index("-i") + 1] == str(key)
    assert "IdentitiesOnly=yes" in argv and "IdentityAgent=none" in argv
