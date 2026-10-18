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
import shutil
import subprocess
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
                ready_after=0,                   # /status calls before `ready` flips to true
                status_calls=0,
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
            FAKE["status_calls"] += 1
            ready = FAKE["status_calls"] > FAKE["ready_after"]
            value = {"state": "success", "ready": ready, "build": {"version": "8.9.2"}}
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

    # ---- is it up at all ----
    def test_ready_follows_the_status_payload(self):
        reset_fake(ready_after=1)
        w = self.client()
        self.assertFalse(w.ready(), "ready:false is not up, even though /status answered")
        self.assertTrue(w.ready())

    def test_ready_is_false_when_nothing_is_listening(self):
        self.assertFalse(self.client("http://127.0.0.1:1").ready())


class EnsureRunningTest(unittest.TestCase):
    """Starting the runner. The real steps are go-ios and iproxy against a phone (wda.py's header
    has what that was verified to do); what a phone cannot conveniently show is the awkward half,
    so the steps here are small processes that stand in for them: one that comes up and stays up,
    one that exits the way `ios runwda` exits when it loses testmanagerd, one that is not installed."""

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
        self.token = "cstack-ensure-" + os.path.basename(self.tmp)
        self.spawned = []

    def tearDown(self):
        subprocess.run(["pkill", "-f", self.token], capture_output=True)
        for proc in self.spawned:
            proc.terminate()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def client(self, steps, url=None, **cfg):
        ensure = {"steps": steps, "log_dir": self.tmp, "step_timeout_s": 10, "timeout_s": 20, "poll_s": 0.2}
        ensure.update(cfg)
        return wda.WDA(url or self.url, state_file=os.path.join(self.tmp, "s.json"),
                       timeout_s=5, ensure=ensure)

    def stays_up(self, name, **kw):
        """A step that prints go-ios's `authorized` line and then sits there, like the XCTest host."""
        step = {"name": name, "ready_log": '"authorized":true', "match": f"^never-matches-{name}",
                "cmd": [sys.executable, "-u", "-c",
                        'print("{\\"authorized\\":true}"); import time; time.sleep(30)', self.token]}
        step.update(kw)
        return step

    def dies(self, name, **kw):
        """A step that exits the way `ios runwda` does when the test runner is killed under it."""
        step = {"name": name, "ready_log": '"authorized":true', "match": f"^never-matches-{name}",
                "cmd": [sys.executable, "-u", "-c",
                        'print("{\\"level\\":\\"ERROR\\",\\"error\\":\\"lost connection to testmanagerd\\"}");'
                        ' raise SystemExit(3)', self.token]}
        step.update(kw)
        return step

    # ---- the happy path ----
    def test_already_answering_starts_nothing(self):
        rc, out, _ = self.client([self.dies("runner")]).ensure_running(udid="UDID-1")
        self.assertEqual((rc, out), (0, ""), "a WDA that is ready must not be restarted")
        self.assertEqual(FAKE["status_calls"], 1, "one /status call, no steps")

    def test_steps_run_in_order_and_then_status_decides(self):
        reset_fake(ready_after=1)                 # not up until after the steps have run
        rc, out, secs = self.client([self.stays_up("tunnel"), self.stays_up("runner")]).ensure_running(udid="UDID-1")
        self.assertEqual(rc, 0, out)
        self.assertEqual(out, "tunnel started, runner started")
        self.assertLess(secs, 20)

    def test_udid_and_port_are_substituted_into_cmd_and_match(self):
        reset_fake(ready_after=1)
        step = self.stays_up("runner", cmd=[sys.executable, "-u", "-c",
                                            'import sys; print("{\\"authorized\\":true}"); '
                                            'open(sys.argv[2], "w").write(sys.argv[1]); '
                                            'import time; time.sleep(30)',
                                            "{udid}:{port}", os.path.join(self.tmp, "args")],
                             match="^never-matches-{udid}")
        w = wda.WDA(f"http://127.0.0.1:{self.server.server_port}",
                    state_file=os.path.join(self.tmp, "s.json"), timeout_s=5,
                    ensure={"steps": [step], "log_dir": self.tmp, "timeout_s": 20, "poll_s": 0.2})
        rc, out, _ = w.ensure_running(udid="UDID-1")
        self.assertEqual(rc, 0, out)
        self.assertEqual(open(os.path.join(self.tmp, "args")).read(),
                         f"UDID-1:{self.server.server_port}")

    def test_a_step_without_a_ready_log_settles_and_is_accepted(self):
        reset_fake(ready_after=1)
        step = {"name": "forwarder", "settle_s": 0.2, "match": f"^never-matches-forwarder",
                "cmd": [sys.executable, "-c", "import time; time.sleep(30)", self.token]}
        rc, out, _ = self.client([step]).ensure_running(udid="UDID-1")
        self.assertEqual((rc, out), (0, "forwarder started"))

    def test_a_step_already_running_is_skipped_not_restarted(self):
        reset_fake(ready_after=1)
        running = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", self.token],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.spawned.append(running)
        step = self.dies("runner", match=f".*{self.token}")   # it would fail if it were started
        rc, out, _ = self.client([step]).ensure_running(udid="UDID-1")
        self.assertEqual((rc, out), (0, "runner already up"))

    # ---- one clear reason, every time it cannot ----
    def test_a_step_that_exits_is_retried_and_then_reported_with_its_own_error(self):
        reset_fake(ready_after=99)
        rc, out, _ = self.client([self.dies("runner", attempts=2)]).ensure_running(udid="UDID-1")
        self.assertEqual(rc, 1)
        self.assertIn("runner exited rc 3", out)
        self.assertIn("lost connection to testmanagerd", out, "the step's own words, not ours")
        self.assertIn("after 2 attempts", out)
        self.assertEqual(len(open(os.path.join(self.tmp, "runner.log")).read().strip().splitlines()), 1,
                         "each attempt starts a fresh log")

    def test_a_tool_that_is_not_installed_says_so_with_the_install_hint(self):
        step = {"name": "runner", "cmd": ["cstack-no-such-tool"], "hint": "npm install -g go-ios"}
        rc, out, _ = self.client([step], url="http://127.0.0.1:1").ensure_running(udid="UDID-1")
        self.assertEqual(rc, 1)
        self.assertIn("cstack-no-such-tool is not installed", out)
        self.assertIn("npm install -g go-ios", out)

    def test_no_configured_steps_says_there_is_nothing_to_start(self):
        rc, out, _ = self.client([], url="http://127.0.0.1:1").ensure_running()
        self.assertEqual(rc, 1)
        self.assertIn("nothing to start", out)
        self.assertIn("127.0.0.1:1", out, "the reason names the url nobody answered on")

    def test_steps_that_need_a_udid_refuse_without_one(self):
        rc, out, _ = self.client([self.stays_up("runner", match="^x-{udid}")],
                                 url="http://127.0.0.1:1").ensure_running()
        self.assertEqual(rc, 1)
        self.assertIn("UDID", out)

    def test_steps_up_but_status_never_ready_gives_up_inside_the_budget(self):
        reset_fake(ready_after=999)
        rc, out, secs = self.client([self.stays_up("runner")], timeout_s=3).ensure_running(udid="UDID-1")
        self.assertEqual(rc, 1)
        self.assertIn("runner started", out)
        self.assertIn("never reported ready", out)
        self.assertLess(secs, 10, "it must not sit there past its budget")

    def test_a_step_that_never_reports_its_marker_times_out_with_its_log_path(self):
        reset_fake(ready_after=999)
        step = {"name": "runner", "ready_log": "never printed", "match": "^never-matches",
                "cmd": [sys.executable, "-c", "import time; time.sleep(30)", self.token]}
        rc, out, _ = self.client([step], step_timeout_s=1).ensure_running(udid="UDID-1")
        self.assertEqual(rc, 1)
        self.assertIn("runner did not report ready", out)
        self.assertIn(os.path.join(self.tmp, "runner.log"), out)


class EnsureHelpersTest(unittest.TestCase):
    def test_fill_substitutes_both_placeholders_and_leaves_regex_alone(self):
        self.assertEqual(wda._fill("^(\\S*/)?iproxy {port} 8100 -u {udid}", {"port": 8100, "udid": "U-1"}),
                         "^(\\S*/)?iproxy 8100 8100 -u U-1")

    def test_why_prefers_the_last_error_of_a_json_log(self):
        path = os.path.join(tempfile.mkdtemp(), "runner.log")
        with open(path, "w") as f:
            f.write('{"level":"INFO","msg":"Running wda"}\n'
                    '{"level":"ERROR","error":"first one"}\n'
                    '{"level":"ERROR","error":"lost connection to testmanagerd"}\n')
        self.assertEqual(wda._why(path), "lost connection to testmanagerd")

    def test_why_falls_back_to_the_last_line_and_never_raises(self):
        d = tempfile.mkdtemp()
        path = os.path.join(d, "runner.log")
        open(path, "w").write("starting\nplain text failure\n\n")
        self.assertEqual(wda._why(path), "plain text failure")
        self.assertEqual(wda._why(os.path.join(d, "not-there.log")), "")

    def test_the_default_config_steps_are_the_verified_ones(self):
        """The steps are config, not code - but the ones this repo ships are the ones wda.py's
        header says were verified, so a silent edit of either should fail here."""
        cfg = wda.config()
        self.assertEqual(cfg["url"], "http://127.0.0.1:8100")
        steps = {s["name"]: s for s in cfg["ensure"]["steps"]}
        self.assertEqual(list(steps), ["tunnel", "runner", "forwarder"])
        self.assertEqual(steps["tunnel"]["cmd"], ["ios", "tunnel", "start", "--userspace", "--udid", "{udid}"])
        self.assertEqual(steps["runner"]["cmd"], ["ios", "runwda", "--udid", "{udid}"])
        self.assertEqual(steps["runner"]["attempts"], 2, "1 of 4 host starts needed a retry")
        self.assertEqual(steps["forwarder"]["cmd"], ["iproxy", "{port}", "8100", "-u", "{udid}"])
        for step in steps.values():
            self.assertTrue(step["match"].startswith("^("), f"{step['name']}: anchor the match pattern")


if __name__ == "__main__":
    unittest.main(verbosity=2)
