"""Persist cancelled admissions independently of an unavailable run database."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
import time
from pathlib import Path
from typing import Literal

_logger = logging.getLogger(__name__)

StartupState = Literal["pending", "started", "refused", "unknown"]


def _sync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class CancellationJournal:
    """Keep recovery requests until the run and reservation commit together."""

    def __init__(self, db_path: str) -> None:
        self.directory = (
            Path(f"{db_path}.cancelled").absolute()
            if db_path not in (":memory:", "")
            else None
        )
        self._memory: dict[str, StartupState] = {}
        self._memory_recorded_at: dict[str, float] = {}

    def _path(self, run_id: str) -> Path:
        assert self.directory is not None
        return self.directory / (hashlib.sha256(run_id.encode()).hexdigest() + ".json")

    def record(self, run_id: str, *, startup_state: StartupState) -> None:
        """Flush the request before allowing cancellation to escape."""
        if self.directory is None:
            self._memory[run_id] = startup_state
            self._memory_recorded_at[run_id] = time.time()
            return
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        _sync_directory(self.directory.parent)
        payload = {"run_id": run_id, "startup_state": startup_state}
        with tempfile.NamedTemporaryFile(
            mode="w", dir=self.directory, delete=False, encoding="utf-8"
        ) as handle:
            temporary = Path(handle.name)
            json.dump(payload, handle)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.replace(temporary, self._path(run_id))
            _sync_directory(self.directory)
        finally:
            temporary.unlink(missing_ok=True)

    def age_seconds(self, run_id: str) -> float:
        """Seconds since this request was recorded; 0.0 when it is unreadable.

        An unreadable age never expires a request: only a request kew can
        prove is old is dropped (governed review round 2, 2026-09-21).
        """
        if self.directory is None:
            recorded = self._memory_recorded_at.get(run_id)
            return 0.0 if recorded is None else max(0.0, time.time() - recorded)
        try:
            recorded = self._path(run_id).stat().st_mtime
        except OSError:
            _logger.warning(
                "cancellation request age is unreadable; keeping it for replay"
            )
            return 0.0
        return max(0.0, time.time() - recorded)

    def pending(self) -> list[tuple[str, StartupState]]:
        """Read complete requests; interrupted temporary writes are excluded.

        One unreadable or malformed file must not take the whole sweep with it:
        this runs inside every dispatch admission and every driver tick, and a
        single corrupt request would otherwise stop kew recovering any of the
        others (governed review round 4, 2026-09-21). An unreadable request is
        named and skipped, and its file is left for an operator to inspect.
        """
        if self.directory is None:
            return list(self._memory.items())
        requests = []
        for path in self.directory.glob("*.json"):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                request = (payload["run_id"], payload["startup_state"])
            except FileNotFoundError:
                continue  # Another reaper completed this request.
            except (OSError, UnicodeError, ValueError, TypeError, KeyError):
                _logger.warning(
                    "cancellation request %s is unreadable; skipping it", path.name
                )
                continue
            requests.append(request)
        return requests

    def remove(self, run_id: str) -> None:
        """A committed terminalization makes replay unnecessary."""
        if self.directory is None:
            self._memory.pop(run_id, None)
            return
        self._path(run_id).unlink(missing_ok=True)
        _sync_directory(self.directory)
