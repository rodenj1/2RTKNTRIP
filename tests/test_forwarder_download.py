"""Tests for the download send path (SimpleDataForwarder._send_data_simple).

Seam under test: _send_data_simple(client_info, data_list), driven with a fake
socket capturing sendall(). NTRIP downloads are intentionally un-chunked raw
octet-stream for every client; these tests lock that invariant so no future
edit can re-enable chunk framing without a matching Transfer-Encoding header
(issue #4).
"""

from typing import Any
from unittest.mock import MagicMock

import pytest

from ntrip_caster.forwarder import SimpleDataForwarder


def _client(protocol_version: str) -> tuple[dict[str, Any], MagicMock]:
    sock = MagicMock()
    info = {
        "socket": sock,
        "mount": "TESTMOUNT",
        "user": "u",
        "protocol_version": protocol_version,
        "send_errors": 0,
    }
    return info, sock


def _sent(sock: MagicMock) -> bytes:
    """Concatenate everything handed to socket.sendall()."""
    return b"".join(call.args[0] for call in sock.sendall.call_args_list)


@pytest.mark.parametrize("protocol_version", ["2.0", "1.0", "0.8", "ntrip2_0"])
def test_download_is_raw_octet_stream_no_chunk_framing(protocol_version: str) -> None:
    fwd = SimpleDataForwarder()
    payload = b"\xd3\x00\x08RTCMDATA"
    info, sock = _client(protocol_version)

    sent_count = fwd._send_data_simple(info, [(1.0, payload)])

    sent = _sent(sock)
    assert sent == payload
    assert sent_count == len(payload)
    # No HTTP chunk-size marker of the form "<hexlen>\r\n" precedes the payload.
    assert not sent.startswith(b"8\r\n")
    assert b"\r\n" not in sent


def test_download_multiple_messages_concatenate_raw() -> None:
    fwd = SimpleDataForwarder()
    a, b = b"\xd3\x00\x03AAA", b"\xd3\x00\x03BBB"
    info, sock = _client("2.0")

    sent_count = fwd._send_data_simple(info, [(1.0, a), (2.0, b)])

    assert _sent(sock) == a + b
    assert sent_count == len(a) + len(b)


def test_download_bytes_identical_across_protocol_versions() -> None:
    fwd = SimpleDataForwarder()
    payload = b"\xd3\x00\x05HELLO"

    v1_info, v1_sock = _client("1.0")
    v2_info, v2_sock = _client("2.0")
    fwd._send_data_simple(v1_info, [(1.0, payload)])
    fwd._send_data_simple(v2_info, [(1.0, payload)])

    assert _sent(v1_sock) == _sent(v2_sock) == payload
