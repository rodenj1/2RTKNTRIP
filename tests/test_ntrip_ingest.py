"""Integration tests for the NTRIP mount ingest path.

Seam under test: NTRIPHandler.handle_request(), driven by a fake socket whose
recv() yields a real upload request, then queued body buffers, then b"" (EOF),
observing the bytes handed to forwarder.upload_data(). No real network.

Covers issue #3: chunked NTRIP v2 uploads must be de-chunked before forwarding,
while v1.0 uploads stay byte-for-byte raw passthrough. And issue #29: a body is
chunked only if its request says ``Transfer-Encoding: chunked`` (matched
case-insensitively); any other upload, whatever its version, is forwarded raw.
"""

from unittest.mock import MagicMock

from pytest_mock import MockerFixture

from ntrip_caster.ntrip import NTRIPHandler


def _framed(payload: bytes) -> bytes:
    """Encode one HTTP chunk the way a spec-compliant NTRIP v2 server does."""
    return f"{len(payload):x}\r\n".encode() + payload + b"\r\n"


def _v2_post(transfer_encoding: str | None = "chunked") -> bytes:
    """An NTRIP v2 upload request, with the given Transfer-Encoding header (None for none)."""
    te = f"Transfer-Encoding: {transfer_encoding}\r\n" if transfer_encoding is not None else ""
    return (
        "POST /TESTMOUNT HTTP/1.1\r\nHost: caster\r\nNtrip-Version: Ntrip/2.0\r\n"
        f"{te}User-Agent: NTRIP Test/1.0\r\n\r\n"
    ).encode()


# The upload request each NTRIP version's server sends before its body. A v2 server
# that chunks its body says so with Transfer-Encoding: chunked.
UPLOAD_REQUESTS = {
    "2.0": _v2_post(),
    "1.0": b"SOURCE secret /TESTMOUNT\r\nSource-Agent: NTRIP Test/1.0\r\n\r\n",
    "0.8": b"SOURCE /TESTMOUNT\r\nSource-Agent: NTRIP Test/1.0\r\n\r\n",
}


def _drive_ingest(mocker: MockerFixture, raw_request: bytes, recv_buffers: list[bytes]) -> bytes:
    """Run one upload through handle_request(); return the bytes forwarded to the mount.

    The socket returns ``raw_request``, then each body buffer in turn, then b"" to
    end the upload. Auth, the connection manager and the cleanup timer are stubbed so
    the test observes only what reaches forwarder.upload_data().
    """
    forwarded = bytearray()

    def _capture(_mount: str, data: bytes) -> None:
        forwarded.extend(data)

    mocker.patch("ntrip_caster.ntrip.forwarder.upload_data", side_effect=_capture)
    mocker.patch("ntrip_caster.ntrip.forwarder.remove_mount_buffer")
    manager = mocker.patch("ntrip_caster.ntrip.connection.get_connection_manager").return_value
    manager.check_mount_live.return_value = False  # no current holder: the upload is accepted
    manager.add_mount_connection.return_value = (True, "ok")
    mocker.patch("ntrip_caster.ntrip.threading.Timer")
    mocker.patch.object(NTRIPHandler, "verify_user", return_value=(True, "ok"))

    sock = MagicMock()
    sock.recv.side_effect = [raw_request, *recv_buffers, b""]

    NTRIPHandler(sock, ("127.0.0.1", 12345), MagicMock()).handle_request()

    return bytes(forwarded)


# A representative RTCM3 1005 frame: 0xD3 preamble, 10-bit length field, payload, 3-byte CRC.
# Fixed literal (independent source of truth), not recomputed by the code under test.
RTCM_1005 = bytes.fromhex("d3000c3ed7d30203ff2e0000000000000000")


def test_v2_upload_is_dechunked_to_raw_rtcm(mocker: MockerFixture) -> None:
    payload = RTCM_1005
    forwarded = _drive_ingest(mocker, UPLOAD_REQUESTS["2.0"], [_framed(payload)])
    assert forwarded == payload


def test_v2_equals_v1_for_same_payload(mocker: MockerFixture) -> None:
    payload = RTCM_1005
    v1 = _drive_ingest(mocker, UPLOAD_REQUESTS["1.0"], [payload])
    v2 = _drive_ingest(mocker, UPLOAD_REQUESTS["2.0"], [_framed(payload)])
    assert v2 == v1 == payload


def test_v1_upload_forwarded_verbatim(mocker: MockerFixture) -> None:
    raw = b"\xd3\x00\x10rawrtcmbytes!!!!"
    forwarded = _drive_ingest(mocker, UPLOAD_REQUESTS["1.0"], [raw])
    assert forwarded == raw


def test_v08_upload_forwarded_verbatim(mocker: MockerFixture) -> None:
    # NTRIP 0.8 uploads are raw like 1.0 — no decoder, byte-for-byte passthrough.
    raw = b"\xd3\x00\x0808bytes!!"
    forwarded = _drive_ingest(mocker, UPLOAD_REQUESTS["0.8"], [raw])
    assert forwarded == raw


def test_v2_chunk_split_across_recv_buffers(mocker: MockerFixture) -> None:
    payload = b"\xd3\x00\x08SPLITME!"
    frame = _framed(payload)
    # Split the single frame into three arbitrary recv() returns.
    buffers = [frame[:2], frame[2:5], frame[5:]]
    forwarded = _drive_ingest(mocker, UPLOAD_REQUESTS["2.0"], buffers)
    assert forwarded == payload


def test_v2_no_chunk_framing_bytes_leak(mocker: MockerFixture) -> None:
    # Two chunks whose hex-length markers ("3c", "5") would appear as CRC-fail
    # bytes if forwarded raw; assert they never reach the mount stream.
    a = b"A" * 0x3C
    b = b"BBBBB"
    forwarded = _drive_ingest(mocker, UPLOAD_REQUESTS["2.0"], [_framed(a) + _framed(b)])
    assert forwarded == a + b
    assert b"3c\r\n" not in forwarded
    assert b"\r\n" not in forwarded


def test_v2_terminal_chunk_ends_cleanly(mocker: MockerFixture) -> None:
    payload = b"\xd3\x00\x04DONE"
    forwarded = _drive_ingest(mocker, UPLOAD_REQUESTS["2.0"], [_framed(payload) + b"0\r\n\r\n"])
    assert forwarded == payload


def test_v2_upload_without_chunked_encoding_is_forwarded_raw(mocker: MockerFixture) -> None:
    forwarded = _drive_ingest(mocker, _v2_post(transfer_encoding=None), [RTCM_1005, RTCM_1005])
    assert forwarded == RTCM_1005 + RTCM_1005


def test_chunked_transfer_encoding_is_matched_case_insensitively(mocker: MockerFixture) -> None:
    forwarded = _drive_ingest(mocker, _v2_post("Chunked"), [_framed(RTCM_1005)])
    assert forwarded == RTCM_1005
