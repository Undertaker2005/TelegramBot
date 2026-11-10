from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from telegram_bot.reference_data import CEFR_LEVELS


_REQUIRED_TEXT_FIELDS = ("id", "title", "use", "comparison")
_FORM_KEYS = ("affirmative", "negative", "question")


def load_tense_course(path: Path) -> list[dict[str, Any]]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Could not read the English tense course at {path}.") from exc
    if not isinstance(raw, list):
        raise ValueError("The English tense course must be a JSON list.")

    lessons: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for index, lesson in enumerate(raw):
        if not isinstance(lesson, dict) or any(
            not isinstance(lesson.get(field), str) or not lesson[field].strip()
            for field in _REQUIRED_TEXT_FIELDS
        ):
            raise ValueError(f"Tense lesson {index + 1} is missing required text fields.")
        if lesson["id"] in seen_ids:
            raise ValueError(f"Duplicate tense lesson ID: {lesson['id']}")
        seen_ids.add(lesson["id"])
        if not isinstance(lesson.get("levels"), list) or not lesson["levels"] or any(
            level not in CEFR_LEVELS for level in lesson["levels"]
        ):
            raise ValueError(f"Tense lesson {lesson['id']} has invalid CEFR levels.")
        if not isinstance(lesson.get("forms"), dict) or any(
            not isinstance(lesson["forms"].get(key), str) or not lesson["forms"][key].strip()
            for key in _FORM_KEYS
        ):
            raise ValueError(f"Tense lesson {lesson['id']} needs all three sentence forms.")
        if not isinstance(lesson.get("examples"), list) or not lesson["examples"]:
            raise ValueError(f"Tense lesson {lesson['id']} needs at least one example.")
        if not isinstance(lesson.get("exercises"), list) or not lesson["exercises"]:
            raise ValueError(f"Tense lesson {lesson['id']} needs at least one exercise.")
        for exercise in lesson["exercises"]:
            options = exercise.get("options")
            answer = exercise.get("answer")
            if (
                not isinstance(exercise.get("prompt"), str)
                or not isinstance(answer, str)
                or not isinstance(options, list)
                or options.count(answer) != 1
                or len(set(options)) != len(options)
            ):
                raise ValueError(f"Tense lesson {lesson['id']} contains an invalid exercise.")
        lessons.append(lesson)
    return lessons
