# devlock design notes

What each part does, what was verified on a real machine, and the incident behind each rule. Anything
not verified is marked so.

## Why each rule exists (incidents, 2026-10-13, one Mac, one iPhone 14, three agent sessions)

| Rule | Incident |
|---|---|
| Hold the phone for the whole run | Two sessions' taps interleaved mid-flow; the app under test went to the background and a later XCUITest screenshot triggered a 0x8BADF00D watchdog kill. |
| Turn-scoped holds release at turn end; `--ttl` for multi-turn | A hold created for a session that never learned about it was never released and blocked the next session. Conversely a multi-turn coverage run lost its hold at every turn end until `--ttl` existed. |
| Never create a hold on behalf of another session | Same incident: the session must know it holds the phone, or it never releases it. |
| Handoff cleanup | After a release, the previous holder's Appium server, port forwarder and keep-awake loop kept driving the phone; the next session's config overrides got no response and the app exited 11 s after launch. |
| `xcrun` shim + session identity | A script's internal `devicectl` calls bypassed the hook entirely. Then a detached Appium server (parent launchd) was refused as "another session" while its own session held the phone — hence `DEVLOCK_SESSION_PID` and the distinct "no identity" message. |
| One keep-awake daemon | Per-session keep-awake loops tapped into other sessions' runs, ran in triplicate, survived their session, and with `--bundle` watched the wrong app. |

## Components

- `devlock` (bash): lock directories (`mkdir` is atomic) with an `owner` file: `pid`, `mode`
  (`run` = one command, owner is that devlock process; `acquire` = session hold, owner is the agent
  process), `since`, `cwd`, optional `expires` (TTL), `bundle`, `note`. A lock counts while its owner is
  alive and it has not expired; a turn-scoped hold also expires after 30 min without use (any device
  command of the holder refreshes it). Take-overs are logged to `locks/handoffs.log`.
- Identity: `DEVLOCK_SESSION_PID` if set, else the nearest agent ancestor, recognised by its executable
  (`ps -o comm=`; the Claude path contains spaces) or, for Cursor desktop, its process title - never by text in a
  command line, which can mention anything. A `claude` ancestor is the session. For a Cursor ancestor
  (`cursor-agent`, or desktop's `Cursor Helper (Plugin): extension-host`) the session is the conversation in
  `CURSOR_CONVERSATION_ID` and the Cursor process only anchors liveness; the hold records `conv=`. A
  `CURSOR_CONVERSATION_ID` inherited under a `claude` ancestor is ignored. A lock is "mine" when its `conv`
  matches mine, or (no `conv`) its owner is my identity or one of my ancestors.
- Phones: USB tree (`ioreg -p IOUSB`), serial → hardware UDID. `idmap.py` maps CoreDevice UUIDs and
  device names to the hardware UDID via `xcrun devicectl list devices --json-output`.
- `cleanup.py`: runs when a phone lock is freshly taken. Victims: Appium servers, WebDriverAgent
  `xcodebuild` runners, keep-awake loops — bound to this phone (UDID in command or `DEVLOCK_DEVICE` /
  `DEVICE_UDID` env), or unbound when this is the only USB phone. Spared: `DEVLOCK_INFRA=1`, the taker's
  identity, our ancestors, `--remote`, anything under `~/.devlock/awake`, and passive services that merely
  read from the phone (an `idevicesyslog` stream another session may be capturing).
  The rule for sparing something is that it has no session identity, its parent is launchd, it has no
  clients, and killing it would break a reader rather than free the phone.
- `keepawaked.py` (launchd `com.devlock.keepawake`): every 30 s, for each USB phone not switched off, keeps
  one `keepawake.py run` child (this skill's loop, on the shared device layer) with the live holder's bundle as `--protect` (restart on
  change), stops it (SIGTERM) on unplug or `awake off`. Children run with `DEVLOCK_INFRA=1`.
- `hook.py`: one script for both hosts, told apart by `hook_event_name`.

## Verified host behaviour

Claude Code (2.1.x):
- PreToolUse deny: `{"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": ...}}`. Settings changes are picked up by running sessions without restart.
- SessionStart can append `export X=...` to `$CLAUDE_ENV_FILE`; it reaches every later Bash command and
  processes they detach (verified with a fresh session and a launchd-reparented child).
- Shell commands run under the `claude` process, so the ancestor walk finds the session.

Cursor (`cursor-agent` 2026.10.01, verified with a project-level `.cursor/hooks.json` probe):
- The CLI runs `sessionStart` and `beforeShellExecution` hooks. Input carries `command`, `cwd`,
  `conversation_id`, `session_id`, `workspace_roots`, `hook_event_name`.
- `beforeShellExecution` output: `{"permission": "allow"|"deny", "user_message", "agent_message"}`; per the
  docs exit 0 with invalid JSON blocks, exit 2 denies, other codes fail open — so hook errors exit 1.
- Hooks and the agent's shell commands are direct children of the `cursor-agent` process.
- The model saw only `user_message` (shown as "Command execution was blocked by a hook: ..."), not
  `agent_message`, so the hook puts the full fix in both. With that, an uninformed cursor-agent quoted the
  required `devlock --name device-<UDID> --` wrapper and the no-bypass rule back correctly.
- `sessionStart`'s `env` reaches later hooks but NOT the agent's shell commands — Cursor sessions get no
  injected identity; helpers need `DEVLOCK_SESSION_PID=$(devlock whoami)`.
- In `-p` (print) mode the `stop` hook did not fire; the process exits at the end, which frees its holds.
- Every agent shell gets `CURSOR_AGENT=1` and `CURSOR_CONVERSATION_ID` (Cursor's `encodeURIComponent` of the
  conversation id with `%` -> `_`; a UUID passes unchanged, so it equals the hooks' `conversation_id`). Verified
  for `cursor-agent`: the same value on every command of a conversation; read from Cursor's agent-exec code for
  desktop.
- Desktop (verified 2026-10-14 with two chats in one window): each command is a fresh `bash` whose parent is the
  window's `Cursor Helper (Plugin): extension-host` process, shared by every chat of that window - no process
  identifies a chat, hence conversation identity. The `stop` hook carries `conversation_id`; it releases that
  conversation's turn-scoped holds unless a process with its `CURSOR_CONVERSATION_ID` still runs a device job.
- End to end on desktop (2026-10-14, two chats in one window, both anchored on extension host pid 77951): chat A
  `devlock acquire --ttl 10m` held the phone as conversation 28318f63; chat B's `whoami` showed its own conversation;
  B's bare `devicectl` command was denied by the beforeShellExecution hook; B re-ran it wrapped as the message said,
  queued "held by ... cursor-conversation=28318f63", and got the phone 34 s later when A released.
- A cloud agent runner may start the Cursor CLI as `~/.local/bin/agent`, a second symlink to the same cursor-agent binary, and
  `ps -o comm=` shows the name it was started under - so agent processes are matched by their executable resolved
  through symlinks. Verified 2026-10-14: `agent -p` -> `devlock whoami` = `cursor conversation <id>`.
- Not verified: interactive `cursor-agent` firing `stop`.

## Testing

`scripts/tests/test_devlock.py` runs everything against a throwaway `DEVLOCK_HOME` and fake phones
(`DEVLOCK_FAKE_WIRED`). Fake helper processes use node, because macOS hides the environment of
platform binaries such as `/bin/sleep` from `ps -E` — a test with `sleep` would misjudge identity.
