"""
actions/gods_eye.py — God's Eye View integration for JARVIS (MARK L).

God's Eye View (GEV) is a browser app (Cesium 3D globe with live aircraft,
ships, satellites, earthquakes, CCTV, radio...). This module lets JARVIS be
its voice controller:

  * launch()     — starts GEV's Vite dev server (npm run dev) if it isn't
                   running and opens it in the browser.
  * a bridge     — a tiny stdlib HTTP server on 127.0.0.1:8770. The GEV page
                   (src/voice/jarvisBridge.js) long-polls /gev/poll for
                   commands and POSTs results to /gev/result.
  * run_tool()   — sends one GEV action (fly_to_location, set_layer_visibility,
                   track_entity, control_cctv, analyst_query, ...) to the page
                   and waits for its result.

The GEV tool schemas are mirrored in gods_eye_tools.json (prefixed "gev_")
and registered with Gemini in main.py, so every GEV capability is voice
activated through JARVIS.
"""
from __future__ import annotations

import json
import os
import platform
import queue
import shutil
import socket
import subprocess
import threading
import time
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BRIDGE_HOST, BRIDGE_PORT = "127.0.0.1", 8770
GEV_PORT = int(os.environ.get("GEV_PORT", "4173"))
GEV_URL = f"http://localhost:{GEV_PORT}/"

_BASE = Path(__file__).resolve().parent.parent
TOOLS_FILE = Path(__file__).resolve().parent / "gods_eye_tools.json"

_cmd_q: "queue.Queue[dict]" = queue.Queue()
_results: dict[str, dict] = {}
_waiters: dict[str, threading.Event] = {}
_lock = threading.Lock()
_last_poll = 0.0
_server: ThreadingHTTPServer | None = None
_proc: subprocess.Popen | None = None

# Set by the JARVIS UI when it can host GEV inside its own window
# (QWebEngineView). Called with the URL instead of opening a browser tab.
EMBED_HANDLER = None
CLOSE_HANDLER = None


def load_tool_declarations() -> list[dict]:
    try:
        return json.loads(TOOLS_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[GodsEye] tool schema load failed: {e}")
        return []


# ── locating the GEV checkout ────────────────────────────────────────────
def find_gev_dir() -> Path | None:
    cands = []
    env = os.environ.get("GEV_DIR")
    if env:
        cands.append(Path(env).expanduser())
    try:
        from memory.config_manager import load_config  # type: ignore
        c = load_config() or {}
        if c.get("gods_eye_dir"):
            cands.append(Path(c["gods_eye_dir"]).expanduser())
    except Exception:
        pass
    try:
        cfg = json.loads((_BASE / "config" / "api_keys.json").read_text(encoding="utf-8"))
        if cfg.get("gods_eye_dir"):
            cands.append(Path(cfg["gods_eye_dir"]).expanduser())
    except Exception:
        pass
    cands += [_BASE.parent / "gods-eye-view", _BASE / "gods-eye-view",
              Path.home() / "gods-eye-view", Path.home() / "Documents" / "gods-eye-view"]
    for c in cands:
        if (c / "package.json").exists() and (c / "src" / "voice").exists():
            return c
    return None


# ── bridge HTTP server ───────────────────────────────────────────────────
class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def _send(self, code: int, body: dict | None):
        data = json.dumps(body if body is not None else {}).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Private-Network", "true")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Private-Network", "true")
        self.end_headers()

    def do_GET(self):
        global _last_poll
        if self.path.startswith("/gev/poll"):
            _last_poll = time.time()
            try:
                cmd = _cmd_q.get(timeout=20)
            except queue.Empty:
                return self._send(200, {})
            _last_poll = time.time()
            return self._send(200, cmd)
        if self.path.startswith("/gev/health"):
            return self._send(200, {"ok": True, "connected": is_connected()})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self.path.startswith("/gev/result"):
            return self._send(404, {"error": "not found"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            msg = json.loads(self.rfile.read(n) or b"{}")
            cid = msg.get("id")
            with _lock:
                if cid in _waiters:
                    _results[cid] = msg.get("result") or {}
                    _waiters[cid].set()
        except Exception as e:
            return self._send(400, {"error": str(e)})
        self._send(200, {"ok": True})


def start_bridge() -> bool:
    global _server
    if _server is not None:
        return True
    try:
        _server = ThreadingHTTPServer((BRIDGE_HOST, BRIDGE_PORT), _Handler)
        _server.daemon_threads = True
    except OSError as e:
        print(f"[GodsEye] bridge port {BRIDGE_PORT} unavailable: {e}")
        _server = None
        return False
    threading.Thread(target=_server.serve_forever, daemon=True, name="gev-bridge").start()
    print(f"[GodsEye] bridge listening on http://{BRIDGE_HOST}:{BRIDGE_PORT}")
    return True


def is_connected() -> bool:
    return time.time() - _last_poll < 30


def _port_open(port: int) -> bool:
    for host in ("127.0.0.1", "::1"):
        try:
            fam = socket.AF_INET6 if ":" in host else socket.AF_INET
            with socket.socket(fam, socket.SOCK_STREAM) as s:
                s.settimeout(0.4)
                if s.connect_ex((host, port)) == 0:
                    return True
        except OSError:
            continue
    return False


# ── lifecycle ────────────────────────────────────────────────────────────
def launch(open_browser: bool = True) -> str:
    global _proc
    start_bridge()
    gev = find_gev_dir()
    if not _port_open(GEV_PORT):
        if gev is None:
            return ("I couldn't find the God's Eye View folder. Put it next to Mark-L-main "
                    "as 'gods-eye-view' or set gods_eye_dir in config/api_keys.json.")
        npm = shutil.which("npm") or "/opt/homebrew/bin/npm"
        if not Path(npm).exists() and not shutil.which("npm"):
            return "Node.js/npm is not installed, so I can't start God's Eye View."
        if not (gev / "node_modules").exists():
            subprocess.run([npm, "install"], cwd=gev, capture_output=True, timeout=600)
        log = open(gev / ".gev-logs" / "jarvis-dev.log", "a") if (gev / ".gev-logs").exists() else subprocess.DEVNULL
        kw = {"creationflags": subprocess.CREATE_NO_WINDOW} if platform.system() == "Windows" else {"start_new_session": True}
        _proc = subprocess.Popen([npm, "run", "dev", "--", "--port", str(GEV_PORT), "--strictPort"],
                                 cwd=gev, stdout=log, stderr=subprocess.STDOUT, **kw)
        for _ in range(90):
            if _port_open(GEV_PORT):
                break
            if _proc.poll() is not None:
                return "God's Eye View failed to start — check .gev-logs/jarvis-dev.log."
            time.sleep(0.5)
        else:
            return "God's Eye View is taking too long to start."
    if EMBED_HANDLER is not None:
        try:
            EMBED_HANDLER(GEV_URL)
        except Exception as e:
            print(f"[GodsEye] embed failed: {e}")
    elif open_browser and not is_connected():
        webbrowser.open(GEV_URL)
        for _ in range(60):          # wait for the page to load and connect
            if is_connected():
                break
            time.sleep(0.5)
    if is_connected():
        return "God's Eye View is online and linked to JARVIS."
    return "God's Eye View is opening; the 3D globe is still loading."


def shutdown() -> str:
    global _proc
    if CLOSE_HANDLER is not None:
        try:
            CLOSE_HANDLER()
        except Exception:
            pass
    if _proc and _proc.poll() is None:
        try:
            if platform.system() == "Windows":
                _proc.terminate()
            else:
                os.killpg(os.getpgid(_proc.pid), 15)
        except Exception:
            _proc.terminate()
        _proc = None
        return "God's Eye View server stopped."
    return "God's Eye View wasn't started by me; close its browser tab to exit."


def status() -> str:
    parts = ["server running" if _port_open(GEV_PORT) else "server stopped",
             "page linked" if is_connected() else "page not linked"]
    return "God's Eye View: " + ", ".join(parts) + "."


def run_tool(name: str, args: dict | None = None, timeout: float = 45.0) -> dict:
    """Send one GEV action to the browser page and wait for its result."""
    name = name[4:] if name.startswith("gev_") else name
    start_bridge()
    if not is_connected():
        msg = launch()
        if not is_connected():
            return {"ok": False, "error": msg}
    cid = uuid.uuid4().hex
    ev = threading.Event()
    with _lock:
        _waiters[cid] = ev
    _cmd_q.put({"id": cid, "name": name, "args": args or {}})
    ok = ev.wait(timeout)
    with _lock:
        _waiters.pop(cid, None)
        res = _results.pop(cid, None)
    if not ok:
        return {"ok": False, "error": f"God's Eye View did not respond to {name} in time."}
    return res if isinstance(res, dict) else {"ok": True, "result": res}


def gods_eye(parameters: dict, player=None) -> str:
    """JARVIS-level control: open | close | status."""
    action = (parameters or {}).get("action", "open").lower()
    if action in ("open", "launch", "start"):
        r = launch()
    elif action in ("close", "stop", "shutdown"):
        r = shutdown()
    else:
        r = status()
    if player:
        try:
            player.write_log(f"SYS: {r}")
        except Exception:
            pass
    return r
