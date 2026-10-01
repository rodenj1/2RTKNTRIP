"""Tests for how the caster answers a request for a mount it can't serve.

Seam under test: NTRIPHandler.handle_request(), driven by a fake socket whose
recv() yields one raw request, observing the reply written back.
Collaborators are stubbed by the ``ntrip_reply`` fixture (conftest).

Covers issue #25: a mount that is unknown (D7) or has no source online (D10)
is answered before authentication, with the sourcetable for v1 and 404 for v2.
An online mount with bad credentials still gets 401. v1 client errors use
HTTP/1.0 status lines, and v1 servers (SOURCE) get the NTRIP v1 server lines
``ERROR - Bad Password`` / ``ERROR - Mount Point Taken or Invalid``, rather than
``SOURCETABLE 401`` or ``ERROR <code>`` (D8).
"""

from collections.abc import Callable

import pytest

V1_GET = "GET {path} HTTP/1.0\r\nUser-Agent: NTRIP Test/1.0\r\n\r\n"
V2_GET = "GET {path} HTTP/1.1\r\nHost: caster\r\nNtrip-Version: Ntrip/2.0\r\nUser-Agent: NTRIP Test/1.0\r\n\r\n"


def _first_line(reply: bytes) -> bytes:
    return reply.split(b"\r\n", 1)[0]


def test_v2_unknown_mount_is_404_not_401(ntrip_reply: Callable[..., bytes]) -> None:
    reply = ntrip_reply(V2_GET.format(path="/NOPE"), mount_exists=False, mount_online=False, authorized=False)
    assert _first_line(reply) == b"HTTP/1.1 404 Not Found"


def test_v1_unknown_mount_gets_the_sourcetable(ntrip_reply: Callable[..., bytes]) -> None:
    reply = ntrip_reply(V1_GET.format(path="/NOPE"), mount_exists=False, mount_online=False, authorized=False)
    assert _first_line(reply) == b"SOURCETABLE 200 OK"
    assert reply.rstrip().endswith(b"ENDSOURCETABLE")


def test_v1_client_error_uses_an_http_1_0_status_line(ntrip_reply: Callable[..., bytes]) -> None:
    reply = ntrip_reply("\r\n\r\n")  # blank request line: rejected before any version is known, so v1
    assert _first_line(reply) == b"HTTP/1.0 400 Bad Request"


@pytest.mark.parametrize(
    ("request_template", "mount_exists", "mount_online", "authorized", "expected_first_line"),
    [
        # Known mount, no source online: same reply as an unknown one, whatever the credentials.
        (V1_GET, True, False, True, b"SOURCETABLE 200 OK"),
        (V2_GET, True, False, True, b"HTTP/1.1 404 Not Found"),
        # Source still online for a mount deleted from the database: unavailable too.
        (V1_GET, False, True, True, b"SOURCETABLE 200 OK"),
        (V2_GET, False, True, True, b"HTTP/1.1 404 Not Found"),
        # Online mount, bad credentials: still a credentials failure.
        (V1_GET, True, True, False, b"HTTP/1.0 401 Unauthorized"),
        (V2_GET, True, True, False, b"HTTP/1.1 401 Unauthorized"),
        # Online mount, good credentials: the stream.
        (V1_GET, True, True, True, b"ICY 200 OK"),
        (V2_GET, True, True, True, b"HTTP/1.1 200 OK"),
    ],
    ids=[
        "v1-offline",
        "v2-offline",
        "v1-deleted",
        "v2-deleted",
        "v1-bad-credentials",
        "v2-bad-credentials",
        "v1-stream",
        "v2-stream",
    ],
)
def test_reply_by_mount_state_and_credentials(
    ntrip_reply: Callable[..., bytes],
    request_template: str,
    mount_exists: bool,
    mount_online: bool,
    authorized: bool,
    expected_first_line: bytes,
) -> None:
    reply = ntrip_reply(
        request_template.format(path="/MP1"),
        mount_exists=mount_exists,
        mount_online=mount_online,
        authorized=authorized,
    )
    assert _first_line(reply) == expected_first_line


V1_SOURCE = "SOURCE secret /{mount}\r\nSource-Agent: NTRIP Test/1.0\r\n\r\n"


def test_v1_server_bad_password_gets_error_bad_password(ntrip_reply: Callable[..., bytes]) -> None:
    reply = ntrip_reply(V1_SOURCE.format(mount="MP1"), authorized=False)
    assert _first_line(reply) == b"ERROR - Bad Password"


def test_v1_server_on_occupied_mount_gets_error_mount_point_taken(ntrip_reply: Callable[..., bytes]) -> None:
    reply = ntrip_reply(V1_SOURCE.format(mount="MP1"), occupied_by="10.0.0.9")
    assert _first_line(reply) == b"ERROR - Mount Point Taken or Invalid"


def test_v1_server_without_a_mountpoint_gets_error_mount_point_invalid(ntrip_reply: Callable[..., bytes]) -> None:
    reply = ntrip_reply(V1_SOURCE.format(mount=""))
    assert _first_line(reply) == b"ERROR - Mount Point Taken or Invalid"
