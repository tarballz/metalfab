"""The gated raw probe path and the overlay writer."""

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from metalfab import cli, device, overlay
from metalfab.capture import Capture
from metalfab.contracts import load_evidence
from metalfab.device import DeviceError, Session, select_profile

REPO = Path(__file__).resolve().parent.parent
MACHINE_VERSION = "40.71.003.2425.01.B04"
RUNNER = CliRunner()


class _Wire:
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


_session_counter = 0


def _session(tmp_path, evidence, wire):
    global _session_counter
    _session_counter += 1
    profile = select_profile(evidence, MACHINE_VERSION)
    capture = Capture(tmp_path / f"recording-{_session_counter}.jsonl",
                      port="/dev/fake", baud=230400, profile=None)
    return Session(wire, capture, evidence, profile)


def test_probe_getter_allowed_unaided(tmp_path, evidence):
    wire = _Wire([(b"M112 0x123456\n",)])
    session = _session(tmp_path, evidence, wire)
    result = session.raw("M112", armed=False, force_unverified=False)
    assert wire.writes == [b"M112\n"]
    assert result["code"] == "M112"
    assert "0x123456" in result["lines"][0]


def test_non_allowlisted_getter_requires_arming(tmp_path, evidence):
    session = _session(tmp_path, evidence, _Wire([]))
    with pytest.raises(DeviceError, match="not a reviewed probe getter"):
        session.raw("M812", armed=False, force_unverified=False)


def test_armed_requires_force_unverified(tmp_path, evidence):
    session = _session(tmp_path, evidence, _Wire([]))
    with pytest.raises(DeviceError, match="force-unverified"):
        session.raw("M812", armed=True, force_unverified=False)


def test_armed_force_sends_exactly_the_wire(tmp_path, evidence):
    wire = _Wire([(b"M812 A1\n",)])
    session = _session(tmp_path, evidence, wire)
    result = session.raw("M812", armed=True, force_unverified=True)
    assert wire.writes == [b"M812\n"]
    assert "M812 A1" in result["lines"][0]


def test_armed_registry_emission_needs_extra_flag(tmp_path, evidence):
    session = _session(tmp_path, evidence, _Wire([]))
    with pytest.raises(DeviceError, match="allow-emission"):
        session.raw("M816 A1", armed=True, force_unverified=True, allow_emission=False)
    wire = _Wire([(b"M816 A1\n",)])
    session = _session(tmp_path, evidence, wire)
    result = session.raw("M816 A1", armed=True, force_unverified=True, allow_emission=True)
    assert wire.writes == [b"M816 A1\n"]
    assert result["lines"]


def test_raw_rejects_malformed_framing(tmp_path, evidence):
    session = _session(tmp_path, evidence, _Wire([]))
    with pytest.raises(DeviceError, match="framing"):
        session.raw("M112  extra  spaces", armed=False, force_unverified=False)


# Overlay writer


def test_overlay_writer_merges_and_preserves(tmp_path, evidence):
    directory = tmp_path / "data"
    directory.mkdir()
    (directory / "commands.json").write_text(json.dumps({"version": 1, "source": {
        "board": "machine", "image": "f.bin", "sha256": "a" * 64}, "commands": [{
        "code": "M99", "summary": "Fixture", "board": "machine", "cmd_type": "get",
        "params": [], "danger": "none", "side_effects": [], "response": {"kind": "echo+text"},
        "evidence": {"offset": "0x10"}, "confidence": "high", "verified": False,
        "verified_by": None}]}))
    overlay_path = directory / "verified-overlay.json"
    overlay_path.write_text(json.dumps({"version": 1, "commands": {"M99": {"verified": True}},
                                        "custom_extra": {"keep": "me"}}))
    overlay.write_verification(directory, "commands", "M99", "capture:test")
    payload = json.loads(overlay_path.read_text())
    assert payload["commands"]["M99"]["verified_by"] == "capture:test"
    assert payload["custom_extra"] == {"keep": "me"}


def test_overlay_writer_refuses_unknown_key(tmp_path):
    directory = tmp_path / "data"
    directory.mkdir()
    (directory / "commands.json").write_text(json.dumps({"version": 1, "source": {
        "board": "machine", "image": "f.bin", "sha256": "a" * 64}, "commands": [{
        "code": "M99", "summary": "Fixture", "board": "machine", "cmd_type": "get",
        "params": [], "danger": "none", "side_effects": [], "response": {"kind": "echo+text"},
        "evidence": {"offset": "0x10"}, "confidence": "high", "verified": False,
        "verified_by": None}]}))
    with pytest.raises(ValueError, match="unknown"):
        overlay.write_verification(directory, "commands", "M999", "capture:test")


def test_overlay_writer_refuses_invalid_json(tmp_path):
    directory = tmp_path / "data"
    directory.mkdir()
    (directory / "commands.json").write_text(json.dumps({"version": 1, "source": {
        "board": "machine", "image": "f.bin", "sha256": "a" * 64}, "commands": [{
        "code": "M99", "summary": "Fixture", "board": "machine", "cmd_type": "get",
        "params": [], "danger": "none", "side_effects": [], "response": {"kind": "echo+text"},
        "evidence": {"offset": "0x10"}, "confidence": "high", "verified": False,
        "verified_by": None}]}))
    (directory / "verified-overlay.json").write_text("{not json")
    with pytest.raises(ValueError, match="valid"):
        overlay.write_verification(directory, "commands", "M99", "capture:test")


def test_verify_cli_writes_the_overlay(tmp_path, monkeypatch):
    from metalfab.cli import app

    # Copy the real data dir so the overlay write stays inside the tmp tree.
    real = REPO / "data"
    target = tmp_path / "data"
    target.mkdir()
    for name in ("commands.json", "faults.json", "status.json", "verified-overlay.json"):
        (target / name).write_text((real / name).read_text())
    result = RUNNER.invoke(app, ["--data-dir", str(target), "verify",
                                 "--command", "M99", "--capture", "capture:cli-test"])
    assert result.exit_code == 0, result.output
    payload = json.loads((target / "verified-overlay.json").read_text())
    assert payload["commands"]["M99"]["verified_by"] == "capture:cli-test"
