# GPT Image 2 Skill

Generate and edit images via OpenAI's GPT Image 2 API with an interactive, guided workflow.

Adapted from [glebis/claude-skills](https://github.com/glebis/claude-skills/tree/main/gpt-image-2).

## Features

- **Style presets:** 21 presets across visual, text-heavy, and community categories
- **Platform sizing:** YouTube, Instagram, slides, blog hero, X/Twitter, story
- **Draft → final flow:** Iterate cheaply at $0.006/image before paying $0.21 for a final
- **Carousels:** Cohesive 5-10 slide sequences with seed-locked composition
- **Photo edit:** Transform an existing photo into a preset style
- **Thinking mode:** Better text rendering and layout fidelity for complex compositions
- **Cost awareness:** Estimates and prompts for confirmation above $0.50

## Quick Start

```bash
# 1. Install the skill (creates venv, installs PyYAML)
./install.sh gpt-image-2

# 2. Set your OpenAI API key
export OPENAI_API_KEY=sk-...

# 3. Run the onboarding wizard
~/.claude/skills/gpt-image-2/.venv/bin/python \
  ~/.claude/skills/gpt-image-2/scripts/gpt_image_2.py init

# 4. Try a draft image
~/.claude/skills/gpt-image-2/.venv/bin/python \
  ~/.claude/skills/gpt-image-2/scripts/gpt_image_2.py \
  --draft --preset editorial "a cat astronaut" ./cat.png
```

In Claude Code, just describe what you want — the skill will guide you interactively.

## API Key

The script reads `OPENAI_API_KEY` (or `OPENROUTER_API_KEY` if you set `--provider openrouter`) from the environment. Put it in your shell rc file:

```bash
echo 'export OPENAI_API_KEY=sk-...' >> ~/.bashrc
```

## Optional Dependencies

- **ImageMagick** (`magick` on PATH) — required for platform resizing and carousel contact sheets
  - macOS: `brew install imagemagick`
  - Linux: `sudo apt install imagemagick`

## Files

- `SKILL.md` — interactive workflow Claude follows when invoked
- `scripts/gpt_image_2.py` — main CLI (Python, requires PyYAML)
- `presets.yaml` — 21 style presets
- `platforms.yaml` — 8 platform sizing presets
- `references/api_reference.md` — full API documentation

User config and history live at `~/.config/gpt-image-2/`.

See `SKILL.md` for the full interactive workflow and CLI reference.
