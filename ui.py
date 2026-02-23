import os, sys, math, re, traceback, json, socket, subprocess, threading, urllib.error, urllib.request

import webbrowser
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QShortcut, QKeySequence
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QHBoxLayout, QVBoxLayout, QGroupBox, QFormLayout,
    QLineEdit, QPushButton, QLabel, QComboBox, QProgressBar, QCheckBox, QMessageBox,
    QFileDialog, QTabWidget, QSplitter, QListWidget, QDialog, QTextEdit
)

from utils import (
    parse_coord_with_autoscale, Point2D, distance_2d, bearing_rad_from_north,
    rad_to_mil, mil_to_deg, wind_components_from_speed_dir, rotate_world_to_fireframe
)
from models import SolveRequest
from solver import suggest_best
from weapon import Weapon, DEFAULT_WEAPON_CATALOG
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



class KnownPointsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Известные точки")
        self.resize(460, 340)
        lay = QVBoxLayout(self)
        self.list_widget = QListWidget()
        lay.addWidget(self.list_widget)

        row = QWidget()
        row_l = QHBoxLayout(row)
        row_l.setContentsMargins(0, 0, 0, 0)
        self.name = QLineEdit(); self.name.setPlaceholderText("Название")
        self.x = QLineEdit(); self.x.setPlaceholderText("X")
        self.y = QLineEdit(); self.y.setPlaceholderText("Y")
        row_l.addWidget(self.name); row_l.addWidget(self.x); row_l.addWidget(self.y)
        lay.addWidget(row)

        btn_row = QWidget()
        btn_l = QHBoxLayout(btn_row)
        btn_l.setContentsMargins(0, 0, 0, 0)
        self.btn_add = QPushButton("Добавить/обновить")
        self.btn_use = QPushButton("Выбрать как цель")
        self.btn_del = QPushButton("Удалить")
        btn_l.addWidget(self.btn_add); btn_l.addWidget(self.btn_use); btn_l.addWidget(self.btn_del)
        lay.addWidget(btn_row)

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Артиллерийский калькулятор Arma Reforger — v29 (вкладки + вывод поверх игры)")
        self.resize(1500, 900)

        self.weapon = Weapon()
        self.tables = TableManager()
        self.guns: List[GunInputs] = []
        self.battery_count = 5
        self.max_guns_per_battery = 5
        self.battery_guns_count: Dict[int, int] = {i: 5 for i in range(1, 6)}
        self.current_battery = 1
        self._net_lock = threading.Lock()
        self._web_poll_busy = False
        self._status_ping_busy = False
        self._pending_web_payload = None
        self._pending_server_online = None
        self.nfa_zones: List[dict] = []
        self.known_points: Dict[str, dict] = {}
        self.fire_history: List[str] = []
        self.last_solution_text: str = ""

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
        self.tab_fire = QWidget()
        self.tab_config = QWidget()
        self.tab_safety = QWidget()

        self.tabs.addTab(self.tab_tactical, "Тактика")
        self.tabs.addTab(self.tab_env, "Погода / ACE")
        self.tabs.addTab(self.tab_fire, "Fire Missions")
        self.tabs.addTab(self.tab_config, "Configuration")
        self.tabs.addTab(self.tab_safety, "Safety & Data")

        self._build_tactical_tab()
        self._build_env_tab()
        self._build_fire_tab()
        self._build_config_tab()
        self._build_safety_tab()

        self.known_points_dialog = KnownPointsDialog(self)
        self.known_points_dialog.btn_add.clicked.connect(self._add_known_point_from_dialog)
        self.known_points_dialog.btn_use.clicked.connect(self._apply_known_point_from_dialog)
        self.known_points_dialog.btn_del.clicked.connect(self._remove_known_point_from_dialog)

        # hotkeys
        QShortcut(QKeySequence("Return"), self, activated=self.compute_selected)
        QShortcut(QKeySequence("Ctrl+Return"), self, activated=self.apply_corr_and_compute)
        QShortcut(QKeySequence("F1"), self, activated=lambda: self.mode.setCurrentText("Прямой"))
        QShortcut(QKeySequence("F2"), self, activated=lambda: (self.mode.setCurrentText("Навесной"), self.arc.setCurrentText("НИЗКАЯ")))
        QShortcut(QKeySequence("F3"), self, activated=lambda: (self.mode.setCurrentText("Навесной"), self.arc.setCurrentText("ВЫСОКАЯ")))

        # load selected ballistic tables
        self._load_selected_profile_tables()

        # restore state
        self._restore_state()
        self._on_battery_changed()

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

    def _build_fire_tab(self):
        lay = QVBoxLayout(self.tab_fire)

        gun_box = QGroupBox("Орудие")
        gun_form = QFormLayout(gun_box)

        self.mode = QComboBox(); self.mode.addItems(["Прямой", "Навесной"])
        self.arc = QComboBox(); self.arc.addItems(["НИЗКАЯ", "ВЫСОКАЯ"])
        self.weapon_name = QComboBox()
        self.weapon_name.addItems([p.name for p in DEFAULT_WEAPON_CATALOG.values()])
        self.projectile_name = QComboBox()
        self._refresh_projectiles_for_weapon()
        self.weapon_name.currentIndexChanged.connect(self._refresh_projectiles_for_weapon)
        self.projectile_name.currentIndexChanged.connect(self._load_selected_profile_tables)

        gun_form.addRow("Профиль", self.weapon_name)
        gun_form.addRow("Снаряд", self.projectile_name)
        gun_form.addRow("Режим", self.mode)
        gun_form.addRow("Траектория", self.arc)

        lay.addWidget(gun_box)
        # mission settings
        mission_box = QGroupBox("Огневые решения / CFF")
        mission_form = QFormLayout(mission_box)
        self.mission_mode = QComboBox(); self.mission_mode.addItems(["По сетке", "От наблюдателя/дрона", "От известных точек", "Polar Plot"])
        self.sheaf_type = QComboBox(); self.sheaf_type.addItems(["Линейный", "Параллельный", "Схождение", "Круговой", "Открытый"])
        self.obs_az = QLineEdit("0")
        self.obs_angle = QLineEdit("0")
        self.drone_alt = QLineEdit("0")
        self.obs1_xy = QLineEdit(); self.obs1_xy.setPlaceholderText("X,Y")
        self.obs2_xy = QLineEdit(); self.obs2_xy.setPlaceholderText("X,Y (опц.)")
        mission_form.addRow("Режим миссии", self.mission_mode)
        mission_form.addRow("Тип снопа", self.sheaf_type)
        mission_form.addRow("Азимут наблюдателя", self.obs_az)
        mission_form.addRow("Угол/вертикаль", self.obs_angle)
        mission_form.addRow("Высота дрона", self.drone_alt)
        mission_form.addRow("Polar Plot: наблюдатель 1", self.obs1_xy)
        mission_form.addRow("Polar Plot: наблюдатель 2", self.obs2_xy)
        lay.addWidget(mission_box)

        hist_box = QGroupBox("История огня (10)")
        hist_lay = QVBoxLayout(hist_box)
        self.mission_history = QListWidget()
        self.btn_save_mission = QPushButton("Сохранить текущее решение")
        self.btn_save_mission.clicked.connect(self._save_current_solution_to_history)
        self.btn_clear_history = QPushButton("Очистить историю")
        self.btn_clear_history.clicked.connect(self._clear_mission_history)
        hist_lay.addWidget(self.mission_history)
        hist_lay.addWidget(self.btn_save_mission)
        hist_lay.addWidget(self.btn_clear_history)
        lay.addWidget(hist_box)
        lay.addStretch(1)

    def _build_config_tab(self):
        lay = QVBoxLayout(self.tab_config)

        org_box = QGroupBox("Батареи")
        org_form = QFormLayout(org_box)
        self.battery_select = QComboBox(); self.battery_select.addItems([f"Батарея {i}" for i in range(1, 6)])
        self.guns_in_battery = QComboBox(); self.guns_in_battery.addItems([str(i) for i in range(1, 6)])
        self.guns_in_battery.setCurrentText("5")
        org_form.addRow("Активная батарея", self.battery_select)
        org_form.addRow("Орудий в батарее", self.guns_in_battery)
        lay.addWidget(org_box)

        self.battery_select.currentIndexChanged.connect(self._on_battery_changed)
        self.guns_in_battery.currentTextChanged.connect(self._on_guns_count_changed)

        calc_box = QGroupBox("Координаты")
        form = QFormLayout(calc_box)

        self.tx, self.ty, self.tz = QLineEdit(), QLineEdit(), QLineEdit("0")
        self.ox, self.oy = QLineEdit(), QLineEdit()
        self.dx, self.dy = QLineEdit(), QLineEdit()

        self.tx.setPlaceholderText("X цели")
        self.ty.setPlaceholderText("Y цели")
        self.tz.setPlaceholderText("H цели")
        self.ox.setPlaceholderText("X наблюдателя")
        self.oy.setPlaceholderText("Y наблюдателя")
        self.dx.setPlaceholderText("X дрона")
        self.dy.setPlaceholderText("Y дрона")

        row_t = QWidget(); row_t_l = QHBoxLayout(row_t); row_t_l.setContentsMargins(0, 0, 0, 0); row_t_l.addWidget(self.tx); row_t_l.addWidget(self.ty)
        row_t_l.addWidget(self.tz)
        row_o = QWidget(); row_o_l = QHBoxLayout(row_o); row_o_l.setContentsMargins(0, 0, 0, 0); row_o_l.addWidget(self.ox); row_o_l.addWidget(self.oy)
        row_d = QWidget(); row_d_l = QHBoxLayout(row_d); row_d_l.setContentsMargins(0, 0, 0, 0); row_d_l.addWidget(self.dx); row_d_l.addWidget(self.dy)

        form.addRow("Цель", row_t)
        form.addRow("Наблюдатель", row_o)
        form.addRow("Дрон", row_d)

        guns_box = QGroupBox("Орудия активной батареи")
        guns_form = QFormLayout(guns_box)
        self.guns.clear()
        for idx in range(1, 6):
            gx = QLineEdit(); gy = QLineEdit(); gh = QLineEdit("0")
            gx.setPlaceholderText(f"X{idx}")
            gy.setPlaceholderText(f"Y{idx}")
            gh.setPlaceholderText(f"H{idx}")
            row = QWidget()
            row_l = QHBoxLayout(row)
            row_l.setContentsMargins(0, 0, 0, 0)
            row_l.addWidget(gx); row_l.addWidget(gy); row_l.addWidget(gh)
            guns_form.addRow(f"Орудие {idx}", row)
            self.guns.append(GunInputs(x=gx, y=gy, h=gh, label=f"gun{idx}"))

        self.lock_guns = QCheckBox("Блокировать автозаполнение орудий")

        kp_box = QGroupBox("Известные точки")
        kp_lay = QVBoxLayout(kp_box)
        self.known_points_list = QListWidget()
        self.btn_open_known_points = QPushButton("Открыть окно известных точек")
        self.btn_open_known_points.clicked.connect(self._open_known_points_dialog)
        self.btn_use_known_target = QPushButton("Выбрать выделенную как цель")
        self.btn_use_known_target.clicked.connect(self._apply_selected_known_point)
        kp_lay.addWidget(self.known_points_list)
        kp_lay.addWidget(self.btn_open_known_points)
        kp_lay.addWidget(self.btn_use_known_target)

        corr_box = QGroupBox("Окно корректировок")
        corr_form = QFormLayout(corr_box)
        self.corr_lr = QLineEdit("0")
        self.corr_ad = QLineEdit("0")
        self.corr_ref = QComboBox(); self.corr_ref.addItems(["От наблюдателя", "От орудия"])
        corr_form.addRow("Влево/вправо (м)", self.corr_lr)
        corr_form.addRow("Добавить/убавить (м)", self.corr_ad)
        corr_form.addRow("Система отсчета", self.corr_ref)

        lay.addWidget(calc_box)
        lay.addWidget(guns_box)
        lay.addWidget(self.lock_guns)
        lay.addWidget(kp_box)
        lay.addWidget(corr_box)
        self.btn_compute = QPushButton("Рассчитать для батареи")
        self.btn_compute.clicked.connect(self.compute_selected)
        lay.addWidget(self.btn_compute)
        lay.addStretch(1)

    def _build_safety_tab(self):
        lay = QVBoxLayout(self.tab_safety)
        nfa_box = QGroupBox("No Fire Areas (NFA)")
        nfa_form = QFormLayout(nfa_box)
        self.nfa_x = QLineEdit(); self.nfa_y = QLineEdit(); self.nfa_r = QLineEdit("150")
        self.nfa_list = QListWidget()
        self.cancel_on_nfa = QCheckBox("Отменять выстрел для орудия, если траектория пересекает NFA")
        self.cancel_on_nfa.setChecked(True)
        self.btn_add_nfa = QPushButton("Добавить NFA")
        self.btn_remove_nfa = QPushButton("Удалить выбранную NFA")
        self.btn_add_nfa.clicked.connect(self._add_nfa_zone)
        self.btn_remove_nfa.clicked.connect(self._remove_selected_nfa_zone)
        row = QWidget(); row_l = QHBoxLayout(row); row_l.setContentsMargins(0,0,0,0); row_l.addWidget(self.nfa_x); row_l.addWidget(self.nfa_y); row_l.addWidget(self.nfa_r)
        nfa_form.addRow("X/Y/R", row)
        nfa_form.addRow(self.btn_add_nfa, self.btn_remove_nfa)
        nfa_form.addRow(self.cancel_on_nfa)
        nfa_form.addRow(self.nfa_list)

        data_box = QGroupBox("Safety & Data")
        data_l = QVBoxLayout(data_box)
        self.btn_reset_runtime = QPushButton("Очистить все данные (кроме таблиц)")
        self.btn_reset_runtime.clicked.connect(self._reset_runtime_data)
        data_l.addWidget(self.btn_reset_runtime)

        lay.addWidget(nfa_box)
        lay.addWidget(data_box)
        lay.addStretch(1)

    def _active_weapon_key(self) -> str:
        keys = list(DEFAULT_WEAPON_CATALOG.keys())
        idx = max(0, self.weapon_name.currentIndex())
        return keys[min(idx, len(keys)-1)]

    def _refresh_projectiles_for_weapon(self):
        if not hasattr(self, "projectile_name"):
            return
        profile = DEFAULT_WEAPON_CATALOG[self._active_weapon_key()]
        self.projectile_name.blockSignals(True)
        self.projectile_name.clear()
        self.projectile_name.addItems([p.name for p in profile.projectiles])
        self.projectile_name.blockSignals(False)
        self._load_selected_profile_tables()

    def _load_selected_profile_tables(self):
        profile = DEFAULT_WEAPON_CATALOG[self._active_weapon_key()]
        pidx = max(0, self.projectile_name.currentIndex()) if hasattr(self, "projectile_name") else 0
        projectile = profile.projectiles[min(pidx, len(profile.projectiles)-1)]
        tables_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), projectile.table_folder)
        try:
            self.tables.load_folder(tables_dir)
            self.status.setText(f"Профиль: {profile.name} / {projectile.name}")
        except Exception as e:
            self.status.setText(f"Таблицы не загружены: {e}")

    def _on_battery_changed(self):
        self.current_battery = self.battery_select.currentIndex() + 1
        count = self.battery_guns_count.get(self.current_battery, 5)
        self.guns_in_battery.blockSignals(True)
        self.guns_in_battery.setCurrentText(str(count))
        self.guns_in_battery.blockSignals(False)
        self._refresh_gun_rows_enabled()

    def _on_guns_count_changed(self):
        self.battery_guns_count[self.current_battery] = max(1, int(self.guns_in_battery.currentText() or "1"))
        self._refresh_gun_rows_enabled()

    def _refresh_gun_rows_enabled(self):
        count = self.battery_guns_count.get(self.current_battery, 5)
        for idx, g in enumerate(self.guns, start=1):
            enabled = idx <= count
            g.x.setEnabled(enabled)
            g.y.setEnabled(enabled)
            g.h.setEnabled(enabled)

    def _clear_gun(self, idx: int):
        if 0 <= idx < len(self.guns):
            self.guns[idx].x.setText("")
            self.guns[idx].y.setText("")
            self.guns[idx].h.setText("0")

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
        for name in ("tx", "ty", "tz", "ox", "oy", "dx", "dy"):
            w = getattr(self, name, None)
            if w is not None and name in state:
                w.setText(str(state.get(name, "")))
        if "battery" in state:
            self.battery_select.setCurrentIndex(max(0, min(4, int(state.get("battery", 1)) - 1)))
        for b in range(1, 6):
            k = f"battery_{b}_count"
            if k in state:
                self.battery_guns_count[b] = max(1, min(5, int(state.get(k, 5))))
        guns_state = state.get("guns", {}) if isinstance(state.get("guns", {}), dict) else {}
        for b in range(1, 6):
            rows = guns_state.get(str(b), [])
            if not isinstance(rows, list):
                continue
            if b == self.current_battery:
                for idx, row in enumerate(rows[:5]):
                    if idx >= len(self.guns):
                        break
                    self.guns[idx].x.setText(str(row.get("x", "")))
                    self.guns[idx].y.setText(str(row.get("y", "")))
                    self.guns[idx].h.setText(str(row.get("h", "0")))
        self.nfa_zones = state.get("nfa_zones", []) if isinstance(state.get("nfa_zones", []), list) else []
        self.known_points = state.get("known_points", {}) if isinstance(state.get("known_points", {}), dict) else {}
        self.fire_history = state.get("fire_history", []) if isinstance(state.get("fire_history", []), list) else []
        if hasattr(self, "cancel_on_nfa"):
            self.cancel_on_nfa.setChecked(bool(state.get("cancel_on_nfa", True)))
        self._refresh_nfa_list() if hasattr(self, "nfa_list") else None
        self._refresh_known_points_ui()
        if hasattr(self, "mission_history"):
            self.mission_history.clear(); self.mission_history.addItems(self.fire_history[:10])

    def _save_state(self):
        payload = {}
        for name in ("tx", "ty", "tz", "ox", "oy", "dx", "dy"):
            w = getattr(self, name, None)
            if w is not None:
                payload[name] = w.text().strip()
        payload["battery"] = self.current_battery
        for b in range(1, 6):
            payload[f"battery_{b}_count"] = self.battery_guns_count.get(b, 5)
        payload["guns"] = {str(self.current_battery): [
            {"x": g.x.text().strip(), "y": g.y.text().strip(), "h": g.h.text().strip() or "0"}
            for g in self.guns
        ]}
        payload["nfa_zones"] = self.nfa_zones
        payload["known_points"] = self.known_points
        payload["fire_history"] = self.fire_history[:10]
        payload["cancel_on_nfa"] = self.cancel_on_nfa.isChecked() if hasattr(self, "cancel_on_nfa") else True
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

    def _sheaf_offsets(self) -> List[float]:
        sheaf = (self.sheaf_type.currentText() if hasattr(self, "sheaf_type") else "").lower()
        if "линей" in sheaf:
            return [-60.0, -30.0, 0.0, 30.0, 60.0]
        if "паралл" in sheaf:
            return [-25.0, 0.0, 25.0]
        if "схожд" in sheaf:
            return [-40.0, -15.0, 0.0, 15.0, 40.0]
        if "круг" in sheaf:
            return [0.0, 20.0, -20.0, 40.0, -40.0]
        return [0.0]

    def _targets_for_sheaf(self, gun_pt: Point2D, base_target: Point2D) -> List[Point2D]:
        bearing = bearing_rad_from_north(gun_pt, base_target)
        right = right_from_bearing(bearing)
        return [
            Point2D(base_target.x + right[0] * off, base_target.y + right[1] * off)
            for off in self._sheaf_offsets()
        ]

    def compute_selected(self):
        self._save_state()
        try:
            target = Point2D(self._parse_coord_field(self.tx, "Цель X"), self._parse_coord_field(self.ty, "Цель Y"))
            target_h = parse_float(self.tz.text(), 0.0)
            corr_lr = parse_float(self.corr_lr.text(), 0.0) if hasattr(self, "corr_lr") else 0.0
            corr_ad = parse_float(self.corr_ad.text(), 0.0) if hasattr(self, "corr_ad") else 0.0
            target = Point2D(target.x + corr_lr, target.y + corr_ad)

            wind_speed = parse_float(self.wind_speed.text(), 0.0)
            if self.wind_unit.currentText() == "км/ч":
                wind_speed /= 3.6
            wind_dir = parse_float(self.wind_dir.text(), 0.0)

            count = self.battery_guns_count.get(self.current_battery, 5)
            lines = [
                f"Миссия: {self.mission_mode.currentText()} | Сноп: {self.sheaf_type.currentText()}",
                f"Polar Plot O1={self.obs1_xy.text().strip() or '-'} O2={self.obs2_xy.text().strip() or '-'} | Аз={self.obs_az.text().strip() or '0'}",
                f"Батарея {self.current_battery} · орудий: {count}",
            ]
            best_line = None
            for idx, gun in enumerate(self.guns[:count], start=1):
                gx = (gun.x.text() or "").strip()
                gy = (gun.y.text() or "").strip()
                if not gx or not gy:
                    continue
                gun_pt = Point2D(self._parse_coord_field(gun.x, f"Орудие {idx} X"), self._parse_coord_field(gun.y, f"Орудие {idx} Y"))
                gun_h = parse_float(gun.h.text(), 0.0)

                sheaf_targets = self._targets_for_sheaf(gun_pt, target)
                active_targets: List[Point2D] = []
                blocked = 0
                for aim in sheaf_targets:
                    if self._line_intersects_nfa(gun_pt, aim):
                        blocked += 1
                        if self.cancel_on_nfa.isChecked():
                            continue
                    active_targets.append(aim)
                if blocked:
                    lines.append(f"Орудие {idx}: ⚠ пересечение NFA для {blocked}/{len(sheaf_targets)} траекторий")
                if not active_targets:
                    lines.append(f"Орудие {idx}: выстрел отменен (все траектории в NFA)")
                    continue

                for shot_idx, shot_target in enumerate(active_targets, start=1):
                    dist = distance_2d(gun_pt, shot_target)
                    bearing = bearing_rad_from_north(gun_pt, shot_target)
                    az_mil = rad_to_mil(bearing)
                    wx, wy = wind_components_from_speed_dir(wind_speed, wind_dir)
                    wind_ff = rotate_world_to_fireframe(wx, wy, bearing)
                    req = SolveRequest(
                        target_x_m=float(dist),
                        target_y_m=float(target_h - gun_h),
                        target_z_m=0.0,
                        wind_ff=wind_ff,
                        arc="LOW" if self.arc.currentText() == "НИЗКАЯ" else "HIGH",
                        direct_fire=(self.mode.currentText() == "Прямой"),
                        tolerance_m=8.0,
                        dt=0.02,
                    )
                    best = suggest_best(req, self.weapon, self.tables)
                    if best is None:
                        lines.append(f"Орудие {idx}/снаряд {shot_idx}: решение не найдено")
                        continue
                    lines.append(
                        f"Орудие {idx}/снаряд {shot_idx}: AZ {az_mil:.1f} mil | Заряд {best.charge} | УВН {best.elev_mil:.1f} mil | TOF {best.tof:.1f} c"
                    )
                    if best_line is None:
                        best_line = f"Орудие {idx}: AZ {az_mil:.1f} mil / УВН {best.elev_mil:.1f} mil"

            if len(lines) <= 2:
                raise RuntimeError("Нет введённых координат орудий в активной батарее.")

            summary = NL.join(lines)
            self.last_solution_text = summary
            self.status.setText(f"Готово: батарея {self.current_battery}")
            self.out.setText(summary.replace(NL, " | "))
            self.sol_win.set_text(summary)
            self.sol_win.show()
            self.progress.setValue(100)
            if best_line:
                self.lbl_server_status.setText(best_line)
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
                    click = http_get_json(req_base + "api/last_click", timeout=0.2)
                    state = http_get_json(req_base + "api/state", timeout=0.2)
                    if click is None and state is None:
                        payload = {"offline": True}
                    else:
                        payload = {"last_click": click or {}, "state": state or {}}
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

        click = r.get("last_click", r) if isinstance(r, dict) else {}
        state = r.get("state", {}) if isinstance(r, dict) else {}

        self._sync_guns_with_server_state(state)

        ts = 0.0
        try:
            ts = float(click.get("ts", 0.0))
        except Exception:
            ts = 0.0
        if ts <= getattr(self, "_web_last_ts", 0.0):
            return
        self._web_last_ts = ts

        x = click.get("x_m", None); y = click.get("y_m", None)
        dest = (click.get("dest","") or "").strip().lower()
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

    def _sync_guns_with_server_state(self, state_payload: dict):
        points = state_payload.get("points", {}) if isinstance(state_payload, dict) else {}
        self.known_points = state_payload.get("known_points", {}) if isinstance(state_payload.get("known_points", {}), dict) else self.known_points
        nfa_payload = state_payload.get("nfa_zones", []) if isinstance(state_payload, dict) else []
        if isinstance(nfa_payload, list):
            self.nfa_zones = [{"x_m": float(z.get("x_m", 0)), "y_m": float(z.get("y_m", 0)), "radius_m": float(z.get("radius_m", z.get("r", 0)))} for z in nfa_payload if isinstance(z, dict)]
            self._refresh_nfa_list()
        self._refresh_known_points_ui()
        known = set(points.keys())
        for idx in range(1, 6):
            dest = f"gun{idx}"
            if dest in known:
                p = points.get(dest) or {}
                try:
                    x = float(p.get("x_m"))
                    y = float(p.get("y_m"))
                except Exception:
                    continue
                if hasattr(self, "lock_guns") and self.lock_guns.isChecked():
                    continue
                if idx - 1 < len(self.guns):
                    self.guns[idx - 1].x.setText(str(int(round(x))))
                    self.guns[idx - 1].y.setText(str(int(round(y))))
            else:
                self._clear_gun(idx - 1)

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

    def _save_current_solution_to_history(self):
        if not self.last_solution_text:
            self.status.setText("Сначала выполните расчёт")
            return
        self._push_fire_history(self.last_solution_text)
        self.status.setText("Решение сохранено в историю")

    def _open_known_points_dialog(self):
        self._refresh_known_points_ui()
        self.known_points_dialog.show()
        self.known_points_dialog.raise_()
        self.known_points_dialog.activateWindow()

    def _add_known_point_from_dialog(self):
        name = (self.known_points_dialog.name.text() or "").strip()
        if not name:
            self.status.setText("Введите имя известной точки")
            return
        try:
            x = self._parse_coord_field(self.known_points_dialog.x, "Known Point X")
            y = self._parse_coord_field(self.known_points_dialog.y, "Known Point Y")
        except Exception as e:
            self.status.setText(f"Known Point ошибка: {e}")
            return
        self.known_points[name] = {"x_m": x, "y_m": y}
        self._refresh_known_points_ui()
        self._push_known_point_to_server(name, self.known_points[name])

    def _apply_known_point_from_dialog(self):
        self._apply_selected_known_point(from_dialog=True)

    def _remove_known_point_from_dialog(self):
        item = self.known_points_dialog.list_widget.currentItem()
        if not item:
            return
        name = item.text().split(" ")[0]
        if name in self.known_points:
            del self.known_points[name]
            self._refresh_known_points_ui()
            base = (self.web_url.text().strip() if hasattr(self, "web_url") else "").rstrip("/")
            if base:
                try:
                    req = urllib.request.Request(base + "/api/delete_known_point", data=json.dumps({"name": name}).encode("utf-8"), headers={"Content-Type": "application/json"}, method="POST")
                    urllib.request.urlopen(req, timeout=0.4).read()
                except Exception:
                    pass

    def _clear_mission_history(self):
        self.fire_history = []
        self.mission_history.clear()

    def _push_fire_history(self, text: str):
        self.fire_history.insert(0, text)
        self.fire_history = self.fire_history[:10]
        self.mission_history.clear()
        self.mission_history.addItems(self.fire_history)

    def _add_nfa_zone(self):
        try:
            x = self._parse_coord_field(self.nfa_x, "NFA X")
            y = self._parse_coord_field(self.nfa_y, "NFA Y")
            r = max(1.0, parse_float(self.nfa_r.text(), 150.0))
        except Exception as e:
            self.status.setText(f"NFA ошибка: {e}")
            return
        self.nfa_zones.append({"x_m": x, "y_m": y, "radius_m": r})
        self._refresh_nfa_list()
        self._push_nfa_to_server()

    def _remove_selected_nfa_zone(self):
        row = self.nfa_list.currentRow()
        if 0 <= row < len(self.nfa_zones):
            self.nfa_zones.pop(row)
            self._refresh_nfa_list()
            self._push_nfa_to_server()

    def _refresh_nfa_list(self):
        self.nfa_list.clear()
        for idx, z in enumerate(self.nfa_zones, start=1):
            self.nfa_list.addItem(f"#{idx} X={z['x_m']:.0f} Y={z['y_m']:.0f} R={z['radius_m']:.0f}")

    def _line_intersects_nfa(self, a: Point2D, b: Point2D) -> bool:
        for z in self.nfa_zones:
            cx, cy, r = float(z.get("x_m", 0)), float(z.get("y_m", 0)), float(z.get("radius_m", 0))
            abx = b.x - a.x
            aby = b.y - a.y
            if abs(abx) < 1e-6 and abs(aby) < 1e-6:
                continue
            t = ((cx - a.x) * abx + (cy - a.y) * aby) / max(1e-6, (abx*abx + aby*aby))
            t = max(0.0, min(1.0, t))
            px = a.x + t * abx
            py = a.y + t * aby
            if math.hypot(px - cx, py - cy) <= r:
                return True
        return False

    def _push_nfa_to_server(self):
        base = (self.web_url.text().strip() if hasattr(self, "web_url") else "").rstrip("/")
        if not base:
            return
        try:
            req = urllib.request.Request(base + "/api/set_nfa_zones", data=json.dumps({"zones": self.nfa_zones}).encode("utf-8"), headers={"Content-Type": "application/json"}, method="POST")
            urllib.request.urlopen(req, timeout=0.4).read()
        except Exception:
            pass

    def _push_known_point_to_server(self, name: str, p: dict):
        base = (self.web_url.text().strip() if hasattr(self, "web_url") else "").rstrip("/")
        if not base:
            return
        try:
            req = urllib.request.Request(
                base + "/api/set_known_point",
                data=json.dumps({"name": name, "x_m": float(p.get("x_m", 0)), "y_m": float(p.get("y_m", 0))}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            urllib.request.urlopen(req, timeout=0.4).read()
        except Exception:
            pass

    def _save_current_target_as_known_point(self):
        name = (self.known_points_dialog.name.text() or "").strip()
        if not name:
            self.status.setText("Введите имя известной точки")
            return
        try:
            x = self._parse_coord_field(self.tx, "Цель X")
            y = self._parse_coord_field(self.ty, "Цель Y")
        except Exception as e:
            self.status.setText(f"Ошибка known point: {e}")
            return
        self.known_points[name] = {"x_m": x, "y_m": y}
        self._refresh_known_points_ui()
        self._push_known_point_to_server(name, self.known_points[name])

    def _apply_selected_known_point(self, from_dialog: bool = False):
        item = self.known_points_dialog.list_widget.currentItem() if from_dialog else self.known_points_list.currentItem()
        if not item:
            return
        name = item.text().split(" ")[0]
        p = self.known_points.get(name)
        if not p:
            return
        self.tx.setText(str(int(round(float(p.get("x_m", 0))))))
        self.ty.setText(str(int(round(float(p.get("y_m", 0))))))

    def _refresh_known_points_ui(self):
        if not hasattr(self, "known_points_list"):
            return
        self.known_points_list.clear()
        if hasattr(self, "known_points_dialog"):
            self.known_points_dialog.list_widget.clear()
        for name, p in sorted(self.known_points.items()):
            label = f"{name} ({p.get('x_m',0):.0f}, {p.get('y_m',0):.0f})"
            self.known_points_list.addItem(label)
            if hasattr(self, "known_points_dialog"):
                self.known_points_dialog.list_widget.addItem(label)

    def _reset_runtime_data(self):
        if QMessageBox.question(self, "Подтверждение", "Очистить все данные кроме баллистических таблиц?") != QMessageBox.Yes:
            return
        self.nfa_zones = []
        self.known_points = {}
        self._clear_mission_history()
        self._refresh_nfa_list()
        self._refresh_known_points_ui()
        base = (self.web_url.text().strip() if hasattr(self, "web_url") else "").rstrip("/")
        if base:
            try:
                req = urllib.request.Request(base + "/api/reset_runtime_data", data=b"{}", headers={"Content-Type": "application/json"}, method="POST")
                urllib.request.urlopen(req, timeout=0.4).read()
            except Exception:
                pass


def main():
    app = QApplication(sys.argv)
    w = MainWindow(); w.show()
    sys.exit(app.exec())

if __name__ == "__main__":
    main()
