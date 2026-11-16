"""Command-line entry point for opsec."""

from __future__ import annotations

import argparse
import json
import os
import sys

from . import __version__, locate, render


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="opsec",
        description="Estimate where an image was taken, using its metadata and offline map data.",
        epilog=(
            "Tiers: EXACT is a GPS fix stored in the file. SEMI-LIKELY comes from place names "
            "read out of the image text or the file name, matched against GeoNames and Natural "
            "Earth and confirmed with OpenStreetMap and Wikipedia when --online is set. "
            "UNLIKELY is weak evidence only, such as a camera-clock offset band."
        ),
    )
    parser.add_argument("images", nargs="+", metavar="IMAGE", help="image file(s) to analyse")
    parser.add_argument(
        "--online",
        action="store_true",
        help="confirm via OpenStreetMap and Wikipedia (off by default: nothing leaves the machine)",
    )
    parser.add_argument("--no-ocr", action="store_true", help="skip image text recognition")
    parser.add_argument("--lang", default="eng", help="tesseract language pack (default: eng)")
    parser.add_argument("--top", type=int, default=5, help="candidates per tier (default: 5)")
    parser.add_argument("--json", action="store_true", help="machine-readable JSON output")
    parser.add_argument("--color", choices=("auto", "always", "never"), default="auto", help="colour mode")
    parser.add_argument("--width", type=int, default=0, help="output width in columns")
    parser.add_argument("--version", action="version", version=f"opsec {__version__}")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.top < 1:
        parser.error("--top must be at least 1")

    missing = [path for path in args.images if not os.path.isfile(path)]
    if missing:
        for path in missing:
            print(f"opsec: not a file: {path}", file=sys.stderr)
        return 2

    style = render.Style(render.resolve_mode(args.color))
    reports = [
        locate.analyse(
            path,
            online=args.online,
            use_ocr=not args.no_ocr,
            ocr_lang=args.lang,
            top=args.top,
        )
        for path in args.images
    ]

    if args.json:
        payload = reports[0].to_dict() if len(reports) == 1 else [item.to_dict() for item in reports]
        print(json.dumps(payload, indent=2))
        return 0

    width = args.width or None
    for index, report in enumerate(reports):
        if index:
            print()
        print(render.render(report, style, width=width))
    return 0


if __name__ == "__main__":
    sys.exit(main())
