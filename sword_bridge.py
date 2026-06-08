"""
MilRadio — SWORD Bridge
========================
Polls the SimBridge Gateway's REST API (/api/tracks) to get real-time unit
positions from SWORD and feed them into the radio server's range model.

This means:
  - When a unit moves in SWORD, its radio range changes automatically
  - The callsign mapping matches SWORD unit names to radio callsigns
  - No additional SWORD connection needed — we piggyback on SimBridge

Polling cycle (default 10s):
  GET http://simbridge:8888/api/tracks
  → for each track with a matching callsign:
       server.update_client_position(callsign, lat, lon, alt)

Callsign matching:
  SWORD unit names often differ from radio callsigns.  We do:
  1. Exact match (callsign == unit name)
  2. Case-insensitive match
  3. Configured mapping in radio_config.ini [sword_callsign_map]
  4. Party-based prefix (e.g. all "blue" party → callsign from unit name)
"""

from __future__ import annotations
import threading
import time
import logging
import json
import socket
import urllib.request
import urllib.error
from typing import Optional, Callable, Dict

log = logging.getLogger("radio.sword")


class SWORDBridge:
    """
    Polls SimBridge Gateway for SWORD unit positions and maps them to
    radio callsigns.

    Usage:
        bridge = SWORDBridge(
            gateway_url     = "http://10.1.1.1:8888",
            position_callback = server.update_client_position,
            callsign_map    = {"SUNRAY": "3/6 RNZIR BG HQ"},
            party_filter    = "blue",
            interval_s      = 10,
        )
        bridge.start()
    """

    def __init__(
        self,
        gateway_url:        str = "http://127.0.0.1:8888",
        position_callback:  Optional[Callable] = None,   # (callsign, lat, lon, alt)
        callsign_map:       Optional[Dict[str, str]] = None,   # radio_cs → sword_name
        party_filter:       str  = "",   # only sync this party, empty = all
        interval_s:         int  = 10,
        timeout_s:          float = 5.0,
    ):
        self._url             = gateway_url.rstrip("/")
        self.position_callback = position_callback
        self._callsign_map    = callsign_map or {}    # radio_cs → sword_name
        # Build reverse map: sword_name → radio_cs
        self._reverse_map: Dict[str, str] = {
            v.lower(): k for k, v in self._callsign_map.items()
        }
        self._party_filter    = party_filter.lower()
        self._interval        = interval_s
        self._timeout         = timeout_s
        self._running         = False
        self._thread: Optional[threading.Thread] = None

        # Stats
        self.tracks_synced   = 0
        self.last_sync       = 0.0
        self.last_error      = ""
        self.connected       = False

    def start(self):
        self._running = True
        self._thread  = threading.Thread(
            target=self._run, daemon=True, name="sword-bridge"
        )
        self._thread.start()
        log.info(f"SWORD bridge started → {self._url} (interval={self._interval}s)")

    def stop(self):
        self._running = False

    def add_callsign_mapping(self, radio_callsign: str, sword_name: str):
        """Dynamically add a callsign mapping."""
        self._callsign_map[radio_callsign]     = sword_name
        self._reverse_map[sword_name.lower()]  = radio_callsign

    def _run(self):
        while self._running:
            try:
                self._poll()
                self.connected  = True
                self.last_error = ""
            except Exception as e:
                self.connected  = False
                self.last_error = str(e)
                log.debug(f"SWORD bridge poll error: {e}")
            time.sleep(self._interval)

    def _poll(self):
        """Fetch /api/tracks and update positions."""
        url     = f"{self._url}/api/tracks"
        req     = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=self._timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        tracks = data.get("tracks", [])
        synced = 0

        for track in tracks:
            if not isinstance(track, dict):
                continue

            # Filter by party
            party = (track.get("party") or "").lower()
            if self._party_filter and party != self._party_filter:
                continue

            lat = float(track.get("lat", 0) or 0)
            lon = float(track.get("lon", 0) or 0)
            alt = float(track.get("alt", 0) or 0)

            if lat == 0.0 and lon == 0.0:
                continue  # no position data

            # Try to resolve to a radio callsign
            unit_name  = (track.get("callsign") or track.get("name") or "").strip()
            radio_cs   = self._resolve_callsign(unit_name)

            if radio_cs and self.position_callback:
                self.position_callback(radio_cs, lat, lon, alt)
                synced += 1
                log.debug(f"Synced [{radio_cs}] from SWORD '{unit_name}' "
                          f"→ ({lat:.4f},{lon:.4f},{alt:.0f}m)")

        self.tracks_synced += synced
        self.last_sync      = time.time()
        if synced:
            log.debug(f"SWORD sync: {synced}/{len(tracks)} tracks mapped")

    def _resolve_callsign(self, sword_name: str) -> Optional[str]:
        """
        Try to map a SWORD unit name to a radio callsign.
        Returns None if no mapping found.
        """
        if not sword_name:
            return None

        # 1. Exact match in reverse map
        cs = self._reverse_map.get(sword_name.lower())
        if cs:
            return cs

        # 2. Direct use of sword_name as callsign (exact)
        # (if the SWORD name IS the radio callsign)
        # This handles the common case where SWORD names match callsigns
        return sword_name if len(sword_name) <= 16 else None

    def get_status(self) -> dict:
        return {
            "connected":    self.connected,
            "gateway_url":  self._url,
            "last_sync":    self.last_sync,
            "tracks_synced":self.tracks_synced,
            "last_error":   self.last_error,
        }