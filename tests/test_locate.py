"""Tests for evidence scoring and the three location tiers."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from opsec import locate

from .test_exif import build_jpeg

FAKE_JPEG = b"\xff\xd8\xff\xd9"


class ScoreTests(unittest.TestCase):
    def test_multi_word_names_beat_single_words(self):
        self.assertGreater(
            locate.match_score(2, 20_000, 1.0),
            locate.match_score(1, 20_000, 1.0),
        )

    def test_bigger_places_score_higher(self):
        self.assertGreater(
            locate.match_score(1, 2_000_000, 1.0),
            locate.match_score(1, 30_000, 1.0),
        )

    def test_image_text_beats_file_name(self):
        self.assertGreater(
            locate.match_score(1, 300_000, locate.SOURCE_WEIGHTS["ocr"]),
            locate.match_score(1, 300_000, locate.SOURCE_WEIGHTS["filename"]),
        )

    def test_low_ocr_confidence_lowers_score(self):
        self.assertLess(
            locate.match_score(1, 300_000, 1.0, ocr_conf=0.4),
            locate.match_score(1, 300_000, 1.0, ocr_conf=0.9),
        )

    def test_tier_threshold(self):
        self.assertEqual(locate.tier_for(0.10), "unlikely")
        self.assertEqual(locate.tier_for(locate.SEMI_MIN), "semi")

    def test_offset_parsing(self):
        self.assertEqual(locate.parse_offset("+02:00"), 2.0)
        self.assertEqual(locate.parse_offset("-05:30"), -5.5)
        self.assertIsNone(locate.parse_offset("nonsense"))
        self.assertIsNone(locate.parse_offset(None))


class AnalyseTests(unittest.TestCase):
    def _image(self, folder: str, name: str, blob: bytes = FAKE_JPEG) -> str:
        path = Path(folder) / name
        path.write_bytes(blob)
        return str(path)

    def test_gps_image_lands_in_exact_tier(self):
        with tempfile.TemporaryDirectory() as folder:
            path = self._image(folder, "IMG_0001.jpg", build_jpeg())
            report = locate.analyse(path, online=False, use_ocr=False)
        self.assertIsNotNone(report.exact)
        self.assertEqual(report.exact.tier, "exact")
        self.assertAlmostEqual(report.exact.lat, 48.85837, places=4)
        self.assertAlmostEqual(report.exact.distance_km, 0, delta=8)
        self.assertEqual(report.semi, [])
        self.assertEqual(report.unlikely, [])

    def test_network_lookup_is_opt_in(self):
        calls = {"reverse": [], "search": [], "wiki": []}
        original_reverse = locate.geocode.reverse
        original_search = locate.geocode.search_place
        original_nearby = locate.wikipedia.nearby

        def spy_reverse(lat, lon, **kwargs):
            calls["reverse"].append(kwargs)
            return None

        locate.geocode.reverse = spy_reverse
        locate.geocode.search_place = lambda name, cc="", **kwargs: calls["search"].append(name)
        locate.wikipedia.nearby = lambda lat, lon, **kwargs: calls["wiki"].append((lat, lon)) or []
        try:
            with tempfile.TemporaryDirectory() as folder:
                gps = locate.analyse(self._image(folder, "IMG_0001.jpg", build_jpeg()), use_ocr=False)
                named = locate.analyse(self._image(folder, "paris_spring_break.jpg"), use_ocr=False)
        finally:
            locate.geocode.reverse = original_reverse
            locate.geocode.search_place = original_search
            locate.wikipedia.nearby = original_nearby
        self.assertEqual(calls["reverse"], [{"online": False}])
        self.assertEqual(calls["search"], [])
        self.assertEqual(calls["wiki"], [])
        self.assertIsNotNone(gps.exact)
        self.assertTrue(named.semi)

    def test_file_name_hint_reaches_semi_likely(self):
        with tempfile.TemporaryDirectory() as folder:
            path = self._image(folder, "paris_spring_break.jpg")
            report = locate.analyse(path, online=False, use_ocr=False)
        semi = {(item.name, item.cc) for item in report.semi}
        self.assertIn(("Paris", "FR"), semi)
        self.assertIsNone(report.exact)

    def test_region_hint_carries_its_country_not_an_admin_code(self):
        with tempfile.TemporaryDirectory() as folder:
            path = self._image(folder, "texas_roadtrip.jpg")
            report = locate.analyse(path, online=False, use_ocr=False)
        region = next(item for item in report.semi + report.unlikely if item.kind == "region")
        self.assertEqual(region.name, "Texas")
        self.assertEqual(region.cc, "US")
        self.assertEqual(region.country, "United States")
        self.assertEqual(region.region, "")

    def test_country_hint_is_not_repeated_as_its_own_parent(self):
        with tempfile.TemporaryDirectory() as folder:
            path = self._image(folder, "iceland_trip.jpg")
            report = locate.analyse(path, online=False, use_ocr=False)
        country = next(item for item in report.semi + report.unlikely if item.kind == "country")
        self.assertEqual(country.name, "Iceland")
        self.assertEqual(country.cc, "IS")
        self.assertEqual(country.country, "")

    def test_no_evidence_is_reported_honestly(self):
        with tempfile.TemporaryDirectory() as folder:
            path = self._image(folder, "IMG_20240501_123456.jpg")
            report = locate.analyse(path, online=False, use_ocr=False)
        self.assertIsNone(report.exact)
        self.assertEqual(report.semi, [])
        self.assertEqual(report.unlikely, [])
        self.assertTrue(any("no location evidence" in note for note in report.notes))

    def test_report_serialises_to_json(self):
        with tempfile.TemporaryDirectory() as folder:
            path = self._image(folder, "berlin_trip.jpg")
            report = locate.analyse(path, online=False, use_ocr=False)
        payload = json.loads(json.dumps(report.to_dict()))
        self.assertEqual(payload["file"], path)
        self.assertIn("semi_likely", payload)
        self.assertIn("sources", payload["semi_likely"][0])


class FusionTests(unittest.TestCase):
    def test_second_offline_dataset_raises_confidence(self):
        original = locate.gazetteer.match_ne_places
        locate.gazetteer.match_ne_places = lambda phrases, data_dir=None: [
            (locate.gazetteer.NePlace("Paris", 48.8566, 2.3522, "FR", "Ile-de-France", 2_100_000, "Europe/Paris"), ["paris"])
        ] if "paris" in phrases else []
        try:
            with tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / "paris_spring_break.jpg"
                path.write_bytes(FAKE_JPEG)
                report = locate.analyse(str(path), use_ocr=False)
        finally:
            locate.gazetteer.match_ne_places = original
        top = next(item for item in report.semi if item.cc == "FR")
        self.assertIn("GeoNames", top.sources)
        self.assertIn("Natural Earth", top.sources)
        self.assertIn("GeoNames and Natural Earth agree on this place", top.reasons)
        self.assertGreater(top.score, 0.45)

    def test_merge_tolerates_missing_country_code(self):
        original = locate.gazetteer.match_ne_places
        # Natural Earth leaves ISO_A2 blank for some places; that must not stop
        # the agreement bonus when the coordinates are the same place.
        locate.gazetteer.match_ne_places = lambda phrases, data_dir=None: [
            (locate.gazetteer.NePlace("Arezzo", 43.4628, 11.8807, "", "Tuscany", 99_000, "Europe/Rome"), ["arezzo"])
        ] if "arezzo" in phrases else []
        try:
            with tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / "arezzo_trip.jpg"
                path.write_bytes(FAKE_JPEG)
                report = locate.analyse(str(path), use_ocr=False)
        finally:
            locate.gazetteer.match_ne_places = original
        top = next(item for item in report.semi if item.name == "Arezzo")
        self.assertIn("GeoNames", top.sources)
        self.assertIn("Natural Earth", top.sources)
        self.assertEqual(top.cc, "IT")
        self.assertEqual(top.region, "Tuscany")


class OnlineEnrichmentTests(unittest.TestCase):
    def test_online_sources_confirm_and_raise_confidence(self):
        original_search = locate.geocode.search_place
        original_nearby = locate.wikipedia.nearby
        locate.geocode.search_place = lambda name, cc="", **kwargs: {
            "lat": 48.8600, "lon": 2.3376, "text": "Paris, Ile-de-France, France",
        }
        locate.wikipedia.nearby = lambda lat, lon, **kwargs: [{"title": "Paris", "dist_m": 120.0}]
        try:
            with tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / "paris_spring_break.jpg"
                path.write_bytes(FAKE_JPEG)
                report = locate.analyse(str(path), online=True, use_ocr=False)
        finally:
            locate.geocode.search_place = original_search
            locate.wikipedia.nearby = original_nearby
        top = next(item for item in report.semi if item.cc == "FR")
        self.assertIn("OpenStreetMap", top.sources)
        self.assertIn("Wikipedia geosearch", top.sources)
        self.assertGreater(top.score, 0.6)


if __name__ == "__main__":
    unittest.main()
