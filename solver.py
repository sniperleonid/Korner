import numpy as np
from typing import Optional
from config import MAX_ELEVATION_MIL, SHELL_MASS_KG, AIR_DRAG
from utils import mil_to_rad
from ballistics import simulate_rk4, closest_approach
from models import SolveRequest, ShotResult

def _eval(req: SolveRequest, v0: float, elev_mil: float) -> ShotResult:
    tr = simulate_rk4(v0=v0, elev_rad=mil_to_rad(elev_mil),
                      mass=SHELL_MASS_KG, air_drag=AIR_DRAG,
                      wind_ff=req.wind_ff, dt=req.dt, ttl=req.ttl, stop_on_ground=True)
    x,y,z,miss = closest_approach(tr, req.target_x_m, req.target_y_m, req.target_z_m)
    return ShotResult(charge=0, elev_mil=float(elev_mil), v0=float(v0), tof=float(tr.t[-1]),
                      miss_total_m=float(miss),
                      miss_range_m=float(x-req.target_x_m),
                      miss_alt_m=float(y-req.target_y_m),
                      miss_drift_m=float(z-req.target_z_m))

def suggest_best(req: SolveRequest, weapon, tables=None) -> Optional[ShotResult]:
    if tables is not None:
        try:
            from table_cache import fast_solve
            r = fast_solve(req, weapon, tables)
            if r is not None:
                return r
        except Exception:
            pass

    best=None
    elev_step=25.0
    if req.direct_fire:
        ranges=[(0.0,250.0)]
    elif req.arc=="LOW":
        ranges=[(0.0,650.0)]
    elif req.arc=="HIGH":
        ranges=[(650.0,float(MAX_ELEVATION_MIL))]
    else:
        ranges=[(0.0,650.0),(650.0,float(MAX_ELEVATION_MIL))]

    for cid in sorted(weapon.charges.keys()):
        v0=weapon.v0_for_charge(cid)
        for emin,emax in ranges:
            for em in np.arange(emin, emax+1e-6, elev_step):
                r=_eval(req,v0,float(em)); r.charge=cid
                if best is None or r.miss_total_m < best.miss_total_m:
                    best=r
            if best is not None and best.miss_total_m <= req.tolerance_m:
                return best
    return best
