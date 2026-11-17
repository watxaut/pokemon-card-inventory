"""Identifier parsing (pure) and real Tesseract runs on drawn cards (skipped if Tesseract is missing)."""

import io

import pytest
from PIL import Image, ImageDraw, ImageFont

from app import reader
from app.reader import Identifier, parse_identifiers, plausible_code


def first(text):
    found = parse_identifiers(text)
    return (found[0].set_code, found[0].language, found[0].number, found[0].total, found[0].rarity) if found else None


@pytest.mark.parametrize("text, expected", [
    ("I MEG EN 179/132", ("MEG", "en", "179", "132", "")),          # English, Mega Evolution era
    ("J M6 058/076 RR", ("M6", "", "058", "076", "RR")),             # Japanese Storm Emeralda (M6)
    ("(J) M6 | 058/076 RR ©2026 Poké", ("M6", "", "058", "076", "RR")),
    ("G SV2a 025/165 C", ("SV2a", "", "025", "165", "C")),
    ("M1L 092/063 MUR", ("M1L", "", "092", "063", "MUR")),          # secret rare above the set size
    ("MEGEN 179/132", ("MEG", "en", "179", "132", "")),              # OCR glued code and language
    ("1MEG EN 179/132", ("MEG", "en", "179", "132", "")),
    ("M6 058 / 076", ("M6", "", "058", "076", "")),                  # spaces around the slash
    ("M6 O58/O76 RR", ("M6", "", "058", "076", "RR")),               # O read instead of 0
    ("M6 058/076RR", ("M6", "", "058", "076", "RR")),                # rarity glued on
    ("001/M-P", ("", "", "001", "M-P", "")),                         # Japanese promo
    ("J M6 JP 058/076", ("M6", "ja", "058", "076", "")),             # if a JP language code is printed
])
def test_parse_identifiers(text, expected):
    assert first(text) == expected


@pytest.mark.parametrize("text", ["HP 280", "©2026 Pokémon", "920/850", "999/076", "12/3", ""])
def test_ignores_things_that_are_not_identifiers(text):
    assert parse_identifiers(text) == []


def test_repeated_reads_are_merged_with_alternatives():
    found = parse_identifiers("GG 058/076\nM6 058/076 RR\nM6 058/076\n058/076")
    assert len(found) == 1
    assert (found[0].set_code, found[0].hits, found[0].rarity) == ("M6", 4, "RR")
    assert found[0].alternatives == ("GG",)


def test_plausible_codes():
    assert all(map(plausible_code, ["MEG", "M6", "SV2a", "M1L", "SV11B"]))
    assert not any(map(plausible_code, ["uy", "Gg", "", "ABCD", "m6"]))


def test_identifier_label():
    assert Identifier("MEG", "en", "179", "132", "").label == "MEG EN 179/132"
    assert Identifier("M6", "", "058", "076", "RR").label == "M6 058/076 RR"


def test_prepare_image_shrinks_and_applies_exif_rotation():
    img = Image.new("RGB", (4000, 3000), (10, 20, 30))
    exif = Image.Exif()
    exif[0x0112] = 6  # rotate 90° on display
    buf = io.BytesIO()
    img.save(buf, format="JPEG", exif=exif)
    out = Image.open(io.BytesIO(reader.prepare_image(buf.getvalue(), 1000)))
    assert out.format == "JPEG" and out.size == (750, 1000)


def test_prepare_image_rejects_garbage():
    with pytest.raises(reader.ImageError):
        reader.prepare_image(b"hello", 100)


# --------------------------------------------------------------- real OCR runs
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
needs_tesseract = pytest.mark.skipif(not reader.tesseract_available(), reason="Tesseract not installed")


def drawn_card_photo(identifier: str) -> bytes:
    """A plain card with an identifier in the bottom-left corner, on a darker table."""
    try:
        font = ImageFont.truetype(FONT, 30)
    except OSError:
        pytest.skip("DejaVu font not available")
    card = Image.new("RGB", (1260, 1760), (235, 225, 190))
    d = ImageDraw.Draw(card)
    d.rectangle((90, 190, 1170, 860), fill=(90, 140, 200))
    d.text((80, 1652), identifier, font=font, fill=(20, 20, 20))
    photo = Image.new("RGB", (1500, 2000), (80, 60, 45))
    photo.paste(card.resize((1130, 1580)), (185, 210))
    out = io.BytesIO()
    photo.save(out, format="JPEG", quality=90)
    return out.getvalue()


@needs_tesseract
@pytest.mark.parametrize("printed, expected", [
    ("I  MEG EN  179/132", ("MEG", "179", "132")),
    ("J  M6  058/076 RR", ("M6", "058", "076")),
])
def test_reads_identifier_from_photo(printed, expected):
    result = reader.read_card(drawn_card_photo(printed))
    assert [(i.set_code, i.number, i.total) for i in result.identifiers][:1] == [expected]


@needs_tesseract
def test_reads_identifier_without_opencv(monkeypatch):
    monkeypatch.setattr(reader, "cv2", None)
    result = reader.read_card(drawn_card_photo("I  MEG EN  179/132"))
    assert result.card_image is None
    assert (result.identifiers[0].number, result.identifiers[0].total) == ("179", "132")


def test_missing_tesseract_raises(monkeypatch):
    monkeypatch.setattr(reader, "tesseract_available", lambda: False)
    with pytest.raises(reader.ReaderUnavailable):
        reader.read_card(drawn_card_photo("MEG EN 179/132"))
