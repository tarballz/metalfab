"""The M351 system-log parser, driven by the firmware's literal format string."""

import pytest

from metalfab.logs import LogRecord, parse_job_index, parse_system_log


FULL_LOG = (
    "\n=== System Log Records (Total: 2) ===\n"
    "#001 [00:01:02.345] [ERROR] [LASER] Param:0x0000000A - Laser Fault Normal\n"
    "#002 [00:02:03.004] [SYSTEM] [BOARD] Param:0x00000000 - Board Key Normal\n"
    "=== End of Log Records ===\n"
)

FILTERED_LOG = (
    "\n=== System Log Records (Type: ERROR, Count: 1) ===\n"
    "#001 [00:01:02.345] [ERROR] [LASER] Param:0x0000000A - Laser Fault Normal\n"
    "=== End of Log Records ===\n"
)


def test_parses_full_log():
    result = parse_system_log(FULL_LOG)
    assert result.kind == "all"
    assert result.total == 2
    assert len(result.records) == 2
    first: LogRecord = result.records[0]
    assert first.index == 1
    assert (first.hours, first.minutes, first.seconds, first.millis) == (0, 1, 2, 345)
    assert first.level == "ERROR"
    assert first.class_name == "LASER"
    assert first.param == 0xA
    assert first.message == "Laser Fault Normal"


def test_parses_filtered_log():
    result = parse_system_log(FILTERED_LOG)
    assert result.kind == "filtered"
    assert result.type_name == "ERROR"
    assert result.total == 1
    assert len(result.records) == 1


def test_parses_empty_forms():
    assert parse_system_log("No log records").records == []
    assert parse_system_log("No log records for type: ERROR").records == []


def test_ignores_unknown_lines_but_keeps_records():
    result = parse_system_log("some async line\n" + FULL_LOG + "trailing noise\n")
    assert len(result.records) == 2


def test_rejects_impossible_records_loudly():
    with pytest.raises(ValueError):
        parse_system_log(FULL_LOG.replace("Param:0x0000000A", "Param:0xZZZZZZZZ"))


JOB_INDEX = (
    "Index file content: {'continus': ['job1.jc', 'job2.jc'], 'pulse': []}\n"
    "Folder 'continus' for mode 0 contains 2 files.\n"
    "No files found in machining mode 'pulse'.\n"
)


def test_parse_job_index_keeps_raw_lines():
    """The index format is unknown until a live run; parse keeps the raw lines."""
    result = parse_job_index(JOB_INDEX)
    assert result["truncated"] is False
    assert len(result["lines"]) == 3
    assert result["lines"][0].startswith("Index file content:")
