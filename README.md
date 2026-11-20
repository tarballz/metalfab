# MetalFab 800W diagnostics

```sh
uv run metalfab
```

Run from this directory on macOS. `uv` installs the dependencies and opens the MetalFab 800W dashboard. Quit xTool Studio and disable lasering before the first connection. The dashboard opens a lone GD32 USB candidate in passive mode, shows a picker for ambiguous connections and CH340, and waits for the welder if no candidate is plugged in. The USB adapter is a candidate identifier. If you're unsure which port belongs to the welder, check which entry appears when you reconnect its USB cable.

- **Overview (F1)** shows the connection, latest status, and recent activity. Choose **Read firmware** to get the installed board versions from the welder. The dashboard uses the machine board's reported version to verify the reviewed profile and start polling every two seconds. Pause polling to listen passively. A reconnect starts in passive mode.
- **Events (F2)** lists live faults, reports, framing issues, and received lines. Select a row for details.
- **Captures (F3)** opens saved recordings for offline replay or starts a separate timed passive recording. A timed capture returns to passive monitoring when it finishes or you stop it.
- **Firmware (F4)** reads and saves installed versions, inspects local images or bundles, and compares reviewed stock images with live or saved versions.
- **Settings (F5)** compares saved snapshots. Live settings depend on reviewed firmware capabilities. The reviewed HJ004 profile carries the settings getters and setters listed under `metalfab settings set`.

Use Tab and Shift+Tab to move between controls, the arrow keys to navigate tables, and F10 to quit and finalize the current recording. If a reviewed request is pending, quitting waits for it to finish or time out.

Every live session records exact serial bytes in a new private JSONL file under `~/Library/Logs/MetalFab/` by default. Captures may contain device identifiers. Set `METALFAB_DATA_DIR` to point to a different evidence directory. Raw capture, raw replay, and local firmware inspection work without the evidence files. Use `uv run metalfab --offline` to open the dashboard without scanning USB ports.

## CLI fallback

Run `uv run metalfab --help` to list commands, or `uv run pytest` to run the tests.

### Device diagnostics

```sh
uv run metalfab ports
uv run metalfab capture --port /dev/cu.usbmodemYOURDEVICE --seconds 90 --output idle.jsonl
uv run metalfab replay idle.jsonl --events
uv run metalfab status --from-capture idle.jsonl --json
uv run metalfab monitor --port /dev/cu.usbmodemYOURDEVICE
```

Live status and monitoring listen passively by default. For CLI polling, pass the exact machine board version (`2425`) to `--device-version`. The screen board (`2540`) reports a separate version. The CLI checks the reviewed profile, command evidence digest, and status schema before transmitting a status request. A live capture confirmed the status query on `40.71.003.2425.01.B04`. The command registry records the stock image's SHA-256, and the handoff validator checks it against the bundled image.

### Fault explainer, system log, and job index

```sh
uv run metalfab faults                     # every known fault, with explanations
uv run metalfab faults --class 4           # one dispatcher event class only
uv run metalfab faults event.heartbeat.1.detection_abnormal   # one fault in full detail
uv run metalfab logs --port /dev/cu.usbmodemYOURDEVICE --device-version 40.71.003.2425.01.B04
uv run metalfab logs --port /dev/cu.usbmodemYOURDEVICE --device-version 40.71.003.2425.01.B04 --type 1   # ERROR records
uv run metalfab jobs --port /dev/cu.usbmodemYOURDEVICE --device-version 40.71.003.2425.01.B04
```

`faults` is the offline explainer: 69 events with curated explanations, the
machine's own on-screen text, popup-to-page mappings (all 13 popups decoded
from the firmware's init data), and decompiled raw-status bit polarity. Each
entry also carries likely causes, operator actions, and what the firmware does
(see `docs/protocol-notes.md` and `docs/laser-board-analysis.md`).

`logs` reads the machine's 100-record system log ring buffer (M351) and `jobs`
reads the machining-mode job index (M350). A machine with no jobs yet answers
`Failed to open index file: -2`; the command surfaces that line instead of a
bare timeout. When a firmware-version read times out because the laser control
board is inside one of its silent heartbeat windows, the dashboard reconnects
and retries once after five seconds.

```sh
uv run metalfab cnc --port /dev/cu.usbmodemYOURDEVICE --device-version 40.71.003.2425.01.B04
uv run metalfab cnc --port /dev/cu.usbmodemYOURDEVICE --device-version 40.71.003.2425.01.B04 --enable
uv run metalfab cnc --port /dev/cu.usbmodemYOURDEVICE --device-version 40.71.003.2425.01.B04 --enable --apply
```

`cnc` reads the CNC/machining-mode state (bare M859). Heartbeat episodes exit
CNC mode and nothing re-enables it automatically, which is why the laser stops
firing after a fault. `--enable`/`--disable` preview the reviewed M859 A1/A0
change; `--apply` rechecks the current state, sends once, and verifies by
read-back.

### Probing and verification

```sh
uv run metalfab raw "M112" --port /dev/cu.usbmodemYOURDEVICE --device-version 40.71.003.2425.01.B04
uv run metalfab raw "M812" --arm --force-unverified --port ... --device-version ... --record
uv run metalfab verify --command M99 --capture capture:20261118T045435Z-f7667a77
uv run metalfab verify --fault event.heartbeat.1.detection_abnormal --capture capture:...
```

`raw` sends exactly one command under strict gates: reviewed probe getters
(`M100`, `M112`) go unaided, everything else needs `--arm
--force-unverified` with the exact wire retyped, and registry
emission/destructive codes additionally need `--allow-emission`. `--record`
writes a successful reply into the verified overlay. `verify` records a live
observation manually. See `docs/probe-runbook.md` for the probe order and the
known traps. The overlay is the only data file the tool ever writes.

### Settings and firmware

```sh
uv run metalfab settings diff before.json after.json
uv run metalfab firmware inspect firmware/40.71.003
uv run metalfab firmware versions --port /dev/cu.usbmodemYOURDEVICE --device-version 40.71.003.2425.01.B04
uv run metalfab firmware plan firmware/40.71.003 --port /dev/cu.usbmodemYOURDEVICE --device-version 40.71.003.2425.01.B04
```

`firmware versions` queries the confirmed machine profile once and reports the four installed application versions and the bootloader versions the device returns. Check the machine version on the touchscreen before querying.

`firmware plan` compares the installed versions with a stock HJ004 image, directory, or ZIP. It verifies each image's SHA-256 against the publisher's `40.71.003` bundle before opening a port. The optional `--installed FILE` input takes saved JSON from `firmware versions` for an offline comparison. The plan reports which boards are current, could be upgraded, or would be downgraded.

Add `--verify-installed` to require every selected board to report the application's target version. For a hardware check, use the live port options. Saved JSON verifies the versions in that snapshot.

```sh
uv run metalfab firmware update-head firmware/40.71.003 \
  --port /dev/cu.usbmodemYOURDEVICE --device-version 40.71.003.2425.01.B04
```

`firmware update-head` previews the HJ004 800W welding-head update using live versions. It accepts the stock `.bin`, release directory, or ZIP. The supported transition is `40.70.005.2722.01.B02` to `40.70.006.2722.01.B01` on head bootloader `40.70.001.2722.00.B01`.

Disable lasering before adding `--apply` and keep power and USB connected during the update. The command verifies the stock image, transfers it to the head, then reads the reported version after reboot. The transfer was tested against simulated replies, and its outgoing bytes matched the vendor client in an offline comparison. A physical flash and interrupted-transfer recovery remain untested.

`settings export` reads the reviewed getters (M804/M805, M810-M813, M815, M817,
M818, M829, M831-M833, M840, M845, M848, M857, M864, M865) into a JSON
snapshot. `settings set NAME VALUE` previews a reviewed change; `--apply`
rechecks the current value, sends once, and verifies the result, either through
the setting's bare readback form or the setter's own clamped-value response.
Reviewed setters: M800 (`laser_power`), M801 (`feed_speed`), M802
(`focus_correction`), M807, M856, M859 (via `metalfab cnc`), M866, M867. The
offset-named settings keep their offsets until live use identifies them.
`settings set-display` previews changes to `language` and `imperial_units` once
their handlers and responses are established.

Part 1 owns `commands.json`, `faults.json`, `status.json`, and the protocol notes. Record live confirmation in `data/verified-overlay.json` using `verified_by` to identify the capture. Transmission eligibility comes from the reviewed profile in the source code.

## For contributors

The vendor firmware images and the decompiled artifacts (`firmware/` and `re/`)
are not redistributed in this repository. The committed `data/*.json` and
`docs/` capture the analysis. The firmware-dependent tests skip when the images
are absent; the data generators (`tools/build_*.py`, `tools/validate_data.py`)
report missing inputs or skip the digest check gracefully. To regenerate the
data from source you need your own copies of the HJ004 images and a Ghidra
project seeded with `tools/ghidra/Bootstrap.java`.

## xTool Studio evidence

Studio 1.10.21 includes HJ004 recognition in the firmware updater, where HJ004 maps to firmware family `40.71` and the 800W wire-feeder update entry is hidden. The local extension cache and bundled extension archive include the HJ003 CNC cutter extension. Its HJ004 reference points to an illustration. The inspected desktop resources establish neither a wire-feeder error recovery command nor the HJ004 display preference handlers.

The machine firmware fault table identifies the diagnostic text `Send Wire Communicate Abnormal` as wire-feeder event class 6, subcode 1. Replay and monitoring surface this message if it appears in serial traffic. The local Studio logs contained neither this message nor an HJ004 installed firmware version. Correlation with the touchscreen error requires a device capture.
