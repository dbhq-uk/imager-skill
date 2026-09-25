#!/bin/bash
# Set up the Python virtual environment for the imager skill.
#
# This script:
#   1. Creates a .venv/ in this directory
#   2. Installs requirements.txt (PyYAML)
#   3. Reminds the user to set OPENAI_API_KEY
#
# It does NOT prompt for an API key - the script reads OPENAI_API_KEY from the
# environment. Set it in your shell rc file.

set -e

# This script lives in scripts/, so the skill root is one level up. The venv has
# to sit at the skill root because SKILL.md names it as
# ${CLAUDE_SKILL_DIR}/.venv - putting it beside this script instead would work
# here and be wrong everywhere the skill is actually invoked from.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_DIR="$(dirname "$SCRIPT_DIR")"
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

# A venv is NOT RELOCATABLE: every script in bin/ carries a shebang with the
# absolute interpreter path it was built with, so moving or renaming the skill
# directory leaves a venv that exists and cannot be used. That is what the
# 13 Sep 2026 rename from gpt-image-2 to imager did, and it failed with
# "bin/pip: cannot execute: required file not found" - a message naming neither
# the cause nor the fix.
#
# PROBE A SCRIPT, NOT THE INTERPRETER. `bin/python` is a symlink to the system
# python and keeps working after a move, so `bin/python -c ""` succeeds on a
# venv that is entirely unusable - that was the first version of this check and
# it passed while pip was broken. `bin/pip --version` is the thing that
# actually fails, so it is the thing to test.
if [ -d "$VENV" ] && ! "$VENV/bin/pip" --version >/dev/null 2>&1; then
    warn "The venv at $VENV cannot run - most likely the skill directory moved"
    info "Rebuilding it. Nothing is lost: a venv is derived, not data"
    rm -rf "$VENV"
fi

if [ ! -d "$VENV" ]; then
    info "Creating venv at $VENV"
    python3 -m venv "$VENV"
fi

info "Installing dependencies"
"$VENV/bin/pip" install --upgrade pip -q
"$VENV/bin/pip" install -r "$SKILL_DIR/requirements.txt" -q
ok "Dependencies installed"

chmod +x "$SKILL_DIR/scripts/imager.py"

if [ -n "$OPENAI_API_KEY" ]; then
    ok "OPENAI_API_KEY is set"
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
echo "    $VENV/bin/python $SKILL_DIR/scripts/imager.py init"
