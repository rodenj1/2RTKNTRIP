"""Tests for how quickly a source can reconnect to its mount.

Seam under test: ConnectionManager.add_mount_connection(), the call every upload
makes to register its mount, with a real RTCM2ParserManager starting and stopping
real STR-correction parser threads (only their background correction wait is
captured instead of started). A second thread stands in for another client
using the connection manager meanwhile.

Covers issue #49: registering a source for a mount whose previous session's STR
parser is still running stopped that parser and waited ~5 s for it, while holding
the mount lock, so the reconnect took ~5 s and every other client's mount check
stalled with it.
"""

import threading
import time
import types
from collections.abc import Iterator
from unittest.mock import MagicMock

import pytest
from pytest_mock import MockerFixture

from ntrip_caster.connection import ConnectionManager
from ntrip_caster.rtcm2_manager import RTCM2ParserManager

FAST_S = 1.0  # a first registration takes a few milliseconds


@pytest.fixture
def manager(mocker: MockerFixture) -> Iterator[ConnectionManager]:
    """A real ConnectionManager whose STR parsers are real threads, managed by a fresh parser manager."""
    parsers = RTCM2ParserManager()
    mocker.patch("ntrip_caster.connection.rtcm_manager", parsers)
    # The correction's background wait (parse_duration + 5 s) isn't under test. Swap only
    # this module's view of threading: patching threading.Thread would hit every thread.
    mocker.patch("ntrip_caster.connection.threading", types.SimpleNamespace(Thread=MagicMock()))
    real = ConnectionManager()
    yield real
    for mount in list(parsers.parsers):
        parsers.stop_parser(mount)


def _register(manager: ConnectionManager) -> None:
    ok, _ = manager.add_mount_connection("MP1", "10.0.0.1", client_socket=MagicMock())
    assert ok


def test_reconnect_is_fast_and_does_not_stall_other_clients(manager: ConnectionManager) -> None:
    _register(manager)  # the first session: its STR parser is now running

    other_client_wait: list[float] = []

    def _other_client() -> None:
        start = time.monotonic()
        manager.check_mount_live("OTHER", 30)
        other_client_wait.append(time.monotonic() - start)

    start = time.monotonic()
    reconnect = threading.Thread(target=_register, args=(manager,))
    reconnect.start()
    time.sleep(0.1)  # the reconnect is now under way
    other = threading.Thread(target=_other_client)
    other.start()
    reconnect.join(15)
    other.join(15)
    reconnect_s = time.monotonic() - start

    other_s = other_client_wait[0] if other_client_wait else float("inf")
    timings = f"reconnect took {reconnect_s:.2f}s; another client's mount check waited {other_s:.2f}s"
    assert reconnect_s < FAST_S and other_s < FAST_S, timings


def test_a_slow_parser_stop_never_holds_up_other_clients(manager: ConnectionManager, mocker: MockerFixture) -> None:
    # Whatever stopping the old parser costs, it must not happen under the mount lock.
    from ntrip_caster.rtcm2 import RTCMParserThread

    real_stop = RTCMParserThread.stop

    def _slow_stop(self: RTCMParserThread) -> None:
        time.sleep(1.5)
        real_stop(self)

    mocker.patch.object(RTCMParserThread, "stop", _slow_stop)
    _register(manager)

    other_client_wait: list[float] = []

    def _other_client() -> None:
        start = time.monotonic()
        manager.check_mount_live("OTHER", 30)
        other_client_wait.append(time.monotonic() - start)

    reconnect = threading.Thread(target=_register, args=(manager,))
    reconnect.start()
    time.sleep(0.2)  # the reconnect is now stopping the old parser
    other = threading.Thread(target=_other_client)
    other.start()
    reconnect.join(15)
    other.join(15)

    assert other_client_wait and other_client_wait[0] < 0.5, f"another client waited {other_client_wait}s"
