"""Tool profiles and permission settings for the agent under test (research D4)."""

from pathlib import Path

DEFAULT_PROFILE = "guide"

_SKILLS_ALLOW = [
    "Write(.specify/memory/**)",
    "Edit(.specify/memory/**)",
    "Write(ROADMAP.md)",
    "Edit(ROADMAP.md)",
    "Bash(.specify/scripts/bash/resolve-template.sh constitution-template --json)",
    "Bash(python3 scripts/check_roadmap.py)",
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
            "Read",
            "Grep",
            "Glob",
            "Skill",
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
            "Write(bootstrap-review-notes.md)",
            "Bash(git diff *)",
            "Bash(git log *)",
            "Bash(git show *)",
        ],
    ),
}

PROFILES = tuple(_DEFINITIONS)

# Reads and writes outside the temporary repository are denied for every profile.
_DENY = [
    f"{tool}({path})" for tool in ("Read", "Edit", "Write") for path in ("../**", "~/**")
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


def settings(profile, run_dir, home, extra_read=()):
    """The `--settings` dict of a profile for a temporary repository at `run_dir`.

    `home` is the maintainer's home directory, hidden from the agent; `extra_read` lists paths under
    it that stay readable (for example the Python interpreter's prefix).
    """
    if Path(run_dir).resolve().is_relative_to(Path(home).resolve()):
        raise ValueError(
            f"the temporary repository {str(run_dir)!r} must live outside the home directory "
            f"{str(home)!r} (for example in the system temp dir): the home deny rules would "
            "block the agent from reading its own repository"
        )
    run_dir = str(run_dir)
    return {
        "autoMemoryEnabled": False,
        "sandbox": {
            "enabled": True,
            "autoAllowBashIfSandboxed": False,
            "allowUnsandboxedCommands": False,
            "filesystem": {
                "denyRead": [str(home)],
                "allowRead": [run_dir, *map(str, extra_read)],
                "allowWrite": [run_dir],
            },
        },
        "permissions": {"allow": _resolve(profile)[1], "deny": list(_DENY)},
    }
