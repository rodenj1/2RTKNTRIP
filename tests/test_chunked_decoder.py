"""Tests for the RFC 7230 chunked-transfer decoder used on the NTRIP v2 ingest path.

Seam under test: ntrip_caster.chunked.ChunkedDecoder, exercised purely as
bytes-in / bytes-out. No sockets, no NTRIP knowledge.

Framing per RFC 7230 4.1: each chunk is ``<hexlen>\r\n<data>\r\n``; the stream
ends with the terminal chunk ``0\r\n\r\n``.
"""

from ntrip_caster.chunked import ChunkedDecoder


def _frame(payload: bytes) -> bytes:
    """Encode one chunk the way a spec-compliant NTRIP v2 server does."""
    return f"{len(payload):x}\r\n".encode() + payload + b"\r\n"


def _feed_all(decoder: ChunkedDecoder, data: bytes, step: int) -> bytes:
    """Feed ``data`` in fixed-size slices, concatenating decoded output."""
    out = bytearray()
    for i in range(0, len(data), step):
        out += decoder.feed(data[i : i + step])
    return bytes(out)


def test_single_chunk_yields_payload() -> None:
    decoder = ChunkedDecoder()
    payload = b"\xd3\x00\x13RTCM1005"
    assert decoder.feed(_frame(payload)) == payload
    assert not decoder.complete


def test_multiple_chunks_in_one_feed_concatenate_in_order() -> None:
    decoder = ChunkedDecoder()
    a, b, c = b"first", b"\x00\x01\x02second", b"third"
    stream = _frame(a) + _frame(b) + _frame(c)
    assert decoder.feed(stream) == a + b + c


def test_chunk_size_header_split_across_feeds() -> None:
    # Payload of 60 bytes -> size header "3c\r\n"; split between '3' and 'c'.
    payload = b"P" * 0x3C
    frame = _frame(payload)
    decoder = ChunkedDecoder()
    first = decoder.feed(frame[:1])  # just "3"
    rest = decoder.feed(frame[1:])
    assert first == b""
    assert first + rest == payload


def test_chunk_body_split_across_feeds_emits_no_marker_bytes() -> None:
    payload = bytes(range(256)) * 4  # 1024 bytes, includes CR/LF byte values
    frame = _frame(payload)
    decoder = ChunkedDecoder()
    assert _feed_all(decoder, frame, 7) == payload


def test_crlf_terminator_split_across_feeds() -> None:
    payload = b"corrections"
    frame = _frame(payload)
    # Split so the trailing "\r\n" after the body straddles two feeds.
    cut = len(frame) - 1  # everything but the final "\n"
    decoder = ChunkedDecoder()
    out = decoder.feed(frame[:cut]) + decoder.feed(frame[cut:])
    assert out == payload
    # A following chunk must still parse cleanly (state resynced after CRLF).
    assert decoder.feed(_frame(b"next")) == b"next"


def test_terminal_chunk_signals_completion() -> None:
    decoder = ChunkedDecoder()
    payload = b"final-rtcm"
    out = decoder.feed(_frame(payload) + b"0\r\n\r\n")
    assert out == payload
    assert decoder.complete


def test_bytes_after_terminal_chunk_are_ignored() -> None:
    decoder = ChunkedDecoder()
    decoder.feed(b"0\r\n\r\n")
    assert decoder.complete
    # Trailing / pipelined bytes after end-of-stream must not corrupt output.
    assert decoder.feed(_frame(b"garbage")) == b""
    assert decoder.complete


def test_trailer_headers_after_terminal_chunk_do_not_corrupt_output() -> None:
    decoder = ChunkedDecoder()
    payload = b"body"
    stream = _frame(payload) + b"0\r\n" + b"X-Checksum: abc123\r\n" + b"\r\n"
    assert decoder.feed(stream) == payload
    assert decoder.complete


def test_chunk_extension_after_size_is_ignored() -> None:
    decoder = ChunkedDecoder()
    payload = b"withext"
    frame = f"{len(payload):x};name=value\r\n".encode() + payload + b"\r\n"
    assert decoder.feed(frame) == payload


def test_terminal_chunk_split_across_feeds() -> None:
    decoder = ChunkedDecoder()
    stream = _frame(b"x") + b"0\r\n\r\n"
    out = _feed_all(decoder, stream, 1)
    assert out == b"x"
    assert decoder.complete
