#!/usr/bin/env python3
"""WebDriverAgent over HTTP - the XCUITest half of the device layer.

WDA (Appium's WebDriverAgentRunner) is what turns "tap the point (4, 4)" into a real touch. It runs
*on the phone* as an XCUITest runner and serves HTTP on port 8100. Over USB you reach it through a
port forwarder from libimobiledevice (`brew install libimobiledevice`):

    iproxy 8100 8100 -u <hardware UDID>

Getting the runner *installed* on the phone is a one-off and outside this file: build Appium's
appium-webdriveragent with your own signing identity and leave it installed, or use whatever
prebuilt runner you already have. **Starting** it is `ensure_running()` - also
`python3 wda.py --ensure --udid <UDID>` - which runs the steps in `ios_device.json`'s
`wda.ensure` (by default: a go-ios tunnel, `ios runwda` to host the runner as an XCTest, and
`iproxy`) and then waits for `/status` to report `ready`. `WDA.probe()` - also
`python3 wda.py <url>` - says whether what is listening is really WDA and which of its routes
this version serves.

## What was verified, and what was not

Verified 2026-10-18 against a real runner: WebDriverAgent 8.9.2
(`com.facebook.WebDriverAgentRunner.xctrunner`), iPhone 14, iOS 26.6.1.

- **Tap**: `wda_tap` (`POST /session/<sid>/wda/tap` with `{"x", "y"}`) is accepted, ~0.9-1.1 s per
  tap. That runner answered on the first route, so `wda_tap_0` and `w3c_actions` were never
  exercised against hardware - they stay as fallbacks pinned only by the unit tests.
- **Front app**: `GET /session/<sid>/wda/activeAppInfo` returns `value.bundleId`, and it follows
  reality - after a `devicectl process launch` it reported the new app within ~3 s (checked with
  Preferences and MobileAddressBook).
- **Sessions**: `/status` reports the id of a session that is already open, so an existing one is
  reused rather than a second one created. A session id that has died is recreated transparently
  on the next call. With nothing listening, `tap()` returns rc 1 and the connection error instead
  of raising.

An `.xctrunner` app has to be *hosted as a test* for WDA to serve, and that has a verified trap:
`xcrun devicectl device process launch com.facebook.WebDriverAgentRunner.xctrunner` starts the
runner *process* - it shows up in the phone's process list - but its HTTP server never comes up
(HTTP 000 through a forwarder that was confirmed listening). The verification above was done
against a runner Appium had hosted.

### Starting the runner without Appium (`ensure_running`)

Verified 2026-10-18 on the same phone (iPhone 14, iOS 26.6.1, runner 8.9.2 already installed),
with **go-ios 1.3.2** installed by `npm install -g go-ios`. No Appium was involved: this Mac does
carry an idle Appium server process, but it was never contacted, and at the start of the run
nothing was listening on 8100, no `iproxy` was up and no `xcodebuild` was running.
`brew install go-ios` is not a thing: there is no such formula, and `brew tap
danielpaulus/go-ios` cannot be cloned. What was observed:

- **The tunnel is not optional here.** `ios runwda` with no tunnel fails outright: "cannot create
  a tunnel connection to testmanagerd ... To start the tunnel, run `ios tunnel start`". `ios kill`
  puts it as "this device runs iOS 26.6.1 and needs an active tunnel", and go-ios frames the
  requirement as iOS 17+ - but 26.6.1 is the only version this was seen on.
- **`ios tunnel start --userspace` needs no root.** It negotiated a userspace tunnel 1.1 s after
  launch as the ordinary user; the kernel tunnel (no `--userspace`) is the one that wants sudo, so
  the default steps use userspace. It keeps running in the foreground and must stay up.
- **Cold start is fast**: three consecutive cold runs (everything killed, runner gone from the
  phone's process list) reached `GET /status` -> `ready: true` **3.3 s, 3.4 s, 3.4 s** after the
  first command - tunnel up at ~1.6 s, runner `authorized` at ~2.2 s, `/status` ready ~1.1 s
  later. That is with the runner already installed and trusted and the phone unlocked; a first
  install, an untrusted certificate or a locked phone are all slower or impossible, and none of
  those was measured here.
- **`ios runwda` fails sometimes and a plain retry fixes it**: 1 of 4 host starts died at once
  with "lost connection to testmanagerd. the test-runner may have been killed", the next attempt
  worked. Hence `attempts: 2` on that step. 1 in 4 is one observation, not a rate.
- **A runner process on the phone is not proof that WDA serves.** Killing the Mac-side `ios
  runwda` left `WebDriverAgentRunner-Runner` in the phone's process list, and through a forwarder
  that was up the port then answered "connection reset by peer" rather than HTTP; the process
  exited by itself a few seconds later. So `ensure_running()` decides on `/status`, never on the
  process list.
- **Teardown still works unchanged**: SIGTERM to the runner on the phone (what
  `teardown_automation()` does) made `ios runwda` exit by itself within 6 s. The tunnel agent
  survives, on purpose.
- **go-ios writes a `selfIdentity.plist` into its working directory** - a binary plist holding the
  pairing private key. It turned up in this repo's root from a hand-run `ios list`, which is why
  every step is started with its cwd set to the ensure log dir and why the name is in `.gitignore`.
- **`ensure_running()` itself**, against the same phone: 3.0 s and 2.9 s from fully cold (no
  tunnel, no host, no forwarder, nothing in the phone's process list, HTTP 000 on 8100), "tunnel
  started, runner started, forwarder started"; 1.6 s with only the host missing, which it reported
  as "tunnel already up, runner started, forwarder already up"; and 0.0 s with one /status call
  and nothing started when WDA was already answering. Through keepawake's `start_wda()` the whole
  cold path - start, then tap - was 3.5 s. With go-ios off PATH the same cold tap returned
  "tunnel: ios is not installed - npm install -g go-ios" in 0.0 s and the loop carried on.
- Through this go-ios-hosted runner, `wda.py <url>` then took the `wda_tap` route and
  `session_active_app`, and reported `com.apple.springboard` in front - the same routes the
  Appium-hosted runner took, in 0.9 s total.

Not verified, and so not claimed: the `xcodebuild test-without-building` fallback (never run -
go-ios worked, so no xctestrun was ever built), any phone other than this one, any iOS below
26.6.1, a runner that is not already installed, and whether a second phone's steps interleave
cleanly (`match` is templated with the UDID for that reason, but only one phone was plugged in).

Unproven, and deliberately so: whether `activeAppInfo` is safe while a video app is in front. It
is an XCUITest query against the front app, and an XCUITest *tap* in that situation has killed an
app by watchdog (../../ios-build-device-lock/references/keepawake.md). keepawake therefore only
asks when the SpringBoard idle-timer signal is missing, which is the rare path.
"""
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request

STATE = os.path.expanduser("~/.cache/ios-device")

# WDA's tap route moved between versions. Tried in this order; `actions` is the W3C one, which is
# what current Appium speaks. The first route that answers is remembered, so the fallback costs one
# extra round trip once per session rather than once per tap.
TAP_ROUTES = ("wda_tap", "wda_tap_0", "w3c_actions")
FRONT_ROUTES = ("session_active_app", "bare_active_app")


class WDAError(Exception):
    """WDA is not reachable, or what answered is not WDA."""


def _ok(status, payload):
    """True when WDA accepted the call. W3C answers 200 with {"value": null}; a failure puts an
    `error` key in `value`. Older JSONWP builds answer 200 with a non-zero top-level `status`."""
    if status != 200:
        return False
    value = payload.get("value")
    if isinstance(value, dict) and value.get("error"):
        return False
    return payload.get("status", 0) in (0, None)


def _stale_session(status, payload):
    """True when the failure means "that session id is gone" - the one error worth retrying once."""
    value = payload.get("value")
    msg = " ".join(str(value.get(k, "")) for k in ("error", "message")) if isinstance(value, dict) else str(value)
    msg = msg.lower()
    return status == 404 or "invalid session id" in msg or "session does not exist" in msg


def _fill(text, subs):
    """`{udid}` / `{port}` in a configured command or match pattern. Literal substitution, so a
    pattern may put regex around a placeholder but not inside one."""
    for k, v in subs.items():
        text = text.replace("{" + k + "}", str(v))
    return text


def _pids_matching(pattern):
    """pgrep -f: pids whose whole command line matches `pattern`, never this process."""
    try:
        out = subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True,
                             timeout=15).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    return [int(p) for p in out.split() if p.isdigit() and int(p) != os.getpid()]


def _listening(port, host="127.0.0.1", timeout=1.0):
    """True when something accepts TCP connections on host:port."""
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except (OSError, ValueError):
        return False


def _why(log_path, limit=160):
    """The most useful line of a step's own output: go-ios writes JSON lines, so its last `error`
    beats the last line. Empty when the file says nothing."""
    try:
        text = open(log_path, errors="replace").read()
    except OSError:
        return ""
    errors = re.findall(r'"(?:error|err)"\s*:\s*"((?:[^"\\]|\\.)*)"', text)
    if errors:
        return errors[-1][:limit]
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    return lines[-1][:limit] if lines else ""


def _tap_request(route, sid, x, y):
    """(path, body) for one of the known tap routes."""
    if route == "wda_tap":
        return f"/session/{sid}/wda/tap", {"x": x, "y": y}
    if route == "wda_tap_0":
        return f"/session/{sid}/wda/tap/0", {"x": x, "y": y}
    return f"/session/{sid}/actions", {"actions": [{
        "type": "pointer", "id": "finger1", "parameters": {"pointerType": "touch"},
        "actions": [{"type": "pointerMove", "duration": 0, "x": x, "y": y},
                    {"type": "pointerDown", "button": 0},
                    {"type": "pause", "duration": 50},
                    {"type": "pointerUp", "button": 0}]}]}


class WDA:
    """One phone's WDA. State - the session id and which routes work - lives in a file under
    ~/.cache/ios-device so every skill in this plugin reuses the same session instead of paying for
    a new one, the same reason the behind-markers live there."""

    def __init__(self, url="http://127.0.0.1:8100", session_idle_s=50, timeout_s=30, state_file=None,
                 ensure=None):
        self.url = url.rstrip("/")
        self.session_idle_s = session_idle_s
        self.timeout_s = timeout_s
        self.state_file = state_file or os.path.join(STATE, "wda_session.json")
        self.ensure_cfg = ensure or {}       # ios_device.json's wda.ensure; no steps = ensure_running() refuses

    # ---- the little bit of state that is shared between skills ----
    def _load(self):
        try:
            with open(self.state_file) as f:
                state = json.load(f)
        except (OSError, ValueError):
            return {}
        return state if state.get("url") == self.url else {}   # a different phone or port: start clean

    def _save(self, **kw):
        state = self._load()
        state.update(url=self.url, **kw)
        try:
            os.makedirs(os.path.dirname(self.state_file), exist_ok=True)
            with open(self.state_file, "w") as f:
                json.dump(state, f)
        except OSError:
            pass
        return state

    def session_warm(self):
        """True when a session was used recently enough that WDA has not torn it down."""
        state = self._load()
        return bool(state.get("session_id")) and time.time() - state.get("last_use", 0) < self.session_idle_s

    # ---- HTTP ----
    def _req(self, method, path, body=None, timeout=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.url + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout_s) as r:
                raw, status = r.read().decode(errors="replace"), r.status
        except urllib.error.HTTPError as e:                      # an error WDA described in its body
            raw, status = e.read().decode(errors="replace"), e.code
        except (urllib.error.URLError, OSError) as e:             # nothing listening, or it hung up
            raise WDAError(f"{method} {path}: {e}") from e
        try:
            return status, (json.loads(raw) if raw.strip() else {})
        except ValueError:
            return status, {"value": {"error": "not json", "message": raw[:200]}}

    def status(self, timeout=None):
        """WDA's /status payload. Raises WDAError when nothing usable is listening."""
        status, payload = self._req("GET", "/status", timeout=timeout)
        if not _ok(status, payload):
            raise WDAError(f"/status answered {status}: {str(payload)[:200]}")
        return payload

    def ready(self, timeout=None):
        """True when /status says WDA is ready to take commands. The only honest test that WDA is
        up: a runner process on the phone whose XCTest host has gone answers nothing (see the
        header), so never decide this from a process list."""
        try:
            value = self.status(timeout=timeout).get("value")
        except WDAError:
            return False
        return bool(isinstance(value, dict) and value.get("ready"))

    def session(self, timeout=None):
        """A usable session id, reusing the cached one. Raises WDAError if one cannot be created."""
        cached = self._load().get("session_id")
        if cached:
            return cached
        # /status reports an already-running session on most builds - cheaper than making another.
        try:
            existing = (self.status(timeout=timeout) or {}).get("sessionId")
        except WDAError:
            existing = None
        if existing:
            self._save(session_id=existing, last_use=time.time())
            return existing
        status, payload = self._req("POST", "/session",
                                    {"capabilities": {"alwaysMatch": {}, "firstMatch": [{}]}}, timeout)
        value = payload.get("value") if isinstance(payload.get("value"), dict) else {}
        sid = value.get("sessionId") or payload.get("sessionId")
        if not _ok(status, payload) or not sid:
            raise WDAError(f"POST /session answered {status}: {str(payload)[:200]}")
        self._save(session_id=sid, last_use=time.time())
        return sid

    def drop_session(self):
        """Delete the session. The phone stops showing "Automation Running" once the runner exits."""
        sid = self._load().get("session_id")
        if sid:
            try:
                self._req("DELETE", f"/session/{sid}")
            except WDAError:
                pass
        self._save(session_id=None, last_use=0)

    def _call(self, make, timeout=None):
        """Run `make(sid)` -> (method, path, body), recreating the session once if it turns out stale."""
        for attempt in (0, 1):
            sid = self.session(timeout=timeout)
            method, path, body = make(sid)
            status, payload = self._req(method, path, body, timeout)
            if _ok(status, payload):
                self._save(last_use=time.time())
                return True, payload
            if attempt == 0 and _stale_session(status, payload):
                self._save(session_id=None, last_use=0)
                continue
            return False, payload
        return False, {}

    # ---- bringing the runner up ----
    def _local_port(self):
        return urllib.parse.urlsplit(self.url).port or 8100

    def _run_step(self, step, subs, log_dir, deadline):
        """Start one long-lived step and wait for it to say it is up. (ok, why) - `why` names the
        step and quotes its own output, because that is the line a caller logs."""
        name = step.get("name", "step")
        cmd = [_fill(str(a), subs) for a in step.get("cmd") or []]
        if not cmd:
            return False, f"{name}: no cmd in wda.ensure.steps"
        if not shutil.which(cmd[0]):
            hint = step.get("hint")
            return False, f"{name}: {cmd[0]} is not installed" + (f" - {hint}" if hint else "")
        log_path = os.path.join(log_dir, f"{name}.log")
        marker, settle = step.get("ready_log"), step.get("settle_s", 1)
        step_deadline = min(deadline, time.time() + step.get("timeout_s",
                            self.ensure_cfg.get("step_timeout_s", 40)))
        try:
            with open(log_path, "w") as log:                  # the child keeps its own handle
                proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT,
                                        stdin=subprocess.DEVNULL, cwd=log_dir,
                                        start_new_session=True)   # outlives us; it is the host
        except OSError as e:
            return False, f"{name}: {cmd[0]} would not start: {e}"
        t0 = time.time()
        try:
            while time.time() < step_deadline:
                if marker:
                    try:
                        if marker in open(log_path, errors="replace").read():
                            return True, ""
                    except OSError:
                        pass
                elif time.time() - t0 >= settle:
                    return True, ""                            # nothing to watch for: /status decides
                if proc.poll() is not None:                    # it gave up - go-ios exits on failure
                    return False, f"{name} exited rc {proc.returncode}: {_why(log_path)} ({log_path})"
                time.sleep(0.25)
        except BaseException:
            self._stop(proc)
            raise
        self._stop(proc)
        return False, (f"{name} did not report ready in {time.time() - t0:.0f} s: "
                       f"{_why(log_path)} ({log_path})")

    @staticmethod
    def _stop(proc):
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)    # it has its own session
        except (OSError, ProcessLookupError):
            pass

    def ensure_running(self, udid=None, timeout_s=None):
        """Bring WebDriverAgent up at `self.url` and return (rc, output, seconds) like tap(): rc 0
        once /status reports ready, rc 1 with one line saying what stopped it.

        The steps come from `ios_device.json`'s `wda.ensure` - by default a go-ios tunnel, `ios
        runwda` to host the already-installed runner as an XCTest, and `iproxy`. A step whose
        `match` pattern already has a process is skipped, not restarted: it may be another
        session's, and this plugin never kills a phone helper it did not start. Each step is left
        running on purpose - the XCTest host *is* WDA's lifetime, so it has to outlive this call.

        Not a thing to call on every tap: it starts processes, and on failure it is a 180 s budget
        spent. Call it when nothing is listening, with a backoff, and log the reason it gives -
        keepawake's `start_wda()` is the worked example."""
        t0 = time.time()
        cfg = self.ensure_cfg or {}
        if self.ready(timeout=min(5, self.timeout_s)):
            return 0, "", time.time() - t0
        steps = cfg.get("steps") or []
        if not steps:
            return 1, (f"nothing is listening at {self.url} and wda.ensure.steps is empty in "
                       "ios_device.json, so there is nothing to start"), time.time() - t0
        if not udid and "{udid}" in json.dumps(steps):
            return 1, (f"nothing is listening at {self.url} and the wda.ensure steps need a "
                       "hardware UDID, which was not given"), time.time() - t0
        subs = {"udid": udid or "", "port": self._local_port()}
        deadline = t0 + (timeout_s or cfg.get("timeout_s", 180))
        log_dir = os.path.expanduser(cfg.get("log_dir", os.path.join(STATE, "ensure")))
        try:
            os.makedirs(log_dir, exist_ok=True)
        except OSError as e:
            return 1, f"cannot write the ensure logs to {log_dir}: {e}", time.time() - t0
        done = []
        for step in steps:
            name = step.get("name", "step")
            match = _fill(str(step.get("match", "")), subs)
            if match and _pids_matching(match):
                done.append(f"{name} already up")
                continue
            # A process the pattern does not see can still hold the step's port - a go-ios tunnel
            # agent started under another command line (npm's node wrapper, another udid, a hand
            # run). Starting a second one only fails with "bind: address already in use"
            # (2026-10-19, on the Mac: three ensure runs in a row), so a listening port counts as up.
            port = _fill(str(step.get("listen_port", "")), subs)
            if port and _listening(port):
                done.append(f"{name} already up (port {port} in use)")
                continue
            attempts = max(1, int(step.get("attempts", 1)))
            for attempt in range(1, attempts + 1):
                if time.time() >= deadline:
                    return 1, f"out of time before {name}: {', '.join(done) or 'nothing started'}", time.time() - t0
                ok, why = self._run_step(step, subs, log_dir, deadline)
                if ok:
                    done.append(f"{name} started")
                    break
                if attempt >= attempts:
                    return 1, (why + (f" - after {attempts} attempts" if attempts > 1 else "")), time.time() - t0
        poll = cfg.get("poll_s", 1)
        while True:
            if self.ready(timeout=min(5, self.timeout_s)):
                return 0, ", ".join(done), time.time() - t0
            if time.time() + poll >= deadline:
                return 1, (f"{', '.join(done) or 'nothing to start'}, but {self.url} never reported "
                           f"ready in {time.time() - t0:.0f} s; logs in {log_dir}"), time.time() - t0
            time.sleep(poll)

    # ---- what the rest of the plugin actually calls ----
    def tap(self, x, y, timeout=None):
        """Tap the point (x, y). POINTS, not pixels. Returns (rc, output, seconds) so it reads like
        ios_device.sh(): rc 0 and empty output on success."""
        t0 = time.time()
        remembered = self._load().get("tap_route")
        routes = ([remembered] + [r for r in TAP_ROUTES if r != remembered]) if remembered else list(TAP_ROUTES)
        last = ""
        for route in routes:
            try:
                ok, payload = self._call(lambda sid, r=route: ("POST",) + _tap_request(r, sid, x, y), timeout)
            except WDAError as e:
                return 1, str(e), time.time() - t0            # unreachable: no other route will help
            if ok:
                if route != remembered:
                    self._save(tap_route=route)
                return 0, "", time.time() - t0
            last = f"{route}: {str(payload)[:160]}"
        return 1, f"no tap route accepted by {self.url} ({last})", time.time() - t0

    def front_app(self, timeout=None):
        """Bundle id of the app in front, or None when WDA cannot say.

        This is an XCUITest query against the front app - read the verification note at the top of
        this file before calling it on a phone whose front app may be a video player."""
        remembered = self._load().get("front_route")
        routes = ([remembered] + [r for r in FRONT_ROUTES if r != remembered]) if remembered else list(FRONT_ROUTES)
        for route in routes:
            try:
                if route == "bare_active_app":
                    status, payload = self._req("GET", "/wda/activeAppInfo", timeout=timeout)
                    ok = _ok(status, payload)
                else:
                    ok, payload = self._call(lambda sid: ("GET", f"/session/{sid}/wda/activeAppInfo", None), timeout)
            except WDAError:
                return None
            value = payload.get("value") if isinstance(payload.get("value"), dict) else {}
            if ok and value.get("bundleId"):
                if route != remembered:
                    self._save(front_route=route)
                return value["bundleId"]
        return None

    def probe(self, timeout=None):
        """What this runner is and which routes it serves - for `python3 wda.py <url>`. The tap it
        tries is the top-left corner, which is the status bar on every iPhone, so it does nothing."""
        out = {"url": self.url, "reachable": False, "status": None, "session": None,
               "tap_route": None, "front_route": None, "front_app": None, "error": None}
        try:
            out["status"] = self.status(timeout=timeout)
            out["reachable"] = True
            out["session"] = self.session(timeout=timeout)
        except WDAError as e:
            out["error"] = str(e)
            return out
        rc, err, _ = self.tap(4, 4, timeout=timeout)
        out["tap_route"] = self._load().get("tap_route") if rc == 0 else None
        if rc:
            out["error"] = err
        out["front_app"] = self.front_app(timeout=timeout)
        out["front_route"] = self._load().get("front_route")
        return out


def config(path=None):
    """`wda` from ios_device.json next to this scripts/ dir - the CLI's config, so the class itself
    stays injected-only. {} when the file is not there or not readable."""
    here = os.path.dirname(os.path.abspath(__file__))
    try:
        with open(path or os.path.join(here, "..", "ios_device.json")) as f:
            return json.load(f).get("wda", {})
    except (OSError, ValueError):
        return {}


def main(argv):
    import argparse
    p = argparse.ArgumentParser(description="Probe WebDriverAgent, or bring it up (--ensure).")
    p.add_argument("url", nargs="?", help="default: ios_device.json's wda.url (for --udid, its url_by_udid)")
    p.add_argument("--ensure", action="store_true", help="start the runner and wait for /status to report ready")
    p.add_argument("--udid", help="hardware UDID; the wda.ensure steps need it")
    p.add_argument("--config", help="an ios_device.json other than this plugin's")
    a = p.parse_args(argv[1:])
    cfg = config(a.config)
    url = a.url or cfg.get("url_by_udid", {}).get(a.udid) or cfg.get("url", "http://127.0.0.1:8100")
    client = WDA(url, timeout_s=cfg.get("timeout_s", 30), ensure=cfg.get("ensure"))
    if a.ensure:
        rc, out, secs = client.ensure_running(udid=a.udid)
        print(json.dumps({"url": url, "ready": rc == 0, "seconds": round(secs, 1),
                          "detail": out}, indent=2))
        return rc
    print(json.dumps(client.probe(), indent=2, default=str))
    return 0


if __name__ == "__main__":
    import sys
    raise SystemExit(main(sys.argv))
