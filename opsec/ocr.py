"""Optional OCR through the ``tesseract`` binary.

Photo text (shop fronts, street plates, station signs) is the strongest
location cue available without shipping a vision model, but tesseract is an
optional system package: when it is missing the caller is told so and the rest
of the analysis still runs.
"""

from __future__ import annotations

import shutil
import subprocess

MIN_CONF = 40.0


def binary() -> str | None:
    return shutil.which("tesseract")


def available() -> bool:
    return binary() is not None


def read_lines(path: str, lang: str = "eng", timeout: float = 120.0):
    """Return (lines, mean_confidence, note). ``lines`` is a list of word lists."""
    exe = binary()
    if exe is None:
        return [], 0.0, "tesseract not installed, image text ignored"
    command = [exe, path, "stdout", "-l", lang, "--psm", "3", "tsv"]
    try:
        result = subprocess.run(command, capture_output=True, timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return [], 0.0, f"OCR failed: {exc}"
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", "replace").strip().splitlines()
        return [], 0.0, f"OCR failed: {detail[-1] if detail else result.returncode}"

    grouped: dict[tuple[str, str, str, str], list[tuple[str, float]]] = {}
    for row in result.stdout.decode("utf-8", "replace").splitlines()[1:]:
        cells = row.split("\t")
        if len(cells) < 12 or cells[0] != "5":
            continue
        try:
            confidence = float(cells[10])
        except ValueError:
            continue
        text = cells[11].strip()
        if not text or confidence < MIN_CONF:
            continue
        grouped.setdefault((cells[1], cells[2], cells[3], cells[4]), []).append((text, confidence))

    lines = [words for _, words in sorted(grouped.items())]
    confidences = [conf for words in lines for _, conf in words]
    mean = sum(confidences) / len(confidences) / 100.0 if confidences else 0.0
    if not lines:
        return [], 0.0, "no legible text found in the image"
    return lines, mean, ""
