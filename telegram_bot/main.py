from __future__ import annotations

import argparse
import asyncio
import json
import logging
import tempfile
from datetime import UTC, datetime
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

from aiogram import Bot, Dispatcher

from telegram_bot.bot import configure_commands, reminder_worker, register_handlers
from telegram_bot.config import Settings, load_settings
from telegram_bot.content import validate_course
from telegram_bot.database import Database
from telegram_bot.logic import learning_plan, reminder_is_due, score_placement
from telegram_bot.open_dataset import OpenDataset
from telegram_bot.reference_data import ReferenceLibrary


ROOT = Path(__file__).resolve().parents[1]
CONTENT_DATABASE_PATH = ROOT / "data" / "course_catalog.sqlite3"
logger = logging.getLogger(__name__)


def _configure_logging(settings: Settings) -> None:
    settings.log_path.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=[
            logging.StreamHandler(),
            TimedRotatingFileHandler(
                settings.log_path,
                when="midnight",
                backupCount=max(0, settings.technical_log_retention_days - 1),
                encoding="utf-8",
                utc=True,
            ),
        ],
    )
    logging.getLogger("aiogram").setLevel(logging.WARNING)


def _load_courses() -> list[dict]:
    path = ROOT / "content" / "courses.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list) or not raw:
        raise ValueError("content/courses.json must contain a non-empty JSON list of courses.")
    return [validate_course(course) for course in raw]


async def _local_check(settings: Settings) -> None:
    courses = _load_courses()
    with tempfile.TemporaryDirectory(prefix="english-bot-check-") as directory:
        database = Database(Path(directory) / "check.sqlite3")
        await database.initialize()
        await database.seed_courses(courses)
        published = await database.published_courses()
        if len(published) != 2:
            raise RuntimeError("The local check expected both English courses to be published.")
        expected_words = sum(
            len(lesson["vocabulary"])
            for course in courses
            for lesson in course["lessons"]
        )
        expected_exercises = sum(
            len(lesson["exercises"])
            for course in courses
            for lesson in course["lessons"]
        )
        catalog = await database.catalog_counts()
        if catalog["vocabulary_catalog"] != expected_words:
            raise RuntimeError("The normalized vocabulary catalog is missing entries.")
        if catalog["exercise_catalog"] != expected_exercises:
            raise RuntimeError("The normalized exercise catalog is missing entries.")
        general = next(course for course in published if course["track"] == "general")
        correct, level = score_placement(
            general["placement_test"],
            [question["correct_index"] for question in general["placement_test"]],
        )
        if correct != len(general["placement_test"]) or level != "C2":
            raise RuntimeError("Placement assessment did not return the expected upper-band result.")
        plan = learning_plan(10)
        if plan["new_words"] != 4 or plan["exercises"] != 2:
            raise RuntimeError("Daily study-plan calculation failed.")
        reminder_due, _ = reminder_is_due(
            datetime(2026, 10, 28, 17, 0, tzinfo=UTC),
            "Europe/Kyiv",
            "19:00",
            [2],
            None,
        )
        if not reminder_due:
            raise RuntimeError("Timezone-aware reminder scheduling failed.")

        user_id = 9_000_000_001
        await database.create_user(user_id)
        await database.update_user(
            user_id,
            privacy_accepted=1,
            course_id=general["id"],
            level="A1",
            minutes_per_day=10,
            timezone="Europe/Kyiv",
            reminder_time="19:00",
            reminder_days_json=[0, 1, 2, 3, 4, 5, 6],
        )
        await database.add_vocabulary(user_id, general["id"], general["lessons"][0]["vocabulary"])
        await database.complete_lesson(user_id, general["id"], general["lessons"][0]["id"])
        if general["lessons"][0]["id"] not in await database.completed_lessons(user_id, general["id"]):
            raise RuntimeError("Lesson progress was not persisted.")
        if not await database.claim_notification(user_id, "2099-01-01", "study_reminder"):
            raise RuntimeError("The first reminder claim was unexpectedly rejected.")
        if await database.claim_notification(user_id, "2099-01-01", "study_reminder"):
            raise RuntimeError("The reminder de-duplication check failed.")
        if not await database.delete_user(user_id):
            raise RuntimeError("The test profile could not be deleted.")
        if await database.get_user(user_id):
            raise RuntimeError("The deleted profile is still present.")
        await database.cleanup_retention(
            settings.inactive_profile_months, settings.technical_log_retention_days
        )
    print(
        "Local check passed: both courses, placement, study plan, timezone scheduling, "
        "SQLite progress, reminder de-duplication, deletion."
    )


async def _run(settings: Settings) -> None:
    _configure_logging(settings)
    database = Database(settings.database_path)
    await database.initialize()
    courses = _load_courses()
    await database.seed_courses(courses)
    dataset_dir = settings.dataset_dir
    if not dataset_dir.is_absolute():
        dataset_dir = ROOT / dataset_dir
    reference = ReferenceLibrary.load(
        dataset_dir,
        courses,
        enable_cefr_sp=settings.enable_cefr_sp_noncommercial,
    )
    await database.cleanup_retention(
        settings.inactive_profile_months, settings.technical_log_retention_days
    )

    bot = Bot(token=settings.bot_token)
    dispatcher = Dispatcher()
    open_dataset = OpenDataset(ROOT / "data" / "open_training.sqlite3")
    register_handlers(dispatcher, database, settings, reference, open_dataset)
    await configure_commands(bot)
    worker = asyncio.create_task(reminder_worker(bot, database, settings), name="reminder-worker")
    try:
        logger.info("Starting Telegram long polling.")
        await dispatcher.start_polling(bot, allowed_updates=dispatcher.resolve_used_update_types())
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
        await bot.session.close()


async def _init_content_database(database_path: Path) -> None:
        courses = _load_courses()
        database = Database(database_path)
        await database.initialize()
        await database.seed_courses(courses)
        counts = await database.catalog_counts()
        expected_words = sum(len(lesson["vocabulary"]) for course in courses for lesson in course["lessons"])
        expected_exercises = sum(len(lesson["exercises"]) for course in courses for lesson in course["lessons"])
        print(f"Course catalog database: {database_path.resolve()}")
        print(f"Courses: {len(courses)} · lessons: {counts['lesson_catalog']}")
        print(f"Words: {counts['vocabulary_catalog']} · exercises: {counts['exercise_catalog']}")
        if counts["vocabulary_catalog"] < expected_words or counts["exercise_catalog"] < expected_exercises:
            print(
                "The database contains older course versions. Existing data was preserved. "
                "Choose a new file path to create a separate catalog database."
            )


async def _inspect_content_database(database_path: Path) -> None:
        if not database_path.is_file():
            raise FileNotFoundError(
                f"Course catalog database not found: {database_path}. "
                "Create it with --init-content-db first."
            )
        database = Database(database_path)
        counts = await database.catalog_counts()
        print(f"Course catalog database: {database_path.resolve()}")
        print(f"Courses: {counts['course_versions']} · lessons: {counts['lesson_catalog']}")
        print(f"Words: {counts['vocabulary_catalog']} · exercises: {counts['exercise_catalog']}")
        async with database._connect() as db:
            cursor = await db.execute(
                "SELECT course_id, word, translation, example FROM vocabulary_catalog "
                "ORDER BY course_id, lesson_id LIMIT 5"
            )
            for row in await cursor.fetchall():
                print(f"• {row['course_id']}: {row['word']} — {row['translation']} | {row['example']}")


def run() -> None:
        parser = argparse.ArgumentParser(description="Local-first Ukrainian English-learning Telegram bot.")
        actions = parser.add_mutually_exclusive_group()
        actions.add_argument(
            "--check",
            action="store_true",
            help="Validate course content and run an isolated local SQLite scenario.",
        )
        actions.add_argument(
            "--init-content-db",
            nargs="?",
            const=str(CONTENT_DATABASE_PATH),
            metavar="PATH",
            help="Create a course-only SQLite database for browsing; this does not launch Telegram.",
        )
        actions.add_argument(
            "--inspect-content-db",
            nargs="?",
            const=str(CONTENT_DATABASE_PATH),
            metavar="PATH",
            help="Show the course counts and sample vocabulary from a catalog database.",
        )
        args = parser.parse_args()
        requires_token = (
            not args.check
            and args.init_content_db is None
            and args.inspect_content_db is None
        )
        settings = load_settings(require_token=requires_token)
        if args.check:
            asyncio.run(_local_check(settings))
            return
        if args.init_content_db is not None:
            asyncio.run(_init_content_database(Path(args.init_content_db)))
            return
        if args.inspect_content_db is not None:
            asyncio.run(_inspect_content_database(Path(args.inspect_content_db)))
            return
        asyncio.run(_run(settings))


if __name__ == "__main__":
    run()
