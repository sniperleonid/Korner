import json, math
from dataclasses import dataclass
from typing import Optional, Tuple, Dict

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QPixmap, QPen, QBrush, QPainterPath
from PySide6.QtWidgets import (
    QGraphicsView, QGraphicsScene, QGraphicsPixmapItem, QGraphicsEllipseItem,
    QGraphicsTextItem, QGraphicsPathItem
)

@dataclass
class SimilarityTransform:
    # world(E,N) = w0 + R*(p - p0)*s   where p is pixel coords (x right, y down)
    p0: Tuple[float,float]
    w0: Tuple[float,float]
    s: float
    ang: float  # radians rotation pixel->world

    def pixel_to_world(self, px: float, py: float) -> Tuple[float,float]:
        dx = px - self.p0[0]
        dy = py - self.p0[1]
        ca = math.cos(self.ang)
        sa = math.sin(self.ang)
        E = (ca*dx - sa*dy) * self.s + self.w0[0]
        N = (sa*dx + ca*dy) * self.s + self.w0[1]
        return (E, N)

    def world_to_pixel(self, E: float, N: float) -> Tuple[float,float]:
        dx = (E - self.w0[0]) / max(1e-12, self.s)
        dy = (N - self.w0[1]) / max(1e-12, self.s)
        ca = math.cos(-self.ang)
        sa = math.sin(-self.ang)
        px = ca*dx - sa*dy + self.p0[0]
        py = sa*dx + ca*dy + self.p0[1]
        return (px, py)

    def to_json(self) -> Dict:
        return {"p0": list(self.p0), "w0": list(self.w0), "s": self.s, "ang": self.ang}

    @staticmethod
    def from_json(d: Dict) -> "SimilarityTransform":
        return SimilarityTransform(tuple(d["p0"]), tuple(d["w0"]), float(d["s"]), float(d["ang"]))

def compute_similarity(p1, w1, p2, w2) -> SimilarityTransform:
    (x1,y1) = p1; (E1,N1) = w1
    (x2,y2) = p2; (E2,N2) = w2
    dpx = x2-x1; dpy = y2-y1
    dE  = E2-E1; dN  = N2-N1
    dp = math.hypot(dpx,dpy)
    dW = math.hypot(dE,dN)
    if dp < 1e-6 or dW < 1e-6:
        raise ValueError("Калибровочные точки слишком близко.")
    s = dW / dp
    ang_p = math.atan2(dpy, dpx)
    ang_w = math.atan2(dN, dE)
    ang = ang_w - ang_p
    return SimilarityTransform(p0=(x1,y1), w0=(E1,N1), s=s, ang=ang)

def bearing_mil_from_EN(dE: float, dN: float) -> float:
    # 0 mil = North, 1600 = East
    ang = math.atan2(dE, dN)  # east as x, north as y
    mil = (ang / (2*math.pi)) * 6400.0
    return (mil + 6400.0) % 6400.0

class TacticalMapView(QGraphicsView):
    clickedPixel = Signal(float, float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setScene(QGraphicsScene(self))
        self._pixmap_item: Optional[QGraphicsPixmapItem] = None
        self._pixmap_path: Optional[str] = None
        self._transform: Optional[SimilarityTransform] = None

        self._markers = []
        self._ruler_items = []  # path + text
        self._ruler_p1 = None  # (px,py)
        self._ruler_p2 = None

        self.setDragMode(QGraphicsView.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.AnchorUnderMouse)

        self._pen_gun = QPen(Qt.green); self._pen_gun.setWidth(2)
        self._pen_target = QPen(Qt.red); self._pen_target.setWidth(2)
        self._pen_ruler = QPen(Qt.yellow); self._pen_ruler.setWidth(2)
        self._brush = QBrush(Qt.transparent)

    def wheelEvent(self, event):
        if event.angleDelta().y() > 0:
            self.scale(1.15, 1.15)
        else:
            self.scale(1/1.15, 1/1.15)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            p = self.mapToScene(event.position().toPoint())
            self.clickedPixel.emit(float(p.x()), float(p.y()))
        super().mousePressEvent(event)

    def load_image(self, path: str):
        pm = QPixmap(path)
        if pm.isNull():
            raise ValueError("Не удалось загрузить изображение карты.")
        self.scene().clear()
        self._markers.clear()
        self._ruler_items.clear()
        self._ruler_p1 = None
        self._ruler_p2 = None
        self._pixmap_item = QGraphicsPixmapItem(pm)
        self.scene().addItem(self._pixmap_item)
        self.scene().setSceneRect(pm.rect())
        self._pixmap_path = path
        self._transform = None
        self.resetTransform()

    def set_transform(self, tr: SimilarityTransform):
        self._transform = tr

    def transform_ready(self) -> bool:
        return self._transform is not None

    def pixel_to_world(self, px: float, py: float):
        if not self._transform:
            raise ValueError("Калибровка карты не выполнена.")
        return self._transform.pixel_to_world(px, py)

    def add_marker_world(self, kind: str, label: str, E: float, N: float):
        if not self._transform:
            return
        px, py = self._transform.world_to_pixel(E, N)
        r = 6.0
        e = QGraphicsEllipseItem(px-r, py-r, 2*r, 2*r)
        e.setBrush(self._brush)
        e.setPen(self._pen_gun if kind=="gun" else self._pen_target)
        t = QGraphicsTextItem(label)
        t.setDefaultTextColor(Qt.white)
        t.setPos(px+r+2, py-r-2)
        self.scene().addItem(e); self.scene().addItem(t)
        self._markers.extend([e,t])

    def clear_markers(self):
        for it in self._markers:
            try:
                self.scene().removeItem(it)
            except Exception:
                pass
        self._markers.clear()

    def clear_ruler(self):
        for it in self._ruler_items:
            try:
                self.scene().removeItem(it)
            except Exception:
                pass
        self._ruler_items.clear()
        self._ruler_p1 = None
        self._ruler_p2 = None

    def set_ruler_point(self, idx: int, px: float, py: float):
        if idx == 1:
            self._ruler_p1 = (px,py)
        else:
            self._ruler_p2 = (px,py)
        self._update_ruler()

    def _update_ruler(self):
        for it in self._ruler_items:
            try:
                self.scene().removeItem(it)
            except Exception:
                pass
        self._ruler_items.clear()
        if not self._ruler_p1 or not self._ruler_p2:
            return

        (x1,y1) = self._ruler_p1
        (x2,y2) = self._ruler_p2
        path = QPainterPath()
        path.moveTo(x1,y1)
        path.lineTo(x2,y2)
        item = QGraphicsPathItem(path)
        item.setPen(self._pen_ruler)
        self.scene().addItem(item)
        self._ruler_items.append(item)

        if self._transform:
            E1,N1 = self._transform.pixel_to_world(x1,y1)
            E2,N2 = self._transform.pixel_to_world(x2,y2)
            dE = E2-E1; dN = N2-N1
            dist = math.hypot(dE,dN)
            az = bearing_mil_from_EN(dE,dN)
            text = QGraphicsTextItem(f"{dist:.0f} м | {az:.1f} mil")
            text.setDefaultTextColor(Qt.yellow)
            text.setPos((x1+x2)/2.0 + 6, (y1+y2)/2.0 + 6)
            self.scene().addItem(text)
            self._ruler_items.append(text)

    def save_state(self, path: str):
        data = {"image": self._pixmap_path, "transform": (self._transform.to_json() if self._transform else None)}
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def load_state(self, path: str):
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        img = data.get("image")
        if img:
            self.load_image(img)
        tr = data.get("transform")
        if tr:
            self._transform = SimilarityTransform.from_json(tr)
