from datetime import UTC, datetime, timedelta
import json
from pathlib import Path

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
            "WHERE exercise_id = 'exercise-catalog-business-agenda'"
        )
        exercise = await cursor.fetchone()

    assert word["translation"] == "порядок денний"
    assert "agenda" in word["example"]
    assert exercise["correct_answer"] == "agenda"
    assert "____" in exercise["sentence"]
