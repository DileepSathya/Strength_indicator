# stream_server.py  ── drop-in SSE broadcaster, no external deps beyond stdlib
import json, queue, threading
from pathlib import Path
from http.server import BaseHTTPRequestHandler, HTTPServer

_clients: list[queue.Queue] = []
_lock = threading.Lock()

def broadcast(data: dict):
    payload = f"data: {json.dumps(data)}\n\n"
    with _lock:
        dead = []
        for q in _clients:
            try:
                q.put_nowait(payload)
            except queue.Full:
                dead.append(q)
        for q in dead:
            _clients.remove(q)

class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass   # silence access log

    def do_GET(self):
        if self.path == "/":
            html = (Path(__file__).parent / "dashboard.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(html)
            return

        if self.path == "/events":
            self.send_response(200)
            self.send_header("Content-Type",  "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection",    "keep-alive")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            q: queue.Queue = queue.Queue(maxsize=64)
            with _lock:
                _clients.append(q)
            try:
                while True:
                    msg = q.get()          # blocks until next tick
                    self.wfile.write(msg.encode())
                    self.wfile.flush()
            except Exception:
                with _lock:
                    if q in _clients:
                        _clients.remove(q)
            return

        self.send_response(404); self.end_headers()

def start(host="localhost", port=5050):
    server = HTTPServer((host, port), _Handler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    print(f"[DASHBOARD] http://{host}:{port}  (opens automatically)")
    return server