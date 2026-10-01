"""Tests for uploads to a mount whose current source may have gone quiet.

Seam under test: NTRIPHandler.handle_request(), driving real v1 SOURCE uploads
over fake sockets against a real ConnectionManager. Auth, the forwarder, STR
generation and the cleanup timer are stubbed, and the upload's ingest loop
returns at once. A mount is made quiet by moving its last data (and
connection) time back past ``ntrip.mount_data_timeout``.

Covers issue #41: before accepting an upload, the caster evicts the target mount
if its source has sent no data for longer than ``ntrip.mount_data_timeout``,
the same rule downloads use (#32), with no subprocess and no IP-address check.
A live source is never evicted.
"""

from unittest.mock import MagicMock

import pytest
from pytest_mock import MockerFixture

from ntrip_caster import config
from ntrip_caster.connection import ConnectionManager
from ntrip_caster.ntrip import NTRIPHandler

MOUNT_DATA_TIMEOUT_S = config.settings.ntrip.mount_data_timeout  # the 30 s default
SOURCE_MP1 = b"SOURCE secret /MP1\r\nSource-Agent: NTRIP Test/1.0\r\n\r\n"


@pytest.fixture
def manager(mocker: MockerFixture) -> ConnectionManager:
    """A fresh, real ConnectionManager wired in for the caster.

    Also stubs: authentication (every SOURCE is accepted), the forwarder, STR
    generation/correction, the delayed cleanup timer, and the ingest loop.
    """
    real = ConnectionManager()
    mocker.patch.object(real, "_generate_initial_str")
    mocker.patch.object(real, "start_str_correction")
    mocker.patch("ntrip_caster.connection.get_connection_manager", return_value=real)
    mocker.patch("ntrip_caster.ntrip.forwarder")
    mocker.patch("ntrip_caster.ntrip.threading.Timer")
    mocker.patch.object(NTRIPHandler, "verify_user", return_value=(True, "ok"))
    mocker.patch.object(NTRIPHandler, "_receive_rtcm_data")
    return real


def _source_online(manager: ConnectionManager, mount: str, ip: str) -> MagicMock:
    """Register ``mount`` with a source at ``ip`` that has just sent data; return its socket."""
    source_socket = MagicMock()
    manager.add_mount_connection(mount, ip, client_socket=source_socket)
    manager.update_mount_data_stats(mount, 18)
    return source_socket


def _go_quiet(manager: ConnectionManager, mount: str, seconds: float) -> None:
    """Make ``mount``'s source look silent for ``seconds``, as if it stopped without disconnecting."""
    info = manager.online_mounts[mount]
    info.connect_time -= seconds
    if info.last_data_time is not None:
        info.last_data_time -= seconds


def _upload(request: bytes, ip: str) -> bytes:
    """Run one authenticated upload from ``ip``; return the first line the caster sends back."""
    sock = MagicMock()
    sock.recv.side_effect = [request, b""]
    NTRIPHandler(sock, (ip, 40000), MagicMock()).handle_request()
    sent = b"".join(c.args[0] for c in sock.send.call_args_list + sock.sendall.call_args_list)
    return sent.split(b"\r\n", 1)[0]


def test_upload_takes_over_a_mount_whose_source_has_gone_quiet(manager: ConnectionManager) -> None:
    old_socket = _source_online(manager, "MP1", "10.0.0.1")
    _go_quiet(manager, "MP1", MOUNT_DATA_TIMEOUT_S + 5)

    assert _upload(SOURCE_MP1, "10.0.0.2") == b"ICY 200 OK"
    assert old_socket.close.called, "the quiet source should be evicted"
    info = manager.get_mount_info("MP1")
    assert info is not None and info["ip_address"] == "10.0.0.2"


def test_upload_to_a_mount_with_a_live_source_is_still_refused(manager: ConnectionManager) -> None:
    holder_socket = _source_online(manager, "MP1", "10.0.0.1")

    assert _upload(SOURCE_MP1, "10.0.0.2") == b"ERROR - Mount Point Taken or Invalid"
    assert not holder_socket.close.called, "a live source must never be evicted"
    info = manager.get_mount_info("MP1")
    assert info is not None and info["ip_address"] == "10.0.0.1"


def test_upload_runs_no_subprocess_and_leaves_other_live_mounts_alone(
    manager: ConnectionManager, mocker: MockerFixture
) -> None:
    run = mocker.patch("subprocess.run")
    # The old netstat check evicted any mount whose source IP had no local TCP
    # connection (e.g. a base seen through PROXY-v2). MP2's IP has none here.
    other_socket = _source_online(manager, "MP2", "192.0.2.50")

    assert _upload(SOURCE_MP1, "10.0.0.2") == b"ICY 200 OK"
    run.assert_not_called()
    assert manager.is_mount_online("MP2")
    assert not other_socket.close.called


def test_unauthenticated_upload_from_the_same_ip_does_not_evict_a_live_source(
    manager: ConnectionManager, mocker: MockerFixture
) -> None:
    # Several bases can share one public IP behind NAT or a proxy: sharing it must
    # not let a request that fails authentication take over a live mount.
    holder_socket = _source_online(manager, "MP1", "10.0.0.1")
    mocker.patch.object(NTRIPHandler, "verify_user", return_value=(False, "Invalid credentials"))

    assert _upload(SOURCE_MP1, "10.0.0.1") == b"ERROR - Bad Password"
    assert manager.is_mount_online("MP1")
    assert not holder_socket.close.called, "a failed login must not evict the live source"
