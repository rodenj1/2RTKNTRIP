#!/usr/bin/env python3
"""
web.py - Web Management Module
Function: Provides frontend interface, displays real-time mount point info,
supports viewing and querying parsed RTCM data.
"""

import os
import re
import threading
import time
from collections.abc import Callable
from datetime import datetime
from functools import wraps
from threading import Thread
from typing import Any, Optional

from flask import (
    Flask,
    Response,
    jsonify,
    redirect,
    render_template_string,
    request,
    send_from_directory,
    session,
    url_for,
)
from flask_socketio import SocketIO, emit, join_room
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from sqlalchemy import select
from authlib.integrations.flask_client import OAuth
from flasgger import Swagger
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from . import config, connection, forwarder, logger, metrics
from .database import DatabaseManager, db_session
from .models import Admin
from .forwarder import SimpleDataForwarder
from .logger import log_error, log_info, log_system_event, log_warning, log_web_request
from .rtcm2_manager import parser_manager as rtcm_manager

# Global server instance reference
server_instance: Any = None


def set_server_instance(server: Any) -> None:
    """Set server instance"""
    global server_instance
    server_instance = server


def get_server_instance() -> Any:
    """Get server instance"""
    return server_instance


class User(UserMixin):
    """Flask-Login User class"""
    def __init__(self, id: int, username: str):
        self.id = id
        self.username = username

class WebManager:
    """Web Manager"""

    def __init__(self, db_manager: DatabaseManager, data_forwarder: SimpleDataForwarder, start_time: float) -> None:
        self.db_manager = db_manager
        self.data_forwarder = data_forwarder
        self.start_time = start_time

        # Template and static file directories
        self.template_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "templates")
        self.static_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static")

        # Create Flask application
        self.app = Flask(__name__, static_folder=self.static_dir, static_url_path="/static")
        self.app.secret_key = config.settings.security.secret_key

        # Initialize Login Manager
        self.login_manager = LoginManager()
        self.login_manager.init_app(self.app)
        self.login_manager.login_view = 'login'

        @self.login_manager.user_loader
        def load_user(user_id: str) -> Optional[User]:
            with db_session() as session:
                admin_data = session.execute(
                    select(Admin.id, Admin.username).where(Admin.id == int(user_id))
                ).first()
                if admin_data:
                    return User(admin_data.id, admin_data.username)
            return None

        # Initialize Swagger
        self.swagger = Swagger(self.app)

        # Initialize OAuth
        self.oauth = OAuth(self.app)
        if config.settings.security.oidc.enabled:
            self.oauth.register(
                name='oidc',
                client_id=config.settings.security.oidc.client_id,
                client_secret=config.settings.security.oidc.client_secret,
                server_metadata_url=f"{config.settings.security.oidc.issuer}/.well-known/openid-configuration",
                client_kwargs={
                    'scope': config.settings.security.oidc.scope
                }
            )

        # Create SocketIO instance
        socketio_kwargs: dict[str, Any] = {
            "async_mode": "gevent" if not config.settings.development.debug_mode else "threading",
            "ping_timeout": config.settings.websocket.ping_timeout,
            "ping_interval": config.settings.websocket.ping_interval,
        }
        # Only set cors_allowed_origins when configured; otherwise leave it unset
        # so python-socketio keeps its safe same-origin default. Never "*".
        _cors_origins = config.resolve_socketio_cors_origins(config.settings.websocket.cors_allowed_origins)
        if _cors_origins is not None:
            socketio_kwargs["cors_allowed_origins"] = _cors_origins
        self.socketio = SocketIO(self.app, **socketio_kwargs)

        # Register routes and SocketIO events
        self._register_routes()
        self._register_socketio_events()

        # Real-time data push thread
        self.push_thread: Thread | None = None
        self.push_running = False

        # Set web instance for logger to push real-time logs
        logger.set_web_instance(self)

    def _format_uptime_simple(self, uptime_seconds: float) -> str:
        """Format uptime (simple version)"""
        try:
            days = int(uptime_seconds // 86400)
            hours = int((uptime_seconds % 86400) // 3600)
            minutes = int((uptime_seconds % 3600) // 60)

            if days > 0:
                return f"{days}d {hours}h {minutes}m"
            elif hours > 0:
                return f"{hours}h {minutes}m"
            else:
                return f"{minutes}m"
        except Exception:
            return "0m"

    def _validate_alphanumeric(self, value: str, field_name: str) -> tuple[bool, str]:
        """Validate input contains only letters, numbers, underscores and hyphens"""
        if not value:
            return False, f"{field_name} cannot be empty"
        if not re.match(r"^[a-zA-Z0-9_-]+$", value):
            return False, f"{field_name} can only contain letters, numbers, underscores and hyphens"
        return True, ""

    def _load_template(self, template_name: str, **kwargs: Any) -> str:
        """Load external template file"""
        template_path = os.path.join(self.template_dir, template_name)
        try:
            with open(template_path, encoding="utf-8") as f:
                template_content = f.read()
            return render_template_string(template_content, **kwargs)
        except FileNotFoundError:
            log_error(f"Template file not found: {template_path}")
            return f"<h1>Template not found: {template_name}</h1>"
        except Exception as e:
            log_error(f"Failed to load template file: {e}")
            return f"<h1>Failed to load template: {e!s}</h1>"

    def _register_routes(self) -> None:
        """Register Flask routes"""

        @self.app.route("/static/<path:filename>")
        def static_files(filename: str) -> Any:
            """Serve static files"""
            static_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static")
            return send_from_directory(static_dir, filename)

        @self.app.route("/")
        def index() -> Any:
            """Main index - SPA Application"""
            app_name = config.settings.app.name
            app_version = config.settings.app.version
            current_year = datetime.now().year

            return self._load_template(
                "spa.html",
                app_name=app_name,
                app_version=app_version,
                current_year=current_year,
                contact_email=config.settings.app.contact,
                website_url=config.settings.app.website,
            )

        @self.app.route("/login", methods=["GET", "POST"])
        def login() -> Any:
            """Login page"""
            if current_user.is_authenticated:
                return redirect(url_for("index"))

            if request.method == "POST":
                username = request.form.get("username", "").strip()
                password = request.form.get("password", "").strip()

                if not username or not password:
                    return self._load_template("login.html", error="Username and password are required")

                with db_session() as session_db:
                    admin_data = session_db.execute(
                        select(Admin.id, Admin.username).where(Admin.username == username)
                    ).first()
                    if admin_data and self.db_manager.verify_admin(username, password):
                        user = User(admin_data.id, admin_data.username)
                        login_user(user)
                        redirect_page = request.args.get("redirect")
                        if redirect_page in ["users", "mounts", "settings"]:
                            return redirect(f"/?page={redirect_page}")
                        return redirect(url_for("index"))
                    else:
                        return self._load_template("login.html", error="Invalid username or password")

            return self._load_template("login.html", oidc_enabled=config.settings.security.oidc.enabled)

        @self.app.route("/logout", methods=["GET", "POST"])
        def logout() -> Any:
            """Logout"""
            logout_user()
            if request.method == "POST":
                return jsonify({"success": True})
            return redirect(url_for("login"))

        @self.app.route("/oidc/login")
        def oidc_login() -> Any:
            """OIDC Login redirect"""
            if not config.settings.security.oidc.enabled:
                return redirect(url_for("login"))
            redirect_uri = url_for('oidc_authorize', _external=True)
            return self.oauth.oidc.authorize_redirect(redirect_uri)

        @self.app.route("/oidc/authorize")
        def oidc_authorize() -> Any:
            """OIDC Authorization callback"""
            if not config.settings.security.oidc.enabled:
                return redirect(url_for("login"))
            token = self.oauth.oidc.authorize_access_token()
            userinfo = token.get('userinfo')
            if userinfo:
                username = userinfo.get('preferred_username') or userinfo.get('email')
                if username:
                    with db_session() as session_db:
                        admin_data = session_db.execute(
                            select(Admin.id, Admin.username).where(Admin.username == username)
                        ).first()
                        if admin_data:
                            user = User(admin_data.id, admin_data.username)
                            login_user(user)
                            return redirect(url_for('index'))
                        else:
                            return self._load_template("login.html", error=f"User {username} not authorized as admin")
            return redirect(url_for('login'))

        @self.app.route("/api/login", methods=["POST"])
        def api_login() -> Any:
            """API Login"""
            try:
                data = request.get_json()
                if not data:
                    return jsonify({"error": "Invalid request format"}), 400
                username, password = data.get("username", "").strip(), data.get("password", "").strip()

                if not username or not password:
                    return jsonify({"error": "Username and password required"}), 400

                with db_session() as session_db:
                    admin_data = session_db.execute(
                        select(Admin.id, Admin.username).where(Admin.username == username)
                    ).first()
                    if admin_data and self.db_manager.verify_admin(username, password):
                        user = User(admin_data.id, admin_data.username)
                        login_user(user)
                        return jsonify({"success": True, "message": "Login successful", "token": "session_based"})
                    else:
                        return jsonify({"error": "Invalid username or password"}), 401
            except Exception as e:
                log_error(f"API Login failed: {e}")
                return jsonify({"error": "Login failed"}), 500

        @self.app.route("/api/mount_info/<mount>")
        @login_required
        def mount_info(mount: str) -> Any:
            """Get parsing info for a specific mount point"""
            parsed_data = rtcm_manager.get_parsed_mount_data(mount)
            statistics = rtcm_manager.get_mount_statistics(mount)
            if parsed_data:
                return jsonify({"success": True, "data": parsed_data, "statistics": statistics})
            else:
                return jsonify({"success": False, "message": "Mount point data does not exist or is not parsed"})

        @self.app.route("/api/system/restart", methods=["POST"])
        @login_required
        def restart_system() -> Any:
            """System restart API"""
            try:

                def delayed_restart() -> None:
                    time.sleep(1)
                    log_info("Admin requested system restart")
                    os._exit(0)

                threading.Thread(target=delayed_restart, daemon=True).start()
                return jsonify({"success": True, "message": "Restart command sent"})
            except Exception as e:
                log_error(f"Restart failed: {e}")
                return jsonify({"success": False, "error": str(e)}), 500

        @self.app.route("/api/mount/<mount_name>/rtcm-parse/start", methods=["POST"])
        @login_required
        def api_start_rtcm_parsing(mount_name: str) -> Any:
            """Start real-time RTCM parsing for a mount point"""
            try:

                def push_callback(parsed_data: dict[str, Any]) -> None:
                    if "mount_name" not in parsed_data:
                        log_warning("Push data missing mount_name")
                        return
                    self.socketio.emit("rtcm_realtime_data", parsed_data)

                success = rtcm_manager.start_realtime_parsing(mount_name=mount_name, push_callback=push_callback)
                if success:
                    log_system_event(f"Real-time RTCM parsing started for {mount_name}")
                    return jsonify({"success": True, "message": f"Real-time RTCM parsing started for {mount_name}"})
                else:
                    return jsonify({"error": "Failed to start parsing - mount may be offline"}), 400
            except Exception as e:
                log_error(f"Failed to start RTCM parsing: {e}")
                return jsonify({"error": str(e)}), 500

        @self.app.route("/api/mount/rtcm-parse/stop", methods=["POST"])
        @login_required
        def api_stop_rtcm_parsing() -> Any:
            """Stop all real-time RTCM parsing"""
            try:
                rtcm_manager.stop_realtime_parsing()
                log_system_event("All real-time RTCM parsing stopped")
                return jsonify({"success": True, "message": "Real-time RTCM parsing stopped"})
            except Exception as e:
                log_error(f"Failed to stop RTCM parsing: {e}")
                return jsonify({"error": str(e)}), 500

        @self.app.route("/api/mount/rtcm-parse/status", methods=["GET"])
        @login_required
        def api_get_rtcm_parsing_status() -> Any:
            """Get RTCM parser status info"""
            try:
                status = rtcm_manager.get_parser_status()
                return jsonify({"success": True, "status": status, "message": "Parser status retrieved successfully"})
            except Exception as e:
                log_error(f"Failed to get parser status: {e}")
                return jsonify({"error": str(e)}), 500

        @self.app.route("/api/app_info")
        def api_app_info() -> Any:
            """Get application info"""
            try:
                return jsonify(
                    {
                        "name": config.settings.app.name,
                        "version": config.settings.app.version,
                        "description": config.settings.app.description,
                        "author": config.settings.app.author,
                        "contact": config.settings.app.contact,
                        "website": config.settings.app.website,
                    }
                )
            except Exception as e:
                log_error(f"Failed to get app info: {e}")
                return jsonify({"error": str(e)}), 500

        @self.app.route("/api/users", methods=["GET", "POST"])
        @login_required
        def api_users() -> Any:
            """User management API"""
            if request.method == "GET":
                try:
                    users = self.db_manager.get_all_users()
                    online_users = connection.get_connection_manager().get_online_users()
                    user_list = []
                    for user in users:
                        username = user[1]
                        user_list.append(
                            {
                                "id": user[0],
                                "username": username,
                                "online": username in online_users,
                                "connection_count": connection.get_connection_manager().get_user_connection_count(
                                    username
                                ),
                                "connect_time": connection.get_connection_manager().get_user_connect_time(username)
                                or "-",
                            }
                        )
                    return jsonify(user_list)
                except Exception as e:
                    log_error(f"Failed to get users: {e}")
                    return jsonify({"error": str(e)}), 500
            elif request.method == "POST":
                try:
                    data = request.get_json()
                    if not data:
                        return jsonify({"error": "Invalid data format"}), 400
                    username, password = data.get("username", "").strip(), data.get("password", "").strip()
                    if not username or not password:
                        return jsonify({"error": "Username and password required"}), 400
                    valid, err = self._validate_alphanumeric(username, "Username")
                    if not valid:
                        return jsonify({"error": err}), 400
                    if not (2 <= len(username) <= 50) or not (6 <= len(password) <= 100):
                        return jsonify({"error": "Invalid length"}), 400
                    success, message = self.db_manager.add_user(username, password)
                    return jsonify({"message": message}), 201 if success else 400
                except Exception as e:
                    log_error(f"Failed to add user: {e}")
                    return jsonify({"error": str(e)}), 500
            return None

        @self.app.route("/api/users/<username>", methods=["PUT", "DELETE"])
        @login_required
        def api_user_detail(username: str) -> Any:
            """User detail API"""
            if request.method == "PUT":
                try:
                    data = request.get_json()
                    new_password, new_username = data.get("password", "").strip(), data.get("username", "").strip()
                    if username == config.settings.admin.username:
                        if new_username:
                            return jsonify({"error": "Admin username cannot be changed"}), 400
                        if not new_password:
                            return jsonify({"error": "New password required"}), 400
                        success = self.db_manager.update_admin_password(username, new_password)
                        return jsonify({"message": "Admin password updated"}) if success else 500
                    else:
                        forwarder.force_disconnect_user(username)
                        if new_username:
                            users = self.db_manager.get_all_users()
                            user_id = next((u[0] for u in users if u[1] == username), None)
                            if user_id is None:
                                return jsonify({"error": "User not found"}), 400
                            success, msg = self.db_manager.update_user(
                                user_id, new_username, next(u[2] for u in users if u[1] == username)
                            )
                            return (
                                jsonify({"message": f"Username updated to {new_username}"})
                                if success
                                else (jsonify({"error": msg}), 400)
                            )
                        elif new_password:
                            success, msg = self.db_manager.update_user_password(username, new_password)
                            return (
                                jsonify({"message": f"Password updated for {username}"})
                                if success
                                else (jsonify({"error": msg}), 400)
                            )
                    return jsonify({"error": "Nothing to update"}), 400
                except Exception as e:
                    log_error(f"Failed to update user: {e}")
                    return jsonify({"error": str(e)}), 500
            elif request.method == "DELETE":
                try:
                    forwarder.force_disconnect_user(username)
                    success, result = self.db_manager.delete_user(username)
                    return (
                        jsonify({"message": f"User {result} deleted"}) if success else (jsonify({"error": result}), 400)
                    )
                except Exception as e:
                    log_error(f"Failed to delete user: {e}")
                    return jsonify({"error": str(e)}), 500
            return None

        @self.app.route("/api/mounts", methods=["GET", "POST"])
        @login_required
        def api_mounts() -> Any:
            """Mount point management API"""
            if request.method == "GET":
                try:
                    mounts = self.db_manager.get_all_mounts()
                    online_mounts = connection.get_connection_manager().get_online_mounts()
                    mount_list = []
                    for mount in mounts:
                        mount_name = mount[1]
                        is_online = mount_name in online_mounts
                        data_rate_str = "0 B/s"
                        if is_online:
                            mount_info = connection.get_connection_manager().get_mount_info(mount_name)
                            if mount_info and "data_rate" in mount_info:
                                dr = mount_info["data_rate"]
                                data_rate_str = f"{dr / 1024:.2f} KB/s" if dr >= 1024 else f"{dr:.2f} B/s"
                        mount_list.append(
                            {
                                "id": mount[0],
                                "mount": mount_name,
                                "password": mount[2],
                                "username": mount[4] if len(mount) > 4 else None,
                                "lat": mount[5] if len(mount) > 5 and mount[5] is not None else 0,
                                "lon": mount[6] if len(mount) > 6 and mount[6] is not None else 0,
                                "active": is_online,
                                "connections": connection.get_connection_manager().get_mount_connection_count(
                                    mount_name
                                )
                                if is_online
                                else 0,
                                "data_rate": data_rate_str,
                            }
                        )
                    return jsonify(mount_list)
                except Exception as e:
                    log_error(f"Failed to get mounts: {e}")
                    return jsonify({"error": str(e)}), 500
            elif request.method == "POST":
                try:
                    data = request.get_json()
                    mount, password, user_id = (
                        data.get("mount", "").strip(),
                        data.get("password", "").strip(),
                        data.get("user_id"),
                    )
                    if not mount or not password:
                        return jsonify({"error": "Name and password required"}), 400
                    if not (2 <= len(mount) <= 50) or not (6 <= len(password) <= 100):
                        return jsonify({"error": "Invalid length"}), 400
                    success, message = self.db_manager.add_mount(mount, password, user_id)
                    return jsonify({"message": message}), 201 if success else 400
                except Exception as e:
                    log_error(f"Failed to add mount: {e}")
                    return jsonify({"error": str(e)}), 500
            return None

        @self.app.route("/api/mounts/<mount_name>", methods=["PUT", "DELETE"])
        @login_required
        def api_mount_detail(mount_name: str) -> Any:
            """Mount point detail API"""
            if request.method == "PUT":
                try:
                    data = request.get_json()
                    new_password, new_mount_name, username = (
                        data.get("password", "").strip(),
                        data.get("mount_name", "").strip(),
                        data.get("username"),
                    )
                    new_user_id = None
                    if username:
                        users = self.db_manager.get_all_users()
                        user = next((u for u in users if u[1] == username), None)
                        if not user:
                            return jsonify({"error": f'User "{username}" not found'}), 400
                        new_user_id = user[0]
                    forwarder.force_disconnect_mount(mount_name)
                    mounts = self.db_manager.get_all_mounts()
                    mount_id = next((m[0] for m in mounts if m[1] == mount_name), None)
                    if mount_id is None:
                        return jsonify({"error": "Mount not found"}), 400
                    success, result = self.db_manager.update_mount(
                        mount_id,
                        new_mount_name or None,
                        new_password or None,
                        new_user_id if username is not None else "keep_current",
                    )
                    return jsonify({"message": "Mount updated"}) if success else (jsonify({"error": result}), 400)
                except Exception as e:
                    log_error(f"Failed to update mount: {e}")
                    return jsonify({"error": str(e)}), 500
            elif request.method == "DELETE":
                try:
                    forwarder.force_disconnect_mount(mount_name)
                    success, result = self.db_manager.delete_mount(mount_name)
                    if success:
                        connection.get_connection_manager().remove_mount_connection(mount_name)
                    return (
                        jsonify({"message": f"Mount {result} deleted"})
                        if success
                        else (jsonify({"error": result}), 400)
                    )
                except Exception as e:
                    log_error(f"Failed to delete mount: {e}")
                    return jsonify({"error": str(e)}), 500
            return None

        @self.app.route("/api/system/stats")
        def api_system_stats() -> Any:
            """System stats API"""
            try:
                server = get_server_instance()
                if server and hasattr(server, "get_system_stats"):
                    return jsonify(server.get_system_stats())
                return jsonify({"error": "Unable to get system stats"}), 500
            except Exception as e:
                log_error(f"API Error getting system stats: {e}")
                return jsonify({"error": str(e)}), 500

        @self.app.route("/api/str-table", methods=["GET"])
        def api_str_table() -> Any:
            """Get real-time STR table data"""
            try:
                cm = connection.get_connection_manager()
                return jsonify(
                    {
                        "success": True,
                        "str_data": cm.get_all_str_data(),
                        "mount_list": cm.generate_mount_list(),
                        "timestamp": time.time(),
                    }
                )
            except Exception as e:
                log_error(f"Failed to get STR table: {e}")
                return jsonify({"success": False, "error": str(e)}), 500

        @self.app.route("/metrics")
        def prometheus_metrics() -> Any:
            """Expose Prometheus metrics"""
            return Response(generate_latest(), mimetype=CONTENT_TYPE_LATEST)

        @self.app.route("/health")
        def health_check() -> Any:
            """Health check endpoint"""
            health_status = {"status": "healthy", "timestamp": time.time()}
            try:
                # Check DB connectivity
                with db_session() as session:
                    session.execute(select(1))
                health_status["database"] = "connected"
            except Exception as e:
                health_status["status"] = "unhealthy"
                health_status["database"] = f"error: {e!s}"

            # Check if main services are running
            server = get_server_instance()
            if server:
                health_status["ntrip_server"] = "running" if server.ntrip_caster and server.ntrip_caster.running else "stopped"

            status_code = 200 if health_status["status"] == "healthy" else 503
            return jsonify(health_status), status_code

    def _register_socketio_events(self) -> None:
        """Register SocketIO events"""

        @self.socketio.on("connect")  # type: ignore
        def handle_connect() -> None:
            log_web_request("websocket", "connect", str(session.get("sid", "unknown")), 200)
            join_room("data_push")
            emit("status", {"message": "Connected successfully"})

        @self.socketio.on("disconnect")  # type: ignore
        def handle_disconnect() -> None:
            log_web_request("websocket", "disconnect", str(session.get("sid", "unknown")), 200)
            try:
                current_web_mount = rtcm_manager.get_current_web_mount()
                if current_web_mount:
                    log_info(f"WebSocket disconnected, cleaning up Web parsing thread [Mount: {current_web_mount}]")
                    rtcm_manager.stop_realtime_parsing()
            except Exception as e:
                log_error(f"Failed to clean up Web parser on disconnect: {e}")

        @self.socketio.on("request_system_stats")  # type: ignore
        def handle_request_system_stats() -> None:
            try:
                server = get_server_instance()
                if server and hasattr(server, "get_system_stats"):
                    stats = server.get_system_stats()
                    if stats:
                        emit("system_stats_update", {"stats": stats, "timestamp": time.time()})
                    else:
                        emit("error", {"message": "Unable to get stats"})
                else:
                    emit("error", {"message": "Server instance not available"})
            except Exception as e:
                log_error(f"Failed to handle system stats request: {e}")
                emit("error", {"message": str(e)})


    def start_rtcm_parsing(self) -> None:
        """Start real-time data push thread"""
        if not self.push_running:
            self.push_running = True
            self.push_thread = Thread(target=self._push_data_loop, daemon=True)
            self.push_thread.start()
            log_system_event("Web real-time data push started")

    def stop_rtcm_parsing(self) -> None:
        """Stop real-time data push thread"""
        if self.push_running:
            self.push_running = False
            if self.push_thread:
                self.push_thread.join(timeout=5)
            log_system_event("Web real-time data push stopped")

    def _push_data_loop(self) -> None:
        """Real-time data push loop"""
        log_info("Data push loop started")
        while self.push_running:
            try:
                server = get_server_instance()
                if server and hasattr(server, "get_system_stats"):
                    stats = server.get_system_stats()
                    if stats:
                        self.socketio.emit(
                            "system_stats_update", {"stats": stats, "timestamp": time.time()}, to="data_push"
                        )

                cm = connection.get_connection_manager()
                self.socketio.emit(
                    "online_users_update", {"users": cm.get_online_users(), "timestamp": time.time()}, to="data_push"
                )
                self.socketio.emit(
                    "online_mounts_update", {"mounts": cm.get_online_mounts(), "timestamp": time.time()}, to="data_push"
                )
                self.socketio.emit(
                    "str_data_update", {"str_data": cm.get_all_str_data(), "timestamp": time.time()}, to="data_push"
                )

                time.sleep(config.settings.web.realtime_push_interval)
            except Exception as e:
                log_error(f"Data push exception: {e}", exc_info=True)
                time.sleep(1)

    def push_log_message(self, message: str, log_type: str = "info") -> None:
        """Push log message to frontend"""
        try:
            self.socketio.emit(
                "log_message", {"message": message, "type": log_type, "timestamp": time.time()}, to="data_push"
            )
        except Exception as e:
            log_error(f"Failed to push log message: {e}")

    def run(self, host: str | None = None, port: int | None = None, debug: bool | None = None) -> None:
        """Start Web server"""
        is_debug = debug if debug is not None else config.settings.development.debug_mode
        self.socketio.run(
            self.app,
            host=host or config.settings.network.host,
            port=port or config.settings.web.port,
            debug=is_debug,
            allow_unsafe_werkzeug=is_debug,
        )


def create_web_manager(
    db_manager: DatabaseManager, data_forwarder: SimpleDataForwarder, start_time: float
) -> WebManager:
    """Create Web Manager instance"""
    return WebManager(db_manager, data_forwarder, start_time)
