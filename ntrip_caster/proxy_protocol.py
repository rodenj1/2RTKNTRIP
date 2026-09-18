"""PROXY protocol v2 detection + parsing for the NTRIP listener.

Optional, backward-compatible: when a connection does NOT begin with the
PROXY-v2 signature (e.g. an internal LAN pusher or a client on a non-PROXY
entrypoint), the peeked bytes are returned untouched and the caster treats the
connection as ordinary raw NTRIP. Only used when explicitly enabled in config,
so a caster with no proxy in front behaves exactly as before.

Reference: HAProxy PROXY protocol v2 spec (binary). We support the common TCP
over IPv4/IPv6 case and gracefully fall back for LOCAL / unknown.
"""

from __future__ import annotations

import socket
import struct
from typing import Protocol


class _Recvable(Protocol):
    def recv(self, n: int, /) -> bytes: ...

# 12-byte v2 signature.
_SIG_V2 = b"\x0d\x0a\x0d\x0a\x00\x0d\x0a\x51\x55\x49\x54\x0a"

_AF_INET = 0x1
_AF_INET6 = 0x2


class ProxyParseResult:
    """Outcome of a peek: the real client (ip, port) if a PROXY header was
    consumed, plus any leftover bytes that belong to the real request and must
    be processed as if they were the first bytes off the socket."""

    def __init__(self, real_addr: tuple[str, int] | None, leftover: bytes, consumed: bool):
        self.real_addr = real_addr      # (ip, port) or None
        self.leftover = leftover        # bytes read past the header, feed to request parser
        self.consumed = consumed        # True if a PROXY v2 header was found + parsed


def _recv_exact(sock: _Recvable, n: int) -> bytes:
    """Read exactly n bytes (best effort); returns fewer only on EOF."""
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            break
        buf += chunk
    return buf


def parse_proxy_v2(sock: _Recvable) -> ProxyParseResult:
    """Peek the connection for a PROXY protocol v2 header.

    - If the first 12 bytes are NOT the v2 signature: returns those bytes as
      leftover, consumed=False, real_addr=None (caller feeds leftover to the
      normal request path — fully backward compatible).
    - If they ARE the signature: parses the header, returns the real client
      (ip, port) and any application bytes already read past the header.
    """
    head = _recv_exact(sock, 16)  # 12 sig + 1 ver/cmd + 1 fam/proto + 2 len
    if len(head) < 16 or head[:12] != _SIG_V2:
        # Not PROXY v2 → hand the bytes back untouched.
        return ProxyParseResult(None, head, consumed=False)

    ver_cmd = head[12]
    fam_proto = head[13]
    addr_len = struct.unpack("!H", head[14:16])[0]
    addr_block = _recv_exact(sock, addr_len)

    # ver must be 2 (high nibble 0x2); cmd 0x1 = PROXY, 0x0 = LOCAL.
    cmd = ver_cmd & 0x0F
    fam = (fam_proto & 0xF0) >> 4

    if cmd != 0x1 or fam == 0x0:
        # LOCAL (health check) or UNSPEC → no real address; nothing leftover
        # belongs to the app for LOCAL, but be safe and drop the addr block.
        return ProxyParseResult(None, b"", consumed=True)

    real_addr: tuple[str, int] | None = None
    try:
        if fam == _AF_INET and addr_len >= 12:
            src_ip = socket.inet_ntop(socket.AF_INET, addr_block[0:4])
            src_port = struct.unpack("!H", addr_block[8:10])[0]
            real_addr = (src_ip, src_port)
        elif fam == _AF_INET6 and addr_len >= 36:
            src_ip = socket.inet_ntop(socket.AF_INET6, addr_block[0:16])
            src_port = struct.unpack("!H", addr_block[32:34])[0]
            real_addr = (src_ip, src_port)
    except (OSError, struct.error):
        real_addr = None

    # Any bytes read past the declared addr block would be application data,
    # but _recv_exact read exactly addr_len, so there is no leftover here.
    return ProxyParseResult(real_addr, b"", consumed=True)
