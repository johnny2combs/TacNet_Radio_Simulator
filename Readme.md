# MilRadio — Military Network Radio Simulator

Networked voice radio simulator with military radio effects, range simulation,
After Action Review (AAR) recording, and integration with TAKServer and MASA
SWORD via SimBridge.

---

## Quick Start (Deployed Build)

1. Run `BUILD.bat` once to produce the `dist/MilRadio/` folder
2. On the **server machine**: run `MilRadio-ServerTray.exe` — appears in system tray
3. On **each operator PC**: run `MilRadio.exe` — configure callsign + server IP
4. Open the **web admin UI**: `http://<server-ip>:8890`

---

## Quick Start (Development)

```
pip install -r requirements.txt
python radio_web.py          # starts server + web admin on :8890
python radio_client.py       # operator client (run one per operator)
```

---

## Architecture

```
SWORD ──► SimBridge ──► /api/sword/status (proxy) ─┐
TAKServer ◄──────────────────► tak_bridge.py        │
                                                    ▼
Operator A ──UDP──► radio_server.py ──UDP──► Operator B
                         │
                    radio_recorder.py (AAR)
                         │
                    aar/<session_id>/
                      manifest.json
                      tx_0001_OA_CH00.wav
                      ...
```

---

## Files

| File | Purpose |
|------|---------|
| `radio_server.py` | UDP/TCP relay server — voice routing + range attenuation |
| `radio_web.py` | HTTP admin server on :8890 — REST API + web UI |
| `radio_recorder.py` | AAR session recorder — WAV capture + HTML report generation |
| `radio_client.py` | Operator GUI client |
| `radio_protocol.py` | Packet format, Opus codec, encryption |
| `radio_effects.py` | DSP: bandpass filter, noise, squelch, distortion |
| `radio_config.py` | Config persistence, channel plans, callsigns |
| `tak_bridge.py` | TAK/CoT position sync |
| `sword_bridge.py` | MASA SWORD position sync via SimBridge |
| `radio_admin.html` | Web admin single-page app (8 tabs) |
| `milradio_aar_v2.html` | Standalone AAR report template |
| `radio_config.ini` | Configuration (edit before first run) |
| `BUILD.bat` | PyInstaller build script → `dist/MilRadio/` |

---

## Web Admin Tabs

| Tab | Function |
|-----|----------|
| Dashboard | Server status, connected clients, live stats |
| Comms Plan | Channel diagram, export/import JSON |
| Channels | Add/edit radio channels (frequency, encryption) |
| Callsigns | Callsign register with unit/role/position |
| Config & Range | Server config, range simulation settings |
| Audio & Effects | DSP chain configuration |
| **AAR** | Session recording, live TX log, past sessions + reports |
| Server Log | Ring-buffer server log |

---

## AAR (After Action Review)

### Recording
1. Open admin UI → **AAR** tab
2. Set exercise name and time source (Wall Clock / Manual H-Hour / SWORD)
3. Click **START RECORDING**
4. All voice transmissions are captured as individual WAV files
5. Click **STOP RECORDING**

### Reports
- Click **VIEW REPORT** to open the generated HTML report in a new browser tab
- Click **SAVE HTML** to download a standalone copy
- Reports include: timeline, TX log with playback, participant summary, channel activity
- Audio playback works when served by the MilRadio server (click any row or ▶ button)

### Time Sources
| Mode | Behaviour |
|------|-----------|
| Wall Clock | Exercise time = PC wall clock time |
| Manual H-Hour | Enter H-hour manually; exercise time = wall − H-hour |
| SWORD / SimBridge | H-hour derived from SimBridge sim_clock; live sim time shown in admin |

### Session Folders
```
aar/
  20260317_102459/
    manifest.json          ← session metadata + TX index
    tx_0001_OA_CH00.wav    ← individual transmission audio
    tx_0002_OB_CH03.wav
    ...
```

---

## Configuration (`radio_config.ini`)

Key settings:
```ini
[server]
bind_address = 0.0.0.0
server_port  = 55500

[web]
web_admin_port = 8890

[aar]
aar_enabled      = true
aar_dir          = aar
aar_time_source  = wall    ; wall | manual | sword
aar_sword_url    = http://localhost:8888
```

---

## Build

`BUILD.bat` produces `dist/MilRadio/` containing:

| File | Notes |
|------|-------|
| `MilRadio-ServerTray.exe` | Server with system tray icon — run this on server machine |
| `MilRadio-Server.exe` | Console server (for debugging) |
| `MilRadio.exe` | Operator client |
| `radio_config.ini` | Edit before deployment |
| `radio_admin.html` | Admin UI — can be updated without rebuilding |
| `milradio_aar_v2.html` | AAR report template — can be updated without rebuilding |

> **Tip:** `radio_admin.html` and `milradio_aar_v2.html` are loaded from disk at
> runtime. You can update them by replacing the files in `dist/MilRadio/` without
> running a full rebuild.

---

## Requirements

- Python 3.12+
- Windows 10/11 (client audio via PyAudio/pynput)
- Server can run on any platform Python supports