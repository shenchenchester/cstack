#!/usr/bin/env python3
"""Tests for the WebDriverAgent client, against a fake WDA served on localhost.

The happy path is verified on hardware (see wda.py's header). What a phone cannot conveniently
show is the awkward half: a runner that serves a *different* tap route, a session that has died
under us, a 200 that is really an error, nothing listening at all. Those are what this pins, so
the fallbacks stay honest when nobody has a phone plugged in.
"""
import http.server
import json
import os
import sys
import tempfile
import threading
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
import wda  # noqa: E402

# What the fake serves. Tests mutate this between calls.
FAKE = {}


def reset_fake(**kw):
    FAKE.clear()
    FAKE.update(tap_routes={"/wda/tap"},        # path suffixes this runner accepts
                front_routes={"session"},        # "session" and/or "bare"
                session="S1",                    # the id the fake considers live, or None
                status_reports_session=True,
                front="com.example.app",
                log=[])
    FAKE.update(kw)


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n).decode() if n else ""
        try:
            return json.loads(raw) if raw else None
        except ValueError:
            return raw

    def _session_of(self, path):
        parts = path.split("/")
        return parts[2] if len(parts) > 2 and parts[1] == "session" else None

    def _dispatch(self, method):
        body = self._read()
        FAKE["log"].append((method, self.path, body))
        if self.path == "/status":
            value = {"state": "success", "ready": True, "build": {"version": "8.9.2"}}
            out = {"value": value}
            if FAKE["status_reports_session"] and FAKE["session"]:
                out["sessionId"] = FAKE["session"]
            return self._send(200, out)
        if self.path == "/session" and method == "POST":
            FAKE["session"] = FAKE["session"] or "S-new"
            return self._send(200, {"value": {"sessionId": FAKE["session"], "capabilities": {}}})
        sid = self._session_of(self.path)
        if sid and sid != FAKE["session"]:
            return self._send(404, {"value": {"error": "invalid session id",
                                              "message": f"Session {sid} does not exist"}})
        if method == "DELETE":
            FAKE["session"] = None
            return self._send(200, {"value": None})
        if self.path.endswith("/wda/activeAppInfo"):
            want = "bare" if not sid else "session"
            if want not in FAKE["front_routes"]:
                return self._send(404, {"value": {"error": "unknown command"}})
            return self._send(200, {"value": {"bundleId": FAKE["front"], "pid": 42}})
        for suffix in FAKE["tap_routes"]:
            if self.path.endswith(suffix):
                return self._send(200, {"value": None})
        return self._send(404, {"value": {"error": "unknown command", "message": self.path}})

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_DELETE(self):
        self._dispatch("DELETE")


class WDATest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        cls.url = f"http://127.0.0.1:{cls.server.server_port}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        reset_fake()
        self.tmp = tempfile.mkdtemp()
        self.state = os.path.join(self.tmp, "wda_session.json")

    def client(self, url=None, **kw):
        return wda.WDA(url or self.url, state_file=self.state, timeout_s=5, **kw)

    def paths(self, method=None):
        return [p for m, p, _ in FAKE["log"] if method in (None, m)]

    # ---- the tap route, and finding it ----
    def test_tap_uses_the_first_route_and_remembers_it(self):
        w = self.client()
        self.assertEqual(w.tap(4, 4)[0], 0)
        self.assertEqual(json.load(open(self.state))["tap_route"], "wda_tap")
        self.assertIn("/session/S1/wda/tap", self.paths("POST"))
        FAKE["log"].clear()
        self.assertEqual(w.tap(4, 4)[0], 0)
        self.assertEqual([p for p in self.paths("POST") if "tap" in p], ["/session/S1/wda/tap"])

    def test_tap_body_is_the_point(self):
        self.client().tap(7, 9)
        body = next(b for m, p, b in FAKE["log"] if p.endswith("/wda/tap"))
        self.assertEqual(body, {"x": 7, "y": 9})

    def test_falls_back_to_the_w3c_actions_route(self):
        reset_fake(tap_routes={"/actions"})
        w = self.client()
        self.assertEqual(w.tap(4, 4)[0], 0)
        self.assertEqual(json.load(open(self.state))["tap_route"], "w3c_actions")
        body = next(b for m, p, b in FAKE["log"] if p.endswith("/actions"))
        moves = body["actions"][0]["actions"]
        self.assertEqual(moves[0], {"type": "pointerMove", "duration": 0, "x": 4, "y": 4})
        self.assertEqual([a["type"] for a in moves], ["pointerMove", "pointerDown", "pause", "pointerUp"])

    def test_falls_back_to_the_indexed_tap_route(self):
        reset_fake(tap_routes={"/wda/tap/0"})
        self.assertEqual(self.client().tap(4, 4)[0], 0)
        self.assertEqual(json.load(open(self.state))["tap_route"], "wda_tap_0")

    def test_no_route_accepted_is_rc_1_naming_the_url(self):
        reset_fake(tap_routes=set())
        rc, out, _ = self.client().tap(4, 4)
        self.assertEqual(rc, 1)
        self.assertIn(self.url, out)

    def test_nothing_listening_is_rc_1_not_an_exception(self):
        rc, out, _ = self.client("http://127.0.0.1:1").tap(4, 4)
        self.assertEqual(rc, 1)
        self.assertIn("/session", out)

    # ---- sessions ----
    def test_an_existing_session_is_reused_not_recreated(self):
        self.client().tap(4, 4)
        self.assertNotIn("/session", self.paths("POST"))

    def test_a_session_is_created_when_status_reports_none(self):
        reset_fake(session=None, status_reports_session=False)
        self.assertEqual(self.client().tap(4, 4)[0], 0)
        self.assertIn("/session", self.paths("POST"))

    def test_a_dead_session_id_is_replaced_once_and_the_call_succeeds(self):
        w = self.client()
        json.dump({"url": self.url, "session_id": "GONE"}, open(self.state, "w"))
        self.assertEqual(w.tap(4, 4)[0], 0)
        self.assertEqual(json.load(open(self.state))["session_id"], "S1")
        self.assertIn("/session/GONE/wda/tap", self.paths("POST"))

    def test_session_warm_follows_last_use_and_the_idle_window(self):
        w = self.client(session_idle_s=50)
        self.assertFalse(w.session_warm())
        w.tap(4, 4)
        self.assertTrue(w.session_warm())
        s = json.load(open(self.state))
        s["last_use"] -= 60
        json.dump(s, open(self.state, "w"))
        self.assertFalse(w.session_warm())

    def test_drop_session_deletes_it_and_forgets_it(self):
        w = self.client()
        w.tap(4, 4)
        w.drop_session()
        self.assertIn("DELETE", [m for m, _, _ in FAKE["log"]])
        self.assertIsNone(json.load(open(self.state))["session_id"])

    def test_state_for_another_url_is_not_reused(self):
        w = self.client()
        w.tap(4, 4)
        self.assertEqual(wda.WDA("http://127.0.0.1:1", state_file=self.state)._load(), {})

    # ---- which app is in front ----
    def test_front_app_reads_the_bundle_id(self):
        self.assertEqual(self.client().front_app(), "com.example.app")
        self.assertEqual(json.load(open(self.state))["front_route"], "session_active_app")

    def test_front_app_falls_back_to_the_session_less_route(self):
        reset_fake(front_routes={"bare"})
        self.assertEqual(self.client().front_app(), "com.example.app")
        self.assertEqual(json.load(open(self.state))["front_route"], "bare_active_app")

    def test_front_app_is_none_when_no_route_answers(self):
        reset_fake(front_routes=set())
        self.assertIsNone(self.client().front_app())

    def test_front_app_is_none_when_nothing_is_listening(self):
        self.assertIsNone(self.client("http://127.0.0.1:1").front_app())

    # ---- "200" is not the same as "it worked" ----
    def test_a_200_carrying_an_error_is_a_failure(self):
        self.assertFalse(wda._ok(200, {"value": {"error": "unknown command"}}))

    def test_a_jsonwp_nonzero_status_is_a_failure(self):
        self.assertFalse(wda._ok(200, {"status": 13, "value": "oops"}))
        self.assertTrue(wda._ok(200, {"status": 0, "value": None}))

    def test_a_non_json_body_does_not_raise(self):
        self.assertFalse(wda._ok(200, {"value": {"error": "not json", "message": "<html>"}}))


if __name__ == "__main__":
    unittest.main(verbosity=2)
