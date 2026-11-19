"""A stand-in ntfy server on 127.0.0.1 for the alerts tests. Nothing here talks to any other host.

    with FakeNtfy() as server:
        server.mode = "ok"          # ok | refuse (closed port) | hang | slow | status
        server.status = 503         # with mode "status"
        ...
        server.received             # [{"body": {...}, "headers": {...}, "raw": bytes}]

Run by hand (python tests/alerts/fake_ntfy.py [port]) it accepts everything and prints what it got.
"""
import http.server
import json
import socket
import sys
import threading
import time


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_POST(self):
        fake = self.server.fake
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        fake.attempts += 1
        mode = fake.mode
        if mode == "hang":
            fake.release.wait(30)
            return
        if mode == "slow":
            # Answers byte by byte, each byte within the client's per-read timeout.
            self.wfile.write(b"HTTP/1.1 200 OK\r\n")
            for _ in range(200):
                if fake.release.wait(0.2):
                    return
                try:
                    self.wfile.write(b"X")
                    self.wfile.flush()
                except OSError:
                    return
            return
        if mode == "redirect":
            self._answer(302, b"", {"Location": f"http://127.0.0.1:{fake.port}/elsewhere"})
            return
        status = fake.status if mode == "status" else 200
        try:
            body = json.loads(raw)
        except ValueError:
            body = None
        if status == 200:
            fake.received.append({"body": body, "headers": {k.lower(): v for k, v in self.headers.items()},
                                  "raw": raw, "path": self.path})
            reply = json.dumps({"id": f"m{len(fake.received)}", "event": "message"}).encode()
        else:
            fake.refused.append({"status": status, "body": body})
            reply = json.dumps({"code": status * 100, "http": status, "error": f"stand-in says {status}"}).encode()
        extra = {"Retry-After": str(fake.retry_after)} if status == 429 and fake.retry_after is not None else {}
        self._answer(status, reply, extra)

    def do_GET(self):
        self.server.fake.gets += 1
        self._answer(405, b"{}", {})

    def _answer(self, status, body, headers):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for k, v in headers.items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)


class _Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class FakeNtfy:
    def __init__(self, port=0):
        self.mode = "ok"
        self.status = 200
        self.retry_after = None
        self.received = []
        self.refused = []
        self.attempts = 0
        self.gets = 0
        self.release = threading.Event()
        self._server = _Server(("127.0.0.1", port), _Handler)
        self._server.fake = self
        self.port = self._server.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self.release.set()
        self._server.shutdown()
        self._server.server_close()

    def bodies(self):
        return [r["body"] for r in self.received]

    def wait_for(self, count, timeout=20.0):
        deadline = time.monotonic() + timeout
        while len(self.received) < count and time.monotonic() < deadline:
            time.sleep(0.05)
        return len(self.received) >= count


def closed_port():
    """A port on 127.0.0.1 where nothing listens: a refused connection."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return f"http://127.0.0.1:{port}"


if __name__ == "__main__":
    with FakeNtfy(int(sys.argv[1]) if len(sys.argv) > 1 else 0) as fake:
        print(f"listening on {fake.url}", flush=True)
        seen = 0
        try:
            while True:
                time.sleep(0.2)
                for r in fake.received[seen:]:
                    print(json.dumps(r["body"]), flush=True)
                seen = len(fake.received)
        except KeyboardInterrupt:
            pass
