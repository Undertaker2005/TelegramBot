import json
from pathlib import Path

from telegram_bot.content import validate_course
from telegram_bot.course_catalog import build_course_catalog


ROOT = Path(__file__).resolve().parents[1]


def _read_demo_data() -> tuple[list[dict], dict]:
    courses = json.loads((ROOT / "content" / "courses.json").read_text(encoding="utf-8"))
    bank = json.loads((ROOT / "content" / "vocabulary_bank.json").read_text(encoding="utf-8"))
    return courses, bank


def test_course_catalog_includes_320_words_and_640_varied_exercises() -> None:
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
    assert len(demo_exercises) == 640
    assert {course["track"] for course in courses} == {"general", "business"}
    assert all(
        word["example"].strip()
        and word["word"] in word["example"]
        and "____" not in word["example"]
        and word["example_translation"].strip()
        for word in demo_words
    )
    assert all(
        exercise["sentence"].strip()
        and (
            exercise["options"] == []
            if exercise.get("answer_mode") == "text"
            else exercise["correct_answer"] in exercise["options"]
        )
        and exercise["explanation"].strip()
        for exercise in demo_exercises
    )
    assert all(
        len({exercise["sentence"] for exercise in lesson["exercises"]}) >= 8
        for course in courses
        for lesson in course["lessons"]
        if lesson["id"].startswith("catalog-")
    )
    home_lesson = next(
        lesson
        for course in courses
        for lesson in course["lessons"]
        if lesson["id"] == "catalog-general-1-1"
    )
    assert [exercise["sentence"] for exercise in home_lesson["exercises"]] == [
        "They bought a ____ with a garden and a front door.",
        "Оберіть український переклад слова “house”:",
        "Оберіть український переклад слова “apartment”:",
        "Оберіть англійський відповідник до «квартира»:",
        "Оберіть англійський відповідник до «кімната»:",
        "Оберіть правильний переклад речення:\nThere is a window in every room of this apartment.",
        "Оберіть правильний переклад речення:\nI made breakfast in the kitchen.",
        "Напишіть англійське слово для перекладу «кухня»:",
        "Напишіть англійське слово для перекладу «спальня»:",
        "She sleeps in the ____ and cooks in the kitchen.",
    ]
    assert [exercise["answer_mode"] for exercise in home_lesson["exercises"]] == [
        "choice",
        "choice",
        "choice",
        "choice",
        "choice",
        "choice",
        "choice",
        "text",
        "text",
        "choice",
    ]
    assert {exercise["correct_answer"] for exercise in home_lesson["exercises"]} == {
        "house",
        "будинок",
        "квартира",
        "apartment",
        "room",
        "У цій квартирі в кожній кімнаті є вікно.",
        "Я приготував сніданок на кухні.",
        "kitchen",
        "bedroom",
    }

    answer_positions = [
        exercise["options"].index(exercise["correct_answer"])
        for exercise in demo_exercises
        if exercise["answer_mode"] == "choice"
    ]
    assert set(answer_positions) == {0, 1, 2}
    assert sum(exercise["answer_mode"] == "text" for exercise in demo_exercises) == 128
    assert all(
        len(course["placement_test"]) == 24
        and {question["level"] for question in course["placement_test"]}
        == {"A1", "A2", "B1", "B2", "C1", "C2"}
        and {question["skill"] for question in course["placement_test"]}
        == {"grammar", "vocabulary", "reading", "usage"}
        and len({question["correct_index"] for question in course["placement_test"]}) > 1
        for course in courses
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
