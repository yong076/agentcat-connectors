"""Local sockets, synthetic traffic, no provider or CLI access."""
import http.client
import importlib.util
import json
from pathlib import Path
import threading
import unittest

spec = importlib.util.spec_from_file_location("gateway_lab", Path(__file__).resolve().parents[1] / "experiments/gateway_lab.py")
lab = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lab)


class GatewayLabTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.mode = "json"
        self.release_stream = threading.Event()
        owner = self

        class Fixture(lab.QuietHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                owner.calls.append((self.path, dict(self.headers), body))
                if owner.mode == "429":
                    return self.reply(429, {"error": "secret upstream message"})
                if owner.mode == "redirect":
                    self.send_response(302)
                    self.send_header("Location", "http://127.0.0.1:1/private")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if owner.mode == "sse":
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self.close_connection = True
                    try:
                        self.wfile.write(b'data: {"delta":"hello"}\n\n')
                        self.wfile.flush()
                        owner.release_stream.wait(3)
                        self.wfile.write(b'data: {"tool_call":{"arguments":"{}"}}\n\ndata: [DONE]\n\n')
                        self.wfile.flush()
                    except OSError:
                        pass
                    return
                self.reply(200, {"model": body["model"], "tools": body.get("tools"), "usage": {"input_tokens": 3}})

        self.fixture = lab.FixtureServer(("127.0.0.1", 0), Fixture)
        self.thread = threading.Thread(target=self.fixture.serve_forever, daemon=True)
        self.thread.start()
        self.gateway = lab.GatewayLab(self.fixture).__enter__()
        self.addCleanup(self.cleanup_servers)

    def cleanup_servers(self):
        self.release_stream.set()
        self.gateway.__exit__()
        self.fixture.shutdown()
        self.fixture.server_close()
        self.thread.join()

    def request(self, path="/v1/chat/completions", body=None, headers=None, method="POST"):
        conn = http.client.HTTPConnection("127.0.0.1", self.gateway.port, timeout=5)
        self.addCleanup(conn.close)
        base = {"Authorization": "Bearer " + self.gateway.token, "Content-Type": "application/json"}
        base.update(headers or {})
        payload = json.dumps({"model": "chat/demo", "messages": []} if body is None else body)
        conn.request(method, path, payload if method == "POST" else None, base)
        return conn.getresponse()

    def test_all_three_protocols_keep_tool_payload_and_usage(self):
        tools = [{"name": "example", "input_schema": {"type": "object"}}]
        for alias, (path, model) in lab.ROUTES.items():
            response = self.request(path, {"model": alias, "tools": tools})
            self.assertEqual(response.status, 200)
            result = json.loads(response.read())
            self.assertEqual(result, {"model": model, "tools": tools, "usage": {"input_tokens": 3}})
            self.assertEqual(self.calls[-1][0], path)

    def test_stream_is_incremental_and_tool_chunks_unchanged(self):
        self.mode = "sse"
        response = self.request(body={"model": "chat/demo", "stream": True})
        self.assertEqual(response.status, 200)
        # Upstream cannot complete until this assertion releases it: buffering
        # the whole response would time out and fail here.
        self.assertEqual(response.readline(), b'data: {"delta":"hello"}\n')
        self.release_stream.set()
        self.assertEqual(response.read(), b'\ndata: {"tool_call":{"arguments":"{}"}}\n\ndata: [DONE]\n\n')

    def test_browser_wrong_host_and_bad_auth_never_reach_upstream(self):
        for headers in ({"Origin": "http://example.test"}, {"Host": "attacker.test"}, {"Authorization": "Bearer wrong"}):
            response = self.request(headers=headers)
            self.assertEqual(response.status, 403)
            response.read()
        self.assertEqual(self.calls, [])

    def test_unknown_route_protocol_and_body_rejected_before_upstream(self):
        for path, body, status in [("/v1/chat/completions?url=secret", {}, 404),
                                   ("/v1/chat/completions", {"model": "unknown"}, 400),
                                   ("/v1/messages", {"model": "chat/demo"}, 422),
                                   ("/v1/messages", [], 400),
                                   ("/v1/messages", {"model": {}}, 400)]:
            response = self.request(path, body)
            self.assertEqual(response.status, status)
            response.read()
        self.assertEqual(self.calls, [])

    def test_redirect_and_rate_limit_are_not_retried_or_exposed(self):
        for mode, status in [("redirect", 502), ("429", 429)]:
            self.mode = mode
            response = self.request()
            self.assertEqual(response.status, status)
            self.assertEqual(json.loads(response.read()), {"error": "upstream_failed"})
        self.assertEqual(len(self.calls), 2)

    def test_private_headers_never_reach_fixture(self):
        response = self.request(headers={"Cookie": "private", "X-Project": "private", "X-Api-Key": "private"})
        response.read()
        headers = {k.lower(): v for k, v in self.calls[0][1].items()}
        self.assertFalse(set(headers) & {"authorization", "cookie", "x-project", "x-api-key"})

    def test_size_framing_and_capacity_are_bounded(self):
        response = self.request(headers={"Content-Length": str(lab.MAX_BODY + 1)})
        self.assertEqual(response.status, 413)
        response.read()
        response = self.request(headers={"Transfer-Encoding": "chunked"})
        self.assertEqual(response.status, 400)
        response.read()
        for _ in range(4):
            self.gateway.slots.acquire()
        try:
            response = self.request()
            self.assertEqual(response.status, 503)
            response.read()
        finally:
            for _ in range(4):
                self.gateway.slots.release()
        self.assertEqual(self.calls, [])
        response = self.request()
        self.assertEqual(response.status, 200)
        response.read()

    def test_models_are_local_only_and_external_upstream_cannot_be_selected(self):
        response = self.request("/v1/models", method="GET")
        self.assertEqual({x["id"] for x in json.loads(response.read())["data"]}, set(lab.ROUTES))
        self.assertEqual(self.calls, [])
        with self.assertRaises(ValueError):
            lab.GatewayLab("https://provider.example")

    def test_disconnected_client_does_not_poison_next_request(self):
        self.mode = "sse"
        response = self.request()
        self.assertEqual(response.readline(), b'data: {"delta":"hello"}\n')
        response.close()
        self.release_stream.set()
        self.mode = "json"
        response = self.request()
        self.assertEqual(response.status, 200)
        response.read()
