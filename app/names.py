"""Japanese -> English card names without AI.

Pokémon cards: the catalogue gives the National Pokédex number, which maps to the
official English species name (bundled from PokeAPI). Prefixes such as メガ (Mega) or
regional forms and suffixes such as ex / V / VMAX are translated by rule.
Trainer and Energy cards: item names come from PokeAPI, plus a short list of common
trainer cards. Anything unknown is left blank for you to fill in.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

_DATA = Path(__file__).resolve().parent / "data" / "names.json"

_SUFFIXES = [  # longest first
    ("VSTAR", " VSTAR"), ("VMAX", " VMAX"), ("V-UNION", " V-UNION"), ("BREAK", " BREAK"),
    ("LEGEND", " LEGEND"), ("ex", " ex"), ("EX", "-EX"), ("GX", "-GX"), ("V", " V"),
]
_PREFIXES = [
    ("メガ", "Mega "), ("アローラ", "Alolan "), ("ガラル", "Galarian "), ("ヒスイ", "Hisuian "),
    ("パルデア", "Paldean "), ("かがやく", "Radiant "),
]
# Forms written after the Pokémon's name; English puts them in front ("Hearthflame Mask Ogerpon ex").
_FORMS = {
    "かまどのめん": "Hearthflame Mask", "いどのめん": "Wellspring Mask", "いしずえのめん": "Cornerstone Mask",
    "みどりのめん": "Teal Mask", "れいばのすがた": "Ice Rider", "こくばのすがた": "Shadow Rider",
    "オリジンフォルム": "Origin Forme", "ブラッドムーン": "Bloodmoon",
}
# Trainers whose Pokémon appear on cards as "<trainer>の<pokemon>".
_OWNERS = {
    "N": "N", "リーリエ": "Lillie", "ヒビキ": "Ethan", "カスミ": "Misty", "タケシ": "Brock",
    "エリカ": "Erika", "ダイゴ": "Steven", "シロナ": "Cynthia", "ナンジャモ": "Iono", "ホップ": "Hop",
    "マリィ": "Marnie", "アオキ": "Larry", "ペパー": "Arven", "ネモ": "Nemona", "ボタン": "Penny",
    "サカキ": "Giovanni", "ロケット団": "Team Rocket", "グズマ": "Guzma", "ブライア": "Briar",
}
_ENERGY_TYPES = {
    "草": "Grass", "炎": "Fire", "水": "Water", "雷": "Lightning", "超": "Psychic",
    "闘": "Fighting", "悪": "Darkness", "鋼": "Metal", "フェアリー": "Fairy",
}
# Common trainer cards that are not items in the games.
_TRAINERS = {
    "博士の研究": "Professor's Research", "ボスの指令": "Boss's Orders", "ナンジャモ": "Iono",
    "ペパー": "Arven", "ネモ": "Nemona", "ジャッジマン": "Judge", "ポケギア3.0": "Pokégear 3.0",
    "なかよしポフィン": "Buddy-Buddy Poffin", "ポケモンいれかえ": "Switch", "夜のタンカ": "Night Stretcher",
    "スーパーエネルギー回収": "Superior Energy Retrieval", "カウンターキャッチャー": "Counter Catcher",
    "プライムキャッチャー": "Prime Catcher", "エネルギー回収": "Energy Retrieval",
    "ペパーの手伝い": "Arven's Help", "サワロ": "Saguaro", "アカマツ": "Crispin", "ブライア": "Briar",
    "ハイパーボール": "Ultra Ball", "ネストボール": "Nest Ball", "ふしぎなアメ": "Rare Candy",
    "大地の器": "Earthen Vessel", "ロストスイーパー": "Lost Vacuum", "すごいつりざお": "Super Rod",
    "テラスタルオーブ": "Tera Orb", "ポケモン回収サイクロン": "Pokémon Retrieval Cyclone",
}


@lru_cache(maxsize=1)
def _tables() -> tuple[dict[int, str], dict[str, str], dict[str, str]]:
    data = json.loads(_DATA.read_text(encoding="utf-8"))
    by_dex = {int(k): v[0] for k, v in data["species"].items()}
    ja_to_en = {v[1]: v[0] for v in data["species"].values() if v[1]}
    return by_dex, ja_to_en, data["items"]


def species_name(dex: int) -> str:
    return _tables()[0].get(int(dex), "")


def english_name(name_jp: str, dex_ids: list[int] | None = None, category: str = "") -> str:
    """Best-effort official English name for a Japanese card name ('' if unknown)."""
    name = (name_jp or "").strip()
    if not name:
        return ""
    by_dex, ja_to_en, items = _tables()

    if name in _TRAINERS:
        return _TRAINERS[name]
    if name in items:
        return items[name]
    energy = re.fullmatch(r"基本(.+)エネルギー", name)
    if energy and energy.group(1) in _ENERGY_TYPES:
        return f"Basic {_ENERGY_TYPES[energy.group(1)]} Energy"
    if category.lower() in {"trainer", "energy", "トレーナーズ", "エネルギー"}:
        return ""

    rest, suffix = name, ""
    for jp, en in _SUFFIXES:
        if rest.endswith(jp) and len(rest) > len(jp):
            rest, suffix = rest[: -len(jp)], en
            break
    form = ""
    for jp, en in _FORMS.items():
        if rest.endswith(jp) and len(rest) > len(jp):
            rest, form = rest[: -len(jp)].strip(), en + " "
            break
    owner = ""
    match = re.match(r"^(.+?)の(.+)$", rest)
    if match and match.group(1) in _OWNERS:
        owner, rest = f"{_OWNERS[match.group(1)]}'s ", match.group(2)

    def species(text: str) -> tuple[str, str]:
        """(English species, ' X'/' Y' form) for an exact Japanese species name, else ('', '')."""
        if text in ja_to_en:
            return ja_to_en[text], ""
        if text[-1:] in ("X", "Y") and text[:-1] in ja_to_en:  # Mega Charizard X / Y
            return ja_to_en[text[:-1]], " " + text[-1]
        return "", ""

    # Whole name first, so メガニウム stays Meganium rather than "Mega" + ニウム.
    rest = rest.strip()
    prefix, (base, xy) = "", species(rest)
    if not base:
        for jp, en in _PREFIXES:
            if rest.startswith(jp) and len(rest) > len(jp):
                base, xy = species(rest[len(jp):])
                if base:
                    prefix = en
                    break
    if not base and dex_ids:
        base = by_dex.get(int(dex_ids[0]), "")
    if not base:
        return ""
    return f"{owner}{form}{prefix}{base}{xy}{suffix}"


# --------------------------------------------------------------- name matching
_JAPANESE = re.compile(r"[぀-ヿ一-鿿]")
_SMALL_KANA = str.maketrans("ァィゥェォッャュョヮぁぃぅぇぉっゃゅょゎ", "アイウエオツヤユヨワあいうえおつやゆよわ")


def _kana(text: str) -> str:
    """Japanese letters only, small kana made full size, long-vowel marks dropped (OCR confuses
    them with frame lines), for comparing a card's name with OCR text."""
    import unicodedata
    text = unicodedata.normalize("NFKC", text or "").translate(_SMALL_KANA)
    return "".join(c for c in text if _JAPANESE.match(c) and c not in "ー一")


def _lcs(a: str, b: str) -> int:
    prev = [0] * (len(b) + 1)
    for ca in a:
        cur = [0]
        for j, cb in enumerate(b):
            cur.append(prev[j] + 1 if ca == cb else max(prev[j + 1], cur[j]))
        prev = cur
    return prev[-1]


def title_score(name_jp: str, texts: list[str]) -> float:
    """How much of the card's Japanese name appears, in order, in any of the OCR'd title texts
    (0..1). Extra characters in the OCR text don't count against it."""
    name = _kana(name_jp)
    if len(name) < 2:
        return 0.0
    return max((_lcs(name, _kana(t)) / len(name) for t in texts), default=0.0)


def title_readable(texts: list[str]) -> bool:
    """Whether the OCR'd title has enough Japanese letters to judge a name by."""
    return any(len(_kana(t)) >= 3 for t in texts)
