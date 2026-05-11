"""
ChillCheck — Local UI
======================
Flask app served at http://chillcheck.local (port 80).
Accessible from any device on the same network as the Pi.

Routes:
  GET  /                    → Dashboard (redirect to /sensors)
  GET  /sensors             → Sensor management page
  GET  /network             → Network / Wi-Fi config page
  GET  /system              → System status page

API routes (called by the frontend JS):
  GET  /api/sensors         → List all sensors with status
  POST /api/sensors/pair    → Enable Zigbee pairing mode
  POST /api/sensors/assign  → Assign sensor to cabinet
  POST /api/sensors/unassign→ Unassign sensor from cabinet
  GET  /api/cabinets        → List cabinets from Supabase
  POST /api/cabinets        → Create a new cabinet
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
from datetime import datetime, timezone
from functools import wraps

from flask import Flask, jsonify, request, render_template_string, redirect
from flask_cors import CORS
from dotenv import load_dotenv

load_dotenv("/etc/chillcheck/.env")

# ── Config ────────────────────────────────────────────────────
SUPABASE_URL        = os.getenv("SUPABASE_URL", "")
SUPABASE_SERVICE_KEY= os.getenv("SUPABASE_SERVICE_KEY", "")
ORGANISATION_ID     = os.getenv("ORGANISATION_ID", "")
SITE_ID             = os.getenv("SITE_ID", "")
DEVICE_ID           = os.getenv("DEVICE_ID", "")
LOCAL_UI_SECRET     = os.getenv("LOCAL_UI_SECRET", "chillcheck")
PORT                = int(os.getenv("LOCAL_UI_PORT", 80))
VERCEL_URL          = os.getenv("VERCEL_URL", "https://app.chillcheck.online")

# ── Logging ───────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("chillcheck.local_ui")

# ── Flask app ─────────────────────────────────────────────────
app = Flask(__name__)
CORS(app)

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
  <title>ChillCheck Local Setup</title>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
      background: #f8fafc;
      color: #0f172a;
      min-height: 100vh;
    }
    #app {
      display: flex;
      align-items: center;
      justify-content: center;
      min-height: 100vh;
    }
    .loading {
      text-align: center;
      color: #64748b;
    }
    .loading h2 { font-size: 18px; margin-bottom: 8px; }
    .loading p  { font-size: 14px; }
    .spinner {
      width: 32px; height: 32px;
      border: 3px solid #e2e8f0;
      border-top-color: #0ea5e9;
      border-radius: 50%;
      animation: spin 0.8s linear infinite;
      margin: 0 auto 16px;
    }
    @keyframes spin { to { transform: rotate(360deg); } }

    /* ── Responsive shell ─────────────────────────────────── */
    .app-shell  { min-height: 100vh; display: flex; flex-direction: column; width: 100%; }
    .app-header { background: #fff; border-bottom: 1px solid #e2e8f0; height: 56px;
                  display: flex; align-items: center; justify-content: space-between;
                  position: sticky; top: 0; z-index: 50; padding: 0 16px; gap: 12px; }
    .app-header-status { display: flex; align-items: center; gap: 12px; flex-shrink: 0; }
    .app-header-status-label { display: none; }
    .app-body   { display: flex; flex: 1; min-height: 0; }
    .app-sidebar { background: #fff; padding: 14px 10px; flex-shrink: 0; }
    .app-main    { flex: 1; padding: 20px 16px; overflow-y: auto; max-width: 100%; }
    .app-nav-btn { display: flex; align-items: center; gap: 9px; border: none;
                   cursor: pointer; padding: 9px 11px; border-radius: 8px;
                   font-size: 13px; font-family: inherit; position: relative;
                   white-space: nowrap; }

    /* Mobile (default): sidebar collapses to a horizontal tab strip below header */
    .app-sidebar { display: flex; flex-direction: row; gap: 4px;
                   overflow-x: auto; border-bottom: 1px solid #e2e8f0;
                   padding: 8px 12px; }
    .app-sidebar .app-nav-btn { flex-shrink: 0; }

    /* Tablet + desktop: real sidebar */
    @media (min-width: 768px) {
      .app-header  { padding: 0 24px; }
      .app-header-status-label { display: inline; }
      .app-sidebar { display: flex; flex-direction: column; width: 200px;
                     border-right: 1px solid #e2e8f0; border-bottom: none;
                     overflow-x: visible; padding: 14px 10px; }
      .app-sidebar .app-nav-btn { width: 100%; text-align: left; margin-bottom: 2px; }
      .app-main    { padding: 28px 32px; max-width: 900px; }
    }
  </style>
</head>
<body>
  <div id="app">
    <div class="loading">
      <div class="spinner"></div>
      <h2>ChillCheck Local</h2>
      <p>Loading setup interface…</p>
    </div>
  </div>

  <script>
    // API base — same origin
    const API = '';

    // ── State ───────────────────────────────────────────────
    let state = {
      view: '{{ initial_view }}',
      cloudConnected: {{ 'true' if cloud_connected else 'false' }},
      sensors: [],
      cabinets: [],
      networkStatus: null,
      networks: [],
      systemStatus: null,
      versionInfo: null,
      updateInProgress: false,
      pairingActive: false,
      pairingStep: 0,
      assigningId: null,
    };

    // ── Helpers ─────────────────────────────────────────────
    const $ = id => document.getElementById(id);
    const esc = s => String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');

    async function api(method, path, body) {
      const opts = { method, headers: { 'Content-Type': 'application/json' } };
      if (body) opts.body = JSON.stringify(body);
      const res = await fetch(API + path, opts);
      return res.json();
    }

    // ── Render ──────────────────────────────────────────────
    function render() {
      const app = document.getElementById('app');
      app.innerHTML = layout();
    }

    function layout() {
      return `
        <div class="app-shell">
          ${header()}
          <div class="app-body">
            ${sidebar()}
            <main class="app-main">
              ${mainContent()}
            </main>
          </div>
        </div>
      `;
    }

    function header() {
      return `
        <header class="app-header">
          <div style="display:flex;align-items:center;gap:10px;min-width:0">
            <div style="width:30px;height:30px;border-radius:8px;background:linear-gradient(135deg,#0ea5e9,#0284c7);display:flex;align-items:center;justify-content:center;font-size:14px;flex-shrink:0">❄</div>
            <div style="min-width:0">
              <div style="font-size:14px;font-weight:700;letter-spacing:-0.02em">ChillCheck <span style="font-weight:400;color:#94a3b8">Local</span></div>
              <div style="font-size:10px;color:#94a3b8;font-weight:500">chillcheck.local</div>
            </div>
          </div>
          <div class="app-header-status">
            ${state.cloudConnected
              ? `<div style="display:flex;align-items:center;gap:6px">
                   <div style="width:7px;height:7px;border-radius:50%;background:#10b981;box-shadow:0 0 0 2px #d1fae5"></div>
                   <span class="app-header-status-label" style="font-size:12px;color:#0a7c4e;font-weight:600">Cloud connected</span>
                 </div>
                 <a href="${'{{ vercel_url }}'}" target="_blank" style="font-size:12px;color:#0ea5e9;font-weight:600;text-decoration:none;white-space:nowrap">Open ↗</a>`
              : `<div style="display:flex;align-items:center;gap:6px">
                   <div style="width:7px;height:7px;border-radius:50%;background:#f59e0b"></div>
                   <span class="app-header-status-label" style="font-size:12px;color:#92400e;font-weight:600">Not connected</span>
                 </div>`
            }
          </div>
        </header>
      `;
    }

    function sidebar() {
      const items = [
        { id: 'connect', label: 'Cloud Link',  icon: '🔗', dot: !state.cloudConnected },
        { id: 'sensors', label: 'Sensors',     icon: '📡', dot: false },
        { id: 'network', label: 'Network',     icon: '🌐', dot: false },
        { id: 'system',  label: 'System',      icon: '⚙️', dot: false },
      ];
      return `
        <aside class="app-sidebar">
          ${items.map(item => `
            <button onclick="navigate('${item.id}')" class="app-nav-btn"
              style="background:${state.view===item.id?'#f1f5f9':'transparent'};
                color:${state.view===item.id?'#0f172a':'#64748b'};
                font-weight:${state.view===item.id?600:400}">
              <span style="font-size:14px">${item.icon}</span>
              ${item.label}
              ${item.dot ? '<span style="width:7px;height:7px;border-radius:50%;background:#f59e0b;margin-left:4px"></span>' : ''}
            </button>
          `).join('')}
        </aside>
      `;
    }

    function mainContent() {
      switch(state.view) {
        case 'connect': return viewConnect();
        case 'sensors': return viewSensors();
        case 'network': return viewNetwork();
        case 'system':  return viewSystem();
        default:        return viewSensors();
      }
    }

    // ── Views ────────────────────────────────────────────────

    function viewConnect() {
      if (state.cloudConnected) {
        return `
          <div style="margin-bottom:24px">
            <h1 style="font-size:20px;font-weight:700;margin:0 0 4px;letter-spacing:-0.02em">Cloud Link</h1>
            <p style="font-size:13px;color:#64748b;margin:0">This Pi is connected to ChillCheck Cloud</p>
          </div>
          <div style="background:#fff;border:1px solid #e2e8f0;border-radius:12px;padding:20px 24px;max-width:420px">
            <div style="display:flex;align-items:center;gap:10px;margin-bottom:16px">
              <div style="width:10px;height:10px;border-radius:50%;background:#10b981;box-shadow:0 0 0 3px #d1fae5"></div>
              <div style="font-size:15px;font-weight:600">Connected to ChillCheck Cloud</div>
            </div>
            <div style="background:#f8fafc;border-radius:8px;padding:12px 14px;margin-bottom:20px;font-size:12px;line-height:1.8">
              <div style="display:flex;justify-content:space-between"><span style="color:#64748b">Organisation</span><span style="font-weight:600">${esc(state.cloudInfo?.org_name||'—')}</span></div>
              <div style="display:flex;justify-content:space-between"><span style="color:#64748b">Site</span><span style="font-weight:600">${esc(state.cloudInfo?.site_name||'—')}</span></div>
              <div style="display:flex;justify-content:space-between"><span style="color:#64748b">Last sync</span><span style="font-weight:600">Just now</span></div>
            </div>
            <button onclick="disconnectCloud()"
              style="background:#fff5f5;color:#dc2626;border:1px solid #fecaca;padding:9px 16px;border-radius:8px;cursor:pointer;font-size:13px;font-weight:600;font-family:inherit;width:100%">
              Disconnect from Cloud
            </button>
          </div>
        `;
      }

      return `
        <div style="margin-bottom:24px">
          <h1 style="font-size:20px;font-weight:700;margin:0 0 4px;letter-spacing:-0.02em">Cloud Link</h1>
          <p style="font-size:13px;color:#64748b;margin:0">Connect this Pi to your ChillCheck cloud account</p>
        </div>

        <div style="background:#eff6ff;border:1px solid #bfdbfe;border-radius:12px;padding:16px 18px;margin-bottom:20px;max-width:480px">
          <div style="font-size:13px;font-weight:600;color:#1d4ed8;margin-bottom:10px">How to link this device</div>
          ${[
            'Log into your ChillCheck dashboard at ' + '{{ vercel_url }}',
            'Go to Settings → Devices → Generate Pairing Code',
            'Enter the 8-character code below',
          ].map((s,i) => `
            <div style="display:flex;gap:12px;align-items:flex-start;margin-bottom:8px">
              <div style="width:22px;height:22px;border-radius:50%;background:#1d4ed8;color:#fff;font-size:11px;font-weight:700;display:flex;align-items:center;justify-content:center;flex-shrink:0">${i+1}</div>
              <div style="font-size:13px;color:#1e40af;line-height:1.5">${s}</div>
            </div>
          `).join('')}
        </div>

        <div style="background:#fff;border:1px solid #e2e8f0;border-radius:12px;padding:20px 24px;max-width:420px">
          <div style="font-size:14px;font-weight:600;margin-bottom:16px">Enter Pairing Code</div>

          <label style="font-size:11px;font-weight:600;color:#94a3b8;letter-spacing:0.06em;text-transform:uppercase;display:block;margin-bottom:6px">Site name (optional)</label>
          <input id="siteName" placeholder="e.g. Pup Planet Wolverhampton"
            style="width:100%;padding:10px 12px;font-size:14px;border:1px solid #e2e8f0;border-radius:8px;background:#f8fafc;font-family:inherit;outline:none;margin-bottom:16px">

          <label style="font-size:11px;font-weight:600;color:#94a3b8;letter-spacing:0.06em;text-transform:uppercase;display:block;margin-bottom:6px">Pairing code</label>
          <input id="pairingCode" placeholder="WOLF-4821" maxlength="9"
            oninput="this.value=this.value.toUpperCase().replace(/[^A-Z0-9-]/g,'')"
            style="width:100%;padding:10px 12px;font-size:22px;font-family:monospace;letter-spacing:0.15em;text-align:center;border:1px solid #e2e8f0;border-radius:8px;background:#f8fafc;outline:none;margin-bottom:8px">
          <div style="font-size:11px;color:#94a3b8;margin-bottom:20px">Codes expire after 10 minutes</div>

          <div id="pairError" style="display:none;background:#fff5f5;border:1px solid #fecaca;border-radius:8px;padding:10px 14px;margin-bottom:16px;font-size:13px;color:#dc2626"></div>

          <button onclick="submitPairingCode()"
            style="background:#0f172a;color:#fff;border:none;padding:10px 16px;border-radius:8px;cursor:pointer;font-size:14px;font-weight:600;font-family:inherit;width:100%">
            Connect to Cloud →
          </button>
        </div>
      `;
    }

    function viewSensors() {
      const unassigned = state.sensors.filter(s => !s.cabinet_id);
      const assigned   = state.sensors.filter(s =>  s.cabinet_id);

      return `
        <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:24px">
          <div>
            <h1 style="font-size:20px;font-weight:700;margin:0 0 4px;letter-spacing:-0.02em">Sensors</h1>
            <p style="font-size:13px;color:#64748b;margin:0">${state.sensors.length} paired · ${unassigned.length} unassigned</p>
          </div>
          <div style="display:flex;gap:8px">
            <button onclick="refreshSensors()" style="background:#f8fafc;color:#374151;border:1px solid #e2e8f0;padding:7px 14px;border-radius:8px;cursor:pointer;font-size:13px;font-weight:500;font-family:inherit">↺ Refresh</button>
            <button onclick="startPairing()" style="background:#0f172a;color:#fff;border:none;padding:9px 16px;border-radius:8px;cursor:pointer;font-size:13px;font-weight:600;font-family:inherit">+ Pair Sensor</button>
          </div>
        </div>

        ${state.pairingActive ? pairingModal() : ''}
        ${state.assigningId   ? assignModal()  : ''}

        ${unassigned.length > 0 ? `
          <div style="font-size:12px;font-weight:600;color:#94a3b8;letter-spacing:0.06em;text-transform:uppercase;margin-bottom:10px">Unassigned (${unassigned.length})</div>
          ${unassigned.map(s => unassignedCard(s)).join('')}
          <div style="margin-bottom:20px"></div>
        ` : ''}

        <div style="font-size:12px;font-weight:600;color:#94a3b8;letter-spacing:0.06em;text-transform:uppercase;margin-bottom:10px">Assigned (${assigned.length})</div>
        ${assigned.length === 0
          ? '<p style="font-size:13px;color:#94a3b8">No sensors assigned yet. Pair a sensor and assign it to a cabinet.</p>'
          : `<div style="background:#fff;border:1px solid #e2e8f0;border-radius:12px;overflow:hidden">
               <table style="width:100%;border-collapse:collapse">
                 <thead>
                   <tr style="border-bottom:1px solid #f1f5f9">
                     ${['Cabinet','Sensor ID','Signal','Battery','Last Seen',''].map(h =>
                       `<th style="padding:10px 14px;font-size:11px;font-weight:600;color:#94a3b8;letter-spacing:0.06em;text-transform:uppercase;text-align:left">${h}</th>`
                     ).join('')}
                   </tr>
                 </thead>
                 <tbody>
                   ${assigned.map((s,i) => sensorRow(s, i, assigned.length)).join('')}
                 </tbody>
               </table>
             </div>`
        }
      `;
    }

    function unassignedCard(s) {
      return `
        <div style="background:#fffbeb;border:1px solid #fde68a;border-radius:12px;padding:16px 18px;display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
          <div style="display:flex;gap:14px;align-items:center">
            <div style="width:36px;height:36px;border-radius:8px;background:#fef3c7;display:flex;align-items:center;justify-content:center;font-size:18px">📡</div>
            <div>
              <div style="font-size:13px;font-weight:600;margin-bottom:2px">New Sensor</div>
              <div style="font-size:11px;font-family:monospace;color:#94a3b8">···${esc(s.zigbee_id?.slice(-4)||'????')} · ${esc(s.last_seen_ago||'unknown')}</div>
              <div style="display:flex;gap:10px;margin-top:4px;align-items:center">
                ${signalBars(s.rssi)} ${batteryBadge(s.battery_pct)}
              </div>
            </div>
          </div>
          <button onclick="openAssign('${s.id}')"
            style="background:#0f172a;color:#fff;border:none;padding:8px 14px;border-radius:8px;cursor:pointer;font-size:13px;font-weight:600;font-family:inherit">
            Assign →
          </button>
        </div>
      `;
    }

    function sensorRow(s, i, total) {
      const offline = s.minutes_since_seen > 30;
      return `
        <tr style="border-bottom:${i<total-1?'1px solid #f8fafc':'none'};background:${offline?'#fff5f5':'transparent'}">
          <td style="padding:11px 14px;font-size:13px;font-weight:600">${esc(s.cabinet_name||'Unknown')}</td>
          <td style="padding:11px 14px;font-size:11px;font-family:monospace;color:#94a3b8">···${esc(s.zigbee_id?.slice(-4)||'????')}</td>
          <td style="padding:11px 14px">${signalBars(s.rssi)}</td>
          <td style="padding:11px 14px">${batteryBadge(s.battery_pct)}</td>
          <td style="padding:11px 14px;font-size:12px;color:${offline?'#dc2626':'#64748b'}">${esc(s.last_seen_ago||'unknown')}</td>
          <td style="padding:11px 14px">
            <button onclick="unassignSensor('${s.id}')"
              style="background:#fff5f5;color:#dc2626;border:1px solid #fecaca;padding:5px 10px;border-radius:6px;cursor:pointer;font-size:12px;font-family:inherit">
              Unassign
            </button>
          </td>
        </tr>
      `;
    }

    function pairingModal() {
      const steps = [
        { title: 'Put sensor into pairing mode', desc: 'Hold the reset button on the SNZB-02LD for 5 seconds until the LED flashes rapidly.' },
        { title: 'Waiting for sensor…',          desc: 'ChillCheck is scanning for a new Zigbee device. This usually takes 10–30 seconds.' },
        { title: 'Sensor found!',                desc: 'New sensor detected. You can now assign it to a cabinet.' },
      ];
      return `
        <div style="position:fixed;inset:0;background:rgba(0,0,0,0.4);z-index:100;display:flex;align-items:center;justify-content:center">
          <div style="background:#fff;border-radius:12px;padding:24px;width:440px;max-width:90vw;box-shadow:0 20px 60px rgba(0,0,0,0.15)">
            <div style="font-size:16px;font-weight:700;margin-bottom:4px">Pair New Sensor</div>
            <div style="font-size:13px;color:#64748b;margin-bottom:20px">Follow the steps below to add a new SNZB-02LD</div>
            ${steps.map((step,i) => `
              <div style="display:flex;gap:14px;margin-bottom:16px;opacity:${state.pairingStep>=i+1?1:0.35}">
                <div style="width:28px;height:28px;border-radius:50%;flex-shrink:0;
                  background:${state.pairingStep>i+1?'#10b981':state.pairingStep===i+1?'#0f172a':'#e2e8f0'};
                  color:${state.pairingStep>=i+1?'#fff':'#94a3b8'};
                  display:flex;align-items:center;justify-content:center;font-size:12px;font-weight:700">
                  ${state.pairingStep>i+1?'✓':i+1}
                </div>
                <div>
                  <div style="font-size:13px;font-weight:600;margin-bottom:2px">${step.title}</div>
                  <div style="font-size:12px;color:#64748b;line-height:1.5">${step.desc}</div>
                </div>
              </div>
            `).join('')}
            ${state.pairingStep===2?`<div style="display:flex;align-items:center;gap:8px;padding:10px 14px;background:#f8fafc;border-radius:8px;margin-bottom:16px"><div style="width:14px;height:14px;border:2px solid #e2e8f0;border-top-color:#0ea5e9;border-radius:50%;animation:spin 0.8s linear infinite"></div><span style="font-size:12px;color:#64748b">Scanning for Zigbee devices…</span></div>`:''}
            ${state.pairingStep===3?`<div style="background:#f0fdf4;border:1px solid #d1fae5;border-radius:8px;padding:10px 14px;margin-bottom:16px;font-size:13px;color:#166534;font-weight:500">✓ New sensor detected and ready to assign</div>`:''}
            <div style="display:flex;gap:8px;justify-content:flex-end">
              <button onclick="cancelPairing()" style="background:#f8fafc;color:#374151;border:1px solid #e2e8f0;padding:8px 14px;border-radius:8px;cursor:pointer;font-size:13px;font-family:inherit">Cancel</button>
              ${state.pairingStep<3?`<button onclick="advancePairing()" style="background:#0f172a;color:#fff;border:none;padding:8px 14px;border-radius:8px;cursor:pointer;font-size:13px;font-weight:600;font-family:inherit">${state.pairingStep===0?'Start':state.pairingStep===1?'Next →':'...'}</button>`:''}
              ${state.pairingStep===3?`<button onclick="cancelPairing();refreshSensors();" style="background:#0f172a;color:#fff;border:none;padding:8px 14px;border-radius:8px;cursor:pointer;font-size:13px;font-weight:600;font-family:inherit">Done</button>`:''}
            </div>
          </div>
        </div>
        <style>@keyframes spin{to{transform:rotate(360deg)}}</style>
      `;
    }

    function assignModal() {
      const opts = state.cabinets.map(c =>
        `<option value="${c.id}">${esc(c.name)}</option>`
      ).join('');
      return `
        <div style="position:fixed;inset:0;background:rgba(0,0,0,0.4);z-index:100;display:flex;align-items:center;justify-content:center">
          <div style="background:#fff;border-radius:12px;padding:24px;width:380px;max-width:90vw;box-shadow:0 20px 60px rgba(0,0,0,0.15)">
            <div style="font-size:15px;font-weight:700;margin-bottom:16px">Assign Sensor</div>
            <label style="font-size:11px;font-weight:600;color:#94a3b8;letter-spacing:0.06em;text-transform:uppercase;display:block;margin-bottom:6px">Cabinet</label>
            <select id="assignTarget" style="width:100%;padding:10px 12px;font-size:14px;border:1px solid #e2e8f0;border-radius:8px;background:#f8fafc;font-family:inherit;outline:none;margin-bottom:20px">
              <option value="">Choose a cabinet…</option>
              ${opts}
              <option value="__new__">+ Create new cabinet</option>
            </select>
            <div style="display:flex;gap:8px;justify-content:flex-end">
              <button onclick="closeAssign()" style="background:#f8fafc;color:#374151;border:1px solid #e2e8f0;padding:8px 14px;border-radius:8px;cursor:pointer;font-size:13px;font-family:inherit">Cancel</button>
              <button onclick="confirmAssign()" style="background:#0f172a;color:#fff;border:none;padding:8px 14px;border-radius:8px;cursor:pointer;font-size:13px;font-weight:600;font-family:inherit">Assign</button>
            </div>
          </div>
        </div>
      `;
    }

    function viewNetwork() {
      const status = state.networkStatus || {};
      return `
        <div style="margin-bottom:24px">
          <h1 style="font-size:20px;font-weight:700;margin:0 0 4px;letter-spacing:-0.02em">Network</h1>
          <p style="font-size:13px;color:#64748b;margin:0">Connection status and Wi-Fi configuration</p>
        </div>

        <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:20px">
          <div style="background:${status.eth_connected?'#f0fdf9':'#f9fafb'};border:1px solid ${status.eth_connected?'#d1fae5':'#e5e7eb'};border-radius:12px;padding:16px 18px">
            <div style="font-size:11px;font-weight:600;color:#94a3b8;letter-spacing:0.06em;text-transform:uppercase;margin-bottom:8px">Ethernet</div>
            <div style="display:flex;align-items:center;gap:8px;margin-bottom:6px">
              <div style="width:8px;height:8px;border-radius:50%;background:${status.eth_connected?'#10b981':'#d1d5db'}"></div>
              <span style="font-size:14px;font-weight:600;color:${status.eth_connected?'#0a7c4e':'#64748b'}">${status.eth_connected?'Connected':'Disconnected'}</span>
            </div>
            ${status.eth_connected?`
              <div style="font-size:12px;color:#64748b;line-height:1.8">
                <div>IP: ${esc(status.eth_ip||'—')}</div>
                <div>Gateway: ${esc(status.gateway||'—')}</div>
                <div>mDNS: chillcheck.local</div>
              </div>
            `:'<div style="font-size:12px;color:#94a3b8">No ethernet connection detected</div>'}
          </div>
          <div style="background:${status.wifi_connected?'#f0fdf9':'#f9fafb'};border:1px solid ${status.wifi_connected?'#d1fae5':'#e5e7eb'};border-radius:12px;padding:16px 18px">
            <div style="font-size:11px;font-weight:600;color:#94a3b8;letter-spacing:0.06em;text-transform:uppercase;margin-bottom:8px">Wi-Fi</div>
            <div style="display:flex;align-items:center;gap:8px;margin-bottom:4px">
              <div style="width:8px;height:8px;border-radius:50%;background:${status.wifi_connected?'#10b981':'#d1d5db'}"></div>
              <span style="font-size:14px;font-weight:600;color:${status.wifi_connected?'#0a7c4e':'#64748b'}">${status.wifi_ssid||'Not connected'}</span>
            </div>
            <div style="font-size:12px;color:#94a3b8">${status.wifi_connected?'Fallback if ethernet disconnects':'Optional — ethernet preferred'}</div>
          </div>
        </div>

        <div style="background:#fff;border:1px solid #e2e8f0;border-radius:12px;padding:20px 24px">
          <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:14px">
            <div style="font-size:14px;font-weight:600">Wi-Fi Networks</div>
            <button onclick="scanNetworks()" style="background:#f8fafc;color:#374151;border:1px solid #e2e8f0;padding:7px 14px;border-radius:8px;cursor:pointer;font-size:13px;font-family:inherit">↺ Scan</button>
          </div>
          ${state.networks.length === 0
            ? '<p style="font-size:13px;color:#94a3b8">Click Scan to search for Wi-Fi networks</p>'
            : state.networks.map(net => `
                <div onclick="selectNetwork('${esc(net.ssid)}')"
                  style="display:flex;justify-content:space-between;align-items:center;padding:10px 14px;border-radius:8px;cursor:pointer;margin-bottom:6px;
                    border:1px solid ${state.selectedNetwork===net.ssid?'#0ea5e9':'#e2e8f0'};
                    background:${state.selectedNetwork===net.ssid?'#f0f9ff':'#f8fafc'}">
                  <div style="display:flex;gap:10px;align-items:center">
                    <span style="font-size:16px">📶</span>
                    <div>
                      <div style="font-size:13px;font-weight:500">${esc(net.ssid)}</div>
                      <div style="font-size:11px;color:#94a3b8">${net.secured?'Secured':'Open'} · ${net.strength}% signal</div>
                    </div>
                  </div>
                  ${state.selectedNetwork===net.ssid?'<span style="color:#0ea5e9;font-weight:700">✓</span>':''}
                </div>
              `).join('')
          }
          ${state.selectedNetwork ? `
            <div style="border-top:1px solid #f1f5f9;padding-top:14px;margin-top:8px">
              <label style="font-size:11px;font-weight:600;color:#94a3b8;letter-spacing:0.06em;text-transform:uppercase;display:block;margin-bottom:6px">Password for "${esc(state.selectedNetwork)}"</label>
              <div style="display:flex;gap:8px">
                <input type="password" id="wifiPassword" placeholder="Enter Wi-Fi password"
                  style="flex:1;padding:10px 12px;font-size:14px;border:1px solid #e2e8f0;border-radius:8px;background:#f8fafc;font-family:inherit;outline:none">
                <button onclick="connectWifi()"
                  style="background:#0f172a;color:#fff;border:none;padding:10px 16px;border-radius:8px;cursor:pointer;font-size:13px;font-weight:600;font-family:inherit">
                  Connect
                </button>
              </div>
            </div>
          ` : ''}
        </div>
      `;
    }

    function viewSystem() {
      const sys = state.systemStatus || {};
      const svcs = sys.services || [];
      const ver = state.versionInfo || {};
      return `
        <div style="margin-bottom:24px">
          <h1 style="font-size:20px;font-weight:700;margin:0 0 4px;letter-spacing:-0.02em">System</h1>
          <p style="font-size:13px;color:#64748b;margin:0">Service status and system information</p>
        </div>

        <div style="background:#fff;border:1px solid #e2e8f0;border-radius:12px;padding:20px 24px;margin-bottom:16px">
          <div style="font-size:14px;font-weight:600;margin-bottom:14px">Software</div>
          <div style="display:flex;justify-content:space-between;padding:8px 0;border-bottom:1px solid #f8fafc">
            <span style="font-size:12px;color:#64748b">Installed</span>
            <span style="font-size:12px;font-weight:600;font-family:monospace">${esc((ver.current||'unknown').slice(0,7))}</span>
          </div>
          <div style="display:flex;justify-content:space-between;padding:8px 0;border-bottom:1px solid #f8fafc">
            <span style="font-size:12px;color:#64748b">Latest available</span>
            <span style="font-size:12px;font-weight:600;font-family:monospace">${esc((ver.latest||'unknown').slice(0,7))}</span>
          </div>
          <div style="display:flex;justify-content:space-between;padding:8px 0;align-items:center">
            <span style="font-size:12px;color:#64748b">Status</span>
            ${ver.up_to_date
              ? '<span style="font-size:12px;font-weight:600;color:#0a7c4e">Up to date ✓</span>'
              : ver.current && ver.latest
                ? '<span style="font-size:12px;font-weight:600;color:#92400e">Update available</span>'
                : '<span style="font-size:12px;color:#94a3b8">Checking…</span>'
            }
          </div>
          <div style="display:flex;gap:8px;margin-top:14px">
            <button onclick="checkForUpdates()"
              style="background:#f8fafc;color:#374151;border:1px solid #e2e8f0;padding:7px 12px;border-radius:8px;cursor:pointer;font-size:12px;font-family:inherit"
              ${state.updateInProgress ? 'disabled' : ''}>
              Check again
            </button>
            ${!ver.up_to_date && ver.latest && !state.updateInProgress
              ? '<button onclick="runUpdate()" style="background:#0f172a;color:#fff;border:none;padding:7px 14px;border-radius:8px;cursor:pointer;font-size:12px;font-weight:600;font-family:inherit">Install update</button>'
              : ''
            }
            ${state.updateInProgress
              ? '<span style="font-size:12px;color:#64748b;align-self:center">Updating… page will reload automatically.</span>'
              : ''
            }
          </div>
        </div>

        <div style="background:#fff;border:1px solid #e2e8f0;border-radius:12px;padding:20px 24px;margin-bottom:16px">
          <div style="font-size:14px;font-weight:600;margin-bottom:14px">Services</div>
          ${svcs.map((svc,i) => `
            <div style="display:flex;justify-content:space-between;align-items:center;padding:10px 0;border-bottom:${i<svcs.length-1?'1px solid #f8fafc':'none'}">
              <div>
                <div style="font-size:13px;font-weight:600;margin-bottom:1px">${esc(svc.name)}</div>
                <div style="font-size:11px;color:#94a3b8">${esc(svc.description)}</div>
              </div>
              <div style="display:flex;align-items:center;gap:10px">
                <div style="display:flex;align-items:center;gap:5px">
                  <div style="width:7px;height:7px;border-radius:50%;background:${svc.active?'#10b981':'#ef4444'};box-shadow:${svc.active?'0 0 0 2px #d1fae5':'none'}"></div>
                  <span style="font-size:12px;font-weight:500;color:${svc.active?'#0a7c4e':'#dc2626'}">${svc.active?'Running':'Stopped'}</span>
                </div>
                <button onclick="restartService('${esc(svc.unit)}')"
                  style="background:#f8fafc;color:#374151;border:1px solid #e2e8f0;padding:5px 10px;border-radius:6px;cursor:pointer;font-size:12px;font-family:inherit">
                  Restart
                </button>
              </div>
            </div>
          `).join('')}
        </div>

        <div style="background:#fff;border:1px solid #e2e8f0;border-radius:12px;padding:20px 24px;margin-bottom:16px">
          <div style="font-size:14px;font-weight:600;margin-bottom:14px">System Info</div>
          ${Object.entries(sys.info||{}).map(([k,v],i,arr) => `
            <div style="display:flex;justify-content:space-between;padding:8px 0;border-bottom:${i<arr.length-1?'1px solid #f8fafc':'none'}">
              <span style="font-size:12px;color:#64748b">${esc(k)}</span>
              <span style="font-size:12px;font-weight:500">${esc(v)}</span>
            </div>
          `).join('')}
        </div>

        <div style="background:#fff5f5;border:1px solid #fecaca;border-radius:12px;padding:20px 24px">
          <div style="font-size:14px;font-weight:600;color:#991b1b;margin-bottom:14px">System Actions</div>
          <div style="display:flex;gap:10px;flex-wrap:wrap">
            <button onclick="restartService('all')" style="background:#fff5f5;color:#dc2626;border:1px solid #fecaca;padding:8px 14px;border-radius:8px;cursor:pointer;font-size:13px;font-weight:500;font-family:inherit">Restart All Services</button>
            <button onclick="if(confirm('Restart the Pi? Monitoring will pause for ~60 seconds.'))rebootPi()" style="background:#fff5f5;color:#dc2626;border:1px solid #fecaca;padding:8px 14px;border-radius:8px;cursor:pointer;font-size:13px;font-weight:500;font-family:inherit">Restart Pi</button>
          </div>
          <div style="font-size:11px;color:#dc2626;margin-top:10px">⚠ Restarting will interrupt temperature monitoring for 30–60 seconds.</div>
        </div>
      `;
    }

    // ── UI Components ────────────────────────────────────────

    function signalBars(rssi) {
      const s = !rssi ? 0 : rssi > -55 ? 3 : rssi > -70 ? 2 : 1;
      return `<div style="display:flex;align-items:flex-end;gap:2px;height:14px">
        ${[6,10,14].map((h,i) => `<div style="width:4px;height:${h}px;border-radius:1px;background:${i<s?'#10b981':'#e2e8f0'}"></div>`).join('')}
      </div>`;
    }

    function batteryBadge(pct) {
      if (pct === null || pct === undefined) return '<span style="font-size:11px;color:#94a3b8">—</span>';
      const color = pct > 60 ? '#10b981' : pct > 30 ? '#f59e0b' : '#ef4444';
      return `<span style="font-size:11px;font-weight:600;color:${color}">${pct}%</span>`;
    }

    // ── Actions ──────────────────────────────────────────────

    function navigate(view) {
      state.view = view;
      render();
      if (view === 'sensors') loadSensors();
      if (view === 'network') loadNetwork();
      if (view === 'system')  loadSystem();
    }

    async function loadSensors() {
      const data = await api('GET', '/api/sensors');
      state.sensors  = data.sensors  || [];
      state.cabinets = data.cabinets || [];
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
      if (!confirm('Install the latest update? Monitoring will pause for ~30 seconds.')) return;
      state.updateInProgress = true; render();
      try { await api('POST', '/api/system/update/run'); } catch (e) { /* expected: service restarts mid-request */ }
      // Wait long enough for the script to finish (or roll back) then reload.
      setTimeout(() => window.location.reload(), 45000);
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

    function openAssign(sensorId) {
      state.assigningId = sensorId;
      render();
    }

    function closeAssign() {
      state.assigningId = null;
      render();
    }

    async function confirmAssign() {
      const target = document.getElementById('assignTarget')?.value;
      if (!target) return;
      await api('POST', '/api/sensors/assign', {
        sensor_id: state.assigningId,
        cabinet_id: target === '__new__' ? null : target,
        create_cabinet: target === '__new__',
      });
      state.assigningId = null;
      await loadSensors();
    }

    async function unassignSensor(sensorId) {
      if (!confirm('Unassign this sensor? It will stop monitoring its cabinet.')) return;
      await api('POST', '/api/sensors/unassign', { sensor_id: sensorId });
      await loadSensors();
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

    render();
    loadCloudInfo();
    if (state.view === 'sensors') loadSensors();
    if (state.view === 'network') loadNetwork();
    if (state.view === 'system')  loadSystem();
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

# ── Sensors ───────────────────────────────────────────────────

@app.route("/api/sensors")
def api_sensors():
    """List all sensors and cabinets for this site."""
    supabase = get_supabase()
    if not supabase:
        return jsonify({"sensors": [], "cabinets": [], "error": "Not connected to cloud"})

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

        cabinets_res = (
            supabase.table("cabinets")
            .select("id, name, type, location")
            .eq("site_id", SITE_ID)
            .eq("active", True)
            .execute()
        )

        return jsonify({"sensors": sensors, "cabinets": cabinets_res.data})

    except Exception as e:
        log.error(f"api_sensors error: {e}")
        return jsonify({"sensors": [], "cabinets": [], "error": str(e)})


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


@app.route("/api/sensors/assign", methods=["POST"])
def api_sensors_assign():
    """Assign a sensor to a cabinet in Supabase."""
    data      = request.json or {}
    sensor_id = data.get("sensor_id")
    cabinet_id= data.get("cabinet_id")

    if not sensor_id or not cabinet_id:
        return jsonify({"success": False, "error": "sensor_id and cabinet_id required"})

    supabase = get_supabase()
    if not supabase:
        return jsonify({"success": False, "error": "Not connected to cloud"})

    try:
        supabase.table("sensors").update({
            "cabinet_id": cabinet_id,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", sensor_id).execute()
        return jsonify({"success": True})
    except Exception as e:
        log.error(f"Assign sensor failed: {e}")
        return jsonify({"success": False, "error": str(e)})


@app.route("/api/sensors/unassign", methods=["POST"])
def api_sensors_unassign():
    """Unassign a sensor from its cabinet."""
    data      = request.json or {}
    sensor_id = data.get("sensor_id")

    supabase = get_supabase()
    if not supabase:
        return jsonify({"success": False, "error": "Not connected to cloud"})

    try:
        supabase.table("sensors").update({
            "cabinet_id": None,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", sensor_id).execute()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


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
            ["sudo", "/usr/local/bin/chillcheck-update"],
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
        })

        # Reload env vars in process
        global SUPABASE_URL, SUPABASE_SERVICE_KEY, ORGANISATION_ID, SITE_ID, DEVICE_ID
        SUPABASE_URL         = result["supabase_url"]
        SUPABASE_SERVICE_KEY = result["supabase_service_key"]
        ORGANISATION_ID      = result["organisation_id"]
        SITE_ID              = result["site_id"]
        DEVICE_ID            = result["device_id"]

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
