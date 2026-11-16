import httpx2 as httpx
import pytest

from app.catalog import CatalogUnavailable, Tcgdex, extract_cardmarket_price
from app.db import normalize_number

PIKACHU = {
    "id": "SV2a-025", "localId": "025", "name": "ピカチュウ", "category": "ポケモン", "rarity": "コモン",
    "image": "https://assets.tcgdex.net/ja/SV/SV2a/025",
    "set": {"id": "SV2a", "name": "ポケモンカード151", "cardCount": {"official": 165, "total": 210}},
    "pricing": {"cardmarket": {
        "updated": "2026-11-15T00:00:00Z", "unit": "EUR", "avg": 1.1, "low": 0.3, "trend": 1.25,
        "avg1": 1.0, "avg7": 1.2, "avg30": 1.3, "avg-holo": 4.0, "low-holo": 2.5, "trend-holo": 4.5, "avg30-holo": 4.2,
    }},
}


def make_client(routes: dict, calls: list | None = None) -> Tcgdex:
    def handler(request: httpx.Request) -> httpx.Response:
        key = request.url.path + (f"?{request.url.query.decode()}" if request.url.query else "")
        if calls is not None:
            calls.append(key)
        if key in routes:
            return httpx.Response(200, json=routes[key])
        return httpx.Response(404, json={"error": "not found"})

    http = httpx.Client(transport=httpx.MockTransport(handler))
    return Tcgdex(base_url="https://api.test/v2", language="ja", http=http)


ROUTES = {
    "/v2/ja/sets": [{"id": "SV2a", "name": "ポケモンカード151"}, {"id": "SV3", "name": "黒炎の支配者"}],
    "/v2/ja/sets/SV2a": {"id": "SV2a", "cards": [
        {"id": "SV2a-001", "localId": "001", "name": "フシギダネ"},
        {"id": "SV2a-025", "localId": "025", "name": "ピカチュウ"},
    ]},
    "/v2/ja/cards/SV2a-025": PIKACHU,
}


@pytest.mark.parametrize("raw, expected", [
    ("025/165", "25"), ("025", "25"), ("25", "25"), ("100", "100"), ("0", "0"),
    ("001/SV-P", "1"), (" 007 ", "7"), ("", ""), (None, ""),
])
def test_normalize_number(raw, expected):
    assert normalize_number(raw) == expected


def test_lookup_by_set_code_and_number_is_case_insensitive():
    calls = []
    tcg = make_client(ROUTES, calls)
    match = tcg.lookup(name_jp="ピカチュウ", set_code="sv2A", number="25")
    assert match["tcgdex_id"] == "SV2a-025"
    assert match["set_name"] == "ポケモンカード151"
    assert match["set_total"] == "165"
    assert match["image_url"] == "https://assets.tcgdex.net/ja/SV/SV2a/025/high.webp"
    assert match["price"]["price_eur"] == 1.25
    # set list and set detail are cached
    tcg.lookup(set_code="SV2a", number="025")
    assert calls.count("/v2/ja/sets") == 1
    assert calls.count("/v2/ja/sets/SV2a") == 1


def test_lookup_falls_back_to_name_search():
    routes = {**ROUTES, "/v2/ja/cards?name=eq%3A%E3%83%94%E3%82%AB%E3%83%81%E3%83%A5%E3%82%A6": [
        {"id": "SV2a-025", "localId": "025"}, {"id": "SV1S-030", "localId": "030"},
    ]}
    tcg = make_client(routes)
    # unknown set code, but the number singles out one candidate
    assert tcg.lookup(name_jp="ピカチュウ", set_code="XX", number="025")["tcgdex_id"] == "SV2a-025"
    # no number: two candidates -> ambiguous -> no match
    assert tcg.lookup(name_jp="ピカチュウ") is None


def test_lookup_returns_none_when_nothing_matches():
    assert make_client(ROUTES).lookup(name_jp="ミュウツー", set_code="SV2a", number="150") is None


def test_unreachable_catalog_raises():
    def boom(request):
        raise httpx.ConnectError("no network", request=request)

    tcg = Tcgdex(base_url="https://api.test/v2", language="ja",
                 http=httpx.Client(transport=httpx.MockTransport(boom)))
    with pytest.raises(CatalogUnavailable):
        tcg.lookup(set_code="SV2a", number="025")


def test_extract_price_prefers_trend_and_handles_holo():
    normal = extract_cardmarket_price(PIKACHU, "normal")
    assert (normal["price_eur"], normal["price_avg30"], normal["price_low"]) == (1.25, 1.3, 0.3)
    holo = extract_cardmarket_price(PIKACHU, "reverse_holo")
    assert (holo["price_eur"], holo["price_avg30"]) == (4.5, 4.2)
    assert "Reverse holo" in holo["price_note"]


def test_extract_price_missing_or_zero():
    assert extract_cardmarket_price({"id": "x"}) is None
    assert extract_cardmarket_price({"pricing": {"cardmarket": {"trend": 0, "avg": None}}}) is None
    only_avg = extract_cardmarket_price({"pricing": {"cardmarket": {"avg": "2.5"}}})
    assert only_avg["price_eur"] == 2.5
    # holo finish without holo prices uses the normal prices
    assert extract_cardmarket_price({"pricing": {"cardmarket": {"trend": 3}}}, "reverse_holo")["price_eur"] == 3
