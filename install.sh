#!/usr/bin/env bash
# Symlink every skill into each installed agent runtime's skills directory.
# Symlinks (not copies) so `git pull` updates what your agent runs, with no reinstall.
#
# Installs for BOTH Claude Code and Cursor in one run. Their layouts are identical —
# a directory per skill, each with a SKILL.md — only the location differs:
#   Claude Code  ~/.claude/skills
#   Cursor       ~/.cursor/skills-cursor      (note: NOT ~/.cursor/skills)
# A runtime is only installed into if its config directory already exists, so this
# does not scatter directories for tools you do not use.
#
# Override the targets with SKILLS_DIR="/a:/b" (colon-separated) if you need to.
#
# It also registers a session-start hook in each runtime that is present — Claude
# Code's SessionStart in ~/.claude/settings.json, and Cursor's sessionStart in
# ~/.cursor/hooks.json — both running scripts/session-update.sh: the `git pull` you
# would otherwise do by hand, in the background, hourly, and only ever on a clean
# checkout of a tracking branch.
#   ./install.sh --no-hook       link only, leave settings / hooks alone
#   ./install.sh --remove-hook   take the hooks out again
#
# The distribution path is the plugin marketplace — see README.
# If you ran this before the repo was reorganised into plugins/, re-run it once:
# the old symlinks now dangle and this prunes them.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

NO_HOOK=0
for arg in "$@"; do
  case "$arg" in
    --no-hook)     NO_HOOK=1 ;;
    --remove-hook) exec python3 "$REPO/scripts/session_hook.py" remove ;;
    -h|--help)     sed -n '2,20p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *)             echo "install.sh: unknown option $arg (try --help)" >&2; exit 2 ;;
  esac
done

# Every plugin's skills by default; PLUGIN="a b" narrows it. Installing all of them
# locally is the right default even for the platform-specific ones — a skill only ever
# runs when its description matches what you asked for, so an Android skill sitting
# unused on an iOS machine costs nothing.
shopt -s nullglob
srcs=()
for p in ${PLUGIN:-$(cd "$REPO/plugins" && echo *)}; do
  [ -d "$REPO/plugins/$p/skills" ] && srcs+=("$REPO/plugins/$p/skills")
done

[ ${#srcs[@]} -gt 0 ] || { echo "No plugin skills directories under $REPO/plugins/" >&2; exit 1; }

# Build the target list: an explicit SKILLS_DIR wins, otherwise every runtime whose
# config directory exists. `~/.claude/skills` is created if missing because Claude
# Code is the primary target; Cursor's is only used when Cursor is actually present.
targets=()
if [ -n "${SKILLS_DIR:-}" ]; then
  IFS=':' read -r -a targets <<< "$SKILLS_DIR"
else
  targets+=("$HOME/.claude/skills")
  [ -d "$HOME/.cursor" ] && targets+=("$HOME/.cursor/skills-cursor")
fi

link_into() {
  local dir="$1" found=0 name target
  mkdir -p "$dir"

  # Remove symlinks into this repo that no longer resolve — left by the pre-plugin
  # layout, which pointed at $REPO/skills/*. Never touches anything else.
  local link
  for link in "$dir"/*; do
    if [ -L "$link" ] && [ ! -e "$link" ]; then
      case "$(readlink "$link")" in
        "$REPO"/*) rm -f "$link"; echo "  prune $(basename "$link") — stale symlink" ;;
      esac
    fi
  done

  local skill src
  for src in "${srcs[@]}"; do
   for skill in "$src"/*/; do
    name="$(basename "$skill")"
    target="$dir/$name"

    # A directory is only a skill if it has a SKILL.md. Shared reference material
    # (skills/_shared/) and the scaffolding READMEs live alongside them and must not
    # be installed as skills.
    [ -f "$skill/SKILL.md" ] || continue

    if [ -e "$target" ] && [ ! -L "$target" ]; then
      echo "  skip  $name — $target exists and is not a symlink; move it aside first" >&2
      continue
    fi

    ln -sfn "${skill%/}" "$target"
    echo "  link  $name"
    found=$((found + 1))
   done
  done

  echo "  -> $found skill(s) in $dir"
  return 0
}

shopt -s nullglob
total=0
for dir in "${targets[@]}"; do
  case "$dir" in
    "$HOME/.claude/skills")        echo "Claude Code:" ;;
    "$HOME/.cursor/skills-cursor") echo "Cursor:" ;;
    *)                             echo "$dir:" ;;
  esac
  link_into "$dir"
  total=$((total + 1))
  echo
done

if [ "$total" -eq 0 ]; then
  echo "No runtime found. Install Claude Code or Cursor first, or set SKILLS_DIR." >&2
  exit 1
fi

# The auto-update hooks — Claude Code and/or Cursor, whichever config dirs exist.
# Each runtime's session start pulls the same checkout the
# symlinks already point at, so a Cursor-only machine stays current without Claude.
if [ "$NO_HOOK" -eq 0 ]; then
  echo "Auto-update hooks:"
  python3 "$REPO/scripts/session_hook.py" install --script "$REPO/scripts/session-update.sh" \
    || echo "  hook  not registered — see above; the links are in place regardless" >&2
  echo
fi

echo "Installed for $total runtime(s). Requires python3 and Xcode; see each skill for its extras."
