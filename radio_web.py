"""
TACNET — Web Admin Server
============================
Lightweight HTTP server (stdlib only) wrapping RadioServer.
Serves the admin SPA and a JSON REST API.

Endpoints:
  GET  /                           → admin SPA
  GET  /api/status                 → server stats
  GET  /api/clients                → connected clients
  GET  /api/channels               → channel plan
  POST /api/channels/<id>          → create / update channel
  DELETE /api/channels/<id>        → delete channel
  GET  /api/callsigns              → callsign register
  POST /api/callsigns/<cs>         → create / update callsign
  DELETE /api/callsigns/<cs>       → delete callsign
  GET  /api/commsplan              → combined plan (channels + callsigns)
  GET  /api/commsplan/export       → plain-text formatted plan
  GET  /api/config                 → RadioConfig fields
  POST /api/config                 → update config (live + persist)
  POST /api/client/<cs>/kick       → drop client
  POST /api/client/<cs>/position   → set client position
  GET  /api/log                    → last 200 log lines
  POST /api/server/restart         → restart relay server
"""

from __future__ import annotations
import http.server
import json
import logging
import os
import sys
import threading
import time
import traceback
import math
import urllib.parse
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Optional, List
import numpy as np
from dataclasses import asdict
import queue
import base64
from radio_protocol import Packet

from radio_config import ConfigManager, ChannelDef, CallsignDef
from radio_effects import RangeModel
from radio_server import RadioServer
from radio_infra import latlon_to_mgrs, mgrs_to_latlon, InfrastructureManager, RetransNode, GPSState
from radio_terrain import get_terrain_manager

log = logging.getLogger("radio.web")

def _resolve_web_dir() -> Path:
    """
    Return the directory where bundled web assets (HTML files) live.

    Resolution order:
      1. Directory containing the running exe / script  (dist/TacNet/ in production)
      2. PyInstaller _MEIPASS extraction dir            (fallback for --onefile builds)
      3. Directory of this source file                  (development)
    """
    import sys
    # In a frozen (PyInstaller) exe, sys.executable is the .exe path
    if getattr(sys, 'frozen', False):
        exe_dir = Path(sys.executable).parent
        return exe_dir
    # Development: use the directory of this source file
    return Path(__file__).parent

def _find_asset(filename: str) -> Path:
    """
    Locate a bundled asset file, checking alongside the exe first,
    then the PyInstaller temp extraction dir, then this file's directory.
    """
    import sys
    candidates = []
    
    # Always search for both exact casing and all-lowercase
    filenames = [filename]
    if filename.lower() != filename:
        filenames.append(filename.lower())
        
    for fname in filenames:
        if getattr(sys, 'frozen', False):
            exe_dir = Path(sys.executable).parent
            # 1. Alongside the exe (--onefile, or same-dir copy)
            candidates.append(exe_dir / fname)
            # 2. Parent of exe dir (--onedir: exe is in TacNet-Server\,
            #    assets copied to parent TacNet\ by BUILD.bat)
            candidates.append(exe_dir.parent / fname)
            # 3. PyInstaller _MEIPASS (bundled inside exe, --onedir _internal\)
            meipass = getattr(sys, '_MEIPASS', None)
            if meipass:
                candidates.append(Path(meipass) / fname)
        # 4. Dev fallback
        candidates.append(Path(__file__).parent / fname)
        
    for p in candidates:
        if p.exists():
            return p
    # Return the most likely path even if it doesn't exist yet
    return candidates[0]


WEB_DIR    = _resolve_web_dir()
ADMIN_HTML = _find_asset("radio_admin.html")
PLANNER_HTML = _find_asset("radio_planner.html")
MOBILE_HTML = _find_asset("radio_mobile.html")
PLANNER_STORE = _find_asset("planner_data.json")
LOG_MAXLEN = 500


# -- In-memory log ring buffer --------------------------------------------------
class _RingHandler(logging.Handler):
    """In-memory circular log buffer for the web admin log tab."""
    def __init__(self, maxlen=LOG_MAXLEN):
        super().__init__()
        self._buf  = deque(maxlen=maxlen)
        self._lock = threading.Lock()

    def emit(self, record):
        try:
            # Use time.strftime directly — formatTime() lives on Formatter not Handler
            ts = time.strftime("%H:%M:%S", time.localtime(record.created))
            with self._lock:
                self._buf.append({
                    "ts":    record.created,
                    "time":  ts,
                    "level": record.levelname,
                    "name":  record.name,
                    "msg":   record.getMessage(),
                })
        except Exception:
            pass  # never let a logging error crash the server

    def get_lines(self, n=200):
        with self._lock:
            return list(self._buf)[-n:]


_ring = _RingHandler()
_root_log = logging.getLogger()
_root_log.addHandler(_ring)

# Ensure root logger passes INFO+ to our ring handler even before basicConfig runs.
# basicConfig called later in main() will not lower this if it's already set.
if _root_log.level == logging.NOTSET or _root_log.level > logging.INFO:
    _root_log.setLevel(logging.INFO)


# -- Net activity ring (PTT events, joins, etc for dashboard) ------------------
class _ActivityRing:
    def __init__(self, maxlen=200):
        self._buf  = deque(maxlen=maxlen)
        self._lock = threading.Lock()

    def add(self, msg: str, kind: str = "sys"):
        with self._lock:
            self._buf.append({
                "ts":   time.time(),
                "time": time.strftime("%H:%M:%S"),
                "kind": kind,
                "msg":  msg,
            })

    def get(self, n=100):
        with self._lock:
            return list(self._buf)[-n:]

_activity = _ActivityRing()


# -- Mobile Bridge (Voice over Web) -------------------------------------------
class _MobileAudioBridge:
    """Manages audio queues for web-based mobile clients."""
    def __init__(self):
        self.listeners: Dict[str, queue.Queue] = {}
        self._lock = threading.Lock()

    def on_audio(self, pkt: Packet, recipient_cs: str):
        """Callback from RadioServer relay loop."""
        with self._lock:
            q = self.listeners.get(recipient_cs)
            if q:
                try:
                    payload = base64.b64encode(pkt.payload).decode('ascii')
                    q.put_nowait({
                        "type":    "voice",
                        "ch":      pkt.channel,
                        "from":    pkt.callsign,
                        "payload": payload
                    })
                except queue.Full:
                    pass

    def get_queue(self, callsign: str):
        with self._lock:
            if callsign not in self.listeners:
                self.listeners[callsign] = queue.Queue(maxsize=50)
            return self.listeners[callsign]

    def remove_listener(self, callsign: str):
        with self._lock:
            self.listeners.pop(callsign, None)

_mobile_bridge = _MobileAudioBridge()


# -- Admin Monitor Bridge (Master Listen) --------------------------------------
class _AdminAudioBridge:
    """Manages a shared audio queue for all connected admin dashboards."""
    def __init__(self):
        self.queues: List[queue.Queue] = []
        self._master_queue = queue.Queue(maxsize=200)
        self._lock = threading.Lock()
        self._worker_thread = threading.Thread(target=self._worker_loop, daemon=True)
        self._worker_thread.start()

    def on_audio_pcm(self, cs: str, ch: int, pcm_bytes: bytes, signal: float):
        """Pushes decoded PCM data to the master worker queue for Web UI."""
        try:
            self._master_queue.put_nowait((cs, ch, pcm_bytes, signal))
        except queue.Full:
            pass

    def _worker_loop(self):
        """Single thread that encodes audio once for all listeners."""
        buffer = []
        last_flush = time.time()
        
        while True:
            try:
                # Wait for at least one message
                msg = self._master_queue.get(timeout=0.1)
                cs, ch, pcm_bytes, signal = msg
                
                # Pre-encode to base64
                pcm_b64 = base64.b64encode(pcm_bytes).decode('ascii')
                buffer.append({"type":"audio", "cs":cs, "ch":ch, "pcm":pcm_b64, "sig":signal})
                
                # Flush every 5 frames or 100ms to reduce network syscalls
                if len(buffer) >= 5 or (time.time() - last_flush > 0.1):
                    if buffer:
                        json_str = json.dumps(buffer)
                        with self._lock:
                            for q in self.queues[:]:
                                try:
                                    q.put_nowait(json_str)
                                except queue.Full:
                                    pass
                        buffer = []
                        last_flush = time.time()
            except queue.Empty:
                # Flush remaining if idle
                if buffer:
                    json_str = json.dumps(buffer)
                    with self._lock:
                        for q in self.queues[:]:
                            try: q.put_nowait(json_str)
                            except: pass
                    buffer = []
                    last_flush = time.time()
            except Exception as e:
                log.error("Error in Admin Audio Worker: %s", e)
                time.sleep(1)

    def get_queue(self):
        q = queue.Queue(maxsize=50)
        with self._lock:
            self.queues.append(q)
        return q

    def remove_queue(self, q):
        with self._lock:
            if q in self.queues:
                self.queues.remove(q)

_admin_bridge = _AdminAudioBridge()

# -- AAR Playback Loopback State ----------------------------------------------
_aar_playback_lock = threading.Lock()
_aar_playback_state = {
    "playing": False,
    "time": 0.0,
    "duration": 0.0,
    "speed": 1.0,
    "session_id": "",
    "file": ""
}
_aar_playback_listeners: List[queue.Queue] = []

def _audio_callback_multiplexer(*args):
    """
    Multiplexes the RadioServer audio_cb which is called with two different signatures:
    1. (callsign, channel, pcm_data, signal) -> for monitoring/recording
    2. (packet, recipient_callsign) -> for mobile relay
    """
    try:
        if len(args) == 4:
            # Monitoring feed (PCM)
            _admin_bridge.on_audio_pcm(*args)
        elif len(args) == 2:
            # Relay feed (Packet)
            _mobile_bridge.on_audio(*args)
    except Exception:
        pass


# -- Request handler ------------------------------------------------------------
class AdminHandler(http.server.BaseHTTPRequestHandler):
    server_ref: "WebAdminServer" = None

    def log_message(self, fmt, *args):
        log.debug("HTTP %s %s", self.address_string(), fmt % args)

    # -- Helpers ---------------------------------------------------------------
    def _cors(self):
        self.send_header("Access-Control-Allow-Origin",  "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,DELETE,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")


    def _json(self, data, status=200):
        try:
            body = json.dumps(data, default=str).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self._cors()
            self.end_headers()
            self.wfile.write(body)
        except (ConnectionError, BrokenPipeError):
            # Client disconnected during write; ignore
            pass

    def _text(self, text, status=200):
        body = text.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _err(self, msg, status=400):
        self._json({"error": msg}, status)

    def _read_json(self):
        try:
            n = int(self.headers.get("Content-Length", 0))
            return json.loads(self.rfile.read(n)) if n else {}
        except Exception as e:
            return None

    def _get_query(self):
        """Parse query parameters from the current request path."""
        try:
            from urllib.parse import urlparse, parse_qs
            query = urlparse(self.path).query
            params = parse_qs(query)
            # Flatten: parse_qs returns lists, we want single values for most things
            return {k: v[0] for k, v in params.items()}
        except Exception:
            return {}

    # -- Router ----------------------------------------------------------------
    def do_OPTIONS(self):
        self.send_response(204); self._cors(); self.end_headers()

    def do_GET(self):
        p = urllib.parse.urlparse(self.path).path.rstrip("/") or "/"
        if p.startswith("/api/sword"): print(f"DEBUG: GET {p}")
        try:
            routes = {
                "/":                    self._serve_html,
                "/index.html":          self._serve_html,
                "/planner":             self._serve_planner,
                "/mobile":              self._serve_mobile,
                "/api/status":          self._get_status,
                "/api/clients":         self._get_clients,
                "/api/channels":        self._get_channels,
                "/api/profiles":        self._get_profiles,
                "/api/callsigns":       self._get_callsigns,
                "/api/nodes":           self._get_nodes,
                "/api/commsplan":       self._get_commsplan,
                "/api/commsplan/export":self._get_commsplan_export,
                "/api/commsplan/download": self._get_commsplan_download,
                "/api/plan/load":       self._get_planner_load,
                "/api/config":          self._get_config,
                "/api/log":             self._get_log,
                "/api/activity":        self._get_activity,
                "/api/range":           self._get_range,
                "/api/aar/status":      self._get_aar_status,
                "/api/aar/sessions":    self._get_aar_sessions,
                "/api/ew/status":        self._get_ew_status,
                "/api/infra":            self._get_infra,
                "/api/infra/status":     self._get_infra_status,
                "/api/infra/routing":    self._get_routing,
                "/api/infra/routing/analytics": self._get_routing_analytics,
                "/api/routing":          self._get_routing,
                "/api/routing/analytics": self._get_routing_analytics,
                "/api/signal":           self._get_signal,
                "/api/mgrs":             self._get_mgrs,
                "/api/weather":          self._get_weather,
                "/api/mobile/stream":    self._get_mobile_stream,
                "/api/mobile/voice":     self._post_mobile_voice, # Handle POST stream
                "/api/daynight":         self._get_daynight,
                "/api/emi":              self._get_emi,
                "/api/terrain":          self._get_terrain,
                "/api/terrain/profile":  self._get_terrain_profile,
                "/api/links":            self._get_links,
                "/api/topology":         self._get_topology,
                "/api/admin/audio/stream": self._get_admin_audio_stream,
                "/api/admin/consolidated": self._get_admin_consolidated,
                "/api/aar/playback/stream": self._get_aar_playback_stream,
                "/api/aar/playback/state":  self._get_aar_playback_state_api,
                "/help/":               self._serve_help_file,
            }
            fn = routes.get(p)
            if fn:
                fn()
            elif p.startswith("/api/aar/session/"):
                parts = p.split("/")
                if len(parts) >= 5:
                    sid = parts[4]
                    if p.endswith("/report"):
                        self._get_aar_report(sid)
                    elif p.endswith("/download"):
                        self._get_aar_download(sid)
                    elif len(parts) >= 7 and parts[5] == "wav":
                        self._get_aar_wav(sid, parts[6])
                    else:
                        self._get_aar_session(sid)
                else:
                    self._err("Not found", 404)
            elif p.startswith("/api/aar/wav/"):
                # Serve a WAV file: /api/aar/wav/<session_id>/<filename>
                parts = p.split("/")
                if len(parts) >= 6:
                    self._get_aar_wav(parts[4], parts[5])
                else:
                    self._err("Not found", 404)
            elif p.startswith("/api/clients/"):
                cs = urllib.parse.unquote(p.split("/api/clients/")[1])
                self._get_client(cs)
            elif p.startswith("/api/tiles/"):
                tile_path = p.split("/api/tiles/")[1]
                self._get_tile(tile_path)
            elif p.startswith("/tabs/"):
                self._serve_tab_file()
            elif p.startswith("/help/"):
                self._serve_help_file()
            else:
                self._err("Not found", 404)
        except (ConnectionError, BrokenPipeError):
            # Log it but don't try to send an error response to a dead socket
            log.debug("Client disconnected during GET %s", p)
        except Exception as e:
            log.error("GET %s: %s\n%s", p, e, traceback.format_exc())
            try:
                self._err(str(e), 500)
            except: pass

    def do_POST(self):
        p    = urllib.parse.urlparse(self.path).path.rstrip("/")
        body = self._read_json()
        if body is None:
            self._err("Invalid JSON"); return
        try:
            if p.startswith("/api/channels/"):
                self._post_channel(int(p.split("/")[-1]), body)
            elif p.endswith("/kick"):
                self._kick_callsign(urllib.parse.unquote(p.split("/")[-2]))
            elif p.endswith("/position"):
                self._post_position(urllib.parse.unquote(p.split("/")[-2]), body)
            elif p.startswith("/api/callsigns/"):
                cs = urllib.parse.unquote(p.split("/api/callsigns/")[1])
                self._post_callsign(cs, body)
            elif p == "/api/callsigns":
                cs = body.get("callsign", "").strip().upper()
                if not cs:
                    self._err("Callsign required", 400)
                    return
                self._post_callsign(cs, body)
            elif p == "/api/plan/save":
                self._post_planner_save(body)
            elif p == "/api/plan/simulate":
                self._post_planner_simulate(body)
            elif p.startswith("/api/nodes/poll/"):
                node_id = urllib.parse.unquote(p.split("/api/nodes/poll/")[1])
                self._post_node_poll(node_id, body)
            elif p.startswith("/api/nodes/assign/"):
                node_id = urllib.parse.unquote(p.split("/api/nodes/assign/")[1])
                self._post_node_assign(node_id, body)
            elif p.startswith("/api/nodes/command/"):
                node_id = urllib.parse.unquote(p.split("/api/nodes/command/")[1])
                self._post_node_command(node_id, body)
            elif p == "/api/plan/path_profile":
                self._post_path_profile(body)
            elif p == "/api/plan/viewshed":
                self._post_planner_viewshed(body)
            elif p == "/api/config":
                self._post_config(body)
            elif p == "/api/server/restart":
                self._post_restart()
            elif p == "/api/server/start":
                self._post_server_start()
            elif p == "/api/server/stop":
                self._post_server_stop()
            elif p == "/api/commsplan/import":
                self._post_commsplan_import(body)
            elif p == "/api/aar/start":
                self._post_aar_start(body)
            elif p == "/api/aar/stop":
                self._post_aar_stop()
            elif p == "/api/aar/hhour":
                self._post_aar_hhour(body)
            elif p == "/api/weather":
                self._post_weather(body)
            elif p == "/api/daynight":
                self._post_daynight(body)
            elif p == "/api/emi":
                self._post_emi(body)
            elif p == "/api/ew/effect":
                self._post_ew_effect(body)
            elif p == "/api/ew/clear":
                self._post_ew_clear(body)
            elif p == "/api/routing" or p == "/api/infra/routing":
                self._post_routing(body)
            elif p == "/api/infra":
                self._post_infra(body)
            elif p == "/api/infra/node":
                self._post_infra_node(body)
            elif p.startswith("/api/infra/node/") and p.endswith("/delete"):
                node_id = p.split("/")[4]
                self._delete_infra_node(node_id)
            elif p == "/api/ew/jam":
                self._post_ew_jam(body)
            elif p == "/api/terrain":
                self._post_terrain(body)
            elif p == "/api/terrain/marker":
                self._post_terrain_marker(body)
            elif p == "/api/profiles":
                self._post_profile(body)
            elif p.startswith("/api/client/") and p.endswith("/channel"):
                callsign = urllib.parse.unquote(p.split("/")[-2])
                self._post_client_channel(callsign, body)
            elif p == "/api/aar/marker":
                self._post_aar_marker(body)
            elif p == "/api/weather/scenario":
                self._post_weather_scenario(body)
            elif p == "/api/aar/playback/control":
                self._post_aar_playback_control(body)
            elif p == "/api/aar/playback/state":
                self._post_aar_playback_state(body)
            else:
                self._err("Not found", 404)
        except Exception as e:
            log.error("POST %s: %s\n%s", p, e, traceback.format_exc())
            self._err(str(e), 500)

    def do_DELETE(self):
        p = urllib.parse.urlparse(self.path).path.rstrip("/")
        try:
            if p.startswith("/api/channels/"):
                self._delete_channel(int(p.split("/")[-1]))
            elif p.startswith("/api/callsigns/"):
                cs = urllib.parse.unquote(p.split("/api/callsigns/")[1])
                self._delete_callsign(cs)
            elif p.startswith("/api/terrain/marker/"):
                mid = urllib.parse.unquote(p.split("/api/terrain/marker/")[1])
                self._delete_terrain_marker(mid)
            elif p.startswith("/api/profiles/"):
                pid = urllib.parse.unquote(p.split("/api/profiles/")[1])
                self._delete_profile(pid)
            else:
                self._err("Not found", 404)
        except Exception as e:
            log.error("DELETE %s: %s", p, e)
            self._err(str(e), 500)

    # -- Static ----------------------------------------------------------------
    def _serve_html(self):
        html = ADMIN_HTML.read_bytes() if ADMIN_HTML.exists() else b"<h1>radio_admin.html not found</h1>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(html)))
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self._cors(); self.end_headers(); self.wfile.write(html)

    def _serve_planner(self):
        html = PLANNER_HTML.read_bytes() if PLANNER_HTML.exists() else b"<h1>radio_planner.html not found</h1>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(html)))
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self._cors(); self.end_headers(); self.wfile.write(html)

    def _serve_mobile(self):
        html = MOBILE_HTML.read_bytes() if MOBILE_HTML.exists() else b"<h1>radio_mobile.html not found</h1>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(html)))
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self._cors(); self.end_headers(); self.wfile.write(html)

    def _serve_help_file(self):
        """Serve files from the 'help/' subdirectory."""
        p = urllib.parse.urlparse(self.path).path
        parts = p.rstrip("/").split("/")
        filename = parts[-1] if len(parts) > 2 else "index.html"
        if not filename.endswith(".html"): filename += ".html"
        help_base = _find_asset("help")
        help_path = help_base / filename
        if help_path.exists():
            html = help_path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html)))
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self._cors(); self.end_headers(); self.wfile.write(html)
        else:
            self._err(f"Help file not found: {filename}", 404)

    def _serve_tab_file(self):
        """Serve files from the 'tabs/' subdirectory for dynamic loading."""
        p = urllib.parse.urlparse(self.path).path
        parts = p.rstrip("/").split("/")
        filename = parts[-1]
        if not filename.endswith(".html"): filename += ".html"
        tabs_base = _find_asset("tabs")
        tab_path = tabs_base / filename
        if tab_path.exists():
            html = tab_path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html)))
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self._cors(); self.end_headers(); self.wfile.write(html)
        else:
            self._err(f"Tab file not found: {filename}", 404)

    # -- GET handlers ----------------------------------------------------------
    def _get_admin_consolidated(self):
        srv = self.server_ref.radio_server
        cfg = self.server_ref.cfg_mgr.config
        
        # 1. status
        s   = srv.get_stats()
        status_data = {
            "running": s["running"], "uptime_s": s["uptime_s"],
            "uptime_str": _fmt_uptime(s["uptime_s"]),
            "packets_rx": s["packets_rx"], "packets_relayed": s["packets_relayed"],
            "packets_dropped": s["packets_dropped"],
            "bytes_rx": s["bytes_rx"], "bytes_tx": s["bytes_tx"],
            "clients_peak": s["clients_peak"], "clients_now": len(s["clients"]),
            "udp_port": cfg.server_port, "tcp_port": cfg.server_ctrl_port,
            "bind": cfg.bind_address, "range_enabled": cfg.range_enabled,
            "range_max_km": cfg.range_max_km, "tak_enabled": cfg.tak_enabled,
            "sword_enabled": cfg.sword_enabled, "web_port": self.server_ref.web_port,
            "active_summary": srv.get_active_summary(),
            "active_transmissions": srv.get_active_transmissions(),
        }
        
        # 2. weather
        weather_data = {
            "weather_enabled":          cfg.weather_enabled,
            "weather_severity":         cfg.weather_severity,
            "weather_type":             cfg.weather_type,
            "weather_humidity":         cfg.weather_humidity,
            "temp_inversion_enabled":   cfg.temp_inversion_enabled,
            "temp_inversion_strength":  cfg.temp_inversion_strength,
            "temp_inversion_echo_ms":   cfg.temp_inversion_echo_ms,
            "iono_enabled":             cfg.iono_enabled,
            "iono_condition":           cfg.iono_condition,
            "iono_fading":              cfg.iono_fading,
            "iono_flutter":             cfg.iono_flutter,
            "iono_absorption":          cfg.iono_absorption,
        }
        
        # 3. daynight
        import datetime as _dt
        wall_hour = _dt.datetime.now().hour + _dt.datetime.now().minute / 60.0
        daynight_data = {
            "day_night_enabled":  cfg.day_night_enabled,
            "day_night_hour":     cfg.day_night_hour,
            "day_night_auto":     cfg.day_night_auto,
            "day_night_latitude": cfg.day_night_latitude,
            "wall_hour":          round(wall_hour, 2),
        }
        
        # 4. emi
        emi_data = {
            "emi_enabled": cfg.emi_enabled,
            "emi_level":   cfg.emi_level,
            "emi_type":    cfg.emi_type,
        }
        
        # 5. infra
        infra_data = srv.infra.get_status() if hasattr(srv, 'infra') else {"retrans_nodes": [], "satcom": {}, "gps": {}}
        
        # 6. ew
        ew_data = srv.ew_engine.get_status() if hasattr(srv, 'ew_engine') else {"active": [], "count": 0, "log": []}
        
        # 7. aar
        rec = self.server_ref.recorder
        if rec is None:
            aar_data = {"recording": False, "enabled": False, "session": None, "active_tx": [], "time_source": "wall", "sword_ok": False}
        else:
            aar_data = rec.get_status()
            
        # 8. clients roster
        clients_data = self._build_clients_list()
            
        self._json({
            "status": status_data,
            "weather": weather_data,
            "daynight": daynight_data,
            "emi": emi_data,
            "infra": infra_data,
            "ew": ew_data,
            "aar": aar_data,
            "clients": clients_data
        })

    def _get_status(self):
        srv = self.server_ref.radio_server
        s   = srv.get_stats()
        cfg = self.server_ref.cfg_mgr.config
        self._json({
            "running": s["running"], "uptime_s": s["uptime_s"],
            "uptime_str": _fmt_uptime(s["uptime_s"]),
            "packets_rx": s["packets_rx"], "packets_relayed": s["packets_relayed"],
            "packets_dropped": s["packets_dropped"],
            "bytes_rx": s["bytes_rx"], "bytes_tx": s["bytes_tx"],
            "clients_peak": s["clients_peak"], "clients_now": len(s["clients"]),
            "udp_port": cfg.server_port, "tcp_port": cfg.server_ctrl_port,
            "bind": cfg.bind_address, "range_enabled": cfg.range_enabled,
            "range_max_km": cfg.range_max_km, "tak_enabled": cfg.tak_enabled,
            "sword_enabled": cfg.sword_enabled, "web_port": self.server_ref.web_port,
            # Active Transmissions Data
            "active_summary": srv.get_active_summary(),
            "active_transmissions": srv.get_active_transmissions(),
            # Topology is now fetched separately by the Topology tab to save CPU
        })

    def _get_topology(self):
        srv = self.server_ref.radio_server
        self._json(srv.get_topology())

    def _get_client_specs(self, profile_id):
        """Helper to return power/range for a profile ID."""
        mgr = self.server_ref.cfg_mgr
        p = mgr.profiles.get(profile_id)
        if p:
            return {"power_w": p.tx_power_w, "max_range_km": p.max_range_km, "full_quiet_km": p.full_quiet_km}
        return {"power_w": 5.0, "max_range_km": 7.0, "full_quiet_km": 1.5} # handheld default

    def _build_clients_list(self):
        s   = self.server_ref.radio_server.get_stats()
        now = time.time()
        out = []
        present_callsigns = set()

        for cs, c in s["clients"].items():
            present_callsigns.add(cs)
            age = now - c["last_seen"]
            prof_id = c.get("radio_profile", "handheld")
            specs = self._get_client_specs(prof_id)
            out.append({
                "callsign": cs, "channel": c["channel"],
                "tx_active": c["tx_active"], "lat": c["lat"], "lon": c["lon"],
                "alt": c.get("alt", 0), "packets_rx": c["packets_rx"],
                "packets_tx": c["packets_tx"],
                "radio_profile": prof_id,
                **specs,
                "mgrs": latlon_to_mgrs(c["lat"], c["lon"]) if c.get("lat") and c.get("lon") else "",
                "last_seen_s": round(age, 1), "last_seen_str": _fmt_age(age),
                "online": True,
                "evicted": c.get("evicted", False),
                "lock_left": max(0, int(c.get("lock_until", 0) - now))
            })

        mgr = self.server_ref.cfg_mgr
        for cs in sorted(mgr.callsigns.values(), key=lambda cl: cl.callsign):
            if cs.callsign in present_callsigns:
                continue
            if not (cs.lat or cs.lon):
                continue
            prof_id = getattr(cs, "radio_profile", "handheld")
            specs = self._get_client_specs(prof_id)
            out.append({
                "callsign": cs.callsign,
                "channel": (cs.channels[0] if cs.channels else -1),
                "tx_active": False,
                "lat": cs.lat,
                "lon": cs.lon,
                "alt": cs.alt,
                "mgrs": latlon_to_mgrs(cs.lat, cs.lon) if cs.lat and cs.lon else "",
                "radio_profile": prof_id,
                **specs,
                "packets_rx": 0,
                "packets_tx": 0,
                "last_seen_s": None,
                "last_seen_str": "stale",
                "online": False,
            })

        out.sort(key=lambda x: x["callsign"])
        return out

    def _get_clients(self):
        out = self._build_clients_list()
        self._json({"clients": out, "count": len(out)})

    def _get_client(self, callsign):
        callsign = callsign.upper()
        
        # Ensure callsign is registered dynamically if missing
        if callsign not in self.server_ref.cfg_mgr.callsigns:
            all_ch_ids = list(self.server_ref.cfg_mgr.channels.keys())
            cs_def = CallsignDef(callsign=callsign, channels=all_ch_ids)
            self.server_ref.cfg_mgr.callsigns[callsign] = cs_def
            self.server_ref.cfg_mgr.save()
            log.info("Dynamically registered unknown callsign: %s", callsign)

        srv = self.server_ref.radio_server
        stats = srv.get_stats()
        c = stats["clients"].get(callsign)
        if not c:
            cs_def = self.server_ref.cfg_mgr.callsigns.get(callsign)
            prof_id = getattr(cs_def, "radio_profile", "handheld")
            specs = self._get_client_specs(prof_id)
            assigned_chs = [c for c in cs_def.channels if c >= 0] if cs_def else None
            
            # Find intended channel pushed by server pace/commsplan for this callsign
            intended_ch = None
            for node in srv.node_manager.nodes.values():
                if node.callsign == callsign:
                    intended_ch = node.channel
                    break

            self._json({
                "callsign": cs_def.callsign,
                "online":   False,
                "assigned_channels": assigned_chs,
                "intended_channel": intended_ch,
                "radio_profile": prof_id,
                "lat":      cs_def.lat,
                "lon":      cs_def.lon,
                "alt":      cs_def.alt,
                **specs,
                "mgrs": latlon_to_mgrs(cs_def.lat, cs_def.lon) if (cs_def.lat or cs_def.lon) else "",
                "jammed":   False
            })
            return

        age = time.time() - c["last_seen"]
        prof_id = c.get("radio_profile", "handheld")
        specs = self._get_client_specs(prof_id)
        
        # Get assigned channels from the callsign register
        cs_def = self.server_ref.cfg_mgr.callsigns.get(callsign)
        assigned_chs = [c for c in cs_def.channels if c >= 0] if cs_def else None
        log.debug("API: Serving client %s with assigned_channels=%s", callsign, assigned_chs)

        # Find if this callsign is assigned to a node for intended channel push
        intended_ch = None
        for n in srv.node_manager.nodes.values():
            if n.callsign == callsign:
                intended_ch = n.channel
                break

        is_jammed = False
        if hasattr(srv, 'ew_engine'):
            is_jammed = srv.ew_engine.is_client_jammed(callsign, c["channel"])

        self._json({
            "callsign": callsign,
            "channel":  c["channel"],
            "intended_channel": intended_ch,
            "assigned_channels": assigned_chs,
            "online":   True,
            "radio_profile": prof_id,
            "lat":      c["lat"],
            "lon":      c["lon"],
            "alt":      c.get("alt", 0.0),
            "ptt_key":  getattr(cs_def, "ptt_key", "space") if cs_def else "space",
            **specs,
            "mgrs": latlon_to_mgrs(c["lat"], c["lon"]) if (c.get("lat") and c.get("lon")) else "",
            "last_seen_s": round(age, 1),
            "jammed": is_jammed
        })

    def _get_mobile_stream(self):
        """SSE stream for mobile audio downstream."""
        from radio_protocol import Packet, PktType
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        cs = qs.get("callsign", [""])[0]
        if not cs:
            self._err("Missing callsign"); return
        
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self._cors()
        self.end_headers()
        
        q = _mobile_bridge.get_queue(cs)
        try:
            while True:
                try:
                    data = q.get(timeout=2.0)
                    line = f"data: {json.dumps(data)}\n\n"
                    self.wfile.write(line.encode('utf-8'))
                    self.wfile.flush()
                except __import__('queue').Empty:
                    # Keepalive
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
        except Exception:
            _mobile_bridge.remove_listener(cs)

    def _post_mobile_voice(self):
        """Receive binary voice frames from mobile client."""
        from radio_protocol import Packet, PktType
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        cs = qs.get("callsign", [""])[0]
        ch = int(qs.get("channel", ["1"])[0])
        pt = qs.get("type", ["VOICE"])[0]
        
        length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(length) if length > 0 else b""
        
        pkt_type = PktType.VOICE
        if pt == "PTT_ON": pkt_type = PktType.PTT_ON
        elif pt == "PTT_OFF": pkt_type = PktType.PTT_OFF
        
        pkt = Packet(type=pkt_type, channel=ch, callsign=cs, payload=body)
        self.server_ref.radio_server.inject_packet(pkt)
        self._json({"ok": True})

    def _get_channels(self):
        mgr = self.server_ref.cfg_mgr
        srv = self.server_ref.radio_server
        out = []
        for ch in sorted(mgr.channels.values(), key=lambda c: c.id):
            members = srv.get_channel_members(ch.id)
            # Find callsigns assigned to this channel
            assigned = [
                cs.callsign for cs in mgr.callsigns.values()
                if ch.id in cs.channels
            ]
            out.append({
                "id": ch.id, "name": ch.name, "frequency": ch.frequency,
                "encrypted": ch.encrypted,
                "passphrase_set": bool(ch.passphrase),
                "description": ch.description, "enabled": ch.enabled,
                "modulation": ch.modulation,
                "bandwidth_khz": ch.bandwidth_khz,
                "waveform_type": ch.waveform_type,
                "eccm_mode": ch.eccm_mode,
                "net_id": ch.net_id,
                "comsec_mode": ch.comsec_mode,
                "key_id": ch.key_id,
                "squelch_tone": ch.squelch_tone,
                "member_count": len(members),
                "members_online": [m["callsign"] for m in members],
                "callsigns_assigned": assigned,
            })
        self._json({"channels": out})

    def _get_nodes(self):
        nodes = self.server_ref.radio_server.node_manager.get_all()
        mgr = self.server_ref.cfg_mgr
        legacy_map = {"handheld": "prc152", "manpack": "prc117g", "vehicle": "sincgars_veh"}
        
        # Enrich nodes with assigned channels and profiles from the callsign register
        for n in nodes:
            cs_name = (n.get("callsign") or "").strip().upper()
            if cs_name:
                cs_def = mgr.callsigns.get(cs_name)
                n["assigned_channels"] = [c for c in cs_def.channels if c >= 0] if cs_def else []
                prof_id = getattr(cs_def, "radio_profile", "prc152") if cs_def else "prc152"
                # Map legacy profile names to modern IDs
                prof_id = legacy_map.get(prof_id, prof_id)
                n["radio_profile"] = prof_id
                
                # Also include friendly name for UI
                p_def = mgr.profiles.get(prof_id)
                n["radio_profile_name"] = p_def.name if p_def else (prof_id.upper() if prof_id else "HANDHELD")
            else:
                n["assigned_channels"] = []
                n["radio_profile"] = "—"
                n["radio_profile_name"] = "—"
        self._json({"nodes": nodes})

    def _get_callsigns(self):
        mgr = self.server_ref.cfg_mgr
        srv = self.server_ref.radio_server
        stats = srv.get_stats()
        online = set(stats["clients"].keys())
        out = []
        for cs in sorted(mgr.callsigns.values(), key=lambda c: c.callsign):
            cli = stats["clients"].get(cs.callsign, {})
            out.append({
                "callsign":  cs.callsign,
                "real_name": cs.real_name,
                "unit":      cs.unit,
                "parent_unit": cs.parent_unit,
                "role":      cs.role,
                "channels":  cs.channels,
                "lat":       cs.lat,
                "lon":       cs.lon,
                "alt":       cs.alt,
                "mgrs":      latlon_to_mgrs(cs.lat, cs.lon) if cs.lat and cs.lon else "",
                "radio_profile": cs.radio_profile,
                "ptt_key":       getattr(cs, "ptt_key", "space"),
                "online":    cs.callsign in online,
                "channel_now": cli.get("channel"),
                "tx_active":   cli.get("tx_active", False),
            })
        self._json({"callsigns": out, "count": len(out)})

    def _get_profiles(self):
        mgr = self.server_ref.cfg_mgr
        profiles_list = [
            asdict(p)
            for p in sorted(mgr.profiles.values(), key=lambda x: x.id)
        ]
        self._json({"profiles": profiles_list})

    def _post_profile(self, body):
        mgr = self.server_ref.cfg_mgr
        pid = body.get("id")
        if not pid:
            self._err("Profile ID required"); return
        from radio_config import RadioProfile
        import dataclasses
        
        # Get existing profile as baseline fallback
        existing = mgr.profiles.get(pid)
        existing_dict = asdict(existing) if existing else {}
        
        kwargs = {}
        for f in dataclasses.fields(RadioProfile):
            k = f.name
            if k in body:
                val = body[k]
                # Coerce values to the correct field types
                if f.type is bool or f.type == "bool":
                    if isinstance(val, str):
                        kwargs[k] = val.lower() in ("true", "1", "yes")
                    else:
                        kwargs[k] = bool(val)
                elif f.type is float or f.type == "float":
                    try: kwargs[k] = float(val)
                    except: kwargs[k] = f.default if f.default is not dataclasses.MISSING else 0.0
                elif f.type is int or f.type == "int":
                    try: kwargs[k] = int(val)
                    except: kwargs[k] = f.default if f.default is not dataclasses.MISSING else 0
                else:
                    kwargs[k] = str(val)
            elif k in existing_dict:
                kwargs[k] = existing_dict[k]
            else:
                if f.default is not dataclasses.MISSING:
                    kwargs[k] = f.default
                elif f.default_factory is not dataclasses.MISSING:
                    kwargs[k] = f.default_factory()
                else:
                    kwargs[k] = ""
                    
        prof = RadioProfile(**kwargs)
        mgr.profiles[pid] = prof
        mgr.save()
        self._json({"status": "ok", "profile": asdict(prof)})

    def _delete_profile(self, pid):
        mgr = self.server_ref.cfg_mgr
        if pid in ["handheld", "manpack", "base_station"]:
            self._err("Cannot delete default hardware profiles", 403); return
        if pid in mgr.profiles:
            del mgr.profiles[pid]
            mgr.save()
            self._json({"status": "ok"})
        else:
            self._err("Profile not found", 404)

    def _get_commsplan(self):
        mgr = self.server_ref.cfg_mgr
        srv = self.server_ref.radio_server
        ch_list  = sorted(mgr.channels.values(), key=lambda c: c.id)
        cs_list  = sorted(mgr.callsigns.values(), key=lambda c: c.callsign)
        stats    = srv.get_stats()
        online   = set(stats["clients"].keys())

        channels_out = []
        for ch in ch_list:
            members = srv.get_channel_members(ch.id)
            assigned = [c.callsign for c in cs_list if ch.id in c.channels]
            channels_out.append({
                "id": ch.id, "name": ch.name, "frequency": ch.frequency,
                "encrypted": ch.encrypted, "passphrase_set": bool(ch.passphrase),
                "description": ch.description, "enabled": ch.enabled,
                "modulation": ch.modulation,
                "bandwidth_khz": ch.bandwidth_khz,
                "waveform_type": ch.waveform_type,
                "eccm_mode": ch.eccm_mode,
                "net_id": ch.net_id,
                "comsec_mode": ch.comsec_mode,
                "key_id": ch.key_id,
                "squelch_tone": ch.squelch_tone,
                "members_online": [m["callsign"] for m in members],
                "callsigns_assigned": assigned,
            })

        callsigns_out = []
        for cs in cs_list:
            cli = stats["clients"].get(cs.callsign, {})
            callsigns_out.append({
                "callsign": cs.callsign, "real_name": cs.real_name,
                "unit": cs.unit, "role": cs.role, "channels": cs.channels,
                "radio_profile": getattr(cs, "radio_profile", "handheld"),
                "online": cs.callsign in online,
                "channel_now": cli.get("channel"),
            })

        self._json({
            "exercise_name": mgr.config.unit or "EXERCISE",
            "generated_at":  time.strftime("%Y-%m-%d %H:%M:%S"),
            "channels":      channels_out,
            "callsigns":     callsigns_out,
        })

    def _get_commsplan_export(self):
        mgr  = self.server_ref.cfg_mgr
        srv  = self.server_ref.radio_server
        now  = time.strftime("%Y-%m-%d %H:%M:%S")
        name = mgr.config.unit or "EXERCISE"
        lines = [
            "=" * 72,
            f"  COMMUNICATIONS PLAN — {name}",
            f"  Generated: {now}",
            "=" * 72,
            "",
            "SECTION 1 — CHANNEL PLAN",
            "-" * 72,
            f"{'CH':<5} {'NAME':<20} {'FREQUENCY':<16} {'ENC':<6} {'DESCRIPTION'}",
            "-" * 72,
        ]
        for ch in sorted(mgr.channels.values(), key=lambda c: c.id):
            enc = "ENC" if ch.encrypted else "CLR"
            en  = "" if ch.enabled else "[DISABLED]"
            lines.append(
                f"CH{ch.id:02d}  {ch.name:<20} {ch.frequency:<16} {enc:<6} "
                f"{ch.description} {en}"
            )
        lines += [
            "",
            "SECTION 2 — CALLSIGN REGISTER",
            "-" * 72,
            f"{'CALLSIGN':<18} {'UNIT':<16} {'ROLE':<20} {'CHANNELS':<20} {'REAL NAME'}",
            "-" * 72,
        ]
        for cs in sorted(mgr.callsigns.values(), key=lambda c: c.callsign):
            ch_str = ", ".join(f"CH{c:02d}" for c in sorted(cs.channels)) or "—"
            lines.append(
                f"{cs.callsign:<18} {cs.unit:<16} {cs.role:<20} "
                f"{ch_str:<20} {cs.real_name}"
            )
        lines += ["", "=" * 72, "// END OF COMMS PLAN", "=" * 72]
        self._text("\n".join(lines))

    def _get_config(self):
        cfg = self.server_ref.cfg_mgr.config
        self._json({
            f: getattr(cfg, f)
            for f in cfg.__dataclass_fields__
            if f != "connected"
        })

    def _get_log(self):
        self._json({"lines": _ring.get_lines(200)})

    def _get_activity(self):
        self._json({"lines": _activity.get(100)})

    def _get_range(self):
        try:
            # Parse optional query params for previewing
            params = self._get_query()
            preview_profile_id = params.get("profile")
            preview_freq = float(params.get("freq", 45.0))

            cfg = self.server_ref.cfg_mgr.config
            
            # Base values from global config
            max_km = cfg.range_max_km
            quiet_km = cfg.range_full_quiet_km
            pwr_w = cfg.transmit_power_w
            
            # Override with profile if requested
            if preview_profile_id:
                prof = self.server_ref.cfg_mgr.profiles.get(preview_profile_id)
                if prof:
                    max_km = prof.max_range_km
                    quiet_km = prof.full_quiet_km
                    pwr_w = prof.tx_power_w

            model = RangeModel(max_range_km=max_km,
                               full_quiet_km=quiet_km,
                               power_w=pwr_w,
                               terrain_factor=cfg.terrain_factor)
            
            # Sync weather state to the model for the preview
            model.weather_enabled = cfg.weather_enabled
            model.weather_severity = cfg.weather_severity
            model.weather_type = cfg.weather_type
            model.weather_humidity = cfg.weather_humidity

            pts = [{"distance_km": d,
                    "signal": round(model.signal_strength(d, freq_mhz=preview_freq), 3)}
                   for d in range(0, int(max_km * 1.5) + 1)]
            
            # Sanitize any potential NaNs in the curve points for JSON safety
            for p in pts:
                if math.isnan(p["signal"]): p["signal"] = 0.0

            self._json({
                "max_range_km": max_km,
                "full_quiet_km": quiet_km,
                "transmit_power_w": pwr_w,
                "terrain_factor": cfg.terrain_factor,
                "range_enabled": cfg.range_enabled,
                "preview_freq_mhz": preview_freq,
                "curve": pts,
            })
        except Exception as e:
            import traceback
            err_msg = f"Range API Error: {e}\n{traceback.format_exc()}"
            log.error(err_msg)
            with open(r'c:\Users\johnn\Desktop\mil_radiov3\simulator_debug.txt', 'a') as f:
                f.write(err_msg + "\n")
            self._err(str(e), 500)

    def _get_terrain(self):
        from radio_terrain import get_terrain_manager
        terrain = get_terrain_manager()
        # Merge with config.terrain_map if needed, or just return terrain manager status
        self._json(terrain.get_status())
        
    def _get_tile(self, tile_path):
        import os
        base_dir = _find_asset("offline_map_data")
        tile_path = tile_path.replace("/", os.sep)
        filepath = os.path.join(base_dir, tile_path)
        
        # Prevent traversal
        if not os.path.abspath(filepath).startswith(os.path.abspath(base_dir)):
            self._err("Forbidden", 403)
            return
            
        if not os.path.exists(filepath):
            self._err("Not Found", 404)
            return
            
        try:
            with open(filepath, "rb") as f:
                data = f.read()
            self.send_response(200)
            if filepath.endswith('.png'):
                self.send_header("Content-Type", "image/png")
            elif filepath.endswith('.jpg') or filepath.endswith('.jpeg'):
                self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(data)))
            self._cors()
            self.end_headers()
            self.wfile.write(data)
        except Exception as e:
            self._err(str(e), 500)

    def _get_links(self):
        """Return all capable transmission links for drawing on the map permanently."""
        srv = self.server_ref.radio_server
        mgr = self.server_ref.cfg_mgr
        stats = srv.get_stats()
        
        entities = []
        for cs_name, cs_def in mgr.callsigns.items():
                client_stats = stats.get("clients", {}).get(cs_name, {})
                # Use live position from server state if callsign def has no position
                lat = cs_def.lat or client_stats.get("lat", 0.0)
                lon = cs_def.lon or client_stats.get("lon", 0.0)
                if lat or lon:
                    active_ch = client_stats.get("channel", cs_def.channels[0] if cs_def.channels else -1)
                    mon_chs = client_stats.get("monitor_channels", [])
                    entities.append({
                        "type": "callsign", 
                        "id": cs_name, 
                        "lat": lat, 
                        "lon": lon, 
                        "channel": active_ch,
                        "monitor_channels": mon_chs,
                        "assigned_channels": cs_def.channels
                    })
                
        for r_id, r_node in srv.infra.retrans_nodes.items():
            if r_node.enabled and (r_node.lat or r_node.lon):
                entities.append({
                    "type": "retrans", 
                    "id": r_node.node_id, 
                    "lat": r_node.lat, 
                    "lon": r_node.lon, 
                    "assigned_channels": [c for c in (r_node.channels or []) if c >= 0]
                })
                
        links = []
        for i, e1 in enumerate(entities):
            for j, e2 in enumerate(entities):
                if i >= j: continue
                
                connections = [] # list of (logical_type, shared_ch)
                
                if e1["type"] == "callsign" and e2["type"] == "callsign":
                    if e1["channel"] >= 0 and e1["channel"] == e2["channel"]:
                        connections.append(("primary", e1["channel"]))
                    if e1["channel"] >= 0 and e1["channel"] in e2["monitor_channels"]:
                        connections.append(("monitor", e1["channel"]))
                    if e2["channel"] >= 0 and e2["channel"] in e1["monitor_channels"]:
                        connections.append(("monitor", e2["channel"]))
                        
                elif (e1["type"] == "callsign" and e2["type"] == "retrans") or \
                     (e1["type"] == "retrans" and e2["type"] == "callsign"):
                    cli = e1 if e1["type"] == "callsign" else e2
                    ret = e2 if e1["type"] == "callsign" else e1
                    
                    ret_chs = [c for c in ret["assigned_channels"] if c >= 0]
                    
                    if cli["channel"] >= 0 and (not ret_chs or cli["channel"] in ret_chs):
                        connections.append(("primary", cli["channel"]))
                        
                    for m_ch in cli.get("monitor_channels", []):
                        if m_ch >= 0 and (not ret_chs or m_ch in ret_chs):
                            connections.append(("monitor", m_ch))
                
                # Deduplicate connections to avoid identical lines, keep best logical type per channel
                unique_conns = {}
                for l_type, ch in connections:
                    if ch not in unique_conns or (l_type == "primary" and unique_conns[ch] == "monitor"):
                        unique_conns[ch] = l_type
                
                for shared_ch, logical_type in unique_conns.items():
                    path = srv.infra.compute_path(
                        e1["lat"], e1["lon"], e2["lat"], e2["lon"], shared_ch,
                        range_fn=srv._range.signal_from_positions,
                        sender_cs=e1["id"], recipient_cs=e2["id"]
                    )
                    
                    if path.signal > 0.05:
                        phys_type = "direct"
                        if "retrans" in path.link_type: phys_type = "retrans"
                        elif "satcom" in path.link_type: phys_type = "satcom"

                        links.append({
                            "source": e1["id"],
                            "target": e2["id"],
                            "signal": round(path.signal, 3),
                            "terrain_blocked": path.terrain_blocked,
                            "logical_type": logical_type,
                            "physical_type": phys_type,
                            "type": path.link_type,
                            "notes": path.notes,
                            "channel": shared_ch
                        })
        self._json({"links": links})

    def _get_terrain(self):
        """GET /api/terrain — return terrain system status."""
        tm = self.server_ref.radio_server.infra.terrain
        self._json(tm.get_status())

    def _get_terrain_profile(self):
        """GET /api/terrain/profile — return elevation profile between two points."""
        try:
            params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            lat1 = float(params.get("lat1", [0])[0])
            lon1 = float(params.get("lon1", [0])[0])
            lat2 = float(params.get("lat2", [0])[0])
            lon2 = float(params.get("lon2", [0])[0])
            
            tm = self.server_ref.radio_server.infra.terrain
            from radio_terrain import EFFECTIVE_EARTH_RADIUS_KM
            
            # Sample points along the path
            dist_km = tm._distance_km(lat1, lon1, lat2, lon2)
            num_samples = max(20, min(100, int(dist_km * 5))) # Performance optimized sampling
            
            profile = []
            for i in range(num_samples + 1):
                t = i / num_samples
                cur_lat = lat1 + (lat2 - lat1) * t
                cur_lon = lon1 + (lon2 - lon1) * t
                
                elev = tm.get_elevation_highres(cur_lat, cur_lon)
                if elev is None:
                    cell = tm.get_cell(cur_lat, cur_lon)
                    elev = cell.elevation_m if cell else 0.0
                
                d_from_start = t * dist_km
                curv_drop = (d_from_start * (dist_km - d_from_start)) / (2 * EFFECTIVE_EARTH_RADIUS_KM) * 1000.0
                
                profile.append({
                    "dist_km": round(d_from_start, 3),
                    "elevation_m": round(elev, 2),
                    "curv_drop_m": round(curv_drop, 2)
                })
            
            self._json({
                "distance_km": round(dist_km, 3),
                "samples": profile
            })
        except Exception as e:
            self._err(f"Profile failed: {e}")

    # -- POST handlers ---------------------------------------------------------
    def _post_channel(self, ch_id, body):
        mgr = self.server_ref.cfg_mgr
        ch  = mgr.channels.get(ch_id) or ChannelDef(id=ch_id, name=f"CH{ch_id:02d}", frequency="")
        mgr.channels[ch_id] = ch
        for f in ("name", "frequency", "description", "modulation", "waveform_type", "eccm_mode", "net_id", "comsec_mode", "key_id"):
            if f in body:
                setattr(ch, f, str(body[f]))
        if "bandwidth_khz" in body:
            try: ch.bandwidth_khz = float(body["bandwidth_khz"])
            except: pass
        if "squelch_tone" in body:
            try: ch.squelch_tone = float(body["squelch_tone"])
            except: pass

        if "passphrase" in body and body["passphrase"] not in ("", None):
            if not set(body["passphrase"]).issubset({"●"}):   # not the masked placeholder
                ch.passphrase = str(body["passphrase"])
        elif body.get("clear_passphrase"):
            ch.passphrase = ""
        if "enabled" in body:
            ch.enabled = bool(body["enabled"])
        mgr.save()
        self.server_ref.radio_server._build_codecs()
        log.info("Channel %d updated: %s", ch_id, ch.name)
        self._json({"ok": True, "channel": ch_id})

    def _delete_channel(self, ch_id):
        mgr = self.server_ref.cfg_mgr
        if ch_id not in mgr.channels:
            self._err("Channel not found", 404); return
        del mgr.channels[ch_id]
        mgr.save()
        log.info("Channel %d deleted", ch_id)
        self._json({"ok": True, "deleted": ch_id})

    def _post_callsign(self, cs_key, body):
        cs_key = cs_key.upper()
        mgr = self.server_ref.cfg_mgr
        cs  = mgr.callsigns.get(cs_key) or CallsignDef(callsign=cs_key)
        # Allow renaming: if body has 'callsign' and it differs, rename key
        new_cs = body.get("callsign", cs_key).strip().upper()
        if new_cs != cs_key and cs_key in mgr.callsigns:
            del mgr.callsigns[cs_key]
        cs.callsign  = new_cs
        cs.real_name = body.get("real_name", cs.real_name)
        cs.unit      = body.get("unit",      cs.unit)
        cs.parent_unit = body.get("parent_unit", cs.parent_unit)
        cs.role      = body.get("role",      cs.role)
        cs.lat       = float(body.get("lat", cs.lat) or 0)
        cs.lon       = float(body.get("lon", cs.lon) or 0)
        cs.alt       = float(body.get("alt", cs.alt) or 0)
        legacy_map = {"handheld": "prc152", "manpack": "prc117g", "vehicle": "sincgars_veh"}
        raw_prof = body.get("radio_profile", getattr(cs, "radio_profile", "prc152"))
        cs.radio_profile = legacy_map.get(raw_prof, raw_prof)
        cs.ptt_key       = body.get("ptt_key", getattr(cs, "ptt_key", "space"))
        
        # If the client provides a node_id, update the remote node assignment
        node_id = body.get("node_id")
        pid = body.get("pid")
        if node_id:
            self.server_ref.radio_server.node_manager.assign_config(node_id, new_cs, None, pid=pid)
        if "channels" in body:
            try:
                cs.channels = [int(c) for c in body["channels"]]
            except Exception:
                pass
        mgr.callsigns[new_cs] = cs
        mgr.save()

        # Update active server client state immediately if connected
        srv = self.server_ref.radio_server
        srv.update_client_profile(new_cs, cs.radio_profile)
        srv.update_client_position(new_cs, cs.lat, cs.lon, cs.alt)

        log.info("Callsign saved: %s (%s / %s)", new_cs, cs.unit, cs.role)
        self._json({"ok": True, "callsign": new_cs})

    def _post_node_poll(self, node_id, body):
        status = body.get("status", "offline")
        pid = body.get("pid", None)
        adopted = body.get("adopted", False)
        res = self.server_ref.radio_server.node_manager.update_node(node_id, status, pid, adopted=adopted)
        self._json(res)

    def _post_node_assign(self, node_id, body):
        cs = body.get("callsign", "")
        ch = body.get("channel", None)
        target_ch = int(ch) if ch is not None else -1
        
        nm = self.server_ref.radio_server.node_manager
        
        # Check if state actually changed before triggering a push
        changed = True
        for n in nm.get_all():
            if n.get("node_id") == node_id:
                if n.get("callsign") == cs and n.get("channel") == target_ch:
                    changed = False
                break
                
        if changed:
            nm.assign_config(node_id, cs, target_ch)
            if cs:
                self.server_ref.radio_server.force_client_channel(cs, target_ch)
            
        self._json({"msg": "Node assigned"})

    def _post_node_command(self, node_id, body):
        cmd = body.get("command", "none")
        # Support for structured control commands
        if "action" in body:
            action = body["action"]
            if action == "set_mute":
                cmd = {"type": "control", "mute": bool(body.get("value", False))}
            elif action == "set_volume":
                cmd = {"type": "control", "volume": float(body.get("value", 1.0))}
            elif action == "set_channel":
                cmd = {"type": "control", "channel": int(body.get("value", 1))}

        # Safety check for adopted nodes
        if cmd == "stop":
            nm = self.server_ref.radio_server.node_manager
            with nm._lock:
                node = nm.nodes.get(node_id)
                if node and node.adopted and not body.get("force", False):
                    self._err("Safety Lock: This node was manually started and 'Adopted'. Use force to stop.", 403)
                    return

        self.server_ref.radio_server.node_manager.send_command(node_id, cmd)
        self._json({"ok": True, "msg": f"Command sent to node {node_id}"})

    def _get_admin_audio_stream(self):
        """SSE endpoint for master audio monitoring."""
        q = _admin_bridge.get_queue()
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self._cors()
            self.end_headers()

            while self.server_ref._running:
                try:
                    # q now contains a pre-encoded JSON string (array of audio frames)
                    json_payload = q.get(timeout=5.0)
                    chunk = f"data: {json_payload}\n\n".encode()
                    self.wfile.write(chunk)
                except queue.Empty:
                    # Keep-alive
                    self.wfile.write(b":keep-alive\n\n")
                except (ConnectionError, BrokenPipeError):
                    break
        finally:
            _admin_bridge.remove_queue(q)

    def _delete_callsign(self, cs):
        mgr = self.server_ref.cfg_mgr
        cs  = cs.upper()
        if cs not in mgr.callsigns:
            self._err("Callsign not found", 404); return
        del mgr.callsigns[cs]
        mgr.save()

        # Also remove active client so they don't "reappear" instantly
        srv = self.server_ref.radio_server
        with srv._clients_lock:
            if cs in srv._clients:
                del srv._clients[cs]
                
        log.info("Callsign deleted: %s", cs)
        self._json({"ok": True, "deleted": cs})

    def _kick_callsign(self, callsign):
        """Force drop a client from the active list and clear position."""
        callsign = callsign.upper()
        srv = self.server_ref.radio_server
        mgr = self.server_ref.cfg_mgr
        kicked_online = False
        kicked_ghost  = False

        # 1. Update live server state
        with srv._clients_lock:
            if callsign in srv._clients:
                c = srv._clients[callsign]
                c.update_position(0, 0, 0, is_admin=True) # This sets evicted=True
                log.info("Admin evicted online client: %s", callsign)
                kicked_online = True
        
        # 2. Clear from register (Ghost removal)
        if callsign in mgr.callsigns:
            cs = mgr.callsigns[callsign]
            cs.evicted = True
            if cs.lat or cs.lon:
                cs.lat = 0
                cs.lon = 0
                cs.alt = 0
                mgr.save()
                log.info("Admin cleared register position and EVICTED: %s", callsign)
                kicked_ghost = True
            else:
                # Even if no position, save the evicted flag
                mgr.save()
                kicked_ghost = True

        if kicked_online or kicked_ghost:
            self._json({"ok": True, "kicked": callsign, "online": kicked_online, "ghost": kicked_ghost})
        else:
            self._err("Callsign not found in active sessions or register", 404)

    def _post_position(self, callsign, body):
        callsign = callsign.upper()
        try:
            lat = float(body.get("lat", 0))
            lon = float(body.get("lon", 0))
            alt = float(body.get("alt", 0))
        except (ValueError, TypeError) as e:
            self._err(f"Invalid coords: {e}"); return
        self.server_ref.radio_server.update_client_position(callsign, lat, lon, alt)
        # Also update callsign register if it exists
        mgr = self.server_ref.cfg_mgr
        if callsign in mgr.callsigns:
            cs = mgr.callsigns[callsign]
            cs.lat, cs.lon, cs.alt = lat, lon, alt
            cs.evicted = False
            mgr.save()
        log.info("Position [%s] → (%.4f,%.4f,%.0fm)", callsign, lat, lon, alt)
        self._json({"ok": True, "callsign": callsign, "lat": lat, "lon": lon, "alt": alt})

    def _post_config(self, body):
        cfg = self.server_ref.cfg_mgr.config
        changed = []
        for k, v in body.items():
            if k in ("connected",) or not hasattr(cfg, k):
                continue
            cur = getattr(cfg, k)
            try:
                if isinstance(cur, bool):   setattr(cfg, k, bool(v))
                elif isinstance(cur, int):  setattr(cfg, k, int(v))
                elif isinstance(cur, float):setattr(cfg, k, float(v))
                else:                       setattr(cfg, k, str(v))
                changed.append(k)
            except (ValueError, TypeError):
                pass
        srv = self.server_ref.radio_server
        srv._range.max_range_km   = cfg.range_max_km
        srv._range.full_quiet_km  = cfg.range_full_quiet_km
        srv._range.power_w        = cfg.transmit_power_w
        srv._range.terrain_factor = cfg.terrain_factor
        srv._cfg.range_enabled    = cfg.range_enabled
        srv._sync_weather_state()
        self.server_ref.cfg_mgr.save()
        # Apply audio effects live to the running RadioEffects engine
        audio_keys = {'distortion','noise_floor','crackle_lvl','ptt_click_enabled','ptt_type','relay_signature',
                      'sample_rate','opus_bitrate','squelch','output_volume','input_gain','squelch_tail_enabled',
                      'weather_enabled','weather_severity','weather_type','bp_low_hz','bp_high_hz',
                      'ptt_tone_f1', 'ptt_tone_f2', 'filter_order'}
        if audio_keys & set(changed):
            radio_server = getattr(self.server_ref, 'radio_server', None)
            if radio_server and hasattr(radio_server, '_effects'):
                try:
                    radio_server._effects.update(
                        distortion          = cfg.distortion,
                        noise_floor         = cfg.noise_floor,
                        crackle_lvl         = cfg.crackle_lvl,
                        squelch             = cfg.squelch,
                        volume              = cfg.output_volume,
                        ptt_click_enabled   = cfg.ptt_click_enabled,
                        ptt_type            = cfg.ptt_type,
                        relay_signature     = cfg.relay_signature,
                        squelch_tail_enabled = cfg.squelch_tail_enabled,
                        bp_low_hz           = cfg.bp_low_hz,
                        bp_high_hz          = cfg.bp_high_hz,
                        ptt_tone_f1         = cfg.ptt_tone_f1,
                        ptt_tone_f2         = cfg.ptt_tone_f2,
                        filter_order        = cfg.filter_order
                    )
                    log.info("Effects engine updated live")
                except Exception as e:
                    log.warning("Could not update effects engine: %s", e)
            is_sim = cfg.distortion > 0 or cfg.noise_floor > 0
            mode = "SIM RADIO" if is_sim else "CPX / CLEAR"
            log.info("Audio mode → %s  dist=%.2f noise=%.3f sr=%d br=%d",
                     mode, cfg.distortion, cfg.noise_floor,
                     cfg.sample_rate, getattr(cfg,'opus_bitrate',24000))
        log.info("Config updated: %s", changed)
        self._json({"ok": True, "updated": changed})

    def _post_restart(self):
        log.info("Admin restart")
        srv = self.server_ref.radio_server
        srv.stop(); time.sleep(0.5)
        srv._build_codecs(); srv.start()
        self._json({"ok": True, "msg": "Server restarted"})

    def _post_server_start(self):
        srv = self.server_ref.radio_server
        if srv._running:
            self._json({"ok": False, "msg": "Server already running"}); return
        try:
            srv._build_codecs()
            srv.start()
            log.info("Admin: server started")
            self._json({"ok": True, "msg": "Server started"})
        except Exception as e:
            self._err(str(e))

    def _post_server_stop(self):
        srv = self.server_ref.radio_server
        if not srv._running:
            self._json({"ok": False, "msg": "Server not running"}); return
        srv.stop()
        log.info("Admin: server stopped")
        self._json({"ok": True, "msg": "Server stopped"})

    # -- AAR handlers -------------------------------------------------------------
    def _get_commsplan_download(self):
        """Export full comms plan as a JSON file for import on another server."""
        mgr = self.server_ref.cfg_mgr
        data = {
            "version":   1,
            "exported":  time.strftime("%Y-%m-%dT%H:%M:%S"),
            "exercise":  mgr.config.aar_exercise_name or "EXERCISE",
            "channels":  [
                {"id": ch.id, "name": ch.name, "frequency": ch.frequency,
                 "passphrase": ch.passphrase, "description": ch.description,
                 "enabled": ch.enabled}
                for ch in sorted(mgr.channels.values(), key=lambda c: c.id)
            ],
            "callsigns": [
                {"callsign": cs.callsign, "real_name": cs.real_name,
                 "unit": cs.unit, "role": cs.role, "channels": cs.channels,
                 "lat": cs.lat, "lon": cs.lon, "alt": cs.alt}
                for cs in sorted(mgr.callsigns.values(), key=lambda c: c.callsign)
            ],
        }
        body = json.dumps(data, indent=2).encode("utf-8")
        fname = f"commsplan_{time.strftime('%Y%m%d_%H%M%S')}.json"
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Disposition", f'attachment; filename="{fname}"')
        self._cors(); self.end_headers(); self.wfile.write(body)

    def _post_commsplan_import(self, body: dict):
        """Import a comms plan — replaces channels and callsigns."""
        if body.get("version") != 1:
            self._err("Invalid or missing version field"); return
        mgr = self.server_ref.cfg_mgr
        from radio_config import ChannelDef, CallsignDef
        errors = []
        imported_ch = 0
        imported_cs = 0

        if "channels" in body:
            mgr.channels.clear()
            for c in body["channels"]:
                try:
                    mgr.channels[int(c["id"])] = ChannelDef(
                        id=int(c["id"]), name=c.get("name",""),
                        frequency=c.get("frequency",""),
                        passphrase=c.get("passphrase",""),
                        description=c.get("description",""),
                        enabled=bool(c.get("enabled", True)),
                    )
                    imported_ch += 1
                except Exception as e:
                    errors.append(f"Channel {c.get('id')}: {e}")

        if "callsigns" in body:
            mgr.callsigns.clear()
            for c in body["callsigns"]:
                try:
                    cs_key = str(c["callsign"]).strip().upper()
                    mgr.callsigns[cs_key] = CallsignDef(
                        callsign=cs_key,
                        real_name=c.get("real_name",""),
                        unit=c.get("unit",""),
                        role=c.get("role",""),
                        channels=[int(x) for x in c.get("channels",[])],
                        lat=float(c.get("lat",0)),
                        lon=float(c.get("lon",0)),
                        alt=float(c.get("alt",0)),
                        radio_profile=c.get("radio_profile", "prc152"),
                    )
                    imported_cs += 1
                except Exception as e:
                    errors.append(f"Callsign {c.get('callsign')}: {e}")

        mgr.save()
        self.server_ref.radio_server._build_codecs()
        log.info("Comms plan imported: %d channels, %d callsigns", imported_ch, imported_cs)
        self._json({"ok": True, "channels": imported_ch,
                    "callsigns": imported_cs, "errors": errors})

    def _post_terrain(self, body):
        from radio_terrain import TerrainType, MapDataSource, get_terrain_manager
        terrain = get_terrain_manager()
        
        # We can update global config for terrain
        if "source_type" in body:
            try:
                terrain._config.source_type = MapDataSource(body["source_type"])
            except ValueError:
                pass
        
        # If mapping UI changed terrain config, save it
        # Actually radio_config.py holds TerrainMap config too, but let's sync to TerrainManager
        # We can trigger a reload or reinit
        log.info("Terrain settings updated via admin")
        self._json({"ok": True})
        
    def _post_terrain_marker(self, body):
        mgr = self.server_ref.cfg_mgr
        from radio_config import MapMarker
        if "marker_id" not in body:
            self._err("Missing marker_id")
            return
        
        m = MapMarker.from_dict(body)
        mgr.terrain_map.add_marker(m)
        mgr.save()
        self._json({"ok": True, "marker": m.to_dict()})
        
    def _delete_terrain_marker(self, mid):
        mgr = self.server_ref.cfg_mgr
        if mgr.terrain_map.remove_marker(mid):
            mgr.save()
            self._json({"ok": True})
        else:
            self._json({"ok": False, "error": "Not found"})

    def _get_aar_status(self):
        rec = self.server_ref.recorder
        if rec is None:
            self._json({"recording": False, "enabled": False,
                        "session": None, "active_tx": [],
                        "time_source": "wall", "sword_ok": False})
            return
        self._json(rec.get_status())

    def _get_sword_status(self):
        """
        Proxy /api/sword/status -> SimBridge /api/status.
        Called server-side to avoid browser CORS restrictions.
        Uses the sword_url from the recorder if set, else falls back to config.
        """
        rec = self.server_ref.recorder
        cfg = self.server_ref.cfg_mgr.config
        base_url = (rec._sword_url if rec and rec._sword_url
                    else cfg.aar_sword_url or "http://127.0.0.1:8888")
        url = base_url.rstrip("/") + "/api/status"
        try:
            import urllib.request as _ur
            with _ur.urlopen(url, timeout=3) as r:
                data = json.loads(r.read())
            self._json({"ok": True, "reachable": True, "data": data,
                        "url": base_url})
        except Exception as e:
            self._json({"ok": False, "reachable": False,
                        "error": str(e), "url": base_url})

    def _get_aar_wav(self, session_id: str, filename: str):
        """Serve a single WAV file from an AAR session folder."""
        # Security: strip any path traversal attempts
        filename = Path(filename).name   # basename only — no directory components
        if not filename.endswith(".wav"):
            self._err("Not found", 404); return
        rec = self.server_ref.recorder
        if rec is None:
            self._err("Recorder not available", 503); return
        wav_path = rec._aar_dir / session_id / filename
        if not wav_path.exists():
            self._err("WAV not found", 404); return
        try:
            data = wav_path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "audio/wav")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Accept-Ranges", "bytes")
            self._cors()
            self.end_headers()
            self.wfile.write(data)
        except Exception as e:
            self._err(str(e), 500)

    def _get_aar_sessions(self):
        rec = self.server_ref.recorder
        if rec is None:
            self._json({"sessions": []}); return
        self._json({"sessions": rec.get_session_list()})

    def _get_aar_session(self, session_id: str):
        rec = self.server_ref.recorder
        if rec is None:
            self._err("Recorder not available", 503); return
        data = rec.get_session(session_id)
        if data is None:
            self._err("Session not found", 404); return
        self._json(data)

    def _get_aar_report(self, session_id: str):
        """Generate and serve the AAR HTML report inline (opens in browser)."""
        rec = self.server_ref.recorder
        if rec is None:
            self._err("Recorder not available", 503); return
        template_path = _find_asset("Milradio_aar_v2.html")
        # Pass the server base URL so the report can fetch WAV files
        server_url = f"http://{self.headers.get('Host', 'localhost')}"
        html = rec.generate_aar_html(session_id, template_path,
                                     server_url=server_url)
        if html is None:
            self._err("Session not found or template missing", 404); return
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _get_aar_download(self, session_id: str):
        """Bundle a full AAR session (manifest + WAVs) into a ZIP for download."""
        rec = self.server_ref.recorder
        if rec is None:
            self._err("Recorder not available", 503); return
        
        session_path = rec._aar_dir / session_id
        if not session_path.exists():
            self._err("Session folder not found", 404); return
            
        import io, zipfile
        buf = io.BytesIO()
        try:
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
                # Add manifest
                m_path = session_path / "manifest.json"
                if m_path.exists():
                    z.write(m_path, "manifest.json")
                
                # Add all WAVs
                for f in session_path.glob("*.wav"):
                    z.write(f, f.name)
                
                # Add portable HTML report for offline viewing
                try:
                    t_path = _find_asset("Milradio_aar_v2.html")
                    if t_path.exists():
                        # Generate HTML with server_url=None for relative paths
                        html = rec.generate_aar_html(session_id, t_path, server_url=None)
                        if html:
                            z.writestr("offline_aar_report.html", html)
                except Exception as e:
                    log.warning("Could not include offline report in ZIP: %s", e)
            
            data = buf.getvalue()
            fname = f"aar_{session_id}.zip"
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Content-Disposition", f'attachment; filename="{fname}"')
            self._cors()
            self.end_headers()
            self.wfile.write(data)
        except Exception as e:
            self._err(f"Zip failed: {e}", 500)

    def _post_aar_start(self, body: dict):
        rec = self.server_ref.recorder
        if rec is None:
            self._err("Recorder not available", 503); return
        cfg          = self.server_ref.cfg_mgr.config
        exercise     = body.get("exercise", cfg.aar_exercise_name or "EXERCISE")
        time_source  = body.get("time_source", cfg.aar_time_source or "wall")
        sword_url    = body.get("sword_url",
                                cfg.aar_sword_url if cfg.sword_enabled else None)
        h_hour_wall  = None
        if time_source == "manual" and body.get("h_hour"):
            h_hour_wall = _parse_hhmmss(body["h_hour"])

        sid = rec.start_session(
            exercise    = exercise,
            time_source = time_source,
            h_hour_wall = h_hour_wall,
            sword_url   = sword_url if time_source == "sword" else None,
        )
        log.info("AAR session started: %s  exercise=%s  time_source=%s",
                 sid, exercise, time_source)
        self._json({"ok": True, "session_id": sid})

    def _post_aar_stop(self):
        rec = self.server_ref.recorder
        if rec is None:
            self._err("Recorder not available", 503); return
        sid = rec.stop_session()
        if sid is None:
            self._err("No active session"); return
        log.info("AAR session stopped: %s", sid)
        self._json({"ok": True, "session_id": sid})

    def _post_aar_hhour(self, body: dict):
        """Update H-hour on the running session (manual or sword re-sync)."""
        rec = self.server_ref.recorder
        if rec is None:
            self._err("Recorder not available", 503); return
        source = body.get("source", "manual")

        if source == "now":
            lt  = time.localtime()
            hh  = lt.tm_hour * 3600 + lt.tm_min * 60 + lt.tm_sec
            rec.set_h_hour(hh, "manual")
            self._json({"ok": True,
                        "h_hour_str": f"{lt.tm_hour:02d}:{lt.tm_min:02d}:{lt.tm_sec:02d}L"})

        elif source == "sword":
            sword_url = body.get("sword_url", rec._sword_url or "")
            if not sword_url:
                self._err("Provide sword_url"); return

            # Store URL and mode regardless of whether recording is active
            rec._sword_url   = sword_url
            rec._time_source = "sword"

            # Immediate synchronous probe — sets sword_ok right now so the UI
            # doesn't have to wait up to 30s for the background poll loop
            import urllib.request as _ur
            probe_url     = sword_url.rstrip("/") + "/api/status"
            sim_clock_str = ""
            try:
                with _ur.urlopen(probe_url, timeout=3) as resp:
                    data = json.loads(resp.read())
                sim_clock_str = data.get("sim_clock") or ""
                # Derive H-hour from sim_clock if present
                if sim_clock_str:
                    from radio_recorder import _parse_sim_clock
                    wall_secs = _parse_sim_clock(sim_clock_str)
                    if wall_secs is not None:
                        rec.set_h_hour(wall_secs, "sword")
                        # Store sim clock reading so _ex_time_str can interpolate
                        # and admin UI can show live ticking time immediately
                        rec._sword_sim_secs         = wall_secs
                        rec._sword_sim_received_at  = time.time()
                rec._sword_ok = True
                log.info("SWORD probe OK: sim_clock=%s", sim_clock_str)
            except Exception as e:
                rec._sword_ok = False
                log.warning("SWORD probe failed (%s): %s", sword_url, e)

            # Start (or restart) the 30s background poll regardless of running state
            rec._start_sword_sync()

            self._json({
                "ok":        True,
                "sword_ok":  rec._sword_ok,
                "sim_clock": sim_clock_str,
                "h_hour_str": _secs_to_hhmmss(rec._h_hour_wall) + "L",
                "msg": f"SWORD {'connected' if rec._sword_ok else 'unreachable'}: {sword_url}",
            })

        elif body.get("h_hour"):
            hh = _parse_hhmmss(body["h_hour"])
            rec.set_h_hour(hh, "manual")
            self._json({"ok": True, "h_hour_str": body["h_hour"]})

        else:
            self._err("Provide h_hour (HH:MM:SS) or source=now|sword")

    def _post_client_channel(self, callsign: str, body: dict):
        """Force change channel of a client remotely."""
        try:
            channel = int(body.get("channel", -1))
        except (ValueError, TypeError):
            self._err("Invalid channel value", 400)
            return
        
        ok = self.server_ref.radio_server.force_client_channel(callsign, channel)
        if ok:
            self._json({"ok": True, "callsign": callsign, "channel": channel})
        else:
            self._err(f"Could not force channel for client {callsign} (either offline or manual lockout active)", 400)

    def _post_aar_marker(self, body: dict):
        """Add custom annotation/event marker to AAR session."""
        rec = self.server_ref.recorder
        if rec is None:
            self._err("Recorder not available", 503)
            return
        label = body.get("label", "MARKER").upper()
        details = body.get("details", "")
        evt = rec.add_event_marker(label, details)
        if evt:
            self._json({"ok": True, "event": evt})
        else:
            self._err("No active AAR recording session", 400)

    def _post_weather_scenario(self, body: dict):
        """Apply macro scenario weather and EMI settings."""
        cfg = self.server_ref.cfg_mgr.config
        profile = body.get("profile", "clear").lower()
        
        changed = []
        if profile == "clear":
            cfg.weather_enabled = False
            cfg.temp_inversion_enabled = False
            cfg.iono_enabled = False
            cfg.emi_enabled = False
            cfg.gps_degraded = False
            cfg.gps_error_km = 0.0
            cfg.satcom_degraded = False
            cfg.satcom_latency_ms = 50.0
            changed = ["weather_enabled", "temp_inversion_enabled", "iono_enabled", "emi_enabled", "gps_degraded", "gps_error_km", "satcom_degraded", "satcom_latency_ms"]
        elif profile == "thunderstorm":
            cfg.weather_enabled = True
            cfg.weather_type = "storm"
            cfg.weather_severity = 0.9
            cfg.weather_humidity = 0.9
            cfg.temp_inversion_enabled = True
            cfg.temp_inversion_strength = 0.7
            cfg.temp_inversion_echo_ms = 150.0
            cfg.iono_enabled = True
            cfg.iono_condition = "storm"
            cfg.iono_absorption = 0.8
            cfg.emi_enabled = True
            cfg.emi_level = 0.7
            cfg.emi_type = "burst"
            changed = ["weather_enabled", "weather_type", "weather_severity", "weather_humidity", "temp_inversion_enabled", "temp_inversion_strength", "temp_inversion_echo_ms", "iono_enabled", "iono_condition", "iono_absorption", "emi_enabled", "emi_level", "emi_type"]
        elif profile == "dust_storm":
            cfg.weather_enabled = True
            cfg.weather_type = "fog"
            cfg.weather_severity = 0.8
            cfg.weather_humidity = 0.7
            cfg.temp_inversion_enabled = False
            cfg.iono_enabled = False
            cfg.emi_enabled = True
            cfg.emi_level = 0.4
            cfg.emi_type = "white"
            changed = ["weather_enabled", "weather_type", "weather_severity", "weather_humidity", "temp_inversion_enabled", "iono_enabled", "emi_enabled", "emi_level", "emi_type"]
        elif profile == "solar_flare":
            cfg.weather_enabled = False
            cfg.temp_inversion_enabled = False
            cfg.iono_enabled = True
            cfg.iono_condition = "blackout"
            cfg.iono_absorption = 1.0
            cfg.emi_enabled = False
            cfg.gps_degraded = True
            cfg.gps_error_km = 8.5
            cfg.satcom_degraded = True
            cfg.satcom_latency_ms = 950.0
            changed = ["weather_enabled", "temp_inversion_enabled", "iono_enabled", "iono_condition", "iono_absorption", "emi_enabled", "gps_degraded", "gps_error_km", "satcom_degraded", "satcom_latency_ms"]
        else:
            self._err(f"Unknown scenario profile: {profile}", 400)
            return

        # Apply atmosphere weather settings live
        radio_server = getattr(self.server_ref, 'radio_server', None)
        if radio_server:
            if hasattr(radio_server, '_effects'):
                try:
                    radio_server._effects.update(
                        weather_enabled         = cfg.weather_enabled,
                        weather_severity        = cfg.weather_severity,
                        weather_type            = cfg.weather_type,
                        weather_humidity        = cfg.weather_humidity,
                        temp_inversion_enabled  = cfg.temp_inversion_enabled,
                        temp_inversion_strength = cfg.temp_inversion_strength,
                        temp_inversion_echo_ms  = cfg.temp_inversion_echo_ms,
                        iono_enabled            = cfg.iono_enabled,
                        iono_condition          = cfg.iono_condition,
                        iono_fading             = cfg.iono_fading,
                        iono_flutter            = cfg.iono_flutter,
                        iono_absorption         = cfg.iono_absorption,
                        emi_enabled             = cfg.emi_enabled,
                        emi_level               = cfg.emi_level,
                        emi_type                = cfg.emi_type,
                    )
                except Exception as e:
                    log.warning("Could not update scenario DSP effects: %s", e)
            if hasattr(radio_server, '_sync_weather_state'):
                try:
                    radio_server._sync_weather_state()
                except Exception as e:
                    log.warning("Could not sync scenario weather propagation: %s", e)
            # Sync infrastructure (SATCOM/GPS settings)
            if hasattr(radio_server, 'infra'):
                try:
                    radio_server.infra.satcom.enabled = cfg.satcom_enabled
                    radio_server.infra.satcom.latency_ms = cfg.satcom_latency_ms
                    radio_server.infra.satcom.degraded = cfg.satcom_degraded
                    radio_server.infra.gps.enabled = cfg.gps_enabled
                    radio_server.infra.gps.degraded = cfg.gps_degraded
                    radio_server.infra.gps.error_km = cfg.gps_error_km
                    radio_server._save_infra_to_config()
                except Exception as e:
                    log.warning("Could not sync scenario infrastructure: %s", e)

        self.server_ref.cfg_mgr.save()
        self._json({"ok": True, "profile": profile, "updated": changed})

    def _get_aar_playback_stream(self):
        """SSE stream for AAR playback control loopback (browser listens to this)."""
        q = queue.Queue(maxsize=100)
        with _aar_playback_lock:
            _aar_playback_listeners.append(q)
        
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self._cors()
        self.end_headers()
        
        try:
            while self.server_ref._running:
                try:
                    data = q.get(timeout=5.0)
                    self.wfile.write(f"data: {json.dumps(data)}\n\n".encode("utf-8"))
                    self.wfile.flush()
                except queue.Empty:
                    # Keepalive
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
        except (ConnectionError, BrokenPipeError):
            pass
        finally:
            with _aar_playback_lock:
                if q in _aar_playback_listeners:
                    _aar_playback_listeners.remove(q)

    def _post_aar_playback_control(self, body: dict):
        """Post a command to AAR playback (Stream Deck -> Server -> Browser)."""
        # Broadcast command to all SSE listeners
        with _aar_playback_lock:
            for q in _aar_playback_listeners:
                try:
                    q.put_nowait(body)
                except queue.Full:
                    pass
        self._json({"ok": True, "command_sent": body})

    def _post_aar_playback_state(self, body: dict):
        """Browser posts current playhead state here."""
        with _aar_playback_lock:
            for k in _aar_playback_state:
                if k in body:
                    _aar_playback_state[k] = body[k]
        self._json({"ok": True})

    def _get_aar_playback_state_api(self):
        """Stream Deck queries current state here."""
        with _aar_playback_lock:
            state = dict(_aar_playback_state)
        self._json(state)

    # ── Day / Night endpoints ──────────────────────────────────────────────────

    def _get_daynight(self):
        cfg = self.server_ref.cfg_mgr.config
        import datetime as _dt
        wall_hour = _dt.datetime.now().hour + _dt.datetime.now().minute / 60.0
        self._json({
            "day_night_enabled":  cfg.day_night_enabled,
            "day_night_hour":     cfg.day_night_hour,
            "day_night_auto":     cfg.day_night_auto,
            "day_night_latitude": cfg.day_night_latitude,
            "wall_hour":          round(wall_hour, 2),
        })

    def _post_daynight(self, body: dict):
        cfg = self.server_ref.cfg_mgr.config
        changed = []
        if "day_night_enabled"  in body: cfg.day_night_enabled  =  bool(body["day_night_enabled"]);  changed.append("day_night_enabled")
        if "day_night_hour"     in body: cfg.day_night_hour      = float(body["day_night_hour"]);      changed.append("day_night_hour")
        if "day_night_auto"     in body: cfg.day_night_auto      =  bool(body["day_night_auto"]);      changed.append("day_night_auto")
        if "day_night_latitude" in body: cfg.day_night_latitude  = float(body["day_night_latitude"]);  changed.append("day_night_latitude")
        self.server_ref.cfg_mgr.save()
        self._json({"ok": True, "updated": changed})

    # ── EMI endpoints ───────────────────────────────────────────────────────────

    def _get_emi(self):
        cfg = self.server_ref.cfg_mgr.config
        self._json({
            "emi_enabled": cfg.emi_enabled,
            "emi_level":   cfg.emi_level,
            "emi_type":    cfg.emi_type,
        })

    def _post_emi(self, body: dict):
        cfg = self.server_ref.cfg_mgr.config
        changed = []
        if "emi_enabled" in body: cfg.emi_enabled =  bool(body["emi_enabled"]); changed.append("emi_enabled")
        if "emi_level"   in body: cfg.emi_level   = float(max(0.0, min(1.0, body["emi_level"]))); changed.append("emi_level")
        if "emi_type"    in body:
            et = str(body["emi_type"])
            if et not in ("white", "hum", "burst"): self._err(f"Invalid emi_type: {et}"); return
            cfg.emi_type = et; changed.append("emi_type")
        radio_server = getattr(self.server_ref, 'radio_server', None)
        if radio_server and hasattr(radio_server, '_effects'):
            try:
                radio_server._effects.update(
                    emi_enabled = cfg.emi_enabled,
                    emi_level   = cfg.emi_level,
                    emi_type    = cfg.emi_type,
                )
                log.info("EMI updated: enabled=%s level=%.2f type=%s",
                         cfg.emi_enabled, cfg.emi_level, cfg.emi_type)
            except Exception as e:
                log.warning("EMI effects update failed: %s", e)
        self.server_ref.cfg_mgr.save()
        self._json({"ok": True, "updated": changed})

    # ── Weather endpoints ──────────────────────────────────────────────────────

    def _get_weather(self):
        """GET /api/weather — return current weather & atmosphere state."""
        cfg = self.server_ref.cfg_mgr.config
        self._json({
            "weather_enabled":          cfg.weather_enabled,
            "weather_severity":         cfg.weather_severity,
            "weather_type":             cfg.weather_type,
            "weather_humidity":         cfg.weather_humidity,
            "temp_inversion_enabled":   cfg.temp_inversion_enabled,
            "temp_inversion_strength":  cfg.temp_inversion_strength,
            "temp_inversion_echo_ms":   cfg.temp_inversion_echo_ms,
            "iono_enabled":             cfg.iono_enabled,
            "iono_condition":           cfg.iono_condition,
            "iono_fading":              cfg.iono_fading,
            "iono_flutter":             cfg.iono_flutter,
            "iono_absorption":          cfg.iono_absorption,
        })

    def _post_weather(self, body: dict):
        """POST /api/weather — update weather & atmosphere settings, apply live."""
        cfg = self.server_ref.cfg_mgr.config
        changed = []
        # Simple float/bool fields
        _float_fields = [
            "weather_severity", "weather_humidity",
            "temp_inversion_strength", "temp_inversion_echo_ms",
            "iono_fading", "iono_flutter", "iono_absorption",
        ]
        _bool_fields = ["weather_enabled", "temp_inversion_enabled", "iono_enabled"]
        _valid_types = {"rain", "storm", "interference", "fog"}
        _valid_conds = {"quiet", "disturbed", "storm", "blackout"}
        for f in _bool_fields:
            if f in body:
                setattr(cfg, f, bool(body[f])); changed.append(f)
        for f in _float_fields:
            if f in body:
                setattr(cfg, f, float(body[f])); changed.append(f)
        if "weather_type" in body:
            wtype = str(body["weather_type"])
            if wtype not in _valid_types:
                self._err(f"Invalid weather_type: {wtype}"); return
            cfg.weather_type = wtype; changed.append("weather_type")
        if "iono_condition" in body:
            cond = str(body["iono_condition"])
            if cond not in _valid_conds:
                self._err(f"Invalid iono_condition: {cond}"); return
            cfg.iono_condition = cond; changed.append("iono_condition")
        # Apply all changes live to RadioEffects engine
        radio_server = getattr(self.server_ref, 'radio_server', None)
        if radio_server:
            # Update DSP effects if present
            if hasattr(radio_server, '_effects'):
                try:
                    radio_server._effects.update(
                        weather_enabled         = cfg.weather_enabled,
                        weather_severity        = cfg.weather_severity,
                        weather_type            = cfg.weather_type,
                        weather_humidity        = cfg.weather_humidity,
                        temp_inversion_enabled  = cfg.temp_inversion_enabled,
                        temp_inversion_strength = cfg.temp_inversion_strength,
                        temp_inversion_echo_ms  = cfg.temp_inversion_echo_ms,
                        iono_enabled            = cfg.iono_enabled,
                        iono_condition          = cfg.iono_condition,
                        iono_fading             = cfg.iono_fading,
                        iono_flutter            = cfg.iono_flutter,
                        iono_absorption         = cfg.iono_absorption,
                    )
                except Exception as e:
                    log.warning("Could not update DSP atmosphere effects: %s", e)
            
            # Sync to propagation engines (Phase 4)
            if hasattr(radio_server, '_sync_weather_state'):
                try:
                    radio_server._sync_weather_state()
                except Exception as e:
                    log.warning("Could not sync propagation weather state: %s", e)

        log.info("Weather & Atmosphere updated: precip=%s/%s sev=%.2f hum=%.2f inv=%s iono=%s/%s",
                 cfg.weather_enabled, cfg.weather_type, cfg.weather_severity,
                 cfg.weather_humidity, cfg.temp_inversion_enabled,
                 cfg.iono_enabled, cfg.iono_condition)
        self.server_ref.cfg_mgr.save()
        self._json({"ok": True, "updated": changed})

    # ── Signal Quality endpoints ───────────────────────────────────────────────

    def _get_signal(self):
        """GET /api/signal — per-client signal quality metrics."""
        srv = self.server_ref.radio_server
        if not hasattr(srv, 'get_signal_quality'):
            self._json({"clients": []}); return
        clients = srv.get_signal_quality()
        # Also annotate with current path info from infra if available
        if hasattr(srv, 'infra'):
            path_map = {}
            with srv._clients_lock:
                client_list = list(srv._clients.values())
            import itertools
            for a, b in itertools.combinations(client_list, 2):
                if a.position_fresh() and b.position_fresh() and srv._cfg.range_enabled:
                    try:
                        result = srv.infra.compute_path(
                            a.lat, a.lon, b.lat, b.lon, a.channel,
                            range_fn=srv._range.signal_from_positions,
                        )
                        path_map[b.callsign] = result.link_type
                        path_map[a.callsign] = result.link_type
                    except Exception:
                        pass
            for c in clients:
                c["link_type"] = path_map.get(c["callsign"], "direct")
        self._json({"clients": clients})

    # ── Routing endpoints ──────────────────────────────────────────────────────

    def _get_routing(self):
        """GET /api/routing — current routing mode and settings."""
        srv = self.server_ref.radio_server
        if not hasattr(srv, 'infra'):
            self._json({"mode": "auto", "min_signal": 0.15, "prefer_low_latency": False})
            return
        self._json({
            "mode":                  srv.infra.routing_mode,
            "min_signal":            srv.infra.routing_min_signal,
            "prefer_low_latency":    srv.infra.routing_prefer_low_latency,
        })

    def _post_routing(self, body: dict):
        """POST /api/routing — update routing mode and settings."""
        srv = self.server_ref.radio_server
        if not hasattr(srv, 'infra'):
            self._err("Infrastructure not available", 503); return
        valid_modes = {"direct", "assisted", "hybrid", "auto"}
        changed = []
        if "mode" in body:
            mode = str(body["mode"])
            if mode not in valid_modes:
                self._err(f"Invalid routing mode: {mode}. Valid: {valid_modes}"); return
            srv.infra.routing_mode = mode; changed.append("mode")
        if "min_signal" in body:
            srv.infra.routing_min_signal = float(max(0.0, min(1.0, body["min_signal"])))
            changed.append("min_signal")
        if "prefer_low_latency" in body:
            srv.infra.routing_prefer_low_latency = bool(body["prefer_low_latency"])
            changed.append("prefer_low_latency")
        srv._save_infra_to_config()
        self.server_ref.cfg_mgr.save()
        log.info("Routing updated: mode=%s min_signal=%.2f low_lat=%s",
                 srv.infra.routing_mode, srv.infra.routing_min_signal,
                 srv.infra.routing_prefer_low_latency)
        self._json({"ok": True, "updated": changed,
                    "mode": srv.infra.routing_mode})

    def _get_routing_analytics(self):
        """GET /api/routing/analytics — path decision log and statistics."""
        srv = self.server_ref.radio_server
        if not hasattr(srv, 'infra'):
            self._json({"total": 0, "by_type": {}, "avg_signal": 0, "recent": []})
            return
        self._json(srv.infra.get_path_analytics())

    # ── Infrastructure endpoints ───────────────────────────────────────────────

    def _get_infra(self):
        """GET /api/infra — return full infrastructure state."""
        srv = self.server_ref.radio_server
        if not hasattr(srv, 'infra'):
            self._json({"retrans_nodes": [], "satcom": {}, "gps": {}})
            return
        self._json(srv.infra.get_status())

    def _get_infra_status(self):
        """GET /api/infra/status — live path status for connected client pairs."""
        import itertools
        srv = self.server_ref.radio_server
        if not hasattr(srv, 'infra'):
            self._json({"paths": []}); return
        paths = []
        clients = []
        with srv._clients_lock:
            clients.extend(list(srv._clients.values()))

        # Include configured callsigns (stored positions) even if not active on UDP
        mgr = self.server_ref.cfg_mgr
        for cs in mgr.callsigns.values():
            if cs.callsign in srv._clients:
                continue
            if not (cs.lat or cs.lon):
                continue
            channel = cs.channels[0] if cs.channels else -1
            clients.append(SimpleNamespace(
                callsign=cs.callsign, lat=cs.lat, lon=cs.lon,
                channel=channel,
                position_fresh=lambda: True
            ))

        for a, b in itertools.combinations(clients, 2):
            if not (a.position_fresh() and b.position_fresh()):
                continue
            try:
                channel = a.channel if a.channel == b.channel else a.channel
                result = srv.infra.compute_path(
                    a.lat, a.lon, b.lat, b.lon, channel,
                    range_fn=srv._range.signal_from_positions,
                )
                paths.append({
                    "from":       a.callsign,
                    "to":         b.callsign,
                    "signal":     round(result.signal, 3),
                    "latency_ms": result.latency_ms,
                    "link_type":  result.link_type,
                    "hops":       result.hops,
                    "notes":      result.notes,
                })
            except Exception:
                pass
        self._json({"paths": paths})

    def _post_infra(self, body: dict):
        """POST /api/infra — update SATCOM and GPS settings."""
        srv = self.server_ref.radio_server
        if not hasattr(srv, 'infra'):
            self._err("Infrastructure not available", 503); return
        import json as _json
        changed = []
        satcom = body.get("satcom", {})
        if satcom:
            if "enabled"    in satcom: srv.infra.satcom.enabled    =  bool(satcom["enabled"]);    changed.append("satcom.enabled")
            if "latency_ms" in satcom: srv.infra.satcom.latency_ms = float(satcom["latency_ms"]); changed.append("satcom.latency_ms")
            if "channels"   in satcom: srv.infra.satcom.channels   =  list(satcom["channels"]);   changed.append("satcom.channels")
            if "degraded"   in satcom: srv.infra.satcom.degraded   =  bool(satcom["degraded"]);   changed.append("satcom.degraded")
            if "uplink_strength" in satcom:
                srv.infra.satcom.uplink_strength = float(satcom["uplink_strength"])
                changed.append("satcom.uplink_strength")
        gps = body.get("gps", {})
        if gps:
            if "enabled"    in gps: srv.infra.gps.enabled    =  bool(gps["enabled"]);    changed.append("gps.enabled")
            if "degraded"   in gps: srv.infra.gps.degraded   =  bool(gps["degraded"]);   changed.append("gps.degraded")
            if "accuracy_m" in gps: srv.infra.gps.accuracy_m = float(gps["accuracy_m"]); changed.append("gps.accuracy_m")
            if "error_km"   in gps: srv.infra.gps.error_km   = float(gps["error_km"]);   changed.append("gps.error_km")
        srv._save_infra_to_config()
        self.server_ref.cfg_mgr.save()
        log.info("Infrastructure updated: %s", changed)
        self._json({"ok": True, "updated": changed})

    def _post_infra_node(self, body: dict):
        """POST /api/infra/node — add or update a retrans node."""
        from radio_infra import RetransNode
        srv = self.server_ref.radio_server
        if not hasattr(srv, 'infra'):
            self._err("Infrastructure not available", 503); return
        node_id = body.get("node_id", "").strip()
        if not node_id:
            self._err("node_id required"); return
        existing = srv.infra.retrans_nodes.get(node_id)
        if existing:
            # Update existing
            for k in ("name","lat","lon","alt_m","channels","enabled",
                      "power_w","latency_ms","degraded","degraded_pct"):
                if k in body:
                    setattr(existing, k, body[k])
            action = "updated"
        else:
            # Create new
            try:
                node = RetransNode(
                    node_id      = node_id,
                    name         = body.get("name", node_id),
                    lat          = float(body.get("lat", 0)),
                    lon          = float(body.get("lon", 0)),
                    alt_m        = float(body.get("alt_m", 0)),
                    channels     = list(body.get("channels", [])),
                    enabled      = bool(body.get("enabled", True)),
                    power_w      = float(body.get("power_w", 5.0)),
                    latency_ms   = float(body.get("latency_ms", 20.0)),
                )
                srv.infra.add_node(node)
                action = "created"
            except Exception as e:
                self._err(f"Invalid node data: {e}"); return
        srv._save_infra_to_config()
        self.server_ref.cfg_mgr.save()
        self._json({"ok": True, "action": action, "node_id": node_id})

    def _delete_infra_node(self, node_id: str):
        """DELETE /api/infra/node/<id>/delete — remove a retrans node."""
        srv = self.server_ref.radio_server
        if not hasattr(srv, 'infra'):
            self._err("Infrastructure not available", 503); return
        ok = srv.infra.remove_node(node_id)
        if ok:
            srv._save_infra_to_config()
            self.server_ref.cfg_mgr.save()
        self._json({"ok": ok, "node_id": node_id})

    def _get_mgrs(self):
        """GET /api/mgrs?lat=X&lon=Y  or  ?mgrs=XXXXX  — convert coordinates."""
        import urllib.parse as _up
        from radio_infra import latlon_to_mgrs, mgrs_to_latlon
        qs = _up.parse_qs(_up.urlparse(self.path).query)
        try:
            if "mgrs" in qs:
                mgrs_str = qs["mgrs"][0].strip()
                lat, lon = mgrs_to_latlon(mgrs_str)
                self._json({"mgrs": mgrs_str, "lat": round(lat,6), "lon": round(lon,6)})
            elif "lat" in qs and "lon" in qs:
                lat = float(qs["lat"][0])
                lon = float(qs["lon"][0])
                mgrs_str = latlon_to_mgrs(lat, lon)
                self._json({"lat": lat, "lon": lon, "mgrs": mgrs_str})
            else:
                self._err("Provide lat+lon or mgrs parameter")
        except Exception as e:
            self._err(str(e))

    def _post_ew_jam(self, body: dict):
        """POST /api/ew/jam — convenience wrapper for jamming effect.
        Body: { channels:[], callsigns:[], intensity:0-1, label:"" }
        intensity 0=off, 1=complete denial. Omit channels/callsigns for area jam.
        """
        srv = self.server_ref.radio_server
        if not hasattr(srv, 'ew_engine'):
            self._err("EW engine not available", 503); return
        intensity = float(max(0.0, min(1.0, body.get("intensity", 1.0))))
        channels  = body.get("channels", [])
        callsigns = body.get("callsigns", [])
        label     = body.get("label", "")
        action    = body.get("action", "apply")   # apply | clear_all
        if action == "clear_all":
            # Clear only jam effects
            with srv.ew_engine._lock:
                to_clear = [eid for eid, e in srv.ew_engine._active.items()
                            if e["type"] == "jam"]
            for eid in to_clear:
                srv.ew_engine.clear_effect(eid)
            self._json({"ok": True, "msg": f"Cleared {len(to_clear)} jam effect(s)"})
            return
        params = {"intensity": intensity}
        if channels:
            params["channels"] = channels
        if callsigns:
            params["callsigns"] = callsigns
        # If no channels or callsigns are specified, we do not restrict the params,
        # which targets all channels dynamically in the EWEngine.
        jam_type = "emcon" if intensity >= 1.0 and callsigns else "jam"
        eid = srv.ew_engine.apply_effect(jam_type, params, label or f"JAM {intensity:.0%}")
        log.info("Jamming applied: type=%s channels=%s callsigns=%s intensity=%.2f",
                 jam_type, channels, callsigns, intensity)
        self._json({"ok": True, "effect_id": eid, "type": jam_type, "params": params})

    def _get_ew_status(self):
        srv = self.server_ref.radio_server
        if not hasattr(srv, 'ew_engine'):
            self._json({"active": [], "count": 0, "log": []})
            return
        self._json(srv.ew_engine.get_status())

    def _post_ew_effect(self, body: dict):
        srv = self.server_ref.radio_server
        if not hasattr(srv, 'ew_engine'):
            self._err("EW engine not available", 503); return
        effect_type = body.get("type", "")
        valid_types = {"jam", "ew", "cyber", "weather", "terrain", "emcon"}
        if effect_type not in valid_types:
            self._err(f"Unknown effect type: {effect_type}"); return
        params = body.get("params", {})
        label  = body.get("label", effect_type.upper())
        eid = srv.ew_engine.apply_effect(effect_type, params, label)
        self._json({"ok": True, "effect_id": eid,
                    "msg": f"EW effect applied: {effect_type} ({label})"})

    def _post_ew_clear(self, body: dict):
        srv = self.server_ref.radio_server
        if not hasattr(srv, 'ew_engine'):
            self._err("EW engine not available", 503); return
        eid = body.get("effect_id", "")
        if eid == "all" or not eid:
            srv.ew_engine.clear_all()
            self._json({"ok": True, "msg": "All EW effects cleared"})
        else:
            ok = srv.ew_engine.clear_effect(eid)
            self._json({"ok": ok, "msg": f"Effect {eid} {'cleared' if ok else 'not found'}"})

    def _get_planner_load(self):
        if PLANNER_STORE.exists():
            try:
                data = json.loads(PLANNER_STORE.read_text('utf-8'))
                self._json(data)
                return
            except Exception as e:
                log.error("Failed to load planner data: %s", e)
        self._json({})

    def _post_planner_save(self, body):
        try:
            PLANNER_STORE.write_text(json.dumps(body, indent=2), 'utf-8')
            self._json({"ok": True})
        except Exception as e:
            self._err(str(e))

    def _post_path_profile(self, body):
        try:
            from radio_terrain import get_terrain_manager
            tm = get_terrain_manager()
            lat1 = float(body.get("lat1", 0.0))
            lon1 = float(body.get("lng1", 0.0))
            alt1 = float(body.get("alt1", 2.0))
            lat2 = float(body.get("lat2", 0.0))
            lon2 = float(body.get("lng2", 0.0))
            alt2 = float(body.get("alt2", 2.0))
            
            # Environment Sandbox
            env = body.get("env", {})
            online = bool(env.get("online_elevation", False))

            
            # Get Absolute Elevations (AGL to AMSL)
            g1 = tm.get_elevation_highres(lat1, lon1, fallback_online=online) or 0.0
            g2 = tm.get_elevation_highres(lat2, lon2, fallback_online=online) or 0.0
            
            profile, source = tm.get_terrain_profile(lat1, lon1, g1 + alt1, lat2, lon2, g2 + alt2, fallback_online=online)
            self._json({"ok": True, "profile": profile, "source": source})
        except Exception as e:
            self._err(str(e))

    def _post_planner_simulate(self, body):
        try:
            nodes = body.get("nodes", [])
            nets = body.get("nets", [])
            links = []
            
            # Resolve profiles dynamically from the ConfigManager
            mgr = self.server_ref.cfg_mgr
            profiles = {}
            for pid, pdef in mgr.profiles.items():
                profiles[pid] = {
                    'power': pdef.tx_power_w,
                    'max_range': pdef.max_range_km,
                    'full_quiet': pdef.full_quiet_km,
                    'alt': pdef.alt_m
                }
            # Fallbacks for standard categories to support client sandbox/legacy modes
            for category, defaults in {
                'handheld': {'power': 5.0,  'max_range': 12.0, 'full_quiet': 3.0,  'alt': 1.6},
                'manpack':  {'power': 20.0, 'max_range': 25.0, 'full_quiet': 8.0,  'alt': 2.0},
                'vehicular':{'power': 50.0, 'max_range': 60.0, 'full_quiet': 20.0, 'alt': 3.5},
                'retrans':  {'power': 50.0, 'max_range': 120.0,'full_quiet': 50.0, 'alt': 12.0}
            }.items():
                if category not in profiles:
                    profiles[category] = defaults
            
            # Environment Sandbox
            env = body.get("env", {})
            env_severity = float(env.get("severity", 0.0))
            env_rain = float(env.get("rain", 0.0))
            env_time = int(env.get("time", 12))
            env_storm = bool(env.get("storm", False))
            env_urban = bool(env.get("urban", False))
            env_ionosphere = bool(env.get("ionosphere", False))
            env_online = bool(env.get("online_elevation", False))
            
            # 0. Global Terrain Config
            tm = get_terrain_manager()

            
            # Compute terrain factor
            terrain_factor = 1.0
            if env_urban: terrain_factor *= 0.3
            
            # Night/Day Factor for VHF/UHF
            is_night = (env_time >= 20 or env_time <= 5)
            if is_night: terrain_factor *= 0.85
            
            # 1. Create a PRIVATE Infrastructure Sandbox
            sim_infra = InfrastructureManager()
            sim_infra.routing_mode = "assisted"
            sim_infra.online_fallback = env_online
            
            # 2. Local RangeModel
            rm = RangeModel(
                max_range_km=25.0, 
                full_quiet_km=5.0,
                power_w=5.0,
                terrain_factor=terrain_factor
            )
            sim_infra._range_fn = rm.signal_from_positions
            
            # Apply weather
            rm.weather_enabled = (env_severity > 0 or env_rain > 0 or env_storm)
            rm.weather_severity = max(env_severity, env_rain)
            rm.weather_humidity = env_rain * 0.8
            rm.weather_type = "storm" if env_storm else "rain"
            
            # 3. Register all planning units
            for n in nodes:
                is_relay = (n.get("type") in ("retrans", "vehicular"))
                prof = profiles.get(n.get("type", "handheld"), profiles['handheld'])
                if is_relay:
                    rn = RetransNode(
                        node_id=str(n["id"]), name=str(n.get("callsign", "Relay")),
                        lat=float(n["lat"]), lon=float(n["lng"]),
                        alt_m=prof['alt'], power_w=prof['power'], enabled=not bool(n.get("emcon", False))
                    )
                    sim_infra.add_node(rn)

            # Jamming Mitigation helper
            threat_nodes = [n for n in nodes if n.get("type") in ("jammer", "emi")]
            threat_zones = []
            for t in threat_nodes:
                radius = 15.0 if t["type"] == "jammer" else 30.0
                # Rain reduces jammer effectiveness slightly
                radius *= (1.0 - env_rain * 0.2)
                threat_zones.append({"lat": float(t["lat"]), "lng": float(t["lng"]), "type": t["type"], "radius_km": radius})
            
            def pt_to_seg_km(j_lat, j_lng, a_lat, a_lng, b_lat, b_lng):
                cos_lat = math.cos(math.radians(j_lat))
                def to_km(lat, lng): return (lat * 111.0, lng * 111.0 * cos_lat)
                P0, P1, P2 = to_km(j_lat, j_lng), to_km(a_lat, a_lng), to_km(b_lat, b_lng)
                L2 = (P2[0]-P1[0])**2 + (P2[1]-P1[1])**2
                if L2 == 0: return math.hypot(P0[0]-P1[0], P0[1]-P1[1])
                t = max(0, min(1, ((P0[0]-P1[0])*(P2[0]-P1[0]) + (P0[1]-P1[1])*(P2[1]-P1[1])) / L2))
                return math.hypot(P0[0]-(P1[0] + t*(P2[0]-P1[0])), P0[1]-(P1[1] + t*(P2[1]-P1[1])))

            for net in nets:
                net_nodes = [n for n in nodes if n.get("netId") == net["id"]]
                if len(net_nodes) < 2: continue
                
                for i in range(len(net_nodes)):
                    for j in range(i + 1, len(net_nodes)):
                        na, nb = net_nodes[i], net_nodes[j]
                        dist = rm.haversine_km(float(na["lat"]), float(na["lng"]), float(nb["lat"]), float(nb["lng"]))
                        
                        # Resolve profile
                        pa = dict(profiles.get(na["type"], profiles['handheld']))
                        if "tx_power_w" in na and na["tx_power_w"] is not None:
                            pa['power'] = float(na["tx_power_w"])
                        if bool(na.get("emcon", False)):
                            pa['power'] = 0.0
                        ld_a = na.get("loadout", {})
                        
                        # Physical boosts
                        if ld_a.get("amp"): pa['power'] *= 2.0; pa['max_range'] *= 1.5
                        if ld_a.get("power_pack"): pa['max_range'] *= 1.2
                        
                        # --- Advanced Environmental Impacts ---
                        freq = 45.0 # Default VHF
                        if ld_a.get("hf_radio"): 
                            freq = 7.0 # HF
                            # HF Day/Night Prop (Skywave)
                            if is_night:
                                pa['max_range'] *= 4.0 # Huge jump at night
                                pa['full_quiet'] *= 2.0
                            if env_ionosphere:
                                pa['max_range'] *= 0.4 # Solar storm kills HF
                        
                        if ld_a.get("sat_terminal"):
                            freq = 8000.0 # SHF
                            # Rain Fade for SATCOM
                            if env_rain > 0.3:
                                pa['max_range'] *= (1.0 - env_rain * 0.8)
                        
                        # --- EW Mitigation ---
                        # ECCM reduces jamming effectiveness
                        # Directional antennas ignore jammers not in LOS (simplified as 50% reduction)
                        jam_penalty = 1.0
                        for tz in threat_zones:
                            if tz["type"] == "jammer":
                                d_jam = rm.haversine_km(float(na["lat"]), float(na["lng"]), tz["lat"], tz["lng"])
                                if d_jam < tz["radius_km"]:
                                    eff = (1.0 - d_jam/tz["radius_km"])
                                    if ld_a.get("eccm"): eff *= 0.3 # 70% reduction
                                    if ld_a.get("directional"): eff *= 0.5 # 50% reduction
                                    jam_penalty *= (1.0 - eff)

                        res = sim_infra.compute_path(
                            float(na["lat"]), float(na["lng"]),
                            float(nb["lat"]), float(nb["lng"]),
                            channel=-1, 
                            sender_cs=na.get("callsign", "A"),
                            recipient_cs=nb.get("callsign", "B"),
                            sender_profile=SimpleNamespace(
                                tx_power_w=pa['power'],
                                max_range_km=pa['max_range'],
                                full_quiet_km=pa['full_quiet'],
                                alt_m=pa.get('alt', 2.0)
                            )
                        )
                        
                        link_sig = res.signal * jam_penalty
                        link_mismatches = []
                        
                        # --- Redundancy & SPF Analysis ---
                        is_redundant = False
                        backup_desc = "None (Single path)"
                        critical_relay_id = None

                        # 0. Global Failover Check (SATCOM)
                        loadout_a = na.get("loadout", {})
                        if loadout_a.get("sat_terminal"):
                            is_redundant = True
                            backup_desc = "PACE Failover: SATCOM"
                        
                        if not is_redundant:
                            if res.link_type and "retrans" in res.link_type and len(res.hops) > 0:
                                # Primary path used a relay. Can we survive its loss?
                                relay_name = res.hops[0]
                                relay_node = next((n for n in nodes if n.get("callsign") == relay_name), None)
                                
                                if relay_node:
                                    rid = str(relay_node["id"])
                                    # Shadow Calculation: Disable the primary relay
                                    sim_infra.remove_node(rid)
                                    res_alt = sim_infra.compute_path(
                                        float(na["lat"]), float(na["lng"]),
                                        float(nb["lat"]), float(nb["lng"]),
                                        channel=-1, 
                                        sender_profile=SimpleNamespace(
                                            tx_power_w=pa['power'], max_range_km=pa['max_range'],
                                            full_quiet_km=pa['full_quiet'], alt_m=pa.get('alt', 2.0)
                                        )
                                    )
                                    # Restore the relay
                                    prof_r = profiles.get(relay_node["type"], profiles['retrans'])
                                    sim_infra.add_node(RetransNode(
                                        node_id=rid, name=str(relay_node.get("callsign")),
                                        lat=float(relay_node["lat"]), lon=float(relay_node["lng"]),
                                        alt_m=prof_r['alt'], power_w=prof_r['power']
                                    ))
                                    
                                    if res_alt.signal > 0.15:
                                        is_redundant = True
                                        backup_desc = f"Failover via {res_alt.link_type.split(':')[-1] if 'retrans' in res_alt.link_type else 'Direct LOS'}"
                                    elif link_sig > 0.15:
                                        critical_relay_id = rid

                            elif res.link_type == "direct" and link_sig > 0.15:
                                # Primary is direct. Do we have a backup relay even if unused?
                                res_relay = sim_infra._best_retrans_path(
                                    float(na["lat"]), float(na["lng"]),
                                    float(nb["lat"]), float(nb["lng"]),
                                    channel=0, fn=rm.signal_from_positions,
                                    sender_specs=(pa['power'], pa['max_range'], pa['full_quiet'])
                                )
                                if res_relay and res_relay.signal > 0.15:
                                    is_redundant = True
                                    backup_desc = f"Failover via {res_relay.hops[0]}"
                        
                        # Hard Cutoff: A failed link has ZERO resilience
                        if link_sig < 0.15:
                            is_redundant = False
                            backup_desc = "MISSION FAILURE: Broken Link"

                        # Add specific "RELAY" notes if hops exist but signal is low
                        is_relay_attempt = (res.link_type == "retrans" or len(res.hops) > 1)
                        if is_relay_attempt and link_sig < 0.1:
                            link_mismatches.append("Tactical relay attempted but signal lost between hops.")

                        # Apply OPFOR Threat Penalties (override infra if in a jammer bubble)
                        for tz in threat_zones:
                            dist_to_threat = pt_to_seg_km(tz["lat"], tz["lng"], float(na["lat"]), float(na["lng"]), float(nb["lat"]), float(nb["lng"]))
                            if dist_to_threat < tz["radius_km"]:
                                if tz["type"] == "jammer":
                                    link_sig = 0.0 # Total blackout override
                                    link_mismatches.append(f"EW ALERT: Link actively JAMMED by OPFOR transmitter.")
                                elif tz["type"] == "emi":
                                    link_sig *= 0.3 # Heavy bleed
                                    link_mismatches.append(f"EMI RISK: Urban interference degrading link.")
                        # --- EMCON Warning Injection ---
                        if bool(na.get("emcon", False)):
                            link_mismatches.append(f"EMCON active: {na.get('callsign', 'Unit')} is in radio silence and cannot transmit.")

                        # --- COMSEC Enforcement Engine ---
                        net_crypto = net.get("crypto", "none")
                        if net_crypto != "none":
                            loadout_a = na.get("loadout", {})
                            loadout_b = nb.get("loadout", {})
                            if not loadout_a.get(net_crypto) or not loadout_b.get(net_crypto):
                                link_sig = 0.0
                                link_mismatches.append(f"COMSEC FAULT: Missing required crypto key ({net_crypto.replace('crypto_', 'KEY-').upper()}).")

                        # --- Bandwidth Allocation Engine ---
                        # Resolve Primary Channel for band categorization
                        unit_p = na.get("pace", {}).get("p", "")
                        net_p = net.get("pace", {}).get("p", "")
                        primary_ch = unit_p if unit_p.strip() else net_p
                        
                        # Map preset to tactical band
                        band_type = "VHF"
                        ch_up = str(primary_ch).upper()
                        if any(x in ch_up for x in ["UHF", "U-"]): band_type = "UHF"
                        elif any(x in ch_up for x in ["SAT", "S-", "SHF"]): band_type = "SAT"
                        elif any(x in ch_up for x in ["HF", "H-"]): band_type = "HF"

                        # Base max rates (Mbps)
                        max_rates = {"VHF": 1.5, "UHF": 10.0, "SAT": 50.0, "HF": 0.05}
                        base_mbps = max_rates.get(band_type, 1.5)
                        
                        # Non-linear degradation: BW drops faster than signal
                        bw_mbps = base_mbps * (link_sig ** 2.0)
                        if link_sig < 0.15: bw_mbps = 0.0 # Cutoff for usability

                        links.append({
                            "source": na["id"],
                            "target": nb["id"],
                            "distance": dist,
                            "signal": link_sig,
                            "bandwidth_mbps": round(bw_mbps, 3),
                            "band": band_type,
                            "netId": net["id"],
                            "mismatches": link_mismatches,
                            "blocked": res.terrain_blocked,
                            "path_notes": res.notes,
                            "link_type": res.link_type,
                            "hops": res.hops,
                            "redundant": is_redundant,
                            "backup_desc": backup_desc,
                            "spf_id": critical_relay_id
                        })
                        
            # --- QoS & Network Congestion Engine ---
            retrans_loads = {}
            for lk in links:
                if lk.get("link_type") == "retrans" and lk.get("hops"):
                    net_id = lk["netId"]
                    net_data = next((n for n in nets if n["id"] == net_id), {})
                    priority = net_data.get("priority", "normal")
                    
                    base_demand_mbps = 2.0
                    if lk["band"] == "SAT": base_demand_mbps = 5.0
                    elif lk["band"] == "UHF": base_demand_mbps = 3.5
                    
                    for hop in lk["hops"]:
                        if hop not in retrans_loads:
                            retrans_loads[hop] = {"demand": 0.0, "links": []}
                        retrans_loads[hop]["demand"] += base_demand_mbps
                        retrans_loads[hop]["links"].append(lk)
            
            freq_conflicts = []
            
            for relay_name, load in retrans_loads.items():
                capacity = 20.0 # Max Mbps a relay can process
                if load["demand"] > capacity:
                    freq_conflicts.append(f"CONGESTION ALERT: Relay {relay_name} overloaded ({load['demand']:.1f} / {capacity} Mbps).")
                    
                    prio_map = {"high": 3, "normal": 2, "low": 1}
                    def get_link_prio(lk):
                        nd = next((n for n in nets if n["id"] == lk["netId"]), {})
                        return prio_map.get(nd.get("priority", "normal"), 2)
                        
                    sorted_links = sorted(load["links"], key=get_link_prio, reverse=True)
                    
                    available = capacity
                    for lk in sorted_links:
                        demand = 2.0
                        if lk["band"] == "SAT": demand = 5.0
                        elif lk["band"] == "UHF": demand = 3.5
                        
                        if available >= demand:
                            available -= demand
                        else:
                            degrade_factor = available / demand if demand > 0 else 0.0
                            lk["bandwidth_mbps"] = round(lk["bandwidth_mbps"] * degrade_factor, 3)
                            lk["signal"] *= max(0.2, degrade_factor)
                            if "QoS: Bandwidth throttled due to relay congestion." not in lk["mismatches"]:
                                lk["mismatches"].append("QoS: Bandwidth throttled due to relay congestion.")
                            available = 0.0

            # Frequency Congestion Check
            freq_usage = {}
            capability_mismatches = []
            
            for lk in links:
                if lk.get("mismatches"):
                    capability_mismatches.extend(lk["mismatches"])
            
            # Map default net frequencies
            net_freqs = {n["id"]: [n.get("pace", {}).get(k, "") for k in ("p","a","c","e")] for n in nets}
            node_freqs = {}
            
            for n in nodes:
                if not n.get("netId"): continue
                
                # Check Overlap Congestion
                unit_primary = n.get("pace", {}).get("p", "")
                primary_ch = unit_primary if unit_primary.strip() else net_freqs.get(n["netId"], [""])[0]
                primary_ch = str(primary_ch).strip().upper()
                node_freqs[n["id"]] = primary_ch
                
                if primary_ch:
                    if primary_ch not in freq_usage:
                        freq_usage[primary_ch] = set()
                    freq_usage[primary_ch].add(n["netId"])
                
                # Capability Mismatch Check
                unit_pace = [n.get("pace", {}).get(k, "") for k in ("p","a","c","e")]
                active_pace = unit_pace if any(v.strip() for v in unit_pace) else net_freqs.get(n["netId"], [])
                loadout = n.get("loadout", {})
                
                for ch in active_pace:
                    ch_up = str(ch).strip().upper()
                    if ("SAT" in ch_up or "SHF" in ch_up) and not loadout.get("sat_terminal"):
                        capability_mismatches.append(f"CAPABILITY: {n.get('callsign')} assigned SATCOM but lacks SAT Terminal in physical loadout.")
                        break
                    if "HF" in ch_up and not loadout.get("hf_radio"):
                        capability_mismatches.append(f"CAPABILITY: {n.get('callsign')} assigned HF but lacks HF Radio in physical loadout.")
                        break
                
                net_data = next((nt for nt in nets if nt["id"] == n["netId"]), {})
                net_crypto = net_data.get("crypto", "none")
                if net_crypto != "none" and not loadout.get(net_crypto):
                    capability_mismatches.append(f"COMSEC MISMATCH: {n.get('callsign')} lacks {net_crypto.replace('crypto_', 'KEY-').upper()} required by its assigned Net.")
                        
            # Check Global Overlap
            overlap_penalty = 0
            for freq, nets_using in freq_usage.items():
                if len(nets_using) > 1:
                    freq_conflicts.append(f"NETWORK OVERLAP: Multiple nets sharing {freq}. Risk of cross-talk.")
                    overlap_penalty += 0.05
                    
            # Check Co-Site Interference
            cosite_penalty = 0
            for i in range(len(nodes)):
                for j in range(i + 1, len(nodes)):
                    n1, n2 = nodes[i], nodes[j]
                    f1 = node_freqs.get(n1["id"])
                    f2 = node_freqs.get(n2["id"])
                    
                    if f1 and f2 and f1 == f2:
                        dist = rm.haversine_km(float(n1["lat"]), float(n1["lng"]), float(n2["lat"]), float(n2["lng"]))
                        if dist < 1.0:
                            freq_conflicts.append(f"CO-SITE INTERFERENCE: {n1.get('callsign')} & {n2.get('callsign')} are too close ({dist*1000:.0f}m) on {f1}.")
                            cosite_penalty += 0.15
                    
            # --- Health Scoring Logic ---
            # 1. Physical Layer (50% instead of 60%): Average link quality
            phy_score = 0
            if links:
                phy_score = sum(lk["signal"] for lk in links) / len(links)
            
            # 2. Resilience Layer (10%): Redundancy check
            spf_counts = {}
            redundant_links = 0
            valid_links = [l for l in links if l["signal"] > 0.15]
            for l in valid_links:
                if l.get("redundant"): redundant_links += 1
                if l.get("spf_id"):
                    sid = l["spf_id"]
                    spf_counts[sid] = spf_counts.get(sid, 0) + 1
            
            resilience_factor = (redundant_links / len(valid_links)) if valid_links else 0
            resilience_score = resilience_factor * 0.1
            spf_node_ids = [sid for sid, count in spf_counts.items() if count >= 1]

            # 3. Spectrum Layer (25%): Penalty for conflicts
            spec_penalty = min(0.25, cosite_penalty + overlap_penalty)
            spec_score = 0.25 - spec_penalty
            
            # 4. Capability Layer (15%): Penalty for loadout mismatches
            cap_penalty = min(0.15, len(capability_mismatches) * 0.03)
            cap_score = 0.15 - cap_penalty
            
            total_score = (phy_score * 0.5) + resilience_score + spec_score + cap_score
            health_pct = max(0, min(100, int(total_score * 100)))

            self._json({
                "ok": True, 
                "links": links, 
                "freq_conflicts": freq_conflicts, 
                "mismatches": capability_mismatches, 
                "threat_zones": threat_zones,
                "score": health_pct,
                "resilience": round(resilience_factor * 100),
                "critical_nodes": spf_node_ids,
                "breakdown": {
                    "physical": round(phy_score * 100),
                    "resilience": round(resilience_factor * 100),
                    "spectrum": round((spec_score / 0.25) * 100),
                    "capability": round((cap_score / 0.15) * 100)
                }
            })
        except Exception as e:
            import traceback
            err_msg = str(e) + "\n" + traceback.format_exc()
            log.error(f"Simulation error: {err_msg}")
            
            # Write to a file since server runs without console
            try:
                with open(r'c:\Users\johnn\Desktop\mil_radiov3\simulator_debug.txt', 'w') as f:
                    f.write(err_msg)
            except:
                pass
                
            self._err({"error": str(e), "traceback": traceback.format_exc()})

    def _post_planner_viewshed(self, body):
        try:
            nodes_input = body.get("nodes", [])
            # Fallback for single node (backward compatibility)
            if not nodes_input and body.get("node"):
                nodes_input = [body.get("node")]
            
            if not nodes_input: return self._err("Missing nodes")
            
            # Read environment sandbox from payload
            env = body.get("env", {})
            env_severity = float(env.get("severity", 0.0))
            env_storm = bool(env.get("storm", False))
            env_urban = bool(env.get("urban", False))
            env_night = bool(env.get("night", False))
            env_online = bool(env.get("online_elevation", False))
            
            # Terrain Manager for high-accuracy ground elevation
            tm = get_terrain_manager()

            night_factor = 0.85 if env_night else 1.0
            
            # Resolve profiles dynamically from the ConfigManager
            mgr = self.server_ref.cfg_mgr
            profiles = {}
            for pid, pdef in mgr.profiles.items():
                profiles[pid] = {
                    'power': pdef.tx_power_w,
                    'max_range': pdef.max_range_km,
                    'full_quiet': pdef.full_quiet_km,
                    'alt': pdef.alt_m
                }
            # Fallbacks for standard categories to support client sandbox/legacy modes
            for category, defaults in {
                'handheld': {'power': 5.0,  'max_range': 20.0, 'full_quiet': 5.0,  'alt': 1.6},
                'manpack':  {'power': 20.0, 'max_range': 50.0, 'full_quiet': 15.0, 'alt': 2.0},
                'vehicular':{'power': 50.0, 'max_range': 80.0, 'full_quiet': 30.0, 'alt': 3.5},
                'retrans':  {'power': 50.0, 'max_range': 120.0,'full_quiet': 50.0, 'alt': 12.0}
            }.items():
                if category not in profiles:
                    profiles[category] = defaults
            
            # Sandboxed RangeModel
            rm = RangeModel(
                max_range_km=25.0,
                full_quiet_km=5.0,
                power_w=5.0,
                terrain_factor=(0.3 if env_urban else 1.0) * night_factor
            )
            rm.weather_enabled = (env_severity > 0 or env_storm)
            rm.weather_severity = env_severity
            rm.weather_type = "storm" if env_storm else "rain"
            rm.weather_humidity = env_severity * 0.5

            # Terrain Manager for high-accuracy ground elevation
            tm = get_terrain_manager()
            
            processed_nodes = []
            min_lat, max_lat = 90.0, -90.0
            min_lng, max_lng = 180.0, -180.0
            
            for n in nodes_input:
                lat, lng = float(n.get("lat", 0)), float(n.get("lng", 0))
                p = dict(profiles.get(n.get("type", "handheld"), profiles['handheld']))
                if "tx_power_w" in n and n["tx_power_w"] is not None:
                    p['power'] = float(n["tx_power_w"])
                loadout = n.get("loadout", {})
                if loadout.get("power_pack"):
                    p['power'] *= 1.5
                    p['max_range'] *= 1.3
                if loadout.get("retrans_kit"):
                    p['full_quiet'] *= 1.5
                if loadout.get("amp"):
                    p['power'] *= 2.0
                
                pn_ground = tm.get_elevation_highres(lat, lng, fallback_online=env_online) or 0.0
                processed_nodes.append({'lat': lat, 'lng': lng, 'p': p, 'abs_alt': pn_ground + p.get('alt', 2.0)})
                
                # Expansion buffer (1.2x range)
                r_km = p['max_range'] * 1.2
                lat_off = r_km / 111.0
                lng_off = r_km / (111.0 * math.cos(math.radians(lat)))
                
                min_lat, max_lat = min(min_lat, lat - lat_off), max(max_lat, lat + lat_off)
                min_lng, max_lng = min(min_lng, lng - lng_off), max(max_lng, lng + lng_off)

            # Highest Accuracy Grid: Increase resolution
            # We aim for ~100-200m per cell for "perfect for hills" mode
            lat_dist_km = (max_lat - min_lat) * 111.0
            avg_lat = (max_lat + min_lat) / 2
            lng_dist_km = (max_lng - min_lng) * (111.0 * math.cos(math.radians(avg_lat)))
            
            grid_size_lat = min(50, max(20, int(lat_dist_km / 0.15)))
            grid_size_lng = min(50, max(20, int(lng_dist_km / 0.15)))
            
            lat_step = (max_lat - min_lat) / grid_size_lat
            lng_step = (max_lng - min_lng) / grid_size_lng
            
            cells = []
            for i in range(grid_size_lat):
                for j in range(grid_size_lng):
                    clat = min_lat + (i * lat_step) + (lat_step/2)
                    clng = min_lng + (j * lng_step) + (lng_step/2)
                    
                    max_sig = 0.0
                    for pn in processed_nodes:
                        d = rm.haversine_km(pn['lat'], pn['lng'], clat, clng)
                        if d > pn['p']['max_range']: continue
                        
                        # -- HIGHEST ACCURACY: 3D Line-of-Sight & Foliage Factor --
                        # Transmitter height (assume 2.0m for handheld/manpack)
                        tx_alt = 2.0
                        rx_alt = 2.0
                        
                        # 1. Resolve cell altitude
                        rx_ground = tm.get_elevation_highres(clat, clng, fallback_online=env_online) or 0.0
                        rx_abs_alt = 2.0 + rx_ground

                        # 2. Check physical blocking
                        los_clear = tm.check_line_of_sight(pn['lat'], pn['lng'], pn['abs_alt'], clat, clng, rx_abs_alt, fallback_online=env_online)
                        
                        # 3. Compute terrain attenuation (Forests/Urban)
                        foliage_factor = tm.compute_terrain_factor(pn['lat'], pn['lng'], clat, clng)
                        
                        sig = rm.evaluate_profile(d, pn['p']['power'], pn['p']['max_range'], pn['p']['full_quiet'])
                        
                        # Apply shadow and foliage losses
                        if not los_clear:
                            sig *= 0.02 # Near total blackout behind hills
                        else:
                            sig *= foliage_factor
                            
                        if sig > max_sig: max_sig = sig
                    
                    if max_sig > 0.01: # Avoid cluttering with near-zero noise
                        cells.append({
                            "bounds": [[clat - lat_step/2.1, clng - lng_step/2.1], 
                                       [clat + lat_step/2.1, clng + lng_step/2.1]],
                            "signal": max_sig
                        })

            self._json({"ok": True, "cells": cells})
        except Exception as e:
            import traceback
            log.error("Viewshed Error: " + traceback.format_exc())
            self._err(str(e))


# -- Web admin server -----------------------------------------------------------
class WebAdminServer:
    def __init__(self, config_path=Path("radio_config.ini"), web_port=8890):
        self.web_port     = web_port
        self.cfg_mgr      = ConfigManager(config_path)
        # Store web port in config so clients can discover it
        self.cfg_mgr.config.web_admin_port = web_port
        self.radio_server = RadioServer(self.cfg_mgr,
                                        activity_cb=_activity.add,
                                        audio_cb=_audio_callback_multiplexer)
        self._httpd       = None
        self._running     = False

        # AAR recorder — always created; only active when a session is started
        try:
            from radio_recorder import Recorder
            aar_dir = Path(self.cfg_mgr.config.aar_dir or "aar")
            self.recorder = Recorder(aar_dir=aar_dir,
                                     sample_rate=self.cfg_mgr.config.sample_rate)
            self.radio_server.set_recorder(self.recorder)
            log.info("AAR recorder initialised (aar_dir=%s)", aar_dir)
        except Exception as e:
            log.warning("AAR recorder not available: %s", e)
            self.recorder = None

        AdminHandler.server_ref = self

    def start(self):
        self.radio_server.start()

        # Auto-start recording if configured
        if self.recorder and self.cfg_mgr.config.aar_enabled:
            cfg = self.cfg_mgr.config
            try:
                h_hour = (_parse_hhmmss(cfg.aar_h_hour)
                          if cfg.aar_time_source == "manual" and cfg.aar_h_hour
                          else None)
                self.recorder.start_session(
                    exercise    = cfg.aar_exercise_name or "EXERCISE",
                    time_source = cfg.aar_time_source or "wall",
                    h_hour_wall = h_hour,
                    sword_url   = (cfg.aar_sword_url
                                   if cfg.aar_time_source == "sword" else None),
                )
                log.info("AAR auto-started (aar_enabled=True)")
            except Exception as e:
                log.warning("AAR auto-start failed: %s", e)

        class _HTTP(http.server.ThreadingHTTPServer): pass
        self._httpd   = _HTTP(("0.0.0.0", self.web_port), AdminHandler)
        self._running = True
        threading.Thread(target=self._httpd.serve_forever,
                         daemon=True, name="web-admin").start()
        log.info("Web admin: http://0.0.0.0:%d", self.web_port)

    def stop(self):
        self._running = False
        if self.recorder:
            try:
                self.recorder.stop_session()
            except Exception:
                pass
        if self._httpd: self._httpd.shutdown()
        self.radio_server.stop()

    def run_forever(self):
        self.start()
        try:
            while self._running: time.sleep(0.5)
        except KeyboardInterrupt:
            pass
        finally:
            self.stop()


# -- Helpers --------------------------------------------------------------------
def _fmt_uptime(s):
    h, r = divmod(int(s), 3600); m, s = divmod(r, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"

def _fmt_age(s):
    if s < 5:    return "just now"
    if s < 60:   return f"{int(s)}s ago"
    if s < 3600: return f"{int(s/60)}m ago"
    return f"{int(s/3600)}h ago"

def _secs_to_hhmmss(secs: float) -> str:
    """Convert seconds-since-midnight to HH:MM:SS string."""
    s = int(secs) % 86400
    return f"{s//3600:02d}:{(s%3600)//60:02d}:{s%60:02d}"

def _parse_hhmmss(s: str) -> float:
    """Parse HH:MM:SS string into seconds since midnight. Returns 0.0 on error."""
    try:
        parts = str(s).strip().split(":")
        h, m, sec = int(parts[0]), int(parts[1]), float(parts[2])
        return h * 3600 + m * 60 + sec
    except Exception:
        return 0.0


# -- Entry point ----------------------------------------------------------------
def main():
    import argparse
    import io

    # Force UTF-8 on stdout/stderr so Unicode characters in the banner don't
    # crash on Windows CP1252 terminals (the cause of the PyInstaller crash loop).
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    # Also set the env var so any subprocesses inherit UTF-8
    os.environ.setdefault("PYTHONUTF8", "1")

    logging.basicConfig(
        level  = logging.INFO,
        format = "%(asctime)s  %(levelname)-7s  %(name)-20s  %(message)s",
        datefmt= "%H:%M:%S",
    )
    p = argparse.ArgumentParser(description="TACNET Web Admin + Relay Server")
    p.add_argument("--config",   default="radio_config.ini")
    p.add_argument("--web-port", type=int, default=8890)
    p.add_argument("--port",     type=int, default=None)
    p.add_argument("--host",     default=None)
    p.add_argument("--debug",    action="store_true")
    args = p.parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    web = WebAdminServer(Path(args.config), web_port=args.web_port)
    if args.port:
        web.radio_server._cfg.server_port = args.port
    if args.host:
        web.radio_server._cfg.bind_address = args.host

    # ASCII-safe banner — no Unicode box characters that can fail on CP1252
    banner = (
        "\n"
        "+----------------------------------------------------------+\n"
        "|        TACNET -- Web Admin + Relay Server                |\n"
        "+----------------------------------------------------------+\n"
        f"|  Admin UI:   http://localhost:{args.web_port:<29}|\n"
        f"|  UDP Voice:  {web.radio_server._cfg.bind_address}:{web.radio_server._cfg.server_port:<35}|\n"
        f"|  TCP Ctrl:   {web.radio_server._cfg.bind_address}:{web.radio_server._cfg.server_ctrl_port:<35}|\n"
        "+----------------------------------------------------------+\n"
    )
    try:
        print(banner)
    except Exception:
        pass  # If even this fails (e.g. fully detached process), just continue

    web.run_forever()


if __name__ == "__main__":
    # Single-instance guard — prevent two server processes running simultaneously
    _srv_mutex = None
    if sys.platform == "win32":
        try:
            import ctypes
            _MUTEX_NAME = "Local\\TacNetServer_SingleInstance"
            # CreateMutexW returns a handle. If it already exists, GetLastError() == 183
            _srv_mutex = ctypes.windll.kernel32.CreateMutexW(None, True, _MUTEX_NAME)
            if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
                print("\n[CRITICAL] TacNet-Server is already running in another process.", file=sys.stderr)
                print("[CRITICAL] Duplicate launch prevented to avoid port conflicts and data corruption.\n", file=sys.stderr)
                ctypes.windll.kernel32.CloseHandle(_srv_mutex)
                sys.exit(1)
        except Exception as e:
            print(f"[WARNING] Single-instance mutex check failed: {e}", file=sys.stderr)
    
    try:
        main()
    finally:
        if _srv_mutex and sys.platform == "win32":
            import ctypes
            ctypes.windll.kernel32.CloseHandle(_srv_mutex)