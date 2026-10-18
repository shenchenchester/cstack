# cstack

Agent skills for iOS work that need nothing but Xcode, [libimobiledevice] and a WebDriverAgent
runner. No company tooling, no internal services.

Two skills so far, both for the awkward parts of working against a **real iPhone** from an agent:
stopping several agent sessions on one Mac from fighting over the build and the phone, and finding
the one log channel that actually carries your app's lines.

[libimobiledevice]: https://libimobiledevice.org

## Skills

| Skill | What it does | |
|---|---|---|
| `ios-build-device-lock` | `devlock`: one build lock and one lock per USB phone, shared by every agent session on the Mac. Queues instead of colliding, hands a phone over cleanly, and runs a per-phone daemon that keeps a plugged-in iPhone from auto-locking — reading the phone's own idle timer rather than guessing, and never tapping while a protected app may be in front. Hooks for Claude Code and Cursor refuse an unlocked build or device command and print the exact fix. | iOS |
| `ios-log` | Picks the one OS channel that carries your app's log lines for the build in front of you (they differ by build type, and the wrong channel is empty with no error), captures it to a file, keeps the Xcode debugger attached while capturing, and filters the result by module tag, level, thread or time window. | iOS |

`plugins/cstack-ios/skills/_shared` is the device layer both sit on: `devicectl` for process and
app lifecycle, WebDriverAgent over HTTP for coordinate taps and for which app is in front. It is
not a skill; it is the one implementation of "talk to a USB iPhone" so the two skills share the
same session and the same state.

## Install

```bash
git clone https://github.com/shenchenchester/cstack.git
cd cstack && ./install.sh
```

> **If you already run a skill of the same name from another checkout, this will take it over.**
> `install.sh` links by skill name with `ln -sfn`, so `~/.claude/skills/ios-build-device-lock`
> ends up pointing here, and the keep-awake daemon then resolves its device layer out of *this*
> repo. That matters because the two are not interchangeable: this one taps through WebDriverAgent.
> Check with `ls -l ~/.claude/skills/ios-build-device-lock` first, and use
> `SKILLS_DIR=/some/other/dir ./install.sh` if you want them side by side.

One run symlinks every skill into Claude Code (`~/.claude/skills`) and Cursor
(`~/.cursor/skills-cursor`), for whichever of the two is present. Symlinks, not copies, so a
`git pull` updates what your agent runs with no reinstall — and the installer registers a
session-start hook that does that pull for you, at most hourly, only ever on a clean checkout of
a tracking branch (`--no-hook` skips it, `--remove-hook` undoes it).

Or add it as a plugin marketplace, in Claude Code:

```
/plugin marketplace add shenchenchester/cstack
```

Then ask for the thing in your own words — "keep the phone from locking", "why is my log empty",
"另一个会话在用手机". A skill loads when its description matches what you asked for.

## What you need

| | For |
|---|---|
| Xcode | everything — `xcrun devicectl` is the backbone |
| python3 | the scripts; standard library only, no packages to install |
| `brew install libimobiledevice` | `idevicesyslog`, which is both the log relay and the signal keep-awake decides on |
| a WebDriverAgent runner | only keep-awake's taps. See [wda.py](plugins/cstack-ios/skills/_shared/scripts/wda.py) — it has to be hosted as an XCTest, and launching the `.xctrunner` app does *not* bring its server up. Without it the locks, queueing and handover all still work. |

## Layout

```
LICENSE                           MIT
.claude-plugin/marketplace.json   what Claude Code can install
.cursor-plugin/marketplace.json   what Cursor can install
install.sh                        symlink the skills + register the update hook
plugins/cstack-ios/
  plugin.json                     name, description, version
  skills/_shared/                 the device layer (not a skill)
  skills/ios-build-device-lock/
  skills/ios-log/
scripts/sync_registry.py          rewrite both marketplace files from what is in plugins/
scripts/session-update.sh         the session-start updater install.sh registers
tests/                            the repo's own guards (see below)
```

## Working on it

Both marketplace files are whitelists — a skill that is not listed reaches nobody, and nothing
errors to tell you. So after adding or renaming a skill:

```bash
python3 scripts/sync_registry.py                      # rewrite both registries
python3 scripts/sync_registry.py --bump cstack-ios     # patch; --level minor for a new skill
```

Publishing happens on a version change, so a change shipped without a bump lands in the repo,
registers fine, and still reaches nobody. `--check` says whether the registries are stale and
which plugins have changed since their last bump; `--gate origin/main` is the CI form, which fails.

Then run the tests:

```bash
python3 tests/test_registry_in_sync.py
python3 tests/test_docs_resolve.py
python3 tests/test_version_gate.py
python3 tests/test_session_update.py
cd plugins/cstack-ios/skills/_shared && python3 -m unittest discover -s scripts/tests
cd plugins/cstack-ios/skills/ios-build-device-lock && python3 -m unittest discover -s scripts/tests
```

The first four are the repo's: a skill that is registered nowhere, a doc link pointing at a file
that is not there, a version gate that has quietly stopped gating, and a session-start hook that
is slow, noisy, or willing to damage a checkout. None of those four failures raises an error on
its own, which is why they are tests. The last two belong to the shared device layer (run it after
touching `ios_device.py` or `wda.py`, which both skills import) and to `devlock`, whose suite runs
against a throwaway lock directory and fake USB phones and never touches the real ones.

## Provenance

These two started life inside a company monorepo, against one specific app, and were rewritten
here to depend on nothing but public tooling: the tap engine moved from an in-house XCUITest CLI
to WebDriverAgent over HTTP, `ios-log` lost its one logger's internals and keeps the channel
mechanism that is true of any iOS app, and `config.json` is where everything app-specific now
lives — empty, for you to fill in.

Verified behaviour is written down as verified, and the rest says so. `wda.py`'s header is the
example worth copying: it names the runner version, device and iOS it was checked against, which
of its three tap routes was actually exercised, and the one trap that was confirmed by watching it
fail.

## License

[MIT](LICENSE).
