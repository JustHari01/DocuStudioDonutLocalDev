from __future__ import annotations

import tkinter as tk
import tempfile
import unittest
from pathlib import Path
from tkinter import ttk
from unittest.mock import Mock, patch

from docurandom.pipeline_checkpoint import update_pipeline_stage
from docurandom.overlay_editor import OverlayEditorState, VideoOverlayEditorDialog
from docurandom.flow_agent import FlowAgentStatus
from docurandom.ui import DocuRandomApp
from docurandom.workspace import WorkspaceStore
from docurandom.workspace_ui import WorkspaceLauncher, _clean_update_notes


class UiWidgetInteractionTests(unittest.TestCase):
    def test_update_notes_hide_github_links_but_keep_changelog_text(self) -> None:
        notes = (
            "**Full Changelog**: https://github.com/example/private/commits/v1\n"
            "- Sửa lỗi tải Master Prompt.\n"
            "Chi tiết: [bản phát hành](https://github.com/example/release)"
        )
        clean = _clean_update_notes(notes)
        self.assertNotIn("http", clean)
        self.assertNotIn("Full Changelog", clean)
        self.assertIn("Sửa lỗi tải Master Prompt", clean)
        self.assertIn("bản phát hành", clean)

    @classmethod
    def setUpClass(cls) -> None:
        # The bundled Windows Tcl runtime is intended for one application root
        # and can fail while repeatedly tearing down/recreating the interpreter.
        # Production also owns exactly one Tk root, so the interaction tests do
        # the same and clear their widgets between cases.
        cls.root = tk.Tk()
        cls.root.geometry("320x140+3000+3000")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.root.destroy()

    def setUp(self) -> None:
        self.app = DocuRandomApp.__new__(DocuRandomApp)
        self.app.root = self.root
        self.app.icons = self.app._build_icons()
        self.app._configure_style()

    def tearDown(self) -> None:
        for widget in self.root.winfo_children():
            widget.destroy()

    def test_donut_combobox_popup_can_be_posted(self) -> None:
        value = tk.StringVar(master=self.root, value="Một")
        combo = ttk.Combobox(
            self.root,
            style="Donut.TCombobox",
            textvariable=value,
            values=("Một", "Hai", "Ba"),
            state="readonly",
        )
        combo.pack()
        self.root.update()

        popdown = self.root.tk.call(
            "ttk::combobox::PopdownWindow", combo._w
        )
        self.root.tk.call("ttk::combobox::Post", combo._w)
        self.assertEqual(
            int(self.root.tk.call("winfo", "ismapped", popdown)), 1
        )
        self.root.tk.call("ttk::combobox::Unpost", combo._w)

    def test_donut_spinbox_plus_and_minus_change_value(self) -> None:
        value = tk.IntVar(master=self.root, value=2)
        spinbox = ttk.Spinbox(
            self.root,
            style="Donut.TSpinbox",
            from_=1,
            to=4,
            increment=1,
            textvariable=value,
        )
        spinbox.pack()
        self.root.update()
        x = max(1, spinbox.winfo_width() - 5)

        spinbox.event_generate("<ButtonPress-1>", x=x, y=4)
        spinbox.event_generate("<ButtonRelease-1>", x=x, y=4)
        self.root.update_idletasks()
        self.assertEqual(value.get(), 3)

        spinbox.event_generate(
            "<ButtonPress-1>", x=x, y=max(5, spinbox.winfo_height() - 4)
        )
        spinbox.event_generate(
            "<ButtonRelease-1>", x=x, y=max(5, spinbox.winfo_height() - 4)
        )
        self.root.update_idletasks()
        self.assertEqual(value.get(), 2)

    def test_selected_main_tab_is_larger_and_higher_contrast(self) -> None:
        style = ttk.Style(self.root)
        selected_padding = tuple(
            int(value)
            for value in self.root.tk.splitlist(
                style.lookup("TNotebook.Tab", "padding", ("selected",))
            )
        )
        idle_padding = tuple(
            int(value)
            for value in self.root.tk.splitlist(
                style.lookup("TNotebook.Tab", "padding", ("!selected",))
            )
        )

        self.assertGreater(sum(selected_padding), sum(idle_padding))
        self.assertEqual(
            style.lookup("TNotebook.Tab", "background", ("selected",)),
            "#ffffff",
        )
        self.assertEqual(
            style.lookup("TNotebook.Tab", "foreground", ("selected",)),
            "#070707",
        )

    def test_overlay_editor_reuses_theme_and_responsive_split_panel(self) -> None:
        dialog = VideoOverlayEditorDialog(
            self.root,
            initial=OverlayEditorState(),
            video_size=(1920, 1080),
            background_path=None,
            ffmpeg_path=None,
            framing="blur",
            on_apply=lambda _state: None,
        )
        self.root.update()
        try:
            self.assertEqual(dialog.window.cget("background"), "#070707")
            self.assertEqual(len(dialog.body_pane.panes()), 2)
            self.assertEqual(dialog.apply_button.cget("style"), "Accent.TButton")
            self.assertGreater(dialog.canvas_width, dialog.canvas_height)
        finally:
            dialog.close()

    def test_schedule_buttons_have_distinct_start_and_cancel_hierarchy(self) -> None:
        style = ttk.Style(self.root)

        self.assertEqual(
            style.lookup("ScheduleStart.TButton", "background"),
            "#61c554",
        )
        self.assertEqual(
            style.lookup("ScheduleCancel.TButton", "foreground"),
            "#ec6a5e",
        )
        self.assertNotEqual(
            style.lookup("ScheduleStart.TButton", "background"),
            style.lookup("ScheduleCancel.TButton", "background"),
        )

    def test_pipeline_input_controls_gray_and_restore_their_native_state(self) -> None:
        render_entry = ttk.Entry(self.root)
        render_combo = ttk.Combobox(
            self.root,
            values=("Một", "Hai"),
            state="readonly",
        )
        youtube_entry = ttk.Entry(self.root)
        self.app.pipeline_input_controls = {}
        self.app.pipeline_inputs_status_var = tk.StringVar(master=self.root, value="")
        self.app.rendering = True
        self.app.youtube_upload_busy = False
        self.app._remember_pipeline_input_control("render", render_entry)
        self.app._remember_pipeline_input_control("render", render_combo)
        self.app._remember_pipeline_input_control("youtube", youtube_entry)

        self.app._refresh_pipeline_input_controls()

        self.assertEqual(str(render_entry.cget("state")), "disabled")
        self.assertEqual(str(render_combo.cget("state")), "disabled")
        self.assertEqual(str(youtube_entry.cget("state")), "normal")
        self.assertIn("render", self.app.pipeline_inputs_status_var.get())

        self.app.rendering = False
        self.app._refresh_pipeline_input_controls()

        self.assertEqual(str(render_entry.cget("state")), "normal")
        self.assertEqual(str(render_combo.cget("state")), "readonly")
        self.assertEqual(str(youtube_entry.cget("state")), "normal")

    def test_workspace_hub_reuses_editor_theme_and_card_controls(self) -> None:
        with tempfile.TemporaryDirectory(prefix="DocuRandomWorkspaceUi_") as temp:
            root = Path(temp)
            store = WorkspaceStore(root / "registry.json", root / "workspaces")
            workspace = store.create("Tin Nhật")
            launcher = WorkspaceLauncher(self.root, store, lambda _workspace: None)
            self.root.update()

            style = ttk.Style(self.root)
            self.assertEqual(style.lookup("Accent.TButton", "background"), "#ffffff")
            self.assertEqual(
                style.lookup("Donut.TCombobox", "fieldbackground", ("readonly",)),
                "#161616",
            )
            self.assertIn(workspace.workspace_id, launcher.workspace_cards)
            self.assertEqual(launcher.category_combo.cget("style"), "Donut.TCombobox")
            self.assertEqual(launcher.create_button.cget("style"), "Smart.TButton")
            self.assertEqual(launcher.cards_scrollbar.cget("style"), "Vertical.TScrollbar")

    def test_workspace_hub_controls_one_global_render_queue(self) -> None:
        with tempfile.TemporaryDirectory(prefix="DocuRandomGlobalRenderUi_") as temp:
            root = Path(temp)
            store = WorkspaceStore(root / "registry.json", root / "workspaces")
            state = {"limit": 2, "active": 2, "waiting": 3}

            def set_limit(value: int) -> int:
                state["limit"] = value
                return value

            launcher = WorkspaceLauncher(
                self.root,
                store,
                lambda _workspace: None,
                render_queue_provider=lambda: dict(state),
                render_limit_handler=set_limit,
            )
            self.root.update()
            self.assertEqual(launcher.global_render_limit_var.get(), "2")
            self.assertIn("3 video đang xếp hàng", launcher.global_render_queue_var.get())

            launcher.global_render_limit_var.set("1")
            launcher._global_render_limit_changed()
            self.assertEqual(state["limit"], 1)
            self.assertIn("toàn bộ DocuRandom", launcher.selection_hint_var.get())
            self.assertIn("Đang hoàn tất 2 video", launcher.global_render_queue_var.get())

            state.update(active=1, waiting=4)
            launcher._refresh_render_queue_status()
            self.assertIn("Đang render 1/1", launcher.global_render_queue_var.get())
            self.assertIn("4 video đang xếp hàng", launcher.global_render_queue_var.get())

            launcher.render_limit_handler = Mock(side_effect=OSError("read-only"))
            launcher.global_render_limit_var.set("4")
            with patch("docurandom.workspace_ui.record_exception"):
                launcher._global_render_limit_changed()
            launcher._refresh_render_queue_status()
            self.assertEqual(launcher.global_render_limit_var.get(), "1")
            self.assertIn("Chưa lưu được", launcher.global_render_queue_var.get())
            launcher.render_limit_handler = set_limit
            launcher.global_render_limit_var.set("2")
            launcher._global_render_limit_changed()
            self.assertNotIn("Chưa lưu được", launcher.global_render_queue_var.get())

    def test_workspace_hub_exposes_one_global_flow_and_smm_control(self) -> None:
        with tempfile.TemporaryDirectory(prefix="DocuRandomGlobalServicesUi_") as temp:
            root = Path(temp)
            store = WorkspaceStore(root / "registry.json", root / "workspaces")
            status = FlowAgentStatus(
                Path("edge.exe"),
                Path("extension"),
                True,
                True,
                True,
                False,
                extension_version="1.3.40",
                browser_open=True,
                flow_tab_count=1,
                ui_image_capable=True,
            )
            with patch(
                "docurandom.workspace_ui.load_smm_api_key",
                return_value="unit-test-api-key",
            ):
                launcher = WorkspaceLauncher(
                    self.root,
                    store,
                    lambda _workspace: None,
                    flow_status_provider=lambda: status,
                    flow_start_handler=lambda: status,
                )
            self.root.update()

            self.assertTrue(hasattr(launcher, "global_flow_start_button"))
            self.assertTrue(hasattr(launcher, "global_smm_api_key_entry"))
            self.assertIn("mã hóa", launcher.global_smm_status_var.get())
            launcher._format_global_flow_status(status)
            self.assertIn("FLOW", launcher.global_flow_status_var.get())
            self.assertIn("sẵn sàng", launcher.global_flow_status_var.get().casefold())

    def test_workspace_hub_defaults_to_one_global_chatgpt_queue(self) -> None:
        with tempfile.TemporaryDirectory(prefix="DocuRandomGlobalChatGPTUi_") as temp:
            root = Path(temp)
            store = WorkspaceStore(root / "registry.json", root / "workspaces")
            state: dict[str, object] = {
                "serial": True,
                "limit": 1,
                "recommended_limit": 3,
                "active": 1,
                "waiting": 2,
                "rate_limited": False,
            }

            def set_serial(enabled: bool) -> bool:
                state["serial"] = enabled
                state["limit"] = 1 if enabled else 3
                return enabled

            launcher = WorkspaceLauncher(
                self.root,
                store,
                lambda _workspace: None,
                chatgpt_queue_provider=lambda: dict(state),
                chatgpt_serial_handler=set_serial,
            )
            self.root.update()
            self.assertTrue(launcher.chatgpt_serial_var.get())
            launcher._refresh_chatgpt_queue_status()
            self.assertIn("2 lượt ChatGPT đang chờ", launcher.chatgpt_queue_var.get())

            launcher.chatgpt_serial_var.set(False)
            launcher._chatgpt_serial_changed()
            self.assertFalse(state["serial"])
            self.assertIn("song song", launcher.selection_hint_var.get())

    def test_workspace_hub_recovers_video_name_and_filters_after_restart(self) -> None:
        with tempfile.TemporaryDirectory(prefix="DocuRandomWorkspaceOutcome_") as temp:
            root = Path(temp)
            store = WorkspaceStore(root / "registry.json", root / "workspaces")
            completed = store.create("Đã dựng")
            incomplete = store.create("Đang dở")
            video = completed.output_dir / "tin-nhat-hoan-tat.mp4"
            video.write_bytes(b"video")
            update_pipeline_stage(
                completed.pipeline_checkpoint_path,
                identity={"workspace_id": completed.workspace_id},
                stage="render",
                status="complete",
                artifacts={"video": str(video)},
            )
            update_pipeline_stage(
                incomplete.pipeline_checkpoint_path,
                identity={"workspace_id": incomplete.workspace_id},
                stage="flow_images",
                status="error",
                detail="CAPTCHA_TIMEOUT",
            )

            launcher = WorkspaceLauncher(self.root, store, lambda _workspace: None)
            self.root.update()
            complete_controls = launcher.workspace_card_controls[completed.workspace_id]
            error_controls = launcher.workspace_card_controls[incomplete.workspace_id]

            self.assertIn("HOÀN TẤT", complete_controls["activity_status_var"].get())
            self.assertEqual(
                complete_controls["activity_badge_var"].get(),
                "✓ ĐÃ RENDER XONG",
            )
            self.assertIn(video.name, complete_controls["activity_video_var"].get())
            self.assertEqual(str(complete_controls["video_button"].cget("state")), "normal")
            self.assertIn("CÓ LỖI", error_controls["activity_status_var"].get())

            launcher.workspace_filter_var.set("Đã render xong")
            launcher.refresh()
            self.assertEqual(
                tuple(launcher.workspace_card_controls),
                (completed.workspace_id,),
            )

            launcher.workspace_filter_var.set("Tất cả trạng thái")
            launcher.workspace_search_var.set(video.name)
            launcher.refresh()
            self.assertEqual(
                tuple(launcher.workspace_card_controls),
                (completed.workspace_id,),
            )

    def test_workspace_card_opens_exact_exported_content_package(self) -> None:
        with tempfile.TemporaryDirectory(prefix="DocuRandomWorkspacePackage_") as temp:
            root = Path(temp)
            store = WorkspaceStore(root / "registry.json", root / "workspaces")
            workspace = store.create("Tin Nhật")
            package = workspace.output_dir / "Tên video Nhật.txt"
            package.write_text("TIÊU ĐỀ\n\nタイトル", encoding="utf-8-sig")
            transcript = workspace.output_dir / "Tên video Nhật_CHAT.txt"
            transcript.write_text("chat phụ", encoding="utf-8-sig")
            update_pipeline_stage(
                workspace.pipeline_checkpoint_path,
                identity={"workspace_id": workspace.workspace_id},
                stage="package",
                status="complete",
                artifacts={"path": str(package)},
            )

            launcher = WorkspaceLauncher(self.root, store, lambda _workspace: None)
            launcher._refresh_workspace_activity(reschedule=False)
            controls = launcher.workspace_card_controls[workspace.workspace_id]

            self.assertEqual(
                str(controls["package_button"].cget("state")),
                "normal",
            )
            self.assertEqual(
                launcher._workspace_package_path(workspace),
                package.resolve(strict=False),
            )
            with patch("docurandom.workspace_ui.os.startfile", create=True) as startfile:
                launcher._open_workspace_package(workspace.workspace_id)
            startfile.assert_called_once_with(str(package.resolve(strict=False)))
            self.assertIn(package.name, launcher.selection_hint_var.get())

    def test_workspace_card_disables_package_button_until_txt_exists(self) -> None:
        with tempfile.TemporaryDirectory(prefix="DocuRandomWorkspaceNoPackage_") as temp:
            root = Path(temp)
            store = WorkspaceStore(root / "registry.json", root / "workspaces")
            workspace = store.create("Chưa viết")
            launcher = WorkspaceLauncher(self.root, store, lambda _workspace: None)
            launcher._refresh_workspace_activity(reschedule=False)
            controls = launcher.workspace_card_controls[workspace.workspace_id]

            self.assertEqual(
                str(controls["package_button"].cget("state")),
                "disabled",
            )

    def test_workspace_card_checkpoint_reset_requires_confirmation(self) -> None:
        with tempfile.TemporaryDirectory(prefix="DocuRandomWorkspaceReset_") as temp:
            root = Path(temp)
            store = WorkspaceStore(root / "registry.json", root / "workspaces")
            workspace = store.create("Reset test")
            handler = Mock(return_value=(True, "Đã xóa checkpoint."))
            launcher = WorkspaceLauncher(
                self.root,
                store,
                lambda _workspace: None,
                checkpoint_reset_handler=handler,
            )
            controls = launcher.workspace_card_controls[workspace.workspace_id]

            self.assertIn("checkpoint_button", controls)
            with patch(
                "docurandom.workspace_ui.messagebox.askyesno",
                return_value=True,
            ):
                launcher._reset_workspace_checkpoint(workspace)

            handler.assert_called_once_with(workspace)
            self.assertIn("Đã xóa checkpoint", launcher.selection_hint_var.get())

    def test_workspace_hub_creates_folder_and_moves_multi_selection(self) -> None:
        with tempfile.TemporaryDirectory(prefix="DocuRandomWorkspaceFolders_") as temp:
            root = Path(temp)
            store = WorkspaceStore(root / "registry.json", root / "workspaces")
            first = store.create("Ô tô Nhật")
            second = store.create("Máy công nghiệp")
            original_roots = {
                first.workspace_id: first.root,
                second.workspace_id: second.root,
            }
            launcher = WorkspaceLauncher(self.root, store, lambda _workspace: None)

            launcher.folder_name_var.set("Công nghiệp Nhật Bản")
            launcher._create_workspace_folder()
            folder = store.list_folders()[0]
            launcher.selected_workspace_ids = {first.workspace_id, second.workspace_id}
            launcher._move_selected_to_folder(folder.folder_id)

            self.assertEqual(launcher.selected_workspace_ids, set())
            self.assertIn(folder.folder_id, launcher.folder_drop_targets)
            for workspace_id, original_root in original_roots.items():
                grouped = store.get(workspace_id)
                self.assertEqual(grouped.folder_id, folder.folder_id)
                self.assertEqual(grouped.root, original_root)
                self.assertIn(workspace_id, launcher.workspace_card_controls)
                self.assertIn(
                    "selected_var",
                    launcher.workspace_card_controls[workspace_id],
                )

    def test_workspace_hub_deletes_only_virtual_folder(self) -> None:
        with tempfile.TemporaryDirectory(prefix="DocuRandomWorkspaceUngroup_") as temp:
            root = Path(temp)
            store = WorkspaceStore(root / "registry.json", root / "workspaces")
            workspace = store.create("Tin Nhật")
            marker = workspace.media_dir / "khong-xoa.jpg"
            marker.write_bytes(b"image")
            folder = store.create_folder("Tin tức")
            store.assign_to_folder([workspace.workspace_id], folder.folder_id)
            launcher = WorkspaceLauncher(self.root, store, lambda _workspace: None)

            with patch(
                "docurandom.workspace_ui.messagebox.askyesno",
                return_value=True,
            ):
                launcher._delete_workspace_folder(folder)

            self.assertEqual(store.list_folders(), [])
            self.assertEqual(store.get(workspace.workspace_id).folder_id, "")
            self.assertEqual(marker.read_bytes(), b"image")
            self.assertIn("", launcher.folder_drop_targets)

    def test_workspace_hub_renders_nested_folder_tree_and_filters_selected_branch(self) -> None:
        with tempfile.TemporaryDirectory(prefix="DocuRandomWorkspaceTree_") as temp:
            root = Path(temp)
            store = WorkspaceStore(root / "registry.json", root / "workspaces")
            first = store.create("Tin kinh tế")
            second = store.create("Tin xã hội")
            parent = store.create_folder("Nhật Bản")
            child = store.create_folder("Kinh tế", parent.folder_id)
            store.assign_to_folder([first.workspace_id], child.folder_id)
            store.assign_to_folder([second.workspace_id], parent.folder_id)

            launcher = WorkspaceLauncher(self.root, store, lambda _workspace: None)
            self.root.update_idletasks()

            self.assertEqual(launcher.folder_tree.parent(child.folder_id), parent.folder_id)
            launcher.folder_tree.selection_set(child.folder_id)
            launcher._on_folder_tree_selected()

            self.assertEqual(launcher.folder_scope_id, child.folder_id)
            self.assertEqual(tuple(launcher.workspace_card_controls), (first.workspace_id,))
            self.assertIn("Nhật Bản", launcher.current_folder_detail_var.get())
            self.assertIn("Kinh tế", launcher.current_folder_detail_var.get())

    def test_workspace_hub_has_non_drag_folder_move_control(self) -> None:
        with tempfile.TemporaryDirectory(prefix="DocuRandomWorkspaceMovePicker_") as temp:
            root = Path(temp)
            store = WorkspaceStore(root / "registry.json", root / "workspaces")
            workspace = store.create("Tin Nhật")
            folder = store.create_folder("Tin tức")
            launcher = WorkspaceLauncher(self.root, store, lambda _workspace: None)
            launcher.selected_workspace_ids = {workspace.workspace_id}
            label = next(
                name
                for name, folder_id in launcher.folder_destination_by_label.items()
                if folder_id == folder.folder_id
            )
            launcher.folder_move_var.set(label)

            launcher._move_selected_with_picker()

            self.assertEqual(store.get(workspace.workspace_id).folder_id, folder.folder_id)

    def test_workspace_card_progress_updates_and_protects_active_delete(self) -> None:
        with tempfile.TemporaryDirectory(prefix="DocuRandomWorkspaceProgress_") as temp:
            root = Path(temp)
            store = WorkspaceStore(root / "registry.json", root / "workspaces")
            workspace = store.create("Đang dựng")
            activity = {
                "active": True,
                "progress": 42.4,
                "status": "Video 1 chạy · 2 chờ · 0 xong",
                "state": "running",
            }
            launcher = WorkspaceLauncher(
                self.root,
                store,
                lambda _workspace: None,
                activity_provider=lambda _workspace_id: activity,
            )
            launcher._refresh_workspace_activity(reschedule=False)
            controls = launcher.workspace_card_controls[workspace.workspace_id]

            self.assertEqual(controls["activity_progress_var"].get(), 42.4)
            self.assertEqual(controls["activity_percent_var"].get(), "42%")
            self.assertIn("ĐANG LÀM", controls["activity_status_var"].get())
            self.assertEqual(str(controls["delete_button"].cget("state")), "disabled")
            self.assertIn("1 đang làm nền", launcher.workspace_count_var.get())
            self.assertIn(workspace.workspace_id, launcher.live_output_rows)
            live_controls = launcher.live_output_rows[workspace.workspace_id]
            self.assertEqual(live_controls["percent_var"].get(), "42%")
            self.assertIn("ĐANG LÀM", live_controls["status_var"].get())
            self.assertIn("1 đang chạy", launcher.live_summary_var.get())

            activity.update(
                active=False,
                progress=100.0,
                status="Hoàn tất 3 video",
                state="completed",
            )
            launcher._refresh_workspace_activity(reschedule=False)
            self.assertEqual(controls["activity_percent_var"].get(), "100%")
            self.assertIn("HOÀN TẤT", controls["activity_status_var"].get())
            self.assertEqual(str(controls["delete_button"].cget("state")), "normal")
            self.assertEqual(live_controls["percent_var"].get(), "100%")
            self.assertIn("HOÀN TẤT", live_controls["status_var"].get())

    def test_workspace_queue_panel_separates_running_and_waiting(self) -> None:
        with tempfile.TemporaryDirectory(prefix="DocuRandomWorkspaceQueueUi_") as temp:
            root = Path(temp)
            store = WorkspaceStore(root / "registry.json", root / "workspaces")
            running = store.create("Đang viết ChatGPT")
            queued = store.create("Chờ audit VOICEVOX")
            ready = store.create("Sẵn sàng")
            activities = {
                running.workspace_id: {
                    "active": True,
                    "progress": 24.0,
                    "status": "AI 24% · ChatGPT đang viết",
                    "state": "running",
                    "queue_state": "running",
                    "stage": "ChatGPT",
                },
                queued.workspace_id: {
                    "active": True,
                    "progress": 0.0,
                    "status": "Đang chờ lượt ChatGPT audit · trước bạn 1 tác vụ",
                    "state": "running",
                    "queue_state": "queued",
                    "queue_detail": "ChatGPT audit VOICEVOX · trước bạn 1",
                    "stage": "ChatGPT audit VOICEVOX",
                },
                ready.workspace_id: {
                    "active": False,
                    "progress": 0.0,
                    "status": "Sẵn sàng",
                    "state": "idle",
                    "queue_state": "idle",
                    "stage": "—",
                },
            }
            launcher = WorkspaceLauncher(
                self.root,
                store,
                lambda _workspace: None,
                activity_provider=lambda workspace_id: activities.get(workspace_id),
            )
            launcher._refresh_workspace_activity(reschedule=False)

            self.assertIn("Đang làm 1", launcher.workspace_queue_summary_var.get())
            self.assertIn("Hàng chờ 1", launcher.workspace_queue_summary_var.get())
            self.assertIsNotNone(launcher.workspace_queue_tree)
            tree = launcher.workspace_queue_tree
            assert tree is not None
            rows = [tree.item(item, "values") for item in tree.get_children("")]
            self.assertEqual(rows[0][0], running.name)
            self.assertEqual(rows[0][1], "ĐANG LÀM")
            self.assertEqual(rows[1][0], queued.name)
            self.assertEqual(rows[1][1], "HÀNG CHỜ")
            self.assertEqual(rows[2][0], ready.name)
            self.assertEqual(rows[2][1], "SẴN SÀNG")

    def test_workspace_queue_bulk_selection_dispatches_without_opening_cards(self) -> None:
        with tempfile.TemporaryDirectory(prefix="DocuRandomWorkspaceBulkUi_") as temp:
            root = Path(temp)
            store = WorkspaceStore(root / "registry.json", root / "workspaces")
            first = store.create("Một")
            second = store.create("Hai")
            calls: list[tuple[str, tuple[str, ...]]] = []

            def continue_selected(workspaces: tuple[object, ...]) -> tuple[int, str]:
                names = tuple(str(getattr(item, "name", "")) for item in workspaces)
                calls.append(("continue", names))
                return len(workspaces), "đã đưa vào hàng chờ chung"

            launcher = WorkspaceLauncher(
                self.root,
                store,
                lambda _workspace: None,
                bulk_continue_handler=continue_selected,
            )
            launcher._select_all_queue_workspaces()
            self.assertEqual(
                launcher._queue_selected_workspace_ids(),
                {first.workspace_id, second.workspace_id},
            )
            launcher._bulk_continue_selected_workspaces()
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0][0], "continue")
            self.assertIn("đã đưa vào hàng chờ chung", launcher.workspace_queue_selection_var.get())
            launcher._clear_queue_workspace_selection()
            self.assertEqual(launcher._queue_selected_workspace_ids(), set())

    def test_workspace_queue_bulk_run_and_skip_use_one_selected_batch(self) -> None:
        with tempfile.TemporaryDirectory(prefix="DocuRandomWorkspaceBulkRunSkipUi_") as temp:
            root = Path(temp)
            store = WorkspaceStore(root / "registry.json", root / "workspaces")
            first = store.create("Một")
            second = store.create("Hai")
            calls: list[tuple[str, tuple[str, ...]]] = []

            def record(action: str):
                def handler(workspaces: tuple[object, ...]) -> tuple[int, str]:
                    names = tuple(str(getattr(item, "name", "")) for item in workspaces)
                    calls.append((action, names))
                    return len(workspaces), "đã xử lý theo batch"

                return handler

            launcher = WorkspaceLauncher(
                self.root,
                store,
                lambda _workspace: None,
                bulk_run_handler=record("run"),
                bulk_skip_handler=record("skip"),
                bulk_stop_handler=record("stop"),
            )
            launcher._select_all_queue_workspaces()
            launcher._bulk_run_selected_workspaces()
            self.assertEqual(calls[0], ("run", ("Hai", "Một")))
            self.assertIn("Chạy toàn bộ", launcher.workspace_queue_selection_var.get())

            with patch("docurandom.workspace_ui.messagebox.askyesno", return_value=True) as confirm:
                launcher._bulk_skip_selected_workspaces()
            confirm.assert_called_once()
            self.assertEqual(calls[1], ("skip", ("Hai", "Một")))
            self.assertIn("Skip phiên", launcher.workspace_queue_selection_var.get())
            self.assertEqual(
                launcher._queue_selected_workspace_ids(),
                {first.workspace_id, second.workspace_id},
            )

    def test_workspace_update_uses_inline_donut_toast(self) -> None:
        with tempfile.TemporaryDirectory(prefix="DocuRandomUpdateToast_") as temp:
            root = Path(temp)
            store = WorkspaceStore(root / "registry.json", root / "workspaces")
            launcher = WorkspaceLauncher(self.root, store, lambda _workspace: None)
            launcher.update_staged_path = root / "staged"
            launcher.update_staged_version = "9.9.9"
            launcher.update_release_notes = "Sửa lỗi và cải thiện hiệu suất."

            launcher._show_ready_update_toast()
            self.root.update_idletasks()

            self.assertIsNotNone(launcher.update_toast)
            self.assertEqual(launcher.update_toast.winfo_manager(), "place")
            self.assertIn("9.9.9", launcher.update_toast_version_var.get())
            self.assertIn("khởi động lại", launcher.update_toast_title_var.get())
            self.assertEqual(
                launcher.update_toast_primary.cget("text"),
                "Khởi động lại ngay",
            )
            self.assertEqual(launcher.update_toast_secondary.cget("text"), "Để sau")

            launcher._dismiss_update_toast()
            self.root.update_idletasks()
            self.assertEqual(launcher.update_toast.winfo_manager(), "")

    def test_workspace_rename_is_inline_and_preserves_directory(self) -> None:
        with tempfile.TemporaryDirectory(prefix="DocuRandomWorkspaceRename_") as temp:
            root = Path(temp)
            store = WorkspaceStore(root / "registry.json", root / "workspaces")
            workspace = store.create("Tên cũ")
            original_root = workspace.root
            launcher = WorkspaceLauncher(self.root, store, lambda _workspace: None)
            self.root.update()

            launcher._begin_rename(workspace)
            controls = launcher.workspace_card_controls[workspace.workspace_id]
            self.assertTrue(controls["rename_frame"].winfo_manager())
            controls["rename_var"].set("Tên mới")
            launcher._save_workspace_name(workspace)

            renamed = store.get(workspace.workspace_id)
            self.assertEqual(renamed.name, "Tên mới")
            self.assertEqual(renamed.root, original_root)

    def test_workspace_delete_sends_exact_folder_to_recycle_bin(self) -> None:
        with tempfile.TemporaryDirectory(prefix="DocuRandomWorkspaceDelete_") as temp:
            root = Path(temp)
            store = WorkspaceStore(root / "registry.json", root / "workspaces")
            workspace = store.create("Sẽ xóa")
            launcher = WorkspaceLauncher(self.root, store, lambda _workspace: None)
            self.root.update()

            with (
                patch("docurandom.workspace_ui.messagebox.askyesno", return_value=True),
                patch("docurandom.workspace_ui.send2trash") as trash,
            ):
                launcher._delete_workspace(workspace)

            trash.assert_called_once_with(str(workspace.root))
            self.assertIsNone(store.get(workspace.workspace_id))

    def test_workspace_delete_retires_session_before_recycling_and_refresh(self) -> None:
        with tempfile.TemporaryDirectory(prefix="DocuRandomWorkspaceDeleteSession_") as temp:
            base = Path(temp)
            store = WorkspaceStore(base / "registry.json", base / "workspaces")
            workspace = store.create("1")
            events = []
            launcher = WorkspaceLauncher(
                self.root, store, lambda _workspace: None,
                workspace_delete_handler=lambda item: events.append(("retired", item.workspace_id)),
            )
            with (
                patch("docurandom.workspace_ui.messagebox.askyesno", return_value=True),
                patch("docurandom.workspace_ui.send2trash", side_effect=lambda path: events.append(("recycled", path))),
            ):
                launcher._delete_workspace(workspace)
            self.assertEqual(events, [("retired", workspace.workspace_id), ("recycled", str(workspace.root))])
            self.assertNotIn(workspace.workspace_id, launcher.workspace_by_id)
            self.assertEqual(store.recover_unregistered(), 0)

    def test_session_retirement_failure_preserves_workspace_and_folder(self) -> None:
        with tempfile.TemporaryDirectory(prefix="DocuRandomWorkspaceDeleteBusy_") as temp:
            base = Path(temp)
            store = WorkspaceStore(base / "registry.json", base / "workspaces")
            workspace = store.create("Busy")
            launcher = WorkspaceLauncher(
                self.root, store, lambda _workspace: None,
                workspace_delete_handler=Mock(side_effect=ValueError("Worker started")),
            )
            with (
                patch("docurandom.workspace_ui.messagebox.askyesno", return_value=True),
                patch("docurandom.workspace_ui.messagebox.showerror"),
                patch("docurandom.workspace_ui.send2trash") as trash,
            ):
                launcher._delete_workspace(workspace)
            trash.assert_not_called()
            self.assertIsNotNone(store.get(workspace.workspace_id))
            self.assertTrue(workspace.root.exists())


if __name__ == "__main__":
    unittest.main()
