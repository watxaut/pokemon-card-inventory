#!/usr/bin/env python3
"""Download Tesseract's accurate Japanese model (about 14 MB) into ./tessdata.

Used to read card names, as a cross-check of the number (Japanese cards only). The app
works without it, but then skips the name check.

    python scripts/get_jpn_model.py
"""

from __future__ import annotations

import sys
import urllib.request
from pathlib import Path

URL = "https://raw.githubusercontent.com/tesseract-ocr/tessdata_best/main/jpn.traineddata"
OUT = Path(__file__).resolve().parent.parent / "tessdata" / "jpn.traineddata"


def main() -> int:
    if OUT.is_file() and OUT.stat().st_size > 1_000_000:
        print(f"Already downloaded: {OUT}")
        return 0
    OUT.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUT.with_suffix(".part")
    print(f"Downloading the Japanese model (about 14 MB) to {OUT} ...")
    with urllib.request.urlopen(URL, timeout=120) as resp, open(tmp, "wb") as fh:  # noqa: S310 - fixed https URL
        while chunk := resp.read(1 << 16):
            fh.write(chunk)
    if tmp.stat().st_size < 1_000_000:
        tmp.unlink()
        print("Download failed (file too small). Try again later.", file=sys.stderr)
        return 1
    tmp.replace(OUT)
    print("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
