import json
import sqlite3
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock


SCRIPTS = Path(__file__).parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import local_provider_sync
import migration_bundle
import session_merge_planner as planner


class LocalProviderSyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name) / ".codex"
        (self.home / "sessions").mkdir(parents=True)
        (self.home / "config.toml").write_text(
            'model_provider = "openai"\n[model_providers.custom]\nname = "Custom"\n', encoding="utf-8"
        )
        self.task_id = str(uuid.uuid4())
        self.session = self.home / "sessions" / f"{self.task_id}.jsonl"
        self.session.write_text(
            json.dumps({"type": "session_meta", "payload": {"id": self.task_id, "thread_name": "Provider chat", "model_provider": "openai"}}) + "\n",
            encoding="utf-8",
        )
        (self.home / "session_index.jsonl").write_text(
            json.dumps({"id": self.task_id, "thread_name": "Provider chat", "rollout_path": str(self.session)}) + "\n",
            encoding="utf-8",
        )
        connection = sqlite3.connect(self.home / "state_5.sqlite")
        connection.execute(
            "create table threads (id text primary key, rollout_path text not null, created_at integer not null, updated_at integer not null, "
            "source text not null, model_provider text not null, cwd text not null, title text not null, sandbox_policy text not null, "
            "approval_mode text not null, tokens_used integer not null default 0, has_user_event integer not null default 0, archived integer not null default 0, "
            "cli_version text not null default '', first_user_message text not null default '', memory_mode text not null default 'enabled', "
            "preview text not null default '', recency_at integer not null default 0, recency_at_ms integer not null default 0, history_mode text not null default 'legacy', "
            "is_pinned integer not null default 0, agent_nickname text, agent_path text)"
        )
        connection.execute(
            "insert into threads (id,rollout_path,created_at,updated_at,source,model_provider,cwd,title,sandbox_policy,approval_mode,has_user_event) "
            "values (?,?,1,1,'vscode','openai',?,'Provider chat','{}','on-request',1)",
            (self.task_id, str(self.session), str(self.home)),
        )
        connection.commit()
        connection.close()

    def tearDown(self):
        self.temp.cleanup()

    def test_discovers_config_rollout_and_sqlite_providers(self):
        providers = {item["id"]: item for item in local_provider_sync.discover_providers(self.home)}
        self.assertEqual(set(providers), {"openai", "custom"})
        self.assertTrue(providers["openai"]["current"])
        self.assertEqual(providers["openai"]["sqlite_count"], 1)
        self.assertTrue(providers["custom"]["configured"])

    def test_windows_codex_process_detection_is_case_insensitive(self):
        completed = mock.Mock(stdout='"codex.exe","11444","Console","1","100 K"\n')
        with mock.patch.object(migration_bundle.os, "name", "nt"), mock.patch.object(
            migration_bundle.subprocess, "run", return_value=completed
        ):
            self.assertTrue(migration_bundle.codex_is_running())

    def test_reassign_accepts_windows_extended_length_rollout_path(self):
        extended_path = "\\\\?\\" + str(self.session)
        connection = sqlite3.connect(self.home / "state_5.sqlite")
        connection.execute(
            "update threads set rollout_path=? where id=?",
            (extended_path, self.task_id),
        )
        connection.commit()
        connection.close()

        listed = local_provider_sync.list_provider_threads(self.home, "openai")
        self.assertEqual([thread["id"] for thread in listed], [self.task_id])
        report = local_provider_sync.reassign_provider(
            self.home,
            "openai",
            "custom",
            {self.task_id},
            require_codex_closed=False,
            create_backup=False,
        )

        self.assertEqual(report["reassigned"], 1)
        first_line = json.loads(self.session.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(first_line["payload"]["model_provider"], "custom")

    def test_preflight_aggregates_all_problems_without_writing(self):
        missing_id = str(uuid.uuid4())
        original_session = self.session.read_bytes()
        with mock.patch.object(migration_bundle, "codex_is_running", return_value=True):
            report = local_provider_sync.preflight_provider_operation(
                self.home,
                "openai",
                "custom",
                {self.task_id, missing_id},
                operation="reassign",
                create_backup=True,
                require_codex_closed=True,
            )

        codes = {problem["code"] for problem in report["problems"]}
        self.assertFalse(report["ok"])
        self.assertIn("codex_running", codes)
        self.assertIn("missing_sqlite_row", codes)
        self.assertEqual(self.session.read_bytes(), original_session)
        self.assertFalse(migration_bundle.backup_root_for(self.home).exists())

    def test_preflight_detects_duplicate_rollout_paths(self):
        duplicate_id = str(uuid.uuid4())
        connection = sqlite3.connect(self.home / "state_5.sqlite")
        connection.execute(
            "insert into threads (id,rollout_path,created_at,updated_at,source,model_provider,cwd,title,sandbox_policy,approval_mode,has_user_event) "
            "values (?,?,1,1,'vscode','openai',?,'Duplicate','{}','on-request',1)",
            (duplicate_id, str(self.session), str(self.home)),
        )
        connection.commit()
        connection.close()

        report = local_provider_sync.preflight_provider_operation(
            self.home,
            "openai",
            "custom",
            {self.task_id, duplicate_id},
            operation="reassign",
            create_backup=True,
            require_codex_closed=False,
        )

        self.assertIn("duplicate_rollout_path", {problem["code"] for problem in report["problems"]})

    def test_preflight_blocks_insufficient_disk_space(self):
        disk_usage = mock.Mock(total=100, used=99, free=1)
        with mock.patch.object(local_provider_sync.shutil, "disk_usage", return_value=disk_usage):
            report = local_provider_sync.preflight_provider_operation(
                self.home,
                "openai",
                "custom",
                {self.task_id},
                operation="reassign",
                create_backup=True,
                require_codex_closed=False,
            )

        self.assertIn("insufficient_space", {problem["code"] for problem in report["problems"]})

    def test_path_normalization_supports_extended_drive_unc_and_relative_paths(self):
        regular = str(self.session)
        self.assertEqual(
            migration_bundle.resolve_local_path("\\\\?\\" + regular),
            self.session.resolve(),
        )
        relative = self.session.relative_to(self.home)
        self.assertEqual(
            migration_bundle.resolve_local_path(str(relative), self.home),
            self.session.resolve(),
        )
        self.assertEqual(
            migration_bundle.normalize_windows_path("\\\\?\\UNC\\server\\share\\file.jsonl"),
            "\\\\server\\share\\file.jsonl",
        )

    def test_discovers_providers_when_config_has_utf8_bom(self):
        (self.home / "config.toml").write_text(
            'model_provider = "openai"\n[model_providers.custom]\nname = "Custom"\n',
            encoding="utf-8-sig",
        )

        providers = {item["id"]: item for item in local_provider_sync.discover_providers(self.home)}

        self.assertEqual(set(providers), {"openai", "custom"})
        self.assertTrue(providers["openai"]["current"])
        self.assertTrue(providers["custom"]["configured"])

    def test_clones_selected_thread_to_target_provider(self):
        source_size = self.session.stat().st_size
        with mock.patch.object(
            local_provider_sync.planner,
            "inventory",
            side_effect=AssertionError("Provider clone must not build a full inventory"),
        ):
            report = local_provider_sync.clone_to_provider(
                self.home, "openai", "custom", {self.task_id}, require_codex_closed=False
            )
        self.assertEqual(report["imported"], 1)
        self.assertEqual(report["scanned_conversations"], 1)
        self.assertEqual(report["scanned_bytes"], source_size)
        connection = sqlite3.connect(self.home / "state_5.sqlite")
        rows = connection.execute("select id,model_provider,rollout_path from threads").fetchall()
        connection.close()
        providers = {row[1] for row in rows}
        self.assertEqual(providers, {"openai", "custom"})
        inventories = planner.inventory(self.home, "test")["conversations"]
        self.assertEqual(len(inventories), 2)
        self.assertIn("custom", {provider for item in inventories for provider in item["providers"]})
        target_id = report["operations"][0]["target_task_id"]
        target_path = Path(next(row[2] for row in rows if row[0] == target_id))
        target_data = target_path.read_text(encoding="utf-8")
        self.assertIn(target_id, target_data)
        self.assertNotIn(self.task_id, target_data)

        migration_bundle.restore_backup(
            Path(report["backup_path"]), self.home, require_codex_closed=False
        )
        self.assertFalse(target_path.exists())
        connection = sqlite3.connect(self.home / "state_5.sqlite")
        restored_ids = {row[0] for row in connection.execute("select id from threads")}
        connection.close()
        self.assertEqual(restored_ids, {self.task_id})

    def test_streams_only_selected_session_and_detects_encrypted_content(self):
        unselected_id = str(uuid.uuid4())
        unselected = self.home / "sessions" / f"{unselected_id}.jsonl"
        unselected.write_bytes(b"unselected" * (local_provider_sync.STREAM_CHUNK_BYTES + 1))
        connection = sqlite3.connect(self.home / "state_5.sqlite")
        connection.execute(
            "insert into threads (id,rollout_path,created_at,updated_at,source,model_provider,cwd,title,sandbox_policy,approval_mode,has_user_event) "
            "values (?,?,1,1,'vscode','openai',?,'Unselected','{}','on-request',1)",
            (unselected_id, str(unselected), str(self.home)),
        )
        connection.commit()
        connection.close()
        with self.session.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({
                "type": "response_item",
                "payload": {"conversation_id": self.task_id, "encrypted_content": "opaque"},
            }) + "\n")
        selected_size = self.session.stat().st_size

        original_stream_clone = local_provider_sync._stream_clone_session
        scanned_paths = []
        def track_stream_clone(source, *args, **kwargs):
            scanned_paths.append(source)
            return original_stream_clone(source, *args, **kwargs)

        with mock.patch.object(local_provider_sync, "_stream_clone_session", side_effect=track_stream_clone):
            report = local_provider_sync.clone_to_provider(
                self.home, "openai", "custom", {self.task_id}, require_codex_closed=False
            )

        self.assertEqual(scanned_paths, [self.session.resolve()])
        self.assertEqual(report["scanned_bytes"], selected_size)
        self.assertEqual(report["encrypted_content_warnings"], 1)

    def test_streaming_clone_rolls_back_files_index_and_database_on_failure(self):
        original_index = (self.home / "session_index.jsonl").read_bytes()
        with mock.patch.object(
            local_provider_sync.migration_bundle,
            "merge_sqlite",
            side_effect=RuntimeError("injected database failure"),
        ):
            with self.assertRaisesRegex(RuntimeError, "injected database failure"):
                local_provider_sync.clone_to_provider(
                    self.home, "openai", "custom", {self.task_id}, require_codex_closed=False
                )

        self.assertEqual((self.home / "session_index.jsonl").read_bytes(), original_index)
        session_files = list((self.home / "sessions").glob("*.jsonl"))
        self.assertEqual(session_files, [self.session])
        connection = sqlite3.connect(self.home / "state_5.sqlite")
        rows = connection.execute("select id,model_provider from threads").fetchall()
        connection.close()
        self.assertEqual(rows, [(self.task_id, "openai")])

    def test_clone_rejects_a_rollout_that_changes_while_streaming(self):
        original_session = self.session.read_bytes()
        staged = Path(self.temp.name) / "staged.jsonl"

        def append_source(_file_descriptor):
            with self.session.open("ab") as stream:
                stream.write(b"live append\n")

        with mock.patch.object(local_provider_sync.os, "fsync", side_effect=append_source):
            with self.assertRaisesRegex(ValueError, "changed while it was being copied"):
                local_provider_sync._stream_clone_session(
                    self.session,
                    staged,
                    self.task_id,
                    str(uuid.uuid4()),
                    "custom",
                )

        self.assertTrue(self.session.read_bytes().startswith(original_session))
        self.assertFalse(staged.exists())

    def test_reassigns_selected_thread_without_creating_a_duplicate_and_restores_full_backup(self):
        with self.session.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({
                "type": "response_item",
                "payload": {"conversation_id": self.task_id, "text": "preserve this conversation"},
            }) + "\n")
        original_session = self.session.read_bytes()
        original_index = (self.home / "session_index.jsonl").read_bytes()
        progress = []

        report = local_provider_sync.reassign_provider(
            self.home,
            "openai",
            "custom",
            {self.task_id},
            require_codex_closed=False,
            create_backup=True,
            progress_callback=lambda stage, detail: progress.append((stage, detail)),
        )

        self.assertEqual(report["reassigned"], 1)
        self.assertEqual(
            list(dict.fromkeys(stage for stage, _ in progress)),
            ["preflight", "backup", "rollouts", "database", "verify", "complete"],
        )
        self.assertTrue(report["backup_created"])
        self.assertEqual(len(list((self.home / "sessions").glob("*.jsonl"))), 1)
        self.assertEqual((self.home / "session_index.jsonl").read_bytes(), original_index)
        lines = self.session.read_text(encoding="utf-8").splitlines()
        self.assertEqual(json.loads(lines[0])["payload"]["model_provider"], "custom")
        self.assertIn("preserve this conversation", lines[1])
        connection = sqlite3.connect(self.home / "state_5.sqlite")
        row = connection.execute(
            "select id,model_provider,rollout_path from threads where id=?", (self.task_id,)
        ).fetchone()
        connection.close()
        self.assertEqual(row, (self.task_id, "custom", str(self.session)))

        backup_root = Path(report["backup_path"])
        full_session_backup = backup_root / "files" / self.session.relative_to(self.home)
        self.assertEqual(full_session_backup.read_bytes(), original_session)

        migration_bundle.restore_backup(backup_root, self.home, require_codex_closed=False)
        self.assertEqual(self.session.read_bytes(), original_session)
        connection = sqlite3.connect(self.home / "state_5.sqlite")
        provider = connection.execute(
            "select model_provider from threads where id=?", (self.task_id,)
        ).fetchone()[0]
        connection.close()
        self.assertEqual(provider, "openai")

    def test_reassign_without_backup_leaves_no_persistent_backup(self):
        report = local_provider_sync.reassign_provider(
            self.home,
            "openai",
            "custom",
            {self.task_id},
            require_codex_closed=False,
            create_backup=False,
        )

        self.assertFalse(report["backup_created"])
        self.assertIsNone(report["backup_path"])
        self.assertEqual(migration_bundle.list_backups(self.home), [])
        first_line = json.loads(self.session.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(first_line["payload"]["model_provider"], "custom")

    def test_reassign_without_backup_rolls_back_file_and_database_on_failure(self):
        original_session = self.session.read_bytes()
        with mock.patch.object(
            local_provider_sync,
            "_update_selected_sqlite_provider",
            side_effect=RuntimeError("injected reassignment failure"),
        ):
            with self.assertRaisesRegex(RuntimeError, "injected reassignment failure"):
                local_provider_sync.reassign_provider(
                    self.home,
                    "openai",
                    "custom",
                    {self.task_id},
                    require_codex_closed=False,
                    create_backup=False,
                )

        self.assertEqual(self.session.read_bytes(), original_session)
        connection = sqlite3.connect(self.home / "state_5.sqlite")
        provider = connection.execute(
            "select model_provider from threads where id=?", (self.task_id,)
        ).fetchone()[0]
        connection.close()
        self.assertEqual(provider, "openai")


if __name__ == "__main__":
    unittest.main()
