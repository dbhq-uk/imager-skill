#!/usr/bin/env python3
"""Tests for gpt_image_2.py.

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

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import gpt_image_2  # noqa: E402

SCRIPT = Path(__file__).parent.parent / "scripts" / "gpt_image_2.py"


def run_cli(*args: str, env_extra: dict | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["OPENAI_API_KEY"] = "test-key-not-real"
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True, text=True, env=env,
    )


class TestCostModel(unittest.TestCase):

    def test_known_combinations(self):
        self.assertAlmostEqual(gpt_image_2.estimate_cost("low", "off", 1), 0.006)
        self.assertAlmostEqual(gpt_image_2.estimate_cost("high", "high", 1), 0.42)
        self.assertAlmostEqual(gpt_image_2.estimate_cost("medium", "medium", 1), 0.09)

    def test_scales_linearly_with_n(self):
        single = gpt_image_2.estimate_cost("medium", "low", 1)
        self.assertAlmostEqual(gpt_image_2.estimate_cost("medium", "low", 4), single * 4)

    def test_zero_images_costs_nothing(self):
        self.assertEqual(gpt_image_2.estimate_cost("high", "high", 0), 0.0)

    def test_unknown_quality_falls_back_to_high(self):
        # Deliberate: an unrecognised quality must not under-quote the user.
        self.assertEqual(
            gpt_image_2.estimate_cost("ludicrous", "off", 1),
            gpt_image_2.estimate_cost("high", "off", 1),
        )

    def test_unknown_thinking_falls_back_to_most_expensive(self):
        self.assertAlmostEqual(gpt_image_2.cost_per_unit("high", "nonsense"), 0.21)

    def test_cost_per_unit_matches_estimate_for_one(self):
        for quality in ("low", "medium", "high"):
            for thinking in gpt_image_2.THINKING_LEVELS:
                with self.subTest(quality=quality, thinking=thinking):
                    self.assertAlmostEqual(
                        gpt_image_2.cost_per_unit(quality, thinking),
                        gpt_image_2.estimate_cost(quality, thinking, 1),
                    )


class TestPresetsFile(unittest.TestCase):
    """presets.yaml must stay structurally sound -- nothing else validates it."""

    @classmethod
    def setUpClass(cls):
        cls.presets = gpt_image_2.load_presets()

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
                    "{subject}", preset["prompt"],
                    f"preset '{name}' drops the user's subject entirely",
                )

    def test_thinking_levels_are_valid_when_present(self):
        for name, preset in self.presets.items():
            if "thinking" in preset:
                with self.subTest(preset=name):
                    self.assertIn(preset["thinking"], gpt_image_2.THINKING_LEVELS)


class TestPlatformsFile(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.platforms = gpt_image_2.load_platforms()

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


class TestComposePrompt(unittest.TestCase):

    def test_no_preset_returns_prompt_unchanged(self):
        prompt, thinking = gpt_image_2.compose_prompt("a red bicycle", None)
        self.assertEqual(prompt, "a red bicycle")
        self.assertIsNone(thinking)

    def test_preset_substitutes_the_subject(self):
        name = next(iter(gpt_image_2.load_presets()))
        prompt, _ = gpt_image_2.compose_prompt("a red bicycle", name)
        self.assertIn("a red bicycle", prompt)
        self.assertNotIn("{subject}", prompt)

    def test_preset_returns_its_thinking_level(self):
        presets = gpt_image_2.load_presets()
        with_thinking = [n for n, p in presets.items() if p.get("thinking")]
        if not with_thinking:
            self.skipTest("no preset declares a thinking level")
        name = with_thinking[0]
        _, thinking = gpt_image_2.compose_prompt("subject", name)
        self.assertEqual(thinking, presets[name]["thinking"])

    def test_unknown_preset_exits_nonzero(self):
        with self.assertRaises(SystemExit) as ctx:
            gpt_image_2.compose_prompt("subject", "no-such-preset")
        self.assertNotEqual(ctx.exception.code, 0)


class TestBuildMultipart(unittest.TestCase):
    """The one piece of hand-rolled wire protocol in the repo."""

    def test_plain_field_has_no_content_type(self):
        body, content_type = gpt_image_2._build_multipart([("model", "gpt-image-2", None)])
        text = body.decode()
        self.assertIn('Content-Disposition: form-data; name="model"', text)
        self.assertNotIn("Content-Type:", text)
        self.assertIn("gpt-image-2", text)
        self.assertTrue(content_type.startswith("multipart/form-data; boundary="))

    def test_boundary_in_header_matches_body(self):
        body, content_type = gpt_image_2._build_multipart([("a", "b", None)])
        boundary = content_type.split("boundary=")[1]
        self.assertIn(f"--{boundary}".encode(), body)
        self.assertTrue(body.endswith(f"--{boundary}--".encode()))

    def test_boundary_is_unique_per_call(self):
        _, first = gpt_image_2._build_multipart([("a", "b", None)])
        _, second = gpt_image_2._build_multipart([("a", "b", None)])
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
                body, _ = gpt_image_2._build_multipart([("image", b"\x89PNG", filename)])
                self.assertIn(f"Content-Type: {expected}".encode(), body)

    def test_unknown_extension_defaults_to_png(self):
        body, _ = gpt_image_2._build_multipart([("image", b"data", "photo.bmp")])
        self.assertIn(b"Content-Type: image/png", body)

    def test_binary_value_survives_intact(self):
        payload = bytes(range(256))
        body, _ = gpt_image_2._build_multipart([("image", payload, "x.png")])
        self.assertIn(payload, body)

    def test_uses_crlf_line_endings(self):
        body, _ = gpt_image_2._build_multipart([("a", "b", None)])
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
            gpt_image_2.CONFIG_DIR,
            gpt_image_2.HISTORY_FILE,
            gpt_image_2.LAST_RUN_FILE,
        )
        gpt_image_2.CONFIG_DIR = root
        gpt_image_2.HISTORY_FILE = root / "history.jsonl"
        gpt_image_2.LAST_RUN_FILE = root / "last.json"

    def tearDown(self):
        (
            gpt_image_2.CONFIG_DIR,
            gpt_image_2.HISTORY_FILE,
            gpt_image_2.LAST_RUN_FILE,
        ) = self._saved
        self.tmp.cleanup()

    def _entry(self, prompt="a subject", project=None):
        return gpt_image_2.HistoryEntry(
            timestamp="2026-07-28T12:00:00",
            prompt=prompt,
            preset=None,
            platform=None,
            thinking="off",
            quality="high",
            provider="openai",
            n=1,
            seed=None,
            output="out.png",
            project=project,
            estimated_cost=0.21,
        )

    def test_missing_history_reads_as_empty(self):
        self.assertEqual(gpt_image_2.load_history(), [])

    def test_missing_last_run_reads_as_none(self):
        self.assertIsNone(gpt_image_2.load_last_run())

    def test_save_then_load_round_trips(self):
        gpt_image_2.save_history(self._entry(prompt="a red bicycle"))
        entries = gpt_image_2.load_history()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["prompt"], "a red bicycle")

    def test_save_history_also_writes_last_run(self):
        gpt_image_2.save_history(self._entry(prompt="most recent"))
        self.assertEqual(gpt_image_2.load_last_run()["prompt"], "most recent")

    def test_load_history_returns_the_last_n(self):
        for i in range(5):
            gpt_image_2.save_history(self._entry(prompt=f"prompt-{i}"))
        entries = gpt_image_2.load_history(n=2)
        self.assertEqual([e["prompt"] for e in entries], ["prompt-3", "prompt-4"])

    def test_project_filter_selects_only_that_project(self):
        gpt_image_2.save_history(self._entry(prompt="alpha", project="alpha-proj"))
        gpt_image_2.save_history(self._entry(prompt="beta", project="beta-proj"))
        entries = gpt_image_2.load_history(project="alpha-proj")
        self.assertEqual([e["prompt"] for e in entries], ["alpha"])

    def test_blank_lines_are_skipped(self):
        gpt_image_2.save_history(self._entry())
        with gpt_image_2.HISTORY_FILE.open("a") as f:
            f.write("\n\n")
        self.assertEqual(len(gpt_image_2.load_history()), 1)

    def test_history_is_valid_jsonl(self):
        gpt_image_2.save_history(self._entry())
        for line in gpt_image_2.HISTORY_FILE.read_text().splitlines():
            if line.strip():
                json.loads(line)


class TestCli(unittest.TestCase):
    """Subprocess-level checks. --dry-run returns before any request is built."""

    def test_help_exits_zero(self):
        result = run_cli("--help")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_no_arguments_prints_help(self):
        result = run_cli()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("usage", result.stdout.lower())

    def test_list_presets_lists_a_real_preset(self):
        result = run_cli("list-presets")
        self.assertEqual(result.returncode, 0, result.stderr)
        name = next(iter(gpt_image_2.load_presets()))
        self.assertIn(name, result.stdout)

    def test_list_platforms_lists_a_real_platform(self):
        result = run_cli("list-platforms")
        self.assertEqual(result.returncode, 0, result.stderr)
        name = next(iter(gpt_image_2.load_platforms()))
        self.assertIn(name, result.stdout)

    def test_dry_run_reports_prompt_and_cost_without_calling_out(self):
        result = run_cli("--dry-run", "a red bicycle")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("a red bicycle", result.stdout)
        self.assertIn("Est. cost:", result.stdout)

    def test_dry_run_applies_the_preset(self):
        name = next(iter(gpt_image_2.load_presets()))
        result = run_cli("--dry-run", "--preset", name, "a red bicycle")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("{subject}", result.stdout)
        self.assertIn("a red bicycle", result.stdout)

    def test_draft_flag_forces_low_quality(self):
        result = run_cli("--dry-run", "--draft", "a red bicycle")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("DRAFT", result.stdout)
        self.assertIn("Quality:   low", result.stdout)

    def test_estimate_only_reports_cost(self):
        result = run_cli("--estimate", "a red bicycle")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Estimated cost", result.stdout)

    def test_missing_api_key_exits_nonzero(self):
        result = run_cli("--dry-run", "a subject", env_extra={"OPENAI_API_KEY": ""})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("No API key", result.stderr)

    def test_unknown_preset_exits_nonzero(self):
        result = run_cli("--dry-run", "--preset", "no-such-preset", "a subject")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unknown preset", result.stderr)

    def test_n_above_ten_is_rejected(self):
        result = run_cli("--dry-run", "--n", "11", "a subject")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--n must be between 1 and 10", result.stderr)

    def test_n_below_one_is_rejected(self):
        result = run_cli("--dry-run", "--n", "0", "a subject")
        self.assertNotEqual(result.returncode, 0)

    def test_invalid_thinking_level_is_rejected(self):
        result = run_cli("--dry-run", "--thinking", "extreme", "a subject")
        self.assertNotEqual(result.returncode, 0)

    def test_invalid_provider_is_rejected(self):
        result = run_cli("--dry-run", "--provider", "nobody", "a subject")
        self.assertNotEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
