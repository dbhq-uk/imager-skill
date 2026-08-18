# Developer setup - gpt-image-2

Set the skill up from source with a **live symlink install**, so your edits are active immediately in Claude Code (and Codex). End users do not need this - they install with `npx skills add dbhq-uk/gpt-image-2-skill` or by cloning and running `./install.sh`.

## Prerequisites

- **Python 3.9+.** The CLI carries `from __future__ import annotations`, which is what makes its PEP 604 hints legal that far back
- `git` (and the GitHub CLI `gh` if you will push changes)
- An **OpenAI API key** in `OPENAI_API_KEY`, to generate anything. Not needed to run the tests
- **ImageMagick** (optional) - only for platform resizing and carousel contact sheets

## 1. Clone

```bash
git clone https://github.com/dbhq-uk/gpt-image-2-skill.git ~/dbhq-gpt-image-2
cd ~/dbhq-gpt-image-2
```

## 2. Install (symlink)

```bash
./install.sh          # Claude Code: symlinks into ~/.claude/skills (edits are live)
./install-codex.sh    # Codex: installs into ~/.codex/skills
```

Any path the skill names uses `${CLAUDE_SKILL_DIR}` (the skill's own directory), which Claude Code substitutes for personal, project and plugin installs alike. So `install.sh` symlinks the **whole skill directory** into `~/.claude/skills/` - `SKILL.md`, `scripts/`, the catalogues and `references/` are all live, and every edit takes effect with no re-run. Codex does not substitute `${CLAUDE_SKILL_DIR}`, so `install-codex.sh` rewrites it to the install path - **re-run `./install-codex.sh` after editing a `SKILL.md`** for Codex.

Both installers build the virtualenv at `skills/gpt-image-2/.venv` and install PyYAML into it. Setup is non-interactive: there is no credential to enter, because the key is read from the environment on every run.

The venv is inside the skill directory rather than somewhere shared, and that is deliberate: `${CLAUDE_SKILL_DIR}/.venv/bin/python` is then the right interpreter under every install shape without a lookup.

## 3. Verify without spending anything

```bash
cd ~/dbhq-gpt-image-2/skills/gpt-image-2
export OPENAI_API_KEY=test-key-not-real

.venv/bin/python scripts/gpt_image_2.py list-presets
.venv/bin/python scripts/gpt_image_2.py --dry-run --preset editorial "a rocket" out.png
.venv/bin/python scripts/gpt_image_2.py --estimate --n 10 --quality high "batch"
.venv/bin/python -m pytest tests/ -v
```

`--dry-run` prints the fully assembled prompt and returns before a request is built. `--estimate` prices a batch and generates nothing. Between them you can check almost everything for free.

## 4. Then spend a penny

The suite makes no network call, so it proves the prompt assembly, the cost arithmetic and the CLI - and nothing at all about what comes back.

After changing prompt assembly or a preset, generate one real draft and look at it:

```bash
export OPENAI_API_KEY=sk-...
.venv/bin/python scripts/gpt_image_2.py --draft --preset editorial "a cat astronaut" /tmp/check.png
```

That costs about $0.006 and it is the only check that can tell you the image got worse. A preset that reads well and renders badly passes every test in this repo.

## Where the content lives

| File | Contents |
|---|---|
| `skills/gpt-image-2/SKILL.md` | The interactive flow: what to ask, in what order, and the cost table |
| `skills/gpt-image-2/scripts/gpt_image_2.py` | The CLI, and all of the logic |
| `skills/gpt-image-2/presets.yaml` | 21 presets, each a `description` and a `prompt` |
| `skills/gpt-image-2/platforms.yaml` | 8 output sizes |
| `skills/gpt-image-2/references/api_reference.md` | The API surface in full |

The two fields on a preset are not interchangeable. `description` is the menu label; `prompt` is what gets sent, and editing it changes every image that preset will ever produce. Treat that as a breaking change.

## Working across machines

Editing anything under `~/dbhq-gpt-image-2` is live immediately in Claude Code - the skill directory is symlinked whole. For Codex, re-run `./install-codex.sh` after a `SKILL.md` edit. If you develop on more than one machine, `git pull` before you start and `git push` when done. The venv is local to each machine and is not in the repository, and neither is your API key.
