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
    prefix, (base, form) = "", species(rest)
    if not base:
        for jp, en in _PREFIXES:
            if rest.startswith(jp) and len(rest) > len(jp):
                base, form = species(rest[len(jp):])
                if base:
                    prefix = en
                    break
    if not base and dex_ids:
        base = by_dex.get(int(dex_ids[0]), "")
    if not base:
        return ""
    return f"{owner}{prefix}{base}{form}{suffix}"
