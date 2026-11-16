"""OCR parsing and integration tests using a stub tesseract binary on PATH."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from opsec import locate, ocr

TSV = (
    "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n"
    "5\t1\t1\t1\t1\t1\t10\t10\t50\t20\t93.5\tGARE\n"
    "5\t1\t1\t1\t1\t2\t70\t10\t50\t20\t91.0\tDE\n"
    "5\t1\t1\t1\t1\t3\t130\t10\t50\t20\t88.0\tLILLE\n"
    "5\t1\t1\t1\t2\t1\t10\t40\t50\t20\t12.0\tnoise\n"
)

STUB = "#!/bin/sh\ncat <<'TSV_END'\n" + TSV + "TSV_END\n"


class StubTesseract(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        root = Path(self.folder.name)
        binary_dir = root / "bin"
        binary_dir.mkdir()
        binary = binary_dir / "tesseract"
        binary.write_text(STUB)
        binary.chmod(0o755)
        self.previous_path = os.environ.get("PATH", "")
        os.environ["PATH"] = f"{binary_dir}{os.pathsep}{self.previous_path}"
        self.image = root / "IMG_0001.jpg"
        self.image.write_bytes(b"\xff\xd8\xff\xd9")

    def tearDown(self):
        os.environ["PATH"] = self.previous_path
        self.folder.cleanup()

    def test_reads_words_and_drops_low_confidence_noise(self):
        lines, confidence, note = ocr.read_lines(str(self.image))
        self.assertEqual(note, "")
        self.assertEqual(len(lines), 1)
        self.assertEqual([word for word, _ in lines[0]], ["GARE", "DE", "LILLE"])
        self.assertGreater(confidence, 0.8)

    def test_image_text_reaches_semi_likely_tier(self):
        report = locate.analyse(str(self.image), online=False)
        semi = {(item.name, item.cc) for item in report.semi}
        self.assertIn(("Lille", "FR"), semi)


if __name__ == "__main__":
    unittest.main()
