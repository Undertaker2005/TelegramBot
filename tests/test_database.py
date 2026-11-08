from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import sqlite3

import pytest

from telegram_bot.content import validate_course
from telegram_bot.database import Database


@pytest.fixture
def demo_courses() -> list[dict]:
    path = Path(__file__).resolve().parents[1] / "content" / "courses.json"
    return [validate_course(item) for item in json.loads(path.read_text(encoding="utf-8"))]


@pytest.mark.asyncio
async def test_profile_progress_persists_and_delete_removes_dependents(
    tmp_path: Path, demo_courses: list[dict]
) -> None:
    db = Database(tmp_path / "bot.sqlite3")
    await db.initialize()
    await db.seed_courses(demo_courses)
    user_id = 123456
    course = demo_courses[0]
    lesson = course["lessons"][0]

    await db.create_user(user_id)
    await db.update_user(
        user_id,
        privacy_accepted=1,
        course_id=course["id"],
        level="A1",
        minutes_per_day=10,
        reminder_days_json=[0, 1, 2, 3, 4, 5, 6],
    )
    await db.add_vocabulary(user_id, course["id"], lesson["vocabulary"])
    await db.record_exercise_attempt(user_id, course["id"], lesson["id"], "g-ex1", True)
    await db.complete_lesson(user_id, course["id"], lesson["id"])

    persisted = await db.get_user(user_id)
    assert persisted["course_id"] == course["id"]
    assert json.loads(persisted["reminder_days_json"]) == list(range(7))
    assert await db.completed_lessons(user_id, course["id"]) == {lesson["id"]}
    assert await db.current_study_streak(user_id) == 1
    assert set(await db.known_vocabulary(user_id, course["id"])) == {
        word["id"] for word in lesson["vocabulary"]
    }
    assert await db.progress_summary(user_id, course["id"]) == {
        "lessons": 1,
        "attempts": 1,
        "correct": 1,
        "words_due": 0,
    }

    assert await db.delete_user(user_id)
    assert await db.get_user(user_id) is None
    assert await db.completed_lessons(user_id, course["id"]) == set()
    async with db._connect() as connection:
        cursor = await connection.execute(
            "SELECT COUNT(*) FROM study_activity WHERE telegram_id = ?", (user_id,)
        )
        activity = await cursor.fetchone()
    assert activity[0] == 0


@pytest.mark.asyncio
async def test_open_practice_statistics_mistakes_and_wordbook_are_user_scoped(
    tmp_path: Path,
) -> None:
    db = Database(tmp_path / "open-practice.sqlite3")
    await db.initialize()
    async with db._connect() as connection:
        migration = await connection.execute("SELECT MAX(version) FROM schema_migrations")
        assert (await migration.fetchone())[0] == 5
        columns = await connection.execute("PRAGMA table_info(open_practice_attempts)")
        assert "topic" in {row["name"] for row in await columns.fetchall()}
    await db.create_user(123456)
    await db.update_user(123456, privacy_accepted=1)

    await db.record_open_practice_attempt(
        123456, "task-a", "definition_to_word", "A1", False, True, "Food"
    )
    await db.record_open_practice_attempt(
        123456, "task-b", "synonym_choice", None, True, False, "Travel"
    )
    summary = await db.open_practice_summary(123456)
    assert summary["attempts"] == 2
    assert summary["correct"] == 1
    assert summary["hints"] == 1
    assert {item["topic"] for item in summary["by_topic"]} == {"Food", "Travel"}
    assert {item["task_id"] for item in await db.recent_open_mistakes(123456)} == {"task-a"}

    word = {
        "entry_id": "word-1",
        "lemma": "apple",
        "part_of_speech": "noun",
        "definition": "A fruit.",
        "cefr_level": "A1",
        "source_id": "oewn-2025",
    }
    assert await db.save_user_word(123456, word)
    assert not await db.save_user_word(123456, word)
    assert (await db.user_wordbook(123456))[0]["word"] == "apple"
    assert await db.remove_user_word(123456, "word-1")
    assert await db.user_wordbook(123456) == []


@pytest.mark.asyncio
async def test_existing_open_attempts_table_migrates_topic_column(tmp_path: Path) -> None:
    path = tmp_path / "legacy-open-practice.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
            INSERT INTO schema_migrations VALUES (3, '2026-01-01T00:00:00+00:00');
            CREATE TABLE open_practice_attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER NOT NULL,
                task_id TEXT NOT NULL,
                task_type TEXT NOT NULL,
                cefr_level TEXT,
                was_correct INTEGER NOT NULL,
                used_hint INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );
            """
        )
    db = Database(path)
    await db.initialize()
    async with db._connect() as connection:
        columns = await connection.execute("PRAGMA table_info(open_practice_attempts)")
        assert "topic" in {row["name"] for row in await columns.fetchall()}
        version = await connection.execute("SELECT MAX(version) FROM schema_migrations")
        assert (await version.fetchone())[0] == 5


@pytest.mark.asyncio
async def test_imported_course_requires_moderator_approval_and_revisions_are_safe(
    tmp_path: Path, demo_courses: list[dict]
) -> None:
    db = Database(tmp_path / "bot.sqlite3")
    await db.initialize()
    course = demo_courses[0]
    await db.seed_courses([course])
    updated = json.loads(json.dumps(course))
    updated["title"] = "Загальна англійська — редакція"

    version = await db.add_course_version(updated, "pending")
    assert version == 2
    assert (await db.get_published_course(course["id"]))["title"] == course["title"]
    assert len(await db.pending_courses()) == 1
    assert await db.approve_course(course["id"], version)
    assert (await db.get_published_course(course["id"]))["title"] == updated["title"]
    assert not await db.approve_course(course["id"], version)

    seed_edit = json.loads(json.dumps(course))
    seed_edit["title"] = "Нова початкова назва"
    await db.seed_courses([seed_edit])
    assert (await db.get_published_course(course["id"]))["title"] == updated["title"]


@pytest.mark.asyncio
async def test_seed_refreshes_only_pristine_course_and_preserves_user_progress(
    tmp_path: Path, demo_courses: list[dict]
) -> None:
    db = Database(tmp_path / "seed-refresh.sqlite3")
    await db.initialize()
    course = demo_courses[0]
    old_course = json.loads(json.dumps(course))
    old_lesson = next(
        item for item in old_course["lessons"] if item["id"] == "catalog-general-1-1"
    )
    old_lesson["exercises"][0]["sentence"] = "We discussed the ____ today."
    await db.seed_courses([old_course])
    await db.create_user(123456)
    await db.complete_lesson(123456, course["id"], course["lessons"][0]["id"])

    expected_sentence = next(
        item for item in course["lessons"] if item["id"] == "catalog-general-1-1"
    )["exercises"][0]["sentence"]
    await db.seed_courses([course])

    published = await db.get_published_course(course["id"])
    refreshed_lesson = next(
        item for item in published["lessons"] if item["id"] == "catalog-general-1-1"
    )
    assert refreshed_lesson["exercises"][0]["sentence"] == expected_sentence
    assert await db.completed_lessons(123456, course["id"]) == {course["lessons"][0]["id"]}

    await db.seed_courses([course])
    async with db._connect() as connection:
        cursor = await connection.execute(
            "SELECT version, status FROM course_versions WHERE course_id = ? ORDER BY version",
            (course["id"],),
        )
        versions = await cursor.fetchall()
    assert [(row["version"], row["status"]) for row in versions] == [
        (1, "published"),
        (2, "published"),
    ]


@pytest.mark.asyncio
async def test_reminder_claim_is_unique_and_vocabulary_review_reschedules(
    tmp_path: Path, demo_courses: list[dict]
) -> None:
    db = Database(tmp_path / "bot.sqlite3")
    await db.initialize()
    course = demo_courses[0]
    await db.seed_courses([course])
    await db.create_user(123)
    await db.update_user(123, course_id=course["id"])
    word_id = course["lessons"][0]["vocabulary"][0]["id"]
    await db.add_vocabulary(123, course["id"], [course["lessons"][0]["vocabulary"][0]])
    async with db._connect() as connection:
        await connection.execute(
            "UPDATE vocabulary_progress SET next_review_at = ? WHERE telegram_id = ? AND word_id = ?",
            ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), 123, word_id),
        )
        await connection.commit()

    assert await db.due_vocabulary(123, course["id"]) == [word_id]
    await db.record_vocabulary_answer(123, course["id"], word_id, correct=False)
    assert await db.due_vocabulary(123, course["id"]) == []
    assert await db.claim_notification(123, "2026-10-31", "study_reminder")
    assert not await db.claim_notification(123, "2026-10-31", "study_reminder")


@pytest.mark.asyncio
async def test_content_catalog_can_be_browsed_as_individual_words_and_exercises(
    tmp_path: Path, demo_courses: list[dict]
) -> None:
    db = Database(tmp_path / "course-catalog.sqlite3")
    await db.initialize()
    await db.seed_courses(demo_courses)
    counts = await db.catalog_counts()
    expected_words = sum(len(lesson["vocabulary"]) for course in demo_courses for lesson in course["lessons"])
    expected_exercises = sum(len(lesson["exercises"]) for course in demo_courses for lesson in course["lessons"])

    assert counts["vocabulary_catalog"] == expected_words
    assert counts["exercise_catalog"] == expected_exercises
    async with db._connect() as connection:
        cursor = await connection.execute(
            "SELECT word, translation, example FROM vocabulary_catalog "
            "WHERE word = 'agenda' AND course_id = 'english_business'"
        )
        word = await cursor.fetchone()
        cursor = await connection.execute(
            "SELECT sentence, correct_answer, explanation FROM exercise_catalog "
            "WHERE exercise_id = 'exercise-catalog-business-agenda-1'"
        )
        exercise = await cursor.fetchone()

    assert word["translation"] == "порядок денний"
    assert "agenda" in word["example"]
    assert exercise["correct_answer"] == "agenda"
    assert "____" in exercise["sentence"]
