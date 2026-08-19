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
