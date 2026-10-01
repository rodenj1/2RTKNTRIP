"""Tests for releasing a user's download slot when the download ends.

Seam under test: NTRIPHandler.handle_request(), driving real v1 GET downloads
with Basic auth over fake sockets, against a real ConnectionManager, the real
per-user limit check and a real forwarder instance. Only the database is
stubbed (every user and mount checks out). Each session runs in a thread, so
a session that never notices its client has gone can't hang the test.

Covers issue #27 (D12): a download's per-user slot is released as soon as it
closes, whether the client or the caster closes it, including on error paths,
so a user can reconnect at once and the admin view's count drops.
"""

import base64
import threading
import time
from collections.abc import Callable, Iterator
from unittest.mock import MagicMock

import pytest
from pytest_mock import MockerFixture

from ntrip_caster import config
from ntrip_caster.connection import ConnectionManager
from ntrip_caster.forwarder import SimpleDataForwarder
from ntrip_caster.ntrip import NTRIPHandler

USER = "rover"
AUTH = base64.b64encode(f"{USER}:secret".encode()).decode()
V1_GET_MP1 = f"GET /MP1 HTTP/1.0\r\nUser-Agent: NTRIP Test/1.0\r\nAuthorization: Basic {AUTH}\r\n\r\n".encode()
SESSION_TIMEOUT_S = 3.0
# A representative RTCM3 1005 frame (same fixed literal as test_ntrip_ingest.py).
RTCM_1005 = bytes.fromhex("d3000c3ed7d30203ff2e0000000000000000")

# What a client socket's recv() returns once the request has been read: bytes
# (b"" is the client closing), or a callable whose result is returned (it may
# block, like an idle client, or raise).
Recv = bytes | Callable[..., bytes]


class Sessions:
    """Download sessions run in threads, plus the events that hold idle clients open."""

    def __init__(self) -> None:
        self.threads: list[threading.Thread] = []
        self.events: list[threading.Event] = []

    def event(self) -> threading.Event:
        """An event an idle client waits on; teardown sets it so no session is left blocked."""
        event = threading.Event()
        self.events.append(event)
        return event

    def start(self, recv_after_request: list[Recv], sock: MagicMock | None = None) -> MagicMock:
        """Start one v1 download session in a thread; return its socket."""
        sock = sock if sock is not None else MagicMock()
        queue: list[Recv] = [V1_GET_MP1, *recv_after_request]

        def _recv(*args: object) -> bytes:
            item = queue.pop(0)
            return item(*args) if callable(item) else item

        sock.recv.side_effect = _recv
        handler = NTRIPHandler(sock, ("127.0.0.1", 50000 + len(self.threads)), _db())
        thread = threading.Thread(target=handler.handle_request, name=f"session-{len(self.threads)}", daemon=True)
        self.threads.append(thread)
        thread.start()
        return sock

    def finish(self, index: int) -> None:
        """Wait for one session to end; fail if it never notices its end."""
        thread = self.threads[index]
        thread.join(SESSION_TIMEOUT_S)
        assert not thread.is_alive(), f"{thread.name} never noticed its download had ended"


@pytest.fixture
def manager(mocker: MockerFixture) -> ConnectionManager:
    """A fresh, real ConnectionManager with MP1 online, wired in for the caster."""
    real = ConnectionManager()
    mocker.patch.object(real, "_generate_initial_str")
    mocker.patch.object(real, "start_str_correction")
    mocker.patch("ntrip_caster.connection.get_connection_manager", return_value=real)
    real.add_mount_connection("MP1", "10.0.0.1")
    return real


@pytest.fixture
def forwarder(mocker: MockerFixture) -> Iterator[SimpleDataForwarder]:
    """A fresh, real forwarder instance, wired in for the caster. Tests start it when they need broadcasting."""
    real = SimpleDataForwarder()
    mocker.patch("ntrip_caster.forwarder.forwarder", real)
    yield real
    real.stop()


@pytest.fixture
def limit(monkeypatch: pytest.MonkeyPatch) -> Callable[[int], None]:
    """Set the per-user connection limit for this test."""

    def _set(n: int) -> None:
        monkeypatch.setattr(config.settings.ntrip, "max_connections_per_user", n)

    return _set


@pytest.fixture
def sessions(manager: ConnectionManager, forwarder: SimpleDataForwarder) -> Iterator[Sessions]:
    """Download sessions for the test. Torn down before the caster's patches are undone:
    every idle client is released and every thread joined, so none outlives the test."""
    started = Sessions()
    yield started
    for event in started.events:
        event.set()
    for thread in started.threads:
        thread.join(SESSION_TIMEOUT_S)


def _db() -> MagicMock:
    db = MagicMock()
    db.verify_download_user.return_value = (True, "ok")
    db.check_mount_exists_in_db.return_value = True
    return db


def _idle_until(event: threading.Event, then: bytes | BaseException) -> Callable[..., bytes]:
    """A recv() that blocks like an idle client until ``event`` is set, then returns or raises ``then``."""

    def _recv(*_args: object) -> bytes:
        event.wait(SESSION_TIMEOUT_S)
        if isinstance(then, BaseException):
            raise then
        return then

    return _recv


def _wait_until(condition: Callable[[], bool]) -> None:
    deadline = time.monotonic() + SESSION_TIMEOUT_S
    while not condition():
        assert time.monotonic() < deadline, "timed out waiting for the caster"
        time.sleep(0.01)


def _first_line(sock: MagicMock) -> bytes:
    sent = b"".join(c.args[0] for c in sock.send.call_args_list + sock.sendall.call_args_list)
    return sent.split(b"\r\n", 1)[0]


def test_user_can_reconnect_at_once_after_closing_a_download(
    manager: ConnectionManager, limit: Callable[[int], None], sessions: Sessions
) -> None:
    limit(1)
    first = sessions.start([b""])  # the client closes straight after connecting
    sessions.finish(0)
    assert _first_line(first) == b"ICY 200 OK"

    second = sessions.start([b""])
    sessions.finish(1)

    assert _first_line(second) == b"ICY 200 OK", "the closed download still counted against the limit"
    assert manager.get_user_connection_count(USER) == 0


def test_caster_dropping_one_download_frees_only_that_slot(
    manager: ConnectionManager, forwarder: SimpleDataForwarder, limit: Callable[[int], None], sessions: Sessions
) -> None:
    limit(2)
    keep_open, dropped = sessions.event(), sessions.event()

    # Two downloads by the same user: A stays connected; B's link is dead, so
    # the caster's next send to it fails and the caster closes its socket.
    sessions.start([_idle_until(keep_open, b"")])
    b_sock = MagicMock()
    b_sock.sendall.side_effect = BrokenPipeError
    b_sock.close.side_effect = lambda: dropped.set()
    sessions.start([_idle_until(dropped, OSError(9, "Bad file descriptor"))], sock=b_sock)
    _wait_until(lambda: manager.get_user_connection_count(USER) == 2)

    forwarder.start()
    forwarder.upload_data("MP1", RTCM_1005)
    sessions.finish(1)

    assert dropped.is_set(), "the caster never dropped B"
    assert manager.get_user_connection_count(USER) == 1, "dropping B must free B's slot and only B's"
    assert sessions.threads[0].is_alive(), "A is still connected"

    keep_open.set()
    sessions.finish(0)
    assert manager.get_user_connection_count(USER) == 0


def test_slot_is_released_when_the_download_fails_to_start(
    manager: ConnectionManager,
    forwarder: SimpleDataForwarder,
    limit: Callable[[int], None],
    sessions: Sessions,
    mocker: MockerFixture,
) -> None:
    limit(1)
    mocker.patch.object(forwarder, "add_client", side_effect=RuntimeError("socket gone"))

    failed = sessions.start([])
    sessions.finish(0)

    assert _first_line(failed).endswith(b" 500 Internal Server Error")
    assert manager.get_user_connection_count(USER) == 0, "the failed download kept its slot"


def test_slot_is_released_when_the_download_fails_after_starting(
    manager: ConnectionManager, limit: Callable[[int], None], sessions: Sessions, mocker: MockerFixture
) -> None:
    limit(1)
    # Something fails once the client is added and the slot taken, before streaming starts:
    # here, recording the new client connection in the metrics.
    metrics = mocker.patch("ntrip_caster.ntrip.metrics")
    metrics.ACTIVE_CONNECTIONS.labels.side_effect = RuntimeError("metrics backend gone")

    sessions.start([b""])
    sessions.finish(0)

    assert manager.get_user_connection_count(USER) == 0, "the failed download kept its slot"
