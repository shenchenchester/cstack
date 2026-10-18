#!/usr/bin/env python3
"""Agent hook for devlock — Claude Code (PreToolUse, Stop) and Cursor (beforeShellExecution, stop).

PreToolUse(Bash): a command that builds or touches a phone must hold the matching lock
(`build`, `device-<UDID>`): either wrapped in `devlock --name ... --` or covered by a session hold
(`devlock acquire`). Otherwise it is denied with the exact fix. With several USB phones a device
command must name its phone. Starting your own keep-awake loop is denied: the daemon owns it.
PreToolUse(MCP build tool): denied while another process holds the build lock.
Stop: a session hold without --ttl is released when the turn ends with no device job running.
Escape hatch (user decision only): DEVLOCK_BYPASS=1 in the command.

The host is told apart by `hook_event_name`: Claude sends PreToolUse/Stop and reads
hookSpecificOutput.permissionDecision; Cursor sends beforeShellExecution/stop and reads
{"permission", "user_message", "agent_message"} (verified with cursor-agent 2026.10.01).
"""
import json, os, re, shlex, subprocess, sys, urllib.parse

DL_HOME = os.environ.get("DEVLOCK_HOME", os.path.expanduser("~/.devlock"))
BIN = os.path.dirname(os.path.realpath(__file__))
DEVLOCK = os.path.join(BIN, "devlock")

def _conf():
    """Overrides install.py copied out of the skill's config.json; absent on a fresh DEVLOCK_HOME."""
    try:
        with open(os.path.join(DL_HOME, "config.json")) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


# Wrapper scripts in your own repo that build, or that drive a phone. `(?!)` never matches, which is
# the right default: devlock already catches xcodebuild, devicectl and the device CLIs directly, and
# only you know what your own build script is called. Set build_scripts / device_scripts in the
# skill's config.json (regexes matched against the end of the command's path).
_C = _conf()
BUILD_SCRIPTS = re.compile(_C.get("build_scripts") or r"(?!)")
DEVICE_SCRIPTS = re.compile(_C.get("device_scripts") or r"(?!)")
XCODEBUILD_READONLY = {"-list", "-showsdks", "-version", "-showBuildSettings", "-showdestinations", "-help", "-usage"}
# Listing what is attached, or what is installed, collides with nobody; anything else takes the phone.
DEVICE_CLI_READONLY = {"--help", "-h", "help", "version", "--version", "-v", "list", "devices",
                       "list-devices", "-l", "--list"}
WRAPPERS = {"bash", "sh", "zsh", "exec", "time", "nohup", "command", "caffeinate", "env", "sudo"}
ASSIGN = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$")
HEREDOC = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1([^\n]*)\n.*?\n\s*\2[ \t]*(?=\n|$)", re.S)
UDID_RE = re.compile(r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{16}|[0-9a-f]{40}|[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}")


def segments(cmd):
    """Quote-aware split on shell operators; yields (env assignments, tokens). Heredoc bodies are data
    (code fed to python, file contents), not commands, so they are dropped first."""
    cmd = HEREDOC.sub(lambda m: m.group(3), cmd)
    try:
        lex = shlex.shlex(cmd.replace("\n", " ; "), posix=True, punctuation_chars=";&|()`")
        lex.whitespace_split = True
        lex.commenters = ""
        toks_all = list(lex)
    except ValueError:
        toks_all = cmd.split()
    seg = []
    for tok in toks_all + [";"]:
        if tok and set(tok) <= set(";&|()`"):
            toks, seg, env = seg, [], {}
            while toks and (ASSIGN.match(toks[0]) or os.path.basename(toks[0]) in WRAPPERS or toks[0].startswith("-") and toks[0] != "--"):
                m = ASSIGN.match(toks[0])
                if m:
                    env[m.group(1)] = m.group(2)
                toks = toks[1:]
            if toks:
                yield env, toks
        else:
            seg.append(tok)


def target_id(env, toks):
    """Phone named by a device command: --device/-d, -u/--udid, -destination id=, DEVICE_UDID/DEVLOCK_DEVICE.
    An unexpanded shell variable (`--device $U`) names nothing the hook can know: treated as unnamed."""
    tid = _target_id(env, toks)
    return None if tid and "$" in tid else tid


def _target_id(env, toks):
    for k in ("DEVLOCK_DEVICE", "DEVICE_UDID"):
        if env.get(k):
            return env[k]
    for i, a in enumerate(toks):
        nxt = toks[i + 1] if i + 1 < len(toks) else ""
        if a in ("--device", "-d", "-u", "--udid", "-i", "--id") and nxt and UDID_RE.search(nxt) or a == "--device" and nxt:
            return nxt
        m = re.match(r"--(?:udid|device)=(\S+)", a)
        if m:
            return m.group(1)
        m = re.search(r"\bid=([0-9A-Fa-f-]{20,})", a)
        if m:
            return m.group(1)
    return None


def needs(cmd):
    """Set of lock needs: 'build', 'device' (phone unnamed), 'device:<id>' (phone named), 'keepawake'."""
    need = set()
    for env, t in segments(cmd):
        head, base = t[0], os.path.basename(t[0])
        if base == "devlock":  # analyze the wrapped command after `--`
            if "--" not in t:
                continue
            t = t[t.index("--") + 1:]
            while t and (ASSIGN.match(t[0]) or os.path.basename(t[0]) in WRAPPERS or t[0].startswith("-")):
                m = ASSIGN.match(t[0])
                if m:
                    env[m.group(1)] = m.group(2)
                if t[0] == "-c" and len(t) > 1:  # bash -c '<script>'
                    need |= needs(t[1])
                    t = []
                    break
                t = t[1:]
            if not t:
                continue
            head, base = t[0], os.path.basename(t[0])
        rest = t[1:]
        dev = False
        if BUILD_SCRIPTS.search(head):
            need.add("build")
            dev = bool(DEVICE_SCRIPTS.search(head))   # a script that both builds and drives a phone
        elif DEVICE_SCRIPTS.search(head):
            dev = True
        elif base == "xcodebuild":
            if not (set(rest) & XCODEBUILD_READONLY):
                need.add("build")
                dest = " ".join(rest)
                dev = bool(re.search(r"\btest", dest) and re.search(r"platform=iOS[,\s]|platform=iOS$|id=[0-9A-F]{8}-", dest))
        elif base == "make" and "install_example" in rest:
            need.add("build")
        elif base == "xcrun" and rest[:2] == ["devicectl", "device"]:
            dev = not ({"--help", "-h", "help"} & set(rest))
        elif base in ("ios-deploy", "tidevice", "go-ios", "iproxy", "appium") or base.startswith("idevice"):
            # go-ios/idevice*/tidevice drive the phone; iproxy forwards its WDA port and appium drives
            # that, so all of them collide with another session's run.
            dev = not (DEVICE_CLI_READONLY & set(rest))
        elif re.match(r"python3?(\.\d+)?$|node$", base):
            joined = " ".join(rest)
            script = next((a for a in rest if not a.startswith("-")), "")  # what python actually runs
            sname = os.path.basename(script)
            starts = {"start", "run"} & set(rest)
            if re.match(r"keepawake(?!d)\S*\.py$", sname) and starts:
                need.add("keepawake")  # starting a loop; status / stop / pause / behind stay allowed
            elif re.search(r"devicectl|\bwda\b|WebDriverAgent|\bappium\b", joined):  # `wda` as a word, not in patch_wda.py
                dev = True
        if dev:
            tid = target_id(env, rest)
            need.add(f"device:{tid}" if tid else "device")
    return need


def wrapper_names(cmd):
    m = re.search(r"\bdevlock((?:\s+--name\s+\S+)+)\s+--", cmd)
    return set(re.findall(r"--name\s+(\S+)", m.group(1))) if m else set()


def run(*args):
    return subprocess.run([DEVLOCK, *args], capture_output=True, text=True)


def udid_of(tid):
    r = subprocess.run([sys.executable, os.path.join(BIN, "idmap.py"), tid], capture_output=True, text=True)
    return r.stdout.strip() or tid


class Deny(Exception):
    pass


def deny(reason):
    raise Deny(reason)


GUIDE = (
    "devlock queues until the lock is free and prints who holds it; run long builds with run_in_background so "
    "queueing doesn't hit the Bash timeout. For a device test run, take the phone for the whole run instead of per "
    "command: `devlock acquire --name device --bundle <app bundle id> --note '<what you are doing>'` (prints "
    "`export DEVLOCK_DEVICE=<UDID>`; pass that UDID to every device command). It is released when your turn ends with "
    "no device job running; add `--ttl 90m` when the run spans turns — then it lasts until "
    "`devlock release --name device` or the TTL. Taking a phone clears the previous holder's Appium/WDA/keep-awake "
    "processes on it. Keep-awake is run by a daemon (`devlock awake status|pause|off|on`), never start your own. "
    "Start background loops on the phone as `devlock guard --name device -- <loop>`, and long-lived helpers "
    "(Appium server, WDA proxy) with `DEVLOCK_SESSION_PID=$(devlock whoami)`. Release as soon as the run is done. "
    "Never add DEVLOCK_BYPASS=1 yourself — only the user may decide to skip the lock. Do not abandon the task: wait "
    "for the lock."
)


def check_bash(cmd):
    if "DEVLOCK_BYPASS=1" in cmd:
        return
    need = needs(cmd)
    if "keepawake" in need:
        deny("[devlock] Don't start your own keep-awake loop: a daemon keeps every USB-connected phone awake and "
             "only taps when the holder's app is not in front. Declare your app with `devlock acquire --name device "
             "--bundle <id>`, pause it around sensitive steps with `devlock awake pause` (120 s, repeat to extend), "
             "or switch it off for your phone with `devlock awake off` / back with `devlock awake on`.")
    if not need:
        return
    wired = run("devices").stdout.split()
    locks = set()
    if "build" in need:
        locks.add("build")
    for n in need:
        if n.startswith("device:"):
            locks.add("device-" + udid_of(n[7:]))
        elif n == "device":
            if len(wired) > 1:
                held = run("mine", "device").returncode == 0
                deny(f"[devlock] {len(wired)} phones are connected ({' '.join(wired)}) and this command doesn't say "
                     "which one, so it may hit a phone another session is using. Pass the UDID explicitly "
                     "(devicectl --device <UDID>, go-ios/idevice* -u <UDID>, xcodebuild -destination id=<UDID>)"
                     + (" — use the phone you hold (`echo $DEVLOCK_DEVICE` / `devlock status`)." if held else
                        " and take that phone first with `devlock acquire --name device-<UDID>`.") + " " + GUIDE)
            locks.add(f"device-{wired[0]}" if wired else "device")
    given = wrapper_names(cmd)
    missing = []
    for n in sorted(locks):
        if n in given or (n.startswith("device") and "device" in given and len(wired) <= 1):
            continue
        if run("mine", n).returncode == 0:  # covered by this session's hold
            continue
        missing.append(n)
    if not missing:
        return
    names = " ".join(f"--name {n}" for n in sorted(locks, key=lambda x: (x != "build", x)))
    deny(f"[devlock] This command needs the lock(s) {', '.join(sorted(locks))} so it doesn't collide with builds/device "
         f"tests from other sessions. Re-run it wrapped as:\n  devlock {names} -- bash -c '<original command>'\n"
         f"(or put `devlock {names} --` in front of the single command). " + GUIDE)


def cursor_conv(raw):
    """The value Cursor puts in CURSOR_CONVERSATION_ID for a hook's conversation_id (its shell-safe transform:
    encodeURIComponent with % -> _; a UUID passes unchanged)."""
    return urllib.parse.quote(raw, safe="-_.!~*'()").replace("%", "_")[:200] if raw else ""


def proc_env_conv(pid):
    """CURSOR_CONVERSATION_ID in a process's environment (ps -E), or None."""
    out = subprocess.run(["ps", "-E", "-o", "command=", "-p", str(pid)], capture_output=True, text=True).stdout
    m = re.search(r"(?:^|\s)CURSOR_CONVERSATION_ID=(\S+)", out)
    return m.group(1) if m else None


def on_cursor_stop(conv):
    """Cursor turn ended: release this conversation's turn-scoped holds unless one of its device jobs still runs."""
    root = os.environ.get("DEVLOCK_ROOT", os.path.join(DL_HOME, "locks"))
    owned = []
    for name in os.listdir(root) if os.path.isdir(root) else []:
        if not name.endswith(".lock"):
            continue
        try:
            owner = open(os.path.join(root, name, "owner")).read()
        except OSError:
            continue
        if f"\nconv={conv}\n" in "\n" + owner and "mode=acquire" in owner and "\nexpires=" not in owner:
            owned.append(name[:-5])
    if not owned:
        return
    busy = set()
    ps = subprocess.run(["ps", "-Ao", "pid=,command="], capture_output=True, text=True).stdout
    for line in ps.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) != 2 or int(parts[0]) == os.getpid():
            continue
        kinds = {"build" if n == "build" else "device" for n in needs(parts[1])}
        if "devlock guard" in parts[1] or re.search(r"devlock\s+--name\s+device", parts[1]):
            kinds.add("device")
        if kinds and proc_env_conv(parts[0]) == conv:
            busy |= kinds
    for n in owned:
        if ("device" if n.startswith("device") else n) not in busy:
            subprocess.run([DEVLOCK, "release", "--name", n], env={**os.environ, "CURSOR_CONVERSATION_ID": conv},
                           capture_output=True)


def agent_kind(pid, cmd):
    """"claude" / "cursor" for an agent process, else None: claude and cursor-agent by their executable (`ps -o comm=`,
    a full path that may contain spaces), Cursor desktop's extension host by its process title."""
    if cmd.startswith("Cursor Helper (Plugin): extension-host"):
        return "cursor"
    comm = subprocess.run(["ps", "-o", "comm=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    name = os.path.basename(os.path.realpath(comm)) if comm else ""  # a cloud runner may start cursor-agent as `agent` (symlink)
    return {"claude": "claude", "cursor-agent": "cursor"}.get(name)


def session_pid():
    p = os.getppid()
    while p > 1:
        out = subprocess.run(["ps", "-o", "ppid=,command=", "-p", str(p)], capture_output=True, text=True).stdout.strip()
        if not out:
            return None
        ppid, cmd = out.split(None, 1)
        if agent_kind(p, cmd):
            return p
        p = int(ppid)
    return None


def on_stop():
    """Turn ended: hand back session holds (no --ttl) unless a device job of this session is still running."""
    sp = session_pid()
    if not sp:
        return
    root = os.environ.get("DEVLOCK_ROOT", os.path.join(DL_HOME, "locks"))
    owned = []
    for name in os.listdir(root) if os.path.isdir(root) else []:
        if not name.endswith(".lock"):
            continue
        try:
            owner = open(os.path.join(root, name, "owner")).read()
        except OSError:
            continue
        if f"pid={sp}\n" in owner and "mode=acquire" in owner and "\nexpires=" not in owner:
            owned.append(name[:-5])
    if not owned:
        return
    ps = subprocess.run(["ps", "-Ao", "pid=,ppid=,command="], capture_output=True, text=True).stdout
    procs = {}
    for line in ps.splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3:
            procs[int(parts[0])] = (int(parts[1]), parts[2])

    def under_session(pid):
        while pid > 1 and pid in procs:
            if pid == sp:
                return True
            pid = procs[pid][0]
        return False
    busy = set()
    for pid, (_, cmd) in procs.items():
        if pid != sp and under_session(pid):
            for n in needs(cmd):
                busy.add("build" if n == "build" else "device")
            m = re.search(r"devlock\s+(?:guard\s+)?((?:--name\s+\S+\s*)+)", cmd)
            if m:
                busy |= {"device" if x.startswith("device") else x for x in re.findall(r"--name\s+(\S+)", m.group(1))}
    for n in owned:
        if ("device" if n.startswith("device") else n) not in busy:
            subprocess.run([DEVLOCK, "release", "--name", n], env={**os.environ, "DEVLOCK_SESSION_PID": str(sp)},
                           capture_output=True)


def main():
    data = json.load(sys.stdin)
    event = data.get("hook_event_name", "")
    conv = cursor_conv(data.get("conversation_id", "")) if event in ("beforeShellExecution", "stop") else ""
    if conv:  # Cursor: every devlock call below speaks for this conversation, like the agent's own shell would
        os.environ["CURSOR_CONVERSATION_ID"] = conv
    if event == "Stop":
        on_stop()
        return
    if event == "stop":  # Cursor
        if conv:
            on_cursor_stop(conv)
        print("{}")
        return
    if event == "beforeShellExecution":  # Cursor
        try:
            check_bash(data.get("command", ""))
        except Deny as e:
            # cursor-agent (2026.10.01) showed the model only user_message, so the full fix goes in both
            print(json.dumps({"permission": "deny", "user_message": str(e), "agent_message": str(e)}))
            return
        print(json.dumps({"permission": "allow"}))
        return
    tool = data.get("tool_name", "")  # Claude PreToolUse
    inp = data.get("tool_input", {}) or {}
    try:
        if tool != "Bash":  # MCP build tool: check-only
            r = run("check", "build")
            if r.returncode == 1:
                deny(f"[devlock] Another session is building right now ({r.stdout.strip()}). Don't start a parallel "
                     "build: wait and retry later, or run the build via Bash wrapped in `devlock --name build -- ...` "
                     "(with run_in_background) so it queues automatically.")
            return
        check_bash(inp.get("command", ""))
    except Deny as e:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                                 "permissionDecisionReason": str(e)}}))


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        sys.exit(1)  # hook bug: fail open (Claude and Cursor both proceed on exit codes other than 0/2)
