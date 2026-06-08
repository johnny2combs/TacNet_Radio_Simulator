"""
MilRadio — Client
The full radio client: audio I/O, PTT, network, effects, and Tkinter GUI.
"""
from __future__ import annotations
import socket
import struct
import threading
import time
import queue
import logging
import sys
import os
import json
import urllib.request
from typing import Optional, Dict, List, Set
from pathlib import Path
try:
    import numpy as np
except ImportError:
    print("CRITICAL: numpy is not installed. Please run: pip install numpy")
    sys.exit(1)

from radio_protocol import (
    Packet, PktType, PacketCodec, ChannelKey, Payloads,
    encode_audio, decode_audio,
    SAMPLE_RATE, FRAME_MS, FRAME_SAMPLES,
    FLAG_COMPRESSED, FLAG_OPUS, FLAG_END_OF_TX,
    HEADER_SIZE,
)
from radio_effects import RadioEffects, EffectState, RangeModel
from radio_config import ConfigManager, RadioConfig, ChannelDef

log = logging.getLogger("radio.client")

# ── Tkinter import ───────────────────────────────────────────────────
try:
    import tkinter as tk
    from tkinter import ttk, messagebox, simpledialog
    _TK_AVAILABLE = True
except ImportError:
    _TK_AVAILABLE = False
    log.info("tkinter not available — GUI disabled")

# ── Audio backend detection ──────────────────────────────────────────
_AUDIO_AVAILABLE = False
_pa = None
def _init_audio():
    global _AUDIO_AVAILABLE, _pa
    try:
        import pyaudio
        _pa = pyaudio
        _AUDIO_AVAILABLE = True
        log.info("pyaudio available — audio I/O enabled")
    except ImportError:
        log.warning("pyaudio not found — running in network-only mode")
    except Exception as e:
        log.warning(f"pyaudio init error: {e}")
_init_audio()


# ── Jitter Buffer ────────────────────────────────────────────────────
class JitterBuffer:
    """
    Simple adaptive jitter buffer for audio playback.
    Target depth: 2-4 frames (40-80 ms at 20ms frames).
    Buffers packets to smooth out network timing jitter.
    """
    TARGET_DEPTH   = 3          # frames to hold before starting playback
    MAX_DEPTH      = 25         # ~500 ms max buffering before dropping
    DRAIN_HYSTER   = 0.5        # allow depth to fall to 50% before pausing
    
    def __init__(self, frame_samples: int):
        self._frame = frame_samples
        self._buf: queue.Queue = queue.Queue()
        self._buffering = True   # True = filling up, False = playing out
        self._lock = threading.Lock()
    
    def put(self, pcm: np.ndarray, callsign: str):
        """Add a decoded audio frame. Drop oldest if overflowing."""
        if self._buf.qsize() >= self.MAX_DEPTH:
            try:
                self._buf.get_nowait()   # drop oldest
            except queue.Empty:
                pass
        try:
            self._buf.put_nowait((pcm, callsign))
        except queue.Full:
            pass
        # Start playing once target depth reached
        with self._lock:
            if self._buffering and self._buf.qsize() >= self.TARGET_DEPTH:
                self._buffering = False
    
    def get(self, timeout: float = 0.01):
        """
        Get next frame.  Returns (pcm, callsign) or None.
        Returns None while buffering or if empty.
        """
        with self._lock:
            # Re-enter buffering state if queue drains below hysteresis
            if not self._buffering and self._buf.qsize() == 0:
                self._buffering = True
            if self._buffering:
                return None
        try:
            return self._buf.get(timeout=timeout)
        except queue.Empty:
            return None
    
    def flush(self):
        """Drain the buffer (e.g. on channel change)."""
        while not self._buf.empty():
            try:
                self._buf.get_nowait()
            except queue.Empty:
                break
        with self._lock:
            self._buffering = True
    
    @property
    def depth(self) -> int:
        return self._buf.qsize()

# ── PTT hotkey detection ──────────────────────────────────────────────
_PTT_LIB = None
def _init_ptt():
    """Initialize PTT hotkey library"""
    global _PTT_LIB
    try:
        import pynput.keyboard
        _PTT_LIB = "pynput"
        log.info("pynput available — keyboard PTT enabled")
        return
    except ImportError:
        pass
    try:
        import keyboard as kb
        _PTT_LIB = "keyboard"
        log.info("keyboard lib available — keyboard PTT enabled")
        return
    except ImportError:
        pass
    log.info("No keyboard PTT lib — use GUI button only")
_init_ptt()

# ── Colour palette ───────────────────────────────────────────────────
C = {
    "chassis":       "#1c1c1c",
    "chassis2":      "#222222",
    "chassis3":      "#2e2e2e",
    "recess":        "#111111",
    "border_hi":     "#505050",
    "border_lo":     "#080808",
    "lcd_bg":        "#061006",
    "lcd_fg":        "#39ff55",
    "lcd_dim":       "#195522",
    "lcd_hi":        "#99ffbb",
    "lcd_amber":     "#ffb833",
    "green":         "#00e676",
    "green_dim":     "#004422",
    "red":           "#ff3333",
    "red_bright":    "#ff6655",
    "red_dim":       "#3d0000",
    "amber":         "#ffaa00",
    "amber_dim":     "#3d2800",
    "cyan":          "#00ddff",
    "cyan_dim":      "#003344",
    "ptt_idle":      "#1c251c",
    "ptt_hot":       "#bb1800",
    "ptt_rx":        "#002a18",
    "text_bright":   "#e0e0e0",
    "text_mid":      "#888888",
    "text_dim":      "#444444",
    "mon_idle":      "#191919",
    "mon_active":    "#001810",
    "mon_tx":        "#1a0000",
    "mon_border":    "#2a2a2a",
    "bg": "#1c1c1c", "bg2": "#111111", "bg3": "#222222", "panel": "#222222",
    "border": "#505050", "accent": "#00ddff", "dim": "#444444",
    "text": "#888888", "bright": "#e0e0e0",
    "tx_active": "#bb1800", "rx_active": "#00e676",
}

# ── Fonts ────────────────────────────────────────────────────────────
_FONT_DISPLAY = ("Courier", 22, "bold")
_FONT_LCD_SM  = ("Courier", 10)
_FONT_LCD_MED = ("Courier", 12, "bold")
_FONT_MONO    = ("Courier", 9)
_FONT_BTN     = ("Courier", 10, "bold")
_FONT_LABEL   = ("Courier", 8)
_LOG_LINES = 6
_MONITOR_ROWS = 8

# ── Connection Dialog / Login Screen ─────────────────────────────────
class ConnectionDialog:
    """Login screen - fetches callsigns from server API"""
    def __init__(self, config_path: Path = Path("radio_config.ini")):
        self._cfg_mgr = ConfigManager(config_path)
        self._cfg = self._cfg_mgr.config
        self.result = None
        self._callsigns_list = []
        
    def _fetch_callsigns(self, host: str, web_port: int) -> List[dict]:
        """Fetch available callsigns from server web admin API"""
        try:
            url = f"http://{host}:{web_port}/api/callsigns"
            log.info(f"Fetching callsigns from {url}")
            with urllib.request.urlopen(url, timeout=3) as resp:
                data = json.loads(resp.read().decode())
                return data.get("callsigns", [])
        except Exception as e:
            log.warning(f"Failed to fetch callsigns: {e}")
            return []
    
    def _fetch_profiles(self, host: str, web_port: int) -> List[dict]:
        """Fetch available radio profiles from server web admin API"""
        try:
            url = f"http://{host}:{web_port}/api/profiles"
            log.info(f"Fetching profiles from {url}")
            with urllib.request.urlopen(url, timeout=3) as resp:
                data = json.loads(resp.read().decode())
                return data.get("profiles", [])
        except Exception as e:
            log.warning(f"Failed to fetch profiles: {e}")
            return []
    
    def show(self):
        if not _TK_AVAILABLE:
            return {"callsign": self._cfg.callsign, "host": self._cfg.server_host, 
                    "port": self._cfg.server_port, "channel": self._cfg.current_channel}
        
        self.root = tk.Tk()
        self.root.title("MilRadio — Connect")
        self.root.configure(bg=C["chassis"])
        self.root.resizable(False, False)
        
        frame = tk.Frame(self.root, bg=C["chassis"], padx=40, pady=30)
        frame.pack()
        
        tk.Label(frame, text="◈ MILRADIO", font=("Courier", 24, "bold"),
                 bg=C["chassis"], fg=C["accent"]).pack(pady=(0, 10))
        tk.Label(frame, text="TACTICAL VOICE NETWORK", font=("Courier", 10),
                 bg=C["chassis"], fg=C["text_dim"]).pack(pady=(0, 20))
        
        # Server host
        tk.Label(frame, text="SERVER HOST", font=_FONT_LABEL,
                 bg=C["chassis"], fg=C["text_mid"]).pack(anchor="w")
        self._host_var = tk.StringVar(value=self._cfg.server_host)
        tk.Entry(frame, textvariable=self._host_var, font=_FONT_LCD_SM,
                 bg=C["recess"], fg=C["lcd_fg"], width=25).pack(pady=(0, 10))
        
        # Voice server port (UDP)
        tk.Label(frame, text="VOICE PORT (UDP)", font=_FONT_LABEL,
                 bg=C["chassis"], fg=C["text_mid"]).pack(anchor="w")
        self._voice_port_var = tk.StringVar(value=str(self._cfg.server_port))
        tk.Entry(frame, textvariable=self._voice_port_var, font=_FONT_LCD_SM,
                 bg=C["recess"], fg=C["lcd_fg"], width=25).pack(pady=(0, 10))
        
        # Web admin port (for API calls)
        tk.Label(frame, text="WEB ADMIN PORT", font=_FONT_LABEL,
                 bg=C["chassis"], fg=C["text_mid"]).pack(anchor="w")
        self._web_port_var = tk.StringVar(value="8890")
        tk.Entry(frame, textvariable=self._web_port_var, font=_FONT_LCD_SM,
                 bg=C["recess"], fg=C["lcd_fg"], width=25).pack(pady=(0, 10))
        
        # Fetch callsigns button
        tk.Button(frame, text="FETCH CALLSIGNS", font=_FONT_BTN,
                  bg=C["cyan"], fg=C["bg"], width=25,
                  command=self._fetch_and_populate).pack(pady=(0, 15))
        
        # Callsign dropdown
        tk.Label(frame, text="CALLSIGN", font=_FONT_LABEL,
                 bg=C["chassis"], fg=C["text_mid"]).pack(anchor="w")
        self._callsign_var = tk.StringVar(value=self._cfg.callsign)
        self._callsign_dropdown = ttk.Combobox(frame, textvariable=self._callsign_var,
                                                values=[], font=_FONT_LCD_SM, width=28)
        self._callsign_dropdown.pack(pady=(0, 15))
        self._callsign_dropdown.bind("<<ComboboxSelected>>", self._on_callsign_change)
        
        # Radio Profile dropdown
        tk.Label(frame, text="RADIO PROFILE", font=_FONT_LABEL,
                 bg=C["chassis"], fg=C["text_mid"]).pack(anchor="w")
        self._profile_var = tk.StringVar(value="handheld")
        self._profile_dropdown = ttk.Combobox(frame, textvariable=self._profile_var,
                                              values=[], font=_FONT_LCD_SM, width=28, state="readonly")
        self._profile_dropdown.pack(pady=(0, 15))
        
        # Initial channel
        tk.Label(frame, text="INITIAL CHANNEL", font=_FONT_LABEL,
                 bg=C["chassis"], fg=C["text_mid"]).pack(anchor="w")
        self._channel_var = tk.StringVar(value=str(self._cfg.current_channel))
        self._channel_dropdown = ttk.Combobox(frame, textvariable=self._channel_var,
                                              values=[], font=_FONT_LCD_SM, width=28, state="readonly")
        self._channel_dropdown.pack(pady=(0, 20))
        
        # Connect button
        self._connect_btn = tk.Button(frame, text="CONNECT", font=_FONT_BTN,
                  bg=C["accent"], fg=C["bg"], width=20,
                  command=self._on_connect)
        self._connect_btn.pack()
        
        # Status label
        self._status_lbl = tk.Label(frame, text="", font=_FONT_LABEL,
                 bg=C["chassis"], fg=C["text_dim"])
        self._status_lbl.pack(pady=(10, 0))
        
        self.root.wait_window()
        return self.result
    
    def _fetch_and_populate(self):
        """Fetch callsigns from server and populate dropdown"""
        host = self._host_var.get().strip()
        try:
            web_port = int(self._web_port_var.get().strip())
        except:
            self._status_lbl.config(text="✗ Invalid web port", fg=C["red"])
            return
        
        self._status_lbl.config(text="Fetching callsigns...", fg=C["amber"])
        self.root.update()
        
        self._callsigns_list = self._fetch_callsigns(host, web_port)
        self._profiles_list = self._fetch_profiles(host, web_port)
        
        if self._profiles_list:
            prof_names = [f"{p['id']} - {p['name']}" for p in self._profiles_list]
            self._profile_dropdown.config(values=prof_names)
            if prof_names:
                self._profile_dropdown.set(prof_names[0])
        
        if self._callsigns_list:
            cs_names = [cs["callsign"] for cs in self._callsigns_list]
            self._callsign_dropdown.config(values=cs_names)
            if self._cfg.callsign in cs_names:
                self._callsign_dropdown.set(self._cfg.callsign)
            elif cs_names:
                self._callsign_dropdown.set(cs_names[0])
            self._connect_btn.config(state="normal")
            self._status_lbl.config(text=f"✓ {len(cs_names)} callsigns available", fg=C["green"])
            self._update_channel_dropdown()
        else:
            self._status_lbl.config(
                text="✗ Could not fetch callsigns — enter callsign manually",
                fg=C["amber"])
    
    def _update_channel_dropdown(self):
        """Update channel dropdown based on selected callsign's assigned channels"""
        callsign = self._callsign_var.get()
        cs_def = next((cs for cs in self._callsigns_list if cs["callsign"] == callsign), None)
        
        if cs_def and cs_def.get("channels"):
            assigned_chs = cs_def["channels"]
            ch_options = []
            for ch in self._cfg_mgr.channel_list():
                if ch.id in assigned_chs:
                    ch_options.append(f"CH{ch.id:02d} — {ch.name}")
            
            self._channel_dropdown.config(values=ch_options)
            if ch_options:
                self._channel_dropdown.set(ch_options[0])
        else:
            ch_options = [f"CH{ch.id:02d} — {ch.name}" for ch in self._cfg_mgr.channel_list()]
            self._channel_dropdown.config(values=ch_options)
            if ch_options:
                self._channel_dropdown.set(ch_options[0])
    
    def _on_callsign_change(self, event=None):
        """When callsign changes, update channel dropdown and profile"""
        self._update_channel_dropdown()
        callsign = self._callsign_var.get()
        cs_def = next((cs for cs in self._callsigns_list if cs["callsign"] == callsign), None)
        if cs_def and getattr(cs_def, "radio_profile", None) is not None:
            prof_id = getattr(cs_def, "radio_profile")
        elif cs_def and "radio_profile" in cs_def:
            prof_id = cs_def["radio_profile"]
        else:
            prof_id = None
        if prof_id and getattr(self, "_profile_dropdown", None):
            for p in self._profile_dropdown["values"]:
                if p.startswith(prof_id):
                    self._profile_dropdown.set(p)
                    break
    
    def _on_connect(self):
        callsign = self._callsign_var.get().strip().upper()
        if not callsign:
            messagebox.showerror("Error", "Callsign required")
            return
            
        profile_str = getattr(self, "_profile_var", tk.StringVar(value="handheld")).get()
        profile_id = profile_str.split(" - ")[0] if profile_str else "handheld"
        
        host = self._host_var.get().strip()
        try:
            voice_port = int(self._voice_port_var.get().strip())
            web_port = int(self._web_port_var.get().strip())
        except:
            messagebox.showerror("Error", "Invalid port number")
            return
        
        channel_id = self._cfg.current_channel
        channel_text = self._channel_var.get()
        for ch in self._cfg_mgr.channel_list():
            if f"CH{ch.id:02d}" in channel_text:
                channel_id = ch.id
                break
        
        cs_def = next((cs for cs in self._callsigns_list if cs["callsign"] == callsign), None)
        assigned_channels = cs_def.get("channels", []) if cs_def else []
        
        self._cfg.callsign = callsign
        self._cfg.server_host = host
        self._cfg.server_port = voice_port
        self._cfg.current_channel = channel_id
        self._cfg.radio_profile = profile_id
        # Store web port so _check_channel_updates can find it later
        self._cfg.web_admin_port = web_port
        self._cfg_mgr.save()
        
        # Before connecting over UDP, tell the web server our assigned profile
        try:
            import urllib.request, urllib.parse, json
            url = f"http://{host}:{web_port}/api/callsigns/{urllib.parse.quote(callsign)}"
            req = urllib.request.Request(url, method="POST")
            req.add_header('Content-Type', 'application/json')
            data = json.dumps({"radio_profile": profile_id, "channels": [channel_id]}).encode()
            with urllib.request.urlopen(req, data=data, timeout=3) as resp:
                pass
        except Exception as e:
            log.warning(f"Failed to push explicit profile: {e}")
        
        self.result = {
            "callsign": callsign, 
            "host": host, 
            "port": voice_port, 
            "channel": channel_id,
            "assigned_channels": assigned_channels
        }
        self.root.destroy()

# ── Receive State ────────────────────────────────────────────────────
class ReceiveState:
    def __init__(self, callsign: str):
        self.callsign = callsign
        self.effect_state = EffectState()
        self.last_seq = 0
        self.last_seen = time.time()
        self.signal = 0.0
        self.active = False
        self.effect_state.click_pending = True

# ── Radio Client Core ────────────────────────────────────────────────
class RadioClientCore:
    def __init__(self, config_path: Path = Path("radio_config.ini")):
        self._cfg_mgr  = ConfigManager(config_path)
        self._cfg      = self._cfg_mgr.config
        self._effects  = RadioEffects(
            squelch=self._cfg.squelch, volume=self._cfg.output_volume,
            distortion=self._cfg.distortion, noise_floor=self._cfg.noise_floor,
        )
        self._codec = self._build_codec()
        self._sock: Optional[socket.socket] = None
        self._running = False
        self._connected = False
        self._tx_active = False
        self._tx_seq = 0
        self._vox_hold_ts = 0.0
        self._ptt_lock = threading.Lock()
        self._pa_instance = None
        self._input_stream = None
        self._output_stream = None
        self._rx_states: Dict[str, ReceiveState] = {}
        self._rx_lock = threading.Lock()
        self._jitter: JitterBuffer = JitterBuffer(FRAME_SAMPLES)
        self._members: List[dict] = []
        self._members_lock = threading.Lock()
        self._rx_signal = 0.0
        self._tx_level = 0.0
        self._monitored_channels: Set[int] = set()
        self._assigned_channels: Set[int] = set()
        
        self.on_ptt_change: Optional[callable] = None
        self.on_signal_change: Optional[callable] = None
        self.on_member_update: Optional[callable] = None
        self.on_rx_start: Optional[callable] = None
        self.on_rx_end: Optional[callable] = None
        self.on_status_change: Optional[callable] = None
        self.on_profile_change: Optional[callable] = None
        self.on_assigned_channels_change: Optional[callable] = None
        self.on_ptt_key_change: Optional[callable] = None
        # JOIN confirmation tracking
        self._join_pending: bool = False
        self._join_retry_count: int = 0
        # Keepalive and auto-reconnect
        self._last_rx_ts: float = 0.0
        self._last_rtt_ms: int = -1
        self._reconnect_delay: float = 2.0
        self._reconnect_attempts: int = 0
        # Squelch tail (brief hold after end-of-TX before squelch closes)
        self._squelch_tail_ts: float = 0.0
        self._squelch_tail_active: bool = False
        # Audio device lists (populated by _start_audio)
        self.input_devices:  list = []
        self.output_devices: list = []

    def _build_codec(self) -> PacketCodec:
        keys = {}
        for ch_id, ch_def in self._cfg_mgr.channels.items():
            if ch_def.passphrase:
                keys[ch_id] = ChannelKey(ch_def.passphrase, ch_id)
        return PacketCodec(keys)

    def connect(self, assigned_channels: List[int] = None):
        # Allow reconnect if socket was closed or connection was lost uncleanly.
        # Check _running rather than just _connected so that a crashed/closed
        # session (where disconnect() was never called) can recover.
        if self._connected and self._running and self._sock is not None:
            return
        self._running = True
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.settimeout(0.5)
        self._sock.bind(("", 0))
        threading.Thread(target=self._rx_loop, daemon=True, name="rx-net").start()
        threading.Thread(target=self._playback_loop, daemon=True, name="playback").start()
        if _AUDIO_AVAILABLE: self._start_audio()
        self._setup_ptt_hotkey()
        
        if assigned_channels:
            self._assigned_channels = set(assigned_channels)
        
        if not self._assigned_channels or self._cfg.current_channel in self._assigned_channels:
            self._send_join(self._cfg.current_channel)
        else:
            if self._assigned_channels:
                self._cfg.current_channel = min(self._assigned_channels)
                self._send_join(self._cfg.current_channel)
        
        self._send_position()
        
        # Initial profile sync-push
        hp = getattr(self._cfg, 'server_host', '127.0.0.1')
        wp = getattr(self._cfg, 'web_admin_port', 8890)
        self._sync_profile_push(hp, wp, self._cfg.radio_profile)

        # Fetch audio config from server and apply it so sim/cpx mode
        # set in the admin UI takes effect on this client immediately.
        self._apply_server_audio_config()
        self._connected = True
        self._reconnect_attempts = 0
        self._reconnect_delay = 2.0
        self._last_rx_ts = time.time()
        threading.Thread(target=self._keepalive_loop, daemon=True, name="keepalive").start()
        threading.Thread(target=self._reconnect_watchdog, daemon=True, name="reconnect").start()
        threading.Thread(target=self._audio_config_poll_loop, daemon=True, name="audio-poll").start()
        self._apply_server_audio_config()
        self._set_status("Connected")
        log.info(f"Client connected as [{self._cfg.callsign}]")

    def _apply_server_audio_config(self) -> None:
        """
        Fetch server config and specific client status to sync audio effects
        and radio hardware profiles.
        """
        web_port = getattr(self._cfg, 'web_admin_port', 8890)
        host     = getattr(self._cfg, 'server_host', '127.0.0.1')
        config_url  = f"http://{host}:{web_port}/api/config"
        status_url  = f"http://{host}:{web_port}/api/clients/{urllib.parse.quote(self._cfg.callsign)}"
        
        # 1. General Config (CPX/Sim mode, global audio params)
        try:
            with urllib.request.urlopen(config_url, timeout=2) as resp:
                srv_cfg = json.loads(resp.read().decode())
            self._effects.update(
                squelch             = srv_cfg.get('squelch',             self._cfg.squelch),
                volume              = srv_cfg.get('output_volume',        self._cfg.output_volume),
                distortion          = srv_cfg.get('distortion',           self._cfg.distortion),
                noise_floor         = srv_cfg.get('noise_floor',          self._cfg.noise_floor),
                ptt_click_enabled   = srv_cfg.get('ptt_click_enabled',    self._cfg.ptt_click_enabled),
                tx_highpass_enabled = srv_cfg.get('tx_highpass_enabled',  self._cfg.tx_highpass_enabled),
                tx_preemph_enabled  = srv_cfg.get('tx_preemph_enabled',   self._cfg.tx_preemph_enabled),
            )
            # Sample rate sync
            srv_sr = srv_cfg.get('sample_rate', self._cfg.sample_rate)
            if srv_sr != self._cfg.sample_rate:
                self._cfg.sample_rate = srv_sr
                self._codec = self._build_codec()
        except Exception as e:
            log.debug("Config poll failed: %s", e)

        # 2. Client Status Sync (Radio Profile)
        try:
            with urllib.request.urlopen(status_url, timeout=2) as resp:
                cli_info = json.loads(resp.read().decode())
                if cli_info and "radio_profile" in cli_info:
                    new_prof = cli_info["radio_profile"]
                    if new_prof != self._cfg.radio_profile:
                        log.info("Server changed profile to: %s", new_prof)
                        self._cfg.radio_profile = new_prof
                        # Request full profiles list to update specs
                        self._sync_profile_specs(host, web_port, new_prof)
                
                # Check for intended channel push (from Node Manager)
                if cli_info and "intended_channel" in cli_info:
                    new_ch = cli_info["intended_channel"]
                    if new_ch is not None and new_ch != self._cfg.current_channel:
                        log.info("Server pushing channel change: CH%02d", new_ch)
                        self.change_channel(new_ch)
                
                # Check for assigned channels update (Access List)
                if cli_info and "assigned_channels" in cli_info:
                    new_assigned = set(cli_info["assigned_channels"])
                    if new_assigned != self._assigned_channels:
                        log.info("Assigned channels changed by server: %s", new_assigned)
                        self._assigned_channels = new_assigned
                        if self.on_assigned_channels_change:
                            self.on_assigned_channels_change(list(new_assigned))
                        
                        # Auto-switch if current channel was removed from access list
                        if new_assigned and self._cfg.current_channel not in new_assigned:
                            log.info("Current channel no longer assigned. Auto-switching...")
                            self.change_channel(min(new_assigned), force=True)
                
                # Check for PTT key update
                if cli_info and "ptt_key" in cli_info:
                    new_ptt = cli_info["ptt_key"]
                    if new_ptt != self._cfg.ptt_key:
                        log.info("Server changed PTT key to: %s", new_ptt)
                        self._cfg.ptt_key = new_ptt
                        self._setup_ptt_hotkey()
                        if self.on_ptt_key_change:
                            self.on_ptt_key_change(new_ptt)
        except Exception as e:
            log.debug("Status poll failed: %s", e)

    def _sync_profile_specs(self, host, port, profile_id):
        """Fetch profile specs and update local effects engine."""
        url = f"http://{host}:{port}/api/profiles"
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                profiles = json.loads(resp.read().decode()).get("profiles", [])
                for p in profiles:
                    if p["id"] == profile_id:
                        self._effects.update(
                            transmit_power_w    = p.get("tx_power_w",   20.0),
                            max_range_km         = p.get("max_range_km",  25.0),
                            full_quiet_km        = p.get("full_quiet_km", 5.0)
                        )
                        if hasattr(self, 'on_status_change'):
                            self.on_status_change(f"Profile: {p['name']}")
                        # Trigger immediate GUI refresh if bound
                        if hasattr(self, 'on_profile_change'):
                            self.on_profile_change(p['id'])
                        break
        except: pass

    def _sync_profile_push(self, host, port, profile_id):
        """Push local profile choice to server to ensure sync on connect."""
        url = f"http://{host}:{port}/api/callsigns/{urllib.parse.quote(self._cfg.callsign)}"
        try:
            req = urllib.request.Request(url, data=json.dumps({"radio_profile": profile_id}).encode(),
                                         headers={'Content-Type': 'application/json'}, method='POST')
            with urllib.request.urlopen(req, timeout=2) as resp:
                pass
        except: pass


    def disconnect(self):
        if self._tx_active: self.ptt_off()
        self._send_leave(self._cfg.current_channel)
        self._running = False
        self._connected = False
        self._audio_restarting = True
        time.sleep(0.1) # Give callback time to exit
        if self._input_stream:
            try: self._input_stream.stop_stream(); self._input_stream.close()
            except: pass
        if self._output_stream:
            try: self._output_stream.stop_stream(); self._output_stream.close()
            except: pass
        if self._pa_instance:
            try: self._pa_instance.terminate()
            except: pass
        try: self._sock.close()
        except: pass
        self._jitter.flush()
        self._set_status("Disconnected")

    def ptt_on(self):
        with self._ptt_lock:
            if self._tx_active:
                return
            if self._cfg.current_channel not in self._cfg_mgr.channels:
                log.warning("PTT blocked - not on a valid primary channel")
                return
            if self._assigned_channels and self._cfg.current_channel not in self._assigned_channels:
                log.warning(f"PTT blocked - no access to CH{self._cfg.current_channel:02d}")
                return
            self._tx_active = True
        self._send_ptt(PktType.PTT_ON)
        log.debug(f"PTT ON - CH{self._cfg.current_channel:02d} (PRIMARY)")
        if self.on_ptt_change:
            self.on_ptt_change(True)
        # Local mic click to confirm TX
        try:
            import winsound
            import threading
            threading.Thread(target=lambda: winsound.Beep(1800, 60), daemon=True).start()
        except Exception:
            pass

    def ptt_off(self):
        with self._ptt_lock:
            if not self._tx_active: return
            self._tx_active = False
        self._send_ptt(PktType.PTT_OFF)
        log.debug("PTT OFF")
        if self.on_ptt_change:
            self.on_ptt_change(False)

    def toggle_ptt(self):
        if self._tx_active:
            self.ptt_off()
        else:
            self.ptt_on()

    def change_channel(self, new_channel: int, force: bool = False):
        if not force and self._assigned_channels and new_channel not in self._assigned_channels:
            log.warning(f"Channel change blocked - no access to CH{new_channel:02d}")
            return
        if new_channel == self._cfg.current_channel:
            return
        was_tx = self._tx_active
        if was_tx:
            self.ptt_off()
        self._send_leave(self._cfg.current_channel)
        self._cfg.current_channel = new_channel
        self._monitored_channels.discard(new_channel)
        self._jitter.flush()
        self._rx_signal = 0.0
        with self._rx_lock:
            self._rx_states.clear()
        with self._members_lock:
            self._members.clear()
        self._send_join(new_channel)
        # Keep status as Connected — do NOT say Joined/Disconnected
        self._set_status(f"Connected — CH{new_channel:02d}")

    def join_channel(self, channel: int, monitor: bool = False, force: bool = False):
        if not force and self._assigned_channels and channel not in self._assigned_channels:
            log.warning(f"Channel join blocked - no access to CH{channel:02d}")
            return
        
        if monitor:
            self._monitored_channels.add(channel)
            self._send_join(channel)
            log.info(f"Monitoring CH{channel:02d} (receive-only)")
        else:
            if self._cfg.current_channel != channel:
                self._send_leave(self._cfg.current_channel)
            self._cfg.current_channel = channel
            self._monitored_channels.discard(channel)
            self._send_join(channel)
            log.info(f"Primary channel switched to CH{channel:02d}")
        
        self._codec = self._build_codec()

    def _send(self, pkt: Packet):
        wire = self._codec.encode(pkt)
        try:
            self._sock.sendto(wire, (self._cfg.server_host, self._cfg.server_port))
        except Exception as e:
            log.debug(f"Send error: {e}")

    def _send_ptt(self, ptype: PktType):
        self._send(Packet(type=ptype, channel=self._cfg.current_channel, callsign=self._cfg.callsign))

    def _send_join(self, channel: int):
        self._send(Packet(type=PktType.CHANNEL_JOIN, channel=channel, callsign=self._cfg.callsign))

    def _send_leave(self, channel: int):
        self._send(Packet(type=PktType.CHANNEL_LEAVE, channel=channel, callsign=self._cfg.callsign))

    def _send_position(self):
        lat, lon, alt = self._cfg_mgr.get_position()
        self._send(Packet(type=PktType.POSITION_UPDATE, channel=self._cfg.current_channel,
                          callsign=self._cfg.callsign, payload=Payloads.position(lat, lon, alt)))

    def send_radio_check(self, target: str = ""):
        self._send(Packet(type=PktType.RADIO_CHECK, channel=self._cfg.current_channel,
                          callsign=self._cfg.callsign, payload=Payloads.radio_check(target)))

    def _rx_loop(self):
        while self._running:
            try:
                data, addr = self._sock.recvfrom(65507)
            except socket.timeout:
                continue
            except OSError:
                # Socket was closed. If we're still running, the reconnect
                # watchdog replaced self._sock — pause briefly and retry
                # so this thread keeps working on the new socket.
                if self._running:
                    time.sleep(0.1)
                    continue
                break
            if len(data) < 8:
                continue
            pkt = self._codec.decode(data)
            if pkt is None:
                continue
            pkt.sender_addr = addr
            self._handle_packet(pkt)

    def _handle_packet(self, pkt: Packet):
        if pkt.callsign == self._cfg.callsign:
            return
        if pkt.type == PktType.VOICE:
            self._handle_voice(pkt)
        elif pkt.type == PktType.PTT_ON:
            log.debug(f"RX PTT ON from {pkt.callsign}")
            self._last_rx_ts = time.time()
            if self.on_rx_start:
                self.on_rx_start(pkt.callsign)
            with self._rx_lock:
                if pkt.callsign not in self._rx_states:
                    self._rx_states[pkt.callsign] = ReceiveState(pkt.callsign)
                self._rx_states[pkt.callsign].active = True
        elif pkt.type == PktType.PTT_OFF:
            log.debug(f"RX PTT OFF from {pkt.callsign}")
            self._last_rx_ts = time.time()
            if self.on_rx_end:
                self.on_rx_end(pkt.callsign)
            with self._rx_lock:
                if pkt.callsign in self._rx_states:
                    self._rx_states[pkt.callsign].active = False
            # Begin signal decay
            self._rx_signal = min(self._rx_signal, 0.6)
        elif pkt.type == PktType.MEMBER_LIST:
            members = Payloads.parse_member_list(pkt.payload)
            with self._members_lock:
                self._members = members
            self._join_pending = False
            self._last_rx_ts = time.time()   # server is alive — reset watchdog
            if self.on_member_update:
                self.on_member_update(members)
        elif pkt.type == PktType.RADIO_CHECK:
            target = pkt.payload[:16].rstrip(b"\x00").decode("ascii", "replace")
            if target == self._cfg.callsign or not target:
                log.info(f"RADIO CHECK from {pkt.callsign}")
                threading.Timer(0.5, lambda: self._send(Packet(
                    type=PktType.RADIO_CHECK_ACK, channel=pkt.channel,
                    callsign=self._cfg.callsign,
                    payload=pkt.callsign.encode("ascii", "replace")[:16].ljust(16,b"\x00")
                ))).start()
        elif pkt.type == PktType.PONG:
            rtt = int(time.time()*1000) - struct.unpack_from("!Q", pkt.payload, 0)[0]
            log.debug(f"PONG from server, RTT={rtt}ms")
            self._last_rtt_ms = rtt
            self._last_rx_ts = time.time()
        elif pkt.type == PktType.FORCE_CHANNEL:
            new_ch = pkt.channel
            log.info(f"SERVER COMMAND: Force switch to CH{new_ch:02d}")
            self.change_channel(new_ch, force=True)
            self._last_rx_ts = time.time()

    def _handle_voice(self, pkt: Packet):
        if pkt.channel != self._cfg.current_channel and pkt.channel not in self._monitored_channels:
            return
        
        payload = pkt.payload
        flags = pkt.flags

        signal = 1.0
        if flags & 0x80:
            if len(payload) >= 4:
                signal = struct.unpack_from("!f", payload, 0)[0]
                payload = payload[4:]
            flags &= ~0x80

        with self._rx_lock:
            state = self._rx_states.get(pkt.callsign)
            if state is None:
                state = ReceiveState(pkt.callsign)
                self._rx_states[pkt.callsign] = state
            state.signal = signal
            state.last_seen = time.time()
            is_new = not state.active
            state.active = True

        if is_new and self.on_rx_start:
            self.on_rx_start(pkt.callsign)

        self._rx_signal = 0.7 * self._rx_signal + 0.3 * signal
        if self.on_signal_change:
            self.on_signal_change(self._rx_signal)

        try:
            pcm = decode_audio(payload, flags)
        except Exception as e:
            log.debug(f"Audio decode error: {e}")
            return

        is_last = bool(flags & FLAG_END_OF_TX)
        try:
            processed = self._effects.process_rx(pcm, signal, state.effect_state, is_last)
        except Exception as e:
            log.debug(f"Effects error: {e}")
            processed = pcm

        self._last_rx_ts = time.time()
        self._jitter.put(processed, pkt.callsign)

    def _start_audio(self):
        try:
            if getattr(self, '_pa_instance', None) is None:
                self._pa_instance = _pa.PyAudio()
            # Enumerate devices for GUI selector
            n = self._pa_instance.get_device_count()
            self.input_devices  = []
            self.output_devices = []
            for i in range(n):
                info = self._pa_instance.get_device_info_by_index(i)
                name = info.get("name", f"Device {i}")
                if info.get("maxInputChannels", 0) > 0:
                    self.input_devices.append((i, name))
                if info.get("maxOutputChannels", 0) > 0:
                    self.output_devices.append((i, name))
            log.info(f"Audio devices: {len(self.input_devices)} in, {len(self.output_devices)} out")
            input_dev  = None if self._cfg.audio_input_device  < 0 else self._cfg.audio_input_device
            output_dev = None if self._cfg.audio_output_device < 0 else self._cfg.audio_output_device

            self._output_stream = self._pa_instance.open(
                format=_pa.paFloat32, channels=1, rate=SAMPLE_RATE, output=True,
                output_device_index=output_dev, frames_per_buffer=FRAME_SAMPLES)
            self._input_stream = self._pa_instance.open(
                format=_pa.paFloat32, channels=1, rate=SAMPLE_RATE, input=True,
                input_device_index=input_dev, frames_per_buffer=FRAME_SAMPLES,
                stream_callback=self._audio_input_cb)
            self._input_stream.start_stream()
            log.info("Audio streams started")
        except Exception as e:
            log.error(f"Audio init error: {e}")

    def _audio_input_cb(self, in_data, frame_count, time_info, status):
        if not _pa or getattr(self, '_audio_restarting', False):
            return (None, _pa.paComplete if _pa else 2)
            
        pcm = np.frombuffer(in_data, dtype=np.float32)

        rms = float(np.sqrt(np.mean(pcm**2)))
        self._tx_level = 0.8 * self._tx_level + 0.2 * rms

        if self._cfg.vox_enabled:
            if rms > self._cfg.vox_threshold:
                self._vox_hold_ts = time.time()
                if not self._tx_active:
                    self.ptt_on()
            elif self._tx_active:
                if (time.time() - self._vox_hold_ts) > (self._cfg.vox_hold_ms / 1000.0):
                    self.ptt_off()

        if self._tx_active:
            self._transmit_frame(pcm)

        return (None, _pa.paContinue)

    def _transmit_frame(self, pcm: np.ndarray):
        tx_state = self._rx_states.setdefault("_tx_", ReceiveState("_tx_"))
        processed = self._effects.process_tx(pcm, tx_state.effect_state, gain=self._cfg.input_gain)
        try:
            encoded, flags = encode_audio(processed)
        except Exception:
            return

        self._tx_seq += 1
        pkt = Packet(type=PktType.VOICE, channel=self._cfg.current_channel,
                     seq=self._tx_seq, callsign=self._cfg.callsign,
                     flags=flags, payload=encoded)
        self._send(pkt)

    def _playback_loop(self):
        """
        Jitter-buffered playback with squelch tail.
        Writes silence to keep the stream clock running between frames.
        """
        SQUELCH_TAIL_S = 0.08
        silence = np.zeros(FRAME_SAMPLES, dtype=np.float32)
        while self._running:
            result = self._jitter.get(timeout=0.005)
            if result is None:
                # Squelch tail: smooth fade-out after last RX frame
                if self._squelch_tail_active:
                    if (time.time() - self._squelch_tail_ts) < SQUELCH_TAIL_S:
                        if self._output_stream and _AUDIO_AVAILABLE:
                            try:
                                self._output_stream.write(
                                    (silence * self._cfg.output_volume).tobytes())
                            except Exception:
                                pass
                    else:
                        self._squelch_tail_active = False
                else:
                    # True idle — write silence to keep stream clock ticking
                    if self._output_stream and _AUDIO_AVAILABLE:
                        try:
                            self._output_stream.write(silence.tobytes())
                        except Exception:
                            pass
                continue
            # Frame received — reset squelch tail timer
            self._squelch_tail_active = True
            self._squelch_tail_ts = time.time()
            pcm, _cs = result
            if self._output_stream and _AUDIO_AVAILABLE:
                n = FRAME_SAMPLES
                if len(pcm) < n:
                    pcm = np.pad(pcm, (0, n - len(pcm)))
                elif len(pcm) > n:
                    pcm = pcm[:n]
                try:
                    self._output_stream.write(
                        (np.clip(pcm, -1.0, 1.0) * self._cfg.output_volume)
                        .astype(np.float32).tobytes())
                except Exception:
                    pass


    def _setup_ptt_hotkey(self):
        if not _PTT_LIB:
            return
        
        # Stop existing listener if any
        if hasattr(self, '_ptt_listener') and self._ptt_listener is not None:
            try:
                if _PTT_LIB == "pynput":
                    self._ptt_listener.stop()
                elif _PTT_LIB == "keyboard":
                    import keyboard as kb
                    kb.unhook_all()
            except Exception:
                pass
                
        try:
            if _PTT_LIB == "pynput":
                from pynput import keyboard
                key_name = self._cfg.ptt_key.lower()
                special = {
                    'ctrl': keyboard.Key.ctrl, 'shift': keyboard.Key.shift,
                    'alt': keyboard.Key.alt, 'space': keyboard.Key.space,
                    'f1': keyboard.Key.f1, 'f2': keyboard.Key.f2,
                    'f3': keyboard.Key.f3, 'f4': keyboard.Key.f4,
                    'caps_lock': keyboard.Key.caps_lock
                }
                ptt_key = special.get(key_name, keyboard.KeyCode.from_char(key_name))

                def check_match(k):
                    if k == ptt_key: return True
                    if hasattr(k, 'name') and k.name:
                        if k.name == key_name or k.name.startswith(key_name + '_'):
                            return True
                    return False

                def on_press(k):
                    if check_match(k) and not self._tx_active:
                        self.ptt_on()
                def on_release(k):
                    if check_match(k) and self._tx_active:
                        self.ptt_off()

                listener = keyboard.Listener(on_press=on_press, on_release=on_release)
                listener.daemon = True
                listener.start()
                self._ptt_listener = listener
                log.info(f"PTT hotkey: {key_name} (pynput)")

            elif _PTT_LIB == "keyboard":
                import keyboard as kb
                key_name = self._cfg.ptt_key
                kb.on_press_key(key_name, lambda _: self.ptt_on(), suppress=True)
                kb.on_release_key(key_name, lambda _: self.ptt_off(), suppress=True)
                self._ptt_listener = True
                log.info(f"PTT hotkey: {key_name} (keyboard lib)")

        except Exception as e:
            log.warning(f"PTT hotkey setup failed: {e}")

    def _keepalive_loop(self):
        """Send PING every 15 s + POSITION every 30 s to keep server state fresh."""
        _ping_count = 0
        while self._running and self._connected:
            time.sleep(15)
            if not (self._running and self._connected):
                break
            _ping_count += 1
            if not self._tx_active:
                try:
                    ts_bytes = struct.pack("!Q", int(time.time() * 1000))
                    self._send(Packet(type=PktType.PING,
                                      channel=self._cfg.current_channel,
                                      callsign=self._cfg.callsign,
                                      payload=ts_bytes))
                    log.debug("Keepalive PING sent")
                except Exception as e:
                    log.debug(f"Keepalive error: {e}")
            # Re-send position every 30 s (every 2nd keepalive cycle)
            # so server position_fresh() never expires during an exercise
            if _ping_count % 2 == 0:
                try:
                    self._send_position()
                    log.debug("Position refresh sent")
                except Exception as e:
                    log.debug(f"Position refresh error: {e}")

    def _reconnect_watchdog(self):
        """
        Watch for network loss.  If no packet received for > 30 s,
        tear down and reconnect with exponential back-off.
        """
        while self._running:
            time.sleep(5)
            if not (self._running and self._connected):
                continue
            if time.time() - self._last_rx_ts > 60:
                log.warning("No packets in 30 s — attempting reconnect")
                self._set_status("Reconnecting…")
                # Tear down sockets (but keep _running=True)
                try:
                    self._sock.close()
                except Exception:
                    pass
                time.sleep(self._reconnect_delay)
                self._reconnect_attempts += 1
                self._reconnect_delay = min(60, self._reconnect_delay * 1.5)
                try:
                    self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                    self._sock.settimeout(0.5)
                    self._sock.bind(("", 0))
                    self._last_rx_ts = time.time()
                    # Re-join all channels (primary + monitored)
                    self._send_join(self._cfg.current_channel)
                    for mon_ch in list(self._monitored_channels):
                        self._send(Packet(type=PktType.CHANNEL_JOIN,
                                          channel=mon_ch,
                                          callsign=self._cfg.callsign))
                    self._send_position()
                    self._set_status("Reconnected")
                    self._reconnect_delay = 2.0
                    log.info(f"Reconnected (attempt {self._reconnect_attempts})")
                except Exception as e:
                    log.error(f"Reconnect failed: {e}")
                    self._set_status(f"Reconnect failed — retrying…")

    def _audio_config_poll_loop(self):
        """
        Poll /api/config every 8 s and apply any audio mode changes to the
        local effects engine. This propagates CPX ↔ SIM switches made in the
        admin panel to all connected clients automatically.
        """
        while self._running and self._connected:
            time.sleep(8)
            if not (self._running and self._connected):
                break
            try:
                web_port = getattr(self._cfg, 'web_admin_port', 8890)
                url = f"http://{self._cfg.server_host}:{web_port}/api/config"
                with urllib.request.urlopen(url, timeout=3) as resp:
                    srv_cfg = json.loads(resp.read().decode())

                # Build new effects kwargs from server response
                new_kw = dict(
                    distortion          = float(srv_cfg.get('distortion',           0.0)),
                    noise_floor         = float(srv_cfg.get('noise_floor',           0.0)),
                    squelch             = float(srv_cfg.get('squelch',               self._cfg.squelch)),
                    volume              = float(srv_cfg.get('output_volume',          self._cfg.output_volume)),
                    ptt_click_enabled   =  bool(srv_cfg.get('ptt_click_enabled',     False)),
                    tx_highpass_enabled =  bool(srv_cfg.get('tx_highpass_enabled',   False)),
                    tx_preemph_enabled  =  bool(srv_cfg.get('tx_preemph_enabled',    False)),
                    weather_enabled         =  bool(srv_cfg.get('weather_enabled',         False)),
                    weather_severity        = float(srv_cfg.get('weather_severity',        0.0)),
                    weather_type            =   str(srv_cfg.get('weather_type',            'rain')),
                    weather_humidity        = float(srv_cfg.get('weather_humidity',        0.0)),
                    temp_inversion_enabled  =  bool(srv_cfg.get('temp_inversion_enabled',  False)),
                    temp_inversion_strength = float(srv_cfg.get('temp_inversion_strength', 0.0)),
                    temp_inversion_echo_ms  = float(srv_cfg.get('temp_inversion_echo_ms',  10.0)),
                    iono_enabled            =  bool(srv_cfg.get('iono_enabled',            False)),
                    iono_condition          =   str(srv_cfg.get('iono_condition',          'quiet')),
                    iono_fading             = float(srv_cfg.get('iono_fading',             0.0)),
                    iono_flutter            = float(srv_cfg.get('iono_flutter',            0.0)),
                    iono_absorption         = float(srv_cfg.get('iono_absorption',         0.0)),
                    day_night_enabled   =  bool(srv_cfg.get('day_night_enabled',     False)),
                    day_night_hour      = float(srv_cfg.get('day_night_hour',        12.0)),
                    day_night_auto      =  bool(srv_cfg.get('day_night_auto',        False)),
                    emi_enabled         =  bool(srv_cfg.get('emi_enabled',           False)),
                    emi_level           = float(srv_cfg.get('emi_level',             0.0)),
                    emi_type            =   str(srv_cfg.get('emi_type',              'white')),
                )

                # Only update if something actually changed
                changed = (
                    new_kw['distortion']          != self._effects.distortion          or
                    new_kw['noise_floor']          != self._effects.noise_floor          or
                    new_kw['ptt_click_enabled']    != self._effects.ptt_click_enabled    or
                    new_kw['tx_highpass_enabled']  != self._effects.tx_highpass_enabled  or
                    new_kw['tx_preemph_enabled']   != self._effects.tx_preemph_enabled  or
                    abs(new_kw['squelch'] - self._effects.squelch) > 0.001             or
                    abs(new_kw['volume']  - self._effects.volume)  > 0.01              or
                    new_kw['weather_enabled']          != self._effects.weather_enabled          or
                    abs(new_kw['weather_severity'] - self._effects.weather_severity)  > 0.01 or
                    new_kw['weather_type']             != self._effects.weather_type             or
                    abs(new_kw['weather_humidity'] - self._effects.weather_humidity)  > 0.01 or
                    new_kw['temp_inversion_enabled']   != self._effects.temp_inversion_enabled   or
                    abs(new_kw['temp_inversion_strength'] - self._effects.temp_inversion_strength) > 0.01 or
                    new_kw['iono_enabled']             != self._effects.iono_enabled             or
                    new_kw['iono_condition']           != self._effects.iono_condition           or
                    abs(new_kw['iono_fading']     - self._effects.iono_fading)     > 0.01 or
                    abs(new_kw['iono_flutter']    - self._effects.iono_flutter)    > 0.01 or
                    abs(new_kw['iono_absorption'] - self._effects.iono_absorption) > 0.01    or
                    new_kw['day_night_enabled']  != self._effects.day_night_enabled     or
                    abs(new_kw['day_night_hour'] - self._effects.day_night_hour) > 0.1  or
                    new_kw['day_night_auto']     != self._effects.day_night_auto        or
                    new_kw['emi_enabled']        != self._effects.emi_enabled           or
                    abs(new_kw['emi_level'] - self._effects.emi_level) > 0.005          or
                    new_kw['emi_type']           != self._effects.emi_type
                )
                if changed:
                    self._effects.update(**new_kw)
                    mode = "SIM" if new_kw['distortion'] > 0 or new_kw['noise_floor'] > 0 else "CPX"
                    log.info(f"Audio mode synced from server: {mode}  "
                             f"dist={new_kw['distortion']:.2f} "
                             f"noise={new_kw['noise_floor']:.3f}")
                    # Note: don't call on_status_change here — it drives
                    # the ONLINE/OFFLINE indicator and "Audio mode: X" 
                    # doesn't contain "Connected" so it would show OFFLINE.
                    log.info(f"Audio mode changed to {mode}")
                    # Rebuild codec if sample rate changed
                    srv_sr = int(srv_cfg.get('sample_rate', self._cfg.sample_rate))
                    if srv_sr != self._cfg.sample_rate:
                        self._cfg.sample_rate = srv_sr
                        self._codec = self._build_codec()
                        log.info(f"Sample rate updated to {srv_sr} Hz")
            except Exception as e:
                log.debug(f"Audio config poll failed: {e}")

    def get_audio_devices(self) -> dict:
        """Return dict of input/output device lists for GUI selectors."""
        if not _AUDIO_AVAILABLE:
            return {"inputs": [], "outputs": []}
        return {"inputs": self.input_devices, "outputs": self.output_devices}

    def set_audio_devices(self, input_idx: int = -1, output_idx: int = -1):
        """
        Change audio I/O devices at runtime.
        Restarts audio streams safely.
        """
        self._cfg.audio_input_device  = input_idx
        self._cfg.audio_output_device = output_idx
        
        def _restart_audio():
            self._audio_restarting = True
            time.sleep(0.15) # Wait for callback to complete
            
            if self._input_stream:
                try:
                    self._input_stream.stop_stream()
                    self._input_stream.close()
                except Exception:
                    pass
                self._input_stream = None
                
            if self._output_stream:
                try:
                    self._output_stream.stop_stream()
                    self._output_stream.close()
                except Exception:
                    pass
                self._output_stream = None
                
            if self._pa_instance:
                try:
                    self._pa_instance.terminate()
                except Exception:
                    pass
                self._pa_instance = None
                
            self._audio_restarting = False
            
            if _AUDIO_AVAILABLE and self._running:
                self._start_audio()
                log.info(f"Audio devices changed: in={input_idx} out={output_idx}")
                
        import threading
        threading.Thread(target=_restart_audio, daemon=True).start()


    def _set_status(self, msg: str):
        if self.on_status_change:
            self.on_status_change(msg)

    @property
    def signal_strength(self) -> float:
        # Decay signal toward 0 when no active RX states
        with self._rx_lock:
            any_active = any(s.active for s in self._rx_states.values()
                             if s is not None)
        if not any_active and self._rx_signal > 0:
            self._rx_signal = max(0.0, self._rx_signal - 0.04)
        return self._rx_signal

    @property
    def tx_level(self) -> float:
        return self._tx_level

    @property
    def members(self) -> list:
        with self._members_lock:
            return list(self._members)
    
    @property
    def assigned_channels(self) -> Set[int]:
        return self._assigned_channels

    @assigned_channels.setter
    def assigned_channels(self, channels: List[int]):
        """Live update of assigned channels list."""
        self._assigned_channels = set(channels)
        log.info("Assigned channels updated: %s", self._assigned_channels)

# ── GUI ───────────────────────────────────────────────────────────────
class RadioClientGUI:
    """
    Redesigned MilRadio client GUI.
    Layout: tabbed notebook (Radio | Channels | Log) inside a fixed-width window.
    """
    # TX/RX history entry limit
    _MAX_HISTORY = 120

    def __init__(self, config_path: Path = Path("radio_config.ini")):
        self._core    = RadioClientCore(config_path)
        self._cfg     = self._core._cfg
        self._cfg_mgr = self._core._cfg_mgr

        self._core.on_ptt_change     = self._on_ptt_change
        self._core.on_signal_change  = self._on_signal_change
        self._core.on_member_update  = self._on_member_update
        self._core.on_rx_start       = self._on_rx_start
        self._core.on_rx_end         = self._on_rx_end
        self._core.on_status_change  = self._on_status_change
        self._core.on_profile_change = self._on_profile_change
        self._core.on_assigned_channels_change = self._on_assigned_channels_change
        self._core.on_ptt_key_change = self._on_ptt_key_change

        self._ptt_held      = False
        self._status_msg    = "READY"
        self._rx_from       = ""
        self._signal        = 0.0
        self._signal_peak   = 0.0
        self._peak_hold_ts  = 0.0
        self._monitored: Dict[int, bool] = {}
        self._blink_state   = False
        self._blink_ts      = 0.0
        self._assigned_channels: List[int] = []
        self._tx_history: list = []   # list of dicts: {ts, cs, ch, kind, dur}
        self._history_tags = {}       # tag → colour for tx_history text widget
        
        import queue
        self._gui_queue = queue.Queue()

        self._build_gui()
        self._start_ticks()

    # ── Layout ────────────────────────────────────────────────────────
    def _build_gui(self):
        self.root = tk.Tk()
        self.root.title("MilRadio")
        self.root.configure(bg=C["chassis"])
        self.root.resizable(False, False)
        if self._cfg.gui_always_on_top:
            self.root.attributes("-topmost", True)

        outer = tk.Frame(self.root, bg=C["chassis"], padx=10, pady=8)
        outer.pack()

        self._build_header(outer)
        self._build_vfd(outer)
        self._build_notebook(outer)
        self._build_ptt(outer)
        self._build_statusbar(outer)

        k = self._cfg.ptt_key.lower()
        # Tkinter requires Function keys to be capitalized (e.g. <F7> not <f7>)
        if k.startswith('f') and k[1:].isdigit():
            k = k.upper()
        try:
            self.root.bind(f"<KeyPress-{k}>",   self._kb_ptt_on)
            self.root.bind(f"<KeyRelease-{k}>", self._kb_ptt_off)
        except Exception as e:
            log.warning(f"Failed to bind hotkey '{k}' in Tkinter: {e}")
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ── Header bar ────────────────────────────────────────────────────
    def _build_header(self, parent):
        bar = tk.Frame(parent, bg=C["chassis"])
        bar.pack(fill="x", pady=(0, 4))

        # Left: title
        tk.Label(bar, text="◈ MILRADIO",
                 font=("Courier", 11, "bold"),
                 bg=C["chassis"], fg=C["cyan"]).pack(side="left")
        tk.Label(bar, text=" TACTICAL NET",
                 font=("Courier", 9),
                 bg=C["chassis"], fg=C["text_dim"]).pack(side="left")

        # Right: connection pill
        pill = tk.Frame(bar, bg=C["recess"],
                        highlightbackground=C["border_lo"],
                        highlightthickness=1)
        pill.pack(side="right")

        self._dot_canvas = tk.Canvas(pill, width=10, height=10,
                                     bg=C["recess"], highlightthickness=0)
        self._dot_canvas.pack(side="left", padx=(6, 2), pady=3)
        self._dot_oval = self._dot_canvas.create_oval(1, 1, 9, 9,
                                                       fill=C["red"], outline="")

        self._lbl_conn = tk.Label(pill, text="OFFLINE",
                 font=("Courier", 9, "bold"),
                 bg=C["recess"], fg=C["red"])
        self._lbl_conn.pack(side="left", padx=(0, 4))

        # RTT badge
        self._lbl_rtt = tk.Label(pill, text="",
                 font=("Courier", 8),
                 bg=C["recess"], fg=C["text_dim"])
        self._lbl_rtt.pack(side="left", padx=(0, 6))

    # ── VFD display ───────────────────────────────────────────────────
    def _build_vfd(self, parent):
        surround = tk.Frame(parent, bg=C["border_lo"],
                            highlightbackground=C["border_lo"],
                            highlightthickness=1)
        surround.pack(fill="x", pady=(0, 4))
        inner = tk.Frame(surround, bg=C["lcd_bg"], padx=12, pady=8)
        inner.pack(fill="x")

        # Column 1 — channel number + frequency
        left = tk.Frame(inner, bg=C["lcd_bg"])
        left.pack(side="left")

        self._vfd_ch = tk.Label(left, text="CH--",
            font=("Courier", 34, "bold"),
            bg=C["lcd_bg"], fg=C["lcd_fg"])
        self._vfd_ch.pack(anchor="w")

        self._vfd_freq = tk.Label(left, text="--- MHz",
            font=("Courier", 11),
            bg=C["lcd_bg"], fg=C["lcd_dim"])
        self._vfd_freq.pack(anchor="w")

        # Divider
        tk.Frame(inner, bg=C["lcd_dim"], width=1).pack(
            side="left", fill="y", padx=14)

        # Column 2 — callsign + net name + enc
        mid = tk.Frame(inner, bg=C["lcd_bg"])
        mid.pack(side="left", fill="y")

        self._vfd_callsign = tk.Label(mid,
            text=self._cfg.callsign,
            font=("Courier", 18, "bold"),
            bg=C["lcd_bg"], fg=C["lcd_hi"])
        self._vfd_callsign.pack(anchor="w", pady=(2, 0))

        self._vfd_netname = tk.Label(mid,
            text="—",
            font=("Courier", 9),
            bg=C["lcd_bg"], fg=C["lcd_dim"])
        self._vfd_netname.pack(anchor="w")

        enc_row = tk.Frame(mid, bg=C["lcd_bg"])
        enc_row.pack(anchor="w", pady=(2, 0))
        self._vfd_enc = tk.Label(enc_row, text="",
            font=("Courier", 8, "bold"),
            bg=C["lcd_bg"], fg=C["lcd_dim"])
        self._vfd_enc.pack(side="left")

        self._vfd_profile = tk.Label(mid, text=self._cfg.radio_profile.upper(),
            font=("Courier", 8, "bold"),
            bg=C["lcd_bg"], fg=C["lcd_dim"])
        self._vfd_profile.pack(anchor="w", pady=(1, 0))

        # Divider
        tk.Frame(inner, bg=C["lcd_dim"], width=1).pack(
            side="left", fill="y", padx=14)

        # Column 3 — TX/RX status + signal bar + net members
        right = tk.Frame(inner, bg=C["lcd_bg"])
        right.pack(side="left", fill="y", expand=True)

        self._vfd_rx_lbl = tk.Label(right, text="",
            font=("Courier", 12, "bold"),
            bg=C["lcd_bg"], fg=C["green"],
            width=22, anchor="w")
        self._vfd_rx_lbl.pack(anchor="w")

        self._vfd_sig_canvas = tk.Canvas(right,
            width=200, height=14,
            bg=C["lcd_bg"], highlightthickness=0)
        self._vfd_sig_canvas.pack(anchor="w", pady=(4, 2))

        self._vfd_sig_label = tk.Label(right, text="S0  0%",
            font=("Courier", 8),
            bg=C["lcd_bg"], fg=C["lcd_dim"])
        self._vfd_sig_label.pack(anchor="w")

        # Net members
        self._vfd_members_lbl = tk.Label(right, text="NET: (empty)",
            font=("Courier", 8),
            bg=C["lcd_bg"], fg=C["lcd_dim"],
            justify="left")
        self._vfd_members_lbl.pack(anchor="w", pady=(4, 0))

    # ── Notebook tabs ─────────────────────────────────────────────────
    def _build_notebook(self, parent):
        style = ttk.Style()
        style.theme_use("default")
        style.configure("MR.TNotebook",
            background=C["chassis"],
            borderwidth=0,
            tabmargins=[0, 0, 0, 0])
        style.configure("MR.TNotebook.Tab",
            background=C["chassis3"],
            foreground=C["text_dim"],
            font=("Courier", 9, "bold"),
            padding=[12, 4],
            borderwidth=0)
        style.map("MR.TNotebook.Tab",
            background=[("selected", C["chassis2"]), ("active", C["border_hi"])],
            foreground=[("selected", C["cyan"]),     ("active", C["text_bright"])])

        self._nb = ttk.Notebook(parent, style="MR.TNotebook")
        self._nb.pack(fill="x", pady=(0, 4))

        # Tab 1 — Radio (S-meter + channels + controls)
        t1 = tk.Frame(self._nb, bg=C["chassis2"])
        self._nb.add(t1, text="  RADIO  ")
        self._build_radio_tab(t1)

        # Tab 2 — Roster (online members per channel)
        t2 = tk.Frame(self._nb, bg=C["chassis2"])
        self._nb.add(t2, text="  ROSTER  ")
        self._build_roster_tab(t2)

        # Tab 3 — History (TX/RX log)
        t3 = tk.Frame(self._nb, bg=C["chassis2"])
        self._nb.add(t3, text="  HISTORY  ")
        self._build_history_tab(t3)

    # ── Tab 1: Radio ──────────────────────────────────────────────────
    def _build_radio_tab(self, parent):
        row = tk.Frame(parent, bg=C["chassis2"])
        row.pack(fill="x", padx=6, pady=6)

        # S-meter + mic meter column
        self._build_meters(row)

        # Channel list (clickable rows)
        self._build_channel_panel(row)

        # Controls column (vol, sql, vox, buttons)
        self._build_controls(row)

    def _build_meters(self, parent):
        frame = _raised_frame(parent, padx=5, pady=5)
        frame.pack(side="left", fill="y", padx=(0, 5))

        # S-Meter
        tk.Label(frame, text="SIG",
                 font=_FONT_LABEL, bg=C["chassis2"], fg=C["text_dim"]).pack()
        well = _sunken_frame(frame)
        well.pack(pady=(2, 4))
        self._smeter_canvas = tk.Canvas(well,
            width=48, height=110,
            bg=C["recess"], highlightthickness=0)
        self._smeter_canvas.pack(padx=3, pady=3)
        self._smeter_peak = tk.Label(frame, text="S0",
            font=_FONT_LCD_SM, bg=C["chassis2"], fg=C["lcd_fg"])
        self._smeter_peak.pack()

        # Divider
        tk.Frame(frame, bg=C["border_lo"], height=1).pack(fill="x", pady=4)

        # Mic meter
        tk.Label(frame, text="MIC",
                 font=_FONT_LABEL, bg=C["chassis2"], fg=C["text_dim"]).pack()
        well2 = _sunken_frame(frame)
        well2.pack(pady=(2, 4))
        self._mic_canvas = tk.Canvas(well2,
            width=48, height=54,
            bg=C["recess"], highlightthickness=0)
        self._mic_canvas.pack(padx=3, pady=3)

        # VOX threshold indicator line label
        self._vox_lbl = tk.Label(frame, text="",
            font=("Courier", 7), bg=C["chassis2"], fg=C["amber_dim"])
        self._vox_lbl.pack()

    def _build_channel_panel(self, parent):
        frame = _raised_frame(parent, padx=5, pady=5)
        frame.pack(side="left", fill="both", expand=True, padx=(0, 5))

        # Header
        hdr = tk.Frame(frame, bg=C["chassis2"])
        hdr.pack(fill="x", pady=(0, 4))
        tk.Label(hdr, text="CHANNELS",
                 font=("Courier", 8, "bold"),
                 bg=C["chassis2"], fg=C["cyan"]).pack(side="left")
        tk.Label(hdr, text="click row = primary  ☑ = monitor",
                 font=("Courier", 7),
                 bg=C["chassis2"], fg=C["text_dim"]).pack(side="right")

        # Scrollable channel rows container
        self._ch_panel_frame = tk.Frame(frame, bg=C["chassis2"])
        self._ch_panel_frame.pack(fill="both", expand=True)
        self._mon_rows: Dict[int, dict] = {}
        self._monitor_panel_built = False

    def _build_monitor_rows(self):
        for w in self._ch_panel_frame.winfo_children():
            w.destroy()
        self._mon_rows.clear()

        channels = self._get_accessible_channels()
        for ch in channels:
            self._add_channel_row(ch)

        self._monitor_panel_built = True

    def _add_channel_row(self, ch):
        ch_id  = ch.id
        is_pri = (ch_id == self._cfg.current_channel)
        bg     = C["mon_active"] if is_pri else C["mon_idle"]

        row = tk.Frame(self._ch_panel_frame, bg=bg,
                       highlightbackground=C["border_lo"],
                       highlightthickness=1,
                       cursor="hand2")
        row.pack(fill="x", pady=1)

        # Status LED
        led = tk.Label(row, text="●", font=("Courier", 8),
                       bg=bg, fg=C["green"] if is_pri else C["green_dim"])
        led.pack(side="left", padx=(4, 2))

        # CH number
        ch_lbl = tk.Label(row, text=f"CH{ch_id:02d}",
                          font=("Courier", 9, "bold"),
                          bg=bg, fg=C["lcd_fg"] if is_pri else C["text_mid"],
                          width=5)
        ch_lbl.pack(side="left")

        # Channel name
        name_lbl = tk.Label(row, text=f"{ch.name[:16]:<16}",
                            font=("Courier", 8),
                            bg=bg, fg=C["lcd_hi"] if is_pri else C["text_dim"],
                            width=17, anchor="w")
        name_lbl.pack(side="left", padx=2)

        # Mini signal bar
        sig_cv = tk.Canvas(row, width=48, height=10,
                           bg=bg, highlightthickness=0)
        sig_cv.pack(side="left", padx=3)

        # Status badge
        status_lbl = tk.Label(row,
            text="PRI" if is_pri else "---",
            font=("Courier", 8, "bold"),
            bg=bg, fg=C["lcd_fg"] if is_pri else C["text_dim"],
            width=4)
        status_lbl.pack(side="left")

        # MON checkbox
        mon_var = tk.BooleanVar(value=self._monitored.get(ch_id, False))
        mon_cb = tk.Checkbutton(row, text="MON",
            variable=mon_var,
            font=("Courier", 7),
            bg=bg, fg=C["amber"],
            selectcolor=C["amber_dim"],
            activebackground=bg, bd=0,
            command=lambda c=ch_id, v=mon_var: self._toggle_monitor(c, v))
        mon_cb.pack(side="right", padx=(2, 4))

        # Bind click on row → select primary (except MON checkbox area)
        for widget in (row, led, ch_lbl, name_lbl, sig_cv, status_lbl):
            widget.bind("<Button-1>", lambda e, c=ch_id: self._select_primary(c))

        self._mon_rows[ch_id] = {
            "row": row, "led": led, "ch_lbl": ch_lbl,
            "name_lbl": name_lbl, "sig_canvas": sig_cv,
            "status_lbl": status_lbl,
            "mon_var": mon_var, "mon_btn": mon_cb,
            "bg_idle": bg,
        }

    def _build_controls(self, parent):
        frame = _raised_frame(parent, padx=6, pady=5)
        frame.pack(side="left", fill="y")

        def _slider_row(lbl_text, var, lo, hi, cmd, val_fmt="{:.1f}"):
            r = tk.Frame(frame, bg=C["chassis2"])
            r.pack(fill="x", pady=2)
            tk.Label(r, text=lbl_text, font=_FONT_LABEL,
                     bg=C["chassis2"], fg=C["text_dim"],
                     width=4, anchor="w").pack(side="left")
            s = ttk.Scale(r, from_=lo, to=hi, orient="horizontal",
                          variable=var, length=80, command=cmd)
            s.pack(side="left")
            lbl = tk.Label(r, font=("Courier", 8),
                           bg=C["chassis2"], fg=C["lcd_fg"],
                           width=5, anchor="e")
            lbl.pack(side="left", padx=(2, 0))
            return lbl

        # Volume
        tk.Label(frame, text="AUDIO", font=("Courier", 7, "bold"),
                 bg=C["chassis2"], fg=C["cyan"]).pack(pady=(0, 2))
        self._vol_var = tk.DoubleVar(value=self._cfg.output_volume)
        self._lbl_vol = _slider_row("VOL", self._vol_var, 0.0, 2.0,
                                     self._on_vol_change)
        self._lbl_vol.config(text=f"{self._cfg.output_volume:.1f}×")

        # Squelch
        self._sql_var = tk.DoubleVar(value=self._cfg.squelch)
        self._lbl_sql = _slider_row("SQL", self._sql_var, 0.0, 0.5,
                                     self._on_sql_change, "{:.2f}")
        self._lbl_sql.config(text=f"{self._cfg.squelch:.2f}")

        # VOX threshold
        self._vox_var = tk.DoubleVar(value=self._cfg.vox_threshold)
        self._lbl_vox = _slider_row("VOX", self._vox_var, 0.0, 0.2,
                                     self._on_vox_change, "{:.3f}")
        self._lbl_vox.config(text=f"{self._cfg.vox_threshold:.3f}")

        tk.Frame(frame, bg=C["border_lo"], height=1).pack(fill="x", pady=5)

        # Channel nav
        tk.Label(frame, text="CHANNEL", font=("Courier", 7, "bold"),
                 bg=C["chassis2"], fg=C["cyan"]).pack()
        ch_row = tk.Frame(frame, bg=C["chassis2"])
        ch_row.pack(pady=(2, 0))
        for txt, cmd in [("◄", self._ch_prev), ("►", self._ch_next)]:
            tk.Button(ch_row, text=txt, command=cmd,
                font=("Courier", 12, "bold"),
                bg=C["chassis3"], fg=C["text_bright"],
                activebackground=C["border_hi"], activeforeground=C["lcd_fg"],
                relief="raised", bd=2, padx=8, pady=2,
                cursor="hand2").pack(side="left", padx=2)

        tk.Frame(frame, bg=C["border_lo"], height=1).pack(fill="x", pady=5)

        # Action buttons
        tk.Label(frame, text="ACTIONS", font=("Courier", 7, "bold"),
                 bg=C["chassis2"], fg=C["cyan"]).pack()

        def _btn(txt, cmd, fg):
            tk.Button(frame, text=txt, command=cmd,
                font=("Courier", 8, "bold"),
                bg=C["chassis3"], fg=fg,
                activebackground=C["border_hi"],
                activeforeground=C["text_bright"],
                relief="flat", bd=0, padx=4, pady=3,
                cursor="hand2", width=10).pack(pady=1)

        _btn("RADIO CHK", self._do_radio_check, C["amber"])
        self._ctrl_connect_btn = tk.Button(frame, text="CONNECT",
            command=self._toggle_connect,
            font=("Courier", 8, "bold"),
            bg=C["chassis3"], fg=C["cyan"],
            activebackground=C["border_hi"],
            activeforeground=C["text_bright"],
            relief="flat", bd=0, padx=4, pady=3,
            cursor="hand2", width=10)
        self._ctrl_connect_btn.pack(pady=1)
        _btn("SETTINGS",  self._open_settings, C["text_mid"])

    # ── Tab 2: Roster ─────────────────────────────────────────────────
    def _build_roster_tab(self, parent):
        frame = tk.Frame(parent, bg=C["chassis2"])
        frame.pack(fill="both", expand=True, padx=6, pady=6)

        hdr = tk.Frame(frame, bg=C["chassis2"])
        hdr.pack(fill="x", pady=(0, 4))
        tk.Label(hdr, text="ONLINE ROSTER",
                 font=("Courier", 8, "bold"),
                 bg=C["chassis2"], fg=C["cyan"]).pack(side="left")
        self._roster_updated_lbl = tk.Label(hdr, text="",
                 font=("Courier", 7),
                 bg=C["chassis2"], fg=C["text_dim"])
        self._roster_updated_lbl.pack(side="right")

        # Column headers
        hdr2 = tk.Frame(frame, bg=C["chassis3"])
        hdr2.pack(fill="x")
        for txt, w in [("CALLSIGN", 12), ("CHANNEL", 10),
                       ("STATUS", 8), ("SIGNAL", 8)]:
            tk.Label(hdr2, text=txt,
                     font=("Courier", 7, "bold"),
                     bg=C["chassis3"], fg=C["text_dim"],
                     width=w, anchor="w", padx=4, pady=3).pack(side="left")

        # Scrollable roster body
        self._roster_frame = tk.Frame(frame, bg=C["chassis2"])
        self._roster_frame.pack(fill="both", expand=True)
        self._roster_rows: dict = {}    # callsign → label widgets

    def _refresh_roster(self, members: list):
        """Rebuild the roster rows from the current member list."""
        seen = set()
        for m in members:
            cs  = m.get("callsign", "?")
            ch  = m.get("channel", 0)
            sig = m.get("signal", 0.0)
            seen.add(cs)

            is_tx = False
            with self._core._rx_lock:
                rx = self._core._rx_states.get(cs)
                if rx and rx.active:
                    is_tx = True

            status = "TX" if is_tx else "RX-READY"
            sig_pct = f"{sig*100:.0f}%"

            if cs not in self._roster_rows:
                self._add_roster_row(cs)

            r = self._roster_rows[cs]
            r["ch_lbl"].config(text=f"CH{ch:02d}")
            r["st_lbl"].config(
                text=status,
                fg=C["red"] if is_tx else C["green"])
            r["sig_lbl"].config(text=sig_pct)
            row_bg = C["mon_tx"] if is_tx else C["mon_idle"]
            r["row"].config(bg=row_bg)
            for key in ("cs_lbl", "ch_lbl", "st_lbl", "sig_lbl"):
                r[key].config(bg=row_bg)

        # Remove stale rows
        for cs in list(self._roster_rows):
            if cs not in seen:
                self._roster_rows[cs]["row"].destroy()
                del self._roster_rows[cs]

        self._roster_updated_lbl.config(
            text=f"updated {time.strftime('%H:%M:%S')}  {len(seen)} online")

    def _add_roster_row(self, callsign: str):
        bg  = C["mon_idle"]
        row = tk.Frame(self._roster_frame, bg=bg,
                       highlightbackground=C["border_lo"],
                       highlightthickness=1)
        row.pack(fill="x", pady=1)

        cs_lbl = tk.Label(row, text=callsign,
            font=("Courier", 9, "bold"), bg=bg, fg=C["lcd_hi"],
            width=12, anchor="w", padx=4)
        cs_lbl.pack(side="left")

        ch_lbl = tk.Label(row, text="CH--",
            font=("Courier", 9), bg=bg, fg=C["lcd_fg"],
            width=10, anchor="w", padx=4)
        ch_lbl.pack(side="left")

        st_lbl = tk.Label(row, text="—",
            font=("Courier", 9, "bold"), bg=bg, fg=C["green"],
            width=8, anchor="w", padx=4)
        st_lbl.pack(side="left")

        sig_lbl = tk.Label(row, text="—",
            font=("Courier", 9), bg=bg, fg=C["text_dim"],
            width=8, anchor="w", padx=4)
        sig_lbl.pack(side="left")

        self._roster_rows[callsign] = {
            "row": row, "cs_lbl": cs_lbl,
            "ch_lbl": ch_lbl, "st_lbl": st_lbl, "sig_lbl": sig_lbl,
        }

    # ── Tab 3: History ────────────────────────────────────────────────
    def _build_history_tab(self, parent):
        frame = tk.Frame(parent, bg=C["chassis2"])
        frame.pack(fill="both", expand=True, padx=6, pady=6)

        hdr = tk.Frame(frame, bg=C["chassis2"])
        hdr.pack(fill="x", pady=(0, 4))
        tk.Label(hdr, text="TX / RX HISTORY",
                 font=("Courier", 8, "bold"),
                 bg=C["chassis2"], fg=C["cyan"]).pack(side="left")
        tk.Button(hdr, text="CLR",
            command=self._clear_history,
            font=("Courier", 7), bg=C["chassis3"], fg=C["text_dim"],
            relief="flat", bd=0, padx=4, pady=1,
            cursor="hand2").pack(side="right")

        well = _sunken_frame(frame)
        well.pack(fill="both", expand=True)
        self._hist_text = tk.Text(well,
            height=10, width=72,
            font=("Courier", 9),
            bg=C["recess"], fg=C["lcd_fg"],
            relief="flat", bd=0,
            state="disabled",
            wrap="none")
        self._hist_text.pack(fill="both", expand=True, padx=4, pady=4)

        sb = tk.Scrollbar(well, command=self._hist_text.yview,
                          bg=C["chassis3"], troughcolor=C["recess"])
        # keep it simple — no scrollbar packing overlap
        self._hist_text.config(yscrollcommand=sb.set)

        # Tags
        self._hist_text.tag_config("ts",   foreground=C["text_dim"])
        self._hist_text.tag_config("tx",   foreground=C["red_bright"])
        self._hist_text.tag_config("rx",   foreground=C["green"])
        self._hist_text.tag_config("sys",  foreground=C["amber"])
        self._hist_text.tag_config("dur",  foreground=C["text_dim"])
        self._hist_text.tag_config("ch",   foreground=C["cyan_dim"])

    def _add_history(self, kind: str, callsign: str, channel: int,
                      duration: float = 0.0, note: str = ""):
        ts = time.strftime("%H:%M:%S")
        entry = dict(ts=ts, kind=kind, cs=callsign,
                     ch=channel, dur=duration, note=note)
        self._tx_history.append(entry)
        if len(self._tx_history) > self._MAX_HISTORY:
            self._tx_history.pop(0)
        self._hist_text.config(state="normal")
        self._hist_text.insert("end", f"{ts}  ", "ts")
        arrow = "▶" if kind == "tx" else "◀"
        tag   = "tx" if kind == "tx" else "rx" if kind == "rx" else "sys"
        self._hist_text.insert("end", f"{arrow} ", tag)
        self._hist_text.insert("end",
            f"{callsign:<10}", tag)
        self._hist_text.insert("end",
            f" CH{channel:02d}", "ch")
        if duration > 0:
            self._hist_text.insert("end",
                f"  {duration:.1f}s", "dur")
        if note:
            self._hist_text.insert("end",
                f"  {note}", "sys")
        self._hist_text.insert("end", "\n")
        # Keep last MAX_HISTORY lines
        line_count = int(self._hist_text.index("end-1c").split(".")[0])
        if line_count > self._MAX_HISTORY + 2:
            self._hist_text.delete("1.0", "3.0")
        self._hist_text.see("end")
        self._hist_text.config(state="disabled")

    def _clear_history(self):
        self._tx_history.clear()
        self._hist_text.config(state="normal")
        self._hist_text.delete("1.0", "end")
        self._hist_text.config(state="disabled")

    # ── Callbacks from Core ───────────────────────────────────────────
    def _on_profile_change(self, profile_id):
        if hasattr(self, '_vfd_profile'):
            self._gui_queue.put(lambda: self._vfd_profile.config(text=f"PR: {profile_id.upper()}"))

    def _on_assigned_channels_change(self, channels):
        log.info("GUI: Refreshing channel panel due to access list change")
        self._gui_queue.put(lambda: self._build_monitor_rows())

    def _on_ptt_key_change(self, new_key):
        log.info(f"GUI: Updating PTT hint to {new_key}")
        if hasattr(self, '_ptt_hint_lbl'):
            self._gui_queue.put(lambda: self._ptt_hint_lbl.config(
                text=f"HOLD BUTTON  or  HOLD {new_key.upper()}  to transmit"))

    def _on_status_change(self, msg: str):
        self._gui_queue.put(lambda: self._update_status(msg))

    # ── Activity log (in status area below PTT) ───────────────────────
    def _build_log(self, parent):
        # Kept for backwards compatibility — log now goes to history tab
        pass

    def _log(self, msg: str, tag: str = "sys"):
        # Route system messages to history tab
        self._add_history("sys", "SYS", self._cfg.current_channel, note=msg)

    # ── PTT button ────────────────────────────────────────────────────
    def _build_ptt(self, parent):
        surround = tk.Frame(parent, bg=C["chassis"], pady=4)
        surround.pack(fill="x")

        self._ptt_btn = tk.Button(surround,
            text="◉   PUSH TO TALK   ◉",
            font=("Courier", 17, "bold"),
            bg=C["ptt_idle"], fg=C["green"],
            activebackground=C["ptt_hot"],
            activeforeground="#ffffff",
            relief="raised", bd=4,
            cursor="hand2",
            height=2)
        self._ptt_btn.pack(fill="x")
        self._ptt_btn.bind("<ButtonPress-1>",   self._btn_ptt_on)
        self._ptt_btn.bind("<ButtonRelease-1>", self._btn_ptt_off)

        hint = tk.Label(surround,
            text=f"HOLD BUTTON  or  HOLD {self._cfg.ptt_key.upper()}  to transmit",
            font=("Courier", 7),
            bg=C["chassis"], fg=C["text_dim"])
        hint.pack(pady=(2, 0))
        self._ptt_hint_lbl = hint

    def _build_statusbar(self, parent):
        bar = tk.Frame(parent, bg=C["recess"], padx=6, pady=2)
        bar.pack(fill="x")
        self._lbl_status = tk.Label(bar, text="READY",
            font=("Courier", 8), bg=C["recess"],
            fg=C["text_dim"], anchor="w")
        self._lbl_status.pack(side="left", fill="x", expand=True)
        self._lbl_audio_warn = tk.Label(bar,
            text="" if _AUDIO_AVAILABLE else "⚠ NO AUDIO",
            font=("Courier", 8), bg=C["recess"], fg=C["amber"])
        self._lbl_audio_warn.pack(side="right")

    # ── Tick loops ────────────────────────────────────────────────────
    def _start_ticks(self):
        self.root.after(50,   self._tick_fast)
        self.root.after(200,  self._tick_medium)
        self.root.after(1000, self._tick_slow)
        self.root.after(10000, self._check_channel_updates_periodic)
        self._process_gui_queue()
        
    def _process_gui_queue(self):
        try:
            while True:
                fn = self._gui_queue.get_nowait()
                try: fn()
                except Exception as e: log.error(f"GUI Queue task error: {e}")
        except queue.Empty:
            pass
        self.root.after(30, self._process_gui_queue)

    def _tick_fast(self):
        self._draw_smeter()
        self._draw_mic_meter()
        now = time.time()
        if now - self._blink_ts > 0.4:
            self._blink_state = not self._blink_state
            self._blink_ts    = now
        self.root.after(50, self._tick_fast)

    def _tick_medium(self):
        self._update_vfd()
        self._update_monitor_rows()
        self.root.after(200, self._tick_medium)

    def _tick_slow(self):
        rtt = self._core._last_rtt_ms
        rtt_txt = f"  RTT:{rtt}ms" if rtt >= 0 else ""
        jb  = self._core._jitter.depth
        jb_txt = f"  JB:{jb}" if jb > 0 else ""
        self._lbl_status.config(text=self._status_msg + rtt_txt + jb_txt)
        if time.time() - self._peak_hold_ts > 2.0:
            self._signal_peak = max(0.0, self._signal_peak - 0.05)
        # Update profile Display
        if hasattr(self, '_vfd_profile'):
            self._vfd_profile.config(text=self._cfg.radio_profile.upper())
        if rtt >= 0:
            col = (C["green"] if rtt < 50
                   else C["amber"] if rtt < 150
                   else C["red"])
            self._lbl_rtt.config(text=f"{rtt}ms", fg=col)
        else:
            self._lbl_rtt.config(text="")
        self.root.after(1000, self._tick_slow)

    def _check_channel_updates_periodic(self):
        # All live updates (profile, channel assignment, access lists) 
        # are now handled via RadioClientCore's polling loop (_apply_server_audio_config)
        # and its associated callbacks.
        self._core._apply_server_audio_config()
        if self._core._connected:
            self.root.after(5000, self._check_channel_updates_periodic)

    # ── Draw routines ─────────────────────────────────────────────────
    def _draw_smeter(self):
        c = self._smeter_canvas
        W, H = 48, 110
        c.delete("all")
        c.create_rectangle(0, 0, W, H, fill=C["recess"], outline="")
        sig  = self._signal
        peak = self._signal_peak
        n_seg, seg_h, gap = 12, 7, 2
        for i in range(n_seg):
            y_top = H - (i + 1) * (seg_h + gap)
            y_bot = H - i * (seg_h + gap) - gap
            filled   = (i / n_seg) < sig
            peak_seg = abs((i / n_seg) - peak) < (1 / n_seg)
            if peak_seg:
                colour = C["lcd_hi"]
            elif filled:
                colour = (C["green"] if i < 8
                          else C["amber"] if i < 10
                          else C["red"])
            else:
                colour = (C["green_dim"] if i < 8
                          else C["amber_dim"] if i < 10
                          else C["red_dim"])
            c.create_rectangle(5, y_top, W-5, y_bot, fill=colour, outline="")
        for i, label in enumerate(["S3", "S6", "S9", "+30"]):
            y = H - (i * 3 + 2) * (seg_h + gap) - seg_h
            c.create_text(W // 2, y, text=label,
                          fill=C["lcd_dim"], font=("Courier", 5), anchor="center")
        s_val = int(sig * 9)
        self._smeter_peak.config(text=f"S{s_val}")

    def _draw_mic_meter(self):
        c = self._mic_canvas
        W, H = 48, 54
        c.delete("all")
        c.create_rectangle(0, 0, W, H, fill=C["recess"], outline="")
        level   = min(1.0, self._core.tx_level * 6)
        vox_thr = self._cfg.vox_threshold * 6
        n_seg, seg_w, gap = 10, 3, 1
        total  = n_seg * (seg_w + gap)
        x_off  = (W - total) // 2
        for i in range(n_seg):
            x1 = x_off + i * (seg_w + gap)
            x2 = x1 + seg_w
            filled = (i / n_seg) < level
            colour = (C["green"] if i < 6
                      else C["amber"] if i < 8
                      else C["red"]) if filled else C["green_dim"]
            c.create_rectangle(x1, 12, x2, H-10, fill=colour, outline="")
        # VOX threshold marker
        if self._cfg.vox_enabled:
            vx = int(x_off + vox_thr * total)
            vx = max(x_off, min(x_off + total, vx))
            c.create_line(vx, 10, vx, H-8, fill=C["amber"], width=1)
            c.create_text(W // 2, 6, text="VOX",
                          fill=C["amber"] if level > vox_thr else C["amber_dim"],
                          font=("Courier", 6))
            c.create_text(W // 2, H-4, text="┤",
                          fill=C["amber_dim"], font=("Courier", 6))
        self._vox_lbl.config(
            text=f"vox {self._cfg.vox_threshold:.3f}" if self._cfg.vox_enabled else "")

    def _draw_monitor_sig(self, canvas, signal: float, bg: str):
        canvas.delete("all")
        canvas.config(bg=bg)
        W, H = 48, 10
        n = 8
        seg_w = (W - (n - 1)) // n
        for i in range(n):
            x1 = i * (seg_w + 1)
            x2 = x1 + seg_w
            filled = (i / n) < signal
            colour = (C["green"] if i < 5
                      else C["amber"] if i < 7
                      else C["red"]) if filled else C["green_dim"]
            canvas.create_rectangle(x1, 1, x2, H-1, fill=colour, outline="")

    # ── VFD update ────────────────────────────────────────────────────
    def _update_vfd(self):
        ch = self._cfg_mgr.get_current_channel()
        if not ch:
            return
        self._vfd_ch.config(text=f"CH{ch.id:02d}")
        self._vfd_freq.config(text=ch.frequency)
        self._vfd_netname.config(text=ch.name)
        self._vfd_callsign.config(text=self._cfg.callsign)
        enc_txt = "▣ AES-256" if ch.encrypted else "○ CLEAR"
        enc_fg  = C["lcd_dim"] if ch.encrypted else C["amber"]
        self._vfd_enc.config(text=enc_txt, fg=enc_fg)
        self._draw_vfd_sigbar()
        if self._rx_from:
            txt = f"◄ RX  {self._rx_from}"
            self._vfd_rx_lbl.config(
                text=txt if self._blink_state else "", fg=C["green"])
        elif self._core._tx_active:
            self._vfd_rx_lbl.config(
                text="▶ TX  TRANSMITTING" if self._blink_state else "▶ TX",
                fg=C["red"])
        else:
            self._vfd_rx_lbl.config(text="")
        # Net members
        members = self._core.members
        pri_ch  = self._cfg.current_channel
        online  = [m.get("callsign", "?") for m in members
                   if m.get("channel") == pri_ch
                   and m.get("callsign") != self._cfg.callsign]
        if online:
            self._vfd_members_lbl.config(
                text="NET: " + "  ".join(online[:6]),
                fg=C["lcd_dim"])
        else:
            self._vfd_members_lbl.config(
                text="NET: (empty)", fg=C["lcd_dim"])

    def _draw_vfd_sigbar(self):
        c = self._vfd_sig_canvas
        c.delete("all")
        W, H = 200, 14
        c.config(bg=C["lcd_bg"])
        sig   = self._signal
        n_seg = 16
        seg_w = 10
        gap   = 2
        for i in range(n_seg):
            x1 = i * (seg_w + gap) + 2
            x2 = x1 + seg_w
            filled = (i / n_seg) < sig
            colour = (C["lcd_fg"] if i < 10
                      else C["lcd_amber"] if i < 13
                      else C["red"]) if filled else C["lcd_dim"]
            c.create_rectangle(x1, 2, x2, H-2, fill=colour, outline="")
        s_val = int(sig * 9)
        self._vfd_sig_label.config(text=f"S{s_val}  {sig*100:.0f}%")

    # ── Monitor rows update ───────────────────────────────────────────
    def _update_monitor_rows(self):
        if not self._monitor_panel_built:
            return
        pri_ch   = self._cfg.current_channel
        ch_signal = {}
        with self._core._rx_lock:
            for cs, rx_state in self._core._rx_states.items():
                if rx_state.active:
                    ch_signal[pri_ch] = rx_state.signal
        for ch_id, row_d in self._mon_rows.items():
            is_pri = (ch_id == pri_ch)
            is_mon = self._monitored.get(ch_id, False)
            is_rx  = ch_id in ch_signal
            is_tx  = self._core._tx_active and is_pri
            # Background
            if is_tx:
                bg = C["mon_tx"]
            elif is_pri:
                bg = C["mon_active"]
            elif is_mon:
                bg = "#0e180e"
            else:
                bg = C["mon_idle"]
            row_d["row"].config(bg=bg)
            for key in ("led", "ch_lbl", "name_lbl", "status_lbl"):
                row_d[key].config(bg=bg)
            row_d["mon_btn"].config(bg=bg)
            # LED colour
            if is_tx:
                led_col = C["red"] if self._blink_state else C["red_dim"]
            elif is_rx and is_pri:
                led_col = C["green"] if self._blink_state else C["green_dim"]
            elif is_pri:
                led_col = C["green"]
            elif is_mon:
                led_col = C["amber_dim"]
            else:
                led_col = C["text_dim"]
            row_d["led"].config(fg=led_col)
            # Status text
            if is_tx:
                status, s_fg = "TX", C["red"]
            elif is_rx and is_pri:
                status, s_fg = "RX", C["green"]
            elif is_pri:
                status, s_fg = "PRI", C["lcd_fg"]
            elif is_mon:
                status, s_fg = "MON", C["amber"]
            else:
                status, s_fg = "---", C["text_dim"]
            row_d["status_lbl"].config(text=status, fg=s_fg, bg=bg)
            row_d["ch_lbl"].config(
                fg=C["lcd_fg"] if is_pri else C["text_mid"])
            row_d["name_lbl"].config(
                fg=C["lcd_hi"] if is_pri else C["text_dim"])
            sig = ch_signal.get(ch_id, 0.0) if is_pri else 0.0
            self._draw_monitor_sig(row_d["sig_canvas"], sig, bg)

    # ── Callbacks from core ───────────────────────────────────────────
    def _on_ptt_change(self, active: bool):
        self._gui_queue.put(lambda: self._update_ptt_visual(active))

    def _on_signal_change(self, strength: float):
        self._signal = strength
        if strength > self._signal_peak:
            self._signal_peak = strength
            self._peak_hold_ts = time.time()

    def _on_member_update(self, members: list):
        self._gui_queue.put(lambda: self._refresh_roster(members))

    def _on_rx_start(self, callsign: str):
        self._rx_from = callsign

    def _on_rx_end(self, callsign: str):
        if self._rx_from == callsign:
            self._rx_from = ""

    def _on_rx_end_with_duration(self, callsign: str, duration: float):
        self._on_rx_end(callsign)
        self._gui_queue.put(lambda: self._add_history(
            "rx", callsign, self._cfg.current_channel, duration))

    def _on_profile_change(self, profile_id):
        if hasattr(self, '_vfd_profile'):
            self._vfd_profile.config(text=str(profile_id).upper())

    def _on_status_change(self, msg: str):
        self._status_msg = msg
        # Treat any of these as "connected" state
        connected = any(k in msg for k in (
            "Connected", "Reconnected"))
        def _apply():
            col = C["green"] if connected else C["red"]
            self._lbl_conn.config(
                text="ONLINE" if connected else "OFFLINE", fg=col)
            self._dot_canvas.itemconfig(self._dot_oval, fill=col)
            if hasattr(self, '_ctrl_connect_btn'):
                self._ctrl_connect_btn.config(
                    text="DISCONNECT" if connected else "CONNECT",
                    fg=C["red_bright"] if connected else C["cyan"])
            self._add_history("sys", "SYS", self._cfg.current_channel, note=msg)
        self.root.after(0, _apply)

    def _update_ptt_visual(self, active: bool):
        if active:
            self._ptt_btn.config(
                bg=C["ptt_hot"], fg="#ffffff",
                text="◉   TRANSMITTING   ◉")
            self._add_history(
                "tx", self._cfg.callsign, self._cfg.current_channel)
        else:
            self._ptt_btn.config(
                bg=C["ptt_idle"], fg=C["green"],
                text="◉   PUSH TO TALK   ◉")

    def _refresh_monitor_members(self, members: list):
        pass  # handled by _on_member_update → _refresh_roster

    # ── User actions ──────────────────────────────────────────────────
    def _toggle_monitor(self, ch_id: int, var: tk.BooleanVar):
        self._monitored[ch_id] = var.get()
        if var.get():
            self._core.join_channel(ch_id, monitor=True)
            self._add_history("sys", "MON", ch_id,
                              note=f"CH{ch_id:02d} monitor ON")
        else:
            self._core._monitored_channels.discard(ch_id)
            self._core._send_leave(ch_id)
            self._add_history("sys", "MON", ch_id,
                              note=f"CH{ch_id:02d} monitor OFF")

    def _select_primary(self, ch_id: int):
        if ch_id == self._cfg.current_channel:
            return
        self._add_history("sys", "SYS", ch_id,
                          note=f"Primary → CH{ch_id:02d}")
        self._core.change_channel(ch_id)
        self._update_monitor_rows()

    def _kb_ptt_on(self, event):
        if not self._ptt_held:
            self._ptt_held = True
            self._core.ptt_on()

    def _kb_ptt_off(self, event):
        if self._ptt_held:
            self._ptt_held = False
            self._core.ptt_off()

    def _btn_ptt_on(self, event):
        self._ptt_held = True
        self._core.ptt_on()

    def _btn_ptt_off(self, event):
        self._ptt_held = False
        self._core.ptt_off()

    def _ch_prev(self):
        chs = self._get_accessible_channels()
        ids = [c.id for c in chs]
        cur = self._cfg.current_channel
        if cur in ids:
            self._select_primary(ids[(ids.index(cur) - 1) % len(ids)])

    def _ch_next(self):
        chs = self._get_accessible_channels()
        ids = [c.id for c in chs]
        cur = self._cfg.current_channel
        if cur in ids:
            self._select_primary(ids[(ids.index(cur) + 1) % len(ids)])

    def _do_radio_check(self):
        target = (simpledialog.askstring(
            "Radio Check",
            "Call which station?\n(leave blank for all net)",
            parent=self.root) or "").strip()
        self._core.send_radio_check(target)
        self._add_history("sys", "RADIO-CHK",
                          self._cfg.current_channel,
                          note=f"→ {target or 'ALL STATIONS'}")

    def _toggle_connect(self):
        if self._core._connected:
            self._core.disconnect()
            self._ctrl_connect_btn.config(text="CONNECT", fg=C["cyan"])
        else:
            try:
                self._core.connect(self._assigned_channels)
                self._build_monitor_rows()
                self._ctrl_connect_btn.config(
                    text="DISCONNECT", fg=C["red_bright"])
            except Exception as e:
                messagebox.showerror("Connect Failed", str(e), parent=self.root)

    def _on_vol_change(self, val):
        v = float(val)
        self._cfg.output_volume    = v
        self._core._effects.volume = v
        self._lbl_vol.config(text=f"{v:.1f}×")

    def _on_sql_change(self, val):
        v = float(val)
        self._cfg.squelch           = v
        self._core._effects.squelch = v
        self._lbl_sql.config(text=f"{v:.2f}")

    def _on_vox_change(self, val):
        v = float(val)
        self._cfg.vox_threshold = v
        self._lbl_vox.config(text=f"{v:.3f}")

    def _get_accessible_channels(self) -> List['ChannelDef']:
        all_chs  = self._cfg_mgr.channel_list()
        assigned = self._core.assigned_channels
        if not assigned:
            return all_chs
        return [ch for ch in all_chs if ch.id in assigned]

    # ── Settings (re-uses existing _open_settings) ────────────────────
    def _open_settings(self):
        win = tk.Toplevel(self.root)
        win.title("Settings — MilRadio")
        win.configure(bg=C["chassis"])
        win.grab_set()
        frame = tk.Frame(win, bg=C["chassis"], padx=16, pady=12)
        frame.pack()
        tk.Label(frame, text="MILRADIO CONFIGURATION",
                 font=_FONT_LCD_MED, bg=C["chassis"],
                 fg=C["lcd_fg"]).grid(row=0, columnspan=2, pady=(0, 10))
        settings = [
            ("callsign",          self._cfg.callsign),
            ("server_host",       self._cfg.server_host),
            ("server_port",       self._cfg.server_port),
            ("squelch",           self._cfg.squelch),
            ("output_volume",     self._cfg.output_volume),
            ("range_max_km",      self._cfg.range_max_km),
            ("range_enabled",     self._cfg.range_enabled),
            ("tak_enabled",       self._cfg.tak_enabled),
            ("tak_server_url",    self._cfg.tak_server_url),
            ("sword_enabled",     self._cfg.sword_enabled),
            ("sword_gateway_url", self._cfg.sword_gateway_url),
            ("vox_enabled",       self._cfg.vox_enabled),
            ("vox_threshold",     self._cfg.vox_threshold),
        ]
        vars_ = {}
        for i, (name, val) in enumerate(settings):
            tk.Label(frame, text=name, font=_FONT_MONO,
                     bg=C["chassis"], fg=C["text_dim"],
                     width=22, anchor="e").grid(
                         row=i+1, column=0, padx=(0, 8), pady=2)
            var = tk.StringVar(value=str(val))
            tk.Entry(frame, textvariable=var, font=_FONT_MONO,
                     bg=C["recess"], fg=C["lcd_fg"],
                     insertbackground=C["lcd_fg"],
                     relief="flat", width=32).grid(row=i+1, column=1, pady=2)
            vars_[name] = var

        # PTT key picker
        n_settings = len(settings)
        tk.Label(frame, text="── PTT KEY",
                 font=_FONT_MONO, bg=C["chassis"],
                 fg=C["accent"]).grid(
                     row=n_settings+1, columnspan=2,
                     pady=(10, 4), sticky="w")
        tk.Label(frame, text="ptt_key",
                 font=_FONT_MONO, bg=C["chassis"],
                 fg=C["text_dim"], width=22, anchor="e").grid(
                     row=n_settings+2, column=0, padx=(0, 8), pady=2)
        _ptt_binding = [False]
        ptt_key_var  = tk.StringVar(value=self._cfg.ptt_key)
        ptt_row = tk.Frame(frame, bg=C["chassis"])
        ptt_row.grid(row=n_settings+2, column=1, pady=2, sticky="w")
        ptt_lbl = tk.Label(ptt_row, textvariable=ptt_key_var,
            font=("Courier", 11, "bold"),
            bg=C["recess"], fg=C["lcd_fg"],
            width=14, anchor="w", padx=8, pady=4)
        ptt_lbl.pack(side="left", padx=(0, 6))
        def _start_ptt_bind():
            _ptt_binding[0] = True
            ptt_bind_btn.config(text="PRESS A KEY…",
                                fg=C["amber"], bg=C["amber_dim"])
            ptt_lbl.config(fg=C["amber"])
        def _on_key_for_ptt(event):
            if not _ptt_binding[0]:
                return
            _ptt_binding[0] = False
            ks = event.keysym.lower()
            name_map = {
                "space": "space",
                "control_l": "ctrl", "control_r": "ctrl",
                "alt_l": "alt", "alt_r": "alt",
                "shift_l": "shift", "shift_r": "shift",
                "f1":"f1","f2":"f2","f3":"f3","f4":"f4",
                "f5":"f5","f6":"f6","f7":"f7","f8":"f8",
                "f9":"f9","f10":"f10","f11":"f11","f12":"f12",
                "caps_lock":"caps_lock","kp_0":"num_0",
                "escape": None,
            }
            mapped = name_map.get(ks, ks)
            if mapped is None:
                ptt_bind_btn.config(text="BIND KEY",
                                    fg=C["cyan"], bg=C["chassis3"])
                ptt_lbl.config(fg=C["lcd_fg"])
                return
            ptt_key_var.set(mapped)
            ptt_bind_btn.config(text="BIND KEY",
                                fg=C["cyan"], bg=C["chassis3"])
            ptt_lbl.config(fg=C["green"])
            win.after(600, lambda: ptt_lbl.config(fg=C["lcd_fg"]))
        ptt_bind_btn = tk.Button(ptt_row, text="BIND KEY",
            font=_FONT_LABEL, bg=C["chassis3"], fg=C["cyan"],
            activebackground=C["border_hi"], activeforeground=C["lcd_fg"],
            relief="flat", bd=0, padx=8, pady=4,
            cursor="hand2", command=_start_ptt_bind)
        ptt_bind_btn.pack(side="left")
        tk.Label(ptt_row, text="(ESC=cancel)",
            font=("Courier", 7), bg=C["chassis"],
            fg=C["text_dim"]).pack(side="left", padx=(6, 0))
        win.bind("<KeyPress>", _on_key_for_ptt)

        # Audio device selector
        devices = self._core.get_audio_devices()
        tk.Label(frame, text="── AUDIO DEVICES",
                 font=_FONT_MONO, bg=C["chassis"],
                 fg=C["accent"]).grid(
                     row=n_settings+4, columnspan=2,
                     pady=(10, 4), sticky="w")
        in_names  = ["Default"] + [f"[{i}] {n}" for i, n in devices["inputs"]]
        out_names = ["Default"] + [f"[{i}] {n}" for i, n in devices["outputs"]]
        tk.Label(frame, text="input_device",
                 font=_FONT_MONO, bg=C["chassis"],
                 fg=C["text_dim"], width=22, anchor="e").grid(
                     row=n_settings+5, column=0, padx=(0, 8), pady=2)
        in_var = tk.StringVar()
        cur_in = self._cfg.audio_input_device
        in_var.set(next((n for i, n in devices["inputs"]
                         if i == cur_in), "Default"))
        in_drop = ttk.Combobox(frame, textvariable=in_var,
                                values=in_names, font=_FONT_MONO,
                                width=32, state="readonly")
        in_drop.grid(row=n_settings+5, column=1, pady=2)
        tk.Label(frame, text="output_device",
                 font=_FONT_MONO, bg=C["chassis"],
                 fg=C["text_dim"], width=22, anchor="e").grid(
                     row=n_settings+6, column=0, padx=(0, 8), pady=2)
        out_var = tk.StringVar()
        cur_out = self._cfg.audio_output_device
        out_var.set(next((n for i, n in devices["outputs"]
                          if i == cur_out), "Default"))
        out_drop = ttk.Combobox(frame, textvariable=out_var,
                                 values=out_names, font=_FONT_MONO,
                                 width=32, state="readonly")
        out_drop.grid(row=n_settings+6, column=1, pady=2)

        def save():
            for name, var in vars_.items():
                raw = var.get().strip()
                cur = getattr(self._cfg, name)
                try:
                    if isinstance(cur, bool):
                        setattr(self._cfg, name,
                                raw.lower() in ("true", "1", "yes", "on"))
                    elif isinstance(cur, int):
                        setattr(self._cfg, name, int(raw))
                    elif isinstance(cur, float):
                        setattr(self._cfg, name, float(raw))
                    else:
                        setattr(self._cfg, name, raw)
                except ValueError:
                    pass
            new_ptt = ptt_key_var.get().strip().lower()
            if new_ptt and new_ptt != self._cfg.ptt_key:
                self._cfg.ptt_key       = new_ptt
                self._core._cfg.ptt_key = new_ptt
                self._core._setup_ptt_hotkey()
                if hasattr(self, '_ptt_hint_lbl'):
                    self._ptt_hint_lbl.config(
                        text=f"HOLD BUTTON  or  HOLD {new_ptt.upper()}  to transmit")
            in_sel, out_sel = in_var.get(), out_var.get()
            in_idx = out_idx = -1
            for i, n in devices["inputs"]:
                if f"[{i}] {n}" == in_sel or n == in_sel:
                    in_idx = i
            for i, n in devices["outputs"]:
                if f"[{i}] {n}" == out_sel or n == out_sel:
                    out_idx = i
            if (in_idx != self._cfg.audio_input_device
                    or out_idx != self._cfg.audio_output_device):
                self._core.set_audio_devices(in_idx, out_idx)
            self._cfg_mgr.save()
            self._vfd_callsign.config(text=self._cfg.callsign)
            win.destroy()
            self._add_history("sys", "SYS", 0, note="Settings saved")

        tk.Button(frame, text="SAVE & CLOSE", command=save,
                  font=_FONT_BTN, bg=C["ptt_idle"],
                  fg=C["green"], relief="flat", padx=14, pady=6,
                  cursor="hand2").grid(
                      row=n_settings+8, columnspan=2, pady=(12, 0))

    # ── App entry ─────────────────────────────────────────────────────
    def _on_close(self):
        self._core.disconnect()
        self._cfg_mgr.save()
        self.root.destroy()

    def run(self, assigned_channels: List[int] = None):
        self._add_history("sys", "SYS", 0, note="MilRadio started")
        self._assigned_channels = assigned_channels or []
        try:
            self._core.connect(self._assigned_channels)
            self._build_monitor_rows()
        except Exception as e:
            self._add_history("sys", "SYS", 0, note=f"Connect error: {e}")
        self.root.mainloop()



# ── Helper functions ──────────────────────────────────────────────────
def _raised_frame(parent, **kw) -> tk.Frame:
    f = tk.Frame(parent, bg=C["chassis2"],
                 highlightbackground=C["border_hi"],
                 highlightcolor=C["border_hi"],
                 highlightthickness=1, **kw)
    return f

def _sunken_frame(parent, **kw) -> tk.Frame:
    f = tk.Frame(parent, bg=C["recess"],
                 highlightbackground=C["border_lo"],
                 highlightcolor=C["border_lo"],
                 highlightthickness=1, **kw)
    return f

# ── Entry point ───────────────────────────────────────────────────────
def main():
    import argparse
    logging.basicConfig(
        level   = logging.INFO,
        format  = "%(asctime)s  %(levelname)-7s  %(name)-18s  %(message)s",
        datefmt = "%H:%M:%S",
    )
    parser = argparse.ArgumentParser(description="MilRadio Client")
    parser.add_argument("--config",    default="radio_config.ini")
    parser.add_argument("--callsign",  default=None)
    parser.add_argument("--server",    default=None, help="host:port")
    parser.add_argument("--channel",   type=int, default=None)
    parser.add_argument("--no-gui",    action="store_true")
    parser.add_argument("--debug",     action="store_true")
    args = parser.parse_args()
    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    cfg_path = Path(args.config)
    mgr = ConfigManager(cfg_path)

    overridden = False
    if args.callsign:
        mgr.config.callsign = args.callsign
        overridden = True
    if args.server:
        host, _, port = args.server.partition(":")
        mgr.config.server_host = host
        if port:
            mgr.config.server_port = int(port)
        overridden = True
    if args.channel is not None:
        mgr.config.current_channel = args.channel
        overridden = True

    if overridden:
        mgr.save()

    if args.no_gui:
        client = RadioClientCore(cfg_path)
        client.connect()
        print(f"Connected as [{mgr.config.callsign}] to "
              f"{mgr.config.server_host}:{mgr.config.server_port} "
              f"CH{mgr.config.current_channel:02d}")
        print("Press Ctrl+C to disconnect")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        client.disconnect()
    else:
        if not args.callsign:
            login = ConnectionDialog(cfg_path)
            creds = login.show()
            if not creds:
                sys.exit(0)
            assigned_channels = creds.get("assigned_channels", [])
        else:
            assigned_channels = []
        
        gui = RadioClientGUI(cfg_path)
        gui.run(assigned_channels)

if __name__ == "__main__":
    main()