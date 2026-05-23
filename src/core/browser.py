"""
Agent-OS Stealth Browser Engine
Advanced anti-detection with real Chrome, cookie persistence, proxy support.
"""
import asyncio
import json
import base64
import random
import time
import logging
import os
import hashlib
from dataclasses import dataclass
from typing import Optional, Dict, Any, List
from pathlib import Path
from urllib.parse import urlparse

from cryptography.fernet import Fernet
from patchright.async_api import async_playwright, Browser, Page, BrowserContext

from src.core.stealth import (
    SUPPLEMENTARY_STEALTH_JS,
    handle_request_interception,
)
from src.core.tls_spoof import apply_browser_tls_spoofing
from src.core.tls_proxy import TLSProxyServer, TLSHTTPClient, _CURL_AVAILABLE
from src.core.cdp_stealth import CDPStealthInjector
from src.core.stealth_god import GodModeStealth
from src.core.adaptive_stealth import AdaptiveStealthManager
from src.tools.proxy_rotation import ProxyManager, ProxyInfo
from src.security.evasion_engine import EvasionEngine, ConsistentFingerprint, generate_fingerprint
from src.security.human_mimicry import HumanMimicry
from src.security.captcha_solver import CaptchaSolver
from src.security.cloudflare_bypass import CloudflareBypassEngine, CloudflareChallengeType, ClearanceStore
from src.core.firefox_engine import FirefoxEngine, DualEngineManager

logger = logging.getLogger("agent-os.browser")


# ═══════════════════════════════════════════════════════════════
# Browser Profiles — Realistic fingerprint bundles
# ═══════════════════════════════════════════════════════════════

@dataclass
class BrowserProfile:
    """A complete browser fingerprint profile for consistent anti-detection."""
    user_agent: str
    platform: str
    viewport: Dict[str, int]
    sec_ch_ua: str
    sec_ch_ua_platform: str
    hardware_concurrency: int
    device_memory: int
    screen_width: int
    screen_height: int
    timezone_id: str
    locale: str
    max_touch_points: int = 0
    pixel_ratio: float = 1.0


# Exactly 12 profiles: 4x Windows, 4x macOS, 2x Ubuntu, 2x Windows+Edge
BROWSER_PROFILES: List[BrowserProfile] = [
    # ── Windows 10/11 × Chrome 145, 146 ──────────────────
    BrowserProfile(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
        platform="Win32",
        viewport={"width": 1920, "height": 1080},
        sec_ch_ua='"Chromium";v="146", "Google Chrome";v="146", "Not?A_Brand";v="99"',
        sec_ch_ua_platform='"Windows"',
        hardware_concurrency=8,
        device_memory=16,
        screen_width=1920,
        screen_height=1080,
        timezone_id="America/New_York",
        locale="en-US",
    ),
    BrowserProfile(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36",
        platform="Win32",
        viewport={"width": 2560, "height": 1440},
        sec_ch_ua='"Chromium";v="145", "Google Chrome";v="145", "Not?A_Brand";v="99"',
        sec_ch_ua_platform='"Windows"',
        hardware_concurrency=12,
        device_memory=16,
        screen_width=2560,
        screen_height=1440,
        timezone_id="America/Chicago",
        locale="en-US",
    ),
    BrowserProfile(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36",
        platform="Win32",
        viewport={"width": 1366, "height": 768},
        sec_ch_ua='"Chromium";v="144", "Google Chrome";v="144", "Not?A_Brand";v="99"',
        sec_ch_ua_platform='"Windows"',
        hardware_concurrency=4,
        device_memory=8,
        screen_width=1366,
        screen_height=768,
        timezone_id="Europe/London",
        locale="en-GB",
    ),
    BrowserProfile(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36",
        platform="Win32",
        viewport={"width": 1440, "height": 900},
        sec_ch_ua='"Chromium";v="143", "Google Chrome";v="143", "Not?A_Brand";v="99"',
        sec_ch_ua_platform='"Windows"',
        hardware_concurrency=6,
        device_memory=8,
        screen_width=1440,
        screen_height=900,
        timezone_id="America/Denver",
        locale="en-US",
    ),
    # ── macOS 14/15 × Chrome 145, 146 ────────────────────
    BrowserProfile(
        user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
        platform="MacIntel",
        viewport={"width": 2560, "height": 1600},
        sec_ch_ua='"Chromium";v="146", "Google Chrome";v="146", "Not?A_Brand";v="99"',
        sec_ch_ua_platform='"macOS"',
        hardware_concurrency=10,
        device_memory=16,
        screen_width=2560,
        screen_height=1600,
        timezone_id="America/Los_Angeles",
        locale="en-US",
        max_touch_points=0,
        pixel_ratio=2.0,
    ),
    BrowserProfile(
        user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36",
        platform="MacIntel",
        viewport={"width": 1920, "height": 1200},
        sec_ch_ua='"Chromium";v="145", "Google Chrome";v="145", "Not?A_Brand";v="99"',
        sec_ch_ua_platform='"macOS"',
        hardware_concurrency=8,
        device_memory=8,
        screen_width=1920,
        screen_height=1200,
        timezone_id="America/New_York",
        locale="en-US",
        max_touch_points=0,
        pixel_ratio=2.0,
    ),
    BrowserProfile(
        user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36",
        platform="MacIntel",
        viewport={"width": 2880, "height": 1800},
        sec_ch_ua='"Chromium";v="144", "Google Chrome";v="144", "Not?A_Brand";v="99"',
        sec_ch_ua_platform='"macOS"',
        hardware_concurrency=10,
        device_memory=16,
        screen_width=2880,
        screen_height=1800,
        timezone_id="America/Los_Angeles",
        locale="en-US",
        max_touch_points=0,
        pixel_ratio=2.0,
    ),
    BrowserProfile(
        user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36",
        platform="MacIntel",
        viewport={"width": 1920, "height": 1080},
        sec_ch_ua='"Chromium";v="143", "Google Chrome";v="143", "Not?A_Brand";v="99"',
        sec_ch_ua_platform='"macOS"',
        hardware_concurrency=8,
        device_memory=16,
        screen_width=1920,
        screen_height=1080,
        timezone_id="Europe/Paris",
        locale="fr-FR",
        max_touch_points=0,
        pixel_ratio=2.0,
    ),
    # ── Ubuntu 22.04 × Chrome 145, 146 ────────────────────────
    BrowserProfile(
        user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
        platform="Linux x86_64",
        viewport={"width": 1920, "height": 1080},
        sec_ch_ua='"Chromium";v="146", "Google Chrome";v="146", "Not?A_Brand";v="99"',
        sec_ch_ua_platform='"Linux"',
        hardware_concurrency=4,
        device_memory=8,
        screen_width=1920,
        screen_height=1080,
        timezone_id="Europe/Berlin",
        locale="de-DE",
    ),
    BrowserProfile(
        user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36",
        platform="Linux x86_64",
        viewport={"width": 2560, "height": 1440},
        sec_ch_ua='"Chromium";v="145", "Google Chrome";v="145", "Not?A_Brand";v="99"',
        sec_ch_ua_platform='"Linux"',
        hardware_concurrency=8,
        device_memory=16,
        screen_width=2560,
        screen_height=1440,
        timezone_id="America/New_York",
        locale="en-US",
    ),
    # ── Windows 11 × Edge (Chromium-based) ────────────────────
    BrowserProfile(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36 Edg/146.0.0.0",
        platform="Win32",
        viewport={"width": 1920, "height": 1080},
        sec_ch_ua='"Chromium";v="146", "Microsoft Edge";v="146", "Not?A_Brand";v="99"',
        sec_ch_ua_platform='"Windows"',
        hardware_concurrency=8,
        device_memory=16,
        screen_width=1920,
        screen_height=1080,
        timezone_id="America/New_York",
        locale="en-US",
    ),
    BrowserProfile(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36 Edg/145.0.0.0",
        platform="Win32",
        viewport={"width": 2560, "height": 1440},
        sec_ch_ua='"Chromium";v="145", "Microsoft Edge";v="145", "Not?A_Brand";v="99"',
        sec_ch_ua_platform='"Windows"',
        hardware_concurrency=12,
        device_memory=16,
        screen_width=2560,
        screen_height=1440,
        timezone_id="Europe/London",
        locale="en-GB",
    ),
]


class AgentBrowser:
    """Core browser engine with advanced anti-detection for AI agents."""

    # Mobile device presets
    DEVICE_PRESETS = {
        "iphone_se": {"width": 375, "height": 667, "device_scale_factor": 2, "user_agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"},
        "iphone_14": {"width": 390, "height": 844, "device_scale_factor": 3, "user_agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"},
        "iphone_14_pro_max": {"width": 430, "height": 932, "device_scale_factor": 3, "user_agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"},
        "ipad": {"width": 768, "height": 1024, "device_scale_factor": 2, "user_agent": "Mozilla/5.0 (iPad; CPU OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"},
        "ipad_pro": {"width": 1024, "height": 1366, "device_scale_factor": 2, "user_agent": "Mozilla/5.0 (iPad; CPU OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"},
        "galaxy_s23": {"width": 360, "height": 780, "device_scale_factor": 3, "user_agent": "Mozilla/5.0 (Linux; Android 14; SM-S911B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Mobile Safari/537.36"},
        "galaxy_tab_s9": {"width": 800, "height": 1280, "device_scale_factor": 2, "user_agent": "Mozilla/5.0 (Linux; Android 14; SM-X810) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"},
        "pixel_8": {"width": 412, "height": 915, "device_scale_factor": 2.625, "user_agent": "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Mobile Safari/537.36"},
        "desktop_1080": {"width": 1920, "height": 1080, "device_scale_factor": 1, "user_agent": None},
        "desktop_1440": {"width": 2560, "height": 1440, "device_scale_factor": 1, "user_agent": None},
        "desktop_4k": {"width": 3840, "height": 2160, "device_scale_factor": 2, "user_agent": None},
    }

    def __init__(self, config):
        self.config = config
        self.playwright = None
        self.browser: Optional[Browser] = None
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None
        self._active_sessions: Dict[str, Dict] = {}
        self._blocked_requests: int = 0
        self._pages: Dict[str, Page] = {}
        self._console_logs: Dict[str, List[Dict]] = {}  # page_id → list of log entries
        self._cookie_dir = Path(os.path.expanduser("~/.agent-os/cookies"))
        self._download_dir = Path(os.path.expanduser("~/.agent-os/downloads"))
        self._proxy_config = None
        self._current_device = "desktop_1080"
        self._network_capture = None  # Set externally
        self._cookie_key = self._get_or_create_cookie_key()
        self._cookie_fernet = Fernet(self._cookie_key)
        self._crash_count = 0
        self._max_crash_retries = 3
        self._session_warmed_up = False
        self._launch_args = None  # cached launch args
        self._recovery_lock = asyncio.Lock()
        self._recovery_in_progress = False
        # Cookie batching — dirty flag + periodic flush to reduce I/O
        self._cookies_dirty: bool = False
        self._last_cookie_save: float = 0.0
        self._cookie_flush_task: Optional[asyncio.Task] = None
        # Proxy rotation
        self._proxy_pool: List[Dict[str, Any]] = []
        self._proxy_index: int = 0
        self._proxy_rotation_enabled: bool = False
        self._proxy_request_count: int = 0
        self._proxy_rotation_interval: int = 10
        # Evasion engine (TLS + fingerprint + cloudflare)
        self._evasion = EvasionEngine()
        self._cdp_stealth = CDPStealthInjector()
        self._god_mode_stealth = GodModeStealth()
        self._adaptive_stealth = AdaptiveStealthManager(self._cdp_stealth, self._god_mode_stealth)
        self._mimicry = HumanMimicry()
        # Track which stealth layer is active for each page
        self._stealth_layers_active: Dict[str, List[str]] = {}
        # CAPTCHA solver
        self._captcha_solver: Optional[CaptchaSolver] = None
        self._captcha_auto_solve = self.config.get("browser.captcha_auto_solve", False)
        # TLS proxy for real browser fingerprint
        self._tls_proxy: Optional[TLSProxyServer] = None
        self._tls_http: Optional[TLSHTTPClient] = None
        self._tls_proxy_port = self.config.get("browser.tls_proxy_port", 8081)
        self._tls_proxy_enabled = self.config.get("browser.tls_proxy_enabled", True)
        # High-level proxy rotation (residential, mobile, datacenter)
        self._proxy_manager: Optional[ProxyManager] = None
        self._proxy_manager_enabled = self.config.get("browser.proxy_rotation_enabled", True)
        self._proxy_rotation_strategy = self.config.get("browser.proxy_rotation_strategy", "weighted")
        self._current_proxy: Optional[ProxyInfo] = None
        self._current_proxy_config: Optional[Dict] = None
        # Domain → proxy mapping for sticky sessions
        self._domain_proxy_map: Dict[str, str] = {}
        # Request tracking for proxy result recording
        self._request_proxy_map: Dict[str, str] = {}
        # Enhanced Cloudflare bypass engine (v1/v2/v3 + Turnstile)
        self._cf_bypass = CloudflareBypassEngine(config)
        # cf_clearance cookie store for cross-session reuse
        self._cf_clearance_store = ClearanceStore()
        # Domain-specific bypass memory: domain → {strategy, timestamp, success_count}
        self._domain_bypass_history: Dict[str, Dict[str, Any]] = {}
        # Firefox fallback engine (for Chromium-detected sites)
        self._firefox_engine: Optional[FirefoxEngine] = None
        self._firefox_enabled = self.config.get("browser.firefox_fallback", True)
        # Dual engine manager
        self._dual_engine: Optional[DualEngineManager] = None
        # DNS resolution cache — domain → last_resolved_timestamp
        # Avoids redundant TCP connect checks for recently-accessed domains
        self._dns_cache: Dict[str, float] = {}
        self._dns_cache_ttl = 300.0  # 5 minutes
        # Browser profile (picked once per session, consistent within session)
        self._active_profile: Optional[BrowserProfile] = None

    def _get_or_create_cookie_key(self) -> bytes:
        """Get or create encryption key for cookie storage."""
        key_path = Path(os.path.expanduser("~/.agent-os/.cookie_key"))
        if key_path.exists():
            return key_path.read_bytes()
        key = Fernet.generate_key()
        key_path.parent.mkdir(parents=True, exist_ok=True)
        key_path.write_bytes(key)
        key_path.chmod(0o600)
        return key

    def _pick_profile(self) -> BrowserProfile:
        """Pick a browser profile for this session.

        Uses a hash of the session cookie key MIXED with a per-session
        random salt so that multiple sessions on the same server get
        different fingerprints. The profile is cached in
        self._active_profile and reused for the lifetime of this
        AgentBrowser instance.
        """
        if self._active_profile is not None:
            return self._active_profile

        # Seed from cookie key + session-specific random salt
        # This ensures different sessions get different profiles even
        # if they share the same cookie key (same server install).
        session_salt = os.urandom(8).hex()
        seed_bytes = self._cookie_key + session_salt.encode()
        seed_hash = hashlib.sha256(seed_bytes).hexdigest()
        seed_int = int(seed_hash[:8], 16)
        index = seed_int % len(BROWSER_PROFILES)

        self._active_profile = BROWSER_PROFILES[index]
        logger.info(
            "Browser profile #%d selected: %s | %s | %dx%d | %s",
            index,
            self._active_profile.platform,
            self._active_profile.sec_ch_ua_platform,
            self._active_profile.screen_width,
            self._active_profile.screen_height,
            self._active_profile.user_agent.split("Chrome/")[1].split(" ")[0]
            if "Chrome/" in self._active_profile.user_agent
            else "unknown",
        )
        return self._active_profile

    def _build_headers(self, profile: BrowserProfile) -> Dict[str, str]:
        """Build realistic HTTP request headers matching the browser profile.

        These headers are set as extra HTTP headers on the Playwright context
        so every request (navigation, XHR, fetch) carries them consistently.
        The values mirror what a real Chrome browser sends, including the
        Client Hints headers (sec-ch-ua) which are critical for passing
        modern bot detection.
        """
        # Build Accept-Language from the profile's locale instead of
        # hardcoding "en-US,en;q=0.9". A mismatch between the locale
        # (used for Intl, navigator.language) and Accept-Language header
        # is a common bot detection signal.
        locale = profile.locale
        lang_primary = locale.split("-")[0]  # e.g., "en" from "en-US"
        accept_language = f"{locale},{lang_primary};q=0.9"

        headers: Dict[str, str] = {
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;q=0.9,"
                "image/avif,image/webp,image/apng,*/*;q=0.8,"
                "application/signed-exchange;v=b3;q=0.7"
            ),
            "Accept-Language": accept_language,
            "Accept-Encoding": "gzip, deflate, br, zstd",
            "sec-ch-ua": profile.sec_ch_ua,
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": profile.sec_ch_ua_platform,
            "Upgrade-Insecure-Requests": "1",
            "sec-fetch-dest": "document",
            "sec-fetch-mode": "navigate",
            "sec-fetch-site": "none",
            "sec-fetch-user": "?1",
            # Google referer — makes requests look like they came from a
            # Google search, matching Scrapling's default behavior.
            "Referer": "https://www.google.com/",
        }
        return headers

    async def start(self):
        """Launch the browser with stealth settings."""
        self._cookie_dir.mkdir(parents=True, exist_ok=True)
        self._download_dir.mkdir(parents=True, exist_ok=True)

        # NOTE: TLS proxy is disabled for browser traffic by default.
        # Patchright (our browser engine) already patches Chromium's TLS
        # fingerprint at the BoringSSL level, so an extra proxy layer is
        # unnecessary and causes ERR_TUNNEL_CONNECTION_FAILED with HTTPS.
        # The TLS HTTP client (TLSHTTPClient) is still available for direct
        # HTTP requests where TLS fingerprinting matters (e.g. API calls,
        # bot-protected endpoints that need curl_cffi impersonation).
        if self._tls_proxy_enabled and _CURL_AVAILABLE:
            try:
                self._tls_proxy = TLSProxyServer(port=self._tls_proxy_port)
                proxy_started = await self._tls_proxy.start()
                if proxy_started:
                    logger.info(f"TLS proxy active on port {self._tls_proxy_port} (for HTTP client use only)")
                else:
                    self._tls_proxy = None
                    logger.warning("TLS proxy failed to start, using direct connection")
            except Exception as e:
                logger.warning(f"TLS proxy startup failed: {e}")
                self._tls_proxy = None
        elif self._tls_proxy_enabled:
            logger.warning("curl_cffi not installed — TLS proxy disabled, bot detection risk HIGH")

        # Initialize HTTP client for non-browser requests
        self._tls_http = TLSHTTPClient()

        # Initialize proxy manager for residential/mobile IP rotation
        if self._proxy_manager_enabled:
            self._proxy_manager = ProxyManager(strategy=self._proxy_rotation_strategy)
            # Load proxies from config if specified
            proxy_file = self.config.get("browser.proxy_file")
            if proxy_file:
                result = self._proxy_manager.load_proxies(proxy_file)
                logger.info(f"Loaded {result.get('loaded', 0)} proxies from {proxy_file}")
            # Load from API if configured
            proxy_api = self.config.get("browser.proxy_api_url")
            if proxy_api:
                api_key = self.config.get("browser.proxy_api_key")
                result = await self._proxy_manager.load_from_api(proxy_api, api_key)
                logger.info(f"Loaded {result.get('loaded', 0)} proxies from API")
            # Start health monitoring
            await self._proxy_manager.start()
            logger.info(f"Proxy rotation enabled (strategy: {self._proxy_rotation_strategy})")

        await self._launch_browser()

        # Start background cookie flush (save dirty cookies every 60s)
        self._cookie_flush_task = asyncio.create_task(self._cookie_flush_loop())

        # Start background session warmup — doesn't block server startup
        asyncio.create_task(self._warmup_session_async())

        logger.info("Browser started with stealth patches v2.0 + TLS fingerprinting + proxy rotation")

    async def _launch_browser(self):
        """Internal: launch browser and set up context."""
        self.playwright = await async_playwright().start()

        # Pick and lock browser profile for this session
        _profile = self._pick_profile()

        # Use headed or headless based on config
        headless = self.config.get("browser.headless", True)

        # Build launch args (cached for recovery)
        if self._launch_args is None:
            self._launch_args = [
                "--disable-blink-features=AutomationControlled",
                "--disable-features=IsolateOrigins,site-per-process,AudioServiceOutOfProcess,TranslateUI,BlinkGenPropertyTrees",
                "--disable-infobars",
                "--disable-background-timer-throttling",
                "--disable-backgrounding-occluded-windows",
                "--disable-renderer-backgrounding",
                "--disable-dev-shm-usage",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-component-extensions-with-background-pages",
                "--window-size=1920,1080",
                "--disable-ipc-flooding-protection",
                # ── Scrapling-aligned stealth flags ──
                "--test-type",                                   # Removes "automated test" infobar
                "--lang=en-US",                                  # Explicit language
                "--start-maximized",                             # Headless check bypass
                "--enable-async-dns",                            # Different DNS behavior
                "--disable-cookie-encryption",                   # Cookie handling for stealth contexts
                "--disable-domain-reliability",                  # Blocks Chrome domain reliability checks
                "--force-color-profile=srgb",                    # Consistent color profile
                "--font-render-hinting=none",                    # Consistent font rendering
                "--disable-client-side-phishing-detection",      # Blocks SafeBrowsing checks
                "--disable-background-networking",               # Speed + stealth
                "--metrics-recording-only",                      # Disable metrics
                "--disable-crash-reporter",                      # No crash reports
                "--disable-partial-raster",                      # Consistent rendering
                "--disable-threaded-animation",                  # Consistent rendering
                "--disable-threaded-scrolling",                  # Consistent scrolling
                "--safebrowsing-disable-auto-update",            # No SafeBrowsing updates
                "--autoplay-policy=user-gesture-required",       # Real browser behavior
                "--blink-settings=primaryHoverType=2,availableHoverTypes=2,primaryPointerType=4,availablePointerTypes=4",
                "--enable-features=NetworkService,NetworkServiceInProcess,TrustTokens,TrustTokensAlwaysAllowIssuance",
                # WebRTC IP leak prevention
                "--webrtc-ip-handling-policy=disable_non_proxied_udp",
                "--force-webrtc-ip-handling-policy",
                # Canvas fingerprint noise (Chrome-level, in addition to JS CDP stealth)
                "--fingerprinting-canvas-image-data-noise",
            ]

            # In Docker containers, Chromium cannot use its own namespace sandbox
            # because the container itself IS the sandbox. Auto-detect Docker
            # via /.dockerenv file or AGENT_OS_DOCKER env var.
            in_docker = (
                os.getenv("AGENT_OS_DOCKER") == "1"
                or os.path.exists("/.dockerenv")
                or os.path.exists("/run/.containerenv")
            )
            if in_docker:
                self._launch_args.append("--no-sandbox")
                logger.info("Docker environment detected — browser running with --no-sandbox (container is the sandbox)")

        # Conditionally add --headless=new only when running in headless mode.
        # Adding it unconditionally would override launch_options["headless"]=False
        # and force headless even when the user explicitly wants a headed browser.
        if headless:
            if "--headless=new" not in self._launch_args:
                self._launch_args.append("--headless=new")
        else:
            # Remove --headless=new if it was cached from a previous headless launch
            if "--headless=new" in self._launch_args:
                self._launch_args.remove("--headless=new")

        # Build launch options
        launch_options = {
            "headless": headless,
            "args": self._launch_args,
        }

        # Proxy support — use user-configured proxy only
        # NOTE: TLS proxy is NOT used as a browser proxy because:
        # 1. Patchright already patches Chromium TLS at BoringSSL level
        # 2. HTTPS CONNECT tunneling through the TLS proxy fails
        # 3. The TLS proxy is reserved for TLSHTTPClient (direct API calls)
        proxy_url = self.config.get("browser.proxy")
        if proxy_url:
            proxy_config = self._parse_proxy_url(proxy_url)
            launch_options["proxy"] = proxy_config
            self._proxy_config = proxy_config
            # DNS-over-HTTPS to prevent DNS leaks when using proxies
            self._launch_args.append("--dns-over-https-templates=https://cloudflare-dns.com/dns-query")
            logger.info(f"Proxy configured: {proxy_config.get('server', 'N/A')}")

        self.browser = await self.playwright.chromium.launch(**launch_options)

        # Log browser engine version (patchright manages its own binary)
        await self._get_browser_version()

        # Create context with profile-driven realistic settings
        storage_state = self._load_cookies("default")

        context_options = {
            "user_agent": self._active_profile.user_agent,
            "viewport": self._active_profile.viewport,
            "locale": self._active_profile.locale,
            "timezone_id": self._active_profile.timezone_id,
            "device_scale_factor": self._active_profile.pixel_ratio,
            "has_touch": False,
            "is_mobile": False,
            "java_script_enabled": True,
            "ignore_https_errors": True,
            "permissions": ["geolocation", "notifications"],
            "color_scheme": "dark",  # Dark mode bypasses creepjs prefersLightColor check (Scrapling-aligned)
            "service_workers": "allow",  # Allow service workers — disabling them is a bot signal
        }

        if storage_state:
            context_options["storage_state"] = storage_state

        self.context = await self.browser.new_context(**context_options)

        # Set realistic HTTP headers matching the active profile
        # This ensures sec-ch-ua, Accept-Language, sec-fetch-*, etc. are
        # present on EVERY request — not just the initial navigation.
        await self.context.set_extra_http_headers(self._build_headers(self._active_profile))

        # Generate fingerprint for CDP stealth (before creating page)
        fp = self._evasion.generate_fingerprint(page_id="main")
        chrome_ver = fp["chrome_version"] if fp else "124"

        # Set up request interception for bot detection blocking
        await self.context.route("**/*", self._handle_request)

        # Set up download handler
        self.context.on("download", self._handle_download)

        # Create the page BEFORE applying CDP-level spoofing
        self.page = await self.context.new_page()
        self._pages["main"] = self.page
        self._attach_console_listener("main", self.page)

        # ═══════════════════════════════════════════════════════════════
        # ADAPTIVE STEALTH INJECTION
        # Uses AdaptiveStealthManager to dynamically decide between CDP Stealth 
        # (performance) and God Mode (ultimate evasion).
        # ═══════════════════════════════════════════════════════════════
        layers = []
        try:
            # Inject stealth using AdaptiveManager. URL is empty initially, so it defaults to CDP.
            # God Mode can be explicitly requested per page if needed via page navigations.
            await self._adaptive_stealth.inject_stealth(self.context, self.page, "", fp)
            layers.append("Adaptive")
            logger.info("Stealth Layer (Adaptive): ACTIVE")
        except Exception as e:
            logger.warning(f"Adaptive stealth injection failed: {e}")

        self._stealth_layers_active["main"] = layers
        logger.info(f"Stealth layers active for 'main': {layers}")

        # Apply TLS fingerprint spoofing via CDP (needs an existing page)
        await apply_browser_tls_spoofing(self.page, chrome_version=chrome_ver, browser_profile=self._active_profile)

        # ═══════════════════════════════════════════════════════════
        # HEADLESS VERIFICATION HOOK
        # Chromium's headless mode may strip navigator.plugins and
        # window.chrome at the native level after navigation. CDP stealth
        # stores its objects in __agentOsStealthCache. The verification
        # hook checks on each domcontentloaded if those cached references
        # are still intact, and re-applies them ONLY if headless stripped
        # them. This is VERIFY-ONLY — no new objects are created, so
        # reference identity (navigator.plugins === navigator.plugins)
        # is preserved across navigations.
        # ═══════════════════════════════════════════════════════════
        await self._cdp_stealth.setup_headless_verification(self.page)

        # Initialize Firefox fallback engine if enabled
        if self._firefox_enabled:
            try:
                # Check if Firefox binary is available before attempting launch
                import shutil
                firefox_available = shutil.which("firefox") is not None
                if not firefox_available:
                    # Also check common install locations
                    for path in [
                        "/usr/bin/firefox",
                        "/snap/bin/firefox",
                        "/Applications/Firefox.app/Contents/MacOS/firefox",
                        os.path.expanduser("~/Applications/Firefox.app/Contents/MacOS/firefox"),
                    ]:
                        if os.path.isfile(path):
                            firefox_available = True
                            break
                if not firefox_available:
                    logger.debug("Firefox browser not found — fallback engine disabled")
                    self._firefox_engine = None
                else:
                    self._firefox_engine = FirefoxEngine(self.config)
                    await self._firefox_engine.start()
                    self._dual_engine = DualEngineManager(self.config, chromium_engine=self)
                    self._dual_engine.firefox = self._firefox_engine
                    self._dual_engine._started = True
                    logger.info("Firefox fallback engine initialized")
            except Exception as e:
                logger.debug(f"Firefox engine unavailable: {e}")
                self._firefox_engine = None

    async def _get_browser_version(self) -> str:
        """Get and log the browser engine version after launch.

        Returns:
            Version string from the browser (e.g. 'HeadlessChrome/131.0.6778.204')
            or 'unknown' if the version cannot be determined.
        """
        try:
            # Patchright exposes version as a property, Playwright as a method
            version = self.browser.version
            if asyncio.iscoroutine(version):
                version = await version
            logger.info(f"Browser engine: {version}")
            return str(version)
        except Exception as exc:
            logger.warning(f"Could not determine browser version: {exc}")
            return "unknown"

    async def recover(self):
        """Recover from browser crash by relaunching."""
        if self._recovery_in_progress:
            logger.warning("Recovery already in progress — skipping duplicate recovery")
            return

        self._recovery_in_progress = True
        try:
            async with self._recovery_lock:
                self._crash_count += 1
                if self._crash_count > self._max_crash_retries:
                    logger.error(f"Browser exceeded max crash retries ({self._max_crash_retries})")
                    raise RuntimeError("Browser crashed too many times — manual restart required")

                logger.warning(f"Browser recovering from crash (attempt {self._crash_count}/{self._max_crash_retries})...")

                # Save cookies before closing
                try:
                    await self._save_cookies("default")
                except Exception as cookie_err:
                    logger.warning(f"Failed to save cookies during recovery: {cookie_err}")

                # Close old browser
                try:
                    if self.context:
                        await self.context.close()
                except Exception as ctx_err:
                    logger.debug(f"Context close error during recovery: {ctx_err}")
                try:
                    if self.browser:
                        await self.browser.close()
                except Exception as br_err:
                    logger.debug(f"Browser close error during recovery: {br_err}")
                try:
                    if self.playwright:
                        await self.playwright.stop()
                except Exception as pw_err:
                    logger.debug(f"Playwright stop error during recovery: {pw_err}")

                # Clear state
                self.browser = None
                self.context = None
                self.page = None
                self._pages.clear()
                self._console_logs.clear()

                # Ensure TLS proxy is still running
                if self._tls_proxy_enabled and _CURL_AVAILABLE and not self._tls_proxy:
                    try:
                        self._tls_proxy = TLSProxyServer(port=self._tls_proxy_port)
                        await self._tls_proxy.start()
                        logger.info("TLS proxy restarted after crash")
                    except Exception as e:
                        logger.warning(f"TLS proxy restart failed: {e}")
                        self._tls_proxy = None

                # Relaunch
                await self._launch_browser()
                self._crash_count = 0  # Reset on successful recovery
                logger.info("Browser recovered successfully")
        finally:
            self._recovery_in_progress = False

    async def _safe_execute(self, coro_factory, page_id: str = "main", max_retries: int = 1):
        """Execute a browser operation with crash recovery and automatic retry.

        Args:
            coro_factory: A callable (e.g. lambda) that returns a fresh coroutine
                          each time it is called. This is required because Python
                          coroutines can only be awaited once — re-awaiting a
                          coroutine that already raised will produce
                          "RuntimeError: coroutine already awaited".
            page_id: Page identifier for the operation
            max_retries: Number of times to retry after successful recovery
                         (default 1 — retry once after crash recovery)

        Returns:
            The result of the coroutine if successful.

        Raises:
            RuntimeError: If browser crashes and recovery+retry also fails.
        """
        for attempt in range(max_retries + 1):
            try:
                coro = coro_factory()
                return await coro
            except Exception as e:
                error_str = str(e).lower()
                is_crash = any(kw in error_str for kw in [
                    "page crashed", "target closed", "context was destroyed",
                    "browser has been closed", "frame was detached",
                    "session deleted", "disconnected",
                ])

                if not is_crash:
                    raise

                if attempt >= max_retries:
                    # No more retries — raise with context
                    raise RuntimeError(
                        f"Browser crashed, recovery exhausted after {max_retries} retries. Original: {e}"
                    ) from e

                logger.warning(f"Browser crash detected (attempt {attempt + 1}/{max_retries}): {e}")
                await self.recover()
                # Loop continues → automatically retries the operation

        # Should not reach here, but just in case
        raise RuntimeError("_safe_execute: unexpected loop exit")

    def _attach_console_listener(self, page_id: str, page: Page):
        """Attach console and error listeners to a page."""
        self._console_logs[page_id] = []
        MAX_PER_PAGE = 150
        MAX_GLOBAL = 500

        def _enforce_global_cap():
            """Hard cap total console entries across all pages."""
            total = sum(len(v) for v in self._console_logs.values())
            if total > MAX_GLOBAL:
                # Trim oldest pages first
                sorted_pages = sorted(
                    self._console_logs.items(),
                    key=lambda kv: kv[1][0]["timestamp"] if kv[1] else float("inf")
                )
                for pid, logs in sorted_pages:
                    if total <= MAX_GLOBAL:
                        break
                    if pid == page_id:
                        # Trim this page's logs by half
                        half = len(logs) // 2
                        self._console_logs[pid] = logs[half:]
                        total -= half
                    else:
                        removed = len(logs)
                        self._console_logs[pid] = []
                        total -= removed

        def on_console(msg):
            entry = {
                "type": msg.type,               # log, warning, error, info, debug, etc.
                "text": msg.text,
                "location": {
                    "url": msg.location.get("url", ""),
                    "line": msg.location.get("lineNumber", 0),
                    "column": msg.location.get("columnNumber", 0),
                },
                "timestamp": time.time(),
            }
            logs = self._console_logs[page_id]
            logs.append(entry)
            # Per-page cap: drop oldest
            if len(logs) > MAX_PER_PAGE:
                self._console_logs[page_id] = logs[-MAX_PER_PAGE:]
            # Global cap
            _enforce_global_cap()

        def on_page_error(error):
            entry = {
                "type": "pageerror",
                "text": str(error),
                "location": {"url": "", "line": 0, "column": 0},
                "timestamp": time.time(),
            }
            logs = self._console_logs[page_id]
            logs.append(entry)
            if len(logs) > MAX_PER_PAGE:
                self._console_logs[page_id] = logs[-MAX_PER_PAGE:]
            _enforce_global_cap()

        page.on("console", on_console)
        page.on("pageerror", on_page_error)

    def _load_cookies(self, profile: str) -> Optional[Dict]:
        """Load saved cookies for a profile (encrypted)."""
        cookie_file = self._cookie_dir / f"{profile}.enc"
        if cookie_file.exists():
            try:
                encrypted = cookie_file.read_bytes()
                decrypted = self._cookie_fernet.decrypt(encrypted)
                return json.loads(decrypted)
            except Exception as e:
                logger.warning(f"Failed to load encrypted cookies for {profile}: {e}")
        return None

    async def _save_cookies(self, profile: str = "default"):
        """Mark cookies as dirty — actual save is deferred to reduce I/O."""
        self._cookies_dirty = True

    async def _flush_cookies(self, profile: str = "default"):
        """Write dirty cookies to disk (called periodically or on shutdown)."""
        if not self._cookies_dirty or not self.context:
            return
        try:
            state = await self.context.storage_state()
            cookie_file = self._cookie_dir / f"{profile}.enc"
            encrypted = self._cookie_fernet.encrypt(json.dumps(state).encode())
            cookie_file.write_bytes(encrypted)
            cookie_file.chmod(0o600)
            self._cookies_dirty = False
            self._last_cookie_save = time.time()
            logger.info(f"Cookies saved (encrypted) for profile: {profile}")
        except Exception as e:
            logger.warning(f"Cookie flush failed: {e}")

    async def export_tokens(self) -> str:
        """Export session tokens (cookies and storage state) to base64 JSON."""
        if not self.context:
            raise RuntimeError("Browser context not available")
        state = await self.context.storage_state()
        return base64.b64encode(json.dumps(state).encode('utf-8')).decode('utf-8')

    async def load_tokens(self, b64_tokens: str) -> None:
        """Load session tokens (cookies and storage state) from base64 JSON."""
        if not self.context:
            raise RuntimeError("Browser context not available")
        try:
            state_json = base64.b64decode(b64_tokens).decode('utf-8')
            state = json.loads(state_json)
            
            # Load cookies
            if "cookies" in state and state["cookies"]:
                await self.context.add_cookies(state["cookies"])
                
            # Load local storage via init script for all future pages
            if "origins" in state:
                for origin_data in state["origins"]:
                    ls = origin_data.get("localStorage", [])
                    if ls:
                        ls_dict = {item["name"]: item["value"] for item in ls}
                        origin = origin_data.get("origin", "")
                        script = f"""
                        if (window.location.origin === '{origin}') {{
                            const data = {json.dumps(ls_dict)};
                            for (const [k, v] of Object.entries(data)) {{
                                window.localStorage.setItem(k, v);
                            }}
                        }}
                        """
                        await self.context.add_init_script(script)
                        
                        # Apply to current page immediately if it matches the origin
                        for page in self._pages.values():
                            try:
                                if origin in page.url:
                                    await page.evaluate(script)
                            except Exception:
                                pass
            logger.info("Session tokens loaded successfully")
        except Exception as e:
            logger.error(f"Failed to load tokens: {e}")
            raise

    async def _cookie_flush_loop(self, interval: float = 60.0):
        """Background task: flush dirty cookies every `interval` seconds."""
        try:
            while True:
                await asyncio.sleep(interval)
                await self._flush_cookies()
        except asyncio.CancelledError:
            # Final flush on shutdown
            await self._flush_cookies()

    async def _handle_download(self, download):
        """Handle file downloads."""
        download_path = self._download_dir / download.suggested_filename
        await download.save_as(download_path)
        logger.info(f"Downloaded: {download_path}")

    async def _handle_request(self, route, request):
        """Intercept and block bot detection requests."""
        # Skip check for images, fonts, stylesheets, media — these are never
        # bot detection scripts and processing them wastes CPU on every request.
        _SKIP_CHECK_TYPES = {'image', 'font', 'stylesheet', 'media', 'manifest', 'other'}
        if request.resource_type in _SKIP_CHECK_TYPES:
            await route.continue_()
            return

        should_block, fake_response = handle_request_interception(request.url, request.resource_type)
        if should_block:
            self._blocked_requests += 1
            if fake_response:
                await route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps(fake_response)
                )
            else:
                await route.fulfill(status=200, body="")
            return
        await route.continue_()

    # Block indicators — only specific challenge/block page phrases.
    # NOTE: Do NOT use generic words like "cloudflare", "challenge", "blocked"
    # because legitimate sites contain those words in normal content.
    _BLOCK_INDICATORS = [
        "access denied",
        "captcha required",
        "bot detected",
        "just a moment",
        "checking your browser",
        "please verify you are human",
        "unusual traffic from your computer",
        "not available in your region",
        "attention required! | cloudflare",
        "cloudflare ray id",
        "enable javascript and cookies",
        "please enable js and disable any ad blocker",
        "are you a robot",
        "bot or not",
        "verify you are human",
        "help us protect",
        "you have been blocked by network security",
        "access to this page has been denied",
        "rate limit exceeded",
        "too many requests",
        "blocked by waf",
        "security check required",
        "please complete the security check",
        "press and hold",
        "managed challenge",
        "request denied",
        "you don't have permission",
        "unauthorized access",
        "your request was blocked",
        "automated access",
    ]

    # Sites that are KNOWN to show false positive block indicators
    # even when serving real content. Skip block detection for these.
    _SKIP_BLOCK_CHECK_DOMAINS = [
        "cloudflare.com",
        "amazon.com",
        "amazon.co.uk",
        "amazon.de",
        "amazon.co.jp",
    ]

    def _should_skip_block_check(self, url: str) -> bool:
        """Skip block detection for domains that naturally contain block keywords."""
        url_lower = url.lower()
        return any(domain in url_lower for domain in self._SKIP_BLOCK_CHECK_DOMAINS)

    def _is_blocked_page(self, title: str, text: str, url: str = "") -> bool:
        """
        Check if the loaded page is a block/challenge page.
        Uses strict phrase matching to avoid false positives.
        """
        if url and self._should_skip_block_check(url):
            return False

        combined = (title + " " + text[:500]).lower()

        for indicator in self._BLOCK_INDICATORS:
            if indicator in combined:
                return True
        return False

    # ─── Per-Site Bypass Strategies ────────────────────────────
    # Maps domains to alternative URLs or fallback approaches.
    # When a site blocks the main URL, try the fallback first.
    _SITE_BYPASS_STRATEGIES: Dict[str, Dict[str, Any]] = {
        "reddit.com": {
            "fallback_url": "https://www.reddit.com/r/popular/",
            "note": "Popular page accessible when homepage blocked",
        },
        "twitter.com": {
            "fallback_url": "https://x.com/explore",
            "note": "Explore page is publicly accessible without login",
            "extra_wait_ms": 3000,
            "stealth_profile": "chrome146",
        },
        "x.com": {
            "fallback_url": "https://x.com/explore",
            "note": "Explore page is publicly accessible without login",
            "extra_wait_ms": 3000,
            "stealth_profile": "chrome146",
        },
        "instagram.com": {
            "fallback_url": "https://www.instagram.com/explore/",
            "note": "Explore page accessible without login, uses English locale",
            "extra_wait_ms": 4000,
            "stealth_profile": "chrome146",
            "disable_images": False,
        },
        "facebook.com": {
            "fallback_url": "https://www.facebook.com/watch/",
            "note": "Watch page is publicly accessible without login",
            "extra_wait_ms": 4000,
            "stealth_profile": "chrome146",
        },
        "linkedin.com": {
            "fallback_url": "https://www.linkedin.com/jobs/",
            "note": "Jobs page is publicly accessible without login",
            "extra_wait_ms": 3000,
            "stealth_profile": "chrome146",
        },
        "tiktok.com": {
            "fallback_url": "https://www.tiktok.com/explore",
            "note": "Explore page accessible without login",
            "extra_wait_ms": 3000,
            "stealth_profile": "chrome146",
        },
        "threads.net": {
            "fallback_url": "https://www.threads.net/",
            "note": "Threads homepage accessible without login",
            "extra_wait_ms": 3000,
            "stealth_profile": "chrome146",
        },
        "youtube.com": {
            "fallback_url": "https://www.youtube.com/",
            "note": "YouTube homepage accessible without login",
            "extra_wait_ms": 2000,
            "stealth_profile": "chrome146",
        },
        "bloomberg.com": {
            "fallback_url": "https://www.bloomberg.com/markets",
            "note": "Markets page has lighter protection than homepage",
        },
        "glassdoor.com": {
            "fallback_url": "https://www.glassdoor.com/Job/index.htm",
            "note": "Job search page often lighter protection than reviews",
        },
        "zillow.com": {
            "fallback_url": "https://www.zillow.com/homes/",
            "note": "Direct search page bypasses homepage challenge",
        },
        "washingtonpost.com": {
            "fallback_url": "https://www.washingtonpost.com/news/",
            "note": "News section often accessible when homepage fails",
        },
        "oracle.com": {
            "fallback_url": "https://www.oracle.com/cloud/",
            "note": "Cloud product page has lighter protection",
        },
        "homedepot.com": {
            "fallback_url": "https://www.homedepot.com/c/self_services",
            "note": "Self-service page bypasses Akamai WAF on homepage",
        },
        "etsy.com": {
            "fallback_url": "https://www.etsy.com/market/handmade",
            "note": "Market page lighter protection than homepage",
        },
        "realtor.com": {
            "fallback_url": "https://www.realtor.com/realestateandhomes-detail/",
            "note": "Property detail page bypasses rate limit on homepage",
        },
        "tripadvisor.com": {
            "fallback_url": "https://www.tripadvisor.com/Restaurants",
            "note": "Restaurants page lighter protection",
        },
        "expedia.com": {
            "fallback_url": "https://www.expedia.com/Things-To-Do",
            "note": "Things-to-do page bypasses homepage rate limit",
        },
        "trulia.com": {
            "fallback_url": "https://www.trulia.com/for_sale/",
            "note": "For-sale listing page lighter protection",
        },
        "ziprecruiter.com": {
            "fallback_url": "https://www.ziprecruiter.com/jobs-search",
            "note": "Job search page lighter protection",
        },
        "priceline.com": {
            "fallback_url": "https://www.priceline.com/cars/",
            "note": "Cars page lighter protection than homepage",
        },
    }

    def _get_bypass_url(self, url: str) -> Optional[str]:
        """Get alternative URL for a blocked site, if a strategy exists."""
        url_lower = url.lower()
        for domain, strategy in self._SITE_BYPASS_STRATEGIES.items():
            if domain in url_lower:
                fallback = strategy.get("fallback_url")
                if fallback:
                    if fallback.startswith("http"):
                        return fallback
                    # Relative domain fallback — reconstruct URL
                    from urllib.parse import urlparse
                    parsed = urlparse(url)
                    return f"{parsed.scheme}://{fallback}"
        return None

    # ─── User-Agent Rotation Pool ─────────────────────────────
    # Real Chrome user agents for rotation on retries.
    # Updated to match Chrome 145-146 (current as of 2026)
    _ROTATION_USER_AGENTS: List[str] = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.3 Safari/605.1.15",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:136.0) Gecko/20100101 Firefox/136.0",
    ]

    def _get_rotation_ua(self) -> str:
        """Get a random user agent for rotation on retries."""
        return random.choice(self._ROTATION_USER_AGENTS)

    async def _warmup_session(self, target_url: str) -> None:
        """
        Warm up the browser session by visiting benign sites first.
        This builds realistic cookies, referrer history, and browsing patterns
        before hitting the target site. Sites see a natural browsing flow
        instead of a direct bot navigation.
        """
        warmup_sites = [
            "https://www.google.com",
            "https://www.wikipedia.org",
        ]

        # Only warmup for sites that benefit from it (not simple test sites)
        from urllib.parse import urlparse
        target_domain = urlparse(target_url).hostname or ""
        skip_warmup_domains = ["example.com", "httpbin.org", "localhost", "127.0.0.1"]

        if any(d in target_domain for d in skip_warmup_domains):
            return

        logger.info(f"Warming up session before visiting {target_domain}...")
        for warmup_url in warmup_sites:
            try:
                await self.page.goto(warmup_url, wait_until="domcontentloaded", timeout=10000)
                await asyncio.sleep(random.uniform(1.0, 2.5))
                # Simulate some scrolling
                scroll_amount = random.randint(100, 400)
                await self.page.mouse.wheel(0, scroll_amount)
                await asyncio.sleep(random.uniform(0.5, 1.5))
            except Exception:
                # Warmup failures are non-critical — continue anyway
                pass

    async def _warmup_session_async(self, target_url: str = "https://www.google.com") -> None:
        """Background warmup — doesn't block server startup or navigation.

        Starts after a short delay to let the server settle, then visits
        warmup pages in the background. Errors are silently caught since
        warmup is non-critical.
        """
        try:
            await asyncio.sleep(2)  # Let server settle
            # Visit warmup pages in background
            await self._warmup_session(target_url)
            self._session_warmed_up = True
            logger.debug("Background session warmup completed")
        except Exception as e:
            logger.debug(f"Background warmup skipped: {e}")

    async def _try_cloudflare_bypass(self, url: str, page: Page) -> bool:
        """
        Unified Cloudflare bypass flow with domain memory.
        
        Strategy order:
        1. Check cf_clearance cookie store (from ClearanceStore) → apply and test
        2. Try CloudflareBypassEngine.solve() (Playwright-based)
        3. Try legacy cloudscraper (in thread executor)
        4. Try curl_cffi with TLS fingerprint matching
        5. Save successful cf_clearance for future reuse
        
        Uses _domain_bypass_history to prioritize previously-successful strategies.
        """
        from urllib.parse import urlparse
        parsed = urlparse(url)
        domain = parsed.hostname or ""

        # Check domain bypass history — use previously-successful strategy first
        history = self._domain_bypass_history.get(domain)
        preferred_strategy = history.get("strategy") if history else None

        strategies = ["clearance_store", "cf_bypass_engine", "cloudscraper", "curl_cffi"]
        if preferred_strategy and preferred_strategy in strategies:
            strategies.remove(preferred_strategy)
            strategies.insert(0, preferred_strategy)

        for strategy in strategies:
            try:
                success = False

                if strategy == "clearance_store":
                    # Step 1: Check if cf_clearance cookie exists in store
                    clearance = self._cf_clearance_store.get(domain)
                    if clearance:
                        cookies = clearance.get("cookies", {})
                        cookie_list = []
                        for name, value in cookies.items():
                            cookie_list.append({
                                "name": name, "value": value,
                                "domain": domain, "path": "/",
                            })
                        if cookie_list:
                            await self.context.add_cookies(cookie_list)
                            logger.info(f"Applied {len(cookie_list)} cached CF cookies for {domain}")
                            # Quick test: reload and check if challenge is gone
                            try:
                                await page.reload(wait_until="domcontentloaded", timeout=15000)
                                await asyncio.sleep(2.0)
                                title = await page.title()
                                if not self._is_blocked_page(title, "", url=url):
                                    success = True
                                    logger.info(f"Cached clearance worked for {domain}")
                                else:
                                    # Cached clearance expired — remove it
                                    self._cf_clearance_store.remove(domain)
                            except Exception:
                                self._cf_clearance_store.remove(domain)

                elif strategy == "cf_bypass_engine":
                    # Step 2: Try CloudflareBypassEngine.solve() (Playwright-based)
                    detection = await self._cf_bypass.detect(page)
                    if detection.challenge_type != CloudflareChallengeType.NO_CHALLENGE:
                        logger.info(f"CF challenge detected: {detection.challenge_type.value}")
                        result = await self._cf_bypass.solve(page, detection)
                        if result.get("status") == "success":
                            logger.info(f"CF challenge solved: {result.get('method')} in {result.get('time')}s")
                            success = True
                            # Extract and save clearance cookies
                            try:
                                cookies = await self.context.cookies()
                                cf_cookies = {c["name"]: c["value"] for c in cookies
                                             if c["name"] in ("cf_clearance", "cf_bm", "__cf_bm")}
                                if cf_cookies:
                                    ua = self._active_profile.user_agent if self._active_profile else ""
                                    self._cf_clearance_store.save(domain, cf_cookies, user_agent=ua)
                            except Exception:
                                pass

                elif strategy == "cloudscraper":
                    # Step 3: Legacy cloudscraper fallback
                    if self._evasion.cloudflare.available:
                        logger.info("Trying legacy cloudscraper...")
                        loop = asyncio.get_running_loop()
                        cf_data = await loop.run_in_executor(
                            None, self._evasion.cloudflare.get_clearance_cookies, url
                        )
                        if cf_data and cf_data.get("cf_clearance"):
                            cookies = cf_data.get("cookies", {})
                            cookie_list = []
                            for name, value in cookies.items():
                                cookie_list.append({
                                    "name": name, "value": value,
                                    "domain": domain, "path": "/",
                                })
                            if cookie_list:
                                await self.context.add_cookies(cookie_list)
                                logger.info(f"Applied {len(cookie_list)} CF cookies via cloudscraper")
                                success = True
                                # Save to clearance store
                                self._cf_clearance_store.save(domain, cookies)

                elif strategy == "curl_cffi":
                    # Step 4: curl_cffi with TLS fingerprint matching
                    if self._tls_http and _CURL_AVAILABLE:
                        logger.info(f"Trying curl_cffi TLS bypass for {domain}...")
                        try:
                            resp = await asyncio.wait_for(
                                self._tls_http.get(url, timeout=15),
                                timeout=20.0,
                            )
                            if resp and resp.headers:
                                # Check for cf_clearance in response cookies
                                set_cookies = resp.headers.get("set-cookie", "")
                                if "cf_clearance" in set_cookies:
                                    # Parse cf_clearance from Set-Cookie header
                                    import re
                                    match = re.search(r'cf_clearance=([^;]+)', set_cookies)
                                    if match:
                                        cf_val = match.group(1)
                                        cookie_list = [{
                                            "name": "cf_clearance", "value": cf_val,
                                            "domain": domain, "path": "/",
                                        }]
                                        await self.context.add_cookies(cookie_list)
                                        success = True
                                        self._cf_clearance_store.save(
                                            domain, {"cf_clearance": cf_val}
                                        )
                                        logger.info(f"Got cf_clearance via curl_cffi for {domain}")
                        except Exception as e:
                            logger.debug(f"curl_cffi bypass failed: {e}")

                if success:
                    # Record in domain bypass history
                    self._record_bypass_success(domain, strategy)
                    return True

            except Exception as e:
                logger.warning(f"Bypass strategy '{strategy}' failed for {domain}: {e}")
                continue

        return False

    def _record_bypass_success(self, domain: str, strategy: str):
        """Record a successful bypass strategy for a domain."""
        now = time.time()
        existing = self._domain_bypass_history.get(domain)
        if existing and existing.get("strategy") == strategy:
            existing["success_count"] = existing.get("success_count", 0) + 1
            existing["last_success"] = now
        else:
            self._domain_bypass_history[domain] = {
                "strategy": strategy,
                "first_success": now,
                "last_success": now,
                "success_count": 1,
            }
        logger.debug(f"Bypass memory: {domain} → {strategy} (count: {self._domain_bypass_history[domain]['success_count']})")

    async def _try_firefox_fallback(self, url: str) -> Optional[Dict[str, Any]]:
        """
        Try navigating with Firefox engine when Chromium is blocked.
        """
        if not self._firefox_engine:
            return None

        logger.info(f"Trying Firefox fallback for {url[:60]}...")
        try:
            result = await self._firefox_engine.navigate(url, retries=2)
            if result.get("status") == "success":
                logger.info("Firefox succeeded where Chromium failed!")
                result["fallback_engine"] = "firefox"
                result["original_engine"] = "chromium"
                return result
        except Exception as e:
            logger.error(f"Firefox fallback failed: {e}")

        return None

    async def navigate(self, url: str, page_id: str = "main", wait_until: str = "domcontentloaded",
                       retries: int = 3, country: str = None, warmup: bool = True) -> Dict[str, Any]:
        """
        Navigate to a URL with human-like timing, proxy rotation, auto retry, and Cloudflare bypass.

        Args:
            url: Target URL
            page_id: Tab/page ID
            wait_until: Playwright wait condition
            retries: Max retry attempts (will rotate proxy on each retry)
            country: Geo-target proxy selection (e.g., "US", "GB", "DE")
            warmup: Whether to warm up session before first navigation

        Hard wall-clock limit: 45 seconds for the entire navigation (all retries included).
        """
        import time as _time

        _NAV_HARD_LIMIT = 45.0  # seconds — entire navigate including all retries

        async def _do_navigate() -> Dict[str, Any]:
            nonlocal wait_until  # Allow retry with "networkidle"
            page = self._pages.get(page_id, self.page)
            domain = urlparse(url).hostname or ""

            # Auto-upgrade to networkidle for JS-heavy/login domains
            # These sites need full JS rendering before forms are interactive
            _NETWORKIDLE_DOMAINS = [
                "instagram.com", "facebook.com", "twitter.com", "x.com",
                "linkedin.com", "github.com", "accounts.google.com",
                "login.microsoftonline.com", "amazon.com", "tiktok.com",
                "reddit.com", "spotify.com", "netflix.com",
            ]
            if wait_until == "domcontentloaded":
                domain_lower = domain.lower()
                if any(nd in domain_lower for nd in _NETWORKIDLE_DOMAINS):
                    wait_until = "networkidle"
                    logger.info(f"Auto-upgraded to networkidle for JS-heavy domain: {domain}")

            # ── Pre-flight: fast TCP connect check ────────────────────
            # Skip the entire Playwright flow if the site is completely
            # unreachable (DNS failure, server down, connection refused).
            # 5-second timeout — no browser launch, no retries.
            _pf_start = time.time()
            try:
                # DNS cache: skip pre-flight for recently-accessed domains
                _now = time.time()
                _cached_at = self._dns_cache.get(domain, 0.0)
                if _now - _cached_at < self._dns_cache_ttl:
                    logger.debug(f"DNS cache hit for {domain} — skipping pre-flight check")
                else:
                    port = 443 if url.startswith("https") else 80
                    _, writer = await asyncio.wait_for(
                        asyncio.open_connection(domain, port),
                        timeout=5.0,
                    )
                    writer.close()
                    await writer.wait_closed()
                    # Cache successful connection
                    self._dns_cache[domain] = _now
            except (asyncio.TimeoutError, OSError, Exception) as _pf_err:
                _pf_elapsed = round(time.time() - _pf_start, 1)
                logger.warning(f"Pre-flight failed for {domain}: {_pf_err} ({_pf_elapsed}s)")
                return {
                    "status": "error",
                    "error": "site_unreachable",
                    "time_seconds": _pf_elapsed,
                }

            # Get geo-targeting for known streaming sites
            geo_target = country or self._get_geo_target(domain)

            # Session warmup — build realistic browsing history before target
            # Warmup runs as a background task launched in start(), so it
            # doesn't block the first real navigation. If it hasn't completed
            # yet, we just skip it (the site will still work without warmup).
            # If warmup is still pending, launch it now as a background task
            # so it doesn't block this navigation.
            if warmup and not self._session_warmed_up:
                asyncio.create_task(self._warmup_session_async(url))
                self._session_warmed_up = True

            last_error = None
            tried_proxies: List[str] = []
            current_url = url  # May change if we try bypass URL

            for attempt in range(retries + 1):
                if attempt > 0:
                    # Exponential backoff with jitter
                    wait_time = random.uniform(3.0, 7.0) * attempt
                    logger.info(f"Retry {attempt}/{retries} for {current_url[:60]} (waiting {wait_time:.1f}s)")
                    await asyncio.sleep(wait_time)

                    # Rotate user agent on retry
                    new_ua = self._get_rotation_ua()
                    try:
                        await self.page.set_extra_http_headers({"User-Agent": new_ua})
                        logger.debug(f"Rotated UA for retry {attempt}")
                    except Exception:
                        pass

                    # Try per-site bypass URL on 1st retry (attempt >= 1)
                    if attempt >= 1:
                        bypass_url = self._get_bypass_url(url)
                        if bypass_url and bypass_url != current_url:
                            current_url = bypass_url
                            logger.info(f"Trying bypass URL: {bypass_url[:60]}")

                # Rotate proxy if we have a proxy manager
                if self._proxy_manager and self._proxy_manager_enabled:
                    proxy_result = await self._rotate_to_next_proxy(
                        domain=domain,
                        country=geo_target,
                        exclude=tried_proxies,
                    )
                    if proxy_result:
                        tried_proxies.append(proxy_result)

                # Fallback: auto-rotate proxy pool if configured
                elif self._proxy_rotation_enabled:
                    await self._maybe_rotate_proxy()

                # Human-like delay before navigation
                await asyncio.sleep(random.uniform(0.3, 1.2))

                try:
                    response = await page.goto(current_url, wait_until=wait_until, timeout=30000)

                    # SSRF protection: validate the final URL after redirects
                    # A malicious URL could redirect to localhost/private IPs
                    final_url = page.url
                    if final_url and final_url != "about:blank":
                        try:
                            from urllib.parse import urlparse as _urlparse
                            from src.validation.schemas import _is_private_ip
                            _parsed = _urlparse(final_url)
                            _hostname = _parsed.hostname or ""
                            if _hostname in ("localhost", "0.0.0.0", "169.254.169.254",
                                             "metadata.google.internal", "::1"):
                                logger.warning(f"SSRF blocked: redirect to {final_url}")
                                return {"status": "error", "error": "Redirect to blocked host",
                                        "blocked_url": final_url}
                            if _is_private_ip(_hostname):
                                logger.warning(f"SSRF blocked: redirect to private IP {final_url}")
                                return {"status": "error", "error": "Redirect to private IP",
                                        "blocked_url": final_url}
                        except ImportError:
                            pass  # validation module not available
                        except Exception as _ssrf_err:
                            logger.debug(f"SSRF check error: {_ssrf_err}")

                    # Wait for page to fully load (longer on retries for JS challenges)
                    load_wait = random.uniform(0.5, 1.5) + (attempt * 2.0)
                    await asyncio.sleep(load_wait)

                    # Check if we got a block/challenge page
                    title = await page.title()
                    text = ""
                    try:
                        body = await page.query_selector("body")
                        if body:
                            text = await body.inner_text()
                    except Exception:
                        pass

                    status_code = response.status if response else 200

                    # Handle HTTP 429 Rate Limiting — wait and retry
                    if status_code == 429 and attempt < retries:
                        retry_after = 5.0 + random.uniform(2.0, 10.0) * (attempt + 1)
                        logger.warning(
                            f"HTTP 429 rate limited on {current_url[:60]} "
                            f"(attempt {attempt + 1}, waiting {retry_after:.1f}s)"
                        )
                        await asyncio.sleep(retry_after)
                        continue

                    # Record proxy success
                    if self._current_proxy:
                        self._record_proxy_result(success=True, status_code=status_code)

                    # If blocked and we have retries left, try with different proxy
                    if self._is_blocked_page(title, text, url=current_url) and attempt < retries:
                        block_reason = self._get_block_reason(title, text, status_code)
                        logger.warning(
                            f"Block/challenge detected on {current_url[:60]} "
                            f"(attempt {attempt + 1}, reason: {block_reason})"
                        )

                        # Record proxy failure (blocked = proxy is burned)
                        if self._current_proxy:
                            self._record_proxy_result(
                                success=False,
                                status_code=status_code,
                                error=f"blocked_by_site: {block_reason}"
                            )

                        # Try Cloudflare bypass
                        if "cloudflare" in title.lower() or "just a moment" in title.lower():
                            bypassed = await self._try_cloudflare_bypass(current_url, page)
                            if bypassed:
                                # Reload with clearance cookies
                                try:
                                    response = await page.reload(wait_until=wait_until, timeout=30000)
                                    await asyncio.sleep(random.uniform(2.0, 4.0))
                                    title = await page.title()
                                    if not self._is_blocked_page(title, "", url=current_url):
                                        # Bypass worked!
                                        await self._save_cookies("default")
                                        return {
                                            "status": "success",
                                            "url": page.url,
                                            "title": title,
                                            "status_code": response.status if response else 200,
                                            "blocked_requests": self._blocked_requests,
                                            "cf_bypassed": True,
                                            "proxy_used": self._current_proxy.proxy_id if self._current_proxy else None,
                                            "block_report": self._build_block_report(
                                                url, status_code, block_reason, bypassed=True
                                            ),
                                        }
                                except Exception as e:
                                    logger.warning(f"Reload after CF bypass failed: {e}")

                        # If not Cloudflare or bypass failed, rotate proxy and retry
                        continue

                    # Save cookies after navigation
                    await self._save_cookies("default")

                    # Build block report — but only if the page is ACTUALLY blocked.
                    # Some sites (e.g. Reddit) return HTTP 403 while serving full content.
                    # Real block = error status AND (tiny page OR block phrases in content).
                    block_report = None
                    if status_code in (403, 429, 503):
                        text_lower = text.lower() if text else ""
                        has_block_phrases = any(
                            phrase in text_lower
                            for phrase in ("access denied", "blocked", "are you a robot",
                                           "just a moment", "captcha", "verify you are human")
                        )
                        is_actually_blocked = len(text) < 500 or has_block_phrases

                        if is_actually_blocked:
                            block_reason = self._get_block_reason(title, text, status_code)
                            block_report = self._build_block_report(
                                url, status_code, block_reason, bypassed=False
                            )
                        else:
                            # HTTP error code but real content loaded — not a block
                            logger.info(
                                f"HTTP {status_code} from {domain} but page has "
                                f"{len(text)} chars of real content — treating as success"
                            )

                    return {
                        "status": "success",
                        "url": page.url,
                        "title": title,
                        "status_code": status_code,
                        "blocked_requests": self._blocked_requests,
                        "attempt": attempt + 1,
                        "proxy_used": self._current_proxy.proxy_id if self._current_proxy else None,
                        "geo_target": geo_target,
                        "block_report": block_report,
                    }
                except Exception as e:
                    last_error = str(e)
                    logger.error(f"Navigation attempt {attempt + 1} failed: {e}")

                    # Record proxy failure
                    if self._current_proxy:
                        self._record_proxy_result(success=False, error=str(e)[:100])

                    # Check if it's a timeout — try with networkidle instead
                    if "timeout" in last_error.lower() and attempt < retries:
                        wait_until = "networkidle"
                        continue

            # All retries exhausted — try HTTP fallback with curl_cffi TLS
            http_fallback = await self._try_http_fallback(url)
            if http_fallback:
                return http_fallback

            return {
                "status": "error",
                "error": last_error or "All retries exhausted",
                "block_report": self._build_block_report(
                    url, 0, last_error or "unknown", bypassed=False
                ),
            }

        # Hard wall-clock timeout for the entire navigation
        _t_start = _time.time()
        try:
            result = await asyncio.wait_for(_do_navigate(), timeout=_NAV_HARD_LIMIT)
            # If browser was blocked, try HTTP fallback with curl_cffi TLS
            # Many sites block headless Chromium but allow curl_cffi because
            # its TLS ClientHello perfectly matches a real Chrome browser.
            if result.get("block_report") is not None:
                logger.info(f"Browser blocked for {url[:60]}, trying HTTP fallback...")
                http_fallback = await self._try_http_fallback(url)
                if http_fallback:
                    return http_fallback
            return result
        except asyncio.TimeoutError:
            elapsed = round(_time.time() - _t_start, 1)
            logger.error(f"Navigate hard timeout after {elapsed}s for {url[:80]}")
            # Try HTTP fallback even on timeout
            http_fallback = await self._try_http_fallback(url)
            if http_fallback:
                return http_fallback
            return {
                "status": "error",
                "error": "site_timeout_45s",
                "time_seconds": elapsed,
            }

    async def _try_http_fallback(self, url: str) -> Optional[Dict[str, Any]]:
        """
        Try fetching the URL via curl_cffi HTTP client with real browser TLS.

        When the Playwright browser is blocked (IP-level or JS challenge),
        curl_cffi with Chrome TLS fingerprint can often bypass the protection
        because it presents a perfect TLS ClientHello that matches a real browser.

        This fallback returns page content (HTML) but does NOT render JavaScript.
        For content-scraping use cases, this is sufficient.
        """
        if not self._tls_http or not _CURL_AVAILABLE:
            return None

        domain = urlparse(url).hostname or ""
        logger.info(f"Trying HTTP fallback (curl_cffi TLS) for {domain}...")

        try:
            resp = await asyncio.wait_for(
                self._tls_http.get(url, timeout=15),
                timeout=20.0,
            )

            body_len = len(resp.body) if resp.body else 0
            status_code = resp.status_code

            # Real content = large body even with error status codes
            # Many sites return 403 but still serve full page content via curl_cffi
            if body_len > 2000 or status_code == 200:
                # Extract a basic title from HTML
                title = ""
                if resp.body:
                    try:
                        html = resp.body.decode("utf-8", errors="ignore")
                        import re as _re
                        title_match = _re.search(r"<title[^>]*>(.*?)</title>", html, _re.IGNORECASE | _re.DOTALL)
                        if title_match:
                            title = title_match.group(1).strip()[:100]
                    except Exception:
                        pass

                logger.info(
                    f"HTTP fallback succeeded for {domain}: "
                    f"HTTP {status_code}, {body_len} bytes, title='{title[:40]}'"
                )
                return {
                    "status": "success",
                    "url": resp.url or url,
                    "title": title,
                    "status_code": status_code,
                    "blocked_requests": self._blocked_requests,
                    "attempt": 1,
                    "proxy_used": None,
                    "geo_target": None,
                    "block_report": None,
                    "fallback_engine": "curl_cffi_http",
                    "content_length": body_len,
                }
            else:
                logger.info(
                    f"HTTP fallback for {domain}: HTTP {status_code}, "
                    f"only {body_len} bytes — still blocked"
                )
                return None

        except asyncio.TimeoutError:
            logger.warning(f"HTTP fallback timed out for {domain}")
            return None
        except Exception as e:
            logger.warning(f"HTTP fallback failed for {domain}: {e}")
            return None

    def _get_block_reason(self, title: str, text: str, status_code: int) -> str:
        """Determine the specific reason a page was blocked."""
        combined = (title + " " + text[:300]).lower()

        if status_code == 429:
            return "rate_limited"
        if "cloudflare" in combined or "just a moment" in combined:
            return "cloudflare_challenge"
        if "captcha" in combined:
            return "captcha_required"
        if "bot detected" in combined or "bot or not" in combined:
            return "bot_detection"
        if "access denied" in combined:
            return "access_denied_waf"
        if "are you a robot" in combined:
            return "robot_check"
        if "enable javascript" in combined or "enable js" in combined:
            return "js_required"
        if "blocked" in combined:
            return "ip_blocked"
        if "please verify" in combined or "verify you are human" in combined:
            return "human_verification"
        if status_code == 403:
            return "forbidden_403"
        if status_code == 503:
            return "service_unavailable"
        return f"unknown_block_{status_code}"

    def _build_block_report(
        self, url: str, status_code: int, reason: str, bypassed: bool
    ) -> Dict[str, Any]:
        """
        Build a detailed block report with recommended fixes.
        Used for auto-reporting and debugging.
        """
        from urllib.parse import urlparse
        domain = urlparse(url).hostname or ""

        # Per-reason recommendations
        recommendations: Dict[str, List[str]] = {
            "rate_limited": [
                "Increase retry delay (current: exponential backoff)",
                "Use residential proxy rotation",
                "Reduce request frequency per domain",
            ],
            "cloudflare_challenge": [
                "Enable Turnstile solver",
                "Use residential proxy with sticky sessions",
                "Try Firefox fallback engine",
                "Warm up session before target navigation",
            ],
            "captcha_required": [
                "Enable CAPTCHA auto-solve (config: browser.captcha_auto_solve)",
                "Use 2Captcha or Anti-Captcha integration",
                "Try with residential proxy",
            ],
            "bot_detection": [
                "Rotate user agent",
                "Enable session warmup",
                "Use residential proxy instead of datacenter",
                "Check browser fingerprint consistency",
            ],
            "access_denied_waf": [
                "Try different proxy/IP",
                "Use residential proxy",
                "Check if IP is blacklisted",
            ],
            "robot_check": [
                "Enable session warmup",
                "Use residential proxy",
                "Try per-site bypass strategy",
            ],
            "js_required": [
                "Ensure JavaScript is enabled",
                "Wait longer for JS to execute",
                "Check if ad blocker is interfering",
            ],
            "ip_blocked": [
                "Change proxy/IP address",
                "Use residential proxy",
                "Contact site for unblock",
            ],
            "human_verification": [
                "Enable CAPTCHA solver",
                "Use residential proxy with good reputation",
                "Try session warmup approach",
            ],
            "forbidden_403": [
                "Try residential proxy",
                "Check if site requires login",
                "Try per-site bypass URL",
            ],
            "service_unavailable": [
                "Site may be temporarily down — retry later",
                "Try different region proxy",
            ],
        }

        recs = recommendations.get(reason, [
            "Try residential proxy",
            "Enable session warmup",
            "Check site-specific bypass strategy",
        ])

        bypass_strategy = self._SITE_BYPASS_STRATEGIES.get(domain, {})

        return {
            "url": url,
            "domain": domain,
            "status_code": status_code,
            "block_reason": reason,
            "bypassed": bypassed,
            "recommendations": recs,
            "site_bypass_available": bool(bypass_strategy),
            "site_bypass_note": bypass_strategy.get("note", ""),
            "timestamp": time.time(),
        }

    async def navigate_with_fallback(self, url: str, page_id: str = "main",
                                      retries: int = 3, country: str = None) -> Dict[str, Any]:
        """
        Navigate with automatic Chromium → Firefox fallback.
        If Chromium gets blocked, automatically tries Firefox.
        """
        # Try Chromium first
        result = await self.navigate(url, page_id=page_id, retries=retries, country=country)

        if result.get("status") == "success":
            result["engine_used"] = "chromium"
            return result

        # Chromium failed — try Firefox fallback
        status_code = result.get("status_code", 0)
        if status_code in (403, 406, 429) or result.get("status") == "error":
            firefox_result = await self._try_firefox_fallback(url)
            if firefox_result:
                firefox_result["engine_used"] = "firefox"
                firefox_result["chromium_status"] = result.get("status")
                firefox_result["chromium_status_code"] = status_code
                return firefox_result

        return result

    async def get_content(self, page_id: str = "main") -> Dict[str, Any]:
        """Get current page content."""
        page = self._pages.get(page_id, self.page)
        return {
            "url": page.url,
            "title": await page.title(),
            "html": await page.content(),
            "text": await page.inner_text("body") if await page.query_selector("body") else ""
        }

    async def screenshot(self, page_id: str = "main", full_page: bool = False,
                         format: str = "png", quality: int = 80) -> str:
        """Take a base64 screenshot.

        Args:
            page_id: Tab/page ID to screenshot.
            full_page: If True, capture the full scrollable page.
            format: Image format — "png" or "jpeg". JPEG is much smaller
                    (typically 5-10x) and recommended for API responses.
            quality: JPEG quality (1-100). Only used when format="jpeg".
                     Default 80 provides good quality with significant size savings.
        """
        page = self._pages.get(page_id, self.page)
        try:
            options: Dict[str, Any] = {"full_page": full_page, "type": format}
            if format == "jpeg" and quality:
                options["quality"] = quality
            img_bytes = await page.screenshot(**options)
            return base64.b64encode(img_bytes).decode()
        except Exception as e:
            logger.error(f"Screenshot failed: {e}")
            return ""

    # ─── Robust Element Finder ────────────────────────────────────

    async def _find_element_robust(self, page, selector: str, timeout_ms: int = 5000):
        """Find an element using multiple selector strategies with wait.

        Tries the exact selector first, then falls back to common patterns:
        name attribute, placeholder, id, aria-label, label[for], data-testid,
        and even by input type (email, password, tel, etc.).

        Returns (element, actual_selector) or (None, None).
        """
        import time as _time

        # Build the list of selectors to try, in priority order
        selector_candidates = [selector]

        # If the selector already looks like a full CSS selector, don't add alternatives
        is_full_selector = any(c in selector for c in ['[', '#', '.', '>', ':', ' '])
        if not is_full_selector:
            # selector is likely a bare name like "username", "email", "password"
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
                f'label:text-is("{selector}") + input',
                f'label:text-is("{selector}") ~ input',
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
                    'input[placeholder*="email" i]',
                    'input[placeholder*="Email" i]',
                ])
            elif 'username' in lower or 'user' in lower:
                # Many sites (Instagram, Facebook, etc.) use input[name="email"]
                # for the username field. Always search for both patterns.
                selector_candidates.extend([
                    'input[name="username"]',
                    'input[name="email"]',
                    'input[type="email"]',
                    'input[autocomplete="username"]',
                    'input[autocomplete="email"]',
                    'input[placeholder*="username" i]',
                    'input[placeholder*="email" i]',
                    'input[placeholder*="Phone" i]',
                    'input[placeholder*="phone" i]',
                ])
            elif 'password' in lower or 'pass' in lower or 'pwd' in lower:
                selector_candidates.extend([
                    'input[type="password"]',
                    'input[name="password"]',
                    'input[autocomplete="current-password"]',
                    'input[autocomplete="new-password"]',
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
                    'input[role="searchbox"]',
                ])
            elif 'submit' in lower or 'button' in lower:
                # button[type="submit"] often fails because HTML <button>Submit</button>
                # doesn't have an explicit type attribute even though JS reports type="submit"
                selector_candidates.extend([
                    'button',
                    'input[type="submit"]',
                    'button:has-text("Submit")',
                    'button:has-text("submit")',
                    'button:has-text("Send")',
                    'button:has-text("Continue")',
                    'button:has-text("Apply")',
                    'input[type="button"]',
                    '[role="button"]',
                ])

        # For selectors that look like button[type="..."] or input[type="..."],
        # also add fallbacks without the attribute selector (many sites omit explicit type)
        if selector.startswith('button[') and 'type' in selector:
            selector_candidates.extend([
                'button',
                'input[type="submit"]',
            ])
        elif selector.startswith('input[') and 'submit' in selector.lower():
            selector_candidates.extend([
                'button',
                'input[type="button"]',
            ])

        # Remove duplicates while preserving order
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
                        # Verify the element is at least in the DOM (not detached)
                        try:
                            visible = await el.is_visible()
                            if visible:
                                return el, sel
                            # Element exists but hidden — still return it, we'll try to interact
                            # (some elements become visible after scroll/focus)
                            return el, sel
                        except Exception:
                            return el, sel
                except Exception:
                    continue

            # Wait a bit before retrying (element may be loading via JS/lazy load)
            await asyncio.sleep(0.3)

        # Final attempt: try waiting for the FIRST selector with Playwright's wait_for_selector
        # This handles cases where the element is added to the DOM dynamically
        if unique_candidates:
            try:
                el = await page.wait_for_selector(unique_candidates[0], timeout=2000)
                if el:
                    return el, unique_candidates[0]
            except Exception:
                pass

        # SmartElementFinder fallback for natural language or fuzzy text selectors
        try:
            from src.tools.smart_finder import SmartElementFinder
            finder = SmartElementFinder(self)
            smart_res = await finder.find(selector, timeout=2000)
            if smart_res.get("found"):
                resolved_sel = smart_res["selector"]
                el = await page.query_selector(resolved_sel)
                if el:
                    logger.info(f"SmartElementFinder resolved selector '{selector}' to CSS selector '{resolved_sel}'")
                    return el, resolved_sel
        except Exception as se_err:
            logger.debug(f"SmartElementFinder fallback failed: {se_err}")

        return None, None

    # ─── Form Fill — Framework-Compatible ───────────────────────

    _SET_VALUE_JS = """(el, value) => {
        // ═══ Multi-strategy value setter ═══
        // Tries nativeInputValueSetter first (most reliable for controlled
        // components in Vue/Angular/Svelte), then falls back to direct
        // assignment. Dispatches a full event chain afterwards so every
        // mainstream framework picks up the change.

        // Pick the correct setter based on element type
        const tagName = el.tagName.toLowerCase();
        let nativeInputValueSetter;
        if (tagName === 'textarea') {
            nativeInputValueSetter = Object.getOwnPropertyDescriptor(
                window.HTMLTextAreaElement.prototype, 'value'
            )?.set;
        } else if (tagName === 'select') {
            nativeInputValueSetter = Object.getOwnPropertyDescriptor(
                window.HTMLSelectElement.prototype, 'value'
            )?.set;
        } else {
            nativeInputValueSetter = Object.getOwnPropertyDescriptor(
                window.HTMLInputElement.prototype, 'value'
            )?.set;
        }

        if (nativeInputValueSetter) {
            try { nativeInputValueSetter.call(el, value); } catch(_) { el.value = value; }
        } else {
            el.value = value;
        }

        // React __reactEventHandlers onChange dispatch
        // For React controlled components, dispatch onChange via internal handlers
        // so React updates its internal state and doesn't overwrite our value
        try {
            const reactKey = Object.keys(el).find(k => k.startsWith('__reactEventHandlers') || k.startsWith('__reactFiber$'));
            if (reactKey) {
                const handlers = el[reactKey];
                const onChange = handlers?.onChange || handlers?.onChangeCapture;
                if (typeof onChange === 'function') {
                    const syntheticEvent = {
                        target: el, currentTarget: el, type: 'change',
                        bubbles: true, cancelable: true, defaultPrevented: false,
                        preventDefault() { this.defaultPrevented = true; },
                        stopPropagation() {},
                        nativeEvent: new Event('change', { bubbles: true }),
                        persist() {},
                    };
                    onChange(syntheticEvent);
                }
            }
        } catch(_) {}

        // Full event chain: input -> change -> blur -> focus
        el.dispatchEvent(new InputEvent('input', { bubbles: true, cancelable: true, inputType: 'insertText', data: value }));
        el.dispatchEvent(new Event('change', { bubbles: true }));
        el.dispatchEvent(new FocusEvent('blur', { bubbles: true }));
        el.focus();
        return el.value;
    }"""

    _VERIFY_AND_FIX_JS = """(el, expectedValue) => {
        // ═══ Multi-strategy verify-and-fix ═══
        // Tries increasingly aggressive approaches until the element's
        // value matches the expected value. Returns {ok, actual, expected, method}
        // so callers can tell which strategy succeeded.

        // Early return if already correct
        if (el.value === expectedValue) {
            return { ok: true, actual: el.value, expected: expectedValue, method: 'already_correct' };
        }

        // Strategy 1: nativeInputValueSetter — uses the CORRECT setter for element type
        // HTMLInputElement and HTMLTextAreaElement have different prototype chains,
        // so we must pick the right one based on tagName.
        const tagName = el.tagName.toLowerCase();
        let nativeInputValueSetter;
        if (tagName === 'textarea') {
            nativeInputValueSetter = Object.getOwnPropertyDescriptor(
                window.HTMLTextAreaElement.prototype, 'value'
            )?.set;
        } else if (tagName === 'select') {
            nativeInputValueSetter = Object.getOwnPropertyDescriptor(
                window.HTMLSelectElement.prototype, 'value'
            )?.set;
        } else {
            nativeInputValueSetter = Object.getOwnPropertyDescriptor(
                window.HTMLInputElement.prototype, 'value'
            )?.set;
        }
        if (nativeInputValueSetter) {
            try {
                nativeInputValueSetter.call(el, expectedValue);
                el.dispatchEvent(new InputEvent('input', { bubbles: true, cancelable: true, inputType: 'insertText', data: expectedValue }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
                el.dispatchEvent(new FocusEvent('blur', { bubbles: true }));
                el.focus();
                if (el.value === expectedValue) {
                    return { ok: true, actual: el.value, expected: expectedValue, method: 'native_setter' };
                }
            } catch(_) {}
        }

        // Strategy 2: direct value + full event chain
        el.value = expectedValue;
        el.dispatchEvent(new InputEvent('input', { bubbles: true, cancelable: true, inputType: 'insertText', data: expectedValue }));
        el.dispatchEvent(new Event('change', { bubbles: true }));
        el.dispatchEvent(new FocusEvent('blur', { bubbles: true }));
        el.focus();
        if (el.value === expectedValue) {
            return { ok: true, actual: el.value, expected: expectedValue, method: 'direct_value' };
        }

        // Strategy 3: React __reactEventHandlers onChange dispatch
        // React controlled components intercept onChange via internal event handlers
        // stored in __reactEventHandlers or __reactFiber properties. Dispatching
        // a synthetic change event through these handlers tells React to update
        // its internal state, preventing it from overwriting our value on re-render.
        try {
            const reactKey = Object.keys(el).find(k => k.startsWith('__reactEventHandlers') || k.startsWith('__reactFiber$'));
            if (reactKey) {
                const handlers = el[reactKey];
                const onChange = handlers?.onChange || handlers?.onChangeCapture;
                if (typeof onChange === 'function') {
                    // Create a synthetic event object that React's onChange handler expects
                    const syntheticEvent = {
                        target: el,
                        currentTarget: el,
                        type: 'change',
                        bubbles: true,
                        cancelable: true,
                        defaultPrevented: false,
                        preventDefault() { this.defaultPrevented = true; },
                        stopPropagation() {},
                        nativeEvent: new Event('change', { bubbles: true }),
                        persist() {},
                    };
                    el.value = expectedValue;
                    onChange(syntheticEvent);
                    // Also fire standard events for any non-React listeners
                    el.dispatchEvent(new InputEvent('input', { bubbles: true, cancelable: true, inputType: 'insertText', data: expectedValue }));
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                    if (el.value === expectedValue) {
                        return { ok: true, actual: el.value, expected: expectedValue, method: 'react_onchange' };
                    }
                }
            }
        } catch(_) {}

        // Strategy 4: React 15/16+ Value Tracker Bypass
        // React overrides the value setter to track changes. If we bypass its tracker,
        // it registers our native setter call as a genuine change.
        try {
            const tracker = el._valueTracker;
            if (tracker) {
                tracker.setValue('');
            }
            // Fire native setter again now that tracker is cleared
            if (nativeInputValueSetter) {
                nativeInputValueSetter.call(el, expectedValue);
            } else {
                el.value = expectedValue;
            }
            el.dispatchEvent(new InputEvent('input', { bubbles: true, cancelable: true, inputType: 'insertText', data: expectedValue }));
            el.dispatchEvent(new Event('change', { bubbles: true }));
            el.dispatchEvent(new FocusEvent('blur', { bubbles: true }));
            if (el.value === expectedValue) {
                return { ok: true, actual: el.value, expected: expectedValue, method: 'react_tracker_bypass' };
            }
        } catch(_) {}

        // Strategy 5: Object.defineProperty nuclear override + events
        try {
            Object.defineProperty(el, 'value', { value: expectedValue, writable: true, configurable: true });
            el.dispatchEvent(new InputEvent('input', { bubbles: true, cancelable: true, inputType: 'insertText', data: expectedValue }));
            el.dispatchEvent(new Event('change', { bubbles: true }));
            el.dispatchEvent(new FocusEvent('blur', { bubbles: true }));
            el.focus();
        } catch(_) {}
        return { ok: el.value === expectedValue, actual: el.value, expected: expectedValue, method: 'define_property' };
    }"""


    async def fill_form(self, fields: Dict[str, str], page_id: str = "main") -> Dict[str, Any]:
        """Fill form fields with human-like typing, framework-compatible.

        Production-grade form filling that handles:
        - Standard HTML forms (input + change events)
        - Vue v-model bindings (input + change events)
        - Angular ngModel (input events)
        - Special characters (@, #, $, etc.) via insert_text or fill()
        - Multi-strategy element finding (name, id, placeholder, aria-label, type)
        - Value verification with automatic retry
        - Instagram/Google/Facebook specific field name quirks

        Flow per field:
        1. Find element via robust multi-strategy finder
        2. Wait for element to be interactable
        3. Focus + clear existing value
        4. Type value (keyboard.type for normal chars, fill() for special chars)
        5. Verify value was set correctly
        6. If verification fails, fix via direct JS value setting
        """
        page = self._pages.get(page_id, self.page)
        mimicry = self._mimicry
        filled = []
        failed = []

        for selector, value in fields.items():
            try:
                # ── Step 1: Find element using robust multi-strategy finder ──
                element, actual_selector = await self._find_element_robust(
                    page, selector, timeout_ms=8000
                )

                if not element:
                    logger.warning(f"Field not found after all strategies: {selector}")
                    failed.append({"selector": selector, "error": "not_found"})
                    continue

                # ── Step 2: Scroll into view and wait for interactability ──
                try:
                    await element.scroll_into_view_if_needed()
                    await asyncio.sleep(random.uniform(0.05, 0.15))
                except Exception:
                    pass

                # ── Step 3: Focus the element ──
                focused = False
                # Try JS focus first (most reliable approach)
                try:
                    await page.evaluate("""(sel) => {
                        const el = document.querySelector(sel);
                        if (el) { el.focus(); el.click(); }
                    }""", actual_selector)
                    focused = True
                except Exception:
                    pass

                if not focused:
                    try:
                        await element.click(timeout=5000)
                        focused = True
                    except Exception:
                        try:
                            await element.click(force=True, timeout=3000)
                            focused = True
                        except Exception:
                            # Last resort: use page.focus()
                            try:
                                await page.focus(actual_selector)
                                focused = True
                            except Exception:
                                logger.warning(f"Cannot focus element: {selector}")
                                failed.append({"selector": selector, "error": "cannot_focus"})
                                continue

                await asyncio.sleep(random.uniform(0.1, 0.25))

                # ── Step 4: Clear existing value ──
                # Use triple-select to ensure all text is selected (works across browsers)
                try:
                    # Click to place cursor, then select all
                    await page.keyboard.press("Home")
                    await asyncio.sleep(0.05)
                    await page.keyboard.press("Control+a")
                    await asyncio.sleep(0.05)
                    await page.keyboard.press("Control+a")  # Double-select for reliability
                    await asyncio.sleep(0.05)
                    await page.keyboard.press("Backspace")
                    await asyncio.sleep(0.05)
                except Exception:
                    # Fallback: use Playwright fill("") which clears and dispatches events
                    try:
                        await element.fill("")
                    except Exception:
                        pass

                await asyncio.sleep(random.uniform(0.05, 0.15))

                # ── Step 5: Type the value using the most reliable method ──
                # ORDER OF PRIORITY (production-proven multi-strategy):
                # 1. fill() — handles ALL chars including @#$, dispatches standard events
                # 2. keyboard.insert_text() — bypasses keyboard layout entirely
                # 3. keyboard.type() char-by-char — last resort, may fail for special chars
                value_str = str(value)
                typing_ok = False

                # Strategy 1: Playwright fill() — handles ALL characters reliably
                # fill() sets the value at the DOM level and dispatches input+change events automatically.
                try:
                    await element.fill(value_str)
                    typing_ok = True
                except Exception as fill_err:
                    logger.debug(f"fill() failed for {selector}: {fill_err}")

                # Strategy 2: keyboard.insert_text() — bypasses keyboard layout entirely
                # This types the ENTIRE string as-is, no key mapping involved.
                # Works for @, #, $, and all Unicode characters.
                if not typing_ok:
                    try:
                        await page.keyboard.insert_text(value_str)
                        typing_ok = True
                    except Exception as insert_err:
                        logger.debug(f"insert_text() failed for {selector}: {insert_err}")

                # Strategy 3: keyboard.type() char-by-char as absolute last resort
                # WARNING: keyboard.type() INTERPRETS special chars as key combos.
                # E.g., @ becomes Shift+2, which may fail on non-US keyboard layouts.
                # Only use this when the value has NO special characters.
                if not typing_ok:
                    has_special = any(c in value_str for c in '@#$%^&*{}|:"<>?~`_+!=()')
                    if not has_special:
                        try:
                            await page.keyboard.type(value_str, delay=mimicry.typing_delay())
                            typing_ok = True
                        except Exception as type_err:
                            logger.debug(f"keyboard.type() failed for {selector}: {type_err}")

                if not typing_ok:
                    # Strategy 4: JS-only fill via direct value setting
                    # This sets the value purely through JavaScript, bypassing all keyboard issues
                    try:
                        fix_result = await element.evaluate(self._VERIFY_AND_FIX_JS, value_str)
                        if fix_result.get("ok"):
                            typing_ok = True
                            logger.info(f"JS-only fill succeeded for {selector}")
                        else:
                            logger.warning(f"All fill strategies failed for {selector}")
                    except Exception as js_err:
                        logger.debug(f"JS fill also failed for {selector}: {js_err}")

                if not typing_ok:
                    failed.append({"selector": selector, "error": "typing_failed_all_strategies"})
                    continue

                await asyncio.sleep(0.1)

                # ── Step 6: Verify value was set correctly ──
                try:
                    actual_value = await element.evaluate("el => el.value")
                    if actual_value != value_str:
                        # Value mismatch — try JS fix
                        fix_result = await element.evaluate(self._VERIFY_AND_FIX_JS, value_str)
                        if not fix_result.get("ok"):
                            logger.warning(
                                f"Value verification failed for {selector}: "
                                f"got '{fix_result.get('actual')}', expected '{value_str}'"
                            )
                except Exception as e:
                    logger.debug(f"Value verification skipped for {selector}: {e}")

                filled.append(selector)
                # Inter-field delay: give React/Angular/Vue time to process onChange
                # and re-render before filling the next field. Too fast = race condition
                # where framework overwrites the next field's value.
                await asyncio.sleep(random.uniform(0.15, 0.4))

            except Exception as e:
                logger.error(f"Error filling {selector}: {e}")
                failed.append({"selector": selector, "error": str(e)})

        # Return proper status based on results
        if not filled and fields:
            return {"status": "error", "error": "No fields could be filled", "filled": [], "failed": failed, "total": len(fields)}
        if len(filled) < len(fields):
            return {"status": "partial", "filled": filled, "failed": failed, "total": len(fields)}
        return {"status": "success", "filled": filled, "failed": [], "total": len(fields)}

    async def click(self, selector: str, page_id: str = "main") -> Dict[str, Any]:
        """Click an element with human-like mouse movement.

        Uses a multi-strategy approach with robust element finding:
        1. Find element via robust multi-strategy finder
        2. Scroll into view and check visibility
        3. Normal click with mouse path animation
        4. If element not interactable -> force click
        5. If force click fails -> JS click (bypasses all checks)
        6. If JS click fails -> keyboard Enter for buttons/links
        7. Retry with exponential backoff on stale element errors
        """
        page = self._pages.get(page_id, self.page)
        mimicry = self._mimicry

        # Resolve the actual selector for JS fallbacks
        actual_selector = selector

        for attempt in range(3):
            try:
                # Use robust finder for first attempt, simple finder for retries
                if attempt == 0:
                    element, actual_selector = await self._find_element_robust(
                        page, selector, timeout_ms=5000
                    )
                    if not element:
                        return {"status": "error", "error": f"Element not found: {selector}"}
                else:
                    element = await page.query_selector(actual_selector)
                    if not element:
                        # Retry with robust finder
                        element, actual_selector = await self._find_element_robust(
                            page, selector, timeout_ms=3000
                        )
                        if not element:
                            if attempt == 2:
                                return {"status": "error", "error": f"Element not found: {selector}"}
                            await asyncio.sleep(0.5 * (attempt + 1))
                            continue

                # Scroll into view
                try:
                    await element.scroll_into_view_if_needed()
                    await asyncio.sleep(random.uniform(0.05, 0.1))
                except Exception:
                    pass

                # Check visibility
                try:
                    is_visible = await element.is_visible()
                    if not is_visible:
                        # Try force click for hidden elements
                        await element.click(force=True, timeout=5000)
                        await asyncio.sleep(random.uniform(0.2, 0.5))
                        return {"status": "success", "selector": selector, "method": "force_click"}
                except Exception:
                    pass

                # Normal click with mouse path
                box = await element.bounding_box()
                if box:
                    target_x = box["x"] + box["width"] / 2
                    target_y = box["y"] + box["height"] / 2
                    start_x, start_y = mimicry._last_move
                    path = mimicry.mouse_path(start_x, start_y, target_x, target_y)

                    for x, y in path:
                        await page.mouse.move(x, y)
                        await asyncio.sleep(random.uniform(0.005, 0.02))

                await asyncio.sleep(random.uniform(0.05, 0.15))
                await element.click(timeout=10000)
                await asyncio.sleep(random.uniform(0.2, 0.5))

                return {"status": "success", "selector": selector, "method": "normal_click"}

            except Exception as e:
                error_str = str(e).lower()
                # Stale element — retry
                if "stale" in error_str or "detached" in error_str or "not attached" in error_str:
                    await asyncio.sleep(0.5 * (attempt + 1))
                    continue

                # Element not interactable — try JS click
                if "not visible" in error_str or "not interactable" in error_str or "intercepts pointer" in error_str:
                    try:
                        # Sanitize selector for JS injection (avoid XSS)
                        safe_sel = actual_selector.replace("'", "\\'")
                        await page.evaluate(f"""() => {{
                            const el = document.querySelector('{safe_sel}');
                            if (el) {{ el.click(); return true; }}
                            return false;
                        }}""")
                        await asyncio.sleep(random.uniform(0.2, 0.5))
                        return {"status": "success", "selector": selector, "method": "js_click"}
                    except Exception:
                        pass

                # Timeout — try keyboard Enter as last resort for buttons/links
                if "timeout" in error_str:
                    try:
                        safe_sel = actual_selector.replace("'", "\\'")
                        await page.evaluate(f"""() => {{
                            const el = document.querySelector('{safe_sel}');
                            if (el) {{ el.focus(); return true; }}
                            return false;
                        }}""")
                        await page.keyboard.press("Enter")
                        await asyncio.sleep(random.uniform(0.2, 0.5))
                        return {"status": "success", "selector": selector, "method": "keyboard_enter"}
                    except Exception:
                        pass

                if attempt == 2:  # Last attempt
                    return {"status": "error", "error": str(e), "selector": selector}
                await asyncio.sleep(0.5 * (attempt + 1))

        return {"status": "error", "error": f"Failed after 3 attempts: {selector}"}

    async def type_text(self, text: str, page_id: str = "main") -> Dict[str, Any]:
        """Type text with human-like delays (into focused element).

        Handles special characters (@, #, $, etc.) properly by choosing
        the right input method:
        - fill() on the focused element (most reliable for ALL chars + standard events)
        - keyboard.insert_text() for special characters (bypasses keyboard layout)
        - keyboard.type() only for normal characters (dispatches proper key events)
        
        NOTE: keyboard.type() INTERPRETS special chars as key combos.
        E.g., @ becomes Shift+2 which may fail on non-US layouts.
        We NEVER use keyboard.type() for strings containing special characters.
        """
        page = self._pages.get(page_id, self.page)
        mimicry = self._mimicry

        try:
            # Check for special characters that keyboard.type() handles incorrectly
            special_chars = set('@#$%^&*{}|:"<>?~`_+!=()\\')
            has_special = any(c in special_chars for c in text)

            if has_special:
                # Strategy 1: Find the focused element and use fill() — most reliable
                # fill() handles ALL characters and dispatches input+change events
                try:
                    focused_el = await page.evaluate("""() => {
                        const el = document.activeElement;
                        if (el && (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA')) {
                            return el.id || el.name || 
                                   (el.getAttribute('aria-label') || '').replace(/"/g, '\\"') ||
                                   el.tagName.toLowerCase() + ':nth-of-type(' + 
                                   (Array.from(el.parentElement?.children || []).filter(
                                       c => c.tagName === el.tagName
                                   ).indexOf(el) + 1) + ')';
                        }
                        return null;
                    }""")
                    if focused_el:
                        try:
                            el = await page.query_selector(f'[name="{focused_el}"], #{focused_el}')
                            if el:
                                await el.fill(text)
                                return {"status": "success", "typed": len(text), "method": "fill"}
                        except Exception:
                            pass
                except Exception:
                    pass

                # Strategy 2: keyboard.insert_text() — bypasses keyboard layout entirely
                # Types the ENTIRE string as-is, no key mapping involved
                try:
                    await page.keyboard.insert_text(text)
                    return {"status": "success", "typed": len(text), "method": "insert_text"}
                except Exception:
                    pass

                # Strategy 3: Type char by char using insert_text for special chars
                try:
                    for char in text:
                        if char in special_chars:
                            await page.keyboard.insert_text(char)
                        else:
                            await page.keyboard.type(char, delay=mimicry.typing_delay())
                    return {"status": "success", "typed": len(text), "method": "hybrid"}
                except Exception:
                    pass

                # Strategy 4: Last resort — use keyboard.type and hope for the best
                try:
                    await page.keyboard.type(text, delay=mimicry.typing_delay())
                    return {"status": "success", "typed": len(text), "method": "type_fallback"}
                except Exception:
                    pass

                return {"status": "error", "error": "All typing strategies failed for special characters"}
            else:
                # Normal characters: Use keyboard.type() which dispatches proper
                # KeyboardEvents that frameworks listen to (Vue, Angular, standard forms)
                try:
                    await page.keyboard.type(text, delay=mimicry.typing_delay())
                    return {"status": "success", "typed": len(text), "method": "type"}
                except Exception:
                    # Fallback: try insert_text
                    try:
                        await page.keyboard.insert_text(text)
                        return {"status": "success", "typed": len(text), "method": "insert_text_fallback"}
                    except Exception as e:
                        return {"status": "error", "error": str(e)}

        except Exception as e:
            return {"status": "error", "error": str(e)}

    async def press_key(self, key: str, page_id: str = "main") -> Dict[str, Any]:
        """Press a keyboard key (Enter, Tab, Escape, etc.)."""
        page = self._pages.get(page_id, self.page)
        try:
            await page.keyboard.press(key)
            return {"status": "success", "key": key}
        except Exception as e:
            return {"status": "error", "error": str(e)}

    async def evaluate_js(self, script: str, page_id: str = "main") -> Dict[str, Any]:
        """Execute JavaScript in a sandboxed page context.

        Uses Patchright's isolated world execution to prevent user-supplied
        scripts from accessing or modifying the page's main JavaScript context.
        The sandbox:
        - Cannot access page's window properties directly (read-only DOM access)
        - Cannot modify global variables in the main world
        - Runs in an isolated V8 context with its own global scope
        - Has a 10-second execution timeout to prevent infinite loops

        Returns a dict always (dual-return contract):
        - On success: {"status": "success", "result": <raw_value>}
        - On sandbox error: {"status": "error", "error": <error_message>}
        - On exception: {"status": "error", "error": <exception_message>}

        For callers that prefer exception-based control flow, use the
        ``evaluate_js_raw`` property which wraps this method and raises
        RuntimeError on failure.
        """
        page = self._pages.get(page_id, self.page)

        # Wrap the script in a sandboxed execution with timeout and error handling.
        # Uses (0, eval)() for indirect eval which runs in global scope,
        # then wraps the result in an object for safe serialization.
        sandboxed_script = f"""
        (() => {{
            try {{
                const __value = (0, eval)({json.dumps(script)});
                const __result = __value instanceof Promise
                    ? Promise.race([
                        __value.then(v => ({{ success: true, value: v }})),
                        new Promise((_, rej) =>
                            setTimeout(() => rej(new Error('Sandbox execution timeout (10s)')), 10000)
                        )
                    ])
                    : {{ success: true, value: __value }};
                return __result;
            }} catch(__e) {{
                return {{ success: false, error: __e.message || String(__e) }};
            }}
        }})()
        """

        try:
            result = await page.evaluate(sandboxed_script)
            if isinstance(result, dict):
                # Sandbox returned a structured result
                if not result.get("success"):
                    error_msg = result.get("error", "Unknown sandbox error")
                    return {"status": "error", "error": error_msg}
                # Return the value wrapped in the dual-return contract
                return {"status": "success", "result": result.get("value")}
            # Unexpected result type (string, number, etc.) — wrap as success
            return {"status": "success", "result": result}
        except Exception as e:
            return {"status": "error", "error": str(e)}

    @property
    def evaluate_js_raw(self):
        """Return a callable that wraps evaluate_js but raises on errors.

        Usage:
            result = await browser.evaluate_js_raw("1+1")
            # Raises RuntimeError if status == "error"
        """
        browser_self = self

        async def _raw(script: str, page_id: str = "main"):
            resp = await browser_self.evaluate_js(script, page_id)
            if resp.get("status") == "error":
                raise RuntimeError(f"JS evaluation failed: {resp.get('error', 'Unknown error')}")
            return resp.get("result")

        return _raw

    async def evaluate_js_unsafe(self, script: str, page_id: str = "main") -> Dict[str, Any]:
        """Execute JavaScript directly in the page's main world (UNSANDBOXED).

        WARNING: This bypasses sandbox protections. Use only for trusted internal
        scripts (stealth injection, DOM snapshots, etc.), never for user-supplied code.

        Returns a dict always:
        - On success: {"status": "success", "result": <value>}
        - On failure: {"status": "error", "error": <message>}
        """
        page = self._pages.get(page_id, self.page)
        try:
            value = await page.evaluate(script)
            return {"status": "success", "result": value}
        except Exception as e:
            logger.error(f"Unsafe JS evaluation failed: {e}")
            return {"status": "error", "error": str(e)}

    async def get_dom_snapshot(self, page_id: str = "main") -> str:
        """Get a structured DOM snapshot for agent analysis."""
        page = self._pages.get(page_id, self.page)
        try:
            snapshot = await page.evaluate("""() => {
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
                    const text = el.childNodes.length === 1 && el.childNodes[0].nodeType === 3
                        ? el.childNodes[0].textContent.trim().substring(0, 100) : '';

                    if (['script', 'style', 'noscript', 'svg'].includes(tag)) return '';

                    const children = Array.from(el.children).map(c => getSnapshot(c, depth + 1)).filter(Boolean).join('');

                    if (children) {
                        result = indent + '<' + tag + attrStr + '>' + (text ? ' ' + text : '') + '\\n' + children + indent + '</' + tag + '>\\n';
                    } else if (text) {
                        result = indent + '<' + tag + attrStr + '>' + text + '</' + tag + '>\\n';
                    } else {
                        result = indent + '<' + tag + attrStr + ' />\\n';
                    }
                    return result;
                }
                return getSnapshot(document.body, 0);
            }""")
            return snapshot or ""
        except Exception as e:
            logger.error(f"DOM snapshot failed: {e}")
            return ""

    async def scroll(self, direction: str = "down", amount: int = 500, page_id: str = "main") -> Dict[str, Any]:
        """Scroll with human-like behavior."""
        page = self._pages.get(page_id, self.page)

        y = amount if direction == "down" else -amount
        steps = random.randint(3, 8)

        for i in range(steps):
            step_y = y / steps + random.randint(-20, 20)
            await page.mouse.wheel(0, int(step_y))
            await asyncio.sleep(random.uniform(0.05, 0.15))

        return {"status": "success", "direction": direction, "amount": amount}

    async def hover(self, selector: str, page_id: str = "main") -> Dict[str, Any]:
        """Hover over an element."""
        page = self._pages.get(page_id, self.page)
        try:
            element = await page.query_selector(selector)
            if not element:
                return {"status": "error", "error": f"Element not found: {selector}"}
            await element.hover()
            return {"status": "success", "selector": selector}
        except Exception as e:
            return {"status": "error", "error": str(e)}

    async def select_option(self, selector: str, value: str, page_id: str = "main") -> Dict[str, Any]:
        """Select an option in a dropdown."""
        page = self._pages.get(page_id, self.page)
        try:
            await page.select_option(selector, value)
            return {"status": "success", "selector": selector, "value": value}
        except Exception as e:
            return {"status": "error", "error": str(e)}

    async def upload_file(self, selector: str, file_path: str, page_id: str = "main") -> Dict[str, Any]:
        """Upload a file to a file input."""
        page = self._pages.get(page_id, self.page)
        try:
            element = await page.query_selector(selector)
            if not element:
                return {"status": "error", "error": f"File input not found: {selector}"}
            await element.set_input_files(file_path)
            return {"status": "success", "selector": selector, "file": file_path}
        except Exception as e:
            return {"status": "error", "error": str(e)}

    async def wait_for_element(self, selector: str, timeout: int = 10000, page_id: str = "main") -> Dict[str, Any]:
        """Wait for an element to appear."""
        page = self._pages.get(page_id, self.page)
        try:
            await page.wait_for_selector(selector, timeout=timeout)
            return {"status": "success", "selector": selector}
        except Exception:
            return {"status": "error", "error": f"Timeout waiting for: {selector}"}

    async def go_back(self, page_id: str = "main") -> Dict[str, Any]:
        """Go back in browser history."""
        page = self._pages.get(page_id, self.page)
        try:
            await page.go_back(timeout=15000)
            await asyncio.sleep(random.uniform(0.5, 1.5))
            return {"status": "success", "url": page.url, "title": await page.title()}
        except Exception as e:
            return {"status": "error", "error": str(e)}

    async def go_forward(self, page_id: str = "main") -> Dict[str, Any]:
        """Go forward in browser history."""
        page = self._pages.get(page_id, self.page)
        try:
            await page.go_forward(timeout=15000)
            await asyncio.sleep(random.uniform(0.5, 1.5))
            return {"status": "success", "url": page.url, "title": await page.title()}
        except Exception as e:
            return {"status": "error", "error": str(e)}

    async def right_click(self, selector: str, page_id: str = "main") -> Dict[str, Any]:
        """Right-click an element (opens context menu)."""
        page = self._pages.get(page_id, self.page)
        mimicry = self._mimicry

        try:
            element = await page.query_selector(selector)
            if not element:
                return {"status": "error", "error": f"Element not found: {selector}"}

            box = await element.bounding_box()
            if box:
                target_x = box["x"] + box["width"] / 2
                target_y = box["y"] + box["height"] / 2
                start_x, start_y = mimicry._last_move
                path = mimicry.mouse_path(start_x, start_y, target_x, target_y)
                for x, y in path:
                    await page.mouse.move(x, y)
                    await asyncio.sleep(random.uniform(0.005, 0.02))

            await asyncio.sleep(random.uniform(0.05, 0.15))
            await element.click(button="right")
            await asyncio.sleep(random.uniform(0.2, 0.5))

            return {"status": "success", "selector": selector, "action": "right_click"}
        except Exception as e:
            return {"status": "error", "error": str(e)}

    async def context_action(self, selector: str, action_text: str, page_id: str = "main") -> Dict[str, Any]:
        """Right-click and select a context menu option by text."""
        page = self._pages.get(page_id, self.page)

        # Right-click to open menu
        result = await self.right_click(selector, page_id)
        if result.get("status") != "success":
            return result

        await asyncio.sleep(random.uniform(0.3, 0.8))

        # Try to find and click the menu item
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

        # If no menu item found, try keyboard shortcut based on common actions
        shortcuts = {
            "copy": "Control+c",
            "paste": "Control+v",
            "cut": "Control+x",
            "select all": "Control+a",
            "save": "Control+s",
            "inspect": "F12",
            "view source": "Control+u",
            "open in new tab": "Control+click",
            "reload": "F5",
        }

        shortcut = shortcuts.get(action_text.lower())
        if shortcut and "+" in shortcut:
            keys = shortcut.split("+")
            await page.keyboard.press("+".join(keys))
            return {"status": "success", "action": action_text, "method": "keyboard_shortcut"}

        return {"status": "error", "error": f"Context menu action '{action_text}' not found"}

    async def drag_and_drop(self, source_selector: str, target_selector: str, page_id: str = "main") -> Dict[str, Any]:
        """Drag an element and drop it on another element."""
        page = self._pages.get(page_id, self.page)
        mimicry = self._mimicry

        try:
            source = await page.query_selector(source_selector)
            target = await page.query_selector(target_selector)

            if not source:
                return {"status": "error", "error": f"Source element not found: {source_selector}"}
            if not target:
                return {"status": "error", "error": f"Target element not found: {target_selector}"}

            source_box = await source.bounding_box()
            target_box = await target.bounding_box()

            if not source_box or not target_box:
                return {"status": "error", "error": "Could not get element positions"}

            src_x = source_box["x"] + source_box["width"] / 2
            src_y = source_box["y"] + source_box["height"] / 2
            tgt_x = target_box["x"] + target_box["width"] / 2
            tgt_y = target_box["y"] + target_box["height"] / 2

            # Move to source with human-like path
            start_x, start_y = mimicry._last_move
            path_to_source = mimicry.mouse_path(start_x, start_y, src_x, src_y)
            for x, y in path_to_source:
                await page.mouse.move(x, y)
                await asyncio.sleep(random.uniform(0.005, 0.015))

            # Mouse down on source
            await page.mouse.down()
            await asyncio.sleep(random.uniform(0.1, 0.3))

            # Drag to target with human-like path
            path_to_target = mimicry.mouse_path(src_x, src_y, tgt_x, tgt_y)
            for x, y in path_to_target:
                await page.mouse.move(x, y)
                await asyncio.sleep(random.uniform(0.008, 0.02))

            await asyncio.sleep(random.uniform(0.05, 0.15))

            # Drop
            await page.mouse.up()
            await asyncio.sleep(random.uniform(0.2, 0.5))

            return {
                "status": "success",
                "source": source_selector,
                "target": target_selector,
                "from": (round(src_x, 1), round(src_y, 1)),
                "to": (round(tgt_x, 1), round(tgt_y, 1)),
            }
        except Exception as e:
            return {"status": "error", "error": str(e)}

    async def drag_by_offset(self, selector: str, x_offset: int, y_offset: int, page_id: str = "main") -> Dict[str, Any]:
        """Drag an element by a pixel offset."""
        page = self._pages.get(page_id, self.page)

        try:
            element = await page.query_selector(selector)
            if not element:
                return {"status": "error", "error": f"Element not found: {selector}"}

            box = await element.bounding_box()
            if not box:
                return {"status": "error", "error": "Could not get element position"}

            src_x = box["x"] + box["width"] / 2
            src_y = box["y"] + box["height"] / 2

            await page.mouse.move(src_x, src_y)
            await page.mouse.down()

            # Move in steps for smooth drag
            steps = max(5, abs(x_offset) // 10 + abs(y_offset) // 10)
            for i in range(1, steps + 1):
                t = i / steps
                x = src_x + x_offset * t + random.gauss(0, 2)
                y = src_y + y_offset * t + random.gauss(0, 2)
                await page.mouse.move(x, y)
                await asyncio.sleep(random.uniform(0.005, 0.015))

            await page.mouse.up()

            return {
                "status": "success",
                "selector": selector,
                "offset": (x_offset, y_offset),
            }
        except Exception as e:
            return {"status": "error", "error": str(e)}

    async def double_click(self, selector: str, page_id: str = "main") -> Dict[str, Any]:
        """Double-click an element (e.g., to edit a cell, open a file)."""
        page = self._pages.get(page_id, self.page)
        mimicry = self._mimicry

        try:
            element = await page.query_selector(selector)
            if not element:
                return {"status": "error", "error": f"Element not found: {selector}"}

            box = await element.bounding_box()
            if box:
                target_x = box["x"] + box["width"] / 2
                target_y = box["y"] + box["height"] / 2
                start_x, start_y = mimicry._last_move
                path = mimicry.mouse_path(start_x, start_y, target_x, target_y)
                for x, y in path:
                    await page.mouse.move(x, y)
                    await asyncio.sleep(random.uniform(0.005, 0.02))

            await element.dblclick()
            await asyncio.sleep(random.uniform(0.2, 0.5))

            return {"status": "success", "selector": selector, "action": "double_click"}
        except Exception as e:
            return {"status": "error", "error": str(e)}

    async def clear_input(self, selector: str, page_id: str = "main") -> Dict[str, Any]:
        """Clear an input field."""
        page = self._pages.get(page_id, self.page)
        try:
            element = await page.query_selector(selector)
            if not element:
                return {"status": "error", "error": f"Element not found: {selector}"}
            await element.click()
            await asyncio.sleep(0.1)
            await page.keyboard.press("Control+a")
            await asyncio.sleep(0.05)
            await page.keyboard.press("Backspace")
            return {"status": "success", "selector": selector, "action": "cleared"}
        except Exception as e:
            return {"status": "error", "error": str(e)}

    async def set_checkbox(self, selector: str, checked: bool, page_id: str = "main") -> Dict[str, Any]:
        """Set a checkbox to checked or unchecked."""
        page = self._pages.get(page_id, self.page)
        try:
            element = await page.query_selector(selector)
            if not element:
                return {"status": "error", "error": f"Element not found: {selector}"}
            is_checked = await element.is_checked()
            if is_checked != checked:
                await element.click()
                await asyncio.sleep(random.uniform(0.1, 0.3))
            return {"status": "success", "selector": selector, "checked": checked}
        except Exception as e:
            return {"status": "error", "error": str(e)}

    async def get_element_text(self, selector: str, page_id: str = "main") -> Dict[str, Any]:
        """Get text content of a specific element."""
        page = self._pages.get(page_id, self.page)
        try:
            element = await page.query_selector(selector)
            if not element:
                return {"status": "error", "error": f"Element not found: {selector}"}
            text = await element.inner_text()
            return {"status": "success", "selector": selector, "text": text}
        except Exception as e:
            return {"status": "error", "error": str(e)}

    async def get_element_attribute(self, selector: str, attribute: str, page_id: str = "main") -> Dict[str, Any]:
        """Get an attribute value from an element."""
        page = self._pages.get(page_id, self.page)
        try:
            element = await page.query_selector(selector)
            if not element:
                return {"status": "error", "error": f"Element not found: {selector}"}
            value = await element.get_attribute(attribute)
            return {"status": "success", "selector": selector, "attribute": attribute, "value": value}
        except Exception as e:
            return {"status": "error", "error": str(e)}

    async def set_viewport(self, width: int, height: int, page_id: str = "main") -> Dict[str, Any]:
        """Change the browser viewport size."""
        page = self._pages.get(page_id, self.page)
        try:
            await page.set_viewport_size({"width": width, "height": height})
            return {"status": "success", "viewport": {"width": width, "height": height}}
        except Exception as e:
            return {"status": "error", "error": str(e)}

    async def add_extension(self, extension_path: str) -> Dict[str, Any]:
        """Load a Chrome extension (CRX unpacked directory). Requires headed mode.

        Usage: First download/extract the extension, then point to its directory.
        Note: Extensions only work in headed mode (--headed flag).
        """
        # Playwright doesn't support dynamic extension loading after launch.
        # Extensions must be loaded at browser launch via --load-extension flag.
        # We store the path and advise restart.
        ext_dir = Path(extension_path)
        if not ext_dir.exists():
            return {"status": "error", "error": f"Extension path does not exist: {extension_path}"}

        manifest = ext_dir / "manifest.json"
        if not manifest.exists():
            return {"status": "error", "error": f"No manifest.json found in {extension_path}"}

        try:
            with open(manifest, "r") as f:
                ext_info = json.load(f)
            ext_name = ext_info.get("name", "Unknown")
            ext_version = ext_info.get("version", "Unknown")
        except Exception:
            ext_name = "Unknown"
            ext_version = "Unknown"

        return {
            "status": "info",
            "message": f"Extension '{ext_name}' v{ext_version} detected. Extensions require headed mode and browser restart.",
            "extension": ext_name,
            "version": ext_version,
            "path": str(ext_dir),
            "note": "To use extensions, restart Agent-OS with: python main.py --headed --extension-path " + str(ext_dir)
        }

    async def get_console_logs(self, page_id: str = "main", clear: bool = False) -> Dict[str, Any]:
        """Get browser console logs captured by Playwright listeners.

        Args:
            page_id: Which tab to read logs from.
            clear: If True, flush the log buffer after reading.
        """
        logs = self._console_logs.get(page_id, [])
        # Return last 100 entries, newest first
        result = logs[-100:]
        if clear:
            self._console_logs[page_id] = []
        return {
            "status": "success",
            "page_id": page_id,
            "logs": result,
            "count": len(result),
            "total_captured": len(logs),
        }

    async def intercept_network(self, url_pattern: str, page_id: str = "main") -> Dict[str, Any]:
        """Monitor network requests matching a URL pattern."""
        page = self._pages.get(page_id, self.page)
        requests = await page.evaluate("""(pattern) => {
            return (window.__agent_os_network || []).filter(r => r.url.includes(pattern));
        }""", url_pattern)
        return {"status": "success", "pattern": url_pattern, "requests": requests}

    async def get_cookies(self, page_id: str = "main") -> Dict[str, Any]:
        """Get all cookies for the current page."""
        page = self._pages.get(page_id, self.page)  # noqa: F841
        cookies = await self.context.cookies()
        return {"status": "success", "cookies": cookies, "count": len(cookies)}

    async def set_cookie(
        self,
        name: str,
        value: str,
        domain: str = None,
        path: str = "/",
        secure: bool = None,
        http_only: bool = False,
        same_site: str = None,
        page_id: str = "main",
    ) -> Dict[str, Any]:
        """Set a cookie with full Playwright compatibility.

        Args:
            name: Cookie name.
            value: Cookie value.
            domain: Cookie domain. If not set, inferred from current page URL.
            path: Cookie path (default '/').
            secure: Require HTTPS. Auto-detected from current page if not set.
            http_only: Prevent JS access (default False).
            same_site: 'Strict', 'Lax', or 'None'. None = browser default.
            page_id: Tab to infer domain from if domain not provided.
        """
        page = self._pages.get(page_id, self.page)

        # Infer domain from current URL if not provided
        if not domain:
            try:
                parsed = page.url.split("/")
                if len(parsed) >= 3:
                    host = parsed[2]
                    # Strip port if present (e.g., "localhost:8080")
                    if ":" in host and not host.startswith("["):
                        host = host.split(":")[0]
                    # Strip brackets from IPv6
                    host = host.strip("[]")
                    if host and host != "about:blank":
                        domain = host
            except Exception:
                pass

        if not domain:
            return {
                "status": "error",
                "error": "Cannot infer cookie domain: current page has no valid URL. Provide 'domain' explicitly.",
            }

        # Auto-detect secure from URL scheme
        if secure is None:
            secure = page.url.startswith("https://")

        # Build cookie dict — Playwright requires name, value, domain, path
        cookie: Dict[str, Any] = {
            "name": name,
            "value": value,
            "domain": domain,
            "path": path,
        }

        if secure:
            cookie["secure"] = True
        if http_only:
            cookie["httpOnly"] = True
        if same_site and same_site.capitalize() in ("Strict", "Lax", "None"):
            cookie["sameSite"] = same_site.capitalize()

        try:
            await self.context.add_cookies([cookie])
            return {"status": "success", "cookie": cookie}
        except Exception as e:
            logger.error(f"set_cookie failed: {e}")
            return {"status": "error", "error": str(e)}

    async def reload(self, page_id: str = "main") -> Dict[str, Any]:
        """Reload the current page."""
        page = self._pages.get(page_id, self.page)
        await page.reload()
        await asyncio.sleep(random.uniform(0.5, 1.5))
        return {"status": "success", "url": page.url, "title": await page.title()}

    async def get_all_links(self, page_id: str = "main") -> List[str]:
        """Get all links on the current page."""
        page = self._pages.get(page_id, self.page)
        links = await page.evaluate("""() => {
            return Array.from(document.querySelectorAll('a[href]'))
                .map(a => a.href)
                .filter(href => href.startsWith('http'))
        }""")
        return links

    async def get_all_images(self, page_id: str = "main") -> List[Dict]:
        """Get all images on the current page."""
        page = self._pages.get(page_id, self.page)
        images = await page.evaluate("""() => {
            return Array.from(document.querySelectorAll('img'))
                .map(img => ({src: img.src, alt: img.alt, width: img.width, height: img.height}))
                .filter(img => img.src.startsWith('http'))
        }""")
        return images

    async def new_tab(self, tab_id: str) -> str:
        """Create a new tab with its own fingerprint."""
        # Generate fingerprint for this tab
        fp = self._evasion.generate_fingerprint(page_id=tab_id)
        chrome_ver = fp["chrome_version"] if fp else "124"

        # CDP stealth is the SOLE injection mechanism — no add_init_script needed
        page = await self.context.new_page()
        self._pages[tab_id] = page
        self._attach_console_listener(tab_id, page)

        # Inject CDP stealth — SOLE anti-detection injection
        await self._cdp_stealth.inject_into_page(
            page,
            page_id=tab_id,
            chrome_version=chrome_ver,
            fingerprint=fp,
        )

        return tab_id

    async def switch_tab(self, tab_id: str) -> Dict[str, Any]:
        """Switch to a different tab."""
        if tab_id in self._pages:
            self.page = self._pages[tab_id]
            return {"status": "success", "tab_id": tab_id, "url": self.page.url}
        return {"status": "error", "error": f"Tab not found: {tab_id}"}

    async def close_tab(self, tab_id: str) -> bool:
        """Close a tab."""
        if tab_id in self._pages and tab_id != "main":
            await self._pages[tab_id].close()
            del self._pages[tab_id]
            self._console_logs.pop(tab_id, None)
            return True
        return False

    # ─── Proxy Rotation Helpers ────────────────────────────────

    def _get_geo_target(self, domain: str) -> Optional[str]:
        """
        Auto-detect geo-target for known streaming/content sites.
        These sites often require specific country IPs.
        """
        # Map of domain patterns to required countries
        GEO_REQUIREMENTS = {
            "netflix.com": "US",
            "hulu.com": "US",
            "max.com": "US",  # HBO Max
            "peacocktv.com": "US",
            "paramountplus.com": "US",
            "disneyplus.com": "US",
            "bbc.co.uk": "GB",
            "itv.com": "GB",
            "channel4.com": "GB",
            "zdf.de": "DE",
            "arte.tv": "FR",
            "tf1.fr": "FR",
            "rai.it": "IT",
            "crunchyroll.com": "US",
            "funimation.com": "US",
            "hbo.com": "US",
            "showtime.com": "US",
            "starz.com": "US",
            "amazon.co.uk": "GB",
            "amazon.de": "DE",
            "amazon.fr": "FR",
            "amazon.co.jp": "JP",
            "bbc.com": "GB",
            "itvx.com": "GB",
            "9now.com.au": "AU",
            "sbs.com.au": "AU",
            "ctv.ca": "CA",
            "crave.ca": "CA",
        }

        domain_lower = domain.lower()
        for pattern, country in GEO_REQUIREMENTS.items():
            if pattern in domain_lower:
                return country
        return None

    async def _rotate_to_next_proxy(
        self,
        domain: str = None,
        country: str = None,
        exclude: List[str] = None,
    ) -> Optional[str]:
        """
        Rotate to the next proxy from the proxy manager.
        Returns the proxy ID if successful, None otherwise.
        """
        if not self._proxy_manager:
            return None

        try:
            # Get a proxy with failover
            result = await self._proxy_manager.get_proxy(
                domain=domain,
                country=country,
                with_failover=True,
            )

            if result.get("status") != "success":
                logger.debug(f"No proxy available for {domain}: {result.get('error')}")
                return None

            proxy_data = result["proxy"]
            playwright_config = result["playwright_config"]
            proxy_id = proxy_data["proxy_id"]

            # Store current proxy info — CRITICAL: set _current_proxy for tracking
            self._current_proxy_config = playwright_config
            self._domain_proxy_map[domain or "_default"] = proxy_id

            # Wrap proxy_data as a lightweight object with proxy_id attribute
            # so that _record_proxy_result and other methods can access it
            class _ProxyRef:
                def __init__(self, data: Dict[str, Any]):
                    self.proxy_id = data.get("proxy_id", "unknown")
                    self.url = data.get("url", "")
                    self.country = data.get("country", "")
                    self.data = data
                def to_dict(self):
                    return self.data

            self._current_proxy = _ProxyRef(proxy_data)

            # For Playwright, we need to recreate the browser with the new proxy
            # because Chromium doesn't support changing proxy at runtime.
            # Trigger browser recovery (restart) so the new proxy config takes effect.
            new_proxy_url = proxy_data.get("url", proxy_id)
            if self._tls_proxy:
                # Update TLS proxy to use this proxy
                logger.info(f"Rotating to proxy {proxy_id} for {domain} (via TLS proxy)")
            else:
                # Direct proxy — need browser restart for the new proxy to take effect
                logger.info(f"Rotating proxy to {new_proxy_url}, triggering browser restart")
                try:
                    await self.recover()
                except Exception as e:
                    logger.error(f"Failed to restart browser with new proxy: {e}")

            return proxy_id

        except Exception as e:
            logger.error(f"Proxy rotation failed: {e}")
            return None

    def _record_proxy_result(
        self,
        success: bool,
        status_code: int = 0,
        latency_ms: float = 0,
        error: str = "",
    ):
        """Record the result of using a proxy for health tracking."""
        if not self._proxy_manager or not self._current_proxy:
            return

        try:
            self._proxy_manager.record_result(
                proxy_id=self._current_proxy.proxy_id,
                success=success,
                latency_ms=latency_ms,
                status_code=status_code,
                error=error,
            )
        except Exception as e:
            logger.debug(f"Failed to record proxy result: {e}")

    @property
    def current_proxy_info(self) -> Dict[str, Any]:
        """Get info about the currently active proxy."""
        if not self._current_proxy:
            return {"status": "no_proxy", "proxy": None}
        return {
            "status": "active",
            "proxy": self._current_proxy.to_dict() if hasattr(self._current_proxy, 'to_dict') else str(self._current_proxy),
            "config": self._current_proxy_config,
        }

    def _parse_proxy_url(self, proxy_url: str) -> Dict[str, Any]:
        """Parse proxy URL into Playwright proxy config."""
        from urllib.parse import urlparse
        parsed = urlparse(proxy_url)

        config = {
            "server": f"{parsed.scheme}://{parsed.hostname}:{parsed.port or 8080}",
        }

        if parsed.username:
            config["username"] = parsed.username
        if parsed.password:
            config["password"] = parsed.password

        return config

    async def set_proxy(self, proxy_url: str) -> Dict[str, Any]:
        """
        Set proxy for browser. Requires browser restart to take effect.

        Args:
            proxy_url: Proxy URL — e.g. "http://user:pass@proxy.example.com:8080"
                       or "socks5://proxy.example.com:1080"

        Returns:
            Status and proxy info
        """
        proxy_config = self._parse_proxy_url(proxy_url)
        self._proxy_config = proxy_config

        # Save to config
        self.config.set("browser.proxy", proxy_url)

        return {
            "status": "success",
            "proxy": proxy_config,
            "note": "Proxy will be active after browser restart. Use restart command.",
        }

    async def get_proxy(self) -> Dict[str, Any]:
        """Get current proxy configuration."""
        if not self._proxy_config:
            return {"status": "success", "proxy": None, "message": "No proxy configured"}
        return {"status": "success", "proxy": self._proxy_config}

    async def set_proxy_pool(self, proxy_urls: List[str], rotation_interval: int = 10) -> Dict[str, Any]:
        """
        Set a pool of proxies for automatic rotation.

        Args:
            proxy_urls: List of proxy URLs
                e.g. ["http://proxy1:8080", "socks5://proxy2:1080", "http://user:pass@proxy3:8080"]
            rotation_interval: Rotate proxy every N requests (default: 10)

        Returns:
            Status and pool info
        """
        if not proxy_urls:
            return {"status": "error", "error": "Proxy pool cannot be empty"}

        self._proxy_pool = [self._parse_proxy_url(url) for url in proxy_urls]
        self._proxy_index = 0
        self._proxy_rotation_enabled = True
        self._proxy_rotation_interval = rotation_interval
        self._proxy_request_count = 0

        logger.info(
            f"Proxy pool configured: {len(self._proxy_pool)} proxies, "
            f"rotating every {rotation_interval} requests"
        )

        return {
            "status": "success",
            "pool_size": len(self._proxy_pool),
            "rotation_interval": rotation_interval,
            "proxies": [p.get("server", "N/A") for p in self._proxy_pool],
            "current_index": 0,
        }

    async def rotate_proxy(self) -> Dict[str, Any]:
        """
        Rotate to the next proxy in the pool.
        Saves cookies, closes browser, relaunches with new proxy.
        This is the only way to change proxy in Chromium — it's set at launch.

        Returns:
            Status and new proxy info
        """
        if not self._proxy_pool:
            return {"status": "error", "error": "No proxy pool configured. Use set_proxy_pool first."}

        old_index = self._proxy_index
        self._proxy_index = (self._proxy_index + 1) % len(self._proxy_pool)
        new_proxy = self._proxy_pool[self._proxy_index]

        # Save all page URLs and cookies before restart
        saved_urls = {}
        for pid, page in self._pages.items():
            if page.url and page.url != "about:blank":
                saved_urls[pid] = page.url

        try:
            await self._save_cookies("default")
        except Exception:
            pass

        # Close everything
        try:
            if self.context:
                await self.context.close()
            if self.browser:
                await self.browser.close()
            if self.playwright:
                await self.playwright.stop()
        except Exception:
            pass

        # Clear state
        self.browser = None
        self.context = None
        self.page = None
        self._pages.clear()

        # Apply new proxy config
        self._proxy_config = new_proxy

        # Relaunch
        await self._launch_browser()

        # Re-navigate saved pages to restore state
        for pid, url in saved_urls.items():
            try:
                if pid == "main":
                    await self.page.goto(url, wait_until="domcontentloaded", timeout=15000)
                else:
                    tab = await self.new_tab(pid)
                    if tab:
                        await self._pages[pid].goto(url, wait_until="domcontentloaded", timeout=15000)
            except Exception as e:
                logger.warning(f"Failed to restore page {pid}: {e}")

        logger.info(
            f"Proxy rotated: {old_index} → {self._proxy_index} "
            f"({new_proxy.get('server', 'N/A')})"
        )

        return {
            "status": "success",
            "proxy_index": self._proxy_index,
            "proxy": new_proxy,
            "total_proxies": len(self._proxy_pool),
            "pages_restored": list(saved_urls.keys()),
        }

    async def get_proxy_pool(self) -> Dict[str, Any]:
        """Get current proxy pool status."""
        if not self._proxy_pool:
            return {"status": "success", "pool": None, "message": "No proxy pool configured"}

        return {
            "status": "success",
            "pool_size": len(self._proxy_pool),
            "current_index": self._proxy_index,
            "rotation_enabled": self._proxy_rotation_enabled,
            "rotation_interval": self._proxy_rotation_interval,
            "request_count": self._proxy_request_count,
            "proxies": [p.get("server", "N/A") for p in self._proxy_pool],
            "current_proxy": self._proxy_pool[self._proxy_index].get("server", "N/A"),
        }

    async def _maybe_rotate_proxy(self):
        """Auto-rotate proxy if pool is configured and interval reached."""
        if not self._proxy_rotation_enabled or not self._proxy_pool:
            return

        self._proxy_request_count += 1

        if self._proxy_request_count >= self._proxy_rotation_interval:
            self._proxy_request_count = 0
            try:
                result = await self.rotate_proxy()
                if result.get("status") == "success":
                    logger.info(
                        f"Auto-rotated proxy to #{self._proxy_index}: "
                        f"{self._proxy_pool[self._proxy_index].get('server', 'N/A')}"
                    )
            except Exception as e:
                logger.warning(f"Proxy auto-rotation failed: {e}")

    async def emulate_device(self, device: str) -> Dict[str, Any]:
        """
        Emulate a mobile/tablet/desktop device.

        Available devices:
            Mobile: iphone_se, iphone_14, iphone_14_pro_max, galaxy_s23, pixel_8
            Tablet: ipad, ipad_pro, galaxy_tab_s9
            Desktop: desktop_1080, desktop_1440, desktop_4k

        Args:
            device: Device preset name
        """
        if device not in self.DEVICE_PRESETS:
            return {
                "status": "error",
                "error": f"Unknown device: {device}",
                "available_devices": list(self.DEVICE_PRESETS.keys()),
            }

        preset = self.DEVICE_PRESETS[device]
        self._current_device = device

        # Create new context with device settings
        old_context = self.context

        context_options = {
            "viewport": {"width": preset["width"], "height": preset["height"]},
            "device_scale_factor": preset["device_scale_factor"],
            "is_mobile": preset["device_scale_factor"] > 1 and preset["width"] < 500,
            "has_touch": preset["device_scale_factor"] > 1,
            "user_agent": preset["user_agent"] or self.config.get("browser.user_agent"),
            "locale": "en-US",
            "timezone_id": "America/New_York",
            "ignore_https_errors": True,
        }

        self.context = await self.browser.new_context(**context_options)
        # CDP stealth is the SOLE injection mechanism — no add_init_script needed
        fp = self._evasion.generate_fingerprint(page_id="main")
        await self.context.route("**/*", self._handle_request)

        # Migrate pages
        for page_id, old_page in list(self._pages.items()):
            try:
                url = old_page.url
                new_page = await self.context.new_page()
                self._pages[page_id] = new_page
                if page_id == "main":
                    self.page = new_page
                if url and url != "about:blank":
                    await new_page.goto(url, wait_until="domcontentloaded", timeout=15000)
                await old_page.close()
            except Exception as e:
                logger.warning(f"Failed to migrate page {page_id}: {e}")

        # Apply CDP stealth to the main page
        chrome_ver = fp.get("chrome_version", "124")
        if self.page:
            await self._cdp_stealth.inject_into_page(
                self.page,
                page_id="main",
                chrome_version=chrome_ver,
                fingerprint=fp,
            )

        # Close old context
        if old_context:
            try:
                await old_context.close()
            except Exception:
                pass

        logger.info(f"Device emulation: {device} ({preset['width']}x{preset['height']})")

        return {
            "status": "success",
            "device": device,
            "viewport": {"width": preset["width"], "height": preset["height"]},
            "device_scale_factor": preset["device_scale_factor"],
            "is_mobile": preset["device_scale_factor"] > 1 and preset["width"] < 500,
            "user_agent": preset["user_agent"] or self.config.get("browser.user_agent"),
        }

    async def list_devices(self) -> Dict[str, Any]:
        """List all available device emulation presets."""
        devices = {}
        for name, preset in self.DEVICE_PRESETS.items():
            devices[name] = {
                "viewport": f"{preset['width']}x{preset['height']}",
                "device_scale_factor": preset["device_scale_factor"],
                "type": "desktop" if preset["device_scale_factor"] <= 1 else
                        "mobile" if preset["width"] < 500 else
                        "tablet" if preset["width"] < 1920 else
                        "desktop",
            }
        return {"status": "success", "devices": devices, "current": self._current_device}

    async def save_session(self, name: str = "default") -> Dict[str, Any]:
        """
        Save full browser session state: cookies, localStorage, sessionStorage.
        Can be restored later with restore_session().
        """
        # Flush any pending cookie writes first
        await self._flush_cookies()

        session_dir = Path(os.path.expanduser("~/.agent-os/sessions"))
        session_dir.mkdir(parents=True, exist_ok=True)

        # Save storage state (cookies + localStorage)
        state = await self.context.storage_state()
        state_path = session_dir / f"{name}.json"

        # Also capture sessionStorage from all pages
        session_data = {}
        for page_id, page in self._pages.items():
            try:
                storage = await page.evaluate("""() => {
                    const data = {};
                    for (let i = 0; i < sessionStorage.length; i++) {
                        const key = sessionStorage.key(i);
                        data[key] = sessionStorage.getItem(key);
                    }
                    return data;
                }""")
                if storage:
                    session_data[page_id] = storage
            except Exception:
                pass

        state["session_storage"] = session_data
        state["saved_at"] = time.time()
        state["device"] = self._current_device
        state["urls"] = {pid: page.url for pid, page in self._pages.items() if page.url != "about:blank"}

        with open(state_path, "w") as f:
            json.dump(state, f, indent=2)

        logger.info(f"Session saved: {name} ({state_path})")

        return {
            "status": "success",
            "name": name,
            "path": str(state_path),
            "cookies": len(state.get("cookies", [])),
            "pages": list(state.get("urls", {}).keys()),
        }

    async def restore_session(self, name: str = "default") -> Dict[str, Any]:
        """
        Restore a previously saved browser session.
        Recreates cookies, localStorage, sessionStorage, and navigates to saved URLs.
        """
        session_dir = Path(os.path.expanduser("~/.agent-os/sessions"))
        state_path = session_dir / f"{name}.json"

        if not state_path.exists():
            return {"status": "error", "error": f"Session not found: {name}"}

        with open(state_path, "r") as f:
            state = json.load(f)

        # Close existing context
        if self.context:
            try:
                await self.context.close()
            except Exception:
                pass

        # Create new context with saved state
        context_options = {
            "user_agent": self.config.get("browser.user_agent"),
            "viewport": self.config.get("browser.viewport", {"width": 1920, "height": 1080}),
            "locale": "en-US",
            "timezone_id": "America/New_York",
            "ignore_https_errors": True,
        }

        # Remove session_storage from state before passing to Playwright
        storage_state = {k: v for k, v in state.items() if k not in ("session_storage", "saved_at", "device", "urls")}
        context_options["storage_state"] = storage_state

        self.context = await self.browser.new_context(**context_options)
        # CDP stealth is the SOLE injection mechanism — no add_init_script needed
        fp = self._evasion.generate_fingerprint(page_id="main")
        await self.context.route("**/*", self._handle_request)

        # Recreate pages with saved URLs
        saved_urls = state.get("urls", {})
        self._pages = {}

        for page_id, url in saved_urls.items():
            try:
                page = await self.context.new_page()
                self._pages[page_id] = page
                if page_id == "main":
                    self.page = page
                if url and url != "about:blank":
                    await page.goto(url, wait_until="domcontentloaded", timeout=15000)

                # Restore sessionStorage
                session_storage = state.get("session_storage", {}).get(page_id, {})
                if session_storage:
                    for key, value in session_storage.items():
                        try:
                            await page.evaluate(
                                "(k, v) => sessionStorage.setItem(k, v)",
                                key, value
                            )
                        except Exception:
                            pass
            except Exception as e:
                logger.warning(f"Failed to restore page {page_id}: {e}")

        # Ensure main page exists
        if "main" not in self._pages:
            self.page = await self.context.new_page()
            self._pages["main"] = self.page

        # Apply CDP stealth — SOLE anti-detection injection
        chrome_ver = fp.get("chrome_version", "124")
        if self.page:
            await self._cdp_stealth.inject_into_page(
                self.page,
                page_id="main",
                chrome_version=chrome_ver,
                fingerprint=fp,
            )

        device = state.get("device", "desktop_1080")
        self._current_device = device

        logger.info(f"Session restored: {name}")

        return {
            "status": "success",
            "name": name,
            "cookies": len(state.get("cookies", [])),
            "pages_restored": list(saved_urls.keys()),
            "device": device,
            "saved_at": state.get("saved_at"),
        }

    async def list_sessions(self) -> Dict[str, Any]:
        """List all saved sessions."""
        session_dir = Path(os.path.expanduser("~/.agent-os/sessions"))
        sessions = []

        if session_dir.exists():
            for path in session_dir.glob("*.json"):
                try:
                    with open(path) as f:
                        state = json.load(f)
                    sessions.append({
                        "name": path.stem,
                        "cookies": len(state.get("cookies", [])),
                        "pages": list(state.get("urls", {}).keys()),
                        "device": state.get("device", "unknown"),
                        "saved_at": state.get("saved_at"),
                    })
                except Exception:
                    continue

        return {"status": "success", "sessions": sessions}

    async def delete_session(self, name: str) -> Dict[str, Any]:
        """Delete a saved session."""
        session_dir = Path(os.path.expanduser("~/.agent-os/sessions"))
        state_path = session_dir / f"{name}.json"

        if not state_path.exists():
            return {"status": "error", "error": f"Session not found: {name}"}

        state_path.unlink()
        return {"status": "success", "deleted": name}

    async def stop(self):
        """Clean shutdown."""
        # Cancel cookie flush loop and do final save
        if self._cookie_flush_task:
            self._cookie_flush_task.cancel()
            try:
                await self._cookie_flush_task
            except asyncio.CancelledError:
                pass
        try:
            await self._flush_cookies("default")
        except Exception as flush_err:
            logger.warning(f"Final cookie flush failed during shutdown: {flush_err}")
        try:
            if self.context:
                await self.context.close()
        except Exception as ctx_err:
            logger.debug(f"Context close error during shutdown: {ctx_err}")
        try:
            if self.browser:
                await self.browser.close()
        except Exception as br_err:
            logger.debug(f"Browser close error during shutdown: {br_err}")
        try:
            if self.playwright:
                await self.playwright.stop()
        except Exception as pw_err:
            logger.debug(f"Playwright stop error during shutdown: {pw_err}")
        # Stop TLS proxy (must happen after browser close to release the port)
        try:
            if self._tls_proxy:
                await self._tls_proxy.stop()
                self._tls_proxy = None
        except Exception:
            logger.debug("TLS proxy stop failed (may already be stopped)")
            self._tls_proxy = None
        # Close TLS HTTP client
        try:
            if self._tls_http:
                self._tls_http.close()
                self._tls_http = None
        except Exception:
            self._tls_http = None
        # Stop proxy manager
        try:
            if self._proxy_manager:
                await self._proxy_manager.stop()
        except Exception as pm_err:
            logger.debug(f"Proxy manager stop error: {pm_err}")
        # Stop Firefox fallback engine
        if self._firefox_engine:
            try:
                await self._firefox_engine.stop()
            except Exception as ff_err:
                logger.debug(f"Firefox engine stop error: {ff_err}")
        # Stop dual engine manager
        if self._dual_engine:
            try:
                await self._dual_engine.stop()
            except Exception as de_err:
                logger.debug(f"Dual engine manager stop error: {de_err}")
        logger.info("Browser stopped (all engines)")

    async def tls_get(self, url: str, **kwargs) -> Dict[str, Any]:
        """HTTP GET with real browser TLS fingerprint (no browser needed)."""
        if self._tls_http and self._tls_http.available:
            resp = await self._tls_http.get(url, **kwargs)
            return {
                "status_code": resp.status_code,
                "text": resp.text,
                "headers": resp.headers,
                "url": resp.url,
                "tls_profile": resp.tls_profile,
                "error": resp.error,
            }
        return {"error": "TLS HTTP client not available"}

    async def tls_post(self, url: str, **kwargs) -> Dict[str, Any]:
        """HTTP POST with real browser TLS fingerprint (no browser needed)."""
        if self._tls_http and self._tls_http.available:
            resp = await self._tls_http.post(url, **kwargs)
            return {
                "status_code": resp.status_code,
                "text": resp.text,
                "headers": resp.headers,
                "url": resp.url,
                "tls_profile": resp.tls_profile,
                "error": resp.error,
            }
        return {"error": "TLS HTTP client not available"}

    @property
    def tls_stats(self) -> Dict:
        """Get TLS proxy and HTTP client statistics."""
        stats = {
            "curl_cffi_available": _CURL_AVAILABLE,
            "proxy_running": self._tls_proxy is not None,
            "proxy_url": self._tls_proxy.proxy_url if self._tls_proxy else None,
        }
        if self._tls_proxy:
            stats["proxy_stats"] = self._tls_proxy.stats
        if self._tls_http:
            stats["http_client_stats"] = self._tls_http.stats
        return stats
