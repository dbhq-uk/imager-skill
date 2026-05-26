#!/bin/bash
# Set up the Python virtual environment for the gpt-image-2 skill.
#
# This script:
#   1. Creates a .venv/ in this directory
#   2. Installs requirements.txt (PyYAML)
#   3. Reminds the user to set OPENAI_API_KEY
#
# It does NOT prompt for an API key — the script reads OPENAI_API_KEY (or
# OPENROUTER_API_KEY) from the environment. Set it in your shell rc file.

set -e

SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$SKILL_DIR/.venv"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

info() { echo -e "${BLUE}==>${NC} $*"; }
ok()   { echo -e "${GREEN}✓${NC} $*"; }
warn() { echo -e "${YELLOW}!${NC} $*"; }

if ! command -v python3 &>/dev/null; then
    echo "Error: python3 not found" >&2
    exit 1
fi

if [ ! -d "$VENV" ]; then
    info "Creating venv at $VENV"
    python3 -m venv "$VENV"
fi

info "Installing dependencies"
"$VENV/bin/pip" install --upgrade pip -q
"$VENV/bin/pip" install -r "$SKILL_DIR/requirements.txt" -q
ok "Dependencies installed"

chmod +x "$SKILL_DIR/scripts/gpt_image_2.py"

if [ -n "$OPENAI_API_KEY" ]; then
    ok "OPENAI_API_KEY is set"
elif [ -n "$OPENROUTER_API_KEY" ]; then
    ok "OPENROUTER_API_KEY is set (use --provider openrouter)"
else
    warn "No API key found in environment"
    echo "    Set one in your shell rc file:"
    echo "      export OPENAI_API_KEY=sk-..."
fi

if ! command -v magick &>/dev/null; then
    warn "ImageMagick not found (optional)"
    echo "    Needed for platform resizing and carousel contact sheets."
    echo "    macOS: brew install imagemagick"
    echo "    Linux: sudo apt install imagemagick"
fi

echo
ok "Setup complete. Run the onboarding wizard:"
echo "    $VENV/bin/python $SKILL_DIR/scripts/gpt_image_2.py init"
