"""Keep renamed screenshots in step with one peer Mac, point to point over SSH.

Transport is `ssh` for a listing and `rsync --ignore-existing` for the bytes, so
nothing passes through a third party: on a tailnet the peer is a MagicDNS name
and the traffic goes machine to machine.

Every sweep works out three things from what is here, what is on the peer, and
what we remembered last time:

* **Deletions.** A name we synced before (`known`) that is now gone was deleted
  by the user. It becomes a tombstone, which keeps us from pulling the peer's
  copy back, and tells the peer to trash its own copy when *it* next sweeps.
  Each machine only ever deletes its own files, and only to the Trash.
* **Sends.** Our names the peer lacks.
* **Receives.** Peer names we lack, minus our tombstones.

A tombstone loses to a copy that is newer than it (by ctime), which is what a
restore from the Trash looks like: the restored file wins, and its tombstone is
dropped. Raw `Screenshot ...` files are never synced — each machine renames its
own — so one capture is never sent to the model twice under two names.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shlex
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config
from .fsutil import rename_no_clobber

log = logging.getLogger("ssrename")

STATE_PATH = Path("~/.local/state/ssrename/sync.json").expanduser()
#: Where the peer keeps the same file, relative to its home directory. ssh
#: starts in the home directory, so no `~` expansion is needed.
REMOTE_STATE_PATH = ".local/state/ssrename/sync.json"
FALLBACK_TRASH = Path("~/.local/state/ssrename/trash").expanduser()

#: Separates the peer's state file from its directory listing in one ssh call.
LISTING_MARKER = "--ssrename-listing--"

#: Tombstones are names, a few dozen bytes each; a year covers a laptop that
#: spends a long time in a drawer.
TOMBSTONE_TTL = 365 * 86400

#: Refuse to record deletions when this many known files vanish at once — an
#: unmounted volume or a wrong watch_dir looks exactly like "the user deleted
#: everything", and the peer would then trash its whole copy.
MASS_DELETE_MIN = 10
MASS_DELETE_FRACTION = 0.5

#: Seconds to let a burst of filesystem events settle before sweeping.
POKE_DEBOUNCE = 3.0

SSH_OPTIONS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=10"]


class SyncError(Exception):
    """Sync is misconfigured or failed in a way worth a loud error."""


class PeerUnreachable(SyncError):
    """The peer is asleep, off, or off the network. Expected; retried quietly."""


@dataclass
class SyncState:
    known: set[str] = field(default_factory=set)
    tombstones: dict[str, float] = field(default_factory=dict)

    @classmethod
    def parse(cls, text: str) -> SyncState:
        if not text.strip():
            return cls()
        data = json.loads(text)
        return cls(
            known=set(data.get("known", [])),
            tombstones={str(k): float(v) for k, v in data.get("tombstones", {}).items()},
        )

    @classmethod
    def load(cls, path: Path) -> SyncState:
        try:
            return cls.parse(path.read_text())
        except FileNotFoundError:
            return cls()

    def save(self, path: Path) -> None:
        """Atomically, since the peer may read it at any moment."""
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {"known": sorted(self.known), "tombstones": dict(sorted(self.tombstones.items()))}
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".sync-", suffix=".json")
        with os.fdopen(fd, "w") as fh:
            json.dump(data, fh, indent=1)
        os.replace(tmp, path)


@dataclass
class SweepReport:
    sent: list[str] = field(default_factory=list)
    received: list[str] = field(default_factory=list)
    trashed: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)  # newly tombstoned here
    refused: str | None = None

    def summary(self) -> str:
        parts = [
            f"sent {len(self.sent)}",
            f"received {len(self.received)}",
            f"trashed {len(self.trashed)} deleted on peer",
            f"recorded {len(self.deleted)} local deletion(s)",
        ]
        return ", ".join(parts)

    @property
    def changed(self) -> bool:
        return bool(self.sent or self.received or self.trashed or self.deleted)


def remote_dir_for(cfg: Config) -> str:
    """The peer's directory as ssh/rsync want it: relative to its home."""
    raw = cfg.sync.remote_dir
    if raw:
        return raw[2:] if raw.startswith("~/") else raw
    try:
        return str(cfg.watch_dir.relative_to(Path.home()))
    except ValueError:
        return str(cfg.watch_dir)


class Syncer:
    def __init__(
        self,
        cfg: Config,
        dry_run: bool = False,
        state_path: Path | None = None,
        trash_dir: Path | None = None,
    ):
        self.cfg = cfg
        self.peer = cfg.sync.peer
        self.remote_dir = remote_dir_for(cfg)
        self.dry_run = dry_run
        self.state_path = state_path or STATE_PATH
        self.trash_dir = trash_dir or Path.home() / ".Trash"
        self._raw = re.compile(cfg.filename_pattern)
        self._poked = threading.Event()
        self._lock = threading.Lock()
        self._down = False

    # --- what is synced -------------------------------------------------

    def eligible(self, name: str) -> bool:
        if not name or name.startswith("."):
            return False
        if Path(name).suffix.lower().lstrip(".") not in self.cfg.sync.extensions:
            return False
        # Unrenamed captures belong to the machine that took them.
        return not self._raw.match(name.replace(" ", " "))

    def local_names(self) -> dict[str, float]:
        """Eligible files here, with their ctime. Raises OSError if unreadable."""
        found = {}
        with os.scandir(self.cfg.watch_dir) as it:
            for entry in it:
                if self.eligible(entry.name) and entry.is_file(follow_symlinks=False):
                    found[entry.name] = entry.stat(follow_symlinks=False).st_ctime
        return found

    # --- talking to the peer ------------------------------------------------

    def _ssh(self) -> list[str]:
        return [*shlex.split(self.cfg.sync.ssh_command), *SSH_OPTIONS]

    def _remote(self, command: str) -> subprocess.CompletedProcess:
        argv = [*self._ssh(), self.peer, command]
        try:
            result = subprocess.run(argv, capture_output=True, text=True, timeout=60)
        except subprocess.TimeoutExpired:
            raise PeerUnreachable(f"{self.peer}: ssh timed out")
        except OSError as e:
            raise SyncError(f"cannot run {argv[0]}: {e}")
        if result.returncode == 255:
            err = result.stderr.strip()
            # Same exit code, different problem: these will not fix themselves.
            if any(s in err for s in ("Permission denied", "Host key verification", "Could not resolve")):
                raise SyncError(f"ssh {self.peer}: {err}")
            raise PeerUnreachable(err or f"ssh {self.peer} failed")
        return result

    def remote_state(self) -> tuple[set[str], SyncState]:
        """The peer's eligible names and its sync state, in one round trip."""
        command = (
            f"cat {shlex.quote(REMOTE_STATE_PATH)} 2>/dev/null; echo; echo {LISTING_MARKER}; "
            f"cd -- {shlex.quote(self.remote_dir)} && ls -1A"
        )
        result = self._remote(command)
        if result.returncode != 0:
            raise SyncError(
                f"cannot list {self.peer}:{self.remote_dir}: {result.stderr.strip()}"
            )
        state_text, sep, listing = result.stdout.partition(f"\n{LISTING_MARKER}\n")
        if not sep:
            raise SyncError(f"unexpected reply from {self.peer}: {result.stdout[:200]!r}")
        try:
            peer_state = SyncState.parse(state_text)
        except (ValueError, TypeError) as e:
            # Without its tombstones we would push back what the peer deleted.
            raise SyncError(f"unreadable sync state on {self.peer}: {e}")
        names = {n for n in listing.splitlines() if self.eligible(n)}
        return names, peer_state

    def _rsync(self, names: list[str], source: str, dest: str) -> None:
        if not names:
            return
        fd, listfile = tempfile.mkstemp(prefix="ssrename-sync-", suffix=".txt")
        try:
            with os.fdopen(fd, "w") as fh:
                fh.write("".join(f"{n}\n" for n in names))
            argv = [
                "rsync", "-a", "--ignore-existing",  # never overwrite, either side
                f"--files-from={listfile}",
                "-e", shlex.join(self._ssh()),
                source, dest,
            ]
            result = subprocess.run(argv, capture_output=True, text=True, timeout=3600)
        finally:
            os.unlink(listfile)
        if result.returncode != 0:
            raise SyncError(f"rsync {source} -> {dest} failed: {result.stderr.strip()[:400]}")

    def _remote_spec(self) -> str:
        return f"{self.peer}:{shlex.quote(self.remote_dir)}/"

    def _trash(self, name: str) -> Path:
        source = self.cfg.watch_dir / name
        for trash in (self.trash_dir, FALLBACK_TRASH):
            try:
                trash.mkdir(parents=True, exist_ok=True)
                stem, suffix = Path(name).stem, Path(name).suffix
                for n in range(1, 100):
                    dest = trash / (name if n == 1 else f"{stem} {n}{suffix}")
                    try:
                        rename_no_clobber(source, dest)
                        return dest
                    except FileExistsError:
                        continue
            except PermissionError:
                continue  # ~/.Trash can be off limits to a LaunchAgent
        raise SyncError(f"could not move {name} to the Trash")

    # --- the sweep ------------------------------------------------------------

    def sweep(self, accept_deletions: bool = False) -> SweepReport:
        """One full reconciliation with the peer. Raises SyncError/PeerUnreachable.

        `accept_deletions` overrides the mass-disappearance guard, for when the
        user really did delete that many files.
        """
        with self._lock:
            return self._sweep(accept_deletions)

    def _sweep(self, accept_deletions: bool) -> SweepReport:
        report = SweepReport()
        now = time.time()
        try:
            local = self.local_names()
        except OSError as e:
            raise SyncError(f"cannot read {self.cfg.watch_dir}: {e}")
        state = SyncState.load(self.state_path)

        # A tombstoned name that is here again was restored (or re-pulled by
        # hand): the file wins.
        for name in [n for n in state.tombstones if n in local]:
            del state.tombstones[name]
        for name, when in list(state.tombstones.items()):
            if now - when > TOMBSTONE_TTL:
                del state.tombstones[name]

        vanished = sorted(state.known - local.keys())
        mass = len(vanished) >= MASS_DELETE_MIN and len(vanished) > MASS_DELETE_FRACTION * len(state.known)
        if mass and not accept_deletions:
            report.refused = (
                f"{len(vanished)} of {len(state.known)} synced screenshots vanished from "
                f"{self.cfg.watch_dir} at once; not treating that as deletions. If you "
                f"really deleted them, run `ssrename sync --accept-deletions`."
            )
            raise SyncError(report.refused)
        for name in vanished:
            state.tombstones[name] = now
        report.deleted = vanished

        try:
            remote, peer_state = self.remote_state()
        except SyncError:
            # Deletions are facts about this machine; keep them even offline.
            if not self.dry_run:
                state.known = set(local)
                state.save(self.state_path)
            raise

        peer_tombstones = peer_state.tombstones
        # The peer deleted these after our copy arrived: follow suit.
        for name, when in sorted(peer_tombstones.items()):
            if name in local and when > local[name]:
                if not self.dry_run:
                    self._trash(name)
                del local[name]
                report.trashed.append(name)

        # Names the peer had and has lost are deletions it has not recorded as
        # tombstones yet. Sending them would undo the delete before it counts.
        unrecorded = peer_state.known - remote
        report.sent = sorted(local.keys() - remote - unrecorded)
        report.received = sorted(remote - local.keys() - state.tombstones.keys() - peer_tombstones.keys())

        if self.dry_run:
            return report
        try:
            self._rsync(report.sent, f"{self.cfg.watch_dir}/", self._remote_spec())
            self._rsync(report.received, self._remote_spec(), f"{self.cfg.watch_dir}/")
        finally:
            # Start from what this sweep began with, not a fresh scan: a file
            # deleted while the transfers ran must stay known, so the next
            # sweep sees it vanish and records the deletion. Received files
            # count only once they have actually arrived.
            try:
                arrived = set(report.received) & set(self.local_names())
            except OSError:
                arrived = set()
            state.known = set(local) | arrived
            state.save(self.state_path)
        return report

    def check(self) -> str:
        """For `doctor`: can we reach the peer and list its directory?"""
        remote, peer_state = self.remote_state()
        return (
            f"{self.peer}:{self.remote_dir} ok - {len(remote)} there, "
            f"{len(self.local_names())} here, {len(peer_state.tombstones)} peer deletion(s)"
        )

    # --- background loop ----------------------------------------------------

    def poke(self, path: Path) -> None:
        """A file in watch_dir changed; sweep soon if it is one we sync."""
        if self.eligible(Path(path).name):
            self._poked.set()

    def run_once(self) -> None:
        """Sweep and log, never raise: the watcher must outlive any sync trouble."""
        try:
            report = self.sweep()
        except PeerUnreachable as e:
            if not self._down:
                log.warning("sync peer %s unreachable, will keep trying: %s", self.peer, e)
                self._down = True
            else:
                log.debug("sync peer %s still unreachable", self.peer)
            return
        except SyncError as e:
            log.error("sync: %s", e)
            return
        except Exception:
            log.exception("sync: unexpected failure")
            return
        if self._down:
            log.info("sync peer %s reachable again", self.peer)
            self._down = False
        if report.changed:
            log.info("sync with %s: %s", self.peer, report.summary())
            for name in report.trashed:
                log.info("sync: trashed %s (deleted on %s)", name, self.peer)

    def run(self, stop: threading.Event) -> None:
        """Sweep on start, after changes, and every interval.

        Scheduled on the wall clock, not monotonic: monotonic time stands still
        while a laptop sleeps, and waking up is exactly when a sweep is due.
        """
        interval = max(self.cfg.sync.interval_seconds, 10.0)
        next_sweep = 0.0
        while not stop.is_set():
            wait = min(max(next_sweep - time.time(), 0.0), 30.0)
            poked = self._poked.wait(wait)
            if stop.is_set():
                return
            if poked:
                stop.wait(POKE_DEBOUNCE)
                self._poked.clear()
            elif time.time() < next_sweep:
                continue
            self.run_once()
            next_sweep = time.time() + interval
