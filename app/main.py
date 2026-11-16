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

from fastapi import BackgroundTasks, FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import catalog as catalog_mod
from . import pricing, recognizer
from .config import BASE_DIR, Settings, settings as default_settings
from .db import Database, now_iso

log = logging.getLogger("cards")
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

STATIC_DIR = BASE_DIR / "static"
MAX_UPLOAD_BYTES = 25 * 1024 * 1024
CLAUDE_IMAGE_SIDE = 1568   # long side sent to Claude
PHOTO_SIDE = 1000          # long side of the photo we keep

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
    language: str | None = None
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
    name_jp: str = ""
    name_en: str = ""
    set_code: str = ""
    number: str = ""
    finish: Finish = "normal"


# --------------------------------------------------------------------------- helpers
def _clean(fields: dict) -> dict:
    out = {}
    for key, value in fields.items():
        if isinstance(value, str):
            value = value.strip()
        out[key] = value
    return out


def merge_with_match(read: dict, match: dict | None) -> dict:
    """Combine what Claude read off the card with the catalogue entry."""
    card = {
        "name_jp": read.get("name_jp", ""),
        "name_en": read.get("name_en", ""),
        "set_code": read.get("set_code", ""),
        "set_name": "",
        "number": read.get("number", ""),
        "set_total": read.get("set_total", ""),
        "rarity": read.get("rarity", ""),
        "finish": read.get("finish") or "normal",
        "category": read.get("category", ""),
        "language": "ja",
        "tcgdex_id": None,
        "image_url": None,
    }
    if match:
        card["tcgdex_id"] = match["tcgdex_id"]
        card["image_url"] = match["image_url"]
        card["set_name"] = match["set_name"]
        card["set_code"] = card["set_code"] or match["set_id"]
        card["number"] = card["number"] or match["number"]
        card["set_total"] = card["set_total"] or match["set_total"]
        card["rarity"] = card["rarity"] or match["rarity"]
        card["name_jp"] = card["name_jp"] or match["name"]
        if match.get("price"):
            card.update(match["price"])
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

    def start(self, db: Database, catalog: catalog_mod.Tcgdex) -> bool:
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

    def _run(self, db: Database, catalog: catalog_mod.Tcgdex, cards: list[dict]):
        try:
            for card in cards:
                try:
                    tcgdex_id = card.get("tcgdex_id")
                    if not tcgdex_id:
                        match = catalog.lookup(name_jp=card["name_jp"], set_code=card["set_code"],
                                               number=card["number"], finish=card["finish"])
                        if match:
                            tcgdex_id = match["tcgdex_id"]
                            db.update_card(card["id"], {
                                "tcgdex_id": tcgdex_id,
                                "image_url": card.get("image_url") or match["image_url"],
                                "set_name": card.get("set_name") or match["set_name"],
                            })
                    price = catalog.price_for(tcgdex_id, card["finish"]) if tcgdex_id else None
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
    catalog: catalog_mod.Tcgdex | None = None,
    recognize: Callable[[bytes], recognizer.Recognition] | None = None,
    ai_price: Callable[[dict], dict] | None = None,
) -> FastAPI:
    cfg = cfg or default_settings
    cfg.photos_dir.mkdir(parents=True, exist_ok=True)
    db = db or Database(cfg.db_path)
    catalog = catalog or catalog_mod.get_catalog()
    recognize = recognize or recognizer.recognize
    ai_price = ai_price or pricing.ai_price_lookup
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

    def lookup_and_describe(read: dict) -> dict:
        match, catalog_error = None, None
        try:
            match = catalog.lookup(name_jp=read.get("name_jp", ""), set_code=read.get("set_code", ""),
                                   number=read.get("number", ""), finish=read.get("finish") or "normal")
        except catalog_mod.CatalogUnavailable:
            catalog_error = "The card catalogue (TCGdex) could not be reached, so there is no official image or price yet."
        card = merge_with_match(read, match)
        dup = db.find_duplicate(tcgdex_id=card["tcgdex_id"], set_code=card["set_code"],
                                number=card["number"], finish=card["finish"])
        return {
            "card": card,
            "matched": match is not None,
            "catalog_error": catalog_error,
            "duplicate": {"id": dup["id"], "quantity": dup["quantity"]} if dup else None,
            "cardmarket_search_url": pricing.cardmarket_search_url(card),
        }

    def ai_available() -> bool:
        return cfg.ai_price_search and cfg.claude_configured

    def run_ai_price(card_id: int) -> None:
        card = db.get_card(card_id)
        if not card:
            return
        try:
            db.set_price(card_id, ai_price(card))
        except pricing.PriceLookupError as exc:
            db.update_card(card_id, {"price_note": f"AI lookup: {exc}"})
        except Exception:  # noqa: BLE001 - background task; log and move on
            log.exception("AI price lookup failed for card %s", card_id)

    def with_links(card: dict) -> dict:
        card["cardmarket_search_url"] = pricing.cardmarket_search_url(card)
        return card

    # ------------------------------------------------------------- routes
    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})

    @app.get("/api/status")
    def status():
        return {
            "claude_configured": cfg.claude_configured,
            "model": cfg.claude_model,
            "ai_price_search": ai_available(),
            "auto_ai_price": cfg.auto_ai_price and ai_available(),
            "language": cfg.card_language,
            "password_protected": bool(cfg.app_password),
        }

    @app.post("/api/scan")
    def scan(file: UploadFile = File(...)):
        raw = file.file.read(MAX_UPLOAD_BYTES + 1)
        if len(raw) > MAX_UPLOAD_BYTES:
            raise HTTPException(413, "Photo is too large (max 25 MB).")
        if not raw:
            raise HTTPException(400, "Empty upload.")
        if not cfg.claude_configured:
            raise HTTPException(503, "Card recognition needs a Claude API key: set ANTHROPIC_API_KEY in .env and restart.")
        try:
            for_claude = recognizer.prepare_image(raw, CLAUDE_IMAGE_SIDE)
            photo_bytes = recognizer.prepare_image(raw, PHOTO_SIDE)
        except recognizer.RecognitionError as exc:
            raise HTTPException(400, str(exc)) from exc
        photo = f"{uuid.uuid4().hex}.jpg"
        (cfg.photos_dir / photo).write_bytes(photo_bytes)
        try:
            result = recognize(for_claude)
        except recognizer.RecognitionError as exc:
            (cfg.photos_dir / photo).unlink(missing_ok=True)
            raise HTTPException(502, str(exc)) from exc
        if not result.cards:
            (cfg.photos_dir / photo).unlink(missing_ok=True)
            photo = None
        cards = []
        for read in result.cards:
            entry = lookup_and_describe(read.model_dump())
            entry["card"]["photo"] = photo
            entry["confidence"] = read.confidence
            entry["notes"] = read.notes
            cards.append(entry)
        return {"photo": photo, "image_problem": result.image_problem, "cards": cards}

    @app.post("/api/lookup")
    def lookup(req: LookupRequest):
        return lookup_and_describe(_clean(req.model_dump()))

    @app.get("/api/cards")
    def list_cards(q: str = "", sort: str = "newest", limit: int | None = Query(default=None, ge=1, le=5000)):
        return [with_links(c) for c in db.list_cards(q=q, sort=sort, limit=limit)]

    @app.get("/api/stats")
    def stats():
        return db.stats()

    @app.post("/api/cards", status_code=201)
    def create_card(body: CardCreate, background: BackgroundTasks):
        data = _clean(body.model_dump(exclude_none=True))
        merge_into = data.pop("merge_into", None)
        if merge_into is not None:
            existing = card_or_404(merge_into)
            card = db.change_quantity(merge_into, data.get("quantity") or 1)
            if data.get("photo") and not existing.get("photo"):
                card = db.update_card(merge_into, {"photo": data["photo"]})
            return {"action": "merged", "card": with_links(card)}
        if not (data.get("name_jp") or data.get("name_en")):
            raise HTTPException(422, "A card needs at least a Japanese or English name.")
        card = db.create_card(data)
        if card["price_eur"] is None and cfg.auto_ai_price and ai_available():
            background.add_task(run_ai_price, card["id"])
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
            match = catalog.lookup(name_jp=card["name_jp"], set_code=card["set_code"],
                                   number=card["number"], finish=card["finish"])
        except catalog_mod.CatalogUnavailable as exc:
            raise HTTPException(502, f"Card catalogue unreachable: {exc}") from exc
        if not match:
            return {"matched": False, "card": with_links(card)}
        db.update_card(card_id, {
            "tcgdex_id": match["tcgdex_id"],
            "image_url": match["image_url"],
            "set_name": match["set_name"],
            "set_total": card["set_total"] or match["set_total"],
            "rarity": card["rarity"] or match["rarity"],
        })
        if match.get("price"):
            db.set_price(card_id, match["price"])
        return {"matched": True, "card": with_links(db.get_card(card_id))}

    @app.post("/api/cards/{card_id}/price")
    def refresh_price(card_id: int, method: Literal["auto", "catalog", "ai"] = "auto"):
        card = card_or_404(card_id)
        message = ""
        if method in ("auto", "catalog") and card.get("tcgdex_id"):
            try:
                price = catalog.price_for(card["tcgdex_id"], card["finish"])
            except catalog_mod.CatalogUnavailable as exc:
                price, message = None, f"Card catalogue unreachable: {exc}"
            if price:
                return {"ok": True, "card": with_links(db.set_price(card_id, price))}
            message = message or "The catalogue has no Cardmarket price for this card."
        elif method == "catalog":
            message = "This card is not linked to the catalogue yet. Try 'Re-match' first."
        if method in ("auto", "ai"):
            if not ai_available():
                message = (message + " " if message else "") + "AI price search is not available (needs ANTHROPIC_API_KEY and AI_PRICE_SEARCH=true)."
            else:
                try:
                    return {"ok": True, "card": with_links(db.set_price(card_id, ai_price(card)))}
                except pricing.PriceLookupError as exc:
                    message = str(exc)
                    db.update_card(card_id, {"price_note": f"AI lookup: {exc}"})
        return {"ok": False, "message": message.strip(), "card": with_links(db.get_card(card_id))}

    @app.post("/api/prices/refresh")
    def start_refresh():
        started = refresh_job.start(db, catalog)
        return {"started": started, **refresh_job.snapshot()}

    @app.get("/api/prices/refresh")
    def refresh_status():
        return refresh_job.snapshot()

    @app.get("/api/export.csv")
    def export_csv():
        columns = ["id", "name_en", "name_jp", "set_code", "set_name", "number", "set_total", "rarity",
                   "finish", "quantity", "value", "price_eur", "price_trend", "price_avg30", "price_low",
                   "manual_price", "price_source", "price_updated_at", "tcgdex_id", "created_at"]
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
