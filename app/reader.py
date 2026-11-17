"""Read a card's identifier from a photo with Tesseract OCR (no AI, runs on your device).

Modern Pokémon cards print the set code, collector number and set size in the
bottom-left corner:

    English, Mega Evolution era ........ I  MEG EN 179/132
    Japanese, Mega era (e.g. M6) ........ J  M6 058/076 RR      (M6 = Storm Emeralda)
    Japanese, Scarlet & Violet era ...... G  SV2a 025/165 C
    Japanese promos ..................... 001/M-P

The leading letter is the regulation mark, EN/JP the language, the trailing letters
the rarity. Set code + number identify the exact card in the catalogue.

If OpenCV is installed, the card is first found in the photo and straightened, which
makes reading much more reliable. Without it, the bottom-left area of the photo is read
directly (works best when the card fills the frame).
"""

from __future__ import annotations

import io
import logging
import re
import shutil
from collections import Counter
from dataclasses import dataclass, replace
from functools import lru_cache

from PIL import Image, ImageFilter, ImageOps

from .config import settings

try:  # optional: card detection + perspective correction
    import cv2
    import numpy as np
except ImportError:  # pragma: no cover - depends on the install
    cv2 = None
    np = None

try:
    import pytesseract
except ImportError:  # pragma: no cover - depends on the install
    pytesseract = None

log = logging.getLogger(__name__)

LANGUAGES = {"EN": "en", "JP": "ja", "JA": "ja", "FR": "fr", "DE": "de", "IT": "it",
             "ES": "es", "PT": "pt", "KO": "ko", "KR": "ko"}
RARITIES = {"C", "U", "R", "RR", "RRR", "AR", "SR", "SAR", "UR", "HR", "MUR", "MA", "ACE",
            "S", "SSR", "CHR", "CSR", "BWR", "PR", "TR"}
REGULATION_MARKS = set("DEFGHIJKL")

# Letters Tesseract commonly confuses with digits (only applied to the number fields).
_DIGITISH = "0-9OoDQIli|!LZzSsBGbT"
_DIGIT_FIX = str.maketrans({"O": "0", "o": "0", "D": "0", "Q": "0", "I": "1", "l": "1", "i": "1",
                            "|": "1", "!": "1", "L": "1", "Z": "2", "z": "2", "S": "5", "s": "5",
                            "B": "8", "G": "6", "b": "6", "T": "7"})
_FRACTION = re.compile(
    rf"^(?P<pre>.*?)(?P<num>[{_DIGITISH}]{{1,3}})/"
    rf"(?P<tot>[A-Z]{{1,3}}-P|[{_DIGITISH}]{{2,3}})(?P<post>[A-Za-z]{{0,3}})$"
)
_CODE = re.compile(r"[A-Za-z][A-Za-z0-9]{1,5}(?:-[A-Za-z0-9]{1,3})?")

# Regions to read, as (left, top, right, bottom) fractions of the image.
_CARD_REGIONS = [(0.0, 0.86, 0.64, 1.0)]                # straightened card: identifier strip
_PHOTO_REGIONS = [(0.0, 0.5, 0.78, 1.0), (0.0, 0.0, 1.0, 1.0)]  # unknown card position
_CARD_SIZE = (1890, 2640)                                # 63 x 88 mm at 30 px/mm


@dataclass(frozen=True)
class Identifier:
    set_code: str   # 'MEG', 'M6', 'SV2a'; '' if it could not be read
    language: str   # 'en', 'ja', ... or '' when not printed
    number: str     # collector number as printed, e.g. '058'
    total: str      # set size ('076') or promo code ('M-P')
    rarity: str     # printed rarity code ('RR') or ''
    hits: int = 1   # how many OCR passes agreed
    alternatives: tuple[str, ...] = ()  # other readings of the set code, best first

    @property
    def label(self) -> str:
        parts = [self.set_code, {"en": "EN", "ja": "JP"}.get(self.language, self.language.upper()),
                 f"{self.number}/{self.total}", self.rarity]
        return " ".join(p for p in parts if p)


@dataclass
class ReadResult:
    identifiers: list[Identifier]
    card_image: bytes | None  # straightened card (JPEG) when the card was found in the photo
    text: str                 # raw OCR text, for troubleshooting


class ReaderUnavailable(RuntimeError):
    """Tesseract is not installed."""


class ImageError(ValueError):
    """The upload is not an image Pillow can read."""


def _open(raw: bytes) -> Image.Image:
    try:
        return ImageOps.exif_transpose(Image.open(io.BytesIO(raw))).convert("RGB")
    except Exception as exc:  # noqa: BLE001 - Pillow raises many types for bad input
        raise ImageError("That file is not an image I can read.") from exc


def prepare_image(raw: bytes, max_side: int) -> bytes:
    """Rotate per EXIF, shrink so the long side is at most `max_side`, re-encode as JPEG."""
    img = _open(raw)
    img.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=88, optimize=True)
    return out.getvalue()


# ------------------------------------------------------------------ availability
@lru_cache(maxsize=1)
def tesseract_available() -> bool:
    if pytesseract is None:
        return False
    if settings.tesseract_cmd:
        pytesseract.pytesseract.tesseract_cmd = settings.tesseract_cmd
    elif not shutil.which("tesseract"):
        return False
    try:
        pytesseract.get_tesseract_version()
        return True
    except Exception:  # noqa: BLE001 - TesseractNotFoundError, OSError, ...
        return False


def opencv_available() -> bool:
    return cv2 is not None


# ----------------------------------------------------------------------- parsing
def parse_identifiers(text: str) -> list[Identifier]:
    """Find card identifiers like 'MEG EN 179/132' or 'J M6 058/076 RR' in OCR text."""
    found: list[Identifier] = []
    for raw_line in text.splitlines():
        line = re.sub(r"\s*[/／⁄]\s*", "/", raw_line).strip()
        tokens = line.split()
        for i, token in enumerate(tokens):
            m = _FRACTION.match(token.strip(".,:;()[]{}'\""))
            if not m:
                continue
            number = m["num"].translate(_DIGIT_FIX)
            total = m["tot"] if m["tot"].endswith("-P") else m["tot"].translate(_DIGIT_FIX)  # '076' or promo 'M-P'
            if not number.isdigit() or not (total.isdigit() or total.endswith("-P")):
                continue
            if total.isdigit() and not (10 <= int(total) <= 300 and int(number) <= 2.2 * int(total)):
                continue  # not a plausible set size / collector number
            before = tokens[max(0, i - 3):i] + ([m["pre"]] if m["pre"] else [])
            code, language = _code_and_language(before)
            rarity = m["post"].upper() if m["post"].upper() in RARITIES else ""
            if not rarity and i + 1 < len(tokens) and tokens[i + 1].upper() in RARITIES:
                rarity = tokens[i + 1].upper()
            found.append(Identifier(code, language, number, total, rarity))
    return _merge(found)


def _code_and_language(before: list[str]) -> tuple[str, str]:
    code, language = "", ""
    for raw in reversed(before):
        tok = raw.strip(".,:;()[]{}'\"|")
        if not _CODE.fullmatch(tok):  # noise glued in front: '1MEG' -> 'MEG'
            tok = re.sub(r"^[^A-Za-z]+", "", tok)
        upper = tok.upper()
        if not code and not language and upper in LANGUAGES:
            language = LANGUAGES[upper]
            continue
        if not code and _CODE.fullmatch(tok):
            # Language glued to the code by OCR: 'MEGEN' -> 'MEG' + 'EN'.
            if not language and len(tok) >= 4 and upper[-2:] in ("EN", "JP") and tok[-3].isalpha():
                tok, language = tok[:-2], LANGUAGES[upper[-2:]]
            code = tok
            continue
        if code or (len(tok) == 1 and upper in REGULATION_MARKS):
            break
    return code, language


def plausible_code(code: str) -> bool:
    """Japanese codes mix letters and digits (M6, SV2a, M1L); English ones are 3 capitals (MEG)."""
    return bool(re.fullmatch(r"[A-Z]{3}", code) or
                (re.fullmatch(r"[A-Za-z][A-Za-z0-9]{1,4}", code) and re.search(r"\d", code) and code[0].isupper()))


def _merge(found: list[Identifier]) -> list[Identifier]:
    """Combine repeated reads of the same identifier; best (plausible set code, most agreed) first."""
    groups: dict[tuple, list[Identifier]] = {}
    for ident in found:
        groups.setdefault((int(ident.number), ident.total.lstrip("0")), []).append(ident)
    merged = []
    for reads in groups.values():
        codes = Counter(r.set_code for r in reads if r.set_code)
        ranked = sorted(codes, key=lambda c: (plausible_code(c), codes[c]), reverse=True)
        first = reads[0]
        merged.append(Identifier(
            set_code=ranked[0] if ranked else "",
            language=next((r.language for r in reads if r.language), ""),
            number=first.number,
            total=first.total,
            rarity=next((r.rarity for r in reads if r.rarity), ""),
            hits=len(reads),
            alternatives=tuple(ranked[1:5]),
        ))
    return sorted(merged, key=lambda x: (plausible_code(x.set_code), x.hits), reverse=True)


# ---------------------------------------------------------------------- reading
def read_card(image_bytes: bytes) -> ReadResult:
    """OCR the identifier(s) in a photo. Raises ReaderUnavailable if Tesseract is missing."""
    if not tesseract_available():
        raise ReaderUnavailable(
            "Text recognition needs Tesseract. Install it (see README: 'Install Tesseract') and restart."
        )
    img = _open(image_bytes)
    texts: list[str] = []
    card_img = None

    card = find_card(img)
    if card is not None:
        for variant in (card, card.rotate(180)):
            _read_regions(variant, _CARD_REGIONS, texts)
            if parse_identifiers("\n".join(texts)):
                card_img = variant
                break
    if card_img is not None and not _complete(texts):
        _read_regions(img, _PHOTO_REGIONS[:1], texts)  # a second look for the set code
    if not parse_identifiers("\n".join(texts)):
        # Card not found or unreadable: read where its bottom-left corner usually is, then the
        # whole photo, then the photo upside down. Stop at the first region that gives a number.
        for photo, regions in ((img, _PHOTO_REGIONS), (img.rotate(180), _PHOTO_REGIONS[:1])):
            for region in regions:
                _read_regions(photo, [region], texts)
                if parse_identifiers("\n".join(texts)):
                    break
            if parse_identifiers("\n".join(texts)):
                break

    text = "\n".join(texts)
    card_jpeg = None
    if card_img is not None:
        out = io.BytesIO()
        card_img.resize((630, 880), Image.Resampling.LANCZOS).save(out, format="JPEG", quality=88)
        card_jpeg = out.getvalue()
    return ReadResult(parse_identifiers(text), card_jpeg, text)


def _complete(texts: list[str]) -> bool:
    """True once an identifier with a plausible set code has been read."""
    found = parse_identifiers("\n".join(texts))
    return bool(found and plausible_code(found[0].set_code))


def _read_regions(img: Image.Image, regions: list, texts: list[str]) -> bool:
    """OCR each region (as a text block first, then as scattered text) until a full identifier is read."""
    for region in regions:
        crop = _crop(img, region)
        for psm, light in ((6, False), (11, False), (6, True)):
            texts.append(_ocr(crop, psm, light))
            if _complete(texts):
                return True
    return False


def _crop(img: Image.Image, region: tuple[float, float, float, float]) -> Image.Image:
    w, h = img.size
    left, top, right, bottom = region
    return img.crop((int(left * w), int(top * h), int(right * w), int(bottom * h)))


def _light_background(gray: Image.Image) -> Image.Image:
    """Turn white-on-dark text (the set-code box on Japanese cards) into dark-on-light text."""
    radius = max(6, min(gray.size) // 25)
    dark_area = gray.filter(ImageFilter.BoxBlur(radius)).point(lambda v: 255 if v < 120 else 0)
    return Image.composite(ImageOps.invert(gray), gray, dark_area)


def _ocr(img: Image.Image, psm: int = 6, light: bool = False) -> str:
    gray = ImageOps.autocontrast(ImageOps.grayscale(img), cutoff=1)
    # Tesseract reads best when capital letters are ~30-40 px tall.
    longest = max(gray.size)
    if longest < 1400:
        factor = 1400 / longest
        gray = gray.resize((int(gray.width * factor), int(gray.height * factor)), Image.Resampling.LANCZOS)
    elif longest > 2600:
        gray.thumbnail((2600, 2600), Image.Resampling.LANCZOS)
    try:
        # psm 6: one block of text (keeps 'J M6 058/076 RR' on one line); psm 11: scattered text.
        return pytesseract.image_to_string(_light_background(gray) if light else gray,
                                           config=f"--oem 1 --psm {psm}")
    except pytesseract.TesseractError as exc:  # pragma: no cover - corrupt input
        log.warning("Tesseract failed: %s", exc)
        return ""


# ------------------------------------------------------------- card detection
def find_card(img: Image.Image) -> Image.Image | None:
    """Find the card in the photo and return it straightened (portrait), or None."""
    if cv2 is None:
        return None
    arr = np.asarray(img)
    h, w = arr.shape[:2]
    scale = min(1.0, 1000 / max(h, w))
    small = cv2.resize(arr, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
    gray = cv2.GaussianBlur(cv2.cvtColor(small, cv2.COLOR_RGB2GRAY), (5, 5), 0)
    edges = cv2.dilate(cv2.Canny(gray, 40, 120), np.ones((5, 5), np.uint8), iterations=2)
    contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    min_area = 0.12 * small.shape[0] * small.shape[1]
    for contour in sorted(contours, key=cv2.contourArea, reverse=True)[:5]:
        if cv2.contourArea(contour) < min_area:
            break
        rect = cv2.minAreaRect(contour)
        side_a, side_b = rect[1]
        if not side_a or not side_b or not 0.6 < min(side_a, side_b) / max(side_a, side_b) < 0.82:
            continue  # a card is 63 x 88 mm (ratio 0.72)
        approx = cv2.approxPolyDP(contour, 0.02 * cv2.arcLength(contour, True), True)
        corners = approx.reshape(-1, 2) if len(approx) == 4 else cv2.boxPoints(rect)
        return _warp(arr, np.asarray(corners, dtype="float32") / scale)
    return None


def _warp(arr, corners) -> Image.Image:
    s, d = corners.sum(axis=1), np.diff(corners, axis=1).ravel()
    tl, br, tr, bl = corners[np.argmin(s)], corners[np.argmax(s)], corners[np.argmin(d)], corners[np.argmax(d)]
    width = max(np.linalg.norm(tr - tl), np.linalg.norm(br - bl))
    height = max(np.linalg.norm(bl - tl), np.linalg.norm(br - tr))
    out_w, out_h = _CARD_SIZE if height >= width else _CARD_SIZE[::-1]
    dst = np.array([[0, 0], [out_w - 1, 0], [out_w - 1, out_h - 1], [0, out_h - 1]], dtype="float32")
    matrix = cv2.getPerspectiveTransform(np.array([tl, tr, br, bl], dtype="float32"), dst)
    warped = Image.fromarray(cv2.warpPerspective(arr, matrix, (out_w, out_h)))
    return warped if out_h >= out_w else warped.rotate(90, expand=True)
