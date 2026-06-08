"""
TacNet — Radio Relay Server (Updated with All Fixes)
Fixes: Client Tracking, Channel Access, Net Activity Stats
"""
from __future__ import annotations
import socket
import struct
import threading
import time
import logging
import json
import queue
import sys
import os
import traceback
import signal as _signal
import numpy as np
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple, Any, Union
from pathlib import Path
from radio_protocol import (
    Packet, PktType, PacketCodec, ChannelKey, Payloads,
    FLAG_ENCRYPTED, SAMPLE_RATE, FRAME_MS, FRAME_SAMPLES,
    HEADER_SIZE,
)

# ── Compatibility shim: older builds may lack MONITOR_JOIN/LEAVE/PING/PONG ──
# Assigns integer constants directly to PktType so that value-based comparisons
# (pkt.type == PktType.MONITOR_JOIN) work even if the enum predates these values.
try:
    _ = PktType.MONITOR_JOIN
except AttributeError:
    PktType.MONITOR_JOIN  = 0x13   # type: ignore[attr-defined]
    PktType.MONITOR_LEAVE = 0x14   # type: ignore[attr-defined]
    log_early = logging.getLogger("radio.server")
    log_early.warning("radio_protocol missing MONITOR_JOIN — shim active. Rebuild recommended.")
try:
    _ = PktType.PING
except AttributeError:
    PktType.PING = 0xF0            # type: ignore[attr-defined]
    PktType.PONG = 0xF1            # type: ignore[attr-defined]
from radio_effects import RangeModel
from radio_infra import InfrastructureManager, latlon_to_mgrs, parse_freq_mhz
from radio_config import ConfigManager, DEFAULT_CONFIG_PATH, RadioConfig

log = logging.getLogger("radio.server")

# -- Tuning constants -------------------------------------------------
MEMBER_BROADCAST_INTERVAL = 5.0
POSITION_TTL              = 120.0  # 2 min — keepalive re-sends every 30s
CLIENT_TIMEOUT = 60.0   # 60s: fast eviction of crashed/silent clients
RELAY_QUEUE_MAX           = 500
MAX_UDP_PACKET            = 65507

# -- Client state -----------------------------------------------------
# ── Signal Quality Tracker ───────────────────────────────────────────────────

class SignalQualityTracker:
    """
    Per-client rolling signal quality metrics.
    Updated on every relay event; read by /api/signal endpoint.

    Output states:
        clear        — signal >= 0.70, loss <= 5%,  latency <= 120ms
        degraded     — signal >= 0.35, loss <= 25%, latency <= 400ms
        intermittent — signal >= 0.10, loss <= 60%
        lost         — signal <  0.10 or loss >  60%
    """
    # State thresholds
    THRESHOLDS = {
        "clear":        dict(min_signal=0.70, max_loss=0.05,  max_lat=120),
        "degraded":     dict(min_signal=0.35, max_loss=0.25,  max_lat=400),
        "intermittent": dict(min_signal=0.10, max_loss=0.60,  max_lat=9999),
    }
    WINDOW = 20   # rolling window size for averaging

    def __init__(self, callsign: str):
        self.callsign       = callsign
        self._lock          = threading.Lock()
        # Rolling samples
        self._signals: list  = []   # 0.0–1.0 signal strength per received packet
        self._latencies: list = []  # ms RTT (from PONG) — may be sparse
        self._lost_count    = 0     # packets blocked/dropped
        self._total_count   = 0     # total relay attempts for this client
        self._last_update   = time.time()
        # Derived (cached, updated each record())
        self.signal_avg     = 1.0
        self.packet_loss    = 0.0
        self.latency_ms     = 0.0
        self.clarity        = 1.0   # 0–1 derived from signal+loss
        self.state          = "clear"
        self.state_since    = time.time()

    def record(self, signal: float, relayed: bool, latency_ms: float = 0):
        """Record one relay event."""
        with self._lock:
            self._total_count += 1
            if not relayed:
                self._lost_count += 1
            else:
                self._signals.append(max(0.0, min(1.0, signal)))
                if latency_ms > 0:
                    self._latencies.append(latency_ms)
            # Keep rolling window
            if len(self._signals) > self.WINDOW:
                self._signals = self._signals[-self.WINDOW:]
            if len(self._latencies) > self.WINDOW:
                self._latencies = self._latencies[-self.WINDOW:]
            self._last_update = time.time()
            self._recompute()

    def _recompute(self):
        """Recompute derived metrics. Called under lock."""
        self.signal_avg  = (sum(self._signals) / len(self._signals)
                            if self._signals else 1.0)
        total = self._total_count or 1
        recent_window = self.WINDOW
        # Loss rate over recent window
        recent_lost = max(0, self._lost_count - max(0, total - recent_window))
        self.packet_loss = min(1.0, recent_lost / recent_window)
        self.latency_ms  = (sum(self._latencies) / len(self._latencies)
                            if self._latencies else 0.0)
        # Clarity: weighted combination of signal and loss
        self.clarity = max(0.0, self.signal_avg * (1.0 - self.packet_loss * 0.8))
        # Determine state
        prev_state = self.state
        if self.signal_avg >= 0.70 and self.packet_loss <= 0.05 and self.latency_ms <= 120:
            self.state = "clear"
        elif self.signal_avg >= 0.35 and self.packet_loss <= 0.25:
            self.state = "degraded"
        elif self.signal_avg >= 0.10 and self.packet_loss <= 0.60:
            self.state = "intermittent"
        else:
            self.state = "lost"
        if self.state != prev_state:
            self.state_since = time.time()

    def to_dict(self) -> dict:
        with self._lock:
            return {
                "callsign":    self.callsign,
                "signal_avg":  round(self.signal_avg, 3),
                "packet_loss": round(self.packet_loss, 3),
                "latency_ms":  round(self.latency_ms, 1),
                "clarity":     round(self.clarity, 3),
                "state":       self.state,
                "state_since": round(time.time() - self.state_since, 1),
                "last_update": round(time.time() - self._last_update, 1),
            }

    def stale(self, timeout: float = 15.0) -> bool:
        return (time.time() - self._last_update) > timeout


import heapq
@dataclass(order=True)
class DelayedPacket:
    delivery_time: float
    data: bytes = field(compare=False)
    addr: Tuple[str, int] = field(compare=False)
    callsign: str = field(compare=False)
    signal: float = field(compare=False)
    latency: float = field(compare=False)

class DelayedRelayManager(threading.Thread):
    """
    High-performance background thread that manages delayed packet delivery.
    Uses a priority queue (heapq) to minimize wakeups and jitter.
    """
    def __init__(self, sock, stats_ref, clients_ref, clients_lock, record_fn):
        super().__init__(name="DelayedRelay", daemon=True)
        self._sock = sock
        self._stats = stats_ref
        self._clients = clients_ref
        self._lock = clients_lock
        self._record_fn = record_fn
        self._queue = []
        self._cv = threading.Condition()
        self._running = True

    def add_job(self, delay_s: float, data: bytes, addr: Tuple[str, int], 
                callsign: str, signal: float, latency: float):
        with self._cv:
            heapq.heappush(self._queue, DelayedPacket(
                time.time() + delay_s, data, addr, callsign, signal, latency
            ))
            self._cv.notify()

    def run(self):
        while self._running:
            with self._cv:
                if not self._queue:
                    self._cv.wait(timeout=1.0)
                    if not self._queue: continue
                
                now = time.time()
                next_pkt = self._queue[0]
                
                if next_pkt.delivery_time <= now:
                    heapq.heappop(self._queue)
                else:
                    wait_time = next_pkt.delivery_time - now
                    self._cv.wait(timeout=wait_time)
                    continue

            # Deliver packet
            try:
                self._sock.sendto(next_pkt.data, next_pkt.addr)
                with self._lock:
                    r = self._clients.get(next_pkt.callsign)
                    if r: r.packets_tx += 1
                self._stats["packets_relayed"] += 1
                self._stats["bytes_tx"] += len(next_pkt.data)
                self._record_fn(next_pkt.callsign, next_pkt.signal, relayed=True, latency_ms=next_pkt.latency)
            except Exception:
                pass


@dataclass
class ClientState:
    callsign:         str
    udp_addr:         Tuple[str, int]
    tcp_conn:         Optional[socket.socket] = field(default=None, repr=False)
    channel:          int                    = -1
    monitor_channels: List[int]              = field(default_factory=list)  # RX-only channels
    lat:              float                  = 0.0
    lon:              float                  = 0.0
    alt:              float                  = 0.0
    radio_profile:    str                    = "handheld"
    last_seen:        float                  = field(default_factory=time.time)
    last_position:    float                  = 0.0
    evicted:          bool                   = False
    tx_active:        bool                   = False
    tx_start:         float                  = 0.0
    packets_rx:       int                    = 0
    packets_tx:       int                    = 0
    pos_override_until: float                 = 0.0  # Admin manual move lock
    _lock:            threading.Lock         = field(default_factory=threading.Lock, repr=False)

    def to_dict(self) -> dict:
        with self._lock:
            return {
                "callsign": self.callsign,
                "channel": self.channel,
                "monitor_channels": list(self.monitor_channels),
                "lat": self.lat,
                "lon": self.lon,
                "alt": self.alt,
                "radio_profile": self.radio_profile,
                "tx_active": self.tx_active,
                "last_seen": self.last_seen,
                "evicted": self.evicted,
                "pos_override_until": self.pos_override_until,
                "packets_rx": self.packets_rx,
                "packets_tx": self.packets_tx
            }

    def update_seen(self):
        self.last_seen = time.time()

    def update_position(self, lat: float, lon: float, alt: float, is_admin: bool = False):
        with self._lock:
            if is_admin:
                if lat == 0 and lon == 0:
                    self.evicted = True
                else:
                    self.evicted = False
                # Lock position for 300 seconds against client GPS updates
                self.pos_override_until = time.time() + 300.0
            
            if not is_admin and self.evicted:
                return # Ignore client updates if evicted by admin
                
            self.lat           = lat
            self.lon           = lon
            self.alt           = alt
            self.last_position = time.time()

    def position_fresh(self) -> bool:
        return (time.time() - self.last_position) < POSITION_TTL

    def as_member_dict(self) -> dict:
        return {
            "callsign": self.callsign,
            "channel":  self.channel,
            "lat":      self.lat,
            "lon":      self.lon,
            "signal":   1.0,
        }

# -- Relay job --------------------------------------------------------
@dataclass
class RelayJob:
    pkt:         Packet
    sender_addr: Tuple[str, int]
    channel:     int

# -- Server -----------------------------------------------------------
# ── EW / Degradation Engine ───────────────────────────────────────────────────
import random as _random

class EWEngine:
    """
    Exercise EW/degradation effects applied at the relay layer.
    All effects are additive and can be stacked.  Thread-safe.

    Effects:
      jam      - RF jamming: drop voice packets on target channels
      ew       - EW degradation: extra noise injected into signal strength
      cyber    - Cyber: artificial packet delay + drop rate
      weather  - Weather: atmospheric attenuation multiplier
      terrain  - Terrain masking: forced blockage between callsign pairs
      emcon    - EMCON: radio silence — drop PTT_ON from targets
    """

    def __init__(self):
        self._lock   = threading.Lock()
        self._active: dict = {}   # effect_id -> effect_dict
        self._log    = []         # list of {ts, msg} for admin UI

    # ── Management ─────────────────────────────────────────────────────────────
    def apply_effect(self, effect_type: str, params: dict, label: str = "") -> str:
        """Add or update an effect. Returns the effect_id."""
        eid = f"{effect_type}_{int(time.time()*1000)}"
        with self._lock:
            self._active[eid] = {
                "id":       eid,
                "type":     effect_type,
                "label":    label or effect_type.upper(),
                "params":   params,
                "applied":  time.time(),
                "hits":     0,
            }
        self._ew_log(f"EW APPLY [{effect_type.upper()}] {label}: {params}")
        return eid

    def clear_effect(self, eid: str) -> bool:
        with self._lock:
            if eid in self._active:
                del self._active[eid]
                self._ew_log(f"EW CLEAR {eid}")
                return True
        return False

    def clear_all(self):
        with self._lock:
            n = len(self._active)
            self._active.clear()
        self._ew_log(f"EW CLEAR ALL ({n} effects removed)")

    def get_status(self) -> dict:
        with self._lock:
            active = list(self._active.values())
        return {"active": active, "count": len(active), "log": self._log[-40:]}

    def _ew_log(self, msg: str):
        ts = time.strftime("%H:%M:%S")
        entry = {"ts": ts, "msg": msg}
        self._log.append(entry)
        if len(self._log) > 200:
            self._log = self._log[-200:]
        log.info("EWEngine: %s", msg)

    # ── Relay-time checks ──────────────────────────────────────────────────────
    def should_block_ptt(self, callsign: str, channel: int) -> bool:
        """Returns True if PTT_ON should be suppressed (EMCON / jamming)."""
        with self._lock:
            for eff in self._active.values():
                t = eff["type"]
                p = eff["params"]
                # EMCON: silence specific callsigns or channels
                if t == "emcon":
                    chs = p.get("channels", [])
                    css = p.get("callsigns", [])
                    ch_match = (not chs) or (channel in chs)
                    cs_match = (not css) or (callsign in css)
                    if ch_match and cs_match:
                        eff["hits"] += 1
                        return True
                # Full jamming (intensity=1.0) blocks PTT too
                if t == "jam" and channel in p.get("channels", []):
                    if p.get("intensity", 1.0) >= 1.0:
                        eff["hits"] += 1
                        return True
        return False

    def is_client_jammed(self, callsign: str, channel: int) -> bool:
        """Returns True if there is an active jam or emcon effect affecting this client/channel."""
        with self._lock:
            for eff in self._active.values():
                t = eff["type"]
                p = eff["params"]
                if t in ("jam", "ew", "emcon"):
                    chs = p.get("channels", [])
                    css = p.get("callsigns", [])
                    ch_match = (not chs) or (channel in chs)
                    cs_match = (not css) or (callsign in css)
                    if ch_match and cs_match:
                        if p.get("intensity", 1.0) > 0.0 or p.get("noise_add", 0.0) > 0.0:
                            return True
        return False

    def filter_relay(self, pkt, channel: int, sender_cs: str,
                     recipient_cs: str) -> tuple:
        """
        Called for each relay candidate.
        Returns (should_relay: bool, signal_multiplier: float, extra_noise: float)
        """
        relay     = True
        sig_mult  = 1.0
        extra_noise = 0.0

        with self._lock:
            for eff in self._active.values():
                t = eff["type"]
                p = eff["params"]

                if t == "jam":
                    chs = p.get("channels", [])
                    if (not chs) or (channel in chs):
                        intensity = p.get("intensity", 1.0)
                        if _random.random() < intensity:
                            eff["hits"] += 1
                            return False, 0.0, 0.0   # block immediately

                elif t == "ew":
                    chs = p.get("channels", [])
                    if (not chs) or (channel in chs):
                        noise_add = p.get("noise_add", 0.2)
                        dist_add  = p.get("distortion_add", 0.0)
                        # Reduce signal + add noise flag for caller
                        sig_mult  *= max(0.0, 1.0 - noise_add * 0.8)
                        extra_noise += noise_add
                        eff["hits"] += 1

                elif t == "cyber":
                    drop = p.get("drop_rate", 0.0)
                    if _random.random() < drop:
                        eff["hits"] += 1
                        return False, 0.0, 0.0

                elif t == "weather":
                    chs = p.get("channels", [])
                    if not chs or channel in chs:
                        sev = p.get("severity", 0.3)
                        sig_mult *= max(0.0, 1.0 - sev)
                        eff["hits"] += 1

                elif t == "terrain":
                    # blocked_pairs: list of [cs1, cs2] — no relay
                    for pair in p.get("blocked_pairs", []):
                        if set(pair) == {sender_cs, recipient_cs}:
                            eff["hits"] += 1
                            return False, 0.0, 0.0
                    # partial_pairs: list of {pair:[cs1,cs2], attenuation:0.7}
                    for pp in p.get("partial_pairs", []):
                        if set(pp.get("pair", [])) == {sender_cs, recipient_cs}:
                            sig_mult *= max(0.0, 1.0 - pp.get("attenuation", 0.5))
                            eff["hits"] += 1

        return relay, sig_mult, extra_noise

    def apply_cyber_delay(self) -> float:
        """Return delay in seconds for cyber effects (0 if none active)."""
        with self._lock:
            for eff in self._active.values():
                if eff["type"] == "cyber":
                    return eff["params"].get("delay_ms", 0) / 1000.0
        return 0.0


@dataclass
class RemoteNode:
    node_id: str
    status: str = "offline"
    client_pid: int = None
    adopted: bool = False
    last_seen: float = 0.0
    command: str = "none"
    callsign: str = ""
    channel: int = None

class NodeManager:
    def __init__(self):
        self.nodes = {}
        self._lock = threading.Lock()

    def get_all(self):
        with self._lock:
            # Clean up old nodes (> 15 seconds)
            now = time.time()
            for n in list(self.nodes.values()):
                if now - n.last_seen > 30.0:
                    n.status = "offline"
                    n.client_pid = None
                    n.adopted = False
            return [vars(n) for n in self.nodes.values()]

    def update_node(self, node_id: str, status: str, pid: int, adopted: bool = False):
        with self._lock:
            if node_id not in self.nodes:
                self.nodes[node_id] = RemoteNode(node_id=node_id)
            n = self.nodes[node_id]
            n.status = status
            n.client_pid = pid
            n.adopted = adopted
            n.last_seen = time.time()
            
            # If the launcher retrieved our command, reset it to none (unless it's continuous, but polling is enough)
            # Actually, we should return the command and clear it.
            cmd = n.command
            cs = n.callsign
            ch = n.channel
            
            if cmd != "none":
                n.command = "none"
                
            return {"command": cmd, "callsign": cs, "channel": ch}

    def assign_config(self, node_id: str, callsign: str, channel: int, pid: int = None):
        callsign = callsign.upper() if callsign else ""
        with self._lock:
            if node_id not in self.nodes:
                self.nodes[node_id] = RemoteNode(node_id=node_id)
            self.nodes[node_id].callsign = callsign
            if channel is not None:
                self.nodes[node_id].channel = channel
            if pid:
                self.nodes[node_id].client_pid = pid
                self.nodes[node_id].status = "online"
                self.nodes[node_id].last_seen = time.time()

    def send_command(self, node_id: str, command: Union[str, dict]):
        with self._lock:
            if node_id in self.nodes:
                if isinstance(command, dict):
                    self.nodes[node_id].command = json.dumps(command)
                else:
                    self.nodes[node_id].command = command

class RadioServer:
    def __init__(self, config: Union[Path, ConfigManager] = Path("radio_config.ini"),
                 activity_cb=None, audio_cb=None):
        if isinstance(config, ConfigManager):
            self._cfg_mgr = config
        else:
            self._cfg_mgr = ConfigManager(config)
        self._cfg        = self._cfg_mgr.config
        self._activity_cb = activity_cb
        self._audio_cb    = audio_cb
        self._range      = RangeModel(
            max_range_km      = self._cfg.range_max_km,
            full_quiet_km     = self._cfg.range_full_quiet_km,
            power_w           = self._cfg.transmit_power_w,
            terrain_factor    = self._cfg.terrain_factor,
        )
        # Optional callback for net activity events: cb(msg, kind)
        self._activity_cb = activity_cb
        # Optional recorder (attached via set_recorder after construction)
        self._recorder = None
        self.ew_engine = EWEngine()
        self.node_manager = NodeManager()
        self.infra = InfrastructureManager()
        self._quality: dict = {}
        self._quality_lock = threading.Lock()
        self.infra._range_fn = self._range.signal_from_positions
        self._load_infra_from_config()
        self._sync_weather_state() # Phase 4

        self._codecs: Dict[int, PacketCodec] = {}
        self._build_codecs()
        self._clients:     Dict[str, ClientState] = {}
        self._clients_lock = threading.RLock()
        self._relay_q: queue.Queue[RelayJob] = queue.Queue(maxsize=RELAY_QUEUE_MAX)
        self._monitor_q: queue.Queue[RelayJob] = queue.Queue(maxsize=1000)
        self._udp_sock: Optional[socket.socket] = None
        self._tcp_sock: Optional[socket.socket] = None
        self._running    = False
        self._threads: List[threading.Thread] = []
        self.stats = {
            "packets_rx":    0,
            "packets_relayed": 0,
            "packets_dropped": 0,
            "bytes_rx":      0,
            "bytes_tx":      0,
            "clients_peak":  0,
            "uptime_start":  0.0,
            "load_pct":      0,
            "util_pct":      0,
        }
        self._active_rx = {} # cs -> timestamp
        self._rx_lock = threading.Lock()
        self._pps_rx = 0
        self._pps_tx = 0

        # Feature 4: TX collision tracking — channel -> set of active PTT callsigns
        self._channel_tx: Dict[int, set] = {}    # {channel_id: {callsign, ...}}
        self._channel_tx_lock = threading.Lock()

        # Signal Caching (O(N^2) optimization for Relay Loop)
        self._signal_cache: Dict[Tuple[str, str], float] = {}
        self._signal_lock = threading.Lock()

        # Topology Caching
        self._topology_lock = threading.Lock()
        self._topology_cache = None
        self._topology_cache_ts = 0.0

        # Feature 3: RETRANS BFS relay graph — channel adjacency
        self._retrans_graph: Dict[int, set] = {}  # {channel_id: {linked_channel_ids}}
        self._build_retrans_graph()

    def _build_retrans_graph(self):
        """Feature 3: Parse retrans_links config string into undirected adjacency dict.
        Format: 'CH1-CH2, CH2-CH3' — each pair is bidirectional.
        """
        self._retrans_graph = {}
        links_str = getattr(self._cfg, 'retrans_links', '').strip()
        if not links_str:
            return
        for part in links_str.split(','):
            part = part.strip()
            if '-' not in part:
                continue
            try:
                a_str, b_str = part.split('-', 1)
                a, b = int(a_str.strip()), int(b_str.strip())
                self._retrans_graph.setdefault(a, set()).add(b)
                self._retrans_graph.setdefault(b, set()).add(a)
            except ValueError:
                log.warning(f"Invalid retrans_link entry: '{part}'")
        if self._retrans_graph:
            log.info(f"RETRANS BFS graph: {self._retrans_graph}")

    def _retrans_bfs_channels(self, start_channel: int) -> set:
        """Feature 3: BFS from start_channel. Returns set of all reachable channels ≠ start."""
        visited = {start_channel}
        frontier = {start_channel}
        while frontier:
            next_frontier = set()
            for ch in frontier:
                for nb in self._retrans_graph.get(ch, set()):
                    if nb not in visited:
                        visited.add(nb)
                        next_frontier.add(nb)
            frontier = next_frontier
        visited.discard(start_channel)
        return visited

    def _build_codecs(self):
        for ch_id, ch_def in self._cfg_mgr.channels.items():
            keys = {}
            if ch_def.passphrase:
                keys[ch_id] = ChannelKey(ch_def.passphrase, ch_id)
            self._codecs[ch_id] = PacketCodec(keys)

    def _act(self, msg: str, kind: str = "sys"):
        """Fire a net-activity event to the web admin callback if registered."""
        if self._activity_cb:
            try:
                self._activity_cb(msg, kind)
            except Exception:
                pass

    def set_recorder(self, recorder):
        """Attach a Recorder instance for AAR capture."""
        self._recorder = recorder
        recorder.channel_names = {
            ch_id: ch.name for ch_id, ch in self._cfg_mgr.channels.items()
        }
        log.info("Recorder attached to server")

    def get_codec(self, channel: int) -> PacketCodec:
        if channel not in self._codecs:
            self._codecs[channel] = PacketCodec()
        return self._codecs[channel]

    def start(self):
        if self._running: return
        self._running = True
        self.stats["uptime_start"] = time.time()
        host = self._cfg.bind_address
        port = self._cfg.server_port
        self._udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._udp_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._udp_sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
        self._udp_sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1 << 20)
        self._udp_sock.bind((host, port))
        self._udp_sock.settimeout(1.0)
        self._tcp_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._tcp_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._tcp_sock.bind((host, self._cfg.server_ctrl_port))
        self._tcp_sock.listen(32)
        self._tcp_sock.settimeout(1.0)
        log.info(f"Server listening on {host}:{port} (UDP) / {host}:{self._cfg.server_ctrl_port} (TCP)")
        
        # Start background delayed relay processor
        self._delayed_relay = DelayedRelayManager(
            self._udp_sock, self.stats, self._clients, 
            self._clients_lock, self._record_quality
        )
        self._delayed_relay.start()
        self._threads.append(self._delayed_relay)

        def t(name, target):
            th = threading.Thread(target=target, name=name, daemon=True)
            th.start()
            self._threads.append(th)
        t("udp-rx",       self._udp_rx_loop)
        t("tcp-accept",   self._tcp_accept_loop)
        t("relay",        self._relay_loop)
        t("monitor",      self._monitor_worker_loop)
        t("housekeeping", self._housekeeping_loop)
        t("member-bcast", self._member_broadcast_loop)
        log.info("Server started")

    def _monitor_worker_loop(self):
        """Low-priority thread for Web UI Spectrum and Recording. Decodes Opus in parallel."""
        decoders = {} # cs -> Decoder
        opus_available = True
        
        while self._running:
            try:
                job = self._monitor_q.get(timeout=0.2)
                pkt = job.pkt
                
                # 1. Update Recording (Raw Opus) - Always works
                if self._recorder:
                    try:
                        self._recorder.on_voice(pkt.callsign, pkt.channel, pkt.payload)
                    except Exception: pass
                
                # 2. Decode for Spectrum/Web UI (Universal Codec)
                if self._audio_cb:
                    try:
                        pcm = decode_audio(pkt.payload, pkt.flags)
                        # Convert float32 [-1,1] back to int16 for the Admin Audio Bridge
                        pcm_i16 = (np.clip(pcm, -1.0, 1.0) * 32767).astype(np.int16).tobytes()
                        # Signature: (callsign, channel, pcm_data, signal)
                        self._audio_cb(pkt.callsign, pkt.channel, pcm_i16, 1.0)
                    except Exception as e:
                        log.debug(f"Monitor decode error: {e}")
            except queue.Empty: continue
            except Exception as e:
                log.error(f"Monitor worker error: {e}")

    def stop(self):
        log.info("Server stopping…")
        self._running = False
        for sock in (self._udp_sock, self._tcp_sock):
            try: sock.close()
            except: pass
        for th in self._threads:
            th.join(timeout=2.0)
        log.info("Server stopped")

    def run_forever(self):
        self.start()
        try:
            while self._running: time.sleep(0.5)
        except KeyboardInterrupt: pass
        finally: self.stop()

    def inject_packet(self, pkt: Packet, addr: Optional[Tuple[str, int]] = None):
        """
        Inject a packet into the server processing logic.
        Used by the Web Bridge to feed packets from mobile browsers.
        """
        if not self._running: return
        # If no address provided, use a placeholder
        addr = addr or ("127.0.0.1", 0)
        # Process as if it came from UDP
        self._handle_udp_packet(pkt, addr)

    def join(self):
        for th in self._threads: th.join()

    def _udp_rx_loop(self):
        codec_default = PacketCodec()
        while self._running:
            try: 
                data, addr = self._udp_sock.recvfrom(MAX_UDP_PACKET)
            except socket.timeout: 
                continue
            except ConnectionResetError:
                # Ignore Windows WSAECONNRESET (10054) caused by ICMP Port Unreachable
                continue
            except OSError: 
                if not self._running:
                    break
                continue
            self.stats["packets_rx"] += 1
            self.stats["bytes_rx"] += len(data)
            pkt = None
            if len(data) >= 8:
                try:
                    channel = struct.unpack_from("!h", data, 6)[0]
                    pkt = self.get_codec(channel).decode(data)
                except Exception as e: 
                    log.debug(f"Packet unpack error: {e}")
            if pkt is None:
                pkt = codec_default.decode(data)
            if pkt is None:
                log.info(f"SYNC: Unreadable/Rejected packet from {addr} (len={len(data)})")
                continue
            pkt.sender_addr = addr
            self._handle_udp_packet(pkt, addr)

    def _handle_udp_packet(self, pkt: Packet, addr: Tuple[str, int]):
        pkt.callsign = pkt.callsign.upper()

        # Feature 3: Anti-loop guard — discard RETRANS-relayed packets to prevent storms
        if pkt.callsign.endswith(" [R]"):
            return

        client = self._get_or_register_client(pkt.callsign, addr)
        client.update_seen()
        client.packets_rx += 1

        if pkt.type == PktType.VOICE:
            try:
                self._relay_q.put_nowait(RelayJob(pkt, addr, pkt.channel))
            except queue.Full:
                self.stats["packets_dropped"] += 1

            # Feature 3: BFS RETRANS relay to linked channels
            retrans_channels = self._retrans_bfs_channels(pkt.channel)
            for target_ch in retrans_channels:
                relay_cs = pkt.callsign[:12] + " [R]"
                retrans_pkt = Packet(
                    type=pkt.type, channel=target_ch, callsign=relay_cs,
                    seq=pkt.seq, ts_ms=pkt.ts_ms, flags=pkt.flags, payload=pkt.payload
                )
                try:
                    self._relay_q.put_nowait(RelayJob(retrans_pkt, addr, target_ch))
                except queue.Full:
                    pass

        elif pkt.type == PktType.PTT_ON:
            # Check EW/EMCON — block PTT if jamming or silence order active
            if self.ew_engine.should_block_ptt(pkt.callsign, pkt.channel):
                log.info("EW: PTT_ON suppressed [%s] CH%02d", pkt.callsign, pkt.channel)
                return
            client.tx_active = True
            client.tx_start  = time.time()
            msg = f"PTT ON  [{pkt.callsign}] CH{pkt.channel:02d}"
            log.info(msg)
            self._act(msg, "tx")
            # Register as actively transmitting on this channel
            with self._channel_tx_lock:
                self._channel_tx.setdefault(pkt.channel, set()).add(pkt.callsign)
                collision_count = len(self._channel_tx.get(pkt.channel, set()))
            if collision_count > 1:
                log.info("COLLISION: %d simultaneous TX on CH%02d: %s",
                         collision_count, pkt.channel,
                         self._channel_tx.get(pkt.channel))
                self._act(f"COLLISION: {collision_count} simultaneous TX on CH{pkt.channel:02d}", "warn")
            if self._recorder:
                try:
                    self._recorder.on_ptt_on(pkt.callsign, pkt.channel)
                except Exception:
                    pass
            self._relay_control(pkt, addr)
            # Feature 3: BFS RETRANS relay PTT_ON to linked channels
            for target_ch in self._retrans_bfs_channels(pkt.channel):
                relay_cs = pkt.callsign[:12] + " [R]"
                retrans_pkt = Packet(type=pkt.type, channel=target_ch, callsign=relay_cs,
                                     seq=pkt.seq, ts_ms=pkt.ts_ms, flags=pkt.flags, payload=pkt.payload)
                self._relay_control(retrans_pkt, addr)

        elif pkt.type == PktType.PTT_OFF:
            client.tx_active = False
            duration = time.time() - client.tx_start
            msg = f"PTT OFF [{pkt.callsign}] CH{pkt.channel:02d} ({duration:.1f}s)"
            log.info(msg)
            self._act(msg, "tx")
            # Deregister callsign from the active TX set
            with self._channel_tx_lock:
                txers = self._channel_tx.get(pkt.channel, set())
                txers.discard(pkt.callsign)
                if not txers:
                    self._channel_tx.pop(pkt.channel, None)
            if self._recorder:
                try:
                    self._recorder.on_ptt_off(pkt.callsign, pkt.channel)
                except Exception:
                    pass
            self._relay_control(pkt, addr)
            # Feature 3: BFS RETRANS relay PTT_OFF to linked channels
            for target_ch in self._retrans_bfs_channels(pkt.channel):
                relay_cs = pkt.callsign[:12] + " [R]"
                retrans_pkt = Packet(type=pkt.type, channel=target_ch, callsign=relay_cs,
                                     seq=pkt.seq, ts_ms=pkt.ts_ms, flags=pkt.flags, payload=pkt.payload)
                self._relay_control(retrans_pkt, addr)

        elif pkt.type == PktType.POSITION_UPDATE:
            if len(pkt.payload) >= 24:
                lat, lon, alt = Payloads.parse_position(pkt.payload)
                if lat == 0.0 and lon == 0.0:
                    # Client has no GPS fix — do NOT overwrite an admin-set position.
                    # Check if we already have a valid position from the callsign register.
                    cs_def = self._cfg_mgr.callsigns.get(pkt.callsign)
                    if cs_def and (cs_def.lat or cs_def.lon):
                        # Keep the register position; just refresh the timestamp
                        if not client.position_fresh() or (client.lat == 0 and client.lon == 0):
                            client.update_position(cs_def.lat, cs_def.lon,
                                                   getattr(cs_def, 'alt', 0.0))
                            log.debug(f"POS [{pkt.callsign}] restored from register: "
                                      f"({cs_def.lat:.4f},{cs_def.lon:.4f})")
                        else:
                            # Just touch the timestamp so position stays fresh
                            client.last_position = time.time()
                    # else: client truly has no position — leave as (0,0)
                else:
                    # Valid client GPS position — apply if not overridden by admin
                    if time.time() > client.pos_override_until and not client.evicted:
                        client.update_position(lat, lon, alt)
                        cs_def = self._cfg_mgr.callsigns.get(pkt.callsign)
                        if cs_def:
                            cs_def.lat = lat
                            cs_def.lon = lon
                            cs_def.alt = alt
                    log.debug(f"POS [{pkt.callsign}] ({lat:.4f},{lon:.4f},{alt:.0f}m)")

        elif pkt.type == PktType.CHANNEL_JOIN:
            if not self._check_channel_access(pkt.callsign, pkt.channel, addr):
                return
            client.channel = pkt.channel
            # If this channel was in monitor list, remove it (now it's primary)
            if pkt.channel in client.monitor_channels:
                client.monitor_channels.remove(pkt.channel)
            msg = f"JOIN [{pkt.callsign}] CH{pkt.channel:02d}"
            log.info(msg)
            self._act(msg, "sys")
            # Send member list to the joiner AND broadcast to all channel members
            # so existing clients know someone has (re)joined.
            self._broadcast_member_list_on_channel(pkt.channel)

        elif pkt.type == PktType.CHANNEL_LEAVE:
            old_ch = client.channel
            msg = f"LEAVE [{pkt.callsign}] CH{old_ch:02d}"
            log.info(msg)
            self._act(msg, "sys")
            # Clear primary channel but KEEP client state active (don't evict yet)
            # This allows monitored channels to stay active during primary switches.
            client.channel = 0
            # Notify remaining channel members
            if old_ch >= 0:
                self._broadcast_member_list_on_channel(old_ch)

        elif pkt.type == PktType.MONITOR_JOIN:
            if not self._check_channel_access(pkt.callsign, pkt.channel, addr):
                return
            if pkt.channel not in client.monitor_channels:
                client.monitor_channels.append(pkt.channel)
            log.info(f"MON JOIN [{pkt.callsign}] CH{pkt.channel:02d}")

        elif pkt.type == PktType.MONITOR_LEAVE:
            client.monitor_channels = [
                c for c in client.monitor_channels if c != pkt.channel
            ]
            log.info(f"MON LEAVE [{pkt.callsign}] CH{pkt.channel:02d}")

        elif pkt.type == PktType.PING:
            if len(pkt.payload) >= 12:
                client.lock_left = struct.unpack_from("!I", pkt.payload, 8)[0]
                client.lock_until = time.time() + client.lock_left
            self._send_pong(addr, pkt)

        elif pkt.type == PktType.RADIO_CHECK:
            target = pkt.payload[:16].rstrip(b"\x00").decode("ascii", "replace") if pkt.payload else ""
            msg = f"RADIO CHECK [{pkt.callsign}] → {target or 'ALL'} CH{pkt.channel:02d}"
            log.info(msg)
            self._act(msg, "sys")
            self._handle_radio_check(pkt, addr)

    def _check_channel_access(self, callsign: str, channel: int, addr: Tuple[str, int]) -> bool:
        """Return True if callsign is permitted on channel. Sends ERROR packet and logs if not."""
        cs_def = self._cfg_mgr.callsigns.get(callsign)
        if cs_def and cs_def.channels and channel not in cs_def.channels:
            log.warning(f"ACCESS DENIED [{callsign}] CH{channel:02d} — not in assigned channels")
            # Send error packet back so the client knows
            err_pkt = Packet(
                type     = PktType.ERROR,
                channel  = channel,
                callsign = "SERVER",
                payload  = Payloads.error(f"Not assigned to CH{channel:02d}"),
            )
            try:
                self._udp_sock.sendto(self.get_codec(0).encode(err_pkt), addr)
            except Exception:
                pass
            return False
        return True

    def _tcp_accept_loop(self):
        while self._running:
            try:
                conn, addr = self._tcp_sock.accept()
                log.info(f"TCP control connection from {addr}")
                th = threading.Thread(target=self._tcp_client_loop, args=(conn, addr), daemon=True)
                th.start()
                self._threads.append(th)
            except socket.timeout: continue
            except OSError: break

    def _tcp_client_loop(self, conn: socket.socket, addr: Tuple[str, int]):
        # Use a short read timeout; we check last_seen to decide when to drop.
        conn.settimeout(30.0)
        callsign = None
        try:
            buf = b""
            while self._running:
                try:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    buf += chunk
                    while len(buf) >= HEADER_SIZE:
                        plen    = struct.unpack_from("!H", buf, HEADER_SIZE - 2)[0]
                        pkt_len = HEADER_SIZE + plen
                        if len(buf) < pkt_len:
                            break
                        pkt_data = buf[:pkt_len]
                        buf      = buf[pkt_len:]
                        channel  = struct.unpack_from("!h", pkt_data, 6)[0]
                        pkt      = self.get_codec(channel).decode(pkt_data)
                        if pkt:
                            callsign = pkt.callsign
                            client   = self._get_or_register_client(pkt.callsign, addr)
                            client.tcp_conn = conn
                            self._handle_tcp_packet(pkt, client)
                except socket.timeout:
                    # No data for 30 s — check if client is still alive via last_seen
                    if callsign:
                        with self._clients_lock:
                            c = self._clients.get(callsign)
                        if c and (time.time() - c.last_seen) > CLIENT_TIMEOUT:
                            log.info(f"TCP client {callsign} timed out")
                            break
                    continue
                except Exception as e:
                    log.debug(f"TCP client {addr} error: {e}")
                    break
        finally:
            try:
                conn.close()
            except Exception:
                pass
            if callsign:
                log.info(f"TCP disconnected: {callsign} {addr}")

    def _handle_tcp_packet(self, pkt: Packet, client: ClientState):
        client.update_seen()
        if pkt.type == PktType.CHANNEL_JOIN:
            if not self._check_channel_access(pkt.callsign, pkt.channel, client.udp_addr):
                return
            client.channel = pkt.channel
            if pkt.channel in client.monitor_channels:
                client.monitor_channels.remove(pkt.channel)
            self._send_member_list_to(client)
        elif pkt.type == PktType.CHANNEL_LEAVE:
            client.channel = 0
        elif pkt.type == PktType.MONITOR_JOIN:
            if not self._check_channel_access(pkt.callsign, pkt.channel, client.udp_addr):
                return
            if pkt.channel not in client.monitor_channels:
                client.monitor_channels.append(pkt.channel)
        elif pkt.type == PktType.MONITOR_LEAVE:
            client.monitor_channels = [c for c in client.monitor_channels if c != pkt.channel]
        elif pkt.type == PktType.PING:
            if len(pkt.payload) >= 12:
                client.lock_left = struct.unpack_from("!I", pkt.payload, 8)[0]
                client.lock_until = time.time() + client.lock_left
            if client.tcp_conn:
                self._tcp_send(client, self._make_pong(pkt))
        elif pkt.type == PktType.POSITION_UPDATE:
            if len(pkt.payload) >= 24:
                lat, lon, alt = Payloads.parse_position(pkt.payload)
                if time.time() > client.pos_override_until and (lat != 0 or lon != 0):
                    if not client.evicted: # Only update if not explicitly evicted
                        client.update_position(lat, lon, alt)
                        # Also update callsign register
                        cs_def = self._cfg_mgr.callsigns.get(pkt.callsign)
                        if cs_def:
                            cs_def.lat, cs_def.lon, cs_def.alt = lat, lon, alt

    def _relay_loop(self):
        """High-priority audio relay path (Pure radio-to-radio)."""
        while self._running:
            try:
                try:
                    job = self._relay_q.get(timeout=0.2)
                except queue.Empty:
                    continue
                pkt     = job.pkt
                channel = job.channel

                # 1. Clone to Monitor/Recorder (Low Priority Background)
                try:
                    self._monitor_q.put_nowait(job)
                except queue.Full: pass

                # 1. Identify recipients
                with self._clients_lock:
                    recipients = [
                        c for cs, c in self._clients.items()
                        if cs != pkt.callsign
                        and c.udp_addr != job.sender_addr
                        and (c.channel == channel or channel in c.monitor_channels)
                    ]
                
                if recipients:
                    codec = self.get_codec(channel)
                    encoded_cache = {}

                    # Fetch sender's radio profile signature ID (once per relay job)
                    sig_id = 0.0
                    with self._clients_lock:
                        sender_client = self._clients.get(pkt.callsign)
                    if sender_client:
                        sender_prof_id = sender_client.radio_profile
                        sender_profile = self._cfg_mgr.profiles.get(sender_prof_id)
                        if sender_profile:
                            sig_name = getattr(sender_profile, 'relay_signature', 'none').lower().strip()
                            if sig_name == 'standard':
                                sig_id = 1.0
                            elif sig_name == 'heavy':
                                sig_id = 2.0
                            elif sig_name == 'digital':
                                sig_id = 3.0
                            elif sig_name == 'tactical':
                                sig_id = 4.0

                    for recipient in recipients:
                        # 1. High-Speed Signal Lookup (Fast Path)
                        cache_key = (pkt.callsign, recipient.callsign)
                        # Feature 5: CH99 VEHICLE INTERCOM — always full signal
                        if channel == 99:
                            signal = 1.0
                        else:
                            with self._signal_lock:
                                signal = self._signal_cache.get(cache_key, 1.0)
                        
                        if signal < 0.05:
                            self._record_quality(recipient.callsign, signal, relayed=False)
                            continue

                        # Feature 4: Collision detection — compute severity for this channel
                        with self._channel_tx_lock:
                            simultaneous_tx = len(self._channel_tx.get(channel, set()))
                        # collision_severity: 0=clean, 1=full collision
                        # 2 transmitters = 0.9 severity; 3+ = 1.0
                        collision_severity = 0.0
                        if simultaneous_tx >= 2:
                            collision_severity = min(1.0, 0.5 + (simultaneous_tx - 2) * 0.25 + 0.4)

                        # Build payload: [signal_hdr(8)] + [collision(4, if colliding)] + audio
                        signal_bytes = struct.pack("!ff", signal, sig_id)
                        flags_out = pkt.flags | 0x80  # signal header present
                        if collision_severity > 0.0:
                            collision_bytes = struct.pack("!f", collision_severity)
                            modified_payload = signal_bytes + collision_bytes + pkt.payload
                            flags_out |= 0x40  # FLAG_COLLISION
                        else:
                            modified_payload = signal_bytes + pkt.payload
                        
                        relay_pkt = Packet(
                            type     = pkt.type,
                            channel  = channel,
                            callsign = pkt.callsign,
                            seq      = pkt.seq,
                            ts_ms    = pkt.ts_ms,
                            flags    = flags_out,
                            payload  = modified_payload,
                        )
                        wire = codec.encode(relay_pkt)

                        # -- SEND --
                        try:
                            self._udp_sock.sendto(wire, recipient.udp_addr)
                            with self._clients_lock:
                                r = self._clients.get(recipient.callsign)
                                if r: r.packets_tx += 1
                            self.stats["packets_relayed"] += 1
                            self.stats["bytes_tx"] += len(wire)
                            self._record_quality(recipient.callsign, signal, relayed=True)
                        except: pass
                        
                        with self._rx_lock:
                            self._active_rx[recipient.callsign] = time.time()
                            
                        # Mobile Bridge (Legacy relay path)
                        if self._audio_cb:
                            try:
                                # This handles the (Packet, recipient_cs) signature
                                self._audio_cb(relay_pkt, recipient.callsign)
                            except: pass

                # 3. Handle Monitoring/Recording (PCM Path)
                # Only decode if there's a reason to (recorder running OR monitor listening)
                if (self._recorder and self._recorder._running) or self._audio_cb:
                    try:
                        from radio_protocol import decode_audio
                        # PCM monitoring always uses 1.0 signal (reference)
                        pcm = decode_audio(pkt.payload, pkt.flags)
                        
                        # AAR
                        if self._recorder and self._recorder._running:
                            self._recorder.on_voice_frame(pkt.callsign, channel, pcm, 1.0)
                        
                        # Master Monitor (Dashboard)
                        if self._audio_cb:
                            # This handles the (cs, ch, pcm, signal) signature
                            self._audio_cb(pkt.callsign, channel, pcm, 1.0)
                    except: pass

                # 4. Global Cyber Delay
                cyber_delay = self.ew_engine.apply_cyber_delay()
                if cyber_delay > 0:
                    time.sleep(cyber_delay)

            except Exception as e:
                log.error(f"CRITICAL ERROR IN RELAY LOOP: {e}\n{traceback.format_exc()}")
                time.sleep(0.5)

    def _relay_control(self, pkt: Packet, sender_addr: Tuple[str, int]):
        """Relay PTT_ON/OFF and RADIO_CHECK to primary + monitor subscribers."""
        with self._clients_lock:
            recipients = [
                c for cs, c in self._clients.items()
                if cs != pkt.callsign
                and (c.channel == pkt.channel or pkt.channel in c.monitor_channels)
            ]
        codec = self.get_codec(pkt.channel)
        wire  = codec.encode(pkt)
        for r in recipients:
            try:
                self._udp_sock.sendto(wire, r.udp_addr)
            except Exception:
                pass

    def _load_infra_from_config(self):
        """Rebuild InfrastructureManager state from config fields."""
        import json as _json
        cfg = self._cfg
        try:
            nodes = _json.loads(cfg.infra_nodes_json or "[]")
        except Exception:
            nodes = []
        try:
            satcom_channels = _json.loads(cfg.satcom_channels or "[]")
        except Exception:
            satcom_channels = []
        self.infra.routing_mode              = cfg.routing_mode
        self.infra.routing_min_signal        = cfg.routing_min_signal
        self.infra.routing_prefer_low_latency = cfg.routing_prefer_low_latency
        self.infra.load_from_dict({
            "retrans_nodes": nodes,
            "satcom": {
                "enabled":         cfg.satcom_enabled,
                "latency_ms":      cfg.satcom_latency_ms,
                "channels":        satcom_channels,
                "degraded":        False,
                "uplink_strength": 1.0,
            },
            "gps": {
                "enabled":    cfg.gps_enabled,
                "degraded":   cfg.gps_degraded,
                "accuracy_m": cfg.gps_accuracy_m,
                "error_km":   cfg.gps_error_km,
            },
        })

    def _save_infra_to_config(self):
        """Persist current InfrastructureManager state back to config fields."""
        import json as _json
        status = self.infra.get_status()
        self._cfg.infra_nodes_json = _json.dumps(status["retrans_nodes"])
        self._cfg.satcom_enabled   = status["satcom"]["enabled"]
        self._cfg.satcom_latency_ms = status["satcom"]["latency_ms"]
        self._cfg.satcom_channels  = _json.dumps(status["satcom"]["channels"])
        self._cfg.gps_enabled      = status["gps"]["enabled"]
        self._cfg.gps_degraded     = status["gps"]["degraded"]
        self._cfg.gps_accuracy_m   = status["gps"]["accuracy_m"]
        self._cfg.gps_error_km     = status["gps"]["error_km"]
        self._cfg.routing_mode               = self.infra.routing_mode
        self._cfg.routing_min_signal         = self.infra.routing_min_signal
        self._cfg.routing_prefer_low_latency = self.infra.routing_prefer_low_latency

    def _sync_weather_state(self):
        """Push weather settings from config to RangeModel and InfrastructureManager. (Phase 4)"""
        c = self._cfg
        # Update RangeModel (affects physical propagation)
        self._range.weather_enabled = c.weather_enabled
        self._range.weather_severity = c.weather_severity
        self._range.weather_type = c.weather_type
        self._range.weather_humidity = c.weather_humidity
        self._range.temp_inversion_enabled = c.temp_inversion_enabled
        self._range.temp_inversion_strength = c.temp_inversion_strength
        
        # Update InfrastructureManager (affects path selection/ducting)
        self.infra.weather = {
            "enabled": c.weather_enabled,
            "type": c.weather_type,
            "severity": c.weather_severity,
            "humidity": c.weather_humidity,
            "temp_inversion_enabled": c.temp_inversion_enabled,
            "temp_inversion_strength": c.temp_inversion_strength
        }
        log.info("Weather state synchronized to propagation engines")

    def _record_quality(self, callsign: str, signal: float,
                        relayed: bool, latency_ms: float = 0):
        """Update SignalQualityTracker for a callsign."""
        with self._quality_lock:
            if callsign not in self._quality:
                self._quality[callsign] = SignalQualityTracker(callsign)
            self._quality[callsign].record(signal, relayed, latency_ms)

    def get_signal_quality(self) -> list:
        """Return quality metrics for all tracked callsigns."""
        dist   = self._cfg.distortion  if hasattr(self, '_cfg') else 0.0
        noise  = self._cfg.noise_floor if hasattr(self, '_cfg') else 0.0
        is_sim = dist > 0 or noise > 0
        audio_penalty = dist * 0.3 + noise * 5.0

        with self._quality_lock:
            quality_map = {cs: q for cs, q in self._quality.items()}
        
        with self._clients_lock:
            all_clients = list(self._clients.values())

        result = []
        for client in all_clients:
            cs = client.callsign
            if cs in quality_map:
                q = quality_map[cs]
                if q.stale(timeout=45.0): # Slightly longer timeout
                    q._signals.clear()
                    q._recompute()
                d = q.to_dict()
            else:
                # Default for connected but silent clients
                d = {
                    "callsign":    cs,
                    "signal_avg":  1.0,
                    "packet_loss": 0.0,
                    "latency_ms":  0.0,
                    "clarity":     1.0,
                    "state":       "clear",
                    "state_since": 0.0,
                    "last_update": 0.0
                }
            
            # Blend in audio effects penalty into perceived clarity
            d["clarity_perceived"] = round(max(0.0, d["clarity"] - audio_penalty), 3)
            d["audio_mode"]  = "sim" if is_sim else "cpx"
            d["distortion"]  = round(dist, 3)
            d["noise_floor"] = round(noise, 3)
            result.append(d)
        
        return result

    def get_active_summary(self) -> dict:
        """Return aggregate metrics for the dashboard summary row."""
        import random as _random
        now = time.time()
        with self._clients_lock:
            tx_count = sum(1 for c in self._clients.values() if c.tx_active)
        with self._rx_lock:
            rx_count = sum(1 for ts in self._active_rx.values() if now - ts < 1.0)
        
        # Calculate Avg Latency and Packet Loss from quality trackers
        total_lat = 0
        total_loss = 0
        q_count = 0
        with self._quality_lock:
            for q in self._quality.values():
                if not q.stale():
                    total_lat += q.latency_ms
                    total_loss += q.packet_loss
                    q_count += 1
        
        avg_lat = total_lat / q_count if q_count > 0 else 0
        avg_loss = total_loss / q_count if q_count > 0 else 0
        
        return {
            "total_nodes": len(self._clients),
            "total_active": tx_count + rx_count,
            "tx_count": tx_count,
            "rx_count": rx_count,
            "load_pct": self.stats.get("load_pct", 0),
            "util_pct": self.stats.get("util_pct", 0),
            "avg_latency": round(avg_lat, 1),
            "packet_loss": round(avg_loss * 100, 1)
        }

    def get_active_transmissions(self) -> list:
        """Return status for ALL online clients (TX, RX, or IDLE)."""
        import random as _random
        now = time.time()
        out = []
        
        def get_cli_info(cs):
            cs_def = self._cfg_mgr.callsigns.get(cs)
            if cs_def:
                return cs_def.real_name or cs, cs_def.radio_profile
            return cs, "handheld"

        with self._clients_lock:
            # We want to show EVERY connected client
            for cs, c in self._clients.items():
                name, prof = get_cli_info(cs)
                ch_def = self._cfg_mgr.channels.get(c.channel)
                
                # Determine state
                state = "IDLE"
                duration = 0
                start_time = "--:--:--"
                
                # Check RX first (received recently)
                with self._rx_lock:
                    rx_ts = self._active_rx.get(cs, 0)
                    is_rx = (now - rx_ts < 1.5)
                
                if c.tx_active:
                    state = "TX"
                    duration = round(now - c.tx_start, 1)
                    start_time = time.strftime("%H:%M:%S", time.localtime(c.tx_start))
                elif is_rx:
                    state = "RX"
                    duration = round(now - rx_ts, 1)
                    start_time = time.strftime("%H:%M:%S", time.localtime(rx_ts))
                
                # Metrics
                sig = 1.0; loss = 0; jitter = 0
                with self._quality_lock:
                    q = self._quality.get(cs)
                    if q:
                        sig = q.signal_avg
                        loss = q.packet_loss
                        if state != "IDLE":
                            jitter = _random.randint(5, 25)

                out.append({
                    "type": state,
                    "callsign": cs,
                    "name": name,
                    "channel_id": c.channel,
                    "channel_name": ch_def.name if ch_def else ("UNASSIGNED" if c.channel == -1 else f"CH{c.channel:02d}"),
                    "freq": ch_def.frequency if ch_def else "--",
                    "bw": ch_def.bandwidth_khz if ch_def else "Narrow",
                    "duration": duration,
                    "start_time": start_time,
                    "signal": round(sig, 2),
                    "quality": "Good" if sig > 0.7 else "Weak" if sig > 0.3 else "Poor",
                    "codec": "Opus",
                    "jitter": jitter,
                    "packet_loss": round(loss * 100, 1),
                    "priority": "NORMAL"
                })
        
        # Add EW Effects (Jamming, Interference)
        ew_status = self.ew_engine.get_status()
        for eff in ew_status.get("active", []):
            params = eff.get("params", {})
            freqs = params.get("channels", []) # We often store freqs as 'channels' in the EW engine
            if not freqs:
                freqs = [params.get("frequency", 0)]
            
            for f in freqs:
                if f == 0: continue
                out.append({
                    "type": eff["type"].upper(),
                    "callsign": eff.get("label", "EW_EMITTER"),
                    "name": eff.get("label", "EW Emitter"),
                    "channel_name": "RF_INTERFERENCE",
                    "freq": f,
                    "bw": "Wide",
                    "duration": 0,
                    "start_time": "--:--:--",
                    "signal": params.get("intensity", 0.5),
                    "quality": "INTERFERENCE",
                    "power_dbm": -40 - (1.0 - params.get("intensity", 1.0)) * 40,
                    "priority": "HIGH"
                })

        # Sort so active ones are at the top (TX > JAM/RX > IDLE)
        state_priority = {"TX": 0, "JAM": 1, "EMI": 1, "RX": 2, "IDLE": 3}
        out.sort(key=lambda x: (state_priority.get(x["type"], 5), x["callsign"]))
        return out


    def _calc_signal(self, sender: Optional[ClientState], recipient: ClientState) -> float:
        # Feature 5: CH99 VEHICLE INTERCOM — always full-strength, bypass range/terrain
        if sender is not None and getattr(sender, 'channel', -1) == 99:
            return 1.0
        if getattr(recipient, 'channel', -1) == 99:
            return 1.0
        if not self._cfg.range_enabled:
            return 1.0
        if sender is None or not sender.position_fresh() or not recipient.position_fresh():
            return 1.0
        return self._range.signal_from_positions(
            sender.lat, sender.lon, recipient.lat, recipient.lon)

    def _make_pong(self, ping_pkt: Packet) -> bytes:
        pong = Packet(type=PktType.PONG, channel=ping_pkt.channel, callsign="SERVER",
                      payload=struct.pack("!Q", ping_pkt.ts_ms))
        return self.get_codec(ping_pkt.channel).encode(pong)

    def _send_pong(self, addr: Tuple[str, int], ping_pkt: Packet):
        wire = self._make_pong(ping_pkt)
        try: self._udp_sock.sendto(wire, addr)
        except: pass

    def _update_signal_cache(self):
        """Pre-calculate signal strengths for all client pairs (Terrain LOS optimization)."""
        if not self._cfg.range_enabled:
            with self._signal_lock:
                if self._signal_cache:
                    self._signal_cache.clear()
            return

        with self._clients_lock:
            clients = list(self._clients.values())
        
        new_cache = {}
        for sender in clients:
            if sender.lat == 0 or sender.lon == 0: continue
            sender_profile = self._cfg_mgr.profiles.get(sender.radio_profile)
            
            for recipient in clients:
                if sender.callsign == recipient.callsign: continue
                if recipient.lat == 0 or recipient.lon == 0: continue
                
                # Feature 5: CH99 VEHICLE INTERCOM — always full signal, skip terrain calc
                if sender.channel == 99 or recipient.channel == 99:
                    new_cache[(sender.callsign, recipient.callsign)] = 1.0
                    continue
                
                try:
                    recipient_profile = self._cfg_mgr.profiles.get(recipient.radio_profile)
                    channel_def = self._cfg_mgr.channels.get(sender.channel)
                    
                    freq_mhz = 45.0
                    modulation = "FM"
                    waveform_type = "Narrowband"
                    if channel_def:
                        freq_mhz = parse_freq_mhz(channel_def.frequency)
                        modulation = getattr(channel_def, 'modulation', 'FM')
                        waveform_type = getattr(channel_def, 'waveform_type', 'Narrowband')

                    # Compute path once per 2s
                    path = self.infra.compute_path(
                        sender.lat, sender.lon,
                        recipient.lat, recipient.lon,
                        sender.channel,
                        range_fn=self._range.signal_from_positions,
                        sender_cs=sender.callsign,
                        recipient_cs=recipient.callsign,
                        sender_profile=sender_profile,
                        recipient_profile=recipient_profile,
                        channel_def=channel_def,
                        freq_mhz_opt=freq_mhz,
                        modulation=modulation,
                        waveform_type=waveform_type,
                    )
                    
                    # Pre-calculate EW & Weather effects here (Background Thread)
                    # This removes the bottleneck from the real-time relay loop.
                    sig = path.signal
                    _relay_ok, sig_mult, _extra = self.ew_engine.filter_relay(
                        None, sender.channel, sender.callsign, recipient.callsign)
                    
                    new_cache[(sender.callsign, recipient.callsign)] = sig * sig_mult
                except Exception as e:
                    log.warning(f"Error computing path for {sender.callsign} -> {recipient.callsign}: {e}")

        with self._signal_lock:
            self._signal_cache = new_cache

    def _handle_radio_check(self, pkt: Packet, addr: Tuple[str, int]):
        target = pkt.payload[:16].rstrip(b"\x00").decode("ascii", "replace") if pkt.payload else ""
        log.info(f"RADIO CHECK [{pkt.callsign}] → {target or 'ALL'} CH{pkt.channel:02d}")
        self._relay_control(pkt, addr)

    def _broadcast_member_list_on_channel(self, channel: int):
        """Send updated member list to ALL clients currently on channel."""
        with self._clients_lock:
            members    = [c.as_member_dict() for c in self._clients.values()
                          if c.channel == channel]
            recipients = [c for c in self._clients.values()
                          if c.channel == channel]
        if not members:
            return
        pkt  = Packet(type=PktType.MEMBER_LIST, channel=channel,
                      callsign="SERVER", payload=Payloads.member_list(members))
        wire = self.get_codec(channel).encode(pkt)
        for recipient in recipients:
            try:
                self._udp_sock.sendto(wire, recipient.udp_addr)
            except Exception:
                pass

    def _send_member_list_to(self, client: ClientState):
        with self._clients_lock:
            members = [c.as_member_dict() for cs, c in self._clients.items() if c.channel == client.channel]
        if not members: return
        pkt = Packet(type=PktType.MEMBER_LIST, channel=client.channel, callsign="SERVER",
                     payload=Payloads.member_list(members))
        wire = self.get_codec(client.channel).encode(pkt)
        try: self._udp_sock.sendto(wire, client.udp_addr)
        except: pass
        if client.tcp_conn: self._tcp_send(client, wire)

    def _member_broadcast_loop(self):
        while self._running:
            time.sleep(MEMBER_BROADCAST_INTERVAL)
            with self._clients_lock: clients = list(self._clients.values())
            for client in clients:
                try: self._send_member_list_to(client)
                except: pass

    def _tcp_send(self, client: ClientState, data: bytes):
        if not client.tcp_conn: return
        try: client.tcp_conn.sendall(data)
        except: client.tcp_conn = None

    def update_client_profile(self, callsign: str, profile_id: str):
        callsign = callsign.upper()
        with self._clients_lock:
            if callsign in self._clients:
                self._clients[callsign].radio_profile = profile_id
                log.info("Client [%s] profile updated to %s", callsign, profile_id)

    def force_client_channel(self, callsign: str, channel: int):
        """Push a channel change command to a connected client via UDP."""
        callsign = callsign.upper()
        with self._clients_lock:
            client = self._clients.get(callsign)
            if not client:
                log.debug("Cannot force channel for [%s]: not in active client list", callsign)
                return False
            
            # Check for manual lockout telemetry from client PINGs
            if hasattr(client, 'lock_until') and time.time() < client.lock_until:
                log.info("Cannot force channel for [%s]: manual override active for %ds", 
                         callsign, int(client.lock_until - time.time()))
                return False
            
            pkt = Packet(type=PktType.FORCE_CHANNEL, channel=channel, callsign="SERVER")
            wire = self.get_codec(0).encode(pkt)
            try:
                self._udp_sock.sendto(wire, client.udp_addr)
                log.info("Sent FORCE_CHANNEL to [%s] at %s -> CH%02d", callsign, client.udp_addr, channel)
                return True
            except Exception as e:
                log.warning("Failed to send FORCE_CHANNEL to [%s]: %s", callsign, e)
                return False

    def _get_or_register_client(self, callsign: str, udp_addr: Tuple[str, int]) -> ClientState:
        callsign = callsign.upper()
        with self._clients_lock:
            if callsign not in self._clients:
                # Brand new client — create state and pre-populate position
                # from the callsign register so admin-set positions are live immediately
                state = ClientState(callsign=callsign, udp_addr=udp_addr)
                cs_def = self._cfg_mgr.callsigns.get(callsign)
                if cs_def:
                    state.evicted = getattr(cs_def, 'evicted', False)
                    if cs_def.lat or cs_def.lon:
                        state.lat          = cs_def.lat
                        state.lon          = cs_def.lon
                        state.alt          = getattr(cs_def, 'alt', 0.0)
                        state.radio_profile = getattr(cs_def, 'radio_profile', 'handheld')
                        state.last_position = time.time()
                        log.info(f"Pre-loaded position for {callsign}: "
                                 f"({cs_def.lat:.4f},{cs_def.lon:.4f}) {'[EVICTED]' if state.evicted else ''}")
                self._clients[callsign] = state
                n = len(self._clients)
                self.stats["clients_peak"] = max(self.stats["clients_peak"], n)
                log.info(f"New client: {callsign} from {udp_addr}  (total: {n})")
                self._act(f"CONNECT [{callsign}] from {udp_addr[0]}", "sys")
            else:
                existing = self._clients[callsign]
                if existing.udp_addr != udp_addr:
                    # Same callsign, different address — client reconnected.
                    # Reset stale transmit state so relay logic isn't blocked.
                    log.info(f"Reconnect: {callsign} moved {existing.udp_addr} → {udp_addr}")
                    self._act(f"RECONNECT [{callsign}] from {udp_addr[0]}", "sys")
                    existing.udp_addr  = udp_addr
                    existing.tx_active = False   # clear any stale PTT state
                    # Re-load position from callsign register if current is stale/zero
                    if not existing.position_fresh() or (existing.lat == 0 and existing.lon == 0):
                        cs_def = self._cfg_mgr.callsigns.get(callsign)
                        if cs_def and (cs_def.lat or cs_def.lon):
                            existing.lat          = cs_def.lat
                            existing.lon          = cs_def.lon
                            existing.alt          = getattr(cs_def, 'alt', 0.0)
                            existing.last_position = time.time()
                            log.info(f"Restored position for {callsign} on reconnect")
                existing.udp_addr = udp_addr
            return self._clients[callsign]

    def _housekeeping_loop(self):
        last_signal_update = 0
        while self._running:
            time.sleep(2.0)
            now = time.time()

            # 1. Update Signal Cache (Performance Fix)
            if now - last_signal_update > 2.0:
                try:
                    self._update_signal_cache()
                except Exception as e:
                    log.exception("Exception in _update_signal_cache: %s", e)
                last_signal_update = now

            # Calculate RF Congestion (Load)
            # Reflects airtime usage and channel collisions
            with self._clients_lock:
                ch_status = {} # channel_id -> speaker_count
                for c in self._clients.values():
                    if c.tx_active and c.channel >= 0:
                        ch_status[c.channel] = ch_status.get(c.channel, 0) + 1
            
            active_ch_count = len(ch_status)
            total_ch_count = max(1, len(self._cfg_mgr.channels))
            
            # Base load: percentage of channels currently in use
            base_congestion = (active_ch_count / total_ch_count) * 70
            
            # Interference penalty: multiple speakers on the same frequency
            collision_penalty = sum((count - 1) * 30 for count in ch_status.values() if count > 1)
            
            self.stats["load_pct"] = min(100, int(base_congestion + collision_penalty))

            # Calculate Utilization (Active Clients / Total Population)
            with self._clients_lock:
                active_users = sum(1 for c in self._clients.values() if c.tx_active)
                total_users = len(self._clients)
            self.stats["util_pct"] = min(100, int((active_users / max(1, total_users)) * 100))

            # Refresh positions + detect stale clients
            stale = []
            with self._clients_lock:
                for cs, c in list(self._clients.items()):
                    # Refresh position from callsign register if stale/zero
                    if not c.position_fresh() or (c.lat == 0.0 and c.lon == 0.0):
                        cs_def = self._cfg_mgr.callsigns.get(cs)
                        if cs_def and (cs_def.lat or cs_def.lon):
                            c.lat           = cs_def.lat
                            c.lon           = cs_def.lon
                            c.alt           = getattr(cs_def, 'alt', 0.0)
                            c.last_position = now
                            log.debug(f"Housekeeping: refreshed position for {cs}")
                    # Evict clients that haven't been seen in CLIENT_TIMEOUT
                    if (now - c.last_seen) > CLIENT_TIMEOUT:
                        stale.append(cs)
                for cs in stale:
                    log.info(f"Dropping stale client: {cs}")
                    c = self._clients[cs]
                    old_ch = c.channel
                    self._act(f"TIMEOUT [{cs}] — dropped after {CLIENT_TIMEOUT:.0f}s", "sys")
                    del self._clients[cs]
                    # Broadcast updated member list to remaining channel members
                    if old_ch:
                        try:
                            self._broadcast_member_list_on_channel(old_ch)
                        except Exception:
                            pass

            # Prune dead thread references to prevent unbounded list growth
            self._threads = [t for t in self._threads if t.is_alive()]

            uptime = int(now - self.stats["uptime_start"])
            with self._clients_lock:
                n = len(self._clients)
            log.info(
                f"Stats — clients:{n}  rx:{self.stats['packets_rx']}  "
                f"relayed:{self.stats['packets_relayed']}  "
                f"dropped:{self.stats['packets_dropped']}  "
                f"uptime:{uptime}s  threads:{len(self._threads)}"
            )

    def update_client_position(self, callsign: str, lat: float,
                               lon: float, alt: float = 0.0):
        """Update live ClientState position AND persist to callsign register."""
        callsign = callsign.upper()
        with self._clients_lock:
            if callsign in self._clients:
                self._clients[callsign].update_position(lat, lon, alt, is_admin=True)
        # Always persist to callsign register so position survives reconnects
        cs_def = self._cfg_mgr.callsigns.get(callsign)
        if cs_def:
            cs_def.lat = lat
            cs_def.lon = lon
            cs_def.alt = alt
            # Save to INI so position survives server restart too
            self._cfg_mgr.save()

    def get_channel_members(self, channel: int) -> List[dict]:
        with self._clients_lock:
            return [c.as_member_dict() for c in self._clients.values() if c.channel == channel]

    def get_topology(self) -> dict:
        """
        Calculates the complete network topology (nodes + logical links).
        Heavily cached (2.5s) to mitigate O(N^2) pathfinding load.
        """
        with self._topology_lock:
            if self._topology_cache and (time.time() - self._topology_cache_ts < 2.5):
                return self._topology_cache

        now = time.time()
        nodes = []
        links = []
        
        with self._clients_lock:
            active_clients = list(self._clients.values())
        
        # 1. Collect all Nodes (Clients + Infrastructure)
        # Add Clients
        for c in active_clients:
            cs_def = self._cfg_mgr.callsigns.get(c.callsign)
            prof_id = c.radio_profile or (cs_def.radio_profile if cs_def else 'prc152')
            prof = self._cfg_mgr.profiles.get(prof_id)
            specs = (prof.tx_power_w, prof.max_range_km, prof.full_quiet_km) if prof else (5.0, 20.0, 5.0)
            assigned_chs = cs_def.channels if cs_def else [c.channel]
            if c.channel not in assigned_chs:
                assigned_chs.append(c.channel)
                
            nodes.append({
                "id": c.callsign,
                "name": getattr(cs_def, 'real_name', c.callsign),
                "callsign": c.callsign,
                "type": "client",
                "state": "TX" if c.tx_active else "RX" if (now - self._active_rx.get(c.callsign, 0) < 0.5) else "IDLE",
                "lat": c.lat,
                "lon": c.lon,
                "mgrs": latlon_to_mgrs(c.lat, c.lon, precision=4),
                "channel": c.channel,
                "monitor_channels": list(c.monitor_channels),
                "assigned_channels": assigned_chs,
                "channel_name": self._cfg_mgr.channels.get(c.channel).name if c.channel in self._cfg_mgr.channels else ("IDLE" if c.channel == -1 else "OFF"),
                "freq": self._cfg_mgr.channels.get(c.channel).frequency if c.channel in self._cfg_mgr.channels else "0.0",
                "profile": c.radio_profile,
                "profile_specs": specs,
                "last_seen": c.last_seen
            })
            
        # Add Infrastructure (Retrans)
        with self.infra._lock:
            for rid, r in self.infra.retrans_nodes.items():
                if not r.enabled: continue
                nodes.append({
                    "id": rid,
                    "name": r.name,
                    "callsign": r.node_id,
                    "type": "retrans",
                    "state": "IDLE", 
                    "lat": r.lat,
                    "lon": r.lon,
                    "mgrs": r.mgrs,
                    "channel": -1, # Multichannel/System 
                    "monitor_channels": [],
                    "assigned_channels": [c for c in (r.channels or []) if c >= 0],
                    "channel_name": "RELAY",
                    "freq": "VAR",
                    "profile": "retrans",
                    "profile_specs": (r.power_w, 60.0, 15.0),
                    "last_seen": now
                })

        # 2. Calculate Links (Mesh)
        for i, n1 in enumerate(nodes):
            for j, n2 in enumerate(nodes):
                if i >= j: continue 
                
                # Link logic: collect all connections between this pair
                connections = []  # list of (logical_type, channel)
                
                if n1["type"] == "client" and n2["type"] == "client":
                    if n1["channel"] >= 0 and n1["channel"] == n2["channel"]:
                        connections.append(("primary", n1["channel"]))
                    if n1["channel"] >= 0 and n1["channel"] in n2["monitor_channels"]:
                        connections.append(("monitor", n1["channel"]))
                    if n2["channel"] >= 0 and n2["channel"] in n1["monitor_channels"]:
                        connections.append(("monitor", n2["channel"]))
                
                # Retrans logic: Client <-> Retrans
                elif (n1["type"] == "client" and n2["type"] == "retrans") or \
                     (n1["type"] == "retrans" and n2["type"] == "client"):
                    cli = n1 if n1["type"] == "client" else n2
                    ret = n2 if n1["type"] == "client" else n1
                    ret_chs = [c for c in ret["assigned_channels"] if c >= 0]
                    if cli["channel"] >= 0 and (not ret_chs or cli["channel"] in ret_chs):
                        connections.append(("primary", cli["channel"]))
                    for m_ch in cli.get("monitor_channels", []):
                        if m_ch >= 0 and (not ret_chs or m_ch in ret_chs):
                            connections.append(("monitor", m_ch))
                
                # Deduplicate: keep best logical type per channel
                unique_conns = {}
                for l_type, c_ch in connections:
                    if c_ch not in unique_conns or (l_type == "primary" and unique_conns[c_ch] == "monitor"):
                        unique_conns[c_ch] = l_type
                
                if not unique_conns:
                    continue
                
                # Calculate physical path for each unique connection
                for ch, logical_type in unique_conns.items():
                    prof1 = self._cfg_mgr.profiles.get(n1.get("profile")) if n1["type"] == "client" else None
                    prof2 = self._cfg_mgr.profiles.get(n2.get("profile")) if n2["type"] == "client" else None
                    channel_def = self._cfg_mgr.channels.get(ch)
                    
                    freq_mhz = 45.0
                    modulation = "FM"
                    waveform_type = "Narrowband"
                    if channel_def:
                        freq_mhz = parse_freq_mhz(channel_def.frequency)
                        modulation = getattr(channel_def, 'modulation', 'FM')
                        waveform_type = getattr(channel_def, 'waveform_type', 'Narrowband')

                    path = self.infra.compute_path(
                        n1["lat"], n1["lon"], n2["lat"], n2["lon"],
                        channel=ch, sender_cs=n1["id"], recipient_cs=n2["id"],
                        sender_profile=prof1,
                        recipient_profile=prof2,
                        channel_def=channel_def,
                        freq_mhz_opt=freq_mhz,
                        modulation=modulation,
                        waveform_type=waveform_type,
                    )
                    
                    if path.signal > 0.05:
                        status = "STRONG" if path.signal > 0.7 else "MEDIUM" if path.signal > 0.3 else "WEAK"
                        is_traffic = (n1["state"] == "TX" or n2["state"] == "TX")
                        
                        # Map path.link_type to visual types
                        # link_type can be "direct", "retrans:<id>", "satcom"
                        phys_type = "direct"
                        if "retrans" in path.link_type: phys_type = "retrans"
                        elif "satcom" in path.link_type: phys_type = "satcom"

                        links.append({
                            "from": n1["id"],
                            "to": n2["id"],
                            "logical_type": logical_type,
                            "physical_type": phys_type,
                            "signal": round(path.signal, 3),
                            "status": status,
                            "traffic": is_traffic,
                            "channel": ch,
                            "tx_active_node": n1["id"] if n1["state"] == "TX" else n2["id"] if n2["state"] == "TX" else None
                        })
        
        result = {
            "nodes": nodes,
            "links": links,
            "stats": {
                "load_pct": self.stats["load_pct"],
                "util_pct": self.stats["util_pct"]
            }
        }
        with self._topology_lock:
            self._topology_cache = result
            self._topology_cache_ts = time.time()
        return result

    def get_stats(self) -> dict:
        now = time.time()
        with self._clients_lock:
            clients = {
                cs: {
                    "channel":          c.channel,
                    "monitor_channels": list(c.monitor_channels),
                    "tx_active":        c.tx_active,
                    "lat":              c.lat,
                    "lon":              c.lon,
                    "alt":              c.alt,
                    "last_seen":        c.last_seen,
                    "idle_s":           round(now - c.last_seen, 1),
                    "packets_rx":       c.packets_rx,
                    "packets_tx":       c.packets_tx,
                    "radio_profile":    c.radio_profile,
                    "evicted":          c.evicted,
                    "lock_until":       getattr(c, 'lock_until', 0),
                }
                for cs, c in self._clients.items()
            }
        return {
            **self.stats,
            "clients":  clients,
            "uptime_s": int(time.time() - self.stats["uptime_start"]),
            "running":  self._running,
        }

# -- Entry point ------------------------------------------------------
def main():
    import argparse, io
    # Force UTF-8 stdout/stderr — prevents UnicodeEncodeError on Windows CP1252
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    os.environ.setdefault("PYTHONUTF8", "1")

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)-18s %(message)s", datefmt="%H:%M:%S")
    parser = argparse.ArgumentParser(description="TacNet Relay Server")
    parser.add_argument("--config", default=None)
    parser.add_argument("--port",   type=int, default=None)
    parser.add_argument("--host",   default=None)
    parser.add_argument("--debug",  action="store_true")
    args = parser.parse_args()
    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)
    cfg_path = Path(args.config) if args.config else DEFAULT_CONFIG_PATH
    server   = RadioServer(cfg_path)
    if args.port: server._cfg.server_port   = args.port
    if args.host: server._cfg.bind_address  = args.host
    try:
        print(
            "\n"
            "+------------------------------------------------------+\n"
            "|          TacNet -- Network Radio Server            |\n"
            "+------------------------------------------------------+\n"
            f"|  UDP Voice:  {server._cfg.bind_address}:{server._cfg.server_port:<35}|\n"
            f"|  TCP Ctrl:   {server._cfg.bind_address}:{server._cfg.server_ctrl_port:<35}|\n"
            f"|  Range:      {'ENABLED' if server._cfg.range_enabled else 'DISABLED':<38}|\n"
            f"|  Max range:  {server._cfg.range_max_km:.0f} km{'':<35}|\n"
            "+------------------------------------------------------+\n"
            "Press Ctrl+C to stop\n"
        )
    except Exception:
        pass
    server.run_forever()
    print("\nServer stopped.")

if __name__ == "__main__":
    main()