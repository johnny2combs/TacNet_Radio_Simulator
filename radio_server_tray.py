"""
MilRadio — Server Tray
======================
System-tray launcher for the MilRadio relay + web-admin server.

Runs radio_web.py as a managed subprocess, sits in the notification area,
and provides a right-click menu for common tasks.

Tray icon colour:
  Green  — server running, at least one client connected
  Amber  — server running, no clients
  Red    — server stopped / crashed

Requirements:
    pip install pystray pillow

Usage:
    pythonw radio_server_tray.py     (background, no console — recommended)
    python  radio_server_tray.py     (shows console — useful for debugging)

Command-line options (passed through to radio_web.py):
    --config  PATH     config file  (default: radio_config.ini)
    --web-port PORT    admin UI port (default: 8890)
    --port    PORT     UDP voice port override
    --host    ADDR     bind address override
    --debug            enable debug logging in server
"""

from __future__ import annotations
import sys
import os

# ── PyInstaller freeze support — must be first ────────────────────────────────
if __name__ == "__main__":
    import multiprocessing
    multiprocessing.freeze_support()

# ── Single-instance lockfile — must be before any other imports ──────────────
# Open a file with exclusive write access. On Windows, a file opened with
# msvcrt.locking() cannot be locked by a second process — no networking,
# no firewall prompts, no privilege required.
import tempfile as _tempfile
_lock_file = None
_lock_fd   = None
try:
    _lock_path = _tempfile.gettempdir() + "\\milradio_tray.lock"
    if sys.platform != "win32":
        _lock_path = _tempfile.gettempdir() + "/milradio_tray.lock"
    _lock_fd = open(_lock_path, "w")
    if sys.platform == "win32":
        import msvcrt as _msvcrt
        try:
            _msvcrt.locking(_lock_fd.fileno(), _msvcrt.LK_NBLCK, 1)
        except OSError:
            _lock_fd.close()
            sys.exit(0)  # another instance holds the lock
    else:
        import fcntl as _fcntl
        try:
            _fcntl.flock(_lock_fd, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
        except OSError:
            _lock_fd.close()
            sys.exit(0)  # another instance holds the lock
    _lock_fd.write(str(os.getpid()))
    _lock_fd.flush()
except Exception:
    pass  # if locking fails for any reason, continue — better to run than block

import subprocess
import threading
import time
import webbrowser
import logging
import logging.handlers
import json
import urllib.request
import urllib.error
from pathlib import Path
from datetime import datetime

# ── Dependency check ──────────────────────────────────────────────────────────
_missing = []
try:
    import pystray
except ImportError:
    _missing.append("pystray")
try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    _missing.append("pillow")

if _missing:
    try:
        import tkinter as _tk
        from tkinter import messagebox as _mb
        _r = _tk.Tk(); _r.withdraw()
        _mb.showerror(
            "MilRadio — Missing Packages",
            f"Required packages not installed:\n\n"
            f"  {' '.join(_missing)}\n\n"
            f"Fix:\n  pip install pystray pillow\n\n"
            f"Python: {sys.executable}"
        )
        _r.destroy()
    except Exception:
        print(f"ERROR: missing packages: {_missing}")
        print("Fix:  pip install pystray pillow")
    sys.exit(1)

# ── Paths ─────────────────────────────────────────────────────────────────────
_FROZEN  = getattr(sys, "frozen", False)

if _FROZEN:
    # Running as a PyInstaller --onedir .exe.
    # The deployment folder (dist\MilRadio\) contains three subfolders:
    #   MilRadio-ServerTray\MilRadio-ServerTray.exe  ← this exe
    #   MilRadio-Server\MilRadio-Server.exe          ← server to launch
    #   MilRadio\MilRadio.exe                        ← operator client
    # plus radio_config.ini, radio_admin.html etc at the top level.
    #
    # sys.executable is the tray exe.  Its parent is MilRadio-ServerTray\.
    # The DEPLOYMENT ROOT is one level up from that.
    _exe_dir  = Path(sys.executable).parent.resolve()
    # If running from a copy placed at the deployment root (top-level shortcut),
    # _exe_dir IS the deployment root.  If running from the subfolder,
    # the deployment root is _exe_dir.parent.
    # Detect by checking whether MilRadio-Server\ exists alongside or above.
    _root_candidate_same   = _exe_dir                # top-level copy case
    _root_candidate_parent = _exe_dir.parent         # subfolder case
    if (_root_candidate_parent / "TacNet-Server" / "TacNet-Server.exe").exists():
        BASE_DIR = _root_candidate_parent
        SERVER_EXE = BASE_DIR / "TacNet-Server" / "TacNet-Server.exe"
    elif (_root_candidate_parent / "MilRadio-Server" / "MilRadio-Server.exe").exists():
        BASE_DIR = _root_candidate_parent
        SERVER_EXE = BASE_DIR / "MilRadio-Server" / "MilRadio-Server.exe"
    else:
        BASE_DIR = _root_candidate_same
        if (BASE_DIR / "TacNet-Server" / "TacNet-Server.exe").exists():
            SERVER_EXE = BASE_DIR / "TacNet-Server" / "TacNet-Server.exe"
        else:
            SERVER_EXE = BASE_DIR / "MilRadio-Server" / "MilRadio-Server.exe"
    SERVER_SCRIPT = None   # not used in frozen mode
    PYTHON        = None   # not used in frozen mode
else:
    # Running as a plain Python script (development / debugging)
    BASE_DIR      = Path(__file__).parent.resolve()
    SERVER_EXE    = None   # not used in script mode
    SERVER_SCRIPT = BASE_DIR / "radio_web.py"
    PYTHON        = sys.executable

LOG_DIR     = BASE_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)

# ── Logging ───────────────────────────────────────────────────────────────────
log = logging.getLogger("milradio.tray")
log.setLevel(logging.INFO)
_fh = logging.handlers.TimedRotatingFileHandler(
    LOG_DIR / "tray.log", when="midnight", backupCount=14, encoding="utf-8"
)
_fh.setFormatter(logging.Formatter("%(asctime)s  %(levelname)-7s  %(message)s"))
log.addHandler(_fh)
log.addHandler(logging.StreamHandler(sys.stdout))

# ── Argument parsing ──────────────────────────────────────────────────────────
import argparse as _ap
_p = _ap.ArgumentParser(description="MilRadio Server Tray", add_help=False)
_p.add_argument("--config",   default="radio_config.ini")
_p.add_argument("--web-port", type=int, default=8890, dest="web_port")
_p.add_argument("--port",     type=int, default=None)
_p.add_argument("--host",     default=None)
_p.add_argument("--debug",    action="store_true")
_args, _unknown = _p.parse_known_args()

WEB_PORT    = _args.web_port
ADMIN_URL   = f"http://localhost:{WEB_PORT}"
CONFIG_FILE = BASE_DIR / _args.config

# ── Process state ─────────────────────────────────────────────────────────────
_proc:    subprocess.Popen | None = None
_proc_lock = threading.Lock()
_watchdog_paused   = False
_watchdog_running  = True

# ── Stats cache (polled from server API) ──────────────────────────────────────
_stats = {
    "clients_now":    0,
    "packets_rx":     0,
    "packets_relayed":0,
    "uptime_str":     "—",
    "running":        False,
}
_stats_lock = threading.Lock()


# ── Process management ────────────────────────────────────────────────────────
def _server_cmd() -> list:
    """
    Build the command to launch the server.
    Frozen build  → run MilRadio-Server.exe directly (it bundles radio_web.py).
    Script mode   → run radio_web.py with the local Python interpreter.
    """
    if _FROZEN:
        # Sanity-check the exe exists before trying to launch it
        if not SERVER_EXE.exists():
            log.error(
                "MilRadio-Server.exe not found at %s — "
                "make sure both exes are in the same folder.", SERVER_EXE
            )
            return []
        cmd = [str(SERVER_EXE),
               "--config",   str(CONFIG_FILE),
               "--web-port", str(WEB_PORT)]
    else:
        cmd = [PYTHON, str(SERVER_SCRIPT),
               "--config",   str(CONFIG_FILE),
               "--web-port", str(WEB_PORT)]

    if _args.port:
        cmd += ["--port", str(_args.port)]
    if _args.host:
        cmd += ["--host", _args.host]
    if _args.debug:
        cmd += ["--debug"]
    return cmd


def start_server():
    global _proc
    with _proc_lock:
        if _proc and _proc.poll() is None:
            log.info("Server already running (pid %d)", _proc.pid)
            return

        cmd = _server_cmd()
        if not cmd:
            log.error("Cannot start server — command is empty (exe missing?)")
            return

        ts       = datetime.now().strftime("%Y-%m-%d")
        log_path = LOG_DIR / f"milradio_server_{ts}.log"
        log_f    = open(log_path, "a", encoding="utf-8", buffering=1)
        log_f.write(f"\n{'='*60}\n")
        log_f.write(f"Started at {datetime.now().isoformat()}\n")
        log_f.write(f"Command:   {' '.join(cmd)}\n")
        log_f.write(f"{'='*60}\n")
        log_f.flush()

        flags = 0
        if sys.platform == "win32":
            flags = subprocess.CREATE_NO_WINDOW

        # For --onedir builds the server EXE must run from its own directory
        # so the Python runtime can locate the _internal\ folder alongside it.
        # For script mode BASE_DIR is fine.
        _server_cwd = str(Path(cmd[0]).parent) if _FROZEN else str(BASE_DIR)
        _proc = subprocess.Popen(
            cmd,
            cwd=_server_cwd,
            stdout=log_f,
            stderr=log_f,
            creationflags=flags,
        )
        log.info("Server started (pid %d)  cmd: %s", _proc.pid, cmd[0])


def stop_server():
    global _proc, _watchdog_paused
    _watchdog_paused = True          # prevent watchdog restarting during stop
    try:
        with _proc_lock:
            if _proc and _proc.poll() is None:
                _proc.terminate()
                try:
                    _proc.wait(timeout=6)
                except subprocess.TimeoutExpired:
                    _proc.kill()
                    _proc.wait(timeout=2)
                log.info("Server stopped")
            _proc = None
    finally:
        # Only re-enable the watchdog if we are NOT quitting.
        # If _watchdog_running is False, quit is in progress — leave watchdog paused.
        if _watchdog_running:
            _watchdog_paused = False


def restart_server():
    global _watchdog_paused
    _watchdog_paused = True
    try:
        stop_server()
        time.sleep(0.8)
        start_server()
        log.info("Server restarted")
    finally:
        _watchdog_paused = False


def is_running() -> bool:
    with _proc_lock:
        return _proc is not None and _proc.poll() is None


def _log_server_exit():
    """Called when the server process exits — log the exit code and last lines of server log."""
    with _proc_lock:
        if _proc is None:
            return
        code = _proc.poll()
        if code is None:
            return  # still running
    log.warning("Server exited with code %s", code)
    # Try to read the last few lines of the server log to surface the error
    ts = datetime.now().strftime("%Y-%m-%d")
    server_log = LOG_DIR / f"milradio_server_{ts}.log"
    try:
        if server_log.exists():
            with open(server_log, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
            tail = [l.rstrip() for l in lines[-20:] if l.strip()]
            if tail:
                log.warning("--- Last lines of server log ---")
                for line in tail:
                    log.warning("  %s", line)
                log.warning("--- End of server log ---")
    except Exception as e:
        log.warning("Could not read server log: %s", e)


# ── Watchdog ──────────────────────────────────────────────────────────────────
def _watchdog():
    """
    Restart server if it crashes.
    Uses exponential backoff: waits 8s, 16s, 32s, 64s, then caps at 60s.
    After 5 consecutive failures, stops trying and shows an error in tray log.
    """
    fail_count  = 0
    wait_secs   = 8
    MAX_FAILS   = 5

    while _watchdog_running:
        time.sleep(wait_secs)
        if _watchdog_paused:
            continue
        if not is_running():
            _log_server_exit()
            fail_count += 1
            if fail_count >= MAX_FAILS:
                log.error(
                    "Server has failed %d times in a row — stopping auto-restart. "
                    "Check logs\\milradio_server_*.log for the error. "
                    "Use the tray menu to restart manually once the problem is fixed.",
                    fail_count,
                )
                # Reset counters but pause watchdog restarts until user intervenes
                # by choosing Restart from the tray menu (which calls restart_server
                # directly and resets fail_count via the explicit restart path).
                fail_count = 0
                wait_secs  = 8
                # Wait a long time before trying again automatically
                for _ in range(60):
                    if _watchdog_paused or not _watchdog_running:
                        break
                    time.sleep(5)
                continue

            log.warning(
                "Server process gone (attempt %d/%d) — restarting in %ds…",
                fail_count, MAX_FAILS, wait_secs,
            )
            start_server()
            # Exponential backoff capped at 60s
            wait_secs = min(wait_secs * 2, 60)
        else:
            # Server is healthy — reset backoff
            if fail_count > 0:
                log.info("Server recovered — resetting restart backoff")
            fail_count = 0
            wait_secs  = 8
def _poll_stats():
    """Background thread — polls /api/status every 5 s."""
    global _stats
    while _watchdog_running:
        try:
            with urllib.request.urlopen(f"{ADMIN_URL}/api/status", timeout=2) as r:
                data = json.loads(r.read())
            with _stats_lock:
                _stats = {
                    "clients_now":    data.get("clients_now", 0),
                    "packets_rx":     data.get("packets_rx", 0),
                    "packets_relayed":data.get("packets_relayed", 0),
                    "uptime_str":     data.get("uptime_str", "—"),
                    "running":        True,
                }
        except Exception:
            with _stats_lock:
                _stats["running"] = False
        time.sleep(5)


def _get_stats() -> dict:
    with _stats_lock:
        return dict(_stats)


# ── Tray icon image ───────────────────────────────────────────────────────────
def _build_icon(state: str = "stopped") -> Image.Image:
    """
    Draw a 64×64 tray icon.
    state: 'active'  — green  (server up + clients connected)
           'idle'    — amber  (server up, no clients)
           'stopped' — red    (server down)
    """
    size   = 64
    img    = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw   = ImageDraw.Draw(img)

    colours = {
        "active":  ((30, 180, 70),   (20, 120, 50)),
        "idle":    ((210, 150, 20),  (140, 95,  10)),
        "stopped": ((200, 50, 45),   (130, 30,  25)),
    }
    fill, shadow = colours.get(state, colours["stopped"])

    # Radio tower silhouette — vertical mast + three arc-pairs
    cx = size // 2

    # Mast
    draw.rectangle([cx - 2, 28, cx + 2, 56], fill=fill)

    # Base spread
    draw.rectangle([cx - 10, 53, cx + 10, 57], fill=shadow)

    # Signal arcs (concentric, centred on top of mast)
    arc_top = 10
    for i, (r, w) in enumerate([(24, 3), (16, 3), (8, 3)]):
        alpha = 255 if state != "stopped" else 100
        c = fill + (alpha,)
        box = [cx - r, arc_top, cx + r, arc_top + r * 2]
        draw.arc(box, start=200, end=340, fill=c, width=w)

    # Small circle at top of mast
    draw.ellipse([cx - 4, 23, cx + 4, 31], fill=fill)

    return img


# ── Menu helpers ──────────────────────────────────────────────────────────────
def _open_admin(_icon=None, _item=None):
    webbrowser.open(ADMIN_URL)
    log.info("Opened admin UI: %s", ADMIN_URL)


def _open_logs(_icon=None, _item=None):
    try:
        if sys.platform == "win32":
            os.startfile(str(LOG_DIR))
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(LOG_DIR)])
        else:
            subprocess.Popen(["xdg-open", str(LOG_DIR)])
    except Exception as e:
        log.warning("Could not open log folder: %s", e)


def _open_config(_icon=None, _item=None):
    try:
        if sys.platform == "win32":
            os.startfile(str(CONFIG_FILE))
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(CONFIG_FILE)])
        else:
            subprocess.Popen(["xdg-open", str(CONFIG_FILE)])
    except Exception as e:
        log.warning("Could not open config: %s", e)


# ── Main tray loop ────────────────────────────────────────────────────────────
def run_tray():
    from pystray import Icon, MenuItem as Item, Menu

    def _icon_state() -> str:
        if not is_running():
            return "stopped"
        s = _get_stats()
        return "active" if s.get("clients_now", 0) > 0 else "idle"

    def _title() -> str:
        s = _get_stats()
        if not is_running():
            return "MilRadio Server — STOPPED"
        return (
            f"MilRadio Server  ●  "
            f"{s['clients_now']} client(s)  ●  "
            f"up {s['uptime_str']}"
        )

    def make_menu():
        running  = is_running()
        s        = _get_stats()
        srv_line = "● RUNNING" if running else "○ STOPPED"
        cli_line = f"{s['clients_now']} connected" if running else "—"

        return Menu(
            Item("MilRadio Server", lambda *_: None, enabled=False),
            Menu.SEPARATOR,
            Item(f"Status:   {srv_line}",   lambda *_: None, enabled=False),
            Item(f"Clients:  {cli_line}",   lambda *_: None, enabled=False),
            Item(f"Uptime:   {s['uptime_str']}", lambda *_: None, enabled=False),
            Item(f"Pkts RX:  {s['packets_rx']}",      lambda *_: None, enabled=False),
            Item(f"Relayed:  {s['packets_relayed']}",  lambda *_: None, enabled=False),
            Menu.SEPARATOR,
            Item(
                "▶  Start Server",
                lambda *_: threading.Thread(target=start_server, daemon=True).start(),
                enabled=not running,
            ),
            Item(
                "■  Stop Server",
                lambda *_: threading.Thread(target=stop_server, daemon=True).start(),
                enabled=running,
            ),
            Item(
                "↺  Restart Server",
                lambda *_: threading.Thread(target=restart_server, daemon=True).start(),
            ),
            Menu.SEPARATOR,
            Item("🌐  Open Admin UI",    lambda *_: _open_admin()),
            Item("📋  View Server Logs", lambda *_: _open_logs()),
            Item("⚙   Edit Config",     lambda *_: _open_config()),
            Menu.SEPARATOR,
            Item("✕  Quit", lambda *_: _do_quit(icon)),
        )

    def _do_quit(icon_ref):
        """Quit: stop watchdog, kill server, exit process."""
        global _watchdog_paused, _watchdog_running
        # Kill watchdog loop first — before anything else
        _watchdog_running = False
        _watchdog_paused  = True
        # Kill the server process directly (not via stop_server which re-enables watchdog)
        with _proc_lock:
            p = _proc
        if p and p.poll() is None:
            try:
                p.terminate()
                try:
                    p.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    p.kill()
                    p.wait(timeout=2)
            except Exception as e:
                log.warning("Error killing server on quit: %s", e)
        with _proc_lock:
            globals()['_proc'] = None
        # Stop the tray icon — this causes icon.run() to return
        icon_ref.stop()

    # Build initial icon
    icon = Icon(
        name  = "milradio_server",
        icon  = _build_icon("stopped"),
        title = "MilRadio Server — starting…",
        menu  = make_menu(),
    )

    # Refresh icon + menu + tooltip every 5 s
    def _refresh_loop():
        while _watchdog_running:
            time.sleep(5)
            try:
                state = _icon_state()
                icon.icon  = _build_icon(state)
                icon.title = _title()
                icon.menu  = make_menu()
            except Exception:
                pass
    threading.Thread(target=_refresh_loop, daemon=True).start()

    # Start background threads
    threading.Thread(target=_poll_stats, daemon=True).start()
    threading.Thread(target=_watchdog,   daemon=True).start()

    # Start the server immediately
    threading.Thread(target=start_server, daemon=True).start()

    log.info("Tray started — admin UI: %s", ADMIN_URL)
    log.info("BASE_DIR:    %s", BASE_DIR)
    log.info("CONFIG_FILE: %s", CONFIG_FILE)
    if _FROZEN:
        log.info("SERVER_EXE:  %s  (exists=%s)", SERVER_EXE, SERVER_EXE.exists())
    else:
        log.info("SERVER_SCRIPT: %s", SERVER_SCRIPT)
    icon.run()
    # icon.run() returns when icon.stop() is called (i.e. user clicked Quit).
    # Hard-exit to ensure no daemon threads (watchdog, poll) can restart the server.
    log.info("Tray exiting")
    os._exit(0)


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    try:
        run_tray()
    except Exception as e:
        import traceback
        err = traceback.format_exc()
        # Write crash to log even under pythonw where stdout is silent
        try:
            crash_log = LOG_DIR / "tray_crash.log"
            with open(crash_log, "a", encoding="utf-8") as f:
                f.write(f"\n{'='*60}\n{datetime.now()}\n{err}\n")
        except Exception:
            pass
        try:
            import tkinter as _tk
            from tkinter import messagebox as _mb
            _r = _tk.Tk(); _r.withdraw()
            _mb.showerror(
                "MilRadio Tray Crashed",
                f"Tray failed:\n\n{e}\n\n"
                f"See logs/tray_crash.log\n\n"
                f"Python: {sys.executable}"
            )
            _r.destroy()
        except Exception:
            pass