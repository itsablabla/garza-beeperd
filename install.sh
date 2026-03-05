#!/usr/bin/env bash
# garza-beeperd — One-Line Installer
# Usage: curl -fsSL https://raw.githubusercontent.com/itsablabla/garza-beeperd/main/install.sh | bash
#
# Supports: macOS (Intel + Apple Silicon), Linux (x86_64, arm64)
# Auto-installs: Python deps, daemon script, LaunchAgent (Mac) or systemd (Linux)

set -euo pipefail

REPO="itsablabla/garza-beeperd"
RAW="https://raw.githubusercontent.com/${REPO}/main"
INSTALL_DIR="${HOME}/.garza/beeperd"
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
echo -e "${BOLD}╚══════════════════════════════════════════════════════╝${RESET}"
echo ""

# ── OS detection ──────────────────────────────────────────────────────────────
OS="$(uname -s)"
ARCH="$(uname -m)"
info "Detected: ${OS} ${ARCH}"

# ── Python check ──────────────────────────────────────────────────────────────
PYTHON=""
for py in python3.11 python3.10 python3.9 python3 python; do
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
            error "Homebrew not found. Install Python from https://python.org then re-run this installer."
        fi
    elif [ "$OS" = "Linux" ]; then
        if command -v apt-get &>/dev/null; then
            sudo apt-get update -qq && sudo apt-get install -y python3 python3-pip
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

# ── pip check ─────────────────────────────────────────────────────────────────
if ! "$PYTHON" -m pip --version &>/dev/null; then
    warn "pip not found, installing..."
    curl -fsSL https://bootstrap.pypa.io/get-pip.py | "$PYTHON"
fi

# ── Install Python dependencies ───────────────────────────────────────────────
info "Installing Python dependencies..."
"$PYTHON" -m pip install --quiet --upgrade websocket-client requests
success "Dependencies installed"

# ── Create install directory ──────────────────────────────────────────────────
mkdir -p "${INSTALL_DIR}"
mkdir -p "${BIN_DIR}"

# ── Download daemon script ────────────────────────────────────────────────────
info "Downloading garza-beeperd..."
if command -v curl &>/dev/null; then
    curl -fsSL "${RAW}/beeperd.py" -o "${SCRIPT}"
elif command -v wget &>/dev/null; then
    wget -qO "${SCRIPT}" "${RAW}/beeperd.py"
else
    error "Neither curl nor wget found. Install one and re-run."
fi
chmod +x "${SCRIPT}"
success "Downloaded to ${SCRIPT}"

# ── Create beeperd wrapper in PATH ────────────────────────────────────────────
cat > "${BIN}" << EOF
#!/usr/bin/env bash
exec "${PYTHON}" "${SCRIPT}" "\$@"
EOF
chmod +x "${BIN}"

# Add ~/.local/bin to PATH if not already there
SHELL_RC=""
if [ -f "${HOME}/.zshrc" ]; then
    SHELL_RC="${HOME}/.zshrc"
elif [ -f "${HOME}/.bashrc" ]; then
    SHELL_RC="${HOME}/.bashrc"
elif [ -f "${HOME}/.bash_profile" ]; then
    SHELL_RC="${HOME}/.bash_profile"
fi

if [ -n "$SHELL_RC" ]; then
    if ! grep -q "${BIN_DIR}" "$SHELL_RC" 2>/dev/null; then
        echo "" >> "$SHELL_RC"
        echo "# garza-beeperd" >> "$SHELL_RC"
        echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$SHELL_RC"
        info "Added ~/.local/bin to PATH in ${SHELL_RC}"
    fi
fi

# Make available in current session
export PATH="${BIN_DIR}:${PATH}"

success "beeperd command installed at ${BIN}"

# ── Done ──────────────────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}${BOLD}Installation complete!${RESET}"
echo ""
echo -e "  ${BOLD}Run setup wizard:${RESET}"
echo -e "  ${BLUE}beeperd setup${RESET}"
echo ""
echo -e "  ${BOLD}Or if you already have a token:${RESET}"
echo -e "  ${BLUE}BEEPER_TOKEN=your_token beeperd run${RESET}"
echo ""
echo -e "  ${BOLD}Check status anytime:${RESET}"
echo -e "  ${BLUE}beeperd status${RESET}"
echo ""
echo -e "  ${BOLD}View logs:${RESET}"
echo -e "  ${BLUE}beeperd logs${RESET}"
echo ""

# ── Auto-run setup if interactive ─────────────────────────────────────────────
if [ -t 0 ]; then
    echo -e "${YELLOW}Run setup wizard now? (Y/n):${RESET} \c"
    read -r ANSWER
    if [ "${ANSWER:-y}" != "n" ] && [ "${ANSWER:-y}" != "N" ]; then
        echo ""
        "${PYTHON}" "${SCRIPT}" setup
    fi
fi
