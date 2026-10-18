#!/bin/bash
# Capture an iOS app's logs on the channel that actually carries them for this build.
# Subcommands: detect | device-console | device-syslog | sim
# See ../SKILL.md for the channel matrix; ../references/channels.md for why.
set -euo pipefail

SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG="$SKILL_DIR/config.json"

cfg() { python3 -c "import json,sys;c=json.load(open('$CONFIG'));print(c$1)" 2>/dev/null; }

CFG_BUNDLE="$(cfg "['app']['bundle_id']")"
CFG_PROCESS="$(cfg "['app']['process_name']")"
RELEASE_ENV="$(cfg "['release_console_env']")"
CAPTURE_DIR="$(eval echo "$(cfg "['default_capture_dir']")")"
SIM_PREDICATE_TPL="$(cfg "['simulator_predicate']")"
mkdir -p "$CAPTURE_DIR"

usage() {
  sed -n '2,4p' "${BASH_SOURCE[0]}"
  cat <<'EOF'
  detect                                   inventory: devices, sims, installed bundle, tools
  device-console [--duration N] [--out F] [--env K=V] [--bundle ID] [--release-echo] [--keep-alive]
                                           relaunch the app owning its stdout+stderr
                                           (a Debug build's console output lives here)
  device-syslog  [--duration N] [--out F] [--process NAME] [--keep-alive]
                                           unified-log relay (blind to a Debug build's console);
                                           --process keeps one process's lines (idevicesyslog -p)
  sim            [--duration N] [--out F]  booted simulator log stream
EOF
  exit 1
}

need_bundle() {
  [ -n "${BUNDLE:-}${CFG_BUNDLE}" ] || {
    echo "no bundle id: set app.bundle_id in config.json, or pass --bundle" >&2; exit 2; }
}

coredevice_id() {
  # Usable states are "connected" (cabled) and "available (paired)" (network pairing);
  # device names may contain spaces, so pull the UUID by shape, not by column.
  xcrun devicectl list devices 2>/dev/null \
    | grep -E 'connected|available \(paired\)' \
    | grep -oE '[A-F0-9]{8}-[A-F0-9]{4}-[A-F0-9]{4}-[A-F0-9]{4}-[A-F0-9]{12}' | head -1
}
raw_udid() { command -v idevice_id >/dev/null && idevice_id -l 2>/dev/null | head -1 || true; }

detect() {
  echo "== devicectl devices (CoreDevice UUIDs; 'connected' rows are usable):"
  xcrun devicectl list devices 2>/dev/null | sed -n '1,8p' || echo "  devicectl unavailable"
  echo "== raw UDIDs (libimobiledevice id system - different from the UUIDs above):"
  command -v idevice_id >/dev/null && idevice_id -l || echo "  idevice_id not installed (brew install libimobiledevice)"
  echo "== tools:"
  for t in xcrun idevicesyslog idevice_id; do
    printf '  %-16s %s\n' "$t" "$(command -v $t || echo 'MISSING')"
  done
  local dev; dev="$(coredevice_id)"
  if [ -n "$dev" ] && [ -n "$CFG_BUNDLE" ]; then
    echo "== $CFG_BUNDLE on $dev:"
    xcrun devicectl device info apps --device "$dev" --bundle-id "$CFG_BUNDLE" 2>/dev/null \
      | grep -F "$CFG_BUNDLE" || echo "  not installed"
  elif [ -n "$dev" ]; then
    echo "== app.bundle_id is not set in config.json; nothing to look up on $dev"
  fi
  if [ -n "$dev" ] && [ -n "$CFG_PROCESS" ]; then
    echo "== processes matching '$CFG_PROCESS':"
    xcrun devicectl device info processes --device "$dev" 2>/dev/null \
      | grep -F "$CFG_PROCESS" | head -5 || echo "  not running"
  fi
  echo "== booted simulators:"
  xcrun simctl list devices booted 2>/dev/null | grep -i booted || echo "  none"
}

# Parse common flags after the subcommand.
DURATION=0; OUT=""; BUNDLE=""; KEEP_ALIVE=0; ENV_VARS=""; PROCESS=""
parse_flags() {
  while [ $# -gt 0 ]; do
    case "$1" in
      --duration) DURATION="$2"; shift 2 ;;
      --out) OUT="$2"; shift 2 ;;
      --bundle) BUNDLE="$2"; shift 2 ;;
      --env) ENV_VARS="${ENV_VARS:+$ENV_VARS,}$2"; shift 2 ;;
      --release-echo)
        [ -n "$RELEASE_ENV" ] || { echo "config.release_console_env is empty: this app has no echo switch" >&2; exit 2; }
        ENV_VARS="${ENV_VARS:+$ENV_VARS,}${RELEASE_ENV}=1"; shift ;;
      --keep-alive) KEEP_ALIVE=1; shift ;;
      --process) PROCESS="$2"; shift 2 ;;
      *) echo "unknown flag: $1" >&2; usage ;;
    esac
  done
}

device_console() {
  need_bundle
  local dev; dev="$(coredevice_id)"
  [ -z "$dev" ] && { echo "no connected device (xcrun devicectl list devices)"; exit 2; }
  local bundle="${BUNDLE:-$CFG_BUNDLE}"
  local out="${OUT:-$CAPTURE_DIR/device_console_$(date +%Y%m%d_%H%M%S).log}"
  local envflag=()
  [ -n "$ENV_VARS" ] && envflag=(--environment-variables "{$(python3 -c "
import sys
pairs=[p.split('=',1) for p in '$ENV_VARS'.split(',')]
print(','.join(f'\"{k}\":\"{v}\"' for k,v in pairs))")}")
  echo "NOTE: this relaunches $bundle (terminates any running instance, including an"
  echo "Xcode-launched one), and stopping this stream will SIGKILL the app."
  echo "capture -> $out"
  if [ "$DURATION" -gt 0 ]; then
    ( xcrun devicectl device process launch --console --terminate-existing \
        --device "$dev" ${envflag[@]+"${envflag[@]}"} "$bundle" > "$out" 2>&1 & echo $! > "$out.pid"
      sleep "$DURATION"; kill "$(cat "$out.pid")" 2>/dev/null || true; rm -f "$out.pid" )
    echo "done ($DURATION s): $(wc -l < "$out" | tr -d " ") lines"
  elif [ "$KEEP_ALIVE" = "1" ]; then
    nohup xcrun devicectl device process launch --console --terminate-existing \
      --device "$dev" ${envflag[@]+"${envflag[@]}"} "$bundle" > "$out" 2>&1 &
    echo $! > "$out.pid"
    echo "streaming in background (pid $(cat "$out.pid")); stop with: kill \$(cat $out.pid)  [kills the app too]"
  else
    # A logger's console lines are NSLog output, i.e. the app's stderr, which devicectl forwards
    # on its own stderr: without 2>&1 the file gets none of them.
    xcrun devicectl device process launch --console --terminate-existing \
      --device "$dev" ${envflag[@]+"${envflag[@]}"} "$bundle" 2>&1 | tee "$out"
  fi
}

device_syslog() {
  command -v idevicesyslog >/dev/null || { echo "idevicesyslog missing: brew install libimobiledevice"; exit 2; }
  local udid; udid="$(raw_udid)"
  [ -z "$udid" ] && { echo "no paired device (idevice_id -l)"; exit 2; }
  local out="${OUT:-$CAPTURE_DIR/device_syslog_$(date +%Y%m%d_%H%M%S).log}"
  local proc="${PROCESS:-$CFG_PROCESS}"
  local pflag=()
  [ -n "$proc" ] && pflag=(-p "$proc")
  echo "REMINDER: a Debug build's console output will NOT appear here (it is the process's"
  echo "stderr, which never enters the unified log). This channel carries your logger only for"
  echo "a Release build with its os_log echo switched on."
  echo "capture -> $out  (raw; filter afterwards with analyze.sh - a grep stage in the pipe has dropped every line)"
  if [ "$DURATION" -gt 0 ]; then
    ( idevicesyslog -u "$udid" ${pflag[@]+"${pflag[@]}"} > "$out" 2>&1 & echo $! > "$out.pid"
      sleep "$DURATION"; kill "$(cat "$out.pid")" 2>/dev/null || true; rm -f "$out.pid" )
    echo "done ($DURATION s): $(wc -l < "$out" | tr -d " ") lines"
  elif [ "$KEEP_ALIVE" = "1" ]; then
    nohup idevicesyslog -u "$udid" ${pflag[@]+"${pflag[@]}"} > "$out" 2>&1 &
    echo $! > "$out.pid"
    echo "relaying in background (pid $(cat "$out.pid")); stop with: kill \$(cat $out.pid)"
  else
    idevicesyslog -u "$udid" ${pflag[@]+"${pflag[@]}"} 2>&1 | tee "$out"
  fi
}

sim_stream() {
  local proc="${PROCESS:-$CFG_PROCESS}"
  [ -z "$proc" ] && { echo "no process name: set app.process_name in config.json, or pass --process" >&2; exit 2; }
  local predicate="${SIM_PREDICATE_TPL//\{process\}/$proc}"
  local out="${OUT:-$CAPTURE_DIR/sim_$(date +%Y%m%d_%H%M%S).log}"
  echo "capture -> $out  (predicate: $predicate)"
  if [ "$DURATION" -gt 0 ]; then
    xcrun simctl spawn booted log stream --style compact --predicate "$predicate" \
      --timeout "${DURATION}s" > "$out" 2>&1 || true
    echo "done ($DURATION s): $(wc -l < "$out" | tr -d " ") lines"
  else
    xcrun simctl spawn booted log stream --style compact --predicate "$predicate" | tee "$out"
  fi
}

CMD="${1:-}"; shift || true
case "$CMD" in
  detect) detect ;;
  device-console) parse_flags "$@"; device_console ;;
  device-syslog)  parse_flags "$@"; device_syslog ;;
  sim)            parse_flags "$@"; sim_stream ;;
  *) usage ;;
esac
