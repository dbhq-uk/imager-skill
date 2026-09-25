#!/bin/bash
# Install the imager skill into ~/.claude/skills/ as a live symlink install.
#
# SKILL.md references scripts via ${CLAUDE_SKILL_DIR}, which Claude Code
# substitutes to the skill's own directory for personal, project, and plugin
# installs alike. So this script symlinks the whole skill directory into
# ~/.claude/skills/ - every edit (scripts AND SKILL.md) is immediately live,
# with no per-file rewrite. Re-run only when you add a new skill directory.
#
# Nothing is installed besides the link. The CLI uses the Python standard
# library only, so the system python3 runs it under every install shape.

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILLS_ROOT="$HOME/.claude/skills"

echo "=== imager skill installer (Claude Code) ==="
echo

# --- Dependencies ---
# 3.9 is the floor: the CLI carries `from __future__ import annotations`, so its
# PEP 604 hints are safe below 3.10. It has no other dependency. ImageMagick is
# checked by setup.sh rather than here, because it is
# optional - it is needed for platform resizing and carousel contact sheets, and
# for nothing else.
if ! command -v python3 >/dev/null 2>&1; then
  echo "Missing required dependency: python3"
  echo "  macOS:  brew install python"
  echo "  Ubuntu: sudo apt install python3"
  exit 1
fi
if ! python3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)"; then
  echo "Error: Python 3.9+ required, found $(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
  exit 1
fi
echo "Dependencies OK."
echo

# --- Install each skill in this repo as a full-directory symlink ---
mkdir -p "$SKILLS_ROOT"
for src in "$SCRIPT_DIR"/skills/*/; do
  src="${src%/}"
  name="$(basename "$src")"
  target="$SKILLS_ROOT/$name"
  echo "Installing '$name' -> $target"
  rm -rf "$target"            # replace any prior copy or partial-symlink install
  ln -sfn "$src" "$target"    # whole-directory symlink; ${CLAUDE_SKILL_DIR} resolves it
  chmod +x "$src"/scripts/*.sh 2>/dev/null || true
done

echo
echo "Installed as directory symlinks - all edits (scripts and SKILL.md) are live. Re-run only when adding a new skill."
echo

# --- Check the machine ---
# setup.sh installs nothing. It checks python3, reminds you about the API key
# (read from the environment, never stored) and looks for ImageMagick.
"$SKILLS_ROOT/imager/scripts/setup.sh"

echo
echo "Done. Try: 'generate an editorial image of a rocket'"
