"""Read a card's identifier from a photo with Tesseract OCR (no AI, runs on your device).

Modern Pokémon cards print the set code, collector number and set size in the
bottom-left corner:

    English, Mega Evolution era ........ I  MEG EN 179/132
    Japanese, Mega era (e.g. M6) ........ J  M6 058/076 RR      (M6 = Storm Emeralda)
    Japanese, Scarlet & Violet era ...... G  SV2a 025/165 C
    Japanese promos ..................... 001/M-P

The leading letter is the regulation mark, EN/JP the language, the trailing letters
the rarity. Set code + number identify the exact card in the catalogue.

The card is usually found and straightened by the browser before upload. Otherwise it is
found here (detect.py) and straightened; failing that, the bottom-left area of the photo is
read directly.
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

import numpy as np

from . import detect
from .config import settings

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
# A straightened card includes a margin of CARD_PAD (of its size) on every side, so an outline
# that is slightly off still contains the identifier. Regions are fractions of that padded image.
CARD_PAD = 0.06
_CARD_REGIONS = [(0.0, 0.79, 0.72, 1.0)]                 # identifier strip, bottom left
_PHOTO_REGIONS = [(0.0, 0.5, 0.78, 1.0), (0.0, 0.0, 1.0, 1.0)]  # unknown card position
_CARD_SIZE = (1890, 2640)                                # 63 x 88 mm at 30 px/mm (plus margin)


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
    """Kept for the status API: card detection no longer needs OpenCV."""
    return True


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
            if total.isdigit() and not (10 <= int(total) <= 999 and int(number) <= 2.2 * int(total)):
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
    """Japanese codes mix letters and digits (M6, SV2a, M1L) or are 2 capitals (MC); English ones
    are 3 capitals (MEG)."""
    return bool(re.fullmatch(r"[A-Z]{2,3}", code) or
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
def read_card(image_bytes: bytes, straightened: bool = False) -> ReadResult:
    """OCR the identifier in a photo. Raises ReaderUnavailable if Tesseract is missing.

    straightened=True means the image is already just the card (with margin), cut out and
    straightened by the browser, so card detection is skipped.
    """
    if not tesseract_available():
        raise ReaderUnavailable(
            "Text recognition needs Tesseract. Install it (see README: 'Install Tesseract') and restart."
        )
    img = _open(image_bytes)
    texts: list[str] = []
    card_img = None
    found: list[Identifier] = []

    if straightened:
        card = img if img.height >= img.width else img.rotate(90, expand=True)
        card = card.resize(_CARD_SIZE, Image.Resampling.LANCZOS)
    else:
        card = find_card(img)
    if card is not None:
        for variant in (card, card.rotate(180)):
            found = read_strip(_crop(variant, _CARD_REGIONS[0]), texts)
            if found:
                card_img = variant
                break
    if not found:
        # Card not found or unreadable: read where its bottom-left corner usually is, then the
        # whole photo, then the photo upside down. Stop at the first region that gives a number.
        for photo, regions in ((img, _PHOTO_REGIONS), (img.rotate(180), _PHOTO_REGIONS[:1])):
            for region in regions:
                _read_regions(photo, [region], texts)
                if parse_identifiers("\n".join(texts)):
                    break
            if parse_identifiers("\n".join(texts)):
                break
        found = parse_identifiers("\n".join(texts))

    card_jpeg = None
    if card_img is not None:
        out = io.BytesIO()
        card_img.resize((630, 880), Image.Resampling.LANCZOS).save(out, format="JPEG", quality=88)
        card_jpeg = out.getvalue()
    return ReadResult(found, card_jpeg, "\n".join(texts))


# ------------------------------------------------------- focused strip reading
_DIGITISH_SET = set("0123456789/oOlIiSsBZzQDTgG|!")
_FRACTION_ONLY = re.compile(r"(?<!\d)(\d{1,3})/(\d{2,3}(?!\d)|[A-Z]{1,3}-P)")  # not inside longer digit runs
_NUMBER_WHITELIST = "-c tessedit_char_whitelist=0123456789/"
_CODE_WHITELIST = "-c tessedit_char_whitelist=ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-"


def read_strip(strip: Image.Image, texts: list[str]) -> list[Identifier]:
    """Read the identifier in the bottom-left strip of a straightened card.

    Votes for the number ('050/103') are pooled until one reading clearly leads:
    1. Tesseract's word boxes locate the number, even when misread; each such box is re-read
       with digits only over several crops/sizes/polarities.
    2. The same on a copy with local contrast evened out (strips with dark areas).
    3. The line where the identifier sits relative to the copyright or "Illus." text.
    Then the set code is read just left of the best box and the rarity just right of it.
    Falls back to parsing the strip's plain OCR text.
    """
    gray = ImageOps.autocontrast(ImageOps.grayscale(strip), cutoff=1)
    words = _words(gray)
    plain = " ".join(w[0] for w in words)
    texts.append(plain)
    pooled: Counter = Counter()
    best_box, best_votes, all_words = None, 0, list(words)

    def strong() -> bool:
        top = pooled.most_common(2)
        return bool(top) and top[0][1] >= 3 and (len(top) == 1 or top[0][1] - top[1][1] >= 2)

    for stage in ("plain", "flat"):
        if stage == "flat":
            flat_words = _words(_flatten(ImageOps.grayscale(strip), 60))
            texts.append(" ".join(w[0] for w in flat_words))
            all_words += flat_words
            stage_words = flat_words
        else:
            stage_words = words
        for box in _number_boxes(stage_words):
            _, x, y, w, h = box
            fractions = _vote(strip, (x, y, x + w, y + h), _NUMBER_WHITELIST, _parse_fraction)
            pooled.update(dict(fractions))
            if fractions and fractions[0][1] > best_votes:
                best_box, best_votes = (box, stage_words), fractions[0][1]
            if strong():
                break
        if strong():
            break
    if not strong():
        for region, text_h in _anchor_regions(strip, all_words):
            pooled.update(dict(_vote_block(strip, region, text_h)))
    if not pooled:
        return parse_identifiers(plain)

    fractions = sorted(pooled.items(), key=lambda kv: (_full_width(kv[0]), kv[1], len(kv[0])), reverse=True)
    codes, language, rarities = _code_and_rarity(strip, *best_box) if best_box else ([], "", [])
    texts.append(f"{' '.join(codes)} {' '.join(f for f, _ in fractions)} {' '.join(rarities)}")
    return [Identifier(codes[0] if codes else "", language, *fraction.split("/"),
                       rarities[0].upper() if rarities else "", hits=votes, alternatives=tuple(codes[1:5]))
            for fraction, votes in fractions[:6]]


def _words(gray: Image.Image) -> list[tuple[str, int, int, int, int]]:
    data = pytesseract.image_to_data(gray, config="--oem 1 --psm 11", output_type=pytesseract.Output.DICT)
    return [(data["text"][i].strip(), data["left"][i], data["top"][i], data["width"][i], data["height"][i])
            for i in range(len(data["text"])) if data["text"][i].strip()]


def _flatten(gray: Image.Image, radius: int) -> Image.Image:
    """Even out lighting: each pixel relative to its blurred surroundings."""
    a = np.asarray(gray, dtype=np.float32)
    b = np.asarray(gray.filter(ImageFilter.GaussianBlur(radius)), dtype=np.float32) + 1
    return ImageOps.autocontrast(Image.fromarray(np.clip(a / b * 180, 0, 255).astype(np.uint8)), cutoff=1)


def _joined(words, rounds=2):
    """Neighbouring words on the same line joined, two or three at a time (OCR may split
    '011/103' into 'on,' + '103', with boxes that touch or overlap)."""
    out, current = [], list(words)
    for _ in range(rounds):
        new = []
        for t1, x1, y1, w1, h1 in current:
            for t2, x2, y2, w2, h2 in words:
                h = max(h1, h2)
                same_line = abs((y1 + h1 / 2) - (y2 + h2 / 2)) < 0.5 * h
                if same_line and -0.3 * h <= x2 - (x1 + w1) < 0.6 * h and x2 > x1:
                    top, bottom = min(y1, y2), max(y1 + h1, y2 + h2)
                    new.append((t1 + t2, x1, top, max(x1 + w1, x2 + w2) - x1, bottom - top))
        out += new
        current = new
    return out


def _number_boxes(words, limit=5):
    """Number-like words (or neighbours joined), lowest (nearest the card's bottom) and leftmost first."""
    boxes = [w for w in words + _joined(words)
             if len(w[0]) >= 4 and sum(c in _DIGITISH_SET for c in w[0]) >= 0.6 * len(w[0])]
    boxes.sort(key=lambda w: (-(w[2] + w[4]) // 20, w[1]))
    return boxes[:limit]


def _code_and_rarity(strip: Image.Image, box, words):
    """Set codes (best first), language and rarities around the number's box."""
    _, x, y, w, h = box
    # Set code and language: first the words read on the same line, left of the number ...
    line = sorted((t, wx) for t, wx, wy, ww, wh in words
                  if wx + ww <= x + h // 3 and abs((wy + wh / 2) - (y + h / 2)) < 0.6 * h)
    code, language = _code_and_language([t for t, _ in line][-3:])
    codes = [code] if code and plausible_code(code) else []
    # ... then the area there read again on its own (and inside a dark box, inverted).
    left = (max(0, x - 7 * h), y, max(1, x - h // 6), y + h)
    dark = _dark_box(strip, (max(0, x - 5 * h), max(0, y - h // 4), x, min(strip.height, y + h + h // 4)), h)
    for c in (_read_dark_box(strip, dark) if dark else []) + _vote_all(strip, left, _CODE_WHITELIST, _parse_codes):
        if c.upper() in ("EN", "JP"):
            language = language or LANGUAGES[c.upper()]
        elif c not in codes:
            codes.append(c)
    if code and code not in codes:
        codes.append(code)
    right = (x + w + h // 6, y, min(strip.width, x + w + 4 * h), y + h)
    rarities = [r for r in _vote_all(strip, right, _CODE_WHITELIST, _parse_codes) if r.upper() in RARITIES]
    return codes, language, rarities


_COPYRIGHT = ("©20", "@20", "pokémon/", "pokemon/", "nintendo", "creatures", "game")


def _anchor_regions(strip: Image.Image, words) -> list[tuple[tuple[int, int, int, int], int]]:
    """Where the identifier line should be: just above-left of the copyright line, or just
    below the "Illus." line."""
    regions = []
    for t, x, y, w, h in words:
        low = t.lower()
        if any(low.startswith(k) or k in low for k in _COPYRIGHT) and y > strip.height * 0.3:
            regions.append(((max(0, x - int(0.45 * strip.width)), max(0, y - int(3.6 * h)),
                             max(1, x - int(0.03 * strip.width)), min(strip.height, y + h // 4)), h))
            break
    for t, x, y, w, h in words:
        if t.lower().lstrip("|!").startswith(("illus", "ilus", "llus")):
            regions.append(((max(0, x - 2 * h), y + int(0.8 * h), min(strip.width, x + 14 * h),
                             min(strip.height, y + int(4.6 * h))), h))
            break
    return [(r, h) for r, h in regions if r[2] - r[0] > 40 and r[3] - r[1] > 20]


def _vote_block(strip: Image.Image, region, text_h: int) -> list[tuple[str, int]]:
    """Fraction readings of a region read as a block of text (psm 6), several sizes/polarities.

    text_h: height of the anchor text's box, to scale the identifier's digits to 30-50 px.
    """
    crop = ImageOps.grayscale(strip.crop(region))
    counts: Counter = Counter()
    for target in (40, 52, 30):
        f = target / max(8, text_h)
        size = (max(1, round(crop.width * f)), max(1, round(crop.height * f)))
        g = ImageOps.autocontrast(crop.resize(size, Image.Resampling.LANCZOS), cutoff=2)
        a = np.asarray(g)
        b = Image.fromarray(((a > _otsu(a)) * 255).astype(np.uint8))
        for v in (g, b, ImageOps.invert(g)):
            for value in _parse_fraction(_tess(ImageOps.expand(v, border=20, fill=255), f"--oem 1 --psm 6 {_NUMBER_WHITELIST}")):
                counts[value] += 1
    return sorted(counts.items(), key=lambda kv: (_full_width(kv[0]), kv[1], len(kv[0])), reverse=True)


def _dark_box(strip: Image.Image, area, h: int):
    """The filled dark box nearest the number (Japanese set codes are printed white in one)."""
    crop = ImageOps.grayscale(strip.crop(area))
    a = np.asarray(crop, dtype=float)
    if a.size == 0:
        return None
    dark = a < min(_otsu(a), np.percentile(a, 40))
    cols = dark.mean(axis=0) > 0.4
    runs, start = [], None
    for i, c in enumerate(list(cols) + [False]):
        if c and start is None:
            start = i
        elif not c and start is not None:
            if runs and start - runs[-1][1] <= 0.35 * h:  # the box's white letters split it: rejoin
                runs[-1] = (runs[-1][0], i)
            else:
                runs.append((start, i))
            start = None
    runs = [r for r in runs if r[1] - r[0] >= 0.7 * h]
    if not runs:
        return None
    c0, c1 = runs[-1]  # nearest the number
    rows = np.nonzero(dark[:, c0:c1].mean(axis=1) > 0.35)[0]
    if len(rows) < 0.4 * h:
        return None
    x0, y0 = area[0] + c0, area[1] + rows[0]
    return (x0, y0, area[0] + c1, area[1] + rows[-1] + 1)


def _read_dark_box(strip: Image.Image, box) -> list[str]:
    """Read the light text inside a dark box (inverted, so Tesseract sees dark on light)."""
    x0, y0, x1, y1 = box
    inset = max(1, (y1 - y0) // 8)
    inner = strip.crop((x0 + inset, y0 + inset, x1 - inset, y1 - inset))
    counts: Counter = Counter()
    for height in (64, 96):
        g = ImageOps.grayscale(inner)
        f = height / max(1, g.height)
        g = ImageOps.autocontrast(g.resize((max(1, round(g.width * f)), height), Image.Resampling.LANCZOS), cutoff=2)
        a = np.asarray(g)
        b = Image.fromarray(((a < _otsu(a)) * 255).astype(np.uint8))  # light text -> black on white
        for v in (b, ImageOps.invert(g)):
            v = ImageOps.expand(v, border=24, fill=255)
            for psm in (7,):
                for tok in _tess(v, f"--oem 1 --psm {psm} {_CODE_WHITELIST}").split():
                    if _CODE.fullmatch(tok) and len(tok) <= 5:
                        counts[tok] += 1
    return sorted(counts, key=lambda c: (plausible_code(c), counts[c]), reverse=True)


def _parse_fraction(text: str) -> list[str]:
    m = _FRACTION_ONLY.search(text.replace(" ", ""))
    if not m:
        return []
    number, total = m.groups()
    if not total.isdigit():
        return [m.group(0)]  # promo, e.g. 001/M-P
    if not 10 <= int(total) <= 999:
        return []
    if int(number) > 2.2 * int(total) and len(number) == len(total) and number[0] in "9867":
        # Numbers are zero-padded (011/103); the italic 0 is often read as 9, 8, 6 or 7.
        number = "0" + number[1:]
    if int(number) > 2.2 * int(total):
        return []
    return [f"{number}/{total}"]


def _parse_codes(text: str) -> list[str]:
    out = []
    for tok in text.split():
        tok = tok.strip("-")
        if len(tok) == 1:
            continue  # regulation mark
        if len(tok) >= 3 and tok[0].upper() in REGULATION_MARKS and plausible_code(tok[1:]) and not plausible_code(tok):
            tok = tok[1:]  # regulation mark glued on: 'JM6a' -> 'M6a'
        if _CODE.fullmatch(tok):
            out.append(tok)
    return out


def _variants(img: Image.Image):
    """The same crop prepared different ways, most promising first: text heights of 48-96 px,
    binarised or grey, dark-on-light first (inverted first if the text is the light part)."""
    prepared = {}
    for height in (64, 96, 48):
        g = ImageOps.grayscale(img)
        f = height / max(1, g.height)
        g = ImageOps.autocontrast(g.resize((max(1, round(g.width * f)), height), Image.Resampling.LANCZOS), cutoff=2)
        a = np.asarray(g)
        b = Image.fromarray(((a > _otsu(a)) * 255).astype(np.uint8))
        prepared[height] = (b, g)
    light_text = (np.asarray(prepared[64][0]) < 128).mean() > 0.5  # mostly dark: the text is light
    flip = ImageOps.invert if light_text else (lambda im: im)
    keep = (lambda im: im) if light_text else ImageOps.invert
    order = [(64, 0, flip), (96, 0, flip), (48, 1, flip), (96, 1, flip), (64, 1, flip), (48, 0, flip),
             (64, 0, keep), (96, 1, keep)]
    for height, which, polarity in order:
        yield ImageOps.expand(polarity(prepared[height][which]), border=24, fill=255)


def _crops(img: Image.Image, box):
    """The box with a few different margins (left, right, top/bottom) in units of its height."""
    x0, y0, x1, y1 = box
    h = y1 - y0
    for ml, mr, my in ((0.4, 0.9, 0.3), (0.2, 0.6, 0.1), (0.6, 1.3, 0.45)):
        yield img.crop((max(0, x0 - int(ml * h)), max(0, y0 - int(my * h)),
                        min(img.width, x1 + int(mr * h)), min(img.height, y1 + int(my * h))))


def _vote(img, box, whitelist, parse, per_crop=6):
    """Readings of a box over several crops and preparations, as (value, votes), most votes first.

    OCR mistakes can repeat (an italic 3 read as 8 several times), so every reading is kept
    and the catalogue decides between them.
    """
    counts: Counter = Counter()

    def settled() -> bool:  # three agreeing reads and nothing close: stop early
        top = counts.most_common(2)
        return bool(top) and top[0][1] >= 3 and (len(top) == 1 or top[0][1] - top[1][1] >= 2)

    for crop in _crops(img, box):
        for k, v in enumerate(_variants(crop)):
            if k >= per_crop or settled():
                break
            for value in parse(_tess(v, f"--oem 1 --psm 7 {whitelist}")):
                counts[value] += 1
        if settled():
            break
    top = counts.most_common(2)
    if len(top) == 2 and top[0][1] - top[1][1] <= 1:
        # Too close to call (e.g. 011 vs 041 in an italic font): more sizes, and the italic
        # slant straightened out.
        for crop in _crops(img, box):
            for v in _tiebreak_variants(crop):
                for value in parse(_tess(v, f"--oem 1 --psm 7 {whitelist}")):
                    counts[value] += 1
    return sorted(counts.items(), key=lambda kv: (_full_width(kv[0]), kv[1], len(kv[0])), reverse=True)


def _tiebreak_variants(img: Image.Image):
    light_text = None
    for height, shear in ((80, 0.0), (112, 0.0), (72, 0.2), (96, 0.2)):
        g = ImageOps.grayscale(img)
        f = height / max(1, g.height)
        g = ImageOps.autocontrast(g.resize((max(1, round(g.width * f)), height), Image.Resampling.LANCZOS), cutoff=2)
        a = np.asarray(g)
        b = Image.fromarray(((a > _otsu(a)) * 255).astype(np.uint8))
        if light_text is None:
            light_text = (np.asarray(b) < 128).mean() > 0.5
        if light_text:
            b = ImageOps.invert(b)
        if shear:  # lean the text back upright (cards print the number in italics)
            b = b.transform((b.width + int(shear * b.height), b.height), Image.Transform.AFFINE,
                            (1, shear, -shear * b.height, 0, 1, 0), Image.Resampling.BICUBIC, fillcolor=255)
        yield ImageOps.expand(b, border=24, fill=255)


def _full_width(fraction: str) -> bool:
    """Modern cards zero-pad the number to the set size's width (058/076, 149/742). A shorter
    number usually means OCR dropped a digit at the edge ('49/742')."""
    number, _, total = fraction.partition("/")
    return not total.isdigit() or len(number) >= len(total)


def _vote_all(img, box, whitelist, parse, budget=6):
    """All parsed readings of a box, most frequent first (plausible set codes before others)."""
    if box[2] - box[0] < 8 or box[3] - box[1] < 8:
        return []
    counts: Counter = Counter()
    tries = 0
    for crop in _crops(img, box):
        for v in _variants(crop):
            tries += 1
            for value in parse(_tess(v, f"--oem 1 --psm 7 {whitelist}")):
                counts[value] += 1
            if tries >= budget or (counts and counts.most_common(1)[0][1] >= 3):
                break
        if tries >= budget or (counts and counts.most_common(1)[0][1] >= 3):
            break
    return sorted(counts, key=lambda c: (plausible_code(c), counts[c]), reverse=True)


def _tess(img: Image.Image, config: str) -> str:
    if img.width < 30 or img.height < 30:
        return ""
    try:
        return pytesseract.image_to_string(img, config=config)
    except pytesseract.TesseractError as exc:  # pragma: no cover - odd crops
        log.debug("Tesseract failed on a crop: %s", exc)
        return ""


def _otsu(a: np.ndarray) -> int:
    hist = np.bincount(a.astype(np.uint8).ravel(), minlength=256).astype(float)
    p = hist / max(1.0, hist.sum())
    omega, mu = np.cumsum(p), np.cumsum(p * np.arange(256))
    with np.errstate(divide="ignore", invalid="ignore"):
        between = (mu[-1] * omega - mu) ** 2 / (omega * (1 - omega))
    return int(np.nanargmax(between)) if np.isfinite(between).any() else 128


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
    """Find the card in the photo and return it straightened (portrait, with margin), or None."""
    corners = detect.find_card_corners(img)
    return warp_card(img, corners) if corners else None


def warp_card(img: Image.Image, corners, size=_CARD_SIZE, pad=CARD_PAD) -> Image.Image:
    """Cut out the card (corners: top-left, top-right, bottom-right, bottom-left) straightened,
    with a margin of `pad` of the card's size around it."""
    out_w, out_h = size
    # Output pixel -> card coordinates (0..1 inside the card, a little beyond in the margin).
    u = lambda x: x / (out_w - 1) * (1 + 2 * pad) - pad  # noqa: E731
    v = lambda y: y / (out_h - 1) * (1 + 2 * pad) - pad  # noqa: E731
    H = homography([(0, 0), (1, 0), (1, 1), (0, 1)], corners)
    src = [_project(H, u(x), v(y)) for x, y in ((0, 0), (out_w - 1, 0), (out_w - 1, out_h - 1), (0, out_h - 1))]
    coeffs = homography([(0, 0), (out_w - 1, 0), (out_w - 1, out_h - 1), (0, out_h - 1)], src)
    return img.transform((out_w, out_h), Image.Transform.PERSPECTIVE, tuple(coeffs[:8]),
                         Image.Resampling.BICUBIC)


def homography(src, dst) -> list[float]:
    """3x3 matrix (flattened, last = 1) mapping 4 points src onto 4 points dst."""
    rows, rhs = [], []
    for (x, y), (u, v) in zip(src, dst):
        rows.append([x, y, 1, 0, 0, 0, -u * x, -u * y]); rhs.append(u)
        rows.append([0, 0, 0, x, y, 1, -v * x, -v * y]); rhs.append(v)
    h = np.linalg.solve(np.array(rows, dtype=float), np.array(rhs, dtype=float))
    return [*h.tolist(), 1.0]


def _project(H, x, y):
    d = H[6] * x + H[7] * y + H[8]
    return ((H[0] * x + H[1] * y + H[2]) / d, (H[3] * x + H[4] * y + H[5]) / d)
