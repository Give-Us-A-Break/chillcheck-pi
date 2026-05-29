"""
ChillCheck — Drift Detector
===========================
Watches recent temperature readings per cabinet, fits a linear trend over
a rolling window, and fires a `predictive_drift` alert when the projected
temperature will breach the critical threshold within DRIFT_PROJECT_MINUTES.

Email-only (not in the escalatable set). Auto-resolves when the trend
reverses or a real threshold alert fires.
"""

import logging
from collections import defaultdict, deque
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger("chillcheck.drift")

DRIFT_PROJECT_MINUTES = 20   # how far ahead to project
DRIFT_MIN_READINGS    = 6    # minimum readings before attempting regression
DRIFT_MIN_SPREAD_SECS = 480  # 8 minutes — window must span this to be meaningful
DRIFT_MIN_SLOPE       = 0.05 # °C/min — ignore shallower trends as sensor noise
DRIFT_WINDOW_SIZE     = 25   # rolling window depth per cabinet


def _linear_regression(xs: list, ys: list):
    """OLS regression returning (slope_per_sec, intercept_at_xs0, xs[0]).

    xs values are normalised to xs[0] internally to avoid float precision
    issues with large epoch timestamps. The returned intercept is the
    y-value at xs[0] so callers can project as:

        y_proj = slope * (target_epoch - xs[0]) + intercept
    """
    n = len(xs)
    if n < 2:
        return 0.0, ys[0] if ys else 0.0, xs[0] if xs else 0.0
    x0 = xs[0]
    xn = [x - x0 for x in xs]
    sx  = sum(xn)
    sy  = sum(ys)
    sxy = sum(x * y for x, y in zip(xn, ys))
    sxx = sum(x * x for x in xn)
    denom = n * sxx - sx * sx
    if abs(denom) < 1e-9:
        return 0.0, sy / n, x0
    slope     = (n * sxy - sx * sy) / denom
    intercept = (sy - slope * sx) / n
    return slope, intercept, x0


class DriftDetector:
    def __init__(self, supabase, organisation_id: str, site_id: str, device_id: str):
        self.supabase        = supabase
        self.organisation_id = organisation_id
        self.site_id         = site_id
        self.device_id       = device_id
        # Per-cabinet rolling window of (epoch_seconds, temperature) tuples
        self._windows: dict = defaultdict(lambda: deque(maxlen=DRIFT_WINDOW_SIZE))

    # ── Public API ─────────────────────────────────────────────

    def add_reading(self, cabinet_id: str, temperature: float, recorded_at: Optional[str] = None):
        """Append a reading to the per-cabinet window. Call before check_drift."""
        if recorded_at:
            try:
                ts = datetime.fromisoformat(recorded_at.replace("Z", "+00:00")).timestamp()
            except Exception:
                ts = datetime.now(timezone.utc).timestamp()
        else:
            ts = datetime.now(timezone.utc).timestamp()
        self._windows[cabinet_id].append((ts, temperature))

    def check_drift(self, cabinet: dict, sensor: dict) -> bool:
        """
        Run drift detection for a cabinet.

        Returns True if a predictive_drift alert is active (raised or
        already open). Side effects: raises or resolves a predictive_drift
        alert in Supabase when the trend warrants it.

        Skipped entirely when the cabinet's temperature alerts are muted —
        consistent with check_temperature behaviour in alerting.py.
        """
        if _is_muted(cabinet):
            return False

        cabinet_id = cabinet["id"]
        window     = list(self._windows[cabinet_id])

        if len(window) < DRIFT_MIN_READINGS:
            return False

        xs = [p[0] for p in window]
        ys = [p[1] for p in window]

        spread = xs[-1] - xs[0]
        if spread < DRIFT_MIN_SPREAD_SECS:
            return False

        slope_per_sec, intercept, x0 = _linear_regression(xs, ys)
        slope_per_min = slope_per_sec * 60

        if abs(slope_per_min) < DRIFT_MIN_SLOPE:
            self._resolve_if_active(cabinet_id)
            return False

        # Project DRIFT_PROJECT_MINUTES from the latest reading
        proj_epoch    = xs[-1] + DRIFT_PROJECT_MINUTES * 60
        projected_temp = slope_per_sec * (proj_epoch - x0) + intercept
        current_temp  = ys[-1]

        crit_high = float(cabinet["crit_high"])
        crit_low  = float(cabinet["crit_low"])

        # If a real threshold alert is already active, the prediction came
        # true — resolve the drift alert rather than leaving both open.
        if self._get_active_temp_alert(cabinet_id):
            self._resolve_if_active(cabinet_id)
            return False

        will_breach_high = slope_per_min >  DRIFT_MIN_SLOPE and projected_temp >= crit_high and current_temp < crit_high
        will_breach_low  = slope_per_min < -DRIFT_MIN_SLOPE and projected_temp <= crit_low  and current_temp > crit_low

        if will_breach_high or will_breach_low:
            direction = "high" if will_breach_high else "low"
            threshold = crit_high if will_breach_high else crit_low
            rate_str  = f"+{slope_per_min:.2f}" if slope_per_min > 0 else f"{slope_per_min:.2f}"
            message   = (
                f"{cabinet['name']}: temperature trending {direction} at {rate_str}°C/min "
                f"— projected to reach {projected_temp:.1f}°C "
                f"(critical threshold {threshold}°C) within {DRIFT_PROJECT_MINUTES} min"
            )
            if not self._get_active_drift_alert(cabinet_id):
                self._raise_drift_alert(cabinet, sensor, message, round(projected_temp, 2))
            return True

        self._resolve_if_active(cabinet_id)
        return False

    # ── Supabase helpers ───────────────────────────────────────

    def _get_active_temp_alert(self, cabinet_id: str) -> Optional[dict]:
        try:
            for atype in ("high_temp", "low_temp"):
                res = (
                    self.supabase.table("alerts")
                    .select("id")
                    .eq("cabinet_id", cabinet_id)
                    .eq("type", atype)
                    .is_("resolved_at", "null")
                    .limit(1)
                    .execute()
                )
                if res.data:
                    return res.data[0]
        except Exception as e:
            log.error(f"Active temp alert check failed: {e}")
        return None

    def _get_active_drift_alert(self, cabinet_id: str) -> Optional[dict]:
        try:
            res = (
                self.supabase.table("alerts")
                .select("id")
                .eq("cabinet_id", cabinet_id)
                .eq("type", "predictive_drift")
                .is_("resolved_at", "null")
                .limit(1)
                .execute()
            )
            return res.data[0] if res.data else None
        except Exception as e:
            log.error(f"Drift alert lookup failed: {e}")
            return None

    def _raise_drift_alert(
        self,
        cabinet: dict,
        sensor: dict,
        message: str,
        projected_temp: float,
    ):
        from notifications import notify
        now = datetime.now(timezone.utc).isoformat()
        try:
            res = self.supabase.table("alerts").insert({
                "organisation_id":  self.organisation_id,
                "site_id":          self.site_id,
                "cabinet_id":       cabinet["id"],
                "sensor_id":        sensor.get("id"),
                "device_id":        self.device_id,
                "type":             "predictive_drift",
                "severity":         "warning",
                "temperature":      projected_temp,
                "message":          message,
                "triggered_at":     now,
                "escalation_level": 0,
            }).execute()
            alert = res.data[0]
            log.warning(f"DRIFT ALERT raised: {message}")
            self._audit("alert.raised", alert["id"], cabinet, sensor, projected_temp, message)
            notify(alert["id"], "email")
        except Exception as e:
            log.error(f"Failed to raise drift alert: {e}")

    def _resolve_if_active(self, cabinet_id: str):
        existing = self._get_active_drift_alert(cabinet_id)
        if not existing:
            return
        try:
            self.supabase.table("alerts").update({
                "resolved_at": datetime.now(timezone.utc).isoformat(),
            }).eq("id", existing["id"]).execute()
            log.info(f"Drift alert {existing['id']} auto-resolved (trend reversed)")
            self._audit("alert.resolved", existing["id"], {"id": cabinet_id}, None)
        except Exception as e:
            log.error(f"Failed to resolve drift alert: {e}")

    def _audit(
        self,
        action: str,
        alert_id: str,
        cabinet: Optional[dict],
        sensor: Optional[dict],
        temperature: Optional[float] = None,
        message: Optional[str] = None,
    ):
        try:
            metadata: dict = {
                "alert_id":   alert_id,
                "alert_type": "predictive_drift",
                "severity":   "warning",
                "cabinet_id": (cabinet or {}).get("id"),
                "cabinet_name": (cabinet or {}).get("name"),
                "sensor_id":  (sensor or {}).get("id") if sensor else None,
            }
            if temperature is not None:
                metadata["temperature"] = float(temperature)
            if message:
                metadata["message_preview"] = message[:140]
            self.supabase.table("audit_log").insert({
                "organisation_id": self.organisation_id,
                "profile_id":      None,
                "action":          action,
                "metadata":        metadata,
            }).execute()
        except Exception as e:
            log.error(f"Failed to write drift audit row: {e}")


# ── Shared helper ──────────────────────────────────────────────

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
