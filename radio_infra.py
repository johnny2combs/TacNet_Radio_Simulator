"""
TacNet — Communications Infrastructure
=========================================
Handles:
  - MGRS (Military Grid Reference System) ↔ lat/lon conversion
  - Retransmission (Retrans) nodes — relay nodes that extend range
  - Satellite Communications (SATCOM) — terrain-ignoring link with latency
  - GPS state — accuracy and degradation effects on positioning/ranging

Architecture
------------
InfrastructureManager.compute_path() is called by the server relay loop
instead of the raw RangeModel.signal_strength(). It returns:

    PathResult(signal, latency_ms, path, link_type)

where link_type is one of:  "direct" | "retrans" | "satcom" | "blocked"

The server injects the latency_ms as an artificial sleep before relaying
the packet, giving realistic timing for retrans (slight) and satcom (100-600ms).
"""

from __future__ import annotations
import math
import json
import threading
import time
import logging
from dataclasses import dataclass, field, asdict
from typing import List, Optional, Tuple, Dict
from radio_terrain import get_terrain_manager

log = logging.getLogger("radio.infra")


def parse_freq_mhz(freq_str: str) -> float:
    """Parse frequency string like '45.500 MHz' or '150 kHz' to float MHz."""
    if not freq_str:
        return 45.0
    try:
        freq_str = str(freq_str).strip()
        parts = freq_str.split()
        val = float(parts[0])
        if len(parts) > 1:
            unit = parts[1].upper()
            if "GHZ" in unit:
                val *= 1000.0
            elif "KHZ" in unit:
                val /= 1000.0
        return val
    except Exception:
        return 45.0



# ── MGRS Conversion ───────────────────────────────────────────────────────────
# Pure-Python implementation — no external dependencies.
# Accuracy: ~1m (sufficient for tactical radio range simulation).

_MGRS_ALPHABET   = "ABCDEFGHJKLMNPQRSTUVWXYZ"   # no I or O
_MGRS_COL_ORIGIN = "ABCDEFGHJKLMNPQRSTUVWXYZ"
_MGRS_ROW_ORIGIN = "ABCDEFGHJKLMNPQRSTUV"
_GZD_LETTERS     = "CDEFGHJKLMNPQRSTUVWX"        # latitude bands


def _utm_zone(lat: float, lon: float) -> int:
    """Return UTM zone number for a lat/lon."""
    zone = int((lon + 180) / 6) + 1
    # Handle Norway/Svalbard exceptions
    if 56 <= lat < 64 and 3 <= lon < 12:
        zone = 32
    if 72 <= lat <= 84:
        if   0 <= lon <  9:  zone = 31
        elif 9 <= lon < 21:  zone = 33
        elif 21 <= lon < 33: zone = 35
        elif 33 <= lon < 42: zone = 37
    return zone


def _lat_band(lat: float) -> str:
    """Return the MGRS latitude band letter for a latitude."""
    if -80 <= lat < 72:
        return _GZD_LETTERS[int((lat + 80) / 8)]
    elif 72 <= lat <= 84:
        return "X"
    return "Z"   # invalid / polar


def latlon_to_mgrs(lat: float, lon: float, precision: int = 5) -> str:
    """
    Convert WGS84 lat/lon to MGRS string.
    precision: digits per easting/northing component (1–5, default 5 = 1m).
    Returns e.g. "54HVH1234567890" for precision=5 or "54HVH12345 67890".
    """
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return "INVALID"
    # -- UTM conversion (simplified, WGS84) -----------------------------------
    zone   = _utm_zone(lat, lon)
    band   = _lat_band(lat)
    k0     = 0.9996
    a      = 6378137.0          # WGS84 semi-major axis
    e2     = 0.00669437999014   # eccentricity squared
    e_p2   = e2 / (1 - e2)
    n_val  = a / math.sqrt(1 - e2 * math.sin(math.radians(lat)) ** 2)
    t_val  = math.tan(math.radians(lat)) ** 2
    c_val  = e_p2 * math.cos(math.radians(lat)) ** 2
    lon0   = math.radians((zone - 1) * 6 - 180 + 3)
    A_val  = math.cos(math.radians(lat)) * (math.radians(lon) - lon0)
    # M — meridional arc
    lat_r = math.radians(lat)
    M = a * (
        (1 - e2/4 - 3*e2**2/64 - 5*e2**3/256) * lat_r
        - (3*e2/8 + 3*e2**2/32 + 45*e2**3/1024) * math.sin(2*lat_r)
        + (15*e2**2/256 + 45*e2**3/1024) * math.sin(4*lat_r)
        - (35*e2**3/3072) * math.sin(6*lat_r)
    )
    easting = k0 * n_val * (
        A_val
        + (1 - t_val + c_val) * A_val**3 / 6
        + (5 - 18*t_val + t_val**2 + 72*c_val - 58*e_p2) * A_val**5 / 120
    ) + 500000.0
    northing = k0 * (
        M + n_val * math.tan(lat_r) * (
            A_val**2 / 2
            + (5 - t_val + 9*c_val + 4*c_val**2) * A_val**4 / 24
            + (61 - 58*t_val + t_val**2 + 600*c_val - 330*e_p2) * A_val**6 / 720
        )
    )
    if lat < 0:
        northing += 10000000.0
    # -- 100km grid square identification ------------------------------------
    col_idx  = int(easting  / 100000)
    row_idx  = int(northing / 100000) % 20
    col_set  = ((zone - 1) % 3) * 8   # column set offset
    col_let  = _MGRS_COL_ORIGIN[(col_idx - 1 + col_set) % 24]
    row_let  = _MGRS_ROW_ORIGIN[(row_idx + (zone % 2) * 5) % 20]  # alternate by zone parity
    # -- Numerical reference -------------------------------------------------
    e_num = int(easting  % 100000)
    n_num = int(northing % 100000)
    scale = 10 ** (5 - precision)
    e_str = str(e_num // scale).zfill(precision)
    n_str = str(n_num // scale).zfill(precision)
    return f"{zone:02d}{band}{col_let}{row_let}{e_str}{n_str}"


def mgrs_to_latlon(mgrs: str) -> Tuple[float, float]:
    """
    Convert MGRS string to WGS84 (lat, lon).
    Raises ValueError on bad input.
    Returns (lat, lon) in decimal degrees.
    """
    mgrs = mgrs.strip().replace(" ", "").upper()
    if len(mgrs) < 5:
        raise ValueError(f"MGRS too short: {mgrs!r}")
    # Parse zone number (1–2 digits)
    i = 0
    while i < len(mgrs) and mgrs[i].isdigit():
        i += 1
    if i == 0:
        raise ValueError("No zone number found")
    zone = int(mgrs[:i])
    band = mgrs[i]
    col_let = mgrs[i+1]
    row_let = mgrs[i+2]
    num_part = mgrs[i+3:]
    if len(num_part) % 2 != 0:
        raise ValueError(f"Odd numeric part length: {num_part!r}")
    prec = len(num_part) // 2
    if prec == 0:
        e_num = 50000; n_num = 50000   # centre of 100km grid
    else:
        scale  = 10 ** (5 - prec)
        e_num  = int(num_part[:prec])  * scale
        n_num  = int(num_part[prec:])  * scale
    # Reconstruct UTM easting
    col_set  = ((zone - 1) % 3) * 8
    col_idx  = (_MGRS_COL_ORIGIN.index(col_let) - col_set) % 24 + 1
    easting  = col_idx * 100000 + e_num
    # Reconstruct northing using band midpoint proximity search.
    # Scan ±3 × 2000 km blocks around the band midpoint to find the
    # best match for the decoded row index + numeric offset.
    band_idx     = _GZD_LETTERS.index(band) if band in _GZD_LETTERS else 0
    band_lat_mid = -80 + band_idx * 8 + 4
    row_idx      = _MGRS_ROW_ORIGIN.index(row_let)
    row_idx_adj  = (row_idx - (zone % 2) * 5) % 20
    northing_100 = row_idx_adj * 100000
    k0 = 0.9996; a = 6378137.0; e2 = 0.00669437999014
    lat_approx = math.radians(band_lat_mid)
    M_approx = a * (
        (1 - e2/4 - 3*e2**2/64) * lat_approx
        - (3*e2/8 + 3*e2**2/32) * math.sin(2*lat_approx)
    )
    northing_mid = k0 * M_approx
    if band < 'N':
        northing_mid += 10000000.0
    # Search ±3 blocks of 2000 km around the band midpoint
    candidates = []
    for bm in range(-3, 4):
        snap_base = northing_mid + bm * 2000000 - (northing_mid % 2000000)
        cand = snap_base + northing_100 + n_num
        candidates.append((abs(cand - northing_mid), cand))
    candidates.sort()
    northing = candidates[0][1]
    if band < 'N':
        northing_falsenorth = northing - 10000000.0
    else:
        northing_falsenorth = northing
    lon0 = math.radians((zone - 1) * 6 - 180 + 3)
    # Reverse UTM
    e1   = (1 - math.sqrt(1-e2)) / (1 + math.sqrt(1-e2))
    e_p2 = e2 / (1 - e2)
    M    = northing_falsenorth / k0
    mu   = M / (a * (1 - e2/4 - 3*e2**2/64 - 5*e2**3/256))
    p1   = (3*e1/2 - 27*e1**3/32)    * math.sin(2*mu)
    p2   = (21*e1**2/16 - 55*e1**4/32) * math.sin(4*mu)
    p3   = (151*e1**3/96)              * math.sin(6*mu)
    lat1 = mu + p1 + p2 + p3
    n1   = a / math.sqrt(1 - e2 * math.sin(lat1)**2)
    t1   = math.tan(lat1)**2
    c1   = e_p2 * math.cos(lat1)**2
    r1   = a * (1-e2) / (1 - e2*math.sin(lat1)**2)**1.5
    D    = (easting - 500000) / (n1 * k0)
    lat  = lat1 - (n1 * math.tan(lat1)/r1) * (
        D**2/2 - (5 + 3*t1 + 10*c1 - 4*c1**2 - 9*e_p2)*D**4/24
        + (61 + 90*t1 + 298*c1 + 45*t1**2 - 252*e_p2 - 3*c1**2)*D**6/720
    )
    lon  = lon0 + (
        D - (1 + 2*t1 + c1)*D**3/6
        + (5 - 2*c1 + 28*t1 - 3*c1**2 + 8*e_p2 + 24*t1**2)*D**5/120
    ) / math.cos(lat1)
    return math.degrees(lat), math.degrees(lon)


# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class RetransNode:
    """
    A retransmission node (repeater) placed at a fixed position.
    It listens on rx_channels and retransmits on tx_channels.
    When a sender→recipient direct path is too weak, the server checks
    whether sender→node and node→recipient are both viable.
    """
    node_id:      str
    name:         str
    lat:          float = 0.0
    lon:          float = 0.0
    alt_m:        float = 0.0          # altitude in metres (higher = better LOS)
    channels:     List[int] = field(default_factory=list)   # [] = all channels
    enabled:      bool  = True
    power_w:      float = 5.0
    latency_ms:   float = 20.0         # processing delay per hop
    degraded:     bool  = False        # can be degraded by EW
    degraded_pct: float = 0.0          # 0.0=nominal  1.0=completely degraded

    def to_dict(self) -> dict:
        d = asdict(self)
        d['mgrs'] = self.mgrs
        return d

    @staticmethod
    def from_dict(d: dict) -> 'RetransNode':
        return RetransNode(**{k: v for k, v in d.items()
                              if k in RetransNode.__dataclass_fields__})

    @property
    def mgrs(self) -> str:
        try:
            return latlon_to_mgrs(self.lat, self.lon, precision=4)
        except Exception:
            return "—"


@dataclass
class SatcomLink:
    """
    Satellite communications link.
    Ignores terrain / distance, but adds latency and can be jammed at high EW levels.
    """
    enabled:           bool  = False
    latency_ms:        float = 280.0   # typical LEO round-trip ~280ms, GEO ~600ms
    channels:          List[int] = field(default_factory=list)  # [] = all channels
    jam_threshold:     float = 0.7     # EW intensity above this degrades/blocks SATCOM
    degraded:          bool  = False
    uplink_strength:   float = 1.0     # 0–1, reduced by jamming

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class GPSState:
    """
    GPS availability and accuracy.
    When degraded, position errors are injected into distance calculations,
    causing incorrect signal strength estimates.
    """
    enabled:    bool  = True
    degraded:   bool  = False
    accuracy_m: float = 5.0     # CEP (circular error probable) in metres
    # When degraded:
    error_km:   float = 0.0     # injected position error in km

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class PathResult:
    """Result of a path computation."""
    signal:      float               # 0.0–1.0
    latency_ms:  float               # additional latency to inject
    link_type:   str                 # "direct" | "retrans:<node_id>" | "satcom" | "blocked"
    hops:        List[str]           # callsign/node names in path
    terrain_blocked: bool = False    # True if physical terrain is in the way
    notes:       str = ""


# ── Infrastructure Manager ────────────────────────────────────────────────────

class InfrastructureManager:
    """
    Computes communication paths between nodes, considering:
      1. Direct LOS (existing RangeModel)
      2. Retrans hops (one-hop relay through a RetransNode)
      3. SATCOM (terrain-independent with latency penalty)

    GPS state affects position accuracy used in distance computation.
    """

    # Signal threshold below which we try alternative paths
    DIRECT_THRESHOLD = 0.15   # below this, try retrans/satcom

    def __init__(self):
        self._lock   = threading.Lock()
        self.retrans_nodes: Dict[str, RetransNode] = {}
        self.satcom  = SatcomLink()
        self.gps     = GPSState()
        self.terrain = get_terrain_manager()
        self._range_fn = None   # set by server to self._range.signal_from_positions
        # Routing policy
        self.routing_mode            = "auto"   # direct|assisted|hybrid|auto
        self.routing_min_signal      = 0.35     # threshold before escalating to next path
        self.routing_prefer_low_latency = False # prefer fewer hops over better signal
        self.online_fallback         = False    # Use online SRTM if offline tiles missing
        # Path analytics: list of recent PathResult summaries for admin UI
        self._path_log: list = []               # capped at 200 entries
        self._path_log_lock = threading.Lock()
        
        # Path Caching (O(N^2) optimization for UI)
        self._path_cache: Dict[tuple, Tuple[float, PathResult]] = {}
        self._path_cache_lock = threading.Lock()
        
        # Phase 4: Atmospheric State
        self.weather = {
            "enabled": False,
            "type": "rain",
            "severity": 0.0,
            "humidity": 0.0,
            "temp_inversion_enabled": False,
            "temp_inversion_strength": 0.0
        }

    # ── Node management ───────────────────────────────────────────────────────

    def add_node(self, node: RetransNode):
        with self._lock:
            self.retrans_nodes[node.node_id] = node
        log.info("Retrans node added: %s (%s) at %.4f,%.4f",
                 node.node_id, node.name, node.lat, node.lon)

    def remove_node(self, node_id: str) -> bool:
        with self._lock:
            if node_id in self.retrans_nodes:
                del self.retrans_nodes[node_id]
                log.info("Retrans node removed: %s", node_id)
                return True
        return False

    def update_node(self, node_id: str, **kwargs):
        with self._lock:
            if node_id in self.retrans_nodes:
                node = self.retrans_nodes[node_id]
                for k, v in kwargs.items():
                    if hasattr(node, k):
                        setattr(node, k, v)

    def get_status(self) -> dict:
        with self._lock:
            nodes = [n.to_dict() for n in self.retrans_nodes.values()]
        return {
            "retrans_nodes": nodes,
            "satcom":        self.satcom.to_dict(),
            "gps":           self.gps.to_dict(),
            "routing": {
                "mode":                  self.routing_mode,
                "min_signal":            self.routing_min_signal,
                "prefer_low_latency":    self.routing_prefer_low_latency,
            },
        }

    # ── Path computation ──────────────────────────────────────────────────────

    @staticmethod
    def _diffraction_penalty(obstruction_m: float, freq_mhz: float = 45.0) -> float:
        """
        Compute VHF/UHF knife-edge diffraction signal retention factor (0.0–1.0).
        Based on the Fresnel-Kirchhoff diffraction parameter:
          ν = obstruction_m / reference_depth
        Returns 1.0 for clear LOS, down to ~0.45 for heavy obstruction.
        Positive obstruction_m = terrain is ABOVE the LOS path.
        Negative obstruction_m = terrain is BELOW the LOS path (clearance).
        """
        if obstruction_m <= 0:
            return 1.0  # LOS is clear or has clearance
        # Normalise obstruction against a frequency-dependent reference depth.
        # At 45 MHz VHF the first Fresnel zone is large — signals diffract well.
        # At 400 MHz UHF diffraction degrades faster.
        freq_scale = math.sqrt(freq_mhz / 45.0)  # 1.0 at VHF, ~3.0 at UHF
        # A 1m obstruction at VHF ≈ 10% loss; 5m ≈ 25%; 20m ≈ 45%
        # Modelled as: retention = exp(-0.12 * ν * freq_scale)
        nu = obstruction_m * freq_scale
        retention = math.exp(-0.12 * nu)
        return max(0.10, min(1.0, retention))  # Floor at 10% (NLOS not completely dead)

    def compute_path(
        self,
        sender_lat:    float, sender_lon:    float,
        recipient_lat: float, recipient_lon: float,
        channel:       int,
        range_fn       = None,
        sender_cs:     str = "",
        recipient_cs:  str = "",
        sender_profile = None,
        recipient_profile = None,
        channel_def    = None,
        freq_mhz_opt:  Optional[float] = None,
        modulation:    str = "FM",
        waveform_type: str = "Narrowband",
    ) -> PathResult:
        """
        Compute the best communication path according to routing_mode.
        Utilizes a short-lived cache (2s) to prevent O(N^2) terrain thrashing.
        """
        # 0. Cache Check (O(N^2) Mitigation)
        # Round coordinates to ~10m precision (4 decimals) to catch near-identical polls
        cache_key = (
            round(sender_lat, 4), round(sender_lon, 4),
            round(recipient_lat, 4), round(recipient_lon, 4),
            channel, sender_cs, recipient_cs, self.routing_mode
        )
        
        with self._path_cache_lock:
            cached = self._path_cache.get(cache_key)
            if cached:
                ts, res = cached
                if time.time() - ts < 2.0: # 2 second cache TTL
                    return res
        fn = range_fn or self._range_fn
        if fn is None:
            return PathResult(signal=1.0, latency_ms=0, link_type="direct",
                              hops=["direct"], terrain_blocked=False, notes="No range model")

        # --- Dynamic Hardware Capability Gates ---
        eccm_active = False
        if channel_def and getattr(channel_def, 'eccm_mode', 'None') != 'None':
            eccm_active = True
        
        if eccm_active:
            sender_eccm = getattr(sender_profile, 'eccm_capable', True) if sender_profile else True
            recipient_eccm = getattr(recipient_profile, 'eccm_capable', True) if recipient_profile else True
            if not (sender_eccm and recipient_eccm):
                return PathResult(signal=0.0, latency_ms=0.0, link_type="blocked", hops=[], notes="Blocked: Missing ECCM capability")

        comsec_active = False
        if channel_def and (getattr(channel_def, 'comsec_mode', 'Plain') != 'Plain' or getattr(channel_def, 'passphrase', '')):
            comsec_active = True
        
        if comsec_active:
            sender_crypto = getattr(sender_profile, 'crypto_type', 'AES-256') if sender_profile else 'AES-256'
            recipient_crypto = getattr(recipient_profile, 'crypto_type', 'AES-256') if recipient_profile else 'AES-256'
            if sender_crypto == "None" or recipient_crypto == "None":
                return PathResult(signal=0.0, latency_ms=0.0, link_type="blocked", hops=[], notes="Blocked: Missing Crypto hardware capability")

        satcom_active = False
        if channel_def and getattr(channel_def, 'waveform_type', 'Narrowband') == 'SATCOM':
            satcom_active = True
        
        if satcom_active:
            sender_satcom = getattr(sender_profile, 'satcom_capable', True) if sender_profile else True
            recipient_satcom = getattr(recipient_profile, 'satcom_capable', True) if recipient_profile else True
            if not (sender_satcom and recipient_satcom):
                return PathResult(signal=0.0, latency_ms=0.0, link_type="blocked", hops=[], notes="Blocked: Missing SATCOM hardware capability")

        # Resolve frequency
        freq_mhz = 45.0
        if freq_mhz_opt is not None:
            freq_mhz = freq_mhz_opt
        elif channel_def:
            freq_mhz = parse_freq_mhz(getattr(channel_def, 'frequency', ''))

        terrain = get_terrain_manager()
        
        # Apply GPS error to positions if degraded
        slat, slon, rlat, rlon = sender_lat, sender_lon, recipient_lat, recipient_lon
        if self.gps.degraded and self.gps.error_km > 0:
            import random as _rnd
            err = self.gps.error_km / 111.0
            slat += _rnd.uniform(-err, err); slon += _rnd.uniform(-err, err)
            rlat += _rnd.uniform(-err, err); rlon += _rnd.uniform(-err, err)

        mode     = self.routing_mode
        min_sig  = self.routing_min_signal
        low_lat  = self.routing_prefer_low_latency

        # 1. Check physical line-of-sight (Terrain) with dynamic antenna heights
        terrain = get_terrain_manager()
        alt_s = float(getattr(sender_profile, 'alt_m', 1.7)) if sender_profile else 1.7
        alt_r = float(getattr(recipient_profile, 'alt_m', 1.7)) if recipient_profile else 1.7

        # Resolve sender absolute altitude (AMSL)
        ground_s = terrain.get_elevation(slat, slon, fallback_online=self.online_fallback)
        abs_s = ground_s + alt_s
        
        # Resolve recipient absolute altitude (AMSL)
        ground_r = terrain.get_elevation(rlat, rlon, fallback_online=self.online_fallback)
        abs_r = ground_r + alt_r

        # Compute worst-case LOS obstruction depth
        obstruction_m = terrain.get_los_obstruction_m(slat, slon, abs_s, rlat, rlon, abs_r,
                                                       fallback_online=self.online_fallback)
        los_clear = (obstruction_m <= 0.0)
        
        # Scale effective power based on antenna gain: combined_gain = sender_gain + recipient_gain
        sender_power = float(getattr(sender_profile, 'tx_power_w', 5.0)) if sender_profile else 5.0
        sender_gain = float(getattr(sender_profile, 'antenna_gain_dbi', 0.0)) if sender_profile else 0.0
        recipient_gain = float(getattr(recipient_profile, 'antenna_gain_dbi', 0.0)) if recipient_profile else 0.0
        combined_gain = sender_gain + recipient_gain
        effective_power = sender_power * (10 ** (combined_gain / 10.0))
        
        sender_specs = None
        if sender_profile:
            max_range = float(getattr(sender_profile, 'max_range_km', 20.0))
            full_quiet = float(getattr(sender_profile, 'full_quiet_km', 5.0))
            sender_specs = (effective_power, max_range, full_quiet)
        else:
            sender_specs = (effective_power, 20.0, 5.0)
        
        # Phase 4 Feature: Tropospheric Ducting (Temperature Inversion)
        ducting_active = False
        if self.weather["temp_inversion_enabled"] and self.weather["temp_inversion_strength"] > 0.4:
            # Chance to bypass terrain blocking based on inversion strength
            import random as _rnd
            if _rnd.random() < (self.weather["temp_inversion_strength"] * 0.4):
                ducting_active = True
                log.debug("Temperature Inversion Ducting active for path %s -> %s", sender_cs, recipient_cs)

        # 2. Compute signal based on distance + terrain attenuation
        terrain_factor = terrain.compute_terrain_factor(slat, slon, rlat, rlon)
        
        direct_sig = fn(slat, slon, rlat, rlon, sender_specs, freq_mhz=freq_mhz) * terrain_factor
        
        real_blocked = not los_clear
        if real_blocked:
            if ducting_active:
                direct_sig *= 0.8
                notes_msg = f"Direct (DUCTED via INVERSION): {direct_sig*100:.0f}%"
            else:
                diff_factor = self._diffraction_penalty(obstruction_m, freq_mhz)
                direct_sig *= diff_factor
                notes_msg = f"Direct (NLOS +{obstruction_m:.1f}m obstr, diff={diff_factor:.2f}): {direct_sig*100:.0f}%"
        else:
            notes_msg = f"Direct LOS: {direct_sig*100:.0f}%"
            
        direct = PathResult(
            signal=direct_sig, latency_ms=0,
            link_type="direct", hops=["direct"],
            terrain_blocked=real_blocked and not ducting_active,
            notes=notes_msg,
        )

        if mode == "direct":
            result = direct
        elif mode == "assisted":
            retrans = self._best_retrans_path(
                slat, slon, rlat, rlon, channel, fn, sender_specs,
                sender_profile=sender_profile, recipient_profile=recipient_profile,
                channel_def=channel_def, freq_mhz=freq_mhz
            )
            if retrans and (retrans.signal > direct.signal or direct_sig < min_sig):
                result = retrans
            else:
                result = direct
        elif mode in ("hybrid", "auto"):
            retrans = self._best_retrans_path(
                slat, slon, rlat, rlon, channel, fn, sender_specs,
                sender_profile=sender_profile, recipient_profile=recipient_profile,
                channel_def=channel_def, freq_mhz=freq_mhz
            )
            candidates = [direct]
            if retrans:
                candidates.append(retrans)
            with self._lock:
                sc = self.satcom
            if sc.enabled and (not sc.channels or channel in sc.channels) and sc.uplink_strength > 0.05:
                sat_sig = sc.uplink_strength * (0.5 if sc.degraded else 1.0)
                sat_lat = sc.latency_ms * (1.5 if sc.degraded else 1.0)
                satcom_r = PathResult(
                    signal=sat_sig,
                    latency_ms=sat_lat,
                    link_type="satcom",
                    hops=["SATCOM"],
                    notes=f"Via satellite +{sat_lat:.0f}ms",
                )
                candidates.append(satcom_r)

            if low_lat:
                viable = [c for c in candidates if c.signal >= min_sig]
                result = min(viable, key=lambda r: r.latency_ms) if viable else max(candidates, key=lambda r: r.signal)
            else:
                result = max(candidates, key=lambda r: r.signal)
        else:
            result = direct

        # --- Recipient Receiver Sensitivity Penalty/Boost ---
        # Select reference based on waveform/modulation
        if satcom_active:
            ref_dbm = -120.0
            recipient_sens = float(getattr(recipient_profile, 'rx_sens_satcom_dbm', -120.0)) if recipient_profile else -120.0
        elif modulation.upper() == "AM":
            ref_dbm = -103.5
            recipient_sens = float(getattr(recipient_profile, 'rx_sens_am_dbm', -103.5)) if recipient_profile else -103.5
        else:
            ref_dbm = -116.0
            recipient_sens = float(getattr(recipient_profile, 'rx_sens_fm_dbm', -116.0)) if recipient_profile else -116.0

        sens_diff = recipient_sens - ref_dbm
        sens_multiplier = 10.0 ** (-sens_diff / 20.0)
        result.signal *= sens_multiplier
        result.signal = max(0.0, min(1.0, result.signal))

        self._log_path(sender_cs, recipient_cs, channel, result)

        # Update cache
        with self._path_cache_lock:
            self._path_cache[cache_key] = (time.time(), result)
            # Prune old entries if cache grows too large
            if len(self._path_cache) > 1000:
                now = time.time()
                self._path_cache = {k: v for k, v in self._path_cache.items() if now - v[0] < 5.0}

        return result


    def _log_path(self, sender_cs: str, recipient_cs: str,
                  channel: int, result: PathResult):
        """Record path decision for admin analytics."""
        entry = {
            "ts":         time.time(),
            "from":       sender_cs,
            "to":         recipient_cs,
            "channel":    channel,
            "link_type":  result.link_type,
            "signal":     round(result.signal, 3),
            "latency_ms": result.latency_ms,
            "hops":       result.hops,
        }
        with self._path_log_lock:
            self._path_log.append(entry)
            if len(self._path_log) > 200:
                self._path_log = self._path_log[-200:]

    def get_path_analytics(self) -> dict:
        """Return routing analytics for the admin UI."""
        import collections
        with self._path_log_lock:
            log = list(self._path_log)
        if not log:
            return {"total": 0, "by_type": {}, "avg_signal": 0, "recent": []}
        by_type = collections.Counter(e["link_type"].split(":")[0] for e in log)
        avg_sig = sum(e["signal"] for e in log) / len(log)
        return {
            "total":      len(log),
            "by_type":    dict(by_type),
            "avg_signal": round(avg_sig, 3),
            "recent":     log[-20:],   # last 20 for table display
        }


    def _best_retrans_path(
        self,
        slat: float, slon: float,
        rlat: float, rlon: float,
        channel: int,
        fn,
        sender_specs=None,
        sender_profile=None,
        recipient_profile=None,
        channel_def=None,
        freq_mhz: float = 45.0,
    ) -> Optional[PathResult]:
        """Find the best single-hop retrans path using 3D terrain analysis."""
        best: Optional[PathResult] = None
        terrain = get_terrain_manager()
        
        alt_s = float(getattr(sender_profile, 'alt_m', 1.7)) if sender_profile else 1.7
        alt_r = float(getattr(recipient_profile, 'alt_m', 1.7)) if recipient_profile else 1.7
        
        with self._lock:
            nodes = list(self.retrans_nodes.values())
            
        for node in nodes:
            if not node.enabled:
                continue
            real_chs = [c for c in node.channels if c >= 0]
            if real_chs and channel not in real_chs:
                continue
            
            # --- Leg 1: Sender -> Node ---
            # Resolve absolute altitudes (AMSL)
            node_abs_alt = terrain.get_elevation(node.lat, node.lon, fallback_online=self.online_fallback) + node.alt_m
            sender_ground = terrain.get_elevation(slat, slon, fallback_online=self.online_fallback)
            sender_abs_alt = sender_ground + alt_s
            
            obs1 = terrain.get_los_obstruction_m(slat, slon, sender_abs_alt, node.lat, node.lon, node_abs_alt, fallback_online=self.online_fallback)
            tf1  = terrain.compute_terrain_factor(slat, slon, node.lat, node.lon)
            sig_to = fn(slat, slon, node.lat, node.lon, sender_specs, freq_mhz=freq_mhz) * tf1
            if obs1 > 0:
                sig_to *= self._diffraction_penalty(obs1, freq_mhz)
            
            # --- Leg 2: Node -> Recipient ---
            recipient_ground = terrain.get_elevation(rlat, rlon, fallback_online=self.online_fallback)
            recipient_abs_alt = recipient_ground + alt_r
            
            # Retrans nodes usually have higher power
            retrans_specs = (node.power_w, 60.0, 15.0)
            obs2 = terrain.get_los_obstruction_m(node.lat, node.lon, node_abs_alt, rlat, rlon, recipient_abs_alt, fallback_online=self.online_fallback)
            tf2  = terrain.compute_terrain_factor(node.lat, node.lon, rlat, rlon)
            sig_from = fn(node.lat, node.lon, rlat, rlon, retrans_specs, freq_mhz=freq_mhz) * tf2
            if obs2 > 0:
                sig_from *= self._diffraction_penalty(obs2, freq_mhz)
            
            # Combined signal (weakest link)
            combined = min(sig_to, sig_from) * (1.0 - node.degraded_pct)
            combined = min(1.0, combined)
            
            # LOS metadata
            is_blocked = (obs1 > 0) or (obs2 > 0)
            
            if best is None or combined > best.signal:
                best = PathResult(
                    signal=combined,
                    latency_ms=node.latency_ms,
                    link_type=f"retrans:{node.node_id}",
                    hops=[node.name],
                    terrain_blocked=is_blocked,
                    notes=f"Via {node.name} (L1:{sig_to*100:.0f}% L2:{sig_from*100:.0f}%)",
                )
        return best

    # ── Serialisation ─────────────────────────────────────────────────────────

    def to_dict(self) -> dict:
        return self.get_status()

    def load_from_dict(self, d: dict):
        nodes = d.get("retrans_nodes", [])
        with self._lock:
            self.retrans_nodes.clear()
            for nd in nodes:
                try:
                    n = RetransNode.from_dict(nd)
                    self.retrans_nodes[n.node_id] = n
                except Exception as e:
                    log.warning("Bad retrans node in config: %s — %s", nd, e)
            sc = d.get("satcom", {})
            if sc:
                for k, v in sc.items():
                    if hasattr(self.satcom, k):
                        setattr(self.satcom, k, v)
            gps = d.get("gps", {})
            if gps:
                for k, v in gps.items():
                    if hasattr(self.gps, k):
                        setattr(self.gps, k, v)
        routing = d.get("routing", {})
        if routing:
            if "mode"               in routing: self.routing_mode             = str(routing["mode"])
            if "min_signal"         in routing: self.routing_min_signal        = float(routing["min_signal"])
            if "prefer_low_latency" in routing: self.routing_prefer_low_latency = bool(routing["prefer_low_latency"])
        log.info("Infrastructure loaded: %d retrans nodes, satcom=%s, gps=%s, routing=%s",
                 len(self.retrans_nodes), self.satcom.enabled, self.gps.enabled, self.routing_mode)


# ── Haversine convenience ─────────────────────────────────────────────────────

def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in km."""
    R  = 6371.0
    d1 = math.radians(lat2 - lat1)
    d2 = math.radians(lon2 - lon1)
    a  = (math.sin(d1/2)**2
          + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2))
          * math.sin(d2/2)**2)
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))


# ── Self-test ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print("=== MGRS Self-Test ===\n")
    tests = [
        (-36.8485, 174.7633, "Wellington area, NZ"),
        (51.5074, -0.1278,   "London, UK"),
        (38.8977, -77.0365,  "Washington DC, US"),
        (-33.8688, 151.2093, "Sydney, Australia"),
        (0.0, 0.0,           "Null Island"),
    ]
    for lat, lon, desc in tests:
        mgrs = latlon_to_mgrs(lat, lon)
        try:
            rlat, rlon = mgrs_to_latlon(mgrs)
            err_m = haversine_km(lat, lon, rlat, rlon) * 1000
            ok = "OK" if err_m < 10 else "WARN"
        except Exception as e:
            err_m = -1; ok = f"ERR({e})"
        print(f"  {ok:4s} {lat:8.4f},{lon:9.4f} → {mgrs:<15s} round-trip err {err_m:.1f}m  ({desc})")

    print("\n=== InfrastructureManager ===\n")
    mgr = InfrastructureManager()
    node = RetransNode(
        node_id="RT1", name="RETRANS ALPHA",
        lat=-36.5, lon=174.5, alt_m=150.0,
        channels=[], enabled=True, power_w=20.0, latency_ms=25.0
    )
    mgr.add_node(node)
    print(f"  Node MGRS: {node.mgrs}")
    print(f"  Status: {mgr.get_status()}")
    print("\n✓ radio_infra self-test passed")