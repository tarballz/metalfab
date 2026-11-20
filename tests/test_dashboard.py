"""Drive the dashboard with Textual's pilot and real sessions over fake serial."""

import asyncio
from contextlib import contextmanager
from dataclasses import replace
import json
from pathlib import Path
import time
from types import SimpleNamespace

import pytest
import serial
from textual.widgets import Button, DataTable, Input, Select, Static, TabbedContent

FIRMWARE_DIR = Path(__file__).resolve().parent.parent / "firmware" / "40.71.003"
needs_firmware_dashboard = pytest.mark.skipif(not FIRMWARE_DIR.exists(), reason="vendor firmware not present")

from typer.testing import CliRunner

from metalfab import capture, cli, device, live, tui
from metalfab.capture import Capture, read_capture
from metalfab.contracts import load_evidence
from metalfab.device import Port, Request
from test_metalfab import FakeSerial, MACHINE_VERSION, RX, VERSION_RESPONSES, _command, evidence, profile


GD32 = Port("/dev/cu.test", "Fixture", "28e9:018a", "GD32 CDC")
CH340 = Port("/dev/cu.other", "Fixture", "1a86:7523", "CH340")
STATUS_RESPONSE = f'M708\nFixture Fault\n{RX.replace("Hot", "Normal")}'.encode()


async def _wait_for(condition, *, timeout=3):
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            pytest.fail("Dashboard did not reach the expected state")
        await asyncio.sleep(0.01)


async def _click(pilot, selector):
    # Let Textual reflow after a state update, which can move controls by a terminal row.
    await pilot.pause(0.08)
    assert await pilot.click(selector)


class _Wire(FakeSerial):
    """Simulate bounded serial reads, while allowing the test to unplug the port."""

    unplugged = False

    def read(self, size):
        if self.unplugged:
            raise serial.SerialException("Test cable unplugged")
        if not self.queued:
            time.sleep(0.005)
        return super().read(size)


@pytest.fixture
def serial_fixture(tmp_path, monkeypatch):
    candidates = [GD32]
    connected = []
    pending = []

    @contextmanager
    def open_port(path, baud):
        assert path in {port.path for port in candidates}
        assert baud == device.DEFAULT_BAUD
        assert all(wire.closed for wire in connected), "One worker must own the port across capture segments"
        wire = pending.pop(0) if pending else _Wire()
        connected.append(wire)
        try:
            yield wire
        finally:
            wire.close()

    monkeypatch.setattr(tui.sys, "platform", "darwin")
    monkeypatch.setattr(tui, "ports", lambda: candidates.copy())
    monkeypatch.setattr(device, "_connect", open_port)
    monkeypatch.setattr(capture, "capture_directory", lambda: tmp_path)
    monkeypatch.setattr(tui, "capture_directory", lambda: tmp_path)
    return candidates, connected, pending


@pytest.fixture
def version_evidence(evidence, profile, monkeypatch):
    evidence = replace(evidence, commands={**evidence.commands, "M99": _command("M99")})
    profile = replace(profile, version=MACHINE_VERSION, versions=Request("M99", "versions", timeout=0.5))
    monkeypatch.setattr(device, "REVIEWED_PROFILES", (profile,))
    return evidence, profile


def test_default_cli_launch_and_offline_flag(monkeypatch):
    launches = []
    monkeypatch.setattr(tui, "run_dashboard", lambda data, **options: launches.append((data, options)))
    runner = CliRunner()
    result = runner.invoke(cli.app, [])
    assert result.exit_code == 1
    assert "interactive terminal" in result.stderr
    # CliRunner supplies non-TTY pipes, so simulate running the actual Typer callback in a terminal.
    stream = SimpleNamespace(isatty=lambda: True)
    monkeypatch.setattr(cli, "sys", SimpleNamespace(stdin=stream, stdout=stream))
    assert runner.invoke(cli.app, []).exit_code == 0
    assert runner.invoke(cli.app, ["--data-dir", "fixture", "--offline"]).exit_code == 0
    assert runner.invoke(cli.app, ["--help"]).exit_code == 0
    assert launches == [(Path("data"), {"offline": False}), (Path("fixture"), {"offline": True})]


def test_automatic_port_requires_a_lone_gd32():
    debug = Port("/dev/cu.debug", "Built-in serial port", None, None)
    dial_in = replace(GD32, path="/dev/tty.test")
    assert live.automatic_port([GD32, debug, dial_in]) == GD32
    assert live.automatic_port([CH340]) is None
    assert live.automatic_port([GD32, CH340]) is None
    assert live.automatic_port([dial_in]) is None


def test_dashboard_waits_for_usb_and_shows_ambiguous_choices(evidence, serial_fixture):
    candidates, connected, _ = serial_fixture
    candidates.clear()
    app = tui.Dashboard(evidence)

    async def run():
        async with app.run_test(size=(120, 32)) as pilot:
            await _wait_for(lambda: "Connect the welder" in app.query_one("#connection-message", Static).content)
            assert app.screen.has_class("wide")
            candidates.extend([GD32, CH340])
            await _click(pilot, "#rescan")
            await _wait_for(lambda: "Choose the welder" in app.query_one("#connection-message", Static).content)
            assert not connected
            app.query_one("#ports", Select).value = GD32.path
            await _wait_for(lambda: not app.query_one("#connect", Button).disabled)
            await _click(pilot, "#connect")
            await _wait_for(lambda: app._update.state == "connected")
            assert connected[0].writes == []
            await pilot.press("f10")
            await _wait_for(lambda: connected[0].closed)

    try:
        asyncio.run(run())
    finally:
        app.close_serial()


def test_dashboard_live_polling_firmware_capture_and_reconnect(tmp_path, version_evidence, serial_fixture):
    _, connected, pending = serial_fixture
    evidence, _ = version_evidence
    pending.extend([_Wire(), _Wire(responses=[VERSION_RESPONSES, (STATUS_RESPONSE,), VERSION_RESPONSES]),
                    _Wire(idle=[b"Fixture Fault\n"])])
    modified = tmp_path / "modified.bin"
    modified.write_bytes(b"invalid")
    installed = tmp_path / "installed.json"
    timed = tmp_path / "timed.jsonl"
    app = tui.Dashboard(evidence)

    async def run():
        async with app.run_test() as pilot:
            await _wait_for(lambda: app._update.state == "connected")
            assert connected[0].writes == []
            assert app.query_one("#read-versions", Button).disabled
            await _click(pilot, "#identify-firmware")
            await _wait_for(lambda: app._update.history.report is not None)
            assert connected[0].closed and connected[1].writes == [b"M99\n", b"M708\n"]
            assert f"Machine {MACHINE_VERSION} · Screen 40.71.003.2540.01.B03" in app.query_one("#connection-message", Static).content
            assert not app.query_one("#save-versions", Button).disabled
            assert "Complete" in app.query_one("#report-status", Static).content
            assert "Fixture Fault" in app.query_one("#latest-fault", Static).content
            await _click(pilot, "#polling")
            await _wait_for(lambda: not app._update.polling)
            active_recording = app._update.recording
            assert json.loads(active_recording.read_text().splitlines()[0])["profile"] == MACHINE_VERSION

            await pilot.press("f4")
            assert app.query_one(TabbedContent).active == "firmware"
            app.query_one("#firmware-input", Input).value = str(modified)
            await _click(pilot, "#compare-live")
            await _wait_for(lambda: "does not match" in app.query_one("#firmware-message", Static).content)
            assert connected[1].writes == [b"M99\n", b"M708\n"]
            await _click(pilot, "#read-versions")
            await _wait_for(lambda: not app.query_one("#save-versions", Button).disabled)
            app.query_one("#versions-output", Input).value = str(installed)
            await _click(pilot, "#save-versions")
            await _wait_for(installed.is_file)
            assert set(json.loads(installed.read_text())) == {"machine", "screen", "laser_control", "welding_head"}
            assert connected[1].writes == [b"M99\n", b"M708\n", b"M99\n"]

            await pilot.press("f3")
            app.query_one("#capture-output", Input).value = str(timed)
            await _click(pilot, "#start-capture")
            await _wait_for(lambda: app._update.capture_until is not None)
            assert app._update.recording == timed
            assert "remaining" in app.query_one("#capture-timer", Static).content
            await _click(pilot, "#stop-capture")
            await _wait_for(lambda: len(connected) >= 4 and app._update.state == "connected" and app._update.capture_until is None)
            assert connected[2].writes == []
            assert list(read_capture(timed))[0].data == b"Fixture Fault\n"
            assert "Capture saved" in app.query_one("#capture-message", Static).content
            assert app._update.profile is None

            connected[-1].unplugged = True
            await _wait_for(lambda: app._monitor is None)
            assert "Test cable unplugged" in app.query_one("#connection-message", Static).content
            assert "disconnected" in app.query_one("#report-status", Static).content
            await pilot.press("f1")
            await _click(pilot, "#connect")
            await _wait_for(lambda: app._update.state == "connected")
            final_recording = app._update.recording
            assert connected[-1].writes == []
            await pilot.press("f10")
            await _wait_for(lambda: all(wire.closed for wire in connected))
            # Reading to the end checks that F10 finalized the real capture.
            assert list(read_capture(final_recording)) == []

    try:
        asyncio.run(run())
    finally:
        app.close_serial()


def test_dashboard_request_timeout_closes_the_recording(version_evidence, serial_fixture):
    _, connected, pending = serial_fixture
    evidence, _ = version_evidence
    pending.extend([_Wire(), _Wire(responses=[VERSION_RESPONSES, ()])])
    app = tui.Dashboard(evidence)

    async def run():
        async with app.run_test() as pilot:
            await _wait_for(lambda: app._update.state == "connected")
            await _click(pilot, "#identify-firmware")
            await _wait_for(lambda: app._monitor is None)
            assert "Timed out" in app.query_one("#connection-message", Static).content
            assert all(wire.closed for wire in connected)
            assert [chunk.data for chunk in read_capture(app._update.recording) if chunk.direction == "tx"] == [b"M99\n", b"M708\n"]

    try:
        asyncio.run(run())
    finally:
        app.close_serial()


def test_dashboard_rejects_unreviewed_machine_version_before_polling(version_evidence, serial_fixture):
    _, connected, pending = serial_fixture
    evidence, _ = version_evidence
    version = "40.71.004.2425.01.B01"
    responses = (f"M99 A{version}\n".encode(), *VERSION_RESPONSES[1:])
    pending.extend([_Wire(), _Wire(responses=[responses])])
    app = tui.Dashboard(evidence)

    async def run():
        async with app.run_test() as pilot:
            await _wait_for(lambda: app._update.state == "connected")
            await _click(pilot, "#identify-firmware")
            await _wait_for(lambda: app._monitor is None)
            assert f"Machine replied with firmware {version}" in app.query_one("#connection-message", Static).content
            assert connected[-1].writes == [b"M99\n"]
            assert app._update.profile is None

    try:
        asyncio.run(run())
    finally:
        app.close_serial()


def test_reviewed_display_change_requires_confirmation_and_verifies_readback(monkeypatch, version_evidence, serial_fixture):
    _, connected, pending = serial_fixture
    evidence, profile = version_evidence
    profile = replace(profile, status=None)
    monkeypatch.setattr(device, "REVIEWED_PROFILES", (profile,))
    pending.extend([_Wire(), _Wire(responses=[VERSION_RESPONSES, (b"M701 es\n",), (b"M701 es\n",),
                                             (b"M702 A1\n",), (b"M701 en\n",)])])
    app = tui.Dashboard(evidence)

    async def run():
        async with app.run_test() as pilot:
            await _wait_for(lambda: app._update.state == "connected")
            await _click(pilot, "#identify-firmware")
            await _wait_for(lambda: app._update.profile is not None)
            await pilot.press("f5")
            app.query_one("#display-preference", Select).value = "language"
            await pilot.pause(0.08)
            app.query_one("#display-value", Select).value = "en"
            await _click(pilot, "#preview-display")
            await _wait_for(lambda: len(app.screen_stack) == 2)
            assert connected[-1].writes == [b"M99\n", b"M701\n"]
            await _click(pilot, "#confirm-display")
            await _wait_for(lambda: "verified by read-back" in app.query_one("#settings-message", Static).content)
            assert connected[-1].writes == [b"M99\n", b"M701\n", b"M701\n", b"M702 A1\n", b"M701\n"]
            await pilot.press("f10")
            await _wait_for(lambda: all(wire.closed for wire in connected))

    try:
        asyncio.run(run())
    finally:
        app.close_serial()


def test_dashboard_offline_replays_pages_and_compares_settings(tmp_path, monkeypatch):
    def forbid_ports():
        pytest.fail("Offline mode must not enumerate ports")

    monkeypatch.setattr(tui, "ports", forbid_ports)
    monkeypatch.setattr(tui, "capture_directory", lambda: tmp_path)
    capture_path = tmp_path / "saved.jsonl"
    with Capture(capture_path, port="test", baud=230400, profile=None) as recording:
        recording.chunk("rx", b"hello\n" * 301)
    bad_path = tmp_path / "invalid.jsonl"
    bad_path.write_text('{"kind":"session","version":1}\n')
    before = tmp_path / "before.json"
    after = tmp_path / "after.json"
    for path, value in ((before, "en"), (after, "es")):
        path.write_text(json.dumps({"version": 1, "firmware": MACHINE_VERSION, "settings": {"language": value}}))
    root = Path(__file__).resolve().parents[1]
    image = next((root / "firmware/40.71.003/4.Welding head").glob("*.bin"))
    installed = tmp_path / "installed.json"
    installed.write_text(json.dumps({"machine": {"app": MACHINE_VERSION},
                                     "welding_head": {"app": "40.70.006.2722.01.B01"}}))
    app = tui.Dashboard(load_evidence(tmp_path / "missing"), offline=True)

    async def run():
        async with app.run_test() as pilot:
            assert "Offline" in app.query_one("#connection-status", Static).content
            assert "valid status evidence" in app.query_one("#report-status", Static).content
            # Textual's inherited quit key must be gone even outside Zellij.
            await pilot.press("ctrl+q", "f2")
            assert app.query_one(TabbedContent).active == "events"
            await pilot.press("f3")
            app.query_one("#replay-input", Input).value = str(capture_path)
            await _click(pilot, "#open-replay")
            await _wait_for(lambda: not app.query_one("#replay-next", Button).disabled)
            table = app.query_one("#replay-events").query_one(DataTable)
            assert table.row_count == 300
            await _click(pilot, "#replay-next")
            await _wait_for(lambda: table.row_count == 1)
            assert app.query_one("#replay-position", Static).content == "Page 2 of 2"
            app.query_one("#replay-input", Input).value = str(bad_path)
            await _click(pilot, "#open-replay")
            await _wait_for(lambda: app.query_one("#capture-message", Static).has_class("error"))

            await pilot.press("f4")
            app.query_one("#firmware-input", Input).value = str(image)
            await _click(pilot, "#inspect-firmware")
            await _wait_for(lambda: app.query_one("#firmware-message", Static).content == "Image inventory")
            app.query_one("#installed-input", Input).value = str(installed)
            await _click(pilot, "#compare-saved")
            await _wait_for(lambda: "1 of 1 selected boards match" in app.query_one("#firmware-message", Static).content)

            await pilot.press("f5")
            app.query_one("#settings-before", Input).value = str(before)
            app.query_one("#settings-after", Input).value = str(after)
            await _click(pilot, "#compare-settings")
            await _wait_for(lambda: app.query_one("#settings-message", Static).content == "Snapshot diff")
            assert '"language": "es"' in str(app.query_one("#settings-result", Static).content)

    asyncio.run(run())


@needs_firmware_dashboard

def test_versions_timeout_retries_after_silent_window(tmp_path, version_evidence, serial_fixture, monkeypatch):
    """A laser-board silent window fails the first read; one delayed retry lands."""
    _, connected, pending = serial_fixture
    evidence, _ = version_evidence
    partial = (
        b"M99 A40.71.003.2425.01.B04 B40.71.001.2425.00.B01\n",
        b"M99 A40.71.003.2540.01.B03\n",
        b"M99 A40.70.005.2722.01.B02 B40.70.001.2722.00.B01\n",  # laser_control never answers
    )
    # Wire 1: passive connect. Wire 2: first profile attempt fails. Wire 3: retry succeeds.
    pending.extend([_Wire(), _Wire(responses=[partial]), _Wire(), _Wire(responses=[VERSION_RESPONSES])])
    monkeypatch.setattr(tui, "VERSIONS_RETRY_DELAY", 0.1)
    app = tui.Dashboard(evidence)

    async def run():
        async with app.run_test() as pilot:
            await _wait_for(lambda: app._update.state == "connected")
            await _click(pilot, "#identify-firmware")
            await _wait_for(lambda: "Reconnecting" in app.query_one("#connection-message", Static).content)
            await _wait_for(lambda: app._update.state == "connected" and app._update.versions is not None, timeout=6)
            assert len(connected) >= 3 and connected[1].closed
            assert "Machine" in app.query_one("#connection-message", Static).content

    try:
        asyncio.run(run())
    finally:
        app.close_serial()
