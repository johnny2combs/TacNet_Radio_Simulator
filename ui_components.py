import sys
import json
import urllib.request
import urllib.error
import socket
from pathlib import Path
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QComboBox, QFormLayout, QSlider, QCheckBox,
    QScrollArea, QWidget, QTabWidget, QMessageBox, QSizePolicy
)
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QDoubleValidator
from radio_config import ConfigManager, RadioConfig, ChannelDef

# Colors from Aether Style
C_BG = "#0f172a"
C_RECESS = "#020617"
C_BORDER = "#1e293b"
C_TEXT = "#f8fafc"
C_TEXT_DIM = "#94a3b8"
C_ACCENT = "#38bdf8"
C_ACCENT_DIM = "#075985"
C_PTT = "#ef4444"
C_SIGNAL = "#22c55e"

AETHER_STYLE = f"""
    QMainWindow, QDialog {{ background: {C_BG}; color: {C_TEXT}; font-family: 'Inter'; }}
    QLabel {{ color: {C_TEXT}; }}
    QLineEdit, QComboBox, QListWidget {{
        background: {C_RECESS};
        border: 1px solid {C_BORDER};
        border-radius: 4px;
        color: {C_TEXT};
        padding: 8px;
    }}
    QPushButton {{
        background: {C_BORDER};
        border: none;
        border-radius: 4px;
        color: {C_TEXT};
        padding: 10px;
        font-weight: bold;
    }}
    QPushButton:hover {{ background: {C_ACCENT_DIM}; }}
    QPushButton#PrimaryButton {{ background: {C_ACCENT}; color: {C_BG}; }}
    QPushButton#PrimaryButton:hover {{ background: #7dd3fc; }}
    QPushButton#SecondaryButton {{ background: {C_RECESS}; border: 1px solid {C_ACCENT}; color: {C_ACCENT}; }}
"""

class KeyCaptureLineEdit(QLineEdit):
    key_pressed = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setPlaceholderText("Press any key...")
        self.setReadOnly(True)
        self.setStyleSheet(f"background: {C_RECESS}; color: {C_SIGNAL}; font-weight: bold; padding: 8px;")

    def keyPressEvent(self, event):
        key = event.key()
        text = event.text()
        
        # Map common keys
        if key == Qt.Key_Space: key_name = "space"
        elif key == Qt.Key_Control: key_name = "ctrl"
        elif key == Qt.Key_Shift: key_name = "shift"
        elif key == Qt.Key_Alt: key_name = "alt"
        elif key == Qt.Key_CapsLock: key_name = "caps_lock"
        elif key >= Qt.Key_F1 and key <= Qt.Key_F12:
            key_name = f"f{key - Qt.Key_F1 + 1}"
        else:
            key_name = text.lower() if text else str(key)
            
        if key_name:
            self.setText(key_name)
            self.key_pressed.emit(key_name)

class SettingsDialog(QDialog):
    def __init__(self, engine, parent=None):
        super().__init__(parent)
        self.engine = engine
        self.setWindowTitle("SYSTEM SETTINGS")
        self.setFixedSize(500, 740)
        self.setStyleSheet(AETHER_STYLE + f"""
            QLabel {{ color: {C_TEXT_DIM}; font-size: 11px; font-weight: bold; }}
            QPushButton#saveBtn {{ background: {C_ACCENT}; color: {C_BG}; }}
            QPushButton#saveBtn:hover {{ background: #7dd3fc; }}
            QScrollArea {{ border: none; background: transparent; }}
            QLineEdit {{ background: rgba(255,255,255,0.05); border: 1px solid rgba(255,255,255,0.1); padding: 8px; border-radius: 4px; }}
            QComboBox {{ background: rgba(255,255,255,0.05); border: 1px solid rgba(255,255,255,0.1); padding: 8px; }}
            QTabWidget::pane {{ border: 1px solid {C_BORDER}; border-radius: 4px; background: {C_BG}; }}
            QTabBar::tab {{
                background: {C_RECESS}; color: {C_TEXT_DIM}; border: 1px solid {C_BORDER};
                border-bottom: none; border-radius: 4px 4px 0 0;
                padding: 8px 16px; font-size: 10px; font-weight: bold; letter-spacing: 1px;
            }}
            QTabBar::tab:selected {{ background: {C_BG}; color: {C_ACCENT}; border-bottom: 2px solid {C_ACCENT}; }}
            QTabBar::tab:hover:!selected {{ background: {C_BORDER}; }}
        """)
        
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(16, 20, 16, 16)
        main_layout.setSpacing(12)

        # ── Header ──────────────────────────────────────────────────────────────
        title = QLabel("◈  SYSTEM CONFIGURATION")
        title.setStyleSheet(f"color: {C_ACCENT}; font-size: 14px; letter-spacing: 2px; margin-bottom: 4px;")
        main_layout.addWidget(title)

        # ── Tab Widget ───────────────────────────────────────────────────────────
        tabs = QTabWidget()
        main_layout.addWidget(tabs, stretch=1)

        # ════════════════════════════════════════════════════════════════════════
        # TAB 1 — GENERAL CONFIG
        # ════════════════════════════════════════════════════════════════════════
        general_tab = QWidget()
        general_tab.setStyleSheet("background: transparent;")
        gen_scroll = QScrollArea()
        gen_scroll.setWidgetResizable(True)
        gen_scroll.setWidget(general_tab)
        gen_scroll.setStyleSheet("border: none; background: transparent;")
        tabs.addTab(gen_scroll, "GENERAL CONFIG")

        gen_layout = QVBoxLayout(general_tab)
        gen_layout.setContentsMargins(12, 12, 12, 12)
        gen_layout.setSpacing(14)

        form = QFormLayout()
        form.setSpacing(10)

        self.cs_edit = QLineEdit(engine._cfg.callsign)
        form.addRow("CALLSIGN", self.cs_edit)

        self.prof_combo = QComboBox()
        for p_id, p in engine._cfg_mgr.profiles.items():
            self.prof_combo.addItem(p.name, p_id)
        idx = self.prof_combo.findData(engine._cfg.radio_profile)
        if idx >= 0: self.prof_combo.setCurrentIndex(idx)
        form.addRow("RADIO PROFILE", self.prof_combo)

        self.key_edit = KeyCaptureLineEdit()
        self.key_edit.setText(engine._cfg.ptt_key)
        form.addRow("PTT HOTKEY", self.key_edit)

        self.input_combo = QComboBox()
        self.output_combo = QComboBox()
        import pyaudio
        pa = pyaudio.PyAudio()
        self.input_combo.addItem("Default", -1)
        self.output_combo.addItem("Default", -1)
        for i in range(pa.get_device_count()):
            dev = pa.get_device_info_by_index(i)
            if dev['maxInputChannels'] > 0:
                self.input_combo.addItem(dev['name'], i)
            if dev['maxOutputChannels'] > 0:
                self.output_combo.addItem(dev['name'], i)
        pa.terminate()
        idx = self.input_combo.findData(engine._cfg.audio_input_device)
        if idx >= 0: self.input_combo.setCurrentIndex(idx)
        idx = self.output_combo.findData(engine._cfg.audio_output_device)
        if idx >= 0: self.output_combo.setCurrentIndex(idx)
        form.addRow("INPUT DEVICE", self.input_combo)
        form.addRow("OUTPUT DEVICE", self.output_combo)

        self.vol_slider = QSlider(Qt.Horizontal)
        self.vol_slider.setRange(0, 200)
        self.vol_slider.setValue(int(engine._cfg.output_volume * 100))
        form.addRow("OUTPUT VOL", self.vol_slider)

        self.gain_slider = QSlider(Qt.Horizontal)
        self.gain_slider.setRange(0, 200)
        self.gain_slider.setValue(int(engine._cfg.input_gain * 100))
        form.addRow("INPUT GAIN", self.gain_slider)

        self.ptt_clicks = QCheckBox()
        self.ptt_clicks.setChecked(engine._cfg.ptt_click_enabled)
        form.addRow("PTT CLICKS", self.ptt_clicks)

        self.relay_sig_combo = QComboBox()
        self.relay_sig_combo.addItem("Default (Profile)", "none")
        self.relay_sig_combo.addItem("Standard", "standard")
        self.relay_sig_combo.addItem("Heavy", "heavy")
        self.relay_sig_combo.addItem("Digital", "digital")
        self.relay_sig_combo.addItem("Tactical", "tactical")
        idx = self.relay_sig_combo.findData(engine._cfg.relay_signature)
        if idx >= 0: self.relay_sig_combo.setCurrentIndex(idx)
        form.addRow("PTT SIGNATURE", self.relay_sig_combo)

        # Tone frequencies
        tone_val = QDoubleValidator(100.0, 10000.0, 1)
        self.tone_f1 = QLineEdit(str(engine._cfg.ptt_tone_f1))
        self.tone_f1.setValidator(tone_val)
        form.addRow("PTT TONE F1 (Hz)", self.tone_f1)
        self.tone_f2 = QLineEdit(str(engine._cfg.ptt_tone_f2))
        self.tone_f2.setValidator(tone_val)
        form.addRow("PTT TONE F2 (Hz)", self.tone_f2)

        # TAK Settings
        self.tak_enabled = QCheckBox()
        self.tak_enabled.setChecked(engine._cfg.tak_enabled)
        form.addRow("TAK ENABLED", self.tak_enabled)
        self.tak_url = QLineEdit(engine._cfg.tak_server_url)
        form.addRow("TAK SERVER URL", self.tak_url)
        self.tak_cs = QLineEdit(engine._cfg.tak_callsign)
        self.tak_cs.setPlaceholderText("Defaults to Radio Callsign")
        form.addRow("TAK CALLSIGN", self.tak_cs)

        # Feature 4: Spatial Audio
        self.spatial_audio_chk = QCheckBox()
        self.spatial_audio_chk.setChecked(getattr(engine._cfg, 'spatial_audio', False))
        self.spatial_audio_chk.setToolTip("Split-ear: primary channel = Left, monitored = Right")
        form.addRow("SPATIAL AUDIO", self.spatial_audio_chk)

        gen_layout.addLayout(form)
        gen_layout.addStretch()

        # ════════════════════════════════════════════════════════════════════════
        # TAB 2 — COMSEC KEYFILL
        # ════════════════════════════════════════════════════════════════════════
        comsec_tab = QWidget()
        comsec_tab.setStyleSheet("background: transparent;")
        comsec_scroll = QScrollArea()
        comsec_scroll.setWidgetResizable(True)
        comsec_scroll.setWidget(comsec_tab)
        comsec_scroll.setStyleSheet("border: none; background: transparent;")
        tabs.addTab(comsec_scroll, "COMSEC KEYFILL")

        comsec_layout = QVBoxLayout(comsec_tab)
        comsec_layout.setContentsMargins(12, 12, 12, 12)
        comsec_layout.setSpacing(10)

        hdr_lbl = QLabel("◈  CRYPTOGRAPHIC KEY FILL")
        hdr_lbl.setStyleSheet(f"color: {C_ACCENT}; font-size: 12px; letter-spacing: 1px; margin-bottom: 4px;")
        comsec_layout.addWidget(hdr_lbl)

        desc = QLabel(
            "Enter or change passphrases for encrypted channels. "
            "Keys are derived via PBKDF2-SHA256 (200,000 iterations). "
            "All entries are masked."
        )
        desc.setWordWrap(True)
        desc.setStyleSheet(f"color: {C_TEXT_DIM}; font-size: 10px;")
        comsec_layout.addWidget(desc)

        # Per-channel key fields
        self.keyfill_inputs: dict = {}
        keyfill_form = QFormLayout()
        keyfill_form.setSpacing(8)

        for ch_id in sorted(engine._cfg_mgr.channels.keys()):
            ch = engine._cfg_mgr.channels[ch_id]
            if ch_id == 99:
                continue  # VIC never encrypted
            inp = QLineEdit()
            inp.setEchoMode(QLineEdit.Password)
            inp.setPlaceholderText("─── not set ───" if not ch.passphrase else "••••••••")
            if ch.passphrase:
                inp.setText(ch.passphrase)
            inp.setStyleSheet(
                f"background: {C_RECESS}; border: 1px solid {C_BORDER}; "
                f"color: {C_SIGNAL}; padding: 8px; border-radius: 4px; font-family: monospace;"
            )
            enc_badge = "🔒" if ch.encrypted else "🔓"
            label = f"CH{ch_id:02d} {enc_badge}  {ch.name}"
            keyfill_form.addRow(label, inp)
            self.keyfill_inputs[ch_id] = inp

        comsec_layout.addLayout(keyfill_form)

        save_keys_btn = QPushButton("💾  APPLY KEY CHANGES")
        save_keys_btn.setStyleSheet(
            f"background: {C_ACCENT_DIM}; color: {C_TEXT}; font-weight: bold; "
            f"padding: 10px; border-radius: 4px; border: 1px solid {C_ACCENT};"
        )
        save_keys_btn.clicked.connect(self._apply_keyfill)
        comsec_layout.addWidget(save_keys_btn)

        comsec_layout.addSpacing(12)

        # ── ZEROIZE ──────────────────────────────────────────────────────────────
        zeroize_frame = QWidget()
        zeroize_frame.setStyleSheet(
            f"background: rgba(239,68,68,0.08); border: 1px solid rgba(239,68,68,0.35); "
            f"border-radius: 6px; padding: 4px;"
        )
        zeroize_layout = QVBoxLayout(zeroize_frame)
        zeroize_layout.setContentsMargins(10, 10, 10, 10)
        zeroize_layout.setSpacing(6)

        zeroize_warn = QLabel("⚠  EMERGENCY ZEROIZE")
        zeroize_warn.setStyleSheet("color: #ef4444; font-size: 12px; font-weight: bold;")
        zeroize_layout.addWidget(zeroize_warn)

        zeroize_desc = QLabel(
            "Destroys ALL cryptographic keys on this device and pushes "
            "the cleared plan to the server. THIS CANNOT BE UNDONE."
        )
        zeroize_desc.setWordWrap(True)
        zeroize_desc.setStyleSheet("color: #fca5a5; font-size: 10px;")
        zeroize_layout.addWidget(zeroize_desc)

        self.zeroize_btn = QPushButton("🚨  ZEROIZE ALL KEYS  🚨")
        self.zeroize_btn.setStyleSheet(
            "background: #7f1d1d; border: 2px solid #ef4444; color: #fecaca; "
            "font-size: 12px; font-weight: bold; letter-spacing: 2px; "
            "padding: 12px; border-radius: 4px;"
        )
        self.zeroize_btn.clicked.connect(self._zeroize_all_keys)
        zeroize_layout.addWidget(self.zeroize_btn)

        comsec_layout.addWidget(zeroize_frame)
        comsec_layout.addStretch()

        # ── Footer buttons ───────────────────────────────────────────────────────
        btns = QHBoxLayout()
        btns.setContentsMargins(0, 4, 0, 0)
        self.cancel_btn = QPushButton("CANCEL")
        self.cancel_btn.clicked.connect(self.reject)
        self.save_btn = QPushButton("SAVE CHANGES")
        self.save_btn.setObjectName("saveBtn")
        self.save_btn.clicked.connect(self.save)
        btns.addWidget(self.cancel_btn)
        btns.addWidget(self.save_btn)
        main_layout.addLayout(btns)

    # ── Key-fill application ─────────────────────────────────────────────────────
    def _apply_keyfill(self):
        """Save individual channel key changes and rebuild codec."""
        mgr = self.engine._cfg_mgr
        changed = 0
        for ch_id, inp in self.keyfill_inputs.items():
            new_key = inp.text().strip()
            ch = mgr.channels.get(ch_id)
            if ch is None:
                continue
            old_key = ch.passphrase
            if new_key != old_key:
                ch.passphrase = new_key
                if new_key:
                    ch.comsec_mode = "AES-256"
                changed += 1
        if changed:
            mgr.save()
            self.engine.rebuild_codec()
            QMessageBox.information(
                self, "KEYFILL APPLIED",
                f"✓ {changed} channel key(s) updated and codec rebuilt."
            )
        else:
            QMessageBox.information(self, "NO CHANGES", "No key changes detected.")

    # ── Emergency Zeroize ────────────────────────────────────────────────────────
    def _zeroize_all_keys(self):
        """Destroy all crypto keys locally and push zeroed plan to server."""
        reply = QMessageBox.question(
            self,
            "⚠ CONFIRM ZEROIZE",
            "ZEROIZE ALL CRYPTOGRAPHIC KEYS?\n\n"
            "This will:\n"
            "  • Clear all channel passphrases\n"
            "  • Set all channels to PLAIN mode\n"
            "  • Push the cleared plan to the server\n\n"
            "THIS CANNOT BE UNDONE. Confirm?",
            QMessageBox.Yes | QMessageBox.Cancel,
            QMessageBox.Cancel
        )
        if reply != QMessageBox.Yes:
            return

        mgr = self.engine._cfg_mgr

        # 1. Clear all local keys
        for ch in mgr.channels.values():
            ch.passphrase = ""
            ch.comsec_mode = "Plain"

        # 2. Save locally
        mgr.save()

        # 3. Rebuild codec with no keys
        self.engine.rebuild_codec()

        # 4. POST zeroed plan to server
        try:
            web_port = getattr(self.engine._cfg, 'web_admin_port', 8890)
            host = self.engine._cfg.server_host
            url = f"http://{host}:{web_port}/api/commsplan/import"
            payload = {
                "version": 1,
                "channels": [ch.to_dict() for ch in mgr.channels.values()]
                # Note: 'callsigns' intentionally omitted to avoid wiping server list
            }
            data = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(
                url, data=data,
                headers={"Content-Type": "application/json"},
                method="POST"
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                pass  # Success
        except Exception as e:
            QMessageBox.warning(
                self, "SERVER SYNC WARNING",
                f"Keys zeroized locally, but server sync failed:\n{e}\n\n"
                "Restart server or re-import plan manually."
            )

        # 5. Clear UI inputs
        for inp in self.keyfill_inputs.values():
            inp.clear()
            inp.setPlaceholderText("─── not set ───")

        QMessageBox.information(
            self, "ZEROIZE COMPLETE",
            "✓ All cryptographic keys have been destroyed.\n"
            "All channels are now operating in PLAIN mode."
        )
        self.accept()

    # ── Save general settings ────────────────────────────────────────────────────
    def save(self):
        cfg = self.engine._cfg
        mgr = self.engine._cfg_mgr

        # 1. Update local config
        cfg.callsign = self.cs_edit.text().upper()
        cfg.ptt_key = self.key_edit.text()
        cfg.audio_input_device = self.input_combo.currentData()
        cfg.audio_output_device = self.output_combo.currentData()
        cfg.output_volume = self.vol_slider.value() / 100.0
        cfg.input_gain = self.gain_slider.value() / 100.0
        cfg.tak_enabled = self.tak_enabled.isChecked()
        cfg.tak_server_url = self.tak_url.text().strip()
        cfg.tak_callsign = self.tak_cs.text().strip()
        cfg.radio_profile = self.prof_combo.currentData()
        cfg.ptt_click_enabled = self.ptt_clicks.isChecked()
        cfg.relay_signature = self.relay_sig_combo.currentData()
        cfg.spatial_audio = self.spatial_audio_chk.isChecked()

        try:
            cfg.ptt_tone_f1 = float(self.tone_f1.text())
            cfg.ptt_tone_f2 = float(self.tone_f2.text())
        except ValueError:
            pass

        self.engine.set_volume(cfg.output_volume)
        self.engine.restart_tak()
        mgr.save()

        # 2. Push to server to ensure persistence and prevent poll-override
        self.engine.push_config_to_server()

        self.accept()

class ConnectionDialog(QDialog):
    def __init__(self, mgr):
        super().__init__()
        self.mgr = mgr
        self.cfg = mgr.config
        self.result = None
        
        self.setWindowTitle("TacNet | Connect")
        self.setFixedWidth(400)
        self.setStyleSheet(AETHER_STYLE)
        
        layout = QVBoxLayout(self)
        layout.setContentsMargins(30, 30, 30, 30)
        layout.setSpacing(15)
        
        header = QLabel("◈ TACNET")
        header.setStyleSheet(f"color: {C_ACCENT}; font-size: 28px; font-weight: bold;")
        layout.addWidget(header, 0, Qt.AlignCenter)
        
        sub = QLabel("AETHER CLIENT v2")
        sub.setStyleSheet(f"color: {C_TEXT_DIM}; font-size: 10px; letter-spacing: 2px;")
        layout.addWidget(sub, 0, Qt.AlignCenter)
        
        form = QFormLayout()
        form.setSpacing(10)
        form.setLabelAlignment(Qt.AlignRight)
        
        self.edit_host = QLineEdit(self.cfg.server_host)
        form.addRow("SERVER HOST", self.edit_host)
        
        self.edit_web_port = QLineEdit(str(getattr(self.cfg, 'web_admin_port', 8890)))
        form.addRow("WEB ADMIN PORT", self.edit_web_port)
        
        self.edit_node = QLineEdit(getattr(self.cfg, 'node_id', socket.gethostname()))
        form.addRow("NODE IDENTITY", self.edit_node)
        
        layout.addLayout(form)
        
        self.btn_fetch = QPushButton("FETCH SERVER CONFIG")
        self.btn_fetch.setObjectName("SecondaryButton")
        self.btn_fetch.setMinimumHeight(45)
        self.btn_fetch.clicked.connect(self._fetch_data)
        layout.addWidget(self.btn_fetch)
        
        self.combo_callsign = QComboBox()
        self.combo_callsign.setEditable(True)
        self.combo_callsign.addItem(self.cfg.callsign)
        self.combo_callsign.setMinimumHeight(35)
        layout.addWidget(QLabel("SELECT CALLSIGN"))
        layout.addWidget(self.combo_callsign)
        
        self.combo_profile = QComboBox()
        self.combo_profile.setMinimumHeight(35)
        layout.addWidget(QLabel("RADIO HARDWARE PROFILE"))
        layout.addWidget(self.combo_profile)
        
        layout.addSpacing(10)
        
        self.btn_connect = QPushButton("CONNECT TO NETWORK")
        self.btn_connect.setObjectName("PrimaryButton")
        self.btn_connect.setMinimumHeight(50)
        self.btn_connect.clicked.connect(self._on_connect)
        layout.addWidget(self.btn_connect)
        
        self.lbl_status = QLabel("Ready to connect")
        self.lbl_status.setAlignment(Qt.AlignCenter)
        self.lbl_status.setStyleSheet(f"color: {C_TEXT_DIM}; font-size: 11px;")
        layout.addWidget(self.lbl_status)

    def _fetch_data(self):
        from PySide6.QtWidgets import QApplication
        host = self.edit_host.text().strip()
        port = self.edit_web_port.text().strip()
        self.lbl_status.setText("Connecting to server API...")
        self.lbl_status.setStyleSheet(f"color: {C_ACCENT};")
        QApplication.processEvents()
        
        try:
            url_cs = f"http://{host}:{port}/api/callsigns"
            url_pr = f"http://{host}:{port}/api/profiles"
            
            with urllib.request.urlopen(url_cs, timeout=4) as r:
                data = json.loads(r.read().decode())
                self.combo_callsign.clear()
                callsigns = data.get("callsigns", [])
                for cs in callsigns: self.combo_callsign.addItem(cs["callsign"])
                if not callsigns: self.combo_callsign.addItem(self.cfg.callsign)
            
            with urllib.request.urlopen(url_pr, timeout=4) as r:
                data = json.loads(r.read().decode())
                self.combo_profile.clear()
                profiles = data.get("profiles", [])
                for p in profiles: self.combo_profile.addItem(f"{p['id']} - {p['name']}", p['id'])
                if not profiles: self.combo_profile.addItem("handheld - Portable Radio", "handheld")

            # Fetch Channels
            url_plan = f"http://{host}:{port}/api/commsplan/download"
            with urllib.request.urlopen(url_plan, timeout=4) as r:
                data = json.loads(r.read().decode())
                channels = data.get("channels", [])
                if channels:
                    self.mgr.channels.clear()
                    for c in channels:
                        ch_id = int(c["id"])
                        self.mgr.channels[ch_id] = ChannelDef(
                            id=ch_id, name=c["name"], frequency=c.get("frequency", ""),
                            passphrase=c.get("passphrase", ""),
                            description=c.get("description", ""), enabled=c.get("enabled", True)
                        )
                    self.mgr.save()

            self.lbl_status.setText(f"✓ Sync: {len(callsigns)} units, {len(profiles)} profiles")
            self.lbl_status.setStyleSheet(f"color: {C_SIGNAL};")
        except Exception as e:
            self.lbl_status.setText(f"Fetch Error: {e}")
            self.lbl_status.setStyleSheet(f"color: {C_PTT};")

    def _on_connect(self):
        callsign = self.combo_callsign.currentText().strip().upper()
        if not callsign: return
        self.result = {
            "host": self.edit_host.text().strip(),
            "web_port": int(self.edit_web_port.text().strip()),
            "node_id": self.edit_node.text().strip(),
            "callsign": callsign,
            "profile": self.combo_profile.currentData() or "handheld"
        }
        self.accept()
