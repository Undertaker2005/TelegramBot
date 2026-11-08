from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urlparse


_COURSE_ID = re.compile(r"^[a-z0-9_-]{2,64}$")
_LEVELS = {"A1", "A2", "B1", "B2", "C1", "C2"}


def validate_course(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ValueError("A course file must contain one JSON object.")
    required = {"id", "language", "track", "title", "description", "level", "placement_test", "lessons"}
    missing = required - data.keys()
    if missing:
        raise ValueError(f"Course is missing required fields: {', '.join(sorted(missing))}.")
    if not isinstance(data["id"], str) or not _COURSE_ID.fullmatch(data["id"]):
        raise ValueError("Course id must use 2-64 lowercase letters, digits, underscores, or hyphens.")
    if data["language"] != "en" or data["track"] not in {"general", "business"}:
        raise ValueError("MVP content must use language 'en' and track 'general' or 'business'.")
    if not all(isinstance(data[key], str) and data[key].strip() for key in ("title", "description")):
        raise ValueError("Course title and description must be non-empty text.")
    if data["level"] not in _LEVELS:
        raise ValueError("Course level must be one of A1, A2, B1, B2, C1, or C2.")

    questions = data["placement_test"]
    if not isinstance(questions, list) or not questions:
        raise ValueError("A course must include at least one placement question.")
    _unique_ids(questions, "placement_test")
    for question in questions:
        if question.get("level") not in _LEVELS:
            raise ValueError("Each placement question needs a CEFR level.")
        if not isinstance(question.get("prompt"), str) or not question["prompt"].strip():
            raise ValueError("Each placement question needs a prompt.")
        options = question.get("options")
        correct_index = question.get("correct_index")
        if (
            not isinstance(options, list)
            or len(options) < 2
            or not all(isinstance(option, str) and option.strip() for option in options)
            or not isinstance(correct_index, int)
            or isinstance(correct_index, bool)
            or not 0 <= correct_index < len(options)
        ):
            raise ValueError("Placement questions need at least two options and a valid correct_index.")

    lessons = data["lessons"]
    if not isinstance(lessons, list) or not lessons:
        raise ValueError("A published course must include at least one lesson.")
    _unique_ids(lessons, "lessons")
    orders: set[int] = set()
    vocabulary_ids: set[str] = set()
    for lesson in lessons:
        if not isinstance(lesson.get("order"), int) or lesson["order"] < 1:
            raise ValueError("Every lesson needs a positive integer order.")
        if lesson["order"] in orders:
            raise ValueError("Lesson order values must be unique.")
        orders.add(lesson["order"])
        if lesson.get("level") not in _LEVELS:
            raise ValueError("Each lesson needs a CEFR level.")
        for field in ("title", "objectives", "explanation"):
            if not isinstance(lesson.get(field), str) or not lesson[field].strip():
                raise ValueError(f"Every lesson needs non-empty {field}.")
        video = lesson.get("video")
        if video is not None:
            url = video.get("url") if isinstance(video, dict) else None
            parsed = urlparse(url) if isinstance(url, str) else None
            if parsed is None or parsed.scheme != "https" or not parsed.netloc:
                raise ValueError("Video links must use HTTPS and include a hostname.")
            if not isinstance(video.get("title"), str) or not video["title"].strip():
                raise ValueError("Video links need a resource title.")
        words = lesson.get("vocabulary")
        if not isinstance(words, list):
            raise ValueError("Every lesson needs a vocabulary list (it may be empty).")
        _unique_ids(words, f"vocabulary in lesson {lesson['id']}")
        for word in words:
            for field in ("word", "translation", "example", "example_translation"):
                if not isinstance(word.get(field), str) or not word[field].strip():
                    raise ValueError(f"Vocabulary items need non-empty {field}.")
            if word["id"] in vocabulary_ids:
                raise ValueError("Vocabulary ids must be unique within a course.")
            vocabulary_ids.add(word["id"])
        exercises = lesson.get("exercises")
        if not isinstance(exercises, list) or not exercises:
            raise ValueError("Every lesson needs at least one exercise.")
        _unique_ids(exercises, f"exercises in lesson {lesson['id']}")
        for exercise in exercises:
            if not isinstance(exercise.get("sentence"), str) or not exercise["sentence"].strip():
                raise ValueError("Every exercise needs a non-empty question.")
            answer_mode = exercise.get("answer_mode", "choice")
            if answer_mode not in {"choice", "text"}:
                raise ValueError("Exercise answer_mode must be 'choice' or 'text'.")
            options = exercise.get("options")
            if answer_mode == "text":
                if (
                    options != []
                    or not isinstance(exercise.get("correct_answer"), str)
                    or not exercise["correct_answer"].strip()
                ):
                    raise ValueError("Text-answer exercises need empty options and a non-empty correct_answer.")
            elif (
                not isinstance(options, list)
                or len(options) < 2
                or not all(isinstance(option, str) and option.strip() for option in options)
                or exercise.get("correct_answer") not in options
            ):
                raise ValueError("Choice exercises need at least two options and a correct_answer from those options.")
            if not isinstance(exercise.get("explanation"), str) or not exercise["explanation"].strip():
                raise ValueError("Every exercise needs a prepared explanation.")
            if not isinstance(exercise.get("max_attempts", 1), int) or exercise.get("max_attempts", 1) < 1:
                raise ValueError("Exercise max_attempts must be a positive integer.")

    return data


def parse_course_json(raw: bytes) -> dict[str, Any]:
    try:
        decoded = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("The uploaded content must be a valid UTF-8 JSON file.") from exc
    return validate_course(decoded)


def _unique_ids(items: list[Any], label: str) -> None:
    if not all(isinstance(item, dict) and isinstance(item.get("id"), str) and item["id"].strip() for item in items):
        raise ValueError(f"Every item in {label} needs a non-empty id.")
    ids = [item["id"] for item in items]
    if len(set(ids)) != len(ids):
        raise ValueError(f"Ids in {label} must be unique.")
