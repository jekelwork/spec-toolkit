"""Tool profiles and permission settings for the agent under test (research D4)."""

from pathlib import Path

DEFAULT_PROFILE = "guide"

_SKILLS_ALLOW = [
    # Exact files only: a pattern would let the agent plant a file that a later allowed command
    # runs (research D19).
    "Write(./.specify/memory/constitution.md)",
    "Edit(./.specify/memory/constitution.md)",
    "Write(./ROADMAP.md)",
    "Edit(./ROADMAP.md)",
    "Bash(.specify/scripts/bash/resolve-template.sh constitution-template --json)",
    "Bash(python3 scripts/check_roadmap.py)",
]

# The skills the scenarios use; `harness` checks their frontmatter and the allow rules name them.
EVAL_SKILLS = ("bootstrap-guide", "speckit-constitution", "roadmap-write")

# Allow rules for the skills above (syntax `Skill(name)` and `Skill(name *)` with arguments, from
# the Claude Code skills documentation). All skills are in the repository, because the engine's
# agent-setup check needs them. Without a rule, a skill whose properties need permission (such as
# `allowed-tools`) asks and is denied in `-p` mode, but a skill with only safe properties may still
# be invokable. So this is a behaviour and permission control, not the security boundary: that is
# the Bash, Edit and Write rules, the sandbox and the harness checks of the repository.
_SKILL_ALLOW = [
    rule for skill in EVAL_SKILLS for rule in (f"Skill({skill})", f"Skill({skill} *)")
]

# name -> (profile it builds on, tools it adds, allow rules it adds)
_DEFINITIONS = {
    "guide": (
        None,
        ["Bash", "Read", "Grep", "Glob", "Skill"],
        [
            "Bash(python3 scripts/bootstrap.py *)",
            "Bash(git status --porcelain)",
            # Claude Code checks each part of a compound command: the suffix is its own rule.
            'Bash(echo "exit $?")',
            # Claude Code applies Read rules to Grep and Glob; a bare rule would let them read
            # anywhere, `Read(./**)` limits all three to the repository (research D18).
            "Read(./**)",
            *_SKILL_ALLOW,
        ],
    ),
    "merge": (
        "guide",
        [],
        ["Bash(git switch *)", "Bash(git pull --ff-only origin *)"],
    ),
    "skills": ("guide", ["Write", "Edit"], _SKILLS_ALLOW),
    "stop": (
        "guide",
        [],
        [
            "Bash(git remote -v)",
            "Bash(git remote get-url *)",
            "Bash(git remote set-url origin *)",
            "Bash(gh repo set-default *)",
        ],
    ),
    "review": (
        "skills",
        ["Agent"],
        [
            "Write(./bootstrap-review-notes.md)",
            "Bash(git diff *)",
            "Bash(git log *)",
            "Bash(git show *)",
        ],
    ),
}

PROFILES = tuple(_DEFINITIONS)

# The home directory is denied for every profile. There is no `../**` rule: Claude Code turns
# these deny rules into Bash sandbox write blocks, and `../**` covered the whole run directory.
# The Read, Grep and Glob tools are limited to the repository by the `Read(./**)` allow rule
# (see the guide profile); Edit and Write by their path-scoped allows. Without a matching
# allow, `-p` refuses. Bash writes only the repository and the call log (its sandbox) and
# reads only what its allowed commands read; the git wrapper refuses `git diff` paths outside
# the repository (explicit or implicit `--no-index`; research D18, "Boundary after the change").
_DENY = [
    f"{tool}(~/**)" for tool in ("Read", "Edit", "Write")
] + [
    # An extra layer against changing the working directory (research D19); whether Claude Code
    # honors these is not verified, so the git wrapper and the integrity check do not rely on it.
    "Bash(cd *)",
    "Bash(pushd *)",
] + [
    # A backstop only: the allow list is the boundary. No allow rule matches inline Python
    # anyway; these deny it by name in case a rule is ever widened.
    f"Bash(python3 {flag}:*)"
    for flag in ("-c", "-m", "-")
] + [
    # What configures the agent itself: settings, hooks, commands, MCP servers and skills.
    # `**/` covers a nested copy too; the top-level rules stay in case `**/` does not match the root.
    "Edit(.claude/**)",
    "Write(.claude/**)",
    "Edit(**/.claude/**)",
    "Write(**/.claude/**)",
    "Edit(.agents/**)",
    "Write(.agents/**)",
    "Edit(.mcp.json)",
    "Write(.mcp.json)",
    "Edit(**/.mcp.json)",
    "Write(**/.mcp.json)",
    # Instruction files the agent reads, and what `.claude/agents` is generated from. No profile
    # needs to write any of them (the profiles write `.specify/memory`, `ROADMAP.md` and the
    # review notes only).
    *[
        f"{tool}({path})"
        for tool in ("Edit", "Write")
        for path in (
            "CLAUDE.md",
            "**/CLAUDE.md",
            "CLAUDE.local.md",
            "**/CLAUDE.local.md",
            "AGENTS.md",
            "**/AGENTS.md",
            "agent-definitions/**",
        )
    ],
]


def _resolve(profile):
    """Return (tools, allow rules) of a profile, including those of the profile it builds on."""
    if profile not in _DEFINITIONS:
        raise ValueError(
            f"unknown profile {profile!r}; expected one of {', '.join(PROFILES)}"
        )
    base, added_tools, added_allow = _DEFINITIONS[profile]
    base_tools, base_allow = _resolve(base) if base else ([], [])
    return (
        base_tools + [t for t in added_tools if t not in base_tools],
        base_allow + [a for a in added_allow if a not in base_allow],
    )


def tools(profile):
    """The `--tools` list of a profile."""
    return _resolve(profile)[0]


def settings(profile, run_dir, home, extra_read=(), extra_write=()):
    """The `--settings` dict of a profile for a temporary repository at `run_dir`.

    `home` is the maintainer's home directory, hidden from the agent; `extra_read` lists paths under
    it that stay readable (for example the Python interpreter's prefix). `extra_write` lists
    absolute paths outside `home` that are writable besides `run_dir` (the gh call-log file).
    """
    if Path(run_dir).resolve().is_relative_to(Path(home).resolve()):
        raise ValueError(
            f"the temporary repository {str(run_dir)!r} must live outside the home directory "
            f"{str(home)!r} (for example in the system temp dir): the home deny rules would "
            "block the agent from reading its own repository"
        )
    for path in extra_write:
        if not Path(path).is_absolute():
            raise ValueError(f"extra write path {str(path)!r} must be absolute")
        if Path(path).resolve().is_relative_to(Path(home).resolve()):
            raise ValueError(
                f"extra write path {str(path)!r} must be outside the home directory {str(home)!r}"
            )
    run_dir = str(run_dir)
    return {
        "autoMemoryEnabled": False,
        # Hooks would run outside the sandbox; the harness refuses to build a repository with
        # project settings, and this switches off any other source.
        "disableAllHooks": True,
        "sandbox": {
            "enabled": True,
            "autoAllowBashIfSandboxed": False,
            "allowUnsandboxedCommands": False,
            "filesystem": {
                "denyRead": [str(home)],
                "allowRead": [run_dir, *map(str, extra_read)],
                "allowWrite": [run_dir, *map(str, extra_write)],
                # Shell commands may not rewrite what configures the agent either. Not
                # `.claude/agents`: the engine's `agent_setup.py sync` generates it, and the
                # harness checks every file there against what the sync would write. The two
                # settings files are listed literally next to the glob; the whole of `.claude`
                # is not denied, because how an allow rule for `.claude/agents` would rank
                # against it is not verified. These entries belong to Claude Code's own
                # sandbox, so a refused write cannot be tested without a paid agent run.
                "denyWrite": [
                    f"{run_dir}/{name}"
                    for name in (
                        ".claude/settings.json",
                        ".claude/settings.local.json",
                        ".claude/settings*.json",
                        ".claude/commands",
                        ".claude/skills",
                        ".claude/hooks",
                        ".claude/CLAUDE.md",
                        ".claude/rules",
                        ".claude/output-styles",
                        "CLAUDE.md",
                        "CLAUDE.local.md",
                        "AGENTS.md",
                        ".mcp.json",
                        ".agents",
                        "agent-definitions",
                        "agent-definitions/agents.toml",
                    )
                ],
            },
        },
        "permissions": {"allow": _resolve(profile)[1], "deny": list(_DENY)},
    }
