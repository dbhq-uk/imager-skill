# Developer setup - imager

Set the skill up from source with a **live symlink install**, so your edits are active immediately in Claude Code (and Codex). End users do not need this - they install with `npx skills add dbhq-uk/imager-skill` or by cloning and running `./install.sh`.

## Prerequisites

- **Python 3.9+.** The CLI carries `from __future__ import annotations`, which is what makes its PEP 604 hints legal that far back
- `git` (and the GitHub CLI `gh` if you will push changes)
- An **OpenAI API key** in `OPENAI_API_KEY`, to generate anything. Not needed to run the tests
- **ImageMagick** (optional) - only for platform resizing and carousel contact sheets

## 1. Clone

```bash
git clone https://github.com/dbhq-uk/imager-skill.git ~/dbhq-uk/imager-skill
cd ~/dbhq-uk/imager-skill
```

## 2. Install (symlink)

```bash
./install.sh          # Claude Code: symlinks into ~/.claude/skills (edits are live)
./install-codex.sh    # Codex: installs into ~/.codex/skills
```

Any path the skill names uses `${CLAUDE_SKILL_DIR}` (the skill's own directory), which Claude Code substitutes for personal, project and plugin installs alike. So `install.sh` symlinks the **whole skill directory** into `~/.claude/skills/` - `SKILL.md`, `scripts/`, the catalogues and `references/` are all live, and every edit takes effect with no re-run. Codex does not substitute `${CLAUDE_SKILL_DIR}`, so `install-codex.sh` rewrites it to the install path - **re-run `./install-codex.sh` after editing a `SKILL.md`** for Codex.

Neither installer installs a package. The CLI uses the standard library only, so the system `python3` runs it under every install shape, including a plugin install that runs no installer at all. Setup is non-interactive: there is no credential to enter, because the key is read from the environment on every run.

## 3. Verify without spending anything

```bash
cd ~/dbhq-uk/imager-skill/skills/imager
export OPENAI_API_KEY=test-key-not-real

python3 scripts/imager.py list-presets
python3 scripts/imager.py --dry-run --preset editorial "a rocket" out.png
python3 scripts/imager.py --estimate --n 10 --quality high "batch"
python3 -m pytest tests/ -v          # needs pytest; the skill itself needs nothing
```

`--dry-run` prints the fully assembled prompt and returns before a request is built. `--estimate` prices a batch and generates nothing. Between them you can check almost everything for free.

## 4. Then spend a penny

The suite makes no network call, so it proves the prompt assembly, the cost arithmetic and the CLI - and nothing at all about what comes back.

After changing prompt assembly or a preset, generate one real draft and look at it:

```bash
export OPENAI_API_KEY=sk-...
python3 scripts/imager.py --draft --preset editorial "a cat astronaut" /tmp/check.png
```

That costs about $0.006 and it is the only check that can tell you the image got worse. A preset that reads well and renders badly passes every test in this repo.

## Where the content lives

| File | Contents |
|---|---|
| `skills/imager/SKILL.md` | The interactive flow: what to ask, in what order, and the cost table |
| `skills/imager/scripts/imager.py` | The CLI, and all of the logic |
| `skills/imager/presets.json` | 27 presets, each a `description` and a `prompt` |
| `skills/imager/platforms.json` | 8 output sizes |
| `skills/imager/references/api_reference.md` | The API surface in full |

The two fields on a preset are not interchangeable. `description` is the menu label; `prompt` is what gets sent, and editing it changes every image that preset will ever produce. Treat that as a breaking change.

## Working across machines

Editing anything under `~/dbhq-uk/imager-skill` is live immediately in Claude Code - the skill directory is symlinked whole. For Codex, re-run `./install-codex.sh` after a `SKILL.md` edit. If you develop on more than one machine, `git pull` before you start and `git push` when done. Your API key is never in the repository.
