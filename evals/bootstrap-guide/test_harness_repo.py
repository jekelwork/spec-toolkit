"""Tests for building a run's temporary repository (research D2, D3a)."""

import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import harness
import launchers
import profiles

MANIFEST = """\
include = [".agents/**", ".claude/skills", "scripts/**", "docs/bootstrap.md", "AGENTS.md"]
exclude = ["scripts/toolkit-export.toml", "evals/**", "ROADMAP.md", ".specify/memory/**"]

[symlinks]
".claude/skills" = "../.agents/skills"
"""

GIT_ENV = {
    "PATH": os.environ.get("PATH", ""),
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_NOSYSTEM": "1",
}


def git(cwd, *args, check=True):
    result = subprocess.run(
        ["git", "-C", str(cwd), "-c", "user.name=T", "-c", "user.email=t@example.invalid", *args],
        capture_output=True,
        text=True,
        env=GIT_ENV,
    )
    if check and result.returncode:
        raise AssertionError(f"git {args}: {result.stderr}")
    return result.stdout.strip()


def write(root, rel, text="x\n"):
    path = Path(root) / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


class RepoTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()
        self.source = self.tmp / "source"
        self.source.mkdir()
        git(self.source, "init", "-q", "-b", "main")
        write(self.source, "scripts/toolkit-export.toml", MANIFEST)
        write(self.source, "scripts/bootstrap.py", "print('v1')\n")
        write(self.source, ".agents/skills/bootstrap-guide/SKILL.md", "skill v1\n")
        write(self.source, "AGENTS.md", "agents\n")
        write(self.source, "ROADMAP.md", "roadmap\n")
        write(self.source, "evals/x/run.py", "evals\n")
        write(self.source, ".specify/memory/constitution.md", "const\n")
        write(self.source, "unclassified.txt", "u\n")
        (self.source / ".claude").mkdir()
        os.symlink("../.agents/skills", self.source / ".claude" / "skills")
        git(self.source, "add", "-A")
        git(self.source, "commit", "-q", "-m", "first")
        self.first = git(self.source, "rev-parse", "HEAD")
        self.fixture = self.tmp / "fixture"
        self.fixture.mkdir()
        self.n = 0

    def build(self, **kwargs):
        self.n += 1
        return harness.build_run(
            self.tmp / "runs" / f"run-{self.n}",
            self.fixture,
            source_repo=self.source,
            **kwargs,
        )

    def files(self, repo):
        return set(git(repo, "ls-files").splitlines())


class WorkingTreeTest(RepoTestCase):
    def test_exported_set_only(self):
        run = self.build()
        self.assertEqual(
            self.files(run.repo),
            {
                "scripts/bootstrap.py",
                ".agents/skills/bootstrap-guide/SKILL.md",
                "AGENTS.md",
                ".claude/skills",
            },
        )
        for rel in ("evals", "ROADMAP.md", ".specify/memory", "scripts/toolkit-export.toml"):
            self.assertFalse((run.repo / rel).exists(), rel)
        self.assertIsNone(run.toolkit_ref)

    def test_untracked_new_file_counts_and_removed_file_is_skipped(self):
        write(self.source, ".agents/skills/bootstrap-guide/references/new.md", "new\n")
        write(self.source, "docs/bootstrap.md", "docs\n")
        git(self.source, "add", "docs/bootstrap.md")
        (self.source / "docs" / "bootstrap.md").unlink()
        (self.source / "scripts" / "bootstrap.py").unlink()  # tracked, gone from the tree
        write(self.source, ".agents/ignored.log", "i\n")
        write(self.source, ".gitignore", "*.log\n")
        run = self.build(include_untracked=True)
        files = self.files(run.repo)
        self.assertIn(".agents/skills/bootstrap-guide/references/new.md", files)
        self.assertNotIn("scripts/bootstrap.py", files)
        self.assertNotIn("docs/bootstrap.md", files)
        self.assertNotIn(".agents/ignored.log", files)

    def test_untracked_exported_files_refuse_the_build_unless_allowed(self):
        write(self.source, ".agents/skills/bootstrap-guide/references/a.md", "a\n")
        write(self.source, "scripts/b.py", "b\n")
        write(self.source, "evals/untracked.py", "not exported\n")
        write(self.source, ".agents/ignored.log", "i\n")
        write(self.source, ".gitignore", "*.log\n")
        with self.assertRaises(harness.RepoBuildError) as caught:
            self.build(include_untracked=False)
        self.assertIn("2 untracked", str(caught.exception))
        self.assertNotIn("a.md", str(caught.exception))
        self.assertNotIn("--include-untracked", str(caught.exception))
        self.assertFalse((self.tmp / "runs" / "run-1").exists())
        self.assertIn("scripts/b.py", self.files(self.build().repo))  # the default allows them

    @unittest.skipUnless(shutil.which("node"), "node is needed for the secret scan")
    def test_untracked_files_are_secret_scanned(self):
        token = "ghp_" + "a" * 36  # built at run time: no secret in this file
        write(self.source, "scripts/clean.py", "print('fine')\n")
        write(self.source, "scripts/leaky.py", f"TOKEN = '{token}'\n")
        with self.assertRaises(harness.RepoBuildError) as caught:
            self.build()
        message = str(caught.exception)
        self.assertIn("1 of 2 untracked", message)
        self.assertNotIn(token, message)
        self.assertNotIn("leaky", message)
        self.assertFalse((self.tmp / "runs" / "run-1").exists())

    @unittest.skipUnless(shutil.which("node"), "node is needed for the secret scan")
    def test_tracked_files_and_clean_untracked_files_pass_the_scan(self):
        write(self.source, "scripts/clean.py", "print('fine')\n")
        self.assertEqual(self.build().warnings, ())

    def test_missing_node_skips_the_scan_with_a_warning(self):
        write(self.source, "scripts/clean.py", "print('fine')\n")
        real_which = shutil.which

        def which(name, *args, **kwargs):
            return None if name == "node" else real_which(name, *args, **kwargs)

        with mock.patch.object(harness.shutil, "which", which):
            run = self.build()
        self.assertEqual(len(run.warnings), 1)
        self.assertIn("secret scan skipped", run.warnings[0])

    def test_no_untracked_files_means_no_scan_and_no_warning(self):
        with mock.patch.object(harness.subprocess, "run", wraps=harness.subprocess.run) as run:
            built = self.build()
        self.assertEqual(built.warnings, ())
        self.assertFalse(any("check-secrets" in str(c) for c in run.call_args_list))

    def test_untracked_files_that_are_not_exported_do_not_block(self):
        write(self.source, "evals/untracked.py", "not exported\n")
        write(self.source, "unclassified-new.txt", "u\n")
        self.build()

    def test_global_ignore_of_the_maintainer_does_not_apply(self):
        xdg = self.tmp / "xdg"
        write(xdg, "git/ignore", "*.md\n")
        write(self.source, ".agents/skills/bootstrap-guide/references/n.md", "n\n")
        env = {"XDG_CONFIG_HOME": str(xdg), "HOME": str(self.tmp)}
        with mock.patch.dict(os.environ, env):
            with self.assertRaises(harness.RepoBuildError):
                self.build(include_untracked=False)
            run = self.build()
        self.assertIn(".agents/skills/bootstrap-guide/references/n.md", self.files(run.repo))

    def test_source_fsmonitor_config_is_not_run(self):
        marker = self.tmp / "fsmonitor-ran.txt"
        git(self.source, "config", "core.fsmonitor", f"touch {marker}; true")
        write(self.source, "scripts/new.py", "n\n")
        self.build(include_untracked=True)
        self.assertFalse(marker.exists())

    def test_uncommitted_edits_are_used(self):
        write(self.source, "scripts/bootstrap.py", "print('edited')\n")
        run = self.build()
        self.assertEqual((run.repo / "scripts/bootstrap.py").read_text(), "print('edited')\n")

    def test_declared_symlink_is_recreated(self):
        run = self.build()
        link = run.repo / ".claude" / "skills"
        self.assertTrue(link.is_symlink())
        self.assertEqual(os.readlink(link), "../.agents/skills")
        self.assertTrue((link / "bootstrap-guide" / "SKILL.md").is_file())

    def test_declared_symlink_missing_from_the_source_is_refused(self):
        (self.source / ".claude" / "skills").unlink()
        with self.assertRaises(harness.RepoBuildError):
            self.build()

    def test_declared_symlink_with_another_target_is_refused(self):
        link = self.source / ".claude" / "skills"
        link.unlink()
        os.symlink("../scripts", link)
        with self.assertRaises(harness.RepoBuildError):
            self.build()

    def test_undeclared_symlink_is_refused(self):
        os.symlink("/etc/passwd", self.source / "scripts" / "evil")
        with self.assertRaises(harness.RepoBuildError):
            self.build()

    def test_one_commit_and_branch(self):
        run = self.build()
        self.assertEqual(git(run.repo, "rev-list", "--count", "HEAD"), "1")
        self.assertEqual(git(run.repo, "branch", "--show-current"), "main")
        self.assertEqual(git(run.repo, "status", "--porcelain"), "")


class RefTest(RepoTestCase):
    def test_toolkit_ref_reads_that_commit_and_records_the_sha(self):
        write(self.source, "scripts/bootstrap.py", "print('v2')\n")
        write(self.source, ".agents/skills/bootstrap-guide/references/later.md", "later\n")
        git(self.source, "add", "-A")
        git(self.source, "commit", "-q", "-m", "second")
        run = self.build(toolkit_ref=self.first)
        self.assertEqual(run.toolkit_ref, self.first)
        self.assertEqual(len(run.toolkit_ref), 40)
        self.assertEqual((run.repo / "scripts/bootstrap.py").read_text(), "print('v1')\n")
        self.assertNotIn("later.md", "".join(self.files(run.repo)))
        self.assertTrue((run.repo / ".claude" / "skills").is_symlink())
        self.assertNotIn("ROADMAP.md", self.files(run.repo))

    def test_ref_does_not_touch_the_source_checkout(self):
        write(self.source, "scripts/bootstrap.py", "print('dirty')\n")
        before = git(self.source, "status", "--porcelain")
        self.build(toolkit_ref=self.first)
        self.assertEqual(git(self.source, "status", "--porcelain"), before)
        self.assertEqual(git(self.source, "worktree", "list").count("\n"), 0)

    def test_ref_must_be_a_full_existing_sha(self):
        for ref in (self.first[:10], "main", "HEAD", "0" * 40, "--output=x", ""):
            with self.subTest(ref=ref):
                with self.assertRaises(harness.RepoBuildError):
                    self.build(toolkit_ref=ref)


def commit_entries(repo, entries):
    """Commit `{path: (mode, bytes)}` with git plumbing, so colliding paths can exist."""
    env = {**GIT_ENV, "GIT_INDEX_FILE": str(Path(repo) / ".git" / "plumbing-index")}

    def run(*args, data=None):
        done = subprocess.run(
            ["git", "-C", str(repo), "-c", "core.ignorecase=false",
             "-c", "core.precomposeunicode=false", *args],
            input=data,
            capture_output=True,
            env=env,
            check=True,
        )
        return done.stdout.decode().strip()

    for path, (mode, data) in entries.items():
        sha = run("hash-object", "-w", "--stdin", data=data)
        run("update-index", "--add", "--cacheinfo", f"{mode},{sha},{path}")
    tree = run("write-tree")
    return run(
        "-c",
        "user.name=T",
        "-c",
        "user.email=t@example.invalid",
        "commit-tree",
        "-m",
        "plumbing",
        tree,
    )


class HostileTreeTest(RepoTestCase):
    """A committed tree may hold paths that only collide on a case-insensitive disk."""

    def test_case_alias_of_an_escaping_symlink_is_refused(self):
        manifest = (
            'include = ["Up", "up/**", "scripts/**"]\nexclude = []\n'
            '[symlinks]\n"Up" = "../../.."\n'
        )
        sha = commit_entries(
            self.source,
            {
                "scripts/toolkit-export.toml": ("100644", manifest.encode()),
                "Up": ("120000", b"../../.."),
                "up/escaped.txt": ("100644", b"escaped\n"),
            },
        )
        with self.assertRaises(harness.RepoBuildError):
            self.build(toolkit_ref=sha)
        self.assertEqual(list(self.tmp.rglob("escaped.txt")), [])

    def test_nfc_nfd_collision_is_refused(self):
        manifest = 'include = ["scripts/**"]\nexclude = []\n'
        sha = commit_entries(
            self.source,
            {
                "scripts/toolkit-export.toml": ("100644", manifest.encode()),
                "scripts/café.txt": ("100644", b"nfc\n"),
                "scripts/café.txt": ("100644", b"nfd\n"),
            },
        )
        with self.assertRaises(harness.RepoBuildError):
            self.build(toolkit_ref=sha)

    def test_symlink_target_may_not_leave_the_repository(self):
        manifest = 'include = ["scripts/**", "lnk"]\nexclude = []\n[symlinks]\n"lnk" = "../x"\n'
        sha = commit_entries(
            self.source,
            {
                "scripts/toolkit-export.toml": ("100644", manifest.encode()),
                "lnk": ("120000", b"../x"),
            },
        )
        with self.assertRaises(harness.RepoBuildError):
            self.build(toolkit_ref=sha)


class FixtureTest(RepoTestCase):
    def test_overlay_keeps_only_the_owner_exec_bit(self):
        write(self.fixture, "files/scripts/yes.sh", "#!/bin/sh\n").chmod(0o711)
        write(self.fixture, "files/scripts/no.sh", "#!/bin/sh\n").chmod(0o611)
        run = self.build()
        modes = {
            line.split("\t")[1]: line.split()[0]
            for line in git(run.repo, "ls-files", "-s").splitlines()
        }
        self.assertEqual(modes["scripts/yes.sh"], "100755")
        self.assertEqual(modes["scripts/no.sh"], "100644")

    def test_overlay_adds_and_replaces_files_and_keeps_mode(self):
        write(self.fixture, "files/project-context.toml", "name='x'\n")
        write(self.fixture, "files/scripts/bootstrap.py", "print('stub')\n")
        script = write(self.fixture, "files/scripts/run.sh", "#!/bin/sh\n")
        script.chmod(0o755)
        run = self.build()
        self.assertEqual((run.repo / "project-context.toml").read_text(), "name='x'\n")
        self.assertEqual((run.repo / "scripts/bootstrap.py").read_text(), "print('stub')\n")
        self.assertTrue(os.access(run.repo / "scripts/run.sh", os.X_OK))
        self.assertIn("project-context.toml", self.files(run.repo))

    def test_overlay_does_not_follow_symlinks(self):
        outside = self.tmp / "outside.txt"
        outside.write_text("secret\n")
        os.makedirs(self.fixture / "files")
        os.symlink(outside, self.fixture / "files" / "leak.txt")
        with self.assertRaises(harness.RepoBuildError):
            self.build()

    def test_overlay_does_not_write_through_a_symlink_in_the_repo(self):
        write(self.fixture, "files/.claude/skills/bootstrap-guide/SKILL.md", "hijack\n")
        with self.assertRaises(harness.RepoBuildError):
            self.build()
        self.assertEqual(
            (self.source / ".agents/skills/bootstrap-guide/SKILL.md").read_text(), "skill v1\n"
        )

    def test_overlay_refuses_git_internals_and_unsafe_paths(self):
        marker = self.tmp / "marker.txt"
        cases = {
            ".git/config": f"[core]\n\tfsmonitor = touch {marker}\n",
            ".GIT/hooks/pre-commit": f"#!/bin/sh\ntouch {marker}\n",
            "sub/.git/config": "x\n",
            "a\\b.txt": "x\n",
        }
        for rel, text in cases.items():
            with self.subTest(rel=rel):
                shutil.rmtree(self.fixture / "files", ignore_errors=True)
                write(self.fixture, f"files/{rel}", text)
                with self.assertRaises(harness.RepoBuildError):
                    self.build()
                self.assertFalse(marker.exists())

    def test_failed_build_leaves_no_run_directory(self):
        write(self.fixture, "files/.git/config", "x\n")
        with self.assertRaises(harness.RepoBuildError):
            self.build()
        self.assertFalse((self.tmp / "runs" / "run-1").exists())

    def test_git_dir_present_before_init_is_refused(self):
        repo = self.tmp / "pre"
        (repo / ".git").mkdir(parents=True)
        with self.assertRaises(harness.RepoBuildError):
            harness._init_repo(repo)

    def test_origin_url_and_bare_repository(self):
        write(
            self.fixture,
            "git.toml",
            '[[branch]]\nname = "feature/x"\ncommits = ["one", "two"]\n\n'
            '[[branch]]\nname = "other"\n',
        )
        run = self.build()
        self.assertEqual(git(run.repo, "remote", "get-url", "origin"), "https://github.com/o/r.git")
        self.assertEqual(
            git(run.repo, "remote", "get-url", "--push", "origin"), "https://github.com/o/r.git"
        )
        self.assertEqual(run.origin, run.run_root / "origin.git")
        self.assertEqual(git(run.origin, "rev-parse", "--is-bare-repository"), "true")
        branches = set(
            git(run.origin, "for-each-ref", "--format=%(refname:short)", "refs/heads").splitlines()
        )
        self.assertEqual(branches, {"main", "feature/x", "other"})
        subjects = git(run.origin, "log", "--format=%s", "feature/x").splitlines()
        self.assertEqual(subjects[:2], ["two", "one"])
        self.assertEqual(git(run.origin, "rev-parse", "main"), git(run.repo, "rev-parse", "main"))
        self.assertEqual(
            git(run.repo, "rev-parse", "origin/feature/x"),
            git(run.origin, "rev-parse", "feature/x"),
        )

    def test_no_git_toml_still_has_origin_main(self):
        run = self.build()
        self.assertEqual(git(run.origin, "rev-parse", "main"), git(run.repo, "rev-parse", "HEAD"))


class LayoutTest(RepoTestCase):
    def test_layout_and_env(self):
        write(self.fixture, "gh.toml", '[[call]]\nargv = ["--version"]\nstdout = "gh 1\\n"\n')
        run = self.build()
        root = run.run_root
        self.assertEqual(root, (self.tmp / "runs" / "run-1").resolve())
        self.assertEqual(run.repo, root / "repo")
        self.assertEqual(run.sidecar, root / "harness")
        self.assertEqual(run.bin_dir, root / "bin")
        self.assertTrue((run.bin_dir / "gh").is_file() and (run.bin_dir / "git").is_file())
        self.assertEqual(run.gh_calls.parent, run.sidecar)
        self.assertEqual(run.gh_calls.read_text(), "")  # exists, empty: a missing log is stale
        self.assertEqual(run.gh_table.parent, run.sidecar)
        self.assertIn("gh 1", run.gh_table.read_text())
        self.assertEqual(
            run.harness_env(),
            {
                "PATH": str(run.bin_dir),
                "EVAL_RUN_DIR": str(root),
                "EVAL_GH_TABLE": str(run.gh_table),
                "EVAL_GH_CALLS": str(run.gh_calls),
            },
        )
        for path in run.repo.rglob("*"):
            self.assertNotEqual(path.name, "gh-calls.jsonl")
        self.assertFalse((run.repo / "harness").exists())

    def test_shown_directories_and_python_are_recorded(self):
        run = self.build()
        self.assertEqual(run.shown_home, run.run_root / "shown-home")
        self.assertEqual(run.shown_tmp, run.run_root / "shown-tmp")
        self.assertTrue(run.shown_home.is_dir() and run.shown_tmp.is_dir())
        self.assertEqual(run.python, harness.sys.executable)

    def test_sandbox_inputs_are_recorded_as_resolved_strings(self):
        write(self.fixture, "files/docs/.gitattributes", "* text\n")
        run = self.build()
        self.assertEqual(run.repo_real, os.path.realpath(run.repo))
        self.assertEqual(run.bin_real, os.path.realpath(run.bin_dir))
        self.assertEqual(run.gh_calls_real, os.path.realpath(run.gh_calls))
        stat = os.lstat(run.gh_calls)
        self.assertEqual(run.gh_calls_id, (stat.st_dev, stat.st_ino))
        self.assertEqual(run.git_real, os.path.realpath(shutil.which("git")))
        self.assertIn("docs/.gitattributes", run.fingerprints)
        sidecar = os.path.realpath(run.sidecar)
        self.assertEqual(run.gh_table_real, os.path.realpath(run.gh_table))
        self.assertEqual(run.gitconfig_real, os.path.join(sidecar, "harness.gitconfig"))
        self.assertEqual(run.gh_config_real, os.path.join(sidecar, "gh-config"))
        self.assertEqual(run.origin_real, os.path.realpath(run.origin))
        exec_path = subprocess.run(
            ["git", "--exec-path"], capture_output=True, text=True, check=True
        ).stdout.strip()
        self.assertEqual(run.git_exec_real, os.path.realpath(exec_path))
        self.assertIn(run.git_real, run.git_paths)
        if os.path.isfile("/usr/bin/git"):
            self.assertIn(os.path.realpath("/usr/bin/git"), run.git_paths)
        for value in run.git_paths:
            self.assertEqual(value, os.path.realpath(value))
        for value in (run.repo_real, run.bin_real, run.gh_calls_real, run.git_real):
            self.assertIsInstance(value, str)

    def test_fingerprints_cover_scripts_and_git_internals(self):
        run = self.build()
        keys = set(run.fingerprints)
        self.assertIn("scripts/bootstrap.py", keys)
        self.assertNotIn(".git/config", keys)  # parsed, not hashed: git itself writes it
        self.assertTrue(any(k.startswith(".git/hooks/") for k in keys))
        self.assertTrue(any(k.startswith("origin.git/hooks/") for k in keys))
        self.assertFalse(any("__pycache__" in k for k in keys))
        self.assertEqual(harness.check_integrity(run), [])
        (run.repo / "scripts" / "bootstrap.py").write_text("print('evil')\n")
        self.assertEqual(harness.check_integrity(run), ["scripts/bootstrap.py"])

    def test_wrappers_run_from_copies_in_bin_lib(self):
        run = self.build()
        lib = run.bin_dir / "lib"
        self.assertTrue((lib / "gh_shim.py").is_file())
        self.assertTrue((lib / "git_wrapper.py").is_file())
        self.assertIn(str(lib / "gh_shim.py"), (run.bin_dir / "gh").read_text())
        self.assertIn(str(lib / "git_wrapper.py"), (run.bin_dir / "git").read_text())

    def test_agent_settings_grant_write_to_the_repo_and_call_log_only(self):
        run = self.build()
        home = str(self.tmp / "home")
        settings = run.agent_settings(profiles.DEFAULT_PROFILE, home, extra_read=["/x/py"])
        fs = settings["sandbox"]["filesystem"]
        self.assertEqual(fs["allowWrite"], [str(run.repo), str(run.gh_calls)])
        self.assertIn(str(run.bin_dir), fs["allowRead"])
        self.assertIn("/x/py", fs["allowRead"])
        for path in (run.bin_dir, run.origin, run.sidecar, run.run_root):
            self.assertNotIn(str(path), fs["allowWrite"])

    def test_missing_gh_table_gives_an_empty_table(self):
        run = self.build()
        self.assertTrue(run.gh_table.is_file())
        self.assertEqual(run.gh_table.read_text(), "")

    def test_existing_run_directory_is_refused(self):
        self.build()
        self.n -= 1
        with self.assertRaises(harness.RepoBuildError):
            self.build()

    def test_wrappers_work_with_the_launcher_env(self):
        write(self.fixture, "gh.toml", '[[call]]\nargv = ["--version"]\nstdout = "gh 1\\n"\n')
        run = self.build()
        launcher = launchers.LocalLauncher("m", 1, home=str(self.tmp / "home"))
        env = launcher.build_env(run.repo, run.sidecar, run.harness_env())

        def sh(*argv):
            return subprocess.run(
                list(argv), cwd=run.repo, env=env, capture_output=True, text=True
            )

        self.assertEqual(sh("gh", "--version").stdout, "gh 1\n")
        self.assertIn('"argv"', run.gh_calls.read_text())
        # A push goes to the local bare repository, the URL still reads as GitHub.
        write(run.repo, "new.txt", "n\n")
        for args in (["add", "new.txt"], ["commit", "-qm", "n"], ["push", "origin", "main"]):
            done = sh("git", *args)
            self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(git(run.origin, "rev-parse", "main"), git(run.repo, "rev-parse", "main"))
        self.assertEqual(
            sh("git", "remote", "get-url", "origin").stdout.strip(), "https://github.com/o/r.git"
        )
        # A push changes objects and refs in `origin.git`, none of the watched files.
        self.assertEqual(harness.check_integrity(run), [])


@unittest.skipUnless(
    sys.platform == "darwin" and os.path.exists(launchers.SANDBOX_EXEC),
    "needs macOS sandbox-exec",
)
class SandboxedEngineTest(unittest.TestCase):
    """A real `start --no-input` through the sandbox reaches the shim and git (research D5)."""

    def test_start_runs_without_sandbox_denials(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp).resolve()
            fixture = tmp / "fixture"
            fixture.mkdir()
            # The HEAD commit, so the maintainer's untracked files cannot make this fail.
            head = subprocess.run(
                ["git", "-C", str(harness.REPO_ROOT), "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
            run = harness.build_run(tmp / "run", fixture, toolkit_ref=head)
            launcher = launchers.LocalLauncher("m", 1, home=str(Path.home()))
            env = launcher.build_env(run.repo, run.sidecar, run.harness_env())
            self.addCleanup(launcher.close)
            text = harness.run_shown(
                "Run this here:\n\n```\npython3 scripts/bootstrap.py start --no-input\n```\n",
                launcher,
                run,
                env,
                timeout=180,
            )
            calls = run.gh_calls.read_text()
        for denied in ("cannot read table", "unable to access", "Operation not permitted"):
            self.assertNotIn(denied, text)
        # With an empty fixture the engine stops at its first `gh` call: the shim ran in the
        # sandbox, answered "no fixture" and logged the call (research D4a).
        self.assertIn("Stopped at prerequisites", text)
        self.assertIn("gh shim: no fixture for", text)
        self.assertIn('"argv": ["auth", "status"', calls)


class AgentConfigRefusalTest(RepoTestCase):
    """The agent loads project settings from the temp repo, so none may be planted (P1)."""

    def test_overlay_refuses_project_settings_hooks_and_mcp(self):
        for rel in (
            ".claude/settings.json",
            ".claude/settings.local.json",
            ".claude/settings.team.json",
            ".Claude/Settings.JSON",
            ".mcp.json",
            ".claude/hooks/pre.sh",
        ):
            with self.subTest(rel=rel):
                shutil.rmtree(self.fixture / "files", ignore_errors=True)
                write(self.fixture, f"files/{rel}", "{}\n")
                with self.assertRaises(harness.RepoBuildError):
                    self.build()

    def test_export_refuses_project_settings(self):
        write(self.source, ".agents/x/.keep", "x\n")
        write(self.source, ".claude/settings.json", '{"hooks": {}}\n')
        git(self.source, "add", "-A")
        manifest = MANIFEST.replace('".agents/**"', '".agents/**", ".claude/settings.json"')
        write(self.source, "scripts/toolkit-export.toml", manifest)
        with self.assertRaises(harness.RepoBuildError):
            self.build()

    def test_only_the_skills_link_may_live_in_dot_claude(self):
        sharp_s = chr(0x17F)  # APFS folds it to `s`, so this names a settings file
        for rel in (
            ".claude/notes.md",
            ".claude/commands/x.md",
            ".claude/agents/x.md",
            f".claude/{sharp_s}ettings.json",
            f".claude/{sharp_s}ettings.local.json",
            "docs/.claude/settings.json",
            "docs/.claude/notes.md",
        ):
            with self.subTest(rel=rel):
                shutil.rmtree(self.fixture / "files", ignore_errors=True)
                write(self.fixture, f"files/{rel}", "{}\n")
                with self.assertRaises(harness.RepoBuildError):
                    self.build()

    def test_names_that_fold_to_dot_mcp_json_are_refused(self):
        for name in (".MCP.json", ".mcp.json", ".m" + chr(0x200B) + "cp.json"):
            shutil.rmtree(self.fixture / "files", ignore_errors=True)
            write(self.fixture, f"files/{name}", "{}\n")
            with self.subTest(name=name), self.assertRaises(harness.RepoBuildError):
                self.build()

    def test_dot_claude_must_be_a_directory_with_only_the_skills_link(self):
        def fresh():
            return Path(tempfile.mkdtemp(dir=self.tmp))

        empty = fresh()
        harness._check_agent_config(empty)  # nothing at all is fine
        good = fresh()
        (good / ".claude").mkdir()
        os.symlink("../.agents/skills", good / ".claude" / "skills")
        harness._check_agent_config(good)
        wrong = fresh()
        (wrong / ".claude").mkdir()
        os.symlink("../elsewhere", wrong / ".claude" / "skills")
        as_file = fresh()
        (as_file / ".claude").write_text("x")
        as_link = fresh()
        (as_link / "docs").mkdir()
        os.symlink("docs", as_link / ".claude")
        for label, repo in {"wrong target": wrong, "file": as_file, "symlink": as_link}.items():
            with self.subTest(label), self.assertRaises(harness.RepoBuildError):
                harness._check_agent_config(repo)

    def test_overlay_may_not_add_or_change_skills(self):
        for rel in (
            ".agents/skills/extra/SKILL.md",
            ".agents/skills/bootstrap-guide/SKILL.md",
            ".agents/skills/bootstrap-guide/references/new.md",
            ".Agents/Skills/extra/SKILL.md",
        ):
            with self.subTest(rel=rel):
                shutil.rmtree(self.fixture / "files", ignore_errors=True)
                write(self.fixture, f"files/{rel}", "x\n")
                with self.assertRaises(harness.RepoBuildError):
                    self.build()

    def test_the_skills_the_scenarios_use_may_not_widen_permissions(self):
        for skill in profiles.EVAL_SKILLS:
            for line in (
                "allowed-tools: Bash(gh:*)",
                "hooks:",
                "Allowed-Tools: Bash",
                '"allowed-tools": Bash',
                "'hooks':",
            ):
                with self.subTest(skill=skill, line=line):
                    text = f"---\nname: {skill}\n{line}\n---\nbody\n"
                    write(self.source, f".agents/skills/{skill}/SKILL.md", text)
                    with self.assertRaises(harness.RepoBuildError):
                        self.build()

    def test_a_byte_order_mark_does_not_hide_the_frontmatter(self):
        text = chr(0xFEFF) + "---\nname: roadmap-write\nallowed-tools: Bash\n---\nbody\n"
        write(self.source, ".agents/skills/roadmap-write/SKILL.md", text)
        with self.assertRaises(harness.RepoBuildError):
            self.build()

    def test_an_unreadable_scenario_skill_is_refused(self):
        big = "---\nname: roadmap-write\n---\n" + "x" * harness.MAX_READ
        write(self.source, ".agents/skills/roadmap-write/SKILL.md", big)
        with self.assertRaises(harness.RepoBuildError):
            self.build()
        repo = Path(tempfile.mkdtemp(dir=self.tmp))
        skill = repo / ".agents" / "skills" / "bootstrap-guide"
        skill.mkdir(parents=True)
        os.symlink("/etc/hosts", skill / "SKILL.md")
        with self.assertRaises(harness.RepoBuildError):
            harness._check_skill_frontmatter(repo)
        other = repo / ".agents" / "skills" / "other"
        other.mkdir()
        (skill / "SKILL.md").unlink()
        (skill / "SKILL.md").write_text("---\nname: bootstrap-guide\n---\n")
        os.symlink("/etc/hosts", other / "SKILL.md")
        harness._check_skill_frontmatter(repo)  # an unreadable skill nobody invokes is not refused

    def test_unterminated_frontmatter_is_refused(self):
        write(self.source, ".agents/skills/roadmap-write/SKILL.md", "---\nname: roadmap-write\n")
        with self.assertRaises(harness.RepoBuildError):
            self.build()

    def test_a_skill_that_goes_by_the_name_of_an_allowed_one_is_checked(self):
        write(
            self.source,
            ".agents/skills/other/SKILL.md",
            "---\nname: roadmap-write\nallowed-tools: Bash\n---\n",
        )
        with self.assertRaises(harness.RepoBuildError):
            self.build()
        write(
            self.source,
            ".agents/skills/Roadmap-Write/SKILL.md",
            "---\nname: other\nhooks:\n---\n",
        )
        (self.source / ".agents/skills/other/SKILL.md").unlink()
        with self.assertRaises(harness.RepoBuildError):
            self.build()

    def test_duplicate_skill_names_are_refused(self):
        for directory in ("a", "b"):
            write(self.source, f".agents/skills/{directory}/SKILL.md", "---\nname: same\n---\n")
        with self.assertRaises(harness.RepoBuildError):
            self.build()

    def test_other_skills_may_use_allowed_tools(self):
        write(
            self.source,
            ".agents/skills/github-project-tasks/SKILL.md",
            "---\nname: github-project-tasks\nallowed-tools: Bash(gh:*)\n---\n",
        )
        self.build()

    def test_a_word_in_the_skill_body_is_not_frontmatter(self):
        text = "---\nname: roadmap-write\n---\nallowed-tools: in the body\nhooks:\n"
        write(self.source, ".agents/skills/roadmap-write/SKILL.md", text)
        self.build()


class SkillExportTest(RepoTestCase):
    def test_every_skill_is_exported(self):
        for name in ("speckit-constitution", "roadmap-write", "github-project-tasks", "other"):
            write(self.source, f".agents/skills/{name}/SKILL.md", "s\n")
        run = self.build()
        skills = {p.split("/")[2] for p in self.files(run.repo) if p.startswith(".agents/skills/")}
        self.assertEqual(
            skills,
            {
                "bootstrap-guide",
                "speckit-constitution",
                "roadmap-write",
                "github-project-tasks",
                "other",
            },
        )

    def test_the_real_toolkit_export_passes_the_engines_agent_setup_check(self):
        # The engine's `check --json` blocks on `agent_setup` problems, such as a skill file
        # listed in `.specify/integrations/*.manifest.json` that the export left out.
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        run = harness.build_run(Path(tmp.name) / "run", self.fixture)
        done = subprocess.run(
            [sys.executable, "scripts/agent_setup.py", "check"],
            cwd=run.repo,
            capture_output=True,
            text=True,
            env={**GIT_ENV, "HOME": tmp.name},
        )
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)


class FingerprintAgentConfigTest(RepoTestCase):
    def test_claude_and_skills_are_fingerprinted(self):
        run = self.build()
        self.assertEqual(run.fingerprints[".claude/skills"], "link:../.agents/skills")
        self.assertIn(".agents/skills/bootstrap-guide/SKILL.md", run.fingerprints)
        (run.repo / ".claude" / "settings.json").write_text("{}\n")
        self.assertIn(".claude/settings.json", harness.check_integrity(run))
        (run.repo / ".claude" / "settings.json").unlink()
        (run.repo / ".agents/skills/bootstrap-guide/SKILL.md").write_text("changed\n")
        self.assertEqual(
            harness.check_integrity(run), [".agents/skills/bootstrap-guide/SKILL.md"]
        )


class EngineAgentSetupTest(unittest.TestCase):
    """`start --agents claude` sets `agent-setup.tools` and runs `agent_setup.py sync`."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tmp.cleanup)
        fixture = Path(cls.tmp.name) / "fixture"
        fixture.mkdir()
        cls.run_ = harness.build_run(Path(cls.tmp.name) / "run", fixture)
        env = {**GIT_ENV, "HOME": cls.tmp.name}
        repo = cls.run_.repo
        subprocess.run(
            ["git", "config", "--local", "agent-setup.tools", "claude"],
            cwd=repo, check=True, env=env,
        )
        done = subprocess.run(
            [sys.executable, "scripts/agent_setup.py", "sync"],
            cwd=repo, capture_output=True, text=True, env=env,
        )
        assert done.returncode == 0, done.stdout + done.stderr
        cls.agents = repo / ".claude" / "agents"

    def test_the_engines_own_sync_is_not_tampering(self):
        self.assertTrue(any(self.agents.glob("*.md")))
        self.assertEqual(harness.check_integrity(self.run_), [])

    def test_a_changed_generated_agent_file_is_tampering(self):
        victim = next(self.agents.glob("*.md"))
        original = victim.read_text()
        self.addCleanup(victim.write_text, original)
        victim.write_text(original + "hooks: {}\n")
        self.assertEqual(harness.check_integrity(self.run_), [f".claude/agents/{victim.name}"])

    def test_an_extra_agent_file_is_tampering(self):
        extra = self.agents / "x.md"
        self.addCleanup(extra.unlink)
        extra.write_text("---\nname: x\n---\n")
        self.assertEqual(harness.check_integrity(self.run_), [".claude/agents/x.md"])

    def test_entries_that_are_not_regular_files_are_tampering(self):
        def nested():
            (self.agents / "team").mkdir()
            (self.agents / "team" / "evil.md").write_text("x")

        def symlink():
            os.symlink("/etc/hosts", self.agents / "link.md")

        def fifo():
            os.mkfifo(self.agents / "pipe.md")

        def oversize():
            (self.agents / "big.md").write_bytes(b"x" * (harness.MAX_READ + 1))

        for name, make, entry in (
            ("nested dir", nested, "team"),
            ("symlink", symlink, "link.md"),
            ("fifo", fifo, "pipe.md"),
            ("oversize", oversize, "big.md"),
        ):
            with self.subTest(name):
                make()
                path = self.agents / entry
                self.assertEqual(harness.check_integrity(self.run_), [f".claude/agents/{entry}"])
                if path.is_dir() and not path.is_symlink():
                    shutil.rmtree(path)
                else:
                    path.unlink()

    def test_with_nothing_expected_every_entry_is_tampering(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        repo = Path(tmp.name)
        (repo / ".claude" / "agents" / "sub").mkdir(parents=True)
        (repo / ".claude" / "agents" / "x.md").write_text("x")
        os.mkfifo(repo / ".claude" / "agents" / "pipe")
        self.assertEqual(
            harness._agents_problems(repo, {}),
            [".claude/agents/pipe", ".claude/agents/sub", ".claude/agents/x.md"],
        )

    def test_the_expected_agent_files_are_fixed_at_build_time(self):
        self.assertIn("code-reviewer.md", self.run_.agents_expected)
        self.assertIsInstance(self.run_.agents_expected["code-reviewer.md"], bytes)

    def test_a_fifo_in_the_agent_definitions_cannot_hang_the_check(self):
        # `agent_setup` reads the definitions with `read_text`, which blocks on a FIFO.
        fifo = self.run_.repo / "agent-definitions" / "pipe.md"
        os.mkfifo(fifo)

        def release():
            fd = os.open(fifo, os.O_RDWR)  # lets a blocked reader through
            os.close(fd)
            fifo.unlink()

        self.addCleanup(release)
        found = []
        thread = threading.Thread(
            target=lambda: found.append(harness.check_integrity(self.run_)), daemon=True
        )
        thread.start()
        thread.join(10)
        self.assertFalse(thread.is_alive(), "check_integrity hangs on a FIFO")
        self.assertIn("agent-definitions/pipe.md", found[0])

    def test_changed_agent_definitions_are_tampering(self):
        source = self.run_.repo / "agent-definitions"
        victim = next(source.glob("*.md"))
        original = victim.read_text()
        self.addCleanup(victim.write_text, original)
        victim.write_text(original + "\nmore\n")
        self.assertIn(f"agent-definitions/{victim.name}", harness.check_integrity(self.run_))

    def test_only_the_tools_key_with_known_tool_names_is_allowed_in_git_config(self):
        ok = "[agent-setup]\n\ttools = claude,codex\n"
        self.assertIsNone(harness.config_problem(ok))
        for bad in (
            "[agent-setup]\n\ttools = claude;touch x\n",
            "[agent-setup]\n\ttools = $(id)\n",
            "[agent-setup]\n\ttools\n",
            "[agent-setup]\n\tother = claude\n",
            "[agent-setup]\n\ttools = claude,\n",
            '[agent-setup "x"]\n\ttools = claude\n',
            "[agent-setup.x]\n\ttools = claude\n",
        ):
            with self.subTest(bad=bad):
                self.assertIsNotNone(harness.config_problem(bad))


class MidRunAgentConfigTest(RepoTestCase):
    """Agent configuration created after the build, anywhere, is tampering (audit A1)."""

    def test_instruction_files_anywhere_are_fingerprinted(self):
        run = self.build()
        self.assertIn("AGENTS.md", run.fingerprints)  # an exported toolkit file
        self.assertEqual(harness.check_integrity(run), [])
        (run.repo / "AGENTS.md").write_text("changed\n")
        self.assertEqual(harness.check_integrity(run), ["AGENTS.md"])
        (run.repo / "AGENTS.md").write_text("agents\n")
        for rel in ("CLAUDE.local.md", "CLAUDE.md", "docs/CLAUDE.md", "docs/sub/agents.md"):
            with self.subTest(rel):
                path = run.repo / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("x\n")
                self.assertEqual(harness.check_integrity(run), [rel])
                path.unlink()

    def test_nested_and_folded_agent_configuration_is_reported(self):
        for rel in (
            "docs/.claude/skills/x/SKILL.md",
            ".mcp.json",
            f".claude/{chr(0x17F)}ettings.json",
            ".claude/commands/x.md",
            ".claude/agents.d/x.md",
        ):
            with self.subTest(rel):
                run = self.build()
                self.assertEqual(harness.check_integrity(run), [])
                path = run.repo / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("{}\n")
                self.assertIn(".claude (agent config)", harness.check_integrity(run))

    def test_a_new_top_level_dot_claude_is_reported_by_run_shown_integrity(self):
        run = self.build()
        (run.repo / ".claude" / "skills").unlink()
        (run.repo / ".claude" / "skills").mkdir()
        self.assertTrue(harness.check_integrity(run))


if __name__ == "__main__":
    unittest.main()
