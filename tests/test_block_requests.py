"""The block response kind: M350 job index and M351 system log reads."""

import json
from pathlib import Path
import time

import pytest

from metalfab.capture import Capture
from metalfab.contracts import load_evidence
from metalfab.device import DeviceError, Request, Session, select_profile

REPO = Path(__file__).resolve().parent.parent
MACHINE_VERSION = "40.71.003.2425.01.B04"

LOG_BLOCK = (
    b"=== System Log Records (Total: 2) ===\n"
    b"#001 [00:01:02.345] [ERROR] [LASER] Param:0x0000000A - Laser Fault Normal\n"
    b"#002 [00:02:03.004] [SYSTEM] [BOARD] Param:0x00000000 - Board Key Normal\n"
    b"=== End of Log Records ===\n"
)

JOB_BLOCK = (
    b"Index file content: {\"mode\": \"continus\", \"files\": []}\n"
)


class _BlockSerial:
    """Answers writes with one chunk group; reads then go quiet or error."""

    def __init__(self, responses=(), *, fail_read=False):
        self.responses = iter(responses)
        self.queued = []
        self.fail_read = fail_read
        self.writes = []
        self.closed = False

    @property
    def in_waiting(self):
        return len(self.queued[0]) if self.queued and isinstance(self.queued[0], bytes) else 0

    def read(self, size: int) -> bytes:
        del size
        if self.fail_read:
            raise OSError("port gone")
        if not self.queued:
            return b""
        return self.queued.pop(0)

    def write(self, data):
        self.writes.append(data)
        self.queued.append(next(self.responses, b""))
        return len(data)

    def close(self):
        self.closed = True


@pytest.fixture
def evidence():
    loaded = load_evidence(REPO / "data")
    assert not loaded.problems
    return loaded


def _session(tmp_path, evidence, wire):
    profile = select_profile(evidence, MACHINE_VERSION)
    capture = Capture(tmp_path / "recording.jsonl", port="/dev/fake", baud=230400, profile=None)
    return Session(wire, capture, evidence, profile), profile


def _block_request(**extra):
    base: dict = dict(wire="M351", response="block", block_start="=== System Log Records",
                      block_end="=== End of Log Records ===", timeout=0.2)
    base.update(extra)
    return Request(**base)  # type: ignore[arg-type] - test helper builds valid shapes


def test_read_block_collects_until_terminator(tmp_path, evidence):
    session, _ = _session(tmp_path, evidence, _BlockSerial(responses=[LOG_BLOCK]))
    block = session.read_block(_block_request())
    assert block["truncated"] is False
    assert len(block["lines"]) == 4
    assert block["lines"][0].startswith("=== System Log Records")


def test_read_block_does_not_require_an_echo(tmp_path, evidence):
    """M350/M351 handlers print no command echo; the block must not wait for one."""
    session, _ = _session(tmp_path, evidence, _BlockSerial(responses=[JOB_BLOCK]))
    block = session.read_block(_block_request(wire="M350", block_start="Index file content:",
                                              block_end=None), quiescence=0.1)
    assert block["started"] is True
    assert "Index file content:" in block["lines"][0]


def test_read_block_quiescence_marks_truncated(tmp_path, evidence):
    """A block with no terminator ends on stream silence, flagged truncated."""
    partial = b"=== System Log Records (Total: 1) ===\n#001 [00:01:02.345] [ERROR] [LASER] Param:0x0000000A - x\n"
    session, _ = _session(tmp_path, evidence, _BlockSerial(responses=[partial]))
    block = session.read_block(_block_request(), quiescence=0.15)
    assert block["truncated"] is True
    assert block["started"] is True


def test_read_block_timeout_is_reported(tmp_path, evidence):
    session, _ = _session(tmp_path, evidence, _BlockSerial(responses=[]))
    with pytest.raises(DeviceError, match="Timed out"):
        session.read_block(_block_request())


def test_read_block_timeout_reports_received_lines(tmp_path, evidence):
    """A device answer that never matches block_start must reach the operator."""
    reply = b"Failed to open index file: -2\n"
    session, _ = _session(tmp_path, evidence, _BlockSerial(responses=[reply]))
    with pytest.raises(DeviceError, match="Failed to open index file"):
        session.read_block(_block_request(wire="M350", block_start="Index file content:",
                                          block_end=None), quiescence=0.05)


def test_block_gate_accepts_reviewed_getters_only(tmp_path, evidence):
    session, profile = _session(tmp_path, evidence, _BlockSerial(responses=[LOG_BLOCK]))
    assert profile.logs is not None
    block = session.read_block(profile.logs)
    assert block["lines"]
    # A setter cannot ride the block path.
    with pytest.raises(DeviceError):
        session.read_block(Request("M800 A1", "block", block_start="X"))


def test_read_block_surfaces_pre_block_lines_as_events(tmp_path, evidence):
    captured = []
    session, _ = _session(tmp_path, evidence, _BlockSerial(responses=[]))
    session._on_event = lambda event: captured.append(event)
    wire = _BlockSerial(responses=[b"async noise\n" + LOG_BLOCK])
    session._connection = wire
    session.read_block(_block_request())
    texts = [event.line.text for event in captured if event.line is not None]
    assert "async noise" in texts
