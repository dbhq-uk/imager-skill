#!/bin/bash
# Install the imager skill into ~/.codex/skills/ for Codex.
#
# Codex does not substitute ${CLAUDE_SKILL_DIR}, so this script rewrites that
# variable to each skill's installed Codex path and symlinks the supporting
# directories (edits stay live). Re-run after editing a SKILL.md.

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILLS_ROOT="$HOME/.codex/skills"

echo "=== imager skill installer (Codex) ==="
echo

if ! command -v python3 >/dev/null 2>&1; then
  echo "Missing required dependency: python3"
  exit 1
fi
if ! python3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)"; then
  echo "Error: Python 3.9+ required"
  exit 1
fi
echo "Dependencies OK."
echo

# --- Check the machine ---
# setup.sh installs nothing: the CLI uses the Python standard library only. It
# checks python3, reminds you about the API key and looks for ImageMagick.
"$SCRIPT_DIR/skills/imager/scripts/setup.sh"
echo

mkdir -p "$SKILLS_ROOT"
for src in "$SCRIPT_DIR"/skills/*/; do
  src="${src%/}"
  name="$(basename "$src")"
  target="$SKILLS_ROOT/$name"
  echo "Installing '$name' -> $target"
  mkdir -p "$target"
  # Clear what a previous install left before linking what this one needs.
  # Without this, an entry that has since been renamed or deleted upstream
  # survives as a symlink to a path that no longer exists - and a dangling
  # link fails more confusingly than a missing file, because it looks
  # installed. Only symlinks are removed, so a real SKILL.md is never at risk.
  find "$target" -mindepth 1 -maxdepth 1 -type l -exec rm -f {} +
  # Every directory SKILL.md can reference, so each one exists under the
  # rewritten path too.
  for sub in references scripts; do
    [ -d "$src/$sub" ] && ln -sfn "$src/$sub" "$target/$sub"
  done
  # The two catalogues are not named by SKILL.md - the CLI finds them from its
  # own resolved location, which lands in the source tree either way. They are
  # linked anyway so the Codex install is a complete picture of the skill, and
  # so that dropping the .resolve() in imager.py cannot silently break it.
  for f in presets.json platforms.json; do
    [ -f "$src/$f" ] && ln -sfn "$src/$f" "$target/$f"
  done
  chmod +x "$src"/scripts/*.sh 2>/dev/null || true
  sed "s#\${CLAUDE_SKILL_DIR}#$target#g" "$src/SKILL.md" > "$target/SKILL.md"
done

echo
echo "Installed for Codex. Re-run after editing a SKILL.md - that file is
rewritten at install time rather than symlinked, so its edits are not live."
echo
echo "Done. Try: 'generate an editorial image of a rocket'"
