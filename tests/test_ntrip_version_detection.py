"""Tests for NTRIP version detection on client downloads and server uploads.

Seam under test: NTRIPHandler.handle_request(), driven by a fake socket whose
recv() yields one raw request, observing the first line of the reply written
back. Collaborators are stubbed by the ``ntrip_reply`` fixture (conftest).

Covers issue #23: the version comes only from the method (SOURCE = v1 server,
POST = v2 server) and, for GET, from an ``Ntrip-Version: Ntrip/2.0`` header
matched case-insensitively. Every other GET is v1 and is answered ``ICY 200 OK``,
whatever its HTTP version, User-Agent, headers or path.
"""

from collections.abc import Callable

import pytest

RELAY_UA = "NTRIP sp-rtk-base-relay/3.2.0"


def _first_line(reply: bytes) -> bytes:
    return reply.split(b"\r\n", 1)[0]


def test_http_1_0_get_from_relay_user_agent_without_host_is_v1(ntrip_reply: Callable[..., bytes]) -> None:
    request = f"GET /MP1 HTTP/1.0\r\nUser-Agent: {RELAY_UA}\r\n\r\n"
    assert _first_line(ntrip_reply(request)) == b"ICY 200 OK"


def test_post_is_v2_whatever_its_user_agent(ntrip_reply: Callable[..., bytes]) -> None:
    request = "POST /MP1 HTTP/1.1\r\nHost: caster\r\nUser-Agent: SomeServer/1.0\r\n\r\n"
    assert _first_line(ntrip_reply(request)) == b"HTTP/1.1 200 OK"


def test_http_1_0_get_from_relay_user_agent_with_host_is_v1(ntrip_reply: Callable[..., bytes]) -> None:
    request = f"GET /MP1 HTTP/1.0\r\nHost: caster\r\nUser-Agent: {RELAY_UA}\r\n\r\n"
    assert _first_line(ntrip_reply(request)) == b"ICY 200 OK"


def test_http_1_1_get_without_ntrip_version_is_v1(ntrip_reply: Callable[..., bytes]) -> None:
    request = f"GET /MP1 HTTP/1.1\r\nHost: caster\r\nUser-Agent: {RELAY_UA}\r\n\r\n"
    assert _first_line(ntrip_reply(request)) == b"ICY 200 OK"


@pytest.mark.parametrize("version_header", ["Ntrip/2.0", "NTRIP/2.0", "ntrip/2.0"])
def test_get_with_ntrip_version_header_is_v2_in_any_case(
    ntrip_reply: Callable[..., bytes], version_header: str
) -> None:
    request = (
        f"GET /MP1 HTTP/1.1\r\nHost: caster\r\nNtrip-Version: {version_header}\r\n"
        "User-Agent: NTRIP SomeClient/1.0\r\n\r\n"
    )
    assert _first_line(ntrip_reply(request)) == b"HTTP/1.1 200 OK"


def test_source_is_v1(ntrip_reply: Callable[..., bytes]) -> None:
    request = f"SOURCE secret /MP1\r\nSource-Agent: {RELAY_UA}\r\n\r\n"
    assert _first_line(ntrip_reply(request)) == b"ICY 200 OK"
