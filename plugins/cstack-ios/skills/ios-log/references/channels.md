# Log channel wiring — the mechanism, and what was verified

Three channels can carry an iOS app's log lines, and they are not interchangeable. What follows
is the OS-level mechanism (true for any app) plus what was actually observed on hardware. Where a
claim depends on how *your* logger is wired, it says so — that part you have to check in your own
source, and `config.json` is where the answer goes.

Observations below are from an iPhone 14 on iOS 26.6 unless stated otherwise.

## The three channels

| Channel | What it is | Who can read it |
|---|---|---|
| the process's **stdout/stderr** | the app's own file descriptors | exactly one subscriber: whoever launched the process |
| the **unified log** (`os_log`) | the system-wide log, on-device ring buffer | anyone: `idevicesyslog`, Console.app, `log stream` |
| the logger's **own files** | whatever your logger mmaps to disk | your crash/log-reporting pipeline; out of scope here |

The channel your lines land on is decided by how your logger was compiled, which almost always
differs between Debug and Release:

- **`NSLog` and `print` go to stderr**, not to the unified log. (`NSLog` *also* writes to the
  unified log on recent OS versions, but a logger that calls it for console output is relying on
  the stderr side.) So a logger whose Debug-build console path is `NSLog` is invisible to
  `idevicesyslog`. Device-verified: 316k syslog lines, 0 from the app, while the same launch's
  stderr carried between 19k and 239k lines.
- **An `os_log` echo is usually compiled out of Debug builds** and switched off by default in
  Release ones. If your app has a switch for it (an env var, or a `NSUserDefaults` key set from a
  debug panel), name the env var in `config.release_console_env` and `capture.sh
  device-console --release-echo` will set it at launch. Verified on one such app: with the echo on,
  the unified log carried the lines in full plaintext while the console capture that launched the
  app stayed at **0** lines — the two channels are genuinely exclusive, not duplicated.
- Payloads logged `%{public}s` bypass the unified log's redaction. Anything else from a
  third-party process reads as `<private>`.

## stdout ownership

stdout/stderr belong to whoever launched the process. There is no second subscriber:

- Xcode Run → Xcode's console has the lines; devicectl/syslog captures see none of them.
- `devicectl device process launch --console` → your capture has them; Xcode's console won't
  (in attach mode it shows only `os_log`). Launching also `--terminate-existing`s the previous
  instance, and terminating the devicectl process SIGKILLs the app — both observed live
  ("App terminated due to signal 9" at the capture's tail).

**The stderr trap.** `devicectl --console` forwards the app's stderr on *its own* stderr. A
capture written as `devicectl … | tee file` therefore keeps only the `printf`-style lines and
**none** of the `NSLog` ones — a file that looks populated but contains nothing from your logger.
`capture.sh` merges both with `2>&1`.

Debugger + capture coexistence: make Xcode attach instead of launch. Scheme → Run → Info →
"Wait for the executable to be launched" (`launchStyle="1"` in the `.xcscheme` XML), press Run,
then launch via devicectl; or Debug → Attach to Process after the fact. Xcode's AppleScript
dictionary has no attach verb, and menu-clicking it via System Events needs an Accessibility grant.

## Two device-id systems

- `xcrun devicectl list devices` → CoreDevice UUIDs (e.g. `31D5357A-…`). Every `devicectl`
  command takes these.
- `idevice_id -l` → raw UDIDs (e.g. `00008110-…`). Every libimobiledevice tool takes these.

They are not interchangeable, and a command given the wrong one fails in a way that reads like
"device not found". `capture.sh detect` prints both.

## Unified-log gotchas (device-syslog and sim channels)

- `<private>` redaction on non-`{public}` payloads — most of syslog reads as `<private>` for
  third-party processes.
- Info/debug-level `os_log` is not streamed by default; widen `log stream` to debug level, at a
  volume cost.
- The firehose drops lines under load. Treat counts as approximate.
- Retrospective pulls: `sudo log collect --device-udid <raw UDID>` grabs the archive for offline
  `log show` queries.
- **Don't filter inside the relay pipe.** `idevicesyslog | grep --line-buffered …` wrote nothing
  for minutes while the raw stream carried ~60 matching lines a second. Capture raw, filter after
  with `analyze.sh`. An empty file reads exactly like "the app logged nothing", which is why this
  one costs so much time.

## Line anatomy

The default `config.log_line_regex` matches this common in-house shape:

```
[2026-08-26 -7.0 15:39:15.140][6118:4401720656*][I][SomeModule][File.swift,method(_:),871] message
```

- `[timestamp tz]` `[pid:tid]` — a `*` suffix on the tid marks the main thread.
- `[I|D|W|E]` level, `[tag]` module tag — the tag is the useful grep target.
- `[file,func,line]` — **some loggers use full-width commas (，) here**, not ASCII ones, and an
  ASCII-comma regex then silently matches nothing. Check one real line before trusting a pattern.
- devicectl captures prefix every line with `date time ProcessName[pid:tid] `; a few devicectl
  status lines (tunnel, launch, terminate) sit un-prefixed at the head and tail.
- Off-format lines mixed in are direct `NSLog`/stderr writes from SDKs. Normal, and not a sign
  anything is wrong.

If your logger prints a different header, change `log_line_regex`, `level_regex`, `tag_regex`,
`error_levels` and `main_thread_regex` in `config.json`; `analyze.sh` reads all five from there.

## Bundle and process discovery

The process name on the phone is often *not* the bundle id's last component, and an App Store
build and an in-house build of one app usually have different ones — which matters because
`idevicesyslog -p` and the simulator predicate both filter on the process name, not the bundle.
Find them with:

```bash
xcrun devicectl device info apps --device <CoreDevice UUID> --bundle-id <bundle>
xcrun devicectl device info processes --device <CoreDevice UUID>
```

Put both in `config.json` so the scripts stop guessing.
