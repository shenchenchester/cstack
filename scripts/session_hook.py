#!/usr/bin/env python3
"""Register, re-point or remove the session-start hooks that run scripts/session-update.sh.

    python3 scripts/session_hook.py install --script /abs/path/to/scripts/session-update.sh
    python3 scripts/session_hook.py remove
    python3 scripts/session_hook.py status

`install.sh` calls this; you should not need to. It edits up to two files — Claude
Code's `~/.claude/settings.json` (`CLAUDE_CONFIG_DIR` honoured) and Cursor's
`~/.cursor/hooks.json` (`CURSOR_CONFIG_DIR` honoured) — and only the entry that is
ours. Ours is identified by the script's file name in the command, because both
hosts rewrite these files and drop unknown keys, so a tag would not survive.

`--runtime claude|cursor|both` (default: both) limits which hosts are touched. A host
whose config directory does not exist is skipped, so a Cursor-only machine is not
forced into a Claude layout and the other way around.

The Claude Code hook:

    {"type": "command", "async": true,
     "command": "[ -f \\"<script>\\" ] && bash \\"<script>\\" || true"}

The Cursor hook (user-level `hooks.json`, schema version 1):

    {"command": "[ -f \\"<script>\\" ] && bash \\"<script>\\" || true"}

`async` on Claude so the session never waits on it; Cursor's sessionStart already
returns as soon as the command exits, and session-update.sh backgrounds itself
before any pull. The guard so a clone that was moved or deleted leaves a hook that
does nothing rather than one that errors on every start. Re-running `install` from
a different checkout re-points the entry in place.

Safety: a settings/hooks file that does not parse is never written (exit 3). A write
that changes the file leaves `<name>.bak.<timestamp>` beside it; a no-op write leaves
nothing. Writes go through a temp file and rename.
"""

import argparse
import json
import os
import sys
import time

MARKER = "session-update.sh"


def claude_settings_path():
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")
    return os.path.join(config_dir, "settings.json")


def cursor_hooks_path():
    config_dir = os.environ.get("CURSOR_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".cursor")
    return os.path.join(config_dir, "hooks.json")


def load(path):
    if not os.path.exists(path):
        return {}, None
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    try:
        return (json.loads(text) if text.strip() else {}), text
    except ValueError as exc:
        print("refusing to touch %s: it does not parse as JSON (%s)" % (path, exc), file=sys.stderr)
        sys.exit(3)


def save(path, doc, before):
    if before is not None and json.loads(before or "{}") == doc:
        return False
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if before is not None:
        with open("%s.bak.%d" % (path, int(time.time())), "w", encoding="utf-8") as fh:
            fh.write(before)
    tmp = "%s.tmp.%d" % (path, os.getpid())
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(doc, indent=2, ensure_ascii=False) + "\n")
    os.replace(tmp, path)
    return True


def command_for(script):
    return '[ -f "%s" ] && bash "%s" || true' % (script, script)


def is_ours(hook):
    return isinstance(hook, dict) and MARKER in str(hook.get("command", ""))


def claude_entries(doc):
    hooks = doc.get("hooks")
    if not isinstance(hooks, dict):
        return []
    session_start = hooks.get("SessionStart")
    return session_start if isinstance(session_start, list) else []


def do_install_claude(path, script):
    script = os.path.abspath(script)
    if not os.path.isfile(script):
        print("no such script: %s" % script, file=sys.stderr)
        return 2
    doc, before = load(path)
    hooks = doc.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        print("refusing to touch %s: `hooks` is not an object" % path, file=sys.stderr)
        return 3
    session_start = hooks.setdefault("SessionStart", [])
    if not isinstance(session_start, list):
        print("refusing to touch %s: `hooks.SessionStart` is not a list" % path, file=sys.stderr)
        return 3
    ours = [e for e in session_start
            if isinstance(e, dict) and any(is_ours(h) for h in e.get("hooks", []))]
    if ours:
        for extra in ours[1:]:
            session_start.remove(extra)
        for hook in ours[0]["hooks"]:
            if is_ours(hook):
                hook["type"] = "command"
                hook["async"] = True
                hook["command"] = command_for(script)
        verb = "updated"
    else:
        session_start.append({"hooks": [{"type": "command", "async": True,
                                         "command": command_for(script)}]})
        verb = "registered"
    changed = save(path, doc, before)
    print("  hook  Claude SessionStart %s in %s" % (verb if changed else "unchanged", path))
    return 0


def do_remove_claude(path):
    doc, before = load(path)
    if before is None:
        print("  hook  Claude nothing registered (%s does not exist)" % path)
        return 0
    session_start = claude_entries(doc)
    for entry in list(session_start):
        if not isinstance(entry, dict):
            continue
        entry["hooks"] = [h for h in entry.get("hooks", []) if not is_ours(h)]
        if not entry["hooks"]:
            session_start.remove(entry)
    if isinstance(doc.get("hooks"), dict):
        if not session_start:
            doc["hooks"].pop("SessionStart", None)
        if not doc["hooks"]:
            doc.pop("hooks")
    changed = save(path, doc, before)
    print("  hook  Claude SessionStart %s in %s"
          % ("removed" if changed else "was not registered", path))
    return 0


def do_status_claude(path):
    doc, _ = load(path)
    found = [h["command"] for e in claude_entries(doc) if isinstance(e, dict)
             for h in e.get("hooks", []) if is_ours(h)]
    if not found:
        print("Claude: not registered in %s" % path)
        return 1
    for cmd in found:
        print("Claude: %s" % cmd)
    return 0


def do_install_cursor(path, script):
    script = os.path.abspath(script)
    if not os.path.isfile(script):
        print("no such script: %s" % script, file=sys.stderr)
        return 2
    doc, before = load(path)
    if "version" not in doc:
        doc["version"] = 1
    hooks = doc.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        print("refusing to touch %s: `hooks` is not an object" % path, file=sys.stderr)
        return 3
    session_start = hooks.setdefault("sessionStart", [])
    if not isinstance(session_start, list):
        print("refusing to touch %s: `hooks.sessionStart` is not a list" % path, file=sys.stderr)
        return 3
    ours = [h for h in session_start if is_ours(h)]
    if ours:
        for extra in ours[1:]:
            session_start.remove(extra)
        ours[0]["command"] = command_for(script)
        # Cursor command hooks are type "command" by default; keep the key only if
        # something else already set it, and drop Claude-only fields if a prior
        # mistaken write left them here.
        ours[0].pop("async", None)
        if "type" in ours[0] and ours[0]["type"] != "command":
            ours[0]["type"] = "command"
        verb = "updated"
    else:
        session_start.append({"command": command_for(script)})
        verb = "registered"
    changed = save(path, doc, before)
    print("  hook  Cursor sessionStart %s in %s" % (verb if changed else "unchanged", path))
    return 0


def do_remove_cursor(path):
    doc, before = load(path)
    if before is None:
        print("  hook  Cursor nothing registered (%s does not exist)" % path)
        return 0
    hooks = doc.get("hooks")
    if not isinstance(hooks, dict):
        print("  hook  Cursor was not registered in %s" % path)
        return 0
    session_start = hooks.get("sessionStart")
    if not isinstance(session_start, list):
        print("  hook  Cursor was not registered in %s" % path)
        return 0
    before_len = len(session_start)
    hooks["sessionStart"] = [h for h in session_start if not is_ours(h)]
    if not hooks["sessionStart"]:
        hooks.pop("sessionStart", None)
    if not hooks:
        doc.pop("hooks", None)
    changed = save(path, doc, before)
    print("  hook  Cursor sessionStart %s in %s"
          % ("removed" if changed or before_len else "was not registered", path))
    return 0


def do_status_cursor(path):
    doc, _ = load(path)
    hooks = doc.get("hooks") if isinstance(doc.get("hooks"), dict) else {}
    session_start = hooks.get("sessionStart") if isinstance(hooks.get("sessionStart"), list) else []
    found = [h["command"] for h in session_start if is_ours(h)]
    if not found:
        print("Cursor: not registered in %s" % path)
        return 1
    for cmd in found:
        print("Cursor: %s" % cmd)
    return 0


def resolve_runtimes(requested):
    """Yield (name, path, install, remove, status) for hosts that exist or should be created.

    Claude's settings.json and Cursor's hooks.json are created on install when the
    parent config directory already exists — that is the signal the runtime is in
    use. status/remove still report on a missing file rather than inventing one.
    """
    mapping = {
        "claude": ("Claude", claude_settings_path(), do_install_claude, do_remove_claude, do_status_claude,
                   os.path.dirname(claude_settings_path())),
        "cursor": ("Cursor", cursor_hooks_path(), do_install_cursor, do_remove_cursor, do_status_cursor,
                   os.path.dirname(cursor_hooks_path())),
    }
    names = ("claude", "cursor") if requested == "both" else (requested,)
    for name in names:
        label, path, install, remove, status, config_dir = mapping[name]
        yield name, label, path, install, remove, status, config_dir


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="action")
    sub.required = True
    inst = sub.add_parser("install", help="register the hook, or re-point an existing one")
    inst.add_argument("--script", required=True, help="absolute path to scripts/session-update.sh")
    inst.add_argument("--runtime", default="both", choices=("claude", "cursor", "both"),
                      help="which host(s) to register (default: both)")
    rem = sub.add_parser("remove", help="remove our hook and nothing else")
    rem.add_argument("--runtime", default="both", choices=("claude", "cursor", "both"),
                     help="which host(s) to clear (default: both)")
    st = sub.add_parser("status", help="print the registered command; exit 1 if none")
    st.add_argument("--runtime", default="both", choices=("claude", "cursor", "both"),
                    help="which host(s) to report (default: both)")
    args = ap.parse_args()

    rc = 0
    any_done = False
    status_found = False
    for name, label, path, install, remove, status, config_dir in resolve_runtimes(args.runtime):
        if args.action == "install":
            if not os.path.isdir(config_dir):
                print("  hook  %s skipped — %s does not exist" % (label, config_dir))
                continue
            result = install(path, args.script)
            any_done = True
            if result:
                rc = result
        elif args.action == "remove":
            result = remove(path)
            any_done = True
            if result:
                rc = result
        else:
            result = status(path)
            any_done = True
            if result == 0:
                status_found = True
            elif rc == 0:
                rc = result
    if args.action == "status" and status_found:
        return 0
    if args.action == "install" and not any_done:
        print("no runtime config directory found; install Claude Code or Cursor first",
              file=sys.stderr)
        return 1
    return rc


if __name__ == "__main__":
    sys.exit(main())
