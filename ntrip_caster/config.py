#!/usr/bin/env python3
"""
config.py - Configuration module
Reads configuration parameters from JSON, YAML or INI file using Pydantic
"""

import configparser
import json
import os
import socket
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# ==================== Models ====================


# ==================== Basic Configuration ====================
class AppConfig(BaseModel):
    """Basic application information"""

    name: str = "2RTK Ntrip Caster"
    version: str = "2.2.0"
    description: str = "Ntrip Caster"
    author: str = "2rtk"
    contact: str = "i@jia.by"
    website: str = "https://2rtk.com"


class DevelopmentConfig(BaseModel):
    debug_mode: bool = False


# ==================== CASTER Configuration ====================
class CasterConfig(BaseModel):
    """NTRIP Caster geographic information"""

    country: str = "CHN"
    latitude: float = 25.20341154
    longitude: float = 110.277492


# ==================== Network Configuration ====================
class NetworkConfig(BaseModel):
    """Network settings"""

    host: str = "0.0.0.0"
    max_connections: int = 5000  # Maximum connections
    buffer_size: int = 81920  # Buffer size (80KB)
    max_buffer_size: int = 655360  # Maximum buffer size (640KB)


# ==================== NTRIP Protocol Configuration ====================
class NtripConfig(BaseModel):
    port: int = Field(default=2101, ge=1024, le=65535)  # NTRIP service port
    supported_versions: list[str] = ["1.0", "2.0"]
    default_version: str = "1.0"
    max_user_connections_per_mount: int = 3000
    max_users_per_mount: int = 3000
    max_connections_per_user: int = 3
    mount_timeout: int = 1800  # 30 minutes
    client_timeout: int = 300  # 5 minutes
    connection_timeout: int = 1800  # Connection timeout (seconds)


# ==================== Web Interface Configuration ====================
class WebConfig(BaseModel):
    port: int = Field(default=5757, ge=1024, le=65535)  # Web service port
    realtime_push_interval: int = 3  # Real-time data push interval (seconds)
    page_refresh_interval: int = 30


# ==================== Database Configuration ====================
class DatabaseConfig(BaseModel):
    url: str = "sqlite:///2rtk.db"
    path: str = "2rtk.db"  # Legacy support
    pool_size: int = 10
    timeout: int = 30

    @property
    def connection_url(self) -> str:
        """Get the database connection URL, prioritizing 'url' then 'path'"""
        if self.url and not self.url.startswith("sqlite:///2rtk.db"):
            return self.url
        if self.path:
            return f"sqlite:///{self.path}"
        return self.url


# ==================== Logging Configuration ====================
class LoggingConfig(BaseModel):
    log_dir: str = "logs"
    main_log_file: str = "main.log"
    ntrip_log_file: str = "ntrip.log"
    error_log_file: str = "errors.log"
    log_level: str = "WARNING"
    log_format: str = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    max_log_size: int = 10 * 1024 * 1024  # 10MB
    backup_count: int = 5  # Keep 5 backup files
    log_frequent_status: bool = False


class OIDCConfig(BaseModel):
    """OIDC Configuration"""

    enabled: bool = False
    issuer: str = ""
    client_id: str = ""
    client_secret: str = ""
    redirect_uri: str = ""
    scope: str = "openid profile email"


class SecurityConfig(BaseModel):
    """Password hash configuration and Flask Secret Key"""

    secret_key: str = "8f4a9c2e7d1b6f3a5e8d7c9b2a4f6e3d5c8b7a9f2e4d6c8b3a5f7e9d1c2b4a6"
    password_hash_rounds: int = 3
    session_timeout: int = 3600  # 1 hour
    oidc: OIDCConfig = OIDCConfig()


class AdminConfig(BaseModel):
    """Default administrator account"""

    username: str = "admin"
    password: str = "admin123"


# ==================== TCP Configuration ====================
class TcpConfig(BaseModel):
    """TCP Keep-Alive Configuration"""

    keepalive_enabled: bool = True
    keepalive_idle: int = 60
    keepalive_interval: int = 10
    keepalive_count: int = 3
    socket_timeout: int = 120


# ==================== Data Forwarding Configuration ====================
class DataForwardingConfig(BaseModel):
    ring_buffer_size: int = 60  # Ring buffer configuration
    broadcast_interval: float = 0.01
    data_send_timeout: int = 5
    client_health_check_interval: int = 120


# ==================== RTCM Parsing ====================
class RtcmConfig(BaseModel):
    parse_interval: int = 5  # RTCM parsing interval (seconds)
    buffer_size: int = 1000  # RTCM buffer size
    parse_duration: int = 30  # RTCM data parsing duration (seconds) - used to correct STR table


class WebsocketConfig(BaseModel):
    """WebSocket Configuration"""

    enabled: bool = True
    ping_timeout: int = 120
    ping_interval: int = 15
    # Socket.IO CORS allowed origins. Empty => same-origin only (python-socketio
    # default). Set to the external ingress hostname(s) when the admin dashboard
    # is served through a reverse proxy, e.g.
    #   NTRIP_CASTER_WEBSOCKET__CORS_ALLOWED_ORIGINS='["https://ntrip-admin.example.com"]'
    # A blanket "*" is deliberately never emitted (credentialed connections).
    cors_allowed_origins: list[str] = []


def resolve_socketio_cors_origins(origins: list[str]) -> list[str] | None:
    """Map the configured allowed-origins list to the SocketIO argument.

    Returns None when no origins are configured, so python-socketio keeps its
    safe same-origin default. Any wildcard entry is stripped — a blanket "*" is
    never passed, because the admin session uses credentialed connections.
    """
    filtered = [o for o in origins if o and o != "*"]
    return filtered or None


# ==================== Reserved ====================
class PaymentConfig(BaseModel):
    """Payment QR Code URLs"""

    alipay_qr_code: str = ""
    wechat_qr_code: str = ""


# ==================== Performance configuration ====================
class PerformanceConfig(BaseModel):
    thread_pool_size: int = 5000
    max_workers: int = 5000
    connection_queue_size: int = 5000
    max_memory_usage: int = 2048
    cpu_warning_threshold: int = 80
    memory_warning_threshold: int = 80


class Settings(BaseSettings):
    app: AppConfig = AppConfig()
    development: DevelopmentConfig = DevelopmentConfig()
    caster: CasterConfig = CasterConfig()
    network: NetworkConfig = NetworkConfig()
    ntrip: NtripConfig = NtripConfig()
    web: WebConfig = WebConfig()
    database: DatabaseConfig = DatabaseConfig()
    logging: LoggingConfig = LoggingConfig()
    security: SecurityConfig = SecurityConfig()
    admin: AdminConfig = AdminConfig()
    tcp: TcpConfig = TcpConfig()
    data_forwarding: DataForwardingConfig = DataForwardingConfig()
    rtcm: RtcmConfig = RtcmConfig()
    websocket: WebsocketConfig = WebsocketConfig()
    payment: PaymentConfig = PaymentConfig()
    performance: PerformanceConfig = PerformanceConfig()

    model_config = SettingsConfigDict(env_nested_delimiter="__", env_prefix="NTRIP_CASTER_")


# ==================== Load Configuration ====================


def load_settings() -> Settings:
    """Load settings from file or environment variables"""
    config_file = os.environ.get("NTRIP_CONFIG_FILE")

    if not config_file:
        defaults = ["config.yaml", "config.yml", "config.json", "config.ini"]
        root_dir = Path(__file__).parent.parent
        for d in defaults:
            p = root_dir / d
            if p.exists():
                config_file = str(p)
                break

    if not config_file or not os.path.exists(config_file):
        # Even if file doesn't exist, pydantic-settings will still load from env vars
        return Settings()

    ext = os.path.splitext(config_file)[1].lower()

    try:
        if ext in (".yaml", ".yml"):
            with open(config_file, encoding="utf-8") as f:
                data = yaml.safe_load(f)
                return Settings.model_validate(data)
        elif ext == ".json":
            with open(config_file, encoding="utf-8") as f:
                data = json.load(f)
                return Settings.model_validate(data)
        elif ext == ".ini":
            cp = configparser.ConfigParser()
            cp.read(config_file, encoding="utf-8")
            ini_data: dict[str, Any] = {}
            for section in cp.sections():
                section_data: dict[str, Any] = {}
                for key, value in cp.items(section):
                    # INI values are always strings, Pydantic will try to convert them
                    # But for lists, we need to handle it
                    if section == "ntrip" and key == "supported_versions":
                        section_data[key] = [item.strip() for item in value.split(",") if item.strip()]
                    else:
                        section_data[key] = value
                ini_data[section] = section_data
            return Settings.model_validate(ini_data)
    except Exception as e:
        print(f"Error loading config file {config_file}: {e}")

    return Settings()


settings = load_settings()

# ==================== RTCM message type descriptions ====================

RTCM_MESSAGE_DESCRIPTIONS = {
    1001: "L1-Only GPS RTK Observables",
    1002: "Extended L1-Only GPS RTK Observables",
    1003: "L1&L2 GPS RTK Observables",
    1004: "Extended L1&L2 GPS RTK Observables",
    1005: "Stationary RTK Reference Station ARP",
    1006: "Stationary RTK Reference Station ARP with Antenna Height",
    1007: "Antenna Descriptor",
    1008: "Antenna Descriptor & Serial Number",
    1009: "L1-Only GLONASS RTK Observables",
    1010: "Extended L1-Only GLONASS RTK Observables",
    1011: "L1&L2 GLONASS RTK Observables",
    1012: "Extended L1&L2 GLONASS RTK Observables",
    1013: "System Parameters",
    1019: "GPS Ephemerides",
    1020: "GLONASS Ephemerides",
    1033: "Receiver and Antenna Descriptors",
    1074: "GPS MSM4",
    1075: "GPS MSM5",
    1077: "GPS MSM7",
    1084: "GLONASS MSM4",
    1085: "GLONASS MSM5",
    1087: "GLONASS MSM7",
    1094: "Galileo MSM4",
    1095: "Galileo MSM5",
    1097: "Galileo MSM7",
    1124: "BeiDou MSM4",
    1125: "BeiDou MSM5",
    1127: "BeiDou MSM7",
}

# ==================== Utility Functions ====================


def get_all_network_interfaces() -> list[tuple[str, str]]:
    """Get IP addresses of all network interfaces"""
    interfaces: list[tuple[str, str]] = []

    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None):
            family, socktype, proto, canonname, sockaddr = info
            if family == socket.AF_INET:  # Only IPv4
                ip = str(sockaddr[0])
                if ip not in [addr[1] for addr in interfaces]:  # Avoid duplicates
                    interfaces.append((f"Interface-{len(interfaces) + 1}", ip))
    except Exception:
        pass

    if not any(addr[1] == "127.0.0.1" for addr in interfaces):
        interfaces.append(("Loopback", "127.0.0.1"))

    return interfaces


def get_private_ips() -> list[tuple[str, str]]:
    """Get all private IP addresses"""
    private_ips: list[tuple[str, str]] = []

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            primary_ip = str(s.getsockname()[0])
            private_ips.append(("Primary", primary_ip))
    except Exception:
        pass

    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None):
            family, socktype, proto, canonname, sockaddr = info
            if family == socket.AF_INET:
                ip = str(sockaddr[0])
                if ip.startswith("192.168.") or ip.startswith("10.") or ip.startswith("172.") or ip == "127.0.0.1":
                    if ip not in [addr[1] for addr in private_ips]:
                        interface_name = f"Interface-{len(private_ips) + 1}"
                        if ip == "127.0.0.1":
                            interface_name = "Loopback"
                        elif ip.startswith("192.168."):
                            interface_name = "LAN"
                        private_ips.append((interface_name, ip))
    except Exception:
        pass

    return private_ips


def get_display_urls(port: int, service_name: str = "Service") -> list[str]:
    """Get all accessible URLs for display"""
    urls = []

    listen_host = settings.network.host

    if listen_host == "0.0.0.0":
        for interface_name, ip in get_private_ips():
            urls.append(f"http://{ip}:{port}")
    else:
        urls.append(f"http://{listen_host}:{port}")

    return urls


def load_from_env() -> None:
    """
    Load configuration from environment variables.
    Note: Pydantic Settings already handles environment variables.
    This function refreshes the settings.
    """
    # Check for legacy env vars
    new_data: dict[str, Any] = {}
    if "NTRIP_PORT" in os.environ:
        try:
            new_data.setdefault("ntrip", {})["port"] = int(os.environ["NTRIP_PORT"])
        except ValueError:
            pass
    if "WEB_PORT" in os.environ:
        try:
            new_data.setdefault("web", {})["port"] = int(os.environ["WEB_PORT"])
        except ValueError:
            pass
    if "DEBUG" in os.environ:
        new_data.setdefault("development", {})["debug_mode"] = os.environ["DEBUG"].lower() in ("true", "1", "yes", "on")
    if "DATABASE_PATH" in os.environ:
        new_data.setdefault("database", {})["path"] = os.environ["DATABASE_PATH"]
    if "SECRET_KEY" in os.environ:
        new_data.setdefault("security", {})["secret_key"] = os.environ["SECRET_KEY"]

    if new_data:
        # Update existing settings with new env data
        for section, values in new_data.items():
            section_model = getattr(settings, section)
            for k, v in values.items():
                setattr(section_model, k, v)


def validate_config() -> list[str]:
    """Validate configuration parameters"""
    errors: list[str] = []

    if not (1024 <= settings.ntrip.port <= 65535):
        errors.append(f"NTRIP port {settings.ntrip.port} is out of valid range (1024-65535)")

    if not (1024 <= settings.web.port <= 65535):
        errors.append(f"Web port {settings.web.port} is out of valid range (1024-65535)")

    if settings.network.buffer_size <= 0 or settings.network.buffer_size > settings.network.max_buffer_size:
        errors.append(f"Buffer size {settings.network.buffer_size} is invalid")

    if not os.path.exists(settings.logging.log_dir):
        try:
            os.makedirs(settings.logging.log_dir)
        except Exception as e:
            errors.append(f"Cannot create log directory {settings.logging.log_dir}: {e}")

    return errors


def init_config() -> bool:
    """Initialize configuration"""
    load_from_env()
    errors = validate_config()
    if errors:
        return False
    return True


def get_config_dict() -> dict[str, Any]:
    """Get configuration dictionary for debugging"""
    return {
        "version": settings.app.version,
        "app_name": settings.app.name,
        "debug": settings.development.debug_mode,
        "ntrip_host": settings.network.host,
        "ntrip_port": settings.ntrip.port,
        "web_host": settings.network.host,
        "web_port": settings.web.port,
        "max_connections": settings.network.max_connections,
        "buffer_size": settings.network.buffer_size,
        "database_path": settings.database.path,
        "log_level": settings.logging.log_level,
        "tcp_keepalive": {
            "enabled": settings.tcp.keepalive_enabled,
            "idle": settings.tcp.keepalive_idle,
            "interval": settings.tcp.keepalive_interval,
            "count": settings.tcp.keepalive_count,
        },
        "ring_buffer_size": settings.data_forwarding.ring_buffer_size,
        "rtcm_parse_interval": settings.rtcm.parse_interval,
    }
