"""Turns the agent's stream-json output into a normalized transcript.

`normalize(events, harness_messages, gh_calls, gh_call_offsets)` returns a `Transcript`, the one
shape the rule checks read (research D6). Fixture and event format:
`testdata/transcripts/README.md`.

Turns: the n-th harness message starts turn n; a `result` event ends the turn it belongs to. Every
text block and tool call carries its `turn` and a `seq` (a counter over all blocks of the run), so
a rule can compare the order of texts and tool calls, also inside one event.
"""

import re
from collections import namedtuple
from dataclasses import dataclass, field

Fence = namedtuple("Fence", "start end")  # content lines [start, end) of a fenced code block

# Commands an invoked skill runs from its own list (research D4). A Bash command that equals one of
# these exactly (after strip()), issued after that skill's `Skill` call, is a skill command, not
# the guide's own.
SKILL_COMMANDS = {
    "speckit-constitution": (
        ".specify/scripts/bash/resolve-template.sh constitution-template --json",
    ),
    "roadmap-write": ("python3 scripts/check_roadmap.py",),
}

_BANG = re.compile(
    r"<bash-input>(?P<input>.*?)</bash-input>\s*"
    r"<bash-stdout>(?P<stdout>.*?)</bash-stdout>\s*"
    r"<bash-stderr>(?P<stderr>.*?)</bash-stderr>",
    re.DOTALL,
)


def fences(text):
    """Fenced code blocks as CommonMark closes them: a closing fence needs the same character, at
    least the opening length and no info string; an unclosed fence runs to the end. Line endings
    (CRLF, CR) are normalised first. Returns `(lines, [Fence])`, where each `Fence` holds the
    content lines `[start, end)` of `lines`; the fence lines are `start - 1` and `end`."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    found = []
    i = 0
    while i < len(lines):
        m = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", lines[i])
        if not m or (m[1][0] == "`" and "`" in m[2]):
            i += 1
            continue
        closing = re.compile(rf"^ {{0,3}}{re.escape(m[1][0])}{{{len(m[1])},}}[ \t]*$")
        j = i + 1
        while j < len(lines) and not closing.match(lines[j]):
            j += 1
        found.append(Fence(i + 1, j))
        i = j + 1
    return lines, found


@dataclass
class TextBlock:
    """One assistant text block."""

    turn: int
    seq: int
    text: str
    in_subagent: bool


@dataclass
class ToolCall:
    """One tool call with its input and result.

    `denied`: the harness refused it (a `permission_denied` event). `in_subagent`: issued inside a
    sub-agent (`parent_tool_use_id` set). `by_skill`: a command an invoked skill runs itself.
    `result` is None when no result arrived.
    """

    id: str
    name: str
    input: dict
    turn: int
    seq: int
    result: str | None = None
    is_error: bool = False
    denied: bool = False
    in_subagent: bool = False
    by_skill: bool = False


@dataclass
class UserMessage:
    """A message the harness sent. `kind` is "text" or "bang" (a `!`-command message)."""

    turn: int
    kind: str
    text: str
    bash_input: str | None = None
    bash_stdout: str | None = None
    bash_stderr: str | None = None


@dataclass(frozen=True)
class GhCall:
    """One call of the gh shim (a `gh-calls.jsonl` entry), with the turn it was made in.

    `argv` is without the leading "gh"; `matched` is the scripted entry's index or None; `writes`
    says whether the call would have changed something on GitHub.
    """

    turn: int
    argv: tuple
    writes: bool
    matched: int | None = None
    query: str | None = None


@dataclass
class Transcript:
    """`meta` carries harness facts for the checks (for example the fake token, or whether the
    `run_shown` token check rejected the shown command)."""

    texts: list = field(default_factory=list)
    tool_calls: list = field(default_factory=list)
    user_messages: list = field(default_factory=list)
    results: list = field(default_factory=list)
    gh_calls: tuple = ()
    meta: dict = field(default_factory=dict)
    turns: int = 0

    @property
    def gh_writes(self):
        """The shim calls that write, in order."""
        return tuple(c for c in self.gh_calls if c.writes)

    @property
    def denied_count(self):
        """Every harness denial, sub-agents included."""
        return sum(1 for c in self.tool_calls if c.denied)

    @property
    def denied_count_main(self):
        """Denials of the main agent's own calls (not sub-agent calls)."""
        return sum(1 for c in self.tool_calls if c.denied and not c.in_subagent)


def _content_text(content):
    if isinstance(content, str):
        return content
    return "\n".join(
        block.get("text", "") for block in content or [] if isinstance(block, dict)
    )


def _message_text(message):
    content = message.get("content")
    if isinstance(content, str):
        return content
    return "".join(b.get("text", "") for b in content or [] if b.get("type") == "text")


def _user_message(turn, line):
    text = _message_text(line.get("message", {}))
    bang = _BANG.search(text)
    if bang:
        return UserMessage(
            turn, "bang", text, bang["input"], bang["stdout"], bang["stderr"]
        )
    return UserMessage(turn, "text", text)


def _is_skill_command(command, invoked, skill_commands):
    # Exact match only: the `skills` profile allows these commands exactly, so a compound
    # command that starts with one is the guide's own.
    command = command.strip()
    return any(command in skill_commands.get(skill, ()) for skill in invoked)


def _gh_calls(gh_calls, offsets):
    """Give each shim call the turn n of the last message whose offset is <= the call's index."""
    result = []
    for index, call in enumerate(gh_calls):
        turn = max((n for n, off in enumerate(offsets) if off <= index), default=0)
        result.append(
            GhCall(
                turn,
                tuple(call["argv"]),
                bool(call["writes"]),
                call.get("matched"),
                call.get("query"),
            )
        )
    return tuple(result)


def normalize(
    events, harness_messages, gh_calls=(), gh_call_offsets=(), skill_commands=None, meta=None
):
    """Build a `Transcript`.

    `gh_calls`: the shim's `gh-calls.jsonl` entries. `gh_call_offsets[n]`: the number of lines in
    that log when the harness sent message n, which gives each call its turn.
    """
    skill_commands = SKILL_COMMANDS if skill_commands is None else skill_commands
    t = Transcript(gh_calls=_gh_calls(gh_calls, gh_call_offsets), meta=dict(meta or {}))
    t.user_messages = [_user_message(i, m) for i, m in enumerate(harness_messages)]
    calls = {}
    invoked = set()
    denied_ids = set()
    turn = 0
    seq = -1  # one counter for every text block and tool call, so blocks of one event order
    for event in events:
        kind = event.get("type")
        if kind == "result":
            t.results.append(event)
            denied_ids.update(d.get("tool_use_id") for d in event.get("permission_denials") or [])
            turn += 1
        elif kind == "assistant":
            sub = event.get("parent_tool_use_id") is not None
            for block in event.get("message", {}).get("content", []):
                if block.get("type") == "text":
                    seq += 1
                    t.texts.append(TextBlock(turn, seq, block["text"], sub))
                elif block.get("type") == "tool_use":
                    seq += 1
                    call = ToolCall(
                        block["id"],
                        block["name"],
                        block.get("input", {}),
                        turn,
                        seq,
                        in_subagent=sub,
                    )
                    command = call.input.get("command")
                    if call.name == "Bash" and isinstance(command, str):
                        call.by_skill = not sub and _is_skill_command(
                            command, invoked, skill_commands
                        )
                    if call.name == "Skill" and not sub:
                        invoked.add(call.input.get("skill"))
                    calls[call.id] = call
                    t.tool_calls.append(call)
        elif kind == "user":
            content = event.get("message", {}).get("content")
            for block in content if isinstance(content, list) else []:
                if block.get("type") == "tool_result" and block.get("tool_use_id") in calls:
                    call = calls[block["tool_use_id"]]
                    call.result = _content_text(block.get("content"))
                    call.is_error = bool(block.get("is_error"))
        elif kind == "system" and event.get("subtype") == "permission_denied":
            call = calls.get(event.get("tool_use_id"))
            if call:
                call.denied = True
    for call_id in denied_ids:  # the `result` event's list is a cross-check and a fallback
        if call_id in calls:
            calls[call_id].denied = True
    t.turns = turn
    return t
