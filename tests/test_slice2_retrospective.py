#!/usr/bin/env python3
"""
Epic 10 Slice 2 — Retrospective Threshold Replay
=================================================
In-process smoke test for the slice 2 changes. Uses temp paths for buffer
and outage state, and a mock Supabase client that records inserts/updates
so we can assert on what would have been sent to the cloud.

Run from repo root:

    python pi/tests/test_slice2_retrospective.py
"""

import json
import os
import sys
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path

# Make the subscriber package importable
ROOT = Path(__file__).resolve().parent.parent / "subscriber"
sys.path.insert(0, str(ROOT))

# Stub the notifications module so we don't need httpx for testing
import types
_notify_calls = []
_notifications_stub = types.ModuleType("notifications")
def _fake_notify(alert_id, channel):
    _notify_calls.append((alert_id, channel))
    return True
def _fake_send_battery_digest():
    return True
_notifications_stub.notify = _fake_notify
_notifications_stub.send_battery_digest = _fake_send_battery_digest
sys.modules["notifications"] = _notifications_stub

from buffer import ReadingBuffer      # noqa: E402
from outage import OutageTracker      # noqa: E402
from alerting import AlertEngine      # noqa: E402


# ── Mock Supabase ────────────────────────────────────────────────

class MockTable:
    def __init__(self, name, store):
        self.name = name
        self.store = store
        self._filters = []
        self._payload = None
        self._op = None

    def insert(self, payload):
        self._op = "insert"
        self._payload = payload
        return self

    def update(self, payload):
        self._op = "update"
        self._payload = payload
        return self

    def select(self, *args, **kw):
        self._op = "select"
        return self

    def eq(self, col, val):
        self._filters.append(("eq", col, val))
        return self

    def is_(self, col, val):
        self._filters.append(("is", col, val))
        return self

    def order(self, *args, **kw):
        return self

    def limit(self, *args, **kw):
        return self

    def single(self):
        return self

    def execute(self):
        if self._op == "insert":
            # Mimic Supabase: return {data: [inserted_row_with_id]}
            row = dict(self._payload)
            # If alerts table, fake an id
            if self.name == "alerts" and "id" not in row:
                row["id"] = f"alert-{len(self.store.get('alerts', []))}"
            self.store.setdefault(self.name, []).append(row)
            return type("Res", (), {"data": [row]})()
        if self._op == "update":
            matched = []
            for row in self.store.get(self.name, []):
                if all(row.get(c) == v for op, c, v in self._filters if op == "eq"):
                    row.update(self._payload)
                    matched.append(row)
            return type("Res", (), {"data": matched})()
        if self._op == "select":
            rows = self.store.get(self.name, [])
            for op, c, v in self._filters:
                if op == "eq":
                    rows = [r for r in rows if r.get(c) == v]
                elif op == "is" and v == "null":
                    rows = [r for r in rows if r.get(c) is None]
            return type("Res", (), {"data": list(rows)})()
        return type("Res", (), {"data": []})()


class MockSupabase:
    def __init__(self):
        self.store = {}

    def table(self, name):
        return MockTable(name, self.store)


# ── Helpers ──────────────────────────────────────────────────────

CABINET = {
    "id": "cab-1",
    "name": "Meat fridge",
    "warn_low": 0.0,
    "warn_high": 5.0,
    "crit_low": -2.0,
    "crit_high": 8.0,
    "alerts_muted_until": None,
}

SENSOR = {
    "id": "sensor-1",
    "zigbee_id": "0xabcd",
    "low_signal_since": None,
}


def fresh_paths():
    """Yield (buffer_path, outage_path) tuple in a temp dir scope."""
    tmp = tempfile.mkdtemp(prefix="chillcheck-slice2-")
    return os.path.join(tmp, "buffer.db"), os.path.join(tmp, "outage.json")


def reading(temp, recorded_at=None):
    return {
        "id": None,  # let buffer generate
        "organisation_id": "org-1",
        "site_id": "site-1",
        "cabinet_id": CABINET["id"],
        "sensor_id": SENSOR["id"],
        "temperature": temp,
        "recorded_at": recorded_at or datetime.now(timezone.utc).isoformat(),
    }


def assert_eq(actual, expected, label):
    if actual != expected:
        print(f"  FAIL {label}: expected {expected!r}, got {actual!r}")
        sys.exit(1)
    print(f"  PASS {label}")


def assert_true(cond, label):
    if not cond:
        print(f"  FAIL {label}")
        sys.exit(1)
    print(f"  PASS {label}")


# ── Tests ────────────────────────────────────────────────────────

def test_outage_tracker_basic():
    print("\n[1] OutageTracker basic lifecycle")
    _, outage_path = fresh_paths()
    t = OutageTracker(state_path=outage_path)
    assert_true(t.enabled, "tracker enabled")
    assert_eq(t.peek_duration_seconds(), None, "no outage initially")
    t.mark_failed_write()
    d1 = t.peek_duration_seconds()
    assert_true(d1 is not None and d1 < 5, "outage start recorded")
    t.mark_failed_write()  # idempotent
    d2 = t.peek_duration_seconds()
    assert_true(d2 is not None and d2 >= d1, "second mark didn't reset")
    duration = t.clear()
    assert_true(duration is not None, "clear returned duration")
    assert_eq(t.peek_duration_seconds(), None, "state cleared")


def test_outage_tracker_persistence():
    print("\n[2] OutageTracker persists across restart")
    _, outage_path = fresh_paths()
    t1 = OutageTracker(state_path=outage_path)
    t1.mark_failed_write()
    # Simulate restart by constructing a fresh tracker against the same path
    t2 = OutageTracker(state_path=outage_path)
    d = t2.peek_duration_seconds()
    assert_true(d is not None, "outage restored from disk")
    state_on_disk = json.loads(Path(outage_path).read_text())
    assert_true("started_at" in state_on_disk, "state file written")
    t2.clear()
    assert_true(not Path(outage_path).exists(), "clear removes state file")


def test_outage_tracker_fail_soft():
    print("\n[3] OutageTracker fail-soft when path unwritable")
    # Path under a non-existent parent without write perm — use /proc/foo/bar
    # which can't be created. On Windows /proc doesn't exist so use a file-as-dir trap.
    bad = tempfile.mktemp(prefix="not-a-dir-")
    Path(bad).write_text("blocking file")  # exists as a file, can't be a dir
    t = OutageTracker(state_path=os.path.join(bad, "child", "outage.json"))
    assert_eq(t.enabled, False, "tracker disabled on bad path")
    t.mark_failed_write()  # no-op
    assert_eq(t.peek_duration_seconds(), None, "peek returns None when disabled")
    assert_eq(t.clear(), None, "clear returns None when disabled")


def test_buffer_drain_returns_rows():
    print("\n[4] ReadingBuffer.drain returns drained dicts")
    buf_path, _ = fresh_paths()
    buf = ReadingBuffer(db_path=buf_path)
    sb = MockSupabase()
    r1 = reading(6.0, "2026-05-19T10:00:00+00:00")
    r2 = reading(6.5, "2026-05-19T10:01:00+00:00")
    buf.enqueue(r1)
    buf.enqueue(r2)
    drained, remaining = buf.drain(sb)
    assert_eq(remaining, 0, "buffer empty after drain")
    assert_eq(len(drained), 2, "two rows returned")
    assert_eq(drained[0]["recorded_at"], "2026-05-19T10:00:00+00:00", "chronological order preserved")
    assert_eq(len(sb.store["readings"]), 2, "rows in Supabase")


def test_retrospective_alert_with_original_triggered_at():
    print("\n[5] check_temperature with recorded_at sets triggered_at")
    sb = MockSupabase()
    ae = AlertEngine(sb, "org-1", "site-1", "dev-1")
    historic = "2026-05-19T08:30:00+00:00"
    ae.check_temperature(
        CABINET, SENSOR, temperature=12.0,
        recorded_at=historic,
        outage_duration_seconds=1800,  # 30min, in mid tier
    )
    alerts = sb.store.get("alerts", [])
    assert_eq(len(alerts), 1, "alert raised")
    assert_eq(alerts[0]["triggered_at"], historic, "triggered_at uses recorded_at")
    assert_eq(alerts[0]["severity"], "critical", "12C above crit_high is critical")
    assert_true("30m offline period" in alerts[0]["message"], "message has outage suffix")


def test_long_outage_bumps_warning_to_critical():
    print("\n[6] >=60min outage bumps warning band to critical")
    sb = MockSupabase()
    ae = AlertEngine(sb, "org-1", "site-1", "dev-1")
    # 6.0 is above warn_high (5.0) but below crit_high (8.0) — warning band
    ae.check_temperature(
        CABINET, SENSOR, temperature=6.5,
        recorded_at="2026-05-19T08:00:00+00:00",
        outage_duration_seconds=4200,  # 70min
    )
    alerts = sb.store.get("alerts", [])
    assert_eq(len(alerts), 1, "alert raised")
    assert_eq(alerts[0]["severity"], "critical", "warning band bumped to critical for long outage")
    assert_true("70m offline period" in alerts[0]["message"], "message has outage suffix")


def test_warning_band_no_bump_under_60min():
    print("\n[7] <60min outage leaves warning band as warning")
    sb = MockSupabase()
    ae = AlertEngine(sb, "org-1", "site-1", "dev-1")
    ae.check_temperature(
        CABINET, SENSOR, temperature=6.5,
        recorded_at="2026-05-19T08:00:00+00:00",
        outage_duration_seconds=1500,  # 25min
    )
    alerts = sb.store.get("alerts", [])
    assert_eq(alerts[0]["severity"], "warning", "warning stays warning under 60min")


def test_normal_temp_no_alert():
    print("\n[8] retrospective check on normal temp raises nothing")
    sb = MockSupabase()
    ae = AlertEngine(sb, "org-1", "site-1", "dev-1")
    ae.check_temperature(
        CABINET, SENSOR, temperature=3.0,
        recorded_at="2026-05-19T08:00:00+00:00",
        outage_duration_seconds=7200,
    )
    assert_eq(len(sb.store.get("alerts", [])), 0, "no alert for in-band temp")


def test_full_replay_flow():
    print("\n[9] End-to-end: buffer outage -> drain -> replay -> alert")
    buf_path, outage_path = fresh_paths()
    buf = ReadingBuffer(db_path=buf_path)
    ot = OutageTracker(state_path=outage_path)
    sb = MockSupabase()
    ae = AlertEngine(sb, "org-1", "site-1", "dev-1")

    # Simulate outage: mark failed write, enqueue breach reading
    ot.mark_failed_write()
    # Fake the outage as 30 minutes by rewinding started_at
    ot._started_at = datetime.now(timezone.utc) - timedelta(minutes=30)
    breach = reading(12.0, "2026-05-19T08:00:00+00:00")
    buf.enqueue(breach)

    # Drain
    drained, remaining = buf.drain(sb)
    assert_eq(len(drained), 1, "one row drained")
    assert_eq(remaining, 0, "buffer empty")

    # Replay
    duration = ot.peek_duration_seconds()
    assert_true(duration >= 1800, "duration peek >= 30min")
    for row in drained:
        ae.check_temperature(
            CABINET, SENSOR, temperature=row["temperature"],
            recorded_at=row["recorded_at"],
            outage_duration_seconds=duration,
        )
    ot.clear()

    alerts = sb.store.get("alerts", [])
    assert_eq(len(alerts), 1, "retrospective alert created")
    assert_eq(alerts[0]["triggered_at"], "2026-05-19T08:00:00+00:00", "stamped at original time")
    assert_true("offline period" in alerts[0]["message"], "outage suffix present")
    assert_true(_notify_calls and _notify_calls[-1][1] == "email", "email notify fired")


# ── Run ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    test_outage_tracker_basic()
    test_outage_tracker_persistence()
    test_outage_tracker_fail_soft()
    test_buffer_drain_returns_rows()
    test_retrospective_alert_with_original_triggered_at()
    test_long_outage_bumps_warning_to_critical()
    test_warning_band_no_bump_under_60min()
    test_normal_temp_no_alert()
    test_full_replay_flow()
    print("\n[OK] All slice 2 smoke tests passed")
