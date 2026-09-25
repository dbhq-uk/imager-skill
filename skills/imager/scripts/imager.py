#!/usr/bin/env python3
"""imager - generate and edit images with OpenAI's GPT Image models

A CLI wrapper around OpenAI's GPT Image models: gpt-image-2.5-flare (default),
gpt-image-2.5-sunburst and gpt-image-2.

Supports style presets, platform-aware sizing, variants, editing, masked
inpainting, transparent backgrounds, output formats, and cost controls that
price from the API's own token usage.

Usage:
    imager.py [flags] "prompt" [output.png]
    imager.py init                    # onboarding wizard
    imager.py again                   # regenerate last
    imager.py history [-n 10]         # show history
    imager.py set-check <dir>         # what did this set use?
    imager.py batch runs.jsonl        # many images, one price, one confirmation
    imager.py list-models
    imager.py list-presets
    imager.py list-platforms
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any, NoReturn

# STANDARD LIBRARY ONLY. /plugin install and `npx skills add` copy the skill
# directory and run nothing, so a dependency that needs an install step is a
# skill that cannot start. PyYAML was the only one, for three small files: the
# two catalogues are JSON now, and config.yaml is read by parse_config below.
SKILL_DIR = Path(__file__).resolve().parent.parent
PRESETS_FILE = SKILL_DIR / "presets.json"
PLATFORMS_FILE = SKILL_DIR / "platforms.json"


def _migrate_legacy_settings() -> None:
    """One-time migrations of the settings directory, oldest source last.

    Two moves have happened. The skill was renamed from gpt-image-2 to imager on
    13 Sep 2026, and before that its settings moved out of ~/.config. Both are
    done here, on first run, guarded on the destination not existing - so an
    existing install keeps working without its owner touching anything, and a
    machine that has already migrated does nothing.

    Order matters: the ~/.dbhq/gpt-image-2 source is checked first, because a
    machine that migrated out of ~/.config already has settings there and that
    is the directory carrying the real history.
    """
    new_dir = Path.home() / ".dbhq" / "imager"
    if new_dir.exists():
        return
    for old_dir in (Path.home() / ".dbhq" / "gpt-image-2", Path.home() / ".config" / "gpt-image-2"):
        if not old_dir.is_dir():
            continue
        new_dir.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(new_dir.parent, 0o700)
        old_dir.rename(new_dir)
        os.chmod(new_dir, 0o700)
        return


# GPT_IMAGE_HOME relocates config, history and the last-run record. It exists so
# the test suite can run against an empty history: cost estimates and the
# set-check both read history.jsonl now, so a suite that used the real one would
# pass or fail depending on what the machine's owner had generated that week.
_ENV_HOME = os.environ.get("GPT_IMAGE_HOME")
if not _ENV_HOME:
    _migrate_legacy_settings()

CONFIG_DIR = Path(_ENV_HOME) if _ENV_HOME else Path.home() / ".dbhq" / "imager"
CONFIG_FILE = CONFIG_DIR / "config.yaml"
HISTORY_FILE = CONFIG_DIR / "history.jsonl"
LAST_RUN_FILE = CONFIG_DIR / "last.json"


def ensure_config_dir() -> None:
    """Create the settings directory owner-only, as every DBHQ skill does."""
    CONFIG_DIR.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    CONFIG_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)


# ---------- Models ----------

# gpt-image-2.5-flare is the default: same token rates as gpt-image-2, about
# half the latency, and it spends fewer output tokens for the same quality
# tier, so the same picture costs less. Sunburst is the precision-editing
# sibling - slower, better on fine detail, dense text and multi-turn edits.
#
# gpt-image-2 stays selectable and is NOT deprecated: it is the replacement
# target for gpt-image-1, 1.5 and 1-mini, which leave the API on 1 Dec 2026.
# Keep it for adding to a set that was generated on it (see set-check), and for
# Batch API runs - the 50% batch discount covers gpt-image-2 and not the 2.5s.

DEFAULT_MODEL = "gpt-image-2.5-flare"

MODELS = {
    "gpt-image-2.5-flare": {
        "aliases": ("flare", "2.5-flare"),
        "qualities": ("low", "medium", "high", "xhigh", "max", "auto"),
        "batch_discount": False,
        "description": "Fast everyday generation. The default.",
    },
    "gpt-image-2.5-sunburst": {
        "aliases": ("sunburst", "2.5-sunburst"),
        "qualities": ("low", "medium", "high", "xhigh", "max", "auto"),
        "batch_discount": False,
        "description": "Precision editing, fine detail and dense text. Slower.",
    },
    "gpt-image-2": {
        "aliases": ("2", "image-2"),
        "qualities": ("low", "medium", "high", "auto"),
        "batch_discount": True,
        "description": "Previous generation. Eligible for the 50% Batch API discount.",
    },
}

QUALITY_CHOICES = ("low", "medium", "high", "xhigh", "max", "auto")

PROVIDERS = {
    "openai": {
        "url": "https://api.openai.com/v1/images/generations",
        "edit_url": "https://api.openai.com/v1/images/edits",
        "key_env": "OPENAI_API_KEY",
    },
}

# Providers that were offered and are not any more. They fail with a reason
# rather than a bare "invalid choice", for the same reason RETIRED_FLAGS does:
# whoever still names one believes it works.
RETIRED_PROVIDERS = {
    "openrouter": (
        "OpenRouter support has been removed. It posted to routes and model IDs that "
        "OpenRouter's image API does not use, so every edit failed, and it read a usage "
        "block OpenRouter does not return, so a paid image would have been recorded as "
        "costing $0.\n"
        "       Set OPENAI_API_KEY and drop --provider. If config.yaml says "
        "provider: openrouter, remove that line."
    ),
}


def check_provider(name: str) -> str:
    """The provider to use, or a clear exit if it is retired or unknown."""
    if name in PROVIDERS:
        return name
    if name in RETIRED_PROVIDERS:
        print(f"Error: {RETIRED_PROVIDERS[name]}", file=sys.stderr)
        sys.exit(2)
    print(f"Error: unknown provider '{name}'. Known providers: {', '.join(PROVIDERS)}", file=sys.stderr)
    sys.exit(1)


def normalise_model(name: str | None) -> str:
    if not name:
        return DEFAULT_MODEL
    if name in MODELS:
        return name
    for canonical, info in MODELS.items():
        if name in info["aliases"]:
            return canonical
    known = ", ".join(MODELS)
    print(f"Error: unknown model '{name}'. Known models: {known}", file=sys.stderr)
    sys.exit(1)


# ---------- Cost ----------

# Token prices per 1,000,000 tokens, standard tier, from the OpenAI pricing
# page. All three models currently share these rates; they are kept per-model
# because they have not always been equal and will not always stay equal.
TOKEN_PRICES = {
    "gpt-image-2.5-flare": {"text_in": 5.00, "image_in": 8.00, "image_out": 30.00},
    "gpt-image-2.5-sunburst": {"text_in": 5.00, "image_in": 8.00, "image_out": 30.00},
    "gpt-image-2": {"text_in": 5.00, "image_in": 8.00, "image_out": 30.00},
}

# The Batch API halves every rate, and only for models flagged batch_discount.
BATCH_DISCOUNT = 0.5

# THE ESTIMATE IS BUILT FROM TOKENS, THE WAY THE BILL IS. A request is billed
# for its text input, its input images and its output image, each at its own
# rate in TOKEN_PRICES. The estimate prices the same three things before the
# request, rather than reading a per-image dollar table. The old table was
# gpt-image-2's, and it was wrong both ways: it priced no input images, so an
# edit or a reference run was quoted at a fraction of its bill, and it quoted
# gpt-image-2's figures for the 2.5 models, which spend far fewer output tokens,
# so the confirmation gate fired on money that was never going to be spent.

# Output tokens per image at 1024x1024, and where each figure comes from.
# "published": OpenAI's per-image price for gpt-image-2, divided by the output
# rate. "calibrated": measured from real runs - the 2.5 models return the same
# token count for a given tier and size every time, and flare and sunburst
# return the same count as each other at high (1,756). The xhigh figure was
# measured on sunburst. "upper bound": no measurement yet, so a figure at or
# above anything the tier can plausibly return, until history supplies one.
OUTPUT_TOKENS_2_5 = {
    "low": (200, "calibrated"),
    "medium": (1756, "upper bound"),
    "high": (1756, "calibrated"),
    "xhigh": (3340, "calibrated"),
    "max": (13400, "upper bound"),
    "auto": (3340, "upper bound"),
}
OUTPUT_TOKENS = {
    "gpt-image-2": {
        "low": (200, "published"),
        "medium": (1767, "published"),
        "high": (7034, "published"),
        "auto": (7034, "upper bound"),
    },
    "gpt-image-2.5-flare": OUTPUT_TOKENS_2_5,
    "gpt-image-2.5-sunburst": OUTPUT_TOKENS_2_5,
}

# Output tokens scale with size by one rule on every model and tier measured:
# about sqrt(width x height) / aspect, against 1024x1024. So a wide image costs
# less than a square one of the same area - 1536x960 returns 0.71x the square
# figure and 1792x608 returns 0.34x. The rule reads 1 to 6% above every size
# measured, which is the right side to miss on. It was measured up to about
# 2.1 megapixels; above that the estimate scales with the pixel count instead,
# which is pessimistic, and says "upper bound".
CALIBRATED_PIXELS = 2_100_000

# size=auto lets the API choose. It has returned up to 1.17x the square
# figure, so auto is priced at 1.2x.
AUTO_SIZE_FACTOR = 1.2

# Input tokens per input image - the --edit image, each --reference, and the
# mask. Measured values run from about 640 to about 1,520 per image depending
# on its size, and they are billed once per output image, not once per request.
INPUT_IMAGE_TOKENS = 1600

# Text input tokens per character of the prompt as sent. Real prompts run at
# about 0.22; a third is the pessimistic side of that.
TEXT_TOKENS_PER_CHAR = 1 / 3

# A measured figure replaces the table once this many runs agree on model,
# quality, size and kind (edit or generation), so one run cannot set the price.
MEASURED_MIN_SAMPLES = 3
MEASURED_WINDOW = 20

CONFIRM_THRESHOLD = 0.50

# Exit codes a script can act on. 1 is any error.
EXIT_CANCELLED = 3
EXIT_OVER_CAP = 4

# A DAY'S SPEND, NOT A CALL'S. CONFIRM_THRESHOLD is per invocation, so a script
# that calls this once per image never trips it: 2,000 separate images at $0.21
# is $420 and every single call is $0.21, comfortably under $0.50. That is the
# exact shape of a batch job, and it is how a real run cost 35x what it needed
# to on 2026-08-23.
DAILY_WARN = 5.00

MIN_PIXELS = 655_360
MAX_PIXELS = 8_294_400
MAX_EDGE = 3840
SIZE_STEP = 16


def entry_cost(entry: dict) -> float:
    """What an image actually cost, preferring the measured figure."""
    for field in ("actual_cost", "estimated_cost"):
        value = entry.get(field)
        if value:
            try:
                return float(value)
            except (TypeError, ValueError):
                continue
    return 0.0


def read_history_entries() -> list:
    """Every history row that parses, one per request.

    A run writes two rows with the same id: a pending row before the request
    and a final one after it (see append_history). The final row takes the
    pending row's place here, so a request is never counted twice. A row with
    no final row is a request whose outcome was never recorded - the process
    was killed, or timed out - and it stays, because it may have been billed.

    A line that does not parse is skipped. It used to end the read, so one
    torn or hand-edited line hid every row after it from the daily total, the
    cost calibration and set-check.
    """
    if not HISTORY_FILE.exists():
        return []
    try:
        text = HISTORY_FILE.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    rows: list = []
    position: dict = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict):
            continue
        run_id = row.get("id")
        if run_id and run_id in position:
            rows[position[run_id]] = row
            continue
        if run_id:
            position[run_id] = len(rows)
        rows.append(row)
    return rows


def images_saved(entry: dict) -> bool:
    """Did this row's request produce images on disk?

    Rows written before the status field existed were only ever written after
    the images were saved, so no status means yes.
    """
    return entry.get("status") in (None, "complete")


def spend_today() -> float:
    """What has already been spent through this skill today, from history.jsonl.

    A pending row counts at its estimate: a request that was sent and never
    recorded may still have been billed, and an under-count is the direction
    that does damage. A failed row counts as nothing - the API returned an
    error, not an image.
    """
    today, total = datetime.now().strftime("%Y-%m-%d"), 0.0
    for e in read_history_entries():
        if e.get("status") == "failed":
            continue
        if str(e.get("timestamp", "")).startswith(today):
            total += entry_cost(e)
    return total


def run_kind(inputs: int) -> str:
    return "edit" if inputs else "generation"


def row_kind(entry: dict) -> str:
    """Was this history row an edit or a generation?

    Rows from before `inputs` was recorded are judged by their usage block: an
    edit or reference run bills input image tokens and a generation does not.
    """
    inputs = entry.get("inputs")
    if inputs is not None:
        try:
            return run_kind(int(inputs))
        except (TypeError, ValueError):
            pass
    details = (entry.get("usage") or {}).get("input_tokens_details") or {}
    return run_kind(1 if details.get("image_tokens") else 0)


def fixed_size(size: str | None) -> tuple | None:
    """(width, height) for a WIDTHxHEIGHT size, or None for auto or no size."""
    match = re.fullmatch(r"(\d+)x(\d+)", size or "")
    if not match or not int(match.group(1)) or not int(match.group(2)):
        return None
    return int(match.group(1)), int(match.group(2))


def size_factor(size: str | None) -> tuple:
    """(output tokens against 1024x1024, is this size inside the measured range)."""
    dims = fixed_size(size)
    if not dims:
        return AUTO_SIZE_FACTOR, True
    width, height = dims
    pixels = width * height
    if pixels > CALIBRATED_PIXELS:
        return pixels / (1024 * 1024), False
    aspect = max(width, height) / min(width, height)
    return (pixels**0.5) / aspect / 1024, True


def measured_output_tokens(model: str, quality: str, size: str, kind: str) -> float | None:
    """Output tokens per image that this exact combination has actually returned.

    Self-calibrating, and output tokens only: the input side of a bill depends
    on how many images went in, which is priced separately. Edits and
    generations are kept apart. The highest of the recent samples is used, not
    the median: at a fixed size the count does not vary, and at size=auto it
    does, and the estimate must not come in under the bill.
    """
    samples = []
    for e in read_history_entries():
        if e.get("model") != model or e.get("quality") != quality or (e.get("size") or "auto") != size:
            continue
        usage = e.get("usage") or {}
        if not usage.get("output_tokens") or row_kind(e) != kind:
            continue
        try:
            samples.append(float(usage["output_tokens"]) / int(e.get("n") or 1))
        except (TypeError, ValueError, ZeroDivisionError):
            continue
    if len(samples) < MEASURED_MIN_SAMPLES:
        return None
    return max(samples[-MEASURED_WINDOW:])


def table_output_tokens(model: str, quality: str, size: str) -> tuple:
    """(output tokens per image, basis) from OUTPUT_TOKENS and the size rule."""
    tiers = OUTPUT_TOKENS.get(model)
    if not tiers:
        # Not a model this file knows: the dearest figure it has.
        tokens, basis = max(t for table in OUTPUT_TOKENS.values() for t, _ in table.values()), "upper bound"
    elif quality in tiers:
        tokens, basis = tiers[quality]
    else:
        tokens, basis = max(t for t, _ in tiers.values()), "upper bound"
    factor, inside = size_factor(size)
    return tokens * factor, basis if inside else "upper bound"


def cost_per_unit(model: str, quality: str, size: str, inputs: int = 0, prompt: str = "") -> tuple:
    """(cost per output image, where the output-token figure came from).

    Priced the way the bill is: text in, input images in, image out, each at
    its own rate. `inputs` is how many images go in (edit image, references
    and mask); each one is billed again for every output image.
    """
    out_tokens, basis = table_output_tokens(model, quality, size)
    measured = measured_output_tokens(model, quality, size, run_kind(inputs))
    # At a fixed size the measured count is exact and replaces the table. At
    # size=auto the API picks the size per request, so the count moves, and a
    # run of small ones must not talk the estimate below the table's figure.
    if measured is not None and (fixed_size(size) or measured >= out_tokens):
        out_tokens, basis = measured, "measured"
    prices = TOKEN_PRICES.get(model) or max(TOKEN_PRICES.values(), key=lambda p: p["image_out"])
    text_tokens = len(prompt) * TEXT_TOKENS_PER_CHAR
    image_tokens = inputs * INPUT_IMAGE_TOKENS
    cost = (
        text_tokens * prices["text_in"] + image_tokens * prices["image_in"] + out_tokens * prices["image_out"]
    ) / 1_000_000
    return cost, basis


def estimate_cost(model: str, quality: str, size: str, n: int, inputs: int = 0, prompt: str = "") -> float:
    return cost_per_unit(model, quality, size, inputs, prompt)[0] * n


def cost_from_usage(model: str, usage: dict, batch: bool = False) -> float | None:
    """The real cost of a call, from the token counts the API reports back.

    OpenAI's own advice is to measure with `usage` rather than read a table,
    and it is the only figure that stays true when prices or token counts move
    under the skill.
    """
    if not usage:
        return None
    prices = TOKEN_PRICES.get(model)
    if not prices:
        return None
    try:
        details = usage.get("input_tokens_details") or {}
        if details:
            text_in = float(details.get("text_tokens") or 0)
            image_in = float(details.get("image_tokens") or 0)
        else:
            # No breakdown: price the whole input at the image rate, the dearer
            # of the two, rather than quietly under-reporting.
            text_in, image_in = 0.0, float(usage.get("input_tokens") or 0)
        image_out = float(usage.get("output_tokens") or 0)
    except (TypeError, ValueError):
        return None
    cost = (text_in * prices["text_in"] + image_in * prices["image_in"] + image_out * prices["image_out"]) / 1_000_000
    if batch and MODELS.get(model, {}).get("batch_discount"):
        cost *= BATCH_DISCOUNT
    return cost


# ---------- Sizing ----------


def parse_size(text: str | None) -> tuple:
    """'1536x864' -> (1536, 864). 'auto' or None -> (None, None)."""
    if not text or text == "auto":
        return (None, None)
    match = re.fullmatch(r"(\d+)\s*[x×]\s*(\d+)", text.strip())
    if not match:
        print(
            f"Error: could not read size '{text}'. Use WIDTHxHEIGHT, for example 1536x864, or 'auto'.",
            file=sys.stderr,
        )
        sys.exit(1)
    return (int(match.group(1)), int(match.group(2)))


def size_problem(width: int, height: int) -> str | None:
    """Why the API would reject this size, or None if it would not.

    Checked here rather than at the API because a rejected request still costs
    a round trip and an opaque error, and every one of these limits is
    documented and stable.
    """
    if width % SIZE_STEP or height % SIZE_STEP:
        return f"both edges must be multiples of {SIZE_STEP}px (got {width}x{height})"
    if max(width, height) > MAX_EDGE:
        return f"the longest edge must be {MAX_EDGE}px or less (got {max(width, height)})"
    ratio = max(width, height) / min(width, height)
    if ratio > 3.0001:
        return f"the aspect ratio must sit between 1:3 and 3:1 (got {ratio:.2f}:1)"
    pixels = width * height
    if pixels < MIN_PIXELS:
        return f"the total pixel count must be at least {MIN_PIXELS:,} (got {pixels:,})"
    if pixels > MAX_PIXELS:
        return f"the total pixel count must be at most {MAX_PIXELS:,} (got {pixels:,})"
    return None


def snap_size(width: int, height: int) -> tuple:
    """Nearest API-legal generation size at (near enough) the requested aspect.

    Rounds each edge UP to a multiple of 16, so the generated image is never
    smaller than the target and the final fit is always a downscale. A crop
    throws away pixels that were paid for; a downscale does not.
    """

    def round_up(value: float) -> int:
        return int((int(value) + SIZE_STEP - 1) // SIZE_STEP * SIZE_STEP)

    w, h = round_up(width), round_up(height)
    aspect = w / h

    pixels = w * h
    if pixels < MIN_PIXELS:
        scale = (MIN_PIXELS / pixels) ** 0.5 * 1.02
        w, h = round_up(w * scale), round_up(h * scale)
    elif pixels > MAX_PIXELS:
        scale = (MAX_PIXELS / pixels) ** 0.5
        w, h = round_up(w * scale), round_up(h * scale)
        while w * h > MAX_PIXELS and w > SIZE_STEP and h > SIZE_STEP:
            w -= SIZE_STEP
            h = round_up(w / aspect)

    while max(w, h) > MAX_EDGE:
        if w >= h:
            w -= SIZE_STEP
            h = round_up(w / aspect)
        else:
            h -= SIZE_STEP
            w = round_up(h * aspect)

    if max(w, h) / min(w, h) > 3:
        if w > h:
            w = h * 3
        else:
            h = w * 3
    return (w, h)


def size_for(args_size: str | None, platform: dict | None) -> str | None:
    """The size string to send. --size wins; --platform sets it when absent."""
    width, height = parse_size(args_size)
    if width:
        problem = size_problem(width, height)
        if problem:
            fixed = snap_size(width, height)
            print(
                f"Error: {args_size} is not a valid generation size - {problem}.\n"
                f"       The nearest legal size is {fixed[0]}x{fixed[1]}.",
                file=sys.stderr,
            )
            sys.exit(1)
        return f"{width}x{height}"
    if platform:
        w, h = snap_size(int(platform["width"]), int(platform["height"]))
        return f"{w}x{h}"
    return None


# ---------- Config & secrets ----------


def _closing_quote(text: str) -> int:
    """Index of the quote that closes text[0], or -1. '' escapes in single quotes, a backslash in double."""
    quote, i = text[0], 1
    while i < len(text):
        if quote == '"' and text[i] == "\\":
            i += 2
            continue
        if text[i] == quote:
            if quote == "'" and text[i + 1 : i + 2] == "'":
                i += 2
                continue
            return i
        i += 1
    return -1


def config_value(text: str, where: str):
    """One value from config.yaml, typed the way YAML would type it."""
    if text[:1] in ("'", '"'):
        end = _closing_quote(text)
        rest = text[end + 1 :].strip() if end != -1 else ""
        if end == -1 or (rest and not rest.startswith("#")):
            config_error(where, "a quoted value that does not close cleanly")
        body = text[1:end]
        if text[0] == "'":
            return body.replace("''", "'")
        try:
            return json.loads(f'"{body}"')
        except ValueError:
            config_error(where, "an escape in a double-quoted value that cannot be read")
    comment = re.search(r"\s#", text)
    if comment:
        text = text[: comment.start()]
    text = text.strip()
    if text[:1] in ("{", "[", "&", "*", "!", "|", ">"):
        config_error(where, "a nested or flow value")
    if text in ("", "~", "null", "Null", "NULL"):
        return None
    if text in ("true", "True", "TRUE"):
        return True
    if text in ("false", "False", "FALSE"):
        return False
    if re.fullmatch(r"[-+]?\d+", text):
        return int(text)
    if re.fullmatch(r"[-+]?(\d+\.\d*|\.\d+|\d+)([eE][-+]?\d+)?", text):
        return float(text)
    return text


def config_error(where: str, what: str) -> NoReturn:
    print(
        f"Error: {where} has {what}. config.yaml holds flat `key: value` lines only, for example\n"
        f"       model: gpt-image-2.5-flare\n"
        f"       daily_cap: 20",
        file=sys.stderr,
    )
    sys.exit(1)


def parse_config(text: str, source: str = "config.yaml") -> dict[str, Any]:
    """config.yaml, read without PyYAML.

    The file has only ever held flat `key: value` lines - provider, model,
    quality, size, daily_cap - so this reads exactly that subset: comments,
    quoted and plain strings, numbers, true/false and null. Anything nested is
    refused with the line number rather than guessed at.
    """
    config: dict[str, Any] = {}
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#") or line == "---":
            continue
        where = f"{source} line {number}"
        if raw[:1] in (" ", "\t") or line.startswith("- "):
            config_error(where, "an indented or list line")
        key, sep, value = line.partition(":")
        key = key.strip()
        if not sep or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", key):
            config_error(where, "a line that is not `key: value`")
        if value and value[:1] not in (" ", "\t"):
            config_error(where, "no space after the colon")
        config[key] = config_value(value.strip(), where)
    return config


def dump_config(config: dict) -> str:
    """The inverse of parse_config, for the handful of plain values init writes."""
    lines = []
    for key, value in config.items():
        if value is None:
            text = "null"
        elif isinstance(value, bool):
            text = str(value).lower()
        elif isinstance(value, str):
            # Plain when it reads back as the same string, quoted otherwise
            # ("true", "20", anything with a colon, a hash or a space).
            plain = re.fullmatch(r"[A-Za-z0-9_./+-]+", value) and config_value(value, key) == value
            text = value if plain else json.dumps(value)
        else:
            text = str(value)
        lines.append(f"{key}: {text}\n")
    return "".join(lines)


def load_config() -> dict[str, Any]:
    if CONFIG_FILE.exists():
        return parse_config(CONFIG_FILE.read_text(encoding="utf-8"), str(CONFIG_FILE))
    return {}


def get_api_key(provider: str) -> str | None:
    prov = PROVIDERS[provider]
    return os.environ.get(prov["key_env"])


# ---------- Presets ----------


def load_catalogue(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8")) or {}
    except ValueError as e:
        print(f"Error: {path} is not valid JSON: {e}", file=sys.stderr)
        sys.exit(1)


def load_presets() -> dict[str, dict]:
    return load_catalogue(PRESETS_FILE)


def load_platforms() -> dict[str, dict]:
    return load_catalogue(PLATFORMS_FILE)


def compose_prompt(user_prompt: str, preset_name: str | None) -> str:
    if not preset_name:
        return user_prompt
    presets = load_presets()
    if preset_name not in presets:
        print(
            f"Error: unknown preset '{preset_name}'. Available: {', '.join(presets.keys())}",
            file=sys.stderr,
        )
        sys.exit(1)
    return presets[preset_name]["prompt"].replace("{subject}", user_prompt)


# ---------- Image I/O ----------

FORMAT_SUFFIX = {"png": ".png", "jpeg": ".jpg", "webp": ".webp"}


def encode_image(path: str) -> str:
    p = Path(path)
    if not p.exists():
        print(f"Error: file not found: {path}", file=sys.stderr)
        sys.exit(1)
    with open(p, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")


def save_image(b64_data: str, output_path: Path, overwrite: bool = False) -> None:
    """Write one image. Without overwrite, an existing file is an error, never a casualty."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "wb" if overwrite else "xb") as f:
        f.write(base64.b64decode(b64_data))


def output_paths(output_path: Path, count: int) -> tuple:
    """(image paths, contact sheet path or None) that a run of `count` images writes."""
    if count <= 1:
        return [output_path], None
    stem, suffix, parent = output_path.stem, output_path.suffix, output_path.parent
    return [parent / f"{stem}-{i:02d}{suffix}" for i in range(1, count + 1)], parent / f"{stem}-contact{suffix}"


def free_output_path(output_path: Path, count: int) -> Path:
    """output_path, or the first name-2, name-3 ... whose files are all free.

    A run used to write over whatever was at its output path, so every rerun to
    the same name replaced an image that had been paid for. Now it moves aside
    instead, the way OpenAI's own imagegen skill does, and --overwrite is the
    explicit way back to replacing.
    """
    candidate, k = output_path, 1
    while True:
        paths, contact = output_paths(candidate, count)
        if not any(p.exists() for p in [*paths, contact] if p):
            return candidate
        k += 1
        candidate = output_path.with_name(f"{output_path.stem}-{k}{output_path.suffix}")


def run_imagemagick(command: list, what: str) -> bool:
    """Run one ImageMagick step. A failure is a warning, never a crash.

    Post-processing runs on images that are already paid for and saved. It used
    to run with check=True before the history row was written, so an
    ImageMagick error lost the record of a billed image.
    """
    try:
        subprocess.run(command, check=True, capture_output=True)
    except (subprocess.CalledProcessError, OSError) as e:
        detail = ""
        if isinstance(e, subprocess.CalledProcessError) and e.stderr:
            detail = " " + e.stderr.decode("utf-8", errors="replace").strip().splitlines()[-1]
        elif isinstance(e, OSError):
            detail = f" {e}"
        print(f"Warning: ImageMagick failed on the {what}, so it was skipped.{detail}", file=sys.stderr)
        return False
    return True


def imagemagick(tool: str) -> list | None:
    """The command that runs an ImageMagick tool, on version 7 or 6, or None.

    ImageMagick 7 ships one binary, `magick`, which does what `convert` did and
    takes `montage` as a subcommand. ImageMagick 6 - which is what
    `apt install imagemagick` gives on Ubuntu 24.04, and what init and setup.sh
    tell Linux users to run - has `convert` and `montage` and no `magick`. Only
    `magick` used to be probed, so on IM6 every platform fit and contact sheet
    was skipped. The arguments this file passes mean the same to both.
    """
    if shutil.which("magick"):
        return ["magick"] if tool == "convert" else ["magick", tool]
    if shutil.which(tool):
        return [tool]
    return None


def image_size(path: Path) -> tuple | None:
    """(width, height) from the file's own header - PNG, JPEG or WebP - or None.

    Read directly rather than through ImageMagick, because the case that needs
    it is the one where ImageMagick is missing or has just failed.
    """
    try:
        data = Path(path).read_bytes()
    except OSError:
        return None
    try:
        if data[:8] == b"\x89PNG\r\n\x1a\n" and data[12:16] == b"IHDR":
            return struct.unpack(">II", data[16:24])
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            kind = data[12:16]
            if kind == b"VP8X":
                return (1 + int.from_bytes(data[24:27], "little"), 1 + int.from_bytes(data[27:30], "little"))
            if kind == b"VP8 ":
                w, h = struct.unpack("<HH", data[26:30])
                return (w & 0x3FFF, h & 0x3FFF)
            if kind == b"VP8L":
                bits = int.from_bytes(data[21:25], "little")
                return ((bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1)
            return None
        if data[:2] == b"\xff\xd8":
            i = 2
            while i + 9 <= len(data):
                if data[i] != 0xFF:
                    i += 1
                    continue
                marker = data[i + 1]
                if marker == 0xFF or marker in (0x01, 0xD8) or 0xD0 <= marker <= 0xD7:
                    i += 1 if marker == 0xFF else 2
                    continue
                # Start-of-frame markers carry the size; C4, C8 and CC are not frames.
                if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                    h, w = struct.unpack(">HH", data[i + 5 : i + 9])
                    return (w, h)
                i += 2 + struct.unpack(">H", data[i + 2 : i + 4])[0]
    except struct.error:
        return None
    return None


def platform_fit(image_path: Path, width: int, height: int) -> bool:
    """Scale and centre-crop to the platform size. True if the file was fitted."""
    command = imagemagick("convert")
    if not command:
        print(
            "Warning: ImageMagick not found (neither magick nor convert is on PATH), skipping platform resize",
            file=sys.stderr,
        )
        return False
    return run_imagemagick(
        [
            *command,
            str(image_path),
            "-resize",
            f"{width}x{height}^",
            "-gravity",
            "center",
            "-extent",
            f"{width}x{height}",
            str(image_path),
        ],
        "platform resize",
    )


def make_contact_sheet(images: list, output: Path, cols: int = 3) -> bool:
    """Tile the images into one sheet. True if the sheet was written."""
    command = imagemagick("montage")
    if not command:
        print(
            "Warning: ImageMagick not found (neither magick nor montage is on PATH), skipping contact sheet",
            file=sys.stderr,
        )
        return False
    return run_imagemagick(
        [*command, *[str(p) for p in images], "-geometry", "+4+4", "-tile", f"{cols}x", str(output)],
        "contact sheet",
    )


# ---------- API ----------


MIME_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}


def _build_multipart(fields: list) -> tuple:
    """Build multipart/form-data body. Each field is (name, value, filename_or_None)."""
    boundary = uuid.uuid4().hex
    lines: list = []
    for name, value, filename in fields:
        lines.append(f"--{boundary}".encode())
        if filename:
            ext = Path(filename).suffix.lower()
            mime = MIME_TYPES.get(ext, "image/png")
            lines.append(f'Content-Disposition: form-data; name="{name}"; filename="{filename}"'.encode())
            lines.append(f"Content-Type: {mime}".encode())
        else:
            lines.append(f'Content-Disposition: form-data; name="{name}"'.encode())
        lines.append(b"")
        lines.append(value if isinstance(value, bytes) else value.encode())
    lines.append(f"--{boundary}--".encode())
    body = b"\r\n".join(lines)
    content_type = f"multipart/form-data; boundary={boundary}"
    return body, content_type


def describe_api_error(body: str, is_edit: bool = False) -> tuple:
    """(human message, is it worth retrying).

    A blocked prompt and a transient failure look the same to anything reading
    only the status code, and retrying a refusal four times just spends four
    times as long arriving at the same refusal.

    The --moderation hint is for generations only. The edit endpoint has no
    moderation field, so suggesting it for a blocked edit sends the user to a
    flag that cannot help.
    """
    try:
        error = (json.loads(body) or {}).get("error") or {}
    except Exception:  # noqa: BLE001
        return body, True
    code = error.get("code") or ""
    etype = error.get("type") or ""
    message = error.get("message") or body
    if code == "moderation_blocked":
        details = error.get("moderation_details") or {}
        stage = details.get("moderation_stage", "unknown")
        categories = ", ".join(details.get("categories") or []) or "unspecified"
        if is_edit:
            hint = "Edits have no moderation setting, so the prompt or the input images are what to change."
        else:
            hint = "If the subject is legitimate, --moderation low is the less restrictive setting."
        return (
            f"Moderation blocked this request at the {stage} stage ({categories}). "
            f"Change the prompt or the input images - retrying as-is will not help. {hint}",
            False,
        )
    if etype == "image_generation_user_error":
        return (
            f"{message} (code: {code or 'none'}). This needs the request changed, not retried.",
            False,
        )
    return (f"{message} (type: {etype or 'unknown'}, code: {code or 'none'})", True)


def api_request(
    prompt: str,
    provider: str,
    api_key: str,
    model: str = DEFAULT_MODEL,
    size: str | None = None,
    quality: str = "low",
    n: int = 1,
    edit_image: str | None = None,
    reference_images: list | None = None,
    mask: str | None = None,
    background: str | None = None,
    output_format: str | None = None,
    output_compression: int | None = None,
    moderation: str | None = None,
) -> tuple:
    """Call the generation/edit API. Returns (list of base64 images, usage dict)."""
    prov = PROVIDERS[provider]
    is_edit = bool(edit_image or reference_images or mask)

    def build_request():
        if is_edit:
            fields = [
                ("model", model, None),
                ("prompt", prompt, None),
                ("n", str(n), None),
                ("quality", quality, None),
                # ALWAYS A SIZE ON AN EDIT. CreateImageEditRequest defaults size
                # to 1024x1024, not auto, so an edit sent with no size came back
                # square whatever the photo's shape, and was billed at the wrong
                # aspect. auto lets the model follow the input, and is what the
                # estimate already priced (size=None is costed as auto).
                ("size", size or "auto", None),
            ]
            if background:
                fields.append(("background", background, None))
            if output_format:
                fields.append(("output_format", output_format, None))
            if output_compression is not None:
                fields.append(("output_compression", str(output_compression), None))
            # No moderation field: CreateImageEditRequest does not have one, and
            # main() refuses --moderation on an edit rather than drop it.
            if edit_image:
                with open(edit_image, "rb") as f:
                    fields.append(("image[]", f.read(), Path(edit_image).name))
            for ref in reference_images or []:
                with open(ref, "rb") as f:
                    fields.append(("image[]", f.read(), Path(ref).name))
            if mask:
                with open(mask, "rb") as f:
                    fields.append(("mask", f.read(), Path(mask).name))
            data, content_type = _build_multipart(fields)
            headers = {"Authorization": f"Bearer {api_key}", "Content-Type": content_type}
            url = prov["edit_url"]
        else:
            body: dict[str, Any] = {
                "model": model,
                "prompt": prompt,
                "n": n,
                "quality": quality,
                "output_format": output_format or "png",
            }
            if size:
                body["size"] = size
            if background:
                body["background"] = background
            if output_compression is not None:
                body["output_compression"] = output_compression
            if moderation:
                body["moderation"] = moderation
            data = json.dumps(body).encode("utf-8")
            headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
            url = prov["url"]
        return urllib.request.Request(url, data=data, headers=headers, method="POST")

    max_retries = 4
    for attempt in range(max_retries):
        try:
            # Rebuilt each attempt: a Request that has already been sent is not
            # guaranteed safe to hand back to urlopen.
            with urllib.request.urlopen(build_request(), timeout=900) as resp:
                result = json.loads(resp.read().decode("utf-8"))
                resp_data = result.get("data")
                if not resp_data:
                    print("Error: API returned no image data.", file=sys.stderr)
                    sys.exit(1)
                images = [item["b64_json"] for item in resp_data if item.get("b64_json")]
                if not images:
                    print("Error: API response missing b64_json fields.", file=sys.stderr)
                    sys.exit(1)
                return images, (result.get("usage") or {})
        except urllib.error.HTTPError as e:
            error_body = e.read().decode("utf-8", errors="replace") if e.fp else ""
            message, retryable = describe_api_error(error_body, is_edit)
            if retryable and (e.code == 429 or e.code >= 500):
                if attempt < max_retries - 1:
                    wait = 2 ** (attempt + 1)
                    label = "Rate limited" if e.code == 429 else f"Server error {e.code}"
                    print(
                        f"{label}, retrying in {wait}s (attempt {attempt + 1}/{max_retries})...",
                        file=sys.stderr,
                    )
                    time.sleep(wait)
                    continue
                print(
                    f"Failed after {max_retries} attempts. Last error {e.code}: {message}",
                    file=sys.stderr,
                )
                sys.exit(1)
            print(f"Error {e.code}: {message}", file=sys.stderr)
            sys.exit(1)
        except TimeoutError:
            # A socket read timeout is NOT a URLError - without this branch it
            # escapes the retry loop entirely and crashes with a raw traceback.
            if attempt < max_retries - 1:
                wait = 2 ** (attempt + 1)
                print(
                    f"Read timed out, retrying in {wait}s (attempt {attempt + 1}/{max_retries})...",
                    file=sys.stderr,
                )
                time.sleep(wait)
            else:
                print(
                    f"Failed after {max_retries} attempts: the API did not respond in time.",
                    file=sys.stderr,
                )
                sys.exit(1)
        except urllib.error.URLError as e:
            if attempt < max_retries - 1:
                wait = 2 ** (attempt + 1)
                print(f"Network error: {e.reason}, retrying in {wait}s...", file=sys.stderr)
                time.sleep(wait)
            else:
                print(f"Failed after {max_retries} attempts: {e.reason}", file=sys.stderr)
                sys.exit(1)
    return [], {}


# ---------- History ----------


@dataclass
class HistoryEntry:
    timestamp: str
    prompt: str
    preset: str | None
    platform: str | None
    model: str
    quality: str
    size: str | None
    background: str | None
    output_format: str
    provider: str
    n: int
    output: str
    output_dir: str
    project: str | None
    estimated_cost: float | None
    actual_cost: float | None
    usage: dict | None
    # The set identity: the repository's common git directory and the output
    # directory relative to its top level. Null outside git. See set_key.
    repo: str | None = None
    repo_dir: str | None = None
    # The write-ahead record. `id` ties a request's pending row to its final
    # row; `status` is pending (sent, outcome not yet known), complete or
    # failed. Rows from before 25 Sep 2026 have neither and are complete.
    id: str | None = None
    status: str | None = None
    duration_s: float | None = None
    # How many images went in (edit image, references, mask). It separates
    # edits from generations when the estimate calibrates from history.
    inputs: int | None = None


def append_history(entry: HistoryEntry) -> None:
    """Append one row to history.jsonl, as a single write.

    O_APPEND and one os.write per row, so rows from runs going at the same
    time land whole rather than interleaved.
    """
    ensure_config_dir()
    line = (json.dumps(asdict(entry)) + "\n").encode("utf-8")
    fd = os.open(HISTORY_FILE, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(fd, line)
    finally:
        os.close(fd)


# A batch saves from several threads at once, and last.json is rewritten whole.
_LAST_RUN_LOCK = threading.Lock()


def save_last_run(entry: HistoryEntry) -> None:
    ensure_config_dir()
    with _LAST_RUN_LOCK, LAST_RUN_FILE.open("w") as f:
        json.dump(asdict(entry), f, indent=2)


def save_history(entry: HistoryEntry) -> None:
    append_history(entry)
    save_last_run(entry)


def load_history(n: int = 20, project: str | None = None) -> list:
    entries = []
    for entry in read_history_entries():
        if project and entry.get("project") != project:
            continue
        entries.append(entry)
    return entries[-n:]


def load_last_run() -> dict | None:
    if not LAST_RUN_FILE.exists():
        return None
    with LAST_RUN_FILE.open() as f:
        return json.load(f)


def entry_dir(entry: dict) -> str | None:
    """Which directory this history row wrote into.

    Rows written before output_dir existed stored a FILE path for single-image
    runs and a DIRECTORY path for multi-image ones, with nothing to tell them
    apart - so a --n 4 run recorded /a/b, and set-check then compared /a
    against /a/b and missed it. New rows carry output_dir outright; old ones
    are resolved by looking at the path.
    """
    direct = entry.get("output_dir")
    if direct:
        return direct
    out = entry.get("output")
    if not out:
        return None
    path = Path(out)
    if path.suffix:
        return str(path.parent)
    return str(path)


def dir_matches(where: str, target: str) -> bool:
    """Does this history row's directory refer to `target` (an absolute path)?

    New rows store an absolute directory. Legacy rows stored whatever the caller
    typed, so 101 of them hold things like `brand/graphics/aurora` - relative to
    a working directory nobody recorded. Resolving those against the CURRENT
    directory would silently attribute one folder's house tier to another, so
    they are matched on a whole-path-component suffix instead, and a bare "."
    (which carries no information and would otherwise match everything) is
    dropped.
    """
    path = Path(where)
    if path.is_absolute():
        try:
            return str(path.resolve()) == target
        except Exception:  # noqa: BLE001
            return False
    parts = tuple(p for p in path.parts if p not in (".", ""))
    if not parts:
        return False
    return Path(target).parts[-len(parts) :] == parts


# Variables that would point git at some other repository than the one the
# directory is in. A hook or a wrapper script can leave them set.
_GIT_ENV_OVERRIDES = ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE")


def set_key(directory) -> tuple | None:
    """(repository, directory relative to its top level), or None outside git.

    An absolute path is the wrong identity for a set. Every git worktree of one
    repository puts the same images/ folder at a different absolute path, and a
    worktree made for one session is gone by the next, so a set keyed on its
    absolute path is a new set every session. The repository is identified by
    its common git directory, which every worktree of it shares, and the set by
    its path from the top of whichever worktree it is in.

    The directory need not exist yet: git is asked from the nearest ancestor
    that does, which is the case for a first image into a new folder.
    """
    try:
        target = Path(directory).resolve()
    except (OSError, RuntimeError):
        return None
    probe = target
    while not probe.is_dir():
        if probe.parent == probe:
            return None
        probe = probe.parent
    git = shutil.which("git")
    if not git:
        return None
    env = {k: v for k, v in os.environ.items() if k not in _GIT_ENV_OVERRIDES}
    try:
        result = subprocess.run(
            [git, "-C", str(probe), "rev-parse", "--git-common-dir", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=10,
            env=env,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    lines = result.stdout.splitlines()
    if result.returncode != 0 or len(lines) < 2:
        return None
    common = Path(lines[0])
    if not common.is_absolute():
        common = probe / common
    try:
        relative = target.relative_to(Path(lines[1]).resolve())
        return (str(common.resolve()), relative.as_posix())
    except (OSError, RuntimeError, ValueError):
        return None


def set_fields(directory) -> dict:
    """The set identity a history row carries: repo and repo_dir, or nulls outside git."""
    key = set_key(directory)
    return {"repo": key[0], "repo_dir": key[1]} if key else {"repo": None, "repo_dir": None}


def _row_key(entry: dict, leaf: str, cache: dict) -> tuple | None:
    """The set key for one history row.

    New rows carry it. Older rows only have an absolute directory, so the key is
    worked out from that directory when it still exists - which is what lets a
    set made in the main checkout before this field existed be found from a
    worktree. Only rows whose last path component matches the target's are
    asked, because a different folder name cannot be the same set and each
    lookup is a git call.
    """
    if entry.get("repo") and entry.get("repo_dir"):
        return (entry["repo"], entry["repo_dir"])
    where = entry_dir(entry)
    if not where or not Path(where).is_absolute() or Path(where).name != leaf:
        return None
    if where not in cache:
        cache[where] = set_key(where) if Path(where).is_dir() else None
    return cache[where]


def set_profile(output_path) -> dict:
    """What model and quality this directory's SET was made at.

    THE CHECK THAT WAS MISSING. A directory of images is a SET, and a set has a
    house setting somebody already chose. On 2026-08-21 all three tiers were run
    side by side for ScentPrism's note art - the history still holds them as
    q-low, q-medium and q-high - and LOW was picked, because the images ship at
    300x400 and the extra detail is thrown away by the downscale. Two days later
    a new batch went out at HIGH for no reason except that high is the default,
    at 35x the price and no visible difference.

    Nothing had to be guessed to avoid that. The answer was in history.jsonl the
    whole time. Model is tracked for the same reason: a set that is half Flare
    and half gpt-image-2 does not read as one set either.

    A row matches on its repository and repo-relative directory first (see
    set_key), so the same folder in another worktree is the same set. It falls
    back to the absolute path, which is all there is outside git.
    """
    try:
        target_path = Path(output_path).resolve().parent
    except Exception:  # noqa: BLE001
        return {"quality": None, "model": None, "count": 0}
    target = str(target_path)
    target_key = set_key(target_path)
    cache: dict = {}
    qualities: dict = {}
    models: dict = {}
    count = 0
    for e in read_history_entries():
        # A pending or failed row has no image in the set to match.
        if not images_saved(e):
            continue
        same_set = bool(target_key) and _row_key(e, target_path.name, cache) == target_key
        if not same_set:
            where = entry_dir(e)
            same_set = bool(where) and dir_matches(where, target)
        if not same_set:
            continue
        count += e.get("n") or 1
        q = e.get("quality")
        # Rows predating --model were all gpt-image-2; there was nothing else.
        m = e.get("model") or "gpt-image-2"
        if q:
            qualities[q] = qualities.get(q, 0) + 1
        models[m] = models.get(m, 0) + 1
    if not count:
        return {"quality": None, "model": None, "count": 0}
    return {
        "quality": max(qualities, key=qualities.get) if qualities else None,
        "model": max(models, key=models.get) if models else None,
        "count": count,
    }


# ---------- Metadata ----------


def save_metadata(output_path: Path, entry: HistoryEntry) -> Path | None:
    """The opt-in --sidecar: the history row again, as <image>.json beside the image.

    Off by default. It duplicates history.jsonl, it is easy to commit next to
    the images by accident, and as <stem>.json it replaced any file with that
    name - generating package.png destroyed package.json. It never overwrites
    now, --overwrite or not: an existing .json may not be ours.
    """
    meta_path = output_path.with_suffix(".json")
    try:
        with meta_path.open("x") as f:
            json.dump(asdict(entry), f, indent=2)
    except FileExistsError:
        print(f"Warning: {meta_path} already exists, so no sidecar was written.", file=sys.stderr)
        return None
    return meta_path


# ---------- Init wizard ----------


def cmd_init():
    print("GPT Image - Setup Wizard\n")

    fit, sheet = imagemagick("convert"), imagemagick("montage")
    if fit and sheet:
        version = "7" if fit[0] == "magick" else "6"
        print(f"  ImageMagick {version}: OK ({' '.join(fit)}, {' '.join(sheet)})")
    else:
        print("  ImageMagick: not found")
        print("\n  Platform fitting and contact sheets will be unavailable. Either version works:")
        print("  macOS: brew install imagemagick")
        print("  Linux: sudo apt install imagemagick")

    print()
    for provider_name in PROVIDERS:
        key = get_api_key(provider_name)
        if key:
            print(f"  {provider_name} key: OK {key[:8]}...{key[-4:]}")
        else:
            print(f"  {provider_name} key: not found")

    ensure_config_dir()
    defaults = load_config()
    if not defaults:
        # Quality is deliberately absent. Writing one here used to pin it to
        # "high", which silently overrode the low default the whole draft-first
        # flow depends on - the wizard reintroduced the bug the default was
        # changed to prevent.
        defaults = {"provider": "openai", "model": DEFAULT_MODEL}
        CONFIG_FILE.write_text(dump_config(defaults), encoding="utf-8")
        print(f"\nConfig saved to {CONFIG_FILE}")
    else:
        print(f"\nConfig already exists at {CONFIG_FILE}")
        if defaults.get("quality"):
            print(
                f"  Warning: config.yaml pins quality={defaults['quality']}, which overrides the "
                f"low default on every call. Remove the line unless you meant it."
            )

    print(f"\nRoughly, per image at 1024x1024 on {DEFAULT_MODEL}:")
    for tier, use in (("low", "draft, fast iteration"), ("high", "production"), ("xhigh", "dense text, fine detail")):
        print(f"  quality={tier + ':':7} ${cost_per_unit(DEFAULT_MODEL, tier, '1024x1024')[0]:.3f}  - {use}")
    per_input = INPUT_IMAGE_TOKENS * TOKEN_PRICES[DEFAULT_MODEL]["image_in"] / 1_000_000
    print(f"  each input image (--edit, --reference, --mask) adds about ${per_input:.3f}")
    print("\n  Real cost is read back from the API's token usage after each call,")
    print("  so history.jsonl holds what was actually billed, not this table.")

    print('\nReady. Try: scripts/imager.py "a cat astronaut" ./cat.png')


# ---------- List commands ----------


def cmd_list_models():
    print("Available models:\n")
    for name, info in MODELS.items():
        default = "  (default)" if name == DEFAULT_MODEL else ""
        print(f"  {name}{default}")
        print(f"    {info['description']}")
        print(f"    quality: {', '.join(info['qualities'])}")
        if info["batch_discount"]:
            print("    eligible for the 50% Batch API discount")
        print(f"    aliases: {', '.join(info['aliases'])}")
        print()


def cmd_list_presets():
    presets = load_presets()
    if not presets:
        print("No presets found.")
        return
    print("Available presets:\n")
    for name, info in presets.items():
        print(f"  {name:16s} {info['description']}")


def cmd_list_platforms():
    platforms = load_platforms()
    if not platforms:
        print("No platforms found.")
        return
    print("Available platforms:\n")
    for name, info in platforms.items():
        w, h = snap_size(int(info["width"]), int(info["height"]))
        note = "" if (w, h) == (info["width"], info["height"]) else f"  [generates {w}x{h}, fits down]"
        print(f"  {name:16s} {info['width']}x{info['height']}  ({info['description']}){note}")


# ---------- Main generate ----------


@dataclass
class Job:
    """One request, fully resolved and priced. The API key is not part of it."""

    user_prompt: str
    prompt: str
    preset: str | None
    platform: str | None
    platform_spec: dict | None
    provider: str
    model: str
    quality: str
    size: str | None
    background: str | None
    output_format: str
    output_compression: int | None
    moderation: str | None
    n: int
    edit: str | None
    reference: list | None
    mask: str | None
    project: str | None
    requested_path: Path
    output_path: Path
    overwrite: bool
    sidecar: bool
    inputs: int
    per: float = 0.0
    basis: str = ""

    @property
    def size_label(self) -> str:
        return self.size or "auto"


def input_count(edit: str | None, reference: list | None, mask: str | None) -> int:
    """How many images go in. Each is billed as input, once per image that comes out."""
    return (1 if edit else 0) + len(reference or []) + (1 if mask else 0)


def missing_inputs(edit: str | None, reference: list | None, mask: str | None) -> list:
    return [p for p in [edit, *(reference or []), mask] if p and not Path(p).is_file()]


def match_set(
    output_path: Path,
    model: str,
    quality: str,
    explicit_model: bool,
    explicit_quality: bool,
    size_label: str,
    inputs: int,
    prompt: str,
) -> tuple:
    """(model, quality) after matching the set output_path is going into.

    WHAT DID THIS SET USE LAST TIME? Adopt it unless the caller said otherwise,
    and say so - with the price per image before and after when it changes.
    """
    asked_model, asked_quality = model, quality
    house = set_profile(output_path)
    if not house["count"]:
        return model, quality
    if not explicit_quality and house["quality"] and house["quality"] != quality:
        print(
            f"Matching the set: {output_path.parent} already holds {house['count']} image(s) "
            f"made at quality={house['quality']}. Pass --quality to override.",
            file=sys.stderr,
        )
        quality = house["quality"]
    elif explicit_quality and house["quality"] and quality != house["quality"]:
        asked_per = cost_per_unit(model, quality, size_label, inputs, prompt)[0]
        house_per = cost_per_unit(model, house["quality"], size_label, inputs, prompt)[0]
        print(
            f"Warning: {output_path.parent} already holds {house['count']} image(s) made at "
            f"quality={house['quality']}, and you asked for {quality} (${asked_per:.3f}/image "
            f"against ${house_per:.3f} at the set's tier). A set that does not match itself is "
            f"the defect this warning exists for.",
            file=sys.stderr,
        )
    if not explicit_model and house["model"] and house["model"] != model:
        print(
            f"Matching the set: it was generated on {house['model']}. A set that is half one "
            f"model and half another does not read as one set. Pass --model to override.",
            file=sys.stderr,
        )
        model = house["model"]
        if quality not in MODELS[model]["qualities"]:
            quality = "high"
    elif explicit_model and house["model"] and model != house["model"]:
        print(
            f"Warning: that set was generated on {house['model']}, and you asked for {model}.",
            file=sys.stderr,
        )
    if (model, quality) != (asked_model, asked_quality):
        # Matching the set can move the price a long way in either
        # direction - one earlier high image in a folder makes every
        # unflagged write there high - so the change is priced out loud.
        before = cost_per_unit(asked_model, asked_quality, size_label, inputs, prompt)[0]
        after = cost_per_unit(model, quality, size_label, inputs, prompt)[0]
        change = f", {after / before:.1f}x" if before else ""
        print(
            f"  Price per image: ${before:.3f} at {asked_model} quality={asked_quality} -> "
            f"${after:.3f} at the set's {model} quality={quality}{change}.",
            file=sys.stderr,
        )
    return model, quality


def enforce_daily_cap(config: dict, adding: float) -> None:
    """Stop, whatever -y says, if this run would take today's spend over daily_cap.

    The per-call gate cannot see a loop of cheap calls and the daily warning
    only warns. daily_cap in config.yaml is the one limit that holds: -y does
    not bypass it, and neither does batch.
    """
    cap = config.get("daily_cap")
    if cap is None:
        return
    try:
        cap = float(cap)
    except (TypeError, ValueError):
        print(f"Error: daily_cap in {CONFIG_FILE} must be a number of dollars, not {cap!r}.", file=sys.stderr)
        sys.exit(1)
    already = spend_today()
    if already + adding > cap:
        print(
            f"Error: this run would take today's spend to ${already + adding:.2f}, over the daily_cap "
            f"of ${cap:.2f} in {CONFIG_FILE}. Nothing was sent, and -y does not bypass the cap. "
            f"Wait until tomorrow, make the run smaller, or raise daily_cap.",
            file=sys.stderr,
        )
        sys.exit(EXIT_OVER_CAP)


def warn_daily(adding: float) -> None:
    already = spend_today()
    if already + adding >= DAILY_WARN:
        print(
            f"Warning: ${already:.2f} already spent through this skill today; this call adds "
            f"${adding:.2f}. Per-call confirmation does not see a batch - check that the "
            f"quality tier is the one this set needs.",
            file=sys.stderr,
        )


def run_request(job: Job, api_key: str) -> dict:
    """Send one request, save what comes back, and record it. Returns what was saved and billed.

    WRITE-AHEAD. The history row goes down before the request is sent, at the
    estimate, and the final row replaces it once the images are saved. A
    request can take minutes and an agent's shell can kill it on a timeout; a
    record written only at the end loses every run that never reached the end,
    and some of those were billed. Absolute paths, always: a relative "."
    recorded from one directory would match every other directory the CLI is
    later run from, and set_profile would hand back some unrelated folder's
    house tier.
    """
    started = time.monotonic()
    output_path = job.output_path
    pending = HistoryEntry(
        timestamp=datetime.now().isoformat(),
        prompt=job.user_prompt,
        preset=job.preset,
        platform=job.platform,
        model=job.model,
        quality=job.quality,
        size=job.size_label,
        background=job.background,
        output_format=job.output_format,
        provider=job.provider,
        n=job.n,
        output=str(output_path.resolve()),
        output_dir=str(output_path.resolve().parent),
        project=job.project,
        estimated_cost=job.per * job.n,
        inputs=job.inputs,
        actual_cost=None,
        usage=None,
        **set_fields(output_path.resolve().parent),
        id=uuid.uuid4().hex,
        status="pending",
    )
    append_history(pending)

    try:
        images, usage = api_request(
            prompt=job.prompt,
            provider=job.provider,
            api_key=api_key,
            model=job.model,
            size=job.size,
            quality=job.quality,
            n=job.n,
            edit_image=job.edit,
            reference_images=job.reference,
            mask=job.mask,
            background=job.background,
            output_format=job.output_format,
            output_compression=job.output_compression,
            moderation=job.moderation,
        )
        if not images:
            print("Error: No images returned.", file=sys.stderr)
            sys.exit(1)
    except SystemExit:
        # The API answered with an error, or with nothing. Recorded as failed,
        # so the day's total does not carry an image that never came back.
        append_history(replace(pending, status="failed", duration_s=round(time.monotonic() - started, 1)))
        raise

    # Checked again now the images are here: the API can return a different
    # count from the one asked for, and the request can take minutes.
    if not job.overwrite:
        output_path = free_output_path(job.requested_path, len(images))
    paths, contact = output_paths(output_path, len(images))
    saved_paths = []
    for img_data, p in zip(images, paths):
        save_image(img_data, p, overwrite=job.overwrite)
        saved_paths.append(p)
        print(f"Saved {p}")

    # The final row, written before any post-processing, so nothing that can
    # go wrong in ImageMagick can lose the record of what was billed.
    estimated = job.per * len(images)
    actual = cost_from_usage(job.model, usage)
    entry = replace(
        pending,
        n=len(images),
        output=str(saved_paths[0].resolve()),
        output_dir=str(saved_paths[0].resolve().parent),
        estimated_cost=estimated,
        actual_cost=actual,
        usage=usage or None,
        **set_fields(saved_paths[0].resolve().parent),
        status="complete",
        duration_s=round(time.monotonic() - started, 1),
    )
    save_history(entry)
    if job.sidecar:
        for p in saved_paths:
            save_metadata(p, entry)

    if contact and make_contact_sheet(saved_paths, contact) and contact.exists():
        print(f"Saved {contact} (contact sheet)")

    if job.platform_spec:
        target_w, target_h = job.platform_spec["width"], job.platform_spec["height"]
        fitted = [platform_fit(p, target_w, target_h) for p in saved_paths]
        if all(fitted):
            print(f"  Fitted to {target_w}x{target_h} ({job.platform})")
        else:
            # Say what was actually saved. This line used to claim the fit
            # whether or not it had happened, so a 1088x1920 file was reported
            # as 1080x1920.
            for p, ok in zip(saved_paths, fitted):
                if ok:
                    continue
                dims = image_size(p)
                saved = f"{dims[0]}x{dims[1]}" if dims else f"{job.size_label} as generated"
                print(f"  Not fitted to {target_w}x{target_h} ({job.platform}): {p} is {saved}")

    return {"paths": saved_paths, "estimated": estimated, "actual": actual}


def report_billed(result: dict) -> None:
    actual, estimated = result["actual"], result["estimated"]
    if actual is not None:
        drift = ""
        if estimated and abs(actual - estimated) / max(estimated, 1e-9) > 0.25:
            drift = f" (the estimate said ${estimated:.3f})"
        print(f"  Billed: ${actual:.4f}{drift}")
    else:
        print(f"  Est. cost: ${estimated:.3f} (the API returned no usage block)")


def require_api_key(provider: str) -> str:
    api_key = get_api_key(provider)
    if not api_key:
        print(f"Error: No API key found for {provider}.", file=sys.stderr)
        print(
            f"Set {PROVIDERS[provider]['key_env']} or run: scripts/imager.py init",
            file=sys.stderr,
        )
        sys.exit(1)
    return api_key


def check_quality(model: str, quality: str) -> None:
    if quality not in MODELS[model]["qualities"]:
        print(
            f"Error: {model} does not support quality={quality}. It accepts: {', '.join(MODELS[model]['qualities'])}.",
            file=sys.stderr,
        )
        sys.exit(1)


VALID_SUFFIXES = {"png": (".png",), "jpeg": (".jpg", ".jpeg"), "webp": (".webp",)}


def output_for_format(path: Path, output_format: str) -> Path:
    if path.suffix.lower() not in VALID_SUFFIXES[output_format]:
        return path.with_suffix(FORMAT_SUFFIX[output_format])
    return path


def cmd_generate(args):
    config = load_config()
    provider = check_provider(args.provider or config.get("provider", "openai"))
    api_key = require_api_key(provider)

    model = normalise_model(args.model or config.get("model"))
    prompt = compose_prompt(args.prompt, args.preset)

    # DEFAULT LOW, NOT HIGH. This skill's own documented flow is "always
    # generate a draft first", and a default of high contradicts it on every
    # non-interactive call. Ask for high when the image needs it.
    quality = args.quality or config.get("quality", "low")
    n = args.n or 1
    is_draft = getattr(args, "draft", False)

    if is_draft:
        quality = "low"

    check_quality(model, quality)

    platforms = load_platforms()
    platform = None
    platform_spec = None
    if args.platform:
        if args.platform not in platforms:
            print(f"Warning: unknown platform '{args.platform}', skipping resize", file=sys.stderr)
        else:
            platform = args.platform
            platform_spec = platforms[args.platform]

    # Generate at the platform's own aspect rather than generating a square and
    # cropping it. A crop discards pixels that were paid for and re-frames the
    # image after the model has already composed it.
    #
    # Drafts too. A draft is where the composition gets approved, so a draft at
    # a different aspect from the final approves a picture that will never be
    # made. There is no saving in dropping the size: non-square costs less.
    size = size_for(args.size or config.get("size"), platform_spec)

    output_format = args.output_format or "png"
    background = args.background
    if background == "transparent" and output_format == "jpeg":
        print(
            "Note: JPEG has no alpha channel; using png so the transparent background survives.",
            file=sys.stderr,
        )
        output_format = "png"

    output_path = (
        Path(args.output) if args.output else Path(f"./gpt-image-{datetime.now().strftime('%Y%m%d-%H%M%S')}.png")
    )

    if args.project:
        # ~/imager/outputs since the 13 Sep 2026 rename. Nothing moves existing
        # output: these are the user's own images, not skill state, so a project
        # generated before the rename keeps its files under ~/gpt-image-2.
        base_dir = Path.home() / "imager" / "outputs" / args.project
        slug = re.sub(r"[^a-z0-9]+", "-", args.prompt.lower()[:40]).strip("-")
        output_path = base_dir / f"{datetime.now().strftime('%Y%m%d')}-{slug}.png"

    output_path = output_for_format(output_path, output_format)

    # Never write over an existing file unless asked to. See free_output_path.
    overwrite = getattr(args, "overwrite", False)
    requested_path = output_path
    if not overwrite:
        output_path = free_output_path(requested_path, n)
        if output_path != requested_path:
            print(
                f"Note: {requested_path} already exists, so this run writes {output_path} instead. "
                f"Pass --overwrite to replace it.",
                file=sys.stderr,
            )

    size_label = size or "auto"
    inputs = input_count(args.edit, args.reference, args.mask)

    if not is_draft:
        model, quality = match_set(
            output_path, model, quality, bool(args.model), bool(args.quality), size_label, inputs, prompt
        )

    per, basis = cost_per_unit(model, quality, size_label, inputs, prompt)
    cost = per * n
    inputs_label = f", {inputs} input image{'s' if inputs != 1 else ''}" if inputs else ""
    mode_label = "DRAFT" if is_draft else quality.upper()

    if args.dry_run:
        print(f"Mode:      {mode_label}")
        print(f"Prompt:    {prompt}")
        print(f"Provider:  {provider}")
        print(f"Model:     {model}")
        print(f"Quality:   {quality}")
        print(f"Size:      {size_label}")
        if platform_spec:
            print(f"Platform:  {platform} -> fits down to {platform_spec['width']}x{platform_spec['height']}")
        print(f"Format:    {output_format}")
        if background:
            print(f"Background: {background}")
        print(f"N:         {n}")
        print(f"Output:    {output_path}")
        if inputs:
            print(f"Inputs:    {inputs} image{'s' if inputs != 1 else ''}, priced as input tokens")
        print(f"Est. cost: ${cost:.3f} ({basis})")
        return

    if args.estimate:
        print(
            f"Estimated cost ({mode_label}): ${cost:.3f} "
            f"({n} image{'s' if n > 1 else ''} x ~${per:.3f}/image, {model}, "
            f"quality={quality}, size={size_label}{inputs_label}) [{basis}]"
        )
        if MODELS[model]["batch_discount"] and n > 1:
            print(f"  Via the Batch API that same run is ~${cost * BATCH_DISCOUNT:.3f} (50% off, {model}).")
        return

    enforce_daily_cap(config, cost)
    warn_daily(cost)
    if not getattr(args, "yes", False) and cost >= CONFIRM_THRESHOLD:
        print(f"Estimated cost: ${cost:.2f} ({n} x ~${per:.3f}/image, {mode_label}{inputs_label}, {basis})")
        try:
            answer = input("Proceed? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            answer = ""
        if answer not in ("y", "yes"):
            print("Cancelled.", file=sys.stderr)
            sys.exit(0)

    print(
        f"Generating {mode_label} with {provider}/{model} "
        f"(quality: {quality}, size: {size_label}, format: {output_format}"
        f"{', background: ' + background if background else ''})...",
        file=sys.stderr,
    )

    job = Job(
        user_prompt=args.prompt,
        prompt=prompt,
        preset=args.preset,
        platform=platform,
        platform_spec=platform_spec,
        provider=provider,
        model=model,
        quality=quality,
        size=size,
        background=background,
        output_format=output_format,
        output_compression=args.output_compression,
        moderation=args.moderation,
        n=n,
        edit=args.edit,
        reference=args.reference,
        mask=args.mask,
        project=args.project,
        requested_path=requested_path,
        output_path=output_path,
        overwrite=overwrite,
        sidecar=getattr(args, "sidecar", False),
        inputs=inputs,
        per=per,
        basis=basis,
    )
    report_billed(run_request(job, api_key))


# ---------- Again ----------


def cmd_again(args):
    last = load_last_run()
    if not last:
        print("No previous run found.", file=sys.stderr)
        sys.exit(1)
    print(f"Re-running: \"{last['prompt']}\"")
    args.prompt = last["prompt"]
    args.preset = last.get("preset")
    args.platform = last.get("platform")
    args.provider = last.get("provider", "openai")
    args.model = last.get("model")
    args.n = last.get("n", 1)
    args.quality = last.get("quality", "low")
    args.size = last.get("size")
    args.background = last.get("background")
    args.output_format = last.get("output_format", "png")
    args.output_compression = None
    args.moderation = None
    args.edit = None
    args.reference = None
    args.mask = None
    args.project = last.get("project")
    args.dry_run = False
    args.estimate = False
    args.draft = False
    args.yes = False
    args.output = None
    args.overwrite = False
    args.sidecar = False
    cmd_generate(args)


# ---------- Batch ----------

# What a batch row may say. Anything else is refused by name rather than
# dropped: a key that is quietly ignored is a setting its author thinks applied.
BATCH_ROW_KEYS = (
    "prompt",
    "output",
    "preset",
    "platform",
    "size",
    "edit",
    "reference",
    "mask",
    "background",
    "output_format",
)
BATCH_MAX_CONCURRENCY = 16
# Stop starting new rows after this many failures in a row. A bad key or a
# refused preset fails every row the same way, and costs a request each time.
BATCH_FAILURE_LIMIT = 5


def load_batch(path: str) -> list:
    """The rows of a batch file, each checked. Exits with every problem listed, before anything is priced."""
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError as e:
        print(f"Error: cannot read {path}: {e}", file=sys.stderr)
        sys.exit(1)
    presets, platforms = load_presets(), load_platforms()
    rows, problems, outputs = [], [], {}
    for number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError as e:
            problems.append(f"line {number}: not valid JSON ({e})")
            continue
        if not isinstance(row, dict):
            problems.append(f"line {number}: not a JSON object")
            continue
        unknown = sorted(set(row) - set(BATCH_ROW_KEYS))
        if unknown:
            problems.append(
                f"line {number}: unknown key {', '.join(unknown)} (a row may carry {', '.join(BATCH_ROW_KEYS)})"
            )
        for key in ("prompt", "output"):
            if not isinstance(row.get(key), str) or not row.get(key).strip():
                problems.append(f"line {number}: '{key}' is required and must be text")
        wrong_type = [k for k in BATCH_ROW_KEYS if k != "reference" and k in row and not isinstance(row[k], str)]
        for key in wrong_type:
            if key not in ("prompt", "output"):
                problems.append(f"line {number}: '{key}' must be text")
            row.pop(key)
        if row.get("preset") and row["preset"] not in presets:
            problems.append(f"line {number}: unknown preset '{row['preset']}'")
        if row.get("platform") and row["platform"] not in platforms:
            problems.append(f"line {number}: unknown platform '{row['platform']}'")
        if row.get("size") and row["size"] != "auto":
            match = re.fullmatch(r"(\d+)x(\d+)", str(row["size"]))
            problem = size_problem(int(match.group(1)), int(match.group(2))) if match else "use WIDTHxHEIGHT"
            if problem:
                problems.append(f"line {number}: size {row['size']} - {problem}")
        if row.get("output_format") and row["output_format"] not in FORMAT_SUFFIX:
            problems.append(f"line {number}: output_format must be one of {', '.join(FORMAT_SUFFIX)}")
        if row.get("background") and row["background"] not in ("transparent", "opaque", "auto"):
            problems.append(f"line {number}: background must be transparent, opaque or auto")
        reference = row.get("reference")
        if isinstance(reference, str):
            row["reference"] = [reference]
        elif reference is not None and not (isinstance(reference, list) and all(isinstance(r, str) for r in reference)):
            problems.append(f"line {number}: reference must be a path or a list of paths")
            row["reference"] = None
        if row.get("mask") and not row.get("edit"):
            problems.append(f"line {number}: mask needs edit")
        for missing in missing_inputs(row.get("edit"), row.get("reference"), row.get("mask")):
            problems.append(f"line {number}: input file not found: {missing}")
        if isinstance(row.get("output"), str):
            fmt = row.get("output_format") or "png"
            if row.get("background") == "transparent" and fmt == "jpeg":
                fmt = "png"
            row["output_format"] = fmt if fmt in FORMAT_SUFFIX else "png"
            key = str(output_for_format(Path(row["output"]), row["output_format"]).resolve())
            if key in outputs:
                problems.append(f"line {number}: same output as line {outputs[key]}")
            outputs.setdefault(key, number)
        row["line"] = number
        rows.append(row)
    if problems:
        print(f"Error: {path} has {len(problems)} problem(s). Nothing was sent.", file=sys.stderr)
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        sys.exit(1)
    if not rows:
        print(f"Error: {path} has no rows.", file=sys.stderr)
        sys.exit(1)
    return rows


def cmd_batch(args):
    """Run a JSONL file of images through every guard a single run has, once.

    THE HOLE THIS CLOSES. The per-call gate cannot see a loop of cheap calls,
    and a script that calls the API itself gets no set check, no daily total
    and no record. So a batch is one command: one set check per directory, one
    total price, one confirmation, the daily cap, a history row per image, and
    a rerun that skips every output already on disk.
    """
    config = load_config()
    provider = check_provider(config.get("provider", "openai"))
    api_key = require_api_key(provider)
    rows = load_batch(args.file)
    model = normalise_model(args.model or config.get("model"))
    quality = args.quality or config.get("quality", "low")
    check_quality(model, quality)
    concurrency = args.concurrency
    if not 1 <= concurrency <= BATCH_MAX_CONCURRENCY:
        print(f"Error: --concurrency must be between 1 and {BATCH_MAX_CONCURRENCY}", file=sys.stderr)
        sys.exit(1)
    platforms = load_platforms()

    jobs, skipped = [], []
    for row in rows:
        output_path = output_for_format(Path(row["output"]), row["output_format"])
        if output_path.exists():
            # RESUME. An output on disk is a row already paid for.
            skipped.append(output_path)
            continue
        platform = row.get("platform")
        platform_spec = platforms[platform] if platform else None
        size = size_for(row.get("size") or config.get("size"), platform_spec)
        jobs.append(
            Job(
                user_prompt=row["prompt"],
                prompt=compose_prompt(row["prompt"], row.get("preset")),
                preset=row.get("preset"),
                platform=platform,
                platform_spec=platform_spec,
                provider=provider,
                model=model,
                quality=quality,
                size=size,
                background=row.get("background"),
                output_format=row["output_format"],
                output_compression=None,
                moderation=None,
                n=1,
                edit=row.get("edit"),
                reference=row.get("reference"),
                mask=row.get("mask"),
                project=None,
                requested_path=output_path,
                output_path=output_path,
                overwrite=False,
                sidecar=False,
                inputs=input_count(row.get("edit"), row.get("reference"), row.get("mask")),
            )
        )

    # One set check per directory, not per row: a set is a folder.
    by_dir: dict = {}
    for job in jobs:
        by_dir.setdefault(job.output_path.resolve().parent, []).append(job)
    for group in by_dir.values():
        first = group[0]
        set_model, set_quality = match_set(
            first.output_path,
            model,
            quality,
            bool(args.model),
            bool(args.quality),
            first.size_label,
            first.inputs,
            first.prompt,
        )
        for job in group:
            job.model, job.quality = set_model, set_quality
            job.per, job.basis = cost_per_unit(job.model, job.quality, job.size_label, job.inputs, job.prompt)

    total = sum(job.per * job.n for job in jobs)
    if skipped:
        print(f"Skipping {len(skipped)} row(s) whose output already exists.")
    if not jobs:
        print("Nothing to do: every output in the batch already exists.")
        return
    if args.dry_run:
        for directory, group in by_dir.items():
            subtotal = sum(job.per * job.n for job in group)
            print(
                f"{directory}: {len(group)} image(s), {group[0].model} quality={group[0].quality}, "
                f"${subtotal:.3f} ({', '.join(sorted({job.basis for job in group}))})"
            )
    print(f"Est. total: ${total:.3f} for {len(jobs)} image(s)")
    if args.dry_run or args.estimate:
        return

    enforce_daily_cap(config, total)
    warn_daily(total)
    if not args.yes and total >= CONFIRM_THRESHOLD:
        try:
            answer = input(f"Proceed with {len(jobs)} image(s) for about ${total:.2f}? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            answer = ""
        if answer not in ("y", "yes"):
            print("Cancelled. Nothing was sent.", file=sys.stderr)
            sys.exit(EXIT_CANCELLED)

    print(f"Running {len(jobs)} image(s), {concurrency} at a time...", file=sys.stderr)
    lock = threading.Lock()
    state = {"done": 0, "failed": 0, "in_a_row": 0, "billed": 0.0, "estimated": 0.0, "stopped": False}

    def work(job: Job) -> None:
        with lock:
            if state["stopped"]:
                return
        try:
            result = run_request(job, api_key)
        except (SystemExit, Exception) as e:
            # SystemExit is how api_request reports an API error, already
            # printed. Anything else is unexpected, so it is named here rather
            # than lost in a worker thread. Either way the row is counted and
            # the rest carry on.
            detail = "" if isinstance(e, SystemExit) else f": {type(e).__name__}: {e}"
            with lock:
                print(f"  Failed: {job.output_path}{detail}", file=sys.stderr)
                state["failed"] += 1
                state["in_a_row"] += 1
                if state["in_a_row"] >= BATCH_FAILURE_LIMIT and not state["stopped"]:
                    state["stopped"] = True
                    print(
                        f"Stopping: {BATCH_FAILURE_LIMIT} rows failed in a row. Fix the cause and rerun; "
                        f"finished outputs are skipped.",
                        file=sys.stderr,
                    )
            return
        with lock:
            state["done"] += 1
            state["in_a_row"] = 0
            state["estimated"] += result["estimated"]
            state["billed"] += result["actual"] if result["actual"] is not None else result["estimated"]

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        list(pool.map(work, jobs))

    not_run = len(jobs) - state["done"] - state["failed"]
    print(
        f"Batch finished: {state['done']} generated, {len(skipped)} skipped (already existed), "
        f"{state['failed']} failed{f', {not_run} not started' if not_run else ''}. "
        f"Billed ${state['billed']:.4f} (estimated ${state['estimated']:.4f})."
    )
    if state["failed"] or not_run:
        sys.exit(1)


# ---------- Set check ----------


def cmd_set_check(args):
    """What model and quality did this set use, and what would a batch cost?

    STEP 0 OF SKILL.md. Run this before adding to any directory that already
    has images, including - especially - from a script that calls the API
    itself and never touches the rest of this file.
    """
    target = Path(args.path)
    probe = target if target.suffix else target / "x.png"
    house = set_profile(probe)
    where = target if target.suffix == "" else target.parent
    model = house["model"] or DEFAULT_MODEL
    if not house["count"]:
        print(
            f"{where}: no history for this directory. It is a new set, so YOU are choosing "
            f"the house tier. Choose low unless the output is displayed large."
        )
    else:
        print(f"{where}: {house['count']} image(s) previously made at quality={house['quality']} on {house['model']}.")
        print(
            "MATCH BOTH. A set at two tiers does not look like one set, a set on two models "
            "even less so, and the difference is usually invisible anyway once the image is "
            "scaled to its published size."
        )
    spent = spend_today()
    if spent:
        print(f"\nSpent through this skill today: ${spent:.2f} (does NOT include scripts that call the API directly).")
    batch = args.n or 100
    print(f"\nA batch of {batch:,} on {model} would cost:")
    for tier in MODELS[model]["qualities"]:
        if tier == "auto":
            continue
        per, basis = cost_per_unit(model, tier, "1024x1024")
        mark = "  <- the set's tier" if tier == house["quality"] else ""
        print(f"   {tier:7} ${per * batch:>10,.2f}  [{basis}]{mark}")
    if MODELS[model]["batch_discount"]:
        print(f"\n   {model} is eligible for the Batch API's 50% discount on a run that size.")
    return 0


# ---------- History ----------


def cmd_history(args):
    entries = load_history(n=args.n, project=args.history_project)
    if not entries:
        print("No history found.")
        return
    for e in entries:
        ts = e["timestamp"][:19]
        prompt = e["prompt"][:46]
        billed = entry_cost(e)
        # ~ marks an estimate; a bare figure is what the API said it billed.
        marker = "" if e.get("actual_cost") else "~"
        cost = f"{marker}${billed:.3f}" if billed else "?"
        status = e.get("status")
        if status == "failed":
            cost = "failed"
        preset = f" [{e['preset']}]" if e.get("preset") else ""
        if status == "pending":
            # Sent, and no result was ever recorded: killed, or timed out.
            preset += " [pending: no result recorded, counted at the estimate]"
        q = e.get("quality", "?")
        model = (e.get("model") or "gpt-image-2").replace("gpt-image-", "")
        print(f"  {ts}  {cost:>8s}  {model:<14s} q:{q:<6s}  {prompt}{preset}")


# ---------- Retired flags ----------

# --seed and --thinking were carried over from the upstream skill, which
# documented both and sent neither. Neither parameter exists anywhere in the
# OpenAI image API: both are absent from CreateImageRequest and
# CreateImageEditRequest in openai/openai-openapi, and from the whole image
# generation guide. --thinking was worse than inert, because the cost estimate
# multiplied by it, so the confirmation gate and the daily warning both fired
# on numbers describing a request nobody had ever sent.
#
# They fail loudly rather than being quietly dropped from the parser, because a
# script that still passes --seed is a script whose author believes composition
# is being locked between draft and final. Silence would leave that belief
# intact.

RETIRED_FLAGS = {
    "--seed": (
        "The image API has no seed parameter - not on generations, not on edits. "
        "The old skill accepted --seed, logged it, and never sent it, so no run was "
        "ever reproducible.\n"
        "       For consistency across a set, carry it in the prompt instead: name the "
        "palette, the lighting, the camera and the recurring subject's 5-tuple (age, "
        "appearance, hairstyle, distinctive features, clothing) in every prompt, and use "
        "--reference to pin the look to an image you have already accepted."
    ),
    "--thinking": (
        "The image API has no thinking parameter. The old skill priced it into the cost "
        "estimate and never sent it.\n"
        "       For complex layouts and dense text, raise --quality instead (the 2.5 models "
        "add xhigh and max), or use --model sunburst, which is the one built for fine "
        "detail and typography."
    ),
}


def reject_retired_flags(argv: list) -> None:
    for flag, why in RETIRED_FLAGS.items():
        if flag in argv or any(a.startswith(flag + "=") for a in argv):
            print(f"Error: {flag} has been removed. {why}", file=sys.stderr)
            sys.exit(2)


# ---------- CLI ----------


def main():
    reject_retired_flags(sys.argv[1:])

    simple = {
        "init": cmd_init,
        "list-models": cmd_list_models,
        "list-presets": cmd_list_presets,
        "list-platforms": cmd_list_platforms,
    }
    if len(sys.argv) > 1 and sys.argv[1] in simple:
        simple[sys.argv[1]]()
        return

    parser = argparse.ArgumentParser(
        description="GPT Image - OpenAI Image Generation",
        epilog="Commands: init, list-models, list-presets, list-platforms, again, history, set-check, batch",
    )
    sub = parser.add_subparsers(dest="command")

    gen_parser = argparse.ArgumentParser(
        prog="imager.py",
        description="GPT Image - Generate images from text prompts",
    )
    gen_parser.add_argument("prompt", nargs="?", help="Text prompt for image generation")
    gen_parser.add_argument("output", nargs="?", help="Output file path (default: auto-named)")
    gen_parser.add_argument("--model", help=f"Model or alias (default: {DEFAULT_MODEL})")
    gen_parser.add_argument("--preset", help="Style preset name")
    gen_parser.add_argument("--platform", help="Platform preset: sets the generation aspect, then fits down")
    gen_parser.add_argument("--provider", help=f"API provider ({', '.join(PROVIDERS)})")
    gen_parser.add_argument("--quality", choices=QUALITY_CHOICES, help="Image quality (xhigh/max: 2.5 models only)")
    gen_parser.add_argument("--size", help="WIDTHxHEIGHT, both multiples of 16, or 'auto'")
    gen_parser.add_argument("--n", type=int, help="Number of variants (1-10)")
    gen_parser.add_argument("--edit", help="Path to image to edit")
    gen_parser.add_argument("--reference", action="append", help="Reference image for style (repeatable)")
    gen_parser.add_argument("--mask", help="PNG mask: transparent areas mark what to replace")
    gen_parser.add_argument("--background", choices=("transparent", "opaque", "auto"), help="Image background")
    gen_parser.add_argument(
        "--output-format", dest="output_format", choices=("png", "jpeg", "webp"), help="File format"
    )
    gen_parser.add_argument("--output-compression", dest="output_compression", type=int, help="0-100, jpeg/webp only")
    gen_parser.add_argument("--moderation", choices=("low", "auto"), help="Content filter strictness")
    gen_parser.add_argument("--project", help="Project name for organized output")
    gen_parser.add_argument("--dry-run", action="store_true", help="Preview prompt without API call")
    gen_parser.add_argument("--estimate", action="store_true", help="Show cost estimate only")
    gen_parser.add_argument("--draft", action="store_true", help="Draft mode: low quality, ~$0.006/image")
    gen_parser.add_argument("-y", "--yes", action="store_true", help="Skip cost confirmation prompt")
    gen_parser.add_argument(
        "--overwrite", action="store_true", help="Replace an existing output file (default: write name-2, name-3 ...)"
    )
    gen_parser.add_argument(
        "--sidecar", action="store_true", help="Also write <image>.json with the run's record (never overwrites)"
    )

    sub.add_parser("again", help="Re-run last generation")

    sub_history = sub.add_parser("history", help="Show generation history")
    sub_history.add_argument("-n", type=int, default=20, help="Number of entries to show")
    sub_history.add_argument("--project", dest="history_project", help="Filter by project")

    sub_batch = sub.add_parser("batch", help="Run a JSONL file of images: one set check, one price, one confirmation")
    sub_batch.add_argument("file", help="JSONL, one image per line: prompt and output required")
    sub_batch.add_argument("--model", help=f"Model or alias (default: {DEFAULT_MODEL}, or the set's)")
    sub_batch.add_argument(
        "--quality", choices=QUALITY_CHOICES, help="Quality for every row (default: the set's, or low)"
    )
    sub_batch.add_argument("--concurrency", type=int, default=4, help="Requests in flight at once (default 4)")
    sub_batch.add_argument("--dry-run", action="store_true", help="Show the plan and the total without calling out")
    sub_batch.add_argument("--estimate", action="store_true", help="Show the total only")
    sub_batch.add_argument("-y", "--yes", action="store_true", help="Skip the confirmation (not the daily_cap)")

    sub_set = sub.add_parser("set-check", help="STEP 0: what model and quality did this directory's set use?")
    sub_set.add_argument("path", help="Directory or file the images will be written to")
    sub_set.add_argument("-n", type=int, default=100, help="Batch size to cost out")

    if len(sys.argv) <= 1 or sys.argv[1] in ("-h", "--help"):
        parser.print_help()
        return

    if sys.argv[1] not in ("again", "history", "set-check", "batch"):
        args = gen_parser.parse_args()
        if not args.prompt:
            gen_parser.print_help()
            sys.exit(1)
        if args.n is not None and (args.n < 1 or args.n > 10):
            print("Error: --n must be between 1 and 10", file=sys.stderr)
            sys.exit(1)
        if args.output_compression is not None:
            if not 0 <= args.output_compression <= 100:
                print("Error: --output-compression must be between 0 and 100", file=sys.stderr)
                sys.exit(1)
            if (args.output_format or "png") == "png":
                print(
                    "Error: --output-compression applies to jpeg and webp only. "
                    "Add --output-format webp, or drop the flag.",
                    file=sys.stderr,
                )
                sys.exit(1)
        if args.mask and not args.edit:
            print(
                "Error: --mask needs --edit: the mask says which part of that image to replace.",
                file=sys.stderr,
            )
            sys.exit(1)
        if args.moderation and (args.edit or args.reference or args.mask):
            # A flag that does not reach the wire must error rather than be
            # dropped (AGENTS.md, constraint 4). Edits and reference runs go to
            # the edit endpoint, and CreateImageEditRequest has no moderation.
            print(
                "Error: --moderation applies to generations only. --edit and --reference use the "
                "edit endpoint, which has no moderation setting. Drop the flag, or change the "
                "prompt or the input images if an edit was blocked.",
                file=sys.stderr,
            )
            sys.exit(1)
        cmd_generate(args)
    else:
        args = parser.parse_args()
        if args.command == "again":
            cmd_again(args)
        elif args.command == "history":
            cmd_history(args)
        elif args.command == "set-check":
            cmd_set_check(args)
        elif args.command == "batch":
            cmd_batch(args)
        else:
            parser.print_help()


if __name__ == "__main__":
    main()
