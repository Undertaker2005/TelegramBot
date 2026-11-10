from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sys

from aiogram import Bot
from aiogram.types import FSInputFile, InputProfilePhotoStatic


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from telegram_bot.config import load_settings
from telegram_bot.bot import configure_commands


async def configure_profile() -> None:
    profile_path = ROOT / "content" / "bot_profile.json"
    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    settings = load_settings()
    bot = Bot(token=settings.bot_token)
    try:
        identity = await bot.get_me()
        if identity.id != profile["expected_bot_id"]:
            raise RuntimeError(
                "The configured BOT_TOKEN belongs to a different bot; no profile changes were made."
            )
        await bot.set_my_name(name=profile["name"])
        await bot.set_my_short_description(short_description=profile["about"])
        await bot.set_my_description(description=profile["description"])
        await bot.set_my_profile_photo(
            InputProfilePhotoStatic(
                photo=FSInputFile(ROOT / "assets" / "lingopilot-profile.jpg")
            )
        )
        await configure_commands(bot)
    finally:
        await bot.session.close()
    print("Bot name, profile photo, About, description, and private-chat command menu updated.")


if __name__ == "__main__":
    asyncio.run(configure_profile())
