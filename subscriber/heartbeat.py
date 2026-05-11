"""
ChillCheck — Heartbeat Service
================================
Pings Uptime Robot every 5 minutes to confirm the Pi is alive.
If the heartbeat stops, Uptime Robot emails Charlie directly
(independent of the Pi — works even if the Pi is fully offline).

Also updates the device's last_heartbeat in Supabase.
"""

import os
import logging
from datetime import datetime, timezone

import httpx

log = logging.getLogger("chillcheck.heartbeat")

HEARTBEAT_URL = os.getenv("HEARTBEAT_URL")


class HeartbeatService:
    def __init__(self, supabase, device_id: str):
        self.supabase  = supabase
        self.device_id = device_id

    def ping(self):
        """Send heartbeat ping and update Supabase device record."""
        now = datetime.now(timezone.utc).isoformat()

        # ── Update Supabase ───────────────────────────────────
        try:
            self.supabase.table("devices").update({
                "last_heartbeat": now,
                "status":         "online",
            }).eq("id", self.device_id).execute()
        except Exception as e:
            log.error(f"Failed to update heartbeat in Supabase: {e}")

        # ── Ping Uptime Robot ─────────────────────────────────
        if not HEARTBEAT_URL:
            log.debug("HEARTBEAT_URL not set — skipping external ping")
            return

        try:
            response = httpx.get(HEARTBEAT_URL, timeout=10)
            if response.status_code == 200:
                log.debug("Heartbeat ping OK")
            else:
                log.warning(f"Heartbeat ping returned {response.status_code}")
        except Exception as e:
            log.warning(f"Heartbeat ping failed: {e}")
