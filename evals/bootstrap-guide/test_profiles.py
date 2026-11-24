import json
import unittest

import profiles

RUN_DIR = "/tmp/eval-run-1/repo"
HOME = "/Users/maintainer"
PREFIX = "/Users/maintainer/.pyenv/versions/3.12"

GUIDE_ALLOW = [
    "Bash(python3 scripts/bootstrap.py *)",
    "Bash(git status --porcelain)",
    'Bash(echo "exit $?")',
    "Read",
    "Grep",
    "Glob",
    "Skill(bootstrap-guide)",
    "Skill(bootstrap-guide *)",
    "Skill(speckit-constitution)",
    "Skill(speckit-constitution *)",
    "Skill(roadmap-write)",
    "Skill(roadmap-write *)",
]
MERGE_EXTRA = ["Bash(git switch *)", "Bash(git pull --ff-only origin *)"]
SKILLS_EXTRA = [
    "Write(.specify/memory/**)",
    "Edit(.specify/memory/**)",
    "Write(ROADMAP.md)",
    "Edit(ROADMAP.md)",
    "Bash(.specify/scripts/bash/resolve-template.sh constitution-template --json)",
    "Bash(python3 scripts/check_roadmap.py)",
]
STOP_EXTRA = [
    "Bash(git remote -v)",
    "Bash(git remote get-url *)",
    "Bash(git remote set-url origin *)",
    "Bash(gh repo set-default *)",
]
REVIEW_EXTRA = [
    "Write(bootstrap-review-notes.md)",
    "Bash(git diff *)",
    "Bash(git log *)",
    "Bash(git show *)",
]
BUILDS_ON = {"merge": "guide", "skills": "guide", "stop": "guide", "review": "skills"}


class ProfilesTest(unittest.TestCase):
    def allow(self, name):
        return profiles.settings(name, RUN_DIR, HOME)["permissions"]["allow"]

    def test_profile_names(self):
        self.assertEqual(
            set(profiles.PROFILES), {"guide", "merge", "skills", "stop", "review"}
        )
        self.assertEqual(profiles.DEFAULT_PROFILE, "guide")

    def test_unknown_profile_is_rejected(self):
        with self.assertRaises(ValueError):
            profiles.tools("nope")
        with self.assertRaises(ValueError):
            profiles.settings("nope", RUN_DIR, HOME)

    def test_tools_lists(self):
        base = ["Bash", "Read", "Grep", "Glob", "Skill"]
        self.assertEqual(profiles.tools("guide"), base)
        self.assertEqual(profiles.tools("merge"), base)
        self.assertEqual(profiles.tools("stop"), base)
        self.assertEqual(profiles.tools("skills"), base + ["Write", "Edit"])
        self.assertEqual(
            profiles.tools("review"), base + ["Write", "Edit", "Agent"]
        )

    def test_allow_rules_exact(self):
        self.assertEqual(self.allow("guide"), GUIDE_ALLOW)
        self.assertEqual(self.allow("merge"), GUIDE_ALLOW + MERGE_EXTRA)
        self.assertEqual(self.allow("skills"), GUIDE_ALLOW + SKILLS_EXTRA)
        self.assertEqual(self.allow("stop"), GUIDE_ALLOW + STOP_EXTRA)
        self.assertEqual(
            self.allow("review"), GUIDE_ALLOW + SKILLS_EXTRA + REVIEW_EXTRA
        )

    def test_exit_suffix_allowed_as_own_part(self):
        for name in profiles.PROFILES:
            self.assertIn('Bash(echo "exit $?")', self.allow(name))

    def test_each_profile_is_superset_of_its_base(self):
        for name, base in BUILDS_ON.items():
            self.assertLessEqual(set(profiles.tools(base)), set(profiles.tools(name)))
            self.assertLessEqual(set(self.allow(base)), set(self.allow(name)))

    def test_every_profile_denies_reads_and_writes_outside_repo(self):
        for name in profiles.PROFILES:
            settings = profiles.settings(name, RUN_DIR, HOME)
            deny = settings["permissions"]["deny"]
            for tool in ("Read", "Edit", "Write"):
                self.assertIn(f"{tool}(../**)", deny)
                self.assertIn(f"{tool}(~/**)", deny)
            fs = settings["sandbox"]["filesystem"]
            self.assertEqual(fs["denyRead"], [HOME])
            self.assertNotIn("/", fs["denyRead"])
            self.assertEqual(fs["allowRead"], [RUN_DIR])
            self.assertEqual(fs["allowWrite"], [RUN_DIR])

    def test_deny_never_blocks_an_allow_rule(self):
        for name in profiles.PROFILES:
            deny = set(profiles.settings(name, RUN_DIR, HOME)["permissions"]["deny"])
            self.assertFalse(deny & set(self.allow(name)))

    def test_extra_read_paths_are_allowed(self):
        fs = profiles.settings("guide", RUN_DIR, HOME, extra_read=[PREFIX])["sandbox"][
            "filesystem"
        ]
        self.assertEqual(fs["allowRead"], [RUN_DIR, PREFIX])
        self.assertEqual(fs["allowWrite"], [RUN_DIR])

    def test_unsandboxed_retries_disabled(self):
        for name in profiles.PROFILES:
            sandbox = profiles.settings(name, RUN_DIR, HOME)["sandbox"]
            self.assertIs(sandbox["allowUnsandboxedCommands"], False)

    def test_run_dir_inside_home_is_rejected(self):
        for run_dir in (HOME + "/work/repo", HOME, HOME + "/a/../work"):
            with self.assertRaisesRegex(ValueError, "outside the home directory"):
                profiles.settings("guide", run_dir, HOME)

    def test_run_dir_outside_home_is_accepted(self):
        profiles.settings("guide", HOME + "-other/repo", HOME)

    def test_extra_write_paths_are_appended(self):
        log = "/tmp/eval-run-1/gh-calls.log"
        fs = profiles.settings("guide", RUN_DIR, HOME, extra_write=[log])["sandbox"][
            "filesystem"
        ]
        self.assertEqual(fs["allowWrite"], [RUN_DIR, log])
        self.assertEqual(fs["allowRead"], [RUN_DIR])

    def test_extra_write_under_home_or_relative_is_rejected(self):
        for path in (HOME + "/x.log", HOME, "relative/x.log"):
            with self.assertRaises(ValueError):
                profiles.settings("guide", RUN_DIR, HOME, extra_write=[path])

    def test_only_the_three_skills_may_be_invoked(self):
        for name in profiles.PROFILES:
            skills = [r for r in self.allow(name) if r.startswith("Skill")]
            self.assertNotIn("Skill", skills)
            self.assertEqual(
                {r.split("(")[1].split(")")[0].split(" ")[0] for r in skills},
                {"bootstrap-guide", "speckit-constitution", "roadmap-write"},
            )
            self.assertIn("Skill", profiles.tools(name))

    def test_agent_configuration_cannot_be_written(self):
        for name in profiles.PROFILES:
            settings = profiles.settings(name, RUN_DIR, HOME)
            deny = settings["permissions"]["deny"]
            for rule in (
                "Edit(.claude/**)",
                "Write(.claude/**)",
                "Edit(.agents/**)",
                "Write(.agents/**)",
                "Write(.mcp.json)",
                "Edit(.mcp.json)",
                "Edit(**/.claude/**)",
                "Write(**/.claude/**)",
                "Edit(**/.mcp.json)",
                "Write(**/.mcp.json)",
                "Edit(agent-definitions/**)",
                "Write(agent-definitions/**)",
            ) + tuple(
                f"{tool}({path})"
                for tool in ("Edit", "Write")
                for name in ("CLAUDE.md", "CLAUDE.local.md", "AGENTS.md")
                for path in (name, f"**/{name}")
            ):
                self.assertIn(rule, deny)
            # Every Edit and Write allow rule targets one of the known files, so none can
            # reach what is denied above.
            for rule in self.allow(name):
                if rule.startswith(("Edit(", "Write(")):
                    self.assertIn(
                        rule.split("(", 1)[1].rstrip(")"),
                        {".specify/memory/**", "ROADMAP.md", "bootstrap-review-notes.md"},
                        rule,
                    )
            # The engine's own `agent_setup.py sync` writes `.claude/agents`, so the sandbox
            # may not deny the whole of `.claude`; the Edit and Write tools still may not.
            deny_write = settings["sandbox"]["filesystem"]["denyWrite"]
            self.assertEqual(
                deny_write,
                [
                    f"{RUN_DIR}/.claude/settings.json",
                    f"{RUN_DIR}/.claude/settings.local.json",
                    f"{RUN_DIR}/.claude/settings*.json",
                    f"{RUN_DIR}/.claude/commands",
                    f"{RUN_DIR}/.claude/skills",
                    f"{RUN_DIR}/.claude/hooks",
                    f"{RUN_DIR}/.claude/CLAUDE.md",
                    f"{RUN_DIR}/.claude/rules",
                    f"{RUN_DIR}/.claude/output-styles",
                    f"{RUN_DIR}/CLAUDE.md",
                    f"{RUN_DIR}/CLAUDE.local.md",
                    f"{RUN_DIR}/AGENTS.md",
                    f"{RUN_DIR}/.mcp.json",
                    f"{RUN_DIR}/.agents",
                    f"{RUN_DIR}/agent-definitions",
                    f"{RUN_DIR}/agent-definitions/agents.toml",
                ],
            )
            self.assertNotIn(f"{RUN_DIR}/.claude", deny_write)
            self.assertNotIn(f"{RUN_DIR}/.claude/agents", deny_write)

    def test_the_skill_names_are_defined_once(self):
        self.assertEqual(
            profiles.EVAL_SKILLS, ("bootstrap-guide", "speckit-constitution", "roadmap-write")
        )

    def test_hooks_are_disabled_for_every_profile(self):
        for name in profiles.PROFILES:
            self.assertIs(profiles.settings(name, RUN_DIR, HOME)["disableAllHooks"], True)

    def test_inline_python_is_denied_for_every_profile(self):
        for name in profiles.PROFILES:
            deny = profiles.settings(name, RUN_DIR, HOME)["permissions"]["deny"]
            for rule in ("Bash(python3 -c:*)", "Bash(python3 -m:*)", "Bash(python3 -:*)"):
                self.assertIn(rule, deny)

    def test_memory_off_and_sandbox_on(self):
        for name in profiles.PROFILES:
            settings = profiles.settings(name, RUN_DIR, HOME)
            self.assertIs(settings["autoMemoryEnabled"], False)
            self.assertIs(settings["sandbox"]["enabled"], True)
            self.assertIs(settings["sandbox"]["autoAllowBashIfSandboxed"], False)

    def test_settings_are_json_serialisable(self):
        for name in profiles.PROFILES:
            json.dumps(profiles.settings(name, RUN_DIR, HOME))

    def test_returns_fresh_copies(self):
        profiles.tools("guide").append("X")
        profiles.settings("guide", RUN_DIR, HOME)["permissions"]["allow"].append("X")
        self.assertNotIn("X", profiles.tools("guide"))
        self.assertNotIn("X", self.allow("guide"))


if __name__ == "__main__":
    unittest.main()
