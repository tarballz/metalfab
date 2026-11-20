"""Serial inventory and sessions for reviewed MetalFab firmware capabilities."""

from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass, field
import fcntl
from pathlib import Path
import re
import subprocess
import sys
import termios
import time
from typing import Callable, Iterator, Literal

import serial
from serial.tools import list_ports

from .capture import Capture
from .contracts import Evidence
from .head_update import HEAD_BOOTLOADER, SOURCE_HEAD_VERSION, HeadTransferError, transfer_stock_head
from .inventory import BOARD_NAMES, HJ004_MACHINE_VERSION, STOCK_HEAD_VERSION, verify_stock_head
from .reports import Decoder, Event, Line, Report

DEFAULT_BAUD = 230400
DISPLAY_FIELDS = ("language", "imperial_units")
_ADAPTERS = {(0x1A86, 0x7523): "CH340", (0x28E9, 0x018A): "GD32 CDC"}
_HJ004_BOARDS = ("machine", "screen", "laser_control", "welding_head")
_VERSION_NUMBER = r"[0-9]{2}\.[0-9]{2}\.[0-9]{3}\.[0-9]{4}\.[0-9]{2}\.B[0-9]+"
_VERSION_PAYLOAD = re.compile(rf"A(?P<app>{_VERSION_NUMBER})(?:[ \t]+B(?P<bootloader>{_VERSION_NUMBER}))?")


class DeviceError(RuntimeError):
    """The serial session or a reviewed operation could not complete."""


class UncertainWrite(DeviceError):
    """A device write requires independent verification before another attempt."""


@dataclass(frozen=True)
class Port:
    """An enumerated serial port with a USB adapter classification."""

    path: str
    description: str
    usb_id: str | None
    adapter: str | None

    @property
    def is_candidate(self) -> bool:
        """Whether the current transport accepts this adapter's call-out port."""
        return self.adapter is not None and self.path.startswith("/dev/cu.")


def ports() -> list[Port]:
    """List serial devices without opening them or inferring a board identity."""
    result = []
    for port in sorted(list_ports.comports()):
        usb_id = f"{port.vid:04x}:{port.pid:04x}" if port.vid is not None and port.pid is not None else None
        result.append(Port(port.device, port.description, usb_id, _ADAPTERS.get((port.vid, port.pid))))
    return result


@dataclass(frozen=True)
class Request:
    """A reviewed literal command and the shape of its response."""

    wire: str
    response: Literal["ack", "value", "report", "versions", "block"]
    timeout: float = 10.0
    echo: str | None = None
    report_start: str | None = None
    block_start: str | None = None
    block_end: str | None = None

    @property
    def tokens(self) -> list[str]:
        """Return the tokens expected at the start of a response line."""
        return (self.echo if self.echo is not None else self.wire).split()


@dataclass(frozen=True)
class DisplayPreference:
    """A display-only getter and exact wire requests for its supported values."""

    read: Request
    writes: dict[str, Request] = field(default_factory=dict)


@dataclass(frozen=True)
class Profile:
    """Capabilities reviewed against one exact HJ004 machine firmware image."""

    version: str
    image_hash: str
    baud: int = DEFAULT_BAUD
    status: Request | None = None
    settings: dict[str, Request] = field(default_factory=dict)
    display: dict[str, DisplayPreference] = field(default_factory=dict)
    versions: Request | None = None
    jobs: Request | None = None
    logs: Request | None = None


@dataclass(frozen=True)
class BoardVersion:
    """Application version and optional bootloader version reported by one board."""

    app: str
    bootloader: str | None


# Add literals here only after reviewing dispatch, side effects, and response framing.
# The JSON registry and observation overlay alone cannot activate a command.
REVIEWED_PROFILES = (
    Profile(
        version=HJ004_MACHINE_VERSION,
        image_hash="5f312f8cdae9f75f33cb791e277320eed6879ab324c767e85bd123c049c6d65e",
        # The handler prints the echo, then the report builder prints STA and STR.
        status=Request("M808", "report", report_start="=== Laser Ruike Status Report ==="),
        versions=Request("M99", "versions"),
        jobs=Request("M350", "block", block_start="Index file content:"),
        logs=Request("M351", "block", block_start="=== System Log Records",
                     block_end="=== End of Log Records ==="),
    ),
)


def select_profile(evidence: Evidence, version: str) -> Profile:
    """Require an exact operator-confirmed version and matching evidence digest."""
    for profile in REVIEWED_PROFILES:
        if profile.version == version:
            if evidence.image_hash == profile.image_hash:
                return profile
            raise DeviceError(f"Available command evidence does not match the image for {version}")
    raise DeviceError(f"No reviewed HJ004 profile for firmware {version!r}")


def version_probe_profile(evidence: Evidence) -> Profile:
    """Find the reviewed version query for the local image evidence before device identification."""
    candidates = [profile for profile in REVIEWED_PROFILES
                  if profile.image_hash == evidence.image_hash and profile.versions is not None]
    if len(candidates) != 1:
        raise DeviceError("Finding the machine firmware requires matching evidence for one reviewed HJ004 version query.")
    return candidates[0]


def _attributes(path: str) -> Port:
    match = next((port for port in ports() if port.path == path), None)
    if match is None or not match.is_candidate:
        raise DeviceError("Choose an enumerated MetalFab candidate from 'metalfab ports'")
    return match


def _connect(path: str, baud: int):
    if sys.platform != "darwin":
        raise DeviceError("Live serial sessions currently require macOS")
    _attributes(path)
    processes = subprocess.run(
        ["/bin/ps", "-axo", "comm="], check=True, capture_output=True, text=True, timeout=3,
    )
    if any("/xTool Studio.app/" in line or line.strip().startswith("xTool Studio")
           for line in processes.stdout.splitlines()):
        raise DeviceError("Quit xTool Studio before opening the serial port")
    connection = serial.Serial(
        port=None, baudrate=baud, timeout=0.1, write_timeout=1.0,
        exclusive=True, dsrdtr=False, rtscts=False, xonxoff=False,
    )
    connection.port = path
    connection.dtr = False
    connection.rts = False
    try:
        connection.open()
        # pySerial's POSIX flock is advisory. TIOCEXCL blocks subsequent tty opens.
        fcntl.ioctl(connection.fileno(), termios.TIOCEXCL)
    except BaseException:
        connection.close()
        raise
    return connection


@contextmanager
def live_session(
    path: str, evidence: Evidence, *, profile: Profile | None = None,
    baud: int = DEFAULT_BAUD, recording: Path | None = None,
    on_event: Callable[[Event], None] | None = None,
) -> Iterator["Session"]:
    """Create a recorded session and optionally observe all decoded events, including during requests."""
    if profile is not None and baud != profile.baud:
        raise DeviceError(f"Firmware {profile.version} requires {profile.baud} baud")
    with _connect(path, baud) as connection:
        with Capture(recording, port=path, baud=baud, profile=profile.version if profile else None) as capture:
            yield Session(connection, capture, evidence, profile, on_event=on_event)


class Session:
    """Read one port, preserving raw traffic and correlating one reviewed request at a time."""

    def __init__(
        self, connection, capture: Capture, evidence: Evidence, profile: Profile | None = None,
        *, on_event: Callable[[Event], None] | None = None,
    ):
        self._connection = connection
        self.capture = capture
        self._evidence = evidence
        self._profile = profile
        self._decoder = Decoder(evidence)
        self._on_event = on_event
        self.line_history: deque[Line] = deque(maxlen=100)
        self.report_history: deque[Report] = deque(maxlen=30)
        self.fault_history: deque[dict] = deque(maxlen=30)

    def receive(self, *, size: int = 4096) -> Iterator[Event]:
        """Capture bytes before decoding and update the dashboard's bounded history."""
        yield from self._remember(self._decoder.feed(self._read_bytes(size)))

    def _read_bytes(self, size: int) -> bytes:
        data = self._connection.read(size)
        self.capture.chunk("rx", data)
        return data

    def _write_bytes(self, data: bytes) -> int:
        written = self._connection.write(data)
        self.capture.chunk("tx", data[:written])
        return written

    def finish(self) -> Iterator[Event]:
        """Publish partial input when the session ends."""
        yield from self._remember(self._decoder.finish())

    def _remember(self, events: Iterator[Event]) -> Iterator[Event]:
        for event in events:
            if event.line is not None:
                self.line_history.append(event.line)
            if event.report is not None:
                self.report_history.append(event.report)
            if event.fault is not None:
                self.fault_history.append(event.fault)
            if self._on_event is not None:
                self._on_event(event)
            yield event

    def _check(self, spec: Request, *, command_type: str, response: str) -> None:
        if self._profile is None or self._profile.image_hash != self._evidence.image_hash:
            raise DeviceError("A reviewed firmware profile and matching evidence are required")
        try:
            spec.wire.encode("ascii")
        except UnicodeEncodeError as error:
            raise DeviceError("A reviewed request must use ASCII tokens") from error
        parts = spec.wire.split()
        if not parts or spec.wire != " ".join(parts) or not spec.wire.isprintable() or not spec.tokens:
            raise DeviceError("Invalid framing in the reviewed request")
        code = parts[0]
        entry = self._evidence.commands.get(code)
        if (not entry or entry["board"] != "machine" or entry["danger"] != "none"
                or entry["confidence"] != "high" or entry["cmd_type"] != command_type
                or spec.response != response):
            raise DeviceError(f"Command evidence does not establish the reviewed operation for {code}")

    def _request(self, spec: Request, *, command_type: str, response: str) -> str | Report | dict[str, BoardVersion]:
        self._check(spec, command_type=command_type, response=response)
        deadline = time.monotonic() + spec.timeout
        try:
            # Consume queued input before writing so an old echo cannot complete this request.
            while pending := self._connection.in_waiting:
                list(self.receive(size=min(pending, 4096)))
                if time.monotonic() >= deadline:
                    raise DeviceError("The serial stream did not settle before the request")
            if self._decoder.reports.in_report or self._decoder.lines.has_fragment:
                raise DeviceError("The serial stream contains an incomplete message")
            wire = spec.wire.encode("ascii") + b"\n"
            if self._write_bytes(wire) != len(wire):
                raise DeviceError("Serial write was incomplete")
            return self._wait(spec, deadline)
        except BaseException:
            # A delayed reply is unsafe to correlate after a timeout or interrupted request.
            list(self.finish())
            self._connection.close()
            raise

    def _wait(self, spec: Request, deadline: float) -> str | Report | dict[str, BoardVersion]:
        if spec.response == "versions":
            return self._wait_versions(spec, deadline)
        echo = spec.tokens
        echoed = False
        collecting = False
        while time.monotonic() < deadline:
            result = None
            for event in self.receive():
                line = event.line
                if line is None or line.issue:
                    continue
                tokens = line.text.split(maxsplit=len(echo))
                match = tokens[:len(echo)] == echo
                if match and spec.response == "value" and len(tokens) == len(echo) + 1:
                    if result is not None or self._decoder.reports.in_report:
                        raise DeviceError("Ambiguous value response")
                    result = tokens[-1].strip()
                elif match and tokens == echo:
                    if result is not None or echoed or self._decoder.reports.in_report:
                        raise DeviceError("Ambiguous echoed response")
                    echoed = True
                    if spec.response == "ack":
                        result = spec.wire
                if spec.response == "report" and echoed and event.started_report:
                    if collecting or line.text.strip() != spec.report_start:
                        raise DeviceError("Ambiguous status report boundary")
                    collecting = True
                if collecting and event.report is not None:
                    if result is not None:
                        raise DeviceError("Ambiguous status response")
                    if not event.report.complete:
                        raise DeviceError(f"Incomplete status response. {', '.join(event.report.issues)}")
                    result = event.report
            if result is not None:
                return result
        raise DeviceError(f"Timed out after {spec.timeout:g} seconds waiting for the reviewed response")

    def _wait_versions(self, spec: Request, deadline: float) -> dict[str, BoardVersion]:
        versions: dict[str, BoardVersion] = {}
        echo = spec.tokens
        while time.monotonic() < deadline:
            for event in self.receive():
                line = event.line
                if line is None or line.issue:
                    continue
                tokens = line.text.split(maxsplit=len(echo))
                if len(tokens) != len(echo) + 1 or tokens[:-1] != echo:
                    continue
                match = _VERSION_PAYLOAD.fullmatch(tokens[-1].strip())
                if match is None:
                    raise DeviceError("Malformed firmware version response")
                app = match["app"]
                bootloader = match["bootloader"]
                code = app.split(".")[3]
                board = BOARD_NAMES.get(code)
                if board not in _HJ004_BOARDS:
                    continue
                if bootloader is not None and bootloader.split(".")[3] != code:
                    raise DeviceError(f"Firmware versions identify different boards for {board}")
                # The machine sends its own version before forwarding the query.
                if board == "machine" and app != self._profile.version:
                    raise DeviceError(f"Machine replied with firmware {app}; expected {self._profile.version}")
                if (board != "machine" and "machine" not in versions) or board in versions:
                    raise DeviceError(f"Ambiguous firmware version response for {board}")
                versions[board] = BoardVersion(app, bootloader)
            # Board identity is part of each version, so interleaved logs and sub-board
            # response order cannot change which board a reported version belongs to.
            if len(versions) == len(_HJ004_BOARDS):
                return versions
        missing_boards = [board for board in _HJ004_BOARDS if board not in versions]
        missing = ", ".join(missing_boards)
        hint = ""
        if "laser_control" in missing_boards:
            hint = (" The laser control board often goes quiet for a few seconds during "
                    "heartbeat episodes (see the Events tab for 'heartbeat packet detection is "
                    "abnormal') - retry once the episode passes.")
        raise DeviceError(f"Timed out waiting for firmware versions from {missing}.{hint}")

    def can_poll_status(self) -> bool:
        """Return whether this session's evidence enables a status request."""
        schema = self._evidence.report
        spec = self._profile.status if self._profile else None
        if schema is None or spec is None or spec.wire != schema.trigger or spec.report_start is None:
            return False
        try:
            self._check(spec, command_type="get", response="report")
        except DeviceError:
            return False
        return True

    def poll_status(self) -> Report:
        """Request one complete report using the reviewed status capability."""
        if not self.can_poll_status():
            raise DeviceError("No reviewed status trigger is available for this session")
        return self._request(self._profile.status, command_type="get", response="report")

    def _read_named(self, requests: dict[str, Request]) -> dict[str, str]:
        for spec in requests.values():
            self._check(spec, command_type="get", response="value")
        return {
            key: self._request(spec, command_type="get", response="value")
            for key, spec in requests.items()
        }

    def snapshot(self) -> dict[str, str]:
        """Read only the settings with reviewed getter requests."""
        getters = dict(self._profile.settings) if self._profile else {}
        if self._profile:
            getters.update({key: preference.read for key, preference in self._profile.display.items()})
        if not getters:
            raise DeviceError("No reviewed settings getters are available")
        return self._read_named(getters)

    def installed_versions(self) -> dict[str, BoardVersion]:
        """Read all four installed application versions and available bootloader versions."""
        if self._profile is None or not self._profile.versions:
            raise DeviceError("No reviewed version getter is available. Check the vendor UI.")
        return self._request(self._profile.versions, command_type="get", response="versions")

    def read_block(self, spec: Request, *, quiescence: float = 1.0) -> dict:
        """Collect a multi-line response that has no command echo.

        The response starts at a line beginning with spec.block_start and ends
        at spec.block_end or after `quiescence` seconds of stream silence. M350
        and M351 handlers print no echo and M350 has no terminator, so neither
        can use the echo-matched response kinds. Pre-block lines stay events on
        the session's observer; the raw capture remains authoritative either way.
        """
        self._check(spec, command_type="get", response="block")
        deadline = time.monotonic() + spec.timeout
        try:
            # Consume queued input before writing so an old line cannot open the block.
            while pending := self._connection.in_waiting:
                list(self.receive(size=min(pending, 4096)))
                if time.monotonic() >= deadline:
                    raise DeviceError("The serial stream did not settle before the request")
            if self._decoder.reports.in_report or self._decoder.lines.has_fragment:
                raise DeviceError("The serial stream contains an incomplete message")
            wire = spec.wire.encode("ascii") + b"\n"
            if self._write_bytes(wire) != len(wire):
                raise DeviceError("Serial write was incomplete")
            lines: list[str] = []
            preamble: list[str] = []
            started = False
            last_data = time.monotonic()
            while time.monotonic() < deadline:
                for event in self.receive():
                    line = event.line
                    if line is None or line.issue:
                        continue
                    if not started:
                        if line.text.strip().startswith(spec.block_start or ""):
                            started = True
                            lines.append(line.text)
                        else:
                            preamble.append(line.text)
                        continue
                    lines.append(line.text)
                    last_data = time.monotonic()
                    if spec.block_end and line.text.strip() == spec.block_end:
                        return {"lines": lines, "truncated": False, "started": True}
                if started and time.monotonic() - last_data >= quiescence:
                    # Quiescence is the completion signal for terminator-less
                    # dumps (M350); a missing terminator marks truncation only
                    # when the block_end contract expected one.
                    return {"lines": lines, "truncated": spec.block_end is not None, "started": True}
            detail = f" Received: {' | '.join(preamble)}." if preamble else ""
            raise DeviceError(f"Timed out after {spec.timeout:g} seconds waiting for the block response.{detail}")
        except BaseException:
            # A delayed reply is unsafe to correlate after a timeout or interrupted request.
            list(self.finish())
            self._connection.close()
            raise

    def update_stock_head(self, image: bytes, *, apply: bool = False) -> dict[str, str]:
        """Preview or install the stock head update using fresh versions on the same port."""
        verify_stock_head(image)
        if self._profile is None or self._profile.version != HJ004_MACHINE_VERSION:
            raise DeviceError("The head update requires the reviewed HJ004 machine firmware")
        before = self.installed_versions()["welding_head"]
        result = {"board": "welding_head", "installed": before.app, "target": STOCK_HEAD_VERSION}
        if before.app == STOCK_HEAD_VERSION:
            return {**result, "action": "current"}
        if before != BoardVersion(SOURCE_HEAD_VERSION, HEAD_BOOTLOADER):
            raise DeviceError(f"Head application {before.app} and bootloader {before.bootloader} "
                              "do not match the reviewed upgrade path")
        if not apply:
            return {**result, "action": "ready"}
        try:
            # Keep the USB connection exclusive from version preflight through read-back.
            transfer_stock_head(self._read_bytes, self._write_bytes, image)
            time.sleep(1)
            verified = self.installed_versions()["welding_head"]
            if verified != BoardVersion(STOCK_HEAD_VERSION, HEAD_BOOTLOADER):
                raise DeviceError(f"The head reported {verified.app} with bootloader {verified.bootloader}")
        except (HeadTransferError, DeviceError, OSError, KeyboardInterrupt) as error:
            reason = str(error) or type(error).__name__
            raise UncertainWrite(
                f"Welding-head update could not be verified. {reason} "
                "Check the installed head version before attempting another write."
            ) from error
        return {**result, "action": "updated"}

    def _display(self, name: str, value: str) -> tuple[DisplayPreference, Request]:
        preference = self._profile.display.get(name) if self._profile and name in DISPLAY_FIELDS else None
        if preference is None or not preference.writes:
            raise DeviceError(f"No reviewed display setter is available for {name}")
        write = preference.writes.get(value)
        if write is None:
            raise DeviceError(f"Supported values for {name} are {', '.join(preference.writes)}")
        self._check(preference.read, command_type="get", response="value")
        self._check(write, command_type="set", response="ack")
        return preference, write

    def preview_display(self, name: str, value: str) -> str:
        """Read the current display value for a proposed, validated change."""
        preference, _ = self._display(name, value)
        return self._request(preference.read, command_type="get", response="value")

    def apply_display(self, name: str, value: str, expected: str) -> None:
        """Re-check the expected value, send once, then verify the new value."""
        preference, write = self._display(name, value)
        current = self._request(preference.read, command_type="get", response="value")
        if current != expected:
            raise DeviceError(f"{name} changed since preview. Run the preview again.")
        if current == value:
            return
        try:
            self._request(write, command_type="set", response="ack")
            actual = self._request(preference.read, command_type="get", response="value")
            if actual != value:
                raise DeviceError(f"Read-back returned {actual!r}")
        except (DeviceError, OSError, KeyboardInterrupt) as error:
            raise UncertainWrite(
                f"The outcome of the {name} write is uncertain. Check the touchscreen before trying again."
            ) from error
