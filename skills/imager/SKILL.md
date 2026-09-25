---
name: imager
description: Generate and edit images using OpenAI's GPT Image API (gpt-image-2.5-flare, gpt-image-2.5-sunburst, gpt-image-2). Interactive skill that guides users through image creation with style presets, a cost-aware draft/final workflow, transparent backgrounds, masked inpainting, carousels and photo editing. This skill should be used when the user requests image generation via OpenAI/GPT Image, wants to create social media carousels, edit photos into artistic styles, needs a logo or asset on a transparent background, or needs images with readable text (infographics, diagrams, posters).
---

# GPT Image - Interactive Image Generation

Generate and edit images via OpenAI's GPT Image API with an interactive, guided workflow.

## Step 0: Is this going into a set that already exists?

**Do this before anything else, every time.**

A directory of images is a SET, and a set has a model and a quality tier somebody already chose.
Adding to it at a different tier is wrong twice over: the new images do not match, and you pay
for a difference nobody can see. Adding to it on a different model is worse - the look changes.

```bash
$PY $GEN set-check <dir-or-file>      # what did this set use last time?
```

The CLI does this for you: if the output path lands in a directory the history has seen, it
adopts that model and quality and says so. Pass `--model` or `--quality` to override and it
warns instead.

A set is a folder in a repository, not an absolute path. Inside git, the history records the
repository and the folder's path from its top level, so the same `images/` folder in another
worktree of the repository is the same set. Outside git, the absolute path is all there is.

**More than a few images? Use `batch` (below). Do not write your own runner.** A script that
calls the API itself gets no set check, no daily total and no record of what it spent.

**This is not hypothetical.** On 2026-08-21 all three tiers were run side by side for a note-art
set - the history still holds them as `q-low`, `q-medium` and `q-high` - and **low** was chosen,
because the images ship at 300x400 and the downscale throws away everything the higher tiers
buy. On 2026-08-23 a new batch for the same set went out at **high**, purely because high was
the default, at 35x the price for no visible difference. The answer was sitting in
`history.jsonl` the whole time and nobody looked.

## Models

| Model | When |
|-------|------|
| `gpt-image-2.5-flare` **(default)** | Everything, unless a reason says otherwise. About half the latency of gpt-image-2 and fewer output tokens for the same tier, so the same picture costs less. |
| `gpt-image-2.5-sunburst` | Fine detail, dense text, precision edits, multi-turn editing where earlier edits must survive later ones. Slower, same token rates. |
| `gpt-image-2` | Adding to a set that was generated on it, or a Batch API run - the 50% batch discount covers gpt-image-2 and **not** the 2.5 models. Not deprecated. |

Aliases work: `--model flare`, `--model sunburst`, `--model 2`.

`gpt-image-1`, `gpt-image-1.5`, `gpt-image-1-mini` and `chatgpt-image-latest` leave the API on
**1 December 2026**. Do not reach for them.

## Quality

`low`, `medium`, `high` on every model; `xhigh` and `max` on the 2.5 models only. The default is
**low**, deliberately - this skill's own flow is "draft first", and a default of high contradicts
that on every non-interactive call.

Raise quality for complex layouts and dense text. There is no separate "thinking" control - see
"Two flags that never existed" below.

## Interactive Flow

When the user invokes this skill, guide them through these steps using AskUserQuestion. Do not
skip steps - the interactive flow is the core experience.

### Step 1: What are we making?

Ask the user what they want to create. Offer these options:

- **Single image** - one image from a text prompt
- **Photo edit** - transform an existing photo into a style
- **Carousel** - 5-10 cohesive slides for LinkedIn/Instagram
- **Variants** - multiple versions of the same concept
- **Quick generate** - skip questions, just run the prompt

If the user already provided a clear prompt (e.g. "generate an editorial image of a rocket"),
skip to Step 3.

### Step 2: Style selection

Show the user available presets grouped by category. Read `presets.json` and present them:

**Visual styles** (no text in image):
editorial, blueprint, ink, risograph, wireframe, constellation, brutalist, grain, nordic,
bauhaus

**Text-heavy** (leverages GPT Image text rendering):
infographic, slide, diagram, poster, menu, manga

**Community favourites:**
trading-card, pixar, app-mockup, isometric, action-figure, cinematic, panorama

**Social:** flat-social, linkedin-hero, social-bold, social-split

**Custom** - user describes their own style

Ask: "Which style? Or describe your own."

### Step 3: Platform & sizing

Ask where this will be used, then pass `--platform`. The platform now drives the **generation**
aspect: the image is generated at the platform's own shape (snapped up to the nearest legal
size) and scaled down to fit, rather than generated square and cropped. A crop throws away
pixels that were paid for and re-frames the picture after the model composed it.

- YouTube thumbnail (1280x720)
- Instagram / LinkedIn square (1080x1080)
- Slides/presentation (1920x1080)
- Blog hero (1200x630)
- X/Twitter (1600x900)
- Story (1080x1920)
- Pinterest (1000x1500)
- Custom `--size WIDTHxHEIGHT`
- No resize (API default)

Custom sizes must satisfy the API: both edges multiples of 16, aspect between 1:3 and 3:1,
longest edge 3840px or less, total pixels between 655,360 and 8,294,400. The CLI checks this
before spending anything and tells you the nearest legal size.

### Step 4: Draft first, then final

**Always generate a draft first** unless the user says "skip draft".

1. Generate with `--draft` (quality=low, ~$0.006/image), with the same `--platform` or `--size`
   the final will use. The draft is generated at that aspect, so it is composed as the final will be
2. Show the image to the user (see "Showing the result" below)
3. Ask: "Like this direction? I can: (a) generate final quality, (b) adjust the prompt,
   (c) try a different style, (d) regenerate"
4. If approved, generate the final, and choose its tier this way:
   - **The set has a tier** (Step 0 found one): pass no `--quality`. The CLI adopts the set's
     tier and prints the price per image. Passing `--quality` here overrides the set, which
     is the exact failure Step 0 exists to stop.
   - **The set is new:** agree the tier with the user before generating, then pass it. Low is
     right unless the image is displayed large; say what each tier costs

This draft-then-final flow saves roughly 97% on the iteration that finding a direction takes.

**Composition will move between draft and final.** There is no seed. If the composition of the
draft is the thing being approved, carry it forward in the prompt (name the framing, the camera,
the placement) or pass the approved draft as `--reference` so the final is anchored to it.

### Step 5: Show result and offer next actions

After generation, always:

1. Show the image using the Read tool
2. **Serve it for a proper look.** `open`/`xdg-open` does nothing on a headless box. If the repo
   has a preview script (DBHQ: `./scripts/preview.sh <path>`), run it and post the clickable URL.
   Otherwise fall back to `xdg-open`/`open`.
3. Report the cost the CLI printed - it is the billed figure, not an estimate
4. Offer: "Want to (a) generate variants, (b) edit this further, (c) use as reference for more
   images, (d) done?"

## Carousel Workflow

When the user wants a carousel (5-10 slides):

### 1. Story arc

Ask: "What's the story? Give me the key message and I'll draft a 10-slide arc."

Then propose a slide-by-slide plan like:

```
Slide 1: [Cover] - hook headline + hero image
Slide 2: [Problem] - bold statement
Slide 3: [Context] - illustration + explanation
...
Slide 10: [CTA] - call to action with URL
```

Ask the user to approve or modify the plan.

### 2. Style consistency

Consistency across a carousel is carried by the **prompt and by reference images**, not by a
seed. For every slide:

- one preset, one model, one quality tier, one size for the whole deck
- repeat the palette, typography description and layout grid verbatim in every prompt
- pass slide 1, once approved, as `--reference` for the rest
- include pagination dots in prompts (e.g. "10 small dots at the bottom, the third highlighted
  orange")
- for a recurring character, repeat the 5-tuple every time: age, appearance, hairstyle,
  distinctive features, clothing

### 3. Draft batch

Generate all slides as drafts first (~$0.006 each). Show them as a contact sheet or one by one.
Ask which to regenerate or adjust.

### 4. Final batch

Only generate finals for approved slides. Offer to generate all at once with `-y`.

## Photo Edit Workflow

1. Ask for the source image (file path or clipboard)
2. For clipboard: save it to a temp file first (`osascript` on macOS,
   `xclip -selection clipboard -t image/png -o > /tmp/clip.png` on Linux)
3. Show available styles and ask which to try
4. Generate a draft edit first
5. Show result, ask if they want adjustments
6. Generate final when approved

Use `--edit <path>`. For changing one region and leaving the rest alone, add `--mask <mask.png>`:
a PNG the same size as the source whose **transparent** areas mark what to replace. Masking is
prompt-guided, so still say in words what should change and what must not.

For preserving a subject across a new scene, `--reference` (repeatable, up to 16 images) is the
right tool rather than `--edit`.

## Transparent backgrounds

`--background transparent` with `--output-format png` or `webp`. This is the correct route for
logos, icons, stickers and any asset that has to sit on an arbitrary ground - do not generate on
white and key it out afterwards. Say so in the prompt too ("isolated on a transparent
background, no shadow, no halo, no backdrop"), because the parameter sets the alpha channel and
the prompt stops the model painting a background into it.

If `--background transparent` is combined with `--output-format jpeg`, the CLI switches to png -
JPEG has no alpha channel.

## Output formats

`--output-format png` (default), `jpeg` or `webp`, with `--output-compression 0-100` on the
latter two. WebP is the right default for anything going on a website. The file extension
follows the format automatically.

## Output files

A run never replaces an existing file. If `out.png` exists, it writes `out-2.png` (then `-3`,
and so on) and says so; a multi-image run moves aside as a whole. Pass `--overwrite` only when
replacing the file is the point. Report the path the CLI printed, not the one you asked for.

No JSON sidecar is written by default: `history.jsonl` already holds the record. `--sidecar`
writes `<image>.json` beside the image, and never over an existing file.

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
`reference` (a path or a list of paths), `mask`, `background` and `output_format`. Any other key
is refused. `--model` and `--quality` apply to the whole file. Relative paths are read from the
directory you run it in. Each row makes one image. These are ordinary requests at the normal
price, not OpenAI's Batch API.

- The whole file is checked before anything is priced. A bad row stops the run, and every
  problem is listed.
- One set check per folder. The set's model and tier apply, as for a single run.
- One total and one confirmation, at $0.50 or more. `-y` skips the confirmation, not `daily_cap`.
- A row whose output already exists is skipped, so running the file again resumes it.
- `--concurrency N` (default 4, at most 16) sends that many requests at once. Every image gets
  its own history row.
- A failed row does not stop the others. Five failures in a row stop the run.

Exit codes: 0 done, 1 an error or a failed row, 3 cancelled at the confirmation, 4 over
`daily_cap`.

## Cost Awareness

Always communicate costs before generating.

The CLI prices a run the way OpenAI bills it: prompt text in, input images in and image out,
each at its own token rate. Per image at 1024x1024 on `gpt-image-2.5-flare` (sunburst spends
the same tokens):

| Quality | Per image | 10 slides | 2,000 images |
|---------|-----------|-----------|--------------|
| low **(default)** | $0.006 | $0.06 | **$12** |
| high | $0.053 | $0.53 | $105 |
| xhigh | $0.100 | $1.00 | $200 |

`gpt-image-2` spends far more output tokens at the top: OpenAI publishes $0.006 low, $0.053
medium and **$0.211 high** at 1024x1024.

Three things that table hides:

- **Input images cost money.** Each `--edit` image, `--reference` and `--mask` adds about
  $0.013, once for every output image. A flare draft with one reference is about $0.020, not
  $0.006.
- **Non-square is cheaper.** Output tokens fall with the aspect ratio: 1536x864 is about 0.63x
  the square price and 1792x608 about 0.35x. `size=auto` is priced at 1.2x square, because the
  API sometimes picks a size above square.
- **The estimate names its basis.** `measured` is this machine's history: three or more runs at
  the same model, quality, size and kind (edit or generation). `calibrated` is the token table,
  built from real runs. `published` is OpenAI's gpt-image-2 table. `upper bound` means nothing
  has been measured yet: 2.5 `medium` and `max`, `auto` quality, and sizes above about 2.1
  megapixels. Those figures are deliberately high. Every run also reads `usage` back and
  records what was actually billed.

### The Batch API

`gpt-image-2` is eligible for the Batch API's **50% discount**; the 2.5 models are not. For a
run of thousands where latency does not matter, that is the difference between $422 and $211.
The CLI prices it for you (`--estimate --model gpt-image-2 --n 4`) but does not submit batch
jobs - build that separately if a run is large enough to want it.

### The guards, and the hole between them

**Per call:** the script prompts when a single invocation costs $0.50 or more.

**Per day:** it warns once cumulative spend through this skill passes $5.

**Per day, a hard limit:** set `daily_cap: 20` (dollars) in `~/.dbhq/imager/config.yaml`. A run
that would take today's spend over it stops before anything is sent, and exits 4. `-y` does not
bypass it, and neither does `batch`. There is no cap until you set one.

The per-call gate alone is useless against a batch. `--n` is capped at 10, so anything larger is
a loop of separate calls - and 2,000 images at $0.21 each is $420 while every single call is
$0.21, comfortably under the threshold. That is the shape of every batch job.

**The hole:** the daily figures only count spend that went through this skill. A script that
calls the OpenAI endpoint itself is invisible to both. Use `batch` rather than a runner of your
own.

## Two flags that never existed

`--seed` and `--thinking` were in this skill until 2026-09-12. **Neither parameter exists in the
OpenAI image API** - both are absent from `CreateImageRequest` and `CreateImageEditRequest` in
`openai/openai-openapi` and from the whole image generation guide. The old code accepted them,
logged them, and sent neither. `--thinking` also multiplied the cost estimate, so the
confirmation gate fired on numbers describing a request nobody had ever sent.

Both now fail with an error rather than being silently dropped, because a script still passing
`--seed` is a script whose author believes composition is locked between draft and final.

- Instead of `--seed`: carry consistency in the prompt, and use `--reference`.
- Instead of `--thinking`: raise `--quality`, or use `--model sunburst`.

## Prompt Engineering Tips

1. **Name the deliverable first.** "A 3:2 conference poster for..." beats "a poster of...".
2. **Structure**: Scene -> Subject -> Detail -> Lighting -> Constraint
3. **Front-load the subject**: put the main thing first
4. **For text in images**: quote the exact string and give it a hierarchy -
   `'headline reading "Hello World", subtitle beneath it reading "..."'`. Never "add some text".
5. **Name what must NOT change** on an edit. On an editing model those sentences are
   load-bearing, not padding.
6. **Reference binding**: when passing `--reference`, say what each reference controls and which
   of its features must carry through.
7. **Character consistency**: repeat the 5-tuple - age + appearance + hairstyle + distinctive
   features + clothing.
8. **Style tags at end**: append tags like `editorial-magazine`, `studio-product` to converge a
   batch.

## CLI Reference

Run it with the system `python3` (3.9 or later). It uses the standard library only, so there is
nothing to install first.

```bash
PY=python3
GEN=${CLAUDE_SKILL_DIR}/scripts/imager.py

# Basic generation (gpt-image-2.5-flare, quality low)
$PY $GEN "prompt" output.png

# Pick a model
$PY $GEN --model sunburst --quality xhigh "dense infographic" out.png

# With preset and platform (generates at the platform's aspect, then fits down)
$PY $GEN --preset editorial --platform square "subject" out.png

# Draft mode (~$0.006/image)
$PY $GEN --draft "prompt" out.png

# Transparent-background asset
$PY $GEN --background transparent --output-format png \
  "a minimal line-art compass rose, isolated, no shadow" logo.png

# WebP for the website, 80% compression
$PY $GEN --output-format webp --output-compression 80 "hero image" hero.webp

# Custom size (multiples of 16, 1:3 to 3:1)
$PY $GEN --size 1536x864 "wide banner" banner.png

# Edit an existing photo
$PY $GEN --edit photo.png "transform into constellation style" out.png

# Replace one region, leave the rest alone
$PY $GEN --edit room.png --mask mask.png "put a flamingo in the pool" out.png

# Keep a subject across a new scene
$PY $GEN --reference face.png --reference jacket.png "the same person on a rooftop" out.png

# Variants with contact sheet
$PY $GEN --n 4 --preset ink "mountain" out.png

# Cost estimate
$PY $GEN --estimate --n 10 --quality high "batch test"

# Skip confirmation
$PY $GEN -y --n 10 "batch" out.png

# Replace an existing file instead of writing out-2.png
$PY $GEN --overwrite "prompt" out.png

# Dry run (show prompt without API call)
$PY $GEN --dry-run --preset editorial "test" out.png

# What did this set use?
$PY $GEN set-check ./assets/notes/

# Many images: one set check, one price, one confirmation (see "Batch runs")
$PY $GEN batch runs.jsonl --dry-run
$PY $GEN batch runs.jsonl --concurrency 4

# Which models, presets, platforms?
$PY $GEN list-models
$PY $GEN list-presets
$PY $GEN list-platforms
```

Requires `OPENAI_API_KEY` in the environment.

## Handling a refusal

A moderation block comes back as `error.code = "moderation_blocked"` with a
`moderation_details` object naming the stage (`input` or `output`) and coarse categories. The
CLI reports both and does **not** retry - a refusal retried four times is the same refusal, four
times slower. Change the prompt or the input images. If the subject is legitimate and it was a
generation, `--moderation low` is the less restrictive filter. Edits and `--reference` runs have
no moderation setting, and the CLI refuses `--moderation` on them.

## Files

- `scripts/imager.py` - main CLI (Python 3.9+, standard library only)
- `scripts/setup.sh` - optional check of python3, the API key and ImageMagick; installs nothing
- `presets.json` - 27 style presets (visual + text-heavy + community + social)
- `platforms.json` - 8 platform sizing presets
- `references/api_reference.md` - full API documentation
- `~/.dbhq/imager/config.yaml` - user defaults, and the optional `daily_cap`. Flat `key: value`
  lines only
- `~/.dbhq/imager/history.jsonl` - generation log, including billed cost and token usage. A
  row is written before each request and completed after it, so a run killed mid-request
  still shows in `history` as pending and still counts, at its estimate, in the day's total
- `~/.dbhq/imager/last.json` - last run (for `again`)

`GPT_IMAGE_HOME` relocates all three, which is how the test suite runs against an empty history.
