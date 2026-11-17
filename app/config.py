"""Application settings, read from environment variables (and an optional .env file)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader: KEY=VALUE lines, '#' comments. Real env vars win."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    app_password: str
    tcgdex_base_url: str
    card_language: str
    tesseract_cmd: str

    @property
    def db_path(self) -> Path:
        return self.data_dir / "cards.db"

    @property
    def photos_dir(self) -> Path:
        return self.data_dir / "photos"


def load_settings() -> Settings:
    _load_dotenv(BASE_DIR / ".env")
    data_dir = Path(os.getenv("DATA_DIR") or BASE_DIR / "data")
    if not data_dir.is_absolute():
        data_dir = (BASE_DIR / data_dir).resolve()
    return Settings(
        data_dir=data_dir,
        app_password=os.getenv("APP_PASSWORD") or "",
        tcgdex_base_url=(os.getenv("TCGDEX_BASE_URL") or "https://api.tcgdex.net/v2").rstrip("/"),
        card_language=os.getenv("CARD_LANGUAGE") or "ja",
        tesseract_cmd=os.getenv("TESSERACT_CMD") or "",
    )


settings = load_settings()
