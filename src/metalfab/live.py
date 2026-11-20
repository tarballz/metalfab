"""Own the dashboard's recorded serial session and publish bounded telemetry."""

from collections import deque
from dataclasses import dataclass, replace
from pathlib import Path
from queue import Empty, SimpleQueue
from subprocess import SubprocessError
from threading import Event as StopSignal, Thread
import time
from typing import Callable, Literal

from .contracts import Evidence
from .device import DEFAULT_BAUD, BoardVersion, DeviceError, Port, Profile, Session, live_session
from .reports import Event


POLL_INTERVAL = 2.0
_PUBLISH_INTERVAL = 0.25


def automatic_port(candidates: list[Port]) -> Port | None:
    """Choose a lone GD32 call-out candidate for passive connection."""
    eligible = [port for port in candidates if port.is_candidate]
    if len(eligible) == 1 and eligible[0].adapter == "GD32 CDC":
        return eligible[0]
    return None


@dataclass(frozen=True)
class TimedEvent:
    """One decoded event at a relative time with a stable table row key."""

    sequence: int
    elapsed: float
    event: Event


@dataclass(frozen=True)
class History:
    """A point-in-time view of recent events and the latest diagnostics."""

    events: tuple[TimedEvent, ...] = ()
    report: TimedEvent | None = None
    previous_report: TimedEvent | None = None
    fault: TimedEvent | None = None
    line: TimedEvent | None = None
    faults: tuple[TimedEvent, ...] = ()
    reports: tuple[TimedEvent, ...] = ()
    framing: tuple[TimedEvent, ...] = ()


class Timeline:
    """Collect live or replay events while bounding the dashboard's memory use."""

    def __init__(self) -> None:
        self.sequence = 0
        self._events: deque[TimedEvent] = deque(maxlen=300)
        self._report: TimedEvent | None = None
        self._previous_report: TimedEvent | None = None
        self._fault: TimedEvent | None = None
        self._line: TimedEvent | None = None
        self._faults: deque[TimedEvent] = deque(maxlen=100)
        self._reports: deque[TimedEvent] = deque(maxlen=100)
        self._framing: deque[TimedEvent] = deque(maxlen=100)

    def add(self, elapsed: float, event: Event) -> None:
        """Record decoded data independently of the report or request that produced it."""
        self.sequence += 1
        entry = TimedEvent(self.sequence, elapsed, event)
        self._events.append(entry)
        if event.report is not None:
            self._previous_report, self._report = self._report, entry
            self._reports.append(entry)
        if event.fault is not None:
            self._fault = entry
            self._faults.append(entry)
        if event.line is not None:
            self._line = entry
            if event.line.issue:
                self._framing.append(entry)

    def snapshot(self) -> History:
        """Freeze the current history before handing it to another thread."""
        return History(
            tuple(self._events), self._report, self._previous_report, self._fault, self._line,
            tuple(self._faults), tuple(self._reports), tuple(self._framing),
        )


@dataclass(frozen=True)
class TimedCapture:
    """Record a separate passive segment for a fixed number of seconds."""

    seconds: float
    path: Path | None


@dataclass(frozen=True)
class SessionAction:
    """A named reviewed operation to run in the serial worker."""

    name: str
    execute: Callable[[Session], object]


@dataclass(frozen=True)
class SessionResult:
    """The completed result of one reviewed operation."""

    name: str
    value: object


@dataclass(frozen=True)
class LiveUpdate:
    """Connection state and telemetry handed from the serial worker to the UI."""

    state: Literal["opening", "connected", "closed"]
    history: History
    recording: Path | None = None
    profile: Profile | None = None
    can_poll: bool = False
    polling: bool = False
    capture_until: float | None = None
    error: str = ""
    versions: dict[str, BoardVersion] | None = None


class LiveMonitor:
    """Run all serial operations for one selected port on a single thread."""

    def __init__(self, port: Port, evidence: Evidence, publish: Callable[[LiveUpdate | SessionResult], None]):
        self.port = port
        self._evidence = evidence
        self._publish = publish
        self._commands: SimpleQueue[Profile | TimedCapture | SessionAction | bool] = SimpleQueue()
        self._stop = StopSignal()
        self._stop_capture = StopSignal()
        self._timeline = Timeline()
        self.started = time.monotonic()
        self._thread = Thread(target=self._run, name="metalfab-serial")

    def start(self) -> None:
        """Start passive recording on the selected port."""
        self._thread.start()

    def submit(self, command: Profile | TimedCapture | SessionAction | bool) -> None:
        """Queue firmware identification, a timed recording, a reviewed operation, or a polling toggle."""
        self._commands.put(command)

    def stop_capture(self) -> None:
        """Finish a timed recording at the next serial read boundary."""
        self._stop_capture.set()

    def stop(self) -> None:
        """Request shutdown after the current read or reviewed request finishes."""
        self._stop.set()

    def join(self) -> None:
        """Wait for the recording to be finalized and the exclusive port released."""
        self._thread.join()

    def _observe(self, event: Event) -> None:
        self._timeline.add(time.monotonic() - self.started, event)

    def _run(self) -> None:
        mode: Profile | TimedCapture | None = None
        state = LiveUpdate("opening", History())
        try:
            while not self._stop.is_set():
                profile = mode if isinstance(mode, Profile) else None
                timed = mode if isinstance(mode, TimedCapture) else None
                self._publish(LiveUpdate("opening", self._timeline.snapshot(), state.recording))
                if timed is not None:
                    self._stop_capture.clear()
                with live_session(
                    self.port.path, self._evidence, profile=profile,
                    baud=profile.baud if profile else DEFAULT_BAUD,
                    recording=timed.path if timed else None, on_event=self._observe,
                ) as session:
                    state = LiveUpdate("opening", self._timeline.snapshot(), session.capture.path)
                    try:
                        if self._stop.is_set():
                            break
                        # The local evidence selects the query. Its reply must match the machine profile
                        # before the dashboard can poll status or offer other reviewed requests.
                        versions = session.installed_versions() if profile else None
                        can_poll = session.can_poll_status()
                        state = LiveUpdate(
                            "connected", self._timeline.snapshot(), recording=session.capture.path, profile=profile,
                            can_poll=can_poll, polling=can_poll,
                            capture_until=time.monotonic() + timed.seconds if timed else None,
                            versions=versions,
                        )
                        self._publish(state)
                        mode = self._read(session, state)
                    finally:
                        list(session.finish())
        except (DeviceError, OSError, ValueError, SubprocessError) as error:
            state = replace(state, error=str(error))
        finally:
            self._publish(LiveUpdate("closed", self._timeline.snapshot(), state.recording, error=state.error))

    def _read(self, session: Session, state: LiveUpdate) -> Profile | TimedCapture | None:
        next_poll = time.monotonic()
        next_publish = next_poll
        published = self._timeline.sequence
        while not self._stop.is_set():
            now = time.monotonic()
            if state.capture_until is not None:
                if now >= state.capture_until or self._stop_capture.is_set():
                    return None
                # A timed recording is passive for the entire segment.
                command = None
            else:
                try:
                    command = self._commands.get_nowait()
                except Empty:
                    command = None
            if isinstance(command, (Profile, TimedCapture)):
                return command
            if isinstance(command, bool):
                state = replace(state, polling=state.can_poll and command, history=self._timeline.snapshot())
                next_poll = now
                self._publish(state)
                published = self._timeline.sequence
            elif isinstance(command, SessionAction):
                result = command.execute(session)
                self._publish(SessionResult(command.name, result))
            elif state.polling and now >= next_poll:
                session.poll_status()
                next_poll = time.monotonic() + POLL_INTERVAL
            else:
                list(session.receive())
            now = time.monotonic()
            # A busy serial stream must not create one UI message per line.
            if self._timeline.sequence != published and now >= next_publish:
                self._publish(replace(state, history=self._timeline.snapshot()))
                published = self._timeline.sequence
                next_publish = now + _PUBLISH_INTERVAL
        return None
