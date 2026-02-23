import math
import numpy as np
from dataclasses import dataclass
from typing import Tuple
from config import G, TIME_TO_LIVE_S

@dataclass
class Trajectory:
    t: np.ndarray
    x: np.ndarray
    y: np.ndarray
    z: np.ndarray

def _deriv(s: np.ndarray, wind: Tuple[float,float,float], air_drag: float, mass: float) -> np.ndarray:
    vx, vy, vz = float(s[3]), float(s[4]), float(s[5])
    wx, wy, wz = wind
    rvx = vx - wx; rvy = vy - wy; rvz = vz - wz
    vrel = math.sqrt(rvx*rvx + rvy*rvy + rvz*rvz) + 1e-9
    k = air_drag / mass
    ax = -k * vrel * rvx
    ay = -G -k * vrel * rvy
    az = -k * vrel * rvz
    return np.array([vx, vy, vz, ax, ay, az], dtype=float)

def simulate_rk4(v0: float, elev_rad: float, mass: float, air_drag: float,
                 wind_ff: Tuple[float,float,float], dt: float, ttl: float = TIME_TO_LIVE_S,
                 stop_on_ground: bool = True) -> Trajectory:
    vx0 = v0 * math.cos(elev_rad)
    vy0 = v0 * math.sin(elev_rad)
    s = np.array([0.0,0.0,0.0,vx0,vy0,0.0], dtype=float)
    nmax = int(max(1, math.ceil(ttl/dt)))
    t = np.empty(nmax+1, dtype=float)
    x = np.empty(nmax+1, dtype=float)
    y = np.empty(nmax+1, dtype=float)
    z = np.empty(nmax+1, dtype=float)
    t[0]=0.0; x[0]=0.0; y[0]=0.0; z[0]=0.0
    lasty = 0.0
    i_end = nmax
    for i in range(1, nmax+1):
        k1 = _deriv(s, wind_ff, air_drag, mass)
        k2 = _deriv(s + 0.5*dt*k1, wind_ff, air_drag, mass)
        k3 = _deriv(s + 0.5*dt*k2, wind_ff, air_drag, mass)
        k4 = _deriv(s + dt*k3, wind_ff, air_drag, mass)
        s = s + (dt/6.0)*(k1 + 2*k2 + 2*k3 + k4)
        t[i] = i*dt
        x[i], y[i], z[i] = s[0], s[1], s[2]
        if stop_on_ground and i>5 and y[i] < 0.0 and (y[i]-lasty) < 0.0:
            i_end = i
            break
        lasty = y[i]
    return Trajectory(t=t[:i_end+1], x=x[:i_end+1], y=y[:i_end+1], z=z[:i_end+1])

def closest_approach(tr: Trajectory, tx: float, ty: float, tz: float):
    dx = tr.x - tx; dy = tr.y - ty; dz = tr.z - tz
    d2 = dx*dx + dy*dy + dz*dz
    idx = int(np.argmin(d2))
    return float(tr.x[idx]), float(tr.y[idx]), float(tr.z[idx]), float(math.sqrt(float(d2[idx])))

def impact_range(tr: Trajectory):
    y=tr.y; x=tr.x; t=tr.t
    if y.size < 2:
        return float(x[-1]) if x.size else 0.0, float(t[-1]) if t.size else 0.0
    idx=None
    for i in range(1,len(y)):
        if y[i] <= 0.0 and y[i-1] > 0.0:
            idx=i; break
    if idx is None:
        return float(x[-1]), float(t[-1])
    y0,y1=float(y[idx-1]), float(y[idx])
    x0,x1=float(x[idx-1]), float(x[idx])
    t0,t1=float(t[idx-1]), float(t[idx])
    a=(0.0-y0)/(y1-y0) if (y1-y0)!=0 else 0.0
    return float(x0+a*(x1-x0)), float(t0+a*(t1-t0))
