#!/usr/bin/env python3
"""Every relative link in the docs must point at a file that is actually there.

A skill once shipped carrying `[PLAN.md](PLAN.md)` — a link to a file that stayed behind
in the repo it was assembled from. It read as an invitation to go and look, and there was
nothing to look at. These docs are read by an agent *as instructions*, so a dead link is
not a typo: it is a next step that cannot be taken, and nothing errors to tell you.

It needs nothing but the files in front of it: a markdown link with a relative target is
a promise that the file is present, and the promise is checkable.

Deliberately narrow. Prose that merely names a file (`see findings.md`) is left alone,
because a doc may legitimately name files that live in someone's own checkout and were
never ours to ship. A link is different — someone put brackets round it.

    python3 tests/test_docs_resolve.py
"""

import os
import re
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGINS = os.path.join(REPO, "plugins")

# Links written inside code formatting are being *displayed*, not followed:
# tech-review's references explain the `[title](url)` shortcut, and `url` is not a
# file anyone expected to find. Stripping code first is what makes the rest safe.
FENCE = re.compile(r"```.*?```", re.S)
CODE = re.compile(r"`[^`\n]*`")
LINK = re.compile(r"\[[^\]^]*\]\(\s*([^)\s]+)")

EXTERNAL = ("http://", "https://", "mailto:", "//")


def markdown_files():
    """Skill docs, plus the top-level files that point into them."""
    for name in sorted(os.listdir(REPO)):
        if name.endswith(".md"):
            yield os.path.join(REPO, name)
    for dirpath, dirs, files in os.walk(PLUGINS):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for f in sorted(files):
            if f.endswith(".md"):
                yield os.path.join(dirpath, f)


def prose(text):
    return CODE.sub(" ", FENCE.sub("\n", text))


def links_in(text):
    for target in LINK.findall(prose(text)):
        if target.startswith(EXTERNAL) or target.startswith("#"):
            continue
        # `[<title>](<url>)` is a message template showing the shape of a line the
        # skill will emit, not a link to follow. scheduled.md is full of them. An
        # angle-bracketed target is a placeholder by convention everywhere in this
        # repo, and nothing real is ever named `<...>`.
        if target.startswith("<") and target.endswith(">"):
            continue
        # `notes.md#section` and `x.md?q=1` are still links to notes.md and x.md.
        clean = target.split("#")[0].split("?")[0]
        if clean:
            yield target, clean


def broken_links_in(path):
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    for target, clean in links_in(text):
        resolved = os.path.normpath(os.path.join(os.path.dirname(path), clean))
        if not os.path.exists(resolved):
            yield target


class LinksResolve(unittest.TestCase):
    def test_no_doc_links_to_a_file_that_is_not_there(self):
        seen, broken = 0, []
        for path in markdown_files():
            with open(path, encoding="utf-8") as fh:
                seen += len(list(links_in(fh.read())))
            rel = os.path.relpath(path, REPO)
            for target in broken_links_in(path):
                broken.append("%s: links to %s, which is not there" % (rel, target))

        # Without this the scan passes by finding nothing, which is how the first
        # draft of this file "passed" while walking a directory that did not exist.
        self.assertTrue(seen, "no links collected — the scan would pass vacuously")
        self.assertEqual([], sorted(set(broken)),
                         "\n  " + "\n  ".join(sorted(set(broken))) if broken else "")


class ParserBehaves(unittest.TestCase):
    """The parser has to be right or the test above is theatre."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def write(self, body):
        path = os.path.join(self.tmp.name, "notes.md")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(body)
        return path

    def test_it_would_have_caught_the_one_that_shipped(self):
        self.assertEqual(["PLAN.md"], list(broken_links_in(self.write(
            "See [the plan](PLAN.md) before starting.\n"))))

    def test_a_link_that_resolves_is_left_alone(self):
        self.assertEqual([], list(broken_links_in(self.write("[here](notes.md)\n"))))

    def test_a_placeholder_target_is_not_a_link(self):
        self.assertEqual([], list(links_in("1. [<title>](<url>) <mention>")))

    def test_link_syntax_shown_as_code_is_not_a_link(self):
        # schema.md: "hyperlink: `[Doc Title](url)`" — documenting the shortcut.
        self.assertEqual([], list(broken_links_in(self.write(
            "The shortcut parses `[a](b)`, and `[Doc Title](url)` renders as a link.\n"))))

    def test_fenced_examples_are_not_links_either(self):
        self.assertEqual([], list(broken_links_in(self.write(
            "```markdown\n[Recording](url)\n```\n"))))

    def test_external_and_in_document_targets_are_not_ours_to_check(self):
        self.assertEqual([], list(broken_links_in(self.write(
            "[a](https://x.example/a.md) [b](#later) [c](mailto:x@y.z)\n"))))

    def test_an_anchor_still_has_to_name_a_file_that_exists(self):
        self.assertEqual(["gone.md#top"], list(broken_links_in(self.write(
            "[x](gone.md#top) and [y](notes.md#top)\n"))))


if __name__ == "__main__":
    unittest.main(verbosity=2)
