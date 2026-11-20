"""CLI jobs and logs commands over a patched serial port."""

import json
from pathlib import Path

from typer.testing import CliRunner

from metalfab import cli, device
from metalfab.cli import app

REPO = Path(__file__).resolve().parent.parent
RUNNER = CliRunner()
MACHINE_VERSION = "40.71.003.2425.01.B04"

LOG_BLOCK = (
    b"=== System Log Records (Total: 1) ===\n"
    b"#001 [00:01:02.345] [ERROR] [LASER] Param:0x0000000A - Laser Fault Normal\n"
    b"=== End of Log Records ===\n"
)
JOB_BLOCK = b"Index file content: {'continus': []}\n"


class _Port:
    """Mimics device._connect's context-manager contract over a fake wire."""

    def __init__(self, wire):
        self.wire = wire

    def __enter__(self):
        return self.wire

    def __exit__(self, *exc):
        self.wire.close()
        return False


class _BlockWire:
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
        self.queued.append(next(self.responses, b""))
        return len(data)

    def close(self):
        self.closed = True


def _patch_connect(monkeypatch, wire: _BlockWire) -> None:
    monkeypatch.setattr(device, "_connect", lambda path, baud: _Port(wire))


def _invoke(args: list[str]):
    return RUNNER.invoke(app, ["--data-dir", str(REPO / "data"), *args])


def test_jobs_command_reads_the_index(monkeypatch):
    wire = _BlockWire(responses=[JOB_BLOCK])
    _patch_connect(monkeypatch, wire)
    result = _invoke(["jobs", "--port", "/dev/cu.test", "--device-version", MACHINE_VERSION, "--json"])
    assert result.exit_code == 0, result.output
    assert wire.writes == [b"M350\n"]
    payload = json.loads(result.stdout)
    assert payload["lines"][0].startswith("Index file content:")
    assert payload["truncated"] is False


def test_logs_command_parses_records(monkeypatch):
    wire = _BlockWire(responses=[LOG_BLOCK])
    _patch_connect(monkeypatch, wire)
    result = _invoke(["logs", "--port", "/dev/cu.test", "--device-version", MACHINE_VERSION, "--json"])
    assert result.exit_code == 0, result.output
    assert wire.writes == [b"M351\n"]
    payload = json.loads(result.stdout)
    assert payload["kind"] == "all"
    assert payload["records"][0]["level"] == "ERROR"
    assert payload["records"][0]["class_name"] == "LASER"


def test_logs_command_type_filter_sends_parameter(monkeypatch):
    wire = _BlockWire(responses=[LOG_BLOCK])
    _patch_connect(monkeypatch, wire)
    result = _invoke(["logs", "--port", "/dev/cu.test", "--device-version", MACHINE_VERSION,
                      "--type", "1", "--json"])
    assert result.exit_code == 0, result.output
    assert wire.writes == [b"M351 A1\n"]
