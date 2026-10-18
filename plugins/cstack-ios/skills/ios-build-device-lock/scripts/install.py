#!/usr/bin/env python3
"""Install / check / remove devlock on this Mac. Idempotent; rerun after updating the skill.

  python3 scripts/install.py            # install or update
  python3 scripts/install.py --check    # report what is installed, exit 1 if something is missing
  python3 scripts/install.py --uninstall

Installs into DEVLOCK_HOME (~/.devlock): bin/ (scripts copied from this skill), locks/, awake/, config.json (with the
content fingerprint of the skill it came from; `refresh.py` reinstalls at session start when the skill changed).
Links `devlock` and the `xcrun` shim into ~/.local/bin, registers the keep-awake launchd agent, and wires
the hooks into Claude Code (~/.claude/settings.json: PreToolUse, Stop, SessionStart) and Cursor
(~/.cursor/hooks.json: beforeShellExecution, stop) when those config dirs exist. Our hook entries are
recognised by the `/.devlock/bin/` (or legacy `/.claude/devlock/`) path in their command; others are untouched.
"""
import argparse, json, os, shutil, subprocess, sys

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
from refresh import fingerprint  # noqa: E402

SKILL = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
CONF = json.load(open(os.path.join(SKILL, "config.json")))
RUNTIME = ["devlock", "hook.py", "cleanup.py", "idmap.py", "keepawaked.py", "xcrun-shim", "session-env.sh", "refresh.py"]
# keepawake.py is NOT copied: it imports the plugin's skills/_shared device layer, so it runs from the skill dir
OURS = ("/.devlock/bin/", "/.claude/devlock/")


def paths(home):
    dl = os.environ.get("DEVLOCK_HOME") if home == os.path.expanduser("~") and os.environ.get("DEVLOCK_HOME") else os.path.join(home, ".devlock")
    return {
        "home": home, "dl": dl, "bin": os.path.join(dl, "bin"),
        "localbin": os.path.join(home, ".local", "bin"),
        "plist": os.path.join(home, "Library", "LaunchAgents", CONF["launchd_label"] + ".plist"),
        "claude": os.path.join(home, ".claude", "settings.json"),
        "cursor": os.path.join(os.environ.get("CURSOR_CONFIG_DIR") or os.path.join(home, ".cursor"), "hooks.json"),
        "legacy": os.path.join(home, ".claude", "devlock"),
    }


def is_ours(entry):
    blob = json.dumps(entry)
    return any(o in blob for o in OURS)


def load(path):
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def save(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".devlock.tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, path)


def claude_hooks(p, remove=False):
    if not os.path.isdir(os.path.dirname(p["claude"])):
        return "skipped (no ~/.claude)"
    s = load(p["claude"])
    hooks = s.setdefault("hooks", {})
    for ev in ("PreToolUse", "Stop", "SessionStart"):
        hooks[ev] = [h for h in hooks.get(ev, []) if not is_ours(h)]
    if not remove:
        hook = f'python3 "{p["bin"]}/hook.py"'
        hooks["PreToolUse"].append({"matcher": "Bash|" + CONF["claude_mcp_build_matcher"],
                                    "hooks": [{"type": "command", "command": hook, "timeout": 10}]})
        hooks["Stop"].append({"hooks": [{"type": "command", "command": hook, "timeout": 10}]})
        hooks["SessionStart"].append({"hooks": [{"type": "command", "command": f'bash "{p["bin"]}/session-env.sh"', "timeout": 5}]})
        hooks["SessionStart"].append({"hooks": [{"type": "command", "command": f'python3 "{p["bin"]}/refresh.py"',
                                                 "timeout": 10, "async": True}]})
    for ev in list(hooks):
        if not hooks[ev]:
            hooks.pop(ev)
    save(p["claude"], s)
    return "removed" if remove else "PreToolUse + Stop + SessionStart (identity, refresh)"


def cursor_hooks(p, remove=False):
    if not os.path.isdir(os.path.dirname(p["cursor"])):
        return "skipped (no ~/.cursor)"
    s = load(p["cursor"])
    s.setdefault("version", 1)
    hooks = s.setdefault("hooks", {})
    for ev in ("beforeShellExecution", "stop", "sessionStart"):
        hooks[ev] = [h for h in hooks.get(ev, []) if not is_ours(h)]
    if not remove:
        hook = f'python3 "{p["bin"]}/hook.py"'
        hooks["beforeShellExecution"].append({"command": hook, "timeout": 10})
        hooks["stop"].append({"command": hook, "timeout": 10})
        hooks["sessionStart"].append({"command": f'python3 "{p["bin"]}/refresh.py" --cursor', "timeout": 10})
    for ev in list(hooks):
        if not hooks[ev]:
            hooks.pop(ev)
    save(p["cursor"], s)
    return "removed" if remove else "beforeShellExecution + stop + sessionStart (refresh)"


def plist_xml(p):
    node = shutil.which("node")
    path = ":".join(x for x in [p["localbin"], os.path.dirname(node) if node else None, "/opt/homebrew/bin",
                                "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"] if x)
    out = os.path.join(p["dl"], "awake", "daemon.stdout")
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{CONF["launchd_label"]}</string>
  <key>ProgramArguments</key>
  <array><string>/usr/bin/python3</string><string>{p["bin"]}/keepawaked.py</string></array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>{path}</string>
    <key>LANG</key><string>en_US.UTF-8</string>
    <key>DEVLOCK_HOME</key><string>{p["dl"]}</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>30</integer>
  <key>StandardOutPath</key><string>{out}</string>
  <key>StandardErrorPath</key><string>{out}</string>
</dict>
</plist>
"""


def launchctl(*args):
    return subprocess.run(["launchctl", *args], capture_output=True, text=True)


def daemon(p, remove=False):
    """(Re)load the keep-awake agent. bootout returns before launchd has removed the service, and a bootstrap in that
    window fails ("Input/output error") - which once left keep-awake down for hours - so wait it out, retry, verify."""
    import time
    domain, label = f"gui/{os.getuid()}", CONF["launchd_label"]
    loaded = lambda: launchctl("print", f"{domain}/{label}").returncode == 0
    launchctl("bootout", f"{domain}/{label}")
    for _ in range(40):
        if not loaded():
            break
        time.sleep(0.5)
    if remove:
        if os.path.exists(p["plist"]):
            os.remove(p["plist"])
        return "removed"
    os.makedirs(os.path.dirname(p["plist"]), exist_ok=True)
    with open(p["plist"], "w") as f:
        f.write(plist_xml(p))
    err = ""
    for _ in range(5):
        r = launchctl("bootstrap", domain, p["plist"])
        if r.returncode == 0 or loaded():
            return "loaded"
        err = r.stderr.strip()
        time.sleep(2)
    return f"FAILED to load: {err}"


def link(src, dst):
    if os.path.islink(dst) or os.path.exists(dst):
        if os.path.islink(dst) and os.path.realpath(dst) == os.path.realpath(src):
            return
        if not os.path.islink(dst):
            raise SystemExit(f"refusing to replace real file {dst}")
        os.remove(dst)
    os.symlink(src, dst)


def migrate_legacy(p):
    """Move locks/state from the pre-skill location (~/.claude/devlock), then point it at bin/."""
    old = p["legacy"]
    if not os.path.isdir(old) or os.path.islink(old):
        return None
    moved = 0
    for sub in ("locks", "awake"):
        src = os.path.join(old, sub)
        if not os.path.isdir(src):
            continue
        dst = os.path.join(p["dl"], sub)
        os.makedirs(dst, exist_ok=True)
        for name in os.listdir(src):
            target = os.path.join(dst, name)
            if not os.path.exists(target):
                os.rename(os.path.join(src, name), target)  # a lock dir moves atomically, holds survive
                moved += 1
    backup = old + ".pre-skill"
    shutil.rmtree(backup, ignore_errors=True)
    os.rename(old, backup)
    os.symlink(p["bin"], old)
    return f"migrated {moved} entries from {old} (old scripts kept in {backup})"


def install(p, args):
    failed = False
    for d in ("bin", "locks", "awake"):
        os.makedirs(os.path.join(p["dl"], d), exist_ok=True)
    for f in RUNTIME:
        dst = os.path.join(p["bin"], f)
        shutil.copy2(os.path.join(SKILL, "scripts", f), dst + ".new")
        os.chmod(dst + ".new", 0o755)
        os.replace(dst + ".new", dst)
    keep = os.path.join(SKILL, "scripts", "keepawake.py")
    version = fingerprint(SKILL)
    # build_scripts / device_scripts travel with the runtime because hook.py reads them from here:
    # the hook runs out of DEVLOCK_HOME and must not depend on the skill directory still being there.
    save(os.path.join(p["dl"], "config.json"), {"fingerprint": version, "skill_dir": SKILL, "keepawake_script": keep,
                                                 "awake_interval_s": CONF["awake_interval_s"],
                                                 "build_scripts": CONF.get("build_scripts", ""),
                                                 "device_scripts": CONF.get("device_scripts", "")})
    report = [f"runtime  {p['bin']} (devlock {version}, from {SKILL})"]
    m = migrate_legacy(p)
    if m:
        report.append("legacy   " + m)
    os.makedirs(p["localbin"], exist_ok=True)
    link(os.path.join(p["bin"], "devlock"), os.path.join(p["localbin"], "devlock"))
    link(os.path.join(p["bin"], "xcrun-shim"), os.path.join(p["localbin"], "xcrun"))
    report.append(f"links    {p['localbin']}/devlock, {p['localbin']}/xcrun (shim)")
    if p["localbin"] not in os.environ.get("PATH", "").split(":"):
        report.append(f"WARNING  {p['localbin']} is not on PATH — add it ahead of /usr/bin, or the xcrun shim never runs")
    if not os.path.exists(keep):
        report.append(f"WARNING  keep-awake loop not found at {keep}")
    if not os.path.exists(os.path.join(SKILL, "..", "_shared", "scripts", "ios_device.py")):
        report.append("WARNING  the plugin's skills/_shared device layer is missing; keep-awake cannot run")
    if not args.no_launchd:
        d = daemon(p)
        report.append("daemon   " + d)
        if d != "loaded":
            failed = True
    if not args.no_hooks:
        report.append("claude   " + claude_hooks(p))
        report.append("cursor   " + cursor_hooks(p))
    if failed or not args.quiet:
        print("\n".join(report))
    if failed:
        sys.exit(1)


def uninstall(p, args):
    out = []
    if not args.no_launchd:
        out.append("daemon   " + daemon(p, remove=True))
    if not args.no_hooks:
        out.append("claude   " + claude_hooks(p, remove=True))
        out.append("cursor   " + cursor_hooks(p, remove=True))
    for n in ("devlock", "xcrun"):
        f = os.path.join(p["localbin"], n)
        if os.path.islink(f) and os.path.realpath(f).startswith(os.path.realpath(p["bin"])):
            os.remove(f)
    out.append(f"links    removed; {p['dl']} kept (locks, logs) — delete it yourself if you want it gone")
    print("\n".join(out))


def check(p, args):
    probs = []
    for f in RUNTIME:
        if not os.path.exists(os.path.join(p["bin"], f)):
            probs.append(f"missing {p['bin']}/{f}")
    for n in ("devlock", "xcrun"):
        f = os.path.join(p["localbin"], n)
        if not (os.path.islink(f) and os.path.realpath(f).startswith(os.path.realpath(p["bin"]))):
            probs.append(f"{f} is not our link")
    conf = load(os.path.join(p["dl"], "config.json"))
    inst, mine = conf.get("fingerprint"), fingerprint(SKILL)
    if inst != mine:
        probs.append(f"installed devlock {inst} != this skill {mine}: rerun install.py (or start a new agent session)")
    elif os.path.realpath(conf.get("skill_dir", "")) != os.path.realpath(SKILL):
        probs.append(f"installed from {conf.get('skill_dir')}, not this copy ({SKILL})")
    if not args.no_launchd and launchctl("print", f"gui/{os.getuid()}/{CONF['launchd_label']}").returncode != 0:
        probs.append("keep-awake daemon not loaded")
    if not args.no_hooks:
        if os.path.isdir(os.path.dirname(p["claude"])) and not any(is_ours(h) for h in load(p["claude"]).get("hooks", {}).get("PreToolUse", [])):
            probs.append("Claude hooks missing")
        if os.path.isdir(os.path.dirname(p["cursor"])) and not any(is_ours(h) for h in load(p["cursor"]).get("hooks", {}).get("beforeShellExecution", [])):
            probs.append("Cursor hooks missing")
    print("\n".join(probs) if probs else f"devlock {inst} installed and wired")
    sys.exit(1 if probs else 0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--uninstall", action="store_true")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--home", default=os.path.expanduser("~"), help="install under another home (tests)")
    ap.add_argument("--no-launchd", action="store_true", help="skip the keep-awake launchd agent")
    ap.add_argument("--no-hooks", action="store_true", help="skip Claude/Cursor hook wiring")
    ap.add_argument("--quiet", action="store_true", help="print only on failure (the session-start refresh)")
    args = ap.parse_args()
    p = paths(os.path.abspath(args.home))
    (uninstall if args.uninstall else check if args.check else install)(p, args)


if __name__ == "__main__":
    main()
