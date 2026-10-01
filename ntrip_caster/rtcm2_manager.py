"""
RTCM2 Parser Manager
"""

import threading
import time
from collections.abc import Callable
from typing import Any

from .logger import log_debug, log_error, log_info


class RTCM2ParserManager:
    """RTCM2 Parser Manager - Compatible with original parser_manager interface"""

    def __init__(self) -> None:
        self.parsers: dict[str, Any] = {}  # RTCMParserThread instances
        self.web_parsers: dict[str, Any] = {}  # Web parsing thread instances
        self.str_parsers: dict[str, Any] = {}  # STR correction thread instances
        self.current_web_mount: str | None = None  # Currently active Web parsing mount point
        self.lock = threading.RLock()
        log_info("RTCM2 data parsing manager initialized")

    def start_parser(
        self,
        mount_name: str,
        mode: str = "str_fix",
        duration: int = 30,
        push_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> Any | None:
        """Start a parser for the mount, replacing any running one; return it, or None on failure."""
        with self.lock:
            if mount_name in self.parsers:
                self.stop_parser(mount_name)

            try:
                from .rtcm2 import start_str_fix_parser, start_web_parser

                if mode == "str_fix":
                    parser = start_str_fix_parser(mount_name, duration, push_callback)
                    self.str_parsers[mount_name] = parser
                    log_info(f"Started RTCM parsing for STR correction [Mount: {mount_name}, Duration: {duration}s]")
                else:  # realtime_web
                    parser = start_web_parser(mount_name, push_callback)
                    self.web_parsers[mount_name] = parser
                    log_info(f"Started Web-side RTCM parsing [Mount: {mount_name}]")

                self.parsers[mount_name] = parser
                log_info(f"Started RTCM data parsing [Mount: {mount_name}, Mode: {mode}]")
                return parser
            except Exception as e:
                log_error(f"Failed to start RTCM parsing [Mount: {mount_name}]: {e!s}")
                return None

    def stop_parser(self, mount_name: str, parser: Any | None = None) -> None:
        """Stop the mount's parser. With ``parser``, only if it is still the mount's current one."""
        with self.lock:
            if parser is not None and self.parsers.get(mount_name) is not parser:
                return
            if mount_name in self.parsers:
                self.parsers[mount_name].stop()
                del self.parsers[mount_name]

                if mount_name in self.web_parsers:
                    del self.web_parsers[mount_name]
                    log_info(f"Web-side RTCM parsing closed for mount {mount_name}")
                elif mount_name in self.str_parsers:
                    del self.str_parsers[mount_name]
                    log_info(f"STR correction parsing closed for mount {mount_name}")
                else:
                    log_info(f"RTCM data parsing closed for mount {mount_name}")

    def get_result(self, mount_name: str) -> dict[str, Any] | None:
        """Get parsing result"""
        with self.lock:
            parser = self.parsers.get(mount_name)
            if parser:
                result = parser.result.copy()
                converted_result = self._convert_result_format(result)
                log_debug(f"Got parsing result [Mount: {mount_name}]: {converted_result is not None}")
                return converted_result

            log_debug(f"Parser not found [Mount: {mount_name}]")
            return None

    def _convert_result_format(self, result: dict[str, Any]) -> dict[str, Any]:
        """Convert result format to match expected interface"""
        converted = {
            "mount": result.get("mount"),
            "bitrate": result.get("bitrate", 0),
            "total_messages": sum(result.get("message_stats", {}).get("types", {}).values()),
            "last_update": time.time(),
        }

        location = result.get("location")
        if location:
            converted.update(
                {
                    "station_id": location.get("station_id"),
                    "lat": location.get("lat"),
                    "lon": location.get("lon"),
                    "country": location.get("country"),
                    "city": location.get("city"),
                }
            )

        device = result.get("device")
        if device:
            converted.update(
                {
                    "receiver": device.get("receiver"),
                    "antenna": device.get("antenna"),
                    "firmware": device.get("firmware"),
                }
            )

        msg_stats = result.get("message_stats", {})
        if msg_stats:
            gnss_set = msg_stats.get("gnss", set())
            converted["gnss_combined"] = "+".join(sorted(gnss_set)) if gnss_set else "N/A"

            carriers_set = msg_stats.get("carriers", set())
            converted["carrier_combined"] = "+".join(sorted(carriers_set)) if carriers_set else "N/A"

            frequency = msg_stats.get("frequency", {})
            if frequency:
                msg_types_list = [f"{msg_id}({freq})" for msg_id, freq in frequency.items()]
                converted["message_types_str"] = ",".join(msg_types_list)
            else:
                converted["message_types_str"] = "N/A"

        return converted

    def stop_all(self) -> None:
        """Stop all parsers"""
        with self.lock:
            for mount_name in list(self.parsers.keys()):
                self.stop_parser(mount_name)
            log_info("All parsers stopped")

    def acquire_parser(
        self, mount_name: str, push_callback: Callable[[dict[str, Any]], None] | None = None
    ) -> dict[str, Any] | None:
        """Acquire parser (Web mode)"""
        success = self.start_parser(mount_name, mode="realtime_web", push_callback=push_callback)
        if success:
            return self.get_result(mount_name)
        return None

    def release_parser(self, mount_name: str) -> None:
        """Release parser (Web mode)"""
        self.stop_parser(mount_name)

    def start_realtime_parsing(
        self, mount_name: str, push_callback: Callable[[dict[str, Any]], None] | None = None
    ) -> bool:
        """Start real-time parsing (Web mode) - Cleans up previous thread first"""
        with self.lock:
            if self.current_web_mount and self.current_web_mount != mount_name:
                log_info(f"Detected previous Web parsing thread [Mount: {self.current_web_mount}], cleaning up")
                self._stop_web_parser_only(self.current_web_mount)

            if mount_name in self.web_parsers:
                log_info(f"Mount point [Mount: {mount_name}] already has a Web parsing thread, stopping it first")
                self._stop_web_parser_only(mount_name)

            success = self.start_parser(mount_name, mode="realtime_web", push_callback=push_callback)
            if success:
                self.current_web_mount = mount_name
                log_info(f"Web parsing thread started successfully, current active mount: {mount_name}")

            return success

    def _stop_web_parser_only(self, mount_name: str) -> None:
        """Stop only the Web parsing thread for a mount, protecting STR correction threads"""
        if mount_name in self.web_parsers:
            parser = self.web_parsers[mount_name]
            parser.stop()
            del self.web_parsers[mount_name]
            if mount_name in self.parsers:
                del self.parsers[mount_name]
            if self.current_web_mount == mount_name:
                self.current_web_mount = None
            log_info(f"Stopped Web parsing thread [Mount: {mount_name}], STR correction thread unaffected")

    def stop_realtime_parsing(self) -> None:
        """Stop all real-time parsing (Web mode)"""
        with self.lock:
            web_mounts = list(self.web_parsers.keys())
            for mount_name in web_mounts:
                self._stop_web_parser_only(mount_name)
            self.current_web_mount = None
            if web_mounts:
                log_info(
                    f"Stopped all Web parsing threads [Mounts: {', '.join(web_mounts)}], "
                    f"STR correction threads continue"
                )
            else:
                log_info("No active Web parsing threads to stop")

    def update_parsing_heartbeat(self, mount_name: str) -> None:
        """Update parsing heartbeat (for future implementation)"""
        pass

    def get_parsed_mount_data(self, mount_name: str, _limit: int | None = None) -> dict[str, Any] | None:
        """Get parsed mount point data"""
        return self.get_result(mount_name)

    def get_mount_statistics(self, mount_name: str) -> dict[str, Any] | None:
        """Get mount point parsing statistics"""
        result = self.get_result(mount_name)
        if result:
            return {
                "bitrate": result.get("bitrate", 0),
                "total_messages": result.get("total_messages", 0),
                "last_update": result.get("last_update"),
            }
        return None

    def get_parser_status(self) -> dict[str, Any]:
        """Get parser status information"""
        with self.lock:
            return {
                "total_parsers": len(self.parsers),
                "web_parsers": len(self.web_parsers),
                "str_parsers": len(self.str_parsers),
                "current_web_mount": self.current_web_mount,
                "web_mounts": list(self.web_parsers.keys()),
                "str_mounts": list(self.str_parsers.keys()),
            }

    def is_web_parsing_active(self, mount_name: str) -> bool:
        """Check if Web parsing is active for a mount point"""
        with self.lock:
            return mount_name in self.web_parsers

    def is_str_parsing_active(self, mount_name: str) -> bool:
        """Check if STR correction parsing is active for a mount point"""
        with self.lock:
            return mount_name in self.str_parsers

    def get_current_web_mount(self) -> str | None:
        """Get currently active Web parsing mount point"""
        return self.current_web_mount


parser_manager = RTCM2ParserManager()
