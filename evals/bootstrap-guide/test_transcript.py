import json
import unittest
from pathlib import Path

import transcript

DATA = Path(__file__).resolve().parent / "testdata" / "transcripts"
SKILL_COMMANDS = {"hello": ("git status --porcelain",)}


def load(stem):
    def read(name):
        text = (DATA / f"{stem}.{name}.jsonl").read_text(encoding="utf-8")
        return [json.loads(line) for line in text.splitlines() if line.strip()]

    return read("events"), read("messages")


def set_command(events, call_id, command):
    for event in events:
        if event.get("type") != "assistant":
            continue
        for block in event["message"]["content"]:
            if block.get("id") == call_id:
                block["input"]["command"] = command


class NormalizeSessionTest(unittest.TestCase):
    def setUp(self):
        events, messages = load("session")
        self.gh_calls = [
            {"argv": ["issue", "create", "--title", "x"], "matched": 0, "writes": True},
            {"argv": ["issue", "list"], "matched": None, "writes": False, "query": "open"},
        ]
        self.t = transcript.normalize(
            events,
            messages,
            self.gh_calls,
            [0, 0, 1, 2],
            skill_commands=SKILL_COMMANDS,
            meta={"fake_token": "PLACEHOLDER"},
        )

    def call(self, call_id):
        return next(c for c in self.t.tool_calls if c.id == call_id)

    def test_text_blocks_in_order_with_turn(self):
        self.assertEqual(
            [(b.turn, b.in_subagent) for b in self.t.texts],
            [(0, False), (1, True), (1, False), (2, False), (3, False)],
        )
        self.assertTrue(self.t.texts[0].text.startswith("Hello from the skill"))
        self.assertEqual(self.t.texts[3].text.split(".")[0], "Your shell shows Python 3")
        seqs = [b.seq for b in self.t.texts]
        self.assertEqual(seqs, sorted(seqs))

    def test_thinking_blocks_are_not_text(self):
        self.assertFalse(any(b.text == "" for b in self.t.texts))

    def test_tool_calls_have_input_and_result(self):
        bash = self.call("toolu_bash1")
        self.assertEqual(bash.name, "Bash")
        self.assertEqual(bash.input["command"], "git status --porcelain")
        self.assertEqual(bash.result, "?? .claude/")
        self.assertFalse(bash.is_error)
        self.assertEqual(bash.turn, 0)

    def test_list_content_result_is_joined_text(self):
        agent = self.call("toolu_agent1")
        self.assertTrue(agent.result.startswith("[Subagent hand-back]"))

    def test_denials_counted_apart(self):
        self.assertTrue(self.call("toolu_bash2").denied)
        self.assertTrue(self.call("toolu_bash3").denied)
        self.assertFalse(self.call("toolu_bash1").denied)
        self.assertEqual(self.t.denied_count, 2)
        self.assertEqual(self.t.denied_count_main, 1)

    def test_subagent_calls_marked_by_parent_tool_use_id(self):
        self.assertTrue(self.call("toolu_bash3").in_subagent)
        self.assertFalse(self.call("toolu_bash2").in_subagent)
        self.assertFalse(self.call("toolu_agent1").in_subagent)

    def test_skill_commands_marked(self):
        self.assertTrue(self.call("toolu_bash1").by_skill)
        self.assertFalse(self.call("toolu_bash2").by_skill)
        self.assertFalse(self.call("toolu_skill1").by_skill)

    def test_skill_command_before_skill_call_is_not_marked(self):
        events, messages = load("session")
        # Drop the Skill call and its results: the same command is then the agent's own.
        events = [e for e in events if "toolu_skill1" not in json.dumps(e)]
        t = transcript.normalize(events, messages, [], skill_commands=SKILL_COMMANDS)
        bash = next(c for c in t.tool_calls if c.id == "toolu_bash1")
        self.assertFalse(bash.by_skill)

    def test_user_messages_including_bang(self):
        kinds = [(m.turn, m.kind) for m in self.t.user_messages]
        self.assertEqual(kinds, [(0, "text"), (1, "text"), (2, "bang"), (3, "text")])
        self.assertEqual(self.t.user_messages[0].text, "Use the hello skill.")
        bang = self.t.user_messages[2]
        self.assertEqual(bang.bash_input, "python3 --version")
        self.assertEqual(bang.bash_stdout, "Python 3.11.9")
        self.assertEqual(bang.bash_stderr, "")

    def test_one_result_per_turn(self):
        self.assertEqual(self.t.turns, 4)
        self.assertEqual(len(self.t.results), 4)
        self.assertTrue(all(r["type"] == "result" for r in self.t.results))

    def test_gh_calls_carry_their_turn(self):
        first, second = self.t.gh_calls
        self.assertEqual((first.turn, second.turn), (1, 2))
        self.assertEqual(first.argv, ("issue", "create", "--title", "x"))
        self.assertEqual((first.matched, first.writes, first.query), (0, True, None))
        self.assertEqual((second.matched, second.writes, second.query), (None, False, "open"))
        with self.assertRaises(AttributeError):
            first.turn = 3

    def test_gh_writes_are_the_writing_calls(self):
        self.assertEqual([c.argv[:2] for c in self.t.gh_writes], [("issue", "create")])

    def test_meta_is_kept(self):
        self.assertEqual(self.t.meta, {"fake_token": "PLACEHOLDER"})

    def test_seq_is_unique_and_increasing_across_blocks(self):
        sub_text = self.t.texts[1]
        sub_call = self.call("toolu_bash3")
        self.assertEqual(sub_text.turn, sub_call.turn)
        self.assertLess(sub_text.seq, sub_call.seq)  # same event, text first
        seqs = sorted([b.seq for b in self.t.texts] + [c.seq for c in self.t.tool_calls])
        self.assertEqual(len(seqs), len(set(seqs)))

    def test_denial_read_from_result_event_when_system_event_is_missing(self):
        events, messages = load("session")
        events = [e for e in events if e.get("subtype") != "permission_denied"]
        t = transcript.normalize(events, messages, [], [], skill_commands=SKILL_COMMANDS)
        denied = {c.id for c in t.tool_calls if c.denied}
        self.assertEqual(denied, {"toolu_bash2", "toolu_bash3"})
        self.assertEqual(t.denied_count, 2)

    def test_skill_command_must_match_exactly(self):
        events, messages = load("session")
        set_command(events, "toolu_bash1", "git status --porcelain; git push")
        t = transcript.normalize(events, messages, [], [], skill_commands=SKILL_COMMANDS)
        bash = next(c for c in t.tool_calls if c.id == "toolu_bash1")
        self.assertFalse(bash.by_skill)

    def test_skill_command_surrounding_whitespace_is_ignored(self):
        events, messages = load("session")
        set_command(events, "toolu_bash1", " git status --porcelain\n")
        t = transcript.normalize(events, messages, [], [], skill_commands=SKILL_COMMANDS)
        bash = next(c for c in t.tool_calls if c.id == "toolu_bash1")
        self.assertTrue(bash.by_skill)

    def test_synthetic_skill_text_is_not_a_user_message(self):
        self.assertFalse(
            any("Base directory" in m.text for m in self.t.user_messages)
        )


class NormalizeOneTurnTest(unittest.TestCase):
    def test_text_only_turn(self):
        events, messages = load("one-turn")
        t = transcript.normalize(events, messages, [])
        self.assertEqual(t.tool_calls, [])
        self.assertEqual(t.turns, 1)
        self.assertEqual(len(t.texts), 1)
        self.assertEqual(t.denied_count, 0)
        self.assertEqual(list(t.gh_writes), [])

    def test_default_skill_commands_follow_research_d4(self):
        self.assertIn("speckit-constitution", transcript.SKILL_COMMANDS)
        self.assertIn("roadmap-write", transcript.SKILL_COMMANDS)
        self.assertIn(
            "python3 scripts/check_roadmap.py",
            transcript.SKILL_COMMANDS["roadmap-write"],
        )


if __name__ == "__main__":
    unittest.main()
