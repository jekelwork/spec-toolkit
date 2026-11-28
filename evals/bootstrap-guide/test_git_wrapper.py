"""Tests for the git wrapper (FR-002, research D3a)."""
import contextlib
import io
import os
import shlex
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

import git_wrapper

URL = "https://github.com/o/r.git"
RUN = "/runs/one"
REWRITE = f"url.file://{RUN}/origin.git.insteadOf={URL}"
REAL = "/usr/bin/real-git"
TOP = f"{RUN}/repo"
GIT_DIR = f"{TOP}/.git"


def run_main(argv, env=None, base=TOP):
    calls = []
    env = {git_wrapper.RUN_DIR_ENV: RUN} if env is None else env
    git_wrapper.main(
        REAL, argv, env=env, execv=lambda path, args: calls.append((path, args)),
        locate=lambda: (base and (TOP, base, GIT_DIR)),
    )
    return calls


class UnitTest(unittest.TestCase):
    def test_network_subcommands_are_rewritten(self):
        for sub in ["push", "fetch", "pull", "ls-remote", "clone"]:
            with self.subTest(sub=sub):
                calls = run_main([sub, "origin", "main"])
                self.assertEqual(calls, [(REAL, [REAL, "-c", REWRITE, sub, "origin", "main"])])

    def test_remote_update_is_rewritten(self):
        calls = run_main(["remote", "update"])
        self.assertEqual(calls, [(REAL, [REAL, "-c", REWRITE, "remote", "update"])])

    def test_get_url_and_others_pass_through(self):
        for argv in [
            ["remote", "get-url", "origin"],
            ["remote", "get-url", "--push", "origin"],
            ["remote"],
            ["status"],
            ["commit", "-m", "push"],
            ["config", "--get", "remote.origin.url"],
        ]:
            with self.subTest(argv=argv):
                self.assertEqual(run_main(argv), [(REAL, [REAL, *argv])])

    def test_global_options_are_skipped(self):
        cases = [
            (["-C", "push", "fetch"], 2),
            (["-c", "user.name=b", "push"], 2),
            (["--git-dir=x", "--no-pager", "pull"], 2),
            (["--git-dir", "x", "--work-tree", "y", "push"], 4),
            (["-C", "d", "-c", "user.name=v", "remote", "update"], 4),
        ]
        for prefix, n in cases:
            with self.subTest(prefix=prefix):
                expected = [REAL, *prefix[:n], "-c", REWRITE, *prefix[n:]]
                self.assertEqual(run_main(prefix), [(REAL, expected)])

    def test_global_option_value_is_not_a_subcommand(self):
        argv = ["-C", "push", "status"]
        self.assertEqual(run_main(argv), [(REAL, [REAL, *argv])])

    def test_no_subcommand(self):
        self.assertEqual(run_main(["--version"]), [(REAL, [REAL, "--version"])])
        self.assertEqual(run_main([]), [(REAL, [REAL])])

    def test_missing_run_dir_fails_for_network_subcommand(self):
        with self.assertRaises(SystemExit) as ctx:
            run_main(["push"], env={})
        self.assertNotEqual(ctx.exception.code, 0)

    def test_missing_run_dir_is_refused_for_local_subcommand_too(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as ctx:
            run_main(["status"], env={})
        self.assertEqual(ctx.exception.code, 128)
        self.assertIn("git wrapper: refused", err.getvalue())

    def test_install_writes_sh_launcher(self):
        with tempfile.TemporaryDirectory() as tmp:
            real = Path(tmp) / "real-git"
            real.write_text("#!/bin/sh\n")
            real.chmod(0o755)
            path = git_wrapper.install(Path(tmp) / "bin", real_git=str(real))
            self.assertEqual(path, Path(tmp) / "bin" / "git")
            self.assertTrue(path.stat().st_mode & stat.S_IXUSR)
            text = path.read_text(encoding="utf-8")
            self.assertTrue(text.startswith("#!/bin/sh\n"))
            self.assertIn("--real-git", text)

    def test_install_rejects_bad_inputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            bin_dir = tmp / "bin"
            bin_dir.mkdir()
            ok = tmp / "real-git"
            ok.write_text("x")
            ok.chmod(0o755)
            plain = tmp / "plain"
            plain.write_text("x")
            inside = bin_dir / "git"
            inside.write_text("x")
            inside.chmod(0o755)
            cases = {
                "none": {"real_git": None},
                "empty": {"real_git": ""},
                "relative": {"real_git": "git"},
                "missing": {"real_git": str(tmp / "nope")},
                "not executable": {"real_git": str(plain)},
                "inside bin dir": {"real_git": str(inside)},
                "relative python": {"real_git": str(ok), "python": "python3"},
                "missing python": {"real_git": str(ok), "python": str(tmp / "nopy")},
            }
            for name, kwargs in cases.items():
                with self.subTest(name):
                    with self.assertRaises(ValueError):
                        git_wrapper.install(bin_dir, **kwargs)

    def assert_refused(self, argv):
        calls = []
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as ctx:
            git_wrapper.main(
                REAL, argv, env={git_wrapper.RUN_DIR_ENV: RUN},
                execv=lambda path, args: calls.append(args),
                locate=lambda: (TOP, TOP, GIT_DIR),
            )
        self.assertEqual(ctx.exception.code, 128, argv)
        self.assertIn("git wrapper: refused option", err.getvalue())
        self.assertEqual(calls, [], argv)

    def test_output_option_is_refused_for_log_show_diff(self):
        for sub in ["log", "show", "diff"]:
            for opt in [["--output", "f"], ["--output=f"], ["--outp=f"]]:
                with self.subTest(sub=sub, opt=opt):
                    self.assert_refused([sub, *opt])

    def test_no_index_is_refused_for_diff(self):
        for opt in ["--no-index", "--no-inde", "--no-ind", "--no-index=x"]:
            for argv in [
                ["diff", opt, "../harness/gh.toml", "/dev/null"],
                ["diff", "--stat", opt, "a", "b"],
                ["diff", "a", "b", opt],
            ]:
                with self.subTest(argv=argv):
                    self.assert_refused(argv)

    def test_no_index_after_double_dash_and_similar_options_are_allowed(self):
        for argv in [
            ["diff", "--", "--no-index"],
            ["diff", "--no-color"],
            ["diff", "--no-indent-heuristic"],
            ["diff", "--no-renames"],
        ]:
            with self.subTest(argv=argv):
                self.assertEqual(run_main(argv), [(REAL, [REAL, *argv])])

    def test_diff_paths_outside_the_repository_are_refused(self):
        for argv in [
            ["diff", "../harness/gh.toml", "/dev/null"],
            ["diff", "../harness/gh.toml", "a"],
            ["diff", "a", "../harness/gh.toml"],
            ["diff", "/dev/null", "../harness/gh.toml"],
            ["diff", "--", "../harness/gh.toml", "/dev/null"],
            ["diff", "--stat", "../harness/gh.toml", "/dev/null"],
            ["diff", "/etc/passwd", "a"],
            ["diff", "a", "--", "/etc/hosts"],
            ["diff", "a/../../b"],
            ["diff", "--", "f", "-x/../../b"],
            ["diff", "--", "-x/../../b", "-x/k"],
            ["diff", "--end-of-options", "f", "-x/../../b"],
            ["diff", "../repo/f", "-"],
            ["diff", "./d/../../repo/f", "x"],
            ["diff", "-"],
            ["diff", "--", "-"],
        ]:
            with self.subTest(argv=argv):
                self.assert_refused(argv)

    def test_diff_outside_a_work_tree_is_refused(self):
        calls = []
        with contextlib.redirect_stderr(io.StringIO()) as err, self.assertRaises(SystemExit) as ctx:
            git_wrapper.main(
                REAL, ["diff", "HEAD"], env={}, execv=lambda p, a: calls.append(a),
                locate=lambda: None,
            )
        self.assertEqual(ctx.exception.code, 128)
        self.assertIn("git wrapper: refused", err.getvalue())
        self.assertEqual(calls, [])

    def test_diff_inside_the_repository_is_allowed(self):
        for argv in [
            ["diff", "HEAD~1..HEAD"],
            ["diff", "--", "a"],
            ["diff", "main..feature"],
            ["diff", "origin/main...HEAD"],
            ["diff", "HEAD:path"],
            ["diff", "--stat"],
            ["diff", "HEAD"],
            ["diff", f"{TOP}/a"],
            ["diff", "--", "-x/k"],
            ["diff", "--end-of-options", "f"],
            ["diff", "--src-prefix=../"],
            ["diff", "./d1/../f"],
        ]:
            with self.subTest(argv=argv):
                self.assertEqual(run_main(argv), [(REAL, [REAL, *argv])])

    def test_input_options_are_refused_for_log_show_diff(self):
        # They read standard input or a file that may lie outside the repository (research D19).
        for sub in ["log", "show", "diff"]:
            for opt in [
                ["--stdin"], ["-O../x"], ["-Ox"], ["-O", "x"], ["--orderfile=x"],
                ["--orderfile", "x"], ["--order=x"], ["--orderf", "x"], ["--std"],
                ["-RO../x"], ["-pO", "x"], ["-sOx"], ["-uO", "x"],
            ]:
                with self.subTest(sub=sub, opt=opt):
                    self.assert_refused([sub, *opt, "HEAD"])

    def test_a_refused_global_option_is_refused_before_the_probe(self):
        probes = []

        def probe():
            probes.append(1)
            return (TOP, TOP, GIT_DIR)

        for argv in [
            ["-c", "core.pager=x", "status"], ["--config-env=core.pager=X", "log"],
            ["-c", "core.sshCommand=x", "push", "origin"],
        ]:
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    git_wrapper.main(
                        REAL, argv, env={git_wrapper.RUN_DIR_ENV: RUN},
                        execv=lambda p, a: None, locate=probe,
                    )
        self.assertEqual(probes, [])

    def test_input_options_after_double_dash_are_paths(self):
        for argv in [["log", "--", "--stdin"], ["show", "HEAD", "--", "-Ox"]]:
            with self.subTest(argv=argv):
                self.assertEqual(run_main(argv), [(REAL, [REAL, *argv])])

    def run_with(self, found, env=None, argv=("status",)):
        calls = []
        err = io.StringIO()
        env = {git_wrapper.RUN_DIR_ENV: RUN} if env is None else env
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as ctx:
            git_wrapper.main(
                REAL, list(argv), env=env, execv=lambda p, a: calls.append(a),
                locate=lambda: found,
            )
        self.assertEqual(ctx.exception.code, 128, found)
        self.assertIn("git wrapper: refused", err.getvalue())
        self.assertEqual(calls, [])

    def test_every_subcommand_needs_the_run_repository(self):
        for sub in ["status", "log", "commit", "push", "fetch", "config", "add", "init"]:
            for found in [
                None,
                ("/elsewhere", "/elsewhere", "/elsewhere/.git"),
                (f"{TOP}/nested", f"{TOP}/nested", f"{TOP}/nested/.git"),
                (TOP, TOP, "/elsewhere/.git"),
                (TOP, TOP, f"{TOP}/.git/modules/x"),
                (TOP, TOP, f"{RUN}/origin.git"),
                (RUN, RUN, f"{RUN}/.git"),
            ]:
                with self.subTest(sub=sub, found=found):
                    self.run_with(found, argv=(sub,))

    def test_missing_or_unusable_run_dir_is_refused(self):
        for env in [{}, {git_wrapper.RUN_DIR_ENV: ""}, {git_wrapper.RUN_DIR_ENV: "relative"}]:
            with self.subTest(env=env):
                self.run_with((TOP, TOP, GIT_DIR), env=env)

    def test_the_run_repository_is_allowed_from_a_subdirectory(self):
        found = (TOP, f"{TOP}/a/b", GIT_DIR)
        calls = []
        git_wrapper.main(
            REAL, ["status"], env={git_wrapper.RUN_DIR_ENV: RUN},
            execv=lambda p, a: calls.append(a), locate=lambda: found,
        )
        self.assertEqual(calls, [[REAL, "status"]])

    def test_output_indicator_options_are_allowed(self):
        argv = ["diff", "--output-indicator-new=+"]
        self.assertEqual(run_main(argv), [(REAL, [REAL, *argv])])

    def test_exec_primitives_are_refused_for_network_subcommands(self):
        for sub in ["push", "fetch", "pull", "ls-remote", "clone"]:
            for opt in [
                ["--upload-pack", "x"], ["--upload-pack=x"], ["--receive-pack=x"],
                ["--exec=x"], ["--exe=x"], ["-c", "k=v"], ["-ck=v"], ["--config-env=a=B"],
                ["--config-env", "a=B"],
            ]:
                with self.subTest(sub=sub, opt=opt):
                    self.assert_refused([sub, *opt, "origin"])
        self.assert_refused(["remote", "update", "-c", "k=v"])

    def test_short_u_is_refused_except_for_push(self):
        for sub in ["fetch", "pull", "clone", "ls-remote"]:
            with self.subTest(sub=sub):
                self.assert_refused([sub, "-u", "x", "origin"])
                self.assert_refused([sub, "-ux", "origin"])
        calls = run_main(["push", "-u", "origin", "main"])
        expected = [REAL, "-c", REWRITE, "push", "-u", "origin", "main"]
        self.assertEqual(calls, [(REAL, expected)])

    def test_short_option_clusters_with_c_are_refused(self):
        for sub in ["push", "fetch", "pull", "ls-remote", "clone"]:
            for cluster in ["-qc", "-vc", "-cq", "-fcv"]:
                with self.subTest(sub=sub, cluster=cluster):
                    self.assert_refused([sub, cluster, "k=v", "origin"])
        self.assert_refused(["remote", "update", "-qc", "k=v"])

    def test_short_option_clusters_with_u_are_refused_except_for_push(self):
        for sub in ["fetch", "pull", "clone", "ls-remote"]:
            for cluster in ["-qu", "-vu", "-uq"]:
                with self.subTest(sub=sub, cluster=cluster):
                    self.assert_refused([sub, cluster, "x", "origin"])
        for cluster in ["-fu", "-vu", "-uf"]:
            with self.subTest(cluster=cluster):
                expected = [REAL, "-c", REWRITE, "push", cluster, "origin", "main"]
                self.assertEqual(run_main(["push", cluster, "origin", "main"]), [(REAL, expected)])

    def test_harmless_clusters_are_allowed(self):
        for argv in (["fetch", "-qp", "origin"], ["clone", "-qv", "u", "d"], ["push", "-fn"]):
            with self.subTest(argv=argv):
                self.assertEqual(len(run_main(argv)), 1)

    def test_options_after_double_dash_are_not_options(self):
        argv = ["push", "origin", "--", "-c"]
        self.assertEqual(len(run_main(argv)), 1)

    def test_dangerous_global_config_is_refused(self):
        keys = [
            "core.fsmonitor=x", "core.hooksPath=/x", "core.sshCommand=x",
            "url.a.insteadOf=b", "url.a.pushInsteadOf=b", "protocol.ext.allow=always",
            "uploadpack.packObjectsHook=x", "receivepack.x=y", "CORE.FSMONITOR=x",
        ]
        for key in keys:
            for form in (["-c", key], ["--config-env", key], [f"--config-env={key}"]):
                with self.subTest(form=form):
                    self.assert_refused([*form, "status"])
                    self.assert_refused([*form, "push"])

    def test_global_config_outside_the_allowlist_is_refused(self):
        bad = [
            "alias.x=!id", "core.pager=x", "core.editor=x", "diff.external=x",
            "credential.helper=x", "include.path=/x", "includeIf.gitdir:/.path=/x",
            "filter.a.clean=x", "gpg.program=x", "commit.gpgsign=true", "commit.gpgsign",
            "user.signingkey=x", "color", "core.quotepathx=1",
        ]
        for config in bad:
            for form in (["-c", config], [f"--config-env={config}"]):
                with self.subTest(form=form):
                    self.assert_refused([*form, "log"])
        self.assert_refused(["--config-env", "commit.gpgsign=FLAG", "commit"])

    def test_global_config_on_the_allowlist_is_allowed(self):
        good = [
            "user.name=t", "user.email=t@e.x", "color.ui=never", "COLOR.diff.new=red",
            "core.quotepath=false", "advice.detachedHead=false", "init.defaultBranch=main",
            "commit.gpgsign=false", "pull.rebase=true", "pull.ff=only",
        ]
        for config in good:
            with self.subTest(config=config):
                argv = ["-c", config, "commit"]
                self.assertEqual(run_main(argv), [(REAL, [REAL, *argv])])
        self.assertEqual(len(run_main(["--config-env=user.name=NAME", "status"])), 1)

    def test_harmless_global_config_is_allowed(self):
        argv = ["-c", "user.name=t", "status"]
        self.assertEqual(run_main(argv), [(REAL, [REAL, *argv])])
        calls = run_main(["-c", "user.name=t", "push"])
        self.assertEqual(calls[0][1], [REAL, "-c", "user.name=t", "-c", REWRITE, "push"])

    def test_install_uses_module_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            real = tmp / "real-git"
            real.write_text("x")
            real.chmod(0o755)
            copy = tmp / "my lib" / "git_wrapper.py"
            copy.parent.mkdir()
            copy.write_text("x")
            launcher = git_wrapper.install(tmp / "bin", real_git=str(real), module_path=str(copy))
            text = launcher.read_text(encoding="utf-8")
            self.assertIn(shlex.quote(str(copy)), text)
            for bad in ["", "rel.py", str(tmp / "missing.py"), str(tmp)]:
                with self.subTest(bad=bad), self.assertRaises(ValueError):
                    git_wrapper.install(tmp / "bin2", real_git=str(real), module_path=bad)

    def test_install_rejects_interpreter_older_than_3_11(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            fake = tmp / "old-python"
            fake.write_text("#!/bin/sh\nexit 1\n")
            fake.chmod(0o755)
            real = tmp / "real-git"
            real.write_text("x")
            real.chmod(0o755)
            with self.assertRaises(ValueError):
                git_wrapper.install(tmp / "bin", real_git=str(real), python=str(fake))
            self.assertFalse((tmp / "bin" / "git").exists())


class EntryTest(unittest.TestCase):
    def test_main_entry_parses_only_leading_real_git(self):
        with tempfile.TemporaryDirectory() as tmp:
            real = Path(tmp) / "fake git"
            top = Path(tmp).resolve() / "run" / "repo"
            (top / ".git").mkdir(parents=True)
            # The wrapper's one probe call (`rev-parse`) gets the run repository's answer.
            real.write_text(
                '#!/bin/sh\nif [ "$1" = rev-parse ]; then '
                f'printf "%s\\n" "{top}" "" "{top}/.git"; exit 0; fi\n'
                'for a in "$@"; do echo "[$a]"; done\n'
            )
            real.chmod(0o755)
            out = subprocess.run(
                [sys.executable, "-I", git_wrapper.__file__, "--real-git", str(real),
                 "status", "--real-git", "x y"],
                capture_output=True, text=True, timeout=30, check=True,
                env={git_wrapper.RUN_DIR_ENV: str(top.parent)},
            ).stdout
            self.assertEqual(out.split(), ["[status]", "[--real-git]", "[x", "y]"])


@unittest.skipUnless(git_wrapper.find_real_git(), "git not installed")
class IntegrationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name).resolve()
        self.run_dir = root / "run"
        self.run_dir.mkdir()
        self.real = git_wrapper.find_real_git()
        bin_dir = root / "bin"
        py_dir = root / "py dir"
        py_dir.mkdir()
        (py_dir / "python3").symlink_to(sys.executable)
        git_wrapper.install(bin_dir, real_git=self.real, python=str(py_dir / "python3"))
        (root / "gitconfig").write_text("")
        self.env = {
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "HOME": str(root),
            "GIT_CONFIG_GLOBAL": str(root / "gitconfig"),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_ALLOW_PROTOCOL": "file",
            git_wrapper.RUN_DIR_ENV: str(self.run_dir),
        }
        self.setup_git("init", "--bare", "-b", "main", str(self.run_dir / "origin.git"), cwd=root)
        self.work = self.run_dir / "repo"
        self.work.mkdir()
        self.setup_git("init", "-b", "main", cwd=self.work)
        self.git("remote", "add", "origin", URL, cwd=self.work)
        (self.work / "f").write_text("x")
        self.git("add", "f", cwd=self.work)
        self.git("-c", "user.name=t", "-c", "user.email=t@e.x", "commit", "-m", "m", cwd=self.work)

    def setup_git(self, *args, cwd):
        """Real git for fixtures the wrapper refuses to create (outside the run repository)."""
        return self.git(*args, cwd=cwd, executable=self.real)

    def git(self, *args, cwd, check=True, input=None, executable="git"):
        return subprocess.run(
            [executable, *args], cwd=cwd, env=self.env, capture_output=True, text=True,
            timeout=30, check=check, input=input,
        )

    def test_push_through_wrapper_reaches_bare_repo(self):
        self.git("push", "origin", "main", cwd=self.work)
        bare = str(self.run_dir / "origin.git")
        out = self.setup_git("--git-dir", bare, "rev-parse", "main", cwd=self.work)
        self.assertEqual(len(out.stdout.strip()), 40)

    def test_get_url_still_shows_github(self):
        out = self.git("remote", "get-url", "--push", "origin", cwd=self.work)
        self.assertEqual(out.stdout.strip(), URL)

    def test_diff_of_a_sibling_file_is_refused_before_any_output(self):
        (self.work.parent / "sibling.txt").write_text("SECRET\n")
        (self.work / "-x").mkdir()
        for args in [
            ("diff", "../sibling.txt", "/dev/null"),
            ("diff", "--stat", "../sibling.txt", "f"),
            ("diff", "--", "/dev/null", "../sibling.txt"),
            ("diff", "--", "f", "-x/../../sibling.txt"),
            ("diff", "--", "-x/../../sibling.txt", "-x/k"),
            ("diff", "--end-of-options", "f", "-x/../../sibling.txt"),
            ("diff", f"../{self.work.name}/f", "-"),
            ("diff", "--", f"../{self.work.name}/f", "-"),
            ("diff", "-"),
        ]:
            with self.subTest(args=args):
                result = self.git(*args, cwd=self.work, check=False, input="SECRET\n")
                self.assertEqual(result.returncode, 128)
                self.assertIn("git wrapper: refused", result.stderr)
                self.assertEqual(result.stdout, "")
        self.assertEqual(self.git("diff", "HEAD", cwd=self.work).returncode, 0)
        outside = self.git("diff", "f", "f", cwd=self.work.parent, check=False)
        self.assertEqual(outside.returncode, 128)
        self.assertEqual(outside.stdout, "")

    def test_nested_repository_is_refused(self):
        nested = self.work / "nested"
        nested.mkdir()
        self.setup_git("init", "-b", "main", cwd=nested)
        (nested / "g").write_text("y")
        for args in [("status",), ("log",), ("add", "g"), ("diff", "HEAD"), ("push", "origin")]:
            with self.subTest(args=args):
                result = self.git(*args, cwd=nested, check=False)
                self.assertEqual(result.returncode, 128)
                self.assertIn("git wrapper: refused", result.stderr)
        self.assertEqual(self.git("status", cwd=self.work).returncode, 0)

    def test_a_repository_elsewhere_is_refused(self):
        other = self.run_dir.parent / "other"
        other.mkdir()
        self.setup_git("init", "-b", "main", cwd=other)
        self.assertEqual(self.git("status", cwd=other, check=False).returncode, 128)
        self.assertEqual(
            self.git("-C", str(other), "status", cwd=self.work, check=False).returncode, 128
        )
        self.assertEqual(self.git("status", cwd=self.run_dir, check=False).returncode, 128)

    def test_a_redirected_git_dir_is_refused(self):
        env = dict(self.env, GIT_DIR=str(self.run_dir / "origin.git"))
        result = subprocess.run(
            ["git", "status"], cwd=self.work, env=env, capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 128)
        self.assertIn("git wrapper: refused", result.stderr)

    def test_input_options_are_refused_with_stdin_fed(self):
        (self.work / "order.txt").write_text("f\n")
        for args in [
            ("diff", "--stdin"),
            ("log", "--stdin"),
            ("show", "--stdin"),
            ("diff", "-Oorder.txt", "HEAD"),
            ("diff", "-O", "order.txt", "HEAD"),
            ("diff", "-RO", "order.txt", "HEAD"),
            ("diff", "-RO../order.txt", "HEAD"),
            ("log", "-pO", "order.txt"),
            ("show", "-sOorder.txt"),
            ("log", "-p", "--orderfile=order.txt"),
            ("show", "--orderfile", "order.txt"),
        ]:
            with self.subTest(args=args):
                result = self.git(*args, cwd=self.work, check=False, input="HEAD\n")
                self.assertEqual(result.returncode, 128)
                self.assertIn("git wrapper: refused", result.stderr)
                self.assertEqual(result.stdout, "")

    def test_direct_push_fails_fast(self):
        env = dict(self.env)
        start = time.monotonic()
        result = subprocess.run(
            [self.real, "push", URL, "main"], cwd=self.work, env=env,
            capture_output=True, text=True, timeout=30,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("transport 'https' not allowed", result.stderr)
        self.assertLess(time.monotonic() - start, 10)


if __name__ == "__main__":
    unittest.main()
