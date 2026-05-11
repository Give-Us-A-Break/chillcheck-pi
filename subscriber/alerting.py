"""
ChillCheck — Alert Engine
=========================
Handles:
  - Temperature threshold checking (warning / critical)
  - Sensor offline alerts
  - Alert deduplication (don't re-raise active alerts)
  - Alert resolution (temp returns to normal)
  - Escalation: email → SMS → phone call
  - Out-of-hours escalation (skip straight to call)
"""

import os
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

from notifications import send_email, send_sms, make_call

log = logging.getLogger("chillcheck.alerts")


class AlertEngine:
    def __init__(self, supabase, organisation_id: str, site_id: str, device_id: str):
        self.supabase        = supabase
        self.organisation_id = organisation_id
        self.site_id         = site_id
        self.device_id       = device_id
        self._settings       = None
        self._settings_ts    = None

    # ── Settings ──────────────────────────────────────────────

    def _get_settings(self) -> dict:
        """Fetch alert settings, cached for 5 minutes."""
        now = datetime.now(timezone.utc)
        if self._settings and self._settings_ts:
            if (now - self._settings_ts).total_seconds() < 300:
                return self._settings
        try:
            res = (
                self.supabase.table("alert_settings")
                .select("*")
                .eq("site_id", self.site_id)
                .single()
                .execute()
            )
            self._settings    = res.data
            self._settings_ts = now
            return self._settings
        except Exception as e:
            log.error(f"Could not fetch alert settings: {e}")
            return {
                "email_delay_mins":      0,
                "sms_delay_mins":        10,
                "call_delay_mins":       15,
                "out_of_hours_enabled":  False,
                "out_of_hours_start":    "20:00",
                "out_of_hours_end":      "07:00",
                "out_of_hours_skip_sms": False,
            }

    def _is_out_of_hours(self) -> bool:
        """Check if current time is within out-of-hours window."""
        settings = self._get_settings()
        if not settings.get("out_of_hours_enabled"):
            return False
        try:
            now_time = datetime.now().time()
            start    = datetime.strptime(settings["out_of_hours_start"], "%H:%M").time()
            end      = datetime.strptime(settings["out_of_hours_end"],   "%H:%M").time()
            # Handle overnight window (e.g. 20:00 to 07:00)
            if start > end:
                return now_time >= start or now_time < end
            return start <= now_time < end
        except Exception:
            return False

    # ── Active alert lookup ───────────────────────────────────

    def _get_active_alert(self, cabinet_id: str, alert_type: str) -> Optional[dict]:
        """Return an unresolved alert of a given type for a cabinet."""
        try:
            res = (
                self.supabase.table("alerts")
                .select("*")
                .eq("cabinet_id", cabinet_id)
                .eq("type", alert_type)
                .is_("resolved_at", "null")
                .order("triggered_at", desc=True)
                .limit(1)
                .execute()
            )
            return res.data[0] if res.data else None
        except Exception as e:
            log.error(f"Active alert lookup failed: {e}")
            return None

    def _get_active_sensor_offline_alert(self, sensor_id: str) -> Optional[dict]:
        """Return an unresolved offline alert for a specific sensor."""
        try:
            res = (
                self.supabase.table("alerts")
                .select("*")
                .eq("sensor_id", sensor_id)
                .eq("type", "sensor_offline")
                .is_("resolved_at", "null")
                .order("triggered_at", desc=True)
                .limit(1)
                .execute()
            )
            return res.data[0] if res.data else None
        except Exception as e:
            log.error(f"Sensor offline alert lookup failed: {e}")
            return None

    # ── Raise alerts ──────────────────────────────────────────

    def _raise_alert(
        self,
        cabinet: dict,
        sensor: Optional[dict],
        alert_type: str,
        severity: str,
        temperature: Optional[float],
        message: str,
    ) -> Optional[dict]:
        """Insert a new alert row and trigger initial email notification."""
        now = datetime.now(timezone.utc).isoformat()
        try:
            res = self.supabase.table("alerts").insert({
                "organisation_id": self.organisation_id,
                "site_id":         self.site_id,
                "cabinet_id":      cabinet["id"],
                "sensor_id":       sensor["id"] if sensor else None,
                "device_id":       self.device_id,
                "type":            alert_type,
                "severity":        severity,
                "temperature":     temperature,
                "message":         message,
                "triggered_at":    now,
                "escalation_level": 0,
            }).execute()
            alert = res.data[0]
            log.warning(f"ALERT raised: [{severity.upper()}] {message}")

            # Send initial email immediately
            self._send_email_notification(alert, cabinet)
            return alert
        except Exception as e:
            log.error(f"Failed to raise alert: {e}")
            return None

    def _resolve_alert(self, alert_id: str):
        """Mark an alert as resolved."""
        try:
            self.supabase.table("alerts").update({
                "resolved_at": datetime.now(timezone.utc).isoformat(),
            }).eq("id", alert_id).execute()
            log.info(f"Alert {alert_id} resolved")
        except Exception as e:
            log.error(f"Failed to resolve alert {alert_id}: {e}")

    # ── Temperature threshold check ───────────────────────────

    def check_temperature(self, cabinet: dict, sensor: dict, temperature: float):
        """
        Check a reading against cabinet thresholds.
        Raises, upgrades, or resolves alerts as needed.
        """
        target   = float(cabinet["target_temp"])
        warn_off = float(cabinet["warning_offset"])
        crit_off = float(cabinet["critical_offset"])
        diff     = abs(temperature - target)

        cabinet_id   = cabinet["id"]
        cabinet_name = cabinet["name"]

        if diff >= crit_off:
            severity = "critical"
            direction = "above" if temperature > target else "below"
            message = (
                f"{cabinet_name}: temperature {temperature}°C is {direction} "
                f"critical threshold (target {target}°C ± {crit_off}°C)"
            )
            existing = self._get_active_alert(cabinet_id, "high_temp" if temperature > target else "low_temp")
            if not existing:
                alert_type = "high_temp" if temperature > target else "low_temp"
                self._raise_alert(cabinet, sensor, alert_type, severity, temperature, message)
            elif existing["severity"] == "warning":
                # Upgrade existing warning to critical
                self._upgrade_alert(existing["id"], "critical", message)

        elif diff >= warn_off:
            severity = "warning"
            direction = "above" if temperature > target else "below"
            message = (
                f"{cabinet_name}: temperature {temperature}°C is {direction} "
                f"warning threshold (target {target}°C ± {warn_off}°C)"
            )
            existing = self._get_active_alert(cabinet_id, "high_temp" if temperature > target else "low_temp")
            if not existing:
                alert_type = "high_temp" if temperature > target else "low_temp"
                self._raise_alert(cabinet, sensor, alert_type, severity, temperature, message)

        else:
            # Temperature is OK — resolve any active temp alerts
            for alert_type in ("high_temp", "low_temp"):
                existing = self._get_active_alert(cabinet_id, alert_type)
                if existing:
                    log.info(f"{cabinet_name}: temperature back to normal ({temperature}°C) — resolving alert")
                    self._resolve_alert(existing["id"])

    def _upgrade_alert(self, alert_id: str, new_severity: str, new_message: str):
        """Upgrade an existing alert from warning to critical."""
        try:
            self.supabase.table("alerts").update({
                "severity": new_severity,
                "message":  new_message,
            }).eq("id", alert_id).execute()
            log.warning(f"Alert {alert_id} upgraded to {new_severity}")
        except Exception as e:
            log.error(f"Failed to upgrade alert: {e}")

    # ── Offline alerts ────────────────────────────────────────

    def raise_offline_alert(self, cabinet: dict, sensor: dict, severity: str, minutes_silent: int):
        """Raise or upgrade a sensor offline alert."""
        existing = self._get_active_sensor_offline_alert(sensor["id"])
        message  = (
            f"{cabinet['name']}: no temperature readings for {minutes_silent} minutes "
            f"— sensor may be offline or out of range"
        )
        if not existing:
            self._raise_alert(cabinet, sensor, "sensor_offline", severity, None, message)
        elif existing["severity"] == "warning" and severity == "critical":
            self._upgrade_alert(existing["id"], "critical", message)

    def resolve_offline_alert(self, sensor_id: str):
        """Resolve offline alert when sensor comes back online."""
        existing = self._get_active_sensor_offline_alert(sensor_id)
        if existing:
            log.info(f"Sensor {sensor_id} back online — resolving offline alert")
            self._resolve_alert(existing["id"])

    # ── Escalation engine ─────────────────────────────────────

    def process_escalations(self):
        """
        Called every minute by the scheduler.
        For each active unacknowledged alert, check if it's
        time to escalate to the next notification level.
        """
        try:
            res = (
                self.supabase.table("alerts")
                .select("*")
                .eq("organisation_id", self.organisation_id)
                .is_("resolved_at", "null")
                .is_("acknowledged_at", "null")
                .execute()
            )
            active_alerts = res.data
        except Exception as e:
            log.error(f"Escalation fetch failed: {e}")
            return

        if not active_alerts:
            return

        settings    = self._get_settings()
        sms_delay   = settings.get("sms_delay_mins", 10)
        call_delay  = settings.get("call_delay_mins", 15)
        ooh         = self._is_out_of_hours()
        ooh_skip_sms = settings.get("out_of_hours_skip_sms", False)

        now = datetime.now(timezone.utc)

        for alert in active_alerts:
            level        = alert.get("escalation_level", 0)
            triggered_at = datetime.fromisoformat(alert["triggered_at"].replace("Z", "+00:00"))
            minutes_old  = (now - triggered_at).total_seconds() / 60

            # Get cabinet for context
            cabinet = None
            try:
                cab_res = self.supabase.table("cabinets").select("*").eq("id", alert["cabinet_id"]).single().execute()
                cabinet = cab_res.data
            except Exception:
                pass

            # ── Level 1: Email already sent at alert creation ──
            # Level 0 = email sent, waiting for SMS threshold
            # Level 1 = SMS sent, waiting for call threshold
            # Level 2 = call made
            # Level 3 = all escalations exhausted

            if level == 0:
                # Email was sent — check if SMS is due
                if ooh and ooh_skip_sms:
                    # Out of hours — skip SMS, go straight to call
                    if minutes_old >= call_delay:
                        self._escalate_to_call(alert, cabinet)
                elif minutes_old >= sms_delay:
                    self._escalate_to_sms(alert, cabinet)

            elif level == 1:
                # SMS sent — check if call is due
                if minutes_old >= call_delay:
                    self._escalate_to_call(alert, cabinet)

    def _escalate_to_sms(self, alert: dict, cabinet: Optional[dict]):
        """Send SMS to all contacts and update escalation level."""
        log.warning(f"Escalating alert {alert['id']} to SMS")
        try:
            contacts = self._get_contacts(notify_sms=True)
            for contact in contacts:
                if contact.get("phone"):
                    send_sms(
                        to=contact["phone"],
                        message=self._format_sms(alert, cabinet),
                    )
            self.supabase.table("alerts").update({
                "escalation_level": 1,
                "sms_sent_at": datetime.now(timezone.utc).isoformat(),
            }).eq("id", alert["id"]).execute()
        except Exception as e:
            log.error(f"SMS escalation failed: {e}")

    def _escalate_to_call(self, alert: dict, cabinet: Optional[dict]):
        """Make phone calls to priority contacts and update escalation level."""
        log.warning(f"Escalating alert {alert['id']} to phone call")
        try:
            contacts = self._get_contacts(notify_call=True)
            # Call in priority order
            contacts_sorted = sorted(contacts, key=lambda c: c.get("priority", 99))
            for contact in contacts_sorted:
                if contact.get("phone"):
                    make_call(
                        to=contact["phone"],
                        message=self._format_call_message(alert, cabinet),
                        alert_id=alert["id"],
                    )
            self.supabase.table("alerts").update({
                "escalation_level": 2,
                "call_made_at": datetime.now(timezone.utc).isoformat(),
            }).eq("id", alert["id"]).execute()
        except Exception as e:
            log.error(f"Call escalation failed: {e}")

    # ── Notification helpers ──────────────────────────────────

    def _get_contacts(self, notify_email=False, notify_sms=False, notify_call=False) -> list:
        """Fetch active contacts filtered by notification preference."""
        try:
            query = (
                self.supabase.table("contacts")
                .select("*")
                .eq("organisation_id", self.organisation_id)
                .eq("active", True)
            )
            if notify_email:
                query = query.eq("notify_email", True)
            if notify_sms:
                query = query.eq("notify_sms", True)
            if notify_call:
                query = query.eq("notify_call", True)

            res = query.order("priority").execute()
            return res.data
        except Exception as e:
            log.error(f"Failed to fetch contacts: {e}")
            return []

    def _send_email_notification(self, alert: dict, cabinet: Optional[dict]):
        """Send initial email alert to all email contacts."""
        try:
            contacts = self._get_contacts(notify_email=True)
            if not contacts:
                log.warning("No email contacts configured")
                return

            subject = self._format_email_subject(alert, cabinet)
            body    = self._format_email_body(alert, cabinet)

            for contact in contacts:
                if contact.get("email"):
                    send_email(
                        to=contact["email"],
                        to_name=contact["name"],
                        subject=subject,
                        body=body,
                    )

            # Update alert to mark email sent
            self.supabase.table("alerts").update({
                "email_sent_at":    datetime.now(timezone.utc).isoformat(),
                "escalation_level": 0,
            }).eq("id", alert["id"]).execute()

        except Exception as e:
            log.error(f"Email notification failed: {e}")

    def _format_email_subject(self, alert: dict, cabinet: Optional[dict]) -> str:
        severity = alert["severity"].upper()
        cab_name = cabinet["name"] if cabinet else "Unknown Cabinet"
        type_map = {
            "high_temp":      "High Temperature",
            "low_temp":       "Low Temperature",
            "sensor_offline": "Sensor Offline",
            "device_offline": "Hub Offline",
        }
        alert_type = type_map.get(alert["type"], alert["type"])
        return f"[ChillCheck {severity}] {alert_type} — {cab_name}"

    def _format_email_body(self, alert: dict, cabinet: Optional[dict]) -> str:
        cab_name = cabinet["name"] if cabinet else "Unknown"
        location = cabinet.get("location", "") if cabinet else ""
        temp     = f"{alert['temperature']}°C" if alert.get("temperature") is not None else "N/A"
        target   = f"{cabinet['target_temp']}°C" if cabinet else "N/A"
        time_str = datetime.fromisoformat(
            alert["triggered_at"].replace("Z", "+00:00")
        ).strftime("%d %b %Y at %H:%M")

        return f"""
ChillCheck Temperature Alert
{'=' * 40}

{alert['message']}

Cabinet:     {cab_name} ({location})
Temperature: {temp}
Target:      {target}
Time:        {time_str}
Severity:    {alert['severity'].upper()}

This alert will escalate to SMS if not acknowledged within the configured time.

Acknowledge this alert at your ChillCheck dashboard.

--
ChillCheck Temperature Monitoring
This is an automated alert. Do not reply to this email.
        """.strip()

    def _format_sms(self, alert: dict, cabinet: Optional[dict]) -> str:
        cab_name = cabinet["name"] if cabinet else "Unknown"
        temp     = f"{alert['temperature']}°C" if alert.get("temperature") is not None else ""
        severity = alert["severity"].upper()
        return (
            f"ChillCheck {severity}: {cab_name}"
            + (f" {temp}" if temp else "")
            + f". {alert['message'][:100]}. "
            f"Acknowledge at app.chillcheck.online"
        )

    def _format_call_message(self, alert: dict, cabinet: Optional[dict]) -> str:
        """TwiML-friendly message for text-to-speech phone call."""
        cab_name = cabinet["name"] if cabinet else "a cabinet"
        temp_str = ""
        if alert.get("temperature") is not None:
            temp = float(alert["temperature"])
            temp_str = f"Current temperature is {abs(temp)} degrees {'below' if temp < 0 else 'above'} zero. "

        type_map = {
            "high_temp":      "exceeded its high temperature threshold",
            "low_temp":       "dropped below its low temperature threshold",
            "sensor_offline": "gone offline — no readings received",
            "device_offline": "lost connection to the monitoring hub",
        }
        what_happened = type_map.get(alert["type"], "triggered an alert")

        return (
            f"This is an automated alert from Chill Check temperature monitoring. "
            f"{cab_name} has {what_happened}. "
            f"{temp_str}"
            f"Please check your dashboard immediately and acknowledge this alert. "
            f"This message will repeat. "
            f"Press any key to stop."
        )
