from __future__ import annotations

import re
from typing import Any


_SLUG = re.compile(r"[^a-z0-9]+")
_UK_PARTS = {
    "noun": "іменник",
    "verb": "дієслово",
    "adjective": "прикметник",
}
_GENERAL_ADJECTIVE_EXAMPLES = {
    "happy": "Щаслива людина сьогодні усміхається.",
    "sad": "Сумна людина сьогодні потребує підтримки.",
    "tired": "Втомлена людина сьогодні відпочиває.",
    "busy": "Зайнята людина сьогодні працює.",
    "careful": "Обережна людина перевіряє деталі.",
    "calm": "Спокійна людина говорить тихо.",
    "confident": "Упевнена людина говорить чітко.",
    "excited": "Схвильована людина чекає на подію.",
    "nervous": "Нервова людина готується до зустрічі.",
    "ready": "Готова людина може починати.",
    "safe": "Ця людина зараз у безпеці.",
    "comfortable": "Ця людина почувається комфортно.",
    "healthy": "Здорова людина має більше сил.",
    "different": "Ця людина не схожа на інших.",
    "friendly": "Привітна людина радо допомагає.",
    "kind": "Добра людина допомагає іншим.",
    "polite": "Ввічлива людина чемно вітається.",
    "quiet": "Тиха людина уважно слухає.",
    "loud": "Гучна людина говорить голосно.",
    "strong": "Сильна людина може нести валізу.",
}


def build_course_catalog(
    courses: list[dict[str, Any]], word_bank: dict[str, Any]
) -> list[dict[str, Any]]:
    result = [dict(course) for course in courses]
    track_courses = {course["track"]: course for course in result}
    for track in ("general", "business"):
        if track not in track_courses:
            raise ValueError(f"The course for track {track!r} is missing.")
        course = track_courses[track]
        legacy_prefix = f"demo-{track}-"
        catalog_prefix = f"catalog-{track}-"
        course["lessons"] = [
            lesson
            for lesson in course["lessons"]
            if not lesson["id"].startswith((legacy_prefix, catalog_prefix))
        ]
        existing_words = {
            word["word"].casefold()
            for lesson in course["lessons"]
            for word in lesson["vocabulary"]
        }
        entries: list[tuple[str, str, str, str, str]] = []
        seen: set[str] = set(existing_words)
        for category_index, category in enumerate(word_bank.get(track, []), start=1):
            for word, translation, part, level in _category_entries(category):
                normalized = word.casefold()
                if normalized in seen:
                    raise ValueError(f"Duplicate vocabulary word {word!r} in the {track} course.")
                seen.add(normalized)
                entries.append((word, translation, part, level, category["title"]))
        if not entries:
            raise ValueError(f"The vocabulary bank for track {track!r} is empty.")

        order = max((lesson["order"] for lesson in course["lessons"]), default=0) + 1
        words_per_lesson = 5
        categories = word_bank[track]
        for category_index, category in enumerate(categories, start=1):
            category_entries = [
                entry for entry in entries if entry[4] == category["title"]
            ]
            for offset in range(0, len(category_entries), words_per_lesson):
                group = category_entries[offset : offset + words_per_lesson]
                part_index = offset // words_per_lesson + 1
                lesson_id = f"{catalog_prefix}{category_index}-{part_index}"
                vocabulary: list[dict[str, str]] = []
                exercises: list[dict[str, Any]] = []
                for word, translation, part, level, _ in group:
                    word_id = f"catalog-{track}-{_slug_word(word)}"
                    example, example_translation = _example(
                        word, translation, part, track
                    )
                    vocabulary.append(
                        {
                            "id": word_id,
                            "word": word,
                            "translation": translation,
                            "part_of_speech": _UK_PARTS[part],
                            "example": example,
                            "example_translation": example_translation,
                        }
                    )
                    options = _exercise_options(
                        word, part, level, entries
                    )
                    blank = example.replace(word, "____", 1)
                    exercises.append(
                        {
                            "id": f"exercise-{word_id}",
                            "sentence": blank,
                            "options": options,
                            "correct_answer": word,
                            "explanation": (
                                f"«{word}» означає «{translation}». "
                                "У цьому реченні за змістом підходить саме це слово."
                            ),
                            "translation": example_translation,
                            "max_attempts": 2,
                        }
                    )
                course["lessons"].append(
                    {
                        "id": lesson_id,
                        "order": order,
                        "level": category["level"],
                        "title": f"{category['title']} — частина {part_index}",
                        "objectives": (
                            f"Вивчити слова з теми «{category['title']}» "
                            "та потренувати їх у реченнях."
                        ),
                        "explanation": (
                            "Прочитайте слова з перекладом і прикладами, "
                            "а потім оберіть слово, яке найкраще підходить до речення."
                        ),
                        "vocabulary": vocabulary,
                        "exercises": exercises,
                    }
                )
                order += 1
        course["lessons"].sort(key=lambda lesson: lesson["order"])
    return result


def _category_entries(
    category: dict[str, Any],
) -> list[tuple[str, str, str, str]]:
    entries: list[tuple[str, str, str, str]] = []
    for row in category.get("words", []):
        if not isinstance(row, list) or len(row) != 3:
            raise ValueError(f"Each word in {category.get('title', 'course content')!r} needs word, translation, and part of speech.")
        word, translation, part = row
        if part not in _UK_PARTS:
            raise ValueError(f"Unsupported part of speech: {part!r}.")
        if not all(isinstance(value, str) and value.strip() for value in (word, translation)):
            raise ValueError("Vocabulary words and translations must be non-empty text.")
        entries.append((word, translation, part, category["level"]))
    if not category.get("title") or category.get("level") not in {"A1", "A2", "B1", "B2", "C1"}:
        raise ValueError("Each vocabulary topic needs a title and valid CEFR level.")
    return entries


def _example(word: str, translation: str, part: str, track: str) -> tuple[str, str]:
    if part == "noun":
        return (
            f"We discussed the {word} today.",
            f"Сьогодні ми обговорили тему «{translation}».",
        )
    if part == "verb":
        return (
            f"I want to {word} today.",
            f"Сьогодні я хочу {translation}.",
        )
    if track == "general":
        sentence = _GENERAL_ADJECTIVE_EXAMPLES.get(word)
        if not sentence:
            raise ValueError(f"Add an example sentence for the general adjective {word!r}.")
        return (
            f"The person seems {word} today.",
            sentence,
        )
    return (
        f"The plan is {word}.",
        f"План сьогодні {translation}.",
    )


def _exercise_options(
    correct: str,
    part: str,
    level: str,
    entries: list[tuple[str, str, str, str, str]],
) -> list[str]:
    candidates = [
        word
        for word, _, candidate_part, candidate_level, _ in entries
        if candidate_part == part and candidate_level == level and word != correct
    ]
    if len(candidates) < 2:
        candidates = [
            word
            for word, _, candidate_part, _, _ in entries
            if candidate_part == part and word != correct
        ]
    if len(candidates) < 2:
        raise ValueError(f"Not enough vocabulary distractors for {correct!r}.")
    return [correct, *candidates[:2]]


def _slug_word(word: str) -> str:
    return _SLUG.sub("-", word.casefold()).strip("-")
