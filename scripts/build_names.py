#!/usr/bin/env python3
"""Rebuild app/data/names.json (Japanese -> English Pokémon and item names) from PokeAPI.

    python scripts/build_names.py            # downloads the CSVs from GitHub
    python scripts/build_names.py CSV_DIR    # or reads pokemon_species_names.csv / item_names.csv from a folder

Data: https://github.com/PokeAPI/pokeapi (BSD-3-Clause). Run again when new Pokémon are released.
"""

from __future__ import annotations

import csv
import io
import json
import sys
import urllib.request
from pathlib import Path

SOURCE = "https://raw.githubusercontent.com/PokeAPI/pokeapi/master/data/v2/csv/"
OUT = Path(__file__).resolve().parent.parent / "app" / "data" / "names.json"
JA_KANA, JA, EN = "1", "11", "9"   # PokeAPI language ids


def load(name: str, folder: Path | None) -> list[dict]:
    if folder:
        text = (folder / name).read_text(encoding="utf-8")
    else:
        with urllib.request.urlopen(SOURCE + name, timeout=60) as resp:  # noqa: S310 - fixed https URL
            text = resp.read().decode("utf-8")
    return list(csv.DictReader(io.StringIO(text)))


def main() -> None:
    folder = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    species: dict[str, dict[str, str]] = {}
    for row in load("pokemon_species_names.csv", folder):
        species.setdefault(row["pokemon_species_id"], {})[row["local_language_id"]] = row["name"]
    items: dict[str, dict[str, str]] = {}
    for row in load("item_names.csv", folder):
        items.setdefault(row["item_id"], {})[row["local_language_id"]] = row["name"]

    out = {
        "source": "PokeAPI (https://github.com/PokeAPI/pokeapi), BSD-3-Clause",
        # dex number -> [English, Japanese (kana)]
        "species": {k: [v[EN], v.get(JA_KANA) or v.get(JA, "")]
                    for k, v in sorted(species.items(), key=lambda kv: int(kv[0])) if EN in v},
        # Japanese item name -> English
        "items": {},
    }
    for names in items.values():
        if EN not in names:
            continue
        for lang in (JA_KANA, JA):
            if names.get(lang):
                out["items"].setdefault(names[lang], names[EN])
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"Wrote {OUT} ({len(out['species'])} species, {len(out['items'])} item names)")


if __name__ == "__main__":
    main()
