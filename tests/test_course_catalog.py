import json
from pathlib import Path

from telegram_bot.content import validate_course
from telegram_bot.course_catalog import build_course_catalog


ROOT = Path(__file__).resolve().parents[1]


def _read_demo_data() -> tuple[list[dict], dict]:
    courses = json.loads((ROOT / "content" / "courses.json").read_text(encoding="utf-8"))
    bank = json.loads((ROOT / "content" / "vocabulary_bank.json").read_text(encoding="utf-8"))
    return courses, bank


def test_course_catalog_includes_320_additional_words_and_exercises() -> None:
    courses, _ = _read_demo_data()

    demo_words = [
        word
        for course in courses
        for lesson in course["lessons"]
        if lesson["id"].startswith("catalog-")
        for word in lesson["vocabulary"]
    ]
    demo_exercises = [
        exercise
        for course in courses
        for lesson in course["lessons"]
        if lesson["id"].startswith("catalog-")
        for exercise in lesson["exercises"]
    ]

    assert len(demo_words) == 320
    assert len({word["word"].casefold() for word in demo_words}) == 320
    assert len(demo_exercises) == 320
    assert {course["track"] for course in courses} == {"general", "business"}
    assert all(word["example"].strip() and word["example_translation"].strip() for word in demo_words)
    assert all(
        "____" in exercise["sentence"]
        and exercise["correct_answer"] in exercise["options"]
        and exercise["explanation"].strip()
        for exercise in demo_exercises
    )


def test_course_catalog_generator_is_repeatable_and_keeps_existing_content() -> None:
    courses, bank = _read_demo_data()
    regenerated = [validate_course(course) for course in build_course_catalog(courses, bank)]

    assert regenerated == courses
    assert all(
        lesson["id"].startswith("catalog-") or lesson["title"] in {"Знайомство та привітання", "Мій звичайний день",
                                                                     "Знайомство на робочій зустрічі", "Підсумки зустрічі"}
        for course in regenerated
        for lesson in course["lessons"][:2]
    )
