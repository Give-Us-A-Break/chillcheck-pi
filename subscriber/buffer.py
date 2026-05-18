"""
ChillCheck — Local Reading Buffer
==================================
Persists temperature readings to a local SQLite database when Supabase
is unreachable, then drains them in chronological order when connectivity
returns.

Closes the data-loss gap during broadband outages: when a Supabase write
fails, the reading lands on disk with the same UUID it would have used in
the cloud, so on retry Supabase either accepts the row or rejects it as a
duplicate (idempotent).

Slice 1 of Epic 10 — retrospective threshold checks against drained rows
are slice 2.
"""

import logging
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("chillcheck.buffer")

DEFAULT_DB_PATH = "/var/lib/chillcheck/buffer.db"
DEFAULT_MAX_ROWS = 50_000  # ~7 days at 1 reading/min across 5 cabinets
DEFAULT_BATCH_SIZE = 100


class ReadingBuffer:
    """SQLite-backed queue for readings that couldn't reach Supabase.

    Drained in recorded_at order. Thread-safe via a reentrant lock; uses a
    fresh connection per operation so sqlite3's single-thread-per-connection
    rule is satisfied without sharing handles across threads.
    """

    def __init__(self, db_path: str = DEFAULT_DB_PATH, max_rows: int = DEFAULT_MAX_ROWS):
        self.db_path = db_path
        self.max_rows = max_rows
        self._lock = threading.RLock()
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    @contextmanager
    def _db(self):
        """Yield a SQLite connection, committing on success and always
        closing on exit. Wraps the lock acquisition so callers don't need
        to nest two context managers everywhere.
        """
        with self._lock:
            conn = sqlite3.connect(self.db_path, timeout=10)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            try:
                with conn:
                    yield conn
            finally:
                conn.close()

    def _init_schema(self):
        with self._db() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS pending_readings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    reading_id TEXT NOT NULL UNIQUE,
                    organisation_id TEXT NOT NULL,
                    site_id TEXT NOT NULL,
                    cabinet_id TEXT NOT NULL,
                    sensor_id TEXT NOT NULL,
                    temperature REAL NOT NULL,
                    recorded_at TEXT NOT NULL,
                    buffered_at TEXT NOT NULL DEFAULT (datetime('now')),
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_attempt_at TEXT
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_pending_recorded_at
                ON pending_readings(recorded_at)
            """)

    def enqueue(self, reading: dict) -> str:
        """Persist a reading locally. The same `reading_id` (UUID) is used
        if the row is later retried against Supabase, so duplicates are
        detected as primary-key conflicts and treated as successful sync.

        `reading` must contain: organisation_id, site_id, cabinet_id,
        sensor_id, temperature, recorded_at. An `id` field is used if
        present; otherwise a fresh UUID is generated and returned.
        """
        reading_id = reading.get("id") or str(uuid.uuid4())
        with self._db() as conn:
            try:
                conn.execute("""
                    INSERT INTO pending_readings
                    (reading_id, organisation_id, site_id, cabinet_id,
                     sensor_id, temperature, recorded_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (
                    reading_id,
                    reading["organisation_id"],
                    reading["site_id"],
                    reading["cabinet_id"],
                    reading["sensor_id"],
                    reading["temperature"],
                    reading["recorded_at"],
                ))
            except sqlite3.IntegrityError:
                log.debug(f"Reading {reading_id} already buffered")
                return reading_id

            count = conn.execute("SELECT COUNT(*) FROM pending_readings").fetchone()[0]
            if count > self.max_rows:
                overflow = count - self.max_rows
                conn.execute("""
                    DELETE FROM pending_readings
                    WHERE id IN (
                        SELECT id FROM pending_readings
                        ORDER BY recorded_at ASC
                        LIMIT ?
                    )
                """, (overflow,))
                log.warning(
                    f"Buffer at cap ({self.max_rows}) - dropped {overflow} oldest "
                    f"reading(s). Extended outage detected."
                )
        return reading_id

    def size(self) -> int:
        with self._db() as conn:
            return conn.execute("SELECT COUNT(*) FROM pending_readings").fetchone()[0]

    def drain(self, supabase, batch_size: int = DEFAULT_BATCH_SIZE) -> tuple[int, int]:
        """Push buffered readings to Supabase in chronological order.

        Returns ``(synced, remaining)``. Stops the batch on the first
        non-duplicate failure so we don't hammer a still-down endpoint;
        the next scheduler tick retries. Duplicate-key errors are treated
        as success (the row reached Supabase on a previous attempt but
        the response was lost).
        """
        with self._db() as conn:
            rows = conn.execute("""
                SELECT id, reading_id, organisation_id, site_id, cabinet_id,
                       sensor_id, temperature, recorded_at
                FROM pending_readings
                ORDER BY recorded_at ASC
                LIMIT ?
            """, (batch_size,)).fetchall()

        if not rows:
            return (0, 0)

        synced = 0
        for row in rows:
            row_id, reading_id, org, site, cab, sensor, temp, recorded = row
            try:
                supabase.table("readings").insert({
                    "id": reading_id,
                    "organisation_id": org,
                    "site_id": site,
                    "cabinet_id": cab,
                    "sensor_id": sensor,
                    "temperature": temp,
                    "recorded_at": recorded,
                }).execute()
                self._delete_row(row_id)
                synced += 1
            except Exception as e:
                err = str(e).lower()
                if "duplicate" in err or "23505" in err or "already exists" in err:
                    log.info(f"Reading {reading_id} already in Supabase, clearing")
                    self._delete_row(row_id)
                    synced += 1
                else:
                    self._mark_attempt(row_id)
                    log.warning(f"Drain failed for reading {reading_id}: {e}")
                    break

        remaining = self.size()
        if synced > 0:
            log.info(f"Drained {synced} buffered reading(s), {remaining} remaining")
        return (synced, remaining)

    def _delete_row(self, row_id: int):
        with self._db() as conn:
            conn.execute("DELETE FROM pending_readings WHERE id = ?", (row_id,))

    def _mark_attempt(self, row_id: int):
        now = datetime.now(timezone.utc).isoformat()
        with self._db() as conn:
            conn.execute("""
                UPDATE pending_readings
                SET attempts = attempts + 1, last_attempt_at = ?
                WHERE id = ?
            """, (now, row_id))
