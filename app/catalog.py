"""Card catalogue lookups against TCGdex (https://tcgdex.dev).

TCGdex is a free, open Pokémon TCG database with English and Japanese sets. From the
identifier printed on a card (set code + collector number) we find the exact card and
get its name, official image, set name and, where TCGdex has it, Cardmarket prices (EUR).

How printed set codes map to TCGdex:
- Japanese sets use the printed code as their id: M6 (Storm Emeralda), M1L, SV2a, SV11B ...
- English sets have ids like 'me01' but store the printed code as an abbreviation (MEG),
  so we narrow candidates by the printed set size (the '132' in 179/132) and compare that.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from typing import Any
from urllib.parse import quote, quote_plus

import httpx2 as httpx

from .config import settings
from .db import normalize_number
from .names import english_name

log = logging.getLogger(__name__)

_SET_CACHE_TTL = 12 * 3600
_REGULATION_MARKS = set("GHIJKL")  # regulation-mark letters OCR may glue to the set code

# Prices Cardmarket lists separately for reverse-holo ("-holo") copies.
_HOLO_FINISHES = {"reverse_holo", "poke_ball_pattern", "master_ball_pattern"}

# OCR look-alikes, folded together when comparing set codes ('MEG' vs 'MFG', 'M1L' vs 'MIL').
_SHAPE = str.maketrans({"0": "o", "q": "o", "d": "o", "1": "i", "l": "i", "|": "i", "5": "s",
                        "8": "b", "2": "z", "6": "g", "f": "e"})


class CatalogUnavailable(RuntimeError):
    """TCGdex could not be reached or returned an error."""


def _norm_code(code: str) -> str:
    return re.sub(r"[^0-9a-z]", "", (code or "").lower())


def _shape(code: str) -> str:
    return _norm_code(code).translate(_SHAPE)


def _distance(a: str, b: str) -> int:
    """Levenshtein distance (short strings)."""
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


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


def cardmarket_search_url(card: dict) -> str:
    """A Cardmarket search for the card, to check or set its price by hand."""
    query = (card.get("name_en") or card.get("name_jp") or "").strip()
    return f"https://www.cardmarket.com/en/Pokemon/Products/Search?searchString={quote_plus(query)}"


def image_url(card: dict) -> str | None:
    base = (card or {}).get("image")
    return f"{base}/high.webp" if base else None


class Tcgdex:
    """TCGdex client for one language, with caching of set data."""

    def __init__(self, base_url: str | None = None, language: str | None = None,
                 http: httpx.Client | None = None):
        self.language = language or settings.card_language
        self.base = f"{(base_url or settings.tcgdex_base_url).rstrip('/')}/{self.language}"
        self.http = http or httpx.Client(timeout=15.0, headers={"User-Agent": "pokemon-card-inventory/1.0"})
        self._lock = threading.Lock()
        self._sets: list[dict] | None = None
        self._sets_at = 0.0
        self._set_detail: dict[str, tuple[float, dict | None]] = {}

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
        with self._lock:
            self._set_detail[set_id] = (time.time(), data)
        return data

    def card(self, card_id: str) -> dict | None:
        return self._get(f"/cards/{quote(card_id, safe='')}")

    def price_for(self, tcgdex_id: str, finish: str = "normal") -> dict | None:
        card = self.card(tcgdex_id)
        return extract_cardmarket_price(card, finish) if card else None

    # --------------------------------------------------------------- lookups
    def abbreviation(self, set_id: str) -> str:
        """The set code printed on the cards (e.g. 'MEG'), if TCGdex has it."""
        detail = self.set_detail(set_id) or {}
        for key in ("abbreviation", "abbreviations"):
            value = detail.get(key)
            if isinstance(value, dict) and value.get("official"):
                return str(value["official"])
            if isinstance(value, str) and value:
                return value
        return str(detail.get("tcgOnline") or "")

    def find_set(self, code: str, total: str = "", check_total: bool = True) -> str | None:
        """TCGdex set id for a printed set code, using the printed set size to disambiguate.

        With check_total, a set whose size differs from the printed total is rejected (the code
        was probably misread).
        """
        wanted = _norm_code(code)
        variants = [v for v in {wanted, wanted[1:] if wanted[:1].upper() in _REGULATION_MARKS else ""} if len(v) >= 2]
        sets = self.sets()
        for s in sets:  # Japanese: id == printed code
            if _norm_code(s.get("id", "")) in variants:
                official = (s.get("cardCount") or {}).get("official")
                if check_total and total.isdigit() and official and int(official) != int(total):
                    continue
                return s["id"]
        if not total.isdigit():
            # Typed in without the set size ('MEG' + '179'): compare with the newest sets' printed codes.
            if variants and self.language != "ja":
                for s in reversed(sets[-40:]):
                    if _norm_code(self.abbreviation(s["id"])) in variants:
                        return s["id"]
            return None
        same_size = [s for s in sets if str((s.get("cardCount") or {}).get("official") or "") == str(int(total))]
        labels = [(s["id"], [s["id"], self.abbreviation(s["id"])]) for s in same_size]
        for set_id, names in labels:  # English: printed code == abbreviation
            if any(_norm_code(n) in variants for n in names if n):
                return set_id
        if variants:  # tolerate one OCR mistake in the code
            scored = sorted((_distance(_shape(v), _shape(n)), set_id)
                            for set_id, names in labels for n in names if n for v in variants)
            if scored and scored[0][0] <= 1 and (len(scored) == 1 or scored[1][0] > scored[0][0]
                                                 or scored[1][1] == scored[0][1]):
                return scored[0][1]
        if not wanted and len(same_size) == 1:  # code unreadable, but only one set has this size
            return same_size[0]["id"]
        return None

    def card_in_set(self, set_id: str, number: str) -> dict | None:
        wanted = normalize_number(number)
        detail = self.set_detail(set_id) or {}
        brief = next((c for c in detail.get("cards", []) if normalize_number(c.get("localId")) == wanted), None)
        return self.card(brief["id"]) if brief else None


class Catalog:
    """Looks cards up in the right language: Japanese (default) and English."""

    def __init__(self, base_url: str | None = None, http: httpx.Client | None = None,
                 languages: tuple[str, ...] | None = None):
        self.base_url = base_url
        self.http = http
        default = settings.card_language
        self.languages = languages or tuple(dict.fromkeys([default, "ja", "en"]))
        self._clients: dict[str, Tcgdex] = {}

    def client(self, language: str) -> Tcgdex:
        if language not in self._clients:
            self._clients[language] = Tcgdex(self.base_url, language, self.http)
        return self._clients[language]

    def _languages_for(self, language: str, set_code: str) -> list[str]:
        if language == "ja":
            return ["ja"]
        if language:  # EN, FR, DE ... share English set codes and numbering
            return ["en"]
        # Not printed: Japanese codes contain digits (M6, SV2a); English ones are letters (MEG).
        first = "ja" if re.search(r"\d", set_code) else "en"
        return [first] + [lang for lang in self.languages if lang != first]

    def lookup(self, *, set_code: str = "", number: str = "", total: str = "", language: str = "",
               finish: str = "normal", check_total: bool = True) -> dict | None:
        """Find the card for a printed identifier. Returns a match dict or None."""
        if not normalize_number(number):
            return None
        for lang in self._languages_for(language, set_code):
            tcg = self.client(lang)
            set_id = tcg.find_set(set_code, total, check_total)
            card = tcg.card_in_set(set_id, number) if set_id else None
            if card:
                match = to_match(card, finish, lang)
                # The code as printed on the card: the id for Japanese sets, e.g. 'MEG' for English ones.
                match["set_code"] = set_id if lang == "ja" else (tcg.abbreviation(set_id) or set_code or set_id)
                return match
        return None

    def price_for(self, tcgdex_id: str, finish: str = "normal", language: str = "ja") -> dict | None:
        return self.client("ja" if language == "ja" else "en").price_for(tcgdex_id, finish)


def to_match(card: dict, finish: str, language: str) -> dict:
    s = card.get("set") or {}
    total = (s.get("cardCount") or {}).get("official")
    name = card.get("name") or ""
    category = card.get("category") or ""
    dex_ids = card.get("dexId") or []
    return {
        "tcgdex_id": card.get("id"),
        "language": language,
        "name_jp": name if language == "ja" else "",
        "name_en": english_name(name, dex_ids, category) if language == "ja" else name,
        "set_id": s.get("id") or "",
        "set_name": s.get("name") or "",
        "set_total": str(total) if total else "",
        "number": str(card.get("localId") or ""),
        "rarity": card.get("rarity") or "",
        "category": category,
        "image_url": image_url(card),
        "price": extract_cardmarket_price(card, finish),
    }


_default: Catalog | None = None


def get_catalog() -> Catalog:
    global _default
    if _default is None:
        _default = Catalog()
    return _default
