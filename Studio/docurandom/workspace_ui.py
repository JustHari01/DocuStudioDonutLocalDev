from __future__ import annotations

from datetime import datetime
import ctypes
import json
import os
from pathlib import Path
import re
import sys
import threading
import webbrowser
import weakref
from tkinter import BOTH, END, HORIZONTAL, LEFT, RIGHT, VERTICAL, StringVar, Tk
from tkinter import filedialog, messagebox, simpledialog, ttk
import tkinter as tk
from typing import Callable

from send2trash import send2trash

from .diagnostics import record_exception
from .browser_extension_bridge import bundled_browser_extension_dir
from .donut_bridge_installer import (
    DonutBridgeInstallResult,
    install_bridge_for_all_profiles,
)
from .features import DONUT_YOUTUBE_ENABLED
from .flow_agent import FLOW_EXTENSION_VERSION, FlowAgentStatus
from .runtime_profile import (
    GLOBAL_CHATGPT_LIMIT_MAX,
    GLOBAL_RENDER_LIMIT_MAX,
    clamp_global_chatgpt_limit,
    clamp_global_render_limit,
)
from .donut_mcp import (
    DonutMcpClient,
    DonutMcpEndpoint,
    load_donut_mcp_endpoint,
    save_donut_mcp_endpoint,
)
from .donut_manager import DonutManagedError, DonutManager
from .private_github import (
    load_private_github_token,
    private_github_token_url,
    save_private_github_token,
    test_private_github_access,
)
from .single_instance import WINDOW_TITLE
from .smm_api import (
    SmmApiClient,
    SmmApiError,
    SmmBalance,
    SmmService,
    clear_smm_api_key,
    load_smm_api_key,
    save_smm_api_key,
)
from .storage import app_data_dir, configured_home_dir
from .telegram_bot import TelegramBotError, TelegramBotService
from .ui_scale import configure_responsive_window
from . import __version__
from .pipeline_checkpoint import (
    WorkspacePipelineOutcome,
    inspect_workspace_pipeline,
    read_pipeline_checkpoint,
)
from .session_archive import (
    reconcile_deleted_workspace_session,
    reset_workspace_session_state,
)
from .updater import (
    UpdateCancelled,
    cached_portable_update_exists,
    consume_latest_install_result,
    download_portable_update,
    find_reusable_staged_update,
    get_latest_release,
    stage_portable_update,
    update_is_newer,
    verify_release_manifest,
)
from .workspace import (
    Workspace,
    WorkspaceFolder,
    WorkspaceRelocationResult,
    WorkspaceStore,
)


# These are the same Donut Mono tokens used by the editor. The actual ttk
# styles and icon bitmaps are installed through DocuRandomApp below, so the
# launcher cannot silently drift into a second visual system again.
BACKGROUND = "#070707"
PANEL = "#0e0e0e"
PANEL_ALT = "#161616"
TEXT = "#ffffff"
MUTED = "#a0a0a0"
SUCCESS = "#61c554"
WARNING = "#f4be4f"
BORDER = "#242424"
ALL_FOLDER_ID = "__all_workspaces__"
UNGROUPED_TREE_ID = "__ungrouped_workspaces__"
_DISMISSED_UPDATE_VERSION = ""
WORKSPACE_FILTERS = (
    "Tất cả trạng thái",
    "Đang làm",
    "Đã render xong",
    "Chưa hoàn tất",
    "Có lỗi",
    "Chưa bắt đầu",
)
WORKSPACE_SORTS = (
    "Cần xử lý trước",
    "Mới cập nhật trước",
    "Cũ cập nhật trước",
    "Tên A → Z",
    "Tên Z → A",
    "Video mới nhất",
)


def _client_animations_enabled() -> bool:
    """Follow the Windows reduced-motion setting when it is available."""

    if sys.platform != "win32":
        return True
    try:
        enabled = ctypes.c_int(1)
        # SPI_GETCLIENTAREAANIMATION
        ok = ctypes.windll.user32.SystemParametersInfoW(
            0x1042,
            0,
            ctypes.byref(enabled),
            0,
        )
        return bool(ok and enabled.value)
    except Exception:
        return True


def _clean_update_notes(value: str, *, limit: int = 300) -> str:
    """Keep release notes readable without exposing repository URLs in the UI."""
    parts: list[str] = []
    for raw_line in str(value or "").splitlines():
        if re.search(r"\bfull\s+changelog\b", raw_line, flags=re.IGNORECASE):
            continue
        line = re.sub(r"\[([^\]]+)\]\(https?://[^)]+\)", r"\1", raw_line)
        line = re.sub(r"https?://\S+", "", line)
        line = re.sub(r"[*_`#>]", "", line)
        line = " ".join(line.split())
        if line:
            parts.append(line)
    return " ".join(parts)[:limit]


class WorkspaceLauncher:
    """Single-window project hub styled by the editor's shared Donut theme."""

    def __init__(
        self,
        root: Tk,
        store: WorkspaceStore,
        on_open: Callable[[Workspace], None],
        on_apply_update: Callable[[Path, str], None] | None = None,
        activity_provider: Callable[[str], dict[str, object] | None] | None = None,
        checkpoint_reset_handler: (
            Callable[[Workspace], tuple[bool, str]] | None
        ) = None,
        telegram_service: TelegramBotService | None = None,
        render_queue_provider: Callable[[], dict[str, int]] | None = None,
        render_limit_handler: Callable[[int], int] | None = None,
        chatgpt_queue_provider: Callable[[], dict[str, object]] | None = None,
        chatgpt_limit_handler: Callable[[int], int] | None = None,
        chatgpt_serial_handler: Callable[[bool], bool] | None = None,
        manual_upload_handler: Callable[[Workspace], tuple[bool, str]] | None = None,
        flow_status_provider: Callable[[], FlowAgentStatus] | None = None,
        flow_start_handler: Callable[[], FlowAgentStatus] | None = None,
        smm_account_broadcast: (
            Callable[[SmmBalance, tuple[SmmService, ...]], None] | None
        ) = None,
        smm_key_changed_handler: Callable[[bool], None] | None = None,
        bulk_run_handler: (
            Callable[[tuple[Workspace, ...]], tuple[int, str]] | None
        ) = None,
        bulk_continue_handler: (
            Callable[[tuple[Workspace, ...]], tuple[int, str]] | None
        ) = None,
        bulk_skip_handler: (
            Callable[[tuple[Workspace, ...]], tuple[int, str]] | None
        ) = None,
        bulk_stop_handler: (
            Callable[[tuple[Workspace, ...]], tuple[int, str]] | None
        ) = None,
        workspace_delete_handler: Callable[[Workspace], None] | None = None,
        google_service=None,
    ) -> None:
        self.root = root
        self.store = store
        self.on_open = on_open
        self.on_apply_update = on_apply_update
        self.activity_provider = activity_provider
        self.checkpoint_reset_handler = checkpoint_reset_handler
        self.telegram_service = telegram_service
        self.render_queue_provider = render_queue_provider
        self.render_limit_handler = render_limit_handler
        self.chatgpt_queue_provider = chatgpt_queue_provider
        self.chatgpt_limit_handler = chatgpt_limit_handler
        self.chatgpt_serial_handler = chatgpt_serial_handler
        self.manual_upload_handler = manual_upload_handler
        self.flow_status_provider = flow_status_provider
        self.flow_start_handler = flow_start_handler
        self.smm_account_broadcast = smm_account_broadcast
        self.smm_key_changed_handler = smm_key_changed_handler
        self.bulk_run_handler = bulk_run_handler
        self.bulk_continue_handler = bulk_continue_handler
        self.bulk_skip_handler = bulk_skip_handler
        self.bulk_stop_handler = bulk_stop_handler
        self.workspace_delete_handler = workspace_delete_handler
        self.google_service = google_service
        self.root.title(WINDOW_TITLE)
        metrics = configure_responsive_window(self.root)
        self.ui_scale = metrics.font_scale
        self.root.configure(bg=BACKGROUND)
        resource_root = Path(
            getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1])
        )
        icon_path = resource_root / "assets" / "docurandom.ico"
        if icon_path.is_file():
            try:
                self.root.iconbitmap(default=str(icon_path))
            except Exception:
                pass
        self.name_var = StringVar(master=root)
        self.category_var = StringVar(master=root, value="Video YouTube")
        self.parent_var = StringVar(master=root, value=str(store.default_root))
        self.storage_root = configured_home_dir() or app_data_dir()
        self.storage_root_var = StringVar(master=root, value=str(self.storage_root))
        self.workspace_storage_var = StringVar(
            master=root,
            value=str(store.default_root),
        )
        self.workspace_storage_status_var = StringVar(
            master=root,
            value="Workspace mới và workspace hiện có dùng kho này.",
        )
        self.workspace_storage_busy = False
        self.workspace_count_var = StringVar(master=root, value="Đang tải workspace…")
        self.workspace_search_var = StringVar(master=root)
        self.workspace_filter_var = StringVar(master=root, value=WORKSPACE_FILTERS[0])
        self.workspace_sort_var = StringVar(master=root, value=WORKSPACE_SORTS[0])
        self.folder_name_var = StringVar(master=root)
        self.folder_scope_id = ALL_FOLDER_ID
        self.current_folder_var = StringVar(master=root, value="Tất cả workspace")
        self.current_folder_detail_var = StringVar(
            master=root,
            value="Toàn bộ workspace trong mọi thư mục",
        )
        self.folder_move_var = StringVar(master=root, value="Chưa phân loại")
        self.folder_destination_by_label: dict[str, str] = {"Chưa phân loại": ""}
        self.folder_tree_items: dict[str, str] = {}
        self.folder_by_id: dict[str, WorkspaceFolder] = {}
        self.refreshing_folder_tree = False
        self.selection_hint_var = StringVar(
            master=root,
            value="Mỗi workspace giữ riêng media, prompt, voice, video và lịch sử chống lặp.",
        )
        self.workspace_cards: dict[str, ttk.Frame] = {}
        self.workspace_card_controls: dict[str, dict[str, object]] = {}
        self.workspace_select_vars: dict[str, tk.BooleanVar] = {}
        self.selected_workspace_ids: set[str] = set()
        self.folder_drop_targets: dict[str, tk.Misc] = {}
        self.drag_workspace_id = ""
        self.drag_origin = (0, 0)
        self.drag_visual_started = False
        self.drag_ghost: tk.Toplevel | None = None
        self.drag_drop_folder_id: str | None = None
        self.drag_animation_after_ids: list[str] = []
        self.animations_enabled = _client_animations_enabled()
        self.workspace_by_id: dict[str, Workspace] = {}
        self.workspace_outcomes: dict[str, WorkspacePipelineOutcome] = {}
        self.workspace_activity_signature: tuple[tuple[str, str, str], ...] = ()
        self.workspace_activity_ui_cache: dict[str, tuple[object, ...]] = {}
        self.workspace_folder_count = 0
        self.live_output_rows: dict[str, dict[str, object]] = {}
        self.live_output_signature: tuple[str, ...] = ("__initial__",)
        self.live_output_activity_cache: dict[str, tuple[object, ...]] = {}
        self.workspace_activity_after_id: str | None = None
        self.live_summary_var = StringVar(master=root, value="Chưa có tác vụ")
        self.workspace_queue_summary_var = StringVar(
            master=root,
            value="Đang làm 0 · Hàng chờ 0 · Cần xử lý 0",
        )
        self.workspace_queue_selection_var = StringVar(
            master=root,
            value="Chưa chọn workspace nào",
        )
        self.workspace_queue_tree: ttk.Treeview | None = None
        self.workspace_queue_items: dict[str, str] = {}
        self.workspace_queue_signature: tuple[tuple[object, ...], ...] = ()
        initial_render_queue = self._render_queue_snapshot()
        self.global_render_limit_var = StringVar(
            master=root,
            value=str(initial_render_queue["limit"]),
        )
        self.global_render_queue_var = StringVar(
            master=root,
            value=f"Sẵn sàng · tối đa {initial_render_queue['limit']} video cùng lúc",
        )
        self.global_render_error = ""
        initial_chatgpt_queue = self._chatgpt_queue_snapshot()
        self.chatgpt_serial_var = tk.BooleanVar(
            master=root,
            value=bool(initial_chatgpt_queue["serial"]),
        )
        configured_chatgpt_limit = int(
            initial_chatgpt_queue.get(
                "configured_limit",
                initial_chatgpt_queue.get("limit", 1),
            )
        )
        self.chatgpt_parallel_var = StringVar(
            master=root,
            value=str(clamp_global_chatgpt_limit(configured_chatgpt_limit)),
        )
        self.chatgpt_queue_var = StringVar(
            master=root,
            value=(
                f"Sẵn sàng · tối đa {self.chatgpt_parallel_var.get()} phiên ChatGPT"
            ),
        )
        self.chatgpt_queue_error = ""
        self.global_flow_status_var = StringVar(
            master=root,
            value="○  FLOW · CHƯA KIỂM TRA",
        )
        self.global_flow_detail_var = StringVar(
            master=root,
            value=(
                "Flow backend, Edge và extension được dùng chung bởi mọi workspace."
            ),
        )
        self.global_flow_version_var = StringVar(master=root, value="")
        self.global_flow_busy = False
        self.global_flow_status: FlowAgentStatus | None = None
        self.global_flow_refresh_after_id: str | None = None
        self.global_smm_api_key_var = StringVar(master=root, value="")
        self.global_smm_status_var = StringVar(
            master=root,
            value=(
                "✓ API key 1MM đã lưu mã hóa trên máy này."
                if load_smm_api_key() is not None
                else "Chưa lưu API key 1MM trên máy này."
            ),
        )
        self.global_smm_balance_var = StringVar(
            master=root,
            value="Số dư: chưa kiểm tra",
        )
        self.global_smm_services_var = StringVar(
            master=root,
            value="Chưa tải danh sách gói YouTube Like.",
        )
        self.global_smm_busy = False
        self.workspace_refresh_after_id: str | None = None
        self.workspace_refresh_worker_active = False
        self.workspace_refresh_pending = False
        self.active_rename_id = ""
        self.cards_layout_after_id: str | None = None
        self.cards_scroll_after_id: str | None = None
        self.cards_pending_width = 0
        self.cards_applied_width = 0
        self.reset_cards_scroll = False
        self.update_status_var = StringVar(master=root, value="●  ĐANG KIỂM TRA CẬP NHẬT")
        self.update_cancel_event = threading.Event()
        self.update_busy = False
        self.update_staged_path: Path | None = None
        self.update_staged_version = ""
        self.update_release_notes = ""
        self.update_release_size = 0
        self.update_toast: tk.Frame | None = None
        self.update_toast_title_var = StringVar(master=root)
        self.update_toast_version_var = StringVar(master=root)
        self.update_toast_detail_var = StringVar(master=root)
        self.update_toast_progress_var = tk.DoubleVar(master=root, value=0.0)
        self.donut_manager = DonutManager() if DONUT_YOUTUBE_ENABLED else None
        saved_donut_endpoint = load_donut_mcp_endpoint() if DONUT_YOUTUBE_ENABLED else None
        if DONUT_YOUTUBE_ENABLED and saved_donut_endpoint is None:
            try:
                saved_donut_endpoint = DonutManager.discover_endpoint()
            except DonutManagedError:
                pass
        self.donut_endpoint_var = StringVar(
            master=root,
            value=saved_donut_endpoint.value if saved_donut_endpoint is not None else "",
        )
        self.donut_status_var = StringVar(
            master=root,
            value=(
                "●  TEST DONUT MCP"
                if saved_donut_endpoint is None
                else "●  KIỂM TRA DONUT MCP"
            ),
        )
        self.donut_manage_action_var = StringVar(
            master=root,
            value=(
                self.donut_manager.manage_action_text()
                if self.donut_manager is not None
                else "CÀI ĐẶT DONUT LOCAL"
            ),
        )
        self.donut_test_detail_var = StringVar(
            master=root,
            value=(
                "Chưa kết nối · tool sẽ tự cài/mở Donut và ghép MCP."
                if saved_donut_endpoint is None
                else "Đã tự nhận MCP và lưu khóa bằng mã hóa Windows · bấm Kiểm tra."
            ),
        )
        self.donut_progress_var = tk.DoubleVar(master=root, value=0.0)
        self.donut_github_token_var = StringVar(master=root, value="")
        self.donut_github_status_var = StringVar(
            master=root,
            value=(
                "✓ Đã lưu đăng nhập GitHub private trên máy này."
                if load_private_github_token()
                else "Chưa đăng nhập GitHub private để tải Donut Local."
            ),
        )
        self.donut_cancel_event = threading.Event()
        self.donut_test_busy = False
        self.donut_github_busy = False
        self.donut_bridge_install_busy = False
        self.donut_update_check_busy = False
        # The Donut update probe runs off the Tk thread.  Keep an explicit
        # lifetime boundary so a late network callback cannot retain Tk
        # variables until their ``__del__`` methods run after root.destroy().
        self._launcher_closed = False
        self.donut_update_check_cancel_event = threading.Event()
        self.donut_update_check_thread: threading.Thread | None = None
        self.donut_update_check_after_id: str | None = None
        self.donut_settings_dialog: tk.Toplevel | None = None
        telegram_settings = (
            telegram_service.settings()
            if telegram_service is not None
            else None
        )
        self.telegram_status_var = StringVar(master=root, value="○  TELEGRAM")
        self.telegram_detail_var = StringVar(
            master=root,
            value="Telegram chưa bật.",
        )
        self.telegram_token_var = StringVar(master=root, value="")
        self.telegram_enabled_var = tk.BooleanVar(
            master=root,
            value=bool(telegram_settings.enabled) if telegram_settings is not None else False,
        )
        self.telegram_notify_prompts_var = tk.BooleanVar(
            master=root,
            value=False,
        )
        self.telegram_notify_errors_var = tk.BooleanVar(
            master=root,
            value=bool(telegram_settings.notify_errors) if telegram_settings is not None else True,
        )
        self.telegram_notify_status_var = tk.BooleanVar(
            master=root,
            value=bool(telegram_settings.notify_status) if telegram_settings is not None else True,
        )
        self.telegram_pairing_var = StringVar(
            master=root,
            value="Chưa tạo mã ghép cặp.",
        )
        self.telegram_settings_dialog: tk.Toplevel | None = None
        self.telegram_config_busy = False
        self._configure_shared_theme()
        self._build()
        self.refresh()
        self._schedule_global_flow_refresh(1200)
        self.root.after_idle(self.folder_tree.focus_set)
        self.root.after(650, self._show_previous_update_result)
        self.root.after(900, self._start_update_check)
        self.donut_update_check_after_id = self.root.after(
            2200,
            self._start_donut_update_check,
        )
        self.root.after(650, self._refresh_workspace_activity)
        self.root.after(700, self._refresh_telegram_status)

    def _show_previous_update_result(self) -> None:
        result = consume_latest_install_result()
        if result is None:
            return
        if result.status == "success" and result.target_version == __version__:
            self._set_update_status(f"✓  ĐÃ CẬP NHẬT {result.target_version}")
            messagebox.showinfo(
                "Cập nhật thành công",
                f"DocuRandom Studio {result.target_version} đã được cài đè và "
                "khởi động lại thành công.",
                parent=self.root,
            )
            return
        if result.status == "failed":
            self._set_update_status("!  CẬP NHẬT CHƯA HOÀN TẤT")
            detail = result.detail.strip() or "Windows chưa cho phép thay thế runtime."
            messagebox.showwarning(
                "Cập nhật chưa hoàn tất",
                "Tool đã giữ nguyên bản đang dùng và giữ lại gói cập nhật.\n\n"
                f"Chi tiết: {detail}\n\n"
                "Bấm Cập nhật lại để thử tiếp; tool sẽ dùng gói đã có, không tải lại từ đầu.",
                parent=self.root,
            )

    def _configure_shared_theme(self) -> None:
        # Import lazily: ui.run imports this launcher only after DocuRandomApp
        # has been defined. Direct tests/imports remain safe as ui has no eager
        # dependency back to workspace_ui.
        from .ui import DocuRandomApp

        theme_host = DocuRandomApp.__new__(DocuRandomApp)
        theme_host.root = self.root
        theme_host.icons = theme_host._build_icons()
        theme_host._configure_style()
        self.icons = theme_host.icons

        style = ttk.Style(self.root)
        style.configure(
            "WorkspaceHero.TLabel",
            background=PANEL,
            foreground=TEXT,
            font=("Segoe UI Semibold", 18),
        )
        style.configure(
            "WorkspaceCardTitle.TLabel",
            background=PANEL_ALT,
            foreground=TEXT,
            font=("Segoe UI Semibold", 11),
        )
        style.configure(
            "WorkspaceCardMeta.TLabel",
            background=PANEL_ALT,
            foreground=MUTED,
            font=("Segoe UI", 9),
        )
        style.configure(
            "WorkspaceCardReady.TLabel",
            background=PANEL_ALT,
            foreground=SUCCESS,
            font=("Segoe UI Semibold", 8),
        )
        style.configure(
            "WorkspaceCardRunning.TLabel",
            background=PANEL_ALT,
            foreground="#5db7ff",
            font=("Segoe UI Semibold", 8),
        )
        style.configure(
            "WorkspaceCardWarning.TLabel",
            background=PANEL_ALT,
            foreground=WARNING,
            font=("Segoe UI Semibold", 8),
        )
        style.configure(
            "WorkspaceCardError.TLabel",
            background=PANEL_ALT,
            foreground="#ff6b62",
            font=("Segoe UI Semibold", 8),
        )
        style.configure(
            "WorkspaceField.TLabel",
            background=PANEL,
            foreground=MUTED,
            font=("Segoe UI Semibold", 8),
        )
        style.configure(
            "WorkspaceEmptyTitle.TLabel",
            background=PANEL_ALT,
            foreground=TEXT,
            font=("Segoe UI Semibold", 12),
        )
        style.configure(
            "WorkspaceFolderTitle.TLabel",
            background=PANEL_ALT,
            foreground=TEXT,
            font=("Segoe UI Semibold", 10),
        )
        style.configure(
            "WorkspaceSelect.TCheckbutton",
            background=PANEL_ALT,
            foreground=TEXT,
        )
        style.map(
            "WorkspaceSelect.TCheckbutton",
            background=[("active", PANEL_ALT)],
        )
        style.configure(
            "Workspace.Treeview",
            background=PANEL,
            fieldbackground=PANEL,
            foreground=TEXT,
            borderwidth=0,
            relief="flat",
            rowheight=max(27, round(31 * self.ui_scale)),
            font=("Segoe UI", 9),
        )
        style.map(
            "Workspace.Treeview",
            background=[("selected", "#24432a")],
            foreground=[("selected", TEXT)],
        )
        style.configure(
            "WorkspaceLiveTitle.TLabel",
            background=PANEL_ALT,
            foreground=TEXT,
            font=("Segoe UI Semibold", 9),
        )
        style.configure(
            "WorkspaceScope.TLabel",
            background=PANEL,
            foreground=TEXT,
            font=("Segoe UI Semibold", 12),
        )
        style.configure(
            "WorkspaceQueue.Treeview",
            background=PANEL_ALT,
            fieldbackground=PANEL_ALT,
            foreground=TEXT,
            borderwidth=0,
            relief="flat",
            rowheight=max(28, round(31 * self.ui_scale)),
            font=("Segoe UI", 8),
        )
        style.map(
            "WorkspaceQueue.Treeview",
            background=[("selected", "#24432a")],
            foreground=[("selected", TEXT)],
        )

    def _build(self) -> None:
        self.root_frame = ttk.Frame(
            self.root,
            style="Root.TFrame",
            padding=(14, 10, 14, 14),
        )
        self.root_frame.pack(fill=BOTH, expand=True)

        self._build_header(self.root_frame)
        self._build_global_controls(self.root_frame)

        body = ttk.Frame(self.root_frame, style="Root.TFrame")
        body.pack(fill=BOTH, expand=True)
        # Let the workspace library and live queue grow with the window.
        # The old fixed side columns left a very narrow queue even on a wide
        # monitor and could clip the three-pane layout near the minimum size.
        body.columnconfigure(0, minsize=max(195, round(210 * self.ui_scale)), weight=1)
        body.columnconfigure(1, minsize=max(340, round(390 * self.ui_scale)), weight=5)
        body.columnconfigure(2, minsize=max(260, round(300 * self.ui_scale)), weight=2)
        body.rowconfigure(0, weight=1)

        library_panel = ttk.Frame(
            body,
            style="Panel.TFrame",
            padding=(10, 10),
        )
        library_panel.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
        library_panel.rowconfigure(0, weight=1)
        library_panel.columnconfigure(0, weight=1)
        self.library_notebook = ttk.Notebook(
            library_panel,
            width=max(175, round(200 * self.ui_scale)),
        )
        self.library_notebook.grid(row=0, column=0, sticky="nsew")
        folder_tab = ttk.Frame(
            self.library_notebook,
            style="Panel.TFrame",
            padding=(8, 10),
        )
        create_tab = ttk.Frame(
            self.library_notebook,
            style="Panel.TFrame",
            padding=(10, 10),
        )
        self.library_notebook.add(folder_tab, text="  THƯ MỤC  ")
        self.library_notebook.add(create_tab, text="  TẠO MỚI  ")
        self._build_workspace_library(folder_tab)
        create_tab.rowconfigure(0, weight=1)
        create_tab.columnconfigure(0, weight=1)
        self.create_canvas = tk.Canvas(
            create_tab,
            bg=PANEL,
            bd=0,
            highlightthickness=0,
            width=1,
            height=1,
            yscrollincrement=20,
        )
        self.create_scrollbar = ttk.Scrollbar(
            create_tab,
            orient=VERTICAL,
            command=self.create_canvas.yview,
            style="Vertical.TScrollbar",
        )
        self.create_canvas.configure(yscrollcommand=self.create_scrollbar.set)
        self.create_scroll_frame = ttk.Frame(
            self.create_canvas,
            style="Panel.TFrame",
        )
        self.create_scroll_window = self.create_canvas.create_window(
            (0, 0),
            window=self.create_scroll_frame,
            anchor="nw",
        )
        self.create_scroll_frame.bind(
            "<Configure>",
            lambda _event: self.create_canvas.configure(
                scrollregion=self.create_canvas.bbox("all")
            ),
        )
        self.create_canvas.bind(
            "<Configure>",
            lambda event: self.create_canvas.itemconfigure(
                self.create_scroll_window,
                width=max(1, int(event.width)),
            ),
        )
        self.create_canvas.grid(row=0, column=0, sticky="nsew")
        self.create_scrollbar.grid(row=0, column=1, sticky="ns")
        self._build_create_panel(self.create_scroll_frame)
        self._bind_create_scroll(self.create_scroll_frame)

        browser_panel = ttk.Frame(
            body,
            style="Panel.TFrame",
            padding=(14, 14),
        )
        browser_panel.grid(row=0, column=1, sticky="nsew")
        self._build_workspace_browser(browser_panel)

        live_panel = ttk.Frame(
            body,
            style="Panel.TFrame",
            padding=(12, 14),
        )
        live_panel.grid(row=0, column=2, sticky="nsew", padx=(10, 0))
        self._build_realtime_panel(live_panel)

    def _build_global_controls(self, parent: ttk.Frame) -> None:
        """Build the machine-wide Flow and 1MM controls.

        Flow is a single local backend shared by all workspaces, and the 1MM
        API key is a single encrypted machine secret.  Keeping these controls
        in the hub prevents every workspace card from looking like it owns a
        separate service and makes the global state visible before a
        workspace is opened.
        """

        # Keep the three machine-wide controls inside one compact card.  The
        # workspace queue lives in the realtime column, so a tall three-card
        # header can otherwise push the queue below the fold on a normal
        # laptop window.
        container = ttk.Frame(parent, style="PanelAlt.TFrame", padding=(10, 6))
        container.pack(fill="x", pady=(0, 8))
        for column in range(3):
            container.columnconfigure(column, weight=1, uniform="infrastructure")
        container.rowconfigure(1, weight=1)
        self.global_infra_container = container

        common_header = ttk.Frame(container, style="PanelAlt.TFrame")
        common_header.grid(row=0, column=0, columnspan=3, sticky="ew", pady=(0, 5))
        ttk.Label(
            common_header,
            text="HẠ TẦNG DÙNG CHUNG",
            image=self.icons["sparkle"],
            compound=LEFT,
            style="WorkspaceLiveTitle.TLabel",
        ).pack(side=LEFT)
        ttk.Label(
            common_header,
            text="Flow · 1MM.me · kho dữ liệu",
            style="WorkspaceCardMeta.TLabel",
        ).pack(side=LEFT, padx=(9, 0))

        flow_card = ttk.Frame(container, style="Panel.TFrame", padding=(8, 5))
        flow_card.grid(row=1, column=0, sticky="nsew", padx=(0, 4))
        flow_header = ttk.Frame(flow_card, style="Panel.TFrame")
        flow_header.pack(fill="x")
        ttk.Label(
            flow_header,
            text="FLOW",
            image=self.icons["sparkle"],
            compound=LEFT,
            style="WorkspaceLiveTitle.TLabel",
        ).pack(side=LEFT)
        ttk.Label(
            flow_header,
            textvariable=self.global_flow_version_var,
            style="WorkspaceCardMeta.TLabel",
        ).pack(side=RIGHT)
        ttk.Label(
            flow_card,
            textvariable=self.global_flow_status_var,
            style="WorkspaceCardRunning.TLabel",
        ).pack(anchor="w", pady=(5, 0))
        ttk.Label(
            flow_card,
            textvariable=self.global_flow_detail_var,
            style="WorkspaceCardMeta.TLabel",
            wraplength=max(180, round(330 * self.ui_scale)),
            justify=LEFT,
        ).pack(anchor="w", pady=(2, 4))
        flow_actions = ttk.Frame(flow_card, style="Panel.TFrame")
        flow_actions.pack(fill="x")
        self.global_flow_check_button = ttk.Button(
            flow_actions,
            text="Kiểm tra Flow",
            image=self.icons["refresh"],
            compound=LEFT,
            style="Secondary.TButton",
            command=self._check_global_flow_async,
        )
        self.global_flow_check_button.pack(side=RIGHT)
        self.global_flow_start_button = ttk.Button(
            flow_actions,
            text="Khởi động/cập nhật Flow",
            image=self.icons["power"],
            compound=LEFT,
            style="Accent.TButton",
            command=self._start_global_flow_async,
        )
        self.global_flow_start_button.pack(side=RIGHT, padx=(0, 7))
        ttk.Label(
            flow_actions,
            text=f"1 backend · ext {FLOW_EXTENSION_VERSION}",
            style="WorkspaceCardMeta.TLabel",
        ).pack(side=LEFT)

        smm_card = ttk.Frame(container, style="Panel.TFrame", padding=(8, 5))
        smm_card.grid(row=1, column=1, sticky="nsew", padx=(2, 2))
        ttk.Label(
            smm_card,
            text="1MM.ME · API CHUNG",
            image=self.icons["play"],
            compound=LEFT,
            style="WorkspaceLiveTitle.TLabel",
        ).pack(anchor="w")
        smm_key_row = ttk.Frame(smm_card, style="Panel.TFrame")
        smm_key_row.pack(fill="x", pady=(4, 0))
        self.global_smm_api_key_entry = ttk.Entry(
            smm_key_row,
            textvariable=self.global_smm_api_key_var,
            show="•",
        )
        self.global_smm_api_key_entry.pack(side=LEFT, fill="x", expand=True)
        self.global_smm_check_button = ttk.Button(
            smm_key_row,
            text="Lưu & kiểm tra",
            style="Secondary.TButton",
            command=self._save_and_check_global_smm_api_key,
        )
        self.global_smm_check_button.pack(side=RIGHT, padx=(7, 0))
        ttk.Button(
            smm_key_row,
            text="Xóa key",
            style="Secondary.TButton",
            command=self._clear_global_smm_api_key,
        ).pack(side=RIGHT, padx=(5, 0))
        ttk.Label(
            smm_card,
            textvariable=self.global_smm_status_var,
            style="WorkspaceCardMeta.TLabel",
            wraplength=max(180, round(330 * self.ui_scale)),
            justify=LEFT,
        ).pack(anchor="w", pady=(3, 0))
        ttk.Label(
            smm_card,
            textvariable=self.global_smm_balance_var,
            style="WorkspaceCardRunning.TLabel",
        ).pack(anchor="w", pady=(2, 0))
        ttk.Label(
            smm_card,
            textvariable=self.global_smm_services_var,
            style="WorkspaceCardMeta.TLabel",
            wraplength=max(180, round(330 * self.ui_scale)),
            justify=LEFT,
        ).pack(anchor="w", pady=(2, 0))

        storage_card = ttk.Frame(container, style="Panel.TFrame", padding=(8, 5))
        storage_card.grid(row=1, column=2, sticky="nsew", padx=(4, 0))
        storage_header = ttk.Frame(storage_card, style="Panel.TFrame")
        storage_header.pack(fill="x")
        ttk.Label(
            storage_header,
            text="KHO DỮ LIỆU CHUNG",
            image=self.icons["folder"],
            compound=LEFT,
            style="WorkspaceLiveTitle.TLabel",
        ).pack(side=LEFT)
        ttk.Label(
            storage_card,
            text="Media · Output · Prompt · VOICEVOX · checkpoint",
            style="WorkspaceCardMeta.TLabel",
            wraplength=max(180, round(330 * self.ui_scale)),
            justify=LEFT,
        ).pack(anchor="w", pady=(3, 4))
        storage_path_row = ttk.Frame(storage_card, style="Panel.TFrame")
        storage_path_row.pack(fill="x")
        ttk.Label(
            storage_path_row,
            textvariable=self.workspace_storage_var,
            style="WorkspaceCardRunning.TLabel",
            wraplength=max(160, round(260 * self.ui_scale)),
            justify=LEFT,
        ).pack(side=LEFT, fill="x", expand=True, anchor="w")
        self.workspace_storage_button = ttk.Button(
            storage_path_row,
            text="Đổi nơi lưu & chuyển dữ liệu",
            image=self.icons["folder"],
            compound=LEFT,
            style="Accent.TButton",
            command=self.choose_workspace_storage,
        )
        self.workspace_storage_button.pack(side=RIGHT, padx=(10, 0))
        ttk.Button(
            storage_path_row,
            text="Mở thư mục",
            image=self.icons["folder"],
            compound=LEFT,
            style="Secondary.TButton",
            command=self.open_workspace_storage,
        ).pack(side=RIGHT, padx=(0, 7))
        ttk.Label(
            storage_card,
            textvariable=self.workspace_storage_status_var,
            style="WorkspaceCardMeta.TLabel",
            wraplength=max(180, round(330 * self.ui_scale)),
            justify=LEFT,
        ).pack(anchor="w", pady=(3, 0))

        self.global_infra_cards = (flow_card, smm_card, storage_card)
        container.bind(
            "<Configure>",
            self._schedule_global_infrastructure_layout,
            add="+",
        )
        self.root.after_idle(self._relayout_global_infrastructure)

    def _schedule_global_infrastructure_layout(self, _event: object | None = None) -> None:
        """Coalesce resize events so the shared controls never flicker."""

        if getattr(self, "global_infra_layout_after_id", None) is not None:
            return
        try:
            self.global_infra_layout_after_id = self.root.after_idle(
                self._relayout_global_infrastructure
            )
        except tk.TclError:
            self.global_infra_layout_after_id = None

    def _relayout_global_infrastructure(self) -> None:
        """Use three/two/one columns according to the actual window width."""

        self.global_infra_layout_after_id = None
        container = getattr(self, "global_infra_container", None)
        cards = getattr(self, "global_infra_cards", ())
        if container is None or len(cards) != 3:
            return
        try:
            width = max(int(container.winfo_width()), int(self.root.winfo_width()))
            for card in cards:
                card.grid_forget()
            for column in range(3):
                container.columnconfigure(column, weight=0)
            if width >= 1180:
                for column in range(3):
                    container.columnconfigure(column, weight=1)
                cards[0].grid(row=1, column=0, sticky="nsew", padx=(0, 4))
                cards[1].grid(row=1, column=1, sticky="nsew", padx=(2, 2))
                cards[2].grid(row=1, column=2, sticky="nsew", padx=(4, 0))
            elif width >= 820:
                container.columnconfigure(0, weight=1)
                container.columnconfigure(1, weight=1)
                cards[0].grid(row=1, column=0, sticky="nsew", padx=(0, 4))
                cards[1].grid(row=1, column=1, sticky="nsew", padx=(4, 0))
                cards[2].grid(
                    row=2,
                    column=0,
                    columnspan=2,
                    sticky="nsew",
                    pady=(6, 0),
                )
            else:
                container.columnconfigure(0, weight=1)
                for row, card in enumerate(cards):
                    card.grid(
                        row=row + 1,
                        column=0,
                        sticky="nsew",
                        pady=(0 if row == 0 else 6, 0),
                    )
        except tk.TclError:
            return

    def _post_global_ui(self, callback: Callable[[], None]) -> None:
        try:
            self.root.after(0, callback)
        except tk.TclError:
            pass

    @staticmethod
    def _is_youtube_like_service(service: SmmService) -> bool:
        category = "".join(
            character if character.isalnum() else " "
            for character in service.category.casefold()
        )
        name = "".join(
            character if character.isalnum() else " "
            for character in service.name.casefold()
        )
        category_tokens = set(category.split())
        name_tokens = set(name.split())
        if not (
            "youtube" in category_tokens
            and bool(category_tokens.intersection({"like", "likes"}))
        ):
            return False
        if category_tokens.intersection(
            {"view", "views", "comment", "comments", "dislike", "dislikes"}
        ):
            return False
        if name_tokens.intersection(
            {"comment", "comments", "community", "post", "dislike", "dislikes"}
        ):
            return False
        return not (
            name_tokens.intersection({"short", "shorts"})
            and "video" not in name_tokens
        )

    def _format_global_flow_status(self, status: FlowAgentStatus) -> None:
        self.global_flow_status = status
        if status.ready:
            label = "✓  FLOW · SẴN SÀNG"
        elif status.backend_running:
            label = "◐  FLOW · BACKEND ĐANG CHẠY, CHỜ PHIÊN"
        elif status.edge_path is None:
            label = "○  FLOW · CHƯA CÓ EDGE"
        else:
            label = "!  FLOW · BACKEND CHƯA SẴN SÀNG"
        self.global_flow_status_var.set(label)
        version = status.extension_version.strip()
        self.global_flow_version_var.set(
            f"ext {version or 'chưa báo'} · {status.flow_tab_count} tab"
        )
        detail = status.detail or "Chưa có chi tiết từ Flow backend."
        if status.connected_client_count:
            detail += f" · {status.connected_client_count} client đang nối"
        self.global_flow_detail_var.set(detail)

    def _set_global_flow_busy(self, busy: bool, *, starting: bool = False) -> None:
        self.global_flow_busy = busy
        if hasattr(self, "global_flow_check_button"):
            state = "disabled" if busy else "normal"
            self.global_flow_check_button.configure(state=state)
            self.global_flow_start_button.configure(state=state)
        if busy:
            self.global_flow_status_var.set(
                "↻  FLOW · ĐANG KHỞI ĐỘNG/KIỂM TRA…"
                if starting
                else "↻  FLOW · ĐANG KIỂM TRA…"
            )

    def _apply_global_flow_error(self, message: str) -> None:
        self.global_flow_status = None
        self.global_flow_status_var.set("!  FLOW · KIỂM TRA THẤT BẠI")
        self.global_flow_detail_var.set(str(message))
        self.global_flow_version_var.set("")

    def _check_global_flow_async(self, *, silent: bool = False) -> None:
        if self.flow_status_provider is None or self.global_flow_busy:
            return
        self._set_global_flow_busy(True)
        if not silent:
            self.selection_hint_var.set(
                "Đang kiểm tra hạ tầng Flow dùng chung; không mở thêm workspace hoặc tab."
            )

        def worker() -> None:
            try:
                status = self.flow_status_provider()
                if not isinstance(status, FlowAgentStatus):
                    raise RuntimeError("Flow status provider trả về dữ liệu không hợp lệ.")
                self._post_global_ui(
                    lambda result=status: self._finish_global_flow_check(result, "")
                )
            except Exception as exc:
                self._post_global_ui(
                    lambda error=str(exc): self._finish_global_flow_check(None, error)
                )

        threading.Thread(
            target=worker,
            name="GlobalFlowStatus",
            daemon=True,
        ).start()

    def _finish_global_flow_check(
        self,
        status: FlowAgentStatus | None,
        error: str,
    ) -> None:
        self._set_global_flow_busy(False)
        if status is None:
            self._apply_global_flow_error(error or "Không nhận được trạng thái Flow.")
            return
        self._format_global_flow_status(status)

    def _start_global_flow_async(self) -> None:
        if self.flow_start_handler is None or self.global_flow_busy:
            return
        self._set_global_flow_busy(True, starting=True)
        self.global_flow_detail_var.set(
            "Đang dùng lại backend chung, cập nhật extension tại chỗ và chỉ mở tab Flow nếu thật sự thiếu."
        )

        def worker() -> None:
            try:
                status = self.flow_start_handler()
                if not isinstance(status, FlowAgentStatus):
                    if self.flow_status_provider is None:
                        raise RuntimeError("Flow start handler không trả về trạng thái.")
                    status = self.flow_status_provider()
                if not isinstance(status, FlowAgentStatus):
                    raise RuntimeError("Flow status provider trả về dữ liệu không hợp lệ.")
                self._post_global_ui(
                    lambda result=status: self._finish_global_flow_check(result, "")
                )
            except Exception as exc:
                record_exception("global-flow-start", exc)
                self._post_global_ui(
                    lambda error=str(exc): self._finish_global_flow_check(None, error)
                )

        threading.Thread(
            target=worker,
            name="GlobalFlowStart",
            daemon=True,
        ).start()

    def _schedule_global_flow_refresh(self, delay_ms: int = 5_000) -> None:
        if self.flow_status_provider is None:
            return
        previous = self.global_flow_refresh_after_id
        self.global_flow_refresh_after_id = None
        if previous is not None:
            try:
                self.root.after_cancel(previous)
            except tk.TclError:
                pass
        try:
            self.global_flow_refresh_after_id = self.root.after(
                max(500, int(delay_ms)),
                self._refresh_global_flow,
            )
        except tk.TclError:
            pass

    def _refresh_global_flow(self) -> None:
        self.global_flow_refresh_after_id = None
        if self.flow_status_provider is not None and not self.global_flow_busy:
            self._check_global_flow_async(silent=True)
        self._schedule_global_flow_refresh(5_000)

    def _refresh_global_smm_display(self) -> None:
        if hasattr(self, "global_smm_api_key_entry"):
            self.global_smm_api_key_entry.configure(
                state="disabled" if self.global_smm_busy else "normal"
            )
            self.global_smm_check_button.configure(
                state="disabled" if self.global_smm_busy else "normal"
            )

    def _save_and_check_global_smm_api_key(self) -> None:
        if self.global_smm_busy:
            return
        entered = self.global_smm_api_key_var.get().strip()
        try:
            if entered:
                save_smm_api_key(entered)
                self.global_smm_api_key_var.set("")
            elif load_smm_api_key() is None:
                raise SmmApiError("Hãy nhập API key 1MM trước khi kiểm tra.")
        except Exception as exc:
            self.global_smm_status_var.set(str(exc))
            messagebox.showerror("Không lưu được API key 1MM", str(exc), parent=self.root)
            self._refresh_global_smm_display()
            return

        self.global_smm_busy = True
        self.global_smm_status_var.set(
            "Đang kiểm tra số dư và tải các gói YouTube Like từ 1MM…"
        )
        self._refresh_global_smm_display()

        def worker() -> None:
            try:
                client = SmmApiClient()
                balance = client.balance()
                services = tuple(
                    service
                    for service in client.services()
                    if self._is_youtube_like_service(service)
                )
                self._post_global_ui(
                    lambda: self._finish_global_smm_account(balance, services, "")
                )
            except Exception as exc:
                self._post_global_ui(
                    lambda error=str(exc): self._finish_global_smm_account(
                        None, (), error
                    )
                )

        threading.Thread(
            target=worker,
            name="GlobalSmmAccount",
            daemon=True,
        ).start()

    def _finish_global_smm_account(
        self,
        balance: SmmBalance | None,
        services: tuple[SmmService, ...],
        error: str,
    ) -> None:
        self.global_smm_busy = False
        if balance is None:
            self.global_smm_status_var.set(error or "Không kiểm tra được tài khoản 1MM.")
            self._refresh_global_smm_display()
            return
        self.global_smm_status_var.set(
            f"✓ Đã kết nối 1MM · {len(services)} gói YouTube Like hợp lệ."
        )
        self.global_smm_balance_var.set(
            f"Số dư: {balance.amount} {balance.currency}"
        )
        self.global_smm_services_var.set(
            "Danh sách gói đã sẵn sàng cho các workspace."
            if services
            else "API hoạt động nhưng chưa có gói YouTube Like phù hợp."
        )
        self._refresh_global_smm_display()
        if self.smm_account_broadcast is not None:
            try:
                self.smm_account_broadcast(balance, tuple(services))
            except Exception as exc:
                record_exception("global-smm-account-broadcast", exc)

    def _clear_global_smm_api_key(self) -> None:
        if self.global_smm_busy or load_smm_api_key() is None:
            return
        if not messagebox.askyesno(
            "Xóa API key 1MM",
            "Xóa API key 1MM đang được mã hóa trên máy này?",
            parent=self.root,
        ):
            return
        try:
            clear_smm_api_key()
        except SmmApiError as exc:
            messagebox.showerror("Không xóa được API key", str(exc), parent=self.root)
            return
        self.global_smm_api_key_var.set("")
        self.global_smm_balance_var.set("Số dư: chưa kiểm tra")
        self.global_smm_services_var.set("Chưa tải danh sách gói YouTube Like.")
        self.global_smm_status_var.set("Đã xóa API key 1MM khỏi kho mã hóa Windows.")
        self._refresh_global_smm_display()
        if self.smm_key_changed_handler is not None:
            try:
                self.smm_key_changed_handler(False)
            except Exception as exc:
                record_exception("global-smm-key-clear-broadcast", exc)

    def _build_header(self, parent: ttk.Frame) -> None:
        header = ttk.Frame(parent, style="Panel.TFrame", padding=(12, 8))
        header.pack(fill="x", pady=(0, 10))
        ttk.Label(
            header,
            image=self.icons["brand_large"],
            background=PANEL,
            padding=(4, 3),
        ).pack(side=LEFT, padx=(0, 10))
        title_block = ttk.Frame(header, style="Panel.TFrame")
        title_block.pack(side=LEFT)
        ttk.Label(
            title_block,
            text="DocuRandom Studio",
            style="Header.TLabel",
        ).pack(anchor="w")
        ttk.Label(
            title_block,
            text=f"WORKSPACES  /  AI EDITOR  /  RANDOM VIDEO  ·  v{__version__}",
            style="Muted.TLabel",
        ).pack(anchor="w", pady=(2, 0))
        self.update_button = ttk.Button(
            header,
            textvariable=self.update_status_var,
            style="Secondary.TButton",
            command=self._update_button_clicked,
        )
        self.update_button.pack(side=RIGHT, padx=(8, 4))
        if self.telegram_service is not None:
            self.telegram_button = ttk.Button(
                header,
                textvariable=self.telegram_status_var,
                image=self.icons["queue"],
                compound=LEFT,
                style="Secondary.TButton",
                command=self._open_telegram_settings,
            )
            self.telegram_button.pack(side=RIGHT, padx=(8, 0))
        if DONUT_YOUTUBE_ENABLED:
            self.donut_button = ttk.Button(
                header,
                textvariable=self.donut_status_var,
                image=self.icons["sliders"],
                compound=LEFT,
                style="Secondary.TButton",
                command=self._open_donut_settings,
            )
            self.donut_button.pack(side=RIGHT, padx=(8, 0))

    def _refresh_telegram_status(self) -> None:
        service = self.telegram_service
        if service is None:
            return
        state = service.state()
        self.telegram_detail_var.set(state.detail)
        if state.status == "connected":
            self.telegram_status_var.set("●  TELEGRAM")
            style = "Accent.TButton" if state.paired_chat_id else "Secondary.TButton"
        elif state.status == "send_only":
            self.telegram_status_var.set("◐  TELEGRAM")
            style = "Secondary.TButton"
        elif state.status == "error":
            self.telegram_status_var.set("!  TELEGRAM")
            style = "Danger.TButton"
        elif state.status == "starting":
            self.telegram_status_var.set("↻  TELEGRAM")
            style = "Secondary.TButton"
        else:
            self.telegram_status_var.set("○  TELEGRAM")
            style = "Secondary.TButton"
        if hasattr(self, "telegram_button"):
            self.telegram_button.configure(style=style)
        try:
            self.root.after(1000, self._refresh_telegram_status)
        except tk.TclError:
            pass

    def _open_telegram_settings(self) -> None:
        service = self.telegram_service
        if service is None:
            return
        dialog = self.telegram_settings_dialog
        if dialog is not None:
            try:
                if dialog.winfo_exists():
                    dialog.deiconify()
                    dialog.lift()
                    dialog.focus_force()
                    return
            except tk.TclError:
                pass
        settings = service.settings()
        self.telegram_enabled_var.set(settings.enabled)
        self.telegram_notify_prompts_var.set(settings.notify_prompts)
        self.telegram_notify_errors_var.set(settings.notify_errors)
        self.telegram_notify_status_var.set(settings.notify_status)
        self.telegram_token_var.set("")
        dialog = tk.Toplevel(self.root)
        self.telegram_settings_dialog = dialog
        dialog.title("Telegram Bot · điều khiển từ xa")
        dialog.configure(bg=BACKGROUND)
        dialog.transient(self.root)
        dialog.resizable(False, False)
        scale = max(0.9, min(1.3, self.ui_scale))
        width = round(650 * scale)
        height = round(660 * scale)
        x = self.root.winfo_rootx() + max(0, (self.root.winfo_width() - width) // 2)
        y = self.root.winfo_rooty() + max(0, (self.root.winfo_height() - height) // 2)
        dialog.geometry(f"{width}x{height}+{x}+{y}")
        body = ttk.Frame(dialog, style="Panel.TFrame", padding=(20, 18))
        body.pack(fill=BOTH, expand=True)
        ttk.Label(
            body,
            text="TELEGRAM BOT · ĐIỀU KHIỂN AN TOÀN",
            image=self.icons["queue"],
            compound=LEFT,
            style="Section.TLabel",
        ).pack(anchor="w")
        ttk.Label(
            body,
            text=(
                "Một bot dùng chung cho toàn bộ DocuRandom: gửi prompt/lỗi/trạng thái, "
                "điều khiển workspace, Flow, VOICEVOX, lịch và thumbnail từ đúng một chat "
                "riêng đã ghép cặp. Telegram tự hiện Menu lệnh; upload và thay ảnh đều có "
                "mã xác nhận một lần."
            ),
            style="Muted.TLabel",
            wraplength=600,
            justify=LEFT,
        ).pack(anchor="w", pady=(8, 12))
        ttk.Label(
            body,
            text=(
                "Token đã từng gửi trong chat phải thu hồi bằng /revoke ở BotFather. "
                "Chỉ dán token mới; tool mã hóa bằng Windows và không ghi token vào workspace/log."
            ),
            style="CardMuted.TLabel",
            wraplength=600,
            justify=LEFT,
        ).pack(anchor="w", pady=(0, 12))
        ttk.Label(body, text="TOKEN MỚI TỪ BOTFATHER", style="WorkspaceField.TLabel").pack(anchor="w")
        token_entry = ttk.Entry(body, textvariable=self.telegram_token_var, show="•")
        token_entry.pack(fill="x", pady=(5, 4))
        ttk.Label(
            body,
            text="Để trống khi chỉ đổi lựa chọn và muốn giữ token đã mã hóa.",
            style="CardMuted.TLabel",
        ).pack(anchor="w")
        ttk.Checkbutton(
            body,
            text="Bật Telegram khi DocuRandom đang mở",
            variable=self.telegram_enabled_var,
        ).pack(anchor="w", pady=(12, 4))
        ttk.Checkbutton(
            body,
            text="Không gửi từng prompt ảnh (chống spam)",
            variable=self.telegram_notify_prompts_var,
            state="disabled",
        ).pack(anchor="w", pady=2)
        ttk.Checkbutton(
            body,
            text="Gửi lỗi có hướng phục hồi",
            variable=self.telegram_notify_errors_var,
        ).pack(anchor="w", pady=2)
        ttk.Checkbutton(
            body,
            text="Gửi trạng thái hoàn tất/dừng",
            variable=self.telegram_notify_status_var,
        ).pack(anchor="w", pady=2)
        ttk.Label(
            body,
            textvariable=self.telegram_detail_var,
            style="CardMuted.TLabel",
            wraplength=600,
            justify=LEFT,
        ).pack(anchor="w", pady=(12, 4))
        ttk.Label(
            body,
            textvariable=self.telegram_pairing_var,
            style="WorkspaceField.TLabel",
            wraplength=600,
            justify=LEFT,
        ).pack(anchor="w", pady=(2, 10))
        actions_top = ttk.Frame(body, style="Panel.TFrame")
        actions_top.pack(fill="x")
        self.telegram_save_button = ttk.Button(
            actions_top,
            text="LƯU & KẾT NỐI",
            image=self.icons["power"],
            compound=LEFT,
            style="Smart.TButton",
            command=self._save_telegram_settings,
        )
        self.telegram_save_button.pack(side=LEFT)
        self.telegram_pair_button = ttk.Button(
            actions_top,
            text="TẠO MÃ GHÉP CẶP",
            style="Secondary.TButton",
            command=self._create_telegram_pairing,
        )
        self.telegram_pair_button.pack(side=LEFT, padx=(8, 0))
        actions_bottom = ttk.Frame(body, style="Panel.TFrame")
        actions_bottom.pack(fill="x", side="bottom", pady=(16, 0))
        ttk.Button(
            actions_bottom,
            text="Xóa token",
            style="Danger.TButton",
            command=self._clear_telegram_token,
        ).pack(side=LEFT)
        ttk.Button(
            actions_bottom,
            text="Gửi thử",
            style="Secondary.TButton",
            command=self._test_telegram_message,
        ).pack(side=RIGHT, padx=(0, 8))
        ttk.Button(
            actions_bottom,
            text="Đóng",
            style="Secondary.TButton",
            command=self._close_telegram_settings,
        ).pack(side=RIGHT)
        dialog.protocol("WM_DELETE_WINDOW", self._close_telegram_settings)
        token_entry.focus_set()

    def _close_telegram_settings(self) -> None:
        dialog = self.telegram_settings_dialog
        self.telegram_settings_dialog = None
        if dialog is not None:
            try:
                dialog.destroy()
            except tk.TclError:
                pass

    def _save_telegram_settings(self) -> None:
        service = self.telegram_service
        if service is None or self.telegram_config_busy:
            return
        token = self.telegram_token_var.get().strip() or service.settings().token
        if not token:
            messagebox.showerror(
                "Chưa có token mới",
                "Hãy thu hồi token cũ trong BotFather rồi dán token mới vào đây.",
                parent=self.telegram_settings_dialog or self.root,
            )
            return
        enabled = bool(self.telegram_enabled_var.get())
        notify_prompts = False
        notify_errors = bool(self.telegram_notify_errors_var.get())
        notify_status = bool(self.telegram_notify_status_var.get())
        self.telegram_config_busy = True
        self.telegram_save_button.configure(state="disabled")
        self.telegram_detail_var.set("Đang xác minh token trực tiếp với Telegram…")

        def worker() -> None:
            try:
                settings = service.configure(
                    token=token,
                    enabled=enabled,
                    notify_prompts=notify_prompts,
                    notify_errors=notify_errors,
                    notify_status=notify_status,
                )
                self._post_update_ui(
                    lambda value=settings: self._finish_telegram_save(value.bot_username)
                )
            except Exception as exc:
                self._post_update_ui(lambda message=str(exc): self._finish_telegram_error(message))

        threading.Thread(target=worker, daemon=True, name="telegram-configure").start()

    def _finish_telegram_save(self, username: str) -> None:
        self.telegram_config_busy = False
        self.telegram_token_var.set("")
        if hasattr(self, "telegram_save_button"):
            self.telegram_save_button.configure(state="normal")
        self.telegram_detail_var.set(f"Đã xác minh @{username}. Hãy tạo mã ghép cặp.")

    def _finish_telegram_error(self, message: str) -> None:
        self.telegram_config_busy = False
        if hasattr(self, "telegram_save_button"):
            self.telegram_save_button.configure(state="normal")
        self.telegram_detail_var.set(message)
        messagebox.showerror(
            "Không kết nối được Telegram",
            message,
            parent=self.telegram_settings_dialog or self.root,
        )

    def _create_telegram_pairing(self) -> None:
        service = self.telegram_service
        if service is None:
            return
        try:
            code = service.begin_pairing()
        except TelegramBotError as exc:
            messagebox.showerror(
                "Chưa thể ghép cặp",
                str(exc),
                parent=self.telegram_settings_dialog or self.root,
            )
            return
        username = service.settings().bot_username
        self.telegram_pairing_var.set(
            f"Mở @{username} và gửi: /pair {code} · mã dùng một lần, hết hạn sau 5 phút."
        )

    def _test_telegram_message(self) -> None:
        service = self.telegram_service
        if service is None:
            return
        try:
            service.send_test()
            self.telegram_detail_var.set("Đã xếp tin nhắn thử; Telegram sẽ nhận trong vài giây.")
        except TelegramBotError as exc:
            messagebox.showerror(
                "Chưa gửi được",
                str(exc),
                parent=self.telegram_settings_dialog or self.root,
            )

    def _clear_telegram_token(self) -> None:
        service = self.telegram_service
        if service is None:
            return
        confirmed = messagebox.askyesno(
            "Xóa kết nối Telegram?",
            "Tool sẽ dừng bot, xóa token đã mã hóa và bỏ ghép cặp chat trên máy này.",
            parent=self.telegram_settings_dialog or self.root,
        )
        if not confirmed:
            return
        service.disable(clear_token=True)
        self.telegram_token_var.set("")
        self.telegram_enabled_var.set(False)
        self.telegram_pairing_var.set("Đã xóa token và chat ghép cặp.")
        self.telegram_detail_var.set("Telegram đã tắt.")

    def _open_donut_settings(self) -> None:
        if not DONUT_YOUTUBE_ENABLED:
            return
        self._refresh_donut_manage_action()
        dialog = self.donut_settings_dialog
        if dialog is not None:
            try:
                if dialog.winfo_exists():
                    dialog.deiconify()
                    dialog.lift()
                    dialog.focus_force()
                    return
            except tk.TclError:
                pass
        dialog = tk.Toplevel(self.root)
        self.donut_settings_dialog = dialog
        dialog.title("Donut Local · MCP")
        dialog.configure(bg=BACKGROUND)
        dialog.transient(self.root)
        dialog.resizable(False, False)
        dialog_scale = max(0.9, min(1.3, self.ui_scale))
        dialog_width = round(600 * dialog_scale)
        dialog_height = round(755 * dialog_scale)
        origin_x = self.root.winfo_rootx() + max(
            0, (self.root.winfo_width() - dialog_width) // 2
        )
        origin_y = self.root.winfo_rooty() + max(
            0, (self.root.winfo_height() - dialog_height) // 2
        )
        dialog.geometry(
            f"{dialog_width}x{dialog_height}+{origin_x}+{origin_y}"
        )
        try:
            dialog.iconbitmap(default=self.root.iconbitmap())
        except Exception:
            pass
        body = ttk.Frame(dialog, style="Panel.TFrame", padding=(20, 18))
        body.pack(fill=BOTH, expand=True)
        ttk.Label(
            body,
            text="DONUT MCP · CÀI ĐẶT TOÀN CỤC",
            image=self.icons["brand"],
            compound=LEFT,
            style="Section.TLabel",
        ).pack(anchor="w")
        ttk.Label(
            body,
            text=(
                "Donut Local Developer được phát hành riêng dưới dạng gói đã ký. Tool tự nhận "
                "phiên bản, hỏi xác nhận, tải, kiểm tra chữ ký/SHA-256, cài nguyên tử và mở "
                "lại. Profile, cookie, proxy và fingerprint dùng nguyên dữ liệu DonutBrowser."
            ),
            style="Muted.TLabel",
            wraplength=550,
            justify=LEFT,
        ).pack(anchor="w", pady=(8, 12))
        ttk.Label(
            body,
            text="GITHUB PRIVATE · ĐĂNG NHẬP MỘT LẦN",
            style="WorkspaceField.TLabel",
        ).pack(anchor="w", pady=(0, 5))
        ttk.Label(
            body,
            text=(
                "Dán mã truy cập GitHub có quyền đọc kho JustHari01/donutbrowser-local. "
                "Mã được mã hóa bằng Windows và chỉ dùng để tool tự tải bản Donut mới."
            ),
            style="CardMuted.TLabel",
            wraplength=550,
            justify=LEFT,
        ).pack(anchor="w", pady=(0, 6))
        github_row = ttk.Frame(body, style="Panel.TFrame")
        github_row.pack(fill="x")
        ttk.Entry(
            github_row,
            textvariable=self.donut_github_token_var,
            show="•",
        ).pack(side=LEFT, fill="x", expand=True)
        self.donut_github_open_button = ttk.Button(
            github_row,
            text="MỞ GITHUB",
            style="Secondary.TButton",
            command=self._open_donut_github_login,
        )
        self.donut_github_open_button.pack(side=RIGHT, padx=(8, 0))
        self.donut_github_button = ttk.Button(
            github_row,
            text="LƯU & KIỂM TRA",
            style="Smart.TButton",
            command=self._save_and_test_donut_github,
        )
        self.donut_github_button.pack(side=RIGHT, padx=(8, 0))
        ttk.Label(
            body,
            textvariable=self.donut_github_status_var,
            style="CardMuted.TLabel",
            wraplength=550,
            justify=LEFT,
        ).pack(anchor="w", pady=(5, 12))
        ttk.Label(
            body,
            text="BROWSER BRIDGE · CÀI MỘT LẦN CHO TẤT CẢ PROFILE",
            style="WorkspaceField.TLabel",
        ).pack(anchor="w", pady=(0, 5))
        ttk.Label(
            body,
            text=(
                "Tool tự cài/cập nhật Bridge cho mọi profile Wayfern. Nhóm tiện ích "
                "đang có được giữ nguyên; khi upload, tool tự mở profile và tự ghép/kích hoạt. "
                "Profile đang mở lúc cập nhật chỉ cần đóng/mở lại một lần."
            ),
            style="CardMuted.TLabel",
            wraplength=550,
            justify=LEFT,
        ).pack(anchor="w", pady=(0, 7))
        self.donut_bridge_install_button = ttk.Button(
            body,
            text="CÀI / CẬP NHẬT BRIDGE CHO TẤT CẢ PROFILE",
            image=self.icons["refresh"],
            compound=LEFT,
            style="Smart.TButton",
            command=self._install_donut_bridge_all_profiles,
        )
        self.donut_bridge_install_button.pack(fill="x", pady=(0, 12))
        ttk.Label(body, text="KẾT NỐI MCP DỰ PHÒNG (KHÔNG BẮT BUỘC)", style="WorkspaceField.TLabel").pack(
            anchor="w", pady=(0, 5)
        )
        entry = ttk.Entry(body, textvariable=self.donut_endpoint_var, show="•")
        entry.pack(fill="x")
        entry.bind("<Return>", lambda _event: self._test_donut_connection())
        ttk.Progressbar(
            body,
            variable=self.donut_progress_var,
            maximum=100.0,
            mode="determinate",
        ).pack(fill="x", pady=(12, 0))
        ttk.Label(
            body,
            textvariable=self.donut_test_detail_var,
            style="CardMuted.TLabel",
            wraplength=550,
            justify=LEFT,
        ).pack(anchor="w", pady=(10, 0))
        actions = ttk.Frame(body, style="Panel.TFrame")
        actions.pack(fill="x", side="bottom", pady=(18, 0))
        self.donut_test_button = ttk.Button(
            actions,
            text="TEST KẾT NỐI",
            image=self.icons["refresh"],
            compound=LEFT,
            style="Smart.TButton",
            command=self._test_donut_connection,
        )
        self.donut_test_button.pack(side=RIGHT)
        self.donut_manage_button = ttk.Button(
            actions,
            textvariable=self.donut_manage_action_var,
            image=self.icons["power"],
            compound=LEFT,
            style="Smart.TButton",
            command=self._install_or_open_donut,
        )
        self.donut_manage_button.pack(side=LEFT)
        ttk.Button(
            actions,
            text="Đóng",
            style="Secondary.TButton",
            command=dialog.destroy,
        ).pack(side=RIGHT, padx=(0, 8))
        dialog.protocol("WM_DELETE_WINDOW", dialog.destroy)
        entry.focus_set()

    def _set_donut_progress(self, percent: float, detail: str) -> None:
        self.donut_progress_var.set(max(0.0, min(100.0, float(percent))))
        self.donut_test_detail_var.set(detail)

    def _open_donut_github_login(self) -> None:
        url = private_github_token_url()
        try:
            opened = bool(webbrowser.open(url, new=2))
        except Exception as exc:
            record_exception("donut-private-github-open", exc)
            opened = False
        if opened:
            self.donut_github_status_var.set(
                "GitHub đã mở · đăng nhập, chọn kho donutbrowser-local, tạo mã rồi dán vào ô."
            )
            return
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(url)
            self.root.update_idletasks()
        except tk.TclError:
            pass
        messagebox.showwarning(
            "Không mở được GitHub",
            "Tool chưa mở được trình duyệt. Link tạo mã đã được sao chép; hãy dán vào trình duyệt.",
            parent=self.donut_settings_dialog or self.root,
        )

    def _save_and_test_donut_github(self) -> None:
        if self.donut_github_busy or self.donut_test_busy:
            return
        raw = self.donut_github_token_var.get().strip()
        token = raw or load_private_github_token()
        if not token:
            messagebox.showwarning(
                "Chưa có đăng nhập GitHub",
                "Hãy dán mã truy cập GitHub có quyền đọc kho Donut Local private.",
                parent=self.donut_settings_dialog or self.root,
            )
            return
        self.donut_github_busy = True
        self.donut_github_status_var.set("Đang kiểm tra quyền tải Donut Local…")
        if hasattr(self, "donut_github_button"):
            self.donut_github_button.configure(state="disabled")

        def worker() -> None:
            try:
                identity = test_private_github_access(token)
                save_private_github_token(token)
                self._post_update_ui(
                    lambda login=identity.login: self._finish_donut_github_test(login)
                )
            except Exception as exc:
                record_exception("donut-private-github-login", exc)
                self._post_update_ui(
                    lambda message=str(exc): self._finish_donut_github_test("", message)
                )

        threading.Thread(
            target=worker,
            name="donut-private-github-login",
            daemon=True,
        ).start()

    def _finish_donut_github_test(self, login: str, error: str = "") -> None:
        self.donut_github_busy = False
        if hasattr(self, "donut_github_button"):
            self.donut_github_button.configure(state="normal")
        if error:
            self.donut_github_status_var.set("! " + error)
            messagebox.showerror(
                "Không đăng nhập được GitHub",
                error,
                parent=self.donut_settings_dialog or self.root,
            )
            return
        self.donut_github_token_var.set("")
        self.donut_github_status_var.set(
            f"✓ GitHub @{login} đã sẵn sàng tải Donut Local private."
        )
        self._start_donut_update_check()

    def _install_donut_bridge_all_profiles(self) -> None:
        if (
            not DONUT_YOUTUBE_ENABLED
            or self.donut_test_busy
            or self.donut_bridge_install_busy
        ):
            return
        extension_dir = bundled_browser_extension_dir()
        if extension_dir is None:
            messagebox.showerror(
                "Thiếu Browser Bridge",
                "Bản DocuRandom hiện tại chưa chứa gói Browser Bridge. Hãy cập nhật tool rồi thử lại.",
                parent=self.donut_settings_dialog or self.root,
            )
            return
        raw_endpoint = self.donut_endpoint_var.get().strip()
        try:
            endpoint = (
                DonutMcpEndpoint.parse(raw_endpoint)
                if raw_endpoint
                else DonutManager.discover_endpoint()
            )
        except Exception as exc:
            messagebox.showwarning(
                "Donut Local chưa sẵn sàng",
                "Hãy mở Donut Local rồi bấm Test kết nối trước.\n\n" + str(exc),
                parent=self.donut_settings_dialog or self.root,
            )
            return

        self.donut_bridge_install_busy = True
        self.donut_test_busy = True
        self.donut_progress_var.set(0.0)
        self.donut_status_var.set("●  ĐANG CÀI BRIDGE CHO TẤT CẢ PROFILE…")
        self.donut_test_detail_var.set("Bước 1/4 · đang xác minh Donut Local và gói Bridge…")
        for name in (
            "donut_bridge_install_button",
            "donut_test_button",
            "donut_manage_button",
        ):
            control = getattr(self, name, None)
            if control is not None:
                control.configure(state="disabled")

        def report(percent: float, detail: str) -> None:
            self._post_update_ui(
                lambda value=percent, message=detail: self._set_donut_progress(
                    value, message
                )
            )

        def worker() -> None:
            try:
                with DonutMcpClient(
                    endpoint,
                    timeout=60.0,
                    shared_session=False,
                ) as client:
                    result = install_bridge_for_all_profiles(
                        client,
                        extension_dir,
                        progress=report,
                    )
                save_donut_mcp_endpoint(endpoint.value)
                self._post_update_ui(
                    lambda value=result: self._apply_donut_bridge_install_success(value)
                )
            except Exception as exc:
                record_exception("donut-bridge-install-all", exc)
                self._post_update_ui(
                    lambda message=str(exc): self._apply_donut_bridge_install_error(message)
                )

        threading.Thread(
            target=worker,
            name="donut-bridge-install-all",
            daemon=True,
        ).start()

    def _apply_donut_bridge_install_success(
        self, result: DonutBridgeInstallResult
    ) -> None:
        self.donut_bridge_install_busy = False
        self.donut_test_busy = False
        for name in (
            "donut_bridge_install_button",
            "donut_test_button",
            "donut_manage_button",
        ):
            control = getattr(self, name, None)
            if control is not None:
                control.configure(state="normal")
        version = f" {result.extension_version}" if result.extension_version else ""
        self.donut_progress_var.set(100.0)
        self.donut_status_var.set(
            f"✓  BRIDGE{version} · {result.compatible_profiles} PROFILE"
        )
        restart_note = (
            f" · {len(result.running_profile_names)} profile đang mở cần đóng/mở lại"
            if result.running_profile_names
            else ""
        )
        self.donut_test_detail_var.set(
            f"Đã cài/cập nhật một gói Bridge dùng chung và gán cho "
            f"{result.compatible_profiles} profile Wayfern; giữ nguyên "
            f"{result.preserved_groups} nhóm tiện ích hiện có{restart_note}."
        )
        detail = (
            f"Đã cài/cập nhật DocuRandom Donut Bridge{version} cho "
            f"{result.compatible_profiles} profile Wayfern.\n\n"
            "Nhóm tiện ích, cookie, proxy và fingerprint hiện có không bị thay đổi."
        )
        if result.running_profile_names:
            preview = ", ".join(result.running_profile_names[:5])
            if len(result.running_profile_names) > 5:
                preview += f" và {len(result.running_profile_names) - 5} profile khác"
            detail += (
                "\n\nCác profile đang mở cần đóng/mở lại một lần để nạp bản mới: "
                + preview
                + "."
            )
        if result.skipped_profile_names:
            detail += (
                f"\n\nĐã bỏ qua {len(result.skipped_profile_names)} profile không phải Wayfern "
                "vì tiện ích Chromium không tương thích."
            )
        messagebox.showinfo(
            "Đã cài Bridge cho tất cả profile",
            detail,
            parent=self.donut_settings_dialog or self.root,
        )

    def _apply_donut_bridge_install_error(self, message: str) -> None:
        self.donut_bridge_install_busy = False
        self.donut_test_busy = False
        for name in (
            "donut_bridge_install_button",
            "donut_test_button",
            "donut_manage_button",
        ):
            control = getattr(self, name, None)
            if control is not None:
                control.configure(state="normal")
        self.donut_progress_var.set(0.0)
        self.donut_status_var.set("!  CHƯA CÀI ĐƯỢC BROWSER BRIDGE")
        self.donut_test_detail_var.set(message)
        messagebox.showerror(
            "Không cài được Bridge cho tất cả profile",
            message,
            parent=self.donut_settings_dialog or self.root,
        )

    def _refresh_donut_manage_action(self) -> None:
        if self.donut_manager is None:
            return
        self.donut_manage_action_var.set(self.donut_manager.manage_action_text())

    def _start_donut_update_check(self) -> None:
        self.donut_update_check_after_id = None
        if (
            getattr(self, "_launcher_closed", False)
            or not DONUT_YOUTUBE_ENABLED
            or self.donut_manager is None
            or self.donut_update_check_busy
            or self.donut_test_busy
            or load_private_github_token() is None
        ):
            self._refresh_donut_manage_action()
            return
        self._refresh_donut_manage_action()
        self.donut_update_check_busy = True
        cancel_event = self.donut_update_check_cancel_event
        manager = self.donut_manager
        launcher_ref = weakref.ref(self)

        def worker() -> None:
            launcher = launcher_ref()
            if (
                cancel_event.is_set()
                or launcher is None
                or getattr(launcher, "_launcher_closed", False)
            ):
                return
            update_error = ""
            try:
                release = manager.available_update()
            except Exception as exc:
                launcher = launcher_ref()
                if (
                    cancel_event.is_set()
                    or launcher is None
                    or getattr(launcher, "_launcher_closed", False)
                ):
                    return
                record_exception("donut-local-background-update-check", exc)
                update_error = str(exc)
                release = None
            launcher = launcher_ref()
            if (
                cancel_event.is_set()
                or launcher is None
                or getattr(launcher, "_launcher_closed", False)
            ):
                return
            launcher._post_update_ui(
                lambda value=release, error=update_error, target=launcher_ref: (
                    target_obj._finish_donut_update_check(value, error=error)
                    if (target_obj := target()) is not None
                    else None
                )
            )

        thread = threading.Thread(
            target=worker,
            name="donut-local-update-check",
            daemon=True,
        )
        self.donut_update_check_thread = thread
        thread.start()

    def _finish_donut_update_check(self, release: object, *, error: str = "") -> None:
        if getattr(self, "_launcher_closed", False) or self.donut_update_check_cancel_event.is_set():
            return
        self.donut_update_check_busy = False
        if error:
            # A failed check says nothing about release availability. Keep the
            # running browser and workflow status; expose the actionable
            # updater failure without opening a modal during automation.
            self.donut_test_detail_var.set(
                "Chưa kiểm tra được cập nhật Donut Local: " + error
                + " Trình duyệt hiện tại vẫn được giữ nguyên."
            )
            return
        if release is None:
            return
        display_version = str(getattr(release, "display_version", "")).strip()
        notes = _clean_update_notes(str(getattr(release, "notes", "")), limit=420)
        self.donut_status_var.set(f"●  DONUT LOCAL {display_version} CÓ BẢN MỚI")
        if hasattr(self, "donut_button"):
            self.donut_button.configure(style="Secondary.TButton")
        detail = (
            f"Có Donut Local {display_version}. Tool sẽ tự tải gói private đã ký, "
            "kiểm tra chữ ký/SHA-256, đóng Donut Local cũ, cài nguyên tử rồi mở lại "
            "và tự nối MCP."
        )
        if notes:
            detail += f"\n\nThay đổi:\n{notes}"
        if messagebox.askyesno(
            "Có bản Donut Local mới",
            detail + "\n\nCập nhật ngay?",
            parent=self.root,
        ):
            self._install_or_open_donut()

    def _install_or_open_donut(self) -> None:
        if not DONUT_YOUTUBE_ENABLED or self.donut_test_busy or self.donut_manager is None:
            return
        if load_private_github_token() is None:
            self._open_donut_github_login()
            messagebox.showwarning(
                "Chưa đăng nhập GitHub private",
                "GitHub đã được mở. Hãy đăng nhập, chọn riêng kho donutbrowser-local, "
                "tạo mã, dán vào ô rồi bấm Lưu & kiểm tra. Sau đó tool sẽ tự tải, "
                "cài và cập nhật Donut Local.",
                parent=self.donut_settings_dialog or self.root,
            )
            return
        self.donut_test_busy = True
        self.donut_cancel_event.clear()
        self.donut_status_var.set("●  ĐANG CHUẨN BỊ DONUT LOCAL…")
        if hasattr(self, "donut_button"):
            self.donut_button.configure(style="Secondary.TButton")
        self._set_donut_progress(
            0.0,
            (
                "Đang kiểm tra gói Donut Local private mới nhất trên GitHub…"
                if self.donut_manager.installed_app() is not None
                else "Đang tìm gói Donut Local private mới nhất trên GitHub…"
            ),
        )
        if hasattr(self, "donut_test_button"):
            self.donut_test_button.configure(state="disabled")
        if hasattr(self, "donut_manage_button"):
            self.donut_manage_button.configure(state="disabled")
        if hasattr(self, "donut_bridge_install_button"):
            self.donut_bridge_install_button.configure(state="disabled")

        def report(percent: float, detail: str) -> None:
            self._post_update_ui(
                lambda value=percent, message=detail: self._set_donut_progress(value, message)
            )

        def worker() -> None:
            try:
                install, endpoint, info = self.donut_manager.install_launch_and_connect(
                    progress=report,
                    cancel_event=self.donut_cancel_event,
                )
                with DonutMcpClient(
                    endpoint,
                    timeout=35.0,
                    shared_session=False,
                ) as client:
                    groups = client.list_groups()
                    profiles = client.list_profiles()
                server_info = info.get("serverInfo") if isinstance(info, dict) else {}
                version = install.display_version
                self._post_update_ui(
                    lambda: self._apply_managed_donut_success(
                        endpoint,
                        len(groups),
                        len(profiles),
                        sum(
                            profile.browser.casefold() in {"wayfern", "camoufox"}
                            for profile in profiles
                        ),
                        version,
                    )
                )
            except Exception as exc:
                record_exception("donut-managed-install", exc)
                self._post_update_ui(
                    lambda message=str(exc): self._apply_donut_test_error(message, notify=True)
                )

        threading.Thread(target=worker, name="donut-managed-install", daemon=True).start()

    def _apply_managed_donut_success(
        self,
        endpoint: DonutMcpEndpoint,
        group_count: int,
        profile_count: int,
        supported_count: int,
        version: str,
    ) -> None:
        self._refresh_donut_manage_action()
        self.donut_endpoint_var.set(endpoint.value)
        self.donut_progress_var.set(100.0)
        self._apply_donut_test_success(
            group_count,
            profile_count,
            supported_count,
            version,
            notify=True,
        )

    def _test_donut_connection(self, *, notify: bool = True) -> None:
        if not DONUT_YOUTUBE_ENABLED:
            return
        if self.donut_test_busy:
            return
        raw = self.donut_endpoint_var.get().strip()
        if not raw:
            try:
                discovered = DonutManager.discover_endpoint()
                raw = discovered.value
                self.donut_endpoint_var.set(raw)
            except DonutManagedError as exc:
                self._apply_donut_test_error(str(exc), notify=False)
                if notify:
                    messagebox.showwarning(
                        "Donut chưa sẵn sàng",
                        "Chưa tự nhận được MCP Donut. Dùng nút Cài đặt/Cập nhật Donut; "
                        "tool sẽ chuẩn bị và ghép kết nối tự động.\n\n"
                        f"Chi tiết: {exc}",
                        parent=self.donut_settings_dialog or self.root,
                    )
                return
        try:
            endpoint = DonutMcpEndpoint.parse(raw)
        except Exception as exc:
            self._apply_donut_test_error(str(exc), notify=False)
            if notify:
                messagebox.showerror(
                    "URL MCP chưa đúng",
                    str(exc),
                    parent=self.donut_settings_dialog or self.root,
                )
            return
        self.donut_test_busy = True
        self.donut_status_var.set("●  ĐANG TEST DONUT…")
        if hasattr(self, "donut_button"):
            self.donut_button.configure(style="Secondary.TButton")
        self.donut_test_detail_var.set(
            "Đang gọi Donut local và đọc danh sách group/profile thực tế…"
        )
        if hasattr(self, "donut_test_button"):
            self.donut_test_button.configure(state="disabled")
        if hasattr(self, "donut_bridge_install_button"):
            self.donut_bridge_install_button.configure(state="disabled")

        def worker() -> None:
            client = DonutMcpClient(
                endpoint,
                timeout=35.0,
                shared_session=False,
            )
            try:
                info = client.connect()
                DonutManager._require_local_server(info)
                DonutManager._require_local_tools(client.list_tool_names())
                groups = client.list_groups()
                profiles = client.list_profiles()
                save_donut_mcp_endpoint(endpoint.value)
                server_info = info.get("serverInfo") if isinstance(info, dict) else {}
                version = (
                    str(server_info.get("version") or "")
                    if isinstance(server_info, dict)
                    else ""
                )
                self._post_update_ui(
                    lambda: self._apply_donut_test_success(
                        len(groups),
                        len(profiles),
                        sum(
                            profile.browser.casefold() in {"wayfern", "camoufox"}
                            for profile in profiles
                        ),
                        version,
                        notify,
                    )
                )
            except Exception as exc:
                record_exception("donut-mcp-global-test", exc)
                self._post_update_ui(
                    lambda message=str(exc): self._apply_donut_test_error(
                        message, notify=notify
                    )
                )
            finally:
                client.close()

        threading.Thread(target=worker, name="donut-mcp-global-test", daemon=True).start()

    def _apply_donut_test_success(
        self,
        group_count: int,
        profile_count: int,
        supported_count: int,
        version: str,
        notify: bool = True,
    ) -> None:
        self.donut_test_busy = False
        if hasattr(self, "donut_test_button"):
            self.donut_test_button.configure(state="normal")
        if hasattr(self, "donut_manage_button"):
            self.donut_manage_button.configure(state="normal")
        if hasattr(self, "donut_bridge_install_button"):
            self.donut_bridge_install_button.configure(state="normal")
        version_text = f" · Donut Local {version}" if version else ""
        self.donut_status_var.set(f"✓  DONUT LOCAL · {profile_count} PROFILE")
        if hasattr(self, "donut_button"):
            self.donut_button.configure(style="Accent.TButton")
        self.donut_test_detail_var.set(
            f"Kết nối thật thành công{version_text} · {group_count} group · "
            f"{supported_count}/{profile_count} profile Wayfern/Camoufox dùng được. "
            "Khóa đã được mã hóa theo tài khoản Windows."
        )
        if notify:
            messagebox.showinfo(
                "Donut Local MCP hoạt động",
                f"Đã đọc được {group_count} group và {profile_count} profile; "
                f"{supported_count} profile dùng được cho tự động hóa. "
                "Bây giờ mở workspace để chọn đúng profile/kênh.",
                parent=self.donut_settings_dialog or self.root,
            )

    def _apply_donut_test_error(self, message: str, *, notify: bool = True) -> None:
        self.donut_test_busy = False
        self._refresh_donut_manage_action()
        if hasattr(self, "donut_test_button"):
            self.donut_test_button.configure(state="normal")
        if hasattr(self, "donut_manage_button"):
            self.donut_manage_button.configure(state="normal")
        if hasattr(self, "donut_bridge_install_button"):
            self.donut_bridge_install_button.configure(state="normal")
        self.donut_status_var.set("!  DONUT LOCAL CHƯA KẾT NỐI")
        if hasattr(self, "donut_button"):
            self.donut_button.configure(style="Danger.TButton")
        self.donut_test_detail_var.set(
            "Không gọi được Donut MCP local. Dùng nút Cài đặt/Cập nhật Donut để tool "
            f"tự sửa phần cài đặt và ghép lại. Chi tiết: {message}"
        )
        if notify:
            messagebox.showwarning(
                "Chưa có Donut MCP hoạt động",
                "Tool chưa kết nối được Donut Local MCP trên máy.\n\n"
                "Dùng nút Cài đặt/Cập nhật Donut Local để tool tự kiểm tra, cài hoặc cập nhật, "
                "bật MCP và ghép lại. Nếu Donut đang yêu cầu đăng nhập/chấp nhận điều khoản, "
                "hoàn tất một lần rồi bấm Kiểm tra.",
                parent=self.donut_settings_dialog or self.root,
            )

    def _set_update_status(self, text: str, *, enabled: bool = True) -> None:
        self.update_status_var.set(text)
        self.update_button.configure(state="normal" if enabled else "disabled")

    def _ensure_update_toast(self) -> None:
        if self.update_toast is not None:
            return
        toast = tk.Frame(
            self.root,
            bg=PANEL_ALT,
            highlightbackground="#343434",
            highlightcolor="#343434",
            highlightthickness=1,
            bd=0,
            padx=16,
            pady=14,
        )
        self.update_toast = toast
        heading = tk.Frame(toast, bg=PANEL_ALT)
        heading.pack(fill="x")
        tk.Label(
            heading,
            text="✓",
            bg=PANEL_ALT,
            fg=SUCCESS,
            font=("Segoe UI Semibold", 14),
        ).pack(side=LEFT, anchor="n", padx=(0, 10))
        title_block = tk.Frame(heading, bg=PANEL_ALT)
        title_block.pack(side=LEFT, fill="x", expand=True)
        tk.Label(
            title_block,
            textvariable=self.update_toast_title_var,
            bg=PANEL_ALT,
            fg=TEXT,
            font=("Segoe UI Semibold", 10),
            anchor="w",
        ).pack(fill="x")
        tk.Label(
            title_block,
            textvariable=self.update_toast_version_var,
            bg=PANEL_ALT,
            fg=MUTED,
            font=("Segoe UI", 9),
            anchor="w",
        ).pack(fill="x", pady=(3, 0))
        self.update_toast_close = ttk.Button(
            heading,
            text="×",
            width=3,
            style="Secondary.TButton",
            command=self._dismiss_update_toast,
        )
        self.update_toast_close.pack(side=RIGHT, anchor="n", padx=(8, 0))
        tk.Label(
            toast,
            textvariable=self.update_toast_detail_var,
            bg=PANEL_ALT,
            fg=MUTED,
            font=("Segoe UI", 9),
            anchor="w",
            justify=LEFT,
            wraplength=390,
        ).pack(fill="x", pady=(10, 0))
        self.update_toast_progress = ttk.Progressbar(
            toast,
            variable=self.update_toast_progress_var,
            maximum=100.0,
            mode="determinate",
        )
        self.update_toast_actions = tk.Frame(toast, bg=PANEL_ALT)
        self.update_toast_actions.pack(fill="x", pady=(12, 0))
        self.update_toast_primary = ttk.Button(
            self.update_toast_actions,
            text="Khởi động lại ngay",
            style="Smart.TButton",
            command=self._apply_ready_update,
        )
        self.update_toast_secondary = ttk.Button(
            self.update_toast_actions,
            text="Để sau",
            style="Secondary.TButton",
            command=self._dismiss_update_toast,
        )

    def _place_update_toast(self) -> None:
        self._ensure_update_toast()
        assert self.update_toast is not None
        self.update_toast.place(x=18, y=18, width=455)
        self.update_toast.lift()

    def _show_downloading_toast(
        self,
        version: str,
        notes: str,
        size: int,
        *,
        cached: bool = False,
    ) -> None:
        global _DISMISSED_UPDATE_VERSION
        if _DISMISSED_UPDATE_VERSION == version:
            return
        self.update_toast_title_var.set(
            "Đang xác minh bản đã tải…" if cached else "Đang tải bản cập nhật an toàn…"
        )
        self.update_toast_version_var.set(f"{__version__}  →  {version}")
        clean_notes = _clean_update_notes(notes, limit=260)
        size_mb = size / (1024 * 1024)
        detail = (
            f"{size_mb:.0f} MB · dùng lại gói trên máy, không tải lần nữa."
            if cached
            else f"{size_mb:.0f} MB · đang xác minh chữ ký và checksum."
        )
        if clean_notes:
            detail = f"{clean_notes}\n\n{detail}"
        self.update_toast_detail_var.set(detail)
        self.update_toast_progress_var.set(0.0)
        self._place_update_toast()
        self.update_toast_progress.pack(fill="x", pady=(12, 0))
        self.update_toast_actions.pack_forget()

    def _set_update_toast_progress(self, percent: int, *, cached: bool = False) -> None:
        self.update_toast_progress_var.set(float(percent))
        action = "Đang xác minh bản đã tải" if cached else "Đang tải bản cập nhật"
        self.update_toast_title_var.set(f"{action}… {percent}%")

    def _show_ready_update_toast(self) -> None:
        global _DISMISSED_UPDATE_VERSION
        if not self.update_staged_version:
            return
        if _DISMISSED_UPDATE_VERSION == self.update_staged_version:
            return
        self.update_toast_title_var.set(
            "Bản cập nhật sẵn sàng, khởi động lại để áp dụng"
        )
        self.update_toast_version_var.set(
            f"{__version__}  →  {self.update_staged_version}"
        )
        clean_notes = _clean_update_notes(self.update_release_notes)
        rollback = "Đã xác minh Ed25519 + SHA-256 · tự phục hồi nếu bản mới lỗi."
        self.update_toast_detail_var.set(
            f"{clean_notes}\n\n{rollback}" if clean_notes else rollback
        )
        self._place_update_toast()
        self.update_toast_progress.pack_forget()
        self.update_toast_actions.pack(fill="x", pady=(12, 0))
        self.update_toast_secondary.pack(side=RIGHT)
        self.update_toast_primary.pack(side=RIGHT, padx=(0, 8))

    def _dismiss_update_toast(self) -> None:
        global _DISMISSED_UPDATE_VERSION
        if self.update_staged_version:
            _DISMISSED_UPDATE_VERSION = self.update_staged_version
        if self.update_toast is not None:
            self.update_toast.place_forget()

    def _post_update_ui(self, callback: Callable[[], None]) -> None:
        if getattr(self, "_launcher_closed", False) or self.donut_update_check_cancel_event.is_set():
            return
        try:
            self.root.after(0, callback)
        except (tk.TclError, RuntimeError):
            # A background check may finish while the launcher is being
            # replaced/closed. Dropping that stale UI callback is correct and
            # prevents a harmless shutdown race from surfacing as an error.
            pass

    def shutdown_background_tasks(self) -> None:
        """Stop launcher-owned callbacks before the Tk root is destroyed."""

        if getattr(self, "_launcher_closed", False):
            return
        self._launcher_closed = True
        self.donut_update_check_cancel_event.set()
        after_id = self.donut_update_check_after_id
        self.donut_update_check_after_id = None
        if after_id:
            try:
                self.root.after_cancel(after_id)
            except (AttributeError, tk.TclError, RuntimeError):
                pass
        thread = self.donut_update_check_thread
        self.donut_update_check_thread = None
        if (
            thread is not None
            and thread.is_alive()
            and thread is not threading.current_thread()
        ):
            # available_update() is network-bound and has its own request
            # timeout.  A bounded join lets normal close proceed even when a
            # private release endpoint is unavailable; the cancellation flag
            # still prevents any UI callback from being posted afterwards.
            thread.join(timeout=2.0)
        self.donut_update_check_busy = False

    def _start_update_check(self) -> None:
        if self.update_busy:
            return
        if not getattr(sys, "frozen", False):
            self._set_update_status("●  CHẾ ĐỘ PHÁT TRIỂN", enabled=False)
            return
        self.update_busy = True
        self._set_update_status("●  ĐANG KIỂM TRA CẬP NHẬT", enabled=False)
        threading.Thread(target=self._update_check_worker, daemon=True).start()

    def _update_check_worker(self) -> None:
        try:
            release = get_latest_release()
            manifest = verify_release_manifest(release)
            if not update_is_newer(release.version):
                self._post_update_ui(
                    lambda: self._set_update_status("●  ĐÃ Ở BẢN MỚI NHẤT")
                )
                return
            self.update_staged_version = release.version
            self.update_release_notes = release.notes
            self.update_release_size = manifest.asset_size
            reusable = find_reusable_staged_update(release.version, manifest)
            if reusable is not None:
                self._post_update_ui(
                    lambda: self._offer_ready_update(reusable, release.version)
                )
                return
            cached = cached_portable_update_exists(release.version, manifest)
            self._post_update_ui(
                lambda: (
                    self._set_update_status(
                        (
                            f"✓  ĐANG XÁC MINH BẢN {release.version}"
                            if cached
                            else f"↓  ĐANG TẢI BẢN {release.version}"
                        ),
                        enabled=False,
                    ),
                    self._show_downloading_toast(
                        release.version,
                        release.notes,
                        manifest.asset_size,
                        cached=cached,
                    ),
                )
            )

            def progress(received: int, total: int) -> None:
                percent = min(100, int(received * 100 / max(1, total)))
                self._post_update_ui(
                    lambda: (
                        self._set_update_status(
                            (
                                f"✓  ĐANG XÁC MINH {percent}%"
                                if cached
                                else f"↓  ĐANG TẢI {percent}%"
                            ),
                            enabled=False,
                        ),
                        self._set_update_toast_progress(percent, cached=cached),
                    )
                )

            archive = download_portable_update(
                release,
                manifest,
                cancel_event=self.update_cancel_event,
                progress=progress,
            )
            staged = stage_portable_update(archive, release.version, manifest)
            self._post_update_ui(
                lambda: self._offer_ready_update(staged, release.version)
            )
        except UpdateCancelled:
            self._post_update_ui(
                lambda: self._set_update_status("●  ĐÃ DỪNG CẬP NHẬT")
            )
        except Exception as exc:
            record_exception("update-download-worker", exc)
            message = str(exc)
            self._post_update_ui(lambda: self._show_update_error(message))
        finally:
            self.update_busy = False

    def _show_update_error(self, message: str) -> None:
        self._set_update_status("!  KIỂM TRA LẠI CẬP NHẬT")
        messagebox.showwarning(
            "Cập nhật tạm gián đoạn",
            f"Tool vẫn hoạt động bình thường. Chưa thể hoàn tất cập nhật:\n\n{message}",
            parent=self.root,
        )

    def _offer_ready_update(self, staged: Path, version: str) -> None:
        self.update_staged_path = staged
        self.update_staged_version = version
        self._set_update_status(f"↻  CÀI BẢN {version}")
        self._show_ready_update_toast()

    def _update_button_clicked(self) -> None:
        global _DISMISSED_UPDATE_VERSION
        if self.update_busy:
            return
        if self.update_staged_path is not None:
            _DISMISSED_UPDATE_VERSION = ""
            self._show_ready_update_toast()
            return
        self._start_update_check()

    def _apply_ready_update(self) -> None:
        if self.update_staged_path is None or not self.update_staged_version:
            return
        if self.on_apply_update is None:
            self._show_update_error("Bản này chưa có tiến trình thay thế portable.")
            return
        self._set_update_status("↻  ĐANG KHỞI ĐỘNG LẠI", enabled=False)
        try:
            self.on_apply_update(
                self.update_staged_path,
                self.update_staged_version,
            )
        except Exception as exc:
            record_exception("update-apply", exc)
            self._show_update_error(str(exc))

    def _build_create_panel(self, panel: ttk.Frame) -> None:
        heading = ttk.Frame(panel, style="Panel.TFrame")
        heading.pack(fill="x")
        ttk.Label(
            heading,
            text="TẠO WORKSPACE MỚI",
            image=self.icons["sparkle"],
            compound=LEFT,
            style="Section.TLabel",
        ).pack(side=LEFT)
        ttk.Label(
            panel,
            text="Tạo một vùng làm việc độc lập rồi bắt đầu. Tool tự chuẩn bị toàn bộ cấu trúc thư mục cần thiết.",
            style="Muted.TLabel",
            wraplength=340,
            justify=LEFT,
        ).pack(anchor="w", pady=(6, 18))

        ttk.Label(panel, text="TÊN WORKSPACE", style="WorkspaceField.TLabel").pack(
            anchor="w", pady=(0, 5)
        )
        self.name_entry = ttk.Entry(panel, textvariable=self.name_var)
        self.name_entry.pack(fill="x")
        self.name_entry.bind("<Return>", lambda _event: self.create_workspace())

        ttk.Label(panel, text="DẠNG VIDEO", style="WorkspaceField.TLabel").pack(
            anchor="w", pady=(15, 5)
        )
        self.category_combo = ttk.Combobox(
            panel,
            textvariable=self.category_var,
            state="readonly",
            values=(
                "Video YouTube",
                "YouTube Shorts",
                "Tin tức Nhật Bản",
                "Phim tài liệu",
                "Khác",
            ),
            style="Donut.TCombobox",
        )
        self.category_combo.pack(fill="x")
        for sequence in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            self.category_combo.bind(sequence, lambda _event: "break")

        layout_card = ttk.Frame(panel, style="PanelAlt.TFrame", padding=(12, 10))
        layout_card.pack(fill="x", pady=(18, 12))
        ttk.Label(
            layout_card,
            text="●  TỰ TÁCH DỮ LIỆU",
            style="EngineReady.TLabel",
        ).pack(anchor="w")
        ttk.Label(
            layout_card,
            text=(
                "Media  ·  ảnh/video & Flow\n"
                "Output  ·  video và gói nội dung\n"
                "Prompts  ·  master prompt dự án\n"
                "VOICEVOX  ·  WAV, SRT và backup"
            ),
            style="CardMuted.TLabel",
            justify=LEFT,
        ).pack(anchor="w", pady=(7, 0))
        ttk.Separator(layout_card, orient=HORIZONTAL).pack(fill="x", pady=(10, 8))
        ttk.Label(
            layout_card,
            text="KHO WORKSPACE DÙNG CHUNG",
            style="WorkspaceField.TLabel",
        ).pack(anchor="w")
        ttk.Label(
            layout_card,
            textvariable=self.workspace_storage_var,
            style="CardMuted.TLabel",
            wraplength=315,
            justify=LEFT,
        ).pack(anchor="w", pady=(4, 7))
        ttk.Label(
            layout_card,
            text="Có thể đổi và chuyển toàn bộ dữ liệu ở thẻ Hệ thống phía trên; workspace mới sẽ luôn dùng kho này.",
            style="CardMuted.TLabel",
            wraplength=315,
            justify=LEFT,
        ).pack(anchor="w")

        self.create_button = ttk.Button(
            panel,
            text="TẠO WORKSPACE & BẮT ĐẦU",
            image=self.icons["play_dark"],
            compound=LEFT,
            style="Smart.TButton",
            command=self.create_workspace,
        )
        self.create_button.pack(fill="x", pady=(2, 0))
        ttk.Label(
            panel,
            text="Các tác vụ trong workspace khác không bị ghi đè hoặc trộn lịch sử.",
            style="Muted.TLabel",
            wraplength=340,
            justify=LEFT,
        ).pack(anchor="w", pady=(9, 0))

    def _build_workspace_library(self, panel: ttk.Frame) -> None:
        ttk.Label(
            panel,
            text="CÂY WORKSPACE",
            image=self.icons["folder"],
            compound=LEFT,
            style="Section.TLabel",
        ).pack(anchor="w")
        ttk.Label(
            panel,
            text="Chọn một nhánh để lọc danh sách ở giữa.",
            style="Muted.TLabel",
        ).pack(anchor="w", pady=(3, 9))

        tree_box = ttk.Frame(panel, style="PanelAlt.TFrame", padding=(6, 6))
        tree_box.pack(fill=BOTH, expand=True)
        self.folder_tree = ttk.Treeview(
            tree_box,
            show="tree",
            selectmode="browse",
            style="Workspace.Treeview",
            height=4,
        )
        folder_scroll = ttk.Scrollbar(
            tree_box,
            orient=VERTICAL,
            command=self.folder_tree.yview,
            style="Vertical.TScrollbar",
        )
        self.folder_tree.configure(yscrollcommand=folder_scroll.set)
        self.folder_tree.pack(side=LEFT, fill=BOTH, expand=True)
        folder_scroll.pack(side=RIGHT, fill="y")
        self.folder_tree.bind("<<TreeviewSelect>>", self._on_folder_tree_selected)
        self.folder_tree.bind("<Return>", self._on_folder_tree_selected)
        self.folder_tree.bind("<Double-Button-1>", self._toggle_selected_folder)

        folder_actions = ttk.Frame(panel, style="Panel.TFrame")
        folder_actions.pack(fill="x", pady=(8, 0))
        self.folder_rename_button = ttk.Button(
            folder_actions,
            text="Đổi tên",
            image=self.icons["edit"],
            compound=LEFT,
            style="Secondary.TButton",
            command=self._rename_selected_workspace_folder,
        )
        self.folder_rename_button.pack(side=LEFT, fill="x", expand=True)
        self.folder_delete_button = ttk.Button(
            folder_actions,
            text="Xóa",
            image=self.icons["trash_dark"],
            compound=LEFT,
            style="Danger.TButton",
            command=self._delete_selected_workspace_folder,
        )
        self.folder_delete_button.pack(side=LEFT, fill="x", expand=True, padx=(6, 0))

        create_box = ttk.Frame(panel, style="PanelAlt.TFrame", padding=(9, 8))
        create_box.pack(fill="x", pady=(9, 0))
        self.folder_parent_hint_var = StringVar(master=self.root, value="Tạo ở cấp gốc")
        ttk.Label(
            create_box,
            textvariable=self.folder_parent_hint_var,
            style="WorkspaceCardMeta.TLabel",
        ).pack(anchor="w", pady=(0, 5))
        folder_input = ttk.Frame(create_box, style="PanelAlt.TFrame")
        folder_input.pack(fill="x")
        self.folder_name_entry = ttk.Entry(
            folder_input,
            textvariable=self.folder_name_var,
        )
        self.folder_name_entry.pack(side=LEFT, fill="x", expand=True)
        self.folder_name_entry.bind(
            "<Return>", lambda _event: self._create_workspace_folder()
        )
        ttk.Button(
            folder_input,
            text="Tạo",
            image=self.icons["check_dark"],
            compound=LEFT,
            style="Accent.TButton",
            command=self._create_workspace_folder,
        ).pack(side=LEFT, padx=(6, 0))

        move_box = ttk.Frame(panel, style="PanelAlt.TFrame", padding=(9, 8))
        move_box.pack(fill="x", pady=(9, 0))
        ttk.Label(
            move_box,
            text="CHUYỂN CÁC WORKSPACE ĐÃ CHỌN",
            style="WorkspaceCardMeta.TLabel",
        ).pack(anchor="w", pady=(0, 5))
        self.folder_move_combo = ttk.Combobox(
            move_box,
            textvariable=self.folder_move_var,
            state="readonly",
            values=("Chưa phân loại",),
            style="Donut.TCombobox",
        )
        self.folder_move_combo.pack(fill="x")
        self.folder_move_button = ttk.Button(
            move_box,
            text="CHUYỂN ĐẾN THƯ MỤC",
            image=self.icons["folder"],
            compound=LEFT,
            style="Secondary.TButton",
            command=self._move_selected_with_picker,
        )
        self.folder_move_button.pack(fill="x", pady=(6, 0))

    def _build_realtime_panel(self, panel: ttk.Frame) -> None:
        ttk.Label(
            panel,
            text="OUTPUT REALTIME",
            image=self.icons["film"],
            compound=LEFT,
            style="Section.TLabel",
        ).pack(anchor="w")
        ttk.Label(
            panel,
            textvariable=self.live_summary_var,
            style="Muted.TLabel",
            wraplength=260,
            justify=LEFT,
        ).pack(anchor="w", pady=(3, 9))

        workspace_queue_card = ttk.Frame(
            panel,
            style="PanelAlt.TFrame",
            padding=(10, 9),
        )
        workspace_queue_card.pack(fill="x", pady=(0, 9))
        ttk.Label(
            workspace_queue_card,
            text="ĐIỀU PHỐI WORKSPACE",
            image=self.icons["queue"],
            compound=LEFT,
            style="WorkspaceLiveTitle.TLabel",
        ).pack(anchor="w")
        ttk.Label(
            workspace_queue_card,
            textvariable=self.workspace_queue_summary_var,
            style="WorkspaceCardRunning.TLabel",
            wraplength=max(210, round(245 * self.ui_scale)),
            justify=LEFT,
        ).pack(anchor="w", pady=(5, 6))
        selection_row = ttk.Frame(workspace_queue_card, style="PanelAlt.TFrame")
        selection_row.pack(fill="x", pady=(0, 5))
        ttk.Button(
            selection_row,
            text="Chọn tất cả",
            style="Secondary.TButton",
            command=self._select_all_queue_workspaces,
        ).pack(side=LEFT, fill="x", expand=True)
        ttk.Button(
            selection_row,
            text="Bỏ chọn",
            style="Secondary.TButton",
            command=self._clear_queue_workspace_selection,
        ).pack(side=LEFT, fill="x", expand=True, padx=(6, 0))
        action_row = ttk.Frame(workspace_queue_card, style="PanelAlt.TFrame")
        action_row.pack(fill="x", pady=(0, 6))
        ttk.Button(
            action_row,
            text="Chạy toàn bộ",
            style="Smart.TButton",
            command=self._bulk_run_selected_workspaces,
        ).pack(side=LEFT, fill="x", expand=True)
        ttk.Button(
            action_row,
            text="Tiếp tục",
            style="Smart.TButton",
            command=self._bulk_continue_selected_workspaces,
        ).pack(side=LEFT, fill="x", expand=True)
        skip_row = ttk.Frame(workspace_queue_card, style="PanelAlt.TFrame")
        skip_row.pack(fill="x", pady=(0, 6))
        ttk.Button(
            skip_row,
            text="Skip phiên",
            style="Danger.TButton",
            command=self._bulk_skip_selected_workspaces,
        ).pack(side=LEFT, fill="x", expand=True)
        ttk.Button(
            skip_row,
            text="Dừng",
            style="Secondary.TButton",
            command=self._bulk_stop_selected_workspaces,
        ).pack(side=LEFT, fill="x", expand=True, padx=(6, 0))
        ttk.Label(
            workspace_queue_card,
            textvariable=self.workspace_queue_selection_var,
            style="WorkspaceCardMeta.TLabel",
            wraplength=max(210, round(245 * self.ui_scale)),
            justify=LEFT,
        ).pack(anchor="w", pady=(0, 5))
        queue_tree_box = ttk.Frame(workspace_queue_card, style="PanelAlt.TFrame")
        queue_tree_box.pack(fill="x")
        self.workspace_queue_tree = ttk.Treeview(
            queue_tree_box,
            columns=("workspace", "state", "detail"),
            show="headings",
            height=7,
            selectmode="extended",
            style="WorkspaceQueue.Treeview",
        )
        self.workspace_queue_tree.heading("workspace", text="Workspace")
        self.workspace_queue_tree.heading("state", text="Trạng thái")
        self.workspace_queue_tree.heading("detail", text="Công đoạn / chờ")
        self.workspace_queue_tree.column(
            "workspace", width=max(82, round(93 * self.ui_scale)),
            minwidth=70, stretch=False,
        )
        self.workspace_queue_tree.column(
            "state", width=max(76, round(84 * self.ui_scale)),
            minwidth=66, stretch=False,
        )
        self.workspace_queue_tree.column(
            "detail", width=max(110, round(145 * self.ui_scale)),
            minwidth=90, stretch=True,
        )
        queue_tree_scroll = ttk.Scrollbar(
            queue_tree_box,
            orient=VERTICAL,
            command=self.workspace_queue_tree.yview,
            style="Vertical.TScrollbar",
        )
        self.workspace_queue_tree.configure(yscrollcommand=queue_tree_scroll.set)
        self.workspace_queue_tree.pack(side=LEFT, fill="x", expand=True)
        queue_tree_scroll.pack(side=RIGHT, fill="y")
        self.workspace_queue_tree.tag_configure("running", foreground="#5db7ff")
        self.workspace_queue_tree.tag_configure("queued", foreground=WARNING)
        self.workspace_queue_tree.tag_configure("attention", foreground="#ff6b62")
        self.workspace_queue_tree.tag_configure("completed", foreground=SUCCESS)
        self.workspace_queue_tree.tag_configure("idle", foreground=MUTED)
        self.workspace_queue_tree.bind(
            "<Double-Button-1>", self._open_selected_queue_workspace
        )
        self.workspace_queue_tree.bind(
            "<<TreeviewSelect>>",
            lambda _event: self._refresh_workspace_queue_selection_count(),
        )
        ttk.Label(
            workspace_queue_card,
            text="Lệnh áp dụng cho workspace đã chọn · nhấp đúp dòng để mở.",
            style="WorkspaceCardMeta.TLabel",
            wraplength=max(210, round(245 * self.ui_scale)),
            justify=LEFT,
        ).pack(anchor="w", pady=(4, 0))

        resource_card = ttk.Frame(
            panel,
            style="PanelAlt.TFrame",
            padding=(10, 7),
        )
        resource_card.pack(fill="x", pady=(0, 8))
        ttk.Label(
            resource_card,
            text="ĐIỀU PHỐI TÀI NGUYÊN TOÀN TOOL",
            image=self.icons["queue"],
            compound=LEFT,
            style="WorkspaceLiveTitle.TLabel",
        ).pack(anchor="w")
        render_limit_row = ttk.Frame(resource_card, style="PanelAlt.TFrame")
        render_limit_row.pack(fill="x", pady=(6, 0))
        ttk.Label(
            render_limit_row,
            text="Render workspace",
            style="WorkspaceCardMeta.TLabel",
        ).pack(side=LEFT)
        self.global_render_limit_combo = ttk.Combobox(
            render_limit_row,
            textvariable=self.global_render_limit_var,
            values=tuple(str(value) for value in range(1, GLOBAL_RENDER_LIMIT_MAX + 1)),
            state="readonly",
            width=4,
            style="Donut.TCombobox",
        )
        self.global_render_limit_combo.pack(side=LEFT, padx=(6, 5))
        self.global_render_limit_combo.bind(
            "<<ComboboxSelected>>",
            self._global_render_limit_changed,
        )
        ttk.Label(
            render_limit_row,
            text="video đồng thời",
            style="WorkspaceCardMeta.TLabel",
        ).pack(side=LEFT)
        chatgpt_limit_row = ttk.Frame(resource_card, style="PanelAlt.TFrame")
        chatgpt_limit_row.pack(fill="x", pady=(5, 0))
        ttk.Label(
            chatgpt_limit_row,
            text="ChatGPT phiên",
            style="WorkspaceCardMeta.TLabel",
        ).pack(side=LEFT)
        self.chatgpt_parallel_combo = ttk.Combobox(
            chatgpt_limit_row,
            textvariable=self.chatgpt_parallel_var,
            values=tuple(str(value) for value in range(1, GLOBAL_CHATGPT_LIMIT_MAX + 1)),
            state="readonly",
            width=4,
            style="Donut.TCombobox",
        )
        self.chatgpt_parallel_combo.pack(side=LEFT, padx=(6, 5))
        self.chatgpt_parallel_combo.bind(
            "<<ComboboxSelected>>",
            self._global_chatgpt_limit_changed,
        )
        ttk.Label(
            chatgpt_limit_row,
            text="workspace song song",
            style="WorkspaceCardMeta.TLabel",
        ).pack(side=LEFT)
        ttk.Label(
            resource_card,
            textvariable=self.global_render_queue_var,
            style="WorkspaceCardRunning.TLabel",
            wraplength=max(210, round(245 * self.ui_scale)),
            justify=LEFT,
        ).pack(anchor="w", pady=(6, 0))
        ttk.Label(
            resource_card,
            textvariable=self.chatgpt_queue_var,
            style="WorkspaceCardRunning.TLabel",
            wraplength=max(210, round(245 * self.ui_scale)),
            justify=LEFT,
        ).pack(anchor="w", pady=(3, 0))
        ttk.Label(
            resource_card,
            text="Giới hạn là số chạy đồng thời; 1 = tuần tự.",
            style="WorkspaceCardMeta.TLabel",
            wraplength=max(210, round(245 * self.ui_scale)),
            justify=LEFT,
        ).pack(anchor="w", pady=(4, 0))

        ttk.Separator(panel, orient=HORIZONTAL).pack(fill="x", pady=(0, 8))

        live_box = ttk.Frame(panel, style="Panel.TFrame")
        live_box.pack(fill=BOTH, expand=True)
        self.live_canvas = tk.Canvas(
            live_box,
            bg=PANEL,
            bd=0,
            highlightthickness=0,
            yscrollincrement=20,
            width=1,
        )
        live_scroll = ttk.Scrollbar(
            live_box,
            orient=VERTICAL,
            command=self.live_canvas.yview,
            style="Vertical.TScrollbar",
        )
        self.live_canvas.configure(yscrollcommand=live_scroll.set)
        self.live_frame = ttk.Frame(self.live_canvas, style="Panel.TFrame")
        self.live_window = self.live_canvas.create_window(
            (0, 0),
            window=self.live_frame,
            anchor="nw",
        )
        self.live_frame.bind(
            "<Configure>",
            lambda _event: self.live_canvas.configure(
                scrollregion=self.live_canvas.bbox("all")
            ),
        )
        self.live_canvas.bind(
            "<Configure>",
            lambda event: self.live_canvas.itemconfigure(
                self.live_window,
                width=max(1, int(event.width)),
            ),
        )
        for widget in (self.live_canvas, self.live_frame):
            for sequence in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
                widget.bind(sequence, self._scroll_live_outputs, add="+")
        self.live_canvas.pack(side=LEFT, fill=BOTH, expand=True)
        live_scroll.pack(side=RIGHT, fill="y")

    def _render_queue_snapshot(self) -> dict[str, int]:
        value: dict[str, int] = {}
        if self.render_queue_provider is not None:
            try:
                candidate = self.render_queue_provider()
                value = candidate if isinstance(candidate, dict) else {}
            except Exception:
                value = {}
        try:
            limit = clamp_global_render_limit(value.get("limit", 1))
        except (TypeError, ValueError):
            limit = 1
        try:
            active = max(0, int(value.get("active", 0)))
        except (TypeError, ValueError):
            active = 0
        try:
            waiting = max(0, int(value.get("waiting", 0)))
        except (TypeError, ValueError):
            waiting = 0
        return {"limit": limit, "active": active, "waiting": waiting}

    def _refresh_render_queue_status(self) -> None:
        snapshot = self._render_queue_snapshot()
        limit = snapshot["limit"]
        active = snapshot["active"]
        waiting = snapshot["waiting"]
        if self.global_render_limit_var.get() != str(limit):
            self.global_render_limit_var.set(str(limit))
        if active > limit:
            detail = (
                f"Đang hoàn tất {active} video · mức mới {limit} "
                f"· {waiting} video chờ"
            )
        elif waiting:
            detail = f"Đang render {active}/{limit} · {waiting} video đang xếp hàng"
        elif active:
            detail = f"Đang render {active}/{limit} · chưa có video chờ"
        else:
            detail = f"Sẵn sàng · tối đa {limit} video cùng lúc"
        if self.global_render_error:
            detail = f"{self.global_render_error}\n{detail}"
        if self.global_render_queue_var.get() != detail:
            self.global_render_queue_var.set(detail)

    def _global_render_limit_changed(self, _event: object | None = None) -> None:
        try:
            requested = clamp_global_render_limit(self.global_render_limit_var.get())
        except (TypeError, ValueError, tk.TclError):
            requested = self._render_queue_snapshot()["limit"]
        try:
            actual = (
                self.render_limit_handler(requested)
                if self.render_limit_handler is not None
                else requested
            )
            actual = clamp_global_render_limit(actual)
            self.global_render_error = ""
            self.global_render_limit_var.set(str(actual))
            self.selection_hint_var.set(
                f"Đã đặt tối đa {actual} video render đồng thời trên toàn bộ DocuRandom."
            )
        except Exception as exc:
            record_exception("global-render-limit-change", exc)
            self.global_render_error = (
                "Chưa lưu được giới hạn. Kiểm tra quyền ghi thư mục Data rồi chọn lại; "
                "mức cũ vẫn được giữ."
            )
        self._refresh_render_queue_status()

    def _chatgpt_queue_snapshot(self) -> dict[str, int | bool]:
        value: dict[str, object] = {}
        if self.chatgpt_queue_provider is not None:
            try:
                candidate = self.chatgpt_queue_provider()
                value = candidate if isinstance(candidate, dict) else {}
            except Exception:
                value = {}
        serial = bool(value.get("serial", True))
        try:
            limit = max(0, int(value.get("limit", 1)))
        except (TypeError, ValueError):
            limit = 1
        try:
            recommended = clamp_global_chatgpt_limit(
                value.get("recommended_limit", 1)
            )
        except (TypeError, ValueError):
            recommended = 1
        try:
            active = max(0, int(value.get("active", 0)))
        except (TypeError, ValueError):
            active = 0
        try:
            waiting = max(0, int(value.get("waiting", 0)))
        except (TypeError, ValueError):
            waiting = 0
        raw_configured = value.get("configured_limit")
        try:
            configured = clamp_global_chatgpt_limit(
                raw_configured
                if raw_configured is not None
                else (1 if serial else max(1, limit or recommended))
            )
        except (TypeError, ValueError):
            configured = 1 if serial else max(1, limit or recommended)
        return {
            "serial": serial,
            "limit": limit,
            "configured_limit": configured,
            "recommended_limit": recommended,
            "active": active,
            "waiting": waiting,
            "rate_limited": bool(value.get("rate_limited", False)),
        }

    def _refresh_chatgpt_queue_status(self) -> None:
        snapshot = self._chatgpt_queue_snapshot()
        serial = bool(snapshot["serial"])
        active = int(snapshot["active"])
        waiting = int(snapshot["waiting"])
        configured = int(snapshot["configured_limit"])
        recommended = int(snapshot["recommended_limit"])
        if self.chatgpt_parallel_var.get() != str(configured):
            self.chatgpt_parallel_var.set(str(configured))
        if self.chatgpt_serial_var.get() != serial:
            self.chatgpt_serial_var.set(serial)
        if bool(snapshot["rate_limited"]):
            detail = f"Đang nghỉ do giới hạn ChatGPT · {waiting} lượt giữ trong hàng"
        elif waiting:
            detail = (
                f"Đang chạy {active}/{configured} · {waiting} lượt ChatGPT đang chờ"
            )
        elif active:
            detail = f"Đang chạy {active}/{configured} lượt ChatGPT"
        else:
            detail = f"Sẵn sàng · tối đa {configured} phiên ChatGPT song song"
        if self.chatgpt_queue_error:
            detail = f"{self.chatgpt_queue_error}\n{detail}"
        if self.chatgpt_queue_var.get() != detail:
            self.chatgpt_queue_var.set(detail)

    def _chatgpt_serial_changed(self) -> None:
        requested = bool(self.chatgpt_serial_var.get())
        try:
            if self.chatgpt_limit_handler is not None:
                snapshot = self._chatgpt_queue_snapshot()
                actual_limit = self.chatgpt_limit_handler(
                    1 if requested else int(snapshot["recommended_limit"])
                )
                actual_limit = clamp_global_chatgpt_limit(actual_limit)
                actual = actual_limit == 1
            else:
                actual = (
                    self.chatgpt_serial_handler(requested)
                    if self.chatgpt_serial_handler is not None
                    else requested
                )
                actual = bool(actual)
            self.chatgpt_queue_error = ""
            self.chatgpt_serial_var.set(actual)
            self.chatgpt_parallel_var.set(
                "1" if actual else str(self._chatgpt_queue_snapshot()["configured_limit"])
            )
            self.selection_hint_var.set(
                "ChatGPT sẽ xếp hàng từng workspace; lượt đang chạy không bị hủy."
                if actual
                else "Đã cho phép các workspace dùng ChatGPT song song theo cấu hình máy."
            )
        except Exception as exc:
            record_exception("global-chatgpt-queue-change", exc)
            self.chatgpt_queue_error = (
                "Chưa lưu được chế độ ChatGPT. Kiểm tra quyền ghi thư mục Data rồi thử lại."
            )
        self._refresh_chatgpt_queue_status()

    def _global_chatgpt_limit_changed(self, _event: object | None = None) -> None:
        """Apply the exact number of workspaces allowed in ChatGPT at once."""

        try:
            requested = clamp_global_chatgpt_limit(self.chatgpt_parallel_var.get())
        except (TypeError, ValueError, tk.TclError):
            requested = int(self._chatgpt_queue_snapshot()["configured_limit"])
        try:
            if self.chatgpt_limit_handler is not None:
                actual = clamp_global_chatgpt_limit(
                    self.chatgpt_limit_handler(requested)
                )
            elif self.chatgpt_serial_handler is not None:
                # Compatibility with an older embedding that only exposes the
                # boolean callback. New app builds always use the numeric one.
                serial = bool(self.chatgpt_serial_handler(requested == 1))
                actual = 1 if serial else requested
            else:
                actual = requested
            self.chatgpt_queue_error = ""
            self.chatgpt_parallel_var.set(str(actual))
            self.chatgpt_serial_var.set(actual == 1)
            self.selection_hint_var.set(
                f"Đã đặt {actual} phiên ChatGPT song song trên toàn bộ DocuRandom."
            )
        except Exception as exc:
            record_exception("global-chatgpt-limit-change", exc)
            self.chatgpt_queue_error = (
                "Chưa lưu được số phiên ChatGPT. Kiểm tra quyền ghi thư mục Data rồi chọn lại; "
                "mức cũ vẫn được giữ."
            )
        self._refresh_chatgpt_queue_status()

    def _build_workspace_browser(self, panel: ttk.Frame) -> None:
        header = ttk.Frame(panel, style="Panel.TFrame")
        header.pack(fill="x", pady=(0, 10))
        title_block = ttk.Frame(header, style="Panel.TFrame")
        title_block.pack(side=LEFT, fill="x", expand=True)
        ttk.Label(
            title_block,
            text="DANH SÁCH WORKSPACE",
            image=self.icons["queue"],
            compound=LEFT,
            style="Section.TLabel",
        ).pack(anchor="w")
        ttk.Label(
            title_block,
            textvariable=self.workspace_count_var,
            style="Muted.TLabel",
        ).pack(anchor="w", pady=(3, 0))
        self.refresh_button = ttk.Button(
            header,
            text="Làm mới",
            image=self.icons["refresh"],
            compound=LEFT,
            style="Secondary.TButton",
            command=self.refresh_async,
        )
        self.refresh_button.pack(side=RIGHT)

        tools = ttk.Frame(panel, style="Panel.TFrame")
        tools.pack(fill="x", pady=(0, 10))
        search_frame = ttk.Frame(tools, style="PanelAlt.TFrame", padding=(8, 5))
        search_frame.pack(fill="x")
        ttk.Label(
            search_frame,
            image=self.icons["sliders"],
            background=PANEL_ALT,
        ).pack(side=LEFT, padx=(0, 6))
        self.workspace_search_entry = ttk.Entry(
            search_frame,
            textvariable=self.workspace_search_var,
        )
        self.workspace_search_entry.pack(side=LEFT, fill="x", expand=True)
        self.workspace_search_entry.bind(
            "<KeyRelease>", self._schedule_workspace_refresh
        )
        filter_row = ttk.Frame(tools, style="Panel.TFrame")
        filter_row.pack(fill="x", pady=(7, 0))
        self.workspace_filter_combo = ttk.Combobox(
            filter_row,
            textvariable=self.workspace_filter_var,
            values=WORKSPACE_FILTERS,
            state="readonly",
            style="Donut.TCombobox",
        )
        self.workspace_filter_combo.pack(side=LEFT, fill="x", expand=True)
        self.workspace_filter_combo.bind(
            "<<ComboboxSelected>>", lambda _event: self._refresh_workspace_view(True)
        )
        self.workspace_sort_combo = ttk.Combobox(
            filter_row,
            textvariable=self.workspace_sort_var,
            values=WORKSPACE_SORTS,
            state="readonly",
            style="Donut.TCombobox",
        )
        self.workspace_sort_combo.pack(side=LEFT, fill="x", expand=True, padx=(7, 0))
        self.workspace_sort_combo.bind(
            "<<ComboboxSelected>>", lambda _event: self._refresh_workspace_view(True)
        )

        scope_bar = ttk.Frame(panel, style="Panel.TFrame")
        scope_bar.pack(fill="x", pady=(0, 9))
        scope_title = ttk.Frame(scope_bar, style="Panel.TFrame")
        scope_title.pack(side=LEFT, fill="x", expand=True)
        ttk.Label(
            scope_title,
            textvariable=self.current_folder_var,
            image=self.icons["folder"],
            compound=LEFT,
            style="WorkspaceScope.TLabel",
        ).pack(anchor="w")
        ttk.Label(
            scope_title,
            textvariable=self.current_folder_detail_var,
            style="Muted.TLabel",
        ).pack(anchor="w", pady=(2, 0))
        scope_actions = ttk.Frame(scope_bar, style="Panel.TFrame")
        scope_actions.pack(side=RIGHT)
        ttk.Button(
            scope_actions,
            text="Chọn tất cả",
            image=self.icons["check"],
            compound=LEFT,
            style="Secondary.TButton",
            command=self._select_all_visible_workspaces,
        ).pack(side=LEFT)
        ttk.Button(
            scope_actions,
            text="Chạy toàn bộ",
            style="Smart.TButton",
            command=self._bulk_run_visible_workspaces,
        ).pack(side=LEFT, padx=(6, 0))
        ttk.Button(
            scope_actions,
            text="Skip phiên",
            style="Danger.TButton",
            command=self._bulk_skip_visible_workspaces,
        ).pack(side=LEFT, padx=(6, 0))

        ttk.Separator(panel, orient=HORIZONTAL).pack(fill="x", pady=(0, 10))

        cards_container = ttk.Frame(panel, style="Panel.TFrame")
        cards_container.pack(fill=BOTH, expand=True)
        self.cards_canvas = tk.Canvas(
            cards_container,
            bg=PANEL,
            bd=0,
            highlightthickness=0,
            width=1,
        )
        self.cards_canvas.configure(yscrollincrement=20)
        self.cards_scrollbar = ttk.Scrollbar(
            cards_container,
            orient=VERTICAL,
            command=self.cards_canvas.yview,
            style="Vertical.TScrollbar",
        )
        self.cards_canvas.configure(yscrollcommand=self.cards_scrollbar.set)
        self.cards_frame = ttk.Frame(self.cards_canvas, style="Panel.TFrame")
        self.cards_window = self.cards_canvas.create_window(
            (0, 0), window=self.cards_frame, anchor="nw"
        )
        self.cards_frame.bind(
            "<Configure>",
            self._schedule_cards_scrollregion,
        )
        self.cards_canvas.bind(
            "<Configure>",
            self._schedule_cards_canvas_layout,
        )
        self.cards_canvas.pack(side=LEFT, fill=BOTH, expand=True)
        self.cards_scrollbar.pack(side=RIGHT, fill="y")
        self._bind_card_scroll(self.cards_canvas)
        self._bind_card_scroll(self.cards_frame)

        footer = ttk.Frame(panel, style="Panel.TFrame")
        footer.pack(fill="x", pady=(10, 0))
        ttk.Label(
            footer,
            textvariable=self.selection_hint_var,
            image=self.icons["check"],
            compound=LEFT,
            style="Muted.TLabel",
        ).pack(side=LEFT)

    def _schedule_workspace_refresh(self, _event: object | None = None) -> None:
        pending = self.workspace_refresh_after_id
        if pending:
            try:
                self.root.after_cancel(pending)
            except tk.TclError:
                pass
        self.reset_cards_scroll = True
        self.workspace_refresh_after_id = self.root.after(180, self.refresh_async)

    def _refresh_workspace_view(self, reset_scroll: bool = False) -> None:
        self.reset_cards_scroll = bool(reset_scroll)
        self.refresh_async()

    @staticmethod
    def _updated_label(value: str) -> str:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed.astimezone().strftime("%d/%m/%Y · %H:%M")
        except (TypeError, ValueError):
            return value.replace("T", " ")[:16]

    @staticmethod
    def _mousewheel_units(event: tk.Event) -> int:
        try:
            button = int(getattr(event, "num", 0) or 0)
        except (TypeError, ValueError):
            button = 0
        if button == 4:
            return -1
        if button == 5:
            return 1
        try:
            delta = int(getattr(event, "delta", 0) or 0)
        except (TypeError, ValueError):
            delta = 0
        if not delta:
            return 0
        return (-1 if delta > 0 else 1) * max(1, round(abs(delta) / 120))

    def _scroll_cards(self, event: tk.Event) -> str:
        units = self._mousewheel_units(event)
        if units:
            try:
                self.cards_canvas.yview_scroll(units, "units")
            except tk.TclError:
                pass
        return "break"

    def _scroll_create_panel(self, event: tk.Event) -> str:
        units = self._mousewheel_units(event)
        if units:
            try:
                self.create_canvas.yview_scroll(units, "units")
            except tk.TclError:
                pass
        return "break"

    def _bind_create_scroll(self, parent: tk.Misc) -> None:
        if not isinstance(parent, (tk.Text, ttk.Combobox, ttk.Scrollbar)):
            for sequence in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
                parent.bind(sequence, self._scroll_create_panel, add="+")
        for child in parent.winfo_children():
            self._bind_create_scroll(child)

    def _schedule_cards_scrollregion(self, _event: tk.Event | None = None) -> None:
        if self.cards_scroll_after_id is not None:
            return

        def apply() -> None:
            self.cards_scroll_after_id = None
            try:
                self.cards_canvas.configure(
                    scrollregion=self.cards_canvas.bbox("all")
                )
            except tk.TclError:
                pass

        self.cards_scroll_after_id = self.root.after(16, apply)

    def _schedule_cards_canvas_layout(self, event: tk.Event) -> None:
        self.cards_pending_width = max(1, int(event.width))
        if self.cards_layout_after_id is not None:
            return

        def apply() -> None:
            self.cards_layout_after_id = None
            width = self.cards_pending_width
            if width == self.cards_applied_width:
                return
            self.cards_applied_width = width
            try:
                self.cards_canvas.itemconfigure(self.cards_window, width=width)
            except tk.TclError:
                return
            self._schedule_cards_scrollregion()

        self.cards_layout_after_id = self.root.after(16, apply)

    def _bind_card_scroll(self, parent: tk.Misc) -> None:
        if not isinstance(parent, (tk.Text, ttk.Scrollbar)):
            for sequence in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
                parent.bind(sequence, self._scroll_cards, add="+")
        for child in parent.winfo_children():
            self._bind_card_scroll(child)

    def _scroll_live_outputs(self, event: tk.Event) -> str:
        units = self._mousewheel_units(event)
        if units:
            try:
                self.live_canvas.yview_scroll(units, "units")
            except tk.TclError:
                pass
        return "break"

    @staticmethod
    def _folder_breadcrumb(
        folder_id: str,
        folder_map: dict[str, WorkspaceFolder],
    ) -> str:
        names: list[str] = []
        visited: set[str] = set()
        current = folder_map.get(folder_id)
        while current is not None and current.folder_id not in visited:
            visited.add(current.folder_id)
            names.append(current.name)
            current = folder_map.get(current.parent_id)
        names.reverse()
        return "  /  ".join(names)

    def _refresh_folder_tree(
        self,
        folders: list[WorkspaceFolder],
        workspaces: list[Workspace],
    ) -> None:
        self.refreshing_folder_tree = True
        tree = self.folder_tree
        first_folder_render = not self.folder_tree_items
        open_folder_ids = {
            folder_id
            for folder_id, item_id in self.folder_tree_items.items()
            if tree.exists(item_id) and bool(tree.item(item_id, "open"))
        }
        existing_items = tree.get_children("")
        if existing_items:
            tree.delete(*existing_items)
        self.folder_tree_items.clear()
        self.folder_by_id = {folder.folder_id: folder for folder in folders}
        direct_counts: dict[str, int] = {folder.folder_id: 0 for folder in folders}
        ungrouped_count = 0
        for workspace in workspaces:
            if workspace.folder_id in direct_counts:
                direct_counts[workspace.folder_id] += 1
            else:
                ungrouped_count += 1

        tree.insert(
            "",
            "end",
            iid=ALL_FOLDER_ID,
            text=f"  Tất cả workspace  ({len(workspaces)})",
            open=True,
        )
        tree.insert(
            "",
            "end",
            iid=UNGROUPED_TREE_ID,
            text=f"  Chưa phân loại  ({ungrouped_count})",
        )
        children: dict[str, list[WorkspaceFolder]] = {}
        for folder in folders:
            parent_id = folder.parent_id if folder.parent_id in self.folder_by_id else ""
            children.setdefault(parent_id, []).append(folder)
        for values in children.values():
            values.sort(key=lambda item: (item.name.casefold(), item.created_at))

        inserted: set[str] = set()

        def add_branch(parent_item: str, parent_id: str) -> None:
            for folder in children.get(parent_id, ()):  # pragma: no branch - tiny tree
                if folder.folder_id in inserted:
                    continue
                inserted.add(folder.folder_id)
                tree.insert(
                    parent_item,
                    "end",
                    iid=folder.folder_id,
                    text=f"  {folder.name}  ({direct_counts.get(folder.folder_id, 0)})",
                    open=(
                        folder.folder_id in open_folder_ids
                        or first_folder_render
                    ),
                )
                self.folder_tree_items[folder.folder_id] = folder.folder_id
                add_branch(folder.folder_id, folder.folder_id)

        add_branch("", "")
        # Broken/cyclic legacy parent links remain visible instead of disappearing.
        for folder in folders:
            if folder.folder_id not in inserted:
                tree.insert(
                    "",
                    "end",
                    iid=folder.folder_id,
                    text=f"  {folder.name}  ({direct_counts.get(folder.folder_id, 0)})",
                )
                self.folder_tree_items[folder.folder_id] = folder.folder_id

        self.folder_drop_targets = {"": tree}
        self.folder_drop_targets.update({folder.folder_id: tree for folder in folders})
        destination_pairs = [("Chưa phân loại", "")]
        destination_pairs.extend(
            (
                self._folder_breadcrumb(folder.folder_id, self.folder_by_id),
                folder.folder_id,
            )
            for folder in folders
        )
        self.folder_destination_by_label = dict(destination_pairs)
        labels = tuple(label for label, _folder_id in destination_pairs)
        self.folder_move_combo.configure(values=labels)
        if self.folder_move_var.get() not in self.folder_destination_by_label:
            self.folder_move_var.set(labels[0])

        scope = self.folder_scope_id
        if scope not in {ALL_FOLDER_ID, ""} and scope not in self.folder_by_id:
            scope = ALL_FOLDER_ID
            self.folder_scope_id = scope
        selection_id = (
            ALL_FOLDER_ID
            if scope == ALL_FOLDER_ID
            else UNGROUPED_TREE_ID if not scope else scope
        )
        if tree.exists(selection_id):
            tree.selection_set(selection_id)
            tree.focus(selection_id)
            tree.see(selection_id)
        self._update_folder_scope_labels()
        self.refreshing_folder_tree = False

    def _update_folder_scope_labels(self) -> None:
        folder = self.folder_by_id.get(self.folder_scope_id)
        if self.folder_scope_id == ALL_FOLDER_ID:
            self.current_folder_var.set("Tất cả workspace")
            self.current_folder_detail_var.set("Toàn bộ thư mục và workspace chưa phân loại")
            self.folder_parent_hint_var.set("Tạo thư mục ở cấp gốc")
            action_state = "disabled"
        elif not self.folder_scope_id:
            self.current_folder_var.set("Chưa phân loại")
            self.current_folder_detail_var.set("Workspace chưa được đưa vào thư mục")
            self.folder_parent_hint_var.set("Tạo thư mục ở cấp gốc")
            action_state = "disabled"
        elif folder is not None:
            breadcrumb = self._folder_breadcrumb(folder.folder_id, self.folder_by_id)
            self.current_folder_var.set(folder.name)
            self.current_folder_detail_var.set(breadcrumb)
            self.folder_parent_hint_var.set(f"Tạo thư mục con trong: {folder.name}")
            action_state = "normal"
        else:
            action_state = "disabled"
        self.folder_rename_button.configure(state=action_state)
        self.folder_delete_button.configure(state=action_state)

    def _on_folder_tree_selected(self, _event: object | None = None) -> None:
        if self.refreshing_folder_tree:
            return
        selected = self.folder_tree.selection()
        if not selected:
            return
        item_id = selected[0]
        scope = (
            ALL_FOLDER_ID
            if item_id == ALL_FOLDER_ID
            else "" if item_id == UNGROUPED_TREE_ID else item_id
        )
        if scope == self.folder_scope_id:
            self._update_folder_scope_labels()
            return
        self.folder_scope_id = scope
        self._update_folder_scope_labels()
        self.reset_cards_scroll = True
        self.refresh()

    def _toggle_selected_folder(self, _event: object | None = None) -> None:
        selected = self.folder_tree.selection()
        if not selected:
            return
        item_id = selected[0]
        if item_id in self.folder_by_id:
            self.folder_tree.item(
                item_id,
                open=not bool(self.folder_tree.item(item_id, "open")),
            )

    def _selected_workspace_folder(self) -> WorkspaceFolder | None:
        return self.folder_by_id.get(self.folder_scope_id)

    def _rename_selected_workspace_folder(self) -> None:
        folder = self._selected_workspace_folder()
        if folder is not None:
            self._rename_workspace_folder(folder)

    def _delete_selected_workspace_folder(self) -> None:
        folder = self._selected_workspace_folder()
        if folder is not None:
            self._delete_workspace_folder(folder)

    def _move_selected_with_picker(self) -> None:
        folder_id = self.folder_destination_by_label.get(self.folder_move_var.get())
        if folder_id is None:
            self.selection_hint_var.set("Hãy chọn một thư mục đích hợp lệ.")
            return
        self._move_selected_to_folder(folder_id)

    def _select_all_visible_workspaces(self) -> None:
        self.selected_workspace_ids = set(self.workspace_card_controls)
        for workspace_id, variable in self.workspace_select_vars.items():
            variable.set(workspace_id in self.selected_workspace_ids)
        self._update_workspace_selection_hint()
        self._refresh_workspace_queue_selection_count()

    def _queue_selected_workspace_ids(self) -> set[str]:
        tree = self.workspace_queue_tree
        if tree is not None:
            selected_rows = {
                self.workspace_queue_items[item_id]
                for item_id in tree.selection()
                if item_id in self.workspace_queue_items
            }
            if selected_rows:
                return selected_rows
        return {
            workspace_id
            for workspace_id in self.selected_workspace_ids
            if workspace_id in self.workspace_by_id
        }

    def _refresh_workspace_queue_selection_count(self) -> None:
        count = len(self._queue_selected_workspace_ids())
        self.workspace_queue_selection_var.set(
            f"Đã chọn {count} workspace · dùng Ctrl/Shift để chọn từng dòng."
            if count
            else "Chưa chọn workspace nào · Chọn tất cả sẽ áp dụng cho toàn bộ danh sách."
        )

    def _select_all_queue_workspaces(self) -> None:
        self.selected_workspace_ids = set(self.workspace_by_id)
        for workspace_id, variable in self.workspace_select_vars.items():
            variable.set(workspace_id in self.selected_workspace_ids)
        tree = self.workspace_queue_tree
        if tree is not None:
            items = tree.get_children("")
            if items:
                tree.selection_set(*items)
        self.selection_hint_var.set(
            f"Đã chọn toàn bộ {len(self.selected_workspace_ids)} workspace · "
            "có thể Tiếp tục hoặc Dừng hàng loạt."
        )
        self._refresh_workspace_queue_selection_count()

    def _clear_queue_workspace_selection(self) -> None:
        self.selected_workspace_ids.clear()
        for variable in self.workspace_select_vars.values():
            variable.set(False)
        tree = self.workspace_queue_tree
        if tree is not None:
            tree.selection_remove(*tree.selection())
        self.selection_hint_var.set(
            "Tích chọn một hoặc nhiều workspace để chuyển sang thư mục khác."
        )
        self._refresh_workspace_queue_selection_count()

    def _run_bulk_workspace_action(
        self,
        action: str,
        *,
        workspace_ids: set[str] | None = None,
    ) -> None:
        selected_ids = (
            set(workspace_ids)
            if workspace_ids is not None
            else self._queue_selected_workspace_ids()
        )
        workspaces = tuple(
            sorted(
                (
                    self.workspace_by_id[workspace_id]
                    for workspace_id in selected_ids
                    if workspace_id in self.workspace_by_id
                ),
                key=lambda workspace: workspace.name.casefold(),
            )
        )
        if not workspaces:
            self.workspace_queue_selection_var.set(
                "Chưa chọn workspace nào · hãy chọn dòng hoặc bấm Chọn tất cả."
            )
            return
        handler_by_action = {
            "run": self.bulk_run_handler,
            "continue": self.bulk_continue_handler,
            "skip": self.bulk_skip_handler,
            "stop": self.bulk_stop_handler,
        }
        handler = handler_by_action.get(action)
        if handler is None:
            self.workspace_queue_selection_var.set(
                "Bản cài hiện tại chưa có bộ điều khiển hàng loạt."
            )
            return
        if action == "skip":
            confirmed = messagebox.askyesno(
                "Skip các phiên đã chọn?",
                f"Hủy và đóng gói {len(workspaces)} phiên workspace đang chọn?\n\n"
                "Các tác vụ đang chạy sẽ được dừng an toàn; checkpoint, Media, Output "
                "và VOICEVOX của từng phiên sẽ chuyển vào thư mục Skipped. "
                "Lịch, profile và cấu hình vẫn được giữ.\n\n"
                "Thao tác này tương đương nút “Skip / xóa checkpoint” trong workspace.",
                icon="warning",
                parent=self.root,
            )
            if not confirmed:
                self.workspace_queue_selection_var.set("Đã hủy Skip hàng loạt.")
                return
        try:
            handled, detail = handler(workspaces)
        except Exception as exc:
            record_exception(
                "workspace-bulk-action",
                exc,
                metadata={"action": action, "workspace_count": len(workspaces)},
            )
            messagebox.showerror(
                "Không thể điều khiển hàng loạt",
                str(exc),
                parent=self.root,
            )
            return
        verb = {
            "run": "Chạy toàn bộ",
            "continue": "Tiếp tục",
            "skip": "Skip phiên",
            "stop": "Dừng",
        }.get(action, action)
        self._refresh_workspace_activity(reschedule=False)
        self.workspace_queue_selection_var.set(
            f"{verb}: đã nhận {handled}/{len(workspaces)} workspace"
            + (f" · {detail}" if detail else "")
        )

    def _bulk_continue_selected_workspaces(self) -> None:
        self._run_bulk_workspace_action("continue")

    def _bulk_run_selected_workspaces(self) -> None:
        self._run_bulk_workspace_action("run")

    def _bulk_run_visible_workspaces(self) -> None:
        self._run_bulk_workspace_action(
            "run",
            workspace_ids=set(self.selected_workspace_ids),
        )

    def _bulk_skip_selected_workspaces(self) -> None:
        self._run_bulk_workspace_action("skip")

    def _bulk_skip_visible_workspaces(self) -> None:
        self._run_bulk_workspace_action(
            "skip",
            workspace_ids=set(self.selected_workspace_ids),
        )

    def _bulk_stop_selected_workspaces(self) -> None:
        self._run_bulk_workspace_action("stop")

    def _add_workspace_folder_section(
        self,
        folder: WorkspaceFolder | None,
        workspaces: list[Workspace],
    ) -> ttk.Frame:
        folder_id = folder.folder_id if folder is not None else ""
        title = folder.name if folder is not None else "Chưa phân loại"
        section = ttk.Frame(self.cards_frame, style="Panel.TFrame")
        section.pack(fill="x", pady=(0, 12))
        header = ttk.Frame(
            section,
            style="PanelAlt.TFrame",
            padding=(12, 8),
        )
        header.pack(fill="x", pady=(0, 6))
        self.folder_drop_targets[folder_id] = header
        ttk.Label(
            header,
            image=self.icons["folder"],
            background=PANEL_ALT,
        ).pack(side=LEFT, padx=(0, 8))
        ttk.Label(
            header,
            text=title,
            style="WorkspaceFolderTitle.TLabel",
        ).pack(side=LEFT)
        ttk.Label(
            header,
            text=f"{len(workspaces)} workspace · thả mục đã chọn vào đây",
            style="WorkspaceCardMeta.TLabel",
        ).pack(side=LEFT, padx=(9, 0))
        if folder is not None:
            ttk.Button(
                header,
                text="Xóa thư mục",
                image=self.icons["trash_dark"],
                compound=LEFT,
                style="Danger.TButton",
                command=lambda item=folder: self._delete_workspace_folder(item),
            ).pack(side=RIGHT)
            ttk.Button(
                header,
                text="Đổi tên",
                image=self.icons["edit"],
                compound=LEFT,
                style="Secondary.TButton",
                command=lambda item=folder: self._rename_workspace_folder(item),
            ).pack(side=RIGHT, padx=(0, 7))
        content = ttk.Frame(section, style="Panel.TFrame")
        content.pack(fill="x")
        if not workspaces:
            ttk.Label(
                content,
                text="Thư mục đang trống · chọn workspace rồi kéo biểu tượng ⠿ vào đây.",
                style="Muted.TLabel",
            ).pack(anchor="w", padx=12, pady=(2, 6))
        self._bind_card_scroll(header)
        return content

    def _add_workspace_card(
        self,
        workspace: Workspace,
        *,
        recent: bool,
        parent: ttk.Frame | None = None,
    ) -> None:
        activity_badge_var = StringVar(master=self.root, value="○ CHƯA BẮT ĐẦU")
        activity_status_var = StringVar(master=self.root, value="○  Sẵn sàng")
        activity_progress_var = tk.DoubleVar(master=self.root, value=0.0)
        activity_percent_var = StringVar(master=self.root, value="0%")
        card = ttk.Frame(
            parent or self.cards_frame,
            style="PanelAlt.TFrame",
            padding=(13, 11),
        )
        card.pack(fill="x", pady=(0, 7))
        self.workspace_cards[workspace.workspace_id] = card

        top = ttk.Frame(card, style="PanelAlt.TFrame")
        top.pack(fill="x")
        selected_var = tk.BooleanVar(
            master=self.root,
            value=workspace.workspace_id in self.selected_workspace_ids,
        )
        self.workspace_select_vars[workspace.workspace_id] = selected_var
        ttk.Checkbutton(
            top,
            variable=selected_var,
            style="WorkspaceSelect.TCheckbutton",
            command=lambda workspace_id=workspace.workspace_id: self._toggle_workspace_selection(
                workspace_id
            ),
        ).pack(side=LEFT, padx=(0, 5))
        drag_handle = ttk.Label(
            top,
            text="⠿",
            style="WorkspaceCardTitle.TLabel",
            cursor="fleur",
            padding=(2, 1),
        )
        drag_handle.pack(side=LEFT, padx=(0, 5))
        drag_handle.bind(
            "<ButtonPress-1>",
            lambda event, workspace_id=workspace.workspace_id: self._start_workspace_drag(
                event, workspace_id
            ),
        )
        drag_handle.bind("<B1-Motion>", self._workspace_drag_motion)
        drag_handle.bind("<ButtonRelease-1>", self._finish_workspace_drag)
        ttk.Label(
            top,
            image=self.icons["folder"],
            background=PANEL_ALT,
            padding=(1, 1),
        ).pack(side=LEFT, padx=(0, 9))
        title = ttk.Frame(top, style="PanelAlt.TFrame")
        title.pack(side=LEFT, fill="x", expand=True)
        ttk.Label(
            title,
            text=workspace.name,
            style="WorkspaceCardTitle.TLabel",
            wraplength=280,
            justify=LEFT,
        ).pack(anchor="w")
        ttk.Label(
            title,
            text=f"{workspace.category}  ·  {self._updated_label(workspace.updated_at)}",
            style="WorkspaceCardMeta.TLabel",
        ).pack(anchor="w", pady=(2, 0))
        header_badges = ttk.Frame(top, style="PanelAlt.TFrame")
        header_badges.pack(side=RIGHT, padx=(8, 0))
        activity_badge_label = ttk.Label(
            header_badges,
            textvariable=activity_badge_var,
            style="WorkspaceCardMeta.TLabel",
        )
        if recent:
            ttk.Label(
                header_badges,
                text="●  GẦN NHẤT",
                style="WorkspaceCardReady.TLabel",
            ).pack(anchor="e", pady=(3, 0))

        ttk.Label(
            card,
            text=(
                f"{self._folder_breadcrumb(workspace.folder_id, self.folder_by_id)}"
                if workspace.folder_id in self.folder_by_id
                else "Chưa phân loại"
            )
            + f"  ·  {workspace.root.name}",
            image=self.icons["sliders"],
            compound=LEFT,
            style="WorkspaceCardMeta.TLabel",
            wraplength=650,
            justify=LEFT,
        ).pack(anchor="w", pady=(10, 8))

        activity_row = ttk.Frame(card, style="PanelAlt.TFrame")
        activity_row.pack(fill="x", pady=(0, 5))
        activity_status_label = ttk.Label(
            activity_row,
            textvariable=activity_status_var,
            style="WorkspaceCardMeta.TLabel",
        )
        activity_status_label.pack(side=LEFT, fill="x", expand=True)
        ttk.Label(
            activity_row,
            textvariable=activity_percent_var,
            style="WorkspaceCardMeta.TLabel",
        ).pack(side=RIGHT, padx=(10, 0))
        ttk.Progressbar(
            card,
            orient=HORIZONTAL,
            mode="determinate",
            maximum=100.0,
            variable=activity_progress_var,
        ).pack(fill="x", pady=(0, 10))
        activity_video_var = StringVar(
            master=self.root,
            value="VIDEO ĐẦU RA · Chưa có file video",
        )
        ttk.Label(
            card,
            textvariable=activity_video_var,
            image=self.icons["film"],
            compound=LEFT,
            style="WorkspaceCardMeta.TLabel",
            wraplength=720,
            justify=LEFT,
        ).pack(anchor="w", pady=(0, 9))

        result_row = ttk.Frame(card, style="PanelAlt.TFrame")
        result_row.pack(fill="x", pady=(0, 7))
        ttk.Label(
            result_row,
            text="TỆP KẾT QUẢ",
            style="WorkspaceCardMeta.TLabel",
        ).pack(side=LEFT)
        video_button = ttk.Button(
            result_row,
            text="Xem video",
            image=self.icons["play"],
            compound=LEFT,
            style="Secondary.TButton",
            command=lambda workspace_id=workspace.workspace_id: self._open_workspace_video(
                workspace_id
            ),
        )
        video_button.pack(side=RIGHT)
        package_button = ttk.Button(
            result_row,
            text="Mở file Tổng hợp",
            image=self.icons["queue"],
            compound=LEFT,
            style="Secondary.TButton",
            command=lambda workspace_id=workspace.workspace_id: self._open_workspace_package(
                workspace_id
            ),
        )
        package_button.pack(side=RIGHT, padx=(0, 7))

        from .google_workspace_ui import add_workspace_controls
        add_workspace_controls(card, workspace, getattr(self, 'google_service', None))
        action_row = ttk.Frame(card, style="PanelAlt.TFrame")
        action_row.pack(fill="x")
        manual_upload_var = tk.BooleanVar(master=self.root, value=False)
        manual_upload_check = ttk.Checkbutton(
            action_row,
            text="Đã upload YouTube thủ công",
            variable=manual_upload_var,
            style="WorkspaceSelect.TCheckbutton",
            command=lambda workspace_id=workspace.workspace_id: self._toggle_manual_upload(
                workspace_id
            ),
        )
        manual_upload_check.pack(side=LEFT, padx=(8, 0))
        open_button = ttk.Button(
            action_row,
            text="MỞ WORKSPACE",
            image=self.icons["play_dark"],
            compound=LEFT,
            style="Accent.TButton",
            command=lambda item=workspace: self._open_workspace(item),
        )
        open_button.pack(side=RIGHT)
        delete_button = ttk.Button(
            action_row,
            text="Xóa",
            image=self.icons["trash_dark"],
            compound=LEFT,
            style="Danger.TButton",
            command=lambda item=workspace: self._delete_workspace(item),
        )
        delete_button.pack(side=RIGHT, padx=(0, 7))
        rename_button = ttk.Button(
            action_row,
            text="Đổi tên",
            image=self.icons["edit"],
            compound=LEFT,
            style="Secondary.TButton",
            command=lambda item=workspace: self._begin_rename(item),
        )
        rename_button.pack(side=RIGHT, padx=(0, 7))
        checkpoint_button = ttk.Button(
            action_row,
            text="Skip / xóa checkpoint",
            image=self.icons["x"],
            compound=LEFT,
            style="Secondary.TButton",
            command=lambda item=workspace: self._reset_workspace_checkpoint(item),
        )
        checkpoint_button.pack(side=LEFT)

        rename_var = StringVar(master=self.root, value=workspace.name)
        rename_frame = ttk.Frame(card, style="PanelAlt.TFrame")
        rename_entry = ttk.Entry(rename_frame, textvariable=rename_var)
        rename_entry.pack(side=LEFT, fill="x", expand=True)
        save_button = ttk.Button(
            rename_frame,
            text="Lưu tên",
            image=self.icons["check_dark"],
            compound=LEFT,
            style="Accent.TButton",
            command=lambda item=workspace: self._save_workspace_name(item),
        )
        save_button.pack(side=RIGHT, padx=(7, 0))
        cancel_button = ttk.Button(
            rename_frame,
            text="Hủy",
            image=self.icons["x"],
            compound=LEFT,
            style="Secondary.TButton",
            command=lambda item=workspace: self._cancel_rename(item.workspace_id),
        )
        cancel_button.pack(side=RIGHT, padx=(7, 0))
        rename_entry.bind(
            "<Return>", lambda _event, item=workspace: self._save_workspace_name(item)
        )
        rename_entry.bind(
            "<Escape>", lambda _event, item=workspace: self._cancel_rename(item.workspace_id)
        )
        self.workspace_card_controls[workspace.workspace_id] = {
            "rename_frame": rename_frame,
            "rename_entry": rename_entry,
            "rename_var": rename_var,
            "rename_button": rename_button,
            "checkpoint_button": checkpoint_button,
            "delete_button": delete_button,
            "open_button": open_button,
            "action_row": action_row,
            "activity_status_var": activity_status_var,
            "activity_badge_var": activity_badge_var,
            "activity_badge_label": activity_badge_label,
            "activity_progress_var": activity_progress_var,
            "activity_percent_var": activity_percent_var,
            "activity_status_label": activity_status_label,
            "activity_video_var": activity_video_var,
            "video_button": video_button,
            "package_button": package_button,
            "manual_upload_var": manual_upload_var,
            "manual_upload_check": manual_upload_check,
            "selected_var": selected_var,
            "drag_handle": drag_handle,
        }

        for widget in (card, top, title):
            widget.bind(
                "<Double-Button-1>",
                lambda _event, item=workspace: self._open_workspace(item),
            )
        self._bind_card_scroll(card)

    def _create_workspace_folder(self) -> None:
        parent_id = (
            self.folder_scope_id
            if self.folder_scope_id in self.folder_by_id
            else ""
        )
        try:
            folder = self.store.create_folder(self.folder_name_var.get(), parent_id)
        except Exception as exc:
            record_exception("workspace-folder-create", exc)
            messagebox.showerror(
                "Không thể tạo thư mục",
                str(exc),
                parent=self.root,
            )
            return
        self.folder_name_var.set("")
        self.folder_scope_id = folder.folder_id
        self.selection_hint_var.set(
            f"Đã tạo thư mục “{folder.name}” · có thể kéo ⠿ hoặc dùng nút Chuyển."
        )
        self.refresh()

    def _rename_workspace_folder(self, folder: WorkspaceFolder) -> None:
        value = simpledialog.askstring(
            "Đổi tên thư mục chủ đề",
            "Nhập tên mới:",
            initialvalue=folder.name,
            parent=self.root,
        )
        if value is None:
            return
        try:
            renamed = self.store.rename_folder(folder.folder_id, value)
        except Exception as exc:
            record_exception(
                "workspace-folder-rename",
                exc,
                metadata={"folder_id": folder.folder_id},
            )
            messagebox.showerror(
                "Không thể đổi tên thư mục",
                str(exc),
                parent=self.root,
            )
            return
        self.selection_hint_var.set(f"Đã đổi tên thư mục thành “{renamed.name}”.")
        self.refresh()

    def _delete_workspace_folder(self, folder: WorkspaceFolder) -> None:
        count = sum(
            workspace.folder_id == folder.folder_id
            for workspace in self.workspace_by_id.values()
        )
        child_count = sum(
            item.parent_id == folder.folder_id for item in self.folder_by_id.values()
        )
        parent = self.folder_by_id.get(folder.parent_id)
        destination = parent.name if parent is not None else "Chưa phân loại"
        confirmed = messagebox.askyesno(
            "Xóa thư mục chủ đề?",
            f"Xóa thư mục “{folder.name}”?\n\n"
            f"{count} workspace trực tiếp sẽ chuyển về {destination}. "
            + (
                f"{child_count} thư mục con sẽ được đưa lên một cấp. "
                if child_count
                else ""
            )
            +
            "Media, video, checkpoint và thư mục thật không bị xóa.",
            icon="warning",
            parent=self.root,
        )
        if not confirmed:
            return
        try:
            self.store.delete_folder(folder.folder_id)
        except Exception as exc:
            record_exception(
                "workspace-folder-delete",
                exc,
                metadata={"folder_id": folder.folder_id},
            )
            messagebox.showerror(
                "Không thể xóa thư mục",
                str(exc),
                parent=self.root,
            )
            return
        self.selection_hint_var.set(
            f"Đã xóa thư mục “{folder.name}” · dữ liệu workspace vẫn được giữ nguyên."
        )
        self.folder_scope_id = folder.parent_id or ALL_FOLDER_ID
        self.refresh()

    def _toggle_workspace_selection(self, workspace_id: str) -> None:
        variable = self.workspace_select_vars.get(workspace_id)
        if variable is not None and variable.get():
            self.selected_workspace_ids.add(workspace_id)
        else:
            self.selected_workspace_ids.discard(workspace_id)
        self._update_workspace_selection_hint()
        self._refresh_workspace_queue_selection_count()

    def _update_workspace_selection_hint(self) -> None:
        count = len(self.selected_workspace_ids)
        if count:
            self.selection_hint_var.set(
                f"Đã chọn {count} workspace · kéo ⠿ vào cây hoặc dùng nút Chuyển."
            )
        else:
            self.selection_hint_var.set(
                "Tích chọn một hoặc nhiều workspace để chuyển sang thư mục khác."
            )

    def _cancel_drag_animation(self) -> None:
        for after_id in self.drag_animation_after_ids:
            try:
                self.root.after_cancel(after_id)
            except tk.TclError:
                pass
        self.drag_animation_after_ids.clear()

    def _destroy_drag_ghost(self) -> None:
        self._cancel_drag_animation()
        ghost = self.drag_ghost
        self.drag_ghost = None
        if ghost is not None:
            try:
                ghost.destroy()
            except tk.TclError:
                pass
        self.drag_visual_started = False
        self._set_drag_drop_target(None)

    def _set_drag_drop_target(self, folder_id: str | None) -> None:
        previous = self.drag_drop_folder_id
        self.drag_drop_folder_id = folder_id
        tree = getattr(self, "folder_tree", None)
        if not isinstance(tree, ttk.Treeview):
            return
        tree.tag_configure("drag-target", background="#24432a", foreground=TEXT)
        for candidate in (previous, folder_id):
            if candidate is None:
                continue
            item_id = UNGROUPED_TREE_ID if candidate == "" else candidate
            if tree.exists(item_id):
                tags = list(tree.item(item_id, "tags"))
                if candidate == folder_id:
                    if "drag-target" not in tags:
                        tags.append("drag-target")
                else:
                    tags = [tag for tag in tags if tag != "drag-target"]
                tree.item(item_id, tags=tuple(tags))

    def _start_workspace_drag(self, event: tk.Event, workspace_id: str) -> None:
        self.drag_workspace_id = workspace_id
        if workspace_id not in self.selected_workspace_ids:
            self.selected_workspace_ids = {workspace_id}
            for item_id, variable in self.workspace_select_vars.items():
                variable.set(item_id == workspace_id)
        self._update_workspace_selection_hint()
        self._destroy_drag_ghost()
        self.drag_origin = (int(event.x_root), int(event.y_root))

    def _show_drag_ghost(self, event: tk.Event) -> None:
        workspace_id = self.drag_workspace_id
        if not workspace_id or self.drag_ghost is not None:
            return
        ghost = tk.Toplevel(self.root)
        ghost.overrideredirect(True)
        ghost.configure(bg=SUCCESS, padx=1, pady=1)
        try:
            ghost.attributes("-topmost", True)
            ghost.attributes("-alpha", 0.94)
        except tk.TclError:
            pass
        shell = tk.Frame(ghost, bg=PANEL_ALT, padx=10, pady=7)
        shell.pack()
        workspace = self.workspace_by_id.get(workspace_id)
        name = workspace.name if workspace is not None else "Workspace"
        tk.Label(
            shell,
            text=f"⠿  {len(self.selected_workspace_ids)} workspace",
            bg=PANEL_ALT,
            fg=SUCCESS,
            font=("Segoe UI Semibold", 9),
        ).pack(anchor="w")
        tk.Label(
            shell,
            text=name,
            bg=PANEL_ALT,
            fg=TEXT,
            font=("Segoe UI", 9),
        ).pack(anchor="w")
        ghost.geometry(f"+{int(event.x_root) + 14}+{int(event.y_root) + 12}")
        self.drag_ghost = ghost
        self.drag_visual_started = True

    def _workspace_drag_motion(self, event: tk.Event) -> None:
        if self.drag_workspace_id:
            if not self.drag_visual_started:
                distance = max(
                    abs(int(event.x_root) - self.drag_origin[0]),
                    abs(int(event.y_root) - self.drag_origin[1]),
                )
                if distance < 6:
                    return
                self._show_drag_ghost(event)
            if self.drag_ghost is not None:
                try:
                    self.drag_ghost.geometry(
                        f"+{int(event.x_root) + 14}+{int(event.y_root) + 12}"
                    )
                except tk.TclError:
                    pass
            folder_id = self._folder_at_pointer(event.x_root, event.y_root)
            self._set_drag_drop_target(folder_id)
            folder = self.folder_by_id.get(folder_id or "")
            destination = (
                folder.name
                if folder is not None
                else "Chưa phân loại" if folder_id == "" else "ngoài cây thư mục"
            )
            self.selection_hint_var.set(
                f"Đang kéo {len(self.selected_workspace_ids)} workspace · đích: {destination}."
            )

    def _folder_at_pointer(self, x_root: int, y_root: int) -> str | None:
        tree = getattr(self, "folder_tree", None)
        if isinstance(tree, ttk.Treeview):
            try:
                tree_x = tree.winfo_rootx()
                tree_y = tree.winfo_rooty()
                inside = (
                    tree_x <= x_root < tree_x + tree.winfo_width()
                    and tree_y <= y_root < tree_y + tree.winfo_height()
                )
                if inside:
                    item_id = tree.identify_row(y_root - tree_y)
                    if item_id == UNGROUPED_TREE_ID:
                        return ""
                    if item_id in self.folder_by_id:
                        return item_id
                    return None
            except tk.TclError:
                return None
        try:
            widget = self.root.winfo_containing(x_root, y_root)
        except tk.TclError:
            return None
        while widget is not None:
            for folder_id, target in self.folder_drop_targets.items():
                if widget == target:
                    return folder_id
            parent_name = widget.winfo_parent()
            if not parent_name:
                break
            try:
                widget = widget._nametowidget(parent_name)
            except (KeyError, tk.TclError):
                break
        return None

    def _animate_drag_finish(
        self,
        folder_id: str | None,
        on_complete: Callable[[], None],
    ) -> None:
        ghost = self.drag_ghost
        if ghost is None or not self.animations_enabled:
            self._destroy_drag_ghost()
            on_complete()
            return
        try:
            start_x = ghost.winfo_x()
            start_y = ghost.winfo_y()
        except tk.TclError:
            self._destroy_drag_ghost()
            on_complete()
            return
        if folder_id is None:
            offsets = (0, -7, 7, -5, 5, 0)

            def shake(index: int = 0) -> None:
                if self.drag_ghost is None or index >= len(offsets):
                    self._destroy_drag_ghost()
                    on_complete()
                    return
                try:
                    self.drag_ghost.geometry(
                        f"+{start_x + offsets[index]}+{start_y}"
                    )
                except tk.TclError:
                    pass
                after_id = self.root.after(24, lambda: shake(index + 1))
                self.drag_animation_after_ids.append(after_id)

            shake()
            return

        item_id = UNGROUPED_TREE_ID if folder_id == "" else folder_id
        try:
            bbox = self.folder_tree.bbox(item_id)
            target_x = self.folder_tree.winfo_rootx() + max(8, bbox[0] + 20)
            target_y = self.folder_tree.winfo_rooty() + max(8, bbox[1] + bbox[3] // 2)
        except (tk.TclError, IndexError):
            self._destroy_drag_ghost()
            on_complete()
            return

        steps = 6

        def glide(step: int = 1) -> None:
            if self.drag_ghost is None:
                on_complete()
                return
            ratio = step / steps
            eased = 1.0 - (1.0 - ratio) ** 3
            x = round(start_x + (target_x - start_x) * eased)
            y = round(start_y + (target_y - start_y) * eased)
            try:
                self.drag_ghost.geometry(f"+{x}+{y}")
            except tk.TclError:
                pass
            if step >= steps:
                self._destroy_drag_ghost()
                on_complete()
                return
            after_id = self.root.after(22, lambda: glide(step + 1))
            self.drag_animation_after_ids.append(after_id)

        glide()

    def _finish_workspace_drag(self, event: tk.Event) -> None:
        if not self.drag_workspace_id:
            return
        self.drag_workspace_id = ""
        if not self.drag_visual_started:
            self._update_workspace_selection_hint()
            return
        folder_id = self._folder_at_pointer(event.x_root, event.y_root)
        if folder_id is None:
            self._animate_drag_finish(
                None,
                lambda: self.selection_hint_var.set(
                    "Chưa di chuyển · hãy thả ⠿ lên một nhánh trong cây thư mục."
                ),
            )
            return
        self._animate_drag_finish(
            folder_id,
            lambda target=folder_id: self._move_selected_to_folder(target),
        )

    def _move_selected_to_folder(self, folder_id: str) -> None:
        selected = set(self.selected_workspace_ids)
        if not selected:
            self._update_workspace_selection_hint()
            return
        try:
            moved = self.store.assign_to_folder(selected, folder_id)
        except Exception as exc:
            record_exception(
                "workspace-folder-assign",
                exc,
                metadata={"workspace_count": len(selected)},
            )
            messagebox.showerror(
                "Không thể chuyển workspace",
                str(exc),
                parent=self.root,
            )
            return
        folder = next(
            (item for item in self.store.list_folders() if item.folder_id == folder_id),
            None,
        )
        destination = folder.name if folder is not None else "Chưa phân loại"
        self.selected_workspace_ids.clear()
        self.selection_hint_var.set(
            f"Đã chuyển {len(moved)} workspace vào “{destination}” · dữ liệu thật giữ nguyên."
        )
        self.refresh()

    def _begin_rename(self, workspace: Workspace) -> None:
        if self.active_rename_id and self.active_rename_id != workspace.workspace_id:
            self._cancel_rename(self.active_rename_id)
        controls = self.workspace_card_controls.get(workspace.workspace_id)
        if controls is None:
            return
        rename_frame = controls["rename_frame"]
        rename_entry = controls["rename_entry"]
        rename_var = controls["rename_var"]
        rename_button = controls["rename_button"]
        action_row = controls["action_row"]
        if not isinstance(rename_frame, ttk.Frame) or not isinstance(rename_entry, ttk.Entry):
            return
        if isinstance(rename_var, StringVar):
            rename_var.set(workspace.name)
        rename_frame.pack(fill="x", pady=(0, 9), before=action_row)
        if isinstance(rename_button, ttk.Button):
            rename_button.configure(state="disabled")
        self.active_rename_id = workspace.workspace_id
        rename_entry.focus_set()
        rename_entry.selection_range(0, END)
        self.selection_hint_var.set(
            "Nhập tên mới rồi bấm Lưu tên · đường dẫn và dữ liệu vẫn được giữ nguyên."
        )

    def _cancel_rename(self, workspace_id: str) -> None:
        controls = self.workspace_card_controls.get(workspace_id)
        if controls is None:
            self.active_rename_id = ""
            return
        rename_frame = controls["rename_frame"]
        rename_button = controls["rename_button"]
        if isinstance(rename_frame, ttk.Frame):
            rename_frame.pack_forget()
        if isinstance(rename_button, ttk.Button):
            rename_button.configure(state="normal")
        if self.active_rename_id == workspace_id:
            self.active_rename_id = ""
        self.selection_hint_var.set(
            "Mỗi workspace giữ riêng media, prompt, voice, video và lịch sử chống lặp."
        )

    def _save_workspace_name(self, workspace: Workspace) -> None:
        controls = self.workspace_card_controls.get(workspace.workspace_id)
        if controls is None:
            return
        rename_var = controls["rename_var"]
        try:
            value = rename_var.get() if isinstance(rename_var, StringVar) else ""
            renamed = self.store.rename(workspace.workspace_id, value)
        except Exception as exc:
            record_exception(
                "workspace-rename",
                exc,
                metadata={"workspace_id": workspace.workspace_id},
            )
            messagebox.showerror("Không thể đổi tên workspace", str(exc), parent=self.root)
            return
        self.active_rename_id = ""
        self.selection_hint_var.set(
            f"Đã đổi tên thành {renamed.name} · toàn bộ dữ liệu và đường dẫn được giữ nguyên."
        )
        self.refresh()

    def _toggle_manual_upload(self, workspace_id: str) -> None:
        controls = self.workspace_card_controls.get(workspace_id)
        workspace = self.workspace_by_id.get(workspace_id)
        if controls is None or workspace is None:
            return
        variable = controls.get("manual_upload_var")
        if not isinstance(variable, tk.BooleanVar) or not variable.get():
            return
        if self.manual_upload_handler is None:
            variable.set(False)
            messagebox.showwarning(
                "Chưa bật xác nhận upload",
                "Không có bộ xử lý upload YouTube trong phiên này.",
                parent=self.root,
            )
            return
        try:
            success, detail = self.manual_upload_handler(workspace)
        except Exception as exc:
            record_exception(
                "workspace-manual-youtube-upload",
                exc,
                metadata={"workspace_id": workspace_id},
            )
            success, detail = False, str(exc)
        if not success:
            variable.set(False)
            messagebox.showerror("Chưa xác nhận được upload", detail, parent=self.root)
            return
        check = controls.get("manual_upload_check")
        if isinstance(check, ttk.Checkbutton):
            check.configure(state="disabled")
        self.selection_hint_var.set(f"{workspace.name}: {detail}")
        # The confirmation is durable; refresh keeps the card state correct
        # after sorting/filtering without stealing focus from another window.
        self._refresh_workspace_activity(reschedule=False)

    def _reset_workspace_checkpoint(self, workspace: Workspace) -> None:
        confirmed = messagebox.askyesno(
            "Skip phiên hiện tại?",
            f"Hủy toàn bộ phiên video đang dở của “{workspace.name}”?\n\n"
            "Tool sẽ dừng các tác vụ đang chạy, chuyển checkpoint, Media, Output "
            "và VOICEVOX của phiên này vào thư mục Skipped, rồi làm sạch workspace "
            "để nhận video mới.\n\n"
            "Lịch hằng ngày, kênh/profile và cấu hình vẫn được giữ. Chỉ bấm Có khi "
            "bạn chắc chắn muốn bỏ phiên hiện tại; lỗi hoặc quá giờ tự động sẽ không "
            "làm thao tác này.",
            icon="warning",
            parent=self.root,
        )
        if not confirmed:
            return
        try:
            if self.checkpoint_reset_handler is not None:
                success, detail = self.checkpoint_reset_handler(workspace)
            else:
                result = reset_workspace_session_state(workspace)
                success = True
                detail = (
                    f"Đã xóa {len(result.removed_state_files)} tệp trạng thái phiên."
                    if result.removed_state_files
                    else "Workspace không còn checkpoint phiên cũ."
                )
        except Exception as exc:
            record_exception(
                "workspace-checkpoint-reset",
                exc,
                metadata={"workspace_id": workspace.workspace_id},
            )
            success, detail = False, str(exc)
        if not success:
            messagebox.showerror(
                "Chưa Skip được phiên",
                detail,
                parent=self.root,
            )
            return
        self.workspace_outcomes.pop(workspace.workspace_id, None)
        self.selection_hint_var.set(
            f"{workspace.name}: {detail} Dữ liệu và lịch vẫn được giữ nguyên."
        )
        self._refresh_workspace_activity(reschedule=False)

    def _open_workspace_video(self, workspace_id: str) -> None:
        activity = self._activity_for(workspace_id)
        raw = str(activity.get("video_path", "")).strip()
        video = Path(raw).expanduser() if raw else None
        if video is not None and video.is_file():
            try:
                os.startfile(str(video))
                return
            except (OSError, AttributeError) as exc:
                messagebox.showerror(
                    "Không mở được video",
                    str(exc),
                    parent=self.root,
                )
                return
        workspace = self.workspace_by_id.get(workspace_id)
        if workspace is not None and workspace.output_dir.is_dir():
            try:
                os.startfile(str(workspace.output_dir))
                return
            except (OSError, AttributeError):
                pass
        messagebox.showinfo(
            "Chưa có video hoàn chỉnh",
            "Workspace này chưa có file video đầu ra hợp lệ.",
            parent=self.root,
        )

    @staticmethod
    def _workspace_package_path(workspace: Workspace) -> Path | None:
        """Return the exact exported content package, with a legacy fallback."""

        checkpoint = read_pipeline_checkpoint(workspace.pipeline_checkpoint_path)
        stages = checkpoint.get("stages") if isinstance(checkpoint, dict) else None
        package_stage = stages.get("package") if isinstance(stages, dict) else None
        artifacts = (
            package_stage.get("artifacts")
            if isinstance(package_stage, dict)
            else None
        )
        raw = (
            str(artifacts.get("path", "")).strip().strip('"')
            if isinstance(artifacts, dict)
            else ""
        )
        if raw:
            exact = Path(raw).expanduser()
            if not exact.is_absolute():
                exact = workspace.output_dir / exact
            exact = exact.resolve(strict=False)
            if exact.is_file() and exact.suffix.casefold() == ".txt":
                return exact

        # Older workspace checkpoints did not always record the package path.
        # Output contains the finished package while chat transcripts use the
        # explicit ``_CHAT.txt`` suffix, so the fallback stays deterministic.
        candidates: list[Path] = []
        try:
            for path in workspace.output_dir.iterdir():
                name = path.name.casefold()
                if (
                    path.is_file()
                    and path.suffix.casefold() == ".txt"
                    and not name.startswith(".")
                    and not name.endswith("_chat.txt")
                    and "master-prompt" not in name
                ):
                    candidates.append(path.resolve(strict=False))
        except OSError:
            return None

        def modified(path: Path) -> float:
            try:
                return path.stat().st_mtime
            except OSError:
                return 0.0

        return max(candidates, key=modified) if candidates else None

    def _open_workspace_package(self, workspace_id: str) -> None:
        workspace = self.workspace_by_id.get(workspace_id)
        package = (
            self._workspace_package_path(workspace)
            if workspace is not None
            else None
        )
        if package is not None and package.is_file():
            try:
                os.startfile(str(package))
                self.selection_hint_var.set(
                    f"Đã mở file Tổng hợp · {package.name}"
                )
                return
            except (OSError, AttributeError) as exc:
                messagebox.showerror(
                    "Không mở được file Tổng hợp",
                    str(exc),
                    parent=self.root,
                )
                return
        messagebox.showinfo(
            "Chưa có file Tổng hợp",
            "Workspace này chưa xuất file TXT tổng hợp. Hãy chạy hoặc bấm Tiếp tục "
            "đến khi công đoạn AI hoàn tất.",
            parent=self.root,
        )

    @staticmethod
    def _validate_workspace_delete_target(workspace: Workspace) -> Path:
        root = workspace.root
        if root == Path(root.anchor) or root == Path.home().resolve(strict=False):
            raise ValueError("Đường dẫn workspace không an toàn để xóa.")
        if not (root / "WORKSPACE.txt").is_file() or not workspace.state_dir.is_dir():
            raise ValueError(
                "Thư mục này không còn đủ dấu nhận dạng Workspace của DocuRandom. "
                "Tool từ chối tự xóa để bảo vệ dữ liệu khác."
            )
        return root

    def _delete_workspace(self, workspace: Workspace) -> None:
        activity = self._activity_for(workspace.workspace_id)
        if bool(activity.get("active", False)):
            messagebox.showinfo(
                "Workspace đang làm việc",
                "Workspace này vẫn có tác vụ chạy nền. Hãy mở workspace và hủy tác vụ "
                "trước khi xóa để bảo vệ dữ liệu đang ghi.",
                parent=self.root,
            )
            return
        root = workspace.root
        folder_exists = root.exists()
        if folder_exists:
            try:
                root = self._validate_workspace_delete_target(workspace)
            except Exception as exc:
                record_exception(
                    "workspace-delete-validation",
                    exc,
                    metadata={"workspace_id": workspace.workspace_id},
                )
                messagebox.showerror("Không thể xóa workspace", str(exc), parent=self.root)
                return
            detail = (
                "Toàn bộ thư mục Media, Output, Prompts, VOICEVOX và lịch sử sẽ được "
                "chuyển vào Thùng rác Windows để còn có thể khôi phục."
            )
        else:
            detail = (
                "Thư mục workspace không còn tồn tại. Mục bị hỏng này chỉ được xóa "
                "khỏi danh sách của tool."
            )
        confirmed = messagebox.askyesno(
            "Xóa workspace?",
            f"Xóa workspace “{workspace.name}”?\n\n{detail}\n\nĐường dẫn:\n{root}",
            icon="warning",
            parent=self.root,
        )
        if not confirmed:
            return
        try:
            if self.workspace_delete_handler is not None:
                # Retire the idle session and its timers before moving files.
                self.workspace_delete_handler(workspace)
            if folder_exists:
                send2trash(str(root))
            self.store.unregister(workspace.workspace_id)
        except Exception as exc:
            record_exception(
                "workspace-delete",
                exc,
                metadata={"workspace_id": workspace.workspace_id},
            )
            messagebox.showerror(
                "Không thể xóa workspace",
                f"Workspace chưa được xóa hoàn tất: {exc}",
                parent=self.root,
            )
            return
        self.active_rename_id = ""
        self.selection_hint_var.set(
            f"Đã xóa {workspace.name}"
            + (" · dữ liệu đang ở Thùng rác Windows." if folder_exists else ".")
        )
        self.refresh()

    def _add_empty_state(self) -> None:
        card = ttk.Frame(
            self.cards_frame,
            style="PanelAlt.TFrame",
            padding=(24, 34),
        )
        card.pack(fill=BOTH, expand=True)
        ttk.Label(
            card,
            image=self.icons["queue"],
            background=PANEL_ALT,
            padding=(4, 3),
        ).pack()
        ttk.Label(
            card,
            text="Chưa có workspace",
            style="WorkspaceEmptyTitle.TLabel",
        ).pack(pady=(10, 3))
        ttk.Label(
            card,
            text=(
                "Không có workspace phù hợp trong nhánh hoặc bộ lọc này. "
                "Mở tab Tạo mới ở khung bên trái để tạo workspace."
            ),
            style="CardMuted.TLabel",
            wraplength=420,
            justify="center",
        ).pack()

    def choose_parent(self) -> None:
        raw = self.parent_var.get().strip().strip('"')
        initial = Path(raw).expanduser() if raw else self.store.default_root
        selected = filedialog.askdirectory(
            title="Chọn nơi lưu các workspace",
            initialdir=str(initial if initial.is_dir() else Path.home()),
            parent=self.root,
        )
        if selected:
            self.parent_var.set(selected)

    def _workspace_storage_active_names(self) -> list[str]:
        if self.activity_provider is None:
            return []
        active: list[str] = []
        for workspace in self.store.list():
            try:
                activity = self.activity_provider(workspace.workspace_id)
            except Exception:
                activity = None
            if not activity:
                continue
            state = str(activity.get("state", "")).casefold()
            if bool(activity.get("active")) or state in {
                "queued",
                "waiting",
                "running",
                "cancelling",
            }:
                active.append(workspace.name)
        return active

    def choose_workspace_storage(self) -> None:
        """Select and safely relocate the complete workspace library."""

        if self.workspace_storage_busy:
            return
        current = self.store.default_root
        selected = filedialog.askdirectory(
            title="Chọn kho lưu toàn bộ workspace",
            initialdir=str(current if current.is_dir() else Path.home()),
            parent=self.root,
        )
        if not selected:
            return
        destination = Path(selected).expanduser().resolve(strict=False)
        if destination == current.resolve(strict=False):
            self.workspace_storage_status_var.set(
                "Kho workspace đang dùng chính thư mục này; không cần chuyển dữ liệu."
            )
            return

        active_names = self._workspace_storage_active_names()
        if active_names:
            messagebox.showwarning(
                "Workspace đang hoạt động",
                "Hãy dừng các workspace đang chạy trước khi chuyển kho dữ liệu:\n\n"
                + "\n".join(f"• {name}" for name in active_names),
                parent=self.root,
            )
            return

        count = len(self.store.list())
        confirmed = messagebox.askyesno(
            "Chuyển kho workspace",
            f"Chuyển toàn bộ {count} workspace sang:\n\n{destination}\n\n"
            "Tool sẽ sao chép và kiểm tra dữ liệu trước, cập nhật checkpoint/settings, "
            "sau đó đưa thư mục cũ vào Thùng rác Windows. Tiếp tục?",
            parent=self.root,
        )
        if not confirmed:
            return

        self.workspace_storage_busy = True
        self.workspace_storage_button.configure(state="disabled")
        self.workspace_storage_status_var.set(
            f"Đang chuyển {count} workspace… không đóng tool trong lúc này."
        )

        def worker() -> None:
            result: WorkspaceRelocationResult | None = None
            error = ""
            try:
                result = self.store.relocate_all(destination)
            except Exception as exc:
                error = str(exc) or exc.__class__.__name__
                record_exception("workspace-storage-relocation", exc)

            self._post_global_ui(
                lambda relocation=result, detail=error: self._finish_workspace_storage_move(
                    relocation,
                    detail,
                )
            )

        threading.Thread(
            target=worker,
            name="WorkspaceStorageRelocation",
            daemon=True,
        ).start()

    def _finish_workspace_storage_move(
        self,
        result: WorkspaceRelocationResult | None,
        error: str,
    ) -> None:
        self.workspace_storage_busy = False
        self.workspace_storage_button.configure(state="normal")
        if result is None:
            self.workspace_storage_status_var.set(
                "Chưa chuyển dữ liệu; workspace cũ vẫn được giữ nguyên."
            )
            messagebox.showerror(
                "Không thể chuyển kho workspace",
                error or "Không nhận được kết quả chuyển dữ liệu.",
                parent=self.root,
            )
            return

        self.workspace_storage_var.set(str(result.destination_root))
        self.parent_var.set(str(result.destination_root))
        self.workspace_outcomes.clear()
        self.workspace_by_id.clear()
        detail = (
            f"Đã chuyển {len(result.workspaces)} workspace sang kho mới."
        )
        if result.retained_sources:
            detail += (
                " Một số thư mục cũ không đưa được vào Thùng rác, dữ liệu mới vẫn "
                "đã được đăng ký an toàn: "
                + ", ".join(str(path) for path in result.retained_sources)
            )
        self.workspace_storage_status_var.set(detail)
        self.selection_hint_var.set(detail)
        self.refresh_async()

    def open_workspace_storage(self) -> None:
        try:
            self.store.default_root.mkdir(parents=True, exist_ok=True)
            os.startfile(str(self.store.default_root))
        except (OSError, AttributeError) as exc:
            messagebox.showerror(
                "Không mở được kho workspace",
                f"Không thể mở thư mục dữ liệu:\n{self.store.default_root}\n\n{exc}",
                parent=self.root,
            )

    def open_storage_root(self) -> None:
        try:
            self.storage_root.mkdir(parents=True, exist_ok=True)
            os.startfile(str(self.storage_root))
        except (OSError, AttributeError) as exc:
            messagebox.showerror(
                "Không mở được thư mục",
                f"Không thể mở thư mục dữ liệu:\n{self.storage_root}\n\n{exc}",
                parent=self.root,
            )

    def _durable_activity(
        self,
        workspace: Workspace,
        *,
        reconcile: bool = True,
    ) -> dict[str, object]:
        if reconcile:
            try:
                reconciled = reconcile_deleted_workspace_session(workspace)
            except Exception:
                reconciled = None
            if reconciled is not None and reconciled.changed:
                self.workspace_outcomes.pop(workspace.workspace_id, None)
        outcome = self.workspace_outcomes.get(workspace.workspace_id)
        if outcome is None:
            outcome = inspect_workspace_pipeline(
                workspace.pipeline_checkpoint_path,
                workspace.history_path,
                workspace.output_dir,
            )
            self.workspace_outcomes[workspace.workspace_id] = outcome
        activity = outcome.to_activity()
        # A closed workspace has no live Tk session, so its durable checkpoint
        # alone cannot tell the launcher that the scheduler is intentionally
        # waiting for the next slot.  Read only this small persisted flag; do
        # not start a worker or mutate the workspace from the launcher poll.
        try:
            settings = json.loads(workspace.settings_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            settings = {}
        if (
            isinstance(settings, dict)
            and bool(settings.get("schedule_armed", False))
            and str(activity.get("state", "idle")) not in {"error", "cancelled"}
        ):
            activity["queue_state"] = "queued"
            activity["queue_detail"] = "Lịch đang chờ mốc giờ tiếp theo"
            activity["stage"] = "Lịch workspace"
        if not DONUT_YOUTUBE_ENABLED:
            return activity
        manual_checkpoint_path = workspace.state_dir / "manual_youtube_upload.json"
        try:
            manual = json.loads(manual_checkpoint_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            manual = {}
        if isinstance(manual, dict) and str(manual.get("status") or "").casefold() == "manual_complete":
            activity["youtube_status"] = "manual_complete"
            activity["youtube_manual"] = True
            activity["youtube_title"] = str(manual.get("title") or "").strip()
            activity["youtube_url"] = str(manual.get("youtube_url") or "").strip()
            activity["status"] = (
                f"{activity.get('status', 'Render hoàn tất')} · YouTube đã xác nhận thủ công"
            )
        checkpoint_path = workspace.state_dir / "youtube_upload.json"
        try:
            upload = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return activity
        if not isinstance(upload, dict):
            return activity
        upload_status = str(upload.get("status") or "").casefold()
        detail = str(upload.get("detail") or "").strip()
        channel = str(upload.get("channel_name") or "").strip()
        youtube_url = str(upload.get("youtube_url") or "").strip()
        activity["youtube_status"] = upload_status
        activity["youtube_url"] = youtube_url
        if upload_status == "complete":
            suffix = f" · {channel}" if channel else ""
            activity["status"] = (
                f"{activity.get('status', 'Render hoàn tất')} · YouTube đã upload{suffix}"
            )
        elif upload_status == "error":
            activity["state"] = "error"
            activity["status"] = f"Upload YouTube lỗi · {detail or 'bấm Mở để kiểm tra'}"
        elif upload_status in {"attaching", "uploading", "submitting", "uncertain"}:
            activity["state"] = "error"
            activity["status"] = (
                "YouTube cần xác minh · lượt upload đã bị khóa để không đăng trùng · "
                f"{detail or 'mở đúng profile YouTube Studio để kiểm tra'}"
            )
        elif upload_status in {"queued", "running", "cancelled"}:
            activity["state"] = "incomplete"
            activity["status"] = (
                "Upload YouTube đang dở · mở workspace và bấm Upload/Tiếp tục"
            )
        return activity

    @staticmethod
    def _filter_matches(state: str, selected: str) -> bool:
        if selected == "Đang làm":
            return state == "running"
        if selected == "Đã render xong":
            return state == "completed"
        if selected == "Chưa hoàn tất":
            return state in {"incomplete", "cancelled"}
        if selected == "Có lỗi":
            return state == "error"
        if selected == "Chưa bắt đầu":
            return state == "idle"
        return True

    def _workspace_sort_key(
        self,
        workspace: Workspace,
        activity: dict[str, object],
    ) -> tuple[object, ...]:
        selected = self.workspace_sort_var.get()
        name = workspace.name.casefold()
        state = str(activity.get("state", "idle"))
        updated = str(activity.get("updated_at") or workspace.updated_at)
        if selected == "Cũ cập nhật trước":
            return (updated, name)
        if selected == "Tên A → Z":
            return (name, updated)
        if selected == "Tên Z → A":
            return (name, updated)
        if selected == "Video mới nhất":
            has_video = bool(activity.get("video_name"))
            return (has_video, updated, name)
        if selected == "Cần xử lý trước":
            priority = {
                "error": 0,
                "incomplete": 1,
                "cancelled": 1,
                "running": 2,
                "idle": 3,
                "completed": 4,
            }.get(state, 3)
            return (priority, name)
        return (updated, name)

    def _load_workspace_snapshot(
        self,
    ) -> tuple[
        list[Workspace],
        list[WorkspaceFolder],
        dict[str, WorkspacePipelineOutcome],
        Workspace | None,
    ]:
        """Read launcher data without touching Tk widgets.

        Checkpoint and output inspection can become noticeable when many
        workspaces are active.  Keeping this work in one small snapshot makes
        it safe to run away from the Tk thread and prevents a refresh from
        rebuilding the UI halfway through a disk scan.
        """
        all_workspaces = self.store.list()
        folders = self.store.list_folders()
        outcomes: dict[str, WorkspacePipelineOutcome] = {}
        for workspace in all_workspaces:
            try:
                outcomes[workspace.workspace_id] = inspect_workspace_pipeline(
                    workspace.pipeline_checkpoint_path,
                    workspace.history_path,
                    workspace.output_dir,
                )
            except Exception as exc:
                record_exception(
                    "workspace-refresh-inspect",
                    exc,
                    metadata={"workspace_id": workspace.workspace_id},
                )
                previous = self.workspace_outcomes.get(workspace.workspace_id)
                if previous is not None:
                    outcomes[workspace.workspace_id] = previous
                else:
                    outcomes[workspace.workspace_id] = WorkspacePipelineOutcome(
                        state="error",
                        status="Không đọc được trạng thái workspace",
                        progress=0.0,
                        updated_at=workspace.updated_at,
                    )
        return all_workspaces, folders, outcomes, self.store.last_opened()

    def refresh_async(self) -> None:
        """Refresh launcher data without blocking the Tk event loop."""
        self.workspace_refresh_after_id = None
        self.workspace_refresh_pending = True
        if self.workspace_refresh_worker_active:
            return
        self.workspace_refresh_worker_active = True
        self.workspace_refresh_pending = False
        refresh_generation = getattr(self, "workspace_refresh_generation", 0) + 1
        self.workspace_refresh_generation = refresh_generation
        button = getattr(self, "refresh_button", None)
        if isinstance(button, ttk.Button):
            button.configure(state="disabled")

        def worker() -> None:
            snapshot: tuple[
                list[Workspace],
                list[WorkspaceFolder],
                dict[str, WorkspacePipelineOutcome],
                Workspace | None,
            ] | None = None
            error = ""
            try:
                snapshot = self._load_workspace_snapshot()
            except Exception as exc:
                error = str(exc) or exc.__class__.__name__
                record_exception("workspace-refresh", exc)

            def apply() -> None:
                self.workspace_refresh_worker_active = False
                try:
                    if (
                        snapshot is not None
                        and not error
                        and refresh_generation
                        == getattr(self, "workspace_refresh_generation", 0)
                    ):
                        self._apply_workspace_snapshot(*snapshot)
                    elif error:
                        self.selection_hint_var.set(
                            f"Chưa làm mới được danh sách workspace: {error}"
                        )
                finally:
                    if self.workspace_refresh_pending:
                        self._start_pending_workspace_refresh()
                    elif isinstance(button, ttk.Button):
                        button.configure(state="normal")

            try:
                self.root.after(0, apply)
            except tk.TclError:
                self.workspace_refresh_worker_active = False

        threading.Thread(
            target=worker,
            name="WorkspaceLauncherRefresh",
            daemon=True,
        ).start()

    def _start_pending_workspace_refresh(self) -> None:
        self.workspace_refresh_pending = False
        self.workspace_refresh_worker_active = False
        self.refresh_async()

    def _apply_workspace_snapshot(
        self,
        all_workspaces: list[Workspace],
        folders: list[WorkspaceFolder],
        outcomes: dict[str, WorkspacePipelineOutcome],
        last: Workspace | None,
    ) -> None:
        self.workspace_activity_ui_cache.clear()
        self.live_output_activity_cache.clear()
        self.live_output_signature = ("__stale__",)
        try:
            previous_scroll = float(self.cards_canvas.yview()[0])
        except (tk.TclError, IndexError, TypeError, ValueError):
            previous_scroll = 0.0
        if self.reset_cards_scroll:
            previous_scroll = 0.0
            self.reset_cards_scroll = False
        for widget in self.cards_frame.winfo_children():
            widget.destroy()
        self.workspace_cards.clear()
        self.workspace_card_controls.clear()
        self.workspace_select_vars.clear()
        self.folder_drop_targets.clear()
        self.active_rename_id = ""
        self.workspace_folder_count = len(folders)
        self.workspace_by_id = {
            workspace.workspace_id: workspace for workspace in all_workspaces
        }
        self._refresh_folder_tree(folders, all_workspaces)
        self.selected_workspace_ids.intersection_update(self.workspace_by_id)
        folder_names = {
            folder.folder_id: self._folder_breadcrumb(folder.folder_id, self.folder_by_id)
            for folder in folders
        }
        self.workspace_outcomes = outcomes
        query = " ".join(self.workspace_search_var.get().casefold().split())
        selected_filter = self.workspace_filter_var.get()
        decorated: list[tuple[Workspace, dict[str, object]]] = []
        for workspace in all_workspaces:
            if self.folder_scope_id != ALL_FOLDER_ID:
                if not self.folder_scope_id and workspace.folder_id:
                    continue
                if self.folder_scope_id and workspace.folder_id != self.folder_scope_id:
                    continue
            activity = self._activity_for(workspace.workspace_id)
            searchable = " ".join(
                (
                    workspace.name,
                    workspace.category,
                    folder_names.get(workspace.folder_id, "Chưa phân loại"),
                    workspace.root_dir,
                    str(activity.get("status", "")),
                    str(activity.get("video_name", "")),
                )
            ).casefold()
            if query and query not in searchable:
                continue
            if not self._filter_matches(
                str(activity.get("state", "idle")), selected_filter
            ):
                continue
            decorated.append((workspace, activity))
        reverse = self.workspace_sort_var.get() in {
            "Mới cập nhật trước",
            "Tên Z → A",
            "Video mới nhất",
        }
        decorated.sort(
            key=lambda pair: self._workspace_sort_key(pair[0], pair[1]),
            reverse=reverse,
        )
        workspaces = [workspace for workspace, _activity in decorated]
        visible_ids = {workspace.workspace_id for workspace in workspaces}
        hidden_selected = self.selected_workspace_ids - visible_ids
        self.selected_workspace_ids.intersection_update(visible_ids)
        if hidden_selected:
            self._update_workspace_selection_hint()
        count = len(all_workspaces)
        visible_count = len(workspaces)
        self.workspace_count_var.set(
            (
                f"Hiển thị {visible_count}/{count} workspace · {len(folders)} thư mục chủ đề"
                if visible_count != count
                else f"{count} workspace độc lập · {len(folders)} thư mục chủ đề"
            )
            if count
            else "Chưa có dự án · tạo workspace đầu tiên ở khung bên trái"
        )
        for workspace in workspaces:
            self._add_workspace_card(
                workspace,
                recent=bool(last and last.workspace_id == workspace.workspace_id),
            )
        if not workspaces:
            self._add_empty_state()
        self.root.after_idle(
            lambda position=previous_scroll: (
                self.cards_canvas.configure(scrollregion=self.cards_canvas.bbox("all")),
                self.cards_canvas.yview_moveto(position),
            )
        )
        self._refresh_workspace_activity(reschedule=False)

    def refresh(self) -> None:
        """Synchronously refresh launcher data for internal/test callers."""
        self.workspace_refresh_after_id = None
        self.workspace_refresh_pending = False
        self.workspace_refresh_generation = (
            getattr(self, "workspace_refresh_generation", 0) + 1
        )
        snapshot = self._load_workspace_snapshot()
        self._apply_workspace_snapshot(*snapshot)

    def show_view(self) -> None:
        self.refresh_async()
        self.root_frame.pack(fill=BOTH, expand=True)

    def hide_view(self) -> None:
        try:
            self.root_frame.pack_forget()
        except tk.TclError:
            pass

    def _open_workspace_output(self, workspace_id: str) -> None:
        workspace = self.workspace_by_id.get(workspace_id)
        if workspace is None:
            return
        try:
            workspace.output_dir.mkdir(parents=True, exist_ok=True)
            os.startfile(str(workspace.output_dir))
        except (OSError, AttributeError) as exc:
            messagebox.showerror(
                "Không mở được Output",
                str(exc),
                parent=self.root,
            )

    def _bind_live_scroll(self, parent: tk.Misc) -> None:
        if not isinstance(parent, (tk.Text, ttk.Scrollbar)):
            for sequence in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
                parent.bind(sequence, self._scroll_live_outputs, add="+")
        for child in parent.winfo_children():
            self._bind_live_scroll(child)

    def _rebuild_realtime_rows(self, workspace_ids: tuple[str, ...]) -> None:
        for widget in self.live_frame.winfo_children():
            widget.destroy()
        self.live_output_rows.clear()
        self.live_output_activity_cache.clear()
        if not workspace_ids:
            empty = ttk.Frame(
                self.live_frame,
                style="PanelAlt.TFrame",
                padding=(16, 24),
            )
            empty.pack(fill="x")
            ttk.Label(
                empty,
                text="Chưa có output để theo dõi",
                image=self.icons["film"],
                compound="top",
                style="WorkspaceEmptyTitle.TLabel",
            ).pack()
            ttk.Label(
                empty,
                text="Khi workflow chạy, trạng thái và file đầu ra sẽ xuất hiện ở đây.",
                style="CardMuted.TLabel",
                wraplength=220,
                justify="center",
            ).pack(pady=(7, 0))
            return

        for workspace_id in workspace_ids:
            workspace = self.workspace_by_id.get(workspace_id)
            if workspace is None:
                continue
            status_var = StringVar(master=self.root, value="○  Sẵn sàng")
            percent_var = StringVar(master=self.root, value="0%")
            progress_var = tk.DoubleVar(master=self.root, value=0.0)
            output_var = StringVar(master=self.root, value="Chưa có file video")
            card = ttk.Frame(
                self.live_frame,
                style="PanelAlt.TFrame",
                padding=(10, 9),
            )
            card.pack(fill="x", pady=(0, 7))
            ttk.Label(
                card,
                text=workspace.name,
                style="WorkspaceLiveTitle.TLabel",
                wraplength=230,
                justify=LEFT,
            ).pack(anchor="w")
            status_row = ttk.Frame(card, style="PanelAlt.TFrame")
            status_row.pack(fill="x", pady=(4, 3))
            status_label = ttk.Label(
                status_row,
                textvariable=status_var,
                style="WorkspaceCardMeta.TLabel",
                wraplength=185,
                justify=LEFT,
            )
            status_label.pack(side=LEFT, fill="x", expand=True)
            ttk.Label(
                status_row,
                textvariable=percent_var,
                style="WorkspaceCardMeta.TLabel",
            ).pack(side=RIGHT, padx=(6, 0))
            ttk.Progressbar(
                card,
                orient=HORIZONTAL,
                mode="determinate",
                maximum=100.0,
                variable=progress_var,
            ).pack(fill="x", pady=(0, 5))
            ttk.Label(
                card,
                textvariable=output_var,
                image=self.icons["film"],
                compound=LEFT,
                style="WorkspaceCardMeta.TLabel",
                wraplength=230,
                justify=LEFT,
            ).pack(anchor="w", pady=(0, 6))
            actions = ttk.Frame(card, style="PanelAlt.TFrame")
            actions.pack(fill="x")
            ttk.Button(
                actions,
                text="Output",
                image=self.icons["folder"],
                compound=LEFT,
                style="Secondary.TButton",
                command=lambda item_id=workspace_id: self._open_workspace_output(item_id),
            ).pack(side=LEFT)
            ttk.Button(
                actions,
                text="Mở",
                image=self.icons["play_dark"],
                compound=LEFT,
                style="Accent.TButton",
                command=lambda item=workspace: self._open_workspace(item),
            ).pack(side=RIGHT)
            self.live_output_rows[workspace_id] = {
                "status_var": status_var,
                "percent_var": percent_var,
                "progress_var": progress_var,
                "output_var": output_var,
                "status_label": status_label,
            }
            self._bind_live_scroll(card)

    def _refresh_realtime_panel(
        self,
        activities: dict[str, dict[str, object]] | None = None,
    ) -> None:
        if activities is None:
            activities = {
                workspace_id: self._activity_for(workspace_id)
                for workspace_id in self.workspace_by_id
            }
        priority = {
            "running": 0,
            "error": 1,
            "incomplete": 2,
            "cancelled": 2,
            "completed": 3,
            "idle": 4,
        }
        ordered = tuple(
            workspace.workspace_id
            for workspace in sorted(
                self.workspace_by_id.values(),
                key=lambda item: (
                    priority.get(
                        str(activities[item.workspace_id].get("state", "idle")),
                        4,
                    ),
                    item.name.casefold(),
                ),
            )
        )
        if ordered != self.live_output_signature:
            self.live_output_signature = ordered
            self._rebuild_realtime_rows(ordered)

        running = 0
        attention = 0
        completed = 0
        for workspace_id, controls in self.live_output_rows.items():
            activity = activities.get(workspace_id, {})
            state = str(activity.get("state", "idle"))
            if bool(activity.get("active", False)):
                running += 1
            if state in {"error", "incomplete", "cancelled"}:
                attention += 1
            if state == "completed":
                completed += 1
            try:
                progress = max(0.0, min(100.0, float(activity.get("progress", 0.0))))
            except (TypeError, ValueError):
                progress = 0.0
            prefix = {
                "running": "● ĐANG LÀM",
                "completed": "✓ HOÀN TẤT",
                "incomplete": "◐ CHƯA XONG",
                "error": "! CÓ LỖI",
                "cancelled": "○ ĐÃ HỦY",
            }.get(state, "○ SẴN SÀNG")
            status = str(activity.get("status", "Sẵn sàng"))
            ui_signature = (
                state,
                bool(activity.get("active", False)),
                round(progress, 3),
                status,
                str(activity.get("video_name", "")).strip(),
            )
            if self.live_output_activity_cache.get(workspace_id) == ui_signature:
                continue
            self.live_output_activity_cache[workspace_id] = ui_signature
            controls["status_var"].set(f"{prefix} · {status}")
            controls["percent_var"].set(f"{round(progress)}%")
            controls["progress_var"].set(progress)
            video_name = str(activity.get("video_name", "")).strip()
            controls["output_var"].set(video_name or "Chưa có file video")
            status_label = controls.get("status_label")
            if isinstance(status_label, ttk.Label):
                status_label.configure(
                    style={
                        "running": "WorkspaceCardRunning.TLabel",
                        "completed": "WorkspaceCardReady.TLabel",
                        "incomplete": "WorkspaceCardWarning.TLabel",
                        "cancelled": "WorkspaceCardWarning.TLabel",
                        "error": "WorkspaceCardError.TLabel",
                    }.get(state, "WorkspaceCardMeta.TLabel")
                )
        if running:
            summary = f"{running} đang chạy"
            if attention:
                summary += f" · {attention} cần kiểm tra"
        elif attention:
            summary = f"{attention} workspace cần kiểm tra · không có tác vụ đang chạy"
        elif self.workspace_by_id:
            summary = f"Không có tác vụ đang chạy · {completed} workspace đã hoàn tất"
        else:
            summary = "Chưa có tác vụ · bảng này luôn hiện khi workflow bắt đầu"
        self.live_summary_var.set(summary)

    def _activity_for(self, workspace_id: str) -> dict[str, object]:
        workspace = self.workspace_by_id.get(workspace_id)
        value: dict[str, object] | None = None
        if self.activity_provider is not None:
            try:
                candidate = self.activity_provider(workspace_id)
                value = candidate if isinstance(candidate, dict) else None
            except Exception:
                value = None
        durable = (
            self._durable_activity(
                workspace,
                reconcile=not bool(value and value.get("active", False)),
            )
            if workspace is not None
            else {
                "active": False,
                "progress": 0.0,
                "status": "Chưa bắt đầu render",
                "state": "idle",
            }
        )
        if value is None:
            return durable
        live_state = str(value.get("state", "idle"))
        durable_state = str(durable.get("state", "idle"))
        if not bool(value.get("active")) and live_state == "idle" and durable_state != "idle":
            return durable
        merged = dict(durable)
        merged.update(value)
        if not value.get("video_name"):
            for key in ("video_name", "video_path", "video_count"):
                merged[key] = durable.get(key, "" if key != "video_count" else 0)
        return merged

    @staticmethod
    def _queue_state_for_activity(activity: dict[str, object]) -> str:
        """Normalize live activity into a control-panel queue state."""

        raw = str(activity.get("queue_state") or "").strip().casefold()
        if raw in {"running", "queued", "running_queued", "error", "incomplete", "cancelled", "completed", "idle"}:
            return raw
        status = str(activity.get("status") or "").casefold()
        active = bool(activity.get("active", False))
        waiting = any(
            marker in status
            for marker in ("đang chờ", "hàng chờ", "xếp hàng", "tạm nghỉ", "chờ tài nguyên")
        )
        if waiting and active:
            return "queued"
        if active:
            return "running"
        return str(activity.get("state") or "idle").casefold() or "idle"

    @staticmethod
    def _queue_display_value(value: object, limit: int) -> str:
        text = " ".join(str(value or "").split())
        if len(text) <= limit:
            return text
        return text[: max(1, limit - 1)].rstrip() + "…"

    def _refresh_workspace_queue_panel(
        self,
        activities: dict[str, dict[str, object]],
    ) -> None:
        """Show every workspace's live slot state in one stable, compact table."""

        tree = self.workspace_queue_tree
        if tree is None:
            return
        rows: list[tuple[Workspace, str, str, str, str]] = []
        counts = {"running": 0, "queued": 0, "attention": 0, "ready": 0}
        priority = {
            "running_queued": 0,
            "running": 1,
            "queued": 2,
            "error": 3,
            "incomplete": 4,
            "cancelled": 4,
            "completed": 5,
            "idle": 6,
        }
        for workspace in self.workspace_by_id.values():
            activity = activities.get(workspace.workspace_id, {})
            queue_state = self._queue_state_for_activity(activity)
            base_state = str(activity.get("state") or "idle").casefold()
            if queue_state in {"running", "running_queued"}:
                counts["running"] += 1
            if queue_state in {"queued", "running_queued"}:
                counts["queued"] += 1
            queue_detail_lower = str(activity.get("queue_detail") or "").casefold()
            scheduled_waiting = (
                queue_state == "queued"
                and "lịch đang chờ" in queue_detail_lower
            )
            if (
                base_state in {"error", "incomplete", "cancelled"}
                and not scheduled_waiting
            ):
                counts["attention"] += 1
            if queue_state == "idle" and base_state not in {"completed", "error", "incomplete", "cancelled"}:
                counts["ready"] += 1

            labels = {
                "running_queued": ("LÀM + CHỜ", "queued"),
                "running": ("ĐANG LÀM", "running"),
                "queued": ("HÀNG CHỜ", "queued"),
                "error": ("LỖI", "attention"),
                "incomplete": ("CẦN XỬ LÝ", "attention"),
                "cancelled": ("ĐÃ HỦY", "attention"),
                "completed": ("ĐÃ XONG", "completed"),
                "idle": ("SẴN SÀNG", "idle"),
            }
            state_label, tag = labels.get(queue_state, ("SẴN SÀNG", "idle"))
            stage = str(activity.get("stage") or "—").strip() or "—"
            detail = str(activity.get("queue_detail") or "").strip()
            if not detail:
                detail = str(activity.get("status") or "Sẵn sàng").strip()
            if queue_state == "running_queued" and detail:
                detail = f"{stage} · {detail}"
            elif queue_state == "running" and stage and stage != "—":
                detail = f"{stage} · {detail}"
            detail = self._queue_display_value(detail, 80)
            rows.append((workspace, queue_state, state_label, detail, tag))

        rows.sort(
            key=lambda row: (
                priority.get(row[1], 9),
                row[0].name.casefold(),
            )
        )
        signature = tuple(
            (
                workspace.workspace_id,
                queue_state,
                state_label,
                detail,
                tag,
            )
            for workspace, queue_state, state_label, detail, tag in rows
        )
        if signature != self.workspace_queue_signature:
            for item_id in tree.get_children(""):
                tree.delete(item_id)
            self.workspace_queue_items.clear()
            for workspace, _queue_state, state_label, detail, tag in rows:
                item_id = tree.insert(
                    "",
                    "end",
                    values=(
                        self._queue_display_value(workspace.name, 19),
                        state_label,
                        detail,
                    ),
                    tags=(tag,),
                )
                self.workspace_queue_items[item_id] = workspace.workspace_id
            selected_items = [
                item_id
                for item_id, workspace_id in self.workspace_queue_items.items()
                if workspace_id in self.selected_workspace_ids
            ]
            if selected_items:
                tree.selection_set(*selected_items)
            self.workspace_queue_signature = signature
        self.workspace_queue_summary_var.set(
            f"Đang làm {counts['running']} · Hàng chờ {counts['queued']} · "
            f"Cần xử lý {counts['attention']} · Sẵn sàng {counts['ready']}"
        )
        self._refresh_workspace_queue_selection_count()

    def _open_selected_queue_workspace(self, _event: object | None = None) -> str:
        tree = self.workspace_queue_tree
        if tree is None:
            return "break"
        selection = tree.selection()
        if not selection:
            return "break"
        workspace_id = self.workspace_queue_items.get(selection[0], "")
        workspace = self.workspace_by_id.get(workspace_id)
        if workspace is not None:
            self._open_workspace(workspace)
        return "break"

    def _refresh_workspace_activity(self, *, reschedule: bool = True) -> None:
        # Polling every 400 ms across all workspaces makes the launcher compete
        # with the pipeline for the Tk thread.  The next poll is scheduled
        # adaptively below and each card skips unchanged UI state.
        self.workspace_activity_after_id = None
        try:
            if not self.root_frame.winfo_exists():
                return
        except tk.TclError:
            return
        self._refresh_render_queue_status()
        self._refresh_chatgpt_queue_status()
        activities = {
            workspace_id: self._activity_for(workspace_id)
            for workspace_id in self.workspace_by_id
        }
        active_count = sum(
            bool(activity.get("active", False)) for activity in activities.values()
        )
        self._refresh_workspace_queue_panel(activities)
        self._refresh_realtime_panel(activities)
        for workspace_id, controls in tuple(self.workspace_card_controls.items()):
            activity = activities.get(workspace_id, {})
            active = bool(activity.get("active", False))
            try:
                progress = max(0.0, min(100.0, float(activity.get("progress", 0.0))))
            except (TypeError, ValueError):
                progress = 0.0
            status = str(activity.get("status", "Sẵn sàng"))
            state = str(activity.get("state", "idle"))
            prefix = {
                "running": "●  ĐANG LÀM",
                "completed": "✓  HOÀN TẤT",
                "incomplete": "◐  CHƯA XONG",
                "error": "!  CÓ LỖI",
                "cancelled": "○  ĐÃ HỦY",
            }.get(state, "○  SẴN SÀNG")
            video_name = str(activity.get("video_name", "")).strip()
            try:
                video_count = max(0, int(activity.get("video_count", 0)))
            except (TypeError, ValueError):
                video_count = 0
            workspace = self.workspace_by_id.get(workspace_id)
            package = (
                self._workspace_package_path(workspace)
                if workspace is not None
                else None
            )
            manual_confirmed = bool(activity.get("youtube_manual", False)) or str(
                activity.get("youtube_status", "")
            ).casefold() == "complete"
            ui_signature = (
                active,
                round(progress, 3),
                status,
                state,
                video_name,
                video_count,
                package is not None,
                manual_confirmed,
            )
            if self.workspace_activity_ui_cache.get(workspace_id) == ui_signature:
                continue
            self.workspace_activity_ui_cache[workspace_id] = ui_signature
            status_var = controls.get("activity_status_var")
            badge_var = controls.get("activity_badge_var")
            badge_label = controls.get("activity_badge_label")
            progress_var = controls.get("activity_progress_var")
            percent_var = controls.get("activity_percent_var")
            delete_button = controls.get("delete_button")
            checkpoint_button = controls.get("checkpoint_button")
            status_label = controls.get("activity_status_label")
            video_var = controls.get("activity_video_var")
            video_button = controls.get("video_button")
            package_button = controls.get("package_button")
            manual_upload_var = controls.get("manual_upload_var")
            manual_upload_check = controls.get("manual_upload_check")
            if isinstance(status_var, StringVar):
                status_var.set(f"{prefix}  ·  {status}")
            if isinstance(badge_var, StringVar):
                badge_var.set(
                    {
                        "running": "● ĐANG LÀM",
                        "completed": "✓ ĐÃ RENDER XONG",
                        "incomplete": "◐ CHƯA HOÀN TẤT",
                        "cancelled": "○ ĐÃ HỦY",
                        "error": "! CÓ LỖI",
                    }.get(state, "○ CHƯA BẮT ĐẦU")
                )
            if isinstance(progress_var, tk.DoubleVar):
                progress_var.set(progress)
            if isinstance(percent_var, StringVar):
                percent_var.set(f"{round(progress)}%")
            if isinstance(delete_button, ttk.Button):
                delete_button.configure(state="disabled" if active else "normal")
            if isinstance(checkpoint_button, ttk.Button):
                checkpoint_button.configure(
                    state="disabled" if active else "normal"
                )
            if isinstance(status_label, ttk.Label):
                activity_style = {
                    "running": "WorkspaceCardRunning.TLabel",
                    "completed": "WorkspaceCardReady.TLabel",
                    "incomplete": "WorkspaceCardWarning.TLabel",
                    "cancelled": "WorkspaceCardWarning.TLabel",
                    "error": "WorkspaceCardError.TLabel",
                }.get(state, "WorkspaceCardMeta.TLabel")
                status_label.configure(style=activity_style)
                if isinstance(badge_label, ttk.Label):
                    badge_label.configure(style=activity_style)
            if isinstance(video_var, StringVar):
                if video_name:
                    video_var.set(
                        f"VIDEO ĐẦU RA · {video_name}"
                        + (f" · còn {video_count - 1} video khác" if video_count > 1 else "")
                    )
                elif state == "error":
                    video_var.set("VIDEO ĐẦU RA · Chưa có video hoàn chỉnh · mở log để sửa")
                elif state in {"incomplete", "cancelled", "running"}:
                    video_var.set("VIDEO ĐẦU RA · Chưa xong · checkpoint đã được giữ")
                else:
                    video_var.set("VIDEO ĐẦU RA · Chưa có file video")
            if isinstance(video_button, ttk.Button):
                video_button.configure(state="normal" if video_name else "disabled")
            if isinstance(package_button, ttk.Button):
                package_button.configure(
                    state="normal" if package is not None else "disabled"
                )
            if isinstance(manual_upload_var, tk.BooleanVar):
                manual_upload_var.set(manual_confirmed)
            if isinstance(manual_upload_check, ttk.Checkbutton):
                can_confirm = bool(video_name and package is not None and not active)
                manual_upload_check.configure(
                    state="disabled" if manual_confirmed else ("normal" if can_confirm else "disabled")
                )

        workspace_count = len(self.workspace_by_id)
        visible_count = len(self.workspace_card_controls)
        if workspace_count:
            suffix = (
                f" · {active_count} đang làm nền · tiến trình cập nhật thời gian thực"
                if active_count
                else " · tất cả đang sẵn sàng"
            )
            visible = (
                f"Hiển thị {visible_count}/{workspace_count} workspace"
                if visible_count != workspace_count
                else f"{workspace_count} workspace độc lập"
            )
            self.workspace_count_var.set(
                f"{visible} · {self.workspace_folder_count} thư mục chủ đề{suffix}"
            )
        signature = tuple(
            (
                workspace_id,
                str((activity := activities.get(workspace_id, {})).get("state", "idle")),
                str(activity.get("video_name", "")),
            )
            for workspace_id in self.workspace_by_id
        )
        previous_signature = self.workspace_activity_signature
        self.workspace_activity_signature = signature
        dynamic_order = self.workspace_sort_var.get() in {
            "Cần xử lý trước",
            "Mới cập nhật trước",
            "Video mới nhất",
        }
        if (
            reschedule
            and previous_signature
            and signature != previous_signature
            and (
                self.workspace_filter_var.get() != WORKSPACE_FILTERS[0]
                or dynamic_order
            )
        ):
            self.root.after_idle(self.refresh_async)
            self._schedule_workspace_activity_tick(active_count)
            return
        if reschedule:
            self._schedule_workspace_activity_tick(active_count)

    def _schedule_workspace_activity_tick(self, active_count: int = 0) -> None:
        """Schedule one coalesced activity poll with a workload-aware delay."""
        pending = self.workspace_activity_after_id
        if pending:
            try:
                self.root.after_cancel(pending)
            except tk.TclError:
                pass
        workspace_count = len(self.workspace_by_id)
        if workspace_count > 12:
            delay = 1200
        elif active_count:
            delay = 800
        else:
            delay = 1400
        try:
            self.workspace_activity_after_id = self.root.after(
                delay,
                self._refresh_workspace_activity,
            )
        except tk.TclError:
            self.workspace_activity_after_id = None

    def create_workspace(self) -> None:
        try:
            workspace = self.store.create(
                self.name_var.get(),
                category=self.category_var.get(),
            )
        except Exception as exc:
            record_exception("workspace-create", exc)
            messagebox.showerror("Không thể tạo workspace", str(exc), parent=self.root)
            return
        self._open_workspace(workspace)

    def _open_workspace(self, workspace: Workspace) -> None:
        try:
            workspace = self.store.touch(workspace)
        except Exception as exc:
            record_exception(
                "workspace-open",
                exc,
                metadata={"workspace_id": workspace.workspace_id},
            )
            messagebox.showerror(
                "Không thể mở workspace",
                str(exc),
                parent=self.root,
            )
            return
        self.selection_hint_var.set(f"Đang mở {workspace.name}…")
        self.on_open(workspace)

    def open_selected(self) -> None:
        """Compatibility helper: open the most recently used workspace."""
        workspace = self.store.last_opened()
        if workspace is None:
            messagebox.showinfo(
                "Chưa có workspace",
                "Hãy tạo workspace đầu tiên ở khung bên trái.",
                parent=self.root,
            )
            return
        self._open_workspace(workspace)
