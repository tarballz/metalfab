"""The versions timeout names the known laser-board silent-window phenomenon."""

from pathlib import Path

import pytest

from metalfab.capture import Capture
from metalfab.contracts import load_evidence
from metalfab.device import DeviceError, Session, select_profile

REPO = Path(__file__).resolve().parent.parent
MACHINE_VERSION = "40.71.003.2425.01.B04"

# Every board except the laser control board answers.
PARTIAL_VERSIONS = (
    b"M99 A40.71.003.2425.01.B04 B40.71.001.2425.00.B01\n",
    b"M99 A40.71.003.2540.01.B03\n",
    b"M99 A40.70.005.2722.01.B02 B40.70.001.2722.00.B01\n",
)


class _FakeSerial:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.queued = []
        self.writes = []
        self.closed = False

    @property
    def in_waiting(self):
        return len(self.queued[0]) if self.queued and isinstance(self.queued[0], bytes) else 0

    def read(self, size: int) -> bytes:
        del size  # a chunked fake ignores the requested read size
        if not self.queued:
            return b""
        return self.queued.pop(0)

    def write(self, data):
        self.writes.append(data)
        self.queued.append(next(self.responses, b""))
        return len(data)

    def close(self):
        self.closed = True


def test_timeout_hint_mentions_the_silent_window(tmp_path):
    evidence = load_evidence(REPO / "data")
    assert not evidence.problems
    profile = select_profile(evidence, MACHINE_VERSION)
    capture = Capture(tmp_path / "recording.jsonl", port="/dev/fake", baud=230400, profile=None)
    session = Session(_FakeSerial(PARTIAL_VERSIONS), capture, evidence, profile)
    with pytest.raises(DeviceError) as caught:
        session.installed_versions()
    message = str(caught.value)
    assert "laser_control" in message
    assert "heartbeat" in message
    assert "retry" in message
