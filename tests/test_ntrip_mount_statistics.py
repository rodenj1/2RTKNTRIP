"""Tests for a mount's data statistics while its source uploads.

Seam under test: NTRIPHandler.handle_request(), driving a real v1 SOURCE upload
over a fake socket, through a real forwarder instance, against a real
ConnectionManager. Auth, STR generation and the delayed cleanup timer are
stubbed, so the mount stays online to be inspected after the upload ends.

Covers issue #42: each uploaded chunk updates the mount's statistics exactly
once, so total bytes, message count and data rate match what the source sent,
and the last-data time still advances on every chunk.
"""

import time
from collections.abc import Callable
from unittest.mock import MagicMock

import pytest
from pytest_mock import MockerFixture

from ntrip_caster.connection import ConnectionManager
from ntrip_caster.forwarder import SimpleDataForwarder
from ntrip_caster.ntrip import NTRIPHandler

SOURCE_MP1 = b"SOURCE secret /MP1\r\nSource-Agent: NTRIP Test/1.0\r\n\r\n"
# A representative RTCM3 1005 frame (same fixed literal as test_ntrip_ingest.py): 18 bytes.
RTCM_1005 = bytes.fromhex("d3000c3ed7d30203ff2e0000000000000000")


@pytest.fixture
def manager(mocker: MockerFixture) -> ConnectionManager:
    """A fresh, real ConnectionManager and a real (not started) forwarder, wired in for the caster.

    Also stubs: authentication (every SOURCE is accepted), STR generation/correction,
    and the delayed cleanup timer (so the mount stays online after the upload).
    """
    real = ConnectionManager()
    mocker.patch.object(real, "_generate_initial_str")
    mocker.patch.object(real, "start_str_correction")
    mocker.patch("ntrip_caster.connection.get_connection_manager", return_value=real)
    mocker.patch("ntrip_caster.forwarder.forwarder", SimpleDataForwarder())
    mocker.patch("ntrip_caster.ntrip.threading.Timer")
    mocker.patch.object(NTRIPHandler, "verify_user", return_value=(True, "ok"))
    return real


def _upload(chunks: list[bytes | Callable[[], bytes]]) -> None:
    """Run one upload; a callable in ``chunks`` is called (mid-upload) and its result sent."""
    queue: list[bytes | Callable[[], bytes]] = [SOURCE_MP1, *chunks, b""]

    def _recv(*_args: object) -> bytes:
        item = queue.pop(0)
        return item() if callable(item) else item

    sock = MagicMock()
    sock.recv.side_effect = _recv
    NTRIPHandler(sock, ("10.0.0.1", 40000), MagicMock()).handle_request()


def test_each_uploaded_chunk_is_counted_once(manager: ConnectionManager) -> None:
    _upload([RTCM_1005, RTCM_1005, RTCM_1005])

    stats = manager.get_mount_info("MP1")
    assert stats is not None
    assert stats["total_bytes"] == 3 * len(RTCM_1005) == 54
    assert stats["data_count"] == 3


def test_last_data_time_advances_with_every_chunk(manager: ConnectionManager) -> None:
    after_first: dict[str, float] = {}

    def _second_chunk() -> bytes:
        # Runs once the first chunk has been ingested: note its time, then send another.
        info = manager.get_mount_info("MP1")
        assert info is not None and info["last_data_time"] is not None
        after_first["t"] = info["last_data_time"]
        time.sleep(0.01)
        return RTCM_1005

    _upload([RTCM_1005, _second_chunk])

    stats = manager.get_mount_info("MP1")
    assert stats is not None
    assert stats["last_data_time"] > after_first["t"], "the second chunk must advance the last-data time"
