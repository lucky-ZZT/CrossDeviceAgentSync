import json
import sqlite3
import sys
import tempfile
import unittest
import uuid
from pathlib import Path


SCRIPTS = Path(__file__).parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import computer_transfer
import migration_bundle


class ComputerTransferTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source_home = self.root / "source-codex"
        self.target_home = self.root / "target-codex"
        self.source_project = self.root / "old" / "blog"
        self.projects_root = self.root / "new-projects"
        self.source_project.mkdir(parents=True)
        (self.source_project / "README.md").write_text("old blog", encoding="utf-8")
        (self.source_home / "sessions").mkdir(parents=True)
        (self.target_home / "sessions").mkdir(parents=True)
        self._make_db(self.source_home)
        self._make_db(self.target_home)
        self.task_id = str(uuid.uuid4())
        rollout = self.source_home / "sessions" / f"{self.task_id}.jsonl"
        rollout.write_text("\n".join((
            json.dumps({"type": "session_meta", "payload": {
                "id": self.task_id, "thread_name": "Blog task", "cwd": str(self.source_project),
            }}),
            json.dumps({"type": "message", "payload": {"role": "user", "content": "continue blog"}}),
        )) + "\n", encoding="utf-8")
        connection = sqlite3.connect(self.source_home / "state_5.sqlite")
        connection.execute(
            "insert into threads (id, rollout_path, created_at, updated_at, source, model_provider, cwd, title, sandbox_policy, approval_mode, has_user_event) "
            "values (?, ?, 1, 1, 'vscode', 'openai', ?, 'Blog task', '{}', 'on-request', 1)",
            (self.task_id, str(rollout), str(self.source_project)),
        )
        connection.commit()
        connection.close()
        self.bundle = self.root / "combined.cdas.zip"

    def tearDown(self):
        self.temporary.cleanup()

    @staticmethod
    def _make_db(home):
        connection = sqlite3.connect(home / "state_5.sqlite")
        connection.execute(
            "create table threads (id text primary key, rollout_path text not null, created_at integer not null, "
            "updated_at integer not null, source text not null, model_provider text not null, cwd text not null, "
            "title text not null, sandbox_policy text not null, approval_mode text not null, "
            "tokens_used integer not null default 0, has_user_event integer not null default 0, "
            "archived integer not null default 0, cli_version text not null default '', "
            "first_user_message text not null default '', memory_mode text not null default 'enabled', "
            "preview text not null default '', recency_at integer not null default 0, "
            "recency_at_ms integer not null default 0, history_mode text not null default 'legacy', "
            "is_pinned integer not null default 0)"
        )
        connection.commit()
        connection.close()

    def test_combined_import_restores_files_conversation_mapping_and_registration(self):
        created = computer_transfer.create_computer_bundle(
            self.source_home, self.source_project, self.bundle
        )
        self.assertTrue(created["has_project_files"])
        self.assertEqual(created["conversation_count"], 1)

        preview = computer_transfer.prepare_computer_import(
            self.bundle, self.target_home, self.projects_root
        )
        self.assertEqual(preview["recommended_action"], "create_project")
        self.assertEqual(len(preview["conversation_operations"]), 1)
        result = computer_transfer.restore_computer_bundle(
            self.bundle,
            self.target_home,
            self.projects_root,
            Path(preview["target_root"]),
            import_project_files=True,
            selected_task_ids={self.task_id},
            expected_state_sha256=preview["registration"]["global_state_sha256"],
            require_codex_closed=False,
        )

        target_project = Path(result["project_path"])
        self.assertEqual((target_project / "README.md").read_text(encoding="utf-8"), "old blog")
        row = migration_bundle.read_sqlite_threads(self.target_home, {self.task_id})[self.task_id]
        self.assertEqual(row["cwd"], str(target_project))
        state = json.loads(
            (self.target_home / ".codex-global-state.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            state["local-projects"][result["project_id"]]["rootPaths"], [str(target_project)]
        )
        self.assertEqual(
            state["thread-project-assignments"][self.task_id]["projectId"], result["project_id"]
        )


if __name__ == "__main__":
    unittest.main()
