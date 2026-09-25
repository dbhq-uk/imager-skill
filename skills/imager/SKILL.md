---
name: imager
description: Generate and edit images using OpenAI's GPT Image API (gpt-image-2.5-flare, gpt-image-2.5-sunburst, gpt-image-2). Interactive skill that guides users through image creation with style presets, a cost-aware draft/final workflow, transparent backgrounds, masked inpainting, carousels, batches and photo editing. This skill should be used when the user requests image generation via OpenAI/GPT Image, wants to create social media carousels, edit photos into artistic styles, needs a raster asset on a transparent background, or needs images with readable text (infographics, posters, slides). Not for vector marks, icons or exact diagrams that should be drawn as SVG or code.
---

# imager - guided image generation

Generate and edit images through OpenAI's GPT Image API: draft cheaply, show the user, then pay
for the final.

```bash
PY=python3
GEN=${CLAUDE_SKILL_DIR}/scripts/imager.py
```

Python 3.9 or later, standard library only: nothing to install. It needs `OPENAI_API_KEY` in the
environment. ImageMagick is optional (platform fitting, contact sheets).

## When not to use

- **Vector marks, logos and icons** that must stay sharp at every size. Draw them as SVG.
- **Exact diagrams:** flowcharts, architecture, charts. Use Mermaid, SVG or a plotting library.
  The model draws plausible diagrams, not correct ones.
- **An asset the repository already generates from code.** Change the generator.
- Under Codex, the built-in `image_gen` tool needs no API key, but has no file paths, masks, set
  check or cost record. Use this skill when those matter.

## Step 0: Is this going into a set that already exists?

**Do this before anything else, every time.**

A folder of images is a set, with a model and tier somebody already chose. Adding at another tier
gives images that do not match, at a price nobody needed to pay. Another model changes the look.

```bash
$PY $GEN set-check <dir-or-file>      # what did this set use last time?
```

The CLI also does this itself: when the output lands in a folder the history knows, it adopts
that model and tier and says so. `--model` or `--quality` overrides it, with a warning.
Inside git a set is the repository plus the folder's path, so another worktree is the same set.

**More than a few images? Use `batch` (below). Do not write your own runner.** A script that
calls the API itself gets no set check, no daily total and no record of what it spent.

## Models and quality

| Model | When |
|-------|------|
| `gpt-image-2.5-flare` **(default)** | Everything, unless a reason says otherwise. Faster and cheaper per tier than gpt-image-2. |
| `gpt-image-2.5-sunburst` | Fine detail, dense text, precision and multi-turn edits. Slower. |
| `gpt-image-2` | A set made on it, or a Batch API run (the 50% discount does not cover 2.5). |

Aliases: `flare`, `sunburst`, `2`. Do not use retiring models: `gpt-image-1` leaves the API on
**23 October 2026**; `gpt-image-1.5`, `gpt-image-1-mini` and `chatgpt-image-latest` on
**1 December 2026**.

Quality: `low`, `medium`, `high`, plus `xhigh` and `max` on the 2.5 models. The default is
**low**, because the flow is draft first. There is no seed and no thinking control.

## Interactive flow

Ask the user one question at a time, and wait for each answer. Do not skip steps.

### Step 1: What are we making?

A single image, a photo edit, a carousel (5-10 slides), variants, or a quick generate with no
questions. If the user already gave a clear prompt, go to Step 3.

### Step 2: Style

Offer the presets (`$PY $GEN list-presets`), grouped:

- **Visual, no text:** editorial, blueprint, ink, risograph, wireframe, constellation,
  brutalist, grain, nordic, bauhaus
- **Text-heavy:** infographic, slide, diagram, poster, menu, manga
- **Community:** trading-card, pixar, app-mockup, isometric, action-figure, cinematic, panorama
- **Social:** flat-social, linkedin-hero, social-bold, social-split
- **Custom:** the user describes their own

### Step 3: Where will it be used?

Pass `--platform`: `youtube`, `youtube-short`, `slides`, `blog`, `x`, `square`, `story` or
`pinterest` (`list-platforms` shows the sizes). The image is generated at that shape and scaled
down, not cropped. Or pass `--size WIDTHxHEIGHT`; the CLI names the nearest legal size if needed.

### Step 4: Draft first, then final

**Always generate a draft first** unless the user says "skip draft".

1. Run `--dry-run` and read the assembled prompt. A preset brings its own background, palette
   and often "no text". If the subject asks for something the preset forbids, such as a headline
   on a "no text" preset, fix the prompt or change the preset before spending.
2. Generate with `--draft` (about $0.006) and the `--platform` or `--size` the final will use, so
   the draft is composed as the final will be.
3. Show it (Step 5). Ask: final, adjust the prompt, another style, or regenerate?
4. If approved, generate the final, and choose its tier this way:
   - **The set has a tier** (Step 0 found one): pass no `--quality`. The CLI adopts the set's
     tier and prints the price. Passing `--quality` overrides the set, the failure Step 0 stops.
   - **The set is new:** agree the tier with the user first, then pass it. Low is right unless
     the image is shown large. Say what each tier costs.

**Composition moves between draft and final**, because there is no seed. To keep it, describe
it in the prompt (framing, camera, placement), or pass the approved draft as `--reference`.

### Step 5: Show the result

1. Show the image with the Read tool.
2. `open` and `xdg-open` do nothing on a headless machine. If the repository has a preview
   script, run it and post the clickable URL.
3. Report the cost the CLI printed. It is the billed figure.
4. Offer: variants, edit further, use as a reference, or done.

## Timeouts

A generation can take two minutes or more at high tiers, large sizes or with several
references, and many agent shells stop a command at 120 seconds. Run finals, `xhigh`, `max`,
`--n` above 1 and every `batch` with a longer timeout (10 minutes) or in the background.

A killed run stays in the history as `pending` and counts at its estimate, because it may have
been billed. Check `$PY $GEN history -n 5` before running it again.

## Carousels

1. **Story arc.** Ask for the key message, propose a slide-by-slide plan, get it approved.
2. **Consistency** comes from the prompt and references. One preset, model, tier and size.
   Repeat the palette, type and grid wording in every prompt. Pass the approved slide 1 as
   `--reference`. Ask for pagination dots. For a recurring character, repeat age, appearance,
   hairstyle, features and clothing every time.
3. **Drafts** of every slide, then **finals** of the approved ones as a `batch`.

## Photo edits

A clipboard image must be saved to a file first (`osascript` on macOS, `xclip -selection
clipboard -t image/png -o > /tmp/clip.png` on Linux). Then draft, show and final, as in Step 4.

- `--edit <path>` edits the photo. The CLI sends `size=auto` unless `--size` or `--platform` is
  set, because the edit endpoint otherwise returns a square.
- `--mask <mask.png>`: a PNG the size of the source whose transparent areas mark what to
  replace. It is guidance, so also say what changes and what must not.
- `--reference` (up to 16) carries a subject or a look into a new scene.

## Assets and formats

**Transparent:** `--background transparent`, png or webp. Say it in the prompt too ("isolated
on a transparent background, no shadow, no halo"): the flag sets alpha, the words stop the model
painting a background. With jpeg the CLI switches to png.

**Formats:** `--output-format png` (default), `jpeg` or `webp`, with `--output-compression
0-100` on the last two. Use webp for the web.

**Files:** a run never replaces a file. If `out.png` exists it writes `out-2.png` and says so.
`--overwrite` replaces on purpose. Report the path the CLI printed. `--sidecar` also writes
`<image>.json`.

## Batch runs

For more than a handful of images, write one JSON object per line and run the file:

```bash
$PY $GEN batch runs.jsonl --dry-run    # the plan for each folder, and one total
$PY $GEN batch runs.jsonl              # one confirmation for the whole file
```

```json
{"prompt": "a lighthouse at dusk", "output": "notes/01.png", "preset": "ink"}
{"prompt": "a harbour crane", "output": "notes/02.png", "platform": "story"}
```

`prompt` and `output` are required. A row may also carry `preset`, `platform`, `size`, `edit`,
`reference`, `mask`, `background` and `output_format`; any other key is refused. `--model` and
`--quality` apply to the whole file. One image per row, at the normal price (not the Batch API).

- The file is checked whole before anything is priced, with every problem listed.
- One set check per folder, one total, one confirmation. `-y` does not skip `daily_cap`.
- Outputs that exist are skipped, so running the file again resumes it.
- `--concurrency N` (default 4, at most 16). A failed row does not stop the others; five in a
  row stop the run.

## Cost

Say what a run costs before running it. The CLI prices prompt text, input images and output
image, each at its token rate. Per image at 1024x1024 on flare or sunburst:

| Quality | Per image | 10 slides | 2,000 images |
|---------|-----------|-----------|--------------|
| low **(default)** | $0.006 | $0.06 | **$12** |
| high | $0.053 | $0.53 | $105 |
| xhigh | $0.100 | $1.00 | $200 |

`gpt-image-2` high is **$0.211**. Each input image adds about $0.013 per output. Non-square is
cheaper (1536x864 is about 0.63x); `size=auto` is priced at 1.2x square. The estimate names its
basis: `measured`, `calibrated`, `published` or `upper bound` (deliberately high). Every run
records what was actually billed.

**Guards.** A run of $0.50 or more asks first. Past $5 in a day, every run warns. `daily_cap: 20`
in `~/.dbhq/imager/config.yaml` stops any run that would pass it, `-y` or not, and exits 4.
They only see spend that went through this CLI.

## Prompt tips

1. Name the deliverable first: "A 3:2 conference poster for...", not "a poster of...".
2. Subject first, then detail, lighting and constraints.
3. For text in the image, quote the exact string and give it a hierarchy. Never "add some text".
4. On an edit, say what must not change. With `--reference`, say what each one controls.
5. End with style tags such as `editorial-magazine` to pull a set together.

## CLI reference

```bash
$PY $GEN --draft --preset editorial --platform square "subject" out.png
$PY $GEN --model sunburst --quality xhigh "dense infographic" out.png
$PY $GEN --n 4 --preset ink "mountain" out.png        # variants and a contact sheet
$PY $GEN --estimate --n 10 --quality high "test"      # price only
$PY $GEN -y "prompt" out.png                          # skip the confirmation
$PY $GEN --project launch "prompt" out.png            # tag it for history --project
$PY $GEN again                                        # the last run, same inputs
$PY $GEN history -n 10
```

`--project` with no output path files the image under `~/imager/outputs/<project>/`. With no
path and no project, a run writes `imager-<timestamp>.png` here.

Exit codes: 0 done, 1 an error (a missing input image is caught before pricing) or a failed
batch row, 2 a bad or removed flag, 3 cancelled at the confirmation, 4 over `daily_cap`. Treat
only 0 as an image made.

## Refusals

A moderation block names its stage and categories. The CLI does not retry: change the prompt or
the inputs. For a legitimate generation, `--moderation low` is less strict. Edits have none.

## Files

`scripts/imager.py` (the CLI), `presets.json`, `platforms.json`, `references/api_reference.md`.
In `~/.dbhq/imager/` (or `GPT_IMAGE_HOME`): `config.yaml` (flat `key: value` defaults),
`history.jsonl` (every run and its billed cost, written before the request and completed after)
and `last.json` (for `again`).
