"""Regression test for the Socket.IO WebSocket transport (issue #17).

Flask-SocketIO's gevent server path (SocketIO.run -> the async_mode=='gevent'
branch) enables a native WebSocket transport ONLY if it can
`from geventwebsocket.handler import WebSocketHandler`. Without the
`gevent-websocket` package that import fails and Flask-SocketIO logs
"WebSocket transport not available. Install gevent-websocket for improved
performance." and falls back to HTTP long-polling.

This test guards that the dependency is present and importable so the native
WebSocket transport stays available (no fallback, no startup warning).
"""

import importlib

import pytest


def test_geventwebsocket_handler_importable() -> None:
    """The exact import Flask-SocketIO's gevent branch depends on must succeed.

    Flask-SocketIO's async_mode=='gevent' server path enables a native WebSocket
    transport iff `from geventwebsocket.handler import WebSocketHandler` succeeds;
    otherwise it logs the 'WebSocket transport not available' warning and falls
    back to HTTP long-polling. This asserts the gate is satisfied.
    """
    handler = importlib.import_module("geventwebsocket.handler")
    assert hasattr(handler, "WebSocketHandler"), (
        "geventwebsocket.handler.WebSocketHandler missing — Flask-SocketIO would "
        "fall back to polling and log the 'WebSocket transport not available' warning"
    )


def test_gevent_pywsgi_accepts_websocket_handler() -> None:
    """The WS handler must actually wire into a gevent pywsgi server at runtime."""
    pywsgi = pytest.importorskip("gevent.pywsgi")
    from geventwebsocket.handler import WebSocketHandler

    server = pywsgi.WSGIServer(
        ("127.0.0.1", 0), lambda environ, start_response: [b"ok"], handler_class=WebSocketHandler
    )
    server.start()
    try:
        assert server.server_port > 0
    finally:
        server.stop()
