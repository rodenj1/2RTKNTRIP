"""Tests for downloads from a mount whose source has silently gone away.

Seam under test: NTRIPHandler.handle_request(), driving real GET downloads over
fake sockets against a real ConnectionManager holding a real mount. Auth, the
database and the forwarder are stubbed, and the download's keep-alive returns
at once. A mount is made quiet by moving its last data (and connection) time
back past ``ntrip.mount_data_timeout``, so no test depends on real sleeps.

Covers issue #32: a mount that has sent no data for longer than
``ntrip.mount_data_timeout`` is unavailable. The download gets the unavailable
reply (v1 sourcetable, v2 404), and the stale mount is removed with its socket
closed. A mount with a live source still streams.
"""

from unittest.mock import MagicMock

import pytest
from pytest_mock import MockerFixture

from ntrip_caster import config
from ntrip_caster.connection import ConnectionManager
from ntrip_caster.ntrip import NTRIPHandler

MOUNT_DATA_TIMEOUT_S = config.settings.ntrip.mount_data_timeout  # the 30 s default
V1_GET_MP1 = b"GET /MP1 HTTP/1.0\r\nUser-Agent: NTRIP Test/1.0\r\n\r\n"
V2_GET_MP1 = b"GET /MP1 HTTP/1.1\r\nHost: caster\r\nNtrip-Version: Ntrip/2.0\r\nUser-Agent: NTRIP Test/1.0\r\n\r\n"


@pytest.fixture
def manager(mocker: MockerFixture) -> ConnectionManager:
    """A fresh, real ConnectionManager wired in for the caster.

    Also stubs: authentication (every user is accepted), the forwarder, STR
    generation/correction, and the download keep-alive (returns at once).
    """
    real = ConnectionManager()
    mocker.patch.object(real, "_generate_initial_str")
    mocker.patch.object(real, "start_str_correction")
    mocker.patch("ntrip_caster.connection.get_connection_manager", return_value=real)
    mocker.patch("ntrip_caster.ntrip.forwarder")
    mocker.patch.object(NTRIPHandler, "verify_user", return_value=(True, "ok"))
    mocker.patch.object(NTRIPHandler, "_keep_connection_alive")
    return real


def _source_online(manager: ConnectionManager) -> MagicMock:
    """Register MP1 with a source that has just sent data; return the source's socket."""
    source_socket = MagicMock()
    manager.add_mount_connection("MP1", "10.0.0.1", client_socket=source_socket)
    manager.update_mount_data_stats("MP1", 18)
    return source_socket


def _go_quiet(manager: ConnectionManager, seconds: float) -> None:
    """Make MP1's source look silent for ``seconds``, as if it stopped sending without disconnecting."""
    mount = manager.online_mounts["MP1"]
    mount.connect_time -= seconds
    if mount.last_data_time is not None:
        mount.last_data_time -= seconds


def _status_line(request: bytes) -> bytes:
    """Run one download request; return the first line the caster sends back."""
    db = MagicMock()
    db.check_mount_exists_in_db.return_value = True
    sock = MagicMock()
    sock.recv.side_effect = [request, b""]
    NTRIPHandler(sock, ("127.0.0.1", 50000), db).handle_request()
    sent = b"".join(c.args[0] for c in sock.send.call_args_list + sock.sendall.call_args_list)
    return sent.split(b"\r\n", 1)[0]


def test_download_from_a_mount_gone_quiet_gets_the_sourcetable(manager: ConnectionManager) -> None:
    source_socket = _source_online(manager)
    _go_quiet(manager, MOUNT_DATA_TIMEOUT_S + 5)

    assert _status_line(V1_GET_MP1) == b"SOURCETABLE 200 OK"
    assert not manager.is_mount_online("MP1"), "the stale mount should be removed"
    assert source_socket.close.called, "the stale source's socket should be closed"


def test_download_from_a_mount_gone_quiet_is_v2_404(manager: ConnectionManager) -> None:
    _source_online(manager)
    _go_quiet(manager, MOUNT_DATA_TIMEOUT_S + 5)

    assert _status_line(V2_GET_MP1) == b"HTTP/1.1 404 Not Found"
    assert not manager.is_mount_online("MP1")


def test_download_from_a_live_mount_streams(manager: ConnectionManager) -> None:
    source_socket = _source_online(manager)

    assert _status_line(V1_GET_MP1) == b"ICY 200 OK"
    assert manager.is_mount_online("MP1")
    assert not source_socket.close.called


def test_a_source_that_just_connected_gets_its_grace_period(manager: ConnectionManager) -> None:
    # Connected but no data yet: idle time counts from the connection, not from never.
    manager.add_mount_connection("MP1", "10.0.0.1", client_socket=MagicMock())

    assert _status_line(V1_GET_MP1) == b"ICY 200 OK"
    assert manager.is_mount_online("MP1")
