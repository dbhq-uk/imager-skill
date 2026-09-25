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
skills/imager/scripts/setup.sh           # checks the machine; installs nothing
skills/imager/presets.json               # 27 style presets
skills/imager/platforms.json             # 8 platform sizes
skills/imager/references/api_reference.md
skills/imager/tests/                     # pytest suite, no network
install.sh / install-codex.sh                 # local symlink installers (Claude / Codex)
```

**There is no venv, and there must not be one again.** `/plugin install` and `npx skills add` copy the skill directory and run nothing, so a skill that needs an install step does not start. PyYAML was the only reason for a venv: the two catalogues are JSON now, and `parse_config` reads `config.yaml`, which has only ever held flat `key: value` lines. `SKILL.md` runs the CLI with plain `python3`, and the `bare-copy` CI job runs it from a copied directory on an interpreter with no packages. A `.venv` left by an old install is unused and can be deleted.

## The four constraints that must not be broken

Everything else here is a preference. These are not.

**1. Nothing spends money without pricing it first.** `estimate_cost` runs before the request, and at or above `CONFIRM_THRESHOLD` ($0.50) the run stops and asks. `-y` is the only way to skip that, and it has to stay an explicit opt-in rather than a default, a config setting, or something the interactive flow quietly passes on the user's behalf. A tool that spends someone's money and surprises them about the amount has done real damage, and it only has to happen once.

**The spend record is write-ahead.** `run_request`, which both a single run and `batch` go through, appends a `pending` row to `history.jsonl` at the estimate before the request is sent, and appends the final row, with the same `id`, once the images are saved and before any ImageMagick step. `read_history_entries` lets the final row replace the pending one, and skips a line that does not parse rather than stopping there. A pending row with no final row is a run that was killed or timed out mid-request; it may have been billed, so `spend_today` counts it at the estimate. Do not move the history write back to the end of the run: that is how billed images went unrecorded.

**`daily_cap` is the one limit `-y` cannot pass.** The per-call gate cannot see a loop of cheap calls, and the $5 daily figure only warns. `enforce_daily_cap` runs before every request, single or batch, and exits 4 when the run would take today's spend over the cap in `config.yaml`. Keep it ahead of the `-y` check, and never give `batch` a way round it. A batch is one command for the same reason: a runner that calls the API itself gets no set check, no daily total and no record, so `SKILL.md` tells agents to use `batch` rather than write one.

An estimate is allowed to be too high and never too low. `cost_per_unit` prices a run the way it is billed: prompt text, input images and output image, each at its `TOKEN_PRICES` rate, and each input image once per output image. Output tokens come from `OUTPUT_TOKENS` (per model and tier, at 1024x1024) scaled by `size_factor`, or from history once three runs agree on model, quality, size and kind. At `size=auto` history may raise that figure but never lower it, because the API picks a different size each time. The estimate reports its basis: `measured`, `calibrated`, `published` or `upper bound`. If you add a model or a quality tier, give it an `OUTPUT_TOKENS` entry at or above what it can really return, and mark it `upper bound` until it has been measured. An under-quote is what lets a batch through the gate.

**2. The draft loop stays the default.** Generate low quality, show it, ask, then upgrade. It is not a nicety - it is a 97% saving on the iteration that finding a direction actually takes, and it is the only reason this is cheap enough to play with. If you are editing `SKILL.md` and about to let it jump to a final because the prompt looked confident, do not. Note that `config.yaml` must not carry a `quality` key by default: `cmd_init` used to write `quality: high` into it, which silently overrode the low default on every later call.

**3. The API key is read, never written.** It comes from `OPENAI_API_KEY` in the environment on every run. `~/.dbhq/imager/` holds config, a history log and a last-run record, and none of the three has a field for a key. Do not add one "for convenience", do not log the request headers, and do not write the key into the history entry so that `again` can replay it.

**4. Never accept a parameter the API does not have.** `--seed` and `--thinking` lived here for months: documented in `SKILL.md`, printed in the run line, written to `history.jsonl`, and never put in a request body. `--thinking` also multiplied the cost estimate, so the confirmation gate fired on numbers describing a request that had never been sent. Both are absent from `CreateImageRequest` and `CreateImageEditRequest` in `openai/openai-openapi`, and from the image generation guide.

They are now in `RETIRED_FLAGS` and exit non-zero with an explanation, because the failure was not the missing feature - it was that the skill told users composition was locked between draft and final when nothing was locking it. If you add a flag, it must reach the wire, and if a flag stops reaching the wire it must start erroring.

## Conventions

- Any path a `SKILL.md` names must use `${CLAUDE_SKILL_DIR}` (the skill's own directory), which Claude Code substitutes for personal, project and plugin installs alike. `install.sh` therefore symlinks the whole skill directory into `~/.claude/skills/` with no rewrite. `install-codex.sh` rewrites the variable, since Codex does not substitute it. **Never hardcode an install path** - it is wrong under a Codex install and wrong under a plugin install, and CI fails on it. The CLI derives its own location from `__file__`, which is how it finds the two catalogues.
- **The standard library only, and no vendor SDK.** The API calls go out over `urllib`, the catalogues are JSON, and `config.yaml` is read by `parse_config`. This is a skill that holds an API key, so the surface between the key and the wire stays small enough for a reader to check in one sitting. Adding `openai` or `requests` would undo that for no capability this does not already have.
- Python floor is **3.9**. The module carries `from __future__ import annotations`, which is what makes its PEP 604 hints legal down there. Do not remove that import, and do not reach for syntax the floor cannot parse - `ruff`'s `target-version = "py39"` will catch most of it and the CI matrix catches the rest.
- Shell scripts use `set -e`; errors go to stderr, output to stdout.
- **`preview_command` has no default, and runs without a shell.** Nothing DBHQ-specific ships: with the key unset, nothing runs. When set, `preview_template` splits it with `shlex` before anything is priced, and `run_preview` fills `{path}` and `{name}` into each argument and runs it with no shell, so a file name can never become shell syntax. `{name}` comes from `unique_name`, because a helper that stores files by basename made every `out.png` replace the last. A preview that fails is a warning: the image is paid for and saved.
- **The last stdout line of a run is JSON, schema `imager.result/v1`.** `result_line` builds it for single runs and batches alike. Add fields to it; never rename or remove one, because scripts and agents read it. Contact sheets go to `contact-sheets/` under the settings directory, never into the set's folder, and `prune_contact_sheets` keeps the newest 50.
- **One provider: OpenAI.** `--provider openrouter` was removed on 25 Sep 2026. It posted un-prefixed model IDs to `/api/v1/images/generations` and multipart to `/api/v1/images/edits`, neither of which OpenRouter's image API uses, and `cost_from_usage` would have read OpenRouter's usage block as zero. It is in `RETIRED_PROVIDERS` and exits with a reason. If a second provider is ever wanted, add it through OpenRouter's `/api/v1/images` with `openai/`-prefixed model IDs and references in `input_references`, and record its `usage.cost` as the billed figure rather than pricing tokens.
- House style: British English, plain hyphens, no em dashes. CI enforces the last one, and it earns its place here: the preset catalogue arrived from upstream with an em dash in every description.

## The catalogues are data, and the split in them matters

Each entry in `presets.json` has two fields and they are not interchangeable. `description` is what the user picks from; `prompt` is what is actually sent to the model. Editing a description changes a menu label. Editing a prompt changes every image that preset will ever produce.

CI checks that both are present on every preset, because a preset with one missing is listable and unusable, and nothing else in the pipeline notices.

## SKILL.md is for the agent; the history lives here

`SKILL.md` is loaded into the agent's context every time the skill runs, so every word in it costs something on every call. Keep it under 2,000 words, with instructions only. A test holds the ceiling, and holds the sections that must stay: "When not to use", the timeout guidance, the retirement dates, and no Claude-only tool names, since the skill also ships for Codex. The reasons behind the rules go here or in a commit message, not there.

**Why Step 0 exists.** On 21 Aug 2026 all three tiers were generated side by side for one set of small images, and low was chosen, because the images ship at 300x400 and the downscale throws away what the higher tiers buy. Two days later a new batch for the same set went out at high, only because high was the default then, at 35 times the price for no visible difference. The answer was in `history.jsonl` the whole time. That is why the CLI matches a set's tier by itself, why Step 4 says to pass no `--quality` into an existing set, and why `batch` exists rather than a hand-written runner.

**Why `--seed` and `--thinking` error rather than vanish** is constraint 4 above. `SKILL.md` now says only that neither exists.

## Validating a change

```bash
bash -n install.sh install-codex.sh
shellcheck ./install.sh ./install-codex.sh ./skills/*/scripts/*.sh
ruff check . && ruff format --check .
cd skills/imager && OPENAI_API_KEY=test-key-not-real python3 -m pytest tests/ -v
claude plugin validate .
```

CI runs all of that, on Python 3.9, 3.11 and 3.13, plus both installers end to end and a `--dry-run` through the installed skill.

Nothing in the suite makes a network call, and that is worth being honest about: it proves the prompt assembly, the cost arithmetic and the CLI, and it proves nothing about what OpenAI does with the result. After changing prompt assembly or a preset, generate one real draft and look at it. It costs less than a penny, and it is the only check that can tell you the image got worse.
