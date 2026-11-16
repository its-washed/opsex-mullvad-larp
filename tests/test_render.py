"""Renderer behaviour: colour modes, plain output and the empty-tier layout."""

from __future__ import annotations

import io
import os
import unittest
from unittest import mock

from opsec import exif, locate, render


def _report(*, with_city: bool = True) -> locate.Report:
    report = locate.Report(path="/photos/paris_spring_break.jpg", size=345_678, exif=exif.blank())
    if with_city:
        report.semi = [locate.Candidate(
            name="Paris",
            cc="FR",
            country="France",
            region="Ile-de-France",
            lat=48.85341,
            lon=2.3488,
            score=0.52,
            tier="semi",
            pop=2_138_551,
            reasons=['file name matched "paris"'],
            sources=["GeoNames", "Natural Earth"],
        )]
    return report


class ModeTests(unittest.TestCase):
    def test_never_disables_colour(self):
        self.assertEqual(render.resolve_mode("never"), "none")

    def test_auto_without_a_terminal_is_plain(self):
        with mock.patch("sys.stdout", new=io.StringIO()):
            self.assertEqual(render.resolve_mode("auto"), "none")

    def test_auto_honours_no_color(self):
        with mock.patch("sys.stdout", io.StringIO()), mock.patch.dict(os.environ, {"NO_COLOR": "1"}):
            self.assertEqual(render.resolve_mode("auto"), "none")

    def test_forced_colour_picks_256_unless_truecolour_is_advertised(self):
        with mock.patch.dict(os.environ, {"COLORTERM": ""}):
            self.assertEqual(render.resolve_mode("always"), "256")
        with mock.patch.dict(os.environ, {"COLORTERM": "truecolor"}):
            self.assertEqual(render.resolve_mode("always"), "truecolor")


class OutputTests(unittest.TestCase):
    def test_plain_output_has_no_escapes_and_shows_sources(self):
        text = render.render(_report(), render.Style("none"), width=88)
        self.assertNotIn("\x1b", text)
        self.assertIn("SEMI-LIKELY", text)
        self.assertIn("Paris", text)
        self.assertIn("sources: GeoNames · Natural Earth", text)
        self.assertIn("0.52", text)

    def test_truecolour_uses_24_bit_escapes(self):
        text = render.render(_report(), render.Style("truecolor"), width=88)
        self.assertIn("\x1b[38;2;", text)
        self.assertNotIn("\x1b[38;5;", text)

    def test_256_colour_uses_palette_escapes(self):
        text = render.render(_report(), render.Style("256"), width=88)
        self.assertIn("\x1b[38;5;", text)
        self.assertNotIn("\x1b[38;2;", text)

    def test_empty_tiers_state_the_reason(self):
        text = render.render(_report(with_city=False), render.Style("none"), width=88)
        self.assertIn("no GPS coordinates in the file", text)
        self.assertIn("no usable place names found", text)
        self.assertIn("nothing to rank", text)

    def test_exact_block_shows_coordinates_and_map_link(self):
        exact = locate.Candidate(
            name="Avenue Gustave Eiffel, Paris",
            lat=48.85837,
            lon=2.294481,
            score=1.0,
            tier="exact",
            kind="fix",
            place="Avenue Gustave Eiffel, Paris",
            reasons=["GPS coordinates stored in the image EXIF"],
            sources=["OpenStreetMap Nominatim"],
        )
        report = _report(with_city=False)
        report.exact = exact
        text = render.render(report, render.Style("none"), width=88)
        self.assertIn("48.858370, 2.294481", text)
        self.assertIn("https://www.openstreetmap.org/?mlat=48.858370", text)
        self.assertIn("Avenue Gustave Eiffel", text)

    def test_bar_fills_in_proportion_to_confidence(self):
        style = render.Style("none")
        self.assertEqual(style.bar(0.0), "░" * 10)
        self.assertEqual(style.bar(1.0), "█" * 10)
        self.assertEqual(style.bar(0.5), "█" * 5 + "░" * 5)


if __name__ == "__main__":
    unittest.main()
