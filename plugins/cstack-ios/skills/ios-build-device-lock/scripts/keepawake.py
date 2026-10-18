#!/usr/bin/env python3
"""The keep-awake loop devlock's daemon (keepawaked.py) runs for each USB-connected phone, for test phones whose
Settings has no "Never" auto-lock. Agents never start it: they use `devlock awake status|pause|resume|off|on|auto`.

A loop taps the status-bar corner through WebDriverAgent (XCUITest, see _shared/scripts/wda.py) every
keepawake.interval_s, except while a protected app (config.protected_apps, plus the phone holder's declared
bundle) may be in front: an XCUITest tap there can get it killed by the watchdog, and a video app's pages keep
the screen on anyway. A running protected app counts as behind only when marked so.

  run       [--protect B]  the loop, in the foreground; ends on SIGTERM (the daemon stops it)
  behind    [--bundle B]   mark a running protected app as not in front (check a screenshot first)
  teardown                 stop the WDA runner ("Automation Running") and its local forwarder
All take --udid and --state-dir (the daemon passes DEVLOCK_HOME/awake/state-<UDID>).
"""
import argparse
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
for _base in (HERE, os.path.realpath(HERE)):  # the plugin's shared device layer, also from a copied/symlinked script
    _d = os.path.join(_base, "..", "..", "_shared", "scripts")
    if os.path.exists(os.path.join(_d, "ios_device.py")):
        sys.path.insert(0, _d)
        break
import ios_device  # noqa: E402

CFG = json.load(open(os.path.join(os.path.realpath(HERE), "..", "config.json")))
K = CFG["keepawake"]


def paths(state_dir):
    d = os.path.expanduser(state_dir)
    os.makedirs(d, exist_ok=True)
    return {n: os.path.join(d, f"keepawake.{n}") for n in ("log", "pause", "last_tap", "signal", "idle_state")}


def now():
    return datetime.datetime.now().isoformat(timespec="seconds")


def decide(apps, free=False, unseen=None, free_after=3, since_tap=None, max_gap=None):
    """(tap?, reason) from the protected apps' state: dicts with name, alive, front (WDA reports this app as the
    one in front) and behind (marked behind for this pid).

    WDA's activeAppInfo names the front app outright, so `front` is normally certain. But it is not always
    available: no runner, no forwarder, or a query that timed out all come back front=False for every app, and
    that is not proof that none is in front. So on a phone someone holds, a running protected app counts as behind
    only when marked so - guessing from "we could not see it" is what an earlier version of this did, and it
    guessed wrong on an app that was in front while its main thread was busy.

    On a free phone (no devlock holder) nobody's run can be disturbed, while a locked phone needs its passcode
    typed by a human. So there an app running but not seen in front for `free_after` consecutive checks is tapped
    anyway: the worst case is a protected app that was in front getting killed by the watchdog. Seen 2026-10-14: the
    user left the app for Settings by hand, nothing marked it behind, and the phone locked."""
    unseen = unseen or {}
    stale = []
    # Safety net on a free phone: whatever the counts say, never go longer than max_gap without a tap unless WDA
    # positively reports a protected app in front (in front it keeps the screen on itself).
    # 2026-10-14: a hung front-app query crashed the loop, the restart reset the counts, and 5 min 40 s passed untapped.
    if free and max_gap and since_tap is not None and since_tap >= max_gap and not any(a["alive"] and a["front"] for a in apps):
        return True, f"free phone, {int(since_tap)} s since the last tap (>= {max_gap} s): tap"
    for a in apps:
        if not a["alive"]:
            continue
        if a["front"]:
            return False, f"{a['name']} is the front app (WDA): no tap, it keeps the screen on itself"
        if a["behind"]:
            continue
        if free and unseen.get(a["name"], 0) >= free_after:
            stale.append(a["name"])
            continue
        if free:
            return False, (f"{a['name']} running, not seen in front {unseen.get(a['name'], 0)}/{free_after} checks "
                           f"on a free phone: no tap yet")
        return False, (f"{a['name']} running and not known to be behind: no tap. If it is not in front "
                       f"(check a screenshot), `devlock awake behind`; if it is, it keeps the screen on")
    if stale:
        return True, (f"free phone, {', '.join(stale)} running but not seen in front for {free_after}+ checks: tap "
                      f"(nobody holds this phone; a locked phone would need its passcode)")
    alive = [a["name"] for a in apps if a["alive"]]
    return True, (f"protected apps marked behind ({', '.join(alive)})" if alive else "no protected app running")


def phone_free(udid):
    """True when no devlock session holds this phone (and devlock is installed)."""
    dl = ios_device.devlock()
    if not dl:
        return False
    env = {k: v for k, v in os.environ.items() if k not in ("CURSOR_CONVERSATION_ID",)}
    env["DEVLOCK_SESSION_PID"] = "0"
    return subprocess.run([dl, "check", f"device-{udid}"], capture_output=True, env=env).returncode == 0


def protected_apps(dev, extra=(), names=None):
    """config.protected_apps plus the `extra` bundle ids, each as {"bundle_id", "app_name"}. An entry without an
    app_name gets its `<Name>.app` from the phone (the process list matches on it, so a wrong one reads an app in
    front as not running). One not installed is left out, and looked up again on the next call; `names` caches."""
    names = {} if names is None else names
    out = []
    for x in CFG["protected_apps"] + [{"bundle_id": b} for b in extra]:
        b = x["bundle_id"]
        if any(y["bundle_id"] == b for y in out):
            continue
        if not names.get(b):
            names[b] = x.get("app_name") or dev.app_name_for(b)
        if names[b]:
            out.append({"bundle_id": b, "app_name": names[b]})
    return out


def observe(protected, pids, front, dev):
    """One check's state per protected app for decide(). `front` is the bundle id WDA reports in front, or None
    when it could not be read. A behind-mark is dropped when the app turns out to be in front after all, or when
    it was made for another process."""
    apps = []
    for x in protected:
        b = x["bundle_id"]
        pid = pids[b][0] if pids.get(b) else None
        in_front = front is not None and b == front
        behind = pid is not None and not in_front and dev.behind(b, pid)
        if pid is None or in_front or not behind:
            dev.clear_behind(b)
        apps.append({"name": x["app_name"], "alive": pid is not None, "front": in_front, "behind": behind})
    return apps


IDLE_RE = re.compile(r"UPDATE BEHAVIOR: SBMainWorkspace reason:(.*?)\s*<SBIdleTimerBehavior: [^;]*; "
                     r"duration: ([^;]+); mode: (\w+)")


class IdleWatch:
    """Will this phone auto-lock right now? Read from SpringBoard's syslog: every change of the effective idle timer is
    logged as `UPDATE BEHAVIOR: SBMainWorkspace ... mode: Auto|Disabled` (Disabled = the front app keeps the screen
    on). Verified 2026-10-14 on an iPhone 14 with four builds of one video app (two of them staff builds, one with no
    in-process debug server at all) and Settings: the signal is SpringBoard's, so it does not depend on how the app
    was built - which is what makes it better than asking the app.

    The state is only known after the first change since the stream (re)started, and is dropped whenever the stream
    ends - then `current()` is None and the caller falls back to the front-app rules. A filtered stream costs
    ~0.1 CPU-s a minute and touches no app, which is why it is the main path."""

    def __init__(self, udid, cmd=None, state_file=None, carry_s=120):
        self.cmd = cmd or ["idevicesyslog", "-u", udid, "-p", "SpringBoard", "-m", "UPDATE BEHAVIOR: SBMainWorkspace"]
        self.mode = self.reason = self.since = None
        self.alive = False
        self.state_file = state_file
        self.carry = None  # last state of a loop that restarted a moment ago (daemon refresh / reinstall)
        try:
            if state_file and time.time() - os.path.getmtime(state_file) < carry_s:
                self.carry = json.load(open(state_file))
        except (OSError, ValueError):
            pass
        self.unavailable = None
        self.lock = threading.Lock()
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        if not shutil.which(self.cmd[0]):
            self.unavailable = f"{self.cmd[0]} not installed (brew install libimobiledevice)"
            return
        while True:
            try:
                p = subprocess.Popen(self.cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                                     text=True, errors="replace")
            except OSError as e:
                self.unavailable = f"cannot start {self.cmd[0]}: {e}"
                time.sleep(30)
                continue
            with self.lock:
                self.alive, self.unavailable = True, None
                if self.carry:  # the previous loop stopped seconds ago: its state still holds until a change says otherwise
                    self.mode, self.reason, self.since = self.carry["mode"], self.carry["reason"] + " (carried over)", self.carry["since"]
                    self.carry = None
            for line in p.stdout:
                m = IDLE_RE.search(line)
                if m:
                    with self.lock:
                        self.reason, self.mode, self.since = m.group(1).strip(), m.group(3), time.time()
                    self._save()
            p.wait()
            with self.lock:  # events may be missed while reconnecting: forget the state
                self.alive, self.mode, self.reason, self.since = False, None, None, None
                self.unavailable = "syslog stream ended; reconnecting"
            time.sleep(5)

    def _save(self):
        if not self.state_file:
            return
        try:
            with open(self.state_file, "w") as f:
                json.dump({"mode": self.mode, "reason": self.reason, "since": self.since}, f)
        except OSError:
            pass

    def touch(self):
        """Refresh the saved state's age while it still holds (so a restart can carry it)."""
        if self.state_file and self.current():
            try:
                os.utime(self.state_file)
            except OSError:
                self._save()

    def current(self):
        """(mode, reason, since) when the live state is known, else None."""
        with self.lock:
            return (self.mode, self.reason, self.since) if self.alive and self.mode else None

    def describe(self):
        with self.lock:
            if self.alive and self.mode:
                return {"source": "syslog", "mode": self.mode, "reason": self.reason, "since": self.since}
            why = self.unavailable or ("no idle-timer change seen since the stream started" if self.alive else "starting")
            return {"source": "fallback", "why": why}


def last_tap(p):
    """Time of the last successful tap, kept in the state dir so a restarted loop knows it."""
    try:
        return os.path.getmtime(p["last_tap"])
    except OSError:
        return None


def run(a):
    p = paths(a.state_dir)
    dev = ios_device.IOSDevice(a.udid, need_lock=False)  # infrastructure: runs while sessions hold the phone

    def log(line):
        with open(p["log"], "a") as f:
            f.write(f"{now()} {line}\n")

    names, unseen = {}, {}
    watch = IdleWatch(dev.udid, cmd=json.loads(os.environ["DEVLOCK_IDLE_CMD"]) if os.environ.get("DEVLOCK_IDLE_CMD") else None,
                      state_file=p["idle_state"])
    log(f"started (pid {os.getpid()}, device {dev.udid}, every {K['interval_s']} s)")
    while True:  # ended by SIGTERM from the daemon
        t0 = time.time()
        try:
            check(dev, a, p, names, unseen, log, watch)
        except Exception as e:  # one bad check (a hung scan, a devicectl hiccup) must not end the loop
            log(f"check failed: {e!r:.200}")
        time.sleep(max(1.0, K["interval_s"] - (time.time() - t0)))  # a fixed cadence: the check's time counts


FAILS = {"n": 0}
WDA = {"last_need": time.time(), "retired": False}  # when a tap was last needed; whether WDA was retired since


def other_phones(udid):
    """Other USB phones on this Mac (one forwarder per phone, but a shared host process cannot be told apart, so
    it is only stopped when this phone is the only one)."""
    dl = ios_device.devlock()
    if not dl:
        return []
    out = subprocess.run([dl, "devices"], capture_output=True, text=True).stdout.split()
    return [u for u in out if u != udid]


def retire_wda(dev, log, why):
    """Stop this phone's WebDriverAgent runner ("Automation Running"), and its local forwarder when no other phone
    is plugged in. Whoever needs a tap next brings WDA back up - see _shared/scripts/wda.py on hosting the runner."""
    if other_phones(dev.udid):
        procs = dev.automation_processes()
        for pid in procs["device_wda_runner"]:
            dev.signal_pid(pid, "SIGTERM")
        log(f"{why}: stopped WebDriverAgent on the phone (forwarder kept: other phones are plugged in)")
    else:
        dev.teardown_automation()
        log(f"{why}: stopped WebDriverAgent on the phone and its local forwarder")


def should_retire(free, idle_s, retired, threshold):
    """Retire WDA once a free phone has needed no tap for `threshold` seconds (a held phone's session may be using it)."""
    return bool(free and threshold and not retired and idle_s >= threshold)


def maybe_retire(dev, log):
    idle = time.time() - WDA["last_need"]
    if should_retire(phone_free(dev.udid), idle, WDA["retired"], K.get("wda_idle_teardown_s")):
        WDA["retired"] = True
        try:
            retire_wda(dev, log, f"no tap needed for {int(idle)} s on a free phone")
        except Exception as e:
            log(f"retiring WebDriverAgent failed: {e!r:.160}")


def tap(dev, p, why, log):
    # After a retire the first tap has to bring WebDriverAgent up from cold: 75 s was not enough
    # (2026-10-15: timed out, the next check's tap worked, 2 min 24 s after the switch).
    cold = WDA["retired"]
    WDA["last_need"], WDA["retired"] = time.time(), False
    timeout = K.get("tap_cold_timeout_s", 150) if cold else K.get("tap_timeout_s", 75)
    rc, out, _ = dev.tap_points(*K["tap_point"], timeout=timeout)
    if rc == 0:
        open(p["last_tap"], "w").close()
        FAILS["n"] = 0
        log(why + "; WDA tap ok")
        return
    FAILS["n"] += 1
    hint = (f" ({FAILS['n']} in a row: WebDriverAgent is not starting - look at the phone for an alert or a "
            "Verify App prompt; see references/keepawake.md)") if FAILS["n"] >= 3 else ""
    log(why + "; WDA tap FAILED: " + (out.strip().splitlines() or ["?"])[-1][:120] + hint)


def check(dev, a, p, names, unseen, log, watch=None):
    if os.path.exists(p["pause"]) and time.time() - os.path.getmtime(p["pause"]) > K["pause_max_s"]:
        os.remove(p["pause"])
    sig = watch.describe() if watch else {"source": "fallback", "why": "no syslog watch"}
    if sig.get("source") == "syslog":
        text = f"syslog idle timer {sig['mode']} for {int(time.time() - sig['since'])} s ({(sig['reason'] or '')[:60]})"
    else:
        text = f"fallback to the front-app rules: {sig.get('why', '?')}"
    try:
        with open(p["signal"], "w") as f:  # one readable line for `devlock awake status`
            f.write(text + "\n")
    except OSError:
        pass
    if os.path.exists(p["pause"]):
        log("paused")
        return
    state = watch.current() if watch else None
    if state and hasattr(watch, "touch"):
        watch.touch()
    if state:  # the phone tells us whether it will auto-lock: no guessing about which app is in front
        mode, reason, since = state
        what = f"idle timer {mode} ({int(time.time() - since)} s, {reason[:70]})"
        if mode == "Disabled":
            log(what + ": the front app keeps the screen on, no tap")
            maybe_retire(dev, log)
        else:
            tap(dev, p, what + ": tap", log)
        return
    fallback(dev, a, p, names, unseen, log, sig.get("why", ""))


def fallback(dev, a, p, names, unseen, log, why_fallback=""):
    """The front-app rules, for when the idle-timer signal is unknown. One WDA query in place of SpringBoard's own
    signal: it answers for any build, not just ones carrying a debug server, but it is an XCUITest call against the
    front app, which is why this is the exception and not the main path."""
    protected = protected_apps(dev, a.protect, names)
    rows = dev.processes()
    pids = {x["bundle_id"]: dev.pids_in(rows, x["app_name"]) for x in protected}
    front = None
    if any(pids.values()):
        try:
            front = dev.front_app(timeout=K.get("front_query_timeout_s", 20))
        except Exception as e:  # unknown this time; the counts and the max-gap net decide
            log(f"front-app query failed ({e!r:.120}); counting as unknown")
    apps = observe(protected, pids, front, dev)
    for x in apps:  # consecutive checks each app ran without turning up in front or being marked behind
        unseen[x["name"]] = unseen.get(x["name"], 0) + 1 if x["alive"] and not x["front"] and not x["behind"] else 0
    lt = last_tap(p)
    do_tap, why = decide(apps, free=phone_free(dev.udid), unseen=unseen, free_after=K.get("free_phone_unseen_checks", 3),
                         since_tap=None if lt is None else time.time() - lt, max_gap=K.get("free_phone_max_gap_s"))
    why = f"[fallback: {why_fallback}] {why}" if why_fallback else why
    if do_tap:
        tap(dev, p, why, log)
    else:
        log(why)
        maybe_retire(dev, log)


def behind(a):
    """Mark a running protected app as behind, after checking (e.g. a screenshot) that it is not in front."""
    dev = ios_device.IOSDevice(a.udid, need_lock=False)  # a marker file and read-only checks only
    bundle = a.bundle or (CFG["protected_apps"] or [{}])[0].get("bundle_id")
    if not bundle:
        sys.exit("no --bundle given and config.protected_apps is empty; name the app to mark")
    x = next((x for x in protected_apps(dev, a.protect) if x["bundle_id"] == bundle), None)
    if not x:
        sys.exit(f"{bundle} is not in config.protected_apps or --protect, or is not installed")
    pids = dev.app_pids(x["app_name"])
    if not pids:
        sys.exit(f"{x['app_name']} is not running; nothing to mark")
    if dev.front_app() == x["bundle_id"]:
        sys.exit(f"WDA reports {x['app_name']} as the front app; not marked")
    dev.mark_behind(x["bundle_id"], pids[0])
    print(f"marked {x['app_name']} (pid {pids[0]}) as behind; dropped when it is relaunched or turns up in front")


def teardown(a):
    """End "Automation Running" (the WDA runner) and, when no other phone is plugged in, its local forwarder. Also
    used by the daemon right after an unplug: it works if the phone is still reachable over the network."""
    dev = ios_device.IOSDevice(a.udid, need_lock=False)
    retire_wda(dev, print, "teardown")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--udid")
    ap.add_argument("--state-dir", default=os.path.expanduser("~/.devlock/awake/state-manual"))
    sub = ap.add_subparsers(dest="cmd", required=True)
    protect = argparse.ArgumentParser(add_help=False)
    protect.add_argument("--protect", action="append", default=[], metavar="BUNDLE",
                         help="also hold off while this app may be in front (repeatable); .app name read from the phone")
    sub.add_parser("run", parents=[protect])
    sub.add_parser("behind", parents=[protect]).add_argument("--bundle", help="protected app (default: the first in config)")
    sub.add_parser("teardown")
    a = ap.parse_args()
    {"run": run, "behind": behind, "teardown": teardown}[a.cmd](a)


if __name__ == "__main__":
    main()
