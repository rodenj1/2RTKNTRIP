"""Regression tests for the PROXY-v2 handler integration (leftover-bytes bug).

The bug (caught in homelab-talos#510): when proxy_protocol is enabled but a
connection has NO PROXY header, parse_proxy_v2 reads 16 bytes to check the
signature and returns them as `leftover`. The handler MUST prepend those bytes
to its first read, or the first 16 bytes of the request line are eaten.

These tests exercise the read path in isolation with a fake socket, mirroring
how NTRIPHandler.handle_request consumes initial_bytes + recv().
"""

from __future__ import annotations

import socket
import struct

from ntrip_caster.proxy_protocol import parse_proxy_v2

_SIG_V2 = b"\x0d\x0a\x0d\x0a\x00\x0d\x0a\x51\x55\x49\x54\x0a"


class ChunkSocket:
    """Fake socket serving preset bytes; recv() returns the remaining buffer."""

    def __init__(self, data: bytes):
        self._data = data
        self.pos = 0

    def recv(self, n: int) -> bytes:
        chunk = self._data[self.pos : self.pos + n]
        self.pos += len(chunk)
        return chunk


def _reassemble(sock: ChunkSocket) -> bytes:
    """Mimic _handle_client_connection + handle_request: parse PROXY (getting
    leftover), then the handler's first recv(); the full request is
    leftover + recv()."""
    result = parse_proxy_v2(sock)
    first_read = sock.recv(81920)
    return result.leftover + first_read


def test_no_proxy_request_line_fully_preserved() -> None:
    # THE regression: a plain NTRIP request with proxy_protocol enabled but no
    # header. The full request line must survive (not lose its first 16 bytes).
    req = b"GET /MOUNT HTTP/1.1\r\nUser-Agent: NTRIP client\r\n\r\n"
    got = _reassemble(ChunkSocket(req))
    assert got == req, f"request corrupted: {got!r}"
    assert got.startswith(b"GET /MOUNT HTTP/1.1")


def test_no_proxy_short_request_preserved() -> None:
    # Request shorter than the 16-byte peek must not be lost or truncated.
    req = b"GET /\r\n\r\n"
    got = _reassemble(ChunkSocket(req))
    assert got == req


def test_proxy_v2_header_stripped_body_intact() -> None:
    # A real PROXY-v2 IPv4 header followed by the request: header consumed,
    # request delivered intact (leftover empty, body still on socket).
    hdr = (
        _SIG_V2
        + bytes([0x21, 0x11])
        + struct.pack("!H", 12)
        + socket.inet_pton(socket.AF_INET, "203.0.113.9")
        + socket.inet_pton(socket.AF_INET, "10.0.0.1")
        + struct.pack("!H", 40000)
        + struct.pack("!H", 2101)
    )
    req = b"GET /MOUNT HTTP/1.1\r\n\r\n"
    result = parse_proxy_v2(ChunkSocket(hdr + req))
    assert result.consumed is True
    assert result.real_addr == ("203.0.113.9", 40000)
    # leftover is empty; the request body is still readable from the socket.
    sock = ChunkSocket(hdr + req)
    got = _reassemble(sock)
    assert got == req
