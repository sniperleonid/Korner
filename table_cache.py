import os
import numpy as np
from typing import Optional
from config import MAX_ELEVATION_MIL, SHELL_MASS_KG, AIR_DRAG
from utils import mil_to_rad
from ballistics import simulate_rk4, closest_approach
from models import SolveRequest, ShotResult

class TableSet:
    def __init__(self, path: str):
        self.path = path
        self.npz = np.load(path, allow_pickle=False)
        self.elev = self.npz["elev_mil"].astype(np.float32)
        self.charges = self.npz["charges_id"].astype(int).tolist()
        # Optional extra fields (only present in ultra v2 tables)
        self.has_wind_sens = False
        try:
            self.has_wind_sens = (self.npz.get("drift1_c1", None) is not None) and (self.npz.get("rdelta1_c1", None) is not None)
        except Exception:
            self.has_wind_sens = False

    def guess_elev_for_range(self, charge_id: int, target_range: float) -> float:
        ranges = self.npz[f"range_c{charge_id}"].astype(np.float32)
        idx = int(np.argmin(np.abs(ranges - float(target_range))))
        return float(self.elev[idx])

class TableManager:
    def __init__(self):
        self.direct: Optional[TableSet] = None
        self.low: Optional[TableSet] = None
        self.high: Optional[TableSet] = None
        self.folder: str = ""

    def loaded(self) -> bool:
        return self.direct is not None and self.low is not None and self.high is not None

    def load_folder(self, folder: str):
        self.folder = folder
        self.direct = TableSet(os.path.join(folder, "ballistic_direct.npz"))
        self.low = TableSet(os.path.join(folder, "ballistic_low.npz"))
        self.high = TableSet(os.path.join(folder, "ballistic_high.npz"))

def _eval(req: SolveRequest, v0: float, elev_mil: float) -> ShotResult:
    tr = simulate_rk4(v0=float(v0), elev_rad=mil_to_rad(float(elev_mil)),
                     mass=SHELL_MASS_KG, air_drag=AIR_DRAG, wind_ff=req.wind_ff,
                     dt=req.dt, ttl=req.ttl, stop_on_ground=True)
    x,y,z,miss = closest_approach(tr, req.target_x_m, req.target_y_m, req.target_z_m)
    return ShotResult(charge=0, elev_mil=float(elev_mil), v0=float(v0), tof=float(tr.t[-1]),
                     miss_total_m=float(miss), miss_range_m=float(x-req.target_x_m),
                     miss_alt_m=float(y-req.target_y_m), miss_drift_m=float(z-req.target_z_m))

def fast_solve(req: SolveRequest, weapon, tm: TableManager) -> Optional[ShotResult]:
    if not tm.loaded():
        return None

    if req.direct_fire:
        sets=[tm.direct]
    elif req.arc=="LOW":
        sets=[tm.low]
    elif req.arc=="HIGH":
        sets=[tm.high]
    else:
        sets=[tm.low, tm.high]

    best=None
    for ts in sets:
        for cid in ts.charges:
            v0 = weapon.v0_for_charge(cid)
            center = ts.guess_elev_for_range(cid, req.target_x_m)

            window=50.0
            for _ in range(4):
                start=max(0.0, center-window)
                end=min(float(MAX_ELEVATION_MIL), center+window)
                elevs=np.linspace(start,end,11,dtype=float)
                local=None
                for em in elevs:
                    r=_eval(req,v0,float(em)); r.charge=cid
                    if local is None or r.miss_total_m < local.miss_total_m:
                        local=r
                if best is None or local.miss_total_m < best.miss_total_m:
                    best=local
                center=float(local.elev_mil)
                window=max(6.0, window*0.45)
                if best.miss_total_m <= req.tolerance_m:
                    return best
    return best
