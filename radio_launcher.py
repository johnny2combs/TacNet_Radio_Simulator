import time
import socket
import sys
import os
import subprocess
import requests
import logging
import threading
import configparser
from pathlib import Path
from tkinter import filedialog, Tk
from PIL import Image, ImageDraw
import pystray
from pystray import MenuItem as Item

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s', datefmt='%H:%M:%S')
log = logging.getLogger("Launcher")

class NodeAgent:
    def __init__(self):
        self.settings_file = Path("launcher_settings.ini")
        self.server_url = "http://127.0.0.1:8890"
        self.client_exe = ""
        self.node_id = socket.gethostname()
        self.client_process = None
        self.active_callsign = None
        self.running = True
        self.connected = False
        
        self.load_settings()
        self._find_default_client()

    def load_settings(self):
        if self.settings_file.exists():
            try:
                cfg = configparser.ConfigParser()
                cfg.read(self.settings_file)
                if 'settings' in cfg:
                    self.server_url = cfg['settings'].get('server_url', self.server_url)
                    self.client_exe = cfg['settings'].get('client_exe', self.client_exe)
                log.info("Settings loaded.")
            except Exception as e:
                log.error(f"Failed to load settings: {e}")

    def save_settings(self):
        try:
            cfg = configparser.ConfigParser()
            cfg['settings'] = {
                'server_url': self.server_url,
                'client_exe': self.client_exe
            }
            with open(self.settings_file, 'w') as f:
                cfg.write(f)
            log.info("Settings saved.")
        except Exception as e:
            log.error(f"Failed to save settings: {e}")

    def _find_default_client(self):
        """Try to find the client if not set."""
        if self.client_exe and Path(self.client_exe).exists():
            return

        # Look in current dir or standard build dirs
        exe_dir = Path(sys.executable).parent if getattr(sys, 'frozen', False) else Path(__file__).parent
        candidates = [
            exe_dir / "TacNet-Client.exe",                  # Standard compiled exe
            exe_dir.parent / "TacNet-Client" / "TacNet-Client.exe", # Standard build folder layout
            exe_dir / "radio_client.exe",
            exe_dir / "radio_client.py",                    # New Aether Client (PySide6)
            exe_dir / "radio_client_v2.py"                  # Old/Obsolete version
        ]
        
        for c in candidates:
            if c.exists():
                self.client_exe = str(c)
                log.info(f"Auto-detected client at: {self.client_exe}")
                return

    def _is_client_running(self) -> bool:
        if self.client_process is None:
            return False
        if hasattr(self.client_process, 'poll'):
            return self.client_process.poll() is None
        # Adopted PID case
        try:
            import os
            os.kill(self.client_process, 0)
            return True
        except:
            return False

    def _start_client(self, callsign: str, channel: int):
        if not self.client_exe or not Path(self.client_exe).exists():
            log.error("Cannot start client: Executable path is invalid.")
            return

        if self._is_client_running():
            self._stop_client()
        
        server_host_port = self.server_url.replace("http://", "").replace("https://", "").split("/")[0]
        
        cmd = [self.client_exe]
        if self.client_exe.endswith(".py"):
            python_exe = sys.executable
            if getattr(sys, 'frozen', False):
                python_exe = "python" 
            cmd = [python_exe, self.client_exe]

        cmd.extend([
            "--callsign", callsign,
            "--server", server_host_port,
            "--node-id", self.node_id,
            "--channel", str(channel),
            "--remote"
        ])
            
        log.info(f"Launching client: {' '.join(cmd)}")
        try:
            self.client_process = subprocess.Popen(cmd)
            self.active_callsign = callsign
            log.info(f"Client started with PID: {self.client_process.pid} (Callsign: {callsign})")
        except Exception as e:
            log.error(f"Failed to start client: {e}")

    def _stop_client(self):
        if self.client_process:
            if self._is_client_running():
                pid = self.client_process.pid if hasattr(self.client_process, 'pid') else self.client_process
                log.info(f"Stopping client (PID {pid})...")
                if hasattr(self.client_process, 'terminate'):
                    self.client_process.terminate()
                    try:
                        self.client_process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        self.client_process.kill()
                else:
                    try:
                        import os
                        os.kill(pid, 15) # SIGTERM
                    except:
                        pass
                log.info("Client stopped.")
            else:
                log.info("Client process already terminated. Cleaning up state.")
            
            self.client_process = None
            self.active_callsign = None

    def poll_loop(self):
        log.info(f"Starting Launcher Agent on Node: {self.node_id}")
        
        while self.running:
            if not self.server_url:
                time.sleep(2)
                continue

            status = "running" if self._is_client_running() else "stopped"
            pid = None
            if self._is_client_running():
                pid = self.client_process.pid if hasattr(self.client_process, 'pid') else self.client_process
            
            is_adopted = self.client_process is not None and not hasattr(self.client_process, 'pid')
            payload = {
                "status": status,
                "pid": pid,
                "adopted": is_adopted
            }
            
            try:
                r = requests.post(
                    f"{self.server_url.rstrip('/')}/api/nodes/poll/{self.node_id}",
                    json=payload,
                    timeout=5
                )
                self.connected = True
                if r.status_code == 200:
                    data = r.json()
                    command = data.get("command")
                    
                    if command == "start":
                        cs = data.get("callsign")
                        ch = data.get("channel")
                        if cs:
                            self._start_client(cs, ch)
                    elif command == "stop":
                        self._stop_client()
                    else:
                        # Adoption / Mismatch logic
                        target_cs = data.get("callsign")
                        remote_pid = data.get("client_pid")
                        
                        # Check for adoption: Server has a PID, we don't think we are running
                        if remote_pid and not self._is_client_running():
                            try:
                                import os
                                os.kill(remote_pid, 0)
                                log.info(f"Adopting manually launched client (PID {remote_pid}, Callsign {target_cs})")
                                self.client_process = remote_pid
                                self.active_callsign = target_cs
                            except:
                                pass

                        if target_cs and self._is_client_running():
                            if self.active_callsign is None:
                                self.active_callsign = target_cs
                                
                            if target_cs != self.active_callsign:
                                self._mismatch_count = getattr(self, '_mismatch_count', 0) + 1
                                if self._mismatch_count >= 3:
                                    log.info(f"Callsign change confirmed: {self.active_callsign} -> {target_cs}. Restarting client...")
                                    self._mismatch_count = 0
                                    self._start_client(target_cs, data.get("channel"))
                                else:
                                    log.debug(f"Callsign mismatch ({self.active_callsign} vs {target_cs}), waiting for sync... ({self._mismatch_count}/3)")
                            else:
                                self._mismatch_count = 0
                        
            except Exception as e:
                self.connected = False
                log.debug(f"Connection failed: {e}")
                
            time.sleep(2.5)

# --- Tray Icon Logic ---

def create_image(color1, color2):
    image = Image.new('RGB', (64, 64), color2)
    dc = ImageDraw.Draw(image)
    dc.rectangle((16, 16, 48, 48), fill=color1)
    return image

import ctypes
from ctypes import wintypes

def select_client_exe(agent, icon):
    def _open_dialog():
        log.info("Opening file dialog...")
        root = Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        
        file_path = filedialog.askopenfilename(
            title="Select Radio Client Executable",
            filetypes=[("Executables", "*.exe;*.py"), ("All Files", "*.*")]
        )
        
        root.destroy()
        
        if file_path:
            agent.client_exe = file_path
            agent.save_settings()
            log.info(f"Updated client path: {file_path}")
            icon.notify(f"Client path updated:\n{file_path}", "Launcher Settings")
        else:
            log.info("File selection cancelled.")

    # Always run dialog in a separate thread to avoid blocking the tray menu
    threading.Thread(target=_open_dialog, daemon=True).start()

def set_server_url(agent, icon):
    def _open_dialog():
        log.info("Opening server URL dialog...")
        root = Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        
        from tkinter import simpledialog
        new_url = simpledialog.askstring(
            "Launcher Settings", 
            "Enter Server URL (e.g., http://192.168.1.100:8890):",
            initialvalue=agent.server_url,
            parent=root
        )
        
        root.destroy()
        
        if new_url:
            if not new_url.startswith("http"):
                new_url = "http://" + new_url
            agent.server_url = new_url
            agent.save_settings()
            log.info(f"Updated server URL: {new_url}")
            icon.notify(f"Server URL updated:\n{new_url}", "Launcher Settings")
        else:
            log.info("Server URL update cancelled.")

    threading.Thread(target=_open_dialog, daemon=True).start()

def quit_app(agent, icon):
    agent.running = False
    agent._stop_client()
    icon.stop()

def run_app():
    agent = NodeAgent()
    
    # Run polling in a separate thread
    thread = threading.Thread(target=agent.poll_loop, daemon=True)
    thread.start()

    def get_status_text(icon):
        state = "Connected" if agent.connected else "Disconnected"
        client = "Client Active" if agent._is_client_running() else "Client Idle"
        return f"TacNet Launcher - {state} ({client})"

    icon = pystray.Icon("TacNetLauncher")
    icon.icon = create_image("blue", "white")
    icon.title = "TacNet Launcher"
    
    def update_icon_loop():
        while agent.running:
            if agent.connected:
                icon.icon = create_image("green", "black")
            else:
                icon.icon = create_image("red", "black")
            icon.title = get_status_text(icon)
            time.sleep(5)

    threading.Thread(target=update_icon_loop, daemon=True).start()

    icon.menu = pystray.Menu(
        Item("TacNet Launcher", lambda: None, enabled=False),
        pystray.Menu.SEPARATOR,
        Item("Select Client EXE...", lambda: select_client_exe(agent, icon)),
        Item("Set Server URL...", lambda: set_server_url(agent, icon)),
        Item("Restart Agent", lambda: agent._stop_client()),
        pystray.Menu.SEPARATOR,
        Item("Quit", lambda: quit_app(agent, icon))
    )

    icon.run()

if __name__ == "__main__":
    run_app()
