"""Tests for the replies an RTSP client gets.

Seam under test: NTRIPHandler.handle_request(), driven by a fake socket whose
recv() yields one raw RTSP request, observing the reply written back.
Collaborators are stubbed by the ``ntrip_reply`` fixture (conftest).

Covers issue #31: RTSP error replies and auth challenges start with
``RTSP/1.0 <code> <reason>`` and carry the request's CSeq, rather than going
out as NTRIP v1 replies (``HTTP/1.0 …``). Every RTSP reply carries the
request's CSeq exactly once.
"""

from collections.abc import Callable

import pytest


def _rtsp(method: str, url: str, cseq: int = 7) -> str:
    return f"{method} {url} RTSP/1.0\r\nCSeq: {cseq}\r\nUser-Agent: Test RTSP/1.0\r\n\r\n"


def _first_line(reply: bytes) -> bytes:
    return reply.split(b"\r\n", 1)[0]


def _header_values(reply: bytes, name: str) -> list[str]:
    head = reply.split(b"\r\n\r\n", 1)[0].decode()
    return [
        line.split(":", 1)[1].strip()
        for line in head.split("\r\n")[1:]
        if ":" in line and line.split(":", 1)[0].strip().lower() == name.lower()
    ]


def test_rtsp_unknown_mount_is_rtsp_404_with_cseq(ntrip_reply: Callable[..., bytes]) -> None:
    reply = ntrip_reply(_rtsp("DESCRIBE", "rtsp://caster/NOPE"), mount_exists=False)
    assert _first_line(reply) == b"RTSP/1.0 404 Not Found"
    assert _header_values(reply, "CSeq") == ["7"]


def test_rtsp_bad_credentials_get_an_rtsp_401_challenge_with_cseq(ntrip_reply: Callable[..., bytes]) -> None:
    reply = ntrip_reply(_rtsp("DESCRIBE", "rtsp://caster/MP1"), authorized=False)
    assert _first_line(reply) == b"RTSP/1.0 401 Unauthorized"
    assert any(v.startswith("Basic ") for v in _header_values(reply, "WWW-Authenticate"))
    assert _header_values(reply, "CSeq") == ["7"]


@pytest.mark.parametrize(
    ("method", "mount_exists", "expected_first_line"),
    [
        ("DESCRIBE", True, b"RTSP/1.0 200 OK"),
        ("SETUP", True, b"RTSP/1.0 200 OK"),
        ("SETUP", False, b"RTSP/1.0 404 Not Found"),
        ("PAUSE", True, b"RTSP/1.0 200 OK"),
        ("TEARDOWN", True, b"RTSP/1.0 200 OK"),
    ],
    ids=["describe", "setup", "setup-unknown-mount", "pause", "teardown"],
)
def test_rtsp_reply_echoes_the_request_cseq_exactly_once(
    ntrip_reply: Callable[..., bytes], method: str, mount_exists: bool, expected_first_line: bytes
) -> None:
    reply = ntrip_reply(_rtsp(method, "rtsp://caster/MP1", cseq=42), mount_exists=mount_exists)
    assert _first_line(reply) == expected_first_line
    assert _header_values(reply, "CSeq") == ["42"]


def test_rtsp_request_without_a_mount_is_rtsp_400(ntrip_reply: Callable[..., bytes]) -> None:
    reply = ntrip_reply(_rtsp("DESCRIBE", "rtsp://caster/"))
    assert _first_line(reply) == b"RTSP/1.0 400 Bad Request"
    assert _header_values(reply, "CSeq") == ["7"]


def test_rtsp_method_the_caster_does_not_implement_is_rtsp_501(ntrip_reply: Callable[..., bytes]) -> None:
    reply = ntrip_reply(_rtsp("ANNOUNCE", "rtsp://caster/MP1"))
    assert _first_line(reply) == b"RTSP/1.0 501 Not Implemented"
    assert _header_values(reply, "CSeq") == ["7"]
