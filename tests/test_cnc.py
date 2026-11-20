"""The M859 CNC-mode read and reviewed enable/disable."""

from dataclasses import replace
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from metalfab import cli, device
from metalfab.capture import Capture
from metalfab.contracts import load_evidence
from metalfab.device import DeviceError, Request, Session, UncertainWrite, select_profile

REPO = Path(__file__).resolve().parent.parent
MACHINE_VERSION = "40.71.003.2425.01.B04"
RUNNER = CliRunner()


class _Wire:
    """Answers each write with one response group, then optional idle noise."""

    def __init__(self, responses):
        self.responses = iter(responses)
        self.queued = []
        self.writes = []
        self.closed = False

    @property
    def in_waiting(self):
        return len(self.queued[0]) if self.queued and isinstance(self.queued[0], bytes) else 0

    def read(self, size: int) -> bytes:
        del size
        if not self.queued:
            return b""
        return self.queued.pop(0)

    def write(self, data):
        self.writes.append(data)
        group = next(self.responses, ())
        self.queued.extend(group)
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
    assert profile.cnc is not None, "the reviewed profile must carry the M859 capability"
    capture = Capture(tmp_path / "recording.jsonl", port="/dev/fake", baud=230400, profile=None)
    return Session(wire, capture, evidence, profile)


def test_cnc_read_reports_current_state(tmp_path, evidence):
    session = _session(tmp_path, evidence, _Wire([(b"M859 A0\n",)]))
    assert session.cnc_state() == "A0"
    assert session._connection.writes == [b"M859\n"]


def test_cnc_enable_writes_once_and_reads_back(tmp_path, evidence):
    wire = _Wire([(b"M859 A0\n",), (b"M859 A1\n",), (b"M859 A1\n",)])
    session = _session(tmp_path, evidence, wire)
    session.set_cnc(True, expected="A0")
    assert wire.writes == [b"M859\n", b"M859 A1\n", b"M859\n"]


def test_cnc_changed_since_preview_aborts_before_writing(tmp_path, evidence):
    wire = _Wire([(b"M859 A1\n",)])
    session = _session(tmp_path, evidence, wire)
    with pytest.raises(DeviceError, match="changed since"):
        session.set_cnc(True, expected="A0")
    assert wire.writes == [b"M859\n"]  # no write was sent


def test_cnc_readback_mismatch_is_uncertain(tmp_path, evidence):
    wire = _Wire([(b"M859 A0\n",), (b"M859 A1\n",), (b"M859 A0\n",)])
    session = _session(tmp_path, evidence, wire)
    with pytest.raises(UncertainWrite):
        session.set_cnc(True, expected="A0")


def test_gate_rejects_undeclared_danger(tmp_path, evidence):
    """The registry cross-check: a setter must declare its reviewed danger."""
    session = _session(tmp_path, evidence, _Wire([]))
    with pytest.raises(DeviceError, match="does not establish"):
        session._request(Request("M859 A1", "ack"), command_type="set", response="ack")


def test_cli_cnc_reports_state(monkeypatch):
    from metalfab.cli import app

    wire = _Wire([(b"M859 A0\n",)])

    class _Port:
        def __init__(self, w):
            self.w = w

        def __enter__(self):
            return self.w

        def __exit__(self, *exc):
            self.w.close()
            return False

    monkeypatch.setattr(device, "_connect", lambda path, baud: _Port(wire))
    result = RUNNER.invoke(app, ["--data-dir", str(REPO / "data"), "cnc",
                                 "--port", "/dev/cu.test", "--device-version", MACHINE_VERSION])
    assert result.exit_code == 0, result.output
    assert "A0" in result.output
    assert wire.writes == [b"M859\n"]
