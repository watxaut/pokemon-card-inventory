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


@pytest.mark.parametrize("text", ["HP 280", "©2026 Pokémon", "999/076", "12/3", "5/1000", "123456/103", ""])
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
def test_photo_without_a_findable_card_falls_back_to_reading_the_corner(monkeypatch):
    monkeypatch.setattr(reader.detect, "find_card_corners", lambda img: None)
    result = reader.read_card(drawn_card_photo("I  MEG EN  179/132"))
    assert result.card_image is None
    assert (result.identifiers[0].number, result.identifiers[0].total) == ("179", "132")


def test_missing_tesseract_raises(monkeypatch):
    monkeypatch.setattr(reader, "tesseract_available", lambda: False)
    with pytest.raises(reader.ReaderUnavailable):
        reader.read_card(drawn_card_photo("MEG EN 179/132"))


def straightened_card(identifier: str, upside_down: bool = False) -> bytes:
    """Just the card, as the browser sends it after detecting and straightening it."""
    try:
        font = ImageFont.truetype(FONT, 30)
    except OSError:
        pytest.skip("DejaVu font not available")
    card = Image.new("RGB", (945, 1320), (235, 225, 190))
    d = ImageDraw.Draw(card)
    d.rectangle((60, 140, 885, 640), fill=(90, 140, 200))
    d.text((50, 1235), identifier, font=font, fill=(20, 20, 20))
    if upside_down:
        card = card.rotate(180)
    out = io.BytesIO()
    card.save(out, format="JPEG", quality=90)
    return out.getvalue()


@needs_tesseract
@pytest.mark.parametrize("upside_down", [False, True])
def test_reads_straightened_card_in_either_orientation(upside_down):
    result = reader.read_card(straightened_card("J  M6  058/076 RR", upside_down), straightened=True)
    assert [(i.set_code, i.number, i.total) for i in result.identifiers][:1] == [("M6", "058", "076")]
    assert result.card_image is not None  # saved the right way up



@pytest.mark.parametrize("text, expected", [
    ("095/0837", ["095/083"]),               # 'AR' glued on as a digit
    ("1095/0837", ["095/083", "109/083"]),   # stray character in front: both splits are candidates
    ("1497/742", ["497/742", "149/742"]),    # the slash read twice
    ("911/103", ["011/103"]),                # italic 0 read as 9
    ("J M6 058/076 RR", ["058/076"]),        # the 6 of M6 stays separate
])
def test_parse_fraction_candidates(text, expected):
    assert reader._parse_fraction(text) == expected
