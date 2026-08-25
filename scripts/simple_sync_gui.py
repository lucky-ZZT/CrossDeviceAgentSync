#!/usr/bin/env python3
"""Simple Windows workflow for agent, computer, and custom-file synchronization."""

from __future__ import annotations

import json
import os
import queue
import sys
import tempfile
import threading
import traceback
import datetime as dt
import tkinter as tk
import webbrowser
import zipfile
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

import app_diagnostics
import app_release_checker
import app_updater
import computer_transfer
import content_manager
import cross_device_agent_sync_gui as advanced_gui
import generic_sync
import local_provider_sync
import migration_bundle
import project_import
import project_registry
import session_merge_planner as planner
from PIL import Image, ImageTk


APP_NAME = "代理与电脑同步工具"
APP_VERSION = "1.0.6"


def format_backup_created_at(value: str, fallback: str = "", timezone=None) -> str:
    if not value:
        return fallback
    try:
        timestamp = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        if timestamp.tzinfo is not None:
            timestamp = timestamp.astimezone(timezone)
        return timestamp.strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return value.replace("T", " ")[:19] or fallback


def format_epoch_timestamp(value, timezone=None) -> str:
    try:
        timestamp = dt.datetime.fromtimestamp(float(value), tz=dt.timezone.utc).astimezone(timezone)
        return timestamp.strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, OSError, OverflowError):
        return "未知"


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
        self._ui_events = queue.Queue()
        self._closing = False
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self._ui_event_after = self.after(50, self._drain_ui_events)
        self.show_home()

    def on_close(self):
        self._closing = True
        if self._ui_event_after:
            self.after_cancel(self._ui_event_after)
            self._ui_event_after = None
        self.diagnostics.event("application_exit")
        self.diagnostics.close()
        self.destroy()

    def clear(self):
        for child in self.content.winfo_children():
            child.destroy()

    def post_ui_event(self, callback):
        """Queue UI work from a worker without calling Tcl from that thread."""
        self._ui_events.put(callback)

    def _drain_ui_events(self):
        for _ in range(100):
            try:
                callback = self._ui_events.get_nowait()
            except queue.Empty:
                break
            try:
                callback()
            except Exception as error:
                self.fail(error, trace=traceback.format_exc())
        if not self._closing:
            self._ui_event_after = self.after(50, self._drain_ui_events)

    def title_block(self, title, subtitle=""):
        ttk.Label(self.content, text=title, font=("Microsoft YaHei UI", 18, "bold")).pack(anchor="w", pady=(0, 4))
        if subtitle:
            ttk.Label(self.content, text=subtitle, foreground="#555555").pack(anchor="w", pady=(0, 18))

    def show_home(self):
        self.clear()
        self.title_block("你想同步什么？", "选择一种方式，后续只需要指定位置并确认内容。")
        actions = (
            ("同步本机不同 Provider", "自动识别 Provider，可切换归属或保留一份副本。", self.show_local_agents),
            ("两台电脑之间传输", "可分别选择项目文件和关联对话，冲突时逐项决定。", self.show_project_transfer),
            ("同步指定文件夹", "选择两个文件夹，检查差异后同步所选文件。", self.show_custom_files),
            ("内容管理", "批量管理对话、项目和会话中的图片。", self.show_content_manager),
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
        self.release_update_button = ttk.Button(
            controls,
            text="立即更新",
            command=self.start_app_update,
            state="disabled",
        )
        self.release_update_button.pack(side="left", padx=(0, 8))
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
            cache_note = ""
            if release.get("using_cached_release"):
                cached_at = release.get("cached_at")
                cached_time = (
                    dt.datetime.fromtimestamp(cached_at).strftime("%Y-%m-%d %H:%M:%S")
                    if isinstance(cached_at, (int, float)) else "未知时间"
                )
                cache_note = (
                    f"网络连接暂时失败，以下是 {cached_time} 保存的上次检查结果。\n"
                    "恢复网络后可再次检查；立即更新仍会重新下载并校验文件。\n\n"
                )
                status += "（缓存结果）"
            if release["update_available"]:
                self.release_update_button.configure(state="normal")
            else:
                self.release_update_button.configure(state="disabled")
            notes = release["release_notes"].strip() or "该 Release 没有更新说明。"
            assets = "\n".join(
                f"- {item['name']}（{self.format_size(item['size'])}）"
                for item in release["assets"]
            )
            if not assets:
                assets = "- 请在 Release 页面查看附件" if not release.get("assets_known", True) else "- 无附件"
            self.set_release_detail(
                f"{status}\n\n{cache_note}"
                f"当前版本：{release['current_version']}\n"
                f"最新版本：{release['latest_version']}\n"
                f"发布时间：{release['published_at'] or '未知'}\n"
                f"发布页面：{release['release_url']}\n\n"
                f"更新说明\n{notes}\n\n发布附件\n{assets}\n\n"
                "软件不会在未确认时更新。发现新版本后，可点击“立即更新”直接下载、校验并替换当前 EXE。"
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

    def start_app_update(self):
        if not self.latest_release or not self.latest_release.get("update_available"):
            return
        latest = self.latest_release["latest_version"]
        if messagebox.askyesno(
            APP_NAME,
            f"将直接下载并校验 v{latest}。\n\n"
            "校验通过后，软件会退出、替换当前 EXE 并自动重启。是否继续？",
        ):
            self.release_update_button.configure(state="disabled")
            self.run("正在下载并校验新版本...", self.download_app_update, self.complete_app_update)

    def download_app_update(self):
        current = app_updater.running_executable()
        app_updater.ensure_replaceable(current)
        downloaded = app_updater.download_and_verify(self.latest_release)
        return app_updater.schedule_replacement(current, downloaded)

    def complete_app_update(self, _script_path):
        messagebox.showinfo(APP_NAME, "新版本已下载并校验完成。软件即将退出、替换并自动重启。")
        self.after(300, self.on_close)

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
        self.title_block("同步本机不同 Provider", "查看每条对话的归属和侧栏状态，再选择切换归属、显示或隐藏。")
        self.local_codex_home = tk.StringVar(value=str(Path.home() / ".codex"))
        self.source_agent = tk.StringVar()
        self.target_agent = tk.StringVar()
        self.provider_full_backup = tk.BooleanVar(value=True)
        self.provider_auto_visibility = tk.BooleanVar(value=True)
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
        ttk.Label(agent_row, text="当前 Provider", width=14).pack(side="left")
        self.source_agent_box = ttk.Combobox(agent_row, textvariable=self.source_agent, state="readonly", width=24)
        self.source_agent_box.pack(side="left")
        ttk.Label(agent_row, text="目标 Provider", width=12).pack(side="left", padx=(24, 0))
        self.target_agent_box = ttk.Combobox(agent_row, textvariable=self.target_agent, state="normal", width=24)
        self.target_agent_box.pack(side="left")
        self.target_agent_box.bind("<<ComboboxSelected>>", lambda _event: self.update_provider_mode())
        self.target_agent_box.bind("<KeyRelease>", lambda _event: self.update_provider_mode())

        mode_panel = ttk.LabelFrame(self.content, text="归属切换选项", padding=8)
        mode_panel.pack(fill="x", pady=(4, 6))
        ttk.Checkbutton(
            mode_panel,
            text="切换归属后自动更新侧栏状态（推荐）",
            variable=self.provider_auto_visibility,
        ).pack(anchor="w")
        self.provider_backup_check = ttk.Checkbutton(
            mode_panel,
            text="执行前完整备份所有将被修改的数据",
            variable=self.provider_full_backup,
        )
        self.provider_backup_check.pack(anchor="w", pady=(6, 0))
        ttk.Label(
            mode_panel,
            text="关闭自动更新后只改变归属，可能出现归属与侧栏显示不一致。",
            foreground="#9A5B00",
        ).pack(anchor="w", pady=(5, 0))
        controls = ttk.Frame(self.content)
        controls.pack(fill="x", pady=(8, 8))
        ttk.Button(controls, text="自动识别 Provider", command=self.load_local_agents).pack(side="left")
        self.provider_show_threads_button = ttk.Button(
            controls, text="加载对话状态", command=self.load_source_threads
        )
        self.provider_show_threads_button.pack(side="left", padx=8)
        self.provider_preflight_button = ttk.Button(
            controls, text="检查归属切换", command=self.check_provider_ready, state="disabled"
        )
        self.provider_preflight_button.pack(side="left")
        self.provider_select_all_button = ttk.Button(controls, text="全选", command=self.select_all_provider_threads)
        self.provider_select_all_button.pack(side="left")
        self.provider_clear_button = ttk.Button(controls, text="全不选", command=self.clear_provider_threads)
        self.provider_clear_button.pack(side="left", padx=(8, 0))
        self.provider_invert_button = ttk.Button(controls, text="反选", command=self.invert_provider_threads)
        self.provider_invert_button.pack(side="left", padx=8)
        ttk.Label(controls, text="点击选择框或双击会话可切换选择").pack(side="right")
        execute_bar = ttk.Frame(self.content, padding=(0, 2, 0, 8))
        execute_bar.pack(fill="x")
        self.provider_selection_summary = tk.StringVar(value="请先加载当前 Provider 的对话")
        ttk.Label(execute_bar, textvariable=self.provider_selection_summary).pack(side="left")
        self.provider_show_button = ttk.Button(
            execute_bar,
            text="显示所选",
            command=lambda: self.set_selected_provider_visibility(True),
            state="disabled",
        )
        self.provider_show_button.pack(side="right", padx=(8, 0), ipady=5)
        self.provider_hide_button = ttk.Button(
            execute_bar,
            text="隐藏所选",
            command=lambda: self.set_selected_provider_visibility(False),
            state="disabled",
        )
        self.provider_hide_button.pack(side="right", padx=(8, 0), ipady=5)
        self.provider_execute_button = ttk.Button(
            execute_bar,
            text="切换所选归属",
            command=self.handoff_local_threads,
            state="disabled",
        )
        self.provider_execute_button.pack(side="right", padx=(8, 0), ipady=5)
        self.provider_clone_button = ttk.Button(
            execute_bar,
            text="创建副本...",
            command=self.clone_selected_provider_threads,
            state="disabled",
        )
        self.provider_clone_button.pack(side="right", ipady=5)
        self.build_result_tree(self.content)
        self.tree.heading("difference", text="侧栏状态")
        self.tree.heading("operation", text="当前归属")
        self.update_provider_mode()
        self.refresh_backup_summary()
        self.load_local_agents()

    def update_provider_mode(self):
        if hasattr(self, "local_agents"):
            self.target_agent_box["values"] = [provider["id"] for provider in self.local_agents]
        for widget in (
            self.provider_show_threads_button,
            self.provider_select_all_button,
            self.provider_clear_button,
            self.provider_invert_button,
        ):
            widget.configure(state="normal")
        self.source_agent_box.configure(state="readonly")
        has_source = bool(self.agent_by_label.get(self.source_agent.get())) if hasattr(self, "agent_by_label") else False
        has_target = bool(self.target_agent.get().strip())
        has_selection = bool(getattr(self, "selected", set()))
        state = "normal" if has_selection and has_target and has_source else "disabled"
        for widget in (
            self.provider_show_button,
            self.provider_hide_button,
            self.provider_execute_button,
            self.provider_clone_button,
            self.provider_preflight_button,
        ):
            widget.configure(state=state)
        if hasattr(self, "tree") and self.tree.winfo_exists():
            for task_id in self.tree.get_children():
                values = list(self.tree.item(task_id, "values"))
                if len(values) >= 4 and task_id in self.local_thread_by_id:
                    thread = self.local_thread_by_id[task_id]
                    values[3] = thread.get("model_provider") or thread.get("provider", "")
                    self.tree.item(task_id, values=values)

    def load_local_agents(self):
        codex_home = Path(self.local_codex_home.get())
        def operation():
            return local_provider_sync.discover_providers(codex_home)
        def show(providers):
            self.local_agents = providers
            def provider_label(provider):
                if provider.get("current"):
                    status = "当前配置"
                elif provider.get("configured"):
                    status = "已配置"
                else:
                    status = "仅历史记录"
                return f"{provider['id']}（{status}，{provider['sqlite_count']} 个对话）"
            labels = [provider_label(provider) for provider in providers]
            self.agent_by_label = dict(zip(labels, providers))
            self.source_agent_box["values"] = labels
            if labels:
                state = local_provider_sync._read_provider_visibility_state(codex_home)
                active_id = state.get("active_provider")
                current = next(
                    (provider for provider in providers if provider.get("id") == active_id),
                    next((provider for provider in providers if provider.get("current")), providers[0]),
                )
                current_label = next(
                    label for label, provider in self.agent_by_label.items()
                    if provider.get("id") == current["id"]
                )
                self.source_agent.set(current_label)
                self.target_agent.set(current["id"])
            if providers:
                self.status.set(f"识别到 {len(providers)} 个 Provider；目标侧栏只显示其所属对话")
            else:
                self.status.set("未识别到 Provider；请检查 Codex 数据位置，目标 Provider 仍可手动输入")
            self.update_provider_mode()
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
                content = f"{thread['title']}（{size}）"
                self.tree.insert(
                    "", "end", iid=thread["id"],
                    values=("☑", content, thread.get("visibility_state", "未知"), thread.get("model_provider", source["id"])),
                )
            total_size = sum(thread["size_bytes"] for thread in threads)
            unavailable = max(0, int(source.get("sqlite_count", 0)) - len(threads))
            if threads:
                self.update_provider_mode()
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
        ready = bool(self.selected) and bool(self.target_agent.get().strip())
        if hasattr(self, "provider_execute_button"):
            for widget in (
                self.provider_show_button,
                self.provider_hide_button,
                self.provider_execute_button,
                self.provider_clone_button,
                self.provider_preflight_button,
            ):
                widget.configure(state="normal" if ready else "disabled")

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
            messagebox.showwarning(APP_NAME, "请先选择当前 Provider、目标 Provider 和至少一个会话。")
            return
        if source["id"] == target_provider:
            messagebox.showwarning(APP_NAME, "来源 Provider 和目标 Provider 不能相同。")
            return
        def operation():
            report = local_provider_sync.plan_provider_workspace(
                Path(self.local_codex_home.get()),
                source["id"],
                source_provider=source["id"],
                target_provider=target_provider,
                selected_ids=set(self.selected),
                create_backup=self.provider_full_backup.get(),
                enforce_provider_isolation=False,
                auto_hide_reassigned=self.provider_auto_visibility.get(),
            )
            if not report["ok"]:
                raise local_provider_sync.ProviderPreflightError(report)
            return report
        def complete(report):
            scope = (
                f"切换：{source['id']} → {target_provider}\n"
                f"选中会话：{len(self.selected)} 个\n"
                f"侧栏自动处理：{'是' if self.provider_auto_visibility.get() else '否'}\n"
                f"执行后隐藏：{report['archive_count']} 个\n"
            )
            text = (
                "执行前检查通过\n\n"
                f"{scope}"
                f"预计所需空间：{self.format_size(report.get('required_bytes', 0))}\n"
                f"当前可用空间：{self.format_size(report['free_bytes'])}\n"
                "\n正式执行前会重新检查，并要求 Codex 已完全关闭。"
            )
            self.show_report_window("执行前检查结果", text, success=True)
        self.run("正在检查所选会话归属、侧栏状态和磁盘空间...", operation, complete)

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

    def choose_action_dialog(self, title, summary, options, default):
        """Return one action value from a small explicit conflict-resolution dialog."""
        result = {"value": None}
        window = tk.Toplevel(self)
        window.title(title)
        window.transient(self)
        window.resizable(False, False)
        frame = ttk.Frame(window, padding=18)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text=title, font=("Microsoft YaHei UI", 14, "bold")).pack(anchor="w")
        ttk.Label(frame, text=summary, wraplength=620, justify="left").pack(
            anchor="w", fill="x", pady=(8, 12)
        )
        variable = tk.StringVar(value=default)
        for value, label in options:
            ttk.Radiobutton(frame, text=label, value=value, variable=variable).pack(
                anchor="w", fill="x", pady=3
            )
        ttk.Label(
            frame,
            text="移除注册只处理侧栏元数据；项目文件删除必须在内容管理中单独执行。",
            foreground="#9A5B00",
            wraplength=620,
        ).pack(anchor="w", fill="x", pady=(12, 4))
        buttons = ttk.Frame(frame)
        buttons.pack(fill="x", pady=(12, 0))

        def confirm():
            result["value"] = variable.get()
            window.destroy()

        ttk.Button(buttons, text="取消", command=window.destroy).pack(side="right")
        ttk.Button(buttons, text="确定", command=confirm).pack(side="right", padx=(0, 8))
        window.protocol("WM_DELETE_WINDOW", window.destroy)
        window.update_idletasks()
        x = self.winfo_rootx() + max(0, (self.winfo_width() - window.winfo_reqwidth()) // 2)
        y = self.winfo_rooty() + max(0, (self.winfo_height() - window.winfo_reqheight()) // 2)
        window.geometry(f"+{x}+{y}")
        window.grab_set()
        window.wait_window()
        return result["value"]

    def choose_path_repair_actions(self, health):
        """Collect per-project path decisions without changing Codex data."""
        result = {"value": None}
        window = tk.Toplevel(self)
        window.title("项目注册处理")
        window.transient(self)
        window.geometry("1080x680")
        window.minsize(900, 580)
        frame = ttk.Frame(window, padding=16)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="逐条处理项目注册", font=("Microsoft YaHei UI", 14, "bold")).pack(anchor="w")
        ttk.Label(
            frame,
            text=(
                "每条项目 ID 独立显示并使用同一组操作。不能安全执行的按钮会置灰，"
                "下方会说明具体原因；删除注册默认保留项目文件和对话。"
            ),
            wraplength=1020,
            foreground="#555555",
        ).pack(anchor="w", fill="x", pady=(4, 10))

        registrations = {
            item["project_id"]: item
            for item in health.get("actionable_project_registrations", [])
        }
        planned_actions = {project_id: "keep" for project_id in registrations}
        planned_names = {}
        planned_paths = {}

        table_frame = ttk.Frame(frame)
        table_frame.pack(fill="both", expand=True)
        columns = ("name", "id", "status", "path", "tasks", "recommendation", "plan")
        tree = ttk.Treeview(table_frame, columns=columns, show="headings", height=10, selectmode="browse")
        headings = {
            "name": "项目名称", "id": "项目 ID", "status": "路径状态", "path": "实际路径",
            "tasks": "对话", "recommendation": "建议", "plan": "计划操作",
        }
        widths = {"name": 150, "id": 95, "status": 105, "path": 330, "tasks": 55, "recommendation": 90, "plan": 150}
        for column in columns:
            tree.heading(column, text=headings[column])
            tree.column(column, width=widths[column], minwidth=50, stretch=column in {"name", "path", "plan"})
        yscroll = ttk.Scrollbar(table_frame, orient="vertical", command=tree.yview)
        xscroll = ttk.Scrollbar(table_frame, orient="horizontal", command=tree.xview)
        tree.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
        tree.grid(row=0, column=0, sticky="nsew")
        yscroll.grid(row=0, column=1, sticky="ns")
        xscroll.grid(row=1, column=0, sticky="ew")
        table_frame.rowconfigure(0, weight=1)
        table_frame.columnconfigure(0, weight=1)

        action_labels = {
            "keep": "保留",
            "normalize": "修复路径后保留",
            "repoint": "更正目录后保留",
            "rename": "重命名后保留",
            "delete": "删除这条注册",
            "full_delete": "彻底删除整个项目",
        }
        recommendation_labels = {
            "keep": "建议保留", "normalize": "建议修复", "delete": "建议删除",
        }

        def refresh_row(project_id):
            item = registrations[project_id]
            root_paths = [root.get("raw_path") or "" for root in item.get("roots", [])]
            path_text = root_paths[0] if len(root_paths) == 1 else f"{root_paths[0]}（另有 {len(root_paths) - 1} 个）"
            plan = action_labels[planned_actions[project_id]]
            if project_id in planned_names:
                plan += f"：{planned_names[project_id]}"
            if project_id in planned_paths:
                plan += f"：{planned_paths[project_id]}"
            values = (
                item["project_name"], project_id[:8], item["status"], path_text,
                item.get("linked_tasks", 0),
                recommendation_labels.get(item.get("recommended_action"), "人工判断"),
                plan,
            )
            if tree.exists(project_id):
                tree.item(project_id, values=values)
            else:
                tree.insert("", "end", iid=project_id, values=values)

        for project_id in registrations:
            refresh_row(project_id)

        selected_summary = tk.StringVar(value="请选择一条项目注册。")
        disabled_summary = tk.StringVar(value="")
        ttk.Label(frame, textvariable=selected_summary, wraplength=1020).pack(anchor="w", fill="x", pady=(10, 3))

        actions_frame = ttk.Frame(frame)
        actions_frame.pack(fill="x")
        action_buttons = {}

        def selected_registration():
            selected = tree.selection()
            return registrations[selected[0]] if selected else None

        def set_action(action):
            item = selected_registration()
            if item is None:
                return
            project_id = item["project_id"]
            capability = item["capabilities"][action]
            if not capability["enabled"]:
                messagebox.showwarning(APP_NAME, capability["reason"], parent=window)
                return
            if action == "repoint":
                initial = next((root.get("normalized_path") for root in item.get("roots", []) if root.get("normalized_path")), str(Path.home()))
                chosen = filedialog.askdirectory(parent=window, initialdir=initial, title="选择正确的项目目录")
                if not chosen:
                    return
                planned_paths[project_id] = chosen
                planned_names.pop(project_id, None)
            elif action == "rename":
                new_name = simpledialog.askstring(
                    "修改侧栏名称", "新的侧栏显示名称：", initialvalue=item["project_name"], parent=window
                )
                if new_name is None:
                    return
                new_name = new_name.strip()
                if not new_name or len(new_name) > 120 or any(ord(character) < 32 for character in new_name):
                    messagebox.showwarning(APP_NAME, "名称不能为空、不能超过 120 个字符，也不能包含控制字符。", parent=window)
                    return
                planned_names[project_id] = new_name
                planned_paths.pop(project_id, None)
            else:
                planned_names.pop(project_id, None)
                planned_paths.pop(project_id, None)
            planned_actions[project_id] = action
            refresh_row(project_id)
            update_selected_state()

        def show_details():
            item = selected_registration()
            if item is None:
                return
            roots = []
            for number, root in enumerate(item.get("roots", []), start=1):
                roots.extend((
                    f"路径 {number}：{root.get('raw_path') or '无'}",
                    f"规范路径：{root.get('normalized_path') or '无法确定'}",
                    f"状态：{'存在' if root.get('exists') else '不存在'} / {root.get('path_kind')}",
                ))
            tasks = item.get("related_tasks", [])
            task_lines = [f"- {task.get('title') or task['task_id']}  ({task['task_id']})" for task in tasks[:20]]
            if len(tasks) > len(task_lines):
                task_lines.append(f"- 另有 {len(tasks) - len(task_lines)} 个对话未展开")
            lines = [
                f"项目名称：{item['project_name']}",
                f"项目 ID：{item['project_id']}",
                f"路径状态：{item['status']}",
                f"已知引用：{item.get('known_reference_count', 0)}",
                f"关联对话：{len(tasks)}",
                "",
                *roots,
                "",
                "关联对话：",
                *(task_lines or ["- 无"]),
                "",
                "操作可用性：",
            ]
            for action, label in action_labels.items():
                capability = item["capabilities"][action]
                lines.append(f"- {label}：{'可用' if capability['enabled'] else '不可用'}；{capability['reason']}")
            messagebox.showinfo("项目注册详情", "\n".join(lines), parent=window)

        button_specs = (
            ("details", "查看详情", show_details),
            ("keep", "保留", lambda: set_action("keep")),
            ("normalize", "修复路径", lambda: set_action("normalize")),
            ("repoint", "更正目录", lambda: set_action("repoint")),
            ("rename", "重命名", lambda: set_action("rename")),
            ("delete", "删除注册", lambda: set_action("delete")),
            ("full_delete", "彻底删除项目", lambda: set_action("full_delete")),
        )
        for key, label, command in button_specs:
            button = ttk.Button(actions_frame, text=label, command=command)
            button.pack(side="left", padx=(0, 6))
            action_buttons[key] = button

        ttk.Label(frame, textvariable=disabled_summary, foreground="#9A5B00", wraplength=1020).pack(
            anchor="w", fill="x", pady=(6, 0)
        )

        def update_selected_state(_event=None):
            item = selected_registration()
            if item is None:
                for button in action_buttons.values():
                    button.configure(state="disabled")
                selected_summary.set("请选择一条项目注册。")
                disabled_summary.set("")
                return
            selected_summary.set(
                f"当前：{item['project_name']}  |  完整 ID：{item['project_id']}  |  {item['status']}  |  "
                f"已知引用 {item.get('known_reference_count', 0)}，关联对话 {item.get('linked_tasks', 0)}"
            )
            disabled = []
            for key, button in action_buttons.items():
                capability = item["capabilities"].get(key, {"enabled": True, "reason": ""})
                button.configure(state="normal" if capability["enabled"] else "disabled")
                if not capability["enabled"]:
                    disabled.append(f"{button.cget('text')}：{capability['reason']}")
            disabled_summary.set("当前不可用：" + "；".join(disabled) if disabled else "当前所有操作均可用。")

        tree.bind("<<TreeviewSelect>>", update_selected_state)
        if registrations:
            first = next(iter(registrations))
            tree.selection_set(first)
            tree.focus(first)
            update_selected_state()

        conversation_var = tk.BooleanVar(value=bool(health.get("repairable_paths")))
        trigger_var = tk.BooleanVar(value=bool(health.get("normalization_triggers")))
        options_frame = ttk.Frame(frame)
        options_frame.pack(fill="x", pady=(10, 0))
        ttk.Checkbutton(
            options_frame,
            text=f"修复已验证的会话扩展路径（{len(health.get('repairable_paths', []))} 条）",
            variable=conversation_var,
        ).pack(anchor="w")
        ttk.Checkbutton(
            options_frame,
            text=f"移除已识别的遗留触发器（{len(health.get('normalization_triggers', []))} 个）",
            variable=trigger_var,
        ).pack(anchor="w", pady=(3, 0))

        buttons = ttk.Frame(frame)
        buttons.pack(fill="x", pady=(12, 0))

        def confirm():
            full_delete_ids = [
                project_id for project_id, action in planned_actions.items() if action == "full_delete"
            ]
            other_changes = [
                project_id for project_id, action in planned_actions.items()
                if action not in {"keep", "full_delete"}
            ]
            if full_delete_ids and (len(full_delete_ids) > 1 or other_changes):
                messagebox.showwarning(
                    APP_NAME,
                    "彻底删除项目必须单独执行，一次只能选择一个项目，不能与其他修复混合。",
                    parent=window,
                )
                return
            result["value"] = {
                "actions": {
                    f"registration:{project_id}": action
                    for project_id, action in planned_actions.items()
                },
                "names": {
                    f"registration:{project_id}": name for project_id, name in planned_names.items()
                },
                "paths": {
                    f"registration:{project_id}": path for project_id, path in planned_paths.items()
                },
                "full_delete_ids": full_delete_ids,
                "repair_conversations": conversation_var.get(),
                "remove_triggers": trigger_var.get(),
            }
            window.destroy()

        ttk.Button(buttons, text="取消", command=window.destroy).pack(side="right")
        ttk.Button(buttons, text="继续", command=confirm).pack(side="right", padx=(0, 8))
        window.protocol("WM_DELETE_WINDOW", window.destroy)
        window.grab_set()
        window.wait_window()
        return result["value"]

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
            ("rollouts", "3. 调整会话归属和侧栏可见性"),
            ("database", "4. 更新 SQLite 和侧栏目录"),
            ("verify", "5. 验证隔离结果"),
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
            text="正在应用 Provider 侧栏隔离",
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

    def start_data_progress(self, title, steps):
        window = tk.Toplevel(self)
        window.title(title)
        window.transient(self)
        window.resizable(False, False)
        window.protocol("WM_DELETE_WINDOW", lambda: None)
        frame = ttk.Frame(window, padding=18)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text=title, font=("Microsoft YaHei UI", 14, "bold")).pack(anchor="w")
        ttk.Label(frame, text="请保持软件打开，操作完成前不要启动或使用 Codex。", foreground="#9A5B00").pack(
            anchor="w", pady=(2, 12)
        )
        labels = {}
        for key, text in steps:
            label = ttk.Label(frame, text=f"等待  {text}")
            label.pack(anchor="w", pady=2)
            labels[key] = label
        detail = tk.StringVar(value="正在准备...")
        ttk.Separator(frame).pack(fill="x", pady=(12, 8))
        ttk.Label(frame, textvariable=detail, wraplength=560).pack(anchor="w", fill="x")
        progressbar = ttk.Progressbar(frame, mode="indeterminate", length=560)
        progressbar.pack(fill="x", pady=(10, 0))
        progressbar.start(12)
        window.update_idletasks()
        x = self.winfo_rootx() + max(0, (self.winfo_width() - window.winfo_reqwidth()) // 2)
        y = self.winfo_rooty() + max(0, (self.winfo_height() - window.winfo_reqheight()) // 2)
        window.geometry(f"+{x}+{y}")
        window.grab_set()
        return {"window": window, "steps": steps, "labels": labels, "detail": detail, "progressbar": progressbar}

    def update_data_progress(self, flow, stage, detail):
        window = flow["window"]
        if not window.winfo_exists():
            return
        order = [key for key, _text in flow["steps"]]
        current_index = len(order) if stage in {"complete", "finalize"} else order.index(stage) if stage in order else 0
        for index, (key, text) in enumerate(flow["steps"]):
            prefix = "完成" if index < current_index else "进行中" if index == current_index else "等待"
            flow["labels"][key].configure(text=f"{prefix}  {text}")
        flow["detail"].set(detail)
        self.status.set(detail)

    def handoff_local_threads(self):
        target_provider = self.target_agent.get().strip()
        source = self.agent_by_label.get(self.source_agent.get())
        if not source or not target_provider or not self.selected:
            messagebox.showwarning(APP_NAME, "请先选择当前 Provider、目标 Provider 和至少一个会话。")
            return
        if source["id"] == target_provider:
            messagebox.showwarning(APP_NAME, "来源 Provider 和目标 Provider 不能相同。")
            return
        selected_bytes = sum(
            self.local_thread_by_id[task_id]["size_bytes"]
            for task_id in self.selected
            if task_id in self.local_thread_by_id
        )
        backup_note = (
            "将完整备份所有移动或改写的会话文件、SQLite、旧索引和侧栏目录。"
            if self.provider_full_backup.get()
            else "不保留长期备份；执行失败仍会自动回滚，成功后无法从软件恢复本次操作。"
        )
        confirmation = (
            f"将 {len(self.selected)} 个会话从 {source['id']} 改为 {target_provider}。\n\n"
            f"所选数据：{self.format_size(selected_bytes)}\n"
            f"切换后自动更新侧栏：{'是' if self.provider_auto_visibility.get() else '否'}\n\n"
            f"{backup_note}\n\n执行前必须完全关闭 Codex。确认继续？"
        )
        if not messagebox.askyesno(APP_NAME, confirmation):
            return
        def complete(result):
            self.refresh_backup_summary()
            backup_text = (
                f"\n\n完整备份：\n{result['backup_path']}"
                if result.get("backup_created", bool(result.get("backup_path")))
                else "\n\n未保留长期备份。"
            )
            messagebox.showinfo(
                APP_NAME,
                "所选会话归属切换完成。\n"
                f"切换归属：{result['reassign_count']} 个\n"
                f"目标侧栏显示：{result['active_provider']}\n"
                f"自动隐藏来源侧栏：{result['archive_count']} 个"
                f"{backup_text}\n\n重新启动 Codex 后，侧栏状态生效。",
            )
            # A successful automatic handoff makes the target Provider the
            # active sidebar, so keep the UI on the same workspace instead of
            # reloading the old source list.
            target_label = next(
                (
                    label
                    for label, provider in self.agent_by_label.items()
                    if provider.get("id") == result.get("active_provider")
                ),
                None,
            )
            if target_label:
                self.source_agent.set(target_label)
            self.load_source_threads()
        self.provider_execute_button.configure(state="disabled")
        progress_flow = self.start_provider_progress()
        def report_progress(stage, detail):
            self.post_ui_event(
                lambda stage=stage, detail=detail: self.update_provider_progress(progress_flow, stage, detail)
            )
        self.run(
            "正在切换所选会话归属...",
            lambda: local_provider_sync.apply_provider_workspace(
                Path(self.local_codex_home.get()),
                source["id"],
                source_provider=source["id"],
                target_provider=target_provider,
                selected_ids=set(self.selected),
                require_codex_closed=True,
                create_backup=self.provider_full_backup.get(),
                enforce_provider_isolation=False,
                auto_hide_reassigned=self.provider_auto_visibility.get(),
                progress_callback=report_progress,
            ),
            complete,
            progress_flow,
        )

    def set_selected_provider_visibility(self, visible: bool):
        source = self.agent_by_label.get(self.source_agent.get())
        if not source or not self.selected:
            messagebox.showwarning(APP_NAME, "请先加载并选择要操作的对话。")
            return
        action = "显示" if visible else "隐藏"
        if not messagebox.askyesno(APP_NAME, f"将{action}选中的 {len(self.selected)} 个会话？\n\n此操作不改变 Provider 归属。"):
            return
        self.provider_show_button.configure(state="disabled")
        self.provider_hide_button.configure(state="disabled")
        self.run(
            f"正在{action}所选会话...",
            lambda: local_provider_sync.apply_provider_workspace(
                Path(self.local_codex_home.get()),
                source["id"],
                selected_ids=set(self.selected),
                visibility_overrides={task_id: visible for task_id in self.selected},
                enforce_provider_isolation=False,
                require_codex_closed=True,
                create_backup=self.provider_full_backup.get(),
            ),
            lambda result: (
                messagebox.showinfo(APP_NAME, f"已{action}所选会话。\n处理：{result['changed']} 个\nCodex 重启后刷新侧栏。"),
                self.load_source_threads(),
            ),
        )

    def clone_selected_provider_threads(self):
        source = self.agent_by_label.get(self.source_agent.get())
        target_provider = self.target_agent.get().strip()
        if not source or not target_provider or not self.selected:
            messagebox.showwarning(APP_NAME, "请先选择来源、目标和至少一个会话。")
            return
        if source["id"] == target_provider:
            messagebox.showwarning(APP_NAME, "来源 Provider 和目标 Provider 不能相同。")
            return
        if not messagebox.askyesno(APP_NAME, f"为 {target_provider} 创建 {len(self.selected)} 个独立副本？"):
            return
        self.provider_clone_button.configure(state="disabled")
        self.run(
            "正在创建 Provider 副本...",
            lambda: local_provider_sync.clone_to_provider(
                Path(self.local_codex_home.get()), source["id"], target_provider, set(self.selected), True
            ),
            lambda result: (
                messagebox.showinfo(APP_NAME, f"已创建 {result['imported']} 个副本。\n备份：{result['backup_path']}"),
                self.load_source_threads(),
            ),
        )

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
        ttk.Button(controls, text="恢复整份备份（谨慎）", command=self.restore_selected_backup).pack(side="left")
        ttk.Button(controls, text="恢复指定对话", command=self.restore_selected_conversation).pack(side="left", padx=8)
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
        conversation_frame = ttk.LabelFrame(self.content, text="备份中的对话（可单独恢复）", padding=6)
        conversation_frame.pack(fill="both", expand=True, pady=(8, 0))
        search_row = ttk.Frame(conversation_frame)
        search_row.pack(fill="x", pady=(0, 6))
        ttk.Label(search_row, text="检索").pack(side="left")
        self.backup_conversation_query = tk.StringVar()
        search_entry = ttk.Entry(search_row, textvariable=self.backup_conversation_query)
        search_entry.pack(side="left", fill="x", expand=True, padx=(6, 8))
        search_entry.bind("<KeyRelease>", self.filter_backup_conversations)
        ttk.Button(search_row, text="检索全部备份", command=self.search_all_backup_conversations).pack(side="left")
        ttk.Button(search_row, text="预览所选", command=self.preview_backup_conversation).pack(side="left", padx=(8, 0))
        conversation_columns = ("backup_time", "updated_at", "title", "task_id", "cwd", "provider")
        conversation_table = ttk.Frame(conversation_frame)
        conversation_table.pack(fill="both", expand=True)
        self.backup_conversation_tree = ttk.Treeview(
            conversation_table, columns=conversation_columns, show="headings", selectmode="browse", height=7
        )
        for column, title, width in (
            ("backup_time", "备份时间", 145),
            ("updated_at", "最后活动", 145),
            ("title", "对话标题", 230),
            ("task_id", "任务 ID", 285),
            ("cwd", "工作目录", 220),
            ("provider", "代理", 90),
        ):
            self.backup_conversation_tree.heading(column, text=title)
            self.backup_conversation_tree.column(column, width=width, stretch=column in {"title", "cwd"}, minwidth=80)
        conversation_y = ttk.Scrollbar(conversation_table, orient="vertical", command=self.backup_conversation_tree.yview)
        conversation_x = ttk.Scrollbar(conversation_table, orient="horizontal", command=self.backup_conversation_tree.xview)
        self.backup_conversation_tree.configure(yscrollcommand=conversation_y.set, xscrollcommand=conversation_x.set)
        self.backup_conversation_tree.grid(row=0, column=0, sticky="nsew")
        conversation_y.grid(row=0, column=1, sticky="ns")
        conversation_x.grid(row=1, column=0, sticky="ew")
        conversation_table.rowconfigure(0, weight=1)
        conversation_table.columnconfigure(0, weight=1)
        self.backup_conversation_records = {}
        self.backup_conversation_all_records = []
        self.backup_tree.bind("<<TreeviewSelect>>", self.show_backup_detail)
        self.backup_conversation_tree.bind("<Double-1>", self.preview_backup_conversation)
        self.load_backups()

    def load_backups(self):
        backups = migration_bundle.list_backups(Path(self.backup_codex_home.get()))
        self.backup_records = {}
        self.backup_conversation_records = {}
        self.backup_conversation_all_records = []
        for item in self.backup_tree.get_children():
            self.backup_tree.delete(item)
        if hasattr(self, "backup_conversation_tree"):
            for item in self.backup_conversation_tree.get_children():
                self.backup_conversation_tree.delete(item)
        labels = {
            "sync": "同步前备份",
            "provider_clone": "Provider 复制前备份",
            "provider_reassign": "Provider 切换前完整备份",
            "restore_guard": "恢复前保护备份",
            "legacy": "旧版备份",
            "conversation_delete": "对话删除前完整备份",
            "conversation_restore": "对话恢复前保护备份",
        }
        for index, backup in enumerate(backups):
            key = f"backup-{index}"
            self.backup_records[key] = backup
            created = format_backup_created_at(backup["created_at"], backup["name"][:15])
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
        if not hasattr(self, "backup_conversation_tree"):
            return
        for item in self.backup_conversation_tree.get_children():
            self.backup_conversation_tree.delete(item)
        self.backup_conversation_records = {}
        self.backup_conversation_all_records = []
        if not backup["restorable"] or backup["operation"] != "conversation_delete":
            return
        try:
            records = migration_bundle.backup_conversation_records(
                Path(backup["path"]), Path(self.backup_codex_home.get())
            )
        except Exception as error:
            self.backup_detail.set(f"{state}；无法读取对话清单：{error}")
            return
        for record in records:
            record["_backup_path"] = backup["path"]
            record["_backup_created_at"] = backup["created_at"]
        self.backup_conversation_all_records = records
        self.filter_backup_conversations()

    def _backup_record_matches(self, record, query):
        if not query:
            return True
        values = (
            record.get("title"), content_manager.repair_mojibake(str(record.get("title") or "")),
            record.get("task_id"), record.get("cwd"),
            record.get("model_provider"), record.get("_backup_path"),
        )
        return query in " ".join(str(value or "") for value in values).casefold()

    def _populate_backup_conversations(self, records):
        for item in self.backup_conversation_tree.get_children():
            self.backup_conversation_tree.delete(item)
        self.backup_conversation_records = {}
        for index, record in enumerate(records):
            key = f"conversation-{index}"
            self.backup_conversation_records[key] = record
            title = content_manager.repair_mojibake(str(record.get("title") or record["task_id"]))
            self.backup_conversation_tree.insert(
                "", "end", iid=key,
                values=(
                    format_backup_created_at(str(record.get("_backup_created_at") or "")),
                    format_epoch_timestamp(record.get("updated_at")),
                    title,
                    record["task_id"],
                    content_manager.display_project_path(str(record.get("cwd") or "")),
                    record.get("model_provider") or "",
                ),
            )

    def filter_backup_conversations(self, _event=None):
        query = self.backup_conversation_query.get().strip().casefold() if hasattr(self, "backup_conversation_query") else ""
        records = [record for record in self.backup_conversation_all_records if self._backup_record_matches(record, query)]
        records.sort(key=lambda item: (float(item.get("updated_at") or 0), str(item.get("_backup_created_at") or "")), reverse=True)
        self._populate_backup_conversations(records)
        if query:
            self.status.set(f"找到 {len(records)} 条匹配的备份对话")

    def search_all_backup_conversations(self):
        query = self.backup_conversation_query.get().strip().casefold()
        if not query:
            messagebox.showwarning(APP_NAME, "请输入标题、任务 ID 或项目路径后再检索全部备份。")
            return
        codex_home = Path(self.backup_codex_home.get())
        backups = [
            backup for backup in migration_bundle.list_backups(codex_home)
            if backup["restorable"] and backup["operation"] == "conversation_delete"
        ]

        def operation():
            matches = []
            for backup in backups:
                for record in migration_bundle.backup_conversation_records(Path(backup["path"]), codex_home):
                    record["_backup_path"] = backup["path"]
                    record["_backup_created_at"] = backup["created_at"]
                    if self._backup_record_matches(record, query):
                        matches.append(record)
            return matches

        def complete(records):
            self.backup_conversation_all_records = records
            self.filter_backup_conversations()
            self.backup_detail.set(f"已跨 {len(backups)} 个对话删除备份检索；结果按最后活动时间排序。")

        self.run("正在检索全部可恢复备份...", operation, complete)

    def preview_backup_conversation(self, _event=None):
        selection = self.backup_conversation_tree.selection()
        if len(selection) != 1:
            messagebox.showwarning(APP_NAME, "请先选择一条备份对话进行预览。")
            return
        record = self.backup_conversation_records[selection[0]]
        rollout = record.get("backup_rollout_path")
        if not rollout or not Path(rollout).is_file():
            messagebox.showwarning(APP_NAME, "这个备份缺少可预览的对话文件。")
            return
        self.run(
            "正在只读加载备份对话预览...",
            lambda: content_manager.preview_conversation(Path(rollout), max_messages=12),
            lambda preview: self.show_backup_conversation_preview(record, preview),
        )

    def show_backup_conversation_preview(self, record, preview):
        title = content_manager.repair_mojibake(str(record.get("title") or record["task_id"]))
        details = [
            "此窗口只读取备份内容，不会恢复或修改任何数据。",
            "",
            f"对话标题：{title}",
            f"任务 ID：{record['task_id']}",
            f"工作目录：{record.get('cwd') or '未记录'}",
            f"Provider：{record.get('model_provider') or '未记录'}",
            f"最后活动：{format_epoch_timestamp(record.get('updated_at'))}",
            f"备份时间：{format_backup_created_at(str(record.get('_backup_created_at') or ''))}",
            f"备份目录：{record.get('_backup_path')}",
            f"可读消息：{preview['message_count']} 条；工具调用：{preview['tool_call_count']} 次",
        ]
        if preview.get("first_user_message"):
            details.append(f"最初请求：{preview['first_user_message']}")
        details.extend(("", "最近对话内容", "=" * 56))
        role_labels = {"user": "用户", "assistant": "代理", "tool": "工具"}
        if preview["messages"]:
            for message in preview["messages"]:
                details.extend(("", f"[{role_labels.get(message['role'], message['role'])}]", message["text"]))
        else:
            details.extend(("", "未找到可预览的用户或代理消息。"))
        self.show_report_window("备份对话只读预览", "\n".join(details))
        self.status.set("备份对话预览已打开")

    def restore_selected_conversation(self):
        conversation_selection = self.backup_conversation_tree.selection() if hasattr(self, "backup_conversation_tree") else ()
        if not conversation_selection:
            messagebox.showwarning(APP_NAME, "请先在下方选择一个要恢复的对话。")
            return
        record = self.backup_conversation_records[conversation_selection[0]]
        backup_path = record.get("_backup_path")
        if not backup_path:
            messagebox.showwarning(APP_NAME, "无法确定这条对话所属的备份。")
            return
        title = content_manager.repair_mojibake(str(record.get("title") or record["task_id"]))
        prompt = (
            "恢复前必须完全关闭 Codex。\n\n"
            f"只恢复这一条对话：\n{title}\n\n"
            f"任务 ID：{record['task_id']}\n"
            "当前其他对话不会回退。程序会先创建保护备份。\n\n确认继续？"
        )
        if not messagebox.askyesno(APP_NAME, prompt):
            return

        def complete(result):
            self.load_backups()
            messagebox.showinfo(
                APP_NAME,
                f"对话恢复完成：\n{title}\n\n"
                f"恢复前的当前状态已备份到：\n{result['safety_backup_path']}\n\n"
                "重新启动 Codex 后，侧栏目录会显示该对话。",
            )

        self.run(
            "正在创建保护备份并恢复指定对话...",
            lambda: migration_bundle.restore_conversation_from_backup(
                Path(backup_path), Path(self.backup_codex_home.get()), record["task_id"], require_codex_closed=True
            ),
            complete,
        )

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

    def _manager_tree(self, parent, columns, headings, widths):
        frame = ttk.Frame(parent)
        frame.pack(fill="both", expand=True)
        tree = ttk.Treeview(frame, columns=columns, show="headings", selectmode="extended")
        for column, heading, width in zip(columns, headings, widths):
            tree.heading(
                column,
                text=heading,
                command=lambda current_tree=tree, current_column=column: self._sort_manager_tree(
                    current_tree, current_column
                ),
            )
            tree.column(column, width=width, minwidth=70, stretch=column in {"title", "project", "path"})
        tree.manager_headings = dict(zip(columns, headings))
        tree.manager_sort_column = None
        tree.manager_sort_descending = False
        vertical = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        horizontal = ttk.Scrollbar(frame, orient="horizontal", command=tree.xview)
        tree.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        tree.grid(row=0, column=0, sticky="nsew")
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        return tree

    @staticmethod
    def _manager_sort_value(column, value):
        text = str(value or "").strip()
        if column in {"size", "images", "conversation_size", "stored"}:
            try:
                number, unit = text.upper().split()
                factor = {
                    "B": 1,
                    "KB": 1024,
                    "MB": 1024 ** 2,
                    "GB": 1024 ** 3,
                    "TB": 1024 ** 4,
                }[unit]
                return float(number.replace(",", "")) * factor
            except (KeyError, TypeError, ValueError):
                return -1.0
        if column in {"threads", "occurrences"}:
            try:
                return float(text.replace(",", ""))
            except ValueError:
                return -1.0
        return text.casefold()

    def _sort_manager_tree(self, tree, column, descending=None):
        if descending is None:
            descending = bool(
                tree.manager_sort_column == column and not tree.manager_sort_descending
            )
        rows = list(tree.get_children())
        rows.sort(
            key=lambda item_id: self._manager_sort_value(column, tree.set(item_id, column)),
            reverse=descending,
        )
        for position, item_id in enumerate(rows):
            tree.move(item_id, "", position)
        tree.manager_sort_column = column
        tree.manager_sort_descending = descending
        for current_column, heading in tree.manager_headings.items():
            indicator = " ↓" if descending else " ↑"
            tree.heading(
                current_column,
                text=heading + indicator if current_column == column else heading,
            )

    def _reapply_manager_tree_sort(self, tree):
        if tree.manager_sort_column:
            self._sort_manager_tree(
                tree,
                tree.manager_sort_column,
                descending=tree.manager_sort_descending,
            )

    def _selection_buttons(self, parent, tree):
        ttk.Button(parent, text="全选", command=lambda: tree.selection_set(tree.get_children())).pack(side="left")
        ttk.Button(parent, text="全不选", command=lambda: tree.selection_remove(tree.selection())).pack(side="left", padx=6)

        def invert():
            current = set(tree.selection())
            tree.selection_set([item for item in tree.get_children() if item not in current])

        ttk.Button(parent, text="反选", command=invert).pack(side="left")

    def show_content_manager(self):
        self.clear()
        self.nav()
        self.title_block("内容管理", "扫描对话、项目关联和内嵌图片；所有清理操作都先创建可恢复备份。")
        self.manager_codex_home = tk.StringVar(value=str(Path.home() / ".codex"))
        self.manager_summary = tk.StringVar(value="尚未扫描")
        self.manager_compatibility = tk.StringVar(value="Codex 兼容性：等待扫描")
        self.manager_path_health = tk.StringVar(value="路径健康：等待扫描")
        self.manager_inventory = None
        self.manager_base_summary = ""
        self.manager_project_filter = tk.StringVar(value="全部项目")
        self.manager_hide_archived = tk.BooleanVar(value=True)
        self.manager_project_filter_paths = {}
        self.content_scanning = False
        self.manager_busy = False
        top = ttk.Frame(self.content)
        top.pack(fill="x", pady=(0, 8))
        ttk.Label(top, text="Codex 数据位置", width=14).pack(side="left")
        ttk.Entry(top, textvariable=self.manager_codex_home).pack(side="left", fill="x", expand=True)
        ttk.Button(top, text="选择...", command=lambda: self.choose_dir(self.manager_codex_home)).pack(side="left", padx=8)
        ttk.Checkbutton(
            top,
            text="隐藏归档对话和图片",
            variable=self.manager_hide_archived,
            command=self.toggle_managed_archived_visibility,
        ).pack(side="left", padx=(0, 8))
        self.manager_scan_button = ttk.Button(top, text="扫描内容", command=self.scan_managed_content)
        self.manager_scan_button.pack(side="left")
        consistency_row = ttk.Frame(self.content)
        consistency_row.pack(fill="x", pady=(0, 4))
        ttk.Label(consistency_row, textvariable=self.manager_summary, foreground="#555555").pack(
            side="left", fill="x", expand=True
        )
        compatibility_row = ttk.Frame(self.content)
        compatibility_row.pack(fill="x", pady=(0, 4))
        self.manager_compatibility_label = ttk.Label(
            compatibility_row,
            textvariable=self.manager_compatibility,
            foreground="#555555",
        )
        self.manager_compatibility_label.pack(side="left", fill="x", expand=True)
        health_row = ttk.Frame(self.content)
        health_row.pack(fill="x", pady=(0, 8))
        self.manager_consistency_button = ttk.Button(
            health_row, text="一致性报告", command=self.show_manager_consistency, state="disabled"
        )
        self.manager_consistency_button.pack(side="right", padx=(7, 0))
        self.manager_clean_stale_button = ttk.Button(
            health_row, text="清理侧栏残留", command=self.clean_stale_sidebar_metadata, state="disabled"
        )
        self.manager_clean_stale_button.pack(side="right", padx=(7, 0))
        self.manager_repair_paths_button = ttk.Button(
            health_row, text="修复路径问题", command=self.repair_managed_rollout_paths, state="disabled"
        )
        self.manager_repair_paths_button.pack(side="right", padx=(12, 0))
        self.manager_path_health_label = ttk.Label(
            health_row, textvariable=self.manager_path_health, foreground="#555555"
        )
        self.manager_path_health_label.pack(side="left", fill="x", expand=True)

        notebook = ttk.Notebook(self.content)
        self.manager_notebook = notebook
        notebook.pack(fill="both", expand=True)
        conversation_tab = ttk.Frame(notebook, padding=8)
        project_tab = ttk.Frame(notebook, padding=8)
        image_tab = ttk.Frame(notebook, padding=8)
        notebook.add(conversation_tab, text="对话")
        notebook.add(project_tab, text="项目")
        notebook.add(image_tab, text="图片")

        conversation_filter_row = ttk.Frame(conversation_tab)
        conversation_filter_row.pack(fill="x", pady=(0, 7))
        ttk.Label(conversation_filter_row, text="项目分类").pack(side="left")
        self.manager_project_filter_combo = ttk.Combobox(
            conversation_filter_row,
            textvariable=self.manager_project_filter,
            state="disabled",
            width=48,
        )
        self.manager_project_filter_combo.pack(side="left", padx=(8, 0))
        self.manager_project_filter_combo.bind("<<ComboboxSelected>>", self.filter_managed_conversations)

        conversation_controls = ttk.Frame(conversation_tab)
        conversation_controls.pack(fill="x", pady=(0, 7))
        self.conversation_tree = self._manager_tree(
            conversation_tab,
            ("title", "project_name", "provider", "project", "updated", "size", "images", "status", "title_source"),
            ("Codex 显示名称", "项目", "Provider", "项目目录", "最近更新", "大小", "图片占用", "状态", "名称来源"),
            (220, 135, 85, 220, 135, 80, 95, 145, 190),
        )
        self._selection_buttons(conversation_controls, self.conversation_tree)
        self.manager_preview_conversation_button = ttk.Button(
            conversation_controls, text="预览对话", command=self.preview_managed_conversation, state="disabled"
        )
        self.manager_preview_conversation_button.pack(side="left", padx=(12, 0))
        self.conversation_tree.bind("<Double-1>", self.preview_managed_conversation)
        self.manager_archive_conversations_button = ttk.Button(
            conversation_controls, text="归档所选", command=lambda: self.change_managed_archive_state(True), state="disabled"
        )
        self.manager_archive_conversations_button.pack(side="right", padx=(7, 0))
        self.manager_unarchive_conversations_button = ttk.Button(
            conversation_controls, text="复原所选归档", command=lambda: self.change_managed_archive_state(False), state="disabled"
        )
        self.manager_unarchive_conversations_button.pack(side="right", padx=(7, 0))
        self.manager_delete_conversations_button = ttk.Button(
            conversation_controls, text="备份并删除所选对话", command=self.delete_managed_conversations, state="disabled"
        )
        self.manager_delete_conversations_button.pack(side="right")

        project_controls = ttk.Frame(project_tab)
        project_controls.pack(fill="x", pady=(0, 7))
        self.project_manager_tree = self._manager_tree(
            project_tab,
            ("path", "threads", "conversation_size", "images", "updated", "exists", "duplicates"),
            ("项目目录", "关联对话", "对话大小", "图片占用", "最近更新", "目录状态", "同名候选"),
            (330, 80, 90, 90, 140, 75, 80),
        )
        self._selection_buttons(project_controls, self.project_manager_tree)
        ttk.Button(project_controls, text="打开项目", command=self.open_managed_project).pack(side="left", padx=(12, 0))
        self.manager_delete_project_threads_button = ttk.Button(
            project_controls, text="删除关联对话", command=self.delete_project_conversations, state="disabled"
        )
        self.manager_delete_project_threads_button.pack(side="right")
        self.manager_archive_projects_button = ttk.Button(
            project_controls, text="项目移入回收区", command=self.archive_managed_projects, state="disabled"
        )
        self.manager_archive_projects_button.pack(side="right", padx=7)
        self.manager_full_delete_projects_button = ttk.Button(
            project_controls, text="彻底删除项目", command=self.fully_delete_managed_project, state="disabled"
        )
        self.manager_full_delete_projects_button.pack(side="right", padx=7)
        self.manager_restore_project_button = ttk.Button(
            project_controls, text="恢复最近移除项目", command=self.restore_latest_project
        )
        self.manager_restore_project_button.pack(side="right")
        self.project_manager_tree.bind("<<TreeviewSelect>>", self.update_project_action_buttons)

        image_controls = ttk.Frame(image_tab)
        image_controls.pack(fill="x", pady=(0, 7))
        self.image_manager_tree = self._manager_tree(
            image_tab,
            ("conversation", "size", "occurrences", "stored", "updated", "type", "risk"),
            ("所属对话", "单张大小", "重复次数", "实际占用", "对话时间", "图片类型", "影响评估"),
            (265, 90, 80, 100, 135, 105, 160),
        )
        self._selection_buttons(image_controls, self.image_manager_tree)
        ttk.Button(image_controls, text="选择浏览器截图", command=self.select_browser_screenshots).pack(side="left", padx=(12, 0))
        ttk.Button(image_controls, text="选择低风险图片", command=self.select_low_risk_images).pack(side="left", padx=6)
        self.manager_preview_image_button = ttk.Button(
            image_controls, text="预览图片", command=self.preview_managed_image, state="disabled"
        )
        self.manager_preview_image_button.pack(side="left", padx=(6, 0))
        self.manager_clean_duplicates_button = ttk.Button(
            image_controls, text="保留1份，清理重复图片", command=lambda: self.clean_managed_images(keep_one=True), state="disabled"
        )
        self.manager_clean_duplicates_button.pack(side="right", padx=6)
        self.manager_clean_images_button = ttk.Button(
            image_controls, text="备份并彻底清理图片", command=self.clean_managed_images, state="disabled"
        )
        self.manager_clean_images_button.pack(side="right")
        self.manager_action_buttons = [
            self.manager_preview_conversation_button,
            self.manager_archive_conversations_button,
            self.manager_unarchive_conversations_button,
            self.manager_delete_conversations_button,
            self.manager_delete_project_threads_button,
            self.manager_archive_projects_button,
            self.manager_full_delete_projects_button,
            self.manager_restore_project_button,
            self.manager_preview_image_button,
            self.manager_clean_duplicates_button,
            self.manager_clean_images_button,
            self.manager_consistency_button,
            self.manager_clean_stale_button,
            self.manager_repair_paths_button,
        ]
        self.manager_mutation_buttons = [
            self.manager_archive_conversations_button,
            self.manager_unarchive_conversations_button,
            self.manager_delete_conversations_button,
            self.manager_delete_project_threads_button,
            self.manager_archive_projects_button,
            self.manager_full_delete_projects_button,
            self.manager_restore_project_button,
            self.manager_clean_duplicates_button,
            self.manager_clean_images_button,
            self.manager_clean_stale_button,
            self.manager_repair_paths_button,
        ]

    def scan_managed_content(self):
        if self.content_scanning or self.manager_busy:
            return
        self.content_scanning = True
        self.manager_scan_button.configure(state="disabled")
        for button in self.manager_action_buttons:
            button.configure(state="disabled")
        self.manager_summary.set("正在扫描对话、项目和图片占用；大型会话可能需要一些时间...")
        self.manager_compatibility.set("Codex 兼容性：正在识别存储协议和项目模型...")
        self.manager_compatibility_label.configure(foreground="#555555")
        self.manager_path_health.set("路径健康：正在检查会话路径、项目注册路径和数据库触发器...")
        self.manager_path_health_label.configure(foreground="#555555")
        self.run(
            "正在扫描内容...",
            lambda: content_manager.scan_content(Path(self.manager_codex_home.get())),
            self.complete_content_scan,
        )

    def complete_content_scan(self, inventory):
        self.content_scanning = False
        self.manager_scan_button.configure(state="normal")
        inventory.setdefault("consistency", {
            "catalog_available": False,
            "catalog_visible": 0,
            "stale_catalog": [],
            "stale_catalog_ids": [],
            "state_only_ids": [],
            "index_only_ids": [],
            "orphan_rollout_ids": [],
            "missing_file_ids": [],
        })
        inventory.setdefault("path_health", {
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
            "registered_projects": [],
        })
        inventory.setdefault("compatibility", {
            "status": "supported",
            "write_compatible": True,
            "blockers": [],
            "warnings": [],
            "state_schema_version": None,
            "history_schema_version": None,
            "project_storage_mode": "legacy_or_empty",
            "native_project_count": 0,
            "global_project_count": 0,
        })
        self.manager_inventory = inventory
        previous_project_path = self.manager_project_filter_paths.get(self.manager_project_filter.get())
        for tree in (self.conversation_tree, self.project_manager_tree, self.image_manager_tree):
            tree.delete(*tree.get_children())
        self.manager_conversations = {item["task_id"]: item for item in inventory["conversations"]}
        self.manager_projects = {}
        for number, item in enumerate(inventory["projects"]):
            item_id = f"project-{number}"
            self.manager_projects[item_id] = item
            if item.get("registered"):
                if item.get("thread_count", 0):
                    project_status = "已注册，存在" if item.get("exists") else "已注册，目录不存在"
                else:
                    project_status = "已注册，暂无聊天" if item.get("exists") else "已注册，目录不存在"
            else:
                project_status = "存在" if item.get("exists") else "已不存在"
            self.project_manager_tree.insert(
                "", "end", iid=item_id,
                values=(
                    item["path"], item["thread_count"], self.format_size(item["conversation_bytes"]),
                    self.format_size(item["image_bytes"]), item["latest_updated_at"][:19],
                    project_status,
                    item.get("possible_duplicates") or "-",
                ),
            )
        self._reapply_manager_tree_sort(self.project_manager_tree)
        self.refresh_managed_images()
        summary = inventory["summary"]
        missing_note = f"；数据库中另有 {summary['missing_files']} 个会话文件缺失" if summary.get("missing_files") else ""
        path_health = inventory["path_health"]
        conversation_path_count = len(path_health.get("extended_paths", []))
        project_path_count = len(path_health.get("project_extended_paths", []))
        duplicate_project_count = len(path_health.get("duplicate_projects", []))
        blocked_duplicate_count = len(path_health.get("blocked_duplicate_projects", []))
        path_issue_count = conversation_path_count + project_path_count + duplicate_project_count + blocked_duplicate_count
        repairable_count = (
            len(path_health.get("repairable_paths", []))
            + len(path_health.get("repairable_project_paths", []))
            + duplicate_project_count
            + len(path_health.get("removable_projects", []))
        )
        blocked_count = (
            len(path_health.get("blocked_paths", []))
            + len(path_health.get("blocked_project_paths", []))
            + blocked_duplicate_count
        )
        trigger_count = len(path_health.get("normalization_triggers", []))
        if path_issue_count or trigger_count:
            self.manager_path_health.set(
                f"路径健康：发现扩展路径 {conversation_path_count + project_path_count} 个"
                f"（会话 {conversation_path_count}，项目 {project_path_count}）；"
                f"同路径重复项目 {duplicate_project_count + blocked_duplicate_count} 组；"
                f"可处理 {repairable_count}，需人工检查 {blocked_count}；"
                f"遗留触发器 {trigger_count} 个"
            )
            self.manager_path_health_label.configure(foreground="#9A5B00")
        else:
            self.manager_path_health.set("路径健康：正常，未发现扩展路径或遗留触发器")
            self.manager_path_health_label.configure(foreground="#26734D")
        if inventory["consistency"].get("catalog_available"):
            catalog_note = (
                f"；Codex 侧栏目录 {summary['catalog_visible']} 个"
                f"（残留 {summary['stale_catalog']} 个）"
            )
        else:
            catalog_note = "；未发现 Codex 侧栏目录库"
        self.manager_base_summary = (
            f"可用对话 {summary['conversations']} 个{catalog_note}；项目关联 {summary['projects']} 个；"
            f"不同图片 {summary['unique_images']} 张，共出现 {summary['image_occurrences']} 次；"
            f"估算图片占用 {self.format_size(summary['image_bytes'])}{missing_note}"
        )
        compatibility = inventory["compatibility"]
        storage_labels = {
            "dual": "双存储合并",
            "state_db": "新版项目表",
            "transitioning": "项目存储过渡模式",
            "global_state": "全局项目注册",
            "state_db_empty": "新版项目表（空）",
            "legacy_or_empty": "旧版或未初始化",
        }
        state_version = compatibility.get("state_schema_version")
        history_version = compatibility.get("history_schema_version")
        protocol_text = f"状态库 {state_version}" if state_version is not None else "状态库按字段识别"
        history_text = f"分页历史 {history_version}" if history_version is not None else "无独立分页历史"
        storage_text = storage_labels.get(
            compatibility.get("project_storage_mode"),
            str(compatibility.get("project_storage_mode") or "未知"),
        )
        if compatibility.get("status") == "supported":
            warning_text = "；" + compatibility["warnings"][0] if compatibility.get("warnings") else ""
            self.manager_compatibility.set(
                f"Codex 兼容性：{protocol_text}；{storage_text}；{history_text}；写入协议已验证{warning_text}"
            )
            self.manager_compatibility_label.configure(
                foreground="#9A5B00" if compatibility.get("warnings") else "#26734D"
            )
        elif compatibility.get("status") == "partial":
            self.manager_compatibility.set(
                f"Codex 兼容性：{protocol_text}；{storage_text}；{history_text}；部分写入受限，需要 App Server 的操作已禁用"
            )
            self.manager_compatibility_label.configure(foreground="#9A5B00")
        else:
            blocker_text = "；".join(compatibility.get("blockers", [])) or "检测到未知协议"
            self.manager_compatibility.set(
                f"Codex 兼容性：只读保护；{blocker_text}。预览可用，写入操作已禁用"
            )
            self.manager_compatibility_label.configure(foreground="#B42318")
        self.configure_manager_project_filter(previous_project_path)
        for button in self.manager_action_buttons:
            button.configure(state="normal")
        if not inventory["consistency"].get("stale_catalog_ids"):
            self.manager_clean_stale_button.configure(state="disabled")
        if not any((
            path_health.get("repairable_paths"),
            path_health.get("actionable_project_registrations"),
            path_health.get("normalization_triggers"),
        )):
            self.manager_repair_paths_button.configure(state="disabled")
        self.update_project_action_buttons()
        self._apply_manager_compatibility_lock()
        self.status.set("内容扫描完成")

    def _apply_manager_compatibility_lock(self):
        if not self.manager_inventory:
            return
        compatibility = self.manager_inventory.get("compatibility", {})
        capabilities = compatibility.get("operation_capabilities")
        # Older scan payloads did not include per-operation capabilities.
        # Preserve their established behavior unless the whole profile is
        # explicitly read-only; current scans always provide this mapping.
        if capabilities is None:
            if not compatibility.get("write_compatible", True):
                for button in self.manager_mutation_buttons:
                    button.configure(state="disabled")
            return
        button_capabilities = {
            self.manager_archive_conversations_button: "thread_lifecycle",
            self.manager_unarchive_conversations_button: "thread_lifecycle",
            self.manager_delete_conversations_button: "thread_lifecycle",
            self.manager_delete_project_threads_button: "thread_lifecycle",
            self.manager_archive_projects_button: "project_registry",
            self.manager_full_delete_projects_button: "full_project_delete",
            self.manager_restore_project_button: "project_registry",
            self.manager_clean_duplicates_button: "conversation_content",
            self.manager_clean_images_button: "conversation_content",
            self.manager_clean_stale_button: "sidebar_cleanup",
            self.manager_repair_paths_button: "path_repair",
        }
        for button, capability in button_capabilities.items():
            if not button.winfo_exists():
                continue
            if not compatibility.get("write_compatible", True) or not capabilities.get(capability, False):
                button.configure(state="disabled")

    def refresh_managed_images(self):
        if not self.manager_inventory:
            return
        self.image_manager_tree.delete(*self.image_manager_tree.get_children())
        self.manager_images = {}
        visible_images = [
            item for item in self.manager_inventory["images"]
            if (
                not self.manager_hide_archived.get()
                or not item.get("archived")
                or not item.get("archive_consistent", True)
            )
        ]
        for number, item in enumerate(visible_images):
            item_id = f"image-{number}"
            self.manager_images[item_id] = item
            kind_labels = {
                "browser_screenshot": "浏览器截图",
                "user_image": "用户图片",
                "tool_image": "工具图片",
            }
            kinds = "、".join(kind_labels.get(kind, kind) for kind in item.get("kinds", []))
            self.image_manager_tree.insert(
                "", "end", iid=item_id,
                values=(
                    item["title"], self.format_size(item["size_bytes"]), item["occurrences"],
                    self.format_size(item["stored_bytes"]), item["updated_at"][:19], kinds or item["mime_type"],
                    f"{item['risk_level']}风险",
                ),
            )
        self._reapply_manager_tree_sort(self.image_manager_tree)

    def configure_manager_project_filter(self, previous_project_path=None):
        grouped = {}
        project_metadata = {}
        for project in self.manager_inventory.get("projects", []):
            path = str(project.get("path") or "")
            if path:
                project_metadata[path] = project
        visible_conversations = [
            item for item in self.manager_conversations.values()
            if (
                not self.manager_hide_archived.get()
                or not item.get("archived")
                or not item.get("archive_consistent", True)
            )
        ]
        for item in visible_conversations:
            grouped.setdefault(item.get("project_path", ""), []).append(item)
        # Keep registered projects in the selector even when no conversation
        # currently points at their path.
        for path, project in project_metadata.items():
            if project.get("registered"):
                grouped.setdefault(path, [])
        stale_grouped = {}
        for item in self.manager_inventory.get("consistency", {}).get("stale_catalog", []):
            if self.manager_hide_archived.get() and item.get("archived"):
                continue
            stale_grouped.setdefault(item.get("project_path", ""), []).append(item)
        name_counts = {}
        for path, items in grouped.items():
            metadata = project_metadata.get(path, {})
            name = (
                (items[0].get("project_name") if items else "")
                or metadata.get("project_name")
                or (metadata.get("registration_names") or [""])[0]
                or (Path(path).name if path else "未关联")
            )
            name_counts[name.casefold()] = name_counts.get(name.casefold(), 0) + 1
        consistency = self.manager_inventory.get("consistency", {})
        if self.manager_hide_archived.get():
            hidden = len(self.manager_conversations) - len(visible_conversations)
            all_label = f"全部项目 (未归档 {len(visible_conversations)}，已隐藏 {hidden})"
        elif consistency.get("catalog_available"):
            visible_count = self.manager_inventory.get("summary", {}).get("catalog_visible", 0)
            all_label = f"全部项目 (可用 {len(self.manager_conversations)}，侧栏 {visible_count})"
        else:
            all_label = f"全部项目 (可用 {len(self.manager_conversations)})"
        labels = [all_label]
        mapping = {all_label: None}
        selected_label = all_label
        entries = []
        for path, items in grouped.items():
            metadata = project_metadata.get(path, {})
            name = (
                (items[0].get("project_name") if items else "")
                or metadata.get("project_name")
                or (metadata.get("registration_names") or [""])[0]
                or (Path(path).name if path else "未关联")
            )
            stale_count = len(stale_grouped.get(path, []))
            count_label = "未归档" if self.manager_hide_archived.get() else "可用"
            if metadata.get("registered") and not items:
                label = f"{name} (已注册，暂无聊天"
            else:
                label = f"{name} ({count_label} {len(items)}"
                if metadata.get("registered"):
                    label += "，已注册"
            if stale_count:
                label += f"，残留 {stale_count}"
            label += ")"
            if path and name_counts[name.casefold()] > 1:
                label = f"{name} | {path} ({len(items)})"
            entries.append((name.casefold(), label, path))
        for _name, label, path in sorted(entries):
            labels.append(label)
            mapping[label] = path
            if previous_project_path is not None and path == previous_project_path:
                selected_label = label
        self.manager_project_filter_paths = mapping
        self.manager_project_filter_combo.configure(values=labels, state="readonly")
        self.manager_project_filter.set(selected_label)
        self.filter_managed_conversations()

    def filter_managed_conversations(self, _event=None):
        if not hasattr(self, "conversation_tree") or not self.conversation_tree.winfo_exists():
            return
        selected_path = self.manager_project_filter_paths.get(self.manager_project_filter.get())
        items = list(self.manager_conversations.values())
        hidden_archived = sum(
            bool(item.get("archived")) and item.get("archive_consistent", True)
            for item in items
        )
        if self.manager_hide_archived.get():
            items = [
                item
                for item in items
                if not item.get("archived") or not item.get("archive_consistent", True)
            ]
        if selected_path is not None:
            items = [item for item in items if item.get("project_path", "") == selected_path]
        else:
            items = sorted(items, key=lambda item: (item.get("project_name") or "未关联").casefold())
        self.conversation_tree.delete(*self.conversation_tree.get_children())
        for item in items:
            task_id = item["task_id"]
            self.conversation_tree.insert(
                "", "end", iid=task_id,
                values=(
                    item["title"], item.get("project_name") or "未关联", item["provider"],
                    item.get("project_path") or "未关联", item["updated_at"][:19],
                    self.format_size(item["size_bytes"]), self.format_size(item["image_bytes"]),
                    (
                        item.get("archive_state", "已归档" if item["archived"] else "使用中")
                        + (f" / 可能重复×{item['possible_duplicates']}" if item.get("possible_duplicates") else "")
                    ),
                    item.get("title_source", "未知"),
                ),
            )
        self._reapply_manager_tree_sort(self.conversation_tree)
        stale_count = 0
        if selected_path is not None:
            stale_count = sum(
                item.get("project_path", "") == selected_path
                for item in self.manager_inventory.get("consistency", {}).get("stale_catalog", [])
            )
        if selected_path is not None:
            project_total = sum(
                item.get("project_path", "") == selected_path for item in self.manager_conversations.values()
            )
            project_hidden = project_total - len(items)
            filter_note = f"；当前项目显示 {len(items)} 个"
            if self.manager_hide_archived.get() and project_hidden:
                filter_note += f"，已隐藏归档 {project_hidden} 个"
        elif self.manager_hide_archived.get() and hidden_archived:
            filter_note = f"；已隐藏归档 {hidden_archived} 个"
        else:
            filter_note = ""
        if stale_count:
            filter_note += f"，Codex 侧栏另有残留 {stale_count} 个"
        self.manager_summary.set(f"{self.manager_base_summary}{filter_note}")

    def toggle_managed_archived_visibility(self):
        if not self.manager_inventory:
            return
        previous_path = self.manager_project_filter_paths.get(self.manager_project_filter.get())
        self.configure_manager_project_filter(previous_path)
        self.refresh_managed_images()

    def select_browser_screenshots(self):
        selected = [
            item_id for item_id, item in self.manager_images.items()
            if "browser_screenshot" in item.get("kinds", [])
        ]
        self.image_manager_tree.selection_set(selected)
        self.manager_summary.set(f"已选择 {len(selected)} 张浏览器截图；清理前可逐张预览。")

    def select_low_risk_images(self):
        selected = [item_id for item_id, item in self.manager_images.items() if item.get("safe_to_clean")]
        self.image_manager_tree.selection_set(selected)
        self.manager_summary.set(f"已选择 {len(selected)} 张低风险重复图片；仍建议先预览。")

    def show_manager_consistency(self):
        if not self.manager_inventory:
            messagebox.showwarning(APP_NAME, "请先扫描内容。")
            return
        consistency = self.manager_inventory["consistency"]
        lines = [
            "数据一致性报告",
            "=" * 56,
            "可用对话：主数据库有记录，并且 rollout 文件存在。",
            "侧栏残留：Codex 侧栏仍有可见记录，但主数据库已无该任务，或任务已经归档。",
            "",
            f"Codex 侧栏目录：{consistency['catalog_visible']} 个",
            f"侧栏残留：{len(consistency['stale_catalog_ids'])} 个",
            f"主库有记录但侧栏目录缺失：{len(consistency['state_only_ids'])} 个",
            f"会话索引孤儿：{len(consistency['index_only_ids'])} 个",
            f"rollout 文件孤儿：{len(consistency['orphan_rollout_ids'])} 个",
            f"主库记录指向缺失文件：{len(consistency['missing_file_ids'])} 个",
        ]
        path_health = self.manager_inventory.get("path_health", {})
        lines.extend((
            "",
            "会话路径健康",
            "-" * 56,
            f"Windows 扩展路径：{len(path_health.get('extended_paths', []))} 个",
            f"可安全修复：{len(path_health.get('repairable_paths', []))} 个",
            f"需人工检查：{len(path_health.get('blocked_paths', []))} 个",
            f"遗留数据库触发器：{len(path_health.get('normalization_triggers', []))} 个",
        ))
        for item in path_health.get("extended_paths", []):
            lines.extend((
                "",
                f"任务 ID：{item['task_id']}",
                f"原路径：{item['raw_path']}",
                f"规范路径：{item.get('normalized_path') or '无法确定'}",
                f"判断：{item['reason']}",
            ))
        lines.extend((
            "",
            "项目注册路径健康",
            "-" * 56,
            f"Windows 扩展路径：{len(path_health.get('project_extended_paths', []))} 个",
            f"可规范化：{len(path_health.get('repairable_project_paths', []))} 个",
            f"可合并同路径重复项目：{len(path_health.get('duplicate_projects', []))} 组",
            f"存在未知引用、禁止自动合并：{len(path_health.get('blocked_duplicate_projects', []))} 组",
            f"可移除残留项目：{len(path_health.get('removable_projects', []))} 个",
            f"需人工检查：{len(path_health.get('blocked_project_paths', []))} 个",
        ))
        for item in path_health.get("project_extended_paths", []):
            lines.extend((
                "",
                f"项目：{item['project_name']}",
                f"项目 ID：{item['project_id']}",
                f"原路径：{item['raw_path']}",
                f"规范路径：{item.get('normalized_path') or '无法确定'}",
                f"关联对话：{item.get('linked_tasks', 0)} 个",
                f"判断：{item['reason']}",
            ))
        for item in path_health.get("duplicate_projects", []):
            lines.extend((
                "",
                f"重复项目合并：{item['keeper_name']}",
                f"保留项目 ID：{item['keeper_id']}",
                f"合并并移除 ID：{', '.join(item['remove_ids'])}",
                f"规范目录：{'; '.join(item['normalized_paths'])}",
                f"判断：{item['reason']}",
            ))
        for item in path_health.get("blocked_duplicate_projects", []):
            unknown = "; ".join(
                f"{project_id}: {', '.join(paths)}"
                for project_id, paths in item.get("unknown_references", {}).items()
            )
            lines.extend((
                "",
                f"禁止自动合并：{item['keeper_name']}",
                f"涉及项目 ID：{', '.join([item['keeper_id']] + item['remove_ids'])}",
                f"未知引用：{unknown or '未列出'}",
                f"判断：{item['reason']}",
            ))
        if path_health.get("normalization_triggers"):
            lines.extend(("", "检测到的遗留触发器", *path_health["normalization_triggers"]))
        if consistency["stale_catalog"]:
            lines.extend(("", "侧栏残留明细", "-" * 56))
            for item in consistency["stale_catalog"]:
                lines.extend((
                    f"标题：{item['title']}",
                    f"项目：{item['project_path'] or '未关联'}",
                    f"任务 ID：{item['task_id']}",
                    f"原目录记录：{item['source_detail'] or '无'}",
                    "",
                ))
        for label, key in (
            ("主库有记录但侧栏目录缺失", "state_only_ids"),
            ("会话索引孤儿", "index_only_ids"),
            ("rollout 文件孤儿", "orphan_rollout_ids"),
            ("主库记录指向缺失文件", "missing_file_ids"),
        ):
            if consistency[key]:
                lines.extend(("", label, "-" * 56, *consistency[key]))
        self.show_report_window("数据一致性报告", "\n".join(lines))

    def repair_managed_rollout_paths(self):
        if not self.manager_inventory:
            messagebox.showwarning(APP_NAME, "请先扫描内容。")
            return
        health = self.manager_inventory.get("path_health", {})
        repairable = health.get("repairable_paths", [])
        blocked = health.get("blocked_paths", [])
        triggers = health.get("normalization_triggers", [])
        project_repairable = health.get("repairable_project_paths", [])
        duplicate_projects = health.get("duplicate_projects", [])
        blocked_projects = health.get("blocked_project_paths", [])
        blocked_duplicates = health.get("blocked_duplicate_projects", [])
        removable_projects = health.get("removable_projects", [])
        if not any((
            repairable,
            health.get("actionable_project_registrations"),
            triggers,
        )):
            messagebox.showinfo(APP_NAME, "没有能够自动处理的会话或项目路径问题。")
            return
        project_registrations = health.get("actionable_project_registrations") or []
        if project_registrations:
            decisions = self.choose_path_repair_actions(health)
            if decisions is None:
                return
        else:
            # A scan can find only rollout paths or legacy triggers. Do not open an empty project dialog.
            decisions = {
                "actions": {},
                "names": {},
                "paths": {},
                "full_delete_ids": [],
                "repair_conversations": bool(repairable),
                "remove_triggers": bool(triggers),
            }
        actions = decisions["actions"]
        selected_normalize = sum(value == "normalize" for value in actions.values())
        selected_repoint = sum(value == "repoint" for value in actions.values())
        selected_remove = sum(value == "delete" for value in actions.values())
        selected_rename = len(decisions.get("names", {}))
        full_delete_ids = decisions.get("full_delete_ids", [])
        selected_conversations = len(repairable) if decisions["repair_conversations"] else 0
        selected_triggers = len(triggers) if decisions["remove_triggers"] else 0
        if full_delete_ids:
            project_id = full_delete_ids[0]
            item = next(
                row for row in health.get("actionable_project_registrations", [])
                if row["project_id"] == project_id
            )
            paths = "\n".join(
                root.get("normalized_path") or root.get("raw_path") or ""
                for root in item.get("roots", [])
            )
            task_lines = "\n".join(
                f"• {task.get('title') or task['task_id']}"
                for task in item.get("related_tasks", [])[:12]
            ) or "• 无关联对话"
            if not messagebox.askyesno(
                APP_NAME,
                f"将彻底删除项目：{item['project_name']}\n"
                f"项目 ID：{project_id}\n项目目录：\n{paths}\n\n"
                f"关联对话（共 {len(item.get('related_tasks', []))} 个）：\n{task_lines}\n\n"
                "执行内容：删除该项目注册；完整备份后删除关联对话；"
                "把项目目录移入软件的可恢复区。Codex 必须完全关闭。是否继续？",
            ):
                return
            self._begin_manager_action()
            self.run(
                "正在完整备份并删除项目...",
                lambda: content_manager.fully_delete_registered_project(
                    Path(self.manager_codex_home.get()), project_id
                ),
                lambda result: self.complete_manager_action(
                    result,
                    f"已彻底删除项目注册和 {result['deleted_conversations']} 个关联对话；"
                    f"项目文件已移入可恢复区。\n项目回收区：{result['trash_root']}\n"
                    f"注册备份：{result['registration_backup']}\n"
                    f"对话备份：{result['conversation_backup'] or '无关联对话'}",
                ),
            )
            return
        if not any((
            selected_normalize,
            selected_repoint,
            selected_remove,
            selected_rename,
            selected_conversations,
            selected_triggers,
        )):
            messagebox.showinfo(APP_NAME, "没有选择任何处理操作，未修改数据。")
            return
        blocked_note = (
            f"\n另有 {len(blocked) + len(blocked_projects)} 条无法确认的路径、"
            f"{len(blocked_duplicates)} 组未知引用重复项目只会报告，不会修改。"
            if blocked or blocked_projects or blocked_duplicates else ""
        )
        trigger_note = (
            f"\n将移除 {selected_triggers} 个非 Codex 官方的永久路径触发器，避免干扰后续升级。"
            if selected_triggers else ""
        )
        if not messagebox.askyesno(
            APP_NAME,
            f"将修复 {selected_conversations} 条已经验证到实际会话文件的扩展路径，"
            f"规范化 {selected_normalize} 个项目，"
            f"更正 {selected_repoint} 个项目目录，"
            f"删除 {selected_remove} 条项目注册（重复项的已知引用会迁移到保留项），"
            f"修改 {selected_rename} 个侧栏显示名称。"
            f"{trigger_note}{blocked_note}\n\n"
            "操作前会完整备份 state_5.sqlite 和 Codex 全局项目状态，并把每条修改写入事务记录。\n"
            "Codex 必须完全关闭。是否继续？",
        ):
            return
        self._begin_manager_action()
        self.run(
            "正在备份并修复会话及项目路径...",
            lambda: content_manager.repair_rollout_path_health(
                Path(self.manager_codex_home.get()),
                remove_normalization_triggers=decisions["remove_triggers"],
                selected_project_actions=actions,
                selected_project_names=decisions.get("names", {}),
                selected_project_paths=decisions.get("paths", {}),
                repair_conversation_paths=decisions["repair_conversations"],
            ),
            lambda result: self.complete_manager_action(
                result,
                f"已修复 {result['repaired']} 条会话路径；"
                f"规范化 {result.get('project_paths_repaired', 0)} 条项目路径；"
                f"更正 {result.get('project_paths_repointed', 0)} 个项目目录；"
                f"合并 {result.get('duplicate_projects_merged', 0)} 组重复项目；"
                f"移除 {result.get('stale_projects_removed', 0)} 个残留项目；"
                f"主动移除 {result.get('project_registrations_removed', 0)} 个侧栏注册；"
                f"修改 {result.get('project_names_changed', 0)} 个侧栏名称；"
                f"移除 {len(result['triggers_removed'])} 个遗留触发器。\n"
                f"仍需人工检查：{result['blocked'] + result.get('project_paths_blocked', 0)} 条。\n"
                f"备份位置：{result['backup_path']}",
            ),
        )

    def clean_stale_sidebar_metadata(self):
        if not self.manager_inventory:
            messagebox.showwarning(APP_NAME, "请先扫描内容。")
            return
        task_ids = set(self.manager_inventory["consistency"].get("stale_catalog_ids", []))
        if not task_ids:
            messagebox.showinfo(APP_NAME, "没有可清理的侧栏残留。")
            return
        if not messagebox.askyesno(
            APP_NAME,
            f"将清理 {len(task_ids)} 个不应继续显示的 Codex 侧栏残留。\n\n"
            "只有主数据库和 rollout 都不存在的任务才会同步移除孤儿索引；"
            "已归档任务的兼容索引会保留。\n"
            "操作前会完整备份侧栏目录库和会话索引；不会删除任何有效对话或 rollout 文件。\n"
            "Codex 必须完全关闭。是否继续？",
        ):
            return
        self._begin_manager_action()
        self.run(
            "正在备份并清理侧栏残留...",
            lambda: content_manager.clean_stale_sidebar_entries(
                Path(self.manager_codex_home.get()), task_ids
            ),
            lambda result: self.complete_manager_action(
                result,
                f"已清理 {result['deleted']} 个侧栏残留。\n备份位置：{result['backup_path']}",
            ),
        )

    def _begin_manager_action(self):
        self.manager_busy = True
        self.manager_scan_button.configure(state="disabled")
        for button in self.manager_action_buttons:
            button.configure(state="disabled")

    def _finish_manager_action(self):
        self.manager_busy = False
        self.manager_scan_button.configure(state="normal")
        self.scan_managed_content()

    def _finish_manager_preview(self):
        self.manager_busy = False
        if hasattr(self, "manager_scan_button") and self.manager_scan_button.winfo_exists():
            self.manager_scan_button.configure(state="normal")
        for button in getattr(self, "manager_action_buttons", []):
            if button.winfo_exists():
                button.configure(state="normal")
        self._apply_manager_compatibility_lock()

    def preview_managed_conversation(self, _event=None):
        selected = self.conversation_tree.selection()
        if len(selected) != 1:
            messagebox.showwarning(APP_NAME, "请选择一个对话进行预览。")
            return
        item = self.manager_conversations[selected[0]]
        self._begin_manager_action()
        self.run(
            "正在只读加载对话预览...",
            lambda: content_manager.preview_conversation(Path(item["rollout_path"])),
            lambda preview: self.show_conversation_preview(item, preview),
        )

    def show_conversation_preview(self, item, preview):
        self._finish_manager_preview()
        display_title, title_source = content_manager.resolve_registered_display_title(
            item.get("catalog_title"),
            item.get("original_title", ""),
            preview.get("session_title"),
            preview.get("first_user_message"),
            item.get("cwd", ""),
            item["task_id"],
        )
        original_title = item.get("original_title", "").strip()
        details = [
            "此窗口仅供查看，不会修改对话、SQLite 或任何文件。",
            "",
            f"Codex 显示名称：{display_title}",
            f"名称来源：{title_source}",
        ]
        if original_title and original_title != display_title:
            details.append(f"原始标题：{original_title}")
        details.extend((
            f"任务 ID：{item['task_id']}",
            f"Provider：{item['provider']}",
            f"所在项目：{item.get('project_name') or '未关联'}",
            f"项目目录：{item.get('project_path') or '未关联'}",
            *([f"工作目录：{item['cwd']}"] if not item.get("project_path") and item.get("cwd") else []),
            f"最近更新：{item['updated_at'][:19]}",
            f"状态：{'已归档' if item['archived'] else '使用中'}",
            f"会话大小：{self.format_size(item['size_bytes'])}",
            f"图片：{item['image_count']} 张不同图片，出现 {item['image_occurrences']} 次，"
            f"占用约 {self.format_size(item['image_bytes'])}",
            f"可读消息：{preview['message_count']} 条；工具调用：{preview['tool_call_count']} 次",
        ))
        if preview.get("first_user_message"):
            details.append(f"最初请求：{preview['first_user_message']}")
        details.extend(("", "最近对话内容", "=" * 56))
        role_labels = {"user": "用户", "assistant": "代理", "tool": "工具"}
        if preview["messages"]:
            for message in preview["messages"]:
                details.extend(("", f"[{role_labels.get(message['role'], message['role'])}]", message["text"]))
        else:
            details.extend(("", "未找到可预览的用户或代理消息。"))
        if preview.get("skipped_large_lines"):
            details.extend((
                "",
                f"有 {preview['skipped_large_lines']} 个超大事件未展开，以避免工具输出占用过多内存。",
            ))
        self.show_report_window("对话只读预览", "\n".join(details))
        self.status.set("对话预览已打开")

    def change_managed_archive_state(self, archived):
        selected = list(self.conversation_tree.selection())
        if not selected:
            messagebox.showwarning(APP_NAME, "请先选择要处理的对话。")
            return
        applicable = {
            task_id for task_id in selected
            if (
                not self.manager_conversations[task_id].get("archive_consistent", True)
                or bool(self.manager_conversations[task_id]["archived"]) != archived
            )
        }
        skipped = len(selected) - len(applicable)
        action = "归档" if archived else "复原归档"
        if not applicable:
            messagebox.showinfo(APP_NAME, f"所选对话已经是目标状态，无需{action}。")
            return
        skipped_note = f"\n其中 {skipped} 个已经是目标状态，将自动跳过。" if skipped else ""
        if not messagebox.askyesno(
            APP_NAME,
            f"将{action} {len(applicable)} 个对话。{skipped_note}\n\n"
            "操作会先完整备份会话文件、SQLite 和索引；Codex 必须完全关闭。是否继续？",
        ):
            return
        self._begin_manager_action()
        self.run(
            f"正在备份并{action}所选对话...",
            lambda: content_manager.set_conversations_archived(
                Path(self.manager_codex_home.get()), applicable, archived=archived
            ),
            lambda result: self.complete_manager_action(
                result,
                f"已{action} {result['changed']} 个对话。\n备份位置：{result['backup_path']}\n\n"
                + (
                    "启动 Codex 后，官方目录同步会重新建立复原任务的侧栏记录；"
                    "若页面已经打开，请刷新或重启 Codex。"
                    if not archived
                    else "启动 Codex 后侧栏会重新读取目录；若旧页面仍显示归档任务，请刷新或重启 Codex。"
                ),
            ),
        )

    def delete_managed_conversations(self):
        selected = set(self.conversation_tree.selection())
        if not selected:
            messagebox.showwarning(APP_NAME, "请先选择要删除的对话。")
            return
        selected_items = [self.manager_conversations[task_id] for task_id in sorted(selected)]
        preview_lines = [
            f"• {item['title']}  |  {item.get('project_name') or '未关联项目'}"
            for item in selected_items[:12]
        ]
        if len(selected_items) > len(preview_lines):
            preview_lines.append(f"• 另有 {len(selected_items) - len(preview_lines)} 个对话未展开")
        if not messagebox.askyesno(
            APP_NAME,
            f"将删除 {len(selected)} 个对话：\n\n"
            + "\n".join(preview_lines)
            + "\n\n删除后，对应项目可能显示‘暂无聊天’。程序会先完整备份会话文件、数据库和索引。"
            "\nCodex 必须完全关闭。确认删除这些具体对话？",
        ):
            return
        self._begin_manager_action()
        self.run(
            "正在备份并删除所选对话...",
            lambda: content_manager.delete_conversations(Path(self.manager_codex_home.get()), selected),
            lambda result: self.complete_manager_action(
                result, f"已删除 {result['deleted']} 个对话。\n备份位置：{result['backup_path']}"
            ),
        )

    def delete_project_conversations(self):
        selected = self.project_manager_tree.selection()
        task_ids = {
            task_id
            for item_id in selected
            for task_id in self.manager_projects[item_id]["thread_ids"]
        }
        if not task_ids:
            messagebox.showwarning(APP_NAME, "所选项目没有可删除的关联对话。")
            return
        self.conversation_tree.selection_set(list(task_ids))
        self.delete_managed_conversations()

    def open_managed_project(self):
        selected = self.project_manager_tree.selection()
        if len(selected) != 1:
            messagebox.showwarning(APP_NAME, "请选择一个项目目录。")
            return
        path = Path(self.manager_projects[selected[0]]["path"])
        if not path.is_dir():
            messagebox.showerror(APP_NAME, f"项目目录不存在：\n{path}")
            return
        os.startfile(path)

    def update_project_action_buttons(self, _event=None):
        """Enable project actions only when the selected rows support them."""
        if not hasattr(self, "project_manager_tree") or not self.project_manager_tree.winfo_exists():
            return
        selected = [self.manager_projects[item_id] for item_id in self.project_manager_tree.selection()]
        can_archive = bool(selected) and all(item.get("exists") for item in selected)
        can_full_delete = (
            len(selected) == 1
            and bool(selected[0].get("exists"))
            and len(selected[0].get("registration_ids", [])) == 1
        )
        self.manager_archive_projects_button.configure(state="normal" if can_archive else "disabled")
        self.manager_full_delete_projects_button.configure(
            state="normal" if can_full_delete else "disabled"
        )
        self._apply_manager_compatibility_lock()

    def archive_managed_projects(self):
        selected = list(self.project_manager_tree.selection())
        projects = [self.manager_projects[item_id] for item_id in selected]
        if not projects:
            messagebox.showwarning(APP_NAME, "请选择要归档的项目。")
            return
        if any(not item.get("exists") for item in projects):
            messagebox.showwarning(APP_NAME, "所选项目中有目录不存在，不能归档。")
            return
        names = "\n".join(f"• {item.get('project_name') or Path(item['path']).name}: {item['path']}" for item in projects)
        if not messagebox.askyesno(
            APP_NAME,
            "将以下项目移入软件回收区：\n\n"
            f"{names}\n\n"
            "执行内容：移除 Codex 项目注册，并把项目目录移入软件的可恢复区。"
            "关联对话不会删除；如需删除对话，请使用‘删除关联对话’。"
            "操作前会备份侧栏注册。Codex 必须完全关闭。是否继续？",
        ):
            return
        self._begin_manager_action()
        self.run(
            "正在把项目移入回收区...",
            lambda: content_manager.archive_projects(
                Path(self.manager_codex_home.get()), projects
            ),
            lambda result: self.complete_manager_action(
                result,
                f"已将 {result['archived']} 个项目移入回收区；移除项目注册 {result['registration_removed']} 条。\n"
                f"项目回收区：{result['trash_root']}\n"
                f"注册备份：{result['registration_backup'] or '无'}",
            ),
        )

    def fully_delete_managed_project(self):
        selected = list(self.project_manager_tree.selection())
        if len(selected) != 1:
            messagebox.showwarning(APP_NAME, "彻底删除只能一次选择一个项目。")
            return
        item = self.manager_projects[selected[0]]
        registration_ids = item.get("registration_ids", [])
        if not item.get("exists") or len(registration_ids) != 1:
            messagebox.showwarning(
                APP_NAME,
                "当前项目没有唯一且可验证的侧栏注册，不能安全执行彻底删除。"
                "请先在路径修复中处理重复注册或目录状态。",
            )
            return
        project_id = registration_ids[0]
        if not messagebox.askyesno(
            APP_NAME,
            f"将彻底删除项目：{item.get('project_name') or Path(item['path']).name}\n"
            f"项目目录：{item['path']}\n\n"
            "执行内容：删除侧栏注册；完整备份并删除关联对话；"
            "把项目目录移入可恢复区。Codex 必须完全关闭。是否继续？",
        ):
            return
        self._begin_manager_action()
        self.run(
            "正在完整备份并彻底删除项目...",
            lambda: content_manager.fully_delete_registered_project(
                Path(self.manager_codex_home.get()), project_id
            ),
            lambda result: self.complete_manager_action(
                result,
                f"已彻底删除项目注册和 {result['deleted_conversations']} 个关联对话。\n"
                f"项目文件已移入可恢复区：{result['trash_root']}\n"
                f"注册备份：{result['registration_backup']}\n"
                f"对话备份：{result['conversation_backup'] or '无关联对话'}",
            ),
        )

    def restore_latest_project(self):
        items = content_manager.list_project_trash()
        if not items:
            messagebox.showinfo(APP_NAME, "项目回收区中没有可恢复项目。")
            return
        latest = items[0]
        if not messagebox.askyesno(
            APP_NAME,
            f"恢复最近移除的项目？\n\n原位置：{latest['original_path']}\n移除时间：{latest['created_at']}\n\nCodex 必须完全关闭。",
        ):
            return
        self._begin_manager_action()
        self.run(
            "正在恢复项目...",
            lambda: content_manager.restore_project(
                Path(latest["item_root"]),
                codex_home=Path(self.manager_codex_home.get()),
            ),
            lambda result: self.complete_manager_action(
                result,
                f"项目已恢复：\n{result['restored_path']}\n"
                + ("侧栏注册也已恢复。" if result.get("registration_restored") else ""),
            ),
        )

    def preview_managed_image(self):
        selected = self.image_manager_tree.selection()
        if len(selected) != 1:
            messagebox.showwarning(APP_NAME, "请选择一张图片进行预览。")
            return
        item = self.manager_images[selected[0]]
        preview_root = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "CrossDeviceAgentSync" / "image-preview"
        try:
            path = content_manager.extract_image(Path(item["rollout_path"]), item["digest"], preview_root)
            image = Image.open(path)
            image.thumbnail((900, 650))
            window = tk.Toplevel(self)
            window.title(f"图片预览 - {item['title']}")
            photo = ImageTk.PhotoImage(image)
            label = ttk.Label(window, image=photo)
            label.image = photo
            label.pack(padx=10, pady=10)
            ttk.Label(
                window,
                text=f"单张 {self.format_size(item['size_bytes'])}；在此对话中出现 {item['occurrences']} 次；"
                f"{item['risk_level']}风险：{item['risk_reason']}",
            ).pack(pady=(0, 10))
        except Exception as error:
            self.fail(error, trace=traceback.format_exc())

    def clean_managed_images(self, keep_one=False):
        selected = self.image_manager_tree.selection()
        if not selected:
            messagebox.showwarning(APP_NAME, "请先预览并选择要清理的图片。")
            return
        selections = {}
        estimated = 0
        for item_id in selected:
            item = self.manager_images[item_id]
            selections.setdefault(item["task_id"], set()).add(item["digest"])
            estimated += item["stored_bytes"]
        selected_items = [self.manager_images[item_id] for item_id in selected]
        high_risk = [item for item in selected_items if item["risk_level"] == "高"]
        action_label = "保留每张图片的一份副本并清理重复副本" if keep_one else "从会话中彻底清理所选图片"
        if not messagebox.askyesno(
            APP_NAME,
            f"{action_label}。\n\n"
            f"选中 {len(selected)} 张不同图片，估算最多释放 {self.format_size(estimated)}。\n"
            f"其中高风险图片 {len(high_risk)} 张。\n\n"
            "图片可能影响后续继续对话；操作前会完整备份相关会话，Codex 必须完全关闭。是否继续？",
        ):
            return
        self._begin_manager_action()
        self.run(
            "正在备份对话并清理图片...",
            lambda: content_manager.clean_images(
                Path(self.manager_codex_home.get()), selections, keep_one=keep_one
            ),
            lambda result: self.complete_manager_action(
                result,
                f"已清理 {result['removed_images']} 处图片数据，释放约 {self.format_size(result['removed_bytes'])}。\n"
                f"备份位置：{result['backup_path']}",
            ),
        )

    def complete_manager_action(self, _result, message):
        messagebox.showinfo(APP_NAME, message)
        self._finish_manager_action()

    def show_project_transfer(self):
        self.clear()
        self.nav()
        self.title_block("两台电脑之间传输", "旧电脑导出项目文件和关联对话，新电脑检查冲突后分别选择导入。")
        choice = ttk.Frame(self.content)
        choice.pack(fill="x", pady=(0, 18))
        ttk.Button(choice, text="我在旧电脑：导出", command=self.show_project_export).pack(side="left", ipady=8)
        ttk.Button(choice, text="我在新电脑：检查并导入", command=self.show_project_import).pack(side="left", padx=12, ipady=8)
        ttk.Button(choice, text="兼容旧版仅对话迁移包", command=self.show_transfer).pack(side="left", ipady=8)

    def show_project_export(self):
        self.clear()
        self.nav()
        self.title_block("从旧电脑导出项目", "默认保留 Git 历史，但不打包依赖、构建产物、密钥或环境变量文件。")
        self.project_source = tk.StringVar(value=str(Path.home() / "Documents"))
        self.project_export_codex_home = tk.StringVar(value=str(Path.home() / ".codex"))
        self.project_output = tk.StringVar(value=str(Path.home() / "Desktop" / "project-transfer.cdas.zip"))
        self.project_export_files = tk.BooleanVar(value=True)
        self.project_export_conversations = tk.BooleanVar(value=True)
        self.project_include_git = tk.BooleanVar(value=True)
        self.project_include_sensitive = tk.BooleanVar(value=False)
        self.path_row(self.content, "旧电脑项目目录", self.project_source)
        self.path_row(self.content, "旧电脑 Codex 数据位置", self.project_export_codex_home)
        self.path_row(self.content, "保存迁移包", self.project_output, save=True)
        options = ttk.LabelFrame(self.content, text="导出内容", padding=8)
        options.pack(fill="x", pady=(6, 8))
        ttk.Checkbutton(options, text="项目文件", variable=self.project_export_files).pack(anchor="w")
        ttk.Checkbutton(
            options, text="该项目关联的 Codex 对话", variable=self.project_export_conversations
        ).pack(anchor="w", pady=(5, 0))
        ttk.Checkbutton(options, text="保留 Git 历史（推荐）", variable=self.project_include_git).pack(anchor="w")
        ttk.Checkbutton(
            options,
            text="包含环境变量、密钥和登录数据（默认不包含）",
            variable=self.project_include_sensitive,
        ).pack(anchor="w", pady=(5, 0))
        ttk.Label(
            self.content,
            text="默认跳过 node_modules、虚拟环境、缓存、构建产物和本地数据库；新电脑按项目说明重新安装依赖。",
            foreground="#555555",
            wraplength=760,
        ).pack(anchor="w", pady=(4, 12))
        ttk.Button(self.content, text="开始导出项目", command=self.export_project).pack(anchor="w", pady=8)

    def export_project(self):
        def operation():
            return computer_transfer.create_computer_bundle(
                Path(self.project_export_codex_home.get()),
                Path(self.project_source.get()),
                Path(self.project_output.get()),
                include_project_files=self.project_export_files.get(),
                include_conversations=self.project_export_conversations.get(),
                include_git=self.project_include_git.get(),
                include_sensitive=self.project_include_sensitive.get(),
            )

        self.run(
            "正在生成项目迁移包...",
            operation,
            lambda result: messagebox.showinfo(
                APP_NAME,
                f"电脑迁移包已生成。\n项目：{result['project_name']}\n"
                f"项目文件：{'已包含' if result['has_project_files'] else '未包含'}\n"
                f"关联对话：{result['conversation_count']} 个\n\n{result['bundle_path']}",
            ),
        )

    def show_project_import(self):
        self.clear()
        self.nav()
        self.title_block("保留本机项目并导入", "先检查项目目录和 Codex 注册冲突，再选择直接映射、改名导入或跳过。")
        self.project_bundle = tk.StringVar()
        self.project_root = tk.StringVar(value=str(Path.home() / "Documents" / "Imported Projects"))
        self.project_codex_home = tk.StringVar(value=str(Path.home() / ".codex"))
        self.project_preview = None
        self.project_resolution = None
        self.project_import_files = tk.BooleanVar(value=True)
        self.project_selected_tasks = set()
        self.project_checking = False
        self.project_importing = False
        self.file_row(self.content, "项目迁移包", self.project_bundle)
        self.path_row(self.content, "新项目存放位置", self.project_root)
        self.path_row(self.content, "Codex 数据位置", self.project_codex_home)
        controls = ttk.Frame(self.content)
        controls.pack(fill="x", pady=(10, 10))
        self.project_check_button = ttk.Button(controls, text="检查项目迁移包", command=self.check_project_import)
        self.project_check_button.pack(side="left")
        self.project_execute_button = ttk.Button(
            controls, text="开始导入项目", command=self.import_project, state="disabled"
        )
        self.project_execute_button.pack(side="left", padx=8)
        self.project_import_files_check = ttk.Checkbutton(
            controls, text="导入项目文件", variable=self.project_import_files
        )
        self.project_import_files_check.pack(side="left", padx=(12, 0))
        self.project_import_notebook = ttk.Notebook(self.content)
        self.project_import_notebook.pack(fill="both", expand=True, pady=(4, 0))
        detail_frame = ttk.Frame(self.project_import_notebook, padding=8)
        self.project_import_notebook.add(detail_frame, text="项目导入预览")
        self.project_preview_detail = tk.Text(
            detail_frame, wrap="word", height=15, font=("Microsoft YaHei UI", 10), padx=8, pady=8
        )
        detail_scroll = ttk.Scrollbar(detail_frame, orient="vertical", command=self.project_preview_detail.yview)
        self.project_preview_detail.configure(yscrollcommand=detail_scroll.set)
        self.project_preview_detail.pack(side="left", fill="both", expand=True)
        detail_scroll.pack(side="right", fill="y")
        conversation_frame = ttk.Frame(self.project_import_notebook, padding=8)
        self.project_import_notebook.add(conversation_frame, text="对话选择")
        selection_controls = ttk.Frame(conversation_frame)
        selection_controls.pack(fill="x", pady=(0, 6))
        ttk.Button(selection_controls, text="全选", command=lambda: self.select_project_import_tasks("all")).pack(side="left")
        ttk.Button(selection_controls, text="全不选", command=lambda: self.select_project_import_tasks("none")).pack(side="left", padx=6)
        ttk.Button(selection_controls, text="反选", command=lambda: self.select_project_import_tasks("invert")).pack(side="left")
        tree_frame = ttk.Frame(conversation_frame)
        tree_frame.pack(fill="both", expand=True)
        self.project_conversation_tree = ttk.Treeview(
            tree_frame, columns=("selected", "title", "action"), show="headings", height=6
        )
        self.project_conversation_tree.heading("selected", text="选择")
        self.project_conversation_tree.heading("title", text="对话")
        self.project_conversation_tree.heading("action", text="导入判断")
        self.project_conversation_tree.column("selected", width=60, anchor="center", stretch=False)
        self.project_conversation_tree.column("title", width=420)
        self.project_conversation_tree.column("action", width=150)
        conversation_scroll = ttk.Scrollbar(
            tree_frame, orient="vertical", command=self.project_conversation_tree.yview
        )
        self.project_conversation_tree.configure(yscrollcommand=conversation_scroll.set)
        self.project_conversation_tree.pack(side="left", fill="both", expand=True)
        conversation_scroll.pack(side="right", fill="y")
        self.project_conversation_tree.bind("<Button-1>", self.toggle_project_import_task)
        self.set_project_preview_detail("尚未检查。检查不会创建项目目录、备份或写入任何本机文件。")
        self.project_bundle.trace_add("write", self.invalidate_project_preview)
        self.project_root.trace_add("write", self.invalidate_project_preview)
        self.project_codex_home.trace_add("write", self.invalidate_project_preview)

    def select_project_import_tasks(self, mode):
        task_ids = set(self.project_conversation_tree.get_children())
        if mode == "all":
            self.project_selected_tasks = task_ids
        elif mode == "none":
            self.project_selected_tasks = set()
        else:
            self.project_selected_tasks = task_ids - self.project_selected_tasks
        for task_id in task_ids:
            values = list(self.project_conversation_tree.item(task_id, "values"))
            values[0] = "☑" if task_id in self.project_selected_tasks else "☐"
            self.project_conversation_tree.item(task_id, values=values)

    def toggle_project_import_task(self, event):
        if self.project_conversation_tree.identify_region(event.x, event.y) != "cell":
            return
        if self.project_conversation_tree.identify_column(event.x) != "#1":
            return
        task_id = self.project_conversation_tree.identify_row(event.y)
        if not task_id:
            return
        if task_id in self.project_selected_tasks:
            self.project_selected_tasks.remove(task_id)
        else:
            self.project_selected_tasks.add(task_id)
        values = list(self.project_conversation_tree.item(task_id, "values"))
        values[0] = "☑" if task_id in self.project_selected_tasks else "☐"
        self.project_conversation_tree.item(task_id, values=values)
        return "break"

    def set_project_preview_detail(self, text):
        self.project_preview_detail.configure(state="normal")
        self.project_preview_detail.delete("1.0", "end")
        self.project_preview_detail.insert("1.0", text)
        self.project_preview_detail.configure(state="disabled")

    def invalidate_project_preview(self, *_args):
        self.project_preview = None
        self.project_resolution = None
        self.project_selected_tasks = set()
        if hasattr(self, "project_conversation_tree"):
            self.project_conversation_tree.delete(*self.project_conversation_tree.get_children())
        if hasattr(self, "project_execute_button"):
            self.project_execute_button.configure(state="disabled")
        if hasattr(self, "project_preview_detail"):
            self.set_project_preview_detail("迁移包或项目存放位置已改变，请重新检查。尚未写入任何本机文件。")

    def check_project_import(self):
        self.project_preview = None
        self.project_execute_button.configure(state="disabled")
        self.project_checking = True
        self.project_check_button.configure(state="disabled")

        def operation():
            bundle = Path(self.project_bundle.get()).expanduser().resolve()
            if not bundle.is_file():
                raise ValueError(f"项目迁移包不存在：{bundle}")
            with zipfile.ZipFile(bundle, "r") as archive:
                outer_manifest = json.loads(archive.read("manifest.json"))
            if outer_manifest.get("kind") == computer_transfer.KIND:
                prepared = computer_transfer.prepare_computer_import(
                    bundle,
                    Path(self.project_codex_home.get()),
                    Path(self.project_root.get()),
                )
                project_preview = prepared.get("project_preview")
                target_root = Path(prepared["target_root"])
                renamed_target = (
                    project_preview["renamed_target"]
                    if project_preview else project_import.next_project_destination(
                        Path(prepared["projects_root"]), prepared["manifest"]["project_name"]
                    )
                )
                direct_conflict = (
                    project_preview["direct_conflict"] if project_preview else prepared["registration"]
                )
                return {
                    "kind": "combined",
                    "bundle": str(bundle),
                    "projects_root": prepared["projects_root"],
                    "codex_home": prepared["codex_home"],
                    "direct_target": str(
                        project_preview["direct_target"] if project_preview else target_root
                    ),
                    "renamed_target": str(renamed_target),
                    "target_root": str(target_root),
                    "manifest": prepared["manifest"],
                    "file_count": project_preview["file_count"] if project_preview else 0,
                    "bytes": project_preview["bytes"] if project_preview else 0,
                    "direct_directory_exists": (
                        project_preview["direct_directory_exists"]
                        if project_preview else target_root.exists()
                    ),
                    "direct_conflict": direct_conflict,
                    "registration": prepared["registration"],
                    "recommended_action": prepared["recommended_action"],
                    "conversation_operations": prepared["conversation_operations"],
                }
            prepared = project_import.prepare_registered_project_import(
                bundle,
                Path(self.project_root.get()),
                Path(self.project_codex_home.get()),
            )
            return {
                "bundle": str(bundle),
                "projects_root": str(prepared["projects_root"]),
                "codex_home": str(Path(self.project_codex_home.get()).expanduser().resolve()),
                "direct_target": str(prepared["direct_target"]),
                "renamed_target": str(prepared["renamed_target"]),
                "target_root": str(prepared["target_root"]),
                "manifest": prepared["manifest"],
                "file_count": prepared["file_count"],
                "bytes": prepared["bytes"],
                "direct_directory_exists": prepared["direct_directory_exists"],
                "direct_conflict": prepared["direct_conflict"],
                "registration": prepared["registration"],
                "recommended_action": prepared["recommended_action"],
                "kind": "legacy-project",
                "conversation_operations": [],
            }

        self.run("正在检查项目迁移包，尚未写入本机文件...", operation, self.show_project_import_preview)

    def show_project_import_preview(self, preview):
        self.project_checking = False
        self.project_check_button.configure(state="normal")
        conflict = preview["direct_conflict"]
        is_combined = preview.get("kind") == "combined"
        project_name = (
            preview["manifest"]["project_name"]
            if is_combined else preview["manifest"]["metadata"]["project_name"]
        )
        has_project_files = (
            bool(preview["manifest"].get("has_project_files")) if is_combined else True
        )
        self.project_import_files.set(has_project_files)
        self.project_import_files_check.configure(state="normal" if has_project_files else "disabled")
        has_conflict = preview["direct_directory_exists"] or conflict["conflict"] != "none"
        resolution = preview["recommended_action"]
        if has_conflict:
            details = [
                f"旧电脑项目：{project_name}",
                f"直接映射目录：{preview['direct_target']}",
                f"目录是否已存在：{'是' if preview['direct_directory_exists'] else '否'}",
                f"同路径项目记录：{len(conflict['same_path_projects'])} 个",
                f"同名不同路径记录：{len(conflict['same_name_projects'])} 个",
            ]
            choices = [(
                "import_renamed",
                f"改名并导入新目录（推荐）：{Path(preview['renamed_target']).name}",
            )]
            if is_combined and conflict.get("same_path_projects"):
                choices.append((
                    "reuse_existing",
                    "使用现有项目，只导入所选对话（不合并项目文件）",
                ))
                if len(conflict["same_path_projects"]) > 1:
                    choices.append((
                        "merge_registration",
                        "合并同目录重复注册，只导入所选对话",
                    ))
            choices.append(("skip", "暂不导入这个项目"))
            resolution = self.choose_action_dialog(
                "项目导入冲突",
                "\n".join(details),
                choices,
                "import_renamed",
            )
            if resolution is None:
                self.project_preview = None
                self.project_resolution = None
                self.set_project_preview_detail("已取消冲突处理，尚未写入任何本机文件。")
                self.status.set("项目导入已取消")
                return
        self.project_resolution = resolution
        if resolution == "skip":
            self.project_preview = None
            self.set_project_preview_detail("已选择暂不导入这个项目，尚未写入任何本机文件。")
            self.status.set("已跳过项目导入")
            return
        if resolution == "import_renamed":
            preview["target_root"] = preview["renamed_target"]
            preview["registration"] = project_registry.inspect_project_conflicts(
                Path(preview["codex_home"]),
                Path(preview["target_root"]),
                Path(preview["target_root"]).name,
            )
        elif resolution in {"reuse_existing", "merge_registration"}:
            self.project_import_files.set(False)
            preview["target_root"] = preview["direct_target"]
            preview["registration"] = conflict
        self.project_preview = preview
        metadata = preview["manifest"].get("metadata", {})
        metadata.setdefault("project_name", project_name)
        git_label = "保留" if metadata.get("include_git") else "不包含"
        sensitive_label = "包含" if metadata.get("include_sensitive") else "不包含"
        lines = [
            "检查完成，尚未写入任何本机文件。",
            "",
            f"项目名称：{metadata['project_name']}",
            f"新项目目录：{preview['target_root']}",
            f"目标环境：{preview['registration']['environment']}",
            f"处理方式：{self.project_resolution_label(resolution)}",
            f"待导入文件：{preview['file_count']} 个，{self.format_size(preview['bytes'])}",
            f"Git 历史：{git_label}",
            f"环境变量、密钥和登录数据：{sensitive_label}",
            f"导出时跳过：{metadata.get('skipped_count', 0)} 项依赖、缓存、构建产物或敏感文件",
            "",
            "开始导入会创建上述目录，并在 Codex 关闭时直接注册普通路径侧栏项目。",
            "不会调用 codex app；若目录或 Codex 项目状态在检查后变化，导入会停止。",
        ]
        self.project_conversation_tree.delete(*self.project_conversation_tree.get_children())
        self.project_selected_tasks = set()
        for item in preview.get("conversation_operations", []):
            task_id = item["source_task_id"]
            self.project_selected_tasks.add(task_id)
            self.project_conversation_tree.insert(
                "", "end", iid=task_id,
                values=("☑", item["title"], item["action"]),
            )
        lines.insert(6, f"选中的关联对话：{len(self.project_selected_tasks)} 个")
        self.set_project_preview_detail("\n".join(lines))
        self.project_execute_button.configure(state="normal")
        self.status.set("项目检查完成，请确认预览后开始导入")

    @staticmethod
    def project_resolution_label(resolution):
        return {
            "import_renamed": "改名后作为独立项目导入",
            "create_project": "直接映射并创建项目",
            "reuse_existing": "复用现有项目，只导入对话",
            "merge_registration": "合并重复注册，只导入对话",
        }.get(resolution, resolution)

    def import_project(self):
        if not self.project_preview:
            messagebox.showwarning(APP_NAME, "请先检查项目迁移包。")
            return
        if not messagebox.askyesno(
            APP_NAME,
            f"将按“{self.project_resolution_label(self.project_resolution)}”处理项目，"
            f"导入项目文件：{'是' if self.project_import_files.get() else '否'}，"
            f"导入对话：{len(self.project_selected_tasks)} 个。\n\n"
            "导入前会再次确认目标目录和 Codex 项目状态，并创建可恢复备份；Codex 必须完全关闭。继续？",
        ):
            return
        self.project_importing = True
        self.project_check_button.configure(state="disabled")
        self.project_execute_button.configure(state="disabled")
        preview = self.project_preview

        def operation():
            registration = preview["registration"]
            if preview.get("kind") == "combined":
                same_path = registration.get("same_path_projects", [])
                ordinary = [item for item in same_path if not item.get("has_extended_path")]
                keeper_id = (ordinary[0] if ordinary else same_path[0])["project_id"] if same_path else None
                action = (
                    self.project_resolution
                    if self.project_resolution in {"reuse_existing", "merge_registration"}
                    else "create_project"
                )
                return computer_transfer.restore_computer_bundle(
                    Path(preview["bundle"]),
                    Path(preview["codex_home"]),
                    Path(preview["projects_root"]),
                    Path(preview["target_root"]),
                    import_project_files=self.project_import_files.get(),
                    selected_task_ids=set(self.project_selected_tasks),
                    expected_state_sha256=registration.get("global_state_sha256"),
                    registration_action=action,
                    keeper_id=keeper_id,
                )
            return project_import.restore_registered_project_bundle(
                Path(preview["bundle"]),
                Path(preview["projects_root"]),
                Path(preview["codex_home"]),
                Path(preview["target_root"]),
                Path(preview["target_root"]).name,
                "create_project",
                registration.get("global_state_sha256"),
            )

        self.run("正在导入为独立项目...", operation, self.complete_project_import)

    def complete_project_import(self, result):
        self.project_importing = False
        self.project_check_button.configure(state="normal")
        self.invalidate_project_preview()
        messagebox.showinfo(
            APP_NAME,
            f"项目、对话和侧栏注册处理完成。\n\n"
            f"项目位置：{result['project_path']}\n"
            f"项目 ID：{result['project_id']}\n"
            f"导入项目文件：{'是' if result.get('project_files_imported', True) else '否'}\n"
            f"导入对话：{result.get('conversations_imported', 0)} 个\n"
            f"项目文件备份：{result.get('project_backup_path') or result.get('backup_path') or '无'}\n"
            f"对话备份：{result.get('conversation_backup_path') or '无'}\n"
            f"注册状态备份：{result['registration_backup_path']}\n\n"
            "重新启动 Codex 后检查侧栏；无需再执行 codex app。",
        )

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
        self.export_button = ttk.Button(self.content, text="开始导出", command=self.export_transfer)
        self.export_button.pack(anchor="w", pady=18)

    def export_transfer(self):
        if getattr(self, "exporting", False):
            return
        self.exporting = True
        self.export_button.configure(state="disabled")
        flow = self.start_data_progress(
            "导出迁移包进度",
            (("scan", "扫描并分析对话"), ("metadata", "读取迁移元数据"), ("package", "流式生成迁移包"), ("finalize", "完成并校验迁移包")),
        )

        def operation(progress):
            source = Path(self.export_source.get())
            output = Path(self.export_output.get())
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                if self.export_type.get() == "codex":
                    left = planner.inventory(source, "old-computer", progress_callback=progress)
                    right = {"schema_version": 1, "kind": "cross-device-agent-sync-inventory", "device_id": "new-computer", "codex_home": "", "generated_at": "", "conversations": []}
                    right["inventory_hash"] = planner.sha256_bytes(planner.canonical_json({"device_id": right["device_id"], "conversations": []}))
                    plan = planner.compare_inventories(left, right, "left-to-right", set(), set())
                    left_path, plan_path = root / "left.json", root / "plan.json"
                    planner.write_json(left_path, left)
                    planner.write_json(plan_path, plan)
                    return migration_bundle.create_bundle(left_path, plan_path, "left", output, progress_callback=progress)
                left = generic_sync.snapshot(source, "old-computer")
                empty = root / "empty"
                empty.mkdir()
                right = generic_sync.snapshot(empty, "new-computer")
                plan = generic_sync.compare(left, right, "left-to-right")
                left_path, plan_path = root / "left.json", root / "plan.json"
                planner.write_json(left_path, left)
                planner.write_json(plan_path, plan)
                return generic_sync.create_bundle(left_path, plan_path, "left", output, progress_callback=progress)
        self.run(
            "正在生成迁移包...",
            operation,
            lambda result: messagebox.showinfo(APP_NAME, f"迁移包已生成：\n{result['bundle_path']}"),
            progress_flow=flow,
            progress_callback=lambda stage, detail: self.post_ui_event(
                lambda: self.update_data_progress(flow, stage, detail)
            ),
        )

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
        if getattr(self, "import_checking", False) or getattr(self, "importing", False):
            return
        self.import_preview = None
        self.import_execute_button.configure(state="disabled")
        self.import_checking = True
        self.import_check_button.configure(state="disabled")
        flow = self.start_data_progress(
            "检查迁移包进度",
            (("validate", "校验迁移包"), ("scan", "扫描新电脑现有对话"), ("compare", "比较冲突和重复"), ("finalize", "生成导入预览")),
        )

        def operation(progress):
            bundle = Path(self.import_bundle.get()).expanduser().resolve()
            target = Path(self.import_target.get()).expanduser().resolve()
            if not bundle.is_file():
                raise ValueError(f"迁移包不存在：{bundle}")
            with zipfile.ZipFile(bundle, "r") as archive:
                manifest = json.loads(archive.read("manifest.json"))
            if manifest.get("kind") == migration_bundle.BUNDLE_KIND:
                prepared = migration_bundle.prepare_restore(bundle, target, progress_callback=progress)
                return {
                    "kind": "codex",
                    "bundle": str(bundle),
                    "target": str(prepared["codex_home"]),
                    "operations": prepared["operations"],
                }
            if manifest.get("kind") == f"{generic_sync.KIND}-bundle":
                prepared = generic_sync.prepare_restore(bundle, target, progress_callback=progress)
                return {
                    "kind": "generic",
                    "bundle": str(bundle),
                    "target": str(prepared["target_root"]),
                    "operations": prepared["operations"],
                }
            raise ValueError("不支持的迁移包类型")

        self.run(
            "正在检查迁移包，尚未写入数据...",
            operation,
            self.show_import_preview,
            progress_flow=flow,
            progress_callback=lambda stage, detail: self.post_ui_event(
                lambda: self.update_data_progress(flow, stage, detail)
            ),
        )

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
        flow = self.start_data_progress(
            "导入迁移包进度",
            (("preflight", "检查迁移包和目标数据"), ("validate", "校验迁移包内容"), ("backup", "备份新电脑现有数据"), ("write", "写入选中的对话"), ("verify", "验证导入结果")),
        )

        def operation(progress):
            bundle = Path(self.import_bundle.get())
            with zipfile.ZipFile(bundle, "r") as archive:
                manifest = json.loads(archive.read("manifest.json"))
            if manifest.get("kind") == migration_bundle.BUNDLE_KIND:
                return migration_bundle.restore_bundle(
                    bundle, Path(self.import_target.get()), require_codex_closed=True, progress_callback=progress
                )
            return generic_sync.restore_bundle(
                bundle, Path(self.import_target.get()), progress_callback=progress
            )
        self.run(
            "正在备份并导入...",
            operation,
            self.complete_import_transfer,
            progress_flow=flow,
            progress_callback=lambda stage, detail: self.post_ui_event(
                lambda: self.update_data_progress(flow, stage, detail)
            ),
        )

    def complete_import_transfer(self, result):
        self.importing = False
        self.import_check_button.configure(state="normal")
        self.invalidate_import_preview()
        catalog_note = ""
        if "bundle_id" in result:
            catalog_note = (
                "\n\n启动 Codex 后，官方目录同步会为导入任务建立侧栏记录；"
                "若侧栏没有立即更新，请刷新页面或重启 Codex。"
            )
        messagebox.showinfo(
            APP_NAME,
            f"导入完成。\n备份位置：{result['backup_path']}{catalog_note}",
        )

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

    def run(self, text, operation, callback=None, progress_flow=None, progress_callback=None):
        self.status.set(text)
        self.diagnostics.event("operation_start", operation=text)

        def worker():
            try:
                result = operation(progress_callback) if progress_callback is not None else operation()
                self.post_ui_event(
                    lambda result=result, callback=callback, progress_flow=progress_flow: self.finish(
                        result, callback, progress_flow
                    )
                )
            except Exception as error:
                trace = traceback.format_exc()
                self.post_ui_event(
                    lambda error=error, progress_flow=progress_flow, trace=trace: self.fail(
                        error, progress_flow, trace
                    )
                )

        threading.Thread(target=worker, daemon=True).start()

    def finish(self, result, callback, progress_flow=None):
        self.close_progress_flow(progress_flow)
        if progress_flow and hasattr(self, "provider_execute_button"):
            self.provider_execute_button.configure(state="normal")
        if getattr(self, "exporting", False):
            self.exporting = False
            if hasattr(self, "export_button") and self.export_button.winfo_exists():
                self.export_button.configure(state="normal")
        self.status.set("完成")
        summary = None
        if isinstance(result, dict):
            summary = {
                key: result.get(key)
                for key in (
                    "backup_path", "backup_created", "reassigned", "imported", "skipped",
                    "selected_count", "selected_bytes", "required_bytes", "free_bytes",
                    "repaired", "blocked", "triggers_removed",
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
        if getattr(self, "exporting", False):
            self.exporting = False
            if hasattr(self, "export_button") and self.export_button.winfo_exists():
                self.export_button.configure(state="normal")
        if getattr(self, "import_checking", False):
            self.import_checking = False
            self.import_check_button.configure(state="normal")
        if getattr(self, "importing", False):
            self.importing = False
            self.import_check_button.configure(state="normal")
            if self.import_preview:
                self.import_execute_button.configure(state="normal")
        if getattr(self, "project_checking", False):
            self.project_checking = False
            self.project_check_button.configure(state="normal")
        if getattr(self, "project_importing", False):
            self.project_importing = False
            self.project_check_button.configure(state="normal")
            if self.project_preview:
                self.project_execute_button.configure(state="normal")
        if getattr(self, "content_scanning", False):
            self.content_scanning = False
            if hasattr(self, "manager_scan_button") and self.manager_scan_button.winfo_exists():
                self.manager_scan_button.configure(state="normal")
        if getattr(self, "manager_busy", False):
            self.manager_busy = False
            if hasattr(self, "manager_scan_button") and self.manager_scan_button.winfo_exists():
                self.manager_scan_button.configure(state="normal")
            for button in getattr(self, "manager_action_buttons", []):
                if button.winfo_exists():
                    button.configure(state="normal")
        if hasattr(self, "release_update_button") and self.latest_release and self.latest_release.get("update_available"):
            self.release_update_button.configure(state="normal")
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
