#!/usr/bin/env bash
# smoke_test_buffer.sh — Offline reading buffer smoke test (Epic 10, slice 1)
#
# Simulates an internet outage by blocking outbound HTTPS to Supabase using
# iptables. Internal traffic (MQTT on 127.0.0.1, local Flask UI on port 80)
# is completely unaffected — we're blocking the specific remote IPs only.
#
# Must run as root. Copy to the Pi, then:
#
#   PowerShell (from repo root):
#     (Get-Content pi\tests\smoke_test_buffer.sh -Raw) -replace "`r`n","`n" | ssh chillcheck@chillcheck.local 'cat > /tmp/smt.sh'
#     ssh -t chillcheck@chillcheck.local 'sudo bash /tmp/smt.sh'
#
#   bash/zsh:
#     scp pi/tests/smoke_test_buffer.sh chillcheck@chillcheck.local:/tmp/smt.sh
#     ssh -t chillcheck@chillcheck.local 'sudo bash /tmp/smt.sh'
#
# Prerequisites: python3, iptables (both present on stock Pi OS)
# Approximate duration: 8–12 minutes

set -uo pipefail

if [[ "$(id -u)" != "0" ]]; then
    echo "This script must run as root (it modifies iptables)."
    echo ""
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
    echo; echo "  --- UI CHECK ---"
    while IFS= read -r line; do echo "  | $line"; done <<< "$*"
    echo
}

FAILURES=0
ENV_FILE="/etc/chillcheck/.env"
BUFFER_DB="/var/lib/chillcheck/buffer.db"
DRAIN_WAIT_SECS=90 # max wait for drain after restoring connectivity
SUPABASE_HOST=""
BLOCKED_IPS=()     # populated by _block_supabase, used by cleanup trap

# ─── Python helpers ───────────────────────────────────────────────────────────
_buffer_count() {
    [[ -f "$BUFFER_DB" ]] || { echo 0; return; }
    python3 - "$BUFFER_DB" 2>/dev/null <<'PYEOF'
import sqlite3, sys
try:
    conn = sqlite3.connect(sys.argv[1])
    print(conn.execute("SELECT COUNT(*) FROM pending_readings").fetchone()[0])
except Exception:
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

_resolve_ips() {
    python3 -c "
import socket
ips = sorted(set(r[4][0] for r in socket.getaddrinfo('$1', 443, socket.AF_INET)))
print('\n'.join(ips))
" 2>/dev/null
}

_can_connect() {
    # Returns 0 if a TCP connection to port 443 succeeds, 1 if it fails/times out
    python3 -c "
import socket, sys
try:
    s = socket.create_connection(('$1', 443), timeout=4)
    s.close()
    sys.exit(0)
except Exception:
    sys.exit(1)
" 2>/dev/null
}

_ts_since() {
    # Convert a Unix timestamp to a journalctl --since string
    date -d "@$1" '+%Y-%m-%d %H:%M:%S' 2>/dev/null \
        || python3 -c "from datetime import datetime; print(datetime.fromtimestamp($1).strftime('%Y-%m-%d %H:%M:%S'))"
}

# ─── iptables block / unblock ─────────────────────────────────────────────────
_block_supabase() {
    # Resolve IPs *before* blocking so we know exactly what to block
    mapfile -t BLOCKED_IPS < <(_resolve_ips "$SUPABASE_HOST")
    if [[ ${#BLOCKED_IPS[@]} -eq 0 ]]; then
        return 1
    fi
    for ip in "${BLOCKED_IPS[@]}"; do
        iptables -I OUTPUT -d "$ip" -p tcp --dport 443 -j REJECT
    done
}

_unblock_supabase() {
    for ip in "${BLOCKED_IPS[@]:-}"; do
        iptables -D OUTPUT -d "$ip" -p tcp --dport 443 -j REJECT 2>/dev/null || true
    done
    BLOCKED_IPS=()
}

# ─── Cleanup trap ─────────────────────────────────────────────────────────────
_cleanup() {
    if [[ ${#BLOCKED_IPS[@]} -gt 0 ]]; then
        _unblock_supabase
        INFO "(cleanup) Removed iptables block for ${SUPABASE_HOST}"
    fi
}
trap _cleanup EXIT INT TERM

# ══════════════════════════════════════════════════════════════════════════════
# PHASE 0 — PRE-FLIGHT
# ══════════════════════════════════════════════════════════════════════════════
SECTION "Phase 0 -- Pre-flight checks"

for tool in python3 iptables; do
    if command -v "$tool" &>/dev/null; then PASS "$tool available"
    else FAIL "$tool not found"; fi
done

if [[ -f "$ENV_FILE" ]]; then PASS "Env file found at $ENV_FILE"
else FAIL "Env file not found at $ENV_FILE — is this running on the Pi?"; exit 1; fi

SUPABASE_URL=$(grep '^SUPABASE_URL=' "$ENV_FILE" | cut -d= -f2- | tr -d '"' | tr -d "'")
if [[ -z "${SUPABASE_URL:-}" ]]; then FAIL "SUPABASE_URL not set in $ENV_FILE"; exit 1; fi
SUPABASE_HOST=$(echo "$SUPABASE_URL" | sed 's|https\?://||' | sed 's|/.*||' | sed 's|:.*||')
PASS "Supabase host: ${SUPABASE_HOST}"

if systemctl is-active --quiet chillcheck-subscriber; then PASS "chillcheck-subscriber is active"
else FAIL "chillcheck-subscriber is not running"; exit 1; fi

BASELINE=$(_buffer_count)
if [[ "$BASELINE" == "ERR" ]]; then
    FAIL "Buffer DB exists but Python could not query it — check permissions on $BUFFER_DB"
    BASELINE=0
elif [[ -f "$BUFFER_DB" && "$BASELINE" -gt 0 ]]; then
    NOTE "Buffer has ${BASELINE} pre-existing row(s) — test tracks the delta"
elif [[ -f "$BUFFER_DB" ]]; then
    PASS "Buffer DB is empty (clean baseline)"
else
    NOTE "Buffer DB not yet created — normal for a hub with no prior outages"
    BASELINE=0
fi

if journalctl -u chillcheck-subscriber --since "10 minutes ago" --no-pager 2>/dev/null \
        | grep -q "ReadingBuffer disabled"; then
    FAIL "Buffer is currently disabled — fix data dir permissions first"
    exit 1
else
    PASS "No 'ReadingBuffer disabled' messages in recent logs"
fi

if _can_connect "$SUPABASE_HOST"; then PASS "Supabase is reachable (will block it in Phase 1)"
else FAIL "Cannot reach ${SUPABASE_HOST}:443 — check internet connectivity"; exit 1; fi

UI_BOX "BEFORE THE TEST -- check app.chillcheck.online and note:
  - Cabinet last-updated timestamps (these will freeze during outage)
  - Current temperatures look live and recent
Also open http://chillcheck.local -- should stay fully
responsive throughout the entire test."

read -rp "  Press ENTER when ready to start the outage simulation..."

# ══════════════════════════════════════════════════════════════════════════════
# PHASE 1 — SIMULATE OUTAGE
# iptables REJECTs outbound TCP:443 to Supabase's IPs. This works at the
# network level regardless of DNS caching or nsswitch.conf configuration.
# MQTT on 127.0.0.1:1883 and Flask on port 80 are completely unaffected.
# ══════════════════════════════════════════════════════════════════════════════
SECTION "Phase 1 -- Simulating outage (blocking ${SUPABASE_HOST})"

OUTAGE_START_TS=$(date +%s)

if _block_supabase; then
    PASS "iptables: blocked ${#BLOCKED_IPS[@]} IP(s) — $(IFS=', '; echo "${BLOCKED_IPS[*]}")"
else
    FAIL "Could not resolve ${SUPABASE_HOST} to block — check connectivity"
    exit 1
fi

# Verify the block is actually working
if _can_connect "$SUPABASE_HOST"; then
    FAIL "Supabase still reachable after iptables block — check iptables OUTPUT chain"
    _unblock_supabase
    exit 1
else
    PASS "Confirmed: Supabase port 443 is now blocked (connection refused)"
fi

INFO ""
INFO "  IMPORTANT: pull the temperature probe OUT of the fridge/freezer now."
INFO "  The sensor only reports when temperature changes — with a stable cold"
INFO "  probe it can be silent for 20+ minutes and nothing will buffer."
INFO "  Once the temperature is rising you'll see buffer rows counting up below."
INFO ""
INFO "  Leave the outage running as long as you like. When you're satisfied"
INFO "  (check app.chillcheck.online for frozen timestamps, http://chillcheck.local"
INFO "  for live MQTT readings, and the buffer count below), press ENTER to restore."
INFO ""

while true; do
    ELAPSED=$(( $(date +%s) - OUTAGE_START_TS ))
    BUF=$(_buffer_count 2>/dev/null || echo "?")
    printf "\r        [%ds] buffer: %s row(s) queued — press ENTER to restore connectivity..." \
        "$ELAPSED" "$BUF"
    if read -t 10 -r -s; then
        break
    fi
done
echo

# ══════════════════════════════════════════════════════════════════════════════
# PHASE 2 — VERIFY BUFFER FILLED
# ══════════════════════════════════════════════════════════════════════════════
SECTION "Phase 2 -- Verifying buffer filled during outage"

OUTAGE_SINCE=$(_ts_since $((OUTAGE_START_TS - 5)))
OUTAGE_SECS=$(( $(date +%s) - OUTAGE_START_TS ))

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
    FAIL "No new rows in buffer after ${OUTAGE_SECS}s."
    INFO "  If the probe was in a stable cold environment the sensor may not have"
    INFO "  sent any readings (it reports on temperature change, not on a fixed timer)."
    INFO "  Re-run with the probe out of the fridge from the start of Phase 1."
    INFO ""
    INFO "  Recent subscriber log:"
    journalctl -u chillcheck-subscriber --since "$OUTAGE_SINCE" --no-pager 2>/dev/null | tail -20 || true
fi

OUTAGE_LOG_HITS=$(journalctl -u chillcheck-subscriber --since "$OUTAGE_SINCE" \
    --no-pager 2>/dev/null \
    | grep -c "Supabase reading insert failed, buffering" || true)
if [[ "$OUTAGE_LOG_HITS" -gt 0 ]]; then
    PASS "Log: ${OUTAGE_LOG_HITS} 'Supabase reading insert failed, buffering' message(s)"
else
    NOTE "Log check inconclusive: 'Supabase reading insert failed, buffering' not found via journalctl"
    INFO "  This can happen when journald hasn't flushed yet, or the --since timestamp format"
    INFO "  isn't matching. The buffer count above is the definitive proof."
    INFO "  Manual check: journalctl -u chillcheck-subscriber --since '$OUTAGE_SINCE' | grep buffering"
fi

SENSOR_FAILS=$(journalctl -u chillcheck-subscriber --since "$OUTAGE_SINCE" \
    --no-pager 2>/dev/null | grep -c "Failed to update sensor" || true)
[[ "$SENSOR_FAILS" -gt 0 ]] && \
    NOTE "Sensor last_seen updates also failed (${SENSOR_FAILS}) -- expected, recovers after reconnect"

MQTT_MSGS=$(journalctl -u chillcheck-subscriber --since "$OUTAGE_SINCE" \
    --no-pager 2>/dev/null | grep -c "Reading:" || true)
if [[ "$MQTT_MSGS" -gt 0 ]]; then
    PASS "MQTT readings still arriving (${MQTT_MSGS} log lines) -- internal traffic unaffected"
else
    NOTE "No 'Reading:' log lines found -- sensors fire ~1/min, may have been missed"
fi

UI_BOX "DURING OUTAGE -- check these now before restoring connectivity:
  app.chillcheck.online:
    - Cabinet last-updated timestamps should be STALE (frozen)
    - Hub may show 'Offline' in Settings > Devices (heartbeat also
      hits port 443 -- expected side-effect of our block)
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
_unblock_supabase
PASS "iptables rules removed"

if _can_connect "$SUPABASE_HOST"; then PASS "Supabase is reachable again"
else NOTE "Port 443 still timing out -- may take a moment for existing connections to clear"; fi

INFO "Drain job runs every 60s. Polling buffer for up to ${DRAIN_WAIT_SECS}s..."

DRAIN_ELAPSED=0; DRAINED=false
while [[ $DRAIN_ELAPSED -lt $DRAIN_WAIT_SECS ]]; do
    sleep 10; DRAIN_ELAPSED=$((DRAIN_ELAPSED + 10))
    CURRENT=$(_buffer_count 2>/dev/null || echo "$BUFFER_AFTER")
    printf "\r        %ds -- buffer: %s row(s)..." "$DRAIN_ELAPSED" "$CURRENT"
    if [[ "$CURRENT" != "ERR" && "$CURRENT" -le "$BASELINE" ]]; then
        echo; DRAINED=true; break
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
    INFO "  Manual check: python3 -c \"import sqlite3; c=sqlite3.connect('${BUFFER_DB}'); print(c.execute('SELECT COUNT(*) FROM pending_readings').fetchone()[0])\""
fi

DRAIN_SINCE=$(_ts_since "$RESTORE_TS")
DRAIN_LINES=$(journalctl -u chillcheck-subscriber --since "$DRAIN_SINCE" \
    --no-pager 2>/dev/null | grep "Drained .* buffered reading" || true)
if [[ -n "$DRAIN_LINES" ]]; then
    PASS "Drain log messages found:"
    echo "$DRAIN_LINES" | while IFS= read -r line; do INFO "    $line"; done
else
    NOTE "Log check inconclusive: 'Drained N buffered reading(s)' not found via journalctl"
    INFO "  The buffer count drop above is the definitive proof that drain worked."
    INFO "  Manual check: journalctl -u chillcheck-subscriber --since '$DRAIN_SINCE' | grep -i drained"
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
