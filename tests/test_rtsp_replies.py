"""Tests for the replies an RTSP client gets.

Seam under test: NTRIPHandler.handle_request(), driven by a fake socket whose
recv() yields one raw RTSP request, observing the reply written back.
Collaborators are stubbed by the ``ntrip_reply`` fixture (conftest).

Covers issue #31: RTSP error replies and auth challenges start with
``RTSP/1.0 <code> <reason>`` and carry the request's CSeq, rather than going
out as NTRIP v1 replies (``HTTP/1.0 …``). Every RTSP reply carries the
request's CSeq exactly once.

Covers issue #39: PLAY and RECORD check the mount and credentials before
replying, and answer with exactly one RTSP reply (an error instead of a 200, or
a 200 with no NTRIP status lines after it); OPTIONS gets an RTSP 200 with a
Public header.
"""

import re
from collections.abc import Callable

import pytest
from pytest_mock import MockerFixture

from ntrip_caster.ntrip import NTRIPHandler


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


STATUS_LINE = re.compile(rb"^(RTSP/1\.0 \d{3}|HTTP/1\.[01] \d{3}|ICY \d{3}|SOURCETABLE \d{3}|ERROR )", re.MULTILINE)


def _status_lines(reply: bytes) -> list[bytes]:
    """Every status line written back (RTSP, HTTP, ICY, SOURCETABLE or ERROR), in order."""
    return [m.group(0) for m in STATUS_LINE.finditer(reply)]


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


def test_rtsp_options_is_an_rtsp_200_listing_the_methods(ntrip_reply: Callable[..., bytes]) -> None:
    reply = ntrip_reply(_rtsp("OPTIONS", "rtsp://caster/MP1"))
    assert _status_lines(reply) == [b"RTSP/1.0 200"]
    public = _header_values(reply, "Public")
    assert len(public) == 1
    assert {m.strip() for m in public[0].split(",")} >= {
        "DESCRIBE",
        "SETUP",
        "PLAY",
        "PAUSE",
        "TEARDOWN",
        "RECORD",
        "OPTIONS",
    }
    assert _header_values(reply, "CSeq") == ["7"]


def test_rtsp_play_on_an_unavailable_mount_is_only_an_rtsp_404(ntrip_reply: Callable[..., bytes]) -> None:
    reply = ntrip_reply(_rtsp("PLAY", "rtsp://caster/MP1"), mount_online=False)
    assert _status_lines(reply) == [b"RTSP/1.0 404"]
    assert _header_values(reply, "CSeq") == ["7"]


def test_rtsp_play_that_succeeds_is_one_rtsp_200_with_no_ntrip_reply(ntrip_reply: Callable[..., bytes]) -> None:
    reply = ntrip_reply(_rtsp("PLAY", "rtsp://caster/MP1"))
    assert _status_lines(reply) == [b"RTSP/1.0 200"]
    assert _header_values(reply, "Range") == ["npt=0.000-"]
    assert len(_header_values(reply, "RTP-Info")) == 1
    assert _header_values(reply, "CSeq") == ["7"]


def test_rtsp_record_that_succeeds_is_one_rtsp_200_with_no_ntrip_reply(ntrip_reply: Callable[..., bytes]) -> None:
    reply = ntrip_reply(_rtsp("RECORD", "rtsp://caster/MP1"))
    assert _status_lines(reply) == [b"RTSP/1.0 200"]
    assert _header_values(reply, "CSeq") == ["7"]


def test_rtsp_record_to_a_taken_mount_is_only_an_rtsp_409(ntrip_reply: Callable[..., bytes]) -> None:
    reply = ntrip_reply(_rtsp("RECORD", "rtsp://caster/MP1"), occupied_by="10.0.0.9")
    assert _status_lines(reply) == [b"RTSP/1.0 409"]
    assert _header_values(reply, "CSeq") == ["7"]


def test_rtsp_record_with_bad_credentials_is_only_an_rtsp_401(ntrip_reply: Callable[..., bytes]) -> None:
    reply = ntrip_reply(_rtsp("RECORD", "rtsp://caster/MP1"), authorized=False)
    assert _status_lines(reply) == [b"RTSP/1.0 401"]
    assert _header_values(reply, "CSeq") == ["7"]


def _download_credentials_only(mocker: MockerFixture) -> None:
    """Credentials valid for downloading (a rover's), not for uploading to the mount."""
    mocker.patch.object(
        NTRIPHandler,
        "verify_user",
        side_effect=lambda _mount, _auth, request_type="upload": (
            (True, "ok") if request_type == "download" else (False, "Invalid credentials")
        ),
    )


def test_rtsp_play_by_a_rover_with_download_credentials_streams(
    ntrip_reply: Callable[..., bytes], mocker: MockerFixture
) -> None:
    _download_credentials_only(mocker)
    reply = ntrip_reply(_rtsp("PLAY", "rtsp://caster/MP1"))
    assert _status_lines(reply) == [b"RTSP/1.0 200"]


def test_rtsp_play_with_bad_credentials_is_only_an_rtsp_401(ntrip_reply: Callable[..., bytes]) -> None:
    reply = ntrip_reply(_rtsp("PLAY", "rtsp://caster/MP1"), authorized=False)
    assert _status_lines(reply) == [b"RTSP/1.0 401"]
    assert _header_values(reply, "CSeq") == ["7"]


def test_rtsp_play_on_an_unknown_mount_is_404_before_any_credential_check(
    ntrip_reply: Callable[..., bytes],
) -> None:
    reply = ntrip_reply(_rtsp("PLAY", "rtsp://caster/NOPE"), mount_exists=False, mount_online=False, authorized=False)
    assert _status_lines(reply) == [b"RTSP/1.0 404"]


def test_rtsp_play_of_the_sourcetable_is_an_rtsp_error_not_the_sourcetable(
    ntrip_reply: Callable[..., bytes],
) -> None:
    reply = ntrip_reply(_rtsp("PLAY", "rtsp://caster/sourcetable"))
    assert _status_lines(reply) == [b"RTSP/1.0 404"]


def test_rtsp_options_for_the_whole_server_is_an_rtsp_200(ntrip_reply: Callable[..., bytes]) -> None:
    reply = ntrip_reply(_rtsp("OPTIONS", "*"))
    assert _status_lines(reply) == [b"RTSP/1.0 200"]
    assert len(_header_values(reply, "Public")) == 1


def test_rtsp_record_without_a_mount_is_only_an_rtsp_400(ntrip_reply: Callable[..., bytes]) -> None:
    reply = ntrip_reply(_rtsp("RECORD", "rtsp://caster/"))
    assert _status_lines(reply) == [b"RTSP/1.0 400"]
    assert _header_values(reply, "CSeq") == ["7"]
