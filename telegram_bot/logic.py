from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
import re
from typing import Any


LEVELS = ("A1", "A2", "B1", "B2", "C1", "C2")
TIME_PATTERN = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")


def placement_breakdown(
    questions: list[dict[str, Any]], answers: list[int]
) -> dict[str, tuple[int, int]]:
    if len(questions) != len(answers) or not questions:
        raise ValueError("Every placement question must have exactly one recorded answer.")
    scores = {level: [0, 0] for level in LEVELS}
    for question, answer in zip(questions, answers, strict=True):
        level = question.get("level")
        correct_index = question.get("correct_index")
        if (
            level not in scores
            or not isinstance(correct_index, int)
            or isinstance(correct_index, bool)
            or not isinstance(answer, int)
            or isinstance(answer, bool)
        ):
            raise ValueError("Placement answers need valid CEFR levels and integer option indexes.")
        scores[level][1] += 1
        scores[level][0] += answer == correct_index
    return {
        level: (correct, total)
        for level, (correct, total) in scores.items()
        if total
    }


def score_placement(questions: list[dict[str, Any]], answers: list[int]) -> tuple[int, str]:
    breakdown = placement_breakdown(questions, answers)
    correct = sum(level_correct for level_correct, _ in breakdown.values())
    level = LEVELS[0]
    for candidate in LEVELS:
        result = breakdown.get(candidate)
        if result is None:
            break
        level_correct, total = result
        if level_correct * 4 < total * 3:
            break
        level = candidate
    return correct, level


def learning_plan(minutes_per_day: int) -> dict[str, int]:
    if minutes_per_day not in (5, 10, 15, 20):
        raise ValueError("Daily study time must be 5, 10, 15, or 20 minutes.")
    return {
        "minutes": minutes_per_day,
        "new_words": max(2, minutes_per_day // 5 * 2),
        "exercises": max(1, minutes_per_day // 5),
    }


def review_passed(correct: int, total: int, threshold: float) -> bool:
    if total < 1 or correct < 0 or correct > total or not 0 < threshold <= 1:
        raise ValueError("Review results and threshold must be within valid ranges.")
    return correct / total >= threshold


def validate_reminder_time(value: str) -> str:
    normalized = value.strip()
    if not TIME_PATTERN.fullmatch(normalized):
        raise ValueError("Use 24-hour time in HH:MM format, for example 19:30.")
    return normalized


def validate_timezone(value: str) -> str:
    normalized = value.strip()
    try:
        ZoneInfo(normalized)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError("Enter a valid IANA timezone, for example Europe/Kyiv.") from exc
    return normalized


def next_lesson(lessons: list[dict[str, Any]], completed_ids: set[str]) -> dict[str, Any] | None:
    return next(
        (lesson for lesson in sorted(lessons, key=lambda item: item["order"]) if lesson["id"] not in completed_ids),
        None,
    )


def study_streak(study_days: set[str], today: date) -> int:
    if today.isoformat() in study_days:
        cursor = today
    elif (today - timedelta(days=1)).isoformat() in study_days:
        cursor = today - timedelta(days=1)
    else:
        return 0
    streak = 0
    while cursor.isoformat() in study_days:
        streak += 1
        cursor -= timedelta(days=1)
    return streak


def reminder_is_due(
    now: datetime,
    timezone_name: str,
    reminder_time: str,
    reminder_days: list[int],
    already_sent_local_date: str | None,
) -> tuple[bool, str]:
    if now.tzinfo is None:
        raise ValueError("Reminder evaluation requires an aware datetime.")
    zone = ZoneInfo(validate_timezone(timezone_name))
    local_now = now.astimezone(zone)
    target_time = time.fromisoformat(validate_reminder_time(reminder_time))
    local_date = local_now.date().isoformat()
    due = (
        local_now.weekday() in reminder_days
        and local_now.time().replace(tzinfo=None) >= target_time
        and already_sent_local_date != local_date
    )
    return due, local_date


def retention_cutoff(now: datetime, months: int) -> str:
    if now.tzinfo is None or months < 1:
        raise ValueError("Retention requires an aware datetime and a positive month count.")
    now = now.astimezone(UTC)
    year = now.year
    month = now.month - months
    while month <= 0:
        year -= 1
        month += 12
    day = min(now.day, (date(year + (month == 12), month % 12 + 1, 1) - date.resolution).day)
    return datetime(year, month, day, tzinfo=UTC).isoformat()
