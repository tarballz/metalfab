"""Exercise offline diagnostics and serial behavior without touching the welder."""

from dataclasses import replace
from contextlib import contextmanager
from hashlib import sha256
import json
from pathlib import Path
import stat
from types import SimpleNamespace
from zipfile import ZipFile

import pytest
import serial
from typer.testing import CliRunner

from metalfab import cli, device
from metalfab.capture import Capture, CaptureError, read_capture, replay_capture
from metalfab.contracts import load_evidence
from metalfab.device import DeviceError, DisplayPreference, Profile, Request, Session
from metalfab.inventory import STOCK_HEAD_VERSION
from metalfab.reports import Decoder, MAX_LINE



FIRMWARE = Path(__file__).resolve().parent.parent / "firmware" / "40.71.003"
needs_firmware = pytest.mark.skipif(not FIRMWARE.exists(), reason="vendor firmware not present")


HEADER = "=== Laser Ruike Status Report ==="
STA = "--- Device Status (STA) ---"
STR = "--- Raycus Status (STR) ---"
END = "=== End of Status Report ==="
DIGEST = "a" * 64
MACHINE_VERSION = "40.71.003.2425.01.B04"
VERSION_RESPONSES = (
    b"M99 A40.71.003.2425.01.B04 B40.71.001.2425.00.B01\n",
    b"M99 A40.71.003.2540.01.B03\n",
    b"M99 A40.71.001.2622.01.B01 B40.71.001.2622.00.B01\n",
    b"M99 A40.70.005.2722.01.B02 B40.70.001.2722.00.B01\n",
)
RX = f"{HEADER}\r\n{STA}\nRaw Status Value: 0x00000000\r\nTemperature Status: Hot\n"
RX += f"heartbeat\n{STR}\r\nRaw Status Value: 0x00000001\n{END}\n"


def _command(code, cmd_type="get"):
    return {
        "code": code, "summary": "Simulated request", "board": "machine",
        "cmd_type": cmd_type, "params": [], "danger": "none", "side_effects": [],
        "response": {"kind": "echo+text"}, "evidence": {"offset": "0x10"},
        "confidence": "high", "verified": False, "verified_by": None,
    }


@pytest.fixture
def evidence(tmp_path):
    directory = tmp_path / "data"
    directory.mkdir()
    docs = {
        "commands": {
            "source": {"board": "machine", "image": "fixture.bin", "sha256": DIGEST},
            "commands": [_command("M708"), _command("M701"), _command("M702", "set")],
        },
        "status": {
            "trigger": {"command": "M708"},
            "report": {
                "headers": [HEADER, STA, STR], "terminator": END,
                "fields": [
                    {"order": 1, "key": "temperature", "label": "Temperature Status",
                     "type": "enum", "values": ["Normal"]},
                    {"order": 2, "key": "raw_sta", "label": "Raw Status Value", "type": "hex"},
                    {"order": 3, "key": "raw_str", "label": "Raw Status Value", "type": "hex"},
                ],
                "raw_status": {"label": "Raw Status Value", "bits": []},
            },
        },
        "faults": {
            "severity_enum": ["fault"],
            "faults": [{
                "id": "fixture.fault", "source_board": "machine", "source_string": "Fixture Fault",
                "severity": "fault", "explanation": "Fixture diagnostic", "bit": None,
                "evidence": {"offset": "0x20"}, "verified": False,
            }],
        },
    }
    for name, doc in docs.items():
        (directory / f"{name}.json").write_text(json.dumps({"version": 1, **doc}))
    return load_evidence(directory)


@pytest.fixture
def profile():
    return Profile(
        "fixture-version", DIGEST,
        status=Request("M708", "report", timeout=0.05, report_start=HEADER),
        display={"language": DisplayPreference(
            Request("M701", "value", timeout=0.05), {"en": Request("M702 A1", "ack", timeout=0.05)},
        )},
    )


class FakeSerial:
    """Emit one group of response chunks for each write, plus optional idle data."""

    def __init__(self, responses=(), idle=()):
        self.responses = iter(responses)
        self.queued = list(idle)
        self.writes = []
        self.closed = False

    @property
    def in_waiting(self):
        return len(self.queued[0]) if self.queued and isinstance(self.queued[0], bytes) else 0

    def read(self, size):
        if not self.queued:
            return b""
        chunk = self.queued.pop(0)
        if isinstance(chunk, Exception):
            raise chunk
        assert len(chunk) <= size
        return chunk

    def write(self, data):
        self.writes.append(data)
        self.queued.extend(next(self.responses, ()))
        return len(data)

    def close(self):
        self.closed = True


def _recorder(tmp_path):
    return Capture(tmp_path / "recording.jsonl", port="/dev/fake", baud=230400, profile=None)


def test_passive_capture_and_replay_preserve_interleaved_events(tmp_path, evidence):
    boundary = RX.index("\r") + 1
    wire = FakeSerial(idle=(b"Fixture Fault\r\n", RX[:boundary].encode(), RX[boundary:].encode()))
    with _recorder(tmp_path) as recording:
        session = Session(wire, recording, evidence)
        live = [event for _ in range(3) for event in session.receive()]
    assert wire.writes == []
    assert stat.S_IMODE(recording.path.stat().st_mode) == 0o600
    assert all(chunk.direction == "rx" for chunk in read_capture(recording.path))
    replayed = [event for _, event in replay_capture(evidence, recording.path)]
    assert live == replayed
    assert any(event.fault and event.fault["id"] == "fixture.fault" for event in live)
    assert any(event.line.text == "heartbeat" for event in live)
    report = live[-1].report
    assert report.complete
    assert report.raw_status == {STA: "0x00000000", STR: "0x00000001"}
    assert report.values == {"temperature": "Hot"}
    assert report.issues == ["Unknown value for temperature"]


def test_capture_cli_replay_and_status(tmp_path, evidence):
    runner = CliRunner()
    with _recorder(tmp_path) as recording:
        recording.chunk("tx", b"IGNORED\n")
        recording.chunk("rx", RX.encode())
    prefix = ["--data-dir", str(evidence.directory)]
    replay = runner.invoke(cli.app, [*prefix, "replay", str(recording.path), "--events"])
    status = runner.invoke(cli.app, [*prefix, "status", "--from-capture", str(recording.path), "--json"])
    assert replay.exit_code == status.exit_code == 0, (replay.output, status.output)
    events = [json.loads(line) for line in replay.stdout.splitlines()]
    assert all(event.get("text") != "IGNORED" for event in events)
    assert json.loads(status.stdout) == {key: value for key, value in events[-1].items() if key not in ("kind", "elapsed_ns")}
    recording.path.write_bytes(recording.path.read_bytes().rsplit(b"\n", 2)[0] + b"\n")
    with pytest.raises(CaptureError, match="without a session end"):
        list(read_capture(recording.path))


def test_incomplete_report_and_oversized_line(evidence):
    decoder = Decoder(evidence)
    lines = list(decoder.feed(b"x" * (MAX_LINE + 1) + b"\n" + (HEADER + "\npartial").encode()))
    assert lines[0].line.issue
    trailing, aborted = decoder.finish()
    assert trailing.line.text == "partial"
    assert not aborted.report.complete


def test_missing_and_malformed_artifacts_leave_replay_available(evidence):
    (evidence.directory / "commands.json").write_text("{")
    (evidence.directory / "status.json").unlink()
    (evidence.directory / "verified-overlay.json").write_text(json.dumps({
        "version": 1, "commands": {"M708": {"verified": True, "verified_by": "capture:fixture"}},
    }))
    partial = load_evidence(evidence.directory)
    assert len(partial.problems) == 2
    assert partial.image_hash is None and partial.report is None
    (event,) = Decoder(partial).feed(b"Fixture Fault\n")
    assert event.fault["severity"] == "fault"


def test_overlay_does_not_make_an_unreviewed_request_eligible(tmp_path, evidence, profile):
    path = evidence.directory / "commands.json"
    doc = json.loads(path.read_text())
    doc["commands"][0]["confidence"] = "unknown"
    path.write_text(json.dumps(doc))
    (evidence.directory / "verified-overlay.json").write_text(json.dumps({
        "version": 1, "commands": {"M708": {"verified": True, "verified_by": "capture:fixture", "danger": "none"}},
    }))
    evidence = load_evidence(evidence.directory)
    assert evidence.commands["M708"]["verified"]
    with _recorder(tmp_path) as capture:
        wire = FakeSerial()
        session = Session(wire, capture, evidence, profile)
        assert not session.can_poll_status()
        with pytest.raises(DeviceError, match="No reviewed status"):
            session.poll_status()
        assert wire.writes == []


def test_exact_echo_then_report(tmp_path, evidence, profile):
    wire = FakeSerial(responses=[(b"M7080\nheartbeat\n", b"M708\r\n", RX.encode())])
    with _recorder(tmp_path) as capture:
        session = Session(wire, capture, evidence, profile)
        assert session.poll_status().complete
        assert [line.text for line in session.line_history][:3] == ["M7080", "heartbeat", "M708"]
    assert [chunk.data for chunk in read_capture(capture.path) if chunk.direction == "tx"] == [b"M708\n"]


@pytest.mark.parametrize(
    ("response", "error"),
    [
        ((b"M7080\n", RX.encode()), DeviceError),
        ((b"M708\nM708\n",), DeviceError),
        ((serial.SerialException("disconnected"),), serial.SerialException),
    ],
)
def test_timeout_ambiguity_and_disconnect_close_the_session(tmp_path, evidence, profile, response, error):
    wire = FakeSerial(responses=[response])
    with _recorder(tmp_path) as capture:
        session = Session(wire, capture, evidence, profile)
        with pytest.raises(error):
            session.poll_status()
    assert wire.closed
    assert wire.writes == [b"M708\n"]


@pytest.mark.parametrize(
    ("apply", "answers", "expected_writes", "message", "exit_code"),
    [
        (False, [b"M701 fr\n"], [b"M701\n"], '+ language = "en"', 0),
        (True, [b"M701 fr\n", b"M701 de\n"], [b"M701\n"] * 2, "changed since preview", 1),
        (True, [b"M701 fr\n"] * 2 + [b"M702 A1\n", b"M701 en\n"],
         [b"M701\n"] * 2 + [b"M702 A1\n", b"M701\n"], "verified by read-back", 0),
        (True, [b"M701 fr\n"] * 2 + [b"M702 A1\n", serial.SerialException("disconnected")],
         [b"M701\n"] * 2 + [b"M702 A1\n", b"M701\n"], "outcome of the language write is uncertain", 1),
    ],
)
def test_display_cli_preview_apply_and_readback(
    tmp_path, evidence, profile, monkeypatch, apply, answers, expected_writes, message, exit_code,
):
    monkeypatch.setattr(device, "REVIEWED_PROFILES", (profile,))
    wire = FakeSerial(responses=[(answer,) for answer in answers])

    @contextmanager
    def session_for_fixture(port, evidence, profile, baud):
        with _recorder(tmp_path) as capture:
            yield Session(wire, capture, evidence, profile)

    monkeypatch.setattr(cli, "live_session", session_for_fixture)
    result = CliRunner().invoke(cli.app, [
        "--data-dir", str(evidence.directory), "settings", "set-display", "language", "en",
        "--port", "/dev/fake", "--device-version", "fixture-version", *(["--apply"] if apply else []),
    ])
    assert result.exit_code == exit_code, result.output
    assert message in result.output
    assert wire.writes == expected_writes


def test_profile_selection_requires_exact_digest(evidence, profile, monkeypatch):
    monkeypatch.setattr(device, "REVIEWED_PROFILES", (profile,))
    with pytest.raises(DeviceError, match="does not match"):
        device.select_profile(replace(evidence, image_hash="b" * 64), profile.version)
    with pytest.raises(DeviceError, match="No reviewed"):
        device.select_profile(evidence, "some-other-version")


def test_shared_evidence_enables_reviewed_machine_status():
    root = Path(__file__).resolve().parents[1]
    evidence = load_evidence(root / "data")
    assert not evidence.problems
    profile = device.select_profile(evidence, "40.71.003.2425.01.B04")
    assert profile.status.report_start in evidence.report.headers
    assert Session(None, None, evidence, profile).can_poll_status()


def test_terminated_machine_status_with_missing_fields_is_incomplete():
    evidence = load_evidence(Path(__file__).resolve().parents[1] / "data")
    payload = f"{HEADER}\n{STA}\nTemperature Status: Normal\n{STR}\n{END}\n"
    events = list(Decoder(evidence).feed(payload.encode()))
    report = events[-1].report
    assert not report.complete
    assert report.values == {"temperature_status": "Normal"}
    assert any(issue.startswith("Missing status fields:") for issue in report.issues)
    assert any(issue.startswith("Missing raw status words:") for issue in report.issues)


def test_firmware_versions_cli_reads_one_query_and_labels_interleaved_responses(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    wire = FakeSerial(responses=[(
        VERSION_RESPONSES[0], b"Failed to open index file: -2\nM990 A40.71.003.2540.01.B03\n",
        VERSION_RESPONSES[3][:12], VERSION_RESPONSES[3][12:], VERSION_RESPONSES[1],
        b"M870 A0\n" + VERSION_RESPONSES[2],
    )])
    history = []

    @contextmanager
    def session_for_fixture(port, evidence, profile, baud):
        with _recorder(tmp_path) as capture:
            session = Session(wire, capture, evidence, profile)
            yield session
            history.extend(line.text for line in session.line_history)

    monkeypatch.setattr(cli, "live_session", session_for_fixture)
    result = CliRunner().invoke(cli.app, [
        "--data-dir", str(root / "data"), "firmware", "versions",
        "--port", "/dev/fake", "--device-version", MACHINE_VERSION,
    ])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {
        "machine": {"app": MACHINE_VERSION, "bootloader": "40.71.001.2425.00.B01"},
        "screen": {"app": "40.71.003.2540.01.B03", "bootloader": None},
        "laser_control": {"app": "40.71.001.2622.01.B01", "bootloader": "40.71.001.2622.00.B01"},
        "welding_head": {"app": "40.70.005.2722.01.B02", "bootloader": "40.70.001.2722.00.B01"},
    }
    assert "Failed to open index file: -2" in history and "M870 A0" in history
    assert wire.writes == [b"M99\n"]


@pytest.mark.parametrize(("responses", "error"), [
    (VERSION_RESPONSES[:3], "Timed out waiting for firmware versions from welding_head"),
    ((VERSION_RESPONSES[0], VERSION_RESPONSES[0]), "Ambiguous firmware version response for machine"),
    ((VERSION_RESPONSES[0].replace(b"003.2425", b"002.2425"),), "Machine replied with firmware"),
])
def test_firmware_version_query_closes_an_unconfirmed_response(tmp_path, responses, error):
    evidence = load_evidence(Path(__file__).resolve().parents[1] / "data")
    profile = device.select_profile(evidence, MACHINE_VERSION)
    profile = replace(profile, versions=replace(profile.versions, timeout=0.05))
    wire = FakeSerial(responses=[responses])
    with _recorder(tmp_path) as capture:
        session = Session(wire, capture, evidence, profile)
        with pytest.raises(DeviceError, match=error):
            session.installed_versions()
    assert wire.closed and wire.writes == [b"M99\n"]


def test_busy_port_reports_serial_error(monkeypatch):
    class BusyPort:
        def __init__(self, **options):
            assert options["port"] is None and options["exclusive"]

        def open(self):
            assert self.dtr is False and self.rts is False
            raise serial.SerialException("port is busy")

        def close(self):
            self.closed = True

    monkeypatch.setattr(device.sys, "platform", "darwin")
    monkeypatch.setattr(device, "ports", lambda: [device.Port("/dev/cu.fake", "Fixture", "28e9:018a", "GD32 CDC")])
    monkeypatch.setattr(device.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(stdout=""))
    monkeypatch.setattr(device.serial, "Serial", BusyPort)
    with pytest.raises(serial.SerialException, match="port is busy"):
        device._connect("/dev/cu.fake", 230400)


def test_mac_serial_open_applies_tty_exclusivity(monkeypatch):
    calls = []

    class MockPort:
        def __init__(self, **options):
            assert options["port"] is None and options["exclusive"]

        def open(self):
            assert self.dtr is False and self.rts is False
            calls.append("opened")

        def fileno(self):
            return 10

    monkeypatch.setattr(device.sys, "platform", "darwin")
    monkeypatch.setattr(device, "ports", lambda: [device.Port("/dev/cu.fake", "Fixture", "28e9:018a", "GD32 CDC")])
    monkeypatch.setattr(device.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(stdout=""))
    monkeypatch.setattr(device.serial, "Serial", MockPort)
    monkeypatch.setattr(device.fcntl, "ioctl", lambda *args: calls.append(args))
    device._connect("/dev/cu.fake", 230400)
    assert calls == ["opened", (10, device.termios.TIOCEXCL)]


def test_firmware_inventory_supports_zip_without_extracting(tmp_path):
    bundle = tmp_path / "bundle.zip"
    with ZipFile(bundle, "w") as archive:
        archive.writestr("screen/800W_Screen_V40.71.003.2540.B03.bin", b"image")
    result = CliRunner().invoke(cli.app, ["firmware", "inspect", str(bundle)])
    assert result.exit_code == 0
    (image,) = json.loads(result.output)
    assert image["board"] == "screen"
    assert image["sha256"] == sha256(b"image").hexdigest()
    assert not (tmp_path / "screen").exists()


def test_firmware_plan_identifies_only_the_head_update(tmp_path):
    root = Path(__file__).resolve().parents[1]
    installed = tmp_path / "installed.json"
    installed.write_text(json.dumps({
        "machine": {"app": MACHINE_VERSION},
        "screen": {"app": "40.71.003.2540.01.B03"},
        "laser_control": {"app": "40.71.001.2622.01.B01"},
        "welding_head": {"app": "40.70.005.2722.01.B02"},
    }))
    result = CliRunner().invoke(cli.app, [
        "firmware", "plan", str(root / "firmware/40.71.003"), "--installed", str(installed),
    ])
    assert result.exit_code == 0, result.output
    plan = json.loads(result.stdout)
    assert {step["board"]: step["action"] for step in plan} == {
        "machine": "current", "screen": "current", "laser_control": "current", "welding_head": "update",
    }
    update = next(step for step in plan if step["action"] == "update")
    assert update["target"] == "40.70.006.2722.01.B01"


def test_firmware_plan_rejects_modified_image_before_opening_port(tmp_path, monkeypatch):
    image = tmp_path / "800W_Welding head_APP_V40.70.006.2722.01.B01.bin"
    image.write_bytes(b"modified image")

    def reject_open(*args, **kwargs):
        pytest.fail("The serial port must stay closed when image validation fails")

    monkeypatch.setattr(cli, "live_session", reject_open)
    result = CliRunner().invoke(cli.app, [
        "firmware", "plan", str(image), "--port", "/dev/fake", "--device-version", MACHINE_VERSION,
    ])
    assert result.exit_code == 1
    assert "does not match the reviewed HJ004 bundle" in result.stderr


@pytest.mark.parametrize(("installed_version", "exit_code"), [
    ("40.70.005.2722.01.B02", 1),
    ("40.70.006.2722.01.B01", 0),
])
def test_firmware_plan_verifies_the_selected_board_version(tmp_path, installed_version, exit_code):
    root = Path(__file__).resolve().parents[1]
    image = next((root / "firmware/40.71.003/4.Welding head").glob("*.bin"))
    installed = tmp_path / "installed.json"
    installed.write_text(json.dumps({
        "machine": {"app": MACHINE_VERSION},
        "welding_head": {"app": installed_version},
    }))
    result = CliRunner().invoke(cli.app, [
        "firmware", "plan", str(image), "--installed", str(installed), "--verify-installed",
    ])
    assert result.exit_code == exit_code, result.output
    assert json.loads(result.stdout)[0]["installed"] == installed_version
    if exit_code:
        assert "Installed firmware differs from the selected image on welding_head" in result.stderr


class HeadUpdateSerial(FakeSerial):
    """Simulate a stock upgrade with split replies and interleaved text."""

    def __init__(self, *, bootloader="40.70.001.2722.00.B01", wrong_block=False, install=True):
        super().__init__()
        self.bootloader = bootloader
        self.wrong_block = wrong_block
        self.install = install
        self.rebooted = False

    @staticmethod
    def _frame(body):
        body += b"\n"
        return b"\xf0" + body + bytes((sum(body) % 128, 0xF7))

    def write(self, data):
        super().write(data)
        if data == b"M99\n":
            version = STOCK_HEAD_VERSION if self.rebooted and self.install else "40.70.005.2722.01.B02"
            self.queued.extend((*VERSION_RESPONSES[:3], f"M99 A{version} B{self.bootloader}\n".encode()))
        elif data.startswith(b"M222"):
            self.queued.append(b"M222 S1\n" if data == b"M222\n" else data)
        else:
            board, service, command = data[1:4]
            assert board == 0x27
            if service == 0x10:
                reply = self._frame(bytes((board, board, board)))
            else:
                ack = self._frame(bytes((board, 0x15, 0x0F)))
                body = data[1:6]
                if service == 0x11:
                    self.rebooted = True
                    body = bytes((board, service, 0))
                elif command == 8:
                    body = bytes((board, service, command, 1))
                elif command == 7 and self.wrong_block and body[3:5] == b"\x01\x00":
                    body = body[:3] + b"\x02\x00"
                reply = ack + self._frame(body)
            self.queued.extend((b"diagnostic log\r\n" + reply[:2], reply[2:]))
        return len(data)


def _run_head_cli(tmp_path, monkeypatch, wire, *, apply, modified=False):
    root = Path(__file__).resolve().parents[1]
    image = next((root / "firmware/40.71.003/4.Welding head").glob("*.bin"))
    if modified:
        copy = tmp_path / image.name
        payload = bytearray(image.read_bytes())
        payload[-1] ^= 1
        copy.write_bytes(payload)
        image = copy

    @contextmanager
    def session_for_fixture(port, evidence, profile, baud):
        assert wire is not None, "The port must stay closed for an unverified image"
        with _recorder(tmp_path) as capture:
            yield Session(wire, capture, evidence, profile)

    monkeypatch.setattr(cli, "live_session", session_for_fixture)
    monkeypatch.setattr(device.time, "sleep", lambda seconds: None)
    return CliRunner().invoke(cli.app, [
        "--data-dir", str(root / "data"), "firmware", "update-head", str(image),
        "--port", "/dev/fake", "--device-version", MACHINE_VERSION, *(["--apply"] if apply else []),
    ])


def test_stock_head_update_matches_vendor_trace_and_verifies_version(tmp_path, monkeypatch):
    wire = HeadUpdateSerial()
    result = _run_head_cli(tmp_path, monkeypatch, wire, apply=True)
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["action"] == "updated"
    assert wire.writes[0] == wire.writes[-1] == b"M99\n"
    # This digest comes from running Studio 1.10.21's updater with scripted
    # replies and the stock image, without a serial connection.
    assert len(wire.writes[1:-1]) == 399
    assert sha256(b"".join(wire.writes[1:-1])).hexdigest() == (
        "a4a556c3e7c13cb2906184c21d1e55bda3384920c28fc83f0a29db6f3451c6e7"
    )


def test_stock_head_preview_only_reads_installed_versions(tmp_path, monkeypatch):
    wire = HeadUpdateSerial()
    result = _run_head_cli(tmp_path, monkeypatch, wire, apply=False)
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["action"] == "ready"
    assert wire.writes == [b"M99\n"]


def test_stock_head_update_rejects_modified_image_before_opening_port(tmp_path, monkeypatch):
    result = _run_head_cli(tmp_path, monkeypatch, None, apply=True, modified=True)
    assert result.exit_code == 1
    assert "does not match" in result.stderr


def test_stock_head_update_rejects_unreviewed_bootloader(tmp_path, monkeypatch):
    wire = HeadUpdateSerial(bootloader="40.70.002.2722.00.B01")
    result = _run_head_cli(tmp_path, monkeypatch, wire, apply=True)
    assert result.exit_code == 1
    assert "reviewed upgrade path" in result.stderr
    assert wire.writes == [b"M99\n"]


def test_stock_head_update_exits_service_on_wrong_block_reply(tmp_path, monkeypatch):
    wire = HeadUpdateSerial(wrong_block=True)
    result = _run_head_cli(tmp_path, monkeypatch, wire, apply=True)
    assert result.exit_code == 1
    assert "acknowledged a different application block" in result.stderr
    assert wire.writes[-1] == b"M222 S7\n"
    assert not wire.rebooted


def test_stock_head_update_requires_read_back_to_report_success(tmp_path, monkeypatch):
    wire = HeadUpdateSerial(install=False)
    result = _run_head_cli(tmp_path, monkeypatch, wire, apply=True)
    assert result.exit_code == 1
    assert "The head reported 40.70.005.2722.01.B02" in " ".join(result.stderr.split())
    assert wire.writes[-2:] == [b"M222 S8\n", b"M99\n"]


def test_stock_head_update_skips_already_installed_application(tmp_path, monkeypatch):
    wire = HeadUpdateSerial()
    wire.rebooted = True
    result = _run_head_cli(tmp_path, monkeypatch, wire, apply=True)
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["action"] == "current"
    assert wire.writes == [b"M99\n"]
