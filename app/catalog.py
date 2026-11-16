"""Card catalogue lookups against TCGdex (https://tcgdex.dev).

TCGdex is a free, open Pokémon TCG database with Japanese sets. For a card read
from a photo we look up the official image, the canonical set name, and, where
TCGdex has it, Cardmarket price data (EUR).
"""

from __future__ import annotations

import logging
import re
import threading
import time
from typing import Any
from urllib.parse import quote

import httpx2 as httpx

from .config import settings
from .db import normalize_number

log = logging.getLogger(__name__)

_SET_CACHE_TTL = 12 * 3600

# Prices Cardmarket lists separately for reverse-holo ("-holo") copies.
_HOLO_FINISHES = {"reverse_holo", "poke_ball_pattern", "master_ball_pattern"}


def _norm_code(code: str) -> str:
    return re.sub(r"[^0-9a-z]", "", (code or "").lower())


def _num(value: Any) -> float | None:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return round(f, 2) if f > 0 else None


def extract_cardmarket_price(card: dict, finish: str = "normal") -> dict | None:
    """Turn TCGdex's `pricing.cardmarket` block into our price fields (or None)."""
    cm = ((card or {}).get("pricing") or {}).get("cardmarket") or {}
    if not cm:
        return None
    suffix = "-holo" if finish in _HOLO_FINISHES and _num(cm.get("trend-holo") or cm.get("avg-holo")) else ""

    def pick(key: str) -> float | None:
        return _num(cm.get(key + suffix))

    trend, avg, low, avg30 = pick("trend"), pick("avg"), pick("low"), pick("avg30")
    headline = trend or avg30 or avg or low
    if headline is None:
        return None
    unit = cm.get("unit") or "EUR"
    note = "Reverse holo price" if suffix else ""
    if unit != "EUR":
        note = f"{note} (unit: {unit})".strip()
    return {
        "price_eur": headline,
        "price_trend": trend,
        "price_avg": avg,
        "price_low": low,
        "price_avg30": avg30,
        "price_source": "Cardmarket (via TCGdex)",
        "price_note": note,
        "price_url": None,
        "price_updated_at": cm.get("updated"),
    }


def image_url(card: dict) -> str | None:
    base = (card or {}).get("image")
    return f"{base}/high.webp" if base else None


class Tcgdex:
    def __init__(self, base_url: str | None = None, language: str | None = None,
                 http: httpx.Client | None = None):
        self.base = f"{(base_url or settings.tcgdex_base_url).rstrip('/')}/{language or settings.card_language}"
        self.http = http or httpx.Client(timeout=15.0, headers={"User-Agent": "pokemon-card-inventory/1.0"})
        self._lock = threading.Lock()
        self._sets: list[dict] | None = None
        self._sets_at = 0.0
        self._set_detail: dict[str, tuple[float, dict]] = {}

    # ------------------------------------------------------------- raw calls
    def _get(self, path: str, params: dict | None = None) -> Any | None:
        try:
            resp = self.http.get(self.base + path, params=params)
        except httpx.HTTPError as exc:
            log.warning("TCGdex request failed for %s: %s", path, exc)
            raise CatalogUnavailable(str(exc)) from exc
        if resp.status_code == 404:
            return None
        if resp.status_code >= 400:
            raise CatalogUnavailable(f"TCGdex returned HTTP {resp.status_code}")
        try:
            return resp.json()
        except ValueError as exc:
            raise CatalogUnavailable("TCGdex returned invalid JSON") from exc

    def sets(self) -> list[dict]:
        with self._lock:
            fresh = self._sets is not None and time.time() - self._sets_at < _SET_CACHE_TTL
        if not fresh:
            data = self._get("/sets") or []
            with self._lock:
                self._sets, self._sets_at = data, time.time()
        return self._sets or []

    def set_detail(self, set_id: str) -> dict | None:
        with self._lock:
            hit = self._set_detail.get(set_id)
        if hit and time.time() - hit[0] < _SET_CACHE_TTL:
            return hit[1]
        data = self._get(f"/sets/{quote(set_id, safe='')}")
        if data:
            with self._lock:
                self._set_detail[set_id] = (time.time(), data)
        return data

    def card(self, card_id: str) -> dict | None:
        return self._get(f"/cards/{quote(card_id, safe='')}")

    def search_by_name(self, name: str) -> list[dict]:
        results = self._get("/cards", params={"name": f"eq:{name}"}) or []
        if not results:
            results = self._get("/cards", params={"name": name}) or []
        return results if isinstance(results, list) else []

    # ---------------------------------------------------------------- lookup
    def resolve_set_id(self, set_code: str) -> str | None:
        wanted = _norm_code(set_code)
        if not wanted:
            return None
        for s in self.sets():
            if _norm_code(s.get("id", "")) == wanted:
                return s["id"]
        return None

    def lookup(self, *, name_jp: str = "", set_code: str = "", number: str = "",
               finish: str = "normal") -> dict | None:
        """Find the catalogue entry for a card. Returns a match dict or None."""
        wanted = normalize_number(number)
        card = None

        set_id = self.resolve_set_id(set_code) if set_code else None
        if set_id and wanted:
            detail = self.set_detail(set_id) or {}
            brief = next((c for c in detail.get("cards", [])
                          if normalize_number(c.get("localId")) == wanted), None)
            if brief:
                card = self.card(brief["id"])

        if card is None and name_jp:
            candidates = self.search_by_name(name_jp)
            if wanted:
                candidates = [c for c in candidates if normalize_number(c.get("localId")) == wanted]
            if set_code and len(candidates) > 1:
                same_set = [c for c in candidates if _norm_code(c.get("id", "")).startswith(_norm_code(set_code))]
                candidates = same_set or candidates
            if len(candidates) == 1:  # only accept an unambiguous match
                card = self.card(candidates[0]["id"])

        return self._to_match(card, finish) if card else None

    def price_for(self, tcgdex_id: str, finish: str = "normal") -> dict | None:
        card = self.card(tcgdex_id)
        return extract_cardmarket_price(card, finish) if card else None

    @staticmethod
    def _to_match(card: dict, finish: str) -> dict:
        s = card.get("set") or {}
        total = (s.get("cardCount") or {}).get("official")
        return {
            "tcgdex_id": card.get("id"),
            "name": card.get("name") or "",
            "set_id": s.get("id") or "",
            "set_name": s.get("name") or "",
            "set_total": str(total) if total else "",
            "number": str(card.get("localId") or ""),
            "rarity": card.get("rarity") or "",
            "category": card.get("category") or "",
            "image_url": image_url(card),
            "price": extract_cardmarket_price(card, finish),
        }


class CatalogUnavailable(RuntimeError):
    """TCGdex could not be reached or returned an error."""


_default: Tcgdex | None = None


def get_catalog() -> Tcgdex:
    global _default
    if _default is None:
        _default = Tcgdex()
    return _default
