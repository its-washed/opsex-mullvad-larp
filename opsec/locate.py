"""Turn image evidence into ranked location candidates.

Three tiers, each backed by the evidence it actually has:

``exact``       a GPS fix stored in EXIF, resolved to an address or nearest city
``semi``        place names read from the image text or the file name, matched
                against two independent place datasets and optionally confirmed
                online, plus region/country names
``unlikely``    weak signals only: a camera-clock offset band, or a place name
                seen in the file name with a large search area

Datasets: GeoNames (cities, regions, countries) and Natural Earth populated
places are offline; OpenStreetMap Nominatim search and Wikipedia geosearch are
opt-in confirmations. A place that several datasets agree on scores higher than
one that only a single dataset knows.

Confidence is a calibrated heuristic over the strength of the evidence (how
specific the name is, how large the place is, which source produced it, OCR
word confidence, cross-source agreement), not a probability from a model.
"""

from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from . import exif, gazetteer, geocode, ocr, wikipedia

SEMI_MIN = 0.30
MAX_SCORE = 0.95
SOURCE_WEIGHTS = {"ocr": 1.0, "filename": 0.55}

OFFLINE_SOURCES = ("GeoNames", "Natural Earth")
AGREEMENT_BONUS = 0.10
OSM_BONUS = 0.12
OSM_AGREE_KM = 25.0
WIKI_BONUS = 0.08
ENRICH_LIMIT = 3
MERGE_RADIUS_KM = 50.0

FILENAME_STOPWORDS = frozenset({
    "img", "image", "images", "pic", "pics", "pix", "photo", "photos", "snap",
    "dsc", "dscn", "dscf", "dcim", "mvimg", "screenshot", "screen", "shot",
    "camera", "android", "iphone", "phone", "pixel", "samsung", "edited",
    "edit", "final", "copy", "export", "download", "downloads", "whatsapp",
    "telegram", "signal", "shared", "uploads", "upload", "temp", "tmp", "new",
    "old", "backup", "thumb", "thumbnail", "original", "enhanced", "crop",
    "cmp", "panorama", "hdr", "live", "framed", "watermarked",
})

_OFFSET_RE = re.compile(r"^([+-])(\d{1,2}):?(\d{2})$")


@dataclass
class Candidate:
    name: str
    cc: str = ""
    country: str = ""
    region: str = ""
    lat: Optional[float] = None
    lon: Optional[float] = None
    score: float = 0.0
    tier: str = "unlikely"
    kind: str = "city"
    pop: int = 0
    reasons: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    place: str = ""
    distance_km: Optional[float] = None


@dataclass
class Report:
    path: str
    size: int
    exif: dict
    exact: Optional[Candidate] = None
    semi: list[Candidate] = field(default_factory=list)
    unlikely: list[Candidate] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        def candidate(item: Optional[Candidate]) -> Optional[dict]:
            if item is None:
                return None
            return {
                "name": item.name,
                "kind": item.kind,
                "country": item.country,
                "country_code": item.cc,
                "region": item.region,
                "lat": item.lat,
                "lon": item.lon,
                "score": round(item.score, 3),
                "tier": item.tier,
                "population": item.pop,
                "sources": item.sources,
                "reasons": item.reasons,
                "place": item.place,
                "distance_km": item.distance_km,
                "map_url": map_url(item.lat, item.lon),
            }

        return {
            "file": self.path,
            "bytes": self.size,
            "exif": self.exif,
            "exact": candidate(self.exact),
            "semi_likely": [candidate(item) for item in self.semi],
            "unlikely": [candidate(item) for item in self.unlikely],
            "notes": self.notes,
        }


def map_url(lat: Optional[float], lon: Optional[float]) -> Optional[str]:
    if lat is None or lon is None:
        return None
    return (
        f"https://www.openstreetmap.org/?mlat={lat:.6f}&mlon={lon:.6f}"
        f"#map=16/{lat:.6f}/{lon:.6f}"
    )


def match_score(words: int, pop: int, weight: float, ocr_conf: float = 1.0, occurrences: int = 1) -> float:
    """Confidence for one place-name match."""
    pop_factor = min(1.0, math.log10(1 + max(pop, 0)) / 6.5)
    if words >= 2:
        specificity = 1.0
    elif pop >= 500_000:
        specificity = 0.8
    elif pop >= 100_000:
        specificity = 0.7
    elif pop >= 20_000:
        specificity = 0.6
    else:
        specificity = 0.5
    score = weight * (0.22 + 0.40 * specificity + 0.20 * pop_factor)
    score *= 0.6 + 0.4 * min(1.0, max(0.0, ocr_conf))
    score += 0.03 * (min(occurrences, 4) - 1)
    return round(min(0.92, score), 3)


def tier_for(score: float) -> str:
    return "semi" if score >= SEMI_MIN else "unlikely"


def parse_offset(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    match = _OFFSET_RE.match(value.strip())
    if not match:
        return None
    hours = int(match.group(2)) + int(match.group(3)) / 60.0
    return -hours if match.group(1) == "-" else hours


def offset_label(hours: float) -> str:
    sign = "+" if hours >= 0 else "-"
    minutes = int(round(abs(hours) * 60))
    return f"UTC{sign}{minutes // 60:02d}:{minutes % 60:02d}"


def parse_datetime(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y:%m:%d %H:%M:%S")
    except ValueError:
        return None


def _phrases(tokens: list[str]) -> set[str]:
    phrases: set[str] = set()
    for size in (1, 2, 3):
        for index in range(len(tokens) - size + 1):
            window = tokens[index:index + size]
            if any(len(token) < 3 for token in window):
                continue
            phrases.add(" ".join(window))
    return phrases


def filename_phrases(path: str) -> set[str]:
    components = os.path.normpath(os.path.abspath(path)).split(os.sep)[-3:]
    tokens: list[str] = []
    for component in components:
        stem = os.path.splitext(component)[0]
        for token in gazetteer.normalize_alias(stem).split():
            if len(token) < 3 or token.isdigit() or token in FILENAME_STOPWORDS:
                continue
            if any(ch.isdigit() for ch in token):
                continue
            tokens.append(token)
    return _phrases(tokens)


def ocr_phrases(lines: list[list[tuple[str, float]]]) -> set[str]:
    phrases: set[str] = set()
    for line in lines:
        tokens: list[str] = []
        for word, _ in line:
            tokens.extend(gazetteer.normalize_alias(word).split())
        phrases |= _phrases(tokens)
    return phrases


def analyse(
    path: str,
    *,
    online: bool = False,
    use_ocr: bool = True,
    ocr_lang: str = "eng",
    top: int = 5,
) -> Report:
    report = Report(path=path, size=os.path.getsize(path), exif=exif.read_exif(path))
    info = report.exif
    if not info["has_exif"]:
        report.notes.append("no readable EXIF metadata (stripped, re-encoded or generated image)")

    offset = parse_offset(info.get("offset_time_original"))
    when = parse_datetime(info.get("datetime_original"))

    gps = info.get("gps")
    if gps:
        place = geocode.reverse(gps["lat"], gps["lon"], online=online)
        exact = Candidate(
            name=place["text"] if place else f'{gps["lat"]:.5f}, {gps["lon"]:.5f}',
            lat=gps["lat"],
            lon=gps["lon"],
            score=1.0,
            tier="exact",
            kind="fix",
            reasons=["GPS coordinates stored in the image EXIF"],
            # An offline hit names the nearest city, which already has its own row.
            place=place["text"] if place and place["distance_km"] is None else "",
            distance_km=place["distance_km"] if place else None,
            sources=[place["source"]] if place else ["EXIF GPS"],
        )
        if place:
            exact.reasons.append(f'resolved with {place["source"]}')
        else:
            exact.reasons.append("no geocoder reachable, raw coordinates only")
        report.exact = exact
        report.notes.append("image text and file name not analysed: the GPS fix is authoritative")
        return report

    if info.get("has_exif"):
        report.notes.append("EXIF present but no GPS tags (camera or export stripped the fix)")

    evidence: dict[str, set[str]] = {}
    ocr_conf = 1.0
    if use_ocr:
        lines, ocr_conf, note = ocr.read_lines(path, ocr_lang)
        if note:
            report.notes.append(note)
        found = ocr_phrases(lines)
        if found:
            evidence["ocr"] = found
            report.notes.append(f"read {sum(len(line) for line in lines)} words from the image")
    else:
        report.notes.append("image text skipped (--no-ocr)")

    from_name = filename_phrases(path)
    if from_name:
        evidence["filename"] = from_name

    candidates = _text_candidates(evidence, ocr_conf, offset, when)
    if online and candidates:
        report.notes.append("online lookups enabled: OpenStreetMap search, Wikipedia geosearch")
        _enrich_online(candidates, report.notes)
        for item in candidates:
            item.score = round(min(MAX_SCORE, item.score), 3)
            item.tier = tier_for(item.score)
        candidates.sort(key=lambda item: (-item.score, -item.pop))

    report.semi = [item for item in candidates if item.tier == "semi"][:top]
    report.unlikely = [item for item in candidates if item.tier == "unlikely"][:top]

    if offset is not None:
        band = _offset_band(offset, when)
        if band is not None:
            report.unlikely = (report.unlikely + [band])[:top]

    if not report.semi and not report.unlikely:
        report.notes.append(
            "no location evidence: no GPS, no place names in the image or file name"
        )
        if not ocr.available() and use_ocr:
            report.notes.append("install tesseract for image text (signs, plates, shop fronts)")
    return report


def _text_candidates(
    evidence: dict[str, set[str]],
    ocr_conf: float,
    offset: Optional[float],
    when: Optional[datetime],
) -> list[Candidate]:
    phrases = set().union(*evidence.values()) if evidence else set()
    if not phrases:
        return []

    origin_of: dict[str, set[str]] = {}
    for origin, found in evidence.items():
        for phrase in found:
            origin_of.setdefault(phrase, set()).add(origin)

    countries = gazetteer.load_countries()
    regions = gazetteer.load_regions()
    # Folded once per run: matching then costs one lookup per phrase instead of
    # re-folding every region and country name for every phrase.
    region_names = {gazetteer.normalize_alias(item.name): item for item in regions.values()}
    country_names = {gazetteer.normalize_alias(item.name): item for item in countries.values()}

    found = _geonames_candidates(phrases, origin_of, ocr_conf, offset, when, countries, regions)
    found += _natural_earth_candidates(phrases, origin_of, ocr_conf, offset, when, countries)
    found += _admin_candidates(phrases, origin_of, region_names, country_names, countries)

    merged = _merge_candidates(found)
    for item in merged:
        if sum(1 for source in item.sources if source in OFFLINE_SOURCES) > 1:
            item.score = round(min(MAX_SCORE, item.score + AGREEMENT_BONUS), 3)
            item.reasons.append("GeoNames and Natural Earth agree on this place")
        item.tier = tier_for(item.score)
    return sorted(merged, key=lambda item: (-item.score, -item.pop))


def _group_by_origin(aliases: list[str], origin_of: dict[str, set[str]]) -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = {}
    for alias in aliases:
        for origin in origin_of.get(alias, ()):
            grouped.setdefault(origin, []).append(alias)
    return grouped


def _geonames_candidates(phrases, origin_of, ocr_conf, offset, when, countries, regions) -> list[Candidate]:
    hits = gazetteer.find_city_ids(phrases)
    cities = gazetteer.cities_by_id(hits)
    out: list[Candidate] = []
    for city_id, aliases in hits.items():
        city = cities.get(city_id)
        if city is None:
            continue
        grouped = _group_by_origin(aliases, origin_of)
        if not grouped:
            continue
        out.append(_match_candidate(
            name=city.name,
            lat=city.lat,
            lon=city.lon,
            cc=city.cc,
            region=regions[city.admin1].name if city.admin1 in regions else "",
            country=countries[city.cc].name if city.cc in countries else city.cc,
            pop=city.pop,
            tz=city.tz,
            grouped=grouped,
            ocr_conf=ocr_conf,
            offset=offset,
            when=when,
            dataset="GeoNames",
        ))
    return out


def _natural_earth_candidates(phrases, origin_of, ocr_conf, offset, when, countries) -> list[Candidate]:
    out: list[Candidate] = []
    for place, aliases in gazetteer.match_ne_places(phrases):
        grouped = _group_by_origin(aliases, origin_of)
        if not grouped:
            continue
        out.append(_match_candidate(
            name=place.name,
            lat=place.lat,
            lon=place.lon,
            cc=place.cc,
            region=place.region,
            country=countries[place.cc].name if place.cc in countries else place.cc,
            pop=place.pop,
            tz=place.tz,
            grouped=grouped,
            ocr_conf=ocr_conf,
            offset=offset,
            when=when,
            dataset="Natural Earth",
        ))
    return out


def _match_candidate(
    *,
    name: str,
    lat: float,
    lon: float,
    cc: str,
    region: str,
    country: str,
    pop: int,
    tz: str,
    grouped: dict[str, list[str]],
    ocr_conf: float,
    offset: Optional[float],
    when: Optional[datetime],
    dataset: str,
) -> Candidate:
    score = 0.0
    reasons: list[str] = []
    for origin, matched in grouped.items():
        words = max(len(alias.split()) for alias in matched)
        confidence = ocr_conf if origin == "ocr" else 1.0
        candidate_score = match_score(words, pop, SOURCE_WEIGHTS[origin], confidence, len(matched))
        label = "image text" if origin == "ocr" else "file name"
        reason = f'{label} matched "{matched[0]}"'
        if len(matched) > 1:
            reason += f" (+{len(matched) - 1} more spelling)"
        reasons.append(reason)
        score = max(score, candidate_score)

    if offset is not None and tz:
        zone = gazetteer.tz_offsets([tz], when).get(tz)
        if zone is not None and abs(zone - offset) <= 0.26:
            score += 0.06
            reasons.append(f"consistent with camera clock offset {offset_label(offset)}")
    if len(grouped) > 1:
        score += 0.08
        reasons.append("matched in both image text and file name")

    return Candidate(
        name=name,
        cc=cc,
        country=country,
        region=region,
        lat=lat,
        lon=lon,
        score=round(min(MAX_SCORE, score), 3),
        tier=tier_for(score),
        pop=pop,
        reasons=reasons,
        sources=[dataset],
    )


def _admin_candidates(
    phrases: set[str], origin_of, region_names, country_names, countries
) -> list[Candidate]:
    out: list[Candidate] = []
    for phrase in sorted(phrases):
        weight = max(SOURCE_WEIGHTS[origin] for origin in origin_of[phrase])
        region = region_names.get(phrase)
        if region is not None:
            score = round(min(MAX_SCORE, 0.45 * weight), 3)
            # The region is the name, so its country is what belongs next to it
            # in the output, not the GeoNames admin code.
            prefix = region.code.split(".")[0]
            home = countries.get(prefix)
            out.append(Candidate(
                name=region.name,
                cc=prefix,
                country=home.name if home else prefix,
                lat=region.lat,
                lon=region.lon,
                score=score,
                tier=tier_for(score),
                kind="region",
                reasons=[f'region name "{phrase}" appears in the image evidence'],
                sources=["GeoNames"],
            ))
        country = country_names.get(phrase)
        if country is not None:
            score = round(min(MAX_SCORE, 0.35 * weight), 3)
            out.append(Candidate(
                name=country.name,
                cc=country.cc,
                lat=country.lat,
                lon=country.lon,
                score=score,
                tier=tier_for(score),
                kind="country",
                reasons=[f'country name "{phrase}" appears in the image evidence (country level only)'],
                sources=["GeoNames"],
            ))
    return out


def _merge_candidates(items: list[Candidate], radius_km: float = MERGE_RADIUS_KM) -> list[Candidate]:
    """Fold the same place reported by several datasets into one candidate.

    Grouping by kind and folded name means only same-name candidates are
    distance-checked, instead of every candidate against every other one.
    """
    buckets: dict[tuple[str, str], list[Candidate]] = {}
    for item in items:
        key = (item.kind, gazetteer.normalize_alias(item.name))
        buckets.setdefault(key, []).append(item)

    merged: list[Candidate] = []
    for bucket in buckets.values():
        placed: list[Candidate] = []
        for item in bucket:
            for existing in placed:
                if _same_place(existing, item, radius_km):
                    _absorb(existing, item)
                    break
            else:
                placed.append(item)
        merged.extend(placed)
    return merged


def _same_place(first: Candidate, second: Candidate, radius_km: float) -> bool:
    """Whether two same-name candidates describe the same place."""
    # Natural Earth leaves ISO_A2 blank for some places. A missing country code
    # is not a contradiction there, so distance decides instead of rejecting.
    if first.cc and second.cc and first.cc != second.cc:
        return False
    if first.lat is None or second.lat is None or first.lon is None or second.lon is None:
        return False
    return gazetteer.haversine_km(first.lat, first.lon, second.lat, second.lon) <= radius_km


def _absorb(target: Candidate, other: Candidate) -> None:
    target.score = max(target.score, other.score)
    target.pop = max(target.pop, other.pop)
    if not target.cc:
        target.cc = other.cc
    if not target.country:
        target.country = other.country
    if not target.region:
        target.region = other.region
    for source in other.sources:
        if source not in target.sources:
            target.sources.append(source)
    for reason in other.reasons:
        if reason not in target.reasons:
            target.reasons.append(reason)


def _enrich_online(candidates: list[Candidate], notes: list[str], timeout: float = 6.0) -> None:
    """Confirm the top candidates against OpenStreetMap and Wikipedia.

    Only runs when the caller asked for online lookups; each confirmation adds
    its dataset to the candidate and raises its confidence.
    """
    looked_up = osm_hits = wiki_hits = 0
    for candidate in candidates:
        if looked_up >= ENRICH_LIMIT:
            break
        if candidate.kind != "city" or candidate.lat is None or candidate.lon is None:
            continue
        looked_up += 1

        match = geocode.search_place(candidate.name, candidate.cc, timeout=timeout)
        if match is not None:
            distance = gazetteer.haversine_km(candidate.lat, candidate.lon, match["lat"], match["lon"])
            if distance <= OSM_AGREE_KM:
                candidate.score += OSM_BONUS
                osm_hits += 1
                _add_source(candidate, "OpenStreetMap")
                candidate.reasons.append(f"OpenStreetMap lookup agrees within {distance:.0f} km")
            else:
                candidate.reasons.append(
                    f"OpenStreetMap has a {candidate.name} {distance:.0f} km away (a different place)"
                )

        article = _wiki_match(candidate.name, wikipedia.nearby(candidate.lat, candidate.lon, timeout=timeout))
        if article is not None:
            candidate.score += WIKI_BONUS
            wiki_hits += 1
            _add_source(candidate, "Wikipedia geosearch")
            metres = article.get("dist_m")
            detail = f" {metres / 1000:.1f} km away" if isinstance(metres, (int, float)) else " nearby"
            candidate.reasons.append(f'Wikipedia geosearch has "{article["title"]}"{detail}')

    notes.append(
        f"online confirmations: {osm_hits} from OpenStreetMap, {wiki_hits} from Wikipedia "
        f"for {looked_up} candidate(s)"
    )


def _wiki_match(name: str, articles: list[dict]) -> Optional[dict]:
    """First nearby article whose title contains every word of the place name."""
    words = set(gazetteer.normalize_alias(name).split())
    if not words:
        return None
    for article in articles:
        title_words = set(gazetteer.normalize_alias(article.get("title") or "").split())
        if words <= title_words:
            return article
    return None


def _add_source(candidate: Candidate, dataset: str) -> None:
    if dataset not in candidate.sources:
        candidate.sources.append(dataset)


def _offset_band(offset: float, when: Optional[datetime]) -> Optional[Candidate]:
    # Half an hour of slack keeps half-hour zones (India, Iran) but drops
    # neighbouring zones, which would widen the band without evidence.
    examples = gazetteer.cities_in_offset_band(offset, when, tolerance=0.51, limit=3)
    if not examples:
        return None
    center = gazetteer.region_lon_center(offset)
    span = f"about {center - 30:.0f}° to {center + 30:.0f}° longitude"
    names = ", ".join(f"{city.name} ({city.cc})" for city in examples)
    return Candidate(
        name=f"{offset_label(offset)} timezone band",
        region=f"{span}; largest cities there: {names}",
        score=0.10,
        tier="unlikely",
        kind="band",
        reasons=[f"camera clock wrote offset {offset_label(offset)}, which bounds longitude only"],
        sources=["EXIF clock offset"],
    )
