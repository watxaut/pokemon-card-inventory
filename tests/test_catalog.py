import httpx2 as httpx
import pytest

from app.catalog import Catalog, CatalogUnavailable, Tcgdex, extract_cardmarket_price
from app.db import normalize_number

RAYQUAZA = {
    "id": "M6-058", "localId": "058", "name": "メガレックウザex", "category": "Pokemon", "rarity": "Double rare",
    "dexId": [384], "image": "https://assets.tcgdex.net/ja/M/M6/058",
    "set": {"id": "M6", "name": "ストームエメラルダ", "cardCount": {"official": 76, "total": 113}},
    "pricing": {"cardmarket": {
        "updated": "2026-11-15T00:00:00Z", "unit": "EUR", "avg": 11.0, "low": 8.0, "trend": 12.5,
        "avg1": 10.0, "avg7": 12.0, "avg30": 13.0, "avg-holo": 40.0, "low-holo": 25.0, "trend-holo": 45.0,
        "avg30-holo": 42.0,
    }},
}
GARDEVOIR = {
    "id": "me01-179", "localId": "179", "name": "Mega Gardevoir ex", "category": "Pokemon",
    "image": "https://assets.tcgdex.net/en/me/me01/179",
    "set": {"id": "me01", "name": "Mega Evolution", "cardCount": {"official": 132, "total": 188}},
}

ROUTES = {
    "/v2/ja/sets": [{"id": "SV2a", "cardCount": {"official": 165}}, {"id": "M5", "cardCount": {"official": 81}},
                    {"id": "M6", "cardCount": {"official": 76}}],
    "/v2/ja/sets/M6": {"id": "M6", "cardCount": {"official": 76},
                       "cards": [{"id": "M6-001", "localId": "001"}, {"id": "M6-058", "localId": "058"}]},
    "/v2/ja/cards/M6-058": RAYQUAZA,
    "/v2/en/sets": [{"id": "sv01", "cardCount": {"official": 198}}, {"id": "sv08", "cardCount": {"official": 191}},
                    {"id": "me01", "cardCount": {"official": 132}}, {"id": "me02", "cardCount": {"official": 94}}],
    "/v2/en/sets/me01": {"id": "me01", "abbreviation": {"official": "MEG"}, "cardCount": {"official": 132},
                         "cards": [{"id": "me01-179", "localId": "179"}]},
    "/v2/en/sets/me02": {"id": "me02", "abbreviation": {"official": "PFL"}, "cards": []},
    "/v2/en/cards/me01-179": GARDEVOIR,
}


def http_client(routes=ROUTES, calls=None):
    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(request.url.path)
        if request.url.path in routes:
            return httpx.Response(200, json=routes[request.url.path])
        return httpx.Response(404, json={"error": "not found"})

    return httpx.Client(transport=httpx.MockTransport(handler))


def catalog(calls=None) -> Catalog:
    return Catalog(base_url="https://api.test/v2", http=http_client(calls=calls), languages=("ja", "en"))


@pytest.mark.parametrize("raw, expected", [
    ("025/165", "25"), ("025", "25"), ("25", "25"), ("100", "100"), ("0", "0"),
    ("001/SV-P", "1"), (" 007 ", "7"), ("", ""), (None, ""),
])
def test_normalize_number(raw, expected):
    assert normalize_number(raw) == expected


def test_japanese_card_by_printed_code_with_english_name_and_price():
    calls = []
    cat = catalog(calls)
    match = cat.lookup(set_code="M6", number="058", total="076")
    assert match["tcgdex_id"] == "M6-058" and match["language"] == "ja" and match["set_code"] == "M6"
    assert (match["name_jp"], match["name_en"]) == ("メガレックウザex", "Mega Rayquaza ex")
    assert match["set_name"] == "ストームエメラルダ" and match["set_total"] == "76"
    assert match["image_url"] == "https://assets.tcgdex.net/ja/M/M6/058/high.webp"
    assert match["price"]["price_eur"] == 12.5
    cat.lookup(set_code="m6", number="58")  # case and zero padding don't matter; set data is cached
    assert calls.count("/v2/ja/sets") == 1 and calls.count("/v2/ja/sets/M6") == 1


def test_regulation_mark_glued_to_code_is_ignored():
    assert catalog().lookup(set_code="JM6", number="058", total="076")["tcgdex_id"] == "M6-058"


def test_english_card_by_abbreviation_and_set_size():
    match = catalog().lookup(set_code="MEG", number="179", total="132", language="en")
    assert match["tcgdex_id"] == "me01-179" and match["set_code"] == "MEG" and match["name_en"] == "Mega Gardevoir ex"


def test_english_language_guessed_from_letters_only_code():
    assert catalog().lookup(set_code="MEG", number="179", total="132")["language"] == "en"


def test_english_typed_without_set_size_checks_recent_sets():
    assert catalog().lookup(set_code="MEG", number="179", language="en")["tcgdex_id"] == "me01-179"


def test_one_ocr_mistake_in_code_is_tolerated():
    assert catalog().lookup(set_code="MFG", number="179", total="132")["tcgdex_id"] == "me01-179"
    assert catalog().lookup(set_code="MG", number="058", total="076")["tcgdex_id"] == "M6-058"  # 6 read as G


def test_wrong_set_size_rejects_code_unless_told_otherwise():
    assert catalog().lookup(set_code="M6", number="058", total="081") is None
    assert catalog().lookup(set_code="M6", number="058", total="081", check_total=False)["tcgdex_id"] == "M6-058"


def test_unknown_card_or_number():
    assert catalog().lookup(set_code="M6", number="999", total="076") is None
    assert catalog().lookup(set_code="ZZZ", number="001", total="050") is None
    assert catalog().lookup(set_code="M6", number="") is None


def test_unreachable_catalog_raises():
    def boom(request):
        raise httpx.ConnectError("no network", request=request)

    cat = Catalog(base_url="https://api.test/v2", http=httpx.Client(transport=httpx.MockTransport(boom)))
    with pytest.raises(CatalogUnavailable):
        cat.lookup(set_code="M6", number="058")


def test_price_for_uses_the_cards_language():
    cat = catalog()
    assert cat.price_for("M6-058", "normal", "ja")["price_eur"] == 12.5
    assert cat.price_for("me01-179", "normal", "en") is None  # no pricing block


def test_abbreviation_formats():
    tcg = Tcgdex(base_url="https://api.test/v2", language="en", http=http_client(routes={
        "/v2/en/sets/a": {"abbreviation": {"official": "AAA"}},
        "/v2/en/sets/b": {"abbreviations": "BBB"},
        "/v2/en/sets/c": {"tcgOnline": "CCC"},
    }))
    assert [tcg.abbreviation(x) for x in "abcd"] == ["AAA", "BBB", "CCC", ""]


def test_extract_price_prefers_trend_and_handles_holo():
    normal = extract_cardmarket_price(RAYQUAZA, "normal")
    assert (normal["price_eur"], normal["price_avg30"], normal["price_low"]) == (12.5, 13.0, 8.0)
    holo = extract_cardmarket_price(RAYQUAZA, "reverse_holo")
    assert (holo["price_eur"], holo["price_avg30"]) == (45.0, 42.0)
    assert "Reverse holo" in holo["price_note"]


def test_extract_price_missing_or_zero():
    assert extract_cardmarket_price({"id": "x"}) is None
    assert extract_cardmarket_price({"pricing": {"cardmarket": {"trend": 0, "avg": None}}}) is None
    assert extract_cardmarket_price({"pricing": {"cardmarket": {"avg": "2.5"}}})["price_eur"] == 2.5
    assert extract_cardmarket_price({"pricing": {"cardmarket": {"trend": 3}}}, "reverse_holo")["price_eur"] == 3
