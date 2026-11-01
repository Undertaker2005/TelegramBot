from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import aiosqlite

from telegram_bot.logic import retention_cutoff, study_streak


_SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS users (
    telegram_id INTEGER PRIMARY KEY,
    privacy_accepted INTEGER NOT NULL DEFAULT 0,
    course_id TEXT,
    level TEXT,
    placement_score INTEGER,
    placement_answers_json TEXT,
    placement_version INTEGER,
    placement_at TEXT,
    minutes_per_day INTEGER,
    timezone TEXT NOT NULL DEFAULT 'Europe/Kyiv',
    reminder_time TEXT NOT NULL DEFAULT '19:00',
    reminder_days_json TEXT NOT NULL DEFAULT '[0,1,2,3,4,5,6]',
    reminders_enabled INTEGER NOT NULL DEFAULT 1,
    paused INTEGER NOT NULL DEFAULT 0,
    state TEXT,
    state_data_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    last_active_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS course_versions (
    course_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'published')),
    created_at TEXT NOT NULL,
    approved_at TEXT,
    PRIMARY KEY (course_id, version)
);
CREATE INDEX IF NOT EXISTS idx_course_versions_status ON course_versions(course_id, status, version DESC);
CREATE TABLE IF NOT EXISTS lesson_catalog (
    course_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    lesson_id TEXT NOT NULL,
    lesson_order INTEGER NOT NULL,
    level TEXT NOT NULL,
    title TEXT NOT NULL,
    objectives TEXT NOT NULL,
    explanation TEXT NOT NULL,
    video_title TEXT,
    video_url TEXT,
    PRIMARY KEY (course_id, version, lesson_id),
    FOREIGN KEY (course_id, version) REFERENCES course_versions(course_id, version) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS vocabulary_catalog (
    course_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    lesson_id TEXT NOT NULL,
    word_id TEXT NOT NULL,
    word TEXT NOT NULL,
    translation TEXT NOT NULL,
    transcription TEXT,
    part_of_speech TEXT,
    example TEXT NOT NULL,
    example_translation TEXT NOT NULL,
    PRIMARY KEY (course_id, version, word_id),
    FOREIGN KEY (course_id, version, lesson_id)
        REFERENCES lesson_catalog(course_id, version, lesson_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS exercise_catalog (
    course_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    lesson_id TEXT NOT NULL,
    exercise_id TEXT NOT NULL,
    exercise_order INTEGER NOT NULL,
    sentence TEXT NOT NULL,
    options_json TEXT NOT NULL,
    correct_answer TEXT NOT NULL,
    explanation TEXT NOT NULL,
    example_translation TEXT,
    max_attempts INTEGER NOT NULL,
    PRIMARY KEY (course_id, version, exercise_id),
    FOREIGN KEY (course_id, version, lesson_id)
        REFERENCES lesson_catalog(course_id, version, lesson_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_catalog_vocab_word ON vocabulary_catalog(word, translation);
CREATE INDEX IF NOT EXISTS idx_catalog_exercises_lesson ON exercise_catalog(course_id, version, lesson_id, exercise_order);
CREATE TABLE IF NOT EXISTS vocabulary_progress (
    telegram_id INTEGER NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
    course_id TEXT NOT NULL,
    word_id TEXT NOT NULL,
    correct_streak INTEGER NOT NULL DEFAULT 0,
    next_review_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (telegram_id, course_id, word_id)
);
CREATE INDEX IF NOT EXISTS idx_vocabulary_due ON vocabulary_progress(telegram_id, course_id, next_review_at);
CREATE TABLE IF NOT EXISTS lesson_progress (
    telegram_id INTEGER NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
    course_id TEXT NOT NULL,
    lesson_id TEXT NOT NULL,
    completed_at TEXT NOT NULL,
    PRIMARY KEY (telegram_id, course_id, lesson_id)
);
CREATE TABLE IF NOT EXISTS study_activity (
    telegram_id INTEGER NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
    local_date TEXT NOT NULL,
    PRIMARY KEY (telegram_id, local_date)
);
CREATE TABLE IF NOT EXISTS exercise_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id INTEGER NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
    course_id TEXT NOT NULL,
    lesson_id TEXT NOT NULL,
    exercise_id TEXT NOT NULL,
    was_correct INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS notification_log (
    telegram_id INTEGER NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
    local_date TEXT NOT NULL,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (telegram_id, local_date, kind)
);
CREATE TABLE IF NOT EXISTS technical_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_users_last_active ON users(last_active_at);
CREATE INDEX IF NOT EXISTS idx_technical_events_created ON technical_events(created_at);
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);
"""

_USER_COLUMNS = {
    "privacy_accepted", "course_id", "level", "placement_score", "placement_answers_json",
    "placement_version", "placement_at", "minutes_per_day", "timezone", "reminder_time",
    "reminder_days_json", "reminders_enabled", "paused", "state", "state_data_json",
    "last_active_at",
}


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path

    async def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.path) as db:
            await db.executescript(_SCHEMA)
            cursor = await db.execute("SELECT COALESCE(MAX(version), 0) FROM schema_migrations")
            version = int((await cursor.fetchone())[0])
            if version > 2:
                raise RuntimeError(f"Database schema version {version} is newer than this application supports.")
            if version == 0:
                await db.execute("INSERT INTO schema_migrations (version, applied_at) VALUES (1, ?)", (_now(),))
            if version < 2:
                await db.execute("INSERT INTO schema_migrations (version, applied_at) VALUES (2, ?)", (_now(),))
            if version < 2:
                cursor = await db.execute(
                    "SELECT course_id, version, payload_json FROM course_versions"
                )
                for row in await cursor.fetchall():
                    course = json.loads(row["payload_json"])
                    await _insert_catalog(db, course, int(row["version"]), ignore=True)
            await db.commit()

    @asynccontextmanager
    async def _connect(self) -> AsyncIterator[aiosqlite.Connection]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("PRAGMA foreign_keys = ON")
            yield db

    async def create_user(self, telegram_id: int) -> None:
        now = _now()
        async with self._connect() as db:
            await db.execute(
                "INSERT INTO users (telegram_id, created_at, last_active_at) VALUES (?, ?, ?) "
                "ON CONFLICT(telegram_id) DO UPDATE SET last_active_at = excluded.last_active_at",
                (telegram_id, now, now),
            )
            await db.commit()

    async def get_user(self, telegram_id: int) -> dict[str, Any] | None:
        async with self._connect() as db:
            cursor = await db.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,))
            row = await cursor.fetchone()
        return dict(row) if row else None

    async def update_user(self, telegram_id: int, **fields: Any) -> None:
        unknown = fields.keys() - _USER_COLUMNS
        if unknown:
            raise ValueError(f"Unsupported user fields: {', '.join(sorted(unknown))}.")
        if not fields:
            return
        fields["last_active_at"] = _now()
        assignments = ", ".join(f"{column} = ?" for column in fields)
        values = [_db_value(value) for value in fields.values()]
        async with self._connect() as db:
            cursor = await db.execute(
                f"UPDATE users SET {assignments} WHERE telegram_id = ?",
                (*values, telegram_id),
            )
            if cursor.rowcount == 0:
                raise LookupError("User profile does not exist.")
            await db.commit()

    async def set_state(self, telegram_id: int, state: str | None, data: dict[str, Any] | None = None) -> None:
        await self.update_user(
            telegram_id,
            state=state,
            state_data_json=json.dumps(data or {}, ensure_ascii=False),
        )

    async def add_course_version(self, course: dict[str, Any], status: str) -> int:
        if status not in {"pending", "published"}:
            raise ValueError("Course version status must be pending or published.")
        async with self._connect() as db:
            cursor = await db.execute(
                "SELECT COALESCE(MAX(version), 0) + 1 FROM course_versions WHERE course_id = ?",
                (course["id"],),
            )
            version = int((await cursor.fetchone())[0])
            now = _now()
            await db.execute(
                "INSERT INTO course_versions "
                "(course_id, version, payload_json, status, created_at, approved_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    course["id"], version, json.dumps(course, ensure_ascii=False), status,
                    now, now if status == "published" else None,
                ),
            )
            await _insert_catalog(db, course, version)
            await db.commit()
        return version

    async def seed_courses(self, courses: list[dict[str, Any]]) -> None:
        for course in courses:
            async with self._connect() as db:
                cursor = await db.execute(
                    "SELECT 1 FROM course_versions WHERE course_id = ? LIMIT 1", (course["id"],)
                )
                exists = await cursor.fetchone()
            if not exists:
                await self.add_course_version(course, "published")

    async def published_courses(self) -> list[dict[str, Any]]:
        async with self._connect() as db:
            cursor = await db.execute(
                "SELECT payload_json FROM course_versions cv "
                "WHERE status = 'published' AND version = "
                "(SELECT MAX(version) FROM course_versions latest "
                "WHERE latest.course_id = cv.course_id AND latest.status = 'published') "
                "ORDER BY course_id"
            )
            rows = await cursor.fetchall()
        return [json.loads(row["payload_json"]) for row in rows]

    async def catalog_counts(self) -> dict[str, int]:
        async with self._connect() as db:
            counts = {}
            for name in ("course_versions", "lesson_catalog", "vocabulary_catalog", "exercise_catalog"):
                cursor = await db.execute(f"SELECT COUNT(*) FROM {name}")
                counts[name] = int((await cursor.fetchone())[0])
        return counts

    async def get_published_course(self, course_id: str) -> dict[str, Any] | None:
        async with self._connect() as db:
            cursor = await db.execute(
                "SELECT payload_json FROM course_versions WHERE course_id = ? AND status = 'published' "
                "ORDER BY version DESC LIMIT 1",
                (course_id,),
            )
            row = await cursor.fetchone()
        return json.loads(row["payload_json"]) if row else None

    async def published_course_version(self, course_id: str) -> int | None:
        async with self._connect() as db:
            cursor = await db.execute(
                "SELECT version FROM course_versions WHERE course_id = ? AND status = 'published' "
                "ORDER BY version DESC LIMIT 1",
                (course_id,),
            )
            row = await cursor.fetchone()
        return int(row["version"]) if row else None

    async def pending_courses(self) -> list[dict[str, Any]]:
        async with self._connect() as db:
            cursor = await db.execute(
                "SELECT course_id, version FROM course_versions "
                "WHERE status = 'pending' ORDER BY created_at"
            )
            rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def approve_course(self, course_id: str, version: int) -> bool:
        async with self._connect() as db:
            cursor = await db.execute(
                "UPDATE course_versions SET status = 'published', approved_at = ? "
                "WHERE course_id = ? AND version = ? AND status = 'pending'",
                (_now(), course_id, version),
            )
            await db.commit()
        return cursor.rowcount == 1

    async def add_vocabulary(self, telegram_id: int, course_id: str, words: list[dict[str, Any]]) -> None:
        now = datetime.now(UTC)
        first_review = (now + timedelta(days=1)).isoformat()
        async with self._connect() as db:
            await db.executemany(
                "INSERT OR IGNORE INTO vocabulary_progress "
                "(telegram_id, course_id, word_id, next_review_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                [(telegram_id, course_id, word["id"], first_review, now.isoformat()) for word in words],
            )
            await db.commit()

    async def due_vocabulary(self, telegram_id: int, course_id: str) -> list[str]:
        async with self._connect() as db:
            cursor = await db.execute(
                "SELECT word_id FROM vocabulary_progress WHERE telegram_id = ? AND course_id = ? "
                "AND next_review_at <= ? ORDER BY next_review_at, word_id",
                (telegram_id, course_id, _now()),
            )
            rows = await cursor.fetchall()
        return [row["word_id"] for row in rows]

    async def known_vocabulary(self, telegram_id: int, course_id: str) -> list[str]:
        async with self._connect() as db:
            cursor = await db.execute(
                "SELECT word_id FROM vocabulary_progress WHERE telegram_id = ? AND course_id = ? "
                "ORDER BY updated_at DESC, word_id",
                (telegram_id, course_id),
            )
            rows = await cursor.fetchall()
        return [row["word_id"] for row in rows]

    async def record_vocabulary_answer(
        self, telegram_id: int, course_id: str, word_id: str, correct: bool
    ) -> None:
        now = datetime.now(UTC)
        interval = timedelta(days=2) if correct else timedelta(hours=1)
        async with self._connect() as db:
            await db.execute(
                "UPDATE vocabulary_progress SET correct_streak = MAX(0, correct_streak + ?), "
                "next_review_at = ?, updated_at = ? "
                "WHERE telegram_id = ? AND course_id = ? AND word_id = ?",
                (
                    1 if correct else -1, (now + interval).isoformat(), now.isoformat(),
                    telegram_id, course_id, word_id,
                ),
            )
            await _record_study_day(db, telegram_id)
            await db.commit()

    async def completed_lessons(self, telegram_id: int, course_id: str) -> set[str]:
        async with self._connect() as db:
            cursor = await db.execute(
                "SELECT lesson_id FROM lesson_progress WHERE telegram_id = ? AND course_id = ?",
                (telegram_id, course_id),
            )
            rows = await cursor.fetchall()
        return {row["lesson_id"] for row in rows}

    async def complete_lesson(self, telegram_id: int, course_id: str, lesson_id: str) -> None:
        async with self._connect() as db:
            await db.execute(
                "INSERT OR IGNORE INTO lesson_progress VALUES (?, ?, ?, ?)",
                (telegram_id, course_id, lesson_id, _now()),
            )
            await db.commit()

    async def record_exercise_attempt(
        self, telegram_id: int, course_id: str, lesson_id: str, exercise_id: str, correct: bool
    ) -> None:
        async with self._connect() as db:
            await db.execute(
                "INSERT INTO exercise_attempts "
                "(telegram_id, course_id, lesson_id, exercise_id, was_correct, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (telegram_id, course_id, lesson_id, exercise_id, int(correct), _now()),
            )
            await _record_study_day(db, telegram_id)
            await db.commit()

    async def current_study_streak(self, telegram_id: int) -> int:
        async with self._connect() as db:
            cursor = await db.execute("SELECT timezone FROM users WHERE telegram_id = ?", (telegram_id,))
            user = await cursor.fetchone()
            if not user:
                return 0
            days = await db.execute(
                "SELECT local_date FROM study_activity WHERE telegram_id = ? ORDER BY local_date DESC",
                (telegram_id,),
            )
            rows = await days.fetchall()
        today = datetime.now(ZoneInfo(user["timezone"])).date()
        return study_streak({row["local_date"] for row in rows}, today)

    async def progress_summary(self, telegram_id: int, course_id: str) -> dict[str, int]:
        async with self._connect() as db:
            lessons = await db.execute(
                "SELECT COUNT(*) FROM lesson_progress WHERE telegram_id = ? AND course_id = ?",
                (telegram_id, course_id),
            )
            attempts = await db.execute(
                "SELECT COUNT(*), COALESCE(SUM(was_correct), 0) FROM exercise_attempts "
                "WHERE telegram_id = ? AND course_id = ?",
                (telegram_id, course_id),
            )
            attempt_row = await attempts.fetchone()
            due = await db.execute(
                "SELECT COUNT(*) FROM vocabulary_progress WHERE telegram_id = ? AND course_id = ? "
                "AND next_review_at <= ?",
                (telegram_id, course_id, _now()),
            )
            return {
                "lessons": int((await lessons.fetchone())[0]),
                "attempts": int(attempt_row[0]),
                "correct": int(attempt_row[1]),
                "words_due": int((await due.fetchone())[0]),
            }

    async def claim_notification(self, telegram_id: int, local_date: str, kind: str) -> bool:
        async with self._connect() as db:
            cursor = await db.execute(
                "INSERT OR IGNORE INTO notification_log "
                "(telegram_id, local_date, kind, status, created_at) VALUES (?, ?, ?, 'claimed', ?)",
                (telegram_id, local_date, kind, _now()),
            )
            await db.commit()
        return cursor.rowcount == 1

    async def update_notification_status(
        self, telegram_id: int, local_date: str, kind: str, status: str
    ) -> None:
        async with self._connect() as db:
            await db.execute(
                "UPDATE notification_log SET status = ? WHERE telegram_id = ? AND local_date = ? AND kind = ?",
                (status, telegram_id, local_date, kind),
            )
            await db.commit()

    async def reminder_users(self) -> list[dict[str, Any]]:
        async with self._connect() as db:
            cursor = await db.execute(
                "SELECT telegram_id, course_id, timezone, reminder_time, reminder_days_json, "
                "reminders_enabled, paused FROM users WHERE privacy_accepted = 1"
            )
            rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def last_notification_date(self, telegram_id: int, kind: str) -> str | None:
        async with self._connect() as db:
            cursor = await db.execute(
                "SELECT local_date FROM notification_log WHERE telegram_id = ? AND kind = ? "
                "ORDER BY local_date DESC LIMIT 1",
                (telegram_id, kind),
            )
            row = await cursor.fetchone()
        return row["local_date"] if row else None

    async def disable_reminders(self, telegram_id: int) -> None:
        await self.update_user(telegram_id, reminders_enabled=0)

    async def delete_user(self, telegram_id: int) -> bool:
        async with self._connect() as db:
            cursor = await db.execute("DELETE FROM users WHERE telegram_id = ?", (telegram_id,))
            await db.commit()
        return cursor.rowcount == 1

    async def cleanup_retention(self, inactive_months: int, log_retention_days: int) -> tuple[int, int]:
        now = datetime.now(UTC)
        profile_cutoff = retention_cutoff(now, inactive_months)
        log_cutoff = (now - timedelta(days=log_retention_days)).isoformat()
        async with self._connect() as db:
            profiles = await db.execute("DELETE FROM users WHERE last_active_at < ?", (profile_cutoff,))
            events = await db.execute("DELETE FROM technical_events WHERE created_at < ?", (log_cutoff,))
            await db.commit()
        return profiles.rowcount, events.rowcount

    async def record_technical_event(self, kind: str) -> None:
        if not kind or len(kind) > 80:
            raise ValueError("Technical event names must be between 1 and 80 characters.")
        async with self._connect() as db:
            await db.execute("INSERT INTO technical_events (kind, created_at) VALUES (?, ?)", (kind, _now()))
            await db.commit()


def _db_value(value: Any) -> Any:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False)
    return value


def _now() -> str:
    return datetime.now(UTC).isoformat()


async def _record_study_day(db: aiosqlite.Connection, telegram_id: int) -> None:
    cursor = await db.execute("SELECT timezone FROM users WHERE telegram_id = ?", (telegram_id,))
    user = await cursor.fetchone()
    if not user:
        raise LookupError("User profile does not exist.")
    local_date = datetime.now(ZoneInfo(user["timezone"])).date().isoformat()
    await db.execute(
        "INSERT OR IGNORE INTO study_activity (telegram_id, local_date) VALUES (?, ?)",
        (telegram_id, local_date),
    )


async def _insert_catalog(
    db: aiosqlite.Connection,
    course: dict[str, Any],
    version: int,
    *,
    ignore: bool = False,
) -> None:
    insert = "INSERT OR IGNORE" if ignore else "INSERT"
    lessons = course["lessons"]
    await db.executemany(
        f"{insert} INTO lesson_catalog "
        "(course_id, version, lesson_id, lesson_order, level, title, objectives, explanation, "
        "video_title, video_url) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                course["id"], version, lesson["id"], lesson["order"], lesson["level"],
                lesson["title"], lesson["objectives"], lesson["explanation"],
                lesson.get("video", {}).get("title"), lesson.get("video", {}).get("url"),
            )
            for lesson in lessons
        ],
    )
    await db.executemany(
        f"{insert} INTO vocabulary_catalog "
        "(course_id, version, lesson_id, word_id, word, translation, transcription, "
        "part_of_speech, example, example_translation) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                course["id"], version, lesson["id"], word["id"], word["word"],
                word["translation"], word.get("transcription"), word.get("part_of_speech"),
                word["example"], word["example_translation"],
            )
            for lesson in lessons
            for word in lesson["vocabulary"]
        ],
    )
    await db.executemany(
        f"{insert} INTO exercise_catalog "
        "(course_id, version, lesson_id, exercise_id, exercise_order, sentence, options_json, "
        "correct_answer, explanation, example_translation, max_attempts) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                course["id"], version, lesson["id"], exercise["id"], exercise_index,
                exercise["sentence"], json.dumps(exercise["options"], ensure_ascii=False),
                exercise["correct_answer"], exercise["explanation"], exercise.get("translation"),
                exercise.get("max_attempts", 1),
            )
            for lesson in lessons
            for exercise_index, exercise in enumerate(lesson["exercises"], start=1)
        ],
    )
