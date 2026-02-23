from __future__ import annotations

import argparse
import json
import mimetypes
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

BASE = Path(__file__).resolve().parent
CAL_STORE_FILE = BASE / "calibrations.json"


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


CAL_STORE = _load_cal_store()
STATE = {
    "ts": 0.0,
    "calibration": {"ok": False, "details": ""},
    "last_click": {"ts": 0.0, "x_m": None, "y_m": None, "dest": "", "note": ""},
    "points": {},
    "guns": {},
}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        return

    def _json(self, code: int, payload: dict):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self) -> dict:
        try:
            n = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(n) if n > 0 else b"{}"
            data = json.loads(raw.decode("utf-8"))
            if isinstance(data, dict):
                return data
        except Exception:
            pass
        return {}

    def _serve_file(self, file_path: Path):
        if not file_path.exists() or not file_path.is_file():
            self.send_error(404)
            return
        data = file_path.read_bytes()
        ctype, _ = mimetypes.guess_type(str(file_path))
        self.send_response(200)
        self.send_header("Content-Type", ctype or "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        p = urlparse(self.path)
        if p.path == "/":
            return self._serve_file(BASE / "templates" / "index.html")
        if p.path.startswith("/static/"):
            rel = p.path[len("/static/"):]
            return self._serve_file(BASE / "static" / rel)
        if p.path == "/api/ping":
            return self._json(200, {"ok": True})
        if p.path == "/api/state":
            return self._json(200, {"ok": True, **STATE})
        if p.path == "/api/last_click":
            return self._json(200, {"ok": True, **STATE["last_click"]})
        if p.path == "/api/cal_status":
            return self._json(200, {"ok": True, **STATE["calibration"]})
        if p.path == "/api/load_calibration":
            q = parse_qs(p.query)
            map_id = (q.get("map_id") or [""])[0]
            payload = CAL_STORE.get(map_id)
            if payload is None:
                return self._json(200, {"ok": False, "error": "not found"})
            return self._json(200, {"ok": True, "payload": payload})
        self.send_error(404)

    def do_POST(self):
        p = urlparse(self.path)
        data = self._read_json_body()
        now = time.time()

        if p.path == "/api/click":
            x = float(data.get("x_m", 0.0))
            y = float(data.get("y_m", 0.0))
            dest = str(data.get("dest", ""))
            note = str(data.get("note", ""))
            STATE["ts"] = now
            STATE["last_click"] = {"ts": now, "x_m": x, "y_m": y, "dest": dest, "note": note}
            if dest:
                STATE["points"][dest] = {"x_m": x, "y_m": y, "ts": now, "label": note or dest}
            return self._json(200, {"ok": True})

        if p.path == "/api/set_point":
            dest = str(data.get("dest", ""))
            x = float(data.get("x_m", 0.0))
            y = float(data.get("y_m", 0.0))
            label = str(data.get("label", ""))
            STATE["ts"] = now
            STATE["points"][dest] = {"x_m": x, "y_m": y, "ts": now, "label": label or dest}
            STATE["last_click"] = {"ts": now, "x_m": x, "y_m": y, "dest": dest, "note": label or dest}
            return self._json(200, {"ok": True})

        if p.path == "/api/delete_point":
            dest = str(data.get("dest", ""))
            if dest in STATE["points"]:
                del STATE["points"][dest]
                STATE["ts"] = now
            return self._json(200, {"ok": True})

        if p.path == "/api/gun_config":
            gun_id = str(data.get("gun_id", ""))
            g = STATE["guns"].get(gun_id, {"heading_mil": 0.0, "sector_mil": 534.0, "min_range": 0.0, "max_range": 6000.0})
            for key in ("heading_mil", "sector_mil", "min_range", "max_range"):
                if key in data and data.get(key) is not None:
                    g[key] = float(data.get(key))
            STATE["guns"][gun_id] = g
            STATE["ts"] = now
            return self._json(200, {"ok": True, "gun": g})

        if p.path == "/api/cal_status":
            STATE["calibration"] = {"ok": bool(data.get("ok", False)), "details": str(data.get("details", ""))}
            STATE["ts"] = now
            return self._json(200, {"ok": True})

        if p.path == "/api/save_calibration":
            map_id = str(data.get("map_id", ""))
            payload = data.get("payload", {})
            if map_id:
                CAL_STORE[map_id] = payload if isinstance(payload, dict) else {}
                _save_cal_store(CAL_STORE)
            return self._json(200, {"ok": True})

        self.send_error(404)


def run(host: str, port: int):
    server = ThreadingHTTPServer((host, port), Handler)
    server.serve_forever()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", default=8000, type=int)
    args = parser.parse_args()
    run(args.host, args.port)
