# Card Binder: Pokémon card inventory (Japanese and English)

Take a photo of a Pokémon card. The app reads the small code printed in the card's
bottom-left corner (for example `M6 058/076` or `MEG EN 179/132`), finds the exact card
in a free card database, and adds it to your collection with its Japanese name, English
name, official image and Cardmarket price. You can then browse your cards and rank them
by value.

**No AI and no API keys.** Text recognition runs on your own laptop or phone with
[Tesseract](https://github.com/tesseract-ocr/tesseract). It is one small Python process
with a SQLite file, and it can run on an
[Android phone](#run-it-on-an-android-phone-termux).

```
 browser (phone / laptop)                          uv run run.py (laptop or phone)
┌───────────────────────────────┐  card   ┌───────────────────────────────────────────┐
│ camera: find the card live,   │  image  │ Tesseract: read "M6 058/076" in the        │
│ outline it, straighten it     │ ──────▶ │   bottom-left strip                        │
│ Scan · Collection · Ranking   │ ◀────── │ TCGdex: name, image, set, Cardmarket price │
└───────────────────────────────┘  JSON   │ SQLite: data/cards.db + data/photos        │
                                          └───────────────────────────────────────────┘
```

## How the app finds the card and reads it

1. **Finding the card (in the browser, live).** About 10 times a second, the app takes a
   small copy of the camera frame and looks for an outline with the shape of a Pokémon
   card (63 × 88 mm). It finds the edges in the image, joins them into closed shapes, and
   keeps the one whose size, shape and straight sides match a card. This is classical
   image processing in plain JavaScript (`static/card-detector.js`): nothing to download,
   no model to train, and it runs on phones.
2. **The overlay.** The detected card gets an outline, amber while it moves and green
   when it is held still. A dashed strip shows the bottom-left area where the code is
   read. If the card is too cluttered or turned sideways, the app shows no outline rather
   than a wrong one.
3. **Capturing.** With **Auto capture** on, the photo is taken once the card is held
   still for about a second; otherwise press **Capture card**. The video is only
   sharp enough for the outline, so the app takes a full-resolution photo from the camera
   when the browser allows it (Chrome and Android; other browsers use the video frame).
   It then finds the card in that photo, cuts it out and straightens it, which corrects
   tilt and perspective. **Take photo** and **Upload photos** go through the same detection.
4. **Reading (on the server).** Tesseract reads the strip at the bottom left of the
   straightened card. It finds the number's box, re-reads it several ways (sizes, dark or
   light text, white digits with a dark outline on full-art cards) and takes the reading
   most attempts agree on. If the browser couldn't find the card, the server looks for it
   itself (same method, `app/detect.py`).
5. **Looking it up.** The set code and number find the exact card (next section). Japanese
   set codes are printed in a stylised font and often misread, so the **set size** is used
   too: `103` cards is M6a, `742` is MC, `193` is M2a.
6. **Checking the name.** The card's name (top left, Japanese) is read and compared with
   the matched card's name. A match confirms the card. If another reading's card matches
   instead, that one is used. If nothing matches, the name is looked up in the likely sets,
   which finds the card even when the number was misread. This needs the Japanese model:
   `uv run scripts/get_jpn_model.py` (about 14 MB, done by the Termux setup).

Uncertain reads are marked **Please check the number**, other readings that are also real
cards are offered as one-tap alternatives, and **Quick add** only saves confident reads.

One card per photo: if several codes are read, the first one found in the catalogue wins.

## How a card is identified

Every modern card prints the same kind of code in the bottom-left corner:

| Cards | Printed (example) | Meaning |
|---|---|---|
| English, Mega Evolution era | `I` `MEG EN` `179/132` | regulation mark, set code + language, number / set size |
| Japanese, Mega era: **Storm Emeralda** | `J` `M6` `058/076` `RR` | regulation mark, set code, number / set size, rarity |
| Japanese, Mega era: other sets | `M1L` `092/063` `MUR` | Mega Brave; secret rares go above the set size |
| Japanese, Scarlet & Violet era | `G` `SV2a` `025/165` `C` | Pokémon Card 151 |
| Japanese promos | `001/M-P` | promo number / promo series |

About Storm Emeralda (often called "Storm Emerald"): it is the Japanese set **M6**,
ストームエメラルダ, released 31 July 2026. It has 76 numbered cards plus secret rares up to
113 and uses regulation mark `J`. The English version is *Delta Reign* (printed code
`DLR`).

- **Japanese sets:** the printed code is the set's ID in the card database (M1L, M2,
  M2a, M3, M4, M5, M6, SV11B …). So `M6` + `058` points to exactly one card.
- **English sets:** the database stores the printed code (`MEG`) next to its own ID, and
  the set size (`132`) narrows the choice.
- **Misread set codes:** the reader tolerates common mistakes (`0`/`O`, `1`/`I`, `6`/`G`,
  a regulation mark stuck to the code). A misread code is accepted only if that set's size
  matches the printed size.
- **Language code:** English cards print `EN`. I couldn't confirm whether Japanese
  Mega-era cards print `JP`, so the reader works either way.

**Names:** Japanese cards get their English name without AI. The card database gives
the Pokédex number, which maps to the official English name (a bundled list from
[PokeAPI](https://github.com/PokeAPI/pokeapi)). Prefixes and suffixes are translated by
rule: メガレックウザex → *Mega Rayquaza ex*, アローラロコン → *Alolan Vulpix*,
リーリエのピッピex → *Lillie's Clefairy ex*. Items, energies and common trainer cards are
covered too (ネストボール → *Nest Ball*, 博士の研究 → *Professor's Research*). Anything unknown
is left blank for you to type.

**Prices:** the price used for value and ranking is **your own price** if you entered
one, otherwise the **Cardmarket price trend** from [TCGdex](https://tcgdex.dev). Every
update is kept in a price history. Cardmarket's own API
[doesn't accept new applications](https://help.cardmarket.com/en/cardmarket-api), so
TCGdex is the source. Japanese cards often have no price there. In that case, tap
**Check on Cardmarket** and type the price into **Your price**.

## Install Tesseract

| System | Command |
|---|---|
| macOS | `brew install tesseract` |
| Ubuntu / Debian | `sudo apt install tesseract-ocr` |
| Windows | Install from the [UB Mannheim builds](https://github.com/UB-Mannheim/tesseract/wiki), then set `TESSERACT_CMD=C:\Program Files\Tesseract-OCR\tesseract.exe` in `.env` if it isn't on your PATH |
| Android (Termux) | done by `termux-setup.sh` (`pkg install tesseract`) |

The English data (included) reads the code. For the name check on Japanese cards, also run
`uv run scripts/get_jpn_model.py` once: it downloads Tesseract's accurate Japanese model into
`./tessdata` (about 14 MB). The `jpn` language from your package manager works too, but is
less accurate.

## Quick start (laptop)

You need [uv](https://docs.astral.sh/uv/getting-started/installation/) and Tesseract. uv
installs Python 3.11+ for you if needed.

```bash
git clone <this repo> && cd pokemon-card-inventory
uv sync                            # creates .venv and installs the locked packages
cp .env.example .env               # optional: password, data folder
uv run scripts/get_jpn_model.py    # optional: name check for Japanese cards
uv run run.py
```

`run.py` prints the addresses to open:

```
  This device:    http://localhost:8000
  Other devices (same Wi-Fi): http://192.168.1.23:8000
```

- **On the laptop**, open `http://localhost:8000`, click **Start camera**, hold the card
  inside the frame and click **Capture card**. You can also use **Upload photos**, with
  several at once.
- **On your phone**, open the Wi-Fi address. **Take photo** opens the camera.

## Run it on an Android phone (Termux)

The whole app runs on the phone, with no internet needed except for the card database.

> **Test status:** the setup script was tested in a simulated Termux environment, not on
> a real phone.

1. **Install Termux** from [F-Droid](https://f-droid.org/packages/com.termux/) or the
   [official GitHub releases](https://github.com/termux/termux-app/releases). These are
   the sources the Termux maintainers support. Avoid APK mirror sites. The Google Play
   build is experimental, and mixing it with the others breaks updates.
2. **Get the code** into your Termux home folder:
   ```bash
   pkg install -y git
   git clone <this repo> ~/pokemon-card-inventory
   ```
3. **Run the setup** (one time):
   ```bash
   cd ~/pokemon-card-inventory
   bash termux-setup.sh
   ```
   It installs Python, Tesseract and a prebuilt Pillow, then compiles one package
   (`pydantic-core`). **That takes 5–15 minutes.** Keep Termux open and the phone
   plugged in.

   It also downloads the Japanese model used to check card names (about 14 MB).
4. **Start the app** whenever you want to use it:
   ```bash
   cd ~/pokemon-card-inventory && source .venv/bin/activate && python run.py
   ```
   Then open `http://localhost:8000` in Chrome on the phone. The live camera preview
   works here, because the browser treats `localhost` as secure.

If the compile step fails, run
`bash termux-setup.sh` again with `CARGO_BUILD_TARGET=aarch64-linux-android` set:
`CARGO_BUILD_TARGET=aarch64-linux-android bash termux-setup.sh`.
If it still fails, keep the error message to ask for help.

**Keeping it safe on the phone**
- **Only the phone can open the app.** Inside Termux, `run.py` listens on `127.0.0.1`
  by default, so other devices on the Wi-Fi can't reach it. `--host 0.0.0.0` shares it on
  your Wi-Fi. Set `APP_PASSWORD` in `.env` before doing that, and don't use public Wi-Fi.
- **Keep the folder in Termux's home (`~`), not in shared storage (`/sdcard`).** Other
  apps can't read Termux's home. The setup script refuses to run from shared storage.
- **Back up your collection.** Uninstalling Termux deletes it. To copy the collection to
  the phone's shared storage, run `termux-setup-storage` once, then
  `cp -r ~/pokemon-card-inventory/data ~/storage/shared/card-binder-backup`.
- **Keep Termux updated** with `pkg upgrade`. If Android stops the server in the
  background, turn off battery optimisation for Termux or run `termux-wake-lock`.

## Pack-opening workflow

1. Open **Scan** and press **Start camera**. Leave **Auto capture** on, and turn on
   **Quick add** if you want every card found in the catalogue saved straight away (with
   **Undo**).
2. Put the first card on the table under the camera and hold still until the outline
   turns green. When it is found, its set (e.g. `M6`) fills in **Set of the packs you're
   opening**. Swap in the next card: a new capture happens when the card changes.
3. For the following cards, the number is usually enough. If the set code is
   unreadable, for example because of glare on a holo card, the pack set is used. If the
   photo can't be read at all, type the number (`058`) and press **Find card**. The photo
   you just took is attached to the card.
4. Duplicates are detected. **Add copy (have ×2)** increases the quantity instead of
   adding a duplicate row. Reverse-holo and Poké Ball/Master Ball mirror versions count as
   separate cards; choose the **Finish** before adding.
5. **This session** lists what you pulled and what it is worth.

**Photo tips:** use a plain table that contrasts with the card's border, with nothing
touching the card. Keep it upright (portrait) and large in the frame, with the dashed strip
sharp. Tilt the card slightly to move glare away from that corner. Reading takes about
1–4 seconds on a laptop, and longer on a phone.

## Collection and ranking

- **Collection:** a grid with the official image or your photo, English and Japanese
  names, set, number, rarity, copies and price. You can search in English or Japanese and
  sort by newest, price, total value, name, or set. Tap a card to edit it, change the
  number of copies, set your own price, paste its exact Cardmarket URL, update its price,
  re-match it with the catalogue, view its price history or delete it.
- **Ranking:** shows the collection value and card counts, and ranks cards by **card
  price** or by **price × copies**.
- **Export CSV** downloads everything as a spreadsheet. Japanese text opens correctly in
  Excel.

## Configuration (`.env`)

| Variable | Default | What it does |
|---|---|---|
| `APP_PASSWORD` | empty | When set, the browser asks for a password (any username). Set it before sharing. |
| `DATA_DIR` | `./data` | Where `cards.db` and your photos are stored. |
| `CARD_LANGUAGE` | `ja` | Catalogue tried first when a card doesn't print its language (`ja` or `en`). |
| `TESSERACT_CMD` | auto | Path to the `tesseract` program, if it's not on your PATH. |
| `TCGDEX_BASE_URL` | TCGdex | Catalogue endpoint. |

## Limits

- **Real cards are untested.** Detection and reading were tuned on drawn cards in
  simulated photos and a simulated camera feed:
  - **Finding the card:** found in all 7 normal scenes, with corners within 1–2%
    (upright, tilted, small, rotated 20°, dark or white table, blurry, webcam). It showed
    no outline for a card cut off by the frame edge, one in heavy clutter, and one turned
    sideways.
  - **Reading:** the number and set size in 55 of 56 photos. English set codes were
    always read. Japanese set codes printed white-on-black were often misread; the pack
    set and the set-size check cover that case.
  - **Live video:** a 1080p frame is the minimum for reading; the full-resolution photo
    is used where the browser allows it.

  Real cards may read better or worse.
- **New sets** appear in TCGdex shortly after release, and prices later. Until then a
  card is "not matched": add it with a typed name and **Re-match** it later.
- **Finish** (reverse holo, ball mirror) isn't visible in the code, so you choose it.

## Sharing with others

The data lives on the device that runs `run.py`.

1. **Same Wi-Fi:** share the `http://192.168.x.x:8000` address (set `APP_PASSWORD`).
2. **Temporary public HTTPS link (no account):** set `APP_PASSWORD`, then run
   [cloudflared](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/do-more-with-tunnels/trycloudflare/)
   `tunnel --url http://localhost:8000`. It prints an `https://….trycloudflare.com` link
   that works until you stop it.
3. **Always on:** run the app on any small Python host (a Raspberry Pi, a VPS, or a
   service with a persistent disk at `DATA_DIR`) with Tesseract installed. Start command:
   `uvicorn app.main:app --host 0.0.0.0 --port $PORT`.

## Backups

Everything is in `DATA_DIR`: `cards.db` (SQLite) and `photos/`. To back up, copy that
folder.

## Development

```bash
uv sync                      # includes the dev group (pytest) by default
uv run pytest -q             # OCR tests run for real when Tesseract is installed
uv run run.py --reload
uv run scripts/build_names.py   # refresh the Japanese→English name list from PokeAPI
```

```
app/
  main.py          FastAPI routes, background price refresh, CSV export, password gate
  reader.py        photo -> card identifier and name: Tesseract, voting, parsing
  detect.py        finds the card in a photo (lines -> card-shaped outline), numpy only
  catalog.py       TCGdex: printed set code -> set, card, names, image, Cardmarket price
  names.py         Japanese -> English names (Pokédex number, prefixes/suffixes, items, trainers)
  data/names.json  Pokémon and item names from PokeAPI (BSD-3-Clause)
  db.py            SQLite schema and queries (cards, price history, stats)
  config.py        settings from environment / .env
static/            single-page UI (plain HTML/CSS/JS, no build step)
  card-detector.js live card detection, overlay geometry and straightening (no libraries)
tests/             pytest suite
run.py             starts the server and prints the addresses to open
termux-setup.sh    one-time setup on an Android phone (Termux)
scripts/           build_names.py, get_jpn_model.py (Japanese model for the name check)
```

Main endpoints: `POST /api/scan?set_hint=M6&card=1` (photo or straightened card → card), `POST /api/lookup`
(set code + number → card), `POST /api/cards`, `GET /api/cards?sort=value|total|newest|name|set&q=`,
`PATCH/DELETE /api/cards/{id}`, `POST /api/cards/{id}/price`, `POST /api/cards/{id}/rematch`,
`POST /api/prices/refresh`, `GET /api/stats`, `GET /api/export.csv`.
