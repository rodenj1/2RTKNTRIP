"""Tests for the headers on NTRIP v2 replies.

Seam under test: NTRIPHandler.handle_request(), driven by a fake socket whose
recv() yields one raw request, observing the reply headers written back.
Collaborators are stubbed by the ``ntrip_reply`` fixture (conftest).

Covers issue #24: v2 replies carry ``Ntrip-Version: Ntrip/2.0`` (D4), a v2
data stream is ``gnss/data`` (D5) and a v2 sourcetable is
``gnss/sourcetable`` (D6), as NTRIP 2.0 defines and BKG's reference client
and server require.
"""

from collections.abc import Callable

V2_GET = "GET {path} HTTP/1.1\r\nHost: caster\r\nNtrip-Version: Ntrip/2.0\r\nUser-Agent: NTRIP Test/1.0\r\n\r\n"


def _headers(reply: bytes) -> dict[str, str]:
    """Parse the header block of a reply into a name -> value map (names lowercased)."""
    head = reply.split(b"\r\n\r\n", 1)[0].decode()
    lines = head.split("\r\n")[1:]
    return {name.strip().lower(): value.strip() for name, value in (line.split(":", 1) for line in lines)}


def test_v2_stream_reply_is_gnss_data_with_ntrip_2_0(ntrip_reply: Callable[..., bytes]) -> None:
    headers = _headers(ntrip_reply(V2_GET.format(path="/MP1")))
    assert headers["content-type"] == "gnss/data"
    assert headers["ntrip-version"] == "Ntrip/2.0"


def test_v2_sourcetable_reply_is_gnss_sourcetable(ntrip_reply: Callable[..., bytes]) -> None:
    headers = _headers(ntrip_reply(V2_GET.format(path="/")))
    assert headers["content-type"] == "gnss/sourcetable"
    assert headers["ntrip-version"] == "Ntrip/2.0"


def test_v2_error_reply_carries_ntrip_2_0(ntrip_reply: Callable[..., bytes]) -> None:
    reply = ntrip_reply(V2_GET.format(path="/NOPE"), mount_exists=False)
    assert reply.startswith(b"HTTP/1.1 404")
    assert _headers(reply)["ntrip-version"] == "Ntrip/2.0"


def test_v2_upload_reply_carries_ntrip_2_0(ntrip_reply: Callable[..., bytes]) -> None:
    reply = ntrip_reply("POST /MP1 HTTP/1.1\r\nHost: caster\r\nNtrip-Version: Ntrip/2.0\r\n\r\n")
    assert reply.startswith(b"HTTP/1.1 200")
    assert _headers(reply)["ntrip-version"] == "Ntrip/2.0"


def test_v2_auth_challenge_carries_ntrip_2_0(ntrip_reply: Callable[..., bytes]) -> None:
    reply = ntrip_reply(V2_GET.format(path="/MP1"), authorized=False)
    assert reply.startswith(b"HTTP/1.1 401")
    assert _headers(reply)["ntrip-version"] == "Ntrip/2.0"
