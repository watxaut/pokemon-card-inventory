# Card Binder: Japanese Pokémon card inventory

Point your phone or laptop camera at a Japanese Pokémon card. The app reads it, adds it
to your collection and looks up its Cardmarket price. You can then browse every card
(Japanese name, English name, your photo, the official image, price) and see a ranking
of your most valuable cards.

It is one small Python process with a SQLite file. There is no cloud setup and no
accounts. Run it on your laptop and open it on your phone over Wi-Fi, or
[run it on the phone itself](#run-it-on-an-android-phone-termux).

```
 browser (phone / laptop)                     python run.py (laptop or phone)
┌──────────────────────┐   photo    ┌────────────────────────────────────────────┐
│ Scan · Collection ·  │ ─────────▶ │ FastAPI ──▶ Claude (vision): reads name,   │
│ Ranking              │ ◀───────── │   │         set code, number, rarity,      │
└──────────────────────┘   JSON     │   │         English name                   │
                                    │   ├──▶ TCGdex: official image, set name,   │
                                    │   │          Cardmarket EUR prices         │
                                    │   └──▶ SQLite  data/cards.db + data/photos │
                                    └────────────────────────────────────────────┘
```

## How it works

1. **Recognition: Claude vision.** The photo is resized and sent to Claude with a strict
   JSON schema. Claude returns the name as printed (katakana/hiragana/kanji), the official
   English TCG name (e.g. ピカチュウex → *Pikachu ex*, ボスの指令 → *Boss's Orders*), the
   set code and collector number from the bottom-left corner (e.g. `SV2a` · `025/165`),
   rarity and finish. Several cards in one photo also work, but one card per photo is the
   most reliable.
2. **Catalogue: [TCGdex](https://tcgdex.dev).** TCGdex is a free, open Pokémon TCG
   database with Japanese sets and no API key. The app matches the set code and number
   (falling back to the Japanese name) to get the official card image, the set name, and
   Cardmarket price data when TCGdex has it.
3. **Price: Cardmarket, in EUR.** The price used for value and ranking is **your own
   price** if you entered one, otherwise the **Cardmarket price trend** from TCGdex (the
   30-day average, average and lowest price are stored too). Every price update is kept
   in a price history, so you can track value over time. **Refresh prices** updates the
   whole collection from TCGdex at no cost.

### Why not the official Cardmarket API?

Cardmarket's own API is
[not accepting new applications](https://help.cardmarket.com/en/cardmarket-api), and it
was limited to professional sellers before that, so the app gets Cardmarket data through
TCGdex instead.

**Japanese cards:** TCGdex's Cardmarket prices are most complete for English sets, so
many Japanese cards will have **no price**. For those, tap **Check on Cardmarket**, look
up the price, and type it into **Your price**. That field is on the scan screen and on
each card.

## Quick start (laptop)

You need Python 3.10+ and a Claude API key from <https://platform.claude.com>.

```bash
git clone <this repo> && cd pokemon-card-inventory
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env               # then edit .env and paste your ANTHROPIC_API_KEY
python run.py
```

`run.py` prints two addresses:

```
  This device:    http://localhost:8000
  Other devices (same Wi-Fi): http://192.168.1.23:8000
```

- **On the laptop**, open `http://localhost:8000`, click **Start camera** and hold the
  card inside the frame. Then click **Capture card**. You can also use **Upload photos**
  to add photos you already took (you can select several at once).
- **On your phone**, open the Wi-Fi address. **Take photo** opens the phone camera, which
  works over plain `http`. A live preview inside the page needs HTTPS (see *Sharing*
  below), but you don't need it.

## Run it on an Android phone (Termux)

The whole app can run on the phone. Reading the card happens on Claude's servers, so the
phone only runs a small web server and a SQLite file. Battery and storage use are small.

> **Test status:** the setup script was tested in a simulated Termux environment and the
> app on Python 3.14 (the version Termux ships), but not on a real phone.

1. **Install Termux** from [F-Droid](https://f-droid.org/packages/com.termux/) or the
   [official GitHub releases](https://github.com/termux/termux-app/releases). These are
   the sources the Termux maintainers support. Avoid APK mirror sites. The Google Play
   build is experimental, and mixing it with the others breaks updates.
2. **Get the code** in Termux, in your Termux home folder:
   ```bash
   pkg install -y git
   git clone <this repo> ~/pokemon-card-inventory
   ```
3. **Run the setup script** (one time):
   ```bash
   cd ~/pokemon-card-inventory
   bash termux-setup.sh
   ```
   It installs Python, a prebuilt Pillow and the Rust compiler, then compiles two
   packages (`pydantic-core`, `jiter`) that have no ready-made Android build. **This takes
   10–30 minutes.** Keep Termux open and the phone plugged in. At the end it asks for your
   Claude API key and stores it in `.env`.
4. **Start the app** whenever you want to use it:
   ```bash
   cd ~/pokemon-card-inventory && source .venv/bin/activate && python run.py
   ```
   Then open `http://localhost:8000` in Chrome on the phone. The live camera preview
   works here, because the browser treats `localhost` as secure.

If step 3 fails while compiling, run this inside the project folder and include the error
if you ask for help:
`source .venv/bin/activate && CARGO_BUILD_TARGET=aarch64-linux-android python -m pip install -r requirements.txt`.

### Keeping it safe on the phone

- **Only the phone can open the app.** Inside Termux, `run.py` listens on `127.0.0.1`
  by default, so other devices on the Wi-Fi can't reach it. If you want to share it,
  `python run.py --host 0.0.0.0` opens it to your Wi-Fi. Set `APP_PASSWORD` in `.env`
  first, and don't do this on public Wi-Fi.
- **Keep the folder in Termux's home (`~`), not in shared storage (`/sdcard`).** Termux's
  home is private to the Termux app, so other apps can't read your API key or collection.
  The setup script refuses to run from shared storage and makes `.env` readable only by
  Termux.
- **Protect the API key.** Create a separate key for this app and set a monthly spend
  limit in the Claude Console. If the phone is lost, revoke the key there.
- **Back up your collection.** Uninstalling Termux deletes everything in it. To copy the
  collection (not the key) to the phone's shared storage:
  ```bash
  termux-setup-storage                 # one time: allow access to shared storage
  cp -r ~/pokemon-card-inventory/data ~/storage/shared/card-binder-backup
  ```
- **Keep Termux updated:** run `pkg upgrade` from time to time.
- **If the server stops in the background:** Android may stop apps it considers idle.
  Leave the Termux notification in place, turn off battery optimisation for Termux in
  Android settings, or run `termux-wake-lock` before a long scanning session.

## Pack-opening workflow

1. Open the **Scan** tab and turn on **Quick add**. Cards read with high confidence that
   are found in the catalogue are saved immediately, with an **Undo** button.
2. Photograph each card. Anything uncertain (glare, unreadable number, no catalogue
   match) stays on screen so you can fix the set code or number. Then press
   **Re-match** and **Add**.
3. Duplicates are detected. The app offers **Add copy (have ×2)** so your binder keeps
   quantities instead of duplicate rows. Reverse-holo and Poké Ball/Master Ball mirror
   versions count as separate cards.
4. The **This session** list shows what you pulled and what it is worth.

Scanning tips: lay the card flat, avoid glare on the bottom-left corner, and fill the
frame.

## Collection and ranking

- **Collection:** a grid with the official image or your photo, English and Japanese
  names, set, number, rarity, copies and price. You can search in English or Japanese and
  sort by newest, price, total value, name, or set. Tap a card to edit it, change the
  number of copies, set your own price, paste the exact Cardmarket page URL, update its
  price, re-match it with the catalogue, view its price history or delete it.
- **Ranking:** shows the collection value, card counts and how many cards have a price,
  and ranks cards by **card price** or by **price × copies**.
- **Export CSV** downloads everything as a spreadsheet. Japanese text opens correctly in
  Excel.

## Configuration (`.env`)

| Variable | Default | What it does |
|---|---|---|
| `ANTHROPIC_API_KEY` | none | Required for card recognition. |
| `CLAUDE_MODEL` | `claude-opus-5-5` | Model used to read cards. Opus reads small Japanese print best. `claude-haiku-5-5` is much cheaper. |
| `CLAUDE_EFFORT` | `medium` | `low` / `medium` / `high`: how much the model reasons before answering. |
| `APP_PASSWORD` | empty | When set, the browser asks for a password (any username). Set it before sharing. |
| `DATA_DIR` | `./data` | Where `cards.db` and your photos are stored. |
| `TCGDEX_BASE_URL` / `CARD_LANGUAGE` | TCGdex / `ja` | Catalogue endpoint and language. |

The app opts in to Claude's server-side refusal fallback (`fallbacks: "default"`). If
Claude declines a request (rare), the API retries it on another model instead of failing
the scan. To stop that, remove `fallback_kwargs` in `app/recognizer.py`.

**Cost:** each scan sends about 3K input tokens (photo plus instructions) and gets a few
hundred to ~1K output tokens back. That is roughly **2–3 cents per card on Opus**, or
**well under a cent on Haiku**. Check current
[Claude pricing](https://platform.claude.com/docs/en/about-claude/pricing). TCGdex is free.

## Sharing with others

The data lives on the device that runs `run.py`. Options from least to most setup:

1. **Same Wi-Fi:** share the `http://192.168.x.x:8000` address (set `APP_PASSWORD`).
2. **Temporary public HTTPS link (no account):** set `APP_PASSWORD` in `.env`, then run
   [cloudflared](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/do-more-with-tunnels/trycloudflare/):
   ```bash
   cloudflared tunnel --url http://localhost:8000
   ```
   It prints an `https://….trycloudflare.com` link. Anyone with the link and the
   password can use the app, including the live camera preview on phones, because the
   link is HTTPS. The link stops working when you stop the command.
3. **Always on:** run the same app on any small Python host (a Raspberry Pi, a VPS, Fly.io
   or Render with a persistent disk mounted at `DATA_DIR`). Start command:
   `uvicorn app.main:app --host 0.0.0.0 --port $PORT`.

For HTTPS on your own network (live preview on other phones without a tunnel), create a
certificate with [mkcert](https://github.com/FiloSottile/mkcert) and run
`python run.py --ssl-certfile cert.pem --ssl-keyfile key.pem`.

## Backups

Everything is in `DATA_DIR`: `cards.db` (SQLite) and `photos/`. To back up, copy that
folder.

## Development

```bash
pip install -r requirements-dev.txt
python -m pytest -q
python run.py --reload
```

The tests use fakes for Claude and TCGdex, so they need no network or API key.
Interactive API docs are at `/api/docs`.

```
app/
  main.py          FastAPI routes, background price refresh, CSV export, password gate
  recognizer.py    Claude vision call (JSON schema) and image preparation
  catalog.py       TCGdex lookups: set/number matching, images, Cardmarket prices and links
  db.py            SQLite schema and queries (cards, price history, stats)
  config.py        settings from environment / .env
static/            single-page UI (plain HTML/CSS/JS, no build step)
tests/             pytest suite
run.py             starts the server and prints the addresses to open
termux-setup.sh    one-time setup on an Android phone (Termux)
```

Main endpoints: `POST /api/scan` (photo → recognised cards), `POST /api/cards`,
`GET /api/cards?sort=value|total|newest|name|set&q=`, `PATCH/DELETE /api/cards/{id}`,
`POST /api/cards/{id}/price`, `POST /api/cards/{id}/rematch`, `POST /api/prices/refresh`,
`GET /api/stats`, `GET /api/export.csv`.
