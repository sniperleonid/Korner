import os, sys, math, re, traceback, json, socket, subprocess, threading, urllib.error, urllib.request

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


def http_get_json(url: str, timeout: float = 0.2) -> Optional[dict]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, ValueError, json.JSONDecodeError):
        return None


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
        self._net_lock = threading.Lock()
        self._web_poll_busy = False
        self._status_ping_busy = False
        self._pending_web_payload = None
        self._pending_server_online = None

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
        lay = QVBoxLayout(self.tab_tactical)

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
        self._web_timer.setInterval(250)
        self._web_timer.timeout.connect(self._web_poll)
        self._web_timer.start()

        self._status_timer = QTimer(self)
        self._status_timer.setInterval(1200)
        self._status_timer.timeout.connect(self._server_update_status)
        self._status_timer.start()

        # auto start server
        self._server_process = None
        self._server_autostart_once()

    def _build_env_tab(self):
        lay = QVBoxLayout(self.tab_env)

        box = QGroupBox("Погода / ACE")
        form = QFormLayout(box)

        self.temp = QLineEdit("15")
        self.pressure = QLineEdit("760")
        self.wind_speed = QLineEdit("0")
        self.wind_dir = QLineEdit("0")
        self.wind_unit = QComboBox(); self.wind_unit.addItems(["м/с", "км/ч"])

        form.addRow("Температура, °C", self.temp)
        form.addRow("Давление, мм рт. ст.", self.pressure)
        form.addRow("Ветер, скорость", self.wind_speed)
        form.addRow("Ветер, направление (°)", self.wind_dir)
        form.addRow("Единицы ветра", self.wind_unit)

        lay.addWidget(box)
        lay.addStretch(1)

    def _build_weapons_tab(self):
        lay = QVBoxLayout(self.tab_weapons)

        gun_box = QGroupBox("Орудие")
        gun_form = QFormLayout(gun_box)

        self.mode = QComboBox(); self.mode.addItems(["Прямой", "Навесной"])
        self.arc = QComboBox(); self.arc.addItems(["НИЗКАЯ", "ВЫСОКАЯ"])
        self.weapon_name = QLineEdit("Default")

        gun_form.addRow("Профиль", self.weapon_name)
        gun_form.addRow("Режим", self.mode)
        gun_form.addRow("Траектория", self.arc)

        lay.addWidget(gun_box)
        lay.addStretch(1)

    def _build_settings_tab(self):
        lay = QVBoxLayout(self.tab_settings)

        calc_box = QGroupBox("Координаты")
        form = QFormLayout(calc_box)

        self.tx, self.ty = QLineEdit(), QLineEdit()
        self.ox, self.oy = QLineEdit(), QLineEdit()
        self.dx, self.dy = QLineEdit(), QLineEdit()

        self.tx.setPlaceholderText("X цели")
        self.ty.setPlaceholderText("Y цели")
        self.ox.setPlaceholderText("X наблюдателя")
        self.oy.setPlaceholderText("Y наблюдателя")
        self.dx.setPlaceholderText("X дрона")
        self.dy.setPlaceholderText("Y дрона")

        row_t = QWidget(); row_t_l = QHBoxLayout(row_t); row_t_l.setContentsMargins(0, 0, 0, 0); row_t_l.addWidget(self.tx); row_t_l.addWidget(self.ty)
        row_o = QWidget(); row_o_l = QHBoxLayout(row_o); row_o_l.setContentsMargins(0, 0, 0, 0); row_o_l.addWidget(self.ox); row_o_l.addWidget(self.oy)
        row_d = QWidget(); row_d_l = QHBoxLayout(row_d); row_d_l.setContentsMargins(0, 0, 0, 0); row_d_l.addWidget(self.dx); row_d_l.addWidget(self.dy)

        form.addRow("Цель", row_t)
        form.addRow("Наблюдатель", row_o)
        form.addRow("Дрон", row_d)

        self.lock_guns = QCheckBox("Блокировать автозаполнение орудий")
        lay.addWidget(calc_box)
        lay.addWidget(self.lock_guns)
        lay.addStretch(1)

    # --- Minimal fallbacks ---

    def load_tables(self, show_popup: bool = False):
        """Load ballistic tables from local ./tables folder when available."""
        tables_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tables")
        try:
            self.tables.load_folder(tables_dir)
            if show_popup:
                QMessageBox.information(self, "Таблицы", "Баллистические таблицы загружены.")
        except Exception as e:
            if show_popup:
                QMessageBox.warning(self, "Таблицы", f"Таблицы не загружены: {e}")

    def _restore_state(self):
        """Restore persisted UI state if available."""
        try:
            state = load_state()
        except Exception:
            return
        if not isinstance(state, dict):
            return
        for name in ("tx", "ty", "ox", "oy", "dx", "dy"):
            w = getattr(self, name, None)
            if w is not None and name in state:
                w.setText(str(state.get(name, "")))

    def _save_state(self):
        payload = {}
        for name in ("tx", "ty", "ox", "oy", "dx", "dy"):
            w = getattr(self, name, None)
            if w is not None:
                payload[name] = w.text().strip()
        try:
            save_state(payload)
        except Exception:
            pass

    def _parse_coord_field(self, w: QLineEdit, field_name: str) -> float:
        text = (w.text() or "").strip()
        if not text:
            raise ValueError(f"Поле '{field_name}' пустое.")
        try:
            return parse_coord_with_autoscale(text)
        except Exception:
            try:
                return float(text.replace(",", "."))
            except Exception as e:
                raise ValueError(f"Поле '{field_name}' заполнено неверно: {text}") from e

    def compute_selected(self):
        self._save_state()
        try:
            gun = Point2D(self._parse_coord_field(self.ox, "Орудие X"), self._parse_coord_field(self.oy, "Орудие Y"))
            target = Point2D(self._parse_coord_field(self.tx, "Цель X"), self._parse_coord_field(self.ty, "Цель Y"))

            dist = distance_2d(gun, target)
            bearing = bearing_rad_from_north(gun, target)
            az_mil = rad_to_mil(bearing)
            az_deg = mil_to_deg(az_mil)

            wind_speed = parse_float(self.wind_speed.text(), 0.0)
            if self.wind_unit.currentText() == "км/ч":
                wind_speed /= 3.6
            wind_dir = parse_float(self.wind_dir.text(), 0.0)
            wx, wy = wind_components_from_speed_dir(wind_speed, wind_dir)
            wind_ff = rotate_world_to_fireframe(wx, wy, bearing)

            req = SolveRequest(
                target_x_m=float(dist),
                target_y_m=0.0,
                target_z_m=0.0,
                wind_ff=wind_ff,
                arc="LOW" if self.arc.currentText() == "НИЗКАЯ" else "HIGH",
                direct_fire=(self.mode.currentText() == "Прямой"),
                tolerance_m=8.0,
                dt=0.02,
            )
            best = suggest_best(req, self.weapon, self.tables)
            if best is None:
                raise RuntimeError("Не удалось подобрать решение для выбранного режима.")

            summary = (
                f"Дист: {dist:.0f} м | Азимут: {az_mil:.1f} mil ({az_deg:.1f}°){NL}"
                f"Заряд: {best.charge} | УВН: {best.elev_mil:.1f} mil | TOF: {best.tof:.1f} c{NL}"
                f"Промах(оценка): {best.miss_total_m:.1f} м"
            )
            self.status.setText(f"Готово: {self.mode.currentText()} / {self.arc.currentText()}")
            self.out.setText(summary.replace(NL, " | "))
            self.sol_win.set_text(summary)
            self.sol_win.show()
            self.progress.setValue(100)
        except Exception as e:
            self.status.setText(f"Ошибка расчёта: {e}")
            self.progress.setValue(0)

    def apply_corr_and_compute(self):
        self.compute_selected()

    def _web_open(self):
        url = self.web_url.text().strip() if hasattr(self, "web_url") else "http://127.0.0.1:8000"
        if not url:
            return
        if not url.endswith("/"):
            url += "/"
        webbrowser.open(url)

    def _server_ping(self) -> bool:
        base = self.web_url.text().strip() if hasattr(self, "web_url") else ""
        if not base:
            return False
        if not base.endswith("/"):
            base += "/"
        try:
            data = http_get_json(base + "api/ping", timeout=0.2)
            return bool(data and data.get("ok"))
        except Exception:
            return False

    def _server_start_clicked(self):
        if self._server_ping():
            self._server_update_status()
            return

        if getattr(self, "_server_process", None) and self._server_process.poll() is None:
            self._server_update_status()
            return

        cmd = [sys.executable, "-m", "map_server.standalone_server", "--host", "0.0.0.0", "--port", "8000"]
        try:
            self._server_process = subprocess.Popen(
                cmd,
                cwd=os.path.dirname(os.path.abspath(__file__)),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception as e:
            QMessageBox.warning(self, "Ошибка старта", f"Не удалось запустить map-server: {e}")
            self._server_process = None
        self._server_update_status()

    def _server_stop_clicked(self):
        p = getattr(self, "_server_process", None)
        if p and p.poll() is None:
            try:
                p.terminate()
                p.wait(timeout=2.0)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass
        self._server_process = None
        self._server_update_status()

    def _server_update_status(self):
        if self._pending_server_online is not None:
            online = self._pending_server_online
            self._pending_server_online = None
            self._apply_server_status_ui(online)

        if self._status_ping_busy:
            return

        self._status_ping_busy = True

        def ping_worker():
            online = self._server_ping()
            with self._net_lock:
                self._pending_server_online = online
            self._status_ping_busy = False

        threading.Thread(target=ping_worker, daemon=True).start()

    def _apply_server_status_ui(self, online: bool):
        if hasattr(self, "lbl_server_status"):
            self.lbl_server_status.setText("online" if online else "offline")
            self.lbl_server_status.setStyleSheet(
                "font-weight:600;color:#65d46e;" if online else "font-weight:600;color:#ef6f6c;"
            )
        if hasattr(self, "btn_start_server"):
            self.btn_start_server.setEnabled(not online)
        if hasattr(self, "btn_stop_server"):
            self.btn_stop_server.setEnabled(online)

        if hasattr(self, "lbl_lan_hint"):
            host = "127.0.0.1"
            try:
                host = socket.gethostbyname(socket.gethostname())
            except Exception:
                pass
            self.lbl_lan_hint.setText(f"http://{host}:8000/" if online else "LAN: —")

    def _server_autostart_once(self):
        if self._server_ping():
            self._server_update_status()
            return
        self._server_start_clicked()

    def _web_poll(self):
        # Consume payload prepared by background worker (if any)
        with self._net_lock:
            pending = self._pending_web_payload
            self._pending_web_payload = None
        if pending is not None:
            self._web_process_payload(pending)

        # Start next non-blocking fetch (if previous finished)
        if self._web_poll_busy:
            return
        self._web_poll_busy = True
        base = self.web_url.text().strip() if hasattr(self, "web_url") else ""

        def worker():
            payload = None
            req_base = base
            if req_base:
                if not req_base.endswith("/"):
                    req_base += "/"
                try:
                    payload = http_get_json(req_base + "api/last_click", timeout=0.2)
                    if payload is None:
                        payload = {"offline": True}
                except Exception:
                    payload = {"offline": True}
            with self._net_lock:
                self._pending_web_payload = payload
            self._web_poll_busy = False

        threading.Thread(target=worker, daemon=True).start()

    def _web_process_payload(self, r):
        base = self.web_url.text().strip() if hasattr(self, "web_url") else ""
        if not base:
            return
        if r and r.get("offline"):
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
