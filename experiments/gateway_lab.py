"""Agent Cat-owned gateway feasibility lab; synthetic loopback traffic only.

Not imported by the daemon, included in release archives, or able to select a
real upstream. The injected fixture must be an in-process loopback HTTP server.
No CLI config, provider credentials, database, logging or telemetry is used.
"""
import contextlib
import hmac
import http.client
import json
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


ROUTES = {
    "chat/demo": ("/v1/chat/completions", "fixture-chat"),
    "responses/demo": ("/v1/responses", "fixture-responses"),
    "messages/demo": ("/v1/messages", "fixture-messages"),
}
MAX_BODY = 1024 * 1024
MAX_REPLY = 8 * 1024 * 1024


class QuietHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def reply(self, status, value):
        body = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        self.wfile.write(body)


class FixtureServer(ThreadingHTTPServer):
    """Marker for a fixture constructed by the local lab, never a URL target."""
    daemon_threads = True


class GatewayLab:
    def __init__(self, fixture):
        if not isinstance(fixture, FixtureServer) or fixture.server_address[0] != "127.0.0.1":
            raise ValueError("in_process_loopback_fixture_required")
        self.token = secrets.token_urlsafe(32)
        self.slots = threading.BoundedSemaphore(4)
        lab = self

        class Handler(QuietHandler):
            def setup(self):
                super().setup()
                self.connection.settimeout(2)

            def authorized(self):
                if self.headers.get("Origin") is not None:
                    return False
                if self.headers.get("Host") != "127.0.0.1:%d" % self.server.server_port:
                    return False
                values = self.headers.get_all("Authorization", [])
                return len(values) == 1 and hmac.compare_digest(
                    values[0].encode(), ("Bearer " + lab.token).encode())

            def do_GET(self):
                if not self.authorized():
                    return self.reply(403, {"error": "local_access_required"})
                if self.path != "/v1/models":
                    return self.reply(404, {"error": "unknown_path"})
                self.reply(200, {"object": "list", "data": [
                    {"id": alias, "object": "model", "owned_by": "agentcat-fixture"} for alias in ROUTES]})

            def do_POST(self):
                if not self.authorized():
                    return self.reply(403, {"error": "local_access_required"})
                if self.path not in {route[0] for route in ROUTES.values()}:
                    return self.reply(404, {"error": "unknown_path"})
                lengths = self.headers.get_all("Content-Length", [])
                if self.headers.get("Transfer-Encoding") is not None or len(lengths) != 1:
                    return self.reply(400, {"error": "invalid_framing"})
                try:
                    length = int(lengths[0])
                except ValueError:
                    return self.reply(400, {"error": "invalid_length"})
                if not 0 < length <= MAX_BODY:
                    return self.reply(413, {"error": "request_size"})
                if self.headers.get_content_type() != "application/json":
                    return self.reply(415, {"error": "json_required"})
                if not lab.slots.acquire(blocking=False):
                    return self.reply(503, {"error": "busy"})
                connection = None
                started = False
                try:
                    raw = self.rfile.read(length)
                    if len(raw) != length:
                        return self.reply(400, {"error": "incomplete_body"})
                    body = json.loads(raw)
                    if not isinstance(body, dict) or not isinstance(body.get("model"), str):
                        return self.reply(400, {"error": "model_required"})
                    route = ROUTES.get(body["model"])
                    if route is None:
                        return self.reply(400, {"error": "unknown_model"})
                    if route[0] != self.path:
                        return self.reply(422, {"error": "protocol_mismatch"})
                    body["model"] = route[1]
                    connection = http.client.HTTPConnection("127.0.0.1", fixture.server_port, timeout=2)
                    # A fresh allowlist, never forwarding caller auth, cookies,
                    # project/session headers or arbitrary routing headers.
                    connection.request("POST", self.path, json.dumps(body).encode(),
                                       {"Content-Type": "application/json"})
                    response = connection.getresponse()
                    if response.status != 200:
                        # No redirects, fallback, retry or provider error text.
                        status = response.status if response.status in (401, 403, 429, 503) else 502
                        return self.reply(status, {"error": "upstream_failed"})
                    content_type = response.getheader("Content-Type", "").split(";", 1)[0]
                    if content_type not in ("application/json", "text/event-stream"):
                        return self.reply(502, {"error": "invalid_upstream_type"})
                    self.send_response(200)
                    self.send_header("Content-Type", content_type)
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self.close_connection = True
                    started = True
                    size = 0
                    while True:
                        chunk = response.read1(4096)
                        if not chunk:
                            break
                        size += len(chunk)
                        if size > MAX_REPLY:
                            break
                        self.wfile.write(chunk)
                        self.wfile.flush()
                except (ValueError, UnicodeError):
                    if not started:
                        self.reply(400, {"error": "invalid_json"})
                except (OSError, http.client.HTTPException):
                    if not started:
                        with contextlib.suppress(OSError):
                            self.reply(502, {"error": "upstream_unavailable"})
                finally:
                    self.close_connection = True
                    if connection:
                        connection.close()
                    lab.slots.release()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def port(self):
        return self.server.server_port

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
