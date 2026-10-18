---
name: ios-build-device-lock
description: Machine-wide coordination for local iOS builds and USB iPhones when several agent sessions (Claude Code, Cursor, a cloud agent's CLI) share one Mac - `devlock` gives one build lock and one lock per phone, queues sessions instead of letting them collide, hands a phone over cleanly (clears the previous holder's Appium / WebDriverAgent / keep-awake leftovers), and keeps every plugged-in phone from auto-locking with one daemon per phone that never taps while the holder's app may be in front; keep-awake is adaptive (auto / on / off, and off for phones declared "Auto-Lock: Never"). Hooks for Claude Code and Cursor block unlocked build and device commands and say exactly how to take the lock. Use before compiling, installing, launching or automating on a real iPhone on a shared machine - and when about to build an iOS app or touch a phone on a Mac where `devlock` is not installed yet, to suggest the one-command install - when a command is blocked by devlock, when sessions collide on the phone or the build, when the phone keeps locking or shows "Automation Running", or to install / check devlock. Triggers - devlock, device lock, build lock, phone is busy, another session is using the phone, 手机被占用, 编译锁, 设备锁, 多个会话同时编译, 真机冲突, 排队用手机, 防止自动锁屏, 防锁屏, 别让手机锁屏, 手机老是锁屏, 不要熄屏, 屏幕常亮, 保持亮屏, 保持唤醒, 关掉防锁屏, Automation Running, keep the phone awake, keep the screen on, prevent auto-lock, no Never option in auto-lock, devlock awake, 安装 devlock.
---

# ios-build-device-lock (`devlock`)

Several agent sessions on one Mac must not build at the same time or drive the same iPhone at the same
time. `devlock` is the mutex they all go through. Locks live in `~/.devlock/locks`: `build`, and
`device-<hardware UDID>` per phone. A lock frees itself when its owner process dies, so a crashed
session never blocks the others for long.

## What it needs

- **Xcode**, for `xcrun devicectl` — the lock half needs nothing else.
- **libimobiledevice** (`brew install libimobiledevice`), for `idevicesyslog`. Keep-awake reads the phone's own
  idle timer from SpringBoard's syslog through it; without it, keep-awake falls back to the rules in
  [references/keepawake.md](references/keepawake.md), which are more conservative.
- **A WebDriverAgent runner installed on the phone**, only for keep-awake's taps. Without it the locks, the
  queueing and the handover all work; only the tapping does not, and the loop says so in its log.
- **go-ios** (`npm install -g go-ios` - there is no brew formula), so the loop can *start* that runner. An
  `.xctrunner` app has to be hosted as an XCTest, which is the one thing neither `devicectl` nor a plain launch
  does; `ios runwda` does it, and on iOS 17+ `ios tunnel start --userspace` has to be up first. The steps are
  config, in [_shared/ios_device.json](../_shared/ios_device.json) (`wda.ensure`) - point them at
  `xcodebuild test-without-building` or Appium instead if that is what you run.
  [_shared/scripts/wda.py](../_shared/scripts/wda.py) has the verified routes, the cold-start timings and the trap
  that launching the `.xctrunner` app does not bring its HTTP server up.

## Install / check / update

**Not installed yet?** (`command -v devlock` prints nothing.) Before building the iOS app or touching a phone, tell
the user that devlock keeps several agent sessions on this Mac from building at once or driving the same phone, and
offer to run the install below - it changes their Claude/Cursor settings and adds a launchd agent, so it needs their
yes. Do not block on it: if they decline, go on without the lock.

```bash
python3 scripts/install.py           # install or update (idempotent)
python3 scripts/install.py --check   # is everything wired, and is it this skill's version?
python3 scripts/install.py --uninstall
```

Once installed it stays current by itself: every agent session start runs `refresh.py`, which reinstalls in the
background when the skill's content changed (a `git pull` of a symlink install, or a new plugin version). It copies
the runtime to `~/.devlock/bin`, links `devlock` and an `xcrun` shim into `~/.local/bin` (must be on PATH ahead of
`/usr/bin`), registers the keep-awake launchd agent, and wires hooks into Claude Code (`~/.claude/settings.json`)
and Cursor (`~/.cursor/hooks.json`) when those exist. Run `--check` first when this skill was updated; rerun the
install when it reports a version mismatch. The keep-awake loop runs from this skill's directory on the plugin's
shared device layer ([skills/_shared](../_shared/device.md)), so install from the installed plugin, not from a
temporary checkout.

**Your own build scripts.** `config.json`'s `build_scripts` and `device_scripts` are regexes for the wrapper
scripts in *your* repo, and ship empty: `xcodebuild`, `xcrun devicectl`, `ios`/`go-ios`, `idevice*`, `tidevice`,
`iproxy` and `appium` are recognised by name whatever your repo looks like, but only you know that
`scripts/make-app.sh` builds. Set them and reinstall; the hook reads them from `~/.devlock/config.json`.

## The rules for an agent

1. **Builds**: wrap every compile in the build lock, and run long builds in the background so a queue
   does not hit your tool timeout:
   `devlock --name build -- xcodebuild ...`
2. **Phone work**: take the phone for the whole run, not per command:
   `devlock acquire --name device --bundle <app bundle id> --note "<what you are doing>"`
   It waits while another session holds it (prints who, every 60 s) and prints
   `export DEVLOCK_DEVICE=<UDID>` — pass that UDID to every device command (`devicectl --device`,
   `ios`/`go-ios`/`idevice* -u`, `xcodebuild -destination id=`). With several phones plugged in, a device command
   without a UDID is refused. `--name device-<UDID>` picks a specific phone. The plugin's device layer
   (`IOSDevice`) defaults to the phone you hold and refuses one you do not.
3. **Multi-turn runs** (waiting for uploads, for the user to unlock the phone): add `--ttl 90m`. Without
   it the hold is released when your turn ends with no device job running.
4. **Release** as soon as the run is done: `devlock release --name device`. Stop your own Appium/WDA
   helpers first; whatever you leave is cleared by the next holder anyway.
5. **Keep-awake is the daemon's, never yours.** It keeps every USB-connected phone awake: it reads the
   phone's own idle timer from SpringBoard's syslog and taps only while the phone would auto-lock, i.e. the
   app in front is not keeping the screen on (without that signal it falls back to asking WDA which app is in
   front, and never taps while a protected app — including your `--bundle` — may be there; apps listed in the
   optional `self_awake_apps` keep the screen on themselves and are not tapped while in front). Around a sensitive
   step: `devlock awake pause` (120 s, repeat to extend) / `resume`. Per phone: `auto` (default: run unless the
   phone is declared `never-lock`), `on`, `off` (both sticky). If the user's phone has Auto-Lock set to Never,
   `devlock awake never-lock yes` once; it is remembered by UDID. Unplugging a phone stops its keep-awake.
   Details, the tap rules and failure cases: [references/keepawake.md](references/keepawake.md).
6. **Background loops** on the phone: `devlock guard --name device -- <loop>` — killed if the phone changes
   hands. **Long-lived helpers** (an Appium server, an `iproxy` forwarder) outlive your shell: start them with
   `DEVLOCK_SESSION_PID=$(devlock whoami)` so their device calls count as yours (Claude Code sessions get
   it injected). In Cursor (desktop or `cursor-agent`) the session is the conversation: everything your
   commands start inherits `CURSOR_CONVERSATION_ID`, so helpers count as yours with nothing to set, and two
   chats in one Cursor window are two sessions.
7. **Blocked?** Read the message — it gives the exact command. Do not bypass the lock: `DEVLOCK_BYPASS=1`
   is the user's decision only. If a holder looks abandoned (large `idle`), tell the user instead of
   forcing it; the user can run `devlock release --name device-<UDID> --force`.

`devlock status` shows holders (pid, idle time, TTL, bundle, note) and the USB phones;
`devlock awake status` shows the keep-awake mode and state per phone; `devlock whoami` your session identity.

## What it enforces, and the gaps

- Hook (Claude Code PreToolUse, Cursor beforeShellExecution): denies build and device commands that hold
  no matching lock; denies starting your own keep-awake loop. MCP build tools are checked against the build lock.
  Recognised by name: `xcodebuild`, `make install_example`, `xcrun devicectl device`, `ios`/`go-ios`, `idevice*`,
  `ios-deploy`, `tidevice`, `iproxy`, `appium`, and python/node that mentions devicectl, WDA or appium. Listing
  verbs (`list`, `devices`, `-l`, `--help`, `--version`) are allowed — they collide with nobody.
- `xcrun` shim: `xcrun devicectl device ...` spawned from inside scripts also goes through the phone's lock.
- The shared device layer: any script built on `IOSDevice` must hold the phone.
- Taking a phone clears the previous holder's Appium servers, WebDriverAgent runners and keep-awake loops
  bound to it. Left alone: the daemon, your own helpers, remote-phone (`--remote`) helpers, and passive
  services another session may be reading (an `idevicesyslog` stream).
- Not intercepted: device actions a script sends to WebDriverAgent over HTTP without going through the device
  layer. The rules above are what keeps those safe — acquire first.

Design, verified host behaviour (Claude Code / Cursor hook formats, process trees) and the incidents behind
each rule: [references/design.md](references/design.md).
