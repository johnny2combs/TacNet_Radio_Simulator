"""
TacNet — Configuration
Channel plans, callsign management, terrain data, and config persistence.
"""
from __future__ import annotations
import os
import json
import logging
import configparser
from dataclasses import dataclass, field, asdict
from pathlib import Path
import sys
from typing import Dict, List, Optional, Tuple
log = logging.getLogger("radio.config")

# ── Default config path ────────────────────────────────────────────────────────
def resource_path(relative: str) -> Path:
    """Return an absolute Path to a bundled resource.
    Works when running from source or from a PyInstaller‑created exe.
    """
    # 1. Check for PyInstaller temp extraction dir (_MEIPASS)
    base = getattr(sys, "_MEIPASS", None)
    if base:
        p = Path(base) / relative
        if p.exists(): return p

    # 2. Check next to executable (production distribution)
    if getattr(sys, 'frozen', False):
        exe_dir = Path(sys.executable).parent
        # Check alongside exe
        p = exe_dir / relative
        if p.exists(): return p
        # Check parent of exe dir (common for our subfolder structure)
        p = exe_dir.parent / relative
        if p.exists(): return p

    # 3. Check next to source file (development)
    p = Path(__file__).parent / relative
    if p.exists(): return p
    
    # Fallback to working directory
    return Path(relative)

def get_default_config_path() -> Path:
    """Search for radio_config.ini, falling back to radio_settings.ini."""
    # Priority 1: radio_config.ini
    p = resource_path("radio_config.ini")
    if p.exists(): return p
    
    # Priority 2: radio_settings.ini (legacy/fallback)
    p = resource_path("radio_settings.ini")
    if p.exists(): return p
    
    # Default to radio_config.ini next to exe/script for creation
    base = Path(sys.executable).parent if getattr(sys, 'frozen', False) else Path(__file__).parent
    return base / "radio_config.ini"

DEFAULT_CONFIG_PATH = get_default_config_path()

# ── Terrain Types ──────────────────────────────────────────────────────────────
class TerrainType:
    """Terrain type definitions with signal attenuation factors."""
    OPEN = "open"           # Flat, minimal obstruction (attenuation: 1.0)
    FOREST = "forest"       # Trees, medium attenuation (attenuation: 0.6)
    URBAN = "urban"         # Buildings, high obstruction (attenuation: 0.3)
    MOUNTAIN = "mountain"   # High elevation, LOS blocking (attenuation: 0.1)
    WATER = "water"         # Minimal obstruction, reflective (attenuation: 0.9)
    
    # Attenuation factors (multiplier to signal strength)
    ATTENUATION = {
        "open": 1.0,
        "forest": 0.6,
        "urban": 0.3,
        "mountain": 0.1,
        "water": 0.9,
    }
    
    # Elevation thresholds (metres above sea level)
    ELEVATION_LOW = 100      # Below this = open/water
    ELEVATION_MID = 500      # Below this = forest
    ELEVATION_HIGH = 1500    # Above this = mountain

# ── Terrain Grid Cell ──────────────────────────────────────────────────────────
@dataclass
class TerrainCell:
    """Single grid cell in the terrain map."""
    lat_min: float
    lat_max: float
    lon_min: float
    lon_max: float
    terrain_type: str = "open"
    elevation_m: float = 0.0
    # Optional: custom attenuation override
    attenuation_override: Optional[float] = None
    
    def get_attenuation(self) -> float:
        """Return signal attenuation factor for this cell."""
        if self.attenuation_override is not None:
            return self.attenuation_override
        return TerrainType.ATTENUATION.get(self.terrain_type, 1.0)
    
    def contains(self, lat: float, lon: float) -> bool:
        """Check if a lat/lon point is within this cell."""
        return (self.lat_min <= lat <= self.lat_max and
                self.lon_min <= lon <= self.lon_max)
    
    def to_dict(self) -> dict:
        return asdict(self)
    
    @staticmethod
    def from_dict(d: dict) -> 'TerrainCell':
        return TerrainCell(**{k: v for k, v in d.items() if k in TerrainCell.__dataclass_fields__})

# ── Map Marker Types ───────────────────────────────────────────────────────────
class MarkerType:
    CALLSIGN = "callsign"
    RETRANS = "retrans"
    SATCOM = "satcom"
    CUSTOM = "custom"

# ── Map Marker ─────────────────────────────────────────────────────────────────
@dataclass
class MapMarker:
    """A marker placed on the tactical map."""
    marker_id: str
    marker_type: str
    name: str
    lat: float
    lon: float
    alt: float = 0.0
    channel: int = -1
    callsign: str = ""  # For callsign markers
    enabled: bool = True
    color: str = "#00ff00"  # Display color
    
    def to_dict(self) -> dict:
        return asdict(self)
    
    @staticmethod
    def from_dict(d: dict) -> 'MapMarker':
        return MapMarker(**{k: v for k, v in d.items() if k in MapMarker.__dataclass_fields__})

# ── Terrain Map Configuration ──────────────────────────────────────────────────
@dataclass
class TerrainMap:
    """Terrain map configuration."""
    enabled: bool = True
    grid_size_deg: float = 0.01  # ~1km per cell
    default_terrain: str = "open"
    cells: List[TerrainCell] = field(default_factory=list)
    markers: List[MapMarker] = field(default_factory=list)
    # Map source
    map_source: str = "canvas"  # canvas | online | offline
    online_tile_url: str = ""   # OSM tile URL if online
    offline_tiles_path: str = ""  # Path to offline tiles
    
    def get_cell(self, lat: float, lon: float) -> Optional[TerrainCell]:
        """Get terrain cell for a lat/lon point."""
        for cell in self.cells:
            if cell.contains(lat, lon):
                return cell
        return None
    
    def get_attenuation(self, lat: float, lon: float) -> float:
        """Get terrain attenuation for a point."""
        cell = self.get_cell(lat, lon)
        if cell:
            return cell.get_attenuation()
        return TerrainType.ATTENUATION.get(self.default_terrain, 1.0)
    
    def add_marker(self, marker: MapMarker):
        """Add or update a marker."""
        for i, m in enumerate(self.markers):
            if m.marker_id == marker.marker_id:
                self.markers[i] = marker
                return
        self.markers.append(marker)
    
    def remove_marker(self, marker_id: str) -> bool:
        """Remove a marker by ID."""
        for i, m in enumerate(self.markers):
            if m.marker_id == marker_id:
                del self.markers[i]
                return True
        return False
    
    def to_dict(self) -> dict:
        return {
            "enabled": self.enabled,
            "grid_size_deg": self.grid_size_deg,
            "default_terrain": self.default_terrain,
            "cells": [c.to_dict() for c in self.cells],
            "markers": [m.to_dict() for m in self.markers],
            "map_source": self.map_source,
            "online_tile_url": self.online_tile_url,
            "offline_tiles_path": self.offline_tiles_path,
        }
    
    @staticmethod
    def from_dict(d: dict) -> 'TerrainMap':
        terrain = TerrainMap(
            enabled=d.get("enabled", True),
            grid_size_deg=d.get("grid_size_deg", 0.01),
            default_terrain=d.get("default_terrain", "open"),
            map_source=d.get("map_source", "canvas"),
            online_tile_url=d.get("online_tile_url", ""),
            offline_tiles_path=d.get("offline_tiles_path", ""),
        )
        terrain.cells = [TerrainCell.from_dict(c) for c in d.get("cells", [])]
        terrain.markers = [MapMarker.from_dict(m) for m in d.get("markers", [])]
        return terrain

# ── Channel definition ────────────────────────────────────────────────────────
@dataclass
class ChannelDef:
    id:             int             # 0–99
    name:           str             # e.g. "COMMAND NET"
    frequency:      str             # e.g. "45.500 MHz"
    passphrase:     str             = ""    # empty = unencrypted
    description:    str             = ""
    enabled:        bool            = True
    
    # Advanced / Military Parameters
    modulation:     str             = "FM"         # FM, AM, USB, LSB, CW
    bandwidth_khz:  float           = 25.0         # 8.33, 12.5, 25, 500, 1200
    waveform_type:  str             = "Narrowband" # Narrowband, Wideband, SATCOM
    eccm_mode:      str             = "None"       # None, SINCGARS, HAVEQUICK
    net_id:         str             = "000"        # For hopping nets
    comsec_mode:    str             = "Plain"      # Plain, AES-256, Citadel
    key_id:         str             = "01"         # Crypto key index
    squelch_tone:   float           = 0.0          # CTCSS tone in Hz (0 = disabled)
    
    @property
    def encrypted(self) -> bool:
        return self.comsec_mode != "Plain" or bool(self.passphrase)
    
    @property
    def display(self) -> str:
        enc = "🔒" if self.encrypted else "🔓"
        mod = f"[{self.modulation}]"
        bw = f"{self.bandwidth_khz}k"
        return f"CH{self.id:02d}  {self.frequency}  {mod} {self.name} {enc}"

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "frequency": self.frequency,
            "passphrase": self.passphrase,
            "description": self.description,
            "enabled": self.enabled,
            "modulation": self.modulation,
            "bandwidth_khz": self.bandwidth_khz,
            "waveform_type": self.waveform_type,
            "eccm_mode": self.eccm_mode,
            "net_id": self.net_id,
            "comsec_mode": self.comsec_mode,
            "key_id": self.key_id,
            "squelch_tone": self.squelch_tone
        }

    @staticmethod
    def from_dict(d: dict) -> 'ChannelDef':
        # Handle legacy data gracefully
        return ChannelDef(
            id=d.get("id", 0),
            name=d.get("name", "NEW CHANNEL"),
            frequency=d.get("frequency", "000.000"),
            passphrase=d.get("passphrase", ""),
            description=d.get("description", ""),
            enabled=d.get("enabled", True),
            modulation=d.get("modulation", "FM"),
            bandwidth_khz=d.get("bandwidth_khz", 25.0),
            waveform_type=d.get("waveform_type", "Narrowband"),
            eccm_mode=d.get("eccm_mode", "None"),
            net_id=d.get("net_id", "000"),
            comsec_mode=d.get("comsec_mode", "Plain"),
            key_id=d.get("key_id", "01"),
            squelch_tone=d.get("squelch_tone", 0.0)
        )

# ── Radio Profile definition ───────────────────────────────────────────────────
@dataclass
class RadioProfile:
    id: str                 # e.g. "prc152", "prc117g"
    name: str               # e.g. "AN/PRC-152 Handheld"
    tx_power_w: float       # Transmit power in watts
    max_range_km: float     # Maximum range under ideal conditions
    full_quiet_km: float    # Range where signal clarity is 100%
    
    # Advanced / Under-the-hood parameters for future modeling
    freq_band: str = "VHF/UHF"         # e.g., "HF", "VHF/UHF", "SATCOM"
    freq_range_mhz: str = "30-512"     # e.g., "30-520, 762-870"
    tx_power_satcom_w: float = 10.0    # TACSAT burst power in watts
    rx_sens_fm_dbm: float = -116.0     # LOS FM Sensitivity (e.g. @ 12dB SINAD)
    rx_sens_am_dbm: float = -103.5     # LOS AM Sensitivity (e.g. @ 10dB SINAD)
    rx_sens_satcom_dbm: float = -120.0 # TACSAT FM Sensitivity
    antenna_gain_dbi: float = 0.0      # Antenna efficiency/gain
    eccm_capable: bool = False         # Electronic Counter-Countermeasures (Frequency Hopping)
    crypto_type: str = "None"          # "None", "Type-1", "AES-256"
    battery_life_hrs: float = 8.0      # Estimated battery life at 8:1:1 duty cycle
    alt_m: float = 2.0                 # Default antenna height AGL
    gps_capable: bool = True           # Internal GPS receiver
    satcom_capable: bool = False       # TACSAT / SATCOM DAMA support
    waveforms_nb: str = "AM/FM"        # Narrowband waveforms
    waveforms_wb: str = ""             # Wideband waveforms
    immersion_rating_m: float = 2.0    # Waterproofing depth (meters)
    relay_signature: str = "none"      # none | standard | heavy | digital | tactical

# ── Callsign definition ────────────────────────────────────────────────────────
@dataclass
class CallsignDef:
    callsign:    str
    real_name:   str     = ""
    unit:        str     = ""     # e.g. "1 IR"
    parent_unit: str     = ""     # Higher echelon e.g. "1 Coy" -> "1 Bn"
    role:        str     = ""     # e.g. "CO", "IO", "Sunray Minor"
    lat:         float   = 0.0
    lon:         float   = 0.0
    alt:         float   = 0.0
    # Which channels this callsign monitors (default: current channel)
    channels:    List[int] = field(default_factory=list)
    radio_profile: str   = "prc152"  # RadioProfile ID
    ptt_key:       str   = "space"     # Keyboard key name
    evicted:       bool  = False

# ── Radio config ──────────────────────────────────────────────────────────────
@dataclass
class RadioConfig:
    # Network
    server_host:        str   = "127.0.0.1"
    server_port:        int   = 55500
    server_ctrl_port:   int   = 55501   # TCP control channel
    server_voice_port:  int   = 55500   # UDP voice channel (same as above — mux by type)
    bind_address:       str   = "0.0.0.0"
    web_admin_port:     int   = 8890    # HTTP admin UI / API port
    enable_local_api:   bool  = True
    local_api_port:     int   = 8895
    
    # Identity
    callsign:           str   = "UNKNOWN"
    unit:               str   = ""
    
    # Audio
    audio_input_device:  int  = -1   # -1 = system default
    audio_output_device: int  = -1
    sample_rate:         int  = 48000
    input_gain:          float = 1.0
    output_volume:       float = 1.2
    
    # PTT
    ptt_key:             str  = "space"   # keyboard key name
    ptt_mouse_button:    int  = -1        # -1 = disabled, 4/5 = side buttons
    vox_enabled:         bool = False
    vox_threshold:       float = 0.05    # RMS threshold for VOX activation
    vox_hold_ms:         int  = 500      # hold time after signal drops
    audio_input_device:  int  = -1       # -1 = default
    audio_output_device: int  = -1       # -1 = default
    
    # Radio behaviour
    squelch:             float = 0.15
    squelch_auto:        bool  = False     # auto-squelch from server signal report
    distortion:          float = 0.0
    noise_floor:         float = 0.0
    
    # Audio effects chain control
    opus_bitrate:        int   = 24000    #  8000=narrow/sim, 16000=mid, 24000=wideband/cpx
    dynamic_squelch:     bool  = False
    ptt_click_enabled:   bool  = True     # inject PTT relay clicks on tx open/close
    ptt_type:            str   = "standard" # none | standard | heavy | digital
    bp_low_hz:           int   = 120      # RX bandpass low cut (Hz)
    bp_high_hz:          int   = 7000     # RX bandpass high cut (Hz)
    crackle_lvl:         float = 0.0      # random crackles/pops intensity
    squelch_tail_enabled: bool = False    # add noise tail after PTT release
    relay_signature: str = "none"         # server-assigned signature override
    ptt_tone_f1: float = 1200.0           # Tactical/Standard tone 1 frequency
    ptt_tone_f2: float = 1600.0           # Tactical tone 2 frequency
    
    # Advanced Audio Engine Settings
    filter_order:        int   = 4
    
    # ── Weather & Atmosphere effects ───────────────────────────────────────────
    # Layer A — Precipitation
    weather_enabled:          bool  = False   # precipitation effects on/off
    weather_severity:         float = 0.0     # 0.0=clear  1.0=severe
    weather_type:             str   = "rain"  # rain | storm | interference | fog
    weather_humidity:         float = 0.0     # 0=dry  1=saturated; drives HF rolloff
    
    # Layer B — Temperature Inversion
    temp_inversion_enabled:   bool  = False   # atmospheric ducting on/off
    temp_inversion_strength:  float = 0.0     # 0=none  1=strong ducting / multi-path
    temp_inversion_echo_ms:   float = 10.0    # echo delay 5–20 ms (multi-path spread)
    
    # Layer C — Ionospheric Conditions (primarily HF)
    iono_enabled:             bool  = False   # ionospheric effects on/off
    iono_condition:           str   = "quiet" # quiet | disturbed | storm | blackout
    iono_fading:              float = 0.0     # slow fading depth 0–1 (0.1–2 Hz)
    iono_flutter:             float = 0.0     # fast flutter depth  0–1 (10–20 Hz)
    iono_absorption:          float = 0.0     # signal absorption 0=none  1=blackout
    
    # Layer D — Day / Night Cycle
    day_night_enabled:        bool  = False   # day/night propagation effects on/off
    day_night_hour:           float = 12.0    # simulated hour 0.0–23.99
    day_night_auto:           bool  = False   # sync to real-world wall-clock time
    day_night_latitude:       float = 0.0     # for sunrise/sunset calc (decimal degrees)
    
    # Layer E — EMI (Electromagnetic Interference)
    emi_enabled:              bool  = False   # EMI noise layer on/off
    emi_level:                float = 0.0     # background EMI noise 0.0–1.0
    emi_type:                 str   = "white" # white | hum | burst
    
    # Range / simulation
    range_enabled:       bool  = False
    range_max_km:        float = 25.0
    range_full_quiet_km: float = 5.0
    transmit_power_w:    float = 20.0
    terrain_factor:      float = 1.0
    
    # ── Communications Infrastructure ─────────────────────────────────────────
    # ── Signal Routing ────────────────────────────────────────────────────────
    routing_mode:        str   = "auto"  # direct|assisted|hybrid|auto
    routing_min_signal:  float = 0.15   # min acceptable signal before trying next path
    routing_prefer_low_latency: bool = False  # prefer fewer hops even at cost of signal
    infra_nodes_json:    str   = "[]"  # JSON list of RetransNode dicts
    satcom_enabled:      bool  = False
    satcom_latency_ms:   float = 280.0
    satcom_channels:     str   = "[]"  # JSON list of channel ints; [] = all
    gps_enabled:         bool  = True
    gps_degraded:        bool  = False
    gps_accuracy_m:      float = 5.0
    gps_error_km:        float = 0.0
    
    # ── Terrain Map System ────────────────────────────────────────────────────
    terrain_map_json:    str   = "{}"  # JSON TerrainMap data
    
    # Radio Profile (persisted)
    radio_profile:       str   = "prc152"
    
    # TAK integration
    tak_enabled:         bool  = False
    tak_server_url:      str   = "tcp://127.0.0.1:8087"
    tak_callsign:        str   = ""     # override callsign for CoT (defaults to radio callsign)
    tak_ptt_events:      bool  = True   # send PTT on/off as CoT events
    tak_position_sync:   bool  = True   # receive position updates from TAK
    
    # AAR recording
    aar_enabled:         bool  = False
    aar_dir:             str   = "aar"
    aar_exercise_name:   str   = "EXERCISE"
    aar_time_source:     str   = "wall"      # "wall" | "manual" | "sword"
    aar_h_hour:          str   = ""          # HH:MM:SS — used when time_source=manual
    aar_sword_url:       str   = "http://127.0.0.1:8888"  # SimBridge URL for sim-time
    
    # SWORD integration
    sword_enabled:       bool  = False
    sword_gateway_url:   str   = "http://127.0.0.1:8888"  # SimBridge web API
    sword_party:         str   = "blue"
    sword_sync_interval: int   = 10     # seconds
    
    # GUI
    gui_theme:           str   = "dark"
    gui_always_on_top:   bool  = True
    gui_compact:         bool  = False
    gui_show_spectrum:   bool  = True
    gui_window_x:        int   = -1     # -1 = auto-centre
    gui_window_y:        int   = -1
    
    # ── Tactical Features ─────────────────────────────────────────────────────
    spatial_audio:       bool  = False  # Feature 4: split-ear spatial audio
    retrans_links:       str   = ""     # Feature 3: e.g. "1-2, 2-3" BFS relay
    
    # Current state (not persisted)
    current_channel:     int   = -1
    connected:           bool  = field(default=False, compare=False, repr=False)

# ── Radio profile presets ──────────────────────────────────────────────────────
PRESET_PROFILES: List[RadioProfile] = [
    RadioProfile(
        id="prc152", name="AN/PRC-152 Handheld",
        tx_power_w=5.0, max_range_km=8.0, full_quiet_km=2.0,
        freq_band="VHF/UHF/SATCOM", freq_range_mhz="30-520, 762-870", tx_power_satcom_w=10.0,
        rx_sens_fm_dbm=-116.0, rx_sens_am_dbm=-103.5, rx_sens_satcom_dbm=-120.0,
        antenna_gain_dbi=1.0, eccm_capable=True, crypto_type="Type-1", battery_life_hrs=8.0, alt_m=1.6,
        gps_capable=True, satcom_capable=True, waveforms_nb="AM/FM/VULOS/SINCGARS/HQ", waveforms_wb="ANW2C/SRW", immersion_rating_m=2.0,
        relay_signature="tactical"
    ),
    RadioProfile(
        id="prc148", name="AN/PRC-148 JEM",
        tx_power_w=5.0, max_range_km=7.0, full_quiet_km=1.5,
        freq_band="VHF/UHF", freq_range_mhz="30-512", tx_power_satcom_w=0.0,
        rx_sens_fm_dbm=-116.0, rx_sens_am_dbm=-103.5, rx_sens_satcom_dbm=0.0,
        antenna_gain_dbi=0.5, eccm_capable=True, crypto_type="Type-1", battery_life_hrs=10.0, alt_m=1.6,
        gps_capable=True, satcom_capable=False, waveforms_nb="AM/FM/VULOS/SINCGARS", waveforms_wb="", immersion_rating_m=20.0,
        relay_signature="standard"
    ),
    RadioProfile(
        id="prc117g", name="AN/PRC-117G Manpack",
        tx_power_w=20.0, max_range_km=25.0, full_quiet_km=6.0,
        freq_band="VHF/UHF/SATCOM", freq_range_mhz="30-2000", tx_power_satcom_w=20.0,
        rx_sens_fm_dbm=-118.0, rx_sens_am_dbm=-105.0, rx_sens_satcom_dbm=-122.0,
        antenna_gain_dbi=2.5, eccm_capable=True, crypto_type="Type-1", battery_life_hrs=12.0, alt_m=2.0,
        gps_capable=True, satcom_capable=True, waveforms_nb="AM/FM/VULOS/SINCGARS/HQ", waveforms_wb="ANW2C/SRW/ROVER", immersion_rating_m=1.0,
        relay_signature="heavy"
    ),
    RadioProfile(
        id="prc160", name="AN/PRC-160 HF Manpack",
        tx_power_w=20.0, max_range_km=3000.0, full_quiet_km=15.0,
        freq_band="HF/VHF", freq_range_mhz="1.5-60", tx_power_satcom_w=0.0,
        rx_sens_fm_dbm=-112.0, rx_sens_am_dbm=-100.0, rx_sens_satcom_dbm=0.0,
        antenna_gain_dbi=-5.0, eccm_capable=False, crypto_type="Type-1", battery_life_hrs=15.0, alt_m=3.0,
        gps_capable=True, satcom_capable=False, waveforms_nb="AM/FM/SSB/ALE/3G", waveforms_wb="", immersion_rating_m=1.0
    ),
    RadioProfile(
        id="sincgars_veh", name="RT-1523 SINCGARS (Vehicle)",
        tx_power_w=50.0, max_range_km=40.0, full_quiet_km=15.0,
        freq_band="VHF", freq_range_mhz="30-88", tx_power_satcom_w=0.0,
        rx_sens_fm_dbm=-112.0, rx_sens_am_dbm=0.0, rx_sens_satcom_dbm=0.0,
        antenna_gain_dbi=3.0, eccm_capable=True, crypto_type="Type-1", battery_life_hrs=99.0, alt_m=3.5,
        gps_capable=False, satcom_capable=False, waveforms_nb="FM/SINCGARS", waveforms_wb="", immersion_rating_m=0.0,
        relay_signature="standard"
    ),
]

# ── Channel plan presets ───────────────────────────────────────────────────────
PRESET_EXERCISE_CHANNELS: List[ChannelDef] = [
    ChannelDef(0,   "GUARD / ADMIN",    "243.000 MHz",   "",              "Guard frequency — always monitored", modulation="AM", bandwidth_khz=25.0, waveform_type="Narrowband", comsec_mode="Plain"),
    ChannelDef(1,   "COMMAND NET",      "45.500 MHz",    "EXERCISE-NET1", "Regimental command net", modulation="FM", bandwidth_khz=25.0, waveform_type="Narrowband", eccm_mode="SINCGARS", net_id="123", comsec_mode="AES-256", key_id="01"),
    ChannelDef(2,   "ADMIN LOG NET",    "46.000 MHz",    "EXERCISE-NET2", "Administration and logistics", modulation="FM", bandwidth_khz=12.5, waveform_type="Narrowband", comsec_mode="AES-256", key_id="02"),
    ChannelDef(3,   "FIRE SUPPORT",     "46.500 MHz",    "EXERCISE-NET3", "Fire support coordination", modulation="FM", bandwidth_khz=25.0, waveform_type="Narrowband", eccm_mode="SINCGARS", net_id="456", comsec_mode="Citadel"),
    ChannelDef(4,   "AVIATION COMD",    "130.000 MHz",   "",              "Aviation coordination (Clear)", modulation="AM", bandwidth_khz=8.33, waveform_type="Narrowband", comsec_mode="Plain"),
    ChannelDef(5,   "ENGINEER",         "47.000 MHz",    "EXERCISE-NET5", "Engineer coordination", modulation="FM", bandwidth_khz=25.0, waveform_type="Narrowband", eccm_mode="SINCGARS", net_id="789", comsec_mode="AES-256"),
    ChannelDef(6,   "MEDICAL",          "47.500 MHz",    "",              "Medical evacuation (unencrypted)", modulation="FM", bandwidth_khz=25.0, waveform_type="Narrowband", comsec_mode="Plain", squelch_tone=150.0),
    ChannelDef(7,   "INTEL / EW",       "48.000 MHz",    "EXERCISE-NET7", "Intelligence and EW net", modulation="FM", bandwidth_khz=12.5, waveform_type="Narrowband", comsec_mode="AES-256", key_id="09"),
    ChannelDef(8,   "AIR CONTROL",      "225.000 MHz",   "EXERCISE-NET8", "Air control (HAVEQUICK)", modulation="AM", bandwidth_khz=25.0, waveform_type="Narrowband", eccm_mode="HAVEQUICK", net_id="HQ1", comsec_mode="Plain"),
    ChannelDef(9,   "WB DATA LINK",     "480.500 MHz",   "EXERCISE-DATA", "High-speed wideband data", modulation="FM", bandwidth_khz=1200.0, waveform_type="Wideband", comsec_mode="AES-256", key_id="12"),
    # Feature 5: Vehicle Intercom System — bypasses all RF path-loss/jamming on server
    ChannelDef(99,  "VEHICLE INTERCOM", "LOCAL WIRE",    "",              "Vehicle hardwired local intercom — always full-strength", modulation="FM", bandwidth_khz=8.0, waveform_type="Narrowband", comsec_mode="Plain"),
]

PRESET_CALLSIGNS: List[CallsignDef] = [
    CallsignDef("0A",           real_name="",  unit="BG HQ",     role="CO"),
    CallsignDef("OB",           real_name="",  unit="BG HQ",     role="2IC"),
    CallsignDef("STARLIGHT",    real_name="",  unit="BG HQ",     role="IO"),
    CallsignDef("ACORN",        real_name="",  unit="A COY",     role="OC"),
    CallsignDef("ACORN 1",      real_name="",  unit="A COY 1PL", role="PL COMD"),
    CallsignDef("ACORN 2",      real_name="",  unit="A COY 2PL", role="PL COMD"),
    CallsignDef("ACORN 3",      real_name="",  unit="A COY 3PL", role="PL COMD"),
    CallsignDef("BLUEBELL",     real_name="",  unit="B COY",     role="OC"),
    CallsignDef("BLUEBELL 1",   real_name="",  unit="B COY 1PL", role="PL COMD"),
    CallsignDef("BLUEBELL 2",   real_name="",  unit="B COY 2PL", role="PL COMD"),
    CallsignDef("COWSLIP",      real_name="",  unit="C COY",     role="OC"),
    CallsignDef("TARA",         real_name="",  unit="CSS COY",   role="OC"),
    CallsignDef("HAWK",         real_name="",  unit="AVIATION",  role="PILOT"),
    CallsignDef("ZERO",         real_name="",  unit="ALL",       role="OPS ROOM"),
]

# ── Config manager ────────────────────────────────────────────────────────────
class ConfigManager:
    """
    Loads/saves RadioConfig and channel/callsign lists from/to an INI file.
    Thread-safe read; write acquires a lock.
    """
    
    def __init__(self, path: Path = DEFAULT_CONFIG_PATH):
        self.path     = Path(path)
        self.config   = RadioConfig()
        self.profiles: Dict[str, RadioProfile] = {}
        self.channels: Dict[int, ChannelDef] = {}
        self.callsigns: Dict[str, CallsignDef] = {}
        self._terrain_map: TerrainMap = TerrainMap()
        self._load_defaults()
        if self.path.exists():
            self.load()
    
    def _load_defaults(self):
        """Load the preset channel plan and callsigns as defaults."""
        for p in PRESET_PROFILES:
            self.profiles[p.id] = p
        for ch in PRESET_EXERCISE_CHANNELS:
            self.channels[ch.id] = ch
        for cs in PRESET_CALLSIGNS:
            cs.callsign = cs.callsign.upper()
            self.callsigns[cs.callsign] = cs
    
    @property
    def terrain_map(self) -> TerrainMap:
        """Get terrain map configuration."""
        return self._terrain_map
    
    @terrain_map.setter
    def terrain_map(self, value: TerrainMap):
        """Set terrain map configuration."""
        self._terrain_map = value
        # Persist to config JSON
        self.config.terrain_map_json = json.dumps(value.to_dict())
    
    def load(self):
        """Load config from INI file."""
        try:
            cfg = configparser.ConfigParser()
            cfg.read(str(self.path), encoding="utf-8")
            
            # [radio] section → RadioConfig fields
            if cfg.has_section("radio"):
                r = cfg["radio"]
                for f_name, f_val in self.config.__dataclass_fields__.items():
                    if f_name in r:
                        raw = r[f_name]
                        cur = getattr(self.config, f_name)
                        try:
                            if isinstance(cur, bool):
                                setattr(self.config, f_name, raw.lower() in ("true", "1", "yes"))
                            elif isinstance(cur, int):
                                setattr(self.config, f_name, int(raw))
                            elif isinstance(cur, float):
                                setattr(self.config, f_name, float(raw))
                            else:
                                if f_name == "callsign":
                                    raw = raw.upper()
                                setattr(self.config, f_name, raw)
                        except (ValueError, TypeError):
                            pass
                            
                # Migrate legacy generic profiles to authentic mil-profiles
                legacy_map = {"handheld": "prc152", "manpack": "prc117g", "vehicle": "sincgars_veh"}
                if self.config.radio_profile in legacy_map:
                    self.config.radio_profile = legacy_map[self.config.radio_profile]

            # Dynamic-initialize audio/DSP parameters on boot since they are not persisted to INI
            if self.config.range_enabled:
                self.config.distortion = 0.15
                self.config.noise_floor = 0.018
                self.config.crackle_lvl = 0.08
                self.config.ptt_click_enabled = True
                self.config.ptt_type = "standard"
                self.config.relay_signature = "tactical"
                self.config.tx_highpass_enabled = True
                self.config.tx_preemph_enabled = True
                self.config.bp_low_hz = 300
                self.config.bp_high_hz = 3000
                self.config.squelch_tail_enabled = True
                self.config.squelch = 0.15
                self.config.squelch_auto = True
            else:
                self.config.distortion = 0.0
                self.config.noise_floor = 0.0
                self.config.crackle_lvl = 0.0
                self.config.ptt_click_enabled = True
                self.config.ptt_type = "standard"
                self.config.relay_signature = "none"
                self.config.tx_highpass_enabled = False
                self.config.tx_preemph_enabled = False
                self.config.bp_low_hz = 120
                self.config.bp_high_hz = 7000
                self.config.squelch_tail_enabled = False
                self.config.squelch = 0.05
                self.config.squelch_auto = False
            
            # Load terrain map from JSON field
            if self.config.terrain_map_json:
                try:
                    terrain_data = json.loads(self.config.terrain_map_json)
                    self._terrain_map = TerrainMap.from_dict(terrain_data)
                except Exception as e:
                    log.warning(f"Failed to load terrain map: {e}")
                    self._terrain_map = TerrainMap()
            
            # [channel.N] sections
            for section in cfg.sections():
                if section.startswith("channel."):
                    try:
                        ch_id = int(section.split(".")[1])
                        s     = cfg[section]
                        self.channels[ch_id] = ChannelDef(
                            id              = ch_id,
                            name            = s.get("name", f"CH{ch_id:02d}"),
                            frequency       = s.get("frequency", ""),
                            passphrase      = s.get("passphrase", ""),
                            description     = s.get("description", ""),
                            enabled         = s.getboolean("enabled", True),
                            modulation      = s.get("modulation", "FM"),
                            bandwidth_khz   = s.getfloat("bandwidth_khz", 25.0),
                            waveform_type   = s.get("waveform_type", "Narrowband"),
                            eccm_mode       = s.get("eccm_mode", "None"),
                            net_id          = s.get("net_id", "000"),
                            comsec_mode     = s.get("comsec_mode", "Plain"),
                            key_id          = s.get("key_id", "01"),
                            squelch_tone    = s.getfloat("squelch_tone", 0.0),
                        )
                    except (IndexError, ValueError):
                        pass
            
            # [callsigns] section — JSON blob for simplicity
            if cfg.has_section("callsigns"):
                try:
                    raw_list = json.loads(cfg["callsigns"].get("list", "[]"))
                    for entry in raw_list:
                        if "callsign" in entry:
                            entry["callsign"] = entry["callsign"].upper()
                        cs = CallsignDef(**entry)
                        legacy_map = {"handheld": "prc152", "manpack": "prc117g", "vehicle": "sincgars_veh"}
                        if cs.radio_profile in legacy_map:
                            cs.radio_profile = legacy_map[cs.radio_profile]
                        self.callsigns[cs.callsign] = cs
                except Exception as e:
                    log.warning(f"Failed to parse callsigns: {e}")
            
            # [profiles] section — JSON blob
            if cfg.has_section("profiles"):
                try:
                    raw_list = json.loads(cfg["profiles"].get("list", "[]"))
                    if raw_list:
                        import dataclasses
                        valid_keys = {f.name for f in dataclasses.fields(RadioProfile)}
                        for entry in raw_list:
                            # Migrate legacy 'waveforms' string to 'waveforms_nb'
                            if "waveforms" in entry and "waveforms_nb" not in entry:
                                entry["waveforms_nb"] = entry["waveforms"]
                                
                            # Migrate legacy rx_sensitivity_dbm
                            if "rx_sensitivity_dbm" in entry and "rx_sens_fm_dbm" not in entry:
                                s = entry["rx_sensitivity_dbm"]
                                entry["rx_sens_fm_dbm"] = s
                                entry["rx_sens_am_dbm"] = s + 12.5
                                entry["rx_sens_satcom_dbm"] = s - 4.0
                                
                            filtered = {k: v for k, v in entry.items() if k in valid_keys}
                            p = RadioProfile(**filtered)
                            self.profiles[p.id] = p
                except Exception as e:
                    log.warning(f"Failed to parse profiles: {e}")
            
            log.info(f"Config loaded from {self.path}")
            
            # Feature 5: Ensure CH99 VEHICLE INTERCOM is always present
            if 99 not in self.channels:
                self.channels[99] = ChannelDef(
                    99, "VEHICLE INTERCOM", "LOCAL WIRE", "",
                    "Vehicle hardwired local intercom — always full-strength",
                    modulation="FM", bandwidth_khz=8.0, waveform_type="Narrowband",
                    comsec_mode="Plain"
                )
            
        except Exception as e:
            log.error(f"Config load error: {e}")
    
    def save(self):
        """Persist current config to INI file."""
        try:
            cfg = configparser.ConfigParser()
            
            # Update terrain map JSON before saving
            self.config.terrain_map_json = json.dumps(self._terrain_map.to_dict())
            
            # [radio] section
            # Audio-effects fields are NOT persisted — server always starts in
            # CPX / clear-comms mode regardless of what was last set in the admin.
            _SKIP_FIELDS = {
                "connected",
                "distortion",  "noise_floor",  "crackle_lvl", "ptt_click_enabled",
                "bp_low_hz",  "bp_high_hz",  "sample_rate",  "opus_bitrate",
                "squelch_auto", "ptt_tone_f1", "ptt_tone_f2", "relay_signature",
            }
            cfg["radio"] = {}
            for f_name in self.config.__dataclass_fields__:
                if f_name in _SKIP_FIELDS:
                    continue
                cfg["radio"][f_name] = str(getattr(self.config, f_name))
            
            # [channel.N] sections
            for ch_id, ch in self.channels.items():
                section = f"channel.{ch_id}"
                cfg[section] = {
                    "name":             ch.name,
                    "frequency":        ch.frequency,
                    "passphrase":       ch.passphrase,
                    "description":      ch.description,
                    "enabled":          str(ch.enabled),
                    "modulation":       ch.modulation,
                    "bandwidth_khz":    str(ch.bandwidth_khz),
                    "waveform_type":    ch.waveform_type,
                    "eccm_mode":        ch.eccm_mode,
                    "net_id":           ch.net_id,
                    "comsec_mode":      ch.comsec_mode,
                    "key_id":           ch.key_id,
                    "squelch_tone":     str(ch.squelch_tone),
                }
            
            # [callsigns] section
            cfg["callsigns"] = {
                "list": json.dumps([
                    {k: v for k, v in asdict(cs).items()}
                    for cs in self.callsigns.values()
                ], indent=2)
            }
            
            # [profiles] section
            cfg["profiles"] = {
                "list": json.dumps([
                    {k: v for k, v in asdict(p).items()}
                    for p in self.profiles.values()
                ], indent=2)
            }
            
            self.path.parent.mkdir(parents=True, exist_ok=True)
            
            # Atomic save: write to temp file then rename
            import tempfile
            import os
            
            fd, temp_path = tempfile.mkstemp(dir=str(self.path.parent), prefix=self.path.name + ".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    cfg.write(f)
                # Atomic replace
                os.replace(temp_path, str(self.path))
                log.info(f"Config saved atomically to {self.path}")
            except Exception as e:
                if os.path.exists(temp_path):
                    os.remove(temp_path)
                raise e
            
        except Exception as e:
            log.error(f"Config save error: {e}")
    
    # ── Convenience accessors ──────────────────────────────────────────────────
    def get_channel(self, ch_id: int) -> Optional[ChannelDef]:
        return self.channels.get(ch_id)
    
    def get_current_channel(self) -> Optional[ChannelDef]:
        return self.get_channel(self.config.current_channel)
    
    def channel_list(self) -> List[ChannelDef]:
        return sorted([c for c in self.channels.values() if c.enabled], key=lambda c: c.id)
    
    def callsign_list(self) -> List[str]:
        return sorted(self.callsigns.keys())
    
    def set_position(self, lat: float, lon: float, alt: float = 0.0):
        """Update own position (from GPS, TAK, or SWORD)."""
        cs_key = self.config.callsign.upper()
        if cs_key not in self.callsigns:
            self.callsigns[cs_key] = CallsignDef(callsign=cs_key)
        
        cs = self.callsigns[cs_key]
        cs.lat = lat
        cs.lon = lon
        cs.alt = alt
    
    def get_position(self) -> tuple[float, float, float]:
        cs_key = self.config.callsign.upper()
        cs = self.callsigns.get(cs_key)
        if cs:
            return cs.lat, cs.lon, cs.alt
        return 0.0, 0.0, 0.0

# ── Self-test ─────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import tempfile
    logging.basicConfig(level=logging.INFO)
    print("=== Config self-test ===\n")
    
    # Test default config
    mgr = ConfigManager(Path("/tmp/test_radio_config.ini"))
    mgr.config.callsign    = "SUNRAY"
    mgr.config.server_host = "10.1.1.100"
    mgr.config.tak_enabled = True
    mgr.channels[1].passphrase = "EXERCISE-ALPHA-2025"
    
    # Test terrain map
    from dataclasses import asdict
    cell = TerrainCell(
        lat_min=-37.0, lat_max=-36.0,
        lon_min=174.0, lon_max=175.0,
        terrain_type="forest",
        elevation_m=150.0
    )
    mgr.terrain_map.cells.append(cell)
    
    # Save
    mgr.save()
    print(f"Saved config to /tmp/test_radio_config.ini")
    
    # Reload
    mgr2 = ConfigManager(Path("/tmp/test_radio_config.ini"))
    assert mgr2.config.callsign    == "SUNRAY"
    assert mgr2.config.server_host == "10.1.1.100"
    assert mgr2.config.tak_enabled == True
    assert mgr2.channels[1].passphrase == "EXERCISE-ALPHA-2025"
    assert len(mgr2.terrain_map.cells) == 1
    print(f"Reload: OK")
    
    # Channel list
    chs = mgr2.channel_list()
    print(f"\nChannels ({len(chs)}):")
    for ch in chs:
        print(f"  {ch.display}")
    
    # Callsigns
    print(f"\nCallsigns ({len(mgr2.callsigns)}):")
    for cs_name in list(mgr2.callsign_list())[:6]:
        cs = mgr2.callsigns[cs_name]
        print(f"  {cs.callsign:<20}  {cs.unit}  ({cs.role})")
    print(f"  ... and {len(mgr2.callsigns)-6} more")
    
    # Terrain
    print(f"\nTerrain cells: {len(mgr2.terrain_map.cells)}")
    for cell in mgr2.terrain_map.cells:
        print(f"  {cell.terrain_type} @ {cell.elevation_m}m  "
              f"({cell.lat_min:.2f}–{cell.lat_max:.2f}, {cell.lon_min:.2f}–{cell.lon_max:.2f})")
    
    print("\n✓ Config tests passed")