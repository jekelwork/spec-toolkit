"""End-to-end check: an export of the toolkit is uninitialized and its own checks pass.

The export is built from the working tree: tracked changes plus every untracked, non-ignored
file, so an unclassified new file fails here and uncommitted work is tested. In CI the working
tree is the checked-out commit, so it exports exactly that commit.
"""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import export_toolkit  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
GIT_IDENTITY = ("-c", "user.name=Test", "-c", "user.email=test@example.invalid")
EXCLUDED_FILES = (
    "project-context.toml",
    "ROADMAP.md",
    ".specify/memory/constitution.md",
    "specs",
    "scripts/export_toolkit.py",
    "scripts/toolkit-export.toml",
    "scripts/test_export_toolkit.py",
    "scripts/test_fresh_export.py",
    "scripts/test_toolkit_source_workflow.py",
    "docs/publishing.md",
    ".github/workflows/toolkit-source.yml",
)


def clean_env(**extra):
    """Environment without GIT_* so a git hook cannot point children at the real repository."""
    return {**{k: v for k, v in os.environ.items() if not k.startswith("GIT_")}, **extra}


def git(repo, *args, env=None):
    result = subprocess.run(
        ["git", "-c", "core.fsmonitor=false", "--literal-pathspecs", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
        env=env or clean_env(),
    )
    return result.stdout.rstrip("\n")


def working_tree_commit(scratch):
    """Commit the working tree into a scratch repository and return (repository, commit).

    The new objects (including untracked files) go to the scratch repository, which reads the
    real repository's objects through an alternate; the real object store, index and refs are
    never written. Every untracked, non-ignored file is added, so the exporter's own
    unclassified-path check also runs for files that are not committed yet.
    """
    source = Path(scratch) / "source"
    source.mkdir()
    git(source, "init", "-q")
    objects = git(REPO, "rev-parse", "--path-format=absolute", "--git-path", "objects")
    (source / ".git" / "objects" / "info" / "alternates").write_text(objects + "\n", encoding="utf-8")
    env = clean_env(
        GIT_DIR=str(source / ".git"),
        GIT_WORK_TREE=str(REPO),
        GIT_INDEX_FILE=str(Path(scratch) / "index"),
    )
    git(REPO, "read-tree", git(REPO, "rev-parse", "HEAD"), env=env)
    git(REPO, "add", "--update", env=env)
    untracked = git(REPO, "ls-files", "-z", "--others", "--exclude-standard").split("\0")
    for path in filter(None, untracked):
        git(REPO, "add", "--", path, env=env)
    # The export reads its manifest from the exported commit; the manifest excludes itself.
    git(REPO, "add", "--", export_toolkit.DEFAULT_MANIFEST, env=env)
    tree = git(REPO, "write-tree", env=env)
    commit = git(REPO, *GIT_IDENTITY, "commit-tree", tree, "-m", "working tree", env=env)
    return source, commit


def export_head(scratch):
    """Export the working tree's commit into scratch/export, initialise it as a repository, return its path."""
    export = Path(scratch) / "export"
    source, commit = working_tree_commit(scratch)
    result = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "export_toolkit.py"), "--repo", str(source),
         "--ref", commit, "--output", str(export)],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
        env=clean_env(),
    )
    if result.returncode != 0:
        raise AssertionError(f"the export failed: {result.stderr}")
    git(export, "init", "-q")
    git(export, "add", "-A")
    git(export, *GIT_IDENTITY, "commit", "-q", "-m", "Fresh export")
    return export


class FreshExportTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.scratch = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.scratch.cleanup)
        cls.export = export_head(cls.scratch.name)

    def run_script(self, *args):
        return subprocess.run(
            [sys.executable, *args],
            cwd=self.export,
            capture_output=True,
            text=True,
            check=False,
            env=clean_env(),
        )

    def test_the_export_is_uninitialized(self):
        result = self.run_script("scripts/project_context.py", "--bootstrap-state")
        self.assertEqual((result.returncode, result.stdout), (0, "uninitialized\n"), result.stderr)

    def test_the_export_has_none_of_the_excluded_files(self):
        for name in EXCLUDED_FILES:
            self.assertFalse((self.export / name).exists(), name)

    def test_the_export_ships_what_adopters_need(self):
        for name in ("README.md", "docs/bootstrap.md", "project-context.example.toml",
                     "scripts/bootstrap.py", ".github/workflows/toolkit.yml",
                     ".github/actions/board-token/action.yml"):
            self.assertTrue((self.export / name).is_file(), name)
        self.assertTrue((self.export / ".claude" / "skills").is_symlink())

    def test_the_exports_unit_tests_pass(self):
        result = self.run_script("-m", "unittest", "discover", "-s", "scripts", "-p", "test_*.py")
        self.assertEqual(result.returncode, 0, result.stderr[-3000:])

    def test_the_exports_agent_setup_check_passes(self):
        result = self.run_script("scripts/agent_setup.py", "check")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class RealRepositoryUntouchedTest(unittest.TestCase):
    def test_building_the_commit_writes_no_objects_to_the_real_repository(self):
        before = git(REPO, "count-objects", "-v")
        with tempfile.TemporaryDirectory() as directory:
            working_tree_commit(directory)
        self.assertEqual(git(REPO, "count-objects", "-v"), before)


class GitEnvironmentTest(unittest.TestCase):
    def test_the_callers_git_variables_never_redirect_the_export(self):
        with tempfile.TemporaryDirectory() as directory:
            victim = Path(directory) / "victim"
            victim.mkdir()
            git(victim, "init", "-q")
            before = git(victim, "count-objects")
            hostile = {
                "GIT_DIR": str(victim / ".git"),
                "GIT_INDEX_FILE": str(victim / ".git" / "index"),
            }
            with mock.patch.dict(os.environ, hostile):
                working_tree_commit(directory)
            self.assertEqual(git(victim, "count-objects"), before)
            self.assertEqual(git(victim, "ls-files"), "")


if __name__ == "__main__":
    unittest.main()
