#!/usr/bin/env python3
"""Called by devlock right after a phone lock is taken: stop the previous holder's helpers that are
still bound to that phone, so the new holder starts from a clean device.

usage: cleanup.py <udid> <taker identity pid> <number of USB phones>

Stopped: Appium servers, WebDriverAgent runners (xcodebuild), keep-awake loops.
Left alone: processes started with DEVLOCK_INFRA=1 (the keep-awake daemon), the taker's own
(DEVLOCK_SESSION_PID or agent ancestor == taker), our own ancestors, remote-phone helpers (--remote),
and passive shared services someone else's session may depend on. A helper whose phone cannot be told from its command
line or environment (Appium) is only treated as bound to this phone when it is the only USB phone.
"""
import os, re, signal, subprocess, sys, time

AWAKE_DIR = os.path.join(os.environ.get("DEVLOCK_HOME", os.path.expanduser("~/.devlock")), "awake")
def agent_kind(pid, cmd):
    """"claude" / "cursor" for an agent process, else None: claude and cursor-agent by their executable (`ps -o comm=`,
    a full path that may contain spaces), Cursor desktop's extension host by its process title."""
    if cmd.startswith("Cursor Helper (Plugin): extension-host"):
        return "cursor"
    comm = subprocess.run(["ps", "-o", "comm=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    name = os.path.basename(os.path.realpath(comm)) if comm else ""  # a cloud runner may start cursor-agent as `agent` (symlink)
    return {"claude": "claude", "cursor-agent": "cursor"}.get(name)


def procs():
    """pid -> (ppid, command, env dict). `ps -E` appends the environment to the command."""
    out = subprocess.run(["ps", "-E", "-Ao", "pid=,ppid=,command="], capture_output=True, text=True).stdout
    plain = subprocess.run(["ps", "-Ao", "pid=,command="], capture_output=True, text=True).stdout
    cmds = {}
    for line in plain.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) == 2:
            cmds[int(parts[0])] = parts[1]
    res = {}
    for line in out.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) < 3:
            continue
        pid, ppid, rest = int(parts[0]), int(parts[1]), parts[2]
        cmd = cmds.get(pid, rest)
        env_part = rest[len(cmd):] if rest.startswith(cmd) else ""
        env = dict(m.groups() for m in re.finditer(r"(?:^|\s)([A-Z_][A-Z0-9_]*)=(\S*)", env_part))
        res[pid] = (ppid, cmd, env)
    return res


def identity(pid, table):
    """Same rule as devlock: DEVLOCK_SESSION_PID, else the nearest agent ancestor - a claude pid, or for a Cursor
    ancestor the conversation in CURSOR_CONVERSATION_ID ("conv:<id>"), which detached helpers inherit too."""
    env = table[pid][2]
    if env.get("DEVLOCK_SESSION_PID"):
        return env["DEVLOCK_SESSION_PID"]
    p = pid
    while p > 1 and p in table:
        cmd = table[p][1]
        kind = agent_kind(p, cmd)
        if kind:
            if kind == "cursor" and env.get("CURSOR_CONVERSATION_ID"):
                return "conv:" + env["CURSOR_CONVERSATION_ID"]
            return str(p)
        p = table[p][0]
    if env.get("CURSOR_CONVERSATION_ID"):  # a detached helper of a Cursor conversation (parent is launchd)
        return "conv:" + env["CURSOR_CONVERSATION_ID"]
    return None


def kind(cmd):
    if "--remote" in cmd:
        return None
    if re.search(r"appium[/\\]index\.js\b.*\bserver\b|(^|/)appium( |$).*\bserver\b", cmd):
        return "appium server"
    if re.search(r"(^|/)xcodebuild\b", cmd) and "WebDriverAgent" in cmd:
        return "WebDriverAgent runner"
    if re.search(r"(^|/)(python\S*|Python)\s+\S*keepawake(?!d)\S*\.py", cmd):
        return "keep-awake loop"
    return None


def main():
    udid, taker, wired = sys.argv[1], sys.argv[2], int(sys.argv[3] or 0)
    table = procs()
    mine = set()
    p = os.getpid()
    while p > 1 and p in table:
        mine.add(p)
        p = table[p][0]
    scope = os.environ.get("DEVLOCK_CLEANUP_SCOPE")  # tests: only touch processes carrying this marker
    victims = []
    for pid, (ppid, cmd, env) in table.items():
        k = kind(cmd)
        if not k or pid in mine or env.get("DEVLOCK_INFRA") == "1":
            continue
        if scope and env.get("DEVLOCK_CLEANUP_SCOPE") != scope:
            continue
        if "devlock guard" in cmd or AWAKE_DIR in cmd:
            continue  # guarded loops stop themselves; the daemon's loops keep their state under DEVLOCK_HOME/awake
        if identity(pid, table) == taker:
            continue
        named = re.findall(r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{16}|[0-9a-f]{40}", cmd + " " + env.get("DEVLOCK_DEVICE", "") + " " + env.get("BDC_UDID", ""))
        if named and udid not in named:
            continue  # bound to another phone
        if not named and wired > 1:
            continue  # cannot tell which phone; only safe to assume when this is the only one
        victims.append((pid, k, cmd))
    for pid, k, cmd in victims:
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM) if os.getpgid(pid) == pid else os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    if victims:
        time.sleep(3)
        for pid, k, cmd in victims:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        for pid, k, cmd in victims:
            print(f"[devlock] cleared previous holder's {k} on {udid}: pid {pid} {cmd[:100]}")


if __name__ == "__main__":
    main()
