#!/usr/bin/env python3
"""
Agent-OS — AI Agent Browser — Production Edition
Entry point. Launches browser + agent server with full production infrastructure.

Usage:
    python main.py                              # Default: headless, port 8000
    python main.py --headed                     # Show browser window
    python main.py --agent-token "my-token"     # Set custom agent token
    python main.py --port 9000                  # Custom WebSocket port
    python main.py --max-ram 450                # Cap RAM at 450MB
    python main.py --database "postgresql+asyncpg://..."  # Enable database
    python main.py --redis "redis://localhost:6379/0"     # Enable Redis
    python main.py --json-logs                   # JSON structured logging
"""
import asyncio
import argparse
import signal
import sys
# Monkeypatch 'crypt' for Python 3.13+ compatibility before passlib is imported
try:
    import crypt
except ImportError:
    import types
    sys.modules['crypt'] = types.ModuleType('crypt')
import psutil
import os
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

# ─── Auto-load .env file ────────────────────────────────
_env_file = Path(__file__).parent / ".env"
if _env_file.exists():
    for line in _env_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and value and key not in os.environ:
                os.environ[key] = value

from src.core.config import Config
from src.core.browser import AgentBrowser
from src.core.session import SessionManager
from src.infra.logging import setup_logging, get_logger


__version__ = "3.2.0"


class AgentOS:
    """Main Agent-OS application with production infrastructure."""

    def __init__(self, args):
        self.args = args
        self.config = Config(args.config)

        # ─── Logging ─────────────────────────────────────
        log_level = args.log_level or self.config.get("logging.level", "INFO")
        json_logs = args.json_logs or self.config.get("logging.json_logs", False)
        setup_logging(level=log_level, json_logs=json_logs)
        self.logger = get_logger("agent-os")

        # ─── Database ────────────────────────────────────
        self.db = None
        db_dsn = args.database or self.config.get("database.dsn")
        if args.database or self.config.get("database.enabled", False):
            from src.infra.database import init_db
            self.db = init_db(
                db_dsn,
                pool_size=self.config.get("database.pool_size", 20),
                max_overflow=self.config.get("database.max_overflow", 10),
            )
            self.logger.info("Database initialized", dsn=db_dsn.split("@")[-1] if "@" in db_dsn else db_dsn)

        # ─── Redis ───────────────────────────────────────
        self.redis = None
        redis_url = args.redis or self.config.get("redis.url")
        if args.redis or self.config.get("redis.enabled", False):
            from src.infra.redis_client import init_redis
            self.redis = init_redis(
                redis_url,
                fallback_on_failure=self.config.get("redis.fallback_on_failure", True),
            )
            self.logger.info("Redis client initialized")

        # ─── Auth System (initialized first so token setup can check it) ────
        self.jwt_handler = None
        self.api_key_manager = None
        self.user_manager = None
        self.auth_middleware = None

        if self.config.get("security.enable_jwt_auth", True):
            from src.auth.jwt_handler import JWTHandler
            jwt_secret = self.config.get("jwt.secret_key") or os.environ.get("JWT_SECRET_KEY")
            if not jwt_secret:
                # Auto-generate for Docker/testing, warn in production
                import secrets
                jwt_secret = secrets.token_urlsafe(48)
                self.logger.warning("JWT_SECRET_KEY not set — auto-generated (sessions won't survive restarts)")
                self.logger.warning("For production: set JWT_SECRET_KEY env var")

            self.jwt_handler = JWTHandler(
                secret_key=jwt_secret,
                algorithm=self.config.get("jwt.algorithm", "HS256"),
                access_token_expire_minutes=self.config.get("jwt.access_token_expire_minutes", 15),
                refresh_token_expire_days=self.config.get("jwt.refresh_token_expire_days", 30),
                issuer=self.config.get("jwt.issuer", "agent-os"),
            )

            from src.auth.api_key_manager import APIKeyManager
            db_factory = self.db.session if self.db else None
            self.api_key_manager = APIKeyManager(db_session_factory=db_factory)

            from src.auth.user_manager import UserManager
            self.user_manager = UserManager(db_session_factory=db_factory)

            from src.auth.middleware import AuthMiddleware
            # Collect legacy tokens for backward compatibility
            legacy_tokens = []
            agent_token = self.config.get("server.agent_token")
            if agent_token:
                legacy_tokens.append(agent_token)
            for t in self.config.get("server.allowed_tokens", []):
                if t:
                    legacy_tokens.append(t)
            self.auth_middleware = AuthMiddleware(
                jwt_handler=self.jwt_handler,
                api_key_manager=self.api_key_manager,
                redis_client=self.redis,
                legacy_tokens=legacy_tokens,
            )
            self.logger.info("Auth system enabled (JWT + API keys + legacy tokens)")

        # Token setup (backward compat) — must run AFTER auth init
        if args.agent_token:
            self.config.set("server.agent_token", args.agent_token)
            # Also add to auth middleware's legacy tokens (since middleware was created before this)
            if self.auth_middleware:
                self.auth_middleware.add_legacy_token(args.agent_token)
        elif not self.config.get("server.agent_token") and not self.jwt_handler:
            auto_token = self.config.generate_agent_token("agent")
            if self.auth_middleware:
                self.auth_middleware.add_legacy_token(auto_token)
            self.logger.info("Auto-generated legacy agent token")

        # ─── Browser ─────────────────────────────────────
        self.browser = AgentBrowser(self.config)
        self.session_manager = SessionManager(self.config)
        self.persistent_manager = None
        if self.config.get("persistent.enabled", False) or args.persistent:
            from src.core.persistent_browser import PersistentBrowserManager
            self.persistent_manager = PersistentBrowserManager(self.config)

        # ─── Server ──────────────────────────────────────
        from src.agents.server import AgentServer
        self.server = AgentServer(
            config=self.config,
            browser=self.browser,
            session_manager=self.session_manager,
            persistent_manager=self.persistent_manager,
            auth_middleware=self.auth_middleware,
            api_key_manager=self.api_key_manager,
            user_manager=self.user_manager,
            redis_client=self.redis,
        )

        # Debug server
        self.debug_server = None
        if args.debug:
            from src.debug.server import DebugServer
            self.debug_server = DebugServer(
                self.config, self.browser, self.session_manager,
                self.server, self.persistent_manager,
            )
            # Set debug_mode config flag so server.py can gate debug query param
            self.config.set("server.debug_mode", True)

        self._running = False
        self._ram_monitor_task = None
        self._shutdown_event = asyncio.Event()

        # ─── Apply CLI Overrides ─────────────────────────
        if args.headed:
            self.config.set("browser.headless", False)
        if args.port:
            self.config.set("server.ws_port", args.port)
            self.config.set("server.http_port", args.port + 1)
            self.config.set("server.debug_port", args.port + 2)
        if args.max_ram:
            self.config.set("browser.max_ram_mb", args.max_ram)
        if args.proxy:
            self.config.set("browser.proxy", args.proxy)
        if args.device:
            self.config.set("browser.device", args.device)

        # Token already set up before auth middleware

        if args.rate_limit:
            self.config.set("server.rate_limit_max", args.rate_limit)

        # ─── Agent Swarm ───────────────────────────────────
        if args.swarm or self.config.get("swarm.enabled", False):
            self.config.set("swarm.enabled", True)
            if args.swarm_api_key:
                self.config.set("swarm.api_key", args.swarm_api_key)
            self.logger.info("Agent Swarm (search) module enabled")

    async def start(self):
        """Start all components after validating configuration."""
        self._running = True

        # ─── Startup Validation ──────────────────────────────
        self._validate_production_config()

        self.logger.info("=" * 60)
        self.logger.info(f"  Agent-OS — AI Agent Browser v{__version__} (Production)")
        self.logger.info("=" * 60)

        # Connect Redis
        if self.redis:
            self.logger.info("Connecting to Redis...")
            await self.redis.connect()

        # Create DB tables if needed (dev mode)
        if self.db and self.args.create_tables:
            self.logger.info("Creating database tables...")
            await self.db.create_tables()

        # Start browser
        self.logger.info("Starting browser engine...")
        await self.browser.start()

        # Start session manager
        self.logger.info("Starting session manager...")
        await self.session_manager.start()

        # Start persistent browser manager
        if self.persistent_manager:
            self.logger.info("Starting persistent browser manager...")
            await self.persistent_manager.start()

        # Start agent server
        self.logger.info("Starting agent server...")
        await self.server.start()

        # Log swarm status
        if self.config.get("swarm.enabled", False):
            http_port = self.config.get("server.http_port", 8001)
            self.logger.info("  Swarm Endpoints:")
            self.logger.info(f"    Health:  GET  http://0.0.0.0:{http_port}/swarm/health")
            self.logger.info(f"    Search:  POST http://0.0.0.0:{http_port}/swarm/search")
            self.logger.info(f"    Route:   POST http://0.0.0.0:{http_port}/swarm/route")
            self.logger.info(f"    Agents:  GET  http://0.0.0.0:{http_port}/swarm/agents")
            self.logger.info(f"    Config:  GET  http://0.0.0.0:{http_port}/swarm/config")

        # Start debug UI server
        if self.debug_server:
            self.logger.info("Starting debug UI server...")
            await self.debug_server.start()

        # Start RAM monitor
        self._ram_monitor_task = asyncio.create_task(self._ram_monitor())

        # Print status
        ws_port = self.config.get("server.ws_port", 8000)
        http_port = self.config.get("server.http_port", 8001)
        debug_port = self.config.get("server.debug_port", 8002)

        self.logger.info(
            "Agent-OS ready",
            ws_port=ws_port,
            http_port=http_port,
            debug_port=debug_port if self.debug_server else None,
            auth_enabled=bool(self.jwt_handler),
            database=bool(self.db),
            redis=bool(self.redis),
        )

        self.logger.info("")
        self.logger.info("  Endpoints:")
        self.logger.info(f"    WebSocket: ws://0.0.0.0:{ws_port}")
        self.logger.info(f"    HTTP API:  http://0.0.0.0:{http_port}")
        self.logger.info(f"    Health:    http://0.0.0.0:{http_port}/health")
        if self.debug_server:
            self.logger.info(f"    Debug UI:  http://0.0.0.0:{debug_port}")
        if self.jwt_handler:
            self.logger.info(f"    Auth:      POST http://0.0.0.0:{http_port}/auth/register")
            self.logger.info(f"               POST http://0.0.0.0:{http_port}/auth/login")
        if self.api_key_manager:
            self.logger.info(f"    API Keys:  POST http://0.0.0.0:{http_port}/auth/api-keys")

        legacy_token = self.config.get("server.agent_token")
        if legacy_token:
            masked = f"{legacy_token[:4]}****{legacy_token[-4:]}" if len(legacy_token) > 12 else "****"
            self.logger.info(f"    Legacy Token: {masked}")
        self.logger.info("")
        self.logger.info("  Press Ctrl+C to stop")

        # Wait for shutdown signal
        try:
            await self._shutdown_event.wait()
        except asyncio.CancelledError:
            pass

    def _validate_production_config(self):
        """Validate critical config at startup. Warn or fail on dangerous defaults."""
        issues = []
        warnings = []

        # CORS must not be wildcard
        cors = self.config.get("server.cors_origin")
        if cors == "*":
            issues.append("server.cors_origin is '*' — set to empty string and use cors_allowed_origins")

        # Must have some form of auth
        has_jwt = bool(self.jwt_handler)
        has_api_key = bool(self.api_key_manager)
        has_legacy = bool(self.config.get("server.agent_token"))
        allow_legacy = self.config.get("security.allow_legacy_token_auth", False)

        if not has_jwt and not has_api_key and not has_legacy:
            issues.append("No authentication configured. Set JWT_SECRET_KEY or --agent-token")

        if allow_legacy:
            warnings.append("Legacy token auth is enabled — disable for production (security.allow_legacy_token_auth)")

        # Debug UI should be off
        if self.debug_server:
            warnings.append("Debug UI server is enabled — disable in production (use --debug only for development)")

        # Browser bound to 0.0.0.0
        host = self.config.get("server.host", "127.0.0.1")
        if host == "0.0.0.0":
            warnings.append("Server bound to 0.0.0.0 — ensure firewall rules are in place")

        # Log all issues
        for issue in issues:
            self.logger.error(f"CONFIG ERROR: {issue}")
        for warning in warnings:
            self.logger.warning(f"CONFIG WARNING: {warning}")

        if issues:
            self.logger.error(f"Found {len(issues)} critical config issue(s). Fix before deploying.")
            sys.exit(1)

    async def stop(self):
        """Graceful shutdown."""
        self._running = False
        self._shutdown_event.set()
        self.logger.info("Shutting down Agent-OS...")

        if self._ram_monitor_task:
            self._ram_monitor_task.cancel()

        if self.debug_server:
            await self.debug_server.stop()

        if self.persistent_manager:
            await self.persistent_manager.stop()

        # Clean up swarm resources
        if self.server._swarm_backend:
            self.server._swarm_backend.close()
        if self.server._swarm_pool:
            self.server._swarm_pool.close()

        await self.server.stop()
        await self.session_manager.stop()
        await self.browser.stop()

        if self.redis:
            await self.redis.disconnect()

        if self.db:
            await self.db.close()

        self.logger.info("Agent-OS stopped")

    async def _ram_monitor(self):
        """Monitor RAM usage and warn if exceeding limits."""
        max_ram = self.config.get("browser.max_ram_mb", 500)
        while self._running:
            try:
                process = psutil.Process(os.getpid())
                ram_mb = process.memory_info().rss / 1024 / 1024
                if ram_mb > max_ram:
                    self.logger.warning("RAM usage exceeds limit", current_mb=round(ram_mb), limit_mb=max_ram)
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                break
            except Exception:
                await asyncio.sleep(10)


def parse_args():
    parser = argparse.ArgumentParser(description="Agent-OS — AI Agent Browser")
    parser.add_argument("--version", action="version", version=f"Agent-OS {__version__}")
    parser.add_argument("--headed", action="store_true", help="Show browser window")
    parser.add_argument("--agent-token", type=str, default=os.environ.get("AGENT_TOKEN"), help="Set legacy agent authentication token")
    parser.add_argument("--port", type=int, default=int(os.environ.get("WS_PORT", "0")) or None, help="WebSocket server port (HTTP = port+1, Debug = port+2)")
    parser.add_argument("--max-ram", type=int, help="Max RAM in MB")
    parser.add_argument("--config", type=str, help="Config file path")
    parser.add_argument("--proxy", type=str, help="Proxy URL (http://user:pass@host:port)")
    parser.add_argument("--device", type=str, help="Device preset (iphone_14, galaxy_s23, ipad, etc.)")
    parser.add_argument("--persistent", action="store_true", help="Enable persistent Chromium (production mode)")
    parser.add_argument("--debug", action="store_true", help="Enable debug UI server (disabled by default)")
    parser.add_argument("--rate-limit", type=int, default=60, help="Max requests per minute per token (default: 60)")
    parser.add_argument("--swarm", action="store_true", help="Enable Agent Swarm (search) module")
    parser.add_argument("--swarm-api-key", type=str, help="API key for swarm search endpoints")

    # Production options
    parser.add_argument("--database", type=str, default=os.environ.get("DATABASE_DSN"), help="PostgreSQL DSN (postgresql+asyncpg://user:pass@host/db)")
    parser.add_argument("--redis", type=str, default=os.environ.get("REDIS_URL"), help="Redis URL (redis://host:6379/0)")
    parser.add_argument("--json-logs", action="store_true", default=False, help="Enable JSON structured logging (default: off, human-readable console)")
    parser.add_argument("--no-json-logs", action="store_true", help="Alias for default behavior (console logging)")
    parser.add_argument("--log-level", type=str, choices=["DEBUG", "INFO", "WARNING", "ERROR"], help="Log level")
    parser.add_argument("--create-tables", action="store_true", help="Create database tables on startup")
    parser.add_argument("--setup", action="store_true", help="Run interactive setup wizard (optional API keys)")
    return parser.parse_args()


async def main():
    args = parse_args()

    # Handle --no-json-logs override
    if args.no_json_logs:
        args.json_logs = False

    # Run setup wizard if requested or first launch
    if args.setup:
        from src.setup.wizard import run_setup_if_needed
        run_setup_if_needed(force=True)
    else:
        from src.setup.wizard import run_setup_if_needed
        run_setup_if_needed(non_interactive=True)

    app = AgentOS(args)

    # Handle shutdown signals
    shutdown_event = asyncio.Event()

    def signal_handler(sig, frame):
        shutdown_event.set()

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    try:
        await app.start()
    except KeyboardInterrupt:
        await app.stop()
    except OSError as e:
        if "address already in use" in str(e).lower() or e.errno == 98:
            ws_port = app.config.get("server.ws_port", 8000)
            http_port = app.config.get("server.http_port", 8001)
            if os.name == "nt":
                kill_cmd = f"Get-Process -Id (Get-NetTCPConnection -LocalPort {ws_port}).OwningProcess | Stop-Process -Force"
            else:
                kill_cmd = f"lsof -i :{ws_port} -i :{http_port}"
            app.logger.error(
                f"Port conflict detected! Another process is using port {ws_port} or {http_port}.\n"
                f"  Fix: Change the port with --port <number> (e.g., --port 9000)\n"
                f"  Or:  Kill the existing process: {kill_cmd}"
            )
            await app.stop()
            sys.exit(1)
        else:
            app.logger.error(f"Network error: {e}", exc_info=True)
            await app.stop()
            sys.exit(1)
    except Exception as e:
        app.logger.error(f"Fatal error: {e}", exc_info=True)
        await app.stop()
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
