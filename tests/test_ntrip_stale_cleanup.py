"""Tests for the delayed cleanup that runs after a mount's source disconnects.

Seam under test: NTRIPHandler.handle_request(), driving real SOURCE upload
sessions over fake sockets against a real ConnectionManager. Auth, the
forwarder and STR generation are stubbed. The 1.5 s cleanup timer is captured
instead of started, so each test decides exactly when it fires.

Covers issue #26 (D9): when a source reconnects to the same mount from the same
IP within 1.5 s, the old session's delayed cleanup must not tear down the new
session. A source that disconnects and does not come back is still cleaned up.
"""

from collections.abc import Callable
from typing import Any
from unittest.mock import MagicMock

import pytest
from pytest_mock import MockerFixture

from ntrip_caster.connection import ConnectionManager
from ntrip_caster.ntrip import NTRIPHandler

SOURCE_MP1 = b"SOURCE secret /MP1\r\nSource-Agent: NTRIP Test/1.0\r\n\r\n"
# A representative RTCM3 1005 frame (same fixed literal as test_ntrip_ingest.py).
RTCM_1005 = bytes.fromhex("d3000c3ed7d30203ff2e0000000000000000")


@pytest.fixture
def forwarder(mocker: MockerFixture) -> MagicMock:
    """The forwarder the handler uploads to, as a mock."""
    return mocker.patch("ntrip_caster.ntrip.forwarder")


@pytest.fixture
def manager(mocker: MockerFixture, forwarder: MagicMock) -> ConnectionManager:
    """A fresh, real ConnectionManager wired in for the handler.

    Also stubs: authentication (every SOURCE is accepted), the forwarder (see the
    ``forwarder`` fixture) and STR generation/correction (no background threads).
    """
    real = ConnectionManager()
    mocker.patch.object(real, "_generate_initial_str")
    mocker.patch.object(real, "start_str_correction")
    mocker.patch("ntrip_caster.ntrip.connection.get_connection_manager", return_value=real)
    mocker.patch.object(NTRIPHandler, "verify_user", return_value=(True, "ok"))
    return real


@pytest.fixture
def cleanup_timers(mocker: MockerFixture) -> list[Callable[[], None]]:
    """Every delayed cleanup scheduled, in order, captured instead of started."""
    captured: list[Callable[[], None]] = []

    def _timer(_delay: float, fn: Callable[[], None]) -> MagicMock:
        captured.append(fn)
        return MagicMock()

    mocker.patch("ntrip_caster.ntrip.threading.Timer", side_effect=_timer)
    return captured


def _upload_session(recv: list[Any], sock: MagicMock | None = None) -> MagicMock:
    """Run one SOURCE upload session to completion; return its socket.

    ``recv`` lists what successive recv() calls return after the request. A
    callable is called and its result returned, so a test can act mid-stream.
    """
    sock = sock if sock is not None else MagicMock()
    queue = [SOURCE_MP1, *recv]

    def _recv(*_args: object) -> bytes:
        item = queue.pop(0)
        return item() if callable(item) else item

    sock.recv.side_effect = _recv
    NTRIPHandler(sock, ("127.0.0.1", 40000), MagicMock()).handle_request()
    return sock


def test_quick_same_ip_reconnect_survives_the_old_sessions_cleanup(
    manager: ConnectionManager, cleanup_timers: list[Callable[[], None]], forwarder: MagicMock
) -> None:
    # Session A streams, then its connection drops: its cleanup is scheduled.
    _upload_session([RTCM_1005, b""])
    assert len(cleanup_timers) == 1
    old_cleanup = cleanup_timers[0]

    # Session B reconnects within the window. While B is streaming, A's timer fires.
    b_sock = MagicMock()
    seen: dict[str, bool] = {}

    def _fire_old_cleanup_then_observe() -> bytes:
        old_cleanup()
        seen["online"] = manager.is_mount_online("MP1")
        seen["b_closed"] = b_sock.close.called
        seen["buffer_removed"] = forwarder.remove_mount_buffer.called
        return RTCM_1005

    _upload_session([RTCM_1005, _fire_old_cleanup_then_observe, b""], sock=b_sock)

    assert seen["online"], "the old session's cleanup took the reconnected mount offline"
    assert not seen["b_closed"], "the old session's cleanup closed the reconnected source's socket"
    assert not seen["buffer_removed"], "the old session's cleanup dropped the reconnected source's buffer"


def test_source_that_does_not_come_back_is_still_cleaned_up(
    manager: ConnectionManager, cleanup_timers: list[Callable[[], None]], forwarder: MagicMock
) -> None:
    _upload_session([RTCM_1005, b""])
    assert manager.is_mount_online("MP1"), "the mount stays listed until its delayed cleanup runs"

    cleanup_timers[0]()

    assert not manager.is_mount_online("MP1")
    forwarder.remove_mount_buffer.assert_called_once_with("MP1")


def test_cleanup_still_removes_the_buffer_when_the_mount_was_already_dropped(
    manager: ConnectionManager, cleanup_timers: list[Callable[[], None]], forwarder: MagicMock
) -> None:
    _upload_session([RTCM_1005, b""])
    # Something else (zombie cleanup, an admin delete) drops the mount before the timer fires.
    manager.remove_mount_connection("MP1", "Zombie connection cleanup")

    cleanup_timers[0]()

    forwarder.remove_mount_buffer.assert_called_once_with("MP1")
