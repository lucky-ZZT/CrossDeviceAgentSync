#!/usr/bin/env python3
"""Simple Windows workflow for agent, computer, and custom-file synchronization."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import traceback
import tkinter as tk
import webbrowser
import zipfile
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import app_diagnostics
import app_release_checker
import cross_device_agent_sync_gui as advanced_gui
import generic_sync
import local_provider_sync
import migration_bundle
import session_merge_planner as planner


APP_NAME = "代理与电脑同步工具"
APP_VERSION = "1.0.3"


class SimpleApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(f"{APP_NAME} {APP_VERSION}")
        self.geometry("900x650")
        self.minsize(850, 560)
        self.option_add("*Font", ("Microsoft YaHei UI", 10))
        self.diagnostics = app_diagnostics.AppDiagnostics(APP_NAME, APP_VERSION)
        self.status = tk.StringVar(value="请选择要做的事情")
        self.content = ttk.Frame(self, padding=20)
        self.content.pack(fill="both", expand=True)
        ttk.Separator(self).pack(fill="x", padx=14)
        ttk.Label(self, textvariable=self.status, anchor="w").pack(fill="x", padx=18, pady=9)
        self.current_kind = "generic"
        self.current_left = None
        self.current_right = None
        self.current_plan = None
        self.selected = set()
        self.agent_by_label = {}
        self.latest_release = None
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.show_home()

    def on_close(self):
        self.diagnostics.event("application_exit")
        self.diagnostics.close()
        self.destroy()

    def clear(self):
        for child in self.content.winfo_children():
            child.destroy()

    def title_block(self, title, subtitle=""):
        ttk.Label(self.content, text=title, font=("Microsoft YaHei UI", 18, "bold")).pack(anchor="w", pady=(0, 4))
        if subtitle:
            ttk.Label(self.content, text=subtitle, foreground="#555555").pack(anchor="w", pady=(0, 18))

    def show_home(self):
        self.clear()
        self.title_block("你想同步什么？", "选择一种方式，后续只需要指定位置并确认内容。")
        actions = (
            ("同步本机不同 Provider", "自动识别 Provider，可切换归属或保留一份副本。", self.show_local_agents),
            ("迁移到另一台电脑", "在旧电脑导出一个文件，在新电脑导入。", self.show_transfer),
            ("同步指定文件夹", "选择两个文件夹，检查差异后同步所选文件。", self.show_custom_files),
            ("备份与恢复", "查看自动备份，并把 Codex 恢复到某次同步前。", self.show_backups),
            ("检查更新", "检查本软件在 GitHub Releases 发布的最新正式版本。", self.show_updates),
        )
        for title, description, command in actions:
            row = ttk.Frame(self.content)
            row.pack(fill="x", pady=8)
            button = ttk.Button(row, text=title, command=command, width=24)
            button.pack(side="left", ipady=10)
            ttk.Label(row, text=description).pack(side="left", padx=18)
        ttk.Separator(self.content).pack(fill="x", pady=(28, 14))
        footer = ttk.Frame(self.content)
        footer.pack(fill="x")
        ttk.Button(footer, text="高级模式", command=self.open_advanced).pack(side="left")
        ttk.Button(footer, text="打开软件日志", command=self.open_log_folder).pack(side="left", padx=8)

    def nav(self):
        bar = ttk.Frame(self.content)
        bar.pack(fill="x", pady=(0, 14))
        ttk.Button(bar, text="返回", command=self.show_home).pack(side="left")

    def show_updates(self):
        self.clear()
        self.nav()
        self.title_block("检查更新", "只检查本软件的 GitHub Release，不会自动下载或安装。")
        controls = ttk.Frame(self.content)
        controls.pack(fill="x", pady=(0, 10))
        self.release_check_button = ttk.Button(
            controls, text="检查新版本", command=self.check_app_release
        )
        self.release_check_button.pack(side="left")
        self.release_page_button = ttk.Button(
            controls,
            text="打开 Release 页面",
            command=self.open_release_page,
            state="normal" if app_release_checker.is_configured() else "disabled",
        )
        self.release_page_button.pack(side="left", padx=8)
        self.release_progress = ttk.Progressbar(controls, mode="indeterminate", length=130)
        self.release_progress.pack(side="left", padx=(12, 0))
        self.release_progress.pack_forget()
        detail_frame = ttk.Frame(self.content)
        detail_frame.pack(fill="both", expand=True, pady=(10, 0))
        self.release_detail = tk.Text(
            detail_frame,
            wrap="word",
            font=("Microsoft YaHei UI", 10),
            padx=8,
            pady=8,
        )
        detail_scroll = ttk.Scrollbar(detail_frame, orient="vertical", command=self.release_detail.yview)
        self.release_detail.configure(yscrollcommand=detail_scroll.set)
        self.release_detail.pack(side="left", fill="both", expand=True)
        detail_scroll.pack(side="right", fill="y")
        self.set_release_detail(f"当前版本：{APP_VERSION}\n\n点击“检查新版本”连接 GitHub Releases。")

    def set_release_detail(self, text):
        self.release_detail.configure(state="normal")
        self.release_detail.delete("1.0", "end")
        self.release_detail.insert("1.0", text)
        self.release_detail.configure(state="disabled")

    def check_app_release(self):
        if getattr(self, "release_checking", False):
            return
        self.release_checking = True
        self.release_check_button.configure(state="disabled")
        self.release_progress.pack(side="left", padx=(12, 0))
        self.release_progress.start(12)
        self.set_release_detail("正在读取本项目最新正式 Release，请稍候...")

        def complete(release):
            self.release_checking = False
            self.release_check_button.configure(state="normal")
            self.release_progress.stop()
            self.release_progress.pack_forget()
            self.latest_release = release
            self.release_page_button.configure(state="normal")
            status = "发现新版本" if release["update_available"] else "当前已是最新版本"
            notes = release["release_notes"].strip() or "该 Release 没有更新说明。"
            assets = "\n".join(
                f"- {item['name']}（{self.format_size(item['size'])}）"
                for item in release["assets"]
            ) or "- 无附件"
            self.set_release_detail(
                f"{status}\n\n"
                f"当前版本：{release['current_version']}\n"
                f"最新版本：{release['latest_version']}\n"
                f"发布时间：{release['published_at'] or '未知'}\n"
                f"发布页面：{release['release_url']}\n\n"
                f"更新说明\n{notes}\n\n发布附件\n{assets}\n\n"
                "软件不会自动下载或安装。需要更新时，请打开 Release 页面并手动获取新版。"
            )
            self.status.set(status)

        self.run(
            "正在检查本软件的新版本...",
            lambda: app_release_checker.check_latest_release(APP_VERSION),
            complete,
        )

    def open_release_page(self):
        url = self.latest_release["release_url"] if self.latest_release else app_release_checker.release_page_url()
        webbrowser.open(url)

    def path_row(self, parent, label, variable, save=False):
        row = ttk.Frame(parent)
        row.pack(fill="x", pady=7)
        ttk.Label(row, text=label, width=14).pack(side="left")
        ttk.Entry(row, textvariable=variable).pack(side="left", fill="x", expand=True)
        command = lambda: self.choose_save(variable) if save else lambda: None
        if save:
            ttk.Button(row, text="选择...", command=lambda: self.choose_save(variable)).pack(side="left", padx=(8, 0))
        else:
            ttk.Button(row, text="选择...", command=lambda: self.choose_dir(variable)).pack(side="left", padx=(8, 0))

    def show_local_agents(self):
        self.clear()
        self.nav()
        self.title_block("同步本机不同 Provider", "程序从配置、会话文件和 SQLite 自动识别 Provider。")
        self.local_codex_home = tk.StringVar(value=str(Path.home() / ".codex"))
        self.source_agent = tk.StringVar()
        self.target_agent = tk.StringVar()
        self.provider_mode = tk.StringVar(value="reassign")
        self.provider_full_backup = tk.BooleanVar(value=True)
        self.local_thread_by_id = {}
        self.path_row(self.content, "Codex 数据位置", self.local_codex_home)
        backup_panel = ttk.LabelFrame(self.content, text="备份与恢复", padding=10)
        backup_panel.pack(fill="x", pady=(4, 10))
        self.backup_summary = tk.StringVar(value="正在检查备份...")
        ttk.Label(backup_panel, textvariable=self.backup_summary, foreground="#9A5B00").pack(side="left", fill="x", expand=True)
        ttk.Button(backup_panel, text="打开备份目录", command=self.open_backup_folder).pack(side="left", padx=6)
        ttk.Button(backup_panel, text="从备份恢复...", command=lambda: self.show_backups(self.local_codex_home.get())).pack(side="left")
        agent_row = ttk.Frame(self.content)
        agent_row.pack(fill="x", pady=8)
        ttk.Label(agent_row, text="来源 Provider", width=14).pack(side="left")
        self.source_agent_box = ttk.Combobox(agent_row, textvariable=self.source_agent, state="readonly", width=24)
        self.source_agent_box.pack(side="left")
        ttk.Label(agent_row, text="目标 Provider", width=12).pack(side="left", padx=(24, 0))
        self.target_agent_box = ttk.Combobox(agent_row, textvariable=self.target_agent, state="normal", width=24)
        self.target_agent_box.pack(side="left")

        mode_panel = ttk.LabelFrame(self.content, text="处理方式", padding=8)
        mode_panel.pack(fill="x", pady=(4, 6))
        mode_options = ttk.Frame(mode_panel)
        mode_options.pack(fill="x")
        ttk.Radiobutton(
            mode_options,
            text="切换归属（不新增会话，推荐）",
            variable=self.provider_mode,
            value="reassign",
            command=self.update_provider_mode,
        ).pack(side="left")
        ttk.Radiobutton(
            mode_options,
            text="创建副本（保留原 Provider 会话）",
            variable=self.provider_mode,
            value="clone",
            command=self.update_provider_mode,
        ).pack(side="left", padx=(18, 0))
        self.provider_backup_check = ttk.Checkbutton(
            mode_panel,
            text="切换归属前完整备份所选会话数据",
            variable=self.provider_full_backup,
        )
        self.provider_backup_check.pack(anchor="w", pady=(6, 0))
        controls = ttk.Frame(self.content)
        controls.pack(fill="x", pady=(8, 8))
        ttk.Button(controls, text="自动识别 Provider", command=self.load_local_agents).pack(side="left")
        ttk.Button(controls, text="显示来源对话", command=self.load_source_threads).pack(side="left", padx=8)
        self.provider_preflight_button = ttk.Button(
            controls, text="执行前检查", command=self.check_provider_ready, state="disabled"
        )
        self.provider_preflight_button.pack(side="left")
        ttk.Button(controls, text="全选", command=self.select_all_provider_threads).pack(side="left")
        ttk.Button(controls, text="全不选", command=self.clear_provider_threads).pack(side="left", padx=(8, 0))
        ttk.Button(controls, text="反选", command=self.invert_provider_threads).pack(side="left", padx=8)
        ttk.Label(controls, text="点击选择框或双击会话可切换选择").pack(side="right")
        execute_bar = ttk.Frame(self.content, padding=(0, 2, 0, 8))
        execute_bar.pack(fill="x")
        self.provider_selection_summary = tk.StringVar(value="请先显示来源对话")
        ttk.Label(execute_bar, textvariable=self.provider_selection_summary).pack(side="left")
        self.provider_execute_button = ttk.Button(
            execute_bar,
            text="开始切换归属",
            command=self.handoff_local_threads,
            state="disabled",
            width=24,
        )
        self.provider_execute_button.pack(side="right", ipady=7)
        self.build_result_tree(self.content)
        self.refresh_backup_summary()
        self.load_local_agents()

    def update_provider_mode(self):
        mode = self.provider_mode.get()
        self.provider_execute_button.configure(
            text="开始切换归属" if mode == "reassign" else "开始创建副本"
        )
        self.provider_backup_check.state(["!disabled"] if mode == "reassign" else ["disabled"])
        action = "原地切换 Provider 归属" if mode == "reassign" else "流式复制给目标 Provider"
        if hasattr(self, "tree") and self.tree.winfo_exists():
            for task_id in self.tree.get_children():
                values = list(self.tree.item(task_id, "values"))
                if len(values) >= 4:
                    values[3] = action
                    self.tree.item(task_id, values=values)

    def load_local_agents(self):
        codex_home = Path(self.local_codex_home.get())
        def operation():
            return local_provider_sync.discover_providers(codex_home)
        def show(providers):
            self.local_agents = providers
            labels = [f"{provider['id']} ({provider['sqlite_count']} 个对话)" for provider in providers]
            self.agent_by_label = dict(zip(labels, providers))
            self.source_agent_box["values"] = labels
            self.target_agent_box["values"] = [provider["id"] for provider in providers]
            if labels:
                self.source_agent.set(labels[0])
                self.target_agent.set(providers[1]["id"] if len(providers) > 1 else "custom")
            if providers:
                self.status.set(f"自动识别到 {len(providers)} 个 Provider；也可以手动输入目标 Provider")
            else:
                self.status.set("未识别到 Provider；请检查 Codex 数据位置，目标 Provider 仍可手动输入")
        self.run("正在从配置、Rollout 和 SQLite 识别 Provider...", operation, show)

    def load_source_threads(self):
        source = self.agent_by_label.get(self.source_agent.get())
        if not source:
            messagebox.showwarning(APP_NAME, "请先读取并选择来源代理。")
            return
        def operation():
            return local_provider_sync.list_provider_threads(Path(self.local_codex_home.get()), source["id"])
        def show(threads):
            self.local_threads = threads
            self.local_thread_by_id = {thread["id"]: thread for thread in threads}
            self.selected = {thread["id"] for thread in threads}
            for item in self.tree.get_children():
                self.tree.delete(item)
            for thread in threads:
                size = self.format_size(thread["size_bytes"])
                action = "原地切换 Provider 归属" if self.provider_mode.get() == "reassign" else "流式复制给目标 Provider"
                self.tree.insert("", "end", iid=thread["id"], values=("☑", thread["title"], size, action))
            total_size = sum(thread["size_bytes"] for thread in threads)
            unavailable = max(0, int(source.get("sqlite_count", 0)) - len(threads))
            self.provider_execute_button.configure(state="normal" if threads else "disabled")
            self.provider_preflight_button.configure(state="normal" if threads else "disabled")
            self.update_provider_selection_summary()
            unavailable_note = f"；另有 {unavailable} 条缺失文件记录已忽略" if unavailable else ""
            self.status.set(
                f"来源 Provider 共有 {len(threads)} 个可处理对话，共 {self.format_size(total_size)}"
                f"{unavailable_note}"
            )
        self.run("正在读取来源 Provider 的对话...", operation, show)

    def update_provider_selection_summary(self):
        if not hasattr(self, "provider_selection_summary"):
            return
        selected_bytes = sum(
            self.local_thread_by_id[task_id]["size_bytes"]
            for task_id in self.selected
            if task_id in self.local_thread_by_id
        )
        self.provider_selection_summary.set(
            f"已选择 {len(self.selected)} 个会话，共 {self.format_size(selected_bytes)}"
        )

    def select_all_provider_threads(self):
        if not hasattr(self, "local_thread_by_id"):
            return
        self.selected = set(self.local_thread_by_id)
        for task_id in self.tree.get_children():
            values = list(self.tree.item(task_id, "values"))
            values[0] = "☑"
            self.tree.item(task_id, values=values)
        self.update_provider_selection_summary()

    def clear_provider_threads(self):
        self.selected.clear()
        for task_id in self.tree.get_children():
            values = list(self.tree.item(task_id, "values"))
            values[0] = "☐"
            self.tree.item(task_id, values=values)
        self.update_provider_selection_summary()

    def invert_provider_threads(self):
        self.selected = set(self.local_thread_by_id) - self.selected
        for task_id in self.tree.get_children():
            values = list(self.tree.item(task_id, "values"))
            values[0] = "☑" if task_id in self.selected else "☐"
            self.tree.item(task_id, values=values)
        self.update_provider_selection_summary()

    def check_provider_ready(self):
        source = self.agent_by_label.get(self.source_agent.get())
        target_provider = self.target_agent.get().strip()
        if not source or not target_provider or not self.selected:
            messagebox.showwarning(APP_NAME, "请先选择来源、目标并至少选择一个会话。")
            return
        mode = self.provider_mode.get()
        def operation():
            report = local_provider_sync.preflight_provider_operation(
                Path(self.local_codex_home.get()),
                source["id"],
                target_provider,
                set(self.selected),
                operation="reassign" if mode == "reassign" else "clone",
                create_backup=self.provider_full_backup.get() if mode == "reassign" else True,
                require_codex_closed=False,
            )
            if not report["ok"]:
                raise local_provider_sync.ProviderPreflightError(report)
            return report
        def complete(report):
            text = (
                "执行前检查通过\n\n"
                f"会话：{report['selected_count']} 个\n"
                f"数据量：{self.format_size(report['selected_bytes'])}\n"
                f"预计所需可用空间：{self.format_size(report['required_bytes'])}\n"
                f"当前可用空间：{self.format_size(report['free_bytes'])}\n\n"
                "路径、会话元数据、SQLite、权限和磁盘空间均通过。\n"
                "正式执行前仍会重新检查，并要求 Codex 已完全关闭。"
            )
            self.show_report_window("执行前检查结果", text, success=True)
        self.run("正在检查全部所选会话、SQLite 和磁盘空间...", operation, complete)

    def show_report_window(self, title, text, success=False):
        window = tk.Toplevel(self)
        window.title(title)
        window.transient(self)
        window.geometry("680x460")
        window.minsize(560, 360)
        frame = ttk.Frame(window, padding=16)
        frame.pack(fill="both", expand=True)
        ttk.Label(
            frame,
            text="检查通过" if success else title,
            font=("Microsoft YaHei UI", 14, "bold"),
            foreground="#176B3A" if success else "#333333",
        ).pack(anchor="w", pady=(0, 10))
        text_frame = ttk.Frame(frame)
        text_frame.pack(fill="both", expand=True)
        viewer = tk.Text(text_frame, wrap="word", font=("Microsoft YaHei UI", 10), padx=8, pady=8)
        scrollbar = ttk.Scrollbar(text_frame, orient="vertical", command=viewer.yview)
        viewer.configure(yscrollcommand=scrollbar.set)
        viewer.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        viewer.insert("1.0", text)
        viewer.configure(state="disabled")
        buttons = ttk.Frame(frame)
        buttons.pack(fill="x", pady=(12, 0))
        ttk.Button(buttons, text="复制结果", command=lambda: self.copy_text(text)).pack(side="left")
        ttk.Button(buttons, text="打开软件日志", command=self.open_log_folder).pack(side="left", padx=8)
        ttk.Button(buttons, text="关闭", command=window.destroy).pack(side="right")
        window.lift()
        window.focus_force()

    def copy_text(self, text):
        self.clipboard_clear()
        self.clipboard_append(text)
        self.update_idletasks()
        self.status.set("内容已复制到剪贴板")

    def open_log_folder(self):
        root = self.diagnostics.root
        root.mkdir(parents=True, exist_ok=True)
        if os.name == "nt":
            os.startfile(root)
        else:
            messagebox.showinfo(APP_NAME, str(root))

    def start_provider_progress(self):
        steps = (
            ("preflight", "1. 检查 Codex 和数据文件"),
            ("backup", "2. 创建备份或临时回滚点"),
            ("rollouts", "3. 切换会话文件归属"),
            ("database", "4. 更新 SQLite 记录"),
            ("verify", "5. 验证切换结果"),
        )
        window = tk.Toplevel(self)
        window.title("Provider 切换进度")
        window.transient(self)
        window.resizable(False, False)
        window.protocol("WM_DELETE_WINDOW", lambda: None)
        frame = ttk.Frame(window, padding=18)
        frame.pack(fill="both", expand=True)
        ttk.Label(
            frame,
            text="正在切换 Provider 归属",
            font=("Microsoft YaHei UI", 14, "bold"),
        ).pack(anchor="w")
        ttk.Label(frame, text="操作完成前请勿启动 Codex。", foreground="#9A5B00").pack(
            anchor="w", pady=(2, 12)
        )
        labels = {}
        for key, text in steps:
            label = ttk.Label(frame, text=f"等待  {text}")
            label.pack(anchor="w", pady=2)
            labels[key] = label
        detail = tk.StringVar(value="正在准备...")
        ttk.Separator(frame).pack(fill="x", pady=(12, 8))
        ttk.Label(frame, textvariable=detail, wraplength=520).pack(anchor="w", fill="x")
        progressbar = ttk.Progressbar(frame, mode="indeterminate", length=520)
        progressbar.pack(fill="x", pady=(10, 0))
        progressbar.start(12)
        window.update_idletasks()
        x = self.winfo_rootx() + max(0, (self.winfo_width() - window.winfo_reqwidth()) // 2)
        y = self.winfo_rooty() + max(0, (self.winfo_height() - window.winfo_reqheight()) // 2)
        window.geometry(f"+{x}+{y}")
        window.grab_set()
        return {
            "window": window,
            "steps": steps,
            "labels": labels,
            "detail": detail,
            "progressbar": progressbar,
        }

    def update_provider_progress(self, flow, stage, detail):
        window = flow["window"]
        if not window.winfo_exists():
            return
        order = [key for key, _ in flow["steps"]]
        current_index = len(order) if stage == "complete" else order.index(stage)
        for index, (key, text) in enumerate(flow["steps"]):
            if index < current_index:
                prefix = "完成"
            elif index == current_index:
                prefix = "进行中"
            else:
                prefix = "等待"
            flow["labels"][key].configure(text=f"{prefix}  {text}")
        flow["detail"].set(detail)
        self.status.set(detail)

    @staticmethod
    def close_progress_flow(flow):
        if not flow:
            return
        window = flow["window"]
        if window.winfo_exists():
            flow["progressbar"].stop()
            window.grab_release()
            window.destroy()

    def handoff_local_threads(self):
        source = self.agent_by_label.get(self.source_agent.get())
        target_provider = self.target_agent.get().strip()
        if not source or not target_provider:
            messagebox.showwarning(APP_NAME, "请选择来源 Provider 并填写目标 Provider。")
            return
        if source["id"] == target_provider:
            messagebox.showwarning(APP_NAME, "来源 Provider 和目标 Provider 不能相同。")
            return
        if not self.selected:
            messagebox.showwarning(APP_NAME, "请至少选择一个对话。")
            return
        selected_bytes = sum(
            self.local_thread_by_id[task_id]["size_bytes"]
            for task_id in self.selected
            if task_id in self.local_thread_by_id
        )
        largest_selected = max(
            (
                self.local_thread_by_id[task_id]["size_bytes"]
                for task_id in self.selected
                if task_id in self.local_thread_by_id
            ),
            default=0,
        )
        mode = self.provider_mode.get()
        if mode == "reassign":
            if self.provider_full_backup.get():
                backup_note = (
                    "将完整备份所选会话文件和 SQLite，可从软件恢复。\n"
                    f"备份体积预计约 {self.format_size(selected_bytes)}，取决于数据库大小。\n"
                    f"执行中还需最多约 {self.format_size(largest_selected)} 的临时空间。"
                )
            else:
                backup_note = (
                    "未选择长期备份。成功后无法从软件恢复这次切换。\n"
                    f"执行中仍需最多约 {self.format_size(largest_selected)} 的临时空间。"
                )
            confirmation = (
                "切换前必须完全关闭 Codex。会话 ID 和内容保持不变，原 Provider 将不再显示这些会话。\n\n"
                f"本次只切换选中的 {len(self.selected)} 个对话，共 {self.format_size(selected_bytes)}。\n\n"
                f"{backup_note}\n\n确认继续？"
            )
        else:
            confirmation = (
                "复制前必须完全关闭 Codex。原对话会保留，目标 Provider 会得到一个完整副本。\n\n"
                f"本次只处理选中的 {len(self.selected)} 个对话，共 {self.format_size(selected_bytes)}。\n\n"
                "创建副本会增加相近大小的磁盘占用，并自动创建恢复备份。\n\n确认继续？"
            )
        if not messagebox.askyesno(APP_NAME, confirmation):
            return
        def complete(result):
            warning = f"\n注意：{result['encrypted_content_warnings']} 个会话含加密内容，换 Provider 后可能无法继续。" if result["encrypted_content_warnings"] else ""
            self.refresh_backup_summary()
            processed = (
                f"本次只处理了 {result['scanned_conversations']} 个选中对话"
                f"（{self.format_size(result['scanned_bytes'])}，{result['duration_seconds']:.1f} 秒）。"
            )
            if mode == "reassign":
                backup_text = (
                    f"\n\n完整备份：\n{result['backup_path']}"
                    if result["backup_created"]
                    else "\n\n未保留长期备份。"
                )
                messagebox.showinfo(APP_NAME, f"归属切换完成。\n{processed}{backup_text}{warning}")
                self.load_local_agents()
            else:
                messagebox.showinfo(APP_NAME, f"复制完成。\n{processed}\n\n已自动创建可恢复备份：\n{result['backup_path']}{warning}")
        if mode == "reassign":
            self.provider_execute_button.configure(state="disabled")
            progress_flow = self.start_provider_progress()
            def report_progress(stage, detail):
                self.after(
                    0,
                    lambda stage=stage, detail=detail: self.update_provider_progress(
                        progress_flow, stage, detail
                    ),
                )
            self.run(
                "正在切换所选会话归属...",
                lambda: local_provider_sync.reassign_provider(
                    Path(self.local_codex_home.get()),
                    source["id"],
                    target_provider,
                    set(self.selected),
                    True,
                    self.provider_full_backup.get(),
                    report_progress,
                ),
                complete,
                progress_flow,
            )
        else:
            self.run("正在备份并复制到目标 Provider...", lambda: local_provider_sync.clone_to_provider(
                Path(self.local_codex_home.get()), source["id"], target_provider, set(self.selected), True
            ), complete)

    @staticmethod
    def format_size(size):
        value = float(size)
        for unit in ("B", "KB", "MB", "GB"):
            if value < 1024 or unit == "GB":
                return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
            value /= 1024

    def refresh_backup_summary(self):
        if not hasattr(self, "backup_summary"):
            return
        backups = migration_bundle.list_backups(Path(self.local_codex_home.get()))
        restorable = [item for item in backups if item["restorable"]]
        if not backups:
            self.backup_summary.set("尚无备份。执行一次带备份的操作后会在这里显示。")
        elif restorable:
            latest = restorable[0]
            self.backup_summary.set(f"可恢复备份：{len(restorable)} 个    最近：{latest['name']}")
        else:
            self.backup_summary.set(f"发现 {len(backups)} 个旧版备份，但缺少自动恢复信息。")

    def open_backup_folder(self):
        if hasattr(self, "backup_tree") and self.backup_tree.winfo_exists():
            codex_home = Path(self.backup_codex_home.get())
        elif hasattr(self, "local_codex_home"):
            codex_home = Path(self.local_codex_home.get())
        else:
            codex_home = Path.home() / ".codex"
        root = migration_bundle.backup_root_for(codex_home)
        root.mkdir(parents=True, exist_ok=True)
        if os.name == "nt":
            os.startfile(root)
        else:
            messagebox.showinfo(APP_NAME, str(root))

    def show_backups(self, codex_home=None):
        initial_home = codex_home or str(Path.home() / ".codex")
        self.clear()
        self.nav()
        self.title_block("备份与恢复", "这里显示已保留的完整备份；恢复操作前还会再建立一份保护备份。")
        self.backup_codex_home = tk.StringVar(value=initial_home)
        self.path_row(self.content, "Codex 数据位置", self.backup_codex_home)
        controls = ttk.Frame(self.content)
        controls.pack(fill="x", pady=(8, 8))
        ttk.Button(controls, text="刷新备份列表", command=self.load_backups).pack(side="left")
        ttk.Button(controls, text="打开备份目录", command=self.open_backup_folder).pack(side="left", padx=8)
        ttk.Button(controls, text="恢复选中的备份", command=self.restore_selected_backup).pack(side="left")
        ttk.Label(controls, text="恢复前必须完全关闭 Codex", foreground="#9A5B00").pack(side="right")
        columns = ("time", "operation", "items", "size", "path")
        self.backup_tree = ttk.Treeview(self.content, columns=columns, show="headings", selectmode="browse")
        for column, title, width in (
            ("time", "备份时间", 150),
            ("operation", "类型", 135),
            ("items", "涉及项目", 75),
            ("size", "大小", 70),
            ("path", "备份文件夹", 350),
        ):
            self.backup_tree.heading(column, text=title)
            self.backup_tree.column(column, width=width, stretch=column == "path")
        self.backup_tree.pack(fill="both", expand=True)
        self.backup_records = {}
        self.backup_detail = tk.StringVar(value="选择一条备份后可恢复。")
        ttk.Label(self.content, textvariable=self.backup_detail, foreground="#555555").pack(anchor="w", pady=(8, 0))
        self.backup_tree.bind("<<TreeviewSelect>>", self.show_backup_detail)
        self.load_backups()

    def load_backups(self):
        backups = migration_bundle.list_backups(Path(self.backup_codex_home.get()))
        self.backup_records = {}
        for item in self.backup_tree.get_children():
            self.backup_tree.delete(item)
        labels = {
            "sync": "同步前备份",
            "provider_clone": "Provider 复制前备份",
            "provider_reassign": "Provider 切换前完整备份",
            "restore_guard": "恢复前保护备份",
            "legacy": "旧版备份",
        }
        for index, backup in enumerate(backups):
            key = f"backup-{index}"
            self.backup_records[key] = backup
            created = backup["created_at"].replace("T", " ")[:19] or backup["name"][:15]
            operation = labels.get(backup["operation"], backup["operation"])
            if not backup["restorable"]:
                operation += "（不可自动恢复）"
            self.backup_tree.insert("", "end", iid=key, values=(
                created, operation, backup["item_count"], self.format_size(backup["size"]), backup["path"]
            ))
        self.status.set(f"找到 {len(backups)} 个备份")
        self.backup_detail.set("没有备份。完成一次带备份的 Provider 操作或 Codex 对话导入后会自动生成。" if not backups else "请选择要查看或恢复的备份。")

    def show_backup_detail(self, _event=None):
        selection = self.backup_tree.selection()
        if not selection:
            return
        backup = self.backup_records[selection[0]]
        state = "可以恢复" if backup["restorable"] else "旧版备份缺少恢复清单，只能手动查看"
        self.backup_detail.set(f"{state}    完整位置：{backup['path']}")

    def restore_selected_backup(self):
        selection = self.backup_tree.selection()
        if not selection:
            messagebox.showwarning(APP_NAME, "请先在列表中选择一个备份。")
            return
        backup = self.backup_records[selection[0]]
        if not backup["restorable"]:
            messagebox.showwarning(APP_NAME, "这个旧版备份缺少完整恢复清单，不能自动恢复。可以打开备份目录手动检查文件。")
            return
        prompt = (
            "恢复前必须完全关闭 Codex。\n\n"
            f"将恢复到这个备份保存的状态：\n{backup['path']}\n\n"
            "程序会先备份当前状态，因此本次恢复也可以撤销。确认继续？"
        )
        if not messagebox.askyesno(APP_NAME, prompt):
            return
        def complete(result):
            self.load_backups()
            messagebox.showinfo(
                APP_NAME,
                f"恢复完成。\n恢复了 {result['restored']} 个文件，移除了 {result['removed']} 个同步新增文件。\n\n"
                f"恢复前的当前状态已备份到：\n{result['safety_backup_path']}",
            )
        self.run(
            "正在创建保护备份并恢复...",
            lambda: migration_bundle.restore_backup(
                Path(backup["path"]), Path(self.backup_codex_home.get()), require_codex_closed=True
            ),
            complete,
        )

    def show_custom_files(self):
        self.show_compare_page("同步指定文件夹", "选择来源和目标文件夹；冲突文件会同时保留。", codex_option=False)

    def show_compare_page(self, title, subtitle, codex_option):
        self.clear()
        self.nav()
        self.title_block(title, subtitle)
        self.left_path = tk.StringVar(value=str(Path.home() / ".codex") if codex_option else "")
        self.right_path = tk.StringVar()
        self.sync_type = tk.StringVar(value="codex" if codex_option else "generic")
        if codex_option:
            modes = ttk.Frame(self.content)
            modes.pack(fill="x", pady=(0, 8))
            ttk.Radiobutton(modes, text="Codex 对话", variable=self.sync_type, value="codex").pack(side="left")
            ttk.Radiobutton(modes, text="代理工作文件", variable=self.sync_type, value="generic").pack(side="left", padx=20)
        self.path_row(self.content, "第一个代理", self.left_path)
        self.path_row(self.content, "第二个代理", self.right_path)
        buttons = ttk.Frame(self.content)
        buttons.pack(fill="x", pady=(12, 8))
        ttk.Button(buttons, text="检查差异", command=self.scan_compare).pack(side="left")
        ttk.Button(buttons, text="同步所选内容", command=self.sync_selected).pack(side="left", padx=8)
        ttk.Label(buttons, text="双击列表可取消或重新选择").pack(side="right")
        self.build_result_tree(self.content)

    def build_result_tree(self, parent):
        columns = ("selected", "name", "difference", "operation")
        container = ttk.Frame(parent)
        container.pack(fill="both", expand=True, pady=(4, 0))
        self.tree = ttk.Treeview(container, columns=columns, show="headings", selectmode="browse")
        vertical = ttk.Scrollbar(container, orient="vertical", command=self.tree.yview)
        horizontal = ttk.Scrollbar(container, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        for column, text, width in (
            ("selected", "同步", 65), ("name", "内容", 390),
            ("difference", "状态", 130), ("operation", "处理方式", 210),
        ):
            self.tree.heading(column, text=text)
            self.tree.column(column, width=width, stretch=column == "name")
        self.tree.grid(row=0, column=0, sticky="nsew")
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        container.rowconfigure(0, weight=1)
        container.columnconfigure(0, weight=1)
        self.tree.bind("<Double-1>", self.toggle_item)
        self.tree.bind("<Button-1>", self.toggle_checkbox)

    def toggle_checkbox(self, event):
        if self.tree.identify_region(event.x, event.y) != "cell":
            return
        if self.tree.identify_column(event.x) != "#1":
            return
        item = self.tree.identify_row(event.y)
        if item:
            self.tree.selection_set(item)
            self.toggle_item()
            return "break"

    def scan_compare(self):
        left_path = Path(self.left_path.get())
        right_path = Path(self.right_path.get())
        kind = self.sync_type.get()
        def operation():
            if kind == "codex":
                left = planner.inventory(left_path, "first-agent")
                right = planner.inventory(right_path, "second-agent")
                plan = planner.compare_inventories(left, right, "bidirectional", set(), set())
            else:
                left = generic_sync.snapshot(left_path, "first-agent")
                right = generic_sync.snapshot(right_path, "second-agent")
                plan = generic_sync.compare(left, right, "bidirectional")
            return kind, left, right, plan
        self.run("正在检查两边的差异...", operation, self.show_comparison)

    def show_comparison(self, result):
        self.current_kind, self.current_left, self.current_right, self.current_plan = result
        self.selected.clear()
        for item in self.tree.get_children():
            self.tree.delete(item)
        entries = self.current_plan["entries"]
        for entry in entries:
            key = entry.get("task_id") or entry["path"]
            name = entry.get("title") or entry["path"]
            action = entry.get("safe_default_action") or entry["action"]
            selected = entry.get("selected", False)
            if selected:
                self.selected.add(key)
            self.tree.insert("", "end", iid=key, values=("☑" if selected else "☐", name, entry["classification"], action))
        self.status.set(f"检查完成：共 {len(entries)} 项，已选择 {len(self.selected)} 项")

    def toggle_item(self, _event=None):
        current = self.tree.selection()
        if not current:
            return
        key = current[0]
        if key in self.selected:
            self.selected.remove(key)
        else:
            self.selected.add(key)
        values = list(self.tree.item(key, "values"))
        values[0] = "☑" if key in self.selected else "☐"
        self.tree.item(key, values=values)
        self.update_provider_selection_summary()
        self.status.set(f"已选择 {len(self.selected)} 项")

    def selected_plan(self):
        plan = json.loads(json.dumps(self.current_plan))
        for entry in plan["entries"]:
            key = entry.get("task_id") or entry["path"]
            entry["selected"] = key in self.selected
        plan["summary"]["selected"] = len(self.selected)
        return plan

    def sync_selected(self):
        if not self.current_plan:
            messagebox.showwarning(APP_NAME, "请先检查差异。")
            return
        if not self.selected:
            messagebox.showwarning(APP_NAME, "请至少选择一项。")
            return
        if self.current_kind == "codex" and not messagebox.askyesno(APP_NAME, "同步 Codex 对话前必须完全关闭 Codex。\n\n确认已经关闭并继续？"):
            return
        def operation():
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                plan = self.selected_plan()
                plan_path = root / "plan.json"
                planner.write_json(plan_path, plan)
                reports = []
                if self.current_kind == "codex":
                    for side, inventory, target in (("left", self.current_left, Path(self.right_path.get())), ("right", self.current_right, Path(self.left_path.get()))):
                        snapshot_path = root / f"{side}.json"
                        planner.write_json(snapshot_path, inventory)
                        bundle = root / f"{side}.zip"
                        try:
                            migration_bundle.create_bundle(snapshot_path, plan_path, side, bundle)
                        except ValueError as error:
                            if "no selected outbound" in str(error).lower():
                                continue
                            raise
                        reports.append(migration_bundle.restore_bundle(bundle, target, require_codex_closed=True))
                else:
                    for side, snapshot, target in (("left", self.current_left, Path(self.right_path.get())), ("right", self.current_right, Path(self.left_path.get()))):
                        snapshot_path = root / f"{side}.json"
                        planner.write_json(snapshot_path, snapshot)
                        bundle = root / f"{side}.zip"
                        try:
                            generic_sync.create_bundle(snapshot_path, plan_path, side, bundle)
                        except ValueError as error:
                            if "no selected outbound" in str(error).lower():
                                continue
                            raise
                        reports.append(generic_sync.restore_bundle(bundle, target))
                return reports
        self.run("正在备份并同步所选内容...", operation, lambda reports: messagebox.showinfo(APP_NAME, f"同步完成，共执行 {len(reports)} 个方向。"))

    def show_transfer(self):
        self.clear()
        self.nav()
        self.title_block("迁移到另一台电脑", "旧电脑负责导出，新电脑负责导入。")
        choice = ttk.Frame(self.content)
        choice.pack(fill="x", pady=(0, 18))
        ttk.Button(choice, text="我在旧电脑：导出", command=self.show_export).pack(side="left", ipady=8)
        ttk.Button(choice, text="我在新电脑：导入", command=self.show_import).pack(side="left", padx=12, ipady=8)

    def show_export(self):
        self.clear()
        self.nav()
        self.title_block("从旧电脑导出", "选择要迁移的内容和保存位置。")
        self.export_type = tk.StringVar(value="codex")
        modes = ttk.Frame(self.content)
        modes.pack(fill="x", pady=(0, 10))
        ttk.Radiobutton(modes, text="Codex 对话", variable=self.export_type, value="codex").pack(side="left")
        ttk.Radiobutton(modes, text="代理目录或自定义文件", variable=self.export_type, value="generic").pack(side="left", padx=18)
        self.export_source = tk.StringVar(value=str(Path.home() / ".codex"))
        self.export_output = tk.StringVar(value=str(Path.home() / "Desktop" / "agent-transfer.cdas.zip"))
        self.path_row(self.content, "要导出的目录", self.export_source)
        self.path_row(self.content, "保存迁移包", self.export_output, save=True)
        ttk.Button(self.content, text="开始导出", command=self.export_transfer).pack(anchor="w", pady=18)

    def export_transfer(self):
        def operation():
            source = Path(self.export_source.get())
            output = Path(self.export_output.get())
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                if self.export_type.get() == "codex":
                    left = planner.inventory(source, "old-computer")
                    right = {"schema_version": 1, "kind": "cross-device-agent-sync-inventory", "device_id": "new-computer", "codex_home": "", "generated_at": "", "conversations": []}
                    right["inventory_hash"] = planner.sha256_bytes(planner.canonical_json({"device_id": right["device_id"], "conversations": []}))
                    plan = planner.compare_inventories(left, right, "left-to-right", set(), set())
                    left_path, plan_path = root / "left.json", root / "plan.json"
                    planner.write_json(left_path, left)
                    planner.write_json(plan_path, plan)
                    return migration_bundle.create_bundle(left_path, plan_path, "left", output)
                left = generic_sync.snapshot(source, "old-computer")
                empty = root / "empty"
                empty.mkdir()
                right = generic_sync.snapshot(empty, "new-computer")
                plan = generic_sync.compare(left, right, "left-to-right")
                left_path, plan_path = root / "left.json", root / "plan.json"
                planner.write_json(left_path, left)
                planner.write_json(plan_path, plan)
                return generic_sync.create_bundle(left_path, plan_path, "left", output)
        self.run("正在生成迁移包...", operation, lambda result: messagebox.showinfo(APP_NAME, f"迁移包已生成：\n{result['bundle_path']}"))

    def show_import(self):
        self.clear()
        self.nav()
        self.title_block("导入到新电脑", "先检查迁移包和现有对话，再决定是否写入。")
        self.import_bundle = tk.StringVar()
        self.import_target = tk.StringVar(value=str(Path.home() / ".codex"))
        self.import_preview = None
        self.import_checking = False
        self.importing = False
        self.file_row(self.content, "迁移包", self.import_bundle)
        self.path_row(self.content, "导入位置", self.import_target)
        controls = ttk.Frame(self.content)
        controls.pack(fill="x", pady=(10, 10))
        self.import_check_button = ttk.Button(controls, text="检查迁移包", command=self.check_import_transfer)
        self.import_check_button.pack(side="left")
        self.import_execute_button = ttk.Button(
            controls, text="开始导入", command=self.import_transfer, state="disabled"
        )
        self.import_execute_button.pack(side="left", padx=8)
        detail_frame = ttk.LabelFrame(self.content, text="导入预览", padding=8)
        detail_frame.pack(fill="both", expand=True, pady=(4, 0))
        self.import_preview_detail = tk.Text(
            detail_frame, wrap="word", height=15, font=("Microsoft YaHei UI", 10), padx=8, pady=8
        )
        detail_scroll = ttk.Scrollbar(detail_frame, orient="vertical", command=self.import_preview_detail.yview)
        self.import_preview_detail.configure(yscrollcommand=detail_scroll.set)
        self.import_preview_detail.pack(side="left", fill="both", expand=True)
        detail_scroll.pack(side="right", fill="y")
        self.set_import_preview_detail("尚未检查。检查不会创建备份、目录或对话文件。")
        self.import_bundle.trace_add("write", self.invalidate_import_preview)
        self.import_target.trace_add("write", self.invalidate_import_preview)

    def set_import_preview_detail(self, text):
        self.import_preview_detail.configure(state="normal")
        self.import_preview_detail.delete("1.0", "end")
        self.import_preview_detail.insert("1.0", text)
        self.import_preview_detail.configure(state="disabled")

    def invalidate_import_preview(self, *_args):
        self.import_preview = None
        if hasattr(self, "import_execute_button"):
            self.import_execute_button.configure(state="disabled")
        if hasattr(self, "import_preview_detail"):
            self.set_import_preview_detail("迁移包或导入位置已改变，请重新检查。尚未写入任何数据。")

    def check_import_transfer(self):
        self.import_preview = None
        self.import_execute_button.configure(state="disabled")
        self.import_checking = True
        self.import_check_button.configure(state="disabled")

        def operation():
            bundle = Path(self.import_bundle.get()).expanduser().resolve()
            target = Path(self.import_target.get()).expanduser().resolve()
            if not bundle.is_file():
                raise ValueError(f"迁移包不存在：{bundle}")
            with zipfile.ZipFile(bundle, "r") as archive:
                manifest = json.loads(archive.read("manifest.json"))
            if manifest.get("kind") == migration_bundle.BUNDLE_KIND:
                prepared = migration_bundle.prepare_restore(bundle, target)
                return {
                    "kind": "codex",
                    "bundle": str(bundle),
                    "target": str(prepared["codex_home"]),
                    "operations": prepared["operations"],
                }
            if manifest.get("kind") == f"{generic_sync.KIND}-bundle":
                prepared = generic_sync.prepare_restore(bundle, target)
                return {
                    "kind": "generic",
                    "bundle": str(bundle),
                    "target": str(prepared["target_root"]),
                    "operations": prepared["operations"],
                }
            raise ValueError("不支持的迁移包类型")

        self.run("正在检查迁移包，尚未写入数据...", operation, self.show_import_preview)

    def show_import_preview(self, preview):
        self.import_checking = False
        self.import_check_button.configure(state="normal")
        self.import_preview = preview
        operations = preview["operations"]
        imported = [item for item in operations if item["action"] == "import"]
        skipped = [item for item in operations if item["action"] == "skip_identical"]
        conflicts = [item for item in operations if item["action"] in {"import_as_alternate_branch", "copy_as_conflict"}]
        kind_label = "Codex 对话" if preview["kind"] == "codex" else "自定义文件"
        lines = [
            "检查完成，尚未写入任何数据。",
            "",
            f"类型：{kind_label}",
            f"导入位置：{preview['target']}",
            f"直接导入：{len(imported)} 项",
            f"完全相同，跳过：{len(skipped)} 项",
        ]
        if preview["kind"] == "codex":
            lines.append(f"同 ID 内容不同，保留两份并新建迁移分支：{len(conflicts)} 项")
        else:
            lines.append(f"同路径内容不同，保留原文件并另存冲突副本：{len(conflicts)} 项")
        if conflicts:
            lines.extend(["", "需要保留两份的项目："])
            for item in conflicts[:50]:
                name = item.get("title") or item.get("source_path") or item.get("path")
                lines.append(f"- {name}")
            if len(conflicts) > 50:
                lines.append(f"- 另有 {len(conflicts) - 50} 项未展开")
        lines.extend([
            "",
            "开始导入前，Codex 必须完全关闭。执行时会再次按相同安全规则检查，并创建可恢复备份。",
        ])
        self.set_import_preview_detail("\n".join(lines))
        self.import_execute_button.configure(state="normal")
        self.status.set("检查完成，请确认预览后开始导入")

    def import_transfer(self):
        if not self.import_preview:
            messagebox.showwarning(APP_NAME, "请先检查迁移包。")
            return
        if not messagebox.askyesno(APP_NAME, "确认预览无误后才会开始导入。\n\n导入前会自动创建可恢复备份；Codex 必须完全关闭。\n\n继续？"):
            return
        self.importing = True
        self.import_check_button.configure(state="disabled")
        self.import_execute_button.configure(state="disabled")

        def operation():
            bundle = Path(self.import_bundle.get())
            with zipfile.ZipFile(bundle, "r") as archive:
                manifest = json.loads(archive.read("manifest.json"))
            if manifest.get("kind") == migration_bundle.BUNDLE_KIND:
                return migration_bundle.restore_bundle(bundle, Path(self.import_target.get()), require_codex_closed=True)
            return generic_sync.restore_bundle(bundle, Path(self.import_target.get()))
        self.run("正在备份并导入...", operation, self.complete_import_transfer)

    def complete_import_transfer(self, result):
        self.importing = False
        self.import_check_button.configure(state="normal")
        self.invalidate_import_preview()
        messagebox.showinfo(APP_NAME, f"导入完成。\n备份位置：{result['backup_path']}")

    def choose_dir(self, variable):
        value = filedialog.askdirectory(initialdir=variable.get() or str(Path.home()))
        if value:
            variable.set(value)

    def choose_save(self, variable):
        value = filedialog.asksaveasfilename(defaultextension=".zip", filetypes=[("同步迁移包", "*.zip"), ("所有文件", "*.*")])
        if value:
            variable.set(value)

    def file_row(self, parent, label, variable):
        row = ttk.Frame(parent)
        row.pack(fill="x", pady=7)
        ttk.Label(row, text=label, width=14).pack(side="left")
        ttk.Entry(row, textvariable=variable).pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="选择...", command=lambda: self.choose_file(variable)).pack(side="left", padx=(8, 0))

    def choose_file(self, variable):
        value = filedialog.askopenfilename(filetypes=[("同步迁移包", "*.zip"), ("所有文件", "*.*")])
        if value:
            variable.set(value)

    def run(self, text, operation, callback=None, progress_flow=None):
        self.status.set(text)
        self.diagnostics.event("operation_start", operation=text)
        def worker():
            try:
                result = operation()
                self.after(
                    0,
                    lambda result=result, callback=callback, progress_flow=progress_flow: self.finish(
                        result, callback, progress_flow
                    ),
                )
            except Exception as error:
                trace = traceback.format_exc()
                self.after(
                    0,
                    lambda error=error, progress_flow=progress_flow, trace=trace: self.fail(
                        error, progress_flow, trace
                    ),
                )
        threading.Thread(target=worker, daemon=True).start()

    def finish(self, result, callback, progress_flow=None):
        self.close_progress_flow(progress_flow)
        if progress_flow and hasattr(self, "provider_execute_button"):
            self.provider_execute_button.configure(state="normal")
        self.status.set("完成")
        summary = None
        if isinstance(result, dict):
            summary = {
                key: result.get(key)
                for key in (
                    "backup_path", "backup_created", "reassigned", "imported", "skipped",
                    "selected_count", "selected_bytes", "required_bytes", "free_bytes",
                )
                if key in result
            }
        self.diagnostics.event("operation_complete", summary=summary)
        if callback:
            callback(result)

    def write_diagnostic_log(self, error, trace):
        try:
            return self.diagnostics.error("operation_failed", error, trace)
        except Exception:
            return None

    def fail(self, error, progress_flow=None, trace=""):
        if getattr(self, "release_checking", False):
            self.release_checking = False
            self.release_check_button.configure(state="normal")
            self.release_progress.stop()
            self.release_progress.pack_forget()
        self.close_progress_flow(progress_flow)
        if progress_flow and hasattr(self, "provider_execute_button"):
            self.provider_execute_button.configure(state="normal")
        if getattr(self, "import_checking", False):
            self.import_checking = False
            self.import_check_button.configure(state="normal")
        if getattr(self, "importing", False):
            self.importing = False
            self.import_check_button.configure(state="normal")
            if self.import_preview:
                self.import_execute_button.configure(state="normal")
        self.status.set("失败")
        log_path = self.write_diagnostic_log(error, trace)
        log_note = f"\n\n诊断日志：\n{log_path}" if log_path else ""
        if isinstance(error, local_provider_sync.ProviderPreflightError):
            self.show_report_window(
                "执行前检查未通过",
                f"{local_provider_sync.format_provider_preflight(error.report)}{log_note}",
                success=False,
            )
        else:
            messagebox.showerror(APP_NAME, f"{error}{log_note}")

    def open_advanced(self):
        self.withdraw()
        self.advanced_window = advanced_gui.AdvancedApp(on_back=self.return_from_advanced)

    def return_from_advanced(self):
        self.advanced_window = None
        self.deiconify()
        self.lift()
        self.focus_force()


def main() -> int:
    if "--self-test" in sys.argv:
        print(json.dumps({"name": APP_NAME, "version": APP_VERSION, "ok": True}, ensure_ascii=False))
        return 0
    SimpleApp().mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
