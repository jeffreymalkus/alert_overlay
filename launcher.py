"""
Portfolio D — Launcher & Process Manager.

Serves the dashboard independently so it's always accessible, and manages
the strategy engine (dashboard.py main) as a child subprocess.

Usage:
    python -m alert_overlay.launcher [--host 127.0.0.1] [--port 7497] [--no-ibkr]

The launcher:
  1. Starts its own HTTP server on port 8877 (the dashboard)
  2. Proxies all existing /api/* requests to the engine when it's running
  3. Adds new /api/engine/* endpoints for start/stop/restart/health
  4. Keeps the dashboard alive even when the engine is stopped
  5. Shows engine status (running/stopped/error) in the dashboard header
"""

import argparse
import json
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from queue import Queue, Empty
from socketserver import ThreadingMixIn

DASHBOARD_PORT = 8877
ENGINE_PORT = 8878  # Internal port for the engine's HTTP server
BASE_DIR = Path(__file__).parent

# ── Engine process state ──
_engine_proc: subprocess.Popen = None
_engine_lock = threading.Lock()
_engine_status = {
    "running": False,
    "pid": None,
    "started_at": None,
    "stopped_at": None,
    "error": None,
    "restart_count": 0,
    "last_health": None,
    "uptime_seconds": 0,
}
_engine_args: list = []  # CLI args to pass to engine
_engine_log_lines: list = []  # Last N log lines from engine stdout/stderr
MAX_LOG_LINES = 500

# ── SSE clients ──
_sse_clients: list = []
_sse_lock = threading.Lock()


def _broadcast_sse(event: str, data: dict):
    """Send SSE event to all connected launcher clients."""
    msg = f"event: {event}\ndata: {json.dumps(data)}\n\n"
    with _sse_lock:
        dead = []
        for q in _sse_clients:
            try:
                q.put_nowait(msg)
            except Exception:
                dead.append(q)
        for q in dead:
            _sse_clients.remove(q)


def _start_engine():
    """Start the strategy engine as a subprocess."""
    global _engine_proc
    with _engine_lock:
        if _engine_proc is not None and _engine_proc.poll() is None:
            return False, "Engine already running"

        cmd = [sys.executable, "-m", "alert_overlay.dashboard",
               "--internal-port", str(ENGINE_PORT)] + _engine_args

        try:
            _engine_proc = subprocess.Popen(
                cmd,
                cwd=str(BASE_DIR.parent),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,  # line-buffered
            )
            _engine_status["running"] = True
            _engine_status["pid"] = _engine_proc.pid
            _engine_status["started_at"] = datetime.now().isoformat()
            _engine_status["stopped_at"] = None
            _engine_status["error"] = None

            # Start log reader thread
            t = threading.Thread(target=_read_engine_output, daemon=True)
            t.start()

            # Start health monitor thread
            h = threading.Thread(target=_health_monitor, daemon=True)
            h.start()

            _broadcast_sse("engine_status", _engine_status)
            return True, f"Engine started (PID {_engine_proc.pid})"
        except Exception as e:
            _engine_status["error"] = str(e)
            _broadcast_sse("engine_status", _engine_status)
            return False, str(e)


def _stop_engine():
    """Stop the engine subprocess gracefully."""
    global _engine_proc
    with _engine_lock:
        if _engine_proc is None or _engine_proc.poll() is not None:
            _engine_status["running"] = False
            _engine_status["pid"] = None
            return True, "Engine not running"

        pid = _engine_proc.pid
        try:
            # Send SIGINT (same as Ctrl+C) for graceful shutdown
            _engine_proc.send_signal(signal.SIGINT)
            # Wait up to 10 seconds for clean exit
            try:
                _engine_proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                _engine_proc.kill()
                _engine_proc.wait(timeout=5)
        except Exception as e:
            _engine_status["error"] = str(e)

        _engine_status["running"] = False
        _engine_status["pid"] = None
        _engine_status["stopped_at"] = datetime.now().isoformat()
        _engine_proc = None
        _broadcast_sse("engine_status", _engine_status)
        return True, f"Engine stopped (was PID {pid})"


def _restart_engine():
    """Stop then start the engine."""
    _stop_engine()
    time.sleep(1)
    ok, msg = _start_engine()
    if ok:
        _engine_status["restart_count"] += 1
    return ok, msg


def _read_engine_output():
    """Read engine subprocess stdout/stderr in a background thread."""
    global _engine_proc
    proc = _engine_proc
    if proc is None:
        return
    try:
        for line in proc.stdout:
            line = line.rstrip("\n")
            _engine_log_lines.append(line)
            if len(_engine_log_lines) > MAX_LOG_LINES:
                _engine_log_lines.pop(0)
            # Forward important lines as SSE events
            if any(kw in line for kw in ["ALERT", "ERROR", "FATAL", "Connected", "Disconnected", "Ready"]):
                _broadcast_sse("engine_log", {"line": line})
    except Exception:
        pass
    finally:
        # Process ended
        rc = proc.poll()
        with _engine_lock:
            _engine_status["running"] = False
            _engine_status["pid"] = None
            _engine_status["stopped_at"] = datetime.now().isoformat()
            if rc and rc != 0 and rc != -2:  # -2 = SIGINT
                _engine_status["error"] = f"Exited with code {rc}"
        _broadcast_sse("engine_status", _engine_status)


def _health_monitor():
    """Periodically check engine health and update uptime."""
    while True:
        time.sleep(5)
        with _engine_lock:
            if _engine_proc is None or _engine_proc.poll() is not None:
                _engine_status["running"] = False
                if _engine_status.get("pid"):
                    _engine_status["pid"] = None
                    _engine_status["stopped_at"] = datetime.now().isoformat()
                    _broadcast_sse("engine_status", _engine_status)
                return

            if _engine_status.get("started_at"):
                started = datetime.fromisoformat(_engine_status["started_at"])
                _engine_status["uptime_seconds"] = int((datetime.now() - started).total_seconds())

            _engine_status["last_health"] = datetime.now().isoformat()


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class LauncherHandler(SimpleHTTPRequestHandler):
    """Serves the dashboard + engine control API."""

    def log_message(self, format, *args):
        pass  # Suppress request logging

    def do_GET(self):
        path = self.path.split("?")[0]

        if path == "/":
            self._serve_dashboard()
        elif path == "/api/engine/status":
            self._engine_health()
        elif path == "/api/engine/start":
            ok, msg = _start_engine()
            self._json(200 if ok else 409, {"ok": ok, "message": msg})
        elif path == "/api/engine/stop":
            ok, msg = _stop_engine()
            self._json(200, {"ok": ok, "message": msg})
        elif path == "/api/engine/restart":
            ok, msg = _restart_engine()
            self._json(200 if ok else 500, {"ok": ok, "message": msg})
        elif path == "/api/engine/logs":
            n = 100
            try:
                qs = self.path.split("?")[1] if "?" in self.path else ""
                for part in qs.split("&"):
                    if part.startswith("n="):
                        n = min(int(part[2:]), MAX_LOG_LINES)
            except Exception:
                pass
            self._json(200, {"ok": True, "lines": _engine_log_lines[-n:]})
        elif path == "/api/engine/stream":
            self._serve_engine_sse()
        elif path == "/api/stream":
            # SSE stream — needs streaming proxy, not buffered
            if _engine_status["running"]:
                self._proxy_sse_to_engine(path)
            else:
                # Return empty SSE that closes immediately
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                msg = 'event: status\ndata: {"connected": false, "symbols": {}, "mode": "ENGINE_STOPPED"}\n\n'
                self.wfile.write(msg.encode())
        elif path == "/api/status":
            # Return offline status when engine is not running
            if _engine_status["running"]:
                self._proxy_to_engine("GET", path)
            else:
                self._json(200, {"connected": False, "symbols": {},
                                 "mode": "ENGINE_STOPPED", "started_at": None})
        elif path == "/api/alerts":
            # Return empty alerts when engine is not running
            if _engine_status["running"]:
                self._proxy_to_engine("GET", path)
            else:
                self._json(200, [])
        elif path.startswith("/api/"):
            # Proxy to engine
            self._proxy_to_engine("GET", path)
        else:
            self.send_error(404)

    def do_POST(self):
        path = self.path.split("?")[0]

        if path == "/api/engine/start":
            ok, msg = _start_engine()
            self._json(200 if ok else 409, {"ok": ok, "message": msg})
        elif path == "/api/engine/stop":
            ok, msg = _stop_engine()
            self._json(200, {"ok": ok, "message": msg})
        elif path == "/api/engine/restart":
            ok, msg = _restart_engine()
            self._json(200 if ok else 500, {"ok": ok, "message": msg})
        elif path.startswith("/api/"):
            # Read POST body and proxy
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length) if content_length > 0 else b""
            self._proxy_to_engine("POST", path, body)
        else:
            self.send_error(404)

    def _serve_dashboard(self):
        """Serve the dashboard HTML with engine control panel injected."""
        html_path = BASE_DIR / "dashboard.html"
        if not html_path.exists():
            self.send_error(500, "dashboard.html not found")
            return

        html = html_path.read_text()

        # Inject engine control panel into the header
        control_panel = ENGINE_CONTROL_HTML
        # Insert before the closing </div> of the header
        html = html.replace(
            '<div class="status">',
            control_panel + '\n    <div class="status">',
            1
        )

        # Inject engine control JavaScript before closing </script>
        js_injection = ENGINE_CONTROL_JS
        html = html.replace("</script>", js_injection + "\n</script>", 1)

        # Inject engine control CSS before closing </style>
        css_injection = ENGINE_CONTROL_CSS
        html = html.replace("</style>", css_injection + "\n</style>", 1)

        data = html.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _engine_health(self):
        """Return engine health status."""
        status = dict(_engine_status)
        # Check if process is actually alive
        with _engine_lock:
            if _engine_proc is not None:
                rc = _engine_proc.poll()
                if rc is not None:
                    status["running"] = False
                    status["error"] = f"Exited with code {rc}" if rc != 0 else None
        self._json(200, {"ok": True, **status})

    def _serve_engine_sse(self):
        """SSE stream for engine status updates."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

        q = Queue()
        with _sse_lock:
            _sse_clients.append(q)

        # Send initial status
        try:
            initial = f"event: engine_status\ndata: {json.dumps(_engine_status)}\n\n"
            self.wfile.write(initial.encode())
            self.wfile.flush()
        except Exception:
            with _sse_lock:
                if q in _sse_clients:
                    _sse_clients.remove(q)
            return

        try:
            while True:
                try:
                    msg = q.get(timeout=15)
                    self.wfile.write(msg.encode())
                    self.wfile.flush()
                except Empty:
                    # Send keepalive
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
        except Exception:
            pass
        finally:
            with _sse_lock:
                if q in _sse_clients:
                    _sse_clients.remove(q)

    def _proxy_to_engine(self, method: str, path: str, body: bytes = b""):
        """Proxy API request to the engine's internal HTTP server."""
        import urllib.request
        import urllib.error

        if not _engine_status["running"]:
            self._json(503, {"ok": False, "error": "Engine not running. Start it first."})
            return

        url = f"http://127.0.0.1:{ENGINE_PORT}{path}"
        try:
            req = urllib.request.Request(url, data=body if method == "POST" else None,
                                         method=method)
            if method == "POST":
                req.add_header("Content-Type", "application/json")
            with urllib.request.urlopen(req, timeout=10) as resp:
                resp_data = resp.read()
                self.send_response(resp.status)
                for k, v in resp.getheaders():
                    if k.lower() not in ("transfer-encoding", "connection"):
                        self.send_header(k, v)
                self.end_headers()
                self.wfile.write(resp_data)
        except urllib.error.URLError as e:
            self._json(502, {"ok": False, "error": f"Engine unreachable: {e.reason}"})
        except Exception as e:
            self._json(502, {"ok": False, "error": f"Proxy error: {e}"})

    def _proxy_sse_to_engine(self, path: str):
        """Stream-proxy an SSE endpoint from the engine."""
        import http.client
        import socket

        if not _engine_status["running"]:
            self._json(503, {"ok": False, "error": "Engine not running. Start it first."})
            return

        try:
            conn = http.client.HTTPConnection("127.0.0.1", ENGINE_PORT, timeout=30)
            conn.request("GET", path)
            resp = conn.getresponse()

            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()

            # Set socket to longer timeout for SSE streaming
            conn.sock.settimeout(90)

            # Stream chunks from engine to client
            while True:
                chunk = resp.read(4096)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
        except (socket.timeout, TimeoutError):
            # Send a retry hint so the browser reconnects
            try:
                self.wfile.write(b"retry: 2000\n\n")
                self.wfile.flush()
            except Exception:
                pass
        except Exception:
            # Connection closed or engine died — client will reconnect
            pass

    def _json(self, code: int, data: dict):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


# ── HTML/CSS/JS injected into the dashboard ──

ENGINE_CONTROL_CSS = """
  /* Engine control panel */
  .engine-controls {
    display: flex;
    align-items: center;
    gap: 6px;
    margin-right: 12px;
    padding: 4px 10px;
    background: #1e293b;
    border-radius: 6px;
    border: 1px solid #334155;
  }
  .engine-controls .engine-label {
    font-size: 11px;
    color: #64748b;
    font-weight: 500;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    margin-right: 4px;
  }
  .engine-controls .engine-status-dot {
    width: 8px; height: 8px;
    border-radius: 50%;
    background: #ef4444;
    transition: background 0.3s;
  }
  .engine-controls .engine-status-dot.running {
    background: #22c55e;
    box-shadow: 0 0 6px #22c55e88;
  }
  .engine-controls .engine-status-dot.starting {
    background: #f59e0b;
    animation: pulse 1s infinite;
  }
  @keyframes pulse {
    0%, 100% { opacity: 1; }
    50% { opacity: 0.4; }
  }
  .engine-btn {
    font-size: 11px;
    padding: 3px 8px;
    border-radius: 4px;
    border: 1px solid #475569;
    background: #1e293b;
    color: #94a3b8;
    cursor: pointer;
    transition: all 0.15s;
  }
  .engine-btn:hover {
    background: #334155;
    color: #e2e8f0;
  }
  .engine-btn.start { border-color: #22c55e55; }
  .engine-btn.start:hover { background: #22c55e22; color: #22c55e; }
  .engine-btn.stop { border-color: #ef444455; }
  .engine-btn.stop:hover { background: #ef444422; color: #ef4444; }
  .engine-btn.restart { border-color: #f59e0b55; }
  .engine-btn.restart:hover { background: #f59e0b22; color: #f59e0b; }
  .engine-btn:disabled { opacity: 0.4; cursor: not-allowed; }
  .engine-uptime {
    font-size: 10px;
    color: #475569;
    margin-left: 4px;
    font-variant-numeric: tabular-nums;
  }
"""

ENGINE_CONTROL_HTML = """
    <div class="engine-controls" id="engineControls">
      <span class="engine-label">Engine</span>
      <div class="engine-status-dot" id="engineDot"></div>
      <button class="engine-btn start" id="engineStartBtn" onclick="engineStart()" title="Start engine">Start</button>
      <button class="engine-btn stop" id="engineStopBtn" onclick="engineStop()" title="Stop engine" disabled>Stop</button>
      <button class="engine-btn restart" id="engineRestartBtn" onclick="engineRestart()" title="Restart engine" disabled>Restart</button>
      <span class="engine-uptime" id="engineUptime"></span>
    </div>
"""

ENGINE_CONTROL_JS = """
  // ── Engine control ──
  let engineRunning = false;

  async function engineStart() {
    document.getElementById('engineStartBtn').disabled = true;
    document.getElementById('engineDot').className = 'engine-status-dot starting';
    try {
      const r = await fetch('/api/engine/start');
      const d = await r.json();
      if (!d.ok) alert(d.message || 'Failed to start');
    } catch(e) { alert('Failed to start engine: ' + e); }
    setTimeout(pollEngineStatus, 1000);
  }

  async function engineStop() {
    document.getElementById('engineStopBtn').disabled = true;
    try {
      const r = await fetch('/api/engine/stop');
      const d = await r.json();
    } catch(e) { alert('Failed to stop engine: ' + e); }
    setTimeout(pollEngineStatus, 1000);
  }

  async function engineRestart() {
    document.getElementById('engineRestartBtn').disabled = true;
    document.getElementById('engineDot').className = 'engine-status-dot starting';
    try {
      const r = await fetch('/api/engine/restart');
      const d = await r.json();
      if (!d.ok) alert(d.message || 'Failed to restart');
    } catch(e) { alert('Failed to restart engine: ' + e); }
    setTimeout(pollEngineStatus, 2000);
  }

  function updateEngineUI(status) {
    engineRunning = status.running;
    const dot = document.getElementById('engineDot');
    const startBtn = document.getElementById('engineStartBtn');
    const stopBtn = document.getElementById('engineStopBtn');
    const restartBtn = document.getElementById('engineRestartBtn');
    const uptime = document.getElementById('engineUptime');

    if (status.running) {
      dot.className = 'engine-status-dot running';
      startBtn.disabled = true;
      stopBtn.disabled = false;
      restartBtn.disabled = false;
      if (status.uptime_seconds > 0) {
        const h = Math.floor(status.uptime_seconds / 3600);
        const m = Math.floor((status.uptime_seconds % 3600) / 60);
        const s = status.uptime_seconds % 60;
        uptime.textContent = h > 0 ? h+'h '+m+'m' : m+'m '+s+'s';
      }
    } else {
      dot.className = 'engine-status-dot';
      startBtn.disabled = false;
      stopBtn.disabled = true;
      restartBtn.disabled = true;
      uptime.textContent = status.error ? 'ERROR' : 'stopped';
      if (status.error) {
        uptime.style.color = '#ef4444';
      } else {
        uptime.style.color = '#475569';
      }
    }
  }

  async function pollEngineStatus() {
    try {
      const r = await fetch('/api/engine/status');
      const d = await r.json();
      updateEngineUI(d);
    } catch(e) {}
  }

  // Poll engine status every 5 seconds
  setInterval(pollEngineStatus, 5000);
  // Initial poll
  pollEngineStatus();

  // Listen to engine SSE stream
  const engineSSE = new EventSource('/api/engine/stream');
  engineSSE.addEventListener('engine_status', (e) => {
    try { updateEngineUI(JSON.parse(e.data)); } catch(err) {}
  });
"""


def main():
    global _engine_args

    parser = argparse.ArgumentParser(description="Portfolio D — Launcher")
    parser.add_argument("--host", default="127.0.0.1",
                        help="IBKR host (passed to engine)")
    parser.add_argument("--port", type=int, default=7497,
                        help="IBKR port (passed to engine)")
    parser.add_argument("--client-id", type=int, default=10,
                        help="IBKR client ID (passed to engine)")
    parser.add_argument("--no-ibkr", action="store_true",
                        help="Start engine in offline mode")
    parser.add_argument("--auto-start", action="store_true",
                        help="Automatically start the engine on launch")
    args = parser.parse_args()

    # Build engine args
    _engine_args = ["--host", args.host, "--port", str(args.port),
                    "--client-id", str(args.client_id)]
    if args.no_ibkr:
        _engine_args.append("--no-ibkr")

    # Start the launcher web server
    server = ThreadedHTTPServer(("0.0.0.0", DASHBOARD_PORT), LauncherHandler)
    print(f"Portfolio D Launcher running at http://localhost:{DASHBOARD_PORT}")
    print(f"  Engine will listen internally on port {ENGINE_PORT}")
    print(f"  Engine args: {' '.join(_engine_args)}")

    if args.auto_start:
        print("Auto-starting engine...")
        ok, msg = _start_engine()
        print(f"  {msg}")

    # Open browser
    import webbrowser
    webbrowser.open(f"http://localhost:{DASHBOARD_PORT}")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down launcher...")
        _stop_engine()
        server.shutdown()


if __name__ == "__main__":
    main()
