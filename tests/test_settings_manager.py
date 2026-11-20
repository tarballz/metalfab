"""The generalized Setting machinery: template setters, readback, and snapshots."""

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from metalfab import device
from metalfab.capture import Capture
from metalfab.contracts import load_evidence
from metalfab.device import (DeviceError, Profile, Request, Session, Setting,
                             UncertainWrite, select_profile)

REPO = Path(__file__).resolve().parent.parent
RUNNER = CliRunner()
DIGEST = "a" * 64


def _command(code, cmd_type="get", danger="none", readback=False):
    return {
        "code": code, "summary": "Fixture", "board": "machine",
        "cmd_type": cmd_type, "params": [], "danger": danger, "side_effects": [],
        "response": {"kind": "echo+text"}, "evidence": {"offset": "0x10"},
        "confidence": "high", "verified": False, "verified_by": None,
        "readback": readback,
    }


@pytest.fixture
def evidence(tmp_path):
    directory = tmp_path / "data"
    directory.mkdir()
    docs = {
        "commands": {"source": {"board": "machine", "image": "f.bin", "sha256": DIGEST},
                     "commands": [_command("M701"), _command("M702", "set", "motion"),
                                  _command("M703", "set", "motion", readback=True),
                                  _command("M704")]},
        "status": {"trigger": {"command": None}, "report": {"headers": ["H"], "terminator": "E",
                   "fields": [], "raw_status": {"label": "R", "bits": []}}},
        "faults": {"severity_enum": ["fault"], "faults": []},
    }
    for name, doc in docs.items():
        (directory / f"{name}.json").write_text(json.dumps({"version": 1, **doc}))
    return load_evidence(directory)


@pytest.fixture
def profile(evidence):
    return Profile("v1", DIGEST, settings={
        "power": Setting(write=Request("M702 A{value}", "value", echo="M702", danger="motion")),
        "flag": Setting(read=Request("M703", "value"),
                        write=Request("M703 A{value}", "value", echo="M703", danger="motion")),
        "snapshot": Setting(read=Request("M704", "block", block_start="M704")),
    })


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


def _session(tmp_path, evidence, profile, wire):
    capture = Capture(tmp_path / "recording.jsonl", port="/dev/fake", baud=230400, profile=None)
    return Session(wire, capture, evidence, profile)


def test_template_setter_substitutes_and_reads_back(tmp_path, evidence, profile):
    wire = _Wire([(b"M702 A60\n",)])
    session = _session(tmp_path, evidence, profile, wire)
    session.apply_setting("power", "60", expected=None)
    assert wire.writes == [b"M702 A60\n"]


def test_apply_with_readback_getter_confirms_then_writes(tmp_path, evidence, profile):
    wire = _Wire([(b"M703 A0\n",), (b"M703 A1\n",), (b"M703 A1\n",)])
    session = _session(tmp_path, evidence, profile, wire)
    session.apply_setting("flag", "1", expected="A0")
    assert wire.writes == [b"M703\n", b"M703 A1\n", b"M703\n"]


def test_apply_aborts_when_state_changed_since_preview(tmp_path, evidence, profile):
    wire = _Wire([(b"M703 A1\n",)])
    session = _session(tmp_path, evidence, profile, wire)
    with pytest.raises(DeviceError, match="changed since"):
        session.apply_setting("flag", "0", expected="A0")
    assert wire.writes == [b"M703\n"]


def test_apply_readback_mismatch_is_uncertain(tmp_path, evidence, profile):
    wire = _Wire([(b"M703 A0\n",), (b"M703 A1\n",), (b"M703 A0\n",)])
    session = _session(tmp_path, evidence, profile, wire)
    with pytest.raises(UncertainWrite):
        session.apply_setting("flag", "1", expected="A0")


def test_snapshot_mixes_value_and_block_getters(tmp_path, evidence, profile):
    wire = _Wire([(b"M703 A1\n",), (b"M704 A1\nM704 A2\n",)])
    session = _session(tmp_path, evidence, profile, wire)
    snapshot = session.snapshot()
    assert snapshot["flag"] == "A1"
    assert snapshot["snapshot"].startswith("M704 A1")
    assert wire.writes == [b"M703\n", b"M704\n"]


def test_unknown_setting_name_is_rejected(tmp_path, evidence, profile):
    session = _session(tmp_path, evidence, profile, _Wire([]))
    with pytest.raises(DeviceError, match="No reviewed"):
        session.apply_setting("nope", "1", expected=None)


def test_cli_settings_set_previews_and_applies(monkeypatch):
    from metalfab.cli import app

    wire = _Wire([(b"M800 A60\n",)])

    class _Port:
        def __init__(self, w):
            self.w = w

        def __enter__(self):
            return self.w

        def __exit__(self, *exc):
            self.w.close()
            return False

    # Patch the reviewed profiles with a tiny synthetic one over the real data.
    real = load_evidence(REPO / "data")
    synthetic = Profile("40.71.003.2425.01.B04", real.image_hash, settings={
        "power": Setting(write=Request("M800 A{value}", "value", echo="M800", danger="motion")),
    })
    monkeypatch.setattr(device, "REVIEWED_PROFILES", (synthetic,))
    monkeypatch.setattr(device, "_connect", lambda path, baud: _Port(wire))
    result = RUNNER.invoke(app, ["--data-dir", str(REPO / "data"), "settings", "set",
                                 "power", "60", "--port", "/dev/cu.test",
                                 "--device-version", "40.71.003.2425.01.B04", "--apply"])
    assert result.exit_code == 0, result.output
    assert wire.writes == [b"M800 A60\n"]
