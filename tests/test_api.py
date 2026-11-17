import base64
import csv
import io
import time
from dataclasses import replace

from starlette.testclient import TestClient

from app.db import Database
from app.main import create_app

from .conftest import FakeCatalog, ident, make_jpeg


def scan(client, hint=""):
    return client.post(f"/api/scan?set_hint={hint}", files={"file": ("card.jpg", make_jpeg(), "image/jpeg")})


def test_scan_reads_identifier_and_finds_japanese_card(client, cfg):
    res = scan(client)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["problem"] == ""
    entry = body["cards"][0]
    assert entry["matched"] is True
    assert entry["read"] == "M6 058/076 RR"
    card = entry["card"]
    assert (card["name_jp"], card["name_en"]) == ("メガレックウザex", "Mega Rayquaza ex")
    assert (card["set_code"], card["number"], card["set_total"], card["rarity"]) == ("M6", "058", "076", "RR")
    assert card["language"] == "ja" and card["tcgdex_id"] == "M6-058"
    assert card["price_eur"] == 1.5
    assert (cfg.photos_dir / body["photo"]).is_file()


def test_scan_english_card_uses_printed_code(client, identifiers):
    identifiers[:] = [ident("MEG", "179", "132", language="en", rarity="")]
    card = scan(client).json()["cards"][0]["card"]
    assert (card["name_en"], card["set_code"], card["language"]) == ("Mega Gardevoir ex", "MEG", "en")


def test_unreadable_code_falls_back_to_alternative_then_pack_set(client, identifiers, fake_catalog):
    identifiers[:] = [ident("GG", alternatives=["M6"])]
    assert scan(client).json()["cards"][0]["matched"] is True
    identifiers[:] = [ident("", total="078")]  # code unreadable and set size misread
    assert scan(client).json()["cards"][0]["matched"] is False
    assert scan(client, hint="M6").json()["cards"][0]["matched"] is True
    assert fake_catalog.lookups[-1] == ("M6", "058", "078", False)  # the pack set skips the size check


def test_misread_code_with_wrong_set_size_is_rejected(client, identifiers):
    identifiers[:] = [ident("M6", total="099", alternatives=["XX"])]
    assert scan(client).json()["cards"][0]["matched"] is False


def test_scan_returns_one_card_the_first_reading_found(client, identifiers):
    identifiers[:] = [ident("M6", number="068"), ident("M6", number="058"), ident("MEG", "179", "132")]
    cards = scan(client).json()["cards"]
    assert [c["card"]["number"] for c in cards] == ["058"]


def test_scan_without_any_match_returns_the_best_reading(client, identifiers):
    identifiers[:] = [ident("M6", number="068"), ident("M6", number="069")]
    cards = scan(client).json()["cards"]
    assert [(c["card"]["number"], c["matched"]) for c in cards] == [("068", False)]


def test_straightened_card_flag_is_passed_to_the_reader(client, reads):
    scan(client)
    client.post("/api/scan?card=1", files={"file": ("card.jpg", make_jpeg(), "image/jpeg")})
    assert reads == [False, True]


def test_scan_without_identifier_keeps_photo_for_typing(client, cfg, identifiers):
    identifiers.clear()
    body = scan(client).json()
    assert body["cards"] == [] and "Type them in" in body["problem"]
    assert (cfg.photos_dir / body["photo"]).is_file()


def test_scan_rejects_non_images(client):
    res = client.post("/api/scan", files={"file": ("x.jpg", b"not an image", "image/jpeg")})
    assert res.status_code == 400


def test_scan_without_tesseract_explains(cfg, monkeypatch):
    from app import reader
    monkeypatch.setattr(reader, "tesseract_available", lambda: False)
    app = create_app(cfg, db=Database(cfg.db_path), catalog=FakeCatalog())
    with TestClient(app) as c:
        assert c.get("/api/status").json()["ocr_available"] is False
        res = c.post("/api/scan", files={"file": ("card.jpg", make_jpeg(), "image/jpeg")})
        assert res.status_code == 503 and "Tesseract" in res.json()["detail"]


def test_manual_lookup(client):
    res = client.post("/api/lookup", json={"set_code": "m6", "number": "58"}).json()
    assert res["matched"] is True and res["card"]["name_en"] == "Mega Rayquaza ex"
    assert client.post("/api/lookup", json={"set_code": "M6", "number": "999"}).json()["matched"] is False


def test_add_then_duplicate_is_detected_and_merged(client):
    entry = scan(client).json()["cards"][0]
    created = client.post("/api/cards", json=entry["card"])
    assert created.status_code == 201 and created.json()["action"] == "created"
    card_id = created.json()["card"]["id"]

    again = scan(client).json()["cards"][0]
    assert again["duplicate"] == {"id": card_id, "quantity": 1}
    merged = client.post("/api/cards", json={**again["card"], "merge_into": card_id, "quantity": 2})
    assert merged.json()["action"] == "merged" and merged.json()["card"]["quantity"] == 3
    assert len(client.get("/api/cards").json()) == 1


def test_reverse_holo_is_a_separate_card(client):
    client.post("/api/cards", json=scan(client).json()["cards"][0]["card"])
    res = client.post("/api/lookup", json={"set_code": "M6", "number": "058", "finish": "reverse_holo"}).json()
    assert res["duplicate"] is None


def test_create_requires_a_name_or_set_and_number(client):
    assert client.post("/api/cards", json={"set_code": "SV2a"}).status_code == 422
    assert client.post("/api/cards", json={"set_code": "SV2a", "number": "025"}).status_code == 201


def test_ranking_sorts_by_price_and_total_and_manual_price_wins(client):
    cheap = client.post("/api/cards", json={"name_en": "Cheap", "price_eur": 1.0, "quantity": 10}).json()["card"]
    pricey = client.post("/api/cards", json={"name_en": "Pricey", "price_eur": 5.0, "quantity": 1}).json()["card"]
    client.post("/api/cards", json={"name_en": "Unpriced"})

    assert [c["name_en"] for c in client.get("/api/cards?sort=value").json()] == ["Pricey", "Cheap", "Unpriced"]
    assert [c["name_en"] for c in client.get("/api/cards?sort=total").json()] == ["Cheap", "Pricey", "Unpriced"]

    client.patch(f"/api/cards/{pricey['id']}", json={"manual_price": 50})
    assert client.get("/api/stats").json() == {"unique_cards": 3, "total_cards": 12, "total_value": 60.0, "priced_cards": 2}
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
    card = client.post("/api/cards", json=scan(client).json()["cards"][0]["card"]).json()["card"]
    assert client.post(f"/api/cards/{card['id']}/quantity?delta=2").json()["quantity"] == 3
    assert client.post(f"/api/cards/{card['id']}/quantity?delta=-5").json()["quantity"] == 0
    photo = cfg.photos_dir / card["photo"]
    assert photo.is_file()
    assert client.delete(f"/api/cards/{card['id']}").status_code == 204
    assert client.get(f"/api/cards/{card['id']}").status_code == 404
    assert not photo.exists()


def test_price_refresh_records_history(client, fake_catalog):
    card = client.post("/api/cards", json=scan(client).json()["cards"][0]["card"]).json()["card"]
    fake_catalog.price["price_eur"] = 2.25
    res = client.post(f"/api/cards/{card['id']}/price").json()
    assert res["ok"] and res["card"]["price_eur"] == 2.25
    assert [h["price_eur"] for h in client.get(f"/api/cards/{card['id']}").json()["history"]] == [1.5, 2.25]


def test_price_refresh_links_unlinked_card_first(client):
    card = client.post("/api/cards", json={"set_code": "M6", "number": "058"}).json()["card"]
    assert card["tcgdex_id"] is None
    res = client.post(f"/api/cards/{card['id']}/price").json()
    assert res["ok"] and res["card"]["tcgdex_id"] == "M6-058" and res["card"]["price_eur"] == 1.5
    assert res["card"]["name_en"] == "Mega Rayquaza ex"  # names filled in from the catalogue


def test_price_refresh_without_match_explains_what_to_do(client):
    card = client.post("/api/cards", json={"name_jp": "ミュウ", "name_en": "Mew"}).json()["card"]
    res = client.post(f"/api/cards/{card['id']}/price").json()
    assert res["ok"] is False and "set code" in res["message"]


def test_manual_price_entered_when_adding(client):
    card = client.post("/api/cards", json={"name_jp": "ミュウ", "manual_price": 4.2}).json()["card"]
    assert card["value"] == 4.2 and card["price_eur"] is None


def test_no_claude_anywhere(client):
    status = client.get("/api/status").json()
    assert set(status) == {"ocr_available", "card_detection", "language", "password_protected"}


def test_rematch_links_card_after_fixing_number(client):
    card = client.post("/api/cards", json={"set_code": "M6", "number": "52"}).json()["card"]
    assert client.post(f"/api/cards/{card['id']}/rematch").json()["matched"] is False
    client.patch(f"/api/cards/{card['id']}", json={"number": "058"})
    res = client.post(f"/api/cards/{card['id']}/rematch").json()
    assert res["matched"] is True and res["card"]["tcgdex_id"] == "M6-058" and res["card"]["price_eur"] == 1.5


def test_bulk_refresh_links_and_prices(client):
    client.post("/api/cards", json={"set_code": "M6", "number": "058"})
    client.post("/api/cards", json={"name_jp": "ミュウ", "set_code": "M6", "number": "151"})
    assert client.post("/api/prices/refresh").json()["started"] is True
    for _ in range(100):
        status = client.get("/api/prices/refresh").json()
        if not status["running"]:
            break
        time.sleep(0.05)
    assert status["updated"] == 1 and status["no_price"] == 1 and status["errors"] == 0
    prices = sorted((c["number"], c["price_eur"]) for c in client.get("/api/cards").json())
    assert prices == [("058", 1.5), ("151", None)]


def test_csv_export_has_bom_and_japanese(client):
    client.post("/api/cards", json={"name_jp": "ピカチュウ", "name_en": "Pikachu", "price_eur": 1.5})
    res = client.get("/api/export.csv")
    text = res.content.decode("utf-8")
    assert res.status_code == 200 and text.startswith("﻿")
    rows = list(csv.DictReader(io.StringIO(text.lstrip("﻿"))))
    assert rows[0]["name_jp"] == "ピカチュウ" and rows[0]["value"] == "1.5"


def test_password_protection(cfg):
    locked = replace(cfg, app_password="s3cret")
    app = create_app(locked, db=Database(locked.db_path), catalog=FakeCatalog(), read=lambda b, s=False: None)
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
    assert client.get("/api/status").json()["ocr_available"] is True


def test_rejects_unsafe_photo_names_and_links(client, cfg):
    (cfg.data_dir / "keep.txt").write_text("important")
    assert client.post("/api/cards", json={"name_en": "X", "photo": "../keep.txt"}).status_code == 422
    assert (cfg.data_dir / "keep.txt").exists()
    assert client.post("/api/cards", json={"name_en": "X", "cardmarket_url": "javascript:alert(1)"}).status_code == 422
    card = client.post("/api/cards", json={"name_en": "X"}).json()["card"]
    assert client.patch(f"/api/cards/{card['id']}", json={"cardmarket_url": "data:text/html,hi"}).status_code == 422
    ok = client.patch(f"/api/cards/{card['id']}",
                      json={"cardmarket_url": "https://www.cardmarket.com/en/Pokemon/Products/Singles/X"})
    assert ok.status_code == 200
