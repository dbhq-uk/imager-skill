#!/usr/bin/env python3
"""Tests for imager.py.

Hermetic by construction: the only function that touches the network is
api_request, and nothing here calls it. The CLI cases run --dry-run, which
returns before the request is built (but *after* the API-key check, hence the
dummy key in the subprocess environment).

The catalogue-shape tests are the point of this file as much as the unit tests are.
presets.json and platforms.json are the skill's real configuration surface --
a preset that loses its {subject} placeholder silently generates an image of
the style description instead of the user's subject, and nothing else in the
repo would catch it.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import io
import json
import os
import shlex
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
import urllib.request
import zlib
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

# Set before the import, so the module never resolves the real settings
# directory and nothing here can read or write the owner's history.
_IMPORT_HOME = tempfile.TemporaryDirectory()
os.environ["GPT_IMAGE_HOME"] = _IMPORT_HOME.name

import imager  # noqa: E402

SCRIPT = Path(__file__).parent.parent / "scripts" / "imager.py"

# Cost estimates and set-check both read history.jsonl, so the subprocess cases
# have to run against an empty one. Without this they pass or fail depending on
# what the machine's owner happened to generate into the working directory.
_CLI_HOME = tempfile.TemporaryDirectory()


def tiny_png(width: int = 4, height: int = 4) -> bytes:
    """A real, valid PNG of the given size, so ImageMagick can read what a fake API returns."""

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    raw = b"".join(b"\x00" + b"\xff\x80\x00" * width for _ in range(height))
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


class IsolatedHome(unittest.TestCase):
    """Points config, history and the last-run record at a fresh temp directory."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self._saved = (imager.CONFIG_DIR, imager.CONFIG_FILE, imager.HISTORY_FILE, imager.LAST_RUN_FILE)
        home = self.root / "settings"
        imager.CONFIG_DIR = home
        imager.CONFIG_FILE = home / "config.yaml"
        imager.HISTORY_FILE = home / "history.jsonl"
        imager.LAST_RUN_FILE = home / "last.json"

    def tearDown(self):
        (imager.CONFIG_DIR, imager.CONFIG_FILE, imager.HISTORY_FILE, imager.LAST_RUN_FILE) = self._saved
        self.tmp.cleanup()

    def generate(self, *argv: str, png: bytes | None = None, usage: dict | None = None, during=None):
        """Run the real CLI in-process with the API replaced by a fake.

        Returns (exit code, stdout, stderr, the keyword arguments the fake
        received). Nothing leaves the machine: api_request is the only function
        that calls out, and it is the thing replaced. `during`, if given, runs
        inside the fake request - to look at the history mid-flight, or to
        raise the way a killed or refused request would.
        """
        calls: list = []
        image = base64.b64encode(png or tiny_png()).decode()

        def fake_api_request(**kwargs):
            calls.append(kwargs)
            if during:
                during()
            return [image] * kwargs.get("n", 1), (usage if usage is not None else {})

        out, err, code = io.StringIO(), io.StringIO(), 0
        # ExitStack rather than a parenthesised with: the floor is Python 3.9.
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(imager, "api_request", side_effect=fake_api_request))
            stack.enter_context(mock.patch.object(sys, "argv", ["imager.py", *argv]))
            stack.enter_context(mock.patch.dict(os.environ, {"OPENAI_API_KEY": "test-key-not-real"}))
            stack.enter_context(contextlib.redirect_stdout(out))
            stack.enter_context(contextlib.redirect_stderr(err))
            try:
                imager.main()
            except SystemExit as exc:
                code = exc.code if isinstance(exc.code, int) else 1
        return code, out.getvalue(), err.getvalue(), calls

    def history_rows(self) -> list:
        if not imager.HISTORY_FILE.exists():
            return []
        return [json.loads(line) for line in imager.HISTORY_FILE.read_text().splitlines() if line.strip()]


def git(*args: str, cwd: Path) -> str:
    """Run git with a throwaway identity, so the suite works on a runner with no git config."""
    result = subprocess.run(
        [
            "git",
            "-c",
            "user.name=imager tests",
            "-c",
            "user.email=tests@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "-c",
            "init.defaultBranch=main",
            *args,
        ],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def make_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    git("init", "-q", cwd=path)
    git("commit", "-q", "--allow-empty", "-m", "init", cwd=path)
    return path


def run_cli(*args: str, env_extra: dict | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["OPENAI_API_KEY"] = "test-key-not-real"
    env["GPT_IMAGE_HOME"] = _CLI_HOME.name
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        env=env,
        cwd=_CLI_HOME.name,
    )


class TestModels(unittest.TestCase):
    def test_default_model_is_a_known_model(self):
        self.assertIn(imager.DEFAULT_MODEL, imager.MODELS)

    def test_aliases_resolve_to_canonical_names(self):
        for canonical, info in imager.MODELS.items():
            for alias in info["aliases"]:
                with self.subTest(alias=alias):
                    self.assertEqual(imager.normalise_model(alias), canonical)

    def test_empty_model_falls_back_to_the_default(self):
        self.assertEqual(imager.normalise_model(None), imager.DEFAULT_MODEL)

    def test_unknown_model_exits_nonzero(self):
        with self.assertRaises(SystemExit):
            imager.normalise_model("gpt-image-9000")

    def test_every_model_has_token_prices(self):
        for name in imager.MODELS:
            with self.subTest(model=name):
                self.assertIn(name, imager.TOKEN_PRICES)

    def test_only_imager_supports_the_batch_discount(self):
        # The 50% batch rate is published for gpt-image-2 and not for the 2.5
        # models; claiming otherwise would under-quote a batch run.
        self.assertTrue(imager.MODELS["gpt-image-2"]["batch_discount"])
        self.assertFalse(imager.MODELS["gpt-image-2.5-flare"]["batch_discount"])

    def test_xhigh_and_max_are_25_only(self):
        self.assertNotIn("xhigh", imager.MODELS["gpt-image-2"]["qualities"])
        self.assertIn("xhigh", imager.MODELS["gpt-image-2.5-flare"]["qualities"])


class TestCostModel(unittest.TestCase):
    """The estimate is priced from tokens: text in, images in, image out."""

    def setUp(self):
        # The measured figures read history, so point it at an empty file.
        self.tmp = tempfile.TemporaryDirectory()
        self._saved = imager.HISTORY_FILE
        imager.HISTORY_FILE = Path(self.tmp.name) / "history.jsonl"

    def tearDown(self):
        imager.HISTORY_FILE = self._saved
        self.tmp.cleanup()

    def out_rate(self, model: str = imager.DEFAULT_MODEL) -> float:
        return imager.TOKEN_PRICES[model]["image_out"] / 1_000_000

    def write_rows(self, *rows: dict) -> None:
        imager.HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
        with imager.HISTORY_FILE.open("a") as f:
            for row in rows:
                f.write(json.dumps(row) + "\n")

    def run_row(self, output_tokens: int, size: str = "1024x1024", n: int = 1, inputs: int = 0, **kw) -> dict:
        row = {
            "timestamp": "2026-09-12T10:00:00",
            "model": "gpt-image-2.5-flare",
            "quality": "medium",
            "size": size,
            "n": n,
            "inputs": inputs,
            "usage": {"output_tokens": output_tokens * n},
        }
        row.update(kw)
        return row

    # --- the two failures the issue names ---

    def test_an_edit_with_one_input_image_is_priced_with_it(self):
        # Flare low with one reference bills $0.0114-0.015. Output-only pricing
        # quoted $0.0034, which is the under-quote the gate cannot survive.
        for size in ("auto", "1024x1024"):
            with self.subTest(size=size):
                per, _ = imager.cost_per_unit("gpt-image-2.5-flare", "low", size, inputs=1, prompt="a subject")
                self.assertGreaterEqual(per, 0.015)

    def test_flare_high_square_is_not_quoted_at_gpt_image_2s_price(self):
        # It bills 1,756 output tokens, about $0.053. The old ceiling said $0.211.
        per, _ = imager.cost_per_unit("gpt-image-2.5-flare", "high", "1024x1024", prompt="a subject")
        self.assertLess(per, 0.10)
        self.assertGreaterEqual(per, 1756 * self.out_rate())

    def test_sunburst_xhigh_three_images_is_quoted_near_its_bill(self):
        # Billed about $0.156 for three at 1536x768; the old ceiling said $1.20.
        cost = imager.estimate_cost("gpt-image-2.5-sunburst", "xhigh", "1536x768", 3, prompt="a subject")
        self.assertGreaterEqual(cost, 3 * 1628 * self.out_rate())
        self.assertLess(cost, 0.156 * 1.5)

    # --- the parts of the price ---

    def test_gpt_image_2_matches_its_published_prices(self):
        for quality, published in (("low", 0.006), ("medium", 0.053), ("high", 0.211)):
            with self.subTest(quality=quality):
                per, basis = imager.cost_per_unit("gpt-image-2", quality, "1024x1024")
                self.assertEqual(basis, "published")
                # Published prices are rounded to a tenth of a cent.
                self.assertGreaterEqual(per, published - 0.0005)
                self.assertLess(per, published * 1.05)

    def test_each_input_image_is_billed_once_per_output_image(self):
        none = imager.estimate_cost(imager.DEFAULT_MODEL, "low", "1024x1024", 3)
        two = imager.estimate_cost(imager.DEFAULT_MODEL, "low", "1024x1024", 3, inputs=2)
        rate = imager.TOKEN_PRICES[imager.DEFAULT_MODEL]["image_in"] / 1_000_000
        self.assertAlmostEqual(two - none, 3 * 2 * imager.INPUT_IMAGE_TOKENS * rate)

    def test_the_prompt_text_is_priced(self):
        short, _ = imager.cost_per_unit(imager.DEFAULT_MODEL, "low", "1024x1024", prompt="a cat")
        long, _ = imager.cost_per_unit(imager.DEFAULT_MODEL, "low", "1024x1024", prompt="a cat " * 300)
        self.assertGreater(long, short)

    def test_the_size_rule_reads_at_or_just_above_every_measured_size(self):
        # Output tokens at each size, as a share of the same tier at 1024x1024,
        # as the API actually returned them.
        measured = {
            "1024x1344": 0.832,
            "1536x960": 0.712,
            "1920x1088": 0.755,
            "1536x864": 0.614,
            "2400x800": 0.429,
            "1792x608": 0.338,
            "1536x512": 0.286,
        }
        for size, share in measured.items():
            with self.subTest(size=size):
                factor, inside = imager.size_factor(size)
                self.assertTrue(inside)
                self.assertGreaterEqual(factor, share)
                self.assertLess(factor, share * 1.1)

    def test_non_square_is_cheaper_than_square_at_the_same_quality(self):
        square, _ = imager.cost_per_unit("gpt-image-2", "high", "1024x1024")
        portrait, _ = imager.cost_per_unit("gpt-image-2", "high", "1024x1536")
        self.assertLess(portrait, square)

    def test_auto_size_is_priced_above_a_square(self):
        self.assertGreater(imager.size_factor("auto")[0], 1.17)
        self.assertEqual(imager.size_factor(None)[0], imager.AUTO_SIZE_FACTOR)

    def test_a_size_beyond_the_measured_range_is_an_upper_bound(self):
        factor, inside = imager.size_factor("3840x2160")
        self.assertFalse(inside)
        self.assertGreaterEqual(factor, 3840 * 2160 / (1024 * 1024))
        _, basis = imager.cost_per_unit(imager.DEFAULT_MODEL, "high", "3840x2160")
        self.assertEqual(basis, "upper bound")

    def test_unknown_quality_does_not_under_quote(self):
        cost, basis = imager.cost_per_unit("gpt-image-2", "ludicrous", "1024x1024")
        self.assertGreaterEqual(cost, imager.cost_per_unit("gpt-image-2", "high", "1024x1024")[0])
        self.assertEqual(basis, "upper bound")

    def test_every_model_prices_every_tier_it_accepts(self):
        for model, info in imager.MODELS.items():
            for quality in info["qualities"]:
                with self.subTest(model=model, quality=quality):
                    self.assertIn(quality, imager.OUTPUT_TOKENS[model])

    def test_scales_linearly_with_n(self):
        single = imager.estimate_cost("gpt-image-2", "medium", "1024x1024", 1)
        self.assertAlmostEqual(imager.estimate_cost("gpt-image-2", "medium", "1024x1024", 4), single * 4)

    def test_zero_images_costs_nothing(self):
        self.assertEqual(imager.estimate_cost("gpt-image-2", "high", "1024x1024", 0), 0.0)

    # --- calibration from history ---

    def test_measured_output_tokens_need_three_samples(self):
        self.write_rows(self.run_row(900), self.run_row(900))
        self.assertIsNone(imager.measured_output_tokens("gpt-image-2.5-flare", "medium", "1024x1024", "generation"))
        self.write_rows(self.run_row(900))
        self.assertEqual(imager.measured_output_tokens("gpt-image-2.5-flare", "medium", "1024x1024", "generation"), 900)

    def test_a_measured_count_replaces_the_table_at_a_fixed_size(self):
        self.write_rows(*[self.run_row(900)] * 3)
        per, basis = imager.cost_per_unit("gpt-image-2.5-flare", "medium", "1024x1024")
        self.assertEqual(basis, "measured")
        self.assertAlmostEqual(per, 900 * self.out_rate())

    def test_measured_tokens_are_per_image_not_per_call(self):
        self.write_rows(*[self.run_row(900, n=4)] * 3)
        self.assertEqual(imager.measured_output_tokens("gpt-image-2.5-flare", "medium", "1024x1024", "generation"), 900)

    def test_edits_and_generations_are_calibrated_apart(self):
        self.write_rows(*[self.run_row(900, inputs=1)] * 3)
        self.assertIsNone(imager.measured_output_tokens("gpt-image-2.5-flare", "medium", "1024x1024", "generation"))
        self.assertEqual(imager.measured_output_tokens("gpt-image-2.5-flare", "medium", "1024x1024", "edit"), 900)

    def test_rows_without_an_inputs_field_are_judged_by_their_usage(self):
        row = self.run_row(900)
        del row["inputs"]
        row["usage"]["input_tokens_details"] = {"image_tokens": 1500, "text_tokens": 40}
        self.write_rows(*[row] * 3)
        self.assertEqual(imager.measured_output_tokens("gpt-image-2.5-flare", "medium", "1024x1024", "edit"), 900)

    def test_the_highest_recent_sample_is_used(self):
        self.write_rows(self.run_row(800), self.run_row(900), self.run_row(850))
        self.assertEqual(imager.measured_output_tokens("gpt-image-2.5-flare", "medium", "1024x1024", "generation"), 900)

    def test_small_runs_at_auto_cannot_pull_the_estimate_below_the_table(self):
        # At size=auto the API chooses the size, so the count moves from run to
        # run. Three small ones must not set the price for the next large one.
        self.write_rows(*[self.run_row(86, size="auto", quality="low")] * 3)
        table, _ = imager.table_output_tokens("gpt-image-2.5-flare", "low", "auto")
        per, basis = imager.cost_per_unit("gpt-image-2.5-flare", "low", "auto")
        self.assertNotEqual(basis, "measured")
        self.assertAlmostEqual(per, table * self.out_rate())


class TestCostFromUsage(unittest.TestCase):
    """The only figure that is not a guess."""

    USAGE = {
        "total_tokens": 4310,
        "input_tokens": 150,
        "output_tokens": 4160,
        "input_tokens_details": {"text_tokens": 50, "image_tokens": 100},
    }

    def test_prices_each_token_class_at_its_own_rate(self):
        # 50 text @ $5/M + 100 image-in @ $8/M + 4160 image-out @ $30/M
        expected = (50 * 5 + 100 * 8 + 4160 * 30) / 1_000_000
        self.assertAlmostEqual(imager.cost_from_usage("gpt-image-2", self.USAGE), expected)

    def test_missing_breakdown_prices_input_at_the_dearer_rate(self):
        usage = {"input_tokens": 150, "output_tokens": 4160}
        expected = (150 * 8 + 4160 * 30) / 1_000_000
        self.assertAlmostEqual(imager.cost_from_usage("gpt-image-2", usage), expected)

    def test_empty_usage_returns_none_rather_than_zero(self):
        # Zero would silently report a free image and poison the calibration.
        self.assertIsNone(imager.cost_from_usage("gpt-image-2", {}))

    def test_unknown_model_returns_none(self):
        self.assertIsNone(imager.cost_from_usage("gpt-image-9000", self.USAGE))

    def test_batch_halves_the_cost_only_for_eligible_models(self):
        full = imager.cost_from_usage("gpt-image-2", self.USAGE)
        batched = imager.cost_from_usage("gpt-image-2", self.USAGE, batch=True)
        self.assertAlmostEqual(batched, full * 0.5)
        flare = imager.cost_from_usage("gpt-image-2.5-flare", self.USAGE)
        self.assertAlmostEqual(imager.cost_from_usage("gpt-image-2.5-flare", self.USAGE, batch=True), flare)


class TestSizing(unittest.TestCase):
    def test_parse_size_reads_width_by_height(self):
        self.assertEqual(imager.parse_size("1536x864"), (1536, 864))

    def test_auto_and_none_parse_as_unset(self):
        self.assertEqual(imager.parse_size("auto"), (None, None))
        self.assertEqual(imager.parse_size(None), (None, None))

    def test_unparseable_size_exits_nonzero(self):
        with self.assertRaises(SystemExit):
            imager.parse_size("big")

    def test_a_legal_size_has_no_problem(self):
        self.assertIsNone(imager.size_problem(1024, 1024))
        self.assertIsNone(imager.size_problem(1536, 864))

    def test_edges_must_be_multiples_of_sixteen(self):
        self.assertIn("multiples of 16", imager.size_problem(1080, 1080) or "")

    def test_oversize_edge_is_rejected(self):
        self.assertIn("longest edge", imager.size_problem(3856, 1024) or "")

    def test_extreme_aspect_is_rejected(self):
        self.assertIn("aspect ratio", imager.size_problem(3200, 800) or "")

    def test_too_few_pixels_is_rejected(self):
        self.assertIn("at least", imager.size_problem(512, 512) or "")

    def test_snap_size_always_returns_something_legal(self):
        for width, height in (
            (1080, 1080),
            (1920, 1080),
            (1280, 720),
            (1200, 630),
            (1600, 900),
            (1080, 1920),
            (1000, 1500),
            (100, 100),
            (6000, 4000),
        ):
            with self.subTest(size=(width, height)):
                w, h = imager.snap_size(width, height)
                self.assertIsNone(imager.size_problem(w, h), f"{width}x{height} -> {w}x{h}")

    def test_snap_size_rounds_up_so_the_fit_is_a_downscale(self):
        w, h = imager.snap_size(1920, 1080)
        self.assertGreaterEqual(w, 1920)
        self.assertGreaterEqual(h, 1080)

    def test_snap_size_keeps_a_legal_size_untouched(self):
        self.assertEqual(imager.snap_size(1280, 720), (1280, 720))

    def test_platform_drives_the_generation_size_when_size_is_absent(self):
        result = imager.size_for(None, {"width": 1920, "height": 1080})
        self.assertEqual(result, "1920x1088")

    def test_explicit_size_wins_over_platform(self):
        result = imager.size_for("1024x1024", {"width": 1920, "height": 1080})
        self.assertEqual(result, "1024x1024")

    def test_illegal_explicit_size_exits_nonzero(self):
        with self.assertRaises(SystemExit):
            imager.size_for("1080x1080", None)


class TestApiErrors(unittest.TestCase):
    """Retrying a refusal just reaches the same refusal, four times slower."""

    def test_moderation_block_is_not_retryable(self):
        body = json.dumps(
            {
                "error": {
                    "type": "image_generation_user_error",
                    "code": "moderation_blocked",
                    "moderation_details": {"moderation_stage": "input", "categories": ["violence"]},
                }
            }
        )
        message, retryable = imager.describe_api_error(body)
        self.assertFalse(retryable)
        self.assertIn("input", message)
        self.assertIn("violence", message)

    BLOCKED = json.dumps(
        {
            "error": {
                "type": "image_generation_user_error",
                "code": "moderation_blocked",
                "moderation_details": {"moderation_stage": "input", "categories": ["violence"]},
            }
        }
    )

    def test_a_blocked_generation_suggests_moderation_low(self):
        message, _ = imager.describe_api_error(self.BLOCKED)
        self.assertIn("--moderation low", message)

    def test_a_blocked_edit_does_not_suggest_moderation_low(self):
        # The edit endpoint has no moderation field, so the flag cannot help.
        message, retryable = imager.describe_api_error(self.BLOCKED, is_edit=True)
        self.assertNotIn("--moderation low", message)
        self.assertIn("Edits have no moderation setting", message)
        self.assertFalse(retryable)

    def test_user_error_is_not_retryable(self):
        body = json.dumps({"error": {"type": "image_generation_user_error", "code": "bad_prompt", "message": "no"}})
        _, retryable = imager.describe_api_error(body)
        self.assertFalse(retryable)

    def test_server_error_stays_retryable(self):
        body = json.dumps({"error": {"type": "server_error", "message": "try later"}})
        _, retryable = imager.describe_api_error(body)
        self.assertTrue(retryable)

    def test_unparseable_body_stays_retryable(self):
        message, retryable = imager.describe_api_error("<html>502</html>")
        self.assertTrue(retryable)
        self.assertIn("502", message)


class FakeResponse:
    """Stands in for what urlopen returns, so a request can be built and read without sending it."""

    def __init__(self, payload: dict):
        self._body = json.dumps(payload).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def capture_request(**kwargs) -> urllib.request.Request:
    """Call api_request with urlopen replaced, and return the Request it would have sent."""
    sent: list = []

    def fake_urlopen(request, timeout=None):
        sent.append(request)
        return FakeResponse({"data": [{"b64_json": base64.b64encode(tiny_png()).decode()}], "usage": {}})

    with mock.patch.object(imager.urllib.request, "urlopen", side_effect=fake_urlopen):
        imager.api_request(provider="openai", api_key="test-key-not-real", **kwargs)
    return sent[0]


class TestWire(unittest.TestCase):
    """What goes in the request body. Nothing is sent: urlopen is replaced."""

    def test_an_edit_never_carries_a_moderation_field(self):
        with tempfile.TemporaryDirectory() as tmp:
            photo = Path(tmp) / "photo.png"
            photo.write_bytes(tiny_png())
            request = capture_request(prompt="make it blue", edit_image=str(photo), moderation="low")
        self.assertTrue(request.full_url.endswith("/images/edits"))
        self.assertNotIn(b'name="moderation"', request.data)

    def multipart_field(self, request, name: str) -> bytes | None:
        marker = f'name="{name}"\r\n\r\n'.encode()
        if marker not in request.data:
            return None
        return request.data.split(marker, 1)[1].split(b"\r\n", 1)[0]

    def test_an_edit_with_no_size_asks_for_auto_not_the_square_default(self):
        # The edit endpoint defaults size to 1024x1024. Sent with no size, a
        # landscape photo came back square.
        with tempfile.TemporaryDirectory() as tmp:
            photo = Path(tmp) / "photo.png"
            photo.write_bytes(tiny_png(8, 4))
            for kwargs in ({"edit_image": str(photo)}, {"reference_images": [str(photo)]}):
                with self.subTest(kind=next(iter(kwargs))):
                    request = capture_request(prompt="make it blue", **kwargs)
                    self.assertTrue(request.full_url.endswith("/images/edits"))
                    self.assertEqual(self.multipart_field(request, "size"), b"auto")

    def test_an_edit_keeps_an_explicit_size(self):
        with tempfile.TemporaryDirectory() as tmp:
            photo = Path(tmp) / "photo.png"
            photo.write_bytes(tiny_png())
            request = capture_request(prompt="make it blue", edit_image=str(photo), size="1536x864")
        self.assertEqual(self.multipart_field(request, "size"), b"1536x864")

    def test_a_generation_still_carries_moderation(self):
        request = capture_request(prompt="a subject", moderation="low")
        self.assertEqual(json.loads(request.data)["moderation"], "low")


class TestPresetsFile(unittest.TestCase):
    """presets.json must stay structurally sound -- nothing else validates it."""

    @classmethod
    def setUpClass(cls):
        cls.presets = imager.load_presets()

    def test_file_is_not_empty(self):
        self.assertGreater(len(self.presets), 0, "presets.json loaded as empty")

    def test_every_preset_has_description_and_prompt(self):
        for name, preset in self.presets.items():
            with self.subTest(preset=name):
                self.assertIsInstance(preset, dict)
                self.assertTrue(preset.get("description"), f"{name} has no description")
                self.assertTrue(preset.get("prompt"), f"{name} has no prompt")

    def test_every_prompt_interpolates_the_subject(self):
        for name, preset in self.presets.items():
            with self.subTest(preset=name):
                self.assertIn(
                    "{subject}",
                    preset["prompt"],
                    f"preset '{name}' drops the user's subject entirely",
                )

    def test_no_preset_still_declares_a_thinking_level(self):
        # The API has no thinking parameter. A preset that names one is
        # describing a request that cannot be sent.
        for name, preset in self.presets.items():
            with self.subTest(preset=name):
                self.assertNotIn("thinking", preset)


class TestPlatformsFile(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.platforms = imager.load_platforms()

    def test_file_is_not_empty(self):
        self.assertGreater(len(self.platforms), 0, "platforms.json loaded as empty")

    def test_every_platform_has_positive_integer_dimensions(self):
        for name, platform in self.platforms.items():
            with self.subTest(platform=name):
                self.assertTrue(platform.get("description"), f"{name} has no description")
                for axis in ("width", "height"):
                    value = platform.get(axis)
                    self.assertIsInstance(value, int, f"{name}.{axis} is not an int")
                    self.assertGreater(value, 0, f"{name}.{axis} is not positive")

    def test_every_platform_snaps_to_a_generatable_size(self):
        for name, platform in self.platforms.items():
            with self.subTest(platform=name):
                w, h = imager.snap_size(platform["width"], platform["height"])
                self.assertIsNone(imager.size_problem(w, h))


class TestNoDependencies(unittest.TestCase):
    """/plugin install and `npx skills add` copy the directory and run nothing."""

    def test_runs_from_a_bare_copy_with_no_site_packages(self):
        # -S keeps site-packages off the path, so only the standard library can
        # be imported: the same position as a system python3 with nothing
        # installed. Run from a copy, as a plugin install would be.
        skill = Path(__file__).resolve().parent.parent
        with tempfile.TemporaryDirectory() as tmp:
            copy = Path(tmp) / "imager"
            shutil.copytree(skill, copy, ignore=shutil.ignore_patterns(".venv", "tests", "__pycache__"))
            env = {
                "PATH": os.environ.get("PATH", ""),
                "OPENAI_API_KEY": "test-key-not-real",
                "GPT_IMAGE_HOME": str(Path(tmp) / "settings"),
            }
            for argv in (
                ["--dry-run", "--preset", "editorial", "--platform", "square", "a subject", str(Path(tmp) / "o.png")],
                ["list-presets"],
            ):
                with self.subTest(argv=argv[0]):
                    result = subprocess.run(
                        [sys.executable, "-S", str(copy / "scripts" / "imager.py"), *argv],
                        capture_output=True,
                        text=True,
                        env=env,
                        cwd=tmp,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("editorial", result.stdout)

    def test_skill_md_runs_the_cli_with_python3_not_a_venv(self):
        text = (Path(__file__).parent.parent / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("PY=python3", text)
        self.assertNotIn(".venv", text)
        self.assertNotIn("PyYAML", text)


class TestConfigFile(IsolatedHome):
    """config.yaml is read without PyYAML, so the reader is tested against what YAML would say."""

    def test_the_shapes_config_yaml_really_has(self):
        text = (
            "# defaults\n"
            "provider: openai\n"
            "model: gpt-image-2.5-flare   # trailing comment\n"
            "size: 1536x864\n"
            "daily_cap: 20\n"
            "limit: 12.5\n"
            "quoted: 'it''s here'\n"
            'command: "/path/to/preview.sh {path} {name} # not a comment"\n'
            "empty:\n"
            "flag: true\n"
            "url: http://example.invalid/a#b\n"
        )
        self.assertEqual(
            imager.parse_config(text),
            {
                "provider": "openai",
                "model": "gpt-image-2.5-flare",
                "size": "1536x864",
                "daily_cap": 20,
                "limit": 12.5,
                "quoted": "it's here",
                "command": "/path/to/preview.sh {path} {name} # not a comment",
                "empty": None,
                "flag": True,
                "url": "http://example.invalid/a#b",
            },
        )

    def test_anything_nested_is_refused_with_its_line(self):
        for text in ("model: x\nsizes:\n  - 1024x1024\n", "model: {a: 1}\n", "model: 'open\n", "model:x\n"):
            with self.subTest(text=text):
                err = io.StringIO()
                with contextlib.redirect_stderr(err), self.assertRaises(SystemExit):
                    imager.parse_config(text, "config.yaml")
                self.assertIn("config.yaml line", err.getvalue())

    def test_dump_reads_back_the_same(self):
        config = {"provider": "openai", "model": "gpt-image-2.5-flare", "a": "true", "b": "20", "c": "x: y #z"}
        self.assertEqual(imager.parse_config(imager.dump_config(config)), config)

    def test_init_writes_a_config_the_cli_reads_back(self):
        with contextlib.redirect_stdout(io.StringIO()):
            imager.cmd_init()
        self.assertEqual(imager.load_config(), {"provider": "openai", "model": imager.DEFAULT_MODEL})


class TestComposePrompt(unittest.TestCase):
    def test_no_preset_returns_prompt_unchanged(self):
        self.assertEqual(imager.compose_prompt("a red bicycle", None), "a red bicycle")

    def test_preset_substitutes_the_subject(self):
        name = next(iter(imager.load_presets()))
        prompt = imager.compose_prompt("a red bicycle", name)
        self.assertIn("a red bicycle", prompt)
        self.assertNotIn("{subject}", prompt)

    def test_unknown_preset_exits_nonzero(self):
        with self.assertRaises(SystemExit) as ctx:
            imager.compose_prompt("subject", "no-such-preset")
        self.assertNotEqual(ctx.exception.code, 0)


class TestBuildMultipart(unittest.TestCase):
    """The one piece of hand-rolled wire protocol in the repo."""

    def test_plain_field_has_no_content_type(self):
        body, content_type = imager._build_multipart([("model", "gpt-image-2", None)])
        text = body.decode()
        self.assertIn('Content-Disposition: form-data; name="model"', text)
        self.assertNotIn("Content-Type:", text)
        self.assertIn("gpt-image-2", text)
        self.assertTrue(content_type.startswith("multipart/form-data; boundary="))

    def test_boundary_in_header_matches_body(self):
        body, content_type = imager._build_multipart([("a", "b", None)])
        boundary = content_type.split("boundary=")[1]
        self.assertIn(f"--{boundary}".encode(), body)
        self.assertTrue(body.endswith(f"--{boundary}--".encode()))

    def test_boundary_is_unique_per_call(self):
        _, first = imager._build_multipart([("a", "b", None)])
        _, second = imager._build_multipart([("a", "b", None)])
        self.assertNotEqual(first, second)

    def test_file_field_derives_mime_from_extension(self):
        for filename, expected in (
            ("photo.png", "image/png"),
            ("photo.jpg", "image/jpeg"),
            ("photo.jpeg", "image/jpeg"),
            ("photo.webp", "image/webp"),
            ("photo.PNG", "image/png"),
        ):
            with self.subTest(filename=filename):
                body, _ = imager._build_multipart([("image", b"\x89PNG", filename)])
                self.assertIn(f"Content-Type: {expected}".encode(), body)

    def test_unknown_extension_defaults_to_png(self):
        body, _ = imager._build_multipart([("image", b"data", "photo.bmp")])
        self.assertIn(b"Content-Type: image/png", body)

    def test_binary_value_survives_intact(self):
        payload = bytes(range(256))
        body, _ = imager._build_multipart([("image", payload, "x.png")])
        self.assertIn(payload, body)

    def test_uses_crlf_line_endings(self):
        body, _ = imager._build_multipart([("a", "b", None)])
        self.assertIn(b"\r\n", body)


class TestHistory(unittest.TestCase):
    """Round-trip against a redirected config dir.

    The module resolves CONFIG_DIR from Path.home() at import time, so the
    constants have to be patched directly -- setting $HOME here would be a no-op.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self._saved = (
            imager.CONFIG_DIR,
            imager.HISTORY_FILE,
            imager.LAST_RUN_FILE,
        )
        imager.CONFIG_DIR = root
        imager.HISTORY_FILE = root / "history.jsonl"
        imager.LAST_RUN_FILE = root / "last.json"

    def tearDown(self):
        (
            imager.CONFIG_DIR,
            imager.HISTORY_FILE,
            imager.LAST_RUN_FILE,
        ) = self._saved
        self.tmp.cleanup()

    def _entry(self, prompt="a subject", project=None, output="out.png", output_dir=".", **kw):
        fields = {
            "timestamp": "2026-07-28T12:00:00",
            "prompt": prompt,
            "preset": None,
            "platform": None,
            "model": "gpt-image-2.5-flare",
            "quality": "high",
            "size": "1024x1024",
            "background": None,
            "output_format": "png",
            "provider": "openai",
            "n": 1,
            "output": output,
            "output_dir": output_dir,
            "project": project,
            "estimated_cost": 0.21,
            "actual_cost": None,
            "usage": None,
        }
        fields.update(kw)
        return imager.HistoryEntry(**fields)

    def test_missing_history_reads_as_empty(self):
        self.assertEqual(imager.load_history(), [])

    def test_missing_last_run_reads_as_none(self):
        self.assertIsNone(imager.load_last_run())

    def test_save_then_load_round_trips(self):
        imager.save_history(self._entry(prompt="a red bicycle"))
        entries = imager.load_history()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["prompt"], "a red bicycle")

    def test_save_history_also_writes_last_run(self):
        imager.save_history(self._entry(prompt="most recent"))
        self.assertEqual(imager.load_last_run()["prompt"], "most recent")

    def test_load_history_returns_the_last_n(self):
        for i in range(5):
            imager.save_history(self._entry(prompt=f"prompt-{i}"))
        entries = imager.load_history(n=2)
        self.assertEqual([e["prompt"] for e in entries], ["prompt-3", "prompt-4"])

    def test_project_filter_selects_only_that_project(self):
        imager.save_history(self._entry(prompt="alpha", project="alpha-proj"))
        imager.save_history(self._entry(prompt="beta", project="beta-proj"))
        entries = imager.load_history(project="alpha-proj")
        self.assertEqual([e["prompt"] for e in entries], ["alpha"])

    def test_blank_lines_are_skipped(self):
        imager.save_history(self._entry())
        with imager.HISTORY_FILE.open("a") as f:
            f.write("\n\n")
        self.assertEqual(len(imager.load_history()), 1)

    def test_history_is_valid_jsonl(self):
        imager.save_history(self._entry())
        for line in imager.HISTORY_FILE.read_text().splitlines():
            if line.strip():
                json.loads(line)

    def test_actual_cost_is_preferred_over_the_estimate(self):
        self.assertAlmostEqual(imager.entry_cost({"estimated_cost": 0.21, "actual_cost": 0.03}), 0.03)
        self.assertAlmostEqual(imager.entry_cost({"estimated_cost": 0.21}), 0.21)
        self.assertEqual(imager.entry_cost({}), 0.0)


class TestSetProfile(unittest.TestCase):
    """Step 0: what did this directory's set use last time?"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self._saved = (imager.CONFIG_DIR, imager.HISTORY_FILE, imager.LAST_RUN_FILE)
        imager.CONFIG_DIR = self.root
        imager.HISTORY_FILE = self.root / "history.jsonl"
        imager.LAST_RUN_FILE = self.root / "last.json"
        self.images = self.root / "set"
        self.images.mkdir()

    def tearDown(self):
        (imager.CONFIG_DIR, imager.HISTORY_FILE, imager.LAST_RUN_FILE) = self._saved
        self.tmp.cleanup()

    def _write(self, **kw):
        row = {
            "timestamp": "2026-09-12T10:00:00",
            "model": "gpt-image-2",
            "quality": "low",
            "n": 1,
            "output": str(self.images / "a.png"),
            "output_dir": str(self.images),
        }
        row.update(kw)
        with imager.HISTORY_FILE.open("a") as f:
            f.write(json.dumps(row) + "\n")

    def test_empty_history_reports_a_new_set(self):
        profile = imager.set_profile(self.images / "new.png")
        self.assertEqual(profile["count"], 0)
        self.assertIsNone(profile["quality"])

    def test_reports_the_dominant_quality_and_model(self):
        self._write(quality="low")
        self._write(quality="low")
        self._write(quality="high")
        profile = imager.set_profile(self.images / "new.png")
        self.assertEqual(profile["quality"], "low")
        self.assertEqual(profile["model"], "gpt-image-2")
        self.assertEqual(profile["count"], 3)

    def test_multi_image_runs_are_counted(self):
        # The bug this replaces: a --n 4 run stored the DIRECTORY in `output`,
        # and the old lookup then took its parent and compared the grandparent
        # against the target, so the whole run was invisible to set-check.
        self._write(n=4, output=str(self.images), output_dir=None)
        profile = imager.set_profile(self.images / "new.png")
        self.assertEqual(profile["count"], 4)
        self.assertEqual(profile["quality"], "low")

    def test_rows_from_another_directory_are_ignored(self):
        other = self.root / "elsewhere"
        other.mkdir()
        self._write(output=str(other / "b.png"), output_dir=str(other), quality="high")
        self._write(quality="low")
        profile = imager.set_profile(self.images / "new.png")
        self.assertEqual(profile["count"], 1)
        self.assertEqual(profile["quality"], "low")

    def test_rows_without_a_model_are_read_as_imager(self):
        self._write(model=None)
        self.assertEqual(imager.set_profile(self.images / "new.png")["model"], "gpt-image-2")

    def test_a_bare_dot_matches_nothing(self):
        # A row written with a relative output path recorded output_dir=".". It
        # names no directory in particular, so matching it against whatever the
        # CLI is run from next would hand back an unrelated folder's tier.
        self._write(output="a.png", output_dir=".", quality="high")
        self.assertEqual(imager.set_profile(self.images / "new.png")["count"], 0)


class TestDirMatches(unittest.TestCase):
    def test_absolute_paths_match_exactly(self):
        self.assertTrue(imager.dir_matches("/a/b/c", "/a/b/c"))
        self.assertFalse(imager.dir_matches("/a/b", "/a/b/c"))

    def test_relative_legacy_paths_match_on_a_component_suffix(self):
        self.assertTrue(imager.dir_matches("brand/graphics/aurora", "/home/d/repo/brand/graphics/aurora"))
        self.assertTrue(imager.dir_matches("aurora", "/home/d/repo/brand/graphics/aurora"))

    def test_a_partial_component_does_not_match(self):
        # "rora" is not the directory "aurora", and a plain string endswith
        # would have said it was.
        self.assertFalse(imager.dir_matches("rora", "/home/d/repo/brand/aurora"))

    def test_a_bare_dot_matches_nothing(self):
        self.assertFalse(imager.dir_matches(".", "/home/d/repo"))
        self.assertFalse(imager.dir_matches("", "/home/d/repo"))

    def test_a_relative_path_longer_than_the_target_does_not_match(self):
        self.assertFalse(imager.dir_matches("a/b/c/d/e", "/x/y"))


class TestNoOverwrite(IsolatedHome):
    """A run never destroys a file it did not make, unless told to."""

    def test_an_existing_image_is_kept_and_the_run_writes_a_new_name(self):
        original = self.root / "a.png"
        original.write_bytes(b"paid for already")
        code, out, err, _ = self.generate("a subject", str(original))
        self.assertEqual(code, 0, err)
        self.assertEqual(original.read_bytes(), b"paid for already")
        self.assertEqual((self.root / "a-2.png").read_bytes(), tiny_png())
        self.assertIn("already exists", err)
        self.assertIn("a-2.png", err)
        self.assertEqual(self.history_rows()[-1]["output"], str(self.root / "a-2.png"))

    def test_the_next_free_number_is_used(self):
        (self.root / "a.png").write_bytes(b"one")
        (self.root / "a-2.png").write_bytes(b"two")
        self.generate("a subject", str(self.root / "a.png"))
        self.assertTrue((self.root / "a-3.png").exists())
        self.assertEqual((self.root / "a-2.png").read_bytes(), b"two")

    def test_overwrite_replaces_the_file(self):
        original = self.root / "a.png"
        original.write_bytes(b"old")
        code, _, err, _ = self.generate("--overwrite", "a subject", str(original))
        self.assertEqual(code, 0, err)
        self.assertEqual(original.read_bytes(), tiny_png())
        self.assertFalse((self.root / "a-2.png").exists())

    def test_dry_run_names_the_file_it_would_write(self):
        (self.root / "a.png").write_bytes(b"old")
        code, out, err, _ = self.generate("--dry-run", "a subject", str(self.root / "a.png"))
        self.assertEqual(code, 0, err)
        self.assertIn(f"Output:    {self.root / 'a-2.png'}", out)

    def test_a_multi_image_run_moves_aside_as_a_whole(self):
        (self.root / "set-01.png").write_bytes(b"old")
        code, _, err, _ = self.generate("--n", "2", "a subject", str(self.root / "set.png"))
        self.assertEqual(code, 0, err)
        self.assertEqual((self.root / "set-01.png").read_bytes(), b"old")
        self.assertTrue((self.root / "set-2-01.png").exists())
        self.assertTrue((self.root / "set-2-02.png").exists())

    def test_a_same_stem_json_is_never_replaced(self):
        # Generating package.png used to replace package.json.
        package = self.root / "package.json"
        package.write_text('{"name": "not an image"}')
        for flags in ((), ("--sidecar",), ("--sidecar", "--overwrite")):
            with self.subTest(flags=flags):
                code, _, err, _ = self.generate(*flags, "a subject", str(self.root / "package.png"))
                self.assertEqual(code, 0, err)
                self.assertEqual(package.read_text(), '{"name": "not an image"}')

    def test_no_sidecar_is_written_by_default(self):
        self.generate("a subject", str(self.root / "a.png"))
        self.assertFalse((self.root / "a.json").exists())

    def test_sidecar_is_written_when_asked_for(self):
        self.generate("--sidecar", "a subject", str(self.root / "a.png"))
        self.assertEqual(json.loads((self.root / "a.json").read_text())["prompt"], "a subject")

    def test_save_image_refuses_to_replace_without_overwrite(self):
        target = self.root / "a.png"
        target.write_bytes(b"old")
        with self.assertRaises(FileExistsError):
            imager.save_image(base64.b64encode(b"new").decode(), target)
        self.assertEqual(target.read_bytes(), b"old")


class TestRetiredProvider(IsolatedHome):
    def test_openrouter_in_config_is_rejected_before_anything_is_sent(self):
        imager.ensure_config_dir()
        imager.CONFIG_FILE.write_text("provider: openrouter\n")
        code, _, err, calls = self.generate("a subject", str(self.root / "a.png"))
        self.assertNotEqual(code, 0)
        self.assertIn("OpenRouter support has been removed", err)
        self.assertEqual(calls, [])

    def test_no_doc_offers_openrouter_as_a_route(self):
        repo = Path(__file__).resolve().parents[3]
        docs = [
            repo / "README.md",
            repo / "SECURITY.md",
            repo / "skills" / "imager" / "SKILL.md",
            repo / "skills" / "imager" / "README.md",
            repo / "skills" / "imager" / "references" / "api_reference.md",
        ]
        for doc in docs:
            with self.subTest(doc=doc.name):
                self.assertNotIn("openrouter", doc.read_text().lower())


class TestWriteAheadLedger(IsolatedHome):
    """A row exists for every request sent, whatever happens after it is sent."""

    USAGE = {"input_tokens": 20, "output_tokens": 229, "input_tokens_details": {"text_tokens": 20, "image_tokens": 0}}

    def today(self) -> str:
        return imager.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

    def write_lines(self, *lines: str) -> None:
        imager.HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
        imager.HISTORY_FILE.write_text("".join(line + "\n" for line in lines))

    def test_a_corrupt_middle_line_does_not_hide_the_rows_after_it(self):
        self.write_lines(
            json.dumps({"timestamp": self.today(), "actual_cost": 1.0}),
            '{"timestamp": "torn mid-wri',
            json.dumps({"timestamp": self.today(), "actual_cost": 4.0}),
        )
        self.assertAlmostEqual(imager.spend_today(), 5.0)
        self.assertEqual(len(imager.read_history_entries()), 2)

    def test_the_request_is_counted_in_todays_spend_while_it_is_in_flight(self):
        seen: list = []
        code, _, err, _ = self.generate(
            "a subject", str(self.root / "a.png"), usage=self.USAGE, during=lambda: seen.append(imager.spend_today())
        )
        self.assertEqual(code, 0, err)
        self.assertEqual(len(seen), 1)
        self.assertGreater(seen[0], 0.0)

    def test_a_killed_run_leaves_a_pending_row_that_counts_today(self):
        def killed():
            raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            self.generate("a subject", str(self.root / "a.png"), during=killed)
        rows = imager.read_history_entries()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "pending")
        self.assertGreater(rows[0]["estimated_cost"], 0.0)
        self.assertAlmostEqual(imager.spend_today(), rows[0]["estimated_cost"])

    def test_the_final_row_replaces_the_pending_row(self):
        code, _, err, _ = self.generate("a subject", str(self.root / "a.png"), usage=self.USAGE)
        self.assertEqual(code, 0, err)
        # Two lines on disk, one request read back, counted once at the billed figure.
        self.assertEqual(len(self.history_rows()), 2)
        rows = imager.read_history_entries()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "complete")
        billed = imager.cost_from_usage(imager.DEFAULT_MODEL, self.USAGE)
        self.assertAlmostEqual(rows[0]["actual_cost"], billed)
        self.assertAlmostEqual(imager.spend_today(), billed)
        self.assertIsNotNone(rows[0]["duration_s"])

    def test_a_refused_request_is_recorded_as_failed_and_costs_nothing(self):
        def refused():
            sys.exit(1)

        code, _, _, _ = self.generate("a subject", str(self.root / "a.png"), during=refused)
        self.assertEqual(code, 1)
        rows = imager.read_history_entries()
        self.assertEqual([r["status"] for r in rows], ["failed"])
        self.assertEqual(imager.spend_today(), 0.0)

    def test_an_imagemagick_failure_keeps_the_record_and_the_run(self):
        real_run = subprocess.run

        def magick_fails(cmd, *a, **kw):
            if cmd and cmd[0] == "magick":
                raise subprocess.CalledProcessError(1, cmd, stderr=b"magick: no decode delegate")
            return real_run(cmd, *a, **kw)

        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(imager.shutil, "which", side_effect=lambda name: f"/usr/bin/{name}"))
            stack.enter_context(mock.patch.object(imager.subprocess, "run", side_effect=magick_fails))
            code, out, err, _ = self.generate(
                "a subject", str(self.root / "a.png"), "--platform", "story", usage=self.USAGE
            )
        self.assertEqual(code, 0, err)
        self.assertIn("ImageMagick failed", err)
        self.assertNotIn("Fitted to", out)
        rows = imager.read_history_entries()
        self.assertEqual([r["status"] for r in rows], ["complete"])
        self.assertIsNotNone(rows[0]["actual_cost"])

    def test_a_pending_row_is_not_an_image_in_the_set(self):
        images = self.root / "set"
        images.mkdir()
        self.write_lines(
            json.dumps(
                {
                    "timestamp": self.today(),
                    "model": "gpt-image-2",
                    "quality": "high",
                    "n": 1,
                    "output": str(images / "a.png"),
                    "output_dir": str(images),
                    "id": "abc",
                    "status": "pending",
                }
            )
        )
        self.assertEqual(imager.set_profile(images / "b.png")["count"], 0)

    def test_history_names_a_pending_row(self):
        self.write_lines(
            json.dumps({"timestamp": self.today(), "prompt": "cut off", "estimated_cost": 0.05, "status": "pending"})
        )
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            imager.cmd_history(argparse.Namespace(n=10, history_project=None))
        self.assertIn("pending", out.getvalue())


# A stand-in for ImageMagick's binaries. It logs how it was called and writes a
# PNG header at the -extent size, which is all image_size reads.
FAKE_IMAGEMAGICK = """#!{python}
import pathlib, struct, sys, zlib
args = sys.argv[1:]
with open({log!r}, "a") as f:
    f.write(pathlib.Path(sys.argv[0]).name + " " + " ".join(args) + "\\n")
w, h = 64, 64
if "-extent" in args:
    w, h = map(int, args[args.index("-extent") + 1].split("x"))
ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
crc = struct.pack(">I", zlib.crc32(b"IHDR" + ihdr) & 0xFFFFFFFF)
pathlib.Path(args[-1]).write_bytes(b"\\x89PNG\\r\\n\\x1a\\n" + struct.pack(">I", 13) + b"IHDR" + ihdr + crc)
"""


class TestImageMagickVersions(IsolatedHome):
    """Platform fitting and contact sheets work on ImageMagick 7 and 6, and never claim a fit that did not happen."""

    STORY_AS_GENERATED = (1088, 1920)

    def setUp(self):
        super().setUp()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.log = self.root / "imagemagick.log"

    def install(self, *names: str) -> None:
        for name in names:
            tool = self.bin / name
            tool.write_text(FAKE_IMAGEMAGICK.format(python=sys.executable, log=str(self.log)))
            tool.chmod(0o755)

    def run_with_path(self, *argv: str):
        # PATH holds only the fake tools, so the machine's own ImageMagick,
        # whichever version it is, cannot answer for them.
        with mock.patch.dict(os.environ, {"PATH": str(self.bin)}):
            return self.generate(*argv, png=tiny_png(*self.STORY_AS_GENERATED))

    def calls(self) -> list:
        return self.log.read_text().splitlines() if self.log.exists() else []

    def test_imagemagick_6_fits_the_platform_size(self):
        self.install("convert", "montage")
        code, out, err, _ = self.run_with_path("a subject", str(self.root / "a.png"), "--platform", "story")
        self.assertEqual(code, 0, err)
        self.assertEqual(imager.image_size(self.root / "a.png"), (1080, 1920))
        self.assertIn("Fitted to 1080x1920 (story)", out)
        self.assertTrue(self.calls()[0].startswith("convert "), self.calls())

    def test_imagemagick_6_makes_the_contact_sheet(self):
        self.install("convert", "montage")
        code, out, err, _ = self.run_with_path("a subject", str(self.root / "a.png"), "--n", "2")
        self.assertEqual(code, 0, err)
        sheets = list(imager.contact_sheet_dir().glob("a-contact-*.png"))
        self.assertEqual(len(sheets), 1, err)
        self.assertTrue(any(c.startswith("montage ") for c in self.calls()), self.calls())

    def test_the_contact_sheet_is_written_outside_the_set(self):
        # It used to land in the set's own folder, beside the images a site ships.
        self.install("convert", "montage")
        code, out, err, _ = self.run_with_path("a subject", str(self.root / "a.png"), "--n", "3")
        self.assertEqual(code, 0, err)
        self.assertEqual(sorted(p.name for p in self.root.glob("a*.png")), ["a-01.png", "a-02.png", "a-03.png"])
        sheet = json.loads(out.splitlines()[-1])["contact_sheet"]
        self.assertEqual(Path(sheet).parent, imager.contact_sheet_dir().resolve())
        self.assertTrue(Path(sheet).exists())

    def test_old_contact_sheets_are_pruned(self):
        self.install("convert", "montage")
        folder = imager.contact_sheet_dir()
        folder.mkdir(parents=True)
        for i in range(3):
            old = folder / f"old-{i}.png"
            old.write_bytes(b"old")
            os.utime(old, (1_000_000 + i, 1_000_000 + i))
        with mock.patch.object(imager, "CONTACT_SHEETS_KEPT", 2):
            code, _, err, _ = self.run_with_path("a subject", str(self.root / "a.png"), "--n", "2")
        self.assertEqual(code, 0, err)
        kept = sorted(p.name for p in folder.iterdir())
        self.assertEqual(len(kept), 2)
        self.assertIn("old-2.png", kept)
        self.assertTrue(any(name.startswith("a-contact-") for name in kept), kept)

    def test_magick_is_used_when_present(self):
        self.install("magick", "convert", "montage")
        with mock.patch.dict(os.environ, {"PATH": str(self.bin)}):
            self.assertEqual(imager.imagemagick("convert"), ["magick"])
            self.assertEqual(imager.imagemagick("montage"), ["magick", "montage"])

    def test_without_imagemagick_the_output_names_the_real_size(self):
        code, out, err, _ = self.run_with_path("a subject", str(self.root / "a.png"), "--platform", "story")
        self.assertEqual(code, 0, err)
        self.assertNotIn("Fitted to", out)
        self.assertIn("Not fitted to 1080x1920 (story)", out)
        self.assertIn("is 1088x1920", out)
        self.assertIn("ImageMagick not found", err)

    @unittest.skipUnless(shutil.which("magick") or shutil.which("convert"), "no ImageMagick on this machine")
    def test_the_real_imagemagick_on_this_machine_fits_the_platform_size(self):
        code, out, err, _ = self.generate(
            "a subject", str(self.root / "a.png"), "--platform", "story", png=tiny_png(*self.STORY_AS_GENERATED)
        )
        self.assertEqual(code, 0, err)
        self.assertEqual(imager.image_size(self.root / "a.png"), (1080, 1920))


class TestImageSize(unittest.TestCase):
    """The saved size is read from the file, because the case that needs it is ImageMagick being absent."""

    SAMPLES = {
        # 48x32 samples made with ImageMagick: baseline JPEG, and lossy, lossless and alpha WebP.
        "jpeg": "/9j/4AAQSkZJRgABAQAAAAAAAAD/2wBDAAMCAgICAgMCAgIDAwMDBAYEBAQEBAgGBgUGCQgKCgkICQkKDA8MCgsOCwkJDRENDg8QEBEQCgwSExIQEw8QEBD/2wBDAQMDAwQDBAgEBAgQCwkLEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBD/wAARCAAgADADAREAAhEBAxEB/8QAFQABAQAAAAAAAAAAAAAAAAAAAAf/xAAUEAEAAAAAAAAAAAAAAAAAAAAA/8QAFgEBAQEAAAAAAAAAAAAAAAAAAAgJ/8QAFBEBAAAAAAAAAAAAAAAAAAAAAP/aAAwDAQACEQMRAD8AvDPpTYAAAAAAAAAAAAAAAAAAAAAAD//Z",
        "webp-lossy": "UklGRlAAAABXRUJQVlA4IEQAAACQAwCdASowACAAPpFGnkslo6KhpWgAsBIJZwC/3oB+AAAr98NwAP7sl4//WV/Mr+ZX++R/+N24w330dZRDy6S/IgAAAA==",
        "webp-lossless": "UklGRhwAAABXRUJQVlA4TBAAAAAvL8AHAAfQ0v5H/wMR0f8A",
        "webp-alpha": "UklGRnIAAABXRUJQVlA4WAoAAAAQAAAALwAAHwAAQUxQSAoAAAABB9C/iAhERP8DVlA4IEIAAABQAwCdASowACAAPpFGnkslo6KhpWgAsBIJZwDO3oAAK/fDcAD+7qY//2LOWwLx//7nA/7nA/7nA/jbB+29aoAAAAA=",
    }

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_png(self):
        path = self.root / "a.png"
        path.write_bytes(tiny_png(1088, 16))
        self.assertEqual(imager.image_size(path), (1088, 16))

    def test_jpeg_and_webp(self):
        for name, data in self.SAMPLES.items():
            with self.subTest(sample=name):
                path = self.root / name
                path.write_bytes(base64.b64decode(data))
                self.assertEqual(imager.image_size(path), (48, 32))

    def test_anything_else_is_unknown(self):
        path = self.root / "a.txt"
        path.write_bytes(b"not an image")
        self.assertIsNone(imager.image_size(path))
        self.assertIsNone(imager.image_size(self.root / "missing.png"))


# A stand-in for a preview helper. It logs the arguments it was given, one JSON
# list per call, and prints a URL built from {name}, the way a helper that
# serves files would.
STUB_PREVIEW = """import json, sys
with open({log!r}, "a") as f:
    f.write(json.dumps(sys.argv[1:]) + "\\n")
if {fail!r}:
    sys.stderr.write("preview server is down\\n")
    sys.exit(1)
print("copied", sys.argv[1])
print("http://preview.test/" + sys.argv[2])
"""


class TestPreviewCommand(IsolatedHome):
    """preview_command in config.yaml runs once per saved file, and a run ends with one JSON line."""

    def configure(self, command: str | None = None, fail: bool = False) -> None:
        self.log = self.root / "preview.log"
        stub = self.root / "stub_preview.py"
        stub.write_text(STUB_PREVIEW.format(log=str(self.log), fail=fail))
        if command is None:
            command = f"{shlex.quote(sys.executable)} {shlex.quote(str(stub))} {{path}} {{name}}"
        imager.ensure_config_dir()
        imager.CONFIG_FILE.write_text(f"preview_command: {json.dumps(command)}\n")

    def preview_calls(self) -> list:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def last_json(self, out: str) -> dict:
        return json.loads(out.strip().splitlines()[-1])

    def test_it_runs_once_per_saved_image_with_a_unique_name(self):
        self.configure()
        with mock.patch.object(imager, "make_contact_sheet", return_value=False):
            code, out, err, _ = self.generate("--n", "2", "a subject", str(self.root / "a.png"))
        self.assertEqual(code, 0, err)
        calls = self.preview_calls()
        self.assertEqual([c[0] for c in calls], [str(self.root / "a-01.png"), str(self.root / "a-02.png")])
        names = [c[1] for c in calls]
        self.assertEqual(len(set(names)), 2)
        for name in names:
            self.assertRegex(name, r"^a-0[12]-\d{8}-\d{6}-[0-9a-f]{6}\.png$")
        self.assertIn(f"Preview: http://preview.test/{names[0]}", out)

    def test_the_same_output_twice_gets_two_names(self):
        # A helper that stores files by basename made every out.png replace
        # the one before it.
        self.configure()
        for _ in range(2):
            code, _, err, _ = self.generate("--overwrite", "a subject", str(self.root / "out.png"))
            self.assertEqual(code, 0, err)
        calls = self.preview_calls()
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][0], calls[1][0])
        self.assertNotEqual(calls[0][1], calls[1][1])
        self.assertNotEqual(calls[0][1], "out.png")

    def test_the_last_line_is_json_with_paths_cost_and_preview_url(self):
        self.configure()
        usage = {
            "input_tokens": 20,
            "output_tokens": 196,
            "input_tokens_details": {"text_tokens": 20, "image_tokens": 0},
        }
        code, out, err, _ = self.generate("a subject", str(self.root / "a.png"), usage=usage)
        self.assertEqual(code, 0, err)
        result = self.last_json(out)
        path = str(self.root / "a.png")
        self.assertEqual(result["schema"], "imager.result/v1")
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["paths"], [path])
        self.assertIsNone(result["contact_sheet"])
        self.assertEqual(result["cost_source"], "billed")
        self.assertAlmostEqual(result["cost"], imager.cost_from_usage(imager.DEFAULT_MODEL, usage), places=6)
        name = self.preview_calls()[0][1]
        self.assertEqual(result["preview_urls"], {path: f"http://preview.test/{name}"})

    def test_the_contact_sheet_is_previewed_too(self):
        self.configure()
        sheet_dir = imager.contact_sheet_dir()

        def fake_sheet(images, output, cols=3):
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(tiny_png())
            return True

        with mock.patch.object(imager, "make_contact_sheet", side_effect=fake_sheet):
            code, out, err, _ = self.generate("--n", "2", "a subject", str(self.root / "a.png"))
        self.assertEqual(code, 0, err)
        calls = self.preview_calls()
        self.assertEqual(len(calls), 3)
        sheet = Path(calls[2][0])
        self.assertEqual(sheet.parent, sheet_dir.resolve())
        self.assertEqual(calls[2][1], sheet.name)
        result = self.last_json(out)
        self.assertEqual(result["contact_sheet"], str(sheet))
        self.assertEqual(len(result["preview_urls"]), 3)

    def test_with_no_preview_command_nothing_runs_and_the_line_is_still_printed(self):
        code, out, err, _ = self.generate("a subject", str(self.root / "a.png"))
        self.assertEqual(code, 0, err)
        result = self.last_json(out)
        self.assertEqual(result["preview_urls"], {})
        self.assertEqual(result["paths"], [str(self.root / "a.png")])
        self.assertEqual(result["cost_source"], "estimate")

    def test_a_failing_preview_is_a_warning_and_the_image_is_kept(self):
        self.configure(fail=True)
        code, out, err, _ = self.generate("a subject", str(self.root / "a.png"))
        self.assertEqual(code, 0, err)
        self.assertIn("preview_command exited 1", err)
        self.assertIn("preview server is down", err)
        self.assertTrue((self.root / "a.png").exists())
        self.assertEqual(self.last_json(out)["preview_urls"], {str(self.root / "a.png"): None})

    def test_a_command_without_path_stops_before_anything_is_sent(self):
        self.configure(command="preview.sh {name}")
        code, _, err, calls = self.generate("a subject", str(self.root / "a.png"))
        self.assertEqual(code, 1)
        self.assertIn("{path}", err)
        self.assertEqual(calls, [])
        self.assertEqual(self.history_rows(), [])

    def test_a_file_name_is_passed_as_one_argument_never_as_shell(self):
        self.configure()
        odd = self.root / "a $(touch pwned) b.png"
        code, _, err, _ = self.generate("a subject", str(odd))
        self.assertEqual(code, 0, err)
        path, name = self.preview_calls()[0]
        self.assertEqual(path, str(odd))
        self.assertRegex(name, r"^[A-Za-z0-9._-]+$")
        self.assertFalse((self.root / "pwned").exists())
        self.assertFalse(Path("pwned").exists())

    def test_a_batch_previews_every_image_and_ends_with_one_json_line(self):
        self.configure()
        rows = [{"prompt": f"subject {i}", "output": str(self.root / "set" / f"{i}.png")} for i in range(3)]
        batch = self.root / "runs.jsonl"
        batch.write_text("".join(json.dumps(row) + "\n" for row in rows))
        code, out, err, _ = self.generate("batch", str(batch), "-y")
        self.assertEqual(code, 0, err)
        self.assertEqual(len(self.preview_calls()), 3)
        result = self.last_json(out)
        self.assertEqual(result["paths"], [row["output"] for row in rows])
        self.assertEqual(result["generated"], 3)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(len(result["preview_urls"]), 3)
        code, out, err, _ = self.generate("batch", str(batch), "-y")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.last_json(out)["skipped"], 3)

    def test_skill_md_points_at_the_result_line_not_a_repository_script(self):
        text = (Path(__file__).parent.parent / "SKILL.md").read_text(encoding="utf-8")
        step5 = text.split("### Step 5")[1].split("\n## ")[0]
        self.assertIn("preview_command", step5)
        self.assertIn("preview_urls", step5)
        self.assertNotIn("preview script", step5)


class TestInputImagesInTheCli(IsolatedHome):
    """Every image that goes in is counted before anything is spent."""

    def test_a_dry_run_prices_the_input_images(self):
        ref = self.root / "ref.png"
        ref.write_bytes(tiny_png())
        code, out, err, calls = self.generate(
            "--dry-run", "--reference", str(ref), "a subject", str(self.root / "a.png")
        )
        self.assertEqual(code, 0, err)
        self.assertEqual(calls, [])
        self.assertIn("Inputs:    1 image", out)
        cost = float(out.split("Est. cost: $")[1].split()[0])
        self.assertGreaterEqual(cost, 0.015)

    def test_edit_mask_and_references_all_count(self):
        for name in ("photo.png", "mask.png", "r1.png", "r2.png"):
            (self.root / name).write_bytes(tiny_png())
        code, out, err, _ = self.generate(
            "--estimate",
            "--edit",
            str(self.root / "photo.png"),
            "--mask",
            str(self.root / "mask.png"),
            "--reference",
            str(self.root / "r1.png"),
            "--reference",
            str(self.root / "r2.png"),
            "a subject",
        )
        self.assertEqual(code, 0, err)
        self.assertIn("4 input images", out)

    def test_the_history_row_records_how_many_images_went_in(self):
        ref = self.root / "ref.png"
        ref.write_bytes(tiny_png())
        code, _, err, _ = self.generate("--reference", str(ref), "a subject", str(self.root / "a.png"))
        self.assertEqual(code, 0, err)
        self.assertEqual(imager.read_history_entries()[0]["inputs"], 1)


@contextlib.contextmanager
def working_directory(path: Path):
    """contextlib.chdir arrived in 3.11; the floor is 3.9."""
    before = os.getcwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(before)


class TestCliSafeToScript(IsolatedHome):
    """Defects that made the CLI unsafe to drive from a script."""

    def edit_run(self) -> tuple:
        photo, ref = self.root / "photo.png", self.root / "ref.png"
        photo.write_bytes(tiny_png())
        ref.write_bytes(tiny_png())
        code, _, err, _ = self.generate(
            "--edit", str(photo), "--reference", str(ref), "make it blue", str(self.root / "a.png")
        )
        self.assertEqual(code, 0, err)
        return photo, ref

    def test_a_cancelled_confirmation_exits_3_and_sends_nothing(self):
        with mock.patch("builtins.input", return_value="n") as asked:
            code, _, err, calls = self.generate(
                "--quality", "xhigh", "--n", "10", "a subject", str(self.root / "a.png")
            )
        self.assertEqual(asked.call_count, 1)
        self.assertEqual(code, imager.EXIT_CANCELLED)
        self.assertIn("Cancelled", err)
        self.assertEqual(calls, [])
        self.assertEqual(self.history_rows(), [])

    def test_again_after_an_edit_replays_the_same_inputs(self):
        photo, ref = self.edit_run()
        with working_directory(self.root):
            code, _, err, calls = self.generate("again")
        self.assertEqual(code, 0, err)
        self.assertEqual(calls[0]["edit_image"], str(photo.resolve()))
        self.assertEqual(calls[0]["reference_images"], [str(ref.resolve())])
        self.assertEqual(calls[0]["prompt"], "make it blue")

    def test_again_refuses_when_an_input_is_gone(self):
        photo, _ = self.edit_run()
        photo.unlink()
        with working_directory(self.root):
            code, _, err, calls = self.generate("again")
        self.assertEqual(code, 1)
        self.assertIn("gone", err)
        self.assertEqual(calls, [])

    def test_again_refuses_an_old_record_of_an_edit(self):
        # Written before input paths were kept: it says images went in, not which.
        imager.ensure_config_dir()
        record = {"prompt": "make it blue", "model": imager.DEFAULT_MODEL, "quality": "low", "n": 1, "inputs": 1}
        imager.LAST_RUN_FILE.write_text(json.dumps(record))
        with working_directory(self.root):
            code, _, err, calls = self.generate("again")
        self.assertEqual(code, 1)
        self.assertIn("predates", err)
        self.assertEqual(calls, [])

    def test_project_with_an_output_path_is_only_a_tag(self):
        home = self.root / "home"
        with mock.patch.object(imager.Path, "home", return_value=home):
            code, _, err, _ = self.generate("--project", "launch", "a subject", str(self.root / "hero.png"))
        self.assertEqual(code, 0, err)
        self.assertTrue((self.root / "hero.png").exists())
        self.assertFalse(home.exists())
        self.assertEqual(imager.read_history_entries()[0]["project"], "launch")

    def test_project_with_no_output_path_still_files_under_the_project(self):
        home = self.root / "home"
        with mock.patch.object(imager.Path, "home", return_value=home):
            code, _, err, _ = self.generate("--project", "launch", "a subject")
        self.assertEqual(code, 0, err)
        self.assertEqual(len(list((home / "imager" / "outputs" / "launch").glob("*.png"))), 1)

    def test_a_missing_input_stops_before_anything_is_priced(self):
        photo = self.root / "photo.png"
        photo.write_bytes(tiny_png())
        gone = str(self.root / "gone.png")
        for argv in (
            ["--edit", gone],
            ["--reference", gone],
            ["--edit", str(photo), "--mask", gone],
            ["--dry-run", "--edit", gone],
        ):
            with self.subTest(argv=argv):
                code, out, err, calls = self.generate(*argv, "a subject", str(self.root / "a.png"))
                self.assertEqual(code, 1)
                self.assertIn(f"input image not found: {gone}", err)
                self.assertNotIn("Generating", err)
                self.assertNotIn("Est. cost", out)
                self.assertEqual(calls, [])
                self.assertEqual(self.history_rows(), [])

    def test_the_default_output_is_named_for_the_skill(self):
        with working_directory(self.root):
            code, _, err, _ = self.generate("a subject")
        self.assertEqual(code, 0, err)
        self.assertEqual(len(list(self.root.glob("imager-*.png"))), 1)
        self.assertEqual(list(self.root.glob("gpt-image-*")), [])

    def test_skill_md_documents_project(self):
        text = (Path(__file__).parent.parent / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("--project", text)
        self.assertIn("3 cancelled", text)


class TestSetTierPrice(IsolatedHome):
    """When set matching moves the tier, the price change is said out loud."""

    def setUp(self):
        super().setUp()
        self.set_dir = self.root / "set"
        self.set_dir.mkdir()

    def seed_set(self, quality: str, count: int = 3) -> None:
        imager.HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
        with imager.HISTORY_FILE.open("a") as f:
            for i in range(count):
                row = {
                    "timestamp": "2026-09-20T10:00:00",
                    "model": imager.DEFAULT_MODEL,
                    "quality": quality,
                    "n": 1,
                    "output": str(self.set_dir / f"{i}.png"),
                    "output_dir": str(self.set_dir),
                }
                f.write(json.dumps(row) + "\n")

    def price(self, quality: str) -> str:
        return f"${imager.cost_per_unit(imager.DEFAULT_MODEL, quality, 'auto', 0, 'a subject')[0]:.3f}"

    def test_raising_the_tier_prints_both_prices(self):
        # One earlier high image in a folder makes every unflagged write there
        # high. That used to happen with no mention of what it costs.
        self.seed_set("high")
        code, out, err, _ = self.generate("--dry-run", "a subject", str(self.set_dir / "new.png"))
        self.assertEqual(code, 0, err)
        self.assertIn("Quality:   high", out)
        self.assertIn(f"Price per image: {self.price('low')} at {imager.DEFAULT_MODEL} quality=low", err)
        self.assertIn(f"-> {self.price('high')} at the set's {imager.DEFAULT_MODEL} quality=high", err)

    def test_lowering_the_tier_prints_both_prices(self):
        self.seed_set("low")
        imager.CONFIG_FILE.write_text("quality: high\n")
        code, out, err, _ = self.generate("--dry-run", "a subject", str(self.set_dir / "new.png"))
        self.assertEqual(code, 0, err)
        self.assertIn("Quality:   low", out)
        self.assertIn(f"{self.price('high')} at {imager.DEFAULT_MODEL} quality=high -> {self.price('low')}", err)

    def test_an_explicit_quality_over_the_set_is_priced_in_the_warning(self):
        self.seed_set("low")
        code, out, err, _ = self.generate("--dry-run", "--quality", "high", "a subject", str(self.set_dir / "new.png"))
        self.assertEqual(code, 0, err)
        self.assertIn("Quality:   high", out)
        self.assertIn(f"you asked for high ({self.price('high')}/image against {self.price('low')}", err)

    def test_nothing_is_said_when_the_tier_already_matches(self):
        self.seed_set("low")
        code, _, err, _ = self.generate("--dry-run", "a subject", str(self.set_dir / "new.png"))
        self.assertEqual(code, 0, err)
        self.assertNotIn("Price per image", err)

    def test_skill_md_step_4_does_not_force_a_tier_over_the_set(self):
        text = (Path(__file__).parent.parent / "SKILL.md").read_text(encoding="utf-8")
        step4 = text.split("### Step 4")[1].split("### Step 5")[0]
        self.assertNotIn("--quality high", step4)
        self.assertIn("pass no `--quality`", step4)


class TestBatch(IsolatedHome):
    """A batch goes through every guard a single run has, once."""

    def write_batch(self, *rows: dict, name: str = "runs.jsonl") -> Path:
        path = self.root / name
        path.write_text("".join(json.dumps(row) + "\n" for row in rows))
        return path

    def rows(self, count: int, folder: str = "set") -> list:
        return [{"prompt": f"subject {i}", "output": str(self.root / folder / f"{i}.png")} for i in range(count)]

    def test_a_dry_run_prints_one_total_for_the_whole_file(self):
        rows = self.rows(4)
        rows[1]["preset"] = "editorial"
        rows[2]["platform"] = "story"
        code, out, err, calls = self.generate("batch", str(self.write_batch(*rows)), "--dry-run")
        self.assertEqual(code, 0, err)
        self.assertEqual(calls, [])
        self.assertEqual(out.count("Est. total:"), 1)
        expected = 0.0
        for row in rows:
            size = imager.size_for(None, imager.load_platforms()[row["platform"]] if row.get("platform") else None)
            prompt = imager.compose_prompt(row["prompt"], row.get("preset"))
            expected += imager.cost_per_unit(imager.DEFAULT_MODEL, "low", size or "auto", 0, prompt)[0]
        self.assertIn(f"Est. total: ${expected:.3f} for 4 image(s)", out)
        self.assertEqual(self.history_rows(), [])

    def test_daily_cap_stops_a_batch_even_with_yes(self):
        imager.CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        imager.CONFIG_FILE.write_text("daily_cap: 0.01\n")
        code, _, err, calls = self.generate("batch", str(self.write_batch(*self.rows(5))), "-y")
        self.assertEqual(code, imager.EXIT_OVER_CAP)
        self.assertIn("daily_cap", err)
        self.assertEqual(calls, [])
        self.assertFalse((self.root / "set" / "0.png").exists())

    def test_daily_cap_also_stops_a_single_run_with_yes(self):
        imager.CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        imager.CONFIG_FILE.write_text("daily_cap: 0.001\n")
        code, _, err, calls = self.generate("-y", "a subject", str(self.root / "a.png"))
        self.assertEqual(code, imager.EXIT_OVER_CAP)
        self.assertEqual(calls, [])

    def test_a_rerun_skips_rows_whose_output_exists(self):
        batch = self.write_batch(*self.rows(3))
        code, _, err, calls = self.generate("batch", str(batch), "-y")
        self.assertEqual(code, 0, err)
        self.assertEqual(len(calls), 3)
        (self.root / "set" / "1.png").unlink()
        code, out, err, calls = self.generate("batch", str(batch), "-y")
        self.assertEqual(code, 0, err)
        self.assertEqual(len(calls), 1)
        self.assertIn("Skipping 2 row(s)", out)
        code, out, _, calls = self.generate("batch", str(batch), "-y")
        self.assertEqual(calls, [])
        self.assertIn("Nothing to do", out)

    def test_every_image_gets_its_own_history_row(self):
        code, out, err, calls = self.generate("batch", str(self.write_batch(*self.rows(6))), "-y", "--concurrency", "3")
        self.assertEqual(code, 0, err)
        self.assertEqual(len(calls), 6)
        rows = imager.read_history_entries()
        self.assertEqual(len(rows), 6)
        self.assertEqual({r["status"] for r in rows}, {"complete"})
        self.assertIn("6 generated", out)

    def test_one_set_check_applies_the_set_tier(self):
        folder = self.root / "set"
        folder.mkdir()
        imager.HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
        imager.HISTORY_FILE.write_text(
            json.dumps(
                {"model": imager.DEFAULT_MODEL, "quality": "high", "n": 1, "output_dir": str(folder), "output": ""}
            )
            + "\n"
        )
        code, out, err, _ = self.generate("batch", str(self.write_batch(*self.rows(3))), "--dry-run")
        self.assertEqual(code, 0, err)
        self.assertIn("quality=high", out)
        self.assertEqual(err.count("Matching the set"), 1)

    def test_one_confirmation_for_the_total_and_a_cancel_is_not_success(self):
        batch = self.write_batch(*self.rows(12))
        with mock.patch("builtins.input", return_value="n") as asked:
            code, _, err, calls = self.generate("batch", str(batch), "--quality", "high")
        self.assertEqual(asked.call_count, 1)
        self.assertEqual(code, imager.EXIT_CANCELLED)
        self.assertEqual(calls, [])

    def run_batch_with(self, fake, *argv: str) -> tuple:
        """Run `batch` with api_request replaced by `fake`. Returns (exit code, stdout, stderr)."""
        out, err, code = io.StringIO(), io.StringIO(), 0
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(imager, "api_request", side_effect=fake))
            stack.enter_context(mock.patch.object(sys, "argv", ["imager.py", "batch", *argv]))
            stack.enter_context(mock.patch.dict(os.environ, {"OPENAI_API_KEY": "test-key-not-real"}))
            stack.enter_context(contextlib.redirect_stdout(out))
            stack.enter_context(contextlib.redirect_stderr(err))
            try:
                imager.main()
            except SystemExit as exc:
                code = exc.code if isinstance(exc.code, int) else 1
        return code, out.getvalue(), err.getvalue()

    def test_a_row_that_fails_does_not_stop_the_others(self):
        image = base64.b64encode(tiny_png()).decode()

        def refuse_one(**kwargs):
            if kwargs["prompt"] == "subject 1":
                sys.exit(1)  # how api_request reports an API error
            return [image], {}

        code, out, err = self.run_batch_with(refuse_one, str(self.write_batch(*self.rows(3))), "-y")
        self.assertEqual(code, 1)
        self.assertIn("2 generated", out)
        self.assertIn("1 failed", out)
        self.assertIn("Failed:", err)
        self.assertTrue((self.root / "set" / "0.png").exists())
        self.assertFalse((self.root / "set" / "1.png").exists())
        self.assertTrue((self.root / "set" / "2.png").exists())
        self.assertEqual(sorted(r["status"] for r in imager.read_history_entries()), ["complete", "complete", "failed"])

    def test_repeated_failures_stop_the_batch(self):
        def refuse(**kwargs):
            sys.exit(1)

        rows = self.rows(imager.BATCH_FAILURE_LIMIT + 4)
        code, out, err = self.run_batch_with(refuse, str(self.write_batch(*rows)), "-y", "--concurrency", "1")
        self.assertEqual(code, 1)
        self.assertIn(f"{imager.BATCH_FAILURE_LIMIT} rows failed in a row", err)
        self.assertIn(f"{imager.BATCH_FAILURE_LIMIT} failed, 4 not started", out)

    def test_concurrency_out_of_range_is_refused(self):
        batch = str(self.write_batch(*self.rows(2)))
        for value in ("0", str(imager.BATCH_MAX_CONCURRENCY + 1)):
            code, _, err, calls = self.generate("batch", batch, "-y", "--concurrency", value)
            self.assertEqual(code, 1)
            self.assertIn("--concurrency", err)
            self.assertEqual(calls, [])

    def test_a_bad_file_is_refused_whole_before_anything_is_sent(self):
        batch = self.write_batch(
            {"prompt": "x", "output": str(self.root / "a.png"), "seed": 1},
            {"prompt": "y", "output": str(self.root / "a.png")},
            {"prompt": "z", "output": str(self.root / "b.png"), "reference": str(self.root / "missing.png")},
            {"prompt": "w", "output": str(self.root / "c.png"), "size": "1000x1000"},
            {"prompt": "v", "output": str(self.root / "d.png"), "preset": ["editorial"]},
        )
        code, _, err, calls = self.generate("batch", str(batch), "-y")
        self.assertEqual(code, 1)
        self.assertEqual(calls, [])
        for expected in (
            "unknown key seed",
            "same output as line 1",
            "input file not found",
            "size 1000x1000",
            "'preset' must be text",
        ):
            self.assertIn(expected, err)

    def test_skill_md_documents_batch_and_drops_the_one_liner(self):
        text = (Path(__file__).parent.parent / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("$PY $GEN batch", text)
        self.assertNotIn("python3 -c", text)


class TestSkillMdShape(unittest.TestCase):
    """SKILL.md is loaded on every call, so its size and its load-bearing sections are held here."""

    @classmethod
    def setUpClass(cls):
        cls.text = (Path(__file__).parent.parent / "SKILL.md").read_text(encoding="utf-8")

    def test_it_stays_under_2000_words(self):
        self.assertLess(len(self.text.split()), 2000)

    def test_it_says_when_not_to_use_it(self):
        section = self.text.split("## When not to use")[1].split("\n## ")[0]
        for kind in ("SVG", "diagram", "image_gen"):
            self.assertIn(kind, section)

    def test_it_gives_timeout_guidance(self):
        section = self.text.split("## Timeouts")[1].split("\n## ")[0]
        self.assertIn("120 seconds", section)
        self.assertIn("background", section)
        self.assertIn("pending", section)

    def test_it_names_no_claude_only_tool(self):
        # The skill ships for Codex too, which has no AskUserQuestion.
        self.assertNotIn("AskUserQuestion", self.text)

    def test_retirement_dates_match_openais_deprecations_page(self):
        # developers.openai.com/api/docs/deprecations: gpt-image-1 shuts down
        # 2026-10-23; gpt-image-1.5, gpt-image-1-mini and chatgpt-image-latest
        # on 2026-12-01.
        self.assertIn("`gpt-image-1` leaves the API on\n**23 October 2026**", self.text)
        self.assertIn("`chatgpt-image-latest` on\n**1 December 2026**", self.text)

    def test_it_asks_for_a_dry_run_read_before_spending(self):
        step4 = self.text.split("### Step 4")[1].split("### Step 5")[0]
        self.assertIn("--dry-run", step4)


class TestDraftSize(IsolatedHome):
    def test_draft_honours_the_config_size(self):
        imager.ensure_config_dir()
        imager.CONFIG_FILE.write_text("size: 1536x1024\n")
        code, out, err, _ = self.generate("--dry-run", "--draft", "a test", str(self.root / "a.png"))
        self.assertEqual(code, 0, err)
        self.assertIn("Size:      1536x1024", out)

    def test_the_draft_request_carries_the_size(self):
        code, _, err, calls = self.generate("--draft", "--platform", "story", "a test", str(self.root / "a.png"))
        self.assertEqual(code, 0, err)
        self.assertEqual(calls[0]["size"], "1088x1920")
        self.assertEqual(calls[0]["quality"], "low")


@unittest.skipUnless(shutil.which("git"), "git is not installed")
class TestSetIdentityAcrossWorktrees(IsolatedHome):
    """A set is a folder in a repository, not an absolute path.

    Every worktree of a repository puts the same folder at a different absolute
    path, and a worktree made for one session is gone by the next. Keyed on the
    absolute path, a set made in one worktree was a brand new set in the next,
    which is the mistake set-check exists to prevent.
    """

    def setUp(self):
        super().setUp()
        self.main = make_repo(self.root / "main")
        git("worktree", "add", "-q", str(self.root / "wt"), cwd=self.main)
        self.wt = self.root / "wt"

    def test_a_set_made_in_one_worktree_is_found_from_another(self):
        code, _, err, _ = self.generate("--quality", "medium", "a subject", str(self.main / "images" / "a.png"))
        self.assertEqual(code, 0, err)
        profile = imager.set_profile(self.wt / "images" / "b.png")
        self.assertEqual(profile["count"], 1)
        self.assertEqual(profile["quality"], "medium")

    def test_set_check_in_a_second_worktree_reports_the_first(self):
        self.generate("--quality", "medium", "a subject", str(self.main / "images" / "a.png"))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            imager.cmd_set_check(argparse.Namespace(path=str(self.wt / "images"), n=10))
        self.assertIn("1 image(s) previously made at quality=medium", out.getvalue())

    def test_new_rows_carry_the_repository_and_the_relative_directory(self):
        self.generate("a subject", str(self.wt / "art" / "notes" / "a.png"))
        row = self.history_rows()[-1]
        self.assertEqual(row["repo"], str((self.main / ".git").resolve()))
        self.assertEqual(row["repo_dir"], "art/notes")

    def test_a_generation_in_the_worktree_adopts_the_set_tier(self):
        self.generate("--quality", "medium", "a subject", str(self.main / "images" / "a.png"))
        code, out, err, calls = self.generate("--dry-run", "another subject", str(self.wt / "images" / "b.png"))
        self.assertEqual(code, 0, err)
        self.assertIn("Quality:   medium", out)
        self.assertIn("Matching the set", err)

    def test_a_row_written_before_the_key_existed_is_still_found(self):
        # Rows from before this change hold only an absolute directory. When
        # that directory still exists, its repository can be worked out.
        (self.main / "images").mkdir()
        imager.ensure_config_dir()
        row = {"model": "gpt-image-2", "quality": "low", "n": 2, "output_dir": str(self.main / "images")}
        imager.HISTORY_FILE.write_text(json.dumps(row) + "\n")
        profile = imager.set_profile(self.wt / "images" / "b.png")
        self.assertEqual(profile["count"], 2)
        self.assertEqual(profile["quality"], "low")

    def test_the_same_folder_name_in_another_repository_is_another_set(self):
        other = make_repo(self.root / "other")
        self.generate("--quality", "medium", "a subject", str(other / "images" / "a.png"))
        self.assertEqual(imager.set_profile(self.wt / "images" / "b.png")["count"], 0)

    def test_another_folder_in_the_same_repository_is_another_set(self):
        self.generate("--quality", "medium", "a subject", str(self.main / "images" / "a.png"))
        self.assertEqual(imager.set_profile(self.wt / "other-images" / "b.png")["count"], 0)

    def test_outside_git_there_is_no_key(self):
        plain = self.root / "plain"
        plain.mkdir()
        self.assertIsNone(imager.set_key(plain))


class TestCli(unittest.TestCase):
    """Subprocess-level checks. --dry-run returns before any request is built."""

    def test_help_exits_zero(self):
        result = run_cli("--help")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_no_arguments_prints_help(self):
        result = run_cli()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("usage", result.stdout.lower())

    def test_list_models_names_the_default(self):
        result = run_cli("list-models")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(imager.DEFAULT_MODEL, result.stdout)

    def test_list_presets_lists_a_real_preset(self):
        result = run_cli("list-presets")
        self.assertEqual(result.returncode, 0, result.stderr)
        name = next(iter(imager.load_presets()))
        self.assertIn(name, result.stdout)

    def test_list_platforms_lists_a_real_platform(self):
        result = run_cli("list-platforms")
        self.assertEqual(result.returncode, 0, result.stderr)
        name = next(iter(imager.load_platforms()))
        self.assertIn(name, result.stdout)

    def test_dry_run_reports_prompt_and_cost_without_calling_out(self):
        result = run_cli("--dry-run", "a red bicycle")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("a red bicycle", result.stdout)
        self.assertIn("Est. cost:", result.stdout)

    def test_dry_run_defaults_to_flare(self):
        result = run_cli("--dry-run", "a red bicycle")
        self.assertIn(imager.DEFAULT_MODEL, result.stdout)

    def test_model_alias_is_accepted(self):
        result = run_cli("--dry-run", "--model", "sunburst", "a red bicycle")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("gpt-image-2.5-sunburst", result.stdout)

    def test_dry_run_applies_the_preset(self):
        name = next(iter(imager.load_presets()))
        result = run_cli("--dry-run", "--preset", name, "a red bicycle")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("{subject}", result.stdout)
        self.assertIn("a red bicycle", result.stdout)

    def test_draft_flag_forces_low_quality(self):
        result = run_cli("--dry-run", "--draft", "a red bicycle")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("DRAFT", result.stdout)
        self.assertIn("Quality:   low", result.stdout)

    def test_draft_keeps_the_platform_aspect(self):
        # The draft is where the composition is approved. Generated at auto, it
        # was composed at a different aspect from the final it stood in for.
        result = run_cli("--dry-run", "--draft", "--platform", "story", "a test")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Size:      1088x1920", result.stdout)

    def test_draft_keeps_an_explicit_size(self):
        result = run_cli("--dry-run", "--draft", "--size", "1536x864", "a test")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Size:      1536x864", result.stdout)

    def test_draft_and_final_are_generated_at_the_same_size(self):
        draft = run_cli("--dry-run", "--draft", "--platform", "blog", "a test")
        final = run_cli("--dry-run", "--quality", "high", "--platform", "blog", "a test")
        size = [line for line in final.stdout.splitlines() if line.startswith("Size:")]
        self.assertEqual(len(size), 1)
        self.assertIn(size[0], draft.stdout)

    def test_estimate_only_reports_cost(self):
        result = run_cli("--estimate", "a red bicycle")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Estimated cost", result.stdout)

    def test_estimate_offers_the_batch_price_for_imager(self):
        result = run_cli("--estimate", "--model", "gpt-image-2", "--n", "4", "a subject")
        self.assertIn("Batch API", result.stdout)

    def test_estimate_does_not_offer_batch_for_the_25_models(self):
        result = run_cli("--estimate", "--n", "4", "a subject")
        self.assertNotIn("Batch API", result.stdout)

    def test_missing_api_key_exits_nonzero(self):
        result = run_cli("--dry-run", "a subject", env_extra={"OPENAI_API_KEY": ""})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("No API key", result.stderr)

    def test_unknown_preset_exits_nonzero(self):
        result = run_cli("--dry-run", "--preset", "no-such-preset", "a subject")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unknown preset", result.stderr)

    def test_unknown_model_exits_nonzero(self):
        result = run_cli("--dry-run", "--model", "gpt-image-9000", "a subject")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unknown model", result.stderr)

    def test_xhigh_is_rejected_on_imager(self):
        result = run_cli("--dry-run", "--model", "gpt-image-2", "--quality", "xhigh", "a subject")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("does not support", result.stderr)

    def test_n_above_ten_is_rejected(self):
        result = run_cli("--dry-run", "--n", "11", "a subject")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--n must be between 1 and 10", result.stderr)

    def test_n_below_one_is_rejected(self):
        result = run_cli("--dry-run", "--n", "0", "a subject")
        self.assertNotEqual(result.returncode, 0)

    def test_invalid_provider_is_rejected(self):
        result = run_cli("--dry-run", "--provider", "nobody", "a subject")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unknown provider", result.stderr)

    def test_openrouter_is_rejected_with_a_reason(self):
        # It posted to routes OpenRouter's image API does not use and would have
        # logged a paid image as $0. Whoever still asks for it should hear why.
        result = run_cli("--dry-run", "--provider", "openrouter", "a subject", env_extra={"OPENROUTER_API_KEY": "x"})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("OpenRouter support has been removed", result.stderr)

    def test_openai_is_still_accepted_by_name(self):
        result = run_cli("--dry-run", "--provider", "openai", "a subject")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_illegal_size_is_rejected_before_the_api_call(self):
        result = run_cli("--dry-run", "--size", "1080x1080", "a subject")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("1088x1088", result.stderr)

    def test_compression_on_png_is_rejected(self):
        result = run_cli("--dry-run", "--output-compression", "50", "a subject")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("jpeg and webp only", result.stderr)

    def test_mask_without_edit_is_rejected(self):
        result = run_cli("--dry-run", "--mask", "m.png", "a subject")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--mask needs --edit", result.stderr)

    def test_moderation_with_edit_is_rejected(self):
        # The edit endpoint has no moderation field. Sending it anyway broke the
        # rule that a flag either reaches the wire or errors.
        result = run_cli("--dry-run", "--edit", "photo.png", "--moderation", "low", "a subject")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--moderation applies to generations only", result.stderr)

    def test_moderation_with_reference_is_rejected(self):
        result = run_cli("--dry-run", "--reference", "face.png", "--moderation", "low", "a subject")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--moderation applies to generations only", result.stderr)

    def test_moderation_on_a_generation_is_still_accepted(self):
        result = run_cli("--dry-run", "--moderation", "low", "a subject")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_seed_fails_loudly_rather_than_being_ignored(self):
        # The whole point: a script still passing --seed believes composition
        # is locked between draft and final. It never was.
        result = run_cli("--dry-run", "--seed", "42", "a subject")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no seed parameter", result.stderr)

    def test_thinking_fails_loudly(self):
        result = run_cli("--dry-run", "--thinking", "medium", "a subject")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no thinking parameter", result.stderr)


if __name__ == "__main__":
    unittest.main()
