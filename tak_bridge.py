"""
TacNet — TAK Bridge
======================
Integrates the radio with the TAK ecosystem:

TX (radio → TAK):
  PTT ON  → CoT event type "b-f-t-r" (radio transmission marker)
            Appears on ATAK as a radio icon at the transmitter's last known position
  PTT OFF → Delete the CoT event (transmission ended)
  Position updates from radio → update our CoT SA track

RX (TAK → radio):
  SA position updates from other units → feed into server range model
  Chat messages mentioning a callsign → optional notification

CoT event format for PTT:
  <event version="2.0" uid="radio-{callsign}" type="b-f-t-r" ...>
    <point lat="-39.0" lon="176.0" hae="100" ce="50" le="50"/>
    <detail>
      <contact callsign="{callsign}"/>
      <remarks>TRANSMITTING on CH{channel} {frequency}</remarks>
    </detail>
  </event>
"""

from __future__ import annotations
import socket
import threading
import time
import logging
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Optional, Callable
from pathlib import Path

log = logging.getLogger("radio.tak")

# CoT time format
_COT_FMT = "%Y-%m-%dT%H:%M:%S.%fZ"

def _cot_time(dt: Optional[datetime] = None) -> str:
    if dt is None:
        dt = datetime.now(timezone.utc)
    return dt.strftime(_COT_FMT)

def _stale_time(seconds: float = 300.0) -> str:
    dt = datetime.fromtimestamp(time.time() + seconds, tz=timezone.utc)
    return dt.strftime(_COT_FMT)


class TAKBridge:
    """
    Connects to a TAKServer via TCP and sends/receives CoT events.

    The bridge runs two threads:
      - TX thread: sends queued CoT events to TAKServer
      - RX thread: receives CoT from TAKServer, calls position_callback

    position_callback(callsign, lat, lon, alt):
        Called whenever a SA position update is received from TAK.
        Used to feed positions into the server's range model.
    """

    @property
    def is_connected(self) -> bool:
        """Returns True if the bridge is currently connected to the TAKServer."""
        return self._sock is not None and self._running

    def __init__(
        self,
        server_url:         str,           # e.g. "tcp://10.0.0.1:8087"
        callsign:           str,
        uid_prefix:         str = "radio",
        position_callback:  Optional[Callable] = None,
    ):
        self.callsign          = callsign
        self.uid_prefix        = uid_prefix
        self.position_callback = position_callback
        self._running          = False
        self._sock:   Optional[socket.socket] = None
        self._lock    = threading.Lock()
        self._tx_q:   list = []

        # Parse server URL
        self._host, self._port = self._parse_url(server_url)

        # Own position (updated from radio_server)
        self._lat = 0.0
        self._lon = 0.0
        self._alt = 0.0

    @staticmethod
    def _parse_url(url: str) -> tuple[str, int]:
        """Parse 'tcp://host:port' → (host, port)."""
        url = url.replace("tcp://", "").replace("ssl://", "")
        if ":" in url:
            host, port_s = url.rsplit(":", 1)
            return host, int(port_s)
        return url, 8087

    def start(self):
        self._running = True
        threading.Thread(target=self._run, daemon=True, name="tak-bridge").start()
        log.info(f"TAK bridge started → {self._host}:{self._port}")

    def stop(self):
        self._running = False
        try:
            if self._sock:
                self._sock.close()
        except Exception:
            pass

    def update_position(self, lat: float, lon: float, alt: float = 0.0):
        """Called when our position changes (from SWORD or GPS)."""
        self._lat = lat
        self._lon = lon
        self._alt = alt

    def send_ptt_on(self, channel: int, frequency: str = ""):
        """Announce we are transmitting."""
        cot = self._make_ptt_cot(True, channel, frequency)
        self._queue_send(cot)

    def send_ptt_off(self, channel: int):
        """Announce transmission ended — delete marker."""
        cot = self._make_delete_cot(f"{self.uid_prefix}-{self.callsign}-tx")
        self._queue_send(cot)

    def send_position(self):
        """Send own SA position update."""
        cot = self._make_sa_cot()
        self._queue_send(cot)

    def send_radio_check(self, target_callsign: str, channel: int):
        """Send a radio check chat message visible in TAK."""
        cot = self._make_chat_cot(
            f"{self.callsign}: Radio check {target_callsign} on CH{channel:02d}"
        )
        self._queue_send(cot)

    # ── CoT builders ──────────────────────────────────────────────────────────
    def _make_ptt_cot(self, transmitting: bool, channel: int, frequency: str = "") -> str:
        uid  = f"{self.uid_prefix}-{self.callsign}-tx"
        now  = _cot_time()
        stale = _stale_time(10.0)  # PTT marker expires in 10s if not renewed
        freq_str = f" {frequency}" if frequency else ""
        
        # Offset lat slightly so it doesn't z-fight with the main SA marker
        offset_lat = self._lat + 0.00005
        
        return f"""<?xml version="1.0" encoding="UTF-8"?>
<event version="2.0" uid="{uid}" type="b-m-p-s-m"
       time="{now}" start="{now}" stale="{stale}" how="m-g">
  <point lat="{offset_lat:.7f}" lon="{self._lon:.7f}"
         hae="{self._alt:.1f}" ce="50" le="50"/>
  <detail>
    <contact callsign="{self.callsign} [TX]"/>
    <remarks>TRANSMITTING — CH{channel:02d}{freq_str}</remarks>
  </detail>
</event>"""

    def _make_sa_cot(self) -> str:
        uid   = f"{self.uid_prefix}-{self.callsign}"
        now   = _cot_time()
        stale = _stale_time(120.0)
        return f"""<?xml version="1.0" encoding="UTF-8"?>
<event version="2.0" uid="{uid}" type="a-f-G-U-C"
       time="{now}" start="{now}" stale="{stale}" how="m-g">
  <point lat="{self._lat:.7f}" lon="{self._lon:.7f}"
         hae="{self._alt:.1f}" ce="50" le="50"/>
  <detail>
    <contact callsign="{self.callsign}"/>
    <remarks>TacNet</remarks>
  </detail>
</event>"""

    def _make_delete_cot(self, uid: str) -> str:
        now = _cot_time()
        return f"""<?xml version="1.0" encoding="UTF-8"?>
<event version="2.0" uid="{uid}" type="t-x-d-d"
       time="{now}" start="{now}" stale="{now}" how="m-g">
  <point lat="0" lon="0" hae="0" ce="9999999" le="9999999"/>
  <detail/>
</event>"""

    def _make_chat_cot(self, message: str) -> str:
        uid   = f"{self.uid_prefix}-chat-{int(time.time()*1000)}"
        now   = _cot_time()
        stale = _stale_time(60.0)
        return f"""<?xml version="1.0" encoding="UTF-8"?>
<event version="2.0" uid="{uid}" type="b-t-f"
       time="{now}" start="{now}" stale="{stale}" how="h-g-i-g-o">
  <point lat="{self._lat:.7f}" lon="{self._lon:.7f}"
         hae="{self._alt:.1f}" ce="9999999" le="9999999"/>
  <detail>
    <__chat parent="RootContactGroup" groupOwner="false"
            messageId="{uid}" chatroom="All Chat Rooms" id="All Chat Rooms">
      <chatgrp uid0="{self.uid_prefix}-{self.callsign}"
               id="All Chat Rooms"/>
    </__chat>
    <link uid="{self.uid_prefix}-{self.callsign}"
          type="a-f-G-U-C" relation="p-p"/>
    <remarks source="{self.callsign}" time="{now}">{message}</remarks>
  </detail>
</event>"""

    # ── Networking ────────────────────────────────────────────────────────────
    def _queue_send(self, cot_xml: str):
        with self._lock:
            self._tx_q.append(cot_xml)

    def _run(self):
        """Main loop: connect, send queued events, receive SA updates."""
        while self._running:
            try:
                self._connect()
                self._io_loop()
            except Exception as e:
                log.warning(f"TAK connection error: {e} — reconnecting in 5s")
            finally:
                if self._sock:
                    try: self._sock.close()
                    except: pass
                    self._sock = None
            if self._running:
                time.sleep(5.0)

    def _connect(self):
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.settimeout(10.0)
        self._sock.connect((self._host, self._port))
        self._sock.settimeout(1.0)
        log.info(f"TAK connected to {self._host}:{self._port}")
        # Send initial SA
        self.send_position()

    def _io_loop(self):
        buf = b""
        while self._running:
            # Send queued events
            with self._lock:
                to_send = self._tx_q[:]
                self._tx_q.clear()
            for cot in to_send:
                try:
                    self._sock.sendall(cot.encode("utf-8"))
                except Exception as e:
                    log.warning(f"TAK send error: {e}")
                    return

            # Receive SA events (non-blocking)
            try:
                chunk = self._sock.recv(32768)
                if not chunk:
                    log.info("TAK connection closed by server")
                    return
                buf += chunk
                # Parse complete XML events
                buf = self._parse_rx(buf)
            except socket.timeout:
                pass
            except Exception as e:
                log.warning(f"TAK recv error: {e}")
                return

    def _parse_rx(self, buf: bytes) -> bytes:
        """
        Parse TAK CoT XML from receive buffer.
        CoT events are concatenated XML — we split on </event>.
        """
        while b"</event>" in buf:
            end   = buf.index(b"</event>") + len(b"</event>")
            chunk = buf[:end]
            buf   = buf[end:]
            try:
                self._process_cot(chunk.decode("utf-8", errors="replace"))
            except Exception as e:
                log.debug(f"CoT parse error: {e}")
        return buf

    def _process_cot(self, xml_str: str):
        """Extract position data from received CoT event."""
        try:
            root = ET.fromstring(xml_str)
            ev_type = root.get("type", "")

            # Only process SA tracks (a-f-*, a-h-*, a-n-*, a-u-*)
            if not ev_type.startswith("a-"):
                return

            pt = root.find("point")
            if pt is None:
                return
            lat = float(pt.get("lat", 0))
            lon = float(pt.get("lon", 0))
            hae = float(pt.get("hae", 0))

            detail  = root.find("detail")
            contact = detail.find("contact") if detail is not None else None
            callsign = contact.get("callsign", "") if contact is not None else ""
            if not callsign:
                uid = root.get("uid", "")
                callsign = uid.split("-")[-1]   # best-effort

            if callsign and self.position_callback:
                self.position_callback(callsign, lat, lon, hae)

        except Exception as e:
            log.debug(f"CoT process error: {e}")