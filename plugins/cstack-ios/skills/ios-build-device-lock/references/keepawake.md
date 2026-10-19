# Keep-awake

Managed test phones often have no "Never" under Settings > Display & Brightness > Auto-Lock, so a phone left alone
locks within minutes and automation stops. devlock's daemon keeps every USB-connected phone awake from the Mac: a loop
(`scripts/keepawake.py run`) taps the status-bar corner (4, 4 pt) through WebDriverAgent every 60 s. The
iPhone 14 test phone locked within 3 min of a status-bar tap and stayed awake with a tap every 60 s.

## Modes (per phone)

| Command | Effect |
|---|---|
| `devlock awake auto` (default) | run, unless the phone is declared to never auto-lock |
| `devlock awake never-lock yes` / `no` | declare that this phone's Auto-Lock is Never (remembered by UDID); in auto mode keep-awake then stays off |
| `devlock awake on` | always run, even if declared never-lock (sticky until `auto` / `off`) |
| `devlock awake off` | never run (sticky until `auto` / `on`); on a free phone it also clears "Automation Running" |
| `devlock awake pause` / `resume` | hold off taps around a sensitive step; a pause expires after 120 s, repeat to extend |
| `devlock awake behind [--bundle B]` | mark a running protected app as not in front, after checking a screenshot |
| `devlock awake status` | mode, running or not, pause, holder, last loop decision |

Only the phone's holder (or anyone, for a free phone) may change its mode; the user can pass `--force`.
Unplugging a phone stops its loop within 30 s; plugging it back in starts it again (verified 2026-10-14 on the iPhone 14:
unplug logged `stopped (unplugged)` with no loop process left, replug restarted the loop at the next poll).

The Auto-Lock setting **cannot be read from the Mac**, so "Never" is a declaration. Checked 2026-10-14 on the iPhone 14
test phone: snapshots of every lockdown domain `ideviceinfo` answers (36 queried, global included) plus
`devicectl device info details` and `lockState`, taken at Auto-Lock 5 min and again at 2 min, differ only in values
that also change with no setting change (timestamps, connection date, tunnel address, disk usage).

## What to tell the user

- While taps are needed the phone shows **"Automation Running"** - the XCUITest runner (WebDriverAgent) that does
  them. It goes away by itself: on a free phone that has needed no tap for `wda_idle_teardown_s` (2 min - which is
  what happens while a video app holds the screen on), the loop stops WebDriverAgent, and its local forwarder too
  when no other phone is plugged in; switching keep-awake off on a free phone does the same at once. The next tap
  starts them again - 3 s where the loop can start the runner itself (verified 2026-10-18 with go-ios), and up to
  ~2 min where it cannot and the tap has to wait for whatever brings WDA up, which is why that tap gets
  `tap_cold_timeout_s` (150 s; verified 2026-10-15: retired after 2 min on a video feed, taps back ~2 min after a
  switch to Settings). The retire looks at the phone, not at whether the loop retired a runner before, so a runner
  someone else started (a hand-run `wda.py --ensure`, a finished test) is stopped the same way; the phone is asked
  at most every `wda_retire_check_s` (120 s). A **held** phone's runner is stopped too, but only once nobody has used
  WDA through the device layer for `wda_idle_teardown_held_s` (10 min) and no Appium server or `xcodebuild test`
  runs on the Mac - those drive WDA on their own, so their runs are never cut. Set it to 0 to leave held phones alone.
- **Killing WebDriverAgentRunner by hand** (app switcher) is fine: the next tap that is needed starts it again
  (see "When the taps fail"). The "Automation Running" banner can stay a few seconds after the kill - iOS clears it
  once the test session has ended (seen 2026-10-19).
- **Unplugging** stops the loop, but WebDriverAgent keeps running on the phone, and a Mac without the cable cannot reach
  it - so "Automation Running" stays. The daemon tries to stop it over the network right after the unplug. With Xcode 15+ and
  iOS 17+ there is no "Connect via network" switch any more: a paired phone is reachable over the network by itself
  when it and the Mac are on the same network and that network lets devices discover and reach each other (Bonjour /
  mDNS). Office Wi-Fi often isolates clients - on 2026-10-15 the iPhone 14 was unreachable from the Mac (Xcode 26,
  iOS 26.6) as soon as the cable was out - so expect to close WebDriverAgentRunner in the phone's app switcher there.
  The daemon log says which happened.
- It does not change any phone setting.
- It cannot unlock a phone that is already locked with a passcode: ask the user to unlock it first.

## How it decides: the phone's own idle timer

The loop follows a filtered SpringBoard syslog stream (`idevicesyslog -u <UDID> -p SpringBoard -m "UPDATE BEHAVIOR:
SBMainWorkspace"`). SpringBoard logs every change of the effective idle timer: `mode: Auto` means the phone will
auto-lock, `mode: Disabled` means the app in front keeps the screen on. The loop taps while it is `Auto` and does
nothing while it is `Disabled` - no XCUITest query at all, no guessing which app is in front, the same on a free or
a held phone. **This is the main path, and the reason it is the main path is that it touches no app.**

Verified 2026-10-14 on the iPhone 14 by launching each app with `devicectl` (Settings as the baseline, `Auto` every time):

| App on the phone | Idle timer once in front |
|---|---|
| a video app, staff build A | `Disabled` after ~1 s |
| a video app, staff build B | `Disabled` after ~5 s |
| the same app, store build | `Disabled` after ~4 s |
| another build of it, older | stayed `Auto` for 20 s (did not keep the screen on where it landed) |
| Home screen | `Auto` (reason `SBHomeScreen`) |

The signal is SpringBoard's, so it does not depend on how the app was built (Debug, Inhouse, TestFlight or App Store);
whether an app keeps the screen on, and on which pages, is the app's own behaviour and can differ between builds -
as the last two rows show for two builds of one app.
SpringBoard logs the line at debug level; a phone or iOS version that does not would never produce it, which the loop
treats as "unknown". Cost: ~0.1 CPU-s a minute on the Mac for the filtered stream; an unfiltered stream is ~4 CPU-s a
minute.

**Fallback.** While the state is unknown the loop uses the front-app rules below: `idevicesyslog` not installed
(`brew install libimobiledevice`), the stream not running or reconnecting (its state is dropped then, as changes may
have been missed), or no idle-timer change seen since the stream started (right after the phone is plugged in or the Mac starts). A loop
that restarts within 2 min (a daemon refresh or reinstall) carries the last state over from `keepawake.idle_state`, so
those restarts do not drop to the fallback. `devlock awake status` shows which signal the loop is on
(`signal: syslog idle timer Auto ...` or `signal: fallback to the front-app rules: <why>`).

**Apps that keep the screen on themselves (`self_awake_apps`, optional).** The gap in the main path is a phone
plugged in - or a Mac started - with such an app already in front: SpringBoard logs nothing until something changes,
so the state stays unknown, and an app the fallback does not protect is tapped every minute for as long as it stays
in front (a game left running on a desk phone, say). `config.self_awake_apps` lists apps known to keep the screen on
by themselves, typically through an in-app "keep screen on" setting that is on by default. While the state is
unknown and one of them is running, the loop asks WDA once which app is in front (starting WDA first if it is not
answering, so the answer is not replaced by a tap); if it is a listed app, no tap. That sighting then holds without
another XCUITest query for as long as the same syslog stream runs and the app keeps its pid - every switch away
from it is logged, which hands the decision back to the idle timer - and WDA is retired as usual on a free phone.
Without a syslog stream WDA is asked every check and kept up, since a retired runner could not answer.

| `self_awake_apps` | while the idle timer is unknown |
|---|---|
| empty (default) | unchanged: the front-app rules below |
| listed app, reported in front | no tap; logged `... listed in self_awake_apps: no tap` |
| listed app, running but not in front | nothing different: unlike a protected app it does not hold taps off |

Once the idle timer is known it decides, list or no list: a listed app whose own setting is off reads `Auto` and
is tapped. The flip side is the only risk: during the unknown stretch the list is trusted, so list only apps whose
keep-awake is really on, or that phone may lock until the next foreground change.

A tap gives up after `tap_timeout_s` (75 s), or `tap_cold_timeout_s` (150 s) for the first one after a retire.
When taps keep failing, WebDriverAgent is not starting: the log says so after three in a row. Seen 2026-10-14 for ~10 min with a transient system overlay on screen (the idle-timer reason was
`SBTransientOverlayScenePresenterRequestedIdleTimer`); it recovered by itself once the phone was back on the home screen.

## When it does not tap (the front-app rules)

A video app's feed and creation pages keep the screen on by themselves, and an XCUITest tap while such an app is in
front can get it killed by the `0x8BADF00D` watchdog (seen on a staff build whose accessibility queries run on its
main thread). For each app in `config.protected_apps` plus the bundle the phone's holder declared
(`devlock acquire --bundle`), the loop checks: running? (devicectl process list), and is it the app WDA reports in
front? (`activeAppInfo`), and is it marked behind?

`config.protected_apps` ships **empty**, because only you know which app on your phone must not be tapped. An app
the loop does not protect counts as not there: on 2026-10-13 a loop tapped while an unprotected build was in front
and pushed it to the background. The process list matches on the app's `<Name>.app`; an entry without `app_name`
gets it from the phone (`devicectl device info apps`, the app's `url`), not the display name.

| Protected app | Tap? |
|---|---|
| not running | yes |
| WDA reports it in front | no - and in front it keeps the screen on itself |
| running, not in front, marked behind | yes |
| running, not in front, not marked - phone held by a session | no |
| running, not in front, not marked for 3 checks in a row - phone free | yes |

On a free phone (no devlock holder) nobody's run can be disturbed, while a locked phone needs its passcode
typed by a human, so an app running without turning up in front for `free_phone_unseen_checks` (3) checks in a row is
tapped anyway; the worst case is a protected app that was in front getting killed by the watchdog. Added after
2026-10-14: the user left the app for Settings by hand, nothing marked it behind, the loop held off and the phone locked.

Two more guards on a free phone, after 2026-10-14 (a hung front-app query crashed the loop, its restart reset the counts,
and 5 min 40 s passed untapped - the phone locked): a check that fails is logged and counted as "unknown" instead of
ending the loop, the query times out after `front_query_timeout_s` (20 s), checks run on a fixed
cadence (a check's own time comes out of the wait), and whatever the counts say the phone is tapped once
`free_phone_max_gap_s` (150 s) pass without a successful tap - unless WDA positively reports a protected app in front,
which then keeps the screen on itself. The last tap's time lives in the state dir, so a restart keeps it.

**"Not in front" is not always proof.** `activeAppInfo` names the front app outright, which is better than the
in-process debug-server scan this replaced - that one missed an app in front during launch, behind a system alert and
while its main thread was busy, and a rule that tapped after ~60 s unseen tapped an app that was in front
(2026-10-12). But WDA is not always there: no runner, no forwarder, or a query that timed out all come back as "not
in front" for every app, and that is not proof that none is in front. Hence the conservative rule on a held phone.

A mark is set by `ios_device.IOSDevice.background_app()`, or by `devlock awake behind` after checking a screenshot.
It is dropped when the app is relaunched through `launch_app`, when its pid changes, or when it turns up in front.
So if the user switches away from the app by hand, the log keeps saying "running and not known to be behind" until
someone marks it - or until the app comes back to the front, where it keeps the screen on anyway. For a screenshot
that needs no XCUITest: `go-ios screenshot`.

## When the taps fail

Since 2026-10-18 the loop **starts WebDriverAgent itself** rather than waiting out a timeout: before a tap that
follows a retire, and once after any tap that failed, it runs the steps in the shared device layer's
`_shared/ios_device.json` (`wda.ensure`: a go-ios tunnel, `ios runwda` to host the runner as an XCTest, `iproxy`)
and waits for `/status` to report ready. Verified on the iPhone 14 (iOS 26.6.1, go-ios 1.3.2): a fully cold tap -
no tunnel, no host, no forwarder, no runner process on the phone - logged `WDA started ... in 3 s` and then
`WDA tap ok`, 3.4 s in all. It starts nothing when WDA is already answering, and the tap is only retried when
something was actually started.

It cannot always work, and then it says so once rather than trying forever:

```
WDA would not start (a tap after WebDriverAgent was retired): tunnel: ios is not installed -
npm install -g go-ios; not trying again for 300 s
```

That is the verified line from a Mac without go-ios on PATH, logged in 0.0 s, with the tap's own failure on the
next line. A failed start blocks the next attempt for `ensure_retry_s` (300 s), so a phone where WDA cannot start
costs one line a check and nothing else - the three-in-a-row hint below is still what tells you to look at the
phone. `keepawake.ensure_wda: false` turns the whole thing off, for a Mac where something else owns the runner.
The runner it starts is torn down the same way as any other: the retire below stops it, and `ios runwda` then
exits by itself (verified: within 6 s). The go-ios tunnel agent is left running on purpose - it drives no app, and
keeping it is what makes the next start ~1.6 s instead of ~3 s.

`WDA tap FAILED: ... Make sure the application com.facebook.WebDriverAgentRunner.xctrunner exists and it is
launchable` together with an "Unable to Verify App" alert on the phone ("An Internet connection is required to verify
trust of the developer ...") means the phone could not verify the WDA runner's developer certificate, so the runner
cannot start; every tap attempt raises the alert again. `devlock awake off`; the user taps Cancel and fixes the phone's
network, then Settings > General > VPN & Device Management > that developer > Verify App; then `devlock awake auto`.

`WDA tap FAILED: ... Connection refused` means nothing is listening where `ios_device.json` says WDA is - the
forwarder died, or the runner was never hosted as an XCTest - *and* the loop could not start it, or was inside its
300 s backoff. `python3 ../_shared/scripts/wda.py <url>` says what is there;
`python3 ../_shared/scripts/wda.py --ensure --udid <UDID>` tries the start by hand and prints the same reason.

## Config

This skill's `config.json`: `protected_apps` (empty by default), `self_awake_apps` (empty by default), `keepawake.interval_s` (60), `tap_point`,
`pause_max_s` (120), `front_query_timeout_s` (20), `wda_idle_teardown_s` (120), `wda_idle_teardown_held_s` (600),
`wda_retire_check_s` (120), `ensure_wda` (true) and `ensure_retry_s` (300) for starting
WebDriverAgent, `awake_interval_s` (the daemon's USB poll, 30). The WDA URL, the steps that start the runner
(`wda.ensure`) and the automation-teardown process patterns are the shared device layer's
(`skills/_shared/ios_device.json`). Loop state
and logs: `~/.devlock/awake/state-<UDID>/keepawake.log`; daemon log `~/.devlock/awake/daemon.log`.
