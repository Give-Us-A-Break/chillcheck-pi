#!/usr/bin/env bash
# smoke_test_buffer.sh — Offline reading buffer smoke test (Epic 10, slice 1)
#
# Simulates an internet outage by poisoning /etc/hosts for the Supabase host.
# Internal traffic (MQTT on 127.0.0.1, local Flask UI on port 80) keeps
# flowing throughout — we're testing outbound cloud connectivity only.
#
# Must run as root (it writes to /etc/hosts). Copy it to the Pi first,
# then run with sudo:
#
#   PowerShell (from repo root):
#     (Get-Content pi\tests\smoke_test_buffer.sh -Raw) -replace "`r`n","`n" | ssh chillcheck@chillcheck.local 'cat > /tmp/smt.sh'
#     ssh -t chillcheck@chillcheck.local 'sudo bash /tmp/smt.sh'
#
#   bash/zsh:
#     scp pi/tests/smoke_test_buffer.sh chillcheck@chillcheck.local:/tmp/smt.sh
#     ssh -t chillcheck@chillcheck.local 'sudo bash /tmp/smt.sh'
#
# Prerequisites: python3 (already required by the subscriber — no extra packages)
# Approximate duration: 8–12 minutes

set -uo pipefail

# ─── Must run as root ────────────────────────────────────────────────────────
if [[ "$(id -u)" != "0" ]]; then
    echo "This script must run as root (it modifies /etc/hosts)."
    echo ""
    echo "Copy the script to the Pi and run:"
    echo "  ssh -t chillcheck@chillcheck.local 'sudo bash /tmp/smt.sh'"
    echo ""
    echo "Or on Windows (PowerShell):"
    echo "  (Get-Content pi\\tests\\smoke_test_buffer.sh -Raw) -replace \"\`r\`n\",\"\`n\" | ssh chillcheck@chillcheck.local 'cat > /tmp/smt.sh'"
    echo "  ssh -t chillcheck@chillcheck.local 'sudo bash /tmp/smt.sh'"
    exit 1
fi

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
    while IFS= read -r line; do echo "  | $line"; done <<< "$*"
    echo
}

FAILURES=0
SUPABASE_HOST=""
ENV_FILE="/etc/chillcheck/.env"
BUFFER_DB="/var/lib/chillcheck/buffer.db"
OUTAGE_SECS=180    # 3 minutes — enough for 3+ readings per cabinet at 1/min
DRAIN_WAIT_SECS=90 # max wait for drain after restoring connectivity

# ─── Python helpers ───────────────────────────────────────────────────────────
# Uses Python's built-in modules — no extra packages needed on the Pi.

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
    python3 -c "import socket; socket.getaddrinfo('$1', 443, socket.AF_INET)" 2>/dev/null
}

# Convert a Unix timestamp to a journalctl-compatible datetime string
_ts_to_since() {
    date -d "@$1" '+%Y-%m-%d %H:%M:%S' 2>/dev/null \
        || python3 -c "from datetime import datetime; print(datetime.fromtimestamp($1).strftime('%Y-%m-%d %H:%M:%S'))"
}

# ─── Cleanup trap ─────────────────────────────────────────────────────────────
_flush_dns() {
    systemd-resolve --flush-caches 2>/dev/null \
        || resolvectl flush-caches 2>/dev/null \
        || true
}

_cleanup() {
    if [[ -n "${SUPABASE_HOST:-}" ]]; then
        sed -i "/127\.0\.0\.1 ${SUPABASE_HOST}/d" /etc/hosts 2>/dev/null || true
        _flush_dns
        INFO "(cleanup) Removed /etc/hosts block for ${SUPABASE_HOST}"
    fi
}
trap _cleanup EXIT INT TERM

# ══════════════════════════════════════════════════════════════════════════════
# PHASE 0 — PRE-FLIGHT
# ══════════════════════════════════════════════════════════════════════════════
SECTION "Phase 0 -- Pre-flight checks"

if command -v python3 &>/dev/null; then
    PASS "python3 available"
else
    FAIL "python3 not found"
fi

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
    FAIL "chillcheck-subscriber is not running"
    INFO "  sudo systemctl start chillcheck-subscriber"
    exit 1
fi

BASELINE=$(_buffer_count)
if [[ "$BASELINE" == "ERR" ]]; then
    FAIL "Buffer DB exists but Python could not query it — check permissions on $BUFFER_DB"
    BASELINE=0
elif [[ -f "$BUFFER_DB" && "$BASELINE" -gt 0 ]]; then
    NOTE "Buffer has ${BASELINE} pre-existing row(s) — test tracks the delta"
elif [[ -f "$BUFFER_DB" ]]; then
    PASS "Buffer DB is empty (clean baseline)"
else
    NOTE "Buffer DB does not exist yet — will be created on first failed write (normal)"
    BASELINE=0
fi

if journalctl -u chillcheck-subscriber --since "10 minutes ago" --no-pager 2>/dev/null \
        | grep -q "ReadingBuffer disabled"; then
    FAIL "Buffer is currently disabled — fix data dir permissions first:"
    INFO "  sudo mkdir -p /var/lib/chillcheck && sudo chown chillcheck:chillcheck /var/lib/chillcheck"
    INFO "  sudo systemctl restart chillcheck-subscriber"
    exit 1
else
    PASS "No 'ReadingBuffer disabled' messages in recent logs"
fi

if _can_resolve "$SUPABASE_HOST"; then
    PASS "Supabase host resolves (will block it in Phase 1)"
else
    FAIL "Cannot resolve ${SUPABASE_HOST} — check internet connectivity before running this test"
    exit 1
fi

UI_BOX "BEFORE THE TEST -- check app.chillcheck.online and note:
  - Cabinet last-updated timestamps (these will freeze during outage)
  - Current temperatures look live and recent
Also open http://chillcheck.local -- should stay fully
responsive throughout the entire test."

read -rp "  Press ENTER when ready to start the outage simulation..."

# ══════════════════════════════════════════════════════════════════════════════
# PHASE 1 — SIMULATE OUTAGE
# Poisons /etc/hosts for the Supabase hostname. Python's socket.getaddrinfo()
# checks /etc/hosts (via nsswitch 'files' before 'dns') so new connections get
# ECONNREFUSED to 127.0.0.1. MQTT on 127.0.0.1:1883 is completely unaffected.
# ══════════════════════════════════════════════════════════════════════════════
SECTION "Phase 1 -- Simulating outage (blocking ${SUPABASE_HOST})"

OUTAGE_START_TS=$(date +%s)

if grep -q "127.0.0.1 ${SUPABASE_HOST}" /etc/hosts 2>/dev/null; then
    NOTE "hosts entry already present (leftover from a previous run) — skipping add"
else
    echo "127.0.0.1 ${SUPABASE_HOST}" >> /etc/hosts
    if grep -q "127.0.0.1 ${SUPABASE_HOST}" /etc/hosts; then
        PASS "Added '127.0.0.1 ${SUPABASE_HOST}' to /etc/hosts"
    else
        FAIL "Failed to write /etc/hosts — check this script is running as root"
        exit 1
    fi
fi

_flush_dns
PASS "DNS cache flushed"

# Verify the block is actually working before counting on it
if _can_resolve "$SUPABASE_HOST"; then
    FAIL "Supabase host still resolves after /etc/hosts block — DNS may be bypassing hosts file"
    INFO "  Check /etc/nsswitch.conf — 'hosts:' line must include 'files' before 'dns'"
    exit 1
else
    PASS "Confirmed: Supabase host no longer resolves (block is active)"
fi

INFO "Outage window: ${OUTAGE_SECS}s (~3 readings per cabinet at 1 reading/min)"
INFO "Waiting 70s for the first failed write cycle to produce log output..."
sleep 70

EARLY_SINCE=$(_ts_to_since $((OUTAGE_START_TS - 5)))
EARLY_FAILS=$(journalctl -u chillcheck-subscriber --since "$EARLY_SINCE" \
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

OUTAGE_SINCE=$(_ts_to_since $((OUTAGE_START_TS - 5)))

BUFFER_AFTER=$(_buffer_count)
NEW_ROWS=$((BUFFER_AFTER - BASELINE))
if [[ "$BUFFER_AFTER" != "ERR" && "$NEW_ROWS" -gt 0 ]]; then
    PASS "Buffer has ${NEW_ROWS} new row(s) (total: ${BUFFER_AFTER})"
    INFO "Sample buffered rows (most recent first):"
    _buffer_rows
    INFO ""
    INFO "  recorded_at is the Pi-side timestamp. After drain, readings slot"
    INFO "  into the chart at these exact times -- no spike, no gap."
else
    FAIL "No new rows in buffer after ${OUTAGE_SECS}s. Possible causes:"
    INFO "  - No sensors are assigned to cabinets (unassigned sensors skip the readings insert)"
    INFO "  - Readings arrive every ~60s; check the recent subscriber log below"
    INFO ""
    INFO "  Recent subscriber log:"
    journalctl -u chillcheck-subscriber --since "$OUTAGE_SINCE" --no-pager 2>/dev/null | tail -20 || true
fi

OUTAGE_LOG_HITS=$(journalctl -u chillcheck-subscriber --since "$OUTAGE_SINCE" \
    --no-pager 2>/dev/null \
    | grep -c "Supabase reading insert failed, buffering" || true)
if [[ "$OUTAGE_LOG_HITS" -gt 0 ]]; then
    PASS "Log shows ${OUTAGE_LOG_HITS} 'Supabase reading insert failed, buffering' message(s)"
else
    FAIL "Expected 'Supabase reading insert failed, buffering' in logs but found none"
fi

SENSOR_UPDATE_FAILS=$(journalctl -u chillcheck-subscriber --since "$OUTAGE_SINCE" \
    --no-pager 2>/dev/null \
    | grep -c "Failed to update sensor" || true)
if [[ "$SENSOR_UPDATE_FAILS" -gt 0 ]]; then
    NOTE "Sensor last_seen updates also failed (${SENSOR_UPDATE_FAILS}) -- expected."
    NOTE "  Sensor timestamps look stale in the cloud. Recover on next successful reading."
fi

MQTT_MESSAGES=$(journalctl -u chillcheck-subscriber --since "$OUTAGE_SINCE" \
    --no-pager 2>/dev/null \
    | grep -c "Reading:" || true)
if [[ "$MQTT_MESSAGES" -gt 0 ]]; then
    PASS "MQTT readings still arriving (${MQTT_MESSAGES} log lines) -- internal traffic unaffected"
else
    NOTE "No 'Reading:' log lines found -- sensors fire ~1/min, may have been missed"
fi

UI_BOX "DURING OUTAGE -- check these now before restoring connectivity:
  app.chillcheck.online:
    - Cabinet last-updated timestamps should be STALE (frozen)
    - Hub may show 'Offline' in Settings > Devices (heartbeat also
      hits the internet -- expected side-effect of our block)
  http://chillcheck.local (should be fully responsive):
    - Sensors tab shows current temperatures (MQTT still flowing)
    - Logs tab > Subscriber: 'Supabase reading insert failed,
      buffering' and 'Failed to update sensor' messages"

read -rp "  Press ENTER to restore connectivity and start drain..."

# ══════════════════════════════════════════════════════════════════════════════
# PHASE 3 — RESTORE CONNECTIVITY
# ══════════════════════════════════════════════════════════════════════════════
SECTION "Phase 3 -- Restoring connectivity"

RESTORE_TS=$(date +%s)

sed -i "/127\.0\.0\.1 ${SUPABASE_HOST}/d" /etc/hosts
SUPABASE_HOST=""  # disarm the cleanup trap
_flush_dns

if _can_resolve "${SUPABASE_HOST:-${SUPABASE_URL}}"; then
    PASS "Supabase host resolves again (block removed)"
else
    NOTE "Host not yet resolving — DNS may need a moment to catch up"
fi

PASS "Connectivity restored"

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
    INFO "  Check live: python3 -c \"import sqlite3; c=sqlite3.connect('${BUFFER_DB}'); print(c.execute('SELECT COUNT(*) FROM pending_readings').fetchone()[0])\""
    INFO "  Logs:       journalctl -u chillcheck-subscriber --since '3 minutes ago' | grep -i drain"
fi

DRAIN_SINCE=$(_ts_to_since "$RESTORE_TS")
DRAIN_LINES=$(journalctl -u chillcheck-subscriber --since "$DRAIN_SINCE" \
    --no-pager 2>/dev/null \
    | grep "Drained .* buffered reading" || true)
if [[ -n "$DRAIN_LINES" ]]; then
    PASS "Drain log messages found:"
    echo "$DRAIN_LINES" | while IFS= read -r line; do INFO "    $line"; done
else
    FAIL "No 'Drained N buffered reading(s)' log line since connectivity restored"
    INFO "  Drain job fires every 60s -- may need one more tick:"
    INFO "  journalctl -u chillcheck-subscriber --since '3 minutes ago' | grep -i drained"
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

UI_BOX "AFTER DRAIN -- verify in app.chillcheck.online:
  - Cabinet last-updated timestamps are RECENT again
  - Temperature chart: readings appear CONTINUOUS across the outage
    window (recorded_at is Pi-time, rows slot in at the right place)
  - If hub showed 'Offline', returns to 'Online' once heartbeat
    reconnects (up to 5 min)
  - No spurious temperature alerts should have fired (alerting
    skipped for unsynced readings; slice-2 covers retrospective alerts)"

echo
exit "$FAILURES"
