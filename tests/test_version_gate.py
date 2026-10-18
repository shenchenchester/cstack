#!/usr/bin/env python3
"""A merge request that changes a plugin must also raise its version, or it reaches nobody.

`sync_registry.py --check` only warns about this locally, on purpose. `--gate <base>` is
the CI form (`.codebase/pipelines/version-gate.yml`), and it fails. This drives it
against a throwaway git repo so the rule is pinned: changed without a bump fails, bumped
passes, a *lower* version fails, an untouched plugin owes nothing, a change outside
plugins/ owes nothing, a brand-new plugin owes nothing, and commits that landed on the
base after the branch was cut are not charged to the branch.

The script is copied into the throwaway repo rather than pointed at it, because it
locates the repo from its own path — so this exercises the real code path.

    python3 tests/test_version_gate.py
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SYNC = os.path.join(REPO, "scripts", "sync_registry.py")


def git_env(home):
    return dict(os.environ, HOME=home, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1",
                GIT_AUTHOR_NAME="gate", GIT_AUTHOR_EMAIL="gate@example.invalid",
                GIT_COMMITTER_NAME="gate", GIT_COMMITTER_EMAIL="gate@example.invalid")


class VersionGate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="version-gate-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(os.path.join(self.repo, "scripts"))
        shutil.copy(SYNC, os.path.join(self.repo, "scripts", "sync_registry.py"))
        self.write("README.md", "# throwaway\n")
        self.write(".claude-plugin/marketplace.json", json.dumps({"name": "t", "plugins": []}))
        self.write(".cursor-plugin/marketplace.json", json.dumps({"name": "t", "plugins": []}))
        self.plugin("p", "1.0.0")
        self.plugin("q", "2.0.0")
        self.git("init", "-q")
        self.git("symbolic-ref", "HEAD", "refs/heads/main")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "base")
        self.git("checkout", "-q", "-b", "feature")

    # -- fixture helpers -------------------------------------------------------------

    def git(self, *args):
        return subprocess.run(["git"] + list(args), cwd=self.repo, check=True,
                              capture_output=True, text=True, env=git_env(self.tmp)).stdout

    def write(self, relpath, text):
        path = os.path.join(self.repo, relpath)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)

    def plugin(self, name, version):
        self.write("plugins/%s/plugin.json" % name,
                   json.dumps({"name": name, "version": version, "description": name}))
        self.write("plugins/%s/skills/%s-skill/SKILL.md" % (name, name),
                   "---\nname: %s-skill\ndescription: x\n---\nbody\n" % name)

    def set_version(self, name, version):
        self.write("plugins/%s/plugin.json" % name,
                   json.dumps({"name": name, "version": version, "description": name}))

    def commit(self, message):
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)

    def gate(self, base="main"):
        return subprocess.run([sys.executable, os.path.join(self.repo, "scripts", "sync_registry.py"),
                               "--gate", base], cwd=self.repo, capture_output=True, text=True,
                              env=git_env(self.tmp))

    # -- the rule --------------------------------------------------------------------

    def test_change_without_bump_fails_and_names_the_plugin(self):
        self.write("plugins/p/skills/p-skill/SKILL.md", "changed\n")
        self.commit("edit p")
        result = self.gate()
        self.assertEqual(1, result.returncode, result.stdout + result.stderr)
        self.assertIn("p", result.stderr)
        self.assertIn("--bump", result.stderr)
        self.assertIn("NOT BUMPED", result.stdout)

    def test_bump_passes(self):
        self.write("plugins/p/skills/p-skill/SKILL.md", "changed\n")
        self.set_version("p", "1.0.1")
        self.commit("edit and bump p")
        result = self.gate()
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("1.0.0 -> 1.0.1", result.stdout)

    def test_lower_version_fails(self):
        self.write("plugins/p/skills/p-skill/SKILL.md", "changed\n")
        self.set_version("p", "0.9.9")
        self.commit("edit p, version goes backwards")
        self.assertEqual(1, self.gate().returncode)

    def test_same_version_written_differently_still_fails(self):
        self.write("plugins/p/skills/p-skill/SKILL.md", "changed\n")
        self.set_version("p", "1.0")
        self.commit("edit p, version restyled")
        self.assertEqual(1, self.gate().returncode)

    def test_untouched_plugin_owes_nothing(self):
        self.write("plugins/p/skills/p-skill/SKILL.md", "changed\n")
        self.set_version("p", "1.1.0")
        self.commit("edit and bump p only")
        result = self.gate()
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertNotIn("q", result.stdout.split("Version gate")[0])

    def test_change_outside_plugins_owes_nothing(self):
        self.write("README.md", "# edited\n")
        self.commit("docs")
        result = self.gate()
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("0 plugin(s) changed", result.stdout)

    def test_new_plugin_owes_nothing(self):
        self.plugin("r", "0.1.0")
        self.commit("add r")
        result = self.gate()
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("(new) -> 0.1.0", result.stdout)

    def test_base_commits_after_the_branch_are_not_charged_to_it(self):
        self.write("README.md", "# branch work\n")
        self.commit("branch: docs only")
        self.git("checkout", "-q", "main")
        self.write("plugins/q/skills/q-skill/SKILL.md", "main moved on\n")
        self.commit("main: edit q without a bump")
        self.git("checkout", "-q", "feature")
        result = self.gate()
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)

    def test_unresolvable_base_is_an_error_not_a_pass(self):
        result = self.gate("no-such-ref")
        self.assertEqual(2, result.returncode)
        self.assertIn("cannot resolve", result.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
