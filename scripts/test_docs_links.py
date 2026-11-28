"""Link check for README.md and docs/*.md.

Every Markdown link other than http, https and mailto must be relative, point at a file that
exists, and (when it has an anchor) at a heading that exists. Links in fenced code blocks are
ignored. The same files ship to adopter projects, so a link must not point at a file the export
manifest excludes, except for files that bootstrap creates in every adopter project
(BOOTSTRAP_CREATED): those are absent in the export and in an adopter project until bootstrap
creates them, so the existence check skips them. Docs the export excludes are not checked for
links to excluded files.

The link syntax, anchor slugs and manifest check are shared with test_agents_index.py.
"""

import re
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.parse import unquote

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_agents_index import FENCE, LINK, ROOT, anchors, manifest_excluded  # noqa: E402

EXTERNAL = re.compile(r"^(https?|mailto):")
BOOTSTRAP_CREATED = {"ROADMAP.md", "project-context.toml", ".specify/memory/constitution.md"}


def doc_files():
    return [ROOT / "README.md", *sorted((ROOT / "docs").glob("*.md"))]


def links(path):
    """(line number, target) for each inline link outside fenced code blocks."""
    fence = None
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if m := FENCE.match(line):
            fence = None if fence == m.group(1) else (fence or m.group(1))
        elif not fence:
            for target in LINK.findall(line):
                if not EXTERNAL.match(target):
                    yield number, target


def resolve(path, target, root=ROOT):
    """The repository-relative path a link points at (the file itself for a bare anchor).

    None when the link leaves the repository.
    """
    file_part = unquote(target.partition("#")[0])
    if not file_part:
        return path.relative_to(root).as_posix()
    try:
        return (path.parent / file_part).resolve().relative_to(root).as_posix()
    except ValueError:
        return None


def excluded_link_failures(paths, root=ROOT, excluded=manifest_excluded):
    """Failure messages for links that leave the repository or point at export-excluded files.

    Source docs that the export excludes are skipped: a maintainer-only doc may link to them.
    `excluded` is a predicate taking a repository-relative path and returning True when the
    export excludes it; it defaults to the real manifest, which the export itself does not ship.
    """
    failures = []
    for path in paths:
        source = path.relative_to(root).as_posix()
        if excluded(source):
            continue
        for number, target in links(path):
            where = f"{source}:{number} -> {target}"
            rel = resolve(path, target, root)
            if rel is None:
                failures.append(f"{where}: outside the repository")
            elif rel not in BOOTSTRAP_CREATED and excluded(rel):
                failures.append(f"{where}: links to {rel}, excluded from the export")
    return failures


class DocsLinksTest(unittest.TestCase):
    def test_files_are_found(self):
        names = {p.relative_to(ROOT).as_posix() for p in doc_files()}
        self.assertIn("README.md", names)
        self.assertIn("docs/bootstrap.md", names)

    def test_links_are_relative_and_exist(self):
        for path in doc_files():
            for number, target in links(path):
                where = f"{path.relative_to(ROOT)}:{number} -> {target}"
                self.assertNotRegex(target, r"^[a-z][a-z0-9+.-]*:|^/", f"not relative: {where}")
                rel = resolve(path, target)
                self.assertIsNotNone(rel, f"outside the repository: {where}")
                if rel in BOOTSTRAP_CREATED:
                    continue
                self.assertTrue((ROOT / rel).exists(), f"missing target: {where}")

    def test_anchors_match_headings(self):
        for path in doc_files():
            for number, target in links(path):
                anchor = target.partition("#")[2]
                rel = resolve(path, target)
                where = f"{path.relative_to(ROOT)}:{number} -> {target}"
                if not anchor or rel in BOOTSTRAP_CREATED:
                    continue
                self.assertEqual(Path(rel).suffix, ".md", f"anchor on non-Markdown: {where}")
                self.assertIn(anchor, anchors(ROOT / rel), f"no heading for anchor: {where}")

    def test_no_link_to_an_export_excluded_file(self):
        self.assertEqual(excluded_link_failures(doc_files()), [])

    def test_no_link_form_is_skipped(self):
        for path in doc_files():
            fence = None
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if m := FENCE.match(line):
                    fence = None if fence == m.group(1) else (fence or m.group(1))
                    continue
                if fence:
                    continue
                leftover = re.sub(r"`[^`]*`", "", LINK.sub("", line))
                for form in ("](", "]["):
                    self.assertNotIn(form, leftover, f"unparsed link form at {path.relative_to(ROOT)}:{number}")


class HelperTest(unittest.TestCase):
    def make_docs(self, files):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name).resolve()
        (root / "docs").mkdir()
        for name, text in files.items():
            (root / name).write_text(text, encoding="utf-8")
        return root

    def test_export_excluded_source_doc_may_link_to_excluded_files(self):
        root = self.make_docs(
            {
                "docs/publishing.md": "[self](publishing.md)\n",
                "docs/guide.md": "[bad](publishing.md)\n",
            }
        )
        def fake(rel):
            return rel == "docs/publishing.md"

        self.assertEqual(
            excluded_link_failures([root / "docs/publishing.md"], root, fake),
            [],
            "excluded doc skipped",
        )
        failures = excluded_link_failures([root / "docs/guide.md"], root, fake)
        self.assertEqual(len(failures), 1)
        self.assertIn("docs/guide.md:1", failures[0])
        self.assertIn("excluded from the export", failures[0])

    def test_bootstrap_created_file_may_be_linked_even_when_excluded(self):
        root = self.make_docs({"docs/guide.md": "[roadmap](../ROADMAP.md)\n"})

        def fake(rel):
            return rel == "ROADMAP.md"

        self.assertEqual(excluded_link_failures([root / "docs/guide.md"], root, fake), [])

    def test_percent_encoded_target_resolves(self):
        root = self.make_docs({"docs/guide.md": "[x](foo%20bar.md)\n", "docs/foo bar.md": ""})
        self.assertEqual(resolve(root / "docs/guide.md", "foo%20bar.md", root), "docs/foo bar.md")

    def test_link_leaving_the_repository_is_a_named_failure(self):
        root = self.make_docs({"docs/guide.md": "[out](../../outside.md)\n"})
        self.assertIsNone(resolve(root / "docs/guide.md", "../../outside.md", root))
        failures = excluded_link_failures([root / "docs/guide.md"], root, lambda rel: False)
        self.assertEqual(len(failures), 1)
        self.assertIn("docs/guide.md:1 -> ../../outside.md", failures[0])
        self.assertIn("outside the repository", failures[0])


if __name__ == "__main__":
    unittest.main()
