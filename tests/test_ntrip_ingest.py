"""Integration tests for the NTRIP mount ingest path (_receive_rtcm_data).

Seam under test: NTRIPHandler._receive_rtcm_data(mount), driven by a fake
socket whose recv() yields queued byte buffers then b"" (EOF), observing the
bytes handed to forwarder.upload_data(). This is the public ingest boundary;
no real network.

Covers issue #3: NTRIP v2 (chunked) uploads must be de-chunked before
forwarding, while v1.0 uploads stay byte-for-byte raw passthrough.
"""

from unittest.mock import MagicMock

from pytest_mock import MockerFixture

from ntrip_caster.ntrip import NTRIPHandler


def _framed(payload: bytes) -> bytes:
    """Encode one HTTP chunk the way a spec-compliant NTRIP v2 server does."""
    return f"{len(payload):x}\r\n".encode() + payload + b"\r\n"


def _drive_ingest(
    mocker: MockerFixture,
    ntrip_version: str,
    recv_buffers: list[bytes],
    mount: str = "TESTMOUNT",
) -> bytes:
    """Run the ingest loop over the given recv() buffers; return forwarded bytes.

    The socket returns each buffer in turn, then b"" to end the loop. All
    downstream collaborators (forwarder, connection manager, cleanup timer) are
    stubbed so the test observes only what reaches forwarder.upload_data().
    """
    forwarded = bytearray()

    def _capture(_mount: str, data: bytes) -> None:
        forwarded.extend(data)

    mocker.patch("ntrip_caster.ntrip.forwarder.upload_data", side_effect=_capture)
    mocker.patch("ntrip_caster.ntrip.forwarder.remove_mount_buffer")
    mocker.patch("ntrip_caster.ntrip.connection.get_connection_manager")
    mocker.patch("ntrip_caster.ntrip.threading.Timer")

    sock = MagicMock()
    sock.recv.side_effect = [*recv_buffers, b""]

    handler = NTRIPHandler(sock, ("127.0.0.1", 12345), MagicMock())
    handler.ntrip_version = ntrip_version
    handler._receive_rtcm_data(mount)

    return bytes(forwarded)


# A representative RTCM3 1005 frame: 0xD3 preamble, 10-bit length field, payload, 3-byte CRC.
# Fixed literal (independent source of truth), not recomputed by the code under test.
RTCM_1005 = bytes.fromhex("d3000c3ed7d30203ff2e0000000000000000")


def test_v2_upload_is_dechunked_to_raw_rtcm(mocker: MockerFixture) -> None:
    payload = RTCM_1005
    forwarded = _drive_ingest(mocker, "2.0", [_framed(payload)])
    assert forwarded == payload


def test_v2_equals_v1_for_same_payload(mocker: MockerFixture) -> None:
    payload = RTCM_1005
    v1 = _drive_ingest(mocker, "1.0", [payload])
    v2 = _drive_ingest(mocker, "2.0", [_framed(payload)])
    assert v2 == v1 == payload


def test_v1_upload_forwarded_verbatim(mocker: MockerFixture) -> None:
    raw = b"\xd3\x00\x10rawrtcmbytes!!!!"
    forwarded = _drive_ingest(mocker, "1.0", [raw])
    assert forwarded == raw


def test_v08_upload_forwarded_verbatim(mocker: MockerFixture) -> None:
    # NTRIP 0.8 uploads are raw like 1.0 — no decoder, byte-for-byte passthrough.
    raw = b"\xd3\x00\x0808bytes!!"
    forwarded = _drive_ingest(mocker, "0.8", [raw])
    assert forwarded == raw


def test_v2_chunk_split_across_recv_buffers(mocker: MockerFixture) -> None:
    payload = b"\xd3\x00\x08SPLITME!"
    frame = _framed(payload)
    # Split the single frame into three arbitrary recv() returns.
    buffers = [frame[:2], frame[2:5], frame[5:]]
    forwarded = _drive_ingest(mocker, "2.0", buffers)
    assert forwarded == payload


def test_v2_no_chunk_framing_bytes_leak(mocker: MockerFixture) -> None:
    # Two chunks whose hex-length markers ("3c", "5") would appear as CRC-fail
    # bytes if forwarded raw; assert they never reach the mount stream.
    a = b"A" * 0x3C
    b = b"BBBBB"
    forwarded = _drive_ingest(mocker, "2.0", [_framed(a) + _framed(b)])
    assert forwarded == a + b
    assert b"3c\r\n" not in forwarded
    assert b"\r\n" not in forwarded


def test_v2_terminal_chunk_ends_cleanly(mocker: MockerFixture) -> None:
    payload = b"\xd3\x00\x04DONE"
    forwarded = _drive_ingest(mocker, "2.0", [_framed(payload) + b"0\r\n\r\n"])
    assert forwarded == payload
