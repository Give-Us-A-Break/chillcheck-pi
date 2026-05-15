"""
ChillCheck — Notifications
===========================
All notification sending is proxied through the ChillCheck cloud API.
The Pi holds no Resend or Vonage credentials — those live in Vercel
environment variables only and are never shipped to customer hardware.

The Pi authenticates with NOTIFY_SECRET, a lightweight shared secret
issued at pairing time. It carries no database access.
"""

import os
import logging

import httpx

log = logging.getLogger("chillcheck.notifications")

VERCEL_URL    = os.getenv("VERCEL_URL", "https://app.chillcheck.online")
NOTIFY_SECRET = os.getenv("NOTIFY_SECRET", "")
DEVICE_ID     = os.getenv("DEVICE_ID", "")


def notify(alert_id: str, notification_type: str) -> bool:
    """
    Ask the ChillCheck cloud to send a notification for this alert.
    Returns True if the cloud accepted the request, False otherwise.
    Failures are logged but never raise — the escalation engine retries
    on the next scheduler tick.
    """
    if not NOTIFY_SECRET:
        log.warning("NOTIFY_SECRET not set — notification skipped")
        return False

    try:
        resp = httpx.post(
            f"{VERCEL_URL}/api/notify",
            headers={"Authorization": f"Bearer {NOTIFY_SECRET}"},
            json={"device_id": DEVICE_ID, "alert_id": alert_id, "type": notification_type},
            timeout=15,
        )
        if resp.status_code == 200:
            data = resp.json()
            log.info(f"Notification ({notification_type}) delivered: {data.get('sent', 0)} recipient(s)")
            return True
        log.error(f"Notify endpoint returned {resp.status_code}: {resp.text[:200]}")
        return False
    except Exception as e:
        log.error(f"Notify request failed: {e}")
        return False


def send_battery_digest() -> bool:
    """
    Ask the ChillCheck cloud to send the weekly battery digest for this site.
    The endpoint is a no-op if no sensors are below threshold, so this is safe
    to run on a fixed schedule without producing spam.
    """
    if not NOTIFY_SECRET:
        log.warning("NOTIFY_SECRET not set — battery digest skipped")
        return False

    try:
        resp = httpx.post(
            f"{VERCEL_URL}/api/digest/battery",
            headers={"Authorization": f"Bearer {NOTIFY_SECRET}"},
            json={"device_id": DEVICE_ID},
            timeout=20,
        )
        if resp.status_code == 200:
            data = resp.json()
            low = data.get("low_battery_count", 0)
            sent = data.get("sent", 0)
            log.info(f"Battery digest: {low} sensor(s) below threshold, {sent} email(s) sent")
            return True
        log.error(f"Battery digest endpoint returned {resp.status_code}: {resp.text[:200]}")
        return False
    except Exception as e:
        log.error(f"Battery digest request failed: {e}")
        return False
