import sys
import os
import argparse
import logging
import threading
import time
from pathlib import Path

import json
import urllib.request
import urllib.parse

# --- ENABLE TERMINAL LOGGING ---
import logging
logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
print("--- TACNET CLIENT STARTING ---")

# PySide6 Imports
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, 
    QLabel, QPushButton, QFrame, QScrollArea, QGraphicsDropShadowEffect,
    QDialog, QFormLayout, QLineEdit, QComboBox, QStackedWidget,
    QListWidget, QListWidgetItem, QSlider, QCheckBox, QGraphicsOpacityEffect
)
from PySide6.QtCore import Qt, QSize, QTimer, Signal, Slot, QEvent, QPointF, QRectF
from PySide6.QtGui import (QColor, QFont, QPalette, QIcon, QLinearGradient, 
                          QAction, QPainter, QPen, QRadialGradient, QPolygonF, QBrush)
import mgrs

# TacNet Core Imports
from radio_engine import RadioEngine
from radio_config import ConfigManager, DEFAULT_CONFIG_PATH
from radio_protocol import SAMPLE_RATE
from ui_components import SettingsDialog, ConnectionDialog

# --- UI CONSTANTS (AETHER DESIGN SYSTEM) ---
C_BG = "#0f172a"
C_RECESS = "#020617"
C_BORDER = "#1e293b"
C_ACCENT = "#38bdf8"
C_ACCENT_DIM = "#0369a1"
C_SIGNAL = "#22c55e"
C_PTT = "#ef4444"
C_TEXT = "#f8fafc"
C_TEXT_DIM = "#64748b"
C_GRID = "#0f172a"

AETHER_STYLE = f"""
QMainWindow {{
    background-color: {C_BG};
}}

QWidget#MainContainer {{
    background-color: {C_BG};
}}

QFrame#LCDPanel {{
    background-color: {C_RECESS};
    border: 2px solid {C_BORDER};
    border-radius: 12px;
}}

QLabel#ChannelDisplay {{
    color: {C_ACCENT};
    font-family: 'JetBrains Mono', 'Consolas', monospace;
    font-size: 42px;
    font-weight: bold;
    background: transparent;
}}

QLabel#FreqDisplay {{
    color: {C_ACCENT_DIM};
    font-family: 'JetBrains Mono', 'Consolas', monospace;
    font-size: 16px;
    background: transparent;
}}

QLabel#StatusLabel {{
    color: {C_TEXT_DIM};
    font-family: 'Inter', sans-serif;
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 1px;
}}

QPushButton#PTTButton {{
    background-color: {C_BORDER};
    color: {C_TEXT};
    border: 2px solid {C_ACCENT_DIM};
    border-radius: 20px;
    font-family: 'Inter', sans-serif;
    font-weight: 800;
    font-size: 18px;
    padding: 10px;
}}

QPushButton#PTTButton:pressed {{
    background-color: {C_PTT};
    border-color: #f87171;
    color: white;
}}

QPushButton#PTTButton[active="true"] {{
    background-color: {C_PTT};
    border-color: #f87171;
    color: white;
}}

QFrame#SignalMeterBG {{
    background-color: #020617;
    border-radius: 4px;
    height: 8px;
}}

QFrame#SignalMeterFill {{
    background-color: {C_SIGNAL};
    border-radius: 4px;
}}

QScrollArea {{
    border: none;
    background: transparent;
}}

QScrollBar:vertical {{
    border: none;
    background: {C_RECESS};
    width: 6px;
    margin: 0px;
}}

QScrollBar::handle:vertical {{
    background: {C_BORDER};
    min-height: 20px;
    border-radius: 3px;
}}

/* Dialog Styles */
QDialog {{
    background-color: {C_BG};
    color: {C_TEXT};
}}

QLineEdit, QComboBox {{
    background-color: {C_RECESS};
    color: {C_TEXT};
    border: 1px solid {C_BORDER};
    border-radius: 4px;
    padding: 5px;
    font-family: 'JetBrains Mono';
}}

QPushButton#SecondaryButton {{
    background-color: {C_BORDER};
    color: {C_TEXT};
    border-radius: 4px;
    padding: 8px;
}}

QPushButton#PrimaryButton {{
    background-color: {C_ACCENT};
    color: {C_BG};
    font-weight: bold;
    border-radius: 4px;
    padding: 8px;
}}

QFrame#ChannelItem[active="true"] {{
    border-color: {C_ACCENT};
    background-color: #1e293b;
}}

QFrame#ChannelItem:hover {{
    background-color: #111b2d;
    border-color: {C_ACCENT_DIM};
}}

QFrame#MapContainer {{
    background-color: {C_RECESS};
    border: 1px solid {C_BORDER};
    border-radius: 8px;
}}
"""

class TacticalPlotter(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.mgr = None 
        self.member_positions = {} 
        self.zoom_km = 10.0 
        self.setMinimumSize(300, 300)

    def paintEvent(self, event):
        import math
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        
        w = self.width()
        h = self.height()
        center = QPointF(w/2, h/2)
        radius = min(w, h) / 2 - 30
        
        # Background Grid (Radar circles)
        painter.setPen(QPen(QColor(30, 41, 59), 1, Qt.DashLine))
        for i in range(1, 4):
            r = radius * (i / 3)
            painter.drawEllipse(center, r, r)
            dist = self.zoom_km * (i / 3)
            painter.setPen(QPen(QColor(71, 85, 105), 1))
            painter.setFont(QFont("Inter", 8))
            painter.drawText(int(center.x() + 5), int(center.y() - r - 5), f"{dist:.1f}km")
            painter.setPen(QPen(QColor(30, 41, 59), 1, Qt.DashLine))

        # Crosshair
        painter.setPen(QPen(QColor(51, 65, 85), 1))
        painter.drawLine(int(center.x()), 30, int(center.x()), h-30)
        painter.drawLine(30, int(center.y()), w-30, int(center.y()))
        
        if not self.mgr: return
        my_lat, my_lon, _ = self.mgr.get_position()
        
        has_gps = (my_lat != 0 or my_lon != 0)
        if not has_gps:
            # Fallback: use a dummy center or last known if we wanted to be fancy,
            # but for now let's just warn and use 0,0 as center without hiding.
            painter.setPen(QPen(QColor(C_PTT), 1))
            painter.setFont(QFont("Inter", 8))
            painter.drawText(center.x() - 40, center.y() + radius + 20, "NO LOCAL GPS FIX")
            # We continue anyway so we can see others relative to 0,0 if needed,
            # OR we just center on the first member. For now, let's just 
            # prevent the 'return' so the map doesn't go blank.

        # Draw Self
        painter.setBrush(QColor(C_ACCENT))
        painter.setPen(QPen(QColor("white"), 1))
        painter.drawEllipse(center, 6, 6)
        
        # Draw Others
        now = time.time()
        for cs, pos in self.member_positions.items():
            his_lat, his_lon, _, ts = pos
            # Simple cartesian projection
            dy = (his_lat - my_lat) * 111.32
            dx = (his_lon - my_lon) * 111.32 * math.cos(math.radians(my_lat))
            
            dist = math.sqrt(dx*dx + dy*dy)
            if dist > self.zoom_km * 2.0: continue 
            
            scale = radius / self.zoom_km
            px = center.x() + dx * scale
            py = center.y() - dy * scale 
            
            age = now - ts
            is_stale = age > 60
            
            color = QColor(C_SIGNAL) if not is_stale else QColor(C_TEXT_DIM)
            painter.setBrush(color)
            painter.setPen(QPen(QColor("white"), 1))
            
            tri = QPolygonF([
                QPointF(px, py - 7),
                QPointF(px - 6, py + 5),
                QPointF(px + 6, py + 5)
            ])
            painter.drawPolygon(tri)
            
            # Label
            painter.setPen(QColor(C_TEXT))
            painter.setFont(QFont("JetBrains Mono", 8, QFont.Bold))
            painter.drawText(int(px + 8), int(py + 4), cs.upper())

    def mousePressEvent(self, event):
        if event.button() == Qt.RightButton and self.mgr:
            # Right click to copy MGRS
            # Find the AetherClient window to get its current MGRS string
            win = self.window()
            grid = win._last_mgrs if hasattr(win, '_last_mgrs') else "No Fix"
            
            from PySide6.QtGui import QGuiApplication
            QGuiApplication.clipboard().setText(grid)
            
            if hasattr(win, '_add_history'):
                win._add_history(f"MAP: Copied local MGRS {grid}")
        super().mousePressEvent(event)

class SpectrumWidget(QWidget):
    def __init__(self, engine=None, parent=None):
        super().__init__(parent)
        self.engine = engine
        self.setFixedHeight(60)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.update)
        self.timer.start(50)
        self.bars = 32
        self.levels = [0.0] * self.bars

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        
        w = self.width()
        h = self.height()
        bar_w = w / self.bars
        
        # Base audio level from engine
        base_level = 0.0
        if self.engine:
            if self.engine._tx_active:
                base_level = getattr(self.engine, '_tx_level', 0.0)
            else:
                base_level = self.engine.rx_audio_level
        
        import random
        for i in range(self.bars):
            # Add some jitter to make it look like a spectrum analyzer
            noise = random.uniform(0.1, 0.4) if base_level > 0.05 else random.uniform(0.0, 0.05)
            target = base_level * random.uniform(0.5, 1.5) + noise
            self.levels[i] = 0.4 * self.levels[i] + 0.6 * target
            
            bar_h = min(h, self.levels[i] * h * 1.5)
            
            rect = QRectF(i * bar_w + 1, h - bar_h, bar_w - 2, bar_h)
            grad = QLinearGradient(rect.topLeft(), rect.bottomLeft())
            if base_level > 0.01:
                grad.setColorAt(0, QColor(C_SIGNAL if not (self.engine and self.engine._tx_active) else C_PTT))
                grad.setColorAt(1, QColor(C_ACCENT_DIM))
            else:
                grad.setColorAt(0, QColor(C_TEXT_DIM))
                grad.setColorAt(1, QColor(C_RECESS))
            
            painter.setBrush(grad)
            painter.setPen(Qt.NoPen)
            painter.drawRoundedRect(rect, 1, 1)

class ChannelItem(QFrame):
    clicked = Signal(int)
    monitor_toggled = Signal(int, bool)

    def __init__(self, ch_id: int, name: str, freq: float, encrypted: bool = False, monitored: bool = False, is_primary: bool = False, parent=None):
        super().__init__(parent)
        self.ch_id = ch_id
        self.monitored = monitored
        self.is_primary = is_primary
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedHeight(50)
        self.setObjectName("ChannelItem")
        self._last_activity = 0
        self.update_style()
        
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 5, 10, 5)
        
        self.num_lbl = QLabel(f"{ch_id:02d}")
        self.num_lbl.setFixedSize(28, 28)
        self.num_lbl.setAlignment(Qt.AlignCenter)
        self.num_lbl.setStyleSheet(f"background: {C_BORDER}; color: {C_TEXT}; border-radius: 6px; font-weight: bold; font-size: 11px;")
        layout.addWidget(self.num_lbl)
        
        self.activity_dot = QFrame()
        self.activity_dot.setFixedSize(6, 6)
        self.activity_dot.setStyleSheet("background: transparent; border-radius: 3px;")
        layout.addWidget(self.activity_dot)
        
        info = QVBoxLayout()
        info.setSpacing(1)
        name_lbl = QLabel(name.upper())
        name_lbl.setStyleSheet(f"color: {C_TEXT}; font-size: 13px; font-weight: 800; font-family: 'Inter';")
        freq_lbl = QLabel(f"{freq:.3f} MHz")
        freq_lbl.setStyleSheet(f"color: {C_TEXT_DIM}; font-size: 11px; font-family: 'JetBrains Mono';")
        info.addWidget(name_lbl)
        info.addWidget(freq_lbl)
        layout.addLayout(info)
        layout.addStretch()
        
        if encrypted:
            lock = QLabel("🔒")
            lock.setStyleSheet("font-size: 10px; color: #aaa;")
            layout.addWidget(lock)

        # Monitor Button
        self.btn_mon = QPushButton("MON")
        self.btn_mon.setCheckable(True)
        self.btn_mon.setChecked(monitored)
        self.btn_mon.setFixedSize(55, 26)
        self.btn_mon.setCursor(Qt.PointingHandCursor)
        self._update_mon_style()
        self.btn_mon.clicked.connect(self._on_mon_clicked)
        layout.addWidget(self.btn_mon)

    def update_style(self):
        bg = "#1e293b" if self.is_primary else C_RECESS
        border = C_ACCENT if self.is_primary else C_BORDER
        self.setProperty("active", self.is_primary)
        self.style().unpolish(self)
        self.style().polish(self)
        
        self.setStyleSheet(f"""
            QFrame#ChannelItem {{
                background: {bg};
                border: 2px solid {border};
                border-radius: 10px;
            }}
        """)

    def set_activity(self, active: bool):
        if active:
            self.activity_dot.setStyleSheet(f"background: {C_SIGNAL}; border-radius: 3px; border: 1px solid white;")
            self._last_activity = time.time()
        else:
            if time.time() - self._last_activity > 2.0:
                self.activity_dot.setStyleSheet("background: transparent; border-radius: 3px;")

    def _update_mon_style(self):
        if self.btn_mon.isChecked():
            self.btn_mon.setStyleSheet(f"background: {C_SIGNAL}; color: black; font-size: 10px; font-weight: bold; border-radius: 4px;")
        else:
            self.btn_mon.setStyleSheet(f"background: {C_BORDER}; color: {C_TEXT_DIM}; font-size: 10px; border-radius: 4px;")

    def _on_mon_clicked(self):
        self._update_mon_style()
        self.monitor_toggled.emit(self.ch_id, self.btn_mon.isChecked())

    def mousePressEvent(self, event):
        # Only trigger primary change if clicking the main area, not the MON button
        if self.childAt(event.position().toPoint()) != self.btn_mon:
            self.clicked.emit(self.ch_id)

class LCDGrid(QFrame):
    def paintEvent(self, event):
        from PySide6.QtGui import QPainter, QPen
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        pen = QPen(QColor(255, 255, 255, 5), 1)
        painter.setPen(pen)
        
        step = 25
        for x in range(0, self.width(), step):
            painter.drawLine(x, 0, x, self.height())
        for y in range(0, self.height(), step):
            painter.drawLine(0, y, self.width(), y)

class ScanlineOverlay(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.opacity = 0.05
        
    def paintEvent(self, event):
        painter = QPainter(self)
        pen = QPen(QColor(255, 255, 255, 15), 1)
        painter.setPen(pen)
        for y in range(0, self.height(), 4):
            painter.drawLine(0, y, self.width(), y)

class WaveformVisualizer(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(40)
        self.points = [0.0] * 50
        self.color = QColor(C_ACCENT_DIM)
        self.active = False

    def update_level(self, level: float, active: bool = False):
        self.points.pop(0)
        self.points.append(level)
        self.active = active
        self.color = QColor(C_SIGNAL if active else C_ACCENT_DIM)
        self.update()

    def paintEvent(self, event):
        from PySide6.QtGui import QPainter, QPen
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        
        w = self.width()
        h = self.height()
        mid_y = h / 2
        step = w / (len(self.points) - 1)
        
        pen = QPen(self.color, 2)
        painter.setPen(pen)
        
        from PySide6.QtGui import QPainterPath
        path = QPainterPath()
        path.moveTo(0, mid_y)
        
        for i, val in enumerate(self.points):
            x = i * step
            # Scale value to half height
            v = val * (h / 2) * 2.0 
            if i % 2 == 0:
                path.lineTo(x, mid_y - v)
            else:
                path.lineTo(x, mid_y + v)
        
        painter.drawPath(path)

class SignalMeter(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(18)
        self.setMinimumWidth(120)
        self.level = 0.0
        self.blocks = 12

    def set_level(self, level: float):
        self.level = max(0.0, min(1.0, level))
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        
        w = self.width()
        h = self.height()
        block_w = (w - (self.blocks * 2)) / self.blocks
        
        active_blocks = int(self.level * self.blocks)
        
        for i in range(self.blocks):
            rect = QRectF(i * (block_w + 2), 0, block_w, h)
            if i < active_blocks:
                # Signal color based on position
                if i < self.blocks * 0.4: color = QColor("#ef4444") # Red
                elif i < self.blocks * 0.7: color = QColor("#eab308") # Yellow
                else: color = QColor("#22c55e") # Green
                painter.setBrush(color)
                painter.setPen(Qt.NoPen)
            else:
                painter.setBrush(QColor(30, 41, 59, 100))
                painter.setPen(QPen(QColor(51, 65, 85), 1))
            
            painter.drawRoundedRect(rect, 2, 2)

class MemberItem(QFrame):
    def __init__(self, callsign: str, parent=None):
        super().__init__(parent)
        self.callsign = callsign
        self.last_seen = time.time()
        self.setStyleSheet(f"background: transparent; border: none;")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 5, 10, 5)
        
        self.dot = QFrame()
        self.dot.setFixedSize(8, 8)
        self.dot.setStyleSheet("background: #334155; border-radius: 4px;")
        layout.addWidget(self.dot)
        
        self.lbl = QLabel(callsign.upper())
        self.lbl.setStyleSheet(f"color: {C_TEXT_DIM}; font-family: 'Inter'; font-size: 13px;")
        layout.addWidget(self.lbl)
        
        self.lbl_stale = QLabel("(STALE)")
        self.lbl_stale.setStyleSheet(f"color: {C_PTT}; font-family: 'Inter'; font-size: 10px; font-weight: bold;")
        self.lbl_stale.setVisible(False)
        layout.addWidget(self.lbl_stale)
        
        layout.addStretch()

        self.lbl_dist = QLabel("-- KM")
        self.lbl_dist.setStyleSheet(f"color: {C_ACCENT_DIM}; font-family: 'JetBrains Mono'; font-size: 11px;")
        layout.addWidget(self.lbl_dist)
        
    def set_active(self, active: bool):
        if active:
            self.dot.setStyleSheet(f"background: {C_SIGNAL}; border-radius: 4px; border: 1px solid #4ade80;")
            self.lbl.setStyleSheet(f"color: {C_TEXT}; font-family: 'Inter'; font-size: 13px; font-weight: bold;")
        else:
            self.dot.setStyleSheet("background: #334155; border-radius: 4px;")
            self.lbl.setStyleSheet(f"color: {C_TEXT_DIM}; font-family: 'Inter'; font-size: 13px;")

    def set_distance(self, dist_km: float):
        if dist_km < 0:
            self.lbl_dist.setText("-- KM")
        elif dist_km < 1.0:
            self.lbl_dist.setText(f"{int(dist_km * 1000)} M")
        else:
            self.lbl_dist.setText(f"{dist_km:.1f} KM")

    def set_stale(self, stale: bool):
        self.lbl_stale.setVisible(stale)
        if stale:
            self.lbl.setStyleSheet(f"color: {C_TEXT_DIM}; font-family: 'Inter'; font-size: 13px; opacity: 0.5;")
            self.lbl_dist.setStyleSheet(f"color: {C_BORDER}; font-family: 'JetBrains Mono'; font-size: 11px;")
        else:
            self.lbl.setStyleSheet(f"color: {C_TEXT}; font-family: 'Inter'; font-size: 13px; font-weight: bold;")
            self.lbl_dist.setStyleSheet(f"color: {C_ACCENT}; font-family: 'JetBrains Mono'; font-size: 11px;")

class AetherClient(QMainWindow):
    # Signals for thread-safe UI updates from the engine
    status_signal = Signal(str)
    ptt_signal = Signal(bool)
    rx_signal = Signal(str, bool) # callsign, active
    members_signal = Signal(list) # list of member dicts
    telemetry_signal = Signal(float, float) # rx_level, signal_strength
    profile_signal = Signal(str)
    pos_signal = Signal(str, float, float, float) # cs, lat, lon, alt
    assigned_channels_signal = Signal(list)
    activity_signal = Signal(int, bool)

    def __init__(self, engine: RadioEngine, config_mgr: ConfigManager):
        super().__init__()
        self.engine = engine
        self.mgr = config_mgr
        self.cfg = self.mgr.config
        self._current_rx_cs = None
        self._member_positions = {} # cs -> (lat, lon, alt)
        self._ptt_listener = None
        
        self.setWindowTitle("TacNet Aether | Radio v2")
        self.setMinimumSize(480, 750)
        self.setStyleSheet(AETHER_STYLE)
        
        self._m = mgrs.MGRS()
        self._last_mgrs = "00A AA 0000 0000"
        
        self._init_ui()
        self._connect_engine()
        self._setup_ptt_listener()
        
        # UI Update Timer (for smooth level meters)
        self.timer = QTimer()
        self.timer.timeout.connect(self._update_telemetry)
        self.timer.start(50) # 20 FPS

    def _init_ui(self):
        main_widget = QWidget()
        main_widget.setObjectName("MainContainer")
        self.setCentralWidget(main_widget)
        
        layout = QVBoxLayout(main_widget)
        layout.setContentsMargins(15, 15, 15, 15)
        layout.setSpacing(10)
        
        # --- TOP BAR (Callsign & Status) ---
        top_bar = QHBoxLayout()
        self.lbl_callsign = QLabel(self.cfg.callsign.upper())
        self.lbl_callsign.setStyleSheet(f"color: {C_TEXT}; font-family: 'Inter'; font-weight: bold; font-size: 18px;")
        top_bar.addWidget(self.lbl_callsign)
        
        top_bar.addStretch()
        
        # Settings Button
        self.settings_btn = QPushButton("⚙")
        self.settings_btn.setFixedSize(32, 32)
        self.settings_btn.setCursor(Qt.PointingHandCursor)
        self.settings_btn.setStyleSheet(f"""
            QPushButton {{
                background: {C_RECESS};
                border: 1px solid {C_BORDER};
                border-radius: 4px;
                color: {C_TEXT_DIM};
                font-size: 18px;
            }}
            QPushButton:hover {{ background: {C_BORDER}; color: {C_TEXT}; }}
        """)
        self.settings_btn.clicked.connect(self.show_settings)
        top_bar.addWidget(self.settings_btn)
        
        top_bar.addSpacing(15)
        
        self.lbl_link = QLabel("● LINK")
        self.lbl_link.setStyleSheet(f"color: {C_BORDER}; font-family: 'Inter'; font-weight: bold; font-size: 11px;")
        top_bar.addWidget(self.lbl_link)
        
        top_bar.addSpacing(10)
        
        self.lbl_reconnect = QLabel("LINK LOST")
        self.lbl_reconnect.setStyleSheet(f"color: {C_PTT}; font-family: 'Inter'; font-weight: bold; font-size: 11px;")
        self.lbl_reconnect.setVisible(False)
        top_bar.addWidget(self.lbl_reconnect)
        
        top_bar.addStretch()
        
        self.lbl_status = QLabel("OFFLINE")
        self.lbl_status.setObjectName("StatusLabel")
        top_bar.addWidget(self.lbl_status)
        layout.addLayout(top_bar)
        
        # --- LCD PANEL ---
        self.lcd_panel = QFrame()
        self.lcd_panel.setObjectName("LCDPanel")
        self.lcd_panel.setFixedHeight(180)
        
        shadow = QGraphicsDropShadowEffect()
        shadow.setBlurRadius(20)
        shadow.setColor(QColor(56, 189, 248, 40))
        shadow.setOffset(0, 0)
        self.lcd_panel.setGraphicsEffect(shadow)
        
        lcd_layout = QVBoxLayout(self.lcd_panel)
        lcd_layout.setContentsMargins(15, 15, 15, 15)
        
        self.lcd_grid = LCDGrid(self.lcd_panel)
        self.lcd_grid.setGeometry(0, 0, 1000, 1000)
        self.lcd_grid.setAttribute(Qt.WA_TransparentForMouseEvents)
        
        # Top Metadata
        top_info = QHBoxLayout()
        self.lbl_tx_mode = QLabel("STBY")
        self.lbl_tx_mode.setStyleSheet(f"color: {C_TEXT_DIM}; font-family: 'JetBrains Mono'; font-weight: bold; font-size: 10px;")
        top_info.addWidget(self.lbl_tx_mode)
        top_info.addStretch()
        
        self.lbl_hw_power = QLabel("5W")
        self.lbl_hw_power.setStyleSheet(f"color: {C_ACCENT}; font-family: 'JetBrains Mono'; font-size: 10px; font-weight: bold;")
        top_info.addWidget(self.lbl_hw_power)
        top_info.addSpacing(10)
        
        self.lbl_hw_sat = QLabel("SAT")
        self.lbl_hw_sat.setStyleSheet(f"color: {C_TEXT_DIM}; font-family: 'JetBrains Mono'; font-size: 10px;")
        top_info.addWidget(self.lbl_hw_sat)
        top_info.addSpacing(10)
        
        self.lbl_net_id = QLabel("NET ID: 000")
        self.lbl_net_id.setStyleSheet(f"color: {C_ACCENT_DIM}; font-family: 'JetBrains Mono'; font-size: 10px;")
        top_info.addWidget(self.lbl_net_id)
        top_info.addSpacing(10)
        self.lbl_enc = QLabel("AES-256")
        self.lbl_enc.setStyleSheet(f"color: {C_ACCENT_DIM}; font-family: 'JetBrains Mono'; font-size: 10px;")
        top_info.addWidget(self.lbl_enc)
        top_info.addSpacing(10)

        self.lbl_tak_status = QLabel("TAK: OFF")
        self.lbl_tak_status.setStyleSheet(f"color: {C_TEXT_DIM}; font-family: 'JetBrains Mono'; font-size: 10px; font-weight: bold;")
        top_info.addWidget(self.lbl_tak_status)
        top_info.addSpacing(10)

        self.lbl_jitter_depth = QLabel("BUF: 0")
        self.lbl_jitter_depth.setStyleSheet(f"color: {C_TEXT_DIM}; font-family: 'JetBrains Mono'; font-size: 10px; font-weight: bold;")
        top_info.addWidget(self.lbl_jitter_depth)
        
        lcd_layout.addLayout(top_info)
        
        self.scanlines = ScanlineOverlay(self.lcd_panel)
        self.scanlines.setGeometry(0, 0, 1000, 1000)
        self.scanlines.lower()
        self.lcd_grid.raise_()
        
        lcd_layout.addStretch()
        self.lbl_ch_num = QLabel(f"CH {self.cfg.current_channel:02d}")
        self.lbl_ch_num.setObjectName("ChannelDisplay")
        lcd_layout.addWidget(self.lbl_ch_num, alignment=Qt.AlignCenter)
        
        self.lbl_freq = QLabel("NET: GUARD / ADMIN")
        self.lbl_freq.setObjectName("FreqDisplay")
        lcd_layout.addWidget(self.lbl_freq, alignment=Qt.AlignCenter)
        
        lcd_layout.addStretch()
        
        # Device / Position Info Row
        pos_info = QHBoxLayout()
        self.lbl_hw_profile = QLabel("AN/PRC-152")
        self.lbl_hw_profile.setStyleSheet(f"color: {C_TEXT_DIM}; font-family: 'JetBrains Mono'; font-size: 10px; font-weight: bold;")
        pos_info.addWidget(self.lbl_hw_profile)
        pos_info.addStretch()
        
        self.lbl_gps_status = QLabel("GPS: LOST")
        self.lbl_gps_status.setStyleSheet(f"color: {C_PTT}; font-family: 'JetBrains Mono'; font-size: 9px; font-weight: bold;")
        pos_info.addWidget(self.lbl_gps_status)
        pos_info.addSpacing(10)
        
        self.lbl_mgrs = QLabel("00A AA 0000 0000")
        self.lbl_mgrs.setStyleSheet(f"color: {C_ACCENT}; font-family: 'JetBrains Mono'; font-size: 10px; font-weight: bold;")
        pos_info.addWidget(self.lbl_mgrs)
        lcd_layout.addLayout(pos_info)
        
        self.waveform = WaveformVisualizer()
        lcd_layout.addWidget(self.waveform)
        
        layout.addWidget(self.lcd_panel)
        
        # --- SIGNAL METER ---
        meter_layout = QVBoxLayout()
        meter_label_layout = QHBoxLayout()
        meter_label = QLabel("SIGNAL STRENGTH")
        meter_label.setObjectName("StatusLabel")
        meter_label_layout.addWidget(meter_label)
        meter_label_layout.addStretch()
        self.lbl_snr = QLabel("0.0 dB")
        self.lbl_snr.setObjectName("StatusLabel")
        meter_label_layout.addWidget(self.lbl_snr)
        meter_layout.addLayout(meter_label_layout)
        
        self.signal_meter = SignalMeter()
        meter_layout.addWidget(self.signal_meter)
        layout.addLayout(meter_layout)
        
        # --- RX MONITOR ---
        rx_box = QFrame()
        rx_box.setStyleSheet(f"background: {C_RECESS}; border-radius: 8px; border: 1px solid {C_BORDER};")
        rx_box.setFixedHeight(80)
        rx_layout = QHBoxLayout(rx_box)
        
        self.lbl_rx_from = QLabel("IDLE")
        self.lbl_rx_from.setStyleSheet(f"color: {C_TEXT_DIM}; font-family: 'JetBrains Mono'; font-size: 14px;")
        rx_layout.addWidget(self.lbl_rx_from, 1)
        
        self.rx_indicator = QFrame()
        self.rx_indicator.setFixedSize(16, 16)
        self.rx_indicator.setStyleSheet(f"background: #1e293b; border-radius: 8px;")
        rx_layout.addWidget(self.rx_indicator)
        layout.addWidget(rx_box)
        
        # --- TABS AREA ---
        self.stack = QStackedWidget()
        
        # TAB 1: RADIO (Channels)
        radio_tab = QWidget()
        radio_tab_layout = QVBoxLayout(radio_tab)
        radio_tab_layout.setContentsMargins(0, 0, 0, 0)
        
        self.spectrum = SpectrumWidget(self.engine)
        radio_tab_layout.addWidget(self.spectrum)
        
        mid_info = QHBoxLayout()
        self.freq_label = QLabel("000.000 MHz")
        self.freq_label.setStyleSheet("font-family: 'JetBrains Mono'; font-size: 22px; color: #4ade80; font-weight: bold;")
        self.mode_label = QLabel("FM / CLEAR")
        self.mode_label.setStyleSheet("font-family: 'Inter'; font-size: 11px; color: #94a3b8; font-weight: bold;")
        mid_info.addWidget(self.freq_label)
        mid_info.addStretch()
        mid_info.addWidget(self.mode_label)
        radio_tab_layout.addLayout(mid_info)
        
        self.channel_scroll = QScrollArea()
        self.channel_scroll.setWidgetResizable(True)
        self.channel_scroll.setStyleSheet("background: transparent; border: none;")
        self.channel_container = QWidget()
        self.channel_layout = QVBoxLayout(self.channel_container)
        self.channel_layout.setContentsMargins(0, 0, 5, 0)
        self.channel_layout.setSpacing(8)
        self.channel_layout.addStretch()
        self.channel_scroll.setWidget(self.channel_container)
        radio_tab_layout.addWidget(self.channel_scroll)
        
        self.stack.addWidget(radio_tab)
        
        # TAB 2: DEVICE (Hardware)
        device_tab = QWidget()
        device_layout = QVBoxLayout(device_tab)
        device_layout.setContentsMargins(10, 10, 10, 10)
        
        device_scroll = QScrollArea()
        device_scroll.setWidgetResizable(True)
        device_scroll.setStyleSheet("background: transparent; border: none;")
        device_content = QWidget()
        self.device_info_layout = QVBoxLayout(device_content)
        self.device_info_layout.setSpacing(15)
        
        device_scroll.setWidget(device_content)
        device_layout.addWidget(device_scroll)
        self.stack.addWidget(device_tab)
        
        # TAB 3: ROSTER (Members)
        roster_tab = QWidget()
        roster_layout = QVBoxLayout(roster_tab)
        roster_layout.setContentsMargins(0, 10, 0, 0)
        
        roster_label = QLabel("OPERATORS IN NET")
        roster_label.setStyleSheet(f"color: {C_TEXT_DIM}; font-size: 11px; font-weight: bold; margin-left: 10px;")
        roster_layout.addWidget(roster_label)
        
        self.member_scroll = QScrollArea()
        self.member_scroll.setWidgetResizable(True)
        self.member_container = QWidget()
        self.members_layout = QVBoxLayout(self.member_container)
        self.members_layout.setContentsMargins(0, 0, 5, 0)
        self.members_layout.setSpacing(5)
        self.members_layout.addStretch()
        self.member_scroll.setWidget(self.member_container)
        roster_layout.addWidget(self.member_scroll)
        
        self.stack.addWidget(roster_tab)
        
        # TAB 4: MAP
        map_tab = QWidget()
        map_layout = QVBoxLayout(map_tab)
        map_layout.setContentsMargins(10, 10, 10, 10)
        
        self.map_container = QFrame()
        self.map_container.setObjectName("MapContainer")
        self.map_container_layout = QVBoxLayout(self.map_container)
        
        self.plotter = TacticalPlotter()
        self.plotter.mgr = self.mgr
        self.plotter.member_positions = self._member_positions
        self.map_container_layout.addWidget(self.plotter)
        
        map_layout.addWidget(self.map_container)
        
        map_ctrls = QHBoxLayout()
        btn_zoom_in = QPushButton("+")
        btn_zoom_in.setFixedSize(30, 30)
        btn_zoom_in.clicked.connect(lambda: self._adjust_zoom(0.5))
        btn_zoom_out = QPushButton("-")
        btn_zoom_out.setFixedSize(30, 30)
        btn_zoom_out.clicked.connect(lambda: self._adjust_zoom(2.0))
        self.lbl_zoom = QLabel("ZOOM: 10KM")
        self.lbl_zoom.setStyleSheet(f"color: {C_ACCENT}; font-family: 'JetBrains Mono'; font-size: 10px; font-weight: bold;")
        
        map_ctrls.addWidget(btn_zoom_in)
        map_ctrls.addWidget(btn_zoom_out)
        map_ctrls.addStretch()
        map_ctrls.addWidget(self.lbl_zoom)
        map_layout.addLayout(map_ctrls)
        
        self.stack.addWidget(map_tab)

        # TAB 5: HISTORY (Logs)
        self.history_page = QWidget()
        history_layout = QVBoxLayout(self.history_page)
        history_layout.setContentsMargins(0, 10, 0, 0)
        
        history_header = QHBoxLayout()
        lbl_hist = QLabel("NET TRAFFIC LOG")
        lbl_hist.setObjectName("StatusLabel")
        history_header.addWidget(lbl_hist)
        history_header.addStretch()
        
        self.sync_plan_btn = QPushButton("SYNC PLAN")
        self.sync_plan_btn.setFixedSize(70, 20)
        self.sync_plan_btn.setStyleSheet(f"font-size: 9px; background: {C_ACCENT_DIM}; color: white; border-radius: 4px;")
        self.sync_plan_btn.clicked.connect(self._on_sync_clicked)
        history_header.addWidget(self.sync_plan_btn)
        
        self.clear_history_btn = QPushButton("CLEAR")
        self.clear_history_btn.setFixedSize(50, 20)
        self.clear_history_btn.setStyleSheet(f"font-size: 9px; background: {C_BORDER}; color: {C_TEXT_DIM}; border-radius: 4px;")
        self.clear_history_btn.clicked.connect(self._clear_history)
        history_header.addWidget(self.clear_history_btn)
        history_layout.addLayout(history_header)
        
        self.history_list = QListWidget()
        self.history_list.setStyleSheet(f"background: {C_RECESS}; border: 1px solid {C_BORDER}; color: {C_TEXT_DIM}; font-family: 'JetBrains Mono'; font-size: 11px; border-radius: 8px;")
        history_layout.addWidget(self.history_list)
        self.stack.addWidget(self.history_page)
        
        # Tab buttons
        self.tab_buttons = QHBoxLayout()
        self.btn_tab_radio = QPushButton("RADIO")
        self.btn_tab_device = QPushButton("DEVICE")
        self.btn_tab_roster = QPushButton("ROSTER")
        self.btn_tab_map = QPushButton("MAP")
        self.btn_tab_history = QPushButton("HISTORY")
        
        for b in [self.btn_tab_radio, self.btn_tab_device, self.btn_tab_roster, self.btn_tab_map, self.btn_tab_history]:
            b.setCheckable(True)
            b.setFixedHeight(35)
            b.setCursor(Qt.PointingHandCursor)
            self.tab_buttons.addWidget(b)
            
        self.btn_tab_radio.clicked.connect(lambda: self._switch_tab(0))
        self.btn_tab_device.clicked.connect(lambda: self._switch_tab(1))
        self.btn_tab_roster.clicked.connect(lambda: self._switch_tab(2))
        self.btn_tab_map.clicked.connect(lambda: self._switch_tab(3))
        self.btn_tab_history.clicked.connect(lambda: self._switch_tab(4))
        
        layout.addLayout(self.tab_buttons)
        layout.addWidget(self.stack)
        
        # --- SETTINGS ROW ---
        settings_row = QHBoxLayout()
        vol_layout = QVBoxLayout()
        vol_label = QLabel("VOL")
        vol_label.setObjectName("StatusLabel")
        vol_layout.addWidget(vol_label)
        self.slider_vol = QSlider(Qt.Horizontal)
        self.slider_vol.setRange(0, 200)
        self.slider_vol.setValue(int(self.cfg.output_volume * 100))
        self.slider_vol.setFixedWidth(100)
        self.slider_vol.setStyleSheet(f"QSlider::groove:horizontal {{ background: {C_RECESS}; height: 4px; }} QSlider::handle:horizontal {{ background: {C_ACCENT}; width: 12px; height: 12px; margin: -4px 0; border-radius: 6px; }}")
        self.slider_vol.valueChanged.connect(self._on_vol_changed)
        vol_layout.addWidget(self.slider_vol)
        settings_row.addLayout(vol_layout)
        
        settings_row.addStretch()
        
        vox_layout = QVBoxLayout()
        vox_label = QLabel("VOX")
        vox_label.setObjectName("StatusLabel")
        vox_layout.addWidget(vox_label, 0, Qt.AlignCenter)
        self.btn_vox = QPushButton("OFF")
        self.btn_vox.setCheckable(True)
        self.btn_vox.setChecked(self.cfg.vox_enabled)
        self.btn_vox.setFixedSize(50, 24)
        self._update_vox_style(self.cfg.vox_enabled)
        self.btn_vox.toggled.connect(self._on_vox_toggled)
        vox_layout.addWidget(self.btn_vox)
        settings_row.addLayout(vox_layout)
        layout.addLayout(settings_row)
        
        # --- PTT BUTTON ---
        self.btn_ptt = QPushButton("PUSH TO TALK")
        self.btn_ptt.setObjectName("PTTButton")
        self.btn_ptt.setFixedHeight(80)
        self.btn_ptt.setCursor(Qt.PointingHandCursor)
        self.btn_ptt.pressed.connect(self._on_ptt_pressed)
        self.btn_ptt.released.connect(self._on_ptt_released)
        layout.addWidget(self.btn_ptt)
        
        # --- FOOTER ---
        footer = QHBoxLayout()
        self.rtt_label = QLabel("RTT: -- ms")
        self.rtt_label.setStyleSheet("color: #555; font-size: 9px;")
        footer.addWidget(self.rtt_label)
        footer.addStretch()
        lbl_v = QLabel("AETHER v2.0-TAC")
        lbl_v.setStyleSheet(f"color: {C_TEXT_DIM}; font-size: 10px;")
        footer.addWidget(lbl_v)
        layout.addLayout(footer)
        
        self.member_widgets = {}
        self._populate_channel_list()
        self._switch_tab(0)

    def _connect_engine(self):
        self.engine.on_status_change = lambda msg: self.status_signal.emit(msg)
        self.engine.on_ptt_change = lambda active: self.ptt_signal.emit(active)
        self.engine.on_rx_start = lambda cs: self.rx_signal.emit(cs, True)
        self.engine.on_rx_end = lambda cs: self.rx_signal.emit(cs, False)
        self.engine.on_member_update = lambda members: self.members_signal.emit(members)
        self.engine.on_profile_change = lambda prof: self.profile_signal.emit(prof)
        self.engine.on_position_update = lambda cs, lat, lon, alt: self.pos_signal.emit(cs, lat, lon, alt)
        self.engine.on_assigned_channels_change = lambda chs: self.assigned_channels_signal.emit(chs)
        self.engine.on_channel_activity = lambda cid, act: self.activity_signal.emit(cid, act)
        
        self.status_signal.connect(self._update_status)
        self.ptt_signal.connect(self._update_ptt_ui)
        self.rx_signal.connect(self._update_rx_ui)
        self.members_signal.connect(self._update_members)
        self.profile_signal.connect(self._update_profile)
        self.pos_signal.connect(self._update_member_pos)
        self.assigned_channels_signal.connect(self._populate_channel_list)
        self.activity_signal.connect(self._on_activity_received)

    def _on_activity_received(self, channel_id, active):
        for i in range(self.channel_layout.count()):
            item = self.channel_layout.itemAt(i)
            if item.widget() and isinstance(item.widget(), ChannelItem):
                if item.widget().ch_id == channel_id:
                    item.widget().set_activity(active)

    def _setup_ptt_listener(self):
        """Initializes or restarts the global keyboard listener for PTT."""
        if self._ptt_listener:
            self._ptt_listener.stop()
            self._ptt_listener = None
            
        try:
            from pynput import keyboard
            
            def on_press(key):
                try:
                    kname = key.char if hasattr(key, 'char') else key.name
                    # Convert pynput names to our internal format if needed
                    if kname == "space": kname = "space" # pynput space is "space"
                    if kname == self.cfg.ptt_key:
                        self.engine.ptt_on()
                except: pass
                
            def on_release(key):
                try:
                    kname = key.char if hasattr(key, 'char') else key.name
                    if kname == self.cfg.ptt_key:
                        self.engine.ptt_off()
                except: pass
                
            self._ptt_listener = keyboard.Listener(on_press=on_press, on_release=on_release)
            self._ptt_listener.start()
            logging.info(f"Global PTT listener started: key='{self.cfg.ptt_key}'")
        except ImportError:
            logging.warning("pynput not found; global hotkeys disabled.")
        except Exception as e:
            logging.error(f"Failed to start PTT listener: {e}")

    def _update_status(self, msg):
        is_connected = "Connected" in msg
        is_reconnecting = "Reconnecting" in msg
        if is_connected:
            self.lbl_status.setText("ONLINE")
            self.lbl_status.setStyleSheet(f"color: {C_SIGNAL}; font-family: 'Inter'; font-weight: bold;")
            self.lbl_link.setStyleSheet(f"color: {C_SIGNAL}; font-family: 'Inter'; font-weight: bold; font-size: 11px;")
        elif is_reconnecting:
            self.lbl_status.setText("RECONNECTING...")
            self.lbl_status.setStyleSheet(f"color: {C_PTT}; font-family: 'Inter'; font-weight: bold;")
            self.lbl_link.setStyleSheet(f"color: {C_BORDER}; font-family: 'Inter'; font-weight: bold; font-size: 11px;")
        else:
            self.lbl_status.setText("OFFLINE")
            self.lbl_status.setStyleSheet(f"color: {C_TEXT_DIM}; font-family: 'Inter'; font-weight: bold;")
            self.lbl_link.setStyleSheet(f"color: {C_BORDER}; font-family: 'Inter'; font-weight: bold; font-size: 11px;")
        self.lbl_reconnect.setVisible(is_reconnecting)

    @Slot(bool)
    def _update_ptt_ui(self, active):
        if active:
            self.btn_ptt.setText("TRANSMITTING...")
            self.btn_ptt.setStyleSheet(f"background-color: {C_PTT}; color: white; border-color: #f87171;")
        else:
            self.btn_ptt.setText("PUSH TO TALK")
            self.btn_ptt.setStyleSheet("")

    @Slot(str, bool)
    def _update_rx_ui(self, callsign, active):
        try:
            if active:
                self._current_rx_cs = callsign.upper()
                self.lbl_rx_from.setText(f"RX {self._current_rx_cs}")
                self.lbl_rx_from.setStyleSheet(f"color: {C_ACCENT}; font-family: 'JetBrains Mono'; font-weight: bold;")
                self.rx_indicator.setStyleSheet(f"background: {C_SIGNAL}; border-radius: 8px; border: 2px solid #4ade80;")
            else:
                self._current_rx_cs = None
                self.lbl_rx_from.setText("IDLE")
                self.lbl_rx_from.setStyleSheet(f"color: {C_TEXT_DIM}; font-family: 'JetBrains Mono';")
                self.rx_indicator.setStyleSheet(f"background: #1e293b; border-radius: 8px;")
            
            if callsign in self.member_widgets:
                self.member_widgets[callsign].set_active(active)
        except: pass

    @Slot(list)
    def _update_members(self, members):
        for cs, widget in self.member_widgets.items():
            self.members_layout.removeWidget(widget)
            widget.deleteLater()
        self.member_widgets.clear()
        for m in members:
            cs = m.get("callsign", "UNKNOWN")
            # Ingest position from member list if provided
            lat = m.get("lat", 0.0)
            lon = m.get("lon", 0.0)
            if lat != 0 and lon != 0:
                # Don't overwrite more recent POSITION_UPDATEs if they exist
                if cs not in self._member_positions or (time.time() - self._member_positions[cs][3] > 10):
                    self._member_positions[cs] = (lat, lon, 0.0, time.time())
            
            item = MemberItem(cs)
            self.member_widgets[cs] = item
            self.members_layout.insertWidget(0, item)
            # Apply known position
            if cs in self._member_positions:
                self._update_member_dist(cs)

    @Slot(str, float, float, float)
    def _update_member_pos(self, cs, lat, lon, alt):
        # Ignore invalid/zero updates if we already have data
        if lat == 0 and lon == 0 and cs in self._member_positions:
            return
        self._member_positions[cs] = (lat, lon, alt, time.time())
        self._update_member_dist(cs)
        self.plotter.update() # Force map repaint
        if cs in self.member_widgets:
            self.member_widgets[cs].set_stale(False)

    def _update_member_dist(self, cs):
        if cs not in self.member_widgets: return
        my_lat, my_lon, _ = self.mgr.get_position()
        if my_lat == 0 and my_lon == 0:
            self.member_widgets[cs].lbl_dist.setText("NO FIX")
            return
        
        pos_data = self._member_positions[cs]
        his_lat, his_lon, _ = pos_data[0], pos_data[1], pos_data[2]
        dist = self._haversine(my_lat, my_lon, his_lat, his_lon)
        self.member_widgets[cs].set_distance(dist)
        
        # Check staleness
        age = time.time() - pos_data[3]
        if age > 60:
            self.member_widgets[cs].set_stale(True)
        else:
            self.member_widgets[cs].set_stale(False)

    def _haversine(self, lat1, lon1, lat2, lon2):
        import math
        R = 6371.0
        dlat = math.radians(lat2 - lat1)
        dlon = math.radians(lon2 - lon1)
        a = math.sin(dlat/2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon/2)**2
        c = 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))
        return R * c

    @Slot(str)
    def _update_profile(self, prof):
        self.lbl_hw_profile.setText(prof.upper())
        self._update_device_tab()

    def _on_vol_changed(self, val):
        vol = val / 100.0
        self.cfg.output_volume = vol
        self.engine._effects.update(volume=vol)

    def _on_vox_toggled(self, checked):
        self.cfg.vox_enabled = checked
        self._update_vox_style(checked)

    def _update_vox_style(self, active):
        if active:
            self.btn_vox.setText("ON")
            self.btn_vox.setStyleSheet(f"background: {C_SIGNAL}; color: black; font-weight: bold; border-radius: 4px;")
        else:
            self.btn_vox.setText("OFF")
            self.btn_vox.setStyleSheet(f"background: {C_BORDER}; color: {C_TEXT_DIM}; border-radius: 4px;")

    def _populate_channel_list(self, _ignored=None):
        # Cleanly remove old widgets
        while self.channel_layout.count() > 1:
            item = self.channel_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        
        assigned = self.engine._assigned_channels
        for ch_id in sorted(self.mgr.channels.keys()):
            # Filter: only show assigned channels if the set is not empty
            if assigned and ch_id not in assigned:
                continue
                
            ch_def = self.mgr.channels[ch_id]
            try: freq = float(ch_def.frequency.split(" ")[0])
            except: freq = 446.000 + (ch_id * 0.0125)
            is_primary = (ch_id == self.cfg.current_channel)
            is_mon = (ch_id in self.engine._monitored_channels)
            item = ChannelItem(ch_id, ch_def.name, freq, bool(ch_def.passphrase), is_mon, is_primary)
            item.clicked.connect(self._on_channel_selected)
            item.monitor_toggled.connect(self._on_monitor_toggled)
            self.channel_layout.insertWidget(self.channel_layout.count()-1, item)

    def _on_channel_selected(self, ch_id):
        try:
            self.engine.change_channel(ch_id)
            self._populate_channel_list()
        except Exception as e:
            logging.error(f"Error selecting channel: {e}")

    def _on_monitor_toggled(self, ch_id, enabled):
        if enabled: self.engine.monitor_channel(ch_id)
        else: self.engine.unmonitor_channel(ch_id)

    def _switch_tab(self, index):
        self.stack.setCurrentIndex(index)
        buttons = [self.btn_tab_radio, self.btn_tab_device, self.btn_tab_roster, self.btn_tab_map, self.btn_tab_history]
        for i, b in enumerate(buttons):
            active = (i == index)
            b.setChecked(active)
            if active: b.setStyleSheet(f"background: {C_ACCENT_DIM}; color: {C_TEXT}; border-bottom: 2px solid {C_ACCENT}; font-weight: bold;")
            else: b.setStyleSheet(f"background: {C_RECESS}; color: {C_TEXT_DIM}; border-bottom: 2px solid transparent;")
        if index == 1: self._update_device_tab()
        if index == 3: self.plotter.update()

    def _adjust_zoom(self, factor):
        self.plotter.zoom_km = max(1.0, min(100.0, self.plotter.zoom_km * factor))
        self.lbl_zoom.setText(f"ZOOM: {self.plotter.zoom_km:.1f}KM")
        self.plotter.update()

    def _update_device_tab(self):
        self._clear_layout(self.device_info_layout)
            
        prof_id = self.cfg.radio_profile
        prof = self.mgr.profiles.get(prof_id)
        if not prof: return
        
        title = QLabel(prof.name.upper())
        title.setStyleSheet(f"color: {C_ACCENT}; font-size: 16px; font-weight: 900; margin-bottom: 5px;")
        self.device_info_layout.addWidget(title)
        
        def add_spec(label, value):
            row = QHBoxLayout()
            lbl = QLabel(label.upper())
            lbl.setStyleSheet(f"color: {C_TEXT_DIM}; font-size: 11px; font-weight: bold;")
            val = QLabel(str(value).upper())
            val.setStyleSheet(f"color: {C_TEXT}; font-family: 'JetBrains Mono'; font-size: 12px;")
            row.addWidget(lbl)
            row.addStretch()
            row.addWidget(val)
            self.device_info_layout.addLayout(row)
            
        add_spec("Frequency Band", prof.freq_band)
        add_spec("TX Power (Max)", f"{prof.tx_power_w} Watts")
        if prof.satcom_capable: add_spec("SATCOM Power", f"{prof.tx_power_satcom_w} Watts")
        add_spec("Waveforms", prof.waveforms_nb)
        add_spec("Crypto", prof.crypto_type)
        add_spec("GPS", "Integrated" if prof.gps_capable else "None")
        add_spec("Battery", f"{prof.battery_life_hrs} Hours")
        if prof.relay_signature != "none":
            add_spec("Relay Sig", prof.relay_signature.upper())
        self.device_info_layout.addStretch()

    def _clear_layout(self, layout):
        if not layout: return
        while layout.count():
            child = layout.takeAt(0)
            if child.widget():
                child.widget().deleteLater()
            elif child.layout():
                self._clear_layout(child.layout())

    def _on_ptt_pressed(self):
        self.engine.ptt_on()

    def _on_ptt_released(self):
        self.engine.ptt_off()

    def _reconnect(self):
        self.engine.disconnect()
        time.sleep(0.5)
        threading.Thread(target=self.engine.connect, daemon=True).start()

    def _clear_history(self):
        self.history_list.clear()

    def _add_history(self, msg):
        ts = time.strftime("%H:%M:%S")
        self.history_list.insertItem(0, QListWidgetItem(f"[{ts}] {msg}"))

    def closeEvent(self, event):
        """Handle clean shutdown."""
        self.engine.disconnect()
        event.accept()

    def _on_sync_clicked(self):
        host, port = self.cfg.server_host, self.cfg.web_admin_port
        try:
            from radio_config import ChannelDef
            url = f"http://{host}:{port}/api/commsplan/download"
            with urllib.request.urlopen(url, timeout=4) as r:
                channels = json.loads(r.read().decode()).get("channels", [])
                if channels:
                    self.mgr.channels.clear()
                    for c in channels:
                        cid = int(c["id"])
                        self.mgr.channels[cid] = ChannelDef(id=cid, name=c["name"], frequency=c.get("frequency", ""), passphrase=c.get("passphrase", ""), enabled=c.get("enabled", True))
                    self.mgr.save()
                    self.engine.rebuild_codec()
                    self._populate_channel_list()
                    self._add_history(f"SYSTEM: Sync complete. {len(self.mgr.channels)} channels.")
        except Exception as e: self._add_history(f"SYSTEM: Sync failed: {e}")

    def _update_telemetry(self):
        rx_level = self.engine.rx_audio_level
        tx_level = getattr(self.engine, '_tx_level', 0.0)
        link_age = time.time() - getattr(self.engine, '_last_link_ts', 0.0)
        
        if link_age < 10.0:
            snr = max(self.engine._rx_signal, 0.4)
            self.lbl_link.setStyleSheet(f"color: {C_SIGNAL}; font-family: 'Inter'; font-weight: bold; font-size: 11px;")
        else:
            self.engine._rx_signal *= 0.95
            snr = self.engine._rx_signal
            self.lbl_link.setStyleSheet(f"color: {C_BORDER}; font-family: 'Inter'; font-weight: bold; font-size: 11px;")

        self.signal_meter.set_level(snr)
        self.lbl_snr.setText(f"{snr*100:.1f} %")
        
        # Fade channel activity dots
        for i in range(self.channel_layout.count()):
            item = self.channel_layout.itemAt(i)
            if item.widget() and isinstance(item.widget(), ChannelItem):
                item.widget().set_activity(False) # set_activity handles timeout internaly
        
        cur_ch = self.cfg.current_channel
        ch_def = self.mgr.channels.get(cur_ch)
        if ch_def:
            try: freq = float(ch_def.frequency.split(" ")[0])
            except: freq = 446.000 + (cur_ch * 0.0125)
            self.freq_label.setText(f"{freq:07.3f} MHz")
            self.lbl_ch_num.setText(f"CH {cur_ch:02d}")
            self.lbl_freq.setText(f"NET: {ch_def.name}")
            self.lbl_net_id.setText(f"NET ID: {ch_def.net_id or '000'}")
            
            # Update mode label based on active effects
            effects = self.engine._effects
            is_sim = effects.distortion > 0 or effects.noise_floor > 0
            modulation = getattr(ch_def, 'modulation', 'FM') or 'FM'
            mode_suffix = "SIM" if is_sim else "CLEAR"
            self.mode_label.setText(f"{modulation} / {mode_suffix}")
        
        rtt = self.engine._last_rtt_ms
        if rtt >= 0:
            self.rtt_label.setText(f"RTT: {rtt} ms")
            self.rtt_label.setStyleSheet("color: #4ade80; font-size: 9px;" if rtt <= 400 else "color: #f87171; font-size: 9px;")
        else: self.rtt_label.setText("RTT: -- ms")
        
        if self.engine._tx_active:
            self.waveform.update_level(tx_level, True)
            self.lbl_tx_mode.setText("TRANSMITTING")
            self.lbl_tx_mode.setStyleSheet(f"color: {C_PTT}; font-family: 'JetBrains Mono'; font-weight: bold;")
            try: self.lcd_panel.graphicsEffect().setColor(QColor(239, 68, 68, 60))
            except: pass
        elif self._current_rx_cs:
            self.waveform.update_level(rx_level, True)
            self.lbl_tx_mode.setText(f"RX {self._current_rx_cs}")
            self.lbl_tx_mode.setStyleSheet(f"color: {C_SIGNAL}; font-family: 'JetBrains Mono'; font-weight: bold;")
            try: self.lcd_panel.graphicsEffect().setColor(QColor(56, 189, 248, 60))
            except: pass
        else:
            self.waveform.update_level(0.001, False)
            self.lbl_tx_mode.setText("STBY")
            self.lbl_tx_mode.setStyleSheet(f"color: {C_TEXT_DIM}; font-family: 'JetBrains Mono';")
            try: self.lcd_panel.graphicsEffect().setColor(QColor(56, 189, 248, 20))
            except: pass
        # Update MGRS / GPS
        lat, lon, alt = self.mgr.get_position()
        if lat != 0 or lon != 0:
            self.lbl_gps_status.setText("GPS: FIX")
            self.lbl_gps_status.setStyleSheet(f"color: {C_SIGNAL}; font-family: 'JetBrains Mono'; font-size: 9px; font-weight: bold;")
            try:
                # Force 10m precision (4 digits per coord)
                m_str = self._m.toMGRS(lat, lon, MGRSPrecision=4)
                # Parse GZD (2 or 3 chars, e.g., '31U' or '3U')
                gzd_end = 0
                for i, char in enumerate(m_str):
                    if char.isalpha(): 
                        gzd_end = i + 1
                        break
                gzd = m_str[:gzd_end]
                sq = m_str[gzd_end:gzd_end+2]
                rest = m_str[gzd_end+2:]
                mid = len(rest) // 2
                easting, northing = rest[:mid], rest[mid:]
                mgrs_formatted = f"{gzd} {sq} {easting} {northing}"
                self.lbl_mgrs.setText(mgrs_formatted)
                self._last_mgrs = mgrs_formatted
            except Exception as e:
                logging.debug(f"MGRS conversion failed for {lat}, {lon}: {e}")
                self.lbl_mgrs.setText("GPS ERROR")
        else:
            self.lbl_gps_status.setText("GPS: LOST")
            self.lbl_gps_status.setStyleSheet(f"color: {C_PTT}; font-family: 'JetBrains Mono'; font-size: 9px; font-weight: bold;")
            self.lbl_mgrs.setText("NO GPS LOCK")
            
        prof = self.mgr.profiles.get(self.cfg.radio_profile)
        if prof:
            self.lbl_hw_profile.setText(prof.id.upper())
            self.lbl_hw_power.setText(f"{int(prof.tx_power_w)}W")
            if prof.satcom_capable:
                self.lbl_hw_sat.setStyleSheet(f"color: {C_ACCENT}; font-family: 'JetBrains Mono'; font-size: 10px; font-weight: bold;")
        
        # TAK Status
        if self.engine._tak and self.engine._tak.is_connected:
            self.lbl_tak_status.setText("TAK: ONLINE")
            self.lbl_tak_status.setStyleSheet(f"color: {C_SIGNAL}; font-family: 'JetBrains Mono'; font-size: 10px; font-weight: bold;")
        else:
            self.lbl_tak_status.setText("TAK: OFFLINE" if self.cfg.tak_enabled else "TAK: DISABLED")
            self.lbl_tak_status.setStyleSheet(f"color: {C_PTT if self.cfg.tak_enabled else C_TEXT_DIM}; font-family: 'JetBrains Mono'; font-size: 10px; font-weight: bold;")
        
        # Jitter Buffer depth
        j_depth = self.engine.jitter_depth
        self.lbl_jitter_depth.setText(f"BUF: {j_depth}")
        if j_depth > 10:
            self.lbl_jitter_depth.setStyleSheet(f"color: {C_PTT}; font-family: 'JetBrains Mono'; font-size: 10px; font-weight: bold;")
        elif j_depth > 0:
            self.lbl_jitter_depth.setStyleSheet(f"color: {C_ACCENT}; font-family: 'JetBrains Mono'; font-size: 10px; font-weight: bold;")
        else:
            self.lbl_jitter_depth.setStyleSheet(f"color: {C_TEXT_DIM}; font-family: 'JetBrains Mono'; font-size: 10px; font-weight: bold;")

    def show_settings(self):
        dlg = SettingsDialog(self.engine, self)
        if dlg.exec():
            self.lbl_callsign.setText(self.cfg.callsign)
            self._setup_ptt_listener() # Update hotkey listener
            self._update_profile(self.cfg.radio_profile)
            self._add_history("SYSTEM: Settings updated.")


def main():
    parser = argparse.ArgumentParser(description="TacNet Aether Radio Client v2")
    parser.add_argument("--callsign", help="Radio callsign")
    parser.add_argument("--server",   help="Server address (host:port)")
    parser.add_argument("--config",   default=None, help="Path to config file")
    parser.add_argument("--node-id",  help="Manual Node ID")
    parser.add_argument("--channel",  type=int, help="Starting channel ID")
    parser.add_argument("--remote",   action="store_true", help="Started by remote launcher")
    args = parser.parse_args()

    # Initialize Config
    cfg_path = Path(args.config) if args.config else DEFAULT_CONFIG_PATH
    mgr = ConfigManager(cfg_path)
    
    # Initialize UI App early
    app = QApplication(sys.argv)
    app.setApplicationName("TacNet Aether")

    # Start-up logic
    if not args.remote and not args.callsign:
        dialog = ConnectionDialog(mgr)
        if dialog.exec() == QDialog.Accepted:
            res = dialog.result
            mgr.config.server_host = res["host"]
            mgr.config.web_admin_port = res["web_port"]
            mgr.config.node_id = res["node_id"]
            mgr.config.callsign = res["callsign"]
            mgr.config.radio_profile = res["profile"]
            # server_port is already set in mgr.config by the dialog's fetch logic
            mgr.save() # Persist successful connection details
        else:
            return

    # Apply CLI Overrides (if any)
    if args.callsign:
        mgr.config.callsign = args.callsign
    
    if args.server:
        host, _, port_str = args.server.partition(":")
        mgr.config.server_host = host
        if port_str:
            port = int(port_str)
            # If port is 8890, assume it's the web port and use default UDP
            if port == 8890:
                mgr.config.web_admin_port = 8890
                # Don't overwrite server_port if it was already loaded
            else:
                # Assume provided port is the UDP voice port
                mgr.config.server_port = port
                
    if args.node_id:
        mgr.config.node_id = args.node_id
        
    if args.channel is not None:
        mgr.config.current_channel = args.channel

    # If we were started with specific settings (e.g. by launcher), 
    # ensure they are saved so they persist through restarts.
    if args.callsign or args.server or args.node_id or args.channel is not None:
        mgr.save()

    # Initialize Engine
    engine = RadioEngine(mgr)
    
    window = AetherClient(engine, mgr)
    window.show()
    
    # Start engine connection in background
    threading.Thread(target=engine.connect, daemon=True).start()
    
    sys.exit(app.exec())

if __name__ == "__main__":
    main()
