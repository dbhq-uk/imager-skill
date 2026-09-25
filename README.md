<div align="center">

<img src="assets/logo.svg" alt="imager skill for Claude Code, by DBHQ" width="560">

# imager

**Iterate on drafts, pay for the one you approved**

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Claude Code](https://img.shields.io/badge/Claude_Code-Plugin-blueviolet)](https://code.claude.com/docs/en/plugins)
[![Platform](https://img.shields.io/badge/Platform-Linux%20%7C%20macOS%20%7C%20WSL-lightgrey)]()

A free, open-source tool by [DBHQ](https://dbhq.uk) - documented at [skills.dbhq.uk](https://skills.dbhq.uk/imager/)

</div>

---

Generate and edit images with OpenAI's GPT Image models, through a guided flow rather than a flag reference: what are we making, in which style, for where it is going, draft first, then final.

## What makes it different

**The draft loop is the point.** A low-quality draft costs about $0.006 against about $0.053 for a high-quality final on the default model, so the workflow generates a draft, shows it to you, and only spends the real money once you have said yes. Across a ten-slide carousel that is $0.06 to find the direction rather than $0.53.

**It tells you the cost before it spends it, and what it cost afterwards.** Every run estimates first. Below $0.50 it proceeds; at or above, it stops and asks. `--estimate` prices a batch without generating anything, and `--dry-run` prints the fully assembled prompt without making a request at all. The estimate is built from tokens, the way the bill is: prompt text, input images and output image, each at its own rate, so an edit is priced with the images it sends. Once the call returns, the token `usage` the API reports is turned into the real billed figure and logged, and after three runs at the same model, quality, size and kind, the measured output count replaces the table. A tool that spends your money should never surprise you about how much.

**A batch goes through the same guards, once.** `batch runs.jsonl` takes one JSON row per image. It checks the whole file, runs one set check per folder, prices the lot, and asks once. A rerun skips every output already on disk, so an interrupted run resumes. Set `daily_cap` in `config.yaml` and no run, batch or single, can take the day's spend past it, `-y` or not.

**It knows a directory is a set.** `set-check` reads the history for the folder you are writing into and tells you which model and quality tier the images already there were made at - then matches them unless you say otherwise. Inside git a set is the repository plus the folder's path within it, so the same folder in another worktree is the same set. This exists because a batch once went out at 35x the price it needed to, purely because a default said so.

**No dependencies, and no vendor SDK.** The standard library only, so it runs on the system `python3` straight from a plugin install, with no setup step. The API calls go out over `urllib`. That is a deliberate trade: this is a skill that holds an API key, and the less third-party code sits between the key and the wire, the less there is for you to audit before you trust it.

**Presets that carry the whole prompt, not a style word.** 27 of them, each pairing a short description you choose from with a full prompt fragment that does the work - `editorial`, `blueprint`, `ink`, `risograph`, `wireframe`, `constellation`, `brutalist`, `grain`, `nordic`, `bauhaus` for visual work; `infographic`, `slide`, `diagram`, `poster`, `menu`, `manga` where the text in the image has to be legible; plus community favourites and a social set. Platform sizing for the eight places images actually go - and the image is generated at the platform's own aspect ratio, then scaled down, rather than generated square and cropped.

## Install

### As a Claude Code plugin (recommended)

```
/plugin marketplace add dbhq-uk/marketplace
/plugin install imager@dbhq
```

### Any agent (Cursor, Copilot, Windsurf, Gemini, Cline and more)

```bash
npx skills add dbhq-uk/imager-skill
```

The [skills.sh](https://skills.sh) CLI installs into whichever agent directories
it finds, so this works outside Claude Code and Codex too.

### Local install (Claude Code or Codex)

```bash
git clone https://github.com/dbhq-uk/imager-skill.git
cd imager-skill
./install.sh          # Claude Code: a live symlink install (edits take effect at once)
./install-codex.sh    # Codex: installs into ~/.codex/skills
```

[`install.sh`](install.sh) and [`install-codex.sh`](install-codex.sh) are the
same install two ways: Claude Code substitutes `${CLAUDE_SKILL_DIR}`, so the
whole skill directory is symlinked untouched, while Codex does not, so its
`SKILL.md` is rewritten at install time. Re-run the Codex one after editing
`SKILL.md`.

## Requirements

- **Python 3.9 or newer.** Nothing else to install: the CLI uses the standard library only.
- **An OpenAI API key** in `OPENAI_API_KEY`. This skill calls a paid API and does nothing useful without one. The key is read from the environment and never written to disk by this skill.
- **ImageMagick 7 or 6** (optional), needed only for platform resizing and carousel contact sheets. Everything else works without it.

## Usage

Describe what you want and the skill takes it from there.

```
"generate an editorial image of a rocket"
"make me a 10-slide LinkedIn carousel about spreadsheet risk"
"turn this photo into the constellation style"
"a diagram of the OAuth flow, with readable labels"
"four variants of a mountain in ink style"
```

Or drive the CLI directly. From a clone of this repository:

```bash
PY=python3
GEN=skills/imager/scripts/imager.py

$PY $GEN --draft --preset editorial "a cat astronaut" out.png   # ~$0.006
$PY $GEN --quality high --preset editorial "a cat astronaut" out.png
$PY $GEN --model sunburst --quality xhigh --preset diagram "OAuth flow" out.png
$PY $GEN --background transparent "a line-art compass rose, isolated" logo.png
$PY $GEN --estimate --n 10 --quality high "batch test"          # price it, generate nothing
$PY $GEN --dry-run --preset diagram "OAuth flow" out.png        # show the prompt, call nothing
$PY $GEN set-check ./assets/icons/                              # what did this set use?
$PY $GEN batch runs.jsonl --dry-run                             # one plan, one total, many images
```

### Previews and the result line

A run that sends a request ends with one line of JSON: the saved `paths`, the `contact_sheet`, the `cost` and whether it was billed or estimated, and any `preview_urls`. A script or an agent reads that line rather than the prose above it. Contact sheets go to `~/.dbhq/imager/contact-sheets/`, not into the folder the images are in.

On a headless machine, point `preview_command` in `~/.dbhq/imager/config.yaml` at whatever serves a file to your browser, and the CLI runs it after every save:

```yaml
preview_command: "/path/to/preview.sh {path} {name}"
```

`{path}` is the saved file. `{name}` is a file name no other run uses, so a helper that stores files by name never replaces an earlier image. The last line the command prints is the URL. There is no default, so with the key unset nothing runs. The command runs without a shell, and a preview that fails is a warning, never a lost image.

### Models

| Model | When |
|---|---|
| `gpt-image-2.5-flare` **(default)** | Everything, unless a reason says otherwise. About half the latency of `gpt-image-2` and fewer output tokens for the same tier. |
| `gpt-image-2.5-sunburst` | Fine detail, dense text, precision and multi-turn edits. Slower, same token rates. |
| `gpt-image-2` | Adding to a set generated on it, or a Batch API run - the 50% batch discount covers `gpt-image-2` and not the 2.5 models. |

`low`, `medium` and `high` on all three; `xhigh` and `max` on the 2.5 models. The default is
**low**, because the flow is draft-first.

### Prices

Per image at 1024x1024 on `gpt-image-2.5-flare`, the default:

| Quality | Per image | Ten-slide carousel |
|---|---|---|
| `--draft` (low, the default) | $0.006 | $0.06 |
| high | $0.053 | $0.53 |
| xhigh | $0.100 | $1.00 |

Each input image (`--edit`, `--reference`, `--mask`) adds about $0.013 per output image.
Non-square is cheaper. `gpt-image-2` costs more at the top: OpenAI publishes $0.211 for `high`
at 1024x1024. The CLI replaces its estimate with the billed figure read back from each call's
token usage. The full workflow and CLI reference is in
[`skills/imager/SKILL.md`](skills/imager/SKILL.md).

### No seed, no thinking mode

Neither parameter exists in the OpenAI image API - both are absent from `CreateImageRequest` and
`CreateImageEditRequest` in `openai/openai-openapi`. Earlier versions of this skill accepted
`--seed` and `--thinking`, logged them, and sent neither; `--thinking` also inflated the cost
estimate. Both now fail with an error saying what to use instead: the prompt and `--reference`
for consistency, `--quality` or `--model sunburst` for complex layouts.

## What this will not do

**Spend money without telling you first.** Every path prices the run before making it. `-y` exists to skip the confirmation in a batch, and it is the only way to turn that off.

**Store your API key.** It is read from the environment on each run. The skill writes config, a generation log and a last-run record to `~/.dbhq/imager/`, and none of the three has a field for a key.

## Development

Want to hack on the skill or run it from source with live edits? See [`docs/dev-setup.md`](docs/dev-setup.md).

[`CONTRIBUTING.md`](CONTRIBUTING.md) covers working on it, and [`AGENTS.md`](AGENTS.md) is for an AI agent doing so. The skill itself is [`skills/imager/SKILL.md`](skills/imager/SKILL.md).

## Acknowledgements

Adapted from [glebis/claude-skills](https://github.com/glebis/claude-skills/tree/main/gpt-image-2) (MIT), which is where the preset catalogue and the draft-then-final shape come from.

## Also from DBHQ

Every DBHQ agent skill is free, open source and installable from the same
marketplace, and all of them are documented at
**[skills.dbhq.uk](https://skills.dbhq.uk)**. The marketplace itself is
[dbhq-uk/marketplace](https://github.com/dbhq-uk/marketplace) - one
`/plugin marketplace add` and every one of them is available.

| Skill | What it does |
|---|---|
| [outlook](https://skills.dbhq.uk/outlook/) | Microsoft 365 mail and calendar, from the terminal |
| [trello](https://skills.dbhq.uk/trello/) | Your boards, run from your agent |
| [legwork](https://skills.dbhq.uk/legwork/) | Research that settles a decision, and says when it cannot |
| [dovetail](https://skills.dbhq.uk/dovetail/) | Checks whether your repository still agrees with itself |
| [verve](https://skills.dbhq.uk/verve/) | Strips AI tells from prose and puts a voice back |
| [vela](https://skills.dbhq.uk/vela/) | Compiler-exact code search, in any language you index |
| [garmin](https://skills.dbhq.uk/garmin/) | Your Garmin data, answered in the terminal |
| [gitview](https://skills.dbhq.uk/gitview/) | Which branches are finished, and safe to delete |
| [atlassian](https://skills.dbhq.uk/atlassian/) | Jira issues and Confluence pages |
| [pennyblack](https://skills.dbhq.uk/pennyblack/) | A physical letter, posted from the terminal |
| [buildwork](https://skills.dbhq.uk/buildwork/) | Your open issues, run as parallel agents |
| [deskwork](https://skills.dbhq.uk/deskwork/) | What an agent noticed, tracked as real work |
| [groupwork](https://skills.dbhq.uk/groupwork/) | A second agent on the work, adversary or partner |
| [headwork](https://skills.dbhq.uk/headwork/) | One decision at a time, with a recommendation |

Plus [heliograph](https://skills.dbhq.uk/heliograph/), for a machine you cannot log into.

## Licence

[MIT](LICENSE) © 2026 DBHQ Consulting Ltd
