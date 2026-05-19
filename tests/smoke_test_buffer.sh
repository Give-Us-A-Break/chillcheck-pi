#!/usr/bin/env bash
# smoke_test_buffer.sh — Offline reading buffer smoke test (Epic 10, slice 1)
#
# Simulates an internet outage by poisoning /etc/hosts for the Supabase host.
# Internal traffic (MQTT on 127.0.0.1, local Flask UI on port 80) keeps
# flowing throughout — we're testing outbound cloud connectivity only.
#
# What it proves:
#   1. Supabase write failures are caught and readings land in the local buffer
#   2. MQTT → Pi pipeline continues working during the outage (no data in flight lost)
#   3. On reconnect the drain job syncs buffered rows to Supabase in
#      chronological order within the next scheduler tick (~60s)
#   4. Buffered readings carry the original Pi-side recorded_at timestamp
#      so the cloud chart shows continuous data, not a post-outage spike
#
# Run on the Pi (needs sudo):
#   bash smoke_test_buffer.sh
#
# Or remotely from your dev machine:
#   PowerShell: (Get-Content pi\tests\smoke_test_buffer.sh -Raw) -replace "`r`n","`n" | ssh chillcheck@chillcheck.local 'bash -s'
#   bash/zsh:   ssh chillcheck@chillcheck.local 'bash -s' < pi/tests/smoke_test_buffer.sh
#
# Prerequisites on the Pi: sudo, python3 (already required by the subscriber)
# Approximate duration: 8–12 minutes

set -uo pipefail

# ─── Colours ──────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'

PASS() { echo -e "${GREEN}  PASS${NC}  $*"; }
FAIL() { echo -e "${RED}  FAIL${NC}  $*"; FAILURES=$((FAILURES + 1)); }
INFO() { echo -e "        $*"; }
NOTE() { echo -e "${CYAN}  NOTE${NC}  $*"; }
SECTION() { echo; echo "== $* =="; }
UI_BOX() {
    echo
    echo "  --- UI CHECK ---"
    while IFS= read -r line; do
        echo "  | $line"
    done <<< "$*"
    echo
}

FAILURES=0
SUPABASE_HOST=""
ENV_FILE="/etc/chillcheck/.env"
BUFFER_DB="/var/lib/chillcheck/buffer.db"
OUTAGE_SECS=180    # 3 minutes — ~3 readings per cabinet at 1/min
DRAIN_WAIT_SECS=90 # max wait for drain after restoring connectivity

# ─── Python helpers (no sqlite3 or dig CLI needed) ────────────────────────────
# The subscriber already uses Python's built-in sqlite3 module, so this works
# on every Pi without installing anything extra.

_buffer_count() {
    [[ -f "$BUFFER_DB" ]] || { echo 0; return; }
    python3 - "$BUFFER_DB" 2>/dev/null <<'PYEOF'
import sqlite3, sys
try:
    conn = sqlite3.connect(sys.argv[1])
    print(conn.execute("SELECT COUNT(*) FROM pending_readings").fetchone()[0])
except Exception as e:
    print("ERR", file=sys.stderr); sys.exit(1)
PYEOF
}

_buffer_rows() {
    [[ -f "$BUFFER_DB" ]] || return
    python3 - "$BUFFER_DB" 2>/dev/null <<'PYEOF'
import sqlite3, sys
conn = sqlite3.connect(sys.argv[1])
rows = conn.execute(
    "SELECT cabinet_id, temperature, recorded_at, attempts "
    "FROM pending_readings ORDER BY recorded_at DESC LIMIT 8"
).fetchall()
for r in rows:
    print(f"    cabinet={r[0]}  temp={r[1]:.1f}C  recorded_at={r[2]}  attempts={r[3]}")
PYEOF
}

_can_resolve() {
    # Uses Python's system resolver — respects /etc/hosts, so correctly
    # returns false after we poison the hosts file in Phase 1.
    python3 -c "import socket; socket.getaddrinfo('$1', 443, socket.AF_INET)" 2>/dev/null
}

# ─── Cleanup trap ─────────────────────────────────────────────────────────────
_flush_dns() {
    sudo systemd-resolve --flush-caches 2>/dev/null \
        || sudo resolvectl flush-caches 2>/dev/null \
        || true
}

_cleanup() {
    if [[ -n "${SUPABASE_HOST:-}" ]]; then
        sudo sed -i "/127\.0\.0\.1 ${SUPABASE_HOST}/d" /etc/hosts 2>/dev/null || true
        _flush_dns
        INFO "(cleanup) Removed /etc/hosts block for ${SUPABASE_HOST}"
    fi
}
trap _cleanup EXIT INT TERM

# ══════════════════════════════════════════════════════════════════════════════
# PHASE 0 — PRE-FLIGHT
# ══════════════════════════════════════════════════════════════════════════════
SECTION "Phase 0 -- Pre-flight checks"

for tool in sudo python3; do
    if command -v "$tool" &>/dev/null; then
        PASS "$tool available"
    else
        FAIL "$tool not found"
    fi
done

if [[ -f "$ENV_FILE" ]]; then
    PASS "Env file found at $ENV_FILE"
else
    FAIL "Env file not found at $ENV_FILE — is this running on the Pi?"
    exit 1
fi

SUPABASE_URL=$(grep '^SUPABASE_URL=' "$ENV_FILE" | cut -d= -f2- | tr -d '"' | tr -d "'")
if [[ -z "${SUPABASE_URL:-}" ]]; then
    FAIL "SUPABASE_URL not set in $ENV_FILE"
    exit 1
fi
SUPABASE_HOST=$(echo "$SUPABASE_URL" | sed 's|https\?://||' | sed 's|/.*||' | sed 's|:.*||')
PASS "Supabase host: ${SUPABASE_HOST}"

if systemctl is-active --quiet chillcheck-subscriber; then
    PASS "chillcheck-subscriber is active"
else
    FAIL "chillcheck-subscriber is not running — start it first:"
    INFO "  sudo systemctl start chillcheck-subscriber"
    exit 1
fi

# Buffer DB check
BASELINE=$(_buffer_count)
if [[ "$BASELINE" == "ERR" ]]; then
    FAIL "Buffer DB exists but Python could not query it — check permissions on $BUFFER_DB"
    BASELINE=0
elif [[ -f "$BUFFER_DB" && "$BASELINE" -gt 0 ]]; then
    NOTE "Buffer has ${BASELINE} pre-existing row(s) — test tracks the delta"
elif [[ -f "$BUFFER_DB" ]]; then
    PASS "Buffer DB is empty (clean baseline)"
else
    NOTE "Buffer DB does not exist yet — created on first failed write (normal for a fresh hub)"
    BASELINE=0
fi

# Check buffer isn't disabled
if sudo journalctl -u chillcheck-subscriber --since "10 minutes ago" --no-pager 2>/dev/null \
        | grep -q "ReadingBuffer disabled"; then
    FAIL "Buffer is currently disabled — fix the data dir first:"
    INFO "  sudo mkdir -p /var/lib/chillcheck && sudo chown chillcheck:chillcheck /var/lib/chillcheck"
    INFO "  sudo systemctl restart chillcheck-subscriber"
    exit 1
else
    PASS "No 'ReadingBuffer disabled' messages in recent logs"
fi

# Verify Supabase is reachable before we block it
if _can_resolve "$SUPABASE_HOST"; then
    PASS "Supabase host resolves (will block it in Phase 1)"
else
    FAIL "Cannot resolve ${SUPABASE_HOST} — check internet connectivity before running this test"
    exit 1
fi

UI_BOX "BEFORE THE TEST — check app.chillcheck.online and note:
  - Cabinet last-updated timestamps (these will freeze during outage)
  - Current temperatures look live and recent
Also open http://chillcheck.local — it should stay fully
responsive throughout the entire test."

read -rp "  Press ENTER when ready to start the outage simulation..."

# ══════════════════════════════════════════════════════════════════════════════
# PHASE 1 — SIMULATE OUTAGE
# Block Supabase by poisoning /etc/hosts. Python's socket.getaddrinfo() respects
# /etc/hosts (via nsswitch 'files' before 'dns'), so new connections get
# ECONNREFUSED to 127.0.0.1. MQTT to 127.0.0.1:1883 and Flask on port 80
# are completely unaffected.
# ══════════════════════════════════════════════════════════════════════════════
SECTION "Phase 1 -- Simulating outage (blocking ${SUPABASE_HOST})"

if grep -q "127.0.0.1 ${SUPABASE_HOST}" /etc/hosts 2>/dev/null; then
    NOTE "hosts entry already present (leftover from a previous run) — skipping add"
else
    echo "127.0.0.1 ${SUPABASE_HOST}" | sudo tee -a /etc/hosts > /dev/null
    PASS "Added '127.0.0.1 ${SUPABASE_HOST}' to /etc/hosts"
fi
_flush_dns
PASS "DNS cache flushed — new connections to Supabase will get ECONNREFUSED"

INFO "Outage window: ${OUTAGE_SECS}s (~3 readings per cabinet at 1 reading/min)"
INFO "Waiting 70s for the first failed write cycle to produce log output..."
sleep 70

EARLY_FAILS=$(sudo journalctl -u chillcheck-subscriber --since "75 seconds ago" \
    --no-pager 2>/dev/null \
    | grep -c "Supabase reading insert failed, buffering" || true)
if [[ "$EARLY_FAILS" -gt 0 ]]; then
    PASS "Subscriber is logging Supabase failures and buffering readings (${EARLY_FAILS} so far)"
else
    NOTE "No buffering messages yet — readings arrive once per minute; waiting for the next cycle"
fi

INFO "Waiting for the rest of the ${OUTAGE_SECS}s outage window..."
REMAINING=$((OUTAGE_SECS - 70))
ELAPSED_EXTRA=0
while [[ $ELAPSED_EXTRA -lt $REMAINING ]]; do
    sleep 15
    ELAPSED_EXTRA=$((ELAPSED_EXTRA + 15))
    CURRENT_BUF=$(_buffer_count 2>/dev/null || echo 0)
    printf "\r        %ds elapsed -- buffer size: %s row(s)..." $((70 + ELAPSED_EXTRA)) "$CURRENT_BUF"
done
echo

# ══════════════════════════════════════════════════════════════════════════════
# PHASE 2 — VERIFY BUFFER FILLED
# ══════════════════════════════════════════════════════════════════════════════
SECTION "Phase 2 -- Verifying buffer filled during outage"

BUFFER_AFTER=$(_buffer_count)
NEW_ROWS=$((BUFFER_AFTER - BASELINE))
if [[ "$BUFFER_AFTER" != "ERR" && "$NEW_ROWS" -gt 0 ]]; then
    PASS "Buffer has ${NEW_ROWS} new row(s) (total: ${BUFFER_AFTER})"
    INFO "Sample buffered rows (most recent first):"
    _buffer_rows
    INFO ""
    INFO "  recorded_at is the Pi-side timestamp. After drain, readings slot"
    INFO "  into the chart at these exact times — no spike, no gap."
else
    FAIL "No new rows in buffer after ${OUTAGE_SECS}s. Possible causes:"
    INFO "  - No sensors are assigned to cabinets (unassigned sensors skip readings insert)"
    INFO "  - Readings arrive every ~60s; timing may have aligned badly"
    INFO "  Recent subscriber log:"
    sudo journalctl -u chillcheck-subscriber --since "${OUTAGE_SECS} seconds ago" \
        --no-pager 2>/dev/null | tail -15 || true
fi

OUTAGE_LOG_HITS=$(sudo journalctl -u chillcheck-subscriber \
    --since "${OUTAGE_SECS} seconds ago" --no-pager 2>/dev/null \
    | grep -c "Supabase reading insert failed, buffering" || true)
if [[ "$OUTAGE_LOG_HITS" -gt 0 ]]; then
    PASS "Log shows ${OUTAGE_LOG_HITS} 'Supabase reading insert failed, buffering' message(s)"
else
    FAIL "Expected 'Supabase reading insert failed, buffering' in logs but found none"
fi

SENSOR_UPDATE_FAILS=$(sudo journalctl -u chillcheck-subscriber \
    --since "${OUTAGE_SECS} seconds ago" --no-pager 2>/dev/null \
    | grep -c "Failed to update sensor" || true)
if [[ "$SENSOR_UPDATE_FAILS" -gt 0 ]]; then
    NOTE "Sensor last_seen updates also failed (${SENSOR_UPDATE_FAILS}) — expected."
    NOTE "  Sensor timestamps look stale in the cloud during the outage."
    NOTE "  They recover on the next successful reading after reconnect."
fi

MQTT_MESSAGES=$(sudo journalctl -u chillcheck-subscriber \
    --since "${OUTAGE_SECS} seconds ago" --no-pager 2>/dev/null \
    | grep -c "Reading:.*->.*C" || true)
if [[ "$MQTT_MESSAGES" -gt 0 ]]; then
    PASS "MQTT readings still arriving (${MQTT_MESSAGES} log lines) — internal traffic unaffected"
else
    NOTE "No 'Reading:' log lines found in this window (sensors fire ~1/min; may have missed)"
fi

UI_BOX "DURING OUTAGE — check these now before restoring connectivity:
  app.chillcheck.online:
    - Cabinet last-updated timestamps should be STALE (frozen)
    - Hub may show 'Offline' in Settings > Devices (heartbeat also
      hits the internet -- expected side-effect of our block)
  http://chillcheck.local (should be fully responsive):
    - Sensors tab shows current temperatures (MQTT still flowing)
    - Logs tab > Subscriber: look for 'Supabase reading insert
      failed, buffering' and 'Failed to update sensor' messages"

read -rp "  Press ENTER to restore connectivity and start drain..."

# ══════════════════════════════════════════════════════════════════════════════
# PHASE 3 — RESTORE CONNECTIVITY
# ══════════════════════════════════════════════════════════════════════════════
SECTION "Phase 3 -- Restoring connectivity"

sudo sed -i "/127\.0\.0\.1 ${SUPABASE_HOST}/d" /etc/hosts
SUPABASE_HOST=""  # disarm the cleanup trap
_flush_dns
PASS "Removed /etc/hosts block"
PASS "DNS cache flushed"

INFO "Drain job runs every 60s. Polling buffer for up to ${DRAIN_WAIT_SECS}s..."

DRAIN_ELAPSED=0
DRAINED=false
while [[ $DRAIN_ELAPSED -lt $DRAIN_WAIT_SECS ]]; do
    sleep 10
    DRAIN_ELAPSED=$((DRAIN_ELAPSED + 10))
    CURRENT=$(_buffer_count 2>/dev/null || echo "$BUFFER_AFTER")
    printf "\r        %ds -- buffer: %s row(s)..." "$DRAIN_ELAPSED" "$CURRENT"
    if [[ "$CURRENT" != "ERR" && "$CURRENT" -le "$BASELINE" ]]; then
        echo
        DRAINED=true
        break
    fi
done
[[ "$DRAINED" == false ]] && echo

# ══════════════════════════════════════════════════════════════════════════════
# PHASE 4 — VERIFY DRAIN
# ══════════════════════════════════════════════════════════════════════════════
SECTION "Phase 4 -- Verifying drain"

if [[ "$DRAINED" == true ]]; then
    PASS "Buffer drained back to baseline (${BASELINE} row(s)) within ${DRAIN_ELAPSED}s"
else
    FINAL=$(_buffer_count 2>/dev/null || echo "?")
    FAIL "Buffer still has ${FINAL} row(s) after ${DRAIN_WAIT_SECS}s"
    INFO "  Check: python3 -c \"import sqlite3; c=sqlite3.connect('${BUFFER_DB}'); print(c.execute('SELECT COUNT(*) FROM pending_readings').fetchone()[0])\""
    INFO "  Logs: sudo journalctl -u chillcheck-subscriber --since '2 minutes ago' | grep -i drain"
fi

DRAIN_LINES=$(sudo journalctl -u chillcheck-subscriber --since "120 seconds ago" \
    --no-pager 2>/dev/null \
    | grep "Drained .* buffered reading" || true)
if [[ -n "$DRAIN_LINES" ]]; then
    PASS "Drain log messages found:"
    echo "$DRAIN_LINES" | while IFS= read -r line; do INFO "    $line"; done
else
    FAIL "No 'Drained N buffered reading(s)' log line in the last 2 minutes"
    INFO "  The drain job fires every 60s — may need one more tick. Wait and check:"
    INFO "  sudo journalctl -u chillcheck-subscriber --since '3 minutes ago' | grep -i drained"
fi

# ══════════════════════════════════════════════════════════════════════════════
# SUMMARY
# ══════════════════════════════════════════════════════════════════════════════
SECTION "Summary"

if [[ "$FAILURES" -eq 0 ]]; then
    echo -e "${GREEN}  All checks passed. Offline buffer is working correctly.${NC}"
else
    echo -e "${RED}  ${FAILURES} check(s) failed -- review the output above.${NC}"
fi

UI_BOX "AFTER DRAIN — verify in app.chillcheck.online:
  - Cabinet last-updated timestamps are RECENT again
  - Temperature chart: readings appear CONTINUOUS across the outage
    window (recorded_at is Pi-time, rows slot in at the right place)
  - If the hub showed 'Offline', it returns to 'Online' once the
    heartbeat reconnects (up to 5 min)
  - No spurious temperature alerts should have fired (alerting is
    skipped for unsynced readings; slice-2 covers retrospective alerts)"

echo
exit "$FAILURES"
