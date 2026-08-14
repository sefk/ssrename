"""Watch the screenshot directory and rename what lands in it."""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from .renamer import Renamer

log = logging.getLogger("ssrename")


class _Handler(FileSystemEventHandler):
    def __init__(self, enqueue):
        self.enqueue = enqueue

    def on_created(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self.enqueue(Path(event.src_path))

    def on_moved(self, event: FileSystemEvent) -> None:
        # Some capture paths write a temp file and move it into place.
        if not event.is_directory and event.dest_path:
            self.enqueue(Path(event.dest_path))


class Watcher:
    """FSEvents watch with a debounce queue.

    The debounce is what keeps us out of the way of the corner thumbnail: macOS
    only writes the file once the thumbnail expires or is dismissed, and we then
    wait `debounce_seconds` more before touching it.
    """

    def __init__(self, renamer: Renamer):
        self.renamer = renamer
        self.cfg = renamer.cfg
        self._pending: dict[Path, float] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._started_at = time.time()

    def enqueue(self, path: Path) -> None:
        if not self.renamer.is_candidate(path):
            return
        with self._lock:
            if path in self._pending:  # never push the deadline back
                return
            self._pending[path] = time.monotonic() + self.cfg.debounce_seconds
        log.debug("queued %s", path.name)

    def _due(self) -> list[Path]:
        now = time.monotonic()
        with self._lock:
            due = [p for p, t in self._pending.items() if t <= now]
            for p in due:
                del self._pending[p]
        return due

    def scan(self) -> None:
        """Pick up anything the event stream missed.

        Only files that appeared since startup: an older backlog is the job of
        `ssrename backfill`, which the user asks for explicitly.
        """
        try:
            entries = list(self.cfg.watch_dir.iterdir())
        except OSError as e:
            log.error("cannot read %s: %s", self.cfg.watch_dir, e)
            return
        for path in entries:
            if not self.renamer.is_candidate(path):
                continue
            try:
                if path.stat().st_mtime < self._started_at:
                    continue
            except OSError:
                continue
            self.enqueue(path)

    def _worker(self) -> None:
        next_scan = 0.0
        while not self._stop.is_set():
            if self.cfg.poll_seconds and time.monotonic() >= next_scan:
                self.scan()
                next_scan = time.monotonic() + self.cfg.poll_seconds
            for path in self._due():
                result = self.renamer.process(path)
                if result.error:
                    log.error("%s: %s", path.name, result.error)
                elif result.skipped:
                    log.debug("%s: skipped (%s)", path.name, result.skipped)
            self._stop.wait(0.5)

    def run(self) -> None:
        watch_dir = self.cfg.watch_dir
        watch_dir.mkdir(parents=True, exist_ok=True)
        observer = Observer()
        observer.schedule(_Handler(self.enqueue), str(watch_dir), recursive=False)
        observer.start()
        worker = threading.Thread(target=self._worker, daemon=True)
        worker.start()
        log.info("watching %s (backend=%s)", watch_dir, self.cfg.backend_kind)
        try:
            while not self._stop.is_set():
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass
        finally:
            self._stop.set()
            observer.stop()
            observer.join()
            worker.join(timeout=2)

    def stop(self) -> None:
        self._stop.set()
