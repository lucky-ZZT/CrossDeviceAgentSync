import os
import sys
import tempfile
import tkinter as tk
import unittest
from pathlib import Path
from unittest import mock


SCRIPTS = Path(__file__).parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import simple_sync_gui


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
        self._assert_buttons_visible({"执行前检查", "开始切换归属", "全选", "全不选", "反选"})
        scrollbars = [child for child in self._walk(self.app) if child.winfo_class() == "TScrollbar"]
        orientations = {str(scrollbar.cget("orient")) for scrollbar in scrollbars if scrollbar.winfo_ismapped()}
        self.assertEqual(orientations, {"vertical", "horizontal"})

    def test_application_update_controls_remain_visible(self):
        self.app.show_updates()
        self._assert_buttons_visible({
            "检查新版本",
            "打开 Release 页面",
        })
        labels = {
            child.cget("text")
            for child in self._walk(self.app)
            if child.winfo_class() == "TButton"
        }
        self.assertNotIn("下载并安装", labels)
        self.assertNotIn("检查软件更新", labels)
        self.assertNotIn("复制统一交接指令", labels)
        self.assertNotIn("打开审查报告目录", labels)

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


if __name__ == "__main__":
    unittest.main()
