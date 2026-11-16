"""Rainbow terminal rendering with true-colour, 256-colour and plain fallbacks."""

from __future__ import annotations

import os
import shutil
import sys

from .locate import MAX_SCORE, Candidate, Report, map_url

RESET = "\x1b[0m"
DIM = "\x1b[2m"
BOLD = "\x1b[1m"

GLYPHS = {
    "O": (" ███ ", "█   █", "█   █", "█   █", " ███ "),
    "P": ("████ ", "█   █", "████ ", "█    ", "█    "),
    "S": (" ████", "█    ", " ███ ", "    █", "████ "),
    "E": ("█████", "█    ", "████ ", "█    ", "█████"),
    "C": (" ████", "█    ", "█    ", "█    ", " ████"),
}

ASCII_GLYPHS = {
    "O": (" ### ", "#   #", "#   #", "#   #", " ### "),
    "P": ("#### ", "#   #", "#### ", "#    ", "#    "),
    "S": (" ####", "#    ", " ### ", "    #", "#### "),
    "E": ("#####", "#    ", "#### ", "#    ", "#####"),
    "C": (" ####", "#    ", "#    ", "#    ", " ####"),
}

TIER_HUES = {"exact": (140.0, 190.0), "semi": (35.0, 60.0), "unlikely": (285.0, 320.0)}
LABEL_WIDTH = 11


def resolve_mode(requested: str) -> str:
    """Pick a colour depth. An explicit request overrides NO_COLOR and redirection."""
    if requested == "never":
        return "none"
    if requested == "auto" and (os.environ.get("NO_COLOR") or not sys.stdout.isatty()):
        return "none"
    # 256 colours is the safe default: 24-bit escapes are wrong on most terminals
    # unless COLORTERM advertises them.
    terminal = os.environ.get("COLORTERM", "").lower()
    if "truecolor" in terminal or "24bit" in terminal:
        return "truecolor"
    return "256"


def _hsv(hue: float) -> tuple[int, int, int]:
    saturation, value = 0.85, 1.0
    sector = (hue % 360.0) / 60.0
    chroma = value * saturation
    second = chroma * (1 - abs(sector % 2 - 1))
    offset = value - chroma
    red, green, blue = [
        (chroma, second, 0), (second, chroma, 0), (0, chroma, second),
        (0, second, chroma), (second, 0, chroma), (chroma, 0, second),
    ][int(sector) % 6]
    return (
        int(round((red + offset) * 255)),
        int(round((green + offset) * 255)),
        int(round((blue + offset) * 255)),
    )


def _to_256(red: int, green: int, blue: int) -> int:
    levels = (0, 95, 135, 175, 215, 255)

    def nearest(value: int) -> int:
        return min(range(6), key=lambda index: abs(levels[index] - value))

    return 16 + 36 * nearest(red) + 6 * nearest(green) + nearest(blue)


def _glyphs() -> tuple[str, str, str]:
    encoding = (sys.stdout.encoding or "utf-8").lower()
    if "utf" in encoding:
        return "█", "░", "▸"
    return "#", "-", ">"


class Style:
    def __init__(self, mode: str = "auto"):
        self.mode = mode
        self.block, self.shade, self.bullet = _glyphs()

    @property
    def enabled(self) -> bool:
        return self.mode != "none"

    def _foreground(self, red: int, green: int, blue: int) -> str:
        if self.mode == "truecolor":
            return f"\x1b[38;2;{red};{green};{blue}m"
        if self.mode == "256":
            return f"\x1b[38;5;{_to_256(red, green, blue)}m"
        return ""

    def paint(self, text: str, hue: float) -> str:
        if not self.enabled:
            return text
        red, green, blue = _hsv(hue)
        return f"{self._foreground(red, green, blue)}{text}{RESET}"

    def rainbow(self, text: str, start: float = 0.0, end: float = 200.0) -> str:
        if not self.enabled:
            return text
        steps = max(1, len(text) - 1)
        parts = []
        for index, char in enumerate(text):
            hue = start + (end - start) * index / steps
            parts.append(self.paint(char, hue))
        return "".join(parts) + RESET

    def dim(self, text: str) -> str:
        return f"{DIM}{text}{RESET}" if self.enabled else text

    def strong(self, text: str) -> str:
        return f"{BOLD}{text}{RESET}" if self.enabled else text

    def bar(self, score: float, width: int = 10) -> str:
        filled = max(0, min(width, int(round(score * width))))
        hue = 130.0 * min(1.0, score / MAX_SCORE)
        if not self.enabled:
            return self.block * filled + self.shade * (width - filled)
        blocks = "".join(
            self.paint(self.block, hue * (0.75 + 0.25 * index / max(1, filled - 1)) if filled > 1 else hue)
            for index in range(filled)
        )
        return blocks + self.dim(self.shade * (width - filled))


def render(report: Report, style: Style, width: int | None = None) -> str:
    columns = width or min(shutil.get_terminal_size((88, 24)).columns, 96)
    lines: list[str] = []

    banner = GLYPHS if style.block == "█" else ASCII_GLYPHS
    for row_index in range(5):
        row = "  ".join(banner[letter][row_index] for letter in "OPSEC")
        base = row_index * 18.0
        lines.append("  " + style.rainbow(row, base, base + 150))
    lines.append(
        "  "
        + style.paint("image → location", 300.0)
        + style.dim("  ·  offline-first  ·  GeoNames + Natural Earth")
    )
    lines.append("")

    lines.append("  " + style.strong(_shorten(report.path, columns - 4)) + style.dim(f"  ({_size(report.size)})"))
    lines.extend(_exif_lines(report, style))
    lines.append("")

    if report.exact is not None:
        lines.extend(_exact_block(report.exact, style, columns))
    else:
        lines.extend(_tier_block(
            "EXACT", report.exact, style, columns,
            "no GPS coordinates in the file", TIER_HUES["exact"],
        ))
        lines.extend(_tier_block(
            "SEMI-LIKELY", report.semi, style, columns,
            "no usable place names found", TIER_HUES["semi"],
        ))
        lines.extend(_tier_block(
            "UNLIKELY", report.unlikely, style, columns,
            "nothing to rank", TIER_HUES["unlikely"],
        ))

    if report.notes:
        lines.append("  " + style.rainbow(" NOTES ", 200.0, 260.0))
        for note in report.notes:
            lines.append("   " + style.dim(f"— {note}"))

    lines.append("")
    lines.append(
        "  "
        + style.dim("map data: GeoNames (CC BY 4.0) · Natural Earth (public domain)")
    )
    lines.append(
        "  "
        + style.dim("online, when enabled: OpenStreetMap Nominatim (ODbL) · Wikipedia (CC BY-SA)")
    )
    if report.exif.get("gps") or report.exif.get("make"):
        lines.append(
            "  "
            + style.paint("privacy:", 0.0)
            + style.dim(" this file still carries metadata — strip it before sharing")
        )
    return "\n".join(lines)


def _exif_lines(report: Report, style: Style) -> list[str]:
    info = report.exif
    details: list[str] = []
    camera = " ".join(part for part in (info.get("make"), info.get("model")) if part)
    if camera:
        details.append(camera)
    if info.get("datetime_original"):
        details.append(info["datetime_original"] + (f' {info["offset_time_original"]}' if info.get("offset_time_original") else ""))
    if info.get("lens"):
        details.append(info["lens"])
    if not details:
        return []
    return ["  " + style.dim("  ".join(details))]


def _exact_block(candidate: Candidate, style: Style, columns: int) -> list[str]:
    lines = [_section("EXACT", style, columns, *TIER_HUES["exact"])]
    rows: list[tuple[str, str]] = [("coordinates", f"{candidate.lat:.6f}, {candidate.lon:.6f}")]
    if candidate.place:
        rows.append(("place", candidate.place))
    if candidate.distance_km is not None:
        rows.append(("nearest", f"{candidate.name} ({candidate.distance_km} km)"))
    url = map_url(candidate.lat, candidate.lon)
    if url:
        rows.append(("map", url))
    for label, value in rows:
        indent = 2 + LABEL_WIDTH + 1
        text = _wrap(value, columns - indent, indent)
        lines.append(" " * 2 + style.paint(label.ljust(LABEL_WIDTH), 150.0) + " " + text)
    for reason in candidate.reasons:
        lines.append("     " + style.dim(f"— {reason}"))
    lines.append("")
    return lines


def _tier_block(
    title: str,
    items: list[Candidate],
    style: Style,
    columns: int,
    empty_reason: str,
    hues: tuple[float, float],
) -> list[str]:
    lines = [_section(title, style, columns, *hues)]
    if not items:
        lines.append("     " + style.dim(f"— none: {empty_reason}"))
        lines.append("")
        return lines
    for index, item in enumerate(items, start=1):
        label = f"{index:>2}. {item.name}"
        where = ", ".join(part for part in (item.region, item.country) if part)
        lines.append("  " + style.paint(label, hues[0]))
        if where:
            lines.append("     " + style.dim(_wrap(where, columns - 5, 5)))
        meta = f"     {style.bar(item.score)}  {item.score:.2f}"
        if item.lat is not None and item.lon is not None:
            meta += style.dim(f"   {item.lat:.4f}, {item.lon:.4f}")
        if item.pop:
            meta += style.dim(f"   pop {_population(item.pop)}")
        lines.append(meta)
        if len(item.sources) > 1:
            lines.append("     " + style.dim("sources: " + " · ".join(item.sources)))
        for reason in item.reasons[:4]:
            lines.append("     " + style.dim(f"{style.bullet} {reason}"))
    lines.append("")
    return lines


def _section(title: str, style: Style, columns: int, start: float, end: float) -> str:
    label = f" {title} "
    fill = "─" * max(0, columns - len(label) - 3)
    return "  " + style.rainbow(label, start, end) + style.dim(fill)


def _population(count: int) -> str:
    if count >= 1_000_000:
        return f"{count / 1_000_000:.1f}M"
    if count >= 1_000:
        return f"{count // 1_000}k"
    return str(count)


def _size(count: int) -> str:
    if count >= 1024 * 1024:
        return f"{count / (1024 * 1024):.1f} MB"
    if count >= 1024:
        return f"{count / 1024:.0f} kB"
    return f"{count} B"


def _shorten(text: str, limit: int) -> str:
    if limit <= 3 or len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _wrap(text: str, limit: int, indent: int = 0) -> str:
    if len(text) <= limit:
        return text
    lines: list[str] = []
    current = ""
    for word in text.split(" "):
        candidate = f"{current} {word}".strip()
        if len(candidate) > limit and current:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return ("\n" + " " * indent).join(lines)
