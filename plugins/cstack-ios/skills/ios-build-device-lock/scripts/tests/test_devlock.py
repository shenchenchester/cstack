"""devlock tests. Every test runs against a throwaway DEVLOCK_HOME and fake USB phones
(DEVLOCK_FAKE_WIRED), never the real locks. Tests that need macOS tools (xcrun devicectl, node for
fake helper processes, an agent ancestor for the Stop hook) skip when those are missing.

    cd plugins/cstack-ios/skills/ios-build-device-lock && python3 -m unittest discover -s scripts/tests
"""
import json, os, shutil, signal, subprocess, sys, tempfile, time, unittest

SCRIPTS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DL = os.path.join(SCRIPTS, "devlock")
HOOK = os.path.join(SCRIPTS, "hook.py")
SHIM = os.path.join(SCRIPTS, "xcrun-shim")
A, B = "00008110-000000000000001E", "00008150-000000000000002C"
U1, U2 = "AAAAAAAA-1111111111111111", "BBBBBBBB-2222222222222222"
NODE = shutil.which("node")
DARWIN_XCRUN = sys.platform == "darwin" and os.path.exists("/usr/bin/xcrun")


def agent_ancestor():
    p = os.getpid()
    while p > 1:
        out = subprocess.run(["ps", "-o", "ppid=,command=", "-p", str(p)], capture_output=True, text=True).stdout.strip()
        if not out:
            return None
        ppid, cmd = out.split(None, 1)
        if any(cmd == n or cmd.startswith(n + " ") or ("/" + n + " ") in cmd + " " for n in ("claude", "cursor-agent")):
            return p
        p = int(ppid)
    return None


class Base(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        # DEVLOCK_CLEANUP_SCOPE: handoff cleanup only ever touches this test's fake processes, never real ones
        self.env = {**os.environ, "DEVLOCK_HOME": self.home, "DEVLOCK_FAKE_WIRED": U1,
                    "DEVLOCK_CLEANUP_SCOPE": f"test-{os.getpid()}-{id(self)}"}
        for k in ("DEVLOCK_ROOT", "DEVLOCK_AWAKE_DIR", "DEVLOCK_SESSION_PID", "DEVLOCK_DEVICE", "DEVLOCK_NO_CLEANUP"):
            self.env.pop(k, None)
        self.procs = []

    def tearDown(self):
        for p in self.procs:
            try:
                p.kill()
                p.wait(timeout=5)
            except Exception:
                pass
        shutil.rmtree(self.home, ignore_errors=True)

    def dl(self, *args, env=None, **kw):
        return subprocess.run([DL, *args], capture_output=True, text=True, env={**self.env, **(env or {})}, **kw)

    def sleeper(self, argv0=None, env=None):
        """A live process; with argv0 its command line reads argv0 (needs node for a visible environment)."""
        if argv0:
            cmd = ["bash", "-c", f'exec -a "{argv0}" "{NODE}" -e "setTimeout(()=>{{}},120000)"']
        else:
            cmd = ["sleep", "300"]
        p = subprocess.Popen(cmd, env={**self.env, **(env or {})}, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.procs.append(p)
        return p

    def hold(self, udid, pid, extra=""):
        d = os.path.join(self.home, "locks", f"device-{udid}.lock")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "owner"), "w") as f:
            f.write(f"pid={pid}\nmode=acquire\nsince=x\ncwd=/other\n{extra}")
        return d


class HookCases(Base):
    S = "/x/repo"

    def setUp(self):
        super().setUp()
        # The two configurable regexes install.py copies into DEVLOCK_HOME, so these cases also pin
        # that a hook running out of DEVLOCK_HOME picks up a repo's own wrapper scripts.
        with open(os.path.join(self.home, "config.json"), "w") as f:
            json.dump({"build_scripts": r"(make-app\.sh|run-unit-test/.*\.sh)$",
                       "device_scripts": r"(run-device|install-app)\.sh$"}, f)

    CASES = [  # (USB phones, command, expected)
        ([A], "cd x && python3 -c \"import sys; sys.path.insert(0,'scripts'); import wda; wda.WDA().tap(1,1)\"", f"device-{A}"),
        ([A], "python3 -c 'print(1)'", "allow"),
        ([A], "grep -r wda_session . | head", "allow"),
        ([A], f"bash {S}/scripts/make-app.sh --target MyApp", "build"),
        ([A], "cd /x && LANG=en_US.UTF-8 make install_example pod=Foo target=MyApp", "build"),
        ([A], f"devlock --name build -- bash {S}/scripts/run-device.sh", f"device-{A}"),
        ([A], f"devlock --name build --name device -- bash -c 'bash {S}/scripts/make-app.sh && xcrun devicectl device install app --device {A} y'", "allow"),
        ([A], f"devlock --name build -- bash -c 'bash {S}/scripts/make-app.sh && xcrun devicectl device install app --device {A} y'", f"build,device-{A}"),
        ([A], f"bash {S}/scripts/install-app.sh", f"device-{A}"),
        ([A], "go-ios tap 4 4", f"device-{A}"),
        ([A], "go-ios --help", "allow"),
        ([A], "go-ios list", "allow"),
        # go-ios installs its binary as `ios`, and `ios runwda` is how the device layer starts WebDriverAgent
        ([A], f"ios runwda --udid {A}", f"device-{A}"),
        ([A], f"ios tunnel start --userspace --udid {A}", f"device-{A}"),
        ([A], "ios version", "allow"),
        ([A], "ios list", "allow"),
        ([A], "iproxy 8100 8100 -u " + A, f"device-{A}"),
        ([A], "iproxy --help", "allow"),
        ([A], "appium --port 4723", f"device-{A}"),
        ([A], "appium --version", "allow"),
        ([A], "echo 'xcodebuild -scheme X build'", "allow"),
        ([A], "xcodebuild -list", "allow"),
        ([A], f"SP=/tmp/x; touch $SP/a; xcrun devicectl device process launch --device {A} --console foo", f"device-{A}"),
        ([A], "xcrun devicectl list devices", "allow"),
        ([A], "xcrun devicectl device info apps --help", "allow"),
        ([A], "idevicesyslog --help", "allow"),
        ([A], "python3 /tmp/x/patch_wda.py", "allow"),
        ([A], "python3 -c 'import wda; wda.Client().home()'", f"device-{A}"),
        ([A], "U=00008110-000000000000001E; xcrun devicectl device process launch --device $U com.x", f"device-{A}"),
        ([A], "git status && git log -3", "allow"),
        ([A], f"ideviceinstaller -u {A} -i x.ipa", f"device-{A}"),
        ([A], f"ideviceinstaller -u {A} -l", "allow"),
        ([A], "idevice_id -l", "allow"),
        ([A], f"tidevice -u {A} install x.ipa", f"device-{A}"),
        ([A], "python3 - ~/.devlock/bin/keepawaked.py <<'EOF'\nprint(1)\nEOF", "allow"),
        ([A], "python3 - x <<'EOF'\nimport subprocess\nsubprocess.run(['go-ios', 'tap'])\nxcrun devicectl device install app --device 1\nEOF\necho done", "allow"),
        ([A], "cat > f.sh <<EOF\ngo-ios tap 1 1\nEOF\ngo-ios tap 4 4", f"device-{A}"),
        ([A], "python3 ../ios-build-device-lock/scripts/keepawake.py start --protect com.x", "deny-keepawake"),
        ([A], "python3 ../ios-build-device-lock/scripts/keepawake.py --udid X run", "deny-keepawake"),
        ([A], "python3 ../ios-build-device-lock/scripts/keepawake.py status", "allow"),
        ([A], "python3 ../ios-build-device-lock/scripts/keepawake.py stop", "allow"),
        ([A, B], "go-ios tap 4 4", "deny-multi"),
        ([A, B], f"go-ios tap 4 4 -u {B}", f"device-{B}"),
        ([A, B], f"DEVICE_UDID={A} go-ios tap 4 4", f"device-{A}"),
        ([A, B], f"xcrun devicectl device info details --device {B}", f"device-{B}"),
        ([A, B], f"devlock --name device-{B} -- go-ios tap 4 4 -u {B}", "allow"),
        ([A, B], f"devlock --name device -- go-ios tap 4 4 -u {B}", f"device-{B}"),
        ([], "go-ios tap 4 4", "device"),
    ]

    def classify(self, out):
        if not out.strip():
            return "allow"
        r = json.loads(out)
        reason = r.get("hookSpecificOutput", {}).get("permissionDecisionReason") or r.get("agent_message", "")
        if r.get("permission") == "allow":
            return "allow"
        if "own keep-awake" in reason:
            return "deny-keepawake"
        if "phones are connected" in reason:
            return "deny-multi"
        import re
        m = re.search(r"needs the lock\(s\) (.+?) so", reason)
        return m.group(1).replace(", ", ",") if m else reason[:80]

    def run_hook(self, wired, payload):
        env = {**self.env, "DEVLOCK_FAKE_WIRED": " ".join(wired)}
        return subprocess.run([sys.executable, HOOK], input=json.dumps(payload), capture_output=True, text=True, env=env)

    def test_claude_cases(self):
        for wired, cmd, exp in self.CASES:
            with self.subTest(cmd=cmd[:70], wired=len(wired)):
                r = self.run_hook(wired, {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": cmd}})
                self.assertEqual(self.classify(r.stdout), exp)

    def test_cursor_cases(self):
        for wired, cmd, exp in self.CASES:
            with self.subTest(cmd=cmd[:70], wired=len(wired)):
                r = self.run_hook(wired, {"hook_event_name": "beforeShellExecution", "command": cmd, "cwd": "/"})
                out = json.loads(r.stdout)  # Cursor always needs valid JSON with a permission
                self.assertIn(out["permission"], ("allow", "deny"))
                self.assertEqual(self.classify(r.stdout), exp)

    def test_cursor_stop_prints_json(self):
        r = self.run_hook([U1], {"hook_event_name": "stop", "status": "completed", "loop_count": 0})
        self.assertEqual(json.loads(r.stdout), {})

    def test_mcp_build_tool_checks_build_lock(self):
        holder = self.sleeper()
        d = os.path.join(self.home, "locks", "build.lock")
        os.makedirs(d)
        open(os.path.join(d, "owner"), "w").write(f"pid={holder.pid}\nmode=run\nsince=x\ncwd=/o\ncmd=x\n")
        r = self.run_hook([U1], {"hook_event_name": "PreToolUse", "tool_name": "mcp__xcode__BuildProject", "tool_input": {}})
        self.assertIn("Another session is building", r.stdout)


class Locks(Base):
    def test_multi_phone_acquire_run_release(self):
        self.env["DEVLOCK_FAKE_WIRED"] = f"{U1} {U2}"
        other = self.sleeper()
        self.hold(U1, other.pid)
        r = self.dl("acquire", "--name", "device", "--bundle", "com.x.app", env={"DEVLOCK_SESSION_PID": str(os.getpid()), "DEVLOCK_NO_CLEANUP": "1"})
        self.assertIn(f"export DEVLOCK_DEVICE={U2}", r.stdout)
        owner = open(os.path.join(self.home, "locks", f"device-{U2}.lock", "owner")).read()
        self.assertIn("bundle=com.x.app", owner)
        me = {"DEVLOCK_SESSION_PID": str(os.getpid())}
        self.assertEqual(self.dl("mine", "device", env=me).returncode, 0)
        self.assertEqual(self.dl("--name", "device", "--", "echo", "ran", env=me).stdout.strip(), "ran")
        self.assertIn(f"released 'device-{U2}'", self.dl("release", "--name", "device", env=me).stdout)
        self.assertIn("say which one", self.dl("--name", "device", "--", "echo", "x").stderr)

    def test_queue_then_acquire(self):
        p = subprocess.Popen([DL, "--name", "build", "--", "sleep", "3"], env=self.env)
        self.procs.append(p)
        time.sleep(0.5)
        t = time.time()
        r = self.dl("--name", "build", "--", "echo", "second")
        self.assertEqual(r.stdout.strip(), "second")
        self.assertGreater(time.time() - t, 1.5)

    def test_stale_and_expired_holds_taken_over(self):
        self.hold(U1, 999999)
        self.assertEqual(self.dl("--name", "device", "--", "echo", "a").stdout.strip(), "a")
        other = self.sleeper()
        self.hold(U1, other.pid, f"expires={int(time.time()) - 5}\n")
        r = self.dl("--name", "device", "--", "echo", "b", env={"DEVLOCK_TIMEOUT": "10"})
        self.assertEqual(r.stdout.strip(), "b")
        self.assertIn(f"taken from pid={other.pid}", open(os.path.join(self.home, "locks", "handoffs.log")).read())

    def test_nested_devlock_passes(self):
        r = self.dl("--name", "device", "--", DL, "--name", "device", "--", "echo", "nested")
        self.assertEqual(r.stdout.strip(), "nested")

    def test_guard_stops_loop_when_hold_lost(self):
        me = {"DEVLOCK_SESSION_PID": str(os.getpid()), "DEVLOCK_NO_CLEANUP": "1"}
        self.dl("acquire", "--name", "device", env=me)
        g = subprocess.Popen([DL, "guard", "--name", "device", "--", "bash", "-c", "while true; do sleep 1; done"],
                             env={**self.env, **me}, stderr=subprocess.PIPE, text=True)
        self.procs.append(g)
        time.sleep(2)
        shutil.rmtree(os.path.join(self.home, "locks", f"device-{U1}.lock"))
        g.wait(timeout=15)
        self.assertEqual(g.returncode, 75)
        self.assertIn("guard: lost", g.stderr.read())

    def test_awake_modes(self):
        me = {"DEVLOCK_SESSION_PID": str(os.getpid())}
        wanted = lambda: self.dl("awake", "wanted", "--udid", U1).returncode == 0
        self.assertTrue(wanted(), "default auto: keep awake")
        self.dl("awake", "never-lock", "yes", env=me)
        self.assertFalse(wanted(), "auto + declared never-lock: not needed")
        self.assertIn("declared: never auto-locks", self.dl("awake", "status").stdout)
        self.dl("awake", "on", env=me)
        self.assertTrue(wanted(), "on overrides the declaration")
        self.dl("awake", "off", env=me)
        self.assertFalse(wanted())
        self.dl("awake", "auto", env=me)
        self.assertFalse(wanted(), "back to auto: declaration still applies")
        self.dl("awake", "never-lock", "no", env=me)
        self.assertTrue(wanted())
        self.dl("awake", "pause", env=me)
        self.dl("awake", "resume", env=me)
        self.assertFalse(os.path.exists(os.path.join(self.home, "awake", f"state-{U1}", "keepawake.pause")))

    def test_awake_control(self):
        me = {"DEVLOCK_SESSION_PID": str(os.getpid())}
        self.dl("awake", "off", env=me)
        self.assertTrue(os.path.exists(os.path.join(self.home, "awake", f"{U1}.off")))
        self.assertIn("mode=off, not running", self.dl("awake", "status").stdout)
        self.dl("awake", "pause", env=me)
        self.assertTrue(os.path.exists(os.path.join(self.home, "awake", f"state-{U1}", "keepawake.pause")))
        self.dl("awake", "on", env=me)
        self.assertFalse(os.path.exists(os.path.join(self.home, "awake", f"{U1}.off")))
        other = self.sleeper()
        self.hold(U1, other.pid)
        self.assertIn("only the holder", self.dl("awake", "off", env=me).stderr)
        self.assertFalse(os.path.exists(os.path.join(self.home, "awake", f"{U1}.off")))


HOST = "Cursor Helper (Plugin): extension-host TestWindow [1-2]"


class CursorConversation(Base):
    """Cursor desktop runs every chat of a window under one extension-host process, a fresh bash per command;
    the conversation (CURSOR_CONVERSATION_ID) is the session and the host process only the liveness anchor."""

    def in_host(self, conv, script, background=False):
        """Run `script` (bash) as a child of a process whose command line reads like Cursor's extension host."""
        env = {**self.env, "CURSOR_CONVERSATION_ID": conv}
        env.pop("DEVLOCK_SESSION_PID", None)
        # script as an argument (no quoting); the trailing `exit $?` keeps bash from exec-ing a lone command in place
        # of the fake host, which would drop it from the ancestry (Cursor's own wrapper keeps the shell)
        cmd = ["bash", "-c", f'exec -a "{HOST}" bash -c "$0"', script + "\nexit $?"]
        if background:
            p = subprocess.Popen(cmd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.procs.append(p)
            return p
        return subprocess.run(cmd, env=env, capture_output=True, text=True)

    def lock(self):
        return os.path.join(self.home, "locks", f"device-{U1}.lock")

    def acquire_as(self, conv, extra=""):
        host = self.in_host(conv, f"DEVLOCK_NO_CLEANUP=1 {DL} acquire --name device {extra} >/dev/null; sleep 60", background=True)
        for _ in range(50):
            if os.path.exists(os.path.join(self.lock(), "owner")):
                break
            time.sleep(0.1)
        time.sleep(0.3)
        return host

    def test_conversations_in_one_window_are_separate_sessions(self):
        host = self.acquire_as("conv-A")
        owner = open(os.path.join(self.lock(), "owner")).read()
        self.assertIn("conv=conv-A", owner)
        self.assertIn(f"pid={host.pid}", owner)  # anchored on the (fake) extension host
        self.assertIn("conversation conv-A", self.in_host("conv-A", f"{DL} whoami").stdout)
        self.assertEqual(self.in_host("conv-A", f"{DL} mine device").returncode, 0)
        self.assertEqual(self.in_host("conv-B", f"{DL} mine device").returncode, 1)
        self.assertEqual(self.in_host("conv-B", f"{DL} check device-{U1}").returncode, 1)
        self.assertEqual(self.in_host("conv-A", f"{DL} --name device -- echo ok").stdout.strip(), "ok")
        r = self.in_host("conv-B", f"DEVLOCK_TIMEOUT=2 {DL} --name device -- echo no")
        self.assertNotIn("no", r.stdout)
        host.kill(); host.wait()
        self.assertEqual(self.in_host("conv-B", f"{DL} check device-{U1}").returncode, 0, "window closed: hold dies")

    def test_cursor_stop_releases_only_that_conversation(self):
        self.acquire_as("conv-A")
        stop_b = json.dumps({"hook_event_name": "stop", "conversation_id": "conv-B", "status": "completed"})
        self.in_host("conv-B", f"echo \'{stop_b}\' | python3 {HOOK}")
        self.assertTrue(os.path.isdir(self.lock()), "another conversation's stop must not release A")
        stop_a = json.dumps({"hook_event_name": "stop", "conversation_id": "conv-A", "status": "completed"})
        self.in_host("conv-B", f"echo \'{stop_a}\' | python3 {HOOK}")  # the hook speaks for the payload's conversation
        self.assertFalse(os.path.isdir(self.lock()))

    def test_ttl_hold_survives_cursor_stop(self):
        self.acquire_as("conv-A", "--ttl 2m")
        stop_a = json.dumps({"hook_event_name": "stop", "conversation_id": "conv-A", "status": "completed"})
        self.in_host("conv-A", f"echo \'{stop_a}\' | python3 {HOOK}")
        self.assertTrue(os.path.isdir(self.lock()))

    def test_cursor_hook_allows_holder_denies_other(self):
        self.acquire_as("conv-A")
        cmd = f"xcrun devicectl device info details --device {U1}"
        pa = json.dumps({"hook_event_name": "beforeShellExecution", "conversation_id": "conv-A", "command": cmd})
        pb = json.dumps({"hook_event_name": "beforeShellExecution", "conversation_id": "conv-B", "command": cmd})
        ra = self.in_host("conv-A", f"echo \'{pa}\' | python3 {HOOK}")
        rb = self.in_host("conv-A", f"echo \'{pb}\' | python3 {HOOK}")
        self.assertEqual(json.loads(ra.stdout)["permission"], "allow")
        self.assertEqual(json.loads(rb.stdout)["permission"], "deny")

    def test_cursor_cli_started_under_another_name(self):
        """A cloud runner may start the Cursor CLI as `agent`, a symlink to cursor-agent: still Cursor."""
        d = tempfile.mkdtemp()
        real = os.path.join(d, "versions", "cursor-agent")
        os.makedirs(os.path.dirname(real))
        # a native binary that runs its arguments and waits, standing in for the CLI (a script would show as bash;
        # a copied system binary is not allowed to run)
        src = os.path.join(d, "fake.c")
        open(src, "w").write("#include <unistd.h>\n#include <sys/wait.h>\nint main(int c, char **v) {"
                             " pid_t p = fork(); if (p == 0) { execvp(v[1], v + 1); return 127; }"
                             " int st; waitpid(p, &st, 0); return WEXITSTATUS(st); }\n")
        if subprocess.run(["cc", "-o", real, src], capture_output=True).returncode != 0:
            shutil.rmtree(d, ignore_errors=True)
            self.skipTest("needs a C compiler for the fake CLI binary")
        os.symlink(real, os.path.join(d, "agent"))
        env = {**self.env, "CURSOR_CONVERSATION_ID": "conv-T"}
        env.pop("DEVLOCK_SESSION_PID", None)
        r = subprocess.run([os.path.join(d, "agent"), "bash", "-c", f"{DL} whoami; exit $?"], capture_output=True, text=True, env=env)
        shutil.rmtree(d, ignore_errors=True)
        self.assertIn("cursor conversation conv-T", r.stdout, r.stderr)

    @unittest.skipUnless(agent_ancestor(), "needs a claude ancestor")
    def test_claude_ignores_inherited_cursor_conversation(self):
        r = subprocess.run([DL, "whoami"], capture_output=True, text=True, env={**self.env, "CURSOR_CONVERSATION_ID": "conv-X"})
        self.assertNotIn("conversation", r.stdout)


@unittest.skipUnless(NODE, "needs node for fake helper processes with a visible environment")
class Cleanup(Base):
    def test_handoff_cleanup_selects_previous_holders_helpers(self):
        mk = lambda argv, ident="1", **env: self.sleeper(argv, {"DEVLOCK_SESSION_PID": ident, **env})
        kill = [mk(f"node /x/appium/index.js server --port 1 --udid {U1}"),
                self.sleeper(f"xcodebuild test-without-building -project WebDriverAgent.xcodeproj -destination id={U1}"),
                mk(f"python3 /x/keepawake.py --udid {U1} keepawake --state-dir s"),
                mk(f"python3 /x/ios-build-device-lock/scripts/keepawake.py --udid {U1} run")]
        keep = [mk("node /x/appium/index.js server --port 2"),                        # phone unknown, 2 phones
                mk(f"node /x/appium/index.js server --port 3 --udid {U2}"),           # other phone
                mk(f"python3 /x/keepawake.py --udid {U1} keepawake --state-dir s", DEVLOCK_INFRA="1"),
                mk(f"python3 /x/keepawake.py --udid {U1} keepawake --state-dir {self.home}/awake/state-{U1}"),
                mk(f"node /x/appium/index.js server --port 4 --udid {U1}", ident="4242"),  # taker's own
                mk(f"node /x/appium/index.js server --port 5 --remote -u {U1}"),   # a remote phone's helper
                self.sleeper(f"idevicesyslog -u {U1}")]                            # passive: someone may be reading it
        time.sleep(0.5)
        subprocess.run([sys.executable, os.path.join(SCRIPTS, "cleanup.py"), U1, "4242", "2"], env=self.env, capture_output=True)
        time.sleep(1)
        for p in kill:
            self.assertIsNotNone(p.poll(), "should have been cleared")
        for p in keep:
            self.assertIsNone(p.poll(), "should have been kept")

    def test_acquire_clears_unnamed_appium_on_single_phone(self):
        a = self.sleeper("node /x/appium/index.js server --port 6", {"DEVLOCK_SESSION_PID": "1"})
        time.sleep(0.5)
        r = self.dl("acquire", "--name", "device", env={"DEVLOCK_SESSION_PID": str(os.getpid())})
        time.sleep(1)
        self.assertIsNotNone(a.poll())
        self.assertIn("cleared previous holder", r.stderr)


@unittest.skipUnless(DARWIN_XCRUN, "needs /usr/bin/xcrun")
class Shim(Base):
    def shim(self, env=None):
        return subprocess.run([SHIM, "devicectl", "device", "info", "details", "--device", U1],
                              capture_output=True, text=True, env={**self.env, **(env or {})})

    def test_refuses_and_lets_through(self):
        other = self.sleeper()
        self.hold(U1, other.pid)
        r = self.shim()
        self.assertEqual(r.returncode, 75)
        self.assertNotEqual(self.shim({"DEVLOCK_INFRA": "1"}).returncode, 75)
        self.assertNotEqual(self.shim({"DEVLOCK_SESSION_PID": str(other.pid)}).returncode, 75)
        self.assertEqual(self.shim({"DEVLOCK_SESSION_PID": "1"}).returncode, 75)

    def test_free_phone_per_call_lock(self):
        self.assertNotEqual(self.shim().returncode, 75)
        self.assertFalse(os.path.exists(os.path.join(self.home, "locks", f"device-{U1}.lock")))
        r = subprocess.run([SHIM, "--find", "clang"], capture_output=True, text=True, env=self.env)
        self.assertEqual(r.returncode, 0)

    def test_detached_caller_without_identity(self):
        other = self.sleeper()
        self.hold(U1, other.pid)
        out = os.path.join(self.home, "out")
        env = {k: v for k, v in self.env.items() if k != "DEVLOCK_SESSION_PID"}
        subprocess.run(["bash", "-c", f'( ( sleep 1; "{SHIM}" devicectl device info details --device {U1} > "{out}" 2>&1; touch "{out}.done" ) </dev/null & )'], env=env)
        for _ in range(50):
            if os.path.exists(out + ".done"):
                break
            time.sleep(0.2)
        self.assertIn("no agent session identity", open(out).read())


@unittest.skipUnless(agent_ancestor(), "Stop auto-release needs an agent (claude / cursor-agent) ancestor")
class StopHook(Base):
    def stop(self):
        subprocess.run([sys.executable, HOOK], input='{"hook_event_name":"Stop"}', capture_output=True, text=True, env=self.env)

    def test_plain_hold_released_ttl_kept(self):
        self.env["DEVLOCK_NO_CLEANUP"] = "1"
        lock = os.path.join(self.home, "locks", f"device-{U1}.lock")
        self.dl("acquire", "--name", "device", "--ttl", "2m")
        self.stop()
        self.assertTrue(os.path.isdir(lock))
        self.dl("release", "--name", "device")
        self.dl("acquire", "--name", "device")
        self.stop()
        self.assertFalse(os.path.isdir(lock))


class Refresh(unittest.TestCase):
    """refresh.py: never installs, reinstalls when the skill copy's content or location changed."""

    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.dl = os.path.join(self.home, ".devlock")
        sys.path.insert(0, SCRIPTS)
        import refresh
        self.refresh = refresh
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("DEVLOCK_")}
        self.env.update(HOME=self.home, DEVLOCK_HOME=self.dl)

    def tearDown(self):
        shutil.rmtree(self.home, ignore_errors=True)

    def run_refresh(self):
        return subprocess.run([sys.executable, os.path.join(SCRIPTS, "refresh.py"), "--dry-run"], capture_output=True,
                              text=True, env=self.env).stdout.strip()

    def fake_skill(self, path, marker):
        os.makedirs(os.path.join(path, "scripts"))
        for rel in self.refresh.FINGERPRINTED:
            f = os.path.join(path, rel)
            if rel.startswith("../"):
                continue
            os.makedirs(os.path.dirname(f), exist_ok=True)
            open(f, "w").write(marker)
        return os.path.realpath(path)

    def installed(self, skill_dir, fp):
        os.makedirs(self.dl, exist_ok=True)
        json.dump({"fingerprint": fp, "skill_dir": skill_dir}, open(os.path.join(self.dl, "config.json"), "w"))

    def test_not_installed_is_left_alone(self):
        self.assertIn("not installed", self.run_refresh())

    def test_fingerprint_tracks_content(self):
        a = self.fake_skill(os.path.join(self.home, "a", "ios-build-device-lock"), "v1")
        f1 = self.refresh.fingerprint(a)
        open(os.path.join(a, "scripts", "keepawake.py"), "w").write("v2")
        self.assertNotEqual(f1, self.refresh.fingerprint(a))

    def test_symlink_install_changed_content(self):
        real = self.fake_skill(os.path.join(self.home, "repo", "ios-build-device-lock"), "v1")
        os.makedirs(os.path.join(self.home, ".claude", "skills"))
        os.symlink(real, os.path.join(self.home, ".claude", "skills", "ios-build-device-lock"))
        self.installed(real, self.refresh.fingerprint(real))
        self.assertIn("up to date", self.run_refresh())
        open(os.path.join(real, "scripts", "devlock"), "w").write("v2")  # a git pull
        self.assertIn("would reinstall", self.run_refresh())

    def test_newest_marketplace_version_wins(self):
        base = os.path.join(self.home, ".claude", "plugins", "cache", "mkt", "cstack-ios")
        old = self.fake_skill(os.path.join(base, "2.0.1", "skills", "ios-build-device-lock"), "old")
        new = self.fake_skill(os.path.join(base, "2.10.0", "skills", "ios-build-device-lock"), "new")
        self.installed(old, self.refresh.fingerprint(old))
        out = self.run_refresh()
        self.assertIn("would reinstall", out)
        self.assertIn(new, out)


class Install(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.home, ".claude"))
        os.makedirs(os.path.join(self.home, ".cursor"))
        json.dump({"hooks": {"PreToolUse": [{"matcher": "Read", "hooks": [{"type": "command", "command": "other"}]}]}},
                  open(os.path.join(self.home, ".claude", "settings.json"), "w"))
        json.dump({"version": 1, "hooks": {"sessionStart": [{"command": "other"}]}},
                  open(os.path.join(self.home, ".cursor", "hooks.json"), "w"))
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("DEVLOCK_") and k != "CURSOR_CONFIG_DIR"}

    def tearDown(self):
        shutil.rmtree(self.home, ignore_errors=True)

    def inst(self, *extra):
        return subprocess.run([sys.executable, os.path.join(SCRIPTS, "install.py"), "--home", self.home, "--no-launchd", *extra],
                              capture_output=True, text=True, env=self.env)

    def test_install_idempotent_uninstall_keeps_others(self):
        legacy = os.path.join(self.home, ".claude", "devlock", "locks", "build.lock")
        os.makedirs(legacy)
        for _ in range(2):
            r = self.inst()
            self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(os.path.isdir(os.path.join(self.home, ".devlock", "locks", "build.lock")), "legacy lock migrated")
        self.assertTrue(os.path.islink(os.path.join(self.home, ".claude", "devlock")))
        c = json.load(open(os.path.join(self.home, ".claude", "settings.json")))["hooks"]
        self.assertEqual(len(c["PreToolUse"]), 2)  # other + ours, not duplicated
        self.assertEqual(len(c["Stop"]), 1)
        self.assertEqual(len(c["SessionStart"]), 2)  # identity + refresh
        self.assertTrue(any("refresh.py" in json.dumps(h) and h["hooks"][0].get("async") for h in c["SessionStart"]))
        u = json.load(open(os.path.join(self.home, ".cursor", "hooks.json")))["hooks"]
        self.assertEqual(len(u["beforeShellExecution"]), 1)
        self.assertEqual(len(u["stop"]), 1)
        self.assertEqual(u["sessionStart"][0], {"command": "other"})
        self.assertIn("refresh.py", u["sessionStart"][1]["command"])
        conf = json.load(open(os.path.join(self.home, ".devlock", "config.json")))
        self.assertRegex(conf["fingerprint"], r"^[0-9a-f]{12}$")
        self.assertTrue(os.path.islink(os.path.join(self.home, ".local", "bin", "xcrun")))
        self.assertEqual(self.inst("--check", "--no-launchd").returncode, 0)
        self.inst("--uninstall")
        c = json.load(open(os.path.join(self.home, ".claude", "settings.json")))["hooks"]
        self.assertEqual(c, {"PreToolUse": [{"matcher": "Read", "hooks": [{"type": "command", "command": "other"}]}]})
        u = json.load(open(os.path.join(self.home, ".cursor", "hooks.json")))["hooks"]
        self.assertEqual(u, {"sessionStart": [{"command": "other"}]})
        self.assertFalse(os.path.lexists(os.path.join(self.home, ".local", "bin", "devlock")))


if __name__ == "__main__":
    unittest.main()
