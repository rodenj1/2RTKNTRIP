#!/usr/bin/env python3

import re
import subprocess
import threading
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from threading import Lock, RLock
from typing import Any

from . import config
from .logger import log_debug, log_error, log_info, log_warning
from .rtcm2_manager import parser_manager as rtcm_manager


class MountRelease(Enum):
    """What happened when a session released its mount."""

    RELEASED = "released"  # this session still held the mount; it is now offline
    NOT_ONLINE = "not_online"  # the mount was already gone (zombie cleanup, admin delete, ...)
    HELD_BY_ANOTHER_SESSION = "held_by_another_session"  # the source reconnected; the new session keeps it


@dataclass
class MountInfo:
    """Mount point information data class"""

    mount_name: str
    ip_address: str = ""
    user_agent: str = ""
    protocol_version: str = "1.0"
    connect_time: float = field(default_factory=time.time)
    connect_datetime: str = field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    last_update: float = field(default_factory=time.time)

    # Socket reference for force closing connections
    client_socket: Any | None = None

    # Station information
    station_id: int | None = None
    lat: float | None = None
    lon: float | None = None
    height: float | None = None

    # Geographic information
    country: str | None = None  # Country code (e.g., CHN)
    city: str | None = None  # City name (e.g., Beijing)

    # Data statistics
    total_bytes: int = 0
    total_messages: int = 0
    data_rate: float = 0.0
    data_count: int = 0
    last_data_time: float | None = None

    # Status information
    status: str = "online"  # 'online', 'offline'

    # STR table information
    str_data: str = ""
    initial_str_generated: bool = False
    final_str_generated: bool = False

    custom_info: dict[str, Any] = field(default_factory=dict)

    @property
    def uptime(self) -> float:
        """Uptime (seconds)"""
        return time.time() - self.connect_time

    @property
    def idle_time(self) -> float:
        """Idle time (seconds)"""
        if self.last_data_time:
            return time.time() - self.last_data_time
        return self.uptime

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization"""
        return {
            "mount_name": self.mount_name,
            "ip_address": self.ip_address,
            "user_agent": self.user_agent,
            "protocol_version": self.protocol_version,
            "connect_time": self.connect_time,
            "connect_datetime": self.connect_datetime,
            "last_update": self.last_update,
            "station_id": self.station_id,
            "lat": self.lat,
            "lon": self.lon,
            "height": self.height,
            "country": self.country,
            "city": self.city,
            "total_bytes": self.total_bytes,
            "total_messages": self.total_messages,
            "data_rate": self.data_rate,
            "data_count": self.data_count,
            "last_data_time": self.last_data_time,
            "status": self.status,
            "str_data": self.str_data,
            "initial_str_generated": self.initial_str_generated,
            "final_str_generated": self.final_str_generated,
            "custom_info": self.custom_info,
        }


class ConnectionManager:
    """Connection and Mount Point Manager - Unifies management of online mount points, user connections, and STR tables"""

    def __init__(self) -> None:
        # Online mount point table: {mount_name: MountInfo}
        self.online_mounts: dict[str, MountInfo] = {}
        # Online user table: {user_id: [connection_info, ...]}
        self.online_users: dict[str, list[dict[str, Any]]] = defaultdict(list)
        # User connection count: {username: count}
        self.user_connection_count: dict[str, int] = defaultdict(int)
        # Mount point connection count: {mount_name: count}
        self.mount_connection_count: dict[str, int] = defaultdict(int)
        # Statistics
        self.total_connections: int = 0
        self.rejected_connections: int = 0
        self.clients: dict[str, Any] = {}  # Active clients

        self.mount_lock = RLock()
        self.user_lock = RLock()

    def print_active_connections(self) -> None:
        """Print current active NTRIP connection information"""
        with self.mount_lock:
            pass

    def force_refresh_connections(self) -> None:
        """Force refresh connection status and print details"""
        invalid_mounts = []
        for mount_name, mount_info in self.online_mounts.items():
            idle_time = mount_info.idle_time
            if idle_time > 60:  # No data for more than 60 seconds
                invalid_mounts.append(mount_name)
        self.print_active_connections()

    def cleanup_zombie_connections(self) -> None:
        """Clean up zombie connections - Check system level socket status"""
        try:
            # Get system level socket connection status
            result = subprocess.run(["netstat", "-an"], capture_output=True, text=True, shell=True, check=False)
            if result.returncode != 0:
                log_warning("Unable to get system socket status")
                return

            # Parse ESTABLISHED connections
            established_ips = set()
            ntrip_port_str = f":{config.settings.ntrip.port}"
            for line in result.stdout.split("\n"):
                if ntrip_port_str in line and "ESTABLISHED" in line:
                    # Extract remote IP address
                    match = re.search(r"(\d+\.\d+\.\d+\.\d+):(\d+)\s+ESTABLISHED", line)
                    if match:
                        remote_ip = match.group(1)
                        established_ips.add(remote_ip)

            # Check application layer connection status
            with self.mount_lock:
                zombie_mounts = []
                for mount_name, mount_info in self.online_mounts.items():
                    if mount_info.ip_address not in established_ips:
                        zombie_mounts.append(mount_name)
                        log_warning(f"Zombie connection detected: Mount {mount_name}, IP {mount_info.ip_address}")

                # Cleanup zombie connections
                for mount_name in zombie_mounts:
                    log_info(f"Cleaning up zombie connection: {mount_name}")
                    self.remove_mount_connection(mount_name, "Zombie connection cleanup")

                if zombie_mounts:
                    log_info(f"Cleaned up {len(zombie_mounts)} zombie connections")
                else:
                    log_debug("No zombie connections found")

        except Exception as e:
            log_error(f"Exception occurred while cleaning up zombie connections: {e}", exc_info=True)

    def add_mount_connection(
        self,
        mount_name: str,
        ip_address: str,
        user_agent: str = "",
        protocol_version: str = "1.0",
        client_socket: Any | None = None,
    ) -> tuple[bool, str]:
        """Add mount point connection (source side)"""
        with self.mount_lock:
            if mount_name in self.online_mounts:
                log_debug(
                    f"Mount point {mount_name} is still in thread table, might be a cleanup of duplicate connection from same IP"
                )
                del self.online_mounts[mount_name]

            log_debug(
                f"Creating mount point connection - Name: {mount_name}, IP: {ip_address}, User-Agent: {user_agent}, Protocol: {protocol_version}"
            )

            # Create mount info
            mount_info = MountInfo(
                mount_name=mount_name,
                ip_address=ip_address,
                user_agent=user_agent,
                protocol_version=protocol_version,
                client_socket=client_socket,
            )

            # Add to online mounts table
            self.online_mounts[mount_name] = mount_info
            log_debug(
                f"Mount point {mount_name} added to online list, current online mount count: {len(self.online_mounts)}"
            )

            # Generate initial STR table
            self._generate_initial_str(mount_name)

            # Start STR correction parsing process
            self.start_str_correction(mount_name)

            log_info(
                f"Mount point {mount_name} is online, IP: {ip_address}, Current online mounts: {len(self.online_mounts)}"
            )
            log_debug(
                f"Mount point {mount_name} connected successfully, initial status: {mount_info.status}, connection time: {mount_info.connect_datetime}"
            )

            self.print_active_connections()

            return True, "Mount point connected successfully"

    def release_mount(self, mount_name: str, client_socket: Any) -> MountRelease:
        """Release a mount on behalf of the session that owns ``client_socket``.

        The mount is removed only if that session is still the one registered for
        it. A session whose source has since reconnected under the same mount must
        not tear the newer session down.
        """
        with self.mount_lock:
            mount_info = self.online_mounts.get(mount_name)
            if mount_info is None:
                return MountRelease.NOT_ONLINE
            if mount_info.client_socket is not client_socket:
                return MountRelease.HELD_BY_ANOTHER_SESSION
            self.remove_mount_connection(mount_name)
            return MountRelease.RELEASED

    def remove_mount_connection(self, mount_name: str, reason: str = "Active disconnect") -> bool:
        """Remove mount point connection (source side disconnected)"""
        with self.mount_lock:
            if mount_name in self.online_mounts:
                mount_info = self.online_mounts[mount_name]

                # Force close socket
                if mount_info.client_socket:
                    try:
                        mount_info.client_socket.close()
                        log_info(f"Force closed socket connection for mount {mount_name}")
                    except Exception as e:
                        log_warning(f"Failed to close socket for mount {mount_name}: {e}")

                # Log disconnection info
                log_debug(
                    f"Mount point {mount_name} disconnected. Details: {reason}, Status: {mount_info.status}, Total bytes: {mount_info.total_bytes}, Data rate: {mount_info.data_rate:.2f} B/s"
                )
                log_debug(
                    f"Mount point {mount_name} stats - Total messages: {mount_info.total_messages}, Packets: {mount_info.data_count}, Idle time: {mount_info.idle_time:.1f}s"
                )

                if mount_info.status == "online":
                    actual_reason = reason if reason != "Active disconnect" else "Normal disconnect"
                else:
                    actual_reason = "Abnormal offline"

                del self.online_mounts[mount_name]

                log_info(
                    f"Mount point {mount_name} is offline, uptime: {mount_info.uptime:.1f}s, Reason: {actual_reason}"
                )
                log_debug(
                    f"Mount point {mount_name} removal complete, remaining online mounts: {len(self.online_mounts)}"
                )
                self.print_active_connections()

                return True
            else:
                log_debug(f"Attempted to remove non-existent mount point: {mount_name}")
                return False

    def _generate_initial_str(self, mount_name: str) -> None:
        """Generate initial STR table"""
        parse_result: dict[str, Any] = {}
        self._process_str_data(mount_name, parse_result, mode="initial")

    def _update_message_statistics(self, mount_name: str, parsed_messages: Any, data_size: int) -> bool:
        """Update basic mount point statistics"""
        if mount_name not in self.online_mounts:
            log_debug(f"Stats update failed - Mount point {mount_name} is not online")
            return False

        mount_info = self.online_mounts[mount_name]
        current_time = time.time()

        with self.mount_lock:
            mount_info.last_update = current_time
            mount_info.last_data_time = current_time
            mount_info.total_bytes += data_size
            mount_info.data_count += 1

            uptime = mount_info.uptime
            if uptime > 0:
                mount_info.data_rate = mount_info.total_bytes / uptime

        return True

    def update_mount_data(self, mount_name: str, data_size: int) -> bool:
        """Update mount point data statistics"""
        if mount_name not in self.online_mounts:
            return False

        return self._update_message_statistics(mount_name, None, data_size)

    def get_mount_str_data(self, mount_name: str) -> str | None:
        """Get STR table data for a mount point"""
        if mount_name in self.online_mounts:
            return self.online_mounts[mount_name].str_data
        return None

    def get_all_str_data(self) -> dict[str, str]:
        """Get STR table data for all mount points"""
        str_data = {}
        with self.mount_lock:
            for mount_name, mount_info in self.online_mounts.items():
                if mount_info.str_data:
                    str_data[mount_name] = mount_info.str_data
        return str_data

    def add_user_connection(
        self,
        username: str,
        mount_name: str,
        ip_address: str,
        user_agent: str = "",
        protocol_version: str = "1.0",
        client_socket: Any | None = None,
    ) -> str:
        """Add user connection"""
        with self.user_lock:
            connection_id = f"{username}_{mount_name}_{uuid.uuid4().hex[:12]}"

            socket_info = (
                "No socket"
                if client_socket is None
                else f"Port:{getattr(client_socket, 'getpeername', lambda: ('Unknown', 'Unknown'))()[1] if hasattr(client_socket, 'getpeername') else 'Unknown'}"
            )
            log_debug(
                f"Creating user connection - User: {username}, Mount: {mount_name}, IP: {ip_address}, {socket_info}, User-Agent: {user_agent}"
            )

            connection_info = {
                "connection_id": connection_id,
                "username": username,
                "mount_name": mount_name,
                "ip_address": ip_address,
                "user_agent": user_agent,
                "protocol_version": protocol_version,
                "connect_time": time.time(),
                "connect_datetime": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "last_activity": time.time(),
                "bytes_sent": 0,
                "client_socket": client_socket,
            }

            if username not in self.online_users:
                self.online_users[username] = []
                log_debug(f"Created connection list for new user {username}")
            if username not in self.user_connection_count:
                self.user_connection_count[username] = 0
            if mount_name not in self.mount_connection_count:
                self.mount_connection_count[mount_name] = 0

            old_user_count = self.user_connection_count[username]
            old_mount_count = self.mount_connection_count[mount_name]

            self.online_users[username].append(connection_info)
            self.user_connection_count[username] += 1
            self.mount_connection_count[mount_name] += 1

            log_info(f"User {username} IP: {ip_address} connected, subscribing to RTCM data from mount {mount_name}")
            log_debug(
                f"User connection count updated - User {username}: {old_user_count} -> {self.user_connection_count[username]}, Mount {mount_name}: {old_mount_count} -> {self.mount_connection_count[mount_name]}"
            )
            log_debug(f"Connection ID generated: {connection_id}, Total online users: {len(self.online_users)}")
            return connection_id

    def remove_user_connection(
        self, username: str, connection_id: str | None = None, mount_name: str | None = None
    ) -> bool:
        """Remove user connection"""
        with self.user_lock:
            if username not in self.online_users:
                return False

            connections_to_remove = []

            for i, conn in enumerate(self.online_users[username]):
                should_remove = False

                # A connection ID names exactly one connection; mount_name only
                # applies when no ID is given (e.g. an admin dropping a user's mount).
                if connection_id:
                    should_remove = conn["connection_id"] == connection_id
                elif mount_name:
                    should_remove = conn["mount_name"] == mount_name
                else:
                    should_remove = True

                if should_remove:
                    connections_to_remove.append(i)
                    self.mount_connection_count[conn["mount_name"]] -= 1

                    if conn.get("client_socket"):
                        try:
                            conn["client_socket"].close()
                        except:
                            pass

                    log_info(f"User {username} disconnected from mount {conn['mount_name']}")

            for i in reversed(connections_to_remove):
                del self.online_users[username][i]
                self.user_connection_count[username] -= 1

            if not self.online_users[username]:
                del self.online_users[username]
                del self.user_connection_count[username]

            return len(connections_to_remove) > 0

    def update_mount_data_stats(self, mount_name: str, data_size: int) -> None:
        """Update mount point data statistics"""
        if mount_name in self.online_mounts:
            mount_info = self.online_mounts[mount_name]
            mount_info.data_count += 1
            mount_info.last_data_time = time.time()
            mount_info.total_bytes += data_size
            uptime = mount_info.uptime
            if uptime > 0:
                mount_info.data_rate = mount_info.total_bytes / uptime

    def update_user_activity(self, username: str, connection_id: str, bytes_sent: int = 0) -> bool:
        """Update user activity status"""
        with self.user_lock:
            if username not in self.online_users:
                log_debug(f"User activity update failed - User {username} is not online")
                return False

            connection_found = False
            for conn in self.online_users[username]:
                if conn["connection_id"] == connection_id:
                    conn["last_activity"] = time.time()
                    conn["bytes_sent"] += bytes_sent
                    connection_found = True
                    break

            if not connection_found:
                log_debug(f"User activity update failed - Connection ID {connection_id} not found for user {username}")
                return False

            return True

    def is_mount_online(self, mount_name: str) -> bool:
        """Check if a mount point is online"""
        with self.mount_lock:
            return mount_name in self.online_mounts

    def get_user_connection_count(self, username: str) -> int:
        """Get connection count for a user"""
        return self.user_connection_count.get(username, 0)

    def get_user_connect_time(self, username: str) -> str | None:
        """Get latest connection time for a user"""
        with self.user_lock:
            if self.online_users.get(username):
                latest_connection = max(self.online_users[username], key=lambda x: float(x["connect_time"]))
                return str(latest_connection["connect_datetime"])
            return None

    def get_mount_connection_count(self, mount_name: str) -> int:
        """Get connection count for a mount point"""
        return self.mount_connection_count.get(mount_name, 0)

    def get_online_mounts(self) -> dict[str, dict[str, Any]]:
        """Get list of online mount points"""
        with self.mount_lock:
            return {name: info.to_dict() for name, info in self.online_mounts.items()}

    def get_online_users(self) -> dict[str, list[dict[str, Any]]]:
        """Get list of online users"""
        with self.user_lock:
            return dict(self.online_users)

    def get_mount_info(self, mount_name: str) -> dict[str, Any] | None:
        """Get mount point information"""
        if mount_name in self.online_mounts:
            return self.online_mounts[mount_name].to_dict()
        return None

    def get_user_connections(self, username: str) -> list[dict[str, Any]]:
        """Get user connection information"""
        return self.online_users.get(username, [])

    def get_mount_statistics(self, mount_name: str) -> dict[str, Any] | None:
        """Get mount point statistics"""
        if mount_name not in self.online_mounts:
            return None

        mount_info = self.online_mounts[mount_name]
        return {
            "mount_name": mount_name,
            "status": mount_info.status,
            "uptime": mount_info.uptime,
            "total_bytes": mount_info.total_bytes,
            "data_rate": mount_info.data_rate,
            "data_count": mount_info.data_count,
        }

    def generate_mount_list(self) -> list[str]:
        """Generate mount point list data"""
        mount_list: list[str] = []

        with self.mount_lock:
            for mount_name, mount_info in self.online_mounts.items():
                if mount_info.str_data:
                    mount_list.append(mount_info.str_data)
                else:
                    # Generate default NTRIP format info
                    mount_data = [
                        "STR",
                        mount_name,  # Mount name
                        "none",  # Identifier
                        "RTCM 3.3",  # Format
                        "1005(10)",  # Format details
                        "0",  # Carrier
                        "GPS",  # Nav system
                        config.settings.app.name,  # Network
                        config.settings.caster.country,  # Country
                        str(mount_info.lat) if mount_info.lat is not None else "0.0",  # Latitude
                        str(mount_info.lon) if mount_info.lon is not None else "0.0",  # Longitude
                        "0",  # NMEA
                        "0",  # Solution
                        f"{config.settings.app.author}_Caster",  # Generator
                        "N",  # Compression
                        "B",  # Authentication
                        "N",  # Fee
                        "500",  # Bitrate
                        "NO",  # Misc
                    ]
                    mount_info_str = ";".join(mount_data)
                    mount_list.append(mount_info_str)
                    log_info(f"Created STR for mount {mount_name}: {mount_info_str}", "connection_manager")

        return mount_list

    def get_statistics(self) -> dict[str, Any]:
        """Get overall statistics"""
        with self.mount_lock, self.user_lock:
            total_mounts = len(self.online_mounts)
            total_users = sum(len(connections) for connections in self.online_users.values())

            mount_stats: list[dict[str, Any]] = []
            for mount_name, mount_info in self.online_mounts.items():
                mount_stats.append(
                    {
                        "mount_name": mount_name,
                        "ip_address": mount_info.ip_address,
                        "uptime": mount_info.uptime,
                        "data_count": mount_info.data_count,
                        "total_bytes": mount_info.total_bytes,
                        "total_messages": mount_info.total_messages,
                        "data_rate": mount_info.data_rate,
                        "user_count": self.mount_connection_count.get(mount_name, 0),
                        "status": mount_info.status,
                        "str_generated": mount_info.final_str_generated,
                    }
                )

            user_stats: list[dict[str, Any]] = []
            for username, connections in self.online_users.items():
                for conn in connections:
                    user_stats.append(
                        {
                            "username": username,
                            "mount_name": conn["mount_name"],
                            "ip_address": conn["ip_address"],
                            "connect_time": conn["connect_time"],
                            "bytes_sent": conn["bytes_sent"],
                        }
                    )

            return {
                "total_mounts": total_mounts,
                "total_users": total_users,
                "mounts": mount_stats,
                "users": user_stats,
            }

    def start_str_correction(self, mount_name: str) -> None:
        """Start RTCM parsing to correct STR table"""
        if mount_name not in self.online_mounts:
            log_warning(f"Cannot start STR correction, mount point {mount_name} is not online")
            return

        success = rtcm_manager.start_parser(
            mount_name=mount_name, mode="str_fix", duration=config.settings.rtcm.parse_duration
        )

        if not success:
            log_error(f"Failed to start STR correction parsing for mount {mount_name}")
            return

        log_info(
            f"STR correction parsing started for mount {mount_name}, will correct STR table in {config.settings.rtcm.parse_duration} seconds"
        )

        def wait_and_correct() -> None:
            log_debug(f"Waiting for STR correction complete for mount {mount_name}")
            time.sleep(config.settings.rtcm.parse_duration + 5)
            log_debug(f"Wait complete, getting parsing results for mount {mount_name}")

            parse_result = rtcm_manager.get_result(mount_name)
            log_debug(f"Got parsing results for mount {mount_name}: {parse_result is not None}")

            if parse_result:
                log_debug(f"Parsing results for mount {mount_name}: {parse_result}")
                self._process_str_data(mount_name, parse_result, mode="correct")
            else:
                log_warning(f"No STR correction results obtained for mount {mount_name}")
                log_debug(
                    f"STR correction failed - Mount: {mount_name}. Possible reasons: Timeout, insufficient data, or parser error."
                )

            log_debug(f"Stopping parser for mount {mount_name}")
            rtcm_manager.stop_parser(mount_name)
            log_debug(f"STR correction process completed for mount {mount_name}")

        threading.Thread(target=wait_and_correct, daemon=True).start()

    def _process_str_data(self, mount_name: str, parse_result: dict[str, Any], mode: str = "correct") -> None:
        """Unified STR processing function: supports initial generation, correction, and regeneration modes"""
        log_debug(f"Starting STR processing [Mount: {mount_name}, Mode: {mode}]")
        log_debug(f"Parsing details: {parse_result}")

        with self.mount_lock:
            if mount_name not in self.online_mounts:
                log_debug(f"Mount point {mount_name} is not online, cannot process STR")
                return

            mount_info = self.online_mounts[mount_name]
            original_str = mount_info.str_data

            if mode == "initial":
                str_parts = self._create_initial_str_parts(mount_name, parse_result)
            elif mode in ["correct", "regenerate"]:
                if not original_str:
                    log_warning(
                        f"Mount point {mount_name} has no initial STR data, switching to initial generation mode"
                    )
                    str_parts = self._create_initial_str_parts(mount_name, parse_result)
                else:
                    log_debug(f"Original STR [Mount: {mount_name}]: {original_str}")
                    str_parts = original_str.split(";")
                    if len(str_parts) < 19:
                        log_error(
                            f"STR format error, cannot process [Mount: {mount_name}] - Field count: {len(str_parts)}, expected: 19"
                        )
                        return

                    self._update_str_fields(str_parts, parse_result, mode)
            else:
                log_error(f"Unknown STR processing mode: {mode}")
                return

            processed_str = ";".join(str_parts)
            log_debug(f"Processed STR [Mount: {mount_name}]: {processed_str}")

            mount_info.str_data = processed_str
            if mode == "initial":
                mount_info.initial_str_generated = True
            else:
                mount_info.final_str_generated = True

            if mode == "correct":
                if original_str != processed_str:
                    log_info(f"STR corrected for mount {mount_name}: {processed_str}")
                else:
                    log_info(f"STR table correction complete for mount {mount_name}, no update needed")
                    log_info(f"Current STR: {processed_str}")
            elif mode == "initial":
                log_info(f"STR generated for mount {mount_name}: {processed_str}")

            log_debug(
                f"STR processing finished for mount {mount_name}, Mode: {mode}, Final state: final_str_generated={mount_info.final_str_generated}"
            )

    def _create_initial_str_parts(self, mount_name: str, parse_result: dict[str, Any]) -> list[str]:
        """Create initial STR field list"""
        mount_info = self.online_mounts[mount_name]
        app_author = config.settings.app.author.replace(" ", "") if config.settings.app.author else "2rtk"

        identifier = parse_result.get("city") or mount_info.city or "none"
        country_code = parse_result.get("country") or mount_info.country or config.settings.caster.country
        latitude = parse_result.get("lat") or config.settings.caster.latitude
        longitude = parse_result.get("lon") or config.settings.caster.longitude

        str_parts = [
            "STR",  # 0: type
            mount_name,  # 1: mountpoint
            identifier,  # 2: identifier
            "RTCM3.x",  # 3: format
            parse_result.get("message_types_str", "1005"),  # 4: format-details
            "0",  # 5: carrier
            parse_result.get("gnss_combined", "GPS"),  # 6: nav-system
            app_author,  # 7: network
            country_code,  # 8: country
            f"{latitude:.4f}",  # 9: latitude
            f"{longitude:.4f}",  # 10: longitude
            "0",  # 11: nmea
            "0",  # 12: solution
            f"{config.settings.app.author}_Caster",  # 13: generator
            "N",  # 14: compression
            "B",  # 15: authentication
            "N",  # 16: fee
            "500",  # 17: bitrate
            "NO",  # 18: misc
        ]

        self._update_str_fields(str_parts, parse_result, "initial")

        return str_parts

    def _update_str_fields(self, str_parts: list[str], parse_result: dict[str, Any], mode: str = "correct") -> None:
        """Update STR fields based on parsing results"""

        if parse_result.get("city"):
            str_parts[2] = parse_result["city"]

        if parse_result.get("message_types_str"):
            str_parts[4] = parse_result["message_types_str"]

        if parse_result.get("carrier_combined"):
            carrier_info = parse_result["carrier_combined"]
            str_parts[5] = carrier_info

        if parse_result.get("gnss_combined"):
            str_parts[6] = parse_result["gnss_combined"]

        if parse_result.get("country"):
            str_parts[8] = parse_result["country"]

        if parse_result.get("lat"):
            str_parts[9] = f"{parse_result['lat']:.4f}"

        if parse_result.get("lon"):
            str_parts[10] = f"{parse_result['lon']:.4f}"

        str_parts[13] = f"{config.settings.app.author}_Caster"
        str_parts[16] = "N"

        if parse_result.get("bitrate"):
            bitrate_bps = parse_result["bitrate"]
            str_parts[17] = str(int(bitrate_bps))

        if mode == "initial":
            str_parts[-1] = "NO"
        else:
            str_parts[-1] = "YES"

    def check_mount_exists(self, mount_name: str) -> bool:
        return mount_name in self.online_mounts


_connection_manager: ConnectionManager | None = None
_manager_lock = Lock()


def get_connection_manager() -> ConnectionManager:
    """Get global connection manager instance"""
    global _connection_manager
    if _connection_manager is None:
        with _manager_lock:
            if _connection_manager is None:
                _connection_manager = ConnectionManager()
    return _connection_manager


def add_mount_connection(
    mount_name: str, ip_address: str, user_agent: str = "", protocol_version: str = "1.0"
) -> tuple[bool, str]:
    """Add mount point connection"""
    return get_connection_manager().add_mount_connection(mount_name, ip_address, user_agent, protocol_version)


def remove_mount_connection(mount_name: str) -> bool:
    """Remove mount point connection"""
    return get_connection_manager().remove_mount_connection(mount_name)


def add_user_connection(
    username: str,
    mount_name: str,
    ip_address: str,
    user_agent: str = "",
    protocol_version: str = "1.0",
    client_socket: Any | None = None,
) -> str:
    """Add user connection"""
    return get_connection_manager().add_user_connection(
        username, mount_name, ip_address, user_agent, protocol_version, client_socket
    )


def remove_user_connection(username: str, connection_id: str | None = None, mount_name: str | None = None) -> bool:
    """Remove user connection"""
    return get_connection_manager().remove_user_connection(username, connection_id, mount_name)


def update_user_activity(username: str, connection_id: str, bytes_sent: int = 0) -> bool:
    """Update user activity"""
    return get_connection_manager().update_user_activity(username, connection_id, bytes_sent)


def is_mount_online(mount_name: str) -> bool:
    """Check if a mount point is online"""
    return get_connection_manager().is_mount_online(mount_name)


def get_user_connection_count(username: str) -> int:
    """Get connection count for a user"""
    return get_connection_manager().get_user_connection_count(username)


def update_mount_data(mount_name: str, data_size: int) -> bool:
    """Update mount point data"""
    return get_connection_manager().update_mount_data(mount_name, data_size)


def update_mount_data_stats(mount_name: str, data_size: int) -> None:
    """Update mount point data statistics"""
    return get_connection_manager().update_mount_data_stats(mount_name, data_size)


def get_statistics() -> dict[str, Any]:
    """Get statistics"""
    return get_connection_manager().get_statistics()


def get_mount_statistics(mount_name: str) -> dict[str, Any] | None:
    """Get mount point statistics"""
    return get_connection_manager().get_mount_statistics(mount_name)


def generate_mount_list() -> list[str]:
    """Generate mount list data"""
    return get_connection_manager().generate_mount_list()


def check_mount_exists(mount_name: str) -> bool:
    """Check if mount point exists"""
    return get_connection_manager().check_mount_exists(mount_name)
