#!/usr/bin/env python3
"""WebDriverAgent over HTTP - the XCUITest half of the device layer.

WDA (Appium's WebDriverAgentRunner) is what turns "tap the point (4, 4)" into a real touch. It runs
*on the phone* as an XCUITest runner and serves HTTP on port 8100. Over USB you reach it through a
port forwarder from libimobiledevice (`brew install libimobiledevice`):

    iproxy 8100 8100 -u <hardware UDID>

Getting the runner onto the phone is a one-off and outside this file: build Appium's
appium-webdriveragent with your own signing identity and leave it installed, or use whatever
prebuilt runner you already have. `WDA.probe()` - also `python3 wda.py <url>` - says whether what
is listening is really WDA and which of its routes this version serves.

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

**The one thing this file does not do is start the runner**, and that part has a verified trap:
`xcrun devicectl device process launch com.facebook.WebDriverAgentRunner.xctrunner` starts the
runner *process* - it shows up in the phone's process list - but its HTTP server never comes up
(HTTP 000 through a forwarder that was confirmed listening). An `.xctrunner` app has to be run as
a *test* for WDA to serve. So something must host it: Appium's XCUITest driver, `go-ios ios runwda`,
or `xcodebuild test-without-building` with WDA's own xctestrun. The verification above was done
against a runner hosted by Appium.

Unproven, and deliberately so: whether `activeAppInfo` is safe while a video app is in front. It
is an XCUITest query against the front app, and an XCUITest *tap* in that situation has killed an
app by watchdog (../../ios-build-device-lock/references/keepawake.md). keepawake therefore only
asks when the SpringBoard idle-timer signal is missing, which is the rare path.
"""
import json
import os
import time
import urllib.error
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

    def __init__(self, url="http://127.0.0.1:8100", session_idle_s=50, timeout_s=30, state_file=None):
        self.url = url.rstrip("/")
        self.session_idle_s = session_idle_s
        self.timeout_s = timeout_s
        self.state_file = state_file or os.path.join(STATE, "wda_session.json")

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


def main(argv):
    url = argv[1] if len(argv) > 1 else "http://127.0.0.1:8100"
    print(json.dumps(WDA(url).probe(), indent=2, default=str))
    return 0


if __name__ == "__main__":
    import sys
    raise SystemExit(main(sys.argv))
