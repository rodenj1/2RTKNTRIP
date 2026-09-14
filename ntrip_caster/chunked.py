"""RFC 7230 §4.1 chunked-transfer-encoding decoder.

Used on the NTRIP v2 mount ingest path: an NTRIP 2.0 Server uploads its RTCM
body framed as HTTP chunked transfer-encoding, and the caster must strip that
framing before forwarding the payload downstream.

The decoder is stateful: chunk-size headers and chunk bodies do not align with
socket ``recv()`` boundaries, so a header or body may be split across several
:meth:`ChunkedDecoder.feed` calls. State is carried between calls and payload
bytes are emitted only once their framing is complete.
"""

from collections.abc import Callable


class ChunkedDecoderError(ValueError):
    """Raised when the byte stream violates the RFC 7230 chunked grammar."""


class ChunkedDecoder:
    """Incremental decoder for HTTP chunked transfer-encoding.

    Feed raw bytes with :meth:`feed`; it returns only the de-chunked payload
    available so far (possibly empty while a partial header or body is
    buffered). Once the terminal ``0\\r\\n\\r\\n`` chunk has been consumed,
    :attr:`complete` is ``True`` and any further payload-bearing bytes are
    rejected.
    """

    # Parser states
    _SIZE = "size"  # reading the <hexlen>[;ext]\r\n chunk header
    _DATA = "data"  # reading <size> payload bytes
    _DATA_CRLF = "data_crlf"  # consuming the \r\n that terminates a chunk body
    _TRAILER = "trailer"  # reading trailer header lines after the terminal chunk
    _DONE = "done"  # terminal chunk (and trailers) fully consumed

    _MAX_LINE = 8192  # guard against an unbounded chunk-size / trailer line

    def __init__(self) -> None:
        self._state = self._SIZE
        self._buf = bytearray()  # bytes for the current line being assembled
        self._remaining = 0  # payload bytes still expected in the current chunk

    @property
    def complete(self) -> bool:
        """True once the terminal chunk (and any trailers) has been consumed."""
        return self._state == self._DONE

    def feed(self, data: bytes) -> bytes:
        """Consume ``data`` and return any de-chunked payload it completes.

        Once the terminal chunk has been consumed (:attr:`complete`), any
        further bytes are ignored and ``b""`` is returned, so a benign trailing
        or pipelined read never corrupts the output stream.
        """
        if not data or self._state == self._DONE:
            return b""

        out = bytearray()
        pos = 0
        length = len(data)

        while pos < length and self._state != self._DONE:
            if self._state == self._SIZE:
                pos = self._consume_line(data, pos, self._on_size_line)
            elif self._state == self._DATA:
                take = min(self._remaining, length - pos)
                out += data[pos : pos + take]
                pos += take
                self._remaining -= take
                if self._remaining == 0:
                    self._state = self._DATA_CRLF
            elif self._state == self._DATA_CRLF:
                pos = self._consume_line(data, pos, self._on_data_crlf)
            elif self._state == self._TRAILER:
                pos = self._consume_line(data, pos, self._on_trailer_line)

        return bytes(out)

    def _consume_line(self, data: bytes, pos: int, on_line: Callable[[bytes], None]) -> int:
        """Accumulate bytes into the line buffer up to and including a ``\\n``.

        When a full line is buffered, strip a trailing CRLF/LF and hand it to
        ``on_line``. Returns the new position; if no newline is present yet the
        remaining bytes are buffered and the caller's loop ends.
        """
        nl = data.find(b"\n", pos)
        if nl == -1:
            self._buf += data[pos:]
            if len(self._buf) > self._MAX_LINE:
                raise ChunkedDecoderError("chunk header/trailer line too long")
            return len(data)
        self._buf += data[pos : nl + 1]
        line = bytes(self._buf).rstrip(b"\r\n")
        self._buf.clear()
        on_line(line)
        return nl + 1

    def _on_size_line(self, line: bytes) -> None:
        # A chunk-size line may carry ";chunk-extension" after the hex length.
        size_token = line.split(b";", 1)[0].strip()
        if not size_token:
            raise ChunkedDecoderError("empty chunk size")
        try:
            size = int(size_token, 16)
        except ValueError as exc:
            raise ChunkedDecoderError(f"invalid chunk size: {size_token!r}") from exc
        if size == 0:
            self._state = self._TRAILER
        else:
            self._remaining = size
            self._state = self._DATA

    def _on_data_crlf(self, line: bytes) -> None:
        if line:
            raise ChunkedDecoderError("expected CRLF after chunk data")
        self._state = self._SIZE

    def _on_trailer_line(self, line: bytes) -> None:
        # Trailers run until a blank line; ignore any trailer header content.
        if line == b"":
            self._state = self._DONE
