#!/bin/bash
# Install the imager skill into ~/.claude/skills/ as a live symlink install.
#
# SKILL.md references scripts via ${CLAUDE_SKILL_DIR}, which Claude Code
# substitutes to the skill's own directory for personal, project, and plugin
# installs alike. So this script symlinks the whole skill directory into
# ~/.claude/skills/ - every edit (scripts AND SKILL.md) is immediately live,
# with no per-file rewrite. Re-run only when you add a new skill directory.
#
# The venv is deliberately built INSIDE the skill directory rather than in a
# shared location: ${CLAUDE_SKILL_DIR}/.venv/bin/python is then correct under a
# personal install, a Codex install and a plugin install without a lookup.

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILLS_ROOT="$HOME/.claude/skills"

echo "=== imager skill installer (Claude Code) ==="
echo

# --- Dependencies ---
# 3.9 is the floor: the CLI carries `from __future__ import annotations`, so its
# PEP 604 hints are safe below 3.10, and PyYAML (its only dependency) supports
# 3.8+. ImageMagick is checked by setup.sh rather than here, because it is
# optional - it is needed for platform resizing and carousel contact sheets, and
# for nothing else.
if ! command -v python3 >/dev/null 2>&1; then
  echo "Missing required dependency: python3"
  echo "  macOS:  brew install python"
  echo "  Ubuntu: sudo apt install python3 python3-venv"
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

# --- Setup: venv + dependency ---
# Run unconditionally, unlike the garmin skill's installer. There is nothing to
# prompt for here: the API key is read from the environment rather than stored,
# so setup is idempotent and safe to repeat.
"$SKILLS_ROOT/imager/scripts/setup.sh"

echo
echo "Done. Try: 'generate an editorial image of a rocket'"
