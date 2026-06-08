"""
MilRadio — Protocol Layer
"""
from __future__ import annotations
import struct
import time
import logging
import hashlib
import os
from enum import IntEnum
from dataclasses import dataclass, field
from typing import Optional, Dict

import numpy as np
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes

log = logging.getLogger("radio.protocol")

# ── Constants ─────────────────────────────────────────────────────────────────
MAGIC = b"MILR"
VERSION = 1
SAMPLE_RATE = 48000
FRAME_MS = 20
FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000
CALLSIGN_LEN = 16
HEADER_FMT = "!4sBBHIQ16sBH"
HEADER_SIZE = struct.calcsize(HEADER_FMT)
HEADER_AAD_FMT = "!4sBBHIQ16sB"
HEADER_AAD_SIZE = struct.calcsize(HEADER_AAD_FMT)
TAG_SIZE = 16
PBKDF2_ITERS = 200_000
PBKDF2_SALT = b"MilRadio-v1-NetKey-Salt-2025"

# ── Packet types ──────────────────────────────────────────────────────────────
class PktType(IntEnum):
    VOICE = 0x01
    PTT_ON = 0x02
    PTT_OFF = 0x03
    CHANNEL_JOIN = 0x10
    CHANNEL_LEAVE = 0x11
    MONITOR_JOIN = 0x13      # FIXED: Added for monitor mode
    MONITOR_LEAVE = 0x14     # FIXED: Added for monitor mode (was missing!)
    MEMBER_LIST = 0x12
    POSITION_UPDATE = 0x20
    RADIO_CHECK = 0x21
    RADIO_CHECK_ACK = 0x22
    FORCE_CHANNEL = 0x23
    PING = 0xF0
    PONG = 0xF1
    ERROR = 0xFE

# ── Packet flags ──────────────────────────────────────────────────────────────
FLAG_ENCRYPTED = 0x01
FLAG_COMPRESSED = 0x02
FLAG_OPUS = 0x04
FLAG_END_OF_TX = 0x08

# ── Codec detection ───────────────────────────────────────────────────────────
_OPUS_AVAILABLE = False
_opus_enc = None
_opus_dec = None

def _init_opus():
    global _OPUS_AVAILABLE, _opus_enc, _opus_dec
    try:
        import os
        import ctypes
        import ctypes.util

        # Hook find_library to fallback to direct file probing if system tools fail
        orig_find_library = ctypes.util.find_library
        def new_find_library(name):
            res = orig_find_library(name)
            if res:
                return res
            if name == 'opus':
                for path in [
                    'libopus.so.0',
                    'libopus.so',
                    '/usr/lib/aarch64-linux-gnu/libopus.so.0',
                    '/usr/lib/arm-linux-gnueabihf/libopus.so.0',
                    '/usr/lib/x86_64-linux-gnu/libopus.so.0',
                    '/usr/lib/libopus.so.0',
                    '/usr/local/lib/libopus.so.0',
                    'libopus-0.dll',
                    'opus.dll',
                ]:
                    try:
                        ctypes.CDLL(path)
                        return path
                    except Exception:
                        pass
            return None
        ctypes.util.find_library = new_find_library

        if os.name == 'nt':
            # Prepend script directory to PATH and DLL search paths on Windows
            current_dir = os.path.dirname(os.path.abspath(__file__))
            os.environ['PATH'] = current_dir + os.pathsep + os.environ.get('PATH', '')
            if hasattr(os, 'add_dll_directory'):
                try:
                    os.add_dll_directory(current_dir)
                except Exception:
                    pass

        import opuslib
        _opus_enc = opuslib.Encoder(SAMPLE_RATE, 1, opuslib.APPLICATION_VOIP)
        _opus_dec = opuslib.Decoder(SAMPLE_RATE, 1)
        _opus_enc.bitrate = 24000
        _opus_enc.vbr = True
        _opus_enc.complexity = 10
        _OPUS_AVAILABLE = True
        log.info("Opus codec available — using 24 kbps Opus")
    except Exception as e:
        log.info(f"Opus not available ({e}) — using μ-law G.711 fallback")

_init_opus()

# ── μ-law codec ───────────────────────────────────────────────────────────────
def _ulaw_encode(pcm_f32: np.ndarray) -> bytes:
    mu = 255.0
    x = np.clip(pcm_f32, -1.0, 1.0)
    enc = np.sign(x) * np.log1p(mu * np.abs(x)) / np.log1p(mu)
    return ((enc * 127.0) + 127.5).astype(np.uint8).tobytes()

def _ulaw_decode(data: bytes) -> np.ndarray:
    mu = 255.0
    arr = np.frombuffer(data, dtype=np.uint8).astype(np.float32)
    x = (arr - 127.5) / 127.0
    return np.sign(x) * (np.expm1(np.abs(x) * np.log1p(mu)) / mu)

# ── Public codec API ──────────────────────────────────────────────────────────
def encode_audio(pcm_f32: np.ndarray) -> tuple:
    if _OPUS_AVAILABLE:
        pcm_i16 = (np.clip(pcm_f32, -1.0, 1.0) * 32767).astype(np.int16).tobytes()
        try:
            encoded = _opus_enc.encode(pcm_i16, FRAME_SAMPLES)
            return encoded, FLAG_COMPRESSED | FLAG_OPUS
        except Exception:
            pass
    return _ulaw_encode(pcm_f32), FLAG_COMPRESSED

def decode_audio(data: bytes, flags: int) -> np.ndarray:
    if flags & FLAG_OPUS and _OPUS_AVAILABLE:
        try:
            pcm_bytes = _opus_dec.decode(data, FRAME_SAMPLES)
            return np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32) / 32767.0
        except Exception:
            pass
    if flags & FLAG_COMPRESSED:
        return _ulaw_decode(data)
    return np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32767.0

# ── Key management ────────────────────────────────────────────────────────────
class ChannelKey:
    def __init__(self, passphrase: str, channel: int):
        self.channel = channel
        self._passphrase = passphrase
        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=32,
            salt=PBKDF2_SALT + struct.pack("!H", channel),
            iterations=PBKDF2_ITERS,
        )
        raw_key = kdf.derive(passphrase.encode("utf-8"))
        self._aes = AESGCM(raw_key)
        self._fingerprint = hashlib.sha256(raw_key).hexdigest()[:12]
        log.debug(f"Channel {channel} key fingerprint: {self._fingerprint}")
    
    @property
    def fingerprint(self) -> str:
        return self._fingerprint
    
    def _nonce(self, seq: int, ts_ms: int) -> bytes:
        raw = struct.pack("!IQ", seq, ts_ms)
        return hashlib.sha256(raw).digest()[:12]
    
    def encrypt(self, plaintext: bytes, aad: bytes, seq: int, ts_ms: int) -> bytes:
        nonce = self._nonce(seq, ts_ms)
        return self._aes.encrypt(nonce, plaintext, aad)
    
    def decrypt(self, ciphertext_with_tag: bytes, aad: bytes, seq: int, ts_ms: int) -> bytes:
        nonce = self._nonce(seq, ts_ms)
        return self._aes.decrypt(nonce, ciphertext_with_tag, aad)

# ── Packet dataclass ──────────────────────────────────────────────────────────
@dataclass
class Packet:
    type: PktType
    channel: int = 0
    seq: int = 0
    ts_ms: int = field(default_factory=lambda: int(time.time() * 1000))
    callsign: str = ""
    flags: int = 0
    payload: bytes = b""
    signal_strength: float = 1.0
    sender_addr: tuple = field(default=None, compare=False, repr=False)
    # Feature 2: COMSEC mismatch / screech detection
    decrypt_failed: bool = False   # True if decryption was attempted but failed
    was_encrypted:  bool = False   # True if packet had FLAG_ENCRYPTED set
    
    @property
    def callsign_bytes(self) -> bytes:
        return self.callsign.encode("ascii", errors="replace")[:CALLSIGN_LEN].ljust(CALLSIGN_LEN, b"\x00")
    
    @staticmethod
    def _cs_from_bytes(b: bytes) -> str:
        return b.rstrip(b"\x00").decode("ascii", errors="replace")

# ── Serialisation ─────────────────────────────────────────────────────────────
class PacketCodec:
    def __init__(self, keys: Optional[Dict[int, ChannelKey]] = None):
        self._keys: Dict[int, ChannelKey] = keys or {}
        self._seq = 0
    
    def add_key(self, channel: int, key: ChannelKey):
        self._keys[channel] = key
    
    def remove_key(self, channel: int):
        self._keys.pop(channel, None)
    
    def _next_seq(self) -> int:
        self._seq = (self._seq + 1) & 0xFFFFFFFF
        return self._seq
    
    def encode(self, pkt: Packet) -> bytes:
        if pkt.seq == 0:
            pkt.seq = self._next_seq()
        if pkt.ts_ms == 0:
            pkt.ts_ms = int(time.time() * 1000)
        
        callsign_b = pkt.callsign_bytes
        payload = pkt.payload
        flags = pkt.flags
        key = self._keys.get(pkt.channel)
        
        header_without_len = struct.pack(
            "!4sBBHIQ16sB",
            MAGIC, VERSION, int(pkt.type),
            pkt.channel, pkt.seq, pkt.ts_ms,
            callsign_b, flags,
        )
        
        if key and payload:
            flags |= FLAG_ENCRYPTED
            aad = struct.pack(
                "!4sBBHIQ16sB",
                MAGIC, VERSION, int(pkt.type),
                pkt.channel, pkt.seq, pkt.ts_ms,
                callsign_b, flags,
            )
            payload = key.encrypt(payload, aad, pkt.seq, pkt.ts_ms)
            header_without_len = aad
        
        full_header = header_without_len + struct.pack("!H", len(payload))
        return full_header + payload
    
    def decode(self, data: bytes) -> Optional[Packet]:
        min_size = HEADER_SIZE
        if len(data) < min_size:
            return None
        
        try:
            magic, ver, ptype, channel, seq, ts_ms, cs_b, flags, plen = struct.unpack_from(
                HEADER_FMT, data, 0
            )
        except struct.error:
            return None
        
        if magic != MAGIC or ver != VERSION:
            return None
        
        payload_start = HEADER_SIZE
        payload_end = payload_start + plen
        if len(data) < payload_end:
            return None
        
        payload = data[payload_start:payload_end]
        
        # Feature 2: COMSEC mismatch/screech detection
        pkt_was_encrypted = bool(flags & FLAG_ENCRYPTED)
        pkt_decrypt_failed = False
        
        if flags & FLAG_ENCRYPTED:
            key = self._keys.get(channel)
            if key is None:
                log.warning(f"Received encrypted packet on ch{channel} but no key loaded")
                # Feature 2: Return packet with decrypt_failed so engine can play screech
                pkt_decrypt_failed = True
                payload = bytes(len(payload))  # zeroed payload
                flags &= ~FLAG_ENCRYPTED
            else:
                aad = data[:HEADER_AAD_SIZE]
                try:
                    payload = key.decrypt(payload, aad, seq, ts_ms)
                    flags &= ~FLAG_ENCRYPTED
                except Exception:
                    log.warning(f"Decryption failed on ch{channel} seq={seq} — wrong key or tampered")
                    # Feature 2: Return packet with decrypt_failed instead of None
                    pkt_decrypt_failed = True
                    payload = bytes(FRAME_SAMPLES * 2)  # zeroed: 960 samples × 2 bytes (i16 silence)
                    flags &= ~FLAG_ENCRYPTED
        
        try:
            ptype_e = PktType(ptype)
        except ValueError:
            ptype_e = PktType.ERROR
        
        return Packet(
            type=ptype_e,
            channel=channel,
            seq=seq,
            ts_ms=ts_ms,
            callsign=Packet._cs_from_bytes(cs_b),
            flags=flags,
            payload=payload,
            decrypt_failed=pkt_decrypt_failed,
            was_encrypted=pkt_was_encrypted,
        )

# ── Payload helpers ───────────────────────────────────────────────────────────
class Payloads:
    @staticmethod
    def position(lat: float, lon: float, alt: float = 0.0) -> bytes:
        return struct.pack("!ddd", lat, lon, alt)
    
    @staticmethod
    def parse_position(data: bytes) -> tuple:
        return struct.unpack("!ddd", data[:24])
    
    @staticmethod
    def member_list(members: list) -> bytes:
        out = struct.pack("!H", len(members))
        for m in members:
            cs = m.get("callsign", "").encode("ascii", "replace")[:16].ljust(16, b"\x00")
            ch = m.get("channel", 0)
            if not isinstance(ch, int) or ch < 0 or ch > 65535:
                ch = 0
            lat = m.get("lat", 0.0)
            lon = m.get("lon", 0.0)
            sig = m.get("signal", 1.0)
            out += struct.pack("!16sHddf", cs, ch, lat, lon, sig)
        return out
    
    @staticmethod
    def parse_member_list(data: bytes) -> list:
        count = struct.unpack_from("!H", data, 0)[0]
        out = []
        off = 2
        entry_size = struct.calcsize("!16sHddf")
        for _ in range(count):
            if off + entry_size > len(data):
                break
            cs_b, ch, lat, lon, sig = struct.unpack_from("!16sHddf", data, off)
            out.append({
                "callsign": cs_b.rstrip(b"\x00").decode("ascii", "replace"),
                "channel": ch,
                "lat": lat,
                "lon": lon,
                "signal": sig,
            })
            off += entry_size
        return out
    
    @staticmethod
    def radio_check(callsign: str) -> bytes:
        return callsign.encode("ascii", "replace")[:16].ljust(16, b"\x00")
    
    @staticmethod
    def error(message: str) -> bytes:
        return message.encode("utf-8")[:128]

if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)
    print("✓ Protocol module loaded")
    print(f"  Opus available: {_OPUS_AVAILABLE}")
    print(f"  Packet types: {len(PktType)}")