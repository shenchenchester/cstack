#!/bin/bash
# SessionStart hook: export this Claude session's pid as DEVLOCK_SESSION_PID into every Bash
# command of the session (via CLAUDE_ENV_FILE), so processes that outlive their shell — an
# Appium server reparented to launchd, WDA proxies — still carry the session's devlock identity.
[ -n "${CLAUDE_ENV_FILE:-}" ] || exit 0
p=$PPID
while [ -n "$p" ] && [ "$p" -gt 1 ]; do
  case "$(ps -o command= -p "$p" 2>/dev/null)" in
    */claude|*/claude\ *|claude|claude\ *) echo "export DEVLOCK_SESSION_PID=$p" >> "$CLAUDE_ENV_FILE"; exit 0 ;;
  esac
  p=$(ps -o ppid= -p "$p" 2>/dev/null | tr -d ' ')
done
exit 0
