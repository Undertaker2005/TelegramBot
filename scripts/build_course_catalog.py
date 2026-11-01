from __future__ import annotations

import json
from pathlib import Path

from telegram_bot.content import validate_course
from telegram_bot.course_catalog import build_course_catalog


ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    courses_path = ROOT / "content" / "courses.json"
    word_bank_path = ROOT / "content" / "vocabulary_bank.json"
    courses = json.loads(courses_path.read_text(encoding="utf-8"))
    word_bank = json.loads(word_bank_path.read_text(encoding="utf-8"))
    expanded = [validate_course(course) for course in build_course_catalog(courses, word_bank)]
    courses_path.write_text(
        json.dumps(expanded, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    new_words = sum(
        len(lesson["vocabulary"])
        for course in expanded
        for lesson in course["lessons"]
        if lesson["id"].startswith("catalog-")
    )
    new_exercises = sum(
        len(lesson["exercises"])
        for course in expanded
        for lesson in course["lessons"]
        if lesson["id"].startswith("catalog-")
    )
    print(f"Built {new_words} vocabulary entries, {new_exercises} exercises, {len(expanded)} courses.")


if __name__ == "__main__":
    main()
