"""A stand-in CostKey server: project config, replay chunks and assets, and a shop page to drive."""
from __future__ import annotations

import gzip
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List

PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c6360f8cfc00000030101006b8e1e1a0000000049454e44ae426082"
)

SHOP = """<!doctype html><html><head><title>Northwind Supply</title></head><body>
<h1>Northwind Supply</h1>
<img src="/lantern.png" alt="Trail lantern" width="40" height="40">
<button id="add" onclick="document.getElementById('count').textContent='1'">Add to cart</button>
<span id="count">0</span>
<label>Email <input id="email" type="email"></label>
<a id="next" href="/checkout">Checkout</a>
</body></html>"""

CHECKOUT = """<!doctype html><html><body><h1>Checkout</h1><button id="pay">Pay $48.00</button></body></html>"""


class FakeCostKey:
    def __init__(self, screen_capture: bool = True) -> None:
        self.screen_capture = screen_capture
        self.chunks: List[Dict[str, Any]] = []
        self.assets: Dict[str, bytes] = {}
        self.headers: List[Dict[str, str]] = []
        self.chunk_status = 202
        self.lock = threading.Lock()
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:  # quiet
                pass

            def _send(self, status: int, body: bytes = b"", content_type: str = "application/json") -> None:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)

            def do_GET(self) -> None:  # noqa: N802
                if self.path.endswith("/config"):
                    self._send(200, json.dumps({"capture": {"screenCaptureEnabled": server.screen_capture}}).encode())
                elif self.path == "/shop":
                    self._send(200, SHOP.encode(), "text/html")
                elif self.path == "/checkout":
                    self._send(200, CHECKOUT.encode(), "text/html")
                elif self.path == "/lantern.png":
                    self._send(200, PNG, "image/png")
                else:
                    self._send(404)

            def do_HEAD(self) -> None:  # noqa: N802
                sha = self.path.rsplit("/", 1)[-1]
                self._send(200 if sha in server.assets else 404)

            def do_PUT(self) -> None:  # noqa: N802
                data = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                with server.lock:
                    server.assets[self.path.rsplit("/", 1)[-1]] = data
                self._send(201)

            def do_POST(self) -> None:  # noqa: N802
                data = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                if self.path.endswith("/replay/chunks"):
                    if self.headers.get("Content-Encoding") == "gzip":
                        data = gzip.decompress(data)
                    with server.lock:
                        server.chunks.append(json.loads(data))
                        server.headers.append(dict(self.headers))
                    self._send(server.chunk_status, b"{}")
                else:
                    self._send(404)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def dsn(self) -> str:
        return f"http://ck_test@127.0.0.1:{self.port}/proj_test"

    def start(self) -> "FakeCostKey":
        self.thread.start()
        return self

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

    def events(self) -> List[Dict[str, Any]]:
        with self.lock:
            return [event for chunk in self.chunks for event in chunk["events"]]

    def custom(self, tag: str) -> List[Dict[str, Any]]:
        return [event["data"]["payload"] for event in self.events() if event.get("type") == 5 and event.get("data", {}).get("tag") == tag]
