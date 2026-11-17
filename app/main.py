"""FastAPI app: JSON API + the single-page web UI in /static."""

from __future__ import annotations

import base64
import binascii
import csv
import io
import logging
import re
import secrets
import threading
import time
import uuid
from typing import Callable, Literal

from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import catalog as catalog_mod
from . import names as names_mod
from . import reader
from .config import BASE_DIR, Settings, settings as default_settings
from .db import Database, now_iso

log = logging.getLogger("cards")
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

STATIC_DIR = BASE_DIR / "static"
MAX_UPLOAD_BYTES = 25 * 1024 * 1024
PHOTO_SIDE = 1000          # long side of the photo we keep
MAX_READS_TO_TRY = 6       # one card per photo; try the best few readings of its code

Finish = Literal["normal", "holo", "reverse_holo", "poke_ball_pattern", "master_ball_pattern"]
PHOTO_NAME = r"^[0-9a-f]{32}\.jpg$"          # only names this app generated
WEB_URL = r"^(https?://\S+)?$"               # no javascript: or data: links
IMAGE_URL = r"^(https?://\S+|/[^/\s]\S*)?$"


# --------------------------------------------------------------------------- schemas
class CardFields(BaseModel):
    name_jp: str | None = None
    name_en: str | None = None
    set_code: str | None = None
    set_name: str | None = None
    number: str | None = None
    set_total: str | None = None
    rarity: str | None = None
    finish: Finish | None = None
    category: str | None = None
    language: str | None = Field(default=None, max_length=5)
    tcgdex_id: str | None = None
    image_url: str | None = Field(default=None, pattern=IMAGE_URL)
    photo: str | None = Field(default=None, pattern=PHOTO_NAME)
    quantity: int | None = Field(default=None, ge=0, le=9999)
    notes: str | None = None
    manual_price: float | None = Field(default=None, ge=0)
    cardmarket_url: str | None = Field(default=None, pattern=WEB_URL)


class CardCreate(CardFields):
    # Price info returned by /api/scan, passed back when the card is saved.
    price_eur: float | None = None
    price_trend: float | None = None
    price_avg: float | None = None
    price_low: float | None = None
    price_avg30: float | None = None
    price_source: str | None = None
    price_note: str | None = None
    price_url: str | None = Field(default=None, pattern=WEB_URL)
    price_updated_at: str | None = None
    merge_into: int | None = None


class LookupRequest(BaseModel):
    """A typed-in identifier: set code + number (e.g. M6 + 058, or MEG + 179 with total 132)."""
    set_code: str = Field(default="", max_length=12)
    number: str = Field(default="", max_length=8)
    total: str = Field(default="", max_length=8)
    language: str = Field(default="", max_length=5)
    rarity: str = Field(default="", max_length=8)
    finish: Finish = "normal"


# --------------------------------------------------------------------------- helpers
def _clean(fields: dict) -> dict:
    out = {}
    for key, value in fields.items():
        if isinstance(value, str):
            value = value.strip()
        out[key] = value
    return out


def card_from(read: dict, match: dict | None) -> dict:
    """Card fields from the identifier (read off the card or typed in) plus the catalogue entry."""
    total = read.get("total", "")
    card = {
        "name_jp": "",
        "name_en": "",
        "set_code": read.get("set_code", ""),
        "set_name": "",
        "number": read.get("number", ""),
        "set_total": total if total.isdigit() else "",
        "rarity": read.get("rarity", ""),
        "finish": read.get("finish") or "normal",
        "category": "",
        "language": read.get("language") or "",
        "tcgdex_id": None,
        "image_url": None,
    }
    if match:
        card.update({
            "tcgdex_id": match["tcgdex_id"],
            "image_url": match["image_url"],
            "set_name": match["set_name"],
            "name_jp": match["name_jp"],
            "name_en": match["name_en"],
            "category": match["category"],
            "language": match["language"],
            "set_code": match.get("set_code") or card["set_code"],
            "number": match["number"] or card["number"],
            "set_total": card["set_total"] or match["set_total"],
            "rarity": card["rarity"] or match["rarity"],
        })
        if match.get("price"):
            card.update(match["price"])
    card["language"] = card["language"] or "ja"
    return card


class RefreshJob:
    """Background job that refreshes every card's catalogue price."""

    def __init__(self):
        self._lock = threading.Lock()
        self.state = {"running": False, "total": 0, "done": 0, "updated": 0,
                      "no_price": 0, "errors": 0, "message": "", "finished_at": None}

    def snapshot(self) -> dict:
        with self._lock:
            return dict(self.state)

    def start(self, db: Database, catalog: catalog_mod.Catalog) -> bool:
        with self._lock:
            if self.state["running"]:
                return False
            cards = db.list_cards()
            self.state = {"running": True, "total": len(cards), "done": 0, "updated": 0,
                          "no_price": 0, "errors": 0, "message": "", "finished_at": None}
        threading.Thread(target=self._run, args=(db, catalog, cards), daemon=True).start()
        return True

    def _bump(self, **changes):
        with self._lock:
            for key, value in changes.items():
                self.state[key] = self.state[key] + value if isinstance(value, int) else value

    def _run(self, db: Database, catalog: catalog_mod.Catalog, cards: list[dict]):
        try:
            for card in cards:
                try:
                    tcgdex_id = card.get("tcgdex_id")
                    if not tcgdex_id:
                        match = catalog.lookup(set_code=card["set_code"], number=card["number"],
                                               total=card["set_total"], language=card["language"],
                                               finish=card["finish"])
                        if match:
                            tcgdex_id = match["tcgdex_id"]
                            db.update_card(card["id"], link_fields(card, match))
                    price = catalog.price_for(tcgdex_id, card["finish"], card["language"]) if tcgdex_id else None
                    if price:
                        db.set_price(card["id"], price)
                        self._bump(updated=1)
                    else:
                        self._bump(no_price=1)
                except catalog_mod.CatalogUnavailable as exc:
                    self._bump(errors=1, message=f"Catalogue unreachable: {exc}")
                    break
                except Exception:  # noqa: BLE001 - keep going for the other cards
                    log.exception("Price refresh failed for card %s", card["id"])
                    self._bump(errors=1)
                finally:
                    self._bump(done=1)
        finally:
            self._bump(running=False, finished_at=now_iso())


def link_fields(card: dict, match: dict) -> dict:
    """Fields to store when an existing card gets linked to its catalogue entry."""
    return {
        "tcgdex_id": match["tcgdex_id"],
        "image_url": match["image_url"],
        "set_name": match["set_name"],
        "language": match["language"],
        "name_jp": card["name_jp"] or match["name_jp"],
        "name_en": card["name_en"] or match["name_en"],
        "set_total": card["set_total"] or match["set_total"],
        "rarity": card["rarity"] or match["rarity"],
    }


def _remove_orphan_photos(cfg: Settings, db: Database, min_age_s: int = 24 * 3600) -> None:
    """Delete photos from scans that were never saved to the collection."""
    in_use = db.photos_in_use()
    cutoff = time.time() - min_age_s
    for path in cfg.photos_dir.glob("*.jpg"):
        if path.name not in in_use and path.stat().st_mtime < cutoff:
            path.unlink(missing_ok=True)


# --------------------------------------------------------------------------- app
def create_app(
    cfg: Settings | None = None,
    *,
    db: Database | None = None,
    catalog: catalog_mod.Catalog | None = None,
    read: Callable[[bytes, bool], reader.ReadResult] | None = None,
) -> FastAPI:
    cfg = cfg or default_settings
    cfg.photos_dir.mkdir(parents=True, exist_ok=True)
    db = db or Database(cfg.db_path)
    catalog = catalog or catalog_mod.get_catalog()
    ocr_available = (lambda: True) if read else reader.tesseract_available
    read = read or reader.read_card
    refresh_job = RefreshJob()
    _remove_orphan_photos(cfg, db)

    app = FastAPI(title="Pokémon Card Inventory", docs_url="/api/docs", redoc_url=None)
    app.state.db = db

    # ---------------------------------------------------------------- auth
    @app.middleware("http")
    async def password_gate(request: Request, call_next):
        if cfg.app_password:
            ok = False
            header = request.headers.get("authorization", "")
            if header.startswith("Basic "):
                try:
                    _, _, given = base64.b64decode(header[6:]).decode("utf-8").partition(":")
                    ok = secrets.compare_digest(given.encode(), cfg.app_password.encode())
                except (binascii.Error, UnicodeDecodeError):
                    ok = False
            if not ok:
                return Response("Password required", status_code=401,
                                headers={"WWW-Authenticate": 'Basic realm="Pokemon card inventory"'})
        return await call_next(request)

    # ------------------------------------------------------------- helpers
    def card_or_404(card_id: int) -> dict:
        card = db.get_card(card_id)
        if not card:
            raise HTTPException(404, "Card not found")
        return card

    def describe(read_fields: dict, hint: str = "", by_size: bool = True) -> dict:
        """Look an identifier up and describe the result for the scan screen."""
        match, catalog_error = None, None
        # First pass: the code as read and its other readings, then the set of the packs being opened
        # (each only if its size matches the printed one). Second pass (by_size): the set identified
        # by its printed size, and as a last resort the pack's set whatever the printed size.
        codes = [read_fields.get("set_code", ""), *read_fields.get("alternatives", ())]
        use_hint = hint and hint.lower() not in (c.lower() for c in codes)
        attempts = [dict(set_code=c, check_total=True) for c in codes if c]
        if use_hint:
            attempts.append(dict(set_code=hint, check_total=True))
        if by_size:
            attempts.append(dict(set_code=codes[0], check_total=True, by_size=True))
            if use_hint:
                attempts.append(dict(set_code=hint, check_total=False))
        try:
            for attempt in attempts:
                match = catalog.lookup(number=read_fields.get("number", ""), total=read_fields.get("total", ""),
                                       language=read_fields.get("language", ""),
                                       finish=read_fields.get("finish") or "normal", **attempt)
                if match:
                    break
        except catalog_mod.CatalogUnavailable:
            catalog_error = "The card catalogue (TCGdex) could not be reached, so there is no name, image or price yet."
        card = card_from(read_fields, match)
        dup = db.find_duplicate(tcgdex_id=card["tcgdex_id"], set_code=card["set_code"],
                                number=card["number"], finish=card["finish"])
        return {
            "card": card,
            "matched": match is not None,
            "catalog_error": catalog_error,
            "duplicate": {"id": dup["id"], "quantity": dup["quantity"]} if dup else None,
            "cardmarket_search_url": catalog_mod.cardmarket_search_url(card),
        }

    def with_links(card: dict) -> dict:
        card["cardmarket_search_url"] = catalog_mod.cardmarket_search_url(card)
        return card

    def link_to_catalog(card: dict) -> dict | None:
        """Match a saved card with the catalogue and store the link, names, image and price."""
        match = catalog.lookup(set_code=card["set_code"], number=card["number"], total=card["set_total"],
                               language=card["language"], finish=card["finish"])
        if not match:
            return None
        db.update_card(card["id"], link_fields(card, match))
        if match.get("price"):
            db.set_price(card["id"], match["price"])
        return match

    def entry_for(match: dict, read_label: str) -> dict:
        card = card_from({"set_code": match["set_code"], "number": match["number"]}, match)
        dup = db.find_duplicate(tcgdex_id=card["tcgdex_id"], set_code=card["set_code"],
                                number=card["number"], finish=card["finish"])
        return {"card": card, "matched": True, "catalog_error": None, "read": read_label,
                "duplicate": {"id": dup["id"], "quantity": dup["quantity"]} if dup else None,
                "cardmarket_search_url": catalog_mod.cardmarket_search_url(card), "alternatives": []}

    def check_name(entry: dict | None, title: list[str], readings, hint: str, photo: str) -> dict | None:
        """Cross-check the match with the card's name read from the photo (Japanese cards).

        - The name matches: the match is confirmed (confident).
        - An alternative reading's card matches the name instead: swap it in.
        - Nothing matches but the name is legible: look the name up in the likely sets (the
          match's set, the pack's set, sets with the printed size), using the number readings
          to pick between prints of the same Pokémon.
        """
        if not title or not names_mod.title_readable(title) or not hasattr(catalog, "find_by_title"):
            return entry
        if entry and entry["matched"]:
            options = [entry["card"], *entry.get("alternatives", [])]
            scores = [names_mod.title_score(c.get("name_jp", ""), title) for c in options]
            best = max(range(len(options)), key=lambda i: scores[i])
            if best > 0 and scores[best] >= 0.6 and scores[best] - scores[0] >= 0.25:
                alt = options[best]
                previous = {**entry["card"], "read": entry["read"]}
                entry = entry_for({k: v for k, v in alt.items() if k not in ("read", "photo")}, alt.get("read", ""))
                entry["alternatives"] = [previous] + [o for i, o in enumerate(options[1:], 1) if i != best]
                scores[0] = scores[best]
            entry["name_check"] = round(scores[0], 2)
            if scores[0] >= 0.6:
                entry["confidence"] = "high"
                # The name confirms it: drop alternatives that are other cards (keep other prints).
                entry["alternatives"] = [a for a in entry.get("alternatives", [])
                                         if names_mod.title_score(a.get("name_jp", ""), title) >= 0.6]
                return entry
            if scores[0] >= 0.35:
                return entry  # partly legible: keep the number's answer, still marked for checking
        sets = []
        if entry and entry["matched"]:
            sets.append(entry["card"].get("set_code"))
        if hint:
            sets.append(hint)
        for r in readings:
            sets += catalog.sets_with_size(r.total)
        if not sets and hasattr(catalog, "recent_sets"):
            sets = catalog.recent_sets()  # nothing else to go on: the newest Japanese sets
        try:
            match = catalog.find_by_title(title, sets, [r.number for r in readings])
        except catalog_mod.CatalogUnavailable:
            return entry
        if not match:
            return entry
        found = entry_for(match, "name: " + (match.get("name_jp") or ""))
        found["confidence"] = "check"
        found["name_check"] = round(names_mod.title_score(match.get("name_jp", ""), title), 2)
        if entry and entry["matched"]:
            found["alternatives"] = [{**entry["card"], "read": entry["read"]}] + entry.get("alternatives", [])
        return found

    # ------------------------------------------------------------- routes
    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})

    @app.get("/api/status")
    def status():
        return {
            "ocr_available": ocr_available(),
            "card_detection": True,
            "name_check": reader.title_available(),
            "language": cfg.card_language,
            "password_protected": bool(cfg.app_password),
        }

    @app.post("/api/scan")
    def scan(file: UploadFile = File(...), set_hint: str = Query(default="", max_length=12),
             card: bool = Query(default=False, description="the photo is already the straightened card")):
        """Read the identifier in a photo (one card per photo) and look the card up."""
        raw = file.file.read(MAX_UPLOAD_BYTES + 1)
        if len(raw) > MAX_UPLOAD_BYTES:
            raise HTTPException(413, "Photo is too large (max 25 MB).")
        if not raw:
            raise HTTPException(400, "Empty upload.")
        try:
            photo_bytes = reader.prepare_image(raw, PHOTO_SIDE)
        except reader.ImageError as exc:
            raise HTTPException(400, str(exc)) from exc
        try:
            result = read(raw, card)
        except reader.ReaderUnavailable as exc:
            raise HTTPException(503, str(exc)) from exc

        photo = f"{uuid.uuid4().hex}.jpg"
        (cfg.photos_dir / photo).write_bytes(result.card_image or photo_bytes)
        hint = set_hint.strip()
        entries = []
        readings = result.identifiers[:MAX_READS_TO_TRY]  # best reading first
        # Every reading against its codes and the pack's set first; only then by set size.
        tried: dict[int, dict] = {}
        chosen = None
        for by_size in (False, True):
            for k, ident in enumerate(readings):
                if k in tried and tried[k]["matched"]:
                    continue
                fields = {"set_code": ident.set_code, "alternatives": ident.alternatives, "number": ident.number,
                          "total": ident.total, "language": ident.language, "rarity": ident.rarity}
                entry = describe(fields, hint, by_size)
                entry["read"] = ident.label
                tried[k] = entry
                if entry["matched"] and chosen is None:
                    chosen = k
            if chosen is not None:
                break
        entries = []
        entry = None
        if readings:
            k = chosen if chosen is not None else 0
            entry = tried.get(k) or describe({"set_code": readings[0].set_code, "number": readings[0].number,
                                              "total": readings[0].total}, hint)
            # Other readings that are also real cards: offered as one-tap alternatives.
            seen = {entry["card"].get("tcgdex_id")}
            entry["alternatives"] = []
            for j, other in tried.items():
                card_j = other["card"]
                if j != k and other["matched"] and card_j["tcgdex_id"] not in seen:
                    seen.add(card_j["tcgdex_id"])
                    entry["alternatives"].append({**card_j, "photo": photo, "read": other["read"]})
            votes = readings[k].hits
            runner_up = max((r.hits for j, r in enumerate(readings) if j != k), default=0)
            entry["confidence"] = "high" if entry["matched"] and votes >= 3 and votes - runner_up >= 2 \
                and not entry["alternatives"] else "check"
        entry = check_name(entry, result.title, readings, hint, photo)
        if entry:
            entries = [entry]
        for entry in entries:
            entry["card"]["photo"] = photo
        problem = "" if entries else (
            "Couldn't read the set code and number in the bottom-left corner. "
            "Type them in below, or retake the photo closer and without glare."
        )
        return {"photo": photo, "problem": problem, "cards": entries}

    @app.post("/api/lookup")
    def lookup(req: LookupRequest):
        return describe(_clean(req.model_dump()))

    @app.get("/api/cards")
    def list_cards(q: str = "", sort: str = "newest", limit: int | None = Query(default=None, ge=1, le=5000)):
        return [with_links(c) for c in db.list_cards(q=q, sort=sort, limit=limit)]

    @app.get("/api/stats")
    def stats():
        return db.stats()

    @app.post("/api/cards", status_code=201)
    def create_card(body: CardCreate):
        data = _clean(body.model_dump(exclude_none=True))
        merge_into = data.pop("merge_into", None)
        if merge_into is not None:
            existing = card_or_404(merge_into)
            card = db.change_quantity(merge_into, data.get("quantity") or 1)
            if data.get("photo") and not existing.get("photo"):
                card = db.update_card(merge_into, {"photo": data["photo"]})
            return {"action": "merged", "card": with_links(card)}
        if not (data.get("name_jp") or data.get("name_en") or (data.get("set_code") and data.get("number"))):
            raise HTTPException(422, "A card needs a name, or a set code and number.")
        card = db.create_card(data)
        return {"action": "created", "card": with_links(card)}

    @app.get("/api/cards/{card_id}")
    def get_card(card_id: int):
        card = card_or_404(card_id)
        card["history"] = db.price_history(card_id)
        return with_links(card)

    @app.patch("/api/cards/{card_id}")
    def update_card(card_id: int, body: CardFields):
        card_or_404(card_id)
        changes = _clean(body.model_dump(exclude_unset=True))
        return with_links(db.update_card(card_id, changes))

    @app.post("/api/cards/{card_id}/quantity")
    def change_quantity(card_id: int, delta: int = Query(..., ge=-100, le=100)):
        card_or_404(card_id)
        return with_links(db.change_quantity(card_id, delta))

    @app.delete("/api/cards/{card_id}", status_code=204)
    def delete_card(card_id: int):
        card = card_or_404(card_id)
        db.delete_card(card_id)
        photo = card.get("photo")
        if photo and re.fullmatch(PHOTO_NAME, photo) and photo not in db.photos_in_use():
            (cfg.photos_dir / photo).unlink(missing_ok=True)
        return Response(status_code=204)

    @app.post("/api/cards/{card_id}/rematch")
    def rematch(card_id: int):
        card = card_or_404(card_id)
        try:
            matched = link_to_catalog(card) is not None
        except catalog_mod.CatalogUnavailable as exc:
            raise HTTPException(502, f"Card catalogue unreachable: {exc}") from exc
        return {"matched": matched, "card": with_links(db.get_card(card_id))}

    @app.post("/api/cards/{card_id}/price")
    def refresh_price(card_id: int):
        """Fetch the latest Cardmarket price from the catalogue (linking the card first if needed)."""
        card = card_or_404(card_id)
        try:
            if not card.get("tcgdex_id"):
                if link_to_catalog(card) is None:
                    return {"ok": False, "card": with_links(card),
                            "message": "No catalogue match for this card. Check the set code and number."}
                card = db.get_card(card_id)
            price = catalog.price_for(card["tcgdex_id"], card["finish"], card["language"])
        except catalog_mod.CatalogUnavailable as exc:
            return {"ok": False, "card": with_links(card), "message": f"Card catalogue unreachable: {exc}"}
        if not price:
            return {"ok": False, "card": with_links(card),
                    "message": "The catalogue has no Cardmarket price for this card. "
                               "Check it on Cardmarket and enter it as your price."}
        return {"ok": True, "card": with_links(db.set_price(card_id, price))}

    @app.post("/api/prices/refresh")
    def start_refresh():
        started = refresh_job.start(db, catalog)
        return {"started": started, **refresh_job.snapshot()}

    @app.get("/api/prices/refresh")
    def refresh_status():
        return refresh_job.snapshot()

    @app.get("/api/export.csv")
    def export_csv():
        columns = ["id", "name_en", "name_jp", "language", "set_code", "set_name", "number", "set_total",
                   "rarity", "finish", "quantity", "value", "price_eur", "price_trend", "price_avg30",
                   "price_low", "manual_price", "price_source", "price_updated_at", "tcgdex_id", "created_at"]
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for card in db.list_cards(sort="total"):
            writer.writerow(card)
        data = "﻿" + buf.getvalue()  # BOM so Excel opens Japanese text correctly
        return StreamingResponse(iter([data]), media_type="text/csv; charset=utf-8",
                                 headers={"Content-Disposition": 'attachment; filename="pokemon-cards.csv"'})

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception):  # pragma: no cover - last resort
        log.exception("Unhandled error on %s", request.url.path)
        return JSONResponse({"detail": "Unexpected server error. See the server log."}, status_code=500)

    app.mount("/photos", StaticFiles(directory=cfg.photos_dir), name="photos")
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app


app = create_app()
