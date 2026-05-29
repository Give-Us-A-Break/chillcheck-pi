#!/usr/bin/env python3
"""
ChillCheck — MQTT Subscriber
============================
Runs on the Pi 4 as a systemd service.

Responsibilities:
  1. Subscribe to Zigbee2MQTT topics for all paired sensors
  2. Parse temperature readings and push to Supabase
  3. Update sensor last_seen and battery/RSSI
  4. Check readings against cabinet thresholds
  5. Detect offline sensors (no reading for X minutes)
  6. Detect Pi/device offline (heartbeat)
  7. Fire and escalate alerts (email → SMS → phone call)
  8. Ping Uptime Robot heartbeat every 5 minutes
  9. Acknowledge alert resolution when temp returns to normal
"""

import os
import sys
import json
import time
import uuid
import logging
import threading
import schedule
from datetime import datetime, timezone
from typing import Optional

import paho.mqtt.client as mqtt
from supabase import create_client, Client
from dotenv import load_dotenv

from alerting import AlertEngine
from drift import DriftDetector
from heartbeat import HeartbeatService
from notifications import send_battery_digest, send_connectivity_restored
from buffer import ReadingBuffer
from outage import OutageTracker

# ── Load environment ──────────────────────────────────────────
load_dotenv("/etc/chillcheck/.env")

SUPABASE_URL        = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_KEY= os.environ["SUPABASE_SERVICE_KEY"]
ORGANISATION_ID     = os.environ["ORGANISATION_ID"]
SITE_ID             = os.environ["SITE_ID"]
DEVICE_ID           = os.environ["DEVICE_ID"]
MQTT_HOST           = os.getenv("MQTT_HOST", "127.0.0.1")
MQTT_PORT           = int(os.getenv("MQTT_PORT", 1883))
MQTT_TOPIC_PREFIX   = os.getenv("MQTT_TOPIC_PREFIX", "zigbee2mqtt")
LOG_LEVEL           = os.getenv("LOG_LEVEL", "INFO")
BUFFER_DB_PATH      = os.getenv("BUFFER_DB_PATH", "/var/lib/chillcheck/buffer.db")
OUTAGE_STATE_PATH   = os.getenv("OUTAGE_STATE_PATH", "/var/lib/chillcheck/outage.json")

# ── Sentry ────────────────────────────────────────────────────
# DSN defaults to the chillcheck-pi project so every Pi running this code
# reports to Sentry automatically — customers don't configure observability.
# Override via SENTRY_DSN in /etc/chillcheck/.env (set to "" to disable).
# DSNs are not secrets: they're routing identifiers designed to be embedded
# in client code (see Sentry docs / NEXT_PUBLIC_SENTRY_DSN). httpx auto-
# integration propagates sentry-trace + baggage headers on the /api/notify
# call, so one trace spans Pi → Vercel → Vonage.
SENTRY_DSN_DEFAULT = "https://ec01713f389bc17f70360cab07883197@o4511468442091520.ingest.de.sentry.io/4511468471910480"
SENTRY_DSN = os.getenv("SENTRY_DSN", SENTRY_DSN_DEFAULT)
if SENTRY_DSN:
    # ImportError is expected on the first update from a pre-sentry release:
    # the old chillcheck-update.sh copies this new main.py but doesn't run
    # `pip install -r requirements.txt`. The new updater (now in place) will
    # install sentry-sdk on the next run. Until then, run without it.
    try:
        import sentry_sdk
        sentry_sdk.init(
            dsn=SENTRY_DSN,
            environment=os.getenv("SENTRY_ENV", "production"),
            traces_sample_rate=1.0,
            # Sensor payloads include zigbee IDs; keep locals out of error reports.
            include_local_variables=False,
        )
        sentry_sdk.set_tag("device_id", DEVICE_ID)
        sentry_sdk.set_tag("organisation_id", ORGANISATION_ID)
        sentry_sdk.set_tag("site_id", SITE_ID)
    except ImportError:
        print("sentry-sdk not installed; observability disabled. Re-run chillcheck-update.sh.", file=sys.stderr)

# Retrospective alert tiering thresholds (Epic 10 slice 2)
RETRO_ALERT_MIN_SECONDS = 5 * 60       # < 5min outage → noise, skip replay
RETRO_ALERT_LONG_SECONDS = 60 * 60     # ≥ 60min → bump warning band to critical

# ── Logging ───────────────────────────────────────────────────
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
    ]
)
log = logging.getLogger("chillcheck.subscriber")


# ════════════════════════════════════════════════════════════
# SUPABASE CLIENT
# ════════════════════════════════════════════════════════════

def get_supabase() -> Client:
    """Returns a Supabase client using the service role key.
    Service role bypasses RLS — safe because this only runs
    on the local Pi, never exposed to the internet.
    """
    return create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)


# ════════════════════════════════════════════════════════════
# SENSOR CACHE
# In-memory cache of sensor and cabinet config.
# Refreshed from Supabase every 5 minutes.
# Avoids a DB round-trip on every MQTT message.
# ════════════════════════════════════════════════════════════

class SensorCache:
    def __init__(self, supabase: Client):
        self.supabase  = supabase
        self.sensors   = {}   # zigbee_id → sensor row
        self.cabinets  = {}   # cabinet_id → cabinet row
        self.lock      = threading.Lock()
        self.refresh()

    def refresh(self):
        """Pull latest sensor + cabinet config from Supabase."""
        try:
            sensors_res = (
                self.supabase.table("sensors")
                .select("*, cabinets(*)")
                .eq("organisation_id", ORGANISATION_ID)
                .eq("site_id", SITE_ID)
                .eq("active", True)
                .execute()
            )
            with self.lock:
                self.sensors = {}
                self.cabinets = {}
                for row in sensors_res.data:
                    self.sensors[row["zigbee_id"]] = row
                    if row.get("cabinets"):
                        cab = row["cabinets"]
                        self.cabinets[cab["id"]] = cab

            log.info(f"Cache refreshed — {len(self.sensors)} sensors, {len(self.cabinets)} cabinets")
        except Exception as e:
            log.error(f"Cache refresh failed: {e}")

    def get_sensor(self, zigbee_id: str) -> Optional[dict]:
        with self.lock:
            return self.sensors.get(zigbee_id)

    def get_sensor_by_id(self, sensor_id: str) -> Optional[dict]:
        """Lookup by Supabase sensor.id (UUID). Used by the retrospective
        replay path which knows sensor_id from buffered reading rows but
        not the zigbee_id."""
        with self.lock:
            for sensor in self.sensors.values():
                if sensor.get("id") == sensor_id:
                    return sensor
            return None

    def get_cabinet(self, cabinet_id: str) -> Optional[dict]:
        with self.lock:
            return self.cabinets.get(cabinet_id)

    def all_sensors(self) -> list:
        with self.lock:
            return list(self.sensors.values())


# ════════════════════════════════════════════════════════════
# READING PROCESSOR
# Handles a single temperature reading end-to-end
# ════════════════════════════════════════════════════════════

class ReadingProcessor:
    def __init__(
        self,
        supabase: Client,
        cache: SensorCache,
        alert_engine,
        buffer: ReadingBuffer,
        outage_tracker: OutageTracker,
        drift_detector: Optional[DriftDetector] = None,
    ):
        self.supabase        = supabase
        self.cache           = cache
        self.alert_engine    = alert_engine
        self.buffer          = buffer
        self.outage_tracker  = outage_tracker
        self.drift_detector  = drift_detector

    def process(self, zigbee_id: str, payload: dict):
        """Process a reading from a Zigbee2MQTT message."""

        # ── 1. Lookup sensor in cache; auto-register if new ───
        sensor = self.cache.get_sensor(zigbee_id)
        if not sensor:
            sensor = self._register_new_sensor(zigbee_id, payload)
            if not sensor:
                return

        # ── 2. Extract temperature ────────────────────────────
        temperature = payload.get("temperature")
        if temperature is None:
            log.debug(f"No temperature in payload for {zigbee_id}")
            return

        try:
            temperature = round(float(temperature), 2)
        except (ValueError, TypeError):
            log.warning(f"Invalid temperature value: {temperature}")
            return

        battery   = payload.get("battery")
        link_quality = payload.get("linkquality")   # 0-255, convert to approx dBm
        rssi      = self._lqi_to_rssi(link_quality) if link_quality else None

        sensor_id  = sensor["id"]
        cabinet_id = sensor.get("cabinet_id")
        now        = datetime.now(timezone.utc).isoformat()

        log.info(f"Reading: {sensor.get('model','sensor')} {zigbee_id[-4:]} → {temperature}°C")

        # ── 3. Update sensor last_seen, battery, RSSI ─────────
        update_data = {
            "last_seen": now,
            "updated_at": now,
        }
        if battery is not None:
            update_data["battery_pct"] = int(battery)
        if rssi is not None:
            update_data["rssi"] = rssi

        try:
            self.supabase.table("sensors").update(update_data).eq("id", sensor_id).execute()
        except Exception as e:
            log.error(f"Failed to update sensor {sensor_id}: {e}")

        # ── 4. Write reading to Supabase (buffer on failure) ──
        if not cabinet_id:
            log.debug(f"Sensor {zigbee_id} not assigned to a cabinet — reading not logged")
            return

        reading_row = {
            "id":              str(uuid.uuid4()),
            "organisation_id": ORGANISATION_ID,
            "site_id":         SITE_ID,
            "cabinet_id":      cabinet_id,
            "sensor_id":       sensor_id,
            "temperature":     temperature,
            "recorded_at":     now,
        }

        reading_synced = False
        try:
            self.supabase.table("readings").insert(reading_row).execute()
            reading_synced = True
        except Exception as e:
            # Supabase unreachable or rejected — persist locally so the
            # reading isn't lost. Drain task syncs it on next tick.
            log.warning(f"Supabase reading insert failed, buffering: {e}")
            self.outage_tracker.mark_failed_write()
            try:
                self.buffer.enqueue(reading_row)
            except Exception as buf_err:
                log.error(f"Local buffer write also failed: {buf_err}")

        # If the reading didn't reach Supabase, skip alerting — the
        # AlertEngine would also fail to insert. Retrospective threshold
        # checks against buffered readings come in slice 2.
        if not reading_synced:
            return

        # ── 5. Threshold check + drift detection ──────────────
        cabinet = self.cache.get_cabinet(cabinet_id)
        if cabinet:
            self.alert_engine.check_temperature(
                cabinet=cabinet,
                sensor=sensor,
                temperature=temperature,
            )

            if self.drift_detector:
                self.drift_detector.add_reading(cabinet_id, temperature, now)
                self.drift_detector.check_drift(cabinet, sensor)

            # Keep cached sensor in sync with the row we just wrote so
            # state like low_signal_since reflects the latest DB value.
            sensor.update({k: v for k, v in update_data.items() if k != "updated_at"})

            if battery is not None:
                self.alert_engine.check_battery(
                    cabinet=cabinet,
                    sensor=sensor,
                    battery_pct=int(battery),
                )
            if rssi is not None:
                self.alert_engine.check_signal(
                    cabinet=cabinet,
                    sensor=sensor,
                    rssi=rssi,
                )

    @staticmethod
    def _lqi_to_rssi(lqi: int) -> int:
        """Approximate conversion from Zigbee LQI (0-255) to dBm."""
        return int(-100 + (lqi / 255) * 60)

    def _register_new_sensor(self, zigbee_id: str, payload: dict) -> Optional[dict]:
        """First time we see a sensor, insert a row in `sensors` so subsequent
        readings have somewhere to land. Cabinet assignment happens later via
        the local UI; until then readings are still recorded against the
        sensor but not against a cabinet.
        """
        try:
            res = self.supabase.table("sensors").insert({
                "organisation_id": ORGANISATION_ID,
                "site_id":         SITE_ID,
                "device_id":       DEVICE_ID,
                "zigbee_id":       zigbee_id,
                "model":           "SNZB-02LD",
                "last_seen":       datetime.now(timezone.utc).isoformat(),
                "active":          True,
            }).execute()
            row = res.data[0] if res.data else None
            if row:
                with self.cache.lock:
                    self.cache.sensors[zigbee_id] = row
                log.info(f"Registered new sensor {zigbee_id}")
            return row
        except Exception as e:
            # Race / duplicate insert — another reading may have inserted it.
            # Pull it back from Supabase and cache it.
            log.warning(f"Sensor insert for {zigbee_id} failed ({e}); refreshing cache")
            self.cache.refresh()
            return self.cache.get_sensor(zigbee_id)


# ════════════════════════════════════════════════════════════
# OFFLINE CHECKER
# Runs every minute, checks last_seen for all sensors
# ════════════════════════════════════════════════════════════

class OfflineChecker:
    def __init__(self, supabase: Client, cache: SensorCache, alert_engine):
        self.supabase      = supabase
        self.cache         = cache
        self.alert_engine  = alert_engine

    def check(self):
        """Check all assigned sensors for offline status."""
        try:
            # Fetch alert settings for this site
            settings_res = (
                self.supabase.table("alert_settings")
                .select("*")
                .eq("site_id", SITE_ID)
                .single()
                .execute()
            )
            settings = settings_res.data
            warn_mins     = settings.get("sensor_warn_mins", 15)
            critical_mins = settings.get("sensor_critical_mins", 30)
        except Exception as e:
            log.error(f"Could not fetch alert settings: {e}")
            warn_mins, critical_mins = 15, 30

        now = datetime.now(timezone.utc)

        for sensor in self.cache.all_sensors():
            if not sensor.get("cabinet_id"):
                continue  # Unassigned sensors don't generate offline alerts

            last_seen = sensor.get("last_seen")
            if not last_seen:
                continue

            try:
                if isinstance(last_seen, str):
                    last_seen_dt = datetime.fromisoformat(last_seen.replace("Z", "+00:00"))
                else:
                    last_seen_dt = last_seen

                minutes_silent = (now - last_seen_dt).total_seconds() / 60

                cabinet = self.cache.get_cabinet(sensor["cabinet_id"])
                if not cabinet:
                    continue

                if minutes_silent >= critical_mins:
                    self.alert_engine.raise_offline_alert(
                        cabinet=cabinet,
                        sensor=sensor,
                        severity="critical",
                        minutes_silent=int(minutes_silent),
                    )
                elif minutes_silent >= warn_mins:
                    self.alert_engine.raise_offline_alert(
                        cabinet=cabinet,
                        sensor=sensor,
                        severity="warning",
                        minutes_silent=int(minutes_silent),
                    )
                else:
                    # Sensor is back — resolve any offline alerts
                    self.alert_engine.resolve_offline_alert(sensor["id"])

            except Exception as e:
                log.error(f"Offline check error for sensor {sensor.get('id')}: {e}")


# ════════════════════════════════════════════════════════════
# MQTT CLIENT
# ════════════════════════════════════════════════════════════

class ChillCheckMQTT:
    def __init__(self, processor: ReadingProcessor):
        self.processor = processor
        self.client    = mqtt.Client(client_id="chillcheck-subscriber", clean_session=True)
        self.client.on_connect    = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message    = self._on_message

    def _on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            log.info(f"Connected to Mosquitto on {MQTT_HOST}:{MQTT_PORT}")
            # Subscribe to all device readings under zigbee2mqtt/
            # "#" wildcard catches all device topics
            client.subscribe(f"{MQTT_TOPIC_PREFIX}/#")
            log.info(f"Subscribed to {MQTT_TOPIC_PREFIX}/#")
        else:
            log.error(f"MQTT connection failed with code {rc}")

    def _on_disconnect(self, client, userdata, rc):
        if rc != 0:
            log.warning(f"Unexpected MQTT disconnect (rc={rc}) — will auto-reconnect")

    def _on_message(self, client, userdata, msg):
        """Handle incoming MQTT message from Zigbee2MQTT."""
        topic   = msg.topic
        payload_raw = msg.payload.decode("utf-8", errors="replace")

        # ── Filter to device reading topics ──────────────────
        # Zigbee2MQTT publishes to:
        #   zigbee2mqtt/<device_friendly_name>    ← readings (JSON)
        #   zigbee2mqtt/bridge/...                ← bridge status (skip)
        #   zigbee2mqtt/<device>/availability     ← availability (handle separately)

        parts = topic.split("/")
        if len(parts) < 2:
            return

        # Skip bridge topics
        if parts[1] == "bridge":
            return

        device_name = parts[1]

        # Handle availability messages
        if len(parts) == 3 and parts[2] == "availability":
            self._handle_availability(device_name, payload_raw)
            return

        # Skip other subtopics
        if len(parts) > 2:
            return

        # ── Parse reading payload ─────────────────────────────
        try:
            payload = json.loads(payload_raw)
        except json.JSONDecodeError:
            log.debug(f"Non-JSON payload on {topic}: {payload_raw[:50]}")
            return

        if not isinstance(payload, dict):
            return

        # Device friendly name in Z2M is set to the Zigbee ID
        # e.g. "0x00158d0001a2b3c4"
        self.processor.process(device_name, payload)

    def _handle_availability(self, device_name: str, payload: str):
        """Handle Zigbee2MQTT availability messages."""
        available = payload.strip().lower() in ("online", "true", '{"state":"online"}')
        status = "online" if available else "offline"
        log.debug(f"Availability: {device_name} → {status}")
        # Offline detection is handled by OfflineChecker using last_seen
        # This is just for logging

    def connect_and_loop(self):
        """Connect to Mosquitto and start the network loop."""
        log.info(f"Connecting to Mosquitto at {MQTT_HOST}:{MQTT_PORT}...")
        self.client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
        self.client.loop_forever(retry_first_connection=True)


# ════════════════════════════════════════════════════════════
# SCHEDULER
# Runs periodic tasks in a background thread
# ════════════════════════════════════════════════════════════

def run_scheduler():
    """Run the schedule loop in a background thread."""
    while True:
        schedule.run_pending()
        time.sleep(10)


# ════════════════════════════════════════════════════════════
# MAIN
# ════════════════════════════════════════════════════════════

def main():
    log.info("=" * 55)
    log.info("ChillCheck Subscriber starting")
    log.info(f"Organisation: {ORGANISATION_ID}")
    log.info(f"Site:         {SITE_ID}")
    log.info(f"Device:       {DEVICE_ID}")
    log.info("=" * 55)

    # ── Initialise Supabase ───────────────────────────────────
    supabase = get_supabase()
    log.info("Supabase client initialised")

    # ── Mark device as online ─────────────────────────────────
    try:
        supabase.table("devices").update({
            "status": "online",
            "last_heartbeat": datetime.now(timezone.utc).isoformat(),
        }).eq("id", DEVICE_ID).execute()
        log.info("Device marked online in Supabase")
    except Exception as e:
        log.error(f"Failed to mark device online: {e}")

    # ── Build services ────────────────────────────────────────
    cache            = SensorCache(supabase)
    alert_engine     = AlertEngine(supabase, ORGANISATION_ID, SITE_ID, DEVICE_ID)
    reading_buffer   = ReadingBuffer(db_path=BUFFER_DB_PATH)
    outage_tracker   = OutageTracker(state_path=OUTAGE_STATE_PATH)
    drift_detector   = DriftDetector(supabase, ORGANISATION_ID, SITE_ID, DEVICE_ID)
    processor        = ReadingProcessor(supabase, cache, alert_engine, reading_buffer, outage_tracker, drift_detector)
    offline_check    = OfflineChecker(supabase, cache, alert_engine)
    heartbeat      = HeartbeatService(supabase, DEVICE_ID)

    initial_buffer_size = reading_buffer.size()
    if initial_buffer_size > 0:
        log.warning(f"Startup: {initial_buffer_size} reading(s) pending in local buffer from previous session")

    def replay_threshold_checks(drained_rows: list, outage_duration_seconds: int):
        """Walk freshly-synced rows through the threshold checker so any
        breaches that occurred during the outage raise alerts now. Order
        matches recorded_at — the same order ``drain()`` yielded them —
        so an out → back → out pattern resolves correctly mid-replay.
        """
        replayed = 0
        for row in drained_rows:
            cabinet = cache.get_cabinet(row["cabinet_id"])
            if not cabinet:
                # Cabinet deleted between buffering and drain — skip cleanly
                continue
            sensor = cache.get_sensor_by_id(row["sensor_id"]) or {"id": row["sensor_id"]}
            try:
                alert_engine.check_temperature(
                    cabinet=cabinet,
                    sensor=sensor,
                    temperature=row["temperature"],
                    recorded_at=row["recorded_at"],
                    outage_duration_seconds=outage_duration_seconds,
                )
                replayed += 1
            except Exception as e:
                log.error(f"Retrospective check failed for reading {row.get('id')}: {e}")
        if replayed:
            log.info(
                f"Replayed {replayed} buffered reading(s) through threshold checks "
                f"(outage was {outage_duration_seconds}s)"
            )

    # Running total of readings drained in the current reconnect window.
    # Reset to 0 once the buffer is fully empty and the outage is cleared.
    total_drained_this_outage = [0]

    def emit_connectivity_restored(duration_seconds: int, buffered_count: int):
        """Write device.connectivity_restored audit row and send reconnect email.
        Both are best-effort — failures are logged but never raise.
        """
        try:
            supabase.table("audit_log").insert({
                "organisation_id": ORGANISATION_ID,
                "profile_id":      None,
                "action":          "device.connectivity_restored",
                "metadata": {
                    "device_id":               DEVICE_ID,
                    "offline_duration_seconds": duration_seconds,
                    "buffered_reading_count":   buffered_count,
                },
            }).execute()
            log.info(
                f"Emitted device.connectivity_restored audit "
                f"(duration={duration_seconds}s, buffered={buffered_count})"
            )
        except Exception as e:
            log.error(f"Failed to write connectivity_restored audit: {e}")
        # Only notify for outages long enough to matter (5+ min = same floor
        # as retrospective alert tiering). Shorter blips are implementation
        # noise and don't warrant emailing contacts.
        if duration_seconds >= RETRO_ALERT_MIN_SECONDS:
            send_connectivity_restored(duration_seconds, buffered_count)

    def drain_reading_buffer():
        if reading_buffer.size() == 0:
            return
        # Peek the outage duration BEFORE draining so all batches in a
        # single recovery use the same tiering. drain() works in 100-row
        # batches, so a 1000-row outage takes 10 ticks; clearing state
        # after the first batch would leave later batches without context.
        duration = outage_tracker.peek_duration_seconds()
        drained_rows, remaining = reading_buffer.drain(supabase)
        if not drained_rows:
            # Drain hit a still-down endpoint — leave the outage window open.
            return
        total_drained_this_outage[0] += len(drained_rows)
        if duration is not None and duration >= RETRO_ALERT_MIN_SECONDS:
            replay_threshold_checks(drained_rows, duration)
        if remaining == 0:
            # Buffer fully drained — outage is officially over.
            duration_final = outage_tracker.clear()
            if duration_final is not None:
                emit_connectivity_restored(duration_final, total_drained_this_outage[0])
            total_drained_this_outage[0] = 0

    # ── Schedule periodic tasks ───────────────────────────────
    schedule.every(5).minutes.do(cache.refresh)
    schedule.every(1).minutes.do(offline_check.check)
    schedule.every(5).minutes.do(heartbeat.ping)
    schedule.every(1).minutes.do(alert_engine.process_escalations)
    schedule.every(1).minutes.do(drain_reading_buffer)
    # Weekly battery health digest. Endpoint is a no-op when no sensors are low,
    # so it's safe to fire on a fixed cadence without filtering on this end.
    schedule.every().monday.at("09:00").do(send_battery_digest)

    log.info("Scheduled tasks registered")

    # ── Start scheduler in background thread ──────────────────
    scheduler_thread = threading.Thread(target=run_scheduler, daemon=True)
    scheduler_thread.start()
    log.info("Scheduler running in background")

    # Run initial checks immediately
    offline_check.check()
    heartbeat.ping()
    drain_reading_buffer()

    # ── Start MQTT loop (blocking) ────────────────────────────
    mqtt_client = ChillCheckMQTT(processor)
    try:
        mqtt_client.connect_and_loop()
    except KeyboardInterrupt:
        log.info("Shutting down...")
    finally:
        # Mark device offline on clean shutdown
        try:
            supabase.table("devices").update({
                "status": "offline",
            }).eq("id", DEVICE_ID).execute()
        except Exception:
            pass


if __name__ == "__main__":
    main()
