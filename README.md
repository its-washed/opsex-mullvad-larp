# opsec

Terminal tool that estimates where an image was taken from its file path, using
the metadata inside the file plus offline map data. It prints three tiers so you
can see how strong the evidence actually is:

| tier | evidence it needs |
| --- | --- |
| **EXACT** | GPS coordinates stored in the image EXIF, resolved to the nearest mapped city, or to a street address with `--online` |
| **SEMI-LIKELY** | place, region or country names read from the image text or the file name, matched against two independent place datasets and optionally confirmed online |
| **UNLIKELY** | weak cues only, e.g. the camera-clock offset, which bounds longitude but not location |

No dependencies, no build step, no model weights: standard-library Python plus
five small tab-separated tables. A lookup streams the tables instead of building
in-memory indexes, so a run peaks at about 24 MB of RSS (measured with Python
3.12) and needs nothing installed beyond the interpreter.

## Run

```sh
bin/opsec photo.jpg          # or: python3 -m opsec photo.jpg
```

```
bin/opsec IMG_4021.HEIC
bin/opsec ~/Pictures/rome_2024.jpg --json
bin/opsec geotagged.jpg --online      # confirm via OpenStreetMap and Wikipedia
bin/opsec a.jpg b.jpg --top 3 --color always
```

Options: `--online` (confirm an exact fix through OpenStreetMap Nominatim and
candidates through OpenStreetMap search and Wikipedia geosearch; off by default
so nothing leaves the machine), `--no-ocr`, `--lang deu` (tesseract language
pack), `--top N`, `--json`, `--color auto|always|never`, `--width N`.

Colour uses 24-bit escapes only when `COLORTERM` advertises them, otherwise the
256-colour palette; output is plain when piped to a file or `NO_COLOR` is set,
and `--color always|never` overrides both.

## What each tier really means

- **GPS** is the only exact answer. If the camera wrote a fix, nothing else is
  consulted. A `0,0` fix is treated as unset, not as a location.
- **Image text** needs `tesseract` on `PATH`. It is optional: without it the
  tool says so and still uses the file name and metadata. Text recognition is
  the strongest non-GPS cue for photos of streets, shops and stations.
- **File names** are weaker than image text by design, and generic words
  (`central`, `station`, `park`, ...) are ignored entirely.
- **Cross-source agreement raises confidence.** A name matched in both GeoNames
  and Natural Earth scores +0.10; an OpenStreetMap search hit within 25 km adds
  +0.12; a Wikipedia geosearch article adds +0.08 (the last two only with
  `--online`). When a dataset places the same name far away, the candidate gets
  a note instead of a bonus, and nothing is silently averaged away.
- **Camera-clock offset** only narrows longitude to a wide band; it is reported
  at 0.10 confidence under UNLIKELY.
- Confidence numbers are a heuristic over name specificity, place population,
  evidence source, OCR confidence and cross-source agreement. They rank
  candidates; they are not calibrated probabilities, and there is no
  image-content (vision) model.
- Nothing is inferred from colours, architecture or scenery. An image with no
  GPS, no readable text and an unhelpful file name produces "no location
  evidence" rather than a guess.

## Map and location data

Four sources, two offline and two opt-in online:

| source | used for | licence |
| --- | --- | --- |
| [GeoNames](https://www.geonames.org/) `cities15000`, `countryInfo`, `admin1CodesASCII` | city, region and country names; nearest place for a fix | CC BY 4.0 |
| [Natural Earth](https://www.naturalearthdata.com/) populated places | independent name match and coordinate cross-check | public domain |
| [OpenStreetMap Nominatim](https://nominatim.openstreetmap.org/) | street address for a fix, name search to confirm a candidate (`--online`) | ODbL |
| [Wikipedia geosearch](https://www.mediawiki.org/wiki/Extension:GeoData) | geotagged article near a candidate (`--online`) | CC BY-SA |

`data/` holds the two offline tables (about 6.5 MB). Region and country
reference points are population-weighted centroids computed from the city table.
Online sources are contacted only with `--online`; Nominatim is rate-limited to
one request per second and at most three candidates are confirmed per image.

To rebuild the tables:

```sh
mkdir -p /tmp/mapdata && cd /tmp/mapdata
curl -O https://download.geonames.org/export/dump/cities15000.zip
curl -O https://download.geonames.org/export/dump/countryInfo.txt
curl -O https://download.geonames.org/export/dump/admin1CodesASCII.txt
unzip -o cities15000.zip
curl -LO https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/ne_10m_populated_places.geojson
cd - && python3 tools/build_gazetteer.py --geonames /tmp/mapdata \
    --natural-earth /tmp/mapdata/ne_10m_populated_places.geojson
```

`--natural-earth` is optional; without it `places_ne.tsv` is left untouched and
the tool simply uses the remaining tables.

## Tests

```sh
python3 -m unittest discover -s tests -t . -v
```

The EXIF and OCR tests build their own fixtures (a byte-level JPEG/TIFF/HEIC
writer and a stub `tesseract` on `PATH`), so they need no image files or system
OCR package.
