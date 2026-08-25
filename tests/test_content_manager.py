import base64
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPTS = Path(__file__).parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import content_manager
import migration_bundle
import repair_archive_incident_20260818


PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9Zl1sAAAAASUVORK5CYII="
)


class ContentManagerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.codex_home = self.root / ".codex"
        self.session = self.codex_home / "sessions" / "2026" / "08" / "13" / "rollout-test.jsonl"
        self.session.parent.mkdir(parents=True)
        self.task_id = "019ffa1a-2a9c-7092-8f34-c16d33906405"
        image_url = "data:image/png;base64," + base64.b64encode(PNG).decode("ascii")
        rows = [
            {"type": "session_meta", "payload": {"id": self.task_id, "timestamp": "2026-08-13T10:00:00Z"}},
            {
                "type": "response_item",
                "payload": {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "keep this text"},
                        {"type": "input_image", "image_url": image_url, "detail": "auto"},
                        {"type": "input_image", "image_url": image_url, "detail": "auto"},
                    ],
                },
            },
        ]
        self.session.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
        self.database = self.codex_home / "state_5.sqlite"
        connection = sqlite3.connect(self.database)
        connection.execute(
            "create table threads (id text primary key, rollout_path text not null, created_at integer not null, "
            "updated_at integer not null, source text not null, model_provider text not null, cwd text not null, "
            "title text not null, sandbox_policy text not null, approval_mode text not null, "
            "archived integer not null default 0, archived_at integer)"
        )
        self.project = self.root / "projects" / "demo"
        self.project.mkdir(parents=True)
        (self.project / "README.md").write_text("demo", encoding="utf-8")
        connection.execute(
            "insert into threads values (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                self.task_id,
                str(self.session),
                1,
                1786615200,
                "vscode",
                "openai",
                str(self.project),
                "Image task",
                "{}",
                "on-request",
                0,
                None,
            ),
        )
        connection.commit()
        connection.close()
        (self.codex_home / "session_index.jsonl").write_text(
            json.dumps({"id": self.task_id, "rollout_path": str(self.session)}) + "\n",
            encoding="utf-8",
        )
        self.catalog_database = self.codex_home / "sqlite" / "codex-dev.db"
        self.catalog_database.parent.mkdir(parents=True)
        connection = sqlite3.connect(self.catalog_database)
        connection.execute(
            "create table local_thread_catalog ("
            "host_id text not null, thread_id text not null, display_title text not null, "
            "cwd text not null, source_detail text, missing_candidate integer not null default 0, "
            "primary key (host_id, thread_id))"
        )
        connection.execute(
            "create table local_thread_catalog_metadata ("
            "id integer primary key, catalog_revision integer not null default 0)"
        )
        connection.execute("insert into local_thread_catalog_metadata values (1, 10)")
        connection.execute(
            "insert into local_thread_catalog values (?,?,?,?,?,0)",
            ("local", self.task_id, "Image task", str(self.project), str(self.session)),
        )
        connection.commit()
        connection.close()
        self.environment = mock.patch.dict(os.environ, {"LOCALAPPDATA": str(self.root / "local")})
        self.environment.start()

    def tearDown(self):
        self.environment.stop()
        self.temporary.cleanup()

    def test_scan_groups_projects_and_deduplicates_images(self):
        inventory = content_manager.scan_content(self.codex_home)

        self.assertEqual(inventory["summary"]["conversations"], 1)
        self.assertEqual(inventory["summary"]["projects"], 1)
        self.assertEqual(inventory["summary"]["unique_images"], 1)
        self.assertEqual(inventory["summary"]["image_occurrences"], 2)
        self.assertEqual(inventory["images"][0]["size_bytes"], len(PNG))
        self.assertEqual(inventory["images"][0]["stored_bytes"], len(PNG) * 2)
        self.assertEqual(inventory["images"][0]["kinds"], ["user_image"])
        self.assertEqual(inventory["images"][0]["risk_level"], "高")
        self.assertFalse(inventory["images"][0]["safe_to_clean"])
        self.assertEqual(inventory["summary"]["catalog_visible"], 1)
        self.assertEqual(inventory["summary"]["stale_catalog"], 0)
        self.assertEqual(inventory["summary"]["extended_rollout_paths"], 0)

    def test_scan_includes_registered_project_without_conversations(self):
        registered_path = r"C:\Users\ZZT\Documents\Codex\conversation-demo-project"
        (self.codex_home / content_manager.GLOBAL_STATE_FILE_NAME).write_text(
            json.dumps({
                "local-projects": {
                    "project-without-chat": {
                        "name": "conversation-demo-project",
                        "rootPaths": ["\\\\?\\" + registered_path],
                    },
                },
            }),
            encoding="utf-8",
        )

        inventory = content_manager.scan_content(self.codex_home)

        project = next(
            item for item in inventory["projects"]
            if item["path"] == registered_path
        )
        self.assertEqual(project["thread_count"], 0)
        self.assertTrue(project["registered"])
        self.assertEqual(project["project_name"], "conversation-demo-project")
        self.assertEqual(project["registration_ids"], ["project-without-chat"])

    def test_scan_uses_first_class_project_assignment_and_metadata(self):
        second_root = self.root / "projects" / "docs"
        second_root.mkdir(parents=True)
        connection = sqlite3.connect(self.database)
        connection.execute("alter table threads add column project_id text")
        connection.execute("alter table threads add column is_pinned integer not null default 0")
        connection.execute("alter table threads add column thread_section_id text")
        connection.execute("alter table threads add column section_position integer")
        connection.execute("alter table threads add column history_mode text not null default 'legacy'")
        connection.execute(
            "create table projects (id text primary key, name text not null, metadata text not null default '{}', "
            "position integer not null, created_at_ms integer not null, updated_at_ms integer not null)"
        )
        connection.execute(
            "create table project_roots (project_id text not null, position integer not null, "
            "path text not null, primary key(project_id, position))"
        )
        connection.execute(
            "insert into projects values (?,?,?,?,?,?)",
            ("native-project", "新版项目名称", "{}", 0, 1, 2),
        )
        connection.execute(
            "insert into project_roots values (?,?,?)",
            ("native-project", 0, str(self.project)),
        )
        connection.execute(
            "insert into project_roots values (?,?,?)",
            ("native-project", 1, str(second_root)),
        )
        connection.execute(
            "update threads set project_id='native-project', is_pinned=1, "
            "thread_section_id='section-1', section_position=3, history_mode='paginated' where id=?",
            (self.task_id,),
        )
        connection.commit()
        connection.close()

        inventory = content_manager.scan_content(self.codex_home)

        conversation = inventory["conversations"][0]
        self.assertEqual(conversation["project_id"], "native-project")
        self.assertEqual(conversation["project_name"], "新版项目名称")
        self.assertEqual(conversation["project_roots"], [str(self.project), str(second_root)])
        self.assertTrue(conversation["is_pinned"])
        self.assertEqual(conversation["thread_section_id"], "section-1")
        self.assertEqual(conversation["history_mode"], "paginated")
        project = inventory["projects"][0]
        self.assertEqual(project["native_project_ids"], ["native-project"])
        self.assertEqual(project["storage_sources"], ["state_db"])
        self.assertEqual(project["project_roots"], [str(self.project), str(second_root)])

    def test_path_health_detects_and_repairs_extended_drive_path(self):
        extended = "\\\\?\\" + str(self.session)
        connection = sqlite3.connect(self.database)
        connection.execute(
            "update threads set rollout_path=? where id=?", (extended, self.task_id)
        )
        connection.commit()
        connection.close()

        health = content_manager.inspect_rollout_path_health(self.codex_home)

        self.assertEqual(len(health["repairable_paths"]), 1)
        self.assertEqual(health["repairable_paths"][0]["task_id"], self.task_id)
        result = content_manager.repair_rollout_path_health(
            self.codex_home, require_codex_closed=False
        )
        self.assertEqual(result["repaired"], 1)
        self.assertTrue(Path(result["backup_path"]).is_dir())
        row = migration_bundle.read_sqlite_threads(self.codex_home, {self.task_id})[self.task_id]
        self.assertEqual(row["rollout_path"], str(self.session.resolve()))
        transaction = json.loads(
            (Path(result["backup_path"]) / "transaction.json").read_text(encoding="utf-8")
        )
        self.assertEqual(transaction["repairs"][0]["raw_path"], extended)

    def test_path_repair_removes_only_known_normalization_triggers(self):
        connection = sqlite3.connect(self.database)
        connection.execute(
            "create trigger threads_rollout_path_normalize_after_insert "
            "after insert on threads begin select 1; end"
        )
        connection.execute(
            "create trigger unrelated_trigger after update on threads begin select 1; end"
        )
        connection.commit()
        connection.close()

        health = content_manager.inspect_rollout_path_health(self.codex_home)
        self.assertEqual(
            health["normalization_triggers"],
            ["threads_rollout_path_normalize_after_insert"],
        )
        result = content_manager.repair_rollout_path_health(
            self.codex_home, require_codex_closed=False
        )

        self.assertEqual(result["triggers_removed"], ["threads_rollout_path_normalize_after_insert"])
        connection = sqlite3.connect(self.database)
        names = {
            row[0]
            for row in connection.execute("select name from sqlite_master where type='trigger'")
        }
        connection.close()
        self.assertNotIn("threads_rollout_path_normalize_after_insert", names)
        self.assertIn("unrelated_trigger", names)

    def test_path_health_reports_unsafe_extended_path_without_repairing_it(self):
        outside = self.root / "outside.jsonl"
        outside.write_text("{}\n", encoding="utf-8")
        extended = "\\\\?\\" + str(outside)
        connection = sqlite3.connect(self.database)
        connection.execute(
            "update threads set rollout_path=? where id=?", (extended, self.task_id)
        )
        connection.commit()
        connection.close()

        health = content_manager.inspect_rollout_path_health(self.codex_home)

        self.assertEqual(len(health["blocked_paths"]), 1)
        self.assertIn("不在当前 Codex 数据目录", health["blocked_paths"][0]["reason"])
        result = content_manager.repair_rollout_path_health(
            self.codex_home, require_codex_closed=False
        )
        self.assertEqual(result["repaired"], 0)
        self.assertEqual(result["blocked"], 1)
        self.assertIsNone(result["backup_path"])

    def test_extended_unc_path_is_normalized_without_losing_the_share_prefix(self):
        normalized, kind = content_manager._normalized_extended_rollout_path(
            r"\\?\UNC\server\share\.codex\sessions\rollout.jsonl"
        )

        self.assertEqual(normalized, r"\\server\share\.codex\sessions\rollout.jsonl")
        self.assertEqual(kind, "extended_unc")

    def test_path_health_detects_and_repairs_extended_project_registry_path(self):
        state_path = self.codex_home / ".codex-global-state.json"
        extended = "\\\\?\\" + str(self.project)
        state_path.write_text(
            json.dumps({
                "local-projects": {
                    "project-demo": {
                        "id": "project-demo",
                        "name": "demo",
                        "rootPaths": [extended],
                    },
                },
            }),
            encoding="utf-8",
        )

        health = content_manager.inspect_rollout_path_health(self.codex_home)

        self.assertEqual(len(health["project_extended_paths"]), 1)
        self.assertEqual(len(health["repairable_project_paths"]), 1)
        self.assertEqual(health["repairable_project_paths"][0]["project_id"], "project-demo")
        result = content_manager.repair_rollout_path_health(
            self.codex_home, require_codex_closed=False
        )
        self.assertEqual(result["project_paths_repaired"], 1)
        self.assertEqual(result["stale_projects_removed"], 0)
        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(
            state["local-projects"]["project-demo"]["rootPaths"],
            [str(self.project.resolve())],
        )
        self.assertTrue((Path(result["backup_path"]) / "files" / ".codex-global-state.json").is_file())

    def test_path_health_respects_per_project_ignore_choice(self):
        state_path = self.codex_home / ".codex-global-state.json"
        extended = "\\\\?\\" + str(self.project)
        original = {
            "local-projects": {
                "project-demo": {
                    "id": "project-demo", "name": "demo", "rootPaths": [extended],
                },
            },
        }
        state_path.write_text(json.dumps(original), encoding="utf-8")

        result = content_manager.repair_rollout_path_health(
            self.codex_home,
            require_codex_closed=False,
            selected_project_actions={"normalize:project-demo": "ignore"},
            repair_conversation_paths=False,
            remove_normalization_triggers=False,
        )

        self.assertIsNone(result["backup_path"])
        self.assertEqual(json.loads(state_path.read_text(encoding="utf-8")), original)

    def test_path_health_can_normalize_and_rename_a_registered_project(self):
        state_path = self.codex_home / ".codex-global-state.json"
        extended = "\\\\?\\" + str(self.project)
        state_path.write_text(
            json.dumps({
                "local-projects": {
                    "project-demo": {
                        "id": "project-demo", "name": "demo", "rootPaths": [extended],
                    },
                },
            }),
            encoding="utf-8",
        )

        result = content_manager.repair_rollout_path_health(
            self.codex_home,
            require_codex_closed=False,
            selected_project_actions={"normalize:project-demo": "normalize"},
            selected_project_names={"normalize:project-demo": "renamed demo"},
            repair_conversation_paths=False,
            remove_normalization_triggers=False,
        )

        self.assertEqual(result["project_paths_repaired"], 1)
        self.assertEqual(result["project_names_changed"], 1)
        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["local-projects"]["project-demo"]["name"], "renamed demo")
        self.assertEqual(
            state["local-projects"]["project-demo"]["rootPaths"],
            [str(self.project.resolve())],
        )

    def test_path_health_can_remove_registration_without_deleting_files_or_conversations(self):
        state_path = self.codex_home / ".codex-global-state.json"
        extended = "\\\\?\\" + str(self.project)
        state_path.write_text(
            json.dumps({
                "project-order": ["project-demo"],
                "thread-project-assignments": {
                    self.task_id: {"projectKind": "local", "projectId": "project-demo"},
                },
                "local-projects": {
                    "project-demo": {
                        "id": "project-demo", "name": "demo", "rootPaths": [extended],
                    },
                },
            }),
            encoding="utf-8",
        )

        result = content_manager.repair_rollout_path_health(
            self.codex_home,
            require_codex_closed=False,
            selected_project_actions={"normalize:project-demo": "remove"},
            repair_conversation_paths=False,
            remove_normalization_triggers=False,
        )

        self.assertEqual(result["project_registrations_removed"], 1)
        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["local-projects"], {})
        self.assertEqual(state["project-order"], [])
        self.assertEqual(state["thread-project-assignments"], {})
        self.assertTrue(self.project.is_dir())
        self.assertTrue(self.session.is_file())
        self.assertIn(
            self.task_id,
            migration_bundle.read_sqlite_threads(self.codex_home, {self.task_id}),
        )

    def test_path_health_removes_only_missing_project_without_linked_threads(self):
        state_path = self.codex_home / ".codex-global-state.json"
        missing = self.root / "projects" / "removed-test"
        existing_extended = "\\\\?\\" + str(self.project)
        missing_extended = "\\\\?\\" + str(missing)
        state_path.write_text(
            json.dumps({
                "project-order": ["project-removed", "project-demo"],
                "pinned-project-ids": "project-removed",
                "selected-project": {"type": "local", "projectId": "project-removed"},
                "project-files": {"project-removed": [{"path": "old.txt"}]},
                "local-projects": {
                    "project-demo": {
                        "id": "project-demo",
                        "name": "demo",
                        "rootPaths": [existing_extended],
                    },
                    "project-removed": {
                        "id": "project-removed",
                        "name": "removed-test",
                        "rootPaths": [missing_extended],
                    },
                },
            }),
            encoding="utf-8",
        )

        health = content_manager.inspect_rollout_path_health(self.codex_home)

        self.assertEqual(len(health["project_extended_paths"]), 2)
        self.assertEqual(
            [item["project_id"] for item in health["removable_projects"]],
            ["project-removed"],
        )
        result = content_manager.repair_rollout_path_health(
            self.codex_home, require_codex_closed=False
        )
        self.assertEqual(result["project_paths_repaired"], 1)
        self.assertEqual(result["stale_projects_removed"], 1)
        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertIn("project-demo", state["local-projects"])
        self.assertNotIn("project-removed", state["local-projects"])
        self.assertEqual(state["project-order"], ["project-demo"])
        self.assertNotIn("pinned-project-ids", state)
        self.assertNotIn("selected-project", state)
        self.assertNotIn("project-removed", state["project-files"])

    def test_path_health_merges_duplicate_project_and_all_known_references(self):
        state_path = self.codex_home / ".codex-global-state.json"
        normal = str(self.project.resolve())
        extended = "\\\\?\\" + normal
        state_path.write_text(
            json.dumps({
                "project-order": ["duplicate", "keeper"],
                "pinned-project-ids": "duplicate",
                "electron-saved-workspace-roots": ["duplicate", "keeper", normal],
                "selected-project": {"type": "local", "projectId": "duplicate"},
                "thread-project-assignments": {
                    self.task_id: {"projectKind": "local", "projectId": "duplicate"},
                },
                "project-appearances": {
                    "keeper": {"color": "green"},
                    "duplicate": {"color": "red"},
                },
                "project-files": {
                    "keeper": [{"path": "keep.txt"}],
                    "duplicate": [{"path": "old.txt"}],
                },
                "sidebar-project-thread-orders": {
                    "keeper": {"threadIds": ["a"], "sortKey": "001"},
                    "duplicate": {"threadIds": ["b", "a"], "sortKey": "002"},
                },
                "local-projects": {
                    "keeper": {
                        "id": "keeper", "name": "demo", "rootPaths": [normal],
                        "createdAt": 10, "updatedAt": 10,
                    },
                    "duplicate": {
                        "id": "duplicate", "name": "demo", "rootPaths": [extended],
                        "createdAt": 20, "updatedAt": 30,
                    },
                },
            }),
            encoding="utf-8",
        )

        health = content_manager.inspect_rollout_path_health(self.codex_home)

        self.assertEqual(len(health["duplicate_projects"]), 1)
        self.assertEqual(health["duplicate_projects"][0]["keeper_id"], "keeper")
        self.assertEqual(health["repairable_project_paths"], [])
        result = content_manager.repair_rollout_path_health(
            self.codex_home, require_codex_closed=False
        )
        self.assertEqual(result["duplicate_projects_merged"], 1)
        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(list(state["local-projects"]), ["keeper"])
        self.assertEqual(state["project-order"], ["keeper"])
        self.assertEqual(state["pinned-project-ids"], "keeper")
        self.assertEqual(state["electron-saved-workspace-roots"], ["keeper", normal])
        self.assertEqual(state["selected-project"]["projectId"], "keeper")
        self.assertEqual(
            state["thread-project-assignments"][self.task_id]["projectId"], "keeper"
        )
        self.assertEqual(state["project-appearances"], {"keeper": {"color": "green"}})
        self.assertEqual(
            state["project-files"]["keeper"],
            [{"path": "keep.txt"}, {"path": "old.txt"}],
        )
        self.assertEqual(
            state["sidebar-project-thread-orders"]["keeper"]["threadIds"], ["a", "b"]
        )
        self.assertEqual(state["local-projects"]["keeper"]["updatedAt"], 30)

    def test_path_health_can_choose_the_duplicate_keeper_and_final_name(self):
        state_path = self.codex_home / ".codex-global-state.json"
        normal = str(self.project.resolve())
        extended = "\\\\?\\" + normal
        state_path.write_text(
            json.dumps({
                "project-order": ["keeper", "duplicate"],
                "local-projects": {
                    "keeper": {"id": "keeper", "name": "normal", "rootPaths": [normal]},
                    "duplicate": {"id": "duplicate", "name": "extended", "rootPaths": [extended]},
                },
            }),
            encoding="utf-8",
        )

        result = content_manager.repair_rollout_path_health(
            self.codex_home,
            require_codex_closed=False,
            selected_project_actions={"duplicate:keeper": "merge:duplicate"},
            selected_project_names={"duplicate:keeper": "chosen project"},
            repair_conversation_paths=False,
            remove_normalization_triggers=False,
        )

        self.assertEqual(result["duplicate_projects_merged"], 1)
        self.assertEqual(result["project_names_changed"], 1)
        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(list(state["local-projects"]), ["duplicate"])
        self.assertEqual(state["local-projects"]["duplicate"]["name"], "chosen project")
        self.assertEqual(state["local-projects"]["duplicate"]["rootPaths"], [normal])
        self.assertEqual(state["project-order"], ["duplicate"])

    def test_duplicate_registration_capabilities_use_verified_path_evidence(self):
        state_path = self.codex_home / ".codex-global-state.json"
        normal = str(self.project.resolve())
        extended = "\\\\?\\" + normal
        state_path.write_text(
            json.dumps({
                "local-projects": {
                    "normal": {"id": "normal", "name": "demo", "rootPaths": [normal]},
                    "extended": {"id": "extended", "name": "demo", "rootPaths": [extended]},
                },
            }),
            encoding="utf-8",
        )

        health = content_manager.inspect_rollout_path_health(self.codex_home)
        rows = {
            item["project_id"]: item
            for item in health["actionable_project_registrations"]
        }

        self.assertEqual(rows["normal"]["status"], "普通路径")
        self.assertFalse(rows["normal"]["capabilities"]["normalize"]["enabled"])
        self.assertIn("已经是普通路径", rows["normal"]["capabilities"]["normalize"]["reason"])
        self.assertEqual(rows["extended"]["status"], "扩展路径异常")
        self.assertTrue(rows["extended"]["capabilities"]["normalize"]["enabled"])
        self.assertTrue(rows["extended"]["capabilities"]["delete"]["enabled"])
        self.assertFalse(rows["normal"]["capabilities"]["full_delete"]["enabled"])
        self.assertIn("共用", rows["normal"]["capabilities"]["full_delete"]["reason"])

    def test_registration_actions_delete_only_the_selected_duplicate_and_migrate_references(self):
        state_path = self.codex_home / ".codex-global-state.json"
        normal = str(self.project.resolve())
        extended = "\\\\?\\" + normal
        state_path.write_text(
            json.dumps({
                "project-order": ["extended", "normal"],
                "thread-project-assignments": {
                    self.task_id: {"projectKind": "local", "projectId": "extended"},
                },
                "local-projects": {
                    "normal": {"id": "normal", "name": "demo", "rootPaths": [normal]},
                    "extended": {"id": "extended", "name": "demo", "rootPaths": [extended]},
                },
            }),
            encoding="utf-8",
        )

        result = content_manager.repair_rollout_path_health(
            self.codex_home,
            require_codex_closed=False,
            selected_project_actions={
                "registration:normal": "keep",
                "registration:extended": "delete",
            },
            repair_conversation_paths=False,
            remove_normalization_triggers=False,
        )

        self.assertEqual(result["duplicate_projects_merged"], 1)
        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(list(state["local-projects"]), ["normal"])
        self.assertEqual(state["project-order"], ["normal"])
        self.assertEqual(
            state["thread-project-assignments"][self.task_id]["projectId"], "normal"
        )

    def test_missing_registration_can_be_repointed_and_renamed(self):
        state_path = self.codex_home / ".codex-global-state.json"
        missing = self.root / "projects" / "missing"
        replacement = self.root / "projects" / "replacement"
        replacement.mkdir()
        state_path.write_text(
            json.dumps({
                "local-projects": {
                    "stale": {
                        "id": "stale", "name": "old", "rootPaths": ["\\\\?\\" + str(missing)],
                    },
                },
            }),
            encoding="utf-8",
        )

        health = content_manager.inspect_rollout_path_health(self.codex_home)
        row = health["actionable_project_registrations"][0]
        self.assertFalse(row["capabilities"]["normalize"]["enabled"])
        self.assertTrue(row["capabilities"]["repoint"]["enabled"])
        self.assertTrue(row["capabilities"]["rename"]["enabled"])
        self.assertTrue(row["capabilities"]["delete"]["enabled"])
        result = content_manager.repair_rollout_path_health(
            self.codex_home,
            require_codex_closed=False,
            selected_project_actions={"registration:stale": "repoint"},
            selected_project_names={"registration:stale": "replacement"},
            selected_project_paths={"registration:stale": str(replacement)},
            repair_conversation_paths=False,
            remove_normalization_triggers=False,
        )

        self.assertEqual(result["project_paths_repointed"], 1)
        self.assertEqual(result["project_names_changed"], 1)
        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["local-projects"]["stale"]["name"], "replacement")
        self.assertEqual(
            state["local-projects"]["stale"]["rootPaths"], [str(replacement.resolve())]
        )

    def test_full_project_delete_removes_every_live_layer_and_keeps_recovery_artifacts(self):
        state_path = self.codex_home / ".codex-global-state.json"
        extended = "\\\\?\\" + str(self.project)
        state_path.write_text(
            json.dumps({
                "project-order": ["project-demo"],
                "thread-project-assignments": {
                    self.task_id: {"projectKind": "local", "projectId": "project-demo"},
                },
                "local-projects": {
                    "project-demo": {
                        "id": "project-demo", "name": "demo", "rootPaths": [extended],
                    },
                },
            }),
            encoding="utf-8",
        )
        health = content_manager.inspect_rollout_path_health(self.codex_home)
        row = health["actionable_project_registrations"][0]
        self.assertTrue(row["capabilities"]["full_delete"]["enabled"])

        result = content_manager.fully_delete_registered_project(
            self.codex_home, "project-demo", require_codex_closed=False
        )

        self.assertEqual(result["deleted_conversations"], 1)
        self.assertFalse(self.project.exists())
        self.assertFalse(self.session.exists())
        self.assertEqual(
            migration_bundle.read_sqlite_threads(self.codex_home, {self.task_id}), {}
        )
        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertNotIn("project-demo", state["local-projects"])
        self.assertTrue(Path(result["registration_backup"]).is_dir())
        self.assertTrue(Path(result["conversation_backup"]).is_dir())
        self.assertTrue(all(Path(item).is_dir() for item in result["trash_items"]))

        restored = content_manager.restore_project(
            Path(result["trash_items"][0]),
            require_codex_closed=False,
            codex_home=self.codex_home,
        )

        self.assertTrue(restored["full_project_restored"])
        self.assertTrue(self.project.is_dir())
        self.assertTrue(self.session.is_file())
        self.assertIn(
            self.task_id,
            migration_bundle.read_sqlite_threads(self.codex_home, {self.task_id}),
        )
        restored_state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertIn("project-demo", restored_state["local-projects"])

    def test_full_project_delete_accepts_an_ordinary_registration(self):
        state_path = self.codex_home / ".codex-global-state.json"
        state_path.write_text(
            json.dumps({
                "project-order": ["project-demo"],
                "thread-project-assignments": {
                    self.task_id: {"projectKind": "local", "projectId": "project-demo"},
                },
                "local-projects": {
                    "project-demo": {
                        "id": "project-demo", "name": "demo", "rootPaths": [str(self.project.resolve())],
                    },
                },
            }),
            encoding="utf-8",
        )

        health = content_manager.inspect_rollout_path_health(self.codex_home)
        self.assertEqual(health["actionable_project_registrations"], [])

        result = content_manager.fully_delete_registered_project(
            self.codex_home, "project-demo", require_codex_closed=False
        )

        self.assertEqual(result["deleted_conversations"], 1)
        self.assertFalse(self.project.exists())
        self.assertNotIn(
            "project-demo",
            json.loads(state_path.read_text(encoding="utf-8"))["local-projects"],
        )
        restored = content_manager.restore_project(
            Path(result["trash_items"][0]),
            require_codex_closed=False,
            codex_home=self.codex_home,
        )
        self.assertTrue(restored["registration_restored"])
        self.assertTrue(self.project.is_dir())
        self.assertTrue(self.session.is_file())

    def test_path_health_blocks_duplicate_project_with_unknown_reference(self):
        state_path = self.codex_home / ".codex-global-state.json"
        normal = str(self.project.resolve())
        extended = "\\\\?\\" + normal
        original = {
            "future-project-cache": {"selected": "duplicate"},
            "local-projects": {
                "keeper": {"id": "keeper", "name": "demo", "rootPaths": [normal]},
                "duplicate": {"id": "duplicate", "name": "demo", "rootPaths": [extended]},
            },
        }
        state_path.write_text(json.dumps(original), encoding="utf-8")

        health = content_manager.inspect_rollout_path_health(self.codex_home)

        self.assertEqual(health["duplicate_projects"], [])
        self.assertEqual(len(health["blocked_duplicate_projects"]), 1)
        blocked_rows = {
            item["project_id"]: item
            for item in health["actionable_project_registrations"]
        }
        self.assertFalse(blocked_rows["duplicate"]["capabilities"]["delete"]["enabled"])
        self.assertTrue(blocked_rows["keeper"]["capabilities"]["delete"]["enabled"])
        self.assertTrue(all(item["capabilities"]["repoint"]["enabled"] for item in blocked_rows.values()))
        result = content_manager.repair_rollout_path_health(
            self.codex_home, require_codex_closed=False
        )
        self.assertEqual(result["duplicate_projects_merged"], 0)
        self.assertEqual(result["project_paths_blocked"], 1)
        self.assertIsNone(result["backup_path"])
        self.assertEqual(json.loads(state_path.read_text(encoding="utf-8")), original)

    def test_path_health_does_not_remove_missing_project_with_task_assignment(self):
        state_path = self.codex_home / ".codex-global-state.json"
        missing = self.root / "projects" / "missing"
        project_id = "assigned-missing"
        state_path.write_text(
            json.dumps({
                "thread-project-assignments": {
                    self.task_id: {"projectKind": "local", "projectId": project_id},
                },
                "local-projects": {
                    project_id: {
                        "id": project_id,
                        "name": "missing",
                        "rootPaths": ["\\\\?\\" + str(missing)],
                    },
                },
            }),
            encoding="utf-8",
        )

        health = content_manager.inspect_rollout_path_health(self.codex_home)

        self.assertEqual(health["removable_projects"], [])
        self.assertEqual(len(health["blocked_project_paths"]), 1)
        self.assertIn("侧栏任务归属", health["blocked_project_paths"][0]["reason"])

    def _set_database_title(self, title):
        connection = sqlite3.connect(self.database)
        connection.execute("update threads set title=? where id=?", (title, self.task_id))
        connection.commit()
        connection.close()
        connection = sqlite3.connect(self.catalog_database)
        connection.execute(
            "update local_thread_catalog set display_title=? where thread_id=?", (title, self.task_id)
        )
        connection.commit()
        connection.close()

    def test_uuid_sidebar_name_is_preserved(self):
        self._set_database_title(self.task_id)

        conversation = content_manager.scan_content(self.codex_home)["conversations"][0]

        self.assertEqual(conversation["title"], self.task_id)
        self.assertEqual(conversation["title_source"], "Codex 侧栏名称")
        self.assertEqual(conversation["original_title"], self.task_id)

    def test_work_in_sidebar_name_is_preserved_without_rewriting_database(self):
        original = f"Work in {self.project}"
        self._set_database_title(original)

        conversation = content_manager.scan_content(self.codex_home)["conversations"][0]

        self.assertEqual(conversation["title"], original)
        connection = sqlite3.connect(self.database)
        stored = connection.execute("select title from threads where id=?", (self.task_id,)).fetchone()[0]
        connection.close()
        self.assertEqual(stored, original)

    def test_work_in_sidebar_name_keeps_exact_text(self):
        original = f"Work in {self.project}. Review the imported project before changing it."
        self._set_database_title(original)

        conversation = content_manager.scan_content(self.codex_home)["conversations"][0]

        self.assertEqual(conversation["title"], original)
        self.assertEqual(conversation["title_source"], "Codex 侧栏名称")

    def test_valid_chinese_title_is_preserved(self):
        self._set_database_title("保留这个正常标题")
        conversation = content_manager.scan_content(self.codex_home)["conversations"][0]
        self.assertEqual(conversation["title"], "保留这个正常标题")
        self.assertEqual(conversation["title_source"], "Codex 侧栏名称")

    def test_sidebar_catalog_title_wins_over_stale_database_title(self):
        self._set_database_title("对话就行")
        connection = sqlite3.connect(self.catalog_database)
        connection.execute(
            "update local_thread_catalog set display_title=? where thread_id=?",
            ("通信原理实验室", self.task_id),
        )
        connection.commit()
        connection.close()

        conversation = content_manager.scan_content(self.codex_home)["conversations"][0]

        self.assertEqual(conversation["title"], "通信原理实验室")
        self.assertEqual(conversation["original_title"], "对话就行")
        self.assertEqual(conversation["catalog_title"], "通信原理实验室")
        self.assertEqual(conversation["title_source"], "Codex 侧栏名称")

    def test_hyphenated_sidebar_name_is_not_replaced_by_database_title(self):
        self._set_database_title("设计一个在对话中生成项目的skill")
        connection = sqlite3.connect(self.catalog_database)
        connection.execute(
            "update local_thread_catalog set display_title=? where thread_id=?",
            ("conversational-project-builder", self.task_id),
        )
        connection.commit()
        connection.close()

        conversation = content_manager.scan_content(self.codex_home)["conversations"][0]

        self.assertEqual(conversation["title"], "conversational-project-builder")
        self.assertEqual(conversation["title_source"], "Codex 侧栏名称")

    def test_extended_windows_project_path_has_a_readable_name_and_display_path(self):
        extended = r"\\?\C:\Users\ZZT\Documents\Codex\skill-design-project"
        connection = sqlite3.connect(self.database)
        connection.execute("update threads set cwd=? where id=?", (extended, self.task_id))
        connection.commit()
        connection.close()

        conversation = content_manager.scan_content(self.codex_home)["conversations"][0]

        self.assertEqual(conversation["project_path"], r"C:\Users\ZZT\Documents\Codex\skill-design-project")
        self.assertEqual(conversation["project_name"], "skill-design-project")

    def test_codex_new_chat_workspace_is_not_classified_as_a_project(self):
        temporary_workspace = r"C:\Users\ZZT\Documents\Codex\2026-08-07\new-chat-2"
        connection = sqlite3.connect(self.database)
        connection.execute("update threads set cwd=? where id=?", (temporary_workspace, self.task_id))
        connection.commit()
        connection.close()

        inventory = content_manager.scan_content(self.codex_home)
        conversation = inventory["conversations"][0]

        self.assertEqual(conversation["project_path"], "")
        self.assertEqual(conversation["project_name"], "")
        self.assertEqual(conversation["cwd"], temporary_workspace)
        self.assertEqual(inventory["summary"]["projects"], 0)

    def test_reversible_mojibake_sidebar_name_is_repaired_for_display(self):
        self._set_database_title("ä½ å¥½")
        conversation = content_manager.scan_content(self.codex_home)["conversations"][0]
        self.assertEqual(conversation["title"], "你好")
        self.assertEqual(conversation["title_source"], "Codex 侧栏名称（可逆乱码修复）")

    def test_normal_accented_sidebar_name_is_not_flagged_or_changed(self):
        self._set_database_title("café")
        conversation = content_manager.scan_content(self.codex_home)["conversations"][0]
        self.assertEqual(conversation["title"], "café")
        self.assertEqual(conversation["title_source"], "Codex 侧栏名称")

    def test_irreversible_mojibake_sidebar_name_is_flagged_but_not_changed(self):
        self._set_database_title("标题�损坏")
        conversation = content_manager.scan_content(self.codex_home)["conversations"][0]
        self.assertEqual(conversation["title"], "标题�损坏")
        self.assertEqual(conversation["title_source"], "Codex 侧栏名称（疑似乱码，未改动）")

    def test_missing_catalog_title_falls_back_to_database_without_semantic_inference(self):
        connection = sqlite3.connect(self.catalog_database)
        connection.execute("delete from local_thread_catalog where thread_id=?", (self.task_id,))
        connection.commit()
        connection.close()
        connection = sqlite3.connect(self.database)
        connection.execute("update threads set title=? where id=?", ("document-production", self.task_id))
        connection.commit()
        connection.close()

        conversation = content_manager.scan_content(self.codex_home)["conversations"][0]

        self.assertEqual(conversation["title"], "document-production")
        self.assertEqual(conversation["title_source"], "主数据库标题")

    def test_first_request_is_not_promoted_to_a_formal_title(self):
        connection = sqlite3.connect(self.catalog_database)
        connection.execute("delete from local_thread_catalog where thread_id=?", (self.task_id,))
        connection.commit()
        connection.close()
        connection = sqlite3.connect(self.database)
        connection.execute("update threads set title='' where id=?", (self.task_id,))
        connection.commit()
        connection.close()

        conversation = content_manager.scan_content(self.codex_home)["conversations"][0]

        self.assertEqual(conversation["title"], self.task_id)
        self.assertEqual(conversation["title_source"], "任务 ID")

    def test_conversation_preview_is_read_only_and_redacts_large_payloads(self):
        before = self.session.read_bytes()
        assistant = {
            "type": "response_item",
            "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "useful answer"}]},
        }
        tool = {
            "type": "response_item",
            "payload": {"type": "function_call", "name": "example", "arguments": "secret tool payload"},
        }
        with self.session.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(tool) + "\n")
            stream.write(json.dumps(assistant) + "\n")
        expected = self.session.read_bytes()

        preview = content_manager.preview_conversation(self.session)

        rendered = json.dumps(preview, ensure_ascii=False)
        self.assertIn("keep this text", rendered)
        self.assertIn("useful answer", rendered)
        self.assertIn("[图片]", rendered)
        self.assertIn("[工具调用]", rendered)
        self.assertNotIn("data:image/", rendered)
        self.assertNotIn("secret tool payload", rendered)
        self.assertEqual(self.session.read_bytes(), expected)
        self.assertNotEqual(before, expected)

    def test_conversation_preview_truncates_very_long_message(self):
        long_text = "x" * 5000
        with self.session.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({
                "type": "response_item",
                "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": long_text}]},
            }) + "\n")

        preview = content_manager.preview_conversation(self.session, text_limit=120)

        assistant = next(item for item in reversed(preview["messages"]) if item["role"] == "assistant")
        self.assertEqual(len(assistant["text"]), 120)
        self.assertTrue(assistant["text"].endswith("…"))

    def test_browser_tool_screenshots_are_classified_for_bulk_selection(self):
        image_url = "data:image/png;base64," + base64.b64encode(PNG).decode("ascii")
        _mime, _data, kind = content_manager._node_image(
            {"tabId": "tab-1", "pageUrl": "https://example.test", "url": image_url}
        )
        self.assertEqual(kind, "browser_screenshot")
        browser_rollout = self.root / "browser.jsonl"
        browser_rollout.write_text(
            json.dumps({"payload": {"output": [{"tabId": "tab-1", "pageUrl": "https://example.test", "url": image_url}]}})
            + "\n"
            + json.dumps({"payload": {"output": [{"tabId": "tab-1", "pageUrl": "https://example.test", "url": image_url}]}})
            + "\n"
            + json.dumps({"payload": {"output": "later event"}})
            + "\n",
            encoding="utf-8",
        )
        image = content_manager.scan_rollout_images(browser_rollout)[0]
        self.assertEqual(image["kinds"], ["browser_screenshot"])
        self.assertTrue(image["has_later_events"])

    def test_image_cleanup_removes_all_selected_duplicates_and_backup_restores_them(self):
        inventory = content_manager.scan_content(self.codex_home)
        image = inventory["images"][0]
        result = content_manager.clean_images(
            self.codex_home,
            {self.task_id: {image["digest"]}},
            require_codex_closed=False,
        )

        self.assertEqual(result["removed_images"], 2)
        self.assertNotIn("data:image/", self.session.read_text(encoding="utf-8"))
        self.assertIn("keep this text", self.session.read_text(encoding="utf-8"))
        migration_bundle.restore_backup(Path(result["backup_path"]), self.codex_home, require_codex_closed=False)
        self.assertEqual(self.session.read_text(encoding="utf-8").count("data:image/"), 2)

    def test_image_cleanup_can_keep_one_copy_of_a_duplicate(self):
        inventory = content_manager.scan_content(self.codex_home)
        image = inventory["images"][0]
        result = content_manager.clean_images(
            self.codex_home,
            {self.task_id: {image["digest"]}},
            require_codex_closed=False,
            keep_one=True,
        )

        self.assertEqual(result["removed_images"], 1)
        self.assertEqual(self.session.read_text(encoding="utf-8").count("data:image/"), 1)

    def test_conversation_delete_is_fully_restorable(self):
        result = content_manager.delete_conversations(
            self.codex_home,
            {self.task_id},
            require_codex_closed=False,
        )

        self.assertFalse(self.session.exists())
        self.assertEqual(migration_bundle.read_sqlite_threads(self.codex_home, {self.task_id}), {})
        self.assertEqual(content_manager._catalog_contains(self.catalog_database, {self.task_id}), set())
        migration_bundle.restore_backup(Path(result["backup_path"]), self.codex_home, require_codex_closed=False)
        self.assertTrue(self.session.is_file())
        self.assertIn(self.task_id, migration_bundle.read_sqlite_threads(self.codex_home, {self.task_id}))
        self.assertEqual(content_manager._catalog_contains(self.catalog_database, {self.task_id}), {self.task_id})

    def test_selective_conversation_restore_does_not_require_full_snapshot_rollback(self):
        result = content_manager.delete_conversations(
            self.codex_home,
            {self.task_id},
            require_codex_closed=False,
        )
        records = migration_bundle.backup_conversation_records(
            Path(result["backup_path"]), self.codex_home
        )
        self.assertEqual([record["task_id"] for record in records], [self.task_id])
        self.assertGreater(records[0]["updated_at"], 0)
        self.assertTrue(Path(records[0]["backup_rollout_path"]).is_file())
        restored = migration_bundle.restore_conversation_from_backup(
            Path(result["backup_path"]),
            self.codex_home,
            self.task_id,
            require_codex_closed=False,
        )
        self.assertEqual(restored["task_id"], self.task_id)
        self.assertTrue(self.session.is_file())
        self.assertIn(self.task_id, migration_bundle.read_sqlite_threads(self.codex_home, {self.task_id}))
        self.assertEqual(content_manager._catalog_contains(self.catalog_database, {self.task_id}), {self.task_id})

    def test_scan_reports_and_cleanup_removes_stale_sidebar_and_index_entries(self):
        stale_id = "019ffa50-ba0d-72a3-853e-fb9de2640ac0"
        connection = sqlite3.connect(self.catalog_database)
        connection.execute(
            "insert into local_thread_catalog values (?,?,?,?,?,0)",
            ("local", stale_id, "Stale task", str(self.project), str(self.codex_home / "missing.jsonl")),
        )
        connection.commit()
        connection.close()
        with (self.codex_home / "session_index.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"id": stale_id, "rollout_path": str(self.codex_home / "missing.jsonl")}) + "\n")

        inventory = content_manager.scan_content(self.codex_home)

        self.assertEqual(inventory["summary"]["conversations"], 1)
        self.assertEqual(inventory["summary"]["catalog_visible"], 2)
        self.assertEqual(inventory["summary"]["stale_catalog"], 1)
        self.assertEqual(inventory["consistency"]["stale_catalog_ids"], [stale_id])
        self.assertEqual(inventory["consistency"]["index_only_ids"], [stale_id])

        result = content_manager.clean_stale_sidebar_entries(
            self.codex_home, {stale_id}, require_codex_closed=False
        )

        self.assertNotIn(stale_id, content_manager._catalog_contains(self.catalog_database, {stale_id}))
        self.assertNotIn(stale_id, migration_bundle.read_session_index(self.codex_home))
        migration_bundle.restore_backup(Path(result["backup_path"]), self.codex_home, require_codex_closed=False)
        self.assertIn(stale_id, content_manager._catalog_contains(self.catalog_database, {stale_id}))
        self.assertIn(stale_id, migration_bundle.read_session_index(self.codex_home))

    def test_cleanup_removes_archived_catalog_residue_without_removing_compatibility_index(self):
        connection = sqlite3.connect(self.database)
        connection.execute("update threads set archived=1,archived_at=123 where id=?", (self.task_id,))
        connection.commit()
        connection.close()

        inventory = content_manager.scan_content(self.codex_home)

        self.assertEqual(inventory["consistency"]["stale_catalog_ids"], [self.task_id])
        self.assertTrue(inventory["consistency"]["stale_catalog"][0]["archived"])
        content_manager.clean_stale_sidebar_entries(
            self.codex_home, {self.task_id}, require_codex_closed=False
        )
        self.assertNotIn(self.task_id, content_manager._catalog_contains(self.catalog_database, {self.task_id}))
        self.assertIn(self.task_id, migration_bundle.read_session_index(self.codex_home))

    def test_partial_archive_state_is_reported_and_repaired(self):
        connection = sqlite3.connect(self.database)
        connection.execute("update threads set archived=1,archived_at=123 where id=?", (self.task_id,))
        connection.commit()
        connection.close()

        conversation = content_manager.scan_content(self.codex_home)["conversations"][0]

        self.assertTrue(conversation["archived"])
        self.assertFalse(conversation["archive_consistent"])
        self.assertIn("文件仍在使用区", conversation["archive_state"])

        result = content_manager.set_conversations_archived(
            self.codex_home, {self.task_id}, archived=True, require_codex_closed=False
        )

        archived_path = self.codex_home / "archived_sessions" / self.session.name
        self.assertEqual(result["changed"], 1)
        self.assertTrue(archived_path.is_file())
        self.assertFalse(self.session.exists())
        self.assertNotIn(
            self.task_id,
            content_manager._catalog_contains(self.catalog_database, {self.task_id}),
        )
        row = migration_bundle.read_sqlite_threads(self.codex_home, {self.task_id})[self.task_id]
        self.assertEqual(row["archived"], 1)
        self.assertIsNotNone(row["archived_at"])
        self.assertEqual(Path(row["rollout_path"]), archived_path)

    def test_active_thread_with_stale_archived_at_is_reported_and_repaired(self):
        connection = sqlite3.connect(self.database)
        connection.execute("update threads set archived=0,archived_at=123 where id=?", (self.task_id,))
        connection.commit()
        connection.close()

        conversation = content_manager.scan_content(self.codex_home)["conversations"][0]

        self.assertFalse(conversation["archive_consistent"])
        self.assertIn("仍保留归档时间", conversation["archive_state"])

        result = content_manager.set_conversations_archived(
            self.codex_home, {self.task_id}, archived=False, require_codex_closed=False
        )

        self.assertEqual(result["changed"], 1)
        row = migration_bundle.read_sqlite_threads(self.codex_home, {self.task_id})[self.task_id]
        self.assertEqual(row["archived"], 0)
        self.assertIsNone(row["archived_at"])
        self.assertEqual(Path(row["rollout_path"]), self.session)

    def test_incident_repair_targets_only_the_confirmed_thread(self):
        connection = sqlite3.connect(self.database)
        connection.execute("update threads set archived=1,archived_at=123 where id=?", (self.task_id,))
        connection.commit()
        connection.close()

        with (
            mock.patch.object(repair_archive_incident_20260818, "TASK_ID", self.task_id),
            mock.patch.object(
                repair_archive_incident_20260818,
                "EXPECTED_ROLLOUT_NAME",
                self.session.name,
            ),
        ):
            report = repair_archive_incident_20260818.repair(
                self.codex_home, require_codex_closed=False
            )

        self.assertEqual(report["verification"]["task_id"], self.task_id)
        self.assertEqual(report["verification"]["archived"], 1)
        self.assertFalse(report["verification"]["catalog_present"])
        self.assertEqual(report["verification"]["state_integrity"], "ok")

    def test_removing_missing_candidate_does_not_increment_visible_catalog_revision(self):
        connection = sqlite3.connect(self.catalog_database)
        connection.execute(
            "update local_thread_catalog set missing_candidate=1 where thread_id=?", (self.task_id,)
        )
        connection.commit()
        connection.close()

        deleted = content_manager._remove_catalog_rows(self.catalog_database, {self.task_id})

        connection = sqlite3.connect(self.catalog_database)
        revision = connection.execute(
            "select catalog_revision from local_thread_catalog_metadata where id=1"
        ).fetchone()[0]
        connection.close()
        self.assertEqual(deleted, 1)
        self.assertEqual(revision, 10)

    def test_conversation_archive_and_unarchive_update_all_registered_layers(self):
        archived = content_manager.set_conversations_archived(
            self.codex_home, {self.task_id}, archived=True, require_codex_closed=False
        )
        archived_path = self.codex_home / "archived_sessions" / self.session.name

        self.assertFalse(self.session.exists())
        self.assertTrue(archived_path.is_file())
        archived_row = migration_bundle.read_sqlite_threads(self.codex_home, {self.task_id})[self.task_id]
        self.assertEqual(archived_row["archived"], 1)
        self.assertIsNotNone(archived_row["archived_at"])
        self.assertEqual(Path(archived_row["rollout_path"]), archived_path)
        self.assertEqual(
            Path(migration_bundle.read_session_index(self.codex_home)[self.task_id]["rollout_path"]), archived_path
        )
        connection = sqlite3.connect(self.catalog_database)
        catalog_row = connection.execute(
            "select source_detail, missing_candidate from local_thread_catalog where thread_id=?",
            (self.task_id,),
        ).fetchone()
        archived_revision = connection.execute(
            "select catalog_revision from local_thread_catalog_metadata where id=1"
        ).fetchone()[0]
        connection.close()
        self.assertIsNone(catalog_row)
        self.assertEqual(archived_revision, 11)
        archived_inventory = content_manager.scan_content(self.codex_home)
        self.assertEqual(archived_inventory["consistency"]["state_only_ids"], [])
        self.assertEqual(archived_inventory["consistency"]["stale_catalog_ids"], [])
        self.assertTrue(Path(archived["backup_path"]).is_dir())

        restored = content_manager.set_conversations_archived(
            self.codex_home, {self.task_id}, archived=False, require_codex_closed=False
        )

        self.assertTrue(self.session.is_file())
        self.assertFalse(archived_path.exists())
        restored_row = migration_bundle.read_sqlite_threads(self.codex_home, {self.task_id})[self.task_id]
        self.assertEqual(restored_row["archived"], 0)
        self.assertIsNone(restored_row["archived_at"])
        self.assertEqual(Path(restored_row["rollout_path"]), self.session)
        connection = sqlite3.connect(self.catalog_database)
        restored_catalog = connection.execute(
            "select source_detail, missing_candidate from local_thread_catalog where thread_id=?",
            (self.task_id,),
        ).fetchone()
        restored_revision = connection.execute(
            "select catalog_revision from local_thread_catalog_metadata where id=1"
        ).fetchone()[0]
        connection.close()
        self.assertIsNone(restored_catalog)
        self.assertEqual(restored_revision, 11)
        self.assertTrue(restored["catalog_rebuild_required"])
        restored_inventory = content_manager.scan_content(self.codex_home)
        self.assertEqual(restored_inventory["consistency"]["state_only_ids"], [self.task_id])
        self.assertTrue(Path(restored["backup_path"]).is_dir())

    def test_archive_backup_restores_original_active_state(self):
        result = content_manager.set_conversations_archived(
            self.codex_home, {self.task_id}, archived=True, require_codex_closed=False
        )
        archived_path = self.codex_home / "archived_sessions" / self.session.name

        migration_bundle.restore_backup(Path(result["backup_path"]), self.codex_home, require_codex_closed=False)

        self.assertTrue(self.session.is_file())
        self.assertFalse(archived_path.exists())
        row = migration_bundle.read_sqlite_threads(self.codex_home, {self.task_id})[self.task_id]
        self.assertEqual(row["archived"], 0)
        self.assertIsNone(row["archived_at"])
        self.assertEqual(Path(row["rollout_path"]), self.session)

    def test_project_trash_is_recoverable(self):
        result = content_manager.move_projects_to_trash([self.project], require_codex_closed=False)

        self.assertFalse(self.project.exists())
        items = content_manager.list_project_trash()
        self.assertEqual(len(items), 1)
        restored = content_manager.restore_project(Path(items[0]["item_root"]), require_codex_closed=False)
        self.assertEqual(Path(restored["restored_path"]), self.project)
        self.assertEqual((self.project / "README.md").read_text(encoding="utf-8"), "demo")


if __name__ == "__main__":
    unittest.main()
