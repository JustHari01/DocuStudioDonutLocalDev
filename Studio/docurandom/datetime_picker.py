from __future__ import annotations

from datetime import datetime, timedelta
import tkinter as tk
from tkinter import ttk
from typing import Callable

from .workspace_scheduler import VIETNAM_TIMEZONE, vietnam_now


_BACKGROUND = "#070707"
_PANEL = "#111111"
_PANEL_ALT = "#191919"
_BORDER = "#2d2d2d"
_TEXT = "#ffffff"
_MUTED = "#9a9a9a"
_SELECTED = "#ffffff"
_SELECTED_TEXT = "#080808"
_TODAY = "#263b2b"
_TODAY_TEXT = "#8ee29a"
_HOVER = "#292929"

_MONTH_NAMES = (
    "",
    "Tháng 1",
    "Tháng 2",
    "Tháng 3",
    "Tháng 4",
    "Tháng 5",
    "Tháng 6",
    "Tháng 7",
    "Tháng 8",
    "Tháng 9",
    "Tháng 10",
    "Tháng 11",
    "Tháng 12",
)
_WEEKDAYS = ("T2", "T3", "T4", "T5", "T6", "T7", "CN")


def month_cells(year: int, month: int) -> tuple[tuple[datetime, bool], ...]:
    """Return a stable six-week Monday-first grid for the visual calendar."""

    first = datetime(year, month, 1, tzinfo=VIETNAM_TIMEZONE)
    grid_start = first - timedelta(days=first.weekday())
    return tuple(
        (
            grid_start + timedelta(days=index),
            (grid_start + timedelta(days=index)).month == month,
        )
        for index in range(42)
    )


class DateTimePickerDialog:
    """Small dependency-free Vietnamese date/time picker for the Tk desktop UI."""

    def __init__(
        self,
        parent: tk.Misc,
        *,
        title: str,
        initial: datetime,
        on_apply: Callable[[datetime], None],
        time_only: bool = False,
    ) -> None:
        self.parent = parent
        self.on_apply = on_apply
        self.time_only = bool(time_only)
        self.selected = self._localized(initial).replace(second=0, microsecond=0)
        self.visible_year = self.selected.year
        self.visible_month = self.selected.month

        self.window = tk.Toplevel(parent)
        self.window.title(title)
        self.window.configure(bg=_BACKGROUND)
        self.window.resizable(False, False)
        self.window.transient(parent.winfo_toplevel())
        self.window.protocol("WM_DELETE_WINDOW", self.close)
        self.window.bind("<Escape>", lambda _event: self.close())
        self.window.bind("<Return>", lambda _event: self.apply())

        self.month_label_var = tk.StringVar(master=self.window)
        self.summary_var = tk.StringVar(master=self.window)
        self.hour_var = tk.StringVar(master=self.window, value=f"{self.selected.hour:02d}")
        self.minute_var = tk.StringVar(
            master=self.window,
            value=f"{self.selected.minute:02d}",
        )
        self.day_buttons: list[tk.Button] = []
        self._build(title)
        if not self.time_only:
            self._render_month()
        self._refresh_summary()
        self._position_near_parent()
        self.window.grab_set()
        self.window.focus_force()

    @staticmethod
    def _localized(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=VIETNAM_TIMEZONE)
        return value.astimezone(VIETNAM_TIMEZONE)

    def _build(self, title: str) -> None:
        shell = tk.Frame(
            self.window,
            bg=_PANEL,
            highlightthickness=1,
            highlightbackground=_BORDER,
            padx=18,
            pady=16,
        )
        shell.pack(fill="both", expand=True, padx=1, pady=1)
        tk.Label(
            shell,
            text=title.upper(),
            bg=_PANEL,
            fg=_TEXT,
            font=("Segoe UI Semibold", 11),
            anchor="w",
        ).pack(fill="x")
        tk.Label(
            shell,
            text="Giờ Việt Nam · GMT+7",
            bg=_PANEL,
            fg=_MUTED,
            font=("Segoe UI", 9),
            anchor="w",
        ).pack(fill="x", pady=(2, 12))

        if not self.time_only:
            header = tk.Frame(shell, bg=_PANEL)
            header.pack(fill="x")
            self._plain_button(header, "‹", self.previous_month, width=3).pack(side="left")
            tk.Label(
                header,
                textvariable=self.month_label_var,
                bg=_PANEL,
                fg=_TEXT,
                font=("Segoe UI Semibold", 12),
                anchor="center",
            ).pack(side="left", fill="x", expand=True)
            self._plain_button(header, "›", self.next_month, width=3).pack(side="right")

            weekdays = tk.Frame(shell, bg=_PANEL)
            weekdays.pack(fill="x", pady=(12, 4))
            for index, label in enumerate(_WEEKDAYS):
                weekdays.grid_columnconfigure(index, weight=1, uniform="weekday")
                tk.Label(
                    weekdays,
                    text=label,
                    bg=_PANEL,
                    fg=_MUTED,
                    width=4,
                    font=("Segoe UI Semibold", 9),
                ).grid(row=0, column=index, sticky="ew")

            calendar_frame = tk.Frame(shell, bg=_PANEL)
            calendar_frame.pack(fill="x")
            for column in range(7):
                calendar_frame.grid_columnconfigure(column, weight=1, uniform="day")
            for index in range(42):
                button = tk.Button(
                    calendar_frame,
                    text="",
                    width=4,
                    height=1,
                    relief="flat",
                    borderwidth=0,
                    highlightthickness=0,
                    font=("Segoe UI Semibold", 9),
                    cursor="hand2",
                    takefocus=False,
                )
                button.grid(
                    row=index // 7,
                    column=index % 7,
                    sticky="nsew",
                    padx=2,
                    pady=2,
                    ipady=4,
                )
                self.day_buttons.append(button)

        separator = tk.Frame(shell, bg=_BORDER, height=1)
        separator.pack(fill="x", pady=12)

        time_row = tk.Frame(shell, bg=_PANEL)
        time_row.pack(fill="x")
        tk.Label(
            time_row,
            text="THỜI GIAN",
            bg=_PANEL,
            fg=_MUTED,
            font=("Segoe UI Semibold", 9),
        ).pack(side="left")
        time_controls = tk.Frame(time_row, bg=_PANEL)
        time_controls.pack(side="right")
        self.hour_combo = ttk.Combobox(
            time_controls,
            textvariable=self.hour_var,
            values=tuple(f"{value:02d}" for value in range(24)),
            state="readonly",
            width=4,
            justify="center",
        )
        self.hour_combo.pack(side="left")
        tk.Label(
            time_controls,
            text=":",
            bg=_PANEL,
            fg=_TEXT,
            font=("Segoe UI Semibold", 14),
        ).pack(side="left", padx=5)
        self.minute_combo = ttk.Combobox(
            time_controls,
            textvariable=self.minute_var,
            values=tuple(f"{value:02d}" for value in range(60)),
            state="readonly",
            width=4,
            justify="center",
        )
        self.minute_combo.pack(side="left")
        self.hour_combo.bind("<<ComboboxSelected>>", self._time_changed)
        self.minute_combo.bind("<<ComboboxSelected>>", self._time_changed)

        if not self.time_only:
            quick = tk.Frame(shell, bg=_PANEL)
            quick.pack(fill="x", pady=(12, 0))
            self._plain_button(quick, "Hôm nay", self.choose_today).pack(side="left")
            self._plain_button(quick, "Ngày mai", self.choose_tomorrow).pack(
                side="left", padx=(6, 0)
            )
            self._plain_button(quick, "Bây giờ", self.choose_now).pack(
                side="left", padx=(6, 0)
            )

        summary = tk.Frame(shell, bg=_PANEL_ALT, padx=10, pady=9)
        summary.pack(fill="x", pady=(12, 0))
        tk.Label(
            summary,
            textvariable=self.summary_var,
            bg=_PANEL_ALT,
            fg=_TEXT,
            font=("Segoe UI Semibold", 10),
            anchor="w",
        ).pack(fill="x")

        actions = tk.Frame(shell, bg=_PANEL)
        actions.pack(fill="x", pady=(12, 0))
        self._plain_button(actions, "Hủy", self.close).pack(side="right")
        tk.Button(
            actions,
            text="ÁP DỤNG",
            command=self.apply,
            bg=_SELECTED,
            fg=_SELECTED_TEXT,
            activebackground="#dddddd",
            activeforeground=_SELECTED_TEXT,
            relief="flat",
            borderwidth=0,
            padx=18,
            pady=8,
            font=("Segoe UI Semibold", 9),
            cursor="hand2",
        ).pack(side="right", padx=(0, 7))

    @staticmethod
    def _plain_button(
        parent: tk.Misc,
        text: str,
        command: Callable[[], None],
        *,
        width: int = 0,
    ) -> tk.Button:
        return tk.Button(
            parent,
            text=text,
            command=command,
            width=width,
            bg=_PANEL_ALT,
            fg=_TEXT,
            activebackground=_HOVER,
            activeforeground=_TEXT,
            relief="flat",
            borderwidth=0,
            padx=9,
            pady=6,
            font=("Segoe UI Semibold", 9),
            cursor="hand2",
        )

    def _position_near_parent(self) -> None:
        self.window.update_idletasks()
        root = self.parent.winfo_toplevel()
        x = root.winfo_rootx() + max(20, (root.winfo_width() - self.window.winfo_width()) // 2)
        y = root.winfo_rooty() + max(20, (root.winfo_height() - self.window.winfo_height()) // 2)
        self.window.geometry(f"+{x}+{y}")

    def _time_changed(self, _event: object | None = None) -> None:
        try:
            hour = int(self.hour_var.get())
            minute = int(self.minute_var.get())
        except (TypeError, ValueError, tk.TclError):
            return
        self.selected = self.selected.replace(hour=hour, minute=minute)
        self._refresh_summary()

    def _render_month(self) -> None:
        self.month_label_var.set(
            f"{_MONTH_NAMES[self.visible_month]} · {self.visible_year}"
        )
        today = vietnam_now().date()
        for button, (date_value, in_month) in zip(
            self.day_buttons,
            month_cells(self.visible_year, self.visible_month),
            strict=True,
        ):
            selected = date_value.date() == self.selected.date()
            is_today = date_value.date() == today
            if selected:
                bg, fg, active = _SELECTED, _SELECTED_TEXT, "#dddddd"
            elif is_today:
                bg, fg, active = _TODAY, _TODAY_TEXT, _HOVER
            elif in_month:
                bg, fg, active = _PANEL, _TEXT, _HOVER
            else:
                bg, fg, active = _PANEL, "#555555", _HOVER
            button.configure(
                text=str(date_value.day),
                bg=bg,
                fg=fg,
                activebackground=active,
                activeforeground=fg,
                command=lambda value=date_value: self.choose_day(value),
            )

    def _refresh_summary(self) -> None:
        if self.time_only:
            self.summary_var.set(f"Lặp hằng ngày lúc {self.selected:%H:%M}")
            return
        self.summary_var.set(
            self.selected.strftime("%A · %d/%m/%Y · %H:%M").replace(
                self.selected.strftime("%A"),
                ("Thứ hai", "Thứ ba", "Thứ tư", "Thứ năm", "Thứ sáu", "Thứ bảy", "Chủ nhật")[
                    self.selected.weekday()
                ],
            )
        )

    def choose_day(self, value: datetime) -> None:
        self.selected = self.selected.replace(
            year=value.year,
            month=value.month,
            day=value.day,
        )
        self.visible_year = value.year
        self.visible_month = value.month
        self._render_month()
        self._refresh_summary()

    def previous_month(self) -> None:
        if self.visible_month == 1:
            self.visible_year -= 1
            self.visible_month = 12
        else:
            self.visible_month -= 1
        self._render_month()

    def next_month(self) -> None:
        if self.visible_month == 12:
            self.visible_year += 1
            self.visible_month = 1
        else:
            self.visible_month += 1
        self._render_month()

    def choose_today(self) -> None:
        now = vietnam_now()
        self.choose_day(now)

    def choose_tomorrow(self) -> None:
        tomorrow = vietnam_now() + timedelta(days=1)
        self.choose_day(tomorrow)

    def choose_now(self) -> None:
        now = vietnam_now().replace(second=0, microsecond=0)
        self.selected = now
        self.visible_year = now.year
        self.visible_month = now.month
        self.hour_var.set(f"{now.hour:02d}")
        self.minute_var.set(f"{now.minute:02d}")
        self._render_month()
        self._refresh_summary()

    def apply(self) -> None:
        self._time_changed()
        selected = self.selected
        self.close()
        self.on_apply(selected)

    def close(self) -> None:
        try:
            self.window.grab_release()
        except tk.TclError:
            pass
        try:
            self.window.destroy()
        except tk.TclError:
            pass
