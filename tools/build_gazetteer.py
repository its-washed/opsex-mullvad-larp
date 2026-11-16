#!/usr/bin/env python3
"""Regenerate the compact map tables in data/ from the source datasets.

GeoNames input (all of these from https://download.geonames.org/export/dump/ ):

  cities15000.txt        (from cities15000.zip)
  countryInfo.txt
  admin1CodesASCII.txt

Optional Natural Earth input (public domain), passed with --natural-earth:

  ne_10m_populated_places.geojson
  https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/ne_10m_populated_places.geojson

GeoNames data is licensed CC BY 4.0; keep the attribution in the output.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from opsec.gazetteer import STOP_ALIASES, normalize_alias  # noqa: E402

REQUIRED = ("cities15000.txt", "countryInfo.txt", "admin1CodesASCII.txt")
NE_NAME_PREFIX = "NAME_"


def _clean(text: str) -> str:
    return " ".join(text.replace("\t", " ").split())


def _accumulate(bucket: dict, key: str, weight: int, lat: str, lon: str) -> None:
    entry = bucket.setdefault(key, [0, 0.0, 0.0])
    entry[0] += weight
    entry[1] += float(lat) * weight
    entry[2] += float(lon) * weight


def _centroid(entry) -> tuple[float, float]:
    weight, lat_sum, lon_sum = entry
    if not weight:
        return 0.0, 0.0
    return round(lat_sum / weight, 4), round(lon_sum / weight, 4)


def _rank_aliases(names, primary: set[str], limit: int) -> list[str]:
    """Pick the aliases worth keeping: own names first, then Latin, then short."""
    ranked: list[tuple[int, int, int, str]] = []
    seen: set[str] = set()
    for name in names:
        if not name:
            continue
        normalized = normalize_alias(name)
        if normalized in seen or normalized in STOP_ALIASES:
            continue
        if not (2 <= len(normalized) <= 48):
            continue
        if any(ch.isdigit() for ch in normalized):
            continue
        is_primary = normalized in primary
        if not is_primary:
            if len(normalized) < 4:
                continue
            # Short all-caps alternates are airport and city codes, not names.
            if len(name.strip()) <= 5 and name.strip().isupper():
                continue
        seen.add(normalized)
        ranked.append((0 if is_primary else 1, 0 if normalized.isascii() else 1, len(normalized), normalized))
    ranked.sort()
    return [item[-1] for item in ranked[:limit]]


def _add_aliases(store: dict[str, set[int]], names, geonameid: int, limit: int) -> None:
    # names[0:2] are the place's own name and ASCII name; they always win the
    # alias cap, otherwise alphabetical alternates would push them out.
    primary = {normalize_alias(name) for name in names[:2] if name}
    for normalized in _rank_aliases(names, primary, limit):
        store.setdefault(normalized, set()).add(geonameid)


def build(source: Path, target: Path, max_aliases: int) -> None:
    target.mkdir(parents=True, exist_ok=True)

    aliases: dict[str, set[int]] = {}
    city_lines: list[str] = []
    region_weights: dict[str, list[float]] = {}
    country_weights: dict[str, list[float]] = {}

    with open(source / "cities15000.txt", encoding="utf-8") as handle:
        for line in handle:
            parts = line.rstrip("\n").split("\t")
            if len(parts) < 18:
                continue
            geonameid, name, ascii_name, alternates = parts[0], parts[1], parts[2], parts[3]
            lat, lon, country, admin1 = parts[4], parts[5], parts[8], parts[10]
            population = int(parts[14] or 0)
            timezone = parts[17]
            if not country or not timezone:
                continue
            region_code = f"{country}.{admin1}" if admin1 else ""
            city_lines.append("\t".join([
                geonameid, _clean(name), lat, lon, country, region_code, str(population), timezone,
            ]))
            weight = max(population, 1)
            if region_code:
                _accumulate(region_weights, region_code, weight, lat, lon)
            _accumulate(country_weights, country, weight, lat, lon)
            _add_aliases(aliases, [name, ascii_name, *alternates.split(",")], int(geonameid), max_aliases)

    (target / "cities.tsv").write_text("\n".join(city_lines) + "\n", encoding="utf-8")

    alias_lines = []
    for alias in sorted(aliases):
        for geonameid in sorted(aliases[alias]):
            alias_lines.append(f"{alias}\t{geonameid}")
    (target / "aliases.tsv").write_text("\n".join(alias_lines) + "\n", encoding="utf-8")

    region_names: dict[str, str] = {}
    with open(source / "admin1CodesASCII.txt", encoding="utf-8") as handle:
        for line in handle:
            parts = line.rstrip("\n").split("\t")
            if len(parts) >= 2:
                region_names[parts[0]] = _clean(parts[1])
    region_lines = []
    for code, entry in sorted(region_weights.items()):
        name = region_names.get(code)
        if not name:
            continue
        lat, lon = _centroid(entry)
        region_lines.append(f"{code}\t{name}\t{lat}\t{lon}")
    (target / "admin1.tsv").write_text("\n".join(region_lines) + "\n", encoding="utf-8")

    country_lines = []
    with open(source / "countryInfo.txt", encoding="utf-8") as handle:
        for line in handle:
            if line.startswith("#"):
                continue
            parts = line.rstrip("\r\n").split("\t")
            if len(parts) < 9 or not parts[0]:
                continue
            code, name, continent = parts[0], _clean(parts[4]), parts[8]
            entry = country_weights.get(code)
            if entry is None:
                continue
            lat, lon = _centroid(entry)
            country_lines.append(f"{code}\t{name}\t{continent}\t{lat}\t{lon}")
    (target / "countries.tsv").write_text("\n".join(country_lines) + "\n", encoding="utf-8")

    print(f"cities     {len(city_lines):>7}")
    print(f"aliases    {len(alias_lines):>7}")
    print(f"regions    {len(region_lines):>7}")
    print(f"countries  {len(country_lines):>7}")
    _report_sizes(target, ("cities.tsv", "aliases.tsv", "admin1.tsv", "countries.tsv"))


def build_natural_earth(source_file: Path, target: Path, max_aliases: int) -> None:
    """Write places_ne.tsv: an independent populated-places table from Natural Earth."""
    with open(source_file, encoding="utf-8") as handle:
        features = (json.load(handle) or {}).get("features") or []

    rows = []
    for feature in features:
        props = feature.get("properties") or {}
        name = _clean(str(props.get("NAME") or ""))
        lat, lon = props.get("LATITUDE"), props.get("LONGITUDE")
        if not name or lat is None or lon is None:
            continue
        cc = str(props.get("ISO_A2") or "").strip().upper()
        if cc in ("", "-99"):
            cc = ""
        population = max(int(props.get("POP_MAX") or 0), int(props.get("POP_MIN") or 0))
        region = _clean(str(props.get("ADM1NAME") or ""))
        timezone = str(props.get("TIMEZONE") or "").strip()
        names = [props.get("NAME"), props.get("NAMEASCII"), props.get("NAME_EN"), props.get("NAMEALT")]
        names += [
            value for key, value in sorted(props.items())
            if key.startswith(NE_NAME_PREFIX) and key != "NAME_EN" and isinstance(value, str)
        ]
        primary = {normalize_alias(str(props.get("NAME") or "")), normalize_alias(str(props.get("NAMEASCII") or ""))}
        aliases = _rank_aliases([value for value in names if isinstance(value, str)], primary, max_aliases)
        if not aliases:
            continue
        rows.append("\t".join([
            "|".join(aliases), name, f"{float(lat):.5f}", f"{float(lon):.5f}",
            cc, region, str(population), timezone,
        ]))

    (target / "places_ne.tsv").write_text("\n".join(rows) + "\n", encoding="utf-8")
    print(f"ne places  {len(rows):>7}")
    _report_sizes(target, ("places_ne.tsv",))


def _report_sizes(target: Path, names) -> None:
    for name in names:
        path = target / name
        if path.is_file():
            print(f"  {name:<14} {path.stat().st_size / 1024:8.0f} kB")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--geonames", required=True, type=Path, help="directory holding the GeoNames text dumps")
    parser.add_argument(
        "--natural-earth",
        type=Path,
        help="ne_10m_populated_places.geojson; skipped when omitted",
    )
    parser.add_argument("--out", type=Path, default=ROOT / "data", help="output directory (default: data/)")
    parser.add_argument("--max-aliases", type=int, default=8, help="aliases kept per place (default: 8)")
    args = parser.parse_args()

    missing = [name for name in REQUIRED if not (args.geonames / name).is_file()]
    if missing:
        print(f"missing input files: {', '.join(missing)}", file=sys.stderr)
        return 2
    build(args.geonames, args.out, args.max_aliases)
    if args.natural_earth:
        if not args.natural_earth.is_file():
            print(f"missing Natural Earth file: {args.natural_earth}", file=sys.stderr)
            return 2
        build_natural_earth(args.natural_earth, args.out, args.max_aliases)
    else:
        print("natural earth skipped: pass --natural-earth to rebuild places_ne.tsv")
    return 0


if __name__ == "__main__":
    sys.exit(main())
