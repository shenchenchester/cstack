---
name: ios-log
description: Read and analyze a real iOS app's own logs on a USB device or the simulator - pick the one OS channel that actually carries them for this build type (they differ, and the wrong channel shows nothing with no error), capture a live stream to a file, keep the Xcode debugger attached while capturing, and filter or summarize the captured lines by module tag, level, thread or time window. Use when someone wants to read, tail, capture or analyze an iOS app's logs, or cannot work out why a log channel is empty - realtime device logs, 真机日志, 实时日志, 读日志, 抓 log, console 输出, idevicesyslog, devicectl 日志, 模拟器日志, 日志分析, 过滤日志, why is my log empty, nothing in the Xcode console, reading logs outside Xcode, NSLog not showing up, os_log is private, 看不到日志, 日志为空.
---

# Reading an iOS app's logs

Most in-house iOS loggers surface on a **different OS channel depending on how the app was
built**, and that is the whole trap this skill exists for. A Debug build usually writes to the
process's own console - and because `NSLog` writes to **stderr**, a capture that keeps only
stdout gets nothing. A Release build usually writes to neither channel unless someone compiled
in an `os_log` echo. Pick the channel from the matrix before running anything: the wrong
channel produces zero lines with no error, which reads exactly like "the app logged nothing".

Fill in `config.json` first (your bundle id, process name, and the env var that enables a
Release-build echo if your app has one). Mechanism and receipts:
[references/channels.md](references/channels.md).

## Channel matrix — pick first

| Scenario | The app's log lines surface on | Capture with |
|---|---|---|
| Device, Debug build | the process's **stderr** (`NSLog`), forwarded by `devicectl --console` | `scripts/capture.sh device-console` (devicectl owns stdout/stderr) |
| Device, Release build with an os_log echo switch | nothing by default; the **unified log** once the switch is set — never stdout | launch with `device-console --release-echo` (its own output stays empty) and read with `device-syslog --process <name>` running alongside |
| Device, Release build with no such switch | nothing on either channel | your crash/log-reporting vendor's on-device files; out of scope here |
| Device + you must keep the Xcode debugger | stdout goes to whoever launched | launch via `device-console`, then Xcode **attaches** (see below) |
| Simulator (any build) | stdout + unified log | `scripts/capture.sh sim` |

Verified traps, each of which has cost someone an afternoon:

- **`idevicesyslog` cannot see a Debug build's console output.** Device-verified: 316k syslog
  lines, 0 from the app. A process's stdout/stderr never enters the unified log, and a Debug
  build's `os_log` echo is usually compiled out. Don't burn time here.
- **The console lines are stderr, not stdout.** `NSLog` writes to stderr, and
  `devicectl --console` forwards it on *its* stderr. A capture that redirects only stdout
  (`devicectl … | tee file`) gets the `printf` lines and **zero** log lines, which looks exactly
  like "the logger never initialized". `capture.sh` merges both (`2>&1`).
- **stdout is exclusive.** Whoever launches the process owns it. Xcode Run takes it (and
  SIGKILLs any devicectl-launched instance); a devicectl launch takes it back. There is no
  second subscriber.
- **Ending a `devicectl --console` stream kills the app** (signal 9). Warn before stopping a
  capture the user is mid-session on.
- **Unified log redacts.** Third-party `os_log` payloads show `<private>` unless logged
  `%{public}`, and info/debug-level `os_log` doesn't stream by default.
- **Don't put `grep` in the relay pipe.** A `idevicesyslog | grep --line-buffered …` stage wrote
  nothing for minutes while the raw stream carried ~60 lines a second. Capture raw, filter after.

## Capturing

`scripts/capture.sh` auto-discovers the device (CoreDevice UUID and raw UDID are different id
systems; it handles both) and takes the bundle id from `config.json`. Output goes to a timestamped
file it prints; pass `--out` to choose.

```bash
scripts/capture.sh detect                  # what's connected, booted, installed, which tools exist
scripts/capture.sh device-console          # relaunch on device, own stdout+stderr, stream to file
scripts/capture.sh device-console --release-echo    # also set config.release_console_env at launch
scripts/capture.sh device-syslog           # unified-log relay (needs libimobiledevice)
scripts/capture.sh device-syslog --process MyApp --keep-alive   # one process, in the background
scripts/capture.sh sim                     # booted simulator, log stream filtered to your process
```

`device-syslog` saves the raw stream; filter it afterwards with `analyze.sh`. `device-console`
runs until you stop it (or `--duration N` seconds) — remind the user the app restarts when
capture starts and dies when a `--duration` capture ends; `--keep-alive` leaves the stream running
in the background between analysis rounds.

### Keeping the Xcode debugger

Breakpoints and the captured stream can coexist — the debugger must **attach** instead of launch:

1. Either set the scheme's launch style to "Wait for the executable to be launched"
   (Edit Scheme → Run → Info; `launchStyle="1"` in the `.xcscheme` XML), press Run, then start
   `device-console` — Xcode auto-attaches; or start `device-console` first and use
   Debug → Attach to Process manually.
2. In attach mode Xcode's console no longer shows stdout (you have it in the capture file);
   `tail -f` the file for the live view.

## Analyzing

`scripts/analyze.sh` keys off `config.log_line_regex`, which defaults to the common in-house
shape:

```
[2026-08-26 -7.0 15:39:15.140][6118:4401720656*][I][SomeModule][File.swift,method(_:),871] message
```

`[time tz][pid:tid][level][tag][file,func,line] msg`, where `*` after the tid marks the main
thread. If your logger prints something else, change the regexes in `config.json`.

```bash
scripts/analyze.sh summary <file>                      # counts by tag and level, time span, top talkers
scripts/analyze.sh errors  <file>                      # error and warning lines only
scripts/analyze.sh tag     <file> <TAG>                # one module's trace (substring match)
scripts/analyze.sh window  <file> <HH:MM:SS> <HH:MM:SS>  # slice a time range
scripts/analyze.sh main    <file>                      # main-thread lines only
```

Two format traps worth knowing before you grep a file yourself: some loggers use **full-width
commas (，)** inside the location bracket, so ASCII-comma regexes silently miss; and devicectl
captures prefix every line with `date time Process[pid:tid] `, with a few un-prefixed devicectl
status lines at the head and tail. Off-format lines mixed in are direct `NSLog`/stderr writes
from SDKs — normal.

Analysis is judgement work: after extracting, actually read the sequence — correlate timestamps
across tags, check thread markers for main-thread violations, and say explicitly which
conclusions are read from lines and which are inferred from absence. An absent log means "this
channel doesn't carry it" at least as often as "it didn't run" — rule out the channel first
(that is what `detect` and the matrix are for).

## When adding logs of your own (the write side)

Reading logs usually means someone just added some. Two rules for those:

1. **Debug `NSLog`/`print` never ships.** They exist to make a value visible right now; delete
   them before the merge. `NSLog` bypasses your logger entirely — no level control, no tag, no
   file routing — and lands in the unified log of every production device, synchronously, on the
   calling thread. If a log deserves to survive the investigation, rewrite it as a proper logger
   line with a tag and a level.
2. **No chatty logging on production paths.** A good logger is cheap but not free — formatting
   plus a write per line — and per-frame / per-scroll-tick / per-cell logging shows up as CPU and
   I/O on exactly the surfaces you guard (frame rate, input latency), while bloating whatever
   your crash reporter pulls. For hot paths: log state *changes* rather than state, sample, gate
   behind a debug flag, or take the log out once the question it answered is closed.

## When zero lines come back

Work the list in order; each item has produced the "empty log" symptom in practice:

1. Wrong channel for the build type (the matrix above; most common by far).
2. Xcode Run stole stdout after capture started (`App terminated due to signal 9` in the capture
   file's tail).
3. The capture kept only stdout, so the lines went to the terminal instead of the file. If the
   file has `printf`-style lines but none in your logger's format, look at the terminal.
4. A `grep` stage in the relay pipe buffered everything (see the traps above).
5. The feature simply didn't run — its trigger surface was never touched, or a feature flag keeps
   it silent. Check for the module's own init lines before concluding anything.
6. A Release build with no echo switch set (`device-console --release-echo`), or you read the
   console capture instead of the syslog one: with the echo on, the lines go to the unified log
   and the console capture stays at 0.
