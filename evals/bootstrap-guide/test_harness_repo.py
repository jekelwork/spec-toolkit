"""Tests for building a run's temporary repository (research D2, D3a)."""

import os
import shutil
import subprocess
import sys
import tempfile
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


if __name__ == "__main__":
    unittest.main()
