#!/usr/bin/env bash
# Agent-OS Setup Script
# Installs dependencies, Playwright, and verifies everything works.
set -e

# ─── Flags ────────────────────────────────────────────────
NO_SUDO=false
for arg in "$@"; do
    case $arg in
        --no-sudo) NO_SUDO=true ;;
        --help|-h)
            echo "Usage: ./setup.sh [--no-sudo]"
            echo "  --no-sudo   Skip sudo-dependent steps (system packages)"
            exit 0
            ;;
    esac
done

# ─── Sudo Helper ──────────────────────────────────────────
run_privileged() {
    if $NO_SUDO; then
        echo "⚠️  --no-sudo flag set. Skipping: $*"
        return 0
    fi
    if command -v sudo > /dev/null 2>&1; then
        sudo "$@"
    else
        echo "⚠️  sudo not found. Trying without sudo..."
        "$@"
    fi
}

echo "🤖 Agent-OS Setup"
echo "================="
echo ""

# ─── Python Check ─────────────────────────────────────────────
PYTHON_VERSION=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
PYTHON_MAJOR=$(echo $PYTHON_VERSION | cut -d. -f1)
PYTHON_MINOR=$(echo $PYTHON_VERSION | cut -d. -f2)

if [ "$PYTHON_MAJOR" -lt 3 ] || ([ "$PYTHON_MAJOR" -eq 3 ] && [ "$PYTHON_MINOR" -lt 10 ]); then
    echo "❌ Python 3.10+ required (found $PYTHON_VERSION)"
    exit 1
fi
echo "✅ Python $PYTHON_VERSION"

# ─── Virtual Environment (required) ──────────────────────────
VENV_DIR="$(dirname "$0")/venv"

if [ ! -d "$VENV_DIR" ]; then
    echo "📦 Creating virtual environment..."

    # Check if python3-venv is available
    if ! python3 -m venv --help > /dev/null 2>&1; then
        echo "⚠️  python3-venv not found. Attempting to install..."
        PYTHON_VER=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
        if command -v apt-get > /dev/null 2>&1; then
            run_privileged apt-get update -qq && run_privileged apt-get install -y -qq "python${PYTHON_VER}-venv" python3-pip python3-dev build-essential
        elif command -v dnf > /dev/null 2>&1; then
            run_privileged dnf install -y -q python3-venv python3-pip python3-devel gcc
        elif command -v yum > /dev/null 2>&1; then
            run_privileged yum install -y -q python3-venv python3-pip python3-devel gcc
        else
            echo "❌ Cannot install python3-venv. Install it manually and re-run."
            exit 1
        fi
    fi

    # Also ensure pip and build tools exist
    if ! python3 -m pip --version > /dev/null 2>&1; then
        if command -v apt-get > /dev/null 2>&1; then
            run_privileged apt-get install -y -qq python3-pip 2>/dev/null || true
        fi
    fi

    # Try normal venv first, fall back to --without-pip + manual pip install
    python3 -m venv "$VENV_DIR" 2>/dev/null || {
        echo "  ⚠️  ensurepip not available, using --without-pip fallback..."
        python3 -m venv --without-pip "$VENV_DIR"
        source "$VENV_DIR/bin/activate"
        curl -sS https://bootstrap.pypa.io/get-pip.py -o /tmp/get-pip.py
        python /tmp/get-pip.py -q
        rm -f /tmp/get-pip.py
    }
    echo "✅ Virtual environment created"
else
    echo "✅ Virtual environment already exists"
fi

source "$VENV_DIR/bin/activate"
PIP_CMD="pip"
PYTHON_CMD="python"
echo "✅ Using virtual environment: $(which python)"

# ─── Environment File ─────────────────────────────────────
ENV_FILE="$(dirname "$0")/.env"
ENV_EXAMPLE="$(dirname "$0")/.env.example"
if [ ! -f "$ENV_FILE" ] && [ -f "$ENV_EXAMPLE" ]; then
    echo "📄 Creating .env from .env.example..."
    cp "$ENV_EXAMPLE" "$ENV_FILE"
    # Auto-generate JWT_SECRET_KEY if not set
    if ! grep -q "^JWT_SECRET_KEY=.\+" "$ENV_FILE" 2>/dev/null; then
        GENERATED_KEY=$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')
        if command -v sed > /dev/null 2>&1; then
            sed -i "s|^JWT_SECRET_KEY=.*|JWT_SECRET_KEY=${GENERATED_KEY}|" "$ENV_FILE"
        fi
        echo "✅ Generated JWT_SECRET_KEY"
    fi
    echo "✅ .env created (edit it to customize settings)"
elif [ -f "$ENV_FILE" ]; then
    echo "✅ .env already exists"
fi

# ─── System Dependencies (for Playwright Chromium) ───────────
echo ""
echo "🔍 Checking system dependencies for Chromium..."

MISSING_DEPS=""
# Check both regular and t64 variants (Debian 12+ renames)
for lib in libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 libxrandr2 libgbm1 libpango-1.0-0 libcairo2 libasound2; do
    if ! dpkg -l "$lib" 2>/dev/null | grep -q "^ii" && ! dpkg -l "${lib}t64" 2>/dev/null | grep -q "^ii"; then
        MISSING_DEPS="$MISSING_DEPS $lib"
    fi
done

if [ -n "$MISSING_DEPS" ]; then
    echo "⚠️  Missing system libraries:$MISSING_DEPS"
    echo "   Attempting to install..."
    if command -v apt-get > /dev/null 2>&1; then
        run_privileged apt-get update -qq 2>/dev/null || true
        run_privileged apt-get install -y -qq $MISSING_DEPS 2>/dev/null || {
            echo "⚠️  Could not install some system deps. Chromium may fail to launch."
            echo "   Try manually: sudo apt install$MISSING_DEPS"
        }
    fi
else
    echo "✅ All system dependencies present"
fi

# ─── Python Dependencies ─────────────────────────────────────
REQUIREMENTS_FILE="$(dirname "$0")/requirements.lock"
if [ ! -f "$REQUIREMENTS_FILE" ]; then
    REQUIREMENTS_FILE="$(dirname "$0")/requirements.txt"
fi
echo ""
echo "📦 Installing Python dependencies from $(basename $REQUIREMENTS_FILE)..."
pip install --upgrade pip -q 2>&1 | tail -1
pip install -r "$REQUIREMENTS_FILE" --no-cache-dir 2>&1 | tail -5 || {
    echo "⚠️  Some packages failed. Retrying..."
    pip install -r "$REQUIREMENTS_FILE" --no-build-isolation 2>&1 | tail -5 || true
}
echo "✅ Python dependencies installed"

# ─── Patchright Chromium ─────────────────────────────────────
echo ""
echo "🌐 Installing Patchright Chromium..."
$PYTHON_CMD -m patchright install chromium 2>&1 | tail -1
$PYTHON_CMD -c "from patchright.async_api import async_playwright; print('Patchright OK')"
echo "✅ Patchright browser installed"

# ─── Verify Installation ─────────────────────────────────────
echo ""
echo "🔍 Verifying installation..."

$PYTHON_CMD -c "
import sys
try:
    import crypt
except ImportError:
    import types
    sys.modules['crypt'] = types.ModuleType('crypt')
errors = []

# Module name -> actual import name
modules = {
    'patchright': 'patchright',
    'websockets': 'websockets',
    'aiohttp': 'aiohttp',
    'httpx': 'httpx',
    'cryptography': 'cryptography',
    'beautifulsoup4': 'bs4',
    'lxml': 'lxml',
    'PyYAML': 'yaml',
    'psutil': 'psutil',
    'numpy': 'numpy',
    'mcp': 'mcp',
    'curl_cffi': 'curl_cffi',
    'cloudscraper': 'cloudscraper',
    'redis': 'redis',
    'sqlalchemy': 'sqlalchemy',
    'pydantic': 'pydantic',
    'structlog': 'structlog',
    'bcrypt': 'bcrypt',
    'passlib': 'passlib',
    'requests': 'requests',
    'aiofiles': 'aiofiles',
}

for pkg_name, import_name in modules.items():
    try:
        __import__(import_name)
    except ImportError:
        errors.append(f'  ❌ {pkg_name} (import as {import_name})')

if errors:
    print('❌ Import failures:')
    for e in errors:
        print(e)
    sys.exit(1)
print('✅ All imports successful')
"

# Verify Patchright browser
$PYTHON_CMD -c "
from patchright.sync_api import sync_playwright
p = sync_playwright().start()
browser = p.chromium.launch(headless=True)
version = browser.version
browser.close()
p.stop()
print(f'✅ Patchright Chromium launches correctly ({version})')
"

# Run tests
echo ""
echo "🧪 Running test suite..."
$PYTHON_CMD -m pytest "$(dirname "$0")/tests/" -v --tb=short 2>&1 || {
    echo ""
    echo "⚠️  Some tests failed. Check output above."
}

echo ""
echo "🎉 Setup complete!"
echo ""
echo "Quick start:"
echo "  source venv/bin/activate"
echo "  python main.py --agent-token 'my-agent'"
echo ""
echo "Test with curl:"
echo "  curl -X POST http://localhost:8001/command \\"
echo "    -H 'Content-Type: application/json' \\"
echo "    -d '{\"token\":\"my-agent\",\"command\":\"navigate\",\"url\":\"https://example.com\"}'"
echo ""
