from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    bot_token: str
    allowed_user_ids: frozenset[int]
    moderator_ids: frozenset[int]
    support_admin_ids: frozenset[int]
    database_path: Path
    log_path: Path
    inactive_profile_months: int
    technical_log_retention_days: int
    vocabulary_pass_threshold: float
    reminder_poll_seconds: int
    dataset_dir: Path = Path("data/english_cefr_datasets")
    enable_cefr_sp_noncommercial: bool = False


def _telegram_ids(value: str) -> frozenset[int]:
    try:
        return frozenset(int(item.strip()) for item in value.split(",") if item.strip())
    except ValueError as exc:
        raise ValueError("Telegram ID allowlists must contain comma-separated integers.") from exc


def _environment_flag(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    normalized = value.strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off", ""}:
        return False
    raise ValueError(f"{name} must be true or false.")


def load_settings(*, require_token: bool = True) -> Settings:
    load_dotenv()
    token = os.getenv("BOT_TOKEN", "").strip()
    if require_token and not token:
        raise ValueError("BOT_TOKEN is required. Copy .env.example to .env and add your BotFather token.")

    months = int(os.getenv("INACTIVE_PROFILE_MONTHS", "12"))
    log_days = int(os.getenv("TECHNICAL_LOG_RETENTION_DAYS", "30"))
    threshold = float(os.getenv("VOCABULARY_PASS_THRESHOLD", "0.7"))
    poll_seconds = int(os.getenv("REMINDER_POLL_SECONDS", "30"))
    if months < 1 or log_days < 1:
        raise ValueError("Profile and technical-log retention periods must be at least one.")
    if not 0 < threshold <= 1:
        raise ValueError("VOCABULARY_PASS_THRESHOLD must be greater than 0 and at most 1.")
    if poll_seconds < 5:
        raise ValueError("REMINDER_POLL_SECONDS must be at least 5.")

    return Settings(
        bot_token=token,
        allowed_user_ids=_telegram_ids(os.getenv("ALLOWED_USER_IDS", "")),
        moderator_ids=_telegram_ids(os.getenv("MODERATOR_IDS", "")),
        support_admin_ids=_telegram_ids(os.getenv("SUPPORT_ADMIN_IDS", "")),
        database_path=Path(os.getenv("DATABASE_PATH", "data/bot.sqlite3")),
        log_path=Path(os.getenv("LOG_PATH", "logs/bot.log")),
        inactive_profile_months=months,
        technical_log_retention_days=log_days,
        vocabulary_pass_threshold=threshold,
        reminder_poll_seconds=poll_seconds,
        dataset_dir=Path(os.getenv("CEFR_DATASET_DIR", "data/english_cefr_datasets")),
        enable_cefr_sp_noncommercial=_environment_flag("CEFR_SP_NONCOMMERCIAL_USE"),
    )
