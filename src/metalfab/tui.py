"""Interactive MetalFab diagnostics with live and offline workflows."""

from dataclasses import asdict, dataclass
from itertools import islice
import json
import os
from pathlib import Path
from subprocess import SubprocessError
import sys
from tempfile import NamedTemporaryFile
import time
from typing import Callable, Literal
from zipfile import BadZipFile

from rich.table import Table
from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.events import Resize
from textual.message import Message
from textual.widgets import Button, Collapsible, Footer, Header, Input, Select, Static, TabbedContent, TabPane, Tabs

from .capture import capture_directory, replay_capture
from .contracts import Evidence, load_evidence
from .device import DeviceError, Port, Profile, Session, ports, version_probe_profile
from .inventory import firmware_plan, inspect_firmware, stock_images
from .live import History, LiveMonitor, LiveUpdate, SessionAction, SessionResult, TimedCapture, Timeline, automatic_port
from .reports import Report
from .settings import settings_diff, settings_snapshot
from .tui_views import ConfirmDisplay, EventBrowser, line_text, report_view, terminal_text


_LOCAL_ERRORS = (OSError, ValueError, BadZipFile, SubprocessError, RuntimeError)
_BOARDS = {"machine": "Machine", "screen": "Screen", "laser_control": "Laser control", "welding_head": "Welding head"}
_PAGE_SIZE = 300
_TABS = ("overview", "events", "captures", "firmware", "settings")
# Delay before one automatic retry of a failed firmware-version read. The laser
# control board can be inside a silent heartbeat window; five seconds usually
# lands after the episode.
VERSIONS_RETRY_DELAY = 5.0


class _SerialMessage(Message):
    def __init__(self, monitor: LiveMonitor, update: LiveUpdate | SessionResult) -> None:
        super().__init__()
        self.monitor = monitor
        self.update = update


class _LocalMessage(Message):
    def __init__(self, group: str, target: str, finish: Callable, result: object, error: Exception | None) -> None:
        super().__init__()
        self.group = group
        self.target = target
        self.finish = finish
        self.result = result
        self.error = error


@dataclass(frozen=True)
class _Replay:
    path: Path
    first_page: History
    total: int
    latest: Report | None


def _read_recording(evidence: Evidence, path: Path) -> _Replay:
    page = Timeline()
    latest = None
    total = 0
    # Read to the end to validate the file and find the latest report while keeping one page in memory.
    for total, (elapsed, event) in enumerate(replay_capture(evidence, path), start=1):
        if total <= _PAGE_SIZE:
            page.add(elapsed / 1e9, event)
        if event.report is not None:
            latest = event.report
    return _Replay(path, page.snapshot(), total, latest)


def _replay_page(evidence: Evidence, path: Path, page: int) -> History:
    timeline = Timeline()
    start = page * _PAGE_SIZE
    # Each page decodes from the start so split lines and reports retain their preceding context.
    for elapsed, event in islice(replay_capture(evidence, path), start, start + _PAGE_SIZE):
        timeline.add(elapsed / 1e9, event)
    return timeline.snapshot()


def _recent_recordings() -> list[Path]:
    return sorted(capture_directory().glob("*.jsonl"), reverse=True)[:50]


def _save_json(path: Path, value: dict) -> Path:
    # Publish the complete, private file atomically and fail if the destination already exists.
    with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent) as temporary:
        json.dump(value, temporary, indent=2, sort_keys=True)
        temporary.write("\n")
        temporary.flush()
        os.link(temporary.name, path)
    return path


def _versions(session: Session) -> dict:
    return {board: asdict(version) for board, version in session.installed_versions().items()}


def _inventory(path: Path) -> list[dict]:
    entries = list(inspect_firmware(path))
    if not entries:
        raise ValueError("No .bin firmware images found")
    return entries


def _table(columns: tuple[str, ...], rows) -> Table:
    table = Table(box=None, expand=True, padding=(0, 1), row_styles=["", "on #202b37"])
    for column in columns:
        table.add_column(column, overflow="fold")
    for row in rows:
        table.add_row(*(Text(terminal_text(value)) for value in row))
    return table


class Dashboard(App[None], inherit_bindings=False):
    """Present MetalFab's recorded serial diagnostics and offline tools."""

    TITLE = "MetalFab 800W"
    CSS_PATH = "tui.tcss"
    ENABLE_COMMAND_PALETTE = False
    # Zellij intercepts Textual's default Ctrl+Q, so define app bindings explicitly.
    BINDINGS = [
        Binding(f"f{index}", f"open_tab('{tab}')", tab.title(), key_display=f"F{index}")
        for index, tab in enumerate(_TABS, start=1)
    ] + [
        Binding("f10", "quit", "Quit", key_display="F10", priority=True),
        Binding("ctrl+c", "help_quit", show=False, system=True),
    ]

    def __init__(self, evidence: Evidence, *, offline: bool = False) -> None:
        super().__init__()
        self.evidence = evidence
        self.offline = offline
        self._live_available = not offline and sys.platform == "darwin"
        self._auto_discover = self._live_available
        self._ports: dict[str, Port] = {}
        self._port: Port | None = None
        self._monitor: LiveMonitor | None = None
        self._update = LiveUpdate("closed", History())
        self._started = 0.0
        self._quitting = False
        self._stopping = False
        self._transition: Literal["profile", "capture"] | None = None
        self._poll_target: bool | None = None
        self._serial_job: tuple[str, str, Callable] | None = None
        self._retry_profile: Profile | None = None
        self._versions_retried = False
        self._jobs: set[str] = set()
        self._replay: _Replay | None = None
        self._page = 0
        self._recordings: list[Path] = []
        self._session_recordings: list[Path] = []
        self._installed: dict | None = None

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="connection-bar"):
            yield Static(id="connection-status", markup=False)
            yield Static(id="recording-status", markup=False)
        with TabbedContent(initial="overview"):
            with TabPane("Overview", id="overview"):
                yield from self._overview()
            with TabPane("Events", id="events"):
                yield EventBrowser(id="live-events")
            with TabPane("Captures", id="captures"):
                yield from self._captures()
            with TabPane("Firmware", id="firmware"):
                yield from self._firmware()
            with TabPane("Settings", id="settings"):
                yield from self._settings()
        yield Footer()

    def _overview(self) -> ComposeResult:
        with VerticalScroll(classes="pane-scroll"):
            with Horizontal(classes="form-row live-only"):
                yield Select([], prompt="Choose a USB port", id="ports", compact=True)
                yield Button("Connect", id="connect", variant="primary")
                yield Button("Rescan", id="rescan")
            yield Static(id="connection-message", markup=False, classes="notice live-only")
            with Horizontal(classes="form-row live-only"):
                yield Button("Read firmware", id="identify-firmware", variant="primary")
                yield Button("Pause polling", id="polling")
            with Horizontal(id="overview-panels"):
                with Vertical(id="status-panel", classes="panel"):
                    yield Static("Device status", classes="section-title")
                    yield Static(id="report-status", markup=False, classes="notice")
                    yield Static(id="live-report")
                with Vertical(id="activity-panel", classes="panel"):
                    yield Static("Latest fault", classes="section-title")
                    yield Static("No fault event received.", id="latest-fault", markup=False)
                    yield Static("Latest serial line", classes="section-title next-section")
                    yield Static("No serial data received.", id="latest-line", markup=False)
            if self.evidence.problems:
                with Collapsible(title=f"Evidence problems ({len(self.evidence.problems)})", collapsed=False):
                    yield Static("\n".join(self.evidence.problems), markup=False, classes="notice")

    def _captures(self) -> ComposeResult:
        with VerticalScroll(classes="pane-scroll"):
            yield Static("Timed passive capture", classes="section-title live-only")
            with Horizontal(classes="form-row live-only"):
                yield Input("90", type="number", id="duration", compact=True)
                yield Static("sec", id="duration-unit")
                yield Input(placeholder="New output path (optional)", id="capture-output", compact=True)
                yield Button("Start capture", id="start-capture", variant="primary")
                yield Button("Stop capture", id="stop-capture")
            yield Static(id="capture-timer", markup=False, classes="notice live-only")
            yield Static("Replay a recording", classes="section-title next-section")
            with Horizontal(classes="form-row"):
                yield Select([], prompt="Recent recordings", id="recordings", compact=True)
                yield Button("Reload list", id="reload-captures")
            with Horizontal(classes="form-row"):
                yield Input(placeholder="Capture file path", id="replay-input", compact=True)
                yield Button("Open recording", id="open-replay", variant="primary")
            yield Static(id="capture-message", markup=False, classes="notice")
            with Vertical(id="replay-output"):
                with Collapsible(title="Latest recorded status", id="replay-status"):
                    yield Static(id="replay-report")
                with Horizontal(classes="form-row"):
                    yield Button("Previous", id="replay-previous")
                    yield Static(id="replay-position", markup=False)
                    yield Button("Next", id="replay-next")
                yield EventBrowser(id="replay-events", follow=False)

    def _firmware(self) -> ComposeResult:
        stock = Path("firmware/40.71.003")
        with VerticalScroll(classes="pane-scroll"):
            yield Static("Installed versions", classes="section-title live-only")
            with Horizontal(classes="form-row live-only"):
                yield Button("Read versions", id="read-versions", variant="primary")
                yield Input(placeholder="New versions JSON path", id="versions-output", compact=True)
                yield Button("Save JSON", id="save-versions")
            yield Static("Inspect and compare images", classes="section-title next-section")
            with Horizontal(classes="form-row"):
                yield Input(str(stock) if stock.is_dir() else "", placeholder="Firmware file, directory, or ZIP",
                            id="firmware-input", compact=True)
                yield Button("Inspect", id="inspect-firmware")
                yield Button("Compare live", id="compare-live", classes="live-only")
            with Horizontal(classes="form-row"):
                yield Input(placeholder="Installed versions JSON path", id="installed-input", compact=True)
                yield Button("Compare saved", id="compare-saved")
            yield Static(id="firmware-message", markup=False, classes="notice")
            yield Static(id="firmware-result", classes="panel")

    def _settings(self) -> ComposeResult:
        with VerticalScroll(classes="pane-scroll"):
            yield Static("Compare saved settings", classes="section-title")
            with Horizontal(classes="form-row"):
                yield Input(placeholder="Before JSON", id="settings-before", compact=True)
                yield Input(placeholder="After JSON", id="settings-after", compact=True)
                yield Button("Compare", id="compare-settings", variant="primary")
            yield Static("Device settings", classes="section-title next-section live-only")
            yield Static(id="settings-capabilities", markup=False, classes="notice live-only")
            with Horizontal(classes="form-row live-only"):
                yield Input(placeholder="New settings JSON path", id="settings-output", compact=True)
                yield Button("Export snapshot", id="export-settings")
            with Horizontal(classes="form-row live-only"):
                yield Select([], prompt="Display preference", id="display-preference", compact=True)
                yield Select([], prompt="Proposed value", id="display-value", compact=True)
                yield Button("Preview change", id="preview-display")
            yield Static(id="settings-message", markup=False, classes="notice")
            yield Static(id="settings-result", classes="panel", markup=False)

    def on_mount(self) -> None:
        self.screen.set_class(not self._live_available, "offline")
        self._render_connection()
        self._sync_controls()
        self._refresh_recordings()
        if self._live_available:
            self._scan()
        self.set_interval(0.5, self._render_clock)
        self.set_interval(2.0, self._scan_if_waiting)

    def on_resize(self, event: Resize) -> None:
        self.screen.set_class(event.size.width >= 104, "wide")

    def action_open_tab(self, tab: str) -> None:
        """Show and focus a dashboard tab using its function key."""
        if len(self.screen_stack) == 1:
            tabs = self.query_one(TabbedContent)
            # Move focus off the old pane before hiding it so it cannot reactivate itself.
            self.screen.set_focus(tabs.query_one(Tabs), scroll_visible=False)
            tabs.active = tab

    async def action_quit(self) -> None:
        """Finalize the recording before leaving the dashboard."""
        if self._monitor is None:
            self.exit()
        else:
            self._quitting = True
            self._disconnect()

    def close_serial(self) -> None:
        """Release the serial session if the terminal stops outside the Quit action."""
        if self._monitor is not None:
            self._monitor.stop()
            self._monitor.join()

    def _notice(self, widget: str, content: str, *, error: bool = False) -> None:
        notice = self.query_one(f"#{widget}", Static)
        notice.set_class(error, "error")
        notice.update(content)

    def _button(self, id: str) -> Button:
        return self.query_one(f"#{id}", Button)

    def _input(self, id: str) -> str:
        return self.query_one(f"#{id}", Input).value.strip()

    def _path(self, id: str, description: str, target: str) -> Path | None:
        value = self._input(id)
        try:
            if not value:
                raise ValueError(f"Enter {description}.")
            return Path(value).expanduser()
        except (RuntimeError, ValueError) as error:
            self._notice(target, str(error), error=True)
            return None

    def _local(self, group: str, target: str, job: Callable, finish: Callable) -> None:
        if group in self._jobs:
            return
        self._jobs.add(group)
        self._sync_controls()

        def work() -> None:
            try:
                result = job()
            except _LOCAL_ERRORS as error:
                self.post_message(_LocalMessage(group, target, finish, None, error))
            else:
                self.post_message(_LocalMessage(group, target, finish, result, None))

        self.run_worker(work, thread=True, group=group)

    @on(_LocalMessage)
    def _local_result(self, message: _LocalMessage) -> None:
        self._jobs.discard(message.group)
        if message.error is not None:
            self._notice(message.target, str(message.error), error=True)
            if message.group == "ports":
                self._auto_discover = False
        else:
            message.finish(message.result)
        self._sync_controls()

    @on(Button.Pressed, "#rescan")
    def _scan(self) -> None:
        if self._live_available and self._monitor is None:
            self._local("ports", "connection-message", ports, self._apply_ports)

    def _scan_if_waiting(self) -> None:
        if self._auto_discover and not self._ports:
            self._scan()

    def _apply_ports(self, entries: list[Port]) -> None:
        self._ports = {port.path: port for port in entries if port.is_candidate}
        picker = self.query_one("#ports", Select)
        previous = picker.value
        picker.set_options([(Text(terminal_text(f"{port.path} · {port.adapter} · {port.usb_id}")), port.path)
                            for port in self._ports.values()])
        candidate = automatic_port(entries)
        if previous in self._ports:
            picker.value = previous
        elif len(self._ports) == 1:
            picker.value = next(iter(self._ports))
        if self._auto_discover and candidate is not None:
            self._connect(candidate)
        elif self._ports:
            self._auto_discover = False
            self._notice("connection-message", "Choose the welder's USB port and connect.")
        else:
            self._notice("connection-message", "Connect the welder over USB to see available ports.")

    @on(Select.Changed, "#ports")
    def _port_changed(self) -> None:
        self._sync_controls()

    @on(Button.Pressed, "#connect")
    def _connect_or_disconnect(self) -> None:
        if self._monitor is not None:
            self._disconnect()
        else:
            path = self.query_one("#ports", Select).value
            if path in self._ports:
                self._connect(self._ports[path])

    def _connect(self, port: Port) -> None:
        self._auto_discover = False
        self._port = port
        self._update = LiveUpdate("opening", History())
        monitor = LiveMonitor(port, self.evidence, lambda update: self.post_message(_SerialMessage(monitor, update)))
        self._monitor = monitor
        self._started = monitor.started
        self._notice("connection-message", "Opening the port in passive mode…")
        self._render_history()
        self._render_connection()
        self._sync_controls()
        monitor.start()

    def _disconnect(self) -> None:
        if self._monitor is not None:
            self._stopping = True
            self._monitor.stop()
            self._render_connection()
            self._sync_controls()

    @on(_SerialMessage)
    def _serial_updated(self, message: _SerialMessage) -> None:
        if message.monitor is not self._monitor:
            return
        update = message.update
        if isinstance(update, SessionResult):
            self._finish_serial_job(update)
            return
        previous = self._update
        self._update = update
        if update.state == "closed":
            self._monitor = None
            transition = self._transition
            self._stopping = False
            self._transition = None
            self._poll_target = None
            self._notice("connection-message", update.error or "Serial session closed. Reconnect when ready.", error=bool(update.error))
            if (transition == "profile" and update.error
                    and "Timed out waiting for firmware versions" in update.error
                    and self._retry_profile is not None and not self._versions_retried):
                # The laser control board can be inside one of its silent heartbeat
                # windows; a single delayed retry usually lands after the episode.
                self._versions_retried = True
                self._notice("connection-message",
                             f"{update.error}\nReconnecting in {VERSIONS_RETRY_DELAY:g} s…", error=True)
                self.set_timer(VERSIONS_RETRY_DELAY, self._retry_versions)
            if self._serial_job is not None:
                self._notice(self._serial_job[1], update.error or "Serial session closed.", error=True)
                self._serial_job = None
            if previous.capture_until is not None:
                self._notice("capture-message", update.error or f"Capture saved to {update.recording}", error=bool(update.error))
            elif transition == "capture" and update.error:
                self._notice("capture-message", update.error, error=True)
            if self._quitting:
                self.exit()
                return
        elif update.state == "connected":
            if update.polling == self._poll_target:
                self._poll_target = None
            if previous.state == "opening":
                self._transition = None
                self._notice("connection-message", self._connection_hint(update))
                self._set_display_preferences()
                if update.versions is not None:
                    self._show_versions({board: asdict(version) for board, version in update.versions.items()})
                if update.capture_until is not None:
                    self._notice("capture-message", "Timed capture in progress.")
        elif previous.capture_until is not None:
            self._notice("capture-message", f"Capture saved to {previous.recording}")
        if previous.recording is not None and (update.state == "closed" or update.state == "opening"):
            self._session_recordings.append(previous.recording)
            self._session_recordings = self._session_recordings[-50:]
            self._update_recording_picker()
        if update.state == "closed" or update.recording != previous.recording:
            self._refresh_recordings()
        if update.history != previous.history:
            self._render_history()
        self._render_connection()
        self._sync_controls()

    def _connection_hint(self, update: LiveUpdate) -> str:
        if update.capture_until is not None:
            return "Timed capture is listening for serial traffic."
        if update.profile is None or update.versions is None:
            return "Listening passively. Read firmware to enable reviewed queries."
        screen = update.versions["screen"].app
        identified = f"Machine {update.profile.version} · Screen {screen}"
        if not update.can_poll:
            return f"{identified}\nStatus polling requires matching status evidence."
        return identified

    def _render_connection(self) -> None:
        update = self._update
        if self.offline:
            state = "Offline mode"
        elif not self._live_available:
            state = "Offline · live USB requires macOS"
        elif self._stopping:
            state = "Closing recording…"
        elif update.state == "opening":
            state = "Reading firmware…" if self._transition == "profile" else "Opening serial port…"
        elif update.state == "connected":
            state = "Connected · polling" if update.polling else "Connected · passive"
        elif self._port:
            state = "Disconnected"
        else:
            state = "Discovering USB" if self._auto_discover else "Disconnected"
        suffix = f"   {self._port.path} · {self._port.adapter}" if self._port else ""
        self._notice("connection-status", f"{state}{suffix}")
        if update.recording is None:
            recording = "No recording open"
        else:
            label = "Recording" if update.state == "connected" else "Last recording"
            path = update.recording
            if path.is_relative_to(Path.home()):
                path = Path("~") / path.relative_to(Path.home())
            recording = f"{label}   {path}"
        self._notice("recording-status", recording)
        self._render_clock()

    def _render_clock(self) -> None:
        update = self._update
        if update.history.report:
            entry = update.history.report
            age = max(0, time.monotonic() - self._started - entry.elapsed)
            status = "Complete" if entry.event.report.complete else "Incomplete"
            state = " · disconnected" if update.state == "closed" else " · stale" if age >= 5 else ""
            self._notice("report-status", f"{status}{state} · {age:.0f}s ago")
        elif self.evidence.report is None:
            self._notice("report-status", "Status decoding requires valid status evidence.")
        else:
            self._notice("report-status", "Waiting for a status report." if self._monitor else "No status report.")
        if update.capture_until is None:
            countdown = "Every live session is recorded automatically."
        else:
            left = max(0, update.capture_until - time.monotonic())
            countdown = f"Timed capture · {left:.0f}s remaining"
        self._notice("capture-timer", countdown)

    def _render_history(self) -> None:
        history = self._update.history
        self.query_one("#live-events", EventBrowser).set_history(history)
        result = self.query_one("#live-report", Static)
        if history.report:
            previous = history.previous_report.event.report if history.previous_report else None
            result.update(report_view(history.report.event.report, previous))
        else:
            result.update("")
        fault = history.fault.event.fault if history.fault else None
        self._notice("latest-fault", f'{fault["message"]} · {fault["severity"]}' if fault else "No fault event received.")
        line = history.line.event.line if history.line else None
        self._notice("latest-line", line_text(line)[:300] if line else "No serial data received.")

    def _sync_controls(self) -> None:
        update = self._update
        profile = update.profile
        ready = (update.state == "connected" and not self._stopping and not self._transition
                 and self._serial_job is None and self._poll_target is None)
        live = ready and update.capture_until is None
        self.query_one("#ports", Select).disabled = not self._live_available or self._monitor is not None
        self._button("rescan").disabled = not self._live_available or self._monitor is not None or "ports" in self._jobs
        connect = self._button("connect")
        connect.label = "Disconnect" if self._monitor else "Reconnect" if self._port else "Connect"
        chosen = self.query_one("#ports", Select).value in self._ports
        connect.disabled = self._stopping or not (self._monitor or (self._live_available and chosen and "ports" not in self._jobs))
        self._button("identify-firmware").disabled = not live or profile is not None
        self._button("polling").disabled = not live or not update.can_poll
        self._button("polling").label = "Pause polling" if update.polling else "Resume polling"
        self._button("start-capture").disabled = not live
        self._button("stop-capture").disabled = not ready or update.capture_until is None
        self._button("open-replay").disabled = "replay" in self._jobs
        self._button("replay-previous").disabled = "replay" in self._jobs or self._page == 0
        self._button("replay-next").disabled = (
            "replay" in self._jobs or self._replay is None or (self._page + 1) * _PAGE_SIZE >= self._replay.total
        )
        firmware_idle = "firmware" not in self._jobs and not self._job_uses("firmware-message")
        versions_ready = live and profile is not None and profile.versions is not None
        self._button("read-versions").disabled = not (firmware_idle and versions_ready)
        self._button("compare-live").disabled = not (firmware_idle and versions_ready)
        self._button("save-versions").disabled = not firmware_idle or self._installed is None
        self._button("inspect-firmware").disabled = not firmware_idle
        self._button("compare-saved").disabled = not firmware_idle
        settings_idle = "settings" not in self._jobs and not self._job_uses("settings-message")
        getters = bool(profile and (profile.settings or profile.display))
        display = bool(profile and any(item.writes for item in profile.display.values()))
        self._notice("settings-capabilities", "Reviewed settings available." if getters else
                     "No reviewed settings getters or setters for the current HJ004 profile." if profile else
                     "Read the connected machine's firmware to check available settings.")
        self._button("compare-settings").disabled = not settings_idle
        self._button("export-settings").disabled = not (live and getters and settings_idle)
        self._button("preview-display").disabled = not (live and display and settings_idle)
        self.query_one("#display-preference", Select).disabled = not (live and display and settings_idle)
        self.query_one("#display-value", Select).disabled = not (live and display and settings_idle)

    def _job_uses(self, target: str) -> bool:
        return self._serial_job is not None and self._serial_job[1] == target

    @on(Button.Pressed, "#identify-firmware")
    def _identify_firmware(self) -> None:
        if self._monitor is None or self._button("identify-firmware").disabled:
            return
        try:
            profile = version_probe_profile(self.evidence)
        except DeviceError as error:
            self._notice("connection-message", str(error), error=True)
            return
        self._notice("connection-message", "Reading installed board versions…")
        self._versions_retried = False
        self._switch_session(profile)

    @on(Button.Pressed, "#polling")
    def _toggle_polling(self) -> None:
        if self._monitor is not None:
            self._poll_target = not self._update.polling
            self._monitor.submit(self._poll_target)
            self._sync_controls()

    def _switch_session(self, mode: Profile | TimedCapture) -> None:
        self._transition = "capture" if isinstance(mode, TimedCapture) else "profile"
        if isinstance(mode, Profile):
            self._retry_profile = mode
        self._sync_controls()
        self._monitor.submit(mode)

    def _retry_versions(self) -> None:
        """Reconnect once after a versions timeout and try the same profile again."""
        if self._monitor is None and not self._stopping and self._retry_profile is not None \
                and self._port is not None and self._port.path in self._ports:
            self._connect(self._ports[self._port.path])
            self._switch_session(self._retry_profile)

    def _serial(self, name: str, target: str, operation: Callable, finish: Callable) -> None:
        if self._monitor is not None and self._serial_job is None and self._update.state == "connected":
            self._serial_job = (name, target, finish)
            self._notice(target, f"{name}…")
            self._monitor.submit(SessionAction(name, operation))
            self._sync_controls()

    def _finish_serial_job(self, result: SessionResult) -> None:
        if self._serial_job is None:
            return
        name, target, finish = self._serial_job
        if result.name != name:
            return
        self._serial_job = None
        try:
            finish(result.value)
        except _LOCAL_ERRORS as error:
            self._notice(target, str(error), error=True)
        self._sync_controls()

    @on(Button.Pressed, "#start-capture")
    def _start_capture(self) -> None:
        if self._monitor is None:
            return
        output = self._input("capture-output")
        try:
            seconds = float(self._input("duration"))
            if not 1 <= seconds <= 3600:
                raise ValueError
        except ValueError:
            self._notice("capture-message", "Enter a capture duration from 1 to 3600 seconds.", error=True)
            return
        path = self._path("capture-output", "a new capture path", "capture-message") if output else None
        if output and path is None:
            return
        if path is not None:
            try:
                if os.path.lexists(path) or not path.parent.is_dir():
                    raise ValueError("Choose a new file in an existing directory.")
            except (OSError, ValueError) as error:
                self._notice("capture-message", str(error), error=True)
                return
        self._notice("capture-message", "Starting timed capture…")
        self._switch_session(TimedCapture(seconds, path))

    @on(Button.Pressed, "#stop-capture")
    def _stop_capture(self) -> None:
        if self._monitor is not None:
            self._transition = "capture"
            self._monitor.stop_capture()
            self._sync_controls()

    @on(Button.Pressed, "#reload-captures")
    def _refresh_recordings(self) -> None:
        self._local("recordings", "capture-message", _recent_recordings, self._set_recordings)

    def _set_recordings(self, paths: list[Path]) -> None:
        self._recordings = paths
        self._update_recording_picker()

    def _update_recording_picker(self) -> None:
        picker = self.query_one("#recordings", Select)
        selected = picker.value
        active = self._update.recording if self._update.state == "connected" else None
        recent = dict.fromkeys([*reversed(self._session_recordings), *self._recordings])
        paths = {str(path): path.name for path in islice((path for path in recent if path != active), 50)}
        with picker.prevent(Select.Changed):
            picker.set_options([(Text(terminal_text(name)), path) for path, name in paths.items()])
            if selected in paths:
                picker.value = selected

    @on(Select.Changed, "#recordings")
    def _recording_selected(self, event: Select.Changed) -> None:
        if isinstance(event.value, str):
            self.query_one("#replay-input", Input).value = event.value

    @on(Button.Pressed, "#open-replay")
    @on(Input.Submitted, "#replay-input")
    def _open_replay(self) -> None:
        if "replay" in self._jobs:
            return
        path = self._path("replay-input", "a recording path", "capture-message")
        if path is None:
            return
        recording = self._update.recording
        active = recording if self._update.state == "connected" else None

        def read() -> _Replay:
            if active is not None and path.resolve() == active.resolve():
                raise ValueError("This recording is still open. Replay it after the session closes.")
            return _read_recording(self.evidence, path)

        self._notice("capture-message", "Reading recording…")
        self._local("replay", "capture-message", read, self._show_replay)

    def _show_replay(self, recording: _Replay) -> None:
        self._replay = recording
        self.query_one("#replay-output").add_class("loaded")
        self._notice("capture-message", f"{recording.path.name} · {recording.total:,} decoded events")
        status = self.query_one("#replay-status", Collapsible)
        result = self.query_one("#replay-report", Static)
        if recording.latest is None:
            status.title = "Latest recorded status · No report"
            result.update("No status report in this recording.")
        else:
            quality = "Complete" if recording.latest.complete else "Incomplete"
            status.title = f"Latest recorded status · {quality}"
            result.update(report_view(recording.latest))
        self._show_page(0, recording.first_page)

    def _show_page(self, page: int, history: History) -> None:
        self._page = page
        pages = max(1, (self._replay.total + _PAGE_SIZE - 1) // _PAGE_SIZE)
        self._notice("replay-position", f"Page {page + 1} of {pages}")
        self.query_one("#replay-events", EventBrowser).set_history(history, reset=True)

    @on(Button.Pressed, "#replay-previous")
    @on(Button.Pressed, "#replay-next")
    def _turn_page(self, event: Button.Pressed) -> None:
        if self._replay is not None:
            page = self._page + (1 if event.button.id == "replay-next" else -1)
            path = self._replay.path
            self._local("replay", "capture-message", lambda: _replay_page(self.evidence, path, page),
                        lambda history: self._show_page(page, history))

    @on(Button.Pressed, "#read-versions")
    def _read_versions(self) -> None:
        self._serial("Reading installed versions", "firmware-message", _versions, self._show_versions)

    def _show_versions(self, versions: dict) -> None:
        self._installed = versions
        rows = [(_BOARDS.get(board, board), item["app"], item["bootloader"] or "—") for board, item in versions.items()]
        self._show_firmware("Installed versions", ("Board", "Application", "Bootloader"), rows)

    @on(Button.Pressed, "#save-versions")
    def _save_versions(self) -> None:
        if self._installed is not None:
            path = self._path("versions-output", "a new versions JSON path", "firmware-message")
            if path is not None:
                installed = self._installed
                self._save(path, installed, "firmware")

    @on(Button.Pressed, "#inspect-firmware")
    def _inspect_firmware(self) -> None:
        path = self._path("firmware-input", "a firmware file, directory, or ZIP", "firmware-message")
        if path is not None:
            self._notice("firmware-message", "Inspecting images…")
            self._local("firmware", "firmware-message", lambda: _inventory(path), self._show_inventory)

    def _show_inventory(self, entries: list[dict]) -> None:
        rows = [(_BOARDS.get(item["board"], item["board"] or "—"), item["file"], item["version"] or "—",
                 f'{item["size"]:,}', item["sha256"]) for item in entries]
        self._show_firmware("Image inventory", ("Board", "File", "Version", "Bytes", "SHA-256"), rows)

    @on(Button.Pressed, "#compare-saved")
    def _compare_saved(self) -> None:
        path = self._path("firmware-input", "a firmware file, directory, or ZIP", "firmware-message")
        installed = self._path("installed-input", "an installed versions JSON path", "firmware-message")
        if path is not None and installed is not None:
            self._notice("firmware-message", "Verifying stock images…")
            self._local("firmware", "firmware-message",
                        lambda: firmware_plan(stock_images(path), json.loads(installed.read_text(encoding="utf-8"))),
                        self._show_plan)

    @on(Button.Pressed, "#compare-live")
    def _compare_live(self) -> None:
        path = self._path("firmware-input", "a firmware file, directory, or ZIP", "firmware-message")
        if path is None:
            return
        monitor = self._monitor
        recording = self._update.recording
        self._notice("firmware-message", "Verifying stock images…")

        def compare(images: dict) -> None:
            if self._monitor is not monitor or recording != self._update.recording or self._stopping or self._transition:
                self._notice("firmware-message", "The live session changed. Start the comparison again.", error=True)
                return
            if self._serial_job is not None:
                self._notice("firmware-message", "A device request is running. Start the comparison again when it finishes.", error=True)
                return

            def show(versions: dict) -> None:
                self._installed = versions
                self._show_plan(firmware_plan(images, versions))

            self._serial("Reading installed versions", "firmware-message", _versions, show)

        self._local("firmware", "firmware-message", lambda: stock_images(path), compare)

    def _show_plan(self, plan: list[dict]) -> None:
        rows = [(_BOARDS.get(item["board"], item["board"]), item["installed"], item["target"], item["action"].title())
                for item in plan]
        matches = sum(item["action"] == "current" for item in plan)
        title = f"Stock image comparison · {matches} of {len(plan)} selected boards match"
        self._show_firmware(title, ("Board", "Installed", "Stock image", "Result"), rows)

    def _show_firmware(self, title: str, columns: tuple[str, ...], rows: list[tuple]) -> None:
        self._notice("firmware-message", title)
        self.query_one("#firmware-result", Static).update(_table(columns, rows))

    @on(Button.Pressed, "#compare-settings")
    def _compare_settings(self) -> None:
        before = self._path("settings-before", "a before snapshot path", "settings-message")
        after = self._path("settings-after", "an after snapshot path", "settings-message")
        if before is not None and after is not None:
            self._notice("settings-message", "Comparing snapshots…")
            self._local("settings", "settings-message", lambda: list(settings_diff(before, after)), self._show_settings_diff)

    def _show_settings_diff(self, changes: list[str]) -> None:
        self._notice("settings-message", "Snapshot diff" if changes else "No settings differences.")
        styled = Text("\n").join(Text(line, "green" if line.startswith("+") else "red" if line.startswith("-") else "")
                                     for line in changes)
        self.query_one("#settings-result", Static).update(styled)

    @on(Button.Pressed, "#export-settings")
    def _export_settings(self) -> None:
        profile = self._update.profile
        path = self._path("settings-output", "a new settings JSON path", "settings-message")
        if profile is not None and path is not None:
            self._serial("Reading settings", "settings-message", Session.snapshot,
                         lambda values: self._save(path, settings_snapshot(profile.version, values), "settings"))

    def _save(self, path: Path, value: dict, group: str) -> None:
        target = f"{group}-message"
        self._notice(target, "Saving JSON…")
        self._local(group, target, lambda: _save_json(path, value), lambda saved: self._notice(target, f"Saved {saved}"))

    def _set_display_preferences(self) -> None:
        profile = self._update.profile
        choices = [(name.replace("_", " ").capitalize(), name) for name, item in profile.display.items() if item.writes] if profile else []
        self.query_one("#display-preference", Select).set_options(choices)
        self._set_display_values()

    @on(Select.Changed, "#display-preference")
    def _set_display_values(self) -> None:
        profile = self._update.profile
        preference = self.query_one("#display-preference", Select).value
        options = profile.display.get(preference) if profile else None
        self.query_one("#display-value", Select).set_options([(value, value) for value in options.writes] if options else [])

    @on(Button.Pressed, "#preview-display")
    def _preview_display(self) -> None:
        preference = self.query_one("#display-preference", Select).value
        value = self.query_one("#display-value", Select).value
        if not isinstance(preference, str) or not isinstance(value, str):
            self._notice("settings-message", "Choose a display preference and value.", error=True)
            return
        monitor = self._monitor
        recording = self._update.recording

        def preview(current: str) -> None:
            if current == value:
                self._notice("settings-message", f"{preference} is already {value}.")
                return
            self._notice("settings-message", "Display change preview")

            def confirmed(approved: bool) -> None:
                if not approved:
                    self._notice("settings-message", "Display change canceled.")
                elif self._monitor is not monitor or self._update.recording != recording or self._stopping:
                    self._notice("settings-message", "The live session changed. Preview the setting again.", error=True)
                else:
                    self._serial("Applying display change", "settings-message",
                                 lambda session: session.apply_display(preference, value, current),
                                 lambda _: self._notice("settings-message", "Display preference verified by read-back."))

            if not self._stopping:
                self.push_screen(ConfirmDisplay(preference, current, value), confirmed)

        self._serial("Reading current display setting", "settings-message",
                     lambda session: session.preview_display(preference, value), preview)


def run_dashboard(data_dir: Path, *, offline: bool = False) -> None:
    """Run the terminal dashboard and close the recording on every exit path."""
    dashboard = Dashboard(load_evidence(data_dir), offline=offline)
    try:
        dashboard.run()
    finally:
        dashboard.close_serial()
