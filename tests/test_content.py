import json
from pathlib import Path

import pytest

from telegram_bot.content import parse_course_json, validate_course


@pytest.fixture
def demo_courses() -> list[dict]:
    path = Path(__file__).resolve().parents[1] / "content" / "courses.json"
    return json.loads(path.read_text(encoding="utf-8"))


def test_both_demo_directions_are_valid_and_have_prepared_explanations(demo_courses: list[dict]) -> None:
    validated = [validate_course(course) for course in demo_courses]

    assert {course["track"] for course in validated} == {"general", "business"}
    assert all(
        exercise["explanation"].strip()
        for course in validated
        for lesson in course["lessons"]
        for exercise in lesson["exercises"]
    )
    assert all(lesson.get("video") is None for course in validated for lesson in course["lessons"])


@pytest.mark.parametrize(
    "url",
    ["http://example.org/video", "javascript:alert(1)", "https://", 5],
)
def test_content_rejects_untrusted_or_invalid_video_urls(demo_courses: list[dict], url: object) -> None:
    course = json.loads(json.dumps(demo_courses[0]))
    course["lessons"][0]["video"] = {"title": "Sample", "url": url}

    with pytest.raises(ValueError):
        validate_course(course)


def test_content_requires_answer_and_explanation(demo_courses: list[dict]) -> None:
    course = json.loads(json.dumps(demo_courses[0]))
    course["lessons"][0]["exercises"][0]["correct_answer"] = "not an option"

    with pytest.raises(ValueError):
        validate_course(course)


def test_import_rejects_malformed_json() -> None:
    with pytest.raises(ValueError, match="valid UTF-8 JSON"):
        parse_course_json(b"{not-json")
