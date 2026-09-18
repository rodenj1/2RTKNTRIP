"""Tests for optional PROXY protocol v2 parsing on the NTRIP listener.

Critical backward-compat guarantee: a connection with NO proxy header must be
handled exactly as before (bytes returned untouched, no address rewrite).
"""

from __future__ import annotations

import socket
import struct

from ntrip_caster.proxy_protocol import parse_proxy_v2

_SIG_V2 = b"\x0d\x0a\x0d\x0a\x00\x0d\x0a\x51\x55\x49\x54\x0a"


class FakeSocket:
    """Minimal socket stand-in that serves a preset byte stream via recv()."""

    def __init__(self, data: bytes):
        self._data = data
        self.pos = 0

    def recv(self, n: int) -> bytes:
        chunk = self._data[self.pos : self.pos + n]
        self.pos += len(chunk)
        return chunk


def _v2_header(family: int, src_ip: str, src_port: int, dst_ip: str, dst_port: int) -> bytes:
    ver_cmd = 0x21  # v2, PROXY
    if family == 4:
        fam_proto = 0x11  # AF_INET + STREAM
        addr = (
            socket.inet_pton(socket.AF_INET, src_ip)
            + socket.inet_pton(socket.AF_INET, dst_ip)
            + struct.pack("!H", src_port)
            + struct.pack("!H", dst_port)
        )
    else:
        fam_proto = 0x21  # AF_INET6 + STREAM
        addr = (
            socket.inet_pton(socket.AF_INET6, src_ip)
            + socket.inet_pton(socket.AF_INET6, dst_ip)
            + struct.pack("!H", src_port)
            + struct.pack("!H", dst_port)
        )
    return _SIG_V2 + bytes([ver_cmd, fam_proto]) + struct.pack("!H", len(addr)) + addr


def test_no_proxy_header_is_backward_compatible() -> None:
    # A normal NTRIP request must pass through untouched.
    raw = b"GET /MOUNT HTTP/1.0\r\n\r\n"
    result = parse_proxy_v2(FakeSocket(raw))
    assert result.consumed is False
    assert result.real_addr is None
    # The first 16 bytes are returned so the caller can prepend them.
    assert raw.startswith(result.leftover)
    assert result.leftover == raw[:16]


def test_ntrip1_source_line_without_proxy_passes_through() -> None:
    raw = b"SOURCE pass /MOUNT\r\n"
    result = parse_proxy_v2(FakeSocket(raw))
    assert result.consumed is False
    assert result.leftover == raw[:16]


def test_proxy_v2_ipv4_parsed() -> None:
    hdr = _v2_header(4, "203.0.113.9", 40000, "10.0.0.1", 2101)
    result = parse_proxy_v2(FakeSocket(hdr))
    assert result.consumed is True
    assert result.real_addr == ("203.0.113.9", 40000)


def test_proxy_v2_ipv6_parsed() -> None:
    hdr = _v2_header(6, "2001:db8::1", 50000, "2001:db8::2", 2101)
    result = parse_proxy_v2(FakeSocket(hdr))
    assert result.consumed is True
    assert result.real_addr == ("2001:db8::1", 50000)


def test_proxy_v2_local_command_no_addr() -> None:
    # LOCAL command (health check): signature + ver_cmd 0x20, no address.
    hdr = _SIG_V2 + bytes([0x20, 0x00]) + struct.pack("!H", 0)
    result = parse_proxy_v2(FakeSocket(hdr))
    assert result.consumed is True
    assert result.real_addr is None


def test_short_read_treated_as_raw() -> None:
    # Fewer than 16 bytes and not the signature → treated as raw, returned.
    raw = b"GET /\r\n"
    result = parse_proxy_v2(FakeSocket(raw))
    assert result.consumed is False
    assert result.leftover == raw
