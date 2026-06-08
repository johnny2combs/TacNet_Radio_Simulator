"""
MilRadio -- AAR Recording Engine
==================================
Hooks into RadioServer's relay loop to capture every transmission.

Architecture:
  RadioServer calls recorder.on_ptt_on() / on_voice() / on_ptt_off() for each
  event.  The recorder:
    - Opens a WAV writer per (callsign, channel) when PTT goes ON
    - Appends decoded PCM frames as they are relayed
    - Closes and finalises the WAV when PTT goes OFF or after a silence timeout
    - Appends one TransmissionRecord to the session manifest
    - Polls SimBridge (SWORD) for exercise sim-time if configured

Session folder layout:
  aar/
    <session_id>/           e.g.  20260316_091200/
      manifest.json         machine-readable session data (updated live)
      tx_0001_OA_CH01.wav
      tx_0002_ACORN_CH01.wav
      ...
    sessions.json           index of all sessions

Sim-time modes:
  "wall"   -- use wall-clock time directly (default, no setup needed)
  "manual" -- h_hour_wall set explicitly via set_h_hour()
  "sword"  -- poll SimBridge /api/status for exercise_time_s; derive h_hour_wall
              from (wall_now - exercise_time_s); refresh every 30s
"""

from __future__ import annotations
import json
import logging
import os
import struct
import threading
import time
import wave
import urllib.request
import urllib.error
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np

log = logging.getLogger("radio.recorder")

SAMPLE_RATE    = 48000   # must match radio_protocol.SAMPLE_RATE
SILENCE_TIMEOUT = 3.0    # close WAV after this many seconds of silence
AAR_DIR        = Path("aar")


# ─────────────────────────────────────────────────────────────────────────────
# Data structures
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class TransmissionRecord:
    idx:           int
    callsign:      str
    channel:       int
    channel_name:  str
    wall_start:    float          # UNIX timestamp
    wall_end:      float
    duration_s:    float
    ex_time_str:   str            # HH:MM:SS L  (local exercise time)
    wall_time_str: str            # HH:MM:SS L  (wall clock)
    signal_avg:    float          # 0.0 - 1.0
    sample_rate:   int
    file:          str            # relative path within session folder
    time_source:   str            # "wall" | "manual" | "sword"


@dataclass
class SessionMeta:
    session_id:    str
    exercise:      str
    date_str:      str
    h_hour_wall:   float          # wall-clock seconds since midnight of ex start
    time_source:   str
    sword_url:     Optional[str]
    started_at:    float          # UNIX timestamp
    ended_at:      Optional[float] = None
    is_live:       bool           = True
    transmissions: List[TransmissionRecord] = field(default_factory=list)
    events:        List[dict]              = field(default_factory=list)


# ─────────────────────────────────────────────────────────────────────────────
# Active transmission state (one per currently-transmitting callsign)
# ─────────────────────────────────────────────────────────────────────────────

class _ActiveTX:
    def __init__(self, callsign: str, channel: int, channel_name: str,
                 wav_path: Path, sample_rate: int):
        self.callsign     = callsign
        self.channel      = channel
        self.channel_name = channel_name
        self.wav_path     = wav_path
        self.sample_rate  = sample_rate
        self.wall_start   = time.time()
        self.last_frame   = time.time()
        self.signal_sum   = 0.0
        self.frame_count  = 0
        self._wav: Optional[wave.Wave_write] = None
        self._lock = threading.Lock()
        self._open_wav()

    def _open_wav(self):
        try:
            self._wav = wave.open(str(self.wav_path), 'wb')
            self._wav.setnchannels(1)
            self._wav.setsampwidth(2)          # 16-bit PCM
            self._wav.setframerate(self.sample_rate)
        except Exception as e:
            log.error("Failed to open WAV %s: %s", self.wav_path, e)
            self._wav = None

    def write_frame(self, pcm_f32: np.ndarray, signal: float):
        with self._lock:
            self.last_frame  = time.time()
            self.signal_sum += signal
            self.frame_count += 1
            if self._wav:
                try:
                    pcm_i16 = (np.clip(pcm_f32, -1.0, 1.0) * 32767).astype(np.int16)
                    self._wav.writeframes(pcm_i16.tobytes())
                except Exception as e:
                    log.debug("WAV write error: %s", e)

    def close(self) -> float:
        """Close WAV file, return duration in seconds."""
        with self._lock:
            if self._wav:
                try:
                    n_frames = self._wav.getnframes()
                    self._wav.close()
                    return n_frames / self.sample_rate
                except Exception as e:
                    log.debug("WAV close error: %s", e)
        return time.time() - self.wall_start

    @property
    def signal_avg(self) -> float:
        if self.frame_count == 0:
            return 1.0
        return self.signal_sum / self.frame_count

    @property
    def idle_secs(self) -> float:
        return time.time() - self.last_frame


# ─────────────────────────────────────────────────────────────────────────────
# Main recorder
# ─────────────────────────────────────────────────────────────────────────────

class Recorder:
    """
    Attach to RadioServer via:
        server._activity_cb = recorder.on_activity
        server.on_voice_frame = recorder.on_voice_frame  (added to server)
        server.on_ptt_on  = recorder.on_ptt_on
        server.on_ptt_off = recorder.on_ptt_off
    Or use the higher-level RadioServer.set_recorder(recorder).
    """

    def __init__(self, aar_dir: Path = AAR_DIR, sample_rate: int = SAMPLE_RATE):
        self._aar_dir     = Path(aar_dir)
        self._sample_rate = sample_rate
        self._session:    Optional[SessionMeta] = None
        self._session_dir: Optional[Path] = None
        self._active:     Dict[str, _ActiveTX] = {}   # callsign -> _ActiveTX
        self._lock        = threading.RLock()
        self._tx_counter  = 0
        self._running     = False

        # Sim-time state
        self._time_source = "wall"     # "wall" | "manual" | "sword"
        self._h_hour_wall = 0.0        # wall-clock secs since midnight at H-hour
        self._sword_url:  Optional[str] = None
        self._sword_ok    = False
        self._sword_sim_secs: Optional[float] = None  # latest sim_clock (secs since midnight)
        self._sword_sim_received_at: Optional[float] = None  # Unix time we received that reading
        self._sword_thread: Optional[threading.Thread] = None

        # Channel name lookup (set by WebAdminServer after init)
        self.channel_names: Dict[int, str] = {}

        # Callbacks for admin UI live updates
        self.on_tx_complete: Optional[callable] = None   # (TransmissionRecord)
        self.on_session_change: Optional[callable] = None

        # Silence-timeout watchdog
        self._watchdog_thread = threading.Thread(
            target=self._silence_watchdog, daemon=True, name="rec-watchdog")

    # ── Session management ─────────────────────────────────────────────────────

    def start_session(self, exercise: str = "EXERCISE",
                      time_source: str = "wall",
                      h_hour_wall: Optional[float] = None,
                      sword_url: Optional[str] = None) -> str:
        """Start a new recording session. Returns session_id."""
        with self._lock:
            if self._session and self._session.is_live:
                self.stop_session()

            now    = time.time()
            ts     = time.localtime(now)
            sid    = time.strftime("%Y%m%d_%H%M%S", ts)
            date_s = time.strftime("%d %b %Y", ts).upper()

            self._time_source = time_source
            self._sword_url   = sword_url

            if time_source == "wall" or h_hour_wall is None:
                # H-hour = right now (session start wall time)
                self._h_hour_wall = ts.tm_hour * 3600 + ts.tm_min * 60 + ts.tm_sec
            else:
                self._h_hour_wall = float(h_hour_wall)
            self._sword_sim_secs = None        # reset each session; poll loop repopulates
            self._sword_sim_received_at = None

            self._session = SessionMeta(
                session_id  = sid,
                exercise    = exercise,
                date_str    = date_s,
                h_hour_wall = self._h_hour_wall,
                time_source = time_source,
                sword_url   = sword_url,
                started_at  = now,
                is_live     = True,
            )

            self._session_dir = self._aar_dir / sid
            self._session_dir.mkdir(parents=True, exist_ok=True)
            self._tx_counter = 0
            self._running    = True

            self._write_manifest()

            if not self._watchdog_thread.is_alive():
                self._watchdog_thread = threading.Thread(
                    target=self._silence_watchdog, daemon=True, name="rec-watchdog")
                self._watchdog_thread.start()

            if time_source == "sword" and sword_url:
                self._start_sword_sync()

            log.info("Recording started: session %s  time_source=%s", sid, time_source)
            if self.on_session_change:
                self.on_session_change(self._session_summary())
            return sid

    def stop_session(self) -> Optional[str]:
        """Stop the current session. Returns session_id."""
        with self._lock:
            if not self._session:
                return None
            # Close any still-open transmissions
            for cs in list(self._active.keys()):
                self._close_tx(cs)
            self._session.is_live   = False
            self._session.ended_at  = time.time()
            self._running = False
            self._write_manifest()
            self._update_sessions_index()
            sid = self._session.session_id
            log.info("Recording stopped: session %s  (%d transmissions)",
                     sid, len(self._session.transmissions))
            if self.on_session_change:
                self.on_session_change(self._session_summary())
            return sid

    def add_event_marker(self, label: str, details: str = "", timestamp: Optional[float] = None) -> Optional[dict]:
        """Add a custom event marker to the active AAR session."""
        if not self._running or not self._session:
            return None
        with self._lock:
            ts = timestamp or time.time()
            evt = {
                "timestamp": ts,
                "ex_time_str": self._ex_time_str(ts),
                "wall_time_str": _wall_time_str(ts),
                "label": label,
                "details": details
            }
            self._session.events.append(evt)
            self._write_manifest()
            log.info("AAR Marker added: %s (%s)", label, evt["ex_time_str"])
            return evt

    # ── H-hour / sim-time control ─────────────────────────────────────────────

    def set_h_hour(self, h_hour_wall: float, source: str = "manual"):
        """
        Set H-hour as wall-clock seconds since midnight.
        e.g. 09:12:00 = 9*3600 + 12*60 = 33120
        """
        with self._lock:
            self._h_hour_wall = float(h_hour_wall)
            self._time_source = source
            if self._session:
                self._session.h_hour_wall = self._h_hour_wall
                self._session.time_source = source
                self._write_manifest()
        log.info("H-hour set to %s (source=%s)",
                 _secs_to_hhmmss(h_hour_wall), source)

    def _start_sword_sync(self):
        """Start (or restart) the SWORD background poll thread."""
        # If a thread is already running, signal it to exit by clearing the URL
        # momentarily — the loop checks self._sword_url each iteration
        # Actually: just let it run and start a fresh one; the old one will
        # exit naturally on the next sleep cycle when _running is checked.
        # We use a generation counter to let the old thread know it's stale.
        self._sword_thread = threading.Thread(
            target=self._sword_poll_loop, daemon=True, name="rec-sword")
        self._sword_thread.start()

    def _sword_poll_loop(self):
        """
        Poll SimBridge /api/status every 30s for sim_clock.

        SimBridge returns:
          { "sim_clock": "2026-03-16T09:14:32", "gateway_running": true, ... }

        sim_clock is the current exercise datetime as an ISO string.
        We derive H-hour by computing the exercise start time = sim_clock minus
        elapsed wall-clock time since this session started.

        The simplest reliable approach: parse sim_clock as a datetime, extract
        its time-of-day component as seconds since midnight, and store that as
        h_hour_wall.  This means the exercise time axis directly reflects the
        SWORD scenario clock, regardless of whether VBS4 and the PC clock agree.
        """
        session_start_wall  = time.time()
        first_sim_secs      = None

        while self._running and self._sword_url:
            try:
                url = self._sword_url.rstrip('/') + '/api/status'
                with urllib.request.urlopen(url, timeout=3) as r:
                    data = json.loads(r.read())

                sim_clock = data.get("sim_clock") or ""

                if sim_clock:
                    # Parse ISO datetime from SWORD: "2026-03-16T09:14:32"
                    sim_secs = _parse_sim_clock(sim_clock)
                    if sim_secs is not None:
                        if first_sim_secs is None:
                            first_sim_secs = sim_secs
                        elapsed = time.time() - session_start_wall
                        new_hhour = sim_secs - elapsed % 86400
                        new_hhour = new_hhour % 86400
                        with self._lock:
                            self._h_hour_wall         = new_hhour
                            self._sword_sim_secs      = sim_secs   # store latest sim clock
                            self._sword_sim_received_at = time.time()  # when we got it
                            self._sword_ok            = True
                            if self._session:
                                self._session.h_hour_wall = new_hhour
                                self._session.time_source = "sword"
                        log.debug("SWORD sim_clock=%s  h_hour=%s",
                                  sim_clock, _secs_to_hhmmss(new_hhour))
                else:
                    # Gateway connected but SWORD not running yet
                    self._sword_ok = True
                    log.debug("SWORD connected — no sim_clock yet (scenario not started?)")

            except Exception as e:
                self._sword_ok = False
                log.debug("SWORD poll failed (%s): %s", self._sword_url, e)
            time.sleep(30)

    # ── PTT / voice hooks ──────────────────────────────────────────────────────

    def on_ptt_on(self, callsign: str, channel: int):
        """Called by RadioServer when a client starts transmitting."""
        if not self._running or not self._session:
            return
        with self._lock:
            if callsign in self._active:
                return  # already recording this callsign
            ch_name  = self.channel_names.get(channel, f"CH{channel:02d}")
            self._tx_counter += 1
            fname    = (f"tx_{self._tx_counter:04d}_"
                        f"{callsign.replace(' ','_')}_CH{channel:02d}.wav")
            wav_path = self._session_dir / fname
            atx      = _ActiveTX(callsign, channel, ch_name, wav_path, self._sample_rate)
            self._active[callsign] = atx
            log.debug("REC start: %s CH%02d -> %s", callsign, channel, fname)

    def on_voice_frame(self, callsign: str, channel: int,
                       pcm: np.ndarray, signal: float):
        """Called by RadioServer for each decoded voice frame."""
        if not self._running:
            return
        with self._lock:
            atx = self._active.get(callsign)
        if atx and atx.channel == channel:
            atx.write_frame(pcm, signal)

    def on_ptt_off(self, callsign: str, channel: int):
        """Called by RadioServer when a client stops transmitting."""
        if not self._running:
            return
        with self._lock:
            self._close_tx(callsign)

    def _close_tx(self, callsign: str):
        """Finalise a WAV and append record. Must hold self._lock."""
        atx = self._active.pop(callsign, None)
        if atx is None:
            return
        duration = atx.close()
        if duration < 0.1:
            # Discard sub-100ms noise bursts
            try:
                atx.wav_path.unlink(missing_ok=True)
            except Exception:
                pass
            return

        wall_start = atx.wall_start
        rec = TransmissionRecord(
            idx          = self._tx_counter,
            callsign     = callsign,
            channel      = atx.channel,
            channel_name = atx.channel_name,
            wall_start   = wall_start,
            wall_end     = wall_start + duration,
            duration_s   = round(duration, 2),
            ex_time_str  = self._ex_time_str(wall_start),
            wall_time_str= _wall_time_str(wall_start),
            signal_avg   = round(atx.signal_avg, 3),
            sample_rate  = atx.sample_rate,
            file         = atx.wav_path.name,
            time_source  = self._time_source,
        )
        self._session.transmissions.append(rec)
        self._write_manifest()

        log.info("REC: %s CH%02d  %s  %.1fs  sig=%.0f%%",
                 callsign, atx.channel, rec.ex_time_str,
                 duration, atx.signal_avg * 100)

        if self.on_tx_complete:
            try:
                self.on_tx_complete(rec)
            except Exception:
                pass

    # ── Silence watchdog ──────────────────────────────────────────────────────

    def _silence_watchdog(self):
        """Close any transmission that has been silent for SILENCE_TIMEOUT seconds."""
        while self._running:
            time.sleep(1.0)
            with self._lock:
                stale = [cs for cs, atx in self._active.items()
                         if atx.idle_secs > SILENCE_TIMEOUT]
                for cs in stale:
                    log.debug("Silence timeout: closing TX for %s", cs)
                    self._close_tx(cs)

    # ── Time helpers ──────────────────────────────────────────────────────────

    def _ex_time_str(self, wall_unix: float) -> str:
        """
        Return the exercise time string for a transmission at wall_unix.

        - SWORD mode: interpolate the scenario sim clock at the moment of the TX.
          sim_time_at_tx = last_known_sim_secs + (wall_unix - when_we_got_it)
          This gives accurate scenario-clock timestamps even between 30s polls.
        - Wall / manual mode: return the wall clock time as HH:MM:SSL.
        """
        if (self._time_source == "sword"
                and self._sword_sim_secs is not None
                and self._sword_sim_received_at is not None):
            elapsed = wall_unix - self._sword_sim_received_at
            sim_at_tx = (self._sword_sim_secs + elapsed) % 86400
            return _secs_to_hhmmss(sim_at_tx) + "L"
        # Fallback: wall clock time
        lt   = time.localtime(wall_unix)
        secs = lt.tm_hour * 3600 + lt.tm_min * 60 + lt.tm_sec
        return _secs_to_hhmmss(secs) + "L"

    # ── Manifest / index ──────────────────────────────────────────────────────

    def _write_manifest(self):
        """Write session manifest JSON (called under self._lock)."""
        if not self._session or not self._session_dir:
            return
        try:
            data = {
                "session_id":   self._session.session_id,
                "exercise":     self._session.exercise,
                "date_str":     self._session.date_str,
                "h_hour_wall":  self._session.h_hour_wall,
                "time_source":  self._session.time_source,
                "sword_url":    self._session.sword_url,
                "started_at":   self._session.started_at,
                "ended_at":     self._session.ended_at,
                "is_live":      self._session.is_live,
                "sword_ok":     self._sword_ok,
                "transmissions": [asdict(t) for t in self._session.transmissions],
                "events":       [e for e in self._session.events],
            }
            path = self._session_dir / "manifest.json"
            path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except Exception as e:
            log.warning("Manifest write failed: %s", e)

    def _update_sessions_index(self):
        """Update aar/sessions.json with summary of all sessions."""
        try:
            self._aar_dir.mkdir(parents=True, exist_ok=True)
            idx_path = self._aar_dir / "sessions.json"
            index    = []
            if idx_path.exists():
                try:
                    index = json.loads(idx_path.read_text(encoding="utf-8"))
                except Exception:
                    index = []
            # Remove old entry for this session if present
            if self._session:
                index = [s for s in index if s.get("session_id") != self._session.session_id]
                index.append(self._session_summary())
            index.sort(key=lambda s: s.get("started_at", 0), reverse=True)
            idx_path.write_text(json.dumps(index, indent=2), encoding="utf-8")
        except Exception as e:
            log.warning("Sessions index write failed: %s", e)

    # ── Public API for web server ─────────────────────────────────────────────

    def get_status(self) -> dict:
        """Return current recorder state for /api/aar/status."""
        with self._lock:
            active_tx = [
                {"callsign": cs, "channel": atx.channel,
                 "duration_s": round(time.time() - atx.wall_start, 1)}
                for cs, atx in self._active.items()
            ]
            if self._session:
                sm = self._session_summary()
            else:
                sm = None
        return {
            "recording":              self._running and self._session is not None,
            "time_source":            self._time_source,
            "sword_ok":               self._sword_ok,
            "sword_url":              self._sword_url,
            "sword_sim_secs":         self._sword_sim_secs,
            "sword_sim_received_at":  self._sword_sim_received_at,
            "sword_sim_str":          (_secs_to_hhmmss(self._sword_sim_secs) + "L"
                                       if self._sword_sim_secs is not None else None),
            "h_hour_wall":            self._h_hour_wall,
            "h_hour_str":             _secs_to_hhmmss(self._h_hour_wall) + "L",
            "active_tx":              active_tx,
            "session":                sm,
        }

    def get_session_list(self) -> list:
        """Return list of all sessions from the index file."""
        idx_path = self._aar_dir / "sessions.json"
        if not idx_path.exists():
            return []
        try:
            return json.loads(idx_path.read_text(encoding="utf-8"))
        except Exception:
            return []

    def get_session(self, session_id: str) -> Optional[dict]:
        """Return full manifest for a specific session."""
        mpath = self._aar_dir / session_id / "manifest.json"
        if not mpath.exists():
            return None
        try:
            return json.loads(mpath.read_text(encoding="utf-8"))
        except Exception:
            return None

    def generate_aar_html(self, session_id: str, template_path: Path,
                          server_url: str = "http://localhost:8890") -> Optional[str]:
        """
        Generate a self-contained AAR HTML report for session_id.
        server_url is the MilRadio web admin base URL used to fetch WAV files.
        Returns the HTML string, or None if session not found.
        """
        manifest = self.get_session(session_id)
        if not manifest:
            return None
        if not template_path.exists():
            log.warning("AAR template not found: %s", template_path)
            return None
        try:
            template = template_path.read_text(encoding="utf-8")

            start_marker = "// CONFIGURATION"
            if start_marker not in template or "const RAW = [" not in template:
                log.error("AAR template missing expected markers")
                return None

            start_pos = template.index(start_marker)
            raw_decl_pos = template.index("const RAW = [")
            after_raw = template[raw_decl_pos:]
            import re as _re
            m = _re.search(r'\n\];', after_raw)
            if not m:
                log.error("AAR template: could not find end of const RAW")
                return None
            end_pos = raw_decl_pos + m.end()

            injection = _manifest_to_session_js(manifest, self.channel_names,
                                                server_url=server_url)
            return template[:start_pos] + injection + template[end_pos:]

        except Exception as e:
            log.error("AAR HTML generation failed: %s", e)
            return None

    def _session_summary(self) -> dict:
        s = self._session
        if not s:
            return {}
        dur = (s.ended_at or time.time()) - s.started_at
        return {
            "session_id":      s.session_id,
            "exercise":        s.exercise,
            "date_str":        s.date_str,
            "started_at":      s.started_at,
            "ended_at":        s.ended_at,
            "is_live":         s.is_live,
            "duration_s":      round(dur, 1),
            "tx_count":        len(s.transmissions),
            "time_source":     s.time_source,
            "h_hour_wall":     s.h_hour_wall,
            "h_hour_str":      _secs_to_hhmmss(s.h_hour_wall) + "L",
            "sword_ok":        self._sword_ok,
        }


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _secs_to_hhmmss(secs: float) -> str:
    s = int(secs) % 86400
    return f"{s//3600:02d}:{(s%3600)//60:02d}:{s%60:02d}"

def _parse_sim_clock(sim_clock: str) -> float | None:
    """
    Parse a SWORD sim_clock string to seconds since midnight.

    Handles all formats SimBridge may return:
      "20240925T152940"       compact ISO (no colons in time)
      "2026-03-16T09:14:32"   ISO 8601 with colons
      "2026-03-16 09:14:32"   space-separated
      "09:14:32"              bare HH:MM:SS
      "152940"                bare HHMMSS compact
    Returns None on failure.
    """
    if not sim_clock:
        return None
    try:
        s = sim_clock.strip()
        # Strip date portion if present (handles both "T" and space separator)
        if "T" in s:
            s = s.split("T")[1]
        elif " " in s and len(s) > 8:
            s = s.split(" ")[1]
        # s is now either "HH:MM:SS[.fff]" or compact "HHMMSS"
        if ":" in s:
            parts = s.split(":")
            h   = int(parts[0])
            m   = int(parts[1])
            sec = float(parts[2]) if len(parts) > 2 else 0.0
        elif len(s) >= 6:
            # Compact HHMMSS or HHMMSS.fff
            h   = int(s[0:2])
            m   = int(s[2:4])
            sec = float(s[4:]) if len(s) > 6 else float(s[4:6])
        else:
            return None
        if not (0 <= h < 24 and 0 <= m < 60 and 0 <= sec < 60):
            return None
        return h * 3600 + m * 60 + sec
    except Exception:
        return None

def _wall_secs_now() -> float:
    lt = time.localtime()
    return lt.tm_hour * 3600 + lt.tm_min * 60 + lt.tm_sec

def _wall_time_str(unix: float) -> str:
    lt = time.localtime(unix)
    return f"{lt.tm_hour:02d}:{lt.tm_min:02d}:{lt.tm_sec:02d}L"

def _manifest_to_session_js(manifest: dict, channel_names: dict = None,
                             server_url: str = "http://localhost:8890") -> str:
    """
    Convert a session manifest into the four JS data constants the AAR template needs:
      SESSION, CHANNELS, CALLSIGNS, RAW
    channel_names: optional {ch_id: ch_name} dict from the recorder.
    """
    txs  = manifest.get("transmissions", [])
    h    = manifest.get("h_hour_wall", 0)

    # ── SESSION ────────────────────────────────────────────────────────────────
    started  = manifest.get("started_at", time.time())
    ended    = manifest.get("ended_at") or time.time()
    duration = round(ended - started)

    session_js = (
        "// CONFIGURATION -- injected by radio_recorder.py\n"
        "// -------------------------------------------------\n"
        f"const SESSION = {{\n"
        f"  exercise:     {json.dumps(manifest.get('exercise', 'EXERCISE'))},\n"
        f"  date:         {json.dumps(manifest.get('date_str', ''))},\n"
        f"  session_id:   {json.dumps(manifest.get('session_id', ''))},\n"
        f"  generated:    {json.dumps(_wall_time_str(time.time()))},\n"
        f"  duration_s:   {duration},\n"
        f"  h_hour_wall:  {h},\n"
        f"  h_hour_source:{json.dumps(manifest.get('time_source', 'wall'))},\n"
        f"  sword_url:    {json.dumps(manifest.get('sword_url'))},\n"
        f"  is_live:      false,\n"
        f"  server_url:   {json.dumps(server_url)},\n"
        f"}};\n"
    )

    # ── CHANNELS ───────────────────────────────────────────────────────────────
    # Build from transmissions — use channel_names for display names if available
    CH_COLOURS = [
        "#39ff55", "#00ddff", "#ffaa00", "#ff6655",
        "#cc88ff", "#55ffee", "#ffdd44", "#ff88cc",
    ]
    seen_chs = {}   # ch_id -> first occurrence order
    for tx in txs:
        cid = tx.get("channel", 0)
        if cid not in seen_chs:
            seen_chs[cid] = len(seen_chs)

    ch_rows = []
    for cid, order in sorted(seen_chs.items(), key=lambda x: x[0]):
        name     = (channel_names or {}).get(cid) or tx.get("channel_name") or f"CH{cid:02d}"
        # Try to get channel_name from first tx on this channel
        for tx in txs:
            if tx.get("channel") == cid and tx.get("channel_name"):
                name = tx["channel_name"]
                break
        colour   = CH_COLOURS[order % len(CH_COLOURS)]
        ch_rows.append(
            f"  {{id:{cid}, name:{json.dumps(name)}, "
            f"freq:\"\", color:{json.dumps(colour)}, encrypted:false}},"
        )

    channels_js = "const CHANNELS = [\n" + "\n".join(ch_rows) + "\n];\n"

    # ── CALLSIGNS ──────────────────────────────────────────────────────────────
    # Derive unique callsigns from transmissions
    seen_cs = {}   # callsign -> first tx
    for tx in txs:
        cs = tx.get("callsign", "")
        if cs and cs not in seen_cs:
            seen_cs[cs] = tx

    cs_rows = []
    for cs in seen_cs:
        cs_rows.append(
            f"  {{cs:{json.dumps(cs)}, name:\"\", unit:\"\", role:\"\"}},"
        )

    callsigns_js = "const CALLSIGNS = [\n" + "\n".join(cs_rows) + "\n];\n"

    # ── RAW transmissions ──────────────────────────────────────────────────────
    raw_rows = []
    for tx in txs:
        wall_unix = tx.get("wall_start", 0)
        lt        = time.localtime(wall_unix)
        wall_day  = lt.tm_hour * 3600 + lt.tm_min * 60 + lt.tm_sec
        dur       = tx.get("duration_s", 0)
        sig       = tx.get("signal_avg", 1.0)
        # exwall: the exercise/scenario clock seconds for this TX.
        # When SWORD is active, ex_time_str contains the sim clock time
        # (e.g. "15:29:40L") — parse it back to seconds for timeline use.
        # Falls back to wall_day when time_source is wall or manual.
        ex_str    = tx.get("ex_time_str", "")
        exwall    = wall_day  # default: same as wall clock
        if ex_str and ex_str.endswith("L"):
            try:
                parts = ex_str[:-1].split(":")
                if len(parts) == 3:
                    exwall = int(parts[0])*3600 + int(parts[1])*60 + float(parts[2])
            except Exception:
                exwall = wall_day
        raw_rows.append(
            f"  {{idx:{tx['idx']},cs:{json.dumps(tx['callsign'])},"
            f"ch:{tx['channel']},wall:{wall_day},exwall:{round(exwall,1)},"
            f"d:{round(dur,2)},sig:{round(sig,3)},"
            f"file:{json.dumps(tx.get('file',''))}}},"
        )

    raw_js = (
        "// Raw transmission data\n"
        "// wall = PC wall clock secs since midnight\n"
        "// exwall = exercise/scenario clock secs (sim time when SWORD active, else = wall)\n"
        "const RAW = [\n" +
        "\n".join(raw_rows) +
        "\n];"
    )

    events_js = f"const EVENTS = {json.dumps(manifest.get('events', []))};\n"

    return "\n".join([session_js, channels_js, callsigns_js, events_js, raw_js])