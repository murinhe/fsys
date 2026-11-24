"""Application entrypoint for the Telegram File Manager bot."""

from __future__ import annotations

import logging

from telegram import BotCommand, Update
from telegram.ext import Application, ApplicationBuilder

from config import load_config
from database import Database
from handlers import register_handlers

logging.basicConfig(
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


async def _post_init(app: Application) -> None:
    db: Database = app.bot_data["db"]
    await db.init()
    await app.bot.set_my_commands(
        [
            BotCommand("start", "Open Root file explorer"),
            BotCommand("files", "Open explorer in current folder"),
            BotCommand("newfolder", "Create a new folder here"),
            BotCommand("search", "Search files by name or tag"),
            BotCommand("cancel", "Cancel current input flow"),
            BotCommand("help", "Show usage help"),
        ]
    )
    logger.info("Database initialized and bot commands registered.")


async def _post_shutdown(app: Application) -> None:
    db: Database = app.bot_data["db"]
    await db.close()
    logger.info("Database connection closed.")


def main() -> None:
    config = load_config()
    db = Database(config.db_path)

    app = (
        ApplicationBuilder()
        .token(config.bot_token)
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
        .build()
    )
    app.bot_data["config"] = config
    app.bot_data["db"] = db

    register_handlers(app)

    logger.info(
        "Starting Telegram File Manager bot (allowed users: %d, db: %s)",
        len(config.allowed_user_ids),
        config.db_path,
    )
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
