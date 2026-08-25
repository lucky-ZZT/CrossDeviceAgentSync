import os
import sys
import tempfile
import threading
import time
import tkinter as tk
import unittest
from pathlib import Path
from unittest import mock


SCRIPTS = Path(__file__).parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import simple_sync_gui


class BackupTimestampTests(unittest.TestCase):
    def test_utc_backup_time_is_displayed_in_selected_local_timezone(self):
        timezone = simple_sync_gui.dt.timezone(simple_sync_gui.dt.timedelta(hours=8))

        displayed = simple_sync_gui.format_backup_created_at(
            "2026-08-17T08:42:02.528983+00:00", timezone=timezone
        )

        self.assertEqual(displayed, "2026-08-17 16:42:02")

    def test_epoch_time_is_displayed_in_selected_local_timezone(self):
        timezone = simple_sync_gui.dt.timezone(simple_sync_gui.dt.timedelta(hours=8))

        displayed = simple_sync_gui.format_epoch_timestamp(1785377450, timezone=timezone)

        self.assertEqual(displayed, "2026-07-30 10:10:50")


class SimpleGuiLayoutTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.environment = mock.patch.dict(os.environ, {"LOCALAPPDATA": self.temporary.name})
        self.environment.start()
        try:
            self.app = simple_sync_gui.SimpleApp()
        except tk.TclError as error:
            self.environment.stop()
            self.temporary.cleanup()
            self.skipTest(f"Tk display is unavailable: {error}")
        self.app.geometry("850x650+20+20")

    def tearDown(self):
        if hasattr(self, "app") and self.app.winfo_exists():
            self.app.on_close()
        self.environment.stop()
        self.temporary.cleanup()

    def _walk(self, widget):
        for child in widget.winfo_children():
            yield child
            yield from self._walk(child)

    def _assert_buttons_visible(self, expected):
        self.app.update_idletasks()
        self.app.update()
        buttons = {
            child.cget("text"): child
            for child in self._walk(self.app)
            if child.winfo_class() == "TButton"
        }
        for label in expected:
            self.assertIn(label, buttons)
            button = buttons[label]
            self.assertTrue(button.winfo_ismapped(), label)
            x = button.winfo_rootx() - self.app.winfo_rootx()
            y = button.winfo_rooty() - self.app.winfo_rooty()
            self.assertGreaterEqual(x, 0, label)
            self.assertGreaterEqual(y, 0, label)
            self.assertLessEqual(x + button.winfo_width(), self.app.winfo_width(), label)
            self.assertLessEqual(y + button.winfo_height(), self.app.winfo_height(), label)

    def test_provider_controls_and_scrollbars_remain_visible(self):
        self.app.load_local_agents = lambda: None
        self.app.refresh_backup_summary = lambda: None
        self.app.show_local_agents()
        self._assert_buttons_visible({"检查归属切换", "显示所选", "隐藏所选", "切换所选归属", "创建副本...", "全选", "全不选", "反选"})
        scrollbars = [child for child in self._walk(self.app) if child.winfo_class() == "TScrollbar"]
        orientations = {str(scrollbar.cget("orient")) for scrollbar in scrollbars if scrollbar.winfo_ismapped()}
        self.assertEqual(orientations, {"vertical", "horizontal"})

    def test_content_manager_size_heading_sorts_by_bytes_and_toggles_direction(self):
        self.app.show_content_manager()
        tree = self.app.conversation_tree
        tree.insert("", "end", iid="small", values=("small", "p", "c", "x", "", "900.0 KB", "0 B", "", ""))
        tree.insert("", "end", iid="large", values=("large", "p", "c", "x", "", "10.0 MB", "0 B", "", ""))
        tree.insert("", "end", iid="medium", values=("medium", "p", "c", "x", "", "2.0 MB", "0 B", "", ""))

        self.app._sort_manager_tree(tree, "size")

        self.assertEqual(tree.get_children(), ("small", "medium", "large"))
        self.assertEqual(tree.heading("size", "text"), "大小 ↑")

        self.app._sort_manager_tree(tree, "size")

        self.assertEqual(tree.get_children(), ("large", "medium", "small"))
        self.assertEqual(tree.heading("size", "text"), "大小 ↓")

    def test_content_manager_reapplies_size_sort_after_filter_refresh(self):
        self.app.show_content_manager()
        self.app.manager_project_filter_paths = {"全部项目": None}
        self.app.manager_project_filter.set("全部项目")
        self.app.manager_conversations = {
            "large": {
                "task_id": "large", "title": "large", "project_name": "p", "provider": "c",
                "project_path": "x", "updated_at": "2026-08-25T00:00:00", "size_bytes": 10 * 1024 ** 2,
                "image_bytes": 0, "archived": False,
            },
            "small": {
                "task_id": "small", "title": "small", "project_name": "p", "provider": "c",
                "project_path": "x", "updated_at": "2026-08-24T00:00:00", "size_bytes": 900 * 1024,
                "image_bytes": 0, "archived": False,
            },
        }
        self.app.manager_inventory = {"consistency": {"stale_catalog": []}}
        self.app.conversation_tree.manager_sort_column = "size"
        self.app.conversation_tree.manager_sort_descending = True

        self.app.filter_managed_conversations()

        self.assertEqual(self.app.conversation_tree.get_children(), ("large", "small"))
        self.assertEqual(self.app.conversation_tree.heading("size", "text"), "大小 ↓")

    def test_provider_mode_refresh_preserves_current_owner_column(self):
        self.app.load_local_agents = lambda: None
        self.app.refresh_backup_summary = lambda: None
        self.app.show_local_agents()
        self.app.local_thread_by_id = {
            "thread-1": {"model_provider": "custom"},
        }
        self.app.tree.insert(
            "", "end", iid="thread-1", values=("☑", "对话", "正在显示", "custom")
        )

        self.app.update_provider_mode()

        self.assertEqual(self.app.tree.item("thread-1", "values")[3], "custom")

    def test_application_update_controls_remain_visible(self):
        self.app.show_updates()
        self._assert_buttons_visible({
            "检查新版本",
            "打开 Release 页面",
            "立即更新",
        })
        labels = {
            child.cget("text")
            for child in self._walk(self.app)
            if child.winfo_class() == "TButton" and child.winfo_ismapped()
        }
        self.assertNotIn("下载并安装", labels)
        self.assertIn("立即更新", labels)
        self.assertNotIn("检查软件更新", labels)
        self.assertNotIn("复制统一交接指令", labels)
        self.assertNotIn("打开审查报告目录", labels)
        button = next(
            child for child in self._walk(self.app)
            if child.winfo_class() == "TButton" and child.cget("text") == "立即更新"
        )
        self.assertEqual(str(button.cget("state")), "disabled")

    def test_backup_search_preview_and_scrollbars_remain_visible(self):
        with mock.patch.object(simple_sync_gui.migration_bundle, "list_backups", return_value=[]):
            self.app.show_backups()
        self._assert_buttons_visible({"检索全部备份", "预览所选", "恢复指定对话"})
        scrollbars = [child for child in self._walk(self.app) if child.winfo_class() == "TScrollbar"]
        orientations = {str(scrollbar.cget("orient")) for scrollbar in scrollbars if scrollbar.winfo_ismapped()}
        self.assertEqual(orientations, {"vertical", "horizontal"})

    def test_home_has_one_read_only_update_entry(self):
        self.app.show_home()
        labels = [
            child.cget("text")
            for child in self._walk(self.app)
            if child.winfo_class() == "TButton"
        ]
        self.assertEqual(labels.count("检查更新"), 1)
        self.assertNotIn("软件更新", labels)
        self.assertNotIn("参考项目更新审查", labels)

    def test_import_requires_a_check_before_starting(self):
        self.app.show_import()
        self._assert_buttons_visible({"检查迁移包", "开始导入"})
        buttons = {
            child.cget("text"): child
            for child in self._walk(self.app)
            if child.winfo_class() == "TButton"
        }
        self.assertEqual(str(buttons["开始导入"].cget("state")), "disabled")

    def test_project_import_requires_a_check_before_starting(self):
        self.app.show_project_transfer()
        self._assert_buttons_visible({"我在旧电脑：导出", "我在新电脑：检查并导入"})
        self.app.show_project_import()
        self._assert_buttons_visible({"检查项目迁移包", "开始导入项目"})
        buttons = {
            child.cget("text"): child
            for child in self._walk(self.app)
            if child.winfo_class() == "TButton"
        }
        self.assertEqual(str(buttons["开始导入项目"].cget("state")), "disabled")
        checkbuttons = {
            child.cget("text")
            for child in self._walk(self.app)
            if child.winfo_class() == "TCheckbutton"
        }
        self.assertIn("导入项目文件", checkbuttons)
        self.app.project_import_notebook.select(1)
        self._assert_buttons_visible({"全选", "全不选", "反选"})

    def test_home_combines_cross_computer_project_and_conversation_transfer(self):
        self.app.show_home()
        labels = {
            child.cget("text")
            for child in self._walk(self.app)
            if child.winfo_class() == "TButton"
        }
        self.assertIn("两台电脑之间传输", labels)
        self.assertNotIn("迁移到另一台电脑", labels)
        self.assertNotIn("导入旧电脑项目", labels)

    def test_content_manager_has_separate_conversation_project_and_image_controls(self):
        self.app.show_content_manager()
        self._assert_buttons_visible({
            "扫描内容", "修复路径问题", "预览对话", "归档所选", "复原所选归档", "备份并删除所选对话"
        })
        self.app.manager_notebook.select(1)
        self._assert_buttons_visible({"删除关联对话", "项目移入回收区", "恢复最近移除项目"})
        self.app.manager_notebook.select(2)
        self._assert_buttons_visible({"选择浏览器截图", "选择低风险图片", "预览图片", "保留1份，清理重复图片", "备份并彻底清理图片"})
        self.assertEqual(str(self.app.manager_delete_conversations_button.cget("state")), "disabled")
        self.assertEqual(str(self.app.manager_preview_conversation_button.cget("state")), "disabled")
        self.assertEqual(str(self.app.manager_archive_conversations_button.cget("state")), "disabled")
        self.assertEqual(str(self.app.manager_unarchive_conversations_button.cget("state")), "disabled")
        self.assertEqual(str(self.app.manager_repair_paths_button.cget("state")), "disabled")
        self.assertEqual(str(self.app.manager_project_filter_combo.cget("state")), "disabled")
        self.assertEqual(str(self.app.manager_clean_images_button.cget("state")), "disabled")
        self.assertTrue(self.app.conversation_tree.bind("<Double-1>"))

    def test_project_registration_buttons_follow_capabilities_not_status_labels(self):
        capabilities = {
            "details": {"enabled": True, "reason": "details"},
            "keep": {"enabled": True, "reason": "keep"},
            "normalize": {"enabled": True, "reason": "verified"},
            "repoint": {"enabled": True, "reason": "repoint"},
            "rename": {"enabled": True, "reason": "rename"},
            "delete": {"enabled": False, "reason": "unknown reference"},
            "full_delete": {"enabled": False, "reason": "shared directory"},
        }
        health = {
            "actionable_project_registrations": [{
                "project_id": "project-1",
                "project_name": "demo",
                "status": "普通路径",
                "roots": [{
                    "raw_path": r"C:\projects\demo",
                    "normalized_path": r"C:\projects\demo",
                    "path_kind": "ordinary",
                    "exists": True,
                }],
                "linked_tasks": 0,
                "known_reference_count": 0,
                "recommended_action": "keep",
                "related_tasks": [],
                "capabilities": capabilities,
            }],
            "repairable_paths": [],
            "normalization_triggers": [],
        }
        observed = {}

        def inspect_dialog():
            windows = [child for child in self.app.winfo_children() if child.winfo_class() == "Toplevel"]
            self.assertTrue(windows)
            window = windows[-1]
            buttons = {
                child.cget("text"): child
                for child in self._walk(window)
                if child.winfo_class() == "TButton"
            }
            observed["normalize"] = str(buttons["修复路径"].cget("state"))
            observed["delete"] = str(buttons["删除注册"].cget("state"))
            observed["full_delete"] = str(buttons["彻底删除项目"].cget("state"))
            buttons["取消"].invoke()

        self.app.after(50, inspect_dialog)
        result = self.app.choose_path_repair_actions(health)

        self.assertIsNone(result)
        self.assertEqual(observed["normalize"], "normal")
        self.assertEqual(observed["delete"], "disabled")
        self.assertEqual(observed["full_delete"], "disabled")

    def test_rollout_only_path_repair_does_not_open_empty_project_dialog(self):
        self.app.show_content_manager()
        self.app.manager_inventory = {
            "path_health": {
                "repairable_paths": [{"task_id": "task-1"}],
                "blocked_paths": [],
                "normalization_triggers": ["threads_rollout_path_normalize_after_insert"],
                "actionable_project_registrations": [],
            },
        }
        self.app.manager_codex_home.set(str(Path(self.temporary.name) / ".codex"))
        with mock.patch.object(self.app, "choose_path_repair_actions") as choose_dialog, \
             mock.patch.object(simple_sync_gui.messagebox, "askyesno", return_value=False) as confirm:
            self.app.repair_managed_rollout_paths()

        choose_dialog.assert_not_called()
        confirm.assert_called_once()

    def test_content_manager_can_repair_a_partial_archive_state(self):
        self.app.show_content_manager()
        self.app.manager_conversations = {
            "thread-1": {
                "archived": True,
                "archive_consistent": False,
            },
        }
        self.app.conversation_tree.insert(
            "", "end", iid="thread-1", values=("对话", "项目", "custom", "路径", "", "", "", "归档状态异常", "")
        )
        self.app.conversation_tree.selection_set("thread-1")
        self.app._begin_manager_action = mock.Mock()
        self.app.run = mock.Mock()

        with mock.patch.object(simple_sync_gui.messagebox, "askyesno", return_value=True), mock.patch.object(
            simple_sync_gui.messagebox, "showinfo"
        ) as showinfo:
            self.app.change_managed_archive_state(True)

        self.app.run.assert_called_once()
        showinfo.assert_not_called()

    def test_conversation_delete_confirmation_lists_titles_and_projects(self):
        self.app.show_content_manager()
        self.app.manager_conversations = {
            "thread-1": {
                "title": "创建 electrical-calc 项目",
                "project_name": "electrical-calc",
            },
        }
        self.app.conversation_tree.insert(
            "", "end", iid="thread-1", values=("创建 electrical-calc 项目", "electrical-calc", "custom", "", "", "", "", "使用中", "")
        )
        self.app.conversation_tree.selection_set("thread-1")

        with mock.patch.object(simple_sync_gui.messagebox, "askyesno", return_value=False) as confirm:
            self.app.delete_managed_conversations()

        prompt = confirm.call_args.args[1]
        self.assertIn("创建 electrical-calc 项目", prompt)
        self.assertIn("electrical-calc", prompt)
        self.assertIn("暂无聊天", prompt)

    def test_conversation_project_filter_groups_visible_rows(self):
        self.app.show_content_manager()
        conversations = []
        for task_id, project_name, project_path, updated, archived in (
            ("task-a", "alpha", r"C:\projects\alpha", "2026-08-17T10:00:00Z", False),
            ("task-b", "beta", r"C:\projects\beta", "2026-08-17T09:00:00Z", False),
            ("task-c", "alpha", r"C:\projects\alpha", "2026-08-17T08:00:00Z", True),
        ):
            conversations.append({
                "task_id": task_id,
                "title": task_id,
                "provider": "custom",
                "project_name": project_name,
                "project_path": project_path,
                "cwd": project_path,
                "updated_at": updated,
                "size_bytes": 100,
                "image_bytes": 0,
                "archived": archived,
                "possible_duplicates": 0,
            })
        self.app.complete_content_scan({
            "conversations": conversations,
            "projects": [],
            "images": [
                {
                    "title": "active image",
                    "size_bytes": 10,
                    "occurrences": 1,
                    "stored_bytes": 10,
                    "updated_at": "2026-08-17T10:00:00Z",
                    "kinds": ["user_image"],
                    "mime_type": "image/png",
                    "risk_level": "高",
                    "archived": False,
                },
                {
                    "title": "archived image",
                    "size_bytes": 20,
                    "occurrences": 1,
                    "stored_bytes": 20,
                    "updated_at": "2026-08-17T08:00:00Z",
                    "kinds": ["browser_screenshot"],
                    "mime_type": "image/png",
                    "risk_level": "高",
                    "archived": True,
                },
            ],
            "consistency": {
                "catalog_available": True,
                "catalog_visible": 4,
                "stale_catalog": [{
                    "task_id": "stale-alpha",
                    "title": "stale",
                    "project_path": r"C:\projects\alpha",
                    "source_detail": r"C:\missing.jsonl",
                }],
                "stale_catalog_ids": ["stale-alpha"],
                "state_only_ids": [],
                "index_only_ids": [],
                "orphan_rollout_ids": [],
                "missing_file_ids": [],
            },
            "path_health": {
                "extended_paths": [],
                "repairable_paths": [],
                "blocked_paths": [],
                "normalization_triggers": [
                    "threads_rollout_path_normalize_after_insert",
                    "threads_rollout_path_normalize_after_update",
                ],
            },
            "summary": {
                "conversations": 3,
                "projects": 2,
                "unique_images": 0,
                "image_occurrences": 0,
                "image_bytes": 0,
                "missing_files": 0,
                "catalog_visible": 4,
                "stale_catalog": 1,
            },
        })

        values = tuple(self.app.manager_project_filter_combo.cget("values"))
        self.assertIn("alpha (未归档 1，残留 1)", values)
        self.assertIn("beta (未归档 1)", values)
        self.app.manager_project_filter.set("alpha (未归档 1，残留 1)")
        self.app.filter_managed_conversations()
        children = self.app.conversation_tree.get_children()
        self.assertEqual(len(children), 1)
        self.assertEqual(len(self.app.image_manager_tree.get_children()), 1)
        self.assertEqual({self.app.conversation_tree.item(item, "values")[1] for item in children}, {"alpha"})
        self.assertIn("当前项目显示 1 个，已隐藏归档 1 个", self.app.manager_summary.get())
        self.assertIn("遗留触发器 2 个", self.app.manager_path_health.get())
        self._assert_buttons_visible({"修复路径问题", "清理侧栏残留", "一致性报告"})

        self.app.manager_hide_archived.set(False)
        self.app.toggle_managed_archived_visibility()

        self.assertEqual(len(self.app.conversation_tree.get_children()), 2)
        self.assertEqual(len(self.app.image_manager_tree.get_children()), 2)
        self.assertEqual(self.app.manager_project_filter.get(), "alpha (可用 2，残留 1)")

    def test_registered_empty_project_is_visible_in_tree_and_filter(self):
        self.app.show_content_manager()
        project_path = r"C:\Users\ZZT\Documents\Codex\conversation-demo-project"
        self.app.complete_content_scan({
            "conversations": [],
            "projects": [{
                "path": project_path,
                "thread_count": 0,
                "conversation_bytes": 0,
                "image_bytes": 0,
                "latest_updated_at": "",
                "exists": True,
                "registered": True,
                "project_name": "conversation-demo-project",
                "registration_ids": ["project-without-chat"],
                "registration_names": ["conversation-demo-project"],
                "registration_statuses": ["扩展路径"],
                "possible_duplicates": 0,
            }],
            "images": [],
            "consistency": {
                "catalog_available": False,
                "catalog_visible": 0,
                "stale_catalog": [],
                "stale_catalog_ids": [],
                "state_only_ids": [],
                "index_only_ids": [],
                "orphan_rollout_ids": [],
                "missing_file_ids": [],
            },
            "path_health": {
                "extended_paths": [],
                "repairable_paths": [],
                "blocked_paths": [],
                "normalization_triggers": [],
                "project_extended_paths": [],
                "repairable_project_paths": [],
                "duplicate_projects": [],
                "blocked_duplicate_projects": [],
                "blocked_project_paths": [],
                "removable_projects": [],
                "actionable_project_registrations": [],
            },
            "summary": {
                "conversations": 0,
                "projects": 1,
                "unique_images": 0,
                "image_occurrences": 0,
                "image_bytes": 0,
                "missing_files": 0,
                "catalog_visible": 0,
                "stale_catalog": 0,
                "state_only": 0,
                "index_only": 0,
                "orphan_rollouts": 0,
                "extended_rollout_paths": 0,
                "repairable_rollout_paths": 0,
                "rollout_path_triggers": 0,
            },
        })

        self.assertEqual(len(self.app.project_manager_tree.get_children()), 1)
        project_values = self.app.project_manager_tree.item(
            self.app.project_manager_tree.get_children()[0], "values"
        )
        self.assertEqual(project_values[0], project_path)
        self.assertEqual(project_values[1], "0")
        self.assertEqual(project_values[5], "已注册，暂无聊天")
        labels = tuple(self.app.manager_project_filter_combo.cget("values"))
        self.assertIn("conversation-demo-project (已注册，暂无聊天)", labels)
        self.app.manager_project_filter.set("conversation-demo-project (已注册，暂无聊天)")
        self.app.filter_managed_conversations()
        self.assertEqual(self.app.conversation_tree.get_children(), ())
        self.assertIn("当前项目显示 0 个", self.app.manager_summary.get())

    def test_unknown_codex_schema_keeps_preview_but_disables_mutations(self):
        self.app.show_content_manager()
        self.app.complete_content_scan({
            "conversations": [],
            "projects": [],
            "images": [],
            "consistency": {
                "catalog_available": False,
                "catalog_visible": 0,
                "stale_catalog": [],
                "stale_catalog_ids": [],
                "state_only_ids": [],
                "index_only_ids": [],
                "orphan_rollout_ids": [],
                "missing_file_ids": [],
            },
            "path_health": {
                "extended_paths": [],
                "repairable_paths": [{"task_id": "task-1"}],
                "blocked_paths": [],
                "normalization_triggers": [],
                "project_extended_paths": [],
                "repairable_project_paths": [],
                "duplicate_projects": [],
                "blocked_duplicate_projects": [],
                "blocked_project_paths": [],
                "removable_projects": [],
                "actionable_project_registrations": [],
            },
            "compatibility": {
                "status": "read_only",
                "write_compatible": False,
                "blockers": ["状态数据库协议 51 高于已验证上限 50"],
                "warnings": [],
                "state_schema_version": 51,
                "history_schema_version": 4,
                "project_storage_mode": "state_db",
            },
            "summary": {
                "conversations": 0,
                "projects": 0,
                "unique_images": 0,
                "image_occurrences": 0,
                "image_bytes": 0,
                "missing_files": 0,
                "catalog_visible": 0,
                "stale_catalog": 0,
            },
        })

        self.assertIn("只读保护", self.app.manager_compatibility.get())
        self.assertEqual(str(self.app.manager_repair_paths_button.cget("state")), "disabled")
        self.assertEqual(str(self.app.manager_delete_conversations_button.cget("state")), "disabled")
        self.assertEqual(str(self.app.manager_clean_images_button.cget("state")), "disabled")
        self.assertEqual(str(self.app.manager_consistency_button.cget("state")), "normal")

    def test_partial_codex_schema_locks_only_unsupported_mutations(self):
        self.app.show_content_manager()
        self.app.complete_content_scan({
            "conversations": [], "projects": [], "images": [],
            "consistency": {
                "catalog_available": True, "catalog_visible": 0,
                "stale_catalog": [], "stale_catalog_ids": [],
                "state_only_ids": [], "index_only_ids": [],
                "orphan_rollout_ids": [], "missing_file_ids": [],
            },
            "path_health": {
                "extended_paths": [], "repairable_paths": [{"task_id": "task-1"}],
                "blocked_paths": [], "normalization_triggers": [],
                "project_extended_paths": [], "repairable_project_paths": [],
                "duplicate_projects": [], "blocked_duplicate_projects": [],
                "blocked_project_paths": [], "removable_projects": [],
                "actionable_project_registrations": [],
            },
            "compatibility": {
                "status": "partial", "write_compatible": True,
                "blockers": [], "warnings": [], "state_schema_version": 50,
                "history_schema_version": 4, "project_storage_mode": "transitioning",
                "operation_capabilities": {
                    "path_repair": True, "sidebar_cleanup": True,
                    "conversation_content": False, "thread_lifecycle": False,
                    "conversation_import": False, "project_registry": True,
                    "full_project_delete": False,
                },
            },
            "summary": {
                "conversations": 0, "projects": 0, "unique_images": 0,
                "image_occurrences": 0, "image_bytes": 0, "missing_files": 0,
                "catalog_visible": 0, "stale_catalog": 0,
            },
        })

        self.assertIn("部分写入受限", self.app.manager_compatibility.get())
        self.assertEqual(str(self.app.manager_repair_paths_button.cget("state")), "normal")
        self.assertEqual(str(self.app.manager_clean_stale_button.cget("state")), "disabled")
        self.assertEqual(str(self.app.manager_archive_conversations_button.cget("state")), "disabled")
        self.assertEqual(str(self.app.manager_delete_conversations_button.cget("state")), "disabled")
        self.assertEqual(str(self.app.manager_clean_images_button.cget("state")), "disabled")
        self.assertEqual(str(self.app.manager_consistency_button.cget("state")), "normal")

    def test_background_operations_complete_on_the_tk_thread(self):
        main_thread = threading.get_ident()
        completed_threads = []
        self.app.run("test background operation", lambda: "done", lambda _: completed_threads.append(threading.get_ident()))
        deadline = time.monotonic() + 2
        while not completed_threads and time.monotonic() < deadline:
            self.app.update()
            time.sleep(0.02)
        self.assertEqual(completed_threads, [main_thread])

    def test_conversation_preview_keeps_first_request_separate_from_title(self):
        self.app.show_content_manager()
        self.app.show_report_window = mock.Mock()
        item = {
            "task_id": "019ffa1a-2a9c-7092-8f34-c16d33906405",
            "catalog_title": "conversational-project-builder",
            "original_title": "旧数据库标题",
            "cwd": r"C:\projects\sample",
            "provider": "openai",
            "project_name": "sample",
            "project_path": r"C:\projects\sample",
            "updated_at": "2026-08-17T10:00:00Z",
            "archived": False,
            "size_bytes": 100,
            "image_count": 0,
            "image_occurrences": 0,
            "image_bytes": 0,
        }
        preview = {
            "session_title": None,
            "first_user_message": "请整理这个项目",
            "message_count": 1,
            "tool_call_count": 0,
            "messages": [{"role": "user", "text": "请整理这个项目"}],
            "skipped_large_lines": 0,
        }

        self.app.show_conversation_preview(item, preview)

        report = self.app.show_report_window.call_args.args[1]
        self.assertIn("Codex 显示名称：conversational-project-builder", report)
        self.assertIn("最初请求：请整理这个项目", report)
        self.assertNotIn("内容摘要", report)


if __name__ == "__main__":
    unittest.main()
