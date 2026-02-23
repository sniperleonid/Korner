from dataclasses import dataclass
from config import TIME_TO_LIVE_S

@dataclass
class SolveRequest:
    target_x_m: float
    target_y_m: float
    target_z_m: float
    wind_ff: tuple
    arc: str
    direct_fire: bool
    tolerance_m: float
    dt: float
    ttl: float = TIME_TO_LIVE_S

@dataclass
class ShotResult:
    charge: int
    elev_mil: float
    v0: float
    tof: float
    miss_total_m: float
    miss_range_m: float
    miss_alt_m: float
    miss_drift_m: float
