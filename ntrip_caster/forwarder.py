#!/usr/bin/env python3

import socket
import threading
import time
from collections import deque
from threading import Lock, RLock
from typing import Any

from . import config, connection, logger, metrics


class RingBuffer:
    """Ring Buffer"""

    def __init__(self, maxlen: int | None = None) -> None:
        self.maxlen: int = maxlen or config.settings.data_forwarding.ring_buffer_size
        self.buffer: deque[dict[str, Any]] = deque(maxlen=self.maxlen)
        self.lock = Lock()
        self.total_bytes: int = 0
        self.total_messages: int = 0
        self.last_timestamp: float = time.time()
        self._write_index: int = 0
        self._read_index: int = 0

    def append(self, data: bytes, timestamp: float | None = None) -> None:
        """Add data to ring buffer"""
        if timestamp is None:
            timestamp = time.time()

        with self.lock:
            item = {"data": data, "timestamp": timestamp, "size": len(data), "index": self._write_index}
            self.buffer.append(item)
            self.total_bytes += len(data)
            self.total_messages += 1
            self.last_timestamp = timestamp
            self._write_index += 1

    def get_since(self, timestamp: float) -> list[tuple[float, bytes]]:
        """Get data since a specific timestamp"""
        with self.lock:
            if not self.buffer:
                return []

            result = []
            for item in self.buffer:
                if item["timestamp"] > timestamp:
                    result.append((item["timestamp"], item["data"]))

            return result

    def get_latest(self, count: int | None = None) -> list[tuple[float, bytes]]:
        """Get latest data"""
        with self.lock:
            if not self.buffer:
                return []

            if count is None:
                return [(item["timestamp"], item["data"]) for item in self.buffer]
            else:
                items = list(self.buffer)[-count:] if count > 0 else []
                return [(item["timestamp"], item["data"]) for item in items]

    def get_range(self, start_index: int, end_index: int | None = None) -> list[tuple[float, bytes]]:
        """Get data in an index range"""
        with self.lock:
            if not self.buffer:
                return []

            result = []
            for item in self.buffer:
                if item["index"] >= start_index:
                    if end_index is None or item["index"] <= end_index:
                        result.append((item["timestamp"], item["data"]))

            return result

    def get_stats(self) -> dict[str, Any]:
        """Get buffer statistics"""
        with self.lock:
            return {
                "size": len(self.buffer),
                "max_size": self.maxlen,
                "total_bytes": self.total_bytes,
                "total_messages": self.total_messages,
                "last_update": self.last_timestamp,
                "usage_percent": (len(self.buffer) / self.maxlen) * 100 if self.maxlen > 0 else 0,
                "write_index": self._write_index,
                "read_index": self._read_index,
            }

    def clear(self) -> None:
        """Clear buffer"""
        with self.lock:
            self.buffer.clear()
            self.total_bytes = 0
            self.total_messages = 0
            self._write_index = 0
            self._read_index = 0

    def is_full(self) -> bool:
        """Check if buffer is full"""
        with self.lock:
            return len(self.buffer) >= self.maxlen

    def is_empty(self) -> bool:
        """Check if buffer is empty"""
        with self.lock:
            return len(self.buffer) == 0


class SimpleDataForwarder:
    """Simplified data broadcaster"""

    def __init__(self, buffer_maxlen: int | None = None, broadcast_interval: float | None = None) -> None:
        self.buffer_maxlen: int = buffer_maxlen or config.settings.data_forwarding.ring_buffer_size
        self.broadcast_interval: float = broadcast_interval or config.settings.data_forwarding.broadcast_interval

        self.mount_buffers: dict[str, RingBuffer] = {}  # {mount_name: RingBuffer}
        self.buffer_lock = RLock()

        self.clients: dict[str, list[dict[str, Any]]] = {}  # {mount_name: [client_info]}
        self.client_lock = RLock()

        self.subscribers: dict[str, list[Any]] = {}  # {mount_name: [socket_write_end]}
        self.subscriber_lock = RLock()

        self.broadcast_thread: threading.Thread | None = None
        self.running: bool = False

        self.stats: dict[str, int] = {
            "total_clients": 0,
            "active_clients": 0,
            "total_bytes_sent": 0,
            "total_messages_sent": 0,
            "failed_sends": 0,
            "disconnected_clients": 0,
        }

    def start(self) -> None:
        """Start broadcast thread"""
        if self.running:
            return

        self.running = True
        self.broadcast_thread = threading.Thread(target=self._broadcast_loop, daemon=True)
        self.broadcast_thread.start()
        logger.log_system_event("Data forwarder started")

    def stop(self) -> None:
        """Stop broadcast thread"""
        self.running = False

        if self.broadcast_thread and self.broadcast_thread.is_alive():
            self.broadcast_thread.join(timeout=5)

        # Close all client connections
        with self.client_lock:
            for mount_clients in self.clients.values():
                for client_info in mount_clients[:]:
                    self._close_client(client_info)

        logger.log_system_event("Data forwarder stopped")

    def add_client(
        self,
        client_socket: socket.socket,
        user: str,
        mount: str,
        agent: str,
        addr: tuple[str, int],
        protocol_version: str,
        connection_id: str | None = None,
    ) -> dict[str, Any]:
        """Add client connection (synchronous)"""
        try:
            # Enable TCP Keep-Alive
            self._enable_keepalive(client_socket)

            client_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

            current_time = time.time()
            client_info = {
                "socket": client_socket,
                "user": user,
                "mount": mount,
                "agent": agent,
                "addr": addr,
                "protocol_version": protocol_version,
                "connection_id": connection_id,
                "connected_at": current_time,
                "last_seen": current_time,
                "last_sent_timestamp": current_time,
                "bytes_sent": 0,
                "messages_sent": 0,
                "send_errors": 0,
            }

            with self.client_lock:
                # Limit connections for same user on same mount point
                if mount not in self.clients:
                    self.clients[mount] = []

                user_connections = [c for c in self.clients[mount] if c["user"] == user]
                if len(user_connections) >= config.settings.ntrip.max_users_per_mount:
                    oldest = min(user_connections, key=lambda x: x["connected_at"])
                    self.remove_client(oldest)

                self.clients[mount].append(client_info)

                self.stats["total_clients"] += 1
                self.stats["active_clients"] = sum(len(clients) for clients in self.clients.values())

            logger.log_client_connect(user, mount, addr[0], protocol_version)
            return client_info

        except Exception as e:
            logger.log_error(f"Failed to add client: {e}", exc_info=True)
            try:
                client_socket.close()
            except Exception:
                pass
            raise

    def _enable_keepalive(self, client_socket: socket.socket) -> None:
        """TCP Keep-Alive settings"""
        try:
            if not config.settings.tcp.keepalive_enabled:
                return

            client_socket.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)

            try:
                if hasattr(socket, "TCP_KEEPIDLE"):
                    client_socket.setsockopt(
                        socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, config.settings.tcp.keepalive_idle
                    )
                if hasattr(socket, "TCP_KEEPINTVL"):
                    client_socket.setsockopt(
                        socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, config.settings.tcp.keepalive_interval
                    )
                if hasattr(socket, "TCP_KEEPCNT"):
                    client_socket.setsockopt(
                        socket.IPPROTO_TCP, socket.TCP_KEEPCNT, config.settings.tcp.keepalive_count
                    )
            except OSError:
                pass

            client_socket.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, config.settings.network.buffer_size)
            client_socket.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, config.settings.network.buffer_size)

        except Exception as e:
            logger.log_warning(f"Failed to set TCP Keep-Alive: {e}", "ntrip")

    def remove_client(self, client_info: dict[str, Any]) -> None:
        """Remove client connection"""
        try:
            self._close_client(client_info)

            with self.client_lock:
                mount = client_info["mount"]
                if mount in self.clients and client_info in self.clients[mount]:
                    self.clients[mount].remove(client_info)

                    if not self.clients[mount]:
                        del self.clients[mount]

                self.stats["active_clients"] = sum(len(clients) for clients in self.clients.values())
                self.stats["disconnected_clients"] += 1

            connection.remove_user_connection(
                client_info["user"], connection_id=client_info.get("connection_id"), mount_name=client_info["mount"]
            )

            logger.log_client_disconnect(client_info["user"], client_info["mount"], client_info["addr"][0])

        except Exception as e:
            logger.log_error(f"Failed to remove client: {e}", exc_info=True)

    def _close_client(self, client_info: dict[str, Any]) -> None:
        """Close client connection"""
        try:
            socket_obj = client_info["socket"]
            socket_obj.close()
        except Exception as e:
            logger.log_debug(f"Failed to close client connection: {e}", "ntrip")

    def upload_data(self, mount: str, data_chunk: bytes) -> None:
        """Upload data to a mount point"""
        timestamp = time.time()

        if mount not in self.mount_buffers:
            self.create_mount_buffer(mount)

        with self.buffer_lock:
            self.mount_buffers[mount].append(data_chunk, timestamp)

        self._send_to_subscribers(mount, data_chunk)

        try:
            connection.update_mount_data_stats(mount, len(data_chunk))
        except Exception as e:
            logger.log_error(f"Error updating stats for mount {mount}: {e}")

    def create_mount_buffer(self, mount: str) -> bool:
        """Create a buffer for a mount point"""
        with self.buffer_lock:
            if mount not in self.mount_buffers:
                self.mount_buffers[mount] = RingBuffer(self.buffer_maxlen)
                logger.log_mount_operation("buffer_created", mount)
                return True
            return False

    def remove_mount_buffer(self, mount: str) -> bool:
        """Remove a buffer for a mount point"""
        with self.buffer_lock:
            if mount in self.mount_buffers:
                del self.mount_buffers[mount]
                logger.log_mount_operation("buffer_removed", mount)
                return True
            return False

    def _broadcast_loop(self) -> None:
        """Broadcast loop"""
        logger.log_system_event("Data broadcast loop running")

        while self.running:
            try:
                self._broadcast_data()
                time.sleep(self.broadcast_interval)
            except Exception as e:
                logger.log_error(f"Broadcast loop exception: {e}", exc_info=True)
                time.sleep(1)

    def _broadcast_data(self) -> None:
        """Broadcast data to all clients"""
        with self.buffer_lock:
            mount_items = list(self.mount_buffers.items())

        for mount_name, buffer in mount_items:
            with self.client_lock:
                if mount_name in self.clients:
                    clients = self.clients[mount_name][:]
                    self._send_data_to_clients(clients, buffer, mount_name)

    def _send_data_to_clients(self, clients: list[dict[str, Any]], buffer: RingBuffer, mount_name: str) -> None:
        """Send data to client list"""
        disconnected_clients = []

        for client_info in clients:
            try:
                self._send_to_client(client_info, buffer)
            except Exception as e:
                logger.log_warning(f"Failed to send data to client ({client_info['addr']}): {e}", "ntrip")
                disconnected_clients.append(client_info)

        # Clean up disconnected clients
        for client_info in disconnected_clients:
            self.remove_client(client_info)

    def _send_to_client(self, client_info: dict[str, Any], buffer: RingBuffer) -> None:
        """Send data to a single client"""
        try:
            last_sent_timestamp = client_info["last_sent_timestamp"]
            new_data = buffer.get_since(last_sent_timestamp)

            if new_data:
                bytes_sent = self._send_data_simple(client_info, new_data)

                if bytes_sent > 0:
                    current_time = time.time()
                    client_info["last_seen"] = current_time
                    client_info["last_sent_timestamp"] = new_data[-1][0]
                    client_info["bytes_sent"] += bytes_sent
                    client_info["messages_sent"] += len(new_data)

                    self.stats["total_bytes_sent"] += bytes_sent
                    self.stats["total_messages_sent"] += len(new_data)
                    metrics.DATA_THROUGHPUT.labels(mount=client_info["mount"], direction="out").inc(bytes_sent)

                    if client_info.get("connection_id"):
                        connection.update_user_activity(client_info["user"], client_info["connection_id"], bytes_sent)

        except Exception as e:
            if "Connection" not in str(e) and "Broken pipe" not in str(e):
                logger.log_warning(f"Failed to send data to client ({client_info['addr']}): {e}", "ntrip")
            raise

    def _send_data_simple(self, client_info: dict[str, Any], data_list: list[tuple[float, bytes]]) -> int:
        """Simple data send method.

        NTRIP downloads are intentionally un-chunked: every client is served the
        raw RTCM octet-stream, which is what real-world NTRIP v2 clients tolerate
        and what this caster already serves successfully (the v2 200 OK response
        deliberately omits Transfer-Encoding: chunked). Do NOT reintroduce
        chunked encoding here without also adding that response header and a
        terminal 0-chunk on stream end (see issue #1's download-path note) — a
        chunk marker sent while the response claims "not chunked" corrupts the
        rover stream.
        """
        try:
            socket_obj = client_info["socket"]
            total_bytes_sent = 0

            for timestamp, data in data_list:
                socket_obj.sendall(data)
                total_bytes_sent += len(data)

            return total_bytes_sent

        except Exception:
            client_info["send_errors"] += 1
            self.stats["failed_sends"] += 1
            raise

    def get_stats(self) -> dict[str, Any]:
        """Get forwarder statistics"""
        with self.buffer_lock, self.client_lock:
            buffer_stats = {}
            for mount, buffer in self.mount_buffers.items():
                buffer_stats[mount] = buffer.get_stats()

            return {
                "forwarder": self.stats.copy(),
                "buffers": buffer_stats,
                "clients_by_mount": {mount: len(clients) for mount, clients in self.clients.items()},
            }

    def get_client_info(self, mount: str | None = None) -> list[dict[str, Any]] | dict[str, list[dict[str, Any]]]:
        """Get client information"""
        with self.client_lock:
            if mount:
                return self.clients.get(mount, [])
            else:
                return dict(self.clients)

    def force_disconnect_user(self, username: str) -> bool:
        """Force disconnect all connections for a user"""
        disconnected_count = 0
        clients_to_remove = []

        with self.client_lock:
            for mount_name, clients in self.clients.items():
                for client_info in clients[:]:
                    if client_info["user"] == username:
                        clients_to_remove.append(client_info)

        for client_info in clients_to_remove:
            try:
                self.remove_client(client_info)
                disconnected_count += 1
                logger.log_info(f"Force disconnected user {username} from mount {client_info['mount']}")
            except Exception as e:
                logger.log_error(f"Failed to force disconnect user {username}: {e}")

        logger.log_info(f"Force disconnect for user {username} complete, {disconnected_count} connections closed")
        return disconnected_count > 0

    def force_disconnect_mount(self, mount_name: str) -> bool:
        """Force disconnect all connections for a mount point"""
        disconnected_count = 0

        with self.client_lock:
            if mount_name in self.clients:
                clients_to_remove = self.clients[mount_name][:]

                for client_info in clients_to_remove:
                    try:
                        self.remove_client(client_info)
                        disconnected_count += 1
                        logger.log_info(f"Force disconnected user {client_info['user']} from mount {mount_name}")
                    except Exception as e:
                        logger.log_error(f"Failed to disconnect user from mount {mount_name}: {e}")

        try:
            self.remove_mount_buffer(mount_name)
            logger.log_info(f"Removed data buffer for mount {mount_name}")
        except Exception as e:
            logger.log_error(f"Failed to remove buffer for mount {mount_name}: {e}")
        logger.log_info(
            f"Force disconnect for mount {mount_name} complete, {disconnected_count} user connections closed"
        )
        return True

    def register_subscriber(self, mount_name: str, socket_write_end: Any) -> None:
        """Register data subscriber (for RTCM parsing etc.)"""
        with self.subscriber_lock:
            if mount_name not in self.subscribers:
                self.subscribers[mount_name] = []
            self.subscribers[mount_name].append(socket_write_end)
            logger.log_debug(f"Added subscriber for mount {mount_name}", "ntrip")

    def unregister_subscriber(self, mount_name: str, socket_write_end: Any) -> None:
        """Unregister data subscriber"""
        with self.subscriber_lock:
            if mount_name in self.subscribers:
                try:
                    self.subscribers[mount_name].remove(socket_write_end)
                    if not self.subscribers[mount_name]:
                        del self.subscribers[mount_name]
                    logger.log_debug(f"Removed subscriber for mount {mount_name}", "ntrip")
                except ValueError:
                    pass

    def _send_to_subscribers(self, mount_name: str, data_chunk: bytes) -> None:
        """Send data to subscribers"""
        with self.subscriber_lock:
            if mount_name in self.subscribers:
                subscribers_to_remove = []
                for i, subscriber in enumerate(self.subscribers[mount_name]):
                    try:
                        if hasattr(subscriber, "send"):
                            subscriber.send(data_chunk)
                        elif hasattr(subscriber, "write"):
                            subscriber.write(data_chunk)
                            subscriber.flush()
                        else:
                            raise AttributeError(f"Subscriber does not support send or write: {type(subscriber)}")
                    except Exception as e:
                        logger.log_error(
                            f"Failed to send data to subscriber #{i + 1} for mount {mount_name}: {e}", "ntrip"
                        )
                        subscribers_to_remove.append(subscriber)

                for subscriber in subscribers_to_remove:
                    try:
                        self.subscribers[mount_name].remove(subscriber)
                    except ValueError:
                        pass

                if not self.subscribers[mount_name]:
                    del self.subscribers[mount_name]


forwarder = SimpleDataForwarder()


def initialize() -> SimpleDataForwarder:
    """Initialize data forwarder"""
    logger.log_system_event("Data forwarder initialized")
    return forwarder


def get_forwarder() -> SimpleDataForwarder:
    """Get global forwarder instance"""
    return forwarder


def start_forwarder() -> None:
    """Start data forwarder"""
    forwarder.start()


def stop_forwarder() -> None:
    """Stop data forwarder"""
    forwarder.stop()


def add_client(
    client_socket: socket.socket,
    user: str,
    mount: str,
    agent: str,
    addr: tuple[str, int],
    protocol_version: str,
    connection_id: str | None = None,
) -> dict[str, Any]:
    """Add client"""
    try:
        return forwarder.add_client(client_socket, user, mount, agent, addr, protocol_version, connection_id)
    except Exception as e:
        logger.log_error(f"Timed out adding client: {e}", "ntrip")
        raise


def remove_client(client_info: dict[str, Any]) -> None:
    """Remove client"""
    return forwarder.remove_client(client_info)


def upload_data(mount: str, data_chunk: bytes) -> None:
    """Upload data"""
    return forwarder.upload_data(mount, data_chunk)


def create_mount_buffer(mount: str) -> bool:
    """Create mount buffer"""
    return forwarder.create_mount_buffer(mount)


def remove_mount_buffer(mount: str) -> bool:
    """Remove mount buffer"""
    return forwarder.remove_mount_buffer(mount)


def get_stats() -> dict[str, Any]:
    """Get stats"""
    return forwarder.get_stats()


def get_client_info(mount: str | None = None) -> list[dict[str, Any]] | dict[str, list[dict[str, Any]]]:
    """Get client info"""
    return forwarder.get_client_info(mount)


def force_disconnect_user(username: str) -> bool:
    """Force disconnect user"""
    return forwarder.force_disconnect_user(username)


def force_disconnect_mount(mount_name: str) -> bool:
    """Force disconnect mount"""
    return forwarder.force_disconnect_mount(mount_name)


def register_subscriber(mount_name: str, socket_write_end: Any) -> None:
    """Register subscriber"""
    return forwarder.register_subscriber(mount_name, socket_write_end)


def unregister_subscriber(mount_name: str, socket_write_end: Any) -> None:
    """Unregister subscriber"""
    return forwarder.unregister_subscriber(mount_name, socket_write_end)
