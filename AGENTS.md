# AGENTS.md

Guidance for AI agents (and people) working in this repository.

## What this is

The **imager** skill for AI coding agents - generate and edit images with OpenAI's GPT Image models (`gpt-image-2.5-flare` by default, plus `gpt-image-2.5-sunburst` and `gpt-image-2`), through a guided draft-then-final flow. It follows the [Agent Skills](https://agentskills.io) layout (`skills/<name>/SKILL.md`) and ships as a [Claude Code plugin](https://code.claude.com/docs/en/plugins).

**The skill is called `imager`, and the model it calls is not.** It was named `gpt-image-2` until 13 Sep 2026, after the model it happened to launch on - which was wrong in both directions: it dated the skill to one model, and it read as OpenAI's product rather than DBHQ's. The rename is only the skill. `gpt-image-2`, `gpt-image-2.5-flare` and `gpt-image-2.5-sunburst` are OpenAI's model identifiers and appear throughout the code, the pricing tables and the docs unchanged.

**If you are renaming anything else here, that is the line.** The skill, its directory, its CLI file, its plugin name and its settings directory are `imager`; every string that goes to the API stays exactly as OpenAI publishes it. In Python the test is mechanical: `imager.` is the module, and a quoted `"gpt-image-2"` is a model.

The old settings directory is migrated on first run by `_migrate_legacy_settings()`, guarded on the destination not existing, so an existing install keeps working untouched. Output written under `~/gpt-image-2/outputs` before the rename is deliberately left where it is - those are the user's images, not skill state.

## Layout

```
.claude-plugin/plugin.json                    # plugin manifest
skills/imager/SKILL.md                   # the skill (agent-facing instructions)
skills/imager/scripts/imager.py     # the CLI, and all of the logic
skills/imager/scripts/setup.sh           # venv + PyYAML
skills/imager/presets.yaml               # 27 style presets
skills/imager/platforms.yaml             # 8 platform sizes
skills/imager/references/api_reference.md
skills/imager/tests/                     # pytest suite, no network
install.sh / install-codex.sh                 # local symlink installers (Claude / Codex)
```

The venv lives at `skills/imager/.venv`, built by `scripts/setup.sh` and gitignored. It is inside the skill directory on purpose: `${CLAUDE_SKILL_DIR}/.venv/bin/python` is then correct under a personal install, a Codex install and a plugin install without a lookup table.

## The four constraints that must not be broken

Everything else here is a preference. These are not.

**1. Nothing spends money without pricing it first.** `estimate_cost` runs before the request, and at or above `CONFIRM_THRESHOLD` ($0.50) the run stops and asks. `-y` is the only way to skip that, and it has to stay an explicit opt-in rather than a default, a config setting, or something the interactive flow quietly passes on the user's behalf. A tool that spends someone's money and surprises them about the amount has done real damage, and it only has to happen once.

An estimate is allowed to be too high and never too low. `cost_per_unit` prefers a figure measured from real runs, falls back to OpenAI's published table, and falls back again to a deliberately pessimistic ceiling - in that order, and it reports which one it used. If you add a model or a quality tier, give it a `FALLBACK_COST` entry at or above what it can really cost. An under-quote is what lets a batch through the gate.

**2. The draft loop stays the default.** Generate low quality, show it, ask, then upgrade. It is not a nicety - it is a 97% saving on the iteration that finding a direction actually takes, and it is the only reason this is cheap enough to play with. If you are editing `SKILL.md` and about to let it jump to a final because the prompt looked confident, do not. Note that `config.yaml` must not carry a `quality` key by default: `cmd_init` used to write `quality: high` into it, which silently overrode the low default on every later call.

**3. The API key is read, never written.** It comes from `OPENAI_API_KEY` in the environment on every run. `~/.dbhq/imager/` holds config, a history log and a last-run record, and none of the three has a field for a key. Do not add one "for convenience", do not log the request headers, and do not write the key into the history entry so that `again` can replay it.

**4. Never accept a parameter the API does not have.** `--seed` and `--thinking` lived here for months: documented in `SKILL.md`, printed in the run line, written to `history.jsonl`, and never put in a request body. `--thinking` also multiplied the cost estimate, so the confirmation gate fired on numbers describing a request that had never been sent. Both are absent from `CreateImageRequest` and `CreateImageEditRequest` in `openai/openai-openapi`, and from the image generation guide.

They are now in `RETIRED_FLAGS` and exit non-zero with an explanation, because the failure was not the missing feature - it was that the skill told users composition was locked between draft and final when nothing was locking it. If you add a flag, it must reach the wire, and if a flag stops reaching the wire it must start erroring.

## Conventions

- Any path a `SKILL.md` names must use `${CLAUDE_SKILL_DIR}` (the skill's own directory), which Claude Code substitutes for personal, project and plugin installs alike. `install.sh` therefore symlinks the whole skill directory into `~/.claude/skills/` with no rewrite. `install-codex.sh` rewrites the variable, since Codex does not substitute it. **Never hardcode an install path** - it is wrong under a Codex install and wrong under a plugin install, and CI fails on it. The CLI derives its own location from `__file__`, which is how it finds the two catalogues.
- **One dependency, PyYAML, and no vendor SDK.** The API calls go out over `urllib` from the standard library. This is a skill that holds an API key, so the surface between the key and the wire stays small enough for a reader to check in one sitting. Adding `openai` or `requests` would undo that for no capability this does not already have.
- Python floor is **3.9**. The module carries `from __future__ import annotations`, which is what makes its PEP 604 hints legal down there. Do not remove that import, and do not reach for syntax the floor cannot parse - `ruff`'s `target-version = "py39"` will catch most of it and the CI matrix catches the rest.
- Shell scripts use `set -e`; errors go to stderr, output to stdout.
- **One provider: OpenAI.** `--provider openrouter` was removed on 25 Sep 2026. It posted un-prefixed model IDs to `/api/v1/images/generations` and multipart to `/api/v1/images/edits`, neither of which OpenRouter's image API uses, and `cost_from_usage` would have read OpenRouter's usage block as zero. It is in `RETIRED_PROVIDERS` and exits with a reason. If a second provider is ever wanted, add it through OpenRouter's `/api/v1/images` with `openai/`-prefixed model IDs and references in `input_references`, and record its `usage.cost` as the billed figure rather than pricing tokens.
- House style: British English, plain hyphens, no em dashes. CI enforces the last one, and it earns its place here: the preset catalogue arrived from upstream with an em dash in every description.

## The catalogues are data, and the split in them matters

Each entry in `presets.yaml` has two fields and they are not interchangeable. `description` is what the user picks from; `prompt` is what is actually sent to the model. Editing a description changes a menu label. Editing a prompt changes every image that preset will ever produce.

CI checks that both are present on every preset, because a preset with one missing is listable and unusable, and nothing else in the pipeline notices.

## Validating a change

```bash
bash -n install.sh install-codex.sh
shellcheck ./install.sh ./install-codex.sh ./skills/*/scripts/*.sh
ruff check . && ruff format --check .
cd skills/imager && OPENAI_API_KEY=test-key-not-real .venv/bin/python -m pytest tests/ -v
claude plugin validate .
```

CI runs all of that, on Python 3.9, 3.11 and 3.13, plus both installers end to end and a `--dry-run` through the installed skill.

Nothing in the suite makes a network call, and that is worth being honest about: it proves the prompt assembly, the cost arithmetic and the CLI, and it proves nothing about what OpenAI does with the result. After changing prompt assembly or a preset, generate one real draft and look at it. It costs less than a penny, and it is the only check that can tell you the image got worse.
