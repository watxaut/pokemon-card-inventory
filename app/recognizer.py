"""Read Pokémon cards from a photo with Claude's vision model.

The photo goes to Claude with a JSON schema, and Claude returns what is printed
on each card (Japanese name, set code, collector number, rarity) plus the
official English name.
"""

from __future__ import annotations

import base64
import io
import json
import logging
from typing import Literal

import anthropic
from PIL import Image, ImageOps
from pydantic import BaseModel, ValidationError

from .config import settings

log = logging.getLogger(__name__)

FINISHES = ["normal", "holo", "reverse_holo", "poke_ball_pattern", "master_ball_pattern"]
CATEGORIES = ["pokemon", "trainer", "energy", "unknown"]


class RecognizedCard(BaseModel):
    name_jp: str
    name_en: str
    set_code: str
    number: str
    set_total: str
    rarity: str
    category: Literal["pokemon", "trainer", "energy", "unknown"]
    finish: Literal["normal", "holo", "reverse_holo", "poke_ball_pattern", "master_ball_pattern"]
    hp: str
    confidence: Literal["high", "medium", "low"]
    notes: str


class Recognition(BaseModel):
    cards: list[RecognizedCard]
    image_problem: str


class RecognitionError(RuntimeError):
    pass


_STR = {"type": "string"}
CARD_SCHEMA = {
    "type": "object",
    "properties": {
        "cards": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name_jp": {**_STR, "description": "Card name exactly as printed (Japanese), e.g. ピカチュウex"},
                    "name_en": {**_STR, "description": "Official English TCG name, e.g. Pikachu ex"},
                    "set_code": {**_STR, "description": "Set/expansion code printed bottom-left, e.g. SV2a, SV8, M1L. Empty if unreadable."},
                    "number": {**_STR, "description": "Collector number before the slash, digits as printed, e.g. 025. Empty if unreadable."},
                    "set_total": {**_STR, "description": "Number after the slash, e.g. 165. Empty if none."},
                    "rarity": {**_STR, "description": "Rarity code as printed, e.g. C, U, R, RR, AR, SR, SAR, UR, ACE, PROMO. Empty if none."},
                    "category": {"type": "string", "enum": CATEGORIES},
                    "finish": {"type": "string", "enum": FINISHES},
                    "hp": {**_STR, "description": "HP for Pokémon cards, else empty"},
                    "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                    "notes": {**_STR, "description": "Anything uncertain or notable, in English. Empty if nothing."},
                },
                "required": [
                    "name_jp", "name_en", "set_code", "number", "set_total", "rarity",
                    "category", "finish", "hp", "confidence", "notes",
                ],
                "additionalProperties": False,
            },
        },
        "image_problem": {**_STR, "description": "Empty if the photo is fine; otherwise what is wrong (blurry, glare, no card)."},
    },
    "required": ["cards", "image_problem"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """You are an expert on the Japanese Pokémon Trading Card Game. You read photos of real \
cards and report exactly what is printed on them, so a collector can catalogue them and look up prices.

For every card visible in the photo:
- name_jp: the card name as printed, in Japanese (katakana/hiragana/kanji), including suffixes such as ex, V, VMAX, VSTAR, GX.
- name_en: the official English name this card uses in the English-language TCG (e.g. ピカチュウex -> "Pikachu ex", \
博士の研究 -> "Professor's Research", ボスの指令 -> "Boss's Orders"). If the card was never released in English, give \
the official English name of the Pokémon or the most faithful translation.
- set_code and number: read the small print at the bottom-left of the card. Modern cards show a regulation mark \
letter, the set code (e.g. SV2a, SV4K, SV8a, M1L) and the collector number with the set size (e.g. 025/165), \
often followed by the rarity code. Copy digits exactly, keeping leading zeros. Promo cards show a number like 001/SV-P: \
put "001" in number and "SV-P" in set_code. Never guess digits you cannot read; leave the field empty and say so in notes.
- finish: "reverse_holo" for mirror/reverse foil cards, "poke_ball_pattern" or "master_ball_pattern" for the ball \
mirror variants, "holo" for a holofoil picture area, otherwise "normal".
- confidence: "high" only when the name, set code and number are all clearly legible.

If there is no Pokémon card in the photo, return an empty cards list and explain in image_problem."""

USER_PROMPT = "Identify the Pokémon card(s) in this photo."

# Models that support the server-side refusal fallback parameter.
_FALLBACK_PREFIXES = ("claude-opus-5", "claude-sonnet-5", "claude-fable-5")
FALLBACK_BETA = "server-side-fallback-2026-07-01"


def prepare_image(raw: bytes, max_side: int) -> bytes:
    """Rotate per EXIF, shrink so the long side is at most `max_side`, re-encode as JPEG."""
    try:
        img = Image.open(io.BytesIO(raw))
        img = ImageOps.exif_transpose(img)
    except Exception as exc:  # noqa: BLE001 - Pillow raises many types for bad input
        raise RecognitionError("That file is not an image I can read.") from exc
    img = img.convert("RGB")
    img.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=88, optimize=True)
    return out.getvalue()


def fallback_kwargs(model: str) -> dict:
    """Opt in to server-side fallbacks so a (rare) classifier refusal is retried on another model."""
    if model.startswith(_FALLBACK_PREFIXES):
        return {"betas": [FALLBACK_BETA], "fallbacks": "default"}
    return {}


_client: anthropic.Anthropic | None = None


def get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic(max_retries=3, timeout=120.0)
    return _client


def recognize(image_jpeg: bytes, client: anthropic.Anthropic | None = None) -> Recognition:
    """Send a prepared JPEG to Claude and return the cards it found."""
    client = client or get_client()
    model = settings.claude_model
    try:
        response = client.beta.messages.create(
            model=model,
            max_tokens=16000,
            system=SYSTEM_PROMPT,
            output_config={
                "effort": settings.claude_effort,
                "format": {"type": "json_schema", "schema": CARD_SCHEMA},
            },
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/jpeg",
                            "data": base64.standard_b64encode(image_jpeg).decode("ascii"),
                        },
                    },
                    {"type": "text", "text": USER_PROMPT},
                ],
            }],
            **fallback_kwargs(model),
        )
    except anthropic.AuthenticationError as exc:
        raise RecognitionError("Claude API key is missing or invalid. Set ANTHROPIC_API_KEY in .env.") from exc
    except anthropic.RateLimitError as exc:
        raise RecognitionError("Claude API rate limit reached. Wait a moment and try again.") from exc
    except anthropic.APIStatusError as exc:
        raise RecognitionError(f"Claude API error ({exc.status_code}): {exc.message}") from exc
    except anthropic.APIConnectionError as exc:
        raise RecognitionError("Could not reach the Claude API. Check your internet connection.") from exc

    if response.stop_reason == "refusal":
        raise RecognitionError("Claude declined to read this image. Try another photo.")
    if response.stop_reason == "max_tokens":
        raise RecognitionError("Claude's answer was cut off. Try a photo with fewer cards.")

    text = next((b.text for b in response.content if b.type == "text"), "")
    try:
        return Recognition.model_validate(json.loads(text))
    except (json.JSONDecodeError, ValidationError) as exc:
        log.warning("Unparseable recognition output (request %s): %r", response._request_id, text[:500])
        raise RecognitionError("Claude returned an unexpected answer. Please try again.") from exc
