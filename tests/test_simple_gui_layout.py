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
        self._assert_buttons_visible({"我在旧电脑：导出项目", "我在新电脑：导入项目"})
        self.app.show_project_import()
        self._assert_buttons_visible({"检查项目迁移包", "开始导入项目"})
        buttons = {
            child.cget("text"): child
            for child in self._walk(self.app)
            if child.winfo_class() == "TButton"
        }
        self.assertEqual(str(buttons["开始导入项目"].cget("state")), "disabled")

    def test_content_manager_has_separate_conversation_project_and_image_controls(self):
        self.app.show_content_manager()
        self._assert_buttons_visible({
            "扫描内容", "预览对话", "归档所选", "复原所选归档", "备份并删除所选对话"
        })
        self.app.manager_notebook.select(1)
        self._assert_buttons_visible({"删除关联对话", "项目移入回收区", "恢复最近移除项目"})
        self.app.manager_notebook.select(2)
        self._assert_buttons_visible({"选择浏览器截图", "选择低风险图片", "预览图片", "保留1份，清理重复图片", "备份并彻底清理图片"})
        self.assertEqual(str(self.app.manager_delete_conversations_button.cget("state")), "disabled")
        self.assertEqual(str(self.app.manager_preview_conversation_button.cget("state")), "disabled")
        self.assertEqual(str(self.app.manager_archive_conversations_button.cget("state")), "disabled")
        self.assertEqual(str(self.app.manager_unarchive_conversations_button.cget("state")), "disabled")
        self.assertEqual(str(self.app.manager_project_filter_combo.cget("state")), "disabled")
        self.assertEqual(str(self.app.manager_clean_images_button.cget("state")), "disabled")
        self.assertTrue(self.app.conversation_tree.bind("<Double-1>"))

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

        self.app.manager_hide_archived.set(False)
        self.app.toggle_managed_archived_visibility()

        self.assertEqual(len(self.app.conversation_tree.get_children()), 2)
        self.assertEqual(len(self.app.image_manager_tree.get_children()), 2)
        self.assertEqual(self.app.manager_project_filter.get(), "alpha (可用 2，残留 1)")

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
