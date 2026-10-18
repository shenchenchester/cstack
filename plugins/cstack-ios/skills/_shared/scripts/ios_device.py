#!/usr/bin/env python3
"""Low-level control of a USB iPhone: Xcode's devicectl for processes and app lifecycle, WebDriverAgent
(XCUITest, over HTTP - see wda.py) for coordinate taps and for which app is in front.

This plugin's shared device layer (skills/_shared): every skill that drives a phone imports this file, so they all
read and write the same state - the WDA session per phone, and which apps we sent to the background. Import it with
    sys.path.insert(0, os.path.join(<skill dir>, "..", "_shared", "scripts")); import ios_device

Phone coordination (devlock, skill ios-build-device-lock): with no --udid the phone this session holds
($DEVLOCK_DEVICE) is used, and on a Mac with devlock installed IOSDevice refuses a phone the caller does not hold.
"""
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import urllib.parse

import wda

HERE = os.path.dirname(os.path.abspath(__file__))
CFG = json.load(open(os.path.join(HERE, "..", "ios_device.json")))
ENV = dict(os.environ)
STATE = os.path.expanduser("~/.cache/ios-device")


def sh(cmd, timeout=120):
    """Run, return (rc, stdout+stderr, seconds)."""
    t0 = time.time()
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL, env=ENV)
        return p.returncode, p.stdout + p.stderr, time.time() - t0
    except subprocess.TimeoutExpired:
        return 124, f"TIMEOUT after {timeout}s", time.time() - t0


def app_name_from_apps_json(data, bundle):
    """`<Name>.app` for `bundle` from `devicectl device info apps --json-output` (its `url` is the .app path), or None.
    Not the `name` field: that is the display name ("Example"), while process paths and crash logs use the .app name."""
    for app in data.get("result", {}).get("apps", []):
        if app.get("bundleIdentifier") == bundle:
            base = os.path.basename(urllib.parse.unquote(urllib.parse.urlparse(app.get("url") or "").path).rstrip("/"))
            return base if base.endswith(".app") else None
    return None


def devlock():
    """Path of the devlock CLI when this Mac coordinates phones with it, else None."""
    return shutil.which("devlock")


INSTALL_HINT = os.path.join(STATE, "devlock_install_hint")


def suggest_devlock_install():
    """On a Mac without devlock, say once a day how to install it (to stderr): it is what stops several agent sessions
    from building at once or driving the same phone."""
    try:
        if os.path.exists(INSTALL_HINT) and time.time() - os.path.getmtime(INSTALL_HINT) < 86400:
            return
        os.makedirs(STATE, exist_ok=True)
        open(INSTALL_HINT, "w").close()
    except OSError:
        return
    script = os.path.realpath(os.path.join(HERE, "..", "..", "ios-build-device-lock", "scripts", "install.py"))
    sys.stderr.write("[devlock] not installed on this Mac. If other agent sessions (Claude Code, Cursor, a cloud agent) build or "
                     "use phones here too, suggest installing it so they queue instead of colliding:\n"
                     f"  python3 {script}\n")


def require_phone_lock(udid):
    """Exit unless the caller holds this phone in devlock (no-op without devlock, and for the keep-awake daemon)."""
    if os.environ.get("DEVLOCK_INFRA") == "1":
        return
    dl = devlock()
    if not dl:
        suggest_devlock_install()
        return
    if subprocess.run([dl, "mine", f"device-{udid}"], capture_output=True).returncode != 0:
        sys.exit(f"[devlock] this session does not hold phone {udid}. Take it first - "
                 f"`devlock acquire --name device-{udid} --bundle <app> --note '<what>'` (waits while another session "
                 f"holds it) - or wrap the command: `devlock --name device-{udid} -- <command>`.")


def _marker(bundle):
    return os.path.join(STATE, f"behind.{bundle}")


class IOSDevice:
    def __init__(self, udid=None, cfg=CFG, need_lock=True):
        self.base_cfg = cfg
        udid = udid or os.environ.get("DEVLOCK_DEVICE")
        self._wda = None
        tmp = "/tmp/ios_device_list.json"
        sh(["xcrun", "devicectl", "list", "devices", "--json-output", tmp])
        devs = json.load(open(tmp))["result"]["devices"]
        usable = [d for d in devs if d.get("connectionProperties", {}).get("tunnelState") != "unavailable"
                  and d.get("hardwareProperties", {}).get("platform") == "iOS"]
        if udid:
            usable = [d for d in usable if udid in (d["hardwareProperties"].get("udid"), d["identifier"])]
        if len(usable) != 1:
            sys.exit(f"need exactly one connected iPhone, found {len(usable)}; pass --udid")
        self.core = usable[0]["identifier"]  # CoreDevice id, for devicectl
        self.udid = usable[0]["hardwareProperties"]["udid"]  # hardware UDID, for WDA's forwarder and the idevice* tools
        if need_lock:
            require_phone_lock(self.udid)

    # ---- keep-awake (devlock's daemon taps the status bar while no protected app may be in front) ----
    def pause_keepawake(self):
        """Hold off keep-awake taps around a step that must not be disturbed (expires after 120 s; call again to extend)."""
        if devlock():
            subprocess.run([devlock(), "awake", "pause", "--udid", self.udid], capture_output=True)

    def resume_keepawake(self):
        if devlock():
            subprocess.run([devlock(), "awake", "resume", "--udid", self.udid], capture_output=True)

    # ---- devicectl: processes and lifecycle (no XCUITest, never touches an app's main thread) ----
    def processes(self):
        """[(pid, executable path)] of everything running on the phone."""
        _, out, _ = sh(["xcrun", "devicectl", "device", "info", "processes", "--device", self.core], 60)
        rows = []
        for line in out.splitlines():
            parts = line.split(None, 1)
            if len(parts) == 2 and parts[0].isdigit():
                rows.append((parts[0], parts[1].strip()))
        return rows

    def locked(self):
        """True / False from `devicectl device info lockState` (passcodeRequired), None if it cannot be read.
        Nothing else in a run reports a lock: the phone read locked just before a launch that returned success and a
        capture that went through, and whether that capture was taken behind the lock screen could not be told."""
        _, out, _ = sh(["xcrun", "devicectl", "device", "info", "lockState", "--device", self.core], 60)
        m = re.search(r"passcodeRequired:\s*(true|false)", out)
        return None if not m else m.group(1) == "true"

    @staticmethod
    def pids_in(rows, app_name):
        """Pids of the app's main executable: directly inside `<Name>.app/` (its name can differ from the .app stem);
        extensions sit deeper, under PlugIns/."""
        main = re.compile(rf"/{re.escape(app_name)}/[^/]+$")
        return [pid for pid, path in rows if main.search(path)]

    def app_name_for(self, bundle):
        """`<Name>.app` of `bundle` as installed on the phone, or None (not installed, or devicectl failed)."""
        tmp = "/tmp/ios_device_apps.json"
        if os.path.exists(tmp):
            os.remove(tmp)
        sh(["xcrun", "devicectl", "device", "info", "apps", "--device", self.core, "--bundle-id", bundle,
            "--json-output", tmp], 60)
        try:
            return app_name_from_apps_json(json.load(open(tmp)), bundle)
        except (OSError, ValueError):
            return None

    def app_pids(self, app_name):
        return self.pids_in(self.processes(), app_name)

    def signal_pid(self, pid, sig="SIGTERM"):
        return sh(["xcrun", "devicectl", "device", "process", "signal", "--device", self.core, "--pid", pid, "--signal", sig])

    def launch_app(self, bundle, cold=False):
        self.clear_behind(bundle)
        return sh(["xcrun", "devicectl", "device", "process", "launch", "--device", self.core]
                  + (["--terminate-existing"] if cold else []) + [bundle])

    def background_app(self, bundle, other="com.apple.Preferences", pid=None):
        """Bring another app to the front and mark `bundle` (process `pid`) as behind, so keepawake may tap."""
        r = sh(["xcrun", "devicectl", "device", "process", "launch", "--device", self.core, other])
        if r[0] == 0:
            self.mark_behind(bundle, pid)
        return r

    # "behind" markers: written only by someone who knows the app is not in front (background_app, `keepawake.py
    # behind`); dropped on our launch, on a new pid, or when WDA reports the app in front
    @staticmethod
    def mark_behind(bundle, pid=None):
        os.makedirs(STATE, exist_ok=True)
        json.dump({"time": time.time(), "pid": pid}, open(_marker(bundle), "w"))

    @staticmethod
    def clear_behind(bundle):
        if os.path.exists(_marker(bundle)):
            os.remove(_marker(bundle))

    @staticmethod
    def behind(bundle, pid):
        """True if `bundle` is marked behind, and the mark was for this process (or for no particular one)."""
        try:
            mark = json.load(open(_marker(bundle)))
        except (OSError, ValueError):
            return False
        return mark.get("pid") in (None, pid)

    # ---- WebDriverAgent (XCUITest): coordinates are POINTS, not pixels ----
    @property
    def wda(self):
        """This phone's WDA client (see wda.py). One forwarder per phone, so the URL can be set per
        UDID in ios_device.json; the session is cached per UDID so skills share it."""
        if self._wda is None:
            c = self.base_cfg["wda"]
            self._wda = wda.WDA(c.get("url_by_udid", {}).get(self.udid) or c["url"],
                                session_idle_s=c.get("session_idle_s", 50),
                                timeout_s=c.get("timeout_s", 30),
                                state_file=os.path.join(STATE, f"wda_session.{self.udid}.json"))
        return self._wda

    def session_warm(self):
        return self.wda.session_warm()

    def tap_points(self, x, y, timeout=120):
        """Tap (x, y) in points. (rc, output, seconds) like sh(): 0 on a tap WDA accepted."""
        return self.wda.tap(x, y, timeout=timeout)

    def front_app(self, timeout=30):
        """Bundle id of the app in front, or None when WDA cannot say.

        An XCUITest query against the front app. Cheap and accurate (verified: it follows launches
        within ~3 s), but it goes through the front app's accessibility, so treat it the way an
        XCUITest tap is treated - keepawake only asks when the SpringBoard idle-timer signal is
        missing. See ../../ios-build-device-lock/references/keepawake.md."""
        return self.wda.front_app(timeout=timeout)

    # ---- app processes ----
    def kill_until_gone(self, app_name, tries=4, wait_s=3):
        """SIGKILL every process of the app, then check again: a new one has been seen 3 s after a SIGKILL, before
        any launch. A launch after this returns [] is a cold start; a non-empty return means it is not."""
        pids = self.app_pids(app_name)
        for _ in range(tries):
            if not pids:
                return []
            for pid in pids:
                self.signal_pid(pid, "SIGKILL")
            time.sleep(wait_s)
            pids = self.app_pids(app_name)
        return pids

    def crash_logs(self, app_name):
        _, out, _ = sh(["xcrun", "devicectl", "device", "info", "files", "--device", self.core, "--domain-type", "systemCrashLogs"], 60)
        stem = app_name.rsplit(".", 1)[0]
        return sorted(set(w for w in out.split() if w.startswith(stem + "-") and w.endswith(".ips")))

    def details(self, bundle):
        """Device model, iOS version and the app's version / build, as devicectl reports them ("unknown" if not)."""
        _, info, _ = sh(["xcrun", "devicectl", "device", "info", "details", "--device", self.core], 60)
        _, apps, _ = sh(["xcrun", "devicectl", "device", "info", "apps", "--device", self.core, "--bundle-id", bundle], 60)
        get = lambda k: (re.search(rf"{k}:\s*(.+)", info) or [None, "unknown"])[1].strip()
        row = next((l.split() for l in apps.splitlines() if bundle in l.split()), [])
        i = row.index(bundle) if row else -1
        return {"device": get("marketingName"), "product_type": get("productType"), "ios": get("osVersionNumber"),
                "ios_build": get("osBuildUpdate"), "app_name": " ".join(row[:i]) if row else "unknown",
                "app_version": row[i + 1] if row and len(row) > i + 1 else "unknown",
                "app_build": row[i + 2] if row and len(row) > i + 2 else "unknown"}

    # ---- the automation left behind: the WDA runner on the phone ("Automation Running") and its forwarder ----
    def automation_processes(self):
        """{"device_wda_runner": pids on the phone, "mac_forwarder": pids of this phone's iproxy}."""
        t = self.base_cfg["teardown"]
        runner = [pid for pid, path in self.processes() if t["device_process_match"] in path]
        _, out, _ = sh(["pgrep", "-f", t["mac_process_pattern"]], 30)
        return {"device_wda_runner": runner, "mac_forwarder": [p for p in out.split() if p.isdigit()]}

    def teardown_automation(self):
        """Drop the WDA session, stop the XCUITest runner (the phone stops showing "Automation Running") and the
        local forwarder. Whoever needs WDA next brings it back up - see wda.py on getting the runner started."""
        self.wda.drop_session()
        procs = self.automation_processes()
        for pid in procs["device_wda_runner"]:
            self.signal_pid(pid, "SIGTERM")
        for pid in procs["mac_forwarder"]:
            try:
                os.kill(int(pid), signal.SIGTERM)
            except ProcessLookupError:
                pass
        return procs
