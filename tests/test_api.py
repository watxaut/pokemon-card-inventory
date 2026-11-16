import base64
import csv
import io
from dataclasses import replace

from starlette.testclient import TestClient

from app.db import Database
from app.main import create_app

from .conftest import FakeCatalog, make_jpeg, read_card


def scan(client):
    return client.post("/api/scan", files={"file": ("card.jpg", make_jpeg(), "image/jpeg")})


def test_scan_returns_matched_card_with_price_and_saves_photo(client, cfg):
    res = scan(client)
    assert res.status_code == 200, res.text
    body = res.json()
    assert len(body["cards"]) == 1
    entry = body["cards"][0]
    assert entry["matched"] is True
    assert entry["duplicate"] is None
    card = entry["card"]
    assert card["name_jp"] == "ピカチュウ"
    assert card["name_en"] == "Pikachu"
    assert card["tcgdex_id"] == "SV2a-025"
    assert card["set_name"] == "ポケモンカード151"
    assert card["price_eur"] == 1.5
    assert "cardmarket.com" in entry["cardmarket_search_url"]
    assert (cfg.photos_dir / body["photo"]).is_file()


def test_scan_without_cards_reports_problem_and_drops_photo(client, cfg, recognized):
    recognized.clear()
    body = scan(client).json()
    assert body["cards"] == []
    assert body["photo"] is None
    assert list(cfg.photos_dir.glob("*.jpg")) == []


def test_scan_rejects_non_images(client):
    res = client.post("/api/scan", files={"file": ("x.jpg", b"not an image", "image/jpeg")})
    assert res.status_code == 400


def test_scan_needs_api_key(client, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    assert scan(client).status_code == 503


def test_unmatched_card_still_scans(client, recognized):
    recognized[:] = [read_card(name_jp="リザードン", name_en="Charizard", set_code="SV9", number="999")]
    entry = scan(client).json()["cards"][0]
    assert entry["matched"] is False
    assert entry["card"]["tcgdex_id"] is None
    assert entry["card"].get("price_eur") is None


def test_add_then_duplicate_is_detected_and_merged(client):
    entry = scan(client).json()["cards"][0]
    created = client.post("/api/cards", json=entry["card"])
    assert created.status_code == 201
    assert created.json()["action"] == "created"
    card_id = created.json()["card"]["id"]

    again = scan(client).json()["cards"][0]
    assert again["duplicate"] == {"id": card_id, "quantity": 1}

    merged = client.post("/api/cards", json={**again["card"], "merge_into": card_id, "quantity": 2})
    assert merged.json()["action"] == "merged"
    assert merged.json()["card"]["quantity"] == 3
    assert len(client.get("/api/cards").json()) == 1


def test_reverse_holo_is_a_separate_card(client, recognized):
    client.post("/api/cards", json=scan(client).json()["cards"][0]["card"])
    recognized[:] = [read_card(finish="reverse_holo")]
    assert scan(client).json()["cards"][0]["duplicate"] is None


def test_create_requires_a_name(client):
    assert client.post("/api/cards", json={"set_code": "SV2a"}).status_code == 422


def test_ranking_sorts_by_price_and_total_and_manual_price_wins(client):
    cheap = client.post("/api/cards", json={"name_en": "Cheap", "price_eur": 1.0, "quantity": 10}).json()["card"]
    pricey = client.post("/api/cards", json={"name_en": "Pricey", "price_eur": 5.0, "quantity": 1}).json()["card"]
    client.post("/api/cards", json={"name_en": "Unpriced"})

    by_value = [c["name_en"] for c in client.get("/api/cards?sort=value").json()]
    assert by_value == ["Pricey", "Cheap", "Unpriced"]
    by_total = [c["name_en"] for c in client.get("/api/cards?sort=total").json()]
    assert by_total == ["Cheap", "Pricey", "Unpriced"]

    client.patch(f"/api/cards/{pricey['id']}", json={"manual_price": 50})
    stats = client.get("/api/stats").json()
    assert stats == {"unique_cards": 3, "total_cards": 12, "total_value": 60.0, "priced_cards": 2}

    client.patch(f"/api/cards/{pricey['id']}", json={"manual_price": None})
    assert client.get(f"/api/cards/{pricey['id']}").json()["value"] == 5.0
    assert cheap["value"] == 1.0


def test_search_matches_japanese_and_english(client):
    client.post("/api/cards", json={"name_jp": "ピカチュウ", "name_en": "Pikachu", "set_code": "SV2a"})
    client.post("/api/cards", json={"name_jp": "リザードンex", "name_en": "Charizard ex", "set_code": "SV3"})
    assert [c["name_en"] for c in client.get("/api/cards", params={"q": "ピカ"}).json()] == ["Pikachu"]
    assert [c["name_en"] for c in client.get("/api/cards", params={"q": "charizard"}).json()] == ["Charizard ex"]
    assert len(client.get("/api/cards", params={"q": "sv"}).json()) == 2


def test_quantity_and_delete(client, cfg):
    entry = scan(client).json()["cards"][0]
    card = client.post("/api/cards", json=entry["card"]).json()["card"]
    assert client.post(f"/api/cards/{card['id']}/quantity?delta=2").json()["quantity"] == 3
    assert client.post(f"/api/cards/{card['id']}/quantity?delta=-5").json()["quantity"] == 0
    photo = cfg.photos_dir / card["photo"]
    assert photo.is_file()
    assert client.delete(f"/api/cards/{card['id']}").status_code == 204
    assert client.get(f"/api/cards/{card['id']}").status_code == 404
    assert not photo.exists()


def test_price_refresh_catalog_then_ai(client, fake_catalog, ai_calls):
    card = client.post("/api/cards", json=scan(client).json()["cards"][0]["card"]).json()["card"]
    fake_catalog.price["price_eur"] = 2.25
    res = client.post(f"/api/cards/{card['id']}/price?method=catalog").json()
    assert res["ok"] and res["card"]["price_eur"] == 2.25

    res = client.post(f"/api/cards/{card['id']}/price?method=ai").json()
    assert res["ok"] and res["card"]["price_eur"] == 42.0
    assert res["card"]["price_source"] == "Cardmarket (AI web search)"
    assert ai_calls == [card["id"]]

    history = client.get(f"/api/cards/{card['id']}").json()["history"]
    assert [h["price_eur"] for h in history] == [1.5, 2.25, 42.0]


def test_auto_price_falls_back_to_ai_for_unlinked_cards(client, ai_calls):
    card = client.post("/api/cards", json={"name_jp": "ミュウ", "name_en": "Mew"}).json()["card"]
    res = client.post(f"/api/cards/{card['id']}/price").json()
    assert res["ok"] and res["card"]["price_eur"] == 42.0
    assert ai_calls == [card["id"]]


def test_rematch_links_card_after_fixing_number(client):
    card = client.post("/api/cards", json={"name_jp": "ピカチュウ", "set_code": "SV2a", "number": "52"}).json()["card"]
    assert client.post(f"/api/cards/{card['id']}/rematch").json()["matched"] is False
    client.patch(f"/api/cards/{card['id']}", json={"number": "025"})
    res = client.post(f"/api/cards/{card['id']}/rematch").json()
    assert res["matched"] is True
    assert res["card"]["tcgdex_id"] == "SV2a-025"
    assert res["card"]["price_eur"] == 1.5


def test_bulk_refresh_links_and_prices(client):
    client.post("/api/cards", json={"name_jp": "ピカチュウ", "set_code": "SV2a", "number": "025"})
    client.post("/api/cards", json={"name_jp": "ミュウ", "set_code": "SV2a", "number": "151"})
    assert client.post("/api/prices/refresh").json()["started"] is True
    for _ in range(100):
        status = client.get("/api/prices/refresh").json()
        if not status["running"]:
            break
        import time
        time.sleep(0.05)
    assert status["updated"] == 1 and status["no_price"] == 1 and status["errors"] == 0
    prices = sorted((c["name_jp"], c["price_eur"]) for c in client.get("/api/cards").json())
    assert prices == [("ピカチュウ", 1.5), ("ミュウ", None)]


def test_lookup_endpoint(client):
    res = client.post("/api/lookup", json={"name_jp": "ピカチュウ", "set_code": "sv2a", "number": "25"}).json()
    assert res["matched"] is True and res["card"]["image_url"].endswith("high.webp")


def test_csv_export_has_bom_and_japanese(client):
    client.post("/api/cards", json={"name_jp": "ピカチュウ", "name_en": "Pikachu", "price_eur": 1.5})
    res = client.get("/api/export.csv")
    assert res.status_code == 200
    text = res.content.decode("utf-8")
    assert text.startswith("﻿")
    rows = list(csv.DictReader(io.StringIO(text.lstrip("﻿"))))
    assert rows[0]["name_jp"] == "ピカチュウ" and rows[0]["value"] == "1.5"


def test_password_protection(cfg):
    locked = replace(cfg, app_password="s3cret")
    app = create_app(locked, db=Database(locked.db_path), catalog=FakeCatalog(),
                     recognize=lambda b: None, ai_price=lambda c: {})
    with TestClient(app) as c:
        assert c.get("/api/cards").status_code == 401
        assert c.get("/").status_code == 401
        bad = base64.b64encode(b"me:wrong").decode()
        assert c.get("/api/cards", headers={"Authorization": f"Basic {bad}"}).status_code == 401
        good = base64.b64encode(b"anyone:s3cret").decode()
        assert c.get("/api/cards", headers={"Authorization": f"Basic {good}"}).status_code == 200


def test_index_and_static_served(client):
    assert "Card Binder" in client.get("/").text
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/api/status").json()["claude_configured"] is True


def test_rejects_unsafe_photo_names_and_links(client, cfg):
    (cfg.data_dir / "keep.txt").write_text("important")
    bad_photo = client.post("/api/cards", json={"name_en": "X", "photo": "../keep.txt"})
    assert bad_photo.status_code == 422
    assert (cfg.data_dir / "keep.txt").exists()
    bad_link = client.post("/api/cards", json={"name_en": "X", "cardmarket_url": "javascript:alert(1)"})
    assert bad_link.status_code == 422
    card = client.post("/api/cards", json={"name_en": "X"}).json()["card"]
    assert client.patch(f"/api/cards/{card['id']}", json={"cardmarket_url": "data:text/html,hi"}).status_code == 422
    ok = client.patch(f"/api/cards/{card['id']}",
                      json={"cardmarket_url": "https://www.cardmarket.com/en/Pokemon/Products/Singles/X"})
    assert ok.status_code == 200
