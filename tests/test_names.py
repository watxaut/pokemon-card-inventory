import pytest

from app.names import english_name, species_name


@pytest.mark.parametrize("jp, dex, expected", [
    ("ピカチュウ", None, "Pikachu"),
    ("メガレックウザex", [384], "Mega Rayquaza ex"),
    ("メガリザードンXex", [6], "Mega Charizard X ex"),
    ("リザードンex", None, "Charizard ex"),
    ("メガニウム", None, "Meganium"),                 # not "Mega" + something
    ("ダークライ", None, "Darkrai"),
    ("アローラロコン", None, "Alolan Vulpix"),
    ("ガラルポニータ", None, "Galarian Ponyta"),
    ("リーリエのピッピex", None, "Lillie's Clefairy ex"),
    ("ロケット団のミュウツーex", None, "Team Rocket's Mewtwo ex"),
    ("ルカリオVSTAR", None, "Lucario VSTAR"),
    ("ゲンガーVMAX", None, "Gengar VMAX"),
    ("ミュウV", None, "Mew V"),
    ("なぞのポケモンex", [1025], "Pecharunt ex"),       # unknown wording: fall back to the Pokédex number
    ("オーガポン かまどのめんex", [1017], "Hearthflame Mask Ogerpon ex"),
    ("オーガポンかまどのめんex", [1017], "Hearthflame Mask Ogerpon ex"),
    ("ゼクロム", [644], "Zekrom"),
    ("ディアルガ", [483], "Dialga"),
])
def test_pokemon_names(jp, dex, expected):
    assert english_name(jp, dex) == expected


@pytest.mark.parametrize("jp, expected", [
    ("博士の研究", "Professor's Research"),
    ("ボスの指令", "Boss's Orders"),
    ("ネストボール", "Nest Ball"),
    ("ふしぎなアメ", "Rare Candy"),
    ("げんきのかけら", "Revive"),                       # from the PokeAPI item list
    ("基本炎エネルギー", "Basic Fire Energy"),
    ("基本超エネルギー", "Basic Psychic Energy"),
])
def test_trainer_and_energy_names(jp, expected):
    assert english_name(jp, None, "Trainer") == expected


def test_unknown_names_are_left_blank():
    assert english_name("知らないトレーナー", None, "Trainer") == ""
    assert english_name("", [25]) == ""
    assert english_name("ナニカ", None) == ""


def test_species_name():
    assert species_name(25) == "Pikachu" and species_name(9999) == ""
