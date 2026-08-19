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
            "archived_at integer, "
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

    def _add_thread(self, provider, *, archived=False, title="Extra chat"):
        task_id = str(uuid.uuid4())
        directory = "archived_sessions" if archived else "sessions"
        path = self.home / directory / f"{task_id}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({
                "type": "session_meta",
                "payload": {
                    "id": task_id,
                    "thread_name": title,
                    "model_provider": provider,
                },
            }) + "\n",
            encoding="utf-8",
        )
        connection = sqlite3.connect(self.home / "state_5.sqlite")
        connection.execute(
            "insert into threads (id,rollout_path,created_at,updated_at,source,model_provider,cwd,title,sandbox_policy,approval_mode,has_user_event,archived,archived_at) "
            "values (?,?,1,1,'vscode',?,?,?,'{}','on-request',1,?,?)",
            (task_id, str(path), provider, str(self.home), title, int(archived), 123 if archived else None),
        )
        connection.commit()
        connection.close()
        return task_id, path

    def test_discovers_config_rollout_and_sqlite_providers(self):
        providers = {item["id"]: item for item in local_provider_sync.discover_providers(self.home)}
        self.assertEqual(set(providers), {"openai", "custom"})
        self.assertTrue(providers["openai"]["current"])
        self.assertEqual(providers["openai"]["sqlite_count"], 1)
        self.assertTrue(providers["custom"]["configured"])

    def test_sync_all_to_provider_aligns_history_without_changing_config(self):
        original_config = (self.home / "config.toml").read_bytes()

        report = local_provider_sync.sync_all_to_provider(
            self.home,
            "custom",
            update_config=False,
            require_codex_closed=False,
            create_backup=False,
        )

        self.assertFalse(report["config_updated"])
        self.assertEqual(report["synchronized_rollouts"], 1)
        self.assertEqual((self.home / "config.toml").read_bytes(), original_config)
        first_line = json.loads(self.session.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(first_line["payload"]["model_provider"], "custom")
        connection = sqlite3.connect(self.home / "state_5.sqlite")
        provider = connection.execute(
            "select model_provider from threads where id=?", (self.task_id,)
        ).fetchone()[0]
        connection.close()
        self.assertEqual(provider, "custom")

    def test_switch_and_sync_updates_config_and_all_history(self):
        report = local_provider_sync.sync_all_to_provider(
            self.home,
            "custom",
            update_config=True,
            require_codex_closed=False,
            create_backup=True,
        )

        self.assertTrue(report["config_updated"])
        self.assertIn('model_provider = "custom"', (self.home / "config.toml").read_text(encoding="utf-8"))
        providers = {item["id"]: item for item in local_provider_sync.discover_providers(self.home)}
        self.assertTrue(providers["custom"]["current"])
        self.assertTrue(Path(report["backup_path"], "transaction.json").is_file())

        migration_bundle.restore_backup(
            Path(report["backup_path"]), self.home, require_codex_closed=False
        )
        self.assertIn('model_provider = "openai"', (self.home / "config.toml").read_text(encoding="utf-8"))

    def test_switch_preflight_blocks_historical_only_provider(self):
        (self.home / "config.toml").write_text('model_provider = "openai"\n', encoding="utf-8")

        report = local_provider_sync.preflight_full_provider_sync(
            self.home,
            "custom",
            update_config=True,
            create_backup=False,
            require_codex_closed=False,
        )

        self.assertFalse(report["ok"])
        self.assertIn("provider_not_configured", {problem["code"] for problem in report["problems"]})

    def test_provider_used_before_reassignment_remains_discoverable_from_backup(self):
        backup = migration_bundle.backup_root_for(self.home) / "previous-operation"
        backup.mkdir(parents=True)
        (backup / "transaction.json").write_text(
            json.dumps({
                "status": "committed",
                "operation": "provider_reassign",
                "source_provider": "legacy-custom",
                "target_provider": "openai",
            }),
            encoding="utf-8",
        )

        providers = {item["id"]: item for item in local_provider_sync.discover_providers(self.home)}

        self.assertIn("legacy-custom", providers)
        self.assertFalse(providers["legacy-custom"]["configured"])
        self.assertIn("managed-backup", providers["legacy-custom"]["sources"])

    def test_provider_workspace_hides_other_provider_and_restores_it_on_switch(self):
        hidden = local_provider_sync.apply_provider_workspace(
            self.home, "custom", require_codex_closed=False
        )

        self.assertEqual(hidden["archive_count"], 1)
        connection = sqlite3.connect(self.home / "state_5.sqlite")
        row = connection.execute(
            "select model_provider,archived,rollout_path,archived_at from threads where id=?", (self.task_id,)
        ).fetchone()
        connection.close()
        self.assertEqual(row[0:2], ("openai", 1))
        archived_path = Path(row[2])
        self.assertIn("archived_sessions", archived_path.parts)
        self.assertIsNotNone(row[3])
        self.assertTrue(archived_path.is_file())

        restored = local_provider_sync.apply_provider_workspace(
            self.home, "openai", require_codex_closed=False
        )

        self.assertEqual(restored["restore_count"], 1)
        connection = sqlite3.connect(self.home / "state_5.sqlite")
        row = connection.execute(
            "select model_provider,archived,rollout_path,archived_at from threads where id=?", (self.task_id,)
        ).fetchone()
        connection.close()
        self.assertEqual(row[0:2], ("openai", 0))
        self.assertIn("sessions", Path(row[2]).parts)
        self.assertIsNone(row[3])
        state = json.loads(local_provider_sync.provider_visibility_state_path(self.home).read_text(encoding="utf-8"))
        self.assertEqual(state["managed_hidden"], [])

    def test_provider_workspace_preserves_user_archived_conversation(self):
        archived_path = self.home / "archived_sessions" / self.session.name
        archived_path.parent.mkdir(parents=True)
        self.session.replace(archived_path)
        connection = sqlite3.connect(self.home / "state_5.sqlite")
        connection.execute(
            "update threads set model_provider='custom',archived=1,archived_at=123,rollout_path=? where id=?",
            (str(archived_path), self.task_id),
        )
        connection.commit()
        connection.close()

        report = local_provider_sync.apply_provider_workspace(
            self.home, "custom", require_codex_closed=False
        )

        self.assertEqual(report["restore_count"], 0)
        connection = sqlite3.connect(self.home / "state_5.sqlite")
        archived = connection.execute(
            "select archived from threads where id=?", (self.task_id,)
        ).fetchone()[0]
        connection.close()
        self.assertEqual(archived, 1)
        self.assertTrue(archived_path.is_file())

    def test_provider_workspace_repairs_database_archived_rollout_left_in_sessions(self):
        connection = sqlite3.connect(self.home / "state_5.sqlite")
        connection.execute(
            "update threads set archived=1,archived_at=123 where id=?",
            (self.task_id,),
        )
        connection.commit()
        connection.close()

        report = local_provider_sync.apply_provider_workspace(
            self.home, "openai", require_codex_closed=False, create_backup=False
        )

        self.assertEqual(report["archive_count"], 1)
        connection = sqlite3.connect(self.home / "state_5.sqlite")
        archived, archived_at, rollout_path = connection.execute(
            "select archived,archived_at,rollout_path from threads where id=?",
            (self.task_id,),
        ).fetchone()
        connection.close()
        self.assertEqual(archived, 1)
        self.assertIsNotNone(archived_at)
        self.assertIn("archived_sessions", Path(rollout_path).parts)
        self.assertTrue(Path(rollout_path).is_file())

    def test_provider_workspace_removes_stale_catalog_row_for_already_archived_other_provider(self):
        archived_path = self.home / "archived_sessions" / self.session.name
        archived_path.parent.mkdir(parents=True)
        self.session.replace(archived_path)
        connection = sqlite3.connect(self.home / "state_5.sqlite")
        connection.execute(
            "update threads set archived=1,archived_at=123,rollout_path=? where id=?",
            (str(archived_path), self.task_id),
        )
        connection.commit()
        connection.close()
        catalog_path = self.home / "sqlite" / "codex-dev.db"
        catalog_path.parent.mkdir(parents=True)
        catalog = sqlite3.connect(catalog_path)
        catalog.execute(
            "create table local_thread_catalog (host_id text,thread_id text,display_title text,cwd text,source_detail text,missing_candidate integer)"
        )
        catalog.execute(
            "create table local_thread_catalog_metadata (id integer primary key,catalog_revision integer)"
        )
        catalog.execute("insert into local_thread_catalog_metadata values (1,0)")
        catalog.execute(
            "insert into local_thread_catalog values ('local',?,?,?,?,0)",
            (self.task_id, "stale", str(self.home), str(archived_path)),
        )
        catalog.commit()
        catalog.close()

        report = local_provider_sync.apply_provider_workspace(
            self.home, "custom", require_codex_closed=False
        )

        self.assertEqual(report["archive_count"], 0)
        self.assertEqual(report["catalog_cleanup_count"], 1)
        catalog = sqlite3.connect(catalog_path)
        count = catalog.execute("select count(*) from local_thread_catalog").fetchone()[0]
        revision = catalog.execute(
            "select catalog_revision from local_thread_catalog_metadata where id=1"
        ).fetchone()[0]
        catalog.close()
        self.assertEqual(count, 0)
        self.assertEqual(revision, 1)

    def test_reassigning_from_active_provider_hides_conversation_until_target_is_active(self):
        moved = local_provider_sync.apply_provider_workspace(
            self.home,
            "openai",
            source_provider="openai",
            target_provider="custom",
            selected_ids={self.task_id},
            require_codex_closed=False,
        )

        self.assertEqual(moved["reassign_count"], 1)
        self.assertEqual(moved["archive_count"], 1)
        connection = sqlite3.connect(self.home / "state_5.sqlite")
        provider, archived = connection.execute(
            "select model_provider,archived from threads where id=?", (self.task_id,)
        ).fetchone()
        connection.close()
        self.assertEqual((provider, archived), ("custom", 1))

        local_provider_sync.apply_provider_workspace(self.home, "custom", require_codex_closed=False)
        connection = sqlite3.connect(self.home / "state_5.sqlite")
        provider, archived = connection.execute(
            "select model_provider,archived from threads where id=?", (self.task_id,)
        ).fetchone()
        connection.close()
        self.assertEqual((provider, archived), ("custom", 0))

    def test_auto_handoff_makes_target_sidebar_active_and_hides_source_sidebar(self):
        source_remaining_id, _ = self._add_thread("openai", title="Source remaining")
        target_hidden_id, _ = self._add_thread("custom", archived=True, title="Target hidden")
        state_path = local_provider_sync.provider_visibility_state_path(self.home)
        state_path.parent.mkdir(parents=True)
        state_path.write_text(
            json.dumps({
                "schema_version": 2,
                "active_provider": "openai",
                "managed_hidden": [target_hidden_id],
                "manual_hidden": [],
            }),
            encoding="utf-8",
        )

        report = local_provider_sync.apply_provider_workspace(
            self.home,
            "openai",
            source_provider="openai",
            target_provider="custom",
            selected_ids={self.task_id},
            auto_hide_reassigned=True,
            enforce_provider_isolation=False,
            require_codex_closed=False,
            create_backup=False,
        )

        self.assertEqual(report["active_provider"], "custom")
        connection = sqlite3.connect(self.home / "state_5.sqlite")
        rows = dict(connection.execute(
            "select id, archived from threads where id in (?,?,?)",
            (self.task_id, source_remaining_id, target_hidden_id),
        ).fetchall())
        providers = dict(connection.execute(
            "select id, model_provider from threads where id in (?,?,?)",
            (self.task_id, source_remaining_id, target_hidden_id),
        ).fetchall())
        connection.close()
        self.assertEqual(providers[self.task_id], "custom")
        self.assertEqual(rows[self.task_id], 0)
        self.assertEqual(rows[source_remaining_id], 1)
        self.assertEqual(rows[target_hidden_id], 0)

    def test_auto_handoff_does_not_restore_user_archived_target_thread(self):
        target_archived_id, target_archived_path = self._add_thread(
            "custom", archived=True, title="User archived target"
        )

        local_provider_sync.apply_provider_workspace(
            self.home,
            "openai",
            source_provider="openai",
            target_provider="custom",
            selected_ids={self.task_id},
            auto_hide_reassigned=True,
            enforce_provider_isolation=False,
            require_codex_closed=False,
            create_backup=False,
        )

        connection = sqlite3.connect(self.home / "state_5.sqlite")
        archived = connection.execute(
            "select archived from threads where id=?", (target_archived_id,)
        ).fetchone()[0]
        connection.close()
        self.assertEqual(archived, 1)
        self.assertTrue(target_archived_path.is_file())

    def test_provider_workspace_removes_orphan_catalog_rows_that_create_empty_projects(self):
        catalog_path = self.home / "sqlite" / "codex-dev.db"
        catalog_path.parent.mkdir(parents=True)
        catalog = sqlite3.connect(catalog_path)
        catalog.execute(
            "create table local_thread_catalog (host_id text,thread_id text,display_title text,cwd text,source_detail text,missing_candidate integer)"
        )
        catalog.execute(
            "create table local_thread_catalog_metadata (id integer primary key,catalog_revision integer)"
        )
        catalog.execute("insert into local_thread_catalog_metadata values (1,0)")
        catalog.execute(
            "insert into local_thread_catalog values ('local',?,?,?,?,0)",
            (self.task_id, "visible", str(self.home), str(self.session)),
        )
        orphan_id = str(uuid.uuid4())
        catalog.execute(
            "insert into local_thread_catalog values ('local',?,?,?,?,0)",
            (orphan_id, "empty project shell", r"C:\stale-project", r"C:\missing.jsonl"),
        )
        catalog.commit()
        catalog.close()

        report = local_provider_sync.apply_provider_workspace(
            self.home, "openai", require_codex_closed=False, create_backup=False
        )

        self.assertEqual(report["catalog_cleanup_count"], 1)
        catalog = sqlite3.connect(catalog_path)
        ids = {row[0] for row in catalog.execute("select thread_id from local_thread_catalog")}
        revision = catalog.execute(
            "select catalog_revision from local_thread_catalog_metadata where id=1"
        ).fetchone()[0]
        catalog.close()
        self.assertEqual(ids, {self.task_id})
        self.assertEqual(revision, 1)

    def test_provider_workspace_without_backup_keeps_no_persistent_backup(self):
        report = local_provider_sync.apply_provider_workspace(
            self.home,
            "custom",
            require_codex_closed=False,
            create_backup=False,
        )

        self.assertFalse(report["backup_created"])
        self.assertIsNone(report["backup_path"])
        self.assertFalse(migration_bundle.backup_root_for(self.home).exists())

    def test_provider_workspace_without_backup_rolls_back_a_failed_move(self):
        original = self.session.read_bytes()
        original_atomic_write = local_provider_sync.migration_bundle.atomic_write

        def fail_state_write(path, payload):
            if Path(path) == local_provider_sync.provider_visibility_state_path(self.home):
                raise RuntimeError("injected visibility state failure")
            return original_atomic_write(path, payload)

        with mock.patch.object(
            local_provider_sync.migration_bundle,
            "atomic_write",
            side_effect=fail_state_write,
        ):
            with self.assertRaisesRegex(RuntimeError, "injected visibility state failure"):
                local_provider_sync.apply_provider_workspace(
                    self.home,
                    "custom",
                    require_codex_closed=False,
                    create_backup=False,
                )

        self.assertTrue(self.session.is_file())
        self.assertEqual(self.session.read_bytes(), original)
        connection = sqlite3.connect(self.home / "state_5.sqlite")
        archived, rollout_path = connection.execute(
            "select archived,rollout_path from threads where id=?", (self.task_id,)
        ).fetchone()
        connection.close()
        self.assertEqual(archived, 0)
        self.assertEqual(Path(rollout_path), self.session)

    def test_selected_visibility_tracks_manual_hidden_state_and_can_be_restored(self):
        hidden = local_provider_sync.apply_provider_workspace(
            self.home,
            "openai",
            selected_ids={self.task_id},
            visibility_overrides={self.task_id: False},
            enforce_provider_isolation=False,
            require_codex_closed=False,
            create_backup=False,
        )
        self.assertEqual(hidden["archive_count"], 1)
        state = json.loads(local_provider_sync.provider_visibility_state_path(self.home).read_text(encoding="utf-8"))
        self.assertEqual(state["manual_hidden"], [self.task_id])

        local_provider_sync.apply_provider_workspace(
            self.home, "openai", require_codex_closed=False, create_backup=False
        )
        connection = sqlite3.connect(self.home / "state_5.sqlite")
        self.assertEqual(
            connection.execute("select archived from threads where id=?", (self.task_id,)).fetchone()[0],
            1,
        )
        connection.close()

        local_provider_sync.apply_provider_workspace(
            self.home,
            "openai",
            selected_ids={self.task_id},
            visibility_overrides={self.task_id: True},
            enforce_provider_isolation=False,
            require_codex_closed=False,
            create_backup=False,
        )
        state = json.loads(local_provider_sync.provider_visibility_state_path(self.home).read_text(encoding="utf-8"))
        self.assertEqual(state["manual_hidden"], [])
        connection = sqlite3.connect(self.home / "state_5.sqlite")
        self.assertEqual(
            connection.execute("select archived from threads where id=?", (self.task_id,)).fetchone()[0],
            0,
        )
        connection.close()

    def test_selected_visibility_cannot_show_a_different_provider(self):
        report = local_provider_sync.plan_provider_workspace(
            self.home,
            "custom",
            selected_ids={self.task_id},
            visibility_overrides={self.task_id: True},
            enforce_provider_isolation=False,
            create_backup=False,
        )
        self.assertFalse(report["ok"])
        self.assertIn("visibility_provider_mismatch", {item["code"] for item in report["problems"]})

    def test_failed_switch_sync_restores_config_rollout_and_database(self):
        original_config = (self.home / "config.toml").read_bytes()
        original_rollout = self.session.read_bytes()
        with mock.patch.object(
            local_provider_sync,
            "_update_all_sqlite_providers",
            side_effect=RuntimeError("injected full sync failure"),
        ):
            with self.assertRaisesRegex(RuntimeError, "injected full sync failure"):
                local_provider_sync.sync_all_to_provider(
                    self.home,
                    "custom",
                    update_config=True,
                    require_codex_closed=False,
                    create_backup=False,
                )

        self.assertEqual((self.home / "config.toml").read_bytes(), original_config)
        self.assertEqual(self.session.read_bytes(), original_rollout)
        connection = sqlite3.connect(self.home / "state_5.sqlite")
        provider = connection.execute(
            "select model_provider from threads where id=?", (self.task_id,)
        ).fetchone()[0]
        connection.close()
        self.assertEqual(provider, "openai")

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
