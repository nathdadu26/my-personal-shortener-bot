import os
import threading
import time
import urllib.request
from http.server import HTTPServer, BaseHTTPRequestHandler


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, format, *args):
        pass


def _self_ping(port: int, interval: int = 270):
    time.sleep(15)

    public_domain = os.getenv("KOYEB_PUBLIC_DOMAIN", "").strip()

    # Remove any accidental https:// or http:// prefix from env var
    public_domain = public_domain.replace("https://", "").replace("http://", "").rstrip("/")

    if public_domain:
        ping_url = f"https://{public_domain}/"
    else:
        ping_url = f"http://localhost:{port}/"

    while True:
        try:
            urllib.request.urlopen(ping_url, timeout=10)
            print(f"[PING] OK → {ping_url}")
        except Exception as e:
            print(f"[PING] Failed ({ping_url}): {e}")
        time.sleep(interval)


def start_health_server(port: int = 8000):
    server = HTTPServer(("0.0.0.0", port), _Handler)

    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    print(f"[HEALTH] Server running on port {port}")

    ping_thread = threading.Thread(target=_self_ping, args=(port,), daemon=True)
    ping_thread.start()
    print("[PING] Self-ping thread started (every 4.5 min)")
