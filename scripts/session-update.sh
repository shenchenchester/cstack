#!/usr/bin/env bash
# session-update.sh — keep a local install of these skills current, from a Claude Code
# SessionStart or Cursor sessionStart hook. Fast, silent and never fatal: the hook
# returns at once, the work runs detached in the background, and nothing here can
# block or clutter a session.
#
#   scripts/session-update.sh            what the hook runs: one attempt per hour
#   scripts/session-update.sh --now      attempt regardless of the throttle
#   scripts/session-update.sh --status   what it sees, and the last few log lines
#
# `./install.sh` registers the hooks (`--no-hook` skips that, `--remove-hook` undoes
# them) in both Claude Code and Cursor when those runtimes are present.
#
# What "update" means depends on how the skills got onto this machine, which it reads
# off what ~/.claude/skills/<skill> or ~/.cursor/skills-cursor/<skill> resolves to:
#
#   a symlink into a git clone   git pull --ff-only in that clone, then re-link, so a
#                                skill added upstream appears too. Only when the clone
#                                is clean and on a branch that tracks a remote: a
#                                checkout someone is editing is never touched, only
#                                logged. It is never reset, and never autostashed —
#                                a conflict marker nobody typed is worse than staleness.
#   anything else                nothing. A Claude Code plugin install is updated by
#                                Claude Code's marketplace auto-update once that is
#                                switched on; a Cursor local-marketplace install is
#                                refreshed by Cursor itself. No install at all means
#                                nothing to do.
#
# State: ~/.cstack/ (CSTACK_HOME)
#   last-session-update    epoch of the last attempt — the throttle
#   session-update.log     one line per outcome; the ONLY place output goes
#   lock/                  held for the duration of an attempt
#
# Tunables: CSTACK_UPDATE_INTERVAL (seconds, default 3600), CLAUDE_CONFIG_DIR,
# CURSOR_CONFIG_DIR. Exit code is always 0. Written for bash 3.2, because that is what
# macOS ships.

set +e

STATE_DIR="${CSTACK_HOME:-$HOME/.cstack}"
INTERVAL="${CSTACK_UPDATE_INTERVAL:-3600}"
CLAUDE_DIR="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
CURSOR_DIR="${CURSOR_CONFIG_DIR:-$HOME/.cursor}"
PROBES="ios-build-device-lock ios-log"   # one skill is enough to find the checkout
# Symlink install targets — Claude Code and Cursor share the same checkout when both
# are linked; probing either is enough. Cursor-only machines only have the second.
SKILL_DIRS="$CLAUDE_DIR/skills $CURSOR_DIR/skills-cursor"
STAMP="$STATE_DIR/last-session-update"
LOG="$STATE_DIR/session-update.log"
LOCK="$STATE_DIR/lock"
LOCK_TTL_MIN=30
STEP_TIMEOUT=120

log() {
  mkdir -p "$STATE_DIR" 2>/dev/null
  printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >> "$LOG" 2>/dev/null
}

# bounded <seconds> <cmd...> — run cmd, kill it after <seconds>. macOS has no timeout(1),
# and a hung `git pull` or `npx` must not outlive the session that started it.
bounded() {
  local secs="$1" pid wd rc
  shift
  "$@" &
  pid=$!
  ( sleep "$secs"; kill "$pid" 2>/dev/null ) &
  wd=$!
  wait "$pid"
  rc=$?
  kill "$wd" 2>/dev/null
  wait "$wd" 2>/dev/null
  return $rc
}

# detect — sets KIND (git | none) and TARGET.
detect() {
  KIND=none
  TARGET=""
  local probe real top skills_dir
  for skills_dir in $SKILL_DIRS; do
    for probe in $PROBES; do
      [ -d "$skills_dir/$probe" ] || continue
      real=$(cd "$skills_dir/$probe" 2>/dev/null && pwd -P) || continue
      top=$(git -C "$real" rev-parse --show-toplevel 2>/dev/null) || continue
      # A checkout of this repo, not just any git repo the symlink happens to land in
      # (a home directory kept in git would otherwise qualify).
      if [ -f "$top/install.sh" ] && [ -d "$top/plugins" ]; then
        KIND=git
        TARGET="$top"
        return 0
      fi
    done
  done
  return 0
}

update_git() {
  local repo="$1" branch upstream old new rc err reason
  if [ -n "$(git -C "$repo" status --porcelain --untracked-files=no 2>/dev/null)" ]; then
    log "SKIP kind=git repo=$repo reason=dirty"
    return
  fi
  branch=$(git -C "$repo" symbolic-ref --short -q HEAD 2>/dev/null)
  if [ -z "$branch" ]; then
    log "SKIP kind=git repo=$repo reason=detached"
    return
  fi
  upstream=$(git -C "$repo" rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>/dev/null)
  if [ -z "$upstream" ]; then
    log "SKIP kind=git repo=$repo reason=no_upstream branch=$branch"
    return
  fi
  old=$(git -C "$repo" rev-parse HEAD 2>/dev/null)
  err=$(mktemp "${TMPDIR:-/tmp}/cstack-pull-XXXXXX" 2>/dev/null || echo /dev/null)
  # No credential prompts, no ssh prompts, no waiting on a dead network.
  bounded "$STEP_TIMEOUT" env GIT_TERMINAL_PROMPT=0 \
    GIT_SSH_COMMAND="${GIT_SSH_COMMAND:-ssh -o BatchMode=yes -o ConnectTimeout=15}" \
    git -C "$repo" pull --ff-only -q 2>"$err"
  rc=$?
  new=$(git -C "$repo" rev-parse HEAD 2>/dev/null)
  if [ "$rc" -ne 0 ]; then
    reason=$(head -c 200 "$err" 2>/dev/null | tr '\n' ' ' | tr -s ' ')
    log "PULL_FAILED kind=git repo=$repo branch=$branch rc=$rc reason=${reason:-unknown}"
    [ "$err" != /dev/null ] && rm -f "$err"
    return
  fi
  [ "$err" != /dev/null ] && rm -f "$err"
  if [ "$old" = "$new" ]; then
    log "UP_TO_DATE kind=git repo=$repo head=$old"
    return
  fi
  # Re-link, so a skill that arrived with the pull is installed too. --no-hook: the
  # background job must never edit settings.json underneath a running session.
  if [ -f "$repo/install.sh" ]; then
    bounded "$STEP_TIMEOUT" bash "$repo/install.sh" --no-hook >/dev/null 2>&1 \
      || log "RELINK_FAILED repo=$repo"
  fi
  log "UPDATED kind=git repo=$repo branch=$branch from=$old to=$new"
}

status() {
  local last now ago
  echo "install:  $KIND${TARGET:+ $TARGET}"
  if [ "$KIND" = git ]; then
    local branch state
    branch=$(git -C "$TARGET" symbolic-ref --short -q HEAD 2>/dev/null || echo detached)
    if [ -n "$(git -C "$TARGET" status --porcelain --untracked-files=no 2>/dev/null)" ]; then
      state="dirty, so it will not pull"
    else
      state="clean"
    fi
    echo "          branch $branch, $state"
  fi
  if [ -f "$STAMP" ]; then
    last=$(cat "$STAMP" 2>/dev/null)
    now=$(date +%s)
    case "$last" in ''|*[!0-9]*) last=0 ;; esac
    ago=$(( now - last ))
    if [ "$ago" -lt "$INTERVAL" ]; then
      echo "throttle: last attempt $(( ago / 60 )) min ago, next after $(( (INTERVAL - ago) / 60 )) min"
    else
      echo "throttle: last attempt $(( ago / 60 )) min ago, next session will try"
    fi
  else
    echo "throttle: never run"
  fi
  echo "log:      $LOG"
  tail -n 5 "$LOG" 2>/dev/null | sed 's/^/          /'
}

FORCE=""
case "${1:-}" in
  "")          ;;
  --now)       FORCE=1 ;;
  --status)    detect; status; exit 0 ;;
  -h|--help)   sed -n '2,32p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
  *)           echo "session-update.sh: unknown option $1 (try --help)" >&2; exit 0 ;;
esac

# Throttle — in the foreground, because it is the common case and costs one read.
if [ -z "$FORCE" ] && [ -f "$STAMP" ]; then
  last=$(cat "$STAMP" 2>/dev/null)
  case "$last" in ''|*[!0-9]*) last=0 ;; esac
  if [ $(( $(date +%s) - last )) -lt "$INTERVAL" ]; then
    exit 0
  fi
fi

detect
mkdir -p "$STATE_DIR" 2>/dev/null
date +%s > "$STAMP" 2>/dev/null

if [ "$KIND" = none ]; then
  log "SKIP reason=no_install"
  exit 0
fi

# Everything slow happens here, detached. The subshell drops the hook's stdio first:
# once the hook has exited, a write to those pipes would kill the job with SIGPIPE.
(
  exec </dev/null >/dev/null 2>&1
  if ! mkdir "$LOCK" 2>/dev/null; then
    if [ -n "$(find "$LOCK" -maxdepth 0 -mmin +"$LOCK_TTL_MIN" 2>/dev/null)" ]; then
      rm -rf "$LOCK"
      mkdir "$LOCK" 2>/dev/null || { log "SKIP reason=locked"; exit 0; }
      log "LOCK_RECLAIMED age_min>$LOCK_TTL_MIN"
    else
      log "SKIP reason=locked"
      exit 0
    fi
  fi
  trap 'rm -rf "$LOCK"' EXIT
  case "$KIND" in
    git) update_git "$TARGET" ;;
  esac
) &

exit 0
