"""
ChillCheck — Outage Tracker
===========================
Tracks Pi-side connectivity-outage windows. The MQTT thread calls
``mark_failed_write()`` whenever a Supabase write fails; the drain thread
calls ``mark_drain_success()`` when buffered readings successfully sync.

Persisted to disk so a subscriber restart mid-outage doesn't lose the
start time — without that, a restart at minute 40 of a 60-minute outage
would look like a fresh outage and we'd undercount the duration for
retrospective-alert tiering.

Fail-soft: if the state file can't be written (no perms, full disk), the
tracker disables itself and becomes a no-op. Losing duration tracking is
far less bad than crash-looping the subscriber.
"""

import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger("chillcheck.outage")

DEFAULT_STATE_PATH = "/var/lib/chillcheck/outage.json"


class OutageTracker:
    """Tracks a single in-progress outage. Thread-safe via lock.

    State on disk is `{"started_at": "<iso8601>"}` or absent (= no outage).
    """

    def __init__(self, state_path: str = DEFAULT_STATE_PATH):
        self.state_path = Path(state_path)
        self._lock = threading.Lock()
        self.enabled = False
        self._started_at: Optional[datetime] = None
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            self._load()
            self.enabled = True
        except Exception as e:
            log.error(
                f"OutageTracker disabled (state path {state_path} unavailable): {e}. "
                f"Retrospective alert tiering will be skipped during outages."
            )

    def _load(self):
        """Restore in-memory state from disk on init."""
        if not self.state_path.exists():
            return
        try:
            data = json.loads(self.state_path.read_text())
            started = data.get("started_at")
            if started:
                self._started_at = datetime.fromisoformat(started.replace("Z", "+00:00"))
                log.info(f"OutageTracker resumed in-progress outage started at {started}")
        except Exception as e:
            log.warning(f"Couldn't parse outage state at {self.state_path}: {e}; ignoring")

    def _persist(self):
        """Write current state to disk atomically. Caller holds the lock."""
        tmp = self.state_path.with_suffix(".tmp")
        if self._started_at is None:
            try:
                self.state_path.unlink(missing_ok=True)
            except Exception as e:
                log.warning(f"Couldn't remove outage state file: {e}")
            return
        payload = json.dumps({"started_at": self._started_at.isoformat()})
        tmp.write_text(payload)
        tmp.replace(self.state_path)

    def mark_failed_write(self):
        """Record that a Supabase write just failed.

        First failure of an outage stamps ``started_at`` and persists.
        Subsequent failures are no-ops — we only care about the start.
        """
        if not self.enabled:
            return
        with self._lock:
            if self._started_at is not None:
                return
            self._started_at = datetime.now(timezone.utc)
            try:
                self._persist()
            except Exception as e:
                log.error(f"Couldn't persist outage start: {e}")
            log.warning(f"Outage started at {self._started_at.isoformat()}")

    def peek_duration_seconds(self) -> Optional[int]:
        """How long the in-progress outage has been running, in seconds.

        Returns ``None`` if no outage is in progress. Non-destructive —
        callers can read this on every drain tick to tier retrospective
        alerts without losing state for the next batch.
        """
        if not self.enabled:
            return None
        with self._lock:
            if self._started_at is None:
                return None
            return int((datetime.now(timezone.utc) - self._started_at).total_seconds())

    def clear(self) -> Optional[int]:
        """Mark the outage as fully resolved, clear state, return duration.

        Should only be called once the buffer is fully drained — otherwise
        a multi-batch drain would lose the outage window after the first
        batch and subsequent batches would skip retrospective tiering.
        Returns ``None`` if there was no outage in progress.
        """
        if not self.enabled:
            return None
        with self._lock:
            if self._started_at is None:
                return None
            duration = (datetime.now(timezone.utc) - self._started_at).total_seconds()
            log.info(f"Outage cleared; duration {int(duration)}s")
            self._started_at = None
            try:
                self._persist()
            except Exception as e:
                log.error(f"Couldn't clear outage state: {e}")
            return int(duration)
