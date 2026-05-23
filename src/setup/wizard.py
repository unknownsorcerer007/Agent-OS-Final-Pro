"""
Agent-OS Setup Wizard
First-launch interactive setup for optional API keys and preferences.
Like Open Claude's approach: everything works without API keys,
but users can add their own keys for enhanced features.

This wizard runs only on first launch (when no config exists) or
when the user explicitly requests it with --setup.
"""
import os
import sys
import secrets
import logging
from pathlib import Path
from typing import Dict, Any, Optional

logger = logging.getLogger("agent-os.setup")


# ─── ASCII Art Banner ───────────────────────────────────────────

BANNER = r"""
   ___              _   _       ___  ___
  / __| _ _  __ _  | | | |___  | _ )/ __|
  \__ \| '_ \/ _` | | | / / -_) | _ \\__ \
  |___/| .__/\__, | |_|_\_\___| |___/|___/
       |_|   |___/
  ___________________________________________
  AI Agent Browser — Zero-API-Required Edition
  ___________________________________________
"""

SKIP_HINT = "  (Press Enter to skip)"


class SetupWizard:
    """
    Interactive first-launch setup wizard.

    All API keys are OPTIONAL. Agent-OS runs fully self-contained
    with zero external dependencies. Keys only unlock enhanced
    features like CAPTCHA solving, proxy APIs, etc.
    """

    # Define what optional integrations we support
    OPTIONAL_INTEGRATIONS = {
        "captcha": {
            "name": "CAPTCHA Solving (2Captcha / Anti-Captcha)",
            "description": "Auto-solve CAPTCHAs on protected sites",
            "env_vars": {
                "CAPTCHA_API_KEY": "Your 2Captcha or Anti-Captcha API key",
            },
            "free_alternative": "Built-in CAPTCHA prevention (blocking CAPTCHA scripts) works without a key",
        },
        "proxy": {
            "name": "Proxy Rotation Provider",
            "description": "Residential proxy rotation for large-scale scraping",
            "env_vars": {
                "PROXY_PROVIDER": "Provider name (brightdata, oxylabs, smartproxy, iproyal)",
                "PROXY_PROVIDER_CUSTOMER_ID": "Your customer/account ID",
                "PROXY_PROVIDER_PASSWORD": "Your provider password",
                "PROXY_PROVIDER_ZONE": "Zone name (default: residential)",
            },
            "free_alternative": "Built-in TLS fingerprint spoofing + smart retries work without proxies",
        },
        "whisper": {
            "name": "Whisper Transcription (Local)",
            "description": "Transcribe audio/video content from pages",
            "env_vars": {
                "WHISPER_MODEL": "Model size (tiny, base, small, medium, large)",
            },
            "free_alternative": "Uses local Whisper model — free, but requires pip install openai-whisper",
        },
        "llm": {
            "name": "LLM Enhancement (Optional)",
            "description": "Use your own LLM API key for smarter page analysis & summarization",
            "env_vars": {
                "LLM_API_KEY": "Your OpenAI/Anthropic/Groq API key",
                "LLM_API_BASE": "API base URL (default: OpenAI)",
                "LLM_MODEL": "Model name (default: gpt-4o-mini)",
            },
            "free_alternative": "Built-in rule-based page analysis works without any LLM",
        },
    }

    def __init__(self, config_path: Optional[str] = None):
        self.config_dir = Path(config_path or os.path.expanduser("~/.agent-os"))
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.config_file = self.config_dir / "config.yaml"
        # Write .env to both the app directory (where main.py loads it) and home config
        self.env_file_app = Path(__file__).parent.parent.parent / ".env"
        self.env_file_home = self.config_dir / ".env"
        self._answers: Dict[str, Any] = {}

    def is_first_launch(self) -> bool:
        """Check if this is the first time Agent-OS is being launched."""
        return not self.config_file.exists()

    def _print_banner(self):
        """Print the welcome banner."""
        print(BANNER)
        print("  Welcome to Agent-OS Setup!")
        print()
        print("  Agent-OS is 100% self-contained and runs without ANY")
        print("  external API keys. The following integrations are")
        print("  OPTIONAL — add keys only if you want enhanced features.")
        print()
        print("  You can always add keys later by running:")
        print("    python main.py --setup")
        print()
        print("=" * 50)

    def _ask_yes_no(self, prompt: str, default: bool = False) -> bool:
        """Ask a yes/no question."""
        suffix = " [Y/n] " if default else " [y/N] "
        while True:
            answer = input(prompt + suffix).strip().lower()
            if not answer:
                return default
            if answer in ("y", "yes"):
                return True
            if answer in ("n", "no"):
                return False
            print("  Please enter y or n.")

    def _ask_input(self, prompt: str, default: str = "", optional: bool = True) -> str:
        """Ask for text input."""
        suffix = SKIP_HINT if optional else ""
        answer = input(f"  {prompt}{suffix}: ").strip()
        return answer or default

    def _ask_integration(self, key: str, integration: Dict) -> Dict[str, str]:
        """Ask about a single optional integration."""
        result = {}
        print()
        print(f"  ── {integration['name']} ──")
        print(f"  {integration['description']}")
        print(f"  Free alternative: {integration['free_alternative']}")
        print()

        if key == "llm":
            if not self._ask_yes_no(f"  Configure LLM API Key?", default=False):
                print(f"  Skipped. (You can add later with --setup)")
                return result

            print()
            print("  Select LLM Provider:")
            print("    1) OpenAI (gpt-4o-mini)")
            print("    2) Anthropic Claude (claude-3-5-haiku-20241022)")
            print("    3) Google Gemini (gemini-2.0-flash)")
            print("    4) DeepSeek (deepseek-chat)")
            print("    5) Groq (llama-3.3-70b-versatile)")
            print("    6) Custom OpenAI-Compatible endpoint")
            print("    7) Skip")
            print()
            choice = self._ask_input("Select LLM Provider (1-7)", default="7", optional=False)
            if choice == "1":
                key_val = self._ask_input("OPENAI_API_KEY", optional=True)
                if key_val:
                    result["OPENAI_API_KEY"] = key_val
                    result["LLM_PROVIDER_NAME"] = "openai"
                    result["LLM_PROVIDER_MODEL"] = "gpt-4o-mini"
            elif choice == "2":
                key_val = self._ask_input("ANTHROPIC_API_KEY", optional=True)
                if key_val:
                    result["ANTHROPIC_API_KEY"] = key_val
                    result["LLM_PROVIDER_NAME"] = "anthropic"
                    result["LLM_PROVIDER_MODEL"] = "claude-3-5-haiku-20241022"
            elif choice == "3":
                key_val = self._ask_input("GOOGLE_API_KEY", optional=True)
                if key_val:
                    result["GOOGLE_API_KEY"] = key_val
                    result["LLM_PROVIDER_NAME"] = "google"
                    result["LLM_PROVIDER_MODEL"] = "gemini-2.0-flash"
            elif choice == "4":
                key_val = self._ask_input("DEEPSEEK_API_KEY", optional=True)
                if key_val:
                    result["DEEPSEEK_API_KEY"] = key_val
                    result["LLM_PROVIDER_NAME"] = "deepseek"
                    result["LLM_PROVIDER_MODEL"] = "deepseek-chat"
            elif choice == "5":
                key_val = self._ask_input("GROQ_API_KEY", optional=True)
                if key_val:
                    result["GROQ_API_KEY"] = key_val
                    result["LLM_PROVIDER_NAME"] = "groq"
                    result["LLM_PROVIDER_MODEL"] = "llama-3.3-70b-versatile"
            elif choice == "6":
                prov_name = self._ask_input("LLM_PROVIDER_NAME (e.g. together, mistral, custom)", optional=True)
                key_val = self._ask_input("LLM_PROVIDER_API_KEY", optional=True)
                base_url = self._ask_input("LLM_PROVIDER_BASE_URL", optional=True)
                model = self._ask_input("LLM_PROVIDER_MODEL", optional=True)
                if key_val:
                    result["LLM_PROVIDER_API_KEY"] = key_val
                    if prov_name: result["LLM_PROVIDER_NAME"] = prov_name
                    if base_url: result["LLM_PROVIDER_BASE_URL"] = base_url
                    if model: result["LLM_PROVIDER_MODEL"] = model
            return result

        if not self._ask_yes_no(f"  Add {integration['name']} key?", default=False):
            print(f"  Skipped. (You can add later with --setup)")
            return result

        for env_var, description in integration["env_vars"].items():
            value = self._ask_input(f"{env_var} — {description}", optional=True)
            if value:
                result[env_var] = value

        return result

    def run_interactive(self) -> Dict[str, Any]:
        """Run the full interactive setup wizard. Returns config dict + env vars."""
        self._print_banner()

        # ─── 1. Basic Setup ─────────────────────────────────
        print()
        print("  ── Basic Configuration ──")
        print()

        # Agent token
        auto_token = f"agent-{secrets.token_hex(16)}"
        token = self._ask_input(
            "Agent token (for authenticating to Agent-OS)",
            default=auto_token,
            optional=False,
        )
        self._answers["agent_token"] = token

        # JWT secret
        jwt_auto = secrets.token_urlsafe(48)
        jwt_secret = self._ask_input(
            "JWT secret key (for session tokens)",
            default=jwt_auto,
            optional=True,
        )
        self._answers["jwt_secret"] = jwt_secret or jwt_auto

        # ─── 2. Optional Integrations ───────────────────────
        print()
        print("=" * 50)
        print("  OPTIONAL INTEGRATIONS")
        print("  All of these are FREE to skip. Agent-OS works")
        print("  perfectly without any of them.")
        print("=" * 50)

        env_vars = {}
        for key, integration in self.OPTIONAL_INTEGRATIONS.items():
            integration_envs = self._ask_integration(key, integration)
            env_vars.update(integration_envs)

        # ─── 3. Browser Preferences ─────────────────────────
        print()
        print("=" * 50)
        print("  BROWSER PREFERENCES")
        print("=" * 50)
        print()

        headless = self._ask_yes_no("Run browser in headless mode?", default=True)
        self._answers["headless"] = headless

        locale = self._ask_input("Locale (default: en-US)", default="en-US")
        self._answers["locale"] = locale

        timezone = self._ask_input("Timezone (default: America/New_York)", default="America/New_York")
        self._answers["timezone"] = timezone

        # ─── 4. Summary ─────────────────────────────────────
        print()
        print("=" * 50)
        print("  SETUP SUMMARY")
        print("=" * 50)
        print()
        print(f"  Agent Token:  {self._answers['agent_token'][:8]}****")
        print(f"  JWT Secret:   {self._answers['jwt_secret'][:8]}****")
        print(f"  Headless:     {self._answers['headless']}")
        print(f"  Locale:       {self._answers.get('locale', 'en-US')}")
        print(f"  Timezone:     {self._answers.get('timezone', 'America/New_York')}")
        print()
        if env_vars:
            print("  Optional Keys Added:")
            for k, v in env_vars.items():
                masked = f"{v[:4]}****{v[-4:]}" if len(v) > 12 else "****"
                print(f"    {k}: {masked}")
        else:
            print("  No optional API keys added (fully self-contained mode)")
        print()

        if not self._ask_yes_no("  Save this configuration?", default=True):
            print("  Setup cancelled. No files written.")
            return {}

        # ─── 5. Save ────────────────────────────────────────
        self._save_config()
        self._save_env(env_vars)

        print()
        print("  Configuration saved!")
        print(f"  Config: {self.config_file}")
        print(f"  Env:    {self.env_file_home}")
        print()
        print("  You can re-run setup anytime with: python main.py --setup")
        print()

        return {"config": self._answers, "env": env_vars}

    def _save_config(self):
        """Save configuration to YAML."""
        import yaml

        config = {
            "server": {
                "agent_token": self._answers["agent_token"],
            },
            "jwt": {
                "secret_key": self._answers["jwt_secret"],
            },
            "browser": {
                "headless": self._answers.get("headless", True),
                "locale": self._answers.get("locale", "en-US"),
                "timezone_id": self._answers.get("timezone", "America/New_York"),
            },
            "security": {
                "captcha_auto_solve": bool(os.environ.get("CAPTCHA_API_KEY")),
                "enable_api_key_auth": True,
                "enable_jwt_auth": True,
                "allow_legacy_token_auth": False,
            },
        }

        with open(self.config_file, "w") as f:
            yaml.dump(config, f, default_flow_style=False)

        logger.info(f"Config saved to {self.config_file}")

    def _save_env(self, env_vars: Dict[str, str]):
        """Save optional environment variables to .env file(s)."""
        lines = [
            "# Agent-OS Environment Variables",
            "# Generated by setup wizard",
            "# All keys are optional — Agent-OS runs without any of these",
            "",
            f"JWT_SECRET_KEY={self._answers['jwt_secret']}",
            f"AGENT_TOKEN={self._answers['agent_token']}",
            "",
        ]

        if env_vars:
            lines.append("# ─── Optional Integration Keys ─────────────")
            for key, value in env_vars.items():
                lines.append(f"{key}={value}")
            lines.append("")

        env_content = "\n".join(lines)

        # Save to home config dir
        with open(self.env_file_home, "w") as f:
            f.write(env_content)
        try:
            os.chmod(self.env_file_home, 0o600)
        except Exception:
            pass

        # Also save to app directory (where main.py auto-loads it)
        try:
            with open(self.env_file_app, "w") as f:
                f.write(env_content)
            try:
                os.chmod(self.env_file_app, 0o600)
            except Exception:
                pass
            logger.info(f"Environment saved to {self.env_file_home} and {self.env_file_app}")
        except Exception as e:
            logger.warning(f"Could not write app .env ({self.env_file_app}): {e}. Home .env saved.")

    def run_non_interactive(self) -> Dict[str, Any]:
        """Auto-setup with sensible defaults (no user input required).
        Used for Docker/CI/automated deployments."""
        auto_token = f"agent-{secrets.token_hex(16)}"
        jwt_secret = secrets.token_urlsafe(48)

        self._answers = {
            "agent_token": auto_token,
            "jwt_secret": jwt_secret,
            "headless": True,
            "locale": "en-US",
            "timezone": "America/New_York",
        }

        self._save_config()
        self._save_env({})

        logger.info("Auto-configured with defaults (no API keys, fully self-contained)")
        return {"config": self._answers, "env": {}}


def run_setup_if_needed(config_path: Optional[str] = None, force: bool = False, non_interactive: bool = False) -> Optional[Dict]:
    """
    Run setup wizard on first launch or when forced.

    Args:
        config_path: Path to config directory
        force: Force setup even if config already exists
        non_interactive: Auto-configure with defaults (no user input)

    Returns:
        Setup result dict, or None if setup was skipped
    """
    wizard = SetupWizard(config_path)

    if not force and not wizard.is_first_launch():
        return None

    try:
        if non_interactive or not sys.stdin.isatty():
            return wizard.run_non_interactive()
        return wizard.run_interactive()
    except (KeyboardInterrupt, EOFError):
        print("\n  Setup interrupted. Running with auto-configured defaults...")
        return wizard.run_non_interactive()
