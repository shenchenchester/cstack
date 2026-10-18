#!/usr/bin/env python3
"""The session-start updater runs at the start of every teammate's session, so it must be
fast, silent, and unable to damage a checkout. Pinned here against a throwaway clone
and remote:

- it returns at once and prints nothing — a hook's stdout lands in the session's context;
- it fast-forwards a clean checkout on a tracking branch, then re-links, so a skill that
  arrived with the pull is installed too;
- it never touches a dirty checkout, or a branch with no upstream, and says so in the log;
- it runs at most once per interval, and `--now` overrides that;
- with nothing installed it does nothing, and says so;
- a Cursor-only symlink install (no Claude skills dir) is still detected and updated;
- and the hook registrar edits only its own entry in Claude settings.json and Cursor
  hooks.json: registers, re-points, removes, leaves every other hook alone, and refuses
  a file that does not parse.

    python3 tests/test_session_update.py
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO, "scripts", "session-update.sh")
HOOK = os.path.join(REPO, "scripts", "session_hook.py")
INSTALL = os.path.join(REPO, "install.sh")


def git_env(home):
    return dict(os.environ, HOME=home, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1",
                GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid",
                GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid")


class Updater(unittest.TestCase):
    """A bare remote, an author clone that pushes releases, and an installed clone."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="session-update-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "home")
        self.state = os.path.join(self.tmp, "state")
        self.origin = os.path.join(self.tmp, "origin.git")
        self.author = os.path.join(self.tmp, "author")
        self.clone = os.path.join(self.tmp, "clone")
        os.makedirs(os.path.join(self.home, ".claude"))

        self.git(None, "init", "-q", "--bare", self.origin)
        self.git(self.origin, "symbolic-ref", "HEAD", "refs/heads/main")

        os.makedirs(os.path.join(self.author, "scripts"))
        shutil.copy(INSTALL, os.path.join(self.author, "install.sh"))
        shutil.copy(SCRIPT, os.path.join(self.author, "scripts", "session-update.sh"))
        shutil.copy(HOOK, os.path.join(self.author, "scripts", "session_hook.py"))
        self.write(self.author, "plugins/cstack-ios/plugin.json",
                   json.dumps({"name": "cstack-ios", "version": "1.0.0"}))
        self.skill(self.author, "ios-build-device-lock")
        self.git(self.author, "init", "-q")
        self.git(self.author, "symbolic-ref", "HEAD", "refs/heads/main")
        self.git(self.author, "add", "-A")
        self.git(self.author, "commit", "-q", "-m", "v1")
        self.git(self.author, "remote", "add", "origin", self.origin)
        self.git(self.author, "push", "-q", "-u", "origin", "main")

        self.git(None, "clone", "-q", self.origin, self.clone)
        subprocess.run(["bash", os.path.join(self.clone, "install.sh"), "--no-hook"],
                       check=True, capture_output=True, env=self.env())

    # -- helpers -----------------------------------------------------------------------

    def env(self, **extra):
        env = git_env(self.home)
        env.update(CSTACK_HOME=self.state, CLAUDE_CONFIG_DIR=os.path.join(self.home, ".claude"),
                   CURSOR_CONFIG_DIR=os.path.join(self.home, ".cursor"))
        env.update(extra)
        return env

    def git(self, cwd, *args):
        return subprocess.run(["git"] + list(args), cwd=cwd, check=True, capture_output=True,
                              text=True, env=git_env(self.home)).stdout.strip()

    def write(self, root, relpath, text):
        path = os.path.join(root, relpath)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)

    def skill(self, root, name):
        self.write(root, "plugins/cstack-ios/skills/%s/SKILL.md" % name,
                   "---\nname: %s\ndescription: x\n---\nbody\n" % name)

    def release(self, message, skill=None):
        """The author ships a change to origin."""
        if skill:
            self.skill(self.author, skill)
        else:
            self.write(self.author, "plugins/cstack-ios/skills/ios-build-device-lock/SKILL.md",
                       "---\nname: ios-build-device-lock\ndescription: x\n---\n%s\n" % message)
        self.git(self.author, "add", "-A")
        self.git(self.author, "commit", "-q", "-m", message)
        self.git(self.author, "push", "-q", "origin", "main")

    def run_script(self, *args, **extra_env):
        return subprocess.run(["bash", SCRIPT] + list(args), capture_output=True, text=True,
                              timeout=60, env=self.env(**extra_env))

    def log_lines(self):
        path = os.path.join(self.state, "session-update.log")
        if not os.path.exists(path):
            return []
        with open(path, encoding="utf-8") as fh:
            return [line.rstrip("\n") for line in fh if line.strip()]

    def wait_for(self, needle, seconds=20):
        """The work is detached; wait for its outcome line rather than for the process."""
        deadline = time.time() + seconds
        while time.time() < deadline:
            lines = self.log_lines()
            if any(needle in line for line in lines):
                return lines
            time.sleep(0.2)
        self.fail("no %r in the log after %ss: %r" % (needle, seconds, self.log_lines()))

    def head(self, repo):
        return self.git(repo, "rev-parse", "HEAD")

    # -- behaviour ---------------------------------------------------------------------

    def test_returns_at_once_and_prints_nothing(self):
        started = time.time()
        result = self.run_script("--now")
        elapsed = time.time() - started
        self.assertEqual(0, result.returncode)
        self.assertEqual("", result.stdout, "stdout would land in the session's context")
        self.assertEqual("", result.stderr)
        self.assertLess(elapsed, 5, "the hook must not wait on the pull")
        self.wait_for("UP_TO_DATE")

    def test_pulls_a_clean_tracking_checkout_and_relinks(self):
        self.release("v2 adds a skill", skill="new-skill")
        self.assertNotEqual(self.head(self.author), self.head(self.clone))
        self.assertEqual(0, self.run_script("--now").returncode)
        self.wait_for("UPDATED")
        self.assertEqual(self.head(self.author), self.head(self.clone))
        self.assertTrue(os.path.islink(os.path.join(self.home, ".claude", "skills", "new-skill")),
                        "a skill that arrived with the pull must be linked")

    def test_never_touches_a_dirty_checkout(self):
        self.release("v2")
        before = self.head(self.clone)
        edited = os.path.join(self.clone, "plugins/cstack-ios/skills/ios-build-device-lock/SKILL.md")
        with open(edited, "a", encoding="utf-8") as fh:
            fh.write("local edit in progress\n")
        self.assertEqual(0, self.run_script("--now").returncode)
        lines = self.wait_for("SKIP")
        self.assertTrue(any("reason=dirty" in line for line in lines), lines)
        self.assertEqual(before, self.head(self.clone))
        with open(edited, encoding="utf-8") as fh:
            self.assertIn("local edit in progress", fh.read())

    def test_never_touches_a_branch_without_an_upstream(self):
        self.git(self.clone, "checkout", "-q", "-b", "scratch")
        self.assertEqual(0, self.run_script("--now").returncode)
        lines = self.wait_for("SKIP")
        self.assertTrue(any("reason=no_upstream" in line for line in lines), lines)

    def test_throttles_to_one_attempt_per_interval(self):
        self.assertEqual(0, self.run_script("--now").returncode)
        lines = self.wait_for("UP_TO_DATE")
        count = len(lines)
        self.assertEqual(0, self.run_script().returncode)          # inside the interval
        time.sleep(1.5)
        self.assertEqual(count, len(self.log_lines()), "a throttled run must do nothing")
        self.assertEqual(0, self.run_script(CSTACK_UPDATE_INTERVAL="0").returncode)
        deadline = time.time() + 20
        while len(self.log_lines()) <= count and time.time() < deadline:
            time.sleep(0.2)
        self.assertGreater(len(self.log_lines()), count, "a zero interval must run again")

    def test_nothing_installed_is_a_silent_noop(self):
        shutil.rmtree(os.path.join(self.home, ".claude", "skills"))
        cursor_skills = os.path.join(self.home, ".cursor", "skills-cursor")
        if os.path.isdir(cursor_skills):
            shutil.rmtree(cursor_skills)
        result = self.run_script("--now")
        self.assertEqual(0, result.returncode)
        self.assertEqual("", result.stdout)
        self.wait_for("reason=no_install", seconds=5)

    def test_cursor_only_symlink_install_is_detected(self):
        """A machine that only ever opened Cursor still gets the git pull."""
        shutil.rmtree(os.path.join(self.home, ".claude", "skills"))
        os.makedirs(os.path.join(self.home, ".cursor"))
        subprocess.run(
            ["bash", os.path.join(self.clone, "install.sh"), "--no-hook"],
            check=True, capture_output=True,
            env=self.env(SKILLS_DIR=os.path.join(self.home, ".cursor", "skills-cursor")),
        )
        self.assertTrue(os.path.islink(os.path.join(self.home, ".cursor", "skills-cursor", "ios-build-device-lock")))
        self.release("v2 cursor-only", skill="cursor-only-skill")
        self.assertEqual(0, self.run_script("--now").returncode)
        self.wait_for("UPDATED")
        self.assertEqual(self.head(self.author), self.head(self.clone))
        self.assertTrue(
            os.path.islink(os.path.join(self.home, ".cursor", "skills-cursor", "cursor-only-skill")),
            "re-link must cover the Cursor skills dir the install used",
        )

    def test_status_names_the_install(self):
        result = self.run_script("--status")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("git", result.stdout)
        self.assertIn(os.path.realpath(self.clone), result.stdout)
        self.assertIn("clean", result.stdout)


class HookRegistrar(unittest.TestCase):
    OTHER = {"hooks": [{"type": "command", "command": "echo somebody else's hook"}]}
    CURSOR_OTHER = {"command": "echo somebody else's cursor hook"}

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="session-hook-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.cfg = os.path.join(self.tmp, "claude")
        self.cursor = os.path.join(self.tmp, "cursor")
        os.makedirs(self.cfg)
        os.makedirs(self.cursor)
        self.settings = os.path.join(self.cfg, "settings.json")
        self.hooks = os.path.join(self.cursor, "hooks.json")
        self.seed_claude({"permissions": {"allow": ["Bash(ls:*)"]},
                          "hooks": {"SessionStart": [dict(self.OTHER)],
                                    "PreToolUse": [{"matcher": "Bash",
                                                   "hooks": [{"type": "command", "command": "true"}]}]}})
        self.seed_cursor({"version": 1,
                          "hooks": {"sessionStart": [dict(self.CURSOR_OTHER)],
                                    "beforeShellExecution": [{"command": "true"}]}})
        self.other_script = os.path.join(self.tmp, "elsewhere", "scripts", "session-update.sh")
        os.makedirs(os.path.dirname(self.other_script))
        shutil.copy(SCRIPT, self.other_script)

    def seed_claude(self, doc):
        with open(self.settings, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(doc, indent=2) + "\n")

    def seed_cursor(self, doc):
        with open(self.hooks, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(doc, indent=2) + "\n")

    def load_claude(self):
        with open(self.settings, encoding="utf-8") as fh:
            return json.load(fh)

    def load_cursor(self):
        with open(self.hooks, encoding="utf-8") as fh:
            return json.load(fh)

    def run_hook(self, *args):
        return subprocess.run([sys.executable, HOOK] + list(args), capture_output=True, text=True,
                              env=dict(os.environ, CLAUDE_CONFIG_DIR=self.cfg,
                                       CURSOR_CONFIG_DIR=self.cursor))

    def ours_claude(self, doc):
        return [h for e in doc.get("hooks", {}).get("SessionStart", [])
                for h in e.get("hooks", []) if "session-update.sh" in h.get("command", "")]

    def ours_cursor(self, doc):
        return [h for h in doc.get("hooks", {}).get("sessionStart", [])
                if "session-update.sh" in h.get("command", "")]

    def backups(self, directory, prefix):
        return [f for f in os.listdir(directory) if f.startswith(prefix)]

    def test_registers_beside_other_hooks_and_backs_up(self):
        result = self.run_hook("install", "--script", SCRIPT)
        self.assertEqual(0, result.returncode, result.stderr)
        doc = self.load_claude()
        ours = self.ours_claude(doc)
        self.assertEqual(1, len(ours))
        self.assertIs(True, ours[0]["async"])
        self.assertIn('bash "%s"' % SCRIPT, ours[0]["command"])
        self.assertIn(self.OTHER, doc["hooks"]["SessionStart"])
        self.assertEqual("Bash", doc["hooks"]["PreToolUse"][0]["matcher"])
        self.assertEqual(["Bash(ls:*)"], doc["permissions"]["allow"])
        self.assertEqual(1, len(self.backups(self.cfg, "settings.json.bak.")))

        cursor = self.load_cursor()
        ours_c = self.ours_cursor(cursor)
        self.assertEqual(1, len(ours_c))
        self.assertIn('bash "%s"' % SCRIPT, ours_c[0]["command"])
        self.assertNotIn("async", ours_c[0])
        self.assertIn(self.CURSOR_OTHER, cursor["hooks"]["sessionStart"])
        self.assertEqual(1, cursor["version"])
        self.assertEqual(1, len(self.backups(self.cursor, "hooks.json.bak.")))

    def test_second_install_is_a_noop_without_a_backup(self):
        self.run_hook("install", "--script", SCRIPT)
        result = self.run_hook("install", "--script", SCRIPT)
        self.assertEqual(0, result.returncode)
        self.assertIn("unchanged", result.stdout)
        self.assertEqual(1, len(self.ours_claude(self.load_claude())))
        self.assertEqual(1, len(self.ours_cursor(self.load_cursor())))
        self.assertEqual(1, len(self.backups(self.cfg, "settings.json.bak.")))
        self.assertEqual(1, len(self.backups(self.cursor, "hooks.json.bak.")))

    def test_install_from_another_checkout_repoints_in_place(self):
        self.run_hook("install", "--script", SCRIPT)
        result = self.run_hook("install", "--script", self.other_script)
        self.assertEqual(0, result.returncode, result.stderr)
        ours = self.ours_claude(self.load_claude())
        self.assertEqual(1, len(ours))
        self.assertIn(self.other_script, ours[0]["command"])
        self.assertNotIn(SCRIPT, ours[0]["command"])
        ours_c = self.ours_cursor(self.load_cursor())
        self.assertEqual(1, len(ours_c))
        self.assertIn(self.other_script, ours_c[0]["command"])

    def test_remove_takes_only_ours(self):
        self.run_hook("install", "--script", SCRIPT)
        result = self.run_hook("remove")
        self.assertEqual(0, result.returncode, result.stderr)
        doc = self.load_claude()
        self.assertEqual([], self.ours_claude(doc))
        self.assertEqual([self.OTHER], doc["hooks"]["SessionStart"])
        self.assertIn("PreToolUse", doc["hooks"])
        cursor = self.load_cursor()
        self.assertEqual([], self.ours_cursor(cursor))
        self.assertEqual([self.CURSOR_OTHER], cursor["hooks"]["sessionStart"])
        self.assertIn("beforeShellExecution", cursor["hooks"])

    def test_status_reflects_registration(self):
        self.assertEqual(1, self.run_hook("status").returncode)
        self.run_hook("install", "--script", SCRIPT)
        result = self.run_hook("status")
        self.assertEqual(0, result.returncode)
        self.assertIn(SCRIPT, result.stdout)
        self.assertIn("Claude:", result.stdout)
        self.assertIn("Cursor:", result.stdout)

    def test_runtime_flag_limits_to_one_host(self):
        result = self.run_hook("install", "--script", SCRIPT, "--runtime", "cursor")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual([], self.ours_claude(self.load_claude()))
        self.assertEqual(1, len(self.ours_cursor(self.load_cursor())))

    def test_refuses_a_settings_file_that_does_not_parse(self):
        with open(self.settings, "w", encoding="utf-8") as fh:
            fh.write("{ not json")
        result = self.run_hook("install", "--script", SCRIPT, "--runtime", "claude")
        self.assertEqual(3, result.returncode)
        with open(self.settings, encoding="utf-8") as fh:
            self.assertEqual("{ not json", fh.read())
        self.assertEqual([], self.backups(self.cfg, "settings.json.bak."))

    def test_missing_settings_file_is_created(self):
        os.remove(self.settings)
        os.remove(self.hooks)
        result = self.run_hook("install", "--script", SCRIPT)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(1, len(self.ours_claude(self.load_claude())))
        self.assertEqual(1, len(self.ours_cursor(self.load_cursor())))
        self.assertEqual([], self.backups(self.cfg, "settings.json.bak."),
                         "nothing to back up when there was no file")
        self.assertEqual([], self.backups(self.cursor, "hooks.json.bak."))

    def test_skips_a_host_whose_config_dir_is_missing(self):
        shutil.rmtree(self.cursor)
        result = self.run_hook("install", "--script", SCRIPT)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("Cursor skipped", result.stdout)
        self.assertEqual(1, len(self.ours_claude(self.load_claude())))
        self.assertFalse(os.path.exists(self.hooks))


if __name__ == "__main__":
    unittest.main(verbosity=2)
