import os, sys, math, re, traceback, json

import requests
import webbrowser
from dataclasses import dataclass
from typing import List, Optional, Tuple

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QShortcut, QKeySequence
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QHBoxLayout, QVBoxLayout, QGroupBox, QFormLayout,
    QLineEdit, QPushButton, QLabel, QComboBox, QProgressBar, QCheckBox, QMessageBox,
    QFileDialog, QTabWidget, QSplitter
)

from utils import (
    parse_coord_with_autoscale, Point2D, distance_2d, bearing_rad_from_north,
    rad_to_mil, mil_to_deg, wind_components_from_speed_dir, rotate_world_to_fireframe
)
from models import SolveRequest
from solver import suggest_best
from weapon import Weapon
from table_cache import TableManager

from map_view import TacticalMapView, compute_similarity
from solution_window import SolutionWindow
from state_store import load_state, save_state

NL = chr(10)

def parse_float(s: str, default: float = 0.0) -> float:
    try:
        return float((s or "").strip().replace(",", "."))
    except Exception:
        return float(default)

def deg_to_rad(d: float) -> float:
    return d * math.pi / 180.0

def az_input_to_deg(val: float, unit_text: str) -> float:
    if "mil" in unit_text.lower():
        return (val % 6400.0) * 360.0 / 6400.0
    return val

def parse_correction(text: str) -> Tuple[float,float]:
    t=(text or "").strip().upper()
    if not t:
        return 0.0, 0.0
    t = t.replace("ПЕРЕЛЕТ","A").replace("НЕДОЛЕТ","D").replace("ПЕРЕЛЁТ","A").replace("НЕДОЛЁТ","D")
    pat = re.compile(r"([RLAD])\s*([+-]?\d+(?:[\.,]\d+)?)|([+-]?\d+(?:[\.,]\d+)?)\s*([RLAD])")
    right=0.0; add=0.0
    for m in pat.finditer(t):
        if m.group(1):
            k=m.group(1); v=float(m.group(2).replace(",", "."))
        else:
            v=float(m.group(3).replace(",", ".")); k=m.group(4)
        if k=="R": right += v
        elif k=="L": right -= v
        elif k=="A": add += v
        elif k=="D": add -= v
    return right, add

def unit_from_bearing(b: float):
    return math.sin(b), math.cos(b)

def right_from_bearing(b: float):
    return math.cos(b), -math.sin(b)

def intersect_bearings(p1: Point2D, b1_rad: float, p2: Point2D, b2_rad: float) -> Optional[Point2D]:
    # Lines: p + t*u, with u from bearing (north-based)
    u1 = Point2D(math.sin(b1_rad), math.cos(b1_rad))
    u2 = Point2D(math.sin(b2_rad), math.cos(b2_rad))
    # Solve p1 + t*u1 = p2 + s*u2
    # [u1 -u2] [t s]^T = (p2-p1)
    det = u1.x*(-u2.y) - u1.y*(-u2.x)
    if abs(det) < 1e-9:
        return None
    dx = p2.x - p1.x
    dy = p2.y - p1.y
    t = (dx*(-u2.y) - dy*(-u2.x)) / det
    return Point2D(p1.x + t*u1.x, p1.y + t*u1.y)

@dataclass
class GunInputs:
    x: QLineEdit
    y: QLineEdit
    h: QLineEdit
    label: str

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Артиллерийский калькулятор Arma Reforger — v29 (вкладки + вывод поверх игры)")
        self.resize(1500, 900)

        self.weapon = Weapon()
        self.tables = TableManager()
        self.guns: List[GunInputs] = []

        self.sol_win = SolutionWindow(self)

        # styles
        self.setStyleSheet("""
            QWidget { background: #0d0f12; color: #e6e6e6; font-size: 12px; }
            QGroupBox { border: 1px solid #2a2f36; margin-top: 10px; padding: 10px; border-radius: 6px; }
            QGroupBox::title { subcontrol-origin: margin; left: 8px; padding: 0 4px; color: #ffffff; font-weight: 700; }
            QLineEdit, QComboBox { background: #141a21; border: 1px solid #2a2f36; padding: 6px; border-radius: 4px; }
            QPushButton { background: #1a2028; border: 1px solid #3a4048; padding: 10px; border-radius: 6px; font-weight: 700; }
            QPushButton:hover { background: #222a34; }
            QPushButton:disabled { color: #888; background: #12161c; }
            QProgressBar { background: #141a21; border: 1px solid #2a2f36; border-radius: 4px; text-align: center; }
            QProgressBar::chunk { background: #2c6bff; }
            QCheckBox { spacing: 8px; }
            QTabWidget::pane { border: 1px solid #2a2f36; }
        """)

        # root with tabs
        root = QWidget(); self.setCentralWidget(root)
        main = QVBoxLayout(root)

        self.tabs = QTabWidget()
        main.addWidget(self.tabs)

        # bottom status
        self.progress = QProgressBar(); self.progress.setRange(0,100); self.progress.setValue(0)
        self.status = QLabel("—"); self.status.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.out = QLabel("—"); self.out.setTextInteractionFlags(Qt.TextSelectableByMouse)
        main.addWidget(self.progress)
        main.addWidget(self.status)
        main.addWidget(self.out)

        # --- Tabs ---
        self.tab_tactical = QWidget()
        self.tab_env = QWidget()
        self.tab_weapons = QWidget()
        self.tab_settings = QWidget()

        self.tabs.addTab(self.tab_tactical, "Тактика")
        self.tabs.addTab(self.tab_env, "Погода / ACE")
        self.tabs.addTab(self.tab_weapons, "Орудия / профили")
        self.tabs.addTab(self.tab_settings, "Настройки")

        self._build_tactical_tab()
        self._build_env_tab()
        self._build_weapons_tab()
        self._build_settings_tab()

        # hotkeys
        QShortcut(QKeySequence("Return"), self, activated=self.compute_selected)
        QShortcut(QKeySequence("Ctrl+Return"), self, activated=self.apply_corr_and_compute)
        QShortcut(QKeySequence("F1"), self, activated=lambda: self.mode.setCurrentText("Прямой"))
        QShortcut(QKeySequence("F2"), self, activated=lambda: (self.mode.setCurrentText("Навесной"), self.arc.setCurrentText("НИЗКАЯ")))
        QShortcut(QKeySequence("F3"), self, activated=lambda: (self.mode.setCurrentText("Навесной"), self.arc.setCurrentText("ВЫСОКАЯ")))

        # load tables
        self.load_tables(show_popup=False)

        # restore state
        self._restore_state()

    # --- BUILD TABS ---

    def _build_tactical_tab(self):
        tab = QWidget()
        lay = QVBoxLayout(tab)

        header = QLabel("Тактика: веб‑карта (LAN) в браузере.\n"
                        "Можно открыть карту на другом устройстве в той же сети.")
        header.setWordWrap(True)
        lay.addWidget(header)

        box = QGroupBox("Веб‑карта / Map‑server")
        form = QFormLayout(box)

        self.web_url = QLineEdit("http://127.0.0.1:8000")
        self.btn_open_map = QPushButton("Открыть карту")
        self.btn_start_server = QPushButton("Запустить сервер")
        self.btn_stop_server = QPushButton("Остановить сервер")
        self.btn_stop_server.setEnabled(False)

        self.lbl_server_status = QLabel("offline")
        self.lbl_server_status.setStyleSheet("font-weight:600;")

        self.lbl_lan_hint = QLabel("LAN: —")
        self.lbl_lan_hint.setTextInteractionFlags(Qt.TextSelectableByMouse)

        self.web_auto = QCheckBox("Автоподстановка кликов (dest) в поля калькулятора")
        self.web_auto.setChecked(True)

        form.addRow("URL", self.web_url)
        form.addRow(self.btn_open_map)
        form.addRow(self.btn_start_server, self.btn_stop_server)
        form.addRow("Статус", self.lbl_server_status)
        form.addRow("Открыть с телефона", self.lbl_lan_hint)
        form.addRow(self.web_auto)

        lay.addWidget(box)
        lay.addStretch(1)

        self.btn_open_map.clicked.connect(self._web_open)
        self.btn_start_server.clicked.connect(self._server_start_clicked)
        self.btn_stop_server.clicked.connect(self._server_stop_clicked)

        # timer: poll clicks + status
        self._web_last_ts = 0.0
        self._web_timer = QTimer(self)
        self._web_timer.setInterval(200)
        self._web_timer.timeout.connect(self._web_poll)
        self._web_timer.start()

        self._status_timer = QTimer(self)
        self._status_timer.setInterval(800)
        self._status_timer.timeout.connect(self._server_update_status)
        self._status_timer.start()

        # auto start server
        self._server_process = None
        self._server_autostart_once()

        self.tabs.addTab(tab, "Тактика")

    def _web_open(self):
        url = self.web_url.text().strip() if hasattr(self, "web_url") else "http://127.0.0.1:8000"
        if not url:
            return
        if not url.endswith("/"):
            url += "/"
        webbrowser.open(url)

    def _web_poll(self):
        # Read clicks from web-map and auto-fill coordinates
        base = self.web_url.text().strip() if hasattr(self, "web_url") else ""
        if not base:
            return
        if not base.endswith("/"):
            base += "/"
        try:
            r = requests.get(base + "api/last_click", timeout=0.25).json()
        except Exception:
            # offline
            if hasattr(self, "lbl_server_status"):
                self.lbl_server_status.setText("offline")
            if hasattr(self, "web_status"):
                self.web_status.setText("offline")
            return

        ts = 0.0
        try:
            ts = float(r.get("ts", 0.0))
        except Exception:
            ts = 0.0
        if ts <= getattr(self, "_web_last_ts", 0.0):
            return
        self._web_last_ts = ts

        x = r.get("x_m", None); y = r.get("y_m", None)
        dest = (r.get("dest","") or "").strip().lower()
        if x is None or y is None or not dest:
            return
        try:
            x = float(x); y = float(y)
        except Exception:
            return

        # status line
        msg = f"Клик: X={x:.1f} Y={y:.1f} {dest}"
        if hasattr(self, "web_status"):
            self.web_status.setText(msg)
        if hasattr(self, "web_status") is False and hasattr(self, "lbl_server_status"):
            self.lbl_server_status.setText(msg)

        if hasattr(self, "web_auto") and self.web_auto.isChecked():
            self._web_apply_dest(dest, x, y)

    def _web_apply_dest(self, dest: str, x_m: float, y_m: float):
        def set_xy(le_x, le_y):
            le_x.setText(str(int(round(x_m))))
            le_y.setText(str(int(round(y_m))))
        if dest == "target":
            set_xy(self.tx, self.ty); return
        if dest == "observer":
            set_xy(self.ox, self.oy); return
        if dest == "drone":
            set_xy(self.dx, self.dy); return
        if dest.startswith("gun"):
            try:
                idx = int(dest.replace("gun","")) - 1
            except Exception:
                return
            if 0 <= idx < len(self.guns):
                if hasattr(self, "lock_guns") and self.lock_guns.isChecked():
                    return
                set_xy(self.guns[idx].x, self.guns[idx].y)

def main():
    app = QApplication(sys.argv)
    w = MainWindow(); w.show()
    sys.exit(app.exec())

if __name__ == "__main__":
    main()
