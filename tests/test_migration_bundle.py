import importlib.util
import json
import sqlite3
import sys
import tempfile
import unittest
import uuid
from pathlib import Path


SCRIPTS = Path(__file__).parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import migration_bundle
import session_merge_planner as planner


def event(event_type, payload):
    return json.dumps({"type": event_type, "payload": payload}, sort_keys=True) + "\n"


class MigrationBundleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.left = self.root / "left"
        self.right = self.root / "right"
        (self.left / "sessions").mkdir(parents=True)
        (self.right / "sessions").mkdir(parents=True)
        self.make_db(self.left)
        self.make_db(self.right)

    def tearDown(self):
        self.temp.cleanup()

    def make_db(self, home):
        connection = sqlite3.connect(home / "state_5.sqlite")
        connection.execute(
            "create table threads ("
            "id text primary key, rollout_path text not null, created_at integer not null, "
            "updated_at integer not null, source text not null, model_provider text not null, "
            "cwd text not null, title text not null, sandbox_policy text not null, "
            "approval_mode text not null, tokens_used integer not null default 0, "
            "has_user_event integer not null default 0, archived integer not null default 0, "
            "cli_version text not null default '', first_user_message text not null default '', "
            "memory_mode text not null default 'enabled', preview text not null default '', "
            "recency_at integer not null default 0, recency_at_ms integer not null default 0, "
            "history_mode text not null default 'legacy', is_pinned integer not null default 0)"
        )
        connection.commit()
        connection.close()

    def write_session(self, home, task_id, tail):
        rows = [
            event("session_meta", {"id": task_id, "thread_name": "Shared task", "cwd": str(home)}),
            event("message", {"role": "user", "content": "base"}),
            event("message", {"role": "assistant", "content": tail}),
        ]
        path = home / "sessions" / f"{task_id}.jsonl"
        path.write_text("".join(rows), encoding="utf-8")
        index = {"id": task_id, "thread_name": "Shared task", "rollout_path": str(path)}
        (home / "session_index.jsonl").write_text(json.dumps(index) + "\n", encoding="utf-8")
        connection = sqlite3.connect(home / "state_5.sqlite")
        connection.execute(
            "insert into threads (id, rollout_path, created_at, updated_at, source, model_provider, cwd, title, sandbox_policy, approval_mode, has_user_event) "
            "values (?, ?, 1, 1, 'vscode', 'openai', ?, 'Shared task', '{}', 'on-request', 1)",
            (task_id, str(path), str(home)),
        )
        connection.commit()
        connection.close()

    def create_plan_and_bundle(self, task_id, direction="left-to-right"):
        left_inventory = planner.inventory(self.left, "left")
        right_inventory = planner.inventory(self.right, "right")
        plan = planner.compare_inventories(left_inventory, right_inventory, direction, set(), set())
        left_path = self.root / "left.json"
        plan_path = self.root / "plan.json"
        planner.write_json(left_path, left_inventory)
        planner.write_json(plan_path, plan)
        bundle = self.root / "bundle.zip"
        migration_bundle.create_bundle(left_path, plan_path, "left", bundle)
        return plan, bundle

    def test_imports_missing_session_and_updates_indexes(self):
        task_id = str(uuid.uuid4())
        self.write_session(self.left, task_id, "left result")
        _, bundle = self.create_plan_and_bundle(task_id)

        report = migration_bundle.restore_bundle(bundle, self.right, require_codex_closed=False)

        self.assertEqual(report["imported"], 1)
        inventory = planner.inventory(self.right, "right")
        self.assertIn(task_id, {item["task_id"] for item in inventory["conversations"]})
        self.assertIn(task_id, migration_bundle.read_session_index(self.right))
        self.assertIn(task_id, migration_bundle.read_sqlite_threads(self.right, {task_id}))
        self.assertTrue(Path(report["backup_path"]).is_dir())

    def test_divergence_imports_source_as_branch(self):
        task_id = str(uuid.uuid4())
        self.write_session(self.left, task_id, "left result")
        self.write_session(self.right, task_id, "right result")
        plan, bundle = self.create_plan_and_bundle(task_id)
        entry = next(item for item in plan["entries"] if item["task_id"] == task_id)
        branch_id = entry["proposed_left_branch_id"]

        report = migration_bundle.restore_bundle(bundle, self.right, require_codex_closed=False)

        self.assertEqual(report["imported"], 1)
        ids = {item["task_id"] for item in planner.inventory(self.right, "right")["conversations"]}
        self.assertEqual(ids, {task_id, branch_id})
        self.assertIn(branch_id, migration_bundle.read_session_index(self.right))
        self.assertIn(branch_id, migration_bundle.read_sqlite_threads(self.right, {branch_id}))

    def test_prepare_restore_is_read_only_for_a_missing_target(self):
        task_id = str(uuid.uuid4())
        self.write_session(self.left, task_id, "left result")
        _, bundle = self.create_plan_and_bundle(task_id)
        missing_target = self.root / "not-created-by-preview"

        prepared = migration_bundle.prepare_restore(bundle, missing_target)

        self.assertFalse(missing_target.exists())
        self.assertEqual(prepared["operations"][0]["action"], "import")

    def test_export_and_restore_report_progress(self):
        task_id = str(uuid.uuid4())
        self.write_session(self.left, task_id, "left result")
        left_inventory = planner.inventory(self.left, "left")
        right_inventory = planner.inventory(self.right, "right")
        plan = planner.compare_inventories(left_inventory, right_inventory, "left-to-right", set(), set())
        left_path = self.root / "left-progress.json"
        plan_path = self.root / "plan-progress.json"
        planner.write_json(left_path, left_inventory)
        planner.write_json(plan_path, plan)
        events = []
        bundle = self.root / "progress.zip"
        migration_bundle.create_bundle(
            left_path, plan_path, "left", bundle,
            progress_callback=lambda stage, detail: events.append((stage, detail)),
        )
        migration_bundle.restore_bundle(
            bundle, self.right, require_codex_closed=False,
            progress_callback=lambda stage, detail: events.append((stage, detail)),
        )
        stages = {stage for stage, _detail in events}
        self.assertTrue({"scan", "package", "validate", "backup", "write", "verify"}.issubset(stages))

    def test_committed_backup_can_restore_and_restore_can_be_undone(self):
        task_id = str(uuid.uuid4())
        self.write_session(self.left, task_id, "left result")
        _, bundle = self.create_plan_and_bundle(task_id)
        import_report = migration_bundle.restore_bundle(bundle, self.right, require_codex_closed=False)

        backups = migration_bundle.list_backups(self.right)
        self.assertEqual(len(backups), 1)
        self.assertTrue(backups[0]["restorable"])
        self.assertEqual(backups[0]["operation"], "sync")

        restore_report = migration_bundle.restore_backup(
            Path(import_report["backup_path"]), self.right, require_codex_closed=False
        )
        self.assertNotIn(task_id, {item["task_id"] for item in planner.inventory(self.right, "right")["conversations"]})
        self.assertNotIn(task_id, migration_bundle.read_session_index(self.right))
        self.assertNotIn(task_id, migration_bundle.read_sqlite_threads(self.right, {task_id}))

        migration_bundle.restore_backup(
            Path(restore_report["safety_backup_path"]), self.right, require_codex_closed=False
        )
        self.assertIn(task_id, {item["task_id"] for item in planner.inventory(self.right, "right")["conversations"]})
        self.assertIn(task_id, migration_bundle.read_session_index(self.right))
        self.assertIn(task_id, migration_bundle.read_sqlite_threads(self.right, {task_id}))


if __name__ == "__main__":
    unittest.main()
