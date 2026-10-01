from unittest.mock import MagicMock

import pytest

from ntrip_caster.database import DatabaseManager
from ntrip_caster.ntrip import NTRIPHandler


@pytest.fixture
def mock_socket() -> MagicMock:
    return MagicMock()


@pytest.fixture
def mock_db_manager() -> MagicMock:
    return MagicMock()


def test_parse_request_line(mock_socket: MagicMock, mock_db_manager: DatabaseManager) -> None:
    handler = NTRIPHandler(mock_socket, ("127.0.0.1", 12345), mock_db_manager)

    # NTRIP 1.0 SOURCE
    method, path, protocol = handler._parse_request_line("SOURCE password /MOUNT")
    assert method == "SOURCE"
    assert path == "/MOUNT"
    assert protocol == "NTRIP/1.0"
    assert handler.ntrip1_password == "password"

    # NTRIP 1.0 GET
    method, path, protocol = handler._parse_request_line("GET /MOUNT HTTP/1.0")
    assert method == "GET"
    assert path == "/MOUNT"
    assert protocol == "HTTP/1.0"

    # RTSP
    method, path, protocol = handler._parse_request_line("DESCRIBE rtsp://localhost/MOUNT RTSP/1.0")
    assert method == "DESCRIBE"
    assert path == "rtsp://localhost/MOUNT"
    assert protocol == "RTSP/1.0"


def test_parse_headers(mock_socket: MagicMock, mock_db_manager: DatabaseManager) -> None:
    handler = NTRIPHandler(mock_socket, ("127.0.0.1", 12345), mock_db_manager)
    header_lines = ["User-Agent: NTRIP Client", "Authorization: Basic dXNlcjpwYXNz", "Host: localhost"]
    headers = handler._parse_headers(header_lines)
    assert headers["user-agent"] == "NTRIP Client"
    assert headers["authorization"] == "Basic dXNlcjpwYXNz"
    assert headers["host"] == "localhost"


def test_determine_ntrip_version(mock_socket: MagicMock, mock_db_manager: DatabaseManager) -> None:
    handler = NTRIPHandler(mock_socket, ("127.0.0.1", 12345), mock_db_manager)

    # NTRIP 1.0
    handler._determine_ntrip_version({}, "SOURCE pass /MOUNT")
    assert handler.ntrip_version == "1.0"
    assert handler.protocol_type == "ntrip1_0"

    # NTRIP 2.0 (via header), matched case-insensitively
    for value in ("Ntrip/2.0", "NTRIP/2.0", "ntrip/2.0"):
        handler._determine_ntrip_version({"ntrip-version": value}, "GET /MOUNT HTTP/1.1")
        assert handler.ntrip_version == "2.0"
        assert handler.protocol_type == "ntrip2_0"

    # The User-Agent is not a version signal: without Ntrip-Version a GET is v1
    handler._determine_ntrip_version({"user-agent": "NTRIP 2.0 Client"}, "GET /MOUNT HTTP/1.1")
    assert handler.ntrip_version == "1.0"
    assert handler.protocol_type == "ntrip1_0_http"


def test_is_valid_request(mock_socket: MagicMock, mock_db_manager: DatabaseManager) -> None:
    handler = NTRIPHandler(mock_socket, ("127.0.0.1", 12345), mock_db_manager)

    # Valid GET
    handler.protocol_type = "ntrip1_0"
    valid, msg = handler._is_valid_request("GET", "/MOUNT", {})
    assert valid

    # Missing Host in HTTP/1.1
    handler.protocol_type = "ntrip2_0"
    valid, msg = handler._is_valid_request("GET", "/MOUNT", {})
    assert not valid
    assert "Missing Host header" in msg

    # Unsupported method (reset protocol_type to avoid Host check)
    handler.protocol_type = "ntrip1_0"
    valid, msg = handler._is_valid_request("UNKNOWN", "/MOUNT", {})
    assert not valid
    assert "Unsupported method" in msg


def test_sanitize_request_for_logging(mock_socket: MagicMock, mock_db_manager: DatabaseManager) -> None:
    handler = NTRIPHandler(mock_socket, ("127.0.0.1", 12345), mock_db_manager)

    raw_request = "SOURCE secretpassword /MOUNT\r\nAuthorization: Basic dXNlcjpwYXNz\r\nOther: Header"
    sanitized = handler._sanitize_request_for_logging(raw_request)

    assert "secretpassword" not in sanitized
    assert "[PASSWORD_REDACTED]" in sanitized
    assert "dXNlcjpwYXNz" not in sanitized
    assert "[REDACTED]" in sanitized
    assert "Other: Header" in sanitized
