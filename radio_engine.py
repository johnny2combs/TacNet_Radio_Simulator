"""
TacNet — Radio Engine (Signal Tracer Edition)
Full diagnostic logging for the entire audio path.
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
import urllib.parse
from typing import Optional, Dict, List, Set, Tuple
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
from radio_effects import RadioEffects, EffectState
from radio_config import ConfigManager, RadioConfig, ChannelDef
from tak_bridge import TAKBridge

log = logging.getLogger("radio.engine")

# ── Jitter Buffer ────────────────────────────────────────────────────
class JitterBuffer:
    def __init__(self, capacity: int = 48000):
        self._capacity = capacity
        self._buffer = np.zeros(capacity, dtype=np.float32)
        self._write_pos = 0
        self._read_pos = 0
        self._count = 0
        self._lock = threading.Lock()
        self._last_put_ts = 0.0

    def put(self, pcm: np.ndarray):
        with self._lock:
            p_len = len(pcm)
            if self._count + p_len > self._capacity:
                # Buffer overflow - force drain
                self._read_pos = (self._read_pos + p_len) % self._capacity
                self._count -= p_len

            end = min(self._write_pos + p_len, self._capacity)
            n1 = end - self._write_pos
            self._buffer[self._write_pos:end] = pcm[:n1]
            if n1 < p_len: self._buffer[0:p_len-n1] = pcm[n1:]
            self._write_pos = (self._write_pos + p_len) % self._capacity
            self._count += p_len
            self._last_put_ts = time.time()

    def get_samples(self, count: int) -> np.ndarray:
        with self._lock:
            out = np.zeros(count, dtype=np.float32)
            take = min(count, self._count)
            if take > 0:
                end = min(self._read_pos + take, self._capacity)
                n1 = end - self._read_pos
                out[:n1] = self._buffer[self._read_pos:end]
                if n1 < take:
                    out[n1:take] = self._buffer[0:take-n1]
                self._read_pos = (self._read_pos + take) % self._capacity
                self._count -= take
            return out

    @property
    def depth(self) -> int:
        return self._count

# ── Receive State ────────────────────────────────────────────────────
class ReceiveState:
    def __init__(self, callsign: str):
        self.callsign = callsign
        self.effect_state = EffectState()
        self.active = False
        self.jb = JitterBuffer()
        self.channel: int = -1  # Feature 4: track which channel this state is for

# ── Radio Engine Core ────────────────────────────────────────────────
class RadioEngine:
    def __init__(self, config_mgr: ConfigManager):
        self._cfg_mgr  = config_mgr
        self._cfg      = self._cfg_mgr.config
        self._effects  = RadioEffects(squelch=0.0) # Disable squelch for testing
        
        self._sock: Optional[socket.socket] = None
        self._running = False
        self._tx_active = False
        self._tx_seq = 0
        self._rx_states: Dict[str, ReceiveState] = {}
        self._rx_lock = threading.Lock()
        
        self._tx_q = queue.Queue(maxsize=100)
        self._voice_q = queue.Queue(maxsize=100)
        
        self._pa_instance = None
        self._output_stream = None
        self._input_stream = None
        
        # UI levels
        self.rx_audio_level = 0.0
        self._tx_level = 0.0
        
        # Channels and Signal Tracking
        self._assigned_channels: Optional[Set[int]] = None
        self._monitored_channels: Set[int] = set()
        self._rx_signal = 0.0
        self._last_link_ts = 0.0
        self._last_rtt_ms = -1
        self._tak = None
        
        # Build codec
        keys = {ch_id: ChannelKey(ch.passphrase, ch_id) for ch_id, ch in self._cfg_mgr.channels.items() if ch.passphrase}
        self._codec = PacketCodec(keys)

        # Callbacks (fully supported stubs)
        self.on_ptt_change = None
        self.on_member_update = None
        self.on_status_change = None
        self.on_rx_start = None
        self.on_rx_end = None
        self.on_position_update = None
        self.on_profile_change = None
        self.on_assigned_channels_change = None
        self.on_channel_activity = None

        self._last_rx_ts = time.time()

    @property
    def jitter_depth(self) -> int:
        with self._rx_lock:
            depths = [int(state.jb._count / FRAME_SAMPLES) for state in self._rx_states.values() if hasattr(state, 'jb')]
            return max(depths) if depths else 0

    def connect(self, assigned_channels=None):
        self._running = True
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.bind(("", 0))
        self._sock.settimeout(0.5)
        
        if assigned_channels:
            self._assigned_channels = set(assigned_channels)
        
        threading.Thread(target=self._rx_loop, daemon=True).start()
        threading.Thread(target=self._tx_worker_loop, daemon=True).start()
        threading.Thread(target=self._voice_worker_loop, daemon=True).start()
        threading.Thread(target=self._keepalive_loop, daemon=True).start()
        
        import pyaudio
        self._pa_instance = pyaudio.PyAudio()
        
        input_dev = None if self._cfg.audio_input_device < 0 else self._cfg.audio_input_device
        output_dev = None if self._cfg.audio_output_device < 0 else self._cfg.audio_output_device
        
        # Open Input
        try:
            self._input_stream = self._pa_instance.open(
                format=pyaudio.paFloat32, channels=1, rate=SAMPLE_RATE, input=True,
                frames_per_buffer=FRAME_SAMPLES,
                input_device_index=input_dev, stream_callback=self._audio_input_cb
            )
            log.info(f"[MIC] Input stream started (dev={input_dev}, buffer={FRAME_SAMPLES})")
        except Exception as e: log.error(f"[MIC] Error: {e}")
            
        # Open Output — Feature 4: always open stereo (channels=2) for spatial audio
        try:
            self._output_stream = self._pa_instance.open(
                format=pyaudio.paFloat32, channels=2, rate=SAMPLE_RATE, output=True,
                frames_per_buffer=FRAME_SAMPLES,
                output_device_index=output_dev, stream_callback=self._audio_output_cb
            )
            log.info(f"[SPEAKER] Stereo output stream started (dev={output_dev}, buffer={FRAME_SAMPLES})")
        except Exception as e:
            log.error(f"[SPEAKER] Stereo output failed, falling back to mono: {e}")
            try:
                self._output_stream = self._pa_instance.open(
                    format=pyaudio.paFloat32, channels=1, rate=SAMPLE_RATE, output=True,
                    frames_per_buffer=FRAME_SAMPLES,
                    output_device_index=output_dev, stream_callback=self._audio_output_cb_mono
                )
                log.info(f"[SPEAKER] Mono output stream started (fallback)")
            except Exception as e2:
                log.error(f"[SPEAKER] Mono fallback also failed: {e2}")

        self._send(Packet(type=PktType.CHANNEL_JOIN, channel=self._cfg.current_channel, callsign=self._cfg.callsign))
        for mon_ch in list(self._monitored_channels):
            self._send(Packet(type=PktType.CHANNEL_JOIN, channel=mon_ch, callsign=self._cfg.callsign))

        # Initialize TAK Bridge if enabled
        if getattr(self._cfg, "tak_enabled", False):
            try:
                server_url = f"tcp://{self._cfg.server_host}:8087"
                self._tak = TAKBridge(
                    server_url=server_url,
                    callsign=self._cfg.callsign,
                    position_callback=self._on_tak_position_update
                )
                self._tak.start()
                log.info("TAK bridge initialized.")
            except Exception as e:
                log.error(f"Failed to start TAK bridge: {e}")

        log.info(f"Engine connected as [{self._cfg.callsign}]")

    def disconnect(self):
        if self._tx_active: 
            self.ptt_off()
        self._send(Packet(type=PktType.CHANNEL_LEAVE, channel=self._cfg.current_channel, callsign=self._cfg.callsign))
        for mon_ch in list(self._monitored_channels):
            self._send(Packet(type=PktType.CHANNEL_LEAVE, channel=mon_ch, callsign=self._cfg.callsign))

        self._running = False
        if self._sock:
            try: self._sock.close()
            except: pass
        if self._input_stream:
            try: self._input_stream.stop_stream(); self._input_stream.close()
            except: pass
        if self._output_stream:
            try: self._output_stream.stop_stream(); self._output_stream.close()
            except: pass
        if self._pa_instance:
            try: self._pa_instance.terminate()
            except: pass
        if self._tak:
            try: self._tak.stop()
            except: pass

        log.info("Engine disconnected.")

    def set_volume(self, val: float):
        self._cfg.output_volume = val
        log.info(f"Volume set to {val:.2f}")

    def restart_tak(self):
        if hasattr(self, "_tak") and self._tak:
            try:
                self._tak.stop()
            except Exception:
                pass
            self._tak = None

        if getattr(self._cfg, "tak_enabled", False):
            try:
                url = getattr(self._cfg, "tak_server_url", "")
                if not url:
                    url = f"tcp://{self._cfg.server_host}:8087"
                elif not url.startswith("tcp://") and not url.startswith("udp://"):
                    url = f"tcp://{url}"
                
                if ":" not in url.replace("://", ""):
                    url = f"{url}:8087"
                
                cs = getattr(self._cfg, "tak_callsign", "") or self._cfg.callsign
                
                self._tak = TAKBridge(
                    server_url=url,
                    callsign=cs,
                    position_callback=self._on_tak_position_update
                )
                self._tak.start()
                log.info(f"TAK bridge restarted (url={url}, callsign={cs})")
            except Exception as e:
                log.error(f"Failed to restart TAK bridge: {e}")

    def push_config_to_server(self):
        import urllib.request
        import json
        try:
            web_port = getattr(self._cfg, "web_admin_port", 8890)
            url = f"http://{self._cfg.server_host}:{web_port}/api/callsigns/{self._cfg.callsign}"
            
            lat, lon, alt = self._cfg_mgr.get_position()
            body = {
                "callsign": self._cfg.callsign,
                "radio_profile": self._cfg.radio_profile,
                "ptt_key": self._cfg.ptt_key,
                "lat": lat,
                "lon": lon,
                "alt": alt,
                "channels": list(self._assigned_channels) if self._assigned_channels is not None else []
            }
            
            data = json.dumps(body).encode('utf-8')
            req = urllib.request.Request(
                url, 
                data=data, 
                headers={'Content-Type': 'application/json'},
                method='POST'
            )
            with urllib.request.urlopen(req, timeout=2.0) as response:
                res = json.loads(response.read().decode())
                log.info(f"Pushed config to server: {res}")
        except Exception as e:
            log.warning(f"Could not push config to server: {e}")

    def change_channel(self, new_channel: int, force: bool = False):
        if not force and self._assigned_channels is not None and new_channel not in self._assigned_channels:
            log.warning(f"Channel change blocked - no access to CH{new_channel:02d}")
            return
        if new_channel == self._cfg.current_channel:
            return
        was_tx = self._tx_active
        if was_tx:
            self.ptt_off()
            
        self._send(Packet(type=PktType.CHANNEL_LEAVE, channel=self._cfg.current_channel, callsign=self._cfg.callsign))
        self._cfg.current_channel = new_channel
        # Bug 3 fix: do NOT discard new_channel from _monitored_channels here.
        # If the user was monitoring a channel and switches to it as primary TX,
        # then switches back to their original channel, monitoring should be preserved.
        # The _rx_loop filter handles primary vs monitored correctly without this discard.
        
        # Flush jitter buffers & receive states on channel switch
        with self._rx_lock:
            self._rx_states.clear()
            
        self._rx_signal = 0.0
        self._send(Packet(type=PktType.CHANNEL_JOIN, channel=new_channel, callsign=self._cfg.callsign))
        
        log.info(f"Primary channel switched to CH{new_channel:02d}")

    def monitor_channel(self, channel: int):
        self._monitored_channels.add(channel)
        self._send(Packet(type=PktType.CHANNEL_JOIN, channel=channel, callsign=self._cfg.callsign))
        log.info(f"Monitoring CH{channel:02d} (receive-only)")

    def unmonitor_channel(self, channel: int):
        self._monitored_channels.discard(channel)
        self._send(Packet(type=PktType.CHANNEL_LEAVE, channel=channel, callsign=self._cfg.callsign))
        log.info(f"Stopped monitoring CH{channel:02d}")

    def rebuild_codec(self):
        if not hasattr(self, '_channel_keys_cache'):
            self._channel_keys_cache = {}
            
        new_keys = {}
        for ch_id, ch in self._cfg_mgr.channels.items():
            if ch.passphrase:
                cached_key = self._channel_keys_cache.get(ch_id)
                if cached_key and getattr(cached_key, '_passphrase', None) == ch.passphrase:
                    new_keys[ch_id] = cached_key
                else:
                    new_key = ChannelKey(ch.passphrase, ch_id)
                    self._channel_keys_cache[ch_id] = new_key
                    new_keys[ch_id] = new_key
                    
        for ch_id in list(self._channel_keys_cache.keys()):
            if ch_id not in new_keys:
                self._channel_keys_cache.pop(ch_id, None)
                
        self._codec = PacketCodec(new_keys)
        log.info("Codec keys rebuilt.")

    def _on_tak_position_update(self, cs, lat, lon, alt):
        if self.on_position_update:
            self.on_position_update(cs, lat, lon, alt)

    def _send(self, pkt: Packet):
        if not self._sock: return
        try:
            data = self._codec.encode(pkt)
            self._sock.sendto(data, (self._cfg.server_host, self._cfg.server_port))
        except: pass

    def ptt_on(self):
        self._tx_active = True
        self._send(Packet(type=PktType.PTT_ON, channel=self._cfg.current_channel, callsign=self._cfg.callsign))
        if self.on_ptt_change: self.on_ptt_change(True)
        if self._tak:
            self._tak.send_ptt_on(self._cfg.current_channel)
        log.info("[PTT] ON")

        # Local mic click/sidetone to confirm TX
        try:
            pt = getattr(self._cfg, 'ptt_type', 'standard')
            if pt != "none" and getattr(self._cfg, 'ptt_click_enabled', True):
                import winsound
                import threading
                
                def _play_click():
                    ch_def = self._cfg_mgr.channels.get(self._cfg.current_channel)
                    comsec_encrypted = ch_def.encrypted if ch_def else False
                    sim_mode_active = (self._effects.distortion > 0 or self._effects.noise_floor > 0)
                    
                    if comsec_encrypted:
                        winsound.Beep(2000, 30)
                        winsound.Beep(2800, 40)
                        winsound.Beep(2400, 50)
                    elif pt == "heavy":
                        winsound.Beep(600, 100)  # Low thud
                    elif pt == "digital":
                        winsound.Beep(3200, 40)  # High chirp
                    else: # standard
                        f1 = getattr(self._cfg, 'ptt_tone_f1', 1200.0)
                        winsound.Beep(max(37, int(f1)), 60)
                        
                threading.Thread(target=_play_click, daemon=True).start()
        except Exception as e:
            log.debug(f"Failed to play PTT ON sidetone: {e}")

    def ptt_off(self):
        self._tx_active = False
        self._send(Packet(type=PktType.PTT_OFF, channel=self._cfg.current_channel, callsign=self._cfg.callsign))
        if self.on_ptt_change: self.on_ptt_change(False)
        if self._tak:
            self._tak.send_ptt_off(self._cfg.current_channel)
        log.info("[PTT] OFF")

        # Local mic click/sidetone on release
        try:
            pt = getattr(self._cfg, 'ptt_type', 'standard')
            if pt != "none" and getattr(self._cfg, 'ptt_click_enabled', True):
                import winsound
                import threading
                
                def _play_release():
                    ch_def = self._cfg_mgr.channels.get(self._cfg.current_channel)
                    comsec_encrypted = ch_def.encrypted if ch_def else False
                    sim_mode_active = (self._effects.distortion > 0 or self._effects.noise_floor > 0)
                    
                    if comsec_encrypted:
                        winsound.Beep(2400, 30)
                        winsound.Beep(1800, 40)
                    elif pt == "heavy":
                        winsound.Beep(450, 80)
                    elif pt == "digital":
                        winsound.Beep(2800, 30)
                    else: # standard
                        f2 = getattr(self._cfg, 'ptt_tone_f2', 1600.0)
                        winsound.Beep(max(37, int(f2)), 50)
                        
                threading.Thread(target=_play_release, daemon=True).start()
        except Exception as e:
            log.debug(f"Failed to play PTT OFF sidetone: {e}")

    def _rx_loop(self):
        while self._running:
            try:
                data, addr = self._sock.recvfrom(65507)
                if not data:
                    continue
                pkt = self._codec.decode(data)
                if pkt:
                    self._last_rx_ts = time.time()
                    self._last_link_ts = time.time()
                    
                    if pkt.callsign == self._cfg.callsign:
                        continue
                        
                    if pkt.type == PktType.VOICE:
                        # Only put in voice queue if channel matches primary or monitored channels
                        if pkt.channel == self._cfg.current_channel or pkt.channel in self._monitored_channels:
                            self._voice_q.put(pkt)
                            
                    elif pkt.type == PktType.PTT_ON:
                        log.info(f"[NET] RX START from {pkt.callsign}")
                        if self.on_rx_start:
                            self.on_rx_start(pkt.callsign)
                        with self._rx_lock:
                            state = self._rx_states.setdefault(pkt.callsign, ReceiveState(pkt.callsign))
                            state.active = True
                            
                    elif pkt.type == PktType.PTT_OFF:
                        log.info(f"[NET] RX END from {pkt.callsign}")
                        if self.on_rx_end:
                            self.on_rx_end(pkt.callsign)
                        with self._rx_lock:
                            if pkt.callsign in self._rx_states:
                                self._rx_states[pkt.callsign].active = False
                                
                    elif pkt.type == PktType.MEMBER_LIST:
                        try:
                            members = Payloads.parse_member_list(pkt.payload)
                            if self.on_member_update:
                                self.on_member_update(members)
                            if self.on_status_change:
                                self.on_status_change("Connected")
                        except Exception as e:
                            log.error(f"Error parsing member list: {e}")
                            
                    elif pkt.type == PktType.PONG:
                        if getattr(pkt, 'decrypt_failed', False):
                            self._last_rtt_ms = -1
                            continue
                        try:
                            rtt = int(time.time()*1000) - struct.unpack_from("!Q", pkt.payload, 0)[0]
                            self._last_rtt_ms = rtt
                            if self.on_status_change:
                                self.on_status_change("Connected")
                        except Exception as e:
                            log.error(f"Error parsing RTT: {e}")
                            
                    elif pkt.type == PktType.POSITION_UPDATE:
                        try:
                            lat, lon, alt = Payloads.parse_position(pkt.payload)
                            if self.on_position_update:
                                self.on_position_update(pkt.callsign, lat, lon, alt)
                        except Exception as e:
                            log.error(f"Error parsing position: {e}")
                            
                    elif pkt.type == PktType.FORCE_CHANNEL:
                        new_ch = pkt.channel
                        log.info(f"SERVER COMMAND: Force switch to CH{new_ch:02d}")
                        self.change_channel(new_ch, force=True)
                        
            except socket.timeout:
                continue
            except Exception as e:
                log.debug(f"Rx loop error: {e}")

    def _audio_input_cb(self, in_data, frame_count, time_info, status):
        if self._tx_active:
            pcm = np.frombuffer(in_data, dtype=np.float32) * self._cfg.input_gain
            rms = float(np.sqrt(np.mean(pcm**2)))
            self._tx_level = 0.8 * self._tx_level + 0.2 * rms
            
            self._tx_q.put(pcm.copy())
        else:
            self._tx_level = 0.0
        return (None, 0) # paContinue

    def _tx_worker_loop(self):
        while self._running:
            try:
                pcm = self._tx_q.get(timeout=0.1)
                data, flags = encode_audio(pcm)
                self._send(Packet(type=PktType.VOICE, channel=self._cfg.current_channel, callsign=self._cfg.callsign, seq=self._tx_seq, flags=flags, payload=data))
                self._tx_seq += 1
                if self._tx_seq % 50 == 0:
                    log.info(f"[NET] Sent 50 voice packets")
            except: pass

    def _voice_worker_loop(self):
        while self._running:
            try:
                pkt = self._voice_q.get(timeout=0.1)
                with self._rx_lock:
                    state = self._rx_states.setdefault(pkt.callsign, ReceiveState(pkt.callsign))
                    state.channel = pkt.channel  # Feature 4: track source channel
                
                payload = pkt.payload
                flags = pkt.flags
                
                # Signal level parsing (if payload begins with it)
                signal = 1.0
                sig_id = 0.0
                if flags & 0x80:
                    if len(payload) >= 8:
                        signal, sig_id = struct.unpack_from("!ff", payload, 0)
                        payload = payload[8:]
                    elif len(payload) >= 4:
                        signal = struct.unpack_from("!f", payload, 0)[0]
                        payload = payload[4:]
                    flags &= ~0x80

                # Feature 4: Collision severity header (FLAG_COLLISION = 0x40)
                collision_severity = 0.0
                if flags & 0x40:
                    if len(payload) >= 4:
                        collision_severity = struct.unpack_from("!f", payload, 0)[0]
                        payload = payload[4:]
                    flags &= ~0x40
                
                self._rx_signal = 0.7 * self._rx_signal + 0.3 * signal
                self._last_link_ts = time.time()
                
                if self.on_channel_activity:
                    self.on_channel_activity(pkt.channel, True)
                
                pcm = decode_audio(payload, flags)
                ch_def = self._cfg_mgr.channels.get(pkt.channel)
                comsec_encrypted = ch_def.encrypted if ch_def else False
                waveform_type = getattr(ch_def, 'waveform_type', 'Narrowband') if ch_def else 'Narrowband'
                eccm_mode = getattr(ch_def, 'eccm_mode', '').upper() if ch_def else ''

                # Feature 2: Detect COMSEC mismatch (decryption failure or unxepected encryption)
                comsec_mismatch = False
                if getattr(pkt, 'decrypt_failed', False):
                    comsec_mismatch = True
                    pcm = np.zeros(len(pcm), dtype=np.float32)  # silence base
                elif comsec_encrypted and not getattr(pkt, 'was_encrypted', True):
                    # Channel expects encryption but packet arrived unencrypted
                    comsec_mismatch = True
                    pcm = np.zeros(len(pcm), dtype=np.float32)

                processed = self._effects.process_rx(
                    pcm, signal, state.effect_state, bool(flags & FLAG_END_OF_TX),
                    sig_id=sig_id, comsec_encrypted=comsec_encrypted,
                    collision_severity=collision_severity,
                    waveform_type=waveform_type, eccm_mode=eccm_mode,
                    comsec_mismatch=comsec_mismatch,
                )
                state.jb.put(processed)
                
                if pkt.seq % 50 == 0:
                    log.info(f"[NET] Received 50 voice packets from {pkt.callsign}")

            except: pass

    def _audio_output_cb(self, in_data, frame_count, time_info, status):
        """
        Stereo output callback. Feature 4 — split-ear spatial audio:
          Primary channel  → Left ear only
          Monitored channels → Right ear only
          Other/unknown → both ears (attenuated by 0.707 constant-power)
        """
        primary_ch = self._cfg.current_channel
        monitored = self._monitored_channels
        
        # Robust boolean coercion to handle string configs gracefully
        spatial_val = getattr(self._cfg, 'spatial_audio', False)
        spatial = spatial_val in (True, 'True', 'true', '1', 1)

        out_left  = np.zeros(frame_count, dtype=np.float32)
        out_right = np.zeros(frame_count, dtype=np.float32)
        is_receiving = False

        with self._rx_lock:
            for state in self._rx_states.values():
                samples = state.jb.get_samples(frame_count)
                if getattr(state, 'active', False):
                    is_receiving = True

                if spatial:
                    src_ch = getattr(state, 'channel', -1)
                    if src_ch == primary_ch:
                        out_left  += samples                  # Primary → Left
                    elif src_ch in monitored:
                        out_right += samples                  # Monitor → Right
                    else:
                        out_left  += samples * 0.707          # Unknown → both (constant power)
                        out_right += samples * 0.707
                else:
                    out_left  += samples
                    out_right += samples

        # Add background static hiss if noise floor is set (e.g. from jamming)
        if self._effects.noise_floor > 0.0 and not self._tx_active:
            hiss = np.random.normal(0.0, self._effects.noise_floor * 0.1, frame_count).astype(np.float32)
            out_left += hiss
            out_right += hiss

        # Volume + clip
        vol = self._cfg.output_volume
        out_left  = np.clip(out_left  * vol, -0.99, 0.99).astype(np.float32)
        out_right = np.clip(out_right * vol, -0.99, 0.99).astype(np.float32)

        # RMS metering
        rms = float(np.sqrt(np.mean(out_left**2 + out_right**2) / 2.0))
        self.rx_audio_level = 0.5 * self.rx_audio_level + 0.5 * rms

        # Interleave L/R into stereo buffer [L0, R0, L1, R1, ...]
        stereo = np.empty(frame_count * 2, dtype=np.float32)
        stereo[0::2] = out_left
        stereo[1::2] = out_right
        return (stereo.tobytes(), 0)  # paContinue

    def _audio_output_cb_mono(self, in_data, frame_count, time_info, status):
        """Mono fallback output callback (used if stereo open failed)."""
        mixed = np.zeros(frame_count, dtype=np.float32)
        is_receiving = False
        with self._rx_lock:
            for state in self._rx_states.values():
                mixed += state.jb.get_samples(frame_count)
                if getattr(state, 'active', False):
                    is_receiving = True
        
        # Add background static hiss if noise floor is set
        if self._effects.noise_floor > 0.0 and not self._tx_active:
            hiss = np.random.normal(0.0, self._effects.noise_floor * 0.1, frame_count).astype(np.float32)
            mixed += hiss

        rms = float(np.sqrt(np.mean(mixed**2)))
        self.rx_audio_level = 0.5 * self.rx_audio_level + 0.5 * rms
        out = np.clip(mixed * self._cfg.output_volume, -0.99, 0.99).astype(np.float32)
        return (out.tobytes(), 0)

    def _keepalive_loop(self):
        ping_count = 0
        while self._running:
            ping_count += 1
            # Send Ping
            self._send(Packet(type=PktType.PING, channel=self._cfg.current_channel, callsign=self._cfg.callsign, payload=struct.pack("!Q", int(time.time()*1000))))
            
            # Send Position every 15 seconds (every 3rd ping)
            if ping_count % 3 == 0:
                lat, lon, alt = self._cfg_mgr.get_position()
                self._send(Packet(type=PktType.POSITION_UPDATE, channel=self._cfg.current_channel,
                                  callsign=self._cfg.callsign, payload=Payloads.position(lat, lon, alt)))
                if self._tak:
                    self._tak.update_position(lat, lon, alt)
                    self._tak.send_position()
            
            time.sleep(5)
