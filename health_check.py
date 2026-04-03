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
        pass  # silence default HTTP logs


def _self_ping(port: int, interval: int = 300):
    """Ping own health endpoint every `interval` seconds to prevent sleep."""
    # Wait for server to start
    time.sleep(10)

    url = os.getenv("KOYEB_PUBLIC_DOMAIN")
    if url:
        ping_url = f"https://{url}/"
    else:
        ping_url = f"http://localhost:{port}/"

    while True:
        try:
            urllib.request.urlopen(ping_url, timeout=10)
            print(f"[PING] Self-ping OK → {ping_url}")
        except Exception as e:
            print(f"[PING] Self-ping failed: {e}")
        time.sleep(interval)


def start_health_server(port: int = 8000):
    server = HTTPServer(("0.0.0.0", port), _Handler)

    # HTTP server thread
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    print(f"[HEALTH] Server running on port {port}")

    # Self-ping thread (every 5 minutes)
    ping_thread = threading.Thread(target=_self_ping, args=(port, 300), daemon=True)
    ping_thread.start()
    print("[PING] Self-ping thread started (every 5 min)")
