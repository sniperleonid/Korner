import math
from dataclasses import dataclass
from typing import Optional, Tuple
from config import MIL_CIRCLE

AUTO_SCALE_4DIG = 10.0
AUTO_SCALE_5DIG = 1.0

@dataclass(frozen=True)
class Point2D:
    x: float
    y: float

def parse_coord_digits(s: str) -> str:
    s = (s or "").strip().replace(" ", "").replace("_", "")
    if not s:
        raise ValueError("empty coordinate")
    if len(s) >= 2 and s[0].lower() in ("x","y"):
        s = s[1:]
    if s.startswith("+"):
        s = s[1:]
    if not s.isdigit():
        raise ValueError(f"coordinate must be digits, got {s!r}")
    return s

def parse_coord_with_autoscale(s: str, scale_override: Optional[float] = None) -> float:
    s2 = parse_coord_digits(s)
    n = int(s2)
    if scale_override is not None:
        return float(n) * float(scale_override)
    if len(s2) == 4:
        return float(n) * AUTO_SCALE_4DIG
    if len(s2) == 5:
        return float(n) * AUTO_SCALE_5DIG
    return float(n)

def distance_2d(a: Point2D, b: Point2D) -> float:
    return math.hypot(b.x - a.x, b.y - a.y)

def bearing_rad_from_north(a: Point2D, b: Point2D) -> float:
    dx = b.x - a.x
    dy = b.y - a.y
    return math.atan2(dx, dy)

def rad_to_mil(rad: float) -> float:
    return (rad % (2*math.pi)) * (MIL_CIRCLE / (2*math.pi))

def mil_to_rad(mil: float) -> float:
    return (mil / MIL_CIRCLE) * (2*math.pi)

def mil_to_deg(mil: float) -> float:
    return mil_to_rad(mil) * 180.0 / math.pi

def wind_components_from_speed_dir(speed_mps: float, wind_from_deg: float) -> Tuple[float, float]:
    ang = math.radians((wind_from_deg + 180.0) % 360.0)
    wx = speed_mps * math.sin(ang)
    wy = speed_mps * math.cos(ang)
    return wx, wy

def rotate_world_to_fireframe(wx: float, wy: float, bearing_rad: float) -> Tuple[float, float]:
    fx = math.sin(bearing_rad); fy = math.cos(bearing_rad)
    rx = math.cos(bearing_rad); ry = -math.sin(bearing_rad)
    wx_ff = wx*fx + wy*fy
    wz_ff = wx*rx + wy*ry
    return wx_ff, wz_ff
