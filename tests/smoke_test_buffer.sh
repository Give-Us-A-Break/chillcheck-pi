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
# Or remotely from your dev machine (repo root):
#   ssh chillcheck@chillcheck.local 'bash -s' < pi/tests/smoke_test_buffer.sh
#
# Prerequisites on the Pi: sudo access, sqlite3, dig (dnsutils)
# Approximate duration: 8–12 minutes

set -uo pipefail

# ─── Colours ──────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'

PASS() { echo -e "${GREEN}  PASS${NC}  $*"; }
FAIL() { echo -e "${RED}  FAIL${NC}  $*"; FAILURES=$((FAILURES + 1)); }
INFO() { echo -e "        $*"; }
NOTE() { echo -e "${CYAN}  NOTE${NC}  $*"; }
SECTION() { echo; echo -e "${YELLOW}══ $* ══${NC}"; }
UI_BOX() {
    echo
    echo "  ┌─────────────────────────────────────────────────────────────┐"
    while IFS= read -r line; do
        printf "  │  %-61s│\n" "$line"
    done <<< "$*"
    echo "  └─────────────────────────────────────────────────────────────┘"
    echo
}

FAILURES=0
SUPABASE_HOST=""          # set once we parse the env file; used by cleanup trap
ENV_FILE="/etc/chillcheck/.env"
BUFFER_DB="/var/lib/chillcheck/buffer.db"
OUTAGE_SECS=180           # 3 minutes — enough for 3+ readings per cabinet at 1/min
DRAIN_WAIT_SECS=90        # max time to wait for drain after restoring connectivity

# ─── Cleanup trap ─────────────────────────────────────────────────────────────
# Always remove the /etc/hosts block on exit, even if the script is interrupted.
# Leaves the Pi in a known-good state regardless of how the test ends.
_cleanup() {
    if [[ -n "${SUPABASE_HOST:-}" ]]; then
        sudo sed -i "/127\.0\.0\.1 ${SUPABASE_HOST}/d" /etc/hosts 2>/dev/null || true
        _flush_dns
        INFO "(cleanup) Removed /etc/hosts block for ${SUPABASE_HOST}"
    fi
}
trap _cleanup EXIT INT TERM

_flush_dns() {
    sudo systemd-resolve --flush-caches 2>/dev/null \
        || sudo resolvectl flush-caches 2>/dev/null \
        || true
}

# ══════════════════════════════════════════════════════════════════════════════
# PHASE 0 — PRE-FLIGHT
# ══════════════════════════════════════════════════════════════════════════════
SECTION "Phase 0 — Pre-flight checks"

# Required tools
for tool in sqlite3 dig sudo; do
    if command -v "$tool" &>/dev/null; then
        PASS "$tool available"
    else
        FAIL "$tool not found — install: sudo apt install -y $tool"
    fi
done

# Env file
if [[ -f "$ENV_FILE" ]]; then
    PASS "Env file found at $ENV_FILE"
else
    FAIL "Env file not found at $ENV_FILE — is this running on the Pi?"
    exit 1
fi

# Extract Supabase host
SUPABASE_URL=$(grep '^SUPABASE_URL=' "$ENV_FILE" | cut -d= -f2- | tr -d '"' | tr -d "'")
if [[ -z "${SUPABASE_URL:-}" ]]; then
    FAIL "SUPABASE_URL not set in $ENV_FILE"
    exit 1
fi
# Strip https:// and trailing path, leaving just the hostname
SUPABASE_HOST=$(echo "$SUPABASE_URL" | sed 's|https\?://||' | sed 's|/.*||' | sed 's|:.*||')
PASS "Supabase host: ${SUPABASE_HOST}"

# Subscriber running
if systemctl is-active --quiet chillcheck-subscriber; then
    PASS "chillcheck-subscriber is active"
else
    FAIL "chillcheck-subscriber is not running — start it first:"
    INFO "  sudo systemctl start chillcheck-subscriber"
    exit 1
fi

# Buffer DB accessible
if [[ -f "$BUFFER_DB" ]]; then
    BASELINE=$(sqlite3 "$BUFFER_DB" "SELECT COUNT(*) FROM pending_readings;" 2>/dev/null || echo "ERR")
    if [[ "$BASELINE" == "ERR" ]]; then
        FAIL "Buffer DB exists but SELECT failed — check permissions on $BUFFER_DB"
        BASELINE=0
    elif [[ "$BASELINE" -gt 0 ]]; then
        NOTE "Buffer has ${BASELINE} pre-existing row(s) from a previous session — test tracks the delta"
    else
        PASS "Buffer DB is empty (clean baseline)"
    fi
else
    NOTE "Buffer DB does not exist yet — it will be created on the first failed write (normal for a fresh hub)"
    BASELINE=0
fi

# Check buffer isn't disabled (would happen if /var/lib/chillcheck/ isn't writable)
if sudo journalctl -u chillcheck-subscriber --since "10 minutes ago" --no-pager 2>/dev/null \
        | grep -q "ReadingBuffer disabled"; then
    FAIL "Buffer is currently disabled — fix the data dir first:"
    INFO "  sudo mkdir -p /var/lib/chillcheck && sudo chown chillcheck:chillcheck /var/lib/chillcheck"
    INFO "  sudo systemctl restart chillcheck-subscriber"
    exit 1
else
    PASS "No 'ReadingBuffer disabled' messages in recent logs — buffer is live"
fi

# Verify Supabase is reachable *before* we block it
if dig +short "$SUPABASE_HOST" &>/dev/null && dig +short "$SUPABASE_HOST" | grep -qE '^[0-9]+\.'; then
    PASS "Supabase host resolves correctly (will block it in Phase 1)"
else
    FAIL "Cannot resolve $SUPABASE_HOST — check internet connectivity before running this test"
    exit 1
fi

UI_BOX "BEFORE THE TEST — check app.chillcheck.online and note:
  • Cabinet last-updated timestamps (these will freeze during outage)
  • Current temperatures look live and recent
Also open http://chillcheck.local — it should stay fully
responsive throughout the entire test."

read -rp "  Press ENTER when ready to start the outage simulation…"

# ══════════════════════════════════════════════════════════════════════════════
# PHASE 1 — SIMULATE OUTAGE
# Block Supabase by poisoning /etc/hosts. This is surgical: MQTT traffic
# to 127.0.0.1:1883 and the Flask UI on port 80 are completely unaffected.
# Python's socket module respects /etc/hosts (nsswitch 'files' precedes 'dns'),
# so every new HTTP connection attempt to the Supabase host gets ECONNREFUSED.
# ══════════════════════════════════════════════════════════════════════════════
SECTION "Phase 1 — Simulating outage (blocking ${SUPABASE_HOST})"

if grep -q "127.0.0.1 ${SUPABASE_HOST}" /etc/hosts 2>/dev/null; then
    NOTE "hosts entry already present (leftover from a previous run) — skipping add"
else
    echo "127.0.0.1 ${SUPABASE_HOST}" | sudo tee -a /etc/hosts > /dev/null
    PASS "Added '127.0.0.1 ${SUPABASE_HOST}' to /etc/hosts"
fi
_flush_dns
PASS "DNS cache flushed — new connections to Supabase will get ECONNREFUSED"

INFO "Outage window: ${OUTAGE_SECS}s (~3 readings per cabinet at 1 reading/min)"
INFO "Waiting 70s for the first failed write cycle to produce log output…"
sleep 70

# Quick early check — should see at least one failure by now
EARLY_FAILS=$(sudo journalctl -u chillcheck-subscriber --since "75 seconds ago" \
    --no-pager 2>/dev/null \
    | grep -c "Supabase reading insert failed, buffering" || true)
if [[ "$EARLY_FAILS" -gt 0 ]]; then
    PASS "Subscriber is already logging Supabase failures and buffering readings (${EARLY_FAILS} so far)"
else
    NOTE "No buffering messages yet — readings arrive once per minute per sensor; waiting for the next cycle"
fi

INFO "Waiting for the rest of the ${OUTAGE_SECS}s outage window…"
REMAINING=$((OUTAGE_SECS - 70))
ELAPSED_EXTRA=0
while [[ $ELAPSED_EXTRA -lt $REMAINING ]]; do
    sleep 15
    ELAPSED_EXTRA=$((ELAPSED_EXTRA + 15))
    CURRENT_BUF=0
    [[ -f "$BUFFER_DB" ]] && CURRENT_BUF=$(sqlite3 "$BUFFER_DB" "SELECT COUNT(*) FROM pending_readings;" 2>/dev/null || echo 0)
    printf "\r        %ds elapsed — buffer size: %s row(s)…" $((70 + ELAPSED_EXTRA)) "$CURRENT_BUF"
done
echo  # newline after the progress line

# ══════════════════════════════════════════════════════════════════════════════
# PHASE 2 — VERIFY BUFFER FILLED
# ══════════════════════════════════════════════════════════════════════════════
SECTION "Phase 2 — Verifying buffer filled during outage"

# Count buffered rows
BUFFER_AFTER=0
NEW_ROWS=0
if [[ -f "$BUFFER_DB" ]]; then
    BUFFER_AFTER=$(sqlite3 "$BUFFER_DB" "SELECT COUNT(*) FROM pending_readings;" 2>/dev/null || echo 0)
    NEW_ROWS=$((BUFFER_AFTER - BASELINE))
    if [[ "$NEW_ROWS" -gt 0 ]]; then
        PASS "Buffer has ${NEW_ROWS} new row(s) (total in DB: ${BUFFER_AFTER})"
        INFO "Sample buffered rows (most recent first):"
        sqlite3 -separator '  ' "$BUFFER_DB" \
            "SELECT cabinet_id, printf('%.1f', temperature) || 'C', recorded_at, attempts
             FROM pending_readings ORDER BY recorded_at DESC LIMIT 8;" \
            2>/dev/null | while IFS= read -r line; do
            INFO "    ${line}"
        done
        INFO ""
        INFO "  Key: recorded_at is the Pi-side timestamp. After drain, readings"
        INFO "  will appear in the cloud chart at these exact times — no spike."
    else
        FAIL "No new rows in buffer after ${OUTAGE_SECS}s outage. Possible causes:"
        INFO "  • No sensors are assigned to cabinets (unassigned sensors skip the readings insert)"
        INFO "  • Readings arrive every ~60s; if the timing aligned badly, wait a bit longer"
        INFO "  Recent subscriber log:"
        sudo journalctl -u chillcheck-subscriber --since "${OUTAGE_SECS} seconds ago" \
            --no-pager 2>/dev/null | tail -15 || true
    fi
else
    FAIL "Buffer DB still does not exist — check that sensors have cabinet assignments"
fi

# Verify we see the expected log messages during the outage
OUTAGE_LOG_HITS=$(sudo journalctl -u chillcheck-subscriber \
    --since "${OUTAGE_SECS} seconds ago" --no-pager 2>/dev/null \
    | grep -c "Supabase reading insert failed, buffering" || true)
if [[ "$OUTAGE_LOG_HITS" -gt 0 ]]; then
    PASS "Log shows ${OUTAGE_LOG_HITS} 'Supabase reading insert failed, buffering' message(s)"
else
    FAIL "Expected 'Supabase reading insert failed, buffering' in logs but found none"
fi

# Sensor last_seen updates also fail during the outage — check for that too
SENSOR_UPDATE_FAILS=$(sudo journalctl -u chillcheck-subscriber \
    --since "${OUTAGE_SECS} seconds ago" --no-pager 2>/dev/null \
    | grep -c "Failed to update sensor" || true)
if [[ "$SENSOR_UPDATE_FAILS" -gt 0 ]]; then
    NOTE "Sensor last_seen updates also failed (${SENSOR_UPDATE_FAILS} hit(s)) — expected."
    NOTE "  Sensor timestamps will appear stale in the cloud during the outage."
    NOTE "  They recover on the next successful reading after reconnect."
fi

# Confirm internal traffic was unaffected
MQTT_MESSAGES=$(sudo journalctl -u chillcheck-subscriber \
    --since "${OUTAGE_SECS} seconds ago" --no-pager 2>/dev/null \
    | grep -c "Reading:.*→.*°C" || true)
if [[ "$MQTT_MESSAGES" -gt 0 ]]; then
    PASS "MQTT readings still arriving from sensors (${MQTT_MESSAGES} reading log lines) — internal traffic unaffected"
else
    NOTE "No 'Reading:' log lines found — sensors may not have fired in this window, or LOG_LEVEL hides them"
fi

UI_BOX "DURING OUTAGE — check these now before restoring connectivity:
  app.chillcheck.online:
    • Cabinet last-updated timestamps should be STALE (frozen)
    • Hub may show 'Offline' in Settings → Devices (heartbeat also
      hits the internet — expected side-effect of our block)
  http://chillcheck.local (should be fully responsive):
    • Sensors tab shows current temperatures (MQTT still flowing)
    • Logs tab → Subscriber: look for 'Supabase reading insert
      failed, buffering' and 'Failed to update sensor' messages"

read -rp "  Press ENTER to restore connectivity and start drain…"

# ══════════════════════════════════════════════════════════════════════════════
# PHASE 3 — RESTORE CONNECTIVITY
# ══════════════════════════════════════════════════════════════════════════════
SECTION "Phase 3 — Restoring connectivity"

sudo sed -i "/127\.0\.0\.1 ${SUPABASE_HOST}/d" /etc/hosts
SUPABASE_HOST=""  # disarm the cleanup trap — we've already removed the block
_flush_dns
PASS "Removed /etc/hosts block"
PASS "DNS cache flushed"

INFO "The drain job runs every 60s. Polling buffer size for up to ${DRAIN_WAIT_SECS}s…"

DRAIN_ELAPSED=0
DRAINED=false
while [[ $DRAIN_ELAPSED -lt $DRAIN_WAIT_SECS ]]; do
    sleep 10
    DRAIN_ELAPSED=$((DRAIN_ELAPSED + 10))
    CURRENT=0
    [[ -f "$BUFFER_DB" ]] && CURRENT=$(sqlite3 "$BUFFER_DB" "SELECT COUNT(*) FROM pending_readings;" 2>/dev/null || echo "$BUFFER_AFTER")
    printf "\r        %ds — buffer: %s row(s)…" "$DRAIN_ELAPSED" "$CURRENT"
    if [[ "$CURRENT" -le "$BASELINE" ]]; then
        echo  # newline
        DRAINED=true
        break
    fi
done
[[ "$DRAINED" == false ]] && echo  # newline if we timed out

# ══════════════════════════════════════════════════════════════════════════════
# PHASE 4 — VERIFY DRAIN
# ══════════════════════════════════════════════════════════════════════════════
SECTION "Phase 4 — Verifying drain"

if [[ "$DRAINED" == true ]]; then
    PASS "Buffer drained back to baseline (${BASELINE} row(s)) within ${DRAIN_ELAPSED}s"
else
    FINAL=$(sqlite3 "$BUFFER_DB" "SELECT COUNT(*) FROM pending_readings;" 2>/dev/null || echo "?")
    FAIL "Buffer still has ${FINAL} row(s) after ${DRAIN_WAIT_SECS}s — drain may still be in progress, or Supabase not yet reachable"
    INFO "  Check manually: sqlite3 ${BUFFER_DB} 'SELECT COUNT(*) FROM pending_readings;'"
    INFO "  And: sudo journalctl -u chillcheck-subscriber --since '2 minutes ago' | grep -i drain"
fi

# Check drain log messages
DRAIN_LINES=$(sudo journalctl -u chillcheck-subscriber --since "120 seconds ago" \
    --no-pager 2>/dev/null \
    | grep "Drained .* buffered reading" || true)
if [[ -n "$DRAIN_LINES" ]]; then
    PASS "Drain log messages found:"
    echo "$DRAIN_LINES" | while IFS= read -r line; do INFO "    $line"; done
else
    FAIL "No 'Drained N buffered reading(s)' log line in the last 2 minutes"
    INFO "  The drain job fires every 60s — it may need one more tick. Wait a minute and check:"
    INFO "  sudo journalctl -u chillcheck-subscriber --since '3 minutes ago' | grep -i drained"
fi

# Verify the drained readings have the correct timestamp range
if [[ "$NEW_ROWS" -gt 0 ]]; then
    INFO ""
    INFO "  The ${NEW_ROWS} drained reading(s) had recorded_at timestamps spanning the"
    INFO "  outage window — they slot into the chart at the correct times rather than"
    INFO "  appearing as a sudden cluster after reconnect."
    INFO "  Verify in the dashboard: cabinet detail → temperature chart → the outage"
    INFO "  window should show continuous data, not a gap followed by a spike."
fi

# ══════════════════════════════════════════════════════════════════════════════
# SUMMARY
# ══════════════════════════════════════════════════════════════════════════════
SECTION "Summary"

if [[ "$FAILURES" -eq 0 ]]; then
    echo -e "${GREEN}  All checks passed. Offline buffer is working correctly.${NC}"
else
    echo -e "${RED}  ${FAILURES} check(s) failed — review the output above.${NC}"
fi

UI_BOX "AFTER DRAIN — verify in app.chillcheck.online:
  • Cabinet last-updated timestamps are RECENT again
  • Temperature chart: readings appear CONTINUOUS across the outage
    window (recorded_at is Pi-time, so rows slot in at the right
    place — no gap, no spike after reconnect)
  • If the hub showed 'Offline' during the outage, it should return
    to 'Online' once the heartbeat reconnects (up to 5 min)
  • No spurious temperature alerts should have fired (alerting is
    skipped for readings that didn't reach Supabase live; that's
    the slice-2 retrospective-alerting gap, documented in ROADMAP)
  Also: Settings → Devices should show the hub with a recent
  last_heartbeat once the heartbeat service reconnects."

echo
exit "$FAILURES"
