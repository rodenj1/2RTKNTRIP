"""Tests for Socket.IO CORS allowed-origins configuration (issue #12).

Seam: WebsocketConfig.cors_allowed_origins (env-injectable) plus the
resolve_socketio_cors_origins() helper that maps the config to the value passed
to SocketIO(cors_allowed_origins=...). Empty config -> None (python-socketio's
safe same-origin default); never a blanket "*".
"""

import os
from pathlib import Path

from ntrip_caster import config


def test_cors_allowed_origins_defaults_empty() -> None:
    # Default: no cross-origins configured -> same-origin only.
    assert config.WebsocketConfig().cors_allowed_origins == []


def test_resolve_origins_none_when_empty() -> None:
    # Empty list must resolve to None so SocketIO keeps its same-origin default.
    assert config.resolve_socketio_cors_origins([]) is None


def test_resolve_origins_returns_configured_list() -> None:
    origins = ["https://ntrip-admin.big-spray.com"]
    assert config.resolve_socketio_cors_origins(origins) == origins


def test_resolve_origins_never_wildcard() -> None:
    # A wildcard must never be emitted (credentialed connections).
    assert config.resolve_socketio_cors_origins(["*"]) != "*"
    assert "*" not in (config.resolve_socketio_cors_origins(["*"]) or [])


def test_cors_origins_from_json_config(tmp_path: Path) -> None:
    json_file = tmp_path / "config.json"
    json_file.write_text(
        '{"websocket": {"cors_allowed_origins": ["https://ntrip-admin.big-spray.com"]}}'
    )
    os.environ["NTRIP_CONFIG_FILE"] = str(json_file)
    try:
        new_settings = config.load_settings()
        assert new_settings.websocket.cors_allowed_origins == ["https://ntrip-admin.big-spray.com"]
        assert config.resolve_socketio_cors_origins(
            new_settings.websocket.cors_allowed_origins
        ) == ["https://ntrip-admin.big-spray.com"]
    finally:
        del os.environ["NTRIP_CONFIG_FILE"]


def test_cors_origins_from_env(monkeypatch) -> None:
    # pydantic-settings parses complex types (list) from JSON in the env var.
    monkeypatch.setenv(
        "NTRIP_CASTER_WEBSOCKET__CORS_ALLOWED_ORIGINS",
        '["https://ntrip-admin.big-spray.com", "https://alt.example.com"]',
    )
    new_settings = config.Settings()
    assert new_settings.websocket.cors_allowed_origins == [
        "https://ntrip-admin.big-spray.com",
        "https://alt.example.com",
    ]
