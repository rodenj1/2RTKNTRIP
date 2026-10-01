"""Tests for what the RTCM parser logs when a source sends invalid frames.

Seam under test: a real RTCMParserThread (STR-correction mode) subscribed to a
real forwarder instance, fed frames through SimpleDataForwarder.upload_data()
exactly as a base station's upload feeds it. Log records are captured.

Covers issue #50: a stream of frames that fail their CRC must not produce an
ERROR log line per frame.
"""

import logging
import socket
import time
from collections.abc import Iterator

import pytest
from pytest_mock import MockerFixture

from ntrip_caster.forwarder import SimpleDataForwarder
from ntrip_caster.rtcm2 import RTCMParserThread, start_str_fix_parser

# An RTCM3 frame whose 3-byte CRC is wrong (zeros): 0xD3 preamble, length 12, payload, CRC.
BAD_CRC_FRAME = bytes.fromhex("d3000c3ed7d30203ff2e0000000000000000")
# A valid RTCM3 1005 frame (all fields zero), CRC computed with pyrtcm's calc_crc24q.
GOOD_1005_FRAME = bytes.fromhex("d300133ed00000000000000000000000000000000000f24bf4")


@pytest.fixture
def forwarder(mocker: MockerFixture) -> SimpleDataForwarder:
    real = SimpleDataForwarder()
    mocker.patch("ntrip_caster.forwarder.forwarder", real)
    mocker.patch("ntrip_caster.forwarder.connection")  # mount statistics aren't under test
    return real


@pytest.fixture
def parser(forwarder: SimpleDataForwarder) -> Iterator[RTCMParserThread]:
    thread = start_str_fix_parser("MP1", duration=30)
    deadline = time.monotonic() + 2
    while not forwarder.subscribers.get("MP1") and time.monotonic() < deadline:
        time.sleep(0.01)
    yield thread
    thread.stop()


def _feed(forwarder: SimpleDataForwarder, frames: list[bytes]) -> None:
    for frame in frames:
        forwarder.upload_data("MP1", frame)
    time.sleep(0.5)  # let the parser read them


def test_a_stream_of_bad_crc_frames_does_not_flood_the_error_log(
    forwarder: SimpleDataForwarder, parser: RTCMParserThread, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    _feed(forwarder, [BAD_CRC_FRAME] * 200)

    crc_errors = [r for r in caplog.records if r.levelno >= logging.ERROR and "CRC" in r.getMessage()]
    assert len(crc_errors) <= 1, f"{len(crc_errors)} ERROR lines for 200 bad frames"


def test_bad_frames_are_reported_once_as_a_warning(
    forwarder: SimpleDataForwarder, parser: RTCMParserThread, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    _feed(forwarder, [BAD_CRC_FRAME] * 200)

    reports = [r for r in caplog.records if "Invalid RTCM frames" in r.getMessage()]
    assert len(reports) == 1
    assert reports[0].levelno == logging.WARNING


def test_valid_frames_among_bad_ones_are_still_parsed(forwarder: SimpleDataForwarder, parser: RTCMParserThread) -> None:
    _feed(forwarder, [BAD_CRC_FRAME, GOOD_1005_FRAME] * 20)
    # The parser's buffered read can hold a burst until more data arrives, as it does on a
    # live stream: keep the stream going until the burst has been parsed.
    sent_after = 0
    deadline = time.monotonic() + 3
    while parser.result["message_stats"]["types"][1005] < 20 + sent_after and time.monotonic() < deadline:
        forwarder.upload_data("MP1", GOOD_1005_FRAME)
        sent_after += 1
        time.sleep(0.05)

    assert parser.result["message_stats"]["types"][1005] >= 20


def test_bad_frames_are_still_relayed_unchanged(forwarder: SimpleDataForwarder, parser: RTCMParserThread) -> None:
    rover_r, rover_w = socket.socketpair()
    forwarder.register_subscriber("MP1", rover_w)
    stream = [BAD_CRC_FRAME, GOOD_1005_FRAME] * 5
    _feed(forwarder, stream)

    rover_r.settimeout(1)
    received = b""
    while len(received) < len(b"".join(stream)):
        received += rover_r.recv(4096)
    assert received == b"".join(stream)
    rover_r.close()
    rover_w.close()
