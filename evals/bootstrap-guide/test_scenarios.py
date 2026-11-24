import tempfile
import textwrap
import unittest
from pathlib import Path

import harness
import profiles
import rules

RULE = rules.RULES[0].id

GOOD = f"""
name = "demo-case"
description = "A demo situation."
fixture = "demo-state"
mode = "guide me"
rules = ["{RULE}"]
turns = [
  {{ say = "start" }},
  {{ run_shown = true, expect = "preview" }},
  {{ say_after_output = "done", expect = {{ pattern = "ok+" }} }},
]
"""


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.scenarios = self.root / "scenarios"
        self.fixtures = self.root / "fixtures"
        self.scenarios.mkdir()
        (self.fixtures / "demo-state" / "files").mkdir(parents=True)
        (self.fixtures / "demo-state" / "files" / "a.txt").write_text("a")

    def write(self, name="demo-case", text=GOOD):
        path = self.scenarios / f"{name}.toml"
        path.write_text(textwrap.dedent(text))
        return path

    def errors(self, text, name="demo-case"):
        self.write(name, text)
        return harness.validate(self.scenarios, self.fixtures)

    def assert_error(self, text, fragment, name="demo-case"):
        errs = self.errors(text, name)
        self.assertTrue(any(fragment in e for e in errs), errs)
        with self.assertRaises(harness.ScenarioError):
            harness.load_scenario(self.scenarios / f"{name}.toml", self.fixtures)


class LoadTests(Base):
    def test_loads_scenario_with_defaults(self):
        s = harness.load_scenario(self.write(), self.fixtures)
        self.assertEqual(s.name, "demo-case")
        self.assertEqual(s.mode, "guide me")
        self.assertEqual(s.tools, "guide")
        self.assertEqual(s.runs, 5)
        self.assertEqual(s.rules, (RULE,))
        self.assertEqual([t.kind for t in s.turns], ["say", "run_shown", "say_after_output"])
        self.assertEqual(s.turns[1].expect, "preview")
        self.assertEqual(s.turns[2].expect_pattern, "ok+")
        with self.assertRaises(Exception):
            s.runs = 1

    def test_optional_fields(self):
        self.write(text=GOOD + 'tools = "merge"\nruns = 3\n')
        s = harness.load_scenario(self.scenarios / "demo-case.toml", self.fixtures)
        self.assertEqual((s.tools, s.runs), ("merge", 3))

    def test_load_all_sorted_and_valid(self):
        self.write()
        self.write("another-one", GOOD.replace("demo-case", "another-one"))
        loaded = harness.load_all(self.scenarios, self.fixtures)
        self.assertEqual([s.name for s in loaded], ["another-one", "demo-case"])
        self.assertEqual(harness.validate(self.scenarios, self.fixtures), [])

    def test_fake_token_in_say_allowed(self):
        text = GOOD.replace('say = "start"', 'say = "my key {fake_token}"')
        self.assertEqual(self.errors(text), [])


class ValidationTests(Base):
    def test_a_message_may_not_start_with_a_slash_command(self):
        # A slash command runs a skill without the Skill allowlist.
        for start in ("/roadmap-write now", "  /speckit-specify", "\\n\\t/x"):
            with self.subTest(start=start):
                text = GOOD.replace('say = "start"', f'say = "{start}"')
                self.assert_error(text, "slash command")
        text = GOOD.replace('say_after_output = "done"', 'say_after_output = "/help"')
        self.assert_error(text, "slash command")

    def test_a_slash_inside_a_message_is_fine(self):
        text = GOOD.replace('say = "start"', 'say = "use the path a/b and /tmp"')
        self.assertEqual(self.errors(text), [])

    def test_name_must_equal_file_name(self):
        self.assert_error(GOOD.replace('"demo-case"', '"other"', 1), "name")

    def test_name_must_be_kebab_case(self):
        self.assert_error(GOOD.replace("demo-case", "Demo_Case"), "kebab", name="Demo_Case")

    def test_unknown_fixture(self):
        self.assert_error(GOOD.replace("demo-state", "missing"), "fixture")

    def test_bad_mode(self):
        self.assert_error(GOOD.replace("guide me", "whatever"), "mode")

    def test_no_turns(self):
        self.assert_error(GOOD.split("turns")[0] + "turns = []\n", "turn")

    def test_first_turn_has_no_previous_reply(self):
        first = '{ say = "start" }'
        for replacement in (
            '{ say = "start", expect = "preview" }',
            '{ say = "start", expect = { pattern = "ok" } }',
            '{ run_shown = true }',
            '{ say_after_output = "done" }',
        ):
            with self.subTest(first=replacement):
                text = GOOD.replace(first, replacement, 1)
                self.assert_error(text, "demo-case.toml: turn 1")
                self.assert_error(text, "no previous reply")

    def test_a_plain_say_may_start_and_later_turns_may_expect(self):
        self.assertEqual(self.errors(GOOD), [])

    def test_turn_needs_exactly_one_kind(self):
        both = GOOD.replace('{ say = "start" }', '{ say = "x", run_shown = true }')
        self.assert_error(both, "exactly one")
        none = GOOD.replace('{ say = "start" }', '{ expect = "preview" }')
        self.assert_error(none, "exactly one")

    def test_unknown_turn_key(self):
        self.assert_error(GOOD.replace('{ say = "start" }', '{ say = "x", foo = 1 }'), "foo")

    def test_bad_expect(self):
        self.assert_error(GOOD.replace('expect = "preview"', 'expect = "nope"'), "expect")

    def test_bad_regex(self):
        self.assert_error(GOOD.replace("ok+", "(unclosed"), "regex")

    def test_each_expect_form(self):
        for form in ("preview", "question", "one_command", "skill_call"):
            text = GOOD.replace('expect = "preview"', f'expect = "{form}"')
            self.assertEqual(self.errors(text), [], form)

    def test_rules_non_empty_and_known(self):
        self.assert_error(GOOD.replace(f'["{RULE}"]', "[]"), "rules")
        self.assert_error(GOOD.replace(RULE, "no-such-rule"), "no-such-rule")

    def test_fixture_must_be_direct_kebab_child(self):
        outside = self.root / "outside"
        outside.mkdir()
        (self.fixtures / "escape").symlink_to(outside, target_is_directory=True)
        for bad in ("/etc", "..", ".", "../scenarios", "demo-state/files", "escape"):
            self.assert_error(GOOD.replace("demo-state", bad), "fixture")

    def test_duplicate_rule_ids(self):
        self.assert_error(GOOD.replace(f'["{RULE}"]', f'["{RULE}", "{RULE}"]'), "duplicate")

    def test_unknown_profile(self):
        self.assert_error(GOOD + 'tools = "root"\n', "tools")

    def test_every_profile_accepted(self):
        for p in profiles.PROFILES:
            self.assertEqual(self.errors(GOOD + f'tools = "{p}"\n'), [], p)

    def test_runs_positive_integer(self):
        for bad in ("0", "-1", '"3"', "true", "2.5"):
            self.assert_error(GOOD + f"runs = {bad}\n", "runs")

    def test_placeholder_only_fake_token_in_say(self):
        self.assert_error(GOOD.replace('say = "start"', 'say = "{other}"'), "placeholder")
        self.assert_error(
            GOOD.replace('say_after_output = "done"', 'say_after_output = "{fake_token}"'),
            "placeholder",
        )
        self.assert_error(GOOD.replace("ok+", "{fake_token}"), "placeholder")

    def test_regex_quantifier_is_not_a_placeholder(self):
        self.assertEqual(self.errors(GOOD.replace("ok+", "o{2}")), [])

    def test_invalid_toml(self):
        self.assert_error("name = ", "demo-case")

    def test_unknown_field(self):
        self.assert_error(GOOD + 'extra = 1\n', "extra")

    def test_validate_reports_fixture_errors(self):
        self.write()
        (self.fixtures / "demo-state" / "gh.toml").write_text('[[call]]\nargv = "gh"\n')
        errs = harness.validate(self.scenarios, self.fixtures)
        self.assertTrue(any("gh.toml" in e for e in errs), errs)

    def test_validate_rejects_symlinks_in_fixture(self):
        self.write()
        files = self.fixtures / "demo-state" / "files"
        (files / "link.txt").symlink_to(files / "a.txt")
        errs = harness.validate(self.scenarios, self.fixtures)
        self.assertTrue(any("symlink" in e for e in errs), errs)
        (files / "link.txt").unlink()
        (files / "dir-link").symlink_to(self.root, target_is_directory=True)
        errs = harness.validate(self.scenarios, self.fixtures)
        self.assertTrue(any("symlink" in e for e in errs), errs)

    def test_validate_collects_all_scenarios(self):
        self.write()
        self.write("bad-one", GOOD.replace("demo-case", "bad-one").replace("guide me", "x"))
        self.write("bad-two", GOOD.replace("demo-case", "bad-two").replace("demo-state", "zz"))
        errs = harness.validate(self.scenarios, self.fixtures)
        self.assertEqual(len(errs), 2, errs)


class GhTableTests(Base):
    def gh(self, text):
        (self.fixtures / "demo-state" / "gh.toml").write_text(textwrap.dedent(text))
        return harness.load_gh_table(self.fixtures / "demo-state")

    def test_missing_table_is_empty(self):
        self.assertEqual(harness.load_gh_table(self.fixtures / "demo-state"), ())

    def test_defaults_and_fields(self):
        calls = self.gh(
            """
            [[call]]
            argv = ["issue", "list", "**"]
            stdout = "[]"
            stderr = ""
            [[call]]
            argv = ["issue", "close", "1"]
            stdout = ""
            stderr = "boom"
            exit = 4
            writes = true
            """
        )
        self.assertEqual(calls[0].argv, ("issue", "list", "**"))
        self.assertEqual((calls[0].exit, calls[0].writes), (0, False))
        self.assertEqual((calls[1].exit, calls[1].writes, calls[1].stderr), (4, True, "boom"))

    def test_stdout_stderr_default_empty(self):
        calls = self.gh('[[call]]\nargv = ["auth", "status"]\n')
        self.assertEqual((calls[0].stdout, calls[0].stderr), ("", ""))

    def test_writes_false_needs_reason(self):
        calls = self.gh('[[call]]\nargv = ["a"]\nwrites = false\nreason = "read only"\n')
        self.assertEqual(calls[0].reason, "read only")
        self.assertEqual(self.gh('[[call]]\nargv = ["a"]\n')[0].reason, "")

    def test_query_regex(self):
        calls = self.gh('[[call]]\nargv = ["api", "graphql"]\nquery = "repo\\\\s+\\\\{"\n')
        self.assertEqual(calls[0].query, "repo\\s+\\{")
        self.assertIsNone(self.gh('[[call]]\nargv = ["a"]\n')[0].query)

    def test_rejects_bad_entries(self):
        bad = [
            '[[call]]\nargv = ["a"]\nquery = "(unclosed"\n',
            '[[call]]\nargv = ["a"]\nquery = 1\n',
            '[[call]]\nargv = ["a"]\nwrites = false\n',
            '[[call]]\nargv = ["a"]\nwrites = false\nreason = ""\n',
            '[[call]]\nargv = ["a"]\nreason = 1\n',
            '[[call]]\nargv = ["a"]\nexit = 256\n',
            '[[call]]\nargv = ["a"]\nexit = -1\n',
            '[[call]]\nargv = ["**", "a"]\n',
            '[[call]]\nstdout = ""\n',
            '[[call]]\nargv = "issue"\n',
            '[[call]]\nargv = [1]\n',
            '[[call]]\nargv = []\n',
            '[[call]]\nargv = ["a"]\nstdout = 1\n',
            '[[call]]\nargv = ["a"]\nstderr = 1\n',
            '[[call]]\nargv = ["a"]\nexit = "1"\n',
            '[[call]]\nargv = ["a"]\nexit = true\n',
            '[[call]]\nargv = ["a"]\nwrites = "yes"\n',
            '[[call]]\nargv = ["a"]\nextra = 1\n',
            "call = 3\n",
            "[[call]\n",
        ]
        for text in bad:
            with self.assertRaises(harness.ScenarioError, msg=text):
                self.gh(text)


class GitSpecTests(Base):
    def git(self, text):
        (self.fixtures / "demo-state" / "git.toml").write_text(textwrap.dedent(text))
        return harness.load_git_spec(self.fixtures / "demo-state")

    def test_optional(self):
        self.assertEqual(harness.load_git_spec(self.fixtures / "demo-state"), ())

    def test_branches_with_commits(self):
        branches = self.git(
            """
            [[branch]]
            name = "feature-x"
            commits = ["first", "second"]
            [[branch]]
            name = "empty"
            """
        )
        self.assertEqual(branches[0].name, "feature-x")
        self.assertEqual(branches[0].commits, ("first", "second"))
        self.assertEqual(branches[1].commits, ())

    def test_rejects_bad_shapes(self):
        for text in (
            "[[branch]]\ncommits = []\n",
            '[[branch]]\nname = ""\n',
            '[[branch]]\nname = "a"\ncommits = "x"\n',
            '[[branch]]\nname = "a"\ncommits = [1]\n',
            '[[branch]]\nname = "a"\nextra = 1\n',
            '[[branch]]\nname = "a"\n[[branch]]\nname = "a"\n',
            "branch = 1\n",
            '[[branch]]\nname = "-x"\n',
            '[[branch]]\nname = "a..b"\n',
            '[[branch]]\nname = "a b"\n',
            '[[branch]]\nname = "/a"\n',
        ):
            with self.assertRaises(harness.ScenarioError, msg=text):
                self.git(text)

    def test_validate_reports_git_errors(self):
        self.write()
        (self.fixtures / "demo-state" / "git.toml").write_text("branch = 1\n")
        self.assertTrue(
            any("git.toml" in e for e in harness.validate(self.scenarios, self.fixtures))
        )


class HashTests(Base):
    def hash(self):
        return harness.scenario_hash(
            harness.load_scenario(self.scenarios / "demo-case.toml", self.fixtures),
            self.fixtures,
        )

    def test_format_and_stability(self):
        self.write()
        h = self.hash()
        self.assertRegex(h, r"^sha256:[0-9a-f]{64}$")
        self.assertEqual(h, self.hash())

    def test_changes_with_scenario_file(self):
        self.write()
        before = self.hash()
        self.write(text=GOOD.replace("A demo situation.", "Changed."))
        self.assertNotEqual(before, self.hash())

    def test_changes_with_fixture_content_add_and_rename(self):
        self.write()
        files = self.fixtures / "demo-state" / "files"
        h0 = self.hash()
        (files / "a.txt").write_text("b")
        h1 = self.hash()
        (files / "b.txt").write_text("x")
        h2 = self.hash()
        (files / "b.txt").rename(files / "c.txt")
        h3 = self.hash()
        self.assertEqual(len({h0, h1, h2, h3}), 4)

    def test_changes_with_gh_table(self):
        self.write()
        before = self.hash()
        (self.fixtures / "demo-state" / "gh.toml").write_text('[[call]]\nargv = ["a"]\n')
        self.assertNotEqual(before, self.hash())

    def test_changes_with_profile_definition(self):
        self.write()
        before = self.hash()
        original = profiles._DEFINITIONS["guide"]
        profiles._DEFINITIONS["guide"] = (
            original[0],
            original[1],
            original[2] + ["Bash(extra)"],
        )
        self.addCleanup(profiles._DEFINITIONS.__setitem__, "guide", original)
        self.assertNotEqual(before, self.hash())

    def test_changes_with_executable_bit(self):
        self.write()
        target = self.fixtures / "demo-state" / "files" / "a.txt"
        before = self.hash()
        target.chmod(0o755)
        self.assertNotEqual(before, self.hash())

    def test_hash_does_not_follow_symlinks(self):
        self.write()
        files = self.fixtures / "demo-state" / "files"
        outside = self.root / "outside.txt"
        outside.write_text("1")
        (files / "link").symlink_to(outside)
        before = self.hash()
        outside.write_text("2")
        self.assertEqual(before, self.hash())

    def test_other_fixture_does_not_matter(self):
        self.write()
        before = self.hash()
        (self.fixtures / "other" / "files").mkdir(parents=True)
        (self.fixtures / "other" / "files" / "z").write_text("z")
        self.assertEqual(before, self.hash())

    def test_independent_of_machine_paths(self):
        # The hash uses placeholder paths, so it must not contain or depend on real ones.
        self.write()
        self.assertEqual(self.hash(), self.hash())
        s = harness.load_scenario(self.scenarios / "demo-case.toml", self.fixtures)
        moved = self.root / "moved"
        moved.mkdir()
        (moved / "demo-case.toml").write_bytes((self.scenarios / "demo-case.toml").read_bytes())
        import shutil

        shutil.copytree(self.fixtures, moved / "fx")
        s2 = harness.load_scenario(moved / "demo-case.toml", moved / "fx")
        self.assertEqual(
            harness.scenario_hash(s, self.fixtures), harness.scenario_hash(s2, moved / "fx")
        )


if __name__ == "__main__":
    unittest.main()
