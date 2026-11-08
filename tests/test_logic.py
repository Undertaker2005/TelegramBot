from datetime import UTC, datetime

import pytest

from telegram_bot.logic import (
    learning_plan,
    next_lesson,
    placement_breakdown,
    reminder_is_due,
    retention_cutoff,
    review_passed,
    score_placement,
    study_streak,
    validate_reminder_time,
    validate_timezone,
)


def test_placement_score_maps_to_orientative_level() -> None:
    levels = ("A1", "A2", "B1", "B2", "C1", "C2")
    questions = [
        {"level": level, "correct_index": 0}
        for level in levels
        for _ in range(4)
    ]
    answers = [0] * 16 + [1, 1, 0, 0] + [0] * 4

    assert score_placement(questions, answers) == (22, "B2")
    assert placement_breakdown(questions, answers)["C1"] == (2, 4)


def test_placement_requires_a_passing_result_at_each_preceding_band() -> None:
    levels = ("A1", "A2", "B1", "B2", "C1", "C2")
    questions = [
        {"level": level, "correct_index": 0}
        for level in levels
        for _ in range(4)
    ]

    assert score_placement(questions, [0] * len(questions)) == (24, "C2")
    answers = [0] * 8 + [1, 1, 1, 0] + [0] * 12
    assert score_placement(questions, answers) == (21, "A2")


@pytest.mark.parametrize("answers", [[], [0]])
def test_placement_rejects_missing_or_incomplete_answers(answers: list[int]) -> None:
    with pytest.raises(ValueError):
        score_placement([{"correct_index": 0}, {"correct_index": 0}], answers)


@pytest.mark.parametrize(
    ("minutes", "words", "exercises"),
    [(5, 2, 1), (10, 4, 2), (15, 6, 3), (20, 8, 4)],
)
def test_daily_plan_matches_supported_pace(minutes: int, words: int, exercises: int) -> None:
    assert learning_plan(minutes) == {
        "minutes": minutes,
        "new_words": words,
        "exercises": exercises,
    }


def test_daily_plan_rejects_unavailable_pace() -> None:
    with pytest.raises(ValueError):
        learning_plan(30)


def test_next_lesson_uses_order_and_skips_completed_lessons() -> None:
    lessons = [
        {"id": "second", "order": 2},
        {"id": "first", "order": 1},
        {"id": "third", "order": 3},
    ]

    assert next_lesson(lessons, {"first"}) == lessons[0]
    assert next_lesson(lessons, {"first", "second", "third"}) is None


def test_study_streak_counts_consecutive_local_days() -> None:
    today = datetime(2026, 10, 31, tzinfo=UTC).date()
    assert study_streak({"2026-10-29", "2026-10-30", "2026-10-31"}, today) == 3
    assert study_streak({"2026-10-29", "2026-10-30"}, today) == 2
    assert study_streak({"2026-10-28"}, today) == 0


@pytest.mark.parametrize(
    ("correct", "total", "threshold", "expected"),
    [(7, 10, 0.7, True), (6, 10, 0.7, False), (10, 10, 1.0, True)],
)
def test_review_pass_threshold(correct: int, total: int, threshold: float, expected: bool) -> None:
    assert review_passed(correct, total, threshold) is expected


def test_review_rejects_invalid_scores() -> None:
    with pytest.raises(ValueError):
        review_passed(2, 0, 0.7)


def test_timezone_schedule_observes_local_time_and_weekdays() -> None:
    now = datetime(2026, 10, 28, 17, 15, tzinfo=UTC)  # Wednesday, 19:15 in Kyiv.

    assert reminder_is_due(now, "Europe/Kyiv", "19:00", [2], None) == (True, "2026-10-28")
    assert reminder_is_due(now, "Europe/Kyiv", "20:00", [2], None)[0] is False
    assert reminder_is_due(now, "Europe/Kyiv", "19:00", [0, 1], None)[0] is False
    assert reminder_is_due(now, "Europe/Kyiv", "19:00", [2], "2026-10-28")[0] is False


def test_reminder_input_validation() -> None:
    assert validate_reminder_time(" 07:05 ") == "07:05"
    assert validate_timezone(" Europe/Kyiv ") == "Europe/Kyiv"
    with pytest.raises(ValueError):
        validate_reminder_time("7pm")
    with pytest.raises(ValueError):
        validate_timezone("Not/A_Timezone")


def test_retention_cutoff_handles_leap_day() -> None:
    assert retention_cutoff(datetime(2024, 2, 29, 12, tzinfo=UTC), 12) == "2023-02-28T00:00:00+00:00"
