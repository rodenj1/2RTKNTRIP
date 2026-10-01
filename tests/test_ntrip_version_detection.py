"""Tests for NTRIP version detection on client downloads and server uploads.

Seam under test: NTRIPHandler.handle_request(), driven by a fake socket whose
recv() yields one raw request, observing the first line of the reply written
back. Auth, the database, the forwarder and the connection manager are
stubbed so a well-formed request for a known mount is accepted.

Covers issue #23: the version comes only from the method (SOURCE = v1 server,
POST = v2 server) and, for GET, from an ``Ntrip-Version: Ntrip/2.0`` header
matched case-insensitively. Every other GET is v1 and is answered ``ICY 200 OK``,
whatever its HTTP version, User-Agent, headers or path.
"""

from unittest.mock import MagicMock

import pytest
from pytest_mock import MockerFixture

from ntrip_caster.ntrip import NTRIPHandler

RELAY_UA = "NTRIP sp-rtk-base-relay/3.2.0"


def _first_reply_line(mocker: MockerFixture, request: str) -> bytes:
    """Handle one raw request; return the first line the caster sends back."""
    mocker.patch.object(NTRIPHandler, "verify_user", return_value=(True, "ok"))
    mocker.patch.object(NTRIPHandler, "_keep_connection_alive")
    mocker.patch.object(NTRIPHandler, "_receive_rtcm_data")
    conn = mocker.patch("ntrip_caster.ntrip.connection")
    manager = conn.get_connection_manager.return_value
    manager.is_mount_online.return_value = False
    manager.add_mount_connection.return_value = (True, "ok")
    mocker.patch("ntrip_caster.ntrip.forwarder")

    db = MagicMock()
    db.check_mount_exists_in_db.return_value = True

    sock = MagicMock()
    sock.recv.side_effect = [request.encode(), b""]

    NTRIPHandler(sock, ("127.0.0.1", 12345), db).handle_request()

    sent = b"".join(c.args[0] for c in sock.send.call_args_list + sock.sendall.call_args_list)
    assert sent, "caster sent nothing"
    return sent.split(b"\r\n", 1)[0]


def test_http_1_0_get_from_relay_user_agent_without_host_is_v1(mocker: MockerFixture) -> None:
    request = f"GET /MP1 HTTP/1.0\r\nUser-Agent: {RELAY_UA}\r\n\r\n"
    assert _first_reply_line(mocker, request) == b"ICY 200 OK"


def test_post_is_v2_whatever_its_user_agent(mocker: MockerFixture) -> None:
    request = "POST /MP1 HTTP/1.1\r\nHost: caster\r\nUser-Agent: SomeServer/1.0\r\n\r\n"
    assert _first_reply_line(mocker, request) == b"HTTP/1.1 200 OK"


def test_http_1_0_get_from_relay_user_agent_with_host_is_v1(mocker: MockerFixture) -> None:
    request = f"GET /MP1 HTTP/1.0\r\nHost: caster\r\nUser-Agent: {RELAY_UA}\r\n\r\n"
    assert _first_reply_line(mocker, request) == b"ICY 200 OK"


def test_http_1_1_get_without_ntrip_version_is_v1(mocker: MockerFixture) -> None:
    request = f"GET /MP1 HTTP/1.1\r\nHost: caster\r\nUser-Agent: {RELAY_UA}\r\n\r\n"
    assert _first_reply_line(mocker, request) == b"ICY 200 OK"


@pytest.mark.parametrize("version_header", ["Ntrip/2.0", "NTRIP/2.0", "ntrip/2.0"])
def test_get_with_ntrip_version_header_is_v2_in_any_case(mocker: MockerFixture, version_header: str) -> None:
    request = (
        f"GET /MP1 HTTP/1.1\r\nHost: caster\r\nNtrip-Version: {version_header}\r\n"
        "User-Agent: NTRIP SomeClient/1.0\r\n\r\n"
    )
    assert _first_reply_line(mocker, request) == b"HTTP/1.1 200 OK"


def test_source_is_v1(mocker: MockerFixture) -> None:
    request = f"SOURCE secret /MP1\r\nSource-Agent: {RELAY_UA}\r\n\r\n"
    assert _first_reply_line(mocker, request) == b"ICY 200 OK"
