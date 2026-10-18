#!/usr/bin/env python3
"""Keep an installed devlock in step with the skill. Run at every agent session start (Claude Code SessionStart,
Cursor sessionStart); silent and fast - the reinstall itself runs detached.

  refresh.py            # Claude Code: prints nothing
  refresh.py --cursor   # Cursor: prints {} (its sessionStart reads JSON)
  refresh.py --dry-run  # say what it would do, then exit

Never installs on a Mac where devlock is not installed (that changes system config and needs the user's yes);
the skill's docs and the shared device layer suggest the install instead.

Which copy of the skill counts: a symlink install (~/.claude/skills or ~/.cursor/skills-cursor, which a `git pull`
updates) wins, else the newest version in a plugin marketplace cache, else the directory it was installed from.
It reinstalls from that copy when its content fingerprint differs from the installed one, or the copy moved
(a marketplace update lands in a new version directory).
"""
import glob, hashlib, json, os, re, subprocess, sys, time

HOME = os.path.expanduser("~")
DL_HOME = os.environ.get("DEVLOCK_HOME", os.path.join(HOME, ".devlock"))
SKILL = "ios-build-device-lock"
# Everything the installed runtime is made of: the copied scripts, the keep-awake loop (run from the skill dir)
# and what it imports, and the configs.
FINGERPRINTED = ["scripts/devlock", "scripts/hook.py", "scripts/cleanup.py", "scripts/idmap.py", "scripts/keepawaked.py",
                 "scripts/xcrun-shim", "scripts/session-env.sh", "scripts/refresh.py", "scripts/install.py",
                 "scripts/keepawake.py", "config.json", "../_shared/scripts/ios_device.py", "../_shared/ios_device.json"]


def fingerprint(skill_dir):
    h = hashlib.sha256()
    for rel in FINGERPRINTED:
        try:
            with open(os.path.join(skill_dir, rel), "rb") as f:
                h.update(rel.encode() + b"\0" + f.read() + b"\0")
        except OSError:
            h.update(rel.encode() + b"\0missing\0")
    return h.hexdigest()[:12]


def _semver(path):
    m = re.search(r"/(\d+)\.(\d+)\.(\d+)/skills/", path)
    return tuple(int(x) for x in m.groups()) if m else (0, 0, 0)


def candidate(installed_from=None):
    for link in (os.path.join(HOME, ".claude", "skills", SKILL), os.path.join(HOME, ".cursor", "skills-cursor", SKILL)):
        if os.path.exists(os.path.join(link, "scripts", "install.py")):
            return os.path.realpath(link)
    cached = glob.glob(os.path.join(HOME, ".claude", "plugins", "cache", "*", "cstack-ios", "*", "skills", SKILL))
    cached = [c for c in cached if os.path.exists(os.path.join(c, "scripts", "install.py"))]
    if cached:
        return os.path.realpath(max(cached, key=_semver))
    if installed_from and os.path.exists(os.path.join(installed_from, "scripts", "install.py")):
        return installed_from
    return None


def main():
    dry = "--dry-run" in sys.argv
    try:
        conf = json.load(open(os.path.join(DL_HOME, "config.json")))
    except Exception:
        return "devlock not installed: nothing to refresh"
    src = candidate(conf.get("skill_dir"))
    if not src:
        return "no copy of the skill found"
    want, have = fingerprint(src), conf.get("fingerprint")
    if want == have and src == conf.get("skill_dir"):
        return f"up to date ({have})"
    why = f"{have} -> {want}" + ("" if src == conf.get("skill_dir") else f", from {src}")
    if dry:
        return f"would reinstall: {why}"
    lock = os.path.join(DL_HOME, "refresh.lock")
    try:  # one refresh at a time; a lock older than 10 min is left over from a crash
        if os.path.isdir(lock) and time.time() - os.path.getmtime(lock) > 600:
            os.rmdir(lock)
        os.mkdir(lock)
    except OSError:
        return "a refresh is already running"
    log = open(os.path.join(DL_HOME, "refresh.log"), "a")
    log.write(time.strftime("%F %T ") + f"reinstall: {why}\n")
    log.flush()
    subprocess.Popen(["/bin/sh", "-c", f'"{sys.executable}" "$1" --quiet; rmdir "$2"', "sh",
                      os.path.join(src, "scripts", "install.py"), lock],
                     stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
    return f"reinstalling: {why}"


if __name__ == "__main__":
    try:
        msg = main()
    except Exception as e:  # a session must never fail to start because of this
        msg = f"error: {e!r}"
    if "--dry-run" in sys.argv:
        print(msg)
    elif "--cursor" in sys.argv:
        print("{}")
