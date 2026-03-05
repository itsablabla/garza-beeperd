#!/usr/bin/env bash
# garza-beeperd — One-Line Installer v1.1
# Usage: curl -fsSL https://raw.githubusercontent.com/itsablabla/garza-beeperd/main/install.sh | bash
#
# Supports: macOS (Intel + Apple Silicon), Linux (x86_64, arm64)
# Handles:  Homebrew Python, system Python, venv isolation, PATH setup
#           LaunchAgent (Mac) or systemd (Linux), Tailscale detection

set -euo pipefail

REPO="itsablabla/garza-beeperd"
RAW="https://raw.githubusercontent.com/${REPO}/main"
INSTALL_DIR="${HOME}/.garza/beeperd"
VENV_DIR="${INSTALL_DIR}/venv"
SCRIPT="${INSTALL_DIR}/beeperd.py"
BIN_DIR="${HOME}/.local/bin"
BIN="${BIN_DIR}/beeperd"

# ── Colors ────────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; BOLD='\033[1m'; RESET='\033[0m'

info()    { echo -e "${BLUE}[garza-beeperd]${RESET} $*"; }
success() { echo -e "${GREEN}[garza-beeperd]${RESET} ✅ $*"; }
warn()    { echo -e "${YELLOW}[garza-beeperd]${RESET} ⚠️  $*"; }
error()   { echo -e "${RED}[garza-beeperd]${RESET} ❌ $*"; exit 1; }

echo ""
echo -e "${BOLD}╔══════════════════════════════════════════════════════╗${RESET}"
echo -e "${BOLD}║       garza-beeperd — Instant Beeper Relay           ║${RESET}"
echo -e "${BOLD}║       Tailscale-aware mesh • Auto-updating            ║${RESET}"
echo -e "${BOLD}╚══════════════════════════════════════════════════════╝${RESET}"
echo ""

# ── OS detection ──────────────────────────────────────────────────────────────
OS="$(uname -s)"
ARCH="$(uname -m)"
info "Detected: ${OS} ${ARCH}"

# ── Tailscale detection (informational) ───────────────────────────────────────
if command -v tailscale &>/dev/null; then
    TS_IP=$(tailscale ip -4 2>/dev/null || echo "")
    if [ -n "$TS_IP" ]; then
        success "Tailscale detected: ${TS_IP} — mesh networking enabled"
    else
        info "Tailscale installed but not connected"
    fi
else
    info "Tailscale not found — install at https://tailscale.com/download for mesh networking"
fi

# ── Step 1: Create directories FIRST (before anything else) ──────────────────
info "Creating install directory: ${INSTALL_DIR}"
mkdir -p "${INSTALL_DIR}"
mkdir -p "${BIN_DIR}"

# ── Step 2: Find Python 3.8+ ─────────────────────────────────────────────────
PYTHON=""

# Prefer specific versions first, then fallback
for py in python3.13 python3.12 python3.11 python3.10 python3.9 python3.8 python3 python; do
    if command -v "$py" &>/dev/null; then
        VER=$("$py" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>/dev/null || echo "0.0")
        MAJOR=$(echo "$VER" | cut -d. -f1)
        MINOR=$(echo "$VER" | cut -d. -f2)
        if [ "$MAJOR" -ge 3 ] && [ "$MINOR" -ge 8 ]; then
            PYTHON="$py"
            success "Python ${VER} found at $(command -v $py)"
            break
        fi
    fi
done

if [ -z "$PYTHON" ]; then
    warn "Python 3.8+ not found. Attempting to install..."
    if [ "$OS" = "Darwin" ]; then
        if command -v brew &>/dev/null; then
            brew install python3
            PYTHON="python3"
        else
            error "Homebrew not found. Install Python from https://python.org then re-run."
        fi
    elif [ "$OS" = "Linux" ]; then
        if command -v apt-get &>/dev/null; then
            sudo apt-get update -qq && sudo apt-get install -y python3 python3-pip python3-venv
            PYTHON="python3"
        elif command -v yum &>/dev/null; then
            sudo yum install -y python3 python3-pip
            PYTHON="python3"
        else
            error "Cannot install Python automatically. Install Python 3.8+ and re-run."
        fi
    else
        error "Unsupported OS: ${OS}"
    fi
fi

# ── Step 3: Create venv (AFTER mkdir, BEFORE pip) ────────────────────────────
if [ ! -d "${VENV_DIR}" ]; then
    info "Creating Python virtual environment..."
    "$PYTHON" -m venv "${VENV_DIR}" || {
        # Some systems need python3-venv package
        if [ "$OS" = "Linux" ] && command -v apt-get &>/dev/null; then
            warn "venv failed — installing python3-venv..."
            sudo apt-get install -y python3-venv
            "$PYTHON" -m venv "${VENV_DIR}"
        else
            error "Failed to create venv. Try: ${PYTHON} -m venv ${VENV_DIR}"
        fi
    }
    success "Virtual environment created"
else
    info "Using existing virtual environment"
fi

# Use venv's python and pip exclusively
VENV_PYTHON="${VENV_DIR}/bin/python"
VENV_PIP="${VENV_DIR}/bin/pip"

# ── Step 4: Install Python dependencies into venv ────────────────────────────
info "Installing Python dependencies (websocket-client, requests)..."
"$VENV_PIP" install --quiet --upgrade pip
"$VENV_PIP" install --quiet websocket-client requests
success "Dependencies installed"

# ── Step 5: Download daemon script ───────────────────────────────────────────
info "Downloading garza-beeperd daemon..."
if command -v curl &>/dev/null; then
    curl -fsSL "${RAW}/beeperd.py" -o "${SCRIPT}"
elif command -v wget &>/dev/null; then
    wget -qO "${SCRIPT}" "${RAW}/beeperd.py"
else
    error "Neither curl nor wget found. Install one and re-run."
fi
chmod +x "${SCRIPT}"
success "Daemon downloaded to ${SCRIPT}"

# ── Step 6: Create beeperd wrapper script ────────────────────────────────────
cat > "${BIN}" << WRAPPER_EOF
#!/usr/bin/env bash
exec "${VENV_PYTHON}" "${SCRIPT}" "\$@"
WRAPPER_EOF
chmod +x "${BIN}"

# ── Step 7: Add ~/.local/bin to PATH ─────────────────────────────────────────
PATH_LINE='export PATH="$HOME/.local/bin:$PATH"'
PATH_COMMENT="# garza-beeperd"

# Detect shell config file
SHELL_RC=""
if [ -n "${ZSH_VERSION:-}" ] || [ "$(basename "${SHELL:-}")" = "zsh" ]; then
    SHELL_RC="${HOME}/.zshrc"
elif [ -n "${BASH_VERSION:-}" ] || [ "$(basename "${SHELL:-}")" = "bash" ]; then
    if [ "$OS" = "Darwin" ]; then
        SHELL_RC="${HOME}/.bash_profile"
    else
        SHELL_RC="${HOME}/.bashrc"
    fi
fi

# Fallback: check which files exist
if [ -z "$SHELL_RC" ]; then
    for rc in "${HOME}/.zshrc" "${HOME}/.bash_profile" "${HOME}/.bashrc"; do
        if [ -f "$rc" ]; then
            SHELL_RC="$rc"
            break
        fi
    done
fi

if [ -n "$SHELL_RC" ]; then
    if ! grep -q "${BIN_DIR}" "$SHELL_RC" 2>/dev/null; then
        echo "" >> "$SHELL_RC"
        echo "${PATH_COMMENT}" >> "$SHELL_RC"
        echo "${PATH_LINE}" >> "$SHELL_RC"
        info "Added ~/.local/bin to PATH in ${SHELL_RC}"
    fi
fi

# Make available in current session immediately
export PATH="${BIN_DIR}:${PATH}"
success "beeperd command installed at ${BIN}"

# ── Done ──────────────────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}${BOLD}╔══════════════════════════════════════════════════════╗${RESET}"
echo -e "${GREEN}${BOLD}║   ✅  garza-beeperd installed successfully!          ║${RESET}"
echo -e "${GREEN}${BOLD}╚══════════════════════════════════════════════════════╝${RESET}"
echo ""
echo -e "  ${BOLD}Next step — run setup wizard:${RESET}"
echo -e "  ${BLUE}beeperd setup${RESET}"
echo ""
echo -e "  ${BOLD}Or if you already have a token:${RESET}"
echo -e "  ${BLUE}BEEPER_TOKEN=your_token beeperd run${RESET}"
echo ""
echo -e "  ${BOLD}Commands:${RESET}"
echo -e "  ${BLUE}beeperd status${RESET}    — check daemon health"
echo -e "  ${BLUE}beeperd mesh${RESET}      — show mesh peer status"
echo -e "  ${BLUE}beeperd logs${RESET}      — tail live logs"
echo -e "  ${BLUE}beeperd update${RESET}    — force update now"
echo -e "  ${BLUE}beeperd stop${RESET}      — stop daemon"
echo ""

# ── Auto-run setup if interactive terminal ────────────────────────────────────
if [ -t 0 ] && [ -t 1 ]; then
    echo -e "${YELLOW}Run setup wizard now? (Y/n):${RESET} \c"
    read -r ANSWER </dev/tty
    if [ "${ANSWER:-y}" != "n" ] && [ "${ANSWER:-y}" != "N" ]; then
        echo ""
        "${VENV_PYTHON}" "${SCRIPT}" setup
    fi
fi
