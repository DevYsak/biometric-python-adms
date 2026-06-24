from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs
from datetime import datetime
import urllib.request

LOG_FILE = "attendance_logs.txt"
PORT     = 5000
LARAVEL  = "http://127.0.0.1:8000"   # Laravel app


class ADMSHandler(BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        return

    def save_log(self, content):
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(content + "\n")

    # ------------------------------------------------------------------
    # GET  — forward to Laravel (handshake + getrequest)
    # ------------------------------------------------------------------
    def do_GET(self):
        parsed = urlparse(self.path)

        log = f"""
==================================================
TIME   : {datetime.now()}
METHOD : GET
PATH   : {parsed.path}
QUERY  : {parse_qs(parsed.query)}
==================================================
"""
        print(log)
        self.save_log(log)

        # Forward to Laravel and relay its response back to the device
        try:
            fwd_url = LARAVEL + self.path
            req     = urllib.request.Request(fwd_url, method="GET")
            with urllib.request.urlopen(req, timeout=5) as resp:
                body = resp.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(body)
            print(f"  → forwarded GET to Laravel, response: {body[:80]}")
        except Exception as e:
            print(f"  → Laravel forward error: {e}")
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"OK")

    # ------------------------------------------------------------------
    # POST — log locally, forward to Laravel, relay stamp back to device
    # ------------------------------------------------------------------
    def do_POST(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body_bytes     = self.rfile.read(content_length)
        body           = body_bytes.decode("utf-8", errors="ignore")

        log = f"""
==================================================
TIME   : {datetime.now()}
METHOD : POST
PATH   : {self.path}
LENGTH : {content_length}

BODY:
{body}
==================================================
"""
        print(log)
        self.save_log(log)

        # Forward to Laravel and relay its reply (contains new stamp)
        laravel_reply = b"OK"
        try:
            fwd_url = LARAVEL + self.path
            req     = urllib.request.Request(
                fwd_url,
                data=body_bytes,
                method="POST",
                headers={"Content-Type": "text/plain"},
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                laravel_reply = resp.read()
            print(f"  → forwarded POST to Laravel, reply: {laravel_reply.strip()}")
        except Exception as e:
            print(f"  → Laravel forward error: {e}")

        # Reply to device with whatever Laravel said (e.g. "OK: 42")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(laravel_reply)


if __name__ == "__main__":
    print(f"ADMS Proxy Server Running On Port {PORT}")
    print(f"Forwarding all punches → {LARAVEL}")
    print("Waiting for biometric device data...\n")

    server = HTTPServer(("0.0.0.0", PORT), ADMSHandler)
    server.serve_forever()
