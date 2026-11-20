"""Command-line entry points for the MetalFab diagnostics tool."""

from dataclasses import asdict, replace
import json
from pathlib import Path
import subprocess
import sys
import time
from typing import Iterator, NoReturn
from zipfile import BadZipFile

from rich.console import Console, Group
from rich.live import Live
from rich.table import Table
from rich.text import Text
import typer

from .capture import CaptureError, replay_capture
from .contracts import Evidence, load_evidence
from .device import DEFAULT_BAUD, DeviceError, Profile, Session, live_session, ports, select_profile
from .inventory import firmware_plan, inspect_firmware, load_stock_head, stock_images
from .logs import parse_job_index, parse_system_log
from .reports import Event, Report
from .settings import settings_diff, settings_snapshot

app = typer.Typer(invoke_without_command=True, no_args_is_help=False)
settings = typer.Typer(no_args_is_help=True)
firmware = typer.Typer(no_args_is_help=True)
app.add_typer(settings, name="settings")
app.add_typer(firmware, name="firmware")
errors = Console(stderr=True)
output = Console()
EXPECTED_ERRORS = (DeviceError, CaptureError, OSError, ValueError, BadZipFile, subprocess.SubprocessError)


@app.callback()
def main(
    ctx: typer.Context,
    data_dir: Path = typer.Option(Path("data"), envvar="METALFAB_DATA_DIR", help="Directory containing Part 1 JSON data."),
    offline: bool = typer.Option(False, "--offline", help="Open the dashboard without scanning USB ports."),
) -> None:
    """Open the MetalFab 800W dashboard or run a diagnostics command."""
    ctx.obj = data_dir
    if ctx.invoked_subcommand is None:
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            _fail(DeviceError("The dashboard needs an interactive terminal. See 'metalfab --help' for CLI commands."))
        from .tui import run_dashboard

        run_dashboard(data_dir, offline=offline)
    elif offline:
        raise typer.BadParameter("Applies to the dashboard", param_hint="--offline")


def _evidence(ctx: typer.Context) -> Evidence:
    evidence = load_evidence(ctx.obj)
    for problem in evidence.problems:
        errors.print(Text(problem, style="yellow"))
    return evidence


def _fail(error: Exception) -> NoReturn:
    errors.print(Text(str(error), style="bold red"))
    raise typer.Exit(1)


def _profile(evidence: Evidence, version: str | None) -> Profile | None:
    return select_profile(evidence, version) if version is not None else None


def _recording(session: Session) -> None:
    errors.print(Text(f"Capture file {session.capture.path}"))


@app.command("ports")
def list_serial_ports(as_json: bool = typer.Option(False, "--json")) -> None:
    """List serial ports and adapter candidates without opening them."""
    try:
        available = ports()
    except EXPECTED_ERRORS as error:
        _fail(error)
    if as_json:
        typer.echo(json.dumps([asdict(port) for port in available], indent=2))
        return
    table = Table("Port", "USB ID", "Identification")
    for port in available:
        adapter = f"{port.adapter} candidate" if port.adapter else f"Unknown ({port.description})"
        table.add_row(Text(port.path), port.usb_id or "—", Text(adapter))
    output.print(table)


@app.command()
def faults(
    ctx: typer.Context,
    fault_id: str | None = typer.Argument(None, help="Fault id for full-detail output."),
    event_class: int | None = typer.Option(None, "--class", min=0, max=10,
                                          help="Filter by dispatcher event class (0-10)."),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """List the fault dictionary with explanations and the machine's on-screen text."""
    evidence = _evidence(ctx)
    if fault_id is not None:
        fault = evidence.faults.get(fault_id)
        if fault is None:
            _fail(DeviceError(f"Unknown fault id {fault_id!r}; run 'metalfab faults' for the list"))
        if as_json:
            typer.echo(json.dumps(fault, indent=2))
            return
        _print_fault_detail(fault)
        return
    entries = sorted(
        (fault for fault in evidence.faults.values()
         if event_class is None or fault.get("evidence", {}).get("event_class") == event_class),
        key=lambda fault: fault["id"],
    )
    if as_json:
        typer.echo(json.dumps(entries, indent=2))
        return
    table = Table("Event", "Serial string", "Severity", "Explanation", "Popup", "On-screen title")
    for fault in entries:
        popup = fault.get("popup")
        screen_text = fault.get("screen_text")
        popup_cell = f"{popup['id']} -> page {popup['page']}" if popup else "—"
        table.add_row(
            Text(fault["id"]), Text(fault["source_string"]), Text(fault["severity"]),
            Text(fault["explanation"]), Text(popup_cell),
            Text(screen_text["title"] if screen_text else "—"),
        )
    output.print(table)


def _print_fault_detail(fault: dict) -> None:
    """Render one fault with every layer the explainer knows about."""
    output.print(Text(f"{fault['id']}", style="bold"))
    output.print(Text(f"Serial: {fault['source_string']}   Severity: {fault['severity']}"))
    output.print()
    output.print(Text(fault["explanation"]))
    for heading, key in (("Likely causes", "causes"), ("Operator actions", "actions"),
                         ("Firmware behavior", "firmware")):
        items = fault.get(key)
        if not items:
            continue
        output.print(Text(f"\n{heading}:", style="bold underline"))
        for item in items:
            output.print(Text(f"  • {item}"))
    popup = fault.get("popup")
    screen_text = fault.get("screen_text")
    if popup or screen_text:
        output.print(Text("\nTouchscreen:", style="bold underline"))
        if popup:
            output.print(Text(f"  popup {popup['id']} -> page {popup['page']}"))
        if screen_text:
            output.print(Text(f"  {screen_text['title']}"))
            if screen_text.get("body"):
                output.print(Text(f"  {screen_text['body']}"))
    bit = fault.get("bit")
    if bit:
        output.print(Text(f"\nRaw status: {bit['word']} bit {bit['index']} active={bit['active']}"))
    evidence = fault.get("evidence", {})
    if evidence:
        detail = ", ".join(f"{name}={value}" for name, value in evidence.items())
        output.print(Text(f"\nEvidence: {detail}"))


@app.command()
def jobs(
    ctx: typer.Context,
    port: str = typer.Option(..., help="Confirmed device port from 'metalfab ports'."),
    device_version: str | None = typer.Option(None, help="Exact machine version shown on the touchscreen."),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Read the machining-mode job index (M350)."""
    evidence = _evidence(ctx)
    try:
        profile = _profile(evidence, device_version)
        with live_session(port, evidence, profile=profile, baud=profile.baud if profile else DEFAULT_BAUD) as session:
            _recording(session)
            if profile is None or profile.jobs is None:
                raise DeviceError("No reviewed job-index request is available for this firmware")
            block = session.read_block(profile.jobs)
        parsed = parse_job_index("\n".join(block["lines"]))
        parsed["truncated"] = block["truncated"]
        if as_json:
            typer.echo(json.dumps(parsed, indent=2))
            return
        for line in parsed["lines"]:
            output.print(Text(line))
        if parsed["truncated"]:
            errors.print(Text("The job-index dump was cut short by stream silence.", style="yellow"))
    except EXPECTED_ERRORS as error:
        _fail(error)


@app.command()
def logs(
    ctx: typer.Context,
    port: str = typer.Option(..., help="Confirmed device port from 'metalfab ports'."),
    device_version: str | None = typer.Option(None, help="Exact machine version shown on the touchscreen."),
    log_type: int | None = typer.Option(None, "--type", min=1, max=3,
                                        help="Filter: 1=ERROR, 2=WARNING, 3=other."),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Read the 100-record system log ring buffer (M351)."""
    evidence = _evidence(ctx)
    try:
        profile = _profile(evidence, device_version)
        with live_session(port, evidence, profile=profile, baud=profile.baud if profile else DEFAULT_BAUD) as session:
            _recording(session)
            if profile is None or profile.logs is None:
                raise DeviceError("No reviewed system-log request is available for this firmware")
            spec = replace(profile.logs, wire=f"M351 A{log_type}") if log_type is not None else profile.logs
            block = session.read_block(spec)
        parsed = parse_system_log("\n".join(block["lines"]))
        if as_json:
            typer.echo(json.dumps(asdict(parsed), indent=2))
            return
        table = Table("#", "Time", "Level", "Class", "Param", "Message")
        for record in parsed.records:
            stamp = f"{record.hours:02d}:{record.minutes:02d}:{record.seconds:02d}.{record.millis:03d}"
            table.add_row(str(record.index), stamp, record.level, record.class_name,
                          f"0x{record.param:08X}", record.message)
        output.print(table)
        if block["truncated"]:
            errors.print(Text("The log dump was cut short by stream silence.", style="yellow"))
    except EXPECTED_ERRORS as error:
        _fail(error)


@app.command()
def capture(
    ctx: typer.Context,
    port: str = typer.Option(..., help="Confirmed device port from 'metalfab ports'."),
    seconds: float = typer.Option(90, min=1, max=3600),
    output_path: Path | None = typer.Option(None, "--output", help="Capture path. The file must be new."),
) -> None:
    """Record serial traffic without transmitting requests."""
    evidence = _evidence(ctx)
    try:
        count = 0
        with live_session(port, evidence, recording=output_path) as session:
            _recording(session)
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline:
                count += sum(event.line is not None for event in session.receive())
            count += sum(event.line is not None for event in session.finish())
        typer.echo(f"Captured {count} decoded lines.")
    except EXPECTED_ERRORS as error:
        _fail(error)


@app.command()
def replay(
    ctx: typer.Context, path: Path,
    events: bool = typer.Option(False, "--events", help="Include every received line and framing issue."),
) -> None:
    """Decode a recording offline and print JSON Lines."""
    evidence = _evidence(ctx)
    try:
        for elapsed, event in replay_capture(evidence, path):
            if event.line is not None and (events or evidence.report is None):
                typer.echo(json.dumps({"elapsed_ns": elapsed, "kind": "line", **asdict(event.line)}))
            if event.fault is not None:
                typer.echo(json.dumps({"elapsed_ns": elapsed, "kind": "fault", **event.fault}))
            if event.report is not None:
                typer.echo(json.dumps({"elapsed_ns": elapsed, "kind": "status", **asdict(event.report)}))
    except EXPECTED_ERRORS as error:
        _fail(error)


def _report_view(report: Report, previous: Report | None = None) -> Group:
    table = Table("Status field", "Value", title="Complete report" if report.complete else "Incomplete report")
    for key, value in report.values.items():
        changed = previous is not None and previous.values.get(key) != value
        table.add_row(key.replace("_", " ").capitalize(), Text(str(value), style="bold cyan" if changed else ""))
    for section, value in report.raw_status.items():
        table.add_row(Text(f"{section} raw word"), Text(value))
    for fault in report.faults:
        table.add_row("Fault", Text(f'{fault["message"]} ({fault["severity"]})', style="yellow"))
    return Group(table, *(Text(issue, style="yellow") for issue in report.issues))


def _latest_report(events: Iterator[Event]) -> Report | None:
    latest = None
    for event in events:
        if event.report is not None:
            latest = event.report
    return latest


def _passive_report(session: Session, timeout: float) -> Report | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        # Process the entire serial chunk even when it includes a complete report.
        reports = [event.report for event in session.receive() if event.report and event.report.complete]
        if reports:
            return reports[-1]
    list(session.finish())
    return session.report_history[-1] if session.report_history else None


@app.command()
def status(
    ctx: typer.Context,
    port: str | None = typer.Option(None),
    from_capture: Path | None = typer.Option(None, "--from-capture"),
    device_version: str | None = typer.Option(None, help="Exact machine version shown on the touchscreen."),
    as_json: bool = typer.Option(False, "--json"),
    timeout: float = typer.Option(10.0, min=0.1, help="Seconds to listen in passive mode."),
) -> None:
    """Read a report from a live session or capture."""
    if (port is None) == (from_capture is None) or (from_capture is not None and device_version is not None):
        raise typer.BadParameter("Choose either --port or --from-capture. --device-version applies to live sessions.")
    evidence = _evidence(ctx)
    try:
        if evidence.report is None:
            raise DeviceError("Status requires a valid status.json. Use replay --events to inspect raw lines.")
        if from_capture is not None:
            report = _latest_report(event for _, event in replay_capture(evidence, from_capture))
        else:
            profile = _profile(evidence, device_version)
            with live_session(port, evidence, profile=profile, baud=profile.baud if profile else DEFAULT_BAUD) as session:
                _recording(session)
                report = session.poll_status() if session.can_poll_status() else _passive_report(session, timeout)
        if report is None:
            raise DeviceError("No status report arrived. The recording can still be inspected with replay --events.")
        if as_json:
            typer.echo(json.dumps(asdict(report), indent=2))
        else:
            output.print(_report_view(report))
        if not report.complete:
            raise typer.Exit(1)
    except EXPECTED_ERRORS as error:
        _fail(error)


def _dashboard(session: Session, now: float, updated: float | None, stale_after: float, state: str) -> Group:
    history = session.report_history
    if state == "Connected":
        if updated is None:
            state = "Waiting for a status report"
        elif now - updated >= stale_after:
            state = f"Stale ({now - updated:.1f}s since last report)"
    parts = [Text(f"MetalFab · {state}", style="bold")]
    if history:
        previous = history[-2] if len(history) > 1 else None
        parts.append(_report_view(history[-1], previous))
    if session.fault_history:
        fault = session.fault_history[-1]
        parts.append(Text(f'Latest fault event: {fault["message"]} ({fault["severity"]})', style="yellow"))
    if session.line_history:
        line = session.line_history[-1]
        text = "".join(char if char.isprintable() else " " for char in line.text)[:160]
        parts.append(Text(f"Latest serial event: {line.issue or text}"))
    return Group(*parts)


@app.command()
def monitor(
    ctx: typer.Context,
    port: str = typer.Option(...),
    device_version: str | None = typer.Option(None, help="Exact machine version shown on the touchscreen."),
    interval: float = typer.Option(2.0, min=1.0, max=60.0, help="Seconds between reviewed status requests."),
) -> None:
    """Show recent serial events and status changes until interrupted."""
    evidence = _evidence(ctx)
    try:
        profile = _profile(evidence, device_version)
        with live_session(port, evidence, profile=profile, baud=profile.baud if profile else DEFAULT_BAUD) as session:
            _recording(session)
            poll = session.can_poll_status()
            updated = None
            next_request = time.monotonic()
            stale_after = max(5.0, 2 * interval)
            with Live(console=output, refresh_per_second=4) as panel:
                try:
                    while True:
                        now = time.monotonic()
                        if poll and now >= next_request:
                            session.poll_status()
                            updated = time.monotonic()
                            next_request = updated + interval
                        else:
                            for event in session.receive():
                                if event.report is not None and event.report.complete:
                                    updated = time.monotonic()
                        panel.update(_dashboard(session, time.monotonic(), updated, stale_after, "Connected"))
                except KeyboardInterrupt:
                    list(session.finish())
                    panel.update(_dashboard(session, time.monotonic(), updated, stale_after, "Stopped"))
                except EXPECTED_ERRORS:
                    list(session.finish())
                    panel.update(_dashboard(session, time.monotonic(), updated, stale_after, "Disconnected"))
                    raise
    except EXPECTED_ERRORS as error:
        _fail(error)


@settings.command("export")
def export_settings(ctx: typer.Context, port: str = typer.Option(...), device_version: str = typer.Option(...)) -> None:
    """Print a JSON snapshot of settings with reviewed read-only queries."""
    evidence = _evidence(ctx)
    try:
        profile = select_profile(evidence, device_version)
        if not (profile.settings or profile.display):
            raise DeviceError("No reviewed settings getters are available for this firmware")
        with live_session(port, evidence, profile=profile, baud=profile.baud) as session:
            _recording(session)
            values = session.snapshot()
        typer.echo(json.dumps(settings_snapshot(profile.version, values), indent=2, sort_keys=True))
    except EXPECTED_ERRORS as error:
        _fail(error)


@settings.command("diff")
def diff_settings(before: Path, after: Path) -> None:
    """Compare two saved settings snapshots without connecting to the device."""
    try:
        for line in settings_diff(before, after):
            typer.echo(line)
    except EXPECTED_ERRORS as error:
        _fail(error)


@settings.command("set-display")
def set_display(
    ctx: typer.Context, preference: str, value: str,
    port: str = typer.Option(...), device_version: str = typer.Option(...),
    apply: bool = typer.Option(False, "--apply", help="Write the previewed change and verify the result."),
) -> None:
    """Preview or apply a reviewed language or display-unit change."""
    evidence = _evidence(ctx)
    try:
        profile = select_profile(evidence, device_version)
        options = profile.display.get(preference)
        if options is None or not options.writes:
            raise DeviceError(f"No reviewed display setter is available for {preference}")
        with live_session(port, evidence, profile=profile, baud=profile.baud) as session:
            _recording(session)
            current = session.preview_display(preference, value)
            if current == value:
                typer.echo(f"{preference} is already {json.dumps(value)}")
                return
            typer.echo(f"- {preference} = {json.dumps(current)}")
            typer.echo(f"+ {preference} = {json.dumps(value)}")
            if apply:
                session.apply_display(preference, value, current)
                typer.echo("Display preference verified by read-back.")
    except EXPECTED_ERRORS as error:
        _fail(error)


@firmware.command("inspect")
def firmware_inventory(path: Path) -> None:
    """Inspect a .bin, directory, or ZIP as a local firmware bundle."""
    try:
        entries = list(inspect_firmware(path))
        if not entries:
            raise DeviceError("No .bin firmware images found")
        typer.echo(json.dumps(entries, indent=2))
    except EXPECTED_ERRORS as error:
        _fail(error)


@firmware.command("versions")
def firmware_versions(ctx: typer.Context, port: str = typer.Option(...), device_version: str = typer.Option(...)) -> None:
    """Report installed versions where the profile has reviewed read-only queries."""
    evidence = _evidence(ctx)
    try:
        profile = select_profile(evidence, device_version)
        if not profile.versions:
            raise DeviceError("No reviewed version getter is available. Check the vendor UI.")
        with live_session(port, evidence, profile=profile, baud=profile.baud) as session:
            _recording(session)
            versions = {board: asdict(version) for board, version in session.installed_versions().items()}
            typer.echo(json.dumps(versions, indent=2, sort_keys=True))
    except EXPECTED_ERRORS as error:
        _fail(error)


@firmware.command("plan")
def plan_firmware(
    ctx: typer.Context, path: Path,
    installed: Path | None = typer.Option(None, help="Read JSON saved from 'firmware versions'."),
    port: str | None = typer.Option(None, help="Query the connected machine for versions."),
    device_version: str | None = typer.Option(None, help="Exact machine version for a live query."),
    verify_installed: bool = typer.Option(
        False, "--verify-installed", help="Require each board to report the selected application's version.",
    ),
) -> None:
    """Verify stock images and show which installed boards have a version change."""
    if (installed is None) == (port is None) or (port is None) != (device_version is None):
        raise typer.BadParameter("Choose --installed, or both --port and --device-version.")
    try:
        images = stock_images(path)
        if installed is not None:
            versions = json.loads(installed.read_text(encoding="utf-8"))
        else:
            evidence = _evidence(ctx)
            profile = select_profile(evidence, device_version)
            with live_session(port, evidence, profile=profile, baud=profile.baud) as session:
                _recording(session)
                versions = {board: asdict(version) for board, version in session.installed_versions().items()}
        plan = firmware_plan(images, versions)
        typer.echo(json.dumps(plan, indent=2))
        if verify_installed:
            # Verify what each board reports. A valid image or compatible board
            # alone does not establish that the application is installed.
            mismatches = [row["board"] for row in plan if row["action"] != "current"]
            if mismatches:
                raise DeviceError(f"Installed firmware differs from the selected image on {', '.join(mismatches)}")
    except EXPECTED_ERRORS as error:
        _fail(error)


@firmware.command("update-head")
def update_head(
    ctx: typer.Context, path: Path,
    port: str = typer.Option(..., help="Confirmed USB port on the HJ004."),
    device_version: str = typer.Option(..., help="Exact machine firmware shown on the touchscreen."),
    apply: bool = typer.Option(False, "--apply", help="Install the stock application and verify the reported version."),
) -> None:
    """Preview or install the stock HJ004 welding-head update."""
    try:
        image = load_stock_head(path)
        evidence = _evidence(ctx)
        profile = select_profile(evidence, device_version)
        with live_session(port, evidence, profile=profile, baud=profile.baud) as session:
            _recording(session)
            if apply:
                errors.print(Text("Checking live versions before the welding-head update. Leave power and USB connected."))
            result = session.update_stock_head(image, apply=apply)
        typer.echo(json.dumps(result, indent=2))
    except EXPECTED_ERRORS as error:
        _fail(error)
