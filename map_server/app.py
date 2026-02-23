from __future__ import annotations

import time
import json
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

app = FastAPI(title="Arma FCS Map Server (LAN)")


@app.get("/api/ping")
def api_ping():
    return {"ok": True}


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

BASE = Path(__file__).resolve().parent
CAL_STORE_FILE = BASE / "calibrations.json"
STATE_FILE = BASE / "server_state.json"

def _load_cal_store():
    try:
        return json.loads(CAL_STORE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}

def _save_cal_store(store: dict):
    try:
        CAL_STORE_FILE.write_text(json.dumps(store, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass

def _load_state_store():
    try:
        payload = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}

def _save_state_store(store: dict):
    try:
        STATE_FILE.write_text(json.dumps(store, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass

CAL_STORE = _load_cal_store()
app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")

STATE: Dict[str, Any] = {
    "ts": 0.0,
    "calibration": {"ok": False, "details": ""},
    "last_click": {"ts": 0.0, "x_m": None, "y_m": None, "dest": "", "note": ""},
    "points": {},
    "guns": {},
    "views": {},
    "known_points": {},
    "nfa_zones": [],
}

STATE.update({k:v for k,v in _load_state_store().items() if k in ("points","guns","views","known_points","nfa_zones")})

def _persist_state():
    _save_state_store({
        "points": STATE.get("points", {}),
        "guns": STATE.get("guns", {}),
        "views": STATE.get("views", {}),
        "known_points": STATE.get("known_points", {}),
        "nfa_zones": STATE.get("nfa_zones", []),
    })

class ClickIn(BaseModel):
    x_m: float
    y_m: float
    dest: str = ""
    note: str = ""

class PointIn(BaseModel):
    dest: str
    x_m: float
    y_m: float
    label: str = ""

class GunCfgIn(BaseModel):
    gun_id: str
    heading_mil: Optional[float] = None
    sector_mil: Optional[float] = None
    min_range: Optional[float] = None
    max_range: Optional[float] = None


class ViewCfgIn(BaseModel):
    dest: str
    heading_mil: Optional[float] = None
    range_m: Optional[float] = None

@app.get("/", response_class=HTMLResponse)
def index():
    return (BASE / "templates" / "index.html").read_text(encoding="utf-8")

@app.get("/api/state")
def api_state():
    return {"ok": True, **STATE}

@app.post("/api/click")
def api_click(data: ClickIn):
    now = time.time()
    STATE["ts"] = now
    STATE["last_click"] = {"ts": now, "x_m": float(data.x_m), "y_m": float(data.y_m), "dest": data.dest, "note": data.note}
    if data.dest:
        STATE["points"][data.dest] = {"x_m": float(data.x_m), "y_m": float(data.y_m), "ts": now, "label": data.note or data.dest}
    _persist_state()
    return {"ok": True}

@app.get("/api/last_click")
def api_last_click():
    return {"ok": True, **STATE["last_click"]}

@app.post("/api/set_point")
def api_set_point(p: PointIn):
    now = time.time()
    STATE["ts"] = now
    STATE["points"][p.dest] = {"x_m": float(p.x_m), "y_m": float(p.y_m), "ts": now, "label": p.label or p.dest}
    STATE["last_click"] = {"ts": now, "x_m": float(p.x_m), "y_m": float(p.y_m), "dest": p.dest, "note": p.label or p.dest}
    _persist_state()
    return {"ok": True}

class DeletePointIn(BaseModel):
    dest: str

class KnownPointIn(BaseModel):
    name: str
    x_m: float
    y_m: float
    note: str = ""

class DeleteKnownPointIn(BaseModel):
    name: str

class NFAZonesIn(BaseModel):
    zones: list

@app.post("/api/delete_point")
def api_delete_point(p: DeletePointIn):
    if p.dest in STATE["points"]:
        del STATE["points"][p.dest]
        STATE["ts"] = time.time()
        _persist_state()
    return {"ok": True}


@app.post("/api/set_known_point")
def api_set_known_point(p: KnownPointIn):
    STATE["known_points"][p.name] = {"x_m": float(p.x_m), "y_m": float(p.y_m), "note": p.note}
    STATE["ts"] = time.time()
    _persist_state()
    return {"ok": True}

@app.post("/api/delete_known_point")
def api_delete_known_point(p: DeleteKnownPointIn):
    if p.name in STATE["known_points"]:
        del STATE["known_points"][p.name]
        STATE["ts"] = time.time()
        _persist_state()
    return {"ok": True}

@app.post("/api/set_nfa_zones")
def api_set_nfa_zones(payload: NFAZonesIn):
    STATE["nfa_zones"] = payload.zones
    STATE["ts"] = time.time()
    _persist_state()
    return {"ok": True}

@app.post("/api/reset_runtime_data")
def api_reset_runtime_data():
    STATE["points"] = {}
    STATE["guns"] = {}
    STATE["views"] = {}
    STATE["known_points"] = {}
    STATE["nfa_zones"] = []
    STATE["last_click"] = {"ts": 0.0, "x_m": None, "y_m": None, "dest": "", "note": ""}
    STATE["ts"] = time.time()
    _persist_state()
    return {"ok": True}

@app.post("/api/gun_config")
def api_gun_config(cfg: GunCfgIn):
    g = STATE["guns"].get(cfg.gun_id, {"heading_mil": 0.0, "sector_mil": 534.0, "min_range": 0.0, "max_range": 6000.0})
    if cfg.heading_mil is not None: g["heading_mil"] = float(cfg.heading_mil)
    if cfg.sector_mil is not None: g["sector_mil"] = float(cfg.sector_mil)
    if cfg.min_range is not None: g["min_range"] = float(cfg.min_range)
    if cfg.max_range is not None: g["max_range"] = float(cfg.max_range)
    STATE["guns"][cfg.gun_id] = g
    STATE["ts"] = time.time()
    _persist_state()
    return {"ok": True, "gun": g}


@app.post("/api/view_config")
def api_view_config(cfg: ViewCfgIn):
    key = cfg.dest.strip().lower()
    if not key:
        return {"ok": False, "error": "dest required"}
    rec = STATE["views"].get(key, {"heading_mil": 0.0, "range_m": 1500.0})
    if cfg.heading_mil is not None:
        rec["heading_mil"] = float(cfg.heading_mil)
    if cfg.range_m is not None:
        rec["range_m"] = max(0.0, float(cfg.range_m))
    STATE["views"][key] = rec
    STATE["ts"] = time.time()
    _persist_state()
    return {"ok": True, "view": rec}

@app.post("/api/cal_status")
def api_cal_status(payload: dict):
    STATE["calibration"] = {"ok": bool(payload.get("ok", False)), "details": str(payload.get("details", ""))}
    STATE["ts"] = time.time()
    return {"ok": True}

@app.get("/api/cal_status")
def api_get_cal_status():
    return {"ok": True, **STATE["calibration"]}

class CalSaveIn(BaseModel):
    map_id: str
    payload: dict

@app.post("/api/save_calibration")
def api_save_calibration(data: CalSaveIn):
    CAL_STORE[data.map_id] = data.payload
    _save_cal_store(CAL_STORE)
    return {"ok": True}

@app.get("/api/load_calibration")
def api_load_calibration(map_id: str):
    payload = CAL_STORE.get(map_id)
    if payload is None:
        return {"ok": False, "error": "not found"}
    return {"ok": True, "payload": payload}
