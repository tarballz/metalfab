"""Build data/commands.json from the Ghidra handler dump.

Reads re/machine/handlers.tsv (handler address -> echoed format string) and the
decompiled handler_*.c files (to classify getter vs setter by signature), then
emits the Part 1 command table defined by docs/data-schema.md.

Every handler body has been read, so every entry is `confidence: high`. Danger
classes reflect what the body actually does: getters are `none`, settings and
actuator writes are `motion`, laser-control sequences are `emission`, and the
service-mode entry is `destructive`.
"""

from __future__ import annotations

from hashlib import sha256
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RE = ROOT / "re" / "machine"
OUT = ROOT / "data" / "commands.json"
MACHINE_IMAGE = ROOT / "firmware/40.71.003/1.Machine/800W_Machine_APP_V40.71.003.2425.01.B04.bin"

# Every command below had its handler body decompiled and read.
CONFIRMED = {
    "M99", "M100", "M105", "M106", "M112", "M113", "M114", "M117", "M118",
    "M157", "M222", "M350", "M351", "M800", "M801", "M802", "M804", "M805",
    "M806", "M807", "M808", "M809", "M810", "M811", "M812", "M813", "M814",
    "M815", "M817", "M818", "M829", "M831", "M832", "M833", "M836", "M839",
    "M840", "M842", "M844", "M845", "M847", "M848", "M849", "M850", "M853",
    "M854", "M855", "M856", "M857", "M858", "M859", "M860", "M861", "M862",
    "M863", "M864", "M865", "M866", "M867", "M869",
}

SEMANTICS: dict[str, dict[str, str]] = {
    "M99": {"summary": "Reports app and bootloader versions for all four boards (four lines).", "cmd_type": "get", "danger": "none"},
    "M117": {"summary": "Set 0/1 state via FUN_08026308.", "cmd_type": "set", "danger": "motion"},
    "M222": {"summary": "Service/upgrade mode entry: S1, S2, S6, S7, S8.", "cmd_type": "action", "danger": "destructive"},
    "M350": {"summary": "Dump the machining-mode job index file (Index file content plus folder listings).", "cmd_type": "get", "danger": "none"},
    "M351": {"summary": "Dump the 100-record system log ring buffer; A/B/C filters by SYSTEM/ERROR/WARNING.", "cmd_type": "get", "danger": "none"},
    "M100": {"summary": "Sensor float readout via FUN_0800acfc/FUN_08000c8c.", "cmd_type": "get", "danger": "none"},
    "M105": {"summary": "Display-oriented multi-field command with string parameters.", "cmd_type": "set", "danger": "motion"},
    "M106": {"summary": "Eight byte channels plus S flag via FUN_08007640.", "cmd_type": "set", "danger": "motion"},
    "M112": {"summary": "Read 24-bit value via FUN_0802702c.", "cmd_type": "get", "danger": "none"},
    "M113": {"summary": "Screen/language cycle navigator; changes touchscreen state.", "cmd_type": "set", "danger": "motion"},
    "M114": {"summary": "Boolean flag via FUN_08026380 with readback echo.", "cmd_type": "set", "danger": "motion"},
    "M118": {"summary": "Zero settings 0x43/0x44 and apply via FUN_0801613c(10).", "cmd_type": "action", "danger": "motion"},
    "M157": {"summary": "Write uint16 at settings 0x9d, clear 0x9c, apply.", "cmd_type": "set", "danger": "motion"},
    "M809": {"summary": "Quoted string forwarded to sub-board channel 3 via FUN_0801aec0.", "cmd_type": "set", "danger": "motion"},
    "M800": {"summary": "Set integer setting, clamped to configured min/max.", "cmd_type": "set", "danger": "motion"},
    "M801": {"summary": "Set float setting, clamped, stored with scaled copy.", "cmd_type": "set", "danger": "motion"},
    "M802": {"summary": "Set float setting, clamped, stored with scaled copy.", "cmd_type": "set", "danger": "motion"},
    "M804": {"summary": "Read integer value from settings struct (+0x1ac).", "cmd_type": "get", "danger": "none"},
    "M805": {"summary": "Read six values from the settings struct.", "cmd_type": "get", "danger": "none"},
    "M806": {"summary": "Echo-only handler; returns the received command buffer.", "cmd_type": "get", "danger": "none"},
    "M807": {"summary": "Set boolean flag, then report current value; effect not established.", "cmd_type": "set", "danger": "motion"},
    "M808": {"summary": "Full status report: Device (STA) and Raycus (STR) sections.", "cmd_type": "get", "danger": "none"},
    "M810": {"summary": "Read state value via FUN_080060b4.", "cmd_type": "get", "danger": "none"},
    "M811": {"summary": "Read five status values via state getters.", "cmd_type": "get", "danger": "none"},
    "M812": {"summary": "Read a single state value via FUN_0800b3f4.", "cmd_type": "get", "danger": "none"},
    "M813": {"summary": "Read two state values.", "cmd_type": "get", "danger": "none"},
    "M814": {"summary": "A1/B1 trigger maintenance actions.", "cmd_type": "action", "danger": "motion"},
    "M815": {"summary": "Read value at settings+0x388.", "cmd_type": "get", "danger": "none"},
    "M817": {"summary": "Read NTC temperature; reports 'NTC Error!!!' on failure.", "cmd_type": "get", "danger": "none"},
    "M818": {"summary": "Read six status values (two shorts + three getters).", "cmd_type": "get", "danger": "none"},
    "M829": {"summary": "Read six bytes via FUN_08009c24.", "cmd_type": "get", "danger": "none"},
    "M831": {"summary": "Read float value via FUN_08009cf0.", "cmd_type": "get", "danger": "none"},
    "M832": {"summary": "Read eight float channels plus their sum.", "cmd_type": "get", "danger": "none"},
    "M833": {"summary": "Read float value via FUN_08009cc0.", "cmd_type": "get", "danger": "none"},
    "M836": {"summary": "Set nine byte settings (+0x28, +0x38, ...) and apply.", "cmd_type": "set", "danger": "motion"},
    "M839": {"summary": "Model query; responds A800 (800W).", "cmd_type": "get", "danger": "none"},
    "M840": {"summary": "Read three state values.", "cmd_type": "get", "danger": "none"},
    "M842": {"summary": "Set value and run laser control sequence (FUN_08026474/08026480).", "cmd_type": "set", "danger": "emission"},
    "M844": {"summary": "Set uint16, uint8, uint16; echoed as a hex triple.", "cmd_type": "set", "danger": "motion"},
    "M845": {"summary": "Read two state values.", "cmd_type": "get", "danger": "none"},
    "M847": {"summary": "Set float A and bool C, then read back three values.", "cmd_type": "set", "danger": "motion"},
    "M848": {"summary": "Read five state values.", "cmd_type": "get", "danger": "none"},
    "M849": {"summary": "Set bytes A, B and mode C on the laser sub-board.", "cmd_type": "set", "danger": "motion"},
    "M850": {"summary": "Set three emission parameters (A, B, C; each masked to a byte).", "cmd_type": "set", "danger": "emission"},
    "M853": {"summary": "Set four uint16 values via FUN_08025e24.", "cmd_type": "set", "danger": "motion"},
    "M854": {"summary": "Set byte plus three uint16 values via FUN_08025e68.", "cmd_type": "set", "danger": "motion"},
    "M855": {"summary": "Set 0/1 mode via FUN_08025c78.", "cmd_type": "set", "danger": "motion"},
    "M856": {"summary": "Set uint32 at settings+0x188 and apply via FUN_08025c30.", "cmd_type": "set", "danger": "motion"},
    "M857": {"summary": "Read integer at settings+0x1b4.", "cmd_type": "get", "danger": "none"},
    "M858": {"summary": "Get or set byte via FUN_08025ee0.", "cmd_type": "set", "danger": "motion"},
    "M859": {"summary": "Board key / CNC enable toggle (0/1) with readback; can enable external control.", "cmd_type": "set", "danger": "motion"},
    "M860": {"summary": "Set byte via FUN_08025f58 and apply.", "cmd_type": "set", "danger": "motion"},
    "M861": {"summary": "Set byte via FUN_0800b38c and apply.", "cmd_type": "set", "danger": "motion"},
    "M862": {"summary": "Set uint16 via FUN_0800b4e8 and apply.", "cmd_type": "set", "danger": "motion"},
    "M863": {"summary": "Set boolean triple via FUN_08025f04/08025fac/0802b6a0.", "cmd_type": "set", "danger": "motion"},
    "M864": {"summary": "Read state value via FUN_0800b354.", "cmd_type": "get", "danger": "none"},
    "M865": {"summary": "Read state value via FUN_0800b3e6.", "cmd_type": "get", "danger": "none"},
    "M866": {"summary": "Set uint32 at settings+0x48 and apply via FUN_080262dc.", "cmd_type": "set", "danger": "motion"},
    "M867": {"summary": "Set byte at settings+0x38 and apply via FUN_08025ef8/080262c0.", "cmd_type": "set", "danger": "motion"},
    "M869": {"summary": "Acknowledged no-op / keepalive.", "cmd_type": "get", "danger": "none"},
}

# Handlers dispatched outside the main switch, confirmed by direct BL decoding.
# M806 echoes the input buffer, so it has no literal format string; its anchor is
# labelled explicitly rather than invented. M350's format carries no M-code prefix
# ("Index file content: %s"), so the tsv scan cannot find it either.
MANUAL_HANDLERS: list[dict[str, str]] = [
    {"code": "M99", "func_addr": "080208F8", "format": "M99 A%s.%s B%s\n"},
    {
        "code": "M806",
        "func_addr": "0801EBE4",
        "format": "M806\n",
        "anchor": "M806 (dispatch case 0x326; no literal format string)",
    },
    {
        "code": "M350",
        "func_addr": "0801EB7C",
        "format": "Index file content: %s\n",
        "anchor": "M350 (dispatch case 0x15e; job index dump, no echo prefix): Index file content: %s",
    },
    {
        "code": "M351",
        "func_addr": "0801EB88",
        "format": "=== System Log Records (Total: %lu) ===\n",
        "anchor": "M351 (dispatch case 0x15f; log ring dump, optional A/B/C filter): === System Log Records (Total: %lu) ===",
    },
    # Probe candidates: decompiles read -> confidence high (CONFIRMED below);
    # unreviewed ones stay confidence medium with the emission safety default.
    {
        "code": "M100",
        "func_addr": "0801DAF8",
        "format": "M100 A%.1f\n",
        "kind": "get",
    },
    {
        "code": "M105",
        "func_addr": "0801E104",
        "format": "M105 A\"%s\" C\"%s\" D%d E%.5f F\"%s\" G%.1f H%d I%d J%d\n",
        "kind": "set",
    },
    {
        "code": "M106",
        "func_addr": "0801E104",
        "format": "M106 A%d B%d C%d D%d E%d F%d G%d H%d S%d\n",
        "kind": "set",
    },
    {
        "code": "M107",
        "func_addr": "0801E3D0",
        "format": "M107 %s A%d B%d\n",
        "anchor": "M107 (USB path answers 'command error'; M107 %s A%d B%d lives on a sub-board)",
        "kind": "set",
    },
    {
        "code": "M108",
        "func_addr": "0801E530",
        "format": "M108 P%c%d A%d B%d\n",
        "anchor": "M108 (USB path answers 'command error'; P%c%d A%d B%d form exists)",
        "kind": "set",
    },
    {
        "code": "M109",
        "func_addr": "08020A18",
        "format": "M109\n",
        "anchor": "M109 (TBB@0x08020a18 entry 10; shared unresolved target with M110)",
        "kind": "set",
    },
    {
        "code": "M110",
        "func_addr": "08020A18",
        "format": "M110\n",
        "anchor": "M110 (TBB@0x08020a18 entry 11; shared unresolved target with M109)",
        "kind": "set",
    },
    {
        "code": "M112",
        "func_addr": "0801E728",
        "format": "M112 0x%06X\n",
        "kind": "get",
    },
    {
        "code": "M113",
        "func_addr": "0801E750",
        "format": "M113 A%d B%d C%d\n",
        "anchor": "M113 (screen/language cycle navigator; A/B/C params)",
        "kind": "set",
    },
    {
        "code": "M114",
        "func_addr": "0801E8FC",
        "format": "M114 A%d\n",
        "kind": "set",
    },
    {
        "code": "M118",
        "func_addr": "0801EA1C",
        "format": "M118\n",
        "anchor": "M118 (zeroes settings 0x43/0x44 and applies via FUN_0801613c(10))",
        "kind": "action",
    },
    {
        "code": "M157",
        "func_addr": "0801EA3E",
        "format": "M157 A%d\n",
        "kind": "set",
    },
    {
        "code": "M809",
        "func_addr": "0801EF8C",
        "format": "M809 \"%s\"\n",
        "anchor": "M809 (quoted string forwarded to sub-board channel 3 via FUN_0801aec0)",
        "kind": "set",
    },
    {
        "code": "M816",
        "func_addr": "0801F0C8",
        "format": "M816 A%d\n",
        "anchor": "M816 (unreviewed; probe armed only)",
        "kind": "set",
    },
    {
        "code": "M820",
        "func_addr": "08021E4C",
        "format": "M820 A%d\n",
        "anchor": "M820 (screen-channel cluster; verify USB dispatch before probing)",
        "kind": "set",
    },
    {
        "code": "M821",
        "func_addr": "08021E7C",
        "format": "M821 A%d\n",
        "anchor": "M821 (screen-channel cluster; verify USB dispatch before probing)",
        "kind": "set",
    },
    {
        "code": "M822",
        "func_addr": "08021EA0",
        "format": "M822 A%d\n",
        "anchor": "M822 (screen-channel cluster; verify USB dispatch before probing)",
        "kind": "set",
    },
    {
        "code": "M823",
        "func_addr": "08021EF8",
        "format": "M823 A%d\n",
        "anchor": "M823 (screen-channel cluster; verify USB dispatch before probing)",
        "kind": "set",
    },
    {
        "code": "M824",
        "func_addr": "080221AC",
        "format": "M824 A%d B%d\n",
        "anchor": "M824 (screen-channel cluster; verify USB dispatch before probing)",
        "kind": "set",
    },
    {
        "code": "M825",
        "func_addr": "08021F8C",
        "format": "M825 A%d B%d C%d\n",
        "anchor": "M825 (screen-channel cluster; verify USB dispatch before probing)",
        "kind": "set",
    },
    {
        "code": "M826",
        "func_addr": "08021FB8",
        "format": "M826 A%d\n",
        "anchor": "M826 (screen-channel cluster; verify USB dispatch before probing)",
        "kind": "set",
    },
    {
        "code": "M827",
        "func_addr": "08022008",
        "format": "M827 A%d\n",
        "anchor": "M827 (screen-channel cluster; verify USB dispatch before probing)",
        "kind": "set",
    },
    {
        "code": "M828",
        "func_addr": "0802202C",
        "format": "M828 A%d\n",
        "anchor": "M828 (screen-channel cluster; verify USB dispatch before probing)",
        "kind": "set",
    },
    {
        "code": "M830",
        "func_addr": "08022508",
        "format": "M830 A%d B%d C%d D%d E%d F%d\n",
        "anchor": "M830 (unreviewed; probe armed only)",
        "kind": "set",
    },
    {
        "code": "M834",
        "func_addr": "08021FE0",
        "format": "M834 A%d B%d\n",
        "anchor": "M834 (screen-channel cluster; verify USB dispatch before probing)",
        "kind": "set",
    },
    {
        "code": "M835",
        "func_addr": "08022938",
        "format": "M835 A%d\n",
        "anchor": "M835 (screen-channel cluster; verify USB dispatch before probing)",
        "kind": "set",
    },
    {
        "code": "M837",
        "func_addr": "08022050",
        "format": "M837 A%d\n",
        "anchor": "M837 (screen-channel cluster; verify USB dispatch before probing)",
        "kind": "set",
    },
    {
        "code": "M838",
        "func_addr": "080229CC",
        "format": "M838 A%d\n",
        "anchor": "M838 (screen-channel cluster; verify USB dispatch before probing)",
        "kind": "set",
    },
    {
        "code": "M841",
        "func_addr": "0801FDA4",
        "format": "M841 A%d\n",
        "anchor": "M841 (unreviewed; probe armed only)",
        "kind": "set",
    },
    {
        "code": "M843",
        "func_addr": "08022AAC",
        "format": "M843 A%d\n",
        "anchor": "M843 (unreviewed; probe armed only)",
        "kind": "set",
    },
    {
        "code": "M846",
        "func_addr": "08022A44",
        "format": "M846 A%d\n",
        "anchor": "M846 (unreviewed; probe armed only)",
        "kind": "set",
    },
    {
        "code": "M851",
        "func_addr": "080202B8",
        "format": "M851 A%d\n",
        "anchor": "M851 (unreviewed; probe armed only)",
        "kind": "set",
    },
    {
        "code": "M852",
        "func_addr": "080202BA",
        "format": "M852 A%d\n",
        "anchor": "M852 (unreviewed; probe armed only)",
        "kind": "set",
    },
    {
        "code": "M868",
        "func_addr": "080208B4",
        "format": "M868 A%d\n",
        "anchor": "M868 (unreviewed; probe armed only)",
        "kind": "set",
    },
    # M870-M898: machine-side forwarders (FUN_080208d4) relaying the raw command
    # line to the laser control board; effect depends on the sub-board handler.
    {
        "code": "M870",
        "func_addr": "080208D4",
        "format": "M870 A%d\n",
        "anchor": "M870 (forwarded to the laser control board via FUN_080208d4)",
        "kind": "set",
    },
    {
        "code": "M871",
        "func_addr": "080208D4",
        "format": "M871 A%.2f\n",
        "anchor": "M871 (forwarded to the laser control board via FUN_080208d4)",
        "kind": "set",
    },
    {
        "code": "M873",
        "func_addr": "080208D4",
        "format": "M873 A%.2f\n",
        "anchor": "M873 (forwarded to the laser control board via FUN_080208d4)",
        "kind": "set",
    },
    {
        "code": "M874",
        "func_addr": "080208D4",
        "format": "M874 A%d\n",
        "anchor": "M874 (forwarded to the laser control board via FUN_080208d4)",
        "kind": "set",
    },
    {
        "code": "M875",
        "func_addr": "080208D4",
        "format": "M875 A%.2f\n",
        "anchor": "M875 (forwarded to the laser control board via FUN_080208d4)",
        "kind": "set",
    },
    {
        "code": "M877",
        "func_addr": "080208D4",
        "format": "M877 A%d\n",
        "anchor": "M877 (forwarded to the laser control board via FUN_080208d4)",
        "kind": "set",
    },
    {
        "code": "M879",
        "func_addr": "080208D4",
        "format": "M879 A%d\n",
        "anchor": "M879 (forwarded to the laser control board via FUN_080208d4)",
        "kind": "set",
    },
    {
        "code": "M881",
        "func_addr": "080208D4",
        "format": "M881 A%.2f\n",
        "anchor": "M881 (forwarded to the laser control board via FUN_080208d4)",
        "kind": "set",
    },
    {
        "code": "M885",
        "func_addr": "080208D4",
        "format": "M885 A%d B%.2f\n",
        "anchor": "M885 (forwarded to the laser control board via FUN_080208d4)",
        "kind": "set",
    },
    {
        "code": "M886",
        "func_addr": "080208D4",
        "format": "M886 A%d B%.2f\n",
        "anchor": "M886 (forwarded to the laser control board via FUN_080208d4)",
        "kind": "set",
    },
    {
        "code": "M887",
        "func_addr": "080208D4",
        "format": "M887 A%d\n",
        "anchor": "M887 (forwarded to the laser control board via FUN_080208d4)",
        "kind": "set",
    },
    {
        "code": "M888",
        "func_addr": "080208D4",
        "format": "M888 A%d\n",
        "anchor": "M888 (forwarded to the laser control board via FUN_080208d4)",
        "kind": "set",
    },
    {
        "code": "M890",
        "func_addr": "080208D4",
        "format": "M890 A%d\n",
        "anchor": "M890 (forwarded to the laser control board via FUN_080208d4)",
        "kind": "set",
    },
    {
        "code": "M891",
        "func_addr": "080208D4",
        "format": "M891 A%d\n",
        "anchor": "M891 (forwarded to the laser control board via FUN_080208d4)",
        "kind": "set",
    },
    {
        "code": "M896",
        "func_addr": "080208D4",
        "format": "M896 A%d\n",
        "anchor": "M896 (forwarded to the laser control board via FUN_080208d4)",
        "kind": "set",
    },
    {
        "code": "M897",
        "func_addr": "080208D4",
        "format": "M897 A%d B%d C%d\n",
        "anchor": "M897 (forwarded to the laser control board via FUN_080208d4)",
        "kind": "set",
    },
    {
        "code": "M898",
        "func_addr": "080208D4",
        "format": "M898 Age counts:%d\n",
        "anchor": "M898 (forwarded to the laser control board via FUN_080208d4)",
        "kind": "set",
    },
]

# Signature return types that carry a handler body.
SIG_RE = re.compile(r"^(?:void|undefined4|undefined8|int|uint)\s+(FUN_[0-9a-f]+)\s*\(([^)]*)\)", re.M)
FMT_RE = re.compile(r"\b(M\d{2,3})\b")
PARAM_RE = re.compile(r"([A-Z])%(?:\.(\d+))?([dfx])")
HEX_TRIPLE_RE = re.compile(r"\{0x%02X, 0x%02X, 0x%02X\}")


def classify(handler_c: Path) -> str:
    """Return 'get' when the handler takes no parameters, else 'set'."""
    text = handler_c.read_text(errors="ignore")
    m = SIG_RE.search(text)
    if not m:
        return "set"
    params = m.group(2).strip()
    return "get" if params in ("void", "") else "set"


def parse_params(formats: list[str]) -> list[dict]:
    """Collect parameters across every format string a handler echoes."""
    params: dict[str, dict] = {}
    for fmt in formats:
        for name, precision, kind in PARAM_RE.findall(fmt):
            if name in params:
                continue
            params[name] = {
                "name": name,
                "type": {"d": "int", "f": "float", "x": "hex"}[kind],
                "precision": int(precision) if precision else None,
                "unit": None,
                "range": None,
                "default": None,
            }
        # Hex-triple forms carry no letter labels; assign A/B/C positionally.
        if HEX_TRIPLE_RE.search(fmt) and not params:
            for name in ("A", "B", "C"):
                params[name] = {
                    "name": name, "type": "hex", "precision": 2,
                    "unit": None, "range": None, "default": None,
                }
    return list(params.values())


def main() -> None:
    rows: dict[str, dict] = {}
    for entry in MANUAL_HANDLERS:
        rows[entry["code"]] = {
            "func_addr": entry["func_addr"],
            "formats": [entry["format"]],
            "anchor": entry.get("anchor"),
            "kind": "get",
        }
    for line in (RE / "handlers.tsv").read_text(errors="ignore").splitlines()[1:]:
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        func_addr, string_addr, text = parts
        found = FMT_RE.search(text)
        if not found:
            continue
        code = found.group(1)
        handler_c = RE / f"handler_{func_addr.upper()}.c"
        if not handler_c.exists():
            continue
        if code in rows:
            if text not in rows[code]["formats"]:
                rows[code]["formats"].append(text)
            continue
        rows[code] = {
            "func_addr": func_addr,
            "formats": [text],
            "anchor": None,
            "kind": classify(handler_c),
        }

    commands = []
    for code in sorted(rows, key=lambda c: int(c[1:])):
        row = rows[code]
        sem = SEMANTICS.get(code, {})
        confirmed = code in CONFIRMED
        commands.append({
            "code": code,
            "summary": sem.get("summary", f"Undocumented; echoes {row['formats'][0]!r}."),
            "board": "machine",
            "cmd_type": sem.get("cmd_type", row["kind"]),
            "params": parse_params(row["formats"]),
            "danger": sem.get("danger", "emission") if confirmed else "emission",
            "side_effects": [],
            "response": {"kind": "line-echo", "format": " / ".join(row["formats"])},
            "evidence": {
                "offset": row["func_addr"],
                "anchor": row["anchor"] or row["formats"][0].rstrip("\n"),
            },
            "confidence": "high" if confirmed else "medium",
            "verified": False,
            "verified_by": None,
        })

    # Fingerprint the exact image used for these decompiled handlers.
    image_digest = sha256(MACHINE_IMAGE.read_bytes()).hexdigest()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({
        "version": 1,
        "source": {
            "board": "machine",
            "image": MACHINE_IMAGE.name,
            "sha256": image_digest,
        },
        "commands": commands,
    }, indent=2) + "\n")
    print(f"wrote {OUT} with {len(commands)} commands")


if __name__ == "__main__":
    main()
