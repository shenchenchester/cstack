"""Tests for the shared device layer that need no phone: behind-marks, process matching, the phone's app list,
lock state, automation teardown, the WDA client wiring, and the devlock phone-lock check. The WDA client itself
is tested against a fake WDA in test_wda.py, and verified on hardware - see wda.py's header.
Run from skills/_shared: python3 -m unittest discover -s scripts/tests"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import ios_device  # noqa: E402

TT = {"bundle_id": "com.example.app", "app_name": "Example.app"}

APPS = {"result": {"apps": [
    {"bundleIdentifier": "com.example.app", "name": "Example Staff",
     "url": "file:///private/var/containers/Bundle/Application/A/Example.app/"},
    {"bundleIdentifier": "com.example.app.staff", "name": "Example",
     "url": "file:///private/var/containers/Bundle/Application/B/ExampleStaff.app/"},
]}}


class DeviceStateTest(unittest.TestCase):
    """IOSDevice with sh() scripted; __init__ (which needs a phone) is skipped."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.saved = ios_device.sh, ios_device.STATE
        ios_device.STATE = self.tmp
        self.dev = ios_device.IOSDevice.__new__(ios_device.IOSDevice)
        self.dev.base_cfg, self.dev._wda, self.dev.udid, self.dev.core = ios_device.CFG, None, "U", "C"

    def tearDown(self):
        ios_device.sh, ios_device.STATE = self.saved

    def test_the_wda_client_is_built_per_phone_from_config(self):
        """One forwarder per phone, so the URL may be set per UDID, and the session file is per UDID too -
        otherwise two phones would hand each other a session id that is not theirs."""
        self.dev.base_cfg = {**ios_device.CFG, "wda": {**ios_device.CFG["wda"], "url": "http://127.0.0.1:8100",
                                                       "url_by_udid": {"OTHER": "http://127.0.0.1:9100"}}}
        self.assertEqual(self.dev.wda.url, "http://127.0.0.1:8100")
        self.assertTrue(self.dev.wda.state_file.endswith("wda_session.U.json"))
        self.dev._wda, self.dev.udid = None, "OTHER"
        self.assertEqual(self.dev.wda.url, "http://127.0.0.1:9100")

    def test_tap_and_front_app_go_straight_to_the_wda_client(self):
        calls = []

        class FakeWDA:
            def tap(self, x, y, timeout=None):
                calls.append(("tap", x, y, timeout))
                return 0, "", 0.1

            def front_app(self, timeout=None):
                calls.append(("front", timeout))
                return "com.example.app"

            def session_warm(self):
                return True

        self.dev._wda = FakeWDA()
        self.assertEqual(self.dev.tap_points(4, 4, timeout=75), (0, "", 0.1))
        self.assertEqual(self.dev.front_app(), "com.example.app")
        self.assertTrue(self.dev.session_warm())
        self.assertEqual(calls, [("tap", 4, 4, 75), ("front", 30)])

    def test_behind_mark_set_by_background_cleared_by_launch(self):
        ios_device.sh = lambda cmd, timeout=120: (0, "", 0.1)
        b = TT["bundle_id"]
        self.dev.background_app(b, pid="10")
        self.assertTrue(self.dev.behind(b, "10"))
        self.assertFalse(self.dev.behind(b, "11"))
        self.dev.launch_app(b)
        self.assertFalse(self.dev.behind(b, "10"))

    def test_pids_are_the_main_executable_of_that_app_only(self):
        rows = [("11", "/private/var/containers/Bundle/Application/A/Example.app/Example"),
                ("22", "/private/var/containers/Bundle/Application/B/ExampleStaff.app/ExampleStaff"),
                ("33", "/private/var/containers/Bundle/Application/B/ExampleStaff.app/PlugIns/Share.appex/Share")]
        self.assertEqual(self.dev.pids_in(rows, "ExampleStaff.app"), ["22"])
        self.assertEqual(self.dev.pids_in(rows, "Example.app"), ["11"])

    def test_app_name_for_reads_the_phone_app_list(self):
        def fake_sh(cmd, timeout=120):
            with open(cmd[cmd.index("--json-output") + 1], "w") as f:
                json.dump(APPS, f)
            return 0, "", 0.1
        ios_device.sh = fake_sh
        self.assertEqual(self.dev.app_name_for("com.example.app.staff"), "ExampleStaff.app")
        self.assertIsNone(self.dev.app_name_for("not.installed"))

    def test_locked_reads_passcode_required(self):
        ios_device.sh = lambda cmd, timeout=120: (0, "Current device lock state:\n• passcodeRequired: true\n", 0.1)
        self.assertIs(self.dev.locked(), True)
        ios_device.sh = lambda cmd, timeout=120: (0, "• passcodeRequired: false\n• unlockedSinceBoot: true\n", 0.1)
        self.assertIs(self.dev.locked(), False)
        ios_device.sh = lambda cmd, timeout=120: (1, "ERROR: device not found", 0.1)
        self.assertIsNone(self.dev.locked())

    def test_teardown_stops_the_wda_runner_and_the_forwarder(self):
        """Verified on hardware 2026-10-18: the runner pid on the phone and exactly one iproxy - the
        anchored pgrep pattern must not also catch the shell whose command line mentions iproxy."""
        calls, dropped = [], []

        def fake_sh(cmd, timeout=120):
            calls.append(cmd)
            if "processes" in cmd:
                return 0, ("12584   /private/var/containers/Bundle/Application/X/"
                           "com.facebook.WebDriverAgentRunner.xctrunner.app/WebDriverAgentRunner-Runner\n"
                           "12595   /private/var/containers/Bundle/Application/Y/Example.app/Example\n"), 0.1
            if cmd[0] == "pgrep":
                return 1, "", 0.1   # no forwarder running on this Mac
            return 0, "", 0.1
        ios_device.sh = fake_sh
        self.dev._wda = type("W", (), {"drop_session": lambda s: dropped.append(1)})()
        procs = self.dev.teardown_automation()
        self.assertEqual(procs, {"device_wda_runner": ["12584"], "mac_forwarder": []})
        self.assertEqual(dropped, [1], "the session is dropped before the runner is signalled")
        signals = [c for c in calls if "signal" in c]
        self.assertEqual(len(signals), 1)
        self.assertIn("12584", signals[0])

    def test_the_forwarder_pattern_is_anchored_on_the_executable(self):
        import re
        pat = ios_device.CFG["teardown"]["mac_process_pattern"]
        self.assertTrue(re.search(pat, "iproxy 8100 8100 -u 00008110-0001"))
        self.assertTrue(re.search(pat, "/opt/homebrew/bin/iproxy 8100 8100 -u X"))
        self.assertIsNone(re.search(pat, "bash -c 'nohup iproxy 8100 8100 -u X &'"),
                          "a shell that merely mentions it is not the forwarder")


class AppNameFromAppsJsonTest(unittest.TestCase):
    def test_maps_bundle_to_app_dir_not_display_name(self):
        self.assertEqual(ios_device.app_name_from_apps_json(APPS, "com.example.app.staff"), "ExampleStaff.app")
        self.assertEqual(ios_device.app_name_from_apps_json(APPS, TT["bundle_id"]), "Example.app")

    def test_unknown_bundle_or_odd_url_is_none(self):
        self.assertIsNone(ios_device.app_name_from_apps_json(APPS, "x.y"))
        self.assertIsNone(ios_device.app_name_from_apps_json(
            {"result": {"apps": [{"bundleIdentifier": "x", "url": None}]}}, "x"))
        self.assertIsNone(ios_device.app_name_from_apps_json({}, "x"))


class InstallHintTest(unittest.TestCase):
    """Without devlock the device layer suggests installing it, at most once a day."""

    def setUp(self):
        import io
        self.tmp = tempfile.mkdtemp()
        self.saved = ios_device.devlock, ios_device.STATE, ios_device.INSTALL_HINT, sys.stderr
        ios_device.devlock = lambda: None
        ios_device.STATE = self.tmp
        ios_device.INSTALL_HINT = os.path.join(self.tmp, "hint")
        self.err = sys.stderr = io.StringIO()

    def tearDown(self):
        ios_device.devlock, ios_device.STATE, ios_device.INSTALL_HINT, sys.stderr = self.saved

    def test_once_a_day(self):
        os.environ.pop("DEVLOCK_INFRA", None)
        ios_device.require_phone_lock("U")
        self.assertIn("ios-build-device-lock/scripts/install.py", self.err.getvalue())
        n = self.err.getvalue().count("[devlock]")
        ios_device.require_phone_lock("U")
        self.assertEqual(self.err.getvalue().count("[devlock]"), n, "second call the same day stays quiet")


class PhoneLockTest(unittest.TestCase):
    """require_phone_lock: a no-op without devlock or for the daemon, an exit when the caller does not hold the phone."""

    def setUp(self):
        self.saved = ios_device.devlock, subprocess.run, os.environ.get("DEVLOCK_INFRA")
        os.environ.pop("DEVLOCK_INFRA", None)

    def tearDown(self):
        ios_device.devlock, subprocess.run = self.saved[0], self.saved[1]
        if self.saved[2] is not None:
            os.environ["DEVLOCK_INFRA"] = self.saved[2]

    def fake_mine(self, rc):
        calls = []
        def run(cmd, **kw):
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, rc)
        ios_device.subprocess.run = run
        return calls

    def test_no_devlock_no_check(self):
        ios_device.devlock = lambda: None
        ios_device.require_phone_lock("U")

    def test_held_passes_and_asks_for_that_phone(self):
        ios_device.devlock = lambda: "/x/devlock"
        calls = self.fake_mine(0)
        ios_device.require_phone_lock("U")
        self.assertEqual(calls, [["/x/devlock", "mine", "device-U"]])

    def test_not_held_exits_with_the_fix(self):
        ios_device.devlock = lambda: "/x/devlock"
        self.fake_mine(1)
        with self.assertRaises(SystemExit) as e:
            ios_device.require_phone_lock("U")
        self.assertIn("devlock acquire --name device-U", str(e.exception))

    def test_daemon_is_exempt(self):
        ios_device.devlock = lambda: "/x/devlock"
        self.fake_mine(1)
        os.environ["DEVLOCK_INFRA"] = "1"
        ios_device.require_phone_lock("U")

if __name__ == "__main__":
    unittest.main()
