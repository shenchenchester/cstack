#!/usr/bin/env python3
"""Keep-awake daemon (launchd agent com.devlock.keepawake): one keep-awake loop per USB-connected phone.

Every 30 s it looks at the USB tree. For each plugged-in phone whose keep-awake is wanted (`devlock awake wanted`:
mode auto unless declared never-lock, or on) it runs this skill's `keepawake.py run` loop (never taps while a
protected app may be in front); the holder's declared app (`devlock acquire --bundle`) is added with `--protect`. A loop is restarted when the holder's bundle changes and stopped when the phone is
unplugged or switched off. Children run with DEVLOCK_INFRA=1 so devlock neither blocks nor cleans them.
"""
import json, os, signal, subprocess, sys, threading, time

DL_HOME = os.environ.get("DEVLOCK_HOME", os.path.expanduser("~/.devlock"))
BIN = os.path.dirname(os.path.realpath(__file__))
DEVLOCK = os.path.join(BIN, "devlock")
LOCKS = os.environ.get("DEVLOCK_ROOT", os.path.join(DL_HOME, "locks"))
AWAKE = os.environ.get("DEVLOCK_AWAKE_DIR", os.path.join(DL_HOME, "awake"))
try:  # written by install.py: where the device-control skill's keepawake lives, poll interval
    CONF = json.load(open(os.path.join(DL_HOME, "config.json")))
except Exception:
    CONF = {}
SKILL = os.environ.get("DEVLOCK_KEEPAWAKE_SCRIPT", CONF.get("keepawake_script", os.path.join(BIN, "keepawake.py")))
INTERVAL = int(os.environ.get("DEVLOCK_AWAKE_INTERVAL", CONF.get("awake_interval_s", 30)))
LOG = os.path.join(AWAKE, "daemon.log")
children = {}  # udid -> (Popen, bundle)


def log(msg):
    os.makedirs(AWAKE, exist_ok=True)
    if os.path.exists(LOG) and os.path.getsize(LOG) > 1_000_000:
        os.replace(LOG, LOG + ".1")
    with open(LOG, "a") as f:
        f.write(time.strftime("%F %T ") + msg + "\n")


def wired():
    r = subprocess.run([DEVLOCK, "devices"], capture_output=True, text=True)
    return [l.strip() for l in r.stdout.splitlines() if l.strip()]


def wanted(udid):
    return subprocess.run([DEVLOCK, "awake", "wanted", "--udid", udid], capture_output=True).returncode == 0


def holder_bundle(udid):
    """Bundle declared by a live holder of this phone, else None."""
    d = os.path.join(LOCKS, f"device-{udid}.lock")
    r = subprocess.run([DEVLOCK, "check", f"device-{udid}"], capture_output=True, text=True,
                       env={**os.environ, "DEVLOCK_SESSION_PID": "0"})
    if r.returncode != 1:  # not held by a live owner
        return None
    try:
        for line in open(os.path.join(d, "owner")):
            if line.startswith("bundle="):
                return line.split("=", 1)[1].strip() or None
    except OSError:
        pass
    return None


def phone_held(udid):
    r = subprocess.run([DEVLOCK, "check", f"device-{udid}"], capture_output=True, text=True,
                       env={**os.environ, "DEVLOCK_SESSION_PID": "0"})
    return r.returncode == 1


def start(udid, bundle):
    state = os.path.join(AWAKE, f"state-{udid}")
    os.makedirs(state, exist_ok=True)
    cmd = [sys.executable, SKILL, "--udid", udid, "--state-dir", state, "run"] + (["--protect", bundle] if bundle else [])
    env = {**os.environ, "DEVLOCK_INFRA": "1"}
    env.pop("DEVLOCK_SESSION_PID", None)
    p = subprocess.Popen(cmd, env=env, stdin=subprocess.DEVNULL, stdout=open(os.path.join(state, "loop.out"), "a"),
                         stderr=subprocess.STDOUT, start_new_session=True)
    children[udid] = (p, bundle)
    log(f"[{udid}] keep-awake started pid={p.pid} bundle={bundle or 'default'}")


def stop(udid, why):
    p, _ = children.pop(udid)
    try:  # keepawake.py run ends on SIGTERM
        os.killpg(p.pid, signal.SIGTERM)
        p.wait(timeout=10)
    except Exception:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except OSError:
            pass
    log(f"[{udid}] keep-awake stopped ({why})")


def network_teardown(udid):
    """Unplugged: the phone's WebDriverAgent keeps "Automation Running" up and a USB-less Mac cannot reach it - unless
    the phone is reachable over the network (Xcode 15+/iOS 17+: automatic for a paired phone on the same network, when
    that network lets devices reach each other; office Wi-Fi often does not). Try that, in the background."""
    def run():
        try:
            r = subprocess.run([sys.executable, SKILL, "--udid", udid, "teardown"], capture_output=True, text=True,
                               env={**os.environ, "DEVLOCK_INFRA": "1"}, timeout=120)
            ok = r.returncode == 0
        except Exception:
            ok = False
        log(f"[{udid}] after unplug: " + ("stopped WebDriverAgent over the network" if ok else
            "phone not reachable over the network; 'Automation Running' stays until WebDriverAgentRunner is closed "
            "in the phone's app switcher"))
    threading.Thread(target=run, daemon=True).start()


def tick():
    plugged = set(wired())
    for udid in list(children):
        p, _ = children[udid]
        if udid not in plugged:
            stop(udid, "unplugged")
            network_teardown(udid)
        elif not wanted(udid):
            stop(udid, "not wanted (devlock awake off / never-lock)")
            if holder_bundle(udid) is None and not phone_held(udid):  # nobody uses it: clear "Automation Running"
                subprocess.run([sys.executable, SKILL, "--udid", udid, "teardown"], capture_output=True,
                               env={**os.environ, "DEVLOCK_INFRA": "1"}, timeout=120)
                log(f"[{udid}] free phone: WDA runner and Appium torn down")
        elif p.poll() is not None:
            children.pop(udid)
            log(f"[{udid}] keep-awake loop exited rc={p.returncode}; restarting")
    for udid in plugged:
        if not wanted(udid):
            continue
        bundle = holder_bundle(udid)
        if udid in children and children[udid][1] != bundle:
            stop(udid, f"target app changed to {bundle or 'default'}")
        if udid not in children:
            start(udid, bundle)


def sweep_orphans():
    """Loops left by a previous daemon instance that died without stopping them (parent is now launchd)."""
    out = subprocess.run(["ps", "-Ao", "pid=,ppid=,command="], capture_output=True, text=True).stdout
    for line in out.splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3 and parts[1] == "1" and os.path.join(AWAKE, "state-") in parts[2]:
            try:
                os.kill(int(parts[0]), signal.SIGTERM)
                log(f"stopped orphaned keep-awake loop pid={parts[0]} from a previous daemon")
            except OSError:
                pass


def main():
    def bye(*_):
        for udid in list(children):
            stop(udid, "daemon exiting")
        sys.exit(0)
    signal.signal(signal.SIGTERM, bye)
    signal.signal(signal.SIGINT, bye)
    log(f"daemon started (interval {INTERVAL}s, skill {SKILL})")
    sweep_orphans()
    if not os.path.exists(SKILL):
        log(f"ERROR keep-awake script not found: {SKILL}")
    while True:
        try:
            tick()
        except Exception as e:  # keep the daemon alive; launchd would restart it anyway
            log(f"tick error: {e!r}")
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
