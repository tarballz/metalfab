"""Display reports, event history, and confirmation prompts for the dashboard."""

from rich.console import Group
from rich.table import Table
from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Select, Static

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
    """A filterable event table with details for the selected row."""

    def __init__(self, *, id: str, follow: bool = True) -> None:
        super().__init__(id=id)
        self._history = History()
        self._visible: tuple[TimedEvent, ...] = ()
        self._follow = follow

    def compose(self) -> ComposeResult:
        yield Select(
            [("All events", "all"), ("Faults", "faults"), ("Reports", "reports"),
             ("Framing issues", "framing"), ("Serial lines", "lines")],
            value="all", allow_blank=False, compact=True, classes="event-filter",
        )
        yield DataTable(cursor_type="row", zebra_stripes=True)
        with VerticalScroll(classes="event-detail-scroll"):
            yield Static("No events to show.", markup=False, classes="event-detail")

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
        # Follow new traffic when the operator has left the cursor on the last row.
        follow = self._follow and (reset or row >= len(self._visible) - 1)
        if reset or not entries or not self._visible:
            table.clear()
            added = entries
        else:
            # The history is a rolling window. Keep rows shared with the previous update.
            for entry in self._visible:
                if entry.sequence >= entries[0].sequence:
                    break
                table.remove_row(str(entry.sequence))
            last = self._visible[-1].sequence
            added = [entry for entry in entries if entry.sequence > last]
        self._visible = entries
        for entry in added:
            kind, description = self._describe(entry, serial_line=selection == "lines")
            table.add_row(f"{entry.elapsed:.2f}s", kind, Text(description[:160]), key=str(entry.sequence))
        if entries:
            row = len(entries) - 1 if follow else 0 if reset else next(
                (index for index, item in enumerate(entries) if item.sequence == previous), 0,
            )
            table.move_cursor(row=row)
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
