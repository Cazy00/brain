"""Tests for the one operation in this repo that can destroy something.

`brain connect --apply` edits config files this system did not create, does not
own, and cannot regenerate. Everything else here writes only inside the brain,
where git is the undo. There is no git in ~/.cursor.

So the four rules get one test each, per format: merge never overwrite, back up
before writing, refuse anything it cannot recognise, and do nothing at all when
the answer is already correct. Nothing in this file touches a real config —
every path is inside a temp directory.
"""
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

sys.path.insert(0, str(ROOT / "bin"))
from brainlib import configedit  # noqa: E402

ENTRY = {"command": "/repo/bin/brain-mcp", "args": []}


class ConfigEditCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def backups(self):
        return sorted(p.name for p in self.base.iterdir()
                      if ".brain-backup-" in p.name)


class TestJsonMerge(ConfigEditCase):
    def test_a_missing_file_is_created_with_just_our_entry(self):
        path = self.base / "mcp.json"
        outcome = configedit.apply_json(path, "mcpServers", "brain", ENTRY)
        self.assertEqual(outcome.action, "created")
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")),
                         {"mcpServers": {"brain": ENTRY}})
        # Nothing existed, so nothing was worth backing up. A directory full of
        # backups of files that were never there is how people learn to ignore
        # the backups that matter.
        self.assertEqual(self.backups(), [])

    def test_other_servers_and_other_top_level_keys_survive(self):
        path = self.base / "mcp.json"
        path.write_text(json.dumps({
            "mcpServers": {"other": {"command": "/usr/local/bin/other", "args": []}},
            "theme": "dark",
        }, indent=2), encoding="utf-8")

        configedit.apply_json(path, "mcpServers", "brain", ENTRY)

        parsed = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(parsed["mcpServers"]["other"]["command"],
                         "/usr/local/bin/other")
        self.assertEqual(parsed["mcpServers"]["brain"], ENTRY)
        self.assertEqual(parsed["theme"], "dark",
                         "an unrelated top-level key was destroyed")

    def test_the_original_is_backed_up_before_it_is_touched(self):
        path = self.base / "mcp.json"
        before = json.dumps({"mcpServers": {"other": {"command": "/x", "args": []}}})
        path.write_text(before, encoding="utf-8")

        outcome = configedit.apply_json(path, "mcpServers", "brain", ENTRY)

        self.assertTrue(outcome.backup, "no backup path was reported")
        self.assertEqual(Path(outcome.backup).read_text(encoding="utf-8"), before,
                         "the backup is not the file we replaced")

    def test_applying_twice_writes_nothing_the_second_time(self):
        path = self.base / "mcp.json"
        configedit.apply_json(path, "mcpServers", "brain", ENTRY)
        first = path.read_text(encoding="utf-8")

        outcome = configedit.apply_json(path, "mcpServers", "brain", ENTRY)

        self.assertEqual(outcome.action, "unchanged")
        self.assertEqual(path.read_text(encoding="utf-8"), first)
        self.assertEqual(self.backups(), [],
                         "an unchanged file was backed up anyway")

    def test_a_changed_command_is_an_update_not_a_second_entry(self):
        # The moving-brain case: the same client, the same key, a new path.
        path = self.base / "mcp.json"
        configedit.apply_json(path, "mcpServers", "brain",
                              {"command": "/old/bin/brain-mcp", "args": []})
        outcome = configedit.apply_json(path, "mcpServers", "brain", ENTRY)
        self.assertEqual(outcome.action, "updated")
        parsed = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(parsed["mcpServers"]["brain"], ENTRY)

    def test_json_with_comments_is_refused_with_the_snippet(self):
        # VS Code and Cursor both accept JSONC. json.loads does not, and a
        # rewrite would silently delete every comment in the user's file.
        path = self.base / "mcp.json"
        original = '{\n  // my servers\n  "servers": {}\n}\n'
        path.write_text(original, encoding="utf-8")

        outcome = configedit.apply_json(path, "servers", "brain", ENTRY)

        self.assertEqual(outcome.action, "refused")
        self.assertIn("brain", outcome.snippet, "a refusal must still be actionable")
        self.assertEqual(path.read_text(encoding="utf-8"), original,
                         "a refused edit modified the file")
        self.assertEqual(self.backups(), [])

    def test_a_container_holding_the_wrong_type_is_refused(self):
        path = self.base / "mcp.json"
        path.write_text('{"mcpServers": ["not", "an", "object"]}', encoding="utf-8")
        outcome = configedit.apply_json(path, "mcpServers", "brain", ENTRY)
        self.assertEqual(outcome.action, "refused")

    def test_a_top_level_array_is_refused(self):
        path = self.base / "mcp.json"
        path.write_text("[]", encoding="utf-8")
        self.assertEqual(
            configedit.apply_json(path, "mcpServers", "brain", ENTRY).action,
            "refused")

    @unittest.skipIf(os.name == "nt", "POSIX file modes only")
    def test_the_files_permissions_survive_the_rewrite(self):
        # Written through a temp file and os.replace, so a half-written config
        # is impossible — but mkstemp creates 0600, and inheriting that would
        # quietly change who can read a config that was deliberately shared.
        path = self.base / "mcp.json"
        path.write_text('{"mcpServers": {}}', encoding="utf-8")
        path.chmod(0o644)
        configedit.apply_json(path, "mcpServers", "brain", ENTRY)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o644)


class TestTomlMerge(ConfigEditCase):
    def test_a_missing_file_is_created_with_the_table(self):
        path = self.base / "config.toml"
        outcome = configedit.apply_toml(path, "mcp_servers.brain", ENTRY)
        self.assertEqual(outcome.action, "created")
        text = path.read_text(encoding="utf-8")
        self.assertIn("[mcp_servers.brain]", text)
        self.assertIn('command = "/repo/bin/brain-mcp"', text)
        self.assertIn("args = []", text)

    def test_every_other_table_survives(self):
        path = self.base / "config.toml"
        path.write_text('model = "o3"\n\n[mcp_servers.other]\ncommand = "/x"\n',
                        encoding="utf-8")

        configedit.apply_toml(path, "mcp_servers.brain", ENTRY)

        text = path.read_text(encoding="utf-8")
        self.assertIn('model = "o3"', text)
        self.assertIn("[mcp_servers.other]", text)
        self.assertIn("[mcp_servers.brain]", text)

    def test_an_existing_brain_table_is_replaced_not_duplicated(self):
        path = self.base / "config.toml"
        path.write_text('[mcp_servers.brain]\ncommand = "/old/brain-mcp"\nargs = []\n'
                        '\n[mcp_servers.other]\ncommand = "/x"\n', encoding="utf-8")

        outcome = configedit.apply_toml(path, "mcp_servers.brain", ENTRY)

        text = path.read_text(encoding="utf-8")
        self.assertEqual(outcome.action, "updated")
        self.assertEqual(text.count("[mcp_servers.brain]"), 1,
                         "the table was duplicated instead of replaced")
        self.assertNotIn("/old/brain-mcp", text)
        self.assertIn("[mcp_servers.other]", text, "a sibling table was eaten")

    def test_applying_twice_writes_nothing_the_second_time(self):
        path = self.base / "config.toml"
        configedit.apply_toml(path, "mcp_servers.brain", ENTRY)
        first = path.read_text(encoding="utf-8")
        outcome = configedit.apply_toml(path, "mcp_servers.brain", ENTRY)
        self.assertEqual(outcome.action, "unchanged")
        self.assertEqual(path.read_text(encoding="utf-8"), first)

    def test_a_multiline_string_is_refused_rather_than_guessed_at(self):
        # There is no TOML parser below Python 3.11, so this is text surgery,
        # and text surgery cannot see that a `[table]` line inside a multi-line
        # string is not a table. Refusing is the only honest answer.
        path = self.base / "config.toml"
        original = 'notes = """\n[mcp_servers.brain]\nnot really a table\n"""\n'
        path.write_text(original, encoding="utf-8")

        outcome = configedit.apply_toml(path, "mcp_servers.brain", ENTRY)

        self.assertEqual(outcome.action, "refused")
        self.assertIn("[mcp_servers.brain]", outcome.snippet)
        self.assertEqual(path.read_text(encoding="utf-8"), original)

    def test_the_original_is_backed_up(self):
        path = self.base / "config.toml"
        before = 'model = "o3"\n'
        path.write_text(before, encoding="utf-8")
        outcome = configedit.apply_toml(path, "mcp_servers.brain", ENTRY)
        self.assertEqual(Path(outcome.backup).read_text(encoding="utf-8"), before)


class TestRoutingMarkers(ConfigEditCase):
    BLOCK = "# brain\nuse the brain tools.\n"

    def test_a_missing_file_is_created_with_the_preamble_first(self):
        path = self.base / "brain.instructions.md"
        outcome = configedit.apply_markers(path, self.BLOCK,
                                           preamble='---\napplyTo: "**"\n---\n')
        self.assertEqual(outcome.action, "created")
        text = path.read_text(encoding="utf-8")
        self.assertTrue(text.startswith('---\napplyTo: "**"\n---\n'),
                        "frontmatter must come first or the file matches nothing")
        self.assertIn(configedit.MARKER_START, text)
        self.assertIn(configedit.MARKER_END, text)

    def test_content_above_and_below_is_preserved(self):
        path = self.base / "CLAUDE.md"
        path.write_text("# my rules\nalways use tabs\n", encoding="utf-8")

        configedit.apply_markers(path, self.BLOCK)

        text = path.read_text(encoding="utf-8")
        self.assertIn("always use tabs", text,
                      "the user's own instructions were destroyed")
        self.assertIn(self.BLOCK.strip(), text)

    def test_a_second_apply_replaces_the_block_in_place(self):
        # The whole reason markers exist. The block changes as this system
        # changes, and before markers there was no way to update it that did
        # not involve a human finding it by eye.
        path = self.base / "CLAUDE.md"
        path.write_text("keep me\n", encoding="utf-8")
        configedit.apply_markers(path, self.BLOCK)
        outcome = configedit.apply_markers(path, "# brain\nsomething new.\n")

        text = path.read_text(encoding="utf-8")
        self.assertEqual(outcome.action, "updated")
        self.assertEqual(text.count(configedit.MARKER_START), 1,
                         "a second block was appended instead of replacing")
        self.assertIn("something new.", text)
        self.assertNotIn("use the brain tools.", text)
        self.assertIn("keep me", text)

    def test_an_unchanged_block_writes_nothing(self):
        path = self.base / "CLAUDE.md"
        configedit.apply_markers(path, self.BLOCK)
        first = path.read_text(encoding="utf-8")
        outcome = configedit.apply_markers(path, self.BLOCK)
        self.assertEqual(outcome.action, "unchanged")
        self.assertEqual(path.read_text(encoding="utf-8"), first)
        self.assertEqual(self.backups(), [])

    def test_half_a_block_is_refused(self):
        # A start marker with no end means somebody edited this by hand and
        # left it broken. Guessing where the block ends risks deleting their
        # text; refusing costs them one manual fix.
        path = self.base / "CLAUDE.md"
        original = f"{configedit.MARKER_START}\nstuff\nmore of my own rules\n"
        path.write_text(original, encoding="utf-8")

        outcome = configedit.apply_markers(path, self.BLOCK)

        self.assertEqual(outcome.action, "refused")
        self.assertEqual(path.read_text(encoding="utf-8"), original)

    def test_removal_takes_the_block_and_leaves_everything_else(self):
        path = self.base / "CLAUDE.md"
        path.write_text("above\n", encoding="utf-8")
        configedit.apply_markers(path, self.BLOCK)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write("below\n")

        outcome = configedit.remove_markers(path)

        text = path.read_text(encoding="utf-8")
        self.assertEqual(outcome.action, "updated")
        self.assertIn("above", text)
        self.assertIn("below", text)
        self.assertNotIn(configedit.MARKER_START, text)
        self.assertNotIn("use the brain tools.", text)

    def test_removing_from_a_file_with_no_block_changes_nothing(self):
        path = self.base / "CLAUDE.md"
        path.write_text("my own rules\n", encoding="utf-8")
        outcome = configedit.remove_markers(path)
        self.assertEqual(outcome.action, "unchanged")
        self.assertEqual(path.read_text(encoding="utf-8"), "my own rules\n")

    def test_removing_from_a_file_that_does_not_exist_is_not_an_error(self):
        outcome = configedit.remove_markers(self.base / "nope.md")
        self.assertEqual(outcome.action, "unchanged")


class TestOutcomeContract(unittest.TestCase):
    def test_an_unknown_action_is_a_bug(self):
        with self.assertRaises(ValueError):
            configedit.Outcome("wrote-it", "detail")

    def test_a_refusal_without_a_snippet_is_a_dead_end(self):
        # Refusing is always allowed. Refusing while leaving the user with no
        # way forward is not: the snippet IS the fallback, and it is what this
        # command did for every client before --apply existed.
        with self.assertRaises(ValueError):
            configedit.Outcome("refused", "cannot parse it")


if __name__ == "__main__":
    unittest.main()
