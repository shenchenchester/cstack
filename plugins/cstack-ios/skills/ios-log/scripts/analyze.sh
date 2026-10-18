#!/bin/bash
# Analysis over a captured log file, keyed off the line format in ../config.json.
# Default line shape: [time tz][pid:tid*][level][tag][file,func,line] msg
# devicectl captures prefix each line with "date time Process[pid:tid] " - handled here.
# Subcommands: summary | errors | tag | window | main
set -euo pipefail

SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG="$SKILL_DIR/config.json"
cfg() { python3 -c "import json;print(json.load(open('$CONFIG'))['$1'])"; }

LINE_RE="$(cfg log_line_regex)"
LEVEL_RE="$(cfg level_regex)"
TAG_RE="$(cfg tag_regex)"
ERROR_LEVELS="$(cfg error_levels)"
MAIN_RE="$(cfg main_thread_regex)"

usage() {
  cat <<'EOF'
usage: analyze.sh <summary|errors|tag|window|main> <file> [args]
  summary <file>                 counts by tag and level, time span, top talkers
  errors  <file>                 error and warning lines (config.error_levels)
  tag     <file> <SUBSTRING>     one module's trace (tag substring match)
  window  <file> <FROM> <TO>     slice by wall-clock HH:MM:SS
  main    <file>                 main-thread lines only
EOF
  exit 1
}

CMD="${1:-}"; FILE="${2:-}"
[ -z "$CMD" ] && usage
[ -f "${FILE:-/nonexistent}" ] || { echo "file not found: ${FILE:-<missing>}" >&2; usage; }

case "$CMD" in
  summary)
    total=$(wc -l < "$FILE" | tr -d " ")
    matched=$(grep -cE "$LINE_RE" "$FILE" || true)
    echo "lines: $total total, $matched in the configured log format"
    if [ "$matched" = "0" ]; then
      echo "NO lines in that format. Two causes, in order of likelihood:"
      echo "  1. a channel mismatch - check the matrix in ../SKILL.md"
      echo "     (Debug build => device-console only; Release => needs the os_log echo)"
      echo "  2. your logger prints a different header - set log_line_regex in config.json"
      echo "Sample of what IS here:"; grep -vE '^\s*$' "$FILE" | head -3
      exit 0
    fi
    echo "== time span:"
    grep -oE '\[[0-9]{4}-[0-9]{2}-[0-9]{2} [^]]*[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]+' "$FILE" \
      | sed 's/^\[//' | sed -n '1p;$p'
    echo "== by level:"
    grep -oE "$LEVEL_RE" "$FILE" | sort | uniq -c | sort -rn
    echo "== top 20 tags:"
    perl -ne "print \"\$1\n\" if /$TAG_RE/" "$FILE" | sort | uniq -c | sort -rn | head -20
    ;;
  errors)
    grep -E "\]\[($ERROR_LEVELS)\]\[" "$FILE" || echo "no lines at levels: $ERROR_LEVELS"
    ;;
  tag)
    T="${3:-}"; [ -z "$T" ] && usage
    grep -E "\]\[[A-Z]\]\[[^]]*${T}[^]]*\]" "$FILE" || echo "no lines for tag ~ '$T'"
    ;;
  window)
    FROM="${3:-}"; TO="${4:-}"; { [ -z "$FROM" ] || [ -z "$TO" ]; } && usage
    awk -v f="$FROM" -v t="$TO" '
      match($0, /[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]+/) {
        ts = substr($0, RSTART, 8)
        if (ts >= f && ts <= t) print
      }' "$FILE"
    ;;
  main)
    grep -E "$MAIN_RE" "$FILE" || echo "no main-thread-marked lines"
    ;;
  *) usage ;;
esac
