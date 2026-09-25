# GPT Image Skill

Generate and edit images via OpenAI's GPT Image API with an interactive, guided workflow.

Adapted from [glebis/claude-skills](https://github.com/glebis/claude-skills/tree/main/gpt-image-2).

## Features

- **Three models:** `gpt-image-2.5-flare` (default), `gpt-image-2.5-sunburst`, `gpt-image-2`
- **Style presets:** 27 presets across visual, text-heavy, community and social categories
- **Platform sizing:** YouTube, Instagram, slides, blog hero, X/Twitter, story, Pinterest - the
  image is generated at the platform's own aspect and scaled down, not generated square and
  cropped
- **Draft then final:** iterate at ~$0.006/image before paying for a final
- **Transparent backgrounds:** `--background transparent` for logos, icons and assets
- **Output formats:** png, jpeg, webp with compression control
- **Masked inpainting:** `--edit` + `--mask` to change one region and leave the rest
- **Reference images:** up to 16, to carry a subject or a look into a new scene
- **Real cost, not an estimate:** every run reads `usage` back from the API and records what was
  actually billed; estimates self-calibrate from that history
- **Set consistency:** `set-check` reads the history for a directory and matches its model and
  quality tier
- **Batch runs:** `batch runs.jsonl` runs many images with one set check, one price and one
  confirmation, resumes where it stopped, and honours a `daily_cap` that `-y` cannot pass

## Quick Start

```bash
# 1. Install the skill. Nothing else to install: standard library only
./install.sh                          # from the repo root

# 2. Set your OpenAI API key
export OPENAI_API_KEY=sk-...

# 3. Run the onboarding wizard
python3 ${CLAUDE_SKILL_DIR}/scripts/imager.py init

# 4. Try a draft image
python3 ${CLAUDE_SKILL_DIR}/scripts/imager.py \
  --draft --preset editorial "a cat astronaut" ./cat.png
```

In Claude Code, just describe what you want - the skill will guide you interactively.

## API Key

The script reads `OPENAI_API_KEY` from the environment. Put it in your shell rc file:

```bash
echo 'export OPENAI_API_KEY=sk-...' >> ~/.bashrc
```

## No seed, no thinking mode

Neither parameter exists in the OpenAI image API. Earlier versions of this skill accepted
`--seed` and `--thinking`, logged them, and sent neither; `--thinking` also inflated the cost
estimate. Both now fail with an error explaining what to use instead - the prompt and
`--reference` for consistency, `--quality` or `--model sunburst` for complex layouts.

## Optional Dependencies

- **ImageMagick** 7 (`magick`) or 6 (`convert` and `montage`) on PATH - required for platform
  fitting and contact sheets. Without it the image is saved at its generated size, and the CLI
  says so
  - macOS: `brew install imagemagick`
  - Linux: `sudo apt install imagemagick`

## Files

- `SKILL.md` - interactive workflow Claude follows when invoked
- `scripts/imager.py` - main CLI (Python 3.9+, standard library only)
- `scripts/setup.sh` - checks python3, the API key and ImageMagick; installs nothing
- `presets.json` - 27 style presets
- `platforms.json` - 8 platform sizes
- `references/api_reference.md` - full API documentation

User config and history live at `~/.dbhq/imager/`. Set `GPT_IMAGE_HOME` to relocate them.

See `SKILL.md` for the full interactive workflow and CLI reference.
