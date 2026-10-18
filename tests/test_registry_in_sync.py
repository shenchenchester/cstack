#!/usr/bin/env python3
"""A skill in plugins/ must be registered, or it publishes to nobody.

The registries are whitelists, which is right — it stops a half-finished directory
publishing itself. But the failure it produces is silent: the skill sits in the repo,
everything looks fine, and it simply never reaches anyone. That happened to
`trace-optimization-assistant`, which was merged and then invisible until someone
went looking.

This is the noisy half of that trade. The whitelist still decides; forgetting to
update it is now a failing test instead of a discovery weeks later.

    python3 tests/test_registry_in_sync.py
"""

import os
import subprocess
import sys
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SYNC = os.path.join(REPO, "scripts", "sync_registry.py")


class RegistryIsInSync(unittest.TestCase):
    def test_every_skill_is_registered(self):
        result = subprocess.run(
            [sys.executable, SYNC, "--check"],
            capture_output=True, text=True, cwd=REPO,
        )
        self.assertEqual(
            0, result.returncode,
            "\n" + (result.stdout + result.stderr).strip() +
            "\n\nA skill exists in plugins/ that no registry lists, so it cannot be "
            "installed or published. Run: python3 scripts/sync_registry.py",
        )

    def test_the_checker_would_actually_notice(self):
        """A --check that cannot fail is not a check."""
        import json
        for marketplace in (
            os.path.join(REPO, ".claude-plugin", "marketplace.json"),
            os.path.join(REPO, ".cursor-plugin", "marketplace.json"),
        ):
            with open(marketplace, encoding="utf-8") as fh:
                original = fh.read()
            try:
                doc = json.loads(original)
                doc["plugins"] = doc["plugins"][:-1]          # drop one, simulating a forgotten skill
                with open(marketplace, "w", encoding="utf-8") as fh:
                    json.dump(doc, fh, indent=2, ensure_ascii=False)
                result = subprocess.run(
                    [sys.executable, SYNC, "--check"],
                    capture_output=True, text=True, cwd=REPO,
                )
                self.assertNotEqual(0, result.returncode,
                                    "--check passed on a stale %s" % marketplace)
            finally:
                with open(marketplace, "w", encoding="utf-8") as fh:
                    fh.write(original)


if __name__ == "__main__":
    unittest.main(verbosity=2)
