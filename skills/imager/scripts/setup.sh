#!/bin/bash
# Check that this machine can run the imager skill.
#
# There is nothing to install. The CLI uses the Python standard library only,
# so it runs with the system python3 straight from a copied skill directory -
# which is all /plugin install and `npx skills add` ever do. This script checks
# what the CLI cannot check for itself, and changes nothing:
#   1. python3 is present and 3.9 or newer
#   2. OPENAI_API_KEY is set (reminder only - it is never stored)
#   3. ImageMagick is present (optional)
#
# Earlier versions built a .venv here for PyYAML. A .venv left behind by one is
# no longer used and can be deleted.

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_DIR="$(dirname "$SCRIPT_DIR")"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

ok()   { echo -e "${GREEN}✓${NC} $*"; }
warn() { echo -e "${YELLOW}!${NC} $*"; }

if ! command -v python3 &>/dev/null; then
    echo "Error: python3 not found" >&2
    exit 1
fi
if ! python3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)"; then
    echo "Error: Python 3.9+ required, found $(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')" >&2
    exit 1
fi
ok "python3 $(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])') - no packages needed"

chmod +x "$SKILL_DIR/scripts/imager.py"

if [ -n "$OPENAI_API_KEY" ]; then
    ok "OPENAI_API_KEY is set"
else
    warn "No API key found in environment"
    echo "    Set one in your shell rc file:"
    echo "      export OPENAI_API_KEY=sk-..."
fi

# Version 7 is one `magick` binary; version 6 (what apt installs on Ubuntu
# 24.04) is `convert` and `montage`. The CLI uses whichever is there.
if command -v magick &>/dev/null; then
    ok "ImageMagick 7 found"
elif command -v convert &>/dev/null && command -v montage &>/dev/null; then
    ok "ImageMagick 6 found"
else
    warn "ImageMagick not found (optional)"
    echo "    Needed for platform resizing and carousel contact sheets. Version 6 or 7."
    echo "    macOS: brew install imagemagick"
    echo "    Linux: sudo apt install imagemagick"
fi

echo
ok "Ready. Run the onboarding wizard:"
echo "    python3 $SKILL_DIR/scripts/imager.py init"
