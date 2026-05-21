"""
ChillCheck — Local UI
======================
Flask app served at http://chillcheck.local (port 80).
Accessible from any device on the same network as the Pi.

Scope: sensor pairing and hub health only. Cabinet creation and sensor-to-
cabinet assignment live in the cloud dashboard at app.chillcheck.online.

Routes:
  GET  /                    → Dashboard (redirect to /sensors)
  GET  /sensors             → Paired sensor list + pairing flow
  GET  /network             → Network / Wi-Fi config page
  GET  /system              → System status page

API routes (called by the frontend JS):
  GET  /api/sensors         → List paired sensors with status
  POST /api/sensors/pair    → Enable Zigbee pairing mode
  GET  /api/network/status  → Current network status
  GET  /api/network/scan    → Scan for Wi-Fi networks
  POST /api/network/connect → Connect to a Wi-Fi network
  GET  /api/system/status   → Service and system status
  POST /api/system/restart  → Restart a named service
  POST /api/cloud/pair      → Exchange pairing code for credentials
  GET  /api/cloud/status    → Cloud connection status
"""

import os
import json
import subprocess
import threading
import logging
from datetime import datetime, timedelta, timezone
from functools import wraps

from flask import Flask, jsonify, request, render_template_string, redirect, session
from flask_cors import CORS
from dotenv import load_dotenv

import auth as ui_auth

load_dotenv("/etc/chillcheck/.env")

# ── Config ────────────────────────────────────────────────────
SUPABASE_URL        = os.getenv("SUPABASE_URL", "")
SUPABASE_SERVICE_KEY= os.getenv("SUPABASE_SERVICE_KEY", "")
ORGANISATION_ID     = os.getenv("ORGANISATION_ID", "")
SITE_ID             = os.getenv("SITE_ID", "")
DEVICE_ID           = os.getenv("DEVICE_ID", "")
NOTIFY_SECRET       = os.getenv("NOTIFY_SECRET", "")
PORT                = int(os.getenv("LOCAL_UI_PORT", 80))
VERCEL_URL          = os.getenv("VERCEL_URL", "https://app.chillcheck.online")

# ── Logging ───────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("chillcheck.local_ui")

# ── Flask app ─────────────────────────────────────────────────
app = Flask(__name__)
CORS(app)

# ── Auth state (loaded once at startup; mutated in-place on change) ──
_auth_state = ui_auth.load_or_init()
app.secret_key = _auth_state["session_secret"]
app.permanent_session_lifetime = timedelta(days=30)

# Endpoints accessible without an authed session.
_PUBLIC_API_PATHS = {
    "/api/auth/status",
    "/api/auth/login",
    "/api/auth/logout",
}


@app.before_request
def _gate_api():
    """Single auth gate for every /api/* route except the public auth ones.

    HTML views are not gated here — the SPA renders the login view itself
    based on /api/auth/status, so the page shell can still load when
    signed out.
    """
    path = request.path
    if not path.startswith("/api/"):
        return None
    if path in _PUBLIC_API_PATHS:
        return None
    if not session.get("authed"):
        return jsonify({"error": "unauthorized"}), 401
    if session.get("must_change") and path != "/api/auth/change-password":
        return jsonify({"error": "password_change_required"}), 403
    return None

# ── Pairing mode state ────────────────────────────────────────
pairing_active   = False
pairing_timer    = None
PAIRING_TIMEOUT  = 120  # seconds


# ════════════════════════════════════════════════════════════
# SUPABASE HELPER
# ════════════════════════════════════════════════════════════

def get_supabase():
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        return None
    try:
        from supabase import create_client
        return create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
    except Exception as e:
        log.error(f"Supabase init failed: {e}")
        return None

def is_cloud_connected() -> bool:
    return bool(ORGANISATION_ID and SITE_ID and DEVICE_ID and SUPABASE_URL)


# ════════════════════════════════════════════════════════════
# HTML TEMPLATE
# Single-page app shell — React/JS loads the real UI
# Falls back gracefully if JS fails
# ════════════════════════════════════════════════════════════

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>ChillCheck Hub</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link href="https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wght@12..96,400..800&family=Geist:wght@300..800&family=JetBrains+Mono:wght@400;500;700&display=swap" rel="stylesheet">
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { font-family: 'Geist', system-ui, sans-serif; background: #F7F2E7; color: #0E1A33; min-height: 100vh; }
    body::before { content:''; position: fixed; inset:0; pointer-events:none; z-index:0;
      background: radial-gradient(circle at 18% 12%, rgba(255,196,80,0.10), transparent 38%),
                  radial-gradient(circle at 88% 78%, rgba(46,72,254,0.07), transparent 40%); }
    #app { display: flex; align-items: center; justify-content: center; min-height: 100vh; position: relative; z-index: 1; }
    .cc-load-mark { font-family: 'Bricolage Grotesque', system-ui, sans-serif; font-weight: 800; font-size: 36px; line-height: 0.95; letter-spacing: -0.035em; text-align: center; }
    .cc-load-mark span { color: #2E48FE; }
    .cc-load-sub { font-size: 10px; color: #9097A8; letter-spacing: 0.18em; text-transform: uppercase; margin-top: 12px; font-family: 'JetBrains Mono', monospace; text-align: center; }
    .cc-nav { display: flex; overflow-x: auto; -webkit-overflow-scrolling: touch; scrollbar-width: none; }
    .cc-nav::-webkit-scrollbar { display: none; }
    @keyframes ping { 0% { transform: scale(1); opacity: .6 } 100% { transform: scale(2.2); opacity: 0 } }
    @keyframes spin  { to { transform: rotate(360deg); } }
    @media (max-width: 700px) { .cc-two-col { grid-template-columns: 1fr !important; } }
  </style>
</head>
<body>
  <div id="app">
    <div>
      <div class="cc-load-mark">chillcheck<span>.</span></div>
      <div class="cc-load-sub">loading…</div>
    </div>
  </div>

  <script>
    const API = '';

    let state = {
      view: '{{ initial_view }}',
      cloudConnected: {{ 'true' if cloud_connected else 'false' }},
      authed: false,
      mustChangePassword: false,
      authChecked: false,
      loginUsername: 'admin',
      loginPassword: '',
      loginError: '',
      changePwError: '',
      changePwBusy: false,
      sensors: [],
      networkStatus: null,
      networks: [],
      systemStatus: null,
      versionInfo: null,
      updateInProgress: false,
      pairingActive: false,
      pairingStep: 0,
      logsUnit: 'chillcheck-subscriber',
      logsContent: '',
      logsLoading: false,
      logsError: '',
      logsAutoRefresh: false,
      logsTimer: null,
    };

    const $ = id => document.getElementById(id);
    const esc = s => String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');

    async function api(method, path, body) {
      const opts = { method, headers: { 'Content-Type': 'application/json' } };
      if (body) opts.body = JSON.stringify(body);
      const res = await fetch(API + path, opts);
      return res.json();
    }

    function render() {
      // Reset the loader-only centering on #app so rendered content fills
      // the viewport instead of shrinking to its intrinsic width.
      const app = document.getElementById('app');
      app.style.display = 'block';
      app.style.alignItems = '';
      app.style.justifyContent = '';
      app.style.minHeight = '';
      app.innerHTML = layout();
    }

    function layout() {
      // Until /api/auth/status resolves we keep the loader visible.
      if (!state.authChecked) {
        return `
          <div style="min-height:100vh;display:flex;align-items:center;justify-content:center;background:#F7F2E7">
            <div>
              <div class="cc-load-mark">ChillCheck<span>.</span></div>
              <div class="cc-load-sub">loading…</div>
            </div>
          </div>
        `;
      }
      // Auth screens render bare — no nav, no utility bar.
      if (!state.authed) {
        return `
          <div style="min-height:100vh;display:flex;align-items:center;justify-content:center;background:#F7F2E7;color:#0E1A33;font-family:'Geist',sans-serif;padding:20px">
            ${viewLogin()}
          </div>
        `;
      }
      if (state.mustChangePassword) {
        return `
          <div style="min-height:100vh;display:flex;align-items:center;justify-content:center;background:#F7F2E7;color:#0E1A33;font-family:'Geist',sans-serif;padding:20px">
            ${viewChangePassword(true)}
          </div>
        `;
      }
      return `
        <div style="min-height:100vh;display:flex;flex-direction:column;background:#F7F2E7;color:#0E1A33;font-family:'Geist',sans-serif">
          ${utilityBar()}
          ${pageHeader()}
          <main style="padding:28px;flex:1">
            ${mainContent()}
          </main>
        </div>
      `;
    }

    function utilityBar() {
      const right = state.cloudConnected
        ? `<span style="width:6px;height:6px;border-radius:50%;background:#5FB28C;box-shadow:0 0 0 2px rgba(95,178,140,0.2);display:inline-block;flex-shrink:0"></span>
           <span style="text-transform:uppercase">${state.cloudInfo?.org_name ? 'cloud connected · ' + esc(state.cloudInfo.org_name) : 'cloud connected'}</span>
           <a href="${'{{ vercel_url }}'}" target="_blank" style="color:#9097A8;text-decoration:none;border-left:1px solid #4A4A45;padding-left:10px;margin-left:6px">Open dashboard ↗</a>`
        : `<span style="width:6px;height:6px;border-radius:50%;background:#C97A1A;display:inline-block;flex-shrink:0"></span>
           <span style="text-transform:uppercase;color:#9097A8">not linked to cloud</span>`;
      const signout = `
        <button onclick="doLogout()" style="background:transparent;border:none;color:#9097A8;text-transform:uppercase;letter-spacing:0.06em;font-size:11px;cursor:pointer;font-family:inherit;border-left:1px solid #4A4A45;padding-left:10px;margin-left:6px">Sign out</button>
      `;
      return `
        <div style="background:#0E1A33;color:#9097A8;padding:10px 28px;display:flex;justify-content:space-between;align-items:center;font-size:11px;letter-spacing:0.06em;font-family:'Geist',sans-serif;gap:16px;flex-wrap:wrap">
          <span style="font-family:'JetBrains Mono',monospace">chillcheck.local</span>
          <div style="display:flex;align-items:center;gap:8px">${right}${signout}</div>
        </div>
      `;
    }

    function pageHeader() {
      const tabs = [
        { id: 'connect', label: 'Cloud Link', dot: !state.cloudConnected },
        { id: 'sensors', label: 'Sensors' },
        { id: 'network', label: 'Network' },
        { id: 'logs',    label: 'Logs' },
        { id: 'system',  label: 'System' },
      ];
      return `
        <header style="padding:22px 28px 0;border-bottom:1px solid #EEE7D3;background:#F7F2E7">
          <div style="display:flex;align-items:flex-end;justify-content:space-between;margin-bottom:18px;gap:16px;flex-wrap:wrap">
            <div>
              <div style="font-family:'Instrument Serif',Georgia,serif;font-size:34px;line-height:0.95;letter-spacing:-0.02em">
                ChillCheck<span style="color:#C97A1A">.</span> <span style="font-style:italic;font-size:24px;color:#565E78">hub</span>
              </div>
              <div style="font-size:10px;color:#565E78;letter-spacing:0.18em;text-transform:uppercase;margin-top:6px">local installer console</div>
            </div>
          </div>
          <nav class="cc-nav">
            ${tabs.map(t => `
              <button onclick="navigate('${t.id}')"
                style="background:transparent;border:none;border-bottom:${state.view===t.id?'2px solid #0E1A33':'2px solid transparent'};
                  padding:12px 16px;font-size:12px;font-weight:${state.view===t.id?600:500};letter-spacing:0.06em;
                  text-transform:uppercase;white-space:nowrap;color:${state.view===t.id?'#0E1A33':'#565E78'};
                  cursor:pointer;font-family:inherit;display:inline-flex;align-items:center;gap:6px;flex-shrink:0">
                ${t.label}
                ${t.dot ? '<span style="width:6px;height:6px;border-radius:50%;background:#C97A1A;display:inline-block"></span>' : ''}
              </button>
            `).join('')}
          </nav>
        </header>
      `;
    }

    function mainContent() {
      switch(state.view) {
        case 'connect': return viewConnect();
        case 'sensors': return viewSensors();
        case 'network': return viewNetwork();
        case 'logs':    return viewLogs();
        case 'system':  return viewSystem();
        case 'change_password': return viewChangePassword(false);
        default:        return viewSensors();
      }
    }

    // ── Views ────────────────────────────────────────────────

    function viewConnect() {
      if (state.cloudConnected) {
        return `
          <div style="max-width:480px">
            <h1 style="font-family:'Instrument Serif',Georgia,serif;font-size:36px;font-weight:400;letter-spacing:-0.02em;margin:0 0 8px">Cloud Link</h1>
            <p style="font-size:13px;color:#565E78;margin:0 0 24px">This hub is linked to ChillCheck Cloud.</p>
            <div style="background:#FFFFFF;border:1px solid #EEE7D3;padding:22px 24px;margin-bottom:16px">
              <div style="display:flex;align-items:center;gap:8px;margin-bottom:16px">
                <span style="width:8px;height:8px;border-radius:50%;background:#1E6F4F;display:inline-block"></span>
                <span style="font-size:13px;font-weight:600;color:#1E6F4F;letter-spacing:0.04em;text-transform:uppercase">Connected</span>
              </div>
              <div style="font-size:12px;font-family:'JetBrains Mono',monospace;color:#565E78;line-height:2">
                <div style="display:flex;justify-content:space-between;border-bottom:1px solid #F7F2E7;padding-bottom:6px;margin-bottom:6px">
                  <span>organisation</span><span style="color:#0E1A33">${esc(state.cloudInfo?.org_name||'—')}</span>
                </div>
                <div style="display:flex;justify-content:space-between;border-bottom:1px solid #F7F2E7;padding-bottom:6px;margin-bottom:6px">
                  <span>site</span><span style="color:#0E1A33">${esc(state.cloudInfo?.site_name||'—')}</span>
                </div>
                <div style="display:flex;justify-content:space-between">
                  <span>dashboard</span>
                  <a href="${'{{ vercel_url }}'}" target="_blank" style="color:#0E1A33">app.chillcheck.online ↗</a>
                </div>
              </div>
            </div>
            <button onclick="disconnectCloud()"
              style="background:transparent;border:1px solid #C72717;color:#C72717;padding:10px 16px;cursor:pointer;font-size:11px;font-weight:600;font-family:inherit;letter-spacing:0.08em;text-transform:uppercase">
              Disconnect from Cloud
            </button>
          </div>
        `;
      }

      return `
        <div style="display:grid;grid-template-columns:1fr 360px;gap:36px;align-items:start" class="cc-two-col">
          <section>
            <h1 style="font-family:'Instrument Serif',Georgia,serif;font-size:38px;font-weight:400;letter-spacing:-0.02em;margin:0 0 8px">This Pi isn't linked yet.</h1>
            <p style="font-size:14px;color:#565E78;line-height:1.6;max-width:560px;margin:0 0 28px">
              Link this hub to your ChillCheck cloud account so its sensor readings show up in the dashboard and alerts get sent to your team.
            </p>
            <div style="background:#FFFFFF;border:1px solid #EEE7D3;padding:24px 28px;margin-bottom:20px">
              <div style="font-size:10px;color:#9097A8;letter-spacing:0.18em;text-transform:uppercase;margin-bottom:12px;font-family:'JetBrains Mono',monospace">step 1 of 2 · on your computer</div>
              <p style="font-size:14px;color:#0E1A33;line-height:1.6;margin:0 0 10px">
                Sign in to <span style="font-family:'JetBrains Mono',monospace;font-size:12px;background:#F7F2E7;padding:2px 6px">app.chillcheck.online</span>, open <strong>Settings → Devices</strong>, and tap "Pair a Pi". You'll get an 8-character code.
              </p>
              <div style="font-size:11px;color:#9097A8;font-family:'JetBrains Mono',monospace">codes expire after 10 minutes</div>
            </div>
            <div style="background:#0E1A33;color:#F7F2E7;padding:24px 28px">
              <div style="font-size:10px;color:#9097A8;letter-spacing:0.18em;text-transform:uppercase;margin-bottom:14px;font-family:'JetBrains Mono',monospace">step 2 · enter the code here</div>
              <label style="font-size:10px;color:#9097A8;letter-spacing:0.12em;text-transform:uppercase;display:block;margin-bottom:8px;font-family:'JetBrains Mono',monospace">Site name (optional)</label>
              <input id="siteName" placeholder="e.g. Pup Planet Wolverhampton"
                style="width:100%;padding:10px 12px;font-size:13px;border:1px solid #2a2a25;background:#0a0a0a;color:#F7F2E7;font-family:inherit;outline:none;margin-bottom:16px">
              <label style="font-size:10px;color:#9097A8;letter-spacing:0.12em;text-transform:uppercase;display:block;margin-bottom:8px;font-family:'JetBrains Mono',monospace">Pairing code</label>
              <input id="pairingCode" placeholder="WOLF-4821" maxlength="9"
                oninput="this.value=this.value.toUpperCase().replace(/[^A-Z0-9-]/g,'')"
                style="width:100%;padding:10px 12px;font-size:24px;font-family:'JetBrains Mono',monospace;letter-spacing:0.2em;text-align:center;border:1px solid #2a2a25;background:#0a0a0a;color:#F7F2E7;outline:none;margin-bottom:8px">
              <div id="pairError" style="display:none;background:#2a0a0a;border:1px solid #C72717;padding:10px 14px;margin-bottom:12px;font-size:13px;color:#E5B0A8"></div>
              <button onclick="submitPairingCode()"
                style="background:#C97A1A;border:none;color:#0E1A33;padding:12px 20px;font-size:11px;font-weight:600;letter-spacing:0.08em;text-transform:uppercase;cursor:pointer;font-family:inherit;margin-top:6px">
                Link this Pi →
              </button>
            </div>
          </section>
          <aside>
            <div style="font-size:10px;color:#9097A8;letter-spacing:0.18em;text-transform:uppercase;margin-bottom:10px;font-family:'JetBrains Mono',monospace">connectivity</div>
            ${[
              { label: 'Cloud API', ok: true, meta: 'app.chillcheck.online' },
              { label: 'Time sync', ok: true, meta: 'ntp ok' },
            ].map(row => `
              <div style="display:flex;justify-content:space-between;align-items:flex-start;padding:12px 0;border-bottom:1px solid #EEE7D3">
                <div>
                  <div style="font-size:13px;font-weight:500">${row.label}</div>
                  <div style="font-size:11px;color:#9097A8;font-family:'JetBrains Mono',monospace;margin-top:2px">${row.meta}</div>
                </div>
                <div style="display:flex;align-items:center;gap:6px">
                  <span style="width:6px;height:6px;border-radius:50%;background:${row.ok?'#1E6F4F':'#A02216'};display:inline-block"></span>
                  <span style="font-size:11px;font-weight:600;color:${row.ok?'#1E6F4F':'#9A1B11'};text-transform:uppercase;letter-spacing:0.06em">${row.ok?'ok':'error'}</span>
                </div>
              </div>
            `).join('')}
            <div style="margin-top:24px;padding:14px 16px;background:#F4F1E8;border:1px solid #EEE7D3;font-size:12px;color:#565E78;line-height:1.55">
              <strong style="color:#0E1A33">No account yet?</strong><br>
              Go to <span style="font-family:'JetBrains Mono',monospace">chillcheck.online</span> to sign up. The first 30 days are free.
            </div>
          </aside>
        </div>
      `;
    }

    function viewSensors() {
      const unassigned = state.sensors.filter(s => !s.cabinet_id);
      const assigned   = state.sensors.filter(s =>  s.cabinet_id);
      return `
        <div style="display:flex;justify-content:space-between;align-items:flex-end;margin-bottom:20px;gap:12px;flex-wrap:wrap">
          <div>
            <h1 style="font-family:'Instrument Serif',Georgia,serif;font-size:36px;font-weight:400;letter-spacing:-0.02em;margin:0">Sensors</h1>
            <div style="font-size:11px;color:#565E78;letter-spacing:0.12em;text-transform:uppercase;margin-top:4px;font-family:'JetBrains Mono',monospace">
              ${state.sensors.length} paired · ${unassigned.length} awaiting assignment
            </div>
          </div>
          <div style="display:flex;gap:8px">
            <button onclick="refreshSensors()" style="background:transparent;color:#0E1A33;border:1px solid #EEE7D3;padding:9px 14px;cursor:pointer;font-size:11px;font-weight:600;letter-spacing:0.06em;text-transform:uppercase;font-family:inherit">Refresh</button>
            <button onclick="startPairing()" style="background:#0E1A33;color:#F7F2E7;border:none;padding:9px 16px;cursor:pointer;font-size:11px;font-weight:600;letter-spacing:0.08em;text-transform:uppercase;font-family:inherit">+ Pair sensor</button>
          </div>
        </div>

        ${state.pairingActive ? pairingModal() : ''}

        <div style="background:#F4F1E8;border:1px solid #EEE7D3;padding:12px 16px;margin-bottom:20px;font-size:12px;color:#565E78;line-height:1.55">
          Cabinet creation and sensor assignment are managed in the cloud dashboard at
          <a href="https://app.chillcheck.online/dashboard/sensors" target="_blank" rel="noopener" style="color:#0E1A33;font-weight:600;text-decoration:underline">app.chillcheck.online</a>.
          This page is for pairing and hub health only.
        </div>

        ${unassigned.length > 0 ? `
          <div style="font-size:10px;color:#9097A8;letter-spacing:0.18em;text-transform:uppercase;margin-bottom:10px;font-family:'JetBrains Mono',monospace">awaiting assignment (${unassigned.length})</div>
          ${unassigned.map(s => unassignedCard(s)).join('')}
          <div style="margin-bottom:24px"></div>
        ` : ''}

        <div style="font-size:10px;color:#9097A8;letter-spacing:0.18em;text-transform:uppercase;margin-bottom:10px;font-family:'JetBrains Mono',monospace">assigned (${assigned.length})</div>
        ${assigned.length === 0
          ? '<p style="font-size:13px;color:#9097A8">No sensors assigned yet. Pair a sensor here, then assign it in the cloud dashboard.</p>'
          : `<div style="background:#FFFFFF;border:1px solid #EEE7D3">
               <div style="display:grid;grid-template-columns:1fr 180px 70px 80px 110px;padding:10px 20px;background:#F7F2E7;border-bottom:1px solid #EEE7D3;font-size:10px;color:#565E78;letter-spacing:0.12em;text-transform:uppercase;font-family:'JetBrains Mono',monospace">
                 <span>cabinet</span><span>sensor id</span><span>signal</span><span>battery</span><span>last seen</span>
               </div>
               ${assigned.map((s,i,a) => sensorRow(s, i, a.length)).join('')}
             </div>`
        }
      `;
    }

    function unassignedCard(s) {
      return `
        <div style="background:#FFFFFF;border:1px dashed #C97A1A;padding:16px 20px;display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;gap:12px;flex-wrap:wrap">
          <div>
            <div style="font-size:14px;font-weight:600;margin-bottom:4px">${esc(s.name || 'New sensor')}</div>
            <div style="font-size:11px;font-family:'JetBrains Mono',monospace;color:#565E78;margin-bottom:6px">···${esc(s.zigbee_id?.slice(-4)||'????')} · ${esc(s.last_seen_ago||'unknown')}</div>
            <div style="display:flex;gap:12px;align-items:center">${signalBars(s.rssi)} ${batteryBadge(s.battery_pct)}</div>
          </div>
          <a href="https://app.chillcheck.online/dashboard/sensors" target="_blank" rel="noopener"
            style="background:#C97A1A;color:#0E1A33;border:none;padding:9px 14px;cursor:pointer;font-size:11px;font-weight:600;letter-spacing:0.08em;text-transform:uppercase;font-family:inherit;text-decoration:none">
            Assign in cloud →
          </a>
        </div>
      `;
    }

    function sensorRow(s, i, total) {
      const offline = s.minutes_since_seen > 30;
      return `
        <div style="display:grid;grid-template-columns:1fr 180px 70px 80px 110px;align-items:center;padding:14px 20px;border-bottom:${i<total-1?'1px solid #F7F2E7':'none'};background:${offline?'#F4EDE8':'transparent'}">
          <span style="font-size:13px;font-weight:600">${esc(s.cabinet_name||'Unknown')}</span>
          <span style="font-size:11px;font-family:'JetBrains Mono',monospace;color:#565E78">···${esc(s.zigbee_id?.slice(-4)||'????')}</span>
          <span>${signalBars(s.rssi)}</span>
          <span>${batteryBadge(s.battery_pct)}</span>
          <span style="font-size:11px;font-family:'JetBrains Mono',monospace;color:${offline?'#C72717':'#565E78'}">${esc(s.last_seen_ago||'—')}</span>
        </div>
      `;
    }

    function pairingModal() {
      const steps = [
        { title: 'Put sensor into pairing mode', desc: 'Hold the reset button on the SNZB-02LD for 5 seconds until the LED flashes rapidly.' },
        { title: 'Waiting for sensor…',          desc: 'ChillCheck is scanning for a new Zigbee device. This usually takes 10–30 seconds.' },
        { title: 'Sensor found!',                desc: 'New sensor detected. You can now assign it to a cabinet.' },
      ];
      return `
        <div style="position:fixed;inset:0;background:rgba(0,0,0,0.6);z-index:100;display:flex;align-items:center;justify-content:center">
          <div style="background:#FFFFFF;border:1px solid #EEE7D3;padding:28px;width:440px;max-width:90vw">
            <div style="font-size:16px;font-weight:700;margin-bottom:4px">Pair New Sensor</div>
            <div style="font-size:13px;color:#565E78;margin-bottom:24px">Follow the steps below to add a new SNZB-02LD</div>
            ${steps.map((step,i) => `
              <div style="display:flex;gap:14px;margin-bottom:18px;opacity:${state.pairingStep>=i+1?1:0.35}">
                <div style="width:26px;height:26px;flex-shrink:0;
                  background:${state.pairingStep>i+1?'#1E6F4F':state.pairingStep===i+1?'#0E1A33':'#EEE7D3'};
                  color:${state.pairingStep>=i+1?'#F7F2E7':'#9097A8'};
                  display:flex;align-items:center;justify-content:center;font-size:11px;font-weight:700;font-family:'JetBrains Mono',monospace">
                  ${state.pairingStep>i+1?'✓':i+1}
                </div>
                <div>
                  <div style="font-size:13px;font-weight:600;margin-bottom:2px">${step.title}</div>
                  <div style="font-size:12px;color:#565E78;line-height:1.5">${step.desc}</div>
                </div>
              </div>
            `).join('')}
            ${state.pairingStep===2
              ? `<div style="display:flex;align-items:center;gap:8px;padding:10px 14px;background:#F7F2E7;margin-bottom:16px">
                   <div style="width:12px;height:12px;border:2px solid #EEE7D3;border-top-color:#C97A1A;border-radius:50%;animation:spin 0.8s linear infinite"></div>
                   <span style="font-size:12px;color:#565E78">Scanning for Zigbee devices…</span>
                 </div>` : ''
            }
            ${state.pairingStep===3
              ? `<div style="background:#EAF3EF;border:1px solid #1E6F4F;padding:10px 14px;margin-bottom:16px;font-size:13px;color:#1E6F4F;font-weight:500">New sensor detected and ready to assign</div>` : ''
            }
            <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:4px">
              <button onclick="cancelPairing()" style="background:transparent;color:#0E1A33;border:1px solid #EEE7D3;padding:9px 14px;cursor:pointer;font-size:11px;letter-spacing:0.06em;text-transform:uppercase;font-family:inherit">Cancel</button>
              ${state.pairingStep<3
                ? `<button onclick="advancePairing()" style="background:#0E1A33;color:#F7F2E7;border:none;padding:9px 16px;cursor:pointer;font-size:11px;font-weight:600;letter-spacing:0.08em;text-transform:uppercase;font-family:inherit">${state.pairingStep===0?'Start':state.pairingStep===1?'Next →':'...'}</button>`
                : ''
              }
              ${state.pairingStep===3
                ? `<button onclick="cancelPairing();refreshSensors();" style="background:#0E1A33;color:#F7F2E7;border:none;padding:9px 16px;cursor:pointer;font-size:11px;font-weight:600;letter-spacing:0.08em;text-transform:uppercase;font-family:inherit">Done</button>`
                : ''
              }
            </div>
          </div>
        </div>
      `;
    }

    function viewNetwork() {
      const status = state.networkStatus || {};
      return `
        <div style="display:grid;grid-template-columns:1fr 340px;gap:36px;align-items:start" class="cc-two-col">
          <section>
            <h1 style="font-family:'Instrument Serif',Georgia,serif;font-size:36px;font-weight:400;letter-spacing:-0.02em;margin:0 0 4px">Network</h1>
            <p style="font-size:13px;color:#565E78;margin:0 0 24px">Connection status and Wi-Fi configuration</p>

            ${status.wifi_connected || status.eth_connected ? `
              <div style="background:#FFFFFF;border:1px solid #EEE7D3;padding:18px 22px;margin-bottom:20px">
                <div style="font-size:10px;color:#1E6F4F;letter-spacing:0.12em;text-transform:uppercase;font-weight:600;margin-bottom:6px">connected</div>
                <div style="font-size:18px;font-weight:600;margin-bottom:4px">${esc(status.wifi_ssid || (status.eth_connected ? 'Ethernet' : '—'))}</div>
                <div style="font-size:11px;color:#565E78;font-family:'JetBrains Mono',monospace;line-height:1.8">
                  ${status.eth_ip||status.wifi_ip ? esc(status.eth_ip||status.wifi_ip) : '—'} · gateway ${esc(status.gateway||'—')}<br>mDNS: chillcheck.local
                </div>
              </div>
            ` : `
              <div style="background:#FFFFFF;border:1px solid #EEE7D3;padding:18px 22px;margin-bottom:20px">
                <div style="font-size:10px;color:#9097A8;letter-spacing:0.12em;text-transform:uppercase;font-weight:600;margin-bottom:6px">disconnected</div>
                <div style="font-size:13px;color:#565E78">No active network connection detected.</div>
              </div>
            `}

            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px">
              <div style="font-size:10px;color:#9097A8;letter-spacing:0.18em;text-transform:uppercase;font-family:'JetBrains Mono',monospace">available networks</div>
              <button onclick="scanNetworks()" style="background:transparent;color:#0E1A33;border:1px solid #EEE7D3;padding:7px 12px;cursor:pointer;font-size:11px;letter-spacing:0.06em;text-transform:uppercase;font-family:inherit">Scan</button>
            </div>
            ${state.networks.length === 0
              ? '<p style="font-size:13px;color:#9097A8">Click Scan to search for Wi-Fi networks</p>'
              : `<div style="background:#FFFFFF;border:1px solid #EEE7D3">
                   ${state.networks.map((net,i,a) => `
                     <div onclick="selectNetwork('${esc(net.ssid)}')"
                       style="display:grid;grid-template-columns:1fr 70px 50px 70px;align-items:center;padding:12px 20px;
                         border-bottom:${i<a.length-1?'1px solid #F7F2E7':'none'};cursor:pointer;
                         background:${state.selectedNetwork===net.ssid?'#F4F1E8':'transparent'}">
                       <div style="font-size:13px;font-weight:500">${esc(net.ssid)}</div>
                       <div style="font-size:11px;color:#565E78;font-family:'JetBrains Mono',monospace;text-transform:uppercase">${net.secured?'wpa2':'open'}</div>
                       <div>${signalBars(null, net.strength)}</div>
                       <div style="text-align:right;font-size:11px;font-weight:500;letter-spacing:0.06em;text-transform:uppercase;
                         border-bottom:1px solid ${state.selectedNetwork===net.ssid?'#C97A1A':'#0E1A33'};
                         color:${state.selectedNetwork===net.ssid?'#C97A1A':'#0E1A33'};
                         justify-self:end;cursor:pointer">Connect</div>
                     </div>
                   `).join('')}
                 </div>
                 ${state.selectedNetwork ? `
                   <div style="border:1px solid #EEE7D3;border-top:none;background:#FFFFFF;padding:16px 20px">
                     <label style="font-size:10px;font-weight:600;color:#9097A8;letter-spacing:0.12em;text-transform:uppercase;display:block;margin-bottom:8px;font-family:'JetBrains Mono',monospace">Password for "${esc(state.selectedNetwork)}"</label>
                     <div style="display:flex;gap:8px">
                       <input type="password" id="wifiPassword" placeholder="Wi-Fi password"
                         style="flex:1;padding:10px 12px;font-size:13px;border:1px solid #EEE7D3;background:#F7F2E7;font-family:inherit;outline:none">
                       <button onclick="connectWifi()"
                         style="background:#0E1A33;color:#F7F2E7;border:none;padding:10px 16px;cursor:pointer;font-size:11px;font-weight:600;letter-spacing:0.08em;text-transform:uppercase;font-family:inherit">
                         Connect
                       </button>
                     </div>
                   </div>
                 ` : ''}`
            }
          </section>

          <aside>
            <div style="font-size:10px;color:#9097A8;letter-spacing:0.18em;text-transform:uppercase;margin-bottom:10px;font-family:'JetBrains Mono',monospace">ethernet</div>
            <div style="padding:12px 0;border-bottom:1px solid #EEE7D3;display:flex;justify-content:space-between">
              <span style="font-size:13px">eth0</span>
              <span style="font-size:11px;font-weight:600;color:${status.eth_connected?'#1E6F4F':'#9097A8'};letter-spacing:0.06em;text-transform:uppercase">${status.eth_connected?'connected':'unplugged'}</span>
            </div>
            ${status.eth_ip ? `<div style="padding:10px 0;border-bottom:1px solid #EEE7D3;display:flex;justify-content:space-between;font-size:12px;font-family:'JetBrains Mono',monospace;color:#565E78"><span>ip</span><span>${esc(status.eth_ip)}</span></div>` : ''}
            <div style="font-size:10px;color:#9097A8;letter-spacing:0.18em;text-transform:uppercase;margin:24px 0 10px;font-family:'JetBrains Mono',monospace">tools</div>
            <button onclick="scanNetworks()" style="width:100%;background:transparent;border:1px solid #0E1A33;color:#0E1A33;padding:10px 14px;font-size:11px;font-weight:600;letter-spacing:0.08em;text-transform:uppercase;cursor:pointer;font-family:inherit;text-align:left">Scan for networks</button>
          </aside>
        </div>
      `;
    }

    function viewSystem() {
      const sys = state.systemStatus || {};
      const svcs = sys.services || [];
      const ver = state.versionInfo || {};
      const info = sys.info || {};
      return `
        <div style="display:grid;grid-template-columns:1fr 340px;gap:36px;align-items:start" class="cc-two-col">
          <section>
            <h1 style="font-family:'Instrument Serif',Georgia,serif;font-size:36px;font-weight:400;letter-spacing:-0.02em;margin:0 0 4px">System</h1>
            <p style="font-size:13px;color:#565E78;margin:0 0 24px">Hub health and maintenance actions.</p>

            ${Object.keys(info).length > 0 ? `
              <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:10px;margin-bottom:28px">
                ${Object.entries(info).map(([k,v]) => `
                  <div style="background:#FFFFFF;border:1px solid #EEE7D3;padding:14px 16px">
                    <div style="font-size:10px;color:#9097A8;letter-spacing:0.12em;text-transform:uppercase;margin-bottom:4px">${esc(k)}</div>
                    <div style="font-family:'JetBrains Mono',monospace;font-size:14px;color:#0E1A33">${esc(v)}</div>
                  </div>
                `).join('')}
              </div>
            ` : ''}

            <div style="font-size:10px;color:#9097A8;letter-spacing:0.18em;text-transform:uppercase;margin-bottom:10px;font-family:'JetBrains Mono',monospace">services</div>
            <div style="background:#FFFFFF;border:1px solid #EEE7D3;margin-bottom:28px">
              ${svcs.length === 0
                ? '<div style="padding:16px 20px;font-size:13px;color:#9097A8">Loading service status…</div>'
                : svcs.map((svc,i,a) => `
                    <div style="display:grid;grid-template-columns:1fr auto auto;align-items:center;gap:16px;padding:12px 20px;border-bottom:${i<a.length-1?'1px solid #F7F2E7':'none'}">
                      <div>
                        <div style="display:flex;align-items:center;gap:8px">
                          <span style="width:6px;height:6px;border-radius:50%;background:${svc.active?'#1E6F4F':'#C72717'};flex-shrink:0;display:inline-block"></span>
                          <span style="font-size:13px;font-family:'JetBrains Mono',monospace">${esc(svc.name)}</span>
                        </div>
                        <div style="font-size:11px;color:#565E78;margin-top:2px;padding-left:14px">${esc(svc.description)}</div>
                      </div>
                      <span style="font-size:11px;font-weight:600;color:${svc.active?'#1E6F4F':'#C72717'};letter-spacing:0.06em;text-transform:uppercase">${svc.active?'running':'down'}</span>
                      <button onclick="restartService('${esc(svc.unit)}')"
                        style="background:transparent;color:#0E1A33;border:1px solid #EEE7D3;padding:5px 10px;cursor:pointer;font-size:11px;letter-spacing:0.04em;font-family:inherit">
                        Restart
                      </button>
                    </div>
                  `).join('')
              }
            </div>

            <div style="font-size:10px;color:#9097A8;letter-spacing:0.18em;text-transform:uppercase;margin-bottom:10px;font-family:'JetBrains Mono',monospace">actions</div>
            <div style="display:flex;gap:8px;flex-wrap:wrap">
              <button onclick="restartService('all')" style="background:transparent;color:#0E1A33;border:1px solid #0E1A33;padding:10px 14px;cursor:pointer;font-size:11px;font-weight:600;letter-spacing:0.08em;text-transform:uppercase;font-family:inherit">Restart all services</button>
              <button onclick="if(confirm('Restart the Pi? Monitoring will pause for ~60 seconds.'))rebootPi()" style="background:transparent;color:#C72717;border:1px solid #C72717;padding:10px 14px;cursor:pointer;font-size:11px;font-weight:600;letter-spacing:0.08em;text-transform:uppercase;font-family:inherit">Restart Pi</button>
            </div>
          </section>

          <aside>
            <div style="font-size:10px;color:#9097A8;letter-spacing:0.18em;text-transform:uppercase;margin-bottom:10px;font-family:'JetBrains Mono',monospace">updates</div>
            <div style="background:#FFFFFF;border:1px solid #EEE7D3;padding:16px 18px;margin-bottom:24px">
              <div style="font-size:13px;font-weight:600;margin-bottom:4px">Firmware ${ver.current ? esc(ver.current.slice(0,7)) : '—'}</div>
              <div style="font-size:11px;color:#565E78;font-family:'JetBrains Mono',monospace;margin-bottom:12px">
                ${ver.up_to_date
                  ? '<span style="color:#1E6F4F;font-weight:600">up to date</span>'
                  : ver.latest
                    ? `latest: ${esc(ver.latest.slice(0,7))}`
                    : 'checking…'
                }
              </div>
              <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">
                <button onclick="checkForUpdates()"
                  style="background:transparent;border:1px solid #0E1A33;color:#0E1A33;padding:8px 12px;font-size:11px;font-weight:600;letter-spacing:0.08em;text-transform:uppercase;cursor:pointer;font-family:inherit"
                  ${state.updateInProgress ? 'disabled' : ''}>
                  Check for updates
                </button>
                ${!ver.up_to_date && ver.latest && !state.updateInProgress
                  ? `<button onclick="runUpdate()" style="background:#0E1A33;color:#F7F2E7;border:none;padding:8px 12px;font-size:11px;font-weight:600;letter-spacing:0.08em;text-transform:uppercase;cursor:pointer;font-family:inherit">Install</button>`
                  : ''
                }
                ${state.updateInProgress ? `<span style="font-size:11px;color:#565E78;font-family:'JetBrains Mono',monospace">Updating…</span>` : ''}
              </div>
            </div>

            <div style="font-size:10px;color:#9097A8;letter-spacing:0.18em;text-transform:uppercase;margin-bottom:10px;font-family:'JetBrains Mono',monospace">maintenance</div>
            <button onclick="restartService('all')" style="width:100%;background:transparent;border:1px solid #0E1A33;color:#0E1A33;padding:12px 14px;font-size:11px;font-weight:600;letter-spacing:0.08em;text-transform:uppercase;cursor:pointer;font-family:inherit;text-align:left;margin-bottom:6px">Restart all services</button>
            <button onclick="navigate('change_password')" style="width:100%;background:transparent;border:1px solid #EEE7D3;color:#0E1A33;padding:12px 14px;font-size:11px;font-weight:600;letter-spacing:0.08em;text-transform:uppercase;cursor:pointer;font-family:inherit;text-align:left;margin-bottom:6px">Change password</button>

            <div style="margin-top:18px;padding:16px;background:#0E1A33;color:#F7F2E7;border-top:3px solid #C72717">
              <div style="font-size:11px;font-weight:600;letter-spacing:0.08em;text-transform:uppercase;color:#E5B0A8;margin-bottom:6px">danger zone</div>
              <div style="font-size:12px;color:#9097A8;line-height:1.55;margin-bottom:12px">Restarting the Pi takes ~60 seconds. Sensors will queue readings while offline.</div>
              <button onclick="if(confirm('Restart the Pi? Monitoring will pause for ~60 seconds.'))rebootPi()"
                style="background:transparent;border:1px solid #C72717;color:#E5B0A8;padding:9px 12px;font-size:11px;font-weight:600;letter-spacing:0.08em;text-transform:uppercase;cursor:pointer;font-family:inherit;width:100%">
                Restart Pi
              </button>
            </div>
          </aside>
        </div>
      `;
    }

    // ── Auth views ───────────────────────────────────────────

    function viewLogin() {
      return `
        <div style="background:#FFFFFF;border:1px solid #EEE7D3;padding:36px 32px;width:380px;max-width:100%">
          <div style="font-family:'Instrument Serif',Georgia,serif;font-size:36px;line-height:0.95;letter-spacing:-0.02em;margin-bottom:4px">
            ChillCheck<span style="color:#C97A1A">.</span>
          </div>
          <div style="font-size:10px;color:#9097A8;letter-spacing:0.18em;text-transform:uppercase;margin-bottom:24px;font-family:'JetBrains Mono',monospace">local installer console · sign in</div>

          ${state.loginError ? `<div style="background:#FAE9E6;border:1px solid #C72717;color:#7A1D12;padding:10px 12px;font-size:12px;margin-bottom:14px">${esc(state.loginError)}</div>` : ''}

          <label style="display:block;font-size:10px;color:#565E78;letter-spacing:0.12em;text-transform:uppercase;margin-bottom:6px">Username</label>
          <input id="login-username" type="text" value="admin" readonly
            style="width:100%;border:1px solid #EEE7D3;background:#F7F2E7;color:#565E78;padding:10px 12px;font-family:'JetBrains Mono',monospace;font-size:13px;margin-bottom:16px">

          <label style="display:block;font-size:10px;color:#565E78;letter-spacing:0.12em;text-transform:uppercase;margin-bottom:6px">Password</label>
          <input id="login-password" type="password" placeholder="chillcheck"
            onkeydown="if(event.key==='Enter')doLogin()"
            style="width:100%;border:1px solid #EEE7D3;background:#FFF;color:#0E1A33;padding:10px 12px;font-family:'JetBrains Mono',monospace;font-size:13px;margin-bottom:20px">

          <button onclick="doLogin()"
            style="width:100%;background:#0E1A33;color:#F7F2E7;border:none;padding:12px;font-size:11px;font-weight:600;letter-spacing:0.08em;text-transform:uppercase;cursor:pointer;font-family:inherit">
            Sign in
          </button>

          <div style="font-size:11px;color:#9097A8;margin-top:18px;line-height:1.55">
            Default password is <span style="font-family:'JetBrains Mono',monospace;color:#0E1A33">chillcheck</span>. You'll be asked to change it on first login.
          </div>
        </div>
      `;
    }

    function viewChangePassword(forced) {
      const title = forced ? 'Set a new password' : 'Change password';
      const subtitle = forced
        ? 'The default password must be replaced before this console can be used.'
        : 'Update the password used to sign in to this hub.';
      return `
        <div style="background:#FFFFFF;border:1px solid #EEE7D3;padding:36px 32px;width:420px;max-width:100%">
          <div style="font-family:'Instrument Serif',Georgia,serif;font-size:32px;line-height:0.95;letter-spacing:-0.02em;margin-bottom:6px">${esc(title)}</div>
          <div style="font-size:12px;color:#565E78;margin-bottom:22px;line-height:1.55">${esc(subtitle)}</div>

          ${state.changePwError ? `<div style="background:#FAE9E6;border:1px solid #C72717;color:#7A1D12;padding:10px 12px;font-size:12px;margin-bottom:14px">${esc(state.changePwError)}</div>` : ''}

          <label style="display:block;font-size:10px;color:#565E78;letter-spacing:0.12em;text-transform:uppercase;margin-bottom:6px">Current password</label>
          <input id="cpw-current" type="password" autocomplete="current-password"
            style="width:100%;border:1px solid #EEE7D3;background:#FFF;color:#0E1A33;padding:10px 12px;font-family:'JetBrains Mono',monospace;font-size:13px;margin-bottom:14px">

          <label style="display:block;font-size:10px;color:#565E78;letter-spacing:0.12em;text-transform:uppercase;margin-bottom:6px">New password</label>
          <input id="cpw-new" type="password" autocomplete="new-password"
            style="width:100%;border:1px solid #EEE7D3;background:#FFF;color:#0E1A33;padding:10px 12px;font-family:'JetBrains Mono',monospace;font-size:13px;margin-bottom:14px">

          <label style="display:block;font-size:10px;color:#565E78;letter-spacing:0.12em;text-transform:uppercase;margin-bottom:6px">Confirm new password</label>
          <input id="cpw-confirm" type="password" autocomplete="new-password"
            onkeydown="if(event.key==='Enter')doChangePassword()"
            style="width:100%;border:1px solid #EEE7D3;background:#FFF;color:#0E1A33;padding:10px 12px;font-family:'JetBrains Mono',monospace;font-size:13px;margin-bottom:20px">

          <div style="display:flex;gap:8px">
            <button onclick="doChangePassword()" ${state.changePwBusy ? 'disabled' : ''}
              style="flex:1;background:#0E1A33;color:#F7F2E7;border:none;padding:12px;font-size:11px;font-weight:600;letter-spacing:0.08em;text-transform:uppercase;cursor:${state.changePwBusy?'wait':'pointer'};font-family:inherit;opacity:${state.changePwBusy?0.6:1}">
              ${state.changePwBusy ? 'Saving…' : 'Set password'}
            </button>
            ${!forced ? `
              <button onclick="navigate('system')"
                style="background:transparent;color:#0E1A33;border:1px solid #EEE7D3;padding:12px 16px;font-size:11px;font-weight:600;letter-spacing:0.08em;text-transform:uppercase;cursor:pointer;font-family:inherit">
                Cancel
              </button>` : ''}
          </div>

          <div style="font-size:11px;color:#9097A8;margin-top:18px;line-height:1.55">
            Minimum 8 characters. If you forget this password, the device must be reflashed — there is no recovery flow.
          </div>
        </div>
      `;
    }

    // ── Logs view ────────────────────────────────────────────

    function viewLogs() {
      const units = [
        { unit: 'chillcheck-subscriber', label: 'Subscriber' },
        { unit: 'zigbee2mqtt',           label: 'Zigbee2MQTT' },
        { unit: 'mosquitto',             label: 'Mosquitto' },
        { unit: 'chillcheck-local-ui',   label: 'Local UI' },
      ];
      const body = state.logsError
        ? `<div style="color:#C72717;padding:14px 16px">${esc(state.logsError)}</div>`
        : state.logsLoading && !state.logsContent
          ? `<div style="color:#9097A8;padding:14px 16px">Loading…</div>`
          : `<pre style="margin:0;padding:14px 16px;font-family:'JetBrains Mono',monospace;font-size:11.5px;line-height:1.5;color:#F7F2E7;white-space:pre-wrap;word-break:break-word">${esc(state.logsContent || '(no log output)')}</pre>`;
      return `
        <div>
          <h1 style="font-family:'Instrument Serif',Georgia,serif;font-size:36px;font-weight:400;letter-spacing:-0.02em;margin:0 0 4px">Logs</h1>
          <p style="font-size:13px;color:#565E78;margin:0 0 20px">Recent journal output for each ChillCheck service. Last 500 lines, oldest first.</p>

          <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:14px">
            <div style="display:flex;gap:6px;flex-wrap:wrap">
              ${units.map(u => `
                <button onclick="selectLogsUnit('${u.unit}')"
                  style="background:${state.logsUnit===u.unit?'#0E1A33':'transparent'};color:${state.logsUnit===u.unit?'#F7F2E7':'#0E1A33'};border:1px solid #0E1A33;padding:8px 12px;font-size:11px;font-weight:600;letter-spacing:0.06em;text-transform:uppercase;cursor:pointer;font-family:inherit">
                  ${esc(u.label)}
                </button>
              `).join('')}
            </div>
            <div style="margin-left:auto;display:flex;gap:8px;align-items:center">
              <label style="font-size:11px;color:#565E78;display:inline-flex;align-items:center;gap:6px;cursor:pointer">
                <input type="checkbox" ${state.logsAutoRefresh?'checked':''} onchange="toggleLogsAutoRefresh()"> Auto-refresh
              </label>
              <button onclick="loadLogs()" ${state.logsLoading?'disabled':''}
                style="background:transparent;border:1px solid #EEE7D3;color:#0E1A33;padding:8px 12px;font-size:11px;font-weight:600;letter-spacing:0.06em;text-transform:uppercase;cursor:${state.logsLoading?'wait':'pointer'};font-family:inherit;opacity:${state.logsLoading?0.6:1}">
                ${state.logsLoading ? 'Loading…' : 'Refresh'}
              </button>
            </div>
          </div>

          <div id="logs-pane" style="background:#0E1A33;border:1px solid #0E1A33;max-height:70vh;overflow:auto">
            ${body}
          </div>
        </div>
      `;
    }

    // ── UI Components ────────────────────────────────────────

    function signalBars(rssi, strengthPct) {
      let s = 0;
      if (rssi) s = rssi > -55 ? 3 : rssi > -70 ? 2 : 1;
      else if (strengthPct) s = strengthPct > 66 ? 3 : strengthPct > 33 ? 2 : 1;
      return `<div style="display:flex;align-items:flex-end;gap:2px;height:14px">
        ${[6,10,14].map((h,i) => `<div style="width:4px;height:${h}px;background:${i<s?'#1E6F4F':'#EEE7D3'}"></div>`).join('')}
      </div>`;
    }

    function batteryBadge(pct) {
      if (pct === null || pct === undefined) return `<span style="font-size:11px;color:#9097A8;font-family:'JetBrains Mono',monospace">—</span>`;
      const color = pct > 60 ? '#1E6F4F' : pct > 30 ? '#C97A1A' : '#C72717';
      return `<span style="font-size:11px;font-weight:600;color:${color};font-family:'JetBrains Mono',monospace">${pct}%</span>`;
    }

    // ── Actions ──────────────────────────────────────────────

    function navigate(view) {
      // Stop any auto-refresh tied to the previous view.
      if (state.logsTimer) { clearInterval(state.logsTimer); state.logsTimer = null; state.logsAutoRefresh = false; }
      state.view = view;
      render();
      if (view === 'sensors') loadSensors();
      if (view === 'network') loadNetwork();
      if (view === 'system')  loadSystem();
      if (view === 'logs')    loadLogs();
    }

    // ── Auth actions ─────────────────────────────────────────

    async function checkAuthStatus() {
      try {
        const s = await api('GET', '/api/auth/status');
        state.authed = !!s.authed;
        state.mustChangePassword = !!s.must_change;
      } catch (e) {
        state.authed = false;
      }
      state.authChecked = true;
      render();
    }

    async function doLogin() {
      const pw = document.getElementById('login-password').value;
      state.loginError = '';
      render();
      const res = await api('POST', '/api/auth/login', { username: 'admin', password: pw });
      if (res.ok) {
        state.authed = true;
        state.mustChangePassword = !!res.must_change;
        state.loginPassword = '';
        render();
        if (!state.mustChangePassword) {
          // Load whatever the active view needs.
          bootstrapAuthedViews();
        }
      } else {
        state.loginError = res.error || 'Sign in failed';
        render();
      }
    }

    async function doChangePassword() {
      const current = document.getElementById('cpw-current').value;
      const newPw   = document.getElementById('cpw-new').value;
      const confirm = document.getElementById('cpw-confirm').value;
      state.changePwError = '';
      state.changePwBusy = true;
      render();
      const res = await api('POST', '/api/auth/change-password', {
        current_password: current, new_password: newPw, confirm_password: confirm,
      });
      state.changePwBusy = false;
      if (res.ok) {
        state.mustChangePassword = false;
        state.changePwError = '';
        // After a voluntary change we were on the change_password view —
        // drop back to System. After a forced change, the layout will
        // re-render to the normal chrome.
        if (state.view === 'change_password') state.view = 'system';
        render();
        bootstrapAuthedViews();
      } else {
        state.changePwError = res.error || 'Could not save password';
        render();
      }
    }

    async function doLogout() {
      if (state.logsTimer) { clearInterval(state.logsTimer); state.logsTimer = null; }
      await api('POST', '/api/auth/logout');
      state.authed = false;
      state.mustChangePassword = false;
      state.loginError = '';
      state.view = 'sensors';
      render();
    }

    function bootstrapAuthedViews() {
      loadCloudInfo();
      if (state.view === 'sensors') loadSensors();
      if (state.view === 'network') loadNetwork();
      if (state.view === 'system')  loadSystem();
      if (state.view === 'logs')    loadLogs();
    }

    // ── Logs actions ─────────────────────────────────────────

    function selectLogsUnit(unit) {
      state.logsUnit = unit;
      state.logsContent = '';
      render();
      loadLogs();
    }

    async function loadLogs() {
      state.logsLoading = true;
      state.logsError = '';
      render();
      try {
        const res = await api('GET', '/api/logs/' + encodeURIComponent(state.logsUnit) + '?lines=500');
        if (res.error && !res.log) {
          state.logsError = res.error;
          state.logsContent = '';
        } else {
          state.logsContent = res.log || '';
          state.logsError = res.error || '';
        }
      } catch (e) {
        state.logsError = String(e);
      }
      state.logsLoading = false;
      render();
      // Scroll log pane to the latest line.
      const pane = document.getElementById('logs-pane');
      if (pane) pane.scrollTop = pane.scrollHeight;
    }

    function toggleLogsAutoRefresh() {
      state.logsAutoRefresh = !state.logsAutoRefresh;
      if (state.logsAutoRefresh) {
        state.logsTimer = setInterval(loadLogs, 5000);
      } else if (state.logsTimer) {
        clearInterval(state.logsTimer);
        state.logsTimer = null;
      }
      render();
    }

    async function loadSensors() {
      const data = await api('GET', '/api/sensors');
      state.sensors = data.sensors || [];
      render();
    }

    async function refreshSensors() { await loadSensors(); }

    async function loadNetwork() {
      const data = await api('GET', '/api/network/status');
      state.networkStatus = data;
      render();
    }

    async function loadSystem() {
      const [status, version] = await Promise.all([
        api('GET', '/api/system/status'),
        api('GET', '/api/system/version'),
      ]);
      state.systemStatus = status;
      state.versionInfo  = version;
      render();
    }

    async function checkForUpdates() {
      state.versionInfo = null; render();
      const data = await api('GET', '/api/system/version');
      state.versionInfo = data;
      render();
    }

    async function runUpdate() {
      if (!confirm('Install the latest update? Monitoring will pause for ~60 seconds.')) return;
      state.updateInProgress = true; render();
      try { await api('POST', '/api/system/update/run'); } catch (e) { /* expected: service restarts mid-request */ }
      // Poll until the version file changes, or give up after 3 minutes.
      const started = Date.now();
      const poll = setInterval(async () => {
        try {
          const v = await api('GET', '/api/system/version');
          if (v.up_to_date || Date.now() - started > 180000) {
            clearInterval(poll);
            window.location.reload();
          }
        } catch (e) { /* service may still be restarting */ }
      }, 8000);
    }

    async function startPairing() {
      await api('POST', '/api/sensors/pair', { enable: true });
      state.pairingActive = true;
      state.pairingStep   = 1;
      render();
    }

    async function advancePairing() {
      state.pairingStep++;
      if (state.pairingStep === 2) {
        // Poll for new sensor
        let attempts = 0;
        const poll = setInterval(async () => {
          const data = await api('GET', '/api/sensors');
          const newSensor = (data.sensors||[]).find(s => !s.cabinet_id && !state.sensors.find(x => x.id === s.id));
          if (newSensor || attempts > 12) {
            clearInterval(poll);
            state.pairingStep = 3;
            state.sensors = data.sensors || [];
            render();
          }
          attempts++;
        }, 5000);
      }
      render();
    }

    async function cancelPairing() {
      await api('POST', '/api/sensors/pair', { enable: false });
      state.pairingActive = false;
      state.pairingStep   = 0;
      render();
    }

    async function scanNetworks() {
      state.networks = [];
      render();
      const data = await api('GET', '/api/network/scan');
      state.networks = data.networks || [];
      render();
    }

    function selectNetwork(ssid) {
      state.selectedNetwork = ssid;
      render();
    }

    async function connectWifi() {
      const pwd = document.getElementById('wifiPassword')?.value;
      if (!pwd) return;
      await api('POST', '/api/network/connect', { ssid: state.selectedNetwork, password: pwd });
      state.selectedNetwork = null;
      await loadNetwork();
    }

    async function restartService(unit) {
      if (!confirm(`Restart ${unit}? Monitoring will pause briefly.`)) return;
      await api('POST', '/api/system/restart', { unit });
      setTimeout(loadSystem, 3000);
    }

    async function rebootPi() {
      await api('POST', '/api/system/restart', { unit: 'pi' });
    }

    async function submitPairingCode() {
      const code = document.getElementById('pairingCode')?.value?.trim();
      const site = document.getElementById('siteName')?.value?.trim();
      if (!code) return;
      const errEl = document.getElementById('pairError');
      errEl.style.display = 'none';
      const data = await api('POST', '/api/cloud/pair', { code, site_name: site });
      if (data.success) {
        state.cloudConnected = true;
        state.cloudInfo = data;
        render();
      } else {
        errEl.textContent = data.error || 'Invalid or expired code. Please try again.';
        errEl.style.display = 'block';
      }
    }

    async function disconnectCloud() {
      if (!confirm('Disconnect from cloud? This Pi will stop syncing readings.')) return;
      await api('POST', '/api/cloud/disconnect');
      state.cloudConnected = false;
      render();
    }

    // ── Init ─────────────────────────────────────────────────
    async function loadCloudInfo() {
      if (!state.cloudConnected) return;
      try {
        const data = await api('GET', '/api/cloud/status');
        state.cloudInfo = data;
        render();
      } catch (e) { /* offline-tolerant */ }
    }

    // Bootstrap: render the loader, then resolve auth status. Once authed,
    // load whatever the active view needs.
    render();
    (async () => {
      await checkAuthStatus();
      if (state.authed && !state.mustChangePassword) {
        bootstrapAuthedViews();
      }
    })();
  </script>
</body>
</html>
"""


# ════════════════════════════════════════════════════════════
# ROUTES — HTML
# ════════════════════════════════════════════════════════════

@app.route("/")
def index():
    view = "connect" if not is_cloud_connected() else "sensors"
    return render_template_string(
        HTML_TEMPLATE,
        initial_view=view,
        cloud_connected=is_cloud_connected(),
        vercel_url=VERCEL_URL,
    )

@app.route("/sensors")
def sensors_page():
    return render_template_string(HTML_TEMPLATE, initial_view="sensors", cloud_connected=is_cloud_connected(), vercel_url=VERCEL_URL)

@app.route("/network")
def network_page():
    return render_template_string(HTML_TEMPLATE, initial_view="network", cloud_connected=is_cloud_connected(), vercel_url=VERCEL_URL)

@app.route("/system")
def system_page():
    return render_template_string(HTML_TEMPLATE, initial_view="system", cloud_connected=is_cloud_connected(), vercel_url=VERCEL_URL)


# ════════════════════════════════════════════════════════════
# ROUTES — API
# ════════════════════════════════════════════════════════════

# ── Auth ──────────────────────────────────────────────────────

@app.route("/api/auth/status")
def api_auth_status():
    return jsonify({
        "authed":       bool(session.get("authed")),
        "must_change":  bool(session.get("must_change")),
        "username":     ui_auth.USERNAME,
    })


@app.route("/api/auth/login", methods=["POST"])
def api_auth_login():
    body = request.get_json(silent=True) or {}
    username = (body.get("username") or "").strip()
    password = body.get("password") or ""

    if username != ui_auth.USERNAME:
        return jsonify({"error": "Invalid credentials"}), 401
    if not ui_auth.verify_password(password, _auth_state):
        return jsonify({"error": "Invalid credentials"}), 401

    session.permanent = True
    session["authed"] = True
    session["must_change"] = bool(_auth_state.get("must_change"))
    return jsonify({"ok": True, "must_change": session["must_change"]})


@app.route("/api/auth/change-password", methods=["POST"])
def api_auth_change_password():
    body = request.get_json(silent=True) or {}
    current = body.get("current_password") or ""
    new_pw = body.get("new_password") or ""
    confirm = body.get("confirm_password") or ""

    if not ui_auth.verify_password(current, _auth_state):
        return jsonify({"error": "Current password is incorrect"}), 400
    if new_pw != confirm:
        return jsonify({"error": "New passwords do not match"}), 400
    err = ui_auth.validate_new_password(new_pw)
    if err:
        return jsonify({"error": err}), 400

    ui_auth.set_password(_auth_state, new_pw)
    session["must_change"] = False
    return jsonify({"ok": True})


@app.route("/api/auth/logout", methods=["POST"])
def api_auth_logout():
    session.clear()
    return jsonify({"ok": True})


# ── Sensors ───────────────────────────────────────────────────

@app.route("/api/sensors")
def api_sensors():
    """List paired sensors with their cloud assignment status.
    Cabinet creation/assignment lives in the cloud dashboard — we only join
    `cabinets(name)` here so the local UI can show whether a sensor has been
    bound to a cabinet yet, not to drive any action."""
    supabase = get_supabase()
    if not supabase:
        return jsonify({"sensors": [], "error": "Not connected to cloud"})

    try:
        now = datetime.now(timezone.utc)

        sensors_res = (
            supabase.table("sensors")
            .select("*, cabinets(name)")
            .eq("site_id", SITE_ID)
            .eq("active", True)
            .execute()
        )

        sensors = []
        for s in sensors_res.data:
            last_seen = s.get("last_seen")
            minutes_since = None
            last_seen_ago = "unknown"
            if last_seen:
                ls = datetime.fromisoformat(last_seen.replace("Z", "+00:00"))
                minutes_since = int((now - ls).total_seconds() / 60)
                if minutes_since < 2:
                    last_seen_ago = "just now"
                elif minutes_since < 60:
                    last_seen_ago = f"{minutes_since} mins ago"
                else:
                    last_seen_ago = f"{minutes_since // 60}h {minutes_since % 60}m ago"

            sensors.append({
                **s,
                "cabinet_name":      s.get("cabinets", {}).get("name") if s.get("cabinets") else None,
                "last_seen_ago":     last_seen_ago,
                "minutes_since_seen": minutes_since,
            })

        return jsonify({"sensors": sensors})

    except Exception as e:
        log.error(f"api_sensors error: {e}")
        return jsonify({"sensors": [], "error": str(e)})


@app.route("/api/sensors/pair", methods=["POST"])
def api_sensors_pair():
    """Enable or disable Zigbee pairing mode via Zigbee2MQTT MQTT."""
    global pairing_active, pairing_timer
    data   = request.json or {}
    enable = data.get("enable", False)

    try:
        import paho.mqtt.publish as publish
        # Z2M 2.x expects just {"time": <seconds>} — non-zero enables, 0 disables.
        # The legacy {"value": ..., "time": ...} payload returns "Invalid payload".
        payload = json.dumps({"time": pairing_timer or 254}) if enable else json.dumps({"time": 0})
        publish.single(
            "zigbee2mqtt/bridge/request/permit_join",
            payload=payload,
            hostname="127.0.0.1",
            port=1883,
        )
        pairing_active = enable
        log.info(f"Pairing mode {'enabled' if enable else 'disabled'}")
        return jsonify({"success": True, "pairing": enable})
    except Exception as e:
        log.error(f"Pairing toggle failed: {e}")
        return jsonify({"success": False, "error": str(e)})


# Cabinet creation and sensor-to-cabinet assignment intentionally have no
# endpoints here — that workflow lives in the cloud dashboard.


# ── Network ───────────────────────────────────────────────────

@app.route("/api/network/status")
def api_network_status():
    """Return current network interface status."""
    try:
        eth_ip   = _get_interface_ip("eth0")
        wifi_ip  = _get_interface_ip("wlan0")
        wifi_ssid= _get_wifi_ssid()
        gateway  = _get_gateway()

        return jsonify({
            "eth_connected":  bool(eth_ip),
            "eth_ip":         eth_ip,
            "wifi_connected": bool(wifi_ip),
            "wifi_ip":        wifi_ip,
            "wifi_ssid":      wifi_ssid,
            "gateway":        gateway,
            "hostname":       "chillcheck.local",
        })
    except Exception as e:
        return jsonify({"error": str(e)})


@app.route("/api/network/scan")
def api_network_scan():
    """Scan for available Wi-Fi networks."""
    try:
        result = subprocess.run(
            ["sudo", "iwlist", "wlan0", "scan"],
            capture_output=True, text=True, timeout=15
        )
        networks = _parse_iwlist(result.stdout)
        return jsonify({"networks": networks})
    except Exception as e:
        return jsonify({"networks": [], "error": str(e)})


@app.route("/api/network/connect", methods=["POST"])
def api_network_connect():
    """Connect to a Wi-Fi network by writing wpa_supplicant.conf."""
    data     = request.json or {}
    ssid     = data.get("ssid", "").strip()
    password = data.get("password", "").strip()

    if not ssid or not password:
        return jsonify({"success": False, "error": "ssid and password required"})

    try:
        wpa_conf = f"""
ctrl_interface=DIR=/var/run/wpa_supplicant GROUP=netdev
update_config=1
country=GB

network={{
    ssid="{ssid}"
    psk="{password}"
    key_mgmt=WPA-PSK
}}
""".strip()
        # Write config
        wpa_path = "/etc/wpa_supplicant/wpa_supplicant.conf"
        with open("/tmp/wpa_supplicant.conf", "w") as f:
            f.write(wpa_conf)
        subprocess.run(["sudo", "cp", "/tmp/wpa_supplicant.conf", wpa_path], check=True)
        subprocess.run(["sudo", "wpa_cli", "-i", "wlan0", "reconfigure"], capture_output=True)
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


# ── System ────────────────────────────────────────────────────

# ── Updates ───────────────────────────────────────────────────

VERSION_FILE  = "/etc/chillcheck/version"
LATEST_VERSION_URL = "https://raw.githubusercontent.com/Give-Us-A-Break/chillcheck-pi/main/VERSION"

@app.route("/api/system/version")
def api_system_version():
    """Compare the locally-installed version with the latest in the public mirror."""
    current = None
    try:
        with open(VERSION_FILE) as f:
            current = f.read().strip() or None
    except FileNotFoundError:
        pass

    latest = None
    try:
        import httpx
        r = httpx.get(LATEST_VERSION_URL, timeout=5)
        if r.status_code == 200:
            latest = r.text.strip() or None
    except Exception as e:
        log.debug(f"Latest-version lookup failed: {e}")

    up_to_date = bool(current and latest and current == latest)
    return jsonify({
        "current":     current,
        "latest":      latest,
        "up_to_date":  up_to_date,
    })


@app.route("/api/system/update/run", methods=["POST"])
def api_system_update_run():
    """Kick off the update script in the background. The script restarts both
    services, so this HTTP response often disconnects mid-flight — the client
    should treat a connection drop as "in progress" and poll /api/system/version
    after ~60 seconds."""
    try:
        subprocess.Popen(
            ["sudo", "-n", "/usr/local/bin/chillcheck-update"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            close_fds=True,
            start_new_session=True,
        )
        return jsonify({"success": True, "message": "Update started"})
    except Exception as e:
        log.error(f"Update kickoff failed: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/system/status")
def api_system_status():
    """Return service and system status."""
    services_config = [
        {"name": "Mosquitto",   "description": "MQTT broker",   "unit": "mosquitto"},
        {"name": "Zigbee2MQTT", "description": "Zigbee bridge", "unit": "zigbee2mqtt"},
        {"name": "Subscriber",  "description": "Supabase sync", "unit": "chillcheck-subscriber"},
        {"name": "Local UI",    "description": "This interface","unit": "chillcheck-local-ui"},
    ]

    services = []
    for svc in services_config:
        try:
            result = subprocess.run(
                ["systemctl", "is-active", svc["unit"]],
                capture_output=True, text=True
            )
            active = result.stdout.strip() == "active"
        except Exception:
            active = False
        services.append({**svc, "active": active})

    info = _get_system_info()
    return jsonify({"services": services, "info": info})


@app.route("/api/system/restart", methods=["POST"])
def api_system_restart():
    """Restart a systemd service or the Pi itself."""
    data = request.json or {}
    unit = data.get("unit", "")

    try:
        if unit == "pi":
            subprocess.Popen(["sudo", "shutdown", "-r", "now"])
            return jsonify({"success": True, "message": "Pi restarting…"})
        elif unit == "all":
            for u in ["zigbee2mqtt", "chillcheck-subscriber"]:
                subprocess.run(["sudo", "systemctl", "restart", u], check=True)
            return jsonify({"success": True})
        else:
            subprocess.run(["sudo", "systemctl", "restart", unit], check=True)
            return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


# ── Logs ──────────────────────────────────────────────────────

# Whitelist of journal units the local UI is allowed to read. Anything
# outside this set is rejected before journalctl is invoked — defence in
# depth on top of the NOPASSWD sudoers rule.
_LOG_UNITS = {
    "chillcheck-subscriber": "ChillCheck Subscriber",
    "zigbee2mqtt":           "Zigbee2MQTT",
    "mosquitto":             "Mosquitto",
    "chillcheck-local-ui":   "Local UI",
}


@app.route("/api/logs/units")
def api_logs_units():
    return jsonify({"units": [{"unit": k, "label": v} for k, v in _LOG_UNITS.items()]})


@app.route("/api/logs/<unit>")
def api_logs(unit):
    if unit not in _LOG_UNITS:
        return jsonify({"error": "unknown unit"}), 400
    try:
        lines = int(request.args.get("lines", 500))
    except ValueError:
        lines = 500
    lines = max(50, min(lines, 2000))
    try:
        result = subprocess.run(
            ["sudo", "-n", "/usr/bin/journalctl",
             "-u", unit, "-n", str(lines), "--no-pager", "--output=short-iso"],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode != 0:
            return jsonify({
                "unit":    unit,
                "log":     result.stdout,
                "error":   result.stderr.strip() or f"journalctl exit {result.returncode}",
            })
        return jsonify({"unit": unit, "log": result.stdout, "lines": lines})
    except subprocess.TimeoutExpired:
        return jsonify({"error": "journalctl timed out"}), 504
    except Exception as e:
        log.error(f"Logs fetch for {unit} failed: {e}")
        return jsonify({"error": str(e)}), 500


# ── Cloud pairing ─────────────────────────────────────────────

@app.route("/api/cloud/status")
def api_cloud_status():
    connected = is_cloud_connected()
    org_name  = None
    site_name = None
    if connected:
        supabase = get_supabase()
        if supabase:
            try:
                org_res = supabase.table("organisations").select("name").eq("id", ORGANISATION_ID).single().execute()
                org_name = (org_res.data or {}).get("name")
            except Exception as e:
                log.debug(f"Org lookup failed: {e}")
            try:
                site_res = supabase.table("sites").select("name").eq("id", SITE_ID).single().execute()
                site_name = (site_res.data or {}).get("name")
            except Exception as e:
                log.debug(f"Site lookup failed: {e}")
    return jsonify({
        "connected":       connected,
        "organisation_id": ORGANISATION_ID,
        "site_id":         SITE_ID,
        "device_id":       DEVICE_ID,
        "org_name":        org_name,
        "site_name":       site_name,
    })


@app.route("/api/cloud/pair", methods=["POST"])
def api_cloud_pair():
    """
    Exchange a pairing code for Supabase credentials.
    Calls the Vercel API endpoint which validates the code
    and returns org/site/device IDs.
    """
    data      = request.json or {}
    code      = data.get("code", "").strip().upper()
    site_name = data.get("site_name", "ChillCheck Hub").strip()

    if not code:
        return jsonify({"success": False, "error": "Pairing code required"})

    try:
        import httpx
        # Call Vercel API to validate code
        response = httpx.post(
            f"{VERCEL_URL}/api/pairing/redeem",
            json={
                "code":      code,
                "site_name": site_name,
                "device_ip": _get_interface_ip("eth0") or _get_interface_ip("wlan0"),
            },
            timeout=15,
        )

        if response.status_code != 200:
            return jsonify({"success": False, "error": "Invalid or expired pairing code"})

        result = response.json()

        # Write credentials to .env
        _update_env({
            "SUPABASE_URL":         result["supabase_url"],
            "SUPABASE_SERVICE_KEY": result["supabase_service_key"],
            "ORGANISATION_ID":      result["organisation_id"],
            "SITE_ID":              result["site_id"],
            "DEVICE_ID":            result["device_id"],
            # NOTIFY_SECRET lets the Pi call /api/notify.
            # No third-party API keys are ever stored on the Pi.
            "NOTIFY_SECRET":        result.get("notify_secret", ""),
        })

        # Reload env vars in process
        global SUPABASE_URL, SUPABASE_SERVICE_KEY, ORGANISATION_ID, SITE_ID, DEVICE_ID, NOTIFY_SECRET
        SUPABASE_URL         = result["supabase_url"]
        SUPABASE_SERVICE_KEY = result["supabase_service_key"]
        ORGANISATION_ID      = result["organisation_id"]
        SITE_ID              = result["site_id"]
        DEVICE_ID            = result["device_id"]
        NOTIFY_SECRET        = result.get("notify_secret", NOTIFY_SECRET)

        # Restart subscriber to pick up new credentials
        subprocess.Popen(["sudo", "systemctl", "restart", "chillcheck-subscriber"])

        return jsonify({
            "success":   True,
            "org_name":  result.get("org_name"),
            "site_name": result.get("site_name"),
        })

    except Exception as e:
        log.error(f"Cloud pairing failed: {e}")
        return jsonify({"success": False, "error": "Could not reach ChillCheck cloud. Check internet connection."})


@app.route("/api/cloud/disconnect", methods=["POST"])
def api_cloud_disconnect():
    """Remove cloud credentials from .env."""
    _update_env({
        "ORGANISATION_ID": "",
        "SITE_ID":         "",
        "DEVICE_ID":       "",
    })
    global ORGANISATION_ID, SITE_ID, DEVICE_ID
    ORGANISATION_ID = SITE_ID = DEVICE_ID = ""
    subprocess.Popen(["sudo", "systemctl", "stop", "chillcheck-subscriber"])
    return jsonify({"success": True})


# ════════════════════════════════════════════════════════════
# SYSTEM HELPERS
# ════════════════════════════════════════════════════════════

def _get_interface_ip(iface: str) -> str:
    """Get IP address for a network interface."""
    try:
        result = subprocess.run(
            ["ip", "-4", "addr", "show", iface],
            capture_output=True, text=True
        )
        for line in result.stdout.splitlines():
            if "inet " in line:
                return line.strip().split()[1].split("/")[0]
    except Exception:
        pass
    return ""


def _get_gateway() -> str:
    """Get default gateway IP."""
    try:
        result = subprocess.run(["ip", "route", "show", "default"], capture_output=True, text=True)
        parts = result.stdout.split()
        if "via" in parts:
            return parts[parts.index("via") + 1]
    except Exception:
        pass
    return ""


def _get_wifi_ssid() -> str:
    """Get currently connected Wi-Fi SSID."""
    try:
        result = subprocess.run(
            ["iwgetid", "wlan0", "--raw"],
            capture_output=True, text=True
        )
        return result.stdout.strip()
    except Exception:
        return ""


def _parse_iwlist(output: str) -> list:
    """Parse iwlist scan output into a list of networks."""
    networks = []
    current  = {}
    for line in output.splitlines():
        line = line.strip()
        if "ESSID:" in line:
            ssid = line.split('ESSID:"')[1].rstrip('"') if 'ESSID:"' in line else ""
            current["ssid"] = ssid
        elif "Quality=" in line:
            try:
                q = line.split("Quality=")[1].split(" ")[0]
                num, den = q.split("/")
                current["strength"] = int(int(num) / int(den) * 100)
            except Exception:
                current["strength"] = 0
        elif "Encryption key:on" in line:
            current["secured"] = True
        elif "Encryption key:off" in line:
            current["secured"] = False
        elif line.startswith("Cell ") and current.get("ssid"):
            networks.append(current)
            current = {}
    if current.get("ssid"):
        networks.append(current)

    # Deduplicate and sort by signal strength
    seen = set()
    unique = []
    for n in networks:
        if n["ssid"] and n["ssid"] not in seen:
            seen.add(n["ssid"])
            unique.append(n)
    return sorted(unique, key=lambda x: x.get("strength", 0), reverse=True)


def _get_system_info() -> dict:
    """Collect system stats."""
    info = {}
    try:
        info["Hostname"] = "chillcheck.local"
        info["IP (eth0)"] = _get_interface_ip("eth0") or "—"
        info["IP (wlan0)"] = _get_interface_ip("wlan0") or "—"

        # Uptime
        with open("/proc/uptime") as f:
            secs = float(f.read().split()[0])
            days = int(secs // 86400)
            hours = int((secs % 86400) // 3600)
            info["Uptime"] = f"{days}d {hours}h"

        # CPU temp
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            temp = int(f.read()) / 1000
            info["CPU Temp"] = f"{temp:.1f}°C"

        # Disk
        result = subprocess.run(["df", "-h", "/"], capture_output=True, text=True)
        lines = result.stdout.splitlines()
        if len(lines) > 1:
            parts = lines[1].split()
            info["Disk Usage"] = f"{parts[2]} / {parts[1]} ({parts[4]})"

        # Memory
        result = subprocess.run(["free", "-m"], capture_output=True, text=True)
        lines = result.stdout.splitlines()
        if len(lines) > 1:
            parts = lines[1].split()
            info["Memory"] = f"{parts[2]} MB / {parts[1]} MB used"

        # ZBDongle
        dongle = "/dev/ttyUSB0" if os.path.exists("/dev/ttyUSB0") else "/dev/ttyACM0" if os.path.exists("/dev/ttyACM0") else "Not detected"
        info["ZBDongle-E"] = dongle

        info["OS"] = "Raspberry Pi OS Lite (64-bit)"

    except Exception as e:
        info["Error"] = str(e)

    return info


def _update_env(updates: dict):
    """Update key=value pairs in /etc/chillcheck/.env."""
    env_path = "/etc/chillcheck/.env"
    try:
        with open(env_path) as f:
            lines = f.readlines()

        updated_keys = set()
        new_lines    = []
        for line in lines:
            key = line.split("=")[0].strip()
            if key in updates:
                new_lines.append(f"{key}={updates[key]}\n")
                updated_keys.add(key)
            else:
                new_lines.append(line)

        # Add any keys not already in the file
        for key, val in updates.items():
            if key not in updated_keys:
                new_lines.append(f"{key}={val}\n")

        with open(env_path, "w") as f:
            f.writelines(new_lines)

    except Exception as e:
        log.error(f"Failed to update .env: {e}")


# ════════════════════════════════════════════════════════════
# MAIN
# ════════════════════════════════════════════════════════════

if __name__ == "__main__":
    log.info(f"ChillCheck Local UI starting on port {PORT}")
    log.info(f"Cloud connected: {is_cloud_connected()}")
    log.info("Access at: http://chillcheck.local")
    app.run(host="0.0.0.0", port=PORT, debug=False)
