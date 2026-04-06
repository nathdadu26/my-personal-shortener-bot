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
        pass  # silence HTTP logs


def _self_ping(port: int, interval: int = 270):
    """Ping public URL every 4.5 min to prevent sleep."""
    time.sleep(15)

    public_domain = os.getenv("KOYEB_PUBLIC_DOMAIN", "").strip()

    if public_domain:
        ping_url = f"https://{public_domain}/"
    else:
        # Fallback: localhost (works locally, not on Koyeb — set env var on Koyeb)
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
