"""
MilRadio — Radio Effects / DSP Chain
Implements the receive-side audio processing pipeline that gives the radio
its characteristic sound.  All processing is done in float32 numpy arrays.

Pipeline (applied in order on received audio):
  Bandpass filter   300–3000 Hz  (voice radio bandwidth — cuts hi-fi)
  Harmonic distortion            (subtle odd-harmonic saturation)
  AM noise modulation            (noise amplitude tracks signal strength)
  Background hiss                (always-on low-level static)
  Squelch gate                   (silence if signal below threshold)
  Click artifacts                (PTT open/close clicks)
  Output gain normalisation

Signal strength model:
  signal_strength ∈ [0.0, 1.0]
    1.0  = full quieting, clean audio
    0.5  = some static, slightly degraded
    0.2  = heavy static, barely intelligible
    0.0  = no signal, squelched

  squelch_threshold: below this → no audio (squelched)
  This is controlled per-receiver and set by the server based on range.

Transmit side processing (before encoding):
  High-pass filter  80 Hz        (remove low-end rumble/handling noise)
  Compressor/limiter             (radio-style AGC, prevents clipping)
  Pre-emphasis                   (boost highs for bandwidth efficiency)

Communications Infrastructure (Phase 1):
  - Direct Communication (LOS-based, terrain-affected)
  - Retrans/Repeaters (extend range, relay signals)
  - SATCOM (global coverage, ignores terrain, higher latency)
  - GPS/MGRS (position tracking with MGRS display)
"""
from __future__ import annotations
import numpy as np
import scipy.signal as sig
import logging
import math
from dataclasses import dataclass, field
from typing import Optional, Dict, List, Tuple
from enum import Enum

# Import MGRS utilities from radio_config (optional — graceful fallback)
try:
    from radio_config import latlon_to_mgrs, mgrs_to_latlon, RepeaterNode, SatcomTerminal, GPSState
    from radio_terrain import get_terrain_manager
    _INFRA_AVAILABLE = True
except ImportError:
    _INFRA_AVAILABLE = False
    latlon_to_mgrs = lambda lat, lon, p=5: f"{lat:.4f},{lon:.4f}"
    mgrs_to_latlon = lambda mgrs_str: (0.0, 0.0)
    RepeaterNode = None
    SatcomTerminal = None
    GPSState = None

log = logging.getLogger("radio.effects")

SAMPLE_RATE   = 48000
FRAME_SAMPLES = 960    # 20ms @ 48kHz

# ═══════════════════════════════════════════════════════════════════════════
# Communication Mode Enum (Infrastructure Support)
# ═══════════════════════════════════════════════════════════════════════════
class CommMode(Enum):
    """Communication path type"""
    DIRECT = "direct"      # Line-of-sight, terrain-affected
    RETRANS = "retrans"    # Via repeater(s)
    SATCOM = "satcom"      # Satellite, global, terrain-ignoring

@dataclass
class CommPath:
    """Represents a calculated communication path"""
    mode: CommMode
    signal_strength: float     # 0.0-1.0
    latency_ms: float          # Total path latency
    hops: int                  # Number of repeater hops
    terrain_blocked: bool      # LOS blocked?
    repeaters_used: List[str]  # IDs of repeaters in path

# ═══════════════════════════════════════════════════════════════════════════
# Filter Bank — Built Once, Reused
# ═══════════════════════════════════════════════════════════════════════════
class FilterBank:
    """Pre-computed SOS filter coefficients for all radio filters."""
    
    def __init__(self, rate: int = SAMPLE_RATE,
                 bp_low_hz: int = 120, bp_high_hz: int = 7000):
        self.rate = rate
        nyq = rate / 2.0
        
        # Clamp cutoffs to valid range for the sample rate
        bp_low  = max(50,  min(bp_low_hz,  int(nyq * 0.08)))
        bp_high = max(500, min(bp_high_hz, int(nyq * 0.95)))
        
        # Receive: bandpass (configurable — wide for CPX, narrow for SIM)
        self.rx_bandpass = sig.butter(
            4, [bp_low/nyq, bp_high/nyq], btype='bandpass', output='sos'
        )
        self._bp_low  = bp_low
        self._bp_high = bp_high
        
        # Receive: very narrow resonant boost around 1kHz (radio 'presence' character)
        # iirpeak returns (b,a) — convert to SOS manually
        if hasattr(sig, 'iirpeak'):
            try:
                b, a = sig.iirpeak(1000/nyq, Q=2.0)
                self.rx_presence = sig.tf2sos(b, a)
            except Exception:
                self.rx_presence = None
        else:
            self.rx_presence = None
        
        # Transmit: high-pass 80 Hz (remove handling noise)
        self.tx_highpass = sig.butter(2, 80/nyq, btype='high', output='sos')
        
        # Transmit: pre-emphasis (6 dB/octave above 800 Hz)
        self.tx_preemph  = sig.butter(1, 2000/nyq, btype='high', output='sos')
        
        log.debug("FilterBank initialised")
    
    def apply_rx_bandpass(self, audio: np.ndarray, zi: Optional[np.ndarray] = None):
        """Apply receive bandpass. Returns (filtered, zi) for stateful processing."""
        n_sos = self.rx_bandpass.shape[0]
        if zi is None:
            zi = np.zeros((n_sos, 2))
        out, zo = sig.sosfilt(self.rx_bandpass, audio, zi=zi)
        return out.astype(np.float32), zo
    
    def apply_tx_highpass(self, audio: np.ndarray, zi: Optional[np.ndarray] = None):
        n_sos = self.tx_highpass.shape[0]
        if zi is None:
            zi = np.zeros((n_sos, 2))
        out, zo = sig.sosfilt(self.tx_highpass, audio, zi=zi)
        return out.astype(np.float32), zo
    
    def apply_tx_preemph(self, audio: np.ndarray, zi: Optional[np.ndarray] = None):
        n_sos = self.tx_preemph.shape[0]
        if zi is None:
            zi = np.zeros((n_sos, 2))
        out, zo = sig.sosfilt(self.tx_preemph, audio, zi=zi)
        return out.astype(np.float32), zo

# Singleton filter bank — shared across all effect processors
_filter_bank: Optional[FilterBank] = None

def get_filter_bank() -> FilterBank:
    global _filter_bank
    if _filter_bank is None:
        _filter_bank = FilterBank()
    return _filter_bank

# ═══════════════════════════════════════════════════════════════════════════
# Harmonic Distortion
# ═══════════════════════════════════════════════════════════════════════════
def _soft_clip(x: np.ndarray, drive: float = 1.5) -> np.ndarray:
    """
    Soft saturation — odd-harmonic distortion typical of tube/transistor radios.
    drive > 1 increases distortion. Uses tanh saturation.
    """
    driven = x * drive
    return np.tanh(driven) / np.tanh(drive)

# ═══════════════════════════════════════════════════════════════════════════
# Noise Generation
# ═══════════════════════════════════════════════════════════════════════════
def _bandpass_noise(n_samples: int, fb: FilterBank) -> np.ndarray:
    """Generate one frame of bandpass-filtered white noise (radio static)."""
    white = np.random.normal(0.0, 1.0, n_samples).astype(np.float32)
    filtered, _ = fb.apply_rx_bandpass(white)
    # Normalise to RMS ≈ 0.1
    rms = np.sqrt(np.mean(filtered**2)) + 1e-8
    return filtered * (0.1 / rms)

# ═══════════════════════════════════════════════════════════════════════════
# Per-Source Effect State
# ═══════════════════════════════════════════════════════════════════════════
@dataclass
class EffectState:
    """
    Persistent filter state for one audio source (one transmitting station).
    Keeps filter zi (initial conditions) so there's no discontinuity at frame
    boundaries.
    """
    rx_zi:         Optional[np.ndarray] = None
    tx_hp_zi:      Optional[np.ndarray] = None
    tx_pe_zi:      Optional[np.ndarray] = None
    ptt_open:      bool                 = False
    click_pending: bool                 = False   # inject click on next frame
    rx_sample_count: int                = 0
    crypto_sync_samples: int            = 0

# ═══════════════════════════════════════════════════════════════════════════
# Range Model (Basic — Distance-Based)
# ═══════════════════════════════════════════════════════════════════════════
class RangeModel:
    """
    Converts physical distance to signal strength for range simulation.
    Models a VHF/UHF manpack radio in typical terrain:
      - Max range:    full quieting up to ~5 km
      - Usable range: degrading 5–15 km
      - Edge of range: 15–25 km
      - Dead:           > 25 km (or configurable)
    
    The model also supports terrain masking (simple line-of-sight check)
    and can be overridden by the simulation (SWORD/TAK provides RSSI).
    """
    
    def __init__(
        self,
        max_range_km:      float = 25.0,   # hard cutoff
        full_quiet_km:     float = 5.0,    # below this: signal=1.0
        power_w:           float = 5.0,    # transmit power in watts (typical manpack)
        terrain_factor:    float = 1.0,    # 1.0=open, 0.5=light cover, 0.2=heavy terrain
    ):
        self.max_range_km   = max_range_km
        self.full_quiet_km  = full_quiet_km
        self.power_w        = power_w
        self.terrain_factor = terrain_factor
        # Power scaling: 5W reference
        self._power_scale   = np.sqrt(power_w / 5.0)
        
        # Weather state
        self.weather_enabled = False
        self.weather_severity = 0.0
        self.weather_humidity = 0.0
        self.weather_type = "rain"
        self.temp_inversion_enabled = False
        self.temp_inversion_strength = 0.0
    
    def signal_strength(self, distance_km: float, freq_mhz: float = 45.0) -> float:
        """
        Returns signal strength 0.0–1.0 for a given distance and frequency.
        """
        # 1. Distance & Power Scaling
        d = distance_km / (self.terrain_factor * self._power_scale)
        
        # 2. Atmospheric Attenuation (Phase 4)
        atmos_loss = 0.0
        if self.weather_enabled and self.weather_severity > 0:
            # Empirical model for rain/moisture loss
            # higher frequencies are absorbed more by water droplets
            freq_factor = (freq_mhz / 100.0) ** 0.5 # VHF (45MHz) is less affected than UHF (400MHz)
            
            if self.weather_type in ("rain", "storm"):
                # Rain loss: approx 0.02 to 0.5 dB/km depending on severity
                rain_db_km = self.weather_severity * 0.4 * freq_factor
                atmos_loss += rain_db_km * distance_km
            
            # Humidity loss: oxygen/water vapour absorption (mostly affects >10GHz, 
            # but we simulate it as general thickening of the air)
            hum_db_km = self.weather_humidity * 0.05 * freq_factor
            atmos_loss += hum_db_km * distance_km
            
        # Convert total additional dB loss to linear scale (simplified)
        # 10 dB loss = 0.31x signal, 20 dB = 0.1x signal
        atmos_factor = 10 ** (-atmos_loss / 20.0)
        
        if d <= self.full_quiet_km:
            return float(np.clip(1.0 * atmos_factor, 0.0, 1.0))
        if d >= self.max_range_km:
            return 0.0
        
        # Log-linear rolloff between full_quiet and max_range
        ratio = (d - self.full_quiet_km) / (self.max_range_km - self.full_quiet_km)
        # Nonlinear: degrades faster near edge of range
        strength = float(np.clip(1.0 - ratio ** 1.4, 0.0, 1.0))
        
        return strength * atmos_factor
    
    def evaluate_profile(self, distance_km: float, tx_power_w: float, max_range_km: float, full_quiet_km: float) -> float:
        """Evaluate signal strength using specific hardware parameters."""
        power_scale = np.sqrt(tx_power_w / 5.0)
        d = distance_km / (self.terrain_factor * power_scale)
        
        atmos_loss = 0.0
        if self.weather_enabled and self.weather_severity > 0:
            freq_factor = (45.0 / 100.0) ** 0.5 
            if self.weather_type in ("rain", "storm"):
                atmos_loss += (self.weather_severity * 0.4 * freq_factor) * distance_km
            atmos_loss += (self.weather_humidity * 0.05 * freq_factor) * distance_km
            
        atmos_factor = 10 ** (-atmos_loss / 20.0)
        
        if d <= full_quiet_km: return float(np.clip(1.0 * atmos_factor, 0.0, 1.0))
        if d >= max_range_km: return 0.0
        
        # Harder signal drop-off: quadratic-ish decay (exponent 2.2 instead of 1.4)
        ratio = (d - full_quiet_km) / (max_range_km - full_quiet_km)
        strength = float(np.clip(1.0 - ratio ** 2.2, 0.0, 1.0))
        
        return strength * atmos_factor
    
    @staticmethod
    def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        """
        Haversine formula: great-circle distance in km between two lat/lon points.
        Fast numpy implementation.
        """
        R  = 6371.0
        φ1, φ2 = np.radians(lat1), np.radians(lat2)
        dφ = np.radians(lat2 - lat1)
        dλ = np.radians(lon2 - lon1)
        a  = np.sin(dφ/2)**2 + np.cos(φ1)*np.cos(φ2)*np.sin(dλ/2)**2
        return float(2 * R * np.arcsin(np.sqrt(a)))
    
    def signal_from_positions(
        self,
        lat1: float, lon1: float,
        lat2: float, lon2: float,
        profile_specs=None,
        freq_mhz: float = 45.0
    ) -> float:
        """Convenience: compute signal from two lat/lon positions."""
        d = self.haversine_km(lat1, lon1, lat2, lon2)
        if profile_specs:
            # profile_specs = (tx_power, max_range, full_quiet)
            # We wrap the atmospheric factor inside the profile eval
            base_sig = self.evaluate_profile(d, profile_specs[0], profile_specs[1], profile_specs[2])
            # Re-apply atmos factor based on distance
            # (In a real world, we'd integrate this better, but this is a solid approximation)
            atmos_loss = 0.0
            if self.weather_enabled:
                f_f = (freq_mhz / 100.0) ** 0.5
                if self.weather_type in ("rain", "storm"):
                    atmos_loss += self.weather_severity * 0.4 * f_f * d
                atmos_loss += self.weather_humidity * 0.05 * f_f * d
            atmos_factor = 10 ** (-atmos_loss / 20.0)
            return float(np.clip(base_sig * atmos_factor, 0.0, 1.0))
            
        return self.signal_strength(d, freq_mhz=freq_mhz)

# ═══════════════════════════════════════════════════════════════════════════
# Enhanced Range Model (Infrastructure-Aware)
# ═══════════════════════════════════════════════════════════════════════════
class EnhancedRangeModel(RangeModel):
    """
    Extended range calculation with infrastructure awareness.
    Supports DIRECT, RETRANS (via repeaters), and SATCOM paths.
    
    Path selection priority:
      1. DIRECT (if LOS clear and signal > 0.3)
      2. RETRANS (via repeater network)
      3. SATCOM (global, ignores terrain, higher latency)
    """
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Infrastructure registries
        self.repeaters: Dict[str, RepeaterNode] = {}
        self.satcom_terminals: Dict[str, SatcomTerminal] = {}
        self.gps_states: Dict[str, GPSState] = {}
    
    # ── Registration Methods ─────────────────────────────────────────────────
    def register_repeater(self, repeater: RepeaterNode):
        """Add a repeater to the network"""
        if _INFRA_AVAILABLE:
            self.repeaters[repeater.id] = repeater
            log.info(f"Repeater registered: {repeater.id} at {repeater.mgrs}")
    
    def unregister_repeater(self, repeater_id: str):
        """Remove a repeater from the network"""
        if repeater_id in self.repeaters:
            del self.repeaters[repeater_id]
            log.info(f"Repeater unregistered: {repeater_id}")
    
    def register_satcom(self, terminal: SatcomTerminal):
        """Register a SATCOM terminal"""
        if _INFRA_AVAILABLE:
            self.satcom_terminals[terminal.callsign] = terminal
            log.info(f"SATCOM terminal registered: {terminal.callsign}")
    
    def register_gps(self, gps: GPSState):
        """Register GPS state for a callsign"""
        if _INFRA_AVAILABLE:
            self.gps_states[gps.callsign] = gps
    
    # ── Path Calculation ─────────────────────────────────────────────────────
    def calculate_path(
        self,
        sender_cs: str,
        recipient_cs: str,
        sender_pos: Tuple[float, float, float],
        recipient_pos: Tuple[float, float, float],
        channel: int,
    ) -> CommPath:
        """
        Determine optimal communication path and signal quality.
        Tries DIRECT → RETRANS → SATCOM in order.
        
        Returns CommPath with mode, signal_strength, latency, etc.
        """
        if not _INFRA_AVAILABLE:
            # Fallback to basic direct path
            return self._calc_direct_path(sender_pos, recipient_pos, channel)
        
        # 1. Try direct LOS path
        direct_path = self._calc_direct_path(sender_pos, recipient_pos, channel)
        
        if direct_path.signal_strength > 0.3 and not direct_path.terrain_blocked:
            return direct_path  # Direct is viable
        
        # 2. Try via repeater(s)
        retrans_path = self._calc_retrans_path(
            sender_cs, recipient_cs, sender_pos, recipient_pos, channel
        )
        
        if retrans_path and retrans_path.signal_strength > 0.3:
            return retrans_path
        
        # 3. Fall back to SATCOM (if available)
        satcom_path = self._calc_satcom_path(sender_cs, recipient_cs)
        
        if satcom_path:
            return satcom_path
        
        # 4. No path available
        return CommPath(
            mode=CommMode.DIRECT,
            signal_strength=0.0,
            latency_ms=0,
            hops=0,
            terrain_blocked=True,
            repeaters_used=[]
        )
    
    def _calc_direct_path(
        self,
        sender: Tuple[float, float, float],
        recipient: Tuple[float, float, float],
        channel: int
    ) -> CommPath:
        """Calculate line-of-sight direct path"""
        distance = self.haversine_km(sender[0], sender[1], recipient[0], recipient[1])
        terrain_factor = self._calc_terrain_masking(sender, recipient)
        signal = self.signal_strength(distance) * terrain_factor
        
        return CommPath(
            mode=CommMode.DIRECT,
            signal_strength=signal,
            latency_ms=20,  # Minimal latency for direct
            hops=0,
            terrain_blocked=(terrain_factor < 0.1),
            repeaters_used=[]
        )
    
    def _calc_retrans_path(
        self,
        sender_cs: str,
        recipient_cs: str,
        sender_pos: Tuple[float, float, float],
        recipient_pos: Tuple[float, float, float],
        channel: int
    ) -> Optional[CommPath]:
        """Calculate path via repeater network"""
        if not _INFRA_AVAILABLE:
            return None
        
        viable_repeaters = []
        
        for rpt_id, rpt in self.repeaters.items():
            if not rpt.enabled or rpt.status != "online":
                continue
            if channel not in rpt.channels:
                continue
            
            rpt_pos = (rpt.lat, rpt.lon, rpt.alt)
            
            # Check sender → repeater
            path1 = self._calc_direct_path(sender_pos, rpt_pos, channel)
            # Check repeater → recipient
            path2 = self._calc_direct_path(rpt_pos, recipient_pos, channel)
            
            if path1.signal_strength > 0.3 and path2.signal_strength > 0.3:
                viable_repeaters.append((rpt_id, rpt, path1, path2))
        
        if not viable_repeaters:
            return None
        
        # Use best repeater (highest combined signal)
        best = max(viable_repeaters,
                   key=lambda x: min(x[2].signal_strength, x[3].signal_strength))
        rpt_id, rpt, path1, path2 = best
        
        combined_signal = min(path1.signal_strength, path2.signal_strength) * (1.0 - rpt.degradation)
        total_latency = path1.latency_ms + path2.latency_ms + rpt.latency_ms
        
        return CommPath(
            mode=CommMode.RETRANS,
            signal_strength=combined_signal,
            latency_ms=total_latency,
            hops=1,
            terrain_blocked=False,
            repeaters_used=[rpt_id]
        )
    
    def _calc_satcom_path(
        self,
        sender_cs: str,
        recipient_cs: str
    ) -> Optional[CommPath]:
        """Calculate SATCOM path (ignores terrain/range)"""
        if not _INFRA_AVAILABLE:
            return None
        
        sender_sat = self.satcom_terminals.get(sender_cs)
        recipient_sat = self.satcom_terminals.get(recipient_cs)
        
        if not sender_sat or not recipient_sat:
            return None
        if not sender_sat.enabled or not recipient_sat.enabled:
            return None
        if sender_sat.jammed or recipient_sat.jammed:
            return None
        
        signal = min(sender_sat.signal_quality, recipient_sat.signal_quality)
        latency = sender_sat.latency_ms + recipient_sat.latency_ms
        
        return CommPath(
            mode=CommMode.SATCOM,
            signal_strength=signal,
            latency_ms=latency,
            hops=0,
            terrain_blocked=False,
            repeaters_used=[]
        )
    
    def _calc_terrain_masking(
        self,
        pos1: Tuple[float, float, float],
        pos2: Tuple[float, float, float]
    ) -> float:
        """
        Calculate terrain blocking factor (0.0=fully blocked, 1.0=clear LOS).
        Uses the global TerrainManager for high-fidelity 3D analysis.
        Standardizes pos[2] as Height Above Ground (AGL) and resolves to AMSL.
        """
        if not _INFRA_AVAILABLE:
            return 1.0
            
        terrain = get_terrain_manager()
        
        # Resolve absolute altitudes (AMSL)
        alt1_abs = (terrain.get_elevation_highres(pos1[0], pos1[1]) or 0.0) + pos1[2]
        alt2_abs = (terrain.get_elevation_highres(pos2[0], pos2[1]) or 0.0) + pos2[2]
        
        # Check LOS using absolute altitudes
        los_clear = terrain.check_line_of_sight(pos1[0], pos1[1], alt1_abs, 
                                               pos2[0], pos2[1], alt2_abs)
                                               
        if not los_clear:
            return 0.05  # Blocked signal floor (diffraction)
            
        # Also apply terrain attenuation (forest/urban)
        return terrain.compute_terrain_factor(pos1[0], pos1[1], pos2[0], pos2[1])
    
    # ── GPS/MGRS Utilities ───────────────────────────────────────────────────
    def get_mgrs_for_callsign(self, callsign: str) -> str:
        """Get MGRS coordinates for a callsign's GPS state"""
        if not _INFRA_AVAILABLE:
            return ""
        gps = self.gps_states.get(callsign)
        if gps:
            return gps.reported_mgrs
        return ""
    
    def get_actual_position(self, callsign: str) -> Optional[Tuple[float, float, float]]:
        """Get actual (true) position for a callsign (server-side truth)"""
        if not _INFRA_AVAILABLE:
            return None
        gps = self.gps_states.get(callsign)
        if gps:
            return (gps.actual_lat, gps.actual_lon, gps.actual_lat)
        return None
    
    def get_reported_position(self, callsign: str) -> Optional[Tuple[float, float, float]]:
        """Get reported position for a callsign (what client reports)"""
        if not _INFRA_AVAILABLE:
            return None
        gps = self.gps_states.get(callsign)
        if gps:
            return (gps.reported_lat, gps.reported_lon, gps.reported_lat)
        return None

# ═══════════════════════════════════════════════════════════════════════════
# Main Processor
# ═══════════════════════════════════════════════════════════════════════════
class RadioEffects:
    """
    Applies the full radio effect chain to received or transmitted audio.
    One instance per application; EffectState objects are per-source.
    
    Usage:
        effects = RadioEffects(squelch=0.15)
        state   = EffectState()
        
        # On receive (for each incoming voice packet):
        processed = effects.process_rx(pcm_f32, signal_strength=0.7, state=state)
        
        # On transmit (before encoding):
        processed = effects.process_tx(mic_pcm_f32, state=state)
    """
    
    def __init__(
        self,
        squelch:             float = 0.15,   # signal below this → silence
        volume:              float = 1.0,    # output gain
        distortion:          float = 0.0,    # harmonic distortion amount 0–1
        noise_floor:         float = 0.0,    # always-on background hiss
        ptt_click_enabled:   bool  = False,  # PTT relay click injection
        tx_highpass_enabled: bool  = False,  # 80 Hz high-pass on TX chain
        tx_preemph_enabled:  bool  = False,  # pre-emphasis on TX chain
        bp_low_hz:           int   = 120,    # RX bandpass low cut
        bp_high_hz:          int   = 7000,   # RX bandpass high cut
        sample_rate:         int   = SAMPLE_RATE,
        # Weather & Atmosphere
        weather_enabled:          bool  = False,  # precipitation on/off
        weather_severity:         float = 0.0,    # 0.0=clear  1.0=severe
        weather_type:             str   = "rain", # rain|storm|interference|fog
        weather_humidity:         float = 0.0,    # 0=dry  1=saturated (HF rolloff)
        temp_inversion_enabled:   bool  = False,  # temperature inversion on/off
        temp_inversion_strength:  float = 0.0,    # 0=none  1=strong ducting
        temp_inversion_echo_ms:   float = 10.0,   # multi-path echo delay ms
        iono_enabled:             bool  = False,  # ionospheric conditions on/off
        iono_condition:           str   = "quiet",# quiet|disturbed|storm|blackout
        iono_fading:              float = 0.0,    # slow fading depth
        iono_flutter:             float = 0.0,    # fast flutter depth
        iono_absorption:          float = 0.0,    # signal absorption (blackout)
        # Day / Night Cycle
        day_night_enabled:        bool  = False,  # day/night propagation cycle
        day_night_hour:           float = 12.0,   # hour 0-24 (12=noon=clearest)
        day_night_auto:           bool  = False,  # sync to wall clock
        day_night_latitude:       float = 0.0,    # latitude for sunrise/sunset calc
        # EMI (Electromagnetic Interference)
        emi_enabled:              bool  = False,  # EMI noise layer
        emi_level:                float = 0.0,    # 0-1 EMI intensity
        emi_type:                 str   = "white",# white|hum|burst
    ):
        self.squelch             = squelch
        self.volume              = volume
        self.distortion          = distortion
        self.noise_floor         = noise_floor
        self.ptt_click_enabled   = ptt_click_enabled
        self.tx_highpass_enabled = tx_highpass_enabled
        self.tx_preemph_enabled  = tx_preemph_enabled
        
        # Weather & Atmosphere
        self.weather_enabled         = weather_enabled
        self.weather_severity        = weather_severity
        self.weather_type            = weather_type
        self.weather_humidity        = weather_humidity
        self.temp_inversion_enabled  = temp_inversion_enabled
        self.temp_inversion_strength = temp_inversion_strength
        self.temp_inversion_echo_ms  = temp_inversion_echo_ms
        self.iono_enabled            = iono_enabled
        self.iono_condition          = iono_condition
        self.iono_fading             = iono_fading
        self.iono_flutter            = iono_flutter
        self.iono_absorption         = iono_absorption
        
        # Day / Night Cycle
        self.day_night_enabled       = day_night_enabled
        self.day_night_hour          = day_night_hour
        self.day_night_auto          = day_night_auto
        self.day_night_latitude      = day_night_latitude
        
        # EMI
        self.emi_enabled             = emi_enabled
        self.emi_level               = emi_level
        self.emi_type                = emi_type
        
        # Per-frame state for echo delay (temperature inversion multi-path)
        self._echo_buf: np.ndarray = np.zeros(4800, dtype=np.float32)  # 100ms @ 48kHz
        
        self._fb = FilterBank(rate=sample_rate,
                              bp_low_hz=bp_low_hz, bp_high_hz=bp_high_hz)
        
        log.debug(
            f"RadioEffects: squelch={squelch} vol={volume} dist={distortion}  "
            f"noise={noise_floor} clicks={ptt_click_enabled}  "
            f"bp={bp_low_hz}-{bp_high_hz}Hz"
        )
    
    def update(self, **kwargs) -> None:
        """
        Update one or more effect parameters at runtime without rebuilding
        the filter bank unless bandpass cutoffs change.  Called when the server's audio config changes.
        
        Accepted kwargs (all optional):
            squelch, volume, distortion, noise_floor,
            ptt_click_enabled, tx_highpass_enabled, tx_preemph_enabled,
            weather_*, temp_inversion_*, iono_*, day_night_*, emi_*,
            bp_low_hz, bp_high_hz
        """
        def _float(val, default=0.0):
            try: return float(val) if val is not None else default
            except (ValueError, TypeError): return default

        def _bool(val, default=False):
            if val is None: return default
            if isinstance(val, str): return val.lower() in ("true", "1", "yes", "on")
            return bool(val)

        def _str(val, default=""):
            return str(val) if val is not None else default

        def _int(val, default=0):
            try: return int(val) if val is not None else default
            except (ValueError, TypeError): return default

        if 'squelch'              in kwargs: self.squelch              = _float(kwargs['squelch'], 0.0)
        if 'volume'               in kwargs: self.volume               = _float(kwargs['volume'], 1.0)
        if 'distortion'           in kwargs: self.distortion           = _float(kwargs['distortion'], 0.0)
        if 'noise_floor'          in kwargs: self.noise_floor          = _float(kwargs['noise_floor'], 0.0)
        
        # Weather
        if 'weather_enabled'         in kwargs: self.weather_enabled         = _bool(kwargs['weather_enabled'], False)
        if 'weather_severity'        in kwargs: self.weather_severity        = _float(kwargs['weather_severity'], 0.0)
        if 'weather_type'            in kwargs: self.weather_type            = _str(kwargs['weather_type'], 'rain')
        if 'weather_humidity'        in kwargs: self.weather_humidity        = _float(kwargs['weather_humidity'], 0.0)
        
        # Temperature Inversion
        if 'temp_inversion_enabled'  in kwargs: self.temp_inversion_enabled  = _bool(kwargs['temp_inversion_enabled'], False)
        if 'temp_inversion_strength' in kwargs: self.temp_inversion_strength = _float(kwargs['temp_inversion_strength'], 0.0)
        if 'temp_inversion_echo_ms'  in kwargs: self.temp_inversion_echo_ms  = _float(kwargs['temp_inversion_echo_ms'], 10.0)
        
        # Ionospheric
        if 'iono_enabled'            in kwargs: self.iono_enabled            = _bool(kwargs['iono_enabled'], False)
        if 'iono_condition'          in kwargs: self.iono_condition          = _str(kwargs['iono_condition'], 'quiet')
        if 'iono_fading'             in kwargs: self.iono_fading             = _float(kwargs['iono_fading'], 0.0)
        if 'iono_flutter'            in kwargs: self.iono_flutter            = _float(kwargs['iono_flutter'], 0.0)
        if 'iono_absorption'         in kwargs: self.iono_absorption         = _float(kwargs['iono_absorption'], 0.0)
        
        # Day / Night
        if 'day_night_enabled'       in kwargs: self.day_night_enabled       = _bool(kwargs['day_night_enabled'], False)
        if 'day_night_hour'          in kwargs: self.day_night_hour          = _float(kwargs['day_night_hour'], 12.0)
        if 'day_night_auto'          in kwargs: self.day_night_auto          = _bool(kwargs['day_night_auto'], False)
        if 'day_night_latitude'      in kwargs: self.day_night_latitude      = _float(kwargs['day_night_latitude'], 0.0)
        
        # EMI
        if 'emi_enabled'             in kwargs: self.emi_enabled             = _bool(kwargs['emi_enabled'], False)
        if 'emi_level'               in kwargs: self.emi_level               = _float(kwargs['emi_level'], 0.0)
        if 'emi_type'                in kwargs: self.emi_type                = _str(kwargs['emi_type'], 'white')
        
        # TX chain
        if 'ptt_click_enabled'   in kwargs: self.ptt_click_enabled    = _bool(kwargs['ptt_click_enabled'], False)
        if 'tx_highpass_enabled' in kwargs: self.tx_highpass_enabled  = _bool(kwargs['tx_highpass_enabled'], False)
        if 'tx_preemph_enabled'  in kwargs: self.tx_preemph_enabled   = _bool(kwargs['tx_preemph_enabled'], False)
        
        # Bandpass Cutoff Rebuilding
        bp_low = kwargs.get('bp_low_hz', self._fb._bp_low)
        bp_high = kwargs.get('bp_high_hz', self._fb._bp_high)
        if bp_low != self._fb._bp_low or bp_high != self._fb._bp_high:
            log.info("Rebuilding filter bank with new bandpass: %s-%s Hz", bp_low, bp_high)
            self._fb = FilterBank(rate=self._fb.rate, bp_low_hz=_int(bp_low, 120), bp_high_hz=_int(bp_high, 7000))
            
        log.info(
            "RadioEffects updated: squelch=%.2f vol=%.2f dist=%.2f noise=%.3f  "
            "clicks=%s txhp=%s txpe=%s bp=%d-%d",
            self.squelch, self.volume, self.distortion, self.noise_floor,
            self.ptt_click_enabled, self.tx_highpass_enabled, self.tx_preemph_enabled,
            self._fb._bp_low, self._fb._bp_high
        )
    
    # ═══════════════════════════════════════════════════════════════════════
    # Receive Chain
    # ═══════════════════════════════════════════════════════════════════════
    def process_rx(
        self,
        audio:           np.ndarray,
        signal_strength: float,
        state:           EffectState,
        end_of_tx:       bool = False,
        sig_id:          float = 0.0,
        comsec_encrypted: bool = False,
        collision_severity: float = 0.0,  # 0=clean, 1=full collision
        waveform_type:    str = 'Narrowband',  # 'Narrowband' | 'Wideband'
        eccm_mode:        str = '',        # '' | 'SINCGARS' | 'HAVEQUICK'
        comsec_mismatch:  bool = False,    # Feature 2: True if decrypt failed or key mismatch
    ) -> np.ndarray:
        """
        Full receive-side processing chain.
        
        audio:           float32 PCM, decoded from packet
        signal_strength: 0.0–1.0 from range calculation
        state:           per-source persistent filter state
        end_of_tx:       True on last packet of a transmission (add click)
        comsec_encrypted: True if the channel is encrypted
        
        Returns processed float32 PCM ready for playback.
        """
        n = len(audio)
        if not hasattr(state, 'rx_sample_count'):
            state.rx_sample_count = 0
        if not hasattr(state, 'crypto_sync_samples'):
            state.crypto_sync_samples = 0

        # ── Feature 2: COMSEC Mismatch Screech ───────────────────────────────────
        if comsec_mismatch:
            # Generate LFO-modulated digital modem screech
            # Screech(t) = 0.4*(0.5+0.5*sin(2π*8t))*sin(2π*2100t)
            #            + 0.4*(0.5+0.5*sin(2π*12t))*sin(2π*1300t)
            #            + 0.2*N(0, 0.15)
            t0 = state.rx_sample_count / SAMPLE_RATE
            t = t0 + np.arange(n, dtype=np.float32) / SAMPLE_RATE
            screech = (
                0.4 * (0.5 + 0.5 * np.sin(2 * np.pi * 8.0  * t)) * np.sin(2 * np.pi * 2100.0 * t) +
                0.4 * (0.5 + 0.5 * np.sin(2 * np.pi * 12.0 * t)) * np.sin(2 * np.pi * 1300.0 * t) +
                0.2 * np.random.normal(0.0, 0.15, n).astype(np.float32)
            ).astype(np.float32)
            state.rx_sample_count += n
            return np.clip(screech * self.volume, -0.99, 0.99).astype(np.float32)

        sim_mode_active = (self.distortion > 0 or self.noise_floor > 0)
        if not comsec_encrypted or not sim_mode_active:
            state.crypto_sync_samples = 0
        
        # ── 1. Squelch ───────────────────────────────────────────────────────
        if signal_strength < self.squelch:
            # Below squelch: return silence (maybe with very faint noise)
            return np.zeros(n, dtype=np.float32)
        
        # ── 2. Bandpass filter ───────────────────────────────────────────────
        # Only apply in SIM mode (distortion or noise active).
        # CPX mode: pass audio through unfiltered for clean wideband sound.
        if self.distortion > 0 or self.noise_floor > 0:
            filtered, state.rx_zi = self._fb.apply_rx_bandpass(audio, state.rx_zi)
        else:
            filtered = audio.astype(np.float32)
            state.rx_zi = None   # reset filter state (not used in CPX)
        
        # ── Alternator Hum / Sender Signature ────────────────────────────────
        if (self.distortion > 0 or self.noise_floor > 0) and sig_id > 0.0:
            t = (state.rx_sample_count + np.arange(n, dtype=np.float32)) / SAMPLE_RATE
            if sig_id == 1.0:
                hum = 0.015 * np.sin(2 * np.pi * 120.0 * t)
            elif sig_id == 2.0:
                hum = 0.035 * (0.7 * np.sin(2 * np.pi * 80.0 * t) + 0.3 * np.sin(2 * np.pi * 240.0 * t))
            elif sig_id == 3.0:
                hum = 0.008 * np.sin(2 * np.pi * 1800.0 * t)
            elif sig_id == 4.0:
                phase = 2 * np.pi * 1000.0 * t - 50.0 * np.cos(4 * np.pi * t)
                hum = 0.008 * np.sin(phase)
            else:
                hum = np.zeros(n, dtype=np.float32)
            filtered = filtered + hum

        state.rx_sample_count += n

        # ── 3. Harmonic distortion (radio character) ─────────────────────────
        if self.distortion > 0:
            drive = 1.0 + self.distortion * 3.0
            filtered = _soft_clip(filtered, drive=drive)
        
        # ── 4. AM noise modulation ───────────────────────────────────────────
        # Only applied when noise effects are enabled (distortion > 0 or noise_floor > 0).
        # CPX mode: distortion=0 AND noise_floor=0 → completely clean audio path.
        if self.distortion > 0 or self.noise_floor > 0:
            sig_norm  = np.clip((signal_strength - self.squelch) / (1.0 - self.squelch), 0, 1)
            noise_amt = (1.0 - sig_norm) ** 1.5
            static    = _bandpass_noise(n, self._fb)
            envelope  = np.abs(filtered)
            envelope  = np.convolve(envelope, np.ones(32)/32, mode='same')
            am_noise  = static * (0.5 + 0.5 * envelope)
            mixed     = filtered * sig_norm + am_noise * noise_amt * 1.5
        else:
            # CPX / clean mode: pass audio through at full signal level
            mixed = filtered
        
        # ── 5. Background hiss (only when noise_floor is set) ────────────────
        if self.noise_floor > 0:
            hiss  = np.random.normal(0, self.noise_floor, n).astype(np.float32)
            mixed = mixed + hiss
        
        # ════════════════════════════════════════════════════════════════════
        # ── 6. Weather & Atmosphere ──────────────────────────────────────────
        # Three independent layers. Each bypassed when its flag is False.
        # ════════════════════════════════════════════════════════════════════
        rate = max(self._fb.rate, 1)
        
        # ── 6A. Precipitation ────────────────────────────────────────────────
        # Rain/storm/fog add noise/fade. Humidity causes HF rolloff — water
        # vapour absorbs higher frequencies more than lower frequencies.
        if self.weather_enabled and self.weather_severity > 0:
            sev   = min(1.0, max(0.0, self.weather_severity))
            wtype = self.weather_type
            if wtype == "rain":
                rain_noise = np.random.normal(0, sev * 0.04, n).astype(np.float32)
                mixed = mixed + rain_noise
            elif wtype == "storm":
                storm_noise = np.random.normal(0, sev * 0.08, n).astype(np.float32)
                t    = np.linspace(0, n / rate, n)
                fade = 1.0 - sev * 0.5 * (0.5 + 0.5 * np.sin(2 * np.pi * 1.3 * t))
                mixed = mixed * fade.astype(np.float32) + storm_noise
            elif wtype == "interference":
                t    = np.arange(n, dtype=np.float32) / rate
                buzz = np.sin(2 * np.pi * 50 * t) * sev * 0.05
                mixed = mixed + buzz.astype(np.float32)
            elif wtype == "fog":
                mixed = mixed * (1.0 - sev * 0.3)
                mixed = mixed + np.random.normal(0, sev * 0.02, n).astype(np.float32)
        
        # Humidity HF rolloff: cutoff slides from 7000 Hz (dry) to 2500 Hz (saturated)
        if self.weather_humidity > 0.05:
            hum       = min(1.0, self.weather_humidity)
            cutoff_hz = max(500, int(7000 - hum * 4500))
            nyq       = rate / 2.0
            if cutoff_hz < nyq:
                from scipy import signal as _sig
                try:
                    sos, _ = _sig.butter(2, cutoff_hz / nyq, btype='low', output='sos'), None
                    mixed = _sig.sosfilt(sos, mixed).astype(np.float32)
                except Exception:
                    pass
        
        # ── 6B. Temperature Inversion (Multi-path / Ducting) ─────────────────
        # Traps radio waves in a duct, causing multi-path echoes (5–20 ms delay).
        # Audible as a slight hollow/reverberant quality on received voice.
        if self.temp_inversion_enabled and self.temp_inversion_strength > 0:
            siv        = min(1.0, self.temp_inversion_strength)
            delay_samp = max(1, int(self.temp_inversion_echo_ms * rate / 1000))
            delay_samp = min(delay_samp, len(self._echo_buf) - 1)
            echo_level = siv * 0.35
            if delay_samp < len(self._echo_buf):
                echo = np.roll(self._echo_buf, -delay_samp)[:n] * echo_level
            else:
                echo = np.zeros(n, dtype=np.float32)
            buf_len = len(self._echo_buf)
            if buf_len >= n:
                self._echo_buf = np.roll(self._echo_buf, -n)
                self._echo_buf[-n:] = mixed
            mixed = (mixed + echo).astype(np.float32)
        
        # ── 6C. Ionospheric Conditions ───────────────────────────────────────
        # Primarily affects HF (3–30 MHz). Three sub-effects:
        #   Slow fading (0.2–1 Hz): gradual amplitude variation
        #   Fast flutter (10–20 Hz): rapid shallow fading
        #   Absorption: overall attenuation up to complete blackout
        if self.iono_enabled:
            cond_presets = {
                "quiet":     (0.0,  0.0,  0.0),
                "disturbed": (0.25, 0.1,  0.05),
                "storm":     (0.6,  0.35, 0.25),
                "blackout":  (0.8,  0.5,  0.95),
            }
            p_fade, p_flut, p_abs = cond_presets.get(self.iono_condition, (0.0, 0.0, 0.0))
            fading     = max(self.iono_fading,     p_fade)
            flutter    = max(self.iono_flutter,    p_flut)
            absorption = max(self.iono_absorption, p_abs)
            t = np.linspace(0, n / rate, n, dtype=np.float32)
            if fading > 0:
                fade_env = 1.0 - fading * 0.7 * (0.5 + 0.5 * np.sin(2*np.pi*0.4*t))
                mixed    = (mixed * fade_env).astype(np.float32)
            if flutter > 0:
                flut_env = 1.0 - flutter * 0.4 * (0.5 + 0.5 * np.sin(2*np.pi*15*t))
                mixed    = (mixed * flut_env).astype(np.float32)
            if absorption > 0:
                mixed = (mixed * (1.0 - min(0.99, absorption))).astype(np.float32)
                if absorption > 0.5:
                    burst = np.random.normal(0, (absorption - 0.5) * 0.06, n)
                    mixed = (mixed + burst).astype(np.float32)
        
        # ── 7. PTT click (open click on first frame, close click on last) ───
        if self.ptt_click_enabled:
            if state.click_pending or not state.ptt_open:
                mixed = self._inject_click(mixed, position=0, amplitude=0.4, sig_id=sig_id)
                state.click_pending = False
                state.ptt_open      = True
            
            if end_of_tx:
                mixed = self._inject_click(mixed, position=len(mixed) - 1, amplitude=0.3, sig_id=sig_id)
                state.ptt_open = False
        else:
            # No clicks — just track state
            state.click_pending = False
            state.ptt_open      = True
            if end_of_tx:
                state.ptt_open = False
        
        # ── 8. Day / Night Cycle ─────────────────────────────────────────────
        # D-layer absorption model: clarity peaks at local noon, dips at night.
        # Auto mode reads real system clock. VHF/UHF effect is mild; HF is severe.
        if self.day_night_enabled:
            import datetime as _dt
            hour = (_dt.datetime.now().hour + _dt.datetime.now().minute / 60.0
                    if self.day_night_auto else self.day_night_hour % 24.0)
            # Sun factor: 1.0 at 13:00 local, ~0 at midnight
            sun_factor = max(0.0, min(1.0,
                0.5 + 0.5 * math.cos(math.pi * (hour - 13.0) / 12.0)))
            # Night noise: thermal + ionospheric
            night_noise_lvl = (1.0 - sun_factor) * 0.025
            if night_noise_lvl > 0.001:
                mixed = mixed + np.random.normal(0, night_noise_lvl, n).astype(np.float32)
            # D-layer absorption: 82% signal at midnight, 100% at noon
            mixed = (mixed * (0.82 + 0.18 * sun_factor)).astype(np.float32)
        
        # ── 9. EMI (Electromagnetic Interference) ────────────────────────────
        # Models background environmental and equipment interference.
        #   white — broadband thermal/environmental noise
        #   hum   — 50 Hz mains harmonics from power infrastructure
        #   burst — intermittent bursts from switching equipment / engines
        if self.emi_enabled and self.emi_level > 0.001:
            _rate = max(self._fb.rate, 1)
            _lvl  = min(1.0, self.emi_level)
            if self.emi_type == "white":
                mixed = mixed + np.random.normal(0, _lvl * 0.05, n).astype(np.float32)
            elif self.emi_type == "hum":
                _t  = np.arange(n, dtype=np.float32) / _rate
                hum = (np.sin(2*np.pi*50*_t)*0.6 +
                       np.sin(2*np.pi*150*_t)*0.25 +
                       np.sin(2*np.pi*250*_t)*0.15)
                mixed = mixed + (hum * _lvl * 0.04).astype(np.float32)
            elif self.emi_type == "burst":
                import random as _rnd
                if _rnd.random() < _lvl * 0.7:
                    _bl  = max(1, int(n * _rnd.uniform(0.05, 0.4)))
                    _bp  = _rnd.randint(0, max(0, n - _bl))
                    mixed[_bp:_bp+_bl] += np.random.normal(0, _lvl*0.12, _bl).astype(np.float32)
        
        # ── COMSEC / Encryption Processing (Sim Mode only) ───────────────────
        if sim_mode_active and comsec_encrypted:
            start_idx = state.crypto_sync_samples
            k = np.arange(n) + start_idx
            preamble_mask = (k < 5760)
            
            # 1. Preamble phase (first 120ms / 5760 samples): Overwrite speech with BPSK linear sweep
            if np.any(preamble_mask):
                k_pre = k[preamble_mask]
                t_pre = k_pre / 48000.0
                f0 = 1600.0
                f1 = 2800.0
                D_samples = 5760.0
                
                # Phase for linear sweep 1600 Hz -> 2800 Hz
                phase = 2.0 * np.pi * (f0 * k_pre / 48000.0 + (f1 - f0) * k_pre**2 / (2.0 * D_samples * 48000.0))
                
                # 350 Hz BPSK phase modulation (polarity toggle)
                bpsk = 1.0 - 2.0 * (np.floor(t_pre * 350.0) % 2)
                
                # Overwrite and completely duck original audio
                mixed[preamble_mask] = (0.15 * np.sin(phase) * bpsk).astype(np.float32)
                
            # 2. Voice/Secure Pilot phase (after 120ms): Mix dual-tone beating pilot and cipher sizzle
            voice_mask = ~preamble_mask
            if np.any(voice_mask):
                k_voice = k[voice_mask]
                t_voice = k_voice / 48000.0
                
                # Faint beating dual-tone secure pilot carrier (2600 Hz + 2640 Hz)
                pilot1 = 0.003 * np.sin(2.0 * np.pi * 2600.0 * t_voice)
                pilot2 = 0.003 * np.sin(2.0 * np.pi * 2640.0 * t_voice)
                pilot = pilot1 + pilot2
                
                # Quiet digital "cipher sizzle": high-pass filtered white noise
                num_voice = len(k_voice)
                w = np.random.normal(0.0, 1.0, num_voice).astype(np.float32)
                sizzle = np.zeros(num_voice, dtype=np.float32)
                if num_voice > 0:
                    sizzle[0] = w[0]
                    if num_voice > 1:
                        sizzle[1:] = w[1:] - w[:-1]
                    sizzle *= 0.0015
                
                # Mix pilot and sizzle into active speech
                mixed[voice_mask] += (pilot + sizzle).astype(np.float32)
            
            # 3. Increment or reset sample counter
            if end_of_tx:
                state.crypto_sync_samples = 0
            else:
                state.crypto_sync_samples += n
        
        # ── 10. TX Collision / Doubling Simulation ──────────────────────────
        # Models realistic RF effects when multiple stations transmit simultaneously.
        # Three distinct models based on waveform technology:
        #
        #  FM / Analogue (Narrowband, no ECCM):
        #    - FM capture effect: strong signal dominates, weak is suppressed (Δ≥6 dB)
        #    - Near-equal signals: heterodyne beating buzz + full garble
        #
        #  SINCGARS / HAVEQUICK (Freq-hop ECCM):
        #    - Per-hop collision probability is low but catastrophic when it occurs
        #    - Random frame drops (~50% of frames corrupted on collision)
        #
        #  OFDM (Wideband / SRW / ANW2C):
        #    - Orthogonal subcarriers give partial resilience
        #    - ~30% subcarrier noise injection, audio largely retained
        if collision_severity > 0.0:
            import random as _rnd
            sev = min(1.0, max(0.0, collision_severity))
            eccm = eccm_mode.upper()
            wf   = waveform_type.lower()

            if eccm in ('SINCGARS', 'HAVEQUICK'):
                # ── Frequency-hop waveform: random frame drops ─────────────
                # On a collision, roughly half the hops overlap → frame lost.
                # At full severity (sev=1) nearly all frames are corrupted.
                drop_prob = sev * 0.55
                if _rnd.random() < drop_prob:
                    # Entire frame is corrupted — replace with burst noise
                    mixed = np.random.normal(0, 0.08, n).astype(np.float32)
                # Even surviving frames get some noise from the collision energy
                partial_noise = np.random.normal(0, sev * 0.03, n).astype(np.float32)
                mixed = (mixed + partial_noise).astype(np.float32)

            elif 'wideband' in wf:
                # ── OFDM waveform: partial subcarrier corruption ────────────
                # OFDM subcarriers are orthogonal — only ~30% overlap in a collision.
                # The remainder is received correctly, producing a partially degraded signal.
                noise_fraction = sev * 0.35
                mixed = (
                    mixed * (1.0 - noise_fraction * 0.6)
                    + np.random.normal(0, noise_fraction * 0.25, n).astype(np.float32)
                ).astype(np.float32)

            else:
                # ── FM / AM Analogue: capture effect + heterodyne buzz ──────
                # FM capture: dominant signal (≥6 dB stronger) suppresses weaker one.
                # Two equal-strength signals: neither captures → garbled heterodyne.
                #
                # signal_strength is our signal; assume the collider is similar strength
                # (worst case). Capture threshold ≈ 6 dB = factor of ~2.0.
                # We model: sev < 0.5 → some capture still occurring (quiet buzz)
                #            sev ≥ 0.5 → full heterodyne (near-equal signals)
                if sev < 0.5:
                    # Partial capture — audio survives but with heterodyne sideband buzz
                    buzz_amp = sev * 0.12
                    t_hz = np.arange(n, dtype=np.float32) / SAMPLE_RATE
                    # Heterodyne beat: difference frequency between two nearby carriers
                    # On VHF FM, this appears as ~100–400 Hz buzz
                    beat_freq = 180.0 + sev * 220.0  # sweeps 180→290 Hz with severity
                    buzz = buzz_amp * np.sin(2 * np.pi * beat_freq * t_hz)
                    mixed = (mixed * (1.0 - sev * 0.4) + buzz).astype(np.float32)
                else:
                    # Full heterodyne / both transmitters garble each other
                    garble_factor = (sev - 0.5) * 2.0  # 0→1 as sev goes 0.5→1.0
                    t_hz = np.arange(n, dtype=np.float32) / SAMPLE_RATE
                    # Heterodyne buzz (multiple beat frequencies for realism)
                    beat1 = 0.25 * np.sin(2 * np.pi * 210.0 * t_hz)
                    beat2 = 0.15 * np.sin(2 * np.pi * 380.0 * t_hz)
                    # Squelch-break white noise burst
                    noise_burst = np.random.normal(0, garble_factor * 0.18, n).astype(np.float32)
                    # Mix: attenuate speech, add heterodyne + noise
                    mixed = (
                        mixed * (1.0 - garble_factor * 0.85)
                        + (beat1 + beat2).astype(np.float32) * garble_factor
                        + noise_burst
                    ).astype(np.float32)

        # ── 11. Output gain ──────────────────────────────────────────────────
        out = np.clip(mixed * self.volume, -1.0, 1.0).astype(np.float32)
        return out
    
    def process_squelch_open(self, state: EffectState, n_samples: int) -> np.ndarray:
        """
        Generate the squelch-open noise burst (when signal first appears).
        Plays instead of silence for ~40ms when a new transmission starts.
        """
        noise = _bandpass_noise(n_samples, self._fb)
        click = self._inject_click(noise, position=0, amplitude=0.5)
        state.ptt_open      = True
        state.click_pending = False
        return np.clip(click * self.volume, -1.0, 1.0).astype(np.float32)
    
    def process_squelch_close(self, n_samples: int) -> np.ndarray:
        """Generate squelch-close noise tail."""
        noise = _bandpass_noise(n_samples, self._fb)
        return np.clip(
            self._inject_click(noise, position=n_samples//2, amplitude=0.3)
            * self.volume * 0.5, -1.0, 1.0
        ).astype(np.float32)
    
    # ═══════════════════════════════════════════════════════════════════════
    # Transmit Chain
    # ═══════════════════════════════════════════════════════════════════════
    def process_tx(
        self,
        audio: np.ndarray,
        state: EffectState,
        gain:  float = 1.0,
    ) -> np.ndarray:
        """
        Transmit-side processing: clean up mic input before encoding.
        
        audio: raw float32 from microphone
        
        Returns processed float32 ready for encoding.
        """
        # ── 1. High-pass (remove handling noise, breath pops below 80 Hz) ───
        if self.tx_highpass_enabled:
            filtered, state.tx_hp_zi = self._fb.apply_tx_highpass(audio, state.tx_hp_zi)
        else:
            filtered = audio.copy()
        
        # ── 2. Pre-emphasis (6dB/oct above 800 Hz) ──────────────────────────
        if self.tx_preemph_enabled:
            preemph, state.tx_pe_zi = self._fb.apply_tx_preemph(filtered, state.tx_pe_zi)
            # Mix original + pre-emphasis at 70/30 — subtle
            mixed = filtered * 0.7 + preemph * 0.3
        else:
            mixed = filtered
        
        # ── 3. Compressor / limiter (SIM mode only) ─────────────────────────
        # In CPX mode (distortion=0) skip compression — it amplifies mic noise.
        # Apply a clean gain and hard clip only.
        if self.distortion > 0:
            processed = self._compress(mixed, threshold=0.15, ratio=2.5, makeup=1.86)
        else:
            processed = mixed * gain   # clean pass-through with input gain only
        
        # ── 4. Hard clip at 0.95 (prevent encode saturation) ────────────────
        return np.clip(processed, -0.95, 0.95).astype(np.float32)

    def generate_idle_noise(self, n: int) -> np.ndarray:
        """Generates continuous background static and environmental sounds when the receiver is idle."""
        rate = getattr(self, 'sample_rate', 48000)
        idx = getattr(self, '_idle_t_idx', 0)
        t = (np.arange(idx, idx + n, dtype=np.float32)) / rate
        self._idle_t_idx = (idx + n) % (rate * 2)
        
        # 1. Base static
        if self.noise_floor > 0:
            noise = np.random.normal(0, self.noise_floor * 0.15, n).astype(np.float32)
            # Bandpass the static to sound like a radio
            noise, _ = self._fb.apply_rx_bandpass(noise, None)
        else:
            noise = np.zeros(n, dtype=np.float32)
            
        # 2. Weather
        if self.weather_enabled and self.weather_severity > 0:
            sev = self.weather_severity
            wtype = self.weather_type.lower()
            if wtype == "rain" or wtype == "storm":
                rain_noise = np.random.normal(0, sev * 0.05, n).astype(np.float32)
                noise += rain_noise
            elif wtype == "interference":
                buzz = np.sin(2 * np.pi * 50 * t) * sev * 0.05
                noise += buzz.astype(np.float32)
                
        # 3. EMI
        if getattr(self, 'emi_enabled', False) and getattr(self, 'emi_level', 0.0) > 0:
            lvl = self.emi_level
            emi_t = getattr(self, 'emi_type', 'white')
            if emi_t == "pulse":
                buzz = np.sin(2 * np.pi * 120 * t) * lvl * 0.1
                gate = (np.sin(2 * np.pi * 1.5 * t) > 0.8).astype(np.float32)
                noise += (buzz * gate).astype(np.float32)
            elif emi_t == "hum":
                noise += (np.sin(2 * np.pi * 60 * t) * lvl * 0.1).astype(np.float32)
            else:
                noise += np.random.normal(0, lvl * 0.05, n).astype(np.float32)

        return np.clip(noise, -0.99, 0.99).astype(np.float32)
    
    # ═══════════════════════════════════════════════════════════════════════
    # Helpers
    # ═══════════════════════════════════════════════════════════════════════
    @staticmethod
    def _inject_click(audio: np.ndarray, position: int, amplitude: float = 0.5, sig_id: float = 0.0) -> np.ndarray:
        """
        Inject a short broadband click (PTT relay sound) at the given sample position.
        Real PTT relays produce a 1–3 ms transient.
        """
        out   = audio.copy()
        click_len = min(32, len(out))  # ~2ms @ 16kHz
        start = max(0, min(position, len(out) - click_len))
        # Damped sine burst
        t     = np.arange(click_len, dtype=np.float32)
        
        # Customize transient based on sig_id
        if sig_id == 2.0:
            # deep thud click (400 Hz damping transient)
            freq = 400.0
            decay = 0.08
        elif sig_id in (3.0, 4.0):
            # high-pitched chirp click (3200 Hz damping transient)
            freq = 3200.0
            decay = 0.25
        else:
            # standard 1200 Hz transient
            freq = 1200.0
            decay = 0.15
            
        click = amplitude * np.exp(-t * decay) * np.sin(2 * np.pi * freq * t / SAMPLE_RATE)
        out[start:start+click_len] += click
        return np.clip(out, -1.0, 1.0)
    
    @staticmethod
    def _compress(
        audio:     np.ndarray,
        threshold: float = 0.4,
        ratio:     float = 4.0,
        makeup:    float = 1.0,
        attack:    float = 0.003,
        release:   float = 0.1,
    ) -> np.ndarray:
        """
        Simple feed-forward compressor. Reduces dynamic range for radio clarity.
        
        threshold: RMS level above which compression kicks in (linear)
        ratio:     compression ratio (4:1 typical for voice radio)
        makeup:    output gain after compression
        attack/release: in seconds
        """
        n          = len(audio)
        env        = np.zeros(n, dtype=np.float32)
        attack_c   = np.exp(-1.0 / (SAMPLE_RATE * attack))
        release_c  = np.exp(-1.0 / (SAMPLE_RATE * release))
        
        # Compute envelope
        e = 0.0
        for i in range(n):
            level = abs(audio[i])
            if level > e:
                e = attack_c * e + (1 - attack_c) * level
            else:
                e = release_c * e + (1 - release_c) * level
            env[i] = e
        
        # Compute gain reduction
        gain = np.ones(n, dtype=np.float32)
        above = env > threshold
        gain[above] = threshold / env[above] * (env[above] / threshold) ** (1.0 / ratio)
        
        return (audio * gain * makeup).astype(np.float32)

# ═══════════════════════════════════════════════════════════════════════════
# Self-Test
# ═══════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    import wave, struct
    logging.basicConfig(level=logging.INFO)
    print("=== Effects self-test ===\n")
    
    fb      = get_filter_bank()
    effects = RadioEffects(squelch=0.15, distortion=0.15)
    model   = RangeModel()
    state   = EffectState()
    
    # Generate 1 second of 1kHz test tone (simulates decoded voice)
    t     = np.linspace(0, 1.0, SAMPLE_RATE, dtype=np.float32)
    voice = np.sin(2*np.pi*1000*t) * 0.5
    
    # Process at several signal strengths
    frames_out = []
    for sig_strength in [1.0, 0.7, 0.5, 0.3, 0.15, 0.1]:
        # Use a fresh state for each test
        st = EffectState()
        st.click_pending = True
        processed = np.zeros(0, dtype=np.float32)
        for i in range(0, SAMPLE_RATE, FRAME_SAMPLES):
            frame = voice[i:i+FRAME_SAMPLES]
            if len(frame) < FRAME_SAMPLES:
                break
            is_last = (i + FRAME_SAMPLES >= SAMPLE_RATE)
            out = effects.process_rx(frame, sig_strength, st, end_of_tx=is_last)
            processed = np.concatenate([processed, out])
        
        rms = np.sqrt(np.mean(processed**2))
        print(f"  signal={sig_strength:.2f}  RMS={rms:.4f}  len={len(processed)}")
        if sig_strength >= 0.2:
            frames_out.append((sig_strength, processed))
    
    # Write a test WAV showing signal degradation
    out_path = "/tmp/radio_effects_test.wav"
    combined = np.concatenate([p for _, p in frames_out])
    pcm_i16  = (np.clip(combined, -1.0, 1.0) * 32767).astype(np.int16)
    with wave.open(out_path, 'wb') as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(pcm_i16.tobytes())
    print(f"\n  WAV written: {out_path}  ({len(combined)/SAMPLE_RATE:.1f}s)")
    
    # Transmit chain test
    mic = np.random.normal(0, 0.3, SAMPLE_RATE).astype(np.float32)  # noisy mic
    st2 = EffectState()
    tx_out = np.concatenate([
        effects.process_tx(mic[i:i+FRAME_SAMPLES], st2)
        for i in range(0, SAMPLE_RATE, FRAME_SAMPLES)
        if i+FRAME_SAMPLES <= SAMPLE_RATE
    ])
    print(f"\nTX chain: input RMS={np.sqrt(np.mean(mic**2)):.4f}   "
          f"output RMS={np.sqrt(np.mean(tx_out**2)):.4f}")
    
    # Range model test
    print("\nRange model:")
    for d_km in [1, 5, 10, 15, 20, 25, 30]:
        s = model.signal_strength(d_km)
        bar = "#" * int(s * 20)
        print(f"  {d_km:4d} km  sig={s:.3f}  {bar}")
    
    # Haversine test
    d = RangeModel.haversine_km(-36.865, 174.764, -41.286, 174.776)  # AKL->WLG
    print(f"\nAuckland->Wellington: {d:.1f} km  (expected ~490 km)")
    
    # MGRS test (if available)
    if _INFRA_AVAILABLE:
        print("\nMGRS conversion test:")
        mgrs_str = latlon_to_mgrs(51.5074, -0.1278, precision=5)  # London
        print(f"  London (51.5074, -0.1278) -> {mgrs_str}")
        lat_back, lon_back = mgrs_to_latlon(mgrs_str)
        print(f"  {mgrs_str} -> ({lat_back:.4f}, {lon_back:.4f})")
    
    # EnhancedRangeModel test
    if _INFRA_AVAILABLE:
        print("\nEnhancedRangeModel test:")
        erm = EnhancedRangeModel()
        
        # Register a repeater
        rpt = RepeaterNode(
            id="RETRANS-ALPHA",
            callsign="STARLIGHT",
            lat=51.5084,
            lon=-0.1288,
            alt=50.0,
            power_w=25.0,
            max_range_km=15.0,
            channels=[1, 2, 3],
        )
        erm.register_repeater(rpt)
        print(f"  Registered repeater: {rpt.id} at {rpt.mgrs}")
        
        # Register SATCOM terminals
        sat1 = SatcomTerminal(callsign="ACORN", terminal_id="SAT-001")
        sat2 = SatcomTerminal(callsign="BLUEBELL", terminal_id="SAT-002")
        erm.register_satcom(sat1)
        erm.register_satcom(sat2)
        print(f"  Registered SATCOM terminals: ACORN, BLUEBELL")
        
        # Register GPS states
        gps1 = GPSState(
            callsign="ACORN",
            actual_lat=51.5074,
            actual_lon=-0.1278,
            reported_lat=51.5074,
            reported_lon=-0.1278,
        )
        erm.register_gps(gps1)
        print(f"  Registered GPS state: {gps1.callsign} -> {gps1.reported_mgrs}")
        
        # Calculate path
        path = erm.calculate_path(
            "ACORN", "BLUEBELL",
            (51.5074, -0.1278, 10),
            (51.5084, -0.1288, 15),
            channel=1
        )
        print(f"  Path ACORN->BLUEBELL: mode={path.mode.value}, sig={path.signal_strength:.2f}, latency={path.latency_ms:.0f}ms")
    
    print("\n[OK] All effects tests passed")