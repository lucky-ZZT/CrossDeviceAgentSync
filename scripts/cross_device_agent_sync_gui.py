#!/usr/bin/env python3
"""Windows GUI for selective Codex conversation migration."""

from __future__ import annotations

import json
import os
import queue
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import migration_bundle
import generic_sync
import session_merge_planner as planner


APP_NAME = "Cross-Device Agent Sync"
APP_VERSION = "1.0.6"


def default_codex_home() -> str:
    return str(Path.home() / ".codex")


class AdvancedApp(tk.Tk):
    def __init__(self, on_back=None) -> None:
        super().__init__()
        self.on_back = on_back
        self.title(f"{APP_NAME} {APP_VERSION}")
        self.geometry("1080x720")
        self.minsize(920, 620)
        self.option_add("*Font", ("Microsoft YaHei UI", 10))
        self.current_plan: dict | None = None
        self.selected_ids: set[str] = set()
        self.status = tk.StringVar(value="就绪")
        self._ui_events = queue.Queue()
        self._closing = False
        self.protocol("WM_DELETE_WINDOW", self._return_to_simple)
        self._ui_event_after = self.after(50, self._drain_ui_events)
        self._build_ui()

    def _build_ui(self) -> None:
        navigation = ttk.Frame(self, padding=(12, 10, 12, 0))
        navigation.pack(fill="x")
        ttk.Button(navigation, text="返回简洁模式", command=self._return_to_simple).pack(side="left")
        ttk.Label(navigation, text="高级模式", font=("Microsoft YaHei UI", 12, "bold")).pack(side="left", padx=12)
        notebook = ttk.Notebook(self)
        notebook.pack(fill="both", expand=True, padx=12, pady=(10, 6))
        self._build_generic_compare_tab(notebook)
        self._build_generic_package_tab(notebook)
        self._build_generic_restore_tab(notebook)
        self._build_inventory_tab(notebook)
        self._build_compare_tab(notebook)
        self._build_package_tab(notebook)
        self._build_restore_tab(notebook)
        ttk.Separator(self).pack(fill="x", padx=12)
        ttk.Label(self, textvariable=self.status, anchor="w").pack(fill="x", padx=14, pady=8)

    def _return_to_simple(self) -> None:
        self._closing = True
        if self._ui_event_after:
            self.after_cancel(self._ui_event_after)
            self._ui_event_after = None
        callback = self.on_back
        self.on_back = None
        self.destroy()
        if callback:
            callback()

    def _post_ui_event(self, callback) -> None:
        self._ui_events.put(callback)

    def _drain_ui_events(self) -> None:
        for _ in range(100):
            try:
                callback = self._ui_events.get_nowait()
            except queue.Empty:
                break
            try:
                callback()
            except Exception as error:
                self._fail(error)
        if not self._closing:
            self._ui_event_after = self.after(50, self._drain_ui_events)

    def _build_generic_compare_tab(self, notebook) -> None:
        frame = ttk.Frame(notebook, padding=14)
        notebook.add(frame, text="A. 代理/文件对比")
        frame.columnconfigure(1, weight=1)
        frame.rowconfigure(7, weight=1)
        self.generic_left_root = tk.StringVar()
        self.generic_right_root = tk.StringVar()
        self.generic_left_id = tk.StringVar(value="agent-a")
        self.generic_right_id = tk.StringVar(value="agent-b")
        self.generic_include = tk.StringVar(value="**/*")
        self.generic_exclude = tk.StringVar(value="")
        self.generic_direction = tk.StringVar(value="bidirectional")
        self.generic_plan_output = tk.StringVar(value=str(Path.home() / "Desktop" / "generic-sync-plan.json"))
        self._entry_row(frame, 0, "左端点根目录", self.generic_left_root, lambda: self._choose_dir(self.generic_left_root))
        self._entry_row(frame, 1, "右端点根目录", self.generic_right_root, lambda: self._choose_dir(self.generic_right_root))
        self._entry_row(frame, 2, "左端点/代理 ID", self.generic_left_id)
        self._entry_row(frame, 3, "右端点/代理 ID", self.generic_right_id)
        self._entry_row(frame, 4, "包含规则", self.generic_include)
        self._entry_row(frame, 5, "排除规则", self.generic_exclude)
        ttk.Label(frame, text="方向").grid(row=6, column=0, sticky="w", padx=(0, 10), pady=5)
        ttk.Combobox(frame, textvariable=self.generic_direction, state="readonly", values=("bidirectional", "left-to-right", "right-to-left"), width=18).grid(row=6, column=1, sticky="w")
        ttk.Button(frame, text="扫描并比较", command=self._generic_compare).grid(row=6, column=2, padx=8)
        columns = ("selected", "path", "classification", "action", "left_hash", "right_hash")
        self.generic_tree = ttk.Treeview(frame, columns=columns, show="headings", selectmode="browse")
        headings = {"selected": "选择", "path": "相对路径", "classification": "差异", "action": "操作", "left_hash": "左端指纹", "right_hash": "右端指纹"}
        widths = {"selected": 55, "path": 300, "classification": 110, "action": 190, "left_hash": 120, "right_hash": 120}
        for column in columns:
            self.generic_tree.heading(column, text=headings[column])
            self.generic_tree.column(column, width=widths[column], stretch=column == "path")
        self.generic_tree.grid(row=7, column=0, columnspan=3, sticky="nsew", pady=(8, 0))
        generic_scrollbar = ttk.Scrollbar(frame, orient="vertical", command=self.generic_tree.yview)
        generic_scrollbar.grid(row=7, column=3, sticky="ns", pady=(8, 0))
        self.generic_tree.configure(yscrollcommand=generic_scrollbar.set)
        self.generic_tree.bind("<Double-1>", self._toggle_generic_item)
        bottom = ttk.Frame(frame)
        bottom.grid(row=8, column=0, columnspan=3, sticky="ew", pady=(8, 0))
        ttk.Button(bottom, text="选择建议项", command=self._select_generic_recommended).pack(side="left")
        ttk.Button(bottom, text="全选", command=lambda: self._select_generic_all(True)).pack(side="left", padx=6)
        ttk.Button(bottom, text="清空", command=lambda: self._select_generic_all(False)).pack(side="left")
        ttk.Entry(bottom, textvariable=self.generic_plan_output, width=55).pack(side="left", padx=(18, 6), fill="x", expand=True)
        ttk.Button(bottom, text="导出计划", command=self._export_generic_plan).pack(side="left")

    def _build_generic_package_tab(self, notebook) -> None:
        frame = ttk.Frame(notebook, padding=18)
        notebook.add(frame, text="B. 制作通用迁移包")
        frame.columnconfigure(1, weight=1)
        self.generic_snapshot = tk.StringVar()
        self.generic_package_plan = tk.StringVar()
        self.generic_package_side = tk.StringVar(value="left")
        self.generic_package_output = tk.StringVar(value=str(Path.home() / "Desktop" / "generic-sync.cdas.zip"))
        self._entry_row(frame, 0, "端点清单", self.generic_snapshot, lambda: self._open_file(self.generic_snapshot, "*.json"))
        self._entry_row(frame, 1, "通用选择计划", self.generic_package_plan, lambda: self._open_file(self.generic_package_plan, "*.json"))
        ttk.Label(frame, text="清单对应位置").grid(row=2, column=0, sticky="w", padx=(0, 10), pady=7)
        ttk.Combobox(frame, textvariable=self.generic_package_side, values=("left", "right"), state="readonly", width=16).grid(row=2, column=1, sticky="w", pady=7)
        self._entry_row(frame, 3, "迁移包输出", self.generic_package_output, lambda: self._save_file(self.generic_package_output, ".zip", "Generic bundle", "*.zip"))
        ttk.Button(frame, text="制作通用迁移包", command=self._create_generic_package).grid(row=4, column=1, sticky="w", pady=(18, 4))
        ttk.Label(frame, text="端点可以是本机另一个代理目录，也可以是另一台电脑上打包前的工作目录。", foreground="#555555").grid(row=5, column=0, columnspan=3, sticky="w", pady=(16, 0))

    def _build_generic_restore_tab(self, notebook) -> None:
        frame = ttk.Frame(notebook, padding=18)
        notebook.add(frame, text="C. 恢复通用迁移包")
        frame.columnconfigure(1, weight=1)
        self.generic_restore_bundle = tk.StringVar()
        self.generic_restore_root = tk.StringVar()
        self._entry_row(frame, 0, "通用迁移包", self.generic_restore_bundle, lambda: self._open_file(self.generic_restore_bundle, "*.zip"))
        self._entry_row(frame, 1, "目标代理/端点目录", self.generic_restore_root, lambda: self._choose_dir(self.generic_restore_root))
        ttk.Button(frame, text="检查迁移包", command=self._preview_generic_restore).grid(row=2, column=1, sticky="w", pady=(12, 4))
        ttk.Button(frame, text="执行通用恢复", command=self._restore_generic).grid(row=3, column=1, sticky="w", pady=(4, 4))
        self.generic_restore_preview = tk.Text(frame, wrap="word", height=20, font=("Consolas", 10))
        self.generic_restore_preview.grid(row=4, column=0, columnspan=3, sticky="nsew", pady=(14, 0))
        frame.rowconfigure(4, weight=1)

    def _entry_row(self, parent, row, label, variable, browse_command=None, width=76):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=(0, 10), pady=7)
        entry = ttk.Entry(parent, textvariable=variable, width=width)
        entry.grid(row=row, column=1, sticky="ew", pady=7)
        if browse_command:
            ttk.Button(parent, text="浏览...", command=browse_command).grid(row=row, column=2, padx=(8, 0), pady=7)
        return entry

    def _build_inventory_tab(self, notebook) -> None:
        frame = ttk.Frame(notebook, padding=18)
        notebook.add(frame, text="1. 生成清单")
        frame.columnconfigure(1, weight=1)
        self.inv_home = tk.StringVar(value=default_codex_home())
        self.inv_device = tk.StringVar(value=os.environ.get("COMPUTERNAME", "computer-a").lower())
        self.inv_output = tk.StringVar(value=str(Path.home() / "Desktop" / "codex-inventory.json"))
        self._entry_row(frame, 0, "Codex 数据目录", self.inv_home, lambda: self._choose_dir(self.inv_home))
        self._entry_row(frame, 1, "电脑标识", self.inv_device)
        self._entry_row(frame, 2, "清单保存位置", self.inv_output, lambda: self._save_file(self.inv_output, ".json", "JSON files", "*.json"))
        ttk.Button(frame, text="生成本机清单", command=self._create_inventory).grid(row=3, column=1, sticky="w", pady=(18, 4))
        ttk.Label(
            frame,
            text="在两台电脑上分别生成清单。清单包含标题、时间和指纹，不包含完整对话正文。",
            foreground="#555555",
        ).grid(row=4, column=0, columnspan=3, sticky="w", pady=(16, 0))

    def _build_compare_tab(self, notebook) -> None:
        frame = ttk.Frame(notebook, padding=14)
        notebook.add(frame, text="2. 对比与选择")
        frame.columnconfigure(1, weight=1)
        frame.rowconfigure(4, weight=1)
        self.left_inventory = tk.StringVar()
        self.right_inventory = tk.StringVar()
        self.compare_direction = tk.StringVar(value="bidirectional")
        self.plan_output = tk.StringVar(value=str(Path.home() / "Desktop" / "codex-merge-plan.json"))
        self._entry_row(frame, 0, "左侧电脑清单", self.left_inventory, lambda: self._open_file(self.left_inventory, "*.json"))
        self._entry_row(frame, 1, "右侧电脑清单", self.right_inventory, lambda: self._open_file(self.right_inventory, "*.json"))
        ttk.Label(frame, text="迁移方向").grid(row=2, column=0, sticky="w", padx=(0, 10), pady=7)
        direction_box = ttk.Combobox(
            frame,
            textvariable=self.compare_direction,
            state="readonly",
            values=("bidirectional", "left-to-right", "right-to-left"),
        )
        direction_box.grid(row=2, column=1, sticky="w", pady=7)
        ttk.Button(frame, text="加载差异", command=self._load_compare).grid(row=2, column=2, padx=(8, 0), pady=7)

        toolbar = ttk.Frame(frame)
        toolbar.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(8, 8))
        ttk.Button(toolbar, text="选择建议项", command=self._select_recommended).pack(side="left")
        ttk.Button(toolbar, text="全选", command=self._select_all).pack(side="left", padx=6)
        ttk.Button(toolbar, text="清空", command=self._select_none).pack(side="left")
        ttk.Label(toolbar, text="双击一行可切换选择").pack(side="right")

        columns = ("selected", "title", "classification", "left", "right", "action")
        self.tree = ttk.Treeview(frame, columns=columns, show="headings", selectmode="browse")
        headings = {
            "selected": "选择", "title": "对话名", "classification": "差异类型",
            "left": "左侧更新时间", "right": "右侧更新时间", "action": "安全操作",
        }
        widths = {"selected": 58, "title": 240, "classification": 130, "left": 155, "right": 155, "action": 190}
        for column in columns:
            self.tree.heading(column, text=headings[column])
            self.tree.column(column, width=widths[column], minwidth=50, stretch=column in {"title", "action"})
        self.tree.grid(row=4, column=0, columnspan=3, sticky="nsew")
        scrollbar = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        scrollbar.grid(row=4, column=3, sticky="ns")
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.bind("<Double-1>", self._toggle_tree_item)

        export = ttk.Frame(frame)
        export.grid(row=5, column=0, columnspan=3, sticky="ew", pady=(10, 0))
        export.columnconfigure(1, weight=1)
        ttk.Label(export, text="计划保存位置").grid(row=0, column=0, padx=(0, 10))
        ttk.Entry(export, textvariable=self.plan_output).grid(row=0, column=1, sticky="ew")
        ttk.Button(export, text="浏览...", command=lambda: self._save_file(self.plan_output, ".json", "JSON files", "*.json")).grid(row=0, column=2, padx=8)
        ttk.Button(export, text="导出选择计划", command=self._export_plan).grid(row=0, column=3)

    def _build_package_tab(self, notebook) -> None:
        frame = ttk.Frame(notebook, padding=18)
        notebook.add(frame, text="3. 制作迁移包")
        frame.columnconfigure(1, weight=1)
        self.package_inventory = tk.StringVar()
        self.package_plan = tk.StringVar()
        self.package_side = tk.StringVar(value="left")
        self.package_output = tk.StringVar(value=str(Path.home() / "Desktop" / "codex-conversations.cdas.zip"))
        self._entry_row(frame, 0, "本机清单", self.package_inventory, lambda: self._open_file(self.package_inventory, "*.json"))
        self._entry_row(frame, 1, "选择计划", self.package_plan, lambda: self._open_file(self.package_plan, "*.json"))
        ttk.Label(frame, text="本机在计划中的位置").grid(row=2, column=0, sticky="w", padx=(0, 10), pady=7)
        ttk.Combobox(frame, textvariable=self.package_side, values=("left", "right"), state="readonly", width=16).grid(row=2, column=1, sticky="w", pady=7)
        self._entry_row(frame, 3, "迁移包保存位置", self.package_output, lambda: self._save_file(self.package_output, ".zip", "Migration bundle", "*.zip"))
        ttk.Button(frame, text="制作选择性迁移包", command=self._create_package).grid(row=4, column=1, sticky="w", pady=(18, 4))
        ttk.Label(
            frame,
            text="迁移包包含所选对话正文和索引元数据，可能涉及隐私，请使用可信渠道传输。",
            foreground="#8a4b00",
        ).grid(row=5, column=0, columnspan=3, sticky="w", pady=(16, 0))

    def _build_restore_tab(self, notebook) -> None:
        frame = ttk.Frame(notebook, padding=18)
        notebook.add(frame, text="4. 预览与恢复")
        frame.columnconfigure(1, weight=1)
        frame.rowconfigure(3, weight=1)
        self.restore_bundle_path = tk.StringVar()
        self.restore_home = tk.StringVar(value=default_codex_home())
        self._entry_row(frame, 0, "迁移包", self.restore_bundle_path, lambda: self._open_file(self.restore_bundle_path, "*.zip"))
        self._entry_row(frame, 1, "目标 Codex 目录", self.restore_home, lambda: self._choose_dir(self.restore_home))
        controls = ttk.Frame(frame)
        controls.grid(row=2, column=0, columnspan=3, sticky="w", pady=(8, 10))
        ttk.Button(controls, text="预览恢复", command=self._preview_restore).pack(side="left")
        ttk.Button(controls, text="执行恢复", command=self._restore).pack(side="left", padx=8)
        self.restore_preview = tk.Text(frame, wrap="word", height=20, font=("Consolas", 10))
        self.restore_preview.grid(row=3, column=0, columnspan=3, sticky="nsew")

    def _choose_dir(self, variable):
        value = filedialog.askdirectory(initialdir=variable.get() or str(Path.home()))
        if value:
            variable.set(value)

    def _open_file(self, variable, pattern):
        value = filedialog.askopenfilename(filetypes=[("Supported files", pattern), ("All files", "*.*")])
        if value:
            variable.set(value)

    def _save_file(self, variable, extension, label, pattern):
        value = filedialog.asksaveasfilename(defaultextension=extension, filetypes=[(label, pattern), ("All files", "*.*")])
        if value:
            variable.set(value)

    def _run(self, label, operation, on_success=None):
        self.status.set(label)
        def worker():
            try:
                result = operation()
                self._post_ui_event(lambda result=result, on_success=on_success: self._finish(result, on_success))
            except Exception as error:
                self._post_ui_event(lambda error=error: self._fail(error))
        threading.Thread(target=worker, daemon=True).start()

    def _finish(self, result, callback):
        self.status.set("完成")
        if callback:
            callback(result)
        else:
            messagebox.showinfo(APP_NAME, json.dumps(result, ensure_ascii=False, indent=2))

    def _fail(self, error):
        self.status.set("失败")
        messagebox.showerror(APP_NAME, str(error))

    def _create_inventory(self):
        def operation():
            result = planner.inventory(Path(self.inv_home.get()), self.inv_device.get().strip())
            planner.write_json(Path(self.inv_output.get()), result)
            return {"清单": self.inv_output.get(), "对话数量": len(result["conversations"])}
        self._run("正在扫描本机 Codex 对话...", operation)

    def _load_compare(self):
        def operation():
            left = planner.load_inventory(Path(self.left_inventory.get()))
            right = planner.load_inventory(Path(self.right_inventory.get()))
            return planner.compare_inventories(left, right, self.compare_direction.get(), set(), set())
        self._run("正在比较两台电脑...", operation, self._show_plan)

    def _show_plan(self, plan):
        self.current_plan = plan
        self.selected_ids = {entry["task_id"] for entry in plan["entries"] if entry["selected"]}
        for item in self.tree.get_children():
            self.tree.delete(item)
        for entry in plan["entries"]:
            left_time = (entry.get("left") or {}).get("updated_at", "")
            right_time = (entry.get("right") or {}).get("updated_at", "")
            self.tree.insert("", "end", iid=entry["task_id"], values=(
                "Yes" if entry["task_id"] in self.selected_ids else "",
                entry["title"], entry["classification"], left_time, right_time, entry["safe_default_action"],
            ))
        self.status.set(f"发现 {len(plan['entries'])} 个会话，已选择 {len(self.selected_ids)} 个")

    def _refresh_marks(self):
        for task_id in self.tree.get_children():
            values = list(self.tree.item(task_id, "values"))
            values[0] = "Yes" if task_id in self.selected_ids else ""
            self.tree.item(task_id, values=values)
        self.status.set(f"已选择 {len(self.selected_ids)} 个会话")

    def _toggle_tree_item(self, _event=None):
        selected = self.tree.selection()
        if not selected:
            return
        task_id = selected[0]
        if task_id in self.selected_ids:
            self.selected_ids.remove(task_id)
        else:
            self.selected_ids.add(task_id)
        self._refresh_marks()

    def _select_recommended(self):
        if self.current_plan:
            self.selected_ids = {
                entry["task_id"] for entry in self.current_plan["entries"]
                if entry["safe_default_action"] not in {"skip", "skip_content_copy", "stop"}
            }
            self._refresh_marks()

    def _select_all(self):
        self.selected_ids = set(self.tree.get_children())
        self._refresh_marks()

    def _select_none(self):
        self.selected_ids.clear()
        self._refresh_marks()

    def _export_plan(self):
        if not self.current_plan:
            messagebox.showwarning(APP_NAME, "请先加载两台电脑的差异。")
            return
        plan = json.loads(json.dumps(self.current_plan))
        for entry in plan["entries"]:
            entry["selected"] = entry["task_id"] in self.selected_ids
        plan["summary"]["selected"] = len(self.selected_ids)
        planner.write_json(Path(self.plan_output.get()), plan)
        self.package_plan.set(self.plan_output.get())
        messagebox.showinfo(APP_NAME, f"选择计划已保存：\n{self.plan_output.get()}")

    def _create_package(self):
        self._run(
            "正在制作迁移包...",
            lambda: migration_bundle.create_bundle(
                Path(self.package_inventory.get()), Path(self.package_plan.get()),
                self.package_side.get(), Path(self.package_output.get()),
            ),
        )

    def _generic_compare(self):
        def operation():
            left = generic_sync.snapshot(Path(self.generic_left_root.get()), self.generic_left_id.get().strip(), self.generic_include.get(), self.generic_exclude.get())
            right = generic_sync.snapshot(Path(self.generic_right_root.get()), self.generic_right_id.get().strip(), self.generic_include.get(), self.generic_exclude.get())
            output = Path(self.generic_plan_output.get()).resolve()
            self.generic_left_snapshot = output.with_name(output.stem + ".left.snapshot.json")
            self.generic_right_snapshot = output.with_name(output.stem + ".right.snapshot.json")
            planner.write_json(self.generic_left_snapshot, left)
            planner.write_json(self.generic_right_snapshot, right)
            return generic_sync.compare(left, right, self.generic_direction.get())
        self._run("正在扫描并比较代理/文件端点...", operation, self._show_generic_plan)

    def _show_generic_plan(self, plan):
        self.generic_plan = plan
        self.generic_selected = {entry["path"] for entry in plan["entries"] if entry["selected"]}
        for item in self.generic_tree.get_children():
            self.generic_tree.delete(item)
        for entry in plan["entries"]:
            self.generic_tree.insert("", "end", iid=entry["path"], values=(
                "Yes" if entry["path"] in self.generic_selected else "",
                entry["path"], entry["classification"], entry["action"],
                (entry.get("left") or {}).get("content_hash", "")[:12],
                (entry.get("right") or {}).get("content_hash", "")[:12],
            ))
        self.status.set(f"通用端点发现 {len(plan['entries'])} 个文件，已选择 {len(self.generic_selected)} 个")

    def _toggle_generic_item(self, _event=None):
        selected = self.generic_tree.selection()
        if not selected:
            return
        path = selected[0]
        if path in self.generic_selected:
            self.generic_selected.remove(path)
        else:
            self.generic_selected.add(path)
        self._refresh_generic_marks()

    def _refresh_generic_marks(self):
        for path in self.generic_tree.get_children():
            values = list(self.generic_tree.item(path, "values"))
            values[0] = "Yes" if path in self.generic_selected else ""
            self.generic_tree.item(path, values=values)
        self.status.set(f"已选择 {len(self.generic_selected)} 个文件")

    def _select_generic_recommended(self):
        if getattr(self, "generic_plan", None):
            self.generic_selected = {entry["path"] for entry in self.generic_plan["entries"] if entry["action"] != "skip"}
            self._refresh_generic_marks()

    def _select_generic_all(self, value):
        self.generic_selected = set(self.generic_tree.get_children()) if value else set()
        self._refresh_generic_marks()

    def _export_generic_plan(self):
        if not getattr(self, "generic_plan", None):
            messagebox.showwarning(APP_NAME, "请先扫描并比较两个端点。")
            return
        plan = json.loads(json.dumps(self.generic_plan))
        for entry in plan["entries"]:
            entry["selected"] = entry["path"] in self.generic_selected
        plan["summary"]["selected"] = len(self.generic_selected)
        planner.write_json(Path(self.generic_plan_output.get()), plan)
        self.generic_package_plan.set(self.generic_plan_output.get())
        self.generic_snapshot.set(str(getattr(self, "generic_left_snapshot", "")))
        messagebox.showinfo(APP_NAME, f"通用选择计划已保存：\n{self.generic_plan_output.get()}\n\n端点清单也已保存到计划旁边。")

    def _create_generic_package(self):
        self._run("正在制作通用迁移包...", lambda: generic_sync.create_bundle(
            Path(self.generic_snapshot.get()), Path(self.generic_package_plan.get()),
            self.generic_package_side.get(), Path(self.generic_package_output.get()),
        ))

    def _preview_generic_restore(self):
        def operation():
            manifest, payloads = generic_sync.inspect_bundle(Path(self.generic_restore_bundle.get()))
            return {"bundle_id": manifest["bundle_id"], "source": manifest["source_endpoint_id"], "files": [item["path"] for item in manifest["files"]], "payloads": len(payloads)}
        def show(result):
            self.generic_restore_preview.delete("1.0", "end")
            self.generic_restore_preview.insert("1.0", json.dumps(result, ensure_ascii=False, indent=2))
        self._run("正在校验通用迁移包...", operation, show)

    def _restore_generic(self):
        if not messagebox.askyesno(APP_NAME, "确认恢复通用文件？目标目录中的同名冲突文件会被保留，并为新文件生成 .from-... 副本。\n\n继续？"):
            return
        self._run("正在备份并恢复通用文件...", lambda: generic_sync.restore_bundle(
            Path(self.generic_restore_bundle.get()), Path(self.generic_restore_root.get()), require_empty_lock=True
        ))

    def _preview_restore(self):
        def operation():
            prepared = migration_bundle.prepare_restore(Path(self.restore_bundle_path.get()), Path(self.restore_home.get()))
            return [{key: operation[key] for key in ("title", "source_task_id", "target_task_id", "action", "target_path")} for operation in prepared["operations"]]
        def show(result):
            self.restore_preview.delete("1.0", "end")
            self.restore_preview.insert("1.0", json.dumps(result, ensure_ascii=False, indent=2))
        self._run("正在校验迁移包并生成恢复预览...", operation, show)

    def _restore(self):
        if not messagebox.askyesno(
            APP_NAME,
            "恢复前必须完全关闭 Codex。工具会自动备份目标会话、索引和数据库。\n\n确认继续？",
        ):
            return
        self._run(
            "正在备份并恢复，请勿关闭工具...",
            lambda: migration_bundle.restore_bundle(
                Path(self.restore_bundle_path.get()), Path(self.restore_home.get()), require_codex_closed=True
            ),
        )


def main() -> int:
    if "--self-test" in sys.argv:
        print(json.dumps({"name": APP_NAME, "version": APP_VERSION, "ok": True}))
        return 0
    app = AdvancedApp()
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
