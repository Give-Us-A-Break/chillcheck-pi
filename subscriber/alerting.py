"""
ChillCheck — Alert Engine
=========================
Handles:
  - Temperature threshold checking (warning / critical)
  - Sensor offline alerts
  - Battery / signal-quality alerts
  - Alert deduplication (don't re-raise active alerts)
  - Alert resolution (temp returns to normal)
  - Escalation: email → SMS → phone call
  - Out-of-hours escalation (skip straight to call)
"""

import logging
from datetime import datetime, timezone
from typing import Optional

from notifications import notify

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
                "battery_warn_pct":      20,
                "battery_critical_pct":  10,
                "signal_warn_dbm":       -85,
                "signal_warn_mins":      30,
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

    def _get_active_sensor_alert(self, sensor_id: str, alert_type: str) -> Optional[dict]:
        """Return an unresolved alert of a given type for a specific sensor."""
        try:
            res = (
                self.supabase.table("alerts")
                .select("*")
                .eq("sensor_id", sensor_id)
                .eq("type", alert_type)
                .is_("resolved_at", "null")
                .order("triggered_at", desc=True)
                .limit(1)
                .execute()
            )
            return res.data[0] if res.data else None
        except Exception as e:
            log.error(f"Sensor {alert_type} alert lookup failed: {e}")
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
        triggered_at: Optional[str] = None,
    ) -> Optional[dict]:
        """Insert a new alert row and trigger initial email notification.

        ``triggered_at`` lets retrospective replays stamp the alert at the
        original Pi-side ``recorded_at`` so the audit log reflects when
        the breach actually occurred, not when we noticed it.
        """
        now = triggered_at or datetime.now(timezone.utc).isoformat()
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
            self._audit_alert_event(
                "alert.raised",
                alert_id=alert["id"],
                alert_type=alert_type,
                severity=severity,
                cabinet=cabinet,
                sensor=sensor,
                temperature=temperature,
                message=message,
            )
            notify(alert["id"], "email")
            return alert
        except Exception as e:
            log.error(f"Failed to raise alert: {e}")
            return None

    def _resolve_alert(self, alert_id: str):
        """Mark an alert as resolved."""
        try:
            res = (
                self.supabase.table("alerts")
                .update({"resolved_at": datetime.now(timezone.utc).isoformat()})
                .eq("id", alert_id)
                .execute()
            )
            alert = res.data[0] if res.data else None
            log.info(f"Alert {alert_id} resolved")
            if alert:
                # Best-effort cabinet name lookup so the compliance audit row
                # reads cleanly without a join. Skipped silently on failure
                # because resolution must succeed even if the audit lookup doesn't.
                cabinet_name = None
                cab_id = alert.get("cabinet_id")
                if cab_id:
                    try:
                        cab_res = (
                            self.supabase.table("cabinets")
                            .select("name")
                            .eq("id", cab_id)
                            .single()
                            .execute()
                        )
                        cabinet_name = (cab_res.data or {}).get("name")
                    except Exception:
                        pass
                self._audit_alert_event(
                    "alert.resolved",
                    alert_id=alert_id,
                    alert_type=alert.get("type"),
                    severity=alert.get("severity"),
                    cabinet={"id": cab_id, "name": cabinet_name} if cab_id else None,
                    sensor={"id": alert.get("sensor_id")} if alert.get("sensor_id") else None,
                )
        except Exception as e:
            log.error(f"Failed to resolve alert {alert_id}: {e}")

    def _audit_alert_event(
        self,
        action: str,
        alert_id: str,
        alert_type: Optional[str],
        severity: Optional[str],
        cabinet: Optional[dict] = None,
        sensor: Optional[dict] = None,
        temperature: Optional[float] = None,
        message: Optional[str] = None,
    ):
        """Write an alert lifecycle event (raised / escalated / resolved) to audit_log.

        System events have profile_id = null. Best-effort — never blocks the
        primary action if the audit write fails.
        """
        try:
            metadata = {
                "alert_id":     alert_id,
                "alert_type":   alert_type,
                "severity":     severity,
                "cabinet_id":   (cabinet or {}).get("id"),
                "cabinet_name": (cabinet or {}).get("name"),
                "sensor_id":    (sensor or {}).get("id"),
            }
            if temperature is not None:
                try:
                    metadata["temperature"] = float(temperature)
                except (TypeError, ValueError):
                    pass
            if message:
                metadata["message_preview"] = message[:140]
            self.supabase.table("audit_log").insert({
                "organisation_id": self.organisation_id,
                "profile_id":      None,
                "action":          action,
                "metadata":        metadata,
            }).execute()
        except Exception as e:
            log.error(f"Failed to write {action} audit row: {e}")

    @staticmethod
    def _is_muted(cabinet: dict) -> bool:
        """True if temperature alerts are currently muted for this cabinet."""
        muted_until = cabinet.get("alerts_muted_until")
        if not muted_until:
            return False
        try:
            if isinstance(muted_until, str):
                until_dt = datetime.fromisoformat(muted_until.replace("Z", "+00:00"))
            else:
                until_dt = muted_until
            return until_dt > datetime.now(timezone.utc)
        except Exception:
            return False

    # ── Temperature threshold check ───────────────────────────

    def check_temperature(
        self,
        cabinet: dict,
        sensor: dict,
        temperature: float,
        recorded_at: Optional[str] = None,
        outage_duration_seconds: Optional[int] = None,
    ):
        """
        Check a reading against cabinet thresholds.
        Raises, upgrades, or resolves alerts as needed.
        Skipped entirely while the cabinet's temperature alerts are muted —
        readings are still recorded by the caller, but no alert state
        transitions happen until the mute expires.

        ``recorded_at`` and ``outage_duration_seconds`` are used by the
        retrospective replay path (Epic 10 slice 2). When ``recorded_at``
        is supplied, any new alert is stamped at that time rather than
        ``now()``. When ``outage_duration_seconds`` exceeds an hour, a
        warning-band reading is escalated straight to critical — sustained
        breaches through long outages indicate real food-safety risk, not
        sensor noise. The duration is appended to the alert message so
        recipients understand the alert was retrospective.
        """
        if self._is_muted(cabinet):
            log.debug(f"{cabinet['name']}: temperature alerts muted, skipping check")
            return

        warn_low  = float(cabinet["warn_low"])
        warn_high = float(cabinet["warn_high"])
        crit_low  = float(cabinet["crit_low"])
        crit_high = float(cabinet["crit_high"])

        cabinet_id   = cabinet["id"]
        cabinet_name = cabinet["name"]

        long_outage = (
            outage_duration_seconds is not None and outage_duration_seconds >= 3600
        )

        def _with_outage_suffix(msg: str) -> str:
            if outage_duration_seconds is None:
                return msg
            minutes = max(1, int(outage_duration_seconds // 60))
            return f"{msg} (detected after {minutes}m offline period)"

        if temperature < crit_low or temperature > crit_high:
            severity = "critical"
            direction = "above" if temperature > crit_high else "below"
            bound = crit_high if temperature > crit_high else crit_low
            message = _with_outage_suffix(
                f"{cabinet_name}: temperature {temperature}°C is {direction} "
                f"critical threshold ({bound}°C)"
            )
            existing = self._get_active_alert(cabinet_id, "high_temp" if temperature > crit_high else "low_temp")
            if not existing:
                alert_type = "high_temp" if temperature > crit_high else "low_temp"
                self._raise_alert(
                    cabinet, sensor, alert_type, severity, temperature, message,
                    triggered_at=recorded_at,
                )
            elif existing["severity"] == "warning":
                # Upgrade existing warning to critical
                self._upgrade_alert(existing["id"], "critical", message)

        elif temperature < warn_low or temperature > warn_high:
            # >60min outages bump warning-band breaches to critical — a
            # sustained out-of-band reading through an hour-plus offline
            # window is food-safety serious, not noise.
            severity = "critical" if long_outage else "warning"
            direction = "above" if temperature > warn_high else "below"
            bound = warn_high if temperature > warn_high else warn_low
            message = _with_outage_suffix(
                f"{cabinet_name}: temperature {temperature}°C is {direction} "
                f"warning threshold ({bound}°C)"
            )
            existing = self._get_active_alert(cabinet_id, "high_temp" if temperature > warn_high else "low_temp")
            if not existing:
                alert_type = "high_temp" if temperature > warn_high else "low_temp"
                self._raise_alert(
                    cabinet, sensor, alert_type, severity, temperature, message,
                    triggered_at=recorded_at,
                )
            elif severity == "critical" and existing["severity"] == "warning":
                self._upgrade_alert(existing["id"], "critical", message)

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

        # Battery and signal alerts are informational — email only, no SMS/call escalation
        escalatable = {"high_temp", "low_temp", "sensor_offline", "device_offline"}

        for alert in active_alerts:
            if alert.get("type") not in escalatable:
                continue
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

            # If the cabinet's temperature alerts are muted, halt further
            # escalation for high/low_temp alerts that were already raised.
            # Sensor/device offline escalations are unaffected — mute is
            # temperature-only by design.
            if alert.get("type") in ("high_temp", "low_temp") and cabinet and self._is_muted(cabinet):
                continue

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
        log.warning(f"Escalating alert {alert['id']} to SMS")
        notify(alert["id"], "sms")
        # DB update (sms_sent_at, escalation_level) is handled by /api/notify

    def _escalate_to_call(self, alert: dict, cabinet: Optional[dict]):
        log.warning(f"Escalating alert {alert['id']} to phone call")
        notify(alert["id"], "call")
        # DB update (call_made_at, escalation_level) is handled by /api/notify

    # ── Battery alerts ────────────────────────────────────────

    def check_battery(self, cabinet: dict, sensor: dict, battery_pct: int):
        """Raise/upgrade/resolve a low_battery alert based on battery level."""
        settings = self._get_settings()
        warn_pct = settings.get("battery_warn_pct", 20)
        crit_pct = settings.get("battery_critical_pct", 10)

        existing = self._get_active_sensor_alert(sensor["id"], "low_battery")
        sensor_label = sensor.get("name") or sensor.get("zigbee_id", "sensor")
        cabinet_name = cabinet["name"]

        if battery_pct < crit_pct:
            message = (
                f"{cabinet_name}: sensor '{sensor_label}' battery at {battery_pct}% "
                f"— replace immediately"
            )
            if not existing:
                self._raise_alert(cabinet, sensor, "low_battery", "critical", None, message)
            elif existing["severity"] == "warning":
                self._upgrade_alert(existing["id"], "critical", message)
        elif battery_pct < warn_pct:
            if not existing:
                message = (
                    f"{cabinet_name}: sensor '{sensor_label}' battery at {battery_pct}% "
                    f"— replace soon"
                )
                self._raise_alert(cabinet, sensor, "low_battery", "warning", None, message)
        else:
            if existing:
                log.info(f"Sensor {sensor['id']} battery back above threshold — resolving")
                self._resolve_alert(existing["id"])

    # ── Signal alerts ─────────────────────────────────────────

    def check_signal(self, cabinet: dict, sensor: dict, rssi: int):
        """Raise/resolve a low_signal alert after sustained poor RSSI."""
        settings    = self._get_settings()
        threshold   = settings.get("signal_warn_dbm", -85)
        sustain_min = settings.get("signal_warn_mins", 30)

        sensor_id    = sensor["id"]
        sensor_label = sensor.get("name") or sensor.get("zigbee_id", "sensor")
        cabinet_name = cabinet["name"]
        existing     = self._get_active_sensor_alert(sensor_id, "low_signal")
        now          = datetime.now(timezone.utc)

        if rssi < threshold:
            # Signal is poor — start (or continue) the timer
            since_str = sensor.get("low_signal_since")
            since     = None
            if since_str:
                try:
                    since = datetime.fromisoformat(since_str.replace("Z", "+00:00"))
                except Exception:
                    since = None

            if since is None:
                # First time below threshold — record and skip
                now_iso = now.isoformat()
                try:
                    self.supabase.table("sensors").update({
                        "low_signal_since": now_iso,
                    }).eq("id", sensor_id).execute()
                    sensor["low_signal_since"] = now_iso
                except Exception as e:
                    log.error(f"Failed to set low_signal_since: {e}")
                return

            poor_minutes = (now - since).total_seconds() / 60
            if poor_minutes >= sustain_min and not existing:
                message = (
                    f"{cabinet_name}: sensor '{sensor_label}' signal poor "
                    f"({rssi} dBm) for {int(poor_minutes)} mins — try moving the hub closer"
                )
                self._raise_alert(cabinet, sensor, "low_signal", "warning", None, message)
        else:
            # Signal recovered — clear timer and resolve any active alert
            if sensor.get("low_signal_since") is not None:
                try:
                    self.supabase.table("sensors").update({
                        "low_signal_since": None,
                    }).eq("id", sensor_id).execute()
                    sensor["low_signal_since"] = None
                except Exception as e:
                    log.error(f"Failed to clear low_signal_since: {e}")
            if existing:
                log.info(f"Sensor {sensor_id} signal recovered — resolving")
                self._resolve_alert(existing["id"])

