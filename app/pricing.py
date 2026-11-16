"""Price lookups.

1. Cardmarket data from TCGdex (free, automatic) - see catalog.py.
2. AI lookup: Claude searches cardmarket.com with the web search / web fetch
   server tools and reports the price it finds on the product page. Used when
   TCGdex has no price, which is common for Japanese cards.
3. Your own price (manual_price on the card) always wins for value and ranking.
"""

from __future__ import annotations

import logging
from urllib.parse import quote_plus, urlparse

import anthropic

from .config import settings
from .recognizer import fallback_kwargs, get_client

log = logging.getLogger(__name__)

FINISH_LABELS = {
    "normal": "normal",
    "holo": "holo",
    "reverse_holo": "reverse holo (mirror foil)",
    "poke_ball_pattern": "Poké Ball pattern mirror",
    "master_ball_pattern": "Master Ball pattern mirror",
}


def cardmarket_search_url(card: dict) -> str:
    query = (card.get("name_en") or card.get("name_jp") or "").strip()
    return f"https://www.cardmarket.com/en/Pokemon/Products/Search?searchString={quote_plus(query)}"


class PriceLookupError(RuntimeError):
    pass


REPORT_TOOL = {
    "name": "report_price",
    "description": (
        "Report the Cardmarket price you found for the card. Call this exactly once, at the end. "
        "Use 0 for any price you could not find."
    ),
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": {
            "found": {"type": "boolean", "description": "True if you found a Cardmarket product page with prices for this card."},
            "product_url": {"type": "string", "description": "URL of the Cardmarket product page, or empty."},
            "product_name": {"type": "string", "description": "Product name and expansion as shown on Cardmarket."},
            "is_japanese_version": {"type": "boolean", "description": "True if the page is for the Japanese printing."},
            "price_trend_eur": {"type": "number", "description": "'Price Trend' in EUR, 0 if unknown."},
            "avg30_eur": {"type": "number", "description": "'30-days average price' in EUR, 0 if unknown."},
            "price_from_eur": {"type": "number", "description": "'From' (lowest listing) price in EUR, 0 if unknown."},
            "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
            "notes": {"type": "string", "description": "Short English note on what you found or why not."},
        },
        "required": [
            "found", "product_url", "product_name", "is_japanese_version",
            "price_trend_eur", "avg30_eur", "price_from_eur", "confidence", "notes",
        ],
        "additionalProperties": False,
    },
}

_TOOLS = [
    {"type": "web_search_20260209", "name": "web_search", "max_uses": 4, "allowed_domains": ["cardmarket.com"]},
    {"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": 4, "allowed_domains": ["cardmarket.com"]},
    REPORT_TOOL,
]

_SYSTEM = (
    "You look up trading card prices on Cardmarket (cardmarket.com), Europe's largest card marketplace. "
    "Cardmarket lists Japanese Pokémon cards as separate products inside Japanese expansions. "
    "Find the product page for the exact printing described (same language, set and collector number), "
    "read its 'Price Trend', '30-days average price' and 'From' prices in EUR, then call report_price. "
    "Never invent numbers: if you cannot see a price, report 0 for it and explain in notes."
)


def _describe(card: dict) -> str:
    number = card.get("number") or "?"
    if card.get("set_total"):
        number = f"{number}/{card['set_total']}"
    lines = [
        f"- Japanese name: {card.get('name_jp') or '?'}",
        f"- English name: {card.get('name_en') or '?'}",
        f"- Set code: {card.get('set_code') or '?'}" + (f" ({card['set_name']})" if card.get("set_name") else ""),
        f"- Collector number: {number}",
        f"- Rarity: {card.get('rarity') or '?'}",
        f"- Finish: {FINISH_LABELS.get(card.get('finish') or 'normal', card.get('finish'))}",
        f"- Language: {'Japanese' if (card.get('language') or 'ja') == 'ja' else card.get('language')}",
    ]
    if card.get("cardmarket_url"):
        lines.append(f"- The collector says this is the Cardmarket page: {card['cardmarket_url']}")
    return "\n".join(lines)


def ai_price_lookup(card: dict, client: anthropic.Anthropic | None = None) -> dict:
    """Ask Claude to find the card on cardmarket.com. Returns price fields, or raises PriceLookupError."""
    if not settings.ai_price_search:
        raise PriceLookupError("AI price search is turned off (AI_PRICE_SEARCH=false).")
    client = client or get_client()
    messages: list[dict] = [{
        "role": "user",
        "content": f"Find the current Cardmarket price for this Pokémon card:\n{_describe(card)}",
    }]
    report = None
    try:
        for _ in range(4):  # the server may pause long tool runs; resume up to a few times
            response = client.beta.messages.create(
                model=settings.claude_model,
                max_tokens=16000,
                system=_SYSTEM,
                tools=_TOOLS,
                output_config={"effort": settings.claude_effort},
                messages=messages,
                **fallback_kwargs(settings.claude_model),
            )
            if response.stop_reason == "refusal":
                raise PriceLookupError("Claude declined this price lookup.")
            report = next((b.input for b in response.content
                           if b.type == "tool_use" and b.name == "report_price"), None)
            if report is not None or response.stop_reason != "pause_turn":
                break
            messages.append({"role": "assistant", "content": response.content})
    except anthropic.AuthenticationError as exc:
        raise PriceLookupError("Claude API key is missing or invalid.") from exc
    except anthropic.APIStatusError as exc:
        raise PriceLookupError(f"Claude API error ({exc.status_code}): {exc.message}") from exc
    except anthropic.APIConnectionError as exc:
        raise PriceLookupError("Could not reach the Claude API.") from exc

    if not report:
        raise PriceLookupError("Claude finished without reporting a price. Try again or enter it by hand.")
    return price_from_report(report)


def _cardmarket_url(url: str | None) -> str | None:
    """Keep only https links to cardmarket.com."""
    parsed = urlparse((url or "").strip())
    host = (parsed.hostname or "").lower()
    if parsed.scheme == "https" and (host == "cardmarket.com" or host.endswith(".cardmarket.com")):
        return parsed.geturl()
    return None


def price_from_report(report: dict) -> dict:
    def eur(key: str) -> float | None:
        try:
            value = float(report.get(key) or 0)
        except (TypeError, ValueError):
            return None
        return round(value, 2) if value > 0 else None

    trend, avg30, low = eur("price_trend_eur"), eur("avg30_eur"), eur("price_from_eur")
    headline = trend or avg30 or low
    if not report.get("found") or headline is None:
        raise PriceLookupError(report.get("notes") or "No Cardmarket price found for this card.")

    note_parts = [f"AI lookup, {report.get('confidence', 'low')} confidence"]
    if not report.get("is_japanese_version", True):
        note_parts.append("price is for a non-Japanese printing")
    if report.get("notes"):
        note_parts.append(report["notes"])
    return {
        "price_eur": headline,
        "price_trend": trend,
        "price_avg": None,
        "price_low": low,
        "price_avg30": avg30,
        "price_source": "Cardmarket (AI web search)",
        "price_note": " · ".join(note_parts),
        "price_url": _cardmarket_url(report.get("product_url")),
        "price_updated_at": None,
    }
