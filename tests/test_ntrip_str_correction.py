"""Tests for a mount's sourcetable (STR) correction when its source reconnects.

Seam under test: NTRIPHandler.handle_request(), driving real v1 SOURCE upload
sessions over fake sockets against a real ConnectionManager and a real
RTCM2ParserManager. Each session's STR parser is a fake whose result says which
session it came from. Each session's STR correction is captured instead of
run on a background thread after its wait, so each test decides when each
correction completes. Auth, the forwarder and the delayed cleanup timer are
stubbed, and the upload's ingest loop returns at once.

Covers issue #34: after a quick reconnect, the old session's STR correction
neither stops the new session's parser nor writes to the new session's STR
entry; the new session's STR is corrected from its own parse; a session that
doesn't reconnect still gets its STR corrected.
"""

import time
import types
from collections.abc import Callable
from typing import Any
from unittest.mock import MagicMock

import pytest
from pytest_mock import MockerFixture

from ntrip_caster.connection import ConnectionManager
from ntrip_caster.ntrip import NTRIPHandler
from ntrip_caster.rtcm2_manager import RTCM2ParserManager

SOURCE_MP1 = b"SOURCE secret /MP1\r\nSource-Agent: NTRIP Test/1.0\r\n\r\n"


class FakeParser:
    """Stands in for an RTCM parsing thread; its result names the session it parsed for."""

    def __init__(self, city: str) -> None:
        self.result: dict[str, Any] = {"mount": "MP1", "bitrate": 1000, "location": {"city": city}}
        self.stopped = False

    def stop(self) -> None:
        self.stopped = True


class Harness:
    """The caster's STR machinery, with each session's parser and correction under test control."""

    def __init__(self, manager: ConnectionManager) -> None:
        self.manager = manager
        self.rtcm_manager = RTCM2ParserManager()
        self.parsers: list[FakeParser] = []
        self.corrections: list[Callable[[], None]] = []

    def upload_session(self) -> FakeParser:
        """Run one SOURCE upload session for MP1; return the STR parser it started."""
        sock = MagicMock()
        sock.recv.side_effect = [SOURCE_MP1, b""]
        NTRIPHandler(sock, ("10.0.0.1", 40000), MagicMock()).handle_request()
        return self.parsers[-1]

    def str_city(self) -> str:
        info = self.manager.get_mount_info("MP1")
        assert info is not None and info["str_data"]
        return str(info["str_data"]).split(";")[2]


@pytest.fixture
def harness(mocker: MockerFixture) -> Harness:
    manager = ConnectionManager()
    h = Harness(manager)
    mocker.patch("ntrip_caster.connection.get_connection_manager", return_value=manager)
    mocker.patch("ntrip_caster.connection.rtcm_manager", h.rtcm_manager)

    def _new_parser(*_args: object, **_kwargs: object) -> FakeParser:
        parser = FakeParser(city=f"session{len(h.parsers) + 1}")
        h.parsers.append(parser)
        return parser

    mocker.patch("ntrip_caster.rtcm2.start_str_fix_parser", side_effect=_new_parser)

    def _capture_thread(target: Callable[[], None], **_kwargs: object) -> MagicMock:
        h.corrections.append(target)
        return MagicMock()

    # Swap only this module's view of threading and time (it uses nothing else from
    # them): corrections are captured instead of run on a thread, and their wait is
    # skipped. Patching threading.Thread itself would affect the whole process.
    mocker.patch("ntrip_caster.connection.threading", types.SimpleNamespace(Thread=_capture_thread))
    mocker.patch("ntrip_caster.connection.time", types.SimpleNamespace(time=time.time, sleep=lambda _s: None))
    mocker.patch("ntrip_caster.ntrip.forwarder")
    mocker.patch("ntrip_caster.ntrip.threading.Timer")
    mocker.patch.object(NTRIPHandler, "verify_user", return_value=(True, "ok"))
    mocker.patch.object(NTRIPHandler, "_receive_rtcm_data")
    return h


def test_old_sessions_correction_leaves_a_reconnected_session_alone(harness: Harness) -> None:
    harness.upload_session()  # session 1 connects, starts its STR parse...
    new_parser = harness.upload_session()  # ...and reconnects before that parse completes
    old_correction = harness.corrections[0]
    str_before = harness.str_city()

    old_correction()

    assert not new_parser.stopped, "the old correction stopped the new session's parser"
    assert harness.str_city() == str_before, "the old correction wrote to the new session's STR"


def test_reconnected_session_is_corrected_from_its_own_parse(harness: Harness) -> None:
    harness.upload_session()
    new_parser = harness.upload_session()
    old_correction, new_correction = harness.corrections
    str_before = harness.str_city()

    old_correction()
    assert harness.str_city() == str_before, "the old correction must not apply anything"
    new_correction()

    assert harness.str_city() == "session2", "the new session's own correction applies its own parse"
    assert new_parser.stopped, "the new session's correction stops its own parser when done"


def test_session_that_does_not_reconnect_is_still_corrected(harness: Harness) -> None:
    parser = harness.upload_session()

    harness.corrections[0]()

    assert harness.str_city() == "session1"
    assert parser.stopped


def test_session_whose_parser_the_web_view_replaced_is_still_corrected(harness: Harness, mocker: MockerFixture) -> None:
    # Opening the web view's live RTCM display for the mount swaps the STR parser for a
    # web parser fed by the same session's data. The correction should still use it,
    # and must not stop it (that would kill the live view).
    harness.upload_session()
    web_parser = FakeParser(city="session1-web")
    mocker.patch("ntrip_caster.rtcm2.start_web_parser", return_value=web_parser)
    harness.rtcm_manager.start_parser("MP1", mode="realtime_web")

    harness.corrections[0]()

    assert harness.str_city() == "session1-web"
    assert not web_parser.stopped, "the correction stopped the web view's live parser"
