"""Tests for the keep-awake loop that need no phone: the tap decision, behind-mark bookkeeping and protected apps.
Run from the skill directory: python3 -m unittest discover -s scripts/tests"""
import json
import os
import sys
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(HERE, "..", "..", "..", "_shared", "scripts"))
import ios_device  # noqa: E402,F401
import keepawake  # noqa: E402

# A stand-in protected app. config.protected_apps ships empty - only the person installing devlock
# knows which app on their phone must not be tapped - so these drive it explicitly.
TT = {"bundle_id": "com.example.app", "app_name": "Example.app"}
OTHER = "com.example.app.staff"


def app(alive=True, front=False, behind=False):
    return {"name": "Example.app", "alive": alive, "front": front, "behind": behind}


class DecideTest(unittest.TestCase):
    def test_taps_when_no_protected_app_runs(self):
        self.assertTrue(keepawake.decide([app(alive=False)])[0])

    def test_no_tap_while_wda_reports_it_in_front(self):
        # in front it keeps the screen on itself, and an XCUITest tap there can get it killed
        self.assertFalse(keepawake.decide([app(front=True, behind=True)])[0])
        self.assertFalse(keepawake.decide([app(front=True)])[0])

    def test_not_seen_in_front_is_not_behind(self):
        # front=False also means "WDA could not say"; a rule that tapped on that tapped an app in front
        self.assertFalse(keepawake.decide([app()])[0])

    def test_taps_when_marked_behind(self):
        self.assertTrue(keepawake.decide([app(behind=True)])[0])


class FreePhoneTest(unittest.TestCase):
    def test_free_phone_taps_after_n_checks_without_seeing_it_in_front(self):
        apps = [app(alive=True, front=False, behind=False)]
        self.assertFalse(keepawake.decide(apps, free=True, unseen={"Example.app": 2}, free_after=3)[0])
        tap, why = keepawake.decide(apps, free=True, unseen={"Example.app": 3}, free_after=3)
        self.assertTrue(tap)
        self.assertIn("free phone", why)

    def test_held_phone_stays_conservative(self):
        apps = [app(alive=True, front=False, behind=False)]
        self.assertFalse(keepawake.decide(apps, free=False, unseen={"Example.app": 99}, free_after=3)[0])

    def test_max_gap_net_on_free_phone(self):
        apps = [app(alive=True, front=False, behind=False)]
        self.assertFalse(keepawake.decide(apps, free=True, unseen={}, since_tap=100, max_gap=150)[0])
        tap, why = keepawake.decide(apps, free=True, unseen={}, since_tap=151, max_gap=150)
        self.assertTrue(tap)
        self.assertIn("since the last tap", why)
        self.assertFalse(keepawake.decide(apps, free=False, unseen={}, since_tap=999, max_gap=150)[0], "held phone: no net")
        in_front = [app(alive=True, front=True, behind=False)]
        self.assertFalse(keepawake.decide(in_front, free=True, unseen={}, since_tap=999, max_gap=150)[0], "in front")

    def test_app_in_front_never_tapped_even_when_free(self):
        apps = [app(alive=True, front=True, behind=False)]
        self.assertFalse(keepawake.decide(apps, free=True, unseen={"Example.app": 99}, free_after=3)[0])


FIXTURE = os.path.join(HERE, "fixtures", "springboard_idle_timer.txt")  # real lines, 2026-10-14, one iPhone 14


class IdleWatchTest(unittest.TestCase):
    def wait_for(self, cond, secs=5):
        end = time.time() + secs
        while time.time() < end and not cond():
            time.sleep(0.05)
        return cond()

    def test_parses_real_springboard_lines(self):
        modes = [m.group(3) for m in map(keepawake.IDLE_RE.search, open(FIXTURE, errors="replace")) if m]
        self.assertIn("Auto", modes)
        self.assertIn("Disabled", modes)
        self.assertEqual(modes[-1], "Disabled")

    def test_stream_state_then_forgotten_when_it_ends(self):
        cmd = [sys.executable, "-c", f"import sys,time; sys.stdout.write(open({FIXTURE!r}).read()); sys.stdout.flush(); time.sleep(1.5)"]
        w = keepawake.IdleWatch("U", cmd=cmd)
        self.assertTrue(self.wait_for(lambda: w.current() is not None))
        self.assertEqual(w.current()[0], "Disabled")
        self.assertEqual(w.describe()["source"], "syslog")
        self.assertTrue(self.wait_for(lambda: w.current() is None, 5), "stream ended: state dropped")
        self.assertEqual(w.describe()["source"], "fallback")

    def test_restart_carries_a_fresh_state_only(self):
        import tempfile
        d = tempfile.mkdtemp()
        sf = os.path.join(d, "idle_state")
        json.dump({"mode": "Auto", "reason": "SBHomeScreen", "since": time.time() - 300}, open(sf, "w"))
        quiet = [sys.executable, "-c", "import time; time.sleep(3)"]  # a live stream with no change yet
        w = keepawake.IdleWatch("U", cmd=quiet, state_file=sf)
        self.assertTrue(self.wait_for(lambda: w.current() is not None))
        self.assertEqual(w.current()[0], "Auto")
        self.assertIn("carried over", w.current()[1])
        old = time.time() - 600
        os.utime(sf, (old, old))
        w2 = keepawake.IdleWatch("U", cmd=quiet, state_file=sf)
        self.assertTrue(self.wait_for(lambda: w2.alive))
        self.assertIsNone(w2.current(), "a stale saved state is not used")

    def test_stream_gen_changes_on_every_restart(self):
        """self_awake_apps trusts an earlier sighting only within one stream: a restart may have missed a change."""
        quick = [sys.executable, "-c", "import time; time.sleep(0.3)"]
        w = keepawake.IdleWatch("U", cmd=quick)
        self.assertTrue(self.wait_for(lambda: w.stream_gen() == 1))
        self.assertTrue(self.wait_for(lambda: w.stream_gen() is None, 3), "between streams: no generation")
        self.assertTrue(self.wait_for(lambda: w.stream_gen() == 2, 8))

    def test_missing_tool_means_fallback(self):
        w = keepawake.IdleWatch("U", cmd=["definitely-not-installed-xyz"])
        self.assertTrue(self.wait_for(lambda: w.unavailable is not None))
        self.assertIsNone(w.current())
        self.assertIn("not installed", w.describe()["why"])


class IdleDecisionTest(unittest.TestCase):
    class Dev:
        udid = "U"
        def __init__(self): self.taps = 0
        def ensure_wda(self, timeout_s=None): return 0, "", 0.0
        def tap_points(self, x, y, timeout=120):
            self.taps += 1
            return 0, "", 0.1

    class Watch:
        def __init__(self, mode): self.mode = mode
        def current(self): return (self.mode, "test", time.time()) if self.mode else None
        def describe(self): return {"source": "syslog", "mode": self.mode, "reason": "test", "since": time.time()} if self.mode else {"source": "fallback", "why": "test"}

    def run_check(self, mode):
        import tempfile
        d = tempfile.mkdtemp()
        p = keepawake.paths(d)
        dev, lines = self.Dev(), []
        class A: protect = []
        keepawake.check(dev, A(), p, {}, {}, lines.append, self.Watch(mode))
        return dev.taps, lines, p

    def test_auto_taps_disabled_does_not(self):
        taps, lines, _ = self.run_check("Auto")
        self.assertEqual(taps, 1)
        taps, lines, p = self.run_check("Disabled")
        self.assertEqual(taps, 0)
        self.assertIn("keeps the screen on", lines[-1])
        self.assertIn("idle timer Disabled", open(p["signal"]).read())


class RetireWdaTest(unittest.TestCase):
    def test_should_retire(self):
        self.assertTrue(keepawake.should_retire(True, 601, 600))
        self.assertFalse(keepawake.should_retire(True, 599, 600), "not idle long enough")
        self.assertFalse(keepawake.should_retire(False, 9999, 600), "held phone, WDA use unknown: may be in use")
        self.assertFalse(keepawake.should_retire(True, 9999, 0), "disabled by config")

    def test_a_held_phone_is_retired_once_nobody_used_wda_for_long(self):
        self.assertFalse(keepawake.should_retire(False, 9999, 120, wda_idle_s=300, held_threshold=600), "used recently")
        self.assertTrue(keepawake.should_retire(False, 9999, 120, wda_idle_s=601, held_threshold=600))
        self.assertFalse(keepawake.should_retire(False, 9999, 120, wda_idle_s=9999, held_threshold=None), "off")

    def retire_run(self, free=True, runner=("7",), automation=False, wda_idle=9999):
        calls, lines = [], []
        class Dev:
            udid = "U"
            def wda_idle_s(self): return wda_idle
            def automation_processes(self):
                calls.append("ps")
                return {"device_wda_runner": list(runner), "mac_forwarder": []}
            def teardown_automation(self): calls.append("teardown")
        saved = (keepawake.phone_free, keepawake.other_phones, keepawake.automation_running, dict(keepawake.WDA),
                 dict(keepawake.RETIRE))
        try:
            keepawake.phone_free = lambda u: free
            keepawake.other_phones = lambda u: []
            keepawake.automation_running = lambda: automation
            keepawake.RETIRE["checked"] = 0.0
            keepawake.WDA.update(last_need=time.time() - 9999, retired=True)   # retired once already
            keepawake.maybe_retire(Dev(), lines.append)
            keepawake.maybe_retire(Dev(), lines.append)                      # inside wda_retire_check_s: no ask
        finally:
            (keepawake.phone_free, keepawake.other_phones, keepawake.automation_running, wda, retire) = saved
            keepawake.WDA.clear(); keepawake.WDA.update(wda)
            keepawake.RETIRE.clear(); keepawake.RETIRE.update(retire)
        return calls, lines

    def test_a_runner_someone_else_started_is_retired_even_after_an_earlier_retire(self):
        """2026-10-19: a hand-started runner kept "Automation Running" up, because the loop had retired an
        earlier one and never looked again."""
        calls, lines = self.retire_run()
        self.assertEqual(calls, ["ps", "teardown"])
        self.assertIn("free phone", lines[0])

    def test_nothing_on_the_phone_means_nothing_to_stop(self):
        self.assertEqual(self.retire_run(runner=()), (["ps"], []))

    def test_a_held_phone_running_appium_or_xcodebuild_test_is_left_alone(self):
        self.assertEqual(self.retire_run(free=False, automation=True), ([], []))
        calls, lines = self.retire_run(free=False, automation=False)
        self.assertEqual(calls, ["ps", "teardown"])
        self.assertIn("held phone", lines[0])
        self.assertEqual(self.retire_run(free=False, wda_idle=10), ([], []), "WDA used 10 s ago")

    def test_cold_tap_gets_the_longer_timeout(self):
        import tempfile
        seen = []
        class Dev:
            udid = "U"
            def ensure_wda(self, timeout_s=None): return 0, "", 0.0   # already up: nothing started
            def tap_points(self, x, y, timeout=120):
                seen.append(timeout)
                return 0, "", 0.1
        p = keepawake.paths(tempfile.mkdtemp())
        keepawake.WDA.update(retired=True)
        keepawake.tap(Dev(), p, "x", lambda l: None)
        keepawake.tap(Dev(), p, "x", lambda l: None)
        self.assertEqual(seen, [keepawake.K.get("tap_cold_timeout_s", 150), keepawake.K.get("tap_timeout_s", 75)])

    def test_tap_resets_the_idle_clock(self):
        class Dev:
            udid = "U"
            def ensure_wda(self, timeout_s=None): return 0, "", 0.0
            def tap_points(self, x, y, timeout=120): return 0, "", 0.1
        import tempfile
        p = keepawake.paths(tempfile.mkdtemp())
        keepawake.WDA.update(last_need=0, retired=True)
        keepawake.tap(Dev(), p, "x", lambda l: None)
        self.assertFalse(keepawake.WDA["retired"])
        self.assertLess(time.time() - keepawake.WDA["last_need"], 5)

    def test_appium_kept_when_other_phones_are_plugged(self):
        calls = []
        class Dev:
            udid = "U"
            def automation_processes(self): return {"device_wda_runner": ["7"], "mac_appium": ["9"]}
            def signal_pid(self, pid, sig): calls.append(("kill-runner", pid))
            def teardown_automation(self): calls.append("full-teardown")
        saved = keepawake.other_phones
        try:
            keepawake.other_phones = lambda u: ["OTHER"]
            keepawake.retire_wda(Dev(), lambda l: None, "t")
            self.assertEqual(calls, [("kill-runner", "7")])
            calls.clear()
            keepawake.other_phones = lambda u: []
            keepawake.retire_wda(Dev(), lambda l: None, "t")
            self.assertEqual(calls, ["full-teardown"])
        finally:
            keepawake.other_phones = saved


class StartWdaTest(unittest.TestCase):
    """Starting WebDriverAgent from the loop. The point is what happens when it cannot be started:
    one clear line per check and a backoff, never a retry every minute (see keepawake.md)."""

    class Dev:
        udid = "U"

        def __init__(self, ensure=(0, "runner started", 1.0), taps=(1, "Connection refused", 0.1)):
            self.ensure, self.taps_result, self.calls, self.taps = ensure, taps, [], []

        def ensure_wda(self, timeout_s=None):
            self.calls.append(timeout_s)
            return self.ensure

        def tap_points(self, x, y, timeout=120):
            self.taps.append(timeout)
            return self.taps_result if len(self.taps) == 1 else (0, "", 0.1)

    def setUp(self):
        import tempfile
        keepawake.ENSURE["blocked_until"] = 0.0
        keepawake.WDA.update(last_need=time.time(), retired=False)
        keepawake.FAILS["n"] = 0
        self.p = keepawake.paths(tempfile.mkdtemp())
        self.lines = []

    def test_a_failed_tap_starts_wda_and_the_tap_is_retried_once(self):
        dev = self.Dev()
        keepawake.tap(dev, self.p, "x", self.lines.append)
        self.assertEqual(len(dev.calls), 1, "started once, not per route")
        self.assertEqual(len(dev.taps), 2, "the tap is worth retrying once WDA is actually there")
        self.assertTrue(any("WDA started" in l for l in self.lines), self.lines)
        self.assertTrue(any("WDA tap ok" in l for l in self.lines), self.lines)

    def test_nothing_started_means_no_pointless_retry(self):
        dev = self.Dev(ensure=(0, "", 0.0))        # WDA was answering: the tap failed for another reason
        keepawake.tap(dev, self.p, "x", self.lines.append)
        self.assertEqual(len(dev.taps), 1)
        self.assertTrue(any("WDA tap FAILED" in l for l in self.lines), self.lines)

    def test_a_failure_logs_one_reason_and_blocks_the_next_attempt(self):
        dev = self.Dev(ensure=(1, "runner: ios is not installed - npm install -g go-ios", 0.2))
        keepawake.tap(dev, self.p, "x", self.lines.append)
        keepawake.tap(dev, self.p, "x", self.lines.append)
        self.assertEqual(len(dev.calls), 1, "it must not try to start WDA again every check")
        said = [l for l in self.lines if "would not start" in l]
        self.assertEqual(len(said), 1, said)
        self.assertIn("npm install -g go-ios", said[0])
        self.assertIn("not trying again for", said[0])
        self.assertGreater(keepawake.ENSURE["blocked_until"], time.time())

    def test_the_cold_tap_after_a_retire_starts_wda_before_tapping(self):
        keepawake.WDA.update(retired=True)
        dev = self.Dev(taps=(0, "", 0.1))
        keepawake.tap(dev, self.p, "x", self.lines.append)
        self.assertEqual(dev.calls, [keepawake.K.get("tap_cold_timeout_s", 150)],
                         "the cold budget is what it gets to come up in")
        self.assertEqual(len(dev.taps), 1)

    def test_an_exploding_ensure_does_not_end_the_loop(self):
        class Boom(self.Dev):
            def ensure_wda(self, timeout_s=None):
                raise RuntimeError("devicectl went away")
        keepawake.tap(Boom(), self.p, "x", self.lines.append)
        self.assertTrue(any("would not start" in l and "devicectl went away" in l for l in self.lines),
                        self.lines)

    def test_it_can_be_switched_off_in_config(self):
        dev = self.Dev()
        saved = keepawake.K.get("ensure_wda", True)
        keepawake.K["ensure_wda"] = False
        try:
            keepawake.tap(dev, self.p, "x", self.lines.append)
        finally:
            keepawake.K["ensure_wda"] = saved
        self.assertEqual(dev.calls, [])
        self.assertEqual(len(dev.taps), 1)


class HungQueryTest(unittest.TestCase):
    """2026-10-14: a hung front-app query raised TimeoutExpired, the loop died and the phone locked."""

    def test_query_timeout_neither_crashes_nor_stops_the_tap(self):
        import subprocess, tempfile, time as _t

        class Dev:
            udid = "U"
            taps = 0
            def processes(self): return [("1", "/x/Example.app/Example")]
            def pids_in(self, rows, name): return ["1"] if name == "Example.app" else []
            def app_name_for(self, b): return "Example.app"   # the holder's --bundle, resolved from the phone
            def front_app(self, timeout=30): raise subprocess.TimeoutExpired(["wda"], timeout)
            def behind(self, b, pid): return False
            def clear_behind(self, b): pass
            def ensure_wda(self, timeout_s=None): return 0, "", 0.0
            def tap_points(self, x, y, timeout=120):
                Dev.taps += 1
                return 0, "", 0.1

        d = tempfile.mkdtemp()
        p = keepawake.paths(d)
        open(p["last_tap"], "w").close()
        old = _t.time() - 200
        os.utime(p["last_tap"], (old, old))
        lines = []
        saved = keepawake.phone_free
        keepawake.phone_free = lambda udid: True
        try:
            class A: protect = [TT["bundle_id"]]
            keepawake.check(Dev(), A(), p, {}, {}, lines.append)
        finally:
            keepawake.phone_free = saved
        self.assertTrue(any("front-app query failed" in l for l in lines), lines)
        self.assertEqual(Dev.taps, 1, "200 s since the last tap on a free phone: tapped despite the failed query")
        self.assertLess(_t.time() - os.path.getmtime(p["last_tap"]), 5, "last tap time refreshed")


class FakeMarks:
    def __init__(self, marks):
        self.marks, self.cleared = dict(marks), []

    def behind(self, b, pid):
        return b in self.marks and self.marks[b] in (None, pid)

    def clear_behind(self, b):
        self.cleared.append(b)
        self.marks.pop(b, None)


class ObserveTest(unittest.TestCase):
    B = TT["bundle_id"]

    def observe(self, dev, pid, front=None):
        return keepawake.observe([TT], {self.B: [pid] if pid else []}, front, dev)[0]

    def test_mark_holds_for_its_pid_only(self):
        dev = FakeMarks({self.B: "10"})
        self.assertTrue(self.observe(dev, "10")["behind"])
        self.assertFalse(self.observe(dev, "11")["behind"])   # relaunched: the mark was for another process
        self.assertEqual(dev.cleared, [self.B])

    def test_mark_dropped_when_the_app_turns_out_to_be_in_front(self):
        dev = FakeMarks({self.B: "10"})
        self.assertFalse(self.observe(dev, "10", front=self.B)["behind"])
        self.assertNotIn(self.B, dev.marks)

    def test_an_unreadable_front_app_is_not_read_as_in_front(self):
        """front=None is "WDA could not say", which must not clear a behind-mark or look like in front."""
        dev = FakeMarks({self.B: "10"})
        row = self.observe(dev, "10", front=None)
        self.assertFalse(row["front"])
        self.assertTrue(row["behind"])


class FakeNames:
    def __init__(self, names):
        self.names, self.asked = names, []

    def app_name_for(self, b):
        self.asked.append(b)
        return self.names.get(b)


class ProtectedAppsTest(unittest.TestCase):
    def test_an_empty_config_is_fine_and_extras_come_from_the_phone(self):
        """config.protected_apps ships empty; the phone holder's `--bundle` arrives as `extra`, and its
        <Name>.app has to be read from the phone because only the bundle id was given."""
        self.assertEqual(keepawake.CFG["protected_apps"], [], "config ships with no app named")
        dev = FakeNames({OTHER: "ExampleStaff.app", "x.y": "X.app"})
        got = keepawake.protected_apps(dev, [OTHER, "x.y"])
        self.assertEqual(got, [{"bundle_id": OTHER, "app_name": "ExampleStaff.app"},
                               {"bundle_id": "x.y", "app_name": "X.app"}])

    def test_a_configured_app_name_is_used_as_is(self):
        dev = FakeNames({})
        got = keepawake.protected_apps(dev, (), names={TT["bundle_id"]: TT["app_name"]})
        self.assertEqual(got, [])                        # nothing configured and nothing extra
        got = keepawake.protected_apps(dev, [TT["bundle_id"]], names={TT["bundle_id"]: TT["app_name"]})
        self.assertEqual(got, [TT])
        self.assertNotIn(TT["bundle_id"], dev.asked)     # the cached name is used, the phone is not asked

    def test_not_installed_left_out_and_asked_again_found_ones_cached(self):
        dev, names = FakeNames({}), {}
        self.assertEqual(keepawake.protected_apps(dev, [OTHER], names=names), [])
        dev.names[OTHER] = "ExampleStaff.app"
        self.assertEqual(len(keepawake.protected_apps(dev, [OTHER], names=names)), 1)
        asked = len(dev.asked)
        keepawake.protected_apps(dev, [OTHER], names=names)
        self.assertEqual(len(dev.asked), asked)

    def test_a_second_protected_app_in_front_is_not_tapped(self):
        """The 2026-10-13 case: a second protected build was in front while the first was not running."""
        dev = FakeNames({OTHER: "ExampleStaff.app", TT["bundle_id"]: TT["app_name"]})
        protected = keepawake.protected_apps(dev, [TT["bundle_id"], OTHER])
        pids = {TT["bundle_id"]: [], OTHER: ["22"]}
        dev.behind, dev.clear_behind = (lambda b, pid: False), (lambda b: None)
        apps = keepawake.observe(protected, pids, OTHER, dev)
        self.assertFalse(keepawake.decide(apps)[0])


class SelfAwakeTest(unittest.TestCase):
    """config.self_awake_apps: an app that keeps the screen on by itself (an in-app "keep screen on" setting) is not
    tapped while it is in front and the idle-timer signal is not known yet - the phone plugged in with the app
    already in front, where SpringBoard logs nothing until something changes."""
    APP = {"bundle_id": "com.example.selfawake", "app_name": "SelfAwake.app"}

    class Dev:
        udid = "U"

        def __init__(self, front="com.example.selfawake", pid="7"):
            self.front, self.pid, self.queries, self.taps, self.ensured, self.torn = front, pid, 0, 0, 0, 0

        def processes(self): return [(self.pid, "/x/SelfAwake.app/SelfAwake")] if self.pid else []
        def pids_in(self, rows, name): return [r[0] for r in rows if f"/{name}/" in r[1]]
        def app_name_for(self, b): return None
        def behind(self, b, pid): return False
        def clear_behind(self, b): pass
        def front_app(self, timeout=30):
            self.queries += 1
            return self.front
        def ensure_wda(self, timeout_s=None):
            self.ensured += 1
            return 0, "", 0.0
        def tap_points(self, x, y, timeout=120):
            self.taps += 1
            return 0, "", 0.1
        def teardown_automation(self): self.torn += 1
        def automation_processes(self): return {"device_wda_runner": ["9"], "mac_forwarder": []}

    class Watch:
        """A running stream with no idle-timer change seen yet: the state is unknown."""
        def __init__(self, gen=1, mode=None): self.gen, self.mode = gen, mode
        def current(self): return (self.mode, "test", time.time()) if self.mode else None
        def describe(self):
            if self.mode:
                return {"source": "syslog", "mode": self.mode, "reason": "test", "since": time.time()}
            return {"source": "fallback", "why": "no idle-timer change seen since the stream started"}
        def stream_gen(self): return self.gen

    def setUp(self):
        import tempfile
        self.saved = (keepawake.CFG.get("self_awake_apps"), keepawake.phone_free, dict(keepawake.WDA),
                      keepawake.other_phones)
        keepawake.CFG["self_awake_apps"] = [self.APP]
        keepawake.phone_free = lambda udid: False
        keepawake.other_phones = lambda udid: []
        keepawake.SELF_AWAKE.clear()
        keepawake.RETIRE["checked"] = 0.0
        self.p = keepawake.paths(tempfile.mkdtemp())
        self.lines = []

    def tearDown(self):
        keepawake.CFG["self_awake_apps"], keepawake.phone_free, wda, keepawake.other_phones = self.saved
        keepawake.WDA.clear()
        keepawake.WDA.update(wda)
        keepawake.SELF_AWAKE.clear()

    def check(self, dev, watch):
        class A: protect = []
        keepawake.check(dev, A(), self.p, {}, {}, self.lines.append, watch)

    def test_ships_empty(self):
        self.assertEqual(json.load(open(os.path.join(HERE, "..", "..", "config.json")))["self_awake_apps"], [])

    def test_in_front_is_not_tapped_and_wda_is_asked_once_per_stream(self):
        dev, watch = self.Dev(), self.Watch()
        self.check(dev, watch)
        self.check(dev, watch)
        self.assertEqual(dev.taps, 0)
        self.assertEqual(dev.queries, 1, "no foreground change logged since: still in front, no new XCUITest query")
        self.assertIn("listed in self_awake_apps", self.lines[0])
        self.assertIn("no foreground change logged", self.lines[1])
        watch.gen = 2                                  # the stream restarted: a change may have been missed
        self.check(dev, watch)
        self.assertEqual(dev.queries, 2)

    def test_a_relaunch_is_asked_again(self):
        dev, watch = self.Dev(), self.Watch()
        self.check(dev, watch)
        dev.pid = "8"
        self.check(dev, watch)
        self.assertEqual(dev.queries, 2)

    def test_running_but_not_in_front_is_tapped(self):
        """Unlike a protected app, a self-awake app behind changes nothing."""
        dev = self.Dev(front="com.apple.Preferences")
        self.check(dev, self.Watch())
        self.assertEqual(dev.taps, 1)
        self.assertEqual(dev.queries, 1)

    def test_not_running_costs_no_query(self):
        dev = self.Dev(pid=None)
        self.check(dev, self.Watch())
        self.assertEqual((dev.taps, dev.queries), (1, 0))

    def test_the_idle_timer_decides_once_known(self):
        """An app whose in-app setting is off reads Auto: tapped, whatever the list says."""
        dev, watch = self.Dev(), self.Watch()
        self.check(dev, watch)
        watch.mode = "Auto"
        self.check(dev, watch)
        self.assertEqual((dev.taps, dev.queries), (1, 1))
        self.assertEqual(keepawake.SELF_AWAKE, {})

    def test_unanswered_query_starts_wda_and_asks_again(self):
        class Cold(self.Dev):
            def front_app(self, timeout=30):
                self.queries += 1
                return None if self.queries == 1 else self.front
            def ensure_wda(self, timeout_s=None):
                self.ensured += 1
                return 0, "runwda started", 3.0
        saved = dict(keepawake.ENSURE)
        try:
            dev = Cold()
            self.check(dev, self.Watch())
        finally:
            keepawake.ENSURE.update(saved)
        self.assertEqual((dev.taps, dev.queries, dev.ensured), (0, 2, 1))

    def test_without_a_syslog_stream_wda_is_asked_every_check_and_kept(self):
        keepawake.phone_free = lambda udid: True
        keepawake.WDA.update(last_need=time.time() - 999, retired=False)
        dev, watch = self.Dev(), self.Watch(gen=None)
        self.check(dev, watch)
        self.check(dev, watch)
        self.assertEqual((dev.taps, dev.queries, dev.torn), (0, 2, 0))
        dev2 = self.Dev()                              # with a stream, a free phone's idle runner is retired
        keepawake.SELF_AWAKE.clear()
        keepawake.RETIRE["checked"] = 0.0
        self.check(dev2, self.Watch(gen=1))
        self.assertEqual(dev2.torn, 1)


if __name__ == "__main__":
    unittest.main()
