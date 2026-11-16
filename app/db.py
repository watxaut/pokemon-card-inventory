"""SQLite storage for the card inventory. One row per distinct card, with a quantity."""

from __future__ import annotations

import re
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

_SCHEMA = """
CREATE TABLE IF NOT EXISTS cards (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    name_jp          TEXT NOT NULL DEFAULT '',
    name_en          TEXT NOT NULL DEFAULT '',
    set_code         TEXT NOT NULL DEFAULT '',
    set_name         TEXT NOT NULL DEFAULT '',
    number           TEXT NOT NULL DEFAULT '',
    set_total        TEXT NOT NULL DEFAULT '',
    rarity           TEXT NOT NULL DEFAULT '',
    finish           TEXT NOT NULL DEFAULT 'normal',
    category         TEXT NOT NULL DEFAULT '',
    language         TEXT NOT NULL DEFAULT 'ja',
    tcgdex_id        TEXT,
    image_url        TEXT,
    photo            TEXT,
    quantity         INTEGER NOT NULL DEFAULT 1,
    notes            TEXT NOT NULL DEFAULT '',
    price_eur        REAL,
    price_trend      REAL,
    price_avg        REAL,
    price_low        REAL,
    price_avg30      REAL,
    price_source     TEXT,
    price_note       TEXT,
    price_url        TEXT,
    price_updated_at TEXT,
    manual_price     REAL,
    cardmarket_url   TEXT,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS price_history (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    card_id    INTEGER NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
    price_eur  REAL NOT NULL,
    source     TEXT NOT NULL DEFAULT '',
    fetched_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_history_card ON price_history(card_id, fetched_at);
"""

# Columns a client may set directly (create / update).
EDITABLE_FIELDS = (
    "name_jp", "name_en", "set_code", "set_name", "number", "set_total", "rarity",
    "finish", "category", "language", "tcgdex_id", "image_url", "photo", "quantity",
    "notes", "manual_price", "cardmarket_url",
)
PRICE_FIELDS = (
    "price_eur", "price_trend", "price_avg", "price_low", "price_avg30",
    "price_source", "price_note", "price_url", "price_updated_at",
)

# The price used for value and ranking: your own price wins over the market price.
VALUE_SQL = "COALESCE(manual_price, price_eur)"

SORTS = {
    "newest": "created_at DESC, id DESC",
    "oldest": "created_at ASC, id ASC",
    "value": f"{VALUE_SQL} IS NULL, {VALUE_SQL} DESC, id DESC",
    "total": f"{VALUE_SQL} IS NULL, {VALUE_SQL} * quantity DESC, id DESC",
    "name": "LOWER(COALESCE(NULLIF(name_en, ''), name_jp)) ASC",
    "set": "LOWER(set_code) ASC, CAST(number AS INTEGER) ASC, number ASC",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def normalize_number(number: str | None) -> str:
    """'025/165' -> '25', '025' -> '25', 'SV-P 001' -> 'svp1'. Used to compare card numbers."""
    if not number:
        return ""
    head = str(number).split("/")[0].strip().lower()
    head = re.sub(r"[^0-9a-z]", "", head)
    return re.sub(r"(?<![0-9])0+(?=[0-9])", "", head)


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _query(self, sql: str, params: Iterable[Any] = ()) -> list[dict]:
        with self._connect() as conn:
            return [_row(r) for r in conn.execute(sql, tuple(params)).fetchall()]

    # ------------------------------------------------------------------ cards
    def create_card(self, fields: dict) -> dict:
        data = {k: fields[k] for k in (*EDITABLE_FIELDS, *PRICE_FIELDS) if k in fields}
        data.setdefault("quantity", 1)
        data["created_at"] = data["updated_at"] = now_iso()
        cols = ", ".join(data)
        marks = ", ".join("?" for _ in data)
        with self._lock, self._connect() as conn:
            cur = conn.execute(f"INSERT INTO cards ({cols}) VALUES ({marks})", tuple(data.values()))
            card_id = cur.lastrowid
            if data.get("price_eur") is not None:
                conn.execute(
                    "INSERT INTO price_history (card_id, price_eur, source, fetched_at) VALUES (?, ?, ?, ?)",
                    (card_id, data["price_eur"], data.get("price_source") or "", data["created_at"]),
                )
        return self.get_card(card_id)

    def update_card(self, card_id: int, fields: dict) -> dict | None:
        data = {k: v for k, v in fields.items() if k in (*EDITABLE_FIELDS, *PRICE_FIELDS)}
        if not data:
            return self.get_card(card_id)
        data["updated_at"] = now_iso()
        assignments = ", ".join(f"{k} = ?" for k in data)
        with self._lock, self._connect() as conn:
            conn.execute(f"UPDATE cards SET {assignments} WHERE id = ?", (*data.values(), card_id))
        return self.get_card(card_id)

    def set_price(self, card_id: int, price: dict) -> dict | None:
        """Store a market price result and append it to the price history."""
        fields = {k: price.get(k) for k in PRICE_FIELDS}
        fields["price_updated_at"] = fields.get("price_updated_at") or now_iso()
        card = self.update_card(card_id, fields)
        if card and price.get("price_eur") is not None:
            with self._lock, self._connect() as conn:
                conn.execute(
                    "INSERT INTO price_history (card_id, price_eur, source, fetched_at) VALUES (?, ?, ?, ?)",
                    (card_id, price["price_eur"], price.get("price_source") or "", now_iso()),
                )
        return card

    def change_quantity(self, card_id: int, delta: int) -> dict | None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE cards SET quantity = MAX(0, quantity + ?), updated_at = ? WHERE id = ?",
                (delta, now_iso(), card_id),
            )
        return self.get_card(card_id)

    def delete_card(self, card_id: int) -> bool:
        with self._lock, self._connect() as conn:
            return conn.execute("DELETE FROM cards WHERE id = ?", (card_id,)).rowcount > 0

    def get_card(self, card_id: int) -> dict | None:
        rows = self._query(f"SELECT *, {VALUE_SQL} AS value FROM cards WHERE id = ?", (card_id,))
        return rows[0] if rows else None

    def list_cards(self, q: str = "", sort: str = "newest", limit: int | None = None) -> list[dict]:
        sql = f"SELECT *, {VALUE_SQL} AS value FROM cards"
        params: list[Any] = []
        if q:
            like = f"%{q.strip().lower()}%"
            sql += (
                " WHERE LOWER(name_en) LIKE ? OR name_jp LIKE ? OR LOWER(set_code) LIKE ?"
                " OR LOWER(set_name) LIKE ? OR number LIKE ? OR LOWER(rarity) LIKE ?"
            )
            params += [like, f"%{q.strip()}%", like, like, f"%{q.strip()}%", like]
        sql += f" ORDER BY {SORTS.get(sort, SORTS['newest'])}"
        if limit:
            sql += " LIMIT ?"
            params.append(int(limit))
        return self._query(sql, params)

    def find_duplicate(self, *, tcgdex_id: str | None, set_code: str, number: str, finish: str) -> dict | None:
        """An existing row for the same printed card (same set + number + finish)."""
        finish = finish or "normal"
        if tcgdex_id:
            rows = self._query(
                f"SELECT *, {VALUE_SQL} AS value FROM cards WHERE tcgdex_id = ? AND finish = ? LIMIT 1",
                (tcgdex_id, finish),
            )
            if rows:
                return rows[0]
        wanted = normalize_number(number)
        if not set_code or not wanted:
            return None
        rows = self._query(
            f"SELECT *, {VALUE_SQL} AS value FROM cards WHERE LOWER(set_code) = ? AND finish = ?",
            (set_code.strip().lower(), finish),
        )
        return next((r for r in rows if normalize_number(r["number"]) == wanted), None)

    def photos_in_use(self) -> set[str]:
        return {r["photo"] for r in self._query("SELECT DISTINCT photo FROM cards WHERE photo IS NOT NULL")}

    def price_history(self, card_id: int) -> list[dict]:
        return self._query(
            "SELECT price_eur, source, fetched_at FROM price_history WHERE card_id = ? ORDER BY fetched_at ASC, id ASC",
            (card_id,),
        )

    def stats(self) -> dict:
        row = self._query(
            f"""SELECT COUNT(*) AS unique_cards,
                       COALESCE(SUM(quantity), 0) AS total_cards,
                       COALESCE(SUM({VALUE_SQL} * quantity), 0) AS total_value,
                       SUM(CASE WHEN {VALUE_SQL} IS NOT NULL THEN 1 ELSE 0 END) AS priced_cards
                FROM cards"""
        )[0]
        row["priced_cards"] = row["priced_cards"] or 0
        row["total_value"] = round(row["total_value"] or 0, 2)
        return row


def _row(row: sqlite3.Row) -> dict:
    return {k: row[k] for k in row.keys()}
