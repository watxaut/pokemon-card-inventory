"""Recognizer and AI price lookup, with a fake Anthropic client (no network)."""

import base64
import io
import json
from types import SimpleNamespace

import pytest
from PIL import Image

from app import pricing, recognizer
from app.config import settings

from .conftest import make_jpeg


class FakeMessages:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.responses.pop(0)


def fake_client(*responses):
    messages = FakeMessages(responses)
    return SimpleNamespace(beta=SimpleNamespace(messages=messages)), messages


def response(content, stop_reason="end_turn"):
    return SimpleNamespace(content=content, stop_reason=stop_reason, _request_id="req_test")


def text_block(text):
    return SimpleNamespace(type="text", text=text)


GOOD_JSON = json.dumps({
    "cards": [{
        "name_jp": "ピカチュウex", "name_en": "Pikachu ex", "set_code": "SV8", "number": "033",
        "set_total": "106", "rarity": "RR", "category": "pokemon", "finish": "normal", "hp": "200",
        "confidence": "high", "notes": "",
    }],
    "image_problem": "",
})


def test_recognize_sends_image_schema_and_fallback():
    client, messages = fake_client(response([text_block(GOOD_JSON)]))
    jpeg = make_jpeg(300, 400)
    result = recognizer.recognize(jpeg, client=client)

    assert result.cards[0].name_en == "Pikachu ex"
    assert result.cards[0].number == "033"
    call = messages.calls[0]
    assert call["model"] == settings.claude_model
    assert call["output_config"]["format"]["type"] == "json_schema"
    assert call["output_config"]["format"]["schema"] == recognizer.CARD_SCHEMA
    image = call["messages"][0]["content"][0]
    assert image["type"] == "image"
    assert base64.standard_b64decode(image["source"]["data"]) == jpeg
    if settings.claude_model.startswith("claude-opus-5"):
        assert call["fallbacks"] == "default"
        assert call["betas"] == [recognizer.FALLBACK_BETA]
    assert "temperature" not in call


def test_fallback_kwargs_only_for_supported_models():
    assert recognizer.fallback_kwargs("claude-opus-5-5")["fallbacks"] == "default"
    assert recognizer.fallback_kwargs("claude-sonnet-5-5")["fallbacks"] == "default"
    assert recognizer.fallback_kwargs("claude-haiku-5-5") == {}


@pytest.mark.parametrize("stop_reason", ["refusal", "max_tokens"])
def test_recognize_bad_stop_reasons(stop_reason):
    client, _ = fake_client(response([], stop_reason=stop_reason))
    with pytest.raises(recognizer.RecognitionError):
        recognizer.recognize(make_jpeg(10, 10), client=client)


def test_recognize_invalid_json():
    client, _ = fake_client(response([text_block("{not json")]))
    with pytest.raises(recognizer.RecognitionError):
        recognizer.recognize(make_jpeg(10, 10), client=client)


def test_prepare_image_shrinks_and_applies_exif_rotation():
    img = Image.new("RGB", (4000, 3000), (10, 20, 30))
    exif = Image.Exif()
    exif[0x0112] = 6  # rotate 90° on display
    buf = io.BytesIO()
    img.save(buf, format="JPEG", exif=exif)
    out = Image.open(io.BytesIO(recognizer.prepare_image(buf.getvalue(), 1568)))
    assert out.format == "JPEG"
    assert out.size == (1176, 1568)  # portrait after rotation, long side capped


def test_prepare_image_rejects_garbage():
    with pytest.raises(recognizer.RecognitionError):
        recognizer.prepare_image(b"hello", 100)


REPORT = {
    "found": True, "product_url": "https://www.cardmarket.com/en/Pokemon/Products/Singles/SV2a/Pikachu",
    "product_name": "Pikachu (SV2a 025)", "is_japanese_version": True, "price_trend_eur": 0.9,
    "avg30_eur": 0.85, "price_from_eur": 0.2, "confidence": "high", "notes": "",
}


def tool_use(report):
    return SimpleNamespace(type="tool_use", name="report_price", input=report)


CARD = {"name_jp": "ピカチュウ", "name_en": "Pikachu", "set_code": "SV2a", "number": "025",
        "set_total": "165", "rarity": "C", "finish": "normal", "language": "ja"}


def test_ai_price_lookup_resumes_after_pause_and_reads_report():
    paused = response([text_block("searching")], stop_reason="pause_turn")
    done = response([tool_use(REPORT)], stop_reason="tool_use")
    client, messages = fake_client(paused, done)
    price = pricing.ai_price_lookup(CARD, client=client)

    assert price["price_eur"] == 0.9
    assert price["price_low"] == 0.2
    assert price["price_url"].startswith("https://www.cardmarket.com/")
    assert len(messages.calls) == 2
    second = messages.calls[1]["messages"]
    assert second[-1]["role"] == "assistant"  # paused turn was sent back to continue
    tools = {t["name"]: t for t in messages.calls[0]["tools"]}
    assert tools["web_search"]["allowed_domains"] == ["cardmarket.com"]
    assert tools["report_price"]["strict"] is True
    assert "SV2a" in messages.calls[0]["messages"][0]["content"]


def test_ai_price_lookup_not_found():
    client, _ = fake_client(response([tool_use({**REPORT, "found": False, "notes": "No listing"})], "tool_use"))
    with pytest.raises(pricing.PriceLookupError, match="No listing"):
        pricing.ai_price_lookup(CARD, client=client)


def test_ai_price_flags_non_japanese_printing():
    price = pricing.price_from_report({**REPORT, "is_japanese_version": False, "price_trend_eur": 0})
    assert price["price_eur"] == 0.85  # falls back to the 30-day average
    assert "non-Japanese" in price["price_note"]


def test_ai_price_drops_non_cardmarket_links():
    assert pricing.price_from_report({**REPORT, "product_url": "javascript:alert(1)"})["price_url"] is None
    assert pricing.price_from_report({**REPORT, "product_url": "https://evil.example/cardmarket.com"})["price_url"] is None
    assert pricing.price_from_report(REPORT)["price_url"] == REPORT["product_url"]
