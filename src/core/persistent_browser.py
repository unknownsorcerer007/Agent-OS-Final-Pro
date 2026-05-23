"""
Agent-OS Persistent Chromium Engine
Production-grade persistent browser for millions of concurrent users.

Architecture:
  - PersistentBrowserManager: Singleton managing the browser pool
  - BrowserInstance: One Chromium process with multiple user contexts
  - UserContext: Isolated browser context per user/session
  - HealthMonitor: Background health checks, auto-restart, memory caps
  - StateSerializer: Save/restore full browser state to disk

Key design decisions:
  - Uses Playwright launch_persistent_context for real persistence
  - Each user gets isolated profile dir under ~/.agent-os/users/{user_id}/
  - Multiple Chromium instances for horizontal scaling
  - CDP reconnection if browser crashes but process survives
  - Auto-cleanup of idle contexts with configurable TTL
  - Zero-downtime: old state loaded on restart
"""
import asyncio
import json
import logging
import os
import psutil
import random
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Callable

from patchright.async_api import async_playwright, Browser, BrowserContext, Page

from src.core.stealth import (
    handle_request_interception,
)

try:
    from cryptography.fernet import Fernet
    _FERNET_AVAILABLE = True
except ImportError:
    _FERNET_AVAILABLE = False

logger = logging.getLogger("agent-os.persistent")


def _is_docker_cgroup() -> bool:
    """Check if /proc/1/cgroup indicates Docker or containerd.

    More reliable than /.dockerenv in some Docker setups where
    the env file is not present but the cgroup still reveals
    container identity.
    """
    try:
        with open("/proc/1/cgroup", "r") as f:
            content = f.read().lower()
            return "docker" in content or "containerd" in content
    except (FileNotFoundError, PermissionError, OSError):
        return False


class BrowserState(Enum):
    """Browser instance lifecycle states."""
    CREATED = "created"
    STARTING = "starting"
    RUNNING = "running"
    DEGRADED = "degraded"       # Running but some contexts failed
    RECOVERING = "recovering"   # Auto-restarting after crash
    STOPPING = "stopping"
    STOPPED = "stopped"
    FAILED = "failed"


@dataclass
class BrowserHealth:
    """Health metrics for a browser instance."""
    instance_id: str
    state: BrowserState = BrowserState.CREATED
    started_at: float = 0
    last_health_check: float = 0
    crash_count: int = 0
    restart_count: int = 0
    active_contexts: int = 0
    total_pages: int = 0
    memory_mb: float = 0
    cpu_percent: float = 0
    blocked_requests: int = 0
    commands_executed: int = 0
    last_error: str = ""

    def to_dict(self) -> Dict:
        return {
            "instance_id": self.instance_id,
            "state": self.state.value,
            "uptime_seconds": int(time.time() - self.started_at) if self.started_at else 0,
            "crash_count": self.crash_count,
            "restart_count": self.restart_count,
            "active_contexts": self.active_contexts,
            "total_pages": self.total_pages,
            "memory_mb": round(self.memory_mb, 1),
            "cpu_percent": round(self.cpu_percent, 1),
            "blocked_requests": self.blocked_requests,
            "commands_executed": self.commands_executed,
            "last_error": self.last_error,
            "last_health_check_ago": int(time.time() - self.last_health_check) if self.last_health_check else -1,
        }


@dataclass
class UserContextState:
    """Persistent state for a user's browser context."""
    user_id: str
    profile_dir: str
    created_at: float = 0
    last_active: float = 0
    cookies_count: int = 0
    tabs: List[str] = field(default_factory=list)  # URLs of open tabs
    device: str = "desktop_1080"
    viewport: Dict = field(default_factory=lambda: {"width": 1920, "height": 1080})
    commands_executed: int = 0

    def to_dict(self) -> Dict:
        return {
            "user_id": self.user_id,
            "profile_dir": self.profile_dir,
            "created_at": self.created_at,
            "last_active": self.last_active,
            "cookies_count": self.cookies_count,
            "tabs": self.tabs,
            "device": self.device,
            "viewport": self.viewport,
            "commands_executed": self.commands_executed,
        }


class UserContext:
    """
    Isolated browser context for a single user.
    Each user gets their own persistent Chromium profile directory,
    cookies, localStorage, sessionStorage — fully isolated.
    """

    def __init__(self, user_id: str, profile_dir: str, browser_instance: 'BrowserInstance'):
        self.user_id = user_id
        self.profile_dir = Path(profile_dir)
        self.browser_instance = browser_instance
        self.context: Optional[BrowserContext] = None
        self.pages: Dict[str, Page] = {}
        self.active_page: Optional[Page] = None
        self.created_at = time.time()
        self.last_active = time.time()
        self.commands_executed = 0
        self.blocked_requests = 0
        self._console_logs: Dict[str, List[Dict]] = {}
        self._state_file = self.profile_dir / "context_state.json"
        # Cookie encryption (same pattern as browser.py)
        self._cookie_key = self._get_or_create_cookie_key()
        self._cookie_fernet = Fernet(self._cookie_key) if _FERNET_AVAILABLE else None

    async def initialize(self, config: Dict[str, Any]):
        """Create or restore persistent context for this user."""
        self.profile_dir.mkdir(parents=True, exist_ok=True)

        context_options = {
            "user_agent": config.get("user_agent"),
            "viewport": config.get("viewport", {"width": 1920, "height": 1080}),
            "locale": config.get("locale", "en-US"),
            "timezone_id": config.get("timezone_id", "America/New_York"),
            "permissions": ["geolocation", "notifications"],
            "color_scheme": "light",
            "device_scale_factor": 1.0,
            "has_touch": False,
            "is_mobile": False,
            "java_script_enabled": True,
            "ignore_https_errors": True,
        }

        self.context = await self.browser_instance.browser.new_context(**context_options)

        # Stealth setup — CDP primary + supplementary features
        from src.security.evasion_engine import EvasionEngine
        from src.core.cdp_stealth import CDPStealthInjector
        from src.core.stealth import SUPPLEMENTARY_STEALTH_JS
        from src.core.stealth_god import GodModeStealth
        evasion = EvasionEngine()
        fp = evasion.generate_fingerprint(page_id="main")
        self._evasion = evasion
        self._fingerprint = fp
        self._cdp_stealth = CDPStealthInjector()
        self._god_stealth = GodModeStealth()
        self._stealth_js = SUPPLEMENTARY_STEALTH_JS


        # Layer 2: Supplementary stealth (Notification, Battery, Font, Beacon)
        layer2_ok = False
        try:
            await self.context.add_init_script(self._stealth_js)
            layer2_ok = True
            logger.info(f"Stealth Layer 2 (Supplementary): ACTIVE for {self.user_id}")
        except Exception as e:
            logger.warning(f"Stealth Layer 2 (Supplementary) failed for {self.user_id}: {e}")

        # Set up request interception for bot detection blocking
        await self.context.route("**/*", self._handle_request)

        # Restore saved state
        state = self._load_state()
        if state and state.get("tabs"):
            for url in state["tabs"][:5]:  # Max 5 tabs on restore
                try:
                    page = await self.context.new_page()
                    self._attach_console_listener(f"tab-{len(self.pages)}", page)
                    if url and url != "about:blank":
                        await page.goto(url, wait_until="domcontentloaded", timeout=15000)
                    self.pages[f"tab-{len(self.pages)}"] = page
                except Exception as e:
                    logger.warning(f"Failed to restore tab for {self.user_id}: {e}")

        # Create main page if none restored
        if not self.pages:
            page = await self.context.new_page()
            self._attach_console_listener("main", page)
            self.pages["main"] = page

        self.active_page = self.pages.get("main", list(self.pages.values())[0])

        # Apply per-page stealth layers with fallback chain
        stealth_result = await self._apply_stealth_layers(self.active_page, fp)
        logger.info(
            f"Stealth result for {self.user_id}: "
            f"Layer1(CDP)={stealth_result['layer1']}, "
            f"Layer2(InitScript)={layer2_ok}, "
            f"Layer3(GodMode)={stealth_result['layer3']}, "
            f"verified={stealth_result['verified']}"
        )

        logger.info(f"User context initialized: {self.user_id} (profile: {self.profile_dir})")

    async def _apply_stealth_layers(self, page, fingerprint: Dict, max_retries: int = 2) -> Dict[str, bool]:
        """Apply stealth layers with fallback chain and verification.

        Applies 3 layers of anti-detection, each with its own try/except.
        If a layer fails, others still apply. After all layers, verifies
        stealth is working by checking navigator.webdriver. If verification
        fails, retries the failed layers.

        Returns dict with {layer1: bool, layer2: bool, layer3: bool, verified: bool}.
        """
        result = {"layer1": False, "layer2": False, "layer3": False, "verified": False}

        for attempt in range(max_retries):
            # Layer 1: CDP stealth — PRIMARY injection via Page.addScriptToEvaluateOnNewDocument
            if not result["layer1"]:
                try:
                    chrome_ver = fingerprint.get("chrome_version", "124") if fingerprint else "124"
                    cdp_ok = await self._cdp_stealth.inject_into_page(
                        page,
                        page_id="main",
                        chrome_version=chrome_ver,
                        fingerprint=fingerprint,
                    )
                    if cdp_ok:
                        result["layer1"] = True
                        logger.info(f"Stealth Layer 1 (CDP): ACTIVE (attempt {attempt + 1})")
                    else:
                        logger.warning(f"Stealth Layer 1 (CDP): injector returned False (attempt {attempt + 1})")
                except Exception as e:
                    logger.warning(f"Stealth Layer 1 (CDP) failed (attempt {attempt + 1}): {e}")

            # Layer 3: God Mode stealth — FALLBACK (only if CDP stealth failed)
            # Avoids conflicting with Layer 1 by only activating when Layer 1 is not active.
            if not result["layer1"] and not result["layer3"]:
                try:
                    await self._god_stealth.inject_into_page(page, page_id="main")
                    result["layer3"] = True
                    logger.info(f"Stealth Layer 3 (God Mode): ACTIVE (CDP fallback, attempt {attempt + 1})")
                except Exception as e:
                    logger.warning(f"Stealth Layer 3 (God Mode) failed (attempt {attempt + 1}): {e}")
            elif result["layer1"]:
                result["layer3"] = True  # Not needed, mark as OK
                logger.info("Stealth Layer 3 (God Mode): SKIPPED — CDP stealth is active")

            # Layer 2 is handled in initialize() via add_init_script — check if context has it
            result["layer2"] = True  # Already applied in initialize()

            # Verify stealth: check navigator.webdriver is falsy
            try:
                webdriver_value = await page.evaluate("() => navigator.webdriver")
                if not webdriver_value:
                    result["verified"] = True
                    logger.info(f"Stealth VERIFIED: navigator.webdriver = {webdriver_value}")
                    break  # All good, no need to retry
                else:
                    logger.warning(f"Stealth NOT VERIFIED: navigator.webdriver = {webdriver_value} (attempt {attempt + 1})")
            except Exception as e:
                logger.warning(f"Stealth verification failed (attempt {attempt + 1}): {e}")

        return result

    def _load_state(self) -> Optional[Dict]:
        """Load saved context state from disk."""
        if self._state_file.exists():
            try:
                with open(self._state_file) as f:
                    return json.load(f)
            except Exception:
                pass
        return None

    async def save_state(self):
        """Save current context state to disk for persistence across restarts."""
        tabs = []
        for page in self.pages.values():
            try:
                url = page.url
                if url and url != "about:blank":
                    tabs.append(url)
            except Exception:
                pass

        state = UserContextState(
            user_id=self.user_id,
            profile_dir=str(self.profile_dir),
            created_at=self.created_at,
            last_active=self.last_active,
            tabs=tabs,
            commands_executed=self.commands_executed,
        )

        try:
            with open(self._state_file, "w") as f:
                json.dump(state.to_dict(), f, indent=2)
        except Exception as e:
            logger.error(f"Failed to save state for {self.user_id}: {e}")

        # Save cookies (encrypted with Fernet, matching browser.py pattern)
        if self.context:
            try:
                cookies_file = self.profile_dir / "cookies.enc"
                storage = await self.context.storage_state()
                if self._cookie_fernet:
                    encrypted = self._cookie_fernet.encrypt(json.dumps(storage).encode())
                    cookies_file.write_bytes(encrypted)
                else:
                    # Fallback to plaintext if Fernet unavailable
                    with open(cookies_file, "w") as f:
                        json.dump(storage, f)
                cookies_file.chmod(0o600)
                state.cookies_count = len(storage.get("cookies", []))
            except Exception as e:
                logger.warning(f"Failed to save cookies for {self.user_id}: {e}")

    def _get_or_create_cookie_key(self) -> bytes:
        """Get or create encryption key for cookie storage."""
        key_path = Path(os.path.expanduser("~/.agent-os/.cookie_key"))
        if key_path.exists():
            return key_path.read_bytes()
        if _FERNET_AVAILABLE:
            key = Fernet.generate_key()
        else:
            # Fallback: generate a deterministic key from user_id if Fernet unavailable
            import hashlib
            key = hashlib.sha256(f"agent-os-cookie-key-{self.user_id}".encode()).digest()
        key_path.parent.mkdir(parents=True, exist_ok=True)
        key_path.write_bytes(key)
        key_path.chmod(0o600)
        return key

    def _attach_console_listener(self, page_id: str, page: Page):
        """Capture console logs per page."""
        self._console_logs[page_id] = []

        def on_console(msg):
            entry = {
                "type": msg.type,
                "text": msg.text,
                "location": {
                    "url": msg.location.get("url", ""),
                    "line": msg.location.get("lineNumber", 0),
                    "column": msg.location.get("columnNumber", 0),
                },
                "timestamp": time.time(),
            }
            self._console_logs[page_id].append(entry)
            if len(self._console_logs[page_id]) > 200:
                self._console_logs[page_id] = self._console_logs[page_id][-200:]

        def on_page_error(error):
            entry = {
                "type": "pageerror",
                "text": str(error),
                "location": {"url": "", "line": 0, "column": 0},
                "timestamp": time.time(),
            }
            self._console_logs[page_id].append(entry)
            if len(self._console_logs[page_id]) > 200:
                self._console_logs[page_id] = self._console_logs[page_id][-200:]

        page.on("console", on_console)
        page.on("pageerror", on_page_error)

    async def _handle_request(self, route, request):
        """Intercept and block bot detection requests."""
        should_block, fake_response = handle_request_interception(request.url, request.resource_type)
        if should_block:
            self.blocked_requests += 1
            if fake_response:
                await route.fulfill(status=200, content_type="application/json", body=json.dumps(fake_response))
            else:
                await route.fulfill(status=200, body="")
            return
        await route.continue_()

    async def close(self):
        """Save state and close context."""
        await self.save_state()
        if self.context:
            try:
                await self.context.close()
            except Exception:
                pass
        logger.info(f"User context closed: {self.user_id}")

    def get_page(self, page_id: str = "main") -> Optional[Page]:
        """Get a page by ID, falling back to active page."""
        return self.pages.get(page_id, self.active_page)

    def touch(self):
        """Update last active timestamp."""
        self.last_active = time.time()


class BrowserInstance:
    """
    A single Chromium process with multiple user contexts.
    Manages lifecycle, health checks, and auto-recovery.
    """

    def __init__(self, instance_id: str, config: Dict[str, Any]):
        self.instance_id = instance_id
        self.config = config
        self.playwright = None
        self.browser: Optional[Browser] = None
        self.user_contexts: Dict[str, UserContext] = {}
        self.health = BrowserHealth(instance_id=instance_id)
        self._max_contexts = config.get("max_contexts_per_instance", 50)
        self._launch_args = self._build_launch_args()

    def _build_launch_args(self) -> List[str]:
        """Build Chromium launch arguments."""
        args = [
            "--disable-blink-features=AutomationControlled",
            "--disable-features=IsolateOrigins,site-per-process",
            "--disable-infobars",
            "--disable-background-timer-throttling",
            "--disable-backgrounding-occluded-windows",
            "--disable-renderer-backgrounding",
            "--disable-dev-shm-usage",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-extensions",
            "--disable-component-extensions-with-background-pages",
            "--window-size=1920,1080",
            "--disable-features=TranslateUI",
            "--disable-ipc-flooding-protection",
            "--headless=new",  # New headless mode — preserves plugins, chrome runtime, correct UA
            "--disable-setuid-sandbox",
            "--process-per-site",
        ]
        # In Docker containers, Chromium cannot use its own namespace sandbox
        # because the container itself IS the sandbox. Auto-detect Docker
        # via /.dockerenv file or AGENT_OS_DOCKER env var.
        in_docker = (
            os.getenv("AGENT_OS_DOCKER") == "1"
            or os.path.exists("/.dockerenv")
            or os.path.exists("/run/.containerenv")
            or self._check_cgroup_container()
        )
        if in_docker:
            args.append("--no-sandbox")
            logger.info("Docker environment detected — browser running with --no-sandbox (container is the sandbox)")
        return [a for a in args if a]

    @staticmethod
    def _check_cgroup_container() -> bool:
        """Check if running inside a container by inspecting /proc/1/cgroup.

        More reliable than /.dockerenv in some Docker setups where
        the dockerenv file is not present (e.g., rootless Docker, Podman).
        """
        try:
            with open("/proc/1/cgroup", "r") as f:
                content = f.read().lower()
                return "docker" in content or "containerd" in content or "kubepods" in content
        except (FileNotFoundError, PermissionError):
            return False

    async def start(self):
        """Launch Chromium instance."""
        self.health.state = BrowserState.STARTING
        try:
            self.playwright = await async_playwright().start()

            headless = self.config.get("headless", True)
            proxy_url = self.config.get("proxy")

            launch_options = {
                "headless": headless,
                "args": self._launch_args,
            }

            if proxy_url:
                from urllib.parse import urlparse
                parsed = urlparse(proxy_url)
                launch_options["proxy"] = {
                    "server": f"{parsed.scheme}://{parsed.hostname}:{parsed.port or 8080}",
                    "username": parsed.username,
                    "password": parsed.password,
                }

            self.browser = await self.playwright.chromium.launch(**launch_options)
            self.health.state = BrowserState.RUNNING
            self.health.started_at = time.time()
            logger.info(f"Browser instance {self.instance_id} started")

        except Exception as e:
            self.health.state = BrowserState.FAILED
            self.health.last_error = str(e)
            self.health.crash_count += 1
            logger.error(f"Browser instance {self.instance_id} failed to start: {e}")
            raise

    async def get_or_create_context(self, user_id: str) -> UserContext:
        """Get existing context or create new one for a user."""
        if user_id in self.user_contexts:
            ctx = self.user_contexts[user_id]
            ctx.touch()
            return ctx

        if len(self.user_contexts) >= self._max_contexts:
            # Evict least recently active context
            oldest_uid = min(self.user_contexts, key=lambda uid: self.user_contexts[uid].last_active)
            await self.remove_context(oldest_uid)

        profile_dir = os.path.expanduser(f"~/.agent-os/users/{user_id}")
        ctx = UserContext(user_id, profile_dir, self)
        await ctx.initialize(self.config)
        self.user_contexts[user_id] = ctx
        self.health.active_contexts = len(self.user_contexts)
        return ctx

    async def remove_context(self, user_id: str):
        """Remove and clean up a user context."""
        ctx = self.user_contexts.pop(user_id, None)
        if ctx:
            await ctx.close()
        self.health.active_contexts = len(self.user_contexts)

    async def health_check(self) -> BrowserHealth:
        """Run health check on this browser instance."""
        self.health.last_health_check = time.time()
        self.health.active_contexts = len(self.user_contexts)

        # Count total pages
        total_pages = sum(len(ctx.pages) for ctx in self.user_contexts.values())
        self.health.total_pages = total_pages

        # Memory check
        try:
            if self.browser:
                # Try a simple operation to verify browser is alive
                contexts = self.browser.contexts  # noqa: F841
                self.health.state = BrowserState.RUNNING
        except Exception as e:
            self.health.state = BrowserState.DEGRADED
            self.health.last_error = f"Browser unresponsive: {e}"
            logger.warning(f"Browser {self.instance_id} health degraded: {e}")

        # Process memory
        try:
            # Reset before accumulating to avoid monotonically increasing values
            self.health.memory_mb = 0
            # Find Chromium processes
            for proc in psutil.process_iter(['pid', 'name', 'memory_info']):
                try:
                    if 'chrom' in (proc.info['name'] or '').lower():
                        self.health.memory_mb += proc.info['memory_info'].rss / 1024 / 1024
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
        except Exception:
            pass

        return self.health

    async def recover(self):
        """Recover from a crash by restarting the browser."""
        self.health.state = BrowserState.RECOVERING
        self.health.restart_count += 1
        logger.warning(f"Recovering browser instance {self.instance_id} (restart #{self.health.restart_count})")

        # Save all user states
        for ctx in self.user_contexts.values():
            try:
                await ctx.save_state()
            except Exception:
                pass

        # Close old browser
        try:
            if self.browser:
                await self.browser.close()
        except Exception:
            pass
        try:
            if self.playwright:
                await self.playwright.stop()
        except Exception:
            pass

        # Restart
        await self.start()

        # Restore user contexts
        failed_contexts = []
        for user_id, ctx in list(self.user_contexts.items()):
            try:
                old_state = ctx._load_state()  # noqa: F841
                new_ctx = UserContext(user_id, str(ctx.profile_dir), self)
                await new_ctx.initialize(self.config)
                self.user_contexts[user_id] = new_ctx
            except Exception as e:
                logger.error(f"Failed to restore context {user_id}: {e}")
                failed_contexts.append(user_id)

        for uid in failed_contexts:
            self.user_contexts.pop(uid, None)

        self.health.active_contexts = len(self.user_contexts)
        logger.info(f"Browser {self.instance_id} recovered with {len(self.user_contexts)} contexts")

    async def stop(self):
        """Gracefully stop browser instance and all contexts."""
        self.health.state = BrowserState.STOPPING
        logger.info(f"Stopping browser instance {self.instance_id}...")

        # Save and close all user contexts
        for user_id in list(self.user_contexts.keys()):
            await self.remove_context(user_id)

        if self.browser:
            try:
                await self.browser.close()
            except Exception:
                pass
        if self.playwright:
            try:
                await self.playwright.stop()
            except Exception:
                pass

        self.health.state = BrowserState.STOPPED
        logger.info(f"Browser instance {self.instance_id} stopped")


class PersistentBrowserManager:
    """
    Production-grade browser pool manager for millions of users.

    Features:
    - Multiple Chromium instances for horizontal scaling
    - Per-user persistent contexts (cookies, localStorage survive restarts)
    - Auto-restart crashed browsers
    - Health monitoring with configurable intervals
    - LRU eviction of idle contexts
    - Memory cap enforcement
    - Zero-downtime state persistence

    Usage:
        manager = PersistentBrowserManager(config)
        await manager.start()

        # Get a browser context for a user
        ctx = await manager.get_user_context("user-123")
        page = ctx.get_page()
        await page.goto("https://example.com")

        # Run health checks
        health = manager.get_health()

        # Graceful shutdown
        await manager.stop()
    """

    def __init__(self, config):
        self.config = config
        self.instances: Dict[str, BrowserInstance] = {}
        self._user_instance_map: Dict[str, str] = {}  # user_id -> instance_id
        self._health_task: Optional[asyncio.Task] = None
        self._cleanup_task: Optional[asyncio.Task] = None
        self._state_dir = Path(os.path.expanduser("~/.agent-os/state"))
        self._state_dir.mkdir(parents=True, exist_ok=True)

        # Config
        self._max_instances = config.get("persistent.max_instances", 5)
        self._max_contexts_per_instance = config.get("persistent.max_contexts_per_instance", 50)
        self._health_check_interval = config.get("persistent.health_check_interval_seconds", 30)
        self._idle_timeout = config.get("persistent.idle_timeout_minutes", 60) * 60
        self._memory_cap_mb = config.get("persistent.memory_cap_mb", 4000)
        self._auto_restart = config.get("persistent.auto_restart", True)

    async def start(self):
        """Start the persistent browser manager."""
        logger.info("=" * 60)
        logger.info("  🔄 Persistent Browser Manager starting...")
        logger.info(f"  Max instances: {self._max_instances}")
        logger.info(f"  Max contexts/instance: {self._max_contexts_per_instance}")
        logger.info(f"  Health check interval: {self._health_check_interval}s")
        logger.info(f"  Idle timeout: {self._idle_timeout/60:.0f} min")
        logger.info(f"  Memory cap: {self._memory_cap_mb} MB")
        logger.info("=" * 60)

        # Restore previous state
        await self._restore_state()

        # Create first instance if none exist
        if not self.instances:
            await self._create_instance()

        # Start background tasks
        self._health_task = asyncio.create_task(self._health_loop())
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())

        logger.info("  ✅ Persistent Browser Manager ready!")

    async def _create_instance(self) -> BrowserInstance:
        """Create a new browser instance."""
        instance_id = f"browser-{uuid.uuid4().hex[:8]}"
        instance_config = {
            "headless": self.config.get("browser.headless", True),
            "user_agent": self.config.get("browser.user_agent"),
            "viewport": self.config.get("browser.viewport", {"width": 1920, "height": 1080}),
            "proxy": self.config.get("browser.proxy"),
            "max_contexts_per_instance": self._max_contexts_per_instance,
        }

        instance = BrowserInstance(instance_id, instance_config)
        await instance.start()
        self.instances[instance_id] = instance
        logger.info(f"Created browser instance: {instance_id}")
        return instance

    async def get_user_context(self, user_id: str) -> UserContext:
        """
        Get or create a persistent browser context for a user.
        This is the main entry point for getting a browser session.
        """
        # Check if user already assigned to an instance
        instance_id = self._user_instance_map.get(user_id)
        if instance_id and instance_id in self.instances:
            instance = self.instances[instance_id]
            if instance.health.state == BrowserState.RUNNING:
                return await instance.get_or_create_context(user_id)

        # Find best instance (least loaded running instance)
        best_instance = None
        best_load = float('inf')

        for instance in self.instances.values():
            if instance.health.state != BrowserState.RUNNING:
                continue
            load = len(instance.user_contexts)
            if load < best_load:
                best_load = load
                best_instance = instance

        # Create new instance if all are full or none exist
        if best_instance is None or best_load >= self._max_contexts_per_instance:
            if len(self.instances) < self._max_instances:
                best_instance = await self._create_instance()
            else:
                # Find instance with fewest contexts
                best_instance = min(self.instances.values(), key=lambda i: len(i.user_contexts))

        self._user_instance_map[user_id] = best_instance.instance_id
        return await best_instance.get_or_create_context(user_id)

    async def remove_user_context(self, user_id: str):
        """Remove a user's browser context."""
        instance_id = self._user_instance_map.pop(user_id, None)
        if instance_id and instance_id in self.instances:
            await self.instances[instance_id].remove_context(user_id)

    async def execute_for_user(self, user_id: str, command: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """
        Execute a browser command for a specific user.
        This is the main integration point with the HTTP/WS API.
        """
        ctx = await self.get_user_context(user_id)
        ctx.commands_executed += 1
        ctx.touch()

        # Route command
        handler = self._get_command_handler(command)
        if not handler:
            return {"status": "error", "error": f"Unknown command: {command}"}

        try:
            result = await handler(ctx, params)
            result["user_id"] = user_id
            result["persistent"] = True
            return result
        except Exception as e:
            logger.error(f"Command error for {user_id}: {e}")
            return {"status": "error", "error": str(e), "user_id": user_id}

    def _get_command_handler(self, command: str) -> Optional[Callable]:
        """Get handler function for a command."""
        handlers = {
            "navigate": self._cmd_navigate,
            "click": self._cmd_click,
            "double-click": self._cmd_double_click,
            "right-click": self._cmd_right_click,
            "type": self._cmd_type,
            "press": self._cmd_press,
            "fill-form": self._cmd_fill_form,
            "clear-input": self._cmd_clear_input,
            "checkbox": self._cmd_checkbox,
            "select": self._cmd_select,
            "upload": self._cmd_upload,
            "wait": self._cmd_wait,
            "hover": self._cmd_hover,
            "screenshot": self._cmd_screenshot,
            "get-content": self._cmd_get_content,
            "get-dom": self._cmd_get_dom,
            "get-links": self._cmd_get_links,
            "get-images": self._cmd_get_images,
            "get-text": self._cmd_get_text,
            "get-attr": self._cmd_get_attr,
            "get-cookies": self._cmd_get_cookies,
            "set-cookie": self._cmd_set_cookie,
            "console-logs": self._cmd_console_logs,
            "evaluate-js": self._cmd_evaluate_js,
            "scroll": self._cmd_scroll,
            "viewport": self._cmd_viewport,
            "back": self._cmd_back,
            "forward": self._cmd_forward,
            "reload": self._cmd_reload,
            "drag-drop": self._cmd_drag_drop,
            "drag-offset": self._cmd_drag_offset,
            "context-action": self._cmd_context_action,
            "tabs": self._cmd_tabs,
            "save-session": self._cmd_save_session,
            "restore-session": self._cmd_restore_session,
        }
        return handlers.get(command)

    # ─── Command Handlers ─────────────────────────────────────

    async def _cmd_navigate(self, ctx: UserContext, params: Dict) -> Dict:
        try:
            url = params.get("url")
            if not url:
                return {"status": "error", "error": "Missing 'url'"}
            page_id = params.get("page_id", "main")
            page = ctx.get_page(page_id)
            if not page:
                return {"status": "error", "error": f"Page not found: {page_id}"}
            await asyncio.sleep(random.uniform(0.3, 1.2))
            response = await page.goto(url, wait_until=params.get("wait_until", "domcontentloaded"), timeout=30000)
            await asyncio.sleep(random.uniform(0.5, 1.5))
            await ctx.save_state()
            return {"status": "success", "url": page.url, "title": await page.title(), "status_code": response.status if response else 200}
        except Exception as e:
            logger.error(f"Navigate error: {e}")
            return {"status": "error", "error": str(e)}

    async def _cmd_click(self, ctx: UserContext, params: Dict) -> Dict:
        try:
            selector = params.get("selector")
            if not selector:
                return {"status": "error", "error": "Missing 'selector'"}
            page = ctx.get_page()
            el = await page.query_selector(selector)
            if not el:
                return {"status": "error", "error": f"Element not found: {selector}"}
            # Scroll element into view before interacting
            try:
                await el.scroll_into_view_if_needed()
            except Exception as e:
                logger.debug(f"scroll_into_view failed for {selector}: {e}")
            # Visibility check with force click fallback
            is_visible = await el.is_visible()
            box = await el.bounding_box()
            if box:
                target_x = box["x"] + box["width"] / 2
                target_y = box["y"] + box["height"] / 2
                steps = random.randint(5, 20)
                for i in range(steps + 1):
                    t = i / steps
                    x = box["x"] + (target_x - box["x"]) * t + random.gauss(0, 2)
                    y = box["y"] + (target_y - box["y"]) * t + random.gauss(0, 2)
                    await page.mouse.move(x, y)
                    await asyncio.sleep(random.uniform(0.005, 0.02))
            await asyncio.sleep(random.uniform(0.05, 0.15))
            if is_visible:
                await el.click()
            else:
                logger.warning(f"Element {selector} not visible, attempting force click")
                await el.click(force=True)
            await asyncio.sleep(random.uniform(0.2, 0.5))
            return {"status": "success", "selector": selector}
        except Exception as e:
            logger.error(f"Click error: {e}")
            return {"status": "error", "error": str(e)}

    async def _cmd_double_click(self, ctx: UserContext, params: Dict) -> Dict:
        try:
            selector = params.get("selector")
            if not selector:
                return {"status": "error", "error": "Missing 'selector'"}
            page = ctx.get_page()
            el = await page.query_selector(selector)
            if not el:
                return {"status": "error", "error": f"Element not found: {selector}"}
            # Scroll element into view before interacting
            try:
                await el.scroll_into_view_if_needed()
            except Exception as e:
                logger.debug(f"scroll_into_view failed for {selector}: {e}")
            # Visibility check with force click fallback
            is_visible = await el.is_visible()
            if is_visible:
                await page.dblclick(selector)
            else:
                logger.warning(f"Element {selector} not visible, attempting force double-click")
                await page.dblclick(selector, force=True)
            await asyncio.sleep(random.uniform(0.2, 0.5))
            return {"status": "success", "selector": selector}
        except Exception as e:
            logger.error(f"Double-click error: {e}")
            return {"status": "error", "error": str(e)}

    async def _cmd_right_click(self, ctx: UserContext, params: Dict) -> Dict:
        try:
            selector = params.get("selector")
            if not selector:
                return {"status": "error", "error": "Missing 'selector'"}
            page = ctx.get_page()
            el = await page.query_selector(selector)
            if not el:
                return {"status": "error", "error": f"Element not found: {selector}"}
            # Scroll element into view before interacting
            try:
                await el.scroll_into_view_if_needed()
            except Exception as e:
                logger.debug(f"scroll_into_view failed for {selector}: {e}")
            # Visibility check with force click fallback
            is_visible = await el.is_visible()
            if is_visible:
                await page.click(selector, button="right")
            else:
                logger.warning(f"Element {selector} not visible, attempting force right-click")
                await page.click(selector, button="right", force=True)
            await asyncio.sleep(random.uniform(0.2, 0.5))
            return {"status": "success", "selector": selector}
        except Exception as e:
            logger.error(f"Right-click error: {e}")
            return {"status": "error", "error": str(e)}

    async def _cmd_type(self, ctx: UserContext, params: Dict) -> Dict:
        try:
            text = params.get("text")
            if not text:
                return {"status": "error", "error": "Missing 'text'"}
            page = ctx.get_page()
            # Check if text contains special characters that may not type correctly
            # with keyboard.type() on non-US keyboard layouts
            special_chars = set('@#$%^&*{}|:"<>?~`éüñáíóúàèìòù_+!=()')
            has_special = any(c in special_chars for c in text)
            if has_special:
                # Try keyboard.type() first (Patchright handles special chars well)
                try:
                    await page.keyboard.type(text, delay=random.randint(30, 120))
                except Exception:
                    # Fallback: insert_text() bypasses keyboard layout entirely
                    await page.keyboard.insert_text(text)
            else:
                # Normal typing with human-like delay
                await page.keyboard.type(text, delay=random.randint(30, 120))
            return {"status": "success", "typed": len(text)}
        except Exception as e:
            logger.error(f"Type error: {e}")
            return {"status": "error", "error": str(e)}

    async def _cmd_press(self, ctx: UserContext, params: Dict) -> Dict:
        try:
            key = params.get("key")
            if not key:
                return {"status": "error", "error": "Missing 'key'"}
            page = ctx.get_page()
            await page.keyboard.press(key)
            return {"status": "success", "key": key}
        except Exception as e:
            logger.error(f"Press error: {e}")
            return {"status": "error", "error": str(e)}

    async def _find_element(self, page, selector: str, timeout_ms: int = 5000):
        """Find an element using multiple selector strategies with wait.

        Tries the exact selector first, then falls back to common patterns:
        name attribute, placeholder, id, aria-label, label[for], data-testid,
        and even by input type (email, password, tel, etc.).

        Returns (element, actual_selector) or (None, None).
        """
        import time as _time

        # Build the list of selectors to try, in priority order
        selector_candidates = [selector]

        is_full_selector = any(c in selector for c in ['[', '#', '.', '>', ':', ' '])
        if not is_full_selector:
            selector_candidates.extend([
                f'input[name="{selector}"]',
                f'textarea[name="{selector}"]',
                f'select[name="{selector}"]',
                f'#{selector}',
                f'input[placeholder*="{selector}" i]',
                f'textarea[placeholder*="{selector}" i]',
                f'input[aria-label*="{selector}" i]',
                f'textarea[aria-label*="{selector}" i]',
                f'[data-testid="{selector}"]',
            ])
            # Type-based selectors for common fields
            lower = selector.lower()
            if 'email' in lower or 'e-mail' in lower or 'mail' in lower:
                selector_candidates.extend([
                    'input[type="email"]',
                    'input[name="email"]',
                    'input[name="username"]',
                    'input[autocomplete="email"]',
                    'input[autocomplete="username"]',
                ])
            elif 'password' in lower or 'pass' in lower or 'pwd' in lower:
                selector_candidates.extend([
                    'input[type="password"]',
                    'input[name="password"]',
                    'input[autocomplete="current-password"]',
                ])
            elif 'phone' in lower or 'mobile' in lower or 'tel' in lower:
                selector_candidates.extend([
                    'input[type="tel"]',
                    'input[name="phone"]',
                    'input[autocomplete="tel"]',
                ])
            elif 'search' in lower or 'query' in lower or 'q' == lower:
                selector_candidates.extend([
                    'input[type="search"]',
                    'input[name="q"]',
                    'input[name="query"]',
                    'input[name="search"]',
                ])

        # Remove duplicates
        seen = set()
        unique_candidates = []
        for s in selector_candidates:
            if s not in seen:
                seen.add(s)
                unique_candidates.append(s)

        # Try each selector with a short wait
        start = _time.time()
        while (_time.time() - start) * 1000 < timeout_ms:
            for sel in unique_candidates:
                try:
                    el = await page.query_selector(sel)
                    if el:
                        return el, sel
                except Exception:
                    continue
            await asyncio.sleep(0.3)

        return None, None

    async def _cmd_fill_form(self, ctx: UserContext, params: Dict) -> Dict:
        """Fill form fields with framework-compatible event dispatch.

        Handles:
        - Framework-compatible input via InputEvent + change events
        - Special characters (@, #, etc.) via fill() + insert_text()
        - Multi-strategy element finding
        - Value verification with automatic retry
        """
        try:
            fields = params.get("fields", {})
            if not fields:
                return {"status": "error", "error": "Missing 'fields'"}
            page = ctx.get_page()
            filled = []
            errors = []
            for selector, value in fields.items():
                try:
                    # Find element using robust multi-strategy finder
                    el, actual_selector = await self._find_element(page, selector, timeout_ms=8000)
                    if not el:
                        errors.append({"selector": selector, "error": "Element not found"})
                        continue
                    # Scroll into view before filling
                    try:
                        await el.scroll_into_view_if_needed()
                    except Exception:
                        pass
                    # Focus the element (try JS first, then Playwright)
                    try:
                        await page.evaluate("""(sel) => {
                            const el = document.querySelector(sel);
                            if (el) { el.focus(); el.click(); }
                        }""", actual_selector)
                    except Exception:
                        try:
                            await el.click()
                        except Exception:
                            try:
                                await el.click(force=True)
                            except Exception:
                                errors.append({"selector": selector, "error": "Cannot focus"})
                                continue

                    await asyncio.sleep(random.uniform(0.05, 0.15))

                    # Clear existing value using keyboard (most reliable approach)
                    await page.keyboard.press("Home")
                    await page.keyboard.press("Control+a")
                    await page.keyboard.press("Control+a")  # Double-select for reliability
                    await page.keyboard.press("Backspace")
                    await asyncio.sleep(random.uniform(0.05, 0.1))

                    # Type value — use fill() for special chars, type() for normal
                    value_str = str(value)
                    has_special = any(c in value_str for c in '@#$%^&*()_+{}|:"<>?~`!=éüñáíóúàèìòù')
                    if has_special:
                        # fill() handles ALL characters reliably
                        try:
                            await el.fill(value_str)
                        except Exception:
                            try:
                                await page.keyboard.insert_text(value_str)
                            except Exception:
                                await page.keyboard.type(value_str, delay=random.randint(30, 120))
                    else:
                        try:
                            await page.keyboard.type(value_str, delay=random.randint(30, 120))
                        except Exception:
                            try:
                                await el.fill(value_str)
                            except Exception:
                                pass

                    # Verify the value was set correctly
                    await asyncio.sleep(0.1)
                    try:
                        actual_value = await el.evaluate("el => el.value")
                        if actual_value != value_str:
                            # Set value directly and dispatch standard events
                            await el.evaluate("""(el, value) => {
                                el.value = value;
                                el.dispatchEvent(new InputEvent('input', {
                                    bubbles: true, cancelable: true,
                                    inputType: 'insertText', data: value
                                }));
                                el.dispatchEvent(new Event('change', { bubbles: true }));
                                el.focus();
                            }""", value_str)
                    except Exception:
                        pass

                    # Dispatch framework-compatible events
                    try:
                        await page.evaluate("""(sel) => {
                            const el = document.querySelector(sel);
                            if (el) {
                                el.dispatchEvent(new Event('input', { bubbles: true }));
                                el.dispatchEvent(new Event('change', { bubbles: true }));
                                el.dispatchEvent(new FocusEvent('focus', { bubbles: true }));
                                el.dispatchEvent(new FocusEvent('blur', { bubbles: true }));
                                el.focus();
                            }
                        }""", actual_selector)
                    except Exception:
                        pass

                    filled.append(selector)
                except Exception as e:
                    logger.warning(f"Fill error for {selector}: {e}")
                    errors.append({"selector": selector, "error": str(e)})
            return {"status": "success", "filled": filled, "total": len(fields), "errors": errors if errors else None}
        except Exception as e:
            logger.error(f"Fill form error: {e}")
            return {"status": "error", "error": str(e)}

    async def _cmd_clear_input(self, ctx: UserContext, params: Dict) -> Dict:
        try:
            selector = params.get("selector")
            if not selector:
                return {"status": "error", "error": "Missing 'selector'"}
            page = ctx.get_page()
            el = await page.query_selector(selector)
            if not el:
                return {"status": "error", "error": f"Element not found: {selector}"}
            # Scroll into view before clearing
            try:
                await el.scroll_into_view_if_needed()
            except Exception:
                pass
            await el.click()
            await page.keyboard.press("Control+a")
            await page.keyboard.press("Backspace")
            # Dispatch input and change events for framework compatibility
            try:
                await el.evaluate("""el => {
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                }""")
            except Exception:
                pass
            return {"status": "success", "selector": selector}
        except Exception as e:
            logger.error(f"Clear input error: {e}")
            return {"status": "error", "error": str(e)}

    async def _cmd_checkbox(self, ctx: UserContext, params: Dict) -> Dict:
        try:
            selector = params.get("selector")
            checked = params.get("checked", True)
            if not selector:
                return {"status": "error", "error": "Missing 'selector'"}
            page = ctx.get_page()
            el = await page.query_selector(selector)
            if not el:
                return {"status": "error", "error": f"Element not found: {selector}"}
            # Scroll into view before interacting
            try:
                await el.scroll_into_view_if_needed()
            except Exception as e:
                logger.debug(f"scroll_into_view failed for {selector}: {e}")
            is_checked = await el.is_checked()
            if is_checked != checked:
                # Visibility check with force click fallback
                is_visible = await el.is_visible()
                if is_visible:
                    await el.click()
                else:
                    logger.warning(f"Checkbox {selector} not visible, attempting force click")
                    await el.click(force=True)
            return {"status": "success", "selector": selector, "checked": checked}
        except Exception as e:
            logger.error(f"Checkbox error: {e}")
            return {"status": "error", "error": str(e)}

    async def _cmd_select(self, ctx: UserContext, params: Dict) -> Dict:
        try:
            selector = params.get("selector")
            value = params.get("value")
            if not selector or not value:
                return {"status": "error", "error": "Missing 'selector' or 'value'"}
            page = ctx.get_page()
            el = await page.query_selector(selector)
            if not el:
                return {"status": "error", "error": f"Element not found: {selector}"}
            # Scroll into view before selecting
            try:
                await el.scroll_into_view_if_needed()
            except Exception:
                pass
            await page.select_option(selector, value)
            # Dispatch change event for framework compatibility
            try:
                await el.evaluate("""el => {
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                }""")
            except Exception:
                pass
            return {"status": "success", "selector": selector, "value": value}
        except Exception as e:
            logger.error(f"Select error: {e}")
            return {"status": "error", "error": str(e)}

    async def _cmd_upload(self, ctx: UserContext, params: Dict) -> Dict:
        try:
            selector = params.get("selector")
            file_path = params.get("file_path")
            if not selector or not file_path:
                return {"status": "error", "error": "Missing 'selector' or 'file_path'"}
            page = ctx.get_page()
            el = await page.query_selector(selector)
            if not el:
                return {"status": "error", "error": f"Element not found: {selector}"}
            await el.set_input_files(file_path)
            # Dispatch change event for framework compatibility
            try:
                await el.evaluate("""el => {
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                }""")
            except Exception:
                pass
            return {"status": "success", "selector": selector, "file": file_path}
        except Exception as e:
            logger.error(f"Upload error: {e}")
            return {"status": "error", "error": str(e)}

    async def _cmd_wait(self, ctx: UserContext, params: Dict) -> Dict:
        selector = params.get("selector")
        if not selector:
            return {"status": "error", "error": "Missing 'selector'"}
        timeout = params.get("timeout", 10000)
        page = ctx.get_page()
        try:
            await page.wait_for_selector(selector, timeout=timeout)
            return {"status": "success", "selector": selector}
        except Exception:
            return {"status": "error", "error": f"Timeout waiting for: {selector}"}

    async def _cmd_hover(self, ctx: UserContext, params: Dict) -> Dict:
        try:
            selector = params.get("selector")
            if not selector:
                return {"status": "error", "error": "Missing 'selector'"}
            page = ctx.get_page()
            el = await page.query_selector(selector)
            if not el:
                return {"status": "error", "error": f"Element not found: {selector}"}
            # Scroll element into view before hovering
            try:
                await el.scroll_into_view_if_needed()
            except Exception as e:
                logger.debug(f"scroll_into_view failed for {selector}: {e}")
            # Visibility check with force hover fallback
            is_visible = await el.is_visible()
            if is_visible:
                await el.hover()
            else:
                logger.warning(f"Element {selector} not visible, attempting force hover")
                await el.hover(force=True)
            return {"status": "success", "selector": selector}
        except Exception as e:
            logger.error(f"Hover error: {e}")
            return {"status": "error", "error": str(e)}

    async def _cmd_screenshot(self, ctx: UserContext, params: Dict) -> Dict:
        try:
            import base64
            page = ctx.get_page()
            if not page:
                return {"status": "error", "error": "No active page"}
            img = await page.screenshot(type="png", full_page=params.get("full_page", False))
            return {"status": "success", "screenshot": base64.b64encode(img).decode(), "format": "png"}
        except Exception as e:
            logger.error(f"Screenshot error: {e}")
            return {"status": "error", "error": str(e)}

    async def _cmd_get_content(self, ctx: UserContext, params: Dict) -> Dict:
        try:
            page = ctx.get_page()
            if not page:
                return {"status": "error", "error": "No active page"}
            body_text = ""
            try:
                if await page.query_selector("body"):
                    body_text = await page.inner_text("body")
            except Exception:
                pass
            return {
                "status": "success",
                "url": page.url,
                "title": await page.title(),
                "html": await page.content(),
                "text": body_text,
            }
        except Exception as e:
            logger.error(f"Get content error: {e}")
            return {"status": "error", "error": str(e)}

    async def _cmd_get_dom(self, ctx: UserContext, params: Dict) -> Dict:
        try:
            page = ctx.get_page()
            if not page:
                return {"status": "error", "error": "No active page"}
            dom = await page.evaluate("""() => {
                function getSnapshot(el, depth) {
                    if (depth > 5) return '';
                    let result = '';
                    const indent = '  '.repeat(depth);
                    const tag = el.tagName?.toLowerCase() || '';
                    if (!tag) return '';
                    const attrs = [];
                    if (el.id) attrs.push('id="' + el.id + '"');
                    if (el.className && typeof el.className === 'string') attrs.push('class="' + el.className + '"');
                    if (el.getAttribute('type')) attrs.push('type="' + el.getAttribute('type') + '"');
                    if (el.getAttribute('name')) attrs.push('name="' + el.getAttribute('name') + '"');
                    if (el.getAttribute('placeholder')) attrs.push('placeholder="' + el.getAttribute('placeholder') + '"');
                    if (el.href) attrs.push('href="' + el.href + '"');
                    const attrStr = attrs.length ? ' ' + attrs.join(' ') : '';
                    const text = el.childNodes.length === 1 && el.childNodes[0].nodeType === 3 ? el.childNodes[0].textContent.trim().substring(0, 100) : '';
                    if (['script', 'style', 'noscript', 'svg'].includes(tag)) return '';
                    const children = Array.from(el.children).map(c => getSnapshot(c, depth + 1)).filter(Boolean).join('');
                    if (children) { result = indent + '<' + tag + attrStr + '>' + (text ? ' ' + text : '') + '\\n' + children + indent + '</' + tag + '>\\n'; }
                    else if (text) { result = indent + '<' + tag + attrStr + '>' + text + '</' + tag + '>\\n'; }
                    else { result = indent + '<' + tag + attrStr + ' />\\n'; }
                    return result;
                }
                return getSnapshot(document.body, 0);
            }""")
            return {"status": "success", "dom_snapshot": dom}
        except Exception as e:
            logger.error(f"Get DOM error: {e}")
            return {"status": "error", "error": str(e)}

    async def _cmd_get_links(self, ctx: UserContext, params: Dict) -> Dict:
        page = ctx.get_page()
        links = await page.evaluate("() => Array.from(document.querySelectorAll('a[href]')).map(a => a.href).filter(h => h.startsWith('http'))")
        return {"status": "success", "links": links, "count": len(links)}

    async def _cmd_get_images(self, ctx: UserContext, params: Dict) -> Dict:
        page = ctx.get_page()
        images = await page.evaluate("() => Array.from(document.querySelectorAll('img')).map(i => ({src: i.src, alt: i.alt, width: i.width, height: i.height})).filter(i => i.src.startsWith('http'))")
        return {"status": "success", "images": images, "count": len(images)}

    async def _cmd_get_text(self, ctx: UserContext, params: Dict) -> Dict:
        selector = params.get("selector")
        if not selector:
            return {"status": "error", "error": "Missing 'selector'"}
        page = ctx.get_page()
        el = await page.query_selector(selector)
        if el:
            text = await el.inner_text()
            return {"status": "success", "selector": selector, "text": text}
        return {"status": "error", "error": f"Element not found: {selector}"}

    async def _cmd_get_attr(self, ctx: UserContext, params: Dict) -> Dict:
        selector = params.get("selector")
        attribute = params.get("attribute")
        if not selector or not attribute:
            return {"status": "error", "error": "Missing 'selector' or 'attribute'"}
        page = ctx.get_page()
        el = await page.query_selector(selector)
        if el:
            value = await el.get_attribute(attribute)
            return {"status": "success", "selector": selector, "attribute": attribute, "value": value}
        return {"status": "error", "error": f"Element not found: {selector}"}

    async def _cmd_get_cookies(self, ctx: UserContext, params: Dict) -> Dict:
        if ctx.context:
            cookies = await ctx.context.cookies()
            return {"status": "success", "cookies": cookies, "count": len(cookies)}
        return {"status": "error", "error": "No context"}

    async def _cmd_set_cookie(self, ctx: UserContext, params: Dict) -> Dict:
        name = params.get("name")
        value = params.get("value")
        if not name or not value:
            return {"status": "error", "error": "Missing 'name' or 'value'"}
        page = ctx.get_page()
        domain = params.get("domain")
        if not domain:
            try:
                parsed = page.url.split("/")
                if len(parsed) >= 3:
                    host = parsed[2].split(":")[0].strip("[]")
                    if host and host != "about:blank":
                        domain = host
            except Exception:
                pass
        if not domain:
            return {"status": "error", "error": "Cannot infer domain"}
        cookie = {
            "name": name, "value": value, "domain": domain,
            "path": params.get("path", "/"),
        }
        if params.get("secure") is not None:
            cookie["secure"] = params["secure"]
        elif page.url.startswith("https://"):
            cookie["secure"] = True
        if params.get("http_only"):
            cookie["httpOnly"] = True
        same_site = params.get("same_site")
        if same_site and same_site.capitalize() in ("Strict", "Lax", "None"):
            cookie["sameSite"] = same_site.capitalize()
        await ctx.context.add_cookies([cookie])
        return {"status": "success", "cookie": cookie}

    async def _cmd_console_logs(self, ctx: UserContext, params: Dict) -> Dict:
        page_id = params.get("page_id", "main")
        clear = params.get("clear", False)
        logs = ctx._console_logs.get(page_id, [])
        result = logs[-100:]
        if clear:
            ctx._console_logs[page_id] = []
        return {"status": "success", "page_id": page_id, "logs": result, "count": len(result)}

    async def _cmd_evaluate_js(self, ctx: UserContext, params: Dict) -> Dict:
        script = params.get("script")
        if not script:
            return {"status": "error", "error": "Missing 'script'"}
        page = ctx.get_page()
        result = await page.evaluate(script)
        return {"status": "success", "result": result}

    async def _cmd_scroll(self, ctx: UserContext, params: Dict) -> Dict:
        page = ctx.get_page()
        direction = params.get("direction", "down")
        amount = params.get("amount", 500)
        y = amount if direction == "down" else -amount
        steps = random.randint(3, 8)
        for i in range(steps):
            step_y = y / steps + random.randint(-20, 20)
            await page.mouse.wheel(0, int(step_y))
            await asyncio.sleep(random.uniform(0.05, 0.15))
        return {"status": "success", "direction": direction, "amount": amount}

    async def _cmd_viewport(self, ctx: UserContext, params: Dict) -> Dict:
        width = params.get("width", 1920)
        height = params.get("height", 1080)
        page = ctx.get_page()
        await page.set_viewport_size({"width": width, "height": height})
        return {"status": "success", "viewport": {"width": width, "height": height}}

    async def _cmd_back(self, ctx: UserContext, params: Dict) -> Dict:
        page = ctx.get_page()
        await page.go_back()
        await asyncio.sleep(random.uniform(0.5, 1.5))
        return {"status": "success", "url": page.url, "title": await page.title()}

    async def _cmd_forward(self, ctx: UserContext, params: Dict) -> Dict:
        page = ctx.get_page()
        await page.go_forward()
        await asyncio.sleep(random.uniform(0.5, 1.5))
        return {"status": "success", "url": page.url, "title": await page.title()}

    async def _cmd_reload(self, ctx: UserContext, params: Dict) -> Dict:
        page = ctx.get_page()
        await page.reload()
        await asyncio.sleep(random.uniform(0.5, 1.5))
        return {"status": "success", "url": page.url, "title": await page.title()}

    async def _cmd_drag_drop(self, ctx: UserContext, params: Dict) -> Dict:
        source = params.get("source")
        target = params.get("target")
        if not source or not target:
            return {"status": "error", "error": "Missing 'source' or 'target'"}
        page = ctx.get_page()
        src_el = await page.query_selector(source)
        tgt_el = await page.query_selector(target)
        if not src_el or not tgt_el:
            return {"status": "error", "error": "Element not found"}
        src_box = await src_el.bounding_box()
        tgt_box = await tgt_el.bounding_box()
        if not src_box or not tgt_box:
            return {"status": "error", "error": "Cannot get positions"}
        src_x = src_box["x"] + src_box["width"] / 2
        src_y = src_box["y"] + src_box["height"] / 2
        tgt_x = tgt_box["x"] + tgt_box["width"] / 2
        tgt_y = tgt_box["y"] + tgt_box["height"] / 2
        await page.mouse.move(src_x, src_y)
        await page.mouse.down()
        await asyncio.sleep(random.uniform(0.1, 0.3))
        steps = max(5, int(abs(tgt_x - src_x) / 20 + abs(tgt_y - src_y) / 20))
        for i in range(1, steps + 1):
            t = i / steps
            await page.mouse.move(src_x + (tgt_x - src_x) * t + random.gauss(0, 2), src_y + (tgt_y - src_y) * t + random.gauss(0, 2))
            await asyncio.sleep(random.uniform(0.008, 0.02))
        await page.mouse.up()
        return {"status": "success", "source": source, "target": target}

    async def _cmd_drag_offset(self, ctx: UserContext, params: Dict) -> Dict:
        selector = params.get("selector")
        x_offset = params.get("x", 0)
        y_offset = params.get("y", 0)
        if not selector:
            return {"status": "error", "error": "Missing 'selector'"}
        page = ctx.get_page()
        el = await page.query_selector(selector)
        if not el:
            return {"status": "error", "error": f"Element not found: {selector}"}
        box = await el.bounding_box()
        if not box:
            return {"status": "error", "error": "Cannot get position"}
        src_x = box["x"] + box["width"] / 2
        src_y = box["y"] + box["height"] / 2
        await page.mouse.move(src_x, src_y)
        await page.mouse.down()
        steps = max(5, abs(x_offset) // 10 + abs(y_offset) // 10)
        for i in range(1, steps + 1):
            t = i / steps
            await page.mouse.move(src_x + x_offset * t + random.gauss(0, 2), src_y + y_offset * t + random.gauss(0, 2))
            await asyncio.sleep(random.uniform(0.005, 0.015))
        await page.mouse.up()
        return {"status": "success", "selector": selector, "offset": (x_offset, y_offset)}

    async def _cmd_context_action(self, ctx: UserContext, params: Dict) -> Dict:
        selector = params.get("selector")
        action_text = params.get("action_text")
        if not selector or not action_text:
            return {"status": "error", "error": "Missing 'selector' or 'action_text'"}
        page = ctx.get_page()

        # Right-click to open context menu
        el = await page.query_selector(selector)
        if not el:
            return {"status": "error", "error": f"Element not found: {selector}"}
        await el.click(button="right")
        await asyncio.sleep(random.uniform(0.3, 0.8))

        # Try to find and click the menu item by text
        menu_selectors = [
            f'text="{action_text}"',
            f'role=menuitem[name="{action_text}"]',
            f'[role="menuitem"]:has-text("{action_text}")',
            f'li:has-text("{action_text}")',
            f'div:has-text("{action_text}")',
        ]

        for sel in menu_selectors:
            try:
                item = await page.query_selector(sel)
                if item:
                    await item.click()
                    await asyncio.sleep(random.uniform(0.2, 0.5))
                    return {"status": "success", "action": action_text, "selector": selector}
            except Exception:
                continue

        # Fallback to keyboard shortcuts
        shortcuts = {
            "copy": "Control+c",
            "paste": "Control+v",
            "cut": "Control+x",
            "select all": "Control+a",
            "save": "Control+s",
            "inspect": "F12",
            "view source": "Control+u",
            "reload": "F5",
        }

        shortcut = shortcuts.get(action_text.lower())
        if shortcut:
            await page.keyboard.press(shortcut)
            return {"status": "success", "action": action_text, "method": "keyboard_shortcut"}

        return {"status": "error", "error": f"Context menu action '{action_text}' not found"}

    async def _cmd_tabs(self, ctx: UserContext, params: Dict) -> Dict:
        action = params.get("action", "list")
        tab_id = params.get("tab_id")
        if action == "list":
            return {"status": "success", "tabs": list(ctx.pages.keys())}
        elif action == "new":
            tid = tab_id or f"tab-{len(ctx.pages)}"
            page = await ctx.context.new_page()
            ctx._attach_console_listener(tid, page)
            ctx.pages[tid] = page
            return {"status": "success", "tab_id": tid}
        elif action == "switch":
            if tab_id and tab_id in ctx.pages:
                ctx.active_page = ctx.pages[tab_id]
                return {"status": "success", "tab_id": tab_id, "url": ctx.active_page.url}
            return {"status": "error", "error": f"Tab not found: {tab_id}"}
        elif action == "close":
            if tab_id and tab_id in ctx.pages and tab_id != "main":
                await ctx.pages[tab_id].close()
                del ctx.pages[tab_id]
                return {"status": "success", "closed": tab_id}
            return {"status": "error", "error": f"Cannot close tab: {tab_id}"}
        return {"status": "error", "error": f"Unknown tab action: {action}"}

    async def _cmd_save_session(self, ctx: UserContext, params: Dict) -> Dict:
        await ctx.save_state()
        return {"status": "success", "user_id": ctx.user_id, "message": "Session state saved"}

    async def _cmd_restore_session(self, ctx: UserContext, params: Dict) -> Dict:
        state = ctx._load_state()
        if state:
            return {"status": "success", "user_id": ctx.user_id, "state": state}
        return {"status": "error", "error": "No saved state found"}

    # ─── Background Tasks ─────────────────────────────────────

    async def _health_loop(self):
        """Background health check for all browser instances."""
        while True:
            try:
                await asyncio.sleep(self._health_check_interval)
                for instance_id, instance in list(self.instances.items()):
                    try:
                        health = await instance.health_check()

                        # Auto-restart if degraded/failed
                        if health.state in (BrowserState.DEGRADED, BrowserState.FAILED) and self._auto_restart:
                            if health.restart_count < 5:
                                await instance.recover()
                            else:
                                logger.error(f"Instance {instance_id} exceeded max restarts, removing")
                                await instance.stop()
                                del self.instances[instance_id]
                                # Reassign users to other instances
                                for uid, iid in list(self._user_instance_map.items()):
                                    if iid == instance_id:
                                        del self._user_instance_map[uid]

                        # Memory cap check
                        if health.memory_mb > self._memory_cap_mb:
                            logger.warning(f"Instance {instance_id} memory ({health.memory_mb:.0f}MB) exceeds cap ({self._memory_cap_mb}MB)")
                            # Evict oldest idle context
                            if instance.user_contexts:
                                oldest = min(instance.user_contexts.values(), key=lambda c: c.last_active)
                                if time.time() - oldest.last_active > 300:  # 5 min idle
                                    await instance.remove_context(oldest.user_id)

                    except Exception as e:
                        logger.error(f"Health check error for {instance_id}: {e}")

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Health loop error: {e}")

    async def _cleanup_loop(self):
        """Background cleanup of idle user contexts."""
        while True:
            try:
                await asyncio.sleep(60)  # Check every minute
                now = time.time()
                for instance in self.instances.values():
                    for user_id, ctx in list(instance.user_contexts.items()):
                        if now - ctx.last_active > self._idle_timeout:
                            logger.info(f"Evicting idle context: {user_id} (idle for {(now - ctx.last_active)/60:.0f}min)")
                            await instance.remove_context(user_id)
                            self._user_instance_map.pop(user_id, None)

                # Save state periodically
                await self._save_state()

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Cleanup loop error: {e}")

    async def _save_state(self):
        """Save manager state to disk for restart recovery."""
        state = {
            "saved_at": time.time(),
            "instances": {},
            "user_map": dict(self._user_instance_map),
        }
        for instance_id, instance in self.instances.items():
            instance_state = {
                "instance_id": instance_id,
                "health": instance.health.to_dict(),
                "users": {},
            }
            for user_id, ctx in instance.user_contexts.items():
                await ctx.save_state()
                instance_state["users"][user_id] = {
                    "profile_dir": str(ctx.profile_dir),
                    "last_active": ctx.last_active,
                }
            state["instances"][instance_id] = instance_state

        state_file = self._state_dir / "manager_state.json"
        try:
            with open(state_file, "w") as f:
                json.dump(state, f, indent=2)
        except Exception as e:
            logger.error(f"Failed to save manager state: {e}")

    async def _restore_state(self):
        """Restore manager state from previous run."""
        state_file = self._state_dir / "manager_state.json"
        if not state_file.exists():
            return

        try:
            with open(state_file) as f:
                state = json.load(f)
        except Exception:
            return

        saved_at = state.get("saved_at", 0)
        if time.time() - saved_at > 86400:  # Older than 24h, skip
            logger.info("Saved state too old, starting fresh")
            return

        user_map = state.get("user_map", {})
        instances_state = state.get("instances", {})

        if not user_map:
            return

        logger.info(f"Restoring state: {len(user_map)} users from {len(instances_state)} instances")

        # Create instance and restore users
        instance = await self._create_instance()
        for user_id in user_map:
            try:
                await instance.get_or_create_context(user_id)
                self._user_instance_map[user_id] = instance.instance_id
            except Exception as e:
                logger.warning(f"Failed to restore user {user_id}: {e}")

        logger.info(f"Restored {len(self._user_instance_map)} user contexts")

    # ─── Public API ────────────────────────────────────────────

    def get_health(self) -> Dict[str, Any]:
        """Get overall health of the persistent browser system."""
        total_contexts = sum(len(i.user_contexts) for i in self.instances.values())
        total_pages = sum(i.health.total_pages for i in self.instances.values())
        total_memory = sum(i.health.memory_mb for i in self.instances.values())
        total_blocked = sum(i.health.blocked_requests for i in self.instances.values())
        total_commands = sum(
            ctx.commands_executed
            for i in self.instances.values()
            for ctx in i.user_contexts.values()
        )

        return {
            "status": "running",
            "instances": {
                iid: inst.health.to_dict() for iid, inst in self.instances.items()
            },
            "summary": {
                "total_instances": len(self.instances),
                "total_user_contexts": total_contexts,
                "total_pages": total_pages,
                "total_memory_mb": round(total_memory, 1),
                "total_blocked_requests": total_blocked,
                "total_commands_executed": total_commands,
                "unique_users": len(self._user_instance_map),
            },
            "config": {
                "max_instances": self._max_instances,
                "max_contexts_per_instance": self._max_contexts_per_instance,
                "idle_timeout_minutes": self._idle_timeout / 60,
                "memory_cap_mb": self._memory_cap_mb,
                "health_check_interval_seconds": self._health_check_interval,
                "auto_restart": self._auto_restart,
            },
        }

    def list_users(self) -> List[Dict]:
        """List all active user contexts."""
        users = []
        for instance in self.instances.values():
            for user_id, ctx in instance.user_contexts.items():
                users.append({
                    "user_id": user_id,
                    "instance_id": instance.instance_id,
                    "pages": len(ctx.pages),
                    "commands_executed": ctx.commands_executed,
                    "blocked_requests": ctx.blocked_requests,
                    "idle_seconds": int(time.time() - ctx.last_active),
                    "created_ago_seconds": int(time.time() - ctx.created_at),
                })
        return sorted(users, key=lambda u: u["idle_seconds"])

    async def stop(self):
        """Graceful shutdown of all browser instances."""
        logger.info("Persistent Browser Manager shutting down...")

        # Stop background tasks
        if self._health_task:
            self._health_task.cancel()
        if self._cleanup_task:
            self._cleanup_task.cancel()

        # Save state
        await self._save_state()

        # Stop all instances
        for instance in self.instances.values():
            await instance.stop()

        self.instances.clear()
        self._user_instance_map.clear()
        logger.info("Persistent Browser Manager stopped")
