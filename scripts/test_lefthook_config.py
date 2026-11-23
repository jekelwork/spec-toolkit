"""Guards lefthook.yml settings that keep the Git hooks from failing open."""

import json
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LEFTHOOK_YML = ROOT / "lefthook.yml"
LEFTHOOK_SCHEMA = ROOT / "node_modules" / "lefthook" / "schema.json"


def schema_keys(path):
    """Top-level configuration keys the installed lefthook's schema.json knows.

    Only the root object's properties count (followed through a root `$ref` into `$defs`), so a key
    of the same name nested under a command or job cannot make the check pass.
    """
    schema = json.loads(path.read_text(encoding="utf-8"))
    root = schema
    ref = schema.get("$ref", "")
    if ref.startswith("#/$defs/"):
        root = schema.get("$defs", {}).get(ref.removeprefix("#/$defs/"), {})
    return set(schema.get("properties", {})) | set(root.get("properties", {}))


class LefthookConfigTest(unittest.TestCase):
    def setUp(self):
        self.text = LEFTHOOK_YML.read_text(encoding="utf-8")

    def test_hooks_fail_when_lefthook_is_missing(self):
        # Without this setting the generated hook prints "Can't find lefthook in PATH" and exits 0,
        # so a commit goes through without the secret scan (for example in a worktree with no
        # node_modules). With it, the hook exits 1 and the commit is refused.
        self.assertRegex(self.text, r"(?m)^assert_lefthook_installed: true[ \t]*(#.*)?$")

    @unittest.skipUnless(LEFTHOOK_SCHEMA.exists(), "lefthook is not installed (run npm ci)")
    def test_installed_lefthook_knows_the_setting(self):
        # An older lefthook ignores unknown keys silently, so the hooks would fail open again.
        self.assertIn("assert_lefthook_installed", schema_keys(LEFTHOOK_SCHEMA))

    def test_schema_keys_reads_only_top_level_keys(self):
        # The guard above must be able to fail: a key that exists only on a nested definition does
        # not count, while a top-level one (directly or through the root $ref) does.
        key = {"assert_lefthook_installed": {}}
        nested_only = {"properties": {"colors": {}}, "$defs": {"Command": {"properties": key}}}
        through_ref = {"$ref": "#/$defs/Config", "$defs": {"Config": {"properties": key}}}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "schema.json"
            path.write_text(json.dumps(nested_only))
            self.assertEqual(schema_keys(path), {"colors"})
            path.write_text(json.dumps(through_ref))
            self.assertIn("assert_lefthook_installed", schema_keys(path))


if __name__ == "__main__":
    unittest.main()
