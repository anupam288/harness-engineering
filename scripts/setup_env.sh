#!/usr/bin/env bash
# scripts/setup_env.sh
# First-run setup for the SDLC harness.
# Run once after cloning: bash scripts/setup_env.sh
#
# What it does:
#   1. Checks Python version (3.11+ required)
#   2. Installs dependencies (pip install -e ".[dev]")
#   3. Copies .env.example → .env if .env doesn't exist
#   4. Checks required environment variables
#   5. Validates harness config files
#   6. Installs pre-commit hooks
#   7. Runs the test suite to confirm everything works

set -euo pipefail

# ── Colours ───────────────────────────────────────────────────────────────────
RED='\033[0;31m'; YELLOW='\033[0;33m'; GREEN='\033[0;32m'; RESET='\033[0m'
ok()   { echo -e "  ${GREEN}✓${RESET} $*"; }
warn() { echo -e "  ${YELLOW}⚠${RESET} $*"; }
fail() { echo -e "  ${RED}✗${RESET} $*"; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

echo ""
echo "=== SDLC Harness — First-run setup ==="
echo ""

# ── 1. Python version ─────────────────────────────────────────────────────────
echo "Checking Python version..."
PYTHON_BIN="${PYTHON:-python3}"

if ! command -v "$PYTHON_BIN" &>/dev/null; then
    fail "Python not found. Install Python 3.11+ and try again."
    exit 1
fi

PYTHON_VERSION=$("$PYTHON_BIN" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
PYTHON_MAJOR=$("$PYTHON_BIN" -c "import sys; print(sys.version_info.major)")
PYTHON_MINOR=$("$PYTHON_BIN" -c "import sys; print(sys.version_info.minor)")

if [ "$PYTHON_MAJOR" -lt 3 ] || { [ "$PYTHON_MAJOR" -eq 3 ] && [ "$PYTHON_MINOR" -lt 11 ]; }; then
    fail "Python $PYTHON_VERSION found, but 3.11+ is required."
    exit 1
fi
ok "Python $PYTHON_VERSION"

# ── 2. Install dependencies ───────────────────────────────────────────────────
echo ""
echo "Installing dependencies..."

if [ -f "pyproject.toml" ]; then
    "$PYTHON_BIN" -m pip install -e ".[dev]" --quiet
    ok "Installed package + dev dependencies (from pyproject.toml)"
elif [ -f "requirements.txt" ]; then
    "$PYTHON_BIN" -m pip install -r requirements.txt --quiet
    ok "Installed dependencies (from requirements.txt)"
else
    fail "Neither pyproject.toml nor requirements.txt found."
    exit 1
fi

# Install python-dotenv if not present
"$PYTHON_BIN" -m pip install python-dotenv --quiet 2>/dev/null || true

# ── 3. Create .env from template ─────────────────────────────────────────────
echo ""
echo "Setting up environment..."

if [ ! -f ".env" ]; then
    if [ -f ".env.example" ]; then
        cp .env.example .env
        ok "Created .env from .env.example"
        warn "Edit .env and fill in your API keys before running agents"
    else
        warn ".env.example not found — skipping .env creation"
    fi
else
    ok ".env already exists"
fi

# ── 4. Check required environment variables ───────────────────────────────────
echo ""
echo "Checking environment variables..."

# Load .env if it exists
if [ -f ".env" ]; then
    set -a
    source .env 2>/dev/null || true
    set +a
fi

MISSING_REQUIRED=()
MISSING_OPTIONAL=()

# Required for basic operation
if [ -z "${ANTHROPIC_API_KEY:-}" ]; then
    MISSING_REQUIRED+=("ANTHROPIC_API_KEY")
fi

# Optional — only warn
for optional_var in HARNESS_LOG_SIGNING_KEY LOKI_API_KEY DD_API_KEY OPENAI_API_KEY; do
    if [ -z "${!optional_var:-}" ]; then
        MISSING_OPTIONAL+=("$optional_var")
    fi
done

if [ ${#MISSING_REQUIRED[@]} -gt 0 ]; then
    for var in "${MISSING_REQUIRED[@]}"; do
        fail "Missing required: $var (set in .env)"
    done
    echo ""
    warn "Set the missing variables in .env and re-run this script."
    echo "  Continuing setup — you'll need these before running agents."
else
    ok "ANTHROPIC_API_KEY is set"
fi

if [ ${#MISSING_OPTIONAL[@]} -gt 0 ]; then
    for var in "${MISSING_OPTIONAL[@]}"; do
        warn "Optional not set: $var"
    done
fi

# ── 5. Validate config files ──────────────────────────────────────────────────
echo ""
echo "Validating harness config..."

"$PYTHON_BIN" - <<'PYEOF'
import sys, yaml
from pathlib import Path

checks = [
    ("harness_config.yaml", lambda d: "llm_model" in d),
    ("model_config.yaml",   lambda d: "default" in d),
    ("policies/policy.yaml", lambda d: "rules" in d),
    ("monitoring_rules.yaml", lambda d: "rules" in d),
    ("observability_config.yaml", lambda d: "budgets" in d),
    ("security_config.yaml", lambda d: "sanitiser" in d),
]

all_ok = True
for filename, validator in checks:
    p = Path(filename)
    if not p.exists():
        print(f"  ⚠  {filename} not found — skipping")
        continue
    try:
        data = yaml.safe_load(p.read_text()) or {}
        if validator(data):
            print(f"  ✓  {filename}")
        else:
            print(f"  ✗  {filename} — unexpected structure")
            all_ok = False
    except Exception as e:
        print(f"  ✗  {filename} — parse error: {e}")
        all_ok = False

sys.exit(0 if all_ok else 1)
PYEOF

# ── 6. Check gate status ──────────────────────────────────────────────────────
echo ""
echo "Checking phase gates..."
"$PYTHON_BIN" cli.py gate --all 2>/dev/null || true

# ── 7. Pre-commit hooks ───────────────────────────────────────────────────────
echo ""
echo "Installing pre-commit hooks..."

if command -v pre-commit &>/dev/null; then
    pre-commit install --quiet
    ok "Pre-commit hooks installed"
else
    warn "pre-commit not found — skipping hook installation"
    warn "Install with: pip install pre-commit && pre-commit install"
fi

# ── 8. Run tests ──────────────────────────────────────────────────────────────
echo ""
echo "Running test suite..."
if "$PYTHON_BIN" -m pytest tests/ -q --tb=short 2>&1 | tail -3; then
    ok "All tests passed"
else
    fail "Some tests failed — check output above"
    exit 1
fi

# ── Done ──────────────────────────────────────────────────────────────────────
echo ""
echo "=== Setup complete ==="
echo ""
echo "Next steps:"
echo "  1. Edit .env with your API keys (if not done already)"
echo "  2. Edit harness_config.yaml and model_config.yaml for your project"
echo "  3. Check gate status:  python cli.py gate --all"
echo "  4. Run first phase:    python cli.py run requirements --input inputs/project.json"
echo "  5. View status:        python cli.py status"
echo ""
