from __future__ import annotations

import asyncio
import os
import queue
import subprocess
import sys
import threading
import traceback
import tkinter as tk
from dataclasses import dataclass
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any

from .config import (
    CATEGORY_LABELS,
    DEFAULT_BENCHMARK_BLOCK_MAX,
    DEFAULT_BENCHMARK_BLOCK_MIN,
    DEFAULT_BENCHMARK_SAMPLE_FILES,
    DEFAULT_BENCHMARK_SECONDS,
    DEFAULT_BENCHMARK_WORKER_MAX,
    DEFAULT_CONFIG_PATH,
    DEFAULT_DESTINATION,
    DEFAULT_PART_SIZE_KB,
    DEFAULT_RETRIES,
    DEFAULT_SESSION_PATH,
    DEFAULT_STALL_TIMEOUT_SECONDS,
    DEFAULT_STATE_DB_PATH,
    DEFAULT_UI_REFRESH_SECONDS,
    DEFAULT_WORKERS,
    FILTER_PRESETS,
    AppSettings,
    build_settings,
    normalize_categories,
    normalize_extensions,
    read_config,
    save_settings,
)
from .engine import (
    DownloadController,
    ScanResult,
    benchmark_settings,
    human_size,
    run_download,
    scan_channel,
)
from .state import StateStore


class InlineProgressBar(ttk.Frame):
    """Canvas-based progress bar with centered inline text."""

    def __init__(
        self,
        parent,
        *,
        height: int = 24,
        fill_color: str = "#8fd3b1",
        background_color: str = "#dfe9e4",
        border_color: str = "#c8d6cf",
        text_color: str = "#173541",
    ) -> None:
        super().__init__(parent)
        self.height = height
        self.fill_color = fill_color
        self.background_color = background_color
        self.border_color = border_color
        self.text_color = text_color
        self.percent = 0.0
        self.text = ""

        self.canvas = tk.Canvas(
            self,
            height=height,
            highlightthickness=0,
            relief="flat",
            bd=0,
            bg=background_color,
        )
        self.canvas.pack(fill="x", expand=True)
        self.canvas.bind("<Configure>", self._on_resize)

    def set(self, percent: float, text: str) -> None:
        self.percent = max(0.0, min(100.0, float(percent)))
        self.text = text
        self._redraw()

    def _on_resize(self, _event=None) -> None:
        self._redraw()

    def _redraw(self) -> None:
        width = max(int(self.canvas.winfo_width()), 1)
        height = self.height
        fill_width = int(width * (self.percent / 100.0))

        self.canvas.delete("all")
        self.canvas.create_rectangle(0, 0, width, height, outline=self.border_color, fill=self.background_color)
        if fill_width > 0:
            self.canvas.create_rectangle(0, 0, fill_width, height, outline="", fill=self.fill_color)
        self.canvas.create_text(
            width // 2,
            height // 2,
            text=self.text,
            fill=self.text_color,
            font=("Segoe UI Semibold", 9),
        )


@dataclass(frozen=True, slots=True)
class SettingsSignature:
    source: str
    destination: str
    categories: tuple[str, ...]
    extensions: tuple[str, ...]


class TelegramDownloaderGUI(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Telegram Downloader")
        self.geometry("1480x940")
        self.minsize(1100, 720)
        self.configure(bg="#f3f5f7")

        self.config_path = Path(DEFAULT_CONFIG_PATH).resolve()
        self.event_queue: queue.Queue[tuple[str, dict[str, Any]]] = queue.Queue()
        self.worker_thread: threading.Thread | None = None
        self.operation_mode: str | None = None
        self.operation_settings_signature: SettingsSignature | None = None
        self.download_controller: DownloadController | None = None
        self.last_scan_result: ScanResult | None = None
        self.last_scan_signature: SettingsSignature | None = None
        self.history_counts: dict[str, int] = {}
        self.scan_summary: dict[str, int] = {}
        self.progress_snapshot: dict[str, Any] = {}
        self.pending_rows: dict[int, str] = {}
        self.completed_rows: dict[int, str] = {}
        self.error_rows: dict[int, str] = {}
        self.active_widgets: dict[int, dict[str, Any]] = {}
        self.settings_window: tk.Toplevel | None = None
        self.settings_benchmark_button: ttk.Button | None = None
        self.notebook: ttk.Notebook | None = None
        self.search_var = tk.StringVar()
        self.pending_dataset: list[dict[str, Any]] = []
        self.completed_dataset: list[dict[str, Any]] = []
        self.error_dataset: list[dict[str, Any]] = []
        self.active_canvas: tk.Canvas | None = None
        self.active_window_id: int | None = None
        self.stop_requested = False

        self.hidden_session_path = str(DEFAULT_SESSION_PATH)
        self.hidden_state_db_path = str(DEFAULT_STATE_DB_PATH)

        self._build_style()
        self._build_variables()
        self._build_layout()
        self._load_config_into_form()
        self._reload_history_from_db()
        self._refresh_header_metrics()
        self._set_log_placeholder()
        self.after(150, self._drain_events)

    def _build_style(self) -> None:
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure(".", font=("Segoe UI", 10), background="#f3f5f7", foreground="#1f2933")
        style.configure("Title.TLabel", font=("Segoe UI Semibold", 28), foreground="#0f2f3a")
        style.configure("Subtitle.TLabel", font=("Segoe UI", 11), foreground="#4b6570")
        style.configure("Card.TLabelframe", background="#ffffff", borderwidth=1, relief="solid")
        style.configure("Card.TLabelframe.Label", font=("Segoe UI Semibold", 11), foreground="#173541")
        style.configure("Surface.TFrame", background="#ffffff")
        style.configure("Muted.TLabel", foreground="#5b7280", background="#ffffff")
        style.configure("Value.TLabel", font=("Segoe UI Semibold", 12), foreground="#173541", background="#ffffff")
        style.configure("ActiveTitle.TLabel", font=("Segoe UI Semibold", 11), foreground="#173541", background="#ffffff")
        style.configure("TNotebook", background="#ffffff", borderwidth=0, tabmargins=(0, 0, 0, 0))
        style.configure("TNotebook.Tab", background="#eef2f3", padding=(12, 6), foreground="#173541")
        style.map(
            "TNotebook.Tab",
            background=[("selected", "#ffffff"), ("active", "#f7fafb")],
            foreground=[("selected", "#173541")],
        )
        style.configure("Treeview", font=("Segoe UI", 10), rowheight=24, fieldbackground="#ffffff", background="#ffffff")
        style.configure("Treeview.Heading", font=("Segoe UI Semibold", 10))
        style.configure("Accent.Horizontal.TProgressbar", troughcolor="#dbe7e1", background="#2f9e6f", bordercolor="#dbe7e1")
        style.configure("Quiet.TButton", padding=(10, 7))
        style.configure("Action.TButton", padding=(12, 8))
        style.map(
            "Action.TButton",
            foreground=[("disabled", "#667784"), ("!disabled", "#173541")],
            background=[("disabled", "#e7ecef"), ("!disabled", "#f7f9fa")],
        )

    def _build_variables(self) -> None:
        self.api_id_var = tk.StringVar()
        self.api_hash_var = tk.StringVar()
        self.source_var = tk.StringVar()
        self.destination_var = tk.StringVar(value=str(DEFAULT_DESTINATION))
        self.filter_preset_var = tk.StringVar(value="Everything")
        self.extensions_var = tk.StringVar()

        self.retries_var = tk.IntVar(value=DEFAULT_RETRIES)
        self.workers_var = tk.IntVar(value=DEFAULT_WORKERS)
        self.part_size_var = tk.IntVar(value=DEFAULT_PART_SIZE_KB)
        self.refresh_var = tk.DoubleVar(value=DEFAULT_UI_REFRESH_SECONDS)
        self.stall_timeout_var = tk.IntVar(value=DEFAULT_STALL_TIMEOUT_SECONDS)
        self.extract_archives_var = tk.BooleanVar(value=False)
        self.keep_archives_var = tk.BooleanVar(value=True)

        self.benchmark_source_var = tk.StringVar()
        self.benchmark_sample_files_var = tk.IntVar(value=DEFAULT_BENCHMARK_SAMPLE_FILES)
        self.benchmark_seconds_var = tk.IntVar(value=DEFAULT_BENCHMARK_SECONDS)
        self.benchmark_block_min_var = tk.IntVar(value=DEFAULT_BENCHMARK_BLOCK_MIN)
        self.benchmark_block_max_var = tk.IntVar(value=DEFAULT_BENCHMARK_BLOCK_MAX)
        self.benchmark_worker_max_var = tk.IntVar(value=DEFAULT_BENCHMARK_WORKER_MAX)

        self.category_vars = {
            key: tk.BooleanVar(value=(key in FILTER_PRESETS["Everything"]["categories"]))
            for key in CATEGORY_LABELS
        }

        self.status_var = tk.StringVar(value="Ready. Save your settings, then run Connect & Scan.")
        self.header_metrics_var = tk.StringVar(value="Pending 0 | Remaining 0B | Speed 0B/s | History 0")
        self.channel_title_var = tk.StringVar(value="No channel scanned yet.")
        self.channel_meta_var = tk.StringVar(value="Channel details will appear here after Connect & Scan.")
        self.channel_about_var = tk.StringVar(value="")
        self.global_progress_var = tk.DoubleVar(value=0.0)
        self.global_caption_var = tk.StringVar(value="Global progress | 0/0 files | 0.0B/0.0B | 0.0%")
        self.active_caption_var = tk.StringVar(value="")

    def _build_layout(self) -> None:
        container = ttk.Frame(self, padding=18)
        container.pack(fill="both", expand=True)
        container.columnconfigure(0, weight=0)
        container.columnconfigure(1, weight=1)
        container.rowconfigure(3, weight=1)

        ttk.Label(container, text="Telegram Downloader", style="Title.TLabel").grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Label(
            container,
            text="Simple desktop control panel for Telegram bulk downloads, scan caching, history, and benchmark tuning.",
            style="Subtitle.TLabel",
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(2, 14))

        self._build_action_bar(container)
        self._build_left_panel(container)
        self._build_right_panel(container)

    def _build_action_bar(self, parent: ttk.Frame) -> None:
        bar = ttk.Frame(parent, padding=(0, 0, 0, 14))
        bar.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(0, 14))
        for column in range(6):
            bar.columnconfigure(column, weight=1)

        self.save_button = ttk.Button(bar, text="Save settings", style="Action.TButton", command=self.save_settings_action)
        self.scan_button = ttk.Button(bar, text="Connect & Scan", style="Action.TButton", command=self.connect_and_scan_action)
        self.start_button = ttk.Button(bar, text="Start download", style="Action.TButton", command=self.start_download_action)
        self.stop_button = ttk.Button(bar, text="Stop gracefully", style="Action.TButton", command=self.stop_download_action)
        self.folder_button = ttk.Button(bar, text="Open download folder", style="Action.TButton", command=self.open_folder_action)
        self.settings_button = ttk.Button(bar, text="Settings...", style="Action.TButton", command=self.open_settings_dialog)

        self.save_button.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        self.scan_button.grid(row=0, column=1, sticky="ew", padx=8)
        self.start_button.grid(row=0, column=2, sticky="ew", padx=8)
        self.stop_button.grid(row=0, column=3, sticky="ew", padx=8)
        self.folder_button.grid(row=0, column=4, sticky="ew", padx=8)
        self.settings_button.grid(row=0, column=5, sticky="ew", padx=(8, 0))

        bar.columnconfigure(5, weight=1)
        self.stop_button.state(["disabled"])

    def _build_left_panel(self, parent: ttk.Frame) -> None:
        left = ttk.Frame(parent)
        left.grid(row=3, column=0, sticky="nsew", padx=(0, 16))
        left.columnconfigure(0, weight=1)

        connection = ttk.LabelFrame(left, text="Connection", style="Card.TLabelframe", padding=14)
        connection.grid(row=0, column=0, sticky="ew")
        connection.columnconfigure(0, weight=1)
        self._add_entry_field(connection, 0, "API ID", self.api_id_var)
        self._add_entry_field(connection, 1, "API Hash", self.api_hash_var, show="*")
        self._add_entry_field(connection, 2, "Channel", self.source_var)

        destination = ttk.LabelFrame(left, text="Destination", style="Card.TLabelframe", padding=14)
        destination.grid(row=1, column=0, sticky="ew", pady=(14, 0))
        destination.columnconfigure(0, weight=1)
        ttk.Entry(destination, textvariable=self.destination_var).grid(row=0, column=0, sticky="ew")
        ttk.Button(destination, text="Browse...", style="Quiet.TButton", command=self.browse_destination_action).grid(row=0, column=1, padx=(10, 0))

        filters = ttk.LabelFrame(left, text="Filters", style="Card.TLabelframe", padding=14)
        filters.grid(row=2, column=0, sticky="ew", pady=(14, 0))
        filters.columnconfigure(1, weight=1)

        ttk.Label(filters, text="Preset").grid(row=0, column=0, sticky="w")
        preset_box = ttk.Combobox(
            filters,
            textvariable=self.filter_preset_var,
            state="readonly",
            values=list(FILTER_PRESETS.keys()),
        )
        preset_box.grid(row=0, column=1, sticky="ew")
        preset_box.bind("<<ComboboxSelected>>", self._on_preset_selected)

        checkbox_frame = ttk.Frame(filters, style="Surface.TFrame")
        checkbox_frame.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(10, 8))
        for column in range(3):
            checkbox_frame.columnconfigure(column, weight=1)
        for index, (category, label) in enumerate(CATEGORY_LABELS.items()):
            ttk.Checkbutton(
                checkbox_frame,
                text=label,
                variable=self.category_vars[category],
                command=self._mark_custom_filter,
            ).grid(row=index // 3, column=index % 3, sticky="w", padx=(0, 10), pady=2)

        ttk.Label(filters, text="Extra extensions").grid(row=2, column=0, sticky="w", pady=(6, 0))
        extensions_entry = ttk.Entry(filters, textvariable=self.extensions_var)
        extensions_entry.grid(row=2, column=1, sticky="ew", pady=(6, 0))
        extensions_entry.bind("<KeyRelease>", lambda _event: self._mark_custom_filter())
        ttk.Label(
            filters,
            text=".zip .rar .mp3 .flac etc. Leave empty to use categories only.",
            style="Muted.TLabel",
        ).grid(row=3, column=0, columnspan=2, sticky="w", pady=(6, 0))

        channel = ttk.LabelFrame(left, text="Channel snapshot", style="Card.TLabelframe", padding=14)
        channel.grid(row=3, column=0, sticky="ew", pady=(14, 0))
        channel_body = ttk.Frame(channel, style="Surface.TFrame")
        channel_body.pack(fill="x")
        channel_body.columnconfigure(0, weight=1)
        ttk.Label(channel_body, textvariable=self.channel_title_var, style="Value.TLabel", wraplength=360, justify="left").grid(row=0, column=0, sticky="w")
        ttk.Label(channel_body, textvariable=self.channel_meta_var, style="Muted.TLabel", wraplength=360, justify="left").grid(row=1, column=0, sticky="w", pady=(6, 0))
        ttk.Label(channel_body, textvariable=self.channel_about_var, style="Muted.TLabel", wraplength=360, justify="left").grid(row=2, column=0, sticky="w", pady=(6, 0))

    def _build_right_panel(self, parent: ttk.Frame) -> None:
        right = ttk.Frame(parent)
        right.grid(row=3, column=1, sticky="nsew")
        right.columnconfigure(0, weight=1)
        right.rowconfigure(1, weight=1)

        live = ttk.LabelFrame(right, text="Live status", style="Card.TLabelframe", padding=14)
        live.grid(row=0, column=0, sticky="ew")
        live.columnconfigure(0, weight=1)

        ttk.Label(live, textvariable=self.header_metrics_var, style="Muted.TLabel").grid(row=0, column=0, sticky="w")

        self.global_bar = InlineProgressBar(live, height=26)
        self.global_bar.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        self.global_bar.set(0.0, self.global_caption_var.get())

        ttk.Label(live, text="Active downloads", style="Value.TLabel").grid(row=2, column=0, sticky="w", pady=(14, 4))
        self.active_summary_label = ttk.Label(live, textvariable=self.active_caption_var, style="Muted.TLabel")
        self.active_summary_label.grid(row=3, column=0, sticky="w")

        active_panel = ttk.Frame(live, style="Surface.TFrame")
        active_panel.grid(row=4, column=0, sticky="ew", pady=(8, 0))
        active_panel.columnconfigure(0, weight=1)
        active_panel.rowconfigure(0, weight=1)

        self.active_canvas = tk.Canvas(active_panel, height=240, bg="#ffffff", highlightthickness=0, relief="flat", bd=0)
        self.active_canvas.grid(row=0, column=0, sticky="ew")
        active_scrollbar = ttk.Scrollbar(active_panel, orient="vertical", command=self.active_canvas.yview)
        active_scrollbar.grid(row=0, column=1, sticky="ns")
        self.active_canvas.configure(yscrollcommand=active_scrollbar.set)

        self.active_holder = ttk.Frame(self.active_canvas, style="Surface.TFrame")
        self.active_holder.columnconfigure(0, weight=1)
        self.active_window_id = self.active_canvas.create_window((0, 0), window=self.active_holder, anchor="nw")
        self.active_holder.bind("<Configure>", self._sync_active_scrollregion)
        self.active_canvas.bind("<Configure>", self._resize_active_window)
        self.active_placeholder = ttk.Label(self.active_holder, text="No active downloads.", style="Muted.TLabel")
        self.active_placeholder.grid(row=0, column=0, sticky="w")

        bottom = ttk.Frame(right, style="Surface.TFrame")
        bottom.grid(row=1, column=0, sticky="nsew", pady=(14, 0))
        bottom.columnconfigure(0, weight=1)
        bottom.rowconfigure(1, weight=1)

        search_row = ttk.Frame(bottom, style="Surface.TFrame")
        search_row.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        search_row.columnconfigure(1, weight=1)
        ttk.Label(search_row, text="Search").grid(row=0, column=0, sticky="w", padx=(0, 8))
        search_entry = ttk.Entry(search_row, textvariable=self.search_var)
        search_entry.grid(row=0, column=1, sticky="ew")
        search_entry.bind("<KeyRelease>", lambda _event: self._apply_search_filter())
        ttk.Button(search_row, text="Clear", style="Quiet.TButton", command=self._clear_search).grid(row=0, column=2, padx=(8, 0))

        self.notebook = ttk.Notebook(bottom)
        self.notebook.grid(row=1, column=0, sticky="nsew")

        pending_tab = ttk.Frame(self.notebook, padding=10)
        completed_tab = ttk.Frame(self.notebook, padding=10)
        errors_tab = ttk.Frame(self.notebook, padding=10)
        logs_tab = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(pending_tab, text="Pending")
        self.notebook.add(completed_tab, text="Completed")
        self.notebook.add(errors_tab, text="Errors")
        self.notebook.add(logs_tab, text="Logs")
        self.notebook.bind("<<NotebookTabChanged>>", lambda _event: self._apply_search_filter())

        self.pending_tree = self._make_tree(pending_tab, columns=("name", "size", "type"), headings=("Name", "Size", "Type"))
        self.completed_tree = self._make_tree(
            completed_tab,
            columns=("name", "size", "type", "status", "updated"),
            headings=("Name", "Size", "Type", "Status", "Updated"),
        )
        self.error_tree = self._make_tree(
            errors_tab,
            columns=("name", "error", "updated"),
            headings=("Name", "Error", "Updated"),
        )

        self.log_text = tk.Text(
            logs_tab,
            wrap="word",
            height=18,
            bg="#fbfcfc",
            fg="#22343c",
            insertbackground="#22343c",
            relief="solid",
            borderwidth=1,
            padx=10,
            pady=10,
        )
        self.log_text.pack(fill="both", expand=True)
        self.log_text.configure(state="disabled")

    def _make_tree(self, parent: ttk.Frame, *, columns: tuple[str, ...], headings: tuple[str, ...]) -> ttk.Treeview:
        container = ttk.Frame(parent)
        container.pack(fill="both", expand=True)
        tree = ttk.Treeview(container, columns=columns, show="headings")
        for column, heading in zip(columns, headings):
            width = 140
            anchor = "w"
            if column in {"size", "status", "updated", "type"}:
                width = 120
            if column == "error":
                width = 420
            if column == "name":
                width = 520
            tree.heading(column, text=heading)
            tree.column(column, width=width, anchor=anchor, stretch=(column == "name" or column == "error"))
        tree.pack(side="left", fill="both", expand=True)
        scrollbar = ttk.Scrollbar(container, orient="vertical", command=tree.yview)
        scrollbar.pack(side="right", fill="y")
        tree.configure(yscrollcommand=scrollbar.set)
        return tree

    def _add_entry_field(
        self,
        parent: ttk.LabelFrame,
        row: int,
        label: str,
        variable: tk.StringVar,
        *,
        show: str | None = None,
    ) -> None:
        ttk.Label(parent, text=label).grid(row=row * 2, column=0, sticky="w")
        ttk.Entry(parent, textvariable=variable, show=show or "").grid(row=row * 2 + 1, column=0, sticky="ew", pady=(2, 10))

    def _load_config_into_form(self) -> None:
        config = read_config(self.config_path)
        self.api_id_var.set(config.get("api_id", ""))
        self.api_hash_var.set(config.get("api_hash", ""))
        self.source_var.set(config.get("source", ""))
        self.destination_var.set(config.get("destination", str(DEFAULT_DESTINATION)))
        self.hidden_session_path = config.get("session", str(DEFAULT_SESSION_PATH))
        self.hidden_state_db_path = config.get("state_db", str(DEFAULT_STATE_DB_PATH))

        self.retries_var.set(int(config.get("retries", str(DEFAULT_RETRIES))))
        self.workers_var.set(int(config.get("workers", str(DEFAULT_WORKERS))))
        self.part_size_var.set(int(config.get("part_size_kb", str(DEFAULT_PART_SIZE_KB))))
        self.refresh_var.set(float(config.get("progress_interval", str(DEFAULT_UI_REFRESH_SECONDS))))
        self.stall_timeout_var.set(int(config.get("stall_timeout", str(DEFAULT_STALL_TIMEOUT_SECONDS))))
        self.extract_archives_var.set(config.get("extract_zips", "false").strip().lower() in {"1", "true", "yes", "on"})
        self.keep_archives_var.set(config.get("keep_archives", "true").strip().lower() in {"1", "true", "yes", "on"})

        self.benchmark_source_var.set(config.get("test_source", ""))
        self.benchmark_sample_files_var.set(int(config.get("test_sample_size", str(DEFAULT_BENCHMARK_SAMPLE_FILES))))
        self.benchmark_seconds_var.set(int(config.get("test_duration_seconds", str(DEFAULT_BENCHMARK_SECONDS))))
        self.benchmark_block_min_var.set(int(config.get("test_block_min", str(DEFAULT_BENCHMARK_BLOCK_MIN))))
        self.benchmark_block_max_var.set(int(config.get("test_block_max", str(DEFAULT_BENCHMARK_BLOCK_MAX))))
        self.benchmark_worker_max_var.set(int(config.get("test_worker_max", str(DEFAULT_BENCHMARK_WORKER_MAX))))

        include_raw = config.get("include", "all")
        extensions_raw = config.get("extensions", "")
        preset = config.get("filter_preset", "Everything") or "Everything"
        self._apply_filter_state(include_raw, extensions_raw, preset)

    def _apply_filter_state(self, include_raw: str, extensions_raw: str, preset: str) -> None:
        categories = normalize_categories(include_raw)
        extensions = normalize_extensions(extensions_raw)
        for key, variable in self.category_vars.items():
            variable.set(key in categories)
        self.extensions_var.set(" ".join(sorted(extensions)))
        self.filter_preset_var.set(preset if preset in FILTER_PRESETS else "Custom")

    def _on_preset_selected(self, _event=None) -> None:
        preset = self.filter_preset_var.get()
        if preset == "Custom":
            return
        profile = FILTER_PRESETS[preset]
        for category, variable in self.category_vars.items():
            variable.set(category in profile["categories"])
        self.extensions_var.set(" ".join(sorted(profile["extensions"])))

    def _mark_custom_filter(self) -> None:
        if self.filter_preset_var.get() != "Custom":
            self.filter_preset_var.set("Custom")

    def _selected_categories(self) -> set[str]:
        categories = {key for key, variable in self.category_vars.items() if variable.get()}
        return categories or set(CATEGORY_LABELS)

    def _settings_signature(self, settings: AppSettings) -> SettingsSignature:
        return SettingsSignature(
            source=settings.source,
            destination=str(settings.destination),
            categories=tuple(sorted(settings.categories)),
            extensions=tuple(sorted(settings.extensions)),
        )

    def _build_settings(self) -> AppSettings:
        if not self.api_id_var.get().strip():
            raise ValueError("API ID is required.")
        if not self.api_hash_var.get().strip():
            raise ValueError("API Hash is required.")
        if not self.source_var.get().strip():
            raise ValueError("Channel is required.")

        return build_settings(
            api_id=self.api_id_var.get().strip(),
            api_hash=self.api_hash_var.get().strip(),
            source=self.source_var.get().strip(),
            destination=self.destination_var.get().strip() or str(DEFAULT_DESTINATION),
            session_path=self.hidden_session_path,
            state_db_path=self.hidden_state_db_path,
            categories=self._selected_categories(),
            extensions=self.extensions_var.get().strip(),
            filter_preset=self.filter_preset_var.get().strip() or "Custom",
            extract_archives=self.extract_archives_var.get(),
            keep_archives=self.keep_archives_var.get(),
            retries=self.retries_var.get(),
            workers=self.workers_var.get(),
            part_size_kb=self.part_size_var.get(),
            ui_refresh_seconds=self.refresh_var.get(),
            stall_timeout_seconds=self.stall_timeout_var.get(),
            benchmark_source=self.benchmark_source_var.get().strip(),
            benchmark_seconds=self.benchmark_seconds_var.get(),
            benchmark_sample_files=self.benchmark_sample_files_var.get(),
            benchmark_block_min_kb=self.benchmark_block_min_var.get(),
            benchmark_block_max_kb=self.benchmark_block_max_var.get(),
            benchmark_worker_max=self.benchmark_worker_max_var.get(),
        )

    def save_settings_action(self) -> None:
        try:
            settings = self._build_settings()
            save_settings(settings, self.config_path)
            self.status_var.set(f"Settings saved to {self.config_path.name}.")
            self._append_log("Settings saved.", level="info")
            self._reload_history_from_db()
        except Exception as exc:
            messagebox.showerror("Invalid settings", str(exc))

    def browse_destination_action(self) -> None:
        selected = filedialog.askdirectory(initialdir=self.destination_var.get() or str(DEFAULT_DESTINATION))
        if selected:
            self.destination_var.set(selected)

    def open_folder_action(self) -> None:
        destination = Path(self.destination_var.get().strip() or str(DEFAULT_DESTINATION)).expanduser()
        destination.mkdir(parents=True, exist_ok=True)
        try:
            if os.name == "nt":
                os.startfile(destination)  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(destination)])
            else:
                subprocess.Popen(["xdg-open", str(destination)])
        except Exception as exc:
            messagebox.showerror("Open folder", f"Could not open the download folder.\n\n{exc}")

    def connect_and_scan_action(self) -> None:
        if self._busy():
            return
        try:
            settings = self._build_settings()
            save_settings(settings, self.config_path)
            signature = self._settings_signature(settings)
            self._prepare_for_scan()
            self._launch_background(
                mode="scan",
                signature=signature,
                runner=lambda: asyncio.run(
                    scan_channel(
                        settings,
                        event_callback=self._push_event,
                        allow_interactive_login=False,
                    )
                ),
            )
        except Exception as exc:
            messagebox.showerror("Scan setup", str(exc))

    def start_download_action(self) -> None:
        if self._busy():
            return
        try:
            settings = self._build_settings()
            save_settings(settings, self.config_path)
            signature = self._settings_signature(settings)
            cached_scan = self.last_scan_result if signature == self.last_scan_signature else None
            self.download_controller = DownloadController()
            self._prepare_for_download(cached_scan is None)
            if cached_scan is None:
                self._append_log("No cached scan matched the current settings. A fresh scan will run first.", level="warning")
            else:
                self._append_log("Using cached scan. Preparing downloads without rescanning.", level="info")
            self._launch_background(
                mode="download",
                signature=signature,
                runner=lambda: asyncio.run(
                    run_download(
                        settings,
                        scan_result=cached_scan,
                        event_callback=self._push_event,
                        controller=self.download_controller,
                        allow_interactive_login=False,
                    )
                ),
            )
        except Exception as exc:
            messagebox.showerror("Download setup", str(exc))

    def stop_download_action(self) -> None:
        if self.operation_mode != "download" or self.download_controller is None:
            return
        self._set_stop_state(True)
        self.download_controller.request_graceful_stop()
        self.status_var.set("Stopping gracefully. Waiting for active downloads to finish...")
        self._append_log("Graceful stop requested.", level="warning")

    def run_benchmark_action(self) -> None:
        if self._busy():
            return
        try:
            settings = self._build_settings()
            save_settings(settings, self.config_path)
            signature = self._settings_signature(settings)
            cached_scan = self.last_scan_result if signature == self.last_scan_signature else None
            self.status_var.set("Benchmark starting...")
            self.global_progress_var.set(0.0)
            self.global_caption_var.set("Benchmark | 0%")
            self.global_bar.set(0.0, self.global_caption_var.get())
            self._render_active_downloads([])
            self._launch_background(
                mode="benchmark",
                signature=signature,
                runner=lambda: asyncio.run(
                    benchmark_settings(
                        settings,
                        scan_result=cached_scan,
                        event_callback=self._push_event,
                        allow_interactive_login=False,
                    )
                ),
            )
        except Exception as exc:
            messagebox.showerror("Benchmark setup", str(exc))

    def open_settings_dialog(self) -> None:
        if self.settings_window and self.settings_window.winfo_exists():
            self.settings_window.focus_force()
            return

        dialog = tk.Toplevel(self)
        dialog.title("Settings")
        dialog.geometry("620x520")
        dialog.resizable(False, False)
        dialog.transient(self)
        dialog.configure(bg="#f3f5f7")
        self.settings_window = dialog

        body = ttk.Frame(dialog, padding=16)
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1)
        body.columnconfigure(1, weight=1)

        download_frame = ttk.LabelFrame(body, text="Download tuning", style="Card.TLabelframe", padding=12)
        download_frame.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        benchmark_frame = ttk.LabelFrame(body, text="Benchmark tuning", style="Card.TLabelframe", padding=12)
        benchmark_frame.grid(row=0, column=1, sticky="nsew", padx=(8, 0))

        self._add_spin_field(download_frame, 0, "Retries", self.retries_var, 1, 10)
        self._add_spin_field(download_frame, 1, "Workers", self.workers_var, 1, 8)
        self._add_spin_field(download_frame, 2, "Block size (KB)", self.part_size_var, 4, 512, increment=4)
        self._add_spin_field(download_frame, 3, "UI refresh (s)", self.refresh_var, 0.2, 5.0, increment=0.1)
        self._add_spin_field(download_frame, 4, "Stall timeout (s)", self.stall_timeout_var, 20, 600)
        ttk.Checkbutton(download_frame, text="Extract ZIP archives", variable=self.extract_archives_var).grid(row=10, column=0, sticky="w", pady=(10, 0))
        ttk.Checkbutton(download_frame, text="Keep original archives", variable=self.keep_archives_var).grid(row=11, column=0, sticky="w", pady=(6, 0))

        ttk.Label(benchmark_frame, text="Test channel (optional)").grid(row=0, column=0, sticky="w")
        ttk.Entry(benchmark_frame, textvariable=self.benchmark_source_var).grid(row=1, column=0, sticky="ew", pady=(2, 10))
        benchmark_frame.columnconfigure(0, weight=1)
        self._add_spin_field(benchmark_frame, 2, "Sample files", self.benchmark_sample_files_var, 1, 10)
        self._add_spin_field(benchmark_frame, 3, "Seconds per test", self.benchmark_seconds_var, 5, 60)
        self._add_spin_field(benchmark_frame, 4, "Block min (KB)", self.benchmark_block_min_var, 4, 512, increment=4)
        self._add_spin_field(benchmark_frame, 5, "Block max (KB)", self.benchmark_block_max_var, 4, 512, increment=4)
        self._add_spin_field(benchmark_frame, 6, "Workers max", self.benchmark_worker_max_var, 1, 8)
        ttk.Label(
            benchmark_frame,
            text="Benchmark downloads a few sample files for short timed windows, then keeps the fastest worker/block pair.",
            style="Muted.TLabel",
            wraplength=240,
            justify="left",
        ).grid(row=20, column=0, sticky="w", pady=(10, 0))
        self.settings_benchmark_button = ttk.Button(
            benchmark_frame,
            text="Run benchmark",
            style="Action.TButton",
            command=self.run_benchmark_action,
        )
        self.settings_benchmark_button.grid(row=21, column=0, sticky="ew", pady=(14, 0))

        buttons = ttk.Frame(body)
        buttons.grid(row=1, column=0, columnspan=2, sticky="e", pady=(16, 0))
        ttk.Button(buttons, text="Close", command=dialog.destroy).pack(side="right")

    def _add_spin_field(self, parent: ttk.LabelFrame, row: int, label: str, variable: Any, from_: float, to: float, *, increment: float = 1) -> None:
        ttk.Label(parent, text=label).grid(row=row * 2, column=0, sticky="w")
        ttk.Spinbox(parent, textvariable=variable, from_=from_, to=to, increment=increment).grid(row=row * 2 + 1, column=0, sticky="ew", pady=(2, 10))

    def _prepare_for_scan(self) -> None:
        self.last_scan_result = None
        self.last_scan_signature = None
        self.scan_summary = {}
        self.progress_snapshot = {}
        self.global_progress_var.set(0.0)
        self.global_caption_var.set("Global progress | 0/0 files | 0.0B/0.0B | 0.0%")
        self.global_bar.set(0.0, self.global_caption_var.get())
        self.status_var.set("Connecting to Telegram and scanning the channel...")
        self.channel_about_var.set("")
        self._clear_tree(self.pending_tree)
        self.pending_rows.clear()
        self._render_active_downloads([])
        self._append_log("Connect & Scan started.", level="info")
        self._refresh_header_metrics()

    def _prepare_for_download(self, needs_scan: bool) -> None:
        self.progress_snapshot = {}
        if not self.scan_summary:
            self.global_progress_var.set(0.0)
            self.global_caption_var.set("Global progress | 0/0 files | 0.0B/0.0B | 0.0%")
            self.global_bar.set(0.0, self.global_caption_var.get())
        self._render_active_downloads([])
        if needs_scan:
            self.status_var.set("No cached scan available. Download session will scan first.")
        else:
            self.status_var.set("Download session started from cached scan.")
        self._append_log("Download session started.", level="info")

    def _launch_background(
        self,
        *,
        mode: str,
        signature: SettingsSignature,
        runner,
    ) -> None:
        self.operation_mode = mode
        self.operation_settings_signature = signature
        self._set_running_state(True)

        def background() -> None:
            try:
                result = runner()
                self.event_queue.put(("operation_finished", {"mode": mode, "result": result}))
            except Exception as exc:
                self.event_queue.put(
                    (
                        "operation_error",
                        {
                            "mode": mode,
                            "message": str(exc),
                            "traceback": traceback.format_exc(),
                        },
                    )
                )

        self.worker_thread = threading.Thread(target=background, daemon=True)
        self.worker_thread.start()

    def _busy(self) -> bool:
        return self.worker_thread is not None and self.worker_thread.is_alive()

    def _set_running_state(self, running: bool) -> None:
        if running:
            self.save_button.state(["disabled"])
            self.scan_button.state(["disabled"])
            self.start_button.state(["disabled"])
            self.settings_button.state(["disabled"])
            if self.settings_benchmark_button is not None and self.settings_benchmark_button.winfo_exists():
                self.settings_benchmark_button.state(["disabled"])
        else:
            self.save_button.state(["!disabled"])
            self.scan_button.state(["!disabled"])
            self.start_button.state(["!disabled"])
            self.settings_button.state(["!disabled"])
            if self.settings_benchmark_button is not None and self.settings_benchmark_button.winfo_exists():
                self.settings_benchmark_button.state(["!disabled"])

        self.folder_button.state(["!disabled"])
        if running and self.operation_mode == "download":
            if self.stop_requested:
                self.stop_button.state(["disabled"])
            else:
                self.stop_button.state(["!disabled"])
        else:
            self.stop_button.state(["disabled"])

    def _push_event(self, event_type: str, payload: dict[str, Any]) -> None:
        self.event_queue.put((event_type, payload))

    def _set_stop_state(self, requested: bool) -> None:
        self.stop_requested = requested
        self.stop_button.configure(text="Stopping..." if requested else "Stop gracefully")
        if requested:
            self.stop_button.state(["disabled"])

    def _drain_events(self) -> None:
        while True:
            try:
                event_type, payload = self.event_queue.get_nowait()
            except queue.Empty:
                break
            self._handle_event(event_type, payload)
        self.after(150, self._drain_events)

    def _handle_event(self, event_type: str, payload: dict[str, Any]) -> None:
        if event_type == "connected":
            self.channel_title_var.set(payload.get("entity_name") or "Unknown channel")
            channel_id = payload.get("channel_id")
            username = payload.get("username") or "-"
            entity_type = payload.get("entity_type") or "unknown"
            self.channel_meta_var.set(f"ID {channel_id} | @{username.lstrip('@') if username != '-' else '-'} | {entity_type}")
            self.history_counts = payload.get("history_counts", {})
            self.status_var.set("Connected. Scan is running...")
            self._reload_history_from_db()
            self._refresh_header_metrics()
        elif event_type == "channel_details":
            about = payload.get("about") or "No channel description."
            members = payload.get("participants_count")
            suffix = f" | members {members}" if members else ""
            self.channel_about_var.set(f"{about}{suffix}")
        elif event_type == "scan_progress":
            self.status_var.set(f"Scanning channel... {payload.get('scanned_messages', 0)} messages checked")
        elif event_type == "scan_complete":
            self.scan_summary = payload.get("summary", {})
            self._set_pending_jobs(payload.get("pending_jobs", []))
            summary = self.scan_summary
            total_files = int(summary.get("seen", 0))
            completed_files = int(summary.get("skipped", 0))
            total_bytes = int(summary.get("pending_bytes", 0)) + int(summary.get("skipped_bytes", 0))
            completed_bytes = int(summary.get("skipped_bytes", 0))
            percent = (completed_bytes / total_bytes * 100.0) if total_bytes else 0.0
            self.global_progress_var.set(percent)
            self.global_caption_var.set(
                f"Global progress | {completed_files}/{total_files} files | {human_size(completed_bytes)}/{human_size(total_bytes)} | {percent:.1f}%"
            )
            self.global_bar.set(percent, self.global_caption_var.get())
            self.status_var.set(
                f"Scan complete. {summary.get('pending', 0)} pending, {summary.get('skipped', 0)} already present, {summary.get('seen', 0)} matched files."
            )
            self._append_log(
                f"Scan complete: {summary.get('pending', 0)} pending, {summary.get('skipped', 0)} already present, {summary.get('pending_bytes', 0)} bytes queued.",
                level="info",
            )
            self._reload_history_from_db()
            self._refresh_header_metrics()
        elif event_type == "preparing_downloads":
            self.status_var.set(f"Preparing {payload.get('pending_jobs', 0)} queued downloads...")
            self._append_log(f"Preparing {payload.get('pending_jobs', 0)} queued downloads.", level="info")
        elif event_type == "progress":
            self.progress_snapshot = payload
            percent = float(payload.get("global_percent", 0.0))
            self.global_progress_var.set(percent)
            completed = int(payload.get("completed_files", 0))
            total = int(payload.get("total_jobs", 0))
            completed_bytes = int(payload.get("completed_bytes", 0))
            total_bytes = int(payload.get("total_bytes", 0))
            self.global_caption_var.set(
                f"Global progress | {completed}/{total} files | {human_size(completed_bytes)}/{human_size(total_bytes)} | {percent:.1f}%"
            )
            self.global_bar.set(percent, self.global_caption_var.get())
            self._render_active_downloads(payload.get("active_items", []))
            self._refresh_header_metrics()
            if self.stop_requested:
                self.status_var.set("Stopping gracefully. Waiting for active downloads to finish...")
        elif event_type == "job_started":
            job = payload.get("job", {})
            if not self.stop_requested:
                self.status_var.set(f"Downloading {job.get('name', 'file')}...")
            self._append_log(f"Started: {job.get('name', 'file')}", level="info")
        elif event_type == "job_done":
            self._remove_pending_row(int(payload.get("message_id", 0)))
            self._append_history_row(
                message_id=int(payload.get("message_id", 0)),
                name=str(payload.get("name", "")),
                size=int(payload.get("file_size", 0) or 0),
                category=str(payload.get("category", "")),
                status="Done",
                updated="now",
            )
            if self.stop_requested:
                self.status_var.set("Stopping gracefully. Waiting for the last active downloads to finish...")
            else:
                self.status_var.set(f"Finished {payload.get('name', 'file')}")
            self._append_log(f"Finished: {payload.get('name', 'file')}", level="info")
        elif event_type == "job_failed":
            self._remove_pending_row(int(payload.get("message_id", 0)))
            self._append_error_row(
                message_id=int(payload.get("message_id", 0)),
                name=str(payload.get("name", "")),
                error=str(payload.get("error", "Unknown error")),
                updated="now",
            )
            if self.stop_requested:
                self.status_var.set("Stopping gracefully. Waiting for the last active downloads to finish...")
            else:
                self.status_var.set(f"Failed: {payload.get('name', 'file')}")
            self._append_log(f"Failed: {payload.get('name', 'file')} -> {payload.get('error', 'Unknown error')}", level="error")
        elif event_type == "summary":
            self.scan_summary = payload
            self._refresh_header_metrics()
        elif event_type == "benchmark_plan":
            self.status_var.set("Benchmark running short transfer tests...")
            self._append_log(
                f"Benchmark plan: {len(payload.get('combinations', []))} profiles, {payload.get('sample_size', 0)} sample files, {payload.get('duration_seconds', 0)} seconds each.",
                level="info",
            )
        elif event_type == "benchmark_jobs_selected":
            self._append_log(f"Benchmark selected {len(payload.get('jobs', []))} sample files.", level="info")
        elif event_type == "benchmark_case_started":
            self.status_var.set(
                f"Benchmark {payload.get('index', 0)}/{payload.get('total', 0)} | workers={payload.get('workers')} | block={payload.get('part_size_kb')}KB"
            )
        elif event_type == "benchmark_case_progress":
            index = float(payload.get("index", 1))
            total = float(max(payload.get("total", 1), 1))
            percent = float(payload.get("percent", 0.0))
            overall = ((index - 1.0) + (percent / 100.0)) / total * 100.0
            self.global_progress_var.set(overall)
            self.global_caption_var.set(f"Benchmark | workers={payload.get('workers')} | block={payload.get('part_size_kb')}KB | {percent:.0f}%")
            self.global_bar.set(overall, self.global_caption_var.get())
            self.header_metrics_var.set(
                f"Transferred {human_size(payload.get('transferred_bytes', 0))} | Speed {human_size(payload.get('avg_speed_bps', 0))}/s | Active {payload.get('active_jobs', 0)}"
            )
        elif event_type == "benchmark_case_done":
            self._append_log(
                f"Benchmark result: workers={payload.get('workers')} block={payload.get('part_size_kb')}KB -> {human_size(payload.get('avg_speed_bps', 0))}/s",
                level="info",
            )
        elif event_type == "benchmark_complete":
            best = payload.get("best_result")
            if best:
                self.workers_var.set(int(best["workers"]))
                self.part_size_var.set(int(best["part_size_kb"]))
                try:
                    save_settings(self._build_settings(), self.config_path)
                except Exception:
                    pass
                self.status_var.set(
                    f"Benchmark complete. Best profile applied: workers={best['workers']} block={best['part_size_kb']}KB."
                )
                self._append_log(
                    f"Best benchmark profile applied: workers={best['workers']} block={best['part_size_kb']}KB.",
                    level="info",
                )
            else:
                self.status_var.set("Benchmark complete, but no result was produced.")
        elif event_type == "log":
            self._append_log(payload.get("message", ""), level=payload.get("level", "info"))
        elif event_type == "operation_finished":
            mode = payload.get("mode")
            result = payload.get("result")
            if mode == "scan" and isinstance(result, ScanResult):
                self.last_scan_result = result
                self.last_scan_signature = self.operation_settings_signature
                self.status_var.set("Connect & Scan finished. You can start the download now.")
            elif mode == "download":
                self.status_var.set("Download session finished.")
                self._reload_history_from_db()
                self._render_active_downloads([])
            elif mode == "benchmark":
                self._refresh_header_metrics()
            self.worker_thread = None
            self.operation_mode = None
            self.operation_settings_signature = None
            self.download_controller = None
            self._set_stop_state(False)
            self._set_running_state(False)
        elif event_type == "operation_error":
            self.worker_thread = None
            self.operation_mode = None
            self.operation_settings_signature = None
            self.download_controller = None
            self._set_stop_state(False)
            self._set_running_state(False)
            self._render_active_downloads([])
            self.status_var.set(f"Operation failed: {payload.get('message', 'unknown error')}")
            self._append_log(payload.get("traceback", payload.get("message", "Unknown error")), level="error")
            messagebox.showerror("Operation failed", str(payload.get("message", "Unknown error")))

    def _refresh_header_metrics(self) -> None:
        completed_files = int(
            self.progress_snapshot.get(
                "completed_files",
                self.scan_summary.get("skipped", 0),
            )
        )
        total_files = int(
            self.progress_snapshot.get(
                "total_jobs",
                self.scan_summary.get("seen", completed_files),
            )
        )
        pending_files = int(self.progress_snapshot.get("remaining_files", self.scan_summary.get("pending", 0)))
        remaining_bytes = int(self.progress_snapshot.get("remaining_bytes", self.scan_summary.get("pending_bytes", 0)))
        speed_bps = float(self.progress_snapshot.get("speed_bps", 0.0))
        failed_total = int(self.progress_snapshot.get("failed_files", self.history_counts.get("failed", 0)))
        self.header_metrics_var.set(
            f"Completed {completed_files}/{total_files} | Pending {pending_files} | Remaining {human_size(remaining_bytes)} | Speed {human_size(speed_bps)}/s | Failed {failed_total}"
        )

    def _set_pending_jobs(self, jobs: list[dict[str, Any]]) -> None:
        self.pending_dataset = [
            {
                "message_id": int(job.get("message_id", 0)),
                "name": job.get("name", ""),
                "size": int(job.get("file_size", 0) or 0),
                "category": job.get("category", ""),
            }
            for job in jobs
        ]
        self._rebuild_pending_tree()

    def _remove_pending_row(self, message_id: int) -> None:
        self.pending_dataset = [row for row in self.pending_dataset if int(row["message_id"]) != message_id]
        self._rebuild_pending_tree()

    def _append_history_row(
        self,
        *,
        message_id: int,
        name: str,
        size: int,
        category: str,
        status: str,
        updated: str,
    ) -> None:
        self.completed_dataset = [row for row in self.completed_dataset if int(row["message_id"]) != message_id]
        self.completed_dataset.insert(
            0,
            {
                "message_id": message_id,
                "name": name,
                "size": size,
                "category": category,
                "status": status,
                "updated": updated,
            },
        )
        self.error_dataset = [row for row in self.error_dataset if int(row["message_id"]) != message_id]
        self._rebuild_completed_tree()
        self._rebuild_error_tree()

    def _append_error_row(self, *, message_id: int, name: str, error: str, updated: str) -> None:
        self.error_dataset = [row for row in self.error_dataset if int(row["message_id"]) != message_id]
        self.error_dataset.insert(
            0,
            {
                "message_id": message_id,
                "name": name,
                "error": error,
                "updated": updated,
            },
        )
        self._rebuild_error_tree()

    def _reload_history_from_db(self) -> None:
        db_path = Path(self.hidden_state_db_path).expanduser()
        if not db_path.is_absolute():
            db_path = (Path.cwd() / db_path).resolve()
        if not db_path.exists():
            self.history_counts = {}
            self.completed_dataset = []
            self.error_dataset = []
            self._rebuild_completed_tree()
            self._rebuild_error_tree()
            self._refresh_header_metrics()
            return

        store = StateStore(db_path)
        try:
            history = store.list_completed(limit=5000)
            failed = store.list_failed(limit=2000)
            self.history_counts = store.counts()
        finally:
            store.close()

        self.completed_dataset = []
        self.error_dataset = []

        completed_ids: set[int] = set()
        for row in history:
            message_id = int(row.get("message_id", 0))
            completed_ids.add(message_id)
            path = Path(str(row.get("file_path") or ""))
            name = path.name or f"message_{message_id}"
            self.completed_dataset.append(
                {
                    "message_id": message_id,
                    "name": name,
                    "size": int(row.get("file_size", 0) or 0),
                    "category": row.get("category", ""),
                    "status": "Done" if row.get("status") == "done" else "Already there",
                    "updated": row.get("updated_at", ""),
                }
            )

        for row in failed:
            message_id = int(row.get("message_id", 0))
            if message_id in completed_ids:
                continue
            path = Path(str(row.get("file_path") or ""))
            name = path.name or f"message_{message_id}"
            self.error_dataset.append(
                {
                    "message_id": message_id,
                    "name": name,
                    "error": row.get("error", ""),
                    "updated": row.get("updated_at", ""),
                }
            )

        self._rebuild_completed_tree()
        self._rebuild_error_tree()

        self._refresh_header_metrics()

    def _render_active_downloads(self, active_items: list[dict[str, Any]]) -> None:
        if not active_items:
            for widgets in self.active_widgets.values():
                widgets["frame"].destroy()
            self.active_widgets.clear()
            self.active_placeholder.grid(row=0, column=0, sticky="w")
            self.active_caption_var.set("")
            self._sync_active_scrollregion()
            return

        self.active_placeholder.grid_remove()
        self.active_caption_var.set(
            f"{len(active_items)} active download(s) | top speed {human_size(max(item.get('speed_bps', 0) for item in active_items))}/s"
        )

        current_ids = {int(item["message_id"]) for item in active_items}
        for message_id in list(self.active_widgets):
            if message_id not in current_ids:
                self.active_widgets[message_id]["frame"].destroy()
                del self.active_widgets[message_id]

        for index, item in enumerate(active_items):
            message_id = int(item["message_id"])
            widgets = self.active_widgets.get(message_id)
            if widgets is None:
                frame = ttk.Frame(self.active_holder, style="Surface.TFrame", padding=(0, 0, 0, 8))
                frame.grid(row=index, column=0, sticky="ew", pady=(0, 0))
                frame.columnconfigure(0, weight=1)

                title_var = tk.StringVar()
                ttk.Label(frame, textvariable=title_var, style="ActiveTitle.TLabel").grid(row=0, column=0, sticky="w")
                bar = InlineProgressBar(frame, height=22)
                bar.grid(row=1, column=0, sticky="ew", pady=(4, 0))

                widgets = {"frame": frame, "title_var": title_var, "bar": bar}
                self.active_widgets[message_id] = widgets

            widgets["frame"].grid_configure(row=index)
            widgets["title_var"].set(str(item.get("label", "")))
            widgets["bar"].set(
                float(item.get("percent", 0.0)),
                f"{item.get('percent', 0.0):.1f}% | {human_size(item.get('current', 0))}/{human_size(item.get('total', 0))} | {human_size(item.get('speed_bps', 0))}/s"
            )
        self._sync_active_scrollregion()

    def _append_log(self, text: str, *, level: str = "info") -> None:
        message = text.strip()
        if not message:
            return
        prefix = {
            "info": "[INFO]",
            "warning": "[WARN]",
            "error": "[ERROR]",
        }.get(level, "[INFO]")
        self.log_text.configure(state="normal")
        if self.log_text.get("1.0", "end-1c").strip() == "No log entries yet.":
            self.log_text.delete("1.0", "end")
        self.log_text.insert("end", f"{prefix} {message}\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _set_log_placeholder(self) -> None:
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.insert("1.0", "No log entries yet.")
        self.log_text.configure(state="disabled")

    def _clear_tree(self, tree: ttk.Treeview) -> None:
        for item in tree.get_children():
            tree.delete(item)

    def _rebuild_pending_tree(self) -> None:
        self._clear_tree(self.pending_tree)
        self.pending_rows.clear()
        query = self.search_var.get().strip().lower()
        for row in self.pending_dataset:
            if query and not self._row_matches_query(row, query):
                continue
            message_id = int(row["message_id"])
            self.pending_rows[message_id] = self.pending_tree.insert(
                "",
                "end",
                values=(row["name"], human_size(row["size"]), row["category"]),
            )

    def _rebuild_completed_tree(self) -> None:
        self._clear_tree(self.completed_tree)
        self.completed_rows.clear()
        query = self.search_var.get().strip().lower()
        for row in self.completed_dataset:
            if query and not self._row_matches_query(row, query):
                continue
            message_id = int(row["message_id"])
            self.completed_rows[message_id] = self.completed_tree.insert(
                "",
                "end",
                values=(row["name"], human_size(row["size"]), row["category"], row["status"], row["updated"]),
            )

    def _rebuild_error_tree(self) -> None:
        self._clear_tree(self.error_tree)
        self.error_rows.clear()
        query = self.search_var.get().strip().lower()
        for row in self.error_dataset:
            if query and not self._row_matches_query(row, query):
                continue
            message_id = int(row["message_id"])
            self.error_rows[message_id] = self.error_tree.insert(
                "",
                "end",
                values=(row["name"], row["error"], row["updated"]),
            )

    def _row_matches_query(self, row: dict[str, Any], query: str) -> bool:
        haystack = " ".join(str(value) for value in row.values()).lower()
        return query in haystack

    def _active_tab_name(self) -> str:
        if self.notebook is None:
            return "Pending"
        current = self.notebook.select()
        return self.notebook.tab(current, "text")

    def _apply_search_filter(self) -> None:
        active_tab = self._active_tab_name()
        if active_tab == "Pending":
            self._rebuild_pending_tree()
        elif active_tab == "Completed":
            self._rebuild_completed_tree()
        elif active_tab == "Errors":
            self._rebuild_error_tree()

    def _clear_search(self) -> None:
        self.search_var.set("")
        self._apply_search_filter()

    def _sync_active_scrollregion(self, _event=None) -> None:
        if self.active_canvas is None:
            return
        self.active_canvas.configure(scrollregion=self.active_canvas.bbox("all"))

    def _resize_active_window(self, event) -> None:
        if self.active_canvas is None or self.active_window_id is None:
            return
        self.active_canvas.itemconfigure(self.active_window_id, width=event.width)


def main() -> None:
    app = TelegramDownloaderGUI()
    app.mainloop()
