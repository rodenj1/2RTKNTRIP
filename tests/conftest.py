import os
import sqlite3
import tempfile
from collections.abc import Generator
from typing import Protocol
from unittest.mock import MagicMock

import pytest
from pytest_mock import MockerFixture

from ntrip_caster import config
from ntrip_caster.database import init_db
from ntrip_caster.ntrip import NTRIPHandler


@pytest.fixture
def temp_db() -> Generator[str, None, None]:
    """Fixture for a temporary database"""
    fd, path = tempfile.mkstemp()
    original_db_path = config.settings.database.path
    config.settings.database.path = path

    # Initialize the database
    init_db()

    yield path

    # Cleanup
    os.close(fd)
    if os.path.exists(path):
        os.remove(path)
    config.settings.database.path = original_db_path


@pytest.fixture
def db_conn(temp_db: str) -> Generator[sqlite3.Connection, None, None]:
    """Fixture for a database connection"""
    conn = sqlite3.connect(temp_db)
    yield conn
    conn.close()


class NtripReply(Protocol):
    """Handle one raw NTRIP request and return every byte the caster sent back."""

    def __call__(
        self,
        request: str,
        mount_exists: bool = True,
        authorized: bool = True,
        mount_online: bool = True,
        occupied_by: str | None = None,
    ) -> bytes: ...


@pytest.fixture
def ntrip_reply(mocker: MockerFixture) -> NtripReply:
    """Handle one raw NTRIP request through NTRIPHandler.handle_request(); return every byte sent back.

    Auth, the database, the forwarder and the connection manager are stubbed so a
    well-formed request for a known mount is accepted. Pass ``mount_exists=False``
    to have the database report the mount as unknown, ``authorized=False`` to
    have authentication reject the request, ``mount_online=False`` to have no
    source online for the mount, or ``occupied_by="<ip>"`` to have another source
    already holding the mount.
    """
    verify_user = mocker.patch.object(NTRIPHandler, "verify_user")
    mocker.patch.object(NTRIPHandler, "_keep_connection_alive")
    mocker.patch.object(NTRIPHandler, "_receive_rtcm_data")
    conn = mocker.patch("ntrip_caster.ntrip.connection")
    conn.generate_mount_list.return_value = []
    manager = conn.get_connection_manager.return_value
    manager.add_mount_connection.return_value = (True, "ok")
    mocker.patch("ntrip_caster.ntrip.forwarder")

    def _reply(
        request: str,
        mount_exists: bool = True,
        authorized: bool = True,
        mount_online: bool = True,
        occupied_by: str | None = None,
    ) -> bytes:
        manager.is_mount_online.return_value = mount_online
        conn.check_mount_exists.return_value = mount_exists
        manager.get_mount_info.return_value = {"ip_address": occupied_by} if occupied_by else None
        verify_user.return_value = (True, "ok") if authorized else (False, "Invalid credentials")
        db = MagicMock()
        db.check_mount_exists_in_db.return_value = mount_exists
        sock = MagicMock()
        sock.recv.side_effect = [request.encode(), b""]
        NTRIPHandler(sock, ("127.0.0.1", 12345), db).handle_request()
        sent = b"".join(c.args[0] for c in sock.send.call_args_list + sock.sendall.call_args_list)
        assert sent, "caster sent nothing"
        return sent

    return _reply
