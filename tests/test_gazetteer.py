"""Tests for the place tables generated from GeoNames."""

from __future__ import annotations

import unittest
from pathlib import Path

from opsec import gazetteer


class NormalizeTests(unittest.TestCase):
    def test_diacritics_and_case_fold(self):
        self.assertEqual(gazetteer.normalize_alias("München"), "munchen")
        self.assertEqual(gazetteer.normalize_alias("ZÜRICH"), "zurich")

    def test_punctuation_and_hyphens(self):
        self.assertEqual(gazetteer.normalize_alias("Paris, France"), "paris france")
        self.assertEqual(gazetteer.normalize_alias("Sankt-Gallen"), "sankt gallen")
        self.assertEqual(gazetteer.normalize_alias("  New   York  "), "new york")
        self.assertEqual(gazetteer.normalize_alias("paris_spring_break"), "paris spring break")

    def test_keeps_non_latin_scripts(self):
        self.assertEqual(gazetteer.normalize_alias("Москва"), "москва")


class TableTests(unittest.TestCase):
    def test_finds_paris(self):
        hits = gazetteer.find_city_ids({"paris"})
        self.assertTrue(hits)
        cities = gazetteer.cities_by_id(hits)
        self.assertTrue(any(city.cc == "FR" for city in cities.values()))

    def test_generic_words_are_not_evidence(self):
        self.assertEqual(gazetteer.find_city_ids({"central"}), {})

    def test_nearest_matches_the_eiffel_tower(self):
        hits = gazetteer.nearest(48.85837, 2.294481, limit=1)
        self.assertEqual(len(hits), 1)
        city, distance = hits[0]
        self.assertLess(distance, 8.0)
        self.assertEqual(city.cc, "FR")

    def test_country_and_region_names(self):
        self.assertEqual(gazetteer.country_name("FR"), "France")
        self.assertEqual(
            gazetteer.normalize_alias(gazetteer.region_name("FR.11")),
            "ile de france",
        )

    def test_offset_band_returns_cities(self):
        cities = gazetteer.cities_in_offset_band(9.0, limit=5)
        self.assertTrue(cities)
        self.assertTrue(all(city.pop > 0 for city in cities))

    def test_haversine_known_distance(self):
        # Paris to London is about 344 km.
        self.assertAlmostEqual(gazetteer.haversine_km(48.8566, 2.3522, 51.5072, -0.1276), 344, delta=5)

    def test_natural_earth_table_matches(self):
        self.assertTrue(gazetteer.ne_places_available())
        hits = gazetteer.match_ne_places({"colonia del sacramento"})
        self.assertTrue(hits)
        place, aliases = hits[0]
        self.assertAlmostEqual(place.lat, -34.47, delta=0.3)
        self.assertEqual(place.cc, "UY")
        self.assertIn("colonia del sacramento", aliases)

    def test_missing_natural_earth_table_is_not_fatal(self):
        self.assertEqual(gazetteer.match_ne_places({"paris"}, data_dir=Path("/nonexistent")), [])


if __name__ == "__main__":
    unittest.main()
