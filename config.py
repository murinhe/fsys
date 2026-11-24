"""Configuration loader for the Telegram File Manager bot."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True, slots=True)
class Config:
    """Immutable runtime configuration loaded from environment variables."""

    bot_token: str
    allowed_user_ids: frozenset[int]
    db_path: Path
    page_size: int = 8


def _parse_allowed_user_ids(raw: str) -> frozenset[int]:
    """Parse a comma-separated list of Telegram numeric user IDs."""
    tokens = [part.strip() for part in raw.split(",") if part.strip()]
    if not tokens:
        raise ValueError(
            "ALLOWED_USER_IDS must contain at least one Telegram numeric user ID."
        )

    user_ids: set[int] = set()
    for token in tokens:
        try:
            uid = int(token)
        except ValueError as exc:
            raise ValueError(
                f"Invalid user ID '{token}' in ALLOWED_USER_IDS; expected integer."
            ) from exc
        if uid <= 0:
            raise ValueError(
                f"Invalid user ID '{uid}' in ALLOWED_USER_IDS; must be positive."
            )
        user_ids.add(uid)
    return frozenset(user_ids)


def load_config(env_path: str | Path | None = None) -> Config:
    """Load and validate configuration from .env and process environment."""
    load_dotenv(dotenv_path=env_path)

    bot_token = os.getenv("BOT_TOKEN", "").strip()
    if not bot_token:
        raise ValueError("BOT_TOKEN is required and cannot be empty.")

    allowed_raw = os.getenv("ALLOWED_USER_IDS", "").strip()
    allowed_user_ids = _parse_allowed_user_ids(allowed_raw)

    db_path_raw = os.getenv("DB_PATH", "data/filebot.db").strip() or "data/filebot.db"
    db_path = Path(db_path_raw)

    return Config(
        bot_token=bot_token,
        allowed_user_ids=allowed_user_ids,
        db_path=db_path,
    )
