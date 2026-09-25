#!/usr/bin/env python3
"""Tests for imager.py.

Hermetic by construction: the only function that touches the network is
api_request, and nothing here calls it. The CLI cases run --dry-run, which
returns before the request is built (but *after* the API-key check, hence the
dummy key in the subprocess environment).

The yaml-shape tests are the point of this file as much as the unit tests are.
presets.yaml and platforms.yaml are the skill's real configuration surface --
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

    def generate(self, *argv: str, png: bytes | None = None, usage: dict | None = None):
        """Run the real CLI in-process with the API replaced by a fake.

        Returns (exit code, stdout, stderr, the keyword arguments the fake
        received). Nothing leaves the machine: api_request is the only function
        that calls out, and it is the thing replaced.
        """
        calls: list = []
        image = base64.b64encode(png or tiny_png()).decode()

        def fake_api_request(**kwargs):
            calls.append(kwargs)
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
    def setUp(self):
        # measured_cost reads history, so point it at an empty file.
        self.tmp = tempfile.TemporaryDirectory()
        self._saved = imager.HISTORY_FILE
        imager.HISTORY_FILE = Path(self.tmp.name) / "history.jsonl"

    def tearDown(self):
        imager.HISTORY_FILE = self._saved
        self.tmp.cleanup()

    def test_published_figures_are_used_when_they_exist(self):
        cost, basis = imager.cost_per_unit("gpt-image-2", "low", "1024x1024")
        self.assertAlmostEqual(cost, 0.006)
        self.assertEqual(basis, "published")

    def test_non_square_is_cheaper_than_square_at_the_same_quality(self):
        square, _ = imager.cost_per_unit("gpt-image-2", "high", "1024x1024")
        portrait, _ = imager.cost_per_unit("gpt-image-2", "high", "1024x1536")
        self.assertLess(portrait, square)

    def test_unknown_combination_falls_back_to_an_upper_bound(self):
        cost, basis = imager.cost_per_unit("gpt-image-2.5-flare", "high", "1088x1088")
        self.assertEqual(basis, "upper bound")
        self.assertAlmostEqual(cost, imager.FALLBACK_COST["high"])

    def test_unknown_quality_does_not_under_quote(self):
        cost, _ = imager.cost_per_unit("gpt-image-2", "ludicrous", "1024x1024")
        self.assertGreaterEqual(cost, imager.FALLBACK_COST["high"])

    def test_scales_linearly_with_n(self):
        single = imager.estimate_cost("gpt-image-2", "medium", "1024x1024", 1)
        self.assertAlmostEqual(imager.estimate_cost("gpt-image-2", "medium", "1024x1024", 4), single * 4)

    def test_zero_images_costs_nothing(self):
        self.assertEqual(imager.estimate_cost("gpt-image-2", "high", "1024x1024", 0), 0.0)

    def test_measured_cost_needs_three_samples(self):
        entry = {
            "timestamp": "2026-09-12T10:00:00",
            "model": "gpt-image-2.5-flare",
            "quality": "medium",
            "size": "1024x1024",
            "n": 1,
            "actual_cost": 0.02,
        }
        imager.HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
        with imager.HISTORY_FILE.open("w") as f:
            for _ in range(2):
                f.write(json.dumps(entry) + "\n")
        self.assertIsNone(imager.measured_cost("gpt-image-2.5-flare", "medium", "1024x1024"))
        with imager.HISTORY_FILE.open("a") as f:
            f.write(json.dumps(entry) + "\n")
        self.assertAlmostEqual(imager.measured_cost("gpt-image-2.5-flare", "medium", "1024x1024"), 0.02)

    def test_measured_cost_beats_the_published_table(self):
        entry = {
            "timestamp": "2026-09-12T10:00:00",
            "model": "gpt-image-2",
            "quality": "low",
            "size": "1024x1024",
            "n": 1,
            "actual_cost": 0.009,
        }
        imager.HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
        with imager.HISTORY_FILE.open("w") as f:
            for _ in range(3):
                f.write(json.dumps(entry) + "\n")
        cost, basis = imager.cost_per_unit("gpt-image-2", "low", "1024x1024")
        self.assertEqual(basis, "measured")
        self.assertAlmostEqual(cost, 0.009)

    def test_measured_cost_is_per_image_not_per_call(self):
        entry = {
            "timestamp": "2026-09-12T10:00:00",
            "model": "gpt-image-2",
            "quality": "low",
            "size": "1024x1024",
            "n": 4,
            "actual_cost": 0.04,
        }
        imager.HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
        with imager.HISTORY_FILE.open("w") as f:
            for _ in range(3):
                f.write(json.dumps(entry) + "\n")
        self.assertAlmostEqual(imager.measured_cost("gpt-image-2", "low", "1024x1024"), 0.01)


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
        # Zero would silently report a free image and poison measured_cost.
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

    def test_a_generation_still_carries_moderation(self):
        request = capture_request(prompt="a subject", moderation="low")
        self.assertEqual(json.loads(request.data)["moderation"], "low")


class TestPresetsFile(unittest.TestCase):
    """presets.yaml must stay structurally sound -- nothing else validates it."""

    @classmethod
    def setUpClass(cls):
        cls.presets = imager.load_presets()

    def test_file_is_not_empty(self):
        self.assertGreater(len(self.presets), 0, "presets.yaml loaded as empty")

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
        self.assertGreater(len(self.platforms), 0, "platforms.yaml loaded as empty")

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
