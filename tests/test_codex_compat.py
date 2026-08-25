import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPTS = Path(__file__).parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import codex_compat


class CodexCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.codex_home = Path(self.temporary.name) / ".codex"
        self.codex_home.mkdir()

    def tearDown(self):
        self.temporary.cleanup()

    def _create_state(self, version=50):
        database = self.codex_home / "state_5.sqlite"
        connection = sqlite3.connect(database)
        connection.execute("create table _sqlx_migrations (version integer, success integer)")
        connection.execute("insert into _sqlx_migrations values (?,1)", (version,))
        connection.execute(
            "create table threads (id text primary key, rollout_path text not null, "
            "cwd text not null, archived integer not null default 0, project_id text, "
            "is_pinned integer not null default 0, thread_section_id text)"
        )
        connection.execute(
            "create table projects (id text primary key, name text not null, metadata text not null, "
            "position integer not null, created_at_ms integer not null, updated_at_ms integer not null)"
        )
        connection.execute(
            "create table project_roots (project_id text not null, position integer not null, "
            "path text not null, primary key(project_id, position))"
        )
        connection.execute(
            "create table thread_sections (id text primary key, name text not null)"
        )
        connection.commit()
        connection.close()
        return database

    def test_detects_transitioning_project_storage_and_paginated_history(self):
        self._create_state()
        (self.codex_home / ".codex-global-state.json").write_text(
            json.dumps({"local-projects": {"legacy-project": {"rootPaths": [r"C:\projects\demo"]}}}),
            encoding="utf-8",
        )
        history = sqlite3.connect(self.codex_home / "thread_history_1.sqlite")
        history.execute("create table _sqlx_migrations (version integer, success integer)")
        history.execute("insert into _sqlx_migrations values (4,1)")
        history.execute("create table thread_turns (thread_id text)")
        history.execute("create table thread_items (thread_id text)")
        history.commit()
        history.close()

        profile = codex_compat.inspect_codex_storage(self.codex_home)

        self.assertTrue(profile["write_compatible"])
        self.assertEqual(profile["state_schema_version"], 50)
        self.assertEqual(profile["history_schema_version"], 4)
        self.assertEqual(profile["project_storage_mode"], "transitioning")
        self.assertTrue(profile["features"]["project_id"])
        self.assertTrue(profile["features"]["thread_sections"])

    def test_unknown_new_state_schema_forces_read_only_mode(self):
        self._create_state(version=codex_compat.KNOWN_STATE_SCHEMA_MAX + 1)

        profile = codex_compat.inspect_codex_storage(self.codex_home)

        self.assertFalse(profile["write_compatible"])
        self.assertEqual(profile["status"], "read_only")
        with self.assertRaisesRegex(RuntimeError, "只读|阻止"):
            codex_compat.require_write_compatible(self.codex_home, "删除对话")

    def test_reads_first_class_projects_with_ordered_multiple_roots(self):
        database = self._create_state()
        connection = sqlite3.connect(database)
        connection.execute(
            "insert into projects values (?,?,?,?,?,?)",
            ("project-1", "multi-root", '{"source":"test"}', 2, 10, 20),
        )
        connection.execute(
            "insert into project_roots values (?,?,?)",
            ("project-1", 1, r"C:\projects\docs"),
        )
        connection.execute(
            "insert into project_roots values (?,?,?)",
            ("project-1", 0, r"C:\projects\app"),
        )
        connection.commit()
        connection.close()

        projects = codex_compat.read_native_projects(self.codex_home)

        self.assertEqual(len(projects), 1)
        self.assertEqual(projects[0]["primary_root"], r"C:\projects\app")
        self.assertEqual(
            projects[0]["roots"],
            [r"C:\projects\app", r"C:\projects\docs"],
        )


if __name__ == "__main__":
    unittest.main()
