from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from .client import HubClient
from .config import (
    DEFAULT_VPN_HEALTH_URL,
    BridgeBinding,
    default_app_dir,
    default_config_path,
    default_hermes_cli_path,
    find_binding,
    hub_workspace_url,
    load_config,
    save_config,
    update_binding_runtime_settings,
    update_hermes_tool_settings,
    update_hub_client_settings,
    update_local_tool_settings,
    write_sample_config,
)
from .controller import BridgeController
from .agent_service import AgentService
from .i18n import text
from .network_guard import UrlHealthProbe, find_default_cmynetwork_path
from .persistent_log import PersistentLog
from .startup import StartupManager
from .tool_locator import find_default_codex_cli_path, find_default_codex_path
from .tray import BridgeTray


class BridgeApp:
    def __init__(self, config_path: Path | None = None, auto_start: bool = False, *, background: bool = False):
        self.background = bool(background)
        self._background_start_failed = False
        self.config_path = config_path or default_config_path()
        if not self.config_path.exists():
            write_sample_config(self.config_path)
        self.config = load_config(self.config_path)
        self.root = tk.Tk()
        if self.background:
            self.root.withdraw()
        self.root.title(text("window_title"))
        self.root.geometry("1080x760")
        self.root.minsize(900, 650)
        self._exiting = False
        self._refresh_after_id: str | None = None
        self.startup = StartupManager()
        if not self.background:
            self.startup.migrate_legacy(Path(sys.executable), args=["--start"])
        set_window_icon(self.root)
        self.persistent_log = PersistentLog(default_app_dir() / "bridge.log")
        self.log_lines: list[str] = self.persistent_log.tail(200)
        self.log_queue: queue.SimpleQueue[str] = queue.SimpleQueue()
        self.ui_queue: queue.SimpleQueue[tuple[str, str]] = queue.SimpleQueue()
        self.controller = BridgeController(self.config, log_callback=self.append_log)
        self.agent_service = AgentService(log_callback=self.append_log)
        self._init_variables()
        self._configure_style()
        self._build_ui()
        self.tray = BridgeTray(
            icon_path=asset_path("codex-owl-logo.png"),
            title="Codex Bridge",
            show_window=lambda: self.root.after(0, self.show_window),
            start_bridge=lambda: self.root.after(0, self.start),
            stop_bridge=lambda: self.root.after(0, self.stop),
            open_hub=lambda: self.root.after(0, self.open_hub),
            open_toolbox=lambda: self.root.after(0, self.open_toolbox),
            exit_app=lambda: self.root.after(0, self.exit_app),
        )
        tray_started = self.tray.start(wait_until_ready=2) if self.background else self.tray.start()
        if self.background and not tray_started:
            self.append_log('后台启动未完成：系统托盘不可用；未启动执行连接。请从正常应用入口打开连接设置。')
            self._background_start_failed = True
            self.tray.stop()
            self.root.destroy()
            return
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.refresh_ui()
        if tray_started:
            self.append_log("CodexBridge 托盘图标已启动。")
        elif self.tray.error:
            self.append_log(f"CodexBridge 托盘不可用：{self.tray.error}")
        if auto_start:
            self.root.after(300, self.start)
        elif self.agent_service.configured:
            self.root.after(300, self.agent_service.start)
        # The packaged application opens the full workspace; connection controls
        # remain available from its tray instead of occupying the main window.
        if not self.background and getattr(sys, 'frozen', False) and Path(sys.executable).with_name('HubDesktop.exe').is_file():
            self.root.after(500, self._open_desktop_workspace)

    def _init_variables(self) -> None:
        codex_binding = find_binding(self.config, "codex")
        hermes_binding = find_binding(self.config, "hermes")
        self.status_var = tk.StringVar(value=text("stopped"))
        self.agent_status_var = tk.StringVar(value="已停止")
        self.device_var = tk.StringVar(value="-")
        self.hub_var = tk.StringVar(value=self.config.hub)
        self.inbox_url_var = tk.StringVar(value=self.config.inbox_url)
        self.hub_test_var = tk.StringVar(value=text("not_tested"))
        self.startup_var = tk.BooleanVar(value=self.startup.is_enabled())

        self.codex_enabled_var = tk.BooleanVar(
            value=binding_value(codex_binding, "enabled", True)
        )
        self.codex_interval_var = tk.StringVar(
            value=str(binding_value(codex_binding, "interval_seconds", self.config.interval_seconds))
        )
        self.codex_open_var = tk.BooleanVar(
            value=binding_value(codex_binding, "open_codex", self.config.open_codex)
        )
        self.codex_path_var = tk.StringVar(value=path_text(self.config.codex_exe_path))
        self.auto_submit_codex_var = tk.BooleanVar(
            value=bool(self.config.auto_submit_codex_prompt)
        )
        self.codex_submit_delay_var = tk.StringVar(
            value=str(self.config.codex_submit_delay_seconds)
        )
        self.codex_submit_hotkey_var = tk.StringVar(
            value=self.config.codex_submit_hotkey
        )
        self.codex_cli_path_var = tk.StringVar(
            value=path_text(self.config.codex_cli_path)
        )
        self.auto_execute_codex_var = tk.BooleanVar(
            value=binding_value(
                codex_binding, "auto_execute_codex", self.config.auto_execute_codex
            )
        )
        self.codex_exec_sandbox_var = tk.StringVar(
            value=self.config.codex_exec_sandbox
        )
        self.codex_exec_timeout_var = tk.StringVar(
            value=str(self.config.codex_exec_timeout_seconds)
        )

        self.hermes_enabled_var = tk.BooleanVar(
            value=binding_value(hermes_binding, "enabled", bool(hermes_binding))
        )
        self.hermes_interval_var = tk.StringVar(
            value=str(binding_value(hermes_binding, "interval_seconds", self.config.interval_seconds))
        )
        self.hermes_auto_execute_var = tk.BooleanVar(
            value=binding_value(hermes_binding, "auto_execute_codex", True)
        )
        self.hermes_cli_path_var = tk.StringVar(
            value=path_text(self.config.hermes_cli_path)
        )
        self.hermes_model_var = tk.StringVar(
            value=binding_value(hermes_binding, "hermes_model", self.config.hermes_model)
        )
        self.hermes_provider_var = tk.StringVar(
            value=binding_value(
                hermes_binding, "hermes_provider", self.config.hermes_provider
            )
        )
        self.hermes_toolsets_var = tk.StringVar(
            value=binding_value(
                hermes_binding, "hermes_toolsets", self.config.hermes_toolsets
            )
        )
        self.hermes_timeout_var = tk.StringVar(
            value=str(
                binding_value(
                    hermes_binding,
                    "hermes_timeout_seconds",
                    self.config.hermes_timeout_seconds,
                )
            )
        )

        self.hub_timeout_var = tk.StringVar(
            value=str(self.config.hub_request_timeout_seconds)
        )
        self.hub_retry_attempts_var = tk.StringVar(
            value=str(self.config.hub_get_retry_attempts)
        )
        self.hub_retry_backoff_var = tk.StringVar(
            value=str(self.config.hub_retry_backoff_seconds)
        )
        self.vpn_path_var = tk.StringVar(value=path_text(self.config.vpn_exe_path))
        self.chatgpt_url_var = tk.StringVar(
            value=self.config.vpn_health_url or DEFAULT_VPN_HEALTH_URL
        )
        self.vpn_auto_recover_var = tk.BooleanVar(
            value=bool(self.config.vpn_auto_recover)
        )
        self.vpn_switch_command_var = tk.StringVar(
            value=self.config.vpn_switch_command
        )

    def _configure_style(self) -> None:
        style = ttk.Style(self.root)
        style.configure("Title.TLabel", font=("Segoe UI", 17, "bold"))
        style.configure("Status.TLabel", font=("Segoe UI", 12, "bold"))
        style.configure("Muted.TLabel", foreground="#5f6368")
        style.configure("Treeview", rowheight=28, font=("Segoe UI", 10))
        style.configure("Treeview.Heading", font=("Segoe UI", 10, "bold"))
        style.configure("TNotebook.Tab", padding=(16, 8))
        style.configure("Primary.TButton", padding=(14, 7))

    def _build_ui(self) -> None:
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(1, weight=1)
        self._build_header()
        self.main_notebook = ttk.Notebook(self.root)
        self.main_notebook.grid(row=1, column=0, sticky="nsew", padx=16, pady=(0, 16))
        self.ai_tab = ttk.Frame(self.main_notebook, padding=12)
        self.network_tab = ttk.Frame(self.main_notebook, padding=12)
        self.logs_tab = ttk.Frame(self.main_notebook, padding=12)
        self.main_notebook.add(self.ai_tab, text=text("tab_ai"))
        self.main_notebook.add(self.network_tab, text=text("tab_network"))
        self.main_notebook.add(self.logs_tab, text=text("tab_logs"))
        self._build_ai_tab()
        self._build_network_tab()
        self._build_logs_tab()

    def _build_header(self) -> None:
        header = ttk.Frame(self.root, padding=(18, 16, 18, 12))
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(0, weight=1)
        ttk.Label(header, text=text("console_title"), style="Title.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        ttk.Label(header, textvariable=self.status_var, style="Status.TLabel").grid(
            row=1, column=0, sticky="w", pady=(6, 0)
        )
        summary = ttk.Frame(header)
        summary.grid(row=2, column=0, columnspan=2, sticky="w", pady=(6, 0))
        ttk.Label(summary, text=text("online_devices"), style="Muted.TLabel").pack(
            side="left"
        )
        ttk.Label(summary, textvariable=self.device_var).pack(side="left", padx=(6, 18))
        ttk.Label(summary, text=text("hub"), style="Muted.TLabel").pack(side="left")
        ttk.Label(summary, textvariable=self.hub_var).pack(side="left", padx=(6, 0))

        actions = ttk.Frame(header)
        actions.grid(row=0, column=1, rowspan=2, sticky="e")
        self.start_button = ttk.Button(
            actions, text=text("start"), command=self.start, style="Primary.TButton"
        )
        self.start_button.grid(row=0, column=0, padx=(0, 8))
        self.stop_button = ttk.Button(actions, text=text("stop"), command=self.stop)
        self.stop_button.grid(row=0, column=1, padx=(0, 8))
        self.run_once_button = ttk.Button(
            actions, text=text("run_once"), command=self.run_once
        )
        self.run_once_button.grid(row=0, column=2, padx=(0, 8))
        ttk.Button(actions, text=text("open_hub"), command=self.open_hub).grid(
            row=0, column=3, padx=(0, 8)
        )
        ttk.Button(actions, text=text("open_inbox"), command=self.open_inbox).grid(
            row=0, column=4
        )
        self.toolbox_button = ttk.Button(
            actions, text="打开工具箱", command=self.open_toolbox
        )
        self.toolbox_button.grid(row=1, column=0, columnspan=2, sticky="w", pady=(10, 0))
        ttk.Checkbutton(
            actions,
            text=text("start_with_windows"),
            variable=self.startup_var,
            command=self.toggle_startup,
        ).grid(row=1, column=2, columnspan=3, sticky="e", pady=(10, 0))

    def _build_ai_tab(self) -> None:
        self.ai_tab.columnconfigure(0, weight=1)
        self.ai_tab.rowconfigure(1, weight=1)
        overview = ttk.LabelFrame(self.ai_tab, text=text("binding_overview"), padding=10)
        overview.grid(row=0, column=0, sticky="ew")
        overview.columnconfigure(0, weight=1)
        self.binding_tree = ttk.Treeview(
            overview,
            columns=("name", "backend", "device", "interval", "status"),
            show="headings",
            height=4,
            selectmode="browse",
        )
        headings = (
            ("name", text("binding_name"), 170),
            ("backend", text("backend"), 120),
            ("device", text("hub_device"), 230),
            ("interval", text("poll_interval"), 120),
            ("status", text("binding_status"), 300),
        )
        for column, label, width in headings:
            self.binding_tree.heading(column, text=label)
            self.binding_tree.column(column, width=width, minwidth=90, anchor="w")
        self.binding_tree.tag_configure("online", foreground="#137333")
        self.binding_tree.tag_configure("error", foreground="#b3261e")
        self.binding_tree.tag_configure("disabled", foreground="#777777")
        self.binding_tree.grid(row=0, column=0, sticky="ew")
        self.binding_tree.bind("<<TreeviewSelect>>", self.on_binding_selected)

        self.backend_notebook = ttk.Notebook(self.ai_tab)
        self.backend_notebook.grid(row=1, column=0, sticky="nsew", pady=(12, 0))
        self.agent_settings_tab = ttk.Frame(self.backend_notebook, padding=20)
        self.backend_notebook.add(self.agent_settings_tab, text="Hub 智能体")
        ttk.Label(self.agent_settings_tab, text="Hub 智能体执行连接", font=("Microsoft YaHei UI", 16, "bold")).pack(anchor="w")
        ttk.Label(self.agent_settings_tab, textvariable=self.agent_status_var, font=("Microsoft YaHei UI", 12)).pack(anchor="w", pady=(12, 16))
        ttk.Label(self.agent_settings_tab, text="总控和各专业智能体通过这条连接执行工作。\n智能体身份、职责和任务在 Hub 网页中统一管理。\n\n应用启动时自动连接，异常退出后自动恢复。\n停止时先等待已有工作收尾；关闭窗口后仍在托盘运行。", justify="left", wraplength=780).pack(anchor="w")
        ttk.Button(self.agent_settings_tab, text="进入 Hub 工作区", command=self.open_hub).pack(anchor="w", pady=(20, 0))
        self.codex_settings_tab = ttk.Frame(self.backend_notebook, padding=16)
        self.hermes_settings_tab = ttk.Frame(self.backend_notebook, padding=16)
        self.backend_notebook.add(self.codex_settings_tab, text="Codex")
        self.backend_notebook.add(self.hermes_settings_tab, text="Hermes")
        self._build_codex_settings()
        self._build_hermes_settings()

        footer = ttk.Frame(self.ai_tab)
        footer.grid(row=2, column=0, sticky="ew", pady=(12, 0))
        ttk.Button(footer, text=text("open_config"), command=self.open_config).pack(
            side="left"
        )
        ttk.Button(footer, text=text("open_prompts"), command=self.open_prompts).pack(
            side="left", padx=(8, 0)
        )
        self.pending_button = ttk.Button(
            footer,
            text="待确认项",
            command=self.manage_pending_mentions,
        )
        self.pending_button.pack(side="left", padx=(8, 0))
        ttk.Button(
            footer,
            text=text("save_apply"),
            command=self.save_settings,
            style="Primary.TButton",
        ).pack(side="right")

    def manage_pending_mentions(self) -> None:
        reader = getattr(self.controller, "pending_deliveries", None)
        pending = (
            reader()
            if callable(reader)
            else self.controller.pending_mention_deliveries()
        )
        if not pending:
            messagebox.showinfo(text("window_title"), "当前没有待确认项。")
            return

        window = tk.Toplevel(self.root)
        window.title("待确认投递")
        window.geometry("760x360")
        window.minsize(640, 300)
        window.transient(self.root)
        window.grab_set()
        window.columnconfigure(0, weight=1)
        window.rowconfigure(1, weight=1)

        ttk.Label(
            window,
            text="这些投递已被防重复锁保护，不会自动重新打开 Codex。",
            padding=(14, 14, 14, 8),
        ).grid(row=0, column=0, sticky="w")
        tree = ttk.Treeview(
            window,
            columns=("ai", "project", "kind", "entry", "stage", "detail"),
            show="headings",
            selectmode="browse",
        )
        for column, label, width in (
            ("ai", "AI", 100),
            ("project", "项目", 170),
            ("kind", "类型", 70),
            ("entry", "消息", 80),
            ("stage", "状态", 100),
            ("detail", "说明", 260),
        ):
            tree.heading(column, text=label)
            tree.column(column, width=width, minwidth=70, anchor="w")
        by_item: dict[str, dict] = {}
        for index, item in enumerate(pending):
            item_id = f"pending-{index}"
            by_item[item_id] = item
            stage = str(item.get("stage") or "uncertain")
            stage_label = "已发送待回执" if stage == "submitted" else "投递待人工确认"
            item_type = str(item.get("item_type") or "mention")
            item_number = item.get(
                "task_id" if item_type == "task" else "mention_id"
            )
            tree.insert(
                "",
                "end",
                iid=item_id,
                values=(
                    item.get("binding_name") or item.get("binding_id"),
                    item.get("project_slug"),
                    "任务" if item_type == "task" else "提及",
                    f"#{item_number}",
                    stage_label,
                    item.get("detail") or "",
                ),
            )
        tree.grid(row=1, column=0, sticky="nsew", padx=14)
        first = next(iter(by_item), "")
        if first:
            tree.selection_set(first)

        actions = ttk.Frame(window, padding=14)
        actions.grid(row=2, column=0, sticky="ew")

        def selected() -> dict | None:
            selection = tree.selection()
            return by_item.get(selection[0]) if selection else None

        def run_action(action: str) -> None:
            item = selected()
            if not item:
                return
            item_type = str(item.get("item_type") or "mention")
            item_id = int(
                item["task_id"] if item_type == "task" else item["mention_id"]
            )
            item_label = "任务" if item_type == "task" else "提及"
            if action == "retry" and not messagebox.askyesno(
                text("window_title"),
                f"确定重新投递{item_label} #{item_id}？\n"
                "只有在确认它没有发送时才应选择重新投递。",
                parent=window,
            ):
                return
            window.destroy()

            def worker() -> None:
                try:
                    binding_id = str(item["binding_id"])
                    project_slug = str(item["project_slug"])
                    if item_type == "task" and action == "confirm":
                        self.controller.confirm_pending_task(
                            binding_id, project_slug, item_id
                        )
                    elif item_type == "task":
                        self.controller.retry_pending_task(
                            binding_id, project_slug, item_id
                        )
                    elif action == "confirm":
                        self.controller.confirm_pending_mention(
                            binding_id, project_slug, item_id
                        )
                    else:
                        self.controller.retry_pending_mention(
                            binding_id, project_slug, item_id
                        )
                except Exception as exc:
                    self.append_log(f"待确认投递操作失败：{exc}")

            threading.Thread(
                target=worker,
                name="CodexBridge-PendingDelivery",
                daemon=True,
            ).start()

        ttk.Button(
            actions,
            text="确认已经发送",
            command=lambda: run_action("confirm"),
        ).pack(side="left")
        ttk.Button(
            actions,
            text="确认未发送，重新投递",
            command=lambda: run_action("retry"),
        ).pack(side="left", padx=(8, 0))
        ttk.Button(actions, text="关闭", command=window.destroy).pack(side="right")

    def _build_codex_settings(self) -> None:
        panel = self.codex_settings_tab
        panel.columnconfigure(1, weight=1)
        ttk.Checkbutton(
            panel, text=text("binding_enabled"), variable=self.codex_enabled_var
        ).grid(row=0, column=0, sticky="w")
        interval = ttk.Frame(panel)
        interval.grid(row=0, column=1, sticky="e")
        ttk.Label(interval, text=text("poll_interval")).pack(side="left")
        ttk.Entry(interval, textvariable=self.codex_interval_var, width=8).pack(
            side="left", padx=(8, 4)
        )
        ttk.Label(interval, text=text("seconds")).pack(side="left")
        self._path_row(
            panel,
            1,
            text("codex_exe"),
            self.codex_path_var,
            self.detect_codex,
            self.browse_codex,
        )
        self._path_row(
            panel,
            2,
            text("codex_cli"),
            self.codex_cli_path_var,
            self.detect_codex_cli,
            self.browse_codex_cli,
        )
        behavior = ttk.LabelFrame(panel, text=text("execution_behavior"), padding=10)
        behavior.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(14, 0))
        behavior.columnconfigure(3, weight=1)
        ttk.Checkbutton(
            behavior, text=text("open_codex_for_work"), variable=self.codex_open_var
        ).grid(row=0, column=0, sticky="w")
        ttk.Checkbutton(
            behavior,
            text=text("auto_submit_codex"),
            variable=self.auto_submit_codex_var,
        ).grid(row=0, column=1, sticky="w", padx=(18, 0))
        ttk.Label(behavior, text=text("submit_delay")).grid(
            row=0, column=2, sticky="e", padx=(18, 6)
        )
        ttk.Entry(
            behavior, textvariable=self.codex_submit_delay_var, width=6
        ).grid(row=0, column=3, sticky="w")
        ttk.Checkbutton(
            behavior,
            text=text("auto_execute_codex"),
            variable=self.auto_execute_codex_var,
        ).grid(row=1, column=0, sticky="w", pady=(12, 0))
        ttk.Label(behavior, text=text("exec_sandbox")).grid(
            row=1, column=1, sticky="e", padx=(18, 6), pady=(12, 0)
        )
        ttk.Combobox(
            behavior,
            textvariable=self.codex_exec_sandbox_var,
            values=("workspace-write", "read-only", "danger-full-access"),
            width=20,
            state="readonly",
        ).grid(row=1, column=2, sticky="w", pady=(12, 0))
        ttk.Label(behavior, text=text("exec_timeout")).grid(
            row=1, column=3, sticky="e", padx=(18, 6), pady=(12, 0)
        )
        ttk.Entry(
            behavior, textvariable=self.codex_exec_timeout_var, width=8
        ).grid(row=1, column=4, sticky="e", pady=(12, 0))
        ttk.Label(
            panel, text=text("codex_behavior_hint"), style="Muted.TLabel"
        ).grid(row=4, column=0, columnspan=2, sticky="w", pady=(10, 0))

    def _build_hermes_settings(self) -> None:
        panel = self.hermes_settings_tab
        panel.columnconfigure(1, weight=1)
        top = ttk.Frame(panel)
        top.grid(row=0, column=0, columnspan=2, sticky="ew")
        top.columnconfigure(3, weight=1)
        ttk.Checkbutton(
            top, text=text("binding_enabled"), variable=self.hermes_enabled_var
        ).grid(row=0, column=0, sticky="w")
        ttk.Checkbutton(
            top,
            text=text("hermes_auto_execute"),
            variable=self.hermes_auto_execute_var,
        ).grid(row=0, column=1, sticky="w", padx=(20, 0))
        ttk.Label(top, text=text("poll_interval")).grid(
            row=0, column=2, sticky="e", padx=(20, 6)
        )
        ttk.Entry(top, textvariable=self.hermes_interval_var, width=8).grid(
            row=0, column=3, sticky="e"
        )
        self._path_row(
            panel,
            1,
            text("hermes_cli"),
            self.hermes_cli_path_var,
            self.detect_hermes,
            self.browse_hermes,
        )
        model_frame = ttk.LabelFrame(panel, text=text("hermes_runtime"), padding=10)
        model_frame.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(14, 0))
        model_frame.columnconfigure(1, weight=1)
        model_frame.columnconfigure(3, weight=1)
        ttk.Label(model_frame, text=text("hermes_model")).grid(row=0, column=0, sticky="w")
        ttk.Entry(model_frame, textvariable=self.hermes_model_var).grid(
            row=0, column=1, sticky="ew", padx=(8, 18)
        )
        ttk.Label(model_frame, text=text("hermes_provider")).grid(
            row=0, column=2, sticky="w"
        )
        ttk.Entry(model_frame, textvariable=self.hermes_provider_var).grid(
            row=0, column=3, sticky="ew", padx=(8, 0)
        )
        ttk.Label(model_frame, text=text("hermes_toolsets")).grid(
            row=1, column=0, sticky="w", pady=(12, 0)
        )
        ttk.Entry(model_frame, textvariable=self.hermes_toolsets_var).grid(
            row=1, column=1, columnspan=2, sticky="ew", padx=(8, 18), pady=(12, 0)
        )
        timeout = ttk.Frame(model_frame)
        timeout.grid(row=1, column=3, sticky="e", pady=(12, 0))
        ttk.Label(timeout, text=text("exec_timeout")).pack(side="left")
        ttk.Entry(timeout, textvariable=self.hermes_timeout_var, width=8).pack(
            side="left", padx=(8, 0)
        )
        ttk.Label(
            panel, text=text("hermes_behavior_hint"), style="Muted.TLabel"
        ).grid(row=3, column=0, columnspan=2, sticky="w", pady=(10, 0))

    def _build_network_tab(self) -> None:
        self.network_tab.columnconfigure(0, weight=1)
        hub_frame = ttk.LabelFrame(
            self.network_tab, text=text("hub_connection"), padding=14
        )
        hub_frame.grid(row=0, column=0, sticky="ew")
        hub_frame.columnconfigure(1, weight=1)
        ttk.Label(hub_frame, text=text("hub")).grid(row=0, column=0, sticky="w")
        ttk.Entry(hub_frame, textvariable=self.hub_var).grid(
            row=0, column=1, columnspan=5, sticky="ew", padx=(10, 0)
        )
        ttk.Label(hub_frame, text=text("inbox_url")).grid(
            row=1, column=0, sticky="w", pady=(12, 0)
        )
        ttk.Entry(hub_frame, textvariable=self.inbox_url_var).grid(
            row=1, column=1, columnspan=5, sticky="ew", padx=(10, 0), pady=(12, 0)
        )
        ttk.Label(hub_frame, text=text("request_timeout")).grid(
            row=2, column=0, sticky="w", pady=(12, 0)
        )
        ttk.Entry(hub_frame, textvariable=self.hub_timeout_var, width=8).grid(
            row=2, column=1, sticky="w", padx=(10, 18), pady=(12, 0)
        )
        ttk.Label(hub_frame, text=text("get_attempts")).grid(
            row=2, column=2, sticky="w", pady=(12, 0)
        )
        ttk.Entry(
            hub_frame, textvariable=self.hub_retry_attempts_var, width=8
        ).grid(row=2, column=3, sticky="w", padx=(10, 18), pady=(12, 0))
        ttk.Label(hub_frame, text=text("retry_backoff")).grid(
            row=2, column=4, sticky="w", pady=(12, 0)
        )
        ttk.Entry(hub_frame, textvariable=self.hub_retry_backoff_var, width=8).grid(
            row=2, column=5, sticky="w", padx=(10, 0), pady=(12, 0)
        )
        test_row = ttk.Frame(hub_frame)
        test_row.grid(row=3, column=0, columnspan=6, sticky="ew", pady=(12, 0))
        ttk.Button(test_row, text=text("test_hub"), command=self.test_hub).pack(
            side="left"
        )
        ttk.Label(test_row, textvariable=self.hub_test_var).pack(
            side="left", padx=(10, 0)
        )
        ttk.Label(
            test_row, text=text("get_retry_hint"), style="Muted.TLabel"
        ).pack(side="right")

        system = ttk.LabelFrame(
            self.network_tab, text=text("network_tools"), padding=14
        )
        system.grid(row=1, column=0, sticky="ew", pady=(14, 0))
        system.columnconfigure(1, weight=1)
        self._path_row(
            system,
            0,
            text("cmynetwork_exe"),
            self.vpn_path_var,
            self.detect_cmynetwork,
            self.browse_cmynetwork,
        )
        ttk.Label(system, text=text("chatgpt_test")).grid(
            row=1, column=0, sticky="w", pady=(12, 0)
        )
        ttk.Entry(system, textvariable=self.chatgpt_url_var).grid(
            row=1, column=1, sticky="ew", padx=(10, 8), pady=(12, 0)
        )
        ttk.Button(system, text=text("test"), command=self.test_chatgpt).grid(
            row=1, column=2, padx=(0, 8), pady=(12, 0)
        )
        ttk.Button(system, text=text("open"), command=self.open_chatgpt).grid(
            row=1, column=3, pady=(12, 0)
        )
        ttk.Label(system, text=text("switch_command")).grid(
            row=2, column=0, sticky="w", pady=(12, 0)
        )
        ttk.Entry(system, textvariable=self.vpn_switch_command_var).grid(
            row=2, column=1, sticky="ew", padx=(10, 8), pady=(12, 0)
        )
        ttk.Checkbutton(
            system,
            text=text("auto_recover"),
            variable=self.vpn_auto_recover_var,
        ).grid(row=2, column=2, columnspan=2, sticky="w", pady=(12, 0))
        ttk.Label(system, text=text("auto_recover_hint"), style="Muted.TLabel").grid(
            row=3, column=1, columnspan=3, sticky="w", padx=(10, 0), pady=(10, 0)
        )
        footer = ttk.Frame(self.network_tab)
        footer.grid(row=2, column=0, sticky="ew", pady=(14, 0))
        ttk.Button(footer, text=text("open_config"), command=self.open_config).pack(
            side="left"
        )
        ttk.Button(
            footer,
            text=text("save_apply"),
            command=self.save_settings,
            style="Primary.TButton",
        ).pack(side="right")

    def _build_logs_tab(self) -> None:
        self.logs_tab.columnconfigure(0, weight=1)
        self.logs_tab.rowconfigure(1, weight=1)
        toolbar = ttk.Frame(self.logs_tab)
        toolbar.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        ttk.Label(toolbar, text=text("log_hint"), style="Muted.TLabel").pack(
            side="left"
        )
        ttk.Button(toolbar, text=text("clear_logs"), command=self.clear_logs).pack(
            side="right"
        )
        log_frame = ttk.Frame(self.logs_tab)
        log_frame.grid(row=1, column=0, sticky="nsew")
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)
        self.log_text = tk.Text(
            log_frame,
            wrap="word",
            state="disabled",
            font=("Consolas", 9),
            padx=10,
            pady=10,
            background="#fbfbfb",
        )
        self.log_text.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(log_frame, command=self.log_text.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=scrollbar.set)

    def _path_row(
        self,
        parent: ttk.Frame | ttk.LabelFrame,
        row: int,
        label: str,
        variable: tk.StringVar,
        detect_command,
        browse_command,
    ) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=(14, 0))
        ttk.Entry(parent, textvariable=variable).grid(
            row=row, column=1, sticky="ew", padx=(12, 8), pady=(14, 0)
        )
        ttk.Button(parent, text=text("detect"), command=detect_command).grid(
            row=row, column=2, padx=(0, 8), pady=(14, 0)
        )
        ttk.Button(parent, text=text("browse"), command=browse_command).grid(
            row=row, column=3, pady=(14, 0)
        )

    def start(self) -> None:
        if self._exiting: return
        self.agent_service.start()
        self.controller.start()
        self.refresh_ui()

    def stop(self) -> None:
        self.agent_service.stop()
        self.controller.stop()
        self.refresh_ui()

    def run_once(self) -> None:
        self.controller.run_all_once_safely()
        self.refresh_ui()

    def open_hub(self) -> bool:
        from .desktop_window import launch_desktop
        try:
            launched = launch_desktop(self.config_path, hub_override=self.hub_var.get() or self.config.hub)
            if launched:
                return True
        except (OSError, RuntimeError) as exc:
            self.append_log('桌面窗口暂未启动，已打开网页工作区：' + str(exc)[:250])
        webbrowser.open(hub_workspace_url(self.hub_var.get() or self.config.hub))
        return False

    def _open_desktop_workspace(self) -> None:
        opened = self.open_hub()
        if opened and self.tray.is_running:
            self.root.withdraw()

    def open_toolbox(self) -> None:
        if self._exiting:
            return
        from .local_tools import launch_toolbox
        try:
            launch_toolbox(self.config_path)
        except OSError as exc:
            messagebox.showerror("无法打开工具箱", str(exc), parent=self.root)

    def open_inbox(self) -> None:
        webbrowser.open(self.inbox_url_var.get().strip() or self.config.inbox_url)

    def show_window(self) -> None:
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def open_config(self) -> None:
        open_path(self.config_path)

    def open_prompts(self) -> None:
        prompt_root = (
            default_app_dir() / "bindings"
            if self.config.bindings
            else self.config.prompt_dir
        )
        prompt_root.mkdir(parents=True, exist_ok=True)
        open_path(prompt_root)

    def on_binding_selected(self, _event=None) -> None:
        selection = self.binding_tree.selection()
        if not selection:
            return
        backend = str(self.binding_tree.set(selection[0], "backend")).lower()
        if selection[0] == "hub-agents":
            self.backend_notebook.select(self.agent_settings_tab)
        elif backend == "hermes":
            self.backend_notebook.select(self.hermes_settings_tab)
        elif backend == "codex":
            self.backend_notebook.select(self.codex_settings_tab)

    def browse_codex(self) -> None:
        self._browse_command(self.codex_path_var, text("select_codex"), "*.exe")

    def browse_codex_cli(self) -> None:
        self._browse_command(
            self.codex_cli_path_var, text("select_codex_cli"), "*.cmd *.bat *.exe"
        )

    def browse_hermes(self) -> None:
        self._browse_command(
            self.hermes_cli_path_var, text("select_hermes_cli"), "*.exe *.cmd *.bat"
        )

    def browse_cmynetwork(self) -> None:
        self._browse_command(self.vpn_path_var, text("select_cmynetwork"), "*.exe")

    def _browse_command(self, variable: tk.StringVar, title: str, pattern: str) -> None:
        path = filedialog.askopenfilename(
            title=title,
            filetypes=[(text("command_files"), pattern), (text("all_files"), "*.*")],
        )
        if path:
            variable.set(path)

    def detect_codex(self) -> None:
        self._set_detected_path(
            self.codex_path_var,
            find_default_codex_path(),
            text("detected_codex"),
            text("codex_not_detected"),
        )

    def detect_codex_cli(self) -> None:
        self._set_detected_path(
            self.codex_cli_path_var,
            find_default_codex_cli_path(),
            text("detected_codex_cli"),
            text("codex_cli_not_detected"),
        )

    def detect_hermes(self) -> None:
        self._set_detected_path(
            self.hermes_cli_path_var,
            default_hermes_cli_path(),
            text("detected_hermes"),
            text("hermes_not_detected"),
        )

    def detect_cmynetwork(self) -> None:
        self._set_detected_path(
            self.vpn_path_var,
            find_default_cmynetwork_path(),
            text("detected_cmynetwork"),
            text("cmynetwork_not_detected"),
        )

    def _set_detected_path(
        self,
        variable: tk.StringVar,
        path: Path | None,
        success: str,
        failure: str,
    ) -> None:
        if path:
            variable.set(str(path))
            self.append_log(f"{success}: {path}")
        else:
            self.append_log(failure)

    def save_settings(self) -> None:
        try:
            codex_interval = self._int_value(self.codex_interval_var, 20, minimum=1)
            hermes_interval = self._int_value(self.hermes_interval_var, 30, minimum=1)
            codex_timeout = self._int_value(
                self.codex_exec_timeout_var, 1800, minimum=30
            )
            hermes_timeout = self._int_value(
                self.hermes_timeout_var, 1800, minimum=30
            )
            submit_delay = self._float_value(
                self.codex_submit_delay_var, 3.0, minimum=0.0
            )
            hub_timeout = self._int_value(self.hub_timeout_var, 12, minimum=3)
            retry_attempts = self._int_value(
                self.hub_retry_attempts_var, 3, minimum=1, maximum=5
            )
            retry_backoff = self._float_value(
                self.hub_retry_backoff_var, 1.0, minimum=0.0
            )
            update_hub_client_settings(
                self.config,
                hub=self.hub_var.get(),
                timeout_seconds=hub_timeout,
                get_retry_attempts=retry_attempts,
                retry_backoff_seconds=retry_backoff,
                inbox_url=self.inbox_url_var.get(),
            )
        except ValueError as exc:
            messagebox.showerror(text("window_title"), str(exc))
            return

        update_local_tool_settings(
            self.config,
            codex_exe_path=self.codex_path_var.get(),
            auto_submit_codex_prompt=self.auto_submit_codex_var.get(),
            codex_submit_delay_seconds=submit_delay,
            codex_submit_hotkey=self.codex_submit_hotkey_var.get(),
            auto_execute_codex=self.auto_execute_codex_var.get(),
            codex_cli_path=self.codex_cli_path_var.get(),
            codex_exec_sandbox=self.codex_exec_sandbox_var.get(),
            codex_exec_timeout_seconds=codex_timeout,
            vpn_exe_path=self.vpn_path_var.get(),
            vpn_health_url=self.chatgpt_url_var.get(),
            vpn_auto_recover=self.vpn_auto_recover_var.get(),
            vpn_switch_command=self.vpn_switch_command_var.get(),
        )
        update_binding_runtime_settings(
            self.config,
            "codex",
            enabled=self.codex_enabled_var.get(),
            interval_seconds=codex_interval,
            open_codex=self.codex_open_var.get(),
            auto_execute_codex=self.auto_execute_codex_var.get(),
        )
        update_binding_runtime_settings(
            self.config,
            "hermes",
            enabled=self.hermes_enabled_var.get(),
            interval_seconds=hermes_interval,
            open_codex=False,
            auto_execute_codex=self.hermes_auto_execute_var.get(),
        )
        update_hermes_tool_settings(
            self.config,
            hermes_cli_path=self.hermes_cli_path_var.get(),
            hermes_model=self.hermes_model_var.get(),
            hermes_provider=self.hermes_provider_var.get(),
            hermes_toolsets=self.hermes_toolsets_var.get(),
            hermes_timeout_seconds=hermes_timeout,
        )
        was_running = self.controller.is_running
        if was_running:
            self.controller.stop()
        save_config(self.config, self.config_path)
        try:
            self.controller = BridgeController(
                self.config, log_callback=self.append_log
            )
        except Exception as exc:
            messagebox.showerror(text("window_title"), f"{text('bridge_error')}: {exc}")
            return
        if was_running:
            self.controller.start()
        self.append_log(text("settings_saved_restarted") if was_running else text("settings_saved"))
        self.refresh_ui()

    def _int_value(
        self,
        variable: tk.StringVar,
        default: int,
        *,
        minimum: int,
        maximum: int | None = None,
    ) -> int:
        try:
            value = int(variable.get().strip())
        except ValueError:
            value = default
        value = max(minimum, value)
        if maximum is not None:
            value = min(maximum, value)
        variable.set(str(value))
        return value

    def _float_value(
        self,
        variable: tk.StringVar,
        default: float,
        *,
        minimum: float,
    ) -> float:
        try:
            value = float(variable.get().strip())
        except ValueError:
            value = default
        value = max(minimum, value)
        variable.set(str(value))
        return value

    def test_hub(self) -> None:
        runtime_configs = self.config.runtime_configs()
        if not runtime_configs:
            self.hub_test_var.set(text("missing_token"))
            return
        try:
            timeout = self._int_value(self.hub_timeout_var, 12, minimum=3)
            attempts = self._int_value(
                self.hub_retry_attempts_var, 3, minimum=1, maximum=5
            )
            backoff = self._float_value(
                self.hub_retry_backoff_var, 1.0, minimum=0.0
            )
        except ValueError:
            return
        hub = self.hub_var.get().strip().rstrip("/")
        token = runtime_configs[0].token
        self.hub_test_var.set(text("testing"))

        def worker() -> None:
            started = time.perf_counter()
            try:
                inbox = HubClient(
                    hub,
                    token,
                    timeout_seconds=timeout,
                    get_retry_attempts=attempts,
                    retry_backoff_seconds=backoff,
                    source_ip=getattr(self.config, "hub_source_ip", ""),
                ).get_inbox()
                elapsed = time.perf_counter() - started
                device = str((inbox.get("device") or {}).get("label") or "device")
                message = f"{text('hub_test_ok')}：{device}，{elapsed:.2f}s"
            except Exception as exc:
                message = f"{text('hub_test_failed')}：{exc}"
            self.ui_queue.put(("hub_test", message))
            self.append_log(message)

        threading.Thread(target=worker, name="CodexBridge-HubTest", daemon=True).start()

    def test_chatgpt(self) -> None:
        url = self.chatgpt_url_var.get().strip() or DEFAULT_VPN_HEALTH_URL
        result = UrlHealthProbe().check(
            url, int(getattr(self.config, "vpn_check_timeout_seconds", 8))
        )
        self.append_log(
            f"{text('chatgpt_ok') if result.ok else text('chatgpt_failed')}: "
            f"{result.detail}"
        )

    def open_chatgpt(self) -> None:
        webbrowser.open(self.chatgpt_url_var.get().strip() or DEFAULT_VPN_HEALTH_URL)

    def toggle_startup(self) -> None:
        try:
            self.startup.set_enabled(
                self.startup_var.get(), Path(sys.executable), args=["--start"]
            )
            self.append_log(
                text("startup_enabled")
                if self.startup_var.get()
                else text("startup_disabled")
            )
        except Exception as exc:
            self.startup_var.set(self.startup.is_enabled())
            messagebox.showerror(text("window_title"), f"{text('startup_error')}: {exc}")

    def append_log(self, message: str) -> None:
        try:
            self.persistent_log.write(message)
        except OSError:
            pass
        self.log_queue.put(message)

    def clear_logs(self) -> None:
        try:
            self.persistent_log.clear()
        except OSError:
            pass
        self.log_lines.clear()
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    def render_log(self) -> None:
        changed = False
        while True:
            try:
                self.log_lines.append(self.log_queue.get_nowait())
                changed = True
            except queue.Empty:
                break
        if not changed:
            return
        self.log_lines = self.log_lines[-500:]
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.insert("end", "\n".join(self.log_lines))
        self.log_text.configure(state="disabled")
        self.log_text.see("end")

    def refresh_ui(self) -> None:
        if self._exiting:
            return
        if self._refresh_after_id:
            try:
                self.root.after_cancel(self._refresh_after_id)
            except tk.TclError:
                pass
        self._drain_ui_queue()
        self.render_log()
        agent = self.agent_service.snapshot()
        self.agent_status_var.set(f"{agent['device']} · {agent['status']}")
        self.status_var.set(f"Hub 智能体：{agent['status']}  ·  其他连接：{self.controller.status_text}")
        snapshots = [agent, *self.controller.binding_snapshots()]
        pending_count = sum(int(row.get("pending_count") or 0) for row in snapshots)
        self.pending_button.configure(
            text=f"待确认项 ({pending_count})" if pending_count else "待确认项"
        )
        devices = [row["device"] for row in snapshots if row["device"] != "-" and str(row['status']).startswith('在线')]
        self.device_var.set("、".join(devices) if devices else "-")
        self._refresh_binding_tree(snapshots)
        running = self.controller.is_running or self.agent_service.is_running
        fully_running = (self.controller.is_running or not self.config.runtime_configs()) and (self.agent_service.is_running or not self.agent_service.configured)
        self.start_button.configure(state="disabled" if fully_running else "normal")
        self.stop_button.configure(state="normal" if running else "disabled")
        self.run_once_button.configure(state="disabled" if running else "normal")
        self._refresh_after_id = self.root.after(1000, self.refresh_ui)

    def _drain_ui_queue(self) -> None:
        while True:
            try:
                event, value = self.ui_queue.get_nowait()
            except queue.Empty:
                break
            if event == "hub_test":
                self.hub_test_var.set(value)

    def _refresh_binding_tree(self, snapshots: list[dict]) -> None:
        rows = self._binding_rows(snapshots)
        existing = set(self.binding_tree.get_children())
        current: set[str] = set()
        for row in rows:
            item_id = str(row["id"])
            current.add(item_id)
            status = str(row["status"])
            tag = (
                "disabled"
                if status == text("disabled")
                else "error"
                if status.startswith(("异常", "错误"))
                else "online"
                if status == "在线"
                else ""
            )
            values = (
                row["name"],
                row["backend"],
                row["device"],
                f"{row['interval']} 秒",
                status,
            )
            if item_id in existing:
                self.binding_tree.item(item_id, values=values, tags=(tag,))
            else:
                self.binding_tree.insert(
                    "", "end", iid=item_id, values=values, tags=(tag,)
                )
        for item_id in existing - current:
            self.binding_tree.delete(item_id)
        if rows and not self.binding_tree.selection():
            self.binding_tree.selection_set(str(rows[0]["id"]))

    def _binding_rows(self, snapshots: list[dict]) -> list[dict]:
        snapshot_map = {str(row["id"]): row for row in snapshots}
        if not self.config.bindings:
            return [
                {
                    **row,
                    "interval": row.get("interval", self.config.interval_seconds),
                }
                for row in snapshots
            ]
        rows: list[dict] = [row for row in snapshots if row['id'] == 'hub-agents']
        for binding in self.config.bindings:
            snapshot = snapshot_map.get(binding.id, {})
            rows.append(
                {
                    "id": binding.id,
                    "name": binding.name or binding.execution_backend.title(),
                    "backend": binding.execution_backend,
                    "device": snapshot.get("device", "-"),
                    "interval": binding.interval_seconds or self.config.interval_seconds,
                    "status": (
                        snapshot.get("status", text("connecting"))
                        if binding.enabled
                        else text("disabled")
                    ),
                }
            )
        return rows

    def on_close(self) -> None:
        if self.tray.is_running and not self._exiting:
            self.root.withdraw()
            self.append_log("CodexBridge 仍在系统托盘运行。")
            return
        self.exit_app()

    def exit_app(self) -> None:
        if self._exiting: return
        self._exiting = True
        if self._refresh_after_id:
            try:
                self.root.after_cancel(self._refresh_after_id)
            except tk.TclError:
                pass
        self.agent_service.stop()
        self.status_var.set("正在安全退出，等待已有工作收尾…")
        self.start_button.configure(state='disabled')
        self.stop_button.configure(state='disabled')
        self.run_once_button.configure(state='disabled')
        # Keep Tk responsive while legacy lanes drain too.
        self._exit_stop_thread = threading.Thread(target=self.controller.stop, daemon=True)
        self._exit_stop_thread.start()
        self._finish_exit()

    def _finish_exit(self) -> None:
        if self.agent_service.is_running or self.controller.is_running or self._exit_stop_thread.is_alive():
            self.render_log()
            self.root.after(250, self._finish_exit)
            return
        self.tray.stop()
        try:
            from .desktop_window import request_desktop_close
            request_desktop_close(self.config_path)
        except (OSError, RuntimeError):
            pass
        self.root.destroy()

    def run(self) -> int:
        if self._background_start_failed:
            return 2
        try:
            if not self.config.runtime_configs() and not self.agent_service.configured:
                if self.background:
                    self.append_log('后台启动未完成：尚无可用执行连接；请从正常应用入口完成配置。')
                    self.root.destroy()
                    return 2
                messagebox.showwarning(
                    text("window_title"), f"{text('missing_token')}\n{self.config_path}"
                )
            self.root.mainloop()
        finally:
            self.tray.stop()
        return 0


def binding_value(binding: BridgeBinding | None, name: str, fallback):
    if binding is None:
        return fallback
    value = getattr(binding, name, None)
    return fallback if value is None else value


def open_path(path: Path) -> None:
    if sys.platform.startswith("win"):
        os.startfile(path)  # type: ignore[attr-defined]
        return
    if sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
        return
    subprocess.Popen(["xdg-open", str(path)])


def asset_path(*parts: str) -> Path:
    bundle_dir = getattr(sys, "_MEIPASS", None)
    if bundle_dir:
        return Path(bundle_dir, "codex_bridge", "assets", *parts)
    return Path(__file__).resolve().parent / "assets" / Path(*parts)


def set_window_icon(root: tk.Tk) -> None:
    icon_path = asset_path("codex-owl.ico")
    if not icon_path.exists():
        return
    try:
        root.iconbitmap(str(icon_path))
    except tk.TclError:
        return


def path_text(path: Path | None) -> str:
    return str(path) if path else ""


def main(config_path: Path | None = None, auto_start: bool = False, *, background: bool = False) -> int:
    return BridgeApp(config_path, auto_start=auto_start, background=background).run()
