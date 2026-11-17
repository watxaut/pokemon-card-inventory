"""Card recognizer, with a fake Anthropic client (no network)."""

import base64
import io
import json
from types import SimpleNamespace

import pytest
from PIL import Image

from app import recognizer
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
