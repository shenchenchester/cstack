# Shared iOS device layer

`scripts/ios_device.py` and `scripts/wda.py` are this plugin's one implementation of "talk to a USB iPhone".
Every skill here that drives a phone imports them instead of carrying its own copy, so they all share the same
state: the WebDriverAgent session per phone and the "app is behind" markers, both under `~/.cache/ios-device/`.

```python
HERE = os.path.dirname(os.path.abspath(__file__))
for base in (HERE, os.path.realpath(HERE)):          # symlink installs resolve to the real plugin dir
    d = os.path.join(base, "..", "..", "_shared", "scripts")
    if os.path.exists(os.path.join(d, "ios_device.py")):
        sys.path.insert(0, d)
        break
import ios_device

dev = ios_device.IOSDevice()       # devicectl for lifecycle, WDA for taps; shared session state
dev.tap_points(4, 4)               # POINTS, not pixels
dev.front_app()                    # bundle id of the app in front, or None
dev.pause_keepawake()              # around a run that must not be tapped (expires after 120 s); resume_keepawake()
dev.background_app(bundle)         # marks `bundle` as behind, so keep-awake may tap
```

## Two halves, and why they are different

**devicectl** (Xcode's own) does processes and app lifecycle. It never runs an XCUITest query, so it never touches
the app's main thread: safe on any page, on any build. This half is verified on hardware.

**WebDriverAgent** does the things only XCUITest can: tap a coordinate, and say which app is in front. It goes
through the front app's accessibility, so it is the half that can disturb what is running - an XCUITest tap into a
video feed has killed an app by watchdog. Prefer the devicectl half whenever it can answer the question.

WDA is not something you can just call: the runner has to be hosted as an XCTest, and reached through a forwarder
(`iproxy 8100 8100 -u <UDID>`). `scripts/wda.py`'s header has the routes, what was verified against a real runner,
and the trap that launching the `.xctrunner` app does *not* bring its HTTP server up.

## What `IOSDevice` holds

- devicectl, app-agnostic: `processes`, `app_pids`, `pids_in`, `app_name_for` (the `<Name>.app` of a bundle, read
  from the phone), `launch_app`, `background_app`, `kill_until_gone`, `crash_logs`, `locked`, `details`.
- WDA (XCUITest, coordinates in points): `tap_points`, `front_app`, `session_warm()`, and `wda` for the client
  itself. One forwarder per phone, so the URL can be set per UDID in `ios_device.json`, and the session file is
  keyed by UDID - two phones must not hand each other a session id.
- Automation left behind: `automation_processes`, `teardown_automation` (drops the session, stops the runner -
  the phone stops showing "Automation Running" - and the local forwarder).

App-specific policy does not live here. Which dialogs an app shows, which permission alerts to grant, and whether
a particular app must never be tapped while in front are the calling skill's business; for keep-awake that is
`config.protected_apps` in `ios-build-device-lock`.

## Phone coordination

On a Mac with devlock (`ios-build-device-lock`), `IOSDevice()` with no udid uses the phone this session holds
(`$DEVLOCK_DEVICE`), and exits unless the caller holds that phone - take it with
`devlock acquire --name device --bundle <app>`, or wrap a one-off command in `devlock --name device-<UDID> --`.
Without devlock, nothing changes. `IOSDevice(udid, need_lock=False)` is for devlock's own keep-awake daemon only.

## Config and tests

`ios_device.json`: the WDA URL (and a per-UDID override), the session idle window, and the process patterns the
automation teardown matches. Tests need no phone - `test_wda.py` runs a fake WDA on localhost:

```bash
cd plugins/cstack-ios/skills/_shared && python3 -m unittest discover -s scripts/tests
```
