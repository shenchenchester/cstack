#!/usr/bin/env python3
"""Keep the registries in step with what is actually in plugins/.

    python3 scripts/sync_registry.py                    # rewrite all manifests
    python3 scripts/sync_registry.py --check            # exit 1 if they are out of date
    python3 scripts/sync_registry.py --dry-run          # print what would change
    python3 scripts/sync_registry.py --gate origin/main # CI: exit 1 if a changed plugin
                                                        # did not bump its version

Three files decide what the outside world can install, and none updates itself:

    .claude-plugin/marketplace.json    Claude Code / the plugin marketplace
    .cursor-plugin/marketplace.json    Cursor local plugin marketplace

Both are whitelists. That is the platform's design and it is a good one — it stops a
half-finished directory from publishing itself. But the failure it produces is silent:
a finished skill sits in the repo and simply never reaches anyone, with no error to
notice. It has happened; that is why this script and `tests/test_registry_in_sync.py`
exist.

So the whitelists stay, and this keeps them honest. Publishing is still a decision —
you run this, or the test tells you that you forgot.

A plugin with no skills is skipped on purpose: an empty plugin is noise in a marketplace.
"""

import argparse
import json
import os
import re
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGINS = os.path.join(REPO, "plugins")
CLAUDE_MARKETPLACE = os.path.join(REPO, ".claude-plugin", "marketplace.json")
CURSOR_MARKETPLACE = os.path.join(REPO, ".cursor-plugin", "marketplace.json")
# Alias kept so older call sites / docs that say MARKETPLACE still resolve.
MARKETPLACE = CLAUDE_MARKETPLACE

def discover():
    """{plugin_name: {"manifest": {...}, "skills": [...]}} for plugins that have skills."""
    found = {}
    if not os.path.isdir(PLUGINS):
        return found
    for name in sorted(os.listdir(PLUGINS)):
        base = os.path.join(PLUGINS, name)
        manifest = os.path.join(base, "plugin.json")
        skills_dir = os.path.join(base, "skills")
        if not (os.path.isfile(manifest) and os.path.isdir(skills_dir)):
            continue
        skills = sorted(
            d for d in os.listdir(skills_dir)
            if os.path.isfile(os.path.join(skills_dir, d, "SKILL.md"))
        )
        if not skills:
            continue  # an empty plugin is noise in a marketplace
        with open(manifest, encoding="utf-8") as fh:
            found[name] = {"manifest": json.load(fh), "skills": skills}
    return found


def build_marketplace(found, path):
    """Rewrite one marketplace.json (Claude or Cursor). Same plugin list; preserve
    hand-written top-level fields and per-plugin extras that live only on that file."""
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    else:
        doc = {"name": "cstack", "owner": {"name": "shenchenchester"}}
    by_name = {p["name"]: p for p in doc.get("plugins", [])}
    plugins = []
    for name, info in found.items():
        entry = dict(by_name.get(name, {}))          # keep hand-written fields
        entry["name"] = name
        entry["source"] = "./plugins/%s" % name
        entry.setdefault("category", "Domain")
        entry["description"] = info["manifest"].get("description", entry.get("description", ""))
        plugins.append(entry)
    doc["plugins"] = plugins
    return json.dumps(doc, indent=2, ensure_ascii=False) + "\n"


def bump_version(plugin, level):
    """Raise a plugin's version. The platform releases on a version change, so a
    change shipped without one reaches nobody — the same silent failure as forgetting
    to register, one step later in the pipeline."""
    path = os.path.join(PLUGINS, plugin, "plugin.json")
    if not os.path.isfile(path):
        print("No such plugin: %s" % plugin, file=sys.stderr)
        return None
    with open(path, encoding="utf-8") as fh:
        doc = json.load(fh)
    parts = [int(x) for x in str(doc.get("version", "0.0.0")).split(".")]
    while len(parts) < 3:
        parts.append(0)
    idx = {"major": 0, "minor": 1, "patch": 2}[level]
    parts[idx] += 1
    for i in range(idx + 1, 3):
        parts[i] = 0
    before, doc["version"] = doc.get("version"), ".".join(str(x) for x in parts)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    print("  %-26s %s -> %s" % (plugin, before, doc["version"]))
    return doc["version"]


def changed_since_version_bump(plugin):
    """True if the plugin has commits touching it after its plugin.json last changed.

    A heuristic, and deliberately advisory rather than fatal: mid-development you often
    have unreleased changes on purpose. It exists to catch the case where a finished,
    merged change never reaches anyone because the version stayed put.
    """
    manifest = "plugins/%s/plugin.json" % plugin
    try:
        last = subprocess.run(["git", "log", "-1", "--format=%H", "--", manifest],
                              capture_output=True, text=True, cwd=REPO).stdout.strip()
        if not last:
            return False
        out = subprocess.run(["git", "diff", "--quiet", last, "HEAD", "--",
                              "plugins/%s" % plugin], cwd=REPO, capture_output=True)
        return out.returncode == 1
    except (OSError, subprocess.SubprocessError):
        return False


def _git(*args):
    return subprocess.run(["git"] + list(args), cwd=REPO, capture_output=True, text=True)


def version_key(version):
    """'1.13.0' -> (1, 13, 0); anything unparseable sorts lowest."""
    numbers = re.findall(r"\d+", str(version or ""))
    return tuple(int(n) for n in numbers) if numbers else (-1,)


def gate(base, found):
    """Fail if a plugin changed against `base` without its version going up.

    The CI form of the note `--check` prints (see changed_since_version_bump): locally
    that stays advisory, because mid-development you have unreleased changes by design.
    A merge request is a finished change, so .codebase/pipelines/version-gate.yml runs
    this against origin/main and lets it fail. Compares from the merge base, so commits
    that landed on `base` after the branch was cut are not charged to the branch.
    """
    if _git("rev-parse", "--verify", "--quiet", base + "^{commit}").returncode != 0:
        print("version gate: cannot resolve %s — fetch it first (git fetch origin main)"
              % base, file=sys.stderr)
        return 2
    merge_base = _git("merge-base", base, "HEAD").stdout.strip()
    if not merge_base:
        print("version gate: no merge base with %s (shallow clone?), comparing against "
              "its tip instead" % base, file=sys.stderr)
        merge_base = base

    changed, failures = [], []
    for name, info in found.items():
        if _git("diff", "--quiet", merge_base, "HEAD", "--", "plugins/%s" % name).returncode == 0:
            continue
        head_version = info["manifest"].get("version")
        shown = _git("show", "%s:plugins/%s/plugin.json" % (merge_base, name))
        if shown.returncode != 0:
            changed.append((name, None, head_version))       # new plugin: nothing to beat
            continue
        try:
            base_version = json.loads(shown.stdout).get("version")
        except ValueError:
            base_version = None
        changed.append((name, base_version, head_version))
        if version_key(head_version) <= version_key(base_version):
            failures.append(name)

    for name, base_version, head_version in changed:
        print("  %-26s %s -> %s%s" % (name, base_version or "(new)", head_version,
                                      "   NOT BUMPED" if name in failures else ""))
    if failures:
        print("\nChanged against %s without a version bump: %s" % (base, ", ".join(failures)),
              file=sys.stderr)
        print("Publishing releases on a version change, so this would reach nobody. Run:\n"
              "  python3 scripts/sync_registry.py --bump <plugin>          # or --level minor",
              file=sys.stderr)
        return 1
    print("Version gate passed against %s (%d plugin(s) changed)" % (base, len(changed)))
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true", help="exit 1 if the manifests are stale")
    ap.add_argument("--dry-run", action="store_true", help="print the diff, write nothing")
    ap.add_argument("--bump", metavar="PLUGIN",
                    help="raise this plugin's version before syncing (publishing needs it)")
    ap.add_argument("--level", default="patch", choices=("major", "minor", "patch"),
                    help="which part --bump raises (default: patch)")
    ap.add_argument("--gate", metavar="BASE",
                    help="exit 1 if a plugin changed against BASE without a higher version (CI)")
    args = ap.parse_args()

    if args.bump and bump_version(args.bump, args.level) is None:
        return 1

    found = discover()
    if not found:
        print("No plugins with skills found under plugins/", file=sys.stderr)
        return 1

    if args.gate:
        return gate(args.gate, found)

    targets = [
        (CLAUDE_MARKETPLACE, build_marketplace(found, CLAUDE_MARKETPLACE)),
        (CURSOR_MARKETPLACE, build_marketplace(found, CURSOR_MARKETPLACE)),
    ]
    stale = []
    for path, want in targets:
        have = open(path, encoding="utf-8").read() if os.path.exists(path) else ""
        if have != want:
            stale.append(os.path.relpath(path, REPO))

    if args.check:
        if stale:
            print("Registry out of date: %s" % ", ".join(stale), file=sys.stderr)
            print("Run: python3 scripts/sync_registry.py", file=sys.stderr)
            return 1
        print("Registry up to date (%d plugin(s), %d skill(s))"
              % (len(found), sum(len(i["skills"]) for i in found.values())))
        unreleased = [p for p in found if changed_since_version_bump(p)]
        if unreleased:
            print("\nNote: changed since their last version bump — %s" % ", ".join(unreleased))
            print("Publishing releases on a version change, so bump before you expect "
                  "users to see this:\n  python3 scripts/sync_registry.py --bump <plugin>")
        return 0

    if args.dry_run:
        print("Would update: %s" % (", ".join(stale) or "nothing"))
        return 0

    for path, want in targets:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(want)
    for name, info in found.items():
        print("  %-26s %s" % (name, ", ".join(info["skills"])))
    print("Updated: %s" % (", ".join(stale) or "nothing (already in sync)"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
