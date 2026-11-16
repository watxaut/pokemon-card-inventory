import io
from dataclasses import replace

import pytest
from PIL import Image
from starlette.testclient import TestClient

from app.config import settings
from app.db import Database
from app.main import create_app
from app.recognizer import Recognition, RecognizedCard


def make_jpeg(width=1200, height=1600, color=(200, 40, 40)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buf, format="JPEG")
    return buf.getvalue()


def read_card(**overrides) -> RecognizedCard:
    base = dict(
        name_jp="ピカチュウ", name_en="Pikachu", set_code="SV2a", number="025", set_total="165",
        rarity="C", category="pokemon", finish="normal", hp="60", confidence="high", notes="",
    )
    base.update(overrides)
    return RecognizedCard(**base)


class FakeCatalog:
    """Stands in for TCGdex. Knows one card: SV2a 025 Pikachu."""

    def __init__(self):
        self.price = {
            "price_eur": 1.5, "price_trend": 1.5, "price_avg": 1.2, "price_low": 0.5, "price_avg30": 1.4,
            "price_source": "Cardmarket (via TCGdex)", "price_note": "", "price_url": None, "price_updated_at": None,
        }
        self.lookups = []

    def lookup(self, *, name_jp="", set_code="", number="", finish="normal"):
        self.lookups.append((name_jp, set_code, number, finish))
        if set_code.lower() == "sv2a" and number.lstrip("0") == "25":
            return {
                "tcgdex_id": "SV2a-025", "name": "ピカチュウ", "set_id": "SV2a", "set_name": "ポケモンカード151",
                "set_total": "165", "number": "025", "rarity": "コモン", "category": "ポケモン",
                "image_url": "https://assets.tcgdex.net/ja/SV/SV2a/025/high.webp", "price": dict(self.price),
            }
        return None

    def price_for(self, tcgdex_id, finish="normal"):
        return dict(self.price) if tcgdex_id == "SV2a-025" else None


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    return replace(settings, data_dir=tmp_path, app_password="", ai_price_search=True, auto_ai_price=False)


@pytest.fixture
def fake_catalog():
    return FakeCatalog()


@pytest.fixture
def recognized():
    """Mutable list of cards the fake recognizer will 'see' in the next photo."""
    return [read_card()]


@pytest.fixture
def ai_calls():
    return []


@pytest.fixture
def client(cfg, fake_catalog, recognized, ai_calls):
    def fake_recognize(image_bytes):
        assert image_bytes[:2] == b"\xff\xd8"  # a JPEG was produced
        return Recognition(cards=list(recognized), image_problem="")

    def fake_ai_price(card):
        ai_calls.append(card["id"])
        return {
            "price_eur": 42.0, "price_trend": 42.0, "price_avg": None, "price_low": 35.0, "price_avg30": 40.0,
            "price_source": "Cardmarket (AI web search)", "price_note": "AI lookup, high confidence",
            "price_url": "https://www.cardmarket.com/en/Pokemon/Products/Singles/x", "price_updated_at": None,
        }

    app = create_app(cfg, db=Database(cfg.db_path), catalog=fake_catalog,
                     recognize=fake_recognize, ai_price=fake_ai_price)
    with TestClient(app) as c:
        yield c
