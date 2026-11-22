"""Display reports, event history, and confirmation prompts for the dashboard."""

from rich.console import Group
from rich.table import Table
from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.events import MouseDown, MouseMove, MouseUp
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Select, Static
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from textual.widgets._data_table import RowKey

from .live import History, TimedEvent
from .reports import Line, Report


def terminal_text(value: object) -> str:
    """Remove device-supplied control characters before rendering one line."""
    return "".join(char if char.isprintable() else " " for char in str(value))


def report_view(report: Report, previous: Report | None = None) -> Group:
    """Render status values, raw words, and report issues with safe text labels."""
    fields = Table(box=None, expand=True, padding=(0, 1))
    fields.add_column("Field", ratio=2)
    fields.add_column("Value", ratio=1)
    for key, value in report.values.items():
        changed = previous is not None and previous.values.get(key) != value
        fields.add_row(Text(key.replace("_", " ").capitalize()), Text(terminal_text(value), "bold cyan" if changed else ""))
    for key, value in report.raw_status.items():
        fields.add_row(Text(f"{key.upper()} raw word"), Text(terminal_text(value)))
    for fault in report.faults:
        fields.add_row("Fault", Text(terminal_text(fault["message"]), "yellow"))
    return Group(fields, *(Text(terminal_text(issue), "yellow") for issue in report.issues))


def line_text(line: Line) -> str:
    """Render a decoded line or its framing issue for summary views."""
    return line.issue or terminal_text(line.text)


class EventBrowser(Vertical):
    """A filterable event table with a resizable details pane below it."""

    BINDINGS = [
        Binding("=", "grow_detail", "Grow detail", key_display="+"),
        Binding("-", "shrink_detail", "Shrink detail"),
    ]

    def __init__(self, *, id: str, follow: bool = True) -> None:
        super().__init__(id=id)
        self._history = History()
        self._visible: tuple[TimedEvent, ...] = ()
        self._follow = follow
        self._detail_cells = 5
        self._resizing = False
        self._drag_start_y = 0
        self._drag_start_cells = 5

    def compose(self) -> ComposeResult:
        yield Select(
            [("All events", "all"), ("Faults", "faults"), ("Reports", "reports"),
             ("Framing issues", "framing"), ("Serial lines", "lines")],
            value="all", allow_blank=False, compact=True, classes="event-filter",
        )
        yield DataTable(cursor_type="row", zebra_stripes=True)
        yield Static(classes="detail-resizer")
        with VerticalScroll(classes="event-detail-scroll"):
            yield Static("No events to show.", markup=False, classes="event-detail")

    def _detail(self) -> VerticalScroll:
        return self.query_one(".event-detail-scroll", VerticalScroll)

    def _set_detail_cells(self, cells: int) -> None:
        """Set the detail pane height, capped so the table keeps usable space."""
        maximum = max(3, self.size.height // 2)
        cells = min(maximum, max(2, cells))
        if cells == self._detail_cells:
            return
        self._detail_cells = cells
        self._detail().styles.height = cells

    def action_grow_detail(self) -> None:
        self._set_detail_cells(self._detail_cells + 2)

    def action_shrink_detail(self) -> None:
        self._set_detail_cells(self._detail_cells - 2)

    @on(MouseDown, ".detail-resizer")
    def _start_resize(self, event: MouseDown) -> None:
        event.stop()
        self._resizing = True
        self._drag_start_y = event.screen_y
        self._drag_start_cells = self._detail_cells
        self.capture_mouse()

    @on(MouseMove)
    def _drag_resize(self, event: MouseMove) -> None:
        if self._resizing:
            # Dragging the boundary up grows the pane; down shrinks it.
            self._resize_from(event.screen_y)

    @on(MouseUp)
    def _end_resize(self, event: MouseUp) -> None:
        if self._resizing:
            self._resizing = False
            self.release_mouse()
            # Terminals that never report held-button motion still report the
            # release position, so apply it here too: click-drag works everywhere.
            self._resize_from(event.screen_y)

    def _resize_from(self, screen_y: int) -> None:
        """Size the detail pane from a pointer position relative to the drag start."""
        self._set_detail_cells(self._drag_start_cells + self._drag_start_y - screen_y)

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.add_column("Elapsed", width=9)
        table.add_column("Type", width=9)
        table.add_column("Event", width=56)

    def set_history(self, history: History, *, reset: bool = False) -> None:
        """Update rows while preserving the selected event during live updates."""
        self._history = history
        self._show_events(reset=reset)

    @on(Select.Changed)
    def _filter_changed(self) -> None:
        self._show_events(reset=True)

    def _show_events(self, *, reset: bool) -> None:
        selection = self.query_one(Select).value
        history = self._history
        entries = {
            "faults": history.faults,
            "reports": history.reports,
            "framing": history.framing,
            "lines": tuple(entry for entry in history.events if entry.event.line is not None),
        }.get(selection, history.events)
        if entries == self._visible and not reset:
            return

        table = self.query_one(DataTable)
        row = table.cursor_row
        previous = self._visible[row].sequence if row < len(self._visible) else None
        # Follow new traffic only while the viewport sits at the bottom. Scrolling
        # up (wheel, scrollbar, or keys) unpins it and the view must stay put;
        # scrolling back to the very bottom re-pins it. A reset starts the live
        # browser pinned and the replay browser at the top.
        pinned = self._follow if reset else (
            self._follow and table.is_vertical_scroll_end
            and not table.is_vertical_scrollbar_grabbed
        )
        scroll_y = table.scroll_y
        removed_height = 0
        if reset or not entries or not self._visible:
            table.clear()
            added = entries
        else:
            # The history is a rolling window. Keep rows shared with the previous update.
            for entry in self._visible:
                if entry.sequence >= entries[0].sequence:
                    break
                removed_height += table.get_row_height(cast("RowKey", str(entry.sequence)))
                table.remove_row(str(entry.sequence))
            last = self._visible[-1].sequence
            added = [entry for entry in entries if entry.sequence > last]
        self._visible = entries
        for entry in added:
            kind, description = self._describe(entry, serial_line=selection == "lines")
            table.add_row(f"{entry.elapsed:.2f}s", kind, Text(description[:160]), key=str(entry.sequence))
        if entries:
            if pinned:
                row = len(entries) - 1
            elif reset:
                row = 0
            else:
                row = next(
                    (index for index, item in enumerate(entries) if item.sequence == previous), 0,
                )
            # scroll=False while unpinned: the cursor may sit off-screen, and
            # move_cursor must not yank the viewport back to its row.
            table.move_cursor(row=row, scroll=pinned)
            if not pinned and not reset:
                # Rows removed from the top shift the content up by their height;
                # push the viewport back down by the same amount to keep it anchored.
                # Deferred so it runs after the cursor watcher's scroll-into-view,
                # which move_cursor schedules on every cursor change.
                table.call_after_refresh(
                    table.scroll_to, y=scroll_y + removed_height, animate=False, immediate=True,
                )
        self._show_detail()

    def _describe(self, entry: TimedEvent, *, serial_line: bool) -> tuple[str, str]:
        event = entry.event
        if event.fault is not None and not serial_line:
            return "Fault", terminal_text(event.fault["message"])
        if event.report is not None and not serial_line:
            return "Status", "Complete report" if event.report.complete else "Incomplete report"
        if event.line is not None:
            return ("Framing" if event.line.issue else "Serial"), line_text(event.line)
        return "Event", "Report boundary"

    @on(DataTable.RowHighlighted)
    def _show_detail(self) -> None:
        row = self.query_one(DataTable).cursor_row
        detail = self.query_one(".event-detail", Static)
        if not 0 <= row < len(self._visible):
            detail.update("No events to show.")
            return
        entry = self._visible[row]
        event = entry.event
        serial_line = self.query_one(Select).value == "lines"
        kind, description = self._describe(entry, serial_line=serial_line)
        parts = [f"{entry.elapsed:.2f}s  {kind}  {description}"]
        if event.fault is not None and not serial_line:
            severity = event.fault.get("severity", "Unknown severity")
            explanation = event.fault.get("explanation", "")
            parts.append(terminal_text(f"{severity} · {explanation}"))
            popup = event.fault.get("popup")
            screen_text = event.fault.get("screen_text")
            if popup or screen_text:
                popup_cell = f"Popup {popup['id']} -> page {popup['page']}" if popup else ""
                title = screen_text["title"] if screen_text else ""
                body = screen_text["body"] if screen_text and screen_text["body"] else ""
                shown = "\n".join(part for part in (popup_cell, title, body) if part)
                if shown:
                    parts.append(terminal_text(shown))
            for heading, key in (("Causes", "causes"), ("Actions", "actions"),
                                 ("Firmware", "firmware")):
                items = event.fault.get(key)
                if items:
                    lines = "\n".join(f"  • {item}" for item in items)
                    parts.append(terminal_text(f"{heading}:\n{lines}"))
        if event.line is not None and event.line.issue:
            parts.append(terminal_text(event.line.text))
        text = Text("\n".join(parts))
        report = event.report if not serial_line else None
        detail.update(Group(text, report_view(report)) if report is not None else text)


class ConfirmDisplay(ModalScreen[bool]):
    """Show the read-back value and require explicit approval of a display change."""

    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, preference: str, current: str, proposed: str) -> None:
        super().__init__()
        self._description = f"{preference}\n\nCurrent   {current}\nProposed  {proposed}"

    def compose(self) -> ComposeResult:
        with Vertical(id="display-confirmation"):
            yield Static("Apply display change?", classes="section-title")
            yield Static(self._description, markup=False)
            with Horizontal(classes="form-row"):
                yield Button("Cancel", id="cancel-display")
                yield Button("Apply", variant="warning", id="confirm-display")

    def on_mount(self) -> None:
        self.query_one("#cancel-display", Button).focus()

    @on(Button.Pressed, "#confirm-display")
    def _confirm(self) -> None:
        self.dismiss(True)

    @on(Button.Pressed, "#cancel-display")
    def action_cancel(self) -> None:
        """Dismiss the preview without a settings write."""
        self.dismiss(False)
