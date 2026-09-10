# AGENTS.md

Guidance for AI agents (and people) working in this repository.

## What this is

The **gpt-image-2** skill for AI coding agents - generate and edit images with OpenAI's GPT Image 2, through a guided draft-then-final flow. It follows the [Agent Skills](https://agentskills.io) layout (`skills/<name>/SKILL.md`) and ships as a [Claude Code plugin](https://code.claude.com/docs/en/plugins).

## Layout

```
.claude-plugin/plugin.json                    # plugin manifest
skills/gpt-image-2/SKILL.md                   # the skill (agent-facing instructions)
skills/gpt-image-2/scripts/gpt_image_2.py     # the CLI, and all of the logic
skills/gpt-image-2/scripts/setup.sh           # venv + PyYAML
skills/gpt-image-2/presets.yaml               # 21 style presets
skills/gpt-image-2/platforms.yaml             # 8 platform sizes
skills/gpt-image-2/references/api_reference.md
skills/gpt-image-2/tests/                     # pytest suite, no network
install.sh / install-codex.sh                 # local symlink installers (Claude / Codex)
```

The venv lives at `skills/gpt-image-2/.venv`, built by `scripts/setup.sh` and gitignored. It is inside the skill directory on purpose: `${CLAUDE_SKILL_DIR}/.venv/bin/python` is then correct under a personal install, a Codex install and a plugin install without a lookup table.

## The three constraints that must not be broken

Everything else here is a preference. These are not.

**1. Nothing spends money without pricing it first.** `estimate_cost` runs before the request, and at or above `CONFIRM_THRESHOLD` ($0.50) the run stops and asks. `-y` is the only way to skip that, and it has to stay an explicit opt-in rather than a default, a config setting, or something the interactive flow quietly passes on the user's behalf. A tool that spends someone's money and surprises them about the amount has done real damage, and it only has to happen once.

**2. The draft loop stays the default.** Generate low quality, show it, ask, then upgrade with the same `--seed`. It is not a nicety - it is a 97% saving on the iteration that finding a direction actually takes, and it is the only reason this is cheap enough to play with. If you are editing `SKILL.md` and about to let it jump to a final because the prompt looked confident, do not.

**3. The API key is read, never written.** It comes from `OPENAI_API_KEY` (or `OPENROUTER_API_KEY`) in the environment on every run. `~/.dbhq/gpt-image-2/` holds config, a history log and a last-run record, and none of the three has a field for a key. Do not add one "for convenience", do not log the request headers, and do not write the key into the history entry so that `again` can replay it.

## Conventions

- Any path a `SKILL.md` names must use `${CLAUDE_SKILL_DIR}` (the skill's own directory), which Claude Code substitutes for personal, project and plugin installs alike. `install.sh` therefore symlinks the whole skill directory into `~/.claude/skills/` with no rewrite. `install-codex.sh` rewrites the variable, since Codex does not substitute it. **Never hardcode an install path** - it is wrong under a Codex install and wrong under a plugin install, and CI fails on it. The CLI derives its own location from `__file__`, which is how it finds the two catalogues.
- **One dependency, PyYAML, and no vendor SDK.** The API calls go out over `urllib` from the standard library. This is a skill that holds an API key, so the surface between the key and the wire stays small enough for a reader to check in one sitting. Adding `openai` or `requests` would undo that for no capability this does not already have.
- Python floor is **3.9**. The module carries `from __future__ import annotations`, which is what makes its PEP 604 hints legal down there. Do not remove that import, and do not reach for syntax the floor cannot parse - `ruff`'s `target-version = "py39"` will catch most of it and the CI matrix catches the rest.
- Shell scripts use `set -e`; errors go to stderr, output to stdout.
- House style: British English, plain hyphens, no em dashes. CI enforces the last one, and it earns its place here: the preset catalogue arrived from upstream with an em dash in every description.

## The catalogues are data, and the split in them matters

Each entry in `presets.yaml` has two fields and they are not interchangeable. `description` is what the user picks from; `prompt` is what is actually sent to the model. Editing a description changes a menu label. Editing a prompt changes every image that preset will ever produce.

CI checks that both are present on every preset, because a preset with one missing is listable and unusable, and nothing else in the pipeline notices.

## Validating a change

```bash
bash -n install.sh install-codex.sh
shellcheck ./install.sh ./install-codex.sh ./skills/*/scripts/*.sh
ruff check . && ruff format --check .
cd skills/gpt-image-2 && OPENAI_API_KEY=test-key-not-real .venv/bin/python -m pytest tests/ -v
claude plugin validate .
```

CI runs all of that, on Python 3.9, 3.11 and 3.13, plus both installers end to end and a `--dry-run` through the installed skill.

Nothing in the suite makes a network call, and that is worth being honest about: it proves the prompt assembly, the cost arithmetic and the CLI, and it proves nothing about what OpenAI does with the result. After changing prompt assembly or a preset, generate one real draft and look at it. It costs less than a penny, and it is the only check that can tell you the image got worse.
