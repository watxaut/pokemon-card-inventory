import io
from dataclasses import replace

import pytest
from PIL import Image
from starlette.testclient import TestClient

from app.config import settings
from app.db import Database
from app.main import create_app
from app.reader import Identifier, ReadResult


def make_jpeg(width=1200, height=1600, color=(200, 40, 40)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buf, format="JPEG")
    return buf.getvalue()


def ident(set_code="M6", number="058", total="076", language="", rarity="RR", alternatives=()) -> Identifier:
    return Identifier(set_code, language, number, total, rarity, alternatives=tuple(alternatives))


class FakeCatalog:
    """Stands in for TCGdex. Knows Storm Emeralda (M6) 058 and Mega Evolution (MEG) 179."""

    CARDS = {
        ("M6", "58"): dict(tcgdex_id="M6-058", language="ja", name_jp="メガレックウザex", name_en="Mega Rayquaza ex",
                           set_id="M6", set_code="M6", set_name="ストームエメラルダ", set_total="76", number="058",
                           rarity="Double rare", category="Pokemon"),
        ("MEG", "179"): dict(tcgdex_id="me01-179", language="en", name_jp="", name_en="Mega Gardevoir ex",
                             set_id="me01", set_code="MEG", set_name="Mega Evolution", set_total="132",
                             number="179", rarity="Special illustration rare", category="Pokemon"),
    }

    def __init__(self):
        self.price = {
            "price_eur": 1.5, "price_trend": 1.5, "price_avg": 1.2, "price_low": 0.5, "price_avg30": 1.4,
            "price_source": "Cardmarket (via TCGdex)", "price_note": "", "price_url": None, "price_updated_at": None,
        }
        self.lookups = []

    def lookup(self, *, set_code="", number="", total="", language="", finish="normal", check_total=True,
               by_size=False):
        self.lookups.append((set_code, number, total, check_total, by_size))
        card = self.CARDS.get((set_code.upper(), number.lstrip("0")))
        if not card and by_size and total.isdigit():  # identify the set from its printed size
            card = next((c for (code, n), c in self.CARDS.items()
                         if n == number.lstrip("0") and int(c["set_total"]) == int(total)), None)
        if not card or (check_total and total.isdigit() and int(total) != int(card["set_total"])):
            return None
        return {**card, "image_url": f"https://assets.tcgdex.net/x/{card['tcgdex_id']}/high.webp",
                "price": dict(self.price)}

    def price_for(self, tcgdex_id, finish="normal", language="ja"):
        return dict(self.price) if tcgdex_id in {c["tcgdex_id"] for c in self.CARDS.values()} else None


@pytest.fixture
def cfg(tmp_path):
    return replace(settings, data_dir=tmp_path, app_password="")


@pytest.fixture
def fake_catalog():
    return FakeCatalog()


@pytest.fixture
def identifiers():
    """Mutable list of identifiers the fake OCR will 'read' from the next photo."""
    return [ident()]


@pytest.fixture
def reads():
    """The `straightened` flag of every read, in order."""
    return []


@pytest.fixture
def client(cfg, fake_catalog, identifiers, reads):
    def fake_read(image_bytes, straightened=False):
        assert image_bytes[:2] == b"\xff\xd8"  # the uploaded JPEG is passed through
        reads.append(straightened)
        return ReadResult(identifiers=list(identifiers), card_image=None, text="")

    app = create_app(cfg, db=Database(cfg.db_path), catalog=fake_catalog, read=fake_read)
    with TestClient(app) as c:
        yield c
